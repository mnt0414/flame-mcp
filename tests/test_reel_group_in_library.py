"""A reel group inside a library is a library: nothing in it is deleted, moved or arranged.

`_tl_location` answers 'library' when a PyLibrary is among the parents, also for a reel group
created in a library (`PyLibrary.create_reel_group`): its empty reels are not deleted and its clips
are not moved. The fake re-parents the rig's reel group to a library."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from journal_test_helpers import Rig
from media_p31b_helpers import add_clip, make_media_rig, names, wt


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_media_rig(monkeypatch, tmp_path)
    desktop = made.flame.projects.current_project.current_workspace.desktop
    made.handles["desktop_reel"] = desktop.create_reel_group("RG2").reels[0]
    made.handles["reel_group"].parent = made.handles["lib"]
    return made


def desktop_reel(rig: Rig) -> Any:
    return rig.handles["desktop_reel"]


def untouched(rig: Rig) -> bool:
    return rig.records() == []


def test_an_empty_reel_of_a_reel_group_in_a_library_is_not_deleted(rig: Rig) -> None:
    reel2 = rig.handles["reel2"]
    env = rig.do("desktop.delete", {"targets": [wt(reel2)]})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "inside a library" in env["error"]
    assert reel2 in rig.handles["reel_group"].reels
    assert untouched(rig)


def test_a_clip_of_a_reel_group_in_a_library_is_not_deleted(rig: Rig) -> None:
    clip = add_clip(rig.handles["reel_group"].reels[0], "inlib")
    env = rig.do("desktop.delete", {"targets": [wt(clip)]})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "inside a library" in env["error"]
    assert "inlib" in names(rig.handles["reel_group"].reels[0])
    assert untouched(rig)


def test_a_clip_of_a_reel_group_in_a_library_is_not_moved(rig: Rig) -> None:
    clip = add_clip(rig.handles["reel_group"].reels[0], "inlib")
    dest = desktop_reel(rig)
    env = rig.do("desktop.move", {"sources": [wt(clip)], "destination": wt(dest)})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "inside a library" in env["error"]
    assert "inlib" in names(rig.handles["reel_group"].reels[0])
    assert untouched(rig)


def test_arrange_does_not_move_from_a_reel_group_in_a_library(rig: Rig) -> None:
    clip = add_clip(rig.handles["reel_group"].reels[0], "inlib")
    dest = desktop_reel(rig)
    env = rig.do(
        "desktop.arrange",
        {"destination": wt(dest), "items": [{"action": "move", "source": wt(clip)}]},
    )
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "inside a library" in env["error"]
    assert "inlib" in names(rig.handles["reel_group"].reels[0])
    assert untouched(rig)


def test_arrange_does_not_use_a_reel_of_a_reel_group_in_a_library_as_destination(
    rig: Rig,
) -> None:
    env = rig.do(
        "desktop.arrange",
        {
            "destination": wt(rig.handles["reel2"]),
            "items": [{"action": "copy", "source": wt(rig.handles["clip1"])}],
        },
    )
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "inside a library" in env["error"]
    assert names(rig.handles["reel2"]) == []
    assert untouched(rig)
