"""Which later groups stand between a CONFLICT of `journal.revert` and the group it refused.

A checkpointed group (a Batch group's `load_setup`, a sequence's `segment.trim` ...) is put back
only while its target still has the fingerprint the group left. When a later write changed the
same target, the revert ends in CONFLICT; `newer_groups` lists the groups of that later write that
have not been reverted, newest first, so the caller can put those back first.

The same target is, for a Batch group, the same stable `target` selector (the group by uid); for a
sequence / clip, the same chain of uids: a restore gives the target a new uid and its outcome
records `replacedUid` -> `targetUid` (`timeline_restore.uid_chain`), so the uids of the older
group are followed through every such link, in both directions.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any, Final

from flame_mcp.checkpoints import TIMELINE_KIND, kind_named
from flame_mcp.journal import STATE_LANDED, Index, IntentView, Record

NEWER_GROUPS_KEY: Final[str] = "newerGroups"


def _is_timeline(view: IntentView) -> bool:
    return kind_named(view.intent.get("targetKind")) is TIMELINE_KIND


def _link_table(records: Iterable[Record]) -> dict[str, set[str]]:
    """uid -> the uids joined to it by a restore's `replacedUid` -> `targetUid`, both ways."""
    linked: dict[str, set[str]] = {}
    for record in records:
        if record.get("type") != "outcome":
            continue
        old, new = record.get("replacedUid"), record.get("targetUid")
        if isinstance(old, str) and isinstance(new, str):
            linked.setdefault(old, set()).add(new)
            linked.setdefault(new, set()).add(old)
    return linked


def _component(seed: set[str], linked: dict[str, set[str]]) -> set[str]:
    seen = set(seed)
    queue = deque(seed)
    while queue:
        for other in linked.get(queue.popleft(), ()):
            if other not in seen:
                seen.add(other)
                queue.append(other)
    return seen


def _uids_of(views: Iterable[IntentView]) -> set[str]:
    found: set[str] = set()
    for view in views:
        outcome = view.outcome or {}
        for value in (
            view.intent.get("targetUid"),
            outcome.get("targetUid"),
            outcome.get("replacedUid"),
        ):
            if isinstance(value, str) and value:
                found.add(value)
    return found


def _stands(index: Index, group: str) -> bool:
    """A group a caller could still put back: it landed, it is not itself a revert, and nothing
    has reverted it."""
    views = index.groups[group]
    if any(isinstance(view.intent.get("reverts"), str) for view in views):
        return False
    if index.is_reverted(group):
        return False
    return any(view.state() == STATE_LANDED and view.is_revertible() for view in views)


def newer_groups(records: list[Record], index: Index, group: str) -> list[dict[str, Any]]:
    """The groups written after `group` to the same target that have not been reverted, newest
    first, as `{group, op, ts}`; [] when `group` is not a checkpointed one or nothing qualifies."""
    views = index.groups.get(group)
    if not views or not any(view.revert_checkpoint() is not None for view in views):
        return []
    order = list(index.groups)
    later = order[order.index(group) + 1 :]
    timeline = any(_is_timeline(view) for view in views)
    wanted_uids = _component(_uids_of(views), _link_table(records)) if timeline else set[str]()
    wanted_targets = [v.intent["target"] for v in views if isinstance(v.intent.get("target"), dict)]
    found: list[dict[str, Any]] = []
    for other in later:
        if not _stands(index, other):
            continue
        other_views = [v for v in index.groups[other] if v.revert_checkpoint() is not None]
        if timeline:
            same = any(_is_timeline(v) for v in other_views) and bool(
                _uids_of(other_views) & wanted_uids
            )
        else:
            same = any(
                not _is_timeline(v) and v.intent.get("target") in wanted_targets
                for v in other_views
            )
        if same:
            first = index.groups[other][0].intent
            found.append({"group": other, "op": first.get("op"), "ts": first.get("ts")})
    found.reverse()
    return found


def newer_groups_sentence(rows: list[dict[str, Any]]) -> str:
    names = ", ".join(str(row["group"]) for row in rows)
    return (
        f" Later group(s) of the same target have not been reverted: revert them first, newest "
        f"first ({names}), then this one."
    )
