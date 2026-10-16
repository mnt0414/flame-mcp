"""Stages a, b, d, e, f, h and i of the checkpoint / trim real-Flame e2e (fingerprint, checkpoint,
revert, the Timeline, conflict, a clip's segment, journal.check); see p3_2a_checkpoint_e2e.py. Stage
c (the trim cases) is in p3_2a_e2e_trim.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, do, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_1a_read_e2e import fill, tx_val  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    EFFECT_SET_CODE,
    FP_ATTR,
    LETTERBOX_RATIO,
    LETTERBOX_TYPE,
    OPEN_CODE,
    TRANSPORT_TRIM_CODE,
    call,
    check_group,
    checkpoint_records,
    ckpt_library,
    expect_error,
    fingerprint,
    groups_now,
    intent_of,
    make_s1,
    media_row,
    name_of,
    names_of,
    reel_name_today,
    revert,
    run_call,
    seg_at,
    sel,
    stash_uids,
    synthetic_open_intent,
    track_rows,
    tx_children,
    tx_displayed,
    tx_find_uid,
    tx_stash,
    tx_struct,
    uids_of,
)

# -- a: the fingerprint


async def stage_a(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p32a-a") as session:
        struct = await tx_struct(ctx, ids["fWt"])
        first = await run_call(session, ctx, "a1_fingerprint")
        second = await run_call(session, ctx, "a1_fingerprint")
        res = _result_of(first)
        counts = [len(track) for track in (struct or [[]])[0]]
        effects = sum(len(r["fx"]) for track in (struct or [[]])[0] for r in track)
        rec(
            ctx,
            "a1_the_fingerprint_is_stable_and_counts_what_transport_counts",
            first.get("ok") is True
            and _result_of(second).get("fingerprint") == res.get("fingerprint")
            and len(str(res.get("fingerprint"))) == 64
            and res.get("versions") == [counts]
            and res.get("segmentCount") == sum(counts)
            and res.get("effectCount") == effects
            and res.get("unreadable") == 0,
            error=first.get("error"),
            result={k: v for k, v in res.items() if k != "target"},
            transportCounts=counts,
            transportEffects=effects,
        )
        target = res.get("target") or {}
        rec(
            ctx,
            "a2_it_names_what_it_read",
            target.get("uid") == ids["fUid"]
            and target.get("wiretapId") == ids["fWt"]
            and (target.get("parent") or {}).get("name") == "SEQ"
            and res.get("stable") == sel(ids["fWt"]),
            target=target,
        )
        base = res.get("fingerprint")
        clip1 = media_row(struct, 0, "clip1")
        if clip1 is None:
            rec(ctx, "a3_the_segment_to_edit_is_there", False, struct=struct)
            return
        edited = await tx_val(
            ctx,
            fill(TRANSPORT_TRIM_CODE, WT=ids["fWt"], TRACK=0, RECORD_IN=clip1["rin"], N=2),
            "p32a_transport_trim",
        )
        after_trim = await fingerprint(session, ctx, ids["fWt"])
        rec(
            ctx,
            "a3_a_trim_by_transport_changes_the_fingerprint",
            after_trim is not None and after_trim != base,
            transportTrim=edited,
        )
        key, value = FP_ATTR
        attr = await call(
            session,
            ctx,
            "segment.set_attributes",
            {"target": seg_at(sel(ids["fWt"]), 0, clip1["rin"]), "values": {key: value}},
        )
        after_attr = await fingerprint(session, ctx, ids["fWt"])
        rec(
            ctx,
            "a4_a_segment_attribute_does_not_change_the_fingerprint",
            attr.get("ok") is True and after_attr == after_trim,
            error=attr.get("error"),
            attribute=key,
        )
        letterbox = media_row(struct, 0, "clip2")
        if letterbox is None:
            rec(ctx, "a5_the_segment_with_the_effect_is_there", False)
            return
        setter = fill(
            EFFECT_SET_CODE,
            WT=ids["fWt"],
            TRACK=0,
            RECORD_IN=letterbox["rin"],
            TYPE=LETTERBOX_TYPE,
        )
        changed = await tx_val(
            ctx, fill(setter, KEY="ratio", VALUE=2.35), "p32a_effect_ratio_changed"
        )
        with_effect = await fingerprint(session, ctx, ids["fWt"])
        restored = await tx_val(
            ctx, fill(setter, KEY="ratio", VALUE=LETTERBOX_RATIO), "p32a_effect_ratio_back"
        )
        back = await fingerprint(session, ctx, ids["fWt"])
        rec(
            ctx,
            "a5_an_effect_attribute_changes_the_fingerprint_and_back_again",
            with_effect is not None and with_effect != after_trim and back == after_trim,
            ratioChanged=changed,
            ratioBack=restored,
            fingerprintMovedByTheRatio=with_effect is not None and with_effect != after_trim,
            fingerprintBackWithTheRatio=back == after_trim,
        )
        reel = await do(session, ctx, "timeline.fingerprint", {"target": sel(ids["seqReelWt"])})
        rec(
            ctx,
            "a6_a_reel_is_not_a_sequence",
            expect_error(reel, "SELECTOR_TYPE"),
            error=reel.get("error"),
        )


# -- b: the checkpoint


async def stage_b(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p32a-b") as session:
        before = await tx_stash(ctx)
        rec(
            ctx,
            "b0_the_stash_library_does_not_exist_yet",
            before is not None and before["libraries"] == 0,
            stash=before,
        )
        s1_struct = await tx_struct(ctx, ids["s1Wt"])
        s1_fp = await fingerprint(session, ctx, ids["s1Wt"])
        reel = reel_name_today()
        first = await run_call(session, ctx, "b1_checkpoint_first")
        after = await tx_stash(ctx)
        res = _result_of(first)
        stash = res.get("stash") or {}
        entry_struct = await tx_struct(ctx, stash.get("wiretapId", "/wt/none"))
        entries = (after or {}).get("entries") or []
        rec(
            ctx,
            "b1_the_first_checkpoint_makes_the_library_and_the_reel_and_copies_into_them",
            first.get("ok") is True
            and res.get("libraryCreated") is True
            and res.get("reelCreated") is True
            and after is not None
            and after["libraries"] == 1
            and after["reels"] == [reel]
            and len(entries) == 1
            and entries[0][3] == stash.get("uid")
            and entries[0][3] != ids["s1Uid"]
            and stash.get("library") == ckpt_library(ctx)
            and stash.get("reel") == reel
            and entry_struct == s1_struct
            and res.get("fingerprint") == s1_fp
            and res.get("stashMatches") is True
            and (res.get("source") or {}).get("uid") == ids["s1Uid"],
            error=first.get("error"),
            stash=stash,
            after=after,
        )
        records = checkpoint_records(ctx)
        record = next((r for r in records if r.get("id") == res.get("checkpoint")), {})
        rec(
            ctx,
            "b1_the_journal_record_names_the_source_the_stash_and_the_fingerprint",
            record.get("kind") == "timeline"
            and record.get("label") == "p32a first"
            and (record.get("source") or {}).get("uid") == ids["s1Uid"]
            and (record.get("source") or {}).get("wiretapId") == ids["s1Wt"]
            and ((record.get("source") or {}).get("parent") or {}).get("name") == "SEQ"
            and (record.get("stash") or {}).get("uid") == stash.get("uid")
            and (record.get("stash") or {}).get("wiretapId") == stash.get("wiretapId")
            and record.get("fingerprint") == s1_fp
            and record.get("displayed") in (True, False)
            and isinstance(record.get("segmentCount"), int),
            record=record,
        )
        second = await run_call(session, ctx, "b2_checkpoint_second")
        again = await tx_stash(ctx)
        rec(
            ctx,
            "b2_the_second_checkpoint_reuses_the_library_and_the_reel",
            second.get("ok") is True
            and _result_of(second).get("libraryCreated") is False
            and _result_of(second).get("reelCreated") is False
            and again is not None
            and again["libraries"] == 1
            and again["reels"] == [reel]
            and len(again["entries"]) == 2,
            error=second.get("error"),
            stash=again,
        )
        uids_before = stash_uids(again)
        groups = groups_now(ctx)
        count = len(checkpoint_records(ctx))
        in_library = await run_call(session, ctx, "b3_checkpoint_in_a_library")
        reel_target = await run_call(session, ctx, "b4_checkpoint_of_a_reel")
        rec(
            ctx,
            "b3_a_target_in_a_library_or_a_reel_is_refused_and_nothing_is_added",
            expect_error(in_library, "PRECONDITION_UNMET")
            and "inside a library" in str(in_library.get("error"))
            and expect_error(reel_target, "SELECTOR_TYPE")
            and stash_uids(await tx_stash(ctx)) == uids_before
            and len(checkpoint_records(ctx)) == count
            and groups_now(ctx) == groups,
            library=in_library.get("error"),
            reel=reel_target.get("error"),
        )
        listing = await run_call(session, ctx, "b5_list_checkpoints")
        items = _result_of(listing).get("items") or []
        mine = [
            i
            for i in items
            if i.get("id") in (res.get("checkpoint"), _result_of(second).get("checkpoint"))
        ]
        rec(
            ctx,
            "b5_list_checkpoints_shows_them_with_kind_timeline",
            listing.get("ok") is True
            and len(mine) == 2
            and all(
                i.get("kind") == "timeline" and (i.get("stash") or {}).get("uid") for i in mine
            ),
            kinds=[i.get("kind") for i in items],
        )
        ids["firstCheckpoint"] = res.get("checkpoint")


# -- d: revert


async def make_for(ctx: Ctx, tag: str, call_id: str) -> dict[str, Any] | None:
    """A fresh S1-like sequence, the segment `call_id` will trim registered in ctx.ids."""
    made = await make_s1(ctx, tag)
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    row = media_row(struct, 0, "clip2")
    if row is None:
        rec(ctx, f"{tag}_the_segment_to_trim_is_there", False, track0=track_rows(struct, 0))
        return None
    ctx.ids.setdefault("case", {})[call_id] = {"wt": made["wt"], "rin": row["rin"]}
    return {**made, "struct": struct}


async def stage_d(ctx: Ctx) -> None:
    ids = ctx.ids
    made = await make_for(ctx, "REV", "d1_trim_for_revert")
    if made is None:
        return
    async with server_session(ctx, "p32a-d") as session:
        wiretap = made["wt"]
        fp0 = await fingerprint(session, ctx, wiretap)
        children0 = await tx_children(ctx, ids["seqReelWt"])
        env = await run_call(session, ctx, "d1_trim_for_revert")
        struct1 = await tx_struct(ctx, wiretap)
        fp1 = await fingerprint(session, ctx, wiretap)
        rec(
            ctx,
            "d1_the_trim_changed_the_sequence",
            env.get("ok") is True and fp1 != fp0 and struct1 != made["struct"],
            error=env.get("error"),
        )
        back = await revert(session, ctx, env)
        res = _result_of(back)
        restored = res.get("restored") or {}
        children1 = await tx_children(ctx, ids["seqReelWt"])
        struct2 = await tx_struct(ctx, restored.get("wiretapId", "/wt/none"))
        old = await do(session, ctx, "timeline.fingerprint", {"target": sel(wiretap)})
        fp2 = await fingerprint(session, ctx, restored.get("wiretapId", "/wt/none"))
        rec(
            ctx,
            "d2_revert_puts_the_sequence_back_the_fingerprint_and_the_segments_are_the_old_ones",
            back.get("ok") is True
            and res.get("matched") is True
            and fp2 == fp0
            and struct2 == made["struct"]
            and res.get("fingerprintMatches") is True
            and res.get("reverted") == (env.get("journal") or {}).get("group"),
            error=back.get("error"),
            notRestored=res.get("notRestored"),
        )
        rec(
            ctx,
            "d3_the_restored_sequence_has_a_new_uid_and_wiretap_id_and_the_old_selector_is_empty",
            restored.get("uid") not in (None, made["uid"])
            and restored.get("wiretapId") not in (None, wiretap)
            and restored.get("selector") == sel(restored.get("wiretapId", ""))
            and expect_error(old, "SELECTOR_EMPTY")
            and await tx_find_uid(ctx, made["uid"]) is None
            and await tx_find_uid(ctx, restored.get("uid", "")) == "PySequence",
            restored=restored,
            old=old.get("error"),
        )
        rec(
            ctx,
            "d4_the_restored_sequence_is_at_the_end_of_its_reel",
            uids_of(children1)[-1:] == [restored.get("uid")]
            and made["uid"] not in uids_of(children1)
            and len(children1 or []) == len(children0 or [])
            and [u for u in uids_of(children0) if u != made["uid"]]
            == [u for u in uids_of(children1) if u != restored.get("uid")]
            and (res.get("position") or {}).get("index") == len(children1 or []) - 1,
            before=names_of(children0),
            after=names_of(children1),
            position=res.get("position"),
        )
        pre_id = res.get("preCheckpoint")
        pre = next((r for r in checkpoint_records(ctx) if r.get("id") == pre_id), {})
        stash = await tx_stash(ctx)
        edited_entry = next(
            (
                e
                for e in (stash or {}).get("entries") or []
                if e[3] == (res.get("preStash") or {}).get("uid")
            ),
            None,
        )
        edited_struct = await tx_struct(ctx, edited_entry[4]) if edited_entry else None
        original_entry = next(
            (
                e
                for e in (stash or {}).get("entries") or []
                if e[3] == (env.get("journal") or {}).get("stash", {}).get("uid")
            ),
            None,
        )
        original_struct = await tx_struct(ctx, original_entry[4]) if original_entry else None
        rec(
            ctx,
            "d5_the_edited_sequence_stays_in_the_stash_as_the_pre_restore_checkpoint",
            isinstance(pre_id, str)
            and pre.get("kind") == "timeline"
            and pre.get("fingerprint") == fp1
            and (pre.get("source") or {}).get("uid") == restored.get("uid")
            and edited_struct == struct1
            and original_struct == made["struct"]
            and res.get("undo")
            == {"op": "journal.restore_checkpoint", "args": {"checkpoint": pre_id}},
            preCheckpoint=pre_id,
            preStash=res.get("preStash"),
        )
        undone = await call(session, ctx, "journal.restore_checkpoint", {"checkpoint": pre_id})
        ures = _result_of(undone)
        urestored = ures.get("restored") or {}
        rec(
            ctx,
            "d6_restoring_the_pre_restore_checkpoint_gives_back_the_edited_state",
            undone.get("ok") is True
            and await fingerprint(session, ctx, urestored.get("wiretapId", "/wt/none")) == fp1
            and await tx_struct(ctx, urestored.get("wiretapId", "/wt/none")) == struct1,
            error=undone.get("error"),
        )
        again = await revert(session, ctx, env)
        rec(
            ctx,
            "d7_a_group_is_reverted_once",
            again.get("ok") is False and "already reverted" in str(again.get("error")),
            error=again.get("error"),
        )
        moves = await tx_children(ctx, ids["seqReelWt"])
        stale = await revert(session, ctx, back)
        rec(
            ctx,
            "d8_a_revert_whose_result_was_changed_since_is_a_conflict_and_nothing_moves",
            expect_error(stale, "CONFLICT")
            and uids_of(await tx_children(ctx, ids["seqReelWt"])) == uids_of(moves),
            error=stale.get("error"),
        )


# -- e: the Timeline


async def stage_e(ctx: Ctx) -> None:
    ids = ctx.ids
    made = await make_for(ctx, "DISP", "e1_trim_the_displayed_sequence")
    if made is None:
        return
    async with server_session(ctx, "p32a-e") as session:
        opened = await tx_val(ctx, fill(OPEN_CODE, WT=made["wt"]), "p32a_open")
        shown = await tx_displayed(ctx)
        rec(
            ctx,
            "e0_open_shows_the_sequence_in_the_timeline",
            isinstance(opened, dict)
            and opened.get("returned") == "True"
            and (shown or {}).get("uid") == made["uid"],
            opened=opened,
            displayed=shown,
        )
        env = await run_call(session, ctx, "e1_trim_the_displayed_sequence")
        checkpoint = next(
            (
                r
                for r in checkpoint_records(ctx)
                if r.get("id") == (env.get("journal") or {}).get("checkpoint")
            ),
            {},
        )
        rec(
            ctx,
            "e1_the_checkpoint_records_that_the_sequence_was_shown",
            env.get("ok") is True and checkpoint.get("displayed") is True,
            error=env.get("error"),
        )
        back = await revert(session, ctx, env)
        res = _result_of(back)
        after = await tx_displayed(ctx)
        rec(
            ctx,
            "e2_after_a_revert_the_timeline_shows_the_restored_sequence",
            back.get("ok") is True
            and res.get("displayedBefore") is True
            and res.get("reopened") is True
            and after is not None
            and after.get("uid") == (res.get("restored") or {}).get("uid")
            and after.get("uid") != made["uid"],
            error=back.get("error"),
            displayed=after,
            restored=res.get("restored"),
        )
        other = await make_for(ctx, "DISP2", "e1_trim_the_displayed_sequence")
        if other is None:
            return
        shown_run = await shown_trim_revert(
            session, ctx, "e3", "e1_trim_the_displayed_sequence", "s2"
        )
        env2, back2, res2, shown2 = shown_run
        children = await tx_children(ctx, ids["seqReelWt"])
        order = [n for n in names_of(children) if n.endswith(("_S2", "_S3"))]
        rec(
            ctx,
            "e3_another_sequence_that_was_shown_stays_shown_after_a_trim_and_a_revert",
            env2.get("ok") is True
            and back2.get("ok") is True
            and res2.get("displayedBefore") is False
            and res2.get("reopened") is None
            and (res2.get("shownBefore") or {}).get("uid") == ids["s2Uid"]
            and (res2.get("shownAfter") or {}).get("uid") == ids["s2Uid"]
            and (shown2 or {}).get("uid") == ids["s2Uid"],
            error=back2.get("error"),
            displayed=shown2,
            shownBefore=res2.get("shownBefore"),
            shownAfterRestore=res2.get("shownAfterRestore"),
            shownAfter=res2.get("shownAfter"),
            reshown=res2.get("reshown"),
        )
        rec(
            ctx,
            "e4_the_order_of_the_other_sequences_in_the_reel_is_kept",
            order == [name_of(ctx, "S2"), name_of(ctx, "S3")]
            and names_of(children)[-1:] == [name_of(ctx, "DISP2")],
            order=names_of(children),
        )
        # the same with the edited sequence IN FRONT of the shown one in its reel (S1 is before S2;
        # a real Flame switched the display when such a sequence was moved out)
        ids.setdefault("case", {})["e5_trim_in_front_of_the_shown_one"] = {
            "wt": ids["s1Wt"],
            "rin": (media_row(ids.get("s1Struct"), 0, "clip2") or {}).get("rin", 48),
        }
        env5, back5, res5, shown5 = await shown_trim_revert(
            session, ctx, "e5", "e5_trim_in_front_of_the_shown_one", "s2"
        )
        rec(
            ctx,
            "e5_a_sequence_in_front_of_the_shown_one_is_restored_and_the_shown_one_stays_shown",
            env5.get("ok") is True
            and back5.get("ok") is True
            and (res5.get("shownBefore") or {}).get("uid") == ids["s2Uid"]
            and (res5.get("shownAfter") or {}).get("uid") == ids["s2Uid"]
            and (shown5 or {}).get("uid") == ids["s2Uid"]
            and (
                (res5.get("shownAfterRestore") or {}).get("uid") == ids["s2Uid"]
                or res5.get("reshown") is True
            ),
            error=back5.get("error"),
            shownBefore=res5.get("shownBefore"),
            shownAfterRestore=res5.get("shownAfterRestore"),
            shownAfter=res5.get("shownAfter"),
            reshown=res5.get("reshown"),
        )
        restored = res5.get("restored") or {}
        if restored.get("uid"):  # S1 has a new identity now
            ids.update(s1Wt=restored.get("wiretapId", ids["s1Wt"]), s1Uid=restored["uid"])


async def shown_trim_revert(
    session: Any, ctx: Ctx, tag: str, call_id: str, shown_key: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Show `shown_key`'s sequence (S2 / S3), trim and revert the sequence `call_id` names, and
    OBSERVE what the Timeline showed after each hand: which one switches it is read on a real
    Flame, not assumed."""
    ids = ctx.ids
    await tx_val(ctx, fill(OPEN_CODE, WT=ids[f"{shown_key}Wt"]), f"p32a_{tag}_open")
    after_open = await tx_displayed(ctx)
    env = await run_call(session, ctx, call_id)
    after_trim = await tx_displayed(ctx)
    back = await revert(session, ctx, env)
    after_revert = await tx_displayed(ctx)
    res = _result_of(back)
    observe(
        ctx,
        f"{tag}_what_the_timeline_showed_after_each_hand",
        afterOpen=(after_open or {}).get("name"),
        afterTrim=(after_trim or {}).get("name"),
        afterRevert=(after_revert or {}).get("name"),
        shownBefore=res.get("shownBefore"),
        shownAfterRestore=res.get("shownAfterRestore"),
        shownAfter=res.get("shownAfter"),
        reshown=res.get("reshown"),
        displayedBefore=res.get("displayedBefore"),
    )
    return env, back, res, after_revert


# -- f: conflict


async def stage_f(ctx: Ctx) -> None:
    ids = ctx.ids
    made = await make_for(ctx, "CONF", "f1_trim_then_a_transport_edit")
    if made is None:
        return
    async with server_session(ctx, "p32a-f") as session:
        wiretap = made["wt"]
        fp0 = await fingerprint(session, ctx, wiretap)
        env = await run_call(session, ctx, "f1_trim_then_a_transport_edit")
        clip1 = media_row(made["struct"], 0, "clip1")
        await tx_val(
            ctx,
            fill(
                TRANSPORT_TRIM_CODE, WT=wiretap, TRACK=0, RECORD_IN=(clip1 or {}).get("rin", 0), N=2
            ),
            "p32a_transport_edit_after_the_trim",
        )
        fp_changed = await fingerprint(session, ctx, wiretap)
        children0 = await tx_children(ctx, ids["seqReelWt"])
        struct0 = await tx_struct(ctx, wiretap)
        stash0 = stash_uids(await tx_stash(ctx))
        groups = groups_now(ctx)
        refused = await revert(session, ctx, env)
        rec(
            ctx,
            "f1_a_changed_sequence_is_a_conflict_and_nothing_moves",
            env.get("ok") is True
            and expect_error(refused, "CONFLICT")
            and _result_of(refused).get("refused") is True
            and uids_of(await tx_children(ctx, ids["seqReelWt"])) == uids_of(children0)
            and await tx_struct(ctx, wiretap) == struct0
            and await fingerprint(session, ctx, wiretap) == fp_changed
            and stash_uids(await tx_stash(ctx)) == stash0
            and groups_now(ctx) == groups,
            error=refused.get("error"),
        )
        forced = await revert(session, ctx, env, force=True)
        res = _result_of(forced)
        group = (forced.get("journal") or {}).get("group")
        intent = intent_of(ctx, group)
        rec(
            ctx,
            "f2_force_restores_it_anyway_and_the_intent_says_so",
            forced.get("ok") is True
            and await fingerprint(
                session, ctx, (res.get("restored") or {}).get("wiretapId", "/wt/none")
            )
            == fp0
            and (intent.get("forced") or {}).get("used") is True
            and (res.get("forced") or {}).get("used") is True,
            error=forced.get("error"),
            forced=intent.get("forced"),
        )


# -- h: a segment inside a clip


async def stage_h(ctx: Ctx) -> None:
    ids = ctx.ids
    if "ccWt" not in ids:
        rec(ctx, "h0_the_clip_copy_is_there", False)
        return
    ids.setdefault("case", {})["h1_trim_inside_a_clip"] = {"wt": ids["ccWt"]}
    async with server_session(ctx, "p32a-h") as session:
        struct0 = await tx_struct(ctx, ids["ccWt"])
        fp0 = await fingerprint(session, ctx, ids["ccWt"])
        env = await run_call(session, ctx, "h1_trim_inside_a_clip")
        struct1 = await tx_struct(ctx, ids["ccWt"])
        rec(
            ctx,
            "h1_a_segment_inside_a_clip_on_the_desktop_can_be_trimmed",
            env.get("ok") is True and struct1 != struct0,
            error=env.get("error"),
            before=struct0,
            after=struct1,
        )
        back = await revert(session, ctx, env)
        res = _result_of(back)
        restored = res.get("restored") or {}
        rec(
            ctx,
            "h2_revert_puts_the_clip_back_with_a_new_uid",
            back.get("ok") is True
            and restored.get("type") == "PyClip"
            and restored.get("uid") not in (None, ids["ccUid"])
            and await fingerprint(session, ctx, restored.get("wiretapId", "/wt/none")) == fp0
            and await tx_struct(ctx, restored.get("wiretapId", "/wt/none")) == struct0,
            error=back.get("error"),
            restored=restored,
            reopened=res.get("reopened"),
        )


# -- i: journal.check


async def stage_i(ctx: Ctx) -> None:
    case = (ctx.ids.get("case") or {}).get("c1_head_shorten") or {}
    group = case.get("group")
    if not group:
        rec(ctx, "i0_a_landed_trim_group_to_copy_is_there", False)
        return
    async with server_session(ctx, "p32a-i") as session:
        now = await fingerprint(session, ctx, case["wt"])
        before = intent_of(ctx, group).get("fingerprintBefore")
        unchanged = await check_group(
            session,
            ctx,
            synthetic_open_intent(ctx, group, fingerprintBefore=now, fingerprintExpected=None),
        )
        landed = await check_group(
            session,
            ctx,
            synthetic_open_intent(ctx, group, fingerprintBefore=before, fingerprintExpected=now),
        )
        elsewhere = await check_group(
            session,
            ctx,
            synthetic_open_intent(
                ctx, group, fingerprintBefore=before, fingerprintExpected="0" * 64
            ),
        )
        rec(
            ctx,
            "i1_journal_check_reads_the_sequence_again_and_decides_not_landed_landed_diverged",
            unchanged.get("verdict") == "not_landed"
            and landed.get("verdict") == "landed"
            and elsewhere.get("verdict") == "diverged"
            and (landed.get("current") or {}).get("fingerprint") == now,
            unchanged=unchanged.get("verdict"),
            landed=landed.get("verdict"),
            elsewhere=elsewhere.get("verdict"),
        )
        gone = await check_group(
            session,
            ctx,
            synthetic_open_intent(ctx, group, target=sel("/wt/p32a/no/such/sequence")),
        )
        rec(
            ctx,
            "i2_an_intent_whose_sequence_is_not_found_is_diverged_not_guessed",
            gone.get("verdict") == "diverged" and "not found" in str(gone.get("detail")),
            detail=gone.get("detail"),
        )
        observe(
            ctx,
            "i3_the_stash_library_and_reel",
            libraryName=ckpt_library(ctx),
            reel=reel_name_today(),
        )
