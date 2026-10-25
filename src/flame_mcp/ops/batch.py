"""`batch` category: Batch groups on the Desktop, addressed by a `batch` selector
(uid or name). Measured on Flame 2025.2.4: writes reach a group that is not current, so no op
here requires the group to be current.

The category also has `info`, the writes that run behind an automatic checkpoint (`create_reel`,
`create_shelf_reel`, `add_clips`, `organize`, `iterate` -- declared with a `CheckpointSpec`,
`checkpointed.py`) and `save_setup` (writes a file, not the project). The Flame-side code of
each is in `batch_prelude.py`. The side effects of a restore are worded once in
`checkpointed.RESTORE_SIDE_EFFECTS`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.checkpointed import RESTORE_SIDE_EFFECTS
from flame_mcp.effective_effect import overwrite_precheck
from flame_mcp.ops._attributes import attribute_requires, register_attribute_ops
from flame_mcp.ops._common import build_source, list_result_src, offset_param
from flame_mcp.reel_kinds import reel_kinds_named
from flame_mcp.registry import Registry
from flame_mcp.types import (
    Category,
    CheckpointSpec,
    OperationParam,
    OperationSpec,
    Refusal,
    SelectorKind,
)

CATEGORY = Category(
    name="batch",
    description=(
        "Batch groups on the Desktop (the 'batch' selector gives a uid or a name). The group "
        "need not be current."
    ),
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)


NODE_COUNT_BUDGET_S: Final[float] = 5.0
"""`batch.list` with `node_counts=true` or `outputs=true` stops reading groups after this long (the
first group is always read). Reading a non-current group's nodes makes Flame load the group."""


def _list_to_python(args: Mapping[str, Any]) -> str:
    limit = args["limit"]
    offset = args["offset"]
    node_counts = bool(args["node_counts"])
    outputs = bool(args.get("outputs", False))
    return build_source(
        "\n".join(
            [
                "_desktop = flame.projects.current_project.current_workspace.desktop",
                "_groups = list(_desktop.batch_groups)",
                "# PyDesktop.current_batch_group is a PyAttribute (api graph: it is one of",
                "# the desktop's attribute keys), so unwrap it before reading the PyBatch.",
                "_current_uid = None",
                "_current_error = None",
                "try:",
                "    _current = _v(_desktop.current_batch_group)",
                "    if _current is not None:",
                "        _current_uid = _v(_current.uid)",
                "except BaseException as _cur_exc:",
                "    _current_error = type(_cur_exc).__name__ + ': ' + str(_cur_exc)",
                f"_node_counts = {node_counts!r}",
                f"_outputs = {outputs!r}",
                "def _output_row(_n):",
                "    _is_writer = type(_n).__name__ == 'PyWriteFileNode'",
                "    _row = {",
                "        'name': _try(lambda: _v(_n.name)),",
                "        'kind': 'write_file' if _is_writer else 'render',",
                "        'bypass': _attr_or_none(_n, 'bypass'),",
                "    }",
                "    if _is_writer:",
                "        _row['mediaPath'] = _attr_or_none(_n, 'media_path')",
                "    else:",
                "        _row['destination'] = _attr_or_none(_n, 'destination')",
                "    return _row",
                "def _describe_batch(_g):",
                "    _uid = _v(_g.uid)",
                "    _item = {",
                "        'name': _v(_g.name),",
                "        'uid': _uid,",
                "        'isCurrent': _current_uid is not None and _uid == _current_uid,",
                "    }",
                "    if _node_counts or _outputs:",
                "        _nodes = list(_g.nodes)",
                "    if _node_counts:",
                "        _item['nodeCount'] = len(_nodes)",
                "    if _outputs:",
                "        _item['outputs'] = [",
                "            _output_row(_n)",
                "            for _n in _nodes",
                "            if type(_n).__name__ in _RENDER_PY_TYPES",
                "        ]",
                "    return _item",
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_groups)",
                "_page = _groups[_offset : _offset + _limit]",
                "import time as _time",
                "_started = _time.monotonic()",
                f"_budget = {NODE_COUNT_BUDGET_S!r}",
                "_items = []",
                "_stopped_early = None",
                "for _g in _page:",
                "    _timed = _node_counts or _outputs",
                "    if _timed and _items and _time.monotonic() - _started > _budget:",
                "        _stopped_early = {",
                "            'reason': 'time budget',",
                "            'budgetS': _budget,",
                "            'read': len(_items),",
                "        }",
                "        break",
                "    _items.append(_describe_batch(_g))",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
        "\n".join(
            [
                "_result['currentError'] = _current_error",
                "if _stopped_early is not None:",
                "    _result['nextOffset'] = _offset + len(_items)",
                "    _result['stoppedEarly'] = _stopped_early",
            ]
        ),
    )


UNDO_UNCONFIRMED: Final[str] = "UNDO UNCONFIRMED"
"""The marker a description carries while its undo claim is a placeholder. No op carries it
any more; `tests/test_ops_batch_p23a.py` pins that."""

MAX_CLIPS: Final[int] = 50
"""Most clips one `batch.add_clips` call takes (one `media_panel.copy` of a list)."""
ADD_CLIPS_TIMEOUT_S: Final[float] = 30.0
CLIP_SELECTOR_KINDS: Final[tuple[SelectorKind, ...]] = ("wiretap", "path", "selection")
REEL_KINDS: Final[tuple[str, ...]] = ("schematic", "shelf")

_CHECKPOINTED = (
    "Runs behind an automatic checkpoint: the group is saved first (batch.checkpoint), an "
    "intent is journaled, the call runs, and the outcome is journaled with the group's "
    "fingerprint. The envelope's `journal` = {group, id, checkpoint, checkpointPath, revert, "
    "changed}; journal.revert(group) restores the checkpoint while the group's fingerprint is "
    "still the one the call left (else CONFLICT; force=true overrides, journaled). The "
    "fingerprint -- and so `changed` and that comparison -- covers nodes, wiring and the inner "
    "nodes of Action / Image / GMask Tracer nodes; it does NOT see reels, shelf reels, "
    "iterations or positions. Native Undo is not used. "
)
_CHECKPOINTED_IN_PLAN = (
    "In plan.run the step is bundled: the plan is one checkpointed write on the group, so the "
    "step takes no checkpoint of its own and journal.revert(group) of the plan is one restore. "
    + RESTORE_SIDE_EFFECTS
    + " "
)
_CHECKPOINTED_NOT_IN_PLAN = (
    "Not allowed in plan.run (shelf reels and iterations are outside a setup, so the plan's "
    "restore would leave them). " + RESTORE_SIDE_EFFECTS + " "
)


def _target_param() -> OperationParam:
    return OperationParam(
        name="target",
        type="selector",
        description="A 'batch' selector (uid or name). The group need not be current.",
        required=True,
    )


def _resolve(args: Mapping[str, Any]) -> str:
    return selectors.resolve_code(args["target"], "target")


def _info_to_python(args: Mapping[str, Any]) -> str:
    return build_source(_resolve(args), "_result = _batch_info(target)")


def _create_reel_to_python(shelf: bool) -> Any:
    def to_python(args: Mapping[str, Any]) -> str:
        return build_source(
            _resolve(args),
            "\n".join(
                [
                    f"_result = _create_reel(target, {args['name']!r}, {shelf!r})",
                    "if _result['error'] is not None:",
                    "    raise _OpError('SILENT_NOOP', _result['error'])",
                ]
            ),
        )

    return to_python


def _add_clips_to_python(args: Mapping[str, Any]) -> str:
    clips = list(args["clips"])
    blocks = [_resolve(args)]
    blocks += [selectors.resolve_code(sel, f"_clip{i}") for i, sel in enumerate(clips)]
    names = ", ".join(f"_clip{i}" for i in range(len(clips)))
    kind = args.get("reel_kind")
    blocks.append(
        "\n".join(
            [
                f"_result = _add_clips(target, {args['reel']!r}, {kind!r}, [{names}])",
                "if _result['error'] is not None:",
                "    raise _OpError(_result.get('errorCode') or 'SILENT_NOOP', _result['error'])",
            ]
        )
    )
    return build_source(*blocks)


def _add_clips_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    clips = args.get("clips")
    if not isinstance(clips, list) or not 1 <= len(clips) <= MAX_CLIPS:
        count = len(clips) if isinstance(clips, list) else clips
        return Refusal("INVALID_ARGS", f"clips must hold 1 to {MAX_CLIPS} selectors, got {count!r}")
    problems = [
        f"clips[{i}]: {issue}"
        for i, sel in enumerate(clips)
        for issue in selectors.validate_selector(sel, CLIP_SELECTOR_KINDS)
    ]
    if problems:
        return Refusal("INVALID_ARGS", "invalid clips: " + "; ".join(problems))
    kind = args.get("reel_kind")
    if kind is not None and kind not in REEL_KINDS:
        return Refusal("INVALID_ARGS", f"reel_kind must be one of {list(REEL_KINDS)}, got {kind!r}")
    return None


def _add_clips_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    """Refuse a reel that is not there (or is ambiguous) before anything is checkpointed: a typo
    should leave no checkpoint and no journal group behind. The Flame side checks again."""
    env = rt.do("batch.info", {"target": args["target"]})
    if env.get("ok") is not True:
        return Refusal(
            env.get("errorCode") or "FLAME_API_ERROR",
            f"nothing was run: the group's reels could not be read first ({env.get('error')})",
            hint=env.get("hint"),
        )
    info = env.get("result") or {}
    wanted = args.get("reel_kind")
    found = reel_kinds_named(info, args["reel"], wanted)
    if not found:
        listed = {
            "reels": [r.get("name") for r in info.get("reels") or []],
            "shelfReels": [r.get("name") for r in info.get("shelfReels") or []],
        }
        qualifier = f"{wanted} " if wanted else ""
        return Refusal(
            "SELECTOR_EMPTY",
            f"nothing was run: no {qualifier}reel named {args['reel']!r} in the group",
            result={"ran": False, **listed},
        )
    if len(found) > 1:
        return Refusal(
            "SELECTOR_AMBIGUOUS",
            f"nothing was run: {len(found)} reels are named {args['reel']!r} (pass reel_kind "
            "to choose)",
            result={"ran": False, "matches": found},
        )
    return None


def _organize_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        _resolve(args),
        "\n".join(
            [
                "_result = _organize(target)",
                "if _result['error'] is not None:",
                "    raise _OpError('FLAME_API_ERROR', _result['error'])",
            ]
        ),
    )


def _iterate_to_python(args: Mapping[str, Any]) -> str:
    index = args.get("index")
    index_src = None if index is None else int(index)
    overwrite = bool(args.get("overwrite", False))
    return build_source(
        _resolve(args),
        "\n".join(
            [
                f"_result = _iterate(target, {index_src!r}, {overwrite!r})",
                "if _result['error'] is not None:",
                "    raise _OpError(_result.get('errorCode') or 'SILENT_NOOP', _result['error'])",
            ]
        ),
    )


def _iterate_check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
    """Refuse an `index` that names an existing iteration before anything is checkpointed
    (Flame overwrites it without warning, and no checkpoint brings an iteration back)."""
    index = args.get("index")
    if index is None or args.get("overwrite", False):
        return None
    env = rt.do("batch.info", {"target": args["target"]})
    if env.get("ok") is not True:
        return Refusal(
            env.get("errorCode") or "FLAME_API_ERROR",
            f"nothing was run: the group's iterations could not be read first ({env.get('error')})",
            hint=env.get("hint"),
        )
    rows = (env.get("result") or {}).get("iterations") or []
    numbers = [row.get("number") for row in rows]
    if int(index) in numbers:
        return Refusal(
            "INVALID_ARGS",
            f"iteration {int(index)} already exists: iterate(index={int(index)}) would "
            "overwrite it without warning (and no checkpoint brings an iteration back). "
            "Pass overwrite=true to do that.",
            result={"ran": False, "existingIterations": numbers},
        )
    return None


def _save_setup_to_python(args: Mapping[str, Any]) -> str:
    overwrite = bool(args.get("overwrite", False))
    return build_source(
        _resolve(args),
        "\n".join(
            [
                f"_result = _save_setup(target, {args['path']!r}, {overwrite!r})",
                "if _result['error'] is not None:",
                "    raise _OpError('FLAME_API_ERROR', _result['error'])",
            ]
        ),
    )


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _register_writes(registry: Registry) -> None:
    requires_reel = (
        "PyBatch.reels",
        "PyBatch.shelf_reels",
        "PyBatch.create_reel",
        "PyBatch.create_shelf_reel",
        "PyReel.clips",
        "PyReel.type",
        "PyReel.uid",
    )
    registry.register(
        OperationSpec(
            name="batch.info",
            category="batch",
            description=(
                "A Batch group's overview, for a group that need not be current: name, uid, "
                "isCurrent, duration, startFrame, nodeCount, reels and shelfReels (each "
                "{name, type, uid, clipCount, clips = the first 100 clip names, "
                "clipsTruncated}), iterations ({name, number, uid}, by number) and "
                "currentIterationNumber. `unreadable` lists every field that could not be read "
                "(the call still succeeds)."
            ),
            effect="read",
            params=(_target_param(),),
            accepts=("batch",),
            to_python=_info_to_python,
            requires=(
                "PyBatch.uid",
                "PyBatch.name",
                "PyBatch.duration",
                "PyBatch.start_frame",
                "PyBatch.nodes",
                "PyBatch.batch_iterations",
                "PyBatch.current_iteration_number",
                "PyDesktop.current_batch_group",
                "PyBatchIteration.iteration_number",
                *requires_reel,
            ),
        )
    )
    for shelf in (False, True):
        noun = "shelf reel" if shelf else "schematic reel"
        name = "batch.create_shelf_reel" if shelf else "batch.create_reel"
        undo_note = (
            "Undo: unsupported -- shelf reels are outside a setup, so journal.revert does not "
            "remove the shelf reel (it only restores the group's nodes and wiring, which this "
            "call did not change). `changed` reads false. Delete it by hand."
            if shelf
            else "Undo: checkpoint -- journal.revert removes the new reel while it is still "
            "empty (a restore drops empty reels). Once it holds clips (and so Clip nodes) the "
            "revert is refused as CONFLICT; with force=true the reel and its clips are lost, "
            "because a restore does not recreate a reel the checkpoint lacks. `changed` reads "
            "false (the fingerprint does not see reels), so the revert's conflict check cannot "
            "notice a changed reel list."
        )
        registry.register(
            OperationSpec(
                name=name,
                category="batch",
                description=(
                    f"Create a {noun} in a Batch group (PyBatch."
                    f"{'create_shelf_reel' if shelf else 'create_reel'}). result.reel is the new "
                    "reel {name, type, uid}; result.renamed says whether Flame changed the "
                    "requested name; reelsBefore / reelsAfter list the names. A call that "
                    "returns but leaves no new reel is an error (SILENT_NOOP). "
                    + _CHECKPOINTED
                    + (_CHECKPOINTED_NOT_IN_PLAN if shelf else _CHECKPOINTED_IN_PLAN)
                    + undo_note
                ),
                effect="create",
                undo="unsupported" if shelf else "checkpoint",
                params=(
                    _target_param(),
                    OperationParam(
                        name="name",
                        type="string",
                        description=f"Name of the new {noun}.",
                        required=True,
                        non_empty=True,
                    ),
                ),
                accepts=("batch",),
                to_python=_create_reel_to_python(shelf),
                undo_steps=_no_undo_steps,
                checkpoint=CheckpointSpec(),
                requires=requires_reel,
            )
        )
    registry.register(
        OperationSpec(
            name="batch.add_clips",
            category="batch",
            description=(
                "Copy clips (desktop or library clips / sequences, by selector) into a reel of "
                "a Batch group with flame.media_panel.copy: into a schematic reel each one also "
                "becomes a Clip node in the group (result.clipNodesAdded), into a shelf reel "
                "it does not. The copy is a new clip that references the same "
                "media. `reel` names the reel (schematic or shelf; `reel_kind` picks when both "
                "have that name). Clips and sequences of the Desktop and of a library are "
                "accepted. The reel must gain exactly as many clips as "
                "were given: none gained is SILENT_NOOP; some but not all, or more than given, "
                "is FLAME_API_ERROR with a message starting `partial:` (result.applied lists the "
                "entries that arrived, result.notApplied the requested ones that did not; "
                "journal.revert still works). A reel that cannot be read before the copy ends in "
                "PRECONDITION_UNMET without copying; one that cannot be read after it ends in "
                "FLAME_API_ERROR (the copy may have been written). "
                f"At most {MAX_CLIPS} clips per call. "
                + _CHECKPOINTED
                + _CHECKPOINTED_IN_PLAN
                + "Undo: checkpoint for a schematic reel -- journal.revert removes the Clip "
                "nodes (the fingerprint sees them; the group equals what it was) and the reels "
                "are rebuilt as described above. Into a shelf reel the copies are not undone "
                "(shelf reels are outside a setup): the clips stay in the shelf reel after a "
                "revert. result.revertCovers says which case a call was: true for a schematic "
                "reel, false for a shelf reel. In plan.run a step into a shelf reel is refused "
                "when the step runs (PRECONDITION_UNMET, nothing written): shelf reels are "
                "outside a Batch setup, so the plan's revert would leave the clips."
            ),
            effect="create",
            undo="checkpoint",
            params=(
                _target_param(),
                OperationParam(
                    name="reel",
                    type="string",
                    description="Name of the reel (a schematic reel or a shelf reel).",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="clips",
                    type="array",
                    description=(
                        f"1 to {MAX_CLIPS} selectors of clips / sequences: wiretap, path or "
                        "selection (each must resolve to one PyClip / PySequence)."
                    ),
                    required=True,
                    item_type="selector",
                ),
                OperationParam(
                    name="reel_kind",
                    type="string",
                    description="'schematic' or 'shelf': only needed when both have that name.",
                    required=False,
                    non_empty=True,
                ),
            ),
            accepts=("batch",),
            to_python=_add_clips_to_python,
            undo_steps=_no_undo_steps,
            precheck=_add_clips_precheck,
            checkpoint=CheckpointSpec(check=_add_clips_check, timeout_s=ADD_CLIPS_TIMEOUT_S),
            sample_args={
                "target": {"kind": "batch", "name": "sample"},
                "reel": "sample",
                "clips": [{"kind": "wiretap", "id": "sample"}],
            },
            requires=(*requires_reel, "PyMediaPanel.copy", "PyBatch.nodes"),
        )
    )
    registry.register(
        OperationSpec(
            name="batch.organize",
            category="batch",
            description=(
                "Arrange the nodes of a Batch group (PyBatch.organize). result.moved counts the "
                "nodes whose position changed, positionsBefore / positionsAfter give every "
                "node's [x, y]. "
                + _CHECKPOINTED
                + _CHECKPOINTED_IN_PLAN
                + "Undo: checkpoint -- journal.revert puts the old positions back. "
                "Positions are not in the fingerprint, so `changed` reads false."
            ),
            effect="reversible",
            undo="checkpoint",
            params=(_target_param(),),
            accepts=("batch",),
            to_python=_organize_to_python,
            undo_steps=_no_undo_steps,
            checkpoint=CheckpointSpec(),
            requires=("PyBatch.organize", "PyBatch.nodes", "PyNode.attributes"),
        )
    )
    registry.register(
        OperationSpec(
            name="batch.iterate",
            category="batch",
            description=(
                "Iterate a Batch group's setup (PyBatch.iterate): a new iteration named "
                "<group>_<number, zero-padded to 3 digits: _001>. result.iteration is "
                "{name, number, uid}, "
                "iterationsBefore / iterationsAfter list them. With `index` the iteration gets "
                "that number; if it is the number of an existing iteration Flame overwrites it "
                "without warning, so that needs overwrite=true (else INVALID_ARGS, before "
                "anything is checkpointed). The iterations are read before and after: none new "
                "is SILENT_NOOP; a new one with another number than `index`, or several new ones, "
                "is FLAME_API_ERROR with a message starting `partial:` (result.newIterations); "
                "a new one whose return value carries no number is ok with a note in `degraded`. "
                + _CHECKPOINTED
                + _CHECKPOINTED_NOT_IN_PLAN
                + "Undo: unsupported -- iterations are outside a setup, so journal.revert "
                "neither removes a new iteration nor brings back an overwritten one; "
                "an overwrite destroys the old iteration for good. A call with overwrite=true "
                "counts as destructive for FLAME_MCP_ALLOW_EFFECTS (the op stays effect=create)."
            ),
            effect="create",
            undo="unsupported",
            params=(
                _target_param(),
                OperationParam(
                    name="index",
                    type="number",
                    description=(
                        "The iteration number to make (1 or more). Omit for the next free one."
                    ),
                    required=False,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="overwrite",
                    type="boolean",
                    description="Allow `index` to overwrite an existing iteration.",
                    required=False,
                    default=False,
                ),
            ),
            accepts=("batch",),
            to_python=_iterate_to_python,
            undo_steps=_no_undo_steps,
            precheck=overwrite_precheck("batch.iterate"),
            checkpoint=CheckpointSpec(check=_iterate_check),
            requires=(
                "PyBatch.iterate",
                "PyBatch.batch_iterations",
                "PyBatch.current_iteration_number",
                "PyBatchIteration.iteration_number",
            ),
        )
    )
    registry.register(
        OperationSpec(
            name="batch.save_setup",
            category="batch",
            description=(
                "Save a Batch group's setup to a file (PyBatch.save_setup); nothing in the "
                "project changes. Flame rewrites the path: a `.batch` is appended and dots in "
                "the name become underscores (a directory of the same stem is written beside "
                "it), so give a name without dots. result.path is the file that really "
                "appeared (found by comparing the directory's `.batch` files before and "
                "after), result.written / overwritten list them. `path` must be absolute and "
                "its directory must exist; an existing target is refused unless "
                "overwrite=true, which replaces the file for good: a call with overwrite=true "
                "counts as destructive for FLAME_MCP_ALLOW_EFFECTS (the op stays effect=create). "
                "The file written is made 0600 (result.madePrivate); a file that could not be "
                "changed is in result.notMadePrivate. "
                "Undo: none (a file was written, delete it by hand). "
                "batch.checkpoint is the same save into the journal directory, with a record."
            ),
            effect="create",
            undo="none",
            params=(
                _target_param(),
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
            accepts=("batch",),
            to_python=_save_setup_to_python,
            undo_steps=_no_undo_steps,
            precheck=overwrite_precheck("batch.save_setup"),
            requires=("PyBatch.save_setup", "PyBatch.uid", "PyBatch.name"),
        )
    )


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)

    registry.register(
        OperationSpec(
            name="batch.list",
            category="batch",
            description=(
                "List the Batch groups on the Desktop, one page at a time: name, uid, "
                "isCurrent. `currentError` is null unless the current group "
                "could not be read (then every isCurrent is false and it says why). "
                "Reading the nodes of a group that is not current makes Flame load that group, "
                "which takes time per group and can exceed the timeout on a whole Desktop, so "
                "the list does not read node counts by default: use batch.info for one group, "
                "or node_counts=true to add `nodeCount` to each item. outputs=true adds "
                "`outputs` to each item, one {name, kind ('render' or 'write_file'), bypass} "
                "per Render / Write File node plus `destination` (Render) or `mediaPath` "
                "(Write File), each null when it cannot be read; it loads non-current groups the "
                "same way. With node_counts=true or outputs=true the "
                "page stops after a time budget (the first group is always read): the items "
                "read so far are returned with truncated=true, `nextOffset` (the offset to "
                "continue from) and `stoppedEarly` {reason, budgetS, read}. "
                "Address a group afterwards with {'kind': 'batch', 'uid': ...} (or name)."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="limit", type="number", description="Max items to return.", required=False
                ),
                offset_param(),
                OperationParam(
                    name="node_counts",
                    type="boolean",
                    description="Add `nodeCount` to each item (Flame loads non-current groups).",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="outputs",
                    type="boolean",
                    description=(
                        "Add `outputs` (the Render and Write File nodes) to each item (Flame "
                        "loads non-current groups)."
                    ),
                    required=False,
                    default=False,
                ),
            ),
            to_python=_list_to_python,
            requires=(
                "PyDesktop.batch_groups",
                "PyDesktop.current_batch_group",
                "PyBatch.name",
                "PyBatch.uid",
                "PyBatch.nodes",
            ),
        )
    )

    _register_writes(registry)

    register_attribute_ops(
        registry,
        category="batch",
        noun="Batch group",
        accepts=("batch",),
        target_description="A 'batch' selector (uid or name). The group need not be current.",
        stable_src=lambda _args: "_stable = _batch_selector(target)",
        revert_kinds=("batch",),
        requires=attribute_requires("PyBatch.attributes", "PyBatch.uid"),
    )
