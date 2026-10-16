"""The in-Flame half of the flame-mcp socket transport.

Stdlib only. Deployed as a single file copy into
``~/Library/Application Support/flame-mcp/bridge/impl.py`` and loaded by the
stub via ``compile()``/``exec()``. It never imports the ``flame_mcp`` package.

Threading contract: handler threads (accept loop, per-connection handlers) never
touch the injected ``flame`` module, and never call the injected ``schedule``
callable (``flame.schedule_idle_event``) either. Calling it from a handler thread
deadlocked against Flame's main thread (the handler holds the GIL inside
``schedule_idle_event`` and waits on ``IdleEventQueue``'s internal mutex, while the
main thread holds that mutex inside ``IdleEventQueue::run`` and waits on the GIL to
destroy a finished callback's Python objects; a lock-order inversion seen in stack
samples taken during the hang, and in a stress run). Only
methods whose name starts with ``_main_`` may read ``self._flame`` or
``self._schedule``; they run on Flame's main thread -- either the ``QTimer``
tick that relays queued requests onto ``schedule`` (``_main_relay_tick``), or
the callback ``schedule`` itself later invokes (``_main_run_exec``) -- and
each begins by confirming it is actually running there. Handler threads only
ever put requests onto ``self._relay_queue``; they never touch ``schedule``.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import math
import os
import platform
import queue
import re
import socket
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol, cast

BRIDGE_ABI: Final[int] = 1
# Optional capabilities announced in `hello` (the ABI does not change for them: a client that
# does not know a feature simply never uses it, and an older bridge that does not know a request
# field ignores it).
FEATURE_CLIENT_HANDLE: Final[str] = "client_handle"
BRIDGE_FEATURES: Final[tuple[str, ...]] = (FEATURE_CLIENT_HANDLE,)
_CLIENT_HANDLE_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")

_LOGGER_NAME = "flame_mcp_bridge"

MAX_LINE_BYTES: Final[int] = 8 * 1024 * 1024
DEFAULT_TIMEOUT_S: Final[float] = 10.0
MAX_TIMEOUT_S: Final[float] = 600.0
MIN_TIMEOUT_S: Final[float] = 0.001
MAX_SOCK_PATH_BYTES: Final[int] = 103
PROBE_TIMEOUT_S: Final[float] = 0.5
ACCEPT_POLL_S: Final[float] = 1.0
LISTEN_BACKLOG: Final[int] = 64
RUN_DIR_MODE: Final[int] = 0o700
SOCK_MODE: Final[int] = 0o600
RECV_CHUNK: Final[int] = 65536
RESULT_FILE_MAX_AGE_S: Final[float] = 24 * 60 * 60
MAX_CONNECTIONS: Final[int] = 32
CONNECTION_IDLE_TIMEOUT_S: Final[float] = 300.0
LOG_FILE_MODE: Final[int] = 0o600
LOG_DIR_MODE: Final[int] = 0o700
LOG_BACKUP_COUNT: Final[int] = 3
# Main-thread relay tick interval. Handler threads enqueue onto `_relay_queue`;
# this QTimer (created in `Bridge.start()`, main thread only) is the only thing
# that calls `self._schedule` (schedule_idle_event).
RELAY_INTERVAL_MS: Final[int] = 10
# Total budget for stop() to wait for handler threads to finish sending their own
# response and close their own connection, before stop() force-closes whatever is
# left. stop() runs on the main thread (app_exited), so this must stay short.
STOP_HANDLER_JOIN_TOTAL_S: Final[float] = 1.0


def _default_run_dir() -> Path:
    override = os.environ.get("FLAME_MCP_RUN_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "flame-mcp" / "run"


def _default_log_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "flame-mcp" / "logs" / "bridge.log"


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating log whose files are 0600 whatever the process umask is (Flame runs with umask 0)."""

    def _open(self) -> Any:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, LOG_FILE_MODE)
        try:
            os.fchmod(fd, LOG_FILE_MODE)
        except OSError:
            pass
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)

    def doRollover(self) -> None:
        super().doRollover()
        _tighten_log_files(Path(self.baseFilename), self.backupCount)


def _tighten_log_files(log_path: Path, backup_count: int) -> None:
    names = [log_path] + [
        log_path.with_name(f"{log_path.name}.{i}") for i in range(1, backup_count + 1)
    ]
    for path in names:
        try:
            os.chmod(path, LOG_FILE_MODE)
        except OSError:
            pass  # missing, or not ours: logging itself must still come up


def _make_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=LOG_DIR_MODE)
    _tighten_log_files(log_path, LOG_BACKUP_COUNT)
    file_handler = _PrivateRotatingFileHandler(
        str(log_path), maxBytes=2_000_000, backupCount=LOG_BACKUP_COUNT
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


def _redact_home(text: str) -> str:
    """Replaces the user's home directory by `~` so a returned traceback does not carry it."""
    home = str(Path.home())
    if len(home) <= 1:
        return text
    return re.sub(re.escape(home) + r"(?![\w.-])", "~", text)


def _probe_alive(sock_path: Path) -> bool:
    if not sock_path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(PROBE_TIMEOUT_S)
    try:
        probe.connect(str(sock_path))
    except OSError:
        return False
    else:
        return True
    finally:
        try:
            probe.close()
        except OSError:
            pass


class _SignalProtocol(Protocol):
    """Structural stand-in for a Qt Signal's bound-connect interface.

    Mirrors ``QTimer().timeout.connect(...)``. A ``Protocol`` so impl.py never has to
    import PySide6 at module level: it must stay loadable by plain
    ``compile()``/``exec()`` outside Flame (e.g. under pytest), where PySide6 is not
    installed.
    """

    def connect(self, slot: Callable[[], None]) -> None: ...


class _TimerProtocol(Protocol):
    """Structural stand-in for ``PySide6.QtCore.QTimer``.

    Covers only what the relay needs: a millisecond interval, start/stop, and a
    ``timeout`` signal to connect the tick callback to. ``timeout`` is a read-only
    property (like a real Qt Signal descriptor; nothing ever assigns to it), not a
    plain settable attribute, so a fake's own read-only ``@property`` structurally
    matches it.
    """

    @property
    def timeout(self) -> _SignalProtocol: ...
    def setInterval(self, msec: int) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


def _default_make_timer() -> _TimerProtocol:
    """Returns a real QTimer, imported lazily.

    That keeps this module importable, and testable via an injected ``make_timer``,
    without PySide6 installed. Only called from `Bridge.start()`, which must run on
    Flame's main thread (``app_initialized``), the thread that must own any QTimer per
    Qt's threading rules.
    """
    from PySide6.QtCore import QTimer

    return cast(_TimerProtocol, QTimer())


class _RequestState:
    """QUEUED -> RUNNING -> DONE, or QUEUED -> CANCELLED (a cancelled request never runs)."""

    __slots__ = ("event", "id", "lock", "needs_file", "result", "status")

    def __init__(self, call_id: str) -> None:
        self.id = call_id
        self.lock = threading.Lock()
        self.status = "QUEUED"
        self.result: dict[str, Any] | None = None
        self.needs_file = False
        self.event = threading.Event()


class Bridge:
    def __init__(
        self,
        schedule: Callable[[Callable[[], None]], None] | None,
        flame_module: Any,
        version: str,
        project_name: str | None,
        run_dir: Path | None = None,
        results_dir: Path | None = None,
        log_path: Path | None = None,
        make_timer: Callable[[], _TimerProtocol] | None = None,
    ) -> None:
        self._schedule = schedule
        self._flame = flame_module
        self._version = version
        self._project_name = project_name
        self._pid = os.getpid()
        self._python_version = platform.python_version()
        self._started_at = time.time()
        self._run_dir = run_dir if run_dir is not None else _default_run_dir()
        self._results_dir = results_dir if results_dir is not None else self._run_dir / "results"
        self._sock_path: Path | None = None
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pending: dict[str, _RequestState] = {}
        self._pending_lock = threading.Lock()
        self._bound_dev_ino: tuple[int, int] | None = None
        self._started = False
        self._logger = _make_logger(log_path if log_path is not None else _default_log_path())
        self._connections: set[socket.socket] = set()
        # Which handler thread owns each live connection, so stop() can join them
        # (bounded) instead of closing out from under a handler that is still
        # writing its response. Guarded by the same lock as `_connections`; both
        # are only mutated together.
        self._handler_threads: dict[socket.socket, threading.Thread] = {}
        self._connections_lock = threading.Lock()
        # Held briefly around "is the bridge stopping? then submit new work" so
        # stop() can never race a handler thread that is right in the middle of
        # enqueueing new work onto the relay queue.
        self._submit_lock = threading.Lock()
        # Handler threads never call `self._schedule` directly (that deadlocked
        # against Flame's main thread). They only put onto this queue; only
        # `_main_relay_tick` (QTimer, main thread only) pops it and calls
        # `self._schedule`.
        self._relay_queue: queue.SimpleQueue[tuple[_RequestState, Any, float]] = queue.SimpleQueue()
        self._make_timer = make_timer if make_timer is not None else _default_make_timer
        self._timer: _TimerProtocol | None = None

    # start() and stop() are called from the main thread (app_initialized / app_exited).

    def start(self) -> None:
        if self._started:
            return
        os.makedirs(self._run_dir, exist_ok=True)
        os.chmod(self._run_dir, RUN_DIR_MODE)
        sock_path = self._run_dir / f"flame-mcp-{self._version}.sock"
        encoded = str(sock_path).encode("utf-8")
        if len(encoded) > MAX_SOCK_PATH_BYTES:
            reason = (
                f"socket path too long ({len(encoded)} bytes > {MAX_SOCK_PATH_BYTES}): {sock_path}"
            )
            self._logger.error(reason)
            raise RuntimeError(reason)
        if _probe_alive(sock_path):
            reason = f"another live bridge is already bound at {sock_path}"
            self._logger.error(reason)
            raise RuntimeError(reason)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(sock_path))
            os.chmod(sock_path, SOCK_MODE)
            listener.listen(LISTEN_BACKLOG)
        except OSError:
            listener.close()
            raise
        stat_result = os.stat(sock_path)
        self._bound_dev_ino = (stat_result.st_dev, stat_result.st_ino)
        self._sock_path = sock_path
        self._listener = listener
        self._results_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._results_dir, RUN_DIR_MODE)
        self._cleanup_stale_results()
        self._relay_queue = queue.SimpleQueue()
        self._timer = self._start_relay_timer()
        self._stop_event.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="flame-mcp-bridge-accept", daemon=True
        )
        self._accept_thread.start()
        self._started = True
        self._logger.info("bridge started at %s (pid=%s)", sock_path, self._pid)

    def _start_relay_timer(self) -> _TimerProtocol | None:
        """Creates and starts the relay QTimer.

        A failure here is degraded, not fatal: the socket/accept loop still come up,
        but every `exec` fails fast with MAIN_THREAD_UNAVAILABLE (see `_handle_exec`'s
        `self._timer is None` check) until a reload makes a new attempt.
        """
        try:
            timer = self._make_timer()
            timer.setInterval(RELAY_INTERVAL_MS)
            timer.timeout.connect(self._main_relay_tick)
            timer.start()
        except BaseException:
            self._logger.exception("failed to create/start the relay timer")
            return None
        return timer

    def _cleanup_stale_results(self) -> None:
        # A poller that never came back (crashed client, abandoned main-async
        # handle) would otherwise leave its result file forever.
        cutoff = time.time() - RESULT_FILE_MAX_AGE_S
        for entry in self._results_dir.glob("*.json"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                pass

    def stop(self) -> None:
        if not self._started:
            return
        # Flip the stop flag (and disable the relay timer) while holding the same
        # lock `_handle_exec` takes right before enqueueing onto the relay queue,
        # so no handler can be caught mid-way through "checked not-stopped/
        # timer-running, about to enqueue" while this runs: it either sees the
        # flag already set / timer already gone (and refuses), or it finishes
        # enqueueing before this is reached.
        with self._submit_lock:
            self._stop_event.set()
            timer = self._timer
            self._timer = None
        if timer is not None:
            try:
                timer.stop()
            except BaseException:
                self._logger.exception("failed to stop the relay timer")
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        accept_thread = self._accept_thread
        if accept_thread is not None:
            accept_thread.join(timeout=5.0)
        # Covers requests still sitting in `_relay_queue` too: they are
        # QUEUED in `_pending` from the moment `_handle_exec` enqueues them
        # until `_main_run_exec` marks RUNNING, regardless of whether they
        # have physically reached `self._schedule` yet. `_main_relay_tick`
        # separately checks this status before calling `self._schedule`, so
        # a cancelled-while-still-queued item is never scheduled at all.
        with self._pending_lock:
            for state in self._pending.values():
                with state.lock:
                    if state.status == "QUEUED":
                        state.status = "CANCELLED"
                        state.event.set()
        # Shut down only the *read* half of every live connection here. A handler
        # thread woken by the cancellation loop just above is about to `sendall()` a
        # TIMEOUT/queued response; shutdown(SHUT_RDWR)+close() right after cancelling
        # raced that send and the client saw EOF instead of its response (observed
        # under test). SHUT_RD alone unblocks a handler still parked in recv() (no
        # more requests will ever arrive) without touching the write side, so any
        # in-flight response finishes. Each handler closes its own connection once it
        # is done, via `_handle_connection`'s own `finally` (recv() next returns EOF
        # because of the SHUT_RD here).
        with self._connections_lock:
            live_connections = list(self._connections)
            handler_threads = list(self._handler_threads.values())
        for conn in live_connections:
            try:
                conn.shutdown(socket.SHUT_RD)
            except OSError:
                pass
        # stop() runs on the main thread (app_exited): bound the total wait,
        # never join per-thread with the full budget each.
        join_deadline = time.monotonic() + STOP_HANDLER_JOIN_TOTAL_S
        for handler_thread in handler_threads:
            remaining = join_deadline - time.monotonic()
            if remaining <= 0:
                break
            handler_thread.join(timeout=remaining)
        # Anything still open after the budget did not finish on its own (stuck
        # handler, or the client never reads its response); force it closed now
        # rather than leaking the connection past stop().
        with self._connections_lock:
            still_live = list(self._connections)
        for conn in still_live:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._sock_path is not None and self._bound_dev_ino is not None:
            try:
                current = os.stat(self._sock_path)
                if (current.st_dev, current.st_ino) == self._bound_dev_ino:
                    os.unlink(self._sock_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self._started = False
        self._logger.info("bridge stopped (pid=%s)", self._pid)

    def status(self) -> dict[str, Any]:
        return {
            "alive": self._started,
            "socket": str(self._sock_path) if self._sock_path else None,
            "pid": self._pid,
            "flameVersion": self._version,
            "projectName": self._project_name,
            "startedAt": self._started_at,
            "pending": len(self._pending),
        }

    def set_project_name(self, project_name: str | None) -> None:
        """Records a project switch within the same Flame session.

        Called by the stub when app_initialized fires again for an already-running
        bridge. Plain bookkeeping only; never touches flame.
        """
        self._project_name = project_name

    # The accept loop and connection handling run on handler threads and never touch flame.

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        listener.settimeout(ACCEPT_POLL_S)
        while not self._stop_event.is_set():
            try:
                conn, _addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            handler = threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                name="flame-mcp-bridge-conn",
                daemon=True,
            )
            with self._connections_lock:
                over_limit = len(self._connections) >= MAX_CONNECTIONS
                if not over_limit:
                    self._connections.add(conn)
                    self._handler_threads[conn] = handler
            if over_limit:
                self._logger.warning("connection refused: %s already open", MAX_CONNECTIONS)
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            handler.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        buf = b""
        try:
            conn.settimeout(CONNECTION_IDLE_TIMEOUT_S)
            while True:
                newline_at = buf.find(b"\n")
                while newline_at == -1:
                    if len(buf) > MAX_LINE_BYTES:
                        self._send_protocol_error(conn, None, "line exceeds 8MiB limit")
                        return
                    chunk = conn.recv(RECV_CHUNK)
                    if not chunk:
                        return
                    buf += chunk
                    newline_at = buf.find(b"\n")
                line, buf = buf[:newline_at], buf[newline_at + 1 :]
                if len(line) > MAX_LINE_BYTES:
                    self._send_protocol_error(conn, None, "line exceeds 8MiB limit")
                    return
                self._process_line(conn, line)
        except OSError:
            pass
        finally:
            with self._connections_lock:
                self._connections.discard(conn)
                self._handler_threads.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass

    def _process_line(self, conn: socket.socket, line: bytes) -> None:
        try:
            body = self._response_for_line(line)
        except Exception:
            self._logger.exception("unexpected error while handling a request line")
            body = self._protocol_error_body(None, "internal error in the bridge")
        self._send(conn, body)

    def _response_for_line(self, line: bytes) -> dict[str, Any]:
        if self._stop_event.is_set():
            # A connection accepted before stop() began may still be sending lines
            # after it. Refuse rather than process.
            return self._protocol_error_body(None, "bridge stopped")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            return self._protocol_error_body(None, f"invalid utf-8: {exc}")
        try:
            req = json.loads(text)
        except json.JSONDecodeError as exc:
            return self._protocol_error_body(None, f"invalid JSON: {exc}")
        except RecursionError:
            return self._protocol_error_body(None, "invalid JSON: nested too deeply")
        if not isinstance(req, dict):
            return self._protocol_error_body(None, "request must be a JSON object")
        req_id = req.get("id")
        try:
            return self._dispatch(req_id, req)
        except Exception:
            self._logger.exception("unexpected error in a %r handler", req.get("kind"))
            return self._protocol_error_body(req_id, "internal error in the bridge")

    def _dispatch(self, req_id: Any, req: dict[str, Any]) -> dict[str, Any]:
        if req.get("v") != 1:
            return self._protocol_error_body(req_id, "unsupported or missing 'v' (expected 1)")
        if not isinstance(req_id, str):
            return self._protocol_error_body(req_id, "missing or invalid 'id'")
        kind = req.get("kind")
        if kind == "hello":
            return self._handle_hello(req_id)
        if kind == "ping":
            return self._handle_ping(req_id)
        if kind == "exec":
            return self._handle_exec(req_id, req)
        if kind == "poll":
            return self._handle_poll(req_id, req)
        return self._protocol_error_body(req_id, f"unknown kind: {kind!r}")

    def _send(self, conn: socket.socket, body: dict[str, Any]) -> None:
        payload = (json.dumps(body) + "\n").encode("utf-8")
        conn.sendall(payload)

    def _send_protocol_error(self, conn: socket.socket, req_id: Any, message: str) -> None:
        self._send(conn, self._protocol_error_body(req_id, message))

    def _protocol_error_body(self, req_id: Any, message: str) -> dict[str, Any]:
        return {"id": req_id, "ok": False, "protocolError": message}

    # The request handlers run on a handler thread and must not touch self._flame.

    def _handle_hello(self, req_id: str) -> dict[str, Any]:
        return {
            "id": req_id,
            "ok": True,
            "bridgeAbi": BRIDGE_ABI,
            "flameVersion": self._version,
            "pid": self._pid,
            "python": self._python_version,
            "startedAt": self._started_at,
            "projectName": self._project_name,
            "socket": str(self._sock_path) if self._sock_path else None,
            "features": list(BRIDGE_FEATURES),
        }

    def _handle_ping(self, req_id: str) -> dict[str, Any]:
        return {"id": req_id, "ok": True, "pong": True}

    def _handle_exec(self, req_id: str, req: dict[str, Any]) -> dict[str, Any]:
        code = req.get("code")
        thread_mode = req.get("thread")
        if not isinstance(code, str):
            return self._protocol_error_body(req_id, "missing or invalid 'code'")
        if not code.isascii():
            return self._protocol_error_body(
                req_id, "'code' holds a non-ASCII character; Flame takes ASCII only"
            )
        if thread_mode not in ("main", "main-async"):
            return self._protocol_error_body(req_id, "missing or invalid 'thread'")
        timeout_s = DEFAULT_TIMEOUT_S
        if "timeoutS" in req:
            timeout_raw = req["timeoutS"]
            if not isinstance(timeout_raw, (int, float)) or isinstance(timeout_raw, bool):
                return self._protocol_error_body(req_id, "invalid 'timeoutS'")
            timeout_s = float(timeout_raw)
            if not math.isfinite(timeout_s):
                return self._protocol_error_body(req_id, "invalid 'timeoutS'")
            if timeout_s > MAX_TIMEOUT_S:
                timeout_s = MAX_TIMEOUT_S
            elif timeout_s <= 0:
                timeout_s = MIN_TIMEOUT_S
        client_handle: str | None = None
        if "handle" in req:
            raw_handle = req["handle"]
            if not isinstance(raw_handle, str) or _CLIENT_HANDLE_RE.fullmatch(raw_handle) is None:
                return self._protocol_error_body(
                    req_id, "invalid 'handle' (expected 32 lowercase hex characters)"
                )
            if thread_mode != "main-async":
                return self._protocol_error_body(
                    req_id, "'handle' is only accepted with thread='main-async'"
                )
            client_handle = raw_handle
        try:
            compiled = compile(code, "<flame_mcp_exec>", "exec")
        except SyntaxError as exc:
            return {
                "id": req_id,
                "ok": False,
                "errorCode": "FLAME_API_ERROR",
                "error": _redact_home(str(exc)),
                "traceback": _redact_home(traceback.format_exc()),
                "queuedMs": 0,
                "runMs": 0,
            }

        handle = client_handle if client_handle is not None else uuid.uuid4().hex
        state = _RequestState(handle)
        # A handle the caller chose is what lets it ask for the result even when this
        # connection's answer never arrives, so its result is always persisted (needs_file from
        # the start), also when the answer does go out in-band; the caller's poll removes it.
        state.needs_file = client_handle is not None
        with self._pending_lock:
            if client_handle is not None and (
                handle in self._pending or (self._results_dir / f"{handle}.json").exists()
            ):
                return self._protocol_error_body(req_id, f"handle already in use: {handle}")
            self._pending[handle] = state
        queued_start = time.monotonic()

        # This handler thread must never call `self._schedule`
        # (flame.schedule_idle_event) itself: doing so deadlocked against Flame's
        # main thread (GIL held here vs. IdleEventQueue's mutex held there). It only
        # enqueues onto the relay queue; the main-thread QTimer tick
        # (`_main_relay_tick`) is the sole caller of `self._schedule`.
        # Check-stopped/timer-running-then-enqueue must be atomic w.r.t. stop()'s
        # own submit_lock acquisition, or a handler could observe "not stopped" and
        # still enqueue after stop() has already torn everything else down.
        with self._submit_lock:
            if self._stop_event.is_set():
                with self._pending_lock:
                    self._pending.pop(handle, None)
                return self._protocol_error_body(req_id, "bridge stopped")
            if self._timer is None:
                with self._pending_lock:
                    self._pending.pop(handle, None)
                return {
                    "id": req_id,
                    "ok": False,
                    "errorCode": "MAIN_THREAD_UNAVAILABLE",
                    "error": "relay timer is not running",
                    "queuedMs": 0,
                    "runMs": 0,
                }
            self._relay_queue.put((state, compiled, queued_start))

        state.event.wait(timeout_s)
        response, finished = self._resolve_after_wait(state, thread_mode)
        if finished:
            with self._pending_lock:
                self._pending.pop(handle, None)
        response["id"] = req_id
        return response

    def _resolve_after_wait(
        self, state: _RequestState, thread_mode: str
    ) -> tuple[dict[str, Any], bool]:
        """Decides the response for a handler that just woke from ``state.event.wait()``.

        The wake is by completion or by timeout. Every read-and-mutate of
        ``state.status``/``needs_file`` happens under a *single* lock acquisition
        here, so this can never interleave with `_finish_call`'s own single locked
        section: one of the two critical sections always fully precedes the other.
        Splitting this across two separate `with state.lock` blocks would leave a
        window where `_finish_call` could see `needs_file` still False (so it skipped
        the result-file write) after the caller had already decided to return a
        `pending`/`running` handle, orphaning that handle forever (poll would never
        find a file, and the pending entry could never resolve). Returns
        (response_without_id, finished) where `finished` tells the caller whether to
        drop the pending entry.
        """
        with state.lock:
            if state.status == "DONE":
                result = state.result
                assert result is not None
                return dict(result), True
            if state.status == "CANCELLED":
                # Cancelled by something other than the branch below, e.g.
                # Bridge.stop() cancelling queued work. Same shape as a
                # queued-phase timeout: guaranteed not to run.
                return {"ok": False, "errorCode": "TIMEOUT", "timeoutPhase": "queued"}, True
            if thread_mode == "main" and state.status == "QUEUED":
                state.status = "CANCELLED"
                return {"ok": False, "errorCode": "TIMEOUT", "timeoutPhase": "queued"}, True
            # RUNNING (either thread mode), or QUEUED-but-main-async: the callback
            # has not delivered a result yet. Mark, atomically with this same status
            # observation, that its eventual result must be persisted to a file,
            # because nobody will be blocked on state.event after this handler returns.
            state.needs_file = True
            if thread_mode == "main":
                return (
                    {
                        "ok": False,
                        "errorCode": "TIMEOUT",
                        "timeoutPhase": "running",
                        "handle": state.id,
                    },
                    False,
                )
            return {"ok": True, "pending": {"handle": state.id}}, False

    def _handle_poll(self, req_id: str, req: dict[str, Any]) -> dict[str, Any]:
        handle = req.get("handle")
        if not isinstance(handle, str) or _CLIENT_HANDLE_RE.fullmatch(handle) is None:
            return self._protocol_error_body(
                req_id, "missing or invalid 'handle' (expected 32 lowercase hex characters)"
            )
        result_path = self._results_dir / f"{handle}.json"
        if result_path.is_file():
            try:
                with open(result_path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                return self._protocol_error_body(req_id, f"corrupt result file: {exc}")
            try:
                result_path.unlink()
            except OSError:
                pass
            with self._pending_lock:
                self._pending.pop(handle, None)
            data = dict(data)
            data["id"] = req_id
            return data
        with self._pending_lock:
            known = handle in self._pending
        if known:
            return {"id": req_id, "ok": True, "pending": {"handle": handle}}
        return self._protocol_error_body(req_id, f"unknown handle: {handle}")

    def _build_run_callback(
        self, state: _RequestState, compiled: Any, queued_start: float
    ) -> Callable[[], None]:
        """Builds the main-thread callback for one relay-queue item.

        Binds `state`/`compiled`/`queued_start` at call time (this method runs once
        per relay-queue item) rather than by name, so callbacks built in the same
        `_main_relay_tick` loop iteration each keep their own values instead of all
        closing over whatever the loop variables held last (Python's
        late-binding-closure trap: a `def` written directly inside a `for` loop body
        captures the *variable*, not its value at that iteration).
        """

        def _cb() -> None:
            self._main_run_exec(state, compiled, queued_start)

        return _cb

    # The main-thread callbacks below are the only place allowed to touch self._flame
    # or call self._schedule.

    def _main_relay_tick(self) -> None:
        """Handles a QTimer tick (RELAY_INTERVAL_MS); runs on Flame's main thread only.

        Drains only what was in the relay queue when this tick began; anything a
        handler thread enqueues after that is left for the next tick, so this call
        always returns promptly (docstring, 2025.2.4: "must not block and be quick
        since it will be executed in the main application thread"). For each item
        still QUEUED, calls `self._schedule` (flame.schedule_idle_event) from here,
        the main thread, which is the entire point of the relay: no handler thread
        ever calls it. A cancelled item (queued-phase timeout, or stop()) is dropped
        without ever reaching `self._schedule`.
        """
        if threading.current_thread() is not threading.main_thread():
            self._logger.error("relay tick invoked off the main thread; skipping")
            return
        pending_count = self._relay_queue.qsize()
        for _ in range(pending_count):
            try:
                state, compiled, queued_start = self._relay_queue.get_nowait()
            except queue.Empty:
                break
            with state.lock:
                if state.status == "CANCELLED":
                    continue
            schedule = self._schedule
            if schedule is None:
                self._finish_call(
                    state,
                    {
                        "ok": False,
                        "errorCode": "MAIN_THREAD_UNAVAILABLE",
                        "error": "no schedule callable is bound",
                        "queuedMs": int((time.monotonic() - queued_start) * 1000),
                        "runMs": 0,
                    },
                )
                continue
            cb = self._build_run_callback(state, compiled, queued_start)
            try:
                schedule(cb)
            except BaseException as exc:  # must not raise out of a QTimer slot
                self._logger.exception("schedule() raised from the relay tick")
                with state.lock:
                    if state.status == "CANCELLED":
                        continue
                self._finish_call(
                    state,
                    {
                        "ok": False,
                        "errorCode": "MAIN_THREAD_UNAVAILABLE",
                        "error": str(exc),
                        "queuedMs": int((time.monotonic() - queued_start) * 1000),
                        "runMs": 0,
                    },
                )

    def _main_run_exec(self, state: _RequestState, compiled: Any, queued_start: float) -> None:
        if threading.current_thread() is not threading.main_thread():
            response: dict[str, Any] = {
                "ok": False,
                "errorCode": "MAIN_THREAD_UNAVAILABLE",
                "error": "callback invoked off the main thread",
                "queuedMs": int((time.monotonic() - queued_start) * 1000),
                "runMs": 0,
            }
            self._finish_call(state, response)
            return

        with state.lock:
            if state.status == "CANCELLED":
                return
            state.status = "RUNNING"

        queued_ms = int((time.monotonic() - queued_start) * 1000)
        run_start = time.monotonic()
        ns: dict[str, Any] = {"flame": self._flame, "__name__": "__flame_mcp_exec__"}
        try:
            exec(compiled, ns)
            result_value = ns.get("_result")
            try:
                json.dumps(result_value)
            except (TypeError, ValueError) as exc:
                response = {
                    "ok": False,
                    "errorCode": "FLAME_API_ERROR",
                    "error": f"_result is not JSON-serializable: {exc}",
                    "traceback": "",
                    "queuedMs": queued_ms,
                    "runMs": int((time.monotonic() - run_start) * 1000),
                }
            else:
                response = {
                    "ok": True,
                    "result": result_value,
                    "queuedMs": queued_ms,
                    "runMs": int((time.monotonic() - run_start) * 1000),
                }
        # Catch here and return it: Flame swallows exceptions escaping a callback.
        except BaseException:
            response = {
                "ok": False,
                "errorCode": "FLAME_API_ERROR",
                "error": _redact_home(traceback.format_exc().strip().splitlines()[-1]),
                "traceback": _redact_home(traceback.format_exc()),
                "queuedMs": queued_ms,
                "runMs": int((time.monotonic() - run_start) * 1000),
            }
        self._finish_call(state, response)

    def _finish_call(self, state: _RequestState, response: dict[str, Any]) -> None:
        with state.lock:
            state.status = "DONE"
            state.result = response
            needs_file = state.needs_file
        if needs_file:
            try:
                self._write_result_file(state.id, response)
            except OSError:
                # This runs inside a callback Flame invokes directly, so nothing may
                # escape it. A failed write here only means a delayed/`main-async`
                # poller never gets its answer; log it so it is at least visible in
                # bridge.log.
                self._logger.exception("failed to write result file for handle %s", state.id)
        # After the file: a waiting handler that answers in-band and drops the pending entry
        # must not leave a window in which a poll finds neither the entry nor the file.
        state.event.set()

    def _write_result_file(self, handle: str, response: dict[str, Any]) -> None:
        self._results_dir.mkdir(parents=True, exist_ok=True)
        final_path = self._results_dir / f"{handle}.json"
        tmp_path = self._results_dir / f".{handle}.json.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(response, f)
        os.replace(tmp_path, final_path)


def start(
    schedule: Callable[[Callable[[], None]], None] | None,
    flame_module: Any,
    version: str,
    project_name: str | None,
    run_dir: str | None = None,
    results_dir: str | None = None,
    log_path: str | None = None,
    make_timer: Callable[[], _TimerProtocol] | None = None,
) -> Bridge:
    bridge = Bridge(
        schedule=schedule,
        flame_module=flame_module,
        version=version,
        project_name=project_name,
        run_dir=Path(run_dir) if run_dir else None,
        results_dir=Path(results_dir) if results_dir else None,
        log_path=Path(log_path) if log_path else None,
        make_timer=make_timer,
    )
    bridge.start()
    return bridge
