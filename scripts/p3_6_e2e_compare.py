"""Stages q (timeline.compare), c (segment.connections) and y (the read-only policy) of the editing
e2e; see p3_6_edit_e2e.py.

Stage q compares copies of one sequence (`flame.media_panel.copy`, made by transport): equal when
nothing was done to either, and different where the edit was done when `timeline.delete_segments`,
`timeline.create_track` or `timeline.shorten` changed one of them. JUDGED: a copy equals its
original, a segment deleted from a copy shows up at its position and, on the track nobody touched,
only at the Gap at its end (the Gap at the end of every track follows the new length, measured),
a track one side lacks is a difference and an empty track is listed in `emptyTracks`,
and the differences between a sequence and its shortened self are all at or after the new end (q3,
video) and, JUDGED on a measurement (Flame 2025.2.4 and 2026.2.3: copying a sequence with sound
keeps the audio, and a 72-frame sequence with two audio channels of one Audio Segment 0-71 each,
shortened to 36, differs from its original in the recordOut and sourceOut of segment 0 of each
channel only, 71 against 35), the audio differences
are exactly the cut ends of the segments that cross the new end (q3b).

Stage c reads `segment.connections` of segments that have no connection: no other segment is
connected, while `PySegment.connected_segments()` read by transport lists the segment itself
(measured), so it answers one. The three write ops (`segment.create_connection`,
`segment.remove_connection`, `segment.sync_connections`) are NOT called: the connections of a
segment may reach segments in other sequences, which no checkpoint covers, and what they do there
was not measured.

Stage y: a READONLY server denies `timeline.delete_segments`, `timeline.move_track` and
`timeline.shorten`, and still answers the two reads."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, rec, server_session  # noqa: E402
from p3_2a_e2e_common import copy_into  # noqa: E402
from p3_6_e2e_common import (  # noqa: E402
    err_text,
    make_s,
    run_call,
    same_video,
    seg_at,
    sel,
    state_snapshot,
    tx_connected,
    tx_full,
    tx_struct,
)

COMPARE_LIMIT = 200
WHERE_PATTERN = re.compile(r"^video track (\d+)(?: segment (\d+))?$")
Q3_NAME = "q3_a_sequence_and_its_shortened_self_differ_only_at_or_after_the_new_end"
Q3B_NAME = "q3b_the_audio_differences_are_the_cut_ends_of_the_segments_that_cross_the_new_end"


def compare_args(a: str, b: str) -> dict[str, Any]:
    return {"a": sel(a), "b": sel(b), "limit": COMPARE_LIMIT}


async def stage_q(ctx: Ctx) -> None:
    case = await make_s(ctx, "QA")
    if case is None:
        return
    original = case["wt"]
    copies = [await copy_into(ctx, original, ctx.ids["seqReelWt"]) for _ in range(3)]
    if any(c is None for c in copies):
        rec(ctx, "q0_three_copies_of_the_sequence_were_made", False, copies=copies)
        return
    same, edited, extended = (c["wt"] for c in copies if c is not None)
    ctx.ids["compareA"] = same
    async with server_session(ctx, "p36e-q") as session:
        await equal_copies(session, ctx, original, same)
        await deleted_segment(session, ctx, same, edited)
        await shortened_pair(session, ctx)
        await extra_track(session, ctx, same, extended)


async def equal_copies(session: Any, ctx: Ctx, original: str, copy: str) -> None:
    snap = await state_snapshot(ctx)
    before = await tx_struct(ctx, original)
    forward = await run_call(session, ctx, "q_compare", compare_args(original, copy))
    backward = await run_call(session, ctx, "q_compare", compare_args(copy, original))
    res = _result_of(forward)
    rec(
        ctx,
        "q1_a_copy_equals_its_original_both_ways_and_no_track_is_empty",
        forward.get("ok") is True
        and backward.get("ok") is True
        and res.get("equal") is True
        and res.get("differenceCount") == 0
        and res.get("differences") == []
        and res.get("emptyTracks") == {"a": [], "b": []}
        and _result_of(backward).get("equal") is True,
        error=forward.get("error"),
        differences=res.get("differences"),
        warnings=res.get("warnings"),
    )
    rec(
        ctx,
        "q1b_a_comparison_writes_nothing",
        await state_snapshot(ctx) == snap and await tx_struct(ctx, original) == before,
    )


async def deleted_segment(session: Any, ctx: Ctx, untouched: str, edited: str) -> None:
    delete = await run_call(
        session,
        ctx,
        "d_delete",
        {"sequence": sel(edited), "segments": [48], "track": 0, "expect_source_names": ["clip2"]},
    )
    env = await run_call(session, ctx, "q_compare", compare_args(untouched, edited))
    res = _result_of(env)
    differences = res.get("differences") or []
    wheres = {d.get("where") for d in differences}
    summary = res.get("summary") or []
    track_1 = {str(w) for w in wheres if str(w).startswith("video track 1")}
    rec(
        ctx,
        "q2_a_segment_deleted_from_a_copy_shows_where_it_was_and_the_other_track_differs_only_at_its_end",
        delete.get("ok") is True
        and env.get("ok") is True
        and res.get("equal") is False
        and (res.get("differenceCount") or 0) >= 1
        and "video track 0 segment 2" in wheres
        and track_1 <= {"video track 1", "video track 1 segment 1"},
        error=env.get("error") or delete.get("error"),
        wheres=sorted(str(w) for w in wheres),
        differences=differences[:12],
        summary=summary,
    )


def expected_audio_cut_differences(
    audio_before: list[dict[str, Any]], end: int
) -> set[tuple[str, str, int, int]]:
    """{(where, field, a, b)} that timeline.compare should list for the audio of a sequence and
    its self shortened to `end`: each segment that crosses the end (record_in < end <= record_out)
    ends at end - 1 (recordOut) with the same source_in, so its sourceOut moves by the same
    amount. A segment that starts at or after `end` is gone, which is not covered here."""
    expected: set[tuple[str, str, int, int]] = set()
    for row in audio_before:
        if row["rin"] < end <= row["rout"]:
            where = f"audio track {row['track']} channel {row['channel']} segment {row['index']}"
            expected.add((where, "recordOut", row["rout"], end - 1))
            expected.add((where, "sourceOut", row["sout"], row["sin"] + (end - 1 - row["rin"])))
    return expected


def first_affected(rows: list[list[Any]], end: int) -> int | None:
    """The index of the first row that ends at or after `end` (it is cut, or removed)."""
    return next((i for i, row in enumerate(rows) if row[2] >= end), None)


async def shortened_pair(session: Any, ctx: Ctx) -> None:
    pair = ctx.ids.get("shortenPair")
    if not (pair and pair.get("before") and pair.get("after")):
        rec(
            ctx,
            Q3_NAME,
            False,
            why="stage s made no pair of a sequence and its shortened self (see s1b and s2)",
        )
        return
    end = pair["end"]
    env = await run_call(
        session, ctx, "q_compare", compare_args(pair["before"]["wt"], pair["after"]["wt"])
    )
    res = _result_of(env)
    starts = [first_affected(rows, end) for rows in pair["rowsBefore"]]
    early: list[Any] = []
    other: list[Any] = []
    unjudgeable: list[Any] = []
    for diff in res.get("differences") or []:
        where = str(diff.get("where"))
        matched = WHERE_PATTERN.match(where)
        if matched is None:
            if where == "sequence":
                (early if diff.get("field") != "duration" else other).append(diff)
            elif where.startswith("audio"):
                other.append(diff)
            else:
                unjudgeable.append(diff)
            continue
        track = int(matched.group(1))
        segment = matched.group(2)
        start = starts[track] if track < len(starts) else None
        if segment is None:
            if diff.get("field") != "segmentCount":
                early.append(diff)
        elif start is None or int(segment) < start:
            early.append(diff)
    audio = [d for d in other if str(d.get("where")).startswith("audio")]
    audio_before = pair.get("audioBefore") or []
    found = {(str(d.get("where")), str(d.get("field")), d.get("a"), d.get("b")) for d in audio}
    rec(
        ctx,
        Q3_NAME,
        env.get("ok") is True
        and res.get("equal") is False
        and (res.get("differenceCount") or 0) >= 1
        and not early
        and not unjudgeable,
        error=env.get("error"),
        end=end,
        firstAffectedIndex=starts,
        early=early[:12],
        unjudgeable=unjudgeable[:12],
        differenceCount=res.get("differenceCount"),
    )
    # JUDGED on a measurement (Flame 2025.2.4; see the module docstring).
    expected = expected_audio_cut_differences(audio_before, end)
    rec(
        ctx,
        Q3B_NAME,
        env.get("ok") is True
        and bool(audio_before)
        and bool(expected)
        and not any(row["rin"] >= end for row in audio_before)
        and len(audio) == len(found)
        and found == expected,
        audioDifferences=audio[:20],
        expectedDifferences=sorted(expected),
        otherDifferences=[d for d in other if d not in audio][:20],
        warnings=res.get("warnings"),
    )


async def extra_track(session: Any, ctx: Ctx, untouched: str, extended: str) -> None:
    made = await run_call(session, ctx, "v_create_track", {"sequence": sel(extended)})
    env = await run_call(session, ctx, "q_compare", compare_args(untouched, extended))
    res = _result_of(env)
    differences = res.get("differences") or []
    fields = {(d.get("where"), d.get("field")) for d in differences}
    rec(
        ctx,
        "q4_a_track_one_side_lacks_is_a_difference_and_an_empty_track_is_listed",
        made.get("ok") is True
        and env.get("ok") is True
        and res.get("equal") is False
        and ("sequence", "videoTrackCount") in fields
        and any(f == "presence" and str(w).startswith("video track 2") for w, f in fields)
        and res.get("emptyTracks") == {"a": [], "b": [2]},
        error=env.get("error") or made.get("error"),
        emptyTracks=res.get("emptyTracks"),
        differences=differences[:12],
    )


# -- c: segment.connections ---------------------------------------------------------------------


async def stage_c(ctx: Ctx) -> None:
    case = await make_s(ctx, "CN")
    if case is None:
        return
    wiretap = case["wt"]
    async with server_session(ctx, "p36e-c") as session:
        for tag, track, source in (
            ("c1_a_segment_of_track_0", 0, "clip1"),
            ("c2_a_segment_of_track_1", 1, "p2_len72"),
        ):
            target = seg_at(sel(wiretap), track, 0, expect=source)
            env = await run_call(session, ctx, "c_connections", {"target": target})
            res = _result_of(env)
            listed = await tx_connected(ctx, wiretap, track, 0)
            rec(
                ctx,
                f"{tag}_with_no_connection_has_no_other_connected_segment",
                env.get("ok") is True
                and listed == 1
                and res.get("count") == 0
                and res.get("total") == 0
                and res.get("items") == []
                and res.get("targetListed") is True,
                error=env.get("error"),
                connectedSegmentsLength=listed,
                result={k: res.get(k) for k in ("count", "total", "items", "targetListed")},
                warnings=res.get("warnings"),
            )


# -- y: the read-only policy --------------------------------------------------------------------


async def stage_y(ctx: Ctx) -> None:
    wiretap = ctx.ids.get("compareA")
    if not wiretap:
        rec(ctx, "y0_a_sequence_to_aim_the_denied_calls_at_is_there", False)
        return
    target = sel(wiretap)
    calls = {
        "y_readonly_delete": {"sequence": target, "segments": [0], "track": 0},
        "y_readonly_move": {"sequence": target, "track": 1, "to": 0},
        "y_readonly_shorten": {"sequence": target, "duration": 10},
    }
    async with server_session(ctx, "p36e-y", readonly=True) as session:
        snap = await state_snapshot(ctx)
        before = await tx_full(ctx, wiretap)
        denied = {
            call_id: (await run_call(session, ctx, call_id, args)).get("errorCode")
            for call_id, args in calls.items()
        }
        rec(
            ctx,
            "y1_a_readonly_server_denies_delete_segments_move_track_and_shorten",
            set(denied.values()) == {"POLICY_DENIED"}
            and await state_snapshot(ctx) == snap
            and same_video(await tx_full(ctx, wiretap), before),
            denied=denied,
        )
        compared = await run_call(
            session, ctx, "y_readonly_compare", {"a": target, "b": target, "limit": 5}
        )
        connected = await run_call(
            session,
            ctx,
            "y_readonly_connections",
            {"target": seg_at(target, 0, 0)},
        )
        rec(
            ctx,
            "y2_the_two_reads_still_answer_on_a_readonly_server",
            compared.get("ok") is True
            and _result_of(compared).get("equal") is True
            and connected.get("ok") is True,
            compareError=err_text(compared),
            connectionsError=err_text(connected),
        )
