"""Which kinds of reel a `batch.info` result holds under a name (used by `batch.add_clips`' check
and by `plan.run`, which refuses a step into a shelf reel)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def reel_kinds_named(info: Mapping[str, Any], reel: str, wanted: str | None) -> list[str]:
    """The kinds ('schematic' / 'shelf') of the reels of a `batch.info` result that are named
    `reel`, among those of kind `wanted` (any kind when None)."""
    return [
        kind
        for kind, key in (("schematic", "reels"), ("shelf", "shelfReels"))
        if wanted in (None, kind)
        for row in info.get(key) or []
        if row.get("name") == reel
    ]
