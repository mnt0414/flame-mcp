"""plan.run: validation before anything runs, one journal group, abort / continue, fail closed,
and journal.revert of a whole plan (including a revert that stops part way). Run through the
real Runtime with a scripted `transport.exec` over a fake Flame tree (journal_test_helpers.py)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from flame_mcp.ops import plan as plan_mod
from flame_mcp.policy import Policy
from flame_mcp.types import Category, OperationParam, OperationSpec
from journal_test_helpers import BG1, COMP, REEL_WIRETAP, Rig, make_rig

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
    return rig.handles["tree_reel"].name.get_value()


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
        expo_red(rig) == 9.0 and reel_name(rig) == "R1"
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
    assert expo_red(rig) == 0.0 and reel_name(rig) == "R1"  # the rest never ran
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
    assert (comp_pos(rig), expo_red(rig), reel_name(rig)) == (0.0, 0.0, "R1")
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
    assert reel_name(rig) == "R1"  # reverted
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
    rename_writes = rig.handles["tree_reel"].name  # untouched from here on
    rig.handles["expo"].exposure_red._value = 1.5  # back to what the plan left
    rig.exec.calls.clear()
    again = rig.do("journal.revert", {"group": group})
    assert again["ok"] is True, again
    assert (comp_pos(rig), expo_red(rig), reel_name(rig)) == (0.0, 0.0, "R1")
    assert [k["keys"] for k in again["result"]["revertedKeys"]] == [["exposure_red"], ["pos_x"]]
    assert "desktop.rename" not in rig.exec.labels()  # the rename was not put back twice
    assert rename_writes.get_value() == "R1"
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
