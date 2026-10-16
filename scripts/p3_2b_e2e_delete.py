"""Stages d, m and g of the plan / delete real-Flame e2e: desktop.delete (clips, sequences, a list,
a library clip refused, what the Timeline shows, the refusals, a parent that is gone), the Timeline
after a desktop.move, and journal.purge_checkpoints; see p3_2b_plan_delete_e2e.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p2_2a_journal_e2e import Ctx, do, rec, server_session  # noqa: E402
from p2_4_common import journal_records, observe  # noqa: E402
from p3_1a_read_e2e import fill, tx_val  # noqa: E402
from p3_2b_e2e_common import (  # noqa: E402
    BATCH_CODE,
    DELETE_UID_CODE,
    DELETE_WT_CODE,
    OPEN_CODE,
    PLANT_CODE,
    checkpoint_records,
    ckpt_library,
    copy_into,
    expect_error,
    fingerprint,
    make_reel,
    mine,
    name_of,
    names_of,
    reel_name_today,
    revert,
    run_call,
    state_snapshot,
    tx_children,
    tx_displayed,
    tx_find_uid,
    tx_stash,
    tx_struct,
    uids_of,
)


async def make_copies(ctx: Ctx, specs: list[tuple[str, str]], dest_wt: str) -> list[dict[str, Any]]:
    """Copies (by transport) of the sources `specs` = [(source wiretap id, label)] in `dest_wt`,
    each with its struct and fingerprint read before anything is done to it."""
    made = []
    for source, label in specs:
        copy = await copy_into(ctx, source, dest_wt)
        rec(ctx, f"prepare_copy_{label}", copy is not None)
        if copy is None:
            continue
        copy["struct"] = await tx_struct(ctx, copy["wt"])
        made.append(copy)
    return made


def stash_rows(stash: dict[str, Any] | None) -> list[list[Any]]:
    return list((stash or {}).get("entries") or [])


async def delete_case(
    session: Any, ctx: Ctx, tag: str, items: list[dict[str, Any]], parent_wt: str
) -> dict[str, Any] | None:
    """Delete `items` (copies made by make_copies) in one call and check, by transport, that they
    are gone, that each has a copy in the stash with its own segments, and that the journal records
    the stash as timeline checkpoints; then revert and check the way back."""
    if not items:
        return None
    fps = [await fingerprint(session, ctx, i["wt"]) for i in items]
    stash0 = stash_rows(await tx_stash(ctx))
    children0 = await tx_children(ctx, parent_wt)
    env = await run_call(session, ctx, "d_delete", case={"targets": [i["wt"] for i in items]})
    res = mine(env)
    stash1 = stash_rows(await tx_stash(ctx))
    children1 = await tx_children(ctx, parent_wt)
    new = [e for e in stash1 if e[3] not in {x[3] for x in stash0}]
    new_structs = [await tx_struct(ctx, e[4]) for e in new]
    recorded = {r["id"]: r for r in checkpoint_records(ctx) if r.get("kind") == "timeline"}
    deleted = res.get("deleted") or []
    gone = [await tx_find_uid(ctx, i["uid"]) for i in items]
    rec(
        ctx,
        f"{tag}_deleted_they_are_gone_each_has_a_stashed_copy_and_a_timeline_checkpoint",
        env.get("ok") is True
        and all(g is None for g in gone)
        and not set(uids_of(children1)) & {i["uid"] for i in items}
        and len(new) == len(items)
        and sorted(e[2] for e in new) == sorted(i["name"] for i in items)
        and all(s in new_structs for s in (i["struct"] for i in items))
        and [d["checkpoint"] in recorded for d in deleted] == [True] * len(items)
        and [d["from"]["uid"] for d in deleted] == [i["uid"] for i in items]
        and (res.get("partial") is False),
        error=env.get("error"),
        stashAdded=len(new),
        shown=(res.get("shownBefore"), res.get("shownAfterDelete"), res.get("shownAfter")),
    )
    groups = ctx.ids.setdefault("deleteGroups", [])
    groups.append({"group": (env.get("journal") or {}).get("group"), "tag": tag})
    back = await revert(session, ctx, env)
    rres = mine(back)
    restored = rres.get("reverted") or []
    children2 = await tx_children(ctx, parent_wt)
    after_fps = [
        await fingerprint(session, ctx, (r.get("to") or {}).get("wiretapId", "/x"))
        for r in restored
    ]
    after_structs = [
        await tx_struct(ctx, (r.get("to") or {}).get("wiretapId", "/x")) for r in restored
    ]
    stash2 = stash_rows(await tx_stash(ctx))
    rec(
        ctx,
        f"{tag}_revert_copies_them_back_into_their_parent_new_uids_same_fingerprints_at_the_end",
        back.get("ok") is True
        and len(restored) == len(items)
        and after_fps == fps
        and after_structs == [i["struct"] for i in items]
        and all(r["to"]["uid"] != i["uid"] for r, i in zip(restored, items, strict=True))
        and uids_of(children2)[-len(items) :] == [r["to"]["uid"] for r in restored]
        and len(children2 or []) == len(children0 or [])
        and len(stash2) == len(stash1),
        error=back.get("error"),
        names=names_of(children2),
    )
    return {"env": env, "back": back, "restored": restored}


async def library_clip_is_refused(
    session: Any, ctx: Ctx, lib_clip: list[dict[str, Any]], lib_reel_wt: str
) -> None:
    """A clip inside a library is not deleted by this server: refused before anything is copied
    or journaled, and the clip stays where it is."""
    if not lib_clip:
        return
    clip = lib_clip[0]
    before = await state_snapshot(ctx)
    children0 = await tx_children(ctx, lib_reel_wt)
    env = await run_call(session, ctx, "d_delete", case={"targets": [clip["wt"]]})
    children1 = await tx_children(ctx, lib_reel_wt)
    rec(
        ctx,
        "d4_a_clip_in_a_library_is_refused_and_nothing_is_copied_journaled_or_deleted",
        expect_error(env, "PRECONDITION_UNMET")
        and "never deleted" in str(env.get("error"))
        and await state_snapshot(ctx) == before
        and await tx_find_uid(ctx, clip["uid"]) is not None
        and uids_of(children1) == uids_of(children0),
        error=env.get("error"),
    )


async def stage_d(ctx: Ctx) -> None:
    ids = ctx.ids
    del_wt = await make_reel(ctx, "DEL")
    if del_wt is None:
        return
    ids["delReelWt"] = del_wt
    clips = await make_copies(ctx, [(ids["clipWt"]["clip1"], "clip_a")], del_wt)
    seqs = await make_copies(ctx, [(ids["s2Wt"], "seq_a"), (ids["s3Wt"], "seq_b")], del_wt)
    pair_clip = await make_copies(ctx, [(ids["clipWt"]["clip2"], "clip_b")], del_wt)
    lib_clip = await make_copies(ctx, [(ids["clipWt"]["clip1"], "library_clip")], ids["libReelWt"])
    async with server_session(ctx, "p32b-d") as session:
        await delete_case(session, ctx, "d1_clip", clips, del_wt)
        await delete_case(session, ctx, "d2_sequence", seqs[:1], del_wt)
        await delete_case(session, ctx, "d3_list_of_two", seqs[1:] + pair_clip, del_wt)
        await library_clip_is_refused(session, ctx, lib_clip, ids["libReelWt"])
        await shown_cases(session, ctx, del_wt)
        await refusals(session, ctx, del_wt)
        await parent_gone(session, ctx)


async def shown_cases(session: Any, ctx: Ctx, del_wt: str) -> None:
    ids = ctx.ids
    shown_seq, other_seq = await make_copies(
        ctx, [(ids["s2Wt"], "shown_seq"), (ids["s3Wt"], "other_seq")], del_wt
    )
    opened = await tx_val(ctx, fill(OPEN_CODE, WT=shown_seq["wt"]), "p32b_open_shown")
    displayed = await tx_displayed(ctx)
    env = await run_call(session, ctx, "d_delete", case={"targets": [shown_seq["wt"]]})
    res = mine(env)
    after = await tx_displayed(ctx)
    observe(
        ctx,
        "d5_what_the_timeline_showed_after_deleting_the_shown_sequence",
        opened=opened,
        displayedBefore=(displayed or {}).get("name"),
        shownBefore=res.get("shownBefore"),
        shownAfterDelete=res.get("shownAfterDelete"),
        shownAfter=res.get("shownAfter"),
        displayedByTransport=(after or {}).get("name"),
        reshown=res.get("reshown"),
    )
    back = await revert(session, ctx, env)
    rres = mine(back)
    restored = ((rres.get("reverted") or [{}])[0].get("to")) or {}
    shown_now = await tx_displayed(ctx)
    rec(
        ctx,
        "d5_a_shown_sequence_that_was_deleted_is_shown_again_after_the_revert",
        env.get("ok") is True
        and back.get("ok") is True
        and ((res.get("deleted") or [{}])[0].get("wasDisplayed") is True)
        and shown_now is not None
        and shown_now.get("uid") == restored.get("uid"),
        error=back.get("error"),
        displayed=shown_now,
        restored=restored,
        reshown=rres.get("reshown"),
        note=rres.get("reshownNote"),
    )
    await tx_val(ctx, fill(OPEN_CODE, WT=ids["s2Wt"]), "p32b_open_s2")
    env2 = await run_call(session, ctx, "d_delete", case={"targets": [other_seq["wt"]]})
    res2 = mine(env2)
    shown2 = await tx_displayed(ctx)
    observe(
        ctx,
        "d6_what_the_timeline_showed_after_deleting_another_sequence",
        shownBefore=res2.get("shownBefore"),
        shownAfterDelete=res2.get("shownAfterDelete"),
        shownAfter=res2.get("shownAfter"),
        reshown=res2.get("reshown"),
        displayedByTransport=(shown2 or {}).get("name"),
    )
    rec(
        ctx,
        "d6_deleting_another_sequence_leaves_the_timeline_on_the_one_that_was_shown",
        env2.get("ok") is True and shown2 is not None and shown2.get("uid") == ids["s2Uid"],
        error=env2.get("error"),
        displayed=shown2,
    )
    await revert(session, ctx, env2)


async def refusals(session: Any, ctx: Ctx, del_wt: str) -> None:
    ids = ctx.ids
    clip = await copy_into(ctx, ids["clipWt"]["clip1"], del_wt)
    batch = await tx_val(
        ctx,
        fill(BATCH_CODE, NAME=name_of(ctx, "BG"), SRC=ids["clipWt"]["clip1"]),
        "p32b_batch",
        timeout_s=60.0,
    )
    stash = stash_rows(await tx_stash(ctx))
    cases: list[tuple[str, list[str], str, str]] = [
        ("d7_a_reel", [del_wt], "PRECONDITION_UNMET", "only a clip or a sequence"),
        ("d7_a_reel_group", [ids["rgWt"]], "PRECONDITION_UNMET", "only a clip or a sequence"),
        (
            "d7_the_stash_library_entry",
            [stash[-1][4]] if stash else [],
            "PRECONDITION_UNMET",
            "purge",
        ),
    ]
    if isinstance(batch, dict) and isinstance(batch.get("clipWt"), str):
        if not batch["clipWt"].startswith("ERR:"):
            cases.append(("d7_a_clip_in_a_batch_reel", [batch["clipWt"]], "INVALID_ARGS", "Batch"))
    else:
        observe(ctx, "d7_no_batch_reel_clip_to_refuse", batch=batch)
    for name, targets, code, needle in cases:
        if not targets:
            observe(ctx, f"{name}_has_nothing_to_aim_at")
            continue
        before = await state_snapshot(ctx)
        env = await run_call(session, ctx, "d_delete", case={"targets": targets})
        rec(
            ctx,
            f"{name}_is_refused_and_nothing_is_copied_or_journaled",
            expect_error(env, code)
            and needle in str(env.get("error"))
            and await state_snapshot(ctx) == before,
            error=env.get("error"),
        )
    if clip is not None:
        before = await state_snapshot(ctx)
        twice = await run_call(session, ctx, "d_delete_twice", case={"targets": [clip["wt"]]})
        rec(
            ctx,
            "d7_the_same_thing_twice_is_invalid_and_nothing_happens",
            expect_error(twice, "INVALID_ARGS")
            and await state_snapshot(ctx) == before
            and await tx_find_uid(ctx, clip["uid"]) is not None,
            error=twice.get("error"),
        )
        plan = await run_call(session, ctx, "d_plan_with_delete", case={"wt": clip["wt"]})
        rec(
            ctx,
            "d7_plan_run_refuses_desktop_delete",
            expect_error(plan, "INVALID_ARGS")
            and await state_snapshot(ctx) == before
            and await tx_find_uid(ctx, clip["uid"]) is not None,
            error=plan.get("error"),
        )


async def parent_gone(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    doomed = await make_reel(ctx, "DOOMED")
    if doomed is None:
        return
    clip = await copy_into(ctx, ids["clipWt"]["clip1"], doomed)
    if clip is None:
        return
    env = await run_call(session, ctx, "d_delete", case={"targets": [clip["wt"]]})
    await tx_val(ctx, fill(DELETE_WT_CODE, WT=doomed), "p32b_delete_parent")
    stash0 = await tx_stash(ctx)
    back = await revert(session, ctx, env)
    stash1 = await tx_stash(ctx)
    rec(
        ctx,
        "d8_revert_when_the_parent_is_gone_is_a_conflict_and_nothing_is_copied",
        env.get("ok") is True
        and expect_error(back, "CONFLICT")
        and "is gone" in str(back.get("error"))
        and await tx_find_uid(ctx, clip["uid"]) is None
        and stash_rows(stash1) == stash_rows(stash0),
        error=back.get("error"),
    )


# -- m: the Timeline after a move


async def stage_m(ctx: Ctx) -> None:
    ids = ctx.ids
    mv = await make_reel(ctx, "MV")
    mv2 = await make_reel(ctx, "MV2")
    if mv is None or mv2 is None:
        return
    a, b, _c = await make_copies(
        ctx, [(ids["s3Wt"], "move_a"), (ids["s2Wt"], "move_b"), (ids["s3Wt"], "move_c")], mv
    )
    async with server_session(ctx, "p32b-m") as session:
        await tx_val(ctx, fill(OPEN_CODE, WT=b["wt"]), "p32b_open_b")
        shown0 = await tx_displayed(ctx)
        env = await run_call(session, ctx, "m_move", case={"wt": a["wt"], "destWt": mv2})
        res = mine(env)
        shown1 = await tx_displayed(ctx)
        observe(
            ctx,
            "m1_what_the_timeline_showed_after_moving_the_sequence_in_front_of_the_shown_one",
            shownBefore=res.get("shownBefore"),
            shownAfterMove=res.get("shownAfterMove"),
            shownAfter=res.get("shownAfter"),
            reshown=res.get("reshown"),
        )
        rec(
            ctx,
            "m1_moving_the_sequence_in_front_of_the_shown_one_leaves_the_timeline_on_it",
            env.get("ok") is True
            and (shown0 or {}).get("uid") == b["uid"]
            and (shown1 or {}).get("uid") == b["uid"],
            error=env.get("error"),
            displayed=shown1,
        )
        back = await revert(session, ctx, env)
        shown2 = await tx_displayed(ctx)
        rec(
            ctx,
            "m2_the_revert_of_the_move_leaves_it_there_too",
            back.get("ok") is True and (shown2 or {}).get("uid") == b["uid"],
            error=back.get("error"),
            reshown=mine(back).get("reshown"),
        )


# -- g: the purge


async def stage_g(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p32b-g") as session:
        taken = await run_call(session, ctx, "g_checkpoint", case={"wt": ids["s2Wt"]})
        cp = mine(taken).get("checkpoint")
        planted = await tx_val(
            ctx, fill(PLANT_CODE, LIB=ckpt_library(ctx), SRC=ids["s3Wt"]), "p32b_plant"
        )
        if not isinstance(planted, dict):
            rec(ctx, "g0_a_copy_without_a_record_was_planted_in_the_stash", False, planted=planted)
            return
        before = stash_rows(await tx_stash(ctx))
        dry = await run_call(session, ctx, "g_purge_dry_run_by_default")
        dres = mine(dry)
        after_dry = stash_rows(await tx_stash(ctx))
        rec(
            ctx,
            "g1_the_default_is_a_dry_run_it_lists_candidates_and_the_unknown_copy_deletes_nothing",
            dry.get("ok") is True
            and dres.get("dryRun") is True
            and (dres.get("count") or 0) >= 1
            and cp in [c.get("id") for c in dres.get("candidates") or []]
            and planted["uid"] in [u.get("uid") for u in dres.get("unknown") or []]
            and after_dry == before,
            error=dry.get("error"),
            count=dres.get("count"),
            unknown=dres.get("unknown"),
        )
        real = await run_call(session, ctx, "g_purge_real", case={"token": dres.get("token")})
        rres = mine(real)
        left = stash_rows(await tx_stash(ctx))
        records = [r for r in journal_records(ctx) if r.get("type") == "purge"]
        listed = await do(session, ctx, "journal.list_checkpoints", {"limit": 200})
        flags = {i.get("id"): i.get("purged") for i in mine(listed).get("items") or []}
        rec(
            ctx,
            "g2_a_real_purge_deletes_the_recorded_copies_and_leaves_the_unknown_one",
            real.get("ok") is True
            and [row[3] for row in left] == [planted["uid"]]
            and await tx_find_uid(ctx, planted["uid"]) is not None
            and cp in [p.get("id") for p in rres.get("purged") or []]
            and cp in (records[-1].get("purged") if records else [])
            and flags.get(cp) is True
            and rres.get("reelsDeleted") == [],
            error=real.get("error"),
            purged=len(rres.get("purged") or []),
            skipped=rres.get("skipped"),
            left=len(left),
        )
        case = {"checkpoint": cp}
        refused = await run_call(session, ctx, "g_restore_a_purged_checkpoint", case=case)
        rec(
            ctx,
            "g3_a_purged_checkpoint_cannot_be_restored",
            expect_error(refused, "PRECONDITION_UNMET") and "purged" in str(refused.get("error")),
            error=refused.get("error"),
        )
        group = next((g["group"] for g in ids.get("deleteGroups", []) if g.get("group")), None)
        if group is not None:
            reverted = await do(session, ctx, "journal.revert", {"group": group})
            rec(
                ctx,
                "g4_a_delete_whose_stash_was_purged_cannot_be_reverted",
                reverted.get("ok") is False
                and reverted.get("errorCode") in ("CONFLICT", "PRECONDITION_UNMET"),
                error=reverted.get("error"),
            )
        await tx_val(ctx, fill(DELETE_UID_CODE, UID=planted["uid"]), "p32b_delete_planted")
        await run_call(session, ctx, "g_checkpoint", case={"wt": ids["s2Wt"]})
        fresh = await run_call(session, ctx, "g_purge_dry_run_by_default")
        again = await run_call(
            session, ctx, "g_purge_real", case={"token": mine(fresh).get("token")}
        )
        stash = await tx_stash(ctx)
        rec(
            ctx,
            "g5_the_date_reel_that_was_emptied_is_deleted_too",
            again.get("ok") is True
            and mine(again).get("reelsDeleted") == [reel_name_today()]
            and (stash or {}).get("reels") == [],
            error=again.get("error"),
            reelsDeleted=mine(again).get("reelsDeleted"),
            stash=stash,
        )
