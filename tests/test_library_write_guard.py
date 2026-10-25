"""What is inside a library (a library itself too) is never renamed, written to, deleted or moved.

desktop.rename and desktop.set_attributes refuse a target in a library, or a library, with
PRECONDITION_UNMET before an intent is journaled (the pre-read refuses). desktop.delete and
desktop.move follow the parents of a target as far as they go, so a folder nested 8 or 9 deep in
a library is refused too; anything that is not found to be on the Desktop is refused. Run
through the real Runtime over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyFolder, adopt
from journal_test_helpers import Rig
from media_p31b_helpers import add_clip
from p32b_helpers import LIB_CLIP1, LIBREEL, REEL1, make_p32b_rig, reel2, wt
from timeline_p32a_helpers import S1, library_copy, reel_names, seg_sel

LIB_WT = "/projects/p/ws/P1O"
LIB = wt(LIB_WT)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def deep_clip(rig: Rig, depth: int) -> Any:
    parent: Any = rig.handles["lib"]
    for i in range(depth):
        folder = PyFolder(f"F{i}", f"fold-{i}", f"{parent.get_wiretap_node_id()}/F{i}")
        adopt(parent, [folder])
        parent._children.append(folder)
        parent = folder
    return add_clip(parent, "deepclip")


def name_of(rig: Rig, wiretap_id: str) -> str:
    found = rig.flame.find_by_wiretap_node_id(wiretap_id)
    assert found is not None
    return str(found.name.get_value())


def intents(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "intent"]


def assert_refused(env: dict[str, Any], rig: Rig) -> None:
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "library" in env["error"] and "nothing was written" in env["error"], env
    assert intents(rig) == [] and env.get("journal") is None


def test_rename_of_a_clip_in_a_library_is_refused(rig: Rig) -> None:
    env = rig.do("desktop.rename", {"target": LIB_CLIP1, "name": "renamed_by_mcp"})
    assert_refused(env, rig)
    assert name_of(rig, "/projects/p/ws/P1O/R1/clip1") == "clip1"


def test_rename_of_a_library_is_refused(rig: Rig) -> None:
    before = name_of(rig, LIB_WT)
    env = rig.do("desktop.rename", {"target": LIB, "name": "renamed_lib"})
    assert_refused(env, rig)
    assert name_of(rig, LIB_WT) == before


def test_rename_of_a_reel_in_a_library_is_refused(rig: Rig) -> None:
    env = rig.do("desktop.rename", {"target": LIBREEL, "name": "renamed_reel"})
    assert_refused(env, rig)


def test_rename_of_a_deeply_nested_clip_in_a_library_is_refused(rig: Rig) -> None:
    clip = deep_clip(rig, 12)
    env = rig.do("desktop.rename", {"target": wt(clip.get_wiretap_node_id()), "name": "x"})
    assert_refused(env, rig)
    assert str(clip.name.get_value()) == "deepclip"


@pytest.mark.parametrize("target", [LIB_CLIP1, LIB, LIBREEL], ids=["clip", "library", "reel"])
def test_set_attributes_of_what_is_in_a_library_is_refused(
    rig: Rig, target: dict[str, Any]
) -> None:
    env = rig.do("desktop.set_attributes", {"target": target, "values": {"name": "changed"}})
    assert_refused(env, rig)


def test_a_plan_step_that_renames_what_is_in_a_library_is_refused(rig: Rig) -> None:
    env = rig.do(
        "plan.run",
        {
            "steps": [
                {"op": "desktop.rename", "args": {"target": LIB_CLIP1, "name": "via_plan"}},
            ]
        },
    )
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "nothing was written" in env["error"] and "library" in env["error"], env
    assert env["result"]["steps"][0]["errorCode"] == "PRECONDITION_UNMET"
    assert intents(rig) == []
    assert name_of(rig, "/projects/p/ws/P1O/R1/clip1") == "clip1"


def test_a_plan_step_that_sets_attributes_in_a_library_is_refused(rig: Rig) -> None:
    env = rig.do(
        "plan.run",
        {
            "steps": [
                {
                    "op": "desktop.set_attributes",
                    "args": {"target": LIB_CLIP1, "values": {"name": "via_plan"}},
                },
            ]
        },
    )
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "nothing was written" in env["error"] and "library" in env["error"], env
    assert env["result"]["steps"][0]["errorCode"] == "PRECONDITION_UNMET"
    assert intents(rig) == []
    assert name_of(rig, "/projects/p/ws/P1O/R1/clip1") == "clip1"


def test_rename_and_set_attributes_still_work_on_the_desktop(rig: Rig) -> None:
    env = rig.do("desktop.rename", {"target": REEL1, "name": "Reel renamed"})
    assert env["ok"] is True, env
    assert name_of(rig, "/wt/RG/Reel 1") == "Reel renamed"
    env = rig.do(
        "desktop.set_attributes", {"target": wt("/wt/RG/Reel 1"), "values": {"name": "Back"}}
    )
    assert env["ok"] is True, env
    assert name_of(rig, "/wt/RG/Reel 1") == "Back"


def test_a_segment_of_a_sequence_in_a_library_is_not_written(rig: Rig) -> None:
    copy = library_copy(rig)
    target = seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1)
    before = rig.do("segment.get_attributes", {"target": target, "keys": ["name"]})
    assert before["ok"] is True, before
    env = rig.do("segment.set_attributes", {"target": target, "values": {"name": "changed"}})
    assert env["ok"] is False, env
    assert env["errorCode"] == "PRECONDITION_UNMET" and "library" in env["error"], env
    after = rig.do("segment.get_attributes", {"target": target, "keys": ["name"]})
    assert after["result"]["attributes"] == before["result"]["attributes"]
    assert intents(rig) == []


def test_a_segment_of_a_sequence_on_the_desktop_is_still_written(rig: Rig) -> None:
    target = seg_sel(S1, 0, index=1)
    env = rig.do("segment.set_attributes", {"target": target, "values": {"name": "changed"}})
    assert env["ok"] is True, env


@pytest.mark.parametrize("depth", [7, 8, 9, 20])
def test_delete_of_a_clip_nested_in_a_library_is_refused_at_any_depth(rig: Rig, depth: int) -> None:
    clip = deep_clip(rig, depth)
    env = rig.do("desktop.delete", {"targets": [wt(clip.get_wiretap_node_id())]})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "library" in env["error"], env
    assert [str(c.name.get_value()) for c in clip.parent.children] == ["deepclip"]
    assert intents(rig) == []


@pytest.mark.parametrize("depth", [7, 8, 9, 20])
def test_move_of_a_clip_nested_in_a_library_is_refused_at_any_depth(rig: Rig, depth: int) -> None:
    clip = deep_clip(rig, depth)
    env = rig.do(
        "desktop.move", {"sources": [wt(clip.get_wiretap_node_id())], "destination": reel2(rig)}
    )
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert reel_names(rig.handles["reel2"]) == []
    assert [str(c.name.get_value()) for c in clip.parent.children] == ["deepclip"]
    assert intents(rig) == []


def test_arrange_does_not_move_a_deeply_nested_clip_out_of_a_library(rig: Rig) -> None:
    clip = deep_clip(rig, 9)
    env = rig.do(
        "desktop.arrange",
        {
            "destination": reel2(rig),
            "items": [{"action": "move", "source": wt(clip.get_wiretap_node_id())}],
        },
    )
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert reel_names(rig.handles["reel2"]) == []


def test_a_clip_whose_parents_end_in_nothing_known_is_not_deleted_or_moved(rig: Rig) -> None:
    clip = add_clip(rig.handles["reel2"], "orphan")
    rig.handles["reel2"].parent = None
    detached = wt(clip.get_wiretap_node_id())
    env = rig.do("desktop.delete", {"targets": [detached]})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    env = rig.do("desktop.move", {"sources": [detached], "destination": wt("/wt/RG/Reel 1")})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert reel_names(rig.handles["reel2"]) == ["orphan"]


def test_reading_the_attributes_of_what_is_in_a_library_still_works(rig: Rig) -> None:
    env = rig.do("desktop.get_attributes", {"target": LIB_CLIP1, "keys": ["name"]})
    assert env["ok"] is True, env
    assert env["result"]["attributes"]["name"]["value"] == "clip1"


def test_the_revert_of_an_earlier_rename_is_not_refused_by_the_library_guard(rig: Rig) -> None:
    env = rig.do("desktop.rename", {"target": REEL1, "name": "Reel renamed"})
    assert env["ok"] is True, env
    rig.handles["reel_group"].parent = rig.handles["lib"]  # the reel now counts as in a library
    refused = rig.do("desktop.rename", {"target": wt("/wt/RG/Reel 1"), "name": "again"})
    assert refused["errorCode"] == "PRECONDITION_UNMET", refused
    reverted = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert reverted["ok"] is True, reverted
    assert name_of(rig, "/wt/RG/Reel 1") == "Reel 1"
