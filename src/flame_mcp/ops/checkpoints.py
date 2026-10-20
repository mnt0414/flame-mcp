"""Batch checkpoints: `batch.checkpoint` / `batch.fingerprint` / `batch.load_setup`
run in Flame; `journal.restore_checkpoint` / `journal.list_checkpoints` run on the server.

`batch.checkpoint` is `create` with no undo of its own (it only writes a file); the server
wraps its call to journal the checkpoint (`checkpoints.run_checkpoint`). `batch.load_setup`
is the one public way to replace a group's contents from a setup file; it never calls Flame's
`load_setup` on a path that is not a file, because a failed load empties the group (measured on
Flame 2025.2.4: `load_setup` on a missing path raised and left the group empty).
The rest -- what a restore does, in what order -- is in `restore.py`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.checkpointed import RESTORE_SIDE_EFFECTS
from flame_mcp.checkpoints import (
    BASE_ARG,
    BATCH_KIND,
    DEFAULT_FINGERPRINT_SECTIONS,
    DIR_ARG,
    FINGERPRINT_SECTIONS,
    REELS_ARG,
    TIMELINE_KIND,
    kind_named,
    run_checkpoint,
)
from flame_mcp.clip_order import CLIP_ORDER_NOTE
from flame_mcp.journal import Index
from flame_mcp.ops._common import build_source, offset_param
from flame_mcp.registry import Registry
from flame_mcp.restore import restore_checkpoint, run_load_setup
from flame_mcp.runtime import ServerContext
from flame_mcp.timeline_purge import DEFAULT_OLDER_THAN_DAYS, purge_checkpoints
from flame_mcp.timeline_restore import RESTORE_NOTE
from flame_mcp.types import OperationParam, OperationSpec, Refusal, ServerResult

_TARGET = OperationParam(
    name="target",
    type="selector",
    description="A 'batch' selector (uid or name). The group need not be current.",
    required=True,
)


def _checkpoint_to_python(args: Mapping[str, Any]) -> str:
    directory, base = args.get(DIR_ARG), args.get(BASE_ARG)
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                f"_result = _save_checkpoint(target, {directory!r}, {base!r}, "
                f"{list(DEFAULT_FINGERPRINT_SECTIONS)!r}, {list(FINGERPRINT_SECTIONS)!r})",
                "if _result['error'] is not None:",
                "    raise _OpError('FLAME_API_ERROR', _result['error'])",
            ]
        ),
    )


def _fingerprint_to_python(args: Mapping[str, Any]) -> str:
    include_nodes = bool(args.get("include_nodes", False))
    sections = args.get("sections")
    if sections is None:
        sections = list(DEFAULT_FINGERPRINT_SECTIONS)
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                f"_result = _batch_fingerprint(target, {include_nodes!r}, {list(sections)!r})",
                "_result['batch'] = _batch_ident(target)",
                "_result['stable'] = _batch_selector(target)",
            ]
        ),
    )


def _fingerprint_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    unknown = [s for s in args.get("sections") or [] if s not in FINGERPRINT_SECTIONS]
    if unknown:
        return Refusal(
            "INVALID_ARGS",
            f"unknown fingerprint section(s) {unknown}; known: {list(FINGERPRINT_SECTIONS)}",
        )
    return None


def _load_setup_to_python(args: Mapping[str, Any]) -> str:
    lines = [
        f"_result = _load_setup_checked(target, {args['path']!r})",
        "if _result['error'] is not None:",
        "    raise _OpError(_result['errorCode'], _result['error'])",
    ]
    recorded = args.get(REELS_ARG)
    if isinstance(recorded, list):
        lines.append(
            f"_result['reelTidy'] = _tidy_reels(target, {recorded!r}, "
            f"{list(FINGERPRINT_SECTIONS)!r})"
        )
    return build_source(selectors.resolve_code(args["target"], "target"), "\n".join(lines))


def _list_row(r: Mapping[str, Any], index: Index) -> dict[str, Any]:
    if kind_named(r.get("kind")) is TIMELINE_KIND:
        purge = index.purged.get(str(r.get("id")))
        return {
            "id": r.get("id"),
            "ts": r.get("ts"),
            "kind": TIMELINE_KIND.name,
            "group": r.get("group"),
            "label": r.get("label"),
            "source": r.get("source"),
            "stash": r.get("stash"),
            "fingerprint": r.get("fingerprint"),
            "segmentCount": r.get("segmentCount"),
            "displayed": r.get("displayed"),
            "purged": purge is not None,
            "purgedAt": purge.get("ts") if purge is not None else None,
        }
    return {
        "id": r.get("id"),
        "ts": r.get("ts"),
        "kind": BATCH_KIND.name,
        "group": r.get("group"),
        "batch": r.get("batch"),
        "label": r.get("label"),
        "path": r.get("path"),
        "fileExists": isinstance(r.get("path"), str) and os.path.isfile(str(r["path"])),
        "fingerprint": r.get("fingerprint"),
        "nodeCount": r.get("nodeCount"),
    }


def _list_checkpoints(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    limit = int(args["limit"])
    offset = int(args["offset"])
    read = ctx.journal.read_all()
    index = Index(read.records)
    newest_first = list(reversed(list(index.checkpoints.values())))
    page = newest_first[offset : offset + limit]
    items = [_list_row(r, index) for r in page]
    return ServerResult(
        result={
            "total": len(newest_first),
            "offset": offset,
            "shown": len(items),
            "truncated": offset + len(items) < len(newest_first),
            "items": items,
            "skippedLines": read.skipped_lines,
        }
    )


def _restore(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    return restore_checkpoint(ctx, str(args["checkpoint"]))


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name="batch.checkpoint",
            category="batch",
            description=(
                "Save a Batch group's setup (PyBatch.save_setup) and record a checkpoint in "
                "the journal. result.checkpoint is its id, result.path the file save_setup "
                "really wrote (it rewrites the path; found by glob), result.fingerprint a "
                "hash of the group's nodes (name, type) and input wiring, and of the inner "
                "nodes (name, type) of Action / Image / GMask Tracer nodes. Changes nothing in "
                "Flame. Files go to <journal dir>/checkpoints/ and are never deleted here. "
                "Restore with journal.restore_checkpoint."
            ),
            effect="create",
            undo="none",
            params=(
                _TARGET,
                OperationParam(
                    name="label",
                    type="string",
                    description="A note stored on the checkpoint record.",
                    required=False,
                    non_empty=True,
                ),
            ),
            accepts=("batch",),
            to_python=_checkpoint_to_python,
            undo_steps=lambda args, affected: 0,
            wrap=run_checkpoint,
            requires=(
                "PyBatch.save_setup",
                "PyBatch.nodes",
                "PyBatch.uid",
                "PyBatch.name",
                "PyBatch.reels",
                "PyBatch.shelf_reels",
                "PyBatch.batch_iterations",
                "PyReel.clips",
                "PyReel.sequences",
            ),
        )
    )
    registry.register(
        OperationSpec(
            name="batch.fingerprint",
            category="batch",
            description=(
                "A Batch group's fingerprint: sha256 over its nodes' (name, `type` attribute, "
                "input sockets -> wired nodes), sorted, plus the inner nodes (name, type) of "
                "Action / Image / GMask Tracer nodes. Sees which nodes exist and how they "
                "are wired, not attribute values or positions (nor, unless `sections` asks, "
                "reels / shelf reels / iterations; `attributes` adds every node's attribute "
                "values). Also nodeCount, "
                "connectionCount, unreadableNodes; include_nodes adds the hashed list; "
                "`stable` addresses the group by uid."
            ),
            effect="read",
            params=(
                _TARGET,
                OperationParam(
                    name="include_nodes",
                    type="boolean",
                    description="Also return the node list the hash covers.",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="sections",
                    type="array",
                    description=(
                        "Extra parts the hash covers besides nodes and wiring: any of "
                        f"{list(FINGERPRINT_SECTIONS)} (reel names + clip names, iteration "
                        "names + numbers, every node's attribute values). Omit for the server's "
                        "default "
                        f"({list(DEFAULT_FINGERPRINT_SECTIONS)}); fingerprints taken with "
                        "different sections do not compare equal."
                    ),
                    required=False,
                    item_type="string",
                ),
            ),
            accepts=("batch",),
            to_python=_fingerprint_to_python,
            precheck=_fingerprint_precheck,
            requires=("PyBatch.nodes", "PyBatch.uid", "PyNode.sockets"),
        )
    )
    registry.register(
        OperationSpec(
            name="batch.load_setup",
            category="batch",
            description=(
                "Replace a Batch group's contents with a setup file (PyBatch.load_setup). The "
                "file must exist (checked before and inside Flame right before the call); "
                "otherwise load_setup is not called (PRECONDITION_UNMET) -- a failed load_setup "
                "empties the group. The group's current state is checkpointed first "
                "(result.preCheckpoint {id, path}, also in a failure's result) and the run is "
                "journaled with fingerprints: journal.revert(group) or "
                "journal.restore_checkpoint(preCheckpoint) puts it back. `path` is a real "
                "`.batch` path (a checkpoint's result.path). " + RESTORE_SIDE_EFFECTS
            ),
            effect="destructive",
            params=(
                _TARGET,
                OperationParam(
                    name="path",
                    type="string",
                    description="Path of the `.batch` setup file to load.",
                    required=True,
                    non_empty=True,
                ),
            ),
            accepts=("batch",),
            to_python=_load_setup_to_python,
            undo_steps=lambda args, affected: 0,
            wrap=run_load_setup,
            requires=(
                "PyBatch.load_setup",
                "PyBatch.nodes",
                "PyBatch.reels",
                "PyBatch.create_reel",
                "PyReel.clips",
                "PyReel.sequences",
                "flame.delete",
            ),
        )
    )
    registry.register(
        OperationSpec(
            name="journal.restore_checkpoint",
            category="journal",
            description=(
                "Put a Batch group (or, for a checkpoint of kind 'timeline', a sequence / clip) "
                "back to a checkpoint. For a timeline checkpoint: the sequence the checkpoint is "
                "of must still be there (found by its uid, or by the uids a restore gave it "
                "since), its stashed copy must still have the checkpoint's fingerprint, and the "
                "restore is the one described below; the checkpoint of the edited sequence is "
                "taken as part of it (result.preCheckpoint, the id, restoring it undoes this "
                "restore). A checkpoint whose stashed copy was purged by "
                "journal.purge_checkpoints is refused (PRECONDITION_UNMET, 'was purged'). "
                + RESTORE_NOTE
                + " For a Batch checkpoint: (1) its file must exist, else nothing "
                "happens and load_setup is not called (PRECONDITION_UNMET); (2) the group's "
                "CURRENT state is checkpointed first (result.preCheckpoint {id, path}, also in "
                "a failure's result; restoring it undoes this restore); (3) load_setup's "
                "return and a read-back are checked: success only if the group's fingerprint "
                "equals the checkpoint's (else FLAME_API_ERROR). Journaled as one intent / "
                "outcome. Success is judged on the group's nodes and wiring; the reels are tidied "
                "afterwards and reported apart (result.reelsRestored, result.reelTidy). "
                "Refused while this server "
                "has an unsettled write (journal.check). "
                + RESTORE_SIDE_EFFECTS
                + " "
                + CLIP_ORDER_NOTE
            ),
            effect="destructive",
            params=(
                OperationParam(
                    name="checkpoint",
                    type="string",
                    description="A checkpoint id (journal.list_checkpoints).",
                    required=True,
                    non_empty=True,
                ),
            ),
            server_handler=_restore,
            undo_steps=lambda args, affected: 0,
        )
    )
    registry.register(
        OperationSpec(
            name="journal.list_checkpoints",
            category="journal",
            description=(
                "List the journal's checkpoints, newest first: id, time, kind ('batch' or "
                "'timeline'), journal group, label, fingerprint and -- Batch: batch group, path, "
                "fileExists, nodeCount; timeline: source {type, name, uid, wiretapId, parent}, "
                "stash {uid, wiretapId, library, reel}, segmentCount, displayed, purged (its "
                "stashed copy was deleted by journal.purge_checkpoints, or found gone by it: it "
                "cannot be restored) and purgedAt. One page at a time."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="limit", type="number", description="Max items to return.", required=False
                ),
                offset_param(),
            ),
            server_handler=_list_checkpoints,
        )
    )
    registry.register(
        OperationSpec(
            name="journal.purge_checkpoints",
            category="journal",
            description=(
                "Delete the stashed copies of Timeline checkpoints from the checkpoint library "
                "(flame.delete): the checkpoints of timeline.checkpoint, of the segment edits "
                "behind them, of a restore's pre-restore copy, of desktop.delete and of an "
                "in-place desktop.reformat. Only what the journal records is deleted, and only a "
                "checkpoint recorded MORE than "
                "`older_than_days` days ago (0 = every one) whose copy is still in the library "
                "(`dry_run`, the default, deletes nothing and reads Flame only). Flame checks "
                "again, copy by copy, that it is inside the checkpoint library with the uid AND "
                "the name the journal recorded; one that is not is skipped. Then the date reels "
                "('timeline_<YYMMDD>', directly in the library) that this left empty are "
                "deleted. dry_run result: candidates [{id, ts, label, group, sourceName, stash "
                "{uid, name, library, reel}, exists, nameMatches}], count (what a real run would "
                "delete), missing (ids whose copy is not in the library any more) and unknown "
                "[{type, name, uid, wiretapId, reel}]: what the library holds that no recorded "
                "checkpoint accounts for (a copy made by hand, or by a call whose record was "
                "lost): it is NEVER deleted here. A real run adds purged [{id, uid, name, "
                "reel}], gone (ids recorded as purged because their copy was missing), skipped "
                "[{id, reason}], reelsDeleted, purgeRecord, and writes a `purge` record to the "
                "journal: the checkpoints are then listed as purged by journal.list_checkpoints, "
                "and journal.restore_checkpoint / journal.revert refuse to restore from them "
                "('purged'), so a delete or an edit whose stash was purged can no longer be "
                "undone. Not undoable. The journal's own 30-day cleanup keeps a journal file "
                "that holds a checkpoint not purged yet. Batch setup files (on disk) are not "
                "touched. A real run (dry_run=false) needs `token`: the value the dry run just "
                "returned (result.token, a digest of the candidate list). Without it, or when "
                "the candidates changed since (or older_than_days differs), nothing is deleted "
                "(PRECONDITION_UNMET). Look at the candidates of the dry run first, and ask the "
                "user before a real run. Denied by a read-only server like every destructive "
                "op, dry_run included."
            ),
            effect="destructive",
            undo="unsupported",
            params=(
                OperationParam(
                    name="older_than_days",
                    type="number",
                    description=(
                        "Only checkpoints recorded more than this many days ago (integer >= 0; "
                        f"default {DEFAULT_OLDER_THAN_DAYS})."
                    ),
                    required=False,
                    default=DEFAULT_OLDER_THAN_DAYS,
                    integer=True,
                    minimum=0,
                ),
                OperationParam(
                    name="dry_run",
                    type="boolean",
                    description=(
                        "true (the default): list what would be deleted and what is unknown, "
                        "delete nothing. false: delete (needs `token`)."
                    ),
                    required=False,
                    default=True,
                ),
                OperationParam(
                    name="token",
                    type="string",
                    description=(
                        "result.token of a dry run with the same older_than_days; required "
                        "when dry_run is false."
                    ),
                    required=False,
                    non_empty=True,
                ),
            ),
            server_handler=purge_checkpoints,
            undo_steps=lambda args, affected: 0,
        )
    )
