"""Tests that a restore puts the schematic reels back.

A checkpoint records the reel inventory and the all-section fingerprint; right after the restore's
`load_setup` (same Flame call) the recorded EMPTY reels that are missing are created, then the
EMPTY reels the record lacks are deleted (never one that holds a clip or a sequence), and the
all-section fingerprint is compared with the recorded one. Through the real Runtime with the
scripted exec over the fake tree; the fake reproduces what Flame 2025.2.4 was measured to do
(load_setup drops empty reels, adds four default ones, rebuilds reels that hold clips with new
uids; deleting a reel that holds clips deletes its Clip nodes; deleting the last reel makes four
default reels)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyBatch, PyReel
from flame_mcp import reel_fidelity
from flame_mcp.checkpoints import FINGERPRINT_SECTIONS
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import BG1, Rig, make_rig

CLIP1_ID = "/projects/p/ws/P1O/R1/clip1"
CLIP2_ID = "/projects/p/ws/P1O/R1/clip2"
DEFAULTS = [f"Schematic Reel {n}" for n in range(1, 5)]
ENABLED = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path, policy=ENABLED)


def bg(rig: Rig) -> PyBatch:
    batch: PyBatch = rig.handles["bg1"]
    return batch


def reel_names(rig: Rig) -> list[str]:
    return [str(r.name.get_value()) for r in bg(rig).reels]


def node_names(rig: Rig) -> list[str]:
    return sorted(str(n.name.get_value()) for n in bg(rig).nodes)


def make_layout(rig: Rig, names: tuple[str, ...] = ("SrcA", "EmptyB", "Keep")) -> None:
    """SrcA holds clip1 (and so has a Clip node); the other reels are empty."""
    batch = bg(rig)
    for name in names:
        reel = batch.create_reel(name)
        assert reel is not None
        if name == "SrcA":
            reel.receive([rig.flame.find_by_wiretap_node_id(CLIP1_ID)])


def full_fingerprint(rig: Rig) -> str:
    env = rig.do("batch.fingerprint", {"target": BG1, "sections": list(FINGERPRINT_SECTIONS)})
    assert env["ok"] is True, env
    value: str = env["result"]["fingerprint"]
    return value


def checkpoint(rig: Rig) -> dict[str, Any]:
    env = rig.do("batch.checkpoint", {"target": BG1})
    assert env["ok"] is True, env
    result: dict[str, Any] = env["result"]
    return result


def restore(rig: Rig, saved: dict[str, Any]) -> dict[str, Any]:
    return rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})


def add_node(rig: Rig) -> None:
    bg(rig).create_node("Mux")


def strip_from_records(rig: Rig, checkpoint_id: str, **changes: Any) -> None:
    """Rewrite one checkpoint record in the journal file: a key with value `...` is removed."""
    for path in rig.journal.directory.glob("journal_*.jsonl"):
        lines = []
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if record.get("type") == "checkpoint" and record.get("id") == checkpoint_id:
                for key, value in changes.items():
                    if value is ...:
                        record.pop(key, None)
                    else:
                        record[key] = value
            lines.append(json.dumps(record))
        path.write_text("\n".join(lines) + "\n")


def write_calls(rig: Rig) -> list[str | None]:
    return [c.label for c in rig.exec.calls if c.label == "batch.load_setup"]


def test_a_checkpoint_records_the_reel_inventory_in_order_and_the_all_section_fingerprint(
    rig: Rig,
) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    [record] = [r for r in rig.records() if r["type"] == "checkpoint"]
    assert record["reels"] == [
        {"name": "SrcA", "empty": False, "clips": ["clip1"], "sequences": []},
        {"name": "EmptyB", "empty": True, "clips": [], "sequences": []},
        {"name": "Keep", "empty": True, "clips": [], "sequences": []},
    ]
    assert record["reelsError"] is None
    assert record["fingerprintFull"] == full_fingerprint(rig)
    assert record["fingerprintFull"] != record["fingerprint"]  # the default one sees nodes only
    assert record["fingerprintFullSections"] == list(FINGERPRINT_SECTIONS)
    assert set(record["sectionData"]) == set(FINGERPRINT_SECTIONS)
    assert saved["reels"] == record["reels"] and "sectionData" not in saved


def test_a_reel_whose_clips_cannot_be_read_is_recorded_as_not_known_to_be_empty(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_layout(rig, ("EmptyB",))

    def unreadable(self: PyReel) -> Any:
        raise RuntimeError("no clips")

    monkeypatch.setattr(PyReel, "clips", property(unreadable))
    saved = checkpoint(rig)
    assert saved["reels"] == [{"name": "EmptyB", "empty": None, "clips": None, "sequences": []}]


def test_a_restore_recreates_missing_empty_reels_and_deletes_the_default_ones(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    bg(rig).delete_reel(bg(rig).reels[1])  # the user deletes EmptyB
    bg(rig).create_reel("Extra")
    add_node(rig)
    old_uid = bg(rig).reels[0].uid.get_value()

    env = restore(rig, saved)

    assert env["ok"] is True, env
    result = env["result"]
    assert sorted(reel_names(rig)) == ["EmptyB", "Keep", "SrcA"]
    assert result["reelsRestored"] is True and result["matched"] is True
    tidy = result["reelTidy"]
    assert tidy["ran"] is True and tidy["errors"] == []
    assert [c["wanted"] for c in tidy["created"]] == ["EmptyB", "Keep"]
    assert all(c["renamed"] is False for c in tidy["created"])
    assert [d["name"] for d in tidy["deleted"]] == DEFAULTS
    assert tidy["fingerprintAfter"] == saved["fingerprintFull"] == full_fingerprint(rig)
    assert "differences" not in tidy
    assert tidy["recordedReels"] == ["SrcA", "EmptyB", "Keep"]
    assert tidy["orderMatches"] is True and tidy["reelsAfter"] == ["SrcA", "EmptyB", "Keep"]
    assert "reelTidy" not in result["load"]
    src = next(r for r in bg(rig).reels if r.name.get_value() == "SrcA")
    assert src.uid.get_value() != old_uid  # rebuilt with a new uid: this is not restored
    clip_node = next(n for n in bg(rig).nodes if n.name.get_value() == "clip1")
    assert clip_node.clip in src.children


def test_the_tidy_runs_in_the_same_flame_call_as_load_setup(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    add_node(rig)
    rig.exec.calls.clear()
    restore(rig, saved)
    labels = [c.label for c in rig.exec.calls]
    assert labels.count("batch.load_setup") == 1
    load = next(c for c in rig.exec.calls if c.label == "batch.load_setup")
    assert "_result['reelTidy'] = _tidy_reels(target," in load.code


def test_a_reel_that_holds_clips_is_never_deleted_even_when_the_record_lacks_it(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    mine = bg(rig).create_reel("Mine")
    assert mine is not None
    mine.receive([rig.flame.find_by_wiretap_node_id(CLIP2_ID)])
    bg(rig).load_keeps_unsaved_filled_reels = True

    env = restore(rig, saved)

    assert env["ok"] is True, env
    assert "Mine" in reel_names(rig)
    assert next(r for r in bg(rig).reels if r.name.get_value() == "Mine").clips
    tidy = env["result"]["reelTidy"]
    assert {"name": "Mine", "reason": "holds clips or sequences"} in tidy["kept"]
    assert "Mine" not in [d["name"] for d in tidy["deleted"]]
    assert bg(rig).delete_reel_calls == DEFAULTS
    assert env["result"]["reelsRestored"] is False
    only_now = tidy["differences"]["reels"]["onlyNow"]
    assert only_now == [["Mine", ["clip2"], []]]


def test_a_recorded_reel_that_held_clips_is_not_recreated_as_an_empty_one(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    setup = Path(saved["path"])
    payload = json.loads(setup.read_text())
    payload["reels"] = []  # a setup that does not bring SrcA back
    setup.write_text(json.dumps(payload))
    env = restore(rig, saved)
    assert env["ok"] is True, env
    tidy = env["result"]["reelTidy"]
    assert "SrcA" not in reel_names(rig)
    assert [c["wanted"] for c in tidy["created"]] == ["EmptyB", "Keep"]
    assert env["result"]["reelsRestored"] is False
    assert tidy["differences"]["reels"]["onlyRecorded"] == [["SrcA", ["clip1"], []]]


def test_create_comes_before_delete_so_the_last_reel_is_never_deleted(rig: Rig) -> None:
    make_layout(rig, ("Only",))
    saved = checkpoint(rig)
    env = restore(rig, saved)
    assert env["ok"] is True, env
    assert reel_names(rig) == ["Only"] and env["result"]["reelsRestored"] is True
    assert bg(rig).delete_reel_calls == DEFAULTS  # four deletes, all after the create


def test_the_fake_makes_four_default_reels_when_the_last_reel_is_deleted(rig: Rig) -> None:
    """Deleting the last reel of a group makes four default reels.

    Measured on Flame 2025.2.4. This is the trap the order (create, then delete) avoids: doing
    it the other way round ends with the default reels back next to the recreated one."""
    make_layout(rig, ("Only",))
    saved = checkpoint(rig)
    add_node(rig)
    bg(rig).load_setup(saved["path"])
    assert reel_names(rig) == DEFAULTS  # load_setup dropped the empty reel, added the defaults
    for reel in list(bg(rig).reels):
        rig.flame.delete(reel, confirm=False)
    assert reel_names(rig) == DEFAULTS  # the last delete made four new default reels
    bg(rig).create_reel("Only")
    assert len(bg(rig).reels) == 5


def test_a_reel_that_flame_renamed_is_reported_and_kept(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    add_node(rig)
    bg(rig).create_reel_suffix = " 1"
    env = restore(rig, saved)
    assert env["ok"] is True, env
    tidy = env["result"]["reelTidy"]
    assert tidy["created"] == [
        {"wanted": "EmptyB", "got": "EmptyB 1", "renamed": True},
        {"wanted": "Keep", "got": "Keep 1", "renamed": True},
    ]
    assert sorted(reel_names(rig)) == ["EmptyB 1", "Keep 1", "SrcA"]  # not deleted as strangers
    assert env["result"]["reelsRestored"] is False
    assert tidy["differences"]["reels"]["onlyRecorded"] == [
        ["EmptyB", [], []],
        ["Keep", [], []],
    ]


def test_reels_restored_looks_at_the_schematic_reels_only_shelf_and_iterations_are_apart(
    rig: Rig,
) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    bg(rig).create_shelf_reel("Shelf")  # a restore never brings these back, nor does the tidy
    bg(rig).iterate()
    env = restore(rig, saved)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["reelsRestored"] is True  # the schematic reels are what they were
    tidy = result["reelTidy"]
    assert "differences" not in tidy
    assert tidy["outsideTheCheckpoint"] == {
        "shelfReels": {"onlyRecorded": [], "onlyNow": [["Shelf", [], []]]},
        "iterations": {"onlyRecorded": [], "onlyNow": [["BG1_001", 1]]},
    }
    assert full_fingerprint(rig) != saved["fingerprintFull"]  # the all-section one does differ


def test_a_restore_with_nothing_outside_the_checkpoint_reports_an_empty_outside(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    env = restore(rig, saved)
    assert env["result"]["reelTidy"]["outsideTheCheckpoint"] == {}


# The tidy is an after-the-fact correction.


def test_a_failing_delete_leaves_the_nodes_restored_and_is_reported_as_raised(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    add_node(rig)
    bg(rig).delete_reel_raises = "boom: cannot delete"
    env = restore(rig, saved)
    assert env["ok"] is True, env  # what load_setup brought back stands
    result = env["result"]
    assert result["matched"] is True and result["reelsRestored"] is False
    assert "mux1" not in node_names(rig) and "clip1" in node_names(rig)
    tidy = result["reelTidy"]
    assert len(tidy["errors"]) == 4
    assert all(
        e["step"] == "delete" and "RuntimeError: boom: cannot delete" in e["error"]
        for e in tidy["errors"]
    )
    assert tidy["deleted"] == []
    assert set(DEFAULTS) <= set(reel_names(rig))  # still there
    assert tidy["differences"]["reels"]["onlyNow"]
    default_fp = rig.do("batch.fingerprint", {"target": BG1})["result"]["fingerprint"]
    assert default_fp == saved["fingerprint"]


def test_a_create_reel_that_makes_nothing_is_an_error_entry_and_the_rest_goes_on(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    bg(rig).create_reel_makes_nothing = True
    env = restore(rig, saved)
    assert env["ok"] is True, env
    tidy = env["result"]["reelTidy"]
    assert [e["name"] for e in tidy["errors"]] == ["EmptyB", "Keep"]
    assert all(e["error"] == "create_reel returned no reel" for e in tidy["errors"])
    assert [d["name"] for d in tidy["deleted"]] == DEFAULTS  # step (b) still ran
    assert env["result"]["reelsRestored"] is False


def test_a_tidy_that_raises_inside_flame_does_not_fail_the_restore(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    add_node(rig)

    class Exploding(PyBatch):
        @property
        def reels(self) -> Any:  # type: ignore[override]
            raise RuntimeError("reels are gone")

    original = PyBatch.load_setup

    def load_then_lose_the_reels(self: PyBatch, path: str) -> Any:
        returned = original(self, path)
        self.__class__ = Exploding
        return returned

    monkeypatch.setattr(PyBatch, "load_setup", load_then_lose_the_reels)
    env = restore(rig, saved)
    bg(rig).__class__ = PyBatch
    assert env["ok"] is True, env
    tidy = env["result"]["reelTidy"]
    assert tidy["ran"] is True and tidy["errors"]
    assert all("reels are gone" in e["error"] for e in tidy["errors"])
    assert env["result"]["reelsRestored"] is None  # nothing could be read back


def test_a_checkpoint_without_a_reel_inventory_is_not_tidied_and_says_so(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    strip_from_records(rig, saved["checkpoint"], reels=..., fingerprintFull=..., sectionData=...)
    add_node(rig)
    env = restore(rig, saved)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["matched"] is True
    tidy = result["reelTidy"]
    assert tidy["skipped"] is True and tidy["ran"] is False
    assert tidy["reason"] == reel_fidelity.NO_INVENTORY_REASON
    assert result["reelsRestored"] is None
    assert bg(rig).delete_reel_calls == []
    assert set(DEFAULTS) <= set(reel_names(rig))  # what load_setup left is left
    load = next(c for c in rig.exec.calls if c.label == "batch.load_setup")
    assert "_tidy_reels(target" not in load.code.split("__op_src = ")[1]


def test_a_checkpoint_whose_reels_could_not_be_read_says_why(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    strip_from_records(rig, saved["checkpoint"], reels=None, reelsError="RuntimeError: nope")
    env = restore(rig, saved)
    assert env["ok"] is True, env
    assert env["result"]["reelTidy"]["skipped"] is True
    assert env["result"]["reelTidy"]["reason"].endswith("(RuntimeError: nope)")


def test_the_last_reel_of_a_group_is_never_deleted(rig: Rig) -> None:
    make_layout(rig, ("E1",))
    saved = checkpoint(rig)
    strip_from_records(rig, saved["checkpoint"], reels=[], fingerprintFull=..., sectionData=...)
    env = restore(rig, saved)
    assert env["ok"] is True, env
    tidy = env["result"]["reelTidy"]
    assert len(tidy["deleted"]) == 3 and len(bg(rig).reels) == 1
    assert tidy["kept"] == [{"name": "Schematic Reel 4", "reason": "the last reel of the group"}]
    assert env["result"]["reelsRestored"] is None
    assert tidy["comparison"] == "not compared: the checkpoint holds no reel section data"


# Everything that restores a checkpoint tidies.


def test_batch_load_setup_of_a_file_keeps_the_files_reels_and_says_so(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["ok"] is True, env
    tidy = env["result"]["reelTidy"]
    assert tidy["skipped"] is True and tidy["reason"] == reel_fidelity.NOT_A_CHECKPOINT_REASON
    assert env["result"]["reelsRestored"] is None
    assert bg(rig).delete_reel_calls == []
    assert set(DEFAULTS) <= set(reel_names(rig))


def test_revert_of_a_load_setup_puts_the_layout_from_before_it_back(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    bg(rig).delete_reel(bg(rig).reels[2])  # Keep is gone: the layout is now SrcA, EmptyB
    before = full_fingerprint(rig)
    loaded = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert loaded["ok"] is True, loaded
    assert full_fingerprint(rig) != before
    back = rig.do("journal.revert", {"group": loaded["journal"]["group"]})
    assert back["ok"] is True, back
    assert back["result"]["reelsRestored"] is True
    assert sorted(reel_names(rig)) == ["EmptyB", "SrcA"]
    assert full_fingerprint(rig) == before


def test_revert_of_a_checkpointed_write_tidies(rig: Rig) -> None:
    make_layout(rig, ("SrcA", "EmptyB"))
    before = full_fingerprint(rig)
    env = rig.do("batch.organize", {"target": BG1})
    assert env["ok"] is True, env
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and back["result"]["reelsRestored"] is True, back
    assert sorted(reel_names(rig)) == ["EmptyB", "SrcA"] and full_fingerprint(rig) == before


def test_revert_of_a_destructive_eval_tidies(rig: Rig) -> None:
    make_layout(rig)
    before = full_fingerprint(rig)
    env = rig.do(
        "eval.run",
        {"code": "target.create_node('Mux')\n_result = 1", "effect": "destructive", "target": BG1},
    )
    assert env["ok"] is True, env
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and back["result"]["reelsRestored"] is True, back
    assert sorted(reel_names(rig)) == ["EmptyB", "Keep", "SrcA"]
    assert full_fingerprint(rig) == before


def test_undoing_a_restore_tidies_to_the_layout_before_that_restore(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    bg(rig).delete_reel(bg(rig).reels[2])
    before = full_fingerprint(rig)
    first = restore(rig, saved)
    assert first["ok"] is True and first["result"]["reelsRestored"] is True
    pre = first["result"]["preCheckpoint"]["id"]
    second = rig.do("journal.restore_checkpoint", {"checkpoint": pre})
    assert second["ok"] is True and second["result"]["reelsRestored"] is True, second
    assert sorted(reel_names(rig)) == ["EmptyB", "SrcA"] and full_fingerprint(rig) == before


def test_the_outcome_record_carries_reels_restored(rig: Rig) -> None:
    make_layout(rig)
    saved = checkpoint(rig)
    env = restore(rig, saved)
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.outcome is not None and view.outcome["reelsRestored"] is True


def test_record_of_needs_a_list_of_named_rows() -> None:
    record, why = reel_fidelity.record_of({"reels": [{"name": "A", "empty": True}]})
    assert record is not None and why is None and record.fingerprint is None
    assert reel_fidelity.record_of({})[0] is None
    assert reel_fidelity.record_of({"reels": [{"empty": True}]})[0] is None
    assert reel_fidelity.record_of({"reels": None, "reelsError": "x"})[1] is not None


def test_differences_is_a_multiset_difference_per_section() -> None:
    recorded = {"reels": [["A", [], []], ["B", [], []]], "iterations": [["i", 1]]}
    now = {"reels": [["B", [], []], ["B", [], []]], "iterations": [["i", 1]]}
    assert reel_fidelity.differences(recorded, now) == {
        "reels": {"onlyRecorded": [["A", [], []]], "onlyNow": [["B", [], []]]}
    }
    assert reel_fidelity.differences(recorded, recorded) == {}
    assert reel_fidelity.differences(None, now) == {}


def test_report_when_the_load_did_not_complete_says_so() -> None:
    record, _ = reel_fidelity.record_of({"reels": [], "fingerprintFull": "f"})
    out = reel_fidelity.report(record, None, None)
    assert out["ran"] is False and out["reelsRestored"] is None
    assert "did not complete" in out["reason"]


def test_the_order_of_the_reels_is_reported_but_not_restored(rig: Rig) -> None:
    make_layout(rig, ("EmptyB", "SrcA"))
    saved = checkpoint(rig)
    add_node(rig)
    env = restore(rig, saved)
    tidy = env["result"]["reelTidy"]
    assert env["result"]["reelsRestored"] is True  # the fingerprint sorts its rows
    assert tidy["recordedReels"] == ["EmptyB", "SrcA"]
    assert tidy["reelsAfter"] == ["SrcA", "EmptyB"] and tidy["orderMatches"] is False
