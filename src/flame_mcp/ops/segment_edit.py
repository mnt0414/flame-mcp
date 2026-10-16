"""`segment.slip` and `segment.replace`: the two edits that move what a segment shows
without moving where it sits. Both run behind a Timeline checkpoint (`_segment_edit`); the Flame
side is `timeline_edit_prelude` (`_te_slip`, `_te_replace`). Measured on Flame 2025.2.4: `slip(n)`
moves the source position n frames earlier and `smart_replace` keeps the offset from the start of
the clip, not the source TC. Derived from that (one case): the TC alignment after a replace is
`slip(source TC after the replace - wanted source TC)`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._segment_edit import (
    CHECKPOINTED_SEGMENT_NOTE,
    SEGMENT_REQUIRES,
    checkpoint_spec,
    edit_source,
    no_undo_steps,
    seg_args,
    segment_target_param,
)
from flame_mcp.registry import Registry
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

SLIP_OP: Final[str] = "segment.slip"
REPLACE_OP: Final[str] = "segment.replace"
MODES: Final[tuple[str, ...]] = ("media", "media_and_fx")
_SOURCE_KINDS: Final[tuple[SelectorKind, ...]] = ("wiretap", "path", "selection")

_SLIP_DESCRIPTION = (
    "Slip one segment's source under its record (PySegment.slip): the segment stays where it is "
    "on the timeline and shows other frames of its source. Sign: offset > 0 "
    "takes the source toward its head -- source_in (and source_out) go down by offset, head "
    "handle goes down and tail handle up by offset; offset < 0 the other way. record_in / "
    "record_out do not change. The return value False is not a failure (result.returned is only "
    "reported). Flame does not refuse a slip beyond the handle: it stops at the handle without a "
    "word (head for a positive offset, tail for a negative one), so a slip that would go past "
    "it is refused up front (PRECONDITION_UNMET, before anything is copied or journaled; "
    "result.available is the handle in that direction, an integer or 'infinite', and "
    "result.handle has both): also a Gap, a sequence inside a library or a Batch group. "
    "INVALID_ARGS: offset 0. "
    "Judged by reading the segment back, never by the return value: nothing in the track changed "
    "= SILENT_NOOP; a change that is not the offset asked for = FLAME_API_ERROR with before and "
    "after (result.moved is how far source_in really went; the sequence is still revertible). "
    "A segment that has a Timewarp effect and whose source length differs from its record "
    "length: sourceIn / sourceOut are not judged (result.timewarp true, sourceJudged false, "
    "sourceNotJudgedBecause says why, expectedDeltas.sourceIn / sourceOut null, deltas as read "
    "back); the record side, head and tail are judged as above. Any other segment: "
    "sourceJudged true. "
    "result: before / after (type, sourceName, recordIn / recordOut / sourceIn / sourceOut / "
    "head / tail / recordDuration, PyTime as {timecode, frame}; recordOut / sourceOut are the "
    "last frame (inclusive)), requested, moved, deltas "
    "(after - before, frames) / expectedDeltas, position {version, track, index, recordIn, "
    "selector (a 'segment' selector by record_in), selectorByIndex}, track (segmentCount, "
    "startFrame, endFrame = the last frame (inclusive) of the track, trailingGap), returned. "
    "To line a replaced segment up with the "
    "source timecode you want, pass the difference between segment.replace's result.after "
    "sourceIn timecode and the one you want (slip(n) takes source_in down by n). "
    + CHECKPOINTED_SEGMENT_NOTE
)

_REPLACE_DESCRIPTION = (
    "Replace the media of one segment with another clip, keeping the segment's place on the "
    "timeline. mode 'media' (default) = PySegment.smart_replace_media (the segment's TimelineFX "
    "stay); 'media_and_fx' = PySegment.smart_replace (the segment's TimelineFX are dropped "
    "when the clip has none of its own: result.effects shows them before and after; a Resize "
    "that Flame added by itself because of a bit depth difference stays, measured on Flame "
    "2026.2.3). `source` "
    "is the clip to put in (a wiretap / path / selection selector of a PyClip). "
    "It keeps the offset from the start of the clip; it does not match the source timecode: "
    "the same range of frames counted from the first frame of the new clip "
    "is used, whatever timecode that clip starts at. Record and head are unchanged; tail follows "
    "the new clip's length; source_name becomes the new clip's name. To line up a source "
    "timecode, compare result.after.sourceIn (timecode and frame) with the one you want and pass "
    "the difference to segment.slip (slip(n) takes source_in down by n). It reaches only as "
    "far as the handle goes. "
    "A clip too short for the range the segment uses changes nothing in Flame and raises "
    "nothing: that is reported as SILENT_NOOP (source_name, the source range, head and tail are "
    "all the same as before -- the same happens when the clip is the one the segment already "
    "has). Refused up front (PRECONDITION_UNMET, before anything is copied or journaled): a Gap, "
    "a source that is not a PyClip (a sequence, a reel ...), a sequence inside a library or a "
    "Batch group; INVALID_ARGS: a mode that is neither of the two, a source that is a segment "
    "selector. "
    "result: mode, source {type, name, uid, wiretapId, selector}, before / after (type, "
    "sourceName, recordIn / recordOut / sourceIn / sourceOut -- frame AND timecode; recordOut / "
    "sourceOut are the last frame (inclusive) -- head, tail, "
    "recordDuration), changed (which of sourceName / sourceIn / sourceOut / head / tail "
    "changed), effects {before, after} (types), position {version, track, index, recordIn, "
    "selector (by record_in, with expect_source_name = the new source name), selectorByIndex}, "
    "warnings, returned. " + CHECKPOINTED_SEGMENT_NOTE
)


def _slip_to_python(args: Mapping[str, Any]) -> str:
    offset = int(args["offset"])
    return edit_source(
        args,
        f"_te_slip_check(target, target__parent, {offset!r})",
        f"_te_slip({seg_args(args)}, {offset!r})",
    )


def _slip_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    if args.get("offset") == 0:
        return Refusal(
            "INVALID_ARGS",
            "offset must not be 0 (positive takes the source toward its head, negative toward "
            "its tail)",
        )
    return None


def _replace_to_python(args: Mapping[str, Any]) -> str:
    mode = args.get("mode", "media")
    return edit_source(
        args,
        f"_te_replace_check(target, target__parent, source, {mode!r})",
        f"_te_replace({seg_args(args)}, source, {mode!r})",
        selectors.resolve_code(args["source"], "source"),
    )


def _replace_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    mode = args.get("mode", "media")
    if mode not in MODES:
        return Refusal("INVALID_ARGS", f"mode must be one of {list(MODES)}, got {mode!r}")
    target = args.get("target")
    if not isinstance(target, Mapping) or target.get("kind") != "segment":
        return Refusal("INVALID_ARGS", "target must be a 'segment' selector")
    problems = selectors.validate_selector(args.get("source"), _SOURCE_KINDS)
    if problems:
        return Refusal(
            "INVALID_ARGS",
            "source must be a wiretap / path / selection selector of a clip: "
            + "; ".join(problems),
        )
    return None


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=SLIP_OP,
            category="segment",
            description=_SLIP_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(
                segment_target_param(),
                OperationParam(
                    name="offset",
                    type="number",
                    description=(
                        "Frames, a non-zero integer: positive takes the source toward its head "
                        "(source_in goes DOWN by offset), negative toward its tail. As far as "
                        "the handle goes."
                    ),
                    required=True,
                    integer=True,
                ),
            ),
            accepts=("segment",),
            to_python=_slip_to_python,
            undo_steps=no_undo_steps,
            precheck=_slip_precheck,
            checkpoint=checkpoint_spec(
                SLIP_OP, lambda args: f"before segment.slip ({args['offset']})"
            ),
            requires=("PySegment.slip", *SEGMENT_REQUIRES),
        )
    )
    registry.register(
        OperationSpec(
            name=REPLACE_OP,
            category="segment",
            description=_REPLACE_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(
                segment_target_param(),
                OperationParam(
                    name="source",
                    type="selector",
                    description=(
                        "A wiretap / path / selection selector of the PyClip to put in (it may "
                        "be in a reel or a library)."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="mode",
                    type="string",
                    description=(
                        "'media' (default): smart_replace_media, the segment's TimelineFX stay. "
                        "'media_and_fx': smart_replace, the TimelineFX do not stay when the clip "
                        "has none of its own."
                    ),
                    required=False,
                    default="media",
                    non_empty=True,
                ),
            ),
            accepts=("segment", *_SOURCE_KINDS),
            to_python=_replace_to_python,
            undo_steps=no_undo_steps,
            precheck=_replace_precheck,
            sample_args={
                "target": {
                    "kind": "segment",
                    "parent": {"kind": "wiretap", "id": "s"},
                    "version": 0,
                    "track": 0,
                    "index": 0,
                },
                "source": {"kind": "wiretap", "id": "c"},
                "mode": "media",
            },
            checkpoint=checkpoint_spec(
                REPLACE_OP, lambda args: f"before segment.replace ({args.get('mode', 'media')})"
            ),
            requires=(
                "PySegment.smart_replace_media",
                "PySegment.smart_replace",
                "PySegment.effects",
                "PyTimelineFX.type",
                *SEGMENT_REQUIRES,
            ),
        )
    )
