from __future__ import annotations

import pytest

from flame_mcp.policy import (
    Policy,
    PolicyConfigError,
    deny_category,
    deny_eval_effect,
    deny_operation,
    deny_tool,
    summary,
    visible_ops,
)
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationSpec

SYMBOLS = frozenset({"flame.projects"})


def make_registry_with_ops() -> Registry:
    registry = Registry(symbols=SYMBOLS)
    registry.register_category(
        Category(
            name="widget",
            description="d",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main", "main-async"}),
            allowed_pacing=frozenset({"none", "structural", "media"}),
        )
    )
    registry.register_category(
        Category(
            name="eval",
            description="d",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    registry.register(
        OperationSpec(
            name="widget.read_thing",
            category="widget",
            description="d",
            effect="read",
            to_python=lambda a: "x = 1",
        )
    )
    registry.register(
        OperationSpec(
            name="widget.rename_thing",
            category="widget",
            description="d",
            effect="reversible",
            to_python=lambda a: "x = 1",
            undo_steps=lambda a, n: 1,
        )
    )
    registry.register(
        OperationSpec(
            name="eval.read_snippet",
            category="eval",
            description="d",
            effect="read",
            to_python=lambda a: "x = 1",
        )
    )
    registry.register(
        OperationSpec(
            name="eval.write_snippet",
            category="eval",
            description="d",
            effect="reversible",
            to_python=lambda a: "x = 1",
            undo_steps=lambda a, n: 1,
        )
    )
    return registry


def test_from_env_defaults_permissive() -> None:
    policy = Policy.from_env({})
    assert policy.readonly is False
    assert policy.allowed_categories is None
    assert policy.allowed_effects is None
    assert policy.eval_enabled is False


def test_from_env_readonly_flag() -> None:
    assert Policy.from_env({"FLAME_MCP_READONLY": "1"}).readonly is True
    assert Policy.from_env({"FLAME_MCP_READONLY": "0"}).readonly is False
    assert Policy.from_env({"FLAME_MCP_READONLY": ""}).readonly is False
    assert Policy.from_env({}).readonly is False


def test_from_env_readonly_rejects_non_canonical_values() -> None:
    with pytest.raises(PolicyConfigError):
        Policy.from_env({"FLAME_MCP_READONLY": "true"})
    with pytest.raises(PolicyConfigError):
        Policy.from_env({"FLAME_MCP_READONLY": "yes"})


def test_from_env_enable_eval_rejects_non_canonical_values() -> None:
    with pytest.raises(PolicyConfigError):
        Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "true"})


def test_from_env_allow_categories_parsed_and_trimmed() -> None:
    policy = Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": " widget, eval ,project"})
    assert policy.allowed_categories == frozenset({"widget", "eval", "project"})


def test_from_env_allow_categories_empty_is_none() -> None:
    assert Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": "  "}).allowed_categories is None
    assert Policy.from_env({}).allowed_categories is None


def test_from_env_allow_effects_valid() -> None:
    policy = Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,reversible"})
    assert policy.allowed_effects == frozenset({"read", "reversible"})


def test_from_env_allow_effects_invalid_raises() -> None:
    with pytest.raises(PolicyConfigError):
        Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,bogus"})


@pytest.mark.parametrize("name", ["FLAME_MCP_ALLOW_EFFECTS", "FLAME_MCP_ALLOW_CATEGORIES"])
@pytest.mark.parametrize("raw", [",", " , ,", ",,,", "\t,\n"])
def test_from_env_allow_list_of_only_separators_fails_closed(name: str, raw: str) -> None:
    with pytest.raises(PolicyConfigError, match=name):
        Policy.from_env({name: raw})


@pytest.mark.parametrize("name", ["FLAME_MCP_ALLOW_EFFECTS", "FLAME_MCP_ALLOW_CATEGORIES"])
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_from_env_allow_list_unset_or_blank_is_unrestricted(name: str, raw: str | None) -> None:
    policy = Policy.from_env({} if raw is None else {name: raw})
    assert policy.allowed_effects is None and policy.allowed_categories is None


def test_from_env_enable_eval() -> None:
    assert Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"}).eval_enabled is True
    assert Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "0"}).eval_enabled is False
    assert Policy.from_env({"FLAME_MCP_ENABLE_EVAL": ""}).eval_enabled is False
    assert Policy.from_env({}).eval_enabled is False


def test_from_env_readonly_overrides_enable_eval() -> None:
    policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1", "FLAME_MCP_READONLY": "1"})
    assert policy.eval_enabled is False


def test_eval_read_denied_by_default_and_the_denial_names_the_switch() -> None:
    registry = make_registry_with_ops()
    op = registry.get("eval.read_snippet")
    assert op is not None
    denial = deny_operation(op, Policy.from_env({}))
    assert denial is not None
    assert "disabled" in denial.message and "FLAME_MCP_ENABLE_EVAL=1" in (denial.hint or "")


def test_other_ops_are_unaffected_by_the_eval_switch() -> None:
    registry = make_registry_with_ops()
    op = registry.get("widget.read_thing")
    assert op is not None
    assert deny_operation(op, Policy.from_env({})) is None


def test_eval_write_denied_by_default() -> None:
    registry = make_registry_with_ops()
    policy = Policy.from_env({})
    op = registry.get("eval.write_snippet")
    assert op is not None
    assert deny_operation(op, policy) is not None


def test_eval_read_and_write_allowed_when_enabled() -> None:
    registry = make_registry_with_ops()
    policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
    for name in ("eval.read_snippet", "eval.write_snippet"):
        op = registry.get(name)
        assert op is not None
        assert deny_operation(op, policy) is None


# Under READONLY no eval is allowed, whatever effect the call declares.


def test_readonly_denies_every_eval_op_even_a_read_one() -> None:
    registry = make_registry_with_ops()
    policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    for name in ("eval.read_snippet", "eval.write_snippet"):
        op = registry.get(name)
        assert op is not None
        denial = deny_operation(op, policy)
        assert denial is not None and "FLAME_MCP_READONLY" in denial.message, name
        assert op not in visible_ops(registry, policy), name


@pytest.mark.parametrize("effect", ["read", "reversible", "create", "destructive"])
@pytest.mark.parametrize(
    "env", [{"FLAME_MCP_READONLY": "1"}, {"FLAME_MCP_READONLY": "1", "FLAME_MCP_ENABLE_EVAL": "1"}]
)
def test_deny_eval_effect_refuses_every_declared_effect_under_readonly(
    effect: str, env: dict[str, str]
) -> None:
    denial = deny_eval_effect(effect, Policy.from_env(env))
    assert denial is not None
    assert "eval.run" in denial.message and "FLAME_MCP_READONLY" in denial.message
    assert denial.hint is not None and "READONLY" in denial.hint


def test_deny_eval_effect_needs_the_switch_for_every_effect() -> None:
    off = Policy.from_env({})
    on = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
    for effect in ("read", "destructive"):
        denial = deny_eval_effect(effect, off)
        assert denial is not None and "FLAME_MCP_ENABLE_EVAL=1" in (denial.hint or "")
        assert deny_eval_effect(effect, on) is None


def test_deny_eval_effect_respects_allow_effects() -> None:
    limited = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1", "FLAME_MCP_ALLOW_EFFECTS": "read"})
    assert deny_eval_effect("read", limited) is None
    denial = deny_eval_effect("destructive", limited)
    assert denial is not None and "FLAME_MCP_ALLOW_EFFECTS" in denial.message


def test_readonly_denies_writes_allows_reads() -> None:
    registry = make_registry_with_ops()
    policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    read_op = registry.get("widget.read_thing")
    write_op = registry.get("widget.rename_thing")
    assert read_op is not None and write_op is not None
    assert deny_operation(read_op, policy) is None
    assert deny_operation(write_op, policy) is not None


def test_allow_categories_denies_other_categories() -> None:
    registry = make_registry_with_ops()
    policy = Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": "widget"})
    widget_op = registry.get("widget.read_thing")
    eval_op = registry.get("eval.read_snippet")
    assert widget_op is not None and eval_op is not None
    assert deny_operation(widget_op, policy) is None
    assert deny_operation(eval_op, policy) is not None


def test_allow_effects_denies_other_effects() -> None:
    registry = make_registry_with_ops()
    policy = Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read"})
    read_op = registry.get("widget.read_thing")
    write_op = registry.get("widget.rename_thing")
    assert read_op is not None and write_op is not None
    assert deny_operation(read_op, policy) is None
    assert deny_operation(write_op, policy) is not None


def test_deny_tool_readonly_blocks_flagged_tool() -> None:
    policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    assert deny_tool("flame_do", True, policy) is not None
    assert deny_tool("flame_catalog", False, policy) is None


def test_deny_tool_not_readonly_allows_everything() -> None:
    policy = Policy.from_env({})
    assert deny_tool("flame_do", True, policy) is None


# The eval category is withheld per op by deny_operation (unless FLAME_MCP_ENABLE_EVAL=1), so
# deny_category itself only reflects the ALLOW_CATEGORIES allowlist.


def test_deny_category_denies_categories_outside_allowlist() -> None:
    policy = Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": "widget"})
    assert deny_category("widget", policy) is None
    assert deny_category("eval", policy) is not None


def test_deny_category_none_when_no_allowlist() -> None:
    policy = Policy.from_env({})
    assert deny_category("widget", policy) is None
    assert deny_category("eval", policy) is None


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"FLAME_MCP_READONLY": "1"},
        {"FLAME_MCP_ALLOW_CATEGORIES": "widget"},
        {"FLAME_MCP_ALLOW_EFFECTS": "read"},
        {"FLAME_MCP_ENABLE_EVAL": "1"},
        {"FLAME_MCP_READONLY": "1", "FLAME_MCP_ALLOW_CATEGORIES": "eval"},
    ],
)
def test_catalog_and_do_agree(env: dict[str, str]) -> None:
    registry = make_registry_with_ops()
    policy = Policy.from_env(env)
    # RegisteredOperation carries a `sources` dict, so it is deliberately not
    # hashable; compare by list membership (uses __eq__) instead of a set.
    visible = visible_ops(registry, policy)
    for op in registry.list_ops():
        assert (op in visible) == (deny_operation(op, policy) is None)


def test_summary_reflects_readonly() -> None:
    assert "readonly=ON" in summary(Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    assert "readonly=off" in summary(Policy.from_env({}))


def test_summary_says_eval_is_off_under_readonly() -> None:
    readonly = summary(Policy.from_env({"FLAME_MCP_READONLY": "1", "FLAME_MCP_ENABLE_EVAL": "1"}))
    assert "eval=disabled (readonly)" in readonly and "eval_write" not in readonly
    assert "eval=disabled" in summary(Policy.from_env({}))
    assert "eval=ENABLED" in summary(Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"}))
