"""Tests `timeline.overwrite`, `insert`, `cut`, `create_version`, `create_track`.

A time is the absolute record frame, what is refused before anything is copied or journaled,
SILENT_NOOP by reading the sequence back, the checkpoint and the revert. Run through the real
Runtime with a scripted `transport.exec` over the fake Flame tree. The fake follows what was
measured on Flame 2025.2.4: cut makes the given frame the first frame of the right-hand part and
does nothing on a segment's first frame, overwrite and insert past the end leave a Gap, and the
in/out marks of the clip limit what goes in (fake_timeline_build.py)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import (
    PyAudioChannel,
    PyAudioTrack,
    PyClip,
    PySegment,
    PySequence,
    PyTime,
    PyTrack,
)
from flame_mcp import opschema
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import add_media_clip, clip_sel, untouched_world
from timeline_p32a_helpers import (
    CLIPX,
    S1,
    S1_WT,
    S2,
    checkpoints,
    fingerprint_of_target,
    library_copy,
    make_timeline_rig,
    rows,
    stash_entries,
    wt,
)

OVERWRITE = "timeline.overwrite"
INSERT = "timeline.insert"
CUT = "timeline.cut"
CREATE_VERSION = "timeline.create_version"
CREATE_TRACK = "timeline.create_track"
GAP = "Gap"
VIDEO = "Video Segment"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    r = make_timeline_rig(monkeypatch, tmp_path)
    r.handles["c12"] = add_media_clip(r, "len12", start=0, length=12)
    return r


def put(rig: Rig, op: str, sequence: dict[str, Any], at: int, **extra: Any) -> dict[str, Any]:
    args = {"sequence": sequence, "source": clip_sel(rig.handles["c12"]), "at": at, **extra}
    return rig.do(op, args)


def layout(seq: Any, track: int = 0) -> list[list[Any]]:
    return [r[:3] for r in rows(seq, track)]


def test_overwrite_after_the_end_leaves_a_gap_and_the_time_is_the_absolute_frame(rig: Rig) -> None:
    env = put(rig, OVERWRITE, S2, 30)
    assert env["ok"] is True, env
    assert layout(rig.handles["s2"]) == [[VIDEO, 0, 23], [GAP, 24, 29], [VIDEO, 30, 41]]
    result = env["result"]
    assert result["mode"] == "overwrite" and result["at"] == 30 and result["changed"] is True
    assert result["source"]["name"] == "len12"
    assert result["before"]["tracks"][0]["endFrame"] == 23
    assert result["after"]["tracks"][0]["endFrame"] == 41
    assert result["after"]["tracks"][0]["layout"] == [
        [VIDEO, 0, 23, "clip1"],
        [GAP, 24, 29, ""],
        [VIDEO, 30, 41, "len12"],
    ]
    assert result["placed"]["recordIn"] == 30 and result["placed"]["selector"]["record_in"] == 30
    assert result["startFrame"] == {"before": 1, "after": 1, "changed": False}
    assert result["warnings"] == []


def test_overwrite_in_the_middle_replaces_what_is_there_and_keeps_the_rest(rig: Rig) -> None:
    env = put(rig, OVERWRITE, S1, 20)
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"]) == [
        [GAP, 0, 9],
        [VIDEO, 10, 19],
        [VIDEO, 20, 31],
        [VIDEO, 32, 33],
        [GAP, 34, 39],
        [VIDEO, 40, 63],
    ]
    assert layout(rig.handles["s1"], 1) == [[VIDEO, 0, 47]]  # the other track is not touched


def test_insert_moves_only_the_target_track_and_splits_the_segment_it_lands_in(rig: Rig) -> None:
    env = put(rig, INSERT, S1, 20)
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"]) == [
        [GAP, 0, 9],
        [VIDEO, 10, 19],
        [VIDEO, 20, 31],
        [VIDEO, 32, 45],
        [GAP, 46, 51],
        [VIDEO, 52, 75],
    ]
    assert layout(rig.handles["s1"], 1) == [[VIDEO, 0, 47]]
    ends = {t["track"]: t["endFrame"] for t in env["result"]["after"]["tracks"]}
    assert ends == {0: 75, 1: 47}  # the other track is out of step now


def test_the_version_and_track_choose_where_it_goes(rig: Rig) -> None:
    env = put(rig, OVERWRITE, S1, 50, track=1)
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"], 1) == [[VIDEO, 0, 47], [GAP, 48, 49], [VIDEO, 50, 61]]
    assert env["result"]["after"]["tracks"][1]["layout"][-1] == [VIDEO, 50, 61, "len12"]
    assert "layout" not in env["result"]["after"]["tracks"][0]  # the target track only


def test_a_clip_with_marks_puts_only_the_marked_range_and_the_start_frame_is_reported(
    rig: Rig,
) -> None:
    empty = rig.handles["s2"]
    empty._versions[0]._tracks[0]._segments[:] = []
    clip: PyClip = rig.handles["c12"]
    clip._media = (0, 24)
    clip.in_mark, clip.out_mark = PyTime(4), PyTime(16)
    env = put(rig, OVERWRITE, S2, 0)
    assert env["ok"] is True, env
    [seg] = empty.versions[0].tracks[0].segments
    assert (seg.record_in.frame, seg.record_out.frame) == (0, 11)
    assert (seg.source_in.frame, seg.source_out.frame) == (4, 15)
    assert env["result"]["startFrame"] == {"before": 1, "after": 5, "changed": True}


def test_a_long_track_is_listed_around_the_frame_only(rig: Rig) -> None:
    seq: PySequence = rig.handles["s2"]
    track = seq._versions[0]._tracks[0]
    track._segments[:] = [rig_seg(i * 3, i * 3 + 2) for i in range(60)]
    env = put(rig, OVERWRITE, S2, 90)
    layout_rows = env["result"]["before"]["tracks"][0]["layout"]
    assert env["result"]["before"]["tracks"][0]["layoutWindowed"] is True
    assert 1 < len(layout_rows) <= 24 and any(r[1] <= 90 <= r[2] for r in layout_rows)
    assert env["result"]["before"]["tracks"][0]["segmentCount"] == 60


def rig_seg(first: int, last: int) -> Any:
    from timeline_p32a_helpers import media

    return media("clip1", first, last)


def test_a_source_that_is_not_a_clip_is_refused(rig: Rig) -> None:
    env = rig.do(OVERWRITE, {"sequence": S1, "source": S2, "at": 0})
    assert env["errorCode"] == "SELECTOR_TYPE" and "not a PyClip" in env["error"]
    assert untouched_world(rig)


def test_a_clip_as_the_target_is_refused(rig: Rig) -> None:
    env = put(rig, OVERWRITE, CLIPX, 0)
    assert env["errorCode"] == "SELECTOR_TYPE" and "not a PySequence" in env["error"]
    assert untouched_world(rig)


def test_a_track_or_version_that_does_not_exist_is_refused(rig: Rig) -> None:
    assert put(rig, OVERWRITE, S1, 0, track=5)["errorCode"] == "SELECTOR_EMPTY"
    assert put(rig, INSERT, S1, 0, version=3)["errorCode"] == "SELECTOR_EMPTY"
    assert untouched_world(rig)


def test_a_sequence_in_a_library_is_refused_and_not_touched(rig: Rig) -> None:
    copy = library_copy(rig)
    before = rows(copy)
    env = put(rig, OVERWRITE, wt(copy.get_wiretap_node_id()), 0)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert rows(copy) == before and checkpoints(rig) == [] and rig.records() == []


@pytest.mark.parametrize(
    ("op", "args"),
    [
        (OVERWRITE, {"at": -1}),
        (OVERWRITE, {"at": 1.5}),
        (INSERT, {"at": 0, "track": -1}),
    ],
)
def test_a_negative_or_fractional_number_is_invalid_args(
    rig: Rig, op: str, args: dict[str, Any]
) -> None:
    env = rig.do(op, {"sequence": S1, "source": clip_sel(rig.handles["c12"]), **args})
    assert env["errorCode"] == "INVALID_ARGS"
    assert untouched_world(rig)


def test_an_overwrite_that_changes_nothing_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySequence, "overwrite", lambda self, clip, time, track: None)
    env = put(rig, OVERWRITE, S1, 20)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert env["result"]["changed"] is False and "nothing in the sequence changed" in env["error"]
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.state() == "failed" and env["journal"]["revert"]["op"] == "journal.revert"


def test_a_change_that_is_not_at_the_frame_is_reported_as_a_warning(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PySequence.overwrite

    def late(self: PySequence, clip: PyClip, time: PyTime, track: PyTrack) -> Any:
        return original(self, clip, PyTime(time.frame + 1, self.frame_rate), track)

    monkeypatch.setattr(PySequence, "overwrite", late)
    env = put(rig, OVERWRITE, S1, 20)
    assert env["ok"] is True and env["result"]["placed"] is None
    assert "no media segment starts at 20" in env["result"]["warnings"][0]


def test_a_revert_puts_the_sequence_back_with_the_fingerprint_it_had(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = put(rig, INSERT, S1, 20)
    assert env["ok"] is True and fingerprint_of_target(rig, S1) != before
    ref = env["journal"]
    assert ref["changed"] is True and ref["revert"]["args"] == {"group": ref["group"]}
    [record] = checkpoints(rig)
    assert record["id"] == ref["checkpoint"] and record["fingerprint"] == before
    out = rig.do("journal.revert", {"group": ref["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before
    assert layout(stash_entries(rig)[0])[1] == [
        VIDEO,
        10,
        33,
    ]  # the stash is the sequence as it was


def test_a_revert_after_another_edit_is_a_conflict(rig: Rig) -> None:
    env = put(rig, OVERWRITE, S1, 20)
    put(rig, OVERWRITE, S1, 60)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"


def test_the_intent_names_the_op_and_the_sequence(rig: Rig) -> None:
    env = put(rig, OVERWRITE, S1, 20)
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.intent["op"] == OVERWRITE and view.intent["kind"] == "overwrite"
    assert view.intent["args"]["at"] == 20 and view.intent["target"] == wt(S1_WT)
    assert view.outcome is not None and view.outcome["ok"] is True
    assert env["execution"]["undo"] == "checkpoint" and env["undoSteps"] == 0


def test_a_track_cut_makes_the_frame_the_first_one_of_the_right_part(rig: Rig) -> None:
    env = rig.do(CUT, {"sequence": S1, "at": 20, "track": 0})
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"]) == [
        [GAP, 0, 9],
        [VIDEO, 10, 19],
        [VIDEO, 20, 33],
        [GAP, 34, 39],
        [VIDEO, 40, 63],
    ]
    assert layout(rig.handles["s1"], 1) == [[VIDEO, 0, 47]]
    result = env["result"]
    assert result["scope"] == "track" and result["split"] is True and result["changed"] is True
    assert result["willCut"] == [
        {"version": 0, "track": 0, "type": VIDEO, "recordIn": 10, "recordOut": 33}
    ]
    assert result["after"]["tracks"][0]["segmentCount"] == 5


def test_a_cut_of_the_whole_sequence_cuts_every_track_that_has_a_segment_there(rig: Rig) -> None:
    env = rig.do(CUT, {"sequence": S1, "at": 20, "scope": "all"})
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"], 1) == [[VIDEO, 0, 19], [VIDEO, 20, 47]]
    assert layout(rig.handles["s1"])[1:3] == [[VIDEO, 10, 19], [VIDEO, 20, 33]]
    assert env["result"]["split"] == [{"version": 0, "track": 0}, {"version": 0, "track": 1}]
    assert [t["track"] for t in env["result"]["after"]["tracks"]] == [0, 1]


@pytest.mark.parametrize(
    "at",
    [0, 10, 34, 40, 64, 500],
    ids=["gap-start", "segment-start", "second-gap-start", "last-segment-start", "after", "far"],
)
def test_a_cut_at_the_first_frame_of_a_segment_or_outside_the_segments_is_refused(
    rig: Rig, at: int
) -> None:
    env = rig.do(CUT, {"sequence": S1, "at": at, "track": 0})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "not inside a segment" in env["error"]
    assert env["result"]["ran"] is False and env["result"]["segments"][0]["recordIn"] == 0
    assert env["result"]["segments"][0]["type"] == GAP
    assert "mediaSegments" not in env["result"]
    assert untouched_world(rig)


@pytest.mark.parametrize("at", [5, 9, 36, 39], ids=["gap-1", "gap-end", "gap2-mid", "gap2-end"])
def test_a_track_cut_inside_a_gap_splits_the_gap(rig: Rig, at: int) -> None:
    before = layout(rig.handles["s1"])
    env = rig.do(CUT, {"sequence": S1, "at": at, "track": 0})
    assert env["ok"] is True, env
    gap_in, gap_out = (0, 9) if at < 10 else (34, 39)
    after = layout(rig.handles["s1"])
    assert len(after) == len(before) + 1
    assert [GAP, gap_in, at - 1] in after and [GAP, at, gap_out] in after
    result = env["result"]
    assert result["split"] is True and result["changed"] is True
    assert result["willCut"] == [
        {"version": 0, "track": 0, "type": GAP, "recordIn": gap_in, "recordOut": gap_out}
    ]
    assert result["warnings"] == []
    assert layout(rig.handles["s1"], 1) == [[VIDEO, 0, 47]]


def test_a_cut_of_the_whole_sequence_needs_media_around_the_frame_and_lists_only_media(
    rig: Rig,
) -> None:
    env = rig.do(CUT, {"sequence": S1, "at": 5, "scope": "all"})
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"], 1) == [[VIDEO, 0, 4], [VIDEO, 5, 47]]
    assert [(h["track"], h["type"]) for h in env["result"]["willCut"]] == [(1, VIDEO)]
    assert env["result"]["split"] == [{"version": 0, "track": 1}]


def test_a_cut_of_the_whole_sequence_where_only_a_gap_is_hit_is_refused(rig: Rig) -> None:
    s1 = rig.handles["s1"]
    s1._versions[0]._tracks[1]._segments[:] = [rig_seg(0, 3)]
    env = rig.do(CUT, {"sequence": S1, "at": 36, "scope": "all"})
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "not inside a media segment" in env["error"] and "use scope track" in env["error"]
    assert env["result"]["ran"] is False
    assert {r["type"] for r in env["result"]["segments"]} == {VIDEO}
    assert untouched_world(rig)
    track = rig.do(CUT, {"sequence": S1, "at": 36, "track": 0})
    assert track["ok"] is True, track  # the same frame is cut with scope='track'
    assert track["result"]["willCut"][0]["type"] == GAP


def test_a_cut_of_the_whole_sequence_with_no_segment_inside_anywhere_is_refused(rig: Rig) -> None:
    env = rig.do(CUT, {"sequence": S1, "at": 500, "scope": "all"})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "any track" in env["error"]
    assert {r["track"] for r in env["result"]["segments"]} == {0, 1}
    assert untouched_world(rig)


@pytest.mark.parametrize(
    "args",
    [
        {"at": 20},
        {"at": 20, "scope": "row", "track": 0},
        {"at": 20, "scope": "all", "track": 0},
        {"at": 20, "scope": "all", "version": 0},
    ],
    ids=["no-track", "bad-scope", "all-with-track", "all-with-version"],
)
def test_cut_arguments_that_do_not_fit_together_are_invalid_args(
    rig: Rig, args: dict[str, Any]
) -> None:
    env = rig.do(CUT, {"sequence": S1, **args})
    assert env["errorCode"] == "INVALID_ARGS"
    assert untouched_world(rig)


def test_a_cut_that_changes_nothing_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PyTrack, "cut", lambda self, time: None)
    env = rig.do(CUT, {"sequence": S1, "at": 20, "track": 0})
    assert env["errorCode"] == "SILENT_NOOP" and env["result"]["changed"] is False
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_a_cut_is_reverted_like_the_others(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = rig.do(CUT, {"sequence": S1, "at": 20, "scope": "all"})
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def audio_seg(source: str, first: int, last: int) -> PySegment:
    return PySegment(
        name="AUD",
        source_name=source,
        record_in=first,
        record_out=last,
        source_in=0,
        source_out=last - first,
        record_duration=last - first + 1,
        type="Audio Segment",
    )


def audio_track(source: str, first: int, last: int, channels: int = 2) -> PyAudioTrack:
    return PyAudioTrack(
        channels=[PyAudioChannel([audio_seg(source, first, last)]) for _ in range(channels)]
    )


def audio_sources(seq: Any) -> list[list[str]]:
    return [
        [s.source_name for s in channel.segments]
        for track in seq.audio_tracks
        for channel in track.channels
    ]


def with_audio_only_change(rig: Rig) -> PySequence:
    """S2 holds the video of c12 at 0-11 (the same call a second time leaves the video as it is)
    and two audio channels that hold another source; c12 carries audio of its own."""
    assert put(rig, OVERWRITE, S2, 0)["ok"] is True
    seq: PySequence = rig.handles["s2"]
    seq.audio_tracks = [audio_track("old30", 0, 11)]
    rig.handles["c12"].audio_tracks = [audio_track("len12", 0, 11)]
    return seq


def test_an_overwrite_that_changes_only_the_audio_is_not_a_silent_noop(rig: Rig) -> None:
    seq = with_audio_only_change(rig)
    video_before = fingerprint_of_target(rig, S2)
    env = put(rig, OVERWRITE, S2, 0)
    assert env["ok"] is True, env
    assert fingerprint_of_target(rig, S2) == video_before  # the video tracks did not change
    assert audio_sources(seq) == [["len12"], ["len12"]]
    result = env["result"]
    assert result["changed"] is True
    audio = result["audio"]
    assert audio["changed"] is True and audio["complete"] is True
    assert audio["before"]["trackCount"] == 1 and audio["before"]["channelCount"] == 2
    assert audio["before"]["segmentCount"] == 2 and audio["after"]["segmentCount"] == 2
    assert audio["before"]["rows"][0] == [0, 0, "Audio Segment", "old30", 0, 11, 0, 11]
    assert audio["after"]["rows"][1] == [0, 1, "Audio Segment", "len12", 0, 11, 0, 11]
    assert audio["before"]["rowsTruncated"] is False and audio["after"]["errors"] == []
    [warning] = result["warnings"]
    assert "only the audio changed" in warning and "video tracks" in warning
    assert "the revert check compares the video fingerprint only" in warning
    assert env["journal"]["changed"] is False  # the journal compares the video fingerprint


def test_an_insert_reports_the_audio_beside_the_video_and_no_only_audio_warning(
    rig: Rig,
) -> None:
    seq = with_audio_only_change(rig)
    video = seq._versions[0]._tracks[0]
    before = [s.record_in.frame for s in video._segments]
    env = put(rig, INSERT, S2, 40)  # past the end of the video: the video changes here as well
    assert env["ok"] is True and [s.record_in.frame for s in video._segments] != before
    assert env["result"]["audio"]["changed"] is True
    assert not any("only the audio changed" in w for w in env["result"]["warnings"])


def test_a_silent_noop_stays_one_when_neither_the_video_nor_the_audio_changed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_audio_only_change(rig)
    monkeypatch.setattr(PySequence, "overwrite", lambda self, clip, time, track: None)
    env = put(rig, OVERWRITE, S2, 0)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    audio = env["result"]["audio"]
    assert audio["changed"] is False and audio["before"] == audio["after"]
    assert env["result"]["changed"] is False


def test_a_sequence_without_audio_tracks_is_judged_as_before(rig: Rig) -> None:
    env = put(rig, OVERWRITE, S1, 20)
    assert env["ok"] is True, env
    audio = env["result"]["audio"]
    assert audio["changed"] is False and audio["complete"] is True
    assert audio["before"] == audio["after"]
    assert audio["before"]["trackCount"] == 0 and audio["before"]["rows"] == []


def test_a_sequence_that_has_no_audio_tracks_member_at_all_still_works(rig: Rig) -> None:
    seq: PySequence = rig.handles["s1"]
    del seq.audio_tracks
    env = put(rig, OVERWRITE, S1, 20)
    assert env["ok"] is True, env
    audio = env["result"]["audio"]
    assert audio["before"]["trackCount"] is None and audio["after"]["errors"] == []
    assert audio["changed"] is False


def test_audio_that_cannot_be_read_is_marked_and_the_call_goes_on(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = with_audio_only_change(rig)
    chan = seq.audio_tracks[0].channels[1]

    def broken(self: PyAudioChannel) -> Any:
        if self is chan:
            raise RuntimeError("not readable at 0x1234")
        return list(self._segments)

    monkeypatch.setattr(PyAudioChannel, "segments", property(broken))
    env = put(rig, OVERWRITE, S2, 0)
    assert env["ok"] is True, env
    audio = env["result"]["audio"]
    assert audio["complete"] is False
    assert audio["before"]["errors"] == ["audio track 0 channel 1: RuntimeError"]
    assert audio["after"]["errors"] == audio["before"]["errors"]  # the type only, not the text
    assert audio["before"]["segmentCount"] == 1 and audio["changed"] is True
    assert any("could not be read" in w for w in env["result"]["warnings"])


def test_a_segment_cell_that_cannot_be_read_leaves_null_and_a_marker(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = with_audio_only_change(rig)
    broken = seq.audio_tracks[0].channels[0]._segments[0]

    class Dead:
        @property
        def frame(self) -> int:
            raise RuntimeError("gone")

    broken.source_out = Dead()
    env = put(rig, OVERWRITE, S2, 0)
    assert env["ok"] is True, env
    audio = env["result"]["audio"]
    assert audio["before"]["rows"][0][7] is None
    assert audio["before"]["errors"] == [
        "audio track 0 channel 0 segment 0 source_out: RuntimeError"
    ]


def test_a_long_audio_listing_is_capped(rig: Rig) -> None:
    seq: PySequence = rig.handles["s2"]
    seq.audio_tracks = [
        PyAudioTrack(
            channels=[PyAudioChannel([audio_seg("a", i * 3, i * 3 + 2) for i in range(40)])]
        )
    ]
    env = put(rig, OVERWRITE, S2, 90)
    audio = env["result"]["audio"]
    assert audio["before"]["segmentCount"] == 40 and len(audio["before"]["rows"]) == 24
    assert audio["before"]["rowsTruncated"] is True and audio["changed"] is False


def test_a_cut_that_changes_only_the_audio_is_not_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = with_audio_only_change(rig)

    def cut_audio_only(self: PyTrack, time: PyTime) -> Any:
        for channel in seq.audio_tracks[0].channels:
            channel._segments[:] = [audio_seg("cut", 0, 5), audio_seg("cut", 6, 11)]
        return None

    monkeypatch.setattr(PyTrack, "cut", cut_audio_only)
    env = rig.do(CUT, {"sequence": S2, "at": 5, "track": 0})
    assert env["ok"] is True, env
    assert env["result"]["audio"]["changed"] is True and env["result"]["changed"] is True
    assert any("only the audio changed" in w for w in env["result"]["warnings"])
    assert not any("no segment ends" in w for w in env["result"]["warnings"])


def test_create_version_adds_one_at_the_end_with_one_track_of_gap(rig: Rig) -> None:
    env = rig.do(CREATE_VERSION, {"sequence": S1})
    assert env["ok"] is True, env
    seq = rig.handles["s1"]
    assert len(seq.versions) == 2
    result = env["result"]
    assert result["versionsBefore"] == [2] and result["versionsAfter"] == [2, 1]
    assert result["version"]["index"] == 1
    [track] = result["version"]["tracks"]
    assert (track["segmentCount"], track["startFrame"], track["endFrame"]) == (1, 0, 63)
    assert track["trailingGap"] == {"recordIn": 0, "recordOut": 63}
    assert result["changed"] is True


def test_create_version_is_reverted_with_the_fingerprint_back(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = rig.do(CREATE_VERSION, {"sequence": S1})
    assert fingerprint_of_target(rig, S1) != before
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_create_track_adds_one_at_the_end_of_the_version(rig: Rig) -> None:
    env = rig.do(CREATE_TRACK, {"sequence": S1})
    assert env["ok"] is True, env
    assert len(rig.handles["s1"].versions[0].tracks) == 3
    result = env["result"]
    assert result["tracksBefore"] == 2 and result["tracksAfter"] == 3
    assert result["track"]["index"] == 2
    assert result["track"]["summary"]["trailingGap"] == {"recordIn": 0, "recordOut": 63}
    assert layout(rig.handles["s1"], 2) == [[GAP, 0, 63]]


def test_a_new_track_can_be_filled_and_a_version_that_is_not_there_is_refused(rig: Rig) -> None:
    rig.do(CREATE_TRACK, {"sequence": S1})
    env = put(rig, OVERWRITE, S1, 8, track=2)
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"], 2) == [[GAP, 0, 7], [VIDEO, 8, 19], [GAP, 20, 63]]
    missing = rig.do(CREATE_TRACK, {"sequence": S1, "version": 4})
    assert missing["errorCode"] == "SELECTOR_EMPTY"


def test_a_noop_create_is_reported(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from fake_media_tree import PyVersion

    monkeypatch.setattr(PyVersion, "create_track", lambda self, track_index=None: PyTrack([]))
    env = rig.do(CREATE_TRACK, {"sequence": S1})
    assert env["errorCode"] == "SILENT_NOOP"


@pytest.mark.parametrize("name", [OVERWRITE, INSERT, CUT, CREATE_VERSION, CREATE_TRACK])
def test_each_op_is_a_timeline_checkpointed_write(rig: Rig, name: str) -> None:
    op = rig.registry.get(name)
    assert op is not None and op.checkpoint is not None
    assert op.checkpoint.target_kind == "timeline" and op.checkpoint.batch_arg == "sequence"
    assert (op.effect, op.undo, op.thread, op.category) == (
        "reversible",
        "checkpoint",
        "main",
        "timeline",
    )
    assert "journal.revert" in op.description and "SILENT_NOOP" in op.description


def test_readonly_denies_the_edit_and_nothing_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    ro.handles["c12"] = add_media_clip(ro, "len12", start=0, length=12)
    assert put(ro, OVERWRITE, S1, 20)["errorCode"] == "POLICY_DENIED"
    assert untouched_world(ro)


def test_an_answer_that_never_came_is_diverged_when_checked(rig: Rig) -> None:
    rig.exec.fault(OVERWRITE, "TIMEOUT", when="after", skip=1)
    env = put(rig, OVERWRITE, S1, 20)
    assert env["errorCode"] == "TIMEOUT" and rig.runtime.guard.denial() is not None
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "diverged" and rig.runtime.guard.denial() is None


@pytest.mark.parametrize("name", [OVERWRITE, INSERT, CUT, "timeline.delete_segments"])
def test_the_descriptions_say_that_the_audio_is_judged_apart_from_the_video(
    rig: Rig, name: str
) -> None:
    op = rig.registry.get(name)
    assert op is not None
    text = " ".join(op.description.split())
    for word in (
        "PySequence.audio_tracks",
        "result.audio",
        "not a SILENT_NOOP",
        "journal.changed stays false",
        "rowsTruncated",
        "the revert check compares the video fingerprint only",
        "can all be read on the Audio Segment objects",
        "A call that changes nothing, video tracks and audio tracks read back, is SILENT_NOOP.",
    ):
        assert word in text, word
    assert "A call that changes nothing is SILENT_NOOP" not in text


def test_the_overwrite_description_gives_the_measured_audio_replacement(rig: Rig) -> None:
    op = rig.registry.get(OVERWRITE)
    assert op is not None
    text = " ".join(op.description.split())
    assert "Measured on Flame 2025.2.4: overwriting a clip that has audio" in text
    assert "replaced the audio of the sequence" in text


def test_the_cut_description_says_that_a_gap_is_cut_like_a_segment(rig: Rig) -> None:
    op = rig.registry.get(CUT)
    assert op is not None
    text = " ".join(op.description.split())
    assert "PyTrack.cut inside a Gap splits it into two Gaps" in text
    assert "scope='all' needs a segment that has media" in text
    assert "a Gap alone is not enough" in text
    assert "not inside a segment of that track (media or Gap" in text
    assert "result.segments" in text and "mediaSegments" not in text
    assert "is not a Gap" not in text and "in a Gap or outside" not in text


@pytest.mark.parametrize(
    "name", [OVERWRITE, INSERT, CUT, CREATE_VERSION, CREATE_TRACK, "timeline.create_sequence"]
)
def test_the_descriptions_follow_the_writing_rules(rig: Rig, name: str) -> None:
    op = rig.registry.get(name)
    assert op is not None
    text = op.description
    # \u8a02\u6b63 and \u672a\u6e2c are the Japanese for "correction" and "not measured".
    assert not re.search(r"plan/\d+|\u8a02\u6b63\d+|correction \d+|P\d-\d", text)
    assert "not measured" not in text and "\u672a\u6e2c" not in text
    assert text.isascii()
    first = opschema.first_sentence(text)
    assert 0 < len(first) <= opschema.SUMMARY_MAX_CHARS and not first.endswith("…")
    assert all(p.description.isascii() for p in op.params)
