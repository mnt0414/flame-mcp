"""`timeline.usage`: which sources the sequences use. Three reports over the video tracks of the
sequences: offline segments, a source used by more than one segment, and clips no scanned sequence
refers to. The Flame side is `usage_prelude`; the walk over the project is `inspect_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.inspect_prelude import INSPECT_BUDGET_S
from flame_mcp.ops._common import build_source, list_result_src, offset_param
from flame_mcp.ops.desktop_list import SCOPES
from flame_mcp.registry import DEFAULT_LIMIT, MAX_LIMIT, Registry
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind
from flame_mcp.usage_prelude import USAGE_MAX_USES

USAGE_OP: Final[str] = "timeline.usage"
REPORTS: Final[tuple[str, ...]] = ("unlinked", "shared", "unreferenced")
DEFAULT_MAX_SEQUENCES: Final[int] = 50
MAX_SEQUENCES: Final[int] = 500
_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")

_DESCRIPTION = (
    "Report how sequences use their sources: offline segments (unlinked), sources used by 2 or "
    "more segments (shared), or clips no scanned sequence refers to (unreferenced). "
    "Read only, but it runs on Flame's main thread for up to "
    f"{INSPECT_BUDGET_S:g} s and the Flame UI is stopped meanwhile. "
    "`report` picks one report per call. The video tracks of every version of each sequence are "
    "read (a Gap and the audio are not); a Batch group is not read, and a reference from a "
    "Batch Clip node is not counted (`batchUsageNotCounted`), so a clip that only a Batch group "
    "uses is listed in `unreferenced` too. "
    "A source is told by its identities: the segment's file_path and its source name, whichever "
    "is not empty. `shared` groups by the first of them (the key; `matchedBy` says which: "
    "'filePath' or 'sourceName', the weaker one). A segment with neither is counted in "
    "`segmentsWithoutKey` and used by no report. An attribute that cannot be read makes its "
    "sequence unreadable: `sequenceErrors` names it. "
    "`unlinked` lists a segment whose source_unlinked is true or whose type starts with "
    "'Unlinked', with the raw `sourceUnlinked` and `type` it was judged by. `shared` lists a key "
    f"used by 2 or more of the scanned segments with `count` and `uses` (the first "
    f"{USAGE_MAX_USES}, `usesTruncated`); it sees only the sequences scanned in this call. "
    "`unreferenced` lists the PyClips in the scope none of whose identities (the clip's own "
    "key is shown) is in any scanned segment; that means no video segment of a scanned sequence "
    "refers to the clip, not that the clip is unused: a reference from an audio track only, or "
    "from anything that is not a segment (the media of an Action, for one), is not counted. Do "
    "not delete on this alone, and delete with desktop.delete one clip at a time after a person "
    "has checked it. It judges only when the scope is 'all', `sequences` is not given, no "
    "library is closed, "
    "and the keys of every sequence of the project were collected in this one call within the "
    "time budget (`max_sequences` is ignored and `sequence_offset` is refused); otherwise "
    "`judged` is false, `reason` is 'scope', 'sequences', 'closedLibraries', 'budget' or "
    "'unreadable' (a container or a sequence could not be read) and `items` is empty. "
    "`judgedAgainst` says what the judgement counted. "
    "Every item of `unlinked` and `shared` carries a `selector` that goes to the segment.* ops as "
    "it is. To swap a source, run segment.replace in plan.run, with one plan.run per sequence. "
    "`sequences` (selectors of PySequence; any other type is SELECTOR_TYPE) names the "
    "sequences to read; without it every PySequence in `scope` ('all', 'desktop' or "
    "'libraries') is read in the order desktop.list_all lists them. A sequence in a closed "
    "library that `sequences` names is opened for the call and closed again; the walk of "
    "`scope` never opens a library, and `closedLibraries` names the ones it left out. "
    "`max_sequences` (default 50, at most 500) and `sequence_offset` page the sequences: "
    "`nextSequenceOffset` is set while sequences remain, `scannedSequences` lists the ones read "
    "and `segmentsScanned` counts the segments. The scan stops before the next sequence once the "
    "time budget is used up (the first sequence is always read): `stoppedEarly` {reason, "
    "budgetS, read} is set, and a walk that stops early says so the same way; a walk starts "
    "again on every call, so when it stops, narrow it with `scope` or `sequences`. "
    "`limit` and `offset` page the items (`total` counts them all)."
)


def _precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    if args.get("report") == "unreferenced" and args.get("sequence_offset") is not None:
        return Refusal(
            "INVALID_ARGS",
            "sequence_offset does not apply to report='unreferenced': it reads every sequence in "
            "one call or does not judge",
        )
    max_sequences = args.get("max_sequences")
    if isinstance(max_sequences, (int, float)) and max_sequences > MAX_SEQUENCES:
        return Refusal("INVALID_ARGS", f"max_sequences is at most {MAX_SEQUENCES}")
    chosen = args.get("sequences")
    if isinstance(chosen, list) and len(chosen) > MAX_SEQUENCES:
        return Refusal("INVALID_ARGS", f"sequences holds at most {MAX_SEQUENCES} selectors")
    return None


def _to_python(args: Mapping[str, Any]) -> str:
    report = args["report"]
    scope = args.get("scope", "all")
    max_n = int(args.get("max_sequences", DEFAULT_MAX_SEQUENCES))
    seq_offset = int(args.get("sequence_offset") or 0)
    chosen = list(args.get("sequences") or [])
    blocks: list[str] = []
    given = "None"
    if chosen:
        listed = [] if report == "unreferenced" else chosen
        for i, sel in enumerate(listed):
            blocks.append(selectors.resolve_code(sel, f"_seq{i}"))
        checked = ", ".join(
            f"_tu_check_sequence(_seq{i}, 'sequences[{i}]')" for i in range(len(listed))
        )
        given = f"[{checked}]"
    limit = int(args.get("limit", DEFAULT_LIMIT))
    blocks += [
        f"_usage = _tu_run({report!r}, {scope!r}, {given}, {seq_offset}, {max_n}, "
        f"{INSPECT_BUDGET_S!r}, {limit}, {int(args.get('offset', 0))})",
        "_items = _usage.pop('items')",
        "_total = _usage.pop('total')",
        f"_offset = {int(args.get('offset', 0))!r}",
    ]
    return build_source(
        *blocks,
        list_result_src("_items", "_total", "_offset"),
        "\n".join(
            [
                "_result.update(_usage)",
                "if 'nextSequenceOffset' in _usage or 'stoppedEarly' in _usage:",
                "    _result['truncated'] = True",
            ]
        ),
    )


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=USAGE_OP,
            category="timeline",
            description=_DESCRIPTION,
            effect="read",
            params=(
                OperationParam(
                    name="report",
                    type="string",
                    description=(
                        "'unlinked' (segments with offline media), 'shared' (a source used by 2 "
                        "or more segments) or 'unreferenced' (clips no scanned sequence refers "
                        "to). One report per call."
                    ),
                    required=True,
                    enum=REPORTS,
                ),
                OperationParam(
                    name="sequences",
                    type="array",
                    description=(
                        "Selectors of the sequences to read (wiretap / path / selection; each "
                        "must be a PySequence). Omit to read every sequence in `scope`."
                    ),
                    required=False,
                    item_type="selector",
                ),
                OperationParam(
                    name="scope",
                    type="string",
                    description=(
                        "Where the walk looks for sequences when `sequences` is omitted: 'all' "
                        "(the Desktop, then the libraries), 'desktop' or 'libraries' (default "
                        "'all'). report='unreferenced' judges only with 'all'."
                    ),
                    required=False,
                    default="all",
                    enum=SCOPES,
                ),
                OperationParam(
                    name="max_sequences",
                    type="number",
                    description=(
                        f"Max sequences to read in this call (default {DEFAULT_MAX_SEQUENCES}, "
                        f"at most {MAX_SEQUENCES}). Ignored by report='unreferenced'."
                    ),
                    required=False,
                    default=DEFAULT_MAX_SEQUENCES,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="sequence_offset",
                    type="number",
                    description=(
                        "Number of sequences to skip (0-based, default 0): the "
                        "`nextSequenceOffset` of the last call. Refused by "
                        "report='unreferenced'."
                    ),
                    required=False,
                    integer=True,
                    minimum=0,
                ),
                OperationParam(
                    name="limit",
                    type="number",
                    description=(
                        f"Max items to return (default {DEFAULT_LIMIT}, at most {MAX_LIMIT}). "
                        "One item of "
                        "`shared` can hold 20 uses, so keep `limit` small for report='shared'."
                    ),
                    required=False,
                    integer=True,
                    minimum=1,
                ),
                offset_param(),
            ),
            accepts=_ACCEPTS,
            to_python=_to_python,
            precheck=_precheck,
            sample_args={
                "report": "unlinked",
                "sequences": [{"kind": "wiretap", "id": "sample"}],
                "scope": "all",
                "max_sequences": DEFAULT_MAX_SEQUENCES,
                "limit": 50,
                "offset": 0,
            },
            requires=(
                "flame.projects",
                "PyProjectSelector.current_project",
                "PyProject.current_workspace",
                "PyWorkspace.libraries",
                "PyWorkspace.desktop",
                "PyDesktop.reel_groups",
                "PyDesktop.batch_groups",
                "PyReelGroup.reels",
                "PyReel.children",
                "PyLibrary.children",
                "PyLibrary.opened",
                "PyFolder.children",
                "PyClip.get_wiretap_node_id",
                "PySequence.get_wiretap_node_id",
                "PyReelGroup.parent",
                "PyReel.parent",
                "PyFolder.parent",
                "PyLibrary.parent",
                "PyClip.parent",
                "PySequence.parent",
                "PyBatch.parent",
                "PyBatch.name",
                "PyBatch.uid",
                "PySequence.name",
                "PySequence.versions",
                "PyClip.versions",
                "PyVersion.tracks",
                "PyTrack.segments",
                "PySegment.type",
                "PySegment.source_name",
                "PySegment.file_path",
                "PySegment.tape_name",
                "PySegment.source_uid",
                "PySegment.source_unlinked",
                "PySegment.record_in",
            ),
        )
    )
