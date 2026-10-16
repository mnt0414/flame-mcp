"""`desktop.delete`. The server side of the write is
`desktop_delete_run`, the Flame side `desktop_delete_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.desktop_delete_run import (
    DELETE_OP,
    PHASE_STASH,
    SNAPSHOT_ARG,
    run_delete,
)
from flame_mcp.desktop_run import (
    CREATED_ARG,
    EXPECTED_ARG,
    MAX_SOURCES,
    PHASE_ALIVE,
    PHASE_ARG,
    PHASE_READ,
    PHASE_REVERT,
    PHASE_WRITE,
)
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._media_phases import alive_source
from flame_mcp.registry import Registry
from flame_mcp.timeline_checkpoints import (
    DEFAULT_LIBRARY,
    LIBRARY_ARG,
    LIBRARY_ENV,
    REEL_ARG,
    REEL_PREFIX,
    checkpoint_library_name,
    stash_reel_name,
)
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")

_DESCRIPTION = (
    "Delete clips / sequences (flame.delete, confirm=False), after copying each one into the "
    f"stash reel of the checkpoint library ('{DEFAULT_LIBRARY}', reel '{REEL_PREFIX}<YYMMDD>'; the "
    f"environment variable {LIBRARY_ENV} names another library) so that journal.revert can put it "
    f"back. `targets`: 1 to {MAX_SOURCES} wiretap / path / selection selectors, each a PyClip or "
    "a PySequence on the Desktop. Refused before anything is copied or "
    "written: something inside a library, whichever library (PRECONDITION_UNMET: library "
    "contents are never deleted by this server; what the checkpoint library holds is cleaned up "
    "with journal.purge_checkpoints), a reel, a reel group, a folder, a library or a Batch group "
    "(PRECONDITION_UNMET: only clips and sequences are deleted), something inside a reel of a "
    "Batch group (INVALID_ARGS), the same thing listed twice (INVALID_ARGS). "
    "FLOW: every target's type, name, uid, parent and fingerprint (timeline.fingerprint: segments "
    "and effects) are read and journaled first; each target is copied into the stash and the "
    "copy's fingerprint must equal the original's (a copy that fails ends the call before anything "
    "is deleted; the copies already made stay recorded as checkpoints); after each copy the "
    "Timeline is put back on what it showed if the copy moved it, as timeline.checkpoint does; "
    "the intent is written "
    "(it lists the checkpoint ids); then one Flame call checks every target still has the "
    "fingerprint that was read (else CONFLICT and nothing is deleted), notes what the Timeline "
    "shows, deletes them in order, looks every old uid up again and puts the Timeline back on "
    "what it showed if that changed. A deleted sequence that was the one shown in the Timeline "
    "cannot be shown again: the Timeline is left empty (result.shownAfterDelete is null). "
    "result: requested, deleted [{from: {parent {type, name, uid, wiretapId, selector}, name, "
    "type, uid}, checkpoint, stash {type, name, uid, wiretapId, library, reel}, fingerprint, "
    "segmentCount, wasDisplayed}], notDeleted, partial, shownBefore / shownAfterDelete / "
    "shownAfter / reshown / reshownNote (the Timeline's sequence {type, uid, name}). A call "
    "after which every target is still there is SILENT_NOOP; only some gone: ok with "
    "result.partial = true, the envelope's partial {deleted, notDeleted} and a degraded note. "
    "JOURNAL as desktop.move (intent -> compare-and-swap -> outcome with the deletes). Envelope "
    "journal = {group, id, revert}. journal.check of a call whose answer was lost: every old "
    "uid gone = landed, every one still there = not_landed, else diverged. "
    "Undo: journal.revert(group) copies each stashed copy back into the thing's old parent "
    "(flame.media_panel.copy): the thing has a new uid and wiretap id and is at the end of the "
    "reel (its old place is not restored) -- result.reverted[].to.selector; a sequence that was "
    "shown in the Timeline before the delete is opened again. A thing whose old parent is gone, "
    "or whose stashed copy is gone (journal.purge_checkpoints) or changed, stays a CONFLICT "
    "while the others are put back (result.partial); a second revert puts back only what the "
    "first one did not. The stashed copies stay in the checkpoint library until "
    "journal.purge_checkpoints removes them. Native Ctrl+Z is not used (undoSteps 0). Not "
    "allowed in plan.run."
)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _delete_to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_WRITE)
    if phase == PHASE_ALIVE:
        return alive_source(args)
    if phase == PHASE_REVERT:
        return build_source(
            f"_result = _dd_revert({dict(args[CREATED_ARG])!r})",
            raise_on_error_src(),
            "_affected = len(_result['reverted'])",
            "_partial = (\n"
            "    {'done': len(_result['reverted']), 'failed': len(_result['notReverted'])}\n"
            "    if _result['partial'] else None\n"
            ")",
        )
    targets = list(args["targets"])
    blocks = [selectors.resolve_code(sel, f"_t{i}") for i, sel in enumerate(targets)]
    listed = "[" + ", ".join(f"_t{i}" for i in range(len(targets))) + "]"
    library = args.get(LIBRARY_ARG) or checkpoint_library_name()
    if phase == PHASE_READ:
        return build_source(
            *blocks, f"_result = _dd_snapshot({listed}, {library!r})", "_affected = 0"
        )
    if phase == PHASE_STASH:
        reel = args.get(REEL_ARG) or stash_reel_name()
        return build_source(
            *blocks,
            f"_result = _dd_stash({listed}, {library!r}, {reel!r})",
            raise_on_error_src(),
            "_affected = 0",
        )
    snapshot = dict(args.get(SNAPSHOT_ARG) or {})
    return build_source(
        *blocks,
        f"_result = _dd_delete({listed}, {snapshot!r}, {args.get(EXPECTED_ARG)!r})",
        raise_on_error_src(),
        "_affected = len(_result['deleted'])",
        "_partial = (\n"
        "    {'deleted': len(_result['deleted']), 'notDeleted': len(_result['notDeleted'])}\n"
        "    if _result['partial'] else None\n"
        ")",
    )


def _delete_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    targets = args.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_SOURCES:
        count = len(targets) if isinstance(targets, list) else targets
        return Refusal(
            "INVALID_ARGS", f"targets must hold 1 to {MAX_SOURCES} selectors, got {count!r}"
        )
    problems = [
        f"targets[{i}]: {issue}"
        for i, sel in enumerate(targets)
        for issue in selectors.validate_selector(sel, _ACCEPTS)
    ]
    if problems:
        return Refusal("INVALID_ARGS", "invalid arguments: " + "; ".join(problems))
    return None


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=DELETE_OP,
            category="desktop",
            description=_DESCRIPTION,
            effect="destructive",
            undo="value",
            params=(
                OperationParam(
                    name="targets",
                    type="array",
                    description=(
                        f"1 to {MAX_SOURCES} selectors of clips / sequences: wiretap, path "
                        "(root='desktop' for the Desktop) or selection; each must resolve to "
                        "one PyClip / PySequence."
                    ),
                    required=True,
                    item_type="selector",
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_delete_to_python,
            undo_steps=_no_undo_steps,
            precheck=_delete_precheck,
            wrap=run_delete,
            sample_args={"targets": [{"kind": "wiretap", "id": "s"}]},
            requires=(
                "flame.delete",
                "PyMediaPanel.copy",
                "PyClip.parent",
                "PySequence.parent",
                "PySequence.open",
                "PyTimeline.clip",
                "PyReel.children",
                "PyReel.type",
                "PyLibrary.children",
                "PyLibrary.reels",
                "PyLibrary.create_reel",
                "PyWorkspace.libraries",
                "PyWorkspace.create_library",
                "PySequence.versions",
                "PyVersion.tracks",
                "PyTrack.segments",
                "flame.find_by_uid",
                "flame.find_by_wiretap_node_id",
            ),
        )
    )
