"""The write-ahead run that `render.batch`, `render.export`, `render.clip` and `render.still`
share (they are not undone by a checkpoint): the refusals found before anything is journaled
(`Refused`), the intent, the Flame call with its client handle, the answer that never comes
(`_unknown_end`) and the closing of the outcome. What each op judges and journals is in its own
module (`render_run`, `export_run`, `render_clip_run`, `still_run`)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import errors, execution, transport
from flame_mcp.checkpointed import ASCII_REFUSAL_HINT, first_non_ascii
from flame_mcp.desktop_run import PHASE_READ, pre_read_failure
from flame_mcp.journal import now_iso
from flame_mcp.journal_records import IntentRecord, OutcomeRecord, PendingRecord
from flame_mcp.journaled_write import (
    UNKNOWN_END_CODES,
    close_outcome,
    note_unresolved,
    outcome_not_journaled_note,
)
from flame_mcp.registry import RegisteredOperation
from flame_mcp.transport import BridgeProtocolError, UnknownHandleError, elapsed_ms
from flame_mcp.types import PHASE_ARG, ErrorCode
from flame_mcp_bridge.impl import FEATURE_CLIENT_HANDLE

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

BRIDGE_BUSY_NOTE: Final[str] = (
    "While a Foreground render is still running the bridge may not answer at all, not even to its "
    "hello: wait a little, then run journal.check again."
)
BRIDGE_BUSY_CODES: Final[frozenset[str]] = frozenset({"BRIDGE_UNAVAILABLE", "TIMEOUT"})
HANDLE_SOURCE_CLIENT: Final[str] = "client"
"""`handleSource` of an intent whose `handle` this server chose and sent to the bridge."""
_PENDING_NOTE: Final[str] = (
    "the bridge answered with a pending handle: the render was still running when timeout_s elapsed"
)
DEFAULT_UNKNOWN_READS: Final[str] = "Flame and the output files"
DEFAULT_UNKNOWN_NOUN: Final[str] = "render"


def new_client_handle(rt: Runtime) -> str | None:
    """A fresh handle for the run when the live bridge announces `client_handle`, else None (an
    older bridge would ignore it and use its own)."""
    if FEATURE_CLIENT_HANDLE in transport.probe_features(rt.run_dir):
        return uuid.uuid4().hex
    return None


def fail_env(env: dict[str, Any], code: ErrorCode, message: str) -> None:
    env.update(
        ok=False,
        error=message,
        errorCode=code,
        hint=errors.hint_for(code),
        retryable=errors.is_retryable(code),
    )


def _unknown_end(
    ctx: Runtime,
    env: dict[str, Any],
    op: RegisteredOperation,
    intent_id: str,
    group_id: str,
    code: str,
    timeout_s: float,
    client_handle: str | None,
    *,
    reads: str = DEFAULT_UNKNOWN_READS,
    noun: str = DEFAULT_UNKNOWN_NOUN,
) -> dict[str, Any]:
    """`noun` is what the hint and the notes call the thing that may still be running."""
    note_unresolved(ctx, intent_id, group_id, env, op.name, code)
    handle = (env.get("pending") or {}).get("handle")
    if isinstance(handle, str) and handle:
        if client_handle is not None and handle != client_handle:
            env["degraded"] = [
                *env.get("degraded", []),
                f"the bridge answered with its own pending handle {handle}, not the handle "
                f"{client_handle} this server gave it: journal.check asks for the bridge's own",
            ]
        try:
            pending: PendingRecord = {
                "type": "pending",
                "id": intent_id,
                "group": group_id,
                "ts": now_iso(),
                "handle": handle,
                "timeoutS": timeout_s,
            }
            ctx.journal.append(pending)
        except OSError:
            pass  # the intent alone says "in doubt"; without the handle check falls back to files
    if env.get("pending") is not None or env.get("ok") is True:
        fail_env(env, "TIMEOUT", _PENDING_NOTE.replace("render", noun))
    hint = str(env.get("hint") or "")
    handle_note = (
        f" If the bridge received the call it keeps the {noun}'s result under handle "
        f"{client_handle} (chosen by this server), and journal.check fetches it once the "
        "bridge answers again."
        if client_handle is not None
        else ""
    )
    env["hint"] = (
        f"{hint} It is not known whether the {noun} finished: a Foreground {noun} keeps "
        "Flame occupied and a TIMEOUT does not stop it. This server now refuses every "
        f"write until journal.check (group={group_id!r}) has read {reads} "
        f"again.{handle_note} {BRIDGE_BUSY_NOTE.replace('render', noun)}"
    ).strip()
    env["journal"] = {"group": group_id, "id": intent_id, "undo": "unsupported", "revert": None}
    return env


class Refused(Exception):
    """A refusal found before anything was journaled or run; `env` is the answer."""

    def __init__(self, env: dict[str, Any]) -> None:
        super().__init__(env.get("error"))
        self.env = env


def read_facts(
    rt: Runtime, op: RegisteredOperation, op_args: Mapping[str, Any], done: str
) -> dict[str, Any]:
    """What Flame says about the target (the op's read phase); `Refused` when it gives no usable
    answer. `done` words the refusal ("rendered")."""
    pre_env = execution.run_op(op, {**op_args, PHASE_ARG: PHASE_READ}, run_dir=rt.run_dir)
    raw = pre_env.get("result")
    if pre_env.get("ok") is not True or not isinstance(raw, dict):
        raise Refused(pre_read_failure(op, pre_env, done))
    facts: dict[str, Any] = raw
    return facts


def check_ascii(op: RegisteredOperation, started: float, done: str, **strings: Any) -> None:
    """`Refused` when a string that would be sent to Flame is not ASCII."""
    issue = first_non_ascii(**strings)
    if issue is not None:
        raise Refused(
            envelope_mod.refusal(
                op,
                "INVALID_ARGS",
                f"nothing was {done}: {issue.path}: {issue.message}",
                started,
                result={"ran": False},
                hint=ASCII_REFUSAL_HINT,
            )
        )


def append_intent(
    rt: Runtime,
    op: RegisteredOperation,
    record: IntentRecord,
    started: float,
    *,
    done: str,
    action: str,
) -> None:
    """Journal the intent before the bridge gets the call; `Refused` when the journal cannot be
    written. `done` and `action` word it ("rendered" / "render")."""
    try:
        rt.journal.append(record)
    except OSError as exc:
        raise Refused(
            envelope_mod.refusal(
                op,
                "POLICY_DENIED",
                f"nothing was {done}: the journal cannot be written ({exc}), and this server "
                f"does not {action} without an intent on record",
                started,
                result={"ran": False},
                hint=f"Make {rt.journal.directory} writable, then retry.",
            )
        ) from exc


def run_and_close(
    rt: Runtime,
    op: RegisteredOperation,
    run_args: dict[str, Any],
    *,
    timeout_s: float,
    handle: str | None,
    intent_id: str,
    group_id: str,
    finish: Callable[[dict[str, Any]], dict[str, Any]],
    reads: str = DEFAULT_UNKNOWN_READS,
    noun: str = DEFAULT_UNKNOWN_NOUN,
    after: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The Flame call of a write-ahead run: an answer is judged by `finish`, then `after` (if
    any) adds to it once the bridge's result file is gone; no answer leaves the intent open
    (`_unknown_end`, worded by `reads` / `noun`)."""
    env = execution.run_op(op, run_args, run_dir=rt.run_dir, timeout_s=timeout_s, handle=handle)
    end_code = env.get("errorCode")
    if end_code in UNKNOWN_END_CODES or env.get("pending") is not None:
        return _unknown_end(
            rt,
            env,
            op,
            intent_id,
            group_id,
            str(end_code or "TIMEOUT"),
            timeout_s,
            handle,
            reads=reads,
            noun=noun,
        )
    finished = finish(env)
    if handle is not None and end_code != "BRIDGE_AMBIGUOUS":
        _remove_result_file(rt, finished, handle)
    return after(finished) if after is not None else finished


def settle_envelope(
    rt: Runtime,
    op: RegisteredOperation,
    env: dict[str, Any],
    judged: RenderJudgement,
    record: OutcomeRecord,
    *,
    result: dict[str, Any],
    affected: int,
    partial: dict[str, Any] | None,
    degraded: list[str],
    intent_id: str,
    group_id: str,
    started: float,
) -> dict[str, Any]:
    """Put a judged run into the envelope and close its outcome in the journal."""
    if not judged.ok and judged.code is not None:
        fail_env(env, judged.code, judged.message or "")
    env["result"] = result
    env["affected"] = affected
    env["partial"] = partial
    env["degraded"] = [*env.get("degraded", []), *degraded]
    env["journal"] = {"group": group_id, "id": intent_id, "undo": "unsupported", "revert": None}
    env["durationMs"] = elapsed_ms(started)
    exc = close_outcome(rt, record, intent_id, group_id, op.name)
    if exc is not None:
        env["degraded"] = [
            *env["degraded"],
            outcome_not_journaled_note(exc, "the run's answer"),
        ]
    return env


def _remove_result_file(rt: Runtime, env: dict[str, Any], handle: str) -> None:
    """The bridge keeps the result of a run that has a client handle in a file even when the
    answer came in-band; one poll hands it over and deletes it. Best effort: a failure only
    leaves the file to the bridge's own 24 h sweep, and is noted in `degraded`."""
    try:
        br = transport.poll(handle, run_dir=rt.run_dir)
    except UnknownHandleError:
        return  # nothing kept under it (or already gone): nothing to remove
    except BridgeProtocolError as exc:
        problem = f"PROTOCOL_ERROR: {exc}"
    else:
        if br.error_code is None:
            return
        problem = f"{br.error_code}: {br.error}"
    env["degraded"] = [
        *env.get("degraded", []),
        f"the result file the bridge keeps under handle {handle} could not be removed ({problem}); "
        "the bridge deletes it after 24 hours",
    ]


@dataclass(frozen=True)
class RenderJudgement:
    ok: bool
    code: ErrorCode | None
    message: str | None
    degraded: list[str] = field(default_factory=list)
