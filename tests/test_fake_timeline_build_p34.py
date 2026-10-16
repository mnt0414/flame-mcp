"""Tests the fake timeline-building calls against what Flame 2025.2.4 gave.

The calls are overwrite / insert / cut / create_version / create_track / create_sequence; the
numbers below were observed on Flame 2025.2.4.
"""

from __future__ import annotations

from typing import Any

import pytest

from fake_media_tree import (
    ACTIVE_TIMELINE,
    FakeFlame,
    PyClip,
    PyLibrary,
    PyReel,
    PySequence,
    PyTime,
    PyTrack,
    PyVersion,
    adopt,
)
from timeline_p32a_helpers import gap, media, rows

FPS = "23.976 fps"


def clip(name: str, length: int = 24) -> PyClip:
    made = PyClip(
        name,
        wiretap_id=f"/wt/{name}",
        duration=length,
        versions=[PyVersion([PyTrack([media(name, 0, length - 1, name=name)])])],
    )
    made._media = (0, length)
    return made


def sequence(*segments: Any, start: int = 0) -> PySequence:
    FakeFlame()
    seq = PySequence(
        "S",
        wiretap_id="/wt/S",
        versions=[PyVersion([PyTrack(list(segments))])],
        start_time=PyTime(start),
    )
    return seq


def at(frame: int) -> PyTime:
    return PyTime(frame, FPS)


def layout(seq: PySequence, track: int = 0) -> list[list[Any]]:
    return [r[:3] for r in rows(seq, track)]


def test_a_cut_at_an_absolute_frame_makes_it_the_first_frame_of_the_right_part() -> None:
    seq = sequence(media("clip1", 86400, 86423), start=86400)
    seq.versions[0].tracks[0].cut(at(86411))
    assert layout(seq) == [["Video Segment", 86400, 86410], ["Video Segment", 86411, 86423]]


def test_a_sequence_cut_does_the_same_with_one_track() -> None:
    seq = sequence(media("clip1", 86400, 86423), start=86400)
    seq.cut(at(86411))
    assert layout(seq) == [["Video Segment", 86400, 86410], ["Video Segment", 86411, 86423]]


def test_an_insert_in_the_middle_splits_and_shifts() -> None:
    seq = sequence(media("clip1", 86400, 86423), start=86400)
    seq.insert(clip("clip2"), at(86412), seq.versions[0].tracks[0])
    assert layout(seq) == [
        ["Video Segment", 86400, 86411],
        ["Video Segment", 86412, 86435],
        ["Video Segment", 86436, 86447],
    ]
    assert seq.duration.frame == 48


def test_into_an_empty_sequence_from_zero_and_from_twelve() -> None:
    zero = sequence()
    zero.overwrite(clip("clip1"), at(0), zero.versions[0].tracks[0])
    assert layout(zero) == [["Video Segment", 0, 23]]
    twelve = sequence()
    twelve.overwrite(clip("clip1"), at(12), twelve.versions[0].tracks[0])
    assert layout(twelve) == [["Gap", 0, 11], ["Video Segment", 12, 35]]
    assert twelve.duration.frame == 36


def test_insert_at_the_start_and_inside() -> None:
    seq = sequence(media("clip1", 0, 23))
    seq.insert(clip("clip2"), at(0), seq.versions[0].tracks[0])
    assert layout(seq) == [["Video Segment", 0, 23], ["Video Segment", 24, 47]]
    assert [r[0] for r in rows(seq)] == ["Video Segment"] * 2
    assert seq.versions[0].tracks[0].segments[0].source_name == "clip2"


def test_a_cut_at_the_first_frame_or_outside_changes_nothing_and_a_gap_is_cut_too() -> None:
    seq = sequence(media("clip1", 0, 23), gap(24, 31), media("clip2", 32, 55))
    track = seq.versions[0].tracks[0]
    before = layout(seq)
    assert track.cut(at(32)) is None and track.cut(at(0)) is None and track.cut(at(900)) is None
    assert layout(seq) == before
    track.cut(at(27))
    assert layout(seq)[1:3] == [["Gap", 24, 26], ["Gap", 27, 31]]


def test_after_the_end_leaves_a_gap_for_overwrite_and_insert_alike() -> None:
    for method in ("overwrite", "insert"):
        seq = sequence(media("clip1", 0, 23))
        getattr(seq, method)(clip("clip2"), at(33), seq.versions[0].tracks[0])
        assert layout(seq) == [["Video Segment", 0, 23], ["Gap", 24, 32], ["Video Segment", 33, 56]]


def test_marks_put_only_the_marked_range_and_an_empty_overwrite_moves_start_frame() -> None:
    marked = clip("clip1")
    marked.in_mark, marked.out_mark = PyTime(4), PyTime(16)
    empty = sequence()
    empty.start_frame = 1
    empty.overwrite(marked, at(0), empty.versions[0].tracks[0])
    [seg] = empty.versions[0].tracks[0].segments
    assert (
        seg.source_in.frame,
        seg.source_out.frame,
        seg.record_in.frame,
        seg.record_out.frame,
    ) == (
        4,
        15,
        0,
        11,
    )
    assert empty.start_frame == 5
    seq = sequence(media("clip2", 0, 23))
    seq.insert(marked, at(12), seq.versions[0].tracks[0])
    assert layout(seq) == [
        ["Video Segment", 0, 11],
        ["Video Segment", 12, 23],
        ["Video Segment", 24, 35],
    ]
    assert seq.start_frame == 1


def test_create_track_goes_to_the_end_with_no_index_and_before_index_minus_one_with_one() -> None:
    one = sequence(media("clip1", 0, 23))
    ver = one.versions[0]
    made = ver.create_track()
    assert type(made).__name__ == "PyTrack" and len(one.versions[0].tracks) == 2
    assert layout(one, 1) == [["Gap", 0, 23]]
    first = one.versions[0].create_track(track_index=1)
    assert one.versions[0].tracks[0] is first


def test_create_version_adds_one_track_of_gap_as_long_as_the_sequence() -> None:
    seq = sequence(media("clip1", 0, 23))
    seq.create_version()
    assert len(seq.versions) == 2 and layout(seq)[0][0] == "Video Segment"
    assert [r[:3] for r in rows(seq, 0, 1)] == [["Gap", 0, 23]]


def test_the_time_must_be_a_pytime_and_the_track_a_track_of_the_sequence() -> None:
    seq = sequence(media("clip1", 0, 23))
    with pytest.raises(TypeError):
        seq.overwrite(clip("clip2"), 5, seq.versions[0].tracks[0])
    with pytest.raises(RuntimeError):
        seq.overwrite(clip("clip2"), at(5), PyTrack([]))


def test_a_sequence_of_a_library_is_locked() -> None:
    library = PyLibrary("L", "uid-l", "/wt/L")
    reel = PyReel("R", "uid-r", "/wt/L/R")
    adopt(library, [reel])
    library._children.append(reel)
    seq = sequence(media("clip1", 0, 23))
    adopt(reel, [seq])
    with pytest.raises(RuntimeError, match="locked"):
        seq.cut(at(5))
    with pytest.raises(RuntimeError, match="locked"):
        reel.create_sequence("X")


def test_create_sequence_reads_back_and_the_timeline_shows_it() -> None:
    FakeFlame()
    reel = PyReel("R", "uid-r", "/wt/R")
    seq = reel.create_sequence(
        name="N",
        video_tracks=2,
        start_at=PyTime("01:00:00:00", FPS),
        frame_rate="25 fps",
        width=3840,
        height=2160,
    )
    assert reel.children == [seq] and ACTIVE_TIMELINE[0].clip is seq
    assert (seq.frame_rate, seq.width, seq.height) == ("25 fps", 3840, 2160)
    assert seq.start_time.get_value().frame == 86400 and seq.start_frame == 1
    assert [r[:3] for r in rows(seq, 1)] == [["Gap", 86400, 86400]]
    assert len(seq.versions[0].tracks) == 2
    again = reel.create_sequence(name="N")
    assert again is not seq and len(reel.children) == 2


def test_a_bad_frame_rate_raises_the_text_flame_gave() -> None:
    reel = PyReel("R", "uid-r", "/wt/R")
    with pytest.raises(
        RuntimeError, match=r"Frame rate should be one of the following: 23.976 fps"
    ):
        reel.create_sequence(name="N", frame_rate="99 fps")
