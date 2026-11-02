"""Tests for `desktop.delete` of an empty reel.

Only a reel of a Desktop reel group with no clip and no sequence is deleted; there is nothing to
stash, so journal.revert makes a new empty reel of the same name in the same reel group. Run
through the real Runtime with a scripted `transport.exec` over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from journal_test_helpers import Rig
from media_p31b_helpers import add_clip
from p32b_helpers import (
    CLIPX,
    DELETE,
    LIBREEL,
    REEL1,
    delete,
    make_p32b_rig,
    reel2,
    uid,
    wt,
)
from timeline_p32a_helpers import reel_names, stash_library


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def group_reels(rig: Rig) -> list[str]:
    return [str(r.name.get_value()) for r in rig.handles["reel_group"].reels]


def revert(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    return rig.do("journal.revert", {"group": env["journal"]["group"]})


def untouched_world(rig: Rig) -> bool:
    return (
        stash_library(rig) is None
        and rig.records() == []
        and rig.flame.media_panel.copy_calls == []
    )


def test_an_empty_reel_is_deleted_without_a_stash(rig: Rig) -> None:
    old = uid(rig.handles["reel2"])
    env = delete(rig, [reel2(rig)])
    assert env["ok"] is True, env
    assert group_reels(rig) == ["Reel 1"]
    row = env["result"]["deleted"][0]
    assert row["from"]["type"] == "PyReel" and row["from"]["name"] == "Reel 2"
    assert row["from"]["uid"] == old and row["from"]["parent"]["name"] == "RG"
    assert row["stash"] is None and row["checkpoint"] is None
    assert env["result"]["partial"] is False and env["affected"] == 1 and env["undoSteps"] == 0
    assert stash_library(rig) is None
    assert env["journal"]["revert"]["op"] == "journal.revert"
    kinds = [r["type"] for r in rig.records()]
    assert kinds == ["intent", "outcome"]


def test_revert_makes_a_new_empty_reel_of_the_same_name_in_the_same_group(rig: Rig) -> None:
    old = uid(rig.handles["reel2"])
    env = delete(rig, [reel2(rig)])
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert group_reels(rig) == ["Reel 1", "Reel 2"]
    back = out["result"]["reverted"][0]
    made = rig.handles["reel_group"].reels[-1]
    assert back["to"]["uid"] == uid(made) != old and back["from"]["name"] == "Reel 2"
    assert list(made.children) == []
    again = revert(rig, env)
    assert again["ok"] is False and group_reels(rig) == ["Reel 1", "Reel 2"]


def test_revert_is_a_conflict_when_the_name_is_taken_and_makes_nothing(rig: Rig) -> None:
    env = delete(rig, [reel2(rig)])
    rig.handles["reel_group"].create_reel("Reel 2")
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "already" in out["error"]
    assert group_reels(rig) == ["Reel 1", "Reel 2"]


def test_a_reel_with_a_clip_is_refused_and_nothing_is_written(rig: Rig) -> None:
    add_clip(rig.handles["reel2"], "inside")
    env = delete(rig, [reel2(rig)])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "only an empty reel" in env["error"]
    assert group_reels(rig) == ["Reel 1", "Reel 2"]
    assert untouched_world(rig)


def test_a_reel_with_a_sequence_is_refused(rig: Rig) -> None:
    rig.flame.media_panel.copy(rig.handles["s2"], rig.handles["reel2"])
    assert len(rig.handles["reel2"].sequences) == 1
    env = delete(rig, [reel2(rig)])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "only an empty reel" in env["error"]
    assert stash_library(rig) is None and rig.records() == []
    assert group_reels(rig) == ["Reel 1", "Reel 2"]


def test_a_reel_of_the_library_is_refused_even_when_empty(rig: Rig) -> None:
    lib_reel = rig.handles["lib"].create_reel("Empty")
    assert lib_reel is not None
    env = delete(rig, [wt(lib_reel.get_wiretap_node_id())])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert untouched_world(rig)
    assert LIBREEL["kind"] == "wiretap"


def test_the_only_schematic_reel_of_a_batch_group_is_refused(rig: Rig) -> None:
    batch_reel = rig.handles["bg1"].create_reel("Schematic Reel 1")
    env = delete(rig, [wt(batch_reel.get_wiretap_node_id())])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "last" in env["error"]
    assert [str(r.name.get_value()) for r in rig.handles["bg1"].reels] == ["Schematic Reel 1"]
    assert untouched_world(rig)


@pytest.mark.parametrize(
    "selector_of",
    [
        lambda rig: wt(rig.handles["reel_group"].get_wiretap_node_id()),
        lambda rig: wt(rig.handles["lib"].get_wiretap_node_id()),
        lambda rig: wt(rig.handles["lib"].create_folder("F").get_wiretap_node_id()),
        lambda rig: {"kind": "path", "path": ["BG1"], "root": "desktop"},
    ],
    ids=["reel group", "library", "folder", "batch group"],
)
def test_the_other_containers_are_still_refused(rig: Rig, selector_of: Any) -> None:
    env = delete(rig, [selector_of(rig)])
    assert env["ok"] is False and env["errorCode"] in ("PRECONDITION_UNMET", "SELECTOR_TYPE")
    assert untouched_world(rig)


def test_a_non_empty_reel_among_valid_targets_stops_the_whole_call(rig: Rig) -> None:
    env = delete(rig, [reel2(rig), REEL1])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "targets[1]" in env["error"]
    assert group_reels(rig) == ["Reel 1", "Reel 2"]
    assert untouched_world(rig)


def test_a_reel_and_a_clip_in_one_call_are_both_deleted_and_both_reverted(rig: Rig) -> None:
    env = delete(rig, [CLIPX, reel2(rig)])
    assert env["ok"] is True, env
    assert [d["from"]["type"] for d in env["result"]["deleted"]] == ["PyClip", "PyReel"]
    assert "CLIPX" not in reel_names(rig.handles["reel"]) and group_reels(rig) == ["Reel 1"]
    out = revert(rig, env)
    assert out["ok"] is True and len(out["result"]["reverted"]) == 2, out
    assert "CLIPX" in reel_names(rig.handles["reel"]) and group_reels(rig) == ["Reel 1", "Reel 2"]


def test_a_reel_that_got_a_clip_between_the_read_and_the_write_is_a_conflict(rig: Rig) -> None:
    def edit(label: str | None) -> None:
        writes = [c for c in rig.exec.calls if c.label == DELETE]
        if label == DELETE and len(writes) == 3:
            add_clip(rig.handles["reel2"], "late")

    rig.exec.before_run = edit
    env = delete(rig, [reel2(rig)])
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert group_reels(rig) == ["Reel 1", "Reel 2"]


def test_readonly_policy_denies_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from flame_mcp.policy import Policy

    rig = make_p32b_rig(monkeypatch, tmp_path)
    rig.runtime.policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    env = delete(rig, [reel2(rig)])
    assert env["errorCode"] == "POLICY_DENIED"
    assert group_reels(rig) == ["Reel 1", "Reel 2"]


def test_plan_run_refuses_it(rig: Rig) -> None:
    env = rig.do("plan.run", {"steps": [{"op": DELETE, "args": {"targets": [reel2(rig)]}}]})
    assert env["errorCode"] == "INVALID_ARGS" and "cannot be a plan step" in str(env["result"])
    assert group_reels(rig) == ["Reel 1", "Reel 2"]


def test_check_of_a_reel_delete_whose_answer_was_lost_is_landed_and_reverts(rig: Rig) -> None:
    rig.exec.fault(DELETE, "TIMEOUT", when="after", skip=2)
    env = delete(rig, [reel2(rig)])
    assert env["errorCode"] == "TIMEOUT"
    out = rig.do("journal.check", {})
    assert out["result"]["checked"][0]["verdict"] == "landed"
    back = revert(rig, env)
    assert back["ok"] is True, back
    assert group_reels(rig) == ["Reel 1", "Reel 2"]


class _Stray:
    """Something in a reel that is neither a clip nor a sequence."""

    parent: Any = None


def test_a_reel_whose_only_content_is_a_child_that_is_no_clip_or_sequence_is_refused(
    rig: Rig,
) -> None:
    reel = rig.handles["reel2"]
    reel._children.append(_Stray())
    assert reel.clips == [] and reel.sequences == [] and len(reel.children) == 1
    env = delete(rig, [reel2(rig)])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "only an empty reel" in env["error"]
    assert group_reels(rig) == ["Reel 1", "Reel 2"]
    assert untouched_world(rig)


def test_a_reel_whose_parent_is_not_a_reel_group_is_refused(rig: Rig) -> None:
    from fake_media_tree import PyFolder

    rig.handles["reel2"].parent = PyFolder("Loose", "uid-loose", "/wt/Loose")
    env = delete(rig, [reel2(rig)])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "not in a reel group" in env["error"]
    assert group_reels(rig) == ["Reel 1", "Reel 2"]
    assert untouched_world(rig)


def test_a_reel_whose_type_cannot_be_read_is_refused(rig: Rig) -> None:
    rig.handles["reel2"].type = None
    env = delete(rig, [reel2(rig)])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "type" in env["error"]
    assert group_reels(rig) == ["Reel 1", "Reel 2"]
    assert untouched_world(rig)


def test_an_empty_sequences_reel_comes_back_as_a_sequences_reel(rig: Rig) -> None:
    made = rig.handles["reel_group"].create_reel("Seqs", sequence=True)
    assert made.type == "Sequences"
    env = delete(rig, [wt(made.get_wiretap_node_id())])
    assert env["ok"] is True, env
    assert env["result"]["deleted"][0]["from"]["reelType"] == "Sequences"
    out = revert(rig, env)
    assert out["ok"] is True, out
    back = rig.handles["reel_group"].reels[-1]
    assert str(back.name.get_value()) == "Seqs" and back.type == "Sequences"


def test_a_plain_empty_reel_comes_back_as_a_plain_reel(rig: Rig) -> None:
    out = revert(rig, delete(rig, [reel2(rig)]))
    assert out["ok"] is True and rig.handles["reel_group"].reels[-1].type == "Reel"


def test_a_made_reel_with_another_name_is_deleted_again_and_the_revert_fails(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = delete(rig, [reel2(rig)])
    group = rig.handles["reel_group"]
    real = group.create_reel
    monkeypatch.setattr(
        group, "create_reel", lambda name, sequence=False: real(name + "x", sequence)
    )
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "FLAME_API_ERROR", out
    assert group_reels(rig) == ["Reel 1"]
    assert "deleted again" in str(out["result"]["notReverted"][0]["reason"])


def test_a_made_reel_with_another_name_that_cannot_be_deleted_is_reported(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = delete(rig, [reel2(rig)])
    group = rig.handles["reel_group"]
    real = group.create_reel
    monkeypatch.setattr(
        group, "create_reel", lambda name, sequence=False: real(name + "x", sequence)
    )
    monkeypatch.setattr(rig.flame, "delete", lambda obj, confirm=True: False)
    out = revert(rig, env)
    row = out["result"]["notReverted"][0]
    assert out["errorCode"] == "FLAME_API_ERROR" and row["leftover"]["name"] == "Reel 2x"
    assert group_reels(rig) == ["Reel 1", "Reel 2x"]
