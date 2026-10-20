"""Tests for the selection-extraction and segment-connection fake (fake_selection_connections.py).

What `extract_selection_to_media_panel` did on Flame 2025.2.4 is stated in the measured terms (the
selected segments leave, the later ones of that track move left, the length of the sequence becomes
the end of the longest content of any track and the trailing Gap of every track follows it, a
destination is needed, a selected Gap makes an empty sequence). The ops' tests lean on these
behaviours; this file is what keeps the fake honest."""

from __future__ import annotations

from typing import Any

import pytest

import fake_selection_connections as fsc
from fake_media_tree import (
    PyAudioChannel,
    PyAudioTrack,
    PyClip,
    PyLibrary,
    PyReel,
    PySegment,
    PySequence,
    PyTrack,
    PyVersion,
    adopt,
    share_sources,
)
from timeline_p32a_helpers import gap, media, rows


def build() -> tuple[PySequence, PyReel]:
    """V1: Gap 0-9 | A 10-33 | Gap 34-39 | B 40-63; V2: C 0-47."""
    reel = PyReel("R", "uid-r", "/wt/R")
    seq = PySequence(
        "S",
        wiretap_id="/wt/R/S",
        duration=64,
        versions=[
            PyVersion(
                [
                    PyTrack(
                        [gap(0, 9), media("a", 10, 33), gap(34, 39), media("b", 40, 63)],
                    ),
                    PyTrack([media("c", 0, 47)]),
                ]
            )
        ],
    )
    adopt(reel, [seq])
    reel._children.append(seq)
    return seq, reel


def pick(seq: PySequence, track: int, *indexes: int) -> None:
    for index in indexes:
        seq.versions[0].tracks[track].segments[index].selected = True


def layout(seq: PySequence, track: int = 0) -> list[list[Any]]:
    return [r[:3] for r in rows(seq, track)]


def test_a_selection_is_a_flag_that_is_read_and_written_as_an_attribute_or_a_bool() -> None:
    seg = media("a", 0, 9)
    assert seg.selected.get_value() is False
    seg.selected.set_value(True)
    assert seg.selected.get_value() is True
    seg.selected = False
    assert seg.selected.get_value() is False
    PySegment.selected_is_attribute = False
    try:
        assert seg.selected is False
        seg.selected = True
        assert seg.selected is True
    finally:
        PySegment.selected_is_attribute = True


def test_extract_removes_the_selected_segments_and_closes_up_only_their_track() -> None:
    seq, reel = build()
    pick(seq, 0, 1)
    made = seq.extract_selection_to_media_panel(reel)
    assert layout(seq) == [
        ["Gap", 0, 9],
        ["Gap", 10, 15],
        ["Video Segment", 16, 39],
        ["Gap", 40, 47],  # the longest content now ends at 47 (track 1): the Gap follows it
    ]
    assert layout(seq, 1) == [["Video Segment", 0, 47]]
    assert seq.duration.frame == 48
    assert type(made).__name__ == "PyClip" and made.duration.frame == 24
    assert made in reel.children and made.parent is reel


def test_a_trailing_gap_that_is_already_there_is_set_to_the_new_end_not_doubled() -> None:
    seq, reel = build()
    seq.versions[0].tracks[0]._segments.append(gap(64, 71))
    pick(seq, 0, 1)
    seq.extract_selection_to_media_panel(reel)
    assert layout(seq)[-1] == ["Gap", 40, 47]


def test_consecutive_selected_segments_come_out_as_one_block() -> None:
    seq, reel = build()
    pick(seq, 0, 1, 2)
    made = seq.extract_selection_to_media_panel(reel)
    assert layout(seq) == [["Gap", 0, 9], ["Video Segment", 10, 33], ["Gap", 34, 47]]
    assert made.duration.frame == 24  # the media of the block (the Gap is not in the clip)


def test_a_selected_gap_is_closed_and_what_comes_back_is_an_empty_sequence() -> None:
    seq, reel = build()
    pick(seq, 0, 2)
    made = seq.extract_selection_to_media_panel(reel)
    assert layout(seq)[2:] == [["Video Segment", 34, 57]]  # it ends the sequence: no Gap
    assert type(made).__name__ == "PySequence" and made.duration.frame == 0


def test_without_a_destination_or_a_selection_nothing_happens() -> None:
    seq, reel = build()
    before = rows(seq)
    pick(seq, 0, 1)
    assert seq.extract_selection_to_media_panel() is None and rows(seq) == before
    seq.versions[0].tracks[0].segments[1].selected = False
    assert seq.extract_selection_to_media_panel(reel) is None and rows(seq) == before
    assert reel.children == [seq]


def test_lift_leaves_a_gap_where_the_media_was_and_moves_nothing() -> None:
    seq, reel = build()
    pick(seq, 0, 1, 3)
    made = seq.lift_selection_to_media_panel(reel)
    assert layout(seq) == [["Gap", 0, 9], ["Gap", 10, 33], ["Gap", 34, 39], ["Gap", 40, 63]]
    assert type(made).__name__ == "PyClip"


def test_a_sequence_in_a_library_is_locked() -> None:
    seq, reel = build()
    library = PyLibrary("L", "uid-l", "/wt/L", children=[reel])
    assert library.children == [reel]
    pick(seq, 0, 1)
    with pytest.raises(RuntimeError, match="Clip is locked"):
        seq.extract_selection_to_media_panel(reel)


def test_connections_are_made_removed_and_listed_with_the_segment_itself() -> None:
    first, second, third = media("a", 0, 9), media("a", 0, 9), media("a", 0, 9)
    share_sources(first, second, third)
    assert first.connected_segments() == [first]  # measured: [self] when not connected
    first.create_connection()
    assert first.connected_segments() == [first, second, third]
    assert second.connected_segments() == [first, second, third]
    second.remove_connection()
    assert first.connected_segments() == [first, third]
    assert second.connected_segments() == [second]
    third.remove_connection()
    assert first.connected_segments() == [first]


def test_the_list_without_the_segment_itself_is_a_test_switch_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = media("a", 0, 9), media("a", 0, 9)
    fsc.connect(first, second)
    monkeypatch.setattr(fsc, "INCLUDE_SELF", False)
    assert first.connected_segments() == [second]
    assert media("a", 0, 9).connected_segments() == []


def test_invented_fake_sync_gives_the_connected_segments_the_source_of_the_one_called_on() -> None:
    first, second = media("a", 0, 9, source_in=4), media("b", 0, 9, source_in=0)
    fsc.connect(first, second)
    first.sync_connected_segments()
    assert second.source_name == "a" and second.source_in.frame == 4


def test_a_replace_reaches_the_connected_segments_and_a_clip_too_short_changes_none() -> None:
    reel = PyReel("R", "uid-r", "/wt/R")
    clip = PyClip("len72", wiretap_id="/wt/R/len72", duration=72)
    clip._media = (0, 72)
    adopt(reel, [clip])
    first, second = media("a", 0, 9), media("a", 0, 9)
    for seg in (first, second):
        PySequence("S", wiretap_id="/wt/S", versions=[PyVersion([PyTrack([seg])])])
    fsc.connect(first, second)
    first.smart_replace_media(clip)
    assert first.source_name == "len72" and second.source_name == "len72"
    tiny = PyClip("tiny", wiretap_id="/wt/R/tiny", duration=3)
    tiny._media = (0, 3)
    first.smart_replace_media(tiny)
    assert second.source_name == "len72"


# -- measured on Flame 2025.2.4 (probe project) --------------------------------------------
# The length of the sequence is the end of the longest content (everything but a Gap, video and
# audio) and the trailing Gap of every track follows it; a track that ends in content at the new
# end has no trailing Gap.


def measured_1() -> tuple[PySequence, PyReel]:
    """Two video tracks and an audio track of one Gap, all 120 frames long: track 0 = clip1 0-23,
    Gap 24-47, clip2 48-71, p2_len48 72-119; track 1 = p2_len72 0-71, Gap 72-119; audio = Gap
    0-119."""
    reel = PyReel("R", "uid-r", "/wt/R")
    seq = PySequence(
        "S",
        wiretap_id="/wt/R/S",
        duration=120,
        versions=[
            PyVersion(
                [
                    PyTrack(
                        [
                            media("clip1", 0, 23),
                            gap(24, 47),
                            media("clip2", 48, 71),
                            media("p2_len48", 72, 119),
                        ]
                    ),
                    PyTrack([media("p2_len72", 0, 71), gap(72, 119)]),
                ]
            )
        ],
    )
    seq.audio_tracks = [PyAudioTrack(channels=[PyAudioChannel([gap(0, 119)])])]
    adopt(reel, [seq])
    reel._children.append(seq)
    return seq, reel


def audio_layout(seq: PySequence) -> list[list[Any]]:
    channel = seq.audio_tracks[0].channels[0]
    return [[s.type, s.record_in.frame, s.record_out.frame] for s in channel.segments]


def test_measured_1_the_track_that_ends_in_content_has_no_gap_and_the_others_shrink() -> None:
    seq, reel = measured_1()
    pick(seq, 0, 2)
    seq.extract_selection_to_media_panel(reel)
    assert layout(seq) == [
        ["Video Segment", 0, 23],
        ["Gap", 24, 47],
        ["Video Segment", 48, 95],
    ]
    assert layout(seq, 1) == [["Video Segment", 0, 71], ["Gap", 72, 95]]  # not touched, but shorter
    assert audio_layout(seq) == [["Gap", 0, 95]]
    assert seq.duration.frame == 96


def test_measured_2_a_track_whose_gap_would_start_at_the_new_end_loses_it() -> None:
    seq, reel = measured_1()
    seq.versions[0].tracks[0]._segments[:] = [
        media("clip1", 0, 23),
        gap(24, 47),
        media("p2_len48", 48, 95),
    ]
    seq.versions[0].tracks[1]._segments[:] = [media("p2_len72", 0, 71), gap(72, 95)]
    seq.audio_tracks[0].channels[0]._segments[:] = [gap(0, 95)]
    adopt(seq.versions[0].tracks[0], seq.versions[0].tracks[0]._segments)
    pick(seq, 0, 1)  # the Gap
    seq.extract_selection_to_media_panel(reel)
    assert layout(seq) == [["Video Segment", 0, 23], ["Video Segment", 24, 71]]
    assert layout(seq, 1) == [["Video Segment", 0, 71]]
    assert audio_layout(seq) == [["Gap", 0, 71]]
    assert seq.duration.frame == 72


def test_measured_3_when_the_audio_sets_the_length_the_track_gets_a_trailing_gap() -> None:
    seq, reel = measured_1()
    seq.audio_tracks[0].channels[0]._segments[:] = [media("audio", 0, 119)]
    seq.versions[0].tracks[1]._segments[:] = [media("p2_len72", 0, 71), gap(72, 119)]
    pick(seq, 0, 2)
    seq.extract_selection_to_media_panel(reel)
    assert layout(seq) == [
        ["Video Segment", 0, 23],
        ["Gap", 24, 47],
        ["Video Segment", 48, 95],
        ["Gap", 96, 119],
    ]
    assert layout(seq, 1) == [["Video Segment", 0, 71], ["Gap", 72, 119]]
    assert audio_layout(seq) == [["Video Segment", 0, 119]]
    assert seq.duration.frame == 120


def test_lift_keeps_every_trailing_gap_where_it_was() -> None:
    seq, reel = measured_1()
    pick(seq, 0, 2)
    seq.lift_selection_to_media_panel(reel)
    assert layout(seq, 1) == [["Video Segment", 0, 71], ["Gap", 72, 119]]
    assert audio_layout(seq) == [["Gap", 0, 119]]
    assert seq.duration.frame == 120
