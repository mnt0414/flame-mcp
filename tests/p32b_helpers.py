"""Shared rig and helpers for the desktop delete / move and plan tests.

Used by test_desktop_delete_p32b.py, test_plan_timeline_p32b.py, test_purge_p32b.py and
test_move_display_p32b.py: the timeline rig (the fake Flame tree with a few sequences on the
Desktop and a library copy of one) plus a second Desktop reel and the helpers that read the fake
directly -- the way the real-Flame e2e reads state back by transport, never through the ops under
test. The fake follows what was measured on Flame 2025.2.4 (fake_media_tree.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import ACTIVE_TIMELINE
from journal_test_helpers import Rig
from timeline_p32a_helpers import (
    CLIPX,
    S1,
    S2,
    S3,
    make_timeline_rig,
    uid,
    wt,
)

DELETE = "desktop.delete"
PURGE = "journal.purge_checkpoints"
MOVE = "desktop.move"
PLAN = "plan.run"
LIB_CLIP1_WT = "/projects/p/ws/P1O/R1/clip1"
LIB_CLIP1 = wt(LIB_CLIP1_WT)
LIBREEL = wt("/projects/p/ws/P1O/R1")
REEL1 = wt("/wt/RG/Reel 1")

__all__ = [
    "CLIPX",
    "DELETE",
    "LIBREEL",
    "LIB_CLIP1",
    "MOVE",
    "PLAN",
    "PURGE",
    "REEL1",
    "S1",
    "S2",
    "S3",
    "delete",
    "make_p32b_rig",
    "reel2",
    "show",
    "shown",
    "uid",
    "wt",
]


def make_p32b_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    rig = make_timeline_rig(monkeypatch, tmp_path)
    rig.handles["reel2"] = rig.handles["reel_group"].create_reel("Reel 2")
    return rig


def reel2(rig: Rig) -> dict[str, Any]:
    return wt(rig.handles["reel2"].get_wiretap_node_id())


def delete(rig: Rig, targets: list[dict[str, Any]]) -> dict[str, Any]:
    return rig.do(DELETE, {"targets": targets})


def show(rig: Rig, name: str) -> None:
    """Open a sequence of the Desktop reel in the fake Timeline."""
    assert rig.handles[name].open() is True


def shown(rig: Rig) -> Any:
    return ACTIVE_TIMELINE[0].clip
