"""`node` category: nodes of a Batch group, addressed by a `node` selector (a
`batch` selector plus the node's name). Measured on Flame 2025.2.4: all 97 node types
can be created in a non-current group, and the attributes of the 32 types with their own
keys are readable through the generic attribute ops."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.ops._attributes import attribute_requires, register_attribute_ops
from flame_mcp.ops._common import build_source, list_result_src, offset_param
from flame_mcp.ops.node_writes import register_writes
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationParam, OperationSpec

CATEGORY = Category(
    name="node",
    description=(
        "Nodes of a Batch group (the 'node' selector = a 'batch' selector + the node name, plus "
        "pos when several nodes of the group share the name). "
        "The group need not be current (node.duplicate works only in the current group)."
    ),
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)


def _list_to_python(args: Mapping[str, Any]) -> str:
    limit = args["limit"]
    offset = args["offset"]
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                "def _describe_listed_node(_n):",
                "    def _try(_key):",
                "        try:",
                "            return _ja(getattr(_n, _key))",
                "        except BaseException:",
                "            return None",
                "    return {",
                "        'name': _v(_n.name),",
                "        'pyType': type(_n).__name__,",
                "        'type': _try('type'),",
                "        'posX': _try('pos_x'),",
                "        'posY': _try('pos_y'),",
                "        'pos': _node_pos(_n),",
                "        'sameNameCount': _counts.get(_v(_n.name), 1),",
                "        'bypass': _try('bypass'),",
                "    }",
                "_nodes = list(target.nodes)",
                "_counts = _name_counts([_v(_n.name) for _n in _nodes])",
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_nodes)",
                "_items = [_describe_listed_node(_n) for _n in _nodes[_offset : _offset + _limit]]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
    )


def _list_types_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "_types = _node_types(target)\n_result = {'count': len(_types), 'types': _types}",
    )


def _info_to_python(args: Mapping[str, Any]) -> str:
    include_values = bool(args.get("include_values", False))
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                f"_result = _node_info(target__batch, target, {include_values!r})",
                "_result['stable'] = _stable_node(target, target__batch)",
            ]
        ),
    )


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)

    registry.register(
        OperationSpec(
            name="node.list_types",
            category="node",
            description=(
                "List every node type this Flame can create in a Batch group "
                "(PyBatch.node_types): result = {count, types}. "
                "The names are the `type` argument of node.create, exactly as spelled here."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A 'batch' selector for the group.",
                    required=True,
                ),
            ),
            accepts=("batch",),
            to_python=_list_types_to_python,
            requires=("PyBatch.node_types",),
        )
    )

    registry.register(
        OperationSpec(
            name="node.info",
            category="node",
            description=(
                "Describe one node: name, pyType (the Flame class), type, every attribute's "
                "value (with include_values also its allowed values / range), and its "
                "`sockets` = {input: {socket: [names wired to it]}, output: {...}}; it names "
                "the wired nodes by name only, and `ambiguousSocketNames` lists the names in "
                "`sockets` that match more than one node of the group (the API does not say "
                "which of those nodes is wired; [] when none, null when the sockets cannot be "
                "read). "
                "A Clip node adds `clip` (name, uid, mediaPath, duration, resolution); a Write "
                "File node adds `render` (resolvedMediaPath, rangeStart, rangeEnd) and a Render "
                "node "
                "adds `render` with rangeStart and rangeEnd only (its resolvedMediaPath is "
                "always null: a Render node has no path); an "
                "Action / Image / GMask Tracer node adds innerNodeCount. `stable` is a selector "
                "that keeps addressing the node; `unreadable` lists what could not be read."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description=(
                        "A 'node' selector (batch selector + node name; add pos when several "
                        "nodes of the group share the name)."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="include_values",
                    type="boolean",
                    description="Also return each attribute's `values` (allowed values / range).",
                    required=False,
                    default=False,
                ),
            ),
            accepts=("node",),
            to_python=_info_to_python,
            requires=attribute_requires(
                "PyNode.sockets",
                "PyNode.attributes",
                "PyBatch.nodes",
                "PyBatch.uid",
                "PyClipNode.clip",
                "PyClipNode.media_path",
                "PyClipNode.duration",
                "PyClipNode.resolution",
                "PyResolution.width",
                "PyResolution.height",
                "PyWriteFileNode.get_resolved_media_path",
                "PyActionNode.nodes",
            ),
        )
    )

    register_writes(registry)

    registry.register(
        OperationSpec(
            name="node.list",
            category="node",
            description=(
                "List the nodes of a Batch group, one page at a time. Each row has name, "
                "pyType (the Flame class, e.g. PyActionNode), type (the node's `type` "
                "attribute), posX / posY, pos ([posX, posY], null when it cannot be read), "
                "sameNameCount (how many nodes of the group have this name) and bypass. "
                "Address a node "
                "afterwards with {'kind': 'node', 'batch': <batch selector>, 'name': ...}, "
                "adding 'pos': [x, y] when sameNameCount is above 1."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A 'batch' selector for the group.",
                    required=True,
                ),
                OperationParam(
                    name="limit", type="number", description="Max items to return.", required=False
                ),
                offset_param(),
            ),
            accepts=("batch",),
            to_python=_list_to_python,
            requires=("PyBatch.nodes", "PyNode.attributes"),
        )
    )

    register_attribute_ops(
        registry,
        category="node",
        noun="Batch node",
        accepts=("node",),
        target_description=(
            "A 'node' selector: a 'batch' selector plus the node's name (and pos when "
            "several nodes of the group share the name). The group need not be current."
        ),
        stable_src=lambda _args: "_stable = _stable_node(target, target__batch)",
        revert_kinds=("node",),
        group_var="target__batch",
        note=(
            "Node names: writing `name` is refused up front when another node of the same "
            "group already has that name. Flame itself checks a name against the current "
            "Batch group, not the node's own group, so renaming a node of a "
            "non-current group -- and reverting such a rename -- fails "
            "('name is already being used') when the current group holds a node with that name. "
            "A revert that puts back a name another node of the group has is not refused by "
            "this server, but Flame refused it in every case measured (Clip nodes, current and "
            "non-current group): renaming one of several nodes that share a name may not be "
            "undoable with journal.revert."
        ),
        requires=attribute_requires("PyNode.attributes", "PyBatch.nodes", "PyBatch.uid"),
    )
