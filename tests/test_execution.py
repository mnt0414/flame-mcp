"""Execution wrapper tests (`flame_mcp.execution.build_exec_code`).

No bridge/socket is involved here: the generated code is `ast.parse`d (as
the bridge itself does before scheduling it) and then `exec`'d directly
against a small fake `flame` module, exactly the shape the real bridge's
`_main_run_exec` hands to generated code (`ns = {"flame": ..., "__name__": ...}`).
"""

from __future__ import annotations

import ast
from types import SimpleNamespace
from typing import Any

from flame_mcp import execution
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationParam, OperationSpec


class _PyAttr:
    """A stand-in for Flame's PyAttribute: has get_value(), unlike a plain str.

    Most `.name` attributes are this, not str."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def get_value(self) -> Any:
        return self._value


_PyAttr.__name__ = "PyAttribute"  # `_v` matches on the type name


class _FakePyTime:
    """Stand-in for Flame's PyTime.

    It has no `get_value()`, but has both `timecode` and `frame` members. `str()` is quoted,
    exactly the bug `_j` must not reproduce; `repr()` is plain.

    `_j` matches on `type(x).__name__ == 'PyTime'`, not on
    `hasattr(x, 'timecode')`/`hasattr(x, 'frame')` -- so this class's `__name__`
    is renamed to `PyTime` below to stand in for the real type.
    """

    def __init__(self, timecode: str, frame: int) -> None:
        self.timecode = timecode
        self.frame = frame

    def __str__(self) -> str:
        return repr(self.timecode)

    def __repr__(self) -> str:
        return self.timecode


_FakePyTime.__name__ = "PyTime"


class _FakeQuotedRepr:
    """Has neither `timecode` nor `frame`, so `_j` must fall through to its
    generic branch. `str()` is quoted like `_FakePyTime`'s so a regression
    back to `str()` would be caught the same way."""

    def __str__(self) -> str:
        return "'quoted'"

    def __repr__(self) -> str:
        return "plain"


class _FakeUnknownAttrsReturnNone:
    """Stand-in for a PyFlameObject subclass whose `__getattr__` returns
    `None` for any name it does not specifically recognize, instead of
    raising `AttributeError` the way `object` does for every miss -- the
    shape that makes a `hasattr(x, 'timecode') and hasattr(x, 'frame')`
    check misfire (any such object looked PyTime-like, since `hasattr` is
    `True` whenever access does not raise). It raises for `get_value`
    specifically (it does not support that method), so `_v(x)` returns `x`
    unchanged and this isolates `_j`'s own PyTime-detection branch.
    `type(x).__name__` is not `'PyTime'`, so `_j` must fall through to
    `repr()`, not build a `{timecode, frame}` dict."""

    def __getattr__(self, name: str) -> None:
        if name == "get_value":
            raise AttributeError(name)
        return None

    def __repr__(self) -> str:
        return "not-a-pytime"


def _fake_flame(
    *,
    project: str = "Proj",
    workspace: str = "WS",
    selected: list[Any] | None = None,
    with_media_panel: bool = True,
) -> SimpleNamespace:
    project_obj = SimpleNamespace(
        name=project,  # PyProject.name is a plain str
        current_workspace=SimpleNamespace(name=_PyAttr(workspace)),
    )
    flame = SimpleNamespace(projects=SimpleNamespace(current_project=project_obj))
    if with_media_panel:
        entries = list(selected) if selected is not None else []
        flame.media_panel = SimpleNamespace(selected_entries=entries)
    return flame


def _registry_with_op(
    *,
    effect: str = "read",
    to_python: Any,
    verify: Any = None,
    undo_steps: Any = None,
    params: tuple[OperationParam, ...] = (),
) -> Registry:
    registry = Registry(symbols=frozenset())
    registry.register_category(
        Category(
            name="test",
            description="test category",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    registry.register(
        OperationSpec(
            name="test.op",
            category="test",
            description="test op",
            effect=effect,  # type: ignore[arg-type]
            params=params,
            to_python=to_python,
            verify=verify,
            undo_steps=undo_steps,
        )
    )
    return registry


def _run(
    registry: Registry, args: dict[str, Any], *, flame: SimpleNamespace | None = None
) -> dict[str, Any]:
    op = registry.get("test.op")
    assert op is not None
    code = execution.build_exec_code(op, args)
    ast.parse(code)  # the bridge parses before scheduling; this must never fail
    ns: dict[str, Any] = {
        "flame": flame if flame is not None else _fake_flame(),
        "__name__": "test",
    }
    exec(compile(code, "<test>", "exec"), ns)
    result: dict[str, Any] = ns["_result"]
    return result


def test_read_op_success_has_default_affected_zero_and_full_rider() -> None:
    registry = _registry_with_op(effect="read", to_python=lambda args: "_result = 'hello'")
    result = _run(registry, {})
    assert result["value"] == "hello"
    assert result["affected"] == 0
    assert result["opError"] is None
    assert result["opTraceback"] is None
    assert result["verified"] is None
    assert result["verifyDetail"] is None
    assert result["context"] == {"project": "Proj", "workspace": "WS", "selected": 0}


def test_op_exception_sets_op_error_and_traceback_and_skips_verify() -> None:
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: "raise ValueError('boom')",
        verify=lambda args: "_verified = True",
        undo_steps=lambda args, affected: affected,
    )
    result = _run(registry, {})
    assert result["value"] is None
    assert result["affected"] == 0  # failure => 0 regardless of effect's default
    assert result["opError"] is not None and "boom" in result["opError"]
    assert result["opTraceback"] is not None and "ValueError: boom" in result["opTraceback"]
    # verify's contract says it only runs on op success; it must not have run.
    assert result["verified"] is None
    assert result["verifyDetail"] is None


def test_reversible_op_default_affected_is_one_on_success() -> None:
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: "_result = 'ok'",
        undo_steps=lambda args, affected: affected,
    )
    result = _run(registry, {})
    assert result["opError"] is None
    assert result["affected"] == 1


def test_op_sets_affected_explicitly_and_preserves_dict_value() -> None:
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: "_result = {'inner': 42}\n_affected = 3",
        undo_steps=lambda args, affected: affected,
    )
    result = _run(registry, {})
    assert result["value"] == {"inner": 42}
    assert result["affected"] == 3


def test_verify_sets_verified_false_with_detail() -> None:
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: f"_result = None\nflame._thing = {args['value']!r}\n_affected = 1",
        verify=lambda args: "_verified = False\n_verify_detail = 'value did not stick'",
        undo_steps=lambda args, affected: affected,
        params=(OperationParam(name="value", type="string", description="v", required=True),),
    )
    result = _run(registry, {"value": "x"})
    assert result["opError"] is None
    assert result["affected"] == 1
    assert result["verified"] is False
    assert result["verifyDetail"] == "value did not stick"


def test_verify_raising_is_caught_and_maps_to_verified_false() -> None:
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: "_result = 'ok'\n_affected = 1",
        verify=lambda args: "raise RuntimeError('verify boom')",
        undo_steps=lambda args, affected: affected,
    )
    result = _run(registry, {})
    assert result["verified"] is False
    detail = result["verifyDetail"]
    assert detail is not None and "RuntimeError: verify boom" in detail


def test_verify_true_on_success() -> None:
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: "_result = 'ok'\n_affected = 1",
        verify=lambda args: "_verified = True",
        undo_steps=lambda args, affected: affected,
    )
    result = _run(registry, {})
    assert result["verified"] is True
    assert result["verifyDetail"] is None


def test_rider_field_failure_is_isolated_to_that_field() -> None:
    registry = _registry_with_op(effect="read", to_python=lambda args: "_result = 1")
    flame = _fake_flame(with_media_panel=False)
    result = _run(registry, {}, flame=flame)
    assert result["context"]["project"] == "Proj"
    assert result["context"]["workspace"] == "WS"
    assert result["context"]["selected"] is None  # media_panel missing -> caught, not raised


def test_project_and_workspace_rider_fields_both_fail_independently() -> None:
    registry = _registry_with_op(effect="read", to_python=lambda args: "_result = 1")
    # deliberately no .projects at all, to exercise that field's own try/except
    flame = SimpleNamespace(media_panel=SimpleNamespace(selected_entries=[1, 2]))
    result = _run(registry, {}, flame=flame)
    assert result["context"] == {"project": None, "workspace": None, "selected": 2}


def test_multiline_string_value_is_preserved_byte_for_byte() -> None:
    # Op source must not be textwrap.indent()'d straight into the wrapper's try block: that
    # silently corrupts any multi-line string literal the op's own code contains (indent has no
    # notion of "inside a triple-quoted string"). It must come through unchanged.
    literal_value = "line one\n    indented line two\nline three"
    op_src = f"_result = '''{literal_value}'''"
    registry = _registry_with_op(effect="read", to_python=lambda args: op_src)
    result = _run(registry, {})
    assert result["value"] == literal_value


def test_op_exception_traceback_names_the_op_and_its_own_line_number() -> None:
    # The op's code runs via its own compile()/exec() with filename "<op:NAME>", so a traceback
    # should name that file and the line *within the op's own source* (line 3 here), not this
    # wrapper's line where the nested exec() call itself sits.
    op_src = "\n".join(["x = 1", "y = 2", "raise ValueError('boom')"])
    registry = _registry_with_op(effect="read", to_python=lambda args: op_src)
    result = _run(registry, {})
    assert result["opError"] is not None and "boom" in result["opError"]
    tb = result["opTraceback"]
    assert tb is not None
    assert "<op:test.op>" in tb
    assert 'File "<op:test.op>", line 3' in tb


def test_generated_code_embeds_distinct_op_and_verify_filenames() -> None:
    # verify's own failure only ever surfaces its last traceback line via
    # `_verify_detail` (no full-traceback field is threaded through), so the
    # filename claim for verify is checked at the source level instead of
    # via a runtime traceback.
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: "_result = 'ok'\n_affected = 1",
        verify=lambda args: "_verified = True",
        undo_steps=lambda args, affected: affected,
    )
    op = registry.get("test.op")
    assert op is not None
    code = execution.build_exec_code(op, {})
    assert "'<op:test.op>'" in code
    assert "'<verify:test.op>'" in code


def test_op_error_sets_op_error_code_and_message_and_skips_generic_traceback() -> None:
    registry = _registry_with_op(
        effect="read",
        to_python=lambda args: "raise _OpError('SELECTOR_EMPTY', 'no match')",
    )
    result = _run(registry, {})
    assert result["opError"] == "no match"
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
    assert result["opTraceback"] is None
    assert result["value"] is None


def test_plain_exception_leaves_op_error_code_none() -> None:
    registry = _registry_with_op(effect="read", to_python=lambda args: "raise ValueError('boom')")
    result = _run(registry, {})
    assert result["opErrorCode"] is None
    assert result["opError"] is not None and "boom" in result["opError"]


def test_j_helper_maps_pytime_like_object_to_timecode_and_frame_dict() -> None:
    registry = _registry_with_op(effect="read", to_python=lambda args: "_result = _j(flame._probe)")
    flame = _fake_flame()
    flame._probe = _FakePyTime("00:00:00+00", 0)
    result = _run(registry, {}, flame=flame)
    assert result["value"] == {"timecode": "00:00:00+00", "frame": 0}


def test_j_helper_uses_repr_not_str_for_non_pytime_objects() -> None:
    registry = _registry_with_op(effect="read", to_python=lambda args: "_result = _j(flame._probe)")
    flame = _fake_flame()
    flame._probe = _FakeQuotedRepr()
    result = _run(registry, {}, flame=flame)
    assert result["value"] == "plain"


def test_j_helper_does_not_misfire_on_object_whose_getattr_returns_none() -> None:
    """`_j` does not treat a getattr-returns-None object as a PyTime.

    An object shaped like the `hasattr` trap (unknown
    attributes resolve to `None` instead of raising) must not be treated as
    a PyTime -- it must fall through to `repr()`, never a
    `{timecode, frame}` dict."""
    registry = _registry_with_op(effect="read", to_python=lambda args: "_result = _j(flame._probe)")
    flame = _fake_flame()
    flame._probe = _FakeUnknownAttrsReturnNone()
    result = _run(registry, {}, flame=flame)
    assert result["value"] == "not-a-pytime"


def test_j_helper_passes_plain_scalars_and_none_through_unchanged() -> None:
    registry = _registry_with_op(
        effect="read",
        to_python=lambda args: "_result = [_j(None), _j('s'), _j(1), _j(1.5), _j(True)]",
    )
    result = _run(registry, {})
    assert result["value"] == [None, "s", 1, 1.5, True]


def test_generated_code_is_always_valid_python_ast() -> None:
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: "_result = 'ok'\n_affected = 1",
        verify=lambda args: "_verified = True",
        undo_steps=lambda args, affected: affected,
    )
    op = registry.get("test.op")
    assert op is not None
    code = execution.build_exec_code(op, {})
    tree = ast.parse(code)  # raises SyntaxError if this is ever not valid Python
    assert isinstance(tree, ast.Module)


class _CatchAllObject:
    """A PyFlameObject-like whose unknown attributes answer None.

    `hasattr(x, 'get_value')` is True, and `x.get_value` is None. A hasattr-based `_v`
    would call `None()` on it; the type-name check leaves it alone."""

    def __getattr__(self, name: str) -> None:
        if name.startswith("__"):
            raise AttributeError(name)
        return None

    def __repr__(self) -> str:
        return "catch-all"


def test_v_leaves_a_catch_all_object_alone_instead_of_calling_none() -> None:
    flame = _fake_flame()
    flame.thing = _CatchAllObject()
    assert hasattr(flame.thing, "get_value")  # the trap this test is about
    registry = _registry_with_op(
        to_python=lambda args: "_result = [_v(flame.thing) is flame.thing, _j(flame.thing)]"
    )
    result = _run(registry, {}, flame=flame)
    assert result["opError"] is None, result["opError"]
    assert result["value"] == [True, "catch-all"]


def test_v_unwraps_only_objects_typed_pyattribute() -> None:
    class Impostor:
        def get_value(self) -> str:
            return "unwrapped"

    flame = _fake_flame()
    flame.real = _PyAttr("unwrapped")
    flame.impostor = Impostor()
    registry = _registry_with_op(
        to_python=lambda args: "_result = [_v(flame.real), _v(flame.impostor) is flame.impostor]"
    )
    result = _run(registry, {}, flame=flame)
    assert result["value"] == ["unwrapped", True]


def test_op_partial_and_value_survive_an_op_error() -> None:
    registry = _registry_with_op(
        effect="reversible",
        to_python=lambda args: (
            "_result = {'kept': 1}\n_affected = 2\n_partial = {'applied': 2, 'failed': 1}\n"
            "raise _OpError('SILENT_NOOP', 'one did not stick')"
        ),
        undo_steps=lambda args, affected: 0,
    )
    result = _run(registry, {})
    assert result["opErrorCode"] == "SILENT_NOOP"
    assert result["value"] == {"kept": 1}
    assert result["partial"] == {"applied": 2, "failed": 1}
    assert result["affected"] == 2


def test_partial_defaults_to_none() -> None:
    registry = _registry_with_op(to_python=lambda args: "_result = 1")
    assert _run(registry, {})["partial"] is None
