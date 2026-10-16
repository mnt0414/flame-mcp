"""`action` category: the inner nodes of an Action / Image / GMask Tracer node of a Batch
group. The Action node is addressed by a `node` selector, an inner node by an `action_node`
selector (the Action node's selector plus the inner node's name). Inner nodes are
created with `create_node` and linked with `connect_nodes`; no delete API for PyCoNode was found,
so no op removes an inner node -- a `journal.revert` of a create restores the group from its
checkpoint instead.

Writes run behind an automatic checkpoint (`CheckpointSpec`, `checkpointed.py`); `get_attributes` /
`set_attributes` are the generic attribute ops (`ops/_attributes.py`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.node_prelude import ACTION_LINK_TYPES
from flame_mcp.ops._attributes import attribute_requires, register_attribute_ops
from flame_mcp.ops._common import build_source
from flame_mcp.ops._node_common import (
    CHECKPOINTED_NODE_NOTE,
    batch_of_action_node,
    batch_of_node,
    canonical_type,
    read_action,
    read_inner,
    rebind_action_node,
    rebind_node,
    rebind_params,
    type_refusal,
)
from flame_mcp.registry import Registry
from flame_mcp.types import (
    Category,
    CheckpointSpec,
    OperationParam,
    OperationSpec,
    Refusal,
)

CATEGORY = Category(
    name="action",
    description=(
        "Inner nodes of Action / Image / GMask Tracer nodes (the Action node is addressed with "
        "the 'node' selector, an inner node with 'action_node'). The group need not be current."
    ),
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)

ACTION_NODE_KINDS = "Action, Image or GMask Tracer"
INNER_REQUIRES = (
    "PyActionNode.nodes",
    "PyActionNode.node_types",
    "PyBatch.nodes",
    "PyBatch.uid",
    "PyNode.attributes",
)
DEFAULT_LINK_TYPE = "Default"


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _raise_if_error(code: str) -> str:
    return "\n".join(
        [
            "if _result['error'] is not None:",
            f"    raise _OpError(_result.get('errorCode') or {code!r}, _result['error'])",
        ]
    )


def _list_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "_result = _action_list(target__batch, target)",
    )


def _create_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                f"_result = _action_create_node(target, {args['type']!r})",
                _raise_if_error("SILENT_NOOP"),
            ]
        ),
    )


def _create_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    refusal, listing = read_action(rt, args["target"], "the Action node")
    if refusal is not None:
        return refusal
    types = [str(t) for t in listing.get("nodeTypes") or []]
    if not types:
        return None  # an unreadable list is left to the Flame side, which checks again
    canonical, hits = canonical_type(types, str(args["type"]))
    if canonical is not None:
        return None
    return type_refusal(types, str(args["type"]), hits, "inner node type", "action.list_nodes")


def _connect_to_python(args: Mapping[str, Any]) -> str:
    link = args.get("link_type", DEFAULT_LINK_TYPE)
    return build_source(
        selectors.resolve_code(args["parent"], "parent"),
        selectors.resolve_code(args["child"], "child"),
        "\n".join(
            [
                "_result = _action_connect(parent__node__batch, parent__node, parent, "
                f"child__node__batch, child__node, child, {link!r})",
                _raise_if_error("SILENT_NOOP"),
            ]
        ),
    )


def _connect_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    link = args.get("link_type", DEFAULT_LINK_TYPE)
    if isinstance(link, str) and link.lower() in ACTION_LINK_TYPES:
        return None
    return Refusal(
        "INVALID_ARGS",
        f"link_type must be one of {list(ACTION_LINK_TYPES)} (any case), got {link!r}",
    )


def _connect_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    refusal, parent = read_inner(rt, args["parent"], "the parent inner node")
    if refusal is not None:
        return refusal
    refusal, child = read_inner(rt, args["child"], "the child inner node")
    if refusal is not None:
        return refusal
    parent_action = (parent.get("stable") or {}).get("node")
    child_action = (child.get("stable") or {}).get("node")
    if parent_action is not None and child_action is not None and parent_action != child_action:
        return Refusal(
            "PRECONDITION_UNMET",
            "nothing was run: the parent and the child are inner nodes of different Action "
            "nodes; a link stays inside one Action",
            result={"ran": False, "parentAction": parent_action, "childAction": child_action},
        )
    if args["parent"]["name"] == args["child"]["name"] and parent_action == child_action:
        return Refusal(
            "INVALID_ARGS",
            f"nothing was run: cannot connect the inner node {args['parent']['name']!r} to itself",
            result={"ran": False},
        )
    return None


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)
    target = OperationParam(
        name="target",
        type="selector",
        description=f"A 'node' selector for the {ACTION_NODE_KINDS} node.",
        required=True,
    )

    registry.register(
        OperationSpec(
            name="action.list_nodes",
            category="action",
            description=(
                f"Describe an {ACTION_NODE_KINDS} node's inside: `nodes` (every inner node's "
                "name, pyType and type), `nodeTypes` (what action.create_node can make), "
                "`mediaLayers` and `outputTypes`. Any other kind of node is SELECTOR_TYPE. "
                "A default Action holds Default and Perspective; `unreadable` lists a field "
                "this kind of node does not have (an Image node has no output types)."
            ),
            effect="read",
            params=(target,),
            accepts=("node",),
            to_python=_list_to_python,
            requires=(
                "PyActionNode.nodes",
                "PyActionNode.node_types",
                "PyActionNode.media_layers",
                "PyActionNode.output_types",
                "PyBatch.nodes",
            ),
        )
    )

    registry.register(
        OperationSpec(
            name="action.create_node",
            category="action",
            description=(
                f"Create an inner node in an {ACTION_NODE_KINDS} node (PyActionNode."
                "create_node). `type` is matched against action.list_nodes' nodeTypes "
                "ignoring case and passed on as the listed spelling (result.requestedType and "
                "result.canonicalType); no match, or several, is refused up front. Flame can "
                "add more than the requested node: a 3D Path comes with an `axis`. The inner nodes "
                "are read back: result.created is the "
                "node the call returned, result.added every new inner node, alsoAdded the "
                "ones Flame added on its own, nodesBefore / nodesAfter the names. This server "
                "has no op to delete an inner node: journal.revert removes "
                "what this call added. " + CHECKPOINTED_NODE_NOTE + "Undo: checkpoint -- "
                "journal.revert removes the new inner nodes (the fingerprint sees inner node "
                "names and types)."
            ),
            effect="create",
            undo="checkpoint",
            params=(
                target,
                OperationParam(
                    name="type",
                    type="string",
                    description="An inner node type from action.list_nodes' nodeTypes.",
                    required=True,
                    non_empty=True,
                ),
            ),
            accepts=("node",),
            to_python=_create_to_python,
            undo_steps=_no_undo_steps,
            checkpoint=CheckpointSpec(
                group_selector=lambda a: batch_of_node(a["target"]),
                rebind=rebind_params(rebind_node, "target"),
                check=_create_check,
            ),
            requires=("PyActionNode.create_node", *INNER_REQUIRES),
        )
    )

    registry.register(
        OperationSpec(
            name="action.connect",
            category="action",
            description=(
                "Link two inner nodes of the same Action node (PyActionNode.connect_nodes): "
                "`child` becomes a child of `parent`. link_type is one of default, look at, "
                "gmask, gmask exclusive, light, light exclusive, mimic (from Flame's own "
                "docstring; checked up front by this server, ignoring case); 'Default' is the "
                "default. Inner nodes "
                "of different Action nodes are PRECONDITION_UNMET, before anything is "
                "checkpointed. The link is read back by CALLING the child's parents(...) and the "
                "parent's children(...) (methods on 2025, not attributes): first with the "
                "link_type given, then with no argument; every call's outcome, or its raw "
                "exception text, is in result.before / result.after and the calls that show the "
                "link in result.linkedBy. No link in any call that worked is SILENT_NOOP "
                "(result.linked); readBackAvailable false means no call worked, and then "
                "nothing is claimed. "
                + CHECKPOINTED_NODE_NOTE
                + "UNDO: checkpoint -- journal.revert restores the group as it was, links "
                "included. `changed` reads false: the fingerprint does not see links between "
                "inner nodes."
            ),
            effect="reversible",
            undo="checkpoint",
            params=(
                OperationParam(
                    name="parent",
                    type="selector",
                    description="An 'action_node' selector: the parent inner node.",
                    required=True,
                ),
                OperationParam(
                    name="child",
                    type="selector",
                    description="An 'action_node' selector: the child inner node.",
                    required=True,
                ),
                OperationParam(
                    name="link_type",
                    type="string",
                    description=(
                        "default, look at, gmask, gmask exclusive, light, light exclusive or "
                        "mimic (matched ignoring case by this server). Default: 'Default'."
                    ),
                    required=False,
                    default=DEFAULT_LINK_TYPE,
                    non_empty=True,
                ),
            ),
            accepts=("action_node",),
            to_python=_connect_to_python,
            undo_steps=_no_undo_steps,
            precheck=_connect_precheck,
            checkpoint=CheckpointSpec(
                batch_arg="parent",
                group_selector=lambda a: batch_of_action_node(a["parent"]),
                rebind=rebind_params(rebind_action_node, "parent", "child"),
                check=_connect_check,
            ),
            requires=(
                "PyActionNode.connect_nodes",
                "PyCoNode.parents",
                "PyCoNode.children",
                *INNER_REQUIRES,
            ),
        )
    )

    register_attribute_ops(
        registry,
        category="action",
        noun="node inside an Action node",
        accepts=("action_node",),
        target_description=(
            "An 'action_node' selector: the Action node's 'node' selector plus the inner node's "
            "name. The group need not be current."
        ),
        stable_src=lambda _args: (
            "_stable = _stable_action_node(target, target__node, target__node__batch)"
        ),
        revert_kinds=("action_node",),
        group_var="target__node",
        note=(
            "Inner node names: writing `name` is refused up front when another inner node of "
            "the same Action already has that name, so the `action_node` selector stays "
            "unambiguous. Inner nodes of an Action are not on the fingerprint's attribute "
            "side: journal.revert here puts the previous VALUES back (no checkpoint)."
        ),
        requires=attribute_requires(
            "PyCoNode.attributes", "PyActionNode.nodes", "PyBatch.nodes", "PyBatch.uid"
        ),
    )
