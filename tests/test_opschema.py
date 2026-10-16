from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from flame_mcp.opschema import (
    compact_params,
    describe_value,
    first_sentence,
    suggest_name,
    summarize_params,
    type_matches,
    validate_op_args,
)
from flame_mcp.types import OperationParam, ParamType, SelectorKind


@dataclass
class FakeOp:
    params: tuple[OperationParam, ...]
    accepts: tuple[SelectorKind, ...] = ()


def make_op(*params: OperationParam, accepts: tuple[SelectorKind, ...] = ()) -> FakeOp:
    return FakeOp(params=params, accepts=accepts)


@pytest.mark.parametrize(
    "type_,value,expected",
    [
        ("string", "hi", True),
        ("string", 1, False),
        ("number", 1, True),
        ("number", 1.5, True),
        ("number", True, False),  # bool must not pass as number
        ("number", float("inf"), False),
        ("boolean", True, True),
        ("boolean", 1, False),
        ("array", [1, 2], True),
        ("array", {}, False),
        ("object", {"a": 1}, True),
        ("object", [1], False),
        ("any", None, True),
        ("any", object(), True),
    ],
)
def test_type_matches(type_: ParamType, value: object, expected: bool) -> None:
    assert type_matches(type_, value) is expected


def test_describe_value_variants() -> None:
    assert describe_value(None) == "null"
    assert describe_value([1, 2]) == "array"
    assert describe_value(True) == "boolean"
    assert describe_value(float("nan")) == "non-finite number"
    assert describe_value(3) == "number"
    assert describe_value("x") == "string"
    assert describe_value({"a": 1}) == "object"


def test_suggest_name_close_typo() -> None:
    assert suggest_name("nam", ["name", "uid", "colour"]) == "name"


def test_suggest_name_no_close_candidate() -> None:
    assert suggest_name("zzzzzzzzzz", ["name", "uid"]) is None


def test_suggest_name_exact_match() -> None:
    assert suggest_name("name", ["name", "uid"]) == "name"


def test_summarize_params_formats_required_and_nullable() -> None:
    params = (
        OperationParam(name="name", type="string", description="d", required=True),
        OperationParam(name="tag", type="string", description="d", nullable=True),
    )
    summary = summarize_params(params)
    assert summary == ["name: string (required)", "tag: string (optional, nullable)"]


def test_compact_params_marks_required_with_a_star() -> None:
    params = (
        OperationParam(name="target", type="object", description="d", required=True),
        OperationParam(name="limit", type="number", description="d"),
    )
    assert compact_params(params) == ["target*", "limit"]
    assert compact_params(()) == []


def test_first_sentence_stops_at_the_first_full_stop_before_a_capital() -> None:
    text = "Rename one entry (library/...). WARNING: a PyAttribute write.\nMore text."
    assert first_sentence(text) == "Rename one entry (library/...)."


def test_first_sentence_is_not_ended_by_e_g_or_a_dotted_name() -> None:
    assert first_sentence("Nodes (the class, e.g. PyActionNode), bypass. Then more.") == (
        "Nodes (the class, e.g. PyActionNode), bypass."
    )
    assert first_sentence("Eval.run / restore groups: put back. Next.") == (
        "Eval.run / restore groups: put back."
    )


def test_first_sentence_keeps_a_text_without_a_full_stop_whole() -> None:
    assert first_sentence("Name/duration/frame rate and counts.") == (
        "Name/duration/frame rate and counts."
    )


def test_first_sentence_cuts_a_long_one_to_the_limit_with_an_ellipsis() -> None:
    cut = first_sentence("word " * 100)
    assert len(cut) == 160 and cut.endswith("\u2026")
    assert first_sentence("a" * 160) == "a" * 160
    assert len(first_sentence("a" * 161)) == 160


def test_validate_op_args_missing_required() -> None:
    op = make_op(OperationParam(name="name", type="string", description="d", required=True))
    result = validate_op_args(op, {})
    assert result.ok is False
    assert result.issues[0].path == "name"


def test_validate_op_args_type_mismatch() -> None:
    op = make_op(OperationParam(name="count", type="number", description="d"))
    result = validate_op_args(op, {"count": "not a number"})
    assert result.ok is False
    assert "must be number" in result.issues[0].message


def test_validate_op_args_bool_rejected_for_number() -> None:
    op = make_op(OperationParam(name="count", type="number", description="d"))
    result = validate_op_args(op, {"count": True})
    assert result.ok is False


def test_validate_op_args_none_rejected_when_not_nullable() -> None:
    op = make_op(OperationParam(name="tag", type="string", description="d"))
    result = validate_op_args(op, {"tag": None})
    assert result.ok is False
    assert "omit the key" in result.issues[0].message


def test_validate_op_args_none_accepted_when_nullable() -> None:
    op = make_op(OperationParam(name="tag", type="string", description="d", nullable=True))
    result = validate_op_args(op, {"tag": None})
    assert result.ok is True


def test_validate_op_args_none_accepted_for_any_type() -> None:
    op = make_op(OperationParam(name="tag", type="any", description="d"))
    result = validate_op_args(op, {"tag": None})
    assert result.ok is True


def test_validate_op_args_unknown_key_suggests_candidate() -> None:
    op = make_op(OperationParam(name="name", type="string", description="d"))
    result = validate_op_args(op, {"nam": "x"})
    assert result.ok is False
    assert "did you mean 'name'" in result.issues[0].message


def test_validate_op_args_unknown_key_no_candidate() -> None:
    op = make_op(OperationParam(name="name", type="string", description="d"))
    result = validate_op_args(op, {"zzzzzzzzzz": "x"})
    assert result.ok is False
    assert "this operation accepts" in result.issues[0].message


def test_validate_op_args_success_returns_value() -> None:
    op = make_op(OperationParam(name="name", type="string", description="d", required=True))
    result = validate_op_args(op, {"name": "clip1"})
    assert result.ok is True
    assert result.value == {"name": "clip1"}


def test_validate_op_args_defaults_not_filled_in() -> None:
    op = make_op(
        OperationParam(name="name", type="string", description="d", default="unused-default")
    )
    result = validate_op_args(op, {})
    assert result.ok is True
    assert result.value == {}


def test_validate_op_args_non_object_rejected() -> None:
    op = make_op()
    result = validate_op_args(op, "not an object")
    assert result.ok is False
    assert result.issues[0].path == "args"


def test_validate_op_args_none_raw_becomes_empty_object() -> None:
    op = make_op()
    result = validate_op_args(op, None)
    assert result.ok is True
    assert result.value == {}


def test_validate_op_args_non_empty_string_rejects_empty() -> None:
    op = make_op(
        OperationParam(name="name", type="string", description="d", required=True, non_empty=True)
    )
    result = validate_op_args(op, {"name": ""})
    assert result.ok is False
    assert "must not be empty" in result.issues[0].message


def test_validate_op_args_non_empty_string_accepts_non_empty() -> None:
    op = make_op(
        OperationParam(name="name", type="string", description="d", required=True, non_empty=True)
    )
    result = validate_op_args(op, {"name": "x"})
    assert result.ok is True


def test_validate_op_args_selector_param_wrong_shape_rejected() -> None:
    op = make_op(
        OperationParam(name="target", type="selector", description="d", required=True),
        accepts=("wiretap", "path", "selection"),
    )
    result = validate_op_args(op, {"target": "not-an-object"})
    assert result.ok is False
    assert "must be selector" in result.issues[0].message


def test_validate_op_args_selector_param_unaccepted_kind_rejected() -> None:
    op = make_op(
        OperationParam(name="target", type="selector", description="d", required=True),
        accepts=("wiretap",),
    )
    result = validate_op_args(op, {"target": {"kind": "selection"}})
    assert result.ok is False
    assert "not accepted here" in result.issues[0].message


def test_validate_op_args_selector_param_accepted_kind_ok() -> None:
    op = make_op(
        OperationParam(name="target", type="selector", description="d", required=True),
        accepts=("wiretap", "selection"),
    )
    result = validate_op_args(op, {"target": {"kind": "selection"}})
    assert result.ok is True


def test_validate_op_args_selector_param_malformed_wiretap_rejected() -> None:
    op = make_op(
        OperationParam(name="target", type="selector", description="d", required=True),
        accepts=("wiretap",),
    )
    result = validate_op_args(op, {"target": {"kind": "wiretap", "id": ""}})
    assert result.ok is False
    assert "non-empty string" in result.issues[0].message


def _offset_op() -> Any:
    return make_op(
        OperationParam(name="offset", type="number", description="d", integer=True, minimum=0)
    )


def test_validate_op_args_integer_minimum_accepts_zero_and_positive() -> None:
    op = _offset_op()
    assert validate_op_args(op, {"offset": 0}).ok is True
    assert validate_op_args(op, {"offset": 50}).ok is True
    assert validate_op_args(op, {"offset": 2.0}).ok is True


def test_validate_op_args_integer_rejects_fraction() -> None:
    result = validate_op_args(_offset_op(), {"offset": 1.5})
    assert result.ok is False
    assert "integer" in result.issues[0].message


def test_validate_op_args_minimum_rejects_negative() -> None:
    result = validate_op_args(_offset_op(), {"offset": -1})
    assert result.ok is False
    assert result.issues[0].path == "offset"
    assert ">= 0" in result.issues[0].message


def _depth_op() -> Any:
    return make_op(
        OperationParam(
            name="bit_depth", type="number", description="d", integer=True, enum=(8, 10, 12)
        )
    )


def test_validate_op_args_enum_accepts_a_listed_value() -> None:
    assert validate_op_args(_depth_op(), {"bit_depth": 10}).ok is True
    assert validate_op_args(_depth_op(), {"bit_depth": 12.0}).ok is True


@pytest.mark.parametrize("bad", [0, 9, 11, 16, -8])
def test_validate_op_args_enum_rejects_a_value_not_listed(bad: int) -> None:
    result = validate_op_args(_depth_op(), {"bit_depth": bad})
    assert result.ok is False
    assert result.issues[0].path == "bit_depth"
    assert "one of 8, 10, 12" in result.issues[0].message and repr(bad) in result.issues[0].message


@pytest.mark.parametrize("bad", ["10", "10-bit", True, None])
def test_validate_op_args_enum_still_needs_the_declared_type(bad: Any) -> None:
    assert validate_op_args(_depth_op(), {"bit_depth": bad}).ok is False


def test_validate_op_args_enum_does_not_repeat_an_integer_complaint() -> None:
    result = validate_op_args(_depth_op(), {"bit_depth": 10.5})
    assert [i.message for i in result.issues] == ["must be an integer, got 10.5"]


def test_validate_op_args_enum_works_for_strings_too() -> None:
    op = make_op(OperationParam(name="mode", type="string", description="d", enum=("a", "b")))
    assert validate_op_args(op, {"mode": "a"}).ok is True
    result = validate_op_args(op, {"mode": "c"})
    assert result.ok is False and "one of 'a', 'b'" in result.issues[0].message


def test_validate_op_args_item_type_rejects_wrong_element() -> None:
    op = make_op(OperationParam(name="types", type="array", description="d", item_type="string"))
    assert validate_op_args(op, {"types": ["PyClip", "PyReel"]}).ok is True
    result = validate_op_args(op, {"types": ["PyClip", 3]})
    assert result.ok is False
    assert result.issues[0].path == "types[1]"


# NaN and Infinity never reach the generated code.


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("where", ["top", "in_list", "in_dict", "deep"])
def test_validate_op_args_rejects_non_finite_floats_anywhere_in_object_values(
    bad: float, where: str
) -> None:
    value: Any = {
        "top": {"a": bad},
        "in_list": {"a": [1.0, bad]},
        "in_dict": {"a": {"b": bad}},
        "deep": {"a": [{"b": [0, {"c": bad}]}]},
    }[where]
    op = make_op(OperationParam(name="values", type="object", description="d", required=True))
    result = validate_op_args(op, {"values": value})
    assert result.ok is False
    assert any("NaN or Infinity" in issue.message for issue in result.issues)
    assert any(issue.path.startswith("values[") for issue in result.issues)


def test_validate_op_args_rejects_non_finite_in_array_and_any_params() -> None:
    op = make_op(
        OperationParam(name="items", type="array", description="d"),
        OperationParam(name="anything", type="any", description="d"),
    )
    result = validate_op_args(op, {"items": [1, float("nan")], "anything": float("inf")})
    assert {issue.path for issue in result.issues} == {"items[1]", "anything"}


def test_validate_op_args_accepts_finite_floats_in_object_values() -> None:
    op = make_op(OperationParam(name="values", type="object", description="d", required=True))
    assert validate_op_args(op, {"values": {"a": 1.5, "b": [0.0, -2.5e300], "c": {"d": 1e-9}}}).ok


def test_set_attributes_refuses_nan_before_generating_code() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    registry = Registry()
    register_all(registry)
    op = registry.get("node.set_attributes")
    assert op is not None
    target = {"kind": "node", "batch": {"kind": "batch", "name": "B"}, "name": "n"}
    for bad in (float("nan"), float("inf"), float("-inf")):
        result = validate_op_args(op, {"target": target, "values": {"pos_x": bad}})
        assert result.ok is False, bad


def test_first_sentence_without_a_limit_keeps_a_long_sentence_whole() -> None:
    text = "word " * 100
    assert first_sentence(text, limit=None) == " ".join(text.split())
