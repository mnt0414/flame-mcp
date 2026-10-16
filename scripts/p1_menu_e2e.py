#!/usr/bin/env python3
"""Real-Flame end-to-end client for the menu (Reload bridge / Show last error).

Run with the `.venv` Python; started by `scripts/run_flame_with_bridge.sh
scripts/p1_menu_e2e.py`. Starting and exiting Flame and detecting crashes are the job of the
orchestrator; this script only covers the steps after the socket has appeared. It touches
Flame only through `flame_mcp.transport.exec` (main thread), and only as test scaffolding.

Steps:
  a. hello (record pid and startedAt)
  b. With exec, find the stub module in `sys.modules` (the one whose `__file__` ends with
     `dev_hooks/flame_mcp_bridge.py`). Record the module name and the contents of `_state`
     (error, whether a bridge exists, version, project_name).
  c. Reload succeeds: with exec, register
     `from PySide6.QtCore import QTimer; QTimer.singleShot(0, <stub>._do_reload)`
     and return at once (the menu's Reload is called on main, so the condition is the same)
     -> wait 2 s -> hello on a new connection -> startedAt has changed (a new instance) and
     the pid is the same.
  d. The old instance survives a failed Reload: move the dev deployment target
     `~/Library/Application Support/flame-mcp/bridge_dev/impl.py` (where
     scripts/deploy_bridge_dev.sh deploys; the orchestrator makes Flame read it through
     FLAME_MCP_BRIDGE_DIR; never touch the production
     `~/Library/Application Support/flame-mcp/bridge/impl.py`) aside into scratch and replace
     it with an impl.py that has a syntax error -> Reload as in c -> 2 s -> hello answers with
     the same startedAt as before (the old instance keeps running) -> with exec, check that
     `<stub>._state["error"]` holds the SyntaxError -> put the correct impl.py back -> Reload
     again -> startedAt changes. Always restore the correct impl.py at the end (try/finally),
     and record whether the sha256 after the restore equals that of the repository's impl.py.
  e. Dialog: with exec, register
     `QTimer.singleShot(0, lambda: <stub>._action_show_last_error(None))` -> wait 3 s -> exec
     `_result = 1` (timeout_s=5). If the response is ok, the dialog does not block main (it may
     have been answered automatically by `--no-confirm`); if it is TIMEOUT (queued), the dialog
     blocks main. Record which one happened (neither fails the step). If main is blocked, skip
     the later steps; the orchestrator stops Flame.
  f. Finally, exec `flame.exit()` (not run if e found main blocked).

The result is written to ~/Library/Application Support/flame-mcp/p1/p1_menu_e2e_<ts>.json.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import shutil
import socket
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import e2e_guard  # noqa: E402

from flame_mcp import transport  # noqa: E402

RUN_DIR_DEFAULT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "run"
OUTPUT_DIR = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p1"
SCRATCH_DIR = (
    Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "p1_menu_e2e"
)
# Development-only deployment (scripts/bridge_dev_env.sh's BRIDGE_DEV_DIR). Step d
# overwrites this file with broken source, so it must never point at the
# production `.../flame-mcp/bridge/impl.py` that a normal Flame session reads.
BRIDGE_DEV_IMPL_PATH = (
    Path.home() / "Library" / "Application Support" / "flame-mcp" / "bridge_dev" / "impl.py"
)
REPO_IMPL_PATH = REPO_ROOT / "src" / "flame_mcp_bridge" / "impl.py"

STUB_FILE_SUFFIX = "dev_hooks/flame_mcp_bridge.py"
BROKEN_IMPL_SOURCE = "!!! deliberately not valid python (p1_menu_e2e step d) !!!\n"

SOCKET_WAIT_S = 120.0
RELOAD_WAIT_S = 2.0
DIALOG_WAIT_S = 3.0
DIALOG_PROBE_TIMEOUT_S = 5.0
EXEC_TIMEOUT_S = 10.0


def record(results: dict[str, Any], name: str, ok: bool, **extra: Any) -> None:
    results["steps"].append({"name": name, "ok": ok, **extra})
    if not ok:
        results["ok"] = False


def _br_dict(br: transport.BridgeResult) -> dict[str, Any]:
    return {
        "ok": br.ok,
        "result": br.result,
        "errorCode": br.error_code,
        "error": br.error,
        "timeoutPhase": br.timeout_phase,
        "durationMs": br.duration_ms,
    }


def _find_socket(run_dir: Path, timeout_s: float) -> Path:
    deadline = time.monotonic() + timeout_s
    pattern = str(run_dir / "flame-mcp-*.sock")
    while time.monotonic() < deadline:
        matches = sorted(glob.glob(pattern))
        if matches:
            return Path(matches[0])
        time.sleep(1.0)
    raise TimeoutError(f"no bridge socket appeared under {run_dir} within {timeout_s}s")


def _raw_hello(sock_path: Path, timeout_s: float = 5.0) -> dict[str, Any]:
    """A `kind: hello` call over its own fresh connection -- `transport.exec`
    doesn't surface `startedAt`/`pid` (only `transport._discover`'s narrower
    `BridgeCandidate` does), and this script needs both to tell "still the
    same bridge instance" from "reloaded" apart."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_s)
        s.connect(str(sock_path))
        s.sendall(
            (json.dumps({"v": 1, "id": uuid.uuid4().hex, "kind": "hello"}) + "\n").encode("utf-8")
        )
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return {"ok": False, "protocolError": "connection closed with no response"}
        result: dict[str, Any] = json.loads(buf.decode("utf-8"))
        return result


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Code strings sent to the bridge (executed inside Flame, never here)
def _find_stub_snippet() -> str:
    """Locates the deployed stub by its dev_hooks path (this script only
    ever runs against `scripts/deploy_bridge_dev.sh`'s dev deployment, driven
    by `scripts/run_flame_with_bridge.sh`) -- never the production
    `/opt/Autodesk/shared/python` path. Binds `_stub`/`_stub_name` (None if
    not found) for the caller's own code to build `_result` from."""
    return "\n".join(
        [
            "import sys",
            "_stub = None",
            "_stub_name = None",
            "for _name, _mod in list(sys.modules.items()):",
            "    _f = getattr(_mod, '__file__', None)",
            f"    if isinstance(_f, str) and _f.endswith({STUB_FILE_SUFFIX!r}):",
            "        _stub = _mod",
            "        _stub_name = _name",
            "        break",
        ]
    )


def _find_stub_state_code() -> str:
    return "\n".join(
        [
            _find_stub_snippet(),
            "_st = dict(_stub._state) if _stub is not None else None",
            "_result = {",
            "    'moduleName': _stub_name,",
            "    'hasStub': _stub is not None,",
            "    'state': ({",
            "        'error': _st.get('error'),",
            "        'hasBridge': _st.get('bridge') is not None,",
            "        'version': _st.get('version'),",
            "        'projectName': _st.get('project_name'),",
            "    } if _st is not None else None),",
            "}",
        ]
    )


def _register_reload_code() -> str:
    """The menu's own `Reload bridge` action calls `_do_reload()` directly
    from `_action_reload`, itself invoked on the main thread by Flame. A
    `QTimer.singleShot(0, ...)` registered from inside a `flame_mcp` `exec`
    (also main thread) is the same condition: the
    callback runs on a later main-thread tick, after this call's own
    response has already gone out, exactly like the menu path."""
    return "\n".join(
        [
            _find_stub_snippet(),
            "_ok = False",
            "if _stub is not None:",
            "    from PySide6.QtCore import QTimer",
            "    QTimer.singleShot(0, _stub._do_reload)",
            "    _ok = True",
            "_result = _ok",
        ]
    )


def _find_stub_error_code() -> str:
    return "\n".join(
        [
            _find_stub_snippet(),
            "_result = {",
            "    'hasStub': _stub is not None,",
            "    'error': (_stub._state.get('error') if _stub is not None else None),",
            "}",
        ]
    )


def _register_show_last_error_dialog_code() -> str:
    return "\n".join(
        [
            _find_stub_snippet(),
            "_ok = False",
            "if _stub is not None:",
            "    from PySide6.QtCore import QTimer",
            "    QTimer.singleShot(0, lambda: _stub._action_show_last_error(None))",
            "    _ok = True",
            "_result = _ok",
        ]
    )


def step_a_hello(sock_path: Path, results: dict[str, Any]) -> dict[str, Any]:
    hello = _raw_hello(sock_path)
    record(results, "a_hello", hello.get("ok") is True, resp=hello)
    return hello


def step_b_find_stub(run_dir: Path, results: dict[str, Any]) -> None:
    br = transport.exec(
        _find_stub_state_code(),
        thread="main",
        timeout_s=EXEC_TIMEOUT_S,
        label="menu_e2e_find_stub",
        run_dir=run_dir,
    )
    ok = br.ok and isinstance(br.result, dict) and br.result.get("hasStub") is True
    record(results, "b_find_stub", ok, resp=_br_dict(br))


def step_c_reload_success(
    run_dir: Path, sock_path: Path, before_hello: dict[str, Any], results: dict[str, Any]
) -> dict[str, Any]:
    """Returns the hello response for the instance running after this step
    (the caller's new "current" baseline for step d)."""
    br = transport.exec(
        _register_reload_code(),
        thread="main",
        timeout_s=EXEC_TIMEOUT_S,
        label="menu_e2e_reload_c",
        run_dir=run_dir,
    )
    record(results, "c_register_reload", br.ok and br.result is True, resp=_br_dict(br))

    time.sleep(RELOAD_WAIT_S)
    after_hello = _raw_hello(sock_path)
    reloaded_ok = (
        after_hello.get("ok") is True
        and after_hello.get("startedAt") != before_hello.get("startedAt")
        and after_hello.get("pid") == before_hello.get("pid")
    )
    record(
        results,
        "c_reload_success_new_instance",
        reloaded_ok,
        beforeHello=before_hello,
        afterHello=after_hello,
    )
    return after_hello if after_hello.get("ok") is True else before_hello


def step_d_reload_failure_keeps_old_instance(
    run_dir: Path, sock_path: Path, before_hello: dict[str, Any], results: dict[str, Any]
) -> dict[str, Any]:
    """`before_hello` is the instance currently running (step c's post-reload
    instance). Returns the hello response for the instance running after
    this step completes."""
    if not BRIDGE_DEV_IMPL_PATH.is_file():
        record(
            results,
            "d_reload_failure",
            False,
            reason=f"deployed dev impl.py not found at {BRIDGE_DEV_IMPL_PATH}",
        )
        return before_hello

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    backup_path = SCRATCH_DIR / f"impl.py.pre_menu_e2e_{ts}"
    shutil.copy2(BRIDGE_DEV_IMPL_PATH, backup_path)

    current_hello = before_hello
    try:
        BRIDGE_DEV_IMPL_PATH.write_text(BROKEN_IMPL_SOURCE, encoding="utf-8")

        br = transport.exec(
            _register_reload_code(),
            thread="main",
            timeout_s=EXEC_TIMEOUT_S,
            label="menu_e2e_reload_d_broken",
            run_dir=run_dir,
        )
        record(
            results,
            "d_register_reload_with_broken_impl",
            br.ok and br.result is True,
            resp=_br_dict(br),
        )

        time.sleep(RELOAD_WAIT_S)
        after_broken_hello = _raw_hello(sock_path)
        old_survives = (
            after_broken_hello.get("ok") is True
            and after_broken_hello.get("startedAt") == before_hello.get("startedAt")
            and after_broken_hello.get("pid") == before_hello.get("pid")
        )
        record(
            results,
            "d_old_instance_survives_broken_reload",
            old_survives,
            beforeHello=before_hello,
            afterBrokenHello=after_broken_hello,
        )
        if after_broken_hello.get("ok") is True:
            current_hello = after_broken_hello

        err_br = transport.exec(
            _find_stub_error_code(),
            thread="main",
            timeout_s=EXEC_TIMEOUT_S,
            label="menu_e2e_check_error",
            run_dir=run_dir,
        )
        error_text = (
            err_br.result.get("error") if err_br.ok and isinstance(err_br.result, dict) else None
        )
        record(
            results,
            "d_state_error_mentions_syntax_error",
            err_br.ok and isinstance(error_text, str) and "SyntaxError" in error_text,
            resp=_br_dict(err_br),
        )
    finally:
        shutil.copy2(backup_path, BRIDGE_DEV_IMPL_PATH)
        restored_sha = _sha256_of(BRIDGE_DEV_IMPL_PATH)
        repo_sha = _sha256_of(REPO_IMPL_PATH) if REPO_IMPL_PATH.is_file() else None
        record(
            results,
            "d_impl_restored_matches_repo",
            restored_sha == repo_sha,
            restoredSha256=restored_sha,
            repoSha256=repo_sha,
            backupPath=str(backup_path),
        )

    br2 = transport.exec(
        _register_reload_code(),
        thread="main",
        timeout_s=EXEC_TIMEOUT_S,
        label="menu_e2e_reload_d_fixed",
        run_dir=run_dir,
    )
    record(
        results,
        "d_register_reload_with_fixed_impl",
        br2.ok and br2.result is True,
        resp=_br_dict(br2),
    )

    time.sleep(RELOAD_WAIT_S)
    after_fixed_hello = _raw_hello(sock_path)
    recovered_ok = (
        after_fixed_hello.get("ok") is True
        and after_fixed_hello.get("startedAt") != current_hello.get("startedAt")
        and after_fixed_hello.get("pid") == current_hello.get("pid")
    )
    record(
        results,
        "d_reload_recovers_after_fix",
        recovered_ok,
        beforeHello=current_hello,
        afterFixedHello=after_fixed_hello,
    )
    return after_fixed_hello if after_fixed_hello.get("ok") is True else current_hello


def step_e_dialog_check(run_dir: Path, results: dict[str, Any]) -> bool:
    """Returns True when the dialog appears to be blocking Flame's main
    thread (a queued-phase TIMEOUT was observed on the probe below) -- the
    caller uses this to decide whether to skip step f."""
    br = transport.exec(
        _register_show_last_error_dialog_code(),
        thread="main",
        timeout_s=EXEC_TIMEOUT_S,
        label="menu_e2e_show_dialog",
        run_dir=run_dir,
    )
    record(
        results, "e_register_show_last_error_dialog", br.ok and br.result is True, resp=_br_dict(br)
    )

    time.sleep(DIALOG_WAIT_S)

    probe_br = transport.exec(
        "_result = 1",
        thread="main",
        timeout_s=DIALOG_PROBE_TIMEOUT_S,
        label="menu_e2e_dialog_probe",
        run_dir=run_dir,
    )
    dialog_blocking = probe_br.error_code == "TIMEOUT" and probe_br.timeout_phase == "queued"
    dialog_answered = probe_br.ok is True
    record(
        results,
        "e_dialog_probe",
        dialog_blocking or dialog_answered,  # either outcome is a valid observation, not a failure
        resp=_br_dict(probe_br),
        dialogBlocking=dialog_blocking,
        dialogAnsweredAutomatically=dialog_answered,
    )
    return dialog_blocking


def step_f_exit(run_dir: Path, results: dict[str, Any]) -> None:
    br = transport.exec(
        "flame.exit()", thread="main", timeout_s=5.0, label="menu_e2e_exit", run_dir=run_dir
    )
    # No response is expected: the socket closes as Flame exits. BRIDGE_LOST
    # (or a client-side TIMEOUT if the exit races the socket teardown) is the
    # success shape here, not a real failure.
    record(
        results,
        "f_flame_exit",
        br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(br),
    )


def run(run_dir: Path, socket_wait_s: float, results: dict[str, Any]) -> None:
    sock_path = _find_socket(run_dir, socket_wait_s)
    results["socketPath"] = str(sock_path)

    before_hello = step_a_hello(sock_path, results)
    step_b_find_stub(run_dir, results)

    if before_hello.get("ok") is not True:
        record(
            results,
            "c_and_d_skipped",
            True,
            skipped=True,
            reason="step a hello did not succeed; skipping reload steps",
        )
        dialog_blocking = step_e_dialog_check(run_dir, results)
    else:
        current_hello = step_c_reload_success(run_dir, sock_path, before_hello, results)
        step_d_reload_failure_keeps_old_instance(run_dir, sock_path, current_hello, results)
        dialog_blocking = step_e_dialog_check(run_dir, results)

    if dialog_blocking:
        record(
            results,
            "f_flame_exit",
            True,
            skipped=True,
            reason=(
                "the last-error dialog appears to be blocking Flame's main thread; "
                "leaving Flame running for the parent to stop"
            ),
        )
    else:
        step_f_exit(run_dir, results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--socket-wait-s", type=float, default=SOCKET_WAIT_S)
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir, args.socket_wait_s)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"ok": True, "steps": []}
    try:
        run(run_dir, args.socket_wait_s, results)
    except BaseException:
        results["ok"] = False
        results["fatalError"] = traceback.format_exc()

    ts = time.strftime("%y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"p1_menu_e2e_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Result: {out_path}")
    print(json.dumps(results, indent=2, default=str))
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
