"""Tests `plan.run` with a Batch-checkpointed step (node / action / batch writes).

It is ONE checkpointed write on one Batch group -- one saved setup, the node selectors resolved up
front (a node a step of the plan makes is found by `late`, implied or explicit), one restore for
`journal.revert`, and the refusals of what would mix the ways to put things back. Run through the
real Runtime with a scripted `transport.exec` over the fake Flame tree (BG1 is not the current
group, BG2 is)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr
from flame_mcp import plan_bundle_batch
from flame_mcp.policy import Policy
from flame_mcp.types import CheckpointSpec, OperationParam, OperationSpec, Refusal
from journal_test_helpers import BG1, Rig, make_rig
from p32b_helpers import make_p32b_rig
from test_timeline_fx_loss import give_fx, make_group
from timeline_p32a_helpers import S1, TRIM, seg_sel

PLAN = "plan.run"
BG2 = {"kind": "batch", "name": "BG2"}
CREATE = "node.create"
SET = "node.set_attributes"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def node(name: str, batch: dict[str, Any] = BG1, **extra: Any) -> dict[str, Any]:
    return {"kind": "node", "batch": batch, "name": name, **extra}


def create(name: str | None, type_: str = "Blur", batch: dict[str, Any] = BG1) -> dict[str, Any]:
    args: dict[str, Any] = {"target": batch, "type": type_}
    if name is not None:
        args["name"] = name
    return {"op": CREATE, "args": args}


def connect(source: str, target: str, batch: dict[str, Any] = BG1, **extra: Any) -> dict[str, Any]:
    args = {"source": node(source, batch), "target": node(target, batch)}
    return {"op": "node.connect", "args": args, **extra}


def set_pos(name: str, x: float, batch: dict[str, Any] = BG1) -> dict[str, Any]:
    return {"op": SET, "args": {"target": node(name, batch), "values": {"pos_x": x}}}


def delete(name: str, batch: dict[str, Any] = BG1) -> dict[str, Any]:
    return {"op": "node.delete", "args": {"target": node(name, batch)}}


def plan(rig: Rig, steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return rig.do(PLAN, {"steps": steps, **extra})


def edit_steps(batch: dict[str, Any] = BG1) -> list[dict[str, Any]]:
    """create Blur b1, create Mux m1, connect b1 -> m1 (both made above: implied late), move b1,
    delete a node that was there before."""
    return [
        create("b1", "Blur", batch),
        create("m1", "Mux", batch),
        connect("b1", "m1", batch),
        set_pos("b1", 40.0, batch),
        delete("exposure1", batch),
    ]


def names(rig: Rig, key: str = "bg1") -> list[str]:
    return [str(n.name.get_value()) for n in rig.handles[key].nodes]


def wiring(rig: Rig, key: str = "bg1") -> dict[str, Any]:
    return {
        str(n.name.get_value()): {
            side: {s: list(v) for s, v in table.items()} for side, table in n._sockets.items()
        }
        for n in rig.handles[key].nodes
    }


def fingerprint(rig: Rig, batch: dict[str, Any] = BG1) -> str:
    env = rig.do("batch.fingerprint", {"target": batch})
    assert env["ok"] is True, env
    value: str = env["result"]["fingerprint"]
    return value


def checkpoints(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r["type"] == "checkpoint"]


def nothing_happened(rig: Rig) -> bool:
    return (
        checkpoints(rig) == []
        and rig.records() == []
        and names(rig)
        == [
            "blur1",
            "comp1",
            "exposure1",
            "action1",
        ]
    )


def test_five_node_edits_take_one_checkpoint_and_run_without_checkpoints_of_their_own(
    rig: Rig,
) -> None:
    before = fingerprint(rig)
    env = plan(rig, edit_steps(), label="five edits")
    assert env["ok"] is True, env
    assert [s["ok"] for s in env["result"]["steps"]] == [True] * 5
    assert names(rig) == ["blur1", "comp1", "action1", "b1", "m1"]
    assert wiring(rig)["b1"]["output"]["Result"] == ["m1"]
    assert rig.handles["bg1"].nodes[3].pos_x.get_value() == 40.0
    assert len(checkpoints(rig)) == 1
    assert [c.label for c in rig.exec.calls].count("batch.checkpoint") == 1
    assert env["undoSteps"] == 0 and env["partial"] == {"done": 5, "failed": 0, "notRun": 0}
    info = env["result"]["batch"]
    assert "timeline" not in env["result"]
    assert info["fingerprintBefore"] == before
    assert info["fingerprintAfter"] == fingerprint(rig) != before
    assert info["checkpoint"] == checkpoints(rig)[0]["id"] and info["changed"] is True
    assert info["checkpointPath"] == checkpoints(rig)[0]["path"]
    assert info["target"] == {
        "type": "PyBatch",
        "name": "BG1",
        "uid": "uid-b1",
        "selector": {"kind": "batch", "uid": "uid-b1"},
    }
    assert "one checkpointed write" in info["note"] and "fingerprintAfter" in info["note"]
    assert env["journal"] == {"group": env["result"]["group"]}


def test_the_journal_holds_the_group_the_checkpoint_the_intent_and_the_outcome(rig: Rig) -> None:
    env = plan(rig, edit_steps())
    group = env["result"]["group"]
    records = rig.records()
    assert [r["type"] for r in records] == [
        "group",
        "checkpoint",
        "intent",
        "intent",
        "outcome",
        "outcome",
    ]
    assert [r["op"] for r in records if r["type"] == "intent"] == ["plan.run", SET]
    intent = next(r for r in records if r["type"] == "intent" and r["op"] == "plan.run")
    assert intent["group"] == group and intent["checkpoint"] == checkpoints(rig)[0]["id"]
    assert intent["fingerprintBefore"] == checkpoints(rig)[0]["fingerprint"]
    assert intent["target"] == {"kind": "batch", "uid": "uid-b1"}
    assert [s["op"] for s in intent["args"]["steps"]] == [
        CREATE,
        CREATE,
        "node.connect",
        SET,
        "node.delete",
    ]
    plan_outcome = next(r for r in records if r["type"] == "outcome" and r["id"] == intent["id"])
    assert plan_outcome["ok"] is True
    assert plan_outcome["fingerprintAfter"] != intent["fingerprintBefore"]
    assert [s["ok"] for s in plan_outcome["steps"]] == [True] * 5
    write = next(c for c in rig.exec.calls if c.label == CREATE)
    assert write.journal_types == ["group", "checkpoint", "intent"]


def test_the_same_plan_runs_in_the_current_group_too(rig: Rig) -> None:
    before = fingerprint(rig, BG2)
    steps = [*edit_steps(BG2)[:4], delete("blur1", BG2)]
    env = plan(rig, steps)
    assert env["ok"] is True, env
    assert names(rig, "bg2") == ["b1", "m1"]
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig, BG2) == before and names(rig, "bg2") == ["blur1"]


def test_a_node_duplicate_runs_in_the_current_group(rig: Rig) -> None:
    steps = [
        {"op": "node.duplicate", "args": {"target": node("blur1", BG2)}},
        create("b1", "Blur", BG2),
    ]
    env = plan(rig, steps)
    assert env["ok"] is True, env
    assert names(rig, "bg2") == ["blur1", "blur2", "b1"]
    assert env["result"]["steps"][0]["result"]["node"]["name"] == "blur2"


def test_one_revert_puts_the_whole_plan_back(rig: Rig) -> None:
    before = fingerprint(rig)
    nodes_before = names(rig)
    sockets_before = wiring(rig)
    env = plan(rig, edit_steps())
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig) == before
    assert names(rig) == nodes_before and wiring(rig) == sockets_before
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert states[env["result"]["group"]] == "reverted"
    again = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert again["errorCode"] == "PRECONDITION_UNMET" and "already reverted" in again["error"]


def test_a_set_attributes_step_comes_back_with_the_restore_not_value_by_value(rig: Rig) -> None:
    steps = [{"op": "batch.organize", "args": {"target": BG1}}, set_pos("comp1", 33)]
    env = plan(rig, steps)
    assert env["ok"] is True, env
    assert env["result"]["batch"]["changed"] is False  # positions are not in the fingerprint
    assert rig.handles["bg1"].nodes[1].pos_x.get_value() == 33
    set_intents = [r for r in rig.records() if r["type"] == "intent" and r["op"] == SET]
    assert len(set_intents) == 1 and set_intents[0]["group"] == env["result"]["group"]
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert rig.handles["bg1"].nodes[1].pos_x.get_value() == 0.0
    assert sum(1 for r in rig.records() if r.get("op") == SET) == 1  # no value write-back


def test_the_resolved_list_says_which_steps_were_left_to_their_turn(rig: Rig) -> None:
    env = plan(rig, edit_steps())
    resolved = env["result"]["batch"]["resolved"]
    assert [(r["index"], r["op"], r["late"]) for r in resolved] == [
        (0, CREATE, False),
        (1, CREATE, False),
        (2, "node.connect", "implied"),
        (3, SET, "implied"),
        (4, "node.delete", False),
    ]
    assert resolved[4]["nodes"][0]["name"] == "exposure1"
    assert resolved[4]["nodes"][0]["type"] == "Exposure"
    assert [n["late"] for n in resolved[2]["nodes"]] == [True, True]


def test_a_node_the_plan_names_is_found_without_late_but_one_with_a_default_name_is_not(
    rig: Rig,
) -> None:
    env = plan(rig, [create(None, "Mux"), connect("mux1", "comp1")])
    assert env["errorCode"] == "SELECTOR_EMPTY" and "step 1" in env["error"]
    assert "plan not run" in env["error"] and env["result"]["ran"] is False
    assert env["result"]["preResolution"][0]["index"] == 1
    assert "late: true" in env["hint"]
    assert nothing_happened(rig)


def test_late_true_lets_a_step_name_a_node_with_a_default_name(rig: Rig) -> None:
    env = plan(rig, [create(None, "Mux"), connect("mux1", "comp1", late=True)])
    assert env["ok"] is True, env
    resolved = env["result"]["batch"]["resolved"]
    assert resolved[1]["late"] is True and resolved[0]["late"] is False
    assert "mux1" in names(rig)
    assert wiring(rig)["mux1"]["output"]["Result"] == ["comp1"]


def test_an_implied_late_step_whose_node_is_not_there_when_its_turn_comes_stops_the_plan(
    rig: Rig,
) -> None:
    before = fingerprint(rig)
    env = plan(rig, [create("b1"), connect("b1", "nope", late=True), create("c1")])
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert env["result"]["stoppedAt"] == 1
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 1}
    assert "journal.revert(group=" in env["hint"]
    assert "b1" in names(rig) and "c1" not in names(rig)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig) == before


def test_the_name_of_a_node_made_in_another_group_is_not_implied(rig: Rig) -> None:
    steps = [create("b1", "Blur", BG2), connect("b1", "comp1")]
    env = plan(rig, steps)
    assert env["errorCode"] == "SELECTOR_EMPTY" and "step 1" in env["error"]
    assert nothing_happened(rig) and names(rig, "bg2") == ["blur1"]


def test_the_same_group_by_another_selector_is_one_group_but_its_names_are_not_implied(
    rig: Rig,
) -> None:
    by_uid = {"kind": "batch", "uid": "uid-b1"}
    steps = [create("b1"), connect("b1", "comp1", by_uid)]
    env = plan(rig, steps)
    assert env["errorCode"] == "SELECTOR_EMPTY" and env["result"]["preResolution"][0]["index"] == 1
    assert "same selector" in env["hint"] and "late: true" in env["hint"]
    assert nothing_happened(rig)
    again = plan(rig, [create("b1"), {**connect("b1", "comp1", by_uid), "late": True}])
    assert again["ok"] is True, again
    assert len(checkpoints(rig)) == 1


def test_a_step_that_does_not_resolve_stops_everything_and_leaves_no_trace(rig: Rig) -> None:
    env = plan(rig, [create("b1"), connect("blur1", "nope")])
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert "step 1" in env["error"] and "plan not run" in env["error"]
    assert env["result"]["ran"] is False
    assert nothing_happened(rig)


def test_every_step_that_fails_to_resolve_is_listed(rig: Rig) -> None:
    env = plan(rig, [delete("nope1"), set_pos("comp1", 1.0), delete("nope2")])
    assert [i["index"] for i in env["result"]["preResolution"]] == [0, 2]
    assert "and 1 more" in env["error"] and nothing_happened(rig)


def test_two_nodes_of_one_name_need_pos_and_the_error_says_so(rig: Rig) -> None:
    rig.handles["bg1"].create_node("Blur").name.set_value("blur1")
    env = plan(rig, [delete("blur1")])
    assert env["errorCode"] == "SELECTOR_AMBIGUOUS", env
    assert "pos" in env["error"]
    assert not checkpoints(rig)


def test_a_group_that_is_not_there_is_found_before_anything_runs(rig: Rig) -> None:
    env = plan(rig, [create("b1", "Blur", {"kind": "batch", "name": "nope"})])
    assert env["errorCode"] == "SELECTOR_EMPTY" and nothing_happened(rig)


def test_a_node_name_that_is_taken_is_refused_up_front_unless_the_plan_frees_it(rig: Rig) -> None:
    env = plan(rig, [create("comp1")])
    assert env["errorCode"] == "INVALID_ARGS" and "comp1" in env["error"]
    assert env["result"]["preResolution"][0]["index"] == 0 and nothing_happened(rig)
    freed = plan(rig, [delete("exposure1"), create("exposure1", "Exposure")])
    assert freed["ok"] is True, freed
    assert names(rig).count("exposure1") == 1


def test_writes_to_two_groups_are_refused_and_nothing_runs(rig: Rig) -> None:
    env = plan(rig, [create("b1"), create("b2", "Blur", BG2)])
    assert env["errorCode"] == "INVALID_ARGS" and "2 different Batch groups" in env["error"]
    assert {t["uid"] for t in env["result"]["targets"]} == {"uid-b1", "uid-b2"}
    assert nothing_happened(rig) and names(rig, "bg2") == ["blur1"]


def test_a_set_attributes_of_another_group_is_refused_too(rig: Rig) -> None:
    env = plan(rig, [create("b1"), set_pos("blur1", 3.0, BG2)])
    assert env["errorCode"] == "INVALID_ARGS" and "different Batch groups" in env["error"]
    assert nothing_happened(rig)


def test_reads_of_any_group_may_share_the_plan(rig: Rig) -> None:
    steps = [
        {"op": "node.info", "args": {"target": node("blur1", BG2)}},
        create("b1"),
        {"op": "node.info", "args": {"target": node("b1")}},
    ]
    env = plan(rig, steps)
    assert env["ok"] is True, env
    assert env["result"]["steps"][2]["result"]["name"] == "b1"


def test_a_failing_third_step_stops_the_plan_and_one_revert_restores_the_fingerprint(
    rig: Rig,
) -> None:
    before = fingerprint(rig)
    nodes_before = names(rig)
    bad = connect("b1", "m1")
    bad["args"]["source_socket"] = "NoSuchSocket"
    env = plan(rig, [create("b1"), create("m1", "Mux"), bad, delete("comp1")])
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert env["result"]["stoppedAt"] == 2
    assert env["partial"] == {"done": 2, "failed": 1, "notRun": 1}
    assert "NoSuchSocket" in env["error"] and "journal.revert(group=" in env["hint"]
    assert names(rig) == [*nodes_before, "b1", "m1"]  # the first two steps stay applied
    outcome = next(r for r in rig.records() if r["type"] == "outcome" and r["id"].startswith("i"))
    assert outcome["ok"] is False and outcome["stoppedAt"] == 2 and outcome["fingerprintAfter"]
    assert env["result"]["batch"]["fingerprintAfter"] != before
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig) == before and names(rig) == nodes_before


def test_the_check_of_each_op_runs_in_its_own_turn(rig: Rig) -> None:
    env = plan(rig, [create("b1"), create("x1", "NoSuchType")])
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "NoSuchType" in env["error"] and env["result"]["stoppedAt"] == 1
    assert "b1" in names(rig) and len(checkpoints(rig)) == 1
    dup = plan(rig, [create("d1"), {"op": "node.duplicate", "args": {"target": node("d1")}}])
    assert dup["errorCode"] == "PRECONDITION_UNMET" and "not the current one" in dup["error"]
    assert dup["result"]["stoppedAt"] == 1 and "d1" in names(rig)


class RenamingAttr(FakeAttr):
    """A node name that Flame changes when it is set: `x` becomes `x_1`."""

    def set_value(self, value: Any) -> None:
        super().set_value(f"{value}_1")


RenamingAttr.__name__ = "PyAttribute"


def rename_new_nodes(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    real = batch.create_node

    def create_node(node_type: str) -> Any:
        made = real(node_type)
        made.name = RenamingAttr(made.name.get_value())
        return made

    batch.create_node = create_node


def test_a_node_made_under_another_name_stops_the_plan_and_the_hint_names_it(rig: Rig) -> None:
    before = fingerprint(rig)
    rename_new_nodes(rig)
    env = plan(rig, [create("b1"), connect("b1", "comp1"), delete("exposure1")])
    assert env["ok"] is False and env["result"]["stoppedAt"] == 0
    assert env["partial"] == {"done": 0, "failed": 1, "notRun": 2}
    assert "b1_1" in env["hint"] and "late: true" in env["hint"]
    assert "b1_1" in names(rig) and "exposure1" in names(rig)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig) == before


def test_a_create_that_answers_ok_under_another_name_is_turned_into_a_failure() -> None:
    args = {"target": BG1, "type": "Blur", "name": "b1"}
    ok_env = {"ok": True, "result": {"node": {"name": "b1_1"}, "createdAs": "blur2"}}
    assert plan_bundle_batch.renamed_by_flame(CREATE, args, ok_env) == "b1_1"
    same = {"ok": True, "result": {"node": {"name": "b1"}, "createdAs": "blur2"}}
    assert plan_bundle_batch.renamed_by_flame(CREATE, args, same) is None
    failed = {"ok": False, "result": {"node": None, "createdAs": "blur2"}}
    assert plan_bundle_batch.renamed_by_flame(CREATE, args, failed) == "blur2"
    assert plan_bundle_batch.renamed_by_flame(CREATE, {"type": "Blur"}, ok_env) is None
    assert plan_bundle_batch.renamed_by_flame("node.delete", args, ok_env) is None


def test_an_ok_create_without_a_read_back_node_is_not_judged_by_created_as() -> None:
    args = {"target": BG1, "type": "Blur", "name": "b1"}
    no_row = {"ok": True, "result": {"node": None, "createdAs": "blur2"}}
    assert plan_bundle_batch.renamed_by_flame(CREATE, args, no_row) is None
    no_result = {"ok": True, "result": None}
    assert plan_bundle_batch.renamed_by_flame(CREATE, args, no_result) is None


@pytest.mark.parametrize(
    "step",
    [
        {"op": "desktop.rename", "args": {"target": {"kind": "batch", "name": "BG2"}, "name": "x"}},
        {"op": "batch.set_attributes", "args": {"target": BG1, "values": {}}},
        {"op": "batch.checkpoint", "args": {"target": BG1}},
    ],
    ids=["desktop.rename", "batch.set_attributes", "batch.checkpoint"],
)
def test_another_kind_of_write_is_refused_for_its_own_way_back(
    rig: Rig, step: dict[str, Any]
) -> None:
    env = plan(rig, [create("b1"), step])
    assert env["errorCode"] == "INVALID_ARGS"
    issues = env["result"]["issues"]
    assert [i["index"] for i in issues] == [1]
    assert "the ways of reverting differ" in issues[0]["problems"][0]
    assert nothing_happened(rig)


def test_a_timeline_write_and_a_batch_write_cannot_share_a_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_p32b_rig(monkeypatch, tmp_path)
    trim = {
        "op": TRIM,
        "args": {"target": seg_sel(S1, 0, record_in=10), "edge": "head", "offset": 1},
    }
    for steps in ([trim, create("b1")], [create("b1"), trim]):
        env = rig.do(PLAN, {"steps": steps})
        assert env["errorCode"] == "INVALID_ARGS", env
        issues = env["result"]["issues"]
        assert [i["index"] for i in issues] == [1]
        assert "the ways of reverting differ" in issues[0]["problems"][0]
        assert rig.records() == []


def test_batch_iterate_is_refused_in_a_plan(rig: Rig) -> None:
    env = plan(rig, [create("b1"), {"op": "batch.iterate", "args": {"target": BG1}}])
    assert env["errorCode"] == "INVALID_ARGS"
    assert "batch.iterate cannot be a plan step" in str(env["result"]["issues"])
    assert nothing_happened(rig) and rig.handles["bg1"].batch_iterations == []


def test_the_ops_that_restore_a_setup_are_still_refused(rig: Rig) -> None:
    env = plan(rig, [{"op": "batch.load_setup", "args": {"target": BG1, "path": "/x"}}])
    assert env["errorCode"] == "INVALID_ARGS" and "cannot be a plan step" in str(env["result"])


def test_late_is_for_a_write_of_a_bundled_plan_only(rig: Rig) -> None:
    read = {"op": "node.info", "args": {"target": node("blur1")}, "late": True}
    env = plan(rig, [create("b1"), read])
    assert env["errorCode"] == "INVALID_ARGS" and "`late` applies only" in str(env["result"])
    env = plan(rig, [set_pos("comp1", 1.0) | {"late": True}])
    assert env["errorCode"] == "INVALID_ARGS" and nothing_happened(rig)


def test_a_plan_of_set_attributes_only_is_not_bundled(rig: Rig) -> None:
    env = plan(rig, [set_pos("comp1", 5.0), set_pos("blur1", 6.0)])
    assert env["ok"] is True and "batch" not in env["result"]
    assert checkpoints(rig) == []
    assert len([r for r in rig.records() if r["type"] == "intent"]) == 2


def test_a_plan_without_a_node_write_is_not_bundled(rig: Rig) -> None:
    env = plan(rig, [{"op": "desktop.rename", "args": {"target": BG2, "name": "BG9"}}])
    assert env["ok"] is True and "batch" not in env["result"] and checkpoints(rig) == []


def test_batch_reel_ops_share_the_plan_and_the_empty_reel_goes_with_the_revert(rig: Rig) -> None:
    before = fingerprint(rig)
    steps = [
        {"op": "batch.create_reel", "args": {"target": BG1, "name": "Scratch"}},
        create("b1"),
    ]
    env = plan(rig, steps)
    assert env["ok"] is True, env
    assert "Scratch" in [str(r.name.get_value()) for r in rig.handles["bg1"].reels]
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig) == before
    assert "Scratch" not in [str(r.name.get_value()) for r in rig.handles["bg1"].reels]


def test_the_action_ops_share_the_plan(rig: Rig) -> None:
    before = fingerprint(rig)
    action = node("action1")
    steps = [
        {"op": "action.create_node", "args": {"target": action, "type": "Axis"}},
        {
            "op": "action.set_attributes",
            "args": {
                "target": {"kind": "action_node", "node": action, "name": "axis1"},
                "values": {"name": "axis_main"},
            },
        },
    ]
    env = plan(rig, steps)
    assert env["ok"] is True, env
    assert [r["late"] for r in env["result"]["batch"]["resolved"]] == [False, False]
    assert env["result"]["batch"]["changed"] is True
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig) == before


def test_a_revert_after_another_edit_is_a_conflict_unless_forced(rig: Rig) -> None:
    env = plan(rig, edit_steps())
    rig.handles["bg1"].create_node("Mux")
    refused = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert refused["errorCode"] == "CONFLICT"
    forced = rig.do("journal.revert", {"group": env["result"]["group"], "force": True})
    assert forced["ok"] is True, forced


def test_the_restore_can_itself_be_undone(rig: Rig) -> None:
    env = plan(rig, edit_steps())
    after = fingerprint(rig)
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    undo = rig.do("journal.revert", {"group": out["journal"]["group"]})
    assert undo["ok"] is True, undo
    assert fingerprint(rig) == after


def test_the_revert_of_a_plan_says_which_clips_lost_their_timeline_fx(rig: Rig) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur", "Timewarp")
    on_reel.fx_lost_on_load = True
    env = plan(rig, [create("b1"), create("m1", "Mux")])
    assert env["ok"] is True, env
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert out["result"]["timelineFxLost"] == [
        {
            "reelKind": "schematic",
            "reel": "SrcA",
            "clip": "clip1",
            "before": ["Blur", "Timewarp"],
            "after": [],
        }
    ]
    assert any("Timeline FX" in w for w in out["result"]["warnings"])


def test_a_clip_that_keeps_its_fx_is_not_reported_after_a_plan_revert(rig: Rig) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur")
    env = plan(rig, [create("b1")])
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert out["result"]["timelineFxLost"] == []


def test_readonly_hides_the_node_edits_in_a_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = plan(ro, [create("b1")])
    assert env["errorCode"] == "POLICY_DENIED" and nothing_happened(ro)


def test_a_continue_plan_with_a_node_write_is_refused_as_before(rig: Rig) -> None:
    env = plan(rig, [create("b1")], on_error="continue")
    assert env["errorCode"] == "INVALID_ARGS"
    assert "only allowed when every step is a read" in str(env["result"])


def test_summary_keeps_the_batch_block_less_resolved_and_briefs_the_created_node(
    rig: Rig,
) -> None:
    env = plan(rig, edit_steps(), result_detail="summary")
    assert env["ok"] is True, env
    result = env["result"]
    assert result["detail"] == "summary" and "resolved" not in result["batch"]
    assert result["batch"]["checkpoint"] and result["batch"]["fingerprintAfter"]
    first = result["steps"][0]
    assert first["brief"]["node"]["name"] == "b1" and "result" not in first
    assert "journal" not in first
    out = rig.do("journal.revert", {"group": result["group"]})
    assert out["ok"] is True, out


def test_full_detail_keeps_resolved(rig: Rig) -> None:
    env = plan(rig, edit_steps(), result_detail="full")
    assert len(env["result"]["batch"]["resolved"]) == 5


def test_a_node_create_that_ends_without_an_answer_stops_the_plan_and_the_check_reads_the_group(
    rig: Rig,
) -> None:
    rig.exec.fault(CREATE, "TIMEOUT", when="after", skip=1)  # the second create ran, answer lost
    env = plan(rig, edit_steps())
    assert env["errorCode"] == "TIMEOUT"
    assert env["result"]["failClosed"]["step"] == 1 and env["result"]["stoppedAt"] == 1
    assert "refuses writes until journal.check" in env["error"]
    assert "m1" in names(rig)
    refused = rig.do("desktop.rename", {"target": BG2, "name": "x"})
    assert refused["errorCode"] == "POLICY_DENIED"
    checked = rig.do("journal.check", {})
    entry = next(c for c in checked["result"]["checked"] if c["op"] == "plan.run")
    assert entry["verdict"] == "diverged" and entry["perKey"] == {"fingerprint": "other"}
    assert rig.do("desktop.rename", {"target": BG2, "name": "x"})["ok"] is True
    out = rig.do("journal.revert", {"group": env["journal"]["group"], "force": True})
    assert out["ok"] is True, out
    assert "m1" not in names(rig) and "b1" not in names(rig)


def test_a_create_that_never_ran_leaves_the_check_saying_not_landed(rig: Rig) -> None:
    rig.exec.fault(CREATE, "TIMEOUT", when="before", skip=0)
    env = plan(rig, edit_steps())
    assert env["errorCode"] == "TIMEOUT" and env["result"]["failClosed"]["step"] == 0
    checked = rig.do("journal.check", {})
    entry = next(c for c in checked["result"]["checked"] if c["op"] == "plan.run")
    assert entry["verdict"] == "not_landed" and entry["perKey"] == {"fingerprint": "before"}


def test_a_set_attributes_that_ends_without_an_answer_blocks_until_checked(rig: Rig) -> None:
    rig.exec.fault(SET, "TIMEOUT", when="after")
    env = plan(rig, edit_steps())
    assert env["errorCode"] == "TIMEOUT" and env["result"]["failClosed"]["step"] == 3
    checked = rig.do("journal.check", {})
    ops = sorted(c["op"] for c in checked["result"]["checked"])
    assert ops == [SET, "plan.run"]
    assert checked["result"]["writesBlocked"] is False


def test_a_checkpoint_that_cannot_be_taken_ends_the_plan_before_anything_runs(rig: Rig) -> None:
    rig.exec.fault("batch.checkpoint", "FLAME_API_ERROR", when="before")
    env = plan(rig, edit_steps())
    assert env["ok"] is False and "plan not run" in env["error"]
    assert env["result"]["ran"] is False
    assert names(rig) == ["blur1", "comp1", "exposure1", "action1"]
    assert [r["type"] for r in rig.records()] == ["group"]


def test_the_group_record_names_the_late_steps(rig: Rig) -> None:
    plan(rig, [create(None, "Mux"), connect("mux1", "comp1", late=True)])
    assert rig.records()[0]["late"] == [1]


def shelf_reel(rig: Rig, name: str = "Shelf1") -> Any:
    reel = rig.handles["bg1"].create_shelf_reel(name)
    assert reel is not None
    return reel


CLIP1 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip1"}


def add_clips_step(reel: str, **extra: Any) -> dict[str, Any]:
    args = {"target": BG1, "reel": reel, "clips": [CLIP1], **extra}
    return {"op": "batch.add_clips", "args": args}


def test_create_shelf_reel_is_refused_in_a_plan(rig: Rig) -> None:
    shelf_step = {"op": "batch.create_shelf_reel", "args": {"target": BG1, "name": "S"}}
    env = plan(rig, [create("b1"), shelf_step])
    assert env["errorCode"] == "INVALID_ARGS"
    assert "batch.create_shelf_reel cannot be a plan step" in str(env["result"]["issues"])
    assert nothing_happened(rig) and rig.handles["bg1"].shelf_reels == []


def test_add_clips_into_a_shelf_reel_is_refused_in_its_turn_and_writes_nothing(rig: Rig) -> None:
    shelf = shelf_reel(rig)
    before = fingerprint(rig)
    env = plan(rig, [create("b1"), add_clips_step("Shelf1")])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["stoppedAt"] == 1
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 0}
    assert "shelf reel" in env["error"] and "outside a Batch setup" in env["error"]
    assert list(shelf.clips) == [] and "b1" in names(rig)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig) == before and list(shelf.clips) == []


def test_add_clips_into_a_shelf_reel_named_by_reel_kind_is_refused_too(rig: Rig) -> None:
    shelf_reel(rig, "Schematic Reel 1")
    env = plan(rig, [add_clips_step("Schematic Reel 1", reel_kind="shelf")])
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["stoppedAt"] == 0


def test_add_clips_into_a_schematic_reel_runs_in_a_plan_and_reverts(rig: Rig) -> None:
    rig.handles["bg1"].create_reel("Src")
    before = fingerprint(rig)
    nodes_before = names(rig)
    env = plan(rig, [add_clips_step("Src"), create("b1")])
    assert env["ok"] is True, env
    assert len(names(rig)) == len(nodes_before) + 2
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint(rig) == before and names(rig) == nodes_before


def test_a_param_default_reaches_the_check_and_the_code_of_a_bundled_step(rig: Rig) -> None:
    def check(rt: Any, args: Any) -> Refusal | None:
        return None if args["mode"] == "fast" else Refusal("INVALID_ARGS", "mode must be fast")

    rig.registry.register(
        OperationSpec(
            name="batch.probe_default",
            category="batch",
            description="x",
            effect="create",
            undo="checkpoint",
            params=(
                OperationParam(name="target", type="selector", description="t", required=True),
                OperationParam(
                    name="mode", type="string", description="m", required=False, default="fast"
                ),
            ),
            accepts=("batch",),
            to_python=lambda args: f"_result = {{'mode': {args['mode']!r}}}",
            undo_steps=lambda args, affected: 0,
            checkpoint=CheckpointSpec(check=check),
            sample_args={"target": {"kind": "batch", "name": "s"}, "mode": "fast"},
        )
    )
    step = {"op": "batch.probe_default", "args": {"target": BG1}}
    env = plan(rig, [step])
    assert env["ok"] is True, env
    assert env["result"]["steps"][0]["result"] == {"mode": "fast"}


def test_a_bundled_plan_is_refused_while_a_write_is_unresolved(rig: Rig) -> None:
    rig.exec.fault(CREATE, "TIMEOUT", when="after")
    first = plan(rig, [create("b1")])
    assert first["errorCode"] == "TIMEOUT" and first["result"]["failClosed"]["step"] == 0
    records = len(rig.records())
    calls = len(rig.exec.calls)
    again = plan(rig, edit_steps())
    assert again["ok"] is False and again["errorCode"] == "POLICY_DENIED"
    assert "plan not run" in again["error"] and again["result"]["ran"] is False
    assert len(rig.records()) == records and len(rig.exec.calls) == calls
    rig.do("journal.check", {})
    assert plan(rig, [create("c1")])["ok"] is True


def group_states(rig: Rig) -> dict[str, str]:
    return {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}


def test_a_plan_that_stopped_after_a_step_applied_reads_partial_and_is_a_newer_group(
    rig: Rig,
) -> None:
    g0 = rig.do(CREATE, {"target": BG1, "type": "Blur", "name": "b0"})
    assert g0["ok"] is True, g0
    stopped = plan(rig, [create("b1"), {**delete("nope"), "late": True}])
    assert stopped["ok"] is False and stopped["result"]["stoppedAt"] == 1
    plan_group = stopped["journal"]["group"]
    assert group_states(rig)[plan_group] == "partial"
    refused = rig.do("journal.revert", {"group": g0["journal"]["group"]})
    assert refused["errorCode"] == "CONFLICT", refused
    assert [row["group"] for row in refused["result"]["newerGroups"]] == [plan_group]
    out = rig.do("journal.revert", {"group": plan_group})
    assert out["ok"] is True, out
    assert "b1" not in names(rig) and "b0" in names(rig)


def test_a_plan_stopped_by_a_check_before_any_write_is_not_revertible(rig: Rig) -> None:
    shelf_reel(rig)
    before = fingerprint(rig)
    env = plan(rig, [add_clips_step("Shelf1"), create("b1")])
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["stoppedAt"] == 0
    assert "stay applied" not in str(env.get("hint")) and "journal.revert" not in str(
        env.get("hint")
    )
    group = env["journal"]["group"]
    assert group_states(rig)[group] == "failed"
    loads = [c.label for c in rig.exec.calls].count("batch.load_setup")
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is False and out["errorCode"] == "PRECONDITION_UNMET", out
    assert [c.label for c in rig.exec.calls].count("batch.load_setup") == loads
    assert fingerprint(rig) == before


def test_a_first_step_that_ran_and_failed_is_still_revertible(rig: Rig) -> None:
    rename_new_nodes(rig)
    env = plan(rig, [create("b1"), delete("exposure1")])
    assert env["result"]["stoppedAt"] == 0 and "journal.revert" in env["hint"]
    assert group_states(rig)[env["journal"]["group"]] == "partial"
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out


def plan_records(rig: Rig, group: str) -> tuple[dict[str, Any], dict[str, Any]]:
    records = rig.records()
    intent = next(r for r in records if r["type"] == "intent" and r["op"] == "plan.run")
    assert intent["group"] == group
    outcome = next(r for r in records if r["type"] == "outcome" and r["id"] == intent["id"])
    return intent, outcome


def test_a_plan_with_a_value_write_does_not_overwrite_a_later_attribute_change(
    rig: Rig,
) -> None:
    env = plan(rig, [create("b1"), set_pos("b1", 40.0)])
    assert env["ok"] is True, env
    group = env["result"]["group"]
    intent, outcome = plan_records(rig, group)
    assert intent["watchAttributes"] is True and outcome["attributeFingerprintAfter"]
    rig.handles["bg1"].nodes[1].pos_x.set_value(77.0)  # comp1, by hand
    refused = rig.do("journal.revert", {"group": group})
    assert refused["errorCode"] == "CONFLICT", refused
    assert rig.handles["bg1"].nodes[1].pos_x.get_value() == 77.0
    forced = rig.do("journal.revert", {"group": group, "force": True})
    assert forced["ok"] is True, forced
    assert rig.handles["bg1"].nodes[1].pos_x.get_value() == 0.0


def test_a_plan_with_a_value_write_reverts_when_nothing_changed_after_it(rig: Rig) -> None:
    env = plan(rig, [create("b1"), set_pos("b1", 40.0)])
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert "b1" not in names(rig)


def test_a_plan_without_a_value_write_keeps_the_default_fingerprint_sections(rig: Rig) -> None:
    env = plan(rig, [create("b1")])
    intent, outcome = plan_records(rig, env["result"]["group"])
    assert "watchAttributes" not in intent and "attributeFingerprintAfter" not in outcome
    rig.handles["bg1"].nodes[1].pos_x.set_value(77.0)
    assert rig.do("journal.revert", {"group": env["result"]["group"]})["ok"] is True


def test_a_set_attributes_step_does_not_free_a_taken_name_unless_it_renames(rig: Rig) -> None:
    env = plan(rig, [set_pos("comp1", 5.0), create("blur1")])
    assert env["errorCode"] == "INVALID_ARGS" and "already has a node named" in env["error"]
    assert nothing_happened(rig)


def test_a_set_attributes_step_that_renames_frees_the_name(rig: Rig) -> None:
    rename = {"op": SET, "args": {"target": node("blur1"), "values": {"name": "blur9"}}}
    env = plan(rig, [rename, create("blur1")])
    assert "already has a node named" not in str(env.get("error")), env
