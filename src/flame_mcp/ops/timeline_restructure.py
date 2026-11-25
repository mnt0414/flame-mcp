"""`timeline.delete_segments` and `timeline.move_track`: the two ops of the timeline build family
that take a track apart (delete segments, rebuild a track at another position). Same Timeline
checkpoint as `timeline_build.py`, whose helpers they share (as `timeline_shorten.py` does); the
Flame side is `timeline_delete_prelude` and `timeline_move_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._segment_edit import CHECK_PHASES
from flame_mcp.ops.timeline_build import (
    ACCEPTS,
    AUDIO_JUDGED,
    AUDIO_REQUIRES,
    CHECKPOINTED_AUDIO,
    PUT_REQUIRES,
    checkpoint_spec,
    index_param,
    phase_code,
    sequence_param,
)
from flame_mcp.registry import Registry
from flame_mcp.timeline_checkpoints import checkpoint_library_name, stash_reel_name
from flame_mcp.types import OperationParam, OperationSpec, Refusal

DELETE_SEGMENTS_OP: Final[str] = "timeline.delete_segments"
MOVE_TRACK_OP: Final[str] = "timeline.move_track"
MAX_DELETE_SEGMENTS: Final[int] = 100

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
    + AUDIO_JUDGED
    + "A removed segment that has other connected segments "
    "(PySegment.connected_segments() minus the segment itself, see segment.connections) adds a "
    "notice with their count; a segment with no connection adds none. The op reads the counts "
    "and changes nothing else about them. Measured on Flame 2025.2.4, with the op run on a copy "
    "whose segments were connected to an original: the original sequence was unchanged "
    "afterwards (segment count and positions read back), also after the op deleted its "
    "by-product clips. "
    "journal.revert restores this sequence only. " + CHECKPOINTED_AUDIO
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
    "revertible. " + CHECKPOINTED_AUDIO
)


def _delete_to_python(args: Mapping[str, Any]) -> str:
    segments = [int(frame) for frame in args["segments"]]
    tail = (
        f"seq, {int(args.get('version', 0))!r}, {int(args.get('track', 0))!r}, {segments!r}, "
        f"{args.get('expect_source_names')!r}, {bool(args.get('ripple', True))!r}"
    )
    return phase_code(
        args,
        f"_td_delete_check({tail})",
        f"_td_delete({tail}, {checkpoint_library_name()!r}, {stash_reel_name()!r})",
        selectors.resolve_code(args["sequence"], "seq"),
    )


def _move_to_python(args: Mapping[str, Any]) -> str:
    tail = f"seq, {int(args.get('version', 0))!r}, {int(args['track'])!r}, {int(args['to'])!r}"
    return phase_code(
        args,
        f"_tm_check({tail})",
        f"_tm_move({tail}, {checkpoint_library_name()!r}, {stash_reel_name()!r})",
        selectors.resolve_code(args["sequence"], "seq"),
    )


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


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=DELETE_SEGMENTS_OP,
            brief_paths=("version", "track", "shifted"),
            category="timeline",
            description=_DELETE_SEGMENTS_DESCRIPTION,
            effect="destructive",
            undo="checkpoint",
            params=(
                sequence_param(),
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
                index_param("version", "version (default 0)"),
                index_param("track", "track of that version to delete from", default=False),
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
            accepts=ACCEPTS,
            to_python=_delete_to_python,
            precheck=_delete_precheck,
            sample_args={
                "sequence": {"kind": "wiretap", "id": "sample"},
                "segments": [0],
                "version": 0,
                "track": 0,
                "ripple": True,
            },
            phases=CHECK_PHASES,
            checkpoint=checkpoint_spec(
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
                *PUT_REQUIRES,
                *AUDIO_REQUIRES,
            ),
        )
    )
    registry.register(
        OperationSpec(
            name=MOVE_TRACK_OP,
            brief_paths=("moved",),
            category="timeline",
            description=_MOVE_TRACK_DESCRIPTION,
            effect="destructive",
            undo="checkpoint",
            params=(
                sequence_param(),
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
                index_param("version", "version (default 0)"),
            ),
            accepts=ACCEPTS,
            to_python=_move_to_python,
            precheck=_move_precheck,
            sample_args={
                "sequence": {"kind": "wiretap", "id": "sample"},
                "track": 1,
                "to": 0,
            },
            phases=CHECK_PHASES,
            checkpoint=checkpoint_spec(
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
                *PUT_REQUIRES,
                *AUDIO_REQUIRES,
            ),
        )
    )
