"""plan.run: validation before anything runs, one journal group, abort / continue, fail closed,
and journal.revert of a whole plan (including a revert that stops part way). Run through the
real Runtime with a scripted `transport.exec` over a fake Flame tree (journal_test_helpers.py)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import envelope
from flame_mcp.ops import plan as plan_mod
from flame_mcp.policy import Policy
from flame_mcp.types import Category, OperationParam, OperationSpec
from journal_test_helpers import BG1, COMP, REEL_WIRETAP, Rig, make_rig
from p32b_helpers import PLAN, S1, make_p32b_rig
from p33_helpers import REPLACE, add_media_clip, clip_sel
from timeline_p32a_helpers import fingerprint_of_target, seg_sel

EXPO = {"kind": "node", "batch": BG1, "name": "exposure1"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def set_step(target: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    return {"op": "node.set_attributes", "args": {"target": target, "values": values}}


def three_writes() -> list[dict[str, Any]]:
    return [
        set_step(COMP, {"pos_x": 5.0}),
        set_step(EXPO, {"exposure_red": 1.5}),
        {"op": "desktop.rename", "args": {"target": REEL_WIRETAP, "name": "R1b"}},
    ]


def run_plan(rig: Rig, steps: list[Any], **extra: Any) -> dict[str, Any]:
    return rig.do("plan.run", {"steps": steps, **extra})


def comp_pos(rig: Rig) -> Any:
    return rig.handles["comp"].pos_x.get_value()


def expo_red(rig: Rig) -> Any:
    return rig.handles["expo"].exposure_red.get_value()


def reel_name(rig: Rig) -> Any:
    return rig.handles["reel"].name.get_value()


def states(rig: Rig) -> dict[str, str]:
    return {i["group"]: i["state"] for i in rig.do("journal.list")["result"]["items"]}


def test_every_write_of_a_plan_lands_in_one_journal_group(rig: Rig) -> None:
    env = run_plan(rig, three_writes(), label="tidy the comp")
    assert env["ok"] is True, env
    group = env["journal"]["group"]
    assert env["result"]["group"] == group and env["result"]["label"] == "tidy the comp"
    intents = [r for r in rig.records() if r["type"] == "intent"]
    assert [i["group"] for i in intents] == [group] * 3
    assert [i["op"] for i in intents] == [
        "node.set_attributes",
        "node.set_attributes",
        "desktop.rename",
    ]
    assert [s["journal"]["group"] for s in env["result"]["steps"]] == [group] * 3
    assert (comp_pos(rig), expo_red(rig), reel_name(rig)) == (5, 1.5, "R1b")
    listing = rig.do("journal.list")["result"]["items"]
    assert len(listing) == 1 and listing[0]["state"] == "landed" and listing[0]["intents"] == 3
    assert listing[0]["label"] == "tidy the comp"


def test_the_plan_group_is_recorded_before_the_first_intent(rig: Rig) -> None:
    run_plan(rig, three_writes(), label="L")
    records = rig.records()
    assert records[0]["type"] == "group" and records[0]["kind"] == "plan.run"
    assert records[0]["label"] == "L" and records[0]["steps"] == [
        "node.set_attributes",
        "node.set_attributes",
        "desktop.rename",
    ]
    shown = rig.do("journal.show", {"group": records[0]["group"]})["result"]
    assert shown["records"][0]["type"] == "group"


def test_envelope_shape_partial_affected_and_undo_steps(rig: Rig) -> None:
    env = run_plan(rig, three_writes())
    assert env["partial"] == {"done": 3, "failed": 0, "notRun": 0}
    assert env["affected"] == 3 and env["undoSteps"] == 0
    assert env["execution"]["thread"] == "server" and env["execution"]["op"] == "plan.run"
    assert env["result"]["stoppedAt"] is None and env["result"]["failClosed"] is None
    assert [s["ok"] for s in env["result"]["steps"]] == [True, True, True]


def test_affected_and_undo_steps_are_sums_over_the_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.registry.register_category(
        Category(
            name="testcat",
            description="t",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    rig.registry.register(
        OperationSpec(
            name="testcat.native_write",
            category="testcat",
            description="a write Flame's own Ctrl+Z tracks (2 steps)",
            effect="create",
            params=(OperationParam(name="value", type="string", description="v", required=True),),
            to_python=lambda args: f"flame._thing = {args['value']!r}\n_affected = 2",
            undo_steps=lambda args, affected: affected,
        )
    )
    steps = [
        {"op": "testcat.native_write", "args": {"value": "a"}},
        {"op": "testcat.native_write", "args": {"value": "b"}},
        set_step(COMP, {"pos_x": 5.0}),
    ]
    env = run_plan(rig, steps)
    assert env["ok"] is True, env
    assert (env["affected"], env["undoSteps"]) == (5, 4)  # 2 + 2 + 1 keys; 2 + 2 + 0 undo steps
    assert rig.flame._thing == "b"


def test_a_plan_that_only_reads_has_no_group(rig: Rig) -> None:
    env = run_plan(
        rig, [{"op": "node.get_attributes", "args": {"target": COMP}}, {"op": "batch.list"}]
    )
    assert env["ok"] is True and env["journal"] is None and env["result"]["group"] is None
    assert rig.records() == []


def test_a_plan_of_exactly_the_maximum_size_runs(rig: Rig) -> None:
    steps = [{"op": "batch.list"}] * plan_mod.MAX_PLAN_STEPS
    env = run_plan(rig, steps)
    assert env["ok"] is True and env["partial"]["done"] == plan_mod.MAX_PLAN_STEPS


def test_one_invalid_step_means_nothing_runs(rig: Rig) -> None:
    steps = [
        set_step(COMP, {"pos_x": 5.0}),
        set_step(EXPO, {"exposure_red": 1.5}),
        {"op": "node.nope"},
    ]
    env = run_plan(rig, steps)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert rig.exec.calls == [] and rig.records() == []  # not even a pre-read
    assert (comp_pos(rig), expo_red(rig)) == (0.0, 0.0)
    issues = env["result"]["issues"]
    assert [(i["index"], i["op"]) for i in issues] == [(2, "node.nope")]
    assert "no operation named 'node.nope'" in issues[0]["problems"][0]
    assert env["result"]["ran"] is False


def test_a_misspelt_op_gets_a_suggestion(rig: Rig) -> None:
    env = run_plan(rig, [{"op": "node.set_attribute", "args": {}}])
    assert "did you mean 'node.set_attributes'" in env["result"]["issues"][0]["problems"][0]


def test_every_bad_step_is_reported_not_just_the_first(rig: Rig) -> None:
    steps: list[Any] = [
        set_step(COMP, {"pos_x": 5.0}),
        {"op": "node.set_attributes", "args": {"values": {"pos_x": 1}}},  # no target
        "not an object",
        {"op": "plan.run", "args": {"steps": [{"op": "batch.list"}]}},
        {"op": "batch.list", "surprise": 1},
        {"args": {}},
    ]
    env = run_plan(rig, steps)
    assert env["errorCode"] == "INVALID_ARGS" and rig.exec.calls == []
    issues = {i["index"]: i["problems"] for i in env["result"]["issues"]}
    assert sorted(issues) == [1, 2, 3, 4, 5]
    assert "target" in issues[1][0]
    assert "must be an object" in issues[2][0]
    assert "cannot be nested" in issues[3][0]
    assert "unknown key" in issues[4][0]
    assert "'op' is required" in issues[5][0]


def test_step_arguments_are_validated_like_flame_do_does(rig: Rig) -> None:
    env = run_plan(rig, [set_step({"kind": "node", "name": "x"}, {"pos_x": 1})])
    assert env["errorCode"] == "INVALID_ARGS" and env["result"]["issues"][0]["index"] == 0
    assert rig.exec.calls == []


def test_a_plan_may_not_be_empty_or_too_long_or_have_a_bad_on_error(rig: Rig) -> None:
    empty = run_plan(rig, [])
    assert empty["errorCode"] == "INVALID_ARGS" and "at least one step" in str(
        empty["result"]["planProblems"]
    )
    too_long = run_plan(rig, [{"op": "batch.list"}] * (plan_mod.MAX_PLAN_STEPS + 1))
    assert too_long["errorCode"] == "INVALID_ARGS"
    assert f"at most {plan_mod.MAX_PLAN_STEPS} steps" in too_long["result"]["planProblems"][0]
    bad = run_plan(rig, [{"op": "batch.list"}], on_error="shrug")
    assert bad["errorCode"] == "INVALID_ARGS" and "on_error" in bad["result"]["planProblems"][0]
    assert rig.exec.calls == []


def test_the_step_limit_is_50() -> None:
    assert plan_mod.MAX_PLAN_STEPS == 50


def test_continue_with_a_write_is_refused_and_nothing_runs(rig: Rig) -> None:
    steps = [{"op": "batch.list"}, set_step(COMP, {"pos_x": 5.0})]
    env = run_plan(rig, steps, on_error="continue")
    assert env["errorCode"] == "INVALID_ARGS" and rig.exec.calls == []
    assert "only allowed when every step is a read" in env["result"]["planProblems"][0]
    assert "[1]" in env["result"]["planProblems"][0]
    assert comp_pos(rig) == 0.0


def test_read_only_policy_refuses_a_plan_with_a_write_and_runs_a_plan_of_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    denied = run_plan(rig, [{"op": "batch.list"}, set_step(COMP, {"pos_x": 5.0})])
    assert denied["ok"] is False and denied["errorCode"] == "POLICY_DENIED"
    assert [i["index"] for i in denied["result"]["issues"]] == [1]
    assert rig.exec.calls == []
    reads = run_plan(
        rig, [{"op": "batch.list"}, {"op": "node.get_attributes", "args": {"target": COMP}}]
    )
    assert reads["ok"] is True, reads


@pytest.mark.parametrize("enable_eval", [False, True])
@pytest.mark.parametrize("effect", ["read", "destructive"])
def test_read_only_policy_refuses_an_eval_run_step_whatever_effect_it_declares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, effect: str, enable_eval: bool
) -> None:
    env_vars = {"FLAME_MCP_READONLY": "1"}
    if enable_eval:
        env_vars["FLAME_MCP_ENABLE_EVAL"] = "1"
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env(env_vars))
    step = {"op": "eval.run", "args": {"code": "_result = 1", "effect": effect, "target": BG1}}
    env = run_plan(rig, [step])
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert "step 0 (eval.run) is denied by policy" in env["error"]
    assert [i["index"] for i in env["result"]["issues"]] == [0]
    assert env["result"]["ran"] is False
    assert rig.exec.calls == []


def test_a_malformed_step_wins_over_a_policy_denial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = run_plan(rig, [set_step(COMP, {"pos_x": 5.0}), {"op": "node.nope"}])
    assert env["errorCode"] == "INVALID_ARGS"
    assert [i["index"] for i in env["result"]["issues"]] == [0, 1]


def test_plan_run_is_visible_and_allowed_under_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    op = rig.registry.get("plan.run")
    assert op is not None and op.effect == "read" and op.thread == "server"


def test_a_plan_is_refused_up_front_while_writes_are_blocked(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "TIMEOUT")
    set_call = rig.do("node.set_attributes", {"target": COMP, "values": {"pos_x": 9}})
    assert set_call["errorCode"] == "TIMEOUT"
    rig.exec.calls.clear()
    env = run_plan(rig, [{"op": "batch.list"}, set_step(EXPO, {"exposure_red": 1.5})])
    assert env["errorCode"] == "POLICY_DENIED" and env["result"] == {"ran": False}
    assert rig.exec.calls == []  # not even the read before it
    reads = run_plan(rig, [{"op": "batch.list"}])
    assert reads["ok"] is True


def test_a_step_flame_rejects_stops_the_plan_and_earlier_steps_stay_applied(rig: Rig) -> None:
    steps = [
        set_step(COMP, {"pos_x": 5.0}),
        set_step(COMP, {"blend_type": "NoSuchMode"}),  # Flame's own preflight: not an option
        set_step(EXPO, {"exposure_red": 1.5}),
    ]
    env = run_plan(rig, steps)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    result = env["result"]
    assert result["stoppedAt"] == 1
    assert [(s["index"], s["ok"]) for s in result["steps"]] == [(0, True), (1, False)]
    assert result["steps"][1]["errorCode"] == "INVALID_ARGS"
    assert "NoSuchMode" in result["steps"][1]["error"]
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 1}
    assert comp_pos(rig) == 5  # step 0 stays
    assert expo_red(rig) == 0.0  # step 2 never ran
    assert env["affected"] == 1 and env["journal"] == {"group": result["group"]}
    assert rig.write_labels() == ["node.set_attributes", "node.set_attributes"]
    assert states(rig)[result["group"]] == "partial"


def test_a_conflict_in_the_middle_stops_the_plan(rig: Rig) -> None:
    writes = {"n": 0}

    def somebody_else(label: str | None) -> None:
        if label == "node.set_attributes":
            writes["n"] += 1
            if writes["n"] == 2:
                rig.handles["expo"].exposure_red._value = 9.0

    rig.exec.before_run = somebody_else
    env = run_plan(rig, three_writes())
    assert env["errorCode"] == "CONFLICT" and env["result"]["stoppedAt"] == 1
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 1}
    assert (
        expo_red(rig) == 9.0 and reel_name(rig) == "Reel 1"
    )  # nothing written over it, step 2 not run
    assert comp_pos(rig) == 5


def test_a_failing_read_stops_an_abort_plan(rig: Rig) -> None:
    missing = {"kind": "node", "batch": BG1, "name": "nope"}
    env = run_plan(
        rig,
        [
            {"op": "batch.list"},
            {"op": "node.get_attributes", "args": {"target": missing}},
            {"op": "batch.list"},
        ],
    )
    assert env["errorCode"] == "SELECTOR_EMPTY" and env["result"]["stoppedAt"] == 1
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 1}


def test_continue_runs_every_read_step_whatever_fails(rig: Rig) -> None:
    missing = {"kind": "node", "batch": BG1, "name": "nope"}
    env = run_plan(
        rig,
        [
            {"op": "node.get_attributes", "args": {"target": COMP, "keys": ["pos_x"]}},
            {"op": "node.get_attributes", "args": {"target": missing}},
            {"op": "batch.list"},
        ],
        on_error="continue",
    )
    assert env["ok"] is True
    result = env["result"]
    assert result["stoppedAt"] is None
    assert [(s["ok"], s["errorCode"]) for s in result["steps"]] == [
        (True, None),
        (False, "SELECTOR_EMPTY"),
        (True, None),
    ]
    assert env["partial"] == {"done": 2, "failed": 1, "notRun": 0}
    assert result["steps"][2]["result"]["total"] == 2  # step 3 ran


def test_a_write_without_an_answer_stops_the_plan_and_closes_the_door(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "TIMEOUT", when="after")  # step 0 lands, no answer
    env = run_plan(rig, three_writes())
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    result = env["result"]
    assert result["stoppedAt"] == 0 and result["failClosed"]["step"] == 0
    assert result["failClosed"]["errorCode"] == "TIMEOUT"
    assert "journal.check" in result["failClosed"]["note"]
    assert env["partial"] == {"done": 0, "failed": 1, "notRun": 2}
    assert expo_red(rig) == 0.0 and reel_name(rig) == "Reel 1"  # the rest never ran
    assert len(rig.runtime.guard.blocked()) == 1
    denied = rig.do("node.set_attributes", {"target": COMP, "values": {"pos_x": 1}})
    assert denied["errorCode"] == "POLICY_DENIED"
    assert states(rig)[result["group"]] == "unknown"


def test_a_lost_answer_stops_a_plan_even_with_the_check_pending_in_the_group(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "BRIDGE_LOST", when="after")
    env = run_plan(rig, three_writes())
    group = env["result"]["group"]
    found = rig.do("journal.check", {"group": group})
    assert found["result"]["checked"][0]["verdict"] == "landed"
    assert rig.runtime.guard.blocked() == []


def test_a_step_result_too_big_for_the_answer_is_left_out_and_listed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_mod, "PLAN_STEP_RESULT_BYTES", 300)
    env = run_plan(
        rig,
        [
            {"op": "node.list", "args": {"target": BG1}},  # bigger than 300 bytes
            {"op": "node.get_attributes", "args": {"target": COMP, "keys": ["pos_x"]}},
        ],
    )
    assert env["ok"] is True
    first, second = env["result"]["steps"]
    assert first["result"]["omitted"] is True and first["result"]["bytes"] > 300
    assert second["result"]["attributes"]["pos_x"] == {"value": 0.0}
    assert env["result"]["resultsOmitted"] == [0]


def test_step_results_beyond_the_total_budget_are_left_out(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_mod, "PLAN_TOTAL_RESULT_BYTES", 700)
    env = run_plan(rig, [{"op": "node.list", "args": {"target": BG1}}] * 4)
    omitted = env["result"]["resultsOmitted"]
    assert omitted and omitted == list(range(4 - len(omitted), 4))  # the late ones, not the early
    assert env["result"]["steps"][0]["result"].get("omitted") is None
    assert all(env["result"]["steps"][i]["ok"] for i in range(4))


def test_journal_revert_puts_a_whole_plan_back_in_reverse_order(rig: Rig) -> None:
    env = run_plan(rig, three_writes())
    group = env["journal"]["group"]
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert (comp_pos(rig), expo_red(rig), reel_name(rig)) == (0.0, 0.0, "Reel 1")
    result = back["result"]
    assert [w["ok"] for w in result["writes"]] == [True, True, True]
    order = [
        r["op"] for r in rig.records() if r["type"] == "intent" and r["group"] == result["group"]
    ]
    assert order == ["desktop.rename", "node.set_attributes", "node.set_attributes"]  # reversed
    assert [k["keys"] for k in result["revertedKeys"]] == [["name"], ["exposure_red"], ["pos_x"]]
    assert result["remaining"] == [] and back["journal"] == {"group": result["group"]}
    assert back["affected"] == 3
    assert states(rig)[group] == "reverted"
    listing = {i["group"]: i for i in rig.do("journal.list")["result"]["items"]}
    assert (
        listing[result["group"]]["reverts"] == group
        and listing[result["group"]]["state"] == "landed"
    )
    revert_intents = [
        r for r in rig.records() if r["type"] == "intent" and r["group"] == result["group"]
    ]
    assert all("revertsIntent" in r for r in revert_intents)


def test_an_aborted_plan_reverts_only_what_it_applied(rig: Rig) -> None:
    steps = [
        set_step(COMP, {"pos_x": 5.0}),
        set_step(COMP, {"blend_type": "NoSuchMode"}),
        set_step(EXPO, {"exposure_red": 1.5}),
    ]
    group = run_plan(rig, steps)["result"]["group"]
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert (comp_pos(rig), expo_red(rig)) == (0.0, 0.0)
    assert states(rig)[group] == "reverted"


def test_a_plan_group_cannot_be_reverted_twice(rig: Rig) -> None:
    group = run_plan(rig, three_writes())["journal"]["group"]
    assert rig.do("journal.revert", {"group": group})["ok"] is True
    again = rig.do("journal.revert", {"group": group})
    assert again["errorCode"] == "PRECONDITION_UNMET" and "already reverted" in again["error"]


def test_revert_of_a_plan_refuses_everything_if_one_current_value_differs(rig: Rig) -> None:
    group = run_plan(rig, three_writes())["journal"]["group"]
    rig.handles["expo"].exposure_red._value = 9.0  # the middle step's key, changed since
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "CONFLICT" and back["ok"] is False
    assert rig.write_labels() == []  # nothing written, not even the steps that still matched
    assert (comp_pos(rig), reel_name(rig)) == (5, "R1b")
    assert states(rig)[group] == "landed"


def test_revert_of_a_plan_that_wrote_one_key_twice_puts_back_the_first_before(rig: Rig) -> None:
    env = run_plan(rig, [set_step(COMP, {"pos_x": 5.0}), set_step(COMP, {"pos_x": 7.0})])
    assert env["ok"] is True, env
    assert comp_pos(rig) == 7
    group = env["journal"]["group"]
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert comp_pos(rig) == 0
    assert states(rig)[group] == "reverted"


def test_revert_of_a_plan_that_wrote_one_key_three_times_with_others_between(rig: Rig) -> None:
    steps = [
        set_step(COMP, {"pos_x": 5.0}),
        set_step(EXPO, {"exposure_red": 1.5}),
        set_step(COMP, {"pos_x": 7.0}),
        {"op": "desktop.rename", "args": {"target": REEL_WIRETAP, "name": "R1b"}},
        set_step(COMP, {"pos_x": 9.0}),
    ]
    env = run_plan(rig, steps)
    assert env["ok"] is True, env
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert (comp_pos(rig), expo_red(rig), reel_name(rig)) == (0, 0.0, "Reel 1")
    assert [w["ok"] for w in back["result"]["writes"]] == [True] * 5


def test_revert_of_a_plan_that_wrote_one_key_twice_reads_the_target_once(rig: Rig) -> None:
    group = run_plan(rig, [set_step(COMP, {"pos_x": 5.0}), set_step(COMP, {"pos_x": 7.0})])[
        "journal"
    ]["group"]
    rig.handles["comp"].pos_x._value = 99.0  # changed since the last step
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "CONFLICT" and back["ok"] is False
    assert rig.write_labels() == []
    assert rig.exec.labels().count("node.get_attributes") == 1
    assert comp_pos(rig) == 99
    entries = back["result"]["intents"]
    assert [e["keys"]["pos_x"]["current"] for e in entries] == [99.0]


def test_a_change_after_the_last_of_several_writes_to_a_key_still_conflicts(rig: Rig) -> None:
    steps = [set_step(COMP, {"pos_x": float(v)}) for v in (5, 7, 9)]
    group = run_plan(rig, steps)["journal"]["group"]
    rig.handles["comp"].pos_x._value = 99.0
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "CONFLICT" and back["ok"] is False
    assert rig.write_labels() == [] and comp_pos(rig) == 99
    assert states(rig)[group] == "landed"


def test_a_broken_chain_of_writes_to_one_key_conflicts_and_says_why(rig: Rig) -> None:
    group = "g_chain_test"
    args = {"target": COMP, "values": {"pos_x": 5.0}}
    assert rig.runtime.call("node.set_attributes", args, group=group)["ok"] is True
    rig.handles["comp"].pos_x._value = 50.0  # between the two writes, outside the journal
    args = {"target": COMP, "values": {"pos_x": 7.0}}
    assert rig.runtime.call("node.set_attributes", args, group=group)["ok"] is True
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "CONFLICT" and back["ok"] is False
    assert rig.write_labels() == [] and comp_pos(rig) == 7
    entries = back["result"]["intents"]
    assert len(entries) == 1
    entry = entries[0]["keys"]["pos_x"]
    assert entry["match"] is False and entry["expected"] == 5 and entry["current"] == 50
    assert "next write" in entry["reason"] and "50" in entry["reason"]
    assert "not continuous" in entry["reason"]


def test_every_key_compared_with_the_next_write_says_so_in_its_reason(rig: Rig) -> None:
    group = "g_reason_test"
    first = {"target": COMP, "values": {"pos_x": 5.0, "pos_y": 3.0}}
    assert rig.runtime.call("node.set_attributes", first, group=group)["ok"] is True
    rig.handles["comp"].pos_x._value = 50.0  # pos_y is left as the first write set it
    second = {"target": COMP, "values": {"pos_x": 7.0, "pos_y": 4.0}}
    assert rig.runtime.call("node.set_attributes", second, group=group)["ok"] is True
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "CONFLICT" and rig.write_labels() == []
    keys = back["result"]["intents"][0]["keys"]
    assert keys["pos_x"]["match"] is False and keys["pos_y"]["match"] is True
    assert "not continuous" in keys["pos_x"]["reason"]
    assert "next write's before" in keys["pos_y"]["reason"]
    assert "not continuous" not in keys["pos_y"]["reason"]
    assert comp_pos(rig) == 7


def node_write(rig: Rig, group: str, target: dict[str, Any], **values: Any) -> dict[str, Any]:
    env = rig.runtime.call("node.set_attributes", {"target": target, "values": values}, group=group)
    assert env["ok"] is True, env
    return env


def test_two_nodes_writing_one_key_are_not_chained_even_when_the_values_meet(rig: Rig) -> None:
    group = "g_two_nodes"
    node_write(rig, group, COMP, pos_x=5.0)  # comp left 5 ...
    rig.handles["expo"].pos_x._value = 5.0  # ... and exposure1 happens to hold 5 as well
    node_write(rig, group, EXPO, pos_x=8.0)
    rig.handles["comp"].pos_x._value = 77.0  # comp changed since the group left it
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "CONFLICT" and back["ok"] is False
    assert rig.write_labels() == []  # not even exposure1 was put back
    assert rig.handles["expo"].pos_x.get_value() == 8 and comp_pos(rig) == 77
    refused = back["result"]["intents"]
    assert len(refused) == 1 and refused[0]["keys"]["pos_x"]["current"] == 77.0
    assert "reason" not in refused[0]["keys"]["pos_x"]


def test_two_nodes_writing_one_key_are_each_put_back(rig: Rig) -> None:
    group = "g_two_nodes_ok"
    node_write(rig, group, COMP, pos_x=5.0)
    rig.handles["expo"].pos_x._value = 5.0
    node_write(rig, group, EXPO, pos_x=8.0)
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert comp_pos(rig) == 0 and rig.handles["expo"].pos_x.get_value() == 5


def test_writes_of_different_key_sets_to_one_node_each_get_their_own_read(rig: Rig) -> None:
    group = "g_key_sets"
    node_write(rig, group, COMP, pos_x=5.0, pos_y=3.0)
    node_write(rig, group, COMP, pos_x=7.0)
    pos_y_before = rig.handles["comp"].pos_y.get_value()
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert comp_pos(rig) == 0 and rig.handles["comp"].pos_y.get_value() == 0
    assert pos_y_before == 3


def test_writes_of_alternating_key_sets_to_one_node_are_all_put_back(rig: Rig) -> None:
    group = "g_key_sets_alt"
    node_write(rig, group, COMP, pos_x=5.0)
    node_write(rig, group, COMP, pos_x=7.0, pos_y=3.0)
    node_write(rig, group, COMP, pos_x=9.0)
    node_write(rig, group, COMP, pos_x=11.0, pos_y=4.0)
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert comp_pos(rig) == 0 and rig.handles["comp"].pos_y.get_value() == 0


def synthetic_write(
    rig: Rig,
    group: str,
    ident: str,
    target: dict[str, Any],
    target_after: dict[str, Any] | None,
    before: float,
    after: float,
) -> None:
    rig.journal.append(
        {
            "type": "intent",
            "id": ident,
            "group": group,
            "ts": "2026-01-01T00:00:00+00:00",
            "op": "node.set_attributes",
            "args": {},
            "target": target,
            "targetAfter": target_after,
            "readOp": "node.get_attributes",
            "before": {"pos_x": before},
            "after": {"pos_x": after},
        }
    )
    rig.journal.append(
        {
            "type": "outcome",
            "id": ident,
            "ok": True,
            "applied": ["pos_x"],
            "readBack": {"pos_x": after},
        }
    )


def test_a_read_is_shared_only_by_intents_that_also_share_the_address_it_falls_back_to(
    rig: Rig,
) -> None:
    ghost = {"kind": "node", "batch": BG1, "name": "renamed_away"}
    nowhere = {"kind": "node", "batch": BG1, "name": "nowhere"}
    group = "g_target_after"
    # the newer write's target is gone but its targetAfter is comp1; the older one's is nowhere
    synthetic_write(rig, group, "i_older", ghost, nowhere, 0.0, 5.0)
    synthetic_write(rig, group, "i_newer", ghost, COMP, 5.0, 0.0)
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "SELECTOR_EMPTY" and back["ok"] is False
    assert "i_older" in back["error"]
    assert rig.write_labels() == []


def hook_second_write(rig: Rig, value: float) -> None:
    counter = {"n": 0}

    def hook(label: str | None) -> None:
        if label is not None and label.endswith("set_attributes"):
            counter["n"] += 1
            if counter["n"] == 2:
                rig.handles["comp"].pos_x._value = value

    rig.exec.before_run = hook


def partly_reverted_chain(rig: Rig) -> str:
    steps = [set_step(COMP, {"pos_x": float(v)}) for v in (5, 7, 9)]
    group = str(run_plan(rig, steps)["journal"]["group"])
    hook_second_write(rig, 99.0)  # the revert of the 9 lands, then the revert of the 7 is refused
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is False and states(rig)[group] == "partially_reverted", back
    rig.exec.before_run = None
    return group


def test_after_a_partial_revert_the_newest_remaining_write_meets_the_current_value(
    rig: Rig,
) -> None:
    group = partly_reverted_chain(rig)
    rig.handles["comp"].pos_x._value = 50.0  # changed again, from outside
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "CONFLICT" and back["ok"] is False
    assert rig.write_labels() == [] and comp_pos(rig) == 50
    entries = back["result"]["intents"]
    assert len(entries) == 1 and entries[0]["keys"]["pos_x"]["current"] == 50.0
    assert entries[0]["keys"]["pos_x"]["expected"] == 7.0
    assert "reason" not in entries[0]["keys"]["pos_x"]


def test_after_a_partial_revert_the_rest_is_put_back_once_the_value_is_what_the_group_left(
    rig: Rig,
) -> None:
    group = partly_reverted_chain(rig)
    rig.handles["comp"].pos_x._value = 7.0
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert comp_pos(rig) == 0 and states(rig)[group] == "reverted"
    assert [k["keys"] for k in back["result"]["revertedKeys"]] == [["pos_x"], ["pos_x"]]


def interfere_at_write(rig: Rig, nth: int) -> dict[str, int]:
    counter = {"n": 0}

    def hook(label: str | None) -> None:
        if label is not None and (label.endswith("set_attributes") or label.endswith("rename")):
            counter["n"] += 1
            if counter["n"] == nth:
                rig.handles["expo"].exposure_red._value = 9.0

    rig.exec.before_run = hook
    return counter


def test_a_revert_that_fails_part_way_reports_and_marks_the_group(rig: Rig) -> None:
    group = run_plan(rig, three_writes())["journal"]["group"]
    interfere_at_write(
        rig, 2
    )  # counted from here: the revert writes rename (1), expo (2!), comp (3)
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is False and back["errorCode"] == "CONFLICT"
    result = back["result"]
    assert [k["keys"] for k in result["revertedKeys"]] == [["name"]]
    assert [(r["op"], r["keys"]) for r in result["remaining"]] == [
        ("node.set_attributes", ["exposure_red"]),
        ("node.set_attributes", ["pos_x"]),
    ]
    assert back["partial"] == {"done": 1, "failed": 1, "notRun": 1}
    assert back["affected"] == 1 and back["journal"] == {"group": result["group"]}
    assert reel_name(rig) == "Reel 1"  # reverted
    assert expo_red(rig) == 9.0 and comp_pos(rig) == 5  # left alone, later step not attempted
    assert states(rig)[group] == "partially_reverted"
    assert "partially_reverted" in back["error"]
    listing = rig.do("journal.list")["result"]["items"]
    assert [i["state"] for i in listing if i["group"] == group] == ["partially_reverted"]


def test_a_partially_reverted_group_can_be_finished_and_skips_what_is_done(rig: Rig) -> None:
    group = run_plan(rig, three_writes())["journal"]["group"]
    interfere_at_write(rig, 2)
    assert rig.do("journal.revert", {"group": group})["ok"] is False
    rig.exec.before_run = None
    rename_writes = rig.handles["reel"].name  # untouched from here on
    rig.handles["expo"].exposure_red._value = 1.5  # back to what the plan left
    rig.exec.calls.clear()
    again = rig.do("journal.revert", {"group": group})
    assert again["ok"] is True, again
    assert (comp_pos(rig), expo_red(rig), reel_name(rig)) == (0.0, 0.0, "Reel 1")
    assert [k["keys"] for k in again["result"]["revertedKeys"]] == [["exposure_red"], ["pos_x"]]
    assert "desktop.rename" not in rig.exec.labels()  # the rename was not put back twice
    assert rename_writes.get_value() == "Reel 1"
    assert states(rig)[group] == "reverted"


def test_a_partial_revert_stays_partial_when_retried_against_a_changed_value(rig: Rig) -> None:
    group = run_plan(rig, three_writes())["journal"]["group"]
    interfere_at_write(rig, 2)
    rig.do("journal.revert", {"group": group})
    rig.exec.before_run = None  # expo is still 9.0 (not what the plan left)
    again = rig.do("journal.revert", {"group": group})
    assert again["errorCode"] == "CONFLICT" and states(rig)[group] == "partially_reverted"


def test_a_revert_stopped_by_fail_closed_is_partially_reverted(rig: Rig) -> None:
    group = run_plan(rig, three_writes())["journal"]["group"]
    # the revert writes rename, exposure_red, pos_x: the first node write (exposure_red)
    # lands but its answer is lost
    rig.exec.fault("node.set_attributes", "BRIDGE_LOST", when="after")
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "BRIDGE_LOST"
    assert [k["keys"] for k in back["result"]["revertedKeys"]] == [["name"]]
    assert [r["keys"] for r in back["result"]["remaining"]] == [["exposure_red"], ["pos_x"]]
    assert states(rig)[group] == "partially_reverted"
    assert len(rig.runtime.guard.blocked()) == 1


def test_read_only_steps_do_not_touch_a_plans_group_or_journal(rig: Rig) -> None:
    env = run_plan(rig, [set_step(COMP, {"pos_x": 5.0}), {"op": "batch.list"}])
    group = env["journal"]["group"]
    intents = [r for r in rig.records() if r["type"] == "intent"]
    assert [i["group"] for i in intents] == [group]
    assert env["result"]["steps"][1]["journal"] is None


def test_the_journal_being_unwritable_refuses_a_plan_that_writes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_record: Mapping[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.journal, "append", broken)
    env = run_plan(rig, three_writes())
    assert env["errorCode"] == "POLICY_DENIED" and rig.exec.calls == []
    assert comp_pos(rig) == 0.0


# result_detail: 'full' | 'summary' | 'auto' (write steps > 10 means summary)

FULL_STEP_KEYS = {"index", "op", "ok", "errorCode", "error", "result", "journal"}
SUMMARY_STEP_KEYS = {"index", "op", "ok", "errorCode", "brief"}
FAILED_SUMMARY_STEP_KEYS = {"index", "op", "ok", "errorCode", "error", "result"}
SUMMARY_LIMIT_BYTES = 5_000


def many_writes(count: int) -> list[dict[str, Any]]:
    return [set_step(COMP, {"pos_x": float(i + 1)}) for i in range(count)]


def test_summary_steps_carry_only_the_verdict_and_a_small_brief(rig: Rig) -> None:
    env = run_plan(rig, three_writes(), result_detail="summary")
    assert env["ok"] is True, env
    result = env["result"]
    assert result["detail"] == "summary"
    steps = result["steps"]
    assert [set(s) for s in steps] == [SUMMARY_STEP_KEYS] * 3
    assert [(s["index"], s["op"], s["ok"], s["errorCode"]) for s in steps] == [
        (0, "node.set_attributes", True, None),
        (1, "node.set_attributes", True, None),
        (2, "desktop.rename", True, None),
    ]
    assert [s["brief"] for s in steps] == [
        {"applied": ["pos_x"]},
        {"applied": ["exposure_red"]},
        {"applied": ["name"]},
    ]
    assert result["resultsOmitted"] == []
    assert env["journal"] == {"group": result["group"]}
    assert env["partial"] == {"done": 3, "failed": 0, "notRun": 0}


def test_a_summarised_plan_reverts_like_any_other(rig: Rig) -> None:
    env = run_plan(rig, three_writes(), result_detail="summary")
    assert (comp_pos(rig), expo_red(rig), reel_name(rig)) == (5, 1.5, "R1b")
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert (comp_pos(rig), expo_red(rig), reel_name(rig)) == (0, 0.0, "Reel 1")


def test_full_keeps_the_step_shape_it_always_had(rig: Rig) -> None:
    env = run_plan(rig, three_writes(), result_detail="full")
    assert env["ok"] is True, env
    assert env["result"]["detail"] == "full"
    steps = env["result"]["steps"]
    assert [set(s) for s in steps] == [FULL_STEP_KEYS] * 3
    assert steps[0]["result"]["applied"] == ["pos_x"]
    assert steps[0]["journal"]["group"] == env["result"]["group"]
    assert "brief" not in steps[0]


def test_auto_is_the_default_and_means_full_for_a_small_plan(rig: Rig) -> None:
    env = run_plan(rig, three_writes())
    assert env["result"]["detail"] == "full"
    assert set(env["result"]["steps"][0]) == FULL_STEP_KEYS


def test_auto_switches_to_summary_above_ten_write_steps(rig: Rig) -> None:
    ten = run_plan(rig, many_writes(10))
    assert ten["ok"] is True, ten
    assert ten["result"]["detail"] == "full"
    assert set(ten["result"]["steps"][0]) == FULL_STEP_KEYS
    eleven = run_plan(rig, many_writes(11))
    assert eleven["ok"] is True, eleven
    assert eleven["result"]["detail"] == "summary"
    assert [set(s) for s in eleven["result"]["steps"]] == [SUMMARY_STEP_KEYS] * 11


def test_auto_counts_write_steps_not_steps(rig: Rig) -> None:
    reads = [{"op": "node.get_attributes", "args": {"target": COMP, "keys": ["pos_x"]}}] * 12
    env = run_plan(rig, [*many_writes(10), *reads])
    assert env["ok"] is True, env
    assert len(env["result"]["steps"]) == 22 and env["result"]["detail"] == "full"
    only_reads = run_plan(rig, reads * 3)
    assert only_reads["result"]["detail"] == "full" and len(only_reads["result"]["steps"]) == 36


def test_an_explicit_choice_beats_auto(rig: Rig) -> None:
    full = run_plan(rig, many_writes(11), result_detail="full")
    assert full["result"]["detail"] == "full"
    small = run_plan(rig, many_writes(2), result_detail="summary")
    assert small["result"]["detail"] == "summary"


def test_an_unknown_result_detail_is_refused_before_anything_runs(rig: Rig) -> None:
    env = run_plan(rig, three_writes(), result_detail="terse")
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert rig.records() == [] and comp_pos(rig) == 0


def test_a_read_step_and_an_op_outside_the_brief_table_have_no_brief(rig: Rig) -> None:
    rig.registry.register_category(
        Category(
            name="testcat",
            description="t",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    rig.registry.register(
        OperationSpec(
            name="testcat.native_write",
            category="testcat",
            description="a write the brief table has never heard of",
            effect="create",
            params=(OperationParam(name="value", type="string", description="v", required=True),),
            to_python=lambda args: f"flame._thing = {args['value']!r}\n_affected = 1",
            undo_steps=lambda args, affected: 0,
        )
    )
    steps = [
        {"op": "testcat.native_write", "args": {"value": "a"}},
        {"op": "node.get_attributes", "args": {"target": COMP, "keys": ["pos_x"]}},
    ]
    env = run_plan(rig, steps, result_detail="summary")
    assert env["ok"] is True, env
    unknown, read = env["result"]["steps"]
    assert set(unknown) == set(read) == SUMMARY_STEP_KEYS - {"brief"}
    assert unknown["ok"] is True and read["ok"] is True
    assert rig.flame._thing == "a"


def test_a_step_without_the_fields_the_table_names_has_no_brief() -> None:
    assert plan_mod._brief("segment.replace", None) is None
    assert plan_mod._brief("segment.replace", {"position": None}) is None
    assert plan_mod._brief("node.set_attributes", {"applied": []}) == {"applied": []}
    assert plan_mod._brief("no.such_op", {"applied": ["x"]}) is None


def test_a_brief_keeps_the_scalars_of_a_position_and_drops_its_selectors() -> None:
    position = {
        "version": 0,
        "track": 1,
        "index": 3,
        "recordIn": 120,
        "selector": {"kind": "segment", "record_in": 120},
        "selectorByIndex": {"kind": "segment", "index": 3},
    }
    assert plan_mod._brief("segment.trim", {"position": position}) == {
        "position": {"version": 0, "track": 1, "index": 3, "recordIn": 120}
    }


def test_ops_a_plan_refuses_as_steps_are_not_in_the_brief_table() -> None:
    assert not set(plan_mod.BRIEF_PATHS) & set(plan_mod.REFUSED_IN_PLAN)
    assert not set(plan_mod.BRIEF_PATHS) & plan_mod.OWN_GROUP_OPS
    assert all("." not in path for paths in plan_mod.BRIEF_PATHS.values() for path in paths)


def test_a_created_node_has_a_brief_with_its_name_and_type() -> None:
    node = {"name": "b1", "pyType": "PyNode", "type": "Blur", "pos": [0, 0], "sameNameCount": 1}
    expected = {"node": {"name": "b1", "pyType": "PyNode", "type": "Blur", "sameNameCount": 1}}
    for op in ("node.create", "node.duplicate"):
        assert plan_mod._brief(op, {"node": node, "addedNames": ["b1"]}) == expected
    assert plan_mod._brief("node.create", {"node": None}) is None


def test_a_long_list_in_a_brief_is_cut_and_the_rest_counted() -> None:
    limit = plan_mod.BRIEF_LIST_LIMIT
    keys = [f"k{i}" for i in range(limit + 5)]
    brief = plan_mod._brief("node.set_attributes", {"applied": keys})
    assert brief == {"applied": keys[:limit], "appliedOmitted": 5}
    exact = plan_mod._brief("node.set_attributes", {"applied": keys[:limit]})
    assert exact == {"applied": keys[:limit]}


def test_a_failed_step_keeps_its_error_cut_at_600_characters_and_its_result(
    rig: Rig,
) -> None:
    steps = [set_step(COMP, {"pos_x": 5.0}), set_step(COMP, {"blend_type": "Z" * 2000})]
    env = run_plan(rig, steps, result_detail="summary")
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    first, second = env["result"]["steps"]
    assert set(first) == SUMMARY_STEP_KEYS and "error" not in first and "result" not in first
    assert set(second) == FAILED_SUMMARY_STEP_KEYS and second["ok"] is False
    assert second["errorCode"] == "INVALID_ARGS" and len(second["error"]) == 600
    assert second["error"].startswith("set_attributes: applied 0, failed 1")
    assert second["result"]["applied"] == [] and "blend_type" in second["result"]["failed"]
    assert env["result"]["stoppedAt"] == 1 and env["result"]["detail"] == "summary"
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 0}


def test_a_failed_summary_step_result_over_the_step_budget_is_left_out_and_listed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_mod, "PLAN_STEP_RESULT_BYTES", 100)
    steps = [set_step(COMP, {"pos_x": 5.0}), set_step(COMP, {"blend_type": "Z" * 2000})]
    env = run_plan(rig, steps, result_detail="summary")
    second = env["result"]["steps"][1]
    assert second["result"]["omitted"] is True and second["result"]["bytes"] > 100
    assert env["result"]["resultsOmitted"] == [1]


def test_the_failure_message_of_a_summarised_plan_names_the_step(rig: Rig) -> None:
    steps = [set_step(COMP, {"pos_x": 5.0}), set_step(COMP, {"blend_type": "NoSuchMode"})]
    env = run_plan(rig, steps, result_detail="summary")
    assert env["ok"] is False
    assert "plan stopped at step 1 (node.set_attributes)" in env["error"]
    assert "NoSuchMode" in env["error"]


def test_summary_keeps_fail_closed_with_its_journal_reference(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "TIMEOUT", when="after")
    env = run_plan(rig, three_writes(), result_detail="summary")
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    result = env["result"]
    assert result["detail"] == "summary" and result["stoppedAt"] == 0
    closed = result["failClosed"]
    assert closed["step"] == 0 and closed["errorCode"] == "TIMEOUT"
    assert closed["journal"] is not None and "journal.check" in closed["note"]
    assert set(result["steps"][0]) >= {"index", "op", "ok", "errorCode", "error"}
    assert "journal" not in result["steps"][0]
    assert env["partial"] == {"done": 0, "failed": 1, "notRun": 2}
    assert states(rig)[result["group"]] == "unknown"


def test_summary_with_continue_reports_every_read_step(rig: Rig) -> None:
    missing = {"kind": "node", "batch": BG1, "name": "nope"}
    env = run_plan(
        rig,
        [
            {"op": "node.get_attributes", "args": {"target": COMP, "keys": ["pos_x"]}},
            {"op": "node.get_attributes", "args": {"target": missing}},
            {"op": "batch.list"},
        ],
        on_error="continue",
        result_detail="summary",
    )
    assert env["ok"] is True
    steps = env["result"]["steps"]
    assert env["result"]["detail"] == "summary" and env["result"]["stoppedAt"] is None
    assert [(s["ok"], s["errorCode"]) for s in steps] == [
        (True, None),
        (False, "SELECTOR_EMPTY"),
        (True, None),
    ]
    assert "error" in steps[1] and "error" not in steps[0] and "result" not in steps[2]
    assert env["partial"] == {"done": 2, "failed": 1, "notRun": 0}


@pytest.fixture
def timeline_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


LONG_CLIP_NAME = "ULIKE_250521_30s_CUT{:02d}_v03_graded_final_for_conform"


def replace_19(rig: Rig) -> list[dict[str, Any]]:
    steps = []
    for i in range(19):
        new = add_media_clip(rig, LONG_CLIP_NAME.format(i), start=0, length=48)
        steps.append(
            {
                "op": REPLACE,
                "args": {
                    "target": seg_sel(S1, 0, record_in=10),
                    "source": clip_sel(new),
                    "mode": "media",
                },
            }
        )
    return steps


def test_the_timeline_block_survives_a_summary_and_still_reverts(timeline_rig: Rig) -> None:
    rig = timeline_rig
    before = fingerprint_of_target(rig, S1)
    env = rig.do(PLAN, {"steps": replace_19(rig)[:3], "result_detail": "summary"})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["detail"] == "summary"
    timeline = result["timeline"]
    assert {"target", "checkpoint", "stash", "fingerprintBefore", "fingerprintAfter"} <= set(
        timeline
    )
    assert timeline["fingerprintBefore"] == before and timeline["changed"] is True
    assert "resolved" not in timeline
    assert [s["brief"]["position"]["recordIn"] for s in result["steps"]] == [10, 10, 10]
    assert all("selector" not in s["brief"]["position"] for s in result["steps"])
    out = rig.do("journal.revert", {"group": result["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_full_keeps_the_timeline_resolution_list(timeline_rig: Rig) -> None:
    env = timeline_rig.do(PLAN, {"steps": replace_19(timeline_rig)[:3], "result_detail": "full"})
    assert env["ok"] is True, env
    assert [r["op"] for r in env["result"]["timeline"]["resolved"]] == [REPLACE] * 3


def test_19_replaces_overrun_the_full_budget_and_summary_stays_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    full_rig = make_p32b_rig(monkeypatch, tmp_path / "full")
    full = full_rig.do(PLAN, {"steps": replace_19(full_rig), "result_detail": "full"})
    assert full["ok"] is True, full
    omitted = full["result"]["resultsOmitted"]
    assert omitted, "full: the 30,000 byte total never cut a step"
    assert omitted == list(range(19 - len(omitted), 19))
    full_bytes = len(json.dumps(full))

    rig = make_p32b_rig(monkeypatch, tmp_path / "summary")
    summary = rig.do(PLAN, {"steps": replace_19(rig), "result_detail": "summary"})
    assert summary["ok"] is True, summary
    shown = envelope.degrade_for_size(summary)
    assert not shown.get("degraded")
    assert [s["ok"] for s in shown["result"]["steps"]] == [True] * 19
    summary_bytes = len(json.dumps(shown))
    print(f"19 replaces: full {full_bytes} B, summary {summary_bytes} B")
    assert summary_bytes < SUMMARY_LIMIT_BYTES < full_bytes


def test_a_failed_summarised_timeline_plan_keeps_the_revert_hint(timeline_rig: Rig) -> None:
    rig = timeline_rig
    steps = []
    for offset in (2, 9):  # the second slip asks for more handle than the segment has
        target = seg_sel(S1, 0, record_in=10)
        steps.append({"op": "segment.slip", "args": {"target": target, "offset": offset}})
    env = rig.do(PLAN, {"steps": steps, "result_detail": "summary"})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["detail"] == "summary" and env["result"]["stoppedAt"] == 1
    group = env["result"]["group"]
    assert f"journal.revert(group={group!r})" in env["hint"]
    assert "timeline" in env["result"] and "resolved" not in env["result"]["timeline"]
    assert set(env["result"]["steps"][1]) >= {"index", "op", "ok", "errorCode", "error"}
