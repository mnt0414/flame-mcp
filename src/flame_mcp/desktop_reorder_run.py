"""`desktop.reorder`, server side.

A reel put into another order by the fewest moves (`reel_reorder.moves_to_reorder`): a move into
the reel an entry is in puts it at the end with a new uid, an entry that is not moved keeps its
uid, so only the entries that cannot stay are sent to the end, in the wanted order, by one
`desktop.move` into the same reel.

  1. read       the destination reel (`PHASE_READ` of this op: a Desktop reel, not a library, a
                folder or a reel of a Batch group) with its entries in the order Flame lists them,
                each with the selector that finds it again.
  2. check      `order` must name every entry exactly once (by uid or by wiretap selector); the
                entries to move must fit one `desktop.move`. Nothing is written when not. An order
                the reel has already ends the call here: ok, moved 0, nothing journaled.
  3. write      one journal group (a `group` record of kind `desktop.reorder` that keeps the uids
                of the reel before), then one `desktop.move` of those entries into the reel
                through the ordinary do path: its own pre-read, intent, compare-and-swap write and
                outcome. One that ends without an answer (TIMEOUT / BRIDGE_LOST /
                BRIDGE_UNAVAILABLE) is never retried and shuts the write guard.
  4. read-back  the reel is read again: it must list the wanted order, with the new uids of the
                entries that moved. Else FLAME_API_ERROR with `result.after` and `result.expected`.

`journal.revert(group)` is `desktop_reorder_revert`: the same kind of move, back to the order the
group recorded.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.desktop_media_run import MOVE_OP
from flame_mcp.desktop_run import MAX_SOURCES, PHASE_ARG, PHASE_READ, pre_read_failure
from flame_mcp.errors import as_error_code
from flame_mcp.journal import new_id, now_iso
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.reel_reorder import moves_to_reorder
from flame_mcp.registry import RegisteredOperation
from flame_mcp.transport import elapsed_ms
from flame_mcp.types import ErrorCode

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

REORDER_OP: Final[str] = "desktop.reorder"
REORDER_GROUP_KIND: Final[str] = "desktop.reorder"
MAX_REORDER_ENTRIES: Final[int] = 500
MAX_REORDER_MOVES: Final[int] = MAX_SOURCES

REORDER_NOTE: Final[str] = (
    "Every entry that was moved has a NEW uid and wiretap id: use result.uidMap[].selector (or "
    "result.after), not the selector it had. The entries that were not moved keep theirs."
)
ORDER_HINT: Final[str] = (
    "The move was applied but Flame lists the reel in another order (result.after against "
    "result.expected). journal.revert(group={group!r}) puts the old order back."
)
STOPPED_HINT: Final[str] = (
    "Part of the move may have landed: journal.revert(group={group!r}) puts back the entries "
    "that moved, in the old order."
)
READ_BACK_FAILED_HINT: Final[str] = (
    "The move was applied (result.uidMap): journal.revert(group={group!r}) puts the old order back."
)


def _refusal(
    op: RegisteredOperation,
    code: ErrorCode,
    message: str,
    started: float,
    *,
    result: Any = None,
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=False,
        result=result if result is not None else {"ran": False},
        error="nothing was written: " + message,
        error_code=code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=elapsed_ms(started),
    )


def _rows_of(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": e.get("name"), "uid": e.get("uid")} for e in entries]


def _read_destination(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any]
) -> dict[str, Any]:
    return execution.run_op(op, {**op_args, PHASE_ARG: PHASE_READ}, run_dir=rt.run_dir)


def resolve_order(
    order: list[Any], entries: list[dict[str, Any]]
) -> tuple[list[str], dict[str, list[Any]]]:
    """The uids `order` names (a uid string or a wiretap selector each) and what is wrong with
    it: `unknown` (names no entry), `twice` (names an entry again), `missing` (entries it leaves
    out, by name)."""
    by_uid = {str(e.get("uid")): e for e in entries}
    by_wiretap = {
        str((e.get("selector") or {}).get("id")): e
        for e in entries
        if isinstance(e.get("selector"), dict)
    }
    uids: list[str] = []
    problems: dict[str, list[Any]] = {"unknown": [], "twice": [], "missing": []}
    for item in order:
        key = item if isinstance(item, str) else str(item.get("id"))
        entry = (by_uid if isinstance(item, str) else by_wiretap).get(key)
        if entry is None:
            problems["unknown"].append(key)
        elif str(entry.get("uid")) in uids:
            problems["twice"].append(entry.get("name"))
        else:
            uids.append(str(entry.get("uid")))
    problems["missing"] = [e.get("name") for e in entries if str(e.get("uid")) not in uids]
    return uids, problems


def _order_problem(problems: dict[str, list[Any]]) -> str | None:
    parts = []
    if problems["unknown"]:
        parts.append(f"unknown entr(ies) {problems['unknown']!r}")
    if problems["twice"]:
        parts.append(f"listed twice {problems['twice']!r}")
    if problems["missing"]:
        parts.append(f"missing from order {problems['missing']!r}")
    return "; ".join(parts) or None


def _journal_ref(group: str, ids: list[str], *, revertible: bool) -> dict[str, Any] | None:
    if not ids:
        return None
    return {
        "group": group,
        "id": ids[0],
        "ids": ids,
        "revert": {"op": "journal.revert", "args": {"group": group}} if revertible else None,
    }


def _uid_map(moved: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for pair in moved:
        origin = pair.get("from") or {}
        made = pair.get("to") or {}
        rows.append(
            {
                "name": made.get("name"),
                "from": origin.get("uid"),
                "to": made.get("uid"),
                "selector": made.get("selector"),
            }
        )
    return rows


def _open_group(
    rt: Runtime, op: RegisteredOperation, group: str, before: list[str], started: float
) -> dict[str, Any] | None:
    try:
        rt.journal.append(
            {
                "type": "group",
                "group": group,
                "ts": now_iso(),
                "kind": REORDER_GROUP_KIND,
                "label": None,
                "steps": ["move"],
                "before": before,
            }
        )
    except OSError as exc:
        return _refusal(
            op,
            "POLICY_DENIED",
            f"the journal cannot be written ({exc}), and this server does not write to the "
            "project without a record",
            started,
        )
    return None


def _failed(
    op: RegisteredOperation,
    body: dict[str, Any],
    env: dict[str, Any],
    group: str,
    started: float,
) -> dict[str, Any]:
    """The envelope of a move that failed: its code, partial and hint, with the journal ref."""
    journal = env.get("journal") or {}
    ids = [str(journal["id"])] if journal.get("id") else []
    code = env.get("errorCode")
    hint = str(env.get("hint") or "") or None
    revertible = bool(journal.get("revert"))
    if revertible and code not in UNKNOWN_END_CODES:
        hint = f"{hint or ''} {STOPPED_HINT.format(group=group)}".strip()
    body["failed"] = {"errorCode": code, "error": env.get("error")}
    return envelope_mod.build_envelope(
        ok=False,
        result=body,
        error=f"the move of the entries to the end of the reel failed: {env.get('error')}",
        error_code=as_error_code(str(code)),
        partial=env.get("partial"),
        journal=_journal_ref(group, ids, revertible=revertible),
        execution=envelope_mod.execution_meta(op),
        duration_ms=elapsed_ms(started),
        hint_override=hint,
    )


def _check_read(
    dest_env: dict[str, Any], op: RegisteredOperation, started: float
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    destination = dest_env["result"].get("destination") or {}
    entries = list(dest_env["result"].get("entries") or [])
    stable = destination.get("selector")
    if not isinstance(stable, dict):
        return {}, _refusal(op, "PRECONDITION_UNMET", "the destination has no wiretap id", started)
    if any(not isinstance(e.get("selector"), dict) or e.get("uid") is None for e in entries):
        return {}, _refusal(
            op,
            "PRECONDITION_UNMET",
            "an entry of the destination has no uid or wiretap id, so it could not be moved and "
            "found again",
            started,
        )
    return {"destination": destination, "stable": stable, "entries": entries}, None


def run_reorder(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `desktop.reorder`."""
    started = time.monotonic()
    with rt.write_lock:
        denial = rt.guard.denial()
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=elapsed_ms(started),
                hint=denial.hint,
            )
        dest_env = _read_destination(rt, op, op_args)
        if dest_env.get("ok") is not True or not isinstance(dest_env.get("result"), dict):
            return pre_read_failure(op, dest_env, "written")
        read, refusal = _check_read(dest_env, op, started)
        if refusal is not None:
            return refusal
        entries: list[dict[str, Any]] = read["entries"]
        current = [str(e["uid"]) for e in entries]
        desired, problems = resolve_order(list(op_args["order"]), entries)
        problem = _order_problem(problems)
        if problem is not None:
            return _refusal(
                op,
                "INVALID_ARGS",
                f"order must name every entry of the reel once: {problem}",
                started,
                result={"ran": False, "entries": _rows_of(entries), **problems},
            )
        moves = moves_to_reorder(current, desired)
        body: dict[str, Any] = {
            "destination": read["destination"],
            "requested": len(desired),
            "before": _rows_of(entries),
            "moved": 0,
            "uidMap": [],
        }
        if not moves:
            return envelope_mod.build_envelope(
                ok=True,
                result={**body, "after": _rows_of(entries), "orderMatches": True},
                execution=envelope_mod.execution_meta(op),
                duration_ms=elapsed_ms(started),
            )
        if len(moves) > MAX_REORDER_MOVES:
            return _refusal(
                op,
                "INVALID_ARGS",
                f"this order needs {len(moves)} entries moved to the end, and one desktop.move "
                f"takes {MAX_REORDER_MOVES} at most",
                started,
                result={"ran": False, "toMove": len(moves)},
            )
        pins = {str(e["uid"]): e["selector"] for e in entries}
        run_group = group if group is not None else new_id("g")
        unwritable = _open_group(rt, op, run_group, current, started)
        if unwritable is not None:
            return unwritable
        env = rt.call(
            MOVE_OP,
            {"sources": [pins[uid] for uid in moves], "destination": read["stable"]},
            group=run_group,
        )
        if env.get("ok") is not True:
            return _failed(op, body, env, run_group, started)
        return _verified(rt, op, op_args, body, desired, env, run_group, started)


def _verified(
    rt: Runtime,
    op: RegisteredOperation,
    op_args: dict[str, Any],
    body: dict[str, Any],
    desired: list[str],
    move_env: dict[str, Any],
    group: str,
    started: float,
) -> dict[str, Any]:
    """The reel read again against the wanted order, after the move went through."""
    journal = move_env.get("journal") or {}
    ids = [str(journal["id"])] if journal.get("id") else []
    uid_map = _uid_map((move_env.get("result") or {}).get("moved") or [])
    renamed = {row["from"]: row["to"] for row in uid_map}
    expected = [renamed.get(uid, uid) for uid in desired]
    body.update({"moved": len(uid_map), "uidMap": uid_map, "notes": [REORDER_NOTE]})
    degraded = [f"move: {note}" for note in move_env.get("degraded") or []]
    after_env = _read_destination(rt, op, op_args)
    common: dict[str, Any] = {
        "affected": len(uid_map),
        "journal": _journal_ref(group, ids, revertible=True),
        "execution": envelope_mod.execution_meta(op),
        "duration_ms": elapsed_ms(started),
    }
    if after_env.get("ok") is not True or not isinstance(after_env.get("result"), dict):
        body["orderMatches"] = None
        return envelope_mod.build_envelope(
            ok=False,
            result=body,
            error=(
                "the move was applied but the destination could not be read back: "
                f"{after_env.get('error')}"
            ),
            error_code=as_error_code(str(after_env.get("errorCode"))),
            hint_override=READ_BACK_FAILED_HINT.format(group=group),
            **common,
        )
    after = _rows_of(after_env["result"].get("entries") or [])
    body["after"] = after
    body["orderMatches"] = [r["uid"] for r in after] == expected
    if not body["orderMatches"]:
        env = envelope_mod.build_envelope(
            ok=False,
            result=_with_expected_names(body, uid_map, desired),
            error="the move was applied but Flame lists the reel in another order than the "
            "order asked for (result.after, result.expected)",
            error_code="FLAME_API_ERROR",
            hint_override=ORDER_HINT.format(group=group),
            **common,
        )
    else:
        env = envelope_mod.build_envelope(ok=True, result=body, **common)
    if degraded:
        env["degraded"] = [*env.get("degraded", []), *degraded]
    return env


def _with_expected_names(
    body: dict[str, Any], uid_map: list[dict[str, Any]], desired: list[str]
) -> dict[str, Any]:
    """`body` with `expected` as {name, uid} rows under the uids the entries have after the move."""
    names = {r["uid"]: r["name"] for r in body["before"]}
    renamed = {row["from"]: row["to"] for row in uid_map}
    body["expected"] = [{"name": names.get(u), "uid": renamed.get(u, u)} for u in desired]
    return body
