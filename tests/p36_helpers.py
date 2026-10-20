"""Fake-Flame helpers for the editing e2e (scripts/p3_6_edit_e2e.py): a clip with sound and the
sequences made from it, the way the rig of tests/p33_helpers.py makes the silent ones."""

from __future__ import annotations

from typing import Any

import pytest

import fake_media_tree
import fake_timeline_build as ftb
from fake_media_tree import PyAudioChannel, PyAudioTrack, PySegment, PySequence, adopt
from fake_segment_edit import clip_media
from flame_mcp import transport
from journal_test_helpers import bridge_result
from p33_helpers import fake_make_sequence, import_with_media
from timeline_p32a_helpers import gap

AV_LENGTH = 72
FAKE_MAKE_SEQ_WITH_SOUND = """
import p36_helpers as _h
_reel = flame.find_by_wiretap_node_id(@REEL@)
_result = _h.fake_make_sequence_with_sound(flame, _reel, @NAME@, @PUTS@, @TRIMS@, @FXS@)
"""


def sound_segment(clip_name: str, length: int) -> PySegment:
    return PySegment(
        name=clip_name,
        source_name=clip_name,
        record_in=0,
        record_out=length - 1,
        source_in=0,
        source_out=length - 1,
        record_duration=length,
        type="Audio Segment",
    )


def import_with_sound(fake_cls: Any) -> Any:
    """`import_with_media` that also gives the audio clip its two audio channels."""
    with_media = import_with_media(fake_cls)

    def import_clips(self: Any, path: Any, destination: Any) -> list[Any]:
        made: list[Any] = with_media(self, path, destination)
        for clip in made:
            name = str(clip.name.get_value())
            if name == "p2_av72":
                clip.audio_tracks = [
                    PyAudioTrack(
                        channels=[
                            PyAudioChannel([sound_segment(name, AV_LENGTH)]),
                            PyAudioChannel([sound_segment(name, AV_LENGTH)]),
                        ]
                    )
                ]
        return made

    return import_clips


def fill_with_gaps(seq: PySequence) -> None:
    """A real Flame has no hole in a video track: a Gap lies between two puts and a track that
    ends short of the sequence has a Gap at its end (measured on the probe sequences,
    where the Gap at the end of every track follows the length of the sequence). The fake
    `fake_make_sequence` puts the clips down without them."""
    length = max(
        s.record_out.frame + 1 for v in seq._versions for t in v._tracks for s in t._segments
    )
    for version in seq._versions:
        for track in version._tracks:
            filled: list[PySegment] = []
            cursor = 0
            for seg in track._segments:
                if seg.record_in.frame > cursor:
                    filled.append(gap(cursor, seg.record_in.frame - 1))
                filled.append(seg)
                cursor = seg.record_out.frame + 1
            if track._segments and cursor < length:
                filled.append(gap(cursor, length - 1))
            track._segments[:] = filled
            adopt(track, filled)
    ftb._refresh(seq)


def fake_make_sequence_with_sound(
    flame: Any, reel: Any, name: str, puts: list[list[Any]], trims: Any, fxs: Any
) -> dict[str, Any]:
    """`fake_make_sequence`, and the audio of the clips put when one of them has sound: the
    sequence gets one audio track of two channels, and an overwrite of a clip with audio onto
    video track 0 lays its audio down (measured: only on track 0; a clip put on another
    track brings no audio). A sequence of silent clips only has no audio track here, because the
    fake's `media_panel.copy` does not copy one."""
    result = fake_make_sequence(flame, reel, name, puts, trims, fxs)
    sounds = [
        clip
        for clip in (flame.find_by_wiretap_node_id(wt) for track, wt, _rel in puts if track == 0)
        if getattr(clip, "audio_tracks", None)
    ]
    seq = flame.find_by_wiretap_node_id(result["wt"])
    if not sounds:
        fill_with_gaps(seq)
        return result
    seq.audio_tracks = [PyAudioTrack(channels=[PyAudioChannel(), PyAudioChannel()])]
    for track, clip_wt, rel in puts:
        clip = flame.find_by_wiretap_node_id(clip_wt)
        if track == 0 and clip in sounds:
            _first, length = clip_media(clip)
            ftb._put_audio(seq, clip, rel - 1, length, insert=False)
    ftb._refresh(seq)
    return result


def lift_drops_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A switch for the tests: a lift also takes the audio off the sequence (what the run must
    notice, because the measured case of timeline.shorten cannot be built then)."""
    real = PySequence.lift_selection_to_media_panel

    def lift(self: Any, destination: Any = None, duplicate_action: str = "add") -> Any:
        made = real(self, destination, duplicate_action)
        for track in getattr(self, "audio_tracks", ()):
            for channel in track.channels:
                channel._segments.clear()
        ftb._refresh(self)
        return made

    monkeypatch.setattr(PySequence, "lift_selection_to_media_panel", lift)


def extract_keeps_every_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """A switch for the tests: an extract that does not make the Gap at the end of the tracks (and
    the length of the sequence) follow the longest content (not what Flame does), which the
    delete stage must notice."""
    import fake_selection_connections as fsc

    monkeypatch.setattr(fsc, "_fit_lengths", lambda seq, selected, floor: None)


def full_reads_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A switch for the tests: the transport read of the e2e (label `p36e_full`) fails every time,
    the way a bridge that does not answer would."""
    real = transport.exec

    def failing(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("label") == "p36e_full":
            return bridge_result(ok=False, error="read failed", error_code="FLAME_API_ERROR")
        return real(*args, **kwargs)

    monkeypatch.setattr(transport, "exec", failing)


def move_touches_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A switch for the tests: overwriting a copied track also adds an audio segment (INVENTED,
    never seen on Flame), so moving the track that holds the audio clip changes the audio, which
    the real run measured not to happen."""
    monkeypatch.setattr(ftb, "TRACK_COPY_TOUCHES_AUDIO", True)


def copy_drops_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A switch for the tests: `media_panel.copy` makes a copy without the audio tracks, so a
    restore from a checkpoint brings no audio back and a copy of a sequence has none. The real
    run measured the opposite (a revert put the audio rows back and a pair of copies
    showed the audio cut)."""
    real = fake_media_tree.copy_media

    def without_audio(*args: Any, **kwargs: Any) -> Any:
        made = real(*args, **kwargs)
        made.audio_tracks = []
        return made

    monkeypatch.setattr(fake_media_tree, "copy_media", without_audio)
