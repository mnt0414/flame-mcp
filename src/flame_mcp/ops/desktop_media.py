"""`desktop.duplicate` and `desktop.move`. The server side of the writes
is `desktop_media_run`, the Flame side `desktop_media_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.desktop_media_run import DUPLICATE_OP, MOVE_OP, run_media_write
from flame_mcp.desktop_run import (
    CREATED_ARG,
    EXPECTED_ARG,
    MAX_SOURCES,
    PHASE_ALIVE,
    PHASE_ARG,
    PHASE_PROBE,
    PHASE_READ,
    PHASE_REVERT,
    PHASE_WRITE,
)
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._media_phases import alive_source, probe_source
from flame_mcp.registry import Registry
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")
_DESTINATION_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path")

_DUPLICATE_DESCRIPTION = (
    "Duplicate a clip, a sequence or a reel with flame.duplicate. The copy is new (a new uid) and "
    "lands beside the original: a clip / sequence in the same reel with the same name, a reel at "
    "the end of the same reel group as '<name> copy' with its contents copied. A clip inside "
    "a library can be duplicated too. "
    "It reads what the Timeline shows before and after the call and, if it changed, opens "
    "what was shown before again "
    "(result.display: shownBefore, shownAfterCopy, reshown, note, shownAfter). Only a "
    "clip, a sequence and a reel are allowed (anything else: PRECONDITION_UNMET); "
    "a reel of a Batch group, or a clip inside one, is refused (INVALID_ARGS). "
    "The duplicate is found by a diff of the parent's children before and after, never by the "
    "answer (the answer for a sequence is a PyClip wrapper whose name and uid may not read; it "
    "is only recorded as result.returned). A call after which the parent did not grow is an error "
    "(SILENT_NOOP); two new entries that cannot be told apart end in FLAME_API_ERROR with "
    "result.candidates and nothing recorded as made. "
    "result: source {type, name, uid, wiretapId, selector, parent}, parent, duplicate {type, "
    "name, uid, wiretapId, selector} (use its selector, the uid of a new thing is not the "
    "source's), state {name, duration | entries}. "
    "JOURNAL as desktop.create (intent with the parent's children's uids -> compare-and-swap -> "
    "outcome with the duplicate). Envelope journal = {group, id, revert}. "
    "Undo: journal.revert(group) deletes the duplicate by uid (flame.delete) only while it is "
    "still as made: a reel has the same name and number of entries, a clip / sequence the same "
    "name, duration and content (segments and effects); otherwise CONFLICT and nothing is "
    "deleted. A record made without a content fingerprint is a CONFLICT as well. A duplicate "
    "that is gone already is ok (result.alreadyGone). Native Ctrl+Z is not used (undoSteps 0). "
    "Not allowed in plan.run."
)

_MOVE_DESCRIPTION = (
    "Move clips / sequences into a reel, folder or library with flame.media_panel.move. "
    "A moved thing gets a new uid and a new wiretap id: the selector and uid it had "
    "stop resolving, so use result.moved[].to "
    "(type, name, uid, wiretapId, selector), never the old selector. It goes to the end of the "
    "destination, after what is there. A move may change what the Timeline shows, so the "
    "server reads what the Timeline shows before the move and after it: if the moved things "
    "include the sequence that was shown, it is opened where it is now (a library sequence "
    "cannot be opened: result.reshownNote); otherwise, if the Timeline now shows another one, "
    "the sequence shown before is opened again. result.shownBefore / shownAfterMove / "
    "shownAfter ({type, uid, name} or null) and reshown (what open() answered; null when "
    "nothing needed putting back) say what happened; journal.revert does the same. A clip "
    "can be moved into a library. "
    f"At most {MAX_SOURCES} sources (wiretap / path / selection of a clip or sequence); the "
    "destination is a wiretap / path selector of a reel, a folder or a library. Refused before "
    "anything is written: a source inside a library (PRECONDITION_UNMET: library contents are not "
    "moved by this server; use desktop.copy to take something out of a library), a reel of a "
    "Batch group as source parent or destination (INVALID_ARGS, "
    "use batch.add_clips), a source that is already in the destination, listed twice, or not a "
    "clip / sequence. "
    "Every source's old parent {type, name, uid, selector}, name, type and uid are read first and "
    "journaled. After the call the moved things are found by a diff of the destination's entries "
    "before and after, matched with Flame's answer (a list in the order given). result: "
    "destination, requested / returned, moved [{from: {parent, name, type, uid (the old one)}, "
    "to}], notMoved. No source reached the destination: SILENT_NOOP. Only some of them: ok with "
    "result.partial = true, the envelope's partial {moved, notMoved} and a degraded note. "
    "JOURNAL as desktop.copy (intent with the destination's uids -> compare-and-swap -> outcome "
    "with the moves). Envelope journal = {group, id, revert}. journal.check of a call whose "
    "answer was lost: all old uids gone and as many new entries as sources = landed (the moves "
    "are paired by type and name), all still there and no new entry = not_landed, else diverged. "
    "Undo: journal.revert(group) moves the things back into their old parents with "
    "flame.media_panel.move, only when every one of them is still in the destination and every "
    "old parent still exists (else CONFLICT and nothing is moved); it also moves things back "
    "out of a library, since it undoes this server's own move. The things get new uids "
    "again and go to the end of their old parent; "
    "result.reverted[].to has the new selectors. Native Ctrl+Z is not used (undoSteps 0). Not "
    "allowed in plan.run."
)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _duplicate_to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_WRITE)
    if phase == PHASE_ALIVE:
        return alive_source(args)
    if phase == PHASE_REVERT:
        return build_source(
            f"_result = _dm_revert_duplicate({dict(args[CREATED_ARG])!r})",
            raise_on_error_src(),
            "_affected = 1 if _result['deleted'] else 0",
        )
    resolve = selectors.resolve_code(args["target"], "target")
    if phase == PHASE_PROBE:
        return build_source(resolve, probe_source(args, "target"))
    if phase == PHASE_READ:
        return build_source(resolve, "_result = _dm_dup_snapshot(target)", "_affected = 0")
    return build_source(
        resolve,
        f"_result = _dm_duplicate(target, {args.get(EXPECTED_ARG)!r})",
        raise_on_error_src(),
        "_affected = 1",
    )


def _move_to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_WRITE)
    if phase == PHASE_ALIVE:
        return alive_source(args)
    if phase == PHASE_REVERT:
        return build_source(
            f"_result = _dm_revert_moves({dict(args[CREATED_ARG])!r})",
            raise_on_error_src(),
            "_affected = len(_result['reverted'])",
        )
    blocks = [selectors.resolve_code(args["destination"], "dest")]
    if phase == PHASE_PROBE:
        return build_source(*blocks, probe_source(args, "dest"))
    sources = list(args["sources"])
    blocks += [selectors.resolve_code(sel, f"_src{i}") for i, sel in enumerate(sources)]
    listed = "[" + ", ".join(f"_src{i}" for i in range(len(sources))) + "]"
    if phase == PHASE_READ:
        snapshot = f"_result = _dm_move_snapshot(dest, {listed})"
        return build_source(*blocks, snapshot, "_affected = 0")
    return build_source(
        *blocks,
        f"_result = _dm_move(dest, {listed}, {args.get(EXPECTED_ARG)!r})",
        raise_on_error_src(),
        "_affected = len(_result['moved'])",
        "_partial = (\n"
        "    {'moved': len(_result['moved']), 'notMoved': len(_result['notMoved'])}\n"
        "    if _result['partial'] else None\n"
        ")",
    )


def _move_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    sources = args.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        count = len(sources) if isinstance(sources, list) else sources
        return Refusal(
            "INVALID_ARGS", f"sources must hold 1 to {MAX_SOURCES} selectors, got {count!r}"
        )
    problems = [
        f"sources[{i}]: {issue}"
        for i, sel in enumerate(sources)
        for issue in selectors.validate_selector(sel, _ACCEPTS)
    ]
    problems += [
        f"destination: {issue}"
        for issue in selectors.validate_selector(args.get("destination"), _DESTINATION_KINDS)
    ]
    if problems:
        return Refusal("INVALID_ARGS", "invalid arguments: " + "; ".join(problems))
    return None


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=DUPLICATE_OP,
            category="desktop",
            description=_DUPLICATE_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description=(
                        "A wiretap / path / selection selector of the clip, sequence or reel to "
                        "duplicate."
                    ),
                    required=True,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_duplicate_to_python,
            undo_steps=_no_undo_steps,
            wrap=run_media_write,
            sample_args={"target": {"kind": "wiretap", "id": "s"}},
            requires=(
                "flame.duplicate",
                "PyClip.parent",
                "PyClip.duration",
                "PySequence.parent",
                "PySequence.duration",
                "PyReel.parent",
                "PyReel.children",
                "PyReel.type",
                "PyReelGroup.reels",
                "PyLibrary.reels",
                "PyLibrary.children",
                "PyFolder.reels",
                "PyFolder.children",
                "flame.delete",
                "flame.find_by_uid",
            ),
        )
    )
    registry.register(
        OperationSpec(
            name=MOVE_OP,
            category="desktop",
            description=_MOVE_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="sources",
                    type="array",
                    description=(
                        f"1 to {MAX_SOURCES} selectors of clips / sequences: wiretap, path "
                        "(root='desktop' for the Desktop) or selection; each must resolve to "
                        "one PyClip / PySequence."
                    ),
                    required=True,
                    item_type="selector",
                ),
                OperationParam(
                    name="destination",
                    type="selector",
                    description=(
                        "A wiretap / path selector of a Desktop reel, a library, a library reel "
                        "or a library folder. A reel of a Batch group is refused."
                    ),
                    required=True,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_move_to_python,
            undo_steps=_no_undo_steps,
            precheck=_move_precheck,
            wrap=run_media_write,
            sample_args={
                "sources": [{"kind": "wiretap", "id": "s"}],
                "destination": {"kind": "wiretap", "id": "d"},
            },
            requires=(
                "PyMediaPanel.move",
                "PyClip.parent",
                "PySequence.parent",
                "PyReel.children",
                "PyReel.type",
                "PyLibrary.children",
                "PyFolder.children",
                "flame.find_by_uid",
                "flame.find_by_wiretap_node_id",
            ),
        )
    )
