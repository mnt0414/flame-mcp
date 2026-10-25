"""`node.edit_setup`: replace the value of tags in a Batch node's setup file and load it back.

A write behind a Batch checkpoint (`CheckpointSpec`), like `node.load_setup`; the Flame side is
`setup_edit_prelude` (`_se_node_edit`, `_se_node_edit_check`) and the text functions are
`setup_edit`. The check phase (`ops/_segment_edit.make_check`) saves the setup and counts the tags
before the group is checkpointed."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._node_common import (
    CHECKPOINTED_NODE_NOTE,
    batch_of_node,
    rebind_node,
    rebind_params,
)
from flame_mcp.ops._segment_edit import PHASE_ARG, PHASE_CHECK, make_check
from flame_mcp.ops._setup_edit import (
    EDIT_NOTE,
    keep_files_param,
    values_param,
    values_precheck,
    work_root,
)
from flame_mcp.ops.node_writes import NODE_REQUIRES
from flame_mcp.registry import Registry
from flame_mcp.types import CheckpointSpec, OperationParam, OperationSpec

EDIT_OP: Final[str] = "node.edit_setup"

_DESCRIPTION = (
    "Replace the value of tags in a Batch node's setup file and load the file back "
    "(PyNode.save_node_setup, then load_node_setup). The group need not be current. Example: a "
    "Timewarp node, values {'TW_FrameMode': 0}. "
    + EDIT_NOTE
    + " A node whose setup is several files (a Clip node saves six) is refused. After the load the "
    "node is found again in its group to save the read-back. result: node, tags, tagCounts, "
    "unchanged, mismatched, files {original, edited, readback}, filesKept, loadReturned, "
    "changedKeys, noChange, otherDifferences. "
    + CHECKPOINTED_NODE_NOTE
    + "Undo: checkpoint -- journal.revert restores the group as saved before the edit (settings "
    "included)."
)


def _to_python(args: Mapping[str, Any]) -> str:
    values, keep, base = dict(args["values"]), bool(args.get("keep_files", False)), work_root()
    resolve = selectors.resolve_code(args["target"], "target")
    if args.get(PHASE_ARG) == PHASE_CHECK:
        return build_source(
            resolve,
            f"_result = _se_node_edit_check(target__batch, target, {values!r}, {keep!r}, {base!r})",
            raise_on_error_src(),
            "_affected = 0",
        )
    return build_source(
        resolve,
        f"_result = _se_node_edit(target__batch, target, {values!r}, {keep!r}, {base!r})",
        raise_on_error_src(),
        "_affected = 1",
    )


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=EDIT_OP,
            category="node",
            description=_DESCRIPTION,
            effect="reversible",
            undo="checkpoint",
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
                values_param("node"),
                keep_files_param(),
            ),
            accepts=("node",),
            to_python=_to_python,
            undo_steps=_no_undo_steps,
            precheck=values_precheck,
            checkpoint=CheckpointSpec(
                group_selector=lambda a: batch_of_node(a["target"]),
                rebind=rebind_params(rebind_node, "target"),
                check=make_check(EDIT_OP),
            ),
            requires=("PyNode.save_node_setup", "PyNode.load_node_setup", *NODE_REQUIRES),
        )
    )
