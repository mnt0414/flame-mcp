"""Stage p of the plan / delete real-Flame e2e: plan.run's bundle of Timeline writes (one
checkpoint, the segment selectors resolved up front, `late`, what may not share the plan, one
revert); see p3_2b_plan_delete_e2e.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import p3_2b_e2e_common as common  # noqa: E402
from p2_2a_journal_e2e import Ctx, rec, server_session  # noqa: E402
from p3_2a_e2e_common import make_s1  # noqa: E402
from p3_2b_e2e_common import (  # noqa: E402
    HEAD_TRIM,
    TAIL_TRIM,
    expect_error,
    fingerprint,
    mine,
    outcome_of,
    revert,
    run_call,
    state_snapshot,
    tx_comment,
    tx_find_uid,
    tx_name,
    tx_struct,
)

SOURCES = ("clip1", "clip2", "p2_len48")


async def fresh(ctx: Ctx, tag: str) -> dict[str, Any] | None:
    """A fresh S1-like sequence and the track 0 rows the plans address (read by transport)."""
    made = await make_s1(ctx, tag)
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    rows = {
        src: next((r for r in struct[0][0] if r["type"] != "Gap" and r["src"] == src), None)
        for src in SOURCES
    }
    if None in rows.values():
        rec(ctx, f"{tag}_the_segments_to_trim_are_there", False, track0=struct[0][0])
        return None
    return {**made, "struct": struct, "rows": rows}


def row_of(struct: Any, src: str) -> dict[str, Any] | None:
    return next((r for r in struct[0][0] if r["type"] != "Gap" and r["src"] == src), None)


async def stage_p(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p32b-p") as session:
        await bundle(session, ctx)
        await pre_resolution(session, ctx)
        await late(session, ctx)
        await mixing(session, ctx)
    ids.pop("case", None)


async def bundle(session: Any, ctx: Ctx) -> None:
    made = await fresh(ctx, "PB")
    if made is None:
        return
    rows = made["rows"]
    rins = [rows[s]["rin"] for s in SOURCES]
    case = {"wt": made["wt"], "rins": rins, "srcs": list(SOURCES)}
    before = await state_snapshot(ctx)
    fp0 = await fingerprint(session, ctx, made["wt"])
    env = await run_call(session, ctx, "p1_three_trims", case=case)
    after = await state_snapshot(ctx)
    struct1 = await tx_struct(ctx, made["wt"])
    res = mine(env)
    steps = res.get("steps") or []
    expected = [{"rin": rows[s]["rin"], "rout": rows[s]["rout"] - TAIL_TRIM} for s in SOURCES]
    got = [
        {
            "rin": (row_of(struct1, s) or {}).get("rin"),
            "rout": (row_of(struct1, s) or {}).get("rout"),
        }
        for s in SOURCES
    ]
    step_outs = [
        ((s.get("result") or {}).get("after") or {}).get("recordOut", {}).get("frame")
        for s in steps
    ]
    group = (env.get("journal") or {}).get("group")
    rec(
        ctx,
        "p1_three_trims_in_one_plan_take_one_checkpoint_and_each_lands_where_transport_reads_it",
        env.get("ok") is True
        and got == expected
        and step_outs == [e["rout"] for e in expected]
        and len(after["stash"]) == len(before["stash"]) + 1
        and after["checkpoints"] == before["checkpoints"] + 1
        and (res.get("timeline") or {}).get("fingerprintBefore") == fp0
        and (res.get("timeline") or {}).get("checkpoint")
        and outcome_of(ctx, group).get("ok") is True,
        error=env.get("error"),
        expected=expected,
        got=got,
        stepOuts=step_outs,
        stashBefore=len(before["stash"]),
        stashAfter=len(after["stash"]),
    )
    back = await revert(session, ctx, env)
    rres = mine(back)
    restored = rres.get("restored") or {}
    struct2 = await tx_struct(ctx, restored.get("wiretapId", "/wt/none"))
    old = await tx_find_uid(ctx, made["uid"])
    rec(
        ctx,
        "p1_one_revert_puts_the_whole_plan_back_a_new_uid_the_fingerprint_before",
        back.get("ok") is True
        and await fingerprint(session, ctx, restored.get("wiretapId", "/wt/none")) == fp0
        and struct2 == made["struct"]
        and restored.get("uid") not in (None, made["uid"])
        and old is None,
        error=back.get("error"),
        restored=restored,
    )
    ids = ctx.ids
    ids["planBundle"] = {"group": group, "wt": restored.get("wiretapId"), "fp": fp0}


async def pre_resolution(session: Any, ctx: Ctx) -> None:
    made = await fresh(ctx, "PR")
    if made is None:
        return
    rows = made["rows"]
    case = {
        "wt": made["wt"],
        "rins": [rows["clip1"]["rin"]],
        "rin": rows["clip2"]["rin"],
        "bogus": 9_999_999,
    }
    for call_id, code, name in (
        ("p2_one_record_in_does_not_exist", "SELECTOR_EMPTY", "p2"),
        ("p3_wrong_expected_source_name", "CONFLICT", "p3"),
    ):
        before = await state_snapshot(ctx)
        env = await run_call(session, ctx, call_id, case=case)
        after = await state_snapshot(ctx)
        rec(
            ctx,
            f"{name}_a_step_that_does_not_resolve_stops_the_whole_plan_and_leaves_no_trace",
            expect_error(env, code)
            and after == before
            and await tx_struct(ctx, made["wt"]) == made["struct"]
            and (mine(env).get("preResolution") or [{}])[0].get("errorCode") == code,
            error=env.get("error"),
            before=before,
            after=after,
        )


async def late(session: Any, ctx: Ctx) -> None:
    made = await fresh(ctx, "LT")
    if made is None:
        return
    rin = made["rows"]["clip2"]["rin"]
    rout = made["rows"]["clip2"]["rout"]
    case = {"wt": made["wt"], "rin": rin, "late": False}
    before = await state_snapshot(ctx)
    refused = await run_call(session, ctx, "p4_head_then_tail", case=case)
    rec(
        ctx,
        "p4_without_late_a_step_that_points_at_the_moved_record_in_is_refused_up_front",
        expect_error(refused, "SELECTOR_EMPTY")
        and await state_snapshot(ctx) == before
        and await tx_struct(ctx, made["wt"]) == made["struct"],
        error=refused.get("error"),
    )
    env = await run_call(session, ctx, "p4_head_then_tail", case={**case, "late": True})
    struct = await tx_struct(ctx, made["wt"])
    row = row_of(struct, "clip2") or {}
    rec(
        ctx,
        "p4_with_late_true_it_is_resolved_when_its_turn_comes_and_lands",
        env.get("ok") is True and row.get("rin") == rin + HEAD_TRIM and row.get("rout") == rout - 2,
        error=env.get("error"),
        row=row,
        expectedIn=rin + HEAD_TRIM,
        expectedOut=rout - 2,
    )
    other = await fresh(ctx, "LF")
    if other is None:
        return
    orow = other["rows"]
    case2 = {
        "wt": other["wt"],
        "rin": orow["clip2"]["rin"],
        "rins": [orow["clip1"]["rin"]],
        "bogus": 9_999_999,
    }
    fp0 = await fingerprint(session, ctx, other["wt"])
    stopped = await run_call(session, ctx, "p5_late_step_that_does_not_resolve", case=case2)
    struct2 = await tx_struct(ctx, other["wt"])
    moved = row_of(struct2, "clip2") or {}
    rec(
        ctx,
        "p5_a_late_step_that_does_not_resolve_stops_the_plan_partial_and_the_first_step_stays",
        expect_error(stopped, "SELECTOR_EMPTY")
        and (stopped.get("partial") or {}).get("done") == 1
        and (stopped.get("partial") or {}).get("failed") == 1
        and (mine(stopped).get("stoppedAt")) == 1
        and moved.get("rin") == orow["clip2"]["rin"] + HEAD_TRIM,
        error=stopped.get("error"),
        partial=stopped.get("partial"),
    )
    back = await revert(session, ctx, stopped)
    restored = mine(back).get("restored") or {}
    rec(
        ctx,
        "p5_and_journal_revert_puts_it_back",
        back.get("ok") is True
        and await fingerprint(session, ctx, restored.get("wiretapId", "/wt/none")) == fp0
        and await tx_struct(ctx, restored.get("wiretapId", "/wt/none")) == other["struct"],
        error=back.get("error"),
    )


async def mixing(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    made = await fresh(ctx, "MX")
    if made is None:
        return
    rin = made["rows"]["clip1"]["rin"]
    other_name = await tx_name(ctx, ids["s2Wt"])
    case = {"wt": made["wt"], "rin": rin, "otherWt": ids["s2Wt"], "otherRin": 0}
    before = await state_snapshot(ctx)
    other_trim = await run_call(session, ctx, "p6_trim_of_another_sequence", case=case)
    rec(
        ctx,
        "p6_a_trim_of_another_sequence_in_the_plan_is_refused_before_anything_runs",
        expect_error(other_trim, "INVALID_ARGS")
        and "different sequences" in str(other_trim.get("error"))
        and await state_snapshot(ctx) == before
        and await tx_struct(ctx, made["wt"]) == made["struct"],
        error=other_trim.get("error"),
    )
    renamed = await run_call(session, ctx, "p7_rename_in_the_plan", case=case)
    problems = " ".join(
        p for i in mine(renamed).get("issues") or [] for p in i.get("problems") or []
    )
    rec(
        ctx,
        "p7_a_desktop_rename_in_the_plan_is_refused_for_its_own_way_back",
        expect_error(renamed, "INVALID_ARGS")
        and "the ways of reverting differ" in problems
        and await state_snapshot(ctx) == before
        and await tx_name(ctx, ids["s2Wt"]) == other_name,
        error=renamed.get("error"),
        problems=problems,
    )
    comment0 = await tx_comment(ctx, made["wt"], 0, rin)
    env = await run_call(session, ctx, "p8_comment_and_trim", case=case)
    comment1 = await tx_comment(ctx, made["wt"], 0, rin)
    struct1 = await tx_struct(ctx, made["wt"])
    rec(
        ctx,
        "p8_a_set_attributes_and_a_trim_of_the_same_sequence_share_one_plan",
        env.get("ok") is True and comment1 == common.COMMENT and struct1 != made["struct"],
        error=env.get("error"),
        before=comment0,
        after=comment1,
    )
    back = await revert(session, ctx, env)
    restored = mine(back).get("restored") or {}
    wiretap = restored.get("wiretapId", "/wt/none")
    rec(
        ctx,
        "p8_one_revert_brings_the_comment_back_too",
        back.get("ok") is True
        and await tx_comment(ctx, wiretap, 0, rin) == comment0
        and await tx_struct(ctx, wiretap) == made["struct"],
        error=back.get("error"),
        comment=await tx_comment(ctx, wiretap, 0, rin),
    )
