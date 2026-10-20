"""The delete stage (timeline.delete_segments) of the editing e2e; see p3_6_edit_e2e.py.

Every case is a fresh sequence (`make_s`: track 0 = clip1 0-23, a Gap 24-47, clip2 48-71, p2_len48
72-119, track 1 = p2_len72 0-71; `make_contiguous`: track 0 = clip1 0-23, clip2 24-47, p2_len48
48-95, track 1 = p2_len72 0-71). What a call did is read back by transport (`tx_full`), never from
the op's answer.

JUDGED (measured on Flame 2025.2.4 and written in the op's description): with ripple the later
media of THAT track moves left by the length removed and the content of the other track stays; the
sequence is then as long as the end of the longest content of any track and the Gap at the end of
every track (the other track's and the audio's too) follows that end, a track that ends in content
at the end having none (measured); without ripple the segment becomes a Gap of the same
length and nothing moves; a Gap can be deleted (closed) too; the by-product clip of the call is
gone from the stash reel (only the checkpoint's copy is added); a revert puts the fingerprint
back; a record_in that no segment has, a source name that differs and arguments that do not fit
are refused before anything is copied or journaled."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, rec, server_session  # noqa: E402
from p3_4_e2e_build import check_journal, revert_restores  # noqa: E402
from p3_6_e2e_common import (  # noqa: E402
    CONTIGUOUS_SHAPE,
    GAP,
    S1_SHAPE,
    added_since,
    checkpoint_stash_uid,
    end_after_delete,
    err_text,
    expect_error,
    fingerprint,
    frame_of,
    gaps_follow,
    make_contiguous,
    make_s,
    media_after_delete,
    media_only,
    raw_track,
    readable,
    run_call,
    same_video,
    sel,
    stash_entries,
    state_snapshot,
    strip_trailing_gaps,
    tx_full,
    video_shape,
    video_track,
    video_tracks,
    vnorm,
)


async def stage_d(ctx: Ctx) -> None:
    async with server_session(ctx, "p36e-d") as session:
        await delete_with_ripple(session, ctx)
        await delete_without_ripple_and_a_gap(session, ctx)


async def delete_with_ripple(session: Any, ctx: Ctx) -> None:
    case = await make_s(ctx, "DL")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_full(ctx, wiretap)
    rec(
        ctx,
        "d0_the_material_was_read_back_and_has_the_rows_it_was_made_with",
        video_shape(before) == S1_SHAPE,
        shape=video_shape(before),
        expected=S1_SHAPE,
    )
    stash0 = await stash_entries(ctx)
    fp0 = await fingerprint(session, ctx, wiretap)
    args = {
        "sequence": sel(wiretap),
        "segments": [48],
        "track": 0,
        "expect_source_names": ["clip2"],
    }
    env = await run_call(session, ctx, "d_delete", args)
    res = _result_of(env)
    after = await tx_full(ctx, wiretap)
    stash1 = await stash_entries(ctx)
    rows_before = raw_track(before, 0)
    rows_after = video_track(after, 0)
    rec(
        ctx,
        "d1_a_ripple_delete_moves_the_later_media_of_that_track_left_and_not_the_other_track",
        env.get("ok") is True
        and readable(before)
        and readable(after)
        and media_only(rows_after) == media_after_delete(rows_before, [48], ripple=True)
        and all(r[5] != "clip2" for r in media_only(rows_after))
        and strip_trailing_gaps(video_track(after, 1))
        == strip_trailing_gaps(video_track(before, 1)),
        error=env.get("error"),
        before=video_track(before, 0),
        after=rows_after,
    )
    end = end_after_delete(before, [48])
    rec(
        ctx,
        "d2_the_sequence_ends_where_the_longest_content_ends_and_every_gap_at_the_end_follows",
        end is not None and gaps_follow(after, end),
        expectedEnd=end,
        duration=(after or {}).get("duration"),
        startTime=(after or {}).get("startTime"),
        durationBefore=(before or {}).get("duration"),
        video=video_tracks(after),
        audio=(after or {}).get("audio"),
    )
    removed = (res.get("removed") or [{}])[0]
    rec(
        ctx,
        "d3_the_result_says_what_was_removed_and_how_far_the_rest_moved",
        res.get("ripple") is True
        and res.get("changed") is True
        and res.get("shifted") == 24
        and removed.get("sourceName") == "clip2"
        and frame_of(removed.get("recordIn")) == 48
        and removed.get("duration") == 24,
        result={k: res.get(k) for k in ("ripple", "method", "shifted", "removed", "changed")},
    )
    await check_journal(ctx, "d4", env)
    added = added_since(stash0, stash1)
    rec(
        ctx,
        "d5_the_by_product_clip_is_gone_only_the_checkpoint_copy_is_in_the_stash_reel",
        env.get("ok") is True and len(added) == 1 and added[0][3] == checkpoint_stash_uid(ctx, env),
        added=added,
        cleanup=res.get("cleanup"),
        extracted=res.get("extracted"),
    )
    await revert_restores(session, ctx, "d6", env, case, fp0)
    restored = await tx_full(ctx, case["wt"])
    rec(
        ctx,
        "d6b_the_reverted_sequence_has_the_rows_it_had",
        same_video(restored, before),
        before=video_tracks(before),
        restored=video_tracks(restored),
    )


async def delete_without_ripple_and_a_gap(session: Any, ctx: Ctx) -> None:
    case = await make_contiguous(ctx, "DR")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_full(ctx, wiretap)
    rec(
        ctx,
        "d0b_the_contiguous_material_was_read_back_and_has_the_rows_it_was_made_with",
        video_shape(before) == CONTIGUOUS_SHAPE,
        shape=video_shape(before),
        expected=CONTIGUOUS_SHAPE,
    )
    fp0 = await fingerprint(session, ctx, wiretap)
    lift = await run_call(
        session,
        ctx,
        "d_delete_lift",
        {"sequence": sel(wiretap), "segments": [24], "track": 0, "ripple": False},
    )
    lifted = await tx_full(ctx, wiretap)
    expected = [[GAP, 24, 47] if r["rin"] == 24 else vnorm(r) for r in raw_track(before, 0)]
    rec(
        ctx,
        "d7_without_ripple_a_gap_of_the_same_length_is_left_and_nothing_moves",
        lift.get("ok") is True
        and readable(before)
        and readable(lifted)
        and video_track(lifted, 0) == expected
        and video_track(lifted, 1) == video_track(before, 1)
        and _result_of(lift).get("ripple") is False
        and _result_of(lift).get("shifted") == 0,
        error=lift.get("error"),
        expected=expected,
        after=video_track(lifted, 0),
    )
    fp1 = await fingerprint(session, ctx, wiretap)
    gap = await run_call(
        session, ctx, "d_delete_gap", {"sequence": sel(wiretap), "segments": [24], "track": 0}
    )
    closed = await tx_full(ctx, wiretap)
    removed = (_result_of(gap).get("removed") or [{}])[0]
    rec(
        ctx,
        "d8_deleting_a_gap_with_ripple_closes_it",
        gap.get("ok") is True
        and readable(lifted)
        and readable(closed)
        and media_only(video_track(closed, 0))
        == media_after_delete(raw_track(lifted, 0), [24], ripple=True)
        and strip_trailing_gaps(video_track(closed, 1))
        == strip_trailing_gaps(video_track(before, 1))
        and gaps_follow(closed, end_after_delete(lifted, [24]))
        and removed.get("type") == GAP
        and removed.get("duration") == 24,
        error=gap.get("error"),
        expectedEnd=end_after_delete(lifted, [24]),
        duration=(closed or {}).get("duration"),
        after=video_tracks(closed),
    )
    await check_journal(ctx, "d9", gap)
    await revert_restores(session, ctx, "d10", gap, case, fp1)
    await revert_restores(session, ctx, "d11", lift, case, fp0)
    restored = await tx_full(ctx, case["wt"])
    rec(
        ctx,
        "d11b_both_reverts_leave_the_rows_the_sequence_started_with",
        same_video(restored, before),
        before=video_tracks(before),
        restored=video_tracks(restored),
    )
    await delete_refusals(session, ctx, case["wt"])


async def delete_refusals(session: Any, ctx: Ctx, wiretap: str) -> None:
    snap = await state_snapshot(ctx)
    current = await tx_full(ctx, wiretap)
    base = {"sequence": sel(wiretap), "track": 0}
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("d12_a_record_in_inside_a_segment", {"segments": [5]}, "SELECTOR_EMPTY"),
        ("d13_a_record_in_after_the_end", {"segments": [500]}, "SELECTOR_EMPTY"),
        (
            "d14_a_source_name_that_differs",
            {"segments": [24], "expect_source_names": ["clip1"]},
            "CONFLICT",
        ),
        ("d15_the_same_record_in_twice", {"segments": [24, 24]}, "INVALID_ARGS"),
        (
            "d16_expect_source_names_of_another_length",
            {"segments": [24], "expect_source_names": ["clip2", "clip1"]},
            "INVALID_ARGS",
        ),
        ("d17_a_track_that_is_not_there", {"segments": [24], "track": 9}, "SELECTOR_EMPTY"),
    ]
    for tag, extra, code in cases:
        env = await run_call(session, ctx, "d_delete_refused", {**base, **extra})
        unchanged = await tx_full(ctx, wiretap)
        rec(
            ctx,
            f"{tag}_is_refused_before_anything_is_copied_or_journaled",
            expect_error(env, code)
            and await state_snapshot(ctx) == snap
            and same_video(unchanged, current),
            error=err_text(env),
            errorCode=env.get("errorCode"),
        )
