"""`mediahub.import`, `desktop.duplicate` and `desktop.move`, server side.

They go through the same write-ahead engine as `desktop.create` / `desktop.copy` (`desktop_run`:
pre-read -> intent -> compare-and-swap write -> outcome with `created`; `journal.revert` and
`journal.check` read Flame again). This module is only what is theirs:

  import     `created` = {destination, imports: [{uid, name, type, wiretapId, selector, state}]}.
             What a call made is the destination's entries that were not there before (Flame
             answers `[]` for a path that does not exist, without an error). `state` = {name,
             duration, fingerprint} read right after the call. Revert: delete them by uid, only
             while every one of them still has its `state` (else CONFLICT and nothing is
             deleted; a record without a fingerprint is a CONFLICT as well).
  duplicate  `created` = {uid, name, type, wiretapId, selector, parent, state}. The duplicate is
             found by comparing the parent's children before and after (the answer of
             `flame.duplicate(sequence)` has no readable name or uid). Revert: delete it, only
             while its name, duration and fingerprint (a reel: its name and number of entries)
             are still what `state` says, else CONFLICT and nothing is deleted (a clip / sequence
             record without a fingerprint is a CONFLICT as well).
  move       `created` = {destination, moves: [{from: {parent, name, type, uid}, to: {type, name,
             uid, wiretapId, selector}}]}. A moved thing always gets a new uid and wiretap id
             (measured). Revert: move the things back into their old parents, only when every one
             of them is still in the destination and every old parent still exists (else
             CONFLICT and nothing is moved); the things get new uids again and go to the end of
             the parent, so the revert's answer carries the new selectors.

`journal.check` of an intent that has no outcome probes the destination / parent for entries the
intent's `existing` did not list (`PHASE_PROBE`), and for a move also looks the sources up by their
old uid: all gone and as many new entries as sources = landed, all still there and no new entry =
not landed, anything else = diverged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.desktop_revert import (
    CheckVerdict,
    RevertProfile,
    check_flow,
    classify_alive,
    observe,
    observe_alive,
    revert_flow,
)
from flame_mcp.desktop_run import (
    CREATED_ARG,
    EXPECTED_ARG,
    PHASE_ARG,
    PHASE_PROBE,
    PHASE_REVERT,
    WriteProfile,
    rows_uids,
    run_write_flow,
)
from flame_mcp.journal import (
    KIND_DESKTOP_DUPLICATE,
    KIND_DESKTOP_IMPORT,
    KIND_DESKTOP_MOVE,
    KIND_DESKTOP_REVERT,
    MEDIA_WRITE_KINDS,
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    IntentView,
    now_iso,
)
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import ServerResult

if TYPE_CHECKING:
    from flame_mcp.journal import Index
    from flame_mcp.runtime import ServerContext

IMPORT_OP: Final[str] = "mediahub.import"
DUPLICATE_OP: Final[str] = "desktop.duplicate"
MOVE_OP: Final[str] = "desktop.move"
KIND_BY_OP: Final[Mapping[str, str]] = {
    IMPORT_OP: KIND_DESKTOP_IMPORT,
    DUPLICATE_OP: KIND_DESKTOP_DUPLICATE,
    MOVE_OP: KIND_DESKTOP_MOVE,
}
OP_BY_KIND: Final[Mapping[str, str]] = {kind: op for op, kind in KIND_BY_OP.items()}
_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"

MOVE_NOTE: Final[str] = (
    "A moved thing gets a NEW uid and a NEW wiretap id: the selector and uid it had "
    "no longer resolve, use moved[].to.selector. It went to the END of the destination, not to a "
    "position. A move may change what the Timeline shows: it is read "
    "before and after and put back (result.shownBefore / shownAfterMove / shownAfter / "
    "reshown)."
)
MOVE_REVERT_NOTE: Final[str] = (
    "The things went back into their old parents with NEW uids and wiretap ids again, at the END "
    "of each parent (their old place in the reel is not restored): use reverted[].to.selector. "
    "What the Timeline shows is put back as after a move (result.shownBefore / shownAfter / "
    "reshown)."
)


def is_media_intent(view: IntentView) -> bool:
    """An intent of `mediahub.import` / `desktop.duplicate` / `desktop.move`, or the revert of
    one."""
    if view.desktop_kind in MEDIA_WRITE_KINDS:
        return True
    return view.desktop_kind == KIND_DESKTOP_REVERT and view.intent.get("revertOf") in (
        MEDIA_WRITE_KINDS
    )


def _entry_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "uid": entry.get("uid"),
        "name": entry.get("name"),
        "type": entry.get("type"),
        "wiretapId": entry.get("wiretapId"),
        "selector": entry.get("selector"),
    }


def _no_refusal(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any], started: float
) -> dict[str, Any] | None:
    return None


def _stable_args(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """The destination / parent (and the target / each source) re-addressed by wiretap id, so the
    write acts on what the pre-read saw even if a `selection` / `path` moved meanwhile."""
    args = dict(op_args)
    if op.name == DUPLICATE_OP:
        stable = (pre.get("parent") or {}).get("selector")
        source = (pre.get("source") or {}).get("selector")
        if not isinstance(stable, dict) or not isinstance(source, dict):
            return args, None, "the target or its parent has no wiretap id"
        args["target"] = source
        return args, stable, None
    stable = (pre.get("destination") or {}).get("selector")
    if not isinstance(stable, dict):
        return args, None, "the destination has no wiretap id, so it cannot be found again"
    args["destination"] = stable
    if op.name == MOVE_OP:
        rows = pre.get("sources") or []
        args["sources"] = [
            row.get("selector") if isinstance(row.get("selector"), dict) else original
            for original, row in zip(op_args["sources"], rows, strict=True)
        ]
    return args, stable, None


def _source_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**_entry_row(row), "parent": row.get("parent")}


def _intent_record(
    op: RegisteredOperation,
    op_args: dict[str, Any],
    pre: dict[str, Any],
    stable: dict[str, Any] | None,
    *,
    intent_id: str,
    group_id: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op.name,
        "kind": KIND_BY_OP[op.name],
        "args": op_args,
        "target": stable,
        "existing": rows_uids(pre.get("existing")),
    }
    if op.name == DUPLICATE_OP:
        record["source"] = _source_row(pre.get("source") or {})
    elif op.name == MOVE_OP:
        record["sources"] = [_source_row(s) for s in pre.get("sources") or []]
    return record


def created_record(op_name: str, result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What an import / duplicate / move result says was made, in the form the journal keeps and
    `journal.revert` works from. None when nothing is known to have been made."""
    if not isinstance(result, Mapping):
        return None
    if op_name == IMPORT_OP:
        made = [
            {**_entry_row(e), "state": e.get("state")}
            for e in result.get("imported") or []
            if e.get("uid")
        ]
        if not made:
            return None
        return {"destination": result.get("destination"), "imports": made}
    if op_name == DUPLICATE_OP:
        dup = result.get("duplicate")
        if not isinstance(dup, Mapping) or not dup.get("uid"):
            return None
        return {
            **_entry_row(dup),
            "parent": result.get("parent"),
            "state": result.get("state"),
            "sourceUid": (result.get("source") or {}).get("uid"),
        }
    moves = [m for m in result.get("moved") or [] if (m.get("to") or {}).get("uid")]
    if not moves:
        return None
    return {"destination": result.get("destination"), "moves": moves}


def _read_back(env: Mapping[str, Any]) -> dict[str, Any] | None:
    result = env.get("result")
    op_name = (env.get("execution") or {}).get("op")
    if not isinstance(result, Mapping):
        return None
    if op_name == IMPORT_OP and "requested" in result:
        return {
            "requested": result["requested"],
            "returned": result.get("returned"),
            "imported": len(result.get("imported") or []),
        }
    if op_name == MOVE_OP and "requested" in result:
        return {
            "requested": result["requested"],
            "returned": result.get("returned"),
            "moved": len(result.get("moved") or []),
        }
    if op_name == DUPLICATE_OP and "returned" in result:
        return {"returned": result["returned"], "duplicated": result.get("duplicate") is not None}
    return None


def _notes(op: RegisteredOperation, op_args: Mapping[str, Any], env: dict[str, Any]) -> None:
    result = env.get("result")
    if not isinstance(result, dict):
        return
    notes: list[str] = []
    imported = result.get("imported") or []
    if op.name == IMPORT_OP and imported:
        asked, made, answered = result.get("requested"), len(imported), result.get("returned")
        if asked != made:
            notes.append(
                f"{asked} path(s) made {made} clip(s): a directory brings every media file in it "
                "and a sequence pattern is one clip, so a path is not one clip"
            )
        if answered != made:
            notes.append(
                f"import_clips answered {answered} clip(s) but {made} new clip(s) are in the "
                "destination: `imported` lists what the destination has"
            )
    if op.name == MOVE_OP and result.get("moved"):
        notes.append(MOVE_NOTE)
    if notes:
        result["notes"] = [*(result.get("notes") or []), *notes]
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warnings:
        env["degraded"] = [*env.get("degraded", []), *[f"read-back differs: {w}" for w in warnings]]


def _pre_hint(op: RegisteredOperation, pre: Mapping[str, Any]) -> str | None:
    if pre.get("errorCode") != "INVALID_ARGS":
        return None
    return (
        "A reel of a Batch group takes clips through batch.add_clips (it checkpoints the group and "
        f"checks the Clip nodes); {op.name} is for Desktop and library places. Nothing was "
        "written and nothing journaled."
    )


MEDIA_PROFILE: Final[WriteProfile] = WriteProfile(
    refuse=_no_refusal,
    stable_args=_stable_args,
    intent=_intent_record,
    created=created_record,
    notes=_notes,
    pre_hint=_pre_hint,
    read_back=_read_back,
)


def run_media_write(
    rt: Any, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `mediahub.import`, `desktop.duplicate` and `desktop.move`."""
    return run_write_flow(rt, op, op_args, MEDIA_PROFILE, group=group)


def _revert_op_name(view: IntentView) -> str:
    return OP_BY_KIND[str(view.desktop_kind)]


def _revert_args(view: IntentView, created: dict[str, Any]) -> dict[str, Any]:
    target = view.intent.get("target") or {"kind": "selection"}
    args: dict[str, Any] = {PHASE_ARG: PHASE_REVERT, CREATED_ARG: created}
    kind = view.desktop_kind
    if kind == KIND_DESKTOP_IMPORT:
        return {"paths": [], "destination": target, **args}
    if kind == KIND_DESKTOP_DUPLICATE:
        return {"target": created.get("selector") or {"kind": "selection"}, **args}
    return {"sources": [], "destination": target, **args}


def _revert_notes(view: IntentView, result: Mapping[str, Any]) -> list[str]:
    if view.desktop_kind == KIND_DESKTOP_MOVE and result.get("reverted"):
        return [MOVE_REVERT_NOTE]
    return []


def _revert_count(view: IntentView, result: Mapping[str, Any]) -> int:
    kind = view.desktop_kind
    if kind == KIND_DESKTOP_IMPORT:
        return len(result.get("deleted") or [])
    if kind == KIND_DESKTOP_DUPLICATE:
        return int(result.get("deleted") is True)
    return len(result.get("reverted") or [])


def _revert_extra(result: Mapping[str, Any]) -> dict[str, Any]:
    return {"reverted": result["reverted"]} if "reverted" in result else {}


MEDIA_REVERT: Final[RevertProfile] = RevertProfile(
    op_name=_revert_op_name,
    args=_revert_args,
    notes=_revert_notes,
    count=_revert_count,
    extra=_revert_extra,
)


def revert_media_group(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView]
) -> ServerResult:
    """`journal.revert` of a group whose intent is an import / duplicate / move."""
    return revert_flow(ctx, index, group, views, MEDIA_REVERT)


def _alive_items(view: IntentView) -> list[dict[str, Any]]:
    created = view.intent.get("created") or {}
    kind = view.intent.get("revertOf")
    if kind == KIND_DESKTOP_IMPORT:
        return [{"uid": c.get("uid")} for c in created.get("imports") or []]
    if kind == KIND_DESKTOP_DUPLICATE:
        return [{"uid": created.get("uid"), "parent": created.get("parent")}]
    return [{"uid": (m.get("to") or {}).get("uid")} for m in created.get("moves") or []]


def _check_revert_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    """What the revert was to delete: alive when any uid of an item's chain still resolves."""
    op_name = OP_BY_KIND[str(view.intent.get("revertOf"))]
    env = observe_alive(
        ctx,
        op_name,
        {
            "paths": [],
            "sources": [],
            "target": {"kind": "selection"},
            "destination": {"kind": "selection"},
        },
        _alive_items(view),
    )
    return classify_alive(env)


def _probe(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """The destination / parent read again: its entries the intent's `existing` did not list
    (with their state), and which of the sources are still found by their old uid."""
    intent = view.intent
    kind = view.desktop_kind
    target = intent.get("target")
    source = intent.get("source") or {}
    sources = intent.get("sources") or []
    args: dict[str, Any] = {
        "paths": [],
        "sources": [],
        "target": target if isinstance(target, dict) else {"kind": "selection"},
        "destination": target if isinstance(target, dict) else {"kind": "selection"},
        PHASE_ARG: PHASE_PROBE,
        EXPECTED_ARG: list(intent.get("existing") or []),
        CREATED_ARG: {
            "reelsOnly": kind == KIND_DESKTOP_DUPLICATE and source.get("type") == "PyReel",
            "withState": kind in (KIND_DESKTOP_DUPLICATE, KIND_DESKTOP_IMPORT),
            "uids": [s.get("uid") for s in sources],
        },
    }
    return observe(ctx, OP_BY_KIND[str(kind)], args)


def _paired_moves(
    sources: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Each source with the first not yet taken new entry of its type and name, None when one
    has no partner (the order the answer came in is lost, so names decide)."""
    free = list(new)
    moves: list[dict[str, Any]] = []
    for source in sources:
        partner = next(
            (
                e
                for e in free
                if e.get("type") == source.get("type") and e.get("name") == source.get("name")
            ),
            None,
        )
        if partner is None:
            return None
        free.remove(partner)
        origin = {k: source.get(k) for k in ("parent", "name", "type", "uid")}
        moves.append({"from": origin, "to": _entry_row(partner)})
    return moves


def _decide_move(view: IntentView, probe: Mapping[str, Any]) -> CheckVerdict:
    intent = view.intent
    sources = [s for s in intent.get("sources") or [] if isinstance(s, dict)]
    new = list(probe.get("new") or [])
    found = probe.get("found") or {}
    still = [uid for uid, flag in found.items() if flag]
    detail: dict[str, Any] = {"newEntries": new, "sourcesFound": found}
    if not still and len(new) == len(sources):
        moves = _paired_moves(sources, new)
        if moves is not None:
            created = {"destination": probe.get("container"), "moves": moves}
            return STATE_LANDED, "after", {**detail, "created": created}
    if len(still) == len(sources) and not new:
        return STATE_NOT_LANDED, "before", detail
    return STATE_DIVERGED, "other", {**detail, "expected": len(sources)}


def _decide_other(view: IntentView, probe: Mapping[str, Any]) -> CheckVerdict:
    new = list(probe.get("new") or [])
    detail: dict[str, Any] = {"newEntries": new}
    if not new:
        return STATE_NOT_LANDED, "before", detail
    if view.desktop_kind == KIND_DESKTOP_DUPLICATE:
        if len(new) != 1:
            return STATE_DIVERGED, "other", {**detail, "expected": 1}
        row = new[0]
        created: dict[str, Any] = {
            **_entry_row(row),
            "parent": probe.get("container"),
            "state": row.get("state"),
            "sourceUid": (view.intent.get("source") or {}).get("uid"),
        }
        return STATE_LANDED, "after", {**detail, "created": created}
    imports = [{**_entry_row(e), "state": e.get("state")} for e in new]
    created = {"destination": probe.get("container"), "imports": imports}
    return STATE_LANDED, "after", {**detail, "created": created}


def _check_write_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    env = _probe(ctx, view)
    if env.get("ok") is not True:
        if env.get("errorCode") == _NOT_FOUND_CODE:
            return STATE_DIVERGED, "other", {"detail": "the parent / destination was not found"}
        return VERDICT_UNKNOWN, str(env.get("errorCode")), {"error": env.get("error")}
    probe = env.get("result") or {}
    if view.desktop_kind == KIND_DESKTOP_MOVE:
        return _decide_move(view, probe)
    return _decide_other(view, probe)


def check_media_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """`journal.check` of an import / duplicate / move intent with no outcome."""
    return check_flow(ctx, view, _check_write_intent, _check_revert_intent)
