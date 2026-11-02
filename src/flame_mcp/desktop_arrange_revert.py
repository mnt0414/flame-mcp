"""`journal.revert` of a `desktop.arrange` group.

`journal.check` uses the check of each step's own op.

A group holds one intent per item, a `desktop_copy` or a `desktop_move`, and with `position` one
more `desktop_move` last, the shift of the entries that were behind it (`desktop_arrange_run`).
The revert goes through them newest first and puts each back by the revert of its own op
(`desktop_revert.DEFAULT_REVERT` deletes the copies, `desktop_media_run.MEDIA_REVERT` moves the
things back), each as a revert group of its own. A step that cannot be put back (a copy that was
edited since, a moved thing that is gone) leaves the others reverted and a second revert does the
rest (`Index.intent_reverted` tells which intents a revert already covered). Newest first is also
what gives a source reel its order back when the moved things were its last ones: each goes to the
end of its old parent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from flame_mcp.desktop_media_run import MEDIA_REVERT, MOVE_REVERT_NOTE
from flame_mcp.desktop_revert import (
    DEFAULT_REVERT,
    RevertProfile,
    failure_code,
    revert_flow,
    unresolved_hint,
    unresolved_of,
)
from flame_mcp.journal import KIND_DESKTOP_COPY, KIND_DESKTOP_MOVE, Index, IntentView
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.types import ServerOpError, ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

INDEPENDENT_CODES: Final[frozenset[str]] = frozenset(
    {"CONFLICT", "PRECONDITION_UNMET", "SELECTOR_EMPTY", "INVALID_ARGS"}
)
"""Failures that touched nothing: the other steps are still put back after one of these."""
_PROFILES: Final[dict[str, RevertProfile]] = {
    KIND_DESKTOP_COPY: DEFAULT_REVERT,
    KIND_DESKTOP_MOVE: MEDIA_REVERT,
}
_COPY_KEYS: Final[tuple[str, ...]] = ("deleted", "skipped")


def _nothing_to_revert(group: str) -> ServerOpError:
    return ServerOpError(
        "PRECONDITION_UNMET",
        f"group {group} made nothing that a revert could undo (no item was applied, or no "
        "outcome yet)",
    )


def _step_row(position: int, view: IntentView, body: dict[str, Any]) -> dict[str, Any]:
    is_copy = view.desktop_kind == KIND_DESKTOP_COPY
    row: dict[str, Any] = {
        "index": position,
        "action": "copy" if is_copy else "move",
        "group": body.get("group"),
    }
    if is_copy:
        row.update({key: body[key] for key in _COPY_KEYS if key in body})
    elif "reverted" in body:
        row["reverted"] = body["reverted"]
    return row


def revert_arrange_group(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView]
) -> ServerResult:
    """`journal.revert` of a group whose intents are the steps of a `desktop.arrange`. The caller
    holds `ctx.write_lock` and has checked that the group is not reverted already."""
    positions = {view.id: position for position, view in enumerate(views)}
    writes = [v for v in views if v.desktop_kind in _PROFILES and v.is_revertible()]
    if not writes:
        raise _nothing_to_revert(group)
    todo = [v for v in reversed(writes) if not index.intent_reverted(v)]
    if not todo:
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"group {group}: every item this call applied was put back already (by "
            f"{', '.join(index.revert_groups_of(group))})",
        )
    steps: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    groups: list[Any] = []
    stopped_at = len(todo)
    for number, view in enumerate(todo):
        position = positions[view.id]
        try:
            done = revert_flow(ctx, index, group, [view], _PROFILES[str(view.desktop_kind)])
        except ServerOpError as exc:
            failures.append(
                {
                    "index": position,
                    "errorCode": exc.code,
                    "reason": exc.message,
                    "result": exc.result,
                    "group": (exc.journal or {}).get("group"),
                }
            )
            if exc.code in UNKNOWN_END_CODES:
                groups.append((exc.journal or {}).get("group"))
            if exc.code not in INDEPENDENT_CODES:
                stopped_at = number + 1
                break
            continue
        body = dict(done.result) if isinstance(done.result, dict) else {}
        steps.append(_step_row(position, view, body))
        groups.append((done.journal or {}).get("group"))
    result: dict[str, Any] = {
        "reverted": group,
        "steps": steps,
        "notReverted": failures,
        "partial": bool(failures and steps),
    }
    if any(s["action"] == "move" and s.get("reverted") for s in steps):
        result["notes"] = [MOVE_REVERT_NOTE]
    journal = {"group": groups[-1], "groups": groups} if groups else None
    if failures:
        unresolved = unresolved_of(failures)
        if unresolved:
            result["unresolved"] = unresolved
        done_count = len(steps)
        raise ServerOpError(
            failure_code(failures),
            "; ".join(f"item {f['index']}: {f['reason']}" for f in failures)
            + (
                f" -- {done_count} item(s) were put back, {len(failures)} were not "
                "(result.notReverted); a second journal.revert puts back only the rest"
                if done_count
                else ""
            ),
            result=result,
            hint=unresolved_hint(unresolved),
            affected=done_count,
            partial=(
                {"done": done_count, "failed": len(failures), "notRun": len(todo) - stopped_at}
                if done_count
                else None
            ),
            journal=journal,
        )
    return ServerResult(result=result, affected=len(steps), journal=journal)
