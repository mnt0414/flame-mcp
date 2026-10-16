"""A call that overwrites or replaces something counts as `destructive` for the policy, whatever
effect the op declares: batch.save_setup / node.save_setup / segment.effect_save_setup with
overwrite=true, batch.iterate with overwrite=true, render.batch with allow_overwrite=true. The
gate is the op's precheck, so Runtime.call and plan.run's up-front check both apply it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp.effective_effect import deny_destructive_call
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from journal_test_helpers import BG1, Rig, make_rig
from timeline_p32a_helpers import S1, seg_sel

NODE = {"kind": "node", "batch": BG1, "name": "exposure1"}
SEGMENT = seg_sel(S1, 0, index=0)
CASES: list[tuple[str, dict[str, Any], str]] = [
    ("batch.save_setup", {"target": BG1, "path": "/x/y"}, "overwrite"),
    ("node.save_setup", {"target": NODE, "path": "/x/y"}, "overwrite"),
    (
        "segment.effect_save_setup",
        {"target": SEGMENT, "type": "Blur", "path": "/x/y"},
        "overwrite",
    ),
    ("batch.iterate", {"target": BG1, "index": 1}, "overwrite"),
    ("render.batch", {"target": BG1}, "allow_overwrite"),
]
NO_DESTRUCTIVE = Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,reversible,create"})
WITH_DESTRUCTIVE = Policy.from_env(
    {"FLAME_MCP_ALLOW_EFFECTS": "read,reversible,create,destructive"}
)
UNRESTRICTED = Policy.from_env({})


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry()
    register_all(reg)
    return reg


def denied(registry: Registry, op: str, args: dict[str, Any], policy: Policy) -> bool:
    spec = registry.get(op)
    assert spec is not None and spec.precheck is not None
    refusal = spec.precheck(args, policy)
    return refusal is not None and refusal.code == "POLICY_DENIED"


@pytest.mark.parametrize(("op", "base", "flag"), CASES)
def test_the_overwriting_call_is_denied_when_destructive_is_not_allowed(
    registry: Registry, op: str, base: dict[str, Any], flag: str
) -> None:
    assert denied(registry, op, {**base, flag: True}, NO_DESTRUCTIVE) is True


@pytest.mark.parametrize(("op", "base", "flag"), CASES)
def test_the_same_op_without_the_overwrite_flag_is_not_a_destructive_call(
    registry: Registry, op: str, base: dict[str, Any], flag: str
) -> None:
    assert denied(registry, op, base, NO_DESTRUCTIVE) is False
    assert denied(registry, op, {**base, flag: False}, NO_DESTRUCTIVE) is False


@pytest.mark.parametrize(("op", "base", "flag"), CASES)
def test_the_overwriting_call_passes_when_destructive_is_allowed_or_nothing_is_restricted(
    registry: Registry, op: str, base: dict[str, Any], flag: str
) -> None:
    assert denied(registry, op, {**base, flag: True}, WITH_DESTRUCTIVE) is False
    assert denied(registry, op, {**base, flag: True}, UNRESTRICTED) is False


@pytest.mark.parametrize(("op", "base", "flag"), CASES)
def test_the_declared_effect_stays_and_the_description_says_the_call_is_destructive(
    registry: Registry, op: str, base: dict[str, Any], flag: str
) -> None:
    spec = registry.get(op)
    assert spec is not None and spec.effect == "create"
    assert "destructive" in spec.description and flag in spec.description


def test_the_denial_says_which_call_counts_as_destructive_and_what_to_change() -> None:
    refusal = deny_destructive_call("batch.save_setup", "overwrite=true", NO_DESTRUCTIVE)
    assert refusal is not None and refusal.code == "POLICY_DENIED"
    assert "batch.save_setup" in refusal.message and "overwrite=true" in refusal.message
    assert "FLAME_MCP_ALLOW_EFFECTS" in refusal.message
    assert refusal.hint is not None and "destructive" in refusal.hint
    assert deny_destructive_call("batch.save_setup", "overwrite=true", UNRESTRICTED) is None


@pytest.mark.parametrize(("op", "base", "flag"), CASES)
def test_runtime_denies_every_overwriting_call_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, op: str, base: dict[str, Any], flag: str
) -> None:
    rig: Rig = make_rig(monkeypatch, tmp_path, policy=NO_DESTRUCTIVE)
    env = rig.do(op, {**base, flag: True})
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert f"{flag}=true" in env["error"]
    assert rig.exec.calls == [] and rig.records() == []


def test_runtime_denies_an_overwriting_save_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig: Rig = make_rig(monkeypatch, tmp_path, policy=NO_DESTRUCTIVE)
    path = str(tmp_path / "setup")
    ok = rig.do("batch.save_setup", {"target": BG1, "path": path})
    assert ok["ok"] is True, ok
    calls = len(rig.exec.calls)
    env = rig.do("batch.save_setup", {"target": BG1, "path": path, "overwrite": True})
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert "overwrite=true" in env["error"]
    assert len(rig.exec.calls) == calls


def test_a_plan_step_that_overwrites_is_denied_the_same_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig: Rig = make_rig(monkeypatch, tmp_path, policy=NO_DESTRUCTIVE)
    step = {
        "op": "batch.save_setup",
        "args": {"target": BG1, "path": str(tmp_path / "setup"), "overwrite": True},
    }
    env = rig.do("plan.run", {"steps": [step]})
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert env["result"]["ran"] is False
    assert not (tmp_path / "setup.batch").exists()
    assert rig.exec.calls == []


def test_a_plan_denied_by_a_step_precheck_carries_the_precheck_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig: Rig = make_rig(monkeypatch, tmp_path, policy=NO_DESTRUCTIVE)
    step = {
        "op": "batch.save_setup",
        "args": {"target": BG1, "path": str(tmp_path / "setup"), "overwrite": True},
    }
    env = rig.do("plan.run", {"steps": [step]})
    assert env["errorCode"] == "POLICY_DENIED", env
    assert env["hint"] is not None, env
    assert "FLAME_MCP_ALLOW_EFFECTS" in env["hint"] and "destructive" in env["hint"]
