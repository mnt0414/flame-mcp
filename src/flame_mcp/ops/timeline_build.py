"""`timeline.overwrite`, `timeline.insert`, `timeline.cut`, `timeline.create_version` and
`timeline.create_track`: build up a sequence on the Desktop. All five run behind a
Timeline checkpoint (`CheckpointSpec(target_kind="timeline")`, the same as `segment.trim` /
`slip` / `replace`: the sequence is copied into the stash first, `journal.revert` puts it back, a
plan bundles them). The Flame side is `timeline_build_prelude`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._segment_edit import PHASE_ARG, PHASE_CHECK, make_check, no_undo_steps
from flame_mcp.registry import Registry
from flame_mcp.timeline_restore import NOT_RESTORED
from flame_mcp.types import (
    CheckpointSpec,
    OperationParam,
    OperationSpec,
    Refusal,
    SelectorKind,
)

OVERWRITE_OP: Final[str] = "timeline.overwrite"
INSERT_OP: Final[str] = "timeline.insert"
CUT_OP: Final[str] = "timeline.cut"
CREATE_VERSION_OP: Final[str] = "timeline.create_version"
CREATE_TRACK_OP: Final[str] = "timeline.create_track"
SCOPES: Final[tuple[str, ...]] = ("track", "all")
_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection", "timeline")
_SOURCE_KINDS: Final[tuple[SelectorKind, ...]] = ("wiretap", "path", "selection")

_CHECKPOINTED = (
    "Runs behind an automatic checkpoint of the sequence: it is copied into the stash reel first "
    "(timeline.checkpoint), the call is journaled (intent with the fingerprint before, outcome "
    "with the fingerprint after) and journal.revert(group) puts the sequence back, only while it "
    "still has the fingerprint the call left (else CONFLICT and nothing is touched; force=true "
    "overrides, journaled). The restored sequence has a new uid and wiretap id and sits at the "
    "end of its reel (result.preCheckpoint: the revert can itself be undone). Not put back: "
    + "; ".join(NOT_RESTORED)
    + ". A call that changes nothing is SILENT_NOOP. A sequence inside a library or a Batch group "
    "is refused (PRECONDITION_UNMET) before anything is copied or journaled. Envelope journal = "
    "{group, id, checkpoint, checkpointPath, stash, revert, changed}. In plan.run the step joins "
    "the plan's one checkpointed write on this sequence."
)
_STATE = (
    "before / after = {startFrame, duration, versionCount, tracks [{version, track, "
    "segmentCount, startFrame, endFrame (the last frame, inclusive), duration, trailingGap}]}"
)
_PUT_RESULT = (
    "result: mode, at, source {type, name, uid, wiretapId, selector}, " + _STATE + ", with "
    "`layout` ([type, recordIn, recordOut, sourceName] of the segments around `at`, "
    "layoutWindowed when only part is listed) on the target track, startFrame {before, after, "
    "changed}, placed (the position of the media segment now at `at`, with a 'segment' selector "
    "for the next edit), changed, returned, warnings."
)

_OVERWRITE_DESCRIPTION = (
    "Lay a clip over one track of a sequence from a record frame, replacing what is there. "
    "`at` is the absolute record frame (the number timeline.list_segments shows as recordIn."
    "frame), 0 or more; a frame after the end of the track leaves a Gap before the clip. A clip "
    "with in / out marks puts only the marked range (the out mark itself is not included). "
    "Putting a clip into an empty sequence can change the sequence's start_frame "
    "(result.startFrame). source is a wiretap / path / selection selector of a PyClip (in a reel "
    "or a library). Refused before anything is copied or journaled: a source that is not a "
    "PyClip (SELECTOR_TYPE), a version or track that does not exist (SELECTOR_EMPTY). "
    + _PUT_RESULT
    + " "
    + _CHECKPOINTED
)
_INSERT_DESCRIPTION = (
    "Insert a clip into one track of a sequence at a record frame, moving what follows in that "
    "track later. "
    "`at` is the absolute record frame (the number timeline.list_segments shows as recordIn."
    "frame), 0 or more; inserting inside a segment splits it, a frame after the end of the track "
    "leaves a Gap before the clip. Only the target track moves: the other tracks stay where they "
    "are and are then out of step with it (the tracks' endFrame in result.after show it). A clip "
    "with in / out marks puts only the marked range (the out mark itself is not included). "
    "source is a wiretap / path / selection selector of a PyClip (in a reel or a library). "
    "Refused before anything is copied or journaled: a source that is not a PyClip "
    "(SELECTOR_TYPE), a version or track that does not exist (SELECTOR_EMPTY). "
    + _PUT_RESULT
    + " "
    + _CHECKPOINTED
)
_CUT_DESCRIPTION = (
    "Cut one track, or every track, of a sequence at a record frame, so that `at` becomes the "
    "first frame of the right part. "
    "`at` is the absolute record frame (the number timeline.list_segments shows as recordIn."
    "frame). scope='track' (default) cuts the track given by version and track and leaves the "
    "others alone; scope='all' cuts the sequence at `at`, and version and track are not given. "
    "Refused before anything is copied or journaled (PRECONDITION_UNMET): scope='track' when `at` "
    "is not inside a media segment of that track (record_in < at <= record_out of a segment that "
    "is not a Gap: a cut at the first frame of a segment, in a Gap or outside the segments "
    "changes nothing), scope='all' when no track has such a segment (result.mediaSegments lists "
    "where there are some). INVALID_ARGS: a scope that is neither, scope='track' without track, "
    "version / track with scope='all'. "
    "result: scope, at, willCut (the media segments `at` falls inside), " + _STATE + ", with "
    "`layout` around `at` (the target track, or every track for scope='all'), split (scope="
    "'track': whether a segment now ends at at - 1 beside one that starts at `at`; scope='all': "
    "the tracks that were cut), changed, returned, warnings. " + _CHECKPOINTED
)
_CREATE_VERSION_DESCRIPTION = (
    "Add a version to a sequence. "
    "It goes at the end; result.version is {index, tracks} of the new one (what it holds), "
    "versionsBefore / versionsAfter list the track count of every version, " + _STATE + ", "
    "changed, returned, warnings. " + _CHECKPOINTED
)
_CREATE_TRACK_DESCRIPTION = (
    "Add a track to one version of a sequence. "
    "It goes at the end of the version; result.track is {index, summary} of the new one, "
    "tracksBefore / tracksAfter are the version's track counts, " + _STATE + ", changed, "
    "returned, warnings. Refused before anything is copied or journaled: a version that does "
    "not exist (SELECTOR_EMPTY). " + _CHECKPOINTED
)


def _checkpoint(op_name: str, label: Callable[[Mapping[str, Any]], str]) -> CheckpointSpec:
    return CheckpointSpec(
        target_kind="timeline",
        batch_arg="sequence",
        label=label,
        check=make_check(op_name),
    )


def _sequence_param() -> OperationParam:
    return OperationParam(
        name="sequence",
        type="selector",
        description=(
            "A wiretap / path / selection / timeline selector of a sequence on the Desktop "
            "(timeline: the sequence the Timeline is showing)."
        ),
        required=True,
    )


def _at_param() -> OperationParam:
    return OperationParam(
        name="at",
        type="number",
        description=(
            "The absolute record frame (an integer, 0 or more): the recordIn.frame "
            "timeline.list_segments shows."
        ),
        required=True,
        integer=True,
        minimum=0,
    )


def _index_param(name: str, what: str, *, default: bool = True) -> OperationParam:
    if default:
        return OperationParam(
            name=name,
            type="number",
            description=f"Index of the {what} (default 0).",
            required=False,
            default=0,
            integer=True,
            minimum=0,
        )
    return OperationParam(
        name=name,
        type="number",
        description=f"Index of the {what}.",
        required=False,
        integer=True,
        minimum=0,
    )


def _source_param() -> OperationParam:
    return OperationParam(
        name="source",
        type="selector",
        description=(
            "A wiretap / path / selection selector of the PyClip to put in (it may be in a reel "
            "or a library)."
        ),
        required=True,
    )


def _put_label(mode: str) -> Callable[[Mapping[str, Any]], str]:
    def label(args: Mapping[str, Any]) -> str:
        return f"before timeline.{mode} (at {args['at']})"

    return label


def _phase_code(args: Mapping[str, Any], check_call: str, write_call: str, *resolve: str) -> str:
    if args.get(PHASE_ARG) == PHASE_CHECK:
        return build_source(
            *resolve, f"_result = {check_call}", raise_on_error_src(), "_affected = 0"
        )
    return build_source(*resolve, f"_result = {write_call}", raise_on_error_src(), "_affected = 1")


def _put_to_python(mode: str) -> Callable[[Mapping[str, Any]], str]:
    def to_python(args: Mapping[str, Any]) -> str:
        tail = (
            f"seq, {int(args.get('version', 0))!r}, {int(args.get('track', 0))!r}, clip, "
            f"{int(args['at'])!r}, {mode!r})"
        )
        return _phase_code(
            args,
            f"_tb_put_check({tail}",
            f"_tb_put({tail}",
            selectors.resolve_code(args["sequence"], "seq"),
            selectors.resolve_code(args["source"], "clip"),
        )

    return to_python


def _cut_to_python(args: Mapping[str, Any]) -> str:
    tail = (
        f"seq, {int(args.get('version', 0))!r}, {int(args.get('track', 0))!r}, "
        f"{int(args['at'])!r}, {args.get('scope', 'track')!r})"
    )
    return _phase_code(
        args,
        f"_tb_cut_check({tail}",
        f"_tb_cut({tail}",
        selectors.resolve_code(args["sequence"], "seq"),
    )


def _create_version_to_python(args: Mapping[str, Any]) -> str:
    return _phase_code(
        args,
        "_tb_create_version_check(seq)",
        "_tb_create_version(seq)",
        selectors.resolve_code(args["sequence"], "seq"),
    )


def _create_track_to_python(args: Mapping[str, Any]) -> str:
    version = int(args.get("version", 0))
    return _phase_code(
        args,
        f"_tb_create_track_check(seq, {version!r})",
        f"_tb_create_track(seq, {version!r})",
        selectors.resolve_code(args["sequence"], "seq"),
    )


def _put_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    problems = selectors.validate_selector(args.get("source"), _SOURCE_KINDS)
    if problems:
        return Refusal(
            "INVALID_ARGS",
            "source must be a wiretap / path / selection selector of a clip: "
            + "; ".join(problems),
        )
    return None


def _cut_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    scope = args.get("scope", "track")
    if scope not in SCOPES:
        return Refusal("INVALID_ARGS", f"scope must be one of {list(SCOPES)}, got {scope!r}")
    if scope == "track" and "track" not in args:
        return Refusal("INVALID_ARGS", "scope='track' needs track (the index of the track to cut)")
    if scope == "all":
        given = [key for key in ("version", "track") if key in args]
        if given:
            return Refusal("INVALID_ARGS", f"{given} only apply to scope='track'")
    return None


_PUT_REQUIRES = (
    "PySequence.overwrite",
    "PySequence.insert",
    "PySequence.frame_rate",
    "PySequence.start_frame",
    "PySequence.duration",
    "PySequence.versions",
    "PyVersion.tracks",
    "PyTrack.segments",
    "PySegment.type",
    "PySegment.source_name",
    "PySegment.record_in",
    "PySegment.record_out",
    "flame.PyTime",
)


def _register_put(
    registry: Registry, name: str, description: str, requires: str, mode: str
) -> None:
    registry.register(
        OperationSpec(
            name=name,
            category="timeline",
            description=description,
            effect="reversible",
            undo="checkpoint",
            params=(
                _sequence_param(),
                _source_param(),
                _at_param(),
                _index_param("version", "version"),
                _index_param("track", "track of that version"),
            ),
            accepts=_ACCEPTS,
            to_python=_put_to_python(mode),
            undo_steps=no_undo_steps,
            precheck=_put_precheck,
            checkpoint=_checkpoint(name, _put_label(mode)),
            requires=(requires, *_PUT_REQUIRES),
        )
    )


def register(registry: Registry) -> None:
    _register_put(
        registry, OVERWRITE_OP, _OVERWRITE_DESCRIPTION, "PySequence.overwrite", "overwrite"
    )
    _register_put(registry, INSERT_OP, _INSERT_DESCRIPTION, "PySequence.insert", "insert")
    registry.register(
        OperationSpec(
            name=CUT_OP,
            category="timeline",
            description=_CUT_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(
                _sequence_param(),
                _at_param(),
                OperationParam(
                    name="scope",
                    type="string",
                    description=(
                        "'track' (default): cut the track given by version and track. 'all': cut "
                        "the whole sequence at `at`."
                    ),
                    required=False,
                    default="track",
                    non_empty=True,
                ),
                _index_param("version", "version (scope='track'; default 0)", default=False),
                _index_param(
                    "track", "track of that version (scope='track', required then)", default=False
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_cut_to_python,
            undo_steps=no_undo_steps,
            precheck=_cut_precheck,
            checkpoint=_checkpoint(CUT_OP, lambda args: f"before timeline.cut (at {args['at']})"),
            requires=("PyTrack.cut", "PySequence.cut", *_PUT_REQUIRES),
        )
    )
    registry.register(
        OperationSpec(
            name=CREATE_VERSION_OP,
            category="timeline",
            description=_CREATE_VERSION_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(_sequence_param(),),
            accepts=_ACCEPTS,
            to_python=_create_version_to_python,
            undo_steps=no_undo_steps,
            checkpoint=_checkpoint(
                CREATE_VERSION_OP, lambda args: "before timeline.create_version"
            ),
            requires=("PySequence.create_version", *_PUT_REQUIRES),
        )
    )
    registry.register(
        OperationSpec(
            name=CREATE_TRACK_OP,
            category="timeline",
            description=_CREATE_TRACK_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(_sequence_param(), _index_param("version", "version")),
            accepts=_ACCEPTS,
            to_python=_create_track_to_python,
            undo_steps=no_undo_steps,
            checkpoint=_checkpoint(CREATE_TRACK_OP, lambda args: "before timeline.create_track"),
            requires=("PyVersion.create_track", *_PUT_REQUIRES),
        )
    )
