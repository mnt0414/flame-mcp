"""`timeline.overwrite`, `timeline.insert`, `timeline.cut`, `timeline.create_version`,
`timeline.create_track`, `timeline.delete_segments` and `timeline.move_track`: build up a sequence
on the Desktop. All seven run behind a Timeline checkpoint
(`CheckpointSpec(target_kind="timeline")`, the same as `segment.trim` / `slip` / `replace`: the
sequence is copied into the stash first, `journal.revert` puts it back, a plan bundles them). The
Flame side is `timeline_build_prelude`, and `timeline_delete_prelude` for
`timeline.delete_segments` and `timeline_move_prelude` for `timeline.move_track`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._segment_edit import PHASE_ARG, PHASE_CHECK, make_check, no_undo_steps
from flame_mcp.registry import Registry
from flame_mcp.timeline_checkpoints import checkpoint_library_name, stash_reel_name
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
DELETE_SEGMENTS_OP: Final[str] = "timeline.delete_segments"
MOVE_TRACK_OP: Final[str] = "timeline.move_track"
MAX_DELETE_SEGMENTS: Final[int] = 100
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
_AUDIO_JUDGED = (
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
_CHECKPOINTED_AUDIO = _CHECKPOINTED.replace(
    "A call that changes nothing is SILENT_NOOP.",
    "A call that changes nothing, video tracks and audio tracks read back, is SILENT_NOOP.",
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
    "Measured on Flame 2025.2.4: overwriting a clip that has audio onto a video track also "
    "replaced the audio of the sequence, even where the video segment held the same source and "
    "did not change. " + _PUT_RESULT + " " + _AUDIO_JUDGED + _CHECKPOINTED_AUDIO
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
    + _AUDIO_JUDGED
    + _CHECKPOINTED_AUDIO
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
    "the tracks that were cut), changed, returned, audio, warnings. "
    + _AUDIO_JUDGED
    + _CHECKPOINTED_AUDIO
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

_DELETE_SEGMENTS_DESCRIPTION = (
    "Delete segments (media and / or Gaps) from one track of a sequence by their record_in, "
    "closing the hole that is left (ripple, the default). "
    "`segments` lists the record_in frames (absolute: the number timeline.list_segments shows as "
    "recordIn.frame; each one a segment of the track, no duplicates, at most "
    f"{MAX_DELETE_SEGMENTS}) of the track given by `version` (default 0) and `track` (required, as "
    "for timeline.cut). `expect_source_names` is an optional list parallel to `segments`: a "
    "segment whose source_name is another ends the call in CONFLICT before anything is copied "
    "or journaled (the sourceName timeline.list_segments shows, a Gap's too). "
    "How it works: only the targets get PySegment.selected = True, then the call is "
    "PySequence.extract_selection_to_media_panel (ripple=true, the default) or "
    "PySequence.lift_selection_to_media_panel (ripple=false) with the stash reel of the Timeline "
    "checkpoints as the destination, so no reel of yours is touched. Measured on Flame 2025.2.4: "
    "extract removes the selected segments and moves the later segments of THAT TRACK left; the "
    "content of the other tracks stays where it is. The length of the sequence then becomes the "
    "end of the longest content (everything but a Gap) of any video track or audio channel, and "
    "the Gap at the end of every track follows it: the track that ends in content at the new end "
    "has no Gap, a track that was not touched has its Gap at the end shortened (or gone), and the "
    "track the segments were taken from gets a Gap at the end when its content ends short of the "
    "new end (when the audio decides the length, the sequence stays as long as it was). So "
    "removing the last content of the longest track makes the whole sequence shorter, and "
    "result.audio can report a change that is only the Gap at the end of the audio "
    "(result.after.sequenceDuration and timeline.list_segments show the rest). "
    "That was measured on sequences as long as their longest content, with start_time 0. For "
    "a sequence longer than its longest content (every track ends in a Gap), for one whose "
    "start_time is not 0, and when the start_time or the audio cannot "
    "be read completely, the length of the sequence and the Gap at the end of the tracks are not "
    "judged, a warning in result.warnings says so, and the media segments are still compared. "
    "Consecutive selected segments come out as one block. A selected Gap is closed the same way. "
    "Several segments that are not next to each other work in one call (shifted is the sum of "
    "the lengths removed in front of the last later segment). lift (ripple=false) removes the "
    "selected segment and leaves a Gap of the same length, nothing after it moves (shifted 0) "
    "and the other tracks are untouched. Both work on a sequence that is not shown in the "
    "Timeline. journal.revert after such a call restored the sequence (the fingerprint matched) "
    "and the connections were kept. A result that is not the media segments left where they "
    "were, for ripple=false, is reported in result.warnings, not as an error. "
    "The call makes a clip (a Gap alone: an empty sequence) of the selection in the stash reel. "
    "That is only a by-product of this call: it is found as the one new entry of the reel and "
    "deleted again (flame.delete); nothing else is ever deleted. result.extracted says what it "
    "was (type, name, uid, duration) and result.cleanup whether deleting it worked (if not, "
    "result.warnings says so and the segments are removed all the same). "
    "The call acts on the whole selection of the sequence, so the selection is read first from "
    "PySequence.selected_segments (video and audio segments), selected_transitions and "
    "selected_markers (and the tracks' selected_transitions). Selected segments are deselected "
    "(selected = False), only the targets are selected, and PySequence.selected_segments is read "
    "back: it must hold exactly the targets or nothing is called. Afterwards the selection is "
    "put back as far as the segments can be followed (a segment that is still there, a target "
    "that was not removed included; result.selection: before, after, changed, error). A "
    "selected transition or marker is not cleared that way and is not guessed at: the call is "
    "refused (PRECONDITION_UNMET) naming what is selected, before anything is copied or "
    "journaled. "
    "Refused before anything is copied or journaled: a record_in that no segment of the track "
    "has (SELECTOR_EMPTY, result.before is what the track held), a source_name that differs "
    "(CONFLICT), a version or track that does not exist (SELECTOR_EMPTY), a sequence inside a "
    "library or a Batch group (PRECONDITION_UNMET), a clip (SELECTOR_TYPE); INVALID_ARGS: no "
    "segments, more than the cap, a duplicate, a record_in that is negative or not an integer, "
    "expect_source_names of another length, no track. "
    "Judged by reading the track back, never by the answer: nothing in the sequence changed = "
    "SILENT_NOOP (audio included, see below); with ripple, media segments left that are not "
    "the others moved left by the length removed in front of them, the content of another track "
    "(video, or audio: everything but the Gap at its end) that changed, a Gap at the end of any "
    "track that does not end where the longest content ends, or a sequence whose length is not "
    "that end (counted from the first record_in) = FLAME_API_ERROR (the sequence is still "
    "revertible). With no content left anywhere, or an audio that cannot be read completely, "
    "the length is not judged. "
    "result: ripple, method, version, track, removed [{index, type, recordIn, recordOut, "
    "duration, name, sourceName}], shifted (frames the last later segment moved: the length "
    "removed in front of it; 0 without ripple), before / after = {segmentCount, gapCount, "
    "mediaCount, startFrame, endFrame (the last frame, inclusive), trailingGap, "
    "sequenceDuration}, changed, returned, extracted, cleanup, selection, display, "
    "stash {library, reel}, libraryCreated, reelCreated, "
    "connected {total, segments [{recordIn, count}], unreadable}, "
    "audio, notices, warnings. "
    + _AUDIO_JUDGED
    + "A removed segment that has other connected segments "
    "(PySegment.connected_segments() minus the segment itself, see segment.connections) adds a "
    "notice with their count; a segment with no connection adds none. The op reads the counts "
    "and changes nothing else about them. Measured on Flame 2025.2.4, with the op run on a copy "
    "whose segments were connected to an original: the original sequence was unchanged "
    "afterwards (segment count and positions read back), also after the op deleted its "
    "by-product clips. "
    "journal.revert restores this sequence only. " + _CHECKPOINTED_AUDIO
)

_MOVE_TRACK_DESCRIPTION = (
    "Move one video track of a version to another position, by rebuilding it: the track is "
    "copied onto a new track at the target and the original is deleted. "
    "Example: the top track to the bottom is track = the track count minus 1, to = 0. "
    "`track` and `to` are 0-based list indexes, as timeline.list_segments uses them (0 is the "
    "bottom track): `to` is the index the track must have in the final order (the track count "
    "minus 1 is the top); `version` defaults to 0. "
    "Measured on Flame 2025.2.4: no native call moves a track (PySequence, "
    "PyVersion, PyTrack and PyAudioTrack have no move or order method), so the op rebuilds the "
    "track: (1) PyTrack.copy_to_media_panel copies it into a clip in the stash reel of the "
    "Timeline checkpoints; the clip holds the track from its first media segment to its last "
    "media segment, internal Gaps included, the leading and the trailing Gap not. (2) "
    "PyVersion.create_track(track_index=k) adds an empty track, one Gap as long as the sequence; "
    "k is 1-based (1 is the bottom, -1 appends at the top, 0 raises 'Invalid track index.'). The "
    "op sends k = to + 1 when the track moves down, k = to + 2 when it moves up, and -1 when `to` "
    "is the top. (3) PySequence.overwrite puts the "
    "clip at the record_in of the first media segment (frame 0 would shift everything), and the "
    "new track then equals the source row for row: type, record range, source range, source_name, "
    "segment name and effect types; Action and Timewarp setups are identical but for display "
    "state, and the connected segments are kept. (4) flame.delete(source track, "
    "confirm=False) removes the source and leaves the other tracks as they are. (5) The clip is "
    "a by-product of this call: it is found as the one new entry of the stash reel and deleted "
    "again; nothing else is ever deleted (result.clip, result.cleanup.clip). The copy is "
    "compared with the source before the source is deleted, and the audio tracks are read again "
    "at that point: a copy that does not match, or audio that changed, leaves the source where "
    "it is, removes the track create_track added again with flame.delete (result.cleanup.track) "
    "and ends in FLAME_API_ERROR; so does a failure after the create_track result was checked "
    "and before the source is deleted (an overwrite or an attribute step that raises). The new "
    "track is left, and journal.revert puts it back, when the create_track result itself does "
    "not check out (wrong track count, a new track that is not empty, wrong position) and when "
    "the delete of the source track fails. A copy that raises has the by-product clip deleted "
    "too. "
    "The readback that gates the delete compares the type, record range, source range, "
    "source_name, segment name and effect types of the segments only: effect parameters (the "
    "Action and Timewarp setups were compared by hand in the measurement, not by the code), "
    "markers and colours are not compared. "
    "A source track that is only Gap(s) is moved without a copy: the empty track create_track "
    "makes goes to the target and the source is deleted (measured on Flame 2025.2.4, "
    "top to bottom: the order came out right). The name, hidden and locked of the source are put "
    "on the new track and read back (result.attributes {copied, failed}; measured on that "
    "call: all three were copied); a value that does not stay is a warning, never a failure. "
    "The audio rows read through result.audio (type, record and source frames, source_name) "
    "were complete (measured). "
    "No method to move or reorder audio tracks was found on PySequence or PyAudioTrack in "
    "Flame 2025.2.4 (measured by listing them). The "
    "audio tracks are not touched; result.audio reads them before and after. Measured on Flame "
    "2025.2.4 and 2026.2.3 on a sequence with two video tracks and one audio "
    "track of two channels, the audio laid on video track 0: moving track 1, which holds no "
    "audio clip, to the bottom (to=0), and moving track 0, which holds the audio clip, to the "
    "top (to=1), both left result.audio as it was (changed=false, complete=true, the audio rows "
    "equal) and the order came out right; journal.revert of each put the fingerprint and the "
    "video rows back and the "
    "audio rows were as before (the audio did not change in either move). "
    "PyTrack.transitions lists a 'Cut' transition of duration 0 at every segment boundary "
    "(measured on Flame 2025.2.4); tracks that hold only those were moved in the "
    "measurement. Refused before anything is copied or journaled (PRECONDITION_UNMET): a source "
    "track with any other transition (a dissolve etc., or a duration other than 0) or with one "
    "whose type or duration cannot be read; likewise a "
    "stereo_linked track and a version with a single track; a track whose Gap(s) at the end (or "
    "whose only Gap(s)) carry an effect or have effects that cannot be read, because the "
    "readback leaves those Gaps out; and a "
    "sequence inside a library or a Batch group. Also refused: `to` equal to `track` "
    "(INVALID_ARGS), a negative or non-integer index (INVALID_ARGS), a version, track or `to` "
    "that does not exist (SELECTOR_EMPTY), a clip (SELECTOR_TYPE). "
    "Judged by reading every track back, never by the answer: the order must be the old order "
    "with the source moved to `to` (each track compared by the type, record range, source range, "
    "source_name, name and effect types of its segments, leaving out the Gap(s) at the end of a "
    "track, because a new track is one Gap as long as the sequence), else FLAME_API_ERROR (the "
    "sequence is still revertible); nothing in the sequence changed = SILENT_NOOP. "
    "result: moved {from, to, k}, stage, before / after = {tracks [{track, segmentCount, "
    "mediaCount, firstSources}]}, verification {ok, expectedOrder, problems}, clip {type, name, "
    "uid, duration}, cleanup, attributes, audio, display, stash {library, reel}, libraryCreated, "
    "reelCreated, returned {copy, overwrite, delete}, changed, notices, warnings. Segment "
    "connections are kept by Flame (measured) and journal.revert restores this sequence only. "
    "The op deletes a track (the source), so its effect is destructive; the checkpoint makes it "
    "revertible. " + _CHECKPOINTED_AUDIO
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


def _delete_to_python(args: Mapping[str, Any]) -> str:
    segments = [int(frame) for frame in args["segments"]]
    tail = (
        f"seq, {int(args.get('version', 0))!r}, {int(args.get('track', 0))!r}, {segments!r}, "
        f"{args.get('expect_source_names')!r}, {bool(args.get('ripple', True))!r}"
    )
    return _phase_code(
        args,
        f"_td_delete_check({tail})",
        f"_td_delete({tail}, {checkpoint_library_name()!r}, {stash_reel_name()!r})",
        selectors.resolve_code(args["sequence"], "seq"),
    )


def _move_to_python(args: Mapping[str, Any]) -> str:
    tail = f"seq, {int(args.get('version', 0))!r}, {int(args['track'])!r}, {int(args['to'])!r}"
    return _phase_code(
        args,
        f"_tm_check({tail})",
        f"_tm_move({tail}, {checkpoint_library_name()!r}, {stash_reel_name()!r})",
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


def _move_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    if args.get("to") == args.get("track"):
        return Refusal("INVALID_ARGS", "to is the track itself: the track would not move")
    return None


def _delete_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    if "track" not in args:
        return Refusal("INVALID_ARGS", "needs track (the index of the track to delete from)")
    segments = args.get("segments")
    if not isinstance(segments, list) or not segments:
        return Refusal("INVALID_ARGS", "segments must hold at least one record_in frame")
    if len(segments) > MAX_DELETE_SEGMENTS:
        return Refusal(
            "INVALID_ARGS",
            f"segments holds {len(segments)} record_in frames, at most {MAX_DELETE_SEGMENTS} "
            "are taken in one call",
        )
    bad = [
        frame
        for frame in segments
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0
    ]
    if bad:
        return Refusal(
            "INVALID_ARGS", f"segments must be non-negative integers (record_in frames), got {bad}"
        )
    repeated = sorted({frame for frame in segments if segments.count(frame) > 1})
    if repeated:
        return Refusal(
            "INVALID_ARGS", f"segments lists the same record_in more than once: {repeated}"
        )
    names = args.get("expect_source_names")
    if names is not None and len(names) != len(segments):
        return Refusal(
            "INVALID_ARGS",
            f"expect_source_names holds {len(names)} name(s) for {len(segments)} segment(s): it "
            "is parallel to segments",
        )
    return None


_AUDIO_REQUIRES = ("PySequence.audio_tracks", "PyAudioTrack.channels")
# What the audio snapshot of `_tb_audio_read` reads. The segments of a channel are reachable only
# that way, but `PyAudioChannel.segments` is not in the API symbol list.
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
            requires=(requires, *_PUT_REQUIRES, *_AUDIO_REQUIRES),
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
            requires=("PyTrack.cut", "PySequence.cut", *_PUT_REQUIRES, *_AUDIO_REQUIRES),
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
    registry.register(
        OperationSpec(
            name=DELETE_SEGMENTS_OP,
            category="timeline",
            description=_DELETE_SEGMENTS_DESCRIPTION,
            effect="destructive",
            undo="checkpoint",
            params=(
                _sequence_param(),
                OperationParam(
                    name="segments",
                    type="array",
                    description=(
                        "The record_in frames (non-negative integers, absolute: recordIn.frame of "
                        f"timeline.list_segments) of the segments to delete, at most "
                        f"{MAX_DELETE_SEGMENTS}, no duplicates. A Gap can be named too."
                    ),
                    required=True,
                    item_type="number",
                ),
                _index_param("version", "version (default 0)"),
                _index_param("track", "track of that version to delete from", default=False),
                OperationParam(
                    name="ripple",
                    type="boolean",
                    description=(
                        "Close the hole: the later segments of this track move left (the other "
                        "tracks do not follow). false calls lift_selection_to_media_panel "
                        "instead: it "
                        "leaves a Gap of the same length where the segment was and moves "
                        "nothing (measured on Flame 2025.2.4). Default true."
                    ),
                    required=False,
                    default=True,
                ),
                OperationParam(
                    name="expect_source_names",
                    type="array",
                    description=(
                        "A safeguard: the source_name each segment must have, in the order of "
                        "segments (as timeline.list_segments shows it, a Gap's too). A "
                        "different one is a CONFLICT and nothing is done."
                    ),
                    required=False,
                    item_type="string",
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_delete_to_python,
            undo_steps=no_undo_steps,
            precheck=_delete_precheck,
            sample_args={
                "sequence": {"kind": "wiretap", "id": "sample"},
                "segments": [0],
                "version": 0,
                "track": 0,
                "ripple": True,
            },
            checkpoint=_checkpoint(
                DELETE_SEGMENTS_OP,
                lambda args: f"before timeline.delete_segments ({len(args['segments'])})",
            ),
            requires=(
                "PySequence.extract_selection_to_media_panel",
                "PySequence.lift_selection_to_media_panel",
                "PySegment.selected",
                "PySequence.selected_segments",
                "PySequence.selected_transitions",
                "PySequence.selected_markers",
                "PyTrack.selected_transitions",
                "PyAudioTrack.selected_transitions",
                "PySegment.connected_segments",
                "PySegment.name",
                "PyWorkspace.libraries",
                "PyWorkspace.create_library",
                "PyLibrary.reels",
                "PyLibrary.create_reel",
                "PyReel.children",
                "PyTimeline.clip",
                "flame.delete",
                "flame.find_by_uid",
                *_PUT_REQUIRES,
                *_AUDIO_REQUIRES,
            ),
        )
    )
    registry.register(
        OperationSpec(
            name=MOVE_TRACK_OP,
            category="timeline",
            description=_MOVE_TRACK_DESCRIPTION,
            effect="destructive",
            undo="checkpoint",
            params=(
                _sequence_param(),
                OperationParam(
                    name="track",
                    type="number",
                    description="Index of the track to move (0 is the bottom track).",
                    required=True,
                    integer=True,
                    minimum=0,
                ),
                OperationParam(
                    name="to",
                    type="number",
                    description=(
                        "The index the track must have afterwards (0 is the bottom, the track "
                        "count minus 1 the top); not the same as `track`."
                    ),
                    required=True,
                    integer=True,
                    minimum=0,
                ),
                _index_param("version", "version (default 0)"),
            ),
            accepts=_ACCEPTS,
            to_python=_move_to_python,
            undo_steps=no_undo_steps,
            precheck=_move_precheck,
            sample_args={
                "sequence": {"kind": "wiretap", "id": "sample"},
                "track": 1,
                "to": 0,
            },
            checkpoint=_checkpoint(
                MOVE_TRACK_OP,
                lambda args: f"before timeline.move_track ({args['track']} to {args['to']})",
            ),
            requires=(
                "PyTrack.copy_to_media_panel",
                "PyTrack.transitions",
                "PyTrack.stereo_linked",
                "PyTrack.hidden",
                "PyTrack.locked",
                "PyTrack.name",
                "PyVersion.create_track",
                "PyWorkspace.libraries",
                "PyWorkspace.create_library",
                "PyLibrary.reels",
                "PyLibrary.create_reel",
                "PyReel.children",
                "PyTimeline.clip",
                "flame.delete",
                "flame.find_by_uid",
                *_PUT_REQUIRES,
                *_AUDIO_REQUIRES,
            ),
        )
    )
