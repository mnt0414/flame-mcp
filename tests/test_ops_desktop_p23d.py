"""Tests for `desktop.create` (Batch group / reel group / reel) and `desktop.copy`.

Covers their journal records, `journal.revert` (delete what was made, with a compare-and-swap) and
`journal.check`.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py). The fake follows what was measured on Flame
2025.2.4 (see the docstrings in fake_media_tree.py): a test here describes the fake, and whether
the real Flame does the same is what scripts/p2_3d_desktop_e2e.py measures."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyBatch, PyClip, PyReelGroup, PySegment, PyTime
from flame_mcp import desktop_run, execution
from flame_mcp.desktop_prelude import DESKTOP_HELPER
from flame_mcp.desktop_run import CURRENT_MOVED_NOTE
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from journal_test_helpers import Rig, make_rig
from op_test_helpers import run_op

RG_WT = "/wt/RG"
REEL1_WT = "/wt/RG/Reel 1"
LIB_WT = "/projects/p/ws/P1O"
CLIP1 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip1"}
CLIP2 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip2"}
RG = {"kind": "wiretap", "id": RG_WT}
REEL1 = {"kind": "wiretap", "id": REEL1_WT}
LIB = {"kind": "wiretap", "id": LIB_WT}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_rig(monkeypatch, tmp_path)
    for key, wiretap in (
        ("lib", LIB_WT),
        ("clip1", CLIP1["id"]),
        ("clip2", CLIP2["id"]),
    ):
        made.handles[key] = made.flame.find_by_wiretap_node_id(wiretap)
    return made


def desktop(rig: Rig) -> Any:
    return rig.flame.projects.current_project.current_workspace.desktop


def batch_names(rig: Rig) -> list[str]:
    return [str(g.name.get_value()) for g in desktop(rig).batch_groups]


def group_names(rig: Rig) -> list[str]:
    return [str(g.name.get_value()) for g in desktop(rig).reel_groups]


def current_name(rig: Rig) -> Any:
    cur = desktop(rig).current_batch_group.get_value()
    return None if cur is None else str(cur.name.get_value())


def create(rig: Rig, **args: Any) -> dict[str, Any]:
    return rig.do("desktop.create", args)


def revert(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    return rig.do("journal.revert", {"group": env["journal"]["group"]})


def types_in(rig: Rig) -> list[str]:
    return [str(r.get("type")) for r in rig.records()]


def entry_names(reel: Any) -> list[str]:
    return [str(c.name.get_value()) for c in reel.children]


def write_calls(rig: Rig) -> int:
    """Exec calls that carried the op's WRITE code (`_result = _dc_create(` / `_dc_copy(`): the
    read phase asks `_dc_snapshot` / `_dc_copy_snapshot` instead."""
    return sum(
        1
        for c in rig.exec.calls
        if "_result = _dc_create(" in c.code or "_result = _dc_copy(" in c.code
    )


def test_create_batch_group_reads_back_what_flame_made_and_says_current_moved(rig: Rig) -> None:
    before = rig.handles["bg2"]
    env = create(rig, kind="batch_group", name="NEWBG")
    assert env["ok"] is True, env
    result = env["result"]
    assert result["name"] == "NEWBG" and result["renamed"] is False
    assert result["selector"] == {"kind": "batch", "uid": result["uid"]}
    assert result["reels"] == [f"Schematic Reel {i}" for i in range(1, 5)]
    assert result["shelfReels"] == ["Batch Renders"]
    assert (result["startFrame"], result["duration"], result["nodeCount"]) == (1, 100, 0)
    assert result["previousCurrent"] == {"name": "BG2", "uid": before.uid.get_value()}
    assert result["current"] == {"name": "NEWBG", "uid": result["uid"]}
    assert result["currentMoved"] is True
    assert CURRENT_MOVED_NOTE in result["notes"]
    assert "NEWBG" in batch_names(rig) and current_name(rig) == "NEWBG"
    assert env["execution"]["effect"] == "create" and env["undoSteps"] == 0
    assert env["journal"]["revert"] == {
        "op": "journal.revert",
        "args": {"group": env["journal"]["group"]},
    }


def test_create_batch_group_with_options_reads_them_back(rig: Rig) -> None:
    env = create(
        rig,
        kind="batch_group",
        name="OPT",
        reels=["FR_A", "FR_B"],
        shelf_reels=["FS_A"],
        start_frame=1001,
        duration=48,
    )
    assert env["ok"] is True, env
    result = env["result"]
    assert result["reels"] == ["FR_A", "FR_B"] and result["shelfReels"] == ["FS_A"]
    assert (result["startFrame"], result["duration"]) == (1001, 48)
    assert env["degraded"] == []


def test_a_read_back_that_differs_from_what_was_asked_is_reported_in_degraded(rig: Rig) -> None:
    def start_ignored(group: PyBatch) -> None:
        group.start_frame.set_value(1)

    original = rig.flame.projects.current_project.current_workspace.desktop.create_batch_group

    def creating(*args: Any, **kwargs: Any) -> Any:
        made = original(*args, **kwargs)
        start_ignored(made)
        return made

    desktop(rig).create_batch_group = creating
    env = create(rig, kind="batch_group", name="OPT", start_frame=1001)
    assert env["ok"] is True
    assert any("startFrame" in note for note in env["degraded"])
    assert env["result"]["startFrame"] == 1


def test_a_taken_name_refuses_before_anything_is_written_or_journaled(rig: Rig) -> None:
    env = create(rig, kind="batch_group", name="BG1")
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["ran"] is False
    assert "BG1" in env["result"]["existingNames"] and "BG2" in env["result"]["existingNames"]
    assert batch_names(rig) == ["BG1", "BG2"] and current_name(rig) == "BG2"
    assert rig.records() == []
    assert write_calls(rig) == 0


def test_allow_rename_lets_flame_rename_and_says_so(rig: Rig) -> None:
    env = create(rig, kind="batch_group", name="BG1", allow_rename=True)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["requestedName"] == "BG1" and result["name"] == "BG1 1"
    assert result["renamed"] is True
    assert batch_names(rig) == ["BG1", "BG2", "BG1 1"]


def test_create_reel_group_lists_the_default_reels(rig: Rig) -> None:
    env = create(rig, kind="reel_group", name="NRG")
    assert env["ok"] is True, env
    result = env["result"]
    assert [r["name"] for r in result["reelRows"]] == [
        "Reel 1",
        "Reel 2",
        "Reel 3",
        "Reel 4",
        "Sequences",
    ]
    assert result["reelRows"][-1]["type"] == "Sequences"
    assert result["selector"]["kind"] == "wiretap"
    assert group_names(rig) == ["RG", "NRG"]
    assert current_name(rig) == "BG2"  # a reel group does not move the current Batch group
    assert result["previousCurrent"] is None and "notes" not in result


def test_a_taken_reel_group_name_refuses_and_allow_rename_takes_the_next_number(rig: Rig) -> None:
    assert create(rig, kind="reel_group", name="RG")["errorCode"] == "PRECONDITION_UNMET"
    first = create(rig, kind="reel_group", name="G1")
    assert first["ok"] is True
    second = create(rig, kind="reel_group", name="G1", allow_rename=True)
    assert second["ok"] is True and second["result"]["name"] == "G2"
    assert second["result"]["renamed"] is True


def test_create_reel_goes_at_the_end_of_the_reel_group(rig: Rig) -> None:
    env = create(rig, kind="reel", name="RL1", parent=RG)
    assert env["ok"] is True, env
    result = env["result"]
    assert (result["name"], result["type"], result["sequence"]) == ("RL1", "Reel", False)
    assert entry_names(rig.handles["reel_group"])[:0] == []
    assert [str(r.name.get_value()) for r in rig.handles["reel_group"].reels] == [
        "Reel 1",
        "RL1",
    ]
    assert result["parent"]["name"] == "RG"


def test_create_sequences_reel_and_a_taken_reel_name(rig: Rig) -> None:
    seq = create(rig, kind="reel", name="SQ1", parent=RG, sequence=True)
    assert seq["ok"] is True and seq["result"]["type"] == "Sequences"
    taken = create(rig, kind="reel", name="Reel 1", parent=RG)
    assert taken["errorCode"] == "PRECONDITION_UNMET"
    assert taken["result"]["existingNames"] == ["Reel 1", "SQ1"]
    renamed = create(rig, kind="reel", name="Reel 1", parent=RG, allow_rename=True)
    assert renamed["ok"] is True and renamed["result"]["name"] == "Reel 2"


def test_create_reel_in_a_library_but_not_a_sequences_one(rig: Rig) -> None:
    env = create(rig, kind="reel", name="LR", parent=LIB)
    assert env["ok"] is True, env
    assert env["result"]["parent"]["type"] == "PyLibrary"
    assert [str(r.name.get_value()) for r in rig.handles["lib"].reels] == ["R1", "LR"]
    refused = create(rig, kind="reel", name="LS", parent=LIB, sequence=True)
    assert refused["ok"] is False and refused["errorCode"] == "INVALID_ARGS"
    assert "library" in refused["error"] and len(rig.records()) == 2  # only the first call's pair


def test_create_reel_needs_a_reel_group_or_library_parent(rig: Rig) -> None:
    env = create(rig, kind="reel", name="X", parent=REEL1)
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE"
    assert "reel group" in env["error"] and rig.records() == []


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"kind": "folder", "name": "x"}, "kind must be one of"),
        ({"kind": "reel", "name": "x"}, "needs parent"),
        ({"kind": "batch_group", "name": "x", "parent": RG}, "parent only applies"),
        ({"kind": "reel_group", "name": "x", "reels": ["a"]}, "only apply to kind='batch_group'"),
        ({"kind": "reel_group", "name": "x", "duration": 5}, "only apply to kind='batch_group'"),
        ({"kind": "batch_group", "name": "x", "sequence": True}, "sequence only applies"),
        ({"kind": "batch_group", "name": "x", "reels": []}, "must hold 1 to"),
        ({"kind": "batch_group", "name": "x", "shelf_reels": [""]}, "non-empty strings"),
    ],
)
def test_bad_argument_combinations_are_refused_before_anything_runs(
    rig: Rig, args: dict[str, Any], needle: str
) -> None:
    env = create(rig, **args)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert needle in env["error"]
    assert rig.exec.calls == [] and rig.records() == []


def test_create_journals_an_intent_before_the_write_and_an_outcome_with_what_it_made(
    rig: Rig,
) -> None:
    env = create(rig, kind="batch_group", name="NEWBG")
    records = rig.records()
    assert types_in(rig) == ["intent", "outcome"]
    intent, outcome = records
    assert intent["kind"] == "desktop_create" and intent["op"] == "desktop.create"
    assert intent["createKind"] == "batch_group"
    assert sorted(intent["existing"]) == ["uid-b1", "uid-b2"]
    assert intent["previousCurrent"]["name"] == "BG2"
    assert outcome["ok"] is True and outcome["created"]["uid"] == env["result"]["uid"]
    assert outcome["created"]["state"]["nodeCount"] == 0
    # the exec calls: a read phase (no journal yet), then the write with the intent already flushed
    phases = [c for c in rig.exec.calls if c.label == "desktop.create"]
    assert len(phases) == 2 and write_calls(rig) == 1
    assert phases[0].journal_types == [] and phases[1].journal_types == ["intent"]
    listing = rig.do("journal.list", {})
    assert listing["result"]["items"][0]["state"] == "landed"


def test_the_write_carries_the_uids_it_read_and_conflicts_when_the_list_changed(rig: Rig) -> None:
    def sneak(label: str | None) -> None:
        if label == "desktop.create" and len(rig.exec.calls) == 2:  # the write, not the read
            desktop(rig).create_batch_group("SNEAK")

    rig.exec.before_run = sneak
    env = create(rig, kind="batch_group", name="NEWBG")
    assert env["ok"] is False and env["errorCode"] == "CONFLICT"
    assert "changed between" in env["error"]
    assert batch_names(rig) == ["BG1", "BG2", "SNEAK"]
    _intent, outcome = rig.records()
    assert outcome["ok"] is False and outcome["errorCode"] == "CONFLICT"
    assert rig.do("journal.list", {})["result"]["items"][0]["state"] == "conflict"
    assert env["journal"]["revert"] is None  # nothing was made


def test_a_parent_given_by_path_or_selection_is_re_addressed_by_wiretap_for_the_write(
    rig: Rig,
) -> None:
    by_path = create(
        rig, kind="reel", name="P1", parent={"kind": "path", "root": "desktop", "path": ["RG"]}
    )
    assert by_path["ok"] is True, by_path
    rig.flame.media_panel.selected_entries = [rig.handles["reel_group"]]
    by_selection = create(rig, kind="reel", name="P2", parent={"kind": "selection"})
    assert by_selection["ok"] is True, by_selection
    intents = [r for r in rig.records() if r["type"] == "intent"]
    assert [i["target"] for i in intents] == [RG, RG]
    assert intents[0]["args"]["parent"]["kind"] == "path"  # what the caller asked is kept as asked


def test_copy_sources_and_destination_by_path_and_selection(rig: Rig) -> None:
    rig.flame.media_panel.selected_entries = [rig.handles["reel_group"].reels[0]]
    env = copy(
        rig,
        [{"kind": "path", "root": "desktop", "path": ["RG", "Reel 1", "c1"]}, CLIP2],
        {"kind": "selection"},
    )
    assert env["ok"] is True, env
    assert [c["name"] for c in env["result"]["copies"]] == ["c1", "clip2"]
    intent = rig.records()[0]
    assert intent["target"] == REEL1


def test_revert_deletes_the_batch_group_and_says_current_did_not_go_back(rig: Rig) -> None:
    env = create(rig, kind="batch_group", name="NEWBG")
    out = revert(rig, env)
    assert out["ok"] is True, out
    result = out["result"]
    assert result["deleted"] is True and result["reverted"] == env["journal"]["group"]
    assert "NEWBG" not in batch_names(rig)
    assert result["previousCurrent"]["name"] == "BG2"
    assert result["current"]["name"] == "BG1"  # the fake picks the first that is left
    assert result["current"]["uid"] != result["previousCurrent"]["uid"]
    assert any("does NOT make the group that was current before" in n for n in result["notes"])
    listing = {i["group"]: i for i in rig.do("journal.list", {})["result"]["items"]}
    assert listing[env["journal"]["group"]]["state"] == "reverted"
    assert listing[out["journal"]["group"]]["reverts"] == env["journal"]["group"]
    reverted_intent = next(r for r in rig.records() if r.get("kind") == "desktop_revert")
    assert reverted_intent["reverts"] == env["journal"]["group"]
    assert reverted_intent["revertsIntent"] == env["journal"]["id"]


def test_revert_twice_is_refused(rig: Rig) -> None:
    env = create(rig, kind="reel_group", name="NRG")
    assert revert(rig, env)["ok"] is True
    again = revert(rig, env)
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"
    assert "already reverted" in again["error"]
    assert group_names(rig) == ["RG"]


def test_a_revert_is_not_itself_revertible(rig: Rig) -> None:
    env = create(rig, kind="reel_group", name="NRG")
    out = revert(rig, env)
    again = rig.do("journal.revert", {"group": out["journal"]["group"]})
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"
    assert group_names(rig) == ["RG"]


def test_revert_of_a_batch_group_with_a_node_is_a_conflict_and_deletes_nothing(rig: Rig) -> None:
    env = create(rig, kind="batch_group", name="NEWBG")
    made = next(g for g in desktop(rig).batch_groups if str(g.name.get_value()) == "NEWBG")
    made.create_node("Blur")
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert "1 node" in " ".join(out["result"]["conflicts"])
    assert "NEWBG" in batch_names(rig) and len(made.nodes) == 1
    assert current_name(rig) == "NEWBG"
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert states[env["journal"]["group"]] == "landed"  # not reverted: it can be tried again
    made.remove_node(made.nodes[0])
    assert revert(rig, env)["ok"] is True and "NEWBG" not in batch_names(rig)


def test_revert_of_a_batch_group_whose_reel_holds_a_clip_or_was_renamed_is_a_conflict(
    rig: Rig,
) -> None:
    env = create(rig, kind="batch_group", name="NEWBG", reels=["A", "B"], shelf_reels=["S"])
    made = next(g for g in desktop(rig).batch_groups if str(g.name.get_value()) == "NEWBG")
    shelf_clip = PyClip("sh", wiretap_id="/wt/sh")
    made.shelf_reels[0]._children.append(shelf_clip)
    first = revert(rig, env)
    assert first["errorCode"] == "CONFLICT" and "shelf reel 'S'" in " ".join(
        first["result"]["conflicts"]
    )
    made.shelf_reels[0]._children.clear()
    made.reels[1].name.set_value("B_renamed")
    second = revert(rig, env)
    assert second["errorCode"] == "CONFLICT"
    assert "were ['A', 'B']" in " ".join(second["result"]["conflicts"])
    made.reels[1].name.set_value("B")
    assert revert(rig, env)["ok"] is True


def test_a_batch_group_that_was_renamed_is_still_reverted(rig: Rig) -> None:
    env = create(rig, kind="batch_group", name="NEWBG")
    next(
        g for g in desktop(rig).batch_groups if g.uid.get_value() == env["result"]["uid"]
    ).name.set_value("Other")
    assert revert(rig, env)["ok"] is True
    assert batch_names(rig) == ["BG1", "BG2"]


def test_revert_of_a_reel_group_needs_every_reel_empty(rig: Rig) -> None:
    env = create(rig, kind="reel_group", name="NRG")
    made = next(g for g in desktop(rig).reel_groups if str(g.name.get_value()) == "NRG")
    made.reels[2]._children.append(PyClip("c", wiretap_id="/wt/c"))
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and "reel 'Reel 3'" in " ".join(
        out["result"]["conflicts"]
    )
    assert "NRG" in group_names(rig)
    made.reels[2]._children.clear()
    assert revert(rig, env)["ok"] is True and group_names(rig) == ["RG"]


def test_revert_of_a_reel_needs_it_empty(rig: Rig) -> None:
    env = create(rig, kind="reel", name="RL1", parent=RG)
    reel = rig.handles["reel_group"].reels[-1]
    reel._children.append(PyClip("c", wiretap_id="/wt/c"))
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and "holds 1" in " ".join(out["result"]["conflicts"])
    assert len(rig.handles["reel_group"].reels) == 2
    reel._children.clear()
    done = revert(rig, env)
    assert done["ok"] is True and len(rig.handles["reel_group"].reels) == 1
    assert "notes" not in done["result"]


def test_reverting_the_last_reel_of_a_group_notes_that_flame_makes_default_reels(rig: Rig) -> None:
    env = create(rig, kind="reel", name="RL1", parent=RG)
    group: PyReelGroup = rig.handles["reel_group"]
    group.reels = [group.reels[-1]]  # the created reel is now the only one
    group.sync_children()
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert desktop_run.LAST_REEL_NOTE in out["result"]["notes"]
    assert out["result"]["parentReelsBefore"] == ["RL1"]
    assert out["result"]["parentReelsAfter"] == [
        "Reel 1",
        "Reel 2",
        "Reel 3",
        "Reel 4",
        "Sequences",
    ]


def test_revert_of_a_library_reel(rig: Rig) -> None:
    env = create(rig, kind="reel", name="LR", parent=LIB)
    out = revert(rig, env)
    assert out["ok"] is True and out["result"]["deleted"] is True
    assert [str(r.name.get_value()) for r in rig.handles["lib"].reels] == ["R1"]


def test_revert_of_something_already_gone_is_ok_and_says_so(rig: Rig) -> None:
    env = create(rig, kind="reel_group", name="NRG")
    made = next(g for g in desktop(rig).reel_groups if str(g.name.get_value()) == "NRG")
    rig.flame.delete(made, confirm=False)
    out = revert(rig, env)
    assert out["ok"] is True and out["result"]["alreadyGone"] is True
    assert out["result"]["deleted"] is False


def test_a_delete_that_does_nothing_is_an_error_and_the_group_stays_revertible(rig: Rig) -> None:
    env = create(rig, kind="reel_group", name="NRG")
    real = rig.flame.delete
    rig.flame.delete = lambda obj, confirm=True: True  # claims success, deletes nothing
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "SILENT_NOOP"
    assert "still there" in out["error"] and "NRG" in group_names(rig)
    rig.flame.delete = real
    assert revert(rig, env)["ok"] is True and group_names(rig) == ["RG"]


def test_revert_is_refused_while_the_call_it_reverts_has_no_outcome(rig: Rig) -> None:
    rig.exec.fault("desktop.create", "BRIDGE_LOST", when="after", skip=1)
    env = create(rig, kind="reel_group", name="NRG")
    assert env["errorCode"] == "BRIDGE_LOST"
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "POLICY_DENIED"  # the write door is shut


def test_a_lost_answer_shuts_the_door_and_check_finds_what_landed_then_revert_deletes_it(
    rig: Rig,
) -> None:
    rig.exec.fault("desktop.create", "TIMEOUT", when="after", skip=1)
    env = create(rig, kind="reel_group", name="NRG")
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    assert "journal.check" in env["hint"]
    group = env["journal"]["group"]
    assert types_in(rig) == ["intent", "unresolved"]
    assert rig.do("journal.list", {})["result"]["items"][0]["state"] == "unknown"
    denied = create(rig, kind="reel_group", name="OTHER")
    assert denied["errorCode"] == "POLICY_DENIED"

    checked = rig.do("journal.check", {"group": group})
    assert checked["ok"] is True, checked
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "landed" and row["created"]["name"] == "NRG"
    assert checked["result"]["writesBlocked"] is False
    assert rig.do("journal.list", {})["result"]["items"][0]["state"] == "landed"

    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True, out
    assert group_names(rig) == ["RG"]


def test_check_says_not_landed_when_the_call_never_reached_flame(rig: Rig) -> None:
    rig.exec.fault("desktop.create", "BRIDGE_LOST", when="before", skip=1)
    env = create(rig, kind="reel_group", name="NRG")
    assert env["errorCode"] == "BRIDGE_LOST"
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "not_landed"
    assert group_names(rig) == ["RG"]
    assert create(rig, kind="reel_group", name="NRG")["ok"] is True  # the door is open again


def test_check_of_a_create_that_found_two_new_entries_is_diverged(rig: Rig) -> None:
    rig.exec.fault("desktop.create", "TIMEOUT", when="after", skip=1)
    env = create(rig, kind="reel_group", name="NRG")
    desktop(rig).create_reel_group("SOMEONE")
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "diverged"


def test_a_lost_answer_of_a_revert_is_decided_by_check(rig: Rig) -> None:
    env = create(rig, kind="reel_group", name="NRG")
    rig.exec.fault("desktop.create", "BRIDGE_LOST", when="after")
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "BRIDGE_LOST"
    assert group_names(rig) == ["RG"]  # the delete did run
    new_group = out["journal"]["group"]
    checked = rig.do("journal.check", {"group": new_group})
    assert checked["result"]["checked"][0]["verdict"] == "landed"
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert states[env["journal"]["group"]] == "reverted"


def copy(rig: Rig, sources: list[dict[str, Any]], destination: dict[str, Any]) -> dict[str, Any]:
    return rig.do("desktop.copy", {"sources": sources, "destination": destination})


def test_copy_library_clip_to_a_desktop_reel_makes_a_new_clip_and_keeps_the_source(
    rig: Rig,
) -> None:
    env = copy(rig, [CLIP1], REEL1)
    assert env["ok"] is True, env
    result = env["result"]
    assert (result["requested"], result["returned"]) == (1, 1)
    [made] = result["copies"]
    source = rig.handles["clip1"]
    assert made["name"] == "clip1" and made["uid"] != source.uid.get_value()
    assert made["sourceUid"] == source.uid.get_value() and made["inDestination"] is True
    assert made["selector"] == {"kind": "wiretap", "id": made["wiretapId"]}
    assert result["sourcesRemain"] is True
    assert result["destinationEntriesBefore"] == ["c1"]
    assert result["destinationEntriesAfter"] == ["c1", "clip1"]
    assert rig.flame.find_by_uid(source.uid.get_value()) is source
    assert entry_names(rig.handles["reel_group"].reels[0]) == ["c1", "clip1"]
    assert env["affected"] == 1 and env["undoSteps"] == 0
    assert rig.flame.media_panel.duplicate_actions == ["add"]


def test_copy_from_a_desktop_reel_to_another_and_several_at_once_in_the_order_given(
    rig: Rig,
) -> None:
    other = create(rig, kind="reel", name="Other", parent=RG)["result"]["selector"]
    one = copy(rig, [REEL1_CLIP], other)
    assert one["ok"] is True and one["result"]["copies"][0]["name"] == "c1"
    third = create(rig, kind="reel", name="Third", parent=RG)["result"]["selector"]
    many = copy(rig, [CLIP2, CLIP1, REEL1_CLIP], third)
    assert many["ok"] is True
    assert [c["name"] for c in many["result"]["copies"]] == ["clip2", "clip1", "c1"]
    assert [c["sourceUid"] for c in many["result"]["copies"]] == [
        rig.handles["clip2"].uid.get_value(),
        rig.handles["clip1"].uid.get_value(),
        rig.handles["reel"].children[0].uid.get_value(),
    ]
    assert len({c["uid"] for c in many["result"]["copies"]}) == 3


REEL1_CLIP = {"kind": "path", "root": "desktop", "path": ["RG", "Reel 1", "c1"]}


def test_copying_a_name_that_is_already_there_adds_another_clip(rig: Rig) -> None:
    assert copy(rig, [CLIP1], REEL1)["ok"] is True
    again = copy(rig, [CLIP1], REEL1)
    assert again["ok"] is True
    assert entry_names(rig.handles["reel_group"].reels[0]) == ["c1", "clip1", "clip1"]


def test_copy_into_a_library(rig: Rig) -> None:
    env = copy(rig, [REEL1_CLIP], LIB)
    assert env["ok"] is True, env
    assert env["result"]["destination"]["type"] == "PyLibrary"
    assert env["result"]["copies"][0]["inDestination"] is True and env["result"]["warnings"] == []


def test_copy_into_a_reel_of_a_batch_group_is_refused_with_a_pointer_to_add_clips(
    rig: Rig,
) -> None:
    bg = rig.handles["bg1"]
    reel = bg.create_reel("Schematic Reel 1")
    shelf = bg.create_shelf_reel("Batch Renders")
    for target in (reel, shelf):
        env = copy(rig, [CLIP1], {"kind": "wiretap", "id": target.get_wiretap_node_id()})
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
        assert "batch.add_clips" in env["hint"] and "batch.add_clips" in env["error"]
    assert bg.nodes[-1].name.get_value() == "action1" and reel.children == []
    assert rig.records() == [] and write_calls(rig) == 0


def test_copy_refuses_a_source_that_is_not_a_clip_and_a_destination_that_is_not_a_container(
    rig: Rig,
) -> None:
    bad_source = copy(rig, [REEL1], REEL1)
    assert bad_source["errorCode"] == "SELECTOR_TYPE" and "sources[0]" in bad_source["error"]
    bad_dest = copy(rig, [CLIP1], CLIP2)
    assert (
        bad_dest["errorCode"] == "SELECTOR_TYPE" and "destination is a PyClip" in bad_dest["error"]
    )
    assert rig.records() == []


@pytest.mark.parametrize(
    ("sources", "needle"),
    [
        ([], "must hold 1 to 50"),
        ([CLIP1] * 51, "must hold 1 to 50"),
        ([{"kind": "desktop"}], "not accepted here"),
    ],
)
def test_copy_argument_checks(rig: Rig, sources: list[Any], needle: str) -> None:
    env = copy(rig, sources, REEL1)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS" and needle in env["error"]
    assert rig.exec.calls == []


def test_copy_does_not_offer_duplicate_action(rig: Rig) -> None:
    env = rig.do(
        "desktop.copy", {"sources": [CLIP1], "destination": REEL1, "duplicate_action": "replace"}
    )
    assert env["errorCode"] == "INVALID_ARGS" and "unknown parameter" in env["error"]


def test_copy_journals_the_copies_and_conflicts_when_the_destination_changed(rig: Rig) -> None:
    ok = copy(rig, [CLIP1, CLIP2], REEL1)
    intent, outcome = rig.records()
    assert intent["kind"] == "desktop_copy" and intent["existing"] == [
        rig.handles["reel"].children[0].uid.get_value()
    ]
    assert [c["name"] for c in outcome["created"]["copies"]] == ["clip1", "clip2"]
    assert ok["journal"]["revert"]["args"]["group"] == ok["journal"]["group"]

    def sneak(label: str | None) -> None:
        if label == "desktop.copy" and len(rig.exec.calls) == 4:  # the second call's write
            rig.handles["reel_group"].reels[0]._children.append(PyClip("x", wiretap_id="/wt/x"))

    rig.exec.before_run = sneak
    refused = copy(rig, [CLIP1], REEL1)
    assert refused["errorCode"] == "CONFLICT" and "changed between" in refused["error"]
    assert entry_names(rig.handles["reel_group"].reels[0]).count("clip1") == 1


def test_revert_of_a_copy_deletes_those_copies_only(rig: Rig) -> None:
    env = copy(rig, [CLIP1, CLIP2], REEL1)
    reel = rig.handles["reel_group"].reels[0]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert [d["name"] for d in out["result"]["deleted"]] == ["clip1", "clip2"]
    assert out["result"]["skipped"] == [] and out["affected"] == 2
    assert entry_names(reel) == ["c1"]
    assert rig.flame.find_by_uid(rig.handles["clip1"].uid.get_value()) is not None
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert states[env["journal"]["group"]] == "reverted"
    assert revert(rig, env)["errorCode"] == "PRECONDITION_UNMET"


def test_revert_of_a_copy_skips_a_copy_that_is_gone_and_deletes_an_unchanged_one(
    rig: Rig,
) -> None:
    env = copy(rig, [CLIP1, CLIP2], REEL1)
    reel = rig.handles["reel_group"].reels[0]
    first, second = reel.children[1], reel.children[2]
    rig.flame.delete(first, confirm=False)
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert [s["name"] for s in out["result"]["skipped"]] == ["clip1"]
    assert "already deleted" in out["result"]["skipped"][0]["reason"]
    assert [d["uid"] for d in out["result"]["deleted"]] == [second.uid.get_value()]
    assert entry_names(reel) == ["c1"]


def edit_content(clip: Any) -> None:
    clip.versions[0].tracks[0]._segments.append(PySegment(name="added by hand"))


@pytest.mark.parametrize(
    "edit",
    [
        lambda clip: clip.name.set_value("renamed by hand"),
        lambda clip: setattr(clip, "duration", PyTime(7)),
        edit_content,
    ],
    ids=["name", "duration", "content"],
)
def test_revert_of_a_copy_is_a_conflict_and_deletes_nothing_when_one_copy_was_edited(
    rig: Rig, edit: Any
) -> None:
    env = copy(rig, [CLIP1, CLIP2], REEL1)
    reel = rig.handles["reel_group"].reels[0]
    edit(reel.children[2])
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "nothing was deleted" in out["error"] and "clip2" in out["error"]
    assert len(out["result"]["conflicts"]) == 1 and out["result"]["deleted"] == []
    assert len(reel.children) == 3
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert states[env["journal"]["group"]] == "landed"


def test_revert_of_a_copy_with_one_gone_and_one_edited_deletes_nothing(rig: Rig) -> None:
    env = copy(rig, [CLIP1, CLIP2], REEL1)
    reel = rig.handles["reel_group"].reels[0]
    rig.flame.delete(reel.children[1], confirm=False)
    reel.children[1].name.set_value("renamed by hand")
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and len(reel.children) == 2


def strip_states_from_the_journal(rig: Rig) -> None:
    path = next(rig.journal.directory.glob("journal_*.jsonl"))
    lines = []
    for line in path.read_text().splitlines():
        record = json.loads(line)
        for item in ((record.get("created") or {}).get("copies")) or []:
            item.pop("state", None)
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")


def test_revert_of_a_copy_recorded_without_a_fingerprint_is_a_conflict(rig: Rig) -> None:
    env = copy(rig, [CLIP1], REEL1)
    strip_states_from_the_journal(rig)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "no fingerprint" in out["error"]
    assert entry_names(rig.handles["reel_group"].reels[0]) == ["c1", "clip1"]


def test_the_outcome_of_a_copy_records_name_duration_and_fingerprint_of_each_copy(
    rig: Rig,
) -> None:
    copy(rig, [CLIP1], REEL1)
    [copied] = rig.records()[-1]["created"]["copies"]
    assert copied["state"]["name"] == "clip1"
    assert copied["state"]["duration"] == 100
    assert len(copied["state"]["fingerprint"]) == 64


def test_revert_of_a_copy_that_cannot_delete_one_is_partial_and_can_be_retried(rig: Rig) -> None:
    env = copy(rig, [CLIP1, CLIP2], REEL1)
    reel = rig.handles["reel_group"].reels[0]
    real = rig.flame.delete
    stuck = reel.children[2]

    def flaky(obj: Any, confirm: bool = True) -> bool:
        return True if obj is stuck else bool(real(obj, confirm))

    rig.flame.delete = flaky
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "SILENT_NOOP"
    assert [d["name"] for d in out["result"]["deleted"]] == ["clip1"]
    assert entry_names(reel) == ["c1", "clip2"]
    rig.flame.delete = real
    retry = revert(rig, env)
    assert retry["ok"] is True
    assert [s["name"] for s in retry["result"]["skipped"]] == ["clip1"]
    assert entry_names(reel) == ["c1"]


def test_a_lost_answer_of_a_copy_is_decided_by_check_and_revert_then_deletes_the_copies(
    rig: Rig,
) -> None:
    rig.exec.fault("desktop.copy", "TIMEOUT", when="after", skip=1)
    env = copy(rig, [CLIP1, CLIP2], REEL1)
    assert env["errorCode"] == "TIMEOUT"
    group = env["journal"]["group"]
    checked = rig.do("journal.check", {"group": group})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "landed"
    assert [c["name"] for c in row["created"]["copies"]] == ["clip1", "clip2"]
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True and len(out["result"]["deleted"]) == 2
    assert entry_names(rig.handles["reel_group"].reels[0]) == ["c1"]


def test_check_of_a_copy_that_never_ran_is_not_landed(rig: Rig) -> None:
    rig.exec.fault("desktop.copy", "BRIDGE_LOST", when="before", skip=1)
    env = copy(rig, [CLIP1], REEL1)
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "not_landed"


def test_a_create_that_returns_an_existing_group_is_a_silent_noop_with_nothing_to_delete(
    rig: Rig,
) -> None:
    existing = desktop(rig).reel_groups[0]
    desktop(rig).create_reel_group = lambda name: existing
    env = create(rig, kind="reel_group", name="X")
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "not a new entry" in env["error"] and env["journal"]["revert"] is None
    assert rig.records()[-1]["created"] is None
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "PRECONDITION_UNMET"
    assert group_names(rig) == ["RG"]  # the group that was there is not touched


def test_a_create_that_returns_the_wrong_type_is_a_silent_noop(rig: Rig) -> None:
    desktop(rig).create_reel_group = lambda name: rig.handles["reel"]
    env = create(rig, kind="reel_group", name="X")
    assert env["errorCode"] == "SILENT_NOOP" and "not a PyReelGroup" in env["error"]


def test_a_read_back_that_fails_after_the_create_still_records_what_was_made(rig: Rig) -> None:
    class Boom(PyReelGroup):
        armed = False

        @property
        def reels(self) -> list[Any]:
            if self.armed:
                raise RuntimeError("boom")
            return list(self.__dict__["_r"])

        @reels.setter
        def reels(self, value: list[Any]) -> None:
            self.__dict__["_r"] = value

    Boom.__name__ = "PyReelGroup"  # the ops look at the type NAME, like Flame's classes

    def make(name: str) -> Boom:
        group = Boom(name, f"/wt/desktop/{name}", uid="boom-uid")
        group.add_default_reels()
        desktop(rig).reel_groups.append(group)
        desktop(rig).sync_children()
        group.armed = True
        return group

    desktop(rig).create_reel_group = make
    env = create(rig, kind="reel_group", name="X")
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env["error"]
    assert "reading it back failed" in env["error"]
    assert env["journal"]["revert"] is not None
    assert rig.records()[-1]["created"]["uid"] == "boom-uid"
    made = next(g for g in desktop(rig).reel_groups if g.uid.get_value() == "boom-uid")
    made.armed = False
    assert revert(rig, env)["ok"] is True and group_names(rig) == ["RG"]


def test_a_copy_that_returns_nothing_is_a_silent_noop_and_journals_no_copies(rig: Rig) -> None:
    rig.flame.media_panel.copy = lambda *args, **kwargs: []
    env = copy(rig, [CLIP1], REEL1)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "0 clip(s) for 1 source(s)" in env["error"] and env["journal"]["revert"] is None
    assert rig.records()[-1]["created"] is None


def test_a_copy_that_returns_a_source_is_not_recorded_as_a_copy_so_a_revert_cannot_delete_it(
    rig: Rig,
) -> None:
    source = rig.handles["clip1"]
    rig.flame.media_panel.copy = lambda *args, **kwargs: [source]
    env = copy(rig, [CLIP1], REEL1)
    assert env["errorCode"] == "SILENT_NOOP" and "not new" in env["error"]
    assert env["result"]["notNew"][0]["uid"] == source.uid.get_value()
    assert rig.records()[-1]["created"] is None and env["journal"]["revert"] is None
    assert rig.flame.find_by_uid(source.uid.get_value()) is source


def test_a_copy_that_does_not_land_in_the_reel_is_an_error_but_its_copies_are_recorded(
    rig: Rig,
) -> None:
    stray = PyClip("stray", wiretap_id="/wt/stray", uid="stray-uid")
    rig.flame.media_panel.copy = lambda *args, **kwargs: [stray]
    env = copy(rig, [CLIP1], REEL1)
    assert env["errorCode"] == "SILENT_NOOP" and "does not list every new clip" in env["error"]
    assert [c["uid"] for c in rig.records()[-1]["created"]["copies"]] == ["stray-uid"]
    out = revert(rig, env)  # not found: skipped, nothing deleted
    assert out["ok"] is True and [s["uid"] for s in out["result"]["skipped"]] == ["stray-uid"]


def test_a_copy_that_takes_a_source_away_says_so_in_sources_remain_and_degraded(rig: Rig) -> None:
    source = rig.handles["clip1"]
    real = rig.flame.media_panel.copy

    def moving(src: Any, dest: Any, duplicate_action: str = "add") -> list[Any]:
        copies: list[Any] = real(src, dest, duplicate_action=duplicate_action)
        rig.flame.delete(source, confirm=False)
        return copies

    rig.flame.media_panel.copy = moving
    env = copy(rig, [CLIP1], REEL1)
    assert env["ok"] is True, env
    assert env["result"]["sourcesRemain"] is False
    assert any("source is not found" in note for note in env["degraded"])


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("desktop.create", {"kind": "reel_group", "name": "P"}),
        ("desktop.copy", {"sources": [CLIP1], "destination": REEL1}),
    ],
)
def test_plan_run_refuses_both_ops(rig: Rig, op: str, args: dict[str, Any]) -> None:
    env = rig.do("plan.run", {"steps": [{"op": op, "args": args}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert f"{op} cannot be a plan step" in env["result"]["issues"][0]["problems"][0]
    assert env["result"]["ran"] is False and rig.exec.calls == [] and group_names(rig) == ["RG"]


def test_readonly_policy_denies_both_ops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    assert create(rig, kind="reel_group", name="P")["errorCode"] == "POLICY_DENIED"
    assert copy(rig, [CLIP1], REEL1)["errorCode"] == "POLICY_DENIED"
    assert rig.exec.calls == []


def test_declarations_match_what_the_ops_do() -> None:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    for name in ("desktop.create", "desktop.copy"):
        op = registry.get(name)
        assert op is not None
        assert (op.effect, op.thread, op.undo) == ("create", "main", "value")
        assert op.undo_steps({}, 3) == 0 and op.wrap is desktop_run.run_desktop_write
        extra = {"batch", "desktop"} if name == "desktop.copy" else set()
        assert set(op.accepts) == {"wiretap", "path", "selection"} | extra
        for word in ("journal.revert", "plan.run", "Native Ctrl+Z"):
            assert word in op.description, (name, word)
    create_op = registry.get("desktop.create")
    assert create_op is not None
    for word in ("current Batch group", "allow_rename", "CONFLICT"):
        assert word in create_op.description


PHASE_ARGS: list[dict[str, Any]] = [
    {"kind": "batch_group", "name": "n"},
    {"kind": "batch_group", "name": "n", "reels": ["a"], "shelf_reels": ["s"], "duration": 3},
    {"kind": "reel_group", "name": "n", "allow_rename": True},
    {"kind": "reel", "name": "n", "parent": RG, "sequence": True},
    {"kind": "batch_group", "name": "n", "_phase": "read"},
    {"kind": "reel", "name": "n", "parent": LIB, "_phase": "read"},
    {"kind": "batch_group", "name": "n", "_phase": "write", "_expected": ["u1"]},
    {"kind": "reel", "name": "n", "_phase": "revert", "_created": {"uid": "u", "parent": None}},
    {"kind": "batch_group", "name": "n", "_phase": "alive", "_created": {"items": [{"uid": "u"}]}},
]
COPY_PHASE_ARGS: list[dict[str, Any]] = [
    {"sources": [CLIP1], "destination": REEL1},
    {"sources": [CLIP1, CLIP2], "destination": REEL1, "_phase": "read"},
    {"sources": [], "destination": REEL1, "_phase": "read"},
    {"sources": [CLIP1], "destination": REEL1, "_phase": "write", "_expected": ["u"]},
    {"sources": [], "destination": REEL1, "_phase": "revert", "_created": {"copies": []}},
    {"sources": [], "destination": REEL1, "_phase": "alive", "_created": {"items": []}},
]


@pytest.mark.parametrize(
    ("op_name", "args"),
    [("desktop.create", a) for a in PHASE_ARGS] + [("desktop.copy", a) for a in COPY_PHASE_ARGS],
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


def test_the_direct_generated_code_runs_the_read_phase_without_writing(rig: Rig) -> None:
    registry = rig.registry
    result = run_op(
        registry,
        "desktop.create",
        {"kind": "batch_group", "name": "R", "_phase": "read"},
        rig.flame,
    )
    assert result["opError"] is None
    value = result["value"]
    assert value["current"]["name"] == "BG2" and value["parent"] is None
    assert [row["name"] for row in value["existing"]] == ["BG1", "BG2"]
    assert batch_names(rig) == ["BG1", "BG2"]


def test_index_treats_only_landed_desktop_intents_with_created_as_revertible() -> None:
    records: list[dict[str, Any]] = [
        {
            "type": "intent",
            "id": "i1",
            "group": "g1",
            "kind": "desktop_create",
            "op": "desktop.create",
        },
        {"type": "outcome", "id": "i1", "group": "g1", "ok": False, "errorCode": "CONFLICT"},
    ]
    view = Index(records).intents["i1"]
    assert view.is_revertible() is False and view.created_info() is None
    records[1] = {**records[1], "ok": True, "created": {"uid": "u"}}
    assert Index(records).intents["i1"].is_revertible() is True


def test_the_prelude_only_ever_asks_flame_for_add() -> None:
    assert "duplicate_action='add'" in DESKTOP_HELPER
    assert "'replace'" not in DESKTOP_HELPER and "'skip'" not in DESKTOP_HELPER
