"""The Timeline restore a revert runs (the internal destructive `timeline.restore`) goes through
the same policy gate as any op: with `destructive` out of FLAME_MCP_ALLOW_EFFECTS it is refused
with POLICY_DENIED and nothing in Flame moves."""

from __future__ import annotations

from pathlib import Path

import pytest

from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from reformat_helpers import make_reformat_rig, reformat
from reformat_helpers import revert as reformat_revert
from timeline_p32a_helpers import a_seg, make_timeline_rig, revert, trim

NO_DESTRUCTIVE = {"FLAME_MCP_ALLOW_EFFECTS": "read,reversible,create"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def test_timeline_revert_is_denied_without_destructive(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is True, env
    moves = len(rig.flame.media_panel.move_calls)
    rig.runtime.policy = Policy.from_env(NO_DESTRUCTIVE)

    back = revert(rig, env)

    assert back["ok"] is False and back["errorCode"] == "POLICY_DENIED", back
    assert "timeline.restore" in back["error"]
    assert len(rig.flame.media_panel.move_calls) == moves


def test_forced_timeline_revert_is_denied_without_destructive(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", 2)
    assert trim(rig, a_seg(), "head", 1)["ok"] is True
    moves = len(rig.flame.media_panel.move_calls)
    rig.runtime.policy = Policy.from_env(NO_DESTRUCTIVE)

    back = revert(rig, env, force=True)

    assert back["ok"] is False and back["errorCode"] == "POLICY_DENIED", back
    assert len(rig.flame.media_panel.move_calls) == moves


def test_timeline_revert_still_works_when_destructive_is_allowed(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", 2)
    rig.runtime.policy = Policy.from_env(
        {"FLAME_MCP_ALLOW_EFFECTS": "read,reversible,create,destructive"}
    )

    back = revert(rig, env)

    assert back["ok"] is True, back


def test_in_place_reformat_revert_is_denied_without_destructive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_reformat_rig(monkeypatch, tmp_path)
    env = reformat(rig)
    assert env["ok"] is True, env
    moves = len(rig.flame.media_panel.move_calls)
    rig.runtime.policy = Policy.from_env(NO_DESTRUCTIVE)

    back = reformat_revert(rig, env)

    assert back["ok"] is False, back
    assert back["errorCode"] == "POLICY_DENIED", back
    assert len(rig.flame.media_panel.move_calls) == moves
