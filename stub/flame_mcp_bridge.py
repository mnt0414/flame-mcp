"""The flame-mcp bridge stub: the only file placed under /opt/Autodesk.

The bridge body lives in impl.py. Deployed as a real file copy (never a
symlink) to /opt/Autodesk/shared/python/flame_mcp_bridge.py, or read via
DL_PYTHON_HOOK_PATH during development. Must stay parseable under the Python
3.9.7 that Flame 2023.3.2 also uses to scan shared/python/: no `match`, no
runtime `X | Y`. Everything the body needs lives in impl.py, loaded from
FLAME_MCP_BRIDGE_DIR (default ~/Library/Application Support/flame-mcp/bridge).

Annotations use typing.Optional/Dict rather than 3.10+ `X | None` / builtin
generics, and `from __future__ import annotations` keeps them as unevaluated
strings -- safe under Python 3.9 either way.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import traceback
import types
from typing import Any, Dict, Optional

_TESTED_FLAME_VERSIONS = ("2025.2.4", "2026.2.3")
_TESTED_VERSIONS_TEXT = ", ".join(_TESTED_FLAME_VERSIONS)
_EXPECTED_BRIDGE_ABI = 1
_IMPL_MODULE_NAME = "flame_mcp_bridge_impl"
_LOGGER_NAME = "flame_mcp_bridge"

_state: Dict[str, Any] = {
    "module": None,
    "bridge": None,
    "error": None,
    "version": None,
    "project_name": None,
    "untested_version": False,
    "untested_warning_emitted": False,
}


def _bridge_dir() -> str:
    # FLAME_MCP_BRIDGE_DIR is for development only (a second body next to the installed one);
    # the ownership and permission checks below apply to it as well.
    override = os.environ.get("FLAME_MCP_BRIDGE_DIR")
    if override:
        return override
    return os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "flame-mcp", "bridge"
    )


_UNSAFE_WRITE_BITS = 0o022


def _check_not_tamperable(path: str, st: os.stat_result, what: str) -> None:
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing to load: {what} is a symlink: {path}")
    if st.st_uid != os.getuid():
        raise RuntimeError(
            f"refusing to load: {what} is owned by uid {st.st_uid}, not by uid {os.getuid()}: "
            f"{path}"
        )
    if st.st_mode & _UNSAFE_WRITE_BITS:
        raise RuntimeError(
            f"refusing to load: {what} is writable by group or other "
            f"(mode {stat.S_IMODE(st.st_mode):04o}): {path}"
        )


def _read_trusted_source(impl_path: str) -> str:
    """Reads impl.py only if it is a regular file we own that no one else can write, in a
    directory no one else can write."""
    _check_not_tamperable(impl_path, os.lstat(impl_path), "impl.py")
    directory = os.path.dirname(impl_path) or "."
    _check_not_tamperable(directory, os.lstat(directory), "the bridge directory")
    fd = os.open(impl_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"refusing to load: impl.py is not a regular file: {impl_path}")
        _check_not_tamperable(impl_path, st, "impl.py")
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read()
    finally:
        os.close(fd)
    return raw.decode("utf-8")


def _load_impl() -> Any:
    impl_path = os.path.join(_bridge_dir(), "impl.py")
    source = _read_trusted_source(impl_path)
    module = types.ModuleType(_IMPL_MODULE_NAME)
    module.__file__ = impl_path
    # dataclasses etc. in impl.py look themselves up via sys.modules[cls.__module__],
    # so the module must be registered before exec() runs. But if compile/exec/the
    # ABI check below fails, that leaves sys.modules pointing at a half-built,
    # broken module forever unless whatever was there before is put back (or the
    # key removed if there was nothing) on the way out.
    previous = sys.modules.get(_IMPL_MODULE_NAME)
    sys.modules[_IMPL_MODULE_NAME] = module
    try:
        code = compile(source, impl_path, "exec")
        exec(code, module.__dict__)
        abi = getattr(module, "BRIDGE_ABI", None)
        if abi != _EXPECTED_BRIDGE_ABI:
            raise RuntimeError(
                f"BRIDGE_ABI mismatch: stub expects {_EXPECTED_BRIDGE_ABI!r}, impl.py has {abi!r}"
            )
    except BaseException:
        if previous is not None:
            sys.modules[_IMPL_MODULE_NAME] = previous
        else:
            sys.modules.pop(_IMPL_MODULE_NAME, None)
        raise
    return module


def _start_module(module: Any, flame_mod: Any, version: str, project_name: Optional[str]) -> Any:
    return module.start(
        schedule=flame_mod.schedule_idle_event,
        flame_module=flame_mod,
        version=version,
        project_name=project_name,
    )


def _note_untested_version(version: str) -> None:
    if version in _TESTED_FLAME_VERSIONS:
        _state["untested_version"] = False
        return
    _state["untested_version"] = True


def _warn_untested_version_once() -> None:
    if not _state["untested_version"] or _state["untested_warning_emitted"]:
        return
    _state["untested_warning_emitted"] = True
    message = (
        f"flame-mcp: Flame {_state['version']} is not a tested version "
        f"(tested: {_TESTED_VERSIONS_TEXT}); starting anyway"
    )
    # Flame shows neither print() nor stderr in app.log or shell.log; bridge.log
    # (the handler impl.py attaches to this logger when it starts) is where it lands.
    try:
        print(message, file=sys.stderr)
    except BaseException:
        pass
    try:
        logging.getLogger(_LOGGER_NAME).warning(message)
    except BaseException:
        pass


def _do_start(project_name: Optional[str]) -> None:
    try:
        import flame

        version = flame.get_version()
        _state["version"] = version
        _state["project_name"] = project_name
        _note_untested_version(version)
        if _state["bridge"] is not None:
            # app_initialized firing again for an already-running bridge (e.g. project
            # switched within the same Flame session) is a no-op restart-wise, but the
            # new project name is real and should be reflected in hello() without
            # touching flame.
            _state["bridge"].set_project_name(project_name)
            return
        module = _load_impl()
        bridge = _start_module(module, flame, version, project_name)
    except BaseException:
        _state["error"] = traceback.format_exc()
        return
    _state["module"] = module
    _state["bridge"] = bridge
    _state["error"] = None
    _warn_untested_version_once()


def _do_reload() -> bool:
    version = _state["version"]
    project_name = _state["project_name"]
    if version is None:
        _state["error"] = "cannot reload: bridge was never started (no known flame version)"
        return False
    try:
        new_module = _load_impl()
    except BaseException:
        _state["error"] = traceback.format_exc()
        return False

    old_bridge = _state["bridge"]
    if old_bridge is not None:
        try:
            old_bridge.stop()
        except BaseException:
            # Old bridge is in an unknown state; leave `_state` untouched
            # (still points at it) rather than guessing.
            _state["error"] = traceback.format_exc()
            return False

    try:
        import flame

        new_bridge = _start_module(new_module, flame, version, project_name)
    except BaseException:
        new_start_error = traceback.format_exc()
        if old_bridge is None:
            _state["error"] = new_start_error
            _state["module"] = new_module
            _state["bridge"] = None
            return False
        # The new body failed to start after the old one was already stopped to free
        # the socket; try to bring the old instance back rather than leaving the
        # bridge dead for no reason.
        try:
            old_bridge.start()
        except BaseException:
            restart_error = traceback.format_exc()
            _state["error"] = (
                "reload failed to start the new body, and failed to restart "
                f"the previous one:\nnew-body error:\n{new_start_error}\n"
                f"old-bridge restart error:\n{restart_error}"
            )
            _state["module"] = new_module
            _state["bridge"] = None
            return False
        _state["error"] = (
            "reload failed to start the new body; the previous bridge was "
            f"restarted instead:\n{new_start_error}"
        )
        # `_state["module"]` intentionally left as-is: the module `old_bridge` was
        # started from is what it is actually running.
        _state["bridge"] = old_bridge
        return False

    _state["module"] = new_module
    _state["bridge"] = new_bridge
    _state["error"] = None
    return True


def app_initialized(project_name: Optional[str]) -> None:
    _do_start(project_name)


def app_exited(info: Any) -> None:
    bridge = _state["bridge"]
    if bridge is not None:
        try:
            bridge.stop()
        except BaseException:
            _state["error"] = traceback.format_exc()
    _state["bridge"] = None


def _show_dialog(message: str, type_: str) -> None:
    import flame

    # Flame can crash on a 2-byte character; a project name or error text read back from
    # Flame may hold one, so it is shown as an escape.
    flame.messages.show_in_dialog(
        title="MCP Bridge",
        message=message.encode("ascii", "backslashreplace").decode("ascii"),
        type=type_,
        buttons=["OK"],
    )


def _action_reload(selection: Any) -> None:
    ok = _do_reload()
    if ok:
        _show_dialog("Bridge reloaded.", "info")
    else:
        _show_dialog(f"Reload failed:\n{_state['error']}", "error")


def _action_show_status(selection: Any) -> None:
    bridge = _state["bridge"]
    if bridge is None:
        message = "Bridge is not running."
    else:
        try:
            message = f"Bridge status:\n{bridge.status()}"
            if _state["untested_version"]:
                message += (
                    f"\nUntested Flame version {_state['version']} "
                    f"(tested: {_TESTED_VERSIONS_TEXT})"
                )
        except BaseException:
            message = f"Bridge status unavailable:\n{traceback.format_exc()}"
    _show_dialog(message, "info")


def _action_show_last_error(selection: Any) -> None:
    error = _state["error"]
    _show_dialog(error if error else "(no error recorded)", "info")


def get_main_menu_custom_ui_actions() -> Any:
    return [
        {
            "name": "MCP Bridge",
            "actions": [
                {"name": "Reload bridge", "execute": _action_reload},
                {"name": "Show status", "execute": _action_show_status},
                {"name": "Show last error", "execute": _action_show_last_error},
            ],
        }
    ]


get_main_menu_custom_ui_actions.minimum_version = "2023.1"  # type: ignore[attr-defined]
