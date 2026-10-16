#!/usr/bin/env python3
"""Real-Flame end-to-end client for the bridge.

Checks: The bridge over its socket: hello and ping, `exec` on the main thread, how an exception is
reported, that a request sent to a blocked main thread is not executed, 200 consecutive reads, and a
burst of writes.

Run with the `.venv` Python. Assumes the dev bridge deployed by scripts/deploy_bridge_dev.sh
is running inside Flame. Runs steps a to g over the socket and writes the result to
~/Library/Application Support/flame-mcp/p1/p1_bridge_e2e_<timestamp>.json.
Starting and exiting Flame and detecting crashes are the job of
scripts/run_p1_bridge_e2e.sh; this script only covers the steps after the socket is up.

Steps:
  a. hello (flameVersion matches) / ping
  b. exec: run on the main thread; read the project name and version
  c. An exception is returned (FLAME_API_ERROR, ValueError in the traceback)
  d. A second request sent to a blocked main thread is not executed.
     First a positive control: exec an ordinary write of a marker file to another path and
     check that the file really appears (without this, "no marker" would prove nothing about
     "not executed"). Then block the main thread for 3 s and, while it is blocked, send the
     second request with timeoutS=1 -> TIMEOUT/queued. 2 s after the block ends, the second
     request's marker must still be absent.
  e. 200 consecutive read execs (current_workspace.name); record the median and maximum time
  f0. Diagnostics: record the name of every ws.libraries entry through str / repr / get_value /
      type (to find out why library-name comparisons fail)
  f. Burst of writes: create a dedicated library in current_workspace (not in the Desktop's
     reel group), create 40 reels in it with lib.create_reel, one request each -> all ok;
     then flame.delete each reel, one request each; finally delete the library itself.
  g. Finally send flame.exit() (the connection closing without a response is expected)
"""

from __future__ import annotations

import argparse
import glob
import json
import socket
import statistics
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import e2e_guard

RUN_DIR_DEFAULT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "run"
OUTPUT_DIR = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p1"
SCRATCH_DIR = Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "p1e2e"
EXPECTED_VERSION = e2e_guard.e2e_flame_version()
WRITE_BURST_COUNT = 40


def _req_id() -> str:
    return uuid.uuid4().hex[:12]


def _find_socket(run_dir: Path, timeout: float) -> Path:
    deadline = time.monotonic() + timeout
    pattern = str(run_dir / "flame-mcp-*.sock")
    while time.monotonic() < deadline:
        matches = sorted(glob.glob(pattern))
        if matches:
            return Path(matches[0])
        time.sleep(1.0)
    raise TimeoutError(f"no bridge socket appeared under {run_dir} within {timeout}s")


def send(sock_path: Path, req: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
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


def exec_code(
    sock_path: Path,
    code: str,
    thread: str = "main",
    timeout_s: float = 10.0,
    wait: float = 15.0,
) -> dict[str, Any]:
    req = {
        "v": 1,
        "id": _req_id(),
        "kind": "exec",
        "code": code,
        "thread": thread,
        "timeoutS": timeout_s,
    }
    return send(sock_path, req, timeout=wait)


def record(results: dict[str, Any], name: str, ok: bool, **extra: Any) -> None:
    results["steps"].append({"name": name, "ok": ok, **extra})
    if not ok:
        results["ok"] = False


def step_a_hello_ping(sock_path: Path, results: dict[str, Any]) -> None:
    hello = send(sock_path, {"v": 1, "id": _req_id(), "kind": "hello"})
    ok = hello.get("ok") is True and hello.get("flameVersion") == EXPECTED_VERSION
    record(results, "a_hello", ok, resp=hello)

    ping = send(sock_path, {"v": 1, "id": _req_id(), "kind": "ping"})
    record(results, "a_ping", ping.get("ok") is True and ping.get("pong") is True, resp=ping)


def step_b_exec_basic(sock_path: Path, results: dict[str, Any]) -> None:
    code = (
        "import threading, flame\n"
        "_result = {\n"
        "    'main': threading.current_thread() is threading.main_thread(),\n"
        "    'project': str(flame.projects.current_project.name),\n"
        "    'version': flame.get_version(),\n"
        "}"
    )
    resp = exec_code(sock_path, code)
    result = resp.get("result") or {}
    ok = (
        resp.get("ok") is True
        and result.get("main") is True
        and result.get("version") == EXPECTED_VERSION
    )
    record(results, "b_exec_basic", ok, resp=resp)


def step_c_exception(sock_path: Path, results: dict[str, Any]) -> None:
    resp = exec_code(sock_path, "raise ValueError('probe')")
    ok = (
        resp.get("ok") is False
        and resp.get("errorCode") == "FLAME_API_ERROR"
        and "ValueError" in str(resp.get("traceback", ""))
    )
    record(results, "c_exception", ok, resp=resp)


def step_d_u7(sock_path: Path, results: dict[str, Any]) -> None:
    # Positive control. If a plain, non-blocked exec that writes a
    # marker file in the exact same shape does NOT actually create it, then
    # "the second marker is absent" below is worthless as evidence that it
    # was never executed — it could just as well mean markers never work.
    positive_marker = SCRATCH_DIR / f"u7_positive_control_{uuid.uuid4().hex[:8]}.txt"
    positive_resp = exec_code(
        sock_path,
        f"open({str(positive_marker)!r}, 'w').write('control')",
        timeout_s=5,
        wait=10,
    )
    positive_control_marker_created = positive_resp.get("ok") is True and positive_marker.exists()
    try:
        positive_marker.unlink()
    except FileNotFoundError:
        pass

    if not positive_control_marker_created:
        record(
            results,
            "d_u7_queued_timeout",
            False,
            positive_control_marker_created=False,
            positive_control_resp=positive_resp,
            note=(
                "positive control failed: a plain marker-write exec did not "
                "create its file, so a missing marker below cannot be trusted "
                "as evidence that the second exec never ran"
            ),
        )
        return

    marker = SCRATCH_DIR / f"u7_marker_{uuid.uuid4().hex[:8]}.txt"
    resp1_holder: dict[str, Any] = {}

    def send_blocking() -> None:
        resp1_holder["resp"] = exec_code(
            sock_path, "import time\ntime.sleep(3.0)\n_result = 1", timeout_s=10, wait=15
        )

    t1 = threading.Thread(target=send_blocking)
    t1.start()
    time.sleep(0.5)  # let exec #1 actually dequeue and start running on main

    start = time.monotonic()
    resp2 = exec_code(sock_path, f"open({str(marker)!r}, 'w').write('ran')", timeout_s=1, wait=5)
    elapsed_s = time.monotonic() - start
    t1.join(15)

    timed_out_queued = (
        resp2.get("ok") is False
        and resp2.get("errorCode") == "TIMEOUT"
        and resp2.get("timeoutPhase") == "queued"
    )
    time.sleep(2.0)
    marker_absent = not marker.exists()
    try:
        marker.unlink()
    except FileNotFoundError:
        pass

    record(
        results,
        "d_u7_queued_timeout",
        bool(timed_out_queued and marker_absent),
        positive_control_marker_created=True,
        resp1=resp1_holder.get("resp"),
        resp2=resp2,
        elapsed_for_resp2_s=elapsed_s,
        marker_absent_after_2s=marker_absent,
    )


def step_e_200_reads(sock_path: Path, results: dict[str, Any]) -> None:
    durations = []
    all_ok = True
    last_resp: dict[str, Any] | None = None
    for _ in range(200):
        t0 = time.monotonic()
        resp = exec_code(
            sock_path,
            "import flame\n_result = str(flame.projects.current_project.current_workspace.name)",
        )
        durations.append(time.monotonic() - t0)
        last_resp = resp
        if resp.get("ok") is not True:
            all_ok = False
    record(
        results,
        "e_200_reads",
        all_ok,
        count=200,
        median_s=statistics.median(durations),
        max_s=max(durations),
        last_resp=last_resp,
    )


def _find_library_code(lib_name: str) -> str:
    # `name` is a PyAttribute; str(PyAttribute) returns a quoted repr-like
    # string (str(l.name) == "'P1E_...'" with literal quote characters, while
    # l.name.get_value() == "P1E_..."), so comparing with str() never matches.
    # get_value() is the correct comparison surface.
    return (
        "import flame\n"
        "ws = flame.projects.current_project.current_workspace\n"
        f"lib = next((_l for _l in ws.libraries if _l.name.get_value() == {lib_name!r}), None)\n"
    )


def _create_library_code(lib_name: str) -> str:
    return (
        _find_library_code(lib_name)
        + f"lib = lib if lib is not None else ws.create_library({lib_name!r})\n"
        "_result = str(lib.name)"
    )


def _create_reel_code(lib_name: str, reel_name: str) -> str:
    return (
        _find_library_code(lib_name) + f"r = lib.create_reel({reel_name!r})\n_result = str(r.name)"
    )


def _delete_reel_code(lib_name: str, reel_name: str) -> str:
    return (
        _find_library_code(lib_name) + "target = (\n"
        f"    next((r for r in lib.reels if r.name.get_value() == {reel_name!r}), None)\n"
        "    if lib is not None else None\n"
        ")\n"
        "if target is not None:\n"
        "    flame.delete(target)\n"
        "_result = target is not None"
    )


def _delete_library_code(lib_name: str) -> str:
    return (
        _find_library_code(lib_name) + "if lib is not None:\n"
        "    flame.delete(lib)\n"
        "_result = lib is not None"
    )


def _max_timing(responses: list[dict[str, Any]], key: str) -> int | None:
    values = [r[key] for r in responses if isinstance(r.get(key), int)]
    return max(values) if values else None


def _failure_key(response: dict[str, Any]) -> str:
    if response.get("ok") is not True:
        return str(
            response.get("error")
            or response.get("protocolError")
            or response.get("errorCode")
            or "unknown_error_shape"
        )
    # ok=True but the logical result was still falsy (e.g. a delete whose
    # target was never found because the library/reel lookup missed).
    return f"ok_true_but_falsy_result={response.get('result')!r}"


def _first_failure(
    responses: list[dict[str, Any]], is_success: Callable[[dict[str, Any]], bool]
) -> dict[str, Any] | None:
    for response in responses:
        if not is_success(response):
            return response
    return None


def _failure_counts(
    responses: list[dict[str, Any]], is_success: Callable[[dict[str, Any]], bool]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for response in responses:
        if is_success(response):
            continue
        key = _failure_key(response)
        counts[key] = counts.get(key, 0) + 1
    return counts


def step_f0_diag_library_names(sock_path: Path, results: dict[str, Any]) -> None:
    """Diagnostic only — does not touch any library. Lists every library
    under current_workspace with str()/repr()/get_value()/type() of its
    `name` attribute. Read-only: a leftover library from a previous run is
    left as-is and simply shows up in this list.
    """
    code = (
        "import flame\n"
        "ws = flame.projects.current_project.current_workspace\n"
        "def _describe(_l):\n"
        "    try:\n"
        "        gv = _l.name.get_value()\n"
        "    except Exception as _e:\n"
        "        gv = 'ERROR: ' + repr(_e)\n"
        "    return {\n"
        "        'str': str(_l.name),\n"
        "        'repr': repr(_l.name),\n"
        "        'get_value': gv,\n"
        "        'type': type(_l.name).__name__,\n"
        "    }\n"
        "_result = [_describe(_l) for _l in ws.libraries]"
    )
    resp = exec_code(sock_path, code)
    record(results, "f0_diag_library_names", resp.get("ok") is True, resp=resp)


def step_f_write_burst(sock_path: Path, results: dict[str, Any]) -> None:
    lib_name = f"P1E_{time.strftime('%y%m%d_%H%M%S')}"

    setup_resp = exec_code(sock_path, _create_library_code(lib_name))
    ok_setup = setup_resp.get("ok") is True

    create_responses = [
        exec_code(sock_path, _create_reel_code(lib_name, f"P1E_{i}"))
        for i in range(WRITE_BURST_COUNT)
    ]
    all_create_ok = all(r.get("ok") is True for r in create_responses)

    delete_responses = [
        exec_code(sock_path, _delete_reel_code(lib_name, f"P1E_{i}"))
        for i in range(WRITE_BURST_COUNT)
    ]
    all_delete_ok = all(r.get("ok") is True and r.get("result") is True for r in delete_responses)

    delete_lib_resp = exec_code(sock_path, _delete_library_code(lib_name))
    ok_delete_lib = delete_lib_resp.get("ok") is True and delete_lib_resp.get("result") is True

    all_responses = [setup_resp, *create_responses, *delete_responses, delete_lib_resp]

    def is_create_ok(r: dict[str, Any]) -> bool:
        return r.get("ok") is True

    def is_delete_ok(r: dict[str, Any]) -> bool:
        return r.get("ok") is True and r.get("result") is True

    record(
        results,
        "f_write_burst",
        bool(ok_setup and all_create_ok and all_delete_ok and ok_delete_lib),
        library_name=lib_name,
        setup_resp=setup_resp,
        create_ok_count=sum(1 for r in create_responses if r.get("ok") is True),
        create_total=WRITE_BURST_COUNT,
        create_first_failure=_first_failure(create_responses, is_create_ok),
        create_failure_counts=_failure_counts(create_responses, is_create_ok),
        delete_ok_count=sum(
            1 for r in delete_responses if r.get("ok") is True and r.get("result") is True
        ),
        delete_total=WRITE_BURST_COUNT,
        delete_first_failure=_first_failure(delete_responses, is_delete_ok),
        delete_failure_counts=_failure_counts(delete_responses, is_delete_ok),
        delete_library_resp=delete_lib_resp,
        max_queued_ms=_max_timing(all_responses, "queuedMs"),
        max_run_ms=_max_timing(all_responses, "runMs"),
    )


def step_g_exit(sock_path: Path, results: dict[str, Any]) -> None:
    try:
        resp = send(
            sock_path,
            {
                "v": 1,
                "id": _req_id(),
                "kind": "exec",
                "code": "import flame\nflame.exit()",
                "thread": "main",
                "timeoutS": 5,
            },
            timeout=5,
        )
    except OSError as exc:
        resp = {"note": f"connection dropped as expected: {exc}"}
    # A dropped/absent response here is expected (Flame is exiting); this step
    # only records what happened, it never fails the overall run on its own.
    record(results, "g_exit_sent", True, resp=resp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--wait-socket-s", type=float, default=120.0)
    parser.add_argument(
        "--skip-write-burst",
        action="store_true",
        help="skip the burst of create_reel / delete calls (step f)",
    )
    args = parser.parse_args()
    refused = e2e_guard.require_probe_project(Path(args.run_dir), args.wait_socket_s)
    if refused is not None:
        return refused

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {"ok": True, "steps": []}
    try:
        sock_path = _find_socket(Path(args.run_dir), args.wait_socket_s)
        results["socket"] = str(sock_path)
        step_a_hello_ping(sock_path, results)
        step_b_exec_basic(sock_path, results)
        step_c_exception(sock_path, results)
        step_d_u7(sock_path, results)
        step_e_200_reads(sock_path, results)
        if not args.skip_write_burst:
            step_f0_diag_library_names(sock_path, results)
            step_f_write_burst(sock_path, results)
        step_g_exit(sock_path, results)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()

    ts = time.strftime("%y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"p1_bridge_e2e_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Result: {out_path}")
    print(json.dumps(results, indent=2, default=str))
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
