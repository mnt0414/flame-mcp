"""Tests `segment.slip`.

The sign (offset > 0 takes source_in DOWN), the handle that stops a slip without a word and so is
checked up front, the judgement by reading the segment back, and what the journal holds. Run
through the real Runtime with a scripted `transport.exec` over the fake Flame tree; the fake's
slip follows what was measured on Flame 2025.2.4: slip(n) moves source_in n frames earlier and
stops at the handle without an error (fake_segment_edit.seg_slip)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PySegment, PyTime
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import (
    SLIP,
    a_seg,
    checkpoints,
    seg_a,
    seg_row,
    sel_at,
    untouched_world,
)
from timeline_p32a_helpers import (
    S1,
    S1_WT,
    fingerprint_of_target,
    library_copy,
    make_timeline_rig,
    media,
    rows,
    seg_sel,
    stash_entries,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def slip(rig: Rig, selector: dict[str, Any], offset: int) -> dict[str, Any]:
    return rig.do(SLIP, {"target": selector, "offset": offset})


def test_a_positive_offset_takes_the_source_toward_its_head_and_the_record_stays(rig: Rig) -> None:
    env = slip(rig, a_seg(), 3)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 10, 33, 2, 25, 2, 8]
    result = env["result"]
    assert result["requested"] == 3 and result["moved"] == 3 and result["returned"] is False
    assert result["before"]["sourceIn"]["frame"] == 5 and result["after"]["sourceIn"]["frame"] == 2
    assert (
        result["deltas"]
        == result["expectedDeltas"]
        == {
            "recordIn": 0,
            "recordOut": 0,
            "sourceIn": -3,
            "sourceOut": -3,
            "head": -3,
            "tail": 3,
        }
    )


def test_a_negative_offset_takes_the_source_toward_its_tail(rig: Rig) -> None:
    env = slip(rig, a_seg(), -4)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 10, 33, 9, 32, 9, 1]
    assert env["result"]["moved"] == -4 and env["result"]["deltas"]["head"] == 4


def test_a_slip_as_long_as_the_handle_is_allowed(rig: Rig) -> None:
    head = slip(rig, a_seg(), 5)
    assert head["ok"] is True and seg_row(seg_a(rig))[5:] == [0, 10]
    tail = slip(rig, a_seg(), -10)
    assert tail["ok"] is True and seg_row(seg_a(rig))[5:] == [10, 0]


def test_the_time_values_come_with_frame_and_timecode(rig: Rig) -> None:
    result = slip(rig, a_seg(), 3)["result"]
    assert result["before"]["sourceIn"] == {"timecode": "00:00:00+05", "frame": 5}
    assert result["after"]["sourceIn"] == {"timecode": "00:00:00+02", "frame": 2}
    assert result["after"]["recordIn"] == {"timecode": "00:00:00+10", "frame": 10}


def test_an_infinite_handle_is_never_short(rig: Rig) -> None:
    seq = rig.handles["s2"]
    seq._versions[0]._tracks[0]._segments[:] = [
        media("clip1", 0, 23, source_in=100, head="infinite", tail="infinite")  # type: ignore[arg-type]
    ]
    env = slip(rig, seg_sel(wt(seq.get_wiretap_node_id()), 0, index=0), 1000)
    assert env["ok"] is True, env
    assert seg_row(seq._versions[0]._tracks[0]._segments[0])[3] == -900
    assert env["result"]["deltas"]["head"] == 0 and env["result"]["deltas"]["tail"] == 0


def test_a_slip_past_the_head_handle_is_refused_with_what_is_available(rig: Rig) -> None:
    env = slip(rig, a_seg(), 6)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["available"] == 5 and env["result"]["handle"] == {"head": 5, "tail": 5}
    assert env["result"]["ran"] is False and "head handle is 5" in env["error"]
    assert untouched_world(rig) and seg_row(seg_a(rig))[3] == 5


def test_a_slip_past_the_tail_handle_is_refused(rig: Rig) -> None:
    env = slip(rig, sel_at(0, track=1), -3)  # C: head 3, tail 2
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["available"] == 2
    assert "tail handle is 2" in env["error"] and untouched_world(rig)


def test_a_gap_is_refused(rig: Rig) -> None:
    env = slip(rig, sel_at(0), 1)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "Gap" in env["error"]
    assert untouched_world(rig)


def test_a_segment_of_a_sequence_in_a_library_is_refused(rig: Rig) -> None:
    copy = library_copy(rig)
    before = rows(copy)
    env = slip(rig, seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1), 1)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert "checkpointError" not in env["result"]  # the op's own check refused, not the checkpoint
    assert rows(copy) == before and checkpoints(rig) == [] and rig.records() == []


def test_a_zero_or_fractional_offset_is_invalid_args_and_touches_nothing(rig: Rig) -> None:
    zero = slip(rig, a_seg(), 0)
    assert zero["errorCode"] == "INVALID_ARGS" and "must not be 0" in zero["error"]
    assert slip(rig, a_seg(), 1.5)["errorCode"] == "INVALID_ARGS"  # type: ignore[arg-type]
    assert untouched_world(rig)


def test_a_segment_that_is_not_there_or_is_the_wrong_one_is_refused(rig: Rig) -> None:
    assert slip(rig, sel_at(9), 1)["errorCode"] == "SELECTOR_EMPTY"
    wrong = seg_sel(S1, 0, index=1)
    wrong["expect_source_name"] = "clip2"
    assert slip(rig, wrong, 1)["errorCode"] == "CONFLICT"
    assert untouched_world(rig)


def test_a_slip_that_changes_nothing_is_a_silent_noop_even_when_flame_says_true(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySegment, "slip", lambda self, offset, *a, **k: True)
    env = slip(rig, a_seg(), 2)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert env["result"]["returned"] is True and "nothing in the track changed" in env["error"]
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.state() == "failed" and view.outcome is not None
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_a_slip_by_another_amount_is_an_error_with_before_and_after(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def half(self: PySegment, offset: int, *args: Any, **kwargs: Any) -> bool:
        n = offset // 2
        self.source_in = PyTime(self.source_in.frame - n)
        self.source_out = PyTime(self.source_out.frame - n)
        self.head = int(self.head) - n
        self.tail = int(self.tail) + n
        return False

    monkeypatch.setattr(PySegment, "slip", half)
    env = slip(rig, a_seg(), 4)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "not by the 4 frame(s) asked for" in env["error"]
    assert env["result"]["moved"] == 2 and env["result"]["requested"] == 4
    assert env["result"]["before"]["sourceIn"]["frame"] == 5
    assert env["result"]["after"]["sourceIn"]["frame"] == 3
    assert env["journal"]["revert"]["op"] == "journal.revert"  # still revertible


def test_a_slip_that_moves_the_record_is_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def moves_record(self: PySegment, offset: int, *args: Any, **kwargs: Any) -> bool:
        self.source_in = PyTime(self.source_in.frame - offset)
        self._set_record(self.record_in.frame + 1, self.record_out.frame + 1)
        return False

    monkeypatch.setattr(PySegment, "slip", moves_record)
    env = slip(rig, a_seg(), 2)
    assert (
        env["errorCode"] == "FLAME_API_ERROR"
        and "no segment starts at record_in 10" in env["error"]
    )


def test_the_position_is_a_selector_the_next_edit_can_use(rig: Rig) -> None:
    first = slip(rig, seg_sel(S1, 0, record_in=10), 2)
    assert first["ok"] is True, first
    position = first["result"]["position"]
    assert position["selector"] == {
        "kind": "segment",
        "parent": S1,
        "version": 0,
        "track": 0,
        "record_in": 10,
        "expect_source_name": "clip1",
    }
    second = slip(rig, position["selector"], -1)
    assert second["ok"] is True and seg_row(seg_a(rig))[3] == 4


def test_the_track_summary_is_reported_and_other_tracks_stay(rig: Rig) -> None:
    other = rows(rig.handles["s1"], 1)
    env = slip(rig, a_seg(), 1)
    assert env["result"]["track"]["segmentCount"] == 4 and env["result"]["track"]["endFrame"] == 63
    assert rows(rig.handles["s1"], 1) == other


def test_a_revert_puts_the_sequence_back_with_the_fingerprint_it_had(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = slip(rig, a_seg(), 3)
    assert fingerprint_of_target(rig, S1) != before
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    restored = out["result"]["restored"]["selector"]
    assert fingerprint_of_target(rig, restored) == before


def test_a_revert_after_another_edit_is_a_conflict(rig: Rig) -> None:
    env = slip(rig, a_seg(), 3)
    slip(rig, a_seg(), 1)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"


def test_a_slip_takes_a_checkpoint_first_and_journals_an_intent_and_an_outcome(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = slip(rig, a_seg(), 3)
    ref = env["journal"]
    [record] = checkpoints(rig)
    assert record["id"] == ref["checkpoint"] and record["fingerprint"] == before
    assert ref["changed"] is True and ref["revert"]["args"] == {"group": ref["group"]}
    [view] = Index(rig.records()).groups[ref["group"]]
    assert view.intent["op"] == SLIP and view.intent["kind"] == "slip"
    assert view.intent["args"]["offset"] == 3 and view.intent["target"] == wt(S1_WT)
    assert view.intent["fingerprintBefore"] == before and view.outcome is not None
    assert view.outcome["ok"] is True and view.outcome["changed"] is True
    assert [c.label for c in rig.exec.calls][1:5] == [
        SLIP,
        "timeline.checkpoint",
        SLIP,
        "timeline.fingerprint",
    ]
    [entry] = stash_entries(rig)
    assert rows(entry)[1][3] == 5  # the stash still has the source as it was


def test_the_envelope_says_undo_is_a_checkpoint_and_native_undo_is_not_used(rig: Rig) -> None:
    env = slip(rig, a_seg(), 3)
    assert env["execution"]["undo"] == "checkpoint" and env["undoSteps"] == 0
    assert env["execution"]["thread"] == "main" and env["execution"]["effect"] == "reversible"


def test_segment_slip_is_declared_as_a_timeline_checkpointed_write(rig: Rig) -> None:
    op = rig.registry.get(SLIP)
    assert op is not None and op.checkpoint is not None
    assert op.checkpoint.target_kind == "timeline" and op.accepts == ("segment",)
    assert (op.effect, op.undo, op.thread) == ("reversible", "checkpoint", "main")
    for word in ("down", "head", "stops at the handle", "journal.revert", "segment.replace"):
        assert word in op.description
    assert [p.name for p in op.params] == ["target", "offset"]


def test_readonly_denies_the_slip_and_nothing_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    assert slip(ro, a_seg(), 2)["errorCode"] == "POLICY_DENIED"
    assert untouched_world(ro)


def test_an_answer_that_never_came_blocks_writes_until_journal_check(rig: Rig) -> None:
    rig.exec.fault(
        SLIP, "TIMEOUT", when="after", skip=1
    )  # the check phase passes, the write is lost
    env = slip(rig, a_seg(), 3)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    assert "journal.check" in env["hint"] and rig.runtime.guard.denial() is not None
    assert seg_row(seg_a(rig))[3] == 2  # the fake did run it
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "diverged" and rig.runtime.guard.denial() is None


def test_a_slip_that_never_ran_is_not_landed_when_checked(rig: Rig) -> None:
    rig.exec.fault(SLIP, "BRIDGE_LOST", when="before", skip=1)
    assert slip(rig, a_seg(), 3)["ok"] is False
    assert seg_row(seg_a(rig))[3] == 5
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "not_landed"
