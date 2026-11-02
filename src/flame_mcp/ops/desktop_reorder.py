"""`desktop.reorder`. The server side of the write is `desktop_reorder_run`, the Flame side only
reads the reel (the move is `desktop.move`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.desktop_reorder_run import (
    MAX_REORDER_ENTRIES,
    MAX_REORDER_MOVES,
    REORDER_OP,
    run_reorder,
)
from flame_mcp.ops._common import build_source
from flame_mcp.registry import Registry
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_DESTINATION_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path")

_DESCRIPTION = (
    "Put the clips / sequences of one Desktop reel into another order, in the same reel. "
    "Flame can only send an entry to the end of its reel, which gives it a new uid, so this "
    f"moves the fewest entries that can reach the order (at most {MAX_REORDER_MOVES}) with one "
    "desktop.move: the entries that move get NEW uids, the others keep theirs. "
    "order lists EVERY entry of the reel once, in the wanted order, each as its uid or a wiretap "
    "selector (list the reel first with desktop.list_children). An order that leaves one out, "
    "names one that is not in the reel or names one twice is INVALID_ARGS with nothing written, "
    f"and so is an order that needs more than {MAX_REORDER_MOVES} entries moved. The destination "
    "is a wiretap / path selector of a reel on the Desktop; a library, a folder or a reel of a "
    "Batch group is refused. An order the reel already has writes nothing: ok, moved 0, no "
    "journal. "
    "The reel is read again after the move and must list the wanted order, else FLAME_API_ERROR "
    "with result.after and result.expected (the move stays applied and journaled). A move that "
    "reaches nothing is SILENT_NOOP, and one that moves only some of the entries is "
    "FLAME_API_ERROR ('partial:', the envelope's partial); in both, what moved is journaled. A "
    "step that ends without an answer (TIMEOUT / BRIDGE_LOST / BRIDGE_UNAVAILABLE) is not "
    "retried: this server then refuses writes until journal.check has read Flame again. "
    "result: destination, requested, moved (the number of entries moved), before / after (name "
    "and uid of the reel's entries), uidMap [{name, from (old uid), to (new uid), selector}], "
    "orderMatches. Use uidMap[].selector for a moved entry from now on; its old selector no "
    "longer resolves. "
    "JOURNAL: one journal group holding the desktop_move. Envelope journal = {group, id, ids, "
    "revert}. "
    "Undo: journal.revert(group) puts the old order back with the same kind of move: the entries "
    "that must move (they can include ones the reorder left alone) get new uids again. It is "
    "CONFLICT, with nothing moved, when the reel no longer holds the entries the reorder left. "
    "Native Ctrl+Z is not used (undoSteps 0). Not allowed in plan.run."
)


def _reorder_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["destination"], "dest"),
        "\n".join(
            [
                "if type(dest).__name__ != 'PyReel':",
                "    raise _OpError(",
                "        'INVALID_ARGS',",
                "        'destination is a ' + type(dest).__name__ + ', not a Desktop reel',",
                "    )",
                "_dm_check_dest(dest)",
                "_where = _tl_refuse_location(dest, 'destination')",
                "if _where is not None:",
                "    raise _OpError('PRECONDITION_UNMET', _where)",
                "_result = {",
                "    'destination': _dc_with_selector(dest),",
                "    'entries': _dm_pins(dest),",
                "}",
                "_affected = 0",
            ]
        ),
    )


def _order_problems(order: Any) -> list[str]:
    if not isinstance(order, list) or not 1 <= len(order) <= MAX_REORDER_ENTRIES:
        count = len(order) if isinstance(order, list) else order
        return [f"order must hold 1 to {MAX_REORDER_ENTRIES} entries, got {count!r}"]
    problems = []
    for i, item in enumerate(order):
        if isinstance(item, str):
            if not item:
                problems.append(f"order[{i}] is an empty uid")
        else:
            problems += [
                f"order[{i}]: {issue}" for issue in selectors.validate_selector(item, ("wiretap",))
            ]
    return problems


def _reorder_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    problems = _order_problems(args.get("order")) + [
        f"destination: {issue}"
        for issue in selectors.validate_selector(args.get("destination"), _DESTINATION_KINDS)
    ]
    if problems:
        return Refusal("INVALID_ARGS", "invalid arguments: " + "; ".join(problems))
    return None


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=REORDER_OP,
            category="desktop",
            description=_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="destination",
                    type="selector",
                    description=(
                        "A wiretap / path selector of the reel to reorder (path root='desktop'). "
                        "A library, a folder, a reel inside a library or a reel of a Batch group "
                        "is refused."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="order",
                    type="array",
                    description=(
                        f"1 to {MAX_REORDER_ENTRIES} entries: every clip / sequence of the reel "
                        "once, in the wanted order, each as its uid (string) or a wiretap "
                        "selector."
                    ),
                    required=True,
                ),
            ),
            accepts=_DESTINATION_KINDS,
            to_python=_reorder_to_python,
            undo_steps=_no_undo_steps,
            precheck=_reorder_precheck,
            wrap=run_reorder,
            sample_args={
                "destination": {"kind": "wiretap", "id": "d"},
                "order": ["u1", "u2"],
            },
            requires=(
                "PyReel.children",
                "PyReel.type",
                "PyReel.parent",
                "PyLibrary.parent",
                "PyReelGroup.parent",
            ),
        )
    )
