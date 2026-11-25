"""Paths and `{{ ... }}` references shared by `plan.run` and the job runner
(`scripts/job_runner_recipe.py`): reading `result.items[0].sel` / `[*]` paths, walking a value
along one, and replacing the references in the strings of a value.

Nothing here knows what a reference points at: the caller gives `substitute` a `lookup`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Final

REF_RE: Final[re.Pattern[str]] = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_HEAD_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEY_RE: Final[re.Pattern[str]] = re.compile(r"(\.)?([^.\[\]\s]+)")
_INDEX_RE: Final[re.Pattern[str]] = re.compile(r"\[(\d+|\*)\]")

Token = tuple[str, Any]  # ("key", name) | ("idx", n) | ("all", None)


class RefsError(Exception):
    """A malformed path or reference: what was written must be fixed."""


class RefsResolveError(RefsError):
    """A well-formed reference that cannot be resolved against the data at hand."""


def parse_path(text: str) -> list[Token]:
    """`result.items[0].sel` / `.key` / `[*]` as tokens."""
    text = text.strip()
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        index = _INDEX_RE.match(text, pos)
        if index is not None:
            raw = index.group(1)
            tokens.append(("all", None) if raw == "*" else ("idx", int(raw)))
            pos = index.end()
            continue
        key = _KEY_RE.match(text, pos)
        if key is None or (pos > 0 and key.group(1) is None):
            raise RefsError(f"malformed path: {text!r}")
        tokens.append(("key", key.group(2)))
        pos = key.end()
    return tokens


def format_path(tokens: list[Token]) -> str:
    out = ""
    for kind, arg in tokens:
        out += f".{arg}" if kind == "key" else (f"[{arg}]" if kind == "idx" else "[*]")
    return out


def _step_into(data: Any, token: Token) -> Any:
    kind, arg = token
    if kind == "key":
        if not isinstance(data, dict) or arg not in data:
            raise RefsResolveError(f"missing key '{arg}'")
        return data[arg]
    if not isinstance(data, list) or (kind == "idx" and arg >= len(data)):
        raise RefsResolveError(
            f"index [{arg if kind == 'idx' else '*'}]: expected a list with that element"
        )
    return data[arg]


def walk(data: Any, tokens: list[Token]) -> Any:
    """The value at `tokens`; `[*]` maps the rest of the path over each list element."""
    for i, token in enumerate(tokens):
        if token[0] == "all":
            if not isinstance(data, list):
                raise RefsResolveError("[*] needs a list")
            return [walk(item, tokens[i + 1 :]) for item in data]
        data = _step_into(data, token)
    return data


def leaves(data: Any, tokens: list[Token]) -> list[Any]:
    """Like `walk`, but the values at the end of a `[*]` path as one flat list."""
    for i, token in enumerate(tokens):
        if token[0] == "all":
            if not isinstance(data, list):
                raise RefsResolveError("[*] needs a list")
            return [leaf for item in data for leaf in leaves(item, tokens[i + 1 :])]
        data = _step_into(data, token)
    return [data]


def _embed(value: Any) -> str:
    if isinstance(value, dict | list):
        raise RefsResolveError(
            "a dict/list cannot be mixed into a string; use a whole-string reference"
        )
    if value is None or isinstance(value, bool):
        raise RefsResolveError(
            "None or bool cannot be mixed into a string; use a whole-string reference"
        )
    return value if isinstance(value, str) else str(value)


def _check_braces(literal: str) -> None:
    if "{{" in literal or "}}" in literal:
        raise RefsError(f"malformed reference in {literal!r}")


def _substitute_str(text: str, lookup: Callable[[str], Any]) -> Any:
    matches = list(REF_RE.finditer(text))
    if not matches:
        _check_braces(text)
        return text
    if len(matches) == 1 and matches[0].span() == (0, len(text)):
        return lookup(matches[0].group(1).strip())
    parts: list[str] = []
    pos = 0
    for match in matches:
        _check_braces(text[pos : match.start()])
        parts.append(text[pos : match.start()])
        parts.append(_embed(lookup(match.group(1).strip())))
        pos = match.end()
    _check_braces(text[pos:])
    parts.append(text[pos:])
    return "".join(parts)


def substitute(value: Any, lookup: Callable[[str], Any]) -> Any:
    """Replaces every `{{ ... }}` in the strings of `value`. A string that is one reference
    becomes the referenced value itself (its type kept); a reference inside a longer string is
    embedded with str() (a dict or list there is an error)."""
    if isinstance(value, str):
        return _substitute_str(value, lookup)
    if isinstance(value, dict):
        return {key: substitute(item, lookup) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, lookup) for item in value]
    return value


def split_ref(expr: str) -> tuple[str, list[Token]]:
    head = _HEAD_RE.match(expr)
    if head is None:
        raise RefsError(f"unknown reference '{{{{ {expr} }}}}'")
    return head.group(0), parse_path(expr[head.end() :])
