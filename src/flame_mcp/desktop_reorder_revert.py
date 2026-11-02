"""`journal.revert` of a `desktop.reorder` group.

`journal.check` uses the check of the group's `desktop_move` intent.

The group holds one `desktop_move` and the uids the reel had before (`before` of the group
record). The revert is the same kind of move: the reel is read, the recorded uids are mapped to
the uids the entries have now (the move gave the entries it moved new ones), and the fewest
entries (`reel_reorder.moves_to_reorder`) are moved to the end of the reel in the old order. That
can include entries the reorder left alone, which then get new uids too. The move itself is the
revert of `desktop_media_run` with its list of moves replaced, so it keeps the compare-and-swap,
outcome and fail-closed handling of a revert. A reel that no longer holds exactly those entries
is CONFLICT and nothing is moved.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.desktop_media_run import MEDIA_REVERT
from flame_mcp.desktop_reorder_run import REORDER_OP
from flame_mcp.desktop_revert import PreRevert, observe, revert_flow
from flame_mcp.desktop_run import PHASE_ARG, PHASE_READ
from flame_mcp.journal import Index, IntentView
from flame_mcp.reel_reorder import moves_to_reorder
from flame_mcp.types import ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext


def _conflict(message: str, code: str = "CONFLICT") -> PreRevert:
    return PreRevert(
        failure={
            "ok": False,
            "errorCode": code,
            "error": f"nothing was moved back: {message}",
            "result": {"reverted": []},
            "hint": "Put the reel's entries back as the reorder left them, then revert again.",
        }
    )


def _old_uids(ctx: ServerContext, view: IntentView) -> list[str] | None:
    record = Index(ctx.journal.read_all().records).group_meta.get(view.group, {})
    before = record.get("before")
    if isinstance(before, list) and all(isinstance(uid, str) for uid in before):
        return before
    return None


def _revert_moves(
    entries: list[dict[str, Any]], order: list[str], parent: Any
) -> list[dict[str, Any]]:
    by_uid = {str(e["uid"]): e for e in entries}
    return [
        {
            "from": {
                "parent": parent,
                "name": by_uid[uid].get("name"),
                "type": by_uid[uid].get("type"),
                "uid": uid,
            },
            "to": {
                "name": by_uid[uid].get("name"),
                "type": by_uid[uid].get("type"),
                "uid": uid,
            },
        }
        for uid in order
    ]


def _pre_revert(
    ctx: ServerContext, view: IntentView, created: dict[str, Any], new_group: str
) -> PreRevert | None:
    """Replaces `created["moves"]` by the entries to move back, read from the reel as it is now."""
    old = _old_uids(ctx, view)
    moves = created.get("moves") or []
    if old is None or not moves:
        return _conflict("the group did not record the order of the reel", "PRECONDITION_UNMET")
    env = observe(
        ctx,
        REORDER_OP,
        {"destination": view.intent.get("target"), "order": [], PHASE_ARG: PHASE_READ},
    )
    if env.get("ok") is not True:
        return PreRevert(failure=env)
    entries = list((env.get("result") or {}).get("entries") or [])
    renamed = {m["from"]["uid"]: m["to"]["uid"] for m in moves}
    wanted = [renamed.get(uid, uid) for uid in old]
    now = [str(e.get("uid")) for e in entries]
    if sorted(wanted) != sorted(now):
        return _conflict(
            f"the reel holds {len(now)} entr(ies) and the reorder left {len(wanted)}: it is not "
            "the set of entries the old order can be rebuilt from"
        )
    todo = moves_to_reorder(now, wanted)
    created["moves"] = _revert_moves(entries, todo, moves[0]["from"].get("parent"))
    return None


REORDER_REVERT: Final = replace(MEDIA_REVERT, pre_revert=_pre_revert)


def revert_reorder_group(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView]
) -> ServerResult:
    """`journal.revert` of a `desktop.reorder` group. The caller holds `ctx.write_lock` and has
    checked that the group is not reverted already."""
    return revert_flow(ctx, index, group, views, REORDER_REVERT)
