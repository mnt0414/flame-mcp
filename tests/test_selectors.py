"""Tests `validate_selector` (shape) and `resolve_code` (generated Python).

No Flame required -- only `ast.parse` and a repr() injection check."""

from __future__ import annotations

import ast
from typing import get_args

import pytest

from flame_mcp import selectors
from flame_mcp.types import SelectorKind

ALL_KINDS: tuple[SelectorKind, ...] = get_args(SelectorKind)


def test_selection_selector_valid() -> None:
    assert selectors.validate_selector({"kind": "selection"}, ALL_KINDS) == []


def test_wiretap_selector_valid() -> None:
    assert selectors.validate_selector({"kind": "wiretap", "id": "abc"}, ALL_KINDS) == []


def test_path_selector_valid() -> None:
    assert selectors.validate_selector({"kind": "path", "path": ["Lib", "R1"]}, ALL_KINDS) == []


def test_segment_selector_valid() -> None:
    sel = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "abc"},
        "version": 0,
        "track": 0,
        "index": 2,
    }
    assert selectors.validate_selector(sel, ALL_KINDS) == []


def test_not_a_mapping_rejected() -> None:
    issues = selectors.validate_selector("not-a-dict", ALL_KINDS)
    assert issues and "object" in issues[0]


def test_unknown_kind_rejected() -> None:
    issues = selectors.validate_selector({"kind": "bogus"}, ALL_KINDS)
    assert issues and "kind" in issues[0]


def test_kind_not_accepted_rejected() -> None:
    issues = selectors.validate_selector({"kind": "selection"}, ("wiretap",))
    assert any("not accepted" in i for i in issues)


def test_selection_extra_key_rejected() -> None:
    issues = selectors.validate_selector({"kind": "selection", "id": "x"}, ALL_KINDS)
    assert any("unexpected key" in i for i in issues)


def test_wiretap_missing_id_rejected() -> None:
    issues = selectors.validate_selector({"kind": "wiretap"}, ALL_KINDS)
    assert any("non-empty string" in i for i in issues)


def test_wiretap_empty_id_rejected() -> None:
    issues = selectors.validate_selector({"kind": "wiretap", "id": ""}, ALL_KINDS)
    assert any("non-empty string" in i for i in issues)


def test_wiretap_non_string_id_rejected() -> None:
    issues = selectors.validate_selector({"kind": "wiretap", "id": 5}, ALL_KINDS)
    assert any("non-empty string" in i for i in issues)


def test_path_empty_list_rejected() -> None:
    issues = selectors.validate_selector({"kind": "path", "path": []}, ALL_KINDS)
    assert any("non-empty list" in i for i in issues)


def test_path_non_list_rejected() -> None:
    issues = selectors.validate_selector({"kind": "path", "path": "Lib"}, ALL_KINDS)
    assert any("non-empty list" in i for i in issues)


def test_path_with_empty_segment_rejected() -> None:
    issues = selectors.validate_selector({"kind": "path", "path": ["Lib", ""]}, ALL_KINDS)
    assert any("non-empty list" in i for i in issues)


def test_segment_parent_not_object_rejected() -> None:
    sel = {"kind": "segment", "parent": "not-an-object", "version": 0, "track": 0, "index": 0}
    issues = selectors.validate_selector(sel, ALL_KINDS)
    assert any("parent" in i for i in issues)


def test_segment_parent_is_segment_rejected() -> None:
    inner_segment_selector = {
        "kind": "segment",
        "parent": {"kind": "selection"},
        "version": 0,
        "track": 0,
        "index": 0,
    }
    sel = {
        "kind": "segment",
        "parent": inner_segment_selector,
        "version": 0,
        "track": 0,
        "index": 0,
    }
    issues = selectors.validate_selector(sel, ALL_KINDS)
    assert any("must not itself be" in i for i in issues)


@pytest.mark.parametrize("field", ["version", "track", "index"])
def test_segment_negative_index_rejected(field: str) -> None:
    sel = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "x"},
        "version": 0,
        "track": 0,
        "index": 0,
    }
    sel[field] = -1
    issues = selectors.validate_selector(sel, ALL_KINDS)
    assert any(field in i for i in issues)


def test_segment_bool_index_rejected() -> None:
    # bool is an int subclass in Python; it must not pass as an index.
    sel = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "x"},
        "version": True,
        "track": 0,
        "index": 0,
    }
    issues = selectors.validate_selector(sel, ALL_KINDS)
    assert any("version" in i for i in issues)


def test_segment_extra_key_rejected() -> None:
    sel = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "x"},
        "version": 0,
        "track": 0,
        "index": 0,
        "bogus": 1,
    }
    issues = selectors.validate_selector(sel, ALL_KINDS)
    assert any("unexpected key" in i for i in issues)


def test_segment_nested_parent_issues_are_prefixed() -> None:
    sel = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": ""},
        "version": 0,
        "track": 0,
        "index": 0,
    }
    issues = selectors.validate_selector(sel, ALL_KINDS)
    assert any(i.startswith("parent.") for i in issues)


_BATCH_SEL: dict[str, object] = {"kind": "batch", "name": "B"}
_NODE_SEL: dict[str, object] = {"kind": "node", "batch": _BATCH_SEL, "name": "blur1"}

SAMPLE_SELECTORS: dict[SelectorKind, dict[str, object]] = {
    "selection": {"kind": "selection"},
    "wiretap": {"kind": "wiretap", "id": "abc"},
    "path": {"kind": "path", "path": ["Lib", "R1"]},
    "segment": {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "abc"},
        "version": 0,
        "track": 0,
        "index": 0,
    },
    "batch": _BATCH_SEL,
    "node": _NODE_SEL,
    "action_node": {"kind": "action_node", "node": _NODE_SEL, "name": "axis1"},
    "desktop": {"kind": "desktop"},
    "timeline": {"kind": "timeline"},
}


def test_sample_selectors_cover_every_kind() -> None:
    # Adding a kind without an example here makes this test fail.
    assert set(SAMPLE_SELECTORS) == set(get_args(SelectorKind))


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_resolve_code_is_valid_python_for_every_kind(kind: SelectorKind) -> None:
    assert selectors.validate_selector(SAMPLE_SELECTORS[kind], ALL_KINDS) == []
    code = selectors.resolve_code(SAMPLE_SELECTORS[kind], "target")
    ast.parse(code)  # raises SyntaxError if this is ever not valid Python


def test_resolve_code_unknown_kind_raises_value_error() -> None:
    with pytest.raises(ValueError):
        selectors.resolve_code({"kind": "bogus"}, "target")


def test_resolve_code_segment_embeds_parent_resolution() -> None:
    sel = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "abc"},
        "version": 1,
        "track": 2,
        "index": 3,
    }
    code = selectors.resolve_code(sel, "target")
    assert "target__parent" in code
    # find_by_wiretap_node_id itself lives in execution.py's prelude
    # (SELECTOR_HELPER) -- the op's own generated code only ever calls the
    # prelude function by name (no selector kind's generated code contains
    # Flame API calls or loops).
    assert "_resolve_wiretap('abc')" in code
    assert "_resolve_segment(target__parent, 1, 2, 3)" in code
    ast.parse(code)


def test_resolve_code_never_contains_a_loop_for_any_kind() -> None:
    """The op's own generated code never carries a loop for any selector kind.

    Selector resolution lives in execution.py's prelude as real functions, so the op's own
    to_python output can never itself carry a loop/comprehension for any kind -- independent of
    registry.py separately smoke-testing every `accepts` kind."""
    sels: list[dict[str, object]] = [
        *SAMPLE_SELECTORS.values(),
        {"kind": "path", "path": ["A", "B", "C"], "root": "desktop"},
        {
            "kind": "segment",
            "parent": {"kind": "path", "path": ["A"]},
            "version": 0,
            "track": 0,
            "index": 0,
        },
    ]
    loop_node_types = (
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
    )
    for sel in sels:
        code = selectors.resolve_code(sel, "target")
        tree = ast.parse(code)
        assert not any(isinstance(node, loop_node_types) for node in ast.walk(tree)), sel


_INJECTION_PAYLOAD = "'); import os; os.system('echo pwned') #"


def test_wiretap_id_injection_stays_a_string_literal() -> None:
    code = selectors.resolve_code({"kind": "wiretap", "id": _INJECTION_PAYLOAD}, "target")
    ast.parse(code)  # must still be syntactically valid, not a broken injection
    assert "os.system" not in _executable_names(code)


def test_path_segment_injection_stays_a_string_literal() -> None:
    code = selectors.resolve_code({"kind": "path", "path": [_INJECTION_PAYLOAD]}, "target")
    ast.parse(code)
    assert "os.system" not in _executable_names(code)


def _executable_names(code: str) -> set[str]:
    """Names that appear as actual identifiers/calls in the parsed AST --
    as opposed to inside a string literal, which is where an injection
    payload must stay stuck if `repr()` embedding is doing its job."""
    tree = ast.parse(code)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_injection_payload_only_appears_inside_a_string_constant() -> None:
    code = selectors.resolve_code({"kind": "wiretap", "id": _INJECTION_PAYLOAD}, "target")
    tree = ast.parse(code)
    found_in_constant = any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _INJECTION_PAYLOAD in node.value
        for node in ast.walk(tree)
    )
    assert found_in_constant
    assert "pwned" not in _executable_names(code)


@pytest.mark.parametrize("kind", get_args(SelectorKind))
def test_describe_kind_example_is_valid_and_resolves(kind: SelectorKind) -> None:
    entry = selectors.describe_kind(kind)
    assert entry["kind"] == kind
    assert selectors.validate_selector(entry["example"], ALL_KINDS) == []
    ast.parse(selectors.resolve_code(entry["example"], "target"))


@pytest.mark.parametrize("kind", get_args(SelectorKind))
def test_describe_kind_fields_are_exactly_the_keys_validation_accepts(kind: SelectorKind) -> None:
    # The example carries every *required* published field; validation rejects any
    # key that is not published, requires each required field, and knows each
    # optional field (a nonsense value for it is rejected) -- so the published
    # fields and validation cannot drift apart.
    entry = selectors.describe_kind(kind)
    fields = entry["fields"]
    required = {name for name, f in fields.items() if f["required"]}
    optional = set(fields) - required
    assert {"kind", *required} <= set(entry["example"]) <= {"kind", *fields}
    bogus = {**entry["example"], "not_a_field": 1}
    assert any("unexpected key" in i for i in selectors.validate_selector(bogus, ALL_KINDS))
    for name in required:
        missing = {k: v for k, v in entry["example"].items() if k != name}
        assert selectors.validate_selector(missing, ALL_KINDS), name
    for name in optional:
        nonsense = {**entry["example"], name: {"nonsense": 1}}
        assert selectors.validate_selector(nonsense, ALL_KINDS), name


def test_describe_kind_segment_fields() -> None:
    fields = selectors.describe_kind("segment")["fields"]
    assert {name: f["type"] for name, f in fields.items()} == {
        "parent": "selector",
        "version": "non-negative integer",
        "track": "non-negative integer",
        "index": "non-negative integer",
        "record_in": "non-negative integer",
        "expect_source_name": "string",
    }
    assert all(f["description"] for f in fields.values())
    assert {name: f["required"] for name, f in fields.items()} == {
        "parent": True,
        "version": True,
        "track": True,
        "index": False,
        "record_in": False,
        "expect_source_name": False,
    }


def test_describe_kind_segment_tells_when_to_use_record_in() -> None:
    fields = selectors.describe_kind("segment")["fields"]
    text = " ".join(f["description"] for f in fields.values())
    assert "exactly one of index / record_in" in fields["index"]["description"]
    assert "exactly one of index / record_in" in fields["record_in"]["description"]
    assert "ripple" in text
    assert "CONFLICT" in fields["expect_source_name"]["description"]
    example = selectors.describe_kind("segment")["example"]
    assert ("index" in example) != ("record_in" in example)


_SEG_PARENT: dict[str, object] = {"kind": "wiretap", "id": "abc"}


def _seg(**extra: object) -> dict[str, object]:
    return {"kind": "segment", "parent": _SEG_PARENT, "version": 0, "track": 1, **extra}


def test_segment_selector_by_record_in_is_valid() -> None:
    assert selectors.validate_selector(_seg(record_in=86400), ALL_KINDS) == []
    assert selectors.validate_selector(_seg(record_in=0), ALL_KINDS) == []


def test_segment_selector_with_expect_source_name_is_valid() -> None:
    assert selectors.validate_selector(_seg(index=2, expect_source_name="clip1"), ALL_KINDS) == []
    assert selectors.validate_selector(_seg(record_in=5, expect_source_name="c"), ALL_KINDS) == []


def test_segment_selector_needs_exactly_one_of_index_and_record_in() -> None:
    both = selectors.validate_selector(_seg(index=0, record_in=0), ALL_KINDS)
    assert any("exactly one of index / record_in" in i for i in both)
    neither = selectors.validate_selector(_seg(), ALL_KINDS)
    assert any("exactly one of index / record_in" in i for i in neither)


@pytest.mark.parametrize("bad", [-1, 1.5, True, "5", None])
def test_segment_record_in_must_be_a_non_negative_integer(bad: object) -> None:
    issues = selectors.validate_selector(_seg(record_in=bad), ALL_KINDS)
    assert any("record_in" in i and "non-negative integer" in i for i in issues)


@pytest.mark.parametrize("bad", ["", 5, None, ["clip1"]])
def test_segment_expect_source_name_must_be_a_non_empty_string(bad: object) -> None:
    issues = selectors.validate_selector(_seg(index=0, expect_source_name=bad), ALL_KINDS)
    assert any("expect_source_name" in i and "non-empty string" in i for i in issues)


def test_segment_selector_still_rejects_unknown_keys_with_record_in() -> None:
    issues = selectors.validate_selector(_seg(record_in=0, bogus=1), ALL_KINDS)
    assert any("unexpected key" in i for i in issues)


def test_resolve_code_segment_by_record_in() -> None:
    code = selectors.resolve_code(_seg(record_in=86400), "target")
    assert code.splitlines() == [
        "target__parent = _resolve_wiretap('abc')",
        "target = _resolve_segment_at(target__parent, 0, 1, 86400)",
    ]


def test_resolve_code_segment_passes_expect_source_name_on() -> None:
    by_record_in = selectors.resolve_code(_seg(record_in=7, expect_source_name="clip1"), "t")
    assert by_record_in.splitlines()[-1] == "t = _resolve_segment_at(t__parent, 0, 1, 7, 'clip1')"
    by_index = selectors.resolve_code(_seg(index=3, expect_source_name="clip1"), "t")
    assert by_index.splitlines()[-1] == "t = _resolve_segment(t__parent, 0, 1, 3, 'clip1')"


def test_resolve_code_segment_index_form_is_unchanged_without_expect() -> None:
    code = selectors.resolve_code(_seg(index=3), "t")
    assert code.splitlines()[-1] == "t = _resolve_segment(t__parent, 0, 1, 3)"


def test_expect_source_name_injection_stays_a_string_literal() -> None:
    code = selectors.resolve_code(_seg(record_in=0, expect_source_name=_INJECTION_PAYLOAD), "t")
    ast.parse(code)
    assert "pwned" not in _executable_names(code)


def test_resolve_code_for_the_new_segment_forms_has_no_loop() -> None:
    for sel in (_seg(record_in=1), _seg(record_in=1, expect_source_name="x"), _seg(index=1)):
        tree = ast.parse(selectors.resolve_code(sel, "target"))
        assert not any(
            isinstance(n, (ast.For, ast.While, ast.ListComp, ast.GeneratorExp))
            for n in ast.walk(tree)
        )


def test_every_segment_resolver_called_exists_in_the_prelude() -> None:
    from flame_mcp import execution

    prelude = ast.parse(execution.SELECTOR_HELPER)
    defined = {n.name for n in ast.walk(prelude) if isinstance(n, ast.FunctionDef)}
    for sel in (_seg(record_in=1, expect_source_name="x"), _seg(index=1, expect_source_name="x")):
        code = ast.parse(selectors.resolve_code(sel, "target"))
        called = {
            n.func.id
            for n in ast.walk(code)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert called <= defined, called - defined


def test_describe_kind_returns_a_copy_of_the_example() -> None:
    entry = selectors.describe_kind("segment")
    entry["example"]["parent"]["id"] = "mutated"
    assert selectors.describe_kind("segment")["example"]["parent"]["id"] != "mutated"


def test_batch_selector_needs_exactly_one_of_uid_and_name() -> None:
    assert selectors.validate_selector({"kind": "batch", "uid": "u1"}, ALL_KINDS) == []
    assert selectors.validate_selector({"kind": "batch", "name": "B"}, ALL_KINDS) == []
    both = selectors.validate_selector({"kind": "batch", "uid": "u", "name": "B"}, ALL_KINDS)
    assert any("exactly one" in i for i in both)
    neither = selectors.validate_selector({"kind": "batch"}, ALL_KINDS)
    assert any("exactly one" in i for i in neither)


@pytest.mark.parametrize("field", ["uid", "name"])
@pytest.mark.parametrize("bad", ["", 5, None])
def test_batch_selector_rejects_empty_or_non_string(field: str, bad: object) -> None:
    issues = selectors.validate_selector({"kind": "batch", field: bad}, ALL_KINDS)
    assert any("non-empty string" in i for i in issues)


def test_node_selector_validates_nested_batch_and_name() -> None:
    assert selectors.validate_selector(_NODE_SEL, ALL_KINDS) == []
    bad_nested = {"kind": "node", "batch": {"kind": "batch"}, "name": "n"}
    assert any(i.startswith("batch.") for i in selectors.validate_selector(bad_nested, ALL_KINDS))
    wrong_kind = {"kind": "node", "batch": {"kind": "wiretap", "id": "x"}, "name": "n"}
    assert any("not accepted" in i for i in selectors.validate_selector(wrong_kind, ALL_KINDS))
    no_name = {"kind": "node", "batch": _BATCH_SEL, "name": ""}
    assert any("name" in i for i in selectors.validate_selector(no_name, ALL_KINDS))
    not_object = {"kind": "node", "batch": "B", "name": "n"}
    assert any(
        "batch must be an object" in i for i in selectors.validate_selector(not_object, ALL_KINDS)
    )


def test_action_node_selector_requires_a_node_selector() -> None:
    assert selectors.validate_selector(SAMPLE_SELECTORS["action_node"], ALL_KINDS) == []
    wrong = {"kind": "action_node", "node": _BATCH_SEL, "name": "axis1"}
    issues = selectors.validate_selector(wrong, ALL_KINDS)
    assert any(i.startswith("node.") and "not accepted" in i for i in issues)


def test_path_root_is_optional_and_restricted() -> None:
    ok = {"kind": "path", "path": ["R"], "root": "desktop"}
    assert selectors.validate_selector(ok, ALL_KINDS) == []
    assert selectors.validate_selector({**ok, "root": "libraries"}, ALL_KINDS) == []
    bad = selectors.validate_selector({**ok, "root": "elsewhere"}, ALL_KINDS)
    assert any("root" in i for i in bad)


def test_path_default_root_generates_the_old_code_unchanged() -> None:
    assert (
        selectors.resolve_code({"kind": "path", "path": ["A"]}, "t") == "t = _resolve_path(['A'])"
    )
    assert (
        selectors.resolve_code({"kind": "path", "path": ["A"], "root": "libraries"}, "t")
        == "t = _resolve_path(['A'])"
    )
    assert (
        selectors.resolve_code({"kind": "path", "path": ["A"], "root": "desktop"}, "t")
        == "t = _resolve_path(['A'], 'desktop')"
    )


def test_segment_parent_only_accepts_selection_wiretap_path() -> None:
    sel = {"kind": "segment", "parent": _BATCH_SEL, "version": 0, "track": 0, "index": 0}
    issues = selectors.validate_selector(sel, ALL_KINDS)
    assert any(i.startswith("parent.") and "not accepted" in i for i in issues)


def test_nested_resolve_code_names_and_order() -> None:
    code = selectors.resolve_code(SAMPLE_SELECTORS["action_node"], "target")
    lines = code.splitlines()
    assert lines == [
        "target__node__batch = _resolve_batch(None, 'B')",
        "target__node = _resolve_node(target__node__batch, 'blur1')",
        "target = _resolve_action_node(target__node, 'axis1')",
    ]


def test_batch_and_node_string_fields_stay_string_literals() -> None:
    for sel in (
        {"kind": "batch", "name": _INJECTION_PAYLOAD},
        {"kind": "batch", "uid": _INJECTION_PAYLOAD},
        {"kind": "node", "batch": _BATCH_SEL, "name": _INJECTION_PAYLOAD},
        {
            "kind": "action_node",
            "node": _NODE_SEL,
            "name": _INJECTION_PAYLOAD,
        },
        {"kind": "path", "path": [_INJECTION_PAYLOAD], "root": "desktop"},
    ):
        code = selectors.resolve_code(sel, "target")
        ast.parse(code)
        assert "pwned" not in _executable_names(code)


def test_describe_kind_marks_optional_fields() -> None:
    path_fields = selectors.describe_kind("path")["fields"]
    assert path_fields["path"]["required"] is True
    assert path_fields["root"]["required"] is False
    batch_fields = selectors.describe_kind("batch")["fields"]
    assert {name: f["required"] for name, f in batch_fields.items()} == {
        "uid": False,
        "name": False,
    }


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_called_resolver_exists_in_the_prelude(kind: SelectorKind) -> None:
    # Every `_resolve_*` that resolve_code calls must exist in the prelude used at run time.
    from flame_mcp import execution

    prelude = ast.parse(execution.SELECTOR_HELPER)
    defined = {n.name for n in ast.walk(prelude) if isinstance(n, ast.FunctionDef)}
    code = ast.parse(selectors.resolve_code(SAMPLE_SELECTORS[kind], "target"))
    called = {
        n.func.id
        for n in ast.walk(code)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert called and called <= defined, called - defined


def test_desktop_selector_has_no_fields_and_rejects_any_key() -> None:
    assert selectors.validate_selector({"kind": "desktop"}, ALL_KINDS) == []
    assert selectors.describe_kind("desktop")["fields"] == {}
    issues = selectors.validate_selector({"kind": "desktop", "name": "x"}, ALL_KINDS)
    assert any("unexpected key" in i for i in issues)


def test_desktop_selector_resolves_through_one_prelude_call() -> None:
    assert selectors.resolve_code({"kind": "desktop"}, "dest") == "dest = _resolve_desktop()"


def test_timeline_selector_has_no_fields_and_rejects_any_key() -> None:
    assert selectors.validate_selector({"kind": "timeline"}, ALL_KINDS) == []
    assert selectors.describe_kind("timeline")["fields"] == {}
    issues = selectors.validate_selector({"kind": "timeline", "name": "x"}, ALL_KINDS)
    assert any("unexpected key" in i for i in issues)


def test_timeline_selector_resolves_through_one_prelude_call() -> None:
    assert selectors.resolve_code({"kind": "timeline"}, "seq") == "seq = _resolve_timeline()"


def test_timeline_selector_is_valid_as_a_segment_parent() -> None:
    sel = {
        "kind": "segment",
        "parent": {"kind": "timeline"},
        "version": 0,
        "track": 0,
        "record_in": 10,
    }
    assert selectors.validate_selector(sel, ALL_KINDS) == []
    code = selectors.resolve_code(sel, "target")
    assert "target__parent = _resolve_timeline()" in code
    ast.parse(code)
