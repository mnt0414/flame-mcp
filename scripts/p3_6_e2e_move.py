"""Stage m (timeline.move_track) of the editing e2e; see p3_6_edit_e2e.py.

The order of the tracks and the rows of every track are read back by transport (`tx_full`), never
from the op's answer. A track is compared by [type, record range, source range, source_name,
effect types] of its segments, the Gap(s) at its end left out (a moved track is rebuilt on a new
track that is one Gap as long as the sequence), a Gap by type and range only.

JUDGED (measured on Flame 2025.2.4 and written in the op's description): the top media track goes
to the bottom and the bottom one to the top with every track keeping its rows and its effect
types, a track that holds only a Gap moves, a revert puts the fingerprint and the rows back, the
audio of a sequence is not touched when the track moved is not the one the audio clip was laid
on, and the refusals made before anything is copied (to == track, a version with a single track,
a track or a target that is not there). Nothing in this stage is only observed.

m9 is JUDGED on a measurement: on Flame 2025.2.4 a sequence with sound (one audio
track of 2 channels, four video tracks) had its track 2 (two media segments and a Gap) moved to 0,
the bottom, with `timeline.move_track`, and result.audio.before and result.audio.after were
identical (changed=false, complete=true). That track held no clip with sound, which is what m9
builds (track 1 of the sequence with sound: clip1, a Gap, clip2).

m10b, m11 and m11b are JUDGED on a measurement too (Flame 2025.2.4 and 2026.2.3): after the revert
of m9's move the audio rows were as they were (m10b); moving the track that holds the audio clip
(track 0, `p2_av72`, which brought the audio in) to the top succeeded with the order as expected,
the audio rows unchanged and result.audio changed=false, complete=true (m11); and the revert of
that move put the fingerprint, the video rows and the audio rows back (m11b). The audio never
changed in these moves, so what they say about a revert is only that it leaves the audio as it
was; whether a revert puts back an audio-only change was not measured here."""

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
    LETTERBOX,
    audio_rows,
    err_text,
    expect_error,
    fingerprint,
    make_av,
    make_effect,
    make_s,
    revert_if_journaled,
    run_call,
    same_video,
    sel,
    state_snapshot,
    strip_trailing_gaps,
    tx_full,
    video_tracks,
)


def signatures(full: dict[str, Any] | None) -> list[list[list[Any]]]:
    """The rows of every video track of version 0, the Gap(s) at the end of a track left out."""
    return [strip_trailing_gaps(t) for t in video_tracks(full)]


async def stage_m(ctx: Ctx) -> None:
    async with server_session(ctx, "p36e-m") as session:
        await move_media_tracks(session, ctx)
        await move_with_audio(session, ctx)
        await move_refusals(session, ctx)


async def move_media_tracks(session: Any, ctx: Ctx) -> None:
    case = await make_effect(ctx, "MV")
    if case is None:
        return
    wiretap = case["wt"]
    added = await run_call(session, ctx, "v_create_track", {"sequence": sel(wiretap)})
    base = await tx_full(ctx, wiretap)
    sigs = signatures(base)
    carries_fx = any(r[0] != "Gap" and r[6] == [LETTERBOX] for r in sigs[0]) if sigs else False
    rec(
        ctx,
        "m0_the_material_has_three_tracks_the_top_one_empty_and_an_effect_on_track_0",
        added.get("ok") is True and len(sigs) == 3 and sigs[2] == [] and carries_fx,
        error=added.get("error"),
        tracks=sigs,
    )
    if len(sigs) != 3:
        return
    fp_start = await fingerprint(session, ctx, wiretap)

    top_gap = await run_call(
        session, ctx, "m_move", {"sequence": sel(wiretap), "track": 2, "to": 0}
    )
    moved = await tx_full(ctx, wiretap)
    res = _result_of(top_gap)
    rec(
        ctx,
        "m1_a_track_that_holds_only_a_gap_moves_to_the_bottom_and_the_others_keep_their_rows",
        top_gap.get("ok") is True
        and signatures(moved) == [sigs[2], sigs[0], sigs[1]]
        and (res.get("moved") or {}).get("from") == 2
        and (res.get("moved") or {}).get("to") == 0
        and (res.get("verification") or {}).get("ok") is True,
        error=top_gap.get("error"),
        order=signatures(moved),
        moved=res.get("moved"),
    )
    await check_journal(ctx, "m2", top_gap)
    await revert_restores(session, ctx, "m3", top_gap, case, fp_start)
    rec(
        ctx,
        "m3b_the_reverted_sequence_has_the_rows_and_the_order_it_had",
        signatures(await tx_full(ctx, case["wt"])) == sigs,
    )

    wiretap = case["wt"]
    top_media = await run_call(
        session, ctx, "m_move", {"sequence": sel(wiretap), "track": 1, "to": 0}
    )
    down = await tx_full(ctx, wiretap)
    rec(
        ctx,
        "m4_the_top_media_track_moves_to_the_bottom_and_every_track_keeps_its_rows",
        top_media.get("ok") is True and signatures(down) == [sigs[1], sigs[0], sigs[2]],
        error=top_media.get("error"),
        order=signatures(down),
    )
    fp_down = await fingerprint(session, ctx, wiretap)
    bottom = await run_call(session, ctx, "m_move", {"sequence": sel(wiretap), "track": 0, "to": 2})
    up = await tx_full(ctx, wiretap)
    rec(
        ctx,
        "m5_the_bottom_track_moves_to_the_top_and_every_track_keeps_its_rows",
        bottom.get("ok") is True and signatures(up) == [sigs[0], sigs[2], sigs[1]],
        error=bottom.get("error"),
        order=signatures(up),
    )
    await revert_restores(session, ctx, "m6", bottom, case, fp_down)
    await revert_restores(session, ctx, "m7", top_media, case, fp_start)
    rec(
        ctx,
        "m7b_both_reverts_leave_the_rows_and_the_order_the_sequence_started_with",
        signatures(await tx_full(ctx, case["wt"])) == sigs,
        order=signatures(await tx_full(ctx, case["wt"])),
    )


async def move_with_audio(session: Any, ctx: Ctx) -> None:
    if not ctx.ids.get("avAudio"):
        rec(ctx, "m8_the_audio_clip_has_audio_so_the_audio_cases_can_run", False)
        return
    case = await make_av(ctx, "MA")
    if case is None:
        return
    wiretap = case["wt"]
    base = await tx_full(ctx, wiretap)
    sigs = signatures(base)
    audio0 = audio_rows(base)
    rec(
        ctx,
        "m8_the_sequence_with_sound_has_audio_segments_and_two_tracks",
        len(sigs) == 2 and bool(audio0),
        tracks=sigs,
        audio=audio0,
    )
    if len(sigs) != 2 or not audio0:
        return
    fp0 = await fingerprint(session, ctx, wiretap)
    env = await run_call(session, ctx, "m_move", {"sequence": sel(wiretap), "track": 1, "to": 0})
    after = await tx_full(ctx, wiretap)
    res = _result_of(env)
    result_audio = res.get("audio") or {}
    # JUDGED on a measurement (Flame 2025.2.4; see the docstring of this module): the
    # track moved holds no clip with sound, and result.audio was unchanged and complete.
    rec(
        ctx,
        "m9_moving_a_track_that_holds_no_audio_clip_leaves_the_audio_as_it_was",
        env.get("ok") is True
        and signatures(after) == [sigs[1], sigs[0]]
        and audio_rows(after) == audio0
        and result_audio.get("changed") is False
        and result_audio.get("complete") is True,
        error=env.get("error"),
        order=signatures(after),
        audioBefore=audio0,
        audioAfter=audio_rows(after),
        resultAudio={k: result_audio.get(k) for k in ("changed", "complete")},
    )
    await revert_restores(session, ctx, "m10", env, case, fp0)
    reverted = await tx_full(ctx, case["wt"])
    # JUDGED on a measurement (Flame 2025.2.4; see the module docstring).
    rec(
        ctx,
        "m10b_the_audio_is_as_it_was_after_the_revert",
        audio_rows(reverted) == audio0,
        rowsEqual=audio_rows(reverted) == audio0,
        audioBefore=audio0,
        audioAfter=audio_rows(reverted),
    )
    wiretap = case["wt"]
    fp1 = await fingerprint(session, ctx, wiretap)
    audio_track = await run_call(
        session, ctx, "m_move", {"sequence": sel(wiretap), "track": 0, "to": 1}
    )
    rebuilt = await tx_full(ctx, wiretap)
    audio_result = _result_of(audio_track).get("audio") or {}
    # JUDGED on a measurement (Flame 2025.2.4; see the module docstring): the move
    # succeeded, the order is the old one with track 0 on top, the audio rows are unchanged and
    # result.audio says changed=false, complete=true.
    rec(
        ctx,
        "m11_moving_the_track_that_holds_the_audio_clip_leaves_the_audio_as_it_was",
        audio_track.get("ok") is True
        and signatures(rebuilt) == [sigs[1], sigs[0]]
        and audio_rows(rebuilt) == audio0
        and audio_result.get("changed") is False
        and audio_result.get("complete") is True,
        errorCode=audio_track.get("errorCode"),
        error=err_text(audio_track)[:400],
        order=signatures(rebuilt),
        audioRowsEqual=audio_rows(rebuilt) == audio0,
        resultAudio={k: audio_result.get(k) for k in ("changed", "complete")},
    )
    back = await revert_if_journaled(session, ctx, audio_track, case)
    if back is None:
        rec(ctx, "m11b_the_move_was_journaled_so_it_could_be_reverted", False)
        return
    restored = await tx_full(ctx, case["wt"])
    fingerprint_equal = await fingerprint(session, ctx, case["wt"]) == fp1
    rows_equal = signatures(restored) == sigs
    audio_equal = audio_rows(restored) == audio0
    rec(
        ctx,
        "m11b_the_revert_of_that_move_puts_the_fingerprint_the_rows_and_the_audio_back",
        back.get("ok") is True and fingerprint_equal and rows_equal and audio_equal,
        revertOk=back.get("ok"),
        fingerprintEqual=fingerprint_equal,
        rowsEqual=rows_equal,
        audioRowsEqual=audio_equal,
    )


async def move_refusals(session: Any, ctx: Ctx) -> None:
    case = await make_s(ctx, "MR")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_full(ctx, wiretap)
    snap = await state_snapshot(ctx)
    base = {"sequence": sel(wiretap)}
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("m12_to_equal_to_the_track", {"track": 1, "to": 1}, "INVALID_ARGS"),
        ("m13_a_track_that_is_not_there", {"track": 9, "to": 0}, "SELECTOR_EMPTY"),
        ("m14_a_target_that_is_not_there", {"track": 1, "to": 9}, "SELECTOR_EMPTY"),
    ]
    for tag, extra, code in cases:
        env = await run_call(session, ctx, "m_move_refused", {**base, **extra})
        rec(
            ctx,
            f"{tag}_is_refused_before_anything_is_copied_or_journaled",
            expect_error(env, code)
            and await state_snapshot(ctx) == snap
            and same_video(await tx_full(ctx, wiretap), before),
            error=err_text(env),
            errorCode=env.get("errorCode"),
        )
    version = await run_call(session, ctx, "v_create_version", base)
    snap = await state_snapshot(ctx)
    single = await run_call(
        session, ctx, "m_move_refused", {**base, "version": 1, "track": 0, "to": 1}
    )
    rec(
        ctx,
        "m15_a_version_with_a_single_track_is_refused_before_anything_is_copied_or_journaled",
        version.get("ok") is True
        and expect_error(single, "PRECONDITION_UNMET")
        and await state_snapshot(ctx) == snap,
        error=err_text(single),
        errorCode=single.get("errorCode"),
    )
