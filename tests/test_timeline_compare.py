"""Tests `timeline.compare`: two sequences compared by their edit (versions, video tracks of version
0, audio channels), record frames counted from each sequence's own start. Run through the real
Runtime with a scripted `transport.exec` over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import (
    PyAudioChannel,
    PyAudioTrack,
    PySegment,
    PySequence,
    PyTime,
    PyTimelineFX,
    PyTrack,
    PyTransition,
    PyVersion,
    adopt,
)
from journal_test_helpers import Rig
from timeline_p32a_helpers import CLIPX, gap, make_timeline_rig, media, wt

OP = "timeline.compare"
A_WT = "/wt/RG/Reel 1/CA"
B_WT = "/wt/RG/Reel 1/CB"
A = wt(A_WT)
B = wt(B_WT)


def audio_seg(first: int, last: int, source: str = "aud", name: str = "AUD") -> PySegment:
    return PySegment(
        name=name,
        source_name=source,
        record_in=first,
        record_out=last,
        source_in=0,
        source_out=last - first,
        record_duration=last - first + 1,
        type="Audio Segment",
    )


def tracks_at(base: int) -> list[list[PySegment]]:
    """track 0: A 0-99 | Gap 100-109 | B 110-199 (a Letterbox); track 1: C 0-199;
    track 2: Gap only; all moved by `base`."""
    return [
        [
            media("clipA", base, base + 99, name="A"),
            gap(base + 100, base + 109),
            media("clipB", base + 110, base + 199, name="B", effects=(PyTimelineFX("Blur"),)),
        ],
        [media("clipC", base, base + 199, name="C")],
        [gap(base, base + 199)],
    ]


def audio_at(base: int, channels: int = 2) -> PyAudioTrack:
    return PyAudioTrack(
        channels=[PyAudioChannel([audio_seg(base, base + 199)]) for _ in range(channels)]
    )


def add(
    rig: Rig,
    key: str,
    wiretap: str,
    *,
    base: int = 0,
    tracks: list[list[PySegment]] | None = None,
    audio: list[PyAudioTrack] | None = None,
    versions: int = 1,
    duration: int = 200,
) -> PySequence:
    reel = rig.handles["reel"]
    made = tracks if tracks is not None else tracks_at(base)
    seq = PySequence(
        key,
        wiretap_id=wiretap,
        duration=duration,
        start_time=PyTime(base),
        versions=[PyVersion([PyTrack(t) for t in made]) for _ in range(versions)],
    )
    tracks_of_audio = audio if audio is not None else [audio_at(base)]
    for track in tracks_of_audio:
        track.parent = seq
    seq.audio_tracks = list(tracks_of_audio)
    adopt(reel, [seq])
    reel._children.append(seq)
    rig.handles[key] = seq
    return seq


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    r = make_timeline_rig(monkeypatch, tmp_path)
    add(r, "ca", A_WT)
    add(r, "cb", B_WT)
    return r


def compare(rig: Rig, a: dict[str, Any] = A, b: dict[str, Any] = B, **extra: Any) -> Any:
    return rig.do(OP, {"a": a, "b": b, **extra})


def diffs(env: dict[str, Any]) -> list[tuple[str, str]]:
    return [(d["where"], d["field"]) for d in env["result"]["differences"]]


def test_two_sequences_with_the_same_edit_are_equal(rig: Rig) -> None:
    env = compare(rig)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["equal"] is True and result["differences"] == []
    assert result["differenceCount"] == 0 and result["truncated"] is False
    assert {s["where"] for s in result["summary"]} == {
        "sequence",
        "video track 0",
        "video track 1",
        "video track 2",
        "audio track 0",
        "audio track 0 channel 0",
        "audio track 0 channel 1",
    }
    assert all(s["equal"] is True and s["differences"] == 0 for s in result["summary"])


def test_a_sequence_is_equal_to_itself(rig: Rig) -> None:
    assert compare(rig, A, A)["result"]["equal"] is True


def test_the_start_timecode_does_not_matter_when_the_edit_is_the_same(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    add(r, "ca", A_WT, base=0)
    add(r, "cb", B_WT, base=86400)
    env = compare(r)
    assert env["ok"] is True, env
    assert env["result"]["equal"] is True, env["result"]["differences"]
    assert env["result"]["a"]["startFrame"] == 0 and env["result"]["b"]["startFrame"] == 86400


def test_audio_that_does_not_start_at_start_time_is_a_warning_and_is_still_compared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    add(r, "ca", A_WT, base=86400, audio=[audio_at(0)])
    add(r, "cb", B_WT, base=86400)
    env = compare(r)
    assert env["ok"] is True, env
    result = env["result"]
    warnings = result["warnings"]
    assert len(warnings) == 1 and "audio track 0 channel 0" in warnings[0]
    assert "a:" in warnings[0] and "counted from start_time" in warnings[0]
    assert ("audio track 0 channel 0 segment 0", "recordIn") in diffs(env)
    assert result["equal"] is False


def test_audio_that_starts_at_start_time_gives_no_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    add(r, "ca", A_WT, base=86400)
    add(r, "cb", B_WT, base=0)
    assert compare(r)["result"]["warnings"] == []


def test_the_frame_rate_and_the_size_are_compared_by_their_field_names(rig: Rig) -> None:
    seq = rig.handles["cb"]
    seq.frame_rate = "25 fps"
    seq.width = 3840
    seq.height = 2160
    env = compare(rig)
    assert env["ok"] is True, env
    found = {(d["where"], d["field"]): d for d in env["result"]["differences"]}
    assert found[("sequence", "frameRate")] == {
        "where": "sequence",
        "field": "frameRate",
        "a": "23.976 fps",
        "b": "25 fps",
    }
    assert found[("sequence", "width")]["a"] == 1920 and found[("sequence", "width")]["b"] == 3840
    assert found[("sequence", "height")]["b"] == 2160
    assert env["result"]["equal"] is False and env["result"]["warnings"] == []
    assert env["result"]["a"]["frameRate"] == "23.976 fps" and env["result"]["b"]["width"] == 3840


def test_a_frame_rate_or_size_that_cannot_be_read_is_a_warning(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: PySequence) -> Any:
        raise RuntimeError("scripted width failure")

    monkeypatch.setattr(PySequence, "width", property(broken), raising=False)
    env = compare(rig)
    assert env["ok"] is True, env
    warnings = env["result"]["warnings"]
    assert any(w.startswith("a:") and "width" in w and "cannot be read" in w for w in warnings)
    assert any(w.startswith("b:") and "width" in w and "cannot be read" in w for w in warnings)
    assert ("sequence", "width") not in diffs(env)
    assert env["result"]["equal"] is True


def test_a_difference_in_a_segment_names_the_place_the_field_and_both_values(rig: Rig) -> None:
    seg = rig.handles["cb"].versions[0].tracks[0].segments[2]
    seg._set_record(110, 189)
    seg.source_in = PyTime(5)
    seg.source_name = "other"
    seg.name.set_value("B2")
    seg.effects = [PyTimelineFX("Timewarp")]
    env = compare(rig)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["equal"] is False
    assert {
        "where": "video track 0 segment 2",
        "field": "recordOut",
        "a": 199,
        "b": 189,
    } in result["differences"]
    where = "video track 0 segment 2"
    assert {(where, f) for f in ("recordOut", "sourceIn", "sourceName", "name", "effects")} <= set(
        diffs(env)
    )
    effects = next(d for d in result["differences"] if d["field"] == "effects")
    assert effects["a"] == ["Blur"] and effects["b"] == ["Timewarp"]
    track = next(s for s in result["summary"] if s["where"] == "video track 0")
    assert track["equal"] is False and track["differences"] >= 5
    assert next(s for s in result["summary"] if s["where"] == "video track 1")["equal"] is True


def test_the_type_of_a_segment_is_compared(rig: Rig) -> None:
    rig.handles["cb"].versions[0].tracks[0]._segments[1] = media("clipG", 100, 109, name="G")
    env = compare(rig)
    assert ("video track 0 segment 1", "type") in diffs(env)
    assert ("video track 0 segment 1", "sourceName") in diffs(env)


def test_the_length_the_version_count_and_the_track_counts_are_compared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    add(r, "ca", A_WT)
    add(r, "cb", B_WT, versions=2, duration=250, audio=[audio_at(0), audio_at(0, channels=1)])
    env = compare(r)
    assert env["ok"] is True, env
    found = {(d["where"], d["field"]): d for d in env["result"]["differences"]}
    assert (
        found[("sequence", "duration")]["a"] == 200 and found[("sequence", "duration")]["b"] == 250
    )
    assert found[("sequence", "versionCount")]["a"] == 1
    assert found[("sequence", "versionCount")]["b"] == 2
    assert found[("sequence", "audioTrackCount")] == {
        "where": "sequence",
        "field": "audioTrackCount",
        "a": 1,
        "b": 2,
    }
    assert found[("audio track 1", "presence")]["a"] == "absent"


def test_only_version_zero_is_compared_for_the_tracks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = make_timeline_rig(monkeypatch, tmp_path)
    add(r, "ca", A_WT, versions=2)
    add(r, "cb", B_WT, versions=2)
    r.handles["cb"].versions[1].tracks[0]._segments[:] = [gap(0, 10)]
    env = compare(r)
    assert env["result"]["equal"] is True


def test_a_track_only_one_sequence_has_is_a_difference(rig: Rig) -> None:
    seq = rig.handles["cb"]
    extra = PyTrack([media("clipD", 0, 199, name="D")])
    seq._versions[0]._tracks.append(extra)
    adopt(seq._versions[0], [extra])
    env = compare(rig)
    result = env["result"]
    assert {
        "where": "video track 3",
        "field": "presence",
        "a": "absent",
        "b": "present (1 segment(s))",
    } in result["differences"]
    assert ("sequence", "videoTrackCount") in diffs(env)
    track = next(s for s in result["summary"] if s["where"] == "video track 3")
    assert track["equal"] is False


def test_a_track_with_a_different_segment_count_reports_the_extra_segments(rig: Rig) -> None:
    rig.handles["cb"].versions[0].tracks[1]._segments[:] = [
        media("clipC", 0, 99, name="C"),
        media("clipC", 100, 199, source_in=100, name="C"),
    ]
    env = compare(rig)
    found = {(d["where"], d["field"]): d for d in env["result"]["differences"]}
    assert found[("video track 1", "segmentCount")]["a"] == 1
    assert found[("video track 1", "segmentCount")]["b"] == 2
    assert found[("video track 1 segment 1", "presence")]["a"] == "absent"
    assert ("video track 1 segment 0", "recordOut") in found


def test_audio_segments_are_compared_by_type_ranges_and_source(rig: Rig) -> None:
    channel = rig.handles["cb"].audio_tracks[0].channels[1]
    channel._segments[:] = [audio_seg(0, 149, source="aud2"), audio_seg(150, 199)]
    env = compare(rig)
    found = {(d["where"], d["field"]): d for d in env["result"]["differences"]}
    where = "audio track 0 channel 1"
    assert found[(where, "segmentCount")] == {
        "where": where,
        "field": "segmentCount",
        "a": 1,
        "b": 2,
    }
    assert ("audio track 0 channel 1 segment 0", "recordOut") in found
    assert ("audio track 0 channel 1 segment 0", "sourceName") in found
    summary = {s["where"]: s for s in env["result"]["summary"]}
    assert summary[where]["equal"] is False
    assert summary["audio track 0 channel 0"]["equal"] is True


def test_the_channel_count_of_an_audio_track_is_compared(rig: Rig) -> None:
    rig.handles["cb"].audio_tracks[:] = [audio_at(0, channels=1)]
    env = compare(rig)
    found = {(d["where"], d["field"]): d for d in env["result"]["differences"]}
    assert found[("audio track 0", "channelCount")]["a"] == 2
    assert found[("audio track 0", "channelCount")]["b"] == 1
    assert found[("audio track 0 channel 1", "presence")]["b"] == "absent"


def test_what_the_op_says_it_does_not_compare_is_not_compared(rig: Rig) -> None:
    seq = rig.handles["cb"]
    seg = seq.versions[0].tracks[0].segments[2]
    seg.effects[0].bypass.set_value(True)  # an effect parameter
    seg.colour.set_value((1.0, 0.0, 0.0))  # a colour
    seg.selected_markers.set_value(["m"])  # a marker
    seq._versions[0]._tracks[0].transitions = [PyTransition("Dissolve", 10), PyTransition()]
    env = compare(rig)
    assert env["result"]["equal"] is True, env["result"]["differences"]


def test_the_differences_are_cut_at_the_limit_and_counted(rig: Rig) -> None:
    for track in rig.handles["cb"].versions[0].tracks[:2]:
        for seg in track._segments:
            seg.source_name = "changed"
    env = compare(rig, limit=2)
    result = env["result"]
    assert len(result["differences"]) == 2 and result["truncated"] is True
    assert result["differenceCount"] > 2 and result["equal"] is False
    full = compare(rig)["result"]
    assert full["truncated"] is False and len(full["differences"]) == full["differenceCount"]
    assert sum(s["differences"] for s in full["summary"]) == full["differenceCount"]


def test_the_limit_defaults_to_fifty(rig: Rig) -> None:
    seq = rig.handles["cb"]
    seq._versions[0]._tracks[1]._segments[:] = [
        media(f"s{i}", i, i, name=str(i)) for i in range(200)
    ]
    result = compare(rig)["result"]
    assert len(result["differences"]) == 50 and result["truncated"] is True


def test_empty_tracks_are_listed_for_each_side(rig: Rig) -> None:
    seq = rig.handles["cb"]
    seq._versions[0]._tracks[1]._segments[:] = [gap(0, 199)]
    result = compare(rig)["result"]
    assert result["emptyTracks"] == {"a": [2], "b": [1, 2]}
    assert compare(rig, B, A)["result"]["emptyTracks"] == {"a": [1, 2], "b": [2]}


def test_two_clips_can_be_compared_too(rig: Rig) -> None:
    env = compare(rig, CLIPX, CLIPX)
    assert env["ok"] is True, env
    assert env["result"]["equal"] is True


def test_a_library_reel_is_not_something_to_compare(rig: Rig) -> None:
    env = compare(rig, A, wt("/projects/p/ws/P1O/R1"))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE"
    assert "not a PySequence" in env["error"] or "PySequence" in env["error"]


def test_each_side_says_which_sequence_it_read(rig: Rig) -> None:
    result = compare(rig)["result"]
    assert result["a"]["wiretapId"] == A_WT and result["b"]["wiretapId"] == B_WT
    assert result["a"]["name"] == "ca" and result["a"]["duration"] == 200
    assert result["a"]["versionCount"] == 1 and result["a"]["videoTrackCount"] == 3
    assert result["a"]["audioTrackCount"] == 1
    assert result["a"]["uid"] == rig.handles["ca"].uid.get_value()


def test_the_op_reads_only(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None and op.effect == "read" and op.undo == "none"
    before = [
        [(s.record_in.frame, s.source_name) for s in t.segments]
        for t in rig.handles["cb"].versions[0].tracks
    ]
    compare(rig)
    after = [
        [(s.record_in.frame, s.source_name) for s in t.segments]
        for t in rig.handles["cb"].versions[0].tracks
    ]
    assert before == after and rig.records() == []


@pytest.mark.parametrize("limit", [0, -1, 2.5])
def test_the_limit_must_be_a_positive_integer(rig: Rig, limit: Any) -> None:
    env = compare(rig, limit=limit)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"


def test_gaps_are_equal_when_their_type_and_record_range_are_the_same(rig: Rig) -> None:
    """Measured on Flame 2025.2.4: the same edit read from two sequences gave a Gap
    with source 177-192 against 90-105 and the name '' against 'untitled'."""
    seq = rig.handles["cb"]
    inner = seq.versions[0].tracks[0].segments[1]
    assert inner.type == "Gap"
    inner.source_in = PyTime(177)
    inner.source_out = PyTime(186)
    inner.name.set_value("untitled")
    lone = seq.versions[0].tracks[2].segments[0]
    lone.name.set_value("untitled")
    lone.source_in = PyTime(7)
    env = compare(rig)
    assert env["ok"] is True, env
    assert env["result"]["equal"] is True, env["result"]["differences"]


def test_a_gap_with_another_record_range_is_still_a_difference(rig: Rig) -> None:
    rig.handles["cb"].versions[0].tracks[2].segments[0]._set_record(0, 189)
    env = compare(rig)
    assert ("video track 2 segment 0", "recordOut") in diffs(env)


def test_a_gap_against_a_media_segment_is_still_a_difference(rig: Rig) -> None:
    rig.handles["cb"].versions[0].tracks[0]._segments[1] = media("clipG", 100, 109, name="G")
    env = compare(rig)
    assert ("video track 0 segment 1", "type") in diffs(env)
    assert ("video track 0 segment 1", "sourceName") in diffs(env)
