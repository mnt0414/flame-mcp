"""`segment.trim`: a write that runs behind a Timeline checkpoint
(`CheckpointSpec(target_kind="timeline")`: the sequence the segment belongs to is copied into a
stash reel first, `journal.revert` puts it back). The Flame side is
`timeline_trim_prelude._tl_trim`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import execution, selectors
from flame_mcp.errors import as_error_code
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.registry import Registry
from flame_mcp.timeline_restore import NOT_RESTORED
from flame_mcp.types import CheckpointSpec, OperationParam, OperationSpec, Refusal

OP_NAME: Final[str] = "segment.trim"
PHASE_ARG: Final[str] = "_phase"
PHASE_CHECK: Final[str] = "check"
EDGES: Final[tuple[str, ...]] = ("head", "tail")

_DESCRIPTION = (
    "Trim one segment's head or tail (PySegment.trim_head / trim_tail), behind an automatic "
    "checkpoint of its sequence: the sequence is copied into the stash reel first "
    "(timeline.checkpoint), the call is journaled (intent with the fingerprint before, outcome "
    "with the fingerprint after) and journal.revert(group) puts the sequence back. "
    "The segment can also be the one inside a reel clip on the Desktop (parent = the clip's "
    "wiretap, version 0, track 0, index 0): that trims the clip itself, with no sequence "
    "involved, and a copy of the clip is what is stashed. Measured on Flame 2025.2.4: a "
    "71-frame clip trimmed 10 at the head and 10 at the tail is a 51-frame clip with head 10 / "
    "tail 10 left, and it still shares its source with the sequences' segments "
    "(segment.shared_sources). "
    "Sign: offset > 0 shortens the segment (takes frames off that edge), offset < 0 extends it "
    "(uses the handle); an extension can go as far as the edge's handle (`head` / `tail` of "
    "timeline.list_segments, an integer or 'infinite'). "
    "Ripple: without ripple a head trim moves record_in by offset and leaves a Gap before the "
    "segment (its record_in is where it starts now); a tail trim leaves a Gap after it. With "
    "ripple=true the same track is closed up, and the other tracks do not follow: "
    "they are out of sync with it afterwards (result.trackEnds shows where every track of the "
    "version ends). "
    "Refused before anything is copied or journaled (PRECONDITION_UNMET): a Gap, an extension "
    "beyond the handle (result.handle is what is available), a trim of the segment's whole "
    "length or more, a sequence inside a library or a Batch group; INVALID_ARGS: edge other than "
    "'head' / 'tail', offset 0. "
    "Judged by reading the segment back, never by trim's return value (False does not mean "
    "failure, result.returned is only reported): nothing in the track changed = SILENT_NOOP; a "
    "change that is not the one asked for (see result.deltas / expectedDeltas) = FLAME_API_ERROR "
    "with before and after (the sequence is still revertible). "
    "A segment that has a Timewarp effect and whose source length differs from its record "
    "length: sourceIn / sourceOut are not judged (result.timewarp true, sourceJudged false, "
    "sourceNotJudgedBecause says why, expectedDeltas.sourceIn / sourceOut null, deltas as read "
    "back); the record side, head and tail are judged as above. Any other segment: "
    "sourceJudged true. "
    "result: before / after (the segment's type, sourceName, recordIn / recordOut / sourceIn / "
    "sourceOut / head / tail, PyTime as {timecode, frame}; recordOut / sourceOut are the last "
    "frame (inclusive), not the one after it), deltas (after - before, frames), "
    "position {version, track, index, recordIn, selector (a 'segment' selector by record_in -- "
    "the one to use for the next edit -- and selectorByIndex)}, track / trackBefore "
    "(segmentCount, startFrame, endFrame = the last frame (inclusive) of the track: 63 for 64 "
    "frames starting at 0, trailingGap), trackEnds, returned. "
    "Envelope journal = {group, id, checkpoint, checkpointPath (the stash: <library>/<reel>), "
    "stash, revert, changed}. Undo: journal.revert(group) restores the checkpoint, only while "
    "the sequence still has the fingerprint the call left (else CONFLICT and nothing is touched; "
    "force=true overrides, journaled). Timeline restore: the edited sequence is moved into the "
    "stash reel (result.preCheckpoint: the revert can itself be undone) and the stashed copy is "
    "copied back. Not put back: " + "; ".join(NOT_RESTORED) + ". "
    "Native Ctrl+Z is not used (undoSteps 0). Segment edits run on Flame's main thread only. "
    "In plan.run: a plan that holds this step is one checkpointed write on this sequence (one "
    "stashed copy, journal.revert(group) is one restore; the step's own checkpoint is not "
    "taken); see plan.run."
)


def _trim_to_python(args: Mapping[str, Any]) -> str:
    sel = args["target"]
    edge = args["edge"]
    offset = int(args["offset"])
    resolve = selectors.resolve_code(sel, "target")
    if args.get(PHASE_ARG) == PHASE_CHECK:
        return build_source(
            resolve,
            f"_result = _tl_trim_check(target, target__parent, {edge!r}, {offset!r})",
            raise_on_error_src(),
            "_affected = 0",
        )
    ripple = bool(args.get("ripple", False))
    return build_source(
        resolve,
        f"_result = _tl_trim(target, target__parent, {sel['version']!r}, {sel['track']!r}, "
        f"{edge!r}, {offset!r}, {ripple!r})",
        raise_on_error_src(),
        "_affected = 1",
    )


def _trim_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    edge = args.get("edge")
    if edge not in EDGES:
        return Refusal("INVALID_ARGS", f"edge must be one of {list(EDGES)}, got {edge!r}")
    if args.get("offset") == 0:
        return Refusal(
            "INVALID_ARGS",
            "offset must not be 0 (positive shortens the segment, negative extends it)",
        )
    return None


def _trim_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    """Everything that can be refused without writing, asked of Flame before a checkpoint is taken:
    a refused call leaves no stash and no journal group behind. The write checks again."""
    op = rt.registry.get(OP_NAME)
    env = execution.run_op(op, {**args, PHASE_ARG: PHASE_CHECK}, run_dir=rt.run_dir)
    if env.get("ok") is True:
        return None
    raw = env.get("result")
    value: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return Refusal(
        as_error_code(str(env.get("errorCode"))),
        f"nothing was run: {env.get('error')}",
        hint=env.get("hint"),
        result={"ran": False, "before": value.get("before"), "handle": value.get("handle")},
    )


def _group_selector(args: Mapping[str, Any]) -> Mapping[str, Any]:
    parent: Mapping[str, Any] = args["target"]["parent"]
    return parent


def _rebind(args: Mapping[str, Any], stable: dict[str, Any]) -> dict[str, Any]:
    return {**args, "target": {**args["target"], "parent": stable}}


def _label(args: Mapping[str, Any]) -> str:
    return f"before segment.trim ({args['edge']} {args['offset']})"


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=OP_NAME,
            category="segment",
            description=_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description=(
                        "A 'segment' selector identifying the segment, by index or by record_in."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="edge",
                    type="string",
                    description="'head' or 'tail': which edge of the segment to trim.",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="offset",
                    type="number",
                    description=(
                        "Frames, a non-zero integer: positive shortens the segment, negative "
                        "extends it (as far as the handle goes)."
                    ),
                    required=True,
                    integer=True,
                ),
                OperationParam(
                    name="ripple",
                    type="boolean",
                    description=(
                        "Close up the rest of this track. The other tracks do not "
                        "follow. Default false."
                    ),
                    required=False,
                    default=False,
                ),
            ),
            accepts=("segment",),
            to_python=_trim_to_python,
            undo_steps=_no_undo_steps,
            precheck=_trim_precheck,
            checkpoint=CheckpointSpec(
                target_kind="timeline",
                label=_label,
                check=_trim_check,
                group_selector=_group_selector,
                rebind=_rebind,
            ),
            requires=(
                "PySegment.trim_head",
                "PySegment.trim_tail",
                "PySegment.record_in",
                "PySegment.record_out",
                "PySegment.record_duration",
                "PySegment.source_in",
                "PySegment.source_out",
                "PySegment.head",
                "PySegment.tail",
                "PySegment.type",
                "PySegment.source_name",
                "PySegment.parent",
                "PyTrack.segments",
                "PyVersion.tracks",
            ),
        )
    )
