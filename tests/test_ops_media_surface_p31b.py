"""Tests for the three media ops in plan.run, under the read-only policy.

Also their declarations and the code they generate. See media_p31b_helpers.py for the rig and the
fake."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import fake_media_tree
from flame_mcp import execution
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from journal_test_helpers import Rig, make_rig
from media_p31b_helpers import (
    ALL_OPS,
    C1,
    CLIP1,
    DUP,
    IMPORT,
    MOVE,
    REEL1,
    dup,
    imp,
    make_media_rig,
    mv,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_media_rig(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    ("op", "args"),
    [
        (IMPORT, {"paths": ["/x/a.mov"], "destination": REEL1}),
        (DUP, {"target": C1}),
        (MOVE, {"sources": [C1], "destination": REEL1}),
    ],
)
def test_plan_run_refuses_all_three(rig: Rig, op: str, args: dict[str, Any]) -> None:
    env = rig.do("plan.run", {"steps": [{"op": op, "args": args}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert f"{op} cannot be a plan step" in env["result"]["issues"][0]["problems"][0]
    assert env["result"]["ran"] is False and rig.exec.calls == []


def test_readonly_policy_denies_all_three(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    assert imp(rig, ["/x/a.mov"], REEL1)["errorCode"] == "POLICY_DENIED"
    assert dup(rig, C1)["errorCode"] == "POLICY_DENIED"
    assert mv(rig, [C1], REEL1)["errorCode"] == "POLICY_DENIED"
    assert rig.exec.calls == []


def test_declarations_match_what_the_ops_do() -> None:
    from flame_mcp import desktop_media_run
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    for name in ALL_OPS:
        op = registry.get(name)
        assert op is not None
        assert (op.effect, op.thread, op.undo) == ("create", "main", "value")
        assert op.undo_steps({}, 3) == 0 and op.wrap is desktop_media_run.run_media_write
        assert (op.precheck is not None) == (name != DUP)
        assert set(op.accepts) == {"wiretap", "path", "selection"}
        for word in ("journal.revert", "plan.run", "Native Ctrl+Z"):
            assert word in op.description, (name, word)
    assert registry.get(IMPORT).category == "mediahub"  # type: ignore[union-attr]
    assert registry.get(DUP).category == registry.get(MOVE).category == "desktop"  # type: ignore[union-attr]
    assert "mediahub" in registry.list_categories()


def test_the_descriptions_carry_what_a_caller_must_know() -> None:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    text = {name: registry.get(name).description for name in ALL_OPS}  # type: ignore[union-attr]
    for word in (
        "MediaHub",
        "import settings",
        "ABSOLUTE",
        "SILENT_NOOP",
        "batch.add_clips",
        "sequence",
    ):
        assert word in text[IMPORT], word
    for word in ("uid", "wiretap", "end of", "Timeline", "new selector", "CONFLICT", "partial"):
        assert word in text[MOVE], word
    for word in ("duration", "CONFLICT", "PRECONDITION_UNMET", "diff"):
        assert word in text[DUP], word


IMPORT_PHASES: list[dict[str, Any]] = [
    {"paths": ["/x/a.mov"], "destination": REEL1},
    {"paths": ["/x/a.mov", "/x/b.mov"], "destination": REEL1, "_phase": "read"},
    {"paths": ["/x/a.mov"], "destination": REEL1, "_phase": "write", "_expected": ["u"]},
    {"paths": [], "destination": REEL1, "_phase": "revert", "_created": {"imports": []}},
    {"paths": [], "destination": REEL1, "_phase": "alive", "_created": {"items": []}},
]
DUP_PHASES: list[dict[str, Any]] = [
    {"target": C1},
    {"target": C1, "_phase": "read"},
    {"target": C1, "_phase": "write", "_expected": ["u"]},
    {"target": C1, "_phase": "revert", "_created": {"uid": "u", "parent": None}},
    {"target": C1, "_phase": "alive", "_created": {"items": [{"uid": "u", "parent": None}]}},
]
MOVE_PHASES: list[dict[str, Any]] = [
    {"sources": [C1], "destination": REEL1},
    {"sources": [C1, CLIP1], "destination": REEL1, "_phase": "read"},
    {"sources": [C1], "destination": REEL1, "_phase": "write", "_expected": ["u"]},
    {"sources": [], "destination": REEL1, "_phase": "probe"},
    {"sources": [], "destination": REEL1, "_phase": "revert", "_created": {"moves": []}},
    {"sources": [], "destination": REEL1, "_phase": "alive", "_created": {"items": []}},
]


@pytest.mark.parametrize(
    ("op_name", "args"),
    [(IMPORT, a) for a in IMPORT_PHASES]
    + [(DUP, a) for a in DUP_PHASES]
    + [(MOVE, a) for a in MOVE_PHASES],
)
def test_every_phase_generates_valid_loop_free_code(op_name: str, args: dict[str, Any]) -> None:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    op = registry.get(op_name)
    assert op is not None and op.to_python is not None
    own = op.to_python(args)
    tree = ast.parse(own)
    loops = (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    assert not any(isinstance(node, loops) for node in ast.walk(tree))
    ast.parse(execution.build_exec_code(op, args))


def test_the_catalog_listing_has_the_new_category_and_ops(rig: Rig) -> None:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    assert {op.name for op in registry.list_ops(category="mediahub")} == {IMPORT}
    assert {DUP, MOVE} <= {op.name for op in registry.list_ops(category="desktop")}
    assert fake_media_tree is not None
