"""Tests for the bridge implementation (flame_mcp_bridge.impl).

The fake main loop (FakeMainLoop) imitates `schedule_idle_event`: callbacks are
registered from another thread (the handler) and pumped on the test process's real
main thread. `threading.main_thread()` is that real thread, so always call
FakeMainLoop.run_until/drain_for from the test function's own thread (pytest's
execution thread, which is the process's main thread).

Handler threads never call `schedule_idle_event` directly; they only put requests on
the Bridge's relay queue, and the main-thread QTimer takes them off and schedules
them. Here FakeTimer stands in for the QTimer: `start()` registers it with the
FakeMainLoop, and it fires (as a registered timer) every time run_until/drain_for
pumps. Like a real QTimer, it fires on the thread that makes the pump call, so the
`_main_relay_tick` of a bridge whose FakeTimer was started always runs on the test
function's own thread.

Sockets and result directories use
~/Library/Application Support/flame-mcp/scratch/p12/<short uuid>/
instead of pytest's tmp_path, to stay within the 104-byte AF_UNIX sun_path limit.
tmp_path lives under /private/var/folders/..., so its paths are long, and combined with
a socket name that contains the full version string they can exceed the limit.
"""

from __future__ import annotations

import ast
import json
import logging
import logging.handlers
import os
import queue
import shutil
import socket
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from flame_mcp_bridge import impl

SCRATCH_ROOT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "p12"


class FakeMainLoop:
    """A FIFO fake of `schedule_idle_event`.

    Callbacks can be registered from any thread; pumping runs on the calling thread.

    `_timers` holds `FakeTimer`s that `start()`ed against this loop (the relay
    timer): every pump (`run_until`/`drain_for`, and test_server.py's own
    `_pump_until`) also fires each of them once per iteration, simulating a
    periodic QTimer tick without a real Qt event loop.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._timers: set[FakeTimer] = set()

    def schedule(self, cb: Callable[[], None]) -> None:
        self._queue.put(cb)

    def _fire_timers(self) -> None:
        for timer in list(self._timers):
            timer._fire()

    def run_until(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("run_until timed out waiting for predicate")
            self._fire_timers()
            try:
                cb = self._queue.get(timeout=min(remaining, 0.01))
            except queue.Empty:
                continue
            cb()

    def drain_for(self, duration: float) -> None:
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._fire_timers()
            try:
                cb = self._queue.get(timeout=min(remaining, 0.01))
            except queue.Empty:
                continue
            cb()


class _FakeSignal:
    """Structural stand-in for a Qt Signal's `.connect()` — see
    `impl._SignalProtocol`."""

    def __init__(self, owner: FakeTimer) -> None:
        self._owner = owner

    def connect(self, slot: Callable[[], None]) -> None:
        self._owner._callback = slot


class FakeTimer:
    """Minimal QTimer stand-in for `impl.Bridge`'s injectable `make_timer`.

    `start()` registers this timer with the owning `FakeMainLoop`
    so every pump also fires it; `stop()` unregisters it. Structurally
    matches `impl._TimerProtocol` (`setInterval`/`start`/`stop`/`timeout`)."""

    def __init__(self, loop: FakeMainLoop) -> None:
        self._loop = loop
        self._callback: Callable[[], None] | None = None
        self.interval_ms = 0

    def setInterval(self, msec: int) -> None:
        self.interval_ms = msec

    @property
    def timeout(self) -> _FakeSignal:
        return _FakeSignal(self)

    def start(self) -> None:
        self._loop._timers.add(self)

    def stop(self) -> None:
        self._loop._timers.discard(self)

    def _fire(self) -> None:
        if self._callback is not None:
            self._callback()


class FakeFlame:
    """A fake flame module for tests, with the attributes that exec'd code uses."""

    def __init__(self) -> None:
        self.marker = "fake-flame"


def _send_line(sock_path: Path, raw: bytes, timeout: float = 5.0) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall(raw)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf


def send_request(sock_path: Path, req: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    raw = (json.dumps(req) + "\n").encode("utf-8")
    resp = _send_line(sock_path, raw, timeout)
    result: dict[str, Any] = json.loads(resp.decode("utf-8"))
    return result


def _sock(bridge: impl.Bridge) -> Path:
    """bridge._sock_path is Optional until start(); tests only call this
    after start(), so assert it down to a plain Path for mypy's benefit."""
    path = bridge._sock_path
    assert path is not None
    return path


@pytest.fixture
def scratch_dir() -> Iterator[Path]:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    d = SCRATCH_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def loop() -> FakeMainLoop:
    return FakeMainLoop()


_UNSET: Any = object()


@pytest.fixture
def bridge_factory(scratch_dir: Path, loop: FakeMainLoop) -> Iterator[Callable[..., impl.Bridge]]:
    created: list[impl.Bridge] = []

    def _make(
        version: str = "2025.2.4",
        project_name: str | None = "proj",
        schedule: Any = _UNSET,
        flame_module: Any = None,
        make_timer: Any = _UNSET,
    ) -> impl.Bridge:
        bridge = impl.Bridge(
            schedule=loop.schedule if schedule is _UNSET else schedule,
            flame_module=FakeFlame() if flame_module is None else flame_module,
            version=version,
            project_name=project_name,
            run_dir=scratch_dir,
            log_path=scratch_dir / "bridge.log",
            make_timer=(lambda: FakeTimer(loop)) if make_timer is _UNSET else make_timer,
        )
        created.append(bridge)
        return bridge

    yield _make
    for bridge in created:
        try:
            bridge.stop()
        except Exception:
            pass


def test_hello_and_ping(bridge_factory: Callable[..., impl.Bridge]) -> None:
    bridge = bridge_factory(project_name="myproj")
    bridge.start()
    hello = send_request(_sock(bridge), {"v": 1, "id": "h1", "kind": "hello"})
    assert hello["ok"] is True
    assert hello["flameVersion"] == "2025.2.4"
    assert hello["projectName"] == "myproj"
    assert hello["bridgeAbi"] == impl.BRIDGE_ABI
    assert hello["pid"] == os.getpid()
    ping = send_request(_sock(bridge), {"v": 1, "id": "p1", "kind": "ping"})
    assert ping == {"id": "p1", "ok": True, "pong": True}


def test_exec_runs_on_main_thread(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "1",
                "kind": "exec",
                "code": (
                    "import threading\n"
                    "_result = threading.current_thread() is threading.main_thread()"
                ),
                "thread": "main",
                "timeoutS": 5,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    assert result["resp"]["ok"] is True
    assert result["resp"]["result"] is True
    assert isinstance(result["resp"]["queuedMs"], int)
    assert isinstance(result["resp"]["runMs"], int)


def test_exec_can_read_injected_flame_module(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "1",
                "kind": "exec",
                "code": "_result = flame.marker",
                "thread": "main",
                "timeoutS": 5,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    assert result["resp"]["result"] == "fake-flame"


def test_exec_exception_returns_flame_api_error(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "e1",
                "kind": "exec",
                "code": "raise ValueError('boom')",
                "thread": "main",
                "timeoutS": 5,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    resp = result["resp"]
    assert resp["ok"] is False
    assert resp["errorCode"] == "FLAME_API_ERROR"
    assert "boom" in resp["error"]
    assert "ValueError" in resp["traceback"]


def test_exec_non_json_result_is_flame_api_error(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "j1",
                "kind": "exec",
                "code": "class Foo:\n    pass\n_result = Foo()",
                "thread": "main",
                "timeoutS": 5,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    resp = result["resp"]
    assert resp["ok"] is False
    assert resp["errorCode"] == "FLAME_API_ERROR"


def test_exec_missing_result_defaults_to_none(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {"v": 1, "id": "n1", "kind": "exec", "code": "x = 1", "thread": "main", "timeoutS": 5},
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    assert result["resp"]["ok"] is True
    assert result["resp"]["result"] is None


def test_main_thread_unavailable_when_timer_is_none(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    """Checks main-thread work fails fast when the relay timer is None.

    The fast-fail path is "the relay timer isn't running"
    (start() never created/started one, or it failed) rather than "no
    schedule callable is bound" — a handler thread must never touch
    `self._schedule` at all, so it can't check that directly. This is a
    synchronous immediate response: nothing needs to pump a loop.
    """

    def raising_make_timer() -> Any:
        raise RuntimeError("no QTimer available in this environment")

    bridge = bridge_factory(make_timer=raising_make_timer)
    bridge.start()  # must not raise: a failed timer is degraded, not fatal
    assert bridge._timer is None
    resp = send_request(
        _sock(bridge),
        {"v": 1, "id": "t1", "kind": "exec", "code": "_result = 1", "thread": "main"},
    )
    assert resp["ok"] is False
    assert resp["errorCode"] == "MAIN_THREAD_UNAVAILABLE"


def test_main_thread_unavailable_when_schedule_missing(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    """Checks the missing schedule callable is found only on the main thread.

    With a running relay timer but no schedule callable bound,
    the failure is only discovered inside `_main_relay_tick` (main thread),
    not by the handler thread — so the loop must be pumped.
    """
    bridge = bridge_factory(schedule=None)
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {"v": 1, "id": "m1", "kind": "exec", "code": "_result = 1", "thread": "main"},
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    resp = result["resp"]
    assert resp["ok"] is False
    assert resp["errorCode"] == "MAIN_THREAD_UNAVAILABLE"


def test_main_thread_unavailable_when_schedule_raises(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    """`schedule()` is only called from `_main_relay_tick`, so a raise shows up after pumping."""

    def bad_schedule(cb: Callable[[], None]) -> None:
        raise RuntimeError("no idle events yet")

    bridge = bridge_factory(schedule=bad_schedule)
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {"v": 1, "id": "m2", "kind": "exec", "code": "_result = 1", "thread": "main"},
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    resp = result["resp"]
    assert resp["ok"] is False
    assert resp["errorCode"] == "MAIN_THREAD_UNAVAILABLE"


def test_schedule_is_only_invoked_from_main_thread(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    """Guards against a deadlock with Flame's main thread.

    Even though the exec
    *request* arrives on a separate handler thread, `self._schedule` (the
    injected `flame.schedule_idle_event` stand-in) must only ever be called
    from the relay tick on the main thread.
    """
    calling_threads: list[int] = []

    def recording_schedule(cb: Callable[[], None]) -> None:
        calling_threads.append(threading.get_ident())
        loop.schedule(cb)

    bridge = bridge_factory(schedule=recording_schedule)
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "s1",
                "kind": "exec",
                "code": "_result = 1",
                "thread": "main",
                "timeoutS": 5,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)

    assert result["resp"]["ok"] is True
    assert calling_threads == [threading.main_thread().ident]


def test_timeout_queued_then_never_executes(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop, scratch_dir: Path
) -> None:
    bridge = bridge_factory()
    bridge.start()
    marker = scratch_dir / "marker_queued.txt"
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "q1",
                "kind": "exec",
                "code": f"open({str(marker)!r}, 'w').write('ran')",
                "thread": "main",
                "timeoutS": 0.2,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    t.join(5)  # main loop is never pumped: the callback stays QUEUED
    resp = result["resp"]
    assert resp["ok"] is False
    assert resp["errorCode"] == "TIMEOUT"
    assert resp["timeoutPhase"] == "queued"
    # Only now let the (already-cancelled) callback reach the front of the
    # queue. It must check the cancellation mark and do nothing.
    loop.drain_for(1.0)
    assert not marker.exists()


def test_timeout_running_then_poll_gets_result(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "r1",
                "kind": "exec",
                "code": "import time\ntime.sleep(1.0)\n_result = 42",
                "thread": "main",
                "timeoutS": 0.2,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    resp = result["resp"]
    assert resp["ok"] is False
    assert resp["errorCode"] == "TIMEOUT"
    assert resp["timeoutPhase"] == "running"
    handle = resp["handle"]

    poll_resp = send_request(_sock(bridge), {"v": 1, "id": "p1", "kind": "poll", "handle": handle})
    assert poll_resp["ok"] is True
    assert poll_resp["result"] == 42

    # Second poll: the file was consumed, and the pending entry was cleared.
    poll_again = send_request(_sock(bridge), {"v": 1, "id": "p2", "kind": "poll", "handle": handle})
    assert poll_again["ok"] is False
    assert "protocolError" in poll_again


def test_main_async_pending_then_poll(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "a1",
                "kind": "exec",
                "code": "import time\ntime.sleep(1.0)\n_result = 'done'",
                "thread": "main-async",
                "timeoutS": 0.2,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    resp = result["resp"]
    assert resp["ok"] is True
    handle = resp["pending"]["handle"]

    poll_resp = send_request(_sock(bridge), {"v": 1, "id": "p2", "kind": "poll", "handle": handle})
    assert poll_resp["ok"] is True
    assert poll_resp["result"] == "done"


def test_poll_unknown_handle_is_protocol_error(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = send_request(_sock(bridge), {"v": 1, "id": "p1", "kind": "poll", "handle": "nope"})
    assert resp["ok"] is False
    assert "protocolError" in resp


@pytest.mark.parametrize("handle", ["../victim", "a/b", "A" * 32, "0" * 31, "0" * 33])
def test_poll_refuses_a_handle_that_is_not_32_lowercase_hex_and_reads_no_file(
    bridge_factory: Callable[..., impl.Bridge], handle: str
) -> None:
    bridge = bridge_factory()
    bridge.start()
    victim = bridge._results_dir.parent / "victim.json"
    victim.write_text(json.dumps({"ok": True, "result": "leaked"}), encoding="utf-8")
    inside = bridge._results_dir / f"{handle}.json"
    if "/" not in handle:
        inside.write_text(json.dumps({"ok": True, "result": "leaked"}), encoding="utf-8")
    resp = send_request(_sock(bridge), {"v": 1, "id": "p1", "kind": "poll", "handle": handle})
    assert resp["ok"] is False
    assert "protocolError" in resp
    assert resp.get("result") != "leaked"
    assert victim.exists()
    if "/" not in handle:
        assert inside.exists()


def test_fifo_order(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop, scratch_dir: Path
) -> None:
    bridge = bridge_factory()
    bridge.start()
    log_path = scratch_dir / "fifo_log.txt"
    n = 5
    responses: list[dict[str, Any] | None] = [None] * n

    def send_one(i: int) -> None:
        code = f"open({str(log_path)!r}, 'a').write('{i}\\n')\n_result = {i}"
        responses[i] = send_request(
            _sock(bridge),
            {"v": 1, "id": str(i), "kind": "exec", "code": code, "thread": "main", "timeoutS": 5},
            timeout=10,
        )

    threads = []
    for i in range(n):
        t = threading.Thread(target=send_one, args=(i,))
        t.start()
        threads.append(t)
        time.sleep(0.05)  # give each handler thread time to reach schedule() first

    loop.run_until(lambda: all(r is not None for r in responses), timeout=10)
    for t in threads:
        t.join(5)

    lines = log_path.read_text().splitlines()
    assert lines == [str(i) for i in range(n)]


def test_concurrent_connections_get_correct_results(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    results: dict[str, dict[str, Any]] = {}

    def worker(key: str, value: int) -> None:
        results[key] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": key,
                "kind": "exec",
                "code": f"_result = {value}",
                "thread": "main",
                "timeoutS": 5,
            },
            timeout=10,
        )

    threads = [threading.Thread(target=worker, args=(f"c{i}", i)) for i in range(10)]
    for t in threads:
        t.start()
    loop.run_until(lambda: len(results) == 10, timeout=10)
    for t in threads:
        t.join(5)
    for i in range(10):
        assert results[f"c{i}"]["result"] == i


def test_invalid_json_returns_protocol_error(bridge_factory: Callable[..., impl.Bridge]) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp_bytes = _send_line(_sock(bridge), b"not json at all\n")
    resp = json.loads(resp_bytes.decode("utf-8"))
    assert resp["ok"] is False
    assert "protocolError" in resp


def test_unknown_kind_returns_protocol_error(bridge_factory: Callable[..., impl.Bridge]) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = send_request(_sock(bridge), {"v": 1, "id": "u1", "kind": "frobnicate"})
    assert resp["ok"] is False
    assert "protocolError" in resp


def test_version_mismatch_returns_protocol_error(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = send_request(_sock(bridge), {"v": 2, "id": "v1", "kind": "ping"})
    assert resp["ok"] is False
    assert "protocolError" in resp


def test_missing_required_field_returns_protocol_error(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = send_request(_sock(bridge), {"v": 1, "id": "m1", "kind": "exec", "thread": "main"})
    assert resp["ok"] is False
    assert "protocolError" in resp


def test_line_length_limit(
    bridge_factory: Callable[..., impl.Bridge], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(impl, "MAX_LINE_BYTES", 200)
    bridge = bridge_factory()
    bridge.start()
    payload = {"v": 1, "id": "h", "kind": "ping", "pad": "x" * 500}
    raw = (json.dumps(payload) + "\n").encode("utf-8")
    resp_bytes = _send_line(_sock(bridge), raw)
    resp = json.loads(resp_bytes.decode("utf-8"))
    assert resp["ok"] is False
    assert "protocolError" in resp


def test_socket_and_rundir_permissions(bridge_factory: Callable[..., impl.Bridge]) -> None:
    bridge = bridge_factory()
    bridge.start()
    run_dir_mode = stat.S_IMODE(os.stat(bridge._run_dir).st_mode)
    sock_mode = stat.S_IMODE(os.stat(_sock(bridge)).st_mode)
    assert run_dir_mode == 0o700
    assert sock_mode == 0o600


def test_stale_socket_file_is_removed_then_bind_succeeds(
    bridge_factory: Callable[..., impl.Bridge], scratch_dir: Path
) -> None:
    sock_path = scratch_dir / "flame-mcp-2025.2.4.sock"
    sock_path.write_text("stale, not a real socket")
    bridge = bridge_factory()
    bridge.start()
    assert _sock(bridge) == sock_path
    hello = send_request(sock_path, {"v": 1, "id": "h", "kind": "hello"})
    assert hello["ok"] is True


def test_live_bridge_blocks_second_start(bridge_factory: Callable[..., impl.Bridge]) -> None:
    bridge_a = bridge_factory()
    bridge_a.start()
    bridge_b = bridge_factory()
    with pytest.raises(RuntimeError):
        bridge_b.start()


def test_stop_unlinks_only_own_socket(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    bridge.start()
    sock_path = _sock(bridge)
    os.unlink(sock_path)
    sock_path.write_text("someone else's file now occupies this path")
    bridge.stop()
    assert sock_path.exists()
    assert sock_path.read_text() == "someone else's file now occupies this path"


def test_socket_path_too_long_refuses_to_start(loop: FakeMainLoop) -> None:
    long_dir = SCRATCH_ROOT / ("x" * 90)
    long_dir.mkdir(parents=True, exist_ok=True)
    try:
        bridge = impl.Bridge(
            schedule=loop.schedule,
            flame_module=FakeFlame(),
            version="2025.2.4",
            project_name="p",
            run_dir=long_dir,
            log_path=long_dir / "b.log",
        )
        with pytest.raises(RuntimeError, match="too long"):
            bridge.start()
        assert not (long_dir / "flame-mcp-2025.2.4.sock").exists()
    finally:
        shutil.rmtree(long_dir, ignore_errors=True)


def test_stop_cancels_queued_requests(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    handle_box: dict[str, str] = {}
    state = impl._RequestState("test-handle")
    handle_box["id"] = state.id
    with bridge._pending_lock:
        bridge._pending[state.id] = state
    assert state.status == "QUEUED"
    bridge.stop()
    assert state.status == "CANCELLED"
    assert state.event.is_set()


def _exec_once(bridge: impl.Bridge, loop: FakeMainLoop, value: int) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": str(value),
                "kind": "exec",
                "code": f"_result = {value} + 1",
                "thread": "main",
                "timeoutS": 5,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    resp: dict[str, Any] = result["resp"]
    return resp


def test_200_consecutive_execs(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    for i in range(200):
        resp = _exec_once(bridge, loop, i)
        assert resp["ok"] is True, resp
        assert resp["result"] == i + 1


def _defined_names(node: ast.AST) -> Iterator[ast.FunctionDef]:
    for item in ast.walk(node):
        if isinstance(item, ast.FunctionDef):
            yield item


def test_flame_and_schedule_are_only_referenced_inside_main_prefixed_functions() -> None:
    """Checks `_flame` and `_schedule` are referenced only inside `_main_*` functions.

    `self._schedule` is treated exactly like `self._flame`: a handler thread
    calling it directly is what deadlocked against Flame's main thread. Only
    `_main_*` functions (currently `_main_relay_tick` and `_main_run_exec`)
    may reference either. Sensitivity: adding `self._schedule(...)` inside
    `_handle_exec` (a non-`_main_` function) must fail this test.
    """
    impl_path = Path(impl.__file__)
    tree = ast.parse(impl_path.read_text(encoding="utf-8"), filename=str(impl_path))

    main_function_bodies: list[ast.AST] = []
    other_function_nodes: list[ast.FunctionDef] = []
    for func in _defined_names(tree):
        if func.name.startswith("_main_"):
            main_function_bodies.append(func)
        else:
            other_function_nodes.append(func)

    def _is_inside_any(node: ast.AST, containers: list[ast.AST]) -> bool:
        for container in containers:
            for child in ast.walk(container):
                if child is node:
                    return True
        return False

    restricted_attrs = {"_flame", "_schedule"}
    violations = []
    for func in other_function_nodes:
        for node in ast.walk(func):
            if isinstance(node, ast.Name) and node.id == "flame" and isinstance(node.ctx, ast.Load):
                if not _is_inside_any(node, main_function_bodies):
                    violations.append((func.name, "Name 'flame'", node.lineno))
            if (
                isinstance(node, ast.Attribute)
                and node.attr in restricted_attrs
                and isinstance(node.ctx, ast.Load)
            ):
                if not _is_inside_any(node, main_function_bodies):
                    violations.append((func.name, f"Attribute '{node.attr}'", node.lineno))

    assert violations == [], f"flame/_schedule referenced outside _main_* functions: {violations}"


def test_errorcode_strings_are_subset_of_flame_mcp_errorcode() -> None:
    from typing import get_args

    from flame_mcp.types import ErrorCode

    allowed = set(get_args(ErrorCode))
    impl_path = Path(impl.__file__)
    tree = ast.parse(impl_path.read_text(encoding="utf-8"), filename=str(impl_path))

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "errorCode"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)

    assert found, "no errorCode string literals found — did the extraction break?"
    assert found <= allowed, f"unknown ErrorCode values in impl.py: {found - allowed}"


# _resolve_after_wait decides everything in one locked section. Reading state.status under one
# acquisition and then setting state.needs_file under a second would let _finish_call run in
# between: it would see needs_file still False and skip the result-file write, while the handler
# had already committed to returning a handle for a result that is never persisted, so the poll
# for that handle would stay pending forever.


def test_resolve_after_wait_done_returns_result_and_finished(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    state = impl._RequestState("h1")
    state.status = "DONE"
    state.result = {"ok": True, "result": 123, "queuedMs": 0, "runMs": 0}

    response, finished = bridge._resolve_after_wait(state, "main")

    assert finished is True
    assert response == {"ok": True, "result": 123, "queuedMs": 0, "runMs": 0}
    assert state.needs_file is False  # already DONE: must not be touched


def test_resolve_after_wait_running_main_marks_needs_file_and_returns_running(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    state = impl._RequestState("h2")
    state.status = "RUNNING"

    response, finished = bridge._resolve_after_wait(state, "main")

    assert finished is False
    assert response == {
        "ok": False,
        "errorCode": "TIMEOUT",
        "timeoutPhase": "running",
        "handle": "h2",
    }
    assert state.needs_file is True
    assert state.status == "RUNNING"  # only the callback may transition to DONE


def test_resolve_after_wait_running_main_async_marks_needs_file_and_returns_pending(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    state = impl._RequestState("h3")
    state.status = "RUNNING"

    response, finished = bridge._resolve_after_wait(state, "main-async")

    assert finished is False
    assert response == {"ok": True, "pending": {"handle": "h3"}}
    assert state.needs_file is True


def test_resolve_after_wait_queued_main_cancels_and_returns_queued_timeout(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    state = impl._RequestState("h4")
    state.status = "QUEUED"

    response, finished = bridge._resolve_after_wait(state, "main")

    assert finished is True
    assert response == {"ok": False, "errorCode": "TIMEOUT", "timeoutPhase": "queued"}
    assert state.status == "CANCELLED"
    assert state.needs_file is False  # cancelled before running: no file will ever come


def test_resolve_after_wait_queued_main_async_marks_needs_file_without_cancelling(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    state = impl._RequestState("h5")
    state.status = "QUEUED"

    response, finished = bridge._resolve_after_wait(state, "main-async")

    assert finished is False
    assert response == {"ok": True, "pending": {"handle": "h5"}}
    assert state.status == "QUEUED"  # main-async never cancels
    assert state.needs_file is True


def test_resolve_after_wait_cancelled_returns_queued_timeout_regardless_of_thread(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    for thread_mode in ("main", "main-async"):
        state = impl._RequestState(f"h6-{thread_mode}")
        state.status = "CANCELLED"
        response, finished = bridge._resolve_after_wait(state, thread_mode)
        assert finished is True
        assert response == {"ok": False, "errorCode": "TIMEOUT", "timeoutPhase": "queued"}


def _race_resolve_against_finish(bridge: impl.Bridge, value: int) -> None:
    state = impl._RequestState(f"stress-{value}")
    state.status = "RUNNING"
    expected = {"ok": True, "result": value, "queuedMs": 0, "runMs": 0}
    outcome: dict[str, Any] = {}

    def call_resolve() -> None:
        response, finished = bridge._resolve_after_wait(state, "main")
        outcome["response"] = response
        outcome["finished"] = finished

    def call_finish() -> None:
        bridge._finish_call(state, dict(expected))

    t1 = threading.Thread(target=call_resolve)
    t2 = threading.Thread(target=call_finish)
    t1.start()
    t2.start()
    t1.join(5)
    t2.join(5)

    with state.lock:
        needs_file_final = state.needs_file
        final_status = state.status
        final_result = state.result

    assert final_status == "DONE"
    assert final_result == expected
    if outcome["finished"]:
        assert outcome["response"] == final_result
    else:
        assert needs_file_final is True


def test_resolve_after_wait_and_finish_call_no_lost_update_stress(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    """Race `_resolve_after_wait` against `_finish_call` on the same state
    from two real threads, many times. Whichever wins the lock first, the
    other must see a consistent picture afterward: either the result was
    already there (finished=True), or needs_file was marked before
    `_finish_call` read it (so the file got written)."""
    bridge = bridge_factory()
    for i in range(200):
        _race_resolve_against_finish(bridge, i)


def test_write_result_file_failure_does_not_escape_callback(
    bridge_factory: Callable[..., impl.Bridge],
    loop: FakeMainLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = bridge_factory()
    bridge.start()

    def boom(handle: str, response: dict[str, Any]) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(bridge, "_write_result_file", boom)

    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {
                "v": 1,
                "id": "wf1",
                "kind": "exec",
                "code": "import time\ntime.sleep(1.0)\n_result = 99",
                "thread": "main",
                "timeoutS": 0.2,
            },
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    # If the OSError from `boom` escaped `_main_run_exec`, it would propagate
    # out of this call and fail the test right here (not as an assertion).
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)

    resp = result["resp"]
    assert resp["ok"] is False
    assert resp["errorCode"] == "TIMEOUT"
    assert resp["timeoutPhase"] == "running"

    # The bridge must still be fully functional afterward.
    ping = send_request(_sock(bridge), {"v": 1, "id": "p", "kind": "ping"})
    assert ping["ok"] is True


class _RecordingConn:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


def test_process_line_after_stop_flag_returns_protocol_error(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    bridge._stop_event.set()
    conn = _RecordingConn()

    bridge._process_line(cast(socket.socket, conn), b'{"v": 1, "id": "x", "kind": "ping"}')

    assert len(conn.sent) == 1
    body = json.loads(conn.sent[0].decode("utf-8").strip())
    assert body == {"id": None, "ok": False, "protocolError": "bridge stopped"}


def test_handle_exec_refuses_to_schedule_after_stop_flag(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    bridge._stop_event.set()  # simulate "stop in progress" without full teardown

    resp = bridge._handle_exec("x1", {"code": "_result = 1", "thread": "main", "timeoutS": 5})

    assert resp == {"id": "x1", "ok": False, "protocolError": "bridge stopped"}
    assert bridge._relay_queue.empty()  # nothing was ever enqueued onto the relay
    assert loop._queue.empty()  # ... and so nothing ever reached "main" either
    with bridge._pending_lock:
        assert bridge._pending == {}


def test_stop_stops_relay_timer(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    timer = bridge._timer
    assert timer is not None
    assert timer in loop._timers

    bridge.stop()

    assert bridge._timer is None
    assert timer not in loop._timers


def test_stop_cancels_request_still_in_relay_queue(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    """Checks stop() cancels a request still in `_relay_queue`.

    A request can be sitting in `_relay_queue`, never having
    reached `self._schedule` at all, when stop() runs — it must still be
    cancelled (the loop/timer is deliberately never pumped here, so the item
    can only leave the relay queue via stop()'s own cancellation, not via
    `_main_relay_tick`).

    Assertions are on `state` directly rather than the client's received
    response: stop() force-closing still-open connections races
    against this same handler thread waking from cancellation and trying to
    send that response, which is a property of stop()'s
    connection teardown (see test_stop_force_closes_existing_idle_connection)
    and not what this test is about.
    """
    bridge = bridge_factory()
    bridge.start()

    def client() -> None:
        try:
            send_request(
                _sock(bridge),
                {
                    "v": 1,
                    "id": "rq1",
                    "kind": "exec",
                    "code": "_result = 1",
                    "thread": "main",
                    "timeoutS": 5,
                },
                timeout=5,
            )
        except (OSError, json.JSONDecodeError):
            pass

    t = threading.Thread(target=client)
    t.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and bridge._relay_queue.empty():
        time.sleep(0.01)
    assert not bridge._relay_queue.empty()
    with bridge._pending_lock:
        (state,) = bridge._pending.values()

    bridge.stop()
    t.join(5)

    assert state.status == "CANCELLED"
    assert state.event.is_set()


def test_stop_force_closes_existing_idle_connection(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    bridge.start()
    client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client_sock.settimeout(5)
    client_sock.connect(str(_sock(bridge)))
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(bridge._connections) == 0:
            time.sleep(0.01)
        assert len(bridge._connections) == 1

        bridge.stop()

        client_sock.settimeout(2.0)
        data = client_sock.recv(1024)
        assert data == b""  # EOF: stop() actively shut this connection down
    finally:
        client_sock.close()


def _run_stop_delivers_queued_timeout_once(bridge: impl.Bridge, i: int) -> None:
    bridge.start()
    result: dict[str, Any] = {}
    errors: list[BaseException] = []

    def client() -> None:
        try:
            result["resp"] = send_request(
                _sock(bridge),
                {
                    "v": 1,
                    "id": f"race{i}",
                    "kind": "exec",
                    "code": "_result = 1",
                    "thread": "main",
                    "timeoutS": 5,
                },
                timeout=5,
            )
        except BaseException as exc:  # captured for assertion, not swallowed
            errors.append(exc)

    t = threading.Thread(target=client)
    t.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and bridge._relay_queue.empty():
        time.sleep(0.001)
    assert not bridge._relay_queue.empty(), f"iteration {i}: request never reached the relay queue"

    bridge.stop()
    t.join(5)

    assert errors == [], f"iteration {i}: client saw {errors!r} instead of a response"
    assert result.get("resp") == {
        "id": f"race{i}",
        "ok": False,
        "errorCode": "TIMEOUT",
        "timeoutPhase": "queued",
    }, f"iteration {i}: unexpected response {result.get('resp')!r}"


def test_stop_delivers_queued_timeout_response_instead_of_eof(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    """Checks stop() delivers a queued TIMEOUT response instead of EOF.

    stop() must not shutdown(SHUT_RDWR)+close() every live connection right
    after cancelling queued work: that races the handler thread it has just
    woken via `state.event.set()`, which is about to `sendall()` its
    TIMEOUT/queued response (the client then sees EOF, not a response). This is
    a timing race, not deterministic on any single run, so it is repeated 20
    times: every one of the 20 must deliver the actual response, never EOF
    or a connection error.
    """
    for i in range(20):
        _run_stop_delivers_queued_timeout_once(bridge_factory(), i)


def test_set_project_name_updates_hello(bridge_factory: Callable[..., impl.Bridge]) -> None:
    bridge = bridge_factory(project_name="proj1")
    bridge.start()
    bridge.set_project_name("proj2")

    hello = send_request(_sock(bridge), {"v": 1, "id": "h", "kind": "hello"})

    assert hello["projectName"] == "proj2"


# Result files older than 24h are swept on start().


def test_start_cleans_up_stale_result_files(
    bridge_factory: Callable[..., impl.Bridge], scratch_dir: Path
) -> None:
    results_dir = scratch_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    old_file = results_dir / "old.json"
    old_file.write_text("{}")
    fresh_file = results_dir / "fresh.json"
    fresh_file.write_text("{}")
    old_time = time.time() - 25 * 3600
    os.utime(old_file, (old_time, old_time))

    bridge = bridge_factory()
    bridge.start()

    assert not old_file.exists()
    assert fresh_file.exists()


CLIENT_HANDLE = "0123456789abcdef0123456789abcdef"
OTHER_HANDLE = "fedcba9876543210fedcba9876543210"


def _exec_req(rid: str, code: str, thread: str, timeout_s: float, **extra: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "id": rid,
        "kind": "exec",
        "code": code,
        "thread": thread,
        "timeoutS": timeout_s,
        **extra,
    }


def _exec_pumped(
    bridge: impl.Bridge, loop: FakeMainLoop, req: dict[str, Any], timeout: float = 5.0
) -> dict[str, Any]:
    """Send one request from a client thread while this (main) thread pumps the fake loop."""
    box: dict[str, Any] = {}

    def client() -> None:
        box["resp"] = send_request(_sock(bridge), req, timeout=timeout)

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in box, timeout=timeout)
    t.join(timeout)
    resp: dict[str, Any] = box["resp"]
    return resp


def test_hello_announces_client_handle_and_the_abi_did_not_change(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    bridge.start()
    hello = send_request(_sock(bridge), {"v": 1, "id": "h", "kind": "hello"})
    assert hello["features"] == ["client_handle"]
    assert hello["bridgeAbi"] == 1 == impl.BRIDGE_ABI


def test_a_client_handle_is_the_state_id_and_its_result_file_is_written_even_in_band(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop, scratch_dir: Path
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = _exec_pumped(
        bridge,
        loop,
        _exec_req("c1", "_result = 7", "main-async", 5.0, handle=CLIENT_HANDLE),
    )
    assert resp["ok"] is True and resp["result"] == 7 and "pending" not in resp
    result_file = scratch_dir / "results" / f"{CLIENT_HANDLE}.json"
    assert result_file.is_file(), "an in-band answer must leave the result file too"
    assert json.loads(result_file.read_text())["result"] == 7

    polled = send_request(
        _sock(bridge), {"v": 1, "id": "p1", "kind": "poll", "handle": CLIENT_HANDLE}
    )
    assert polled["ok"] is True and polled["result"] == 7
    assert not result_file.exists()
    again = send_request(
        _sock(bridge), {"v": 1, "id": "p2", "kind": "poll", "handle": CLIENT_HANDLE}
    )
    assert "unknown handle" in again["protocolError"]


def test_a_client_handle_run_that_raised_also_leaves_its_result_file(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop, scratch_dir: Path
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = _exec_pumped(
        bridge,
        loop,
        _exec_req("c1", "raise RuntimeError('boom')", "main-async", 5.0, handle=CLIENT_HANDLE),
    )
    assert resp["errorCode"] == "FLAME_API_ERROR"
    saved = json.loads((scratch_dir / "results" / f"{CLIENT_HANDLE}.json").read_text())
    assert saved["ok"] is False and saved["errorCode"] == "FLAME_API_ERROR"


def test_the_result_file_is_written_before_the_waiting_handler_is_woken(
    bridge_factory: Callable[..., impl.Bridge],
    loop: FakeMainLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the handler could answer in-band and drop the pending entry while the file is
    still to come: a poll in that window would find neither."""
    bridge = bridge_factory()
    bridge.start()
    seen: list[bool] = []
    real_write = bridge._write_result_file

    def spying_write(handle: str, response: dict[str, Any]) -> None:
        seen.append(bridge._pending[handle].event.is_set())
        real_write(handle, response)

    monkeypatch.setattr(bridge, "_write_result_file", spying_write)
    _exec_pumped(
        bridge, loop, _exec_req("c1", "_result = 1", "main-async", 5.0, handle=CLIENT_HANDLE)
    )
    assert seen == [False]


def test_a_client_handle_is_what_a_pending_answer_and_the_late_file_use(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = _exec_pumped(
        bridge,
        loop,
        _exec_req(
            "c1",
            "import time\ntime.sleep(0.6)\n_result = 'late'",
            "main-async",
            0.2,
            handle=CLIENT_HANDLE,
        ),
    )
    assert resp["ok"] is True and resp["pending"] == {"handle": CLIENT_HANDLE}
    loop.drain_for(1.0)
    polled = send_request(
        _sock(bridge), {"v": 1, "id": "p1", "kind": "poll", "handle": CLIENT_HANDLE}
    )
    assert polled["ok"] is True and polled["result"] == "late"


def test_a_run_without_a_handle_still_gets_its_own_uuid(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop, scratch_dir: Path
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = _exec_pumped(
        bridge,
        loop,
        _exec_req("c1", "import time\ntime.sleep(0.6)\n_result = 3", "main-async", 0.2),
    )
    handle = resp["pending"]["handle"]
    assert len(handle) == 32 and handle != CLIENT_HANDLE
    loop.drain_for(1.0)
    assert (
        send_request(_sock(bridge), {"v": 1, "id": "p", "kind": "poll", "handle": handle})["result"]
        == 3
    )


def test_a_handle_already_pending_is_refused_and_the_code_does_not_run(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    flame = FakeFlame()
    bridge = bridge_factory(flame_module=flame)
    bridge.start()
    first = _exec_pumped(
        bridge,
        loop,
        _exec_req(
            "c1",
            "import time\ntime.sleep(0.6)\n_result = 1",
            "main-async",
            0.2,
            handle=CLIENT_HANDLE,
        ),
    )
    assert first["pending"] == {"handle": CLIENT_HANDLE}
    second = send_request(
        _sock(bridge),
        _exec_req("c2", "flame.ran = True\n_result = 2", "main-async", 0.2, handle=CLIENT_HANDLE),
    )
    assert second["ok"] is False and "already in use" in second["protocolError"]
    loop.drain_for(1.0)
    assert not hasattr(flame, "ran")
    polled = send_request(
        _sock(bridge), {"v": 1, "id": "p1", "kind": "poll", "handle": CLIENT_HANDLE}
    )
    assert polled["result"] == 1


def test_a_handle_whose_result_file_exists_is_refused(
    bridge_factory: Callable[..., impl.Bridge], loop: FakeMainLoop
) -> None:
    bridge = bridge_factory()
    bridge.start()
    _exec_pumped(
        bridge, loop, _exec_req("c1", "_result = 1", "main-async", 5.0, handle=CLIENT_HANDLE)
    )
    again = send_request(
        _sock(bridge), _exec_req("c2", "_result = 2", "main-async", 0.2, handle=CLIENT_HANDLE)
    )
    assert "already in use" in again["protocolError"]
    # the first result is untouched
    polled = send_request(
        _sock(bridge), {"v": 1, "id": "p", "kind": "poll", "handle": CLIENT_HANDLE}
    )
    assert polled["result"] == 1
    # and once handed over, the same handle may be used again
    resp = _exec_pumped(
        bridge, loop, _exec_req("c3", "_result = 3", "main-async", 5.0, handle=CLIENT_HANDLE)
    )
    assert resp["result"] == 3


def test_a_handle_with_thread_main_is_refused(
    bridge_factory: Callable[..., impl.Bridge], scratch_dir: Path
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = send_request(
        _sock(bridge), _exec_req("c1", "_result = 1", "main", 0.2, handle=CLIENT_HANDLE)
    )
    assert resp["ok"] is False and "main-async" in resp["protocolError"]
    assert not (scratch_dir / "results" / f"{CLIENT_HANDLE}.json").exists()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        CLIENT_HANDLE.upper(),
        CLIENT_HANDLE + "0",
        CLIENT_HANDLE[:-1] + "\n",
        "g" * 32,
        "../" + "0" * 29,
        123,
        None,
        ["a"],
    ],
)
def test_a_malformed_handle_is_a_protocol_error(
    bridge_factory: Callable[..., impl.Bridge], bad: Any
) -> None:
    bridge = bridge_factory()
    bridge.start()
    resp = send_request(
        _sock(bridge), _exec_req("c1", "_result = 1", "main-async", 0.2, handle=bad)
    )
    assert resp["ok"] is False and "invalid 'handle'" in resp["protocolError"]


# -- hardening: file permissions, connection limits, input validation ---------


@pytest.fixture
def umask_zero() -> Iterator[None]:
    previous = os.umask(0)
    yield
    os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _close_bridge_logger() -> None:
    logger = logging.getLogger("flame_mcp_bridge")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_log_file_and_rotated_files_are_private_even_with_umask_zero(
    tmp_path: Path, umask_zero: None
) -> None:
    log_path = tmp_path / "logs" / "bridge.log"
    try:
        logger = impl._make_logger(log_path)
        logger.info("first")
        handler = logger.handlers[0]
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        handler.doRollover()
        logger.info("second")
        handler.flush()
        assert _mode(log_path) == 0o600
        assert _mode(tmp_path / "logs" / "bridge.log.1") == 0o600
        assert _mode(log_path.parent) == 0o700
    finally:
        _close_bridge_logger()


def test_an_existing_loose_log_and_its_rotations_are_tightened(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for name in ("bridge.log", "bridge.log.1", "bridge.log.3"):
        (log_dir / name).write_text("old\n")
        (log_dir / name).chmod(0o666)
    try:
        impl._make_logger(log_dir / "bridge.log")
        for name in ("bridge.log", "bridge.log.1", "bridge.log.3"):
            assert _mode(log_dir / name) == 0o600
    finally:
        _close_bridge_logger()


def _wait_closed(conn: socket.socket, timeout: float) -> bool:
    conn.settimeout(timeout)
    try:
        return conn.recv(1) == b""
    except (TimeoutError, ConnectionResetError):
        return False


def _connect(bridge: impl.Bridge) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(_sock(bridge)))
    return s


def test_connections_over_the_limit_are_closed_at_once(
    bridge_factory: Callable[..., impl.Bridge], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(impl, "MAX_CONNECTIONS", 2)
    bridge = bridge_factory()
    bridge.start()
    kept = [_connect(bridge), _connect(bridge)]
    try:
        deadline = time.monotonic() + 3
        while len(bridge._connections) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        extra = _connect(bridge)
        try:
            assert _wait_closed(extra, 3.0)
        finally:
            extra.close()
        for s in kept:
            s.sendall(b'{"v": 1, "id": "p", "kind": "ping"}\n')
            s.settimeout(3)
            assert json.loads(s.recv(4096))["pong"] is True
    finally:
        for s in kept:
            s.close()


def test_a_slot_frees_up_when_a_connection_closes(
    bridge_factory: Callable[..., impl.Bridge], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(impl, "MAX_CONNECTIONS", 1)
    bridge = bridge_factory()
    bridge.start()
    first = _connect(bridge)
    deadline = time.monotonic() + 3
    while not bridge._connections and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bridge._connections
    first.close()
    while bridge._connections and time.monotonic() < deadline:
        time.sleep(0.01)
    resp = send_request(_sock(bridge), {"v": 1, "id": "p", "kind": "ping"})
    assert resp["pong"] is True


def test_an_idle_connection_is_closed_after_the_idle_timeout(
    bridge_factory: Callable[..., impl.Bridge], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(impl, "CONNECTION_IDLE_TIMEOUT_S", 0.3)
    bridge = bridge_factory()
    bridge.start()
    s = _connect(bridge)
    try:
        assert _wait_closed(s, 3.0)
    finally:
        s.close()


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_timeout_is_a_protocol_error(
    bridge_factory: Callable[..., impl.Bridge], raw: str
) -> None:
    bridge = bridge_factory()
    bridge.start()
    line = (
        '{"v": 1, "id": "t1", "kind": "exec", "code": "_result = 1", '
        f'"thread": "main", "timeoutS": {raw}}}\n'
    ).encode()
    resp = json.loads(_send_line(_sock(bridge), line))
    assert resp["ok"] is False and resp["protocolError"] == "invalid 'timeoutS'"


def test_an_unexpected_error_in_a_handler_answers_with_a_protocol_error(
    bridge_factory: Callable[..., impl.Bridge], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = bridge_factory()
    bridge.start()
    original = bridge._handle_ping
    calls: list[str] = []

    def flaky(req_id: str) -> dict[str, Any]:
        calls.append(req_id)
        if len(calls) == 1:
            raise RuntimeError("handler exploded")
        return original(req_id)

    monkeypatch.setattr(bridge, "_handle_ping", flaky)
    s = _connect(bridge)
    try:
        s.settimeout(3)
        s.sendall(b'{"v": 1, "id": "a", "kind": "ping"}\n')
        first = json.loads(s.recv(4096))
        assert first["id"] == "a" and first["ok"] is False
        assert "protocolError" in first and "handler exploded" not in first["protocolError"]
        s.sendall(b'{"v": 1, "id": "b", "kind": "ping"}\n')
        assert json.loads(s.recv(4096)) == {"id": "b", "ok": True, "pong": True}
    finally:
        s.close()


def test_deeply_nested_json_is_a_protocol_error_not_a_dead_handler(
    bridge_factory: Callable[..., impl.Bridge],
) -> None:
    bridge = bridge_factory()
    bridge.start()
    s = _connect(bridge)
    try:
        s.settimeout(5)
        s.sendall(b"[" * 200_000 + b"\n")
        resp = json.loads(s.recv(4096))
        assert resp["ok"] is False and "protocolError" in resp
        s.sendall(b'{"v": 1, "id": "b", "kind": "ping"}\n')
        assert json.loads(s.recv(4096))["pong"] is True
    finally:
        s.close()


def test_home_is_replaced_by_a_tilde_in_returned_tracebacks(
    bridge_factory: Callable[..., impl.Bridge],
    loop: FakeMainLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/Users/someone")
    bridge = bridge_factory()
    bridge.start()
    code = "raise ValueError('/Users/someone/proj/x.batch and /Users/someoneelse/y')"
    resp = _exec_pumped(bridge, loop, _exec_req("r1", code, "main", 5))
    assert resp["ok"] is False
    assert "/Users/someone/" not in resp["traceback"]
    assert "~/proj/x.batch" in resp["traceback"]
    assert "/Users/someoneelse/y" in resp["traceback"]
    assert "/Users/someone/" not in resp["error"]


def test_home_is_replaced_in_a_syntax_error_traceback_too(
    bridge_factory: Callable[..., impl.Bridge], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/Users/someone")
    bridge = bridge_factory()
    bridge.start()
    resp = send_request(_sock(bridge), _exec_req("s1", "x = '/Users/someone/a", "main", 5))
    assert resp["errorCode"] == "FLAME_API_ERROR"
    assert "/Users/someone" not in json.dumps(resp)
