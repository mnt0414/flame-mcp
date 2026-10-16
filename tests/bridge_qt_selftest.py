"""Self-test of the bridge under the Python and PySide6 that ship with Flame.

Do not run this through pytest (the file name does not start with test_).
Run it with the 3.11.5 interpreter that ships with Flame:
    /opt/Autodesk/python/2025.2.4/bin/python3 -B tests/bridge_qt_selftest.py

It uses only the standard library and PySide6. It loads impl.py the same way the stub
does (compile() the source, then exec()), and uses a fake schedule_idle_event
(Scheduler) that hands a Signal emitted from another thread to a slot on the main
thread through a QueuedConnection. `bridge = impl_module.start(...)` is called from
this script's real process main thread (before `app.exec()`), so the real QTimer that
`Bridge.start()` creates is also owned by the main thread. That lets the test check, on
real threads, that the relay works: handler threads only put requests on the relay
queue, and `_main_relay_tick` calls `schedule_idle_event` from the main thread's QTimer
tick.

Scheduler.schedule records the calling thread on every call. At the end the test checks
that every call came from the main thread (calls from any other thread = 0); if that
fails, the relay does not work. Over the socket it checks: hello; exec running on the
main thread; returning an exception; a request that timed out while queued never runs
afterwards (case u7); 200 requests in a row; and 4 threads x 250 requests in parallel.
It prints the results as JSON to standard output and exits with 1 on failure.
"""

from __future__ import annotations

import json
import os
import socket
import statistics
import sys
import threading
import time
import traceback
import types
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot

_IMPL_MODULE_NAME = "flame_mcp_bridge_impl"
_EXPECTED_BRIDGE_ABI = 1

SCRATCH_DIR = (
    Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "qtselftest"
)


class FakeFlame:
    def get_version(self) -> str:
        return "2025.2.4"


class Scheduler(QObject):
    """Stand-in for schedule_idle_event, built on a queued Qt signal.

    A Signal(object) is queued-connected to a slot on this QObject's own
    thread. Emitting from any thread posts the callback to that thread's
    Qt event loop — this is the property schedule_idle_event needs to have
    for the bridge's `main` callback contract to hold under test.

    Records the identity of every calling thread. The only
    caller must ever be `Bridge._main_relay_tick`, itself only ever invoked
    from the main thread's QTimer tick — never a handler thread. `main()`
    checks `call_threads` against `threading.main_thread().ident` at the end
    and fails the run if anything called in from elsewhere.
    """

    _trigger = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._trigger.connect(self._run, Qt.QueuedConnection)  # type: ignore[arg-type]
        self.call_threads: list[int] = []
        self._call_threads_lock = threading.Lock()

    def schedule(self, cb: Callable[[], None]) -> None:
        ident = threading.get_ident()
        with self._call_threads_lock:
            self.call_threads.append(ident)
        self._trigger.emit(cb)

    @Slot(object)  # type: ignore[misc]
    def _run(self, cb: Callable[[], None]) -> None:
        cb()


class _MainThreadInvoker(QObject):
    """Test-harness-only helper, deliberately separate from `Scheduler`
    (which is instrumented to prove nothing but `Bridge._main_relay_tick`
    ever calls it — routing test-harness dispatch through it too would
    contaminate that count). Runs an arbitrary callback on the real main
    thread via the same QueuedConnection-across-threads trick, so `main()`
    can call `bridge.stop()` there — matching production's `app_exited`
    contract (always the main thread) so `QTimer.stop()` inside `stop()`
    isn't invoked from another thread (Qt warns/misbehaves if it is)."""

    _trigger = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._trigger.connect(self._run, Qt.QueuedConnection)  # type: ignore[arg-type]

    def invoke(self, cb: Callable[[], None]) -> None:
        self._trigger.emit(cb)

    @Slot(object)  # type: ignore[misc]
    def _run(self, cb: Callable[[], None]) -> None:
        cb()


def _bridge_dir() -> str:
    override = os.environ.get("FLAME_MCP_BRIDGE_DIR")
    if override:
        return override
    return str(Path.home() / "Library" / "Application Support" / "flame-mcp" / "bridge")


def _load_impl() -> Any:
    impl_path = os.path.join(_bridge_dir(), "impl.py")
    with open(impl_path, encoding="utf-8") as f:
        source = f.read()
    module = types.ModuleType(_IMPL_MODULE_NAME)
    module.__file__ = impl_path
    sys.modules[_IMPL_MODULE_NAME] = module
    code = compile(source, impl_path, "exec")
    exec(code, module.__dict__)
    abi = getattr(module, "BRIDGE_ABI", None)
    if abi != _EXPECTED_BRIDGE_ABI:
        raise RuntimeError(f"BRIDGE_ABI mismatch: expected {_EXPECTED_BRIDGE_ABI!r}, got {abi!r}")
    return module


def _send(sock_path: str, req: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        result: dict[str, Any] = json.loads(buf.decode("utf-8"))
        return result


def _record(results: dict[str, Any], name: str, ok: bool, **extra: Any) -> None:
    results["steps"].append({"name": name, "ok": ok, **extra})
    if not ok:
        results["ok"] = False


def _step_hello_and_ping(sock_path: str, results: dict[str, Any]) -> None:
    hello = _send(sock_path, {"v": 1, "id": "h", "kind": "hello"})
    ok = hello.get("ok") is True and hello.get("flameVersion") == "2025.2.4"
    _record(results, "hello", ok, resp=hello)

    ping = _send(sock_path, {"v": 1, "id": "p", "kind": "ping"})
    _record(results, "ping", ping == {"id": "p", "ok": True, "pong": True}, resp=ping)


def _step_exec_main_thread(sock_path: str, results: dict[str, Any]) -> None:
    resp = _send(
        sock_path,
        {
            "v": 1,
            "id": "m",
            "kind": "exec",
            "code": (
                "import threading\n_result = threading.current_thread() is threading.main_thread()"
            ),
            "thread": "main",
            "timeoutS": 5,
        },
    )
    ok = resp.get("ok") is True and resp.get("result") is True
    _record(results, "exec_runs_on_main_thread", ok, resp=resp)


def _step_exception(sock_path: str, results: dict[str, Any]) -> None:
    resp = _send(
        sock_path,
        {
            "v": 1,
            "id": "e",
            "kind": "exec",
            "code": "raise ValueError('selftest boom')",
            "thread": "main",
            "timeoutS": 5,
        },
    )
    ok = (
        resp.get("ok") is False
        and resp.get("errorCode") == "FLAME_API_ERROR"
        and "selftest boom" in str(resp.get("error", ""))
        and "ValueError" in str(resp.get("traceback", ""))
    )
    _record(results, "exception_returns_flame_api_error", ok, resp=resp)


def _step_u7(sock_path: str, results: dict[str, Any]) -> None:
    marker = SCRATCH_DIR / f"u7_marker_{uuid.uuid4().hex[:8]}.txt"
    resp1_holder: dict[str, Any] = {}

    def send_blocking() -> None:
        resp1_holder["resp"] = _send(
            sock_path,
            {
                "v": 1,
                "id": "u7-1",
                "kind": "exec",
                "code": "import time\ntime.sleep(3.0)\n_result = 1",
                "thread": "main",
                "timeoutS": 10,
            },
            timeout=15,
        )

    t1 = threading.Thread(target=send_blocking)
    t1.start()
    time.sleep(0.5)  # let exec #1 actually dequeue and start running on main

    start = time.monotonic()
    resp2 = _send(
        sock_path,
        {
            "v": 1,
            "id": "u7-2",
            "kind": "exec",
            "code": f"open({str(marker)!r}, 'w').write('ran')",
            "thread": "main",
            "timeoutS": 1,
        },
        timeout=5,
    )
    elapsed_s = time.monotonic() - start
    t1.join(15)

    timed_out_queued = (
        resp2.get("ok") is False
        and resp2.get("errorCode") == "TIMEOUT"
        and resp2.get("timeoutPhase") == "queued"
    )
    time.sleep(2.0)  # after exec #1 finishes, give the cancelled callback time to (not) run
    marker_absent = not marker.exists()
    try:
        marker.unlink()
    except FileNotFoundError:
        pass

    _record(
        results,
        "u7_queued_timeout_then_never_executes",
        bool(timed_out_queued and marker_absent),
        resp1=resp1_holder.get("resp"),
        resp2=resp2,
        elapsed_for_resp2_s=elapsed_s,
        marker_absent_after_2s=marker_absent,
    )


def _step_200_consecutive(sock_path: str, results: dict[str, Any]) -> None:
    all_ok = True
    durations = []
    for i in range(200):
        t0 = time.monotonic()
        resp = _send(
            sock_path,
            {
                "v": 1,
                "id": str(i),
                "kind": "exec",
                "code": f"_result = {i} + 1",
                "thread": "main",
                "timeoutS": 5,
            },
        )
        durations.append(time.monotonic() - t0)
        if not (resp.get("ok") is True and resp.get("result") == i + 1):
            all_ok = False
    _record(
        results,
        "200_consecutive_execs",
        all_ok,
        count=200,
        median_s=statistics.median(durations),
        max_s=max(durations),
    )


def _step_concurrent_stress(sock_path: str, results: dict[str, Any]) -> None:
    """Runs 4 threads x 250 requests in parallel.

    Each of 4 client threads
    sends 250 back-to-back exec requests with no gap. All must succeed with
    the right per-thread/per-request result -- and (checked separately, at
    the very end of `run_all`, via `scheduler.call_threads`) every one of
    the resulting `schedule_idle_event` calls must have come from the main
    thread, never from any of these 4 threads or a bridge handler thread."""
    n_threads = 4
    n_per_thread = 250
    lock = threading.Lock()
    durations: list[float] = []
    errors: list[str] = []
    ok_count = 0

    def worker(thread_index: int) -> None:
        nonlocal ok_count
        for i in range(n_per_thread):
            t0 = time.monotonic()
            resp = _send(
                sock_path,
                {
                    "v": 1,
                    "id": f"cs-{thread_index}-{i}",
                    "kind": "exec",
                    "code": f"_result = {thread_index} * 1000 + {i}",
                    "thread": "main",
                    "timeoutS": 10,
                },
                timeout=15,
            )
            elapsed = time.monotonic() - t0
            expected = thread_index * 1000 + i
            with lock:
                durations.append(elapsed)
                if resp.get("ok") is True and resp.get("result") == expected:
                    ok_count += 1
                elif len(errors) < 10:
                    errors.append(f"t{thread_index}#{i}: {resp}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)

    total = n_threads * n_per_thread
    _record(
        results,
        "concurrent_4x250",
        ok_count == total,
        count=len(durations),
        expectedCount=total,
        okCount=ok_count,
        threads=n_threads,
        perThread=n_per_thread,
        medianS=statistics.median(durations) if durations else None,
        maxS=max(durations) if durations else None,
        errors=errors,
    )


def main() -> int:
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    app = QCoreApplication([])
    scheduler = Scheduler()
    main_invoker = _MainThreadInvoker()

    impl_module = _load_impl()
    bridge = impl_module.start(
        schedule=scheduler.schedule,
        flame_module=FakeFlame(),
        version="2025.2.4",
        project_name="qt_selftest",
        run_dir=str(SCRATCH_DIR / "run"),
        log_path=str(SCRATCH_DIR / "bridge.log"),
    )

    results: dict[str, Any] = {"ok": True, "steps": []}

    def run_all() -> None:
        try:
            sock_path = bridge.status()["socket"]
            _step_hello_and_ping(sock_path, results)
            _step_exec_main_thread(sock_path, results)
            _step_exception(sock_path, results)
            _step_u7(sock_path, results)
            _step_200_consecutive(sock_path, results)
            _step_concurrent_stress(sock_path, results)
        except BaseException:
            results["ok"] = False
            results["fatal_error"] = traceback.format_exc()
        finally:
            # bridge.stop() must run on the main thread (matching production's app_exited
            # contract) because it stops a QTimer: Qt warns "Timers cannot be stopped from
            # another thread" and the timer does not reliably stop if this runs on `driver`.
            stop_done = threading.Event()

            def _stop_on_main() -> None:
                try:
                    bridge.stop()
                except BaseException:
                    pass
                finally:
                    stop_done.set()

            main_invoker.invoke(_stop_on_main)
            stop_done.wait(timeout=5)
            # The whole point of the relay: every recorded
            # schedule_idle_event call, across every step above (hello/exec/
            # exception/u7/200 in a row/4x250 in parallel), must have come from the main
            # thread. Checked last, after everything that could still call
            # in has stopped (bridge.stop() joins the accept thread).
            main_ident = threading.main_thread().ident
            with scheduler._call_threads_lock:
                call_threads = list(scheduler.call_threads)
            off_main = [t for t in call_threads if t != main_ident]
            _record(
                results,
                "schedule_only_called_from_main_thread",
                len(off_main) == 0,
                totalScheduleCalls=len(call_threads),
                offMainThreadScheduleCalls=len(off_main),
            )
            app.quit()

    driver = threading.Thread(target=run_all, daemon=True)
    driver.start()
    app.exec()
    driver.join(timeout=5)

    print(json.dumps(results, indent=2, default=str))
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
