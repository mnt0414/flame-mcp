from __future__ import annotations

from types import MappingProxyType
from typing import Any, cast

import pytest

from flame_mcp.registry import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    RegisteredOperation,
    RegistrationError,
    Registry,
)
from flame_mcp.types import (
    Category,
    Depth,
    Effect,
    JournalSpec,
    OperationParam,
    OperationSpec,
    Pacing,
    SelectorKind,
    ServerResult,
)

SYMBOLS = frozenset({"flame.projects", "PyClip.name", "PyClip.get_wiretap_node_id"})


def make_registry() -> Registry:
    return Registry(symbols=SYMBOLS)


def widget_category(**overrides: Any) -> Category:
    defaults: dict[str, Any] = dict(
        name="widget",
        description="a test category",
        default_thread="main",
        default_pacing="none",
        allowed_thread=frozenset({"main", "main-async"}),
        allowed_pacing=frozenset({"none", "structural", "media"}),
        preconditions=(),
    )
    defaults.update(overrides)
    return Category(**defaults)


def server_category(**overrides: Any) -> Category:
    defaults: dict[str, Any] = dict(
        name="plan",
        description="server-only category",
        default_thread="server",
        default_pacing="none",
        allowed_thread=frozenset({"server"}),
        allowed_pacing=frozenset({"none"}),
        preconditions=(),
    )
    defaults.update(overrides)
    return Category(**defaults)


def read_op(**overrides: Any) -> OperationSpec:
    defaults: dict[str, Any] = dict(
        name="widget.read_thing",
        category="widget",
        description="reads a thing",
        effect="read",
        to_python=lambda args: "result = 1",
    )
    defaults.update(overrides)
    return OperationSpec(**defaults)


def write_op(**overrides: Any) -> OperationSpec:
    defaults: dict[str, Any] = dict(
        name="widget.rename_thing",
        category="widget",
        description="renames a thing",
        effect="reversible",
        to_python=lambda args: "result = 1",
        undo_steps=lambda args, target_count: 1,
    )
    defaults.update(overrides)
    return OperationSpec(**defaults)


def test_register_category_positive() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    assert registry.list_categories() == ("widget",)


def test_register_category_duplicate_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register_category(widget_category())


def test_register_category_empty_allowed_thread_rejected() -> None:
    registry = make_registry()
    with pytest.raises(RegistrationError):
        registry.register_category(widget_category(allowed_thread=frozenset()))


def test_register_category_empty_allowed_pacing_rejected() -> None:
    registry = make_registry()
    with pytest.raises(RegistrationError):
        registry.register_category(widget_category(allowed_pacing=frozenset()))


def test_register_category_mixed_server_and_main_rejected() -> None:
    registry = make_registry()
    with pytest.raises(RegistrationError):
        registry.register_category(
            widget_category(
                allowed_thread=frozenset({"main", "server"}),
                default_thread="main",
            )
        )


def test_register_category_server_only_allowed() -> None:
    registry = make_registry()
    registry.register_category(server_category())
    assert registry.list_categories() == ("plan",)


def test_register_category_default_thread_not_in_allowed_rejected() -> None:
    registry = make_registry()
    with pytest.raises(RegistrationError):
        registry.register_category(
            widget_category(allowed_thread=frozenset({"main"}), default_thread="main-async")
        )


def test_register_category_default_pacing_not_in_allowed_rejected() -> None:
    registry = make_registry()
    with pytest.raises(RegistrationError):
        registry.register_category(
            widget_category(allowed_pacing=frozenset({"none"}), default_pacing="structural")
        )


def test_register_op_positive() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op())
    assert isinstance(op, RegisteredOperation)
    assert registry.get("widget.read_thing") is op


def test_registered_op_sources_and_sample_args_are_read_only() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op())
    assert isinstance(op.sources, MappingProxyType)
    assert isinstance(op.sample_args, MappingProxyType)
    with pytest.raises(TypeError):
        op.sources["thread"] = "op"  # type: ignore[index]
    with pytest.raises(TypeError):
        op.sample_args["x"] = 1  # type: ignore[index]


def test_registered_op_sample_args_copied_not_aliased() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    caller_dict = {"name": "clip1"}
    op = registry.register(
        read_op(
            params=(OperationParam(name="name", type="string", description="d"),),
            sample_args=caller_dict,
        )
    )
    caller_dict["name"] = "mutated"
    assert op.sample_args["name"] == "clip1"


def test_register_op_duplicate_name_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    registry.register(read_op())
    with pytest.raises(RegistrationError):
        registry.register(read_op())


def test_register_op_wrong_prefix_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(name="gadget.read_thing"))


def test_register_op_unregistered_category_rejected() -> None:
    registry = make_registry()
    with pytest.raises(RegistrationError):
        registry.register(read_op())


def test_register_op_duplicate_param_name_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(
            read_op(
                params=(
                    OperationParam(name="x", type="string", description="a"),
                    OperationParam(name="x", type="number", description="b"),
                )
            )
        )


def test_register_op_invalid_effect_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(effect=cast(Effect, "not_an_effect")))


def test_register_op_invalid_param_type_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(
            read_op(
                params=(OperationParam(name="x", type=cast(Any, "not_a_type"), description="a"),)
            )
        )


def test_register_op_invalid_accepts_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(accepts=(cast(SelectorKind, "not_a_selector"),)))


def test_register_op_invalid_default_depth_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(default_depth=cast(Depth, "not_a_depth")))


def test_op_inherits_category_thread_and_pacing() -> None:
    registry = make_registry()
    registry.register_category(
        widget_category(default_thread="main-async", default_pacing="structural")
    )
    op = registry.register(read_op())
    assert op.thread == "main-async"
    assert op.pacing == "structural"
    assert op.sources["thread"] == "category"
    assert op.sources["pacing"] == "category"


def test_op_declared_thread_and_pacing_take_precedence() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op(thread="main-async", pacing="structural"))
    assert op.thread == "main-async"
    assert op.pacing == "structural"
    assert op.sources["thread"] == "op"
    assert op.sources["pacing"] == "op"


def test_on_error_defaults_to_global_abort() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op())
    assert op.on_error == "abort"
    assert op.sources["on_error"] == "global"


def test_op_declared_thread_outside_category_allowed_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category(allowed_thread=frozenset({"main"})))
    with pytest.raises(RegistrationError):
        registry.register(read_op(thread="main-async"))


def test_op_declared_pacing_outside_category_allowed_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category(allowed_pacing=frozenset({"none"})))
    with pytest.raises(RegistrationError):
        registry.register(read_op(pacing="structural"))


@pytest.mark.parametrize(
    "effect,expected_undo",
    [
        ("read", "none"),
        ("reversible", "value"),
        ("create", "checkpoint"),
        ("destructive", "checkpoint"),
    ],
)
def test_undo_default_derivation(effect: Effect, expected_undo: str) -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    kwargs: dict[str, Any] = dict(
        name="widget.op",
        category="widget",
        description="d",
        effect=effect,
        to_python=lambda a: "x = 1",
    )
    if effect != "read":
        kwargs["undo_steps"] = lambda args, target_count: 1
    op = registry.register(OperationSpec(**kwargs))
    assert op.undo == expected_undo
    assert op.sources["undo"] == "global"


@pytest.mark.parametrize(
    "pacing,expected_phase_size", [("none", 50), ("structural", 5), ("media", 5)]
)
def test_phase_size_default_derivation(pacing: Pacing, expected_phase_size: int) -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op(pacing=pacing))
    assert op.phase_size == expected_phase_size
    assert op.sources["phase_size"] == "global"


def test_default_limit_and_max_limit_use_global_constants() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op())
    assert op.default_limit == DEFAULT_LIMIT
    assert op.max_limit == MAX_LIMIT
    assert op.sources["default_limit"] == "global"
    assert op.sources["max_limit"] == "global"


def test_phase_size_below_one_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(phase_size=0))


def test_phase_size_declared_positive_accepted() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op(phase_size=1))
    assert op.phase_size == 1


def test_default_limit_below_one_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(default_limit=0))


def test_max_limit_below_one_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(max_limit=0))


def test_default_limit_above_max_limit_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(default_limit=100, max_limit=10))


def test_default_limit_and_max_limit_declared_used_with_op_source() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op(default_limit=10, max_limit=20))
    assert op.default_limit == 10
    assert op.max_limit == 20
    assert op.sources["default_limit"] == "op"
    assert op.sources["max_limit"] == "op"


def test_server_op_with_to_python_rejected() -> None:
    registry = make_registry()
    registry.register_category(server_category())
    with pytest.raises(RegistrationError):
        registry.register(
            OperationSpec(
                name="plan.compute",
                category="plan",
                description="d",
                effect="read",
                to_python=lambda args: "x = 1",
            )
        )


def test_server_op_without_to_python_accepted() -> None:
    registry = make_registry()
    registry.register_category(server_category())
    op = registry.register(
        OperationSpec(
            name="plan.compute",
            category="plan",
            description="d",
            effect="read",
            server_handler=lambda args, ctx: ServerResult(result=None),
        )
    )
    assert op.thread == "server"
    assert op.to_python is None
    assert op.server_handler is not None


def test_main_op_without_to_python_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(
            OperationSpec(
                name="widget.read_thing", category="widget", description="d", effect="read"
            )
        )


def test_to_python_raising_exception_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())

    def boom(args: Any) -> str:
        raise RuntimeError("boom")

    with pytest.raises(RegistrationError):
        registry.register(read_op(to_python=boom))


def test_to_python_invalid_syntax_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(to_python=lambda args: "this is not ( valid python"))


_LOOP_CODE = "result = []\nfor item in flame.projects:\n    result.append(item)\n"


def test_write_op_with_loop_and_pacing_none_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category(default_pacing="none"))
    with pytest.raises(RegistrationError):
        registry.register(
            write_op(pacing="none", to_python=lambda args: _LOOP_CODE, requires=("flame.projects",))
        )


def test_write_op_with_loop_and_pacing_structural_accepted() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(
        write_op(
            pacing="structural", to_python=lambda args: _LOOP_CODE, requires=("flame.projects",)
        )
    )
    assert op.pacing == "structural"


def test_read_op_with_loop_and_pacing_none_accepted() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(
        read_op(pacing="none", to_python=lambda args: _LOOP_CODE, requires=("flame.projects",))
    )
    assert op.pacing == "none"


# The pacing guard must test every `accepts` kind, not just the first.


def _loop_only_for_second_accepts_kind(args: Any) -> str:
    """A write op's to_python that only generates a loop when its selector
    param is the *second* declared `accepts` kind ('wiretap' here, with
    accepts=('selection', 'wiretap')) -- everywhere else it's loop-free."""
    target = args["target"]
    if isinstance(target, dict) and target.get("kind") == "wiretap":
        return "for _x in flame.projects:\n    pass\n_result = 1"
    return "_result = 1"


def _make_hidden_loop_op(**overrides: Any) -> OperationSpec:
    defaults: dict[str, Any] = dict(
        name="widget.rename_thing",
        category="widget",
        description="d",
        effect="reversible",
        params=(OperationParam(name="target", type="selector", description="d", required=True),),
        accepts=("selection", "wiretap"),
        pacing="none",
        to_python=_loop_only_for_second_accepts_kind,
        undo_steps=lambda args, affected: affected,
    )
    defaults.update(overrides)
    return OperationSpec(**defaults)


def test_loop_hidden_behind_non_first_accepts_kind_is_rejected() -> None:
    """A loop that only appears for a later accepts kind is still rejected.

    A smoke test that built a sample selector for `accepts[0]` only ('selection' here) would
    let a loop that appears for a later kind ('wiretap') slip through undetected. The registry
    must smoke-test every declared `accepts` kind."""
    registry = make_registry()
    registry.register_category(widget_category(default_pacing="none"))
    with pytest.raises(RegistrationError, match="loop"):
        registry.register(_make_hidden_loop_op())


def test_loop_free_op_using_a_non_first_path_kind_is_still_accepted() -> None:
    """An op with no loop for any accepts kind registers with any kind listed first.

    Every real op is like that, since selector resolution lives in execution.py's prelude. It
    must register cleanly no matter which kind is listed first -- 'path' first here."""
    registry = make_registry()
    registry.register_category(widget_category(default_pacing="none"))
    op = registry.register(
        _make_hidden_loop_op(
            accepts=("path", "selection", "wiretap"),
            to_python=lambda args: "_result = 1",  # loop-free for every kind
        )
    )
    assert op.accepts == ("path", "selection", "wiretap")


def test_read_op_without_undo_steps_defaults_to_zero() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op())
    assert op.undo_steps({}, 5) == 0


def test_non_read_op_without_undo_steps_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(
            OperationSpec(
                name="widget.rename_thing",
                category="widget",
                description="d",
                effect="reversible",
                to_python=lambda args: "x = 1",
            )
        )


def test_requires_typo_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(requires=("PyClip.nmae",)))


def test_requires_correct_symbol_accepted() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(read_op(requires=("PyClip.name",)))
    assert op.requires == ("PyClip.name",)


def test_diagnose_typo_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(diagnose=("PyClip.get_wiretap_node_iddd",)))


def test_requires_malformed_symbol_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(requires=("PyClip",)))


def test_requires_too_many_dots_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError):
        registry.register(read_op(requires=("flame.PyClip.name",)))


def test_list_ops_filters_by_category() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    registry.register_category(server_category())
    registry.register(read_op())
    registry.register(
        OperationSpec(
            name="plan.compute",
            category="plan",
            description="d",
            effect="read",
            server_handler=lambda args, ctx: ServerResult(result=None),
        )
    )
    assert len(registry.list_ops()) == 2
    assert len(registry.list_ops(category="widget")) == 1
    assert registry.list_ops(category="widget")[0].name == "widget.read_thing"


def test_get_missing_returns_none() -> None:
    registry = make_registry()
    assert registry.get("widget.nope") is None


def test_server_op_without_a_handler_rejected() -> None:
    registry = make_registry()
    registry.register_category(server_category())
    with pytest.raises(RegistrationError, match="server_handler"):
        registry.register(
            OperationSpec(name="plan.compute", category="plan", description="d", effect="read")
        )


def test_flame_op_with_a_server_handler_rejected() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    with pytest.raises(RegistrationError, match="server_handler"):
        registry.register(
            read_op(server_handler=lambda args, ctx: ServerResult(result=None)),
        )


def journal_spec(**overrides: Any) -> JournalSpec:
    defaults: dict[str, Any] = dict(
        read_op="widget.read_thing",
        keys=lambda args: ["name"],
        values=lambda args: {"name": "x"},
        write_args=lambda target, values: {"target": target, "values": values},
    )
    defaults.update(overrides)
    return JournalSpec(**defaults)


def test_journaled_write_op_registered() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    op = registry.register(write_op(undo="value", journal=journal_spec()))
    assert op.journal is not None and op.journal.read_op == "widget.read_thing"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (dict(effect="read", undo=None, undo_steps=None), "read operation is never journaled"),
        (dict(undo="checkpoint"), "undo='value'"),
        (dict(journal=journal_spec(values=lambda args: {"other": 1})), "keys must equal"),
        (dict(journal=journal_spec(keys=lambda args: "name")), "list of str"),
        (dict(journal=journal_spec(write_args=lambda target, values: {})), "'target'"),
        (dict(journal=journal_spec(keys=lambda args: 1 / 0)), "journal spec raised"),
    ],
)
def test_bad_journal_spec_rejected_at_registration(overrides: dict[str, Any], message: str) -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    kwargs: dict[str, Any] = {"undo": "value", "journal": journal_spec()}
    kwargs.update(overrides)
    with pytest.raises(RegistrationError, match=message):
        registry.register(write_op(**kwargs))


def test_journal_on_a_server_thread_op_rejected() -> None:
    registry = make_registry()
    registry.register_category(server_category())
    with pytest.raises(RegistrationError, match="run in Flame"):
        registry.register(
            OperationSpec(
                name="plan.write",
                category="plan",
                description="d",
                effect="reversible",
                undo="value",
                undo_steps=lambda args, n: 0,
                server_handler=lambda args, ctx: ServerResult(result=None),
                journal=journal_spec(),
            )
        )


def reads_with_target_and_keys(**overrides: Any) -> OperationSpec:
    defaults: dict[str, Any] = dict(
        params=(
            OperationParam(name="target", type="selector", description="t", required=True),
            OperationParam(name="keys", type="array", description="k", item_type="string"),
        ),
        accepts=("wiretap",),
    )
    defaults.update(overrides)
    return read_op(**defaults)


def journaled_write(**overrides: Any) -> OperationSpec:
    defaults: dict[str, Any] = dict(
        name="widget.write_thing",
        undo="value",
        journal=journal_spec(),
        params=(OperationParam(name="target", type="selector", description="t", required=True),),
        accepts=("wiretap",),
    )
    defaults.update(overrides)
    return write_op(**defaults)


def test_validate_references_accepts_a_matching_read_op() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    registry.register(journaled_write())  # registered BEFORE its read op: allowed
    registry.register(reads_with_target_and_keys())
    registry.validate_references()


def test_validate_references_rejects_a_missing_read_op() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    registry.register(journaled_write())
    with pytest.raises(RegistrationError, match="not registered"):
        registry.validate_references()


def test_validate_references_rejects_a_read_op_that_is_a_write() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    registry.register(journaled_write(journal=journal_spec(read_op="widget.other_write")))
    registry.register(write_op(name="widget.other_write"))
    with pytest.raises(RegistrationError, match="not a read"):
        registry.validate_references()


def test_validate_references_rejects_a_read_op_without_target_and_keys() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    registry.register(journaled_write())
    registry.register(read_op())
    with pytest.raises(RegistrationError, match="'target' and 'keys'"):
        registry.validate_references()


def test_validate_references_rejects_a_read_op_that_does_not_accept_the_write_kinds() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    registry.register(journaled_write(accepts=("wiretap", "path")))
    registry.register(reads_with_target_and_keys())
    with pytest.raises(RegistrationError, match="does not accept"):
        registry.validate_references()


def test_every_real_journaled_op_passes_reference_validation() -> None:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)  # register_all ends with validate_references()
    journaled = sorted(op.name for op in registry.list_ops() if op.journal is not None)
    assert journaled == [
        "action.set_attributes",
        "batch.set_attributes",
        "desktop.rename",
        "desktop.set_attributes",
        "node.set_attributes",
        "segment.set_attributes",
    ]
