"""`desktop.list_all`: one read that walks the Desktop and every library of the project (the walk
itself is `_ip_walk` in `inspect_prelude.py`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp.inspect_prelude import INSPECT_BUDGET_S
from flame_mcp.list_fields import fields_note, fields_param
from flame_mcp.ops._common import build_source, limit_param, list_result_src, offset_param
from flame_mcp.registry import Registry
from flame_mcp.types import OperationParam, OperationSpec

SCOPES: tuple[str, ...] = ("all", "desktop", "libraries")

OFFSET_BEYOND_HINT = (
    "The walk of this call stopped before `offset`: it stopped earlier than the walk of the call "
    "that gave that offset. Narrow the walk with scope or types."
)

_DESCRIPTION = (
    "List what the Desktop and the libraries hold, in a fixed order, one page at a time. "
    "The order: each Desktop reel group with its reels and their clips and sequences, then the "
    "Desktop's Batch groups, then each library with its folders (to any depth), reels, clips and "
    "sequences; a container comes before what it holds. "
    "`types` keeps only the named Flame types (e.g. ['PySequence']; `total` counts after it) and "
    "the walk goes through the other containers all the same. `scope` walks 'desktop' or "
    "'libraries' only (default 'all'). "
    "Each item has the shape of desktop.find (type, name, uid, `selector`, `parent`) plus `path` "
    "(names from the reel group or the library down to the item), `location` ('desktop' or "
    "'library') and `library` (its name, null on the Desktop). An entry that could not be read "
    "is an item {error, path, location, library}, and the walk goes on. "
    "A Batch group is listed by name and uid only; nothing inside it is read, and "
    "`batchGroupsSkipped` counts the Batch groups the walk met (also those `types` dropped). A "
    "library that is closed, or whose open state cannot be read, is listed but never opened, and "
    "nothing of what it holds is listed: `closedLibraries` names them. To look "
    "inside one, give desktop.list_children a selector of that library (kind 'path', path "
    "[name]); that call opens it and closes it again. "
    "The walk reads containers for up to a time budget (the first container is always read). "
    "Past the budget the walk stops: `stoppedEarly` {reason, budgetS, read: containers read} "
    "is set, `truncated` is true, `total` counts only what was reached, and `nextOffset` is "
    "set while listed items remain after this page. A walk starts again from the beginning "
    "on every call, so narrow a slow project with `scope` or `types`. If `offset` is at or "
    "past `total` and the walk stopped early, the walk of this call stopped before `offset` "
    "(earlier than the walk of the call that gave it): `offsetBeyondReached` is true and "
    "`hint` says to narrow the walk. A page larger than the response size limit "
    "comes back with `items` collapsed to {count, omitted}: keep `limit` at 100 or less. "
    + fields_note()
)


def _to_python(args: Mapping[str, Any]) -> str:
    types = list(args.get("types") or [])
    return build_source(
        "\n".join(
            [
                f"_walk = _ip_walk({args['scope']!r}, {types!r}, {INSPECT_BUDGET_S!r})",
                "_entries = _walk['entries']",
                f"_limit = {args['limit']!r}",
                f"_offset = {args['offset']!r}",
                "_total = len(_entries)",
                "_items = [_ip_item(_e) for _e in _entries[_offset : _offset + _limit]]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
        "\n".join(
            [
                "_result['closedLibraries'] = _walk['closedLibraries']",
                "_result['batchGroupsSkipped'] = _walk['batchGroupsSkipped']",
                "if _walk['stoppedEarly'] is not None:",
                "    _result['truncated'] = True",
                "    _result['stoppedEarly'] = _walk['stoppedEarly']",
                "    if _offset + len(_items) < _total:",
                "        _result['nextOffset'] = _offset + len(_items)",
                "    elif _offset >= _total:",
                "        _result['offsetBeyondReached'] = True",
                f"        _result['hint'] = {OFFSET_BEYOND_HINT!r}",
            ]
        ),
    )


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name="desktop.list_all",
            category="desktop",
            description=_DESCRIPTION,
            effect="read",
            params=(
                OperationParam(
                    name="types",
                    type="array",
                    description=(
                        "Flame type names to keep, e.g. ['PyClip', 'PySequence']. "
                        "Omit (or pass []) to keep every type."
                    ),
                    required=False,
                    item_type="string",
                ),
                OperationParam(
                    name="scope",
                    type="string",
                    description=(
                        "'all' (the Desktop, then the libraries), 'desktop' or 'libraries' "
                        "(default 'all')."
                    ),
                    required=False,
                    default="all",
                    enum=SCOPES,
                ),
                limit_param(),
                offset_param(),
                fields_param("desktop.list_all"),
            ),
            to_python=_to_python,
            requires=(
                "flame.projects",
                "PyProjectSelector.current_project",
                "PyProject.current_workspace",
                "PyWorkspace.libraries",
                "PyWorkspace.desktop",
                "PyDesktop.reel_groups",
                "PyDesktop.batch_groups",
                "PyReelGroup.reels",
                "PyReel.children",
                "PyLibrary.children",
                "PyLibrary.opened",
                "PyFolder.children",
                "PyClip.get_wiretap_node_id",
                "PySequence.get_wiretap_node_id",
                "PyReelGroup.parent",
                "PyReel.parent",
                "PyFolder.parent",
                "PyLibrary.parent",
                "PyClip.parent",
                "PySequence.parent",
                "PyBatch.parent",
                "PyBatch.name",
                "PyBatch.uid",
            ),
        )
    )
