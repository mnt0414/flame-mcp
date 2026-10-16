"""`render` category: what a Batch group's Write File / Render nodes would write
(`render.plan`) and the render itself (`render.batch`). Observed on Flame 2025.2.4: a render works
on a group that is not current, writes as many frames as a Write File's range says (default 1-100)
and occupies Flame while it runs (Foreground; the bridge may not answer meanwhile). By design
render.batch provides no undo. The server-side halves are in `render_plan.py` and
`render_run.py`, the Flame side in `render_prelude.py`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.effective_effect import deny_destructive_call
from flame_mcp.ops._common import build_source
from flame_mcp.registry import Registry
from flame_mcp.render_plan import DEFAULT_MAX_FRAMES, run_plan
from flame_mcp.render_run import (
    DEFAULT_TIMEOUT_S,
    MAX_FRAMES_LIMIT,
    MAX_TIMEOUT_S,
    run_render,
)
from flame_mcp.types import Category, OperationParam, OperationSpec, Refusal

CATEGORY = Category(
    name="render",
    description=(
        "Rendering a Batch group (Write File / Render nodes). Look at what a render would "
        "write with plan, then run it with batch. The group need not be current."
    ),
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)


def _target_param() -> OperationParam:
    return OperationParam(
        name="target",
        type="selector",
        description="A 'batch' selector (uid or name). The group need not be current.",
        required=True,
    )


def _plan_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"), "_result = _render_plan(target)"
    )


def _batch_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"), "_result = _render_run(target)"
    )


def _batch_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    if args.get("allow_overwrite") is True:
        denial = deny_destructive_call("render.batch", "allow_overwrite=true", policy)
        if denial is not None:
            return denial
    timeout_s = args.get("timeout_s")
    if isinstance(timeout_s, (int, float)) and timeout_s > MAX_TIMEOUT_S:
        return Refusal(
            "INVALID_ARGS", f"timeout_s must be at most {MAX_TIMEOUT_S}, got {timeout_s}"
        )
    max_frames = args.get("max_frames")
    if isinstance(max_frames, (int, float)) and max_frames > MAX_FRAMES_LIMIT:
        return Refusal(
            "INVALID_ARGS", f"max_frames must be at most {MAX_FRAMES_LIMIT}, got {max_frames}"
        )
    return None


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


PLAN_REQUIRES = (
    "PyBatch.nodes",
    "PyBatch.uid",
    "PyBatch.name",
    "PyBatch.reels",
    "PyBatch.shelf_reels",
    "PyReel.clips",
    "PyNode.sockets",
    "PyNode.attributes",
    "PyWriteFileNode.get_resolved_media_path",
)


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)

    registry.register(
        OperationSpec(
            name="render.plan",
            category="render",
            description=(
                "Show what render.batch would write for a Batch group, changing nothing. "
                "result.nodes lists every Write File / Render node: bypass, wiredInputs, "
                "effective (not bypassed AND an input connected; skipReason otherwise), "
                "rangeStart / rangeEnd and plannedFrames -- the frame count is the Write File's "
                "range (default 1-100), not the group's duration. A Write File adds mediaPath, "
                "mediaPathPattern, fileType, resolvedMediaPath (get_resolved_media_path), "
                "outputDir, dirExists, defaultPath (/var/tmp and /tmp), plannedFiles, "
                "firstFile / lastFile and existingPlannedFiles (files a render would overwrite; "
                "render.batch refuses unless allow_overwrite=true). "
                "A Render node has no path attribute: `output` says its clip goes into its "
                "destination reel(s) and cannot be checked on disk. result.reels = the clip "
                "count of each schematic / shelf reel, result.summary.wouldRefuse = what "
                "render.batch would refuse with its defaults, result.summary.busyNote = that a "
                "render.batch of these frames occupies Flame while it runs."
            ),
            effect="read",
            params=(_target_param(),),
            accepts=("batch",),
            to_python=_plan_to_python,
            wrap=run_plan,
            requires=PLAN_REQUIRES,
        )
    )

    registry.register(
        OperationSpec(
            name="render.batch",
            category="render",
            description=(
                "Render a Batch group in the Foreground (PyBatch.render, works on a group that "
                "is not current). While it runs Flame is occupied: MCP calls -- reads "
                "included -- may not be answered (BRIDGE_UNAVAILABLE or TIMEOUT). Make timeout_s "
                "long enough for the whole render: this server has no op to stop a render. The "
                "frames written "
                "are set by each Write File node's range_start / range_end (default 1-100), not "
                "by the group's duration -- render.plan shows them. render.batch provides no "
                "undo: the files on disk and the clip put into the shelf reel `Batch Renders` stay "
                "(undo=unsupported; journal.revert has nothing to restore). "
                "Refused before anything is written (PRECONDITION_UNMET, nothing journaled; "
                "result.problems + result.plan): no Write File / Render node that is not "
                "bypassed and has an input connected; a Write File whose media_path is the "
                "default /var/tmp or /tmp (unless allow_default_path=true); more than "
                "max_frames frames in total; an output directory that does not exist; planned "
                "files that already exist (render.plan's existingPlannedFiles above 0), unless "
                "allow_overwrite=true. "
                "Then an intent (planned files, what was already in the directories) is "
                "journaled, the render runs for at most timeout_s, and the outcome records the "
                "files really written: result.nodes counts frames per Write File against the "
                "plan (short = SILENT_NOOP, or FLAME_API_ERROR when render returned False), "
                "otherNewFiles lists what else appeared (the `.clip`), newClips the clips the "
                "reels gained. Render nodes write clips, not files, so they are not verified on "
                "disk (result.verified null when only Render nodes ran). "
                "A TIMEOUT / BRIDGE_LOST is not an answer: the render may still be running "
                "(a TIMEOUT does not stop it), this server refuses every write until "
                "journal.check has decided. When the wait ended with the bridge's pending "
                "handle it is journaled and journal.check asks the bridge for its result "
                "(that says the render has ended; complete frames on disk do not), then "
                "counts the output files: landed / partial / failed. With allow_overwrite=true "
                "the render may overwrite existing files and cannot be undone: such a call "
                "counts as destructive for FLAME_MCP_ALLOW_EFFECTS (the op stays effect=create). "
                "Not allowed in plan.run."
            ),
            effect="create",
            undo="unsupported",
            thread="main-async",
            params=(
                _target_param(),
                OperationParam(
                    name="timeout_s",
                    type="number",
                    description=(
                        f"How long to wait for the render, 1 to {MAX_TIMEOUT_S} seconds "
                        f"(default {DEFAULT_TIMEOUT_S}). Make it long enough for the whole "
                        "render: while it runs the bridge may not answer MCP calls (reads "
                        "included) and this server has no op to stop it. The bridge caps a wait at "
                        "600 s."
                    ),
                    required=False,
                    default=DEFAULT_TIMEOUT_S,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="max_frames",
                    type="number",
                    description=(
                        f"Refuse when the effective nodes would write more frames than this in "
                        f"total (default {DEFAULT_MAX_FRAMES}, at most {MAX_FRAMES_LIMIT})."
                    ),
                    required=False,
                    default=DEFAULT_MAX_FRAMES,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="allow_default_path",
                    type="boolean",
                    description="Allow a Write File whose media_path is /var/tmp or /tmp.",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="allow_overwrite",
                    type="boolean",
                    description=(
                        "Allow the render although some of the files it would write already "
                        "exist (render.plan: existingPlannedFiles above 0). They are replaced "
                        "and cannot be brought back. The call then counts as destructive for "
                        "FLAME_MCP_ALLOW_EFFECTS."
                    ),
                    required=False,
                    default=False,
                ),
            ),
            accepts=("batch",),
            to_python=_batch_to_python,
            undo_steps=_no_undo_steps,
            precheck=_batch_precheck,
            wrap=run_render,
            requires=(*PLAN_REQUIRES, "PyBatch.render"),
        )
    )
