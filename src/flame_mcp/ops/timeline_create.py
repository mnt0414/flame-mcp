"""`timeline.create_sequence`: make a new sequence in a reel of the Desktop. The server side
of the write is `timeline_create_run`, the Flame side `timeline_create_prelude`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.desktop_run import (
    CREATED_ARG,
    EXPECTED_ARG,
    PHASE_ALIVE,
    PHASE_ARG,
    PHASE_PROBE,
    PHASE_READ,
    PHASE_REVERT,
    PHASE_WRITE,
)
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._media_phases import alive_source, probe_source
from flame_mcp.registry import Registry
from flame_mcp.timeline_create_run import CREATE_SEQUENCE_OP, run_create_sequence
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

FRAME_RATES: Final[tuple[str, ...]] = (
    "23.976 fps",
    "24 fps",
    "25 fps",
    "29.97 fps DF",
    "29.97 fps NDF",
    "30 fps",
    "50 fps",
    "59.94 fps DF",
    "59.94 fps NDF",
    "60 fps",
)
BIT_DEPTHS: Final[tuple[int, ...]] = (8, 10, 12, 16, 32)
MIN_VIDEO_TRACKS: Final[int] = 1
MAX_VIDEO_TRACKS: Final[int] = 8
DEFAULT_START_TC: Final[str] = "00:00:00:00"
_TC_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(\d{2}):(\d{2}):(\d{2}):(\d{2})$")
_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")

_DESCRIPTION = (
    "Create a new sequence in a reel of the Desktop with the given name, frame rate, size, start "
    "timecode, number of video tracks and (optionally) bit depth. "
    "`bit_depth` is one of " + ", ".join(str(d) for d in BIT_DEPTHS) + " (an integer; anything "
    "else is INVALID_ARGS before Flame is called). Left out, the sequence gets the project's "
    "default, which differs from project to project: read the result "
    "(result.sequence.bitDepth) instead of assuming it. When a sequence's bit depth differs from "
    "the media put on it (a clip's bitDepth, desktop.info), Flame adds a TimelineFX Resize to "
    "that segment by itself. On Flame 2026.2.3 Flame refused to delete that Resize, and "
    "segment.delete_effect refuses it up front on every version, for safety: give the new "
    "sequence the bit depth of the media it will hold and none is added. "
    "The parent is a wiretap / path / selection selector of a reel of a reel group. Refused before "
    "anything is written and nothing is journaled: a reel inside a library "
    "(PRECONDITION_UNMET), a reel of a Batch group (INVALID_ARGS), something that is not a reel "
    "(SELECTOR_TYPE), a frame_rate that is not one of " + ", ".join(FRAME_RATES) + ", a start_tc "
    "that is not HH:MM:SS:FF for that frame rate, video_tracks outside "
    f"{MIN_VIDEO_TRACKS}-{MAX_VIDEO_TRACKS} (INVALID_ARGS). A name that is already in the reel "
    "is fine: two sequences can share it. "
    "The new sequence is found by comparing the reel's entries before and after the call. "
    "Flame shows the new sequence in the Timeline: result.shownBefore / shownAfter ({type, uid, "
    "name} or null) say what the Timeline showed before and after. "
    "result: parent, created {type, name, uid, wiretapId, selector} (use its selector), sequence "
    "{frameRate, width, height, bitDepth, startFrame, startTime, duration, videoTracks} as read "
    "back, state {name, duration, fingerprint}, returned, warnings (a value that reads back "
    "differently from what was asked). Fill it with timeline.overwrite / timeline.insert. "
    "Undo: journal.revert(group) deletes the new sequence, only while its name, duration and "
    "content are still what they were right after the call (else CONFLICT and nothing is "
    "deleted); a sequence that is gone already is ok (result.alreadyGone). When it was the one "
    "the Timeline showed, the sequence shown before the call is opened again (result.reshown). "
    "Envelope journal = {group, id, revert}. Not allowed in plan.run."
)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_WRITE)
    if phase == PHASE_ALIVE:
        return alive_source(args)
    if phase == PHASE_REVERT:
        return build_source(
            f"_result = _ts_revert({dict(args[CREATED_ARG])!r})",
            raise_on_error_src(),
            "_affected = 1 if _result['deleted'] else 0",
        )
    parent = selectors.resolve_code(args["parent"], "parent")
    if phase == PHASE_PROBE:
        return build_source(parent, probe_source(args, "parent"))
    if phase == PHASE_READ:
        return build_source(parent, "_result = _ts_snapshot(parent)", "_affected = 0")
    depth = args.get("bit_depth")
    call = (
        f"_result = _ts_create(parent, {args['name']!r}, {int(args.get('video_tracks', 1))!r}, "
        f"{args['frame_rate']!r}, {int(args['width'])!r}, {int(args['height'])!r}, "
        f"{args.get('start_tc')!r}, {args.get(EXPECTED_ARG)!r}, "
        f"{None if depth is None else int(depth)!r})"
    )
    return build_source(parent, call, raise_on_error_src(), "_affected = 1")


def _precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    rate = args.get("frame_rate")
    if rate not in FRAME_RATES:
        return Refusal(
            "INVALID_ARGS", f"frame_rate must be one of {list(FRAME_RATES)}, got {rate!r}"
        )
    tracks = args.get("video_tracks", 1)
    if not isinstance(tracks, int) or not MIN_VIDEO_TRACKS <= tracks <= MAX_VIDEO_TRACKS:
        return Refusal(
            "INVALID_ARGS",
            f"video_tracks must be an integer from {MIN_VIDEO_TRACKS} to {MAX_VIDEO_TRACKS}, "
            f"got {tracks!r}",
        )
    tc = args.get("start_tc")
    if tc is not None:
        match = _TC_PATTERN.match(str(tc))
        if match is None:
            return Refusal(
                "INVALID_ARGS",
                f"start_tc must be HH:MM:SS:FF (for example {DEFAULT_START_TC}), got {tc!r}",
            )
        hours, minutes, seconds, frames = (int(g) for g in match.groups())
        nominal = round(float(str(rate).split()[0]))
        if hours > 23 or minutes > 59 or seconds > 59 or frames >= nominal:
            return Refusal(
                "INVALID_ARGS",
                f"start_tc {tc!r} is out of range for {rate} (hours 00-23, minutes and seconds "
                f"00-59, frames 00-{nominal - 1})",
            )
    return None


_SAMPLE_ARGS: Final[Mapping[str, Any]] = {
    "parent": {"kind": "wiretap", "id": "r"},
    "name": "sample",
    "frame_rate": "23.976 fps",
    "width": 1920,
    "height": 1080,
    "start_tc": "01:00:00:00",
    "video_tracks": 1,
    "bit_depth": 10,
}


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=CREATE_SEQUENCE_OP,
            category="timeline",
            description=_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="parent",
                    type="selector",
                    description="A wiretap / path / selection selector of a reel of the Desktop.",
                    required=True,
                ),
                OperationParam(
                    name="name",
                    type="string",
                    description="Name of the new sequence.",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="frame_rate",
                    type="string",
                    description="One of " + ", ".join(FRAME_RATES) + ".",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="width",
                    type="number",
                    description="Width in pixels (an integer, 1 or more).",
                    required=True,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="height",
                    type="number",
                    description="Height in pixels (an integer, 1 or more).",
                    required=True,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="start_tc",
                    type="string",
                    description=(
                        f"Start timecode HH:MM:SS:FF, for example 01:00:00:00. Default "
                        f"{DEFAULT_START_TC}."
                    ),
                    required=False,
                    non_empty=True,
                ),
                OperationParam(
                    name="video_tracks",
                    type="number",
                    description=(
                        f"Number of video tracks, {MIN_VIDEO_TRACKS} to {MAX_VIDEO_TRACKS} "
                        "(default 1)."
                    ),
                    required=False,
                    default=1,
                    integer=True,
                    minimum=MIN_VIDEO_TRACKS,
                ),
                OperationParam(
                    name="bit_depth",
                    type="number",
                    description=(
                        "Bit depth of the sequence, one of "
                        + ", ".join(str(d) for d in BIT_DEPTHS)
                        + ". Omit it for the project's default (it differs per project). If it "
                        "differs from the media put on the sequence, Flame adds a Resize to "
                        "those segments."
                    ),
                    required=False,
                    integer=True,
                    enum=BIT_DEPTHS,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_to_python,
            undo_steps=_no_undo_steps,
            precheck=_precheck,
            wrap=run_create_sequence,
            sample_args=_SAMPLE_ARGS,
            requires=(
                "PyReel.create_sequence",
                "PyReel.children",
                "PyReel.type",
                "PySequence.versions",
                "PySequence.start_time",
                "PySequence.start_frame",
                "PySequence.frame_rate",
                "PySequence.duration",
                "PyTimeline.clip",
                "PySequence.open",
                "flame.PyTime",
                "flame.delete",
                "flame.find_by_uid",
            ),
        )
    )
