"""Restoring a sequence / clip from a Timeline checkpoint (`journal.restore_checkpoint` of a
`kind: "timeline"` checkpoint, and `journal.revert` of a group whose intent is a timeline one).

The checkpoint is a copy in a stash reel (`timeline_checkpoints`). A restore, in one Flame call
(`timeline_prelude._tl_restore_write`):

    (a) note whether the target is the sequence shown in the Timeline;
    (b) move the edited target into the stash reel (`media_panel.move`: a new uid) -- this is the
        pre-restore checkpoint, recorded in the journal as a new timeline checkpoint
        (`preCheckpoint`), so the restore itself can be undone; nothing is deleted;
    (c) copy the stashed copy into the target's parent (`media_panel.copy`: a new uid, the end of
        the reel); if that fails the edited target is moved back into its parent first;
    (d) if (a), `open()` the restored sequence;
    (e) the restored thing's fingerprint must be the checkpoint's.

Before that, a read call (`_tl_restore_read`) finds the target by uid and gives its fingerprint,
which the write then compares again (compare-and-swap). For `journal.revert` the target's
fingerprint must still be the one the run left (`force` overrides, and is journaled). Every restore
gives the target a new uid and wiretap id: the outcome records `replacedUid` -> `targetUid`, and a
later revert / restore finds the thing again by following those links (`uid_chain`).

The restore is an intent / outcome pair of its own (op `journal.restore_checkpoint`), whose intent
names `revertCheckpoint` = the id of the pre-restore checkpoint, so `journal.revert` can undo it the
same way it undoes an edit. The intent is written before the pre-restore checkpoint exists (that
comes out of the call): its id is chosen first and its record follows the call.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import execution
from flame_mcp.checkpointed import refuse_if_denied
from flame_mcp.checkpoints import (
    CHECKPOINT_TIMEOUT_S,
    TIMELINE_KIND,
    fingerprint_of,
    read_fingerprint,
    wiretap_target,
)
from flame_mcp.desktop_run import PHASE_READ, PHASE_WRITE
from flame_mcp.errors import as_error_code
from flame_mcp.execution import raise_on_error_src
from flame_mcp.journal import (
    NO_LINK,
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    Index,
    IntentView,
    JournalLink,
    new_id,
    now_iso,
    purge_records,
)
from flame_mcp.journal_records import CheckRecord, IntentRecord, OutcomeRecord
from flame_mcp.journaled_write import UNKNOWN_END_CODES, close_outcome, note_unresolved
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.timeline_checkpoints import (
    LIBRARY_ARG,
    REEL_ARG,
    checkpoint_library_name,
    checkpoint_record,
    stash_reel_name,
)
from flame_mcp.types import (
    PHASE_ARG,
    Category,
    OperationSpec,
    PhaseSpec,
    ServerOpError,
    ServerResult,
)

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

OP_NAME: Final[str] = "journal.restore_checkpoint"
UIDS_ARG: Final[str] = "_uids"
STASH_UID_ARG: Final[str] = "_stash_uid"
STASH_FP_ARG: Final[str] = "_stash_fp"
EXPECTED_ARG: Final[str] = "_expected"

NOT_RESTORED: Final[tuple[str, ...]] = (
    "the uid and the wiretap id: a restored thing always has new ones, so a selector or uid that "
    "named the old one no longer resolves (use restored.selector)",
    "its place in the reel: the restored thing is at the end of its reel (position)",
    "what the fingerprint does not see (markers, transitions, audio, an effect's parameters that "
    "are not attributes, the names and attributes of segments and of the sequence): these come "
    "back only as far as media_panel.copy copies them (audio: the revert check compares the "
    "video fingerprint only; the revert of timeline.shorten and of timeline.move_track brought "
    "the audio rows back, measured on Flame 2025.2.4 and 2026.2.3, and in the "
    "timeline.move_track measurements the audio did not change)",
)
"""What a restore does not bring back, worded once for every result and description."""

RESTORE_NOTE: Final[str] = (
    "TIMELINE RESTORE: the edited sequence / clip is moved into the stash reel (the "
    "pre-restore checkpoint, result.preCheckpoint: a restore can itself be undone; nothing is "
    "deleted) and the stashed copy is copied back into its reel. If the edited sequence was the "
    "one shown in the Timeline, the restored one is opened (result.reopened); otherwise what the "
    "Timeline shows is read before and after and, if it changed, the one shown before is opened "
    "again (result.shownBefore / shownAfter / reshown). Only a sequence can be opened: for a "
    "clip reshown is false. Not put "
    "back: " + "; ".join(NOT_RESTORED) + ". result.fingerprintMatches says whether the restored "
    "thing has the checkpoint's fingerprint."
)
"""Worded once for the descriptions of everything that restores a Timeline checkpoint."""

_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"


def _source(*blocks: str) -> str:
    return "\n\n".join(blocks)


def _restore_to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_READ)
    uids = list(args.get(UIDS_ARG) or [])
    stash_uid = args.get(STASH_UID_ARG)
    stash_fp = args.get(STASH_FP_ARG)
    if phase == PHASE_WRITE:
        library = args.get(LIBRARY_ARG)
        reel = args.get(REEL_ARG)
        return _source(
            f"_result = _tl_restore_write({uids!r}, {args.get(EXPECTED_ARG)!r}, {stash_uid!r}, "
            f"{stash_fp!r}, {library!r}, {reel!r})",
            raise_on_error_src(),
            "_affected = 1",
        )
    return _source(
        f"_result = _tl_restore_read({uids!r}, {stash_uid!r}, {stash_fp!r})",
        raise_on_error_src(),
        "_affected = 0",
    )


@cache
def restore_op() -> RegisteredOperation:
    """The Flame-side restore as an operation of a registry of its own: it gets the registration
    checks and the execution path of every op, but is not in the public catalog (a raw restore
    without the journal around it is not something a caller should have)."""
    registry = Registry()
    registry.register_category(
        Category(
            name="timeline",
            description="internal",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    return registry.register(
        OperationSpec(
            name="timeline.restore",
            category="timeline",
            description="internal: the Flame side of a Timeline restore",
            effect="destructive",
            to_python=_restore_to_python,
            phases=(PhaseSpec(PHASE_WRITE),),
            requires=(
                "PyMediaPanel.copy",
                "PyMediaPanel.move",
                "PySequence.open",
                "PyTimeline.clip",
                "PyWorkspace.libraries",
                "PyLibrary.reels",
                "PyLibrary.create_reel",
                "PyWorkspace.create_library",
                "flame.find_by_uid",
            ),
        )
    )


def _run(
    ctx: ServerContext, args: Mapping[str, Any], *, timeout_s: float | None = None
) -> dict[str, Any]:
    return execution.run_op(
        restore_op(), dict(args), run_dir=ctx.run_dir, timeout_s=timeout_s or CHECKPOINT_TIMEOUT_S
    )


def uid_chain(records: list[dict[str, Any]], uid: str) -> list[str]:
    """`uid` and every uid the thing had after it, newest first: a restore (or a rolled-back one)
    gives the target a new uid and its outcome records `replacedUid` -> `targetUid`."""
    mapping = {
        str(r["replacedUid"]): str(r["targetUid"])
        for r in records
        if r.get("type") == "outcome"
        and isinstance(r.get("replacedUid"), str)
        and isinstance(r.get("targetUid"), str)
    }
    chain = [uid]
    while chain[-1] in mapping and mapping[chain[-1]] not in chain:
        chain.append(mapping[chain[-1]])
    chain.reverse()
    return chain


def _short(fingerprint: Any) -> str:
    return str(fingerprint)[:12] if fingerprint else "none"


def _selector_row(ident: Any) -> dict[str, Any] | None:
    if not isinstance(ident, dict):
        return None
    wiretap = ident.get("wiretapId")
    return {
        "type": ident.get("type"),
        "name": ident.get("name"),
        "uid": ident.get("uid"),
        "wiretapId": wiretap,
        "selector": wiretap_target(wiretap) if isinstance(wiretap, str) and wiretap else None,
    }


def _flame_value(env: Mapping[str, Any]) -> dict[str, Any]:
    raw = env.get("result")
    return dict(raw) if isinstance(raw, dict) else {}


def restore_timeline_checkpoint(
    ctx: ServerContext,
    record: Mapping[str, Any],
    *,
    uid: str | None = None,
    check_current: bool = False,
    expected_current: str | None = None,
    force: bool = False,
    link: JournalLink = NO_LINK,
    forced_note: Mapping[str, Any] | None = None,
) -> ServerResult:
    """Put the sequence / clip of the timeline checkpoint `record` back, as described above.
    `forced_note`: what the caller overrode with `force` on its own check; it is journaled in the
    restore's intent (`forced`) beside the fingerprint override, if there is one.
    `uid` is the thing to replace (default: the checkpoint's source). With `check_current` its
    fingerprint must now be `expected_current` (a revert: what the run left), else CONFLICT unless
    `force`. Raises `ServerOpError` on every refusal and failure; a failure after the edited thing
    was moved always names where it is."""
    checkpoint_id = str(record.get("id"))
    source = record.get("source")
    stash = record.get("stash")
    fingerprint = record.get("fingerprint")
    source_uid = uid if uid is not None else (source or {}).get("uid")
    stash_uid = (stash or {}).get("uid") if isinstance(stash, dict) else None
    if not (
        isinstance(source_uid, str) and isinstance(stash_uid, str) and isinstance(fingerprint, str)
    ):
        raise ServerOpError(
            "INVALID_ARGS",
            f"checkpoint {checkpoint_id!r} has no source uid, stash uid or fingerprint: there is "
            "nothing to restore into, or from",
        )
    refuse_if_denied(ctx, (*TIMELINE_KIND.needed_ops, restore_op()))
    records = ctx.journal.read_all().records
    purge = purge_records(records).get(checkpoint_id)
    if purge is not None:
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"nothing was restored: the stashed copy of checkpoint {checkpoint_id} was purged "
            f"(journal.purge_checkpoints, {purge.get('ts')}): there is nothing to restore from",
        )
    uids = uid_chain(records, source_uid)
    return _restore(
        ctx,
        record,
        uids=uids,
        stash_uid=stash_uid,
        fingerprint=fingerprint,
        check_current=check_current,
        expected_current=expected_current,
        force=force,
        link=link,
        forced_note=forced_note,
    )


def _refuse_read(env: Mapping[str, Any], checkpoint_id: str) -> ServerOpError:
    value = _flame_value(env)
    return ServerOpError(
        as_error_code(str(env.get("errorCode"))),
        f"nothing was restored: {env.get('error')}",
        result={
            "ran": False,
            "checkpoint": checkpoint_id,
            "target": value.get("target"),
            "stash": value.get("stash"),
        },
        hint=env.get("hint"),
    )


def _conflict(
    checkpoint_id: str, expected: str | None, current: Any, forced: bool
) -> tuple[dict[str, Any], ServerOpError | None]:
    detail = {
        "expectedFingerprint": expected,
        "currentFingerprint": current,
        "note": (
            "the run recorded no fingerprint of its result"
            if expected is None
            else "the sequence's segments or effects changed after the run"
        ),
    }
    if forced:
        return {"used": True, **detail}, None
    return detail, ServerOpError(
        "CONFLICT",
        f"nothing was restored: {detail['note']}, so restoring checkpoint {checkpoint_id} "
        "would overwrite whatever happened since (pass force=true to restore anyway; that is "
        "journaled)",
        result={"refused": True, "checkpoint": checkpoint_id, **detail},
    )


def _check_current_fingerprint(
    pre: Mapping[str, Any],
    checkpoint_id: str,
    *,
    check_current: bool,
    expected_current: str | None,
    force: bool,
    forced_note: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The `forced` note of the restore: what was overridden (the revert's fingerprint check, the
    caller's own check), or None. Raises CONFLICT when the fingerprint is not the expected one and
    `force` is not set."""
    current_fp = pre.get("fingerprint")
    forced: dict[str, Any] | None = None
    if check_current and (expected_current is None or current_fp != expected_current):
        forced, refusal = _conflict(checkpoint_id, expected_current, current_fp, force)
        if refusal is not None:
            raise refusal
    if forced_note is not None:
        forced = {"used": True, **(forced or {}), **forced_note}
    return forced


def _restore_intent(
    record: Mapping[str, Any],
    pre: Mapping[str, Any],
    link: JournalLink,
    forced: dict[str, Any] | None,
    fingerprint: str,
    ids: tuple[str, str, str],
) -> IntentRecord:
    """The intent of the restore; `ids` is (group, intent, pre-restore checkpoint)."""
    group_id, intent_id, pre_id = ids
    target = _selector_row(pre.get("target"))
    intent: IntentRecord = {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": OP_NAME,
        "kind": "restore",
        "args": {"checkpoint": str(record.get("id"))},
        "target": target["selector"] if target else None,
        "targetKind": TIMELINE_KIND.name,
        "targetUid": target["uid"] if target else None,
        "targetIdent": pre.get("target"),
        "checkpoint": str(record.get("id")),
        "stash": record.get("stash"),
        "preCheckpoint": pre_id,
        "revertCheckpoint": pre_id,
        "fingerprintBefore": pre.get("fingerprint"),
        "fingerprintExpected": fingerprint,
        "segmentCountBefore": pre.get("segmentCount"),
    }
    link.stamp(intent)
    if forced is not None:
        intent["forced"] = forced
    return intent


def _read_current(
    ctx: ServerContext, uids: list[str], stash_uid: str, fingerprint: str, checkpoint_id: str
) -> dict[str, Any]:
    """The read phase: what the edited thing is now (its fingerprint, address, segment count)."""
    read_env = _run(
        ctx,
        {
            PHASE_ARG: PHASE_READ,
            UIDS_ARG: uids,
            STASH_UID_ARG: stash_uid,
            STASH_FP_ARG: fingerprint,
        },
    )
    if read_env.get("ok") is not True:
        raise _refuse_read(read_env, checkpoint_id)
    return _flame_value(read_env)


def _write_restore(
    ctx: ServerContext,
    uids: list[str],
    stash_uid: str,
    fingerprint: str,
    current_fp: Any,
    ids: tuple[str, str, str],
    checkpoint_id: str,
) -> dict[str, Any]:
    """The write phase: Flame's envelope of the restore. Raises when no answer came (the guard is
    shut and the intent is noted unresolved)."""
    group_id, intent_id, _ = ids
    env = _run(
        ctx,
        {
            PHASE_ARG: PHASE_WRITE,
            UIDS_ARG: uids,
            STASH_UID_ARG: stash_uid,
            STASH_FP_ARG: fingerprint,
            EXPECTED_ARG: current_fp,
            LIBRARY_ARG: checkpoint_library_name(),
            REEL_ARG: stash_reel_name(),
        },
    )
    code = env.get("errorCode")
    if code in UNKNOWN_END_CODES:
        note_unresolved(ctx, intent_id, group_id, env, OP_NAME, str(code))
        raise ServerOpError(
            as_error_code(str(code)),
            f"the restore of checkpoint {checkpoint_id} ended without an answer ({code}); it is "
            "not known whether the sequence was replaced. This server now refuses writes until "
            f"journal.check (group={group_id!r}) has read Flame again.",
            result={"checkpoint": checkpoint_id, "group": group_id},
            hint=env.get("hint"),
            journal={"group": group_id, "id": intent_id},
        )
    return env


def _restore(
    ctx: ServerContext,
    record: Mapping[str, Any],
    *,
    uids: list[str],
    stash_uid: str,
    fingerprint: str,
    check_current: bool,
    expected_current: str | None,
    force: bool,
    link: JournalLink,
    forced_note: Mapping[str, Any] | None = None,
) -> ServerResult:
    checkpoint_id = str(record.get("id"))
    pre = _read_current(ctx, uids, stash_uid, fingerprint, checkpoint_id)
    current_fp = pre.get("fingerprint")
    forced = _check_current_fingerprint(
        pre,
        checkpoint_id,
        check_current=check_current,
        expected_current=expected_current,
        force=force,
        forced_note=forced_note,
    )
    ids = (
        link.group if link.group is not None else new_id("g"),
        new_id("i"),
        new_id("c"),
    )
    intent = _restore_intent(record, pre, link, forced, fingerprint, ids)
    try:
        ctx.journal.append(intent)
    except OSError as exc:
        raise ServerOpError(
            "POLICY_DENIED",
            f"nothing was restored: the journal cannot be written ({exc}), and this server does "
            "not restore a sequence without an intent on record",
            hint=f"Make {ctx.journal.directory} writable, then retry.",
        ) from exc
    env = _write_restore(ctx, uids, stash_uid, fingerprint, current_fp, ids, checkpoint_id)
    return _finish(
        ctx,
        env,
        record=record,
        intent_id=ids[1],
        group_id=ids[0],
        pre_id=ids[2],
        fingerprint=fingerprint,
        current_fp=current_fp,
        forced=forced,
    )


def _record_pre_checkpoint(
    ctx: ServerContext,
    value: Mapping[str, Any],
    restored: dict[str, Any] | None,
    pre_id: str,
    group_id: str,
    checkpoint_id: str,
) -> tuple[bool, str | None]:
    """Journals the pre-restore checkpoint when the edited thing was moved and not moved back:
    `(recorded, journal error)`."""
    rolled = value.get("rolledBack")
    try:
        if value.get("moved") is True and not (
            isinstance(rolled, dict) and rolled.get("done") is True
        ):
            pre_record = checkpoint_record(
                pre_id,
                group=group_id,
                label=f"before restoring {checkpoint_id}",
                source=value.get("restored"),
                stash=value.get("preStash"),
                fingerprint=value.get("fingerprintBefore"),
                segment_count=None,
                displayed=value.get("displayedBefore"),
            )
            pre_record["replacedUid"] = value.get("replacedUid")
            pre_record["orphaned"] = restored is None
            ctx.journal.append(pre_record)
            return True, None
    except OSError as exc:
        return False, str(exc)
    return False, None


def _restore_outcome(
    env: Mapping[str, Any],
    value: Mapping[str, Any],
    restored: dict[str, Any] | None,
    ids: tuple[str, str, str],
    pre_recorded: bool,
    fingerprint: str,
) -> tuple[OutcomeRecord, str | None]:
    """The outcome record of the restore (`ids`: group, intent, pre-restore checkpoint) and the
    failure text (None when it matched)."""
    group_id, intent_id, pre_id = ids
    flame_ok = env.get("ok") is True
    matches = value.get("fingerprintMatches") is True
    ok = flame_ok and matches
    moved = value.get("moved") is True
    after_fp = value.get("fingerprintAfter")
    new_uid, new_ident = _uid_after(value, restored)
    failure = (
        None
        if ok
        else (
            f"restore failed: {env.get('error')}"
            if not flame_ok
            else (
                f"the restore ran but the restored thing's fingerprint {_short(after_fp)} is not "
                f"the checkpoint's {_short(fingerprint)}"
            )
        )
    )
    outcome: OutcomeRecord = {
        "type": "outcome",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "ok": ok,
        "applied": [],
        "failed": {} if ok else {"restore": failure},
        "errorCode": None if ok else ("FLAME_API_ERROR" if flame_ok else env.get("errorCode")),
        "readBack": None,
        "fingerprintAfter": after_fp,
        "fingerprintAfterError": None,
        "changed": moved,
        "matched": matches,
        "targetUid": new_uid,
        "targetIdent": new_ident,
        "replacedUid": value.get("replacedUid") if moved else None,
        "preCheckpoint": pre_id if pre_recorded else None,
        "displayedBefore": value.get("displayedBefore"),
        "reopened": value.get("reopened"),
        "shownBefore": value.get("shownBefore"),
        "shownAfter": value.get("shownAfter"),
        "reshown": value.get("reshown"),
    }
    return outcome, failure


def _restore_result(
    value: Mapping[str, Any],
    restored: dict[str, Any] | None,
    ids: tuple[str, str, str],
    checkpoint_id: str,
    pre_recorded: bool,
    fingerprint: str,
    current_fp: Any,
) -> dict[str, Any]:
    group_id, _, pre_id = ids
    moved = value.get("moved") is True
    return {
        "group": group_id,
        "checkpoint": checkpoint_id,
        "matched": value.get("fingerprintMatches") is True,
        "restored": restored,
        "preCheckpoint": pre_id if pre_recorded else None,
        "preStash": value.get("preStash"),
        "replaced": {"uid": value.get("replacedUid")} if moved else None,
        "displayedBefore": value.get("displayedBefore"),
        "reopened": value.get("reopened"),
        "reopenNote": value.get("reopenNote"),
        "shownBefore": value.get("shownBefore"),
        "shownAfterRestore": value.get("shownAfterRestore"),
        "shownAfter": value.get("shownAfter"),
        "reshown": value.get("reshown"),
        "reshownNote": value.get("reshownNote"),
        "fingerprintBefore": current_fp,
        "fingerprintAfter": value.get("fingerprintAfter"),
        "expectedFingerprint": fingerprint,
        "fingerprintMatches": value.get("fingerprintMatches"),
        "preFingerprintMatches": value.get("preFingerprintMatches"),
        "position": value.get("position"),
        "rolledBack": value.get("rolledBack"),
        "notRestored": list(NOT_RESTORED),
    }


def _finish(
    ctx: ServerContext,
    env: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    intent_id: str,
    group_id: str,
    pre_id: str,
    fingerprint: str,
    current_fp: Any,
    forced: Mapping[str, Any] | None,
) -> ServerResult:
    checkpoint_id = str(record.get("id"))
    value = _flame_value(env)
    restored = _selector_row(value.get("restored"))
    ids = (group_id, intent_id, pre_id)
    pre_recorded, journal_error = _record_pre_checkpoint(
        ctx, value, restored, pre_id, group_id, checkpoint_id
    )
    outcome, failure = _restore_outcome(env, value, restored, ids, pre_recorded, fingerprint)
    err = close_outcome(ctx, outcome, intent_id, group_id, OP_NAME)
    if err is not None:
        journal_error = str(err)
    result = _restore_result(
        value, restored, ids, checkpoint_id, pre_recorded, fingerprint, current_fp
    )
    if pre_recorded:
        result["undo"] = {"op": OP_NAME, "args": {"checkpoint": pre_id}}
    if forced is not None:
        result["forced"] = dict(forced)
    if journal_error is not None:
        result["journalError"] = (
            f"the journal could not take a record ({journal_error}); this server now refuses "
            "writes until journal.check has settled the intent"
        )
    journal_ref = {"group": group_id, "id": intent_id}
    if outcome["ok"]:
        return ServerResult(result=result, affected=1, journal=journal_ref)
    hint = (
        f"The edited sequence is saved: journal.restore_checkpoint(checkpoint={pre_id!r}) puts it "
        "back."
        if pre_recorded
        else "Nothing is recorded to restore from; see result.rolledBack and result.preStash."
    )
    raise ServerOpError(
        as_error_code(str(outcome["errorCode"])),
        str(failure),
        result=result,
        hint=hint,
        journal=journal_ref,
    )


def _uid_after(
    value: Mapping[str, Any], restored: dict[str, Any] | None
) -> tuple[Any, dict[str, Any] | None]:
    """Who the target is after the call: the restored thing, the edited thing moved back (a
    rollback), or the thing untouched (nothing was moved)."""
    if restored is not None:
        return restored["uid"], value.get("restored")
    rolled = value.get("rolledBack")
    if isinstance(rolled, dict) and isinstance(rolled.get("object"), dict):
        return rolled["object"].get("uid"), rolled["object"]
    return None, None


def revert_timeline_group(
    ctx: ServerContext,
    index: Index,
    group: str,
    view: IntentView,
    force: bool,
    *,
    forced_note: Mapping[str, Any] | None = None,
) -> ServerResult:
    """Restore the checkpoint a timeline intent names (an edit: its checkpoint; a restore: its
    pre-restore checkpoint), but only while the target's fingerprint is the one the run left.
    `forced_note` is journaled in the restore's intent when the caller forced it past something
    else (later groups of the same target)."""
    checkpoint_id = view.revert_checkpoint()
    assert checkpoint_id is not None  # the view was picked by it
    record = index.checkpoints.get(checkpoint_id)
    if record is None:
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"nothing was restored: the journal has no record of checkpoint {checkpoint_id} (a "
            "restore that stopped before it moved anything leaves none: the group changed "
            "nothing)",
        )
    outcome = view.outcome
    target_uid = (outcome.get("targetUid") if outcome is not None else None) or view.intent.get(
        "targetUid"
    )
    # a plan.run that was bundled has the plan's other writes in the same group (a
    # segment.set_attributes): the one restore puts them back too, so it covers the whole group
    covers_group = len(index.groups[group]) > 1
    result = restore_timeline_checkpoint(
        ctx,
        record,
        uid=target_uid if isinstance(target_uid, str) else None,
        check_current=True,
        expected_current=view.fingerprint_after(),
        force=force,
        link=JournalLink(reverts=group, reverts_intent=None if covers_group else view.id),
        forced_note=forced_note,
    )
    body = dict(result.result) if isinstance(result.result, dict) else {}
    body["reverted"] = group
    return ServerResult(result=body, affected=result.affected, journal=result.journal)


def check_timeline_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """An intent of a timeline write / restore with no outcome: the target's fingerprint now
    against the one before the run and, for a restore, the one it was to reach (the same rule as
    a Batch group's). A restore that did run has moved the target (a new uid and wiretap id), so
    the old address finds nothing: that is `diverged`, with the target's new place unknown."""
    intent = view.intent
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    target = intent.get("target")
    env = read_fingerprint(ctx, target if isinstance(target, dict) else {}, TIMELINE_KIND)
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
        verdict, finding = STATE_DIVERGED, "other"
        entry["detail"] = (
            "the sequence was not found at its recorded address: a restore that ran gives it a "
            "new uid and wiretap id, so the call may have run; look for the restored sequence in "
            "its reel"
        )
    elif expected is not None and current == expected:
        verdict, finding = STATE_LANDED, "after"
    elif current == before:
        verdict, finding = STATE_NOT_LANDED, "before"
        entry["detail"] = (
            "the segments and effects are as before the run; effects the fingerprint does not "
            "see cannot be ruled out (a segment.replace that changed only the segment's name or "
            "source size, or swapped a same-name same-size clip, looks like this too)"
        )
    else:
        verdict, finding = STATE_DIVERGED, "other"
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
        "target": target,
    }
    ctx.journal.append(check)
    ctx.guard.release(view.id)
    return entry
