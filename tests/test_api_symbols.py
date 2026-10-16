from __future__ import annotations

from flame_mcp import api_symbols


def test_default_symbols_contains_known_names() -> None:
    symbols = api_symbols.default_symbols()
    assert "flame.schedule_idle_event" in symbols
    assert "PyClip.name" in symbols
    assert "PyClip.get_wiretap_node_id" in symbols


def test_has_symbol_uses_default_when_no_set_given() -> None:
    assert api_symbols.has_symbol("flame.schedule_idle_event") is True
    assert api_symbols.has_symbol("PyClip.nonexistent_typo") is False


def test_has_symbol_respects_explicit_set() -> None:
    custom = frozenset({"Foo.bar"})
    assert api_symbols.has_symbol("Foo.bar", custom) is True
    assert api_symbols.has_symbol("flame.schedule_idle_event", custom) is False


def test_load_symbols_returns_sorted_unique_strings() -> None:
    symbols = api_symbols.load_symbols()
    assert isinstance(symbols, frozenset)
    assert len(symbols) > 0
    assert all(isinstance(s, str) for s in symbols)
