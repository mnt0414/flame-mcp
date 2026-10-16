"""Response envelope assembly and the byte-budget layer of the response limit.

`build_envelope` is the single pure function that shapes the wire form. Every other
function in this module calls it, or is the byte-budget degrade pass that runs on
its output.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast, get_args

from flame_mcp import errors
from flame_mcp.registry import RegisteredOperation
from flame_mcp.transport import BridgeResult, CrashInfo
from flame_mcp.types import ErrorCode

# Layer 3 of the response limit: the byte budget of one envelope (not derived from measured
# response sizes).
MAX_RESPONSE_BYTES: Final[int] = 60_000
TRACEBACK_TAIL_LINES: Final[int] = 20
LOGS_TAIL_LINES: Final[int] = 20

SPILL_DIR_ENV: Final[str] = "FLAME_MCP_SPILL_DIR"
SPILL_FILE_MODE: Final[int] = 0o600
SPILL_DIR_MODE: Final[int] = 0o700
SPILL_MAX_AGE_S: Final[float] = 7 * 24 * 60 * 60
_SPILL_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\.json")

MEDIA_PANEL_LOG_LINE: Final[str] = (
    "The Media Panel needs to be available to modify it's objects selection."
)
MEDIA_PANEL_NOTICE: Final[str] = (
    "Flame's Media Panel is not available (a modal such as the message history may be open). "
    "Until it is closed, writes to the Media Panel can silently do nothing."
)


def _default_spill_dir() -> Path:
    override = os.environ.get(SPILL_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "flame-mcp" / "spill"


def notices_from_logs(logs: Sequence[str]) -> list[str]:
    """Returns notices the Flame log lines of a call give beyond the call itself.

    Currently: the Media Panel is unavailable (Flame logs this line for every Media
    Panel access while a modal is open).
    """
    if any(MEDIA_PANEL_LOG_LINE in line for line in logs):
        return [MEDIA_PANEL_NOTICE]
    return []


def build_envelope(
    *,
    ok: bool,
    result: Any = None,
    affected: int = 0,
    undo_steps: int = 0,
    partial: Mapping[str, int] | None = None,
    pending: Mapping[str, str] | None = None,
    error: str | None = None,
    error_code: ErrorCode | None = None,
    traceback: str | None = None,
    context: Mapping[str, Any] | None = None,
    execution: Mapping[str, Any] | None = None,
    duration_ms: int = 0,
    logs: Sequence[str] = (),
    logs_note: str | None = None,
    crash: Mapping[str, Any] | None = None,
    hint_override: str | None = None,
    timeout_phase: str | None = None,
    journal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Shapes the envelope.

    `journal`: `{"group", "id"}` of the journal records a journaled write left,
    None for everything else.

    `hint`/`retryable` are derived from `error_code` here so every caller
    gets them for free instead of re-deriving them at each call site.
    `hint_override` lets a caller substitute a more specific hint (e.g. a
    policy `Denial.hint`) while `retryable` still reflects the ErrorCode.
    `timeout_phase` (`"queued" | "running" | None`) distinguishes a TIMEOUT
    that is guaranteed not to have run yet from one that may still be
    running or may already have applied; the next thing to do differs
    between the two.
    """
    hint = (
        hint_override
        if hint_override is not None
        else (errors.hint_for(error_code) if error_code is not None else None)
    )
    retryable = errors.is_retryable(error_code) if error_code is not None else None
    return {
        "ok": ok,
        "result": result,
        "affected": affected,
        "undoSteps": undo_steps,
        "partial": dict(partial) if partial is not None else None,
        "pending": dict(pending) if pending is not None else None,
        "error": error,
        "errorCode": error_code,
        "hint": hint,
        "retryable": retryable,
        "traceback": traceback,
        "context": dict(context) if context is not None else None,
        "execution": dict(execution) if execution is not None else None,
        "durationMs": duration_ms,
        "logs": list(logs),
        "logsNote": logs_note,
        "crash": dict(crash) if crash is not None else None,
        "degraded": [],
        "notices": notices_from_logs(logs),
        "timeoutPhase": timeout_phase,
        "journal": dict(journal) if journal is not None else None,
    }


def execution_meta(op: RegisteredOperation) -> dict[str, Any]:
    """Returns the effective thread/pacing and where each came from (op / category / global)."""
    return {
        "op": op.name,
        "thread": op.thread,
        "threadSource": op.sources.get("thread"),
        "pacing": op.pacing,
        "pacingSource": op.sources.get("pacing"),
        "effect": op.effect,
        "undo": op.undo,
    }


def crash_meta(crash: CrashInfo) -> dict[str, Any]:
    return {
        "inflightPath": str(crash.inflight_path) if crash.inflight_path is not None else None,
        "shellCrashLines": list(crash.shell_crash_lines),
        "appLogTail": list(crash.app_log_tail),
    }


def from_pre_bridge_error(
    *,
    error: str,
    error_code: ErrorCode,
    op: RegisteredOperation | None,
    duration_ms: int,
    hint: str | None = None,
) -> dict[str, Any]:
    """Builds the envelope for UNKNOWN_OPERATION / INVALID_ARGS / POLICY_DENIED.

    These are failures the server stops on before anything is sent to the bridge. `context` stays
    None (the bridge was never touched); `execution` is populated whenever
    the operation itself resolved (POLICY_DENIED / INVALID_ARGS), and stays
    None for UNKNOWN_OPERATION (there is no operation to describe). `hint`
    overrides the generic ErrorCode hint with a policy Denial's own hint."""
    return build_envelope(
        ok=False,
        error=error,
        error_code=error_code,
        context=None,
        execution=execution_meta(op) if op is not None else None,
        duration_ms=duration_ms,
        hint_override=hint,
    )


def timeout_error_and_pending(br: BridgeResult) -> tuple[str | None, dict[str, str] | None]:
    """Returns the error text and `pending` handle for a TIMEOUT.

    A TIMEOUT's `timeoutPhase` changes what to do next: a
    "queued" timeout is guaranteed not to have run; a "running" one may
    complete later and (when the bridge handed back a handle) can be
    polled. `br.error` is None for both real-bridge phases (the bridge's
    own JSON carries no `error` key there, only `errorCode`/`timeoutPhase`),
    so this fills in the distinction; a plain client-side "bridge alive but
    no reply" TIMEOUT (no phase) already has its own `br.error` and passes
    through unchanged. Shared by `from_bridge_result` and `flame_context`,
    the two call sites that map a raw `BridgeResult` onto the envelope."""
    if br.error_code != "TIMEOUT":
        return br.error, None
    if br.timeout_phase == "queued":
        return (
            br.error
            or "the call timed out before Flame's main thread started it; "
            "it is guaranteed not to have run",
            None,
        )
    if br.timeout_phase == "running":
        pending = {"handle": br.pending_handle} if br.pending_handle is not None else None
        return (
            br.error
            or "the call was still running when the timeout elapsed; it may complete "
            "later or may already have applied -- a read: journal.poll(handle=pending.handle); "
            "a write: journal.check",
            pending,
        )
    return br.error, None


def _partial_counts(raw: object) -> dict[str, int] | None:
    """Returns the op's own `_partial` (e.g. `{"applied": 1, "failed": 2}`) as `partial`.

    Anything that is not a `{str: int}` dict is dropped --
    an op returning garbage here is a bug in that op, not a reason to fail."""
    if not isinstance(raw, dict):
        return None
    counts = {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
    }
    return counts if len(counts) == len(raw) and counts else None


def from_bridge_result(
    op: RegisteredOperation,
    args: Mapping[str, Any],
    br: BridgeResult,
) -> dict[str, Any]:
    """Maps a `transport.BridgeResult` for `op` onto the envelope shape.

    `br.result` (when `br.ok`) is the `_result` dict the generated wrapper
    code assembled inside the bridge; see `execution.build_exec_code`.
    """
    execution = execution_meta(op)
    logs = list(br.app_log)
    logs_note = br.app_log_note
    crash = crash_meta(br.crash) if br.crash is not None else None

    if br.error_code is not None:
        # Transport-level failure (BRIDGE_LOST/BRIDGE_UNAVAILABLE/BRIDGE_AMBIGUOUS/
        # TIMEOUT/MAIN_THREAD_UNAVAILABLE): the wrapper's `_result` dict was
        # never produced, so context stays None -- never confused with the
        # "rider ran and every field failed" shape ({"selected": None, ...}).
        error_text, pending = timeout_error_and_pending(br)
        return build_envelope(
            ok=False,
            error=error_text,
            error_code=br.error_code,
            traceback=br.traceback,
            context=None,
            execution=execution,
            duration_ms=br.duration_ms,
            logs=logs,
            logs_note=logs_note,
            crash=crash,
            timeout_phase=br.timeout_phase,
            pending=pending,
            hint_override=br.hint,
        )

    if br.pending_handle is not None:
        # main-async optimistic poll didn't resolve within the window.
        return build_envelope(
            ok=True,
            pending={"handle": br.pending_handle},
            context=None,
            execution=execution,
            duration_ms=br.duration_ms,
            logs=logs,
            logs_note=logs_note,
        )

    raw = br.result
    if not isinstance(raw, dict):
        # A conforming wrapper always produces a dict; anything else is a
        # bug in this server's own generated code, not a Flame API failure.
        return build_envelope(
            ok=False,
            error=f"malformed wrapper result (expected dict, got {type(raw).__name__})",
            error_code="FLAME_API_ERROR",
            execution=execution,
            duration_ms=br.duration_ms,
            logs=logs,
            logs_note=logs_note,
        )

    context = raw.get("context") if isinstance(raw.get("context"), dict) else None
    partial = _partial_counts(raw.get("partial"))
    op_error = raw.get("opError")
    affected_raw = raw.get("affected")
    affected = affected_raw if isinstance(affected_raw, int) else 0
    undo_steps = op.undo_steps(args, affected)

    if op_error is not None:
        # An op (or its selector resolution) may raise `_OpError(code,
        # message)` to report a specific ErrorCode (SELECTOR_EMPTY/SELECTOR_TYPE/
        # SELECTOR_AMBIGUOUS) instead of the generic FLAME_API_ERROR every
        # other exception maps to. `opErrorCode` is only trusted when it is
        # actually a member of ErrorCode -- an op returning garbage here is a
        # bug in that op, not license to fabricate an unknown error code.
        op_error_code_raw = raw.get("opErrorCode")
        error_message = str(op_error)
        op_error_code: ErrorCode
        if isinstance(op_error_code_raw, str) and op_error_code_raw in get_args(ErrorCode):
            op_error_code = cast(ErrorCode, op_error_code_raw)
        else:
            op_error_code = "FLAME_API_ERROR"
            if op_error_code_raw is not None:
                error_message = (
                    f"{error_message} (op raised unknown opErrorCode {op_error_code_raw!r}; "
                    "treated as FLAME_API_ERROR)"
                )
        return build_envelope(
            ok=False,
            result=raw.get("value"),
            error=error_message,
            error_code=op_error_code,
            traceback=raw.get("opTraceback"),
            affected=affected,
            undo_steps=undo_steps,
            partial=partial,
            context=context,
            execution=execution,
            duration_ms=br.duration_ms,
            logs=logs,
            logs_note=logs_note,
        )

    if raw.get("verified") is False:
        verify_detail = raw.get("verifyDetail")
        return build_envelope(
            ok=False,
            error=(
                str(verify_detail)
                if verify_detail
                else "operation raised no exception but had no observable effect"
            ),
            error_code="SILENT_NOOP",
            affected=affected,
            undo_steps=undo_steps,
            partial=partial,
            context=context,
            execution=execution,
            duration_ms=br.duration_ms,
            logs=logs,
            logs_note=logs_note,
        )

    return build_envelope(
        ok=True,
        result=raw.get("value"),
        affected=affected,
        undo_steps=undo_steps,
        partial=partial,
        context=context,
        execution=execution,
        duration_ms=br.duration_ms,
        logs=logs,
        logs_note=logs_note,
    )


def _byte_len(envelope: Mapping[str, Any]) -> int:
    return len(json.dumps(envelope, default=str).encode("utf-8"))


def _collapse_lists(value: Any) -> tuple[Any, bool]:
    """Collapses the top-level result (and one level down) from a list to `{"count", "omitted"}`.

    Returns (new_value, changed).
    """
    if isinstance(value, list):
        return {"count": len(value), "omitted": True}, True
    if isinstance(value, dict):
        changed = False
        new_value: dict[str, Any] = dict(value)
        for key, sub in value.items():
            if isinstance(sub, list):
                new_value[key] = {"count": len(sub), "omitted": True}
                changed = True
        return new_value, changed
    return value, False


def degrade_for_size(
    envelope: Mapping[str, Any],
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    spill_dir: Path | None = None,
) -> dict[str, Any]:
    """Applies layer 3 of the response limit: a fixed-order degrade, never silent.

    Each step that fires is recorded in `degraded`. The spill file holds the envelope as
    passed in, before the traceback, logs and result were cut.
    """
    out = dict(envelope)
    degraded = list(out.get("degraded") or [])
    preexisting_degraded = list(degraded)

    if _byte_len(out) <= max_bytes:
        return out

    tb = out.get("traceback")
    if isinstance(tb, str):
        lines = tb.splitlines()
        if len(lines) > TRACEBACK_TAIL_LINES:
            out["traceback"] = "\n".join(lines[-TRACEBACK_TAIL_LINES:])
            degraded.append(f"traceback truncated to last {TRACEBACK_TAIL_LINES} lines")
    logs = out.get("logs")
    if isinstance(logs, list) and len(logs) > LOGS_TAIL_LINES:
        out["logs"] = logs[-LOGS_TAIL_LINES:]
        degraded.append(f"logs truncated to last {LOGS_TAIL_LINES} lines")
    out["degraded"] = degraded
    if _byte_len(out) <= max_bytes:
        return out

    new_result, changed = _collapse_lists(out.get("result"))
    if changed:
        out["result"] = new_result
        degraded.append("result list(s) collapsed to {count, omitted}")
    out["degraded"] = degraded
    if _byte_len(out) <= max_bytes:
        return out

    effective_spill_dir = spill_dir if spill_dir is not None else _default_spill_dir()
    effective_spill_dir.mkdir(parents=True, exist_ok=True, mode=SPILL_DIR_MODE)
    spill_path = effective_spill_dir / f"{uuid.uuid4().hex}.json"
    spill_note = "response exceeded the byte budget; full envelope spilled to disk"
    degraded.append(spill_note)
    out["degraded"] = degraded
    spilled = {**envelope, "degraded": [*preexisting_degraded, spill_note]}
    fd = os.open(spill_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, SPILL_FILE_MODE)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(spilled, default=str))
    out["result"] = {"spilledTo": str(spill_path)}
    return out


def cleanup_old_spills(
    spill_dir: Path | None = None,
    *,
    now: float | None = None,
    max_age_s: float = SPILL_MAX_AGE_S,
) -> int:
    """Remove spill files not modified for `max_age_s`. Called once at server startup.
    Returns the number removed.

    Only names this module gives a spill file (`<32 lowercase hex>.json`) are removed, so a
    FLAME_MCP_SPILL_DIR pointed at a folder that holds other files leaves them alone."""
    directory = spill_dir if spill_dir is not None else _default_spill_dir()
    cutoff = (now if now is not None else time.time()) - max_age_s
    removed = 0
    try:
        entries = [p for p in directory.glob("*.json") if _SPILL_NAME_RE.fullmatch(p.name)]
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            pass
    return removed
