"""`segment.copy_to_media_panel` and `segment.match`: take one
segment out of a sequence as a new clip. The server side of the writes is `segment_extract_run`,
the Flame side `segment_extract_prelude`."""

from __future__ import annotations

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
from flame_mcp.ops._segment_edit import SEGMENT_REQUIRES, no_undo_steps, segment_target_param
from flame_mcp.registry import Registry
from flame_mcp.segment_extract_run import COPY_OP, MATCH_OP, run_extract
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_DESTINATION_KINDS: Final[tuple[SelectorKind, ...]] = ("wiretap", "path")

_SHAPES = (
    "The three shapes (for a segment with a Timewarp, used range 52 frames, "
    "head 10, tail 10, in a 72-frame media): "
    "segment.copy_to_media_panel = the used range with its soft handles and its effects "
    "(duration 52, start_frame 11, source range as in the sequence, head 10, tail 10); "
    "segment.match (default) = the whole source clip with an in / out mark on the used range, "
    "no effect (duration 52, start_frame 1, inMark 10 / outMark 62, head 0, tail 0); "
    "segment.match with preserve_handle = the used range with its handles, no effect "
    "(duration 52, start_frame 11, head 10, tail 10); with include_timeline_fx = a clip with "
    "the effects, in a shape that differs from the others (duration 72, tail 'infinite': "
    "read result.clip). A media with no handle gives a handle-0 clip whichever is "
    "used, with no error."
)
_COMMON = (
    "`destination` is a wiretap / path selector of a reel (a Desktop reel "
    "or one in a library; a reel of a Batch group is refused: INVALID_ARGS, use batch.add_clips). "
    "Refused before anything is written and nothing is journaled: a Gap "
    "(PRECONDITION_UNMET), a destination that is not a reel / folder / library (SELECTOR_TYPE), "
    "a segment selector that does not resolve. "
    "The new clip is found by a diff of the destination's entries before and after, never by the "
    "answer (a PyClip or a list of them): a call after which the destination did not grow is "
    "SILENT_NOOP; several new entries that cannot be told apart end in FLAME_API_ERROR with "
    "result.candidates and nothing recorded as made. "
    "result: mode, source {segment (type, sourceName, record / source in-out, head, tail), "
    "sequence {type, name, uid, wiretapId, selector}, location, effects}, destination, created "
    "{type, name, uid, wiretapId, selector} (use its selector), clip {duration, startFrame, "
    "inMark, outMark (null when the clip has none), segmentCount, segments (up to 8, of the first "
    "version: track, type, sourceName, recordIn / recordOut / sourceIn / sourceOut (recordOut / "
    "sourceOut are the last frame, inclusive), head, tail, "
    "effects), effects}, state {name, duration, fingerprint}, returned {shape, count, types}, "
    "warnings. "
    "JOURNAL as desktop.duplicate (intent with the destination's entries' uids -> compare-and-"
    "swap -> outcome with the clip). Envelope journal = {group, id, revert}. Undo: "
    "journal.revert(group) deletes the new clip by uid (flame.delete) only while its name, "
    "duration and content (segments and effects) are still what they were right after the call; "
    "otherwise CONFLICT and nothing is deleted; a clip that is gone already is ok "
    "(result.alreadyGone). The sequence is never touched. Native Ctrl+Z is not used (undoSteps "
    "0). Not allowed in plan.run."
)

_COPY_DESCRIPTION = (
    "Take one segment out of its sequence as a new clip in a reel (PySegment.copy_to_media_panel): "
    "the used range with its soft handles and its TimelineFX -- the way to hand a shot on with its "
    "handles (for example, take it out, then trim the clip's inner segment to the handle length "
    "with segment.trim). " + _SHAPES + " " + _COMMON
)

_MATCH_DESCRIPTION = (
    "Match the source of one segment out as a new clip in a reel (PySegment.match): by default the "
    "whole source clip, with an in / out mark on the range the segment uses (result.clip.inMark / "
    "outMark) and no effect. The segment may be in a library (it is only read). "
    "preserve_handle=true gives the used range with its handles instead "
    "(no effect); include_timeline_fx=true keeps the segment's effects (a different shape, see "
    "below). use_sequence_info and include_nested_content are not offered. "
    + _SHAPES
    + " "
    + _COMMON
)


def _extract_to_python(mode: str) -> Any:
    def to_python(args: Mapping[str, Any]) -> str:
        phase = args.get(PHASE_ARG, PHASE_WRITE)
        if phase == PHASE_ALIVE:
            return alive_source(args)
        if phase == PHASE_REVERT:
            return build_source(
                f"_result = _ex_revert({dict(args[CREATED_ARG])!r})",
                raise_on_error_src(),
                "_affected = 1 if _result['deleted'] else 0",
            )
        dest = selectors.resolve_code(args["destination"], "dest")
        if phase == PHASE_PROBE:
            return build_source(dest, probe_source(args, "dest"))
        resolve = selectors.resolve_code(args["target"], "target")
        if phase == PHASE_READ:
            return build_source(
                resolve,
                dest,
                "_result = _ex_snapshot(target, target__parent, dest)",
                "_affected = 0",
            )
        preserve = bool(args.get("preserve_handle", False))
        include_fx = bool(args.get("include_timeline_fx", False))
        return build_source(
            resolve,
            dest,
            f"_result = _ex_run(target, target__parent, dest, {mode!r}, {preserve!r}, "
            f"{include_fx!r}, {args.get(EXPECTED_ARG)!r})",
            raise_on_error_src(),
            "_affected = 1",
        )

    return to_python


def _precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    target = args.get("target")
    if not isinstance(target, Mapping) or target.get("kind") != "segment":
        return Refusal("INVALID_ARGS", "target must be a 'segment' selector")
    problems = selectors.validate_selector(args.get("destination"), _DESTINATION_KINDS)
    if problems:
        return Refusal(
            "INVALID_ARGS",
            "destination must be a wiretap / path selector of a reel, folder or library: "
            + "; ".join(problems),
        )
    return None


def _destination_param() -> OperationParam:
    return OperationParam(
        name="destination",
        type="selector",
        description=(
            "A wiretap / path selector of a reel, a library reel, a folder or a library. A reel "
            "of a Batch group is refused."
        ),
        required=True,
    )


_SAMPLE_ARGS: Final[Mapping[str, Any]] = {
    "target": {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "s"},
        "version": 0,
        "track": 0,
        "index": 0,
    },
    "destination": {"kind": "wiretap", "id": "d"},
}

_REQUIRES = (
    "PyClip.parent",
    "PyClip.duration",
    "PyClip.start_frame",
    "PyClip.in_mark",
    "PyClip.out_mark",
    "PyClip.versions",
    "PyReel.children",
    "PyLibrary.children",
    "PyFolder.children",
    "PyReel.type",
    "flame.delete",
    "flame.find_by_uid",
    *SEGMENT_REQUIRES,
)


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=COPY_OP,
            category="segment",
            description=_COPY_DESCRIPTION,
            effect="create",
            undo="value",
            params=(segment_target_param(), _destination_param()),
            accepts=("segment", *_DESTINATION_KINDS),
            to_python=_extract_to_python("copy"),
            undo_steps=no_undo_steps,
            precheck=_precheck,
            wrap=run_extract,
            sample_args=_SAMPLE_ARGS,
            requires=("PySegment.copy_to_media_panel", *_REQUIRES),
        )
    )
    registry.register(
        OperationSpec(
            name=MATCH_OP,
            category="segment",
            description=_MATCH_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                segment_target_param(),
                _destination_param(),
                OperationParam(
                    name="preserve_handle",
                    type="boolean",
                    description=(
                        "Keep the used range with its handles instead of the whole source clip "
                        "(no effect either). Default false."
                    ),
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="include_timeline_fx",
                    type="boolean",
                    description="Keep the segment's TimelineFX on the new clip. Default false.",
                    required=False,
                    default=False,
                ),
            ),
            accepts=("segment", *_DESTINATION_KINDS),
            to_python=_extract_to_python("match"),
            undo_steps=no_undo_steps,
            precheck=_precheck,
            wrap=run_extract,
            sample_args={**_SAMPLE_ARGS, "preserve_handle": False, "include_timeline_fx": False},
            requires=("PySegment.match", *_REQUIRES),
        )
    )
