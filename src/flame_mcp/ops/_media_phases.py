"""The Flame-side phases `mediahub.import`, `desktop.duplicate` and `desktop.move` share:
the alive check (`journal.check` of a revert) and the probe (`journal.check` of a write)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp.desktop_run import CREATED_ARG, EXPECTED_ARG
from flame_mcp.ops._common import build_source


def alive_source(args: Mapping[str, Any]) -> str:
    items = list(args[CREATED_ARG]["items"])
    return build_source(f"_result = _dm_alive({items!r})", "_affected = 0")


def probe_source(args: Mapping[str, Any], container: str) -> str:
    """`container` is the variable the selector resolved to (a destination, or a parent)."""
    known = list(args.get(EXPECTED_ARG) or [])
    flags = dict(args.get(CREATED_ARG) or {})
    return build_source(f"_result = _dm_probe({container}, {known!r}, {flags!r})", "_affected = 0")
