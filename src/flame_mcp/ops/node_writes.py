"""The `node` category's writes: create, delete, connect, disconnect, duplicate,
save_setup, load_setup. Every one that changes the project runs behind an automatic checkpoint
(`CheckpointSpec`, `checkpointed.py`): native Undo cannot be relied on in a non-current group
(measured on Flame 2025.2.4: Undo after a disconnect removed a different node instead, and Undo
after a node delete restored nothing), so the way back is `journal.revert` -- a restore of the
checkpoint.
The Flame-side code of each is in `node_prelude.py`; what each op refuses before a checkpoint is
taken is in its `check` (a typo leaves no checkpoint and no journal group behind)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.effective_effect import overwrite_precheck
from flame_mcp.ops._common import build_source
from flame_mcp.ops._node_common import (
    CHECKPOINTED_NODE_NOTE,
    batch_of_node,
    canonical_type,
    group_node_names,
    read_node,
    rebind_node,
    rebind_params,
    refusal_from,
    type_refusal,
)
from flame_mcp.registry import Registry
from flame_mcp.types import (
    CheckpointSpec,
    OperationParam,
    OperationSpec,
    Refusal,
)

NODE_REQUIRES = ("PyBatch.nodes", "PyBatch.uid", "PyNode.attributes")


def _node_param(name: str, description: str) -> OperationParam:
    return OperationParam(name=name, type="selector", description=description, required=True)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _raise_if_error(code: str) -> str:
    return "\n".join(
        [
            "if _result['error'] is not None:",
            f"    raise _OpError(_result.get('errorCode') or {code!r}, _result['error'])",
        ]
    )


def _create_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                f"_result = _create_node(target, {args['type']!r}, {args.get('name')!r})",
                _raise_if_error("SILENT_NOOP"),
            ]
        ),
    )


def _create_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    env = rt.do("node.list_types", {"target": args["target"]})
    if env.get("ok") is not True:
        return refusal_from(env, "the group's node types could not be read")
    types = [str(t) for t in (env.get("result") or {}).get("types") or []]
    canonical, hits = canonical_type(types, str(args["type"]))
    if canonical is None:
        return type_refusal(types, str(args["type"]), hits, "node type", "node.list_types")
    name = args.get("name")
    if name is None:
        return None
    refusal, names = group_node_names(rt, args["target"])
    if refusal is not None:
        return refusal
    if name in names:
        return Refusal(
            "INVALID_ARGS",
            f"nothing was run: the group already has a node named {name!r} (in a non-current "
            "group Flame would silently make a second one)",
            result={"ran": False},
        )
    return None


def _delete_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            ["_result = _delete_node(target__batch, target)", _raise_if_error("SILENT_NOOP")]
        ),
    )


def _delete_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    return read_node(rt, args["target"], "the node")[0]


def _connect_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["source"], "source"),
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                "_result = _connect_nodes(source__batch, source, target__batch, target, "
                f"{args.get('source_socket', 'Default')!r}, "
                f"{args.get('target_socket', 'Default')!r})",
                _raise_if_error("SILENT_NOOP"),
            ]
        ),
    )


def _socket_problem(sockets: Any, side: str, socket: str, node_name: Any) -> str | None:
    if not isinstance(sockets, dict) or socket == "Default":
        return None
    names = sorted((sockets.get(side) or {}).keys())
    if socket in names:
        return None
    return f"{node_name!r} has no {side} socket {socket!r} (it has {names})"


def _connect_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    refusal, source = read_node(rt, args["source"], "the source node")
    if refusal is not None:
        return refusal
    refusal, target = read_node(rt, args["target"], "the target node")
    if refusal is not None:
        return refusal
    source_batch = ((source.get("stable") or {}).get("batch") or {}).get("uid")
    target_batch = ((target.get("stable") or {}).get("batch") or {}).get("uid")
    if source_batch is not None and target_batch is not None and source_batch != target_batch:
        return Refusal(
            "PRECONDITION_UNMET",
            "nothing was run: the source and the target are in different Batch groups; a "
            "connection stays inside one group",
            result={"ran": False, "sourceGroup": source_batch, "targetGroup": target_batch},
        )
    if source.get("name") == target.get("name") and (
        source.get("stable") is not None and source.get("stable") == target.get("stable")
    ):
        return Refusal(
            "INVALID_ARGS",
            f"nothing was run: cannot connect the node {source.get('name')!r} to itself",
            result={"ran": False},
        )
    problem = _socket_problem(
        source.get("sockets"), "output", args.get("source_socket", "Default"), source.get("name")
    ) or _socket_problem(
        target.get("sockets"), "input", args.get("target_socket", "Default"), target.get("name")
    )
    if problem is not None:
        return Refusal(
            "INVALID_ARGS",
            f"nothing was run: {problem}",
            result={
                "ran": False,
                "sourceSockets": source.get("sockets"),
                "targetSockets": target.get("sockets"),
            },
        )
    return None


def _disconnect_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                f"_result = _disconnect_node(target__batch, target, {args.get('socket')!r})",
                _raise_if_error("SILENT_NOOP"),
            ]
        ),
    )


def _disconnect_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    refusal, node = read_node(rt, args["target"], "the node")
    if refusal is not None:
        return refusal
    socket = args.get("socket")
    if socket is None:
        return None
    problem = _socket_problem(node.get("sockets"), "input", socket, node.get("name"))
    if problem is None:
        return None
    return Refusal(
        "INVALID_ARGS",
        f"nothing was run: {problem}",
        result={"ran": False, "sockets": node.get("sockets")},
    )


def _duplicate_to_python(args: Mapping[str, Any]) -> str:
    keep = bool(args.get("keep_connections", False))
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                f"_result = _duplicate_node(target__batch, target, {keep!r})",
                _raise_if_error("SILENT_NOOP"),
            ]
        ),
    )


def _duplicate_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    """Flame duplicates a node only while its group is the current one: refuse before
    a checkpoint is taken when it is not (or cannot be told)."""
    refusal, _node = read_node(rt, args["target"], "the node")
    if refusal is not None:
        return refusal
    env = rt.do("batch.info", {"target": dict(batch_of_node(args["target"]))})
    if env.get("ok") is not True:
        return refusal_from(env, "the group could not be read")
    info = env.get("result") or {}
    if info.get("isCurrent") is True:
        return None
    if info.get("isCurrent") is False:
        why = "the node's Batch group is not the current one"
    else:
        why = "it could not be told whether the node's Batch group is the current one"
    return Refusal(
        "PRECONDITION_UNMET",
        f"nothing was run: {why}. Flame duplicates a node only in the current Batch group "
        "('Nodes need to be in the current batch to be duplicated.'): make the group current in "
        "Flame, then call this again.",
        result={"ran": False, "group": {"name": info.get("name"), "uid": info.get("uid")}},
    )


def _save_setup_to_python(args: Mapping[str, Any]) -> str:
    overwrite = bool(args.get("overwrite", False))
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                "_result = _node_save_setup(target__batch, target, "
                f"{args['path']!r}, {overwrite!r})",
                _raise_if_error("FLAME_API_ERROR"),
            ]
        ),
    )


def _load_setup_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                f"_result = _node_load_setup(target__batch, target, {args['path']!r})",
                _raise_if_error("FLAME_API_ERROR"),
            ]
        ),
    )


def _load_setup_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    path = args.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return Refusal("INVALID_ARGS", f"path must be an absolute path, got {path!r}")
    if not os.path.isfile(path):
        return Refusal(
            "PRECONDITION_UNMET",
            f"nothing was run: there is no setup file at {path!r} on this machine. "
            "node.save_setup returns the file it really wrote (result.path; Flame appends an "
            "extension that depends on the node's type). Flame itself would answer True for a "
            "missing path and do nothing, so it is not called.",
            result={"ran": False},
        )
    return None


def _load_setup_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    return read_node(rt, args["target"], "the node")[0]


def _spec_checkpoint(**kwargs: Any) -> CheckpointSpec:
    return CheckpointSpec(
        group_selector=lambda a: batch_of_node(a["target"]),
        rebind=rebind_params(rebind_node, "target"),
        **kwargs,
    )


def register_writes(registry: Registry) -> None:
    target = _node_param(
        "target",
        "A 'node' selector (batch selector + node name; add pos when several nodes of the "
        "group share the name).",
    )

    registry.register(
        OperationSpec(
            name="node.create",
            category="node",
            description=(
                "Create a node of a given type in a Batch group (PyBatch.create_node), "
                "optionally naming it. The group need not be current. "
                "`type` is matched against node.list_types ignoring case (Flame takes 'Mux' for "
                "the listed 'MUX') and passed on as the listed spelling: result.requestedType and "
                "result.canonicalType. A type nothing matches, or several match, is refused up "
                "front, and so is a `name` another node of the group already has (a "
                "non-current group would silently get two nodes with one name). Flame checks a "
                "new name against the CURRENT group's nodes, so naming can still fail when the "
                "current group holds a node with that name: the node then exists under its "
                "default name (result.createdAs; the error keeps the result) and journal.revert "
                "removes it. The node is read back: result.node = {name, pyType, type, pos, "
                "sameNameCount}, typeAsRequested / typeAsCanonical, addedNames. "
                + CHECKPOINTED_NODE_NOTE
                + "Undo: checkpoint -- "
                "journal.revert removes the new node (the fingerprint sees it)."
            ),
            effect="create",
            undo="checkpoint",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A 'batch' selector (uid or name). The group need not be current.",
                    required=True,
                ),
                OperationParam(
                    name="type",
                    type="string",
                    description="A node type as node.list_types lists it (case does not matter).",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="name",
                    type="string",
                    description="Name for the new node (omit for Flame's default, e.g. blur1).",
                    required=False,
                    non_empty=True,
                ),
            ),
            accepts=("batch",),
            to_python=_create_to_python,
            undo_steps=_no_undo_steps,
            checkpoint=CheckpointSpec(check=_create_check),
            sample_args={
                "target": {"kind": "batch", "name": "sample"},
                "type": "Blur",
                "name": "sample",
            },
            requires=(
                "PyBatch.create_node",
                "PyBatch.node_types",
                *NODE_REQUIRES,
                "PyAttribute.set_value",
            ),
        )
    )

    registry.register(
        OperationSpec(
            name="node.delete",
            category="node",
            description=(
                "Delete a node from its Batch group (PyNode.delete(confirm=False)). Its links "
                "go with it: it disappears from `nodes` and from its neighbours' sockets. "
                "The node is read back (gone from `nodes`); result."
                "wasWiredTo says what it was wired to and staleLinksInNeighbours lists any "
                "neighbour that still names it (staleLinksAmbiguousNames: when another node of "
                "that name remains, its links cannot be told from the deleted node's). "
                + CHECKPOINTED_NODE_NOTE
                + "Undo: checkpoint "
                "-- journal.revert brings the node and its wiring back (a restore, so attribute "
                "values come back as saved at the checkpoint). Undo a delete with journal.revert, "
                "not with Ctrl+Z."
            ),
            effect="destructive",
            undo="checkpoint",
            params=(target,),
            accepts=("node",),
            to_python=_delete_to_python,
            undo_steps=_no_undo_steps,
            checkpoint=_spec_checkpoint(check=_delete_check),
            requires=("PyNode.delete", "PyNode.sockets", *NODE_REQUIRES),
        )
    )

    registry.register(
        OperationSpec(
            name="node.connect",
            category="node",
            description=(
                "Connect an output socket of one node to an input socket of another in the same "
                "Batch group (PyBatch.connect_nodes). Socket 'Default' lets Flame choose. "
                "Refused up front: nodes of different groups (PRECONDITION_UNMET), a node "
                "joined to itself, and a socket the node does not have (INVALID_ARGS; "
                "node.info lists the sockets). The result is read back from both nodes' "
                "`sockets` -- a return value alone is not trusted: no link afterwards is "
                "SILENT_NOOP. result.linkedInputSockets / newLinks / replaced say what "
                "happened. A socket lists the wired nodes by name only: when the names of "
                "both nodes are shared with other nodes of the group, the read-back cannot "
                "tell this link from a sibling's and result.readbackAmbiguous is true "
                "(SILENT_NOOP is decided only when the names are absent). "
                + CHECKPOINTED_NODE_NOTE
                + "UNDO: checkpoint -- journal.revert "
                "removes the link (the fingerprint sees the wiring)."
            ),
            effect="reversible",
            undo="checkpoint",
            params=(
                _node_param("source", "The node whose output is connected ('node' selector)."),
                _node_param("target", "The node whose input is connected ('node' selector)."),
                OperationParam(
                    name="source_socket",
                    type="string",
                    description="Output socket of `source` (default 'Default' = Flame's choice).",
                    required=False,
                    default="Default",
                    non_empty=True,
                ),
                OperationParam(
                    name="target_socket",
                    type="string",
                    description="Input socket of `target` (default 'Default' = Flame's choice).",
                    required=False,
                    default="Default",
                    non_empty=True,
                ),
            ),
            accepts=("node",),
            to_python=_connect_to_python,
            undo_steps=_no_undo_steps,
            checkpoint=CheckpointSpec(
                batch_arg="target",
                group_selector=lambda a: batch_of_node(a["target"]),
                rebind=rebind_params(rebind_node, "source", "target"),
                check=_connect_check,
            ),
            requires=("PyBatch.connect_nodes", "PyNode.sockets", *NODE_REQUIRES),
        )
    )

    registry.register(
        OperationSpec(
            name="node.disconnect",
            category="node",
            description=(
                "Cut the input links of a node (PyBatch.disconnect_node): the named input "
                "socket only, or every input when `socket` is omitted. "
                "A socket the node does not have is refused up front. The node's `sockets` are "
                "read back: a link still there afterwards is SILENT_NOOP. result.disconnected "
                "says what was cut; hadLinks false means there was nothing to cut. "
                + CHECKPOINTED_NODE_NOTE
                + "Undo: checkpoint -- journal.revert puts the links back (the fingerprint "
                "sees the wiring)."
            ),
            effect="reversible",
            undo="checkpoint",
            params=(
                target,
                OperationParam(
                    name="socket",
                    type="string",
                    description="Input socket to cut. Omit to cut every input of the node.",
                    required=False,
                    non_empty=True,
                ),
            ),
            accepts=("node",),
            to_python=_disconnect_to_python,
            undo_steps=_no_undo_steps,
            checkpoint=_spec_checkpoint(check=_disconnect_check),
            requires=("PyBatch.disconnect_node", "PyNode.sockets", *NODE_REQUIRES),
        )
    )

    registry.register(
        OperationSpec(
            name="node.duplicate",
            category="node",
            description=(
                "Duplicate a node (PyNode.duplicate). Only works while the node's Batch group "
                "is the current one: Flame answers 'Nodes need to be in the current batch to be "
                "duplicated.' otherwise. "
                "So for any other group the call ends in PRECONDITION_UNMET before anything "
                "is written or checkpointed. With keep_connections the copy keeps the "
                "original's input links. result.node = {name, pyType, type, pos, sameNameCount} of "
                "the new node (its name is Flame's choice), addedNames, sockets. "
                + CHECKPOINTED_NODE_NOTE
                + "Undo: checkpoint -- journal.revert removes the copy."
            ),
            effect="create",
            undo="checkpoint",
            params=(
                target,
                OperationParam(
                    name="keep_connections",
                    type="boolean",
                    description="Keep the original's input connections on the copy.",
                    required=False,
                    default=False,
                ),
            ),
            accepts=("node",),
            to_python=_duplicate_to_python,
            undo_steps=_no_undo_steps,
            checkpoint=_spec_checkpoint(check=_duplicate_check),
            requires=("PyNode.duplicate", "PyNode.sockets", *NODE_REQUIRES),
        )
    )

    registry.register(
        OperationSpec(
            name="node.save_setup",
            category="node",
            description=(
                "Save a node's setup to a file (PyNode.save_node_setup); nothing in the project "
                "changes. Flame appends an extension that depends on the node's type "
                "(`.exposure_node`), so result.path is the file that really appeared (found by "
                "comparing the directory before and after), with result.extension, written and "
                "overwritten. A node of several parts writes several files in one save (a Clip "
                "node: `.clip_node`, `.clip_node_clip`, `.clip_node_format`, `.lut_node`, "
                "`.resize`, `.resize_node`): result.files lists every file the save wrote and "
                "result.path is the one named after the node type (`.clip_node`); when several "
                "files appear and none of them is that one, the call fails and result.files "
                "still lists them. `path` must be absolute and its directory must exist; a file "
                "already there under that name is refused unless overwrite=true, which "
                "replaces it for good: a call with overwrite=true counts as destructive for "
                "FLAME_MCP_ALLOW_EFFECTS (the op stays effect=create). Pass "
                "result.path to node.load_setup (measured on a Clip node: the load is accepted and "
                "a note changed after the save goes back to its saved value). Undo: none (files "
                "were "
                "written, delete them by hand)."
            ),
            effect="create",
            undo="none",
            params=(
                target,
                OperationParam(
                    name="path",
                    type="string",
                    description="Absolute path (no dots in the name) of the setup to write.",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="overwrite",
                    type="boolean",
                    description="Allow an existing setup file to be overwritten.",
                    required=False,
                    default=False,
                ),
            ),
            accepts=("node",),
            to_python=_save_setup_to_python,
            undo_steps=_no_undo_steps,
            precheck=overwrite_precheck("node.save_setup"),
            requires=("PyNode.save_node_setup", *NODE_REQUIRES),
        )
    )

    registry.register(
        OperationSpec(
            name="node.load_setup",
            category="node",
            description=(
                "Load a node setup file into a node (PyNode.load_node_setup): the node's "
                "settings become those of the file. `path` is the real file "
                "(node.save_setup's result.path), absolute. Flame answers True for a path that "
                "is not there and does nothing, so the file is checked to exist on "
                "this machine (PRECONDITION_UNMET, before anything is checkpointed) and again "
                "inside Flame (load is then not called). Every attribute of the node is read "
                "before and after: result.changed = {key: {before, after}}, changedKeys, and "
                "noChange=true when the load changed nothing (e.g. the node already matched the "
                "file). " + CHECKPOINTED_NODE_NOTE + "Undo: checkpoint -- journal.revert "
                "restores the group as saved before the load (settings included)."
            ),
            effect="reversible",
            undo="checkpoint",
            params=(
                target,
                OperationParam(
                    name="path",
                    type="string",
                    description="Absolute path of an existing node setup file.",
                    required=True,
                    non_empty=True,
                ),
            ),
            accepts=("node",),
            to_python=_load_setup_to_python,
            undo_steps=_no_undo_steps,
            precheck=_load_setup_precheck,
            checkpoint=_spec_checkpoint(check=_load_setup_check),
            requires=("PyNode.load_node_setup", *NODE_REQUIRES),
        )
    )
