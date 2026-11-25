"""`flame_mcp.conditions`: the `{path, equals | in | len}` conditions the job runner and
`plan.for_each` share."""

from __future__ import annotations

from typing import Any

import pytest

from flame_mcp import conditions
from flame_mcp.refs import RefsError


def test_equals_does_not_take_true_for_one() -> None:
    assert conditions.holds({"path": "p", "equals": True}, True)
    assert not conditions.holds({"path": "p", "equals": True}, 1)
    assert not conditions.holds({"path": "p", "equals": 1}, True)
    assert conditions.holds({"path": "p", "equals": 1}, 1.0)


def test_in_and_len_judge_a_value_against_a_list_and_a_length() -> None:
    assert conditions.holds({"path": "p", "in": ["a", "b"]}, "b")
    assert not conditions.holds({"path": "p", "in": ["a"]}, "c")
    assert conditions.holds({"path": "p", "len": 2}, [1, 2])
    assert not conditions.holds({"path": "p", "len": 2}, 5)


def test_a_condition_is_described_as_path_operator_value() -> None:
    assert conditions.describe_condition({"path": "a.b", "equals": 1}) == "a.b == 1"
    assert conditions.describe_condition({"path": "a", "in": [1]}) == "a in [1]"
    assert conditions.describe_condition({"path": "a", "len": 2}) == "a len 2"


@pytest.mark.parametrize(
    "bad",
    [None, [], "x", [1], [{"equals": 1}], [{"path": "a"}], [{"path": "a", "equals": 1, "in": [1]}]],
)
def test_a_malformed_list_is_refused(bad: Any) -> None:
    with pytest.raises(RefsError):
        conditions.check_conditions(bad, "where")


def test_templates_pass_only_when_allowed() -> None:
    template = [{"path": "a", "in": "{{ steps.x.result.l }}"}]
    conditions.check_conditions(template, "t", allow_templates=True)
    with pytest.raises(RefsError):
        conditions.check_conditions(template, "t")


def judged(cond: dict[str, Any], root: Any) -> str | None:
    return conditions.why_not(conditions.compile_conditions([cond])[0], root)


def test_a_condition_that_holds_has_no_reason_to_fail() -> None:
    assert judged({"path": "a.b", "equals": 1}, {"a": {"b": 1}}) is None
    assert judged({"path": "a[*]", "in": [1, 2]}, {"a": [1, 2, 1]}) is None


def test_a_missing_path_fails_with_the_reason_the_path_gives() -> None:
    reason = judged({"path": "a.b", "equals": 1}, {"a": {}})
    assert reason is not None and "b" in reason


def test_a_wildcard_over_nothing_fails_as_no_elements() -> None:
    assert judged({"path": "a[*]", "equals": 1}, {"a": []}) == "no elements"


def test_a_value_that_does_not_hold_is_shown_as_got_its_json() -> None:
    assert judged({"path": "a[*]", "equals": 1}, {"a": [1, "x"]}) == 'got "x"'


def test_matches_needs_every_condition_to_hold() -> None:
    root = {"name": "A", "tags": ["x", "y"]}
    both = conditions.compile_conditions(
        [{"path": "name", "equals": "A"}, {"path": "tags", "len": 2}]
    )
    assert conditions.matches(both, root)
    one_off = conditions.compile_conditions(
        [{"path": "name", "equals": "A"}, {"path": "tags", "len": 3}]
    )
    assert not conditions.matches(one_off, root)
    assert conditions.matches([], root)


def test_a_condition_message_names_a_condition_not_a_test() -> None:
    with pytest.raises(RefsError, match="a condition needs 'path'"):
        conditions.check_conditions([{"path": "a"}], "where")
