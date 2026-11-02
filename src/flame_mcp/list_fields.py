"""`fields` for the list ops: keep only the named members of each item of the result.

The code that runs in Flame is the same with or without `fields`; `Runtime.call` checks the names
before anything is sent and cuts the items once the op has answered. The tables below are read
from what each op builds (the helper that makes an item, in the prelude, and the op's own code):

  desktop.list_children   `_describe_entry` + `_rp_size_facts` (execution.py, read_prelude.py)
  desktop.list_all        `_ip_item`, which wraps `_rp_find_item` (inspect_prelude.py)
  desktop.find            `_rp_find_item` (read_prelude.py)
  timeline.list_segments  `_describe_segment` / `_describe_audio_segment` and `connections`
                          (ops/timeline.py)

tests/test_list_fields.py runs each op against the fake Flame and fails when an item carries a
member that is not listed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from flame_mcp.types import OperationParam

ERROR_MEMBER: Final[str] = "error"


@dataclass(frozen=True)
class ListFields:
    items_key: str
    fields: tuple[str, ...]


LIST_FIELDS: Final[Mapping[str, ListFields]] = {
    "desktop.list_children": ListFields(
        "items",
        (
            "type",
            "name",
            "uid",
            "wiretapId",
            "duration",
            "frameRate",
            "width",
            "height",
            "bitDepth",
        ),
    ),
    "desktop.list_all": ListFields(
        "items",
        ("type", "name", "uid", "selector", "parent", "path", "location", "library"),
    ),
    "desktop.find": ListFields("items", ("type", "name", "uid", "selector", "parent")),
    "timeline.list_segments": ListFields(
        "items",
        (
            "index",
            "name",
            "sourceName",
            "recordIn",
            "recordOut",
            "sourceIn",
            "sourceOut",
            "recordDuration",
            "type",
            "isGap",
            "head",
            "tail",
            "effects",
            "connections",
        ),
    ),
}


def fields_note() -> str:
    """The sentence an op's description ends with."""
    return (
        "`fields` keeps only the named top-level members of each item (an unknown name is "
        "INVALID_ARGS); an item that could not be read keeps its `error`."
    )


def fields_param(op_name: str) -> OperationParam:
    spec = LIST_FIELDS[op_name]
    return OperationParam(
        name="fields",
        type="array",
        description=(
            f"Members of each item to keep, e.g. ['name', 'uid']. Omit (or pass []) to keep "
            f"every member. One of: {', '.join(spec.fields)}. An item that could not be read "
            f"keeps its `{ERROR_MEMBER}`."
        ),
        required=False,
        item_type="string",
    )


def unknown_fields_message(op_name: str, args: Mapping[str, Any]) -> str | None:
    """The INVALID_ARGS message for names in `args['fields']` that the op's items never carry."""
    spec = LIST_FIELDS.get(op_name)
    wanted = args.get("fields")
    if spec is None or not wanted:
        return None
    unknown = [name for name in wanted if name not in spec.fields]
    if not unknown:
        return None
    return (
        f"invalid arguments: fields: unknown member(s) {', '.join(repr(n) for n in unknown)}; "
        f"known: {', '.join(spec.fields)}"
    )


def select_fields(op_name: str, args: Mapping[str, Any], env: dict[str, Any]) -> None:
    """Cuts the items of a successful list result to `args['fields']`, in place."""
    spec = LIST_FIELDS.get(op_name)
    wanted = args.get("fields")
    if spec is None or not wanted or not env.get("ok"):
        return
    result = env.get("result")
    if not isinstance(result, dict) or not isinstance(result.get(spec.items_key), list):
        return
    keep = set(wanted) | {ERROR_MEMBER}
    items = [
        {key: value for key, value in item.items() if key in keep}
        if isinstance(item, dict)
        else item
        for item in result[spec.items_key]
    ]
    env["result"] = {**result, spec.items_key: items}
