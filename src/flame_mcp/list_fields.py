"""`fields` for the list ops: keep only the named members of each item of the result (for
`timeline.list_segments` with `all_tracks=true`, of the items of each entry of `tracks`).

The code that runs in Flame is the same with or without `fields`; `Runtime.call` checks the names
before anything is sent and cuts the items once the op has answered. Each op declares its
`ListFields` (`OperationSpec.list_fields`), read from what the op builds (the helper that makes an
item, in the prelude, and the op's own code):

  desktop.list_children   `_describe_entry` + `_rp_size_facts` (execution.py, read_prelude.py)
  desktop.list_all        `_ip_item`, which wraps `_rp_find_item` (inspect_prelude.py)
  desktop.find            `_rp_find_item` (read_prelude.py)
  timeline.list_segments  `_describe_segment` / `_describe_audio_segment` and `connections`
                          (ops/timeline.py)

tests/test_list_fields.py runs each op against the fake Flame and fails when an item carries a
member that is not listed in its declaration.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp.types import ListFields, OperationParam

ERROR_MEMBER: Final[str] = "error"


def fields_note() -> str:
    """The sentence an op's description ends with."""
    return (
        "`fields` keeps only the named top-level members of each item (an unknown name is "
        "INVALID_ARGS); an item that could not be read keeps its `error`."
    )


def fields_param(spec: ListFields) -> OperationParam:
    return OperationParam(
        name="fields",
        type="array",
        description=(
            f"Members of each item to keep, one of: {', '.join(spec.fields)}. "
            f"E.g. ['name', 'uid']. Omit (or pass []) to keep every member. An item that could "
            f"not be read keeps its `{ERROR_MEMBER}`."
        ),
        required=False,
        item_type="string",
    )


def unknown_fields_message(spec: ListFields | None, args: Mapping[str, Any]) -> str | None:
    """The INVALID_ARGS message for names in `args['fields']` that the op's items never carry."""
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


def select_fields(spec: ListFields | None, args: Mapping[str, Any], env: dict[str, Any]) -> None:
    """Cuts the items of a successful list result to `args['fields']`, in place."""
    wanted = args.get("fields")
    if spec is None or not wanted or not env.get("ok"):
        return
    result = env.get("result")
    if not isinstance(result, dict):
        return
    keep = set(wanted) | {ERROR_MEMBER}

    def cut(items: list[Any]) -> list[Any]:
        return [
            {key: value for key, value in item.items() if key in keep}
            if isinstance(item, dict)
            else item
            for item in items
        ]

    if isinstance(result.get(spec.items_key), list):
        env["result"] = {**result, spec.items_key: cut(result[spec.items_key])}
    elif spec.groups_key is not None and isinstance(result.get(spec.groups_key), list):
        groups = [
            {**group, spec.items_key: cut(group[spec.items_key])}
            if isinstance(group, dict) and isinstance(group.get(spec.items_key), list)
            else group
            for group in result[spec.groups_key]
        ]
        env["result"] = {**result, spec.groups_key: groups}
