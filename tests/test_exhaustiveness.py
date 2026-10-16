"""get_args() drives parametrization, so adding a member to a Literal in types.py
automatically grows this test until every registered branch function handles it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_args

import pytest

from flame_mcp import errors, hierarchy, opschema, policy, registry, selectors, transport
from flame_mcp import types as t


def _sample_selector_for_kind(kind: t.SelectorKind) -> dict[str, Any]:
    """Minimal well-formed selector of `kind`, for probing the SelectorKind
    branch functions below (validate_selector / resolve_code /
    registry.sample_selector_for all match on `sel["kind"]`)."""
    if kind == "wiretap":
        return {"kind": "wiretap", "id": "x"}
    if kind == "path":
        return {"kind": "path", "path": ["x"]}
    if kind == "segment":
        return {
            "kind": "segment",
            "parent": {"kind": "wiretap", "id": "x"},
            "version": 0,
            "track": 0,
            "index": 0,
        }
    if kind == "batch":
        return {"kind": "batch", "name": "x"}
    if kind == "node":
        return {"kind": "node", "batch": {"kind": "batch", "name": "x"}, "name": "x"}
    if kind == "action_node":
        return {
            "kind": "action_node",
            "node": {"kind": "node", "batch": {"kind": "batch", "name": "x"}, "name": "x"},
            "name": "x",
        }
    if kind == "desktop":
        return {"kind": "desktop"}
    if kind == "timeline":
        return {"kind": "timeline"}
    assert kind == "selection"  # a kind added to SelectorKind must get its sample above
    return {"kind": "selection"}


def _probe_validate_selector(kind: t.SelectorKind) -> None:
    selectors.validate_selector(_sample_selector_for_kind(kind), get_args(t.SelectorKind))


def _probe_resolve_code(kind: t.SelectorKind) -> None:
    selectors.resolve_code(_sample_selector_for_kind(kind), "target")


def _probe_sample_selector_for(kind: t.SelectorKind) -> None:
    registry.sample_selector_for((kind,))


def _probe_timeout_for(thread: t.Thread) -> None:
    """`transport.timeout_for` is a match+assert_never function like the
    others, but 'server' is a legitimately-handled case that raises
    ValueError on purpose (server-thread ops never reach the bridge) rather
    than returning a float. Plain `fn(member)` can't tell "designed to
    raise ValueError" apart from "fell through to assert_never" (both are
    just an exception), so this wrapper asserts the *specific* exception
    for 'server' and lets every other member call through normally — a
    forgotten branch still fails this test, either as an unexpected
    AssertionError propagating past `pytest.raises(ValueError)`, or (for
    'main'/'main-async') as an uncaught AssertionError from assert_never.
    """
    if thread == "server":
        with pytest.raises(ValueError):
            transport.timeout_for(thread)
        return
    transport.timeout_for(thread)


# Explicit annotation: with 9 heterogeneous-signature callables as dict values,
# mypy's join collapses the inferred value type to bare `object` (losing the
# list structure entirely, so `for f in fns` fails as "not iterable"). Pin the
# type instead of relying on inference.
BRANCHES: dict[Any, list[Callable[[Any], Any]]] = {
    t.ParamType: [lambda v: opschema.type_matches(v, "probe")],
    t.Effect: [policy.blocked_in_readonly, policy.effect_label, registry.default_undo_for],
    t.Thread: [registry.touches_flame, _probe_timeout_for],
    t.Pacing: [registry.default_phase_size],
    t.OnError: [registry.continues_on_error],
    t.Undo: [policy.undo_label],
    t.Depth: [hierarchy.next_level],
    t.SelectorKind: [
        selectors.selector_label,
        _probe_validate_selector,
        _probe_resolve_code,
        _probe_sample_selector_for,
        selectors.describe_kind,
    ],
    t.ErrorCode: [errors.hint_for, errors.is_retryable],
}


@pytest.mark.parametrize(
    "enum_type,member,fn",
    [(et, m, f) for et, fns in BRANCHES.items() for m in get_args(et) for f in fns],
    ids=lambda v: str(v),
)
def test_every_member_is_handled(enum_type: object, member: str, fn: object) -> None:
    fn(member)  # type: ignore[operator]  # assert_never must not raise here


def test_branches_covers_every_enum() -> None:
    declared = {
        v for v in vars(t).values() if get_args(v) and all(isinstance(a, str) for a in get_args(v))
    }
    assert declared <= set(BRANCHES), f"Enums missing from BRANCHES: {declared - set(BRANCHES)}"


def test_branches_are_non_empty() -> None:
    for enum_type, fns in BRANCHES.items():
        assert fns, f"{enum_type} has no branch function registered"
