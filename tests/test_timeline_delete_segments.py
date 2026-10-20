"""Tests `timeline.delete_segments`.

Which segments go (by record_in), what ripple does to the track and to the length of the sequence
(measured: the sequence is as long as the end of the longest content of any track and the
trailing Gap of every track follows), the selection that is saved and put back, the by-product the
extract leaves in the stash reel and its clean-up, the judgement by reading the track back, the
refusals that leave nothing behind and the checkpoint. Run through the real Runtime with a
scripted `transport.exec` over the fake Flame tree. The fake follows what was
measured on Flame 2025.2.4 for `extract_selection_to_media_panel` (fake_selection_connections.py);
`lift_selection_to_media_panel` was measured (a Gap of the same length is left,
nothing moves); what the fake does beyond that is marked INVENTED there."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fake_media_tree
import fake_selection_connections as fsc
from fake_media_tree import (
    FakeAttr,
    FakeFlame,
    PyAudioChannel,
    PyAudioTrack,
    PySegment,
    PySequence,
    PyTime,
)
from fake_segment_edit import put_into
from fake_timeline_build import _refresh
from flame_mcp import timeline_checkpoints
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import untouched_world
from timeline_p32a_helpers import (
    CLIPX,
    S1,
    S2,
    S2_WT,
    checkpoints,
    fingerprint_of_target,
    gap,
    library_copy,
    make_timeline_rig,
    media,
    reel_names,
    rows,
    seg_sel,
    stash_entries,
    stash_library,
    wt,
)

OP = "timeline.delete_segments"
GAP = "Gap"
VIDEO = "Video Segment"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def delete(rig: Rig, segments: list[int], sequence: dict[str, Any] = S1, **extra: Any) -> Any:
    args: dict[str, Any] = {"sequence": sequence, "segments": segments}
    args.setdefault("track", 0)
    args.update(extra)
    return rig.do(OP, args)


def layout(seq: Any, track: int = 0) -> list[list[Any]]:
    return [r[:3] for r in rows(seq, track)]


def selected(seq: Any) -> list[tuple[int, int, int]]:
    return [
        (vi, ti, seg.record_in.frame)
        for vi, ver in enumerate(seq.versions)
        for ti, trk in enumerate(ver.tracks)
        for seg in trk.segments
        if seg._selected.get_value()
    ]


def stash_name(rig: Rig) -> str:
    library = stash_library(rig)
    assert library is not None
    return str(library.name.get_value())


def select(seq: Any, *positions: tuple[int, int]) -> None:
    for ti, index in positions:
        seq.versions[0].tracks[ti].segments[index]._selected.set_value(True)


def segs(rig: Rig, track: int = 0) -> list[PySegment]:
    found: list[PySegment] = rig.handles["s1"].versions[0].tracks[track].segments
    return found


# S1 track 0: Gap 0-9 | A clip1 10-33 | Gap 34-39 | B clip2 40-63 (Burn-in Letterbox)
# S1 track 1: C clip3 0-47


def test_a_ripple_delete_closes_the_hole_and_the_gap_at_the_end_follows_the_new_length(
    rig: Rig,
) -> None:
    env = delete(rig, [10])
    assert env["ok"] is True, env
    # the longest content now ends at 47 (track 1), so track 0 ends in a Gap up to 47, not 63
    assert layout(rig.handles["s1"]) == [[GAP, 0, 9], [GAP, 10, 15], [VIDEO, 16, 39], [GAP, 40, 47]]
    assert layout(rig.handles["s1"], 1) == [[VIDEO, 0, 47]]  # the other track stays
    result = env["result"]
    assert result["ripple"] is True and result["method"] == "extract_selection_to_media_panel"
    assert result["removed"] == [
        {
            "index": 1,
            "type": VIDEO,
            "recordIn": 10,
            "recordOut": 33,
            "duration": 24,
            "name": "A",
            "sourceName": "clip1",
        }
    ]
    assert result["shifted"] == 24 and result["changed"] is True
    assert result["before"]["segmentCount"] == 4 and result["after"]["segmentCount"] == 4
    assert (result["before"]["gapCount"], result["before"]["mediaCount"]) == (2, 2)
    assert (result["after"]["gapCount"], result["after"]["mediaCount"]) == (3, 1)
    assert result["before"]["sequenceDuration"]["frame"] == 64
    assert result["after"]["sequenceDuration"]["frame"] == 48
    assert (result["before"]["endFrame"], result["after"]["endFrame"]) == (63, 47)
    assert result["warnings"] == [] and result["notices"] == []


def test_without_ripple_the_media_is_replaced_by_a_gap_and_nothing_moves(rig: Rig) -> None:
    env = delete(rig, [10], ripple=False)
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"]) == [[GAP, 0, 9], [GAP, 10, 33], [GAP, 34, 39], [VIDEO, 40, 63]]
    result = env["result"]
    assert result["ripple"] is False and result["method"] == "lift_selection_to_media_panel"
    assert result["shifted"] == 0 and result["warnings"] == []
    assert result["after"]["mediaCount"] == 1


def test_without_ripple_two_segments_that_are_not_adjacent_leave_two_gaps_and_nothing_moves(
    rig: Rig,
) -> None:
    others = rows(rig.handles["s1"], 1)
    env = delete(rig, [10, 40], ripple=False)
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"]) == [[GAP, 0, 9], [GAP, 10, 33], [GAP, 34, 39], [GAP, 40, 63]]
    assert env["result"]["shifted"] == 0 and env["result"]["warnings"] == []
    assert rows(rig.handles["s1"], 1) == others  # the other track is untouched


def test_ripple_is_the_default_and_two_segments_are_closed_up_together(rig: Rig) -> None:
    env = delete(rig, [40, 10])
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"]) == [[GAP, 0, 9], [GAP, 10, 47]]  # track 1 ends at 47
    removed = env["result"]["removed"]
    assert [r["recordIn"] for r in removed] == [40, 10]  # in the order given
    assert env["result"]["shifted"] == 24  # nothing is behind B: only the gap in front moved
    assert env["result"]["after"]["mediaCount"] == 0


def test_a_gap_can_be_deleted_and_closes_the_gap(rig: Rig) -> None:
    env = delete(rig, [34])
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"]) == [
        [GAP, 0, 9],
        [VIDEO, 10, 33],
        [VIDEO, 34, 57],  # it ends the sequence: no Gap behind it
    ]
    result = env["result"]
    assert result["removed"][0]["type"] == GAP and result["shifted"] == 6
    assert result["extracted"]["type"] == "PySequence"  # an empty sequence, not a clip
    assert result["cleanup"]["deleted"] is True
    assert result["connected"] == {"total": 0, "segments": [], "unreadable": 0}


def test_the_by_product_is_found_reported_and_deleted_and_no_user_reel_is_touched(
    rig: Rig,
) -> None:
    reel = rig.handles["reel"]
    names_before = reel_names(reel)
    entries_before = len(stash_entries(rig))
    env = delete(rig, [10])
    assert env["ok"] is True, env
    extracted = env["result"]["extracted"]
    assert extracted["type"] == "PyClip" and extracted["name"] == "S1 extract"
    assert env["result"]["stash"]["library"] == stash_name(rig)
    assert env["result"]["stash"]["reel"].startswith(timeline_checkpoints.REEL_PREFIX)
    assert extracted["duration"]["frame"] == 24 and isinstance(extracted["uid"], str)
    cleanup = env["result"]["cleanup"]
    assert cleanup == {"attempted": True, "deleted": True, "returned": True, "error": None}
    assert rig.flame.find_by_uid(extracted["uid"]) is None
    assert reel_names(reel) == names_before  # nothing was put into or taken out of the Desktop reel
    assert len(stash_entries(rig)) == entries_before + 1  # only the checkpoint copy is new
    [made] = [c for c in rig.flame.media_panel.copy_calls]  # the checkpoint's copy, nothing else
    assert made[0] is rig.handles["s1"]


def test_the_stash_reel_is_the_checkpoints_and_it_is_the_destination_of_the_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    original = fsc.take_selection

    def spy(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        seen.append(destination)
        return original(seq, destination, ripple=ripple)

    monkeypatch.setattr(fsc, "take_selection", spy)
    assert delete(rig, [10])["ok"] is True
    [destination] = seen
    assert destination.parent is stash_library(rig)
    assert stash_name(rig) == timeline_checkpoints.checkpoint_library_name()
    assert str(destination.name.get_value()).startswith(timeline_checkpoints.REEL_PREFIX)


def test_only_the_targets_are_selected_for_the_call_and_the_old_selection_comes_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    select(rig.handles["s1"], (0, 3), (1, 0))  # B and C were selected by the user
    when_called: list[list[tuple[int, int, int]]] = []
    original = fsc.take_selection

    def spy(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        when_called.append(selected(seq))
        return original(seq, destination, ripple=ripple)

    monkeypatch.setattr(fsc, "take_selection", spy)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert when_called == [[(0, 0, 10)]]  # A and nothing else
    assert selected(rig.handles["s1"]) == [(0, 0, 16), (0, 1, 0)]  # B moved to 16, C stays
    assert env["result"]["selection"] == {"before": 2, "changed": 2, "after": 2, "error": None}


def test_nothing_selected_before_means_nothing_selected_after(rig: Rig) -> None:
    env = delete(rig, [10, 40])
    assert env["ok"] is True and selected(rig.handles["s1"]) == []
    assert env["result"]["selection"]["before"] == 0 and env["result"]["selection"]["after"] == 0


def test_a_selection_the_segment_wrapper_reports_as_a_plain_bool_works_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySegment, "selected_is_attribute", False)
    select(rig.handles["s1"], (1, 0))
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert layout(rig.handles["s1"])[2] == [VIDEO, 16, 39] and selected(rig.handles["s1"]) == [
        (0, 1, 0)
    ]


def test_a_selection_that_does_not_stick_stops_before_anything_is_removed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ignore(self: PySegment, value: Any) -> None:
        return None

    monkeypatch.setattr(
        PySegment, "selected", property(lambda self: bool(self._selected.get_value()), ignore)
    )
    before = rows(rig.handles["s1"])
    env = delete(rig, [10])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "after selected was set" in env["error"] and "nothing was removed" in env["error"]
    assert env["result"]["ran"] is False and rows(rig.handles["s1"]) == before
    assert env["result"]["cleanup"] is None and len(rig.handles["reel"].children) == 5


def test_a_call_that_changes_nothing_is_a_silent_noop_and_the_targets_are_not_left_selected(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", lambda *a, **k: None)
    select(rig.handles["s1"], (0, 3))
    before = rows(rig.handles["s1"])
    env = delete(rig, [10])
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "nothing in the sequence changed" in env["error"]
    result = env["result"]
    assert result["changed"] is False and result["returned"]["shape"] == "none"
    assert rows(rig.handles["s1"]) == before
    assert selected(rig.handles["s1"]) == [(0, 0, 40)]  # B as it was; A was not left selected
    assert "nothing to delete" in result["cleanup"]["error"]
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_a_change_that_is_not_the_measured_one_is_an_error_with_before_and_after(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drops_without_closing(self: PySequence, destination: Any = None, *a: Any) -> Any:
        return fsc.take_selection(self, destination, ripple=False)

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", drops_without_closing)
    env = delete(rig, [10])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "not the way extract_selection_to_media_panel is measured" in env["error"]
    assert env["result"]["before"]["mediaCount"] == 2 and env["result"]["after"]["mediaCount"] == 1
    assert env["journal"]["revert"]["op"] == "journal.revert"  # the sequence is still revertible


def test_without_ripple_a_readback_that_differs_from_the_measured_one_is_a_warning(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def closes_up(self: PySequence, destination: Any = None, *a: Any) -> Any:
        return fsc.take_selection(self, destination, ripple=True)

    monkeypatch.setattr(PySequence, "lift_selection_to_media_panel", closes_up)
    env = delete(rig, [10], ripple=False)
    assert env["ok"] is True, env
    [warning] = env["result"]["warnings"]
    assert (
        "lift_selection_to_media_panel did not leave the media segments where they were" in warning
    )
    assert "not measured" not in warning


def test_another_track_that_changed_is_an_error_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsc.take_selection

    def also_touches_the_other_track(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        seq.versions[0].tracks[1].segments[0]._set_record(2, 49)
        return made

    monkeypatch.setattr(fsc, "take_selection", also_touches_the_other_track)
    env = delete(rig, [10])
    assert env["errorCode"] == "FLAME_API_ERROR" and "version 0 track 1 changed" in env["error"]


def test_a_selection_that_raises_is_reported_as_such_and_nothing_is_removed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: PySegment, value: Any) -> None:
        raise RuntimeError("scripted selection failure")

    monkeypatch.setattr(
        PySegment, "selected", property(lambda self: bool(self._selected.get_value()), refuse)
    )
    before = rows(rig.handles["s1"])
    env = delete(rig, [10])
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "selecting the segments raised RuntimeError: scripted selection failure" in env["error"]
    assert "is unchanged" in env["error"] and rows(rig.handles["s1"]) == before


def test_a_selection_that_cannot_be_put_back_is_a_warning_and_does_not_fail_the_op(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"broken": False}

    def setter(self: PySegment, value: Any) -> None:
        if state["broken"]:
            raise RuntimeError("scripted restore failure")
        self._selected.set_value(bool(value))

    monkeypatch.setattr(
        PySegment, "selected", property(lambda self: bool(self._selected.get_value()), setter)
    )
    original = PySequence.extract_selection_to_media_panel

    def extracts_then_breaks(self: PySequence, destination: Any = None, *a: Any) -> Any:
        answer = original(self, destination, *a)
        state["broken"] = True
        return answer

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", extracts_then_breaks)
    select(rig.handles["s1"], (1, 0))  # C was selected by the user
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert "scripted restore failure" in env["result"]["selection"]["error"]
    assert any(
        "selection" in w and "scripted restore failure" in w for w in env["result"]["warnings"]
    )


def test_a_call_that_raises_is_reported_with_what_the_track_looks_like_now(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explodes(self: PySequence, destination: Any = None, *a: Any) -> Any:
        raise RuntimeError("scripted extract failure")

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", explodes)
    select(rig.handles["s1"], (1, 0))
    env = delete(rig, [10])
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert (
        "extract_selection_to_media_panel() raised RuntimeError: scripted extract" in env["error"]
    )
    assert "is unchanged" in env["error"]
    assert selected(rig.handles["s1"]) == [(0, 1, 0)]


def test_a_by_product_that_cannot_be_deleted_is_a_warning_and_the_segments_stay_removed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = FakeFlame.delete

    def refuses(self: FakeFlame, obj: Any, confirm: bool = True) -> bool:
        if str(obj.name.get_value()).endswith(" extract"):
            raise RuntimeError("scripted delete failure")
        return original(self, obj, confirm)

    monkeypatch.setattr(FakeFlame, "delete", refuses)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    cleanup = env["result"]["cleanup"]
    assert cleanup["attempted"] is True and cleanup["deleted"] is False
    assert "scripted delete failure" in cleanup["error"]
    [warning] = env["result"]["warnings"]
    assert warning.startswith("by-product of the call:")
    assert layout(rig.handles["s1"])[2] == [VIDEO, 16, 39]
    assert rig.flame.find_by_uid(env["result"]["extracted"]["uid"]) is not None


def test_when_the_by_product_cannot_be_told_apart_nothing_is_deleted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsc.take_selection

    def two_new_entries(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        original(seq, destination, ripple=ripple)
        return None  # the answer does not say which of the two is the one

    monkeypatch.setattr(fsc, "take_selection", two_new_entries)
    original_made = fsc._made_of

    def extra_entry(seq: PySequence, destination: Any, taken: list[list[PySegment]]) -> Any:
        put_into(destination, original_made(seq, destination, [[]]))
        return original_made(seq, destination, taken)

    monkeypatch.setattr(fsc, "_made_of", extra_entry)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    cleanup = env["result"]["cleanup"]
    assert cleanup["attempted"] is False and "none of them can be told" in cleanup["error"]
    assert env["result"]["extracted"] is None


def test_the_display_is_put_back_when_the_call_moves_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    s1 = rig.handles["s1"]
    rig.flame.timeline.clip = s1
    monkeypatch.setattr(fsc, "EXTRACT_MOVES_DISPLAY", True)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert rig.flame.timeline.clip is s1
    display = env["result"]["display"]
    assert display["reshown"] is True and display["shownAfter"]["name"] == "S1"


def test_a_connected_segment_is_reported_as_a_notice_and_not_read_or_changed_by_the_op(
    rig: Rig,
) -> None:
    s2_seg = rig.handles["s2"].versions[0].tracks[0].segments[0]
    fsc.connect(segs(rig)[1], s2_seg)
    s2_before = rows(rig.handles["s2"])
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert env["result"]["connected"] == {
        "total": 1,
        "segments": [{"recordIn": 10, "count": 1}],
        "unreadable": 0,
    }
    [notice] = env["result"]["notices"]
    assert "1 other segment(s) are connected" in notice
    assert "the segment itself left out" in notice
    assert "left the original sequence unchanged" in notice
    assert "flame.delete of a segment deletes" not in notice
    assert "journal.revert restores this sequence only" in notice
    assert rows(rig.handles["s2"]) == s2_before  # measured: the original was unchanged
    assert s2_seg._link_group  # the other side keeps its link in the fake


def test_a_segment_with_no_connection_counts_zero_others_and_adds_no_notice(rig: Rig) -> None:
    only = segs(rig)[1]
    assert only.connected_segments() == [only]  # measured: an unconnected segment answers [self]
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert env["result"]["connected"] == {"total": 0, "segments": [], "unreadable": 0}
    assert env["result"]["notices"] == []


def test_a_pair_counts_one_other_segment_not_two(rig: Rig) -> None:
    s2_seg = rig.handles["s2"].versions[0].tracks[0].segments[0]
    fsc.connect(segs(rig)[1], s2_seg)
    assert len(segs(rig)[1].connected_segments()) == 2  # measured: each answers 2
    env = delete(rig, [10])
    assert env["result"]["connected"]["total"] == 1


def test_a_connection_that_cannot_be_read_is_counted_and_does_not_stop_the_delete(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: PySegment) -> list[PySegment]:
        raise RuntimeError("no connections here")

    monkeypatch.setattr(PySegment, "connected_segments", broken)
    env = delete(rig, [10, 34])
    assert env["ok"] is True, env
    # the Gap is not asked: one unreadable, not two
    assert env["result"]["connected"] == {"total": 0, "segments": [], "unreadable": 1}


def test_expect_source_names_are_checked_per_segment_before_anything_is_copied(rig: Rig) -> None:
    wrong = delete(rig, [34, 40], expect_source_names=["", "clip1"])
    assert wrong["ok"] is False and wrong["errorCode"] == "CONFLICT"
    assert "segments[1] = 40" in wrong["error"] and "'clip2'" in wrong["error"]
    assert "nothing was done" in wrong["error"]
    ok = delete(rig, [34, 10], expect_source_names=["", "clip1"])
    assert ok["ok"] is True, ok


def test_a_mismatch_leaves_no_stash_and_no_journal_behind(rig: Rig) -> None:
    before = rows(rig.handles["s1"])
    env = delete(rig, [10], expect_source_names=["other"])
    assert env["errorCode"] == "CONFLICT" and untouched_world(rig)
    assert rows(rig.handles["s1"]) == before and checkpoints(rig) == []


def test_a_record_in_no_segment_has_is_refused_with_the_nearest_ones(rig: Rig) -> None:
    env = delete(rig, [10, 11])
    assert env["errorCode"] == "SELECTOR_EMPTY" and "segments[1] = 11" in env["error"]
    assert "nearest record_in values: [10, 0, 34, 40]" in env["error"]
    assert env["result"]["before"]["segmentCount"] == 4 and untouched_world(rig)


def test_a_version_or_track_that_does_not_exist_is_refused(rig: Rig) -> None:
    track = delete(rig, [10], track=5)
    assert track["errorCode"] == "SELECTOR_EMPTY" and "track index 5" in track["error"]
    version = delete(rig, [10], version=3)
    assert version["errorCode"] == "SELECTOR_EMPTY" and "version index 3" in version["error"]
    assert untouched_world(rig)


def test_the_track_is_required_as_for_timeline_cut(rig: Rig) -> None:
    env = rig.do(OP, {"sequence": S1, "segments": [10]})
    assert env["errorCode"] == "INVALID_ARGS" and "needs track" in env["error"]
    assert untouched_world(rig)


@pytest.mark.parametrize(
    ("segments", "extra", "text"),
    [
        ([], {}, "at least one record_in"),
        (list(range(101)), {}, "at most 100"),
        ([10, 40, 10], {}, "more than once: [10]"),
        ([10.5], {}, "non-negative integers"),
        ([10, -1], {}, "got [-1]"),
        ([10, 40], {"expect_source_names": ["clip1"]}, "parallel to segments"),
    ],
)
def test_bad_arguments_are_invalid_args_and_touch_nothing(
    rig: Rig, segments: list[Any], extra: dict[str, Any], text: str
) -> None:
    env = delete(rig, segments, **extra)
    assert env["errorCode"] == "INVALID_ARGS" and text in env["error"]
    assert untouched_world(rig)


def test_a_name_or_a_record_in_of_the_wrong_type_is_refused_by_the_schema(rig: Rig) -> None:
    assert delete(rig, ["10"])["errorCode"] == "INVALID_ARGS"  # type: ignore[list-item]
    assert delete(rig, [True])["errorCode"] == "INVALID_ARGS"
    assert delete(rig, [10], expect_source_names=[3])["errorCode"] == "INVALID_ARGS"
    assert delete(rig, [10], ripple="yes")["errorCode"] == "INVALID_ARGS"
    non_ascii = delete(rig, [10], expect_source_names=["テ"])
    assert non_ascii["errorCode"] == "INVALID_ARGS" and "ASCII" in non_ascii["error"]
    assert untouched_world(rig)


def test_a_sequence_in_a_library_and_a_clip_are_refused_before_anything_is_copied(
    rig: Rig,
) -> None:
    copy = library_copy(rig)
    before = rows(copy)
    in_library = delete(rig, [10], sequence=wt(copy.get_wiretap_node_id()))
    assert (
        in_library["errorCode"] == "PRECONDITION_UNMET"
        and "inside a library" in in_library["error"]
    )
    clip = delete(rig, [0], sequence=CLIPX)
    assert clip["errorCode"] == "SELECTOR_TYPE" and "not a PySequence" in clip["error"]
    assert rows(copy) == before and checkpoints(rig) == [] and rig.records() == []


def test_a_flame_without_the_method_is_refused_up_front(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySequence, "lift_selection_to_media_panel", None)
    env = delete(rig, [10], ripple=False)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "no PySequence.lift_selection_to_media_panel" in env["error"]
    assert untouched_world(rig)


def test_it_takes_a_checkpoint_first_and_journals_an_intent_and_an_outcome(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    ref = env["journal"]
    assert ref["revert"] == {"op": "journal.revert", "args": {"group": ref["group"]}}
    assert ref["changed"] is True
    [record] = checkpoints(rig)
    assert record["fingerprint"] == before and record["id"] == ref["checkpoint"]
    [intent] = [r for r in rig.records() if r["type"] == "intent"]
    assert intent["op"] == OP and intent["fingerprintBefore"] == before
    assert intent["args"]["segments"] == [10] and intent["targetKind"] == "timeline"
    [outcome] = [r for r in rig.records() if r["type"] == "outcome"]
    assert outcome["ok"] is True and outcome["fingerprintAfter"] == fingerprint_of_target(rig, S1)
    labels = [c.label for c in rig.exec.calls]
    assert labels.count(OP) == 2  # the check before the checkpoint, then the write


def test_the_write_addresses_the_sequence_by_wiretap_id(rig: Rig) -> None:
    nothing = delete(rig, [10], sequence={"kind": "selection"})  # nothing is selected
    assert nothing["ok"] is False and nothing["errorCode"] == "SELECTOR_EMPTY", nothing
    assert untouched_world(rig)
    rig.flame.media_panel.selected_entries = [rig.handles["s1"]]
    env = delete(rig, [10], sequence={"kind": "selection"})
    assert env["ok"] is True, env
    write = [c.code for c in rig.exec.calls if c.label == OP][-1].split("__op_src = ")[1]
    assert "_resolve_selection" not in write and "_td_delete(seq" in write


def test_journal_revert_puts_the_sequence_back_but_the_track_is_not_judged_by_the_selection(
    rig: Rig,
) -> None:
    before = rows(rig.handles["s1"])
    env = delete(rig, [10, 34])
    assert env["ok"] is True, env
    revert = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert revert["ok"] is True, revert
    [restored] = [c for c in rig.handles["reel"].children if c.name.get_value() == "S1"]
    assert rows(restored) == before


def test_the_by_product_is_not_in_the_stash_when_the_revert_restores_from_it(rig: Rig) -> None:
    env = delete(rig, [10])
    rig.do("journal.revert", {"group": env["journal"]["group"]})
    names = [str(e.name.get_value()) for e in stash_entries(rig)]
    assert not [n for n in names if n.endswith(" extract")]


def test_the_op_is_declared_as_a_timeline_checkpointed_destructive_write(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None and op.checkpoint is not None
    assert op.checkpoint.target_kind == "timeline" and op.checkpoint.batch_arg == "sequence"
    assert (op.effect, op.undo, op.thread, op.category) == (
        "destructive",
        "checkpoint",
        "main",
        "timeline",
    )
    assert op.wrap is None and op.journal is None
    assert [p.name for p in op.params] == [
        "sequence",
        "segments",
        "version",
        "track",
        "ripple",
        "expect_source_names",
    ]
    required = [p.name for p in op.params if p.required]
    assert required == ["sequence", "segments"]
    flat = " ".join(op.description.split())
    for word in (
        "PySegment.selected",
        "extract_selection_to_media_panel",
        "lift_selection_to_media_panel",
        "lift (ripple=false) removes the selected segment and leaves a Gap of the same length",
        "Both work on a sequence that is not shown in the Timeline",
        "the fingerprint matched",
        "THAT TRACK",
        "other tracks",
        "by-product",
        "SILENT_NOOP",
        "restores this sequence only",
        "connected_segments",
    ):
        assert word in flat, word
    for symbol in (
        "PySequence.extract_selection_to_media_panel",
        "PySequence.lift_selection_to_media_panel",
        "PySegment.selected",
        "PySegment.connected_segments",
    ):
        assert symbol in op.requires


def test_a_plan_bundles_it_like_any_timeline_write(rig: Rig) -> None:
    env = rig.do(
        "plan.run",
        {"steps": [{"op": OP, "args": {"sequence": S1, "segments": [10], "track": 0}}]},
    )
    assert env["ok"] is True, env
    assert len(checkpoints(rig)) == 1
    assert layout(rig.handles["s1"])[2] == [VIDEO, 16, 39]


def test_readonly_denies_it_and_nothing_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    assert delete(ro, [10])["errorCode"] == "POLICY_DENIED"
    assert untouched_world(ro)


def test_another_sequence_is_addressed_just_as_well(rig: Rig) -> None:
    env = delete(rig, [0], sequence=S2, track=0)
    assert env["ok"] is True, env
    assert env["result"]["removed"][0]["sourceName"] == "clip1"
    assert wt(S2_WT)["id"] == S2_WT and seg_sel(S2, 0, index=0)["parent"] == S2
    assert rows(rig.handles["s2"]) == [[GAP, 0, 23, 0, 23, "infinite", "infinite"]]


def reshape(
    rig: Rig,
    track0: list[PySegment],
    track1: list[PySegment],
    audio: list[PySegment] | None = None,
) -> PySequence:
    """S1 rebuilt as two video tracks (and one audio track of one channel when `audio` is given),
    the way the probe sequence of the real measurements looked."""
    seq: PySequence = rig.handles["s1"]
    tracks = seq._versions[0]._tracks
    tracks[0]._segments[:] = track0
    tracks[1]._segments[:] = track1
    if audio is not None:
        channel = PyAudioChannel(audio)
        atrack = PyAudioTrack(channels=[channel])
        atrack.parent = seq
        seq.audio_tracks = [atrack]
    _refresh(seq)
    return seq


def audio_rows(seq: PySequence) -> list[list[Any]]:
    channel = seq.audio_tracks[0].channels[0]
    return [[s.type, s.record_in.frame, s.record_out.frame] for s in channel.segments]


def probe_1(rig: Rig) -> PySequence:
    """Measured: track 0 = clip1 0-23, Gap 24-47, clip2 48-71, p2_len48 72-119; track
    1 = p2_len72 0-71, Gap 72-119; the audio is one Gap 0-119; 120 frames."""
    return reshape(
        rig,
        [media("clip1", 0, 23), gap(24, 47), media("clip2", 48, 71), media("p2_len48", 72, 119)],
        [media("p2_len72", 0, 71), gap(72, 119)],
        [gap(0, 119)],
    )


def test_measured_1_the_track_ending_in_content_has_no_gap_and_the_others_end_at_the_new_length(
    rig: Rig,
) -> None:
    seq = probe_1(rig)
    env = delete(rig, [48])
    assert env["ok"] is True, env
    assert layout(seq) == [[VIDEO, 0, 23], [GAP, 24, 47], [VIDEO, 48, 95]]
    assert layout(seq, 1) == [[VIDEO, 0, 71], [GAP, 72, 95]]  # shorter, not touched
    assert audio_rows(seq) == [[GAP, 0, 95]]
    result = env["result"]
    assert result["before"]["sequenceDuration"]["frame"] == 120
    assert result["after"]["sequenceDuration"]["frame"] == 96
    assert result["shifted"] == 24 and result["warnings"] == []
    assert result["audio"]["changed"] is True  # only its Gap at the end: not a warning


def test_measured_2_a_hole_closed_in_a_track_makes_the_other_track_lose_its_gap(rig: Rig) -> None:
    seq = reshape(
        rig,
        [media("clip1", 0, 23), gap(24, 47), media("p2_len48", 48, 95)],
        [media("p2_len72", 0, 71), gap(72, 95)],
        [gap(0, 95)],
    )
    env = delete(rig, [24])
    assert env["ok"] is True, env
    assert layout(seq) == [[VIDEO, 0, 23], [VIDEO, 24, 71]]
    assert layout(seq, 1) == [[VIDEO, 0, 71]]
    assert audio_rows(seq) == [[GAP, 0, 71]]
    assert env["result"]["after"]["sequenceDuration"]["frame"] == 72
    assert env["result"]["warnings"] == []


def test_measured_3_when_the_audio_sets_the_length_the_track_gets_a_gap_at_its_end(
    rig: Rig,
) -> None:
    seq = reshape(
        rig,
        [media("clip1", 0, 23), media("clip2", 24, 47), media("clip3", 48, 119)],
        [media("p2_len72", 0, 71), gap(72, 119)],
        [media("audio1", 0, 119)],
    )
    env = delete(rig, [24])
    assert env["ok"] is True, env
    assert layout(seq) == [[VIDEO, 0, 23], [VIDEO, 24, 95], [GAP, 96, 119]]
    assert layout(seq, 1) == [[VIDEO, 0, 71], [GAP, 72, 119]]
    assert audio_rows(seq) == [[VIDEO, 0, 119]]
    assert env["result"]["audio"]["changed"] is False
    assert env["result"]["before"]["sequenceDuration"] == env["result"]["after"]["sequenceDuration"]


def test_a_gap_at_the_end_of_the_target_track_is_not_required_and_may_be_absent(rig: Rig) -> None:
    seq = reshape(
        rig,
        [media("clip1", 0, 23), media("clip2", 24, 47), media("clip3", 48, 71)],
        [media("p2_len48", 0, 47)],
    )
    env = delete(rig, [24])
    assert env["ok"] is True, env
    assert layout(seq) == [[VIDEO, 0, 23], [VIDEO, 24, 47]]  # it ends the sequence: no Gap
    assert layout(seq, 1) == [[VIDEO, 0, 47]]


def test_with_no_content_left_the_length_is_not_judged(rig: Rig) -> None:
    seq = reshape(rig, [media("clip1", 0, 23), gap(24, 47)], [gap(0, 47)])
    env = delete(rig, [0])
    assert env["ok"] is True, env
    assert layout(seq) == [[GAP, 0, 47]] and layout(seq, 1) == [[GAP, 0, 47]]
    assert env["result"]["warnings"] == []


def test_a_sequence_whose_length_is_not_the_longest_content_is_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_1(rig)
    original = fsc.take_selection

    def forgets_the_length(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        seq.duration = PyTime(100)
        return made

    monkeypatch.setattr(fsc, "take_selection", forgets_the_length)
    env = delete(rig, [48])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "the sequence is 100 frame(s) long, not the 96" in env["error"]
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_another_track_whose_gap_at_the_end_is_not_at_the_new_length_is_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = probe_1(rig)
    original = fsc.take_selection

    def leaves_the_gap(seq_: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq_, destination, ripple=ripple)
        seq_.versions[0].tracks[1].segments[-1]._set_record(72, 119)
        return made

    monkeypatch.setattr(fsc, "take_selection", leaves_the_gap)
    env = delete(rig, [48])
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "version 0 track 1: the Gap at the end ends at frame 119, not at 95" in env["error"]
    assert layout(seq, 1)[-1] == [GAP, 72, 119]


def test_the_target_track_whose_gap_at_the_end_is_not_at_the_new_length_is_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fsc.take_selection

    def overshoots(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        seq.versions[0].tracks[0].segments[-1]._set_record(40, 50)
        return made

    monkeypatch.setattr(fsc, "take_selection", overshoots)
    env = delete(rig, [10])
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "version 0 track 0: the Gap at the end ends at frame 50, not at 47" in env["error"]


def test_another_track_whose_content_changed_is_an_error_even_with_a_gap_at_its_end(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_1(rig)
    original = fsc.take_selection

    def moves_content(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        seq.versions[0].tracks[1].segments[0]._set_record(0, 69)
        return made

    monkeypatch.setattr(fsc, "take_selection", moves_content)
    env = delete(rig, [48])
    assert env["errorCode"] == "FLAME_API_ERROR" and "version 0 track 1 changed" in env["error"]


def test_audio_content_that_changed_is_an_error_but_its_gap_at_the_end_is_not(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = reshape(
        rig,
        [media("clip1", 0, 23), media("clip2", 24, 47), media("clip3", 48, 119)],
        [media("p2_len72", 0, 71), gap(72, 119)],
        [media("audio1", 0, 119)],
    )
    original = fsc.take_selection

    def moves_audio(seq_: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq_, destination, ripple=ripple)
        seq_.audio_tracks[0].channels[0]._segments[0]._set_record(2, 119)
        return made

    monkeypatch.setattr(fsc, "take_selection", moves_audio)
    env = delete(rig, [24])
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "audio track 0 channel 0 changed" in env["error"]
    assert audio_rows(seq) == [[VIDEO, 2, 119]]


NOT_JUDGED = "the length of the sequence and the Gap at the end of the tracks were not judged"


def after_extract(monkeypatch: pytest.MonkeyPatch, mutate: Any) -> None:
    """Runs `mutate(seq)` right after the fake extract, to give the readback something odd."""
    original = fsc.take_selection

    def wrapped(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        mutate(seq)
        return made

    monkeypatch.setattr(fsc, "take_selection", wrapped)


def long_sequence(rig: Rig) -> PySequence:
    """120 frames long, but the longest content ends at 72 and every track ends in a Gap (NOT
    measured on Flame)."""
    return reshape(
        rig,
        [media("clip1", 0, 23), gap(24, 47), media("clip2", 48, 71), gap(72, 119)],
        [media("p2_len72", 0, 71), gap(72, 119)],
        [gap(0, 119)],
    )


def shifted(rig: Rig, start: int) -> PySequence:
    """Two tracks of content from frame 10 on; `start` is the start_time of the sequence."""
    seq = reshape(
        rig,
        [media("clip1", 10, 33), media("clip2", 34, 57)],
        [media("p2_len72", 10, 81)],
    )
    seq.start_time = FakeAttr(PyTime(start))
    return seq


def test_the_length_is_read_through_the_attribute_wrapper_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_1(rig)
    after_extract(monkeypatch, lambda s: setattr(s, "duration", FakeAttr(PyTime(96))))
    env = delete(rig, [48])
    assert env["ok"] is True, env
    assert env["result"]["warnings"] == []


def test_a_sequence_longer_than_its_longest_content_is_not_judged_for_length_and_gaps(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_sequence(rig)

    def odd(seq: PySequence) -> None:
        seq.duration = PyTime(100)
        seq.versions[0].tracks[1]._segments.append(gap(72, 119))

    after_extract(monkeypatch, odd)
    env = delete(rig, [48])
    assert env["ok"] is True, env
    warnings = env["result"]["warnings"]
    assert len(warnings) == 1
    assert "every track ending in a Gap), so " + NOT_JUDGED in warnings[0]
    assert "120 frame(s) long before" in warnings[0]


def test_a_sequence_longer_than_its_longest_content_still_has_its_media_compared(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_sequence(rig)
    after_extract(monkeypatch, lambda seq: seq.versions[0].tracks[1].segments[0]._set_record(0, 69))
    env = delete(rig, [48])
    assert env["errorCode"] == "FLAME_API_ERROR" and "version 0 track 1 changed" in env["error"]


def test_a_start_time_that_is_the_first_record_in_of_the_content_sets_the_length(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    shifted(rig, 10)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert env["result"]["warnings"] == []
    after_extract(monkeypatch, lambda seq: setattr(seq, "duration", PyTime(80)))
    shifted(rig, 10)
    bad = delete(rig, [10])
    assert bad["errorCode"] == "FLAME_API_ERROR"
    assert "the sequence is 80 frame(s) long, not the 72" in bad["error"]


def test_a_start_time_that_is_not_the_first_record_in_of_the_content_is_not_judged(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    shifted(rig, 0)
    after_extract(monkeypatch, lambda seq: setattr(seq, "duration", PyTime(100)))
    env = delete(rig, [10])
    assert env["ok"] is True, env
    warnings = env["result"]["warnings"]
    assert len(warnings) == 1
    first = "start_time of the sequence (frame 0) is not the first record_in of its content"
    assert first in warnings[0]
    assert "), so " + NOT_JUDGED in warnings[0]


def test_a_start_time_that_cannot_be_read_means_the_length_is_not_judged(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq: Any = shifted(rig, 10)
    seq.start_time = None
    after_extract(monkeypatch, lambda s: setattr(s, "duration", PyTime(100)))
    env = delete(rig, [10])
    assert env["ok"] is True, env
    warnings = env["result"]["warnings"]
    assert len(warnings) == 1
    assert "start_time of the sequence could not be read, so " + NOT_JUDGED in warnings[0]


def test_audio_that_cannot_be_read_means_audio_length_and_gaps_are_not_judged_for_ripple(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = probe_1(rig)
    chan = seq.audio_tracks[0].channels[0]

    def broken(self: PyAudioChannel) -> Any:
        if self is chan:
            raise RuntimeError("not readable")
        return list(self._segments)

    monkeypatch.setattr(PyAudioChannel, "segments", property(broken))
    after_extract(monkeypatch, lambda s: setattr(s, "duration", PyTime(100)))
    env = delete(rig, [48])
    assert env["ok"] is True, env
    warnings = env["result"]["warnings"]
    assert any("part of the audio could not be read" in w for w in warnings)
    ripple = [w for w in warnings if "for ripple the audio, " + NOT_JUDGED in w]
    assert len(ripple) == 1 and "media segments of the video tracks were compared" in ripple[0]
    assert len(warnings) == 2


def with_audio(rig: Rig, *, selected: bool = False) -> PySegment:
    """An audio track with one segment on S1 (the API lists an audio track's segments only
    through its selected_segments)."""
    seg = media("audio1", 0, 47, name="AUD")
    seg.selected = selected
    track = PyAudioTrack([seg])
    track.parent = rig.handles["s1"]
    rig.handles["s1"].audio_tracks.append(track)
    return seg


def test_the_audio_is_read_before_and_after_and_a_delete_leaves_it_as_it_was(rig: Rig) -> None:
    with_audio(rig)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    audio = env["result"]["audio"]
    assert audio["changed"] is False and audio["complete"] is True
    assert audio["before"]["rows"][0][:4] == [0, 0, "Video Segment", "audio1"]
    assert audio["before"] == audio["after"]
    assert not any("only the audio" in w for w in env["result"]["warnings"])


def test_a_call_that_changed_only_the_audio_is_not_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = with_audio(rig)

    def moves_audio_only(self: PySequence, *a: Any, **k: Any) -> Any:
        audio._set_record(8, 55)
        return None

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", moves_audio_only)
    env = delete(rig, [10])
    assert env["errorCode"] != "SILENT_NOOP"
    result = env["result"]
    assert result["changed"] is True and result["audio"]["changed"] is True
    assert result["audio"]["after"]["rows"][0][4:6] == [8, 55]


def test_a_selected_audio_segment_is_deselected_for_the_call_and_selected_again_after(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = with_audio(rig, selected=True)
    select(rig.handles["s1"], (0, 3))
    when_called: list[list[str]] = []
    original = fsc.take_selection

    def spy(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        when_called.append([str(s.name.get_value()) for s in seq.selected_segments.get_value()])
        return original(seq, destination, ripple=ripple)

    monkeypatch.setattr(fsc, "take_selection", spy)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert when_called == [["A"]]  # the audio segment and B were not part of the call
    assert audio.selected.get_value() is True  # and it is selected again
    assert [str(s.name.get_value()) for s in rig.handles["s1"].selected_segments.get_value()] == [
        "B",
        "AUD",
    ]
    assert env["result"]["selection"] == {"before": 2, "changed": 2, "after": 2, "error": None}
    assert layout(rig.handles["s1"])[2] == [VIDEO, 16, 39]  # the audio was not taken with it


def test_an_audio_segment_the_api_cannot_deselect_stops_the_call_before_it_is_made(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = with_audio(rig, selected=True)
    monkeypatch.setattr(PySegment, "selected_is_attribute", False)  # assignment is all there is
    original: Any = PySegment.__dict__["selected"]

    def stubborn(self: PySegment, value: Any) -> None:
        if self is not audio:
            original.fset(self, value)

    monkeypatch.setattr(PySegment, "selected", property(original.fget, stubborn))
    before = rows(rig.handles["s1"])
    env = delete(rig, [10])
    assert (
        env["errorCode"] == "FLAME_API_ERROR"
        and "PySequence.selected_segments holds 2" in env["error"]
    )
    assert env["result"]["ran"] is False and rows(rig.handles["s1"]) == before


def test_a_selected_transition_refuses_the_call_before_anything_is_copied(rig: Rig) -> None:
    rig.handles["s1"].versions[0].tracks[0]._selected_transitions.append(object())
    env = delete(rig, [10])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "1 selected in PySequence.selected_transitions" in env["error"]
    assert "1 selected in PyTrack.selected_transitions (version 0, track 0)" in env["error"]
    assert "nothing was done" in env["error"] and untouched_world(rig)


def test_a_selected_audio_transition_or_marker_refuses_it_too(rig: Rig) -> None:
    audio = with_audio(rig)
    rig.handles["s1"].audio_tracks[0]._selected_transitions.append(object())
    rig.handles["s1"]._selected_markers.append(object())
    env = delete(rig, [10])
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "1 selected in PySequence.selected_markers" in env["error"]
    assert "PyAudioTrack.selected_transitions (audio track 0)" in env["error"]
    assert untouched_world(rig) and audio.selected.get_value() is False


def test_a_selection_list_that_cannot_be_read_is_not_guessed_at(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: PySequence) -> list[Any]:
        raise RuntimeError("scripted read failure")

    monkeypatch.setattr(PySequence, "selected_markers", property(broken))
    env = delete(rig, [10])
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "PySequence.selected_markers cannot be read (RuntimeError: scripted read" in env["error"]
    assert untouched_world(rig)


def test_targets_that_stay_because_the_call_did_nothing_get_their_old_selection_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", lambda *a, **k: None)
    select(rig.handles["s1"], (0, 1), (0, 3))  # the user had A (the target) and B selected
    env = delete(rig, [10])
    assert env["errorCode"] == "SILENT_NOOP"
    assert selected(rig.handles["s1"]) == [(0, 0, 10), (0, 0, 40)]


def test_targets_that_stay_because_the_call_raised_get_their_old_selection_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explodes(self: PySequence, destination: Any = None, *a: Any) -> Any:
        raise RuntimeError("scripted extract failure")

    monkeypatch.setattr(PySequence, "extract_selection_to_media_panel", explodes)
    select(rig.handles["s1"], (0, 1))
    delete(rig, [10])
    assert selected(rig.handles["s1"]) == [(0, 0, 10)]


def test_one_segment_that_cannot_be_followed_does_not_lose_the_selections_after_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = media("clip9", 64, 70, name="Z")
    track = rig.handles["s1"]._versions[0]._tracks[0]
    track._segments.append(extra)
    extra.parent = track
    select(rig.handles["s1"], (0, 3), (0, 4), (1, 0))  # B, Z and C

    original = fsc.take_selection

    def renames_b(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
        made = original(seq, destination, ripple=ripple)
        seq.versions[0].tracks[0].segments[2].name.set_value("renamed")  # B is not B
        return made

    monkeypatch.setattr(fsc, "take_selection", renames_b)
    env = delete(rig, [10])
    assert env["ok"] is True, env
    # B cannot be followed; Z (behind it) and C (another track) still are
    assert selected(rig.handles["s1"]) == [(0, 0, 40), (0, 1, 0)]
    assert env["result"]["selection"]["error"] is None


def test_the_selection_members_are_attributes_that_cannot_be_iterated_as_on_flame(
    rig: Rig,
) -> None:
    """Measured on Flame 2025.2.4: iterating one raises TypeError, get_value() holds the list."""
    s1 = rig.handles["s1"]
    track = s1.versions[0].tracks[0]
    with_audio(rig, selected=True)
    members = [
        s1.selected_segments,
        s1.selected_transitions,
        s1.selected_markers,
        track.selected_segments,
        track.selected_transitions,
        s1.audio_tracks[0].selected_segments,
        s1.audio_tracks[0].selected_transitions,
    ]
    for member in members:
        assert type(member).__name__ == "PyAttribute"
        with pytest.raises(TypeError):
            list(member)
        assert isinstance(member.get_value(), list)
    assert [s.name.get_value() for s in s1.selected_segments.get_value()] == ["AUD"]


def test_a_flame_that_answers_plain_lists_works_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake_media_tree, "SELECTION_MEMBERS_AS_ATTRIBUTES", False)
    with_audio(rig, selected=True)
    select(rig.handles["s1"], (0, 3))
    env = delete(rig, [10])
    assert env["ok"] is True, env
    assert env["result"]["selection"] == {"before": 2, "changed": 2, "after": 2, "error": None}


def test_a_clean_sequence_is_not_refused_for_its_selection_attributes(rig: Rig) -> None:
    """The first real-Flame run refused a clean sequence because the members were iterated."""
    env = delete(rig, [10])
    assert env["ok"] is True, env
