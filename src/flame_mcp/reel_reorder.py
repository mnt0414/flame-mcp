"""Which entries of a reel have to move to give it another order (`desktop.reorder`).

Pure Python, no Flame. `media_panel.move` into the reel an entry is in puts it at the end of the
reel with a new uid (measured), so a reorder is a move of some entries to the end in the wanted
order, and every entry that is moved gets a new uid. Entries that are not moved keep theirs, so
the fewest moves keep the most uids: `moves_to_reorder` returns the smallest set that can be moved
to the end, in order, to reach `desired`."""

from __future__ import annotations

from collections.abc import Sequence


def moves_to_reorder(current: Sequence[str], desired: Sequence[str]) -> list[str]:
    """The uids to move to the end of the reel, in this order, to turn `current` into `desired`.

    The entries of the longest prefix of `desired` that already stands in that relative order
    in `current` stay; the rest are moved in the order of `desired`. Empty when the order is
    already right. Raises ValueError unless `desired` holds exactly the entries of `current`."""
    if len(set(current)) != len(current) or len(set(desired)) != len(desired):
        raise ValueError("a uid is listed twice")
    if set(current) != set(desired):
        raise ValueError("desired must hold exactly the entries of the reel")
    place = {uid: i for i, uid in enumerate(current)}
    kept = 0
    last = -1
    for uid in desired:
        if place[uid] <= last:
            break
        last = place[uid]
        kept += 1
    return list(desired[kept:])
