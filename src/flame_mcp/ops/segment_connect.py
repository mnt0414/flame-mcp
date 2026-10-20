"""`segment.connections` (a read) and `segment.create_connection`, `segment.remove_connection`,
`segment.sync_connections` (writes): the segments connected to a segment
(`PySegment.connected_segments()` and the three methods beside it). The writes are not journaled
and not checkpointed: the Timeline checkpoint covers only the sequence of the target, never the
connected segments in other sequences, so each says so. The Flame side is
`segment_connection_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._common import build_source, offset_param
from flame_mcp.ops._segment_edit import edit_source, no_undo_steps, seg_args, segment_target_param
from flame_mcp.registry import Registry
from flame_mcp.types import OperationParam, OperationSpec

CONNECTIONS_OP: Final[str] = "segment.connections"
CREATE_OP: Final[str] = "segment.create_connection"
REMOVE_OP: Final[str] = "segment.remove_connection"
SYNC_OP: Final[str] = "segment.sync_connections"
WRITE_OPS: Final[tuple[str, ...]] = (CREATE_OP, REMOVE_OP, SYNC_OP)

_CONNECTIONS_DESCRIPTION = (
    "List the segments connected to one segment (PySegment.connected_segments()), one page at a "
    "time. The call lists the segment itself too (measured on Flame 2025.2.4: a connected pair "
    "answers 2 for each, a segment with no connection answers [self]), so the target is left "
    "out: `count` (= `total`) is the number of OTHER connected segments, 0 for a segment with "
    "no connection. The target is found by its position in its sequence (isTarget); "
    "`targetListed` says whether it was found, and when it was not (its position is not "
    "unique) `count` may include it and result.warnings says so. "
    "Each item: sequence {type, name} (the sequence or clip it is in), uid and wiretapId of that "
    "container, version, track, index, name, sourceName, recordIn / recordOut (PyTime as "
    "{timecode, frame}), `selector` (a 'segment' selector that addresses it; null when its "
    "position is not unique), `ambiguous` (with up to 5 `candidates`), `isTarget` and, when part "
    "of it could not be resolved, `error` (what is known is still given). A segment has no id, "
    "so its position is found by the container's segments that have the same record_in, "
    "record_out, source_name and source_in, as segment.shared_sources does. result.target is the "
    "target segment as it is now. Replacing the media of a connected segment also replaces the "
    "media of the segments connected to it, in other sequences (measured on Flame 2025.2.4): "
    "this is how to see which ones, before segment.replace. "
)

_WRITE_NOTE = (
    "The connections of a segment may reach segments in other sequences: neither a Timeline "
    "checkpoint of this segment's sequence nor journal.revert covers those, so this runs with "
    "no checkpoint and no journal record (undo unsupported, nothing to revert) and cannot be a "
    "plan step. result.notices says so. result.inverse is a PRESUMED inverse call, "
    "given only when the connected count moved the expected way. "
    "result: action, before / after (the segment: type, sourceName, recordIn / recordOut / "
    "sourceIn / sourceOut / head / tail), connected {before, after} (the number of other "
    "segments connected_segments() answers), returned (as Flame answered), position "
    "{version, track, "
    "index, recordIn, selector, selectorByIndex}, inverse, notices, warnings. "
    "connected_segments() lists the segment itself (measured), so the counts are of the OTHER "
    "segments, as in segment.connections. A count "
    "that did not move the expected way is a warning in result.warnings, never an error "
    "(there is no journal to fall back on). "
    "Refused before anything is changed (PRECONDITION_UNMET): a Gap, a sequence inside a library "
    "or a Batch group; FLAME_API_ERROR when connected_segments() raises. "
)

_CREATE_DESCRIPTION = (
    "Create a connection for one segment (PySegment.create_connection). "
    + _WRITE_NOTE
    + "Expected: more other segments are connected after than before; "
    "result.inverse is segment.remove_connection on the same segment."
)

_REMOVE_DESCRIPTION = (
    "Remove the connection of one segment (PySegment.remove_connection). "
    + _WRITE_NOTE
    + "Expected: fewer other segments are connected after than before; "
    "result.inverse is segment.create_connection on the same segment."
)

_SYNC_DESCRIPTION = (
    "Call PySegment.sync_connected_segments() on one segment. It reports the connected count "
    "before and after and what the call returned, and makes no judgement of its own. "
    + _WRITE_NOTE
    + "There is no inverse op: result.inverse is null."
)


def _connections_to_python(args: Mapping[str, Any]) -> str:
    sel = args["target"]
    return build_source(
        selectors.resolve_code(sel, "target"),
        f"_result = _sc_connections(target, target__parent, {sel['version']!r}, "
        f"{sel['track']!r}, {args['limit']!r}, {args['offset']!r})",
    )


def _write_to_python(action: str) -> Any:
    def to_python(args: Mapping[str, Any]) -> str:
        sel = args["target"]
        return edit_source(
            args,
            f"_sc_write_check(target, target__parent, {action!r}, "
            f"{sel.get('version', 0)!r}, {sel.get('track', 0)!r})",
            f"_sc_write({seg_args(args)}, {action!r})",
        )

    return to_python


_WRITE_REQUIRES = (
    "PySegment.connected_segments",
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
)


def _register_write(
    registry: Registry, name: str, description: str, call: str, action: str
) -> None:
    registry.register(
        OperationSpec(
            name=name,
            category="segment",
            description=description,
            effect="destructive",
            undo="unsupported",
            params=(segment_target_param(),),
            accepts=("segment",),
            to_python=_write_to_python(action),
            undo_steps=no_undo_steps,
            requires=(call, *_WRITE_REQUIRES),
        )
    )


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=CONNECTIONS_OP,
            category="segment",
            description=_CONNECTIONS_DESCRIPTION,
            effect="read",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A 'segment' selector identifying one segment.",
                    required=True,
                ),
                OperationParam(
                    name="limit",
                    type="number",
                    description="Max items per call.",
                    required=False,
                ),
                offset_param(),
            ),
            accepts=("segment",),
            default_limit=20,
            max_limit=100,
            to_python=_connections_to_python,
            requires=(
                *_WRITE_REQUIRES,
                "PySegment.name",
                "PyTrack.parent",
                "PyVersion.parent",
                "PyClip.get_wiretap_node_id",
                "PySequence.get_wiretap_node_id",
            ),
        )
    )
    _register_write(
        registry, CREATE_OP, _CREATE_DESCRIPTION, "PySegment.create_connection", "create"
    )
    _register_write(
        registry, REMOVE_OP, _REMOVE_DESCRIPTION, "PySegment.remove_connection", "remove"
    )
    _register_write(
        registry, SYNC_OP, _SYNC_DESCRIPTION, "PySegment.sync_connected_segments", "sync"
    )
