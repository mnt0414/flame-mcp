"""Loads the Flame API symbol table.

Registry uses it to validate `requires` / `diagnose` declarations at registration time.

The symbol file lists the `Class.member` names of Flame 2025.2.4's Python API and is checked into
src/flame_mcp/data/.
"""

from __future__ import annotations

import json
from collections.abc import Set as AbstractSet
from importlib import resources

_SYMBOLS_FILE = "api_symbols_2025.2.4.json"

_default_symbols_cache: frozenset[str] | None = None


def load_symbols() -> frozenset[str]:
    """Reads the bundled symbol file fresh (no caching)."""
    data = resources.files("flame_mcp").joinpath("data", _SYMBOLS_FILE).read_text(encoding="utf-8")
    return frozenset(json.loads(data))


def default_symbols() -> frozenset[str]:
    """Returns the bundled symbol table, loaded once and cached for the process."""
    global _default_symbols_cache
    if _default_symbols_cache is None:
        _default_symbols_cache = load_symbols()
    return _default_symbols_cache


def has_symbol(symbol: str, symbols: AbstractSet[str] | None = None) -> bool:
    return symbol in (symbols if symbols is not None else default_symbols())
