"""`desktop.sync_record_tc` (many clips, one call), `desktop.set_record_tc` (one clip, journaled)
and `desktop.get_record_tc` (the read the journal uses). The server side of the sync is
`tc_sync_run`, the Flame side `tc_prelude`, the rules `tc_rules`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.desktop_run import MAX_SOURCES
from flame_mcp.journaled_write import FOR_WRITE_ARG
from flame_mcp.ops._common import build_source
from flame_mcp.registry import Registry
from flame_mcp.tc_sync_run import SET_OP, SYNC_OP, run_sync
from flame_mcp.types import JournalSpec, OperationParam, OperationSpec, Refusal, SelectorKind

GET_OP: Final[str] = "desktop.get_record_tc"
TC_KEYS: Final[tuple[str, ...]] = ("start_time_frame", "current_time_frame")
_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")
_SINGLE: tuple[SelectorKind, ...] = ("wiretap", "path")

_SYNC_DESCRIPTION = (
    "Set the Record TC of clips to their Source TC (what flame-tc-sync does): the segment's "
    "record_in becomes its source_in, by writing PyClip.start_time (and current_time, to the same "
    "frame). "
    f"`targets`: 1 to {MAX_SOURCES} wiretap / path / selection selectors of PyClips on the "
    "Desktop. Every clip is read first and gets one row in result.rows: status 'sync' (record_in "
    "differs from source_in: written), 'in_sync' (already equal), or 'skipped' with `reason`. "
    "Skipped, never written silently: not_a_clip, in_library, not_on_desktop, multiple_versions, "
    "no_segment, multi_segment, gap_at_head (a Gap before the media), frame_rate_unreadable / "
    "frame_rate_mismatch (the clip's rate and the segment's source rate differ), duplicate, "
    "unreadable; and, unless include_risky=true, dpx_source (a DPX source) and "
    "no_source_tc (source_in is 0 while the media starts at a later frame). With "
    "include_risky=true those two are written, flagged `risky`. "
    "dry_run=true reads and judges only and writes nothing. "
    "Each row has before / after {recordTcFrame, startTimeFrame, currentTimeFrame} and "
    "sourceTcFrame; a written row has status 'synced' only if record_in reads as source_in "
    "afterwards, else 'failed'. Clips are written one after another, each with its own journal "
    "intent in one group; the first failure stops the call (rows after it are 'not_run', "
    "FLAME_API_ERROR starting with 'partial:' when some were written). A call with nothing "
    "to write (every row in_sync or skipped) is ok with `affected` 0; rows that were skipped "
    "are counted by reason in `degraded`. "
    "Undo: envelope journal = {group, id, revert}; journal.revert(group) writes the earlier "
    "start_time (and current_time) back, newest first, while the clip still reads as the call "
    "left it (CONFLICT otherwise). Native Ctrl+Z is not used. Not allowed in plan.run."
)

_SET_DESCRIPTION = (
    "Set the Record TC of one clip as frame numbers: PyClip.start_time and, if given, "
    "current_time. The frames are absolute (the frame of the timecode at the clip's frame rate). "
    "Prefer desktop.sync_record_tc, which finds the frame from the Source TC; use this to put a "
    "known value back. A target in a library is PRECONDITION_UNMET, a non-clip SELECTOR_TYPE; a "
    "clip anywhere else is written. start_time_frame must read back as asked (else "
    "FLAME_API_ERROR); a current_time_frame that reads back differently is only listed in "
    "result.warnings. "
    "result.recordInFrame is the segment's record_in after the write when the clip has exactly "
    "one segment. JOURNAL: the server reads the current frames, records an intent (envelope "
    "journal = {group, id}), writes with a compare-and-swap (CONFLICT, nothing written, when a "
    "frame changed in between) and reads back; journal.revert(group) writes the earlier frames."
)

_GET_DESCRIPTION = (
    "Read the Record TC of one clip as frame numbers (start_time_frame, current_time_frame): "
    "the read desktop.set_record_tc is journaled with."
)


def _sync_to_python(args: Mapping[str, Any]) -> str:
    targets = list(args["targets"])
    blocks = [selectors.resolve_code(sel, f"_t{i}") for i, sel in enumerate(targets)]
    listed = "[" + ", ".join(f"_t{i}" for i in range(len(targets))) + "]"
    return build_source(*blocks, f"_result = _rtc_facts_many({listed})", "_affected = 0")


def _sync_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    targets = args.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_SOURCES:
        count = len(targets) if isinstance(targets, list) else targets
        return Refusal(
            "INVALID_ARGS", f"targets must hold 1 to {MAX_SOURCES} selectors, got {count!r}"
        )
    problems = [
        f"targets[{i}]: {issue}"
        for i, sel in enumerate(targets)
        for issue in selectors.validate_selector(sel, _ACCEPTS)
    ]
    if problems:
        return Refusal("INVALID_ARGS", "invalid arguments: " + "; ".join(problems))
    return None


def _get_to_python(args: Mapping[str, Any]) -> str:
    guard = "_tl_check_not_library(target, 'the target')" if args.get(FOR_WRITE_ARG) else ""
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        guard,
        f"_result = _rtc_get(target, {list(args['keys'])!r})",
    )


def _set_to_python(args: Mapping[str, Any]) -> str:
    values = {k: int(args[k]) for k in TC_KEYS if args.get(k) is not None}
    expected = args.get("_expected")
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        f"_result = _rtc_set(target, {values!r}, {expected!r})",
        "_affected = len(_result['applied'])",
    )


def _frame_param(name: str, required: bool) -> OperationParam:
    return OperationParam(
        name=name,
        type="number",
        description=f"Absolute frame to write as {name.removesuffix('_frame')}.",
        required=required,
        integer=True,
    )


def _keys(args: Mapping[str, Any]) -> list[str]:
    return [k for k in TC_KEYS if args.get(k) is not None]


def register(registry: Registry) -> None:
    requires = (
        "PyClip.start_time",
        "PyClip.current_time",
        "PyClip.frame_rate",
        "PyClip.start_frame",
        "PyClip.versions",
        "PyClip.get_wiretap_node_id",
        "PyVersion.tracks",
        "PyTrack.segments",
        "PySegment.type",
        "PySegment.record_in",
        "PySegment.source_in",
        "PySegment.source_frame_rate",
        "PySegment.file_path",
        "PyTime.frame",
        "flame.PyTime",
    )
    registry.register(
        OperationSpec(
            name=SYNC_OP,
            category="desktop",
            description=_SYNC_DESCRIPTION,
            effect="reversible",
            undo="value",
            params=(
                OperationParam(
                    name="targets",
                    type="array",
                    description=f"1 to {MAX_SOURCES} selectors of PyClips on the Desktop.",
                    required=True,
                    item_type="selector",
                ),
                OperationParam(
                    name="include_risky",
                    type="boolean",
                    description="Also write DPX sources and sources with no Source TC.",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="dry_run",
                    type="boolean",
                    description="Read and judge only; write nothing.",
                    required=False,
                    default=False,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_sync_to_python,
            undo_steps=lambda args, affected: 0,
            precheck=_sync_precheck,
            wrap=run_sync,
            sample_args={"targets": [{"kind": "wiretap", "id": "s"}]},
            requires=requires,
        )
    )
    registry.register(
        OperationSpec(
            name=SET_OP,
            category="desktop",
            description=_SET_DESCRIPTION,
            effect="reversible",
            undo="value",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A wiretap / path selector of one PyClip (not one in a library).",
                    required=True,
                ),
                _frame_param("start_time_frame", True),
                _frame_param("current_time_frame", False),
            ),
            accepts=_SINGLE,
            to_python=_set_to_python,
            undo_steps=lambda args, affected: 0,
            journal=JournalSpec(
                read_op=GET_OP,
                keys=_keys,
                values=lambda args: {k: int(args[k]) for k in _keys(args)},
                write_args=lambda target, values: {"target": target, **values},
            ),
            sample_args={"target": {"kind": "wiretap", "id": "s"}, "start_time_frame": 0},
            requires=requires,
        )
    )
    registry.register(
        OperationSpec(
            name=GET_OP,
            category="desktop",
            description=_GET_DESCRIPTION,
            effect="read",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A wiretap / path selector of one PyClip.",
                    required=True,
                ),
                OperationParam(
                    name="keys",
                    type="array",
                    description=f"Which frames to read: {list(TC_KEYS)}.",
                    required=True,
                    item_type="string",
                ),
            ),
            accepts=_SINGLE,
            to_python=_get_to_python,
            sample_args={"target": {"kind": "wiretap", "id": "s"}, "keys": ["start_time_frame"]},
            requires=requires,
        )
    )
