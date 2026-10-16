"""Stages x (segment.copy_to_media_panel / segment.match) and p (plan.run with the new edits) of the
segment-edits real-Flame e2e; see p3_3_segment_e2e.py.

The segment taken out is p2_len72 on track 1, head / tail 10, a Timewarp on it (used range 52 frames
in a 72-frame media). JUDGED: the measured shapes of `copy_to_media_panel`, `match` and
`match(preserve_handle=True)` (duration, start_frame, in / out mark, handles, whether the effect
came along), read back from the new clip by transport and compared with the op's own `clip`; that a
revert deletes the clip and leaves the sequence alone; that a clip that was renamed or edited is a
CONFLICT; that a reel of a Batch group is refused. OBSERVED: `match(include_timeline_fx=True)` (its
odd shape is not understood) and taking a segment out of a library."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, judged, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_1a_read_e2e import fill, is_error, tx_val  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    TRANSPORT_TRIM_CODE,
    copy_into,
    media_row,
    name_of,
    names_of,
    tx_children,
    tx_find_uid,
    uids_of,
)
from p3_3_e2e_common import (  # noqa: E402
    BATCH_CODE,
    HANDLE,
    RENAME_CODE,
    err_text,
    expect_error,
    fingerprint,
    fx_index_order,
    groups_now,
    make_seq,
    restored_wt,
    revert,
    run_call,
    seg_at,
    sel,
    stash_uids,
    state_snapshot,
    track_rows,
    tx_clip,
    tx_fx,
    tx_seg,
    tx_stash,
    tx_struct,
)

TIMEWARP = "Timewarp"
USED = 52
"""p2_len72 trimmed 10 / 10: 72 - 20 frames are used. (A segment's `record_out` / `source_out` is
the LAST frame of the range -- clip1 reads 0-23 -- in a real Flame and in the fake alike.)"""


async def extract_case(ctx: Ctx, tag: str) -> dict[str, Any] | None:
    made = await make_seq(
        ctx,
        tag,
        ((0, "clip1", 1), (1, "p2_len72", 1), (0, "clip2", 25)),
        trims=((1, HANDLE, HANDLE),),
        fxs=((1, "p2_len72", TIMEWARP, {}),),
    )
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    row = media_row(struct, 1, "p2_len72")
    if row is None:
        rec(ctx, f"{tag}_the_segment_to_take_out_is_there", False, track1=track_rows(struct, 1))
        return None
    return {"wt": made["wt"], "rin": row["rin"], "uid": made["uid"]}


def args_for(case: dict[str, Any], dest_wt: str, **extra: Any) -> dict[str, Any]:
    return {
        "target": seg_at(sel(case["wt"]), 1, case["rin"]),
        "destination": sel(dest_wt),
        **extra,
    }


def clip_matches_op(facts: dict[str, Any] | None, op_clip: dict[str, Any] | None) -> bool:
    """transport's reading of the new clip against what the op reported of it."""
    if not isinstance(facts, dict) or not isinstance(op_clip, dict):
        return False
    first = (op_clip.get("segments") or [{}])[0]
    seg = (facts.get("segments") or [{}])[0]
    return (
        (op_clip.get("duration") or {}).get("frame") == facts.get("duration")
        and op_clip.get("startFrame") == facts.get("startFrame")
        and (op_clip.get("inMark") or {}).get("frame") == facts.get("inMark")
        and (op_clip.get("outMark") or {}).get("frame") == facts.get("outMark")
        and sorted(op_clip.get("effects") or []) == sorted(seg.get("fx") or [])
        and first.get("head") == seg.get("head")
        and first.get("tail") == seg.get("tail")
    )


def shape_parts(
    facts: dict[str, Any] | None,
    *,
    duration: int,
    start_frame: int,
    head: Any,
    tail: Any,
    effects: list[str],
    marks: tuple[int, int] | None,
) -> dict[str, bool]:
    """Each statement of the shape of a clip taken out, one by one (all False when there is no
    clip or it holds no segment)."""
    if not isinstance(facts, dict) or not facts.get("segments"):
        return {"transport_reads_a_clip_with_a_segment": False}
    seg = facts["segments"][0]
    return {
        "transport_reads_a_clip_with_a_segment": True,
        "duration": facts.get("duration") == duration,
        "start_frame": facts.get("startFrame") == start_frame,
        "head": seg.get("head") == head,
        "tail": seg.get("tail") == tail,
        "effects": sorted(seg.get("fx") or []) == sorted(effects),
        "marks": (facts.get("inMark"), facts.get("outMark")) == marks if marks else True,
    }


def shape_ok(facts: dict[str, Any] | None, **shape: Any) -> bool:
    return all(shape_parts(facts, **shape).values())


async def take_and_check(
    session: Any, ctx: Ctx, tag: str, call_id: str, case: dict[str, Any], dest: str, **flags: Any
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The call, then the new clip read back by transport; records the three checks every shape
    shares (the clip is new and in the destination, the op says what transport read, the sequence is
    untouched)."""
    before_children = uids_of(await tx_children(ctx, dest))
    fp0 = await fingerprint(session, ctx, case["wt"])
    env = await run_call(session, ctx, call_id, args_for(case, dest, **flags))
    res = _result_of(env)
    created = res.get("created") or {}
    selector = created.get("selector") or {}
    facts = await tx_clip(ctx, selector["id"]) if selector.get("id") else None
    children = uids_of(await tx_children(ctx, dest))
    rec(
        ctx,
        f"{tag}_a_new_clip_is_in_the_destination_and_is_the_one_the_result_names",
        env.get("ok") is True
        and facts is not None
        and facts.get("type") == "PyClip"
        and created.get("uid") in children
        and created.get("uid") not in before_children
        and len(children) == len(before_children) + 1,
        error=env.get("error"),
        created=created,
    )
    rec(
        ctx,
        f"{tag}_the_result_says_what_transport_read_of_the_new_clip",
        clip_matches_op(facts, res.get("clip")),
        result=res.get("clip"),
        transport=facts,
    )
    rec(
        ctx,
        f"{tag}_the_sequence_was_not_touched",
        fp0 is not None and await fingerprint(session, ctx, case["wt"]) == fp0,
    )
    return env, facts


async def revert_and_check(
    session: Any, ctx: Ctx, tag: str, env: dict[str, Any], dest: str, before_children: list[str]
) -> None:
    uid = (_result_of(env).get("created") or {}).get("uid")
    out = await revert(session, ctx, env)
    rec(
        ctx,
        f"{tag}_a_revert_deletes_the_clip_and_the_destination_is_as_before",
        out.get("ok") is True
        and (_result_of(out).get("deleted") is True)
        and await tx_find_uid(ctx, uid) is None
        and uids_of(await tx_children(ctx, dest)) == before_children,
        error=out.get("error"),
    )


async def stage_x(ctx: Ctx) -> None:
    case = await extract_case(ctx, "XT")
    if case is None:
        return
    ctx.ids.setdefault("case", {})["extract"] = case
    dest = ctx.ids["ccpWt"]
    struct = await tx_struct(ctx, case["wt"])
    seg0 = await tx_seg(ctx, case["wt"], 1, case["rin"])
    rec(
        ctx,
        "x0_the_segment_is_the_52_frame_Timewarp_segment_with_handles_10_and_10",
        seg0 is not None
        and seg0["head"] == HANDLE
        and seg0["tail"] == HANDLE
        and (await tx_fx(ctx, case["wt"], 1, case["rin"]) or {}).get("effects", [{}])[0].get("type")
        == TIMEWARP,
        segment=seg0,
        track1=track_rows(struct, 1),
    )
    async with server_session(ctx, "p33-x") as session:
        before = uids_of(await tx_children(ctx, dest))
        env, facts = await take_and_check(
            session, ctx, "x1_copy_to_media_panel", "x_copy_out", case, dest
        )
        seg = (facts or {}).get("segments", [{}])[0]
        ok, parts = judged(
            **shape_parts(
                facts,
                duration=USED,
                start_frame=HANDLE + 1,
                head=HANDLE,
                tail=HANDLE,
                effects=[TIMEWARP],
                marks=None,
            ),
            the_source_segment_was_read=seg0 is not None,
            record_range_is_the_segments=seg0 is not None
            and (seg.get("rin"), seg.get("rout")) == (seg0["rin"], seg0["rout"]),
            source_range_is_the_segments=seg0 is not None
            and (seg.get("sin"), seg.get("sout")) == (seg0["sin"], seg0["sout"]),
        )
        rec(
            ctx,
            "x2_copy_to_media_panel_is_the_used_range_with_soft_handles_and_the_effect",
            ok,
            clauses=parts,
            transport=facts,
            segment=seg0,
        )
        await revert_and_check(session, ctx, "x3_copy", env, dest, before)

        env, facts = await take_and_check(session, ctx, "x4_match", "x_match", case, dest)
        seg = (facts or {}).get("segments", [{}])[0]
        rec(
            ctx,
            "x5_match_is_the_whole_source_with_marks_on_the_used_range_and_no_effect",
            shape_ok(
                facts,
                duration=USED,
                start_frame=1,
                head=0,
                tail=0,
                effects=[],
                marks=(HANDLE, HANDLE + USED),
            )
            and seg.get("sin") == 0
            and seg.get("sout") in (71, 72),
            transport=facts,
        )
        await revert_and_check(session, ctx, "x6_match", env, dest, before)

        env, facts = await take_and_check(
            session,
            ctx,
            "x7_match_preserve_handle",
            "x_match_preserve_handle",
            case,
            dest,
            preserve_handle=True,
        )
        seg = (facts or {}).get("segments", [{}])[0]
        rec(
            ctx,
            "x8_match_with_preserve_handle_is_the_used_range_with_its_handles_and_no_effect",
            shape_ok(
                facts,
                duration=USED,
                start_frame=HANDLE + 1,
                head=HANDLE,
                tail=HANDLE,
                effects=[],
                marks=None,
            )
            and seg.get("sin") == seg0["sin"],
            transport=facts,
        )
        await revert_and_check(session, ctx, "x9_match_preserve", env, dest, before)

        env = await run_call(
            session, ctx, "x_match_with_effects", args_for(case, dest, include_timeline_fx=True)
        )
        created = _result_of(env).get("created") or {}
        facts = await tx_clip(ctx, created["selector"]["id"]) if created.get("selector") else None
        observe(
            ctx,
            "x10_match_with_include_timeline_fx_(plan_59_saw_duration_72_tail_infinite_effect_kept)",
            ok=env.get("ok"),
            error=env.get("error"),
            clip=_result_of(env).get("clip"),
            transport=facts,
        )
        if env.get("ok") is True:
            await revert_and_check(session, ctx, "x11_match_with_effects", env, dest, before)
        await conflicts(session, ctx, case, dest)
        await refusals(session, ctx, case, dest)
        await library_cases(session, ctx, case)


async def conflicts(session: Any, ctx: Ctx, case: dict[str, Any], dest: str) -> None:
    env = await run_call(session, ctx, "x_copy_out", args_for(case, dest))
    uid = (_result_of(env).get("created") or {}).get("uid")
    selector = (_result_of(env).get("created") or {}).get("selector") or {}
    renamed = await tx_val(
        ctx, fill(RENAME_CODE, WT=selector.get("id"), NAME=name_of(ctx, "renamed")), "p33_rename"
    )
    out = await revert(session, ctx, env)
    rec(
        ctx,
        "x12_a_clip_that_was_renamed_is_a_conflict_and_is_not_deleted",
        env.get("ok") is True
        and not is_error(renamed)
        and expect_error(out, "CONFLICT")
        and await tx_find_uid(ctx, uid) is not None,
        error=out.get("error"),
        conflicts=_result_of(out).get("conflicts"),
    )
    env = await run_call(session, ctx, "x_copy_out", args_for(case, dest))
    uid = (_result_of(env).get("created") or {}).get("uid")
    selector = (_result_of(env).get("created") or {}).get("selector") or {}
    inner = ((_result_of(env).get("clip") or {}).get("segments") or [{}])[0]
    trimmed = await tx_val(
        ctx,
        fill(
            TRANSPORT_TRIM_CODE,
            WT=selector.get("id"),
            TRACK=0,
            RECORD_IN=(inner.get("recordIn") or {}).get("frame"),
            N=2,
        ),
        "p33_trim_inside",
    )
    out = await revert(session, ctx, env)
    rec(
        ctx,
        "x13_a_clip_whose_content_was_edited_is_a_conflict_and_is_not_deleted",
        env.get("ok") is True
        and not is_error(trimmed)
        and expect_error(out, "CONFLICT")
        and await tx_find_uid(ctx, uid) is not None,
        error=out.get("error"),
        conflicts=_result_of(out).get("conflicts"),
    )


async def refusals(session: Any, ctx: Ctx, case: dict[str, Any], dest: str) -> None:
    batch = await tx_val(
        ctx,
        fill(BATCH_CODE, NAME=name_of(ctx, "BG"), SRC=ctx.ids["clipWt"]["clip1"]),
        "p33_batch",
        timeout_s=60.0,
    )
    reel_wt = batch.get("reelWt") if isinstance(batch, dict) else None
    usable = isinstance(reel_wt, str) and not reel_wt.startswith("ERR:")
    rec(ctx, "x14_a_batch_group_with_a_reel_is_there", usable, batch=batch)
    snap = await state_snapshot(ctx)
    if usable:
        before = names_of(await tx_children(ctx, reel_wt))
        env = await run_call(session, ctx, "x_into_a_batch_reel", args_for(case, reel_wt))
        rec(
            ctx,
            "x15_a_reel_of_a_batch_group_is_refused_and_nothing_is_written_or_journaled",
            expect_error(env, "INVALID_ARGS")
            and "Batch" in err_text(env)
            and names_of(await tx_children(ctx, reel_wt)) == before
            and await state_snapshot(ctx) == snap,
            error=env.get("error"),
        )
    gap_rin = track_rows(await tx_struct(ctx, case["wt"]), 1)[0]["rin"]
    gap = await run_call(
        session,
        ctx,
        "x_copy_out",
        {"target": seg_at(sel(case["wt"]), 1, gap_rin), "destination": sel(dest)},
    )
    rec(
        ctx,
        "x16_a_gap_is_refused",
        expect_error(gap, "PRECONDITION_UNMET") and "Gap" in err_text(gap),
        error=gap.get("error"),
    )
    not_container = await run_call(session, ctx, "x_match", args_for(case, case["wt"]))
    rec(
        ctx,
        "x17_a_destination_that_is_a_sequence_is_refused",
        expect_error(not_container, "SELECTOR_TYPE") and await state_snapshot(ctx) == snap,
        error=not_container.get("error"),
    )


async def library_cases(session: Any, ctx: Ctx, case: dict[str, Any]) -> None:
    lib_reel = ctx.ids["libReelWt"]
    before = uids_of(await tx_children(ctx, lib_reel))
    env = await run_call(session, ctx, "x_copy_out", args_for(case, lib_reel))
    created = _result_of(env).get("created") or {}
    rec(
        ctx,
        "x18_a_library_reel_is_a_destination_and_a_revert_deletes_the_clip",
        env.get("ok") is True and created.get("uid") in uids_of(await tx_children(ctx, lib_reel)),
        error=env.get("error"),
    )
    if env.get("ok") is True:
        await revert_and_check(session, ctx, "x19_library_copy", env, lib_reel, before)
    library = await copy_into(ctx, case["wt"], lib_reel)
    if library is not None:
        env = await run_call(
            session,
            ctx,
            "x_match",
            {
                "target": seg_at(sel(library["wt"]), 1, case["rin"]),
                "destination": sel(ctx.ids["ccpWt"]),
            },
        )
        observe(
            ctx,
            "x20_taking_a_segment_out_of_a_sequence_inside_a_library_(not_measured)",
            ok=env.get("ok"),
            errorCode=env.get("errorCode"),
            error=env.get("error"),
            clip=_result_of(env).get("clip"),
        )
        if env.get("ok") is True:
            await revert(session, ctx, env)


# -- p: plan.run with the new edits


async def stage_p(ctx: Ctx) -> None:
    made = await make_seq(
        ctx,
        "PL",
        ((0, "clip1", 1), (0, "clip2", 25), (1, "p2_len72", 1)),
        trims=((1, HANDLE, HANDLE),),
    )
    if made is None:
        return
    wiretap = made["wt"]
    struct = await tx_struct(ctx, wiretap)
    long = media_row(struct, 1, "p2_len72")
    one = media_row(struct, 0, "clip1")
    two = media_row(struct, 0, "clip2")
    if not (long and one and two):
        rec(ctx, "p0_the_segments_are_there", False)
        return
    long_before = await tx_seg(ctx, wiretap, 1, long["rin"])
    one_before = await tx_seg(ctx, wiretap, 0, one["rin"])
    steps = [
        {
            "op": "segment.slip",
            "args": {"target": seg_at(sel(wiretap), 1, long["rin"]), "offset": 3},
        },
        {
            "op": "segment.replace",
            "args": {
                "target": seg_at(sel(wiretap), 0, two["rin"]),
                "source": sel(ctx.ids["clipWt"]["p2_len48"]),
            },
        },
        {
            "op": "segment.trim",
            "args": {
                "target": seg_at(sel(wiretap), 0, one["rin"]),
                "edge": "tail",
                "offset": 3,
            },
        },
        {
            "op": "segment.create_effect",
            "args": {"target": seg_at(sel(wiretap), 1, long["rin"]), "type": "Blur"},
        },
    ]
    async with server_session(ctx, "p33-p") as session:
        fp0 = await fingerprint(session, ctx, wiretap)
        stash0 = stash_uids(await tx_stash(ctx))
        env = await run_call(session, ctx, "p_plan", {"steps": steps, "label": "p33 mix"})
        res = _result_of(env)
        long_after = await tx_seg(ctx, wiretap, 1, long["rin"])
        one_after = await tx_seg(ctx, wiretap, 0, one["rin"])
        two_after = await tx_seg(ctx, wiretap, 0, two["rin"])
        stash1 = stash_uids(await tx_stash(ctx))
        long_fx = fx_index_order(await tx_fx(ctx, wiretap, 1, long["rin"]))
        ok, parts = judged(
            plan_ok=env.get("ok") is True,
            all_four_steps_ok=[s.get("ok") for s in res.get("steps") or []] == [True] * 4,
            slip_took_source_in_down_by_3=long_before is not None
            and long_after is not None
            and long_after["sin"] == long_before["sin"] - 3,
            trim_took_record_out_down_by_3=one_before is not None
            and one_after is not None
            and one_after["rout"] == one_before["rout"] - 3,
            replace_put_p2_len48_in=two_after is not None and two_after["src"] == "p2_len48",
            create_effect_left_only_a_blur=sorted(long_fx) == ["Blur"],
        )
        rec(
            ctx,
            "p1_four_steps_of_the_new_ops_ran_as_one_plan_and_transport_sees_every_edit",
            ok,
            clauses=parts,
            effectsOfTheLongSegment=long_fx,
            error=env.get("error"),
            steps=[(s.get("op"), s.get("ok"), s.get("errorCode")) for s in res.get("steps") or []],
        )
        rec(
            ctx,
            "p2_the_plan_took_one_checkpoint_one_stashed_copy_and_one_group",
            len(stash1) == len(stash0) + 1
            and bool((res.get("timeline") or {}).get("checkpoint"))
            and (res.get("timeline") or {}).get("changed") is True,
            stash0=len(stash0),
            stash1=len(stash1),
            timeline=res.get("timeline"),
        )
        out = await revert(session, ctx, env)
        new_wt = restored_wt(out)
        rec(
            ctx,
            "p3_one_revert_puts_the_whole_plan_back_with_the_old_fingerprint",
            out.get("ok") is True
            and new_wt is not None
            and await fingerprint(session, ctx, new_wt) == fp0
            and await tx_seg(ctx, new_wt, 1, long["rin"]) == long_before
            and await tx_seg(ctx, new_wt, 0, one["rin"]) == one_before,
            error=out.get("error"),
        )
        snap = await state_snapshot(ctx)
        match_step = {
            "op": "segment.match",
            "args": {
                "target": seg_at(sel(new_wt or wiretap), 1, long["rin"]),
                "destination": sel(ctx.ids["ccpWt"]),
            },
        }
        with_match = await run_call(
            session, ctx, "p_plan_with_a_match", {"steps": [steps[0], match_step]}
        )
        rec(
            ctx,
            "p4_a_plan_that_holds_a_match_is_refused_and_nothing_runs",
            expect_error(with_match, "INVALID_ARGS")
            and "plan step" in str(_result_of(with_match))
            and await state_snapshot(ctx) == snap
            and groups_now(ctx) == snap["groups"],
            error=with_match.get("error"),
        )
