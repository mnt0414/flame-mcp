"""`journal` category (thread=server): list / show / check / revert over the write journal.

These ops run on the server (`server_handler`), never in Flame. When they need Flame they
call an ordinary op through `ctx.do` -- so the read of the current values goes through the
same policy, validation and envelope as any other read, and the write of a revert goes
through the same journaled path (pre-read, intent, compare-and-swap, outcome) as any other
write.

  check   decides, for every intent that has no outcome, whether its write landed by reading
          Flame again -- and writes nothing to the project (recovery after a crash is never
          automatic). Its finding is appended to the journal as a `check`.
  revert  puts back what a group wrote, by the kind of the group (`journal_revert`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import journal_revert, transport
from flame_mcp.checkpointed import RESTORE_SIDE_EFFECTS
from flame_mcp.checkpoints import (
    TIMELINE_KIND,
    fingerprint_of,
    kind_named,
    read_fingerprint,
)
from flame_mcp.clip_order import CLIP_ORDER_NOTE
from flame_mcp.desktop_revert import check_desktop_intent
from flame_mcp.errors import as_error_code
from flame_mcp.journal import (
    LOOP_GROUP_KIND,
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    Index,
    IntentView,
    attribute_values_equal,
    now_iso,
)
from flame_mcp.journal_records import CheckRecord
from flame_mcp.journal_revert import NOT_FOUND_CODE, read_current
from flame_mcp.ops._common import limit_param, offset_param
from flame_mcp.registry import Registry
from flame_mcp.render_check import poll_handle
from flame_mcp.runtime import ServerContext
from flame_mcp.timeline_purge import settle_unrecorded
from flame_mcp.timeline_restore import (
    RESTORE_NOTE,
    check_timeline_intent,
)
from flame_mcp.types import (
    Category,
    OperationParam,
    OperationSpec,
    PlanKind,
    PlanUse,
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

_HANDLE_FORM: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
_INFLIGHT_DAYS: Final[int] = int(transport.INFLIGHT_MAX_AGE_S // 86400)
# Keys of a write that landed vs did not, from the key's current value against the intent.
_KEY_BEFORE: Final[str] = "before"
_KEY_AFTER: Final[str] = "after"
_KEY_BOTH: Final[str] = "both"
_KEY_OTHER: Final[str] = "other"


def classify(intent: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """(verdict, per-key finding): `not_landed` when every key still holds its previous
    value, `landed` when every key holds what was to be written, `diverged` otherwise."""
    before = intent.get("before") or {}
    after = intent.get("after") or {}
    per_key: dict[str, str] = {}
    all_before = True
    all_after = True
    for key, previous in before.items():
        is_before = attribute_values_equal(key, current.get(key), previous)
        is_after = attribute_values_equal(key, current.get(key), after.get(key))
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
    if index.is_loop_parent(group):
        return _show_loop(index, group, read.skipped_lines)
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


def _show_loop(index: Index, parent: str, skipped_lines: int) -> ServerResult:
    """A plan.for_each parent: its record and the child groups in the order they ran."""
    children = [
        {
            "group": row.get("group"),
            "key": row.get("key"),
            "op": row.get("op"),
            "step": row.get("step"),
            "revertible": row.get("revertible"),
            "state": index.group_state(str(row.get("group")))
            if str(row.get("group")) in index.groups
            else None,
        }
        for row in index.loop_child_records(parent)
    ]
    return ServerResult(
        result={
            "group": parent,
            "kind": LOOP_GROUP_KIND,
            "label": index.group_meta[parent].get("label"),
            "ts": index.group_meta[parent].get("ts"),
            "children": children,
            "skippedLines": skipped_lines,
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
    if current is None and env.get("errorCode") != NOT_FOUND_CODE:
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
    check: CheckRecord = {
        "type": "check",
        "id": view.id,
        "group": view.group,
        "ts": now_iso(),
        "verdict": verdict,
        "perKey": per_key,
        "current": {"fingerprint": current},
        "target": intent.get("target"),
    }
    ctx.journal.append(check)
    ctx.guard.release(view.id)
    return entry


def _is_timeline_fingerprint(view: IntentView) -> bool:
    intent = view.intent
    return intent.get("fingerprintBefore") is not None and (
        kind_named(intent.get("targetKind")) is TIMELINE_KIND
    )


def _check_one(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """By kind (`journal_revert.KINDS`); an intent of no listed kind is a Desktop write, then a
    fingerprint write (a timeline's first), else a value write."""
    spec = journal_revert.KINDS.get(view.effective_kind) if view.effective_kind else None
    if spec is not None:
        return spec.check(ctx, view)
    if view.desktop_kind is not None:
        return check_desktop_intent(ctx, view)
    if _is_timeline_fingerprint(view):
        return check_timeline_intent(ctx, view)
    if view.intent.get("fingerprintBefore") is not None:
        return _check_fingerprint_one(ctx, view)
    return _check_values_one(ctx, view)


def _check_values_one(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    intent = view.intent
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
    check: CheckRecord = {
        "type": "check",
        "id": view.id,
        "group": view.group,
        "ts": now_iso(),
        "verdict": verdict,
        "perKey": per_key,
        "current": values,
        "target": current.target,
    }
    ctx.journal.append(check)
    ctx.guard.release(view.id)
    return entry


def _check(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    group = args.get("group")
    with ctx.write_lock:  # never classify a write that is still on its way
        index = Index(ctx.journal.read_all().records)
        if group is not None and group not in index.groups and not index.is_loop_parent(group):
            raise ServerOpError("INVALID_ARGS", f"no journal group {group!r}")
        pending = index.in_doubt(str(group) if group is not None else None)
        checked = [_check_one(ctx, view) for view in pending]
        unrecorded = settle_unrecorded(ctx, index) if group is None else []
    undecided = [c for c in checked if c["verdict"] == VERDICT_UNKNOWN]
    return ServerResult(
        result={
            "inDoubt": len(pending),
            "checked": checked,
            "unrecorded": unrecorded,
            "stillUnknown": len(undecided) + sum(1 for u in unrecorded if not u["released"]),
            "writesBlocked": bool(ctx.guard.blocked()),
        },
        affected=len(checked) - len(undecided),
    )


_PLAN_USE_JOURNAL_REVERT = PlanUse(
    PlanKind.NEVER,
)


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)
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
                "found nothing written), failed (nothing applied or changed), partial (a write "
                "applied or changed some of its keys but not all as asked; a check found only "
                "some of a render's frames, or a render came out short), conflict (a "
                "compare-and-swap found the value changed and wrote nothing), unknown (no outcome "
                "and not checked yet), "
                "diverged (a check found neither the old nor the new value), reverted. "
                "One page at a time (limit / offset)."
            ),
            effect="read",
            params=(limit_param("Max groups to return."), offset_param()),
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
                "the planned output files: landed / partial (some frames) / not_landed (no "
                "error reported, nothing written; for Render nodes only, no new clip) / failed "
                "(the render reported a failure), "
                "basis 'handle'. A render still running, or a "
                "bridge that does not answer, is unknown (the intent stays open and writes stay "
                "refused; call journal.check again later, which is a read and not a retry of "
                "the render). "
                "Without a handle, or with one the bridge no longer knows, the files alone "
                "decide (landed = every planned frame written since the intent, partial = "
                "some, not_landed = none; basis 'disk', an estimate: the files can be "
                "complete before render() returns). An open render.clip intent is decided the "
                "same way, the bridge's answer first (basis 'handle'); without one Flame is "
                "read again and is_rendered() decides: true = landed, false = not_landed (basis "
                "'flame', an estimate; when the intent's isRenderedBefore was true, a true read is "
                "not evidence that the render ran). An open render.export intent is decided the "
                "same way, the bridge's answer first (basis 'handle'); without one the output "
                "directory is listed again (basis 'disk', an estimate): every expected file "
                "written since the intent = landed (any file when the count was not known), some "
                "= partial, none = not_landed. An open render.still intent is decided like a "
                "render.export one (one new file = landed) and then also removes the "
                "temporary reference copy of the clip that the call left in Flame, if its name, "
                "duration and marks are the call's. Each finding is "
                "appended to the journal. "
                "A finding of any kind other than unknown lifts this server's refusal of writes "
                "that followed a write with no answer. "
                "A refusal that has no intent (a checkpoint or stash copy, or a purge batch, "
                "that ended without an answer) is lifted by one read of the checkpoint library "
                "when `group` is omitted: result.unrecorded lists those refusals and the stash "
                "copies no checkpoint accounts for (a copy made by such a call is among them)."
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
            params=(limit_param("Max groups to return."), offset_param()),
            server_handler=_inflight,
        )
    )
    registry.register(
        OperationSpec(
            name="journal.revert",
            plan_use=_PLAN_USE_JOURNAL_REVERT,
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
                "A desktop.reformat group: newest clip first, each stashed copy is restored "
                "(a Timeline restore: the reformatted clip is moved into the stash reel and the "
                "stashed copy is copied back; a new uid, the end of the reel; force=true "
                "overrides the fingerprint check) and its format is read back "
                "(result.restored[].format); with duplicate=true the copy is deleted instead, "
                "only while it is as left. One that cannot be put back leaves the others "
                "restored (result.partial). "
                "A desktop.arrange group: newest item first, each by the revert of its own op "
                "(a copy is deleted only while it is as made; a move puts the thing back into "
                "its old parent, at the end, with a new uid); one that cannot be put back leaves "
                "the others reverted (result.notReverted, result.partial) and a second revert "
                "does the rest. "
                "A desktop.reorder group: the fewest entries are moved to the end of the reel in "
                "the old order (a desktop.move; the entries it moves get new uids, and they can "
                "include ones the reorder did not move), CONFLICT with nothing moved when the "
                "reel no longer holds the entries the reorder left. "
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
                "effect_load_setup / edit_effect_setup (a sequence / clip): restore its stash "
                "copy, only "
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
                "previous values of the keys the group applied or changed (a key whose value moved "
                "without becoming the one asked for counts), each only if it still holds "
                "what the group left (compare-and-swap, checked twice); if any differs nothing "
                "is written and the current values are reported (CONFLICT) -- a value changed "
                "by someone else is never overwritten automatically. An already reverted group, "
                "or one that applied and changed nothing, is refused. Journaled as a new group "
                "(`reverts` "
                "= this one), itself revertible. The newer-group rule: a revert of a checkpointed "
                "group, of a desktop.reformat in place, of a timeline.create_sequence, of a "
                "segment.match / copy_to_media_panel or of a desktop.move stops while a later "
                "group landed (or has an unknown or diverged result) on the same target and was "
                "not reverted -- the same Batch group, or the same sequence / clip followed "
                "through the new uids a restore or a desktop.reformat gave it, or the thing the "
                "group made or moved. A value write (set_attributes, rename ...), a reformat, a "
                "move and a checkpointed write count; a read, a reverted group and a group that "
                "made something new do not. The revert then ends in CONFLICT, nothing is restored, "
                "and the landed groups are in result.newerGroups [{group, op, ts}] (newest first): "
                "revert those first, or pass force=true (journaled for a checkpointed group; the "
                "result still has newerGroups; for the other kinds force skips only this rule). "
                "Other desktop groups (desktop.delete, copy, import, duplicate, create, "
                "arrange) have no such rule. The parent group of a plan.for_each write loop "
                "(`result.group` of the loop) holds one child group per item or per step, "
                "and journal.revert(parent) puts the children back newest first, each by its "
                "own rule above (journal.revert(child) puts back that one only); the first "
                "child that cannot be put back (CONFLICT ...) stops it, result.children says "
                "how far it got and result.remaining what is left, and calling again goes on "
                "from there. A child that made output that cannot be put back (a render) is "
                "not touched and is listed in result.kept. "
                "What a person changed in the GUI is seen only as far "
                "as the fingerprint reaches (nodes and wiring, or segments and effects), so a "
                "change of that kind is not caught. "
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
            server_handler=journal_revert.revert,
        )
    )
