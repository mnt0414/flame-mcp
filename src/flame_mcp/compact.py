"""Compact responses: a smaller form of the envelope, chosen once at server start.

FLAME_MCP_RESPONSE=compact (default `full`) makes `server._tool_result` run every envelope
through `compact_envelope` (see `server._compact_within_limit` for how it meets the size limit).
The transform is pure: the input is never modified.

  - outer: items that are None / [] / {} / "" are left out (`affected` and `undoSteps` at 0
    too); `ok` is always kept; `execution` is kept only on a failure; `context` stays
  - `result` (recursively): dict items that are None are left out, and so is a `wiretapId`
    that repeats the id of a wiretap `selector` in the same dict
  - `result` (recursively): a list of 3 or more dicts that flatten to scalars only becomes
    {"columns": [...], "rows": [[...], ...]}; a list of plain values (strings, numbers, bools,
    nulls; [] too) is kept as a cell
  - except under PASSTHROUGH_KEYS, which are copied as they are
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Final, Literal

RESPONSE_ENV: Final[str] = "FLAME_MCP_RESPONSE"
ResponseMode = Literal["full", "compact"]
DEFAULT_RESPONSE_MODE: Final[ResponseMode] = "full"
MIN_TABLE_ROWS: Final[int] = 3
PASSTHROUGH_KEYS: Final[frozenset[str]] = frozenset({"revert", "inverse", "args"})
"""Members of `result` that hold a call to hand back as it is (`revert`, `inverse`: {op, args};
`args`: the arguments a journal record kept). Nothing under them is trimmed, and a list holding
one is never made a table. Found in: the `revert` of desktop.rename and of the *.set_attributes
ops (attribute_prelude `_set_result`) and, inside plan.run steps, of each step's `journal`; the
`inverse` of segment.create_connection / remove_connection; the `args` of the intent records
that journal.show returns. (The `journal` of an envelope is outside `result`, so it is never
trimmed.)"""

INSTRUCTIONS_NOTE: Final[str] = (
    "Responses are compact: missing fields mean null; lists of 3+ alike objects come as "
    "{columns, rows}; build a selector from selector.kind and selector.id (or "
    "{kind: wiretap, id: wiretapId}). Details: flame_context responseFormat."
)

RESPONSE_FORMAT: Final[str] = (
    "Compact responses (FLAME_MCP_RESPONSE=compact). "
    "A field that is null, [], {} or an empty string is not sent: read a missing field as "
    "null; affected and undoSteps are missing when 0. ok is always sent. execution is sent "
    "only when ok is false. "
    "Inside result, a dict item that is null is not sent, and a wiretapId equal to the id of "
    "the wiretap selector next to it is not sent: pass {kind: <selector.kind>, id: "
    "<selector.id>} to the next call. "
    "Inside result, a list of 3 or more objects whose members are all plain values "
    "(nested objects count, as dotted names such as selector.id) is sent as "
    "{columns: [names], rows: [[values in the order of columns], ...]}: row i is the i-th "
    "object, and a null cell is a member that object did not have. A cell can itself be a list "
    "of plain values (e.g. path: ['Lib', 'Reel']): read it as that member's value. Any other "
    "list keeps its form."
)


class ResponseModeConfigError(ValueError):
    """Raised by response_mode_from_env on an unusable FLAME_MCP_RESPONSE (fail closed)."""


def response_mode_from_env(env: Mapping[str, str]) -> ResponseMode:
    raw = env.get(RESPONSE_ENV)
    if raw is None or raw == "full":
        return "full"
    if raw == "compact":
        return "compact"
    raise ResponseModeConfigError(f"{RESPONSE_ENV} must be unset, 'full' or 'compact', got {raw!r}")


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, (list, dict, str)) and len(value) == 0


def _dedupe_wiretap_id(item: dict[str, Any]) -> dict[str, Any]:
    selector = item.get("selector")
    if (
        isinstance(selector, dict)
        and selector.get("kind") == "wiretap"
        and "id" in selector
        and "wiretapId" in item
        and item["wiretapId"] == selector["id"]
    ):
        return {key: value for key, value in item.items() if key != "wiretapId"}
    return item


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _flatten(item: Mapping[str, Any], prefix: str = "") -> dict[str, Any] | None:
    """Nested dicts joined with dots. A list of plain values stays a cell (as a copy). None when
    a list holding a dict or a list, an empty dict, a dotted key or a passthrough key is met."""
    flat: dict[str, Any] = {}
    for key, value in item.items():
        name = str(key)
        if "." in name or name in PASSTHROUGH_KEYS:
            return None
        if isinstance(value, list):
            if not all(_is_scalar(member) for member in value):
                return None
            flat[f"{prefix}{name}"] = list(value)
        elif isinstance(value, dict):
            if not value:
                return None
            inner = _flatten(value, f"{prefix}{name}.")
            if inner is None:
                return None
            flat.update(inner)
        else:
            flat[f"{prefix}{name}"] = value
    return flat


def _table(items: list[Any]) -> dict[str, Any] | None:
    if len(items) < MIN_TABLE_ROWS or not all(isinstance(item, dict) for item in items):
        return None
    flats: list[dict[str, Any]] = []
    for item in items:
        flat = _flatten(item)
        if flat is None:
            return None
        flats.append(flat)
    columns: list[str] = []
    seen: set[str] = set()
    for flat in flats:
        for name in flat:
            if name not in seen:
                seen.add(name)
                columns.append(name)
    if not columns:
        return None
    return {"columns": columns, "rows": [[flat.get(name) for name in columns] for flat in flats]}


def _compact_result(value: Any) -> Any:
    if isinstance(value, dict):
        trimmed = {
            key: copy.deepcopy(sub) if key in PASSTHROUGH_KEYS else _compact_result(sub)
            for key, sub in value.items()
            if sub is not None
        }
        return _dedupe_wiretap_id(trimmed)
    if isinstance(value, list):
        items = [_compact_result(sub) for sub in value]
        table = _table(items)
        return items if table is None else table
    return value


def compact_envelope(env: Mapping[str, Any]) -> dict[str, Any]:
    ok = bool(env.get("ok"))
    out: dict[str, Any] = {}
    for key, value in env.items():
        if key == "ok":
            out[key] = value
        elif key in ("affected", "undoSteps"):
            if value != 0 and not _is_empty(value):
                out[key] = value
        elif key == "execution":
            if not ok and not _is_empty(value):
                out[key] = copy.deepcopy(value)
        elif key == "result":
            compacted = _compact_result(value)
            if not _is_empty(compacted):
                out[key] = compacted
        elif not _is_empty(value):
            out[key] = copy.deepcopy(value)
    return out
