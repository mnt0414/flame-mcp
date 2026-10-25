"""The pure text functions behind `segment.edit_effect_setup` and `node.edit_setup`.

They replace the value of a tag that occurs exactly once as `<TAG>value</TAG>` in a setup file and
refuse everything else. They run inside Flame too (`setup_edit_prelude` embeds their source), so
the last tests run that embedded copy."""

from __future__ import annotations

from typing import Any

import pytest

from flame_mcp import setup_edit as se
from flame_mcp.setup_edit_prelude import SETUP_EDIT_HELPER

TIMEWARP = (
    "<TimewarpNode><TW_FrameMode>2</TW_FrameMode><TW_Bypass>False</TW_Bypass>"
    '<TW_Speed><Channel Name="Speed"><Value>100</Value><Value>200</Value></Channel></TW_Speed>'
    '<Range Enable="False" Start="1"/><Frame>1</Frame><Frame>2</Frame></TimewarpNode>'
)


def test_a_tag_that_occurs_once_is_counted_once() -> None:
    assert se.count_tags(TIMEWARP, ["TW_FrameMode"]) == {"TW_FrameMode": 1}
    assert se.tag_problems(TIMEWARP, ["TW_FrameMode"]) == {}


def test_a_tag_that_is_not_there_is_counted_zero_and_refused() -> None:
    assert se.count_tags(TIMEWARP, ["TW_Nope"]) == {"TW_Nope": 0}
    problems = se.tag_problems(TIMEWARP, ["TW_Nope"])
    assert "not in the setup" in problems["TW_Nope"]


def test_a_tag_that_occurs_twice_is_refused() -> None:
    assert se.count_tags(TIMEWARP, ["Frame"]) == {"Frame": 2}
    assert "2 times" in se.tag_problems(TIMEWARP, ["Frame"])["Frame"]


def test_a_tag_with_child_elements_is_refused() -> None:
    assert se.count_tags(TIMEWARP, ["TW_Speed"]) == {"TW_Speed": 1}
    assert "child" in se.tag_problems(TIMEWARP, ["TW_Speed"])["TW_Speed"]


def test_a_tag_with_attributes_is_refused() -> None:
    text = '<A><Mode Kind="x">1</Mode></A>'
    assert se.count_tags(text, ["Mode"]) == {"Mode": 1}
    assert "attributes" in se.tag_problems(text, ["Mode"])["Mode"]


def test_a_self_closing_tag_is_refused() -> None:
    text = "<A><Mode/><Other>1</Other></A>"
    assert se.count_tags(text, ["Mode"]) == {"Mode": 1}
    assert se.tag_problems(text, ["Mode"])["Mode"]
    spaced = "<A><Mode /></A>"
    assert se.tag_problems(spaced, ["Mode"])["Mode"]


def test_a_scalar_tag_that_also_occurs_with_attributes_is_refused() -> None:
    text = '<A><Mode>1</Mode><Mode Kind="x"/></A>'
    assert se.count_tags(text, ["Mode"]) == {"Mode": 2}
    assert se.tag_problems(text, ["Mode"])["Mode"]


def test_a_tag_that_starts_with_the_same_letters_is_not_counted() -> None:
    text = "<A><TW_Frame>7</TW_Frame><TW_FrameMode>2</TW_FrameMode></A>"
    assert se.count_tags(text, ["TW_Frame", "TW_FrameMode"]) == {"TW_Frame": 1, "TW_FrameMode": 1}
    assert se.replace_tags(text, {"TW_Frame": 8}) == (
        "<A><TW_Frame>8</TW_Frame><TW_FrameMode>2</TW_FrameMode></A>"
    )


def test_an_empty_value_is_a_scalar_tag() -> None:
    text = "<A><Note></Note></A>"
    assert se.tag_problems(text, ["Note"]) == {}
    assert se.replace_tags(text, {"Note": "x"}) == "<A><Note>x</Note></A>"


def test_the_replacement_changes_nothing_but_the_value_and_keeps_crlf() -> None:
    text = "<A>\r\n  <TW_FrameMode>2</TW_FrameMode>\r\n  <B>1</B>\r\n</A>\r\n"
    out = se.replace_tags(text, {"TW_FrameMode": 0})
    assert out == "<A>\r\n  <TW_FrameMode>0</TW_FrameMode>\r\n  <B>1</B>\r\n</A>\r\n"


def test_several_tags_are_replaced_in_one_call() -> None:
    out = se.replace_tags(TIMEWARP, {"TW_FrameMode": 0, "TW_Bypass": True})
    assert "<TW_FrameMode>0</TW_FrameMode>" in out and "<TW_Bypass>True</TW_Bypass>" in out
    assert (
        out.replace("0</TW_FrameMode>", "2</TW_FrameMode>").replace(
            "<TW_Bypass>True", "<TW_Bypass>False"
        )
        == TIMEWARP
    )


@pytest.mark.parametrize("value", [r"\1", r"\0", r"\g", r"\\", "$1", r"\n", "a\\1b"])
def test_a_value_that_looks_like_a_regex_replacement_goes_in_as_it_is(value: str) -> None:
    out = se.replace_tags(TIMEWARP, {"TW_FrameMode": value})
    assert f"<TW_FrameMode>{value}</TW_FrameMode>" in out
    assert out.count("TW_FrameMode") == 2


def test_replace_refuses_a_tag_that_is_not_unique() -> None:
    with pytest.raises(ValueError, match="Frame"):
        se.replace_tags(TIMEWARP, {"Frame": 3})
    with pytest.raises(ValueError, match="TW_Speed"):
        se.replace_tags(TIMEWARP, {"TW_Speed": 3})


def test_replace_refuses_a_value_that_is_not_allowed_even_when_called_directly() -> None:
    with pytest.raises(ValueError, match="TW_FrameMode"):
        se.replace_tags(TIMEWARP, {"TW_FrameMode": "0</TW_FrameMode><X>1"})


def test_a_bool_is_written_as_the_setup_writes_it() -> None:
    assert se.format_value(True) == "True" and se.format_value(False) == "False"
    out = se.replace_tags(TIMEWARP, {"TW_Bypass": True})
    assert "<TW_Bypass>True</TW_Bypass>" in out


def test_numbers_and_strings_are_formatted_plainly() -> None:
    assert se.format_value(0) == "0" and se.format_value(-3) == "-3"
    assert se.format_value(0.5) == "0.5" and se.format_value(1e-07) == "1e-07"
    assert se.format_value("Auto Mode") == "Auto Mode"
    assert se.format_value(2.0) == "2.0"


@pytest.mark.parametrize(
    "char",
    ["<", ">", "&", '"', "\n", "\r", "\t", "\x00", "\x1b", "\x7f"],
)
def test_a_value_with_a_markup_or_control_character_is_refused(char: str) -> None:
    problem = se.value_problem(f"a{char}b")
    assert problem is not None
    assert se.check_values({"TW_FrameMode": f"a{char}b"})
    assert problem.isascii()


def test_a_value_that_closes_the_tag_and_opens_another_is_refused() -> None:
    assert se.value_problem("0</TW_FrameMode><X>1") is not None


def test_a_non_ascii_value_is_refused() -> None:
    assert se.value_problem("café") is not None


def test_a_single_quote_and_a_space_are_allowed() -> None:
    assert se.value_problem("it's a value") is None


@pytest.mark.parametrize("value", [None, [1], {"a": 1}, (1,), b"x", float("nan"), float("inf")])
def test_a_value_of_another_type_or_not_finite_is_refused(value: Any) -> None:
    assert se.value_problem(value) is not None


@pytest.mark.parametrize(
    "tag",
    ["", "1abc", "a-b", "a b", "a\n", "a/b", "a>b", "<a", "a.b", "a:b", "café", " a"],
)
def test_a_tag_name_of_another_shape_is_refused(tag: str) -> None:
    assert se.tag_name_problem(tag) is not None
    assert se.check_values({tag: 1})


@pytest.mark.parametrize("tag", ["a", "_a", "TW_FrameMode", "A1_b2", "x"])
def test_a_tag_name_of_the_allowed_shape_passes(tag: str) -> None:
    assert se.tag_name_problem(tag) is None
    assert se.check_values({tag: 1}) == []


def test_a_tag_name_that_is_not_a_string_is_refused() -> None:
    assert se.tag_name_problem(3) is not None


def test_the_number_of_values_is_between_one_and_twenty() -> None:
    assert se.check_values({}) and se.check_values("x") and se.check_values(None)
    assert se.check_values({f"T{i}": i for i in range(20)}) == []
    assert se.check_values({f"T{i}": i for i in range(21)})


def test_every_problem_is_listed_not_only_the_first() -> None:
    problems = se.check_values({"a b": 1, "ok": "<", "fine": 2})
    assert len(problems) == 2


def test_a_number_is_compared_as_a_number_and_everything_else_as_text() -> None:
    assert se.values_match(0, "0") and se.values_match(0.5, "0.500000")
    assert se.values_match(2, "2.0") and se.values_match(2.0, "2") and not se.values_match(2, "3")
    assert se.values_match(1e-07, "0.0000001") and se.values_match(-3, "-3.00")
    assert se.values_match(True, "True") and not se.values_match(True, "False")
    assert not se.values_match(0, None) and not se.values_match("0", None)
    assert se.values_match("Auto", "Auto") and not se.values_match("Auto", "auto")


def test_a_string_request_is_never_compared_as_a_number() -> None:
    assert not se.values_match("0", "0.0") and not se.values_match("2", "2.0")
    assert se.values_match("2", "2")


@pytest.mark.parametrize("found", ["1_0", " 1", "1 ", "inf", "-inf", "nan", "0x1", "", "1e", "+"])
def test_a_text_that_is_not_a_plain_number_is_not_taken_for_one(found: str) -> None:
    assert not se.values_match(10, found)
    assert not se.values_match(1, found)


def test_a_bool_is_never_matched_as_a_number() -> None:
    assert not se.values_match(True, "1") and not se.values_match(1, "True")
    assert not se.values_match(False, "0") and not se.values_match(0, "False")


def test_a_value_longer_than_the_limit_is_refused() -> None:
    assert se.value_problem("x" * se.SETUP_MAX_VALUE_LENGTH) is None
    assert se.value_problem("x" * (se.SETUP_MAX_VALUE_LENGTH + 1)) is not None
    assert se.check_values({"A": "x" * (se.SETUP_MAX_VALUE_LENGTH + 1)})


def test_scalar_differences_lists_the_tags_whose_values_changed() -> None:
    a = "<A><X>1</X><Y>2</Y><Z>3</Z></A>"
    b = "<A><X>1</X><Y>9</Y><Z>3</Z><W>5</W></A>"
    diff = se.scalar_differences(a, b, [])
    assert diff == {
        "Y": {"edited": ["2"], "readback": ["9"]},
        "W": {"edited": [], "readback": ["5"]},
    }
    assert se.scalar_differences(a, b, ["Y"]) == {"W": {"edited": [], "readback": ["5"]}}
    assert se.scalar_differences(a, a, []) == {}


def test_scalar_differences_is_cut_at_a_limit() -> None:
    a = "<A>" + "".join(f"<T{i}>1</T{i}>" for i in range(40)) + "</A>"
    b = "<A>" + "".join(f"<T{i}>2</T{i}>" for i in range(40)) + "</A>"
    assert len(se.scalar_differences(a, b, [])) == se.MAX_DIFFERENCES


def test_the_module_is_ascii_and_needs_nothing_but_re() -> None:
    import inspect

    source = inspect.getsource(se)
    assert source.isascii()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert imports == ["import re", "from decimal import Decimal"]


def test_the_copy_that_runs_inside_flame_behaves_the_same() -> None:
    assert SETUP_EDIT_HELPER.isascii()
    namespace: dict[str, Any] = {}
    exec(compile(SETUP_EDIT_HELPER, "<helper>", "exec"), namespace)
    assert namespace["_se_count_tags"](TIMEWARP, ["Frame", "TW_FrameMode"]) == {
        "Frame": 2,
        "TW_FrameMode": 1,
    }
    assert namespace["_se_replace_tags"](TIMEWARP, {"TW_FrameMode": r"\1"}) == se.replace_tags(
        TIMEWARP, {"TW_FrameMode": r"\1"}
    )
    assert namespace["_se_check_values"]({"a b": 1}) == se.check_values({"a b": 1})
    assert namespace["_se_values_match"](0.5, "0.500000") is True


def test_the_copy_leaves_no_public_name_in_the_namespace_every_op_shares() -> None:
    namespace: dict[str, Any] = {}
    exec(compile(SETUP_EDIT_HELPER, "<helper>", "exec"), namespace)
    public = sorted(k for k in namespace if not k.startswith("_"))
    assert public == []
