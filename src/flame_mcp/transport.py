"""Server-side transport to the in-Flame bridge.

Discovery, framing, and BRIDGE_LOST/BRIDGE_UNAVAILABLE/BRIDGE_AMBIGUOUS
classification live here. One request = one connection (synchronous,
blocking sockets) -- the MCP layer is expected to call this from a
worker thread (e.g. via `anyio.to_thread`).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, assert_never, get_args

from flame_mcp.applog import (
    LogMark,
    LogReadResult,
    LogWindow,
    app_log_path,
    crash_lines_since,
    shell_log_path,
)
from flame_mcp.types import ErrorCode, Thread
from flame_mcp_bridge.impl import BRIDGE_ABI, FEATURE_CLIENT_HANDLE

MAIN_TIMEOUT_S: Final[float] = 10.0
MAIN_ASYNC_TIMEOUT_S: Final[float] = 30.0  # optimistic-polling window
CLIENT_TIMEOUT_MARGIN_S: Final[float] = 5.0
HELLO_TIMEOUT_S: Final[float] = 1.0
POLL_TIMEOUT_S: Final[float] = 5.0
RECV_CHUNK: Final[int] = 65536
INFLIGHT_DIR_MODE: Final[int] = 0o700
INFLIGHT_FILE_MODE: Final[int] = 0o600
# A crash-recovery record left by a Flame session that later crashed (or was
# killed) has no other cleanup path, so it is swept once at server startup
# (`__main__.main`), like the bridge's own `impl.RESULT_FILE_MAX_AGE_S` for its
# results/ directory.
INFLIGHT_MAX_AGE_S: Final[float] = 7 * 24 * 60 * 60
# BRIDGE_LOST means Flame just died; shell.log's "abnormal termination" line
# and app.log's final lines are sometimes written a moment after the socket
# actually drops, so read the crash channel only after this grace period.
CRASH_LOG_GRACE_S: Final[float] = 1.0

RUN_DIR_ENV: Final[str] = "FLAME_MCP_RUN_DIR"
BRIDGE_SOCKET_ENV: Final[str] = "FLAME_BRIDGE_SOCKET"


def _default_run_dir() -> Path:
    override = os.environ.get(RUN_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "flame-mcp" / "run"


def default_run_dir() -> Path:
    """Returns the discovery default run directory.

    For callers (server.py's introspection tools) that need it without duplicating
    the path logic.
    """
    return _default_run_dir()


def timeout_for(thread: Thread) -> float:
    match thread:
        case "main":
            return MAIN_TIMEOUT_S
        case "main-async":
            return MAIN_ASYNC_TIMEOUT_S
        case "server":
            raise ValueError(
                "thread='server' operations run entirely on the server and are "
                "never sent to the bridge, so they have no bridge-side timeout"
            )
        case _ as unreachable:
            assert_never(unreachable)


@dataclass(frozen=True)
class CrashInfo:
    app_log_tail: tuple[str, ...]
    shell_crash_lines: tuple[str, ...]
    inflight_path: Path | None


@dataclass(frozen=True)
class BridgeResult:
    ok: bool
    result: Any
    error_code: ErrorCode | None
    error: str | None
    traceback: str | None
    queued_ms: int | None
    run_ms: int | None
    timeout_phase: str | None
    pending_handle: str | None
    app_log: tuple[str, ...]
    app_log_note: str | None
    crash: CrashInfo | None
    inflight_path: Path | None
    duration_ms: int
    bridge_features: tuple[str, ...] = ()
    """What the bridge that took this call announced in `hello` (`features`).

    Empty for an older bridge, or when no bridge was reached.
    """
    hint: str | None = None
    """A hint that fits this failure better than the error code's default one."""


class BridgeProtocolError(Exception):
    """The bridge violated the wire protocol.

    This is a server bug, not an ErrorCode, e.g. a `protocolError` reply or an unknown
    `errorCode`.
    """


@dataclass(frozen=True)
class BridgeCandidate:
    path: Path
    pid: int
    flame_version: str
    project_name: str | None
    bridge_abi: int
    features: tuple[str, ...] = ()


@dataclass(frozen=True)
class _DiscoveryFailure:
    error_code: ErrorCode
    message: str


class _ConnectFailed(Exception):
    """Pre-send connection failure (ENOENT / ECONNREFUSED) -- BRIDGE_UNAVAILABLE."""


class _ResponseTimeout(Exception):
    """Our own client-side socket timeout elapsed with no bytes back at all."""


class _ResponseLost(Exception):
    """EOF / reset while reading a response that had already been sent."""


def _recv_line(sock: socket.socket) -> bytes:
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = sock.recv(RECV_CHUNK)
        except TimeoutError as exc:
            raise _ResponseTimeout(str(exc)) from exc
        except OSError as exc:
            raise _ResponseLost(str(exc)) from exc
        if not chunk:
            raise _ResponseLost("connection closed before a full response was received")
        buf += chunk
    return buf.split(b"\n", 1)[0]


def _send_request(path: Path, req: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(str(path))
    except OSError as exc:
        sock.close()
        raise _ConnectFailed(str(exc)) from exc
    try:
        try:
            sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        except OSError as exc:
            raise _ResponseLost(str(exc)) from exc
        line = _recv_line(sock)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeProtocolError(f"invalid JSON from bridge: {exc}") from exc
    if not isinstance(data, dict):
        raise BridgeProtocolError("bridge response is not a JSON object")
    protocol_error = data.get("protocolError")
    if protocol_error is not None:
        raise BridgeProtocolError(str(protocol_error))
    error_code = data.get("errorCode")
    if error_code is not None and error_code not in get_args(ErrorCode):
        raise BridgeProtocolError(f"unknown errorCode from bridge: {error_code!r}")
    return data


def _hello(path: Path, timeout_s: float) -> dict[str, Any] | None:
    """Sends a hello over a fresh connection, best effort.

    Returns None for anything short of a clean `{"ok": true, ...}` reply. Used both
    for discovery and for the post-timeout liveness re-check, neither of which wants
    an exception for the common case of "nothing is there".
    """
    try:
        data = _send_request(path, {"v": 1, "id": uuid.uuid4().hex, "kind": "hello"}, timeout_s)
    except (_ConnectFailed, _ResponseTimeout, _ResponseLost, BridgeProtocolError):
        return None
    return data


def _discover(run_dir: Path) -> BridgeCandidate | _DiscoveryFailure:
    override = os.environ.get(BRIDGE_SOCKET_ENV)
    paths = [Path(override)] if override else sorted(run_dir.glob("flame-mcp-*.sock"))
    if not paths:
        return _DiscoveryFailure("BRIDGE_UNAVAILABLE", f"no bridge socket found under {run_dir}")

    alive: list[BridgeCandidate] = []
    abi_mismatches: list[str] = []
    unreachable: list[str] = []
    for path in paths:
        hello = _hello(path, HELLO_TIMEOUT_S)
        if hello is None or hello.get("ok") is not True:
            unreachable.append(str(path))
            continue
        abi = hello.get("bridgeAbi")
        if abi != BRIDGE_ABI:
            abi_mismatches.append(f"{path} (bridgeAbi={abi!r})")
            continue
        pid = hello.get("pid")
        flame_version = hello.get("flameVersion")
        if not isinstance(pid, int) or not isinstance(flame_version, str):
            unreachable.append(str(path))
            continue
        project_name = hello.get("projectName")
        features_raw = hello.get("features")
        features = (
            tuple(f for f in features_raw if isinstance(f, str))
            if isinstance(features_raw, list)
            else ()
        )
        alive.append(
            BridgeCandidate(
                path=path,
                pid=pid,
                flame_version=flame_version,
                project_name=project_name if isinstance(project_name, str) else None,
                bridge_abi=abi,
                features=features,
            )
        )

    if len(alive) == 1:
        return alive[0]
    if len(alive) >= 2:
        listing = "; ".join(
            f"{c.path} (pid={c.pid}, flameVersion={c.flame_version}, "
            f"projectName={c.project_name!r})"
            for c in alive
        )
        return _DiscoveryFailure(
            "BRIDGE_AMBIGUOUS",
            f"multiple live bridges found: {listing}. Set {BRIDGE_SOCKET_ENV} to select one.",
        )
    if abi_mismatches and not unreachable:
        return _DiscoveryFailure(
            "BRIDGE_UNAVAILABLE",
            f"no bridge with a matching ABI ({BRIDGE_ABI}) found; "
            f"ABI mismatch on: {', '.join(abi_mismatches)}",
        )
    if override:
        return _DiscoveryFailure(
            "BRIDGE_UNAVAILABLE",
            f"no live bridge at {override} (from {BRIDGE_SOCKET_ENV})",
        )
    return _DiscoveryFailure("BRIDGE_UNAVAILABLE", f"no live bridge found under {run_dir}")


def probe_features(run_dir: Path | None = None) -> tuple[str, ...]:
    """Returns the optional features the live bridge announces (`hello.features`).

    Empty when there is none (no bridge, more than one, or an older bridge that
    announces nothing).
    """
    effective_run_dir = run_dir if run_dir is not None else _default_run_dir()
    candidate = _discover(effective_run_dir)
    if isinstance(candidate, _DiscoveryFailure):
        return ()
    return candidate.features


def _inflight_dir(run_dir: Path) -> Path:
    d = run_dir / "inflight"
    os.makedirs(d, mode=INFLIGHT_DIR_MODE, exist_ok=True)
    os.chmod(d, INFLIGHT_DIR_MODE)
    return d


def _write_inflight(
    run_dir: Path,
    request_id: str,
    label: str | None,
    thread: Thread,
    timeout_s: float,
    candidate: BridgeCandidate,
    code: str,
    handle: str | None = None,
) -> Path:
    d = _inflight_dir(run_dir)
    final_path = d / f"{request_id}.json"
    tmp_path = d / f".{request_id}.json.tmp"
    payload = {
        "id": request_id,
        "label": label,
        "thread": thread,
        "timeoutS": timeout_s,
        "sentAt": datetime.now(UTC).isoformat(),
        "socket": str(candidate.path),
        "bridgePid": candidate.pid,
        "handle": handle,
        "codeSha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "code": code,
    }
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, INFLIGHT_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp_path, final_path)
    return final_path


def _remove_inflight(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def cleanup_stale_inflight(run_dir: Path | None = None) -> int:
    """Removes ``inflight/`` records older than `INFLIGHT_MAX_AGE_S`.

    Called once at server startup, before any MCP traffic. It never touches a record a
    request currently in flight could still need, since a live request's record is
    always younger than that. Returns the number removed.
    """
    effective_run_dir = run_dir if run_dir is not None else _default_run_dir()
    d = effective_run_dir / "inflight"
    if not d.is_dir():
        return 0
    cutoff = time.time() - INFLIGHT_MAX_AGE_S
    removed = 0
    for entry in d.glob("*.json"):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed


@dataclass(frozen=True)
class InflightSummary:
    count: int
    newest: dict[str, Any] | None


def read_inflight_summary(run_dir: Path | None = None) -> InflightSummary:
    """Summarizes leftover `inflight/` records for `flame_version_info`.

    E.g. one left behind by a Flame session that crashed before this server could
    observe BRIDGE_LOST and clean up on its own. A record that fails to parse still
    counts (it is still a leftover file on disk) but is skipped when picking `newest`.
    """
    effective_run_dir = run_dir if run_dir is not None else _default_run_dir()
    d = effective_run_dir / "inflight"
    if not d.is_dir():
        return InflightSummary(count=0, newest=None)
    entries = list(d.glob("*.json"))
    newest_record: dict[str, Any] | None = None
    newest_sent_at = ""
    for entry in entries:
        try:
            with open(entry, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        sent_at = data.get("sentAt")
        if not isinstance(sent_at, str):
            continue
        if newest_record is None or sent_at > newest_sent_at:
            newest_record = data
            newest_sent_at = sent_at
    newest = (
        {
            "id": newest_record.get("id"),
            "label": newest_record.get("label"),
            "sentAt": newest_record.get("sentAt"),
        }
        if newest_record is not None
        else None
    )
    return InflightSummary(count=len(entries), newest=newest)


@dataclass(frozen=True)
class InflightEntry:
    record: dict[str, Any]
    elapsed_s: float | None
    """Seconds since the record's `sentAt`, None when that is not a readable timestamp."""


@dataclass(frozen=True)
class InflightListing:
    items: list[InflightEntry]
    """The readable records, newest first (by `sentAt`; one without a `sentAt` goes last)."""
    total: int
    """Every record file, readable or not."""
    unreadable: int


def _elapsed_since(sent_at: Any) -> float | None:
    if not isinstance(sent_at, str):
        return None
    try:
        sent = datetime.fromisoformat(sent_at)
    except ValueError:
        return None
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - sent).total_seconds())


def read_inflight_records(run_dir: Path | None = None) -> InflightListing:
    """Returns every readable `inflight/` record as `_write_inflight` wrote it.

    Records are newest first, with the time since each was sent. A file that does not
    parse (or is not an object) counts in `total` and `unreadable` and is not listed.
    Nothing is removed.
    """
    effective_run_dir = run_dir if run_dir is not None else _default_run_dir()
    d = effective_run_dir / "inflight"
    if not d.is_dir():
        return InflightListing(items=[], total=0, unreadable=0)
    entries = sorted(d.glob("*.json"))
    items: list[InflightEntry] = []
    unreadable = 0
    for entry in entries:
        try:
            with open(entry, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            unreadable += 1
            continue
        if not isinstance(data, dict):
            unreadable += 1
            continue
        items.append(InflightEntry(record=data, elapsed_s=_elapsed_since(data.get("sentAt"))))
    items.sort(key=lambda item: str(item.record.get("sentAt") or ""), reverse=True)
    return InflightListing(items=items, total=len(entries), unreadable=unreadable)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _app_log_note(read_result: LogReadResult) -> str | None:
    """Returns a note on *why* `app_log` might be empty or incomplete.

    Keeps a read failure or a rotation from looking identical to "no warnings
    happened".
    """
    if read_result.reason is not None:
        return read_result.reason
    if read_result.rotated:
        return "app.log rotated during the call; read from the start of the new file"
    return None


def _simple_result(
    started: float,
    error_code: ErrorCode,
    error: str,
    app_log: tuple[str, ...] = (),
    app_log_note: str | None = None,
    inflight_path: Path | None = None,
    hint: str | None = None,
) -> BridgeResult:
    return BridgeResult(
        ok=False,
        result=None,
        error_code=error_code,
        error=error,
        traceback=None,
        queued_ms=None,
        run_ms=None,
        timeout_phase=None,
        pending_handle=None,
        app_log=app_log,
        app_log_note=app_log_note,
        crash=None,
        inflight_path=inflight_path,
        duration_ms=_elapsed_ms(started),
        hint=hint,
    )


def _bridge_lost_result(
    started: float,
    app_log_window: LogWindow,
    app_mark: LogMark,
    shell_mark: LogMark,
    inflight_path: Path | None,
) -> BridgeResult:
    # Flame just died (or is suspected to have); shell.log's "abnormal termination"
    # line and app.log's tail sometimes land a moment after the socket itself
    # drops, so wait before reading either.
    time.sleep(CRASH_LOG_GRACE_S)
    app_read = app_log_window.read(app_mark)
    crash = CrashInfo(
        app_log_tail=app_log_window.tail(20),
        shell_crash_lines=crash_lines_since(shell_mark),
        inflight_path=inflight_path,
    )
    return BridgeResult(
        ok=False,
        result=None,
        error_code="BRIDGE_LOST",
        error="the connection was lost before a response was received",
        traceback=None,
        queued_ms=None,
        run_ms=None,
        timeout_phase=None,
        pending_handle=None,
        app_log=app_read.lines,
        app_log_note=_app_log_note(app_read),
        crash=crash,
        inflight_path=inflight_path,
        duration_ms=_elapsed_ms(started),
    )


def _pending_handle_of(response: dict[str, Any]) -> str | None:
    pending = response.get("pending")
    if isinstance(pending, dict):
        handle = pending.get("handle")
        return handle if isinstance(handle, str) else None
    handle = response.get("handle")
    return handle if isinstance(handle, str) else None


def _result_from_response(
    response: dict[str, Any],
    started: float,
    app_log_window: LogWindow,
    app_mark: LogMark,
    inflight_path: Path | None,
) -> BridgeResult:
    app_read = app_log_window.read(app_mark)
    return BridgeResult(
        ok=bool(response.get("ok")),
        result=response.get("result"),
        error_code=response.get("errorCode"),
        error=response.get("error"),
        traceback=response.get("traceback"),
        queued_ms=response.get("queuedMs"),
        run_ms=response.get("runMs"),
        timeout_phase=response.get("timeoutPhase"),
        pending_handle=_pending_handle_of(response),
        app_log=app_read.lines,
        app_log_note=_app_log_note(app_read),
        crash=None,
        inflight_path=inflight_path,
        duration_ms=_elapsed_ms(started),
    )


NOT_SENT_PREFIX: Final[str] = "nothing was sent to Flame"
"""Starts the error of a call that `exec` refused because its code was not ASCII."""

_NON_ASCII_CODE_HINT: Final[str] = (
    "The code built for Flame held a non-ASCII character, so nothing was sent and nothing "
    "changed in Flame. It is probably not from this call's arguments (they were checked); "
    "it may be a value read from Flame or from the journal, or a setting such as "
    "FLAME_MCP_JOURNAL_DIR or FLAME_MCP_CHECKPOINT_LIBRARY. Flame takes ASCII only."
)


def was_refused_before_sending(env: Mapping[str, Any]) -> bool:
    """Whether an envelope is the answer to code that `exec` refused to send (not ASCII)."""
    return env.get("errorCode") == "INVALID_ARGS" and str(env.get("error", "")).startswith(
        NOT_SENT_PREFIX
    )


def _non_ascii_code_message(code: str, label: str | None) -> str:
    char = next(ch for ch in code if not ch.isascii())
    subject = f"the code for '{label}'" if label and label.isascii() else "the code"
    return f"{NOT_SENT_PREFIX}: {subject} holds a non-ASCII character U+{ord(char):04X}"


def exec(  # matches the wire protocol's `kind: exec`; shadows the builtin on purpose
    code: str,
    thread: Thread,
    timeout_s: float | None = None,
    label: str | None = None,
    *,
    run_dir: Path | None = None,
    handle: str | None = None,
) -> BridgeResult:
    """Sends `code` to the bridge and returns the classified result.

    `handle`: a caller-chosen handle (32 lowercase hex characters) for a `main-async` call. A
    bridge that announces `client_handle` files the call's result under it (also when the reply
    is delivered in-band), so `poll(handle)` can still fetch it if the connection is lost. It is
    sent only to such a bridge; `BridgeResult.bridge_features` says which one took the call.

    Code that is not ASCII is refused with INVALID_ARGS before any connection is made: nothing
    was sent, so unlike a lost answer there is nothing in doubt.
    """
    if thread == "server":
        raise ValueError(
            "thread='server' operations are handled entirely on the server "
            "and must not be sent to the bridge"
        )
    effective_run_dir = run_dir if run_dir is not None else _default_run_dir()
    effective_timeout = timeout_s if timeout_s is not None else timeout_for(thread)
    started = time.monotonic()

    if not code.isascii():
        return _simple_result(
            started,
            "INVALID_ARGS",
            _non_ascii_code_message(code, label),
            hint=_NON_ASCII_CODE_HINT,
        )

    candidate = _discover(effective_run_dir)
    if isinstance(candidate, _DiscoveryFailure):
        return _simple_result(started, candidate.error_code, candidate.message)

    app_log_window = LogWindow(app_log_path(candidate.flame_version))
    shell_log_window = LogWindow(shell_log_path(candidate.flame_version))
    app_mark = app_log_window.mark()
    shell_mark = shell_log_window.mark()

    sent_handle = (
        handle if handle is not None and FEATURE_CLIENT_HANDLE in candidate.features else None
    )
    request_id = uuid.uuid4().hex
    inflight_path = _write_inflight(
        effective_run_dir,
        request_id,
        label,
        thread,
        effective_timeout,
        candidate,
        code,
        sent_handle,
    )

    socket_timeout = effective_timeout + CLIENT_TIMEOUT_MARGIN_S
    req = {
        "v": 1,
        "id": request_id,
        "kind": "exec",
        "code": code,
        "thread": thread,
        "timeoutS": effective_timeout,
    }
    if sent_handle is not None:
        req["handle"] = sent_handle

    keep_inflight = False
    try:
        response = _send_request(candidate.path, req, socket_timeout)
    except _ConnectFailed as exc:
        return replace(
            _simple_result(started, "BRIDGE_UNAVAILABLE", str(exc)),
            bridge_features=candidate.features,
        )
    except _ResponseLost:
        keep_inflight = True
        return replace(
            _bridge_lost_result(started, app_log_window, app_mark, shell_mark, inflight_path),
            bridge_features=candidate.features,
        )
    except _ResponseTimeout:
        hello = _hello(candidate.path, HELLO_TIMEOUT_S)
        if hello is not None and hello.get("ok") is True:
            # The bridge is alive but it is unknown whether it will still run this
            # exec later (it may still be QUEUED/RUNNING on its side), so keep the
            # in-flight record and surface its path rather than declaring it resolved.
            keep_inflight = True
            app_read = app_log_window.read(app_mark)
            return replace(
                _simple_result(
                    started,
                    "TIMEOUT",
                    "bridge alive but no reply",
                    app_log=app_read.lines,
                    app_log_note=_app_log_note(app_read),
                    inflight_path=inflight_path,
                ),
                bridge_features=candidate.features,
            )
        keep_inflight = True
        return replace(
            _bridge_lost_result(started, app_log_window, app_mark, shell_mark, inflight_path),
            bridge_features=candidate.features,
        )
    finally:
        if not keep_inflight:
            _remove_inflight(inflight_path)

    return replace(
        _result_from_response(response, started, app_log_window, app_mark, inflight_path=None),
        bridge_features=candidate.features,
    )


def poll(handle: str, *, run_dir: Path | None = None) -> BridgeResult:
    effective_run_dir = run_dir if run_dir is not None else _default_run_dir()
    started = time.monotonic()

    candidate = _discover(effective_run_dir)
    if isinstance(candidate, _DiscoveryFailure):
        return _simple_result(started, candidate.error_code, candidate.message)

    app_log_window = LogWindow(app_log_path(candidate.flame_version))
    shell_log_window = LogWindow(shell_log_path(candidate.flame_version))
    app_mark = app_log_window.mark()
    shell_mark = shell_log_window.mark()

    req = {"v": 1, "id": uuid.uuid4().hex, "kind": "poll", "handle": handle}
    try:
        response = _send_request(candidate.path, req, POLL_TIMEOUT_S)
    except _ConnectFailed as exc:
        return _simple_result(started, "BRIDGE_UNAVAILABLE", str(exc))
    except _ResponseLost:
        return _bridge_lost_result(started, app_log_window, app_mark, shell_mark, None)
    except _ResponseTimeout:
        hello = _hello(candidate.path, HELLO_TIMEOUT_S)
        if hello is not None and hello.get("ok") is True:
            app_read = app_log_window.read(app_mark)
            return _simple_result(
                started,
                "TIMEOUT",
                "bridge alive but no reply",
                app_log=app_read.lines,
                app_log_note=_app_log_note(app_read),
            )
        return _bridge_lost_result(started, app_log_window, app_mark, shell_mark, None)

    return _result_from_response(response, started, app_log_window, app_mark, inflight_path=None)
