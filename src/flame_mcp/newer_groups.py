"""Which later groups stand between `journal.revert` and the group it was asked to put back.

A checkpointed group (a Batch group's `load_setup`, a sequence's `segment.trim` ...) is put back by
restoring its checkpoint, which overwrites the whole target. Anything written to the same target
after the group would go with it, and the fingerprint the revert compares sees only part of a
target (a Batch group's nodes and wiring, not attribute values; a sequence's segments and effects,
not a segment's comment or a clip's format). So `journal.revert` looks at the journal first:
`newer_groups` lists the groups written after `group` to the same target that landed (or applied
part of their steps, or whose result is unknown or diverged) and were not reverted, newest first.
They are what the caller should put back first.

The rule covers a checkpointed group, a `desktop.reformat` in place, and the groups whose revert
deletes or moves back what they made (`timeline.create_sequence`, `segment.match` /
`copy_to_media_panel`, `desktop.move`). It does not cover `desktop.delete` (nothing can be written
to what was deleted), the copy / import / duplicate / create groups (they have a compare-and-swap of
their own) and `desktop.arrange`.

The same target is, for a Batch group, the same Batch uid: the `target` of a checkpointed write
(`{kind: batch, uid}`), of a node / Action node write (`{kind: node, batch: {uid}}`) or of a
rename of the group. For a sequence / clip it is the same chain of uids and wiretap ids: a restore
or a `desktop.reformat` gives the target a new uid and its outcome records `replacedUid` ->
`targetUid`, and an identity records its uid with its wiretap id, so the chain is followed through
every such link, in both directions. Groups that only make something new (a copy, an import, a
duplicate, a created sequence, a `segment.match`, a reformat into a duplicate) are not writes to
their source.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any, Final

from flame_mcp.checkpoints import TIMELINE_KIND, kind_named
from flame_mcp.journal import (
    KIND_DESKTOP_COPY,
    KIND_DESKTOP_CREATE,
    KIND_DESKTOP_DUPLICATE,
    KIND_DESKTOP_EXTRACT,
    KIND_DESKTOP_IMPORT,
    KIND_DESKTOP_MOVE,
    KIND_DESKTOP_REFORMAT,
    KIND_DESKTOP_SEQUENCE,
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_PARTIAL,
    STATE_UNKNOWN,
    Index,
    IntentView,
    Record,
)

NEWER_GROUPS_KEY: Final[str] = "newerGroups"

_MAKES_NEW: Final[frozenset[str]] = frozenset(
    {
        KIND_DESKTOP_COPY,
        KIND_DESKTOP_CREATE,
        KIND_DESKTOP_DUPLICATE,
        KIND_DESKTOP_EXTRACT,
        KIND_DESKTOP_IMPORT,
        KIND_DESKTOP_SEQUENCE,
    }
)
"""Intents that make a new thing and leave their source as it was."""
_IDENT_FIELDS: Final[tuple[str, ...]] = ("targetIdent", "source", "created")


def _is_timeline(view: IntentView) -> bool:
    return kind_named(view.intent.get("targetKind")) is TIMELINE_KIND


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _batch_uid(selector: Any) -> str | None:
    """The Batch group uid a stable selector points into: a batch, a node of it, a node inside an
    Action node."""
    while isinstance(selector, dict):
        kind = selector.get("kind")
        if kind == "batch":
            return _text(selector.get("uid"))
        selector = selector.get("batch") if kind == "node" else selector.get("node")
    return None


def _wiretap_of(selector: Any) -> str | None:
    """The wiretap id a stable selector points at: a wiretap, or the sequence a segment is in."""
    while isinstance(selector, dict):
        if selector.get("kind") == "wiretap":
            return _text(selector.get("id"))
        selector = selector.get("parent")
    return None


def _ident_pairs(value: Any) -> list[tuple[str, str]]:
    """(uid, wiretap id) of an identity dict (and of the sequences / clips listed in it)."""
    if isinstance(value, list):
        return [pair for item in value for pair in _ident_pairs(item)]
    if not isinstance(value, dict):
        return []
    pairs = _ident_pairs(value.get("sequence"))
    uid, wiretap = _text(value.get("uid")), _text(value.get("wiretapId"))
    if uid and wiretap:
        pairs.append((uid, wiretap))
    return pairs


def _ident_keys(value: Any) -> set[str]:
    if isinstance(value, list):
        return {key for item in value for key in _ident_keys(item)}
    if not isinstance(value, dict):
        return set()
    keys = _ident_keys(value.get("sequence"))
    keys.update(k for k in (_text(value.get("uid")), _text(value.get("wiretapId"))) if k)
    return keys


def _moves(record: Record) -> list[dict[str, Any]]:
    """`{from, to}` rows of a `desktop.move` outcome: what moved, and who it is afterwards."""
    created = record.get("created")
    rows = created.get("moves") if isinstance(created, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _link_table(records: Iterable[Record]) -> dict[str, set[str]]:
    """key -> the keys joined to it, both ways: a restore's / reformat's `replacedUid` ->
    `targetUid`, and the uid with the wiretap id of every identity a record holds."""
    linked: dict[str, set[str]] = {}

    def join(a: str, b: str) -> None:
        linked.setdefault(a, set()).add(b)
        linked.setdefault(b, set()).add(a)

    for record in records:
        kind = record.get("type")
        if kind == "outcome":
            old, new = _text(record.get("replacedUid")), _text(record.get("targetUid"))
            if old and new:
                join(old, new)
        if kind == "outcome":
            for move in _moves(record):
                from_uid = _text((move.get("from") or {}).get("uid"))
                for uid, wiretap in _ident_pairs(move.get("to")):
                    join(uid, wiretap)
                    if from_uid:
                        join(from_uid, uid)
        if kind in ("intent", "outcome"):
            for name in _IDENT_FIELDS:
                for uid, wiretap in _ident_pairs(record.get(name)):
                    join(uid, wiretap)
            sources = record.get("sources")
            for uid, wiretap in _ident_pairs(sources):
                join(uid, wiretap)
        elif kind == "checkpoint":
            for uid, wiretap in _ident_pairs(record.get("source")):
                join(uid, wiretap)
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


def _timeline_keys(view: IntentView) -> set[str]:
    """The uids and wiretap ids of the sequences / clips a write of `view` acted on."""
    intent = view.intent
    outcome = view.outcome or {}
    keys: set[str] = set()
    for value in (
        intent.get("targetUid"),
        outcome.get("targetUid"),
        outcome.get("replacedUid"),
        _wiretap_of(intent.get("target")),
    ):
        if text := _text(value):
            keys.add(text)
    for name in (*_IDENT_FIELDS, "sources"):
        keys |= _ident_keys(intent.get(name))
        keys |= _ident_keys(outcome.get(name))
    return keys


def _made_keys(view: IntentView) -> set[str]:
    """Who the thing a make / move left behind is: the sequence a `create_sequence` made, the clip
    a `segment.match` / `copy_to_media_panel` made, the things a `desktop.move` put in the
    destination (its intent's `target` is the destination, not one of them)."""
    outcome = view.outcome or {}
    keys = _ident_keys(outcome.get("created"))
    for move in _moves(outcome):
        keys |= _ident_keys(move.get("to"))
    return keys


def _own_keys(view: IntentView) -> set[str] | None:
    """The keys of what a revert of `view` acts on, None when `view` is a kind of group the
    newer-group rule does not cover (the revert has a check of its own)."""
    kind = view.intent.get("kind")
    if view.revert_checkpoint() is not None or (
        kind == KIND_DESKTOP_REFORMAT and view.intent.get("mode") == "in_place"
    ):
        return _timeline_keys(view)
    if kind in (KIND_DESKTOP_SEQUENCE, KIND_DESKTOP_EXTRACT, KIND_DESKTOP_MOVE):
        return _made_keys(view)
    return None


def _batch_uids(view: IntentView) -> set[str]:
    uid = _batch_uid(view.intent.get("target"))
    return {uid} if uid else set()


def _writes_source(view: IntentView) -> bool:
    kind = view.intent.get("kind")
    if kind in _MAKES_NEW:
        return False
    return not (kind == KIND_DESKTOP_REFORMAT and view.intent.get("mode") != "in_place")


def _stands(index: Index, group: str) -> bool:
    """A group that may still be in the way: it landed (or a bundled plan applied part of its
    steps), or its result is not known (`unknown`, or a check found the target `diverged`: a
    write may well have gone through), it is not itself a revert, and nothing has reverted it."""
    views = index.groups[group]
    if any(isinstance(view.intent.get("reverts"), str) for view in views):
        return False
    if index.is_reverted(group):
        return False
    return any(
        view.state() in (STATE_DIVERGED, STATE_UNKNOWN)
        or (view.state() in (STATE_LANDED, STATE_PARTIAL) and view.is_revertible())
        for view in views
    )


def newer_groups(records: list[Record], index: Index, group: str) -> list[dict[str, Any]]:
    """The groups written after `group` to the same target that landed (or whose result is not
    known) and have not been reverted, newest first, as `{group, op, ts}`; [] when `group` is not
    a kind the rule covers (a checkpointed group, a `desktop.reformat` in place, a made sequence
    or clip, a `desktop.move`) or nothing qualifies. Value writes and a `desktop.reformat` in
    place count as well as the checkpointed writes; reads, reverts and reverted groups do not."""
    views = index.groups.get(group)
    if not views:
        return []
    batch = any(v.revert_checkpoint() is not None and not _is_timeline(v) for v in views)
    own = [keys for v in views if (keys := _own_keys(v)) is not None]
    if not own:
        return []
    order = list(index.groups)
    later = order[order.index(group) + 1 :]
    wanted_keys: set[str] = set()
    wanted_batches: set[str] = set()
    if batch:
        wanted_batches = {uid for view in views for uid in _batch_uids(view)}
    else:
        wanted_keys = _component(set().union(*own), _link_table(records))
    found: list[dict[str, Any]] = []
    for other in later:
        if not _stands(index, other):
            continue
        writes = [v for v in index.groups[other] if _writes_source(v)]
        if batch:
            same = any(_batch_uids(v) & wanted_batches for v in writes)
        else:
            same = any(_timeline_keys(v) & wanted_keys for v in writes)
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
