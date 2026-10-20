"""`timeline.shorten`: make a sequence shorter by cutting every track at the new end and removing
what is after it. Behind a Timeline checkpoint like `timeline.delete_segments`
(`CheckpointSpec(target_kind="timeline")`). The Flame side is `timeline_shorten_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._segment_edit import no_undo_steps
from flame_mcp.ops.timeline_build import (
    _ACCEPTS,
    _AUDIO_JUDGED,
    _AUDIO_REQUIRES,
    _CHECKPOINTED_AUDIO,
    _PUT_REQUIRES,
    _checkpoint,
    _phase_code,
    _sequence_param,
)
from flame_mcp.registry import Registry
from flame_mcp.timeline_checkpoints import checkpoint_library_name, stash_reel_name
from flame_mcp.types import OperationParam, OperationSpec

SHORTEN_OP: Final[str] = "timeline.shorten"

_DESCRIPTION = (
    "Shorten a sequence to a new length: every video track and every audio channel is cut at the "
    "new end and everything after it is removed. "
    "`duration` is the new length in frames (an integer, 1 or more, shorter than the sequence). "
    "The end is start_time.frame + duration, the same absolute numbering as the recordIn.frame of "
    "timeline.list_segments. A sequence whose start_time is not 0 is run only "
    "when the first record_in of the video tracks and of every audio channel is start_time. "
    "How it works: (1) every video track of version 0 and every channel of every audio track that "
    "has a segment with record_in < end <= record_out is cut at `end` (PyTrack.cut on a video "
    "track and on an audio channel -- the channel object is a PyTrack too, measured on Flame "
    "2025.2.4 -- `end` becomes the first frame of the right part); (2) every segment with "
    "record_in >= end -- video Gaps, video media, audio segments -- gets PySegment.selected = True "
    "and nothing else is selected; (3) one PySequence.extract_selection_to_media_panel with the "
    "stash reel of the Timeline checkpoints as the destination removes them, and each track closes "
    "up on its own; (4) the clip that call makes there is a by-product: it is found as the one new "
    "entry of the reel and deleted again (flame.delete), and nothing else is ever deleted "
    "(result.extracted, result.cleanup); (5) the selection that was there is put back as far as "
    "the segments can be followed (result.selection); a selected segment that the extract removed "
    "is not put back and is only counted (selection.dropped), so no similar segment is selected "
    "in its place. "
    "Measured on Flame 2025.2.4: a sequence of 720 frames with two audio channels and "
    "video tracks that ended in a Gap became 360 frames long this way -- the Gaps at the end of "
    "every video track and the second half of the audio were selected and one extract was made; "
    "the audio stayed Audio Segments, and it was the audio that had decided the length of the "
    "sequence. PyTrack.cut cuts a Gap, and the audio channel took cut as well. Audio segments can "
    "be read only as PySequence.audio_tracks[].channels[].segments; PySegment.trim_tail on an "
    "audio segment turns it into a Gap and smart_replace_media on one crashes Flame, so neither "
    "is used. "
    "Refused before anything is copied or journaled (PRECONDITION_UNMET): a "
    "sequence with two or more versions; a boundary at `end` whose transition is not a Cut of "
    "duration 0, or cannot be read (PyTrack.transitions lists a Cut of duration 0 at every "
    "segment boundary, measured; the same is asked of an audio channel that has a boundary at "
    "`end`); a stereo_linked video track; audio that cannot be read completely; a start_time that "
    "is not the first record_in of the video tracks, or of any audio channel that has a segment; "
    "a selected transition "
    "or marker (an extract acts on the whole selection). "
    "Cutting a video media segment at `end` relies on timeline.cut. A transition that starts "
    "before `end` and reaches past it is not looked for. "
    "Measured on Flame 2025.2.4 and 2026.2.3 on a video track that ends in media: a "
    "sequence of 72 frames became 60 frames long, that track ended at the new end with no Gap, a "
    "track of clips and a Gap kept its content, both audio channels were one Audio Segment up "
    "to the new end, and journal.revert put the fingerprint, the video rows, the audio rows and "
    "the length 72 back. On a sequence whose video tracks ended in a Gap and whose audio crossed "
    "the new end (72 frames, 36 asked) journal.revert brought the length, the video rows and the "
    "audio rows back too. If a Gap is nevertheless left at the end of a track the length is "
    "wrong and the result is FLAME_API_ERROR (the sequence is still revertible). "
    "Also refused up front: a duration that is not shorter than the sequence (INVALID_ARGS: this "
    "op never lengthens), a sequence inside a library or a Batch group (PRECONDITION_UNMET), a "
    "sequence with nothing at or after the end to remove (PRECONDITION_UNMET), a "
    "clip (SELECTOR_TYPE). "
    "Judged by reading everything back, never by the answer: the sequence must be `duration` "
    "frames long, every segment of every video track and audio channel that ended before `end` "
    "must be as it was (type, record range, source range, source_name, name and effect types for "
    "video), and a segment that crossed `end` must be left ending at end - 1 with the same "
    "source_in (its source_out is not compared), else FLAME_API_ERROR (the sequence is still "
    "revertible); nothing in the sequence changed = SILENT_NOOP (audio included). A Gap is "
    "judged by type, record range and effect types only: measured on Flame 2025.2.4, "
    "a Gap that crossed `end` comes out of the cut with its source_in counted from "
    "0 again and its name '' (it was 'untitled'), so neither is compared for a Gap, and the "
    "same-source_in condition above does not apply to one. "
    "result: duration (asked), end, startFrame, before / after = {duration, startFrame}, tracks "
    "[{media 'video' | 'audio', track, channel (audio only), cut, removed (segments removed, the "
    "cut-off right part of a crossing segment included)}], removedMedia [{media, track, channel, "
    "sourceName, recordIn, recordOut}] (the media segments, video and audio, that are gone; at "
    "most 20, removedMediaCount / removedMediaTruncated say how many there were), extracted, "
    "cleanup, selection {before, dropped, changed, after, error, beforeCuts}, display, stash "
    "{library, "
    "reel}, libraryCreated, reelCreated, returned, changed, audio, notices, warnings. "
    + _AUDIO_JUDGED
    + "The op deletes the tail of the sequence, so its effect is destructive; the checkpoint makes "
    "it revertible, and journal.revert restores the video and, measured as above, the audio "
    "rows. " + _CHECKPOINTED_AUDIO
)


def _shorten_to_python(args: Mapping[str, Any]) -> str:
    tail = f"seq, {int(args['duration'])!r}"
    return _phase_code(
        args,
        f"_ts_check({tail})",
        f"_ts_shorten({tail}, {checkpoint_library_name()!r}, {stash_reel_name()!r})",
        selectors.resolve_code(args["sequence"], "seq"),
    )


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=SHORTEN_OP,
            category="timeline",
            description=_DESCRIPTION,
            effect="destructive",
            undo="checkpoint",
            params=(
                _sequence_param(),
                OperationParam(
                    name="duration",
                    type="number",
                    description=(
                        "The new length of the sequence in frames (an integer, 1 or more, "
                        "shorter than the sequence now)."
                    ),
                    required=True,
                    integer=True,
                    minimum=1,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_shorten_to_python,
            undo_steps=no_undo_steps,
            sample_args={
                "sequence": {"kind": "wiretap", "id": "sample"},
                "duration": 1,
            },
            checkpoint=_checkpoint(
                SHORTEN_OP, lambda args: f"before timeline.shorten (to {args['duration']})"
            ),
            requires=(
                "PyTrack.cut",
                "PyTrack.transitions",
                "PyTrack.stereo_linked",
                "PySequence.extract_selection_to_media_panel",
                "PySegment.selected",
                "PySequence.selected_segments",
                "PySequence.selected_transitions",
                "PySequence.selected_markers",
                "PyTrack.selected_transitions",
                "PyAudioTrack.selected_transitions",
                "PySequence.start_time",
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
