#!/usr/bin/env python3
"""Real-Flame end-to-end client for the server transport.

Checks: The server transport: discovery, `exec`, errors and timeouts, and what it reports when Flame
is killed (`BRIDGE_LOST`, the in-flight file left behind, a stale socket).

Run with the `.venv` Python. Meant to be called by scripts/run_p1_transport_e2e.sh after
it has started Flame through the dev hooks and the bridge socket has appeared under
`--run-dir`. It uses only flame_mcp.transport (exec / poll / discovery) and covers the steps
after the socket is up (a to e) and the recording of their result (f). Starting and exiting
Flame and detecting crashes are the job of the orchestrator (run_p1_transport_e2e.sh).

Steps:
  a. Discovery finds exactly one bridge (record the pid from hello; with 0 or 2 bridges, stop
     here).
  b. exec succeeds (current_project.name is a plain str, so it is used as is).
     Positive control for the app.log window: with levels=None (all levels), the window of an
     exec that calls current_workspace.create_library(...) must contain an
     "Entering createLibrary" line (the library is removed with flame.delete in the same
     exec). Then record the default (error / warn) window too.
  c. FLAME_API_ERROR (an exception) and TIMEOUT (the first exec blocks main for 3 s; the
     second, sent with timeout_s=1, must time out as queued).
  d. SIGKILL: send an exec that blocks main for 8 s from another thread and, 1.5 s later,
     run kill -9 on the pid from hello. Before sending it, check that
     `ps -p <pid> -o command=` matches the Flame application pattern; if it does not, abort
     without killing. Expected: the blocked exec's result is BRIDGE_LOST, the in-flight file
     stays, and crash.app_log_tail is not empty.
  e. Discovery after the kill returns BRIDGE_UNAVAILABLE (the stale socket file should remain;
     check that this still gives neither AMBIGUOUS nor an exception).
  f. Write the result to ~/Library/Application Support/flame-mcp/p1/p1_transport_e2e_<ts>.json
     and print the list of stale sockets and in-flight files left behind (they are not
     deleted).

Discovery uses internal functions that flame_mcp.transport does not export
(`transport._discover` / `transport._DiscoveryFailure`). exec and poll alone cannot return
"the pid of the bridge that was found" or "whether discovery itself succeeded" in the form
steps a and e need, so this script is the only place that accepts this internal access.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import e2e_guard  # noqa: E402

from flame_mcp import applog, transport  # noqa: E402

OUTPUT_DIR = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p1"
RUN_DIR_DEFAULT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "run"

FLAME_VERSION = e2e_guard.e2e_flame_version()
FLAME_BIN_PATTERN = re.compile(
    rf"^/opt/Autodesk/(\.flamefamily|flame)_{re.escape(FLAME_VERSION)}"
    r"/bin/flame\.app/Contents/MacOS/flame(\s|$)"
)
KILL_BLOCK_SECONDS: float = 8.0
KILL_DELAY_S: float = 1.5
APP_LOG_POSITIVE_CONTROL_MAX_LINES: int = 500
APP_LOG_POSITIVE_CONTROL_MAX_BYTES: int = 256 * 1024


def record(results: dict[str, Any], name: str, ok: bool, **extra: Any) -> None:
    results["steps"].append({"name": name, "ok": ok, **extra})
    if not ok:
        results["ok"] = False


def _result_to_dict(r: transport.BridgeResult | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d: dict[str, Any] = {
        "ok": r.ok,
        "result": r.result,
        "errorCode": r.error_code,
        "error": r.error,
        "traceback": r.traceback,
        "queuedMs": r.queued_ms,
        "runMs": r.run_ms,
        "timeoutPhase": r.timeout_phase,
        "pendingHandle": r.pending_handle,
        "appLog": list(r.app_log),
        "appLogNote": r.app_log_note,
        "inflightPath": str(r.inflight_path) if r.inflight_path else None,
        "durationMs": r.duration_ms,
    }
    if r.crash is not None:
        d["crash"] = {
            "appLogTail": list(r.crash.app_log_tail),
            "shellCrashLines": list(r.crash.shell_crash_lines),
            "inflightPath": str(r.crash.inflight_path) if r.crash.inflight_path else None,
        }
    return d


def step_a_discover(run_dir: Path, results: dict[str, Any]) -> transport.BridgeCandidate | None:
    candidate = transport._discover(run_dir)
    if isinstance(candidate, transport.BridgeCandidate):
        record(
            results,
            "a_discover",
            True,
            path=str(candidate.path),
            pid=candidate.pid,
            flameVersion=candidate.flame_version,
            projectName=candidate.project_name,
            bridgeAbi=candidate.bridge_abi,
        )
        return candidate
    record(results, "a_discover", False, errorCode=candidate.error_code, error=candidate.message)
    return None


def step_b_exec_and_applog_positive_control(run_dir: Path, results: dict[str, Any]) -> None:
    basic = transport.exec(
        "import flame\n_result = flame.projects.current_project.name",
        thread="main",
        timeout_s=10,
        run_dir=run_dir,
    )
    record(results, "b_exec_basic", basic.ok is True, resp=_result_to_dict(basic))

    window = applog.LogWindow(applog.app_log_path(FLAME_VERSION))
    mark = window.mark()
    lib_name = f"P1E3_{time.strftime('%y%m%d_%H%M%S')}"
    code = (
        "import flame\n"
        "ws = flame.projects.current_project.current_workspace\n"
        f"lib = ws.create_library({lib_name!r})\n"
        "created_name = lib.name.get_value()\n"
        "flame.delete(lib)\n"
        "_result = created_name\n"
    )
    create_resp = transport.exec(code, thread="main", timeout_s=10, run_dir=run_dir)
    positive = window.read(
        mark,
        levels=None,
        max_lines=APP_LOG_POSITIVE_CONTROL_MAX_LINES,
        max_bytes=APP_LOG_POSITIVE_CONTROL_MAX_BYTES,
    )
    saw_marker = any("Entering createLibrary" in line for line in positive.lines)
    record(
        results,
        "b_applog_positive_control",
        bool(create_resp.ok and create_resp.result == lib_name and saw_marker),
        create_resp=_result_to_dict(create_resp),
        default_window_app_log=list(create_resp.app_log),
        all_levels_window_lines=list(positive.lines),
        all_levels_window_rotated=positive.rotated,
        saw_entering_create_library=saw_marker,
    )


def step_c_error_and_timeout(run_dir: Path, results: dict[str, Any]) -> None:
    exc_resp = transport.exec(
        "raise ValueError('p1_3_probe')", thread="main", timeout_s=5, run_dir=run_dir
    )
    record(
        results,
        "c_flame_api_error",
        bool(
            exc_resp.error_code == "FLAME_API_ERROR"
            and exc_resp.traceback is not None
            and "ValueError" in exc_resp.traceback
        ),
        resp=_result_to_dict(exc_resp),
    )

    holder: dict[str, transport.BridgeResult] = {}

    def blocker() -> None:
        holder["r"] = transport.exec(
            "import time\ntime.sleep(3.0)\n_result = 1",
            thread="main",
            timeout_s=10,
            run_dir=run_dir,
        )

    t = threading.Thread(target=blocker)
    t.start()
    time.sleep(0.5)  # let exec #1 dequeue and start running on main before #2 arrives
    timeout_resp = transport.exec("_result = 1", thread="main", timeout_s=1, run_dir=run_dir)
    t.join(15)

    record(
        results,
        "c_timeout_queued",
        bool(timeout_resp.error_code == "TIMEOUT" and timeout_resp.timeout_phase == "queued"),
        resp=_result_to_dict(timeout_resp),
        blocker_resp=_result_to_dict(holder.get("r")),
    )


def is_flame_process(pid: int) -> tuple[bool, str]:
    """(matches_flame_binary_pattern, raw `ps` command string)."""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"ps failed: {exc}"
    command = proc.stdout.strip()
    return bool(FLAME_BIN_PATTERN.match(command)), command


def step_d_sigkill(
    run_dir: Path, candidate: transport.BridgeCandidate, results: dict[str, Any]
) -> None:
    changed = e2e_guard.reconfirm_candidate(run_dir, candidate)
    if changed is not None:
        record(
            results,
            "d_sigkill",
            False,
            aborted=True,
            reason=f"refusing to kill: {changed}",
        )
        return

    matches, command = is_flame_process(candidate.pid)
    if not matches:
        record(
            results,
            "d_sigkill",
            False,
            aborted=True,
            reason=(
                f"pid {candidate.pid} command does not match the Flame binary "
                f"pattern, refusing to kill: {command!r}"
            ),
        )
        return

    holder: dict[str, transport.BridgeResult] = {}

    def blocker() -> None:
        holder["r"] = transport.exec(
            f"import time\ntime.sleep({KILL_BLOCK_SECONDS})\n_result = 1",
            thread="main",
            timeout_s=KILL_BLOCK_SECONDS + 5,
            run_dir=run_dir,
        )

    t = threading.Thread(target=blocker)
    t.start()
    time.sleep(KILL_DELAY_S)
    subprocess.run(["kill", "-9", str(candidate.pid)], check=False)
    t.join(KILL_BLOCK_SECONDS + 15)

    resp = holder.get("r")
    ok = bool(
        resp is not None
        and resp.error_code == "BRIDGE_LOST"
        and resp.inflight_path is not None
        and resp.inflight_path.exists()
        and resp.crash is not None
        and len(resp.crash.app_log_tail) > 0
    )
    record(
        results,
        "d_sigkill",
        ok,
        killed_pid=candidate.pid,
        resp=_result_to_dict(resp),
        inflight_exists=bool(
            resp is not None and resp.inflight_path and resp.inflight_path.exists()
        ),
    )


def step_e_discover_after_kill(run_dir: Path, results: dict[str, Any]) -> None:
    candidate = transport._discover(run_dir)
    ok = isinstance(candidate, transport._DiscoveryFailure) and candidate.error_code == (
        "BRIDGE_UNAVAILABLE"
    )
    record(
        results,
        "e_discover_after_kill",
        ok,
        outcome=(
            "candidate_found_unexpectedly"
            if isinstance(candidate, transport.BridgeCandidate)
            else candidate.error_code
        ),
        message=(None if isinstance(candidate, transport.BridgeCandidate) else candidate.message),
    )


def leftover_listing(run_dir: Path) -> dict[str, list[str]]:
    sockets = sorted(str(p) for p in run_dir.glob("flame-mcp-*.sock"))
    inflight_dir = run_dir / "inflight"
    inflight = sorted(str(p) for p in inflight_dir.glob("*.json")) if inflight_dir.is_dir() else []
    return {"sockets": sockets, "inflight": inflight}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"ok": True, "steps": []}
    try:
        candidate = step_a_discover(run_dir, results)
        if candidate is not None:
            step_b_exec_and_applog_positive_control(run_dir, results)
            step_c_error_and_timeout(run_dir, results)
            step_d_sigkill(run_dir, candidate, results)
            step_e_discover_after_kill(run_dir, results)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()

    results["leftover"] = leftover_listing(run_dir)

    ts = time.strftime("%y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"p1_transport_e2e_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Result: {out_path}")
    print(json.dumps(results, indent=2, default=str))
    print("--- Sockets left behind (expected after the SIGKILL route) ---")
    for s in results["leftover"]["sockets"]:
        print(s)
    print("--- In-flight files left behind ---")
    for f in results["leftover"]["inflight"]:
        print(f)
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
