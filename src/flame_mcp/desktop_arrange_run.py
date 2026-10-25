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
                   later item (INVALID_ARGS, found here from the uids).
  3. steps         one journal group for the call (a `group` record of kind `desktop.arrange`),
                   then each item through the ordinary do path with that group: its own pre-read,
                   intent, compare-and-swap write and outcome. A step that fails stops the call;
                   the steps before it stay applied (`result.applied`, `result.stoppedAt`). One that
                   ends without an answer (TIMEOUT / BRIDGE_LOST / BRIDGE_UNAVAILABLE) is never
                   retried and shuts the write guard (writes are refused until
                   `journal.check`), as for the op alone.
  4. read-back     the destination is read again: its entries must be what it held before, in
                   that order, then what each step made, in the order of the items. Else
                   FLAME_API_ERROR with `result.order` (what Flame lists) and `result.expected`.

`journal.revert(group)` is `desktop_arrange_revert`: the steps newest first, each by the revert of
its own op.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.desktop_media_run import MOVE_OP
from flame_mcp.desktop_run import COPY_OP, PHASE_ARG, PHASE_READ, pre_read_failure
from flame_mcp.errors import as_error_code
from flame_mcp.journal import new_id, now_iso
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import ErrorCode

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

ARRANGE_OP: Final[str] = "desktop.arrange"
ARRANGE_GROUP_KIND: Final[str] = "desktop.arrange"
MAX_ARRANGE_ITEMS: Final[int] = 100
ACTIONS: Final[tuple[str, ...]] = ("move", "copy")
CHILD_OPS: Final[dict[str, str]] = {"copy": COPY_OP, "move": MOVE_OP}
_ENTRY_TYPES: Final[tuple[str, ...]] = ("PyClip", "PySequence")
_MADE_KEYS: Final[tuple[str, ...]] = ("uid", "name", "type", "wiretapId", "selector")

MOVE_NOTE: Final[str] = (
    "A moved thing has a NEW uid and wiretap id: use result.applied[].made.selector, not the "
    "selector it had."
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
class _Run:
    group: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    failed: dict[str, Any] | None = None
    failed_env: dict[str, Any] | None = None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


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
        duration_ms=_elapsed_ms(started),
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


def _run_steps(rt: Runtime, rows: list[_Row], stable: dict[str, Any], run: _Run) -> None:
    for row in rows:
        env = rt.call(
            CHILD_OPS[row.action],
            {"sources": [row.pinned], "destination": stable},
            group=run.group,
        )
        journal = env.get("journal") or {}
        if journal.get("id"):
            run.intents.append(str(journal["id"]))
        run.degraded += [f"items[{row.index}]: {note}" for note in env.get("degraded") or []]
        if env.get("ok") is True:
            run.applied.append(_applied_row(row, env))
            continue
        run.failed_env = env
        run.failed = {
            "index": row.index,
            "action": row.action,
            "errorCode": env.get("errorCode"),
            "error": env.get("error"),
        }
        return


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
    destination: dict[str, Any], items: int, existing: list[dict[str, Any]], run: _Run
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "destination": destination,
        "requested": items,
        "existing": len(existing),
        "applied": run.applied,
    }
    if any(a["action"] == "move" for a in run.applied):
        body["notes"] = [MOVE_NOTE]
    return body


def _stopped(
    op: RegisteredOperation,
    body: dict[str, Any],
    run: _Run,
    started: float,
) -> dict[str, Any]:
    failed = run.failed or {}
    index = int(failed.get("index", 0))
    total = int(body["requested"])
    body["stoppedAt"] = index
    body["failed"] = failed
    body["notRun"] = list(range(index + 1, total))
    unknown = failed.get("errorCode") in UNKNOWN_END_CODES
    hint = str((run.failed_env or {}).get("hint") or "") or None
    if run.applied and not unknown:
        hint = f"{hint or ''} {STOPPED_HINT.format(group=run.group)}".strip()
    done = len(run.applied)
    env = envelope_mod.build_envelope(
        ok=False,
        result=body,
        error=(
            f"stopped at items[{index}] ({failed.get('action')}): {failed.get('error')}"
            + (f" -- the {done} item(s) before it stay applied" if done else "")
        ),
        error_code=as_error_code(str(failed.get("errorCode"))),
        affected=done,
        partial={"done": done, "failed": 1, "notRun": total - index - 1} if done else None,
        journal=_journal_ref(run),
        execution=envelope_mod.execution_meta(op),
        duration_ms=_elapsed_ms(started),
        hint_override=hint,
    )
    return _with_degraded(env, run)


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
    made = [a["made"] or {} for a in run.applied]
    expected = [*_rows_of(before), *_rows_of(made)]
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
                duration_ms=_elapsed_ms(started),
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
            duration_ms=_elapsed_ms(started),
            hint_override=ORDER_HINT.format(group=run.group),
        ),
        run,
    )


def _open_group(
    rt: Runtime, op: RegisteredOperation, rows: list[_Row], group: str, started: float
) -> dict[str, Any] | None:
    try:
        rt.journal.append(
            {
                "type": "group",
                "group": group,
                "ts": now_iso(),
                "kind": ARRANGE_GROUP_KIND,
                "label": None,
                "steps": [r.action for r in rows],
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
                duration_ms=_elapsed_ms(started),
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

        rows, refusal = _pin_items(rt, op, items, stable, started)
        refusal = refusal or _reuse_refusal(op, rows, started)
        if refusal is not None:
            return refusal

        run = _Run(group=group if group is not None else new_id("g"))
        unwritable = _open_group(rt, op, rows, run.group, started)
        if unwritable is not None:
            return unwritable
        _run_steps(rt, rows, stable, run)
        body = _body(destination, len(items), before, run)
        if run.failed is not None:
            return _stopped(op, body, run, started)
        mismatch = _verify(rt, op, op_args, body, before, run, started)
        if mismatch is not None:
            return mismatch
        env = envelope_mod.build_envelope(
            ok=True,
            result=body,
            affected=len(run.applied),
            journal=_journal_ref(run),
            execution=envelope_mod.execution_meta(op),
            duration_ms=_elapsed_ms(started),
        )
        return _with_degraded(env, run)
