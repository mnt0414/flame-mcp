"""`desktop.arrange`. The server side of the write is `desktop_arrange_run`, the Flame side
only reads the destination reel (the copies and moves are `desktop.copy` / `desktop.move`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.desktop_arrange_run import (
    ACTIONS,
    ARRANGE_OP,
    MAX_ARRANGE_ITEMS,
    MAX_SHIFTED,
    run_arrange,
)
from flame_mcp.ops._common import build_source
from flame_mcp.registry import Registry
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_DESTINATION_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path")
_SOURCE_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")
_ITEM_KEYS = frozenset({"action", "source"})

_DESCRIPTION = (
    "Build a Desktop reel in the order given, or put things in the middle of a reel that "
    "already holds some (position), copying or moving one clip / sequence per item. "
    "An item with action='copy' or action='move' works as desktop.copy or desktop.move would. "
    "Because every item is a call of its own, a source named twice gives two clips and "
    "the order of the items is kept; this is how to lay out a reel with a divider clip copied "
    "between the others. "
    "The order is checked at the end by reading the reel again: if it differs from the items "
    "the call ends in FLAME_API_ERROR. "
    f"{MAX_ARRANGE_ITEMS} items at most. The destination is a wiretap / path selector of a "
    "reel on the Desktop. "
    "POSITION: by default the items go to the end of the reel. position (0 or more) puts them "
    "before the Nth entry the reel already holds (0 = first; position = the number of entries "
    "is the end). Flame has no insert, so after the items the entries that were behind the "
    "insertion point (existing[position:]) are sent to the end in their order by one more "
    "desktop.move into the same reel (a move into the reel a thing is in puts it at the end "
    "with a new uid): those entries get NEW uids, so after the call use the selectors in "
    f"result.applied[].shifted[].made; at most {MAX_SHIFTED} entries may be behind "
    "position. This last step is part of the same journal group and is the last index "
    "(index = the number of items). If it moves only some of the entries, the call ends in "
    "FLAME_API_ERROR ('partial:'), result.failed.notMoved names the ones left and the hint "
    "says the reel will not get its old order back. "
    "Everything that can be refused is checked first and refuses the whole call with nothing "
    "written and nothing journaled: a source that does not resolve; a move whose source is "
    "inside a library (PRECONDITION_UNMET); a destination that "
    "is not a Desktop reel, or is a reel of a Batch group (INVALID_ARGS; batch.add_clips takes "
    "clips into a Batch group); a later item that uses a source an earlier item moved "
    "(INVALID_ARGS: a moved clip gets a new uid and wiretap id, so copy a source before you "
    "move it, and move it at most once); a position past the end of the reel, or with more "
    f"than {MAX_SHIFTED} entries behind it; with a position below the number of entries, a "
    "move item whose source is an entry of the destination (INVALID_ARGS: copy it, or leave "
    "position out). Without position, or with position = the number of entries, a move of an "
    "entry of the destination is allowed and puts it at the end. Just before the last step "
    "the entries behind position are read again: one that has left the destination meanwhile "
    "stops the call there (CONFLICT, the items stay applied) and nothing is moved. "
    "A copy may take its source out of a library. "
    "The items run one at a time. The first that fails stops the call; the ones before it stay "
    "applied (result.applied, result.stoppedAt, result.failed, result.notRun, the envelope's "
    "partial = {done, failed, notRun}; with position, the move of the entries behind it is the "
    "last step and an item that fails leaves it not run) and the hint names journal.revert. "
    "A step that ends "
    "without an answer (TIMEOUT / BRIDGE_LOST / BRIDGE_UNAVAILABLE) is not retried and the "
    "rest is not started: this server then refuses writes until journal.check has read Flame "
    "again. "
    "After the last step the reel is read again: it must list what it held before (less what "
    "an item moved out of it), in that order, with what each item made put in at position "
    "(else at the end) in the order of the items, and the entries sent behind them last; "
    "otherwise the call ends in "
    "FLAME_API_ERROR with result.order (what the reel lists) and result.expected, and the "
    "steps stay applied. result: destination, requested, existing (entries before), position, "
    "shiftedBehind, applied "
    "[{index, action, source {name, uid}, made {uid, name, type, wiretapId, selector}, intent}; "
    "the shift step: {index, action: 'move', shift: true, shifted [{source {name, uid}, made}], "
    "intent}], "
    "order (name and uid of the reel's entries after), orderMatches. A moved thing has a new "
    "uid: use made.selector, not the selector it had. "
    "JOURNAL: one journal group for the call, with one intent per item (a desktop_copy or a "
    "desktop_move, each with the compare-and-swap, outcome and fail-closed handling of its op). "
    "Envelope journal = {group, id, ids, revert}. "
    "Undo: journal.revert(group) puts the items back newest first, each by the revert of its "
    "op: a copy is deleted only while it still has the name, duration and content it had "
    "right after the call, a move puts the thing back into its old parent (at the end of it, "
    "with a new uid again) only while it is still in the destination. A moved thing goes back "
    "to the end of its old reel, so that reel gets its old order back only when the moved "
    "things were its last ones. With position the move of the entries behind it is put back "
    "first (into the same reel, at its end: their order does not change, their uids do), "
    "then the items. The reel it was built in then lists what it held in its old order, with "
    "new uids for the entries that were behind position, only if the move of those entries "
    "went through whole and the reel was not changed before the revert; if that move moved "
    "only some of the entries, the ones that did not move stay in front of the ones that "
    "did. A move item that took an entry out of the destination (no position) goes back to "
    "the end of the same reel. A step "
    "that cannot be "
    "put back (CONFLICT) leaves the others reverted, result.notReverted names it, and a "
    "second journal.revert does the rest. Native Ctrl+Z is not used (undoSteps 0). Not "
    "allowed in plan.run."
)


def _arrange_to_python(args: Mapping[str, Any]) -> str:
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
                "    'entries': _dc_rows(_dm_media_children(dest)),",
                "}",
                *(["_result['pins'] = _dm_pins(dest)"] if args.get("position") is not None else []),
                "_affected = 0",
            ]
        ),
    )


def _item_problems(index: int, entry: Any) -> list[str]:
    label = f"items[{index}]"
    if not isinstance(entry, dict):
        return [f"{label} must be an object {{action, source}}"]
    problems = []
    extra = sorted(set(entry) - _ITEM_KEYS)
    if extra:
        problems.append(f"{label} has unknown key(s) {extra} (only 'action' and 'source')")
    if entry.get("action") not in ACTIONS:
        problems.append(
            f"{label}.action must be one of {list(ACTIONS)}, got {entry.get('action')!r}"
        )
    if "source" not in entry:
        problems.append(f"{label}.source is required")
    else:
        problems += [
            f"{label}.source: {issue}"
            for issue in selectors.validate_selector(entry["source"], _SOURCE_KINDS)
        ]
    return problems


def _arrange_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    items = args.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ARRANGE_ITEMS:
        count = len(items) if isinstance(items, list) else items
        return Refusal(
            "INVALID_ARGS", f"items must hold 1 to {MAX_ARRANGE_ITEMS} entries, got {count!r}"
        )
    position = args.get("position")
    if position is not None and (
        isinstance(position, bool) or not isinstance(position, int) or position < 0
    ):
        return Refusal(
            "INVALID_ARGS", f"position must be an integer of 0 or more, got {position!r}"
        )
    problems = [problem for i, entry in enumerate(items) for problem in _item_problems(i, entry)]
    problems += [
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
            name=ARRANGE_OP,
            category="desktop",
            description=_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="destination",
                    type="selector",
                    description=(
                        "A wiretap / path selector of a reel on the Desktop (path root='desktop'). "
                        "A library, a folder, a reel inside a library or a reel of a Batch group "
                        "is refused."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="items",
                    type="array",
                    description=(
                        f"1 to {MAX_ARRANGE_ITEMS} objects {{action: 'move' | 'copy', source: "
                        "selector}}, applied in this order. source is a wiretap / path "
                        "(root='desktop' for the Desktop) / selection selector of one clip or "
                        "sequence."
                    ),
                    required=True,
                    item_type="object",
                ),
                OperationParam(
                    name="position",
                    type="number",
                    description=(
                        "Put the items before the Nth entry the destination already holds "
                        "(0 = first). Omit for the end, which is also position = the number of "
                        "entries. Larger than that: INVALID_ARGS before anything is written."
                    ),
                    required=False,
                    integer=True,
                    minimum=0,
                ),
            ),
            accepts=_DESTINATION_KINDS,
            to_python=_arrange_to_python,
            undo_steps=_no_undo_steps,
            precheck=_arrange_precheck,
            wrap=run_arrange,
            sample_args={
                "destination": {"kind": "wiretap", "id": "d"},
                "items": [{"action": "copy", "source": {"kind": "wiretap", "id": "s"}}],
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
