"""Stage c (move) of the import / duplicate / move real-Flame e2e; see p3_1b_write_e2e.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import (  # noqa: E402
    _result_of,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do,
    rec,
    server_session,
)
from p2_4_common import (  # noqa: E402
    observe,
)
from p3_1a_read_e2e import (  # noqa: E402
    fill,
    tx_struct,
    tx_val,
)
from p3_1b_e2e_common import (  # noqa: E402
    DELETE_UID_CODE,
    DELETE_WT_CODE,
    MAKE_REEL_CODE,
    added,
    batch_refusal,
    check_group,
    err_text,
    expect_error,
    fresh_clip,
    groups_now,
    intent_of,
    make_sequence,
    name_of,
    names_of,
    revert,
    run_call,
    s1_puts,
    segments_of,
    sel,
    synthetic_open_intent,
    tx_find_uid,
    tx_find_wt,
    tx_list,
    uids_of,
    written_groups,
)

# -- c: move


async def move(session: Any, ctx: Ctx, sources: list[str], destination: str) -> dict[str, Any]:
    env = await do(
        session,
        ctx,
        "desktop.move",
        {"sources": [sel(s) for s in sources], "destination": sel(destination)},
    )
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": "desktop.move", "group": group})
    return env


async def stage_c(ctx: Ctx) -> None:
    async with server_session(ctx, "p31b-c") as session:
        await stage_c_one(session, ctx)
        await stage_c_more(session, ctx)
        await stage_c_revert(session, ctx)
        await stage_c_refusals(session, ctx)
        await stage_c_check(session, ctx)


async def stage_c_one(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    x = await fresh_clip(ctx, ids["dWt"])
    if x is None:
        rec(ctx, "c1_a_clip_to_move_could_not_be_made", False)
        return
    d_before = await tx_list(ctx, ids["dWt"])
    e_before = await tx_list(ctx, ids["eWt"])
    c1 = await move(session, ctx, [x["wt"]], ids["eWt"])
    d_after = await tx_list(ctx, ids["dWt"])
    e_after = await tx_list(ctx, ids["eWt"])
    res = _result_of(c1)
    row = (res.get("moved") or [{}])[0]
    to = row.get("to") or {}
    origin = row.get("from") or {}
    old_info = await do(session, ctx, "desktop.info", {"target": sel(x["wt"])})
    new_info = await do(session, ctx, "desktop.info", {"target": to.get("selector")})
    rec(
        ctx,
        "c1_a_clip_moves_to_another_reel_with_a_new_uid_and_wiretap_id_at_the_end",
        c1.get("ok") is True
        and len(res.get("moved") or []) == 1
        and origin.get("uid") == x["uid"]
        and origin.get("name") == x["name"]
        and (origin.get("parent") or {}).get("name") == "D"
        and (origin.get("parent") or {}).get("selector") == sel(ids["dWt"])
        and to.get("uid") not in (None, x["uid"])
        and to.get("wiretapId") != x["wt"]
        and await tx_find_uid(ctx, x["uid"]) is None
        and await tx_find_wt(ctx, x["wt"]) is None
        and uids_of(e_after)[-1:] == [to.get("uid")]
        and uids_of(added(e_before, e_after)) == [to.get("uid")]
        and x["uid"] not in uids_of(d_after)
        and len(d_after or []) == len(d_before or []) - 1
        and expect_error(old_info, "SELECTOR_EMPTY")
        and new_info.get("ok") is True
        and _result_of(new_info).get("uid") == to.get("uid")
        and any("NEW uid" in n for n in res.get("notes") or []),
        error=c1.get("error"),
        moved=res.get("moved"),
    )
    ids["c1"] = {"env": c1, "x": x, "to": to}


async def stage_c_more(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    s2 = await make_sequence(ctx, "S2", ids["dWt"], 2, s1_puts(ids))
    if s2 is not None:
        want = [await segments_of(session, ctx, s2["wt"], t) for t in (0, 1)]
        struct = await tx_struct(ctx, s2["wt"])
        c2 = await move(session, ctx, [s2["wt"]], ids["eWt"])
        to = ((_result_of(c2).get("moved") or [{}])[0]).get("to") or {}
        got = (
            [await segments_of(session, ctx, to["wiretapId"], t) for t in (0, 1)]
            if to.get("wiretapId")
            else None
        )
        rec(
            ctx,
            "c2_a_sequence_moves_with_its_segments_unchanged_and_a_new_uid",
            c2.get("ok") is True
            and to.get("type") == "PySequence"
            and to.get("uid") != s2["uid"]
            and got == want
            and struct == (await tx_struct(ctx, to["wiretapId"]) if to.get("wiretapId") else None),
            error=c2.get("error"),
        )
        ids["c2"] = {"env": c2, "seq": s2}
    y1 = await fresh_clip(ctx, ids["dWt"], "clip2")
    y2 = await fresh_clip(ctx, ids["dWt"], "clip3")
    if y1 and y2:
        e_before = await tx_list(ctx, ids["eWt"])
        c3 = await move(session, ctx, [y1["wt"], y2["wt"]], ids["eWt"])
        e_after = await tx_list(ctx, ids["eWt"])
        moved = _result_of(c3).get("moved") or []
        rec(
            ctx,
            "c3_a_list_of_two_moves_in_the_order_given_to_the_end",
            c3.get("ok") is True
            and [m["from"]["uid"] for m in moved] == [y1["uid"], y2["uid"]]
            and uids_of(e_after)[-2:] == [m["to"]["uid"] for m in moved]
            and names_of(added(e_before, e_after)) == [y1["name"], y2["name"]],
            error=c3.get("error"),
        )
    z = await fresh_clip(ctx, ids["dWt"])
    if z:
        lr2_before = await tx_list(ctx, ids["lr2Wt"])
        c4 = await move(session, ctx, [z["wt"]], ids["lr2Wt"])
        lr2_after = await tx_list(ctx, ids["lr2Wt"])
        to = ((_result_of(c4).get("moved") or [{}])[0]).get("to") or {}
        rec(
            ctx,
            "c4_a_desktop_clip_moves_into_a_library_reel",
            c4.get("ok") is True
            and uids_of(added(lr2_before, lr2_after)) == [to.get("uid")]
            and await tx_find_uid(ctx, z["uid"]) is None,
            error=c4.get("error"),
        )
    w = await fresh_clip(ctx, ids["lr2Wt"])
    if w:
        d_before = await tx_list(ctx, ids["dWt"])
        lr2_before = await tx_list(ctx, ids["lr2Wt"])
        c5 = await move(session, ctx, [w["wt"]], ids["dWt"])
        d_after = await tx_list(ctx, ids["dWt"])
        lr2_after = await tx_list(ctx, ids["lr2Wt"])
        rec(
            ctx,
            "c5_a_library_clip_is_not_moved_out_by_move_it_is_refused_and_nothing_changes",
            expect_error(c5, "PRECONDITION_UNMET")
            and "inside a library" in err_text(c5)
            and "desktop.copy" in err_text(c5)
            and await tx_find_uid(ctx, w["uid"]) == "PyClip"
            and uids_of(d_before) == uids_of(d_after)
            and uids_of(lr2_before) == uids_of(lr2_after)
            and not (c5.get("journal") or {}).get("group"),
            error=c5.get("error"),
        )


async def stage_c_revert(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    first = ids.get("c1")
    if first:
        x, to = first["x"], first["to"]
        d_before = await tx_list(ctx, ids["dWt"])
        e_before = await tx_list(ctx, ids["eWt"])
        out = await revert(session, ctx, first["env"])
        d_after = await tx_list(ctx, ids["dWt"])
        e_after = await tx_list(ctx, ids["eWt"])
        back = (_result_of(out).get("reverted") or [{}])[0].get("to") or {}
        info = await do(session, ctx, "desktop.info", {"target": back.get("selector")})
        old = await do(session, ctx, "desktop.info", {"target": to.get("selector")})
        rec(
            ctx,
            "c6_revert_of_a_move_puts_the_clip_back_with_yet_another_uid_and_returns_it",
            out.get("ok") is True
            and back.get("uid") not in (None, x["uid"], to.get("uid"))
            and uids_of(added(d_before, d_after)) == [back.get("uid")]
            and to.get("uid") not in uids_of(e_after)
            and len(e_after or []) == len(e_before or []) - 1
            and uids_of(d_after)[-1:] == [back.get("uid")]
            and info.get("ok") is True
            and expect_error(old, "SELECTOR_EMPTY")
            and any("NEW uids" in n for n in _result_of(out).get("notes") or []),
            error=out.get("error"),
            back=back,
        )
        again = await revert(session, ctx, first["env"])
        rec(
            ctx,
            "c6_a_second_revert_of_the_same_group_is_refused",
            expect_error(again, "PRECONDITION_UNMET"),
            error=again.get("error"),
        )
    p1 = await fresh_clip(ctx, ids["dWt"])
    p2 = await fresh_clip(ctx, ids["dWt"], "clip2")
    if p1 and p2:
        env = await move(session, ctx, [p1["wt"], p2["wt"]], ids["eWt"])
        moved = _result_of(env).get("moved") or []
        if len(moved) == 2:
            await tx_val(ctx, fill(DELETE_UID_CODE, UID=moved[1]["to"]["uid"]), "p31b_remove")
            e_before = await tx_list(ctx, ids["eWt"])
            out = await revert(session, ctx, env)
            e_after = await tx_list(ctx, ids["eWt"])
            rec(
                ctx,
                "c7_a_thing_removed_from_the_destination_is_a_conflict_and_nothing_is_moved",
                expect_error(out, "CONFLICT")
                and "not in the destination" in " ".join(_result_of(out).get("conflicts") or [])
                and uids_of(e_before) == uids_of(e_after)
                and moved[0]["to"]["uid"] in uids_of(e_after),
                error=out.get("error"),
            )
    q = await tx_val(
        ctx, fill(MAKE_REEL_CODE, RG=ids["rgWt"], NAME=name_of(ctx, "F")), "p31b_make_reel"
    )
    if isinstance(q, str) and not q.startswith("ERR:"):
        clip = await fresh_clip(ctx, q)
        if clip:
            env = await move(session, ctx, [clip["wt"]], ids["eWt"])
            await tx_val(ctx, fill(DELETE_WT_CODE, WT=q), "p31b_delete_reel")
            out = await revert(session, ctx, env)
            moved = (_result_of(env).get("moved") or [{}])[0].get("to") or {}
            rec(
                ctx,
                "c8_an_old_parent_that_is_gone_is_a_conflict_and_the_thing_stays",
                expect_error(out, "CONFLICT")
                and "gone" in err_text(out)
                and await tx_find_uid(ctx, moved.get("uid", "")) == "PyClip",
                error=out.get("error"),
            )


async def stage_c_refusals(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    x = await fresh_clip(ctx, ids["dWt"])
    if x is None:
        return
    ids.update(mvSrcWt=x["wt"], mvDestWt=ids["eWt"], mvSrcReelWt=ids["dWt"])
    before = groups_now(ctx)
    await batch_refusal(session, ctx, "c9_dest", "c9_move_into_a_batch_reel", "batch.add_clips")
    if ids.get("batchClipHasWt"):
        out = await run_call(session, ctx, "c9_move_out_of_a_batch_reel")
        rec(
            ctx,
            "c9_a_clip_inside_a_batch_reel_is_refused_as_a_source",
            expect_error(out, "INVALID_ARGS") and "Batch group" in err_text(out),
            error=out.get("error"),
        )
    else:
        observe(ctx, "c9_the_clip_in_the_batch_reel_has_no_wiretap_id")
    same = await run_call(session, ctx, "c10_move_into_its_own_reel")
    selection = await run_call(session, ctx, "c11_move_to_a_selection")
    reel = await run_call(session, ctx, "c12_move_a_reel")
    rec(
        ctx,
        "c10_same_reel_selection_destination_and_a_reel_source_are_refused_before_a_write",
        expect_error(same, "PRECONDITION_UNMET")
        and "already in" in err_text(same)
        and expect_error(selection, "INVALID_ARGS")
        and expect_error(reel, "SELECTOR_TYPE")
        and groups_now(ctx) == before
        and await tx_find_uid(ctx, x["uid"]) == "PyClip",
        sameError=same.get("error"),
        selectionError=selection.get("error"),
        reelError=reel.get("error"),
    )


async def stage_c_check(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    x = await fresh_clip(ctx, ids["dWt"])
    if x is None:
        return
    d_before = await tx_list(ctx, ids["dWt"])
    real = await move(session, ctx, [x["wt"]], ids["eWt"])
    group = (real.get("journal") or {}).get("group")
    to = ((_result_of(real).get("moved") or [{}])[0]).get("to") or {}
    synthetic = synthetic_open_intent(ctx, group)
    landed = await check_group(session, ctx, synthetic)
    moves = (landed.get("created") or {}).get("moves") or []
    rec(
        ctx,
        "c13_check_of_an_open_move_intent_is_landed_and_pairs_the_thing_with_its_new_entry",
        landed.get("verdict") == "landed"
        and [m["to"]["uid"] for m in moves] == [to.get("uid")]
        and [m["from"]["uid"] for m in moves] == [x["uid"]],
        verdict=landed.get("verdict"),
        moves=moves,
    )
    out = await do(session, ctx, "journal.revert", {"group": synthetic})
    written_groups(ctx).append({"op": "journal.revert", "group": synthetic})
    rec(
        ctx,
        "c14_revert_after_that_check_moves_the_thing_back",
        out.get("ok") is True
        and len(added(d_before, await tx_list(ctx, ids["dWt"]))) == 1
        and to.get("uid") not in uids_of(await tx_list(ctx, ids["eWt"])),
        error=out.get("error"),
    )
    y = await fresh_clip(ctx, ids["dWt"])
    if y:
        parent = ((intent_of(ctx, group).get("sources") or [{}])[0]).get("parent")
        source = {
            "name": y["name"],
            "type": "PyClip",
            "uid": y["uid"],
            "selector": sel(y["wt"]),
            "parent": parent,
        }
        existing = uids_of(await tx_list(ctx, ids["eWt"]))
        nothing = synthetic_open_intent(
            ctx, group, existing=existing, sources=[source], target=sel(ids["eWt"])
        )
        verdict = await check_group(session, ctx, nothing)
        rec(
            ctx,
            "c15_check_of_an_open_move_intent_whose_source_never_left_is_not_landed",
            verdict.get("verdict") == "not_landed",
            verdict=verdict.get("verdict"),
        )
