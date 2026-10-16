"""The `journal` category (list / show / check / revert): run through the real Runtime with a
scripted `transport.exec` over a fake Flame tree (journal_test_helpers.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp.journal import new_id, now_iso
from flame_mcp.policy import Policy, deny_operation
from flame_mcp.runtime import TestHooks
from journal_test_helpers import BG1, COMP, REEL_WIRETAP, Rig, make_rig


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def set_values(rig: Rig, values: dict[str, Any]) -> dict[str, Any]:
    return rig.do("node.set_attributes", {"target": COMP, "values": values})


def comp_values(rig: Rig) -> dict[str, Any]:
    comp = rig.handles["comp"]
    return {"blend_type": comp.blend_type.get_value(), "pos_x": comp.pos_x.get_value()}


def open_intent(rig: Rig, *, landed: bool, values: dict[str, Any] | None = None) -> str:
    """A write whose answer was lost (`landed`: whether it had taken effect). Returns its group."""
    rig.exec.fault("node.set_attributes", "BRIDGE_LOST", when="after" if landed else "before")
    env = set_values(rig, values or {"blend_type": "Gimp"})
    assert env["errorCode"] == "BRIDGE_LOST"
    group = env["journal"]["group"]
    assert isinstance(group, str)
    return group


def test_list_is_newest_first_with_ops_time_and_state(rig: Rig) -> None:
    first = set_values(rig, {"blend_type": "Gimp"})
    second = rig.do("desktop.rename", {"target": REEL_WIRETAP, "name": "R1b"})
    result = rig.do("journal.list")["result"]
    assert [i["group"] for i in result["items"]] == [
        second["journal"]["group"],
        first["journal"]["group"],
    ]
    newest = result["items"][0]
    assert newest["ops"] == ["desktop.rename"] and newest["state"] == "landed"
    assert newest["intents"] == 1 and newest["reverts"] is None and newest["revertedBy"] == []
    assert isinstance(newest["ts"], str)
    assert (result["total"], result["offset"], result["shown"], result["truncated"]) == (
        2,
        0,
        2,
        False,
    )


def test_list_pages(rig: Rig) -> None:
    groups = [set_values(rig, {"pos_x": float(i)})["journal"]["group"] for i in range(3)]
    page = rig.do("journal.list", {"limit": 1, "offset": 1})["result"]
    assert [i["group"] for i in page["items"]] == [groups[1]]
    assert page["total"] == 3 and page["truncated"] is True
    assert rig.do("journal.list", {"offset": 3})["result"]["items"] == []


def test_list_of_an_empty_journal(rig: Rig) -> None:
    result = rig.do("journal.list")["result"]
    assert result["total"] == 0 and result["items"] == []


def test_show_returns_every_record_of_the_group_and_its_state(rig: Rig) -> None:
    env = set_values(rig, {"blend_type": "Gimp"})
    other = set_values(rig, {"pos_x": 2.0})
    shown = rig.do("journal.show", {"group": env["journal"]["group"]})["result"]
    assert [r["type"] for r in shown["records"]] == ["intent", "outcome"]
    assert {r["group"] for r in shown["records"]} == {env["journal"]["group"]}
    assert shown["state"] == "landed"
    assert other["journal"]["group"] != shown["group"]


def test_show_and_check_and_revert_refuse_an_unknown_group(rig: Rig) -> None:
    for op in ("journal.show", "journal.check", "journal.revert"):
        env = rig.do(op, {"group": "g_nope"})
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", op


def test_the_journal_ops_are_server_side_and_read_only_policy_hides_only_revert(rig: Rig) -> None:
    for name in ("journal.list", "journal.show", "journal.check", "journal.revert"):
        op = rig.registry.get(name)
        assert op is not None and op.thread == "server" and op.to_python is None
    readonly = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    denied = {
        n: deny_operation(rig.registry.get(n), readonly) is not None  # type: ignore[arg-type]
        for n in ("journal.list", "journal.show", "journal.check", "journal.revert")
    }
    assert denied == {
        "journal.list": False,
        "journal.show": False,
        "journal.check": False,
        "journal.revert": True,
    }


def test_check_finds_landed_when_the_new_value_is_there(rig: Rig) -> None:
    group = open_intent(rig, landed=True)
    found = rig.do("journal.check", {"group": group})["result"]
    entry = found["checked"][0]
    assert entry["verdict"] == "landed" and entry["perKey"] == {"blend_type": "after"}
    assert entry["current"] == {"blend_type": "Gimp"}
    assert rig.do("journal.show", {"group": group})["result"]["state"] == "landed"


def test_check_finds_not_landed_when_the_old_value_is_still_there(rig: Rig) -> None:
    group = open_intent(rig, landed=False)
    entry = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    assert entry["verdict"] == "not_landed" and entry["perKey"] == {"blend_type": "before"}
    assert rig.do("journal.show", {"group": group})["result"]["state"] == "not_landed"


def test_check_finds_diverged_when_neither_value_is_there(rig: Rig) -> None:
    group = open_intent(rig, landed=False)
    rig.handles["comp"].blend_type._value = "Photoshop"  # somebody else
    entry = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    assert entry["verdict"] == "diverged" and entry["perKey"] == {"blend_type": "other"}
    assert rig.do("journal.show", {"group": group})["result"]["state"] == "diverged"


def test_check_finds_diverged_when_only_some_keys_landed(rig: Rig) -> None:
    group = open_intent(rig, landed=False, values={"blend_type": "Gimp", "pos_x": 9.0})
    rig.handles["comp"].pos_x._value = 9.0  # one key of two took effect
    entry = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    assert entry["verdict"] == "diverged"
    assert entry["perKey"] == {"blend_type": "before", "pos_x": "after"}


def test_check_finds_diverged_when_the_target_is_gone(rig: Rig) -> None:
    group = open_intent(rig, landed=False)
    rig.handles["bg1"].nodes.remove(rig.handles["comp"])
    entry = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    assert entry["verdict"] == "diverged" and "not found" in entry["detail"]


def test_check_of_a_renamed_node_looks_under_the_new_name(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "BRIDGE_LOST", when="after")
    env = rig.do("node.set_attributes", {"target": COMP, "values": {"name": "comp_new"}})
    assert env["errorCode"] == "BRIDGE_LOST"
    assert rig.handles["comp"].name.get_value() == "comp_new"  # landed: the old address is gone
    entry = rig.do("journal.check", {"group": env["journal"]["group"]})["result"]["checked"][0]
    assert entry["verdict"] == "landed"


def test_check_of_a_rename_uses_the_desktop_read_op_recorded_in_the_intent(rig: Rig) -> None:
    rig.exec.fault("desktop.rename", "BRIDGE_LOST", when="after")
    env = rig.do("desktop.rename", {"target": REEL_WIRETAP, "name": "R1b"})
    assert env["errorCode"] == "BRIDGE_LOST"
    entry = rig.do("journal.check", {"group": env["journal"]["group"]})["result"]["checked"][0]
    assert entry["verdict"] == "landed" and entry["current"] == {"name": "R1b"}
    assert rig.exec.labels()[-1] == "desktop.get_attributes"


def test_check_writes_nothing_to_the_project(rig: Rig) -> None:
    open_intent(rig, landed=False)
    before = comp_values(rig)
    writes_before = rig.write_labels().count("node.set_attributes")
    rig.do("journal.check")
    assert comp_values(rig) == before
    assert (
        rig.handles["comp"].blend_type.set_calls == [] and rig.handles["comp"].pos_x.set_calls == []
    )
    assert rig.write_labels().count("node.set_attributes") == writes_before  # no write op sent
    assert set(rig.exec.labels()) <= {"node.get_attributes", "node.set_attributes"}


def test_check_without_a_group_covers_every_undecided_intent_and_skips_decided_ones(
    rig: Rig,
) -> None:
    decided = set_values(rig, {"pos_x": 1.0})
    open_intent(rig, landed=True)
    found = rig.do("journal.check")["result"]
    assert found["inDoubt"] == 1 and len(found["checked"]) == 1
    assert found["checked"][0]["group"] != decided["journal"]["group"]
    assert rig.do("journal.check")["result"]["inDoubt"] == 0  # a finding decides it


def test_check_of_a_decided_group_has_nothing_to_do(rig: Rig) -> None:
    env = set_values(rig, {"pos_x": 1.0})
    found = rig.do("journal.check", {"group": env["journal"]["group"]})["result"]
    assert found["inDoubt"] == 0 and found["checked"] == []


def test_check_appends_its_finding_as_a_check_record(rig: Rig) -> None:
    group = open_intent(rig, landed=True)
    rig.do("journal.check", {"group": group})
    record = rig.records()[-1]
    assert record["type"] == "check" and record["verdict"] == "landed" and record["group"] == group
    assert record["perKey"] == {"blend_type": "after"} and record["current"] == {
        "blend_type": "Gimp"
    }


def test_check_reads_an_intent_left_by_a_dead_process(rig: Rig) -> None:
    """An intent with no outcome and no `unresolved` note at all: the server died mid-write."""
    intent_id = new_id("i")
    rig.journal.append(
        {
            "type": "intent",
            "id": intent_id,
            "group": "g_dead",
            "ts": now_iso(),
            "op": "node.set_attributes",
            "args": {},
            "target": {
                "kind": "node",
                "batch": {"kind": "batch", "uid": "uid-b1"},
                "name": "comp1",
            },
            "targetAfter": None,
            "readOp": "node.get_attributes",
            "before": {"blend_type": "Flame"},
            "after": {"blend_type": "Gimp"},
        }
    )
    found = rig.do("journal.check")["result"]
    assert found["checked"][0]["verdict"] == "not_landed"


def test_revert_puts_the_previous_values_back_and_records_a_new_group(rig: Rig) -> None:
    env = set_values(rig, {"blend_type": "Gimp", "pos_x": 5.0})
    group = env["journal"]["group"]
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert comp_values(rig) == {"blend_type": "Flame", "pos_x": 0.0}
    result = back["result"]
    assert result["reverted"] == group and result["group"] != group
    assert result["writes"][0]["ok"] is True

    listing = {i["group"]: i for i in rig.do("journal.list")["result"]["items"]}
    assert listing[group]["state"] == "reverted" and listing[group]["revertedBy"] == [
        result["group"]
    ]
    assert listing[result["group"]]["state"] == "landed"
    assert listing[result["group"]]["reverts"] == group
    intent = next(
        r for r in rig.records() if r["group"] == result["group"] and r["type"] == "intent"
    )
    assert intent["reverts"] == group
    assert intent["before"] == {"blend_type": "Gimp", "pos_x": 5.0}  # what it replaced


def test_revert_reports_affected_keys_and_no_native_undo(rig: Rig) -> None:
    env = set_values(rig, {"blend_type": "Gimp", "pos_x": 5.0})
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["affected"] == 2 and back["undoSteps"] == 0


def test_revert_restores_a_rename(rig: Rig) -> None:
    env = rig.do("desktop.rename", {"target": REEL_WIRETAP, "name": "R1b"})
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert rig.handles["tree_reel"].name.get_value() == "R1"


def test_revert_of_a_renamed_node_finds_it_under_its_new_name(rig: Rig) -> None:
    env = rig.do("node.set_attributes", {"target": COMP, "values": {"name": "comp_new"}})
    assert env["ok"] is True
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert rig.handles["comp"].name.get_value() == "comp1"


def test_revert_refuses_when_a_value_is_not_what_the_group_left(rig: Rig) -> None:
    env = set_values(rig, {"blend_type": "Gimp", "pos_x": 5.0})
    rig.handles["comp"].pos_x._value = 99.0  # changed since
    rig.exec.calls.clear()
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is False and back["errorCode"] == "CONFLICT"
    assert comp_values(rig) == {
        "blend_type": "Gimp",
        "pos_x": 99.0,
    }  # nothing touched, even the key that matched
    assert rig.handles["comp"].blend_type.set_calls == ["Gimp"]  # only the original write
    refused = back["result"]["intents"][0]["keys"]
    assert refused["pos_x"] == {"expected": 5.0, "current": 99.0, "match": False}
    assert refused["blend_type"]["match"] is True
    assert "pos_x" in back["error"]
    # refused by the check made BEFORE the write path: one read, no write path, no new record
    assert rig.exec.labels() == ["node.get_attributes"]
    assert [r["type"] for r in rig.records()] == ["intent", "outcome"]
    listing = rig.do("journal.list")["result"]["items"]
    assert [i["state"] for i in listing] == ["landed"]  # not reverted


def test_revert_twice_is_refused_and_writes_nothing(rig: Rig) -> None:
    env = set_values(rig, {"blend_type": "Gimp"})
    group = env["journal"]["group"]
    assert rig.do("journal.revert", {"group": group})["ok"] is True
    set_calls = list(rig.handles["comp"].blend_type.set_calls)
    again = rig.do("journal.revert", {"group": group})
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"
    assert "already reverted" in again["error"]
    assert rig.handles["comp"].blend_type.set_calls == set_calls


def test_a_revert_can_itself_be_reverted(rig: Rig) -> None:
    group = set_values(rig, {"blend_type": "Gimp"})["journal"]["group"]
    revert_group = rig.do("journal.revert", {"group": group})["result"]["group"]
    redo = rig.do("journal.revert", {"group": revert_group})
    assert redo["ok"] is True, redo
    assert comp_values(rig)["blend_type"] == "Gimp"
    states = {i["group"]: i["state"] for i in rig.do("journal.list")["result"]["items"]}
    assert states[group] == "landed" and states[revert_group] == "reverted"


def test_revert_refuses_a_group_that_applied_nothing(rig: Rig) -> None:
    env = set_values(rig, {"blend_type": "NoSuchMode"})
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["errorCode"] == "PRECONDITION_UNMET" and "applied no key" in back["error"]


def test_revert_of_an_undecided_group_asks_for_a_check_first(rig: Rig) -> None:
    group = open_intent(rig, landed=True)
    rig.runtime.guard.release(rig.records()[0]["id"])  # door open again (e.g. a new process)
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "PRECONDITION_UNMET" and "journal.check" in back["error"]
    rig.do("journal.check", {"group": group})
    done = rig.do("journal.revert", {"group": group})
    assert done["ok"] is True, done
    assert comp_values(rig)["blend_type"] == "Flame"


def test_revert_after_a_check_that_found_not_landed_has_nothing_to_do(rig: Rig) -> None:
    group = open_intent(rig, landed=False)
    rig.do("journal.check", {"group": group})
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "PRECONDITION_UNMET"


def test_revert_of_a_partial_group_puts_back_only_the_applied_keys(rig: Rig) -> None:
    # pos_x lands, blend_type does not (silent no-op): the outcome says applied = [pos_x]
    comp = rig.handles["comp"]

    class Ignoring(type(comp.blend_type)):  # type: ignore[misc]
        def set_value(self, value: Any) -> None:
            self.set_calls.append(value)

    ignoring = Ignoring("Flame", ("Flame", "Photoshop", "Gimp"))
    ignoring.__class__.__name__ = "PyAttribute"
    comp.blend_type = ignoring
    env = set_values(rig, {"pos_x": 5.0, "blend_type": "Gimp"})
    assert env["errorCode"] == "SILENT_NOOP" and env["partial"] == {"applied": 1, "failed": 1}
    listing = rig.do("journal.list")["result"]["items"][0]
    assert listing["state"] == "partial"
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert comp.pos_x.get_value() == 0.0
    assert ignoring.set_calls == ["Gimp"]  # blend_type was never written back


def test_revert_loses_a_race_cleanly_at_the_compare_and_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    group = set_values(rig, {"blend_type": "Gimp"})["journal"]["group"]

    def sleeper(_seconds: float) -> None:  # the revert's own write is paused; somebody changes it
        rig.handles["comp"].blend_type._value = "Photoshop"

    rig.runtime.hooks = TestHooks(pause_after_preread_s=1.0, sleep=sleeper)
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is False and back["errorCode"] == "CONFLICT"
    assert rig.handles["comp"].blend_type.get_value() == "Photoshop"
    assert rig.handles["comp"].blend_type.set_calls == ["Gimp"]  # the revert wrote nothing
    states = {i["group"]: i["state"] for i in rig.do("journal.list")["result"]["items"]}
    assert states[group] == "landed"  # not reverted
    assert "conflict" in states.values()  # the failed revert is on record


def test_revert_rechecks_the_values_when_the_write_path_reads_them_again(rig: Rig) -> None:
    """The first read (the plan) matched; somebody changes the value before the write path's
    own read. That second read is compared with the same expected values, so nothing is written
    and nothing is journaled -- without it the revert would overwrite the newer value."""
    group = set_values(rig, {"blend_type": "Gimp"})["journal"]["group"]
    reads: list[int] = []

    def change_before_second_read(label: str | None) -> None:
        if label == "node.get_attributes":
            reads.append(1)
            if len(reads) == 2:
                rig.handles["comp"].blend_type._value = "Photoshop"

    rig.exec.before_run = change_before_second_read
    records_before = len(rig.records())
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is False and back["errorCode"] == "CONFLICT"
    assert rig.handles["comp"].blend_type.get_value() == "Photoshop"
    assert rig.handles["comp"].blend_type.set_calls == ["Gimp"]  # only the original write
    assert len(rig.records()) == records_before  # the refused revert left no intent behind
    assert rig.write_labels().count("node.set_attributes") == 1


def test_revert_is_refused_while_the_door_is_closed(rig: Rig) -> None:
    landed = set_values(rig, {"pos_x": 1.0})["journal"]["group"]
    open_intent(rig, landed=False)
    back = rig.do("journal.revert", {"group": landed})
    assert back["ok"] is False and back["errorCode"] == "POLICY_DENIED"


def test_revert_of_a_target_that_is_gone_says_so_and_writes_nothing(rig: Rig) -> None:
    group = set_values(rig, {"blend_type": "Gimp"})["journal"]["group"]
    rig.handles["bg1"].nodes.remove(rig.handles["comp"])
    back = rig.do("journal.revert", {"group": group})
    assert back["errorCode"] == "SELECTOR_EMPTY"
    assert BG1  # the batch itself is still there; only the node is gone


def test_revert_when_flame_cannot_be_read_writes_nothing(rig: Rig) -> None:
    group = set_values(rig, {"blend_type": "Gimp"})["journal"]["group"]
    rig.exec.fault("node.get_attributes", "BRIDGE_UNAVAILABLE")
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is False and back["errorCode"] == "BRIDGE_UNAVAILABLE"
    assert rig.handles["comp"].blend_type.get_value() == "Gimp"
