"""`library.*` tests: list, run directly against `fake_media_tree.FakeFlame`
(no bridge)."""

from __future__ import annotations

from fake_media_tree import FakeFlame, PyLibrary, build_default_tree
from flame_mcp.ops import register_all
from flame_mcp.registry import Registry
from op_test_helpers import run_op


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


def test_list_libraries_returns_the_one_library() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    result = run_op(registry, "library.list", {"limit": 50}, flame)
    assert result["opError"] is None
    assert result["value"]["total"] == 1
    assert result["value"]["items"][0]["name"] == "P1O"
    assert result["value"]["items"][0]["type"] == "PyLibrary"
    assert result["value"]["items"][0]["uid"] == "uid-lib"


def test_list_libraries_limit_truncates() -> None:
    registry = make_registry()
    libs = [PyLibrary(f"Lib{i}", uid=f"u{i}", wiretap_id=f"/l{i}") for i in range(5)]
    flame = FakeFlame(libraries=libs)
    result = run_op(registry, "library.list", {"limit": 2}, flame)
    assert result["value"]["total"] == 5
    assert result["value"]["shown"] == 2
    assert result["value"]["truncated"] is True


def test_list_offset_returns_the_second_page() -> None:
    registry = make_registry()
    libs = [PyLibrary(f"Lib{i}", uid=f"u{i}", wiretap_id=f"/l{i}") for i in range(5)]
    flame = FakeFlame(libraries=libs)
    result = run_op(registry, "library.list", {"limit": 2, "offset": 2}, flame)
    value = result["value"]
    assert [i["name"] for i in value["items"]] == ["Lib2", "Lib3"]
    assert (value["total"], value["offset"], value["shown"], value["truncated"]) == (5, 2, 2, True)


def test_list_offset_last_page_is_not_truncated() -> None:
    registry = make_registry()
    libs = [PyLibrary(f"Lib{i}", uid=f"u{i}", wiretap_id=f"/l{i}") for i in range(5)]
    flame = FakeFlame(libraries=libs)
    result = run_op(registry, "library.list", {"limit": 2, "offset": 4}, flame)
    value = result["value"]
    assert [i["name"] for i in value["items"]] == ["Lib4"]
    assert value["truncated"] is False


def test_list_offset_defaults_to_zero() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    result = run_op(registry, "library.list", {"limit": 50}, flame)
    assert result["value"]["offset"] == 0
