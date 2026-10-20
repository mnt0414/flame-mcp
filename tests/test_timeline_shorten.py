"""Tests `timeline.shorten`.

The new length is reached by cutting every video track and every audio channel at the new end
(`PyTrack.cut`, `PyAudioChannel.cut`), selecting everything at or after it and one
`extract_selection_to_media_panel`. The tests cover the cuts, the selection that is saved and put
back, the by-product of the extract and its clean-up, the readback that judges the result, the
refusals that leave nothing behind and the checkpoint. Run through the real Runtime with a scripted
`transport.exec` over the fake Flame tree. What the fake does follows the measurement of
the shortening (fake_timeline_build.py, fake_selection_connections.py); the rest is marked
INVENTED there."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fake_selection_connections as fsc
from fake_media_tree import (
    PyAudioChannel,
    PyAudioTrack,
    PySegment,
    PySequence,
    PyTime,
    PyTrack,
    PyTransition,
    PyVersion,
    adopt,
)
from journal_test_helpers import Rig
from p33_helpers import untouched_world
from timeline_p32a_helpers import (
    CLIPX,
    gap,
    library_copy,
    make_timeline_rig,
    media,
    rows,
    stash_entries,
    wt,
)

OP = "timeline.shorten"
SH_WT = "/wt/RG/Reel 1/SH"
SH = wt(SH_WT)
GAP = "Gap"
VIDEO = "Video Segment"
AUDIO = "Audio Segment"
LENGTH = 720
END = 360


def audio_seg(first: int, last: int, source: str = "aud") -> PySegment:
    return PySegment(
        name="AUD",
        source_name=source,
        record_in=first,
        record_out=last,
        source_in=first,
        source_out=last,
        record_duration=last - first + 1,
        type=AUDIO,
    )


def sh_tracks(base: int = 0) -> list[list[PySegment]]:
    """track 0: clipA base+0..299 | clipB base+300..719 (B is cut by END);
    track 1: one Gap (cut by END); track 2: Gap | clipC 100-199 | Gap 200..719."""
    return [
        [
            media("clipA", base, base + 299, name="A"),
            media("clipB", base + 300, base + 719, source_in=10, name="B"),
        ],
        [gap(base, base + 719)],
        [
            gap(base, base + 99),
            media("clipC", base + 100, base + 199, name="C"),
            gap(base + 200, base + 719),
        ],
    ]


def audio_track(base: int = 0, channels: int = 2) -> PyAudioTrack:
    return PyAudioTrack(
        channels=[PyAudioChannel([audio_seg(base, base + 719)]) for _ in range(channels)]
    )


def add_sh(rig: Rig, *, base: int = 0, audio: bool = True, versions: int = 1) -> PySequence:
    reel = rig.handles["reel"]
    seq = PySequence(
        "SH",
        wiretap_id=SH_WT,
        duration=LENGTH,
        start_time=PyTime(base),
        versions=[PyVersion([PyTrack(t) for t in sh_tracks(base)]) for _ in range(versions)],
    )
    if audio:
        track = audio_track(base)
        track.parent = seq
        seq.audio_tracks = [track]
    adopt(reel, [seq])
    reel._children.append(seq)
    rig.handles["sh"] = seq
    return seq


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    r = make_timeline_rig(monkeypatch, tmp_path)
    add_sh(r)
    return r


def shorten(rig: Rig, duration: int = END, sequence: dict[str, Any] = SH, **extra: Any) -> Any:
    return rig.do(OP, {"sequence": sequence, "duration": duration, **extra})


def layout(seq: PySequence, track: int = 0) -> list[list[Any]]:
    return [r[:3] for r in rows(seq, track)]


def audio_layout(seq: PySequence) -> list[list[Any]]:
    return [
        [ai, ci, s.type, s.record_in.frame, s.record_out.frame]
        for ai, track in enumerate(seq.audio_tracks)
        for ci, channel in enumerate(track.channels)
        for s in channel.segments
    ]


def selected(seq: PySequence) -> list[Any]:
    return [s for s in seq.selected_segments.get_value()]


def test_a_sequence_is_shortened_by_cutting_every_track_and_extracting_the_tail(rig: Rig) -> None:
    seq = rig.handles["sh"]
    env = shorten(rig)
    assert env["ok"] is True, env
    assert layout(seq, 0) == [[VIDEO, 0, 299], [VIDEO, 300, 359]]
    assert layout(seq, 1) == [[GAP, 0, 359]]
    assert layout(seq, 2) == [[GAP, 0, 99], [VIDEO, 100, 199], [GAP, 200, 359]]
    assert audio_layout(seq) == [[0, 0, AUDIO, 0, 359], [0, 1, AUDIO, 0, 359]]
    assert seq.duration.frame == END
    result = env["result"]
    assert result["end"] == END and result["startFrame"] == 0
    assert result["before"]["duration"] == LENGTH and result["after"]["duration"] == END
    assert result["changed"] is True and result["warnings"] == []


def test_the_piece_that_stays_keeps_its_source_in_and_ends_at_end_minus_one(rig: Rig) -> None:
    seq = rig.handles["sh"]
    assert shorten(rig)["ok"] is True
    kept = seq.versions[0].tracks[0].segments[1]
    assert (kept.record_in.frame, kept.record_out.frame, kept.source_in.frame) == (300, 359, 10)
    assert kept.source_name == "clipB"


def test_the_cuts_and_the_removals_are_reported_per_track_and_channel(rig: Rig) -> None:
    result = shorten(rig)["result"]
    by_track = {(t["media"], t["track"], t.get("channel")): t for t in result["tracks"]}
    assert by_track[("video", 0, None)] == {
        "media": "video",
        "track": 0,
        "channel": None,
        "cut": True,
        "removed": 1,
    }
    assert (
        by_track[("video", 1, None)]["cut"] is True and by_track[("video", 1, None)]["removed"] == 1
    )
    assert by_track[("video", 2, None)]["removed"] == 1
    assert by_track[("audio", 0, 1)]["cut"] is True and by_track[("audio", 0, 1)]["removed"] == 1
    assert len(result["tracks"]) == 5


def test_the_media_that_goes_is_listed_with_its_place(rig: Rig) -> None:
    result = shorten(rig)["result"]
    removed = result["removedMedia"]
    assert {
        "media": "video",
        "track": 0,
        "channel": None,
        "sourceName": "clipB",
        "recordIn": 360,
        "recordOut": 719,
    } in removed
    assert {
        "media": "audio",
        "track": 0,
        "channel": 0,
        "sourceName": "aud",
        "recordIn": 360,
        "recordOut": 719,
    } in removed
    assert len(removed) == 3 and result["removedMediaCount"] == 3
    assert result["removedMediaTruncated"] is False


def test_the_list_of_removed_media_is_capped(rig: Rig) -> None:
    seq = rig.handles["sh"]
    many = [media("m", 400 + i * 10, 400 + i * 10 + 9, name=f"m{i}") for i in range(30)]
    track = PyTrack([media("head", 0, 399, name="H"), *many])
    seq._versions[0]._tracks.append(track)
    adopt(seq._versions[0], [track])
    result = shorten(rig, 300)["result"]
    assert result["removedMediaCount"] == 34 and len(result["removedMedia"]) == 20
    assert result["removedMediaTruncated"] is True


def test_the_start_frame_of_the_sequence_is_counted_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    seq = add_sh(r, base=86400)
    env = shorten(r)
    assert env["ok"] is True, env
    assert env["result"]["end"] == 86400 + END and env["result"]["startFrame"] == 86400
    assert layout(seq, 0) == [[VIDEO, 86400, 86699], [VIDEO, 86700, 86759]]
    assert audio_layout(seq)[0][3:] == [86400, 86759]
    assert seq.duration.frame == END


def test_the_audio_that_starts_at_start_time_is_shortened_like_the_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    seq = add_sh(r, base=86400)
    assert seq.audio_tracks[0].channels[0].segments[0].record_in.frame == 86400
    assert shorten(r)["ok"] is True


@pytest.mark.parametrize(("base", "audio_first"), [(86400, 0), (0, 10), (86400, 86410)])
def test_audio_that_does_not_start_at_start_time_is_refused_before_anything_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, base: int, audio_first: int
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    seq = add_sh(r, base=base)
    seq.audio_tracks[0].channels[1]._segments[:] = [audio_seg(audio_first, audio_first + 719)]
    env = shorten(r)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "audio track 0 channel 1" in env["error"] and "nothing was done" in env["error"]
    assert str(audio_first) in env["error"] and str(base) in env["error"]
    assert untouched_world(r)
    assert seq.duration.frame == LENGTH


def test_a_channel_without_segments_does_not_have_a_start_to_check(rig: Rig) -> None:
    rig.handles["sh"].audio_tracks[0].channels[1]._segments[:] = []
    assert shorten(rig)["ok"] is True


def test_a_track_that_has_a_boundary_at_end_is_not_cut(rig: Rig) -> None:
    seq = rig.handles["sh"]
    seq._versions[0]._tracks[0]._segments[:] = [
        media("clipA", 0, 359, name="A"),
        media("clipB", 360, 719, name="B"),
    ]
    env = shorten(rig)
    assert env["ok"] is True, env
    track = next(t for t in env["result"]["tracks"] if (t["media"], t["track"]) == ("video", 0))
    assert track["cut"] is False and track["removed"] == 1
    assert layout(seq, 0) == [[VIDEO, 0, 359]]


def test_a_track_that_is_already_shorter_is_left_alone(rig: Rig) -> None:
    seq = rig.handles["sh"]
    seq._versions[0]._tracks[1]._segments[:] = [gap(0, 199)]
    env = shorten(rig)
    assert env["ok"] is True, env
    assert layout(seq, 1) == [[GAP, 0, 199]]
    track = next(t for t in env["result"]["tracks"] if (t["media"], t["track"]) == ("video", 1))
    assert track["cut"] is False and track["removed"] == 0


def test_a_sequence_without_audio_is_shortened_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    seq = add_sh(r, audio=False)
    env = shorten(r)
    assert env["ok"] is True, env
    assert seq.duration.frame == END and env["result"]["audio"]["before"]["trackCount"] == 0


def test_the_audio_is_reported_before_and_after(rig: Rig) -> None:
    audio = shorten(rig)["result"]["audio"]
    assert audio["changed"] is True and audio["complete"] is True
    assert audio["before"]["segmentCount"] == 2 and audio["before"]["channelCount"] == 2
    assert audio["before"]["rows"][0][4:6] == [0, 719]
    assert audio["after"]["rows"][0][4:6] == [0, 359]


def test_the_by_product_is_found_and_deleted_and_the_stash_is_left_empty(rig: Rig) -> None:
    env = shorten(rig)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["cleanup"] == {
        "attempted": True,
        "deleted": True,
        "returned": True,
        "error": None,
    }
    assert result["extracted"]["name"].endswith(" extract")
    assert stash_entries(rig) == [] or all(
        not str(e.name.get_value()).endswith(" extract") for e in stash_entries(rig)
    )
    assert rig.flame.find_by_uid(result["extracted"]["uid"]) is None
    assert result["notices"] == []


def test_the_selection_the_user_had_is_put_back(rig: Rig) -> None:
    seq = rig.handles["sh"]
    channel = seq.audio_tracks[0].channels[0]
    channel._segments[:] = [audio_seg(0, 359), audio_seg(360, 719)]
    seq.versions[0].tracks[0].segments[0].selected = True
    channel.segments[0].selected = True
    env = shorten(rig)
    assert env["ok"] is True, env
    assert env["result"]["selection"]["error"] is None
    assert env["result"]["selection"]["beforeCuts"] == 2
    assert env["result"]["warnings"] == []
    assert seq.versions[0].tracks[0].segments[0].selected.get_value() is True
    assert [s.record_in.frame for s in selected(seq)] == [0, 0]
    assert all(not s._selected.get_value() for t in seq.versions[0].tracks[1:] for s in t.segments)


def test_a_selection_the_cuts_took_away_is_a_warning(rig: Rig) -> None:
    seq = rig.handles["sh"]
    seq.versions[0].tracks[0].segments[0].selected = True
    seq.audio_tracks[0].channels[0].segments[0].selected = True  # crosses the end: it is cut
    env = shorten(rig)
    assert env["ok"] is True, env
    assert (
        env["result"]["selection"]["beforeCuts"] == 2 and env["result"]["selection"]["before"] == 1
    )
    assert any("before the cuts" in w for w in env["result"]["warnings"])
    assert [s.record_in.frame for s in selected(seq)] == [0]


def test_the_targets_are_the_only_segments_selected_when_the_call_is_made(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = rig.handles["sh"]
    seq.versions[0].tracks[0].segments[0].selected = True
    seen: list[list[tuple[int, int]]] = []
    original = PySequence.extract_selection_to_media_panel

    def spy(self: PySequence, destination: Any = None, *a: Any) -> Any:
        seen.append(sorted((s.record_in.frame, s.record_out.frame) for s in selected(self)))
        return original(self, destination, *a)

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", spy)
    assert shorten(rig)["ok"] is True
    assert seen == [[(360, 719)] * 5]


def test_a_selected_transition_or_marker_refuses_it_before_anything_is_copied(rig: Rig) -> None:
    rig.handles["sh"]._selected_markers.append(object())
    env = shorten(rig)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "selected_markers" in env["error"]
    assert untouched_world(rig)


@pytest.mark.parametrize("duration", [LENGTH, LENGTH + 10])
def test_a_duration_that_does_not_shorten_is_refused_before_anything_is_copied(
    rig: Rig, duration: int
) -> None:
    before = [rows(rig.handles["sh"], t) for t in range(3)]
    env = shorten(rig, duration)
    assert env["errorCode"] == "INVALID_ARGS" and "does not lengthen" in env["error"]
    assert untouched_world(rig) and [rows(rig.handles["sh"], t) for t in range(3)] == before


@pytest.mark.parametrize("duration", [0, -5, 2.5, "x"])
def test_a_duration_that_is_not_a_positive_integer_is_invalid(rig: Rig, duration: Any) -> None:
    env = shorten(rig, duration)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert untouched_world(rig)


def test_a_sequence_with_two_versions_is_refused_before_anything_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    add_sh(r, versions=2)
    env = shorten(r)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "2 versions" in env["error"]
    assert "nothing was done" in env["error"] and untouched_world(r)


def set_transitions(track: PyTrack, values: list[Any]) -> None:
    track.transitions = values


@pytest.mark.parametrize(
    ("transition", "needle"),
    [
        (PyTransition("Dissolve", 12), "Dissolve"),
        (PyTransition("Cut", 4), "duration 4"),
    ],
)
def test_a_transition_at_the_end_boundary_that_is_not_a_cut_of_nothing_is_refused(
    rig: Rig, transition: PyTransition, needle: str
) -> None:
    seq = rig.handles["sh"]
    seq._versions[0]._tracks[0]._segments[:] = [
        media("clipA", 0, 359, name="A"),
        media("clipB", 360, 719, name="B"),
    ]
    set_transitions(seq._versions[0]._tracks[0], [transition])
    env = shorten(rig)
    assert env["errorCode"] == "PRECONDITION_UNMET" and needle in env["error"]
    assert "nothing was done" in env["error"] and untouched_world(rig)


def test_a_transition_elsewhere_does_not_matter(rig: Rig) -> None:
    seq = rig.handles["sh"]
    seq._versions[0]._tracks[2].transitions = [PyTransition("Dissolve", 12), PyTransition()]
    assert shorten(rig)["ok"] is True


def test_transitions_that_cannot_be_read_at_the_end_boundary_are_refused(rig: Rig) -> None:
    seq = rig.handles["sh"]
    track = seq._versions[0]._tracks[0]
    track._segments[:] = [media("clipA", 0, 359, name="A"), media("clipB", 360, 719, name="B")]

    class Unreadable(PyTrack):
        @property
        def transitions(self) -> Any:
            raise RuntimeError("scripted transitions failure")

        @transitions.setter
        def transitions(self, value: Any) -> None:
            pass

    bad = Unreadable(track._segments)
    seq._versions[0]._tracks[0] = bad
    adopt(seq._versions[0], [bad])
    env = shorten(rig)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "cannot be read" in env["error"] and "scripted transitions failure" in env["error"]
    assert untouched_world(rig)


def test_an_audio_boundary_at_end_with_a_dissolve_is_refused_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    seq = add_sh(r)
    channel = seq.audio_tracks[0].channels[1]
    channel._segments[:] = [audio_seg(0, 359), audio_seg(360, 719)]
    channel.transitions = [PyTransition("Dissolve", 6)]
    env = shorten(r)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "audio track 0 channel 1" in env["error"]
    assert untouched_world(r)


def test_a_stereo_linked_track_is_refused_before_anything_is_copied(rig: Rig) -> None:
    rig.handles["sh"]._versions[0]._tracks[1].stereo_linked.set_value(True)
    env = shorten(rig)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "stereo_linked" in env["error"]
    assert "nothing was done" in env["error"] and untouched_world(rig)


def test_a_sequence_in_a_library_and_a_clip_are_refused_before_anything_is_copied(
    rig: Rig,
) -> None:
    copy = library_copy(rig, "sh")
    before = rows(copy)
    in_library = shorten(rig, sequence=wt(copy.get_wiretap_node_id()))
    assert (
        in_library["errorCode"] == "PRECONDITION_UNMET"
        and "inside a library" in in_library["error"]
    )
    clip = shorten(rig, sequence=CLIPX)
    assert clip["errorCode"] == "SELECTOR_TYPE" and "not a PySequence" in clip["error"]
    assert rows(copy) == before and rig.records() == []


def test_audio_that_cannot_be_read_completely_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: PyAudioChannel) -> Any:
        raise RuntimeError("scripted audio failure")

    monkeypatch.setattr(PyAudioChannel, "segments", property(broken))
    env = shorten(rig)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "audio" in env["error"]
    assert untouched_world(rig)


def test_a_flame_whose_channel_has_no_cut_is_refused_up_front(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PyAudioChannel, "cut", None)
    env = shorten(rig)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "PyAudioChannel.cut" in env["error"]
    assert untouched_world(rig)


def test_a_flame_without_the_extract_is_refused_up_front(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", None)
    env = shorten(rig)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "extract_selection_to_media_panel" in env["error"] and untouched_world(rig)


def test_the_start_frame_that_disagrees_with_the_segments_is_refused(rig: Rig) -> None:
    rig.handles["sh"].start_time.set_value(PyTime(50))
    env = shorten(rig)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "start" in env["error"]
    assert untouched_world(rig)


def test_the_call_is_checkpointed_and_journaled(rig: Rig) -> None:
    env = shorten(rig)
    assert env["ok"] is True, env
    journal = env["journal"]
    assert journal["revert"]["op"] == "journal.revert" and journal["changed"] is True
    assert env["undoSteps"] == 0
    types = [r["type"] for r in rig.records()]
    assert "checkpoint" in types and "intent" in types and "outcome" in types


def test_shorten_reverts_the_video_to_the_checkpoint(rig: Rig) -> None:
    seq = rig.handles["sh"]
    before = [rows(seq, t) for t in range(3)]
    env = shorten(rig)
    assert env["ok"] is True, env
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    restored = rig.flame.find_by_wiretap_node_id(back["result"]["restored"]["selector"]["id"])
    assert [rows(restored, t) for t in range(3)] == before


def test_a_cut_that_raises_is_reported_and_the_sequence_stays_revertible(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explodes(self: PyTrack, time: Any) -> Any:
        raise RuntimeError("scripted cut failure")

    monkeypatch.setattr(PyTrack, "cut", explodes)
    env = shorten(rig)
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "cut" in env["error"] and "scripted cut failure" in env["error"]
    assert env["journal"]["revert"]["op"] == "journal.revert"
    assert selected(rig.handles["sh"]) == []


def test_a_cut_that_does_nothing_is_an_error_before_the_extract(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PyTrack, "cut", lambda self, time: None)
    called: list[int] = []
    original = PySequence.extract_selection_to_media_panel

    def spy(self: PySequence, *a: Any, **k: Any) -> Any:
        called.append(1)
        return original(self, *a, **k)

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", spy)
    env = shorten(rig)
    assert env["errorCode"] == "FLAME_API_ERROR" and "did not split" in env["error"]
    assert called == []


def test_an_extract_that_raises_is_reported_and_the_selection_is_put_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = rig.handles["sh"]
    seq.versions[0].tracks[0].segments[0].selected = True

    def explodes(self: PySequence, destination: Any = None, *a: Any) -> Any:
        raise RuntimeError("scripted extract failure")

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", explodes)
    env = shorten(rig)
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "extract_selection_to_media_panel() raised RuntimeError" in env["error"]
    assert "CHANGED" in env["error"]  # the cuts were made
    assert [s.record_in.frame for s in selected(seq)] == [0]


def test_an_extract_that_changes_nothing_when_no_cut_was_needed_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = rig.handles["sh"]
    for track in seq._versions[0]._tracks:
        track._segments[:] = [gap(0, 359), gap(360, 719)]
    for channel in seq.audio_tracks[0].channels:
        channel._segments[:] = [audio_seg(0, 359), audio_seg(360, 719)]
    monkeypatch.setattr(fsc, "take_selection", lambda seq, destination, *, ripple: None)
    env = shorten(rig)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "nothing in the sequence changed" in env["error"]
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_an_extract_that_does_nothing_after_the_cuts_is_a_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fsc, "take_selection", lambda seq, destination, *, ripple: None)
    env = shorten(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "duration" in env["error"] and "CHANGED" not in env["error"]
    assert selected(rig.handles["sh"]) == []


def test_an_extract_that_moves_what_stays_is_a_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsc.take_selection

    def also_moves_the_head(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        seq.versions[0].tracks[0].segments[0]._set_record(2, 301)
        return made

    monkeypatch.setattr(fsc, "take_selection", also_moves_the_head)
    env = shorten(rig)
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "video track 0" in env["error"] and "not the way" in env["error"]
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_an_audio_segment_that_stays_but_is_changed_is_a_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsc.take_selection

    def touches_audio(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        seq.audio_tracks[0].channels[1].segments[0].source_in = PyTime(7)
        return made

    monkeypatch.setattr(fsc, "take_selection", touches_audio)
    env = shorten(rig)
    assert env["errorCode"] == "FLAME_API_ERROR" and "audio track 0 channel 1" in env["error"]


def test_a_length_that_is_not_the_one_asked_for_is_a_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsc.take_selection

    def leaves_a_gap(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        seq.versions[0].tracks[1]._segments.append(PySegment._new_gap(360, 419))
        return made

    monkeypatch.setattr(fsc, "take_selection", leaves_a_gap)
    env = shorten(rig)
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "video track 1" in env["error"]


def test_a_by_product_that_cannot_be_deleted_is_a_warning_and_the_sequence_stays_short(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_media_tree import FakeFlame

    original = FakeFlame.delete

    def refuses(self: FakeFlame, obj: Any, confirm: bool = True) -> bool:
        if str(obj.name.get_value()).endswith(" extract"):
            raise RuntimeError("scripted delete failure")
        return original(self, obj, confirm)

    monkeypatch.setattr(FakeFlame, "delete", refuses)
    env = shorten(rig)
    assert env["ok"] is True, env
    assert env["result"]["cleanup"]["deleted"] is False
    assert any(w.startswith("by-product of the call:") for w in env["result"]["warnings"])
    assert rig.handles["sh"].duration.frame == END


def test_the_op_is_destructive_and_checkpointed(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None and op.effect == "destructive" and op.undo == "checkpoint"
    assert op.checkpoint is not None and op.checkpoint.target_kind == "timeline"
    assert len(op.description) > 0


def split_tail_selection_setup(seq: PySequence) -> tuple[PySegment, PySegment, PySegment]:
    """Track 1 holds two equal media segments (same key), the head at 0-99 and the tail at
    400-499; audio channel 0 is cut at END already. Selects the tail ones, video and audio, and
    track 0's first segment. Answers (head X, tail X, tail audio)."""
    track = seq.versions[0].tracks[1]
    track._segments[:] = [
        media("m", 0, 99, name="X"),
        gap(100, 399),
        media("m", 400, 499, name="X"),
        gap(500, 719),
    ]
    adopt(track, track._segments)
    channel = seq.audio_tracks[0].channels[0]
    channel._segments[:] = [audio_seg(0, 359), audio_seg(360, 719)]
    head, tail = track.segments[0], track.segments[2]
    tail_audio = channel.segments[1]
    tail.selected = True
    tail_audio.selected = True
    seq.versions[0].tracks[0].segments[0].selected = True
    return head, tail, tail_audio


def test_a_selection_on_what_is_removed_is_dropped_and_no_look_alike_is_selected(rig: Rig) -> None:
    seq = rig.handles["sh"]
    head, _, _ = split_tail_selection_setup(seq)
    env = shorten(rig)
    assert env["ok"] is True, env
    selection = env["result"]["selection"]
    assert head._selected.get_value() is False
    assert [(s.record_in.frame, s.source_name) for s in selected(seq)] == [(0, "clipA")]
    assert selection["dropped"] == 2 and selection["before"] == 3 and selection["error"] is None
    assert env["result"]["warnings"] == []


def test_a_selection_that_was_not_removed_is_not_dropped(rig: Rig) -> None:
    seq = rig.handles["sh"]
    seq.versions[0].tracks[0].segments[0].selected = True
    env = shorten(rig)
    assert env["ok"] is True, env
    assert env["result"]["selection"]["dropped"] == 0
    assert [s.record_in.frame for s in selected(seq)] == [0]


def test_a_tail_selection_comes_back_when_the_extract_removed_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = rig.handles["sh"]
    split_tail_selection_setup(seq)
    monkeypatch.setattr(fsc, "take_selection", lambda seq, destination, *, ripple: None)
    env = shorten(rig)
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["selection"]["dropped"] == 0
    assert sorted(s.record_in.frame for s in selected(seq)) == [0, 360, 400]


def test_audio_that_cannot_be_deselected_afterwards_is_a_warning_and_ok_stays(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = rig.handles["sh"]
    original = PySequence.extract_selection_to_media_panel

    def extract_then_break(self: PySequence, destination: Any = None, *a: Any) -> Any:
        made = original(self, destination, *a)

        def unreadable(seg: PySegment) -> Any:
            if seg.type == AUDIO:
                raise RuntimeError("scripted selected failure")
            return seg._selected

        monkeypatch.setattr(PySegment, "selected", property(unreadable))
        return made

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", extract_then_break)
    env = shorten(rig)
    assert env["ok"] is True, env
    lines = [w for w in env["result"]["warnings"] if "deselect" in w]
    assert len(lines) == 1 and "scripted selected failure" in lines[0]
    assert seq.duration.frame == END


def test_an_audio_channel_whose_transitions_raise_attribute_error_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    seq = add_sh(r)

    class NoTransitions(PyAudioChannel):
        @property
        def transitions(self) -> Any:
            raise AttributeError("scripted no transitions")

        @transitions.setter
        def transitions(self, value: Any) -> None:
            pass

    bad = NoTransitions([audio_seg(0, 359), audio_seg(360, 719)])
    bad.parent = seq.audio_tracks[0]
    seq.audio_tracks[0]._channels[1] = bad
    env = shorten(r)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "audio track 0 channel 1" in env["error"] and "scripted no transitions" in env["error"]
    assert untouched_world(r)


def test_audio_that_is_longer_than_the_video_and_sets_the_length_is_shortened(
    rig: Rig,
) -> None:
    seq = rig.handles["sh"]
    for track in seq._versions[0]._tracks:
        track._segments[-1]._set_record(track._segments[-1].record_in.frame, 599)
    assert seq.duration.frame == LENGTH
    env = shorten(rig)
    assert env["ok"] is True, env
    assert seq.duration.frame == END
    assert env["result"]["before"]["duration"] == LENGTH
    assert layout(seq, 0) == [[VIDEO, 0, 299], [VIDEO, 300, 359]]
    assert layout(seq, 1) == [[GAP, 0, 359]]
    assert audio_layout(seq) == [[0, 0, AUDIO, 0, 359], [0, 1, AUDIO, 0, 359]]


def test_a_cut_that_fails_on_the_second_video_track_only_is_reported_and_revertible(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = rig.handles["sh"]
    second = seq._versions[0]._tracks[1]
    original = PyTrack.cut

    def cut(self: PyTrack, time: Any) -> Any:
        if self is second:
            raise RuntimeError("scripted second track failure")
        return original(self, time)

    monkeypatch.setattr(PyTrack, "cut", cut)
    called: list[int] = []
    extract = PySequence.extract_selection_to_media_panel

    def spy(self: PySequence, *a: Any, **k: Any) -> Any:
        called.append(1)
        return extract(self, *a, **k)

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", spy)
    before = [rows(seq, t) for t in range(3)]
    env = shorten(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "video track 1" in env["error"] and "scripted second track failure" in env["error"]
    assert "CHANGED" in env["error"] and called == []
    assert env["result"]["changed"] is True
    assert env["journal"]["revert"]["op"] == "journal.revert"
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    restored = rig.flame.find_by_wiretap_node_id(back["result"]["restored"]["selector"]["id"])
    assert [rows(restored, t) for t in range(3)] == before


def test_a_cut_that_fails_on_one_audio_channel_only_is_reported_and_revertible(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = rig.handles["sh"]
    second = seq.audio_tracks[0].channels[1]
    original = PyAudioChannel.cut

    def cut(self: PyAudioChannel, time: Any) -> Any:
        if self is second:
            raise RuntimeError("scripted channel failure")
        return original(self, time)

    monkeypatch.setattr(PyAudioChannel, "cut", cut)
    env = shorten(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "audio track 0 channel 1" in env["error"] and "scripted channel failure" in env["error"]
    assert "CHANGED" in env["error"]
    assert env["journal"]["revert"]["op"] == "journal.revert"
    assert selected(seq) == []


def test_a_stash_reel_that_cannot_be_made_after_the_cuts_keeps_the_partial_result(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flame_mcp.timeline_checkpoints import checkpoint_library_name

    original = PyTrack.cut

    def cut_then_close_the_library(self: PyTrack, time: Any) -> Any:
        made = original(self, time)
        workspace = rig.flame.projects.current_project.current_workspace
        for lib in workspace.libraries:
            if lib.name.get_value() == checkpoint_library_name():
                lib.opened.set_value(False)
        return made

    monkeypatch.setattr(PyTrack, "cut", cut_then_close_the_library)
    env = rig.do(OP, {"sequence": SH, "duration": END})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "is closed" in env["error"] and "CHANGED" in env["error"]
    result = env["result"]
    assert result["changed"] is True and result["ran"] is False
    assert result["tracks"] is not None and result["after"]["duration"] == LENGTH
    assert result["audio"]["changed"] is True
    assert "nothing was removed" in env["error"]
    assert env["journal"]["revert"]["op"] == "journal.revert"
    assert rig.handles["sh"].versions[0].tracks[0].segments[1].record_out.frame == 359


def test_a_cut_gap_whose_source_range_and_name_were_restarted_is_a_good_shorten(rig: Rig) -> None:
    """Measured on Flame 2025.2.4: a Gap that crossed the end comes out of the cut
    with source_in counted from 0 again and name '' (it was record_in and 'untitled')."""
    seq = rig.handles["sh"]
    for ti in (1, 2):
        last = seq._versions[0]._tracks[ti]._segments[-1]
        assert last.type == GAP
        last.source_in = PyTime(last.record_in.frame)
        last.name.set_value("untitled")
    env = shorten(rig)
    assert env["ok"] is True, env
    for ti in (1, 2):
        kept = seq.versions[0].tracks[ti].segments[-1]
        assert kept.type == GAP and kept.record_out.frame == END - 1
        assert kept.source_in.frame == 0 and kept.name.get_value() == ""
    assert layout(seq, 2) == [[GAP, 0, 99], [VIDEO, 100, 199], [GAP, 200, 359]]


def test_a_gap_that_ends_in_the_wrong_place_is_still_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsc.take_selection

    def shrinks_a_gap(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        last = seq.versions[0].tracks[2]._segments[-1]
        last._set_record(last.record_in.frame, last.record_out.frame - 1)
        return made

    monkeypatch.setattr(fsc, "take_selection", shrinks_a_gap)
    env = shorten(rig)
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "video track 2 segment 2" in env["error"], env
