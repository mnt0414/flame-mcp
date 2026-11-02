"""`batch.delete`: delete a Batch group after saving its setup. The Flame side is
`batch_delete_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.batch_delete_run import PHASE_ALIVE, PHASE_SAVE, run_batch_delete
from flame_mcp.desktop_run import PHASE_ARG, PHASE_READ, PHASE_WRITE
from flame_mcp.ops._common import build_source
from flame_mcp.registry import Registry
from flame_mcp.setup_work import work_root
from flame_mcp.types import OperationParam, OperationSpec

DELETE_DIR_NAME = "batch_delete"

_DESCRIPTION = (
    "Delete a Batch group of the Desktop (flame.delete, confirm=False) after saving its setup. "
    "The setup is saved first into the work directory of this server (result.setupPath, a "
    ".batch file made private; it is in every answer that got as far as the save); when it "
    "cannot be saved nothing is deleted. Refused before anything is saved (PRECONDITION_UNMET, "
    "result holds what was read): a group that is not a Batch group of the Desktop, the only "
    "Batch group left, the current group or an open one -- also when it could not be read "
    "whether it is -- unless allow_current=true (deleting the current group moves current to "
    "another group that cannot be chosen), and a group whose reels or iterations hold anything "
    "(result.content: schematicReelEntries, shelfReelEntries, iterations; also when they could "
    "not be read) unless allow_content_loss=true, because a saved setup does not bring those "
    "back. expect_name: when given, a group with another name is CONFLICT and nothing is "
    "deleted (select by uid and pass the name you were shown). The group is judged again right "
    "before each of the save and the delete. result: name, uid, nodeCount, content, wasCurrent, "
    "setupPath, setupSize, deleted, flameReturned, warnings. A delete that raised in Flame but "
    "left the group gone is ok with the exception in `warnings` and `degraded`. A group still "
    "there afterwards is SILENT_NOOP (the setup stays saved). "
    "JOURNAL: an intent (kind batch_delete: user and process of this server, time, uid, name, "
    "arguments, setupPath) is written before the delete and an outcome after it; envelope "
    "journal = {group, id, undo: 'unsupported', revert: null}. After TIMEOUT / BRIDGE_LOST this "
    "server refuses writes until journal.check has looked for the uid on the Desktop (gone = "
    "landed). Undo: none -- journal.revert has nothing to restore; create a Batch group and "
    "batch.load_setup the saved file, which brings back nodes and wiring and not what "
    "result.content counts. Ask the user to confirm before calling."
)


def _phase_call(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_READ)
    if phase == PHASE_ALIVE:
        return f"_result = _bd_alive({args['target'].get('uid')!r})"
    allow = (
        bool(args.get("allow_current")),
        bool(args.get("allow_content_loss")),
        args.get("expect_name"),
    )
    if phase == PHASE_SAVE:
        directory = f"{work_root()}/{DELETE_DIR_NAME}"
        return (
            f"_result = _bd_save(target, {directory!r}, {allow[0]!r}, {allow[1]!r}, {allow[2]!r})"
        )
    if phase == PHASE_WRITE:
        return (
            f"_result = _bd_delete(target, {allow[0]!r}, {allow[1]!r}, {allow[2]!r}, "
            f"{args.get('setup_path')!r})"
        )
    return f"_result = _bd_inspect(target, {allow[0]!r}, {allow[1]!r}, {allow[2]!r})"


def _to_python(args: Mapping[str, Any]) -> str:
    if args.get(PHASE_ARG) == PHASE_ALIVE:
        return build_source(_phase_call(args), "_affected = 0")
    return build_source(
        selectors.resolve_code(args["target"], "target"), _phase_call(args), "_affected = 0"
    )


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name="batch.delete",
            category="batch",
            description=_DESCRIPTION,
            effect="destructive",
            undo="none",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A 'batch' selector (uid or name) of the group to delete.",
                    required=True,
                ),
                OperationParam(
                    name="expect_name",
                    type="string",
                    description="The name the group has; another name leaves it alone.",
                    required=False,
                    non_empty=True,
                ),
                OperationParam(
                    name="allow_content_loss",
                    type="boolean",
                    description="Also delete a group whose reels or iterations hold anything.",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="allow_current",
                    type="boolean",
                    description="Also delete the current group or an open one.",
                    required=False,
                    default=False,
                ),
            ),
            accepts=("batch",),
            to_python=_to_python,
            wrap=run_batch_delete,
            undo_steps=lambda args, affected: 0,
            sample_args={"target": {"kind": "batch", "uid": "u"}},
            requires=(
                "flame.delete",
                "PyBatch.save_setup",
                "PyBatch.uid",
                "PyBatch.name",
                "PyBatch.nodes",
                "PyBatch.opened",
                "PyBatch.reels",
                "PyBatch.shelf_reels",
                "PyBatch.batch_iterations",
                "PyReel.clips",
                "PyReel.sequences",
                "PyDesktop.batch_groups",
                "PyDesktop.current_batch_group",
            ),
        )
    )
