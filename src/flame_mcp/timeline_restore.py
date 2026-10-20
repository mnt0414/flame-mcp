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
from flame_mcp.checkpointed import note_unresolved, refuse_if_denied
from flame_mcp.checkpoints import (
    CHECKPOINT_TIMEOUT_S,
    TIMELINE_KIND,
    fingerprint_of,
    read_fingerprint,
    wiretap_target,
)
from flame_mcp.errors import as_error_code
from flame_mcp.journal import (
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    Index,
    IntentView,
    new_id,
    now_iso,
    purge_records,
)
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.timeline_checkpoints import (
    LIBRARY_ARG,
    REEL_ARG,
    checkpoint_library_name,
    checkpoint_record,
    stash_reel_name,
)
from flame_mcp.types import Category, OperationSpec, ServerOpError, ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

OP_NAME: Final[str] = "journal.restore_checkpoint"
PHASE_ARG: Final[str] = "_phase"
UIDS_ARG: Final[str] = "_uids"
STASH_UID_ARG: Final[str] = "_stash_uid"
STASH_FP_ARG: Final[str] = "_stash_fp"
EXPECTED_ARG: Final[str] = "_expected"
PHASE_READ: Final[str] = "read"
PHASE_WRITE: Final[str] = "write"

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


_RAISE_ON_ERROR: Final[str] = (
    "if _result['error'] is not None:\n    raise _OpError(_result['errorCode'], _result['error'])"
)


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
            _RAISE_ON_ERROR,
            "_affected = 1",
        )
    return _source(
        f"_result = _tl_restore_read({uids!r}, {stash_uid!r}, {stash_fp!r})",
        _RAISE_ON_ERROR,
        "_affected = 0",
    )


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


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
            undo_steps=_no_undo_steps,
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
    reverts: str | None = None,
    reverts_intent: str | None = None,
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
    refuse_if_denied(ctx, TIMELINE_KIND.needed_ops)
    records = ctx.journal.read_all().records
    purge = purge_records(records).get(checkpoint_id)
    if purge is not None:
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"nothing was restored: the stashed copy of checkpoint {checkpoint_id} was purged "
            f"(journal.purge_checkpoints, {purge.get('ts')}): there is nothing to restore from",
        )
    uids = uid_chain(records, source_uid)
    with ctx.write_lock:
        return _restore_locked(
            ctx,
            record,
            uids=uids,
            stash_uid=stash_uid,
            fingerprint=fingerprint,
            check_current=check_current,
            expected_current=expected_current,
            force=force,
            reverts=reverts,
            reverts_intent=reverts_intent,
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


def _restore_locked(
    ctx: ServerContext,
    record: Mapping[str, Any],
    *,
    uids: list[str],
    stash_uid: str,
    fingerprint: str,
    check_current: bool,
    expected_current: str | None,
    force: bool,
    reverts: str | None,
    reverts_intent: str | None,
    forced_note: Mapping[str, Any] | None = None,
) -> ServerResult:
    checkpoint_id = str(record.get("id"))
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
    pre = _flame_value(read_env)
    current_fp = pre.get("fingerprint")
    forced: dict[str, Any] | None = None
    if check_current and (expected_current is None or current_fp != expected_current):
        forced, refusal = _conflict(checkpoint_id, expected_current, current_fp, force)
        if refusal is not None:
            raise refusal
    if forced_note is not None:
        forced = {"used": True, **(forced or {}), **forced_note}
    target = _selector_row(pre.get("target"))
    group_id = new_id("g")
    intent_id = new_id("i")
    pre_id = new_id("c")
    library = checkpoint_library_name()
    reel = stash_reel_name()
    intent: dict[str, Any] = {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": OP_NAME,
        "kind": "restore",
        "args": {"checkpoint": checkpoint_id},
        "target": target["selector"] if target else None,
        "targetKind": TIMELINE_KIND.name,
        "targetUid": target["uid"] if target else None,
        "targetIdent": pre.get("target"),
        "checkpoint": checkpoint_id,
        "stash": record.get("stash"),
        "preCheckpoint": pre_id,
        "revertCheckpoint": pre_id,
        "fingerprintBefore": current_fp,
        "fingerprintExpected": fingerprint,
        "segmentCountBefore": pre.get("segmentCount"),
    }
    if reverts is not None:
        intent["reverts"] = reverts
    if reverts_intent is not None:
        intent["revertsIntent"] = reverts_intent
    if forced is not None:
        intent["forced"] = forced
    try:
        ctx.journal.append(intent)
    except OSError as exc:
        raise ServerOpError(
            "POLICY_DENIED",
            f"nothing was restored: the journal cannot be written ({exc}), and this server does "
            "not restore a sequence without an intent on record",
            hint=f"Make {ctx.journal.directory} writable, then retry.",
        ) from exc
    env = _run(
        ctx,
        {
            PHASE_ARG: PHASE_WRITE,
            UIDS_ARG: uids,
            STASH_UID_ARG: stash_uid,
            STASH_FP_ARG: fingerprint,
            EXPECTED_ARG: current_fp,
            LIBRARY_ARG: library,
            REEL_ARG: reel,
        },
    )
    code = env.get("errorCode")
    if code in UNKNOWN_END_CODES:
        ctx.guard.block(intent_id, group_id, OP_NAME, str(code))
        note_unresolved(ctx, intent_id, group_id, env)
        raise ServerOpError(
            as_error_code(str(code)),
            f"the restore of checkpoint {checkpoint_id} ended without an answer ({code}); it is "
            "not known whether the sequence was replaced. This server now refuses writes until "
            f"journal.check (group={group_id!r}) has read Flame again.",
            result={"checkpoint": checkpoint_id, "group": group_id},
            hint=env.get("hint"),
            journal={"group": group_id, "id": intent_id},
        )
    return _finish(
        ctx,
        env,
        record=record,
        intent_id=intent_id,
        group_id=group_id,
        pre_id=pre_id,
        fingerprint=fingerprint,
        current_fp=current_fp,
        forced=forced,
    )


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
    flame_ok = env.get("ok") is True
    matches = value.get("fingerprintMatches") is True
    ok = flame_ok and matches
    restored = _selector_row(value.get("restored"))
    moved = value.get("moved") is True
    rolled = value.get("rolledBack")
    pre_recorded = False
    journal_error: str | None = None
    try:
        if moved and not (isinstance(rolled, dict) and rolled.get("done") is True):
            ctx.journal.append(
                checkpoint_record(
                    pre_id,
                    group=group_id,
                    label=f"before restoring {checkpoint_id}",
                    source=value.get("restored"),
                    stash=value.get("preStash"),
                    fingerprint=value.get("fingerprintBefore"),
                    segment_count=None,
                    displayed=value.get("displayedBefore"),
                    replacedUid=value.get("replacedUid"),
                    orphaned=restored is None,
                )
            )
            pre_recorded = True
    except OSError as exc:
        journal_error = str(exc)
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
    outcome: dict[str, Any] = {
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
    try:
        ctx.journal.append(outcome)
    except OSError as exc:
        journal_error = str(exc)
        ctx.guard.block(intent_id, group_id, OP_NAME, "JOURNAL_OUTCOME_NOT_WRITTEN")
    result: dict[str, Any] = {
        "group": group_id,
        "checkpoint": checkpoint_id,
        "matched": matches,
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
        "fingerprintAfter": after_fp,
        "expectedFingerprint": fingerprint,
        "fingerprintMatches": value.get("fingerprintMatches"),
        "preFingerprintMatches": value.get("preFingerprintMatches"),
        "position": value.get("position"),
        "rolledBack": rolled,
        "notRestored": list(NOT_RESTORED),
    }
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
    if ok:
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
    ctx: ServerContext, index: Index, group: str, view: IntentView, force: bool
) -> ServerResult:
    """Restore the checkpoint a timeline intent names (an edit: its checkpoint; a restore: its
    pre-restore checkpoint), but only while the target's fingerprint is the one the run left."""
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
    outcome = view.outcome or {}
    target_uid = outcome.get("targetUid") or view.intent.get("targetUid")
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
        reverts=group,
        reverts_intent=None if covers_group else view.id,
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
            "see cannot be ruled out"
        )
    else:
        verdict, finding = STATE_DIVERGED, "other"
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
            "target": target,
        }
    )
    ctx.guard.release(view.id)
    return entry
