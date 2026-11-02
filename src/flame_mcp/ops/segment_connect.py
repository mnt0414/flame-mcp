"""`segment.connections` (a read) and `segment.create_connection`, `segment.remove_connection`,
`segment.sync_connections` (writes): the segments connected to a segment
(`PySegment.connected_segments()` and the three methods beside it). The writes are not
checkpointed (the Timeline checkpoint covers only the sequence of the target, never the connected
segments in other sequences, so each says so) and have no revert, but each writes an intent to the
journal (`segment_remove_run`). The Flame side is `segment_connection_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._common import build_source, limit_param, offset_param, raise_on_error_src
from flame_mcp.ops._segment_edit import (
    SEGMENT_TARGET_DESCRIPTION,
    edit_source,
    no_undo_steps,
    seg_args,
    segment_target_param,
)
from flame_mcp.registry import Registry
from flame_mcp.segment_remove_run import (
    CONNECTIONS_OP,
    CREATE_OP,
    PHASE_ARG,
    PHASE_STEP,
    REMOVE_OP,
    SYNC_OP,
    run_connection_write,
)
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

MAX_REMOVE_TARGETS: Final[int] = 50

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

_STEP_JOURNAL = (
    "this runs with no checkpoint (undo unsupported, nothing to revert); each write is still an "
    "intent in the journal (the envelope's `journal`, with the segment's stable address: the "
    "wiretap id of its sequence and its record_in), and if one ends without an answer (TIMEOUT, "
    "BRIDGE_LOST) the server refuses every write until journal.check has read "
    "segment.connections again and decided landed / not_landed / diverged"
)
_WRITE_NOTE_TAIL = (
    ". It cannot be a "
    "plan step; result.notices says so. result.inverse is a PRESUMED inverse call, "
    "given only when the connected count moved the expected way. "
    "result: action, before / after (the segment: type, sourceName, recordIn / recordOut / "
    "sourceIn / sourceOut / head / tail), connected {before, after} (the number of other "
    "segments connected_segments() answers), returned (as Flame answered), position "
    "{version, track, "
    "index, recordIn, selector, selectorByIndex}, inverse, notices, warnings. "
    "connected_segments() lists the segment itself (measured), so the counts are of the OTHER "
    "segments, as in segment.connections. The result is judged by reading back, not by the "
    "answer of the call: a count that did not move the expected way is SILENT_NOOP (measured: "
    "a connection made afterwards did not connect the segments), one that moved the other "
    "way is FLAME_API_ERROR, and a read-back that raised is FLAME_API_ERROR with the intent left "
    "open (the server then refuses writes until journal.check). "
    "Refused before anything is changed or journaled (PRECONDITION_UNMET): a Gap, a sequence "
    "inside a library or a Batch group; FLAME_API_ERROR when connected_segments() raises. "
)


def _write_note(journal_text: str) -> str:
    return (
        "The connections of a segment may reach segments in other sequences: neither a Timeline "
        "checkpoint of this segment's sequence nor journal.revert covers those, so "
        + journal_text
        + _WRITE_NOTE_TAIL
    )


_CREATE_DESCRIPTION = (
    "Create a connection for one segment (PySegment.create_connection). "
    + _write_note(_STEP_JOURNAL)
    + "Expected: more other segments are connected after than before; "
    "result.inverse is segment.remove_connection on the same segment. journal.check of a create "
    "that ended without an answer: landed when more are connected than before, not_landed when "
    "as many, diverged when fewer."
)

_REMOVE_DESCRIPTION = (
    "Remove the connection of one segment (PySegment.remove_connection), or of several in one "
    "call. Give `target` (one segment selector) or `targets` (1 to "
    f"{MAX_REMOVE_TARGETS} segment selectors), not both. "
    + _write_note(_STEP_JOURNAL)
    + "Expected: fewer other segments are connected after than before; "
    "result.inverse is segment.create_connection on the same segment. "
    "`targets`: every target is checked first, in one read (a Gap, a sequence inside a library "
    "or a Batch group, a selector that does not resolve, connected_segments() raising), and a "
    "refusal changes no connection (the error names `targets[i]`). Then each target is a call "
    "of its own, in order, and the connected segments are read again before each one. A failure "
    "stops the call and nothing is retried: the targets before it stay removed. A target whose "
    "call ran but whose read-back failed, or that ended without an answer (TIMEOUT, "
    "BRIDGE_LOST), is `unknown` (its connection may be gone): its intent stays open and "
    "writes are refused until journal.check has decided it. "
    "Measured on Flame 2025.2.4 with a pair of connected segments: removing the connection of "
    "one also removes it from the other. For a group of three or more, read "
    "items[].connected to see what is left. A target that has no other connected segment left "
    "when that target is reached "
    "is not called and is listed in result.alreadyDisconnected; one that was not connected "
    "before the call also gets a warning. result: requested, removed / notRemoved / unknown / "
    "alreadyDisconnected (indexes in `targets`), items (one per target reached: index, ran, "
    "alreadyDisconnected, unknown, the single-target fields above and its own inverse), notices, "
    "warnings, partial. Envelope partial = {removed (includes unknown), notRemoved}. "
    "It cannot be a plan step."
)

_SYNC_DESCRIPTION = (
    "Call PySegment.sync_connected_segments() on one segment. A count cannot show a sync, "
    "so the call is judged by the facts of every segment connected_segments() lists (type, "
    "source name, record and source range, head, tail), read before and after: none changed "
    "is SILENT_NOOP (also when the segment has no connection, or was already in sync), else "
    "ok with result.syncedSegments (how many changed). It also reports the connected count "
    "before and after and what the call returned. "
    + _write_note(_STEP_JOURNAL)
    + "There is no inverse op: result.inverse is null. journal.check of a sync that ended "
    "without an answer is always diverged (a count cannot say whether it ran): read "
    "segment.connections and segment.info and compare by hand."
)


def _connections_to_python(args: Mapping[str, Any]) -> str:
    sel = args["target"]
    return build_source(
        selectors.resolve_code(sel, "target"),
        f"_result = _sc_connections(target, target__parent, {sel['version']!r}, "
        f"{sel['track']!r}, {args['limit']!r}, {args['offset']!r})",
    )


def _remove_many_source(args: Mapping[str, Any]) -> str:
    targets = list(args["targets"])
    blocks = [selectors.resolve_code(sel, f"_t{i}") for i, sel in enumerate(targets)]
    places = [
        f"(_t{i}, _t{i}__parent, {sel.get('version', 0)!r}, {sel.get('track', 0)!r})"
        for i, sel in enumerate(targets)
    ]
    if args.get(PHASE_ARG) == PHASE_STEP:
        return build_source(
            *blocks,
            f"_result = _sc_remove_step(*{places[0]})",
            "_affected = 1 if _result['ran'] else 0",
            raise_on_error_src(),
        )
    return build_source(
        *blocks,
        f"_result = _sc_remove_check_many([{', '.join(places)}])",
        "_affected = 0",
        raise_on_error_src(),
    )


def _write_to_python(action: str) -> Any:
    def to_python(args: Mapping[str, Any]) -> str:
        if args.get("targets") is not None:
            return _remove_many_source(args)
        sel = args["target"]
        return edit_source(
            args,
            f"_sc_write_check(target, target__parent, {action!r}, "
            f"{sel.get('version', 0)!r}, {sel.get('track', 0)!r})",
            f"_sc_write({seg_args(args)}, {action!r})",
        )

    return to_python


_ACCEPTS: Final[tuple[SelectorKind, ...]] = ("segment",)
_REMOVE_SAMPLE: Final[Mapping[str, Any]] = {
    "targets": [
        {
            "kind": "segment",
            "parent": {"kind": "wiretap", "id": "sample"},
            "version": 0,
            "track": 0,
            "index": 0,
        }
    ]
}


def _remove_params() -> tuple[OperationParam, ...]:
    return (
        OperationParam(
            name="target",
            type="selector",
            description=SEGMENT_TARGET_DESCRIPTION + " Not with `targets`.",
            required=False,
        ),
        OperationParam(
            name="targets",
            type="array",
            description=(
                f"1 to {MAX_REMOVE_TARGETS} 'segment' selectors, in any sequences, each as "
                "`target`. Not with `target`."
            ),
            required=False,
            item_type="selector",
        ),
    )


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


def _remove_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    targets = args.get("targets")
    if (targets is None) == (args.get("target") is None):
        return Refusal("INVALID_ARGS", "give exactly one of `target` and `targets`")
    if targets is None:
        return None
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_REMOVE_TARGETS:
        count = len(targets) if isinstance(targets, list) else targets
        return Refusal(
            "INVALID_ARGS",
            f"targets must hold 1 to {MAX_REMOVE_TARGETS} selectors, got {count!r}",
        )
    problems = [
        f"targets[{i}]: {issue}"
        for i, sel in enumerate(targets)
        for issue in selectors.validate_selector(sel, _ACCEPTS)
    ]
    if problems:
        return Refusal("INVALID_ARGS", "invalid arguments: " + "; ".join(problems))
    return None


def _register_write(
    registry: Registry, name: str, description: str, call: str, action: str
) -> None:
    many = action == "remove"
    registry.register(
        OperationSpec(
            name=name,
            category="segment",
            description=description,
            effect="destructive",
            undo="unsupported",
            params=_remove_params() if many else (segment_target_param(),),
            accepts=_ACCEPTS,
            to_python=_write_to_python(action),
            undo_steps=no_undo_steps,
            precheck=_remove_precheck if many else None,
            wrap=run_connection_write,
            sample_args=_REMOVE_SAMPLE if many else None,
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
                limit_param("Max items per call."),
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
