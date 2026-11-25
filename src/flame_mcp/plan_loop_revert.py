"""`journal.revert` of the parent group of a `plan.for_each` write loop.

The parent holds no intent. Its children are the groups whose `group` record holds it as
`parent`, in the order the loop ran them, and the revert goes through them newest first, each by
the revert of its own kind (`journal.revert(child)`, so a Timeline / Batch checkpoint is restored,
a `desktop.arrange` group is put back step by step, and so on). Like the revert of a
`desktop.arrange` group, a child that cannot be put back stops it: the answer says how far it got
and what remains, nothing is retried or forced on its own, and a second call starts again from the
first child that is not back. A child whose output cannot be put back (a render) is not touched
and comes back under `kept`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.journal import Index
from flame_mcp.journal_records import GroupRecord
from flame_mcp.types import ServerOpError, ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

RevertChild = Callable[[Mapping[str, Any], "ServerContext"], ServerResult]
CONTINUE_HINT: Final[str] = (
    "journal.revert(group={parent!r}) again goes on from this child (the ones already back are "
    "skipped); nothing is retried or forced on its own."
)


def _row(record: GroupRecord, **extra: Any) -> dict[str, Any]:
    return {
        "group": record.get("group"),
        "key": record.get("key"),
        "op": record.get("op"),
        "step": record.get("step"),
        **extra,
    }


def _reason_to_skip(index: Index, child: str) -> str | None:
    if index.is_reverted(child):
        return "already put back"
    if not any(view.is_revertible() for view in index.groups[child]):
        return "it applied nothing a revert could put back"
    return None


def _refuse_in_doubt(index: Index, parent: str) -> None:
    doubt = index.in_doubt(parent)
    if not doubt:
        return
    raise ServerOpError(
        "PRECONDITION_UNMET",
        f"group {parent}: {len(doubt)} intent(s) of its children have no outcome yet (state "
        "unknown); nothing was put back",
        result={
            "reverted": parent,
            "inDoubt": [{"group": v.group, "id": v.id, "op": v.intent.get("op")} for v in doubt],
        },
        hint=f"Run journal.check(group={parent!r}) first, then journal.revert again.",
    )


def _split_children(
    index: Index, parent: str
) -> tuple[list[GroupRecord], list[dict[str, Any]], list[dict[str, Any]]]:
    """The children newest first, sorted into (to put back, skipped, kept as they are)."""
    todo: list[GroupRecord] = []
    skipped: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for record in reversed(index.loop_child_records(parent)):
        child = str(record.get("group"))
        if child not in index.groups:  # recorded, but nothing was written (even a render)
            skipped.append(_row(record, skipped="nothing of it was written"))
        elif record.get("revertible") is False:
            kept.append(_row(record))
        elif (why := _reason_to_skip(index, child)) is not None:
            skipped.append(_row(record, skipped=why))
        else:
            todo.append(record)
    return todo, skipped, kept


@dataclass
class _Sweep:
    """What a revert of the parent has done so far."""

    skipped: list[dict[str, Any]]
    kept: list[dict[str, Any]]
    done: list[dict[str, Any]] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    """The groups the children's reverts journaled, in order."""

    @property
    def affected(self) -> int:
        return sum(int(d.get("affected") or 0) for d in self.done)

    def journal_ref(self, also: str | None = None) -> dict[str, Any] | None:
        found = [*self.groups, *([also] if also is not None else [])]
        return {"group": found[-1], "groups": found} if found else None


def _group_of(journal: Mapping[str, Any] | None) -> str | None:
    group = journal.get("group") if journal is not None else None
    return None if group is None else str(group)


def _stopped_error(
    parent: str,
    sweep: _Sweep,
    record: GroupRecord,
    remaining: list[GroupRecord],
    exc: ServerOpError,
) -> ServerOpError:
    rows = [_row(r) for r in remaining]
    failure = _row(record, errorCode=exc.code, reason=exc.message, result=exc.result)
    return ServerOpError(
        exc.code,
        f"child {record.get('group')} (item {record.get('key')!r}, "
        f"{record.get('op')}) could not be put back: {exc.message} -- "
        f"{len(sweep.done)} child(ren) were put back, {len(rows)} remain (result.remaining)",
        result={
            "reverted": parent,
            "children": sweep.done,
            "stoppedAt": failure,
            "remaining": rows,
            "skipped": sweep.skipped,
            "kept": sweep.kept,
        },
        hint=CONTINUE_HINT.format(parent=parent),
        affected=sweep.affected,
        partial={"done": len(sweep.done), "failed": 1, "notRun": len(rows) - 1},
        journal=sweep.journal_ref(_group_of(exc.journal)),
    )


def revert_loop_group(
    ctx: ServerContext, index: Index, parent: str, force: bool, revert_child: RevertChild
) -> ServerResult:
    """The caller holds `ctx.write_lock`."""
    _refuse_in_doubt(index, parent)
    todo, skipped, kept = _split_children(index, parent)
    if not todo:
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"group {parent}: no child has anything left to put back "
            f"({len(skipped)} already back or empty, {len(kept)} kept as they are)",
            result={"reverted": parent, "children": [], "skipped": skipped, "kept": kept},
        )
    sweep = _Sweep(skipped, kept)
    for position, record in enumerate(todo):
        try:
            outcome = revert_child({"group": record["group"], "force": force}, ctx)
        except ServerOpError as exc:
            raise _stopped_error(parent, sweep, record, todo[position:], exc) from exc
        body = outcome.result if isinstance(outcome.result, dict) else {}
        sweep.done.append(
            _row(record, reverted=True, affected=outcome.affected, revert=body.get("group"))
        )
        group = _group_of(outcome.journal)
        if group is not None:
            sweep.groups.append(group)
    return ServerResult(
        result={
            "reverted": parent,
            "children": sweep.done,
            "skipped": skipped,
            "kept": kept,
            "note": (
                "The children listed under `kept` made output that cannot be put back; they were "
                "not touched."
                if kept
                else None
            ),
        },
        affected=sweep.affected,
        journal=sweep.journal_ref(),
    )
