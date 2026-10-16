"""Depth ladder for step-wise disclosure (layer 1 of the response limit)."""

from __future__ import annotations

from typing import assert_never

from flame_mcp.types import Depth


def next_level(depth: Depth) -> Depth | None:
    match depth:
        case "library":
            return "reel"
        case "reel":
            return "clip"
        case "clip":
            return "version"
        case "version":
            return "track"
        case "track":
            return "segment"
        case "segment":
            return None
        case _ as unreachable:
            assert_never(unreachable)
