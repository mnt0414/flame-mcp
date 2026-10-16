"""Tests `segment.trim`, the first write that runs behind a Timeline checkpoint.

The sign of the offset, ripple on one track, the refusals that leave nothing behind, the judgement
by reading the segment back (never by the return value), the position that can be addressed next,
and what the journal holds. Run through the real Runtime with a scripted `transport.exec` over the
fake Flame tree. The fake's trim follows what was measured on Flame 2025.2.4: the return value is
False even when the trim worked, and a ripple moves only the trimmed track
(fake_media_tree.PySegment._trim)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PySegment
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from timeline_p32a_helpers import (
    CLIPX,
    S1,
    S1_WT,
    TRIM,
    a_seg,
    checkpoints,
    fingerprint_of_target,
    library_copy,
    make_timeline_rig,
    reel_names,
    rows,
    seg_sel,
    stash_entries,
    stash_library,
    trim,
    uid,
    wt,
)

GAP = "Gap"
VIDEO = "Video Segment"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def track0(rig: Rig) -> list[list[Any]]:
    return rows(rig.handles["s1"], 0)


def untouched_world(rig: Rig) -> bool:
    """Nothing was copied, journaled or touched."""
    return (
        stash_library(rig) is None
        and rig.records() == []
        and rig.flame.media_panel.copy_calls == []
    )


def test_a_positive_head_offset_shortens_the_head_and_leaves_a_gap_before(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is True, env
    assert track0(rig)[:3] == [
        [GAP, 0, 11, 0, 11, "infinite", "infinite"],
        [VIDEO, 12, 33, 7, 28, 7, 5],
        [GAP, 34, 39, 0, 5, "infinite", "infinite"],
    ]
    result = env["result"]
    assert (
        result["before"]["recordIn"]["frame"] == 10 and result["after"]["recordIn"]["frame"] == 12
    )
    assert result["before"]["head"] == 5 and result["after"]["head"] == 7
    assert (
        result["deltas"]
        == result["expectedDeltas"]
        == {
            "recordIn": 2,
            "recordOut": 0,
            "sourceIn": 2,
            "sourceOut": 0,
            "head": 2,
            "tail": 0,
        }
    )


def test_a_positive_tail_offset_shortens_the_tail_and_leaves_a_gap_after(rig: Rig) -> None:
    env = trim(rig, a_seg(), "tail", 3)
    assert env["ok"] is True, env
    assert track0(rig)[1:3] == [
        [VIDEO, 10, 30, 5, 25, 5, 8],
        [GAP, 31, 39, 0, 8, "infinite", "infinite"],
    ]
    assert env["result"]["deltas"] == {
        "recordIn": 0,
        "recordOut": -3,
        "sourceIn": 0,
        "sourceOut": -3,
        "head": 0,
        "tail": 3,
    }


def test_a_negative_offset_extends_into_the_handle(rig: Rig) -> None:
    head = trim(rig, a_seg(), "head", -3)
    assert head["ok"] is True, head
    assert track0(rig)[:2] == [
        [GAP, 0, 6, 0, 6, "infinite", "infinite"],
        [VIDEO, 7, 33, 2, 28, 2, 5],
    ]
    tail = trim(rig, a_seg(), "tail", -4)
    assert tail["ok"] is True, tail
    assert track0(rig)[1:3] == [
        [VIDEO, 7, 37, 2, 32, 2, 1],
        [GAP, 38, 39, 0, 1, "infinite", "infinite"],
    ]
    assert tail["result"]["deltas"]["recordOut"] == 4 and tail["result"]["deltas"]["tail"] == -4


def test_an_extension_as_long_as_the_handle_is_allowed(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", -5)
    assert env["ok"] is True and track0(rig)[1][5] == 0


def test_a_trim_that_has_no_gap_before_it_makes_one_and_the_index_moves(rig: Rig) -> None:
    env = trim(rig, seg_sel(S1, 1, index=0), "head", 5)
    assert env["ok"] is True, env
    assert rows(rig.handles["s1"], 1) == [
        [GAP, 0, 4, 0, 4, "infinite", "infinite"],
        [VIDEO, 5, 47, 8, 50, 8, 2],
    ]
    position = env["result"]["position"]
    assert (position["version"], position["track"], position["index"]) == (0, 1, 1)
    assert position["recordIn"] == 5


def test_ripple_closes_up_only_the_trimmed_track_and_the_other_track_stays(rig: Rig) -> None:
    other = rows(rig.handles["s1"], 1)
    env = trim(rig, a_seg(), "tail", 3, ripple=True)
    assert env["ok"] is True, env
    assert track0(rig) == [
        [GAP, 0, 9, 0, 9, "infinite", "infinite"],
        [VIDEO, 10, 30, 5, 25, 5, 8],
        [GAP, 31, 36, 0, 5, "infinite", "infinite"],
        [VIDEO, 37, 60, 0, 23, 0, 0],
        [GAP, 61, 63, 0, 2, "infinite", "infinite"],
    ]
    assert rows(rig.handles["s1"], 1) == other
    assert env["result"]["track"]["trailingGap"] == {"recordIn": 61, "recordOut": 63}
    assert env["result"]["trackEnds"] == [
        {"track": 0, "endFrame": 63},
        {"track": 1, "endFrame": 47},
    ]


def test_a_ripple_head_trim_keeps_record_in_and_moves_record_out(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", 2, ripple=True)
    assert env["ok"] is True, env
    assert track0(rig)[1] == [VIDEO, 10, 31, 7, 28, 7, 5]
    assert env["result"]["deltas"] == {
        "recordIn": 0,
        "recordOut": -2,
        "sourceIn": 2,
        "sourceOut": 0,
        "head": 2,
        "tail": 0,
    }
    assert env["result"]["position"]["recordIn"] == 10


def test_a_ripple_extension_pushes_the_rest_of_the_track_out(rig: Rig) -> None:
    env = trim(rig, a_seg(), "tail", -2, ripple=True)
    assert env["ok"] is True, env
    assert track0(rig)[1:] == [
        [VIDEO, 10, 35, 5, 30, 5, 3],
        [GAP, 36, 41, 0, 5, "infinite", "infinite"],
        [VIDEO, 42, 65, 0, 23, 0, 0],
    ]


def test_ripple_defaults_to_false(rig: Rig) -> None:
    trim(rig, a_seg(), "tail", 3)
    assert track0(rig)[3] == [VIDEO, 40, 63, 0, 23, 0, 0]


def test_an_extension_beyond_the_handle_is_refused_with_what_is_available(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", -6)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["handle"] == 5 and env["result"]["ran"] is False
    assert "head handle is 5" in env["error"]
    assert untouched_world(rig) and track0(rig)[1][5] == 5


def test_a_tail_extension_beyond_its_own_handle_is_refused(rig: Rig) -> None:
    env = trim(rig, seg_sel(S1, 1, index=0), "tail", -3)  # C: tail 2
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["handle"] == 2
    assert untouched_world(rig)


def test_a_gap_is_refused(rig: Rig) -> None:
    env = trim(rig, a_seg(0), "head", 1)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "Gap" in env["error"]
    assert untouched_world(rig)


def test_a_trim_of_the_whole_length_or_more_is_refused_and_one_frame_short_is_not(rig: Rig) -> None:
    for offset in (24, 30):
        env = trim(rig, a_seg(), "tail", offset)
        assert env["errorCode"] == "PRECONDITION_UNMET" and "leave nothing" in env["error"]
    assert untouched_world(rig)
    assert trim(rig, a_seg(), "tail", 23)["ok"] is True


def test_a_segment_of_a_sequence_in_a_library_is_refused(rig: Rig) -> None:
    copy = library_copy(rig)
    before = rows(copy)
    env = trim(rig, seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1), "head", 2)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert rows(copy) == before and checkpoints(rig) == [] and rig.records() == []


def test_a_bad_edge_or_a_zero_offset_is_invalid_args_and_touches_nothing(rig: Rig) -> None:
    assert trim(rig, a_seg(), "middle", 2)["errorCode"] == "INVALID_ARGS"
    zero = trim(rig, a_seg(), "head", 0)
    assert zero["errorCode"] == "INVALID_ARGS" and "must not be 0" in zero["error"]
    assert trim(rig, a_seg(), "head", 1.5)["errorCode"] == "INVALID_ARGS"  # type: ignore[arg-type]
    assert untouched_world(rig)


def test_a_segment_that_is_not_there_or_is_the_wrong_one_is_refused(rig: Rig) -> None:
    assert trim(rig, a_seg(9), "head", 1)["errorCode"] == "SELECTOR_EMPTY"
    wrong = seg_sel(S1, 0, index=1)
    wrong["expect_source_name"] = "clip2"
    assert trim(rig, wrong, "head", 1)["errorCode"] == "CONFLICT"
    assert untouched_world(rig)


def test_the_return_value_is_reported_but_never_used(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is True and env["result"]["returned"] is False  # Flame answers False


def test_a_trim_that_changes_nothing_is_a_silent_noop_even_when_flame_says_true(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySegment, "_trim", lambda self, edge, offset, ripple: True)
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert env["result"]["returned"] is True and "nothing in the track changed" in env["error"]
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.state() == "failed" and view.outcome is not None and view.outcome["ok"] is False


def test_a_change_that_is_not_the_measured_one_is_an_error_with_before_and_after(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def odd(self: PySegment, edge: str, offset: int, ripple: bool) -> bool:
        self._set_record(self.record_in.frame + offset + 1, self.record_out.frame)
        return False

    monkeypatch.setattr(PySegment, "_trim", odd)
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "no segment starts at record_in 12" in env["error"]
    assert env["result"]["before"]["recordIn"]["frame"] == 10
    assert env["journal"]["revert"]["op"] == "journal.revert"  # still revertible


def test_a_segment_that_changed_in_another_way_reports_the_two_deltas(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def odd(self: PySegment, edge: str, offset: int, ripple: bool) -> bool:
        self.head = int(self.head) + offset + 3  # the head moves more than asked
        self.source_in = self.source_in.__class__(self.source_in.frame + offset)
        self._set_record(self.record_in.frame + offset, self.record_out.frame)
        return False

    monkeypatch.setattr(PySegment, "_trim", odd)
    env = trim(rig, a_seg(), "head", 2)
    assert env["errorCode"] == "FLAME_API_ERROR" and "not the way" in env["error"]
    assert env["result"]["deltas"]["head"] == 5 and env["result"]["expectedDeltas"]["head"] == 2


def test_the_position_is_a_selector_the_next_edit_can_use(rig: Rig) -> None:
    first = trim(rig, seg_sel(S1, 0, record_in=10), "head", 2)
    assert first["ok"] is True, first
    position = first["result"]["position"]
    assert position["selector"] == {
        "kind": "segment",
        "parent": S1,
        "version": 0,
        "track": 0,
        "record_in": 12,
        "expect_source_name": "clip1",
    }
    info = rig.do("segment.info", {"target": position["selector"]})
    assert info["ok"] is True and info["result"]["recordIn"]["frame"] == 12
    by_index = rig.do("segment.info", {"target": position["selectorByIndex"]})
    assert by_index["result"]["recordIn"]["frame"] == 12
    second = trim(rig, position["selector"], "tail", 1)
    assert second["ok"] is True and second["result"]["position"]["recordIn"] == 12


def test_the_track_summary_names_the_end_and_the_trailing_gap(rig: Rig) -> None:
    env = trim(rig, a_seg(), "tail", 3)
    assert env["result"]["trackBefore"] == {
        "track": 0,
        "segmentCount": 4,
        "startFrame": 0,
        "endFrame": 63,
        "trailingGap": None,
    }
    assert env["result"]["track"]["segmentCount"] == 4 and env["result"]["track"]["endFrame"] == 63


def test_a_segment_inside_a_clip_on_the_desktop(rig: Rig) -> None:
    env = trim(rig, seg_sel(CLIPX, 0, index=0), "tail", 2)
    assert env["ok"] is True, env
    assert rows(rig.handles["clipx"])[0][2] == 21
    revert = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert revert["ok"] is True and rows(_clip(rig))[0][2] == 23


def _clip(rig: Rig) -> Any:
    return next(c for c in rig.handles["reel"].children if str(c.name.get_value()) == "CLIPX")


def test_a_selection_parent_is_pinned_to_the_checkpointed_sequence(rig: Rig) -> None:
    rig.flame.media_panel.selected_entries = [rig.handles["s1"]]
    calls = {"after_checkpoint": False}

    def switch(label: str | None) -> None:
        if label == "timeline.checkpoint":
            calls["after_checkpoint"] = True
        elif label == TRIM and calls["after_checkpoint"]:
            rig.flame.media_panel.selected_entries = [rig.handles["s2"]]

    rig.exec.before_run = switch
    env = trim(rig, seg_sel({"kind": "selection"}, 0, index=1), "tail", 3)
    assert env["ok"] is True, env
    assert track0(rig)[1][2] == 30  # S1 was edited, not the sequence the selection moved to
    assert rows(rig.handles["s2"]) == [[VIDEO, 0, 23, 0, 23, 0, 0]]
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.intent["target"] == wt(S1_WT)


def test_a_trim_takes_a_checkpoint_first_and_journals_an_intent_and_an_outcome(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = trim(rig, a_seg(), "head", 2)
    after = fingerprint_of_target(rig, S1)
    assert before != after
    ref = env["journal"]
    [record] = checkpoints(rig)
    assert record["id"] == ref["checkpoint"] and record["fingerprint"] == before
    assert record["group"] == ref["group"] and record["kind"] == "timeline"
    assert ref["changed"] is True and ref["stash"]["uid"] == record["stash"]["uid"]
    assert ref["checkpointPath"] == f"{record['stash']['library']}/{record['stash']['reel']}"
    assert ref["revert"] == {"op": "journal.revert", "args": {"group": ref["group"]}}
    [view] = Index(rig.records()).groups[ref["group"]]
    intent = view.intent
    assert intent["op"] == TRIM and intent["kind"] == "trim" and intent["targetKind"] == "timeline"
    assert intent["target"] == wt(S1_WT) and intent["targetUid"] == uid(rig.handles["s1"])
    assert intent["fingerprintBefore"] == before and intent["revertCheckpoint"] == record["id"]
    assert intent["args"]["edge"] == "head" and intent["args"]["offset"] == 2
    assert view.outcome is not None and view.outcome["ok"] is True
    assert view.outcome["fingerprintAfter"] == after and view.outcome["changed"] is True
    assert view.outcome["targetUid"] == uid(rig.handles["s1"])
    labels = [c.label for c in rig.exec.calls]
    assert labels[1:5] == [TRIM, "timeline.checkpoint", TRIM, "timeline.fingerprint"]


def test_the_intent_is_flushed_before_the_trim_is_sent(rig: Rig) -> None:
    trim(rig, a_seg(), "head", 2)
    write_call = [c for c in rig.exec.calls if c.label == TRIM][-1]
    assert write_call.journal_types[-1] == "intent" and "checkpoint" in write_call.journal_types


def test_the_envelope_says_undo_is_a_checkpoint_and_native_undo_is_not_used(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", 2)
    assert env["execution"]["undo"] == "checkpoint" and env["undoSteps"] == 0
    assert env["execution"]["thread"] == "main" and env["execution"]["effect"] == "reversible"


def test_the_stash_holds_the_sequence_as_it_was_and_the_original_is_edited(rig: Rig) -> None:
    before = track0(rig)
    trim(rig, a_seg(), "head", 2)
    [entry] = stash_entries(rig)
    assert rows(entry) == before and track0(rig) != before
    assert len(reel_names(stash_library(rig))) == 1


def test_a_failure_after_the_checkpoint_names_the_checkpoint_and_the_revert(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySegment, "_trim", lambda self, edge, offset, ripple: False)
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is False
    assert env["journal"]["checkpoint"] in env["hint"] and "journal.revert" in env["hint"]


def test_an_answer_that_never_came_blocks_writes_until_journal_check(rig: Rig) -> None:
    rig.exec.fault(
        TRIM, "TIMEOUT", when="after", skip=1
    )  # the check phase passes, the write is lost
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    assert "journal.check" in env["hint"] and rig.runtime.guard.denial() is not None
    assert track0(rig)[1][1] == 12  # the fake did run it
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "diverged" and rig.runtime.guard.denial() is None


def test_a_checkpoint_that_cannot_be_taken_stops_the_trim_before_it_is_sent(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = type(rig.handles["libreel"]).receive

    def lossy(self: Any, sources: list[Any]) -> list[Any]:
        copies: list[Any] = original(self, sources)
        copies[0]._versions[0]._tracks[0]._segments[1].source_name = "somewhere else"
        return copies

    monkeypatch.setattr(type(rig.handles["libreel"]), "receive", lossy)
    before = track0(rig)
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "could not be taken" in env["error"] and "NOT deleted" in env["error"]
    assert env["result"]["ran"] is False and track0(rig) == before
    assert [r for r in rig.records() if r["type"] == "intent"] == []
    assert [c.label for c in rig.exec.calls].count(TRIM) == 1  # the check only, never the write


def test_a_journal_that_cannot_be_written_stops_the_trim_before_it_is_sent(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_append = rig.journal.append

    def refuse_intent(record: dict[str, Any]) -> None:
        if record["type"] == "intent":
            raise OSError("read-only volume")
        real_append(record)

    monkeypatch.setattr(rig.journal, "append", refuse_intent)
    before = track0(rig)
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert "without an intent on record" in env["error"] and track0(rig) == before
    assert [c.label for c in rig.exec.calls].count(TRIM) == 1


def test_segment_trim_is_declared_as_a_timeline_checkpointed_write(rig: Rig) -> None:
    op = rig.registry.get(TRIM)
    assert op is not None and op.checkpoint is not None
    assert op.checkpoint.target_kind == "timeline"
    assert (op.effect, op.undo, op.thread, op.category) == (
        "reversible",
        "checkpoint",
        "main",
        "segment",
    )
    assert op.accepts == ("segment",) and op.wrap is None and op.journal is None
    for word in ("shortens", "extends", "ripple", "other tracks", "journal.revert", "uid"):
        assert word in op.description
    assert [p.name for p in op.params] == ["target", "edge", "offset", "ripple"]


def test_plan_run_bundles_a_checkpointed_timeline_write(rig: Rig) -> None:
    """A plan holding a Timeline-checkpointed step is one checkpointed write.

    The bundle's own tests are in test_plan_timeline_p32b.py."""
    before = fingerprint_of_target(rig, S1)
    env = rig.do("plan.run", {"steps": [{"op": TRIM, "args": a_args()}]})
    assert env["ok"] is True, env
    assert env["result"]["timeline"]["fingerprintBefore"] == before
    assert len(checkpoints(rig)) == 1


def a_args() -> dict[str, Any]:
    return {"target": a_seg(), "edge": "head", "offset": 2}


def test_readonly_denies_the_trim_and_nothing_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    assert trim(ro, a_seg(), "head", 2)["errorCode"] == "POLICY_DENIED"
    assert untouched_world(ro)


def test_a_denied_engine_op_is_refused_up_front(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    only = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": "segment"})
    )
    env = trim(only, a_seg(), "head", 2)
    assert env["errorCode"] == "POLICY_DENIED" and "timeline.checkpoint" in env["error"]
    assert untouched_world(only)


def test_the_stable_target_is_addressed_by_wiretap_id_in_the_write(rig: Rig) -> None:
    trim(rig, a_seg(), "head", 2)
    write = [c.code for c in rig.exec.calls if c.label == TRIM][-1]
    assert repr(S1_WT) in write and "_resolve_selection" not in write.split("__op_src = ")[1]
