"""Stages a (reading the audio) and s (timeline.shorten) of the editing e2e; see p3_6_edit_e2e.py.

The sequences with sound are made with `p2_av72` (72 frames, picture and PCM audio) laid on
track 0 of a fresh sequence, where an overwrite was measured to bring its audio in. Every audio
channel and every video track is read back by transport (`tx_full`), never from the op's answer.

Stage a. JUDGED: timeline.info's `audio` (track count, channel count, segment count per channel)
and every segment timeline.list_segments(media='audio') lists agree with what transport reads;
`channel` with media='video' is INVALID_ARGS and a track or channel that is not there is
SELECTOR_EMPTY.

Stage s. The measured case of timeline.shorten (Flame 2025.2.4, 720 -> 360 frames) was a sequence
whose video tracks ended in a Gap and whose audio crossed the new end. This stage builds that: the
picture of p2_av72 is lifted off track 0 with timeline.delete_segments (ripple=false), which
leaves a Gap of its length (s1 judges that the lift succeeded, track 0 is one Gap and the audio
stayed; when it does not, or the lift fails, s1 and s1b are JUDGED failures and so is q3, because
s2 to s9 could not be judged and a run must not be green then). JUDGED there: the new length, the
video rows and every audio channel as they should be (what ends before the new end as it was, what
crosses it cut to end - 1 and still an Audio Segment, what starts at or after it gone), a Gap that
crosses the end cut there, only the checkpoint's copy added to the stash reel, the journal, a
revert putting the fingerprint back, and (s9b) the length, the video rows and the audio rows too.
JUDGED on a measurement (Flame 2025.2.4 and 2026.2.3): s1 (the lift leaves one Gap on track 0 and
both audio channels as they were, one Audio Segment 0-71), s9b (the revert brought back the length
72, the video rows and the audio rows), s10 (a sequence whose tracks end in media, 72 -> 60: the
extract succeeded, track 0 was p2_av72 0-59 with no Gap at the end, track 1 clip1 0-23, Gap 24-47,
clip2 48-59, both audio channels one Audio Segment 0-59) and s10b (its revert put the
fingerprint, the video rows, the audio rows and the length 72 back). Not measured, so not built
here: a start_time other than 0 and a sequence in which every track, the audio channels included,
ends in a Gap; a shorten of a sequence with two or more versions is only refused (s13).
JUDGED refusals: a duration that is not shorter (INVALID_ARGS) and a sequence with two versions
(PRECONDITION_UNMET)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, rec, server_session  # noqa: E402
from p3_2a_e2e_common import copy_into  # noqa: E402
from p3_4_e2e_build import check_journal, revert_restores  # noqa: E402
from p3_6_e2e_common import (  # noqa: E402
    AUDIO_SEGMENT,
    AV,
    GAP,
    added_since,
    audio_channels,
    audio_rows,
    checkpoint_stash_uid,
    err_text,
    expect_error,
    fingerprint,
    frame_of,
    make_av,
    make_s,
    masked,
    readable,
    revert_if_journaled,
    run_call,
    same_video,
    sel,
    shorten_expect,
    stash_entries,
    state_snapshot,
    tx_full,
    video_track,
    video_tracks,
)

S10B_NAME = "s10b_the_revert_of_that_call_puts_the_fingerprint_the_rows_and_the_audio_back"
LIST_LIMIT = 100
S2_NAME = "s2_the_sequence_is_as_long_as_asked"
# s9b and s10b are JUDGED on a measurement (Flame 2025.2.4 and 2026.2.3; see the docstring)
S9B_NAME = "s9b_a_revert_brings_the_length_the_video_rows_and_the_audio_rows_back"


# -- a: reading the audio -----------------------------------------------------------------------


async def stage_a(ctx: Ctx) -> None:
    if not ctx.ids.get("avAudio"):
        rec(ctx, "a0_the_audio_clip_has_audio_so_the_audio_cases_can_run", False)
        return
    case = await make_av(ctx, "AR")
    if case is None:
        return
    wiretap = case["wt"]
    full = await tx_full(ctx, wiretap)
    channels = audio_channels(full)
    rec(
        ctx,
        "a1_transport_reads_audio_segments_on_the_sequence",
        bool(channels) and any(channels.values()),
        audio=(full or {}).get("audio"),
    )
    if not (channels and any(channels.values())):
        return
    async with server_session(ctx, "p36e-a") as session:
        await info_matches(session, ctx, wiretap, full)
        await list_matches(session, ctx, wiretap, channels)
        await list_refusals(session, ctx, wiretap)


async def info_matches(session: Any, ctx: Ctx, wiretap: str, full: dict[str, Any] | None) -> None:
    env = await run_call(session, ctx, "a_info", {"target": sel(wiretap)})
    res = _result_of(env)
    counts = ((full or {}).get("audio") or {}).get("channels") or []
    listed = res.get("audio")
    same = (
        isinstance(listed, list)
        and len(listed) == len(counts)
        and all(
            isinstance(track, dict)
            and track.get("channelCount") == len(chans)
            and [(c or {}).get("segmentCount") for c in track.get("channels") or []] == chans
            for track, chans in zip(listed, counts, strict=True)
        )
    )
    rec(
        ctx,
        "a2_info_lists_the_audio_tracks_their_channels_and_the_segments_of_each",
        env.get("ok") is True and res.get("audioTracks") == len(counts) and same,
        error=env.get("error"),
        info=res.get("audio"),
        transport=counts,
    )


async def list_matches(
    session: Any, ctx: Ctx, wiretap: str, channels: dict[tuple[int, int], list[list[Any]]]
) -> None:
    problems: dict[str, Any] = {}
    for (track, channel), expected in channels.items():
        env = await run_call(
            session,
            ctx,
            "a_list_audio",
            {
                "target": sel(wiretap),
                "media": "audio",
                "track": track,
                "channel": channel,
                "limit": LIST_LIMIT,
            },
        )
        res = _result_of(env)
        items = res.get("items") or []
        listed = [_listed_row(i) for i in items]
        if (
            env.get("ok") is not True
            or listed != expected
            or res.get("total") != len(expected)
            or [i.get("index") for i in items] != list(range(len(expected)))
        ):
            problems[f"{track}/{channel}"] = {
                "error": env.get("error"),
                "listed": listed,
                "expected": expected,
            }
    rec(
        ctx,
        "a3_list_segments_media_audio_agrees_with_transport_on_every_channel",
        not problems and bool(channels),
        channels=len(channels),
        problems=problems,
    )


def _listed_row(item: dict[str, Any]) -> list[Any]:
    if item.get("type") == GAP:
        return [GAP, frame_of(item.get("recordIn")), frame_of(item.get("recordOut"))]
    return [
        item.get("type"),
        frame_of(item.get("recordIn")),
        frame_of(item.get("recordOut")),
        frame_of(item.get("sourceIn")),
        frame_of(item.get("sourceOut")),
        item.get("sourceName"),
    ]


async def list_refusals(session: Any, ctx: Ctx, wiretap: str) -> None:
    target = sel(wiretap)
    cases = [
        (
            "a4_a_channel_with_media_video",
            {"target": target, "media": "video", "track": 0, "channel": 0},
            "INVALID_ARGS",
        ),
        (
            "a5_a_channel_that_is_not_there",
            {"target": target, "media": "audio", "track": 0, "channel": 99},
            "SELECTOR_EMPTY",
        ),
        (
            "a6_an_audio_track_that_is_not_there",
            {"target": target, "media": "audio", "track": 9, "channel": 0},
            "SELECTOR_EMPTY",
        ),
    ]
    for tag, args, code in cases:
        env = await run_call(session, ctx, "a_list_refused", args)
        rec(
            ctx,
            f"{tag}_is_refused",
            expect_error(env, code),
            error=err_text(env),
            errorCode=env.get("errorCode"),
        )


# -- s: timeline.shorten ------------------------------------------------------------------------


async def stage_s(ctx: Ctx) -> None:
    async with server_session(ctx, "p36e-s") as session:
        if ctx.ids.get("avAudio"):
            await shorten_gap_tail(session, ctx)
            await shorten_media_tail(session, ctx)
        else:
            rec(ctx, "s0_the_audio_clip_has_audio_so_the_audio_cases_can_run", False)
        await shorten_refusals(session, ctx)


def tracks_as_expected(
    before: dict[str, Any] | None, after: dict[str, Any] | None, end: int
) -> bool:
    """Every video track of `after` is what `shorten_expect` says of the same track of `before`."""
    if not (readable(before) and readable(after)):
        return False
    tracks_before = video_tracks(before)
    tracks_after = video_tracks(after)
    return (
        bool(tracks_before)
        and len(tracks_before) == len(tracks_after)
        and all(
            masked(now, end) == shorten_expect(was, end)
            for was, now in zip(tracks_before, tracks_after, strict=True)
        )
    )


def audio_as_expected(
    before: dict[str, Any] | None, after: dict[str, Any] | None, end: int
) -> bool:
    """Every audio channel of `after` is what `shorten_expect` says of the same channel of
    `before`."""
    was = audio_channels(before)
    now = audio_channels(after)
    return (
        bool(was)
        and set(was) == set(now)
        and all(masked(now[key], end) == shorten_expect(was[key], end) for key in was)
    )


async def shorten_gap_tail(session: Any, ctx: Ctx) -> None:
    case = await make_av(ctx, "SG", ((0, AV, 1), (1, "clip1", 1)))
    if case is None:
        return
    wiretap = case["wt"]
    lift = await run_call(
        session,
        ctx,
        "d_delete_lift",
        {"sequence": sel(wiretap), "segments": [0], "track": 0, "ripple": False},
    )
    before = await tx_full(ctx, wiretap)
    track0 = video_track(before, 0)
    gap_only = readable(before) and len(track0) == 1 and track0[0][0] == GAP
    has_audio = any(audio_channels(before).values())
    # JUDGED on a measurement (Flame 2025.2.4; see the module docstring): the lift
    # succeeded, track 0 was one Gap and both audio channels stayed (Audio Segment 0-71).
    rec(
        ctx,
        "s1_the_picture_lifted_off_track_0_leaves_a_gap_and_the_audio_stays",
        lift.get("ok") is True and gap_only and has_audio,
        errorCode=lift.get("errorCode"),
        error=err_text(lift)[:400],
        gapOnly=gap_only,
        hasAudio=has_audio,
        tracks=video_tracks(before),
        audio=audio_rows(before),
    )
    length = (before or {}).get("duration")
    start = (before or {}).get("startTime")
    built = lift.get("ok") is True and gap_only and has_audio
    readable_length = isinstance(length, int) and isinstance(start, int) and length >= 4
    rec(
        ctx,
        "s1b_the_measured_case_was_built_so_s2_to_s9_could_be_judged",
        built and readable_length,
        liftOk=lift.get("ok"),
        liftErrorCode=lift.get("errorCode"),
        gapOnly=gap_only,
        hasAudio=has_audio,
        length=length,
        start=start,
        why=(
            "timeline.delete_segments(ripple=false) did not succeed"
            if lift.get("ok") is not True
            else "track 0 is not one Gap after the lift"
            if not gap_only
            else "no audio segment is left after the lift"
            if not has_audio
            else "the length or the start time of the sequence could not be read (length >= 4)"
            if not readable_length
            else None
        ),
    )
    if not (built and readable_length):
        return
    duration = length // 2
    end = start + duration
    fp0 = await fingerprint(session, ctx, wiretap)
    stash0 = await stash_entries(ctx)
    pair_before = await copy_into(ctx, wiretap, ctx.ids["seqReelWt"])
    env = await run_call(
        session, ctx, "s_shorten", {"sequence": sel(wiretap), "duration": duration}
    )
    res = _result_of(env)
    after = await tx_full(ctx, wiretap)
    stash1 = await stash_entries(ctx)
    pair_after = await copy_into(ctx, wiretap, ctx.ids["seqReelWt"])
    ctx.ids["shortenPair"] = {
        "before": pair_before,
        "after": pair_after,
        "end": end,
        "rowsBefore": video_tracks(before),
        "audioBefore": audio_rows(before),
    }
    rec(
        ctx,
        S2_NAME,
        env.get("ok") is True
        and (after or {}).get("duration") == duration
        and res.get("duration") == duration
        and res.get("end") == end
        and res.get("changed") is True,
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        duration=(after or {}).get("duration"),
        result={k: res.get(k) for k in ("duration", "end", "changed", "after")},
    )
    rec(
        ctx,
        "s3_every_video_track_keeps_what_ended_before_the_end_and_loses_the_rest",
        env.get("ok") is True and tracks_as_expected(before, after, end),
        end=end,
        before=video_tracks(before),
        after=video_tracks(after),
    )
    rec(
        ctx,
        "s4_the_audio_is_cut_at_the_end_and_stays_an_audio_segment",
        env.get("ok") is True
        and audio_as_expected(before, after, end)
        and any(
            row[0] == AUDIO_SEGMENT and row[2] == end - 1
            for rows in audio_channels(after).values()
            for row in rows
        ),
        end=end,
        before=audio_rows(before),
        after=audio_rows(after),
    )
    rec(
        ctx,
        "s5_a_gap_that_crosses_the_end_is_cut_there",
        env.get("ok") is True and video_track(after, 0) == [[GAP, track0[0][1], end - 1]],
        track0=video_track(after, 0),
    )
    rec(
        ctx,
        "s6_the_result_says_which_tracks_were_cut",
        any(t.get("media") == "video" and t.get("cut") for t in res.get("tracks") or [])
        and any(t.get("media") == "audio" and t.get("cut") for t in res.get("tracks") or []),
        tracks=res.get("tracks"),
    )
    added = added_since(stash0, stash1)
    rec(
        ctx,
        "s7_the_by_product_clip_is_gone_only_the_checkpoint_copy_is_in_the_stash_reel",
        env.get("ok") is True and len(added) == 1 and added[0][3] == checkpoint_stash_uid(ctx, env),
        added=added,
        cleanup=res.get("cleanup"),
        extracted=res.get("extracted"),
    )
    await check_journal(ctx, "s8", env)
    await revert_restores(session, ctx, "s9", env, case, fp0)
    reverted = await tx_full(ctx, case["wt"])
    # JUDGED on a measurement (Flame 2025.2.4; see the module docstring).
    video_back = video_tracks(reverted) == video_tracks(before)
    audio_back = audio_rows(reverted) == audio_rows(before)
    rec(
        ctx,
        S9B_NAME,
        readable(reverted)
        and (reverted or {}).get("duration") == length
        and video_back
        and audio_back
        and bool(audio_rows(before)),
        durationBack=(reverted or {}).get("duration"),
        durationBefore=length,
        videoRowsEqual=video_back,
        audioRowsEqual=audio_back,
        audioBefore=audio_rows(before),
        audioAfterRevert=audio_rows(reverted),
    )


async def shorten_media_tail(session: Any, ctx: Ctx) -> None:
    case = await make_av(ctx, "SA")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_full(ctx, wiretap)
    length = (before or {}).get("duration")
    start = (before or {}).get("startTime")
    if not (isinstance(length, int) and isinstance(start, int) and length >= 4):
        rec(
            ctx,
            "s10_the_length_or_start_of_the_sequence_could_not_be_read",
            False,
            duration=length,
            startTime=start,
        )
        return
    duration = (length * 5) // 6
    end = start + duration
    fp0 = await fingerprint(session, ctx, wiretap)
    env = await run_call(
        session, ctx, "s_shorten", {"sequence": sel(wiretap), "duration": duration}
    )
    after = await tx_full(ctx, wiretap)
    # JUDGED on a measurement (Flame 2025.2.4; see the module docstring):
    # 72 -> 60 succeeded; track 0 = p2_av72 0-59 with no Gap at the end, track 1 = clip1 0-23,
    # Gap 24-47, clip2 48-59, both audio channels one Audio Segment 0-59.
    video_ok = tracks_as_expected(before, after, end)
    audio_ok = audio_as_expected(before, after, end)
    rec(
        ctx,
        "s10_shortening_where_the_tracks_end_in_media_cuts_video_and_audio_at_the_new_end",
        env.get("ok") is True
        and (after or {}).get("duration") == duration
        and video_ok
        and audio_ok,
        errorCode=env.get("errorCode"),
        error=err_text(env)[:600],
        end=end,
        durationAfter=(after or {}).get("duration"),
        videoAsExpected=video_ok,
        audioAsExpected=audio_ok,
        before=video_tracks(before),
        after=video_tracks(after),
        audioBefore=audio_rows(before),
        audioAfter=audio_rows(after),
    )
    back = await revert_if_journaled(session, ctx, env, case)
    if back is None:
        rec(ctx, S10B_NAME, False, why="the call was not journaled, so there was nothing to revert")
        return
    reverted = await tx_full(ctx, case["wt"])
    fingerprint_equal = await fingerprint(session, ctx, case["wt"]) == fp0
    video_back = video_tracks(reverted) == video_tracks(before)
    audio_back = audio_rows(reverted) == audio_rows(before)
    rec(
        ctx,
        S10B_NAME,
        back.get("ok") is True
        and fingerprint_equal
        and video_back
        and audio_back
        and bool(audio_rows(before))
        and (reverted or {}).get("duration") == length,
        revertOk=back.get("ok"),
        fingerprintEqual=fingerprint_equal,
        videoRowsEqual=video_back,
        audioRowsEqual=audio_back,
        durationBack=(reverted or {}).get("duration"),
    )


async def shorten_refusals(session: Any, ctx: Ctx) -> None:
    case = await make_s(ctx, "SR")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_full(ctx, wiretap)
    length = (before or {}).get("duration")
    if not isinstance(length, int):
        rec(ctx, "s11_the_length_of_the_sequence_can_be_read", False, duration=length)
        return
    snap = await state_snapshot(ctx)
    for tag, duration in (
        ("s11_a_duration_equal_to_the_length", length),
        ("s12_a_duration_longer_than_the_sequence", length + 50),
    ):
        env = await run_call(
            session, ctx, "s_shorten_refused", {"sequence": sel(wiretap), "duration": duration}
        )
        rec(
            ctx,
            f"{tag}_is_invalid_args_and_nothing_is_copied_or_journaled",
            expect_error(env, "INVALID_ARGS")
            and await state_snapshot(ctx) == snap
            and same_video(await tx_full(ctx, wiretap), before),
            error=err_text(env),
            errorCode=env.get("errorCode"),
        )
    version = await run_call(session, ctx, "v_create_version", {"sequence": sel(wiretap)})
    snap = await state_snapshot(ctx)
    two = await run_call(
        session, ctx, "s_shorten_refused", {"sequence": sel(wiretap), "duration": 30}
    )
    rec(
        ctx,
        "s13_a_sequence_with_two_versions_is_refused_and_nothing_is_copied_or_journaled",
        version.get("ok") is True
        and expect_error(two, "PRECONDITION_UNMET")
        and "version" in err_text(two)
        and await state_snapshot(ctx) == snap,
        error=err_text(two),
        errorCode=two.get("errorCode"),
    )
