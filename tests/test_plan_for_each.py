"""`plan.for_each` (reads only): the item list (`items` or `source`), `key`, `where`,
`expect_count`, `{{ item.* }}` and the in-item `{{ steps.* }}` references, the limits, the time
budget, `return`, and the size limit of the answer. Run through the real Runtime over the fake
Flame tree."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import loop_limits, plan_loop_report, plan_step
from flame_mcp.loop_limits import LoopLimits
from flame_mcp.ops import plan_loop
from flame_mcp.policy import Policy
from flame_mcp.refs import RefsError, parse_path
from journal_test_helpers import BG1, REEL_WIRETAP, Rig, make_rig
from rig_fixtures import rig_fixture

LOOP = "plan.for_each"
ITEMS = [{"name": "BG1", "tag": "a", "n": 1}, {"name": "BG2", "tag": "b", "n": 2}]
BY_NAME = {"kind": "batch", "name": "{{ item.name }}"}
INFO = {"id": "i", "op": "batch.info", "args": {"target": BY_NAME}}
PLAIN_INFO = {"op": "batch.info", "args": {"target": BG1}}
SOURCE = {"op": "batch.list", "args": {}, "path": "result.items[*]"}


rig = rig_fixture(make_rig)


def loop(rig: Rig, **args: Any) -> dict[str, Any]:
    args.setdefault("key", "name")
    return rig.do(LOOP, args)


def refused(
    env: dict[str, Any], rig: Rig, text: str | None = None, code: str = "INVALID_ARGS"
) -> None:
    assert env["ok"] is False and env["errorCode"] == code, env
    assert rig.exec.calls == [], rig.exec.calls
    if text is not None:
        assert text in str(env), env


def keys_of(env: dict[str, Any]) -> list[Any]:
    return list(env["result"]["keys"])


def statuses(env: dict[str, Any]) -> dict[Any, str]:
    return {i["key"]: i["status"] for i in env["result"]["items"]}


# ---- registration ----------------------------------------------------------------------------


def test_the_op_is_a_read_in_the_plan_category(rig: Rig) -> None:
    op = rig.registry.get(LOOP)
    assert op is not None
    assert op.category == "plan" and op.effect == "read" and op.undo == "none"


# ---- the list of items -----------------------------------------------------------------------


def test_items_are_run_in_order_with_the_item_values_put_in(rig: Rig) -> None:
    env = loop(rig, items=ITEMS, steps=[INFO])
    assert env["ok"] is True, env
    result = env["result"]
    assert result["total"] == 2 and keys_of(env) == ["BG1", "BG2"]
    assert statuses(env) == {"BG1": "done", "BG2": "done"}
    assert [i["steps"][0]["result"]["name"] for i in result["items"]] == ["BG1", "BG2"]
    assert result["stoppedAt"] is None and result["notRun"] == [] and result["budgetStop"] is None
    assert env["partial"] == {"done": 2, "failed": 0, "notRun": 0}


def test_a_source_read_gives_the_items(rig: Rig) -> None:
    env = loop(rig, source=SOURCE, steps=[INFO])
    assert env["ok"] is True, env
    assert keys_of(env) == ["BG1", "BG2"]


def test_the_source_path_reads_from_result_and_a_plain_list_is_the_list(rig: Rig) -> None:
    for path in ("result.items[*]", "result.items"):
        env = loop(rig, source={**SOURCE, "path": path}, steps=[INFO])
        assert env["ok"] is True, (path, env)
        assert keys_of(env) == ["BG1", "BG2"]


@pytest.mark.parametrize("path", ["items[*]", "items", "", "name"])
def test_a_source_path_without_result_first_is_refused_with_an_example(rig: Rig, path: str) -> None:
    refused(loop(rig, source={**SOURCE, "path": path}, steps=[INFO]), rig, "result.")


def test_only_the_leading_result_is_dropped_so_result_result_reaches_a_result_key() -> None:
    tokens = plan_loop.below_result(parse_path("result.result.x"))
    assert tokens == [("key", "result"), ("key", "x")]
    assert plan_loop.below_result(parse_path("result")) == []
    with pytest.raises(RefsError, match=r"result\."):
        plan_loop.below_result(parse_path("x"))


def test_a_source_that_returned_only_its_first_page_is_refused_before_any_step(rig: Rig) -> None:
    env = loop(rig, source={**SOURCE, "args": {"limit": 1}}, steps=[INFO])
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert "more pages follow" in env["error"] and "limit" in env["error"], env
    assert env["result"]["source"]["shown"] == 1 and env["result"]["source"]["total"] == 2, env
    assert len(rig.exec.calls) == 1, rig.exec.calls


def test_items_and_source_together_are_refused(rig: Rig) -> None:
    refused(loop(rig, items=ITEMS, source=SOURCE, steps=[INFO]), rig, "exactly one")


def test_neither_items_nor_source_is_refused(rig: Rig) -> None:
    refused(loop(rig, steps=[INFO]), rig, "exactly one")


@pytest.mark.parametrize(
    "source",
    [
        {"op": "desktop.rename", "args": {"target": REEL_WIRETAP, "name": "x"}, "path": "result"},
        {"op": "plan.run", "args": {"steps": []}, "path": "result.steps"},
        {"op": "plan.for_each", "args": {}, "path": "result.items"},
        {"op": "eval.run", "args": {}, "path": "result"},
        {"op": "no.such_op", "args": {}, "path": "result"},
        {"args": {}, "path": "result.items"},
        {"op": "batch.list", "args": {}},
        {"op": "batch.list", "args": {}, "path": "result.items", "extra": 1},
        {"op": "batch.list", "args": {}, "path": "result.items["},
        "batch.list",
    ],
)
def test_a_source_that_is_not_a_read_of_one_op_is_refused(rig: Rig, source: Any) -> None:
    refused(loop(rig, source=source, steps=[INFO]), rig)


def test_a_source_op_that_fails_stops_before_any_step(rig: Rig) -> None:
    missing = {"op": "batch.info", "args": {"target": {"kind": "batch", "name": "nope"}}}
    env = loop(rig, source={**missing, "path": "result"}, steps=[PLAIN_INFO])
    assert env["ok"] is False and env["result"]["ran"] is False, env
    assert env["errorCode"] != "INVALID_ARGS"


def test_a_source_path_that_is_missing_is_refused(rig: Rig) -> None:
    env = loop(rig, source={**SOURCE, "path": "result.nothing[*]"}, steps=[INFO])
    assert env["ok"] is False and env["result"]["ran"] is False, env


def test_items_must_be_objects(rig: Rig) -> None:
    refused(loop(rig, items=["BG1"], steps=[PLAIN_INFO]), rig)


# ---- key -------------------------------------------------------------------------------------


def test_key_is_required(rig: Rig) -> None:
    env = rig.do(LOOP, {"items": ITEMS, "steps": [INFO]})
    refused(env, rig, "key")


def test_key_may_be_a_nested_path_and_a_number(rig: Rig) -> None:
    items = [{"selector": {"id": 7}, "name": "BG1"}, {"selector": {"id": 8}, "name": "BG2"}]
    env = loop(rig, items=items, key="selector.id", steps=[INFO])
    assert env["ok"] is True, env
    assert keys_of(env) == [7, 8]


@pytest.mark.parametrize(
    "items",
    [
        [{"name": "BG1"}, {"other": "BG2"}],
        [{"name": "BG1"}, {"name": True}],
        [{"name": "BG1"}, {"name": None}],
        [{"name": "BG1"}, {"name": {"a": 1}}],
        [{"name": "BG1"}, {"name": ["BG2"]}],
        [{"name": "BG1"}, {"name": "BG1"}],
        [{"name": 1}, {"name": "1"}],
    ],
)
def test_a_missing_wrong_typed_or_duplicate_key_refuses_everything(
    rig: Rig, items: list[Any]
) -> None:
    refused(loop(rig, items=items, steps=[PLAIN_INFO]), rig)


def test_key_is_checked_on_the_items_that_remain_after_where(rig: Rig) -> None:
    items = [{"name": "BG1", "use": True}, {"name": "BG1", "use": False}, {"other": 1, "use": 0}]
    env = loop(rig, items=items, where=[{"path": "use", "equals": True}], steps=[PLAIN_INFO])
    assert env["ok"] is True, env
    assert keys_of(env) == ["BG1"]


# ---- where and expect_count ------------------------------------------------------------------

WHERE_ITEMS = [
    {"name": "BG1", "tag": "a", "n": 1, "tags": ["x"]},
    {"name": "BG2", "tag": "b", "n": 2, "tags": ["x", "y"]},
    {"name": "BG3", "tag": "a", "n": 3, "tags": []},
]


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        ([{"path": "tag", "equals": "a"}], ["BG1", "BG3"]),
        ([{"path": "tag", "in": ["b", "z"]}], ["BG2"]),
        ([{"path": "tags", "len": 2}], ["BG2"]),
        ([{"path": "n", "equals": 3}], ["BG3"]),
        ([{"path": "tag", "equals": "a"}, {"path": "tags", "len": 0}], ["BG3"]),
        ([{"path": "tag", "equals": "nope"}], []),
        ([{"path": "missing", "equals": 1}], []),
    ],
)
def test_where_keeps_the_items_that_meet_every_condition(
    rig: Rig, where: list[Any], expected: list[str]
) -> None:
    env = loop(rig, items=WHERE_ITEMS, where=where, steps=[PLAIN_INFO])
    assert env["ok"] is True, env
    assert keys_of(env) == expected and env["result"]["total"] == len(expected)


def test_where_does_not_take_true_for_one(rig: Rig) -> None:
    items = [{"name": "a", "f": True}, {"name": "b", "f": 1}]
    env = loop(rig, items=items, where=[{"path": "f", "equals": True}], steps=[PLAIN_INFO])
    assert keys_of(env) == ["a"]


@pytest.mark.parametrize(
    "where",
    [
        [],
        "tag",
        [{"equals": 1}],
        [{"path": "tag"}],
        [{"path": "tag", "equals": 1, "in": [1]}],
        [{"path": "tag", "in": "a"}],
        [{"path": "tag", "len": -1}],
        [{"path": "tag", "equals": 1, "extra": 1}],
        [{"path": "tag[", "equals": 1}],
    ],
)
def test_a_malformed_where_is_refused(rig: Rig, where: Any) -> None:
    refused(loop(rig, items=WHERE_ITEMS, where=where, steps=[PLAIN_INFO]), rig)


def test_expect_count_that_differs_runs_nothing_and_says_what_was_found(rig: Rig) -> None:
    env = loop(
        rig,
        items=WHERE_ITEMS,
        where=[{"path": "tag", "equals": "a"}],
        expect_count=3,
        steps=[PLAIN_INFO],
    )
    refused(env, rig)
    assert env["result"]["count"] == 2 and env["result"]["keys"] == ["BG1", "BG3"]
    assert env["result"]["expectCount"] == 3


def test_expect_count_that_matches_runs(rig: Rig) -> None:
    env = loop(rig, items=ITEMS, expect_count=2, steps=[PLAIN_INFO])
    assert env["ok"] is True, env


def test_no_items_left_is_an_answer_with_nothing_run(rig: Rig) -> None:
    env = loop(rig, items=ITEMS, where=[{"path": "tag", "equals": "z"}], steps=[PLAIN_INFO])
    assert env["ok"] is True and env["result"]["total"] == 0, env
    assert rig.exec.calls == []


# ---- references ------------------------------------------------------------------------------


def test_a_whole_string_item_reference_keeps_the_type(rig: Rig) -> None:
    items = [
        {
            "name": "BG1",
            "attrs": ["pos_x"],
            "target": {"kind": "node", "batch": BG1, "name": "comp1"},
        }
    ]
    step = {
        "op": "node.get_attributes",
        "args": {"target": "{{ item.target }}", "keys": "{{ item.attrs }}"},
    }
    env = loop(rig, items=items, steps=[step])
    assert env["ok"] is True, env
    assert list(env["result"]["items"][0]["steps"][0]["result"]["attributes"]) == ["pos_x"]


def test_a_reference_inside_a_longer_string_is_embedded_as_text(rig: Rig) -> None:
    items = [{"name": "BG", "n": 1}]
    step = {
        "op": "batch.info",
        "args": {"target": {"kind": "batch", "name": "{{ item.name }}{{ item.n }}"}},
    }
    env = loop(rig, items=items, steps=[step])
    assert env["ok"] is True, env
    assert env["result"]["items"][0]["steps"][0]["result"]["name"] == "BG1"


def test_the_item_itself_can_be_referred_to(rig: Rig) -> None:
    items = [{"name": "BG1", "kind": "batch"}]
    step = {"op": "batch.info", "args": {"target": "{{ item }}"}}
    env = loop(rig, items=items, steps=[step])
    assert env["ok"] is True, env


def test_a_step_may_use_an_earlier_step_of_the_same_item(rig: Rig) -> None:
    again = {
        "id": "again",
        "op": "batch.info",
        "args": {"target": {"kind": "batch", "name": "{{ steps.i.result.name }}"}},
    }
    env = loop(rig, items=ITEMS, steps=[INFO, again])
    assert env["ok"] is True, env
    names = [[s["result"]["name"] for s in i["steps"]] for i in env["result"]["items"]]
    assert names == [["BG1", "BG1"], ["BG2", "BG2"]]


def test_a_reference_to_an_earlier_step_keeps_the_type_of_the_value(rig: Rig) -> None:
    items = [{"name": "BG1"}]
    nodes = {"id": "n", "op": "node.list", "args": {"target": {"kind": "batch", "name": "BG1"}}}
    pick = {
        "op": "node.get_attributes",
        "args": {
            "target": {"kind": "node", "batch": BG1, "name": "{{ steps.n.result.items[1].name }}"},
            "keys": ["pos_x"],
        },
    }
    env = loop(rig, items=items, steps=[nodes, pick])
    assert env["ok"] is True, env


@pytest.mark.parametrize(
    ("steps", "text"),
    [
        ([{"op": "batch.info", "args": {"target": "{{ steps.later.result.x }}"}}, INFO], "no step"),
        (
            [
                {"id": "a", "op": "batch.info", "args": {"target": "{{ steps.b.result.x }}"}},
                {"id": "b", "op": "batch.info", "args": {"target": BG1}},
            ],
            "earlier step only",
        ),
        (
            [{"id": "a", "op": "batch.info", "args": {"target": "{{ steps.a.result.x }}"}}],
            "earlier step only",
        ),
        ([{"id": "a", **PLAIN_INFO}, {"id": "a", **PLAIN_INFO}], "duplicate"),
        ([{"op": "batch.info", "args": {"target": "{{ nope.x }}"}}], "only"),
        ([{"op": "batch.info", "args": {"target": "{{ item. }}"}}], "malformed"),
        ([{"op": "batch.info", "args": {"target": "{{ steps.a.x }}"}}], "result"),
        ([{"op": "batch.info", "args": {"target": "{{ item.name"}}], "malformed"),
    ],
)
def test_bad_references_are_refused_before_anything_runs(
    rig: Rig, steps: list[Any], text: str
) -> None:
    refused(loop(rig, items=ITEMS, steps=steps), rig, text)


def test_plan_run_still_does_not_know_item(rig: Rig) -> None:
    step = {"op": "batch.info", "args": {"target": {"kind": "batch", "name": "{{ item.name }}"}}}
    env = rig.do("plan.run", {"steps": [step]})
    refused(env, rig)


def test_the_values_put_in_are_checked_when_the_step_is_sent(rig: Rig) -> None:
    items = [{"name": "BG1", "target": BG1}, {"name": "BG2", "target": 5}]
    step = {"op": "batch.info", "args": {"target": "{{ item.target }}"}}
    env = loop(rig, items=items, on_error="continue", steps=[step])
    assert statuses(env) == {"BG1": "done", "BG2": "failed"}, env
    failed = env["result"]["items"][1]["steps"][0]
    assert failed["errorCode"] == "INVALID_ARGS", failed


def test_a_read_loop_checks_the_values_of_an_item_when_its_step_is_sent_not_up_front(
    rig: Rig,
) -> None:
    items = [{"name": "BG1"}, {"name": 5}, {"name": "BG2"}]
    aborted = loop(rig, items=items, steps=[INFO])
    assert aborted["ok"] is False and aborted["result"]["stoppedAt"] == 5, aborted
    assert statuses(aborted) == {"BG1": "done", 5: "failed", "BG2": "not_run"}
    went_on = loop(rig, items=items, on_error="continue", steps=[INFO])
    assert went_on["ok"] is True, went_on
    assert statuses(went_on) == {"BG1": "done", 5: "failed", "BG2": "done"}


def test_non_ascii_in_the_items_is_refused_like_any_argument(rig: Rig) -> None:
    refused(loop(rig, items=[{"name": "\u30c6\u30b9\u30c8"}], steps=[INFO]), rig)


# ---- steps that are not allowed --------------------------------------------------------------


def test_a_write_that_cannot_be_put_back_is_refused_with_its_name(rig: Rig) -> None:
    env = loop(rig, items=ITEMS, steps=[PLAIN_INFO, {"op": "project.save", "args": {}}])
    refused(env, rig, "project.save cannot be a plan.for_each step")


@pytest.mark.parametrize(
    "op", ["desktop.copy", "plan.run", "plan.for_each", "eval.run", "render.batch"]
)
def test_refused_ops_are_not_steps(rig: Rig, op: str) -> None:
    refused(loop(rig, items=ITEMS, steps=[{"op": op, "args": {}}]), rig)


def test_late_is_not_accepted(rig: Rig) -> None:
    refused(loop(rig, items=ITEMS, steps=[{**PLAIN_INFO, "late": True}]), rig, "late")


def test_steps_must_be_a_non_empty_list_of_valid_steps(rig: Rig) -> None:
    refused(loop(rig, items=ITEMS, steps=[]), rig)
    refused(loop(rig, items=ITEMS, steps=["batch.info"]), rig)
    refused(loop(rig, items=ITEMS, steps=[{"op": "no.such_op"}]), rig)
    refused(loop(rig, items=ITEMS, steps=[{"op": "batch.info", "args": {"target": 3}}]), rig)


def test_on_error_must_be_abort_or_continue(rig: Rig) -> None:
    refused(loop(rig, items=ITEMS, steps=[PLAIN_INFO], on_error="stop"), rig, "on_error")


# ---- on_error --------------------------------------------------------------------------------

BAD_ITEMS = [{"name": "BG1"}, {"name": "nope"}, {"name": "BG2"}]


def test_abort_stops_at_the_first_failed_item_and_names_the_rest(rig: Rig) -> None:
    env = loop(rig, items=BAD_ITEMS, steps=[INFO])
    assert env["ok"] is False and env["errorCode"] != "INVALID_ARGS", env
    result = env["result"]
    assert result["stoppedAt"] == "nope" and result["notRun"] == ["BG2"]
    assert statuses(env) == {"BG1": "done", "nope": "failed", "BG2": "not_run"}
    assert result["items"][2]["steps"] == []
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 1}


def test_continue_goes_on_to_the_next_item(rig: Rig) -> None:
    env = loop(rig, items=BAD_ITEMS, on_error="continue", steps=[INFO])
    assert env["ok"] is True, env
    assert statuses(env) == {"BG1": "done", "nope": "failed", "BG2": "done"}
    assert env["result"]["stoppedAt"] is None and env["result"]["notRun"] == []
    assert env["partial"] == {"done": 2, "failed": 1, "notRun": 0}


def test_a_failed_step_ends_its_item_and_the_later_steps_of_it_do_not_run(rig: Rig) -> None:
    second = {"id": "second", **PLAIN_INFO}
    env = loop(rig, items=BAD_ITEMS, on_error="continue", steps=[INFO, second])
    failed = env["result"]["items"][1]
    assert failed["status"] == "failed" and [s["id"] for s in failed["steps"]] == ["i"]


def test_an_item_whose_reference_cannot_be_resolved_stops_only_that_item(rig: Rig) -> None:
    items = [{"name": "BG1", "id": "a"}, {"id": "b"}, {"name": "BG2", "id": "c"}]
    env = loop(rig, items=items, key="id", on_error="continue", steps=[INFO])
    assert statuses(env) == {"a": "done", "b": "failed", "c": "done"}
    step = env["result"]["items"][1]["steps"][0]
    assert step["ok"] is False and step["result"]["ran"] is False
    assert step["result"]["reference"] == "{{ item.name }}"
    aborted = loop(rig, items=items, key="id", steps=[INFO])
    assert aborted["ok"] is False and aborted["result"]["stoppedAt"] == "b"
    assert aborted["result"]["notRun"] == ["c"]


def test_a_step_of_one_item_cannot_see_the_steps_of_another(rig: Rig) -> None:
    first = {
        "id": "a",
        "op": "batch.info",
        "args": {"target": {"kind": "batch", "name": "{{ item.name }}"}},
    }
    second = {
        "op": "batch.info",
        "args": {"target": {"kind": "batch", "name": "{{ steps.a.result.name }}"}},
    }
    env = loop(rig, items=ITEMS, steps=[first, second])
    assert env["ok"] is True, env
    assert [i["steps"][1]["result"]["name"] for i in env["result"]["items"]] == ["BG1", "BG2"]


# ---- limits ----------------------------------------------------------------------------------


def test_the_defaults_are_50_items_200_steps_and_120_seconds() -> None:
    assert loop_limits.DEFAULT_LOOP_LIMITS == LoopLimits(50, 200, 120)
    assert loop_limits.loop_limits_from_env({}) == loop_limits.DEFAULT_LOOP_LIMITS


def test_more_items_than_the_limit_are_refused_before_anything_runs(rig: Rig) -> None:
    items = [{"name": f"B{i}"} for i in range(51)]
    refused(loop(rig, items=items, steps=[PLAIN_INFO]), rig, "FLAME_MCP_LOOP_MAX_ITEMS")


def test_the_limit_counts_the_items_that_remain_after_where(rig: Rig) -> None:
    items = [{"name": f"B{i}", "use": i < 2} for i in range(60)]
    env = loop(rig, items=items, where=[{"path": "use", "equals": True}], steps=[PLAIN_INFO])
    assert env["ok"] is True and env["result"]["total"] == 2, env


def test_more_expanded_steps_than_the_limit_are_refused(rig: Rig) -> None:
    rig.runtime.loop_limits = LoopLimits(max_items=50, max_steps=3, budget_s=120)
    refused(loop(rig, items=ITEMS, steps=[PLAIN_INFO, PLAIN_INFO]), rig, "FLAME_MCP_LOOP_MAX_STEPS")
    ok = loop(rig, items=ITEMS[:1], steps=[PLAIN_INFO, PLAIN_INFO])
    assert ok["ok"] is True, ok


def test_a_raised_limit_lets_more_items_through(rig: Rig) -> None:
    rig.runtime.loop_limits = LoopLimits(max_items=60, max_steps=200, budget_s=120)
    items = [{"name": f"B{i}"} for i in range(51)]
    env = loop(rig, items=items, steps=[PLAIN_INFO])
    assert env["ok"] is True and env["result"]["total"] == 51, env


def test_the_limits_come_from_the_environment() -> None:
    env = {
        "FLAME_MCP_LOOP_MAX_ITEMS": "7",
        "FLAME_MCP_LOOP_MAX_STEPS": "30",
        "FLAME_MCP_LOOP_BUDGET_S": "9",
    }
    assert loop_limits.loop_limits_from_env(env) == LoopLimits(7, 30, 9)
    assert loop_limits.loop_limits_from_env({"FLAME_MCP_LOOP_BUDGET_S": "9"}) == LoopLimits(
        50, 200, 9
    )


@pytest.mark.parametrize("name", ["MAX_ITEMS", "MAX_STEPS", "BUDGET_S"])
@pytest.mark.parametrize("value", ["", "0", "-1", "abc", "1.5", " 5", "5 ", "5s", "٣", "1" * 10])
def test_an_unusable_value_is_a_config_error_naming_the_variable_and_the_value(
    name: str, value: str
) -> None:
    var = f"FLAME_MCP_LOOP_{name}"
    with pytest.raises(loop_limits.LoopLimitsConfigError, match=var) as info:
        loop_limits.loop_limits_from_env({var: value})
    assert repr(value) in str(info.value)


# ---- time budget -----------------------------------------------------------------------------


def fake_clock(monkeypatch: pytest.MonkeyPatch, times: list[float]) -> None:
    ticks = iter(times)
    monkeypatch.setattr(plan_loop_report, "_now", lambda: next(ticks))


def test_a_spent_budget_stops_between_items_as_a_pause_not_a_failure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [{"name": "BG1"}, {"name": "BG2"}, {"name": "BG1b"}, {"name": "BG2b"}]
    fake_clock(monkeypatch, [0, 0, 100, 121, 999])  # start, then once before each item
    env = loop(rig, items=items, steps=[PLAIN_INFO, PLAIN_INFO])
    assert env["ok"] is True, env
    result = env["result"]
    assert statuses(env) == {
        "BG1": "done",
        "BG2": "done",
        "BG1b": "not_run",
        "BG2b": "not_run",
    }
    assert result["notRun"] == ["BG1b", "BG2b"] and result["stoppedAt"] is None
    assert result["budgetStop"]["budgetS"] == 120 and result["budgetStop"]["after"] == "BG2"
    assert all(len(i["steps"]) == 2 for i in result["items"][:2])  # never stops inside an item
    assert env["partial"] == {"done": 2, "failed": 0, "notRun": 2}


def test_a_budget_not_spent_runs_every_item(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_clock(monkeypatch, [0, 0, 60, 120])
    env = loop(rig, items=[{"name": "a"}, {"name": "b"}], steps=[PLAIN_INFO])
    assert env["ok"] is True and env["result"]["budgetStop"] is None, env
    assert statuses(env) == {"a": "done", "b": "done"}


def test_the_budget_comes_from_the_limits(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.runtime.loop_limits = LoopLimits(max_items=50, max_steps=200, budget_s=5)
    fake_clock(monkeypatch, [0, 0, 6])
    env = loop(rig, items=[{"name": "a"}, {"name": "b"}], steps=[PLAIN_INFO])
    assert env["result"]["notRun"] == ["b"] and env["result"]["budgetStop"]["budgetS"] == 5


# ---- return ----------------------------------------------------------------------------------


def test_return_gives_only_the_paths_chosen(rig: Rig) -> None:
    step = {**INFO, "return": ["result.name", "result.nodeCount", "result.reels"]}
    env = loop(rig, items=ITEMS[:1], steps=[step])
    summary = env["result"]["items"][0]["steps"][0]
    assert summary["result"] == {"result.name": "BG1", "result.nodeCount": 4, "result.reels": []}, (
        summary
    )
    assert "journal" not in summary


def test_return_may_map_over_a_list(rig: Rig) -> None:
    step = {"op": "batch.list", "args": {}, "return": ["result.items[*].name"]}
    env = loop(rig, items=ITEMS[:1], steps=[step])
    assert env["result"]["items"][0]["steps"][0]["result"] == {
        "result.items[*].name": ["BG1", "BG2"]
    }


def test_a_path_the_result_does_not_have_is_named(rig: Rig) -> None:
    step = {**INFO, "return": ["result.name", "result.nothing"]}
    env = loop(rig, items=ITEMS[:1], steps=[step])
    summary = env["result"]["items"][0]["steps"][0]
    assert summary["result"] == {"result.name": "BG1"}
    assert summary["returnMissing"] == ["result.nothing"]


def test_an_empty_return_keeps_the_result_out_but_the_reference_still_works(rig: Rig) -> None:
    quiet = {**INFO, "return": []}
    again = {
        "id": "again",
        "op": "batch.info",
        "args": {"target": {"kind": "batch", "name": "{{ steps.i.result.name }}"}},
        "return": ["result.name"],
    }
    env = loop(rig, items=ITEMS, steps=[quiet, again])
    assert env["ok"] is True, env
    first, second = env["result"]["items"][1]["steps"]
    assert "result" not in first and first["ok"] is True and first["id"] == "i"
    assert second["result"] == {"result.name": "BG2"}


def test_a_failed_step_keeps_its_error_whatever_return_says(rig: Rig) -> None:
    step = {**INFO, "return": []}
    env = loop(rig, items=[{"name": "nope"}], steps=[step])
    summary = env["result"]["items"][0]["steps"][0]
    assert summary["ok"] is False and summary["error"] and "result" in summary


@pytest.mark.parametrize("bad", ["name", [1], ["a["], {"a": 1}, ["name"], ["items[*]"]])
def test_a_malformed_return_is_refused(rig: Rig, bad: Any) -> None:
    refused(loop(rig, items=ITEMS, steps=[{**PLAIN_INFO, "return": bad}]), rig)


# ---- size of the answer ----------------------------------------------------------------------


def test_a_result_too_big_for_the_answer_is_left_out_and_named(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_step, "PLAN_STEP_RESULT_BYTES", 10)
    env = loop(rig, items=ITEMS, steps=[INFO])
    assert env["result"]["resultsOmitted"] == [
        {"key": "BG1", "step": "i"},
        {"key": "BG2", "step": "i"},
    ]
    assert env["result"]["items"][0]["steps"][0]["result"]["omitted"] is True


def test_the_results_together_are_held_to_the_total_budget(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    size = len(json.dumps(rig.do("batch.info", {"target": BG1})["result"]))
    monkeypatch.setattr(plan_step, "PLAN_TOTAL_RESULT_BYTES", size + 10)
    env = loop(rig, items=ITEMS, steps=[INFO])
    omitted = env["result"]["resultsOmitted"]
    assert omitted and omitted[0]["key"] == "BG2"


def test_a_reference_reaches_a_result_left_out_of_the_answer(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_step, "PLAN_STEP_RESULT_BYTES", 10)
    again = {
        "op": "batch.info",
        "args": {"target": {"kind": "batch", "name": "{{ steps.i.result.name }}"}},
    }
    env = loop(rig, items=ITEMS, steps=[INFO, again])
    assert env["ok"] is True, env


def test_label_is_kept_in_the_answer(rig: Rig) -> None:
    env = loop(rig, items=ITEMS, steps=[PLAIN_INFO], label="check")
    assert env["result"]["label"] == "check" and env["result"]["onError"] == "abort"


def test_startup_refuses_an_unusable_loop_limit_and_says_which(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from flame_mcp.__main__ import build_server

    monkeypatch.setenv("FLAME_MCP_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("FLAME_MCP_LOOP_BUDGET_S", "0")
    with pytest.raises(SystemExit) as info:
        build_server(run_dir=tmp_path / "run")
    assert info.value.code == 1
    assert "FLAME_MCP_LOOP_BUDGET_S" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["MAX_ITEMS", "MAX_STEPS", "BUDGET_S"])
def test_a_value_with_thousands_of_digits_is_a_config_error_not_a_bare_value_error(
    name: str,
) -> None:
    var = f"FLAME_MCP_LOOP_{name}"
    with pytest.raises(loop_limits.LoopLimitsConfigError, match=var) as info:
        loop_limits.loop_limits_from_env({var: "1" * 4301})
    assert len(str(info.value)) < 200


def test_nine_digits_are_still_a_value() -> None:
    env = {"FLAME_MCP_LOOP_MAX_ITEMS": "999999999"}
    assert loop_limits.loop_limits_from_env(env).max_items == 999_999_999


# ---- policy ----------------------------------------------------------------------------------


def policy_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Rig:
    return make_rig(monkeypatch, tmp_path, policy=Policy.from_env(env))


def test_a_read_loop_runs_while_the_server_is_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = policy_rig(monkeypatch, tmp_path, FLAME_MCP_READONLY="1")
    env = loop(ro, items=ITEMS, steps=[INFO])
    assert env["ok"] is True and statuses(env) == {"BG1": "done", "BG2": "done"}, env


def test_a_write_step_is_refused_while_the_server_is_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = policy_rig(monkeypatch, tmp_path, FLAME_MCP_READONLY="1")
    write = {"op": "desktop.rename", "args": {"target": REEL_WIRETAP, "name": "x"}}
    refused(loop(ro, items=ITEMS, steps=[write]), ro, "denied by policy", "POLICY_DENIED")


def test_a_step_the_policy_denies_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    only = policy_rig(monkeypatch, tmp_path, FLAME_MCP_ALLOW_CATEGORIES="plan,desktop")
    refused(loop(only, items=ITEMS, steps=[INFO]), only, "denied by policy", "POLICY_DENIED")


def test_a_source_op_the_policy_denies_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    only = policy_rig(monkeypatch, tmp_path, FLAME_MCP_ALLOW_CATEGORIES="plan,desktop")
    env = loop(only, source=SOURCE, steps=[PLAIN_INFO])
    refused(env, only, "denied by policy", "POLICY_DENIED")


# ---- budget after a failed item --------------------------------------------------------------


def test_budget_stop_after_a_failed_item_names_that_item_and_the_run_is_still_ok(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_clock(monkeypatch, [0, 0, 10, 121])
    items = [{"name": "BG1"}, {"name": "nope"}, {"name": "BG2"}]
    env = loop(rig, items=items, on_error="continue", steps=[INFO])
    assert env["ok"] is True, env
    assert statuses(env) == {"BG1": "done", "nope": "failed", "BG2": "not_run"}
    assert env["result"]["budgetStop"]["after"] == "nope"
    assert env["result"]["notRun"] == ["BG2"] and env["partial"]["failed"] == 1
