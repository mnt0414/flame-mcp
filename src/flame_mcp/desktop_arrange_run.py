"""`desktop.arrange`, server side.

A reel built one thing at a time: the items are applied in order, each as its own `desktop.copy`
or `desktop.move` of one source (a call that gets several sources at once folds a repeated source
into one clip and does not keep the order; one source per call does). Nothing runs in Flame beyond
those calls and two reads of the destination, so the loop is here and no generated code has one.

  1. destination   one Flame read of the destination reel (`PHASE_READ` of this op): it must be a
                   Desktop reel (not a library, a folder, a reel of a Batch group). Its entries are
                   kept, in the order Flame lists them.
  2. items         the read phase of the item's own op (`desktop.copy` / `desktop.move`) for every
                   item, before anything is written: a source that does not resolve, a move whose
                   source is in the destination or in a library, a destination the op refuses, end
                   the call with nothing written. The sources are pinned by wiretap id here, so a
                   `selection` selector means what it meant when the call began. A move gives its
                   source a new uid and wiretap id, so a source that is moved cannot be named by a
                   later item (INVALID_ARGS, found here from the uids). With `position` below the
                   number of entries, a move item whose source is an entry of the destination is
                   refused too (it is one of the entries sent behind the items), and the read
                   phase of the shift's `desktop.move` is run here as well: an entry behind
                   `position` that is not in the destination refuses the call (CONFLICT).
  3. steps         one journal group for the call (a `group` record of kind `desktop.arrange`),
                   then each item through the ordinary do path with that group: its own pre-read,
                   intent, compare-and-swap write and outcome. A step that fails stops the call;
                   the steps before it stay applied (`result.applied`, `result.stoppedAt`). One that
                   ends without an answer (TIMEOUT / BRIDGE_LOST / BRIDGE_UNAVAILABLE) is never
                   retried and shuts the write guard (writes are refused until
                   `journal.check`), as for the op alone.
  4. shift         only with `position` below the number of entries the reel held: the entries
                   from `position` on (read in step 1, pinned by the read phase of `desktop.move`
                   as the items are) are sent behind what the items made by one more
                   `desktop.move` into the same reel, in their order. `media_panel.move` into the
                   reel an entry is already in puts it at the end with a new uid (measured), so
                   the entries that were behind the insertion point end up behind the items. It is
                   the last step of the group (index = the number of items); an item that fails
                   leaves it not run. Just before it is written the entries are read again
                   (the same read phase): one that left the destination while the items ran
                   stops the call there (CONFLICT, nothing moved). A shift that moves only
                   some of its entries ends the call as a partial failure naming the ones left
                   (`result.failed.notMoved`): the reel does not get its old order back by
                   `journal.revert`.
  5. read-back     the destination is read again: its entries must be what it held before (minus
                   the entries an item moved out of it), in that order with what the items made
                   put in at `position` (else at the end), the shifted entries last, under their
                   new uids. Else FLAME_API_ERROR with `result.order` (what Flame lists) and
                   `result.expected`.

`journal.revert(group)` is `desktop_arrange_revert`: the steps newest first, each by the revert of
its own op (the shift, the last step, is put back first).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.desktop_media_run import MOVE_OP
from flame_mcp.desktop_run import COPY_OP, MAX_SOURCES, PHASE_ARG, PHASE_READ, pre_read_failure
from flame_mcp.errors import as_error_code
from flame_mcp.journal import new_id, now_iso
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.registry import RegisteredOperation
from flame_mcp.transport import elapsed_ms
from flame_mcp.types import ErrorCode

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

ARRANGE_OP: Final[str] = "desktop.arrange"
ARRANGE_GROUP_KIND: Final[str] = "desktop.arrange"
MAX_ARRANGE_ITEMS: Final[int] = 100
MAX_SHIFTED: Final[int] = MAX_SOURCES
ACTIONS: Final[tuple[str, ...]] = ("move", "copy")
CHILD_OPS: Final[dict[str, str]] = {"copy": COPY_OP, "move": MOVE_OP}
_ENTRY_TYPES: Final[tuple[str, ...]] = ("PyClip", "PySequence")
_MADE_KEYS: Final[tuple[str, ...]] = ("uid", "name", "type", "wiretapId", "selector")

MOVE_NOTE: Final[str] = (
    "A moved thing has a NEW uid and wiretap id: use result.applied[].made.selector, not the "
    "selector it had."
)
SHIFT_NOTE: Final[str] = (
    "The entries that were behind `position` have NEW uids: use "
    "result.applied[].shifted[].made.selector, not the selector they had."
)
SHIFT_PARTIAL_NOTE: Final[str] = (
    "The move of the entries behind position moved only some of them (result.failed.moved, "
    "result.failed.notMoved): journal.revert puts back the items and the entries that moved, "
    "but the ones that did not move stay where they were, so the entries that were behind "
    "position do not get their old order back."
)
SHIFT_PARTIAL_HINT: Final[str] = (
    "The move of the entries behind position moved only some of them. Not moved: {left}. "
    "journal.revert(group={group!r}) puts back the items and the entries that moved, but the "
    "reel does not get its old order back (the ones that moved end up behind the ones that "
    "did not); put the order right by hand or with desktop.move."
)
ORDER_HINT: Final[str] = (
    "The items were applied but Flame lists the reel in another order (result.order against "
    "result.expected). journal.revert(group={group!r}) puts every step back, newest first."
)
STOPPED_HINT: Final[str] = (
    "The items before the failed one stay applied (result.applied): "
    "journal.revert(group={group!r}) puts them back, newest first."
)
READ_BACK_FAILED_HINT: Final[str] = (
    "All items stay applied (result.applied): journal.revert(group={group!r}) puts them back, "
    "newest first."
)


@dataclass
class _Row:
    index: int
    action: str
    source: dict[str, Any]
    row: dict[str, Any]
    pinned: dict[str, Any]


@dataclass
class _Shift:
    """The entries behind the insertion point, sent behind the items by one move."""

    index: int
    pins: list[dict[str, Any]]
    pinned: list[dict[str, Any]]
    destination_uid: Any


@dataclass
class _Run:
    group: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    failed: dict[str, Any] | None = None
    failed_env: dict[str, Any] | None = None


def _refusal(
    op: RegisteredOperation,
    code: ErrorCode,
    message: str,
    started: float,
    *,
    result: Any = None,
    hint: str | None = None,
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=False,
        result=result,
        error=message,
        error_code=code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=elapsed_ms(started),
        hint_override=hint,
    )


def _label_error(failure: dict[str, Any], label: str) -> dict[str, Any]:
    """`failure` (from `pre_read_failure`) with the item it is about in front of its reason."""
    error = str(failure.get("error"))
    lead = "nothing was written: "
    failure["error"] = lead + label + ": " + error[len(lead) :] if error.startswith(lead) else error
    return failure


def _read_destination(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any]
) -> dict[str, Any]:
    return execution.run_op(op, {**op_args, PHASE_ARG: PHASE_READ}, run_dir=rt.run_dir)


def _pin_items(
    rt: Runtime,
    op: RegisteredOperation,
    items: list[dict[str, Any]],
    stable: dict[str, Any],
    started: float,
) -> tuple[list[_Row], dict[str, Any] | None]:
    """The read phase of every item's own op: its source pinned by wiretap id, or the envelope
    that ends the call (nothing has been written)."""
    rows: list[_Row] = []
    for index, item in enumerate(items):
        child = rt.registry.get(CHILD_OPS[item["action"]])
        if child is None:  # both are registered by register_all
            return rows, _refusal(
                op, "UNKNOWN_OPERATION", f"'{CHILD_OPS[item['action']]}' is not registered", started
            )
        pre = execution.run_op(
            child,
            {"sources": [item["source"]], "destination": stable, PHASE_ARG: PHASE_READ},
            run_dir=rt.run_dir,
        )
        label = f"items[{index}] ({item['action']})"
        if pre.get("ok") is not True or not isinstance(pre.get("result"), dict):
            return rows, _label_error(pre_read_failure(op, pre, "written"), label)
        listed = pre["result"].get("sources") or []
        found = listed[0] if listed and isinstance(listed[0], dict) else {}
        if found.get("type") not in _ENTRY_TYPES:
            return rows, _refusal(
                op,
                "INVALID_ARGS",
                f"nothing was written: {label}: the source is a {found.get('type')}, not a "
                "clip or a sequence",
                started,
                result={"ran": False, "itemIndex": index},
            )
        pinned = found.get("selector")
        rows.append(
            _Row(
                index,
                item["action"],
                item["source"],
                found,
                pinned if isinstance(pinned, dict) else item["source"],
            )
        )
    return rows, None


def _reuse_refusal(
    op: RegisteredOperation, rows: list[_Row], started: float
) -> dict[str, Any] | None:
    """A moved thing gets a new uid and wiretap id, so no later item can name it again."""
    for row in rows:
        if row.action != "move" or row.row.get("uid") is None:
            continue
        again = next(
            (later for later in rows[row.index + 1 :] if later.row.get("uid") == row.row["uid"]),
            None,
        )
        if again is None:
            continue
        return _refusal(
            op,
            "INVALID_ARGS",
            f"nothing was written: items[{again.index}] ({again.action}) uses the source that "
            f"items[{row.index}] moves, after it was moved: a moved thing gets a new uid and "
            "wiretap id (a source is moved once, and any copy of it comes before the move)",
            started,
            result={"ran": False, "itemIndex": again.index, "movedBy": row.index},
        )
    return None


def _position_refusal(
    op: RegisteredOperation,
    position: int,
    before: list[dict[str, Any]],
    pins: list[Any],
    started: float,
) -> dict[str, Any] | None:
    if position > len(before):
        return _refusal(
            op,
            "INVALID_ARGS",
            f"nothing was written: position {position} is past the end of the destination, "
            f"which holds {len(before)} entr(ies) (0 to {len(before)})",
            started,
            result={"ran": False, "existing": len(before)},
        )
    if len(pins) != len(before) or any(
        not isinstance(pin, dict) or not isinstance(pin.get("selector"), dict) for pin in pins
    ):
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            "nothing was written: an entry of the destination has no wiretap id, so it could not "
            "be sent behind the items and found again",
            started,
            result={"ran": False},
        )
    behind = len(before) - position
    if behind > MAX_SHIFTED:
        return _refusal(
            op,
            "INVALID_ARGS",
            f"nothing was written: position {position} leaves {behind} entr(ies) behind it to "
            f"send to the end with one desktop.move, which takes {MAX_SHIFTED} at most "
            f"(position at least {len(before) - MAX_SHIFTED}, or the end)",
            started,
            result={"ran": False, "existing": len(before), "behind": behind},
        )
    return None


def _position_move_refusal(
    op: RegisteredOperation, rows: list[_Row], before: list[dict[str, Any]], started: float
) -> dict[str, Any] | None:
    """With `position`, an entry the destination holds is sent behind the items, so no move item
    may take one of them."""
    held = {entry.get("uid") for entry in before}
    for row in rows:
        if row.action == "move" and row.row.get("uid") in held:
            return _refusal(
                op,
                "INVALID_ARGS",
                f"nothing was written: items[{row.index}] (move) takes an entry out of the "
                "destination, which position cannot be combined with (the entries behind the "
                "insertion point are moved after the items); leave position out to move it to "
                "the end, or copy it",
                started,
                result={"ran": False, "itemIndex": row.index},
            )
    return None


def _found_selector(found: Any, pin: dict[str, Any]) -> dict[str, Any]:
    selector = found.get("selector") if isinstance(found, dict) else None
    return selector if isinstance(selector, dict) else pin["selector"]


def _read_shift(
    rt: Runtime, tail: list[dict[str, Any]], stable: dict[str, Any]
) -> dict[str, Any] | None:
    """The read phase of the shift's own `desktop.move` (None when it is not registered)."""
    child = rt.registry.get(MOVE_OP)
    if child is None:  # registered by register_all
        return None
    return execution.run_op(
        child,
        {
            "sources": [pin["selector"] for pin in tail],
            "destination": stable,
            PHASE_ARG: PHASE_READ,
        },
        run_dir=rt.run_dir,
    )


def _shift_parent_problem(
    listed: list[Any], tail: list[dict[str, Any]], dest_uid: Any
) -> str | None:
    """Why the entries behind position are not all in the destination any more, else None."""
    if len(listed) != len(tail):
        return f"the read gave {len(listed)} source(s) for {len(tail)}"
    for found, pin in zip(listed, tail, strict=True):
        parent = found.get("parent") if isinstance(found, dict) else None
        if not isinstance(parent, dict) or parent.get("uid") != dest_uid:
            return (
                f"{pin.get('name')!r} (uid {pin.get('uid')}) is not in the destination any more "
                f"(its parent is {parent.get('name') if isinstance(parent, dict) else None!r})"
            )
    return None


def _pin_shift(
    rt: Runtime,
    op: RegisteredOperation,
    tail: list[dict[str, Any]],
    stable: dict[str, Any],
    dest_uid: Any,
    index: int,
    started: float,
) -> tuple[_Shift | None, dict[str, Any] | None]:
    """The read phase of the shift's own `desktop.move`, before anything is written: every
    entry behind position must still be in the destination."""
    pre = _read_shift(rt, tail, stable)
    if pre is None:
        return None, _refusal(op, "UNKNOWN_OPERATION", f"'{MOVE_OP}' is not registered", started)
    label = "the entries behind position (move)"
    if pre.get("ok") is not True or not isinstance(pre.get("result"), dict):
        return None, _label_error(pre_read_failure(op, pre, "written"), label)
    listed = pre["result"].get("sources") or []
    problem = _shift_parent_problem(listed, tail, dest_uid)
    if problem is not None:
        return None, _refusal(
            op,
            "CONFLICT",
            f"nothing was written: {label}: {problem}; the destination changed after it was read",
            started,
            result={"ran": False},
        )
    pinned = [_found_selector(found, pin) for found, pin in zip(listed, tail, strict=True)]
    return _Shift(index, tail, pinned, dest_uid), None


def _shift_still_behind(rt: Runtime, shift: _Shift, stable: dict[str, Any], run: _Run) -> bool:
    """Read the entries behind position again just before the shift is written: they may have
    left the destination while the items ran. False, with the failure kept in `run` as for a
    step, when one did (the shift is then not started)."""
    pre = _read_shift(rt, shift.pins, stable)
    problem: str | None
    code: Any = "CONFLICT"
    if pre is None or pre.get("ok") is not True or not isinstance(pre.get("result"), dict):
        problem = str((pre or {}).get("error") or "the read of the entries behind position failed")
        code = (pre or {}).get("errorCode") or "FLAME_API_ERROR"
    else:
        problem = _shift_parent_problem(
            pre["result"].get("sources") or [], shift.pins, shift.destination_uid
        )
    if problem is None:
        return True
    run.failed = {
        "index": shift.index,
        "action": "move",
        "errorCode": code,
        "error": f"nothing was moved: {problem}",
        "shift": True,
    }
    return False


def _made(action: str, env: dict[str, Any]) -> dict[str, Any] | None:
    result = env.get("result") or {}
    if action == "copy":
        made = (result.get("copies") or [None])[0]
    else:
        made = ((result.get("moved") or [None])[0] or {}).get("to")
    if not isinstance(made, dict):
        return None
    return {key: made.get(key) for key in _MADE_KEYS}


def _applied_row(row: _Row, env: dict[str, Any]) -> dict[str, Any]:
    journal = env.get("journal") or {}
    return {
        "index": row.index,
        "action": row.action,
        "source": {"name": row.row.get("name"), "uid": row.row.get("uid")},
        "made": _made(row.action, env),
        "intent": journal.get("id"),
    }


def _shift_row(shift: _Shift, env: dict[str, Any]) -> dict[str, Any]:
    journal = env.get("journal") or {}
    moved = {
        (m.get("from") or {}).get("uid"): m.get("to")
        for m in (env.get("result") or {}).get("moved") or []
        if isinstance(m, dict)
    }
    shifted = []
    for pin in shift.pins:
        made = moved.get(pin.get("uid"))
        shifted.append(
            {
                "source": {"name": pin.get("name"), "uid": pin.get("uid")},
                "made": (
                    {key: made.get(key) for key in _MADE_KEYS} if isinstance(made, dict) else None
                ),
            }
        )
    return {
        "index": shift.index,
        "action": "move",
        "shift": True,
        "shifted": shifted,
        "intent": journal.get("id"),
    }


def _name_uid(row: Any) -> dict[str, Any]:
    origin = row.get("from") if isinstance(row, dict) else None
    origin = origin if isinstance(origin, dict) else {}
    return {"name": origin.get("name"), "uid": origin.get("uid")}


def _partial_shift(env: dict[str, Any]) -> dict[str, Any]:
    """`moved` and `notMoved` (name and uid before the move) of a shift that moved only some of
    its entries; nothing for any other failure."""
    result = env.get("result")
    if not isinstance(result, dict) or result.get("partial") is not True:
        return {}
    return {
        "moved": [_name_uid(m) for m in result.get("moved") or []],
        "notMoved": [
            {**_name_uid(m), "uidGone": m.get("uidGone")} for m in result.get("notMoved") or []
        ],
    }


def _call_step(
    rt: Runtime,
    run: _Run,
    index: int,
    action: str,
    args: dict[str, Any],
    *,
    shift: bool = False,
) -> dict[str, Any] | None:
    """One child op in the call's group: its envelope when it succeeded, else None with the
    failure kept in `run`."""
    env = rt.call(CHILD_OPS[action], args, group=run.group)
    journal = env.get("journal") or {}
    if journal.get("id"):
        run.intents.append(str(journal["id"]))
    where = "shift" if shift else f"items[{index}]"
    run.degraded += [f"{where}: {note}" for note in env.get("degraded") or []]
    if env.get("ok") is True:
        return env
    run.failed_env = env
    run.failed = {
        "index": index,
        "action": action,
        "errorCode": env.get("errorCode"),
        "error": env.get("error"),
    }
    if shift:
        run.failed["shift"] = True
        run.failed.update(_partial_shift(env))
    return None


def _run_steps(
    rt: Runtime, rows: list[_Row], shift: _Shift | None, stable: dict[str, Any], run: _Run
) -> None:
    for row in rows:
        env = _call_step(
            rt, run, row.index, row.action, {"sources": [row.pinned], "destination": stable}
        )
        if env is None:
            return
        run.applied.append(_applied_row(row, env))
    if shift is None or not _shift_still_behind(rt, shift, stable, run):
        return
    env = _call_step(
        rt,
        run,
        shift.index,
        "move",
        {"sources": shift.pinned, "destination": stable},
        shift=True,
    )
    if env is not None:
        run.applied.append(_shift_row(shift, env))


def _with_degraded(env: dict[str, Any], run: _Run) -> dict[str, Any]:
    """`env` with the notes of the steps (`items[i]: ...`) added to its `degraded`."""
    if run.degraded:
        env["degraded"] = [*env.get("degraded", []), *run.degraded]
    return env


def _journal_ref(run: _Run) -> dict[str, Any] | None:
    if not run.intents:
        return None
    return {
        "group": run.group,
        "id": run.intents[0],
        "ids": list(run.intents),
        "revert": ({"op": "journal.revert", "args": {"group": run.group}} if run.applied else None),
    }


def _rows_of(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": e.get("name"), "uid": e.get("uid")} for e in entries]


def _body(
    destination: dict[str, Any],
    items: int,
    existing: list[dict[str, Any]],
    run: _Run,
    position: int | None,
    shift: _Shift | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "destination": destination,
        "requested": items,
        "existing": len(existing),
        "applied": run.applied,
    }
    if position is not None:
        body["position"] = position
    if shift is not None:
        body["shiftedBehind"] = len(shift.pins)
    if any(a["action"] == "move" for a in run.applied):
        body["notes"] = [MOVE_NOTE]
    if any(a.get("shift") for a in run.applied):
        body["notes"] = [*body.get("notes", []), SHIFT_NOTE]
    return body


def _stopped(
    op: RegisteredOperation,
    body: dict[str, Any],
    run: _Run,
    started: float,
    total: int,
) -> dict[str, Any]:
    failed = run.failed or {}
    index = int(failed.get("index", 0))
    body["stoppedAt"] = index
    body["failed"] = failed
    body["notRun"] = list(range(index + 1, total))
    unknown = failed.get("errorCode") in UNKNOWN_END_CODES
    hint = str((run.failed_env or {}).get("hint") or "") or None
    if run.applied and not unknown:
        hint = f"{hint or ''} {STOPPED_HINT.format(group=run.group)}".strip()
    left = failed.get("notMoved")
    if failed.get("shift") and left is not None and not unknown:
        names = ", ".join(f"{r.get('name')!r} (uid {r.get('uid')})" for r in left) or "none named"
        hint = f"{hint or ''} {SHIFT_PARTIAL_HINT.format(left=names, group=run.group)}".strip()
        body["notes"] = [*body.get("notes", []), SHIFT_PARTIAL_NOTE]
    done = len(run.applied)
    env = envelope_mod.build_envelope(
        ok=False,
        result=body,
        error=(
            f"stopped at {'the shift move' if failed.get('shift') else f'items[{index}]'} "
            f"({failed.get('action')}): {failed.get('error')}"
            + (f" -- the {done} item(s) before it stay applied" if done else "")
        ),
        error_code=as_error_code(str(failed.get("errorCode"))),
        affected=done,
        partial={"done": done, "failed": 1, "notRun": total - index - 1} if done else None,
        journal=_journal_ref(run),
        execution=envelope_mod.execution_meta(op),
        duration_ms=elapsed_ms(started),
        hint_override=hint,
    )
    return _with_degraded(env, run)


def _expected(before: list[dict[str, Any]], run: _Run) -> list[dict[str, Any]]:
    """What the destination should list after the steps: what it held (less what an item moved
    out of it), the items' results put in at `position` (else at the end), the shifted entries
    last under their new uids."""
    items = [a for a in run.applied if not a.get("shift")]
    shift = next((a for a in run.applied if a.get("shift")), None)
    moved_out = {a["source"]["uid"] for a in items if a["action"] == "move"}
    kept = [row for row in _rows_of(before) if row["uid"] not in moved_out]
    made = _rows_of([a["made"] or {} for a in items])
    if shift is None:
        return [*kept, *made]
    behind = {s["source"]["uid"] for s in shift["shifted"]}
    head = [row for row in kept if row["uid"] not in behind]
    return [*head, *made, *_rows_of([s["made"] or {} for s in shift["shifted"]])]


def _verify(
    rt: Runtime,
    op: RegisteredOperation,
    op_args: dict[str, Any],
    body: dict[str, Any],
    before: list[dict[str, Any]],
    run: _Run,
    started: float,
) -> dict[str, Any] | None:
    """The destination read again against the order the items should have left: None when it
    matches, else the failure envelope (the steps stay applied and journaled)."""
    after_env = _read_destination(rt, op, op_args)
    expected = _expected(before, run)
    if after_env.get("ok") is not True or not isinstance(after_env.get("result"), dict):
        body["orderMatches"] = None
        body["expected"] = expected
        return _with_degraded(
            envelope_mod.build_envelope(
                ok=False,
                result=body,
                error=(
                    f"all {len(run.applied)} item(s) were applied but the destination could not be "
                    f"read back: {after_env.get('error')}"
                ),
                error_code=as_error_code(str(after_env.get("errorCode"))),
                affected=len(run.applied),
                journal=_journal_ref(run),
                execution=envelope_mod.execution_meta(op),
                duration_ms=elapsed_ms(started),
                hint_override=READ_BACK_FAILED_HINT.format(group=run.group),
            ),
            run,
        )
    listing = _rows_of(after_env["result"].get("entries") or [])
    body["order"] = listing
    body["orderMatches"] = [r["uid"] for r in listing] == [e["uid"] for e in expected]
    if body["orderMatches"]:
        return None
    body["expected"] = expected
    return _with_degraded(
        envelope_mod.build_envelope(
            ok=False,
            result=body,
            error=(
                "the items were applied but Flame lists the reel in another order than the items "
                "(result.order, result.expected)"
            ),
            error_code="FLAME_API_ERROR",
            affected=len(run.applied),
            journal=_journal_ref(run),
            execution=envelope_mod.execution_meta(op),
            duration_ms=elapsed_ms(started),
            hint_override=ORDER_HINT.format(group=run.group),
        ),
        run,
    )


def _open_group(
    rt: Runtime,
    op: RegisteredOperation,
    rows: list[_Row],
    shift: _Shift | None,
    group: str,
    started: float,
) -> dict[str, Any] | None:
    try:
        rt.journal.append(
            {
                "type": "group",
                "group": group,
                "ts": now_iso(),
                "kind": ARRANGE_GROUP_KIND,
                "label": None,
                "steps": [*(r.action for r in rows), *(["move"] if shift else [])],
            }
        )
    except OSError as exc:
        return _refusal(
            op,
            "POLICY_DENIED",
            f"nothing was written: the journal cannot be written ({exc}), and this server does "
            "not write to the project without a record",
            started,
            result={"ran": False},
            hint=f"Make {rt.journal.directory} writable, then retry.",
        )
    return None


def run_arrange(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `desktop.arrange`."""
    started = time.monotonic()
    items: list[dict[str, Any]] = list(op_args["items"])
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
        destination: dict[str, Any] = dest_env["result"].get("destination") or {}
        stable = destination.get("selector")
        if not isinstance(stable, dict):
            return _refusal(
                op,
                "PRECONDITION_UNMET",
                "nothing was written: the destination has no wiretap id, so it cannot be found "
                "again",
                started,
                result={"ran": False},
            )
        before: list[dict[str, Any]] = list(dest_env["result"].get("entries") or [])
        position: int | None = op_args.get("position")
        pins: list[Any] = list(dest_env["result"].get("pins") or [])
        refusal = (
            None
            if position is None
            else _position_refusal(op, int(position), before, pins, started)
        )
        if refusal is not None:
            return refusal

        rows, refusal = _pin_items(rt, op, items, stable, started)
        refusal = refusal or _reuse_refusal(op, rows, started)
        if refusal is None and position is not None and position < len(before):
            refusal = _position_move_refusal(op, rows, before, started)
        if refusal is not None:
            return refusal
        shift: _Shift | None = None
        if position is not None and position < len(before):
            shift, refusal = _pin_shift(
                rt, op, pins[position:], stable, destination.get("uid"), len(items), started
            )
            if refusal is not None:
                return refusal

        run = _Run(group=group if group is not None else new_id("g"))
        unwritable = _open_group(rt, op, rows, shift, run.group, started)
        if unwritable is not None:
            return unwritable
        _run_steps(rt, rows, shift, stable, run)
        body = _body(destination, len(items), before, run, position, shift)
        if run.failed is not None:
            return _stopped(op, body, run, started, len(items) + (1 if shift else 0))
        mismatch = _verify(rt, op, op_args, body, before, run, started)
        if mismatch is not None:
            return mismatch
        env = envelope_mod.build_envelope(
            ok=True,
            result=body,
            affected=len(run.applied),
            journal=_journal_ref(run),
            execution=envelope_mod.execution_meta(op),
            duration_ms=elapsed_ms(started),
        )
        return _with_degraded(env, run)
