"""Tests for `project.info`.

The generated code is run directly against `fake_media_tree.FakeFlame` (no bridge, no socket)."""

from __future__ import annotations

from fake_media_tree import build_default_tree
from flame_mcp.ops import register_all
from flame_mcp.registry import Registry
from op_test_helpers import run_op


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


def test_project_info_reports_name_workspace_library_count_and_desktop() -> None:
    registry = make_registry()
    flame, _handles = build_default_tree()

    result = run_op(registry, "project.info", {}, flame)

    assert result["opError"] is None
    assert result["value"] == {
        "projectName": "TestProject",
        "workspaceName": "Workspace",
        "libraryCount": 1,
        "currentDesktopName": "Desktop",
    }


def test_project_info_library_count_reflects_workspace_libraries() -> None:
    from fake_media_tree import FakeFlame, PyLibrary

    registry = make_registry()
    libs = [
        PyLibrary("A", uid="a", wiretap_id="/a"),
        PyLibrary("B", uid="b", wiretap_id="/b"),
    ]
    flame = FakeFlame(libraries=libs)

    result = run_op(registry, "project.info", {}, flame)

    assert result["value"]["libraryCount"] == 2


def test_project_info_is_read_effect_zero_undo() -> None:
    registry = make_registry()
    op = registry.get("project.info")
    assert op is not None
    assert op.effect == "read"
    assert op.undo == "none"
    assert op.undo_steps({}, 5) == 0
