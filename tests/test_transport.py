"""Tests the transport (no Flame required).

Two bridge doubles are used, matching two different concerns:
  - the real `flame_mcp_bridge.impl.Bridge` (+ test_bridge_impl's
    FakeMainLoop) for everything the real wire protocol actually produces
    (hello/exec/timeout/main-async/poll);
  - a minimal hand-rolled `FakeRawBridge` for wire-level failure modes the
    real bridge never produces by construction (mid-response EOF, silently
    hanging forever, a `protocolError`, an unknown `errorCode`) — these
    exist to drive transport.py's own BRIDGE_LOST/TIMEOUT/BridgeProtocolError
    classification, not the bridge's behavior.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import applog, transport
from flame_mcp_bridge import impl
from test_bridge_impl import FakeFlame, FakeMainLoop, FakeTimer

SCRATCH_ROOT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "p13"

# Read at import time, before `_fast_client_timeouts` shrinks the live constant.
PRODUCTION_CLIENT_TIMEOUT_MARGIN_S = transport.CLIENT_TIMEOUT_MARGIN_S


@pytest.fixture
def scratch_dir() -> Iterator[Path]:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    d = SCRATCH_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def run_dir(scratch_dir: Path) -> Path:
    d = scratch_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(autouse=True)
def _fast_client_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    # The "broken bridge" tests below deliberately let the client time out;
    # shrinking these constants keeps that a sub-second wait instead of the
    # real (timeout_s + 5.0s) + 1.0s + 1.0s production values. The one test
    # that specifically exercises the crash-log grace period overrides
    # CRASH_LOG_GRACE_S back up to a small positive value itself.
    monkeypatch.setattr(transport, "CLIENT_TIMEOUT_MARGIN_S", 0.05)
    monkeypatch.setattr(transport, "HELLO_TIMEOUT_S", 0.1)
    monkeypatch.setattr(transport, "CRASH_LOG_GRACE_S", 0.0)


@pytest.fixture
def bridge_answers_within_production_margin(
    _fast_client_timeouts: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # For tests that expect the bridge's own answer to a short `timeout_s`: the
    # bridge sends that answer only after `timeout_s` has elapsed, so with the
    # shrunken margin the client gives up (and reports "bridge alive but no
    # reply") whenever a slow runner delays the reply by more than 0.05 s.
    monkeypatch.setattr(transport, "CLIENT_TIMEOUT_MARGIN_S", PRODUCTION_CLIENT_TIMEOUT_MARGIN_S)


@pytest.fixture(autouse=True)
def _fake_logs(monkeypatch: pytest.MonkeyPatch, scratch_dir: Path) -> None:
    # Point applog's env-var overrides at scratch files instead of this
    # machine's real /opt/Autodesk/log — the transport layer always marks
    # and reads these on every call, so they must exist and be inert.
    monkeypatch.setenv(applog.APP_LOG_ENV, str(scratch_dir / "app.log"))
    monkeypatch.setenv(applog.SHELL_LOG_ENV, str(scratch_dir / "shell.log"))
    monkeypatch.delenv(transport.BRIDGE_SOCKET_ENV, raising=False)


@pytest.fixture
def loop() -> FakeMainLoop:
    return FakeMainLoop()


@pytest.fixture
def real_bridge(run_dir: Path, scratch_dir: Path, loop: FakeMainLoop) -> Iterator[impl.Bridge]:
    bridge = impl.Bridge(
        schedule=loop.schedule,
        flame_module=FakeFlame(),
        version="2025.2.4",
        project_name="proj",
        run_dir=run_dir,
        log_path=scratch_dir / "bridge.log",
        make_timer=lambda: FakeTimer(loop),
    )
    bridge.start()
    yield bridge
    try:
        bridge.stop()
    except Exception:
        pass


def _inflight_files(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "inflight").glob("*.json")) if (run_dir / "inflight").is_dir() else []


def test_timeout_for_main() -> None:
    assert transport.timeout_for("main") == transport.MAIN_TIMEOUT_S


def test_timeout_for_main_async() -> None:
    assert transport.timeout_for("main-async") == transport.MAIN_ASYNC_TIMEOUT_S


def test_timeout_for_server_raises_value_error() -> None:
    with pytest.raises(ValueError):
        transport.timeout_for("server")


def test_exec_rejects_server_thread(run_dir: Path) -> None:
    with pytest.raises(ValueError):
        transport.exec("_result = 1", thread="server", run_dir=run_dir)


def test_discover_zero_sockets_is_unavailable(run_dir: Path) -> None:
    failure = transport._discover(run_dir)
    assert isinstance(failure, transport._DiscoveryFailure)
    assert failure.error_code == "BRIDGE_UNAVAILABLE"


def test_discover_stale_socket_file_only_is_unavailable(run_dir: Path) -> None:
    (run_dir / "flame-mcp-9999.sock").write_text("not a real socket")
    failure = transport._discover(run_dir)
    assert isinstance(failure, transport._DiscoveryFailure)
    assert failure.error_code == "BRIDGE_UNAVAILABLE"


def test_discover_one_alive_bridge_is_selected(real_bridge: impl.Bridge, run_dir: Path) -> None:
    candidate = transport._discover(run_dir)
    assert isinstance(candidate, transport.BridgeCandidate)
    assert candidate.pid == os.getpid()
    assert candidate.flame_version == "2025.2.4"
    assert candidate.project_name == "proj"
    assert candidate.bridge_abi == impl.BRIDGE_ABI


def test_discover_two_alive_bridges_is_ambiguous(
    scratch_dir: Path, run_dir: Path, loop: FakeMainLoop
) -> None:
    bridge_a = impl.Bridge(
        schedule=loop.schedule,
        flame_module=FakeFlame(),
        version="2025.2.4",
        project_name="alpha",
        run_dir=run_dir,
        log_path=scratch_dir / "a.log",
        make_timer=lambda: FakeTimer(loop),
    )
    bridge_b = impl.Bridge(
        schedule=loop.schedule,
        flame_module=FakeFlame(),
        version="2025.2.2",
        project_name="beta",
        run_dir=run_dir,
        log_path=scratch_dir / "b.log",
        make_timer=lambda: FakeTimer(loop),
    )
    bridge_a.start()
    bridge_b.start()
    try:
        failure = transport._discover(run_dir)
        assert isinstance(failure, transport._DiscoveryFailure)
        assert failure.error_code == "BRIDGE_AMBIGUOUS"
        assert str(bridge_a._sock_path) in failure.message
        assert str(bridge_b._sock_path) in failure.message
        assert "alpha" in failure.message
        assert "beta" in failure.message
        assert transport.BRIDGE_SOCKET_ENV in failure.message
    finally:
        bridge_a.stop()
        bridge_b.stop()


def test_discover_flame_bridge_socket_env_selects_that_one(
    scratch_dir: Path, run_dir: Path, loop: FakeMainLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_a = impl.Bridge(
        schedule=loop.schedule,
        flame_module=FakeFlame(),
        version="2025.2.4",
        project_name="alpha",
        run_dir=run_dir,
        log_path=scratch_dir / "a.log",
        make_timer=lambda: FakeTimer(loop),
    )
    bridge_b = impl.Bridge(
        schedule=loop.schedule,
        flame_module=FakeFlame(),
        version="2025.2.2",
        project_name="beta",
        run_dir=run_dir,
        log_path=scratch_dir / "b.log",
        make_timer=lambda: FakeTimer(loop),
    )
    bridge_a.start()
    bridge_b.start()
    try:
        assert bridge_b._sock_path is not None
        monkeypatch.setenv(transport.BRIDGE_SOCKET_ENV, str(bridge_b._sock_path))
        candidate = transport._discover(run_dir)
        assert isinstance(candidate, transport.BridgeCandidate)
        assert candidate.project_name == "beta"
    finally:
        bridge_a.stop()
        bridge_b.stop()


def test_discover_flame_bridge_socket_env_dead_path_is_unavailable(
    run_dir: Path, scratch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_path = scratch_dir / "flame-mcp-dead.sock"
    monkeypatch.setenv(transport.BRIDGE_SOCKET_ENV, str(dead_path))
    failure = transport._discover(run_dir)
    assert isinstance(failure, transport._DiscoveryFailure)
    assert failure.error_code == "BRIDGE_UNAVAILABLE"
    # The message must name the path the caller actually asked for,
    # not just blame run_dir (which FLAME_BRIDGE_SOCKET mode never even
    # looks at).
    assert str(dead_path) in failure.message
    assert transport.BRIDGE_SOCKET_ENV in failure.message


def test_discover_abi_mismatch_is_unavailable_and_notes_abi(
    real_bridge: impl.Bridge, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(impl, "BRIDGE_ABI", 999)
    failure = transport._discover(run_dir)
    assert isinstance(failure, transport._DiscoveryFailure)
    assert failure.error_code == "BRIDGE_UNAVAILABLE"
    assert "ABI" in failure.message


def test_exec_success_returns_result_and_cleans_up_inflight(
    real_bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path
) -> None:
    result_holder: dict[str, transport.BridgeResult] = {}

    def call() -> None:
        result_holder["r"] = transport.exec(
            "_result = 1 + 1", thread="main", timeout_s=5, run_dir=run_dir
        )

    t = threading.Thread(target=call)
    t.start()
    loop.run_until(lambda: "r" in result_holder, timeout=5)
    t.join(5)

    result = result_holder["r"]
    assert result.ok is True
    assert result.result == 2
    assert result.error_code is None
    assert result.inflight_path is None
    assert _inflight_files(run_dir) == []


def test_exec_flame_api_error(real_bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path) -> None:
    result_holder: dict[str, transport.BridgeResult] = {}

    def call() -> None:
        result_holder["r"] = transport.exec(
            "raise ValueError('boom')", thread="main", timeout_s=5, run_dir=run_dir
        )

    t = threading.Thread(target=call)
    t.start()
    loop.run_until(lambda: "r" in result_holder, timeout=5)
    t.join(5)

    result = result_holder["r"]
    assert result.ok is False
    assert result.error_code == "FLAME_API_ERROR"
    assert result.error is not None and "boom" in result.error
    assert result.traceback is not None and "ValueError" in result.traceback
    assert _inflight_files(run_dir) == []


@pytest.mark.usefixtures("bridge_answers_within_production_margin")
def test_exec_timeout_queued_phase(real_bridge: impl.Bridge, run_dir: Path) -> None:
    # The loop is never pumped, so the callback stays QUEUED until the
    # bridge's own timeoutS elapses and self-cancels it — this is a
    # blocking call on the calling thread, no background thread needed.
    result = transport.exec("_result = 1", thread="main", timeout_s=0.2, run_dir=run_dir)
    assert result.ok is False
    assert result.error_code == "TIMEOUT"
    assert result.timeout_phase == "queued"
    assert result.inflight_path is None
    assert _inflight_files(run_dir) == []


@pytest.mark.usefixtures("bridge_answers_within_production_margin")
def test_exec_main_async_pending_then_poll(
    real_bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path
) -> None:
    result_holder: dict[str, transport.BridgeResult] = {}

    def call() -> None:
        result_holder["r"] = transport.exec(
            "import time\ntime.sleep(1.0)\n_result = 'done'",
            thread="main-async",
            timeout_s=0.2,
            run_dir=run_dir,
        )

    t = threading.Thread(target=call)
    t.start()
    loop.run_until(lambda: "r" in result_holder, timeout=5)
    t.join(5)

    pending = result_holder["r"]
    assert pending.ok is True
    assert pending.pending_handle is not None

    poll_result = transport.poll(pending.pending_handle, run_dir=run_dir)
    assert poll_result.ok is True
    assert poll_result.result == "done"


def test_app_log_note_reports_reason_when_log_file_missing(
    real_bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path, scratch_dir: Path
) -> None:
    # `_fake_logs` points FLAME_MCP_APP_LOG at a file that is
    # never created in this test. An empty `app_log` tuple must not look
    # identical to "no warnings happened" -- `app_log_note` has to say why.
    assert not (scratch_dir / "app.log").exists()
    result_holder: dict[str, transport.BridgeResult] = {}

    def call() -> None:
        result_holder["r"] = transport.exec(
            "_result = 1", thread="main", timeout_s=5, run_dir=run_dir
        )

    t = threading.Thread(target=call)
    t.start()
    loop.run_until(lambda: "r" in result_holder, timeout=5)
    t.join(5)

    result = result_holder["r"]
    assert result.ok is True
    assert result.app_log == ()
    assert result.app_log_note is not None
    assert "cannot stat" in result.app_log_note


def _hello_ok_payload(
    req_id: Any, pid: int = 4242, project: str = "proj", features: Any = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": req_id,
        "ok": True,
        "bridgeAbi": impl.BRIDGE_ABI,
        "flameVersion": "2025.2.4",
        "pid": pid,
        "python": "3.11.5",
        "startedAt": 0.0,
        "projectName": project,
        "socket": None,
    }
    if features is not None:
        payload["features"] = features
    return payload


def _send_json(conn: socket.socket, body: dict[str, Any]) -> None:
    conn.sendall((json.dumps(body) + "\n").encode("utf-8"))


class FakeRawBridge:
    """A hand-rolled bridge for simulating wire-level failure modes the real
    `impl.Bridge` never produces by construction."""

    def __init__(
        self, sock_path: Path, handler: Callable[[socket.socket, dict[str, Any]], None]
    ) -> None:
        self.sock_path = sock_path
        self._handler = handler
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            self._handler(conn, req)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass


def _close_and_write_crash_handler(
    shell_log_path: Path,
) -> Callable[[socket.socket, dict[str, Any]], None]:
    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        if req.get("kind") == "hello":
            _send_json(conn, _hello_ok_payload(req.get("id")))
            return
        # Simulate Flame writing its own "it crashed" evidence to shell.log
        # while our request was in flight, then the connection dying — the
        # crash window must see this because it happened *after* our mark().
        with open(shell_log_path, "a") as f:
            f.write("abnormal termination happened here\n")

    return handler


def _rotate_app_log_then_succeed_handler(
    app_log_path: Path,
) -> Callable[[socket.socket, dict[str, Any]], None]:
    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        if req.get("kind") == "hello":
            _send_json(conn, _hello_ok_payload(req.get("id")))
            return
        # Simulate Flame's own log rotating out from under us mid-call, then
        # answer normally -- app_log_note must flag this even
        # though the call itself otherwise succeeds.
        if app_log_path.exists():
            app_log_path.rename(app_log_path.with_name(app_log_path.name + ".1"))
        app_log_path.write_text("[notice] new file after rotation\n")
        _send_json(conn, {"id": req.get("id"), "ok": True, "result": 1, "queuedMs": 0, "runMs": 0})

    return handler


def _hello_once_then_hang_handler(
    hang_seconds: float,
) -> Callable[[socket.socket, dict[str, Any]], None]:
    state = {"used": False}
    lock = threading.Lock()

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        allow = False
        if req.get("kind") == "hello":
            with lock:
                if not state["used"]:
                    state["used"] = True
                    allow = True
        if allow:
            _send_json(conn, _hello_ok_payload(req.get("id")))
            return
        time.sleep(hang_seconds)

    return handler


def _answer_hello_hang_exec_handler(
    hang_seconds: float,
) -> Callable[[socket.socket, dict[str, Any]], None]:
    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        if req.get("kind") == "hello":
            _send_json(conn, _hello_ok_payload(req.get("id")))
            return
        time.sleep(hang_seconds)

    return handler


def _protocol_error_handler(conn: socket.socket, req: dict[str, Any]) -> None:
    if req.get("kind") == "hello":
        _send_json(conn, _hello_ok_payload(req.get("id")))
        return
    _send_json(conn, {"id": req.get("id"), "ok": False, "protocolError": "simulated"})


def _unknown_error_code_handler(conn: socket.socket, req: dict[str, Any]) -> None:
    if req.get("kind") == "hello":
        _send_json(conn, _hello_ok_payload(req.get("id")))
        return
    _send_json(
        conn, {"id": req.get("id"), "ok": False, "errorCode": "NOT_A_REAL_CODE", "error": "x"}
    )


def test_bridge_lost_when_connection_closes_after_reading_request(
    run_dir: Path, scratch_dir: Path
) -> None:
    shell_log_path = scratch_dir / "shell.log"
    shell_log_path.write_text("")  # establish the file before mark() sees it
    bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock", _close_and_write_crash_handler(shell_log_path)
    )
    try:
        result = transport.exec("_result = 1", thread="main", timeout_s=1.0, run_dir=run_dir)
    finally:
        bridge.close()

    assert result.ok is False
    assert result.error_code == "BRIDGE_LOST"
    assert result.crash is not None
    assert result.inflight_path is not None
    assert result.inflight_path.exists()
    payload = json.loads(result.inflight_path.read_text())
    assert payload["code"] == "_result = 1"
    assert payload["thread"] == "main"
    assert any("abnormal termination" in line for line in result.crash.shell_crash_lines)


def test_bridge_lost_when_client_times_out_and_hello_also_fails(run_dir: Path) -> None:
    bridge = FakeRawBridge(run_dir / "flame-mcp-fake.sock", _hello_once_then_hang_handler(2.0))
    try:
        result = transport.exec("_result = 1", thread="main", timeout_s=0.05, run_dir=run_dir)
    finally:
        bridge.close()

    assert result.ok is False
    assert result.error_code == "BRIDGE_LOST"
    assert result.inflight_path is not None
    assert result.inflight_path.exists()


def test_bridge_lost_waits_crash_log_grace_period_before_reading(
    run_dir: Path, scratch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # shell.log's "abnormal termination" line and app.log's tail can
    # land a moment after the socket actually drops, so BRIDGE_LOST must
    # wait CRASH_LOG_GRACE_S before reading either -- measure that the
    # grace period genuinely elapses (not just that it's referenced).
    monkeypatch.setattr(transport, "CRASH_LOG_GRACE_S", 0.2)
    shell_log_path = scratch_dir / "shell.log"
    shell_log_path.write_text("")
    bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock", _close_and_write_crash_handler(shell_log_path)
    )
    try:
        started = time.monotonic()
        result = transport.exec("_result = 1", thread="main", timeout_s=1.0, run_dir=run_dir)
        elapsed_s = time.monotonic() - started
    finally:
        bridge.close()

    assert result.error_code == "BRIDGE_LOST"
    assert elapsed_s >= 0.2


def test_app_log_note_reports_rotation(run_dir: Path, scratch_dir: Path) -> None:
    app_log_path = scratch_dir / "app.log"
    app_log_path.write_text("[error] before rotation\n")
    bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock", _rotate_app_log_then_succeed_handler(app_log_path)
    )
    try:
        result = transport.exec("_result = 1", thread="main", timeout_s=1.0, run_dir=run_dir)
    finally:
        bridge.close()

    assert result.ok is True
    assert result.app_log_note == (
        "app.log rotated during the call; read from the start of the new file"
    )


def test_timeout_when_hello_answers_but_exec_never_replies(run_dir: Path) -> None:
    # The bridge is confirmed alive, but we still don't know whether
    # it will eventually run this exec (it may be sitting QUEUED/RUNNING on
    # its side) -- the in-flight record must survive a TIMEOUT, not just a
    # BRIDGE_LOST, and its path must be surfaced on the result.
    bridge = FakeRawBridge(run_dir / "flame-mcp-fake.sock", _answer_hello_hang_exec_handler(2.0))
    try:
        result = transport.exec("_result = 1", thread="main", timeout_s=0.05, run_dir=run_dir)
    finally:
        bridge.close()

    assert result.ok is False
    assert result.error_code == "TIMEOUT"
    assert result.error == "bridge alive but no reply"
    assert result.inflight_path is not None
    assert result.inflight_path.exists()
    assert _inflight_files(run_dir) == [result.inflight_path]


def test_protocol_error_raised_on_protocol_error_response(run_dir: Path) -> None:
    bridge = FakeRawBridge(run_dir / "flame-mcp-fake.sock", _protocol_error_handler)
    try:
        with pytest.raises(transport.BridgeProtocolError):
            transport.exec("_result = 1", thread="main", timeout_s=1.0, run_dir=run_dir)
    finally:
        bridge.close()
    assert _inflight_files(run_dir) == []


def test_protocol_error_raised_on_unknown_error_code(run_dir: Path) -> None:
    bridge = FakeRawBridge(run_dir / "flame-mcp-fake.sock", _unknown_error_code_handler)
    try:
        with pytest.raises(transport.BridgeProtocolError):
            transport.exec("_result = 1", thread="main", timeout_s=1.0, run_dir=run_dir)
    finally:
        bridge.close()
    assert _inflight_files(run_dir) == []


def _write_fake_inflight(
    run_dir: Path, request_id: str, *, label: str | None, sent_at: str, age_s: float = 0.0
) -> Path:
    d = run_dir / "inflight"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{request_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": request_id,
                "label": label,
                "thread": "main",
                "timeoutS": 10.0,
                "sentAt": sent_at,
                "socket": str(run_dir / "flame-mcp-fake.sock"),
                "bridgePid": 12345,
                "codeSha256": "deadbeef",
                "code": "_result = 1",
            }
        ),
        encoding="utf-8",
    )
    if age_s:
        old = time.time() - age_s
        os.utime(path, (old, old))
    return path


def test_cleanup_stale_inflight_removes_only_records_older_than_max_age(run_dir: Path) -> None:
    old_path = _write_fake_inflight(
        run_dir, "old", label="l1", sent_at="2020-01-01T00:00:00+00:00", age_s=8 * 24 * 60 * 60
    )
    fresh_path = _write_fake_inflight(
        run_dir, "fresh", label="l2", sent_at="2020-01-02T00:00:00+00:00"
    )

    removed = transport.cleanup_stale_inflight(run_dir)

    assert removed == 1
    assert not old_path.exists()
    assert fresh_path.exists()


def test_cleanup_stale_inflight_on_missing_dir_is_a_noop(run_dir: Path) -> None:
    assert not (run_dir / "inflight").exists()
    assert transport.cleanup_stale_inflight(run_dir) == 0


def test_read_inflight_summary_empty_when_no_records(run_dir: Path) -> None:
    summary = transport.read_inflight_summary(run_dir)
    assert summary.count == 0
    assert summary.newest is None


def test_read_inflight_summary_reports_count_and_newest_by_sent_at(run_dir: Path) -> None:
    _write_fake_inflight(run_dir, "r1", label="first", sent_at="2026-01-01T00:00:00+00:00")
    _write_fake_inflight(run_dir, "r2", label="second", sent_at="2026-01-02T00:00:00+00:00")
    _write_fake_inflight(run_dir, "r3", label="third", sent_at="2026-01-01T12:00:00+00:00")

    summary = transport.read_inflight_summary(run_dir)

    assert summary.count == 3
    assert summary.newest == {"id": "r2", "label": "second", "sentAt": "2026-01-02T00:00:00+00:00"}


def test_read_inflight_summary_counts_corrupt_record_but_skips_it_for_newest(
    run_dir: Path,
) -> None:
    d = run_dir / "inflight"
    d.mkdir(parents=True, exist_ok=True)
    (d / "corrupt.json").write_text("not json", encoding="utf-8")
    _write_fake_inflight(run_dir, "ok", label="ok", sent_at="2026-01-01T00:00:00+00:00")

    summary = transport.read_inflight_summary(run_dir)

    assert summary.count == 2
    assert summary.newest == {"id": "ok", "label": "ok", "sentAt": "2026-01-01T00:00:00+00:00"}


CLIENT_HANDLE = "0123456789abcdef0123456789abcdef"


def _recording_handler(
    seen: list[dict[str, Any]], features: Any, *, answer: bool = True
) -> Callable[[socket.socket, dict[str, Any]], None]:
    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        if req.get("kind") == "hello":
            _send_json(conn, _hello_ok_payload(req.get("id"), features=features))
            return
        seen.append(req)
        if answer:
            _send_json(
                conn, {"id": req.get("id"), "ok": True, "result": 1, "queuedMs": 0, "runMs": 0}
            )

    return handler


def test_discover_reads_the_features_a_bridge_announces(
    real_bridge: impl.Bridge, run_dir: Path
) -> None:
    candidate = transport._discover(run_dir)
    assert isinstance(candidate, transport.BridgeCandidate)
    assert candidate.features == ("client_handle",)
    assert transport.probe_features(run_dir) == ("client_handle",)


@pytest.mark.parametrize("features", [None, "client_handle", 7, {"a": 1}])
def test_a_bridge_without_a_usable_features_list_has_none(run_dir: Path, features: Any) -> None:
    bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock", _recording_handler([], features=features)
    )
    try:
        candidate = transport._discover(run_dir)
        assert isinstance(candidate, transport.BridgeCandidate)
        assert candidate.features == ()
        assert transport.probe_features(run_dir) == ()
    finally:
        bridge.close()


def test_only_string_features_are_kept(run_dir: Path) -> None:
    bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock", _recording_handler([], features=["client_handle", 3, None])
    )
    try:
        assert transport.probe_features(run_dir) == ("client_handle",)
    finally:
        bridge.close()


def test_probe_features_with_no_bridge_is_empty(run_dir: Path) -> None:
    assert transport.probe_features(run_dir) == ()


def test_exec_sends_the_handle_to_a_bridge_that_announces_client_handle(run_dir: Path) -> None:
    seen: list[dict[str, Any]] = []
    bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock", _recording_handler(seen, features=["client_handle"])
    )
    try:
        result = transport.exec(
            "_result = 1", thread="main-async", timeout_s=1.0, run_dir=run_dir, handle=CLIENT_HANDLE
        )
    finally:
        bridge.close()
    assert result.ok is True and result.bridge_features == ("client_handle",)
    (req,) = seen
    assert req["handle"] == CLIENT_HANDLE and req["thread"] == "main-async"


def test_exec_does_not_send_the_handle_to_a_bridge_that_does_not_know_it(run_dir: Path) -> None:
    seen: list[dict[str, Any]] = []
    bridge = FakeRawBridge(run_dir / "flame-mcp-fake.sock", _recording_handler(seen, features=None))
    try:
        result = transport.exec(
            "_result = 1", thread="main-async", timeout_s=1.0, run_dir=run_dir, handle=CLIENT_HANDLE
        )
    finally:
        bridge.close()
    assert result.ok is True and result.bridge_features == ()
    (req,) = seen
    assert "handle" not in req


def test_exec_without_a_handle_sends_none(run_dir: Path) -> None:
    seen: list[dict[str, Any]] = []
    bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock", _recording_handler(seen, features=["client_handle"])
    )
    try:
        transport.exec("_result = 1", thread="main-async", timeout_s=1.0, run_dir=run_dir)
    finally:
        bridge.close()
    assert "handle" not in seen[0]


def test_a_lost_connection_still_says_which_features_the_bridge_had_and_keeps_the_handle(
    run_dir: Path,
) -> None:
    seen: list[dict[str, Any]] = []
    bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock",
        _recording_handler(seen, features=["client_handle"], answer=False),
    )
    try:
        result = transport.exec(
            "_result = 1", thread="main-async", timeout_s=1.0, run_dir=run_dir, handle=CLIENT_HANDLE
        )
    finally:
        bridge.close()
    assert result.error_code == "BRIDGE_LOST" and result.bridge_features == ("client_handle",)
    assert result.inflight_path is not None
    assert json.loads(result.inflight_path.read_text())["handle"] == CLIENT_HANDLE


def test_a_client_handle_run_can_be_fetched_with_poll_after_an_in_band_answer(
    real_bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path
) -> None:
    box: dict[str, transport.BridgeResult] = {}

    def call() -> None:
        box["r"] = transport.exec(
            "_result = 5", thread="main-async", timeout_s=5, run_dir=run_dir, handle=CLIENT_HANDLE
        )

    t = threading.Thread(target=call)
    t.start()
    loop.run_until(lambda: "r" in box, timeout=5)
    t.join(5)
    assert box["r"].ok is True and box["r"].result == 5
    assert box["r"].bridge_features == ("client_handle",)
    polled = transport.poll(CLIENT_HANDLE, run_dir=run_dir)
    assert polled.ok is True and polled.result == 5
    with pytest.raises(transport.BridgeProtocolError, match="unknown handle"):
        transport.poll(CLIENT_HANDLE, run_dir=run_dir)


@pytest.fixture
def bridge_2026(run_dir: Path, scratch_dir: Path, loop: FakeMainLoop) -> Iterator[impl.Bridge]:
    bridge = impl.Bridge(
        schedule=loop.schedule,
        flame_module=FakeFlame(),
        version="2026.2.3",
        project_name="proj",
        run_dir=run_dir,
        log_path=scratch_dir / "bridge_2026.log",
        make_timer=lambda: FakeTimer(loop),
    )
    bridge.start()
    yield bridge
    try:
        bridge.stop()
    except Exception:
        pass


@pytest.fixture
def window_paths(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The log paths transport opens windows on, with no env override and a fixed host name."""
    monkeypatch.delenv(applog.APP_LOG_ENV, raising=False)
    monkeypatch.delenv(applog.SHELL_LOG_ENV, raising=False)
    monkeypatch.setattr(applog, "_hostname_short", lambda: "example-host")
    paths: list[Path] = []

    class Spy(applog.LogWindow):
        def __init__(self, path: Path) -> None:
            paths.append(path)
            super().__init__(path)

    monkeypatch.setattr(transport, "LogWindow", Spy)
    return paths


def test_exec_reads_the_logs_of_the_flame_version_it_is_connected_to(
    bridge_2026: impl.Bridge, run_dir: Path, window_paths: list[Path]
) -> None:
    transport.exec("_result = 1", thread="main", timeout_s=0.2, run_dir=run_dir)

    assert window_paths == [
        Path("/opt/Autodesk/log/flame202623_example-host_app.log"),
        Path("/opt/Autodesk/log/flame202623_example-host_shell.log"),
    ]


def test_poll_reads_the_logs_of_the_flame_version_it_is_connected_to(
    bridge_2026: impl.Bridge, run_dir: Path, window_paths: list[Path]
) -> None:
    with pytest.raises(transport.BridgeProtocolError, match="unknown handle"):
        transport.poll("0" * 32, run_dir=run_dir)

    assert window_paths == [
        Path("/opt/Autodesk/log/flame202623_example-host_app.log"),
        Path("/opt/Autodesk/log/flame202623_example-host_shell.log"),
    ]


def test_exec_reads_the_2025_logs_for_a_2025_bridge(
    real_bridge: impl.Bridge, run_dir: Path, window_paths: list[Path]
) -> None:
    transport.exec("_result = 1", thread="main", timeout_s=0.2, run_dir=run_dir)

    assert window_paths == [
        Path("/opt/Autodesk/log/flame202524_example-host_app.log"),
        Path("/opt/Autodesk/log/flame202524_example-host_shell.log"),
    ]


def test_the_log_env_overrides_still_win_over_the_connected_version(
    bridge_2026: impl.Bridge,
    run_dir: Path,
    scratch_dir: Path,
    window_paths: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(applog.APP_LOG_ENV, str(scratch_dir / "my_app.log"))
    monkeypatch.setenv(applog.SHELL_LOG_ENV, str(scratch_dir / "my_shell.log"))

    transport.exec("_result = 1", thread="main", timeout_s=0.2, run_dir=run_dir)

    assert window_paths == [scratch_dir / "my_app.log", scratch_dir / "my_shell.log"]
