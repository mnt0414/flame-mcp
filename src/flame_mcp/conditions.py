"""`{path, equals | in | len}` conditions shared by the job runner (`test`, `until`) and
`plan.for_each` (`where`): checking their shape, judging a value against one, and describing one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from flame_mcp.refs import RefsError, RefsResolveError, Token, leaves, parse_path

OPERATORS: Final[tuple[str, ...]] = ("equals", "in", "len")


def same(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    return bool(actual == expected)


def holds(cond: Mapping[str, Any], leaf: Any) -> bool:
    if "equals" in cond:
        return same(leaf, cond["equals"])
    if "in" in cond:
        return isinstance(cond["in"], list) and any(same(leaf, item) for item in cond["in"])
    try:
        return bool(len(leaf) == cond["len"])
    except TypeError:
        return False


@dataclass(frozen=True)
class Condition:
    """A condition with its path read once."""

    spec: Mapping[str, Any]
    tokens: tuple[Token, ...]

    @property
    def is_wild(self) -> bool:
        return any(kind == "all" for kind, _ in self.tokens)


def compile_conditions(conditions: Sequence[Mapping[str, Any]]) -> list[Condition]:
    """The conditions with their paths parsed (`check_conditions` has accepted them)."""
    return [Condition(cond, tuple(parse_path(cond["path"]))) for cond in conditions]


def why_not(cond: Condition, root: Any) -> str | None:
    """Why `cond` does not hold for `root`, None when it does. A path with `[*]` needs a
    non-empty list whose every element satisfies the condition; a missing path fails."""
    try:
        values = leaves(root, list(cond.tokens))
    except RefsResolveError as exc:
        return str(exc)
    if cond.is_wild and not values:
        return "no elements"
    for leaf in values:
        if not holds(cond.spec, leaf):
            return f"got {json.dumps(leaf, default=str)}"
    return None


def matches(conditions: Sequence[Condition], root: Any) -> bool:
    """`root` meets every condition."""
    return all(why_not(cond, root) is None for cond in conditions)


def describe_condition(cond: Mapping[str, Any]) -> str:
    """`path == value` / `path in [...]` / `path len N`."""
    operator = next(op for op in OPERATORS if op in cond)
    shown = json.dumps(cond[operator])
    return f"{cond['path']} {'==' if operator == 'equals' else operator} {shown}"


def _is_template(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value


def check_conditions(conditions: Any, where: str, *, allow_templates: bool = False) -> None:
    """Raises `RefsError` unless `conditions` is a non-empty list of well-formed conditions.
    `allow_templates`: an `in` or `len` value may still be a `{{ ... }}` string (the job runner
    fills it in later)."""
    if not isinstance(conditions, list) or not conditions:
        raise RefsError(f"{where}: must be a non-empty list of conditions")
    for cond in conditions:
        if not isinstance(cond, dict) or not isinstance(cond.get("path"), str):
            raise RefsError(f"{where}: a condition needs a string 'path'")
        present = [op for op in OPERATORS if op in cond]
        if len(present) != 1 or set(cond) != {"path", present[0]}:
            raise RefsError(f"{where}: a condition needs 'path' and exactly one of equals/in/len")
        parse_path(cond["path"])
        if present[0] == "in" and not isinstance(cond["in"], list):
            if not (allow_templates and _is_template(cond["in"])):
                raise RefsError(f"{where}: 'in' needs a list")
        length = cond.get("len")
        if "len" in cond and not (allow_templates and _is_template(length)):
            if not isinstance(length, int) or isinstance(length, bool) or length < 0:
                raise RefsError(f"{where}: 'len' needs an integer >= 0")
