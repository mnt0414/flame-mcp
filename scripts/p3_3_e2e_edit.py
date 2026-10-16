"""Stages s (segment.slip) and r (segment.replace) of the segment-edits real-Flame e2e; see
p3_3_segment_e2e.py.

Every case is a fresh sequence, so one edit never starts from another's result. The segment slipped
is p2_len72 on track 1 (head 10 / tail 10 left by two trims, a Gap before it): it can be slipped as
far as its handles go. What a call did is read back by transport (the segment's record_in / out,
source_in / out, head and tail) and compared with the op's own `before` / `after` / `deltas`;
`SLIP_DELTAS` states the measured behaviour again (slip(n) takes source_in DOWN by n, record
unchanged) so the op's idea of it is checked against a second statement. The replace stage states
the replace rule (the OFFSET from the start of the clip is kept, never the timecode) and runs the
scenario-1 shrink: replace with a clip whose timecode starts later, then slip the difference
back."""

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
from p3_2a_e2e_common import copy_into, media_row  # noqa: E402
from p3_3_e2e_common import (  # noqa: E402
    HANDLE,
    LETTERBOX,
    err_text,
    expect_error,
    fingerprint,
    fx_index_order,
    groups_now,
    make_seq,
    op_time,
    outcome_of,
    restored_wt,
    revert,
    run_call,
    seg_at,
    sel,
    state_snapshot,
    track_rows,
    tx_fx,
    tx_seg,
    tx_struct,
)

FACT_KEYS = ("rin", "rout", "sin", "sout", "head", "tail")
OP_FACT_KEYS = {
    "rin": ("recordIn", True),
    "rout": ("recordOut", True),
    "sin": ("sourceIn", True),
    "sout": ("sourceOut", True),
    "head": ("head", False),
    "tail": ("tail", False),
}


def slip_deltas(offset: int) -> dict[str, int]:
    """slip(n) takes source_in and source_out DOWN by n, head down and tail up by n."""
    return {"rin": 0, "rout": 0, "sin": -offset, "sout": -offset, "head": -offset, "tail": offset}


def deltas_of(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    return {k: after[k] - before[k] for k in FACT_KEYS}


def op_facts_equal(op_side: dict[str, Any] | None, facts: dict[str, Any] | None) -> bool:
    """The op's `before` / `after` (PyTime as {timecode, frame}) against transport's facts."""
    if not isinstance(op_side, dict) or not isinstance(facts, dict):
        return False
    for key, (name, is_time) in OP_FACT_KEYS.items():
        value = op_side.get(name)
        if is_time:
            value = op_time(value)
        if value != facts.get(key):
            return False
    return True


async def slip_target(ctx: Ctx, tag: str) -> dict[str, Any] | None:
    """A fresh sequence: clip1 on track 0 (no handles), p2_len72 on track 1 (head / tail 10)."""
    made = await make_seq(
        ctx,
        tag,
        ((0, "clip1", 1), (1, "p2_len72", 1)),
        trims=((1, HANDLE, HANDLE),),
    )
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    row = media_row(struct, 1, "p2_len72")
    if row is None:
        rec(ctx, f"{tag}_the_segment_to_slip_is_there", False, track1=track_rows(struct, 1))
        return None
    gap = track_rows(struct, 1)[0]
    clip1 = media_row(struct, 0, "clip1")
    return {
        "wt": made["wt"],
        "rin": row["rin"],
        "struct": struct,
        "gapRin": gap["rin"] if gap["type"] == "Gap" else None,
        "clip1Rin": clip1["rin"] if clip1 else None,
    }


async def slip_step(session: Any, ctx: Ctx, tag: str, case: dict[str, Any], offset: int) -> Any:
    wiretap, rin = case["wt"], case["rin"]
    before = await tx_seg(ctx, wiretap, 1, rin)
    env = await run_call(
        session, ctx, "s_slip", {"target": seg_at(sel(wiretap), 1, rin), "offset": offset}
    )
    after = await tx_seg(ctx, wiretap, 1, rin)
    res = _result_of(env)
    want = slip_deltas(offset)
    got = deltas_of(before, after) if before and after else None
    rec(
        ctx,
        f"{tag}_the_segment_moved_the_way_it_is_measured_to_(read_back_by_transport)",
        env.get("ok") is True and got == want,
        error=env.get("error"),
        want=want,
        got=got,
        before=before,
        after=after,
    )
    rec(
        ctx,
        f"{tag}_the_result_says_what_transport_read",
        env.get("ok") is True
        and op_facts_equal(res.get("before"), before)
        and op_facts_equal(res.get("after"), after)
        and res.get("moved") == offset
        and res.get("requested") == offset
        and res.get("deltas")
        == {
            "recordIn": 0,
            "recordOut": 0,
            "sourceIn": -offset,
            "sourceOut": -offset,
            "head": -offset,
            "tail": offset,
        },
        result={k: res.get(k) for k in ("before", "after", "deltas", "moved", "returned")},
    )
    journal = env.get("journal") or {}
    outcome = outcome_of(ctx, journal.get("group"))
    rec(
        ctx,
        f"{tag}_the_journal_holds_a_checkpoint_and_the_fingerprints",
        bool(journal.get("checkpoint"))
        and journal.get("changed") is True
        and outcome.get("ok") is True
        and outcome.get("fingerprintAfter") is not None,
        journal=journal,
    )
    observe(ctx, f"{tag}_returned_by_slip", returned=res.get("returned"), offset=offset)
    return env


async def stage_s(ctx: Ctx) -> None:
    case = await slip_target(ctx, "SL")
    if case is None:
        return
    wiretap, rin = case["wt"], case["rin"]
    ctx.ids.setdefault("case", {})["slip"] = case
    async with server_session(ctx, "p33-s") as session:
        await slip_step(session, ctx, "s1_slip_plus_3", case, 3)
        second = await slip_step(session, ctx, "s2_slip_minus_5", case, -5)
        position = _result_of(second).get("position") or {}
        info = await do(session, ctx, "segment.info", {"target": position.get("selector")})
        rec(
            ctx,
            "s3_the_position_selector_resolves_to_the_slipped_segment",
            info.get("ok") is True and op_time(_result_of(info).get("recordIn")) == rin,
            position=position,
        )
        before = await tx_seg(ctx, wiretap, 1, rin)
        assert before is not None
        snap = await state_snapshot(ctx)
        refused = [
            ("s4_past_the_head_handle", before["head"] + 1, "head handle"),
            ("s5_far_past_the_head_handle", 500, "head handle"),
            ("s6_past_the_tail_handle", -(before["tail"] + 1), "tail handle"),
        ]
        for tag, offset, needle in refused:
            env = await run_call(
                session,
                ctx,
                "s_slip_refused",
                {"target": seg_at(sel(wiretap), 1, rin), "offset": offset},
            )
            res = _result_of(env)
            handle_key = "head" if offset > 0 else "tail"
            rec(
                ctx,
                f"{tag}_is_refused_before_anything_is_copied_or_journaled",
                expect_error(env, "PRECONDITION_UNMET")
                and needle in err_text(env)
                and res.get("available") == before[handle_key]
                and await state_snapshot(ctx) == snap,
                error=env.get("error"),
                available=res.get("available"),
                handle=before[handle_key],
            )
        none = await run_call(
            session,
            ctx,
            "s_slip_refused",
            {"target": seg_at(sel(wiretap), 0, case["clip1Rin"]), "offset": 1},
        )
        rec(
            ctx,
            "s7_a_segment_without_handles_cannot_be_slipped",
            expect_error(none, "PRECONDITION_UNMET")
            and _result_of(none).get("available") == 0
            and await state_snapshot(ctx) == snap,
            error=none.get("error"),
        )
        gap = await run_call(
            session,
            ctx,
            "s_slip_refused",
            {"target": seg_at(sel(wiretap), 1, case["gapRin"]), "offset": 1},
        )
        rec(
            ctx,
            "s8_a_gap_is_refused",
            expect_error(gap, "PRECONDITION_UNMET") and "Gap" in err_text(gap),
            error=gap.get("error"),
        )
        zero = await run_call(
            session,
            ctx,
            "s_slip_bad_offset",
            {"target": seg_at(sel(wiretap), 1, rin), "offset": 0},
        )
        rec(
            ctx,
            "s9_offset_zero_is_invalid_args",
            expect_error(zero, "INVALID_ARGS") and await state_snapshot(ctx) == snap,
            error=zero.get("error"),
        )
        library = await copy_into(ctx, wiretap, ctx.ids["libReelWt"])
        if library is None:
            rec(ctx, "s10_a_copy_of_the_sequence_is_in_the_library", False)
        else:
            inside = await run_call(
                session,
                ctx,
                "s_slip_in_a_library",
                {"target": seg_at(sel(library["wt"]), 1, rin), "offset": 1},
            )
            rec(
                ctx,
                "s10_a_segment_inside_a_library_is_refused",
                expect_error(inside, "PRECONDITION_UNMET")
                and "inside a library" in err_text(inside)
                and await state_snapshot(ctx) == snap,
                error=inside.get("error"),
            )
        after_all = await tx_seg(ctx, wiretap, 1, rin)
        rec(ctx, "s11_none_of_the_refused_calls_changed_the_segment", after_all == before)
        await slip_revert(session, ctx)


async def slip_revert(session: Any, ctx: Ctx) -> None:
    case = await slip_target(ctx, "SLR")
    if case is None:
        return
    wiretap, rin = case["wt"], case["rin"]
    before = await tx_seg(ctx, wiretap, 1, rin)
    fp0 = await fingerprint(session, ctx, wiretap)
    env = await run_call(
        session, ctx, "s_slip", {"target": seg_at(sel(wiretap), 1, rin), "offset": 4}
    )
    fp1 = await fingerprint(session, ctx, wiretap)
    out = await revert(session, ctx, env)
    new_wt = restored_wt(out)
    after = await tx_seg(ctx, new_wt, 1, rin) if new_wt else None
    fp2 = await fingerprint(session, ctx, new_wt) if new_wt else None
    rec(
        ctx,
        "s12_a_revert_puts_the_sequence_back_the_fingerprint_and_the_segment_are_the_old_ones",
        env.get("ok") is True
        and out.get("ok") is True
        and fp0 is not None
        and fp1 != fp0
        and fp2 == fp0
        and after == before
        and new_wt != wiretap,
        error=out.get("error"),
        fingerprints=[fp0, fp1, fp2],
        before=before,
        after=after,
    )
    again = await run_call(
        session, ctx, "s_slip", {"target": seg_at(sel(wiretap), 1, rin), "offset": 1}
    )
    rec(
        ctx,
        "s13_the_old_address_does_not_resolve_after_a_restore",
        expect_error(again, "SELECTOR_EMPTY"),
        errorCode=again.get("errorCode"),
    )


# -- r: segment.replace


async def replace_target(
    ctx: Ctx, tag: str, clip: str = "p2_src_tc1h", fx: bool = False
) -> dict[str, Any] | None:
    """A fresh sequence: `clip` on track 1 trimmed to head / tail 10; with `fx`, clip2 on track 0
    carries a Burn-in Letterbox (ratio 1.5)."""
    puts = [(1, clip, 1)]
    fxs = ()
    if fx:
        puts.append((0, "clip2", 1))
        fxs = ((0, "clip2", LETTERBOX, {"mode": "Custom", "ratio": 1.5}),)
    made = await make_seq(ctx, tag, tuple(puts), trims=((1, HANDLE, HANDLE),), fxs=fxs)
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    row = media_row(struct, 1, clip)
    if row is None:
        rec(ctx, f"{tag}_the_segment_to_replace_is_there", False, track1=track_rows(struct, 1))
        return None
    fx_row = media_row(struct, 0, "clip2") if fx else None
    return {"wt": made["wt"], "rin": row["rin"], "fxRin": fx_row["rin"] if fx_row else None}


async def replace_call(
    session: Any, ctx: Ctx, call_id: str, case: dict[str, Any], source_wt: str, **extra: Any
) -> dict[str, Any]:
    track, rin = (0, case["fxRin"]) if extra.pop("on_fx", False) else (1, case["rin"])
    args = {"target": seg_at(sel(case["wt"]), track, rin), "source": sel(source_wt), **extra}
    return await run_call(session, ctx, call_id, args)


async def stage_r(ctx: Ctx) -> None:
    clips = ctx.ids["clipWt"]
    async with server_session(ctx, "p33-r") as session:
        # r1: smart_replace_media with a clip whose timecode is the same
        case = await replace_target(ctx, "R1")
        if case is None:
            return
        wiretap, rin = case["wt"], case["rin"]
        ctx.ids.setdefault("case", {})["replace"] = case
        fp0 = await fingerprint(session, ctx, wiretap)
        before = await tx_seg(ctx, wiretap, 1, rin)
        env = await replace_call(session, ctx, "r_replace", case, clips["p2_rep_tc1h"])
        after = await tx_seg(ctx, wiretap, 1, rin)
        res = _result_of(env)
        rec(
            ctx,
            "r1_the_source_name_changed_and_the_record_stayed_(read_back_by_transport)",
            env.get("ok") is True
            and before is not None
            and after is not None
            and before["src"] == "p2_src_tc1h"
            and after["src"] == "p2_rep_tc1h"
            and (after["rin"], after["rout"]) == (before["rin"], before["rout"])
            and (after["sin"], after["sout"], after["head"])
            == (before["sin"], before["sout"], before["head"]),
            error=env.get("error"),
            before=before,
            after=after,
        )
        rec(
            ctx,
            "r2_the_result_has_the_time_values_with_frame_and_timecode_and_the_changes",
            env.get("ok") is True
            and op_facts_equal(res.get("before"), before)
            and op_facts_equal(res.get("after"), after)
            and isinstance((res.get("after") or {}).get("sourceIn", {}).get("timecode"), str)
            and "sourceName" in (res.get("changed") or [])
            and (res.get("position") or {}).get("selector", {}).get("expect_source_name")
            == "p2_rep_tc1h",
            result={k: res.get(k) for k in ("before", "after", "changed", "position", "returned")},
        )
        out = await revert(session, ctx, env)
        new_wt = restored_wt(out)
        rec(
            ctx,
            "r3_a_revert_puts_the_old_source_back_with_the_old_fingerprint",
            out.get("ok") is True
            and new_wt is not None
            and await fingerprint(session, ctx, new_wt) == fp0
            and await tx_seg(ctx, new_wt, 1, rin) == before,
            error=out.get("error"),
        )

        # r4: the scenario-1 shrink -- the offset rule lands a later clip late; slip fixes it
        case = await replace_target(ctx, "R4")
        if case is not None:
            wiretap, rin = case["wt"], case["rin"]
            before = await tx_seg(ctx, wiretap, 1, rin)
            env = await replace_call(session, ctx, "r_replace", case, clips["p2_rep_tc1h_plus10"])
            res = _result_of(env)
            after = await tx_seg(ctx, wiretap, 1, rin)
            diff = (op_time((res.get("after") or {}).get("sourceIn")) or 0) - (before or {}).get(
                "sin", 0
            )
            rec(
                ctx,
                "r4_the_offset_from_the_clip_start_is_kept_not_the_timecode_(10_frames_later)",
                env.get("ok") is True
                and before is not None
                and after is not None
                and after["sin"] - before["sin"] == 10
                and after["head"] == before["head"]
                and diff == 10,
                before=before,
                after=after,
                diff=diff,
            )
            slipped = await run_call(
                session,
                ctx,
                "r_replace_slip",
                {"target": (res.get("position") or {}).get("selector"), "offset": diff},
            )
            final = await tx_seg(ctx, wiretap, 1, rin)
            rec(
                ctx,
                "r5_slip_by_the_difference_brings_the_source_timecode_back_to_what_it_was",
                slipped.get("ok") is True
                and final is not None
                and before is not None
                and (final["sin"], final["sout"]) == (before["sin"], before["sout"])
                and final["src"] == "p2_rep_tc1h_plus10"
                and (final["rin"], final["rout"]) == (before["rin"], before["rout"]),
                error=slipped.get("error"),
                before=before,
                final=final,
            )

        # r6: a clip too short for the range -- nothing changes, nothing is raised
        case = await replace_target(ctx, "R6", clip="p2_len72")
        if case is not None:
            wiretap, rin = case["wt"], case["rin"]
            before = await tx_seg(ctx, wiretap, 1, rin)
            groups = groups_now(ctx)
            env = await replace_call(session, ctx, "r_replace_too_short", case, clips["p2_len12"])
            after = await tx_seg(ctx, wiretap, 1, rin)
            journal = env.get("journal") or {}
            rec(
                ctx,
                "r6_a_clip_too_short_for_the_range_is_a_silent_noop_and_nothing_changed",
                expect_error(env, "SILENT_NOOP")
                and "too short" in err_text(env)
                and after == before
                and groups_now(ctx) == groups + 1
                and bool(journal.get("checkpoint")),
                error=env.get("error"),
                before=before,
                after=after,
            )

        # r7 / r8: the effects stay (media) or go (media_and_fx) -- what Flame does is RECORDED
        for tag, call_id, mode in (
            ("r7_media", "r_replace", None),
            ("r8_media_and_fx", "r_replace_media_and_fx", "media_and_fx"),
        ):
            case = await replace_target(ctx, tag.split("_")[0].upper() + "FX", fx=True)
            if case is None:
                continue
            fx_before = await tx_fx(ctx, case["wt"], 0, case["fxRin"])
            extra = {"mode": mode} if mode else {}
            env = await replace_call(
                session, ctx, call_id, case, clips["clip1"], on_fx=True, **extra
            )
            fx_after = await tx_fx(ctx, case["wt"], 0, case["fxRin"])
            res = _result_of(env)
            rec(
                ctx,
                f"{tag}_the_replace_ran_and_the_result_names_the_effects_before_and_after",
                env.get("ok") is True
                and (res.get("effects") or {}).get("before") == fx_index_order(fx_before)
                and (res.get("effects") or {}).get("after") == fx_index_order(fx_after),
                error=env.get("error"),
                result=res.get("effects"),
                transportBefore=fx_index_order(fx_before),
                transportAfter=fx_index_order(fx_after),
            )
            observe(
                ctx,
                f"{tag}_what_happened_to_the_effects",
                before=fx_index_order(fx_before),
                after=fx_index_order(fx_after),
                mode=mode or "media",
            )

        # r9..: refusals
        case = await replace_target(ctx, "R9")
        if case is not None:
            snap = await state_snapshot(ctx)
            struct = await tx_struct(ctx, case["wt"])
            refused = [
                (
                    "r9_a_source_that_is_a_sequence",
                    "PRECONDITION_UNMET",
                    "not a PyClip",
                    case["wt"],
                    {},
                ),
                ("r10_a_bad_mode", "INVALID_ARGS", "mode", clips["clip1"], {"mode": "everything"}),
            ]
            for tag, code, needle, source_wt, extra in refused:
                env = await replace_call(
                    session, ctx, "r_replace_refused", case, source_wt, **extra
                )
                rec(
                    ctx,
                    f"{tag}_is_refused_before_anything_is_copied_or_journaled",
                    expect_error(env, code)
                    and needle in err_text(env)
                    and await state_snapshot(ctx) == snap,
                    error=env.get("error"),
                )
            gap_rin = track_rows(struct, 1)[0]["rin"]
            gap = await run_call(
                session,
                ctx,
                "r_replace_refused",
                {"target": seg_at(sel(case["wt"]), 1, gap_rin), "source": sel(clips["clip1"])},
            )
            rec(
                ctx,
                "r11_a_gap_is_refused",
                expect_error(gap, "PRECONDITION_UNMET") and "Gap" in err_text(gap),
                error=gap.get("error"),
            )
            wrong = seg_at(sel(case["wt"]), 1, case["rin"], expect="no-such-source")
            conflict = await run_call(
                session, ctx, "r_replace_refused", {"target": wrong, "source": sel(clips["clip1"])}
            )
            rec(
                ctx,
                "r12_a_wrong_expect_source_name_is_a_conflict_before_anything_is_copied",
                expect_error(conflict, "CONFLICT") and await state_snapshot(ctx) == snap,
                error=conflict.get("error"),
            )
            library = await copy_into(ctx, case["wt"], ctx.ids["libReelWt"])
            if library is not None:
                inside = await run_call(
                    session,
                    ctx,
                    "r_replace_refused",
                    {
                        "target": seg_at(sel(library["wt"]), 1, case["rin"]),
                        "source": sel(clips["clip1"]),
                    },
                )
                rec(
                    ctx,
                    "r13_a_segment_inside_a_library_is_refused",
                    expect_error(inside, "PRECONDITION_UNMET")
                    and "inside a library" in err_text(inside),
                    error=inside.get("error"),
                )
            after = await tx_seg(ctx, case["wt"], 1, case["rin"])
            rec(
                ctx,
                "r14_none_of_the_refused_calls_changed_the_segment",
                after is not None and after["src"] == "p2_src_tc1h",
                after=after,
            )
