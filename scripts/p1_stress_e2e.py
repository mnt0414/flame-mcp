#!/usr/bin/env python3
"""Load test that checks the bridge deadlock does not recur.

Run with the `.venv` Python; the requests go through transport.exec. Several clients send
short execs back to back to the new bridge and the test checks that it does not hang. K
threads each send `_result = flame.projects.current_project.name` M times without pausing.
If any request ends in BRIDGE_LOST or TIMEOUT or takes more than 15 s, it counts as a
suspected hang: all threads stop, the result is written and the script exits with code 3 (it
does not kill Flame; the orchestrator, scripts/run_flame_with_bridge.sh, takes the process
sample). If everything passes, it writes the count, the median and the maximum, and finally
sends `flame.exit()`.

Usage (meant to be called from scripts/run_flame_with_bridge.sh; it also runs on its own):
    .venv/bin/python scripts/p1_stress_e2e.py --label new
    .venv/bin/python scripts/p1_stress_e2e.py --label old --threads 4 --requests 500

For the positive control (the old implementation), deploy a different impl.py through
BRIDGE_IMPL_SRC of scripts/deploy_bridge_dev.sh, then call this script with --label old.

The result is written to ~/Library/Application Support/flame-mcp/p1/p1_stress_<label>_<ts>.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import e2e_guard  # noqa: E402

from flame_mcp import transport  # noqa: E402

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p1"
RUN_DIR_DEFAULT: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "run"

DEFAULT_WAIT_SOCKET_S: Final[float] = 120.0
BRIDGE_POLL_INTERVAL_S: Final[float] = 1.0
DEFAULT_THREADS: Final[int] = 4
DEFAULT_REQUESTS_PER_THREAD: Final[int] = 500
STUCK_SUSPECT_DURATION_S: Final[float] = 15.0
EXEC_TIMEOUT_S: Final[float] = 10.0
EXEC_CODE: Final[str] = "_result = flame.projects.current_project.name"


def _wait_for_bridge(run_dir: Path, timeout_s: float) -> transport.BridgeCandidate:
    """Glob `run_dir` for `flame-mcp-*.sock` and confirm liveness with a
    hello, exactly like `transport._discover` already does internally --
    reusing it here instead of re-implementing glob+hello."""
    deadline = time.monotonic() + timeout_s
    last: transport._DiscoveryFailure | None = None
    while True:
        candidate = transport._discover(run_dir)
        if isinstance(candidate, transport.BridgeCandidate):
            return candidate
        last = candidate
        if time.monotonic() >= deadline:
            detail = f"{last.error_code}: {last.message}" if last is not None else "no attempt made"
            raise TimeoutError(
                f"no live bridge socket appeared under {run_dir} within {timeout_s}s ({detail})"
            )
        time.sleep(BRIDGE_POLL_INTERVAL_S)


def _worker(
    thread_index: int,
    n_requests: int,
    run_dir: Path,
    durations_s: list[float],
    results_lock: threading.Lock,
    stop_event: threading.Event,
    failure_box: dict[str, Any],
) -> None:
    for i in range(n_requests):
        if stop_event.is_set():
            return
        t0 = time.monotonic()
        br = transport.exec(
            EXEC_CODE,
            thread="main",
            timeout_s=EXEC_TIMEOUT_S,
            label=f"stress_t{thread_index}_r{i}",
            run_dir=run_dir,
        )
        elapsed_s = time.monotonic() - t0
        with results_lock:
            durations_s.append(elapsed_s)

        suspect_reason: str | None = None
        if br.error_code == "BRIDGE_LOST":
            suspect_reason = "BRIDGE_LOST"
        elif br.error_code == "TIMEOUT":
            suspect_reason = "TIMEOUT"
        elif elapsed_s > STUCK_SUSPECT_DURATION_S:
            suspect_reason = f"exceeded {STUCK_SUSPECT_DURATION_S}s ({elapsed_s:.1f}s)"

        if suspect_reason is not None:
            with results_lock:
                if "reason" not in failure_box:
                    failure_box["reason"] = suspect_reason
                    failure_box["threadIndex"] = thread_index
                    failure_box["requestIndex"] = i
                    failure_box["durationS"] = elapsed_s
                    failure_box["errorCode"] = br.error_code
                    failure_box["error"] = br.error
            stop_event.set()
            return


def run(
    run_dir: Path, threads: int, requests_per_thread: int, wait_socket_s: float
) -> dict[str, Any]:
    results: dict[str, Any] = {
        "ok": True,
        "threads": threads,
        "requestsPerThread": requests_per_thread,
    }

    candidate = _wait_for_bridge(run_dir, wait_socket_s)
    results["bridge"] = {
        "pid": candidate.pid,
        "flameVersion": candidate.flame_version,
        "projectName": candidate.project_name,
    }

    durations_s: list[float] = []
    results_lock = threading.Lock()
    stop_event = threading.Event()
    failure_box: dict[str, Any] = {}

    worker_args = (requests_per_thread, run_dir, durations_s, results_lock, stop_event, failure_box)
    workers = [
        threading.Thread(
            target=_worker,
            args=(i, *worker_args),
            name=f"stress-worker-{i}",
        )
        for i in range(threads)
    ]
    started_at = time.monotonic()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    total_elapsed_s = time.monotonic() - started_at

    results["totalElapsedS"] = total_elapsed_s
    results["completedRequests"] = len(durations_s)

    if failure_box:
        results["ok"] = False
        results["stuckSuspected"] = failure_box
        return results

    results["durations"] = {
        "count": len(durations_s),
        "medianS": statistics.median(durations_s) if durations_s else None,
        "maxS": max(durations_s) if durations_s else None,
        "minS": min(durations_s) if durations_s else None,
    }

    exit_br = transport.exec(
        "flame.exit()", thread="main", timeout_s=5.0, label="stress_exit", run_dir=run_dir
    )
    # No response is expected: the socket closes as Flame exits. BRIDGE_LOST
    # (or a client-side TIMEOUT if the exit races the socket teardown) is the
    # success shape here, not a real failure.
    results["exit"] = {"errorCode": exit_br.error_code, "ok": exit_br.ok}
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--wait-socket-s", type=float, default=DEFAULT_WAIT_SOCKET_S)
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS_PER_THREAD)
    parser.add_argument("--label", required=True, help="results filename tag, e.g. old/new")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir, args.wait_socket_s)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    exit_code = 1
    try:
        results = run(run_dir, args.threads, args.requests, args.wait_socket_s)
        exit_code = 0 if results.get("ok") else 3
    except BaseException:
        results = {"ok": False, "fatalError": traceback.format_exc()}
        exit_code = 1

    ts = time.strftime("%y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"p1_stress_{args.label}_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Result: {out_path}")
    print(json.dumps(results, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
