"""`batch.delete`, server side.

  1. inspect   one Flame call reads the group and judges it (`batch_delete_prelude._bd_blocked`);
               a refusal ends the call: nothing is saved, journaled or deleted
  2. save      one Flame call judges again and saves the setup; no saved setup, no delete
  3. intent    written and flushed before the delete: who (the user and process of this server),
               when, the uid and name, the arguments and the path of the saved setup
  4. delete    one Flame call judges a third time, deletes the group and looks its uid up
  5. outcome   written; `result.setupPath` is in every answer that got as far as the save

The journal record is `kind: "batch_delete"`. A call that ends without an answer (TIMEOUT /
BRIDGE_LOST / BRIDGE_UNAVAILABLE, or a pending handle) leaves the intent open and shuts the write
guard; `journal.check` decides it by looking for the uid on the Desktop
(`check_batch_delete_intent`).
There is no revert: the group is gone, and the saved setup is loaded into a new group by hand.
"""

from __future__ import annotations

import getpass
import os
import time
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.checkpointed import note_unresolved
from flame_mcp.desktop_run import PHASE_ARG, PHASE_READ, PHASE_WRITE, pre_read_failure
from flame_mcp.journal import (
    STATE_LANDED,
    STATE_NOT_LANDED,
    IntentView,
    new_id,
    now_iso,
)
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.registry import RegisteredOperation
from flame_mcp.render_check import _append_check, _unknown_entry
from flame_mcp.transport import elapsed_ms
from flame_mcp.types import ErrorCode

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime, ServerContext

DELETE_OP: Final[str] = "batch.delete"
BATCH_DELETE_KIND: Final[str] = "batch_delete"
PHASE_SAVE: Final[str] = "save"
PHASE_ALIVE: Final[str] = "alive"
UNDO_NOTE: Final[str] = (
    "The group cannot be put back by journal.revert: create a Batch group and batch.load_setup "
    "the file in result.setupPath."
)


def _envelope(
    op: RegisteredOperation,
    started: float,
    *,
    result: Any,
    code: str | None = None,
    error: str | None = None,
    journal: dict[str, Any] | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=code is None,
        result=result,
        affected=1 if code is None else 0,
        error=error,
        error_code=code,  # type: ignore[arg-type]
        journal=journal,
        execution=envelope_mod.execution_meta(op),
        duration_ms=elapsed_ms(started),
        hint_override=hint,
    )


def _step(op: RegisteredOperation, rt: Any, args: dict[str, Any], phase: str, **extra: Any) -> Any:
    return execution.run_op(op, {**args, PHASE_ARG: phase, **extra}, run_dir=rt.run_dir)


def _facts_of(env: dict[str, Any]) -> dict[str, Any] | None:
    raw = env.get("result")
    return raw if env.get("ok") is True and isinstance(raw, dict) else None


def _intent(
    op: RegisteredOperation, args: dict[str, Any], facts: dict[str, Any], group: str, intent_id: str
) -> dict[str, Any]:
    return {
        "type": "intent",
        "id": intent_id,
        "group": group,
        "ts": now_iso(),
        "op": op.name,
        "kind": BATCH_DELETE_KIND,
        "args": dict(args),
        "target": {"kind": "batch", "uid": facts.get("uid")},
        "uid": facts.get("uid"),
        "name": facts.get("name"),
        "nodeCount": facts.get("nodeCount"),
        "content": facts.get("content"),
        "setupPath": facts.get("setupPath"),
        "user": _user(),
        "pid": os.getpid(),
    }


def _user() -> str | None:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return None


def run_batch_delete(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `batch.delete`."""
    started = time.monotonic()
    with rt.write_lock:
        denial = rt.guard.denial()
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=elapsed_ms(started),
                hint=denial.hint,
            )
        env = _step(op, rt, op_args, PHASE_READ)
        facts = _facts_of(env)
        if facts is None:
            return pre_read_failure(op, env, "deleted")
        if facts.get("errorCode"):
            return _envelope(
                op, started, result=facts, code=facts["errorCode"], error=facts["error"]
            )
        env = _step(op, rt, op_args, PHASE_SAVE)
        saved = _facts_of(env)
        if saved is None:
            return pre_read_failure(op, env, "deleted")
        if saved.get("errorCode"):
            return _envelope(
                op, started, result=saved, code=saved["errorCode"], error=saved["error"]
            )
        group_id = group if group is not None else new_id("g")
        intent_id = new_id("i")
        try:
            rt.journal.append(_intent(op, op_args, saved, group_id, intent_id))
        except OSError as exc:
            return _envelope(
                op,
                started,
                result=saved,
                code="POLICY_DENIED",
                error=f"nothing was deleted: the journal cannot be written ({exc})",
                hint=f"The setup stays saved at {saved.get('setupPath')}.",
            )
        env = _step(op, rt, op_args, PHASE_WRITE, setup_path=saved["setupPath"])
        return _finish(rt, op, env, saved, group_id, intent_id, started)


def _finish(
    rt: Runtime,
    op: RegisteredOperation,
    env: dict[str, Any],
    saved: dict[str, Any],
    group_id: str,
    intent_id: str,
    started: float,
) -> dict[str, Any]:
    journal = {"group": group_id, "id": intent_id, "undo": "unsupported", "revert": None}
    code = env.get("errorCode")
    if code in UNKNOWN_END_CODES or env.get("pending") is not None:
        rt.guard.block(intent_id, group_id, op.name, str(code or "TIMEOUT"))
        note_unresolved(rt, intent_id, group_id, env)
        env["journal"] = journal
        env["result"] = saved
        env["hint"] = (
            f"{env.get('hint') or ''} It is not known whether the group was deleted: this server "
            f"now refuses every write until journal.check (group={group_id!r}) has looked for "
            f"the group. The setup saved before the delete is at {saved.get('setupPath')}."
        ).strip()
        return env
    flame = _facts_of(env)
    result = {**saved, **flame} if flame is not None else saved
    result["setupSize"] = saved.get("setupSize")
    failure: tuple[str, str] | None = None
    if flame is None:
        failure = (str(code or "FLAME_API_ERROR"), str(env.get("error")))
    elif flame.get("errorCode"):
        failure = (flame["errorCode"], flame["error"])
    deleted = bool(flame and flame.get("deleted"))
    try:
        rt.journal.append(
            {
                "type": "outcome",
                "id": intent_id,
                "group": group_id,
                "ts": now_iso(),
                "ok": failure is None,
                "applied": [],
                "failed": {} if failure is None else {DELETE_OP: failure[1]},
                "errorCode": failure[0] if failure else None,
                "readBack": None,
                "verdict": STATE_LANDED if deleted else STATE_NOT_LANDED,
                "setupPath": saved.get("setupPath"),
                "warnings": (flame or {}).get("warnings"),
            }
        )
    except OSError:
        rt.guard.block(intent_id, group_id, op.name, "JOURNAL_OUTCOME_NOT_WRITTEN")
    out = _envelope(
        op,
        started,
        result={**result, "undo": UNDO_NOTE},
        code=failure[0] if failure else None,
        error=failure[1] if failure else None,
        journal=journal,
    )
    if flame and flame.get("warnings"):
        out["degraded"] = [*out.get("degraded", []), *flame["warnings"]]
    return out


def check_batch_delete_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """Decide an open `batch.delete` intent: the uid gone from the Desktop = landed, still there =
    not_landed, a read that fails = unknown. Nothing is written to the project."""
    intent = view.intent
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    uid = intent.get("uid")
    op = ctx.registry.get(DELETE_OP)
    if op is None or not isinstance(uid, str):
        return _unknown_entry(entry, "the intent records no uid to look for", noun="delete")
    env = execution.run_op(
        op, {"target": {"kind": "batch", "uid": uid}, PHASE_ARG: PHASE_ALIVE}, run_dir=ctx.run_dir
    )
    raw = env.get("result")
    if env.get("ok") is not True or not isinstance(raw, dict):
        code: ErrorCode | None = env.get("errorCode")
        return _unknown_entry(
            entry, f"Flame could not be read ({code}: {env.get('error')})", code, noun="delete"
        )
    verdict = STATE_NOT_LANDED if raw.get("there") else STATE_LANDED
    entry.update(
        verdict=verdict,
        detail=f"setup saved at {intent.get('setupPath')}",
        perKey={"group": "before" if raw.get("there") else "after"},
        current={"there": raw.get("there")},
    )
    _append_check(ctx, view, verdict, entry["perKey"], entry["current"])
    return entry
