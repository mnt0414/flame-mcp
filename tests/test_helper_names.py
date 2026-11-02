"""The helpers share one namespace, so one name must not be defined by two of them."""

from __future__ import annotations

import ast
from collections import defaultdict

from flame_mcp import execution


def _top_level_defs(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def test_no_function_or_class_is_defined_by_two_helpers() -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for attr in dir(execution):
        value = getattr(execution, attr)
        if attr.endswith("_HELPER") and isinstance(value, str):
            for name in _top_level_defs(value):
                owners[name].append(attr)
    clashes = {name: sorted(set(who)) for name, who in owners.items() if len(set(who)) > 1}
    assert clashes == {}
