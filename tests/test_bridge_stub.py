"""Tests for the stub (stub/flame_mcp_bridge.py).

The stub's name collides with the `src/flame_mcp_bridge` package, so each test loads it
with importlib under a fresh alias (flame_mcp_bridge_stub_under_test_<uuid>).
The stub runs `import flame` inside each function, so a fake module placed in
sys.modules["flame"] lets the tests run without a real Flame.
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import logging
import os
import shutil
import sys
import types
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import server as server_mod

STUB_PATH = Path(__file__).resolve().parents[1] / "stub" / "flame_mcp_bridge.py"
SCRATCH_ROOT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "p12s"

GOOD_BODY_V1 = """
BRIDGE_ABI = 1
TAG = "v1"

class FakeBridge:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.stopped = False
        self.tag = "v1"
        self.fail_on_restart = False
        self.project_name = kwargs.get("project_name")

    def start(self):
        if self.fail_on_restart:
            raise RuntimeError("simulated old-bridge restart failure")
        self.stopped = False

    def stop(self):
        self.stopped = True

    def set_project_name(self, project_name):
        self.project_name = project_name

    def status(self):
        return {"alive": not self.stopped, "tag": self.tag, "project_name": self.project_name}


def start(schedule, flame_module, version, project_name, run_dir=None,
          results_dir=None, log_path=None):
    return FakeBridge(
        schedule=schedule, flame_module=flame_module, version=version,
        project_name=project_name,
    )
"""

GOOD_BODY_V2 = GOOD_BODY_V1.replace('TAG = "v1"', 'TAG = "v2"').replace(
    'self.tag = "v1"', 'self.tag = "v2"'
)

BAD_SYNTAX_BODY = "def start(:\n    this is not python\n"

WRONG_ABI_BODY = """
BRIDGE_ABI = 999

def start(**kwargs):
    return None
"""

START_FAILS_BODY = """
BRIDGE_ABI = 1

def start(schedule, flame_module, version, project_name, run_dir=None,
          results_dir=None, log_path=None):
    raise RuntimeError("simulated new-body start failure")
"""


class FakeMessages:
    def __init__(self) -> None:
        self.dialogs: list[dict[str, Any]] = []

    def show_in_dialog(self, title: str, message: str, type: str, buttons: list[str]) -> str | None:
        self.dialogs.append({"title": title, "message": message, "type": type, "buttons": buttons})
        return buttons[0] if buttons else None


class FakeFlameModule:
    def __init__(self, version: str = "2025.2.4") -> None:
        self._version = version
        self.messages = FakeMessages()
        self.schedule_idle_event = lambda cb: None

    def get_version(self) -> str:
        return self._version


def _load_stub_module() -> Any:
    unique_name = f"flame_mcp_bridge_stub_under_test_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(unique_name, STUB_PATH)
    assert spec is not None and spec.loader is not None
    module = types.ModuleType(unique_name)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bridge_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    d = SCRATCH_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    # The stub refuses a group- or other-writable directory or body, so set the modes that
    # install_bridge.sh sets instead of leaving them to the umask (002 on many Linux accounts).
    d.chmod(0o700)
    monkeypatch.setenv("FLAME_MCP_BRIDGE_DIR", str(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def stub_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    module = _load_stub_module()
    yield module
    sys.modules.pop(module.__name__, None)
    sys.modules.pop("flame_mcp_bridge_impl", None)
    monkeypatch.delitem(sys.modules, "flame", raising=False)


def _write_impl(bridge_dir: Path, body: str) -> None:
    (bridge_dir / "impl.py").write_text(body, encoding="utf-8")
    (bridge_dir / "impl.py").chmod(0o600)


def test_menu_always_registered_before_any_start(stub_module: Any) -> None:
    actions = stub_module.get_main_menu_custom_ui_actions()
    assert len(actions) == 1
    assert actions[0]["name"] == "MCP Bridge"
    names = [a["name"] for a in actions[0]["actions"]]
    assert names == ["Reload bridge", "Show status", "Show last error"]


def test_load_impl_success_starts_bridge(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)

    stub_module.app_initialized("proj1")

    assert stub_module._state["error"] is None
    assert stub_module._state["bridge"] is not None
    assert stub_module._state["bridge"].tag == "v1"


def test_body_syntax_error_keeps_menu_and_holds_error(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, BAD_SYNTAX_BODY)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)

    stub_module.app_initialized("proj1")

    assert stub_module._state["bridge"] is None
    assert stub_module._state["error"] is not None
    assert "SyntaxError" in stub_module._state["error"]
    # Menu must still be available even though the body failed to load.
    actions = stub_module.get_main_menu_custom_ui_actions()
    assert actions[0]["name"] == "MCP Bridge"


def test_abi_mismatch_holds_error_and_does_not_start(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, WRONG_ABI_BODY)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)

    stub_module.app_initialized("proj1")

    assert stub_module._state["bridge"] is None
    assert "BRIDGE_ABI mismatch" in stub_module._state["error"]


def test_untested_version_still_starts_and_warns_once(
    stub_module: Any,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("9999.9.9")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)

    stub_module.app_initialized("proj1")
    stub_module.app_initialized("proj1")

    assert stub_module._state["error"] is None
    assert stub_module._state["bridge"] is not None
    assert stub_module._state["untested_version"] is True
    err = capsys.readouterr().err
    assert err.count("is not a tested version") == 1
    assert "flame-mcp: Flame 9999.9.9 is not a tested version (tested: 2025.2.4, 2026.2.3)" in err


@pytest.mark.parametrize("version", ["2025.2.4", "2026.2.3"])
def test_tested_version_does_not_warn(
    stub_module: Any,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    version: str,
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule(version)
    monkeypatch.setitem(sys.modules, "flame", fake_flame)

    stub_module.app_initialized("proj1")

    assert stub_module._state["bridge"] is not None
    assert stub_module._state["untested_version"] is False
    captured = capsys.readouterr()
    assert "tested version" not in captured.err
    assert "tested version" not in captured.out


def test_app_initialized_twice_is_noop(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)

    stub_module.app_initialized("proj1")
    first_bridge = stub_module._state["bridge"]
    stub_module.app_initialized("proj1")
    assert stub_module._state["bridge"] is first_bridge
    assert first_bridge.stopped is False


def test_app_exited_stops_bridge_and_clears_state(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")
    bridge = stub_module._state["bridge"]

    stub_module.app_exited({"version": "2025.2.4"})

    assert bridge.stopped is True
    assert stub_module._state["bridge"] is None


def test_reload_with_broken_new_body_keeps_old_running(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")
    old_bridge = stub_module._state["bridge"]
    assert old_bridge is not None

    _write_impl(bridge_dir, BAD_SYNTAX_BODY)
    stub_module._action_reload(None)

    assert stub_module._state["bridge"] is old_bridge
    assert old_bridge.stopped is False
    assert "SyntaxError" in stub_module._state["error"]
    assert fake_flame.messages.dialogs[-1]["type"] == "error"


def test_reload_success_stops_old_and_starts_new(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")
    old_bridge = stub_module._state["bridge"]

    _write_impl(bridge_dir, GOOD_BODY_V2)
    ok = stub_module._do_reload()

    assert ok is True
    assert old_bridge.stopped is True
    new_bridge = stub_module._state["bridge"]
    assert new_bridge is not old_bridge
    assert new_bridge.tag == "v2"
    assert stub_module._state["error"] is None


def test_show_status_and_show_last_error_actions(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")

    stub_module._action_show_status(None)
    assert "Bridge status" in fake_flame.messages.dialogs[-1]["message"]

    stub_module._action_show_last_error(None)
    assert "(no error recorded)" in fake_flame.messages.dialogs[-1]["message"]


def test_every_dialog_text_is_ascii_even_when_the_status_holds_a_non_ascii_name(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj\u30c6")
    stub_module._state["error"] = "failed on \u30c6"

    stub_module._action_show_status(None)
    stub_module._action_show_last_error(None)
    stub_module._action_reload(None)

    assert len(fake_flame.messages.dialogs) == 3
    for dialog in fake_flame.messages.dialogs:
        assert dialog["title"].isascii() and dialog["message"].isascii(), dialog
    assert "\\u30c6" in fake_flame.messages.dialogs[1]["message"]


# A failed load must not leave sys.modules pointing at a half-built module.


def test_load_failure_restores_previous_sys_modules_entry(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")
    good_module = sys.modules["flame_mcp_bridge_impl"]
    assert good_module is stub_module._state["module"]

    _write_impl(bridge_dir, BAD_SYNTAX_BODY)
    ok = stub_module._do_reload()

    assert ok is False
    # The broken load must not have clobbered the sys.modules entry that was
    # there before the attempt.
    assert sys.modules["flame_mcp_bridge_impl"] is good_module


def test_load_failure_removes_sys_modules_entry_when_none_existed(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    assert "flame_mcp_bridge_impl" not in sys.modules

    _write_impl(bridge_dir, BAD_SYNTAX_BODY)
    stub_module.app_initialized("proj1")

    assert stub_module._state["bridge"] is None
    assert "flame_mcp_bridge_impl" not in sys.modules


# A failed reload falls back to restarting the old bridge.


def test_reload_start_failure_falls_back_to_restarting_old(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")
    old_bridge = stub_module._state["bridge"]

    _write_impl(bridge_dir, START_FAILS_BODY)
    ok = stub_module._do_reload()

    assert ok is False
    assert stub_module._state["bridge"] is old_bridge
    assert old_bridge.stopped is False  # was stopped then successfully restarted
    assert "restarted" in stub_module._state["error"]
    assert "simulated new-body start failure" in stub_module._state["error"]


def test_reload_start_failure_and_old_restart_failure_leaves_bridge_none(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")
    old_bridge = stub_module._state["bridge"]
    old_bridge.fail_on_restart = True

    _write_impl(bridge_dir, START_FAILS_BODY)
    ok = stub_module._do_reload()

    assert ok is False
    assert stub_module._state["bridge"] is None
    assert "failed to restart the previous one" in stub_module._state["error"]
    assert "simulated new-body start failure" in stub_module._state["error"]
    assert "simulated old-bridge restart failure" in stub_module._state["error"]


# app_initialized on an already-running bridge updates its project name.


def test_app_initialized_twice_with_different_project_updates_name(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("2025.2.4")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")
    bridge = stub_module._state["bridge"]

    stub_module.app_initialized("proj2")

    assert stub_module._state["bridge"] is bridge  # same instance, no restart
    assert stub_module._state["project_name"] == "proj2"
    assert bridge.project_name == "proj2"
    assert bridge.stopped is False


# The untested-version warning reaches bridge.log, because Flame shows neither print nor stderr.


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def bridge_log() -> Iterator[_ListHandler]:
    logger = logging.getLogger("flame_mcp_bridge")
    handler = _ListHandler()
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def _untested_warnings(handler: _ListHandler) -> list[logging.LogRecord]:
    return [r for r in handler.records if "is not a tested version" in r.getMessage()]


def test_untested_version_warning_goes_to_the_bridge_logger_once(
    stub_module: Any,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    bridge_log: _ListHandler,
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    monkeypatch.setitem(sys.modules, "flame", FakeFlameModule("9999.9.9"))

    stub_module.app_initialized("proj1")
    stub_module.app_initialized("proj1")

    records = _untested_warnings(bridge_log)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == (
        "flame-mcp: Flame 9999.9.9 is not a tested version "
        "(tested: 2025.2.4, 2026.2.3); starting anyway"
    )


def test_untested_version_warning_is_not_repeated_after_a_reload(
    stub_module: Any,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    bridge_log: _ListHandler,
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    monkeypatch.setitem(sys.modules, "flame", FakeFlameModule("9999.9.9"))
    stub_module.app_initialized("proj1")
    assert len(_untested_warnings(bridge_log)) == 1

    _write_impl(bridge_dir, GOOD_BODY_V2)
    assert stub_module._do_reload() is True

    assert len(_untested_warnings(bridge_log)) == 1


def test_tested_version_logs_no_warning(
    stub_module: Any,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    bridge_log: _ListHandler,
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    monkeypatch.setitem(sys.modules, "flame", FakeFlameModule("2025.2.4"))

    stub_module.app_initialized("proj1")

    assert _untested_warnings(bridge_log) == []


def test_failing_print_does_not_stop_the_bridge_or_the_log_line(
    stub_module: Any,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    bridge_log: _ListHandler,
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    monkeypatch.setitem(sys.modules, "flame", FakeFlameModule("9999.9.9"))

    def broken_print(*args: Any, **kwargs: Any) -> None:
        raise OSError("stderr is closed")

    monkeypatch.setattr(builtins, "print", broken_print)

    stub_module.app_initialized("proj1")

    assert stub_module._state["error"] is None
    assert stub_module._state["bridge"] is not None
    assert len(_untested_warnings(bridge_log)) == 1


def test_stub_and_server_agree_on_the_tested_flame_versions(stub_module: Any) -> None:
    assert stub_module._TESTED_FLAME_VERSIONS == server_mod.TESTED_FLAME_VERSIONS
    assert len(server_mod.TESTED_FLAME_VERSIONS) >= 1


def _literal_assignment(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = [
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        and any(
            isinstance(t, ast.Name) and t.id == name
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    assert len(values) == 1, f"{name} must be defined exactly once in {path}"
    return values[0]


def test_stub_and_server_source_files_define_the_same_tested_versions_once() -> None:
    server_path = Path(server_mod.__file__)
    from_server = _literal_assignment(server_path, "TESTED_FLAME_VERSIONS")
    from_stub = _literal_assignment(STUB_PATH, "_TESTED_FLAME_VERSIONS")
    assert isinstance(from_server, tuple) and isinstance(from_stub, tuple)
    assert all(isinstance(v, str) for v in from_server)
    assert from_stub == from_server


def test_show_status_names_all_tested_versions_when_the_version_is_untested(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    fake_flame = FakeFlameModule("9999.9.9")
    monkeypatch.setitem(sys.modules, "flame", fake_flame)
    stub_module.app_initialized("proj1")

    stub_module._action_show_status(None)

    message = fake_flame.messages.dialogs[-1]["message"]
    assert "Untested Flame version 9999.9.9 (tested: 2025.2.4, 2026.2.3)" in message


# -- the body file is only loaded when it is ours and not writable by others --


def _start_with(stub_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "flame", FakeFlameModule("2025.2.4"))
    stub_module.app_initialized("proj1")


def _assert_refused(stub_module: Any, fragment: str) -> None:
    assert stub_module._state["bridge"] is None
    error = stub_module._state["error"]
    assert error is not None and fragment in error
    assert error.isascii()
    assert "flame_mcp_bridge_impl" not in sys.modules


def test_a_symlinked_body_is_not_loaded(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = bridge_dir.parent / f"real_{uuid.uuid4().hex[:6]}.py"
    real.write_text(GOOD_BODY_V1, encoding="utf-8")
    (bridge_dir / "impl.py").symlink_to(real)
    try:
        _start_with(stub_module, monkeypatch)
        _assert_refused(stub_module, "symlink")
    finally:
        real.unlink()


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666, 0o620])
def test_a_body_writable_by_group_or_other_is_not_loaded(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    (bridge_dir / "impl.py").chmod(mode)
    _start_with(stub_module, monkeypatch)
    _assert_refused(stub_module, "writable")


def test_a_body_owned_by_someone_else_is_not_loaded(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    _start_with(stub_module, monkeypatch)
    _assert_refused(stub_module, "is owned by uid")


def test_a_body_in_a_directory_writable_by_others_is_not_loaded(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    bridge_dir.chmod(0o777)
    _start_with(stub_module, monkeypatch)
    _assert_refused(stub_module, "writable")


def test_a_private_body_is_loaded(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    (bridge_dir / "impl.py").chmod(0o600)
    bridge_dir.chmod(0o700)
    _start_with(stub_module, monkeypatch)
    assert stub_module._state["error"] is None
    assert stub_module._state["bridge"] is not None


def test_reload_refuses_a_body_that_became_group_writable_and_keeps_the_old_bridge(
    stub_module: Any, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_impl(bridge_dir, GOOD_BODY_V1)
    _start_with(stub_module, monkeypatch)
    old = stub_module._state["bridge"]
    (bridge_dir / "impl.py").chmod(0o666)

    assert stub_module._do_reload() is False

    assert stub_module._state["bridge"] is old
    assert "writable" in stub_module._state["error"]
    assert old.stopped is False


def test_stub_parses_under_python_3_9() -> None:
    import ast

    source = STUB_PATH.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))
