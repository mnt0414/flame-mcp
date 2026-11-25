"""`timeline.overwrite`, `timeline.insert`, `timeline.cut`, `timeline.create_version` and
`timeline.create_track`: build up a sequence on the Desktop, and the helpers every op of the family
shares (`timeline_restructure.py` has `timeline.delete_segments` and `timeline.move_track`,
`timeline_shorten.py` has `timeline.shorten`). All of them run behind a Timeline checkpoint
(`CheckpointSpec(target_kind="timeline")`, the same as `segment.trim` / `slip` / `replace`: the
sequence is copied into the stash first, `journal.revert` puts it back, a plan bundles them). The
Flame side is `timeline_build_prelude`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._segment_edit import (
    CHECK_PHASES,
    PHASE_CHECK,
    make_check,
)
from flame_mcp.registry import Registry
from flame_mcp.timeline_restore import NOT_RESTORED
from flame_mcp.types import (
    PHASE_ARG,
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
ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection", "timeline")
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
AUDIO_JUDGED = (
    "The fingerprint that decides 'nothing changed' sees the video versions, tracks and segments, "
    "not audio, so the audio is read on its own before and after (PySequence.audio_tracks, each "
    "track's channels, each channel's segments: type, source_name, record_in / record_out and "
    "source_in / source_out frames) and a difference counts as a change: a call that changed only "
    "the audio is not a SILENT_NOOP and says so in result.warnings. result.audio = {before, "
    "after, changed, complete}; before / after = {trackCount (null when the sequence has no "
    "audio_tracks), channelCount, segmentCount, rows [track, channel, type, sourceName, recordIn, "
    "recordOut, sourceIn, sourceOut] (at most 24), rowsTruncated, errors}. A member that cannot "
    "be read leaves null in its cell and a marker in errors, and the call goes on. result.changed "
    "is the video or the audio changed. The fingerprint the journal and journal.revert compare is "
    "not changed by this, so journal.changed stays false for a call that changed only the audio, "
    "and the revert check compares the video fingerprint only. Measured on Flame 2025.2.4: type, "
    "source_name, record_in, record_out, source_in and source_out can all be read on the "
    "Audio Segment objects of an audio channel. "
)
CHECKPOINTED_AUDIO = _CHECKPOINTED.replace(
    "A call that changes nothing is SILENT_NOOP.",
    "A call that changes nothing, video tracks and audio tracks read back, is SILENT_NOOP.",
)
_PUT_RESULT = (
    "result: mode, at, source {type, name, uid, wiretapId, selector}, " + _STATE + ", with "
    "`layout` ([type, recordIn, recordOut, sourceName] of the segments around `at`, "
    "layoutWindowed when only part is listed) on the target track, startFrame {before, after, "
    "changed}, placed (the position of the media segment now at `at`, with a 'segment' selector "
    "for the next edit), placement {expectedName, foundName, nameChecked, expectedLength, "
    "foundLength}, changed, returned, warnings. After the put the track is read back: a media "
    "segment must start at `at`, carry the clip's name as source_name and be as long as the "
    "clip's used range (the marked range, else the whole clip); a segment whose source_name is "
    "empty, or put from a clip whose name could not be read, is judged by its start and length "
    "only (placement.nameChecked false, and degraded says so); when the sequence changed but not "
    "that way "
    "the call is FLAME_API_ERROR with a message starting `partial:` (the change stays, "
    "result.after shows it, journal.revert puts the sequence back). If the length of the clip "
    "cannot be read it is not compared and a warning says so."
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
    "Measured on Flame 2025.2.4: overwriting a clip that has audio onto a video track also "
    "replaced the audio of the sequence, even where the video segment held the same source and "
    "did not change. " + _PUT_RESULT + " " + AUDIO_JUDGED + CHECKPOINTED_AUDIO
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
    + AUDIO_JUDGED
    + CHECKPOINTED_AUDIO
)
_CUT_DESCRIPTION = (
    "Cut one track, or every track, of a sequence at a record frame, so that `at` becomes the "
    "first frame of the right part. "
    "`at` is the absolute record frame (the number timeline.list_segments shows as recordIn."
    "frame). scope='track' (default) cuts the track given by version and track and leaves the "
    "others alone; scope='all' cuts the sequence at `at`, and version and track are not given. "
    "A segment is cut where record_in < at <= record_out. Measured on Flame 2025.2.4: "
    "PyTrack.cut inside a Gap splits it into two Gaps (and through this op: a "
    "cut at 40 inside a Gap 0-83 gave 0-39 and 40-83), so scope='track' also cuts a Gap. "
    "scope='all' needs a segment that has media. "
    "Refused before anything is copied or journaled (PRECONDITION_UNMET): scope='track' when `at` "
    "is not inside a segment of that track (media or Gap: a cut at the first frame of a segment, "
    "or outside the track's segments, changes nothing), scope='all' when no track has a media "
    "segment around `at` (a Gap alone is not enough; result.segments lists the segments of the "
    "track, or the media segments of all tracks, with their type). INVALID_ARGS: a scope that "
    "is neither, scope='track' without track, "
    "version / track with scope='all'. "
    "result: scope, at, willCut (the segments `at` falls inside, Gaps only for scope='track': "
    "version, track, type, recordIn, recordOut), " + _STATE + ", with "
    "`layout` around `at` (the target track, or every track for scope='all'), split (scope="
    "'track': whether a segment now ends at at - 1 beside one that starts at `at`; scope='all': "
    "the tracks that were cut, and notSplit, the tracks willCut lists that were not), changed, "
    "returned, audio, warnings. With scope='all', when the video changed but some track of "
    "willCut was not split, the call is FLAME_API_ERROR with a message starting `partial:` "
    "(journal.revert puts the sequence back). " + AUDIO_JUDGED + CHECKPOINTED_AUDIO
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


def checkpoint_spec(op_name: str, label: Callable[[Mapping[str, Any]], str]) -> CheckpointSpec:
    return CheckpointSpec(
        target_kind="timeline",
        batch_arg="sequence",
        label=label,
        check=make_check(op_name),
    )


def sequence_param() -> OperationParam:
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


def index_param(name: str, what: str, *, default: bool = True) -> OperationParam:
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


def phase_code(args: Mapping[str, Any], check_call: str, write_call: str, *resolve: str) -> str:
    if args.get(PHASE_ARG) == PHASE_CHECK:
        return build_source(
            *resolve, f"_result = {check_call}", raise_on_error_src(), "_affected = 0"
        )
    return build_source(
        *resolve,
        f"_result = {write_call}",
        "_affected = 0 if _result['error'] is not None and not _result.get('changed') else 1",
        raise_on_error_src(),
    )


def _put_to_python(mode: str) -> Callable[[Mapping[str, Any]], str]:
    def to_python(args: Mapping[str, Any]) -> str:
        tail = (
            f"seq, {int(args.get('version', 0))!r}, {int(args.get('track', 0))!r}, clip, "
            f"{int(args['at'])!r}, {mode!r})"
        )
        return phase_code(
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
    return phase_code(
        args,
        f"_tb_cut_check({tail}",
        f"_tb_cut({tail}",
        selectors.resolve_code(args["sequence"], "seq"),
    )


def _create_version_to_python(args: Mapping[str, Any]) -> str:
    return phase_code(
        args,
        "_tb_create_version_check(seq)",
        "_tb_create_version(seq)",
        selectors.resolve_code(args["sequence"], "seq"),
    )


def _create_track_to_python(args: Mapping[str, Any]) -> str:
    version = int(args.get("version", 0))
    return phase_code(
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


AUDIO_REQUIRES = ("PySequence.audio_tracks", "PyAudioTrack.channels")
# What the audio snapshot of `_tb_audio_read` reads. The segments of a channel are reachable only
# that way, but `PyAudioChannel.segments` is not in the API symbol list.
PUT_REQUIRES = (
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
    "PyClip.in_mark",
    "PyClip.out_mark",
    "PyClip.duration",
    "flame.PyTime",
)


def _register_put(
    registry: Registry, name: str, description: str, requires: str, mode: str
) -> None:
    registry.register(
        OperationSpec(
            name=name,
            brief_paths=("at", "placed"),
            category="timeline",
            description=description,
            effect="reversible",
            undo="checkpoint",
            params=(
                sequence_param(),
                _source_param(),
                _at_param(),
                index_param("version", "version"),
                index_param("track", "track of that version"),
            ),
            accepts=ACCEPTS,
            to_python=_put_to_python(mode),
            precheck=_put_precheck,
            phases=CHECK_PHASES,
            checkpoint=checkpoint_spec(name, _put_label(mode)),
            requires=(requires, *PUT_REQUIRES, *AUDIO_REQUIRES),
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
            brief_paths=("scope", "at", "split", "notSplit"),
            category="timeline",
            description=_CUT_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(
                sequence_param(),
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
                index_param("version", "version (scope='track'; default 0)", default=False),
                index_param(
                    "track", "track of that version (scope='track', required then)", default=False
                ),
            ),
            accepts=ACCEPTS,
            to_python=_cut_to_python,
            precheck=_cut_precheck,
            phases=CHECK_PHASES,
            checkpoint=checkpoint_spec(
                CUT_OP, lambda args: f"before timeline.cut (at {args['at']})"
            ),
            requires=("PyTrack.cut", "PySequence.cut", *PUT_REQUIRES, *AUDIO_REQUIRES),
        )
    )
    registry.register(
        OperationSpec(
            name=CREATE_VERSION_OP,
            brief_paths=("version",),
            category="timeline",
            description=_CREATE_VERSION_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(sequence_param(),),
            accepts=ACCEPTS,
            to_python=_create_version_to_python,
            phases=CHECK_PHASES,
            checkpoint=checkpoint_spec(
                CREATE_VERSION_OP, lambda args: "before timeline.create_version"
            ),
            requires=("PySequence.create_version", *PUT_REQUIRES),
        )
    )
    registry.register(
        OperationSpec(
            name=CREATE_TRACK_OP,
            brief_paths=("track",),
            category="timeline",
            description=_CREATE_TRACK_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
            params=(sequence_param(), index_param("version", "version")),
            accepts=ACCEPTS,
            to_python=_create_track_to_python,
            phases=CHECK_PHASES,
            checkpoint=checkpoint_spec(
                CREATE_TRACK_OP, lambda args: "before timeline.create_track"
            ),
            requires=("PyVersion.create_track", *PUT_REQUIRES),
        )
    )
