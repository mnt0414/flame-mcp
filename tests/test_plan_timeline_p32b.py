"""Tests `plan.run` with a Timeline-checkpointed step.

It is ONE checkpointed write on one sequence -- one stashed copy, the segment selectors resolved
up front (and `late: true` for the ones that cannot be), one restore for `journal.revert`, and the
refusals of what would mix the ways to put things back. Run through the real Runtime with a
scripted `transport.exec` over the fake Flame tree. The fake's trim follows what was measured on
Flame 2025.2.4: the return value is False even when the trim worked, and a ripple moves only the
trimmed track (fake_media_tree.PySegment._trim)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr
from journal_test_helpers import Rig
from p32b_helpers import PLAN, S1, S2, make_p32b_rig, uid
from timeline_p32a_helpers import (
    CHECKPOINT,
    S1_WT,
    TRIM,
    checkpoints,
    fingerprint_of_target,
    reel_names,
    rows,
    seg_sel,
    stash_entries,
    stash_library,
    wt,
)

SET = "segment.set_attributes"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_p32b_rig(monkeypatch, tmp_path)
    seg = made.handles["s1"].versions[0].tracks[0].segments[1]  # A (record_in 10)
    seg.comment = FakeAttr("old")
    seg.attributes.append("comment")
    made.handles["seg_a"] = seg
    return made


def at(record_in: int, track: int = 0, parent: dict[str, Any] = S1, **extra: Any) -> dict[str, Any]:
    return seg_sel(parent, track, record_in=record_in) | extra


def trim_step(
    record_in: int, edge: str = "head", offset: int = 2, track: int = 0, **extra: Any
) -> dict[str, Any]:
    args = {"target": at(record_in, track), "edge": edge, "offset": offset}
    return {"op": TRIM, "args": args, **extra}


def three_trims() -> list[dict[str, Any]]:
    return [trim_step(10), trim_step(40, "tail"), trim_step(0, "head", 1, track=1)]


def plan(rig: Rig, steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return rig.do(PLAN, {"steps": steps, **extra})


def s1_rows(rig: Rig, track: int = 0) -> list[list[Any]]:
    return rows(rig.handles["s1"], track)


def nothing_happened(rig: Rig) -> bool:
    return (
        stash_library(rig) is None
        and rig.records() == []
        and rig.flame.media_panel.copy_calls == []
        and rig.flame.media_panel.move_calls == []
    )


def test_three_trims_take_one_checkpoint_and_run_without_checkpoints_of_their_own(
    rig: Rig,
) -> None:
    before = fingerprint_of_target(rig, S1)
    env = plan(rig, three_trims(), label="three trims")
    assert env["ok"] is True, env
    assert [s["ok"] for s in env["result"]["steps"]] == [True, True, True]
    assert env["result"]["steps"][0]["result"]["position"]["recordIn"] == 12
    assert len(checkpoints(rig)) == 1 and len(stash_entries(rig)) == 1
    assert [c.label for c in rig.exec.calls].count("timeline.checkpoint") == 1
    assert env["undoSteps"] == 0 and env["partial"] == {"done": 3, "failed": 0, "notRun": 0}
    timeline = env["result"]["timeline"]
    assert timeline["fingerprintBefore"] == before
    assert timeline["fingerprintAfter"] == fingerprint_of_target(rig, S1) != before
    assert timeline["checkpoint"] == checkpoints(rig)[0]["id"] and timeline["changed"] is True
    assert timeline["target"]["uid"] == uid(rig.handles["s1"])
    assert env["journal"] == {"group": env["result"]["group"]}


def test_the_edits_land_where_each_trim_was_aimed(rig: Rig) -> None:
    plan(rig, three_trims())
    assert s1_rows(rig, 0)[1][1:3] == [12, 33]  # A: head trim 2, no ripple
    assert [r[1:3] for r in s1_rows(rig, 0) if r[1] == 40] == [[40, 61]]  # B: tail trim 2
    assert s1_rows(rig, 1)[1][1:3] == [1, 47]  # C: head trim 1 on the other track


def test_the_journal_holds_the_group_the_checkpoint_the_intent_and_the_outcome(rig: Rig) -> None:
    env = plan(rig, three_trims())
    group = env["result"]["group"]
    kinds = [(r["type"], r.get("op") or r.get("kind")) for r in rig.records()]
    assert kinds == [
        ("group", "plan.run"),
        ("checkpoint", "timeline"),
        ("intent", "plan.run"),
        ("outcome", None),
    ]
    intent, outcome = rig.records()[2], rig.records()[3]
    assert intent["group"] == group and intent["targetKind"] == "timeline"
    assert intent["checkpoint"] == checkpoints(rig)[0]["id"]
    assert intent["fingerprintBefore"] == checkpoints(rig)[0]["fingerprint"]
    assert [s["op"] for s in intent["args"]["steps"]] == [TRIM] * 3
    assert outcome["ok"] is True and outcome["fingerprintAfter"] != intent["fingerprintBefore"]
    assert outcome["steps"] == [
        {"index": i, "op": TRIM, "ok": True, "errorCode": None} for i in range(3)
    ]
    write = next(c for c in rig.exec.calls if c.label == TRIM)
    assert write.journal_types == ["group", "checkpoint", "intent"]


def test_one_revert_puts_the_whole_plan_back(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    old_uid = uid(rig.handles["s1"])
    env = plan(rig, three_trims())
    moves, copies = len(rig.flame.media_panel.move_calls), len(rig.flame.media_panel.copy_calls)
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert len(rig.flame.media_panel.move_calls) == moves + 1
    assert len(rig.flame.media_panel.copy_calls) == copies + 1
    back = out["result"]["restored"]
    assert back["uid"] != old_uid and out["result"]["fingerprintMatches"] is True
    assert fingerprint_of_target(rig, back["selector"]) == before
    assert reel_names(rig.handles["reel"])[-1] == "S1"
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert states[env["result"]["group"]] == "reverted"
    again = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert again["errorCode"] == "PRECONDITION_UNMET" and "already reverted" in again["error"]


def test_a_set_attributes_step_on_the_same_sequence_comes_back_with_the_restore(rig: Rig) -> None:
    steps = [
        {"op": SET, "args": {"target": at(10), "values": {"comment": "new"}}},
        trim_step(10),
    ]
    env = plan(rig, steps)
    assert env["ok"] is True, env
    assert rig.handles["s1"].versions[0].tracks[0].segments[1].comment.get_value() == "new"
    assert len([c for c in rig.exec.calls if c.label == "timeline.checkpoint"]) == 1
    types = [(r["type"], r.get("op")) for r in rig.records() if r["type"] == "intent"]
    assert types == [("intent", "plan.run"), ("intent", SET)]
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    restored = rig.flame.find_by_uid(out["result"]["restored"]["uid"])
    assert restored.versions[0].tracks[0].segments[1].comment.get_value() == "old"
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert states[env["result"]["group"]] == "reverted"
    assert sum(1 for r in rig.records() if r.get("op") == SET) == 1  # no value write-back


def test_reads_of_any_sequence_may_share_the_plan(rig: Rig) -> None:
    steps = [
        {"op": "timeline.list_segments", "args": {"target": S2}},
        trim_step(10),
        {"op": "timeline.list_segments", "args": {"target": S1}},
    ]
    env = plan(rig, steps)
    assert env["ok"] is True, env
    after = env["result"]["steps"][2]["result"]
    assert "items" in after or "segments" in after


def test_selectors_of_other_shapes_that_name_the_same_sequence_are_one_target(rig: Rig) -> None:
    by_path = {"kind": "path", "path": ["RG", "Reel 1", "S1"], "root": "desktop"}
    steps = [trim_step(10), {"op": TRIM, "args": trim_step(40, "tail")["args"]}]
    steps[1]["args"]["target"]["parent"] = by_path
    env = plan(rig, steps)
    assert env["ok"] is True, env
    assert len(checkpoints(rig)) == 1


def test_the_group_record_names_the_late_steps(rig: Rig) -> None:
    plan(rig, [trim_step(10), trim_step(12, "tail", late=True)])
    assert rig.records()[0]["late"] == [1]


def test_a_step_that_does_not_resolve_stops_everything_and_leaves_no_trace(rig: Rig) -> None:
    before = s1_rows(rig)
    env = plan(rig, [trim_step(10), trim_step(9999)])
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert "step 1" in env["error"] and "plan not run" in env["error"]
    assert env["result"]["preResolution"][0]["index"] == 1
    assert env["result"]["ran"] is False and "late" in env["hint"]
    assert nothing_happened(rig) and s1_rows(rig) == before


def test_an_expected_source_name_that_differs_is_a_conflict_before_anything_runs(rig: Rig) -> None:
    step = trim_step(10)
    step["args"]["target"]["expect_source_name"] = "not-clip1"
    env = plan(rig, [trim_step(40, "tail"), step])
    assert env["errorCode"] == "CONFLICT" and "not-clip1" in env["error"]
    assert nothing_happened(rig)


def test_every_step_that_fails_to_resolve_is_listed(rig: Rig) -> None:
    env = plan(rig, [trim_step(9998), trim_step(10), trim_step(9999)])
    assert [i["index"] for i in env["result"]["preResolution"]] == [0, 2]
    assert "and 1 more" in env["error"]


def test_an_index_that_is_out_of_range_does_not_resolve(rig: Rig) -> None:
    step = {"op": TRIM, "args": {"target": seg_sel(S1, 0, index=77), "edge": "head", "offset": 1}}
    env = plan(rig, [step])
    assert env["errorCode"] == "SELECTOR_EMPTY" and nothing_happened(rig)


def test_the_sequence_itself_must_be_found(rig: Rig) -> None:
    step = trim_step(10)
    step["args"]["target"]["parent"] = wt("/nowhere")
    env = plan(rig, [step])
    assert env["errorCode"] == "SELECTOR_EMPTY" and nothing_happened(rig)


def test_a_sequence_inside_a_library_is_refused_before_anything_runs(rig: Rig) -> None:
    copy = rig.flame.media_panel.copy(rig.handles["s1"], rig.handles["libreel"])[0]
    step = trim_step(10)
    step["args"]["target"]["parent"] = wt(copy.get_wiretap_node_id())
    before_copies = len(rig.flame.media_panel.copy_calls)
    env = plan(rig, [step])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "library" in env["error"]
    assert len(rig.flame.media_panel.copy_calls) == before_copies and rig.records() == []


def moved_head_then_tail(**second: Any) -> list[dict[str, Any]]:
    """The first trim moves A's record_in from 10 to 12; the second points at 12."""
    return [trim_step(10), trim_step(12, "tail", **second)]


def test_without_late_a_step_that_points_at_a_segment_an_earlier_step_makes_is_refused(
    rig: Rig,
) -> None:
    env = plan(rig, moved_head_then_tail())
    assert env["errorCode"] == "SELECTOR_EMPTY" and "record_in 12" in env["error"]
    assert nothing_happened(rig)


def test_late_lets_that_step_through_and_it_is_resolved_when_its_turn_comes(rig: Rig) -> None:
    env = plan(rig, moved_head_then_tail(late=True))
    assert env["ok"] is True, env
    assert s1_rows(rig)[1][1:3] == [12, 31]
    assert env["result"]["timeline"]["resolved"][1]["late"] is True
    assert env["result"]["timeline"]["resolved"][1]["segment"] is None
    assert env["result"]["timeline"]["resolved"][0]["segment"]["recordIn"] == 10


def test_a_late_step_that_does_not_resolve_when_its_turn_comes_stops_the_plan_and_it_reverts(
    rig: Rig,
) -> None:
    before = fingerprint_of_target(rig, S1)
    env = plan(rig, [trim_step(10), trim_step(9999, late=True), trim_step(40, "tail")])
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 1}
    assert env["result"]["stoppedAt"] == 1
    assert s1_rows(rig)[1][1:3] == [12, 33]  # step 0 stays applied
    assert "journal.revert(group=" in env["hint"]
    outcome = next(r for r in rig.records() if r["type"] == "outcome")
    assert outcome["ok"] is False and outcome["stoppedAt"] == 1 and outcome["fingerprintAfter"]
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_a_trim_that_is_refused_by_the_op_when_its_turn_comes_stops_the_plan(rig: Rig) -> None:
    gap_step = trim_step(0)  # the Gap at record_in 0: no media to trim
    env = plan(rig, [trim_step(10), gap_step, trim_step(40, "tail")])
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["stoppedAt"] == 1
    assert s1_rows(rig)[1][1:3] == [12, 33]


def test_late_is_a_boolean_and_only_for_a_timeline_write_of_a_bundled_plan(rig: Rig) -> None:
    env = plan(rig, [trim_step(10, late="yes")])
    assert env["errorCode"] == "INVALID_ARGS" and "must be a boolean" in str(env["result"])
    read = {"op": "timeline.info", "args": {"target": S1}, "late": True}
    env = plan(rig, [trim_step(10), read])
    assert env["errorCode"] == "INVALID_ARGS" and "`late` applies only" in str(env["result"])
    env = plan(rig, [{"op": "timeline.info", "args": {"target": S1}, "late": True}])
    assert env["errorCode"] == "INVALID_ARGS" and nothing_happened(rig)


def test_a_trim_of_another_sequence_is_refused_and_nothing_runs(rig: Rig) -> None:
    other = {"op": TRIM, "args": {"target": at(0, parent=S2), "edge": "tail", "offset": 2}}
    env = plan(rig, [trim_step(10), other])
    assert env["errorCode"] == "INVALID_ARGS" and "2 different sequences" in env["error"]
    assert {t["uid"] for t in env["result"]["targets"]} == {
        uid(rig.handles["s1"]),
        uid(rig.handles["s2"]),
    }
    assert nothing_happened(rig)


def test_a_set_attributes_of_another_sequence_is_refused_too(rig: Rig) -> None:
    other = {"op": SET, "args": {"target": at(0, parent=S2), "values": {"name": "x"}}}
    env = plan(rig, [trim_step(10), other])
    assert env["errorCode"] == "INVALID_ARGS" and "different sequences" in env["error"]
    assert nothing_happened(rig)


@pytest.mark.parametrize(
    "step",
    [
        {"op": "desktop.rename", "args": {"target": S2, "name": "renamed"}},
        {
            "op": "batch.set_attributes",
            "args": {"target": {"kind": "batch", "name": "BG1"}, "values": {}},
        },
        {
            "op": "node.set_attributes",
            "args": {
                "target": {
                    "kind": "node",
                    "batch": {"kind": "batch", "name": "BG1"},
                    "name": "comp1",
                },
                "values": {},
            },
        },
    ],
    ids=["desktop.rename", "batch.set_attributes", "node.set_attributes"],
)
def test_another_kind_of_write_is_refused_for_its_own_way_back(
    rig: Rig, step: dict[str, Any]
) -> None:
    env = plan(rig, [trim_step(10), step])
    assert env["errorCode"] == "INVALID_ARGS"
    issues = env["result"]["issues"]
    assert [i["index"] for i in issues] == [1]
    assert "the ways of reverting differ" in issues[0]["problems"][0]
    assert nothing_happened(rig)


def test_a_batch_checkpointed_op_is_still_refused_in_a_plan(rig: Rig) -> None:
    step = {
        "op": "batch.create_reel",
        "args": {"target": {"kind": "batch", "name": "BG1"}, "name": "r"},
    }
    env = plan(rig, [trim_step(10), step])
    assert env["errorCode"] == "INVALID_ARGS"
    assert "cannot be a plan step" in str(env["result"]["issues"])
    assert nothing_happened(rig)


def test_timeline_checkpoint_and_the_other_timeline_ops_follow_the_same_rule(rig: Rig) -> None:
    step = {"op": CHECKPOINT, "args": {"target": S1}}
    env = plan(rig, [trim_step(10), step])
    assert env["errorCode"] == "INVALID_ARGS" and "the ways of reverting differ" in str(
        env["result"]
    )
    assert nothing_happened(rig)


def test_a_plan_without_a_timeline_step_is_not_bundled(rig: Rig) -> None:
    env = plan(rig, [{"op": "desktop.rename", "args": {"target": S2, "name": "renamed"}}])
    assert env["ok"] is True and "timeline" not in env["result"]
    assert checkpoints(rig) == [] and stash_library(rig) is None


def test_a_continue_plan_with_a_write_is_refused_as_before(rig: Rig) -> None:
    env = plan(rig, [trim_step(10)], on_error="continue")
    assert env["errorCode"] == "INVALID_ARGS" and "only allowed when every step is a read" in str(
        env["result"]
    )


def test_readonly_hides_the_trim_in_a_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from flame_mcp.policy import Policy

    rig = make_p32b_rig(monkeypatch, tmp_path)
    rig.runtime.policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    env = plan(rig, [trim_step(10)])
    assert env["errorCode"] == "POLICY_DENIED" and nothing_happened(rig)


def test_a_trim_that_ends_without_an_answer_stops_the_plan_and_the_check_reads_the_sequence(
    rig: Rig,
) -> None:
    rig.exec.fault(TRIM, "TIMEOUT", when="after", skip=1)  # the second trim ran, the answer is lost
    env = plan(rig, three_trims())
    assert env["errorCode"] == "TIMEOUT"
    assert env["result"]["failClosed"]["step"] == 1 and env["result"]["stoppedAt"] == 1
    assert "refuses writes until journal.check" in env["error"]
    assert [r[1:3] for r in s1_rows(rig) if r[1] == 40] == [[40, 61]]  # the second trim landed
    refused = rig.do("desktop.rename", {"target": S2, "name": "x"})
    assert refused["errorCode"] == "POLICY_DENIED"
    checked = rig.do("journal.check", {})
    entry = next(c for c in checked["result"]["checked"] if c["op"] == "plan.run")
    assert entry["verdict"] == "diverged" and entry["perKey"] == {"fingerprint": "other"}
    assert rig.do("desktop.rename", {"target": S2, "name": "x"})["ok"] is True
    group = env["journal"]["group"]
    out = rig.do("journal.revert", {"group": group, "force": True})
    assert out["ok"] is True, out


def test_a_trim_that_never_ran_leaves_the_check_saying_not_landed(rig: Rig) -> None:
    rig.exec.fault(TRIM, "TIMEOUT", when="before", skip=0)
    env = plan(rig, three_trims())
    assert env["errorCode"] == "TIMEOUT" and env["result"]["failClosed"]["step"] == 0
    checked = rig.do("journal.check", {})
    entry = next(c for c in checked["result"]["checked"] if c["op"] == "plan.run")
    assert entry["verdict"] == "not_landed" and entry["perKey"] == {"fingerprint": "before"}


def test_a_set_attributes_that_ends_without_an_answer_blocks_both_intents_until_checked(
    rig: Rig,
) -> None:
    rig.exec.fault(SET, "TIMEOUT", when="after")
    steps = [{"op": SET, "args": {"target": at(10), "values": {"comment": "new"}}}, trim_step(10)]
    env = plan(rig, steps)
    assert env["errorCode"] == "TIMEOUT" and env["result"]["failClosed"]["step"] == 0
    checked = rig.do("journal.check", {})
    ops = sorted(c["op"] for c in checked["result"]["checked"])
    assert ops == ["plan.run", SET]
    assert checked["result"]["writesBlocked"] is False


def test_a_revert_after_another_edit_is_a_conflict_unless_forced(rig: Rig) -> None:
    env = plan(rig, three_trims())
    rig.handles["s1"].versions[0].tracks[0].segments[-1].trim_tail(1)
    refused = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert refused["errorCode"] == "CONFLICT"
    forced = rig.do("journal.revert", {"group": env["result"]["group"], "force": True})
    assert forced["ok"] is True, forced


def test_the_restore_can_itself_be_undone(rig: Rig) -> None:
    env = plan(rig, three_trims())
    after = fingerprint_of_target(rig, S1)
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    undo = rig.do("journal.revert", {"group": out["journal"]["group"]})
    assert undo["ok"] is True, undo
    assert fingerprint_of_target(rig, undo["result"]["restored"]["selector"]) == after


def test_a_second_plan_on_the_restored_sequence_works_by_its_new_address(rig: Rig) -> None:
    env = plan(rig, three_trims())
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    new = out["result"]["restored"]["selector"]
    again = plan(
        rig, [{"op": TRIM, "args": {"target": at(10, parent=new), "edge": "head", "offset": 2}}]
    )
    assert again["ok"] is True, again
    assert len(checkpoints(rig)) == 3  # the plan, the restore's pre-copy, the second plan
    assert S1_WT != new["id"]


def test_a_checkpoint_that_cannot_be_taken_ends_the_plan_before_anything_runs(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(source: Any, destination: Any, duplicate_action: str = "add") -> Any:
        raise RuntimeError("scripted copy failure")

    monkeypatch.setattr(rig.flame.media_panel, "copy", failing)
    before = s1_rows(rig)
    env = plan(rig, three_trims())
    assert env["ok"] is False and "plan not run" in env["error"]
    assert "scripted copy failure" in env["error"] and env["result"]["ran"] is False
    assert s1_rows(rig) == before and checkpoints(rig) == []
    assert [r["type"] for r in rig.records()] == ["group"]
