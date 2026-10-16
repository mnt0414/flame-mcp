"""Argument validation for registry operations.

Adapted from mcp-aftereffects `src/opschema.ts` (kumo.productions, Inc., MIT License; see
THIRD_PARTY_NOTICES.md).

`flame_do` accepts a free-form args object; this module turns the same
OperationParam declarations the catalog publishes into a real gate, so an
operation can never advertise a schema it doesn't enforce.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, assert_never

from flame_mcp import selectors
from flame_mcp.types import OperationParam, ParamType, SelectorKind

_UNSET = object()


class HasParams(Protocol):
    @property
    def params(self) -> Sequence[OperationParam]: ...

    @property
    def accepts(self) -> Sequence[SelectorKind]: ...


@dataclass(frozen=True)
class ArgIssue:
    path: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    value: Mapping[str, Any] | None = None
    issues: tuple[ArgIssue, ...] = ()


def type_matches(type_: ParamType, value: Any) -> bool:
    match type_:
        case "string":
            return isinstance(value, str)
        case "number":
            # bool is an int subclass in Python; it must not pass as a number.
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        case "boolean":
            return isinstance(value, bool)
        case "array":
            return isinstance(value, list)
        case "object":
            return isinstance(value, dict)
        case "any":
            return True
        case "selector":
            return isinstance(value, dict)
        case _ as unreachable:
            assert_never(unreachable)


def describe_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, float) and not math.isfinite(value):
        return "non-finite number"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def find_non_finite(value: Any, path: str = "") -> str | None:
    """Returns the path of the first NaN / Infinity / -Infinity inside `value`, or None.

    Looks through lists and dicts. Generated code embeds arguments with `repr()`, and
    `repr(float('nan'))` is not a Python literal, so a value like that must be refused
    here, before any code is generated.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return path or "(value)"
    if isinstance(value, list):
        for index, element in enumerate(value):
            found = find_non_finite(element, f"{path}[{index}]")
            if found is not None:
                return found
    if isinstance(value, dict):
        for key, element in value.items():
            found = find_non_finite(element, f"{path}[{key!r}]")
            if found is not None:
                return found
    return None


_NON_ASCII_ADVICE = "Flame takes ASCII only. Use ASCII characters."


def _non_ascii_issue(path: str, text: str, where: str) -> ArgIssue:
    char = next(ch for ch in text if not ch.isascii())
    return ArgIssue(
        path, f"non-ASCII character U+{ord(char):04X} in a {where}. {_NON_ASCII_ADVICE}"
    )


def find_non_ascii(value: Any, path: str = "args") -> ArgIssue | None:
    """Returns where the first non-ASCII character sits inside `value`, or None.

    Looks through every string, list element and dict key. Flame can crash on a 2-byte
    character, so nothing that holds one is sent. The issue names the path and the code point
    and never repeats the character itself, so the message stays ASCII.
    """
    if isinstance(value, str):
        return None if value.isascii() else _non_ascii_issue(path, value, "value")
    if isinstance(value, (list, tuple)):
        for index, element in enumerate(value):
            found = find_non_ascii(element, f"{path}[{index}]")
            if found is not None:
                return found
    if isinstance(value, Mapping):
        for key, element in value.items():
            if isinstance(key, str) and not key.isascii():
                return _non_ascii_issue(path, key, "key")
            found = find_non_ascii(element, f"{path}.{key}")
            if found is not None:
                return found
    return None


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        curr = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[len(b)]


def suggest_name(key: str, candidates: Sequence[str]) -> str | None:
    lowered = key.lower()
    best: str | None = None
    best_distance = math.inf
    for candidate in candidates:
        distance = _levenshtein(lowered, candidate.lower())
        if distance < best_distance:
            best_distance = distance
            best = candidate
    if best is None:
        return None
    budget = max(1, int(max(len(key), len(best)) * 0.4))
    return best if best_distance <= budget else None


def summarize_params(params: Sequence[OperationParam]) -> list[str]:
    result = []
    for param in params:
        required = "required" if param.required else "optional"
        nullable = ", nullable" if param.nullable else ""
        result.append(f"{param.name}: {param.type} ({required}{nullable})")
    return result


def compact_params(params: Sequence[OperationParam]) -> list[str]:
    """Returns names only, with a trailing `*` on the required ones (`["target*", "limit"]`)."""
    return [f"{param.name}*" if param.required else param.name for param in params]


SUMMARY_MAX_CHARS = 160
_SENTENCE_END = re.compile(r"(?<!e\.g)(?<!i\.e)(?<!etc)\.(?=\s+[A-Z`'\"(])")


def first_sentence(text: str, limit: int | None = SUMMARY_MAX_CHARS) -> str:
    """Returns the first sentence of `text`, at most `limit` characters (`None`: no limit).

    A sentence ends at a period followed by whitespace and a capital, quote, backtick
    or paren; `e.g.` / `i.e.` / `etc.` do not end it. A longer one is cut to
    `limit - 1` characters plus an ellipsis.
    """
    flat = " ".join(text.split())
    match = _SENTENCE_END.search(flat)
    sentence = flat[: match.end()] if match else flat
    if limit is None or len(sentence) <= limit:
        return sentence
    return sentence[: limit - 1].rstrip() + "\u2026"


def validate_op_args(op: HasParams, raw: object) -> ValidationResult:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        return ValidationResult(
            ok=False,
            issues=(ArgIssue("args", f"must be an object, got {describe_value(raw)}"),),
        )

    non_ascii = find_non_ascii(raw)
    if non_ascii is not None:
        return ValidationResult(ok=False, issues=(non_ascii,))

    issues: list[ArgIssue] = []
    declared = op.params
    declared_names = [p.name for p in declared]

    for param in declared:
        value = raw.get(param.name, _UNSET)
        if value is _UNSET:
            if param.required:
                issues.append(
                    ArgIssue(
                        param.name,
                        f"missing required parameter ({param.type}) -- {param.description}",
                    )
                )
            continue
        if value is None:
            if not param.nullable and param.type != "any":
                issues.append(
                    ArgIssue(
                        param.name,
                        f"must be {param.type}, got null -- omit the key instead of passing null",
                    )
                )
            continue
        if not type_matches(param.type, value):
            issues.append(
                ArgIssue(param.name, f"must be {param.type}, got {describe_value(value)}")
            )
            continue
        if param.type in ("object", "array", "any"):
            non_finite = find_non_finite(value, param.name)
            if non_finite is not None:
                issues.append(
                    ArgIssue(non_finite, "must not be NaN or Infinity (not representable in JSON)")
                )
                continue
        found_before = len(issues)
        if param.type == "string" and param.non_empty and value == "":
            issues.append(ArgIssue(param.name, "must not be empty"))
        if param.type == "number":
            if param.integer and value != int(value):
                issues.append(ArgIssue(param.name, f"must be an integer, got {value!r}"))
            if param.minimum is not None and value < param.minimum:
                issues.append(ArgIssue(param.name, f"must be >= {param.minimum}, got {value!r}"))
        if param.enum is not None and len(issues) == found_before and value not in param.enum:
            listed = ", ".join(repr(v) for v in param.enum)
            issues.append(ArgIssue(param.name, f"must be one of {listed}, got {value!r}"))
        if param.type == "array" and param.item_type is not None:
            for index, element in enumerate(value):
                if not type_matches(param.item_type, element):
                    issues.append(
                        ArgIssue(
                            f"{param.name}[{index}]",
                            f"must be {param.item_type}, got {describe_value(element)}",
                        )
                    )
        if param.type == "selector":
            for selector_issue in selectors.validate_selector(value, tuple(op.accepts)):
                issues.append(ArgIssue(param.name, selector_issue))

    for key in raw:
        if key in declared_names:
            continue
        suggestion = suggest_name(key, declared_names)
        accepted = ", ".join(declared_names) or "no parameters"
        message = (
            f"unknown parameter -- did you mean '{suggestion}'?"
            if suggestion
            else f"unknown parameter (this operation accepts: {accepted})"
        )
        issues.append(ArgIssue(key, message))

    if issues:
        return ValidationResult(ok=False, issues=tuple(issues))
    return ValidationResult(ok=True, value=raw)
