"""`journal` category (thread=server): list / show / check / revert over the write journal.

These ops run on the server (`server_handler`), never in Flame. When they need Flame they
call an ordinary op through `ctx.do` -- so the read of the current values goes through the
same policy, validation and envelope as any other read, and the write of a revert goes
through the same journaled path (pre-read, intent, compare-and-swap, outcome) as any other
write.

  check   decides, for every intent that has no outcome, whether its write landed by reading
          Flame again -- and writes nothing to the project (recovery after a crash is never
          automatic). Its finding is appended to the journal as a `check`.
  revert  puts the previous values back for the keys a group applied, but only while each
          still holds what the group left; otherwise it refuses and reports the current values.
          A group of a checkpoint intent (an `eval.run` destructive run, a checkpoint restore, a
          `batch.load_setup`, an op that declares a `CheckpointSpec`: checkpointed.py)
          is put back by restoring its checkpoint, but only while the Batch group's fingerprint
          is still the one the run left (`force` overrides, and is journaled). The same for a
          group whose intent is of a sequence / clip (`targetKind: "timeline"`), with the
          Timeline fingerprint and `timeline_restore`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from flame_mcp import transport
from flame_mcp.checkpointed import RESTORE_SIDE_EFFECTS
from flame_mcp.checkpoints import TIMELINE_KIND, fingerprint_of, kind_named, read_fingerprint
from flame_mcp.clip_order import CLIP_ORDER_NOTE
from flame_mcp.desktop_delete_run import (
    check_delete_intent,
    is_delete_intent,
    revert_delete_group,
)
from flame_mcp.desktop_media_run import check_media_intent, is_media_intent, revert_media_group
from flame_mcp.desktop_revert import check_desktop_intent, revert_desktop_group
from flame_mcp.errors import as_error_code
from flame_mcp.journal import (
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    STATE_REVERTED,
    STATE_UNKNOWN,
    VERDICT_UNKNOWN,
    Index,
    IntentView,
    new_id,
    now_iso,
    values_equal,
)
from flame_mcp.newer_groups import NEWER_GROUPS_KEY, newer_groups, newer_groups_sentence
from flame_mcp.ops._common import offset_param
from flame_mcp.registry import Registry
from flame_mcp.render_check import check_render_intent, poll_handle
from flame_mcp.render_run import RENDER_KIND
from flame_mcp.restore import restore_checkpoint
from flame_mcp.runtime import ServerContext
from flame_mcp.segment_extract_run import (
    check_extract_intent,
    is_extract_intent,
    revert_extract_group,
)
from flame_mcp.timeline_create_run import (
    check_sequence_intent,
    is_sequence_intent,
    revert_sequence_group,
)
from flame_mcp.timeline_restore import (
    RESTORE_NOTE,
    check_timeline_intent,
    revert_timeline_group,
)
from flame_mcp.types import (
    Category,
    ErrorCode,
    OperationParam,
    OperationSpec,
    ServerOpError,
    ServerResult,
)

CATEGORY = Category(
    name="journal",
    description=(
        "The write journal: lists, checks, tells apart and reverts the records of writes "
        "(intent, compare-and-swap write, outcome). It runs inside the server and calls "
        "ordinary ops when it needs Flame."
    ),
    default_thread="server",
    default_pacing="none",
    allowed_thread=frozenset({"server"}),
    allowed_pacing=frozenset({"none"}),
)

# The one read failure that says something about the target (it is not there); every other
# failure means Flame was not read, so an intent stays undecided rather than being guessed at.
_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"
_HANDLE_FORM: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
_INFLIGHT_DAYS: Final[int] = int(transport.INFLIGHT_MAX_AGE_S // 86400)
# Keys of a write that landed vs did not, from the key's current value against the intent.
_KEY_BEFORE: Final[str] = "before"
_KEY_AFTER: Final[str] = "after"
_KEY_BOTH: Final[str] = "both"
_KEY_OTHER: Final[str] = "other"


@dataclass(frozen=True)
class CurrentRead:
    """The current values of an intent's keys, read from Flame through the intent's read op."""

    status: str  # "ok" | "not_found" | "unreachable"
    values: dict[str, Any]
    target: dict[str, Any] | None
    code: str | None
    message: str | None


def read_current(ctx: ServerContext, intent: Mapping[str, Any], keys: list[str]) -> CurrentRead:
    """Read `keys` of the intent's target through its read op. The target is tried at the
    address recorded before the write and, when the write renames the object itself (a node
    name), at the address it would have afterwards."""
    op = ctx.registry.get(str(intent.get("op")))
    read_op = intent.get("readOp") or (op.journal.read_op if op and op.journal else None)
    if not isinstance(read_op, str):
        return CurrentRead("unreachable", {}, None, "INVALID_ARGS", "the intent names no read op")
    candidates = [
        t for t in (intent.get("target"), intent.get("targetAfter")) if isinstance(t, dict)
    ]
    for target in candidates:
        env = ctx.do(read_op, {"target": target, "keys": keys})
        if env.get("ok") is not True:
            code = str(env.get("errorCode"))
            if code == _NOT_FOUND_CODE:
                continue
            return CurrentRead("unreachable", {}, None, code, str(env.get("error")))
        attributes = (env.get("result") or {}).get("attributes")
        values: dict[str, Any] = {}
        for key in keys:
            entry = attributes.get(key) if isinstance(attributes, dict) else None
            if not isinstance(entry, dict) or "error" in entry or "value" not in entry:
                detail = entry.get("error") if isinstance(entry, dict) else "missing"
                return CurrentRead(
                    "unreachable", {}, None, "FLAME_API_ERROR", f"{key!r} unreadable: {detail}"
                )
            values[key] = entry["value"]
        return CurrentRead("ok", values, target, None, None)
    return CurrentRead("not_found", {}, None, _NOT_FOUND_CODE, "the target was not found")


def classify(intent: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """(verdict, per-key finding): `not_landed` when every key still holds its previous
    value, `landed` when every key holds what was to be written, `diverged` otherwise."""
    before = intent.get("before") or {}
    after = intent.get("after") or {}
    per_key: dict[str, str] = {}
    all_before = True
    all_after = True
    for key, previous in before.items():
        is_before = values_equal(current.get(key), previous)
        is_after = values_equal(current.get(key), after.get(key))
        all_before &= is_before
        all_after &= is_after
        if is_before and is_after:
            per_key[key] = _KEY_BOTH
        elif is_before:
            per_key[key] = _KEY_BEFORE
        elif is_after:
            per_key[key] = _KEY_AFTER
        else:
            per_key[key] = _KEY_OTHER
    if all_before:
        return STATE_NOT_LANDED, per_key
    if all_after:
        return STATE_LANDED, per_key
    return STATE_DIVERGED, per_key


def _list(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    limit = int(args["limit"])
    offset = int(args["offset"])
    read = ctx.journal.read_all()
    index = Index(read.records)
    groups = index.newest_first()
    page = groups[offset : offset + limit]
    return ServerResult(
        result={
            "total": len(groups),
            "offset": offset,
            "shown": len(page),
            "truncated": offset + len(page) < len(groups),
            "items": [index.group_summary(g) for g in page],
            "skippedLines": read.skipped_lines,
        }
    )


def _show(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    group = str(args["group"])
    read = ctx.journal.read_all()
    index = Index(read.records)
    if group not in index.groups:
        raise ServerOpError("INVALID_ARGS", f"no journal group {group!r}")
    ids = {view.id for view in index.groups[group]}
    records = [
        r
        for r in read.records
        if r.get("group") == group
        and (r.get("type") in ("group", "checkpoint") or str(r.get("id")) in ids)
    ]
    return ServerResult(
        result={
            **index.group_summary(group),
            "records": records,
            "revertGroups": index.revert_groups_of(group),
            "skippedLines": read.skipped_lines,
        }
    )


def _intent_of_handle(index: Index, handle: str) -> IntentView | None:
    """The intent that owns `handle` (the pending handle a run ended with, or the client handle
    the server chose for it), None when no intent is tied to it."""
    for view in index.intents.values():
        if handle in (view.pending_handle, view.client_handle):
            return view
    return None


def _poll(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    handle = str(args["handle"])
    if _HANDLE_FORM.fullmatch(handle) is None:
        raise ServerOpError(
            "INVALID_ARGS",
            f"handle {handle!r} is not a bridge handle (32 lowercase hexadecimal characters): "
            "nothing was asked of the bridge",
        )
    owner = _intent_of_handle(Index(ctx.journal.read_all().records), handle)
    if owner is not None and owner.outcome is not None:
        raise ServerOpError(
            "INVALID_ARGS",
            f"handle {handle} belongs to journal group {owner.group}, which is already decided: "
            "there is no result left to take (journal.show(group) has what was recorded). "
            "Nothing was asked of the bridge.",
        )
    if owner is not None:
        raise ServerOpError(
            "INVALID_ARGS",
            f"handle {handle} belongs to journal group {owner.group}: its result can be handed "
            f"over only once, and journal.check(group={owner.group!r}) is what records it. "
            "Nothing was asked of the bridge.",
        )
    polled = poll_handle(ctx, handle)
    if polled.kind == "unknown_handle":
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"the bridge does not know handle {handle}: Flame was restarted since the call, or "
            "its result was already handed over",
        )
    if polled.kind == "unreachable":
        raise ServerOpError(
            as_error_code(polled.code),
            f"the bridge could not be asked about handle {handle}: {polled.message}",
        )
    if polled.kind == "running" or polled.br is None:
        return ServerResult(
            result={"state": "pending", "handle": handle, "ok": None, "result": None}
        )
    br = polled.br
    return ServerResult(
        result={
            "state": "done",
            "handle": handle,
            "ok": br.ok,
            "result": br.result,
            "errorCode": br.error_code,
            "error": br.error,
            "traceback": br.traceback,
            "queuedMs": br.queued_ms,
            "runMs": br.run_ms,
        }
    )


_INFLIGHT_KEYS: Final[tuple[str, ...]] = (
    "id",
    "label",
    "thread",
    "timeoutS",
    "sentAt",
    "socket",
    "bridgePid",
    "handle",
    "codeSha256",
)


def _inflight_row(item: transport.InflightEntry) -> dict[str, Any]:
    row = {key: item.record.get(key) for key in _INFLIGHT_KEYS}
    code = item.record.get("code")
    row["codeChars"] = len(code) if isinstance(code, str) else None
    row["elapsedS"] = round(item.elapsed_s, 1) if item.elapsed_s is not None else None
    return row


def _inflight(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    limit = int(args["limit"])
    offset = int(args["offset"])
    listing = transport.read_inflight_records(ctx.run_dir)
    page = listing.items[offset : offset + limit]
    return ServerResult(
        result={
            "total": listing.total,
            "offset": offset,
            "shown": len(page),
            "truncated": offset + len(page) < len(listing.items),
            "unreadable": listing.unreadable,
            "items": [_inflight_row(item) for item in page],
        }
    )


def _check_fingerprint_one(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """An eval / restore intent with no outcome: compare the Batch group's fingerprint now with
    the one before the run and, for a restore, the one it was to reach. Equal to the one
    before -> `not_landed` (the structure is unchanged; what the fingerprint cannot see --
    attribute values, positions -- cannot be ruled out); the expected one -> `landed`; anything
    else -> `diverged`."""
    intent = view.intent
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    env = read_fingerprint(ctx, intent.get("target") or {})
    current = fingerprint_of(env)
    if current is None and env.get("errorCode") != _NOT_FOUND_CODE:
        entry.update(
            verdict=VERDICT_UNKNOWN,
            detail=f"Flame could not be read ({env.get('errorCode')}: {env.get('error')})",
        )
        return entry
    before = intent.get("fingerprintBefore")
    expected = intent.get("fingerprintExpected")
    if current is None:
        verdict, finding = STATE_DIVERGED, _KEY_OTHER
        entry["detail"] = "the Batch group was not found"
    elif expected is not None and current == expected:
        verdict, finding = STATE_LANDED, _KEY_AFTER
    elif current == before:
        verdict, finding = STATE_NOT_LANDED, _KEY_BEFORE
        entry["detail"] = (
            "the group's nodes and wiring are as before the run; effects the fingerprint "
            "does not see (attribute values, positions) cannot be ruled out"
        )
    else:
        verdict, finding = STATE_DIVERGED, _KEY_OTHER
    per_key = {"fingerprint": finding}
    entry.update(verdict=verdict, perKey=per_key, current={"fingerprint": current})
    ctx.journal.append(
        {
            "type": "check",
            "id": view.id,
            "group": view.group,
            "ts": now_iso(),
            "verdict": verdict,
            "perKey": per_key,
            "current": {"fingerprint": current},
            "target": intent.get("target"),
        }
    )
    ctx.guard.release(view.id)
    return entry


def _check_one(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    intent = view.intent
    if intent.get("kind") == RENDER_KIND:
        return check_render_intent(ctx, view)
    if view.desktop_kind is not None:
        if is_delete_intent(view):
            return check_delete_intent(ctx, view)
        if is_media_intent(view):
            return check_media_intent(ctx, view)
        if is_extract_intent(view):
            return check_extract_intent(ctx, view)
        if is_sequence_intent(view):
            return check_sequence_intent(ctx, view)
        return check_desktop_intent(ctx, view)
    if intent.get("fingerprintBefore") is not None:
        if kind_named(intent.get("targetKind")) is TIMELINE_KIND:
            return check_timeline_intent(ctx, view)
        return _check_fingerprint_one(ctx, view)
    keys = [str(k) for k in (intent.get("before") or {})]
    current = read_current(ctx, intent, keys)
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    if current.status == "unreachable":
        entry.update(
            verdict=VERDICT_UNKNOWN,
            detail=f"Flame could not be read ({current.code}: {current.message})",
        )
        return entry
    if current.status == "not_found":
        verdict = STATE_DIVERGED
        per_key: dict[str, str] = {}
        values: dict[str, Any] = {}
        entry["detail"] = "the target was not found at its recorded address"
    else:
        verdict, per_key = classify(intent, current.values)
        values = current.values
    entry.update(verdict=verdict, perKey=per_key, current=values)
    ctx.journal.append(
        {
            "type": "check",
            "id": view.id,
            "group": view.group,
            "ts": now_iso(),
            "verdict": verdict,
            "perKey": per_key,
            "current": values,
            "target": current.target,
        }
    )
    ctx.guard.release(view.id)
    return entry


def _check(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    group = args.get("group")
    with ctx.write_lock:  # never classify a write that is still on its way
        index = Index(ctx.journal.read_all().records)
        if group is not None and group not in index.groups:
            raise ServerOpError("INVALID_ARGS", f"no journal group {group!r}")
        pending = index.in_doubt(str(group) if group is not None else None)
        checked = [_check_one(ctx, view) for view in pending]
    undecided = [c for c in checked if c["verdict"] == VERDICT_UNKNOWN]
    return ServerResult(
        result={
            "inDoubt": len(pending),
            "checked": checked,
            "stillUnknown": len(undecided),
            "writesBlocked": bool(ctx.guard.blocked()),
        },
        affected=len(checked) - len(undecided),
    )


@dataclass(frozen=True)
class _RevertPlan:
    view: IntentView
    keys: list[str]
    target: dict[str, Any]
    expected: dict[str, Any]


def _nothing_to_revert(index: Index, group: str) -> ServerOpError:
    if any(view.intent.get("kind") == RENDER_KIND for view in index.groups[group]):
        return ServerOpError(
            "PRECONDITION_UNMET",
            f"group {group} is a render: its output (the files on disk, the clips in the shelf "
            "reel) cannot be undone, so there is nothing to revert",
        )
    state = index.group_state(group)
    if state == STATE_REVERTED:
        by = ", ".join(index.reverted_by(group))
        return ServerOpError(
            "PRECONDITION_UNMET", f"group {group} is already reverted (by {by}); nothing to do"
        )
    if state == STATE_UNKNOWN:
        return ServerOpError(
            "PRECONDITION_UNMET",
            f"group {group} has no outcome yet (state unknown): run journal.check first",
        )
    return ServerOpError(
        "PRECONDITION_UNMET",
        f"group {group} is in state {state!r}: it applied no key that a revert could put back",
    )


def _plan_revert(ctx: ServerContext, index: Index, group: str) -> list[_RevertPlan]:
    """Read Flame for every key the group applied (and that no earlier, partial revert already
    put back) and require each to still hold what the group left. Raises before anything is
    written. Intents are taken in reverse order of the group."""
    views = [
        v
        for v in reversed(index.groups[group])
        if v.revertible_keys() and not index.intent_reverted(v)
    ]
    if not views:
        raise _nothing_to_revert(index, group)
    plans: list[_RevertPlan] = []
    refused: list[dict[str, Any]] = []
    for view in views:
        keys = view.revertible_keys()
        current = read_current(ctx, view.intent, keys)
        if current.status == "unreachable":
            raise ServerOpError(
                as_error_code(current.code),
                f"nothing was written: Flame could not be read ({current.message})",
            )
        if current.status == "not_found" or current.target is None:
            raise ServerOpError(
                "SELECTOR_EMPTY",
                f"nothing was written: the target of {view.id} was not found at its recorded "
                "address",
            )
        expected = {key: view.expected_after(key) for key in keys}
        per_key = {
            key: {
                "expected": expected[key],
                "current": current.values[key],
                "match": values_equal(current.values[key], expected[key]),
            }
            for key in keys
        }
        if not all(entry["match"] for entry in per_key.values()):
            refused.append({"id": view.id, "op": view.intent.get("op"), "keys": per_key})
        plans.append(_RevertPlan(view, keys, current.target, expected))
    if refused:
        raise ServerOpError(
            "CONFLICT",
            "nothing was written: the current value of "
            + ", ".join(k for r in refused for k, e in r["keys"].items() if not e["match"])
            + " is not what the group left, so it was not touched (it is not reverted "
            "automatically: decide from the current values)",
            result={"refused": True, "intents": refused},
        )
    return plans


def _revert_by_checkpoint(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView], force: bool
) -> ServerResult:
    """Revert a group whose intent is a checkpoint intent (an eval run, a restore): restore
    the checkpoint it names, but only while the Batch group's fingerprint is the one the run
    left -- someone changing the group since would be overwritten. `force` skips that
    comparison; when it really overrides one, the restore's intent records it (`forced`)."""
    view = views[0]
    if len(views) != 1 or not view.is_revertible():
        raise _nothing_to_revert(index, group)
    if kind_named(view.intent.get("targetKind")) is TIMELINE_KIND:
        return revert_timeline_group(ctx, index, group, view, force)
    checkpoint_id = view.revert_checkpoint()
    assert checkpoint_id is not None  # views were picked by it
    if checkpoint_id not in index.checkpoints:
        raise ServerOpError(
            "INVALID_ARGS",
            f"nothing was restored: the journal has no record of checkpoint {checkpoint_id}",
        )
    env = read_fingerprint(ctx, view.intent.get("target") or {})
    current = fingerprint_of(env)
    if current is None:
        raise ServerOpError(
            as_error_code(str(env.get("errorCode"))),
            f"nothing was restored: the Batch group could not be read ({env.get('error')})",
        )
    expected = view.fingerprint_after()
    matches = expected is not None and current == expected
    forced: dict[str, Any] | None = None
    if not matches:
        detail = {
            "expectedFingerprint": expected,
            "currentFingerprint": current,
            "note": (
                "the run recorded no fingerprint of its result"
                if expected is None
                else "the group's nodes or wiring changed after the run"
            ),
        }
        if not force:
            raise ServerOpError(
                "CONFLICT",
                f"nothing was restored: {detail['note']}, so restoring checkpoint "
                f"{checkpoint_id} would overwrite whatever happened since (pass force=true to "
                "restore anyway; that is journaled)",
                result={"refused": True, "group": group, **detail},
            )
        forced = {"used": True, **detail}
    outcome = restore_checkpoint(
        ctx, checkpoint_id, reverts=group, reverts_intent=view.id, forced=forced
    )
    result = dict(outcome.result) if isinstance(outcome.result, dict) else {}
    result["reverted"] = group
    return ServerResult(result=result, affected=outcome.affected, journal=outcome.journal)


def _with_newer_groups(
    ctx: ServerContext, index: Index, group: str, error: ServerOpError
) -> ServerOpError:
    """A CONFLICT of a checkpointed group, with the later groups of the same target that were not
    reverted (`result.newerGroups`, newest first) and the advice to put those back first. Any other
    error, and a CONFLICT with no such group, is returned as it was."""
    if error.code != "CONFLICT":
        return error
    rows = newer_groups(ctx.journal.read_all().records, index, group)
    if not rows:
        return error
    body = dict(error.result) if isinstance(error.result, dict) else {}
    body[NEWER_GROUPS_KEY] = rows
    message = error.message.rstrip()
    return ServerOpError(
        error.code,
        message + ("" if message.endswith(".") else ".") + newer_groups_sentence(rows),
        result=body,
        hint=error.hint,
        affected=error.affected,
        undo_steps=error.undo_steps,
        partial=error.partial,
        journal=error.journal,
    )


def _revert(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    group = str(args["group"])
    with ctx.write_lock:
        index = Index(ctx.journal.read_all().records)
        if group not in index.groups:
            raise ServerOpError("INVALID_ARGS", f"no journal group {group!r}")
        if index.is_reverted(group):
            raise _nothing_to_revert(index, group)
        desktop_views = [v for v in index.groups[group] if v.desktop_kind is not None]
        if desktop_views:
            if any(is_delete_intent(v) for v in desktop_views):
                return revert_delete_group(ctx, index, group, desktop_views)
            if any(is_media_intent(v) for v in desktop_views):
                return revert_media_group(ctx, index, group, desktop_views)
            if any(is_extract_intent(v) for v in desktop_views):
                return revert_extract_group(ctx, index, group, desktop_views)
            if any(is_sequence_intent(v) for v in desktop_views):
                return revert_sequence_group(ctx, index, group, desktop_views)
            return revert_desktop_group(ctx, index, group, desktop_views)
        checkpoint_views = [v for v in index.groups[group] if v.revert_checkpoint() is not None]
        if checkpoint_views:
            try:
                return _revert_by_checkpoint(
                    ctx, index, group, checkpoint_views, bool(args.get("force", False))
                )
            except ServerOpError as exc:
                raise _with_newer_groups(ctx, index, group, exc) from exc
        plans = _plan_revert(ctx, index, group)

        new_group = new_id("g")
        journal_ref = {"group": new_group}
        writes: list[dict[str, Any]] = []
        reverted: list[dict[str, Any]] = []

        def remaining_from(start: int) -> list[dict[str, Any]]:
            return [
                {"intent": p.view.id, "op": p.view.intent.get("op"), "keys": p.keys}
                for p in plans[start:]
            ]

        for position, plan in enumerate(plans):
            op_name = str(plan.view.intent.get("op"))
            op = ctx.registry.get(op_name)
            failure: tuple[ErrorCode, str, str | None] | None = None
            if op is None or op.journal is None:
                failure = (
                    "PRECONDITION_UNMET",
                    f"'{op_name}' is no longer a journaled op",
                    None,
                )
            else:
                before = plan.view.intent.get("before") or {}
                env = ctx.do(
                    op_name,
                    op.journal.write_args(plan.target, {key: before[key] for key in plan.keys}),
                    group=new_group,
                    reverts=group,
                    reverts_intent=plan.view.id,
                    precondition=plan.expected,
                )
                writes.append(
                    {
                        "intent": plan.view.id,
                        "ok": env.get("ok"),
                        "errorCode": env.get("errorCode"),
                        "journal": env.get("journal"),
                        "result": env.get("result"),
                    }
                )
                if env.get("ok") is True:
                    reverted.append({"intent": plan.view.id, "op": op_name, "keys": plan.keys})
                    continue
                failure = (
                    as_error_code(str(env.get("errorCode"))),
                    f"the revert of {plan.view.id} failed: {env.get('error')}",
                    env.get("hint"),
                )
            assert failure is not None  # every path that reaches here set it
            code, message, hint = failure
            still = remaining_from(position)
            raise ServerOpError(
                code,
                message
                + (
                    f" -- {len(reverted)} intent(s) were put back, {len(still)} remain "
                    "(the group is now partially_reverted; decide from the current values, "
                    "nothing is retried automatically)"
                    if reverted
                    else ""
                ),
                result={
                    "group": new_group,
                    "reverts": group,
                    "writes": writes,
                    "revertedKeys": reverted,
                    "remaining": still,
                },
                hint=hint,
                affected=sum(len(r["keys"]) for r in reverted),
                partial={"done": len(reverted), "failed": 1, "notRun": len(still) - 1},
                journal=journal_ref,
            )
    return ServerResult(
        result={
            "reverted": group,
            "group": new_group,
            "writes": writes,
            "revertedKeys": reverted,
            "remaining": [],
        },
        affected=sum(len(p.keys) for p in plans),
        journal=journal_ref,
    )


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)
    limit_param = OperationParam(
        name="limit", type="number", description="Max groups to return.", required=False
    )
    group_param = OperationParam(
        name="group",
        type="string",
        description="A journal group id (from journal.list, or a write's envelope `journal`).",
        required=True,
        non_empty=True,
    )

    registry.register(
        OperationSpec(
            name="journal.list",
            category="journal",
            description=(
                "List the journal's groups, newest first: id, time, op names, state and any "
                "revert relation. state: landed (the write took effect), not_landed (a check "
                "found nothing written), failed, partial (a check found only some of a render's "
                "frames, or a render came out short), conflict (a compare-and-swap found "
                "the value changed and wrote nothing), unknown (no outcome and not checked yet), "
                "diverged (a check found neither the old nor the new value), reverted. "
                "One page at a time (limit / offset)."
            ),
            effect="read",
            params=(limit_param, offset_param()),
            server_handler=_list,
        )
    )
    registry.register(
        OperationSpec(
            name="journal.show",
            category="journal",
            description=(
                "Every record of one journal group (intent / outcome / unresolved / check), "
                "with its state."
            ),
            effect="read",
            params=(group_param,),
            server_handler=_show,
        )
    )
    registry.register(
        OperationSpec(
            name="journal.check",
            category="journal",
            description=(
                "Decide what became of every intent that has no outcome (all of them, or those "
                "of one group): Flame is READ again (nothing is written) and each key compared "
                "with the previous value and the value that was to be written. Verdict "
                "not_landed (all previous), landed (all new), diverged (neither: someone else "
                "changed it, or only some keys landed -- see perKey), or unknown when Flame could "
                "not be reached (the intent stays open). An open render.batch intent whose "
                "wait ended with a bridge pending handle is decided by asking the bridge for "
                "that handle's result first (the answer, not the files, says the render has "
                "ended; the result is handed over once and journaled at once), then counting "
                "the planned output files: landed / partial (some frames) / failed (the "
                "render reported a failure), basis 'handle'. A render still running, or a "
                "bridge that does not answer, is unknown (the door stays shut: retry later). "
                "Without a handle, or with one the bridge no longer knows, the files alone "
                "decide (landed = every planned frame written since the intent, partial = "
                "some, not_landed = none; basis 'disk', an estimate: the files can be "
                "complete before render() returns). Each finding is "
                "appended to the journal. "
                "A finding of any kind other than unknown lifts this server's refusal of writes "
                "that followed a write with no answer."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="group",
                    type="string",
                    description="Only the intents of this group. Omit for every undecided intent.",
                    required=False,
                    non_empty=True,
                ),
            ),
            server_handler=_check,
        )
    )
    registry.register(
        OperationSpec(
            name="journal.poll",
            category="journal",
            description=(
                "Ask the bridge for the result of a pending handle. The handle is the "
                "`pending.handle` of a TIMEOUT whose call was still running: this is how a read "
                "that ran past its timeout is finished (a write: journal.check). state 'done': "
                "`result` is the "
                "bridge's raw answer of the call (ok / errorCode / error / traceback beside it); "
                "'pending': the call is still running, ask again later. The bridge hands a "
                "result over once, and this op records nothing in the journal. A handle Flame "
                "does not know (restarted since, or already handed over) is PRECONDITION_UNMET. "
                "A handle that belongs to a journal intent (a render's pending or client handle) "
                "is INVALID_ARGS and the bridge is not asked: journal.check(group) takes that "
                "result and journals it."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="handle",
                    type="string",
                    description=(
                        "The pending handle (`pending.handle` of the TIMEOUT envelope): "
                        "32 lowercase hex characters."
                    ),
                    required=True,
                    non_empty=True,
                ),
            ),
            server_handler=_poll,
        )
    )
    registry.register(
        OperationSpec(
            name="journal.inflight",
            category="journal",
            description=(
                "List the bridge calls the server still holds a record for, newest first. "
                "They are the files of run/inflight/; each item is the record as written (id, "
                "label, thread, timeoutS, sentAt, socket, bridgePid, handle, codeSha256) plus "
                "elapsedS since it was sent and codeChars, the length of the generated script "
                "(the script itself is not returned: it runs to hundreds of kilobytes). A call "
                "that ended normally leaves no record; one that "
                "is still running, lost its answer or outlived a crashed Flame does, until the "
                f"server's startup sweep removes records older than {_INFLIGHT_DAYS} days. total "
                "counts the record files, unreadable the ones that do not parse (they are not "
                "listed). Nothing is removed. One page at a time (limit / offset)."
            ),
            effect="read",
            params=(limit_param, offset_param()),
            server_handler=_inflight,
        )
    )
    registry.register(
        OperationSpec(
            name="journal.revert",
            category="journal",
            description=(
                "Undo the writes of one journal group (`group`: the id an earlier call returned "
                "in its `journal`). How depends on what the group did. "
                "A desktop.delete group: each stashed copy is copied back into the "
                "thing's old parent (flame.media_panel.copy; a new uid, the end of the reel; a "
                "sequence that was shown in the Timeline is opened again); a thing whose old "
                "parent is gone, or whose stashed copy is gone or changed, stays a CONFLICT "
                "while the others are put back (result.partial), and a second revert puts back "
                "only what the first one did not. "
                "A segment.copy_to_media_panel / segment.match group: the clip the call "
                "made is deleted by uid (flame.delete), only while its name, duration and content "
                "(segments and effects, by fingerprint) are still what they were right after the "
                "call, else CONFLICT and nothing is deleted; one that is gone already is "
                "result.alreadyGone. The sequence is never touched. "
                "A timeline.create_sequence group: the sequence the call made is deleted by "
                "uid, only while its name, duration and content are still what they were right "
                "after the call, else CONFLICT and nothing is deleted; when it was the one the "
                "Timeline showed, the sequence shown before the call is opened again "
                "(result.reshown). "
                "A mediahub.import / desktop.duplicate / desktop.move group: an import's "
                "clips and a duplicate are deleted by uid, only while each still has the name, "
                "duration and content (segments and effects, by fingerprint) it was made with "
                "(a reel: name and number of entries), else CONFLICT and nothing is deleted; "
                "one already gone is result.skipped (alreadyGone for a duplicate); a move is "
                "undone by moving the things back into their old parents (also out of a library: "
                "it undoes this server's own move), only when every one of "
                "them is still in the destination and every old parent exists, else CONFLICT and "
                "nothing is moved -- the things then get new uids again and go to the end of the "
                "parent (result.reverted[].to has the new selectors). "
                "A desktop.create / desktop.copy group: delete what it made (flame.delete). A "
                "create only while it is still as created (a Batch group: no node, every reel and "
                "shelf reel empty and named as created; a reel group: every reel empty; a reel: "
                "empty), else CONFLICT and nothing is deleted; a copy's clips are deleted by uid, "
                "only while each still has the name, duration and content it had right after "
                "the call, else CONFLICT and nothing is deleted (one already gone is "
                "result.skipped). A record kept without a content fingerprint cannot be checked "
                "and is a CONFLICT. Deleting a "
                "Batch group does not make the "
                "previously current group current again (result.current says which is). Journaled "
                "as a group of its own (`reverts` = this one), which is not itself revertible; "
                "`force` does not apply. "
                "Groups of a checkpointed write (a destructive eval.run, batch.load_setup, "
                "journal.restore_checkpoint, batch.create_reel / add_clips / organize; "
                "batch.create_shelf_reel and batch.iterate have a group too, but their shelf reel "
                "and iteration are outside a setup and stay): restore the group's checkpoint, "
                "only while the Batch "
                "group's fingerprint (nodes, wiring, Action inner nodes) is still the one the "
                "run left (else CONFLICT, nothing restored; force=true overrides, journaled); "
                "what the restore does not bring back is in the op's own description. A group "
                "of segment.trim / slip / replace / create_effect / delete_effect / set_effect / "
                "effect_load_setup (a sequence / clip): restore its stash copy, only "
                "while "
                "the sequence's fingerprint (segments and effects) is still the one the call "
                "left (else CONFLICT; force=true overrides, journaled); result.restored is the "
                "restored thing (new uid), result.preCheckpoint the id of the checkpoint that "
                "holds the edited one. A plan.run that was bundled around a Timeline write "
                "is one such group: one restore puts back the whole plan, its "
                "segment.set_attributes steps included. A checkpoint whose stashed copy was "
                "purged (journal.purge_checkpoints) cannot be restored (PRECONDITION_UNMET). "
                + RESTORE_NOTE
                + " Other groups: put back the "
                "previous values of the keys the group applied, each only if it still holds "
                "what the group left (compare-and-swap, checked twice); if any differs nothing "
                "is written and the current values are reported (CONFLICT) -- a value changed "
                "by someone else is never overwritten automatically. An already reverted group, "
                "or one that applied nothing, is refused. Journaled as a new group (`reverts` "
                "= this one), itself revertible. A CONFLICT of a checkpointed group lists the "
                "later landed groups of the same target that were not reverted in "
                "result.newerGroups [{group, op, ts}], newest first: revert those first. "
                "Refused while a write is unsettled "
                "(journal.check). A revert of a checkpointed group returns result.reelsRestored / "
                "result.reelTidy (the reel layout put back after the load). Restoring a "
                "checkpoint has side effects on reels: "
                + RESTORE_SIDE_EFFECTS
                + " "
                + CLIP_ORDER_NOTE
            ),
            effect="reversible",
            params=(
                group_param,
                OperationParam(
                    name="force",
                    type="boolean",
                    description=(
                        "Eval / restore groups only: restore even though the Batch group's "
                        "fingerprint is not the one the run left. Journaled. Restoring this way "
                        "also loses the nodes, reels and reel clips added to the group after the "
                        "checkpoint (load_setup does not create a reel the setup lacks); for a "
                        "sequence it loses whatever was edited after the run."
                    ),
                    required=False,
                    default=False,
                ),
            ),
            server_handler=_revert,
            undo_steps=lambda args, affected: 0,
        )
    )
