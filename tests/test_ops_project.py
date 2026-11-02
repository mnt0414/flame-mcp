"""Tests for `project.info` and `project.save`.

The generated code is run directly against `fake_media_tree.FakeFlame` (no bridge, no socket)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import build_default_tree
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from journal_test_helpers import Rig, make_rig
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


def workspace_of(rig: Rig) -> Any:
    return rig.flame.projects.current_project.current_workspace


def test_project_save_commits_once_and_reports_what_it_did(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    env = rig.do("project.save", {})

    assert env["ok"] is True, env
    assert workspace_of(rig).commit_count == 1
    result = env["result"]
    assert set(result) == {
        "projectName",
        "workspaceName",
        "elapsedS",
        "returned",
        "readBackDecisive",
    }
    assert result["projectName"] == "TestProject"
    assert result["workspaceName"] == "Workspace"
    assert result["returned"] == "None"
    assert result["readBackDecisive"] is False
    assert isinstance(result["elapsedS"], float) and result["elapsedS"] >= 0
    assert round(result["elapsedS"], 3) == result["elapsedS"]
    assert env["undoSteps"] == 0


def test_project_save_leaves_no_journal_record_like_batch_save_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)

    env = rig.do("project.save", {})

    assert env["journal"] is None and env["execution"]["undo"] == "unsupported"
    assert rig.records() == []


def test_project_save_turns_a_commit_exception_into_flame_api_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)

    def commit() -> None:
        raise RuntimeError("commit boom")

    workspace_of(rig).commit = commit

    env = rig.do("project.save", {})

    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "commit boom" in str(env["error"])


def test_project_save_is_denied_by_readonly_without_calling_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = rig.do("project.save", {})

    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert workspace_of(rig).commit_count == 0


@pytest.mark.parametrize("readonly", [False, True])
def test_project_save_cannot_be_a_plan_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, readonly: bool
) -> None:
    env_vars = {"FLAME_MCP_READONLY": "1"} if readonly else {}
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env(env_vars))

    env = rig.do("plan.run", {"steps": [{"op": "project.save", "args": {}}]})

    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert "cannot be a plan step" in str(env["result"])
    assert workspace_of(rig).commit_count == 0


def test_project_save_is_a_create_effect_with_unsupported_undo() -> None:
    registry = make_registry()
    op = registry.get("project.save")
    assert op is not None
    assert op.effect == "create"
    assert op.undo == "unsupported"
    assert op.thread == "main-async"
    assert op.params == ()
    assert op.undo_steps({}, 5) == 0
    assert "readBackDecisive" in op.description
