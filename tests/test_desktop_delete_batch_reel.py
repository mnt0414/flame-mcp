"""Tests for `desktop.delete` of an empty schematic reel of a Batch group.

Measured on Flame 2025.2.4 (P-D1): `flame.delete(reel, confirm=False)` of an empty schematic reel
removes it, the node count and the other reels stay, current or not. Refused: a shelf reel, a
reel that holds anything, a reel whose type cannot be read, and the last schematic reel of a group
(not tried). A revert makes a new empty schematic reel of the same name with
`PyBatch.create_reel`, at the end. Run through the real Runtime over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from journal_test_helpers import Rig
from media_p31b_helpers import add_clip
from p32b_helpers import DELETE, delete, make_p32b_rig, reel2, uid, wt
from timeline_p32a_helpers import stash_library


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    rig = make_p32b_rig(monkeypatch, tmp_path)
    for name in ("Sch A", "Sch B", "Sch C"):
        rig.handles["bg1"].create_reel(name)
        rig.handles["bg2"].create_reel(name)
    rig.handles["bg1"].create_shelf_reel("Shelf A")
    return rig


def reels(batch: Any) -> list[str]:
    return [str(r.name.get_value()) for r in batch.reels]


def reel_of(batch: Any, name: str) -> Any:
    return next(r for r in batch.reels if str(r.name.get_value()) == name)


def sel(reel: Any) -> dict[str, Any]:
    return wt(reel.get_wiretap_node_id())


def revert(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    return rig.do("journal.revert", {"group": env["journal"]["group"]})


def untouched(rig: Rig) -> bool:
    return (
        stash_library(rig) is None
        and rig.records() == []
        and rig.flame.media_panel.copy_calls == []
        and rig.handles["bg1"].delete_reel_calls == []
        and rig.handles["bg2"].delete_reel_calls == []
    )


@pytest.mark.parametrize("group", ["bg1", "bg2"], ids=["not current", "current"])
def test_an_empty_schematic_reel_is_deleted_without_a_stash(rig: Rig, group: str) -> None:
    batch = rig.handles[group]
    assert (
        rig.flame.projects.current_project.current_workspace.desktop.current_batch_group.get_value()
        is batch
    ) == (group == "bg2")
    old = uid(reel_of(batch, "Sch B"))
    others = {n: uid(reel_of(batch, n)) for n in ("Sch A", "Sch C")}
    nodes = len(batch.nodes)
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is True, env
    assert reels(batch) == ["Sch A", "Sch C"]
    assert {n: uid(reel_of(batch, n)) for n in others} == others
    assert len(batch.nodes) == nodes
    row = env["result"]["deleted"][0]
    assert row["from"]["type"] == "PyReel" and row["from"]["name"] == "Sch B"
    assert row["from"]["uid"] == old and row["from"]["reelType"] == "Schematic"
    assert row["from"]["parent"]["type"] == "PyBatch" and row["from"]["parent"]["uid"] == uid(batch)
    assert row["stash"] is None and row["checkpoint"] is None
    assert env["affected"] == 1 and env["undoSteps"] == 0 and env["result"]["partial"] is False
    assert stash_library(rig) is None
    assert [r["type"] for r in rig.records()] == ["intent", "outcome"]
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_the_target_can_be_given_by_path(rig: Rig) -> None:
    target = {"kind": "path", "root": "desktop", "path": ["BG1", "Sch B"]}
    env = delete(rig, [target])
    assert env["ok"] is True, env
    assert reels(rig.handles["bg1"]) == ["Sch A", "Sch C"]


def test_a_shelf_reel_is_refused(rig: Rig) -> None:
    shelf = rig.handles["bg1"].shelf_reels[-1]
    env = delete(rig, [sel(shelf)])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "shelf" in env["error"]
    assert str(shelf.name.get_value()) in [
        str(r.name.get_value()) for r in rig.handles["bg1"].shelf_reels
    ]
    assert untouched(rig)


def test_a_schematic_reel_with_a_clip_is_refused(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    add_clip(reel_of(batch, "Sch B"), "inside")
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "only an empty reel" in env["error"] and "Clip nodes" in env["error"]
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]
    assert untouched(rig)


def test_a_schematic_reel_with_a_sequence_is_refused(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    target = reel_of(batch, "Sch B")
    rig.flame.media_panel.copy(rig.handles["s2"], target)
    rig.flame.media_panel.copy_calls.clear()
    assert len(target.sequences) == 1
    env = delete(rig, [sel(target)])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "only an empty reel" in env["error"]
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]
    assert stash_library(rig) is None and rig.records() == []


def test_a_schematic_reel_whose_type_cannot_be_read_is_refused(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    reel_of(batch, "Sch B").type = None
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "type" in env["error"]
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]
    assert untouched(rig)


def test_the_last_schematic_reel_of_a_group_is_refused(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    assert delete(rig, [sel(reel_of(batch, "Sch A"))])["ok"] is True
    assert delete(rig, [sel(reel_of(batch, "Sch B"))])["ok"] is True
    before = len(rig.records())
    env = delete(rig, [sel(reel_of(batch, "Sch C"))])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "last" in env["error"] and "Sch C" in env["error"]
    assert reels(batch) == ["Sch C"] and len(rig.records()) == before


def test_listing_every_reel_of_a_group_in_one_call_is_refused_and_nothing_is_deleted(
    rig: Rig,
) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, n)) for n in ("Sch A", "Sch B", "Sch C")])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "last" in env["error"]
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]
    assert untouched(rig)


def test_listing_all_but_one_reel_of_a_group_is_allowed(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, n)) for n in ("Sch A", "Sch C")])
    assert env["ok"] is True, env
    assert reels(batch) == ["Sch B"]
    assert len(env["result"]["deleted"]) == 2


def test_the_count_is_per_group(rig: Rig) -> None:
    one, two = rig.handles["bg1"], rig.handles["bg2"]
    env = delete(rig, [sel(reel_of(one, "Sch A")), sel(reel_of(two, "Sch A"))])
    assert env["ok"] is True, env
    assert reels(one) == ["Sch B", "Sch C"] and reels(two) == ["Sch B", "Sch C"]


def test_a_batch_reel_and_a_refused_one_in_one_call_stop_the_whole_call(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, "Sch A")), sel(rig.handles["bg1"].shelf_reels[-1])])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "targets[1]" in env["error"]
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]
    assert untouched(rig)


def test_a_desktop_reel_and_a_batch_reel_in_one_call_are_both_deleted_and_reverted(
    rig: Rig,
) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [reel2(rig), sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is True, env
    out = revert(rig, env)
    assert out["ok"] is True and len(out["result"]["reverted"]) == 2, out
    assert reels(batch) == ["Sch A", "Sch C", "Sch B"]
    assert [str(r.name.get_value()) for r in rig.handles["reel_group"].reels] == [
        "Reel 1",
        "Reel 2",
    ]


def test_revert_makes_an_empty_schematic_reel_of_the_same_name_at_the_end(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    old = uid(reel_of(batch, "Sch B"))
    nodes = len(batch.nodes)
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert reels(batch) == ["Sch A", "Sch C", "Sch B"]
    made = batch.reels[-1]
    assert made.type == "Schematic" and list(made.children) == []
    back = out["result"]["reverted"][0]
    assert back["to"]["uid"] == uid(made) != old and back["from"]["name"] == "Sch B"
    assert len(batch.nodes) == nodes
    again = revert(rig, env)
    assert again["ok"] is False and reels(batch) == ["Sch A", "Sch C", "Sch B"]


def test_revert_is_a_conflict_when_a_schematic_reel_of_that_name_exists(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    batch.create_reel("Sch B")
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "already" in out["error"]
    assert reels(batch) == ["Sch A", "Sch C", "Sch B"]


def test_revert_is_a_conflict_when_the_batch_group_is_gone(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    rig.flame.delete(batch, confirm=False)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "gone" in out["error"]


def test_a_made_reel_with_another_name_is_deleted_again_and_the_revert_fails(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    batch.create_reel_suffix = "x"
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "FLAME_API_ERROR", out
    assert reels(batch) == ["Sch A", "Sch C"]
    assert "deleted again" in str(out["result"]["notReverted"][0]["reason"])


def test_create_reel_that_makes_nothing_fails_the_revert(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    batch.create_reel_makes_nothing = True
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "FLAME_API_ERROR", out
    assert reels(batch) == ["Sch A", "Sch C"]


def test_a_delete_that_leaves_the_reel_there_is_silent_noop(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    batch.delete_reel_raises = "refused"
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]


def test_a_delete_that_also_removes_a_node_is_reported_and_stays_revertible(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    real = batch.delete_reel

    def delete_and_a_node(reel: Any) -> bool:
        answer = bool(real(reel))
        batch.remove_node(batch.nodes[0])
        return answer

    batch.delete_reel = delete_and_a_node
    before = len(batch.nodes)
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "node" in env["error"] and str(before) in env["error"]
    assert len(env["result"]["deleted"]) == 1
    assert env["result"]["deleted"][0]["nodeCountBefore"] == before
    assert env["result"]["deleted"][0]["nodeCountAfter"] == before - 1
    assert env["journal"]["revert"]["op"] == "journal.revert"
    assert revert(rig, env)["ok"] is True
    assert reels(batch) == ["Sch A", "Sch C", "Sch B"]


def test_the_node_count_is_recorded_when_it_does_not_change(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    row = env["result"]["deleted"][0]
    assert row["nodeCountBefore"] == row["nodeCountAfter"] == len(batch.nodes)


def test_a_batch_group_itself_is_still_refused(rig: Rig) -> None:
    env = delete(rig, [{"kind": "path", "path": ["BG1"], "root": "desktop"}])
    assert env["ok"] is False and env["errorCode"] in ("PRECONDITION_UNMET", "SELECTOR_TYPE")
    assert untouched(rig)


def test_a_clip_inside_a_batch_reel_is_still_refused(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    clip = add_clip(reel_of(batch, "Sch B"), "inside")
    env = delete(rig, [wt(clip.get_wiretap_node_id())])
    assert env["errorCode"] == "INVALID_ARGS" and "Batch group" in env["error"]
    assert untouched(rig)


def test_plan_run_refuses_it(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    env = rig.do(
        "plan.run", {"steps": [{"op": DELETE, "args": {"targets": [sel(reel_of(batch, "Sch B"))]}}]}
    )
    assert env["errorCode"] == "INVALID_ARGS" and "cannot be a plan step" in str(env["result"])
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]
    assert untouched(rig)


def test_readonly_policy_denies_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from flame_mcp.policy import Policy

    rig = make_p32b_rig(monkeypatch, tmp_path)
    batch = rig.handles["bg1"]
    batch.create_reel("Sch A")
    batch.create_reel("Sch B")
    rig.runtime.policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["errorCode"] == "POLICY_DENIED"
    assert reels(batch) == ["Sch A", "Sch B"]


def test_check_of_a_batch_reel_delete_whose_answer_was_lost_is_landed_and_reverts(
    rig: Rig,
) -> None:
    batch = rig.handles["bg1"]
    rig.exec.fault(DELETE, "TIMEOUT", when="after", skip=2)
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["errorCode"] == "TIMEOUT"
    out = rig.do("journal.check", {})
    assert out["result"]["checked"][0]["verdict"] == "landed"
    back = revert(rig, env)
    assert back["ok"] is True, back
    assert reels(batch) == ["Sch A", "Sch C", "Sch B"]


def test_check_of_a_batch_reel_delete_that_did_not_happen_is_not_landed(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    rig.exec.fault(DELETE, "TIMEOUT", when="before", skip=2)
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["errorCode"] == "TIMEOUT"
    out = rig.do("journal.check", {})
    assert out["result"]["checked"][0]["verdict"] == "not_landed", out
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]


def _on_write(rig: Rig, action: Any) -> None:
    def edit(label: str | None) -> None:
        writes = [c for c in rig.exec.calls if c.label == DELETE]
        if label == DELETE and len(writes) == 3:
            action()

    rig.exec.before_run = edit


def test_a_batch_reel_that_got_a_clip_between_the_read_and_the_write_is_a_conflict(
    rig: Rig,
) -> None:
    batch = rig.handles["bg1"]
    _on_write(rig, lambda: add_clip(reel_of(batch, "Sch B"), "late"))
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert reels(batch) == ["Sch A", "Sch B", "Sch C"]
    assert batch.delete_reel_calls == []


def test_a_batch_reel_that_became_the_last_one_between_the_read_and_the_write_is_a_conflict(
    rig: Rig,
) -> None:
    batch = rig.handles["bg1"]

    def others_go() -> None:
        batch.reels = [r for r in batch.reels if str(r.name.get_value()) == "Sch B"]

    _on_write(rig, others_go)
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert "no schematic reel" in env["error"]
    assert reels(batch) == ["Sch B"] and batch.delete_reel_calls == []


def test_a_batch_group_that_is_gone_before_the_write_deletes_nothing(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    target = sel(reel_of(batch, "Sch B"))
    _on_write(rig, lambda: rig.flame.delete(batch, confirm=False))
    env = delete(rig, [target])
    # the same as a Desktop reel that is gone: the selector finds nothing before Flame is asked
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert batch.delete_reel_calls == []
    assert [r["type"] for r in rig.records()] == ["intent", "outcome"]


def test_a_batch_reel_whose_group_no_longer_lists_it_is_a_conflict_at_the_write(
    rig: Rig,
) -> None:
    batch = rig.handles["bg1"]
    other = rig.handles["bg2"]
    moved = reel_of(batch, "Sch B")

    def detach() -> None:
        # still found by its wiretap id, but no Batch group of the Desktop lists it any more
        batch.reels = [r for r in batch.reels if r is not moved]
        rig.flame._wiretap_index[moved.get_wiretap_node_id()] = moved

    _on_write(rig, detach)
    env = delete(rig, [sel(moved)])
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert "Batch group" in env["error"]
    assert other.delete_reel_calls == [] and batch.delete_reel_calls == []


def test_a_batch_reel_that_turned_into_a_shelf_reel_is_a_conflict_at_the_write(
    rig: Rig,
) -> None:
    batch = rig.handles["bg1"]
    moved = reel_of(batch, "Sch B")

    def to_shelf() -> None:
        batch.reels = [r for r in batch.reels if r is not moved]
        batch.shelf_reels.append(moved)
        moved.type = "Shelf"

    _on_write(rig, to_shelf)
    env = delete(rig, [sel(moved)])
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert "not a schematic reel" in env["error"]
    assert batch.delete_reel_calls == []


def test_a_reel_of_a_batch_group_outside_the_desktop_is_refused(rig: Rig) -> None:
    from fake_media_tree import PyBatch

    stray = PyBatch("InLibrary", "uid-in-lib")
    reel = stray.create_reel("Only")
    assert reel is not None
    stray.create_reel("Two")
    rig.flame._wiretap_index[reel.get_wiretap_node_id()] = reel
    env = delete(rig, [sel(reel)])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "not found among the Batch groups" in env["error"]
    assert untouched(rig) and stray.delete_reel_calls == []


class _Unreadable:
    def __iter__(self) -> Any:
        raise RuntimeError("cannot list the nodes")


def test_a_reel_of_a_batch_group_whose_nodes_cannot_be_counted_is_refused(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    batch.nodes = _Unreadable()
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "node" in env["error"]
    assert batch.delete_reel_calls == [] and rig.records() == []


def test_check_reads_a_renamed_batch_group_by_its_uid(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    rig.exec.fault(DELETE, "TIMEOUT", when="after", skip=2)
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["errorCode"] == "TIMEOUT"
    batch.name.set_value("Renamed")
    out = rig.do("journal.check", {})
    assert out["result"]["checked"][0]["verdict"] == "landed", out


def test_check_does_not_read_a_gone_batch_group_as_landed(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    rig.exec.fault(DELETE, "TIMEOUT", when="after", skip=2)
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    assert env["errorCode"] == "TIMEOUT"
    rig.flame.delete(batch, confirm=False)
    out = rig.do("journal.check", {})
    row = out["result"]["checked"][0]
    assert row["verdict"] == "unknown", out
    assert "gone" in str(row)


def test_a_made_reel_that_cannot_be_found_by_uid_is_still_reported_as_a_leftover(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = rig.handles["bg1"]
    env = delete(rig, [sel(reel_of(batch, "Sch B"))])
    batch.create_reel_suffix = "x"
    monkeypatch.setattr(rig.flame, "find_by_uid", lambda u: None)
    monkeypatch.setattr(rig.flame, "delete", lambda obj, confirm=True: False)
    out = revert(rig, env)
    row = out["result"]["notReverted"][0]
    assert out["errorCode"] == "FLAME_API_ERROR" and "leftover" in row, out
    assert row["leftover"]["name"] == "Sch Bx"
    assert "deleted again" not in row["reason"]


def test_only_a_clip_inside_a_batch_reel_gets_the_batch_hint(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    clip = add_clip(reel_of(batch, "Sch B"), "inside")
    inside = delete(rig, [wt(clip.get_wiretap_node_id())])
    assert inside["errorCode"] == "INVALID_ARGS" and "Batch group" in str(inside.get("hint"))
    twice = sel(reel_of(batch, "Sch A"))
    env = delete(rig, [twice, twice])
    assert env["errorCode"] == "INVALID_ARGS" and "twice" in env["error"]
    assert "Batch" not in str(env.get("hint"))
