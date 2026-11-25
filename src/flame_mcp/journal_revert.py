"""`journal.revert`: puts back what a journal group wrote.

- the parent of a `plan.for_each` write loop: its children, newest first (`plan_loop_revert`)
- a group of Desktop writes: by the kind of the write (delete what was made, move back, ...)
- a group of a checkpoint intent (an `eval.run` destructive run, a checkpoint restore, a
  `batch.load_setup`, an op that declares a `CheckpointSpec`): restores the checkpoint, but only
  while the Batch group's fingerprint is still the one the run left (`force` overrides, and is
  journaled); the same for a sequence / clip (`targetKind: "timeline"`) with `timeline_restore`
- any other group: the previous values of the keys it applied or changed, but only while each
  still holds what the group left; otherwise it refuses and reports the current values. When a
  group wrote one key of one target several times, only the newest write is compared to
  Flame's current value; each earlier one is compared to the value the next write found before
  it (`reason` on that entry), and the writes are put back newest first.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from flame_mcp.batch_delete_run import BATCH_DELETE_KIND, check_batch_delete_intent
from flame_mcp.checkpoints import (
    ATTRIBUTES_SECTION,
    TIMELINE_KIND,
    fingerprint_of,
    kind_named,
    read_fingerprint,
)
from flame_mcp.desktop_arrange_revert import revert_arrange_group
from flame_mcp.desktop_arrange_run import ARRANGE_GROUP_KIND
from flame_mcp.desktop_delete_run import check_delete_intent, revert_delete_group
from flame_mcp.desktop_media_run import check_media_intent, revert_media_group
from flame_mcp.desktop_reformat_check import check_reformat_intent
from flame_mcp.desktop_reformat_revert import revert_reformat_group
from flame_mcp.desktop_reorder_revert import revert_reorder_group
from flame_mcp.desktop_reorder_run import REORDER_GROUP_KIND
from flame_mcp.desktop_revert import revert_desktop_group
from flame_mcp.errors import as_error_code
from flame_mcp.export_check import check_export_intent
from flame_mcp.export_run import EXPORT_KIND
from flame_mcp.journal import (
    KIND_DESKTOP_DELETE,
    KIND_DESKTOP_DUPLICATE,
    KIND_DESKTOP_EXTRACT,
    KIND_DESKTOP_IMPORT,
    KIND_DESKTOP_MOVE,
    KIND_DESKTOP_REFORMAT,
    KIND_DESKTOP_SEQUENCE,
    STATE_REVERTED,
    STATE_UNKNOWN,
    Index,
    IntentView,
    JournalLink,
    attribute_values_equal,
    new_id,
)
from flame_mcp.newer_groups import NEWER_GROUPS_KEY, newer_groups, newer_groups_sentence
from flame_mcp.plan_loop_revert import revert_loop_group
from flame_mcp.render_check import check_render_intent
from flame_mcp.render_clip_check import check_render_clip_intent
from flame_mcp.render_clip_run import RENDER_CLIP_KIND
from flame_mcp.render_run import RENDER_KIND
from flame_mcp.restore import restore_checkpoint
from flame_mcp.runtime import ServerContext
from flame_mcp.segment_extract_run import check_extract_intent, revert_extract_group
from flame_mcp.segment_remove_run import REMOVE_KIND, check_remove_intent
from flame_mcp.still_check import check_still_intent
from flame_mcp.still_run import STILL_KIND
from flame_mcp.timeline_create_run import check_sequence_intent, revert_sequence_group
from flame_mcp.timeline_restore import (
    revert_timeline_group,
)
from flame_mcp.types import (
    ErrorCode,
    ServerOpError,
    ServerResult,
)

# The one read failure that says something about the target (it is not there); every other
# failure means Flame was not read, so an intent stays undecided rather than being guessed at.
NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"


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
            if code == NOT_FOUND_CODE:
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
    return CurrentRead("not_found", {}, None, NOT_FOUND_CODE, "the target was not found")


@dataclass(frozen=True)
class _RevertPlan:
    view: IntentView
    keys: list[str]
    target: dict[str, Any]
    expected: dict[str, Any]


_Check = Callable[[ServerContext, IntentView], dict[str, Any]]
_Revert = Callable[[ServerContext, Index, str, list[IntentView], bool], ServerResult]


@dataclass(frozen=True)
class KindSpec:
    """What `journal.check` and `journal.revert` do with an intent of one kind: `check` decides
    whether its write landed; `revert` puts a Desktop write back, `irreversible` says why a group
    of that kind has nothing to revert (`{group}` is filled in). Neither: the ops have no revert
    and the generic "nothing to revert" answer is given."""

    check: _Check
    revert: _Revert | None = None
    irreversible: str | None = None


_MEDIA = KindSpec(check_media_intent, revert_media_group)

KINDS: Final[dict[str, KindSpec]] = {
    RENDER_KIND: KindSpec(
        check_render_intent,
        irreversible="group {group} is a render: its output (the files on disk, the clips in "
        "the shelf reel) cannot be undone, so there is nothing to revert",
    ),
    RENDER_CLIP_KIND: KindSpec(
        check_render_clip_intent,
        irreversible="group {group} is a render of a clip's Timeline FX: it cannot be undone, "
        "so there is nothing to revert",
    ),
    EXPORT_KIND: KindSpec(
        check_export_intent,
        irreversible="group {group} is an export: the files it wrote cannot be undone, so "
        "there is nothing to revert",
    ),
    STILL_KIND: KindSpec(
        check_still_intent,
        irreversible="group {group} is a still: the frame it wrote cannot be undone, so there "
        "is nothing to revert",
    ),
    BATCH_DELETE_KIND: KindSpec(
        check_batch_delete_intent,
        irreversible="group {group} deleted a Batch group: it cannot be put back by a revert; "
        "create a Batch group and batch.load_setup the file in the intent's setupPath",
    ),
    REMOVE_KIND: KindSpec(check_remove_intent),
    KIND_DESKTOP_DELETE: KindSpec(check_delete_intent, revert_delete_group),
    KIND_DESKTOP_IMPORT: _MEDIA,
    KIND_DESKTOP_DUPLICATE: _MEDIA,
    KIND_DESKTOP_MOVE: _MEDIA,
    KIND_DESKTOP_REFORMAT: KindSpec(check_reformat_intent, revert_reformat_group),
    KIND_DESKTOP_EXTRACT: KindSpec(check_extract_intent, revert_extract_group),
    KIND_DESKTOP_SEQUENCE: KindSpec(check_sequence_intent, revert_sequence_group),
}
"""Intent kind (`IntentView.effective_kind`: a Desktop revert counts as the write it reverts) ->
its check and revert. The order matters for a revert: of a group with writes of several of these
kinds, the first row with a `revert` that any intent of the group fits reverts it. An intent of
no row is checked by `journal.check` as a Desktop write, a fingerprint write or a value write,
and a Desktop group of no row goes to `revert_desktop_group`."""


def _nothing_to_revert(index: Index, group: str) -> ServerOpError:
    kinds = {view.effective_kind for view in index.groups[group]}
    for kind, spec in KINDS.items():
        if spec.irreversible is not None and kind in kinds:
            return ServerOpError("PRECONDITION_UNMET", spec.irreversible.format(group=group))
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
        f"group {group} is in state {state!r}: it applied or changed no key that a revert "
        "could put back",
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _plan_revert(ctx: ServerContext, index: Index, group: str) -> list[_RevertPlan]:
    """Read Flame for every key the group applied or changed (and that no earlier, partial
    revert already put back) and require each to still hold what the group left. Raises before
    anything is written. Intents are taken in reverse order of the group.

    A key written more than once on one target is compared to Flame's current value by its
    newest intent only; each older intent is compared to what the next one recorded as its
    `before` (the value it found), so the chain must be unbroken (an entry of `keys` says so in
    its `reason`). The chain is followed per recorded `target`, so writes that moved the target's
    address (a node renamed twice, a node of a shared name moved) are not linked: they are
    each compared to the current value, which can end in CONFLICT or SELECTOR_EMPTY."""
    views = [
        v
        for v in reversed(index.groups[group])
        if v.revertible_keys() and not index.intent_reverted(v)
    ]
    if not views:
        raise _nothing_to_revert(index, group)
    plans: list[_RevertPlan] = []
    refused: list[dict[str, Any]] = []
    reads: dict[str, CurrentRead] = {}
    newer_writes: dict[tuple[str, str], IntentView] = {}  # (target, key) -> next intent in time
    for view in views:
        keys = view.revertible_keys()
        intent = view.intent
        read_key = _canonical(
            [
                intent.get("readOp"),
                intent.get("op"),
                intent.get("target"),
                intent.get("targetAfter"),
                keys,
            ]
        )
        current = reads.get(read_key)
        if current is None:
            current = reads[read_key] = read_current(ctx, intent, keys)
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
        target_id = _canonical(intent.get("target"))
        per_key: dict[str, dict[str, Any]] = {}
        for key in keys:
            newer = newer_writes.get((target_id, key))
            found = (
                current.values[key] if newer is None else (newer.intent.get("before") or {})[key]
            )
            match = attribute_values_equal(key, found, expected[key])
            per_key[key] = {"expected": expected[key], "current": found, "match": match}
            if newer is not None:
                per_key[key]["reason"] = (
                    f"compared with the next write's before ({newer.id}), not with the current "
                    "value"
                    + (
                        ""
                        if match
                        else f": the journal is not continuous (this write left "
                        f"{expected[key]!r} but the next write found {found!r})"
                    )
                )
            newer_writes[(target_id, key)] = view
        if not all(entry["match"] for entry in per_key.values()):
            refused.append({"id": view.id, "op": intent.get("op"), "keys": per_key})
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


def _attribute_change_note(ctx: ServerContext, view: IntentView) -> str | None:
    """For a bundled plan with value writes: why a revert must not go on (an attribute value of
    the group changed after the run, or the run recorded none to compare), None when they match.
    Raises `ServerOpError` when the group cannot be read."""
    env = read_fingerprint(ctx, view.intent.get("target") or {}, sections=[ATTRIBUTES_SECTION])
    current = fingerprint_of(env)
    if current is None:
        raise ServerOpError(
            as_error_code(str(env.get("errorCode"))),
            f"nothing was restored: the Batch group's attribute values could not be read "
            f"({env.get('error')})",
        )
    expected = view.attribute_fingerprint_after()
    if expected is None:
        return "the run recorded no fingerprint of the group's attribute values"
    if current != expected:
        return "the group's attribute values changed after the run"
    return None


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
    newer = _refuse_for_newer_groups(index, group, force)
    forced_over_newer = {NEWER_GROUPS_KEY: [row["group"] for row in newer]} if newer else None
    if kind_named(view.intent.get("targetKind")) is TIMELINE_KIND:
        return _with_newer_groups(
            revert_timeline_group(ctx, index, group, view, force, forced_note=forced_over_newer),
            newer,
        )
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
    attribute_note: str | None = None
    if matches and view.intent.get("watchAttributes") is True:
        attribute_note = _attribute_change_note(ctx, view)
    forced: dict[str, Any] | None = None
    if not matches or attribute_note is not None:
        detail = {
            "expectedFingerprint": expected,
            "currentFingerprint": current,
            "note": (
                attribute_note
                if matches and attribute_note is not None
                else "the run recorded no fingerprint of its result"
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
    if forced_over_newer is not None:
        forced = {"used": True, **(forced or {}), **forced_over_newer}
    # a plan.run that was bundled has the plan's value writes (node.set_attributes ...) in the
    # same group: the one restore puts them back too, so it covers the whole group
    covers_group = len(index.groups[group]) > 1
    outcome = restore_checkpoint(
        ctx,
        checkpoint_id,
        link=JournalLink(reverts=group, reverts_intent=None if covers_group else view.id),
        forced=forced,
    )
    result = dict(outcome.result) if isinstance(outcome.result, dict) else {}
    result["reverted"] = group
    return _with_newer_groups(
        ServerResult(result=result, affected=outcome.affected, journal=outcome.journal), newer
    )


def _refuse_for_newer_groups(index: Index, group: str, force: bool) -> list[dict[str, Any]]:
    """The groups written to the same target after the checkpointed `group` that landed and were
    not reverted, newest first. Without `force` they end the revert in CONFLICT before anything is
    restored (the restore would overwrite what they wrote, and the fingerprint cannot see all of
    it); with `force` the list comes back for the result."""
    rows = newer_groups(index.records, index, group)
    if rows and not force:
        raise ServerOpError(
            "CONFLICT",
            f"nothing was restored: restoring the checkpoint of {group} would overwrite what "
            "later group(s) wrote to the same target (pass force=true to restore anyway; that "
            "is journaled)." + newer_groups_sentence(rows),
            result={"refused": True, "group": group, NEWER_GROUPS_KEY: rows},
        )
    return rows


def _with_newer_groups(result: ServerResult, rows: list[dict[str, Any]]) -> ServerResult:
    """A forced revert's result, with the newer groups it restored over."""
    if not rows:
        return result
    body = dict(result.result) if isinstance(result.result, dict) else {}
    body[NEWER_GROUPS_KEY] = rows
    return replace(result, result=body)


_GroupRevert = Callable[[ServerContext, Index, str, list[IntentView]], ServerResult]

_GROUP_KIND_REVERTS: Final[Mapping[str, _GroupRevert]] = {
    ARRANGE_GROUP_KIND: revert_arrange_group,
    REORDER_GROUP_KIND: revert_reorder_group,
}
"""A group record's `kind` -> its revert; tested before the kinds of the writes."""


def _revert_desktop(
    ctx: ServerContext, index: Index, group: str, desktop_views: list[IntentView], force: bool
) -> ServerResult:
    """The revert of a group of Desktop writes, by the kind of the write."""
    meta = index.group_meta.get(group)
    group_kind = meta.get("kind") if meta is not None else None
    group_revert = _GROUP_KIND_REVERTS.get(group_kind) if isinstance(group_kind, str) else None
    if group_revert is not None:
        return group_revert(ctx, index, group, desktop_views)
    kinds = {v.effective_kind for v in desktop_views}
    for kind, spec in KINDS.items():
        if spec.revert is not None and kind in kinds:
            return spec.revert(ctx, index, group, desktop_views, force)
    return revert_desktop_group(ctx, index, group, desktop_views)


_Handler = Callable[["ServerContext", Index, str, bool], ServerResult | None]
"""One kind of group: its revert, or None when `group` is not of that kind."""


def _desktop_kind(ctx: ServerContext, index: Index, group: str, force: bool) -> ServerResult | None:
    views = [v for v in index.groups[group] if v.desktop_kind is not None]
    if not views:
        return None
    newer = (
        _refuse_for_newer_groups(index, group, force)
        if any(v.is_revertible() for v in views)
        else []
    )
    return _with_newer_groups(_revert_desktop(ctx, index, group, views, force), newer)


def _checkpoint_kind(
    ctx: ServerContext, index: Index, group: str, force: bool
) -> ServerResult | None:
    views = [v for v in index.groups[group] if v.revert_checkpoint() is not None]
    if not views:
        return None
    return _revert_by_checkpoint(ctx, index, group, views, force)


def _values_kind(ctx: ServerContext, index: Index, group: str, force: bool) -> ServerResult:
    del force  # a value write is compared key by key; nothing is forced
    return _revert_values(ctx, index, group)


_HANDLERS: Final[tuple[_Handler, ...]] = (_desktop_kind, _checkpoint_kind, _values_kind)
"""Tried in order: the first that knows the group reverts it (the last takes any group)."""


def revert(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    group = str(args["group"])
    force = bool(args.get("force", False))
    with ctx.write_lock:
        index = Index(ctx.journal.read_all().records)
        if index.is_loop_parent(group):
            return revert_loop_group(ctx, index, group, force, revert)
        if group not in index.groups:
            raise ServerOpError("INVALID_ARGS", f"no journal group {group!r}")
        if index.is_reverted(group):
            raise _nothing_to_revert(index, group)
        for handler in _HANDLERS:
            result = handler(ctx, index, group, force)
            if result is not None:
                return result
    raise AssertionError("the last handler takes any group")


def _revert_values(ctx: ServerContext, index: Index, group: str) -> ServerResult:
    """The previous values back, intent by intent, newest first. The caller holds the lock."""
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
                link=JournalLink(new_group, reverts=group, reverts_intent=plan.view.id),
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
