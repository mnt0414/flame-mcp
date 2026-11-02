"""The journaled run of the three connection writes: `segment.create_connection`,
`segment.remove_connection` (also with `targets`) and `segment.sync_connections`.

One read call (the `check` phase) resolves the target(s) and checks them (`_sc_remove_check_many`,
`_sc_write_check`: a Gap, a library, connected_segments() raising) and answers the segment's
stable address (wiretap id of its sequence and its record_in), so the intent and the write use
that, not the caller's `selection` / `path`, which can move; a refusal there changes nothing and
journals nothing. With `targets`, each target is a call of its own (`_sc_remove_step`), so no
generated code has a loop and each callback on Flame's main thread stays short. Before each call
the target's connected segments are read again: with none left (an earlier target took the
connection away from both sides) the target is not called and is listed in `alreadyDisconnected`.

A step that fails stops the call; the targets before it stay removed. A step whose call ran but
whose read-back raised (`readBackFailed`), or that ended without an answer (TIMEOUT / BRIDGE_LOST /
BRIDGE_UNAVAILABLE), is `unknown`: the connection may have changed. A read-back that showed no
change (SILENT_NOOP) or the wrong change (FLAME_API_ERROR) is an answer, not `unknown`. Nothing is
retried.

The ops have no revert (undo unsupported), but every write is an intent in the journal (`kind`
`REMOVE_KIND`, `action` create / remove / sync, one intent per step, one group per call; the single
`target` form is one step): written before the call, closed by an outcome when Flame answered. An
`unknown` step keeps its intent open, adds an `unresolved` record and shuts the `WriteGuard`;
`journal.check` then reads `segment.connections` again (`check_remove_intent`): create is
landed / not_landed / diverged by the count, remove likewise, and sync is always diverged (a count
cannot say whether the segments were synced; read the segments and compare them by hand).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.errors import as_error_code
from flame_mcp.journal import (
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    IntentView,
    new_id,
    now_iso,
)
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.registry import RegisteredOperation
from flame_mcp.transport import elapsed_ms

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime, ServerContext

CREATE_OP: Final[str] = "segment.create_connection"
REMOVE_OP: Final[str] = "segment.remove_connection"
SYNC_OP: Final[str] = "segment.sync_connections"
CONNECTIONS_OP: Final[str] = "segment.connections"
ACTIONS: Final[dict[str, str]] = {CREATE_OP: "create", REMOVE_OP: "remove", SYNC_OP: "sync"}
"""The `action` of each write op, as the intent records it."""
REMOVE_KIND: Final[str] = "segment_connection_remove"
"""`kind` of the intent a step of any connection write makes. The intent's `action` says which
write it was, and `check_remove_intent` decides by it. `journal.check` dispatches on this kind."""

PHASE_ARG: Final[str] = "_phase"
PHASE_CHECK: Final[str] = "check"
PHASE_STEP: Final[str] = "step"
"""Reserved args key and its values: only this module puts them into the op's args (a caller cannot
send them, `validate_op_args` rejects an unknown key)."""

_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"

_ROW_DROP: Final[tuple[str, ...]] = ("notices", "warnings", "error", "errorCode")


def _item(index: int, env: dict[str, Any], *, unknown: bool) -> dict[str, Any]:
    raw = env.get("result")
    row: dict[str, Any] = (
        {k: v for k, v in raw.items() if k not in _ROW_DROP}
        if isinstance(raw, dict)
        else {"ran": False}
    )
    row["index"] = index
    row["unknown"] = unknown
    row.setdefault("alreadyDisconnected", False)
    if env.get("ok") is not True:
        row["error"] = env.get("error")
    return row


def _is_unknown(env: dict[str, Any]) -> bool:
    if env.get("ok") is True:
        return False
    if env.get("errorCode") in UNKNOWN_END_CODES:
        return True
    raw = env.get("result")
    return isinstance(raw, dict) and raw.get("readBackFailed") is True


def _write_intent(
    rt: Runtime,
    op_name: str,
    group: str,
    selector: dict[str, Any],
    connected_before: int | None,
    step: dict[str, int],
) -> str:
    """Appends the intent of one step (raises OSError when the journal cannot be written) and
    returns its id. `selector` is the segment's stable address. `connectedBefore` is the number of
    other connected segments as last read; the check compares the count read again with it."""
    intent_id = new_id("i")
    rt.journal.append(
        {
            "type": "intent",
            "id": intent_id,
            "group": group,
            "ts": now_iso(),
            "op": op_name,
            "kind": REMOVE_KIND,
            "action": ACTIONS[op_name],
            "args": {"target": selector, "step": step},
            "target": selector,
            "connectedBefore": connected_before,
        }
    )
    return intent_id


def _close_step(
    rt: Runtime, op_name: str, intent_id: str, group: str, env: dict[str, Any], *, unknown: bool
) -> list[str]:
    """Closes one step's intent: an outcome when Flame answered, else (unknown) the intent stays
    open, an `unresolved` note is added and the write guard is shut. Returns the notes to add to
    the envelope's `degraded`."""
    code = env.get("errorCode")
    if unknown:
        rt.guard.block(intent_id, group, op_name, str(code or "UNKNOWN_END"))
        try:
            rt.journal.append(
                {
                    "type": "unresolved",
                    "id": intent_id,
                    "group": group,
                    "ts": now_iso(),
                    "errorCode": code,
                    "error": env.get("error"),
                }
            )
        except OSError:
            pass  # the open intent already says "in doubt"; the guard is what matters
        return []
    raw = env.get("result")
    result: dict[str, Any] = raw if isinstance(raw, dict) else {}
    try:
        rt.journal.append(
            {
                "type": "outcome",
                "id": intent_id,
                "group": group,
                "ts": now_iso(),
                "ok": env.get("ok") is True,
                "applied": [],
                "changedKeys": [],
                "failed": {},
                "errorCode": code,
                "readBack": {"connected": result.get("connected"), "ran": result.get("ran")},
            }
        )
    except OSError as exc:
        rt.guard.block(intent_id, group, op_name, "JOURNAL_OUTCOME_NOT_WRITTEN")
        return [
            f"the outcome could not be journaled ({exc}); the write's answer stands but this "
            "server now refuses writes until journal.check has settled the intent"
        ]
    return []


def _unwritable(rt: Runtime, exc: OSError) -> dict[str, Any]:
    return {
        "ok": False,
        "result": None,
        "errorCode": "POLICY_DENIED",
        "error": (
            f"nothing was written: the journal cannot be written ({exc}), and this server does "
            "not write to the project without an intent on record"
        ),
        "hint": f"Make {rt.journal.directory} writable, then retry.",
    }


def _before_of(raw: Any) -> int | None:
    connected = raw.get("connected") if isinstance(raw, dict) else None
    value = connected.get("before") if isinstance(connected, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


_UNKNOWN_HINT: Final[str] = (
    " It is not known whether the connection was changed: this server refuses every write "
    "until journal.check (group={group!r}) has read segment.connections again{sync}."
)
_SYNC_HINT: Final[str] = (
    " (a sync cannot be judged by a count: journal.check answers diverged; read the connected "
    "segments with segment.connections and segment.info)"
)


def unknown_hint(action: str, group: str) -> str:
    return _UNKNOWN_HINT.format(group=group, sync=_SYNC_HINT if action == "sync" else "")


def _refused_without_address(op: RegisteredOperation, started: float, what: str) -> dict[str, Any]:
    return envelope_mod.from_pre_bridge_error(
        error=(
            f"nothing was written: {what} has no stable address (its sequence has no wiretap id, "
            "or no segment starts at its record_in), so a journal record could not find it again"
        ),
        error_code="PRECONDITION_UNMET",
        op=op,
        duration_ms=elapsed_ms(started),
        hint=None,
    )


def _run_single(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], group: str | None
) -> dict[str, Any]:
    started = time.monotonic()
    action = ACTIONS[op.name]
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
        check = execution.run_op(op, {**op_args, PHASE_ARG: PHASE_CHECK}, run_dir=rt.run_dir)
        if check.get("ok") is not True:
            return check
        raw = check.get("result")
        stable = raw.get("stable") if isinstance(raw, dict) else None
        if not isinstance(stable, dict):
            return _refused_without_address(op, started, "the target")
        group_id = group if group is not None else new_id("g")
        try:
            intent_id = _write_intent(
                rt, op.name, group_id, stable, _before_of(raw), {"index": 0, "of": 1}
            )
        except OSError as exc:
            failure = _unwritable(rt, exc)
            return envelope_mod.from_pre_bridge_error(
                error=failure["error"],
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=elapsed_ms(started),
                hint=failure["hint"],
            )
        env = execution.run_op(op, {**op_args, "target": stable}, run_dir=rt.run_dir)
        unknown = _is_unknown(env)
        degraded = _close_step(rt, op.name, intent_id, group_id, env, unknown=unknown)
        if unknown:
            env["hint"] = (str(env.get("hint") or "") + unknown_hint(action, group_id)).strip()
        if degraded:
            env["degraded"] = [*env.get("degraded", []), *degraded]
        env["journal"] = {"group": group_id, "id": intent_id}
        return env


def run_connection_write(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of the three connection writes: `target` is one journaled step; `targets`
    (remove only) is one step per target."""
    if op_args.get("targets") is None:
        return _run_single(rt, op, op_args, group)
    started = time.monotonic()
    targets: list[dict[str, Any]] = list(op_args["targets"])
    group_id = group if group is not None else new_id("g")
    intent_ids: list[str] = []
    degraded: list[str] = []
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
        check = execution.run_op(
            op, {"targets": targets, PHASE_ARG: PHASE_CHECK}, run_dir=rt.run_dir
        )
        raw = check.get("result")
        if check.get("ok") is not True or not isinstance(raw, dict):
            return check
        initial = [i["connected"]["before"] for i in raw["items"]]
        stables = [i.get("stable") for i in raw["items"]]
        missing = [i for i, address in enumerate(stables) if not isinstance(address, dict)]
        if missing:
            return _refused_without_address(op, started, f"targets[{missing[0]}]")
        body: dict[str, Any] = {
            "action": "remove",
            "requested": len(targets),
            "ran": True,
            "removed": [],
            "notRemoved": [],
            "unknown": [],
            "alreadyDisconnected": [],
            "items": [],
            "notices": list(raw.get("notices") or []),
            "warnings": [],
            "partial": False,
        }
        failed: dict[str, Any] | None = None
        for index in range(len(targets)):
            try:
                intent_id = _write_intent(
                    rt,
                    op.name,
                    group_id,
                    stables[index],
                    initial[index],
                    {"index": index, "of": len(targets)},
                )
            except OSError as exc:
                env = _unwritable(rt, exc)
                body["items"].append(_item(index, env, unknown=False))
                body["notRemoved"] += list(range(index, len(targets)))
                failed = {"index": index, "env": env, "unknown": False}
                break
            intent_ids.append(intent_id)
            env = execution.run_op(
                op, {"targets": [stables[index]], PHASE_ARG: PHASE_STEP}, run_dir=rt.run_dir
            )
            unknown = _is_unknown(env)
            degraded += _close_step(rt, op.name, intent_id, group_id, env, unknown=unknown)
            step = env.get("result")
            for text in (step.get("warnings") or []) if isinstance(step, dict) else []:
                body["warnings"].append(f"targets[{index}]: {text}")
            if env.get("ok") is True and isinstance(step, dict):
                body["items"].append(_item(index, env, unknown=False))
                if step.get("alreadyDisconnected"):
                    body["alreadyDisconnected"].append(index)
                    if initial[index] == 0:
                        body["warnings"].append(
                            f"targets[{index}] was not connected before this call: "
                            "remove_connection() was not called for it"
                        )
                else:
                    body["removed"].append(index)
                continue
            body["items"].append(_item(index, env, unknown=unknown))
            (body["unknown"] if unknown else body["notRemoved"]).append(index)
            body["notRemoved"] += list(range(index + 1, len(targets)))
            failed = {"index": index, "env": env, "unknown": unknown}
            break

    meta = envelope_mod.execution_meta(op)
    removed = len(body["removed"]) + len(body["unknown"])
    partial = (
        {"removed": removed, "notRemoved": len(body["notRemoved"])} if failed and removed else None
    )
    ref = {"group": group_id, "id": intent_ids[0], "ids": intent_ids} if intent_ids else None
    if failed is None:
        return _with_degraded(
            envelope_mod.build_envelope(
                ok=True,
                result=body,
                affected=removed,
                journal=ref,
                execution=meta,
                duration_ms=elapsed_ms(started),
            ),
            degraded,
        )
    body["partial"] = partial is not None
    index = failed["index"]
    env = failed["env"]
    earlier = len(body["removed"])
    reason = f"targets[{index}]: {env.get('error')}"
    if failed["unknown"]:
        reason += " -- the call ran, so its connection may be gone (items[].unknown)"
    hint = (
        "Nothing is retried. Read segment.connections of the targets to see what is connected "
        "now; result.items[].inverse of a removed target presumably connects it again."
    )
    if failed["unknown"]:
        hint += unknown_hint("remove", group_id)
    elif env.get("hint"):
        hint = str(env["hint"])
    return _with_degraded(
        envelope_mod.build_envelope(
            ok=False,
            result=body,
            error=f"stopped at {reason}; {earlier} earlier target(s) were removed",
            error_code=as_error_code(str(env.get("errorCode") or "FLAME_API_ERROR")),
            affected=removed,
            partial=partial,
            journal=ref,
            execution=meta,
            duration_ms=elapsed_ms(started),
            hint_override=hint,
        ),
        degraded,
    )


def _with_degraded(env: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    if notes:
        env["degraded"] = [*env.get("degraded", []), *notes]
    return env


def _count_now(ctx: ServerContext, target: dict[str, Any]) -> tuple[int | None, str | None, str]:
    """`(count, code, message)`: the number of other segments connected to the target now, read
    through `segment.connections`; `count` is None when it could not be read."""
    env = ctx.do(CONNECTIONS_OP, {"target": target, "limit": 1, "offset": 0})
    raw = env.get("result")
    count = raw.get("count") if isinstance(raw, dict) else None
    if env.get("ok") is True and isinstance(count, int):
        return count, None, ""
    return None, str(env.get("errorCode")), str(env.get("error"))


def check_remove_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """Decide what became of a connection write (`action` of the intent: create, remove or sync;
    an intent without one is a remove) that has no outcome. Nothing is written to the project; the
    segment's connections are read again at the address the intent recorded.

    create: more other segments connected than `connectedBefore` -> landed; as many -> not_landed;
    fewer -> diverged. remove: none connected -> landed (it also reads so for a segment that was
    not connected when the step ran); fewer than `connectedBefore`, but not none -> diverged; as
    many or more -> not_landed. `connectedBefore` is the count as last read, which an earlier
    target of the same call may have lowered, so a not_landed remove can read as diverged. sync:
    always diverged, because a count cannot say whether the segments were synced (the answer
    names the count now; read the segments and compare them by hand). A segment that
    cannot be found is diverged; a read that fails leaves the intent unknown."""
    intent = view.intent
    action = intent.get("action", "remove")
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    target = intent.get("target")
    if not isinstance(target, dict):
        entry.update(verdict=VERDICT_UNKNOWN, detail="the intent records no target")
        return entry
    count, code, message = _count_now(ctx, target)
    if count is None and code != _NOT_FOUND_CODE:
        entry.update(verdict=VERDICT_UNKNOWN, detail=f"Flame could not be read ({code}: {message})")
        return entry
    before = intent.get("connectedBefore")
    if count is None:
        verdict, finding = STATE_DIVERGED, "other"
        entry["detail"] = "the segment was not found at its recorded address"
    elif action == "sync":
        verdict, finding = STATE_DIVERGED, "other"
        entry["detail"] = (
            f"a sync cannot be judged from a count ({count} other segment(s) are connected now, "
            f"{before} were): whether the connected segments were synced is not known, read them "
            "with segment.connections and segment.info and compare by hand"
        )
    elif action == "create":
        if isinstance(before, int) and count > before:
            verdict, finding = STATE_LANDED, "after"
        elif isinstance(before, int) and count < before:
            verdict, finding = STATE_DIVERGED, "other"
            entry["detail"] = f"{count} segment(s) are connected; {before} were when the step began"
        else:
            verdict, finding = STATE_NOT_LANDED, "before"
            entry["detail"] = f"{count} segment(s) are connected, as before"
    elif count == 0:
        verdict, finding = STATE_LANDED, "after"
    elif isinstance(before, int) and count < before:
        verdict, finding = STATE_DIVERGED, "other"
        entry["detail"] = (
            f"{count} segment(s) are still connected; {before} were when the step began"
        )
    else:
        verdict, finding = STATE_NOT_LANDED, "before"
        entry["detail"] = f"{count} segment(s) are still connected"
    per_key = {"connected": finding}
    current = {"connected": count}
    entry.update(verdict=verdict, perKey=per_key, current=current)
    ctx.journal.append(
        {
            "type": "check",
            "id": view.id,
            "group": view.group,
            "ts": now_iso(),
            "verdict": verdict,
            "perKey": per_key,
            "current": current,
            "target": target,
        }
    )
    ctx.guard.release(view.id)
    return entry
