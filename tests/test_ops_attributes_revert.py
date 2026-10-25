"""A write that changed a value but not to the requested one (a Colour Source that turns bit
depth 10 into 16, a scalar that Flame rounds) is put back by `journal.revert`.

The outcome record lists the keys whose value changed as `changedKeys`, besides `applied` (the keys
that read back as asked); the revert puts `changedKeys` back and compares each with the value Flame
read back after the write. Run through the real Runtime over the fake Flame tree
(journal_test_helpers.py); the resolution doubles are in test_ops_attributes.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyResolution
from journal_test_helpers import BG1, COMP, Rig, make_rig
from test_ops_attributes import (
    RES_START,
    ClampAttr,
    NoopAttr,
    add_attr,
    add_resolution_nodes,
    res_node,
    res_of,
)

PLAN = "plan.run"
SET = "node.set_attributes"
BIT10 = {"width": 800, "height": 1080, "bit_depth": 10}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_rig(monkeypatch, tmp_path)
    add_resolution_nodes(made.handles)
    return made


def write(rig: Rig, name: str, resolution: dict[str, Any]) -> dict[str, Any]:
    return rig.do(SET, {"target": res_node(name), "values": {"resolution": resolution}})


def outcome_of(rig: Rig, group: str) -> dict[str, Any]:
    return next(r for r in rig.records() if r["type"] == "outcome" and r["group"] == group)


def states(rig: Rig) -> dict[str, str]:
    return {i["group"]: i["state"] for i in rig.do("journal.list")["result"]["items"]}


def test_the_outcome_of_a_partial_write_lists_the_changed_key_apart_from_the_applied_ones(
    rig: Rig,
) -> None:
    env = write(rig, "res_colour", BIT10)
    assert env["errorCode"] == "FLAME_API_ERROR"
    outcome = outcome_of(rig, env["journal"]["group"])
    assert outcome["applied"] == [] and outcome["changedKeys"] == ["resolution"]
    assert outcome["failed"]["resolution"].startswith("partial:")
    assert outcome["readBack"]["resolution"] == {**RES_START, "width": 800}
    assert states(rig)[env["journal"]["group"]] == "partial"


def test_a_clean_write_records_the_changed_key_too(rig: Rig) -> None:
    env = write(rig, "res_full", {"width": 800})
    outcome = outcome_of(rig, env["journal"]["group"])
    assert outcome["applied"] == ["resolution"] and outcome["changedKeys"] == ["resolution"]


def test_revert_after_a_partial_write_puts_width_and_height_back(rig: Rig) -> None:
    env = write(rig, "res_colour", BIT10)
    group = env["journal"]["group"]
    assert res_of(rig.handles, "res_colour") == (800, 1080, 16, 1.7778, "P")
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert res_of(rig.handles, "res_colour") == (1920, 1080, 16, 1.7778, "P")
    assert back["result"]["revertedKeys"][0]["keys"] == ["resolution"]
    assert states(rig)[group] == "reverted"
    again = rig.do("journal.revert", {"group": group})
    assert again["errorCode"] == "PRECONDITION_UNMET" and "already reverted" in again["error"]


def test_the_revert_compares_with_the_read_back_not_with_the_value_that_was_asked_for(
    rig: Rig,
) -> None:
    env = write(rig, "res_colour", BIT10)
    intent = next(r for r in rig.records() if r["type"] == "intent")
    assert intent["after"]["resolution"]["bit_depth"] == 10  # what was asked for
    assert rig.handles["res_colour"].resolution.get_value().bit_depth == 16  # what is there
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back


def test_revert_of_a_partial_write_is_refused_when_somebody_changed_the_resolution_since(
    rig: Rig,
) -> None:
    env = write(rig, "res_colour", BIT10)
    rig.handles["res_colour"].resolution._value = PyResolution(640, 360, 16)
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is False and back["errorCode"] == "CONFLICT"
    assert res_of(rig.handles, "res_colour") == (640, 360, 16, 1.7778, "P")
    refused = back["result"]["intents"][0]["keys"]["resolution"]
    assert refused["match"] is False and refused["current"]["width"] == 640
    assert len(rig.handles["res_colour"].resolution.set_calls) == 1  # only the original write


def test_the_compare_and_swap_of_a_resolution_write_sees_a_change_between_read_and_write(
    rig: Rig,
) -> None:
    def change(label: str | None) -> None:
        if label == SET:
            rig.handles["res_full"].resolution._value = PyResolution(1280, 720, 16)

    rig.exec.before_run = change
    env = write(rig, "res_full", {"width": 800})
    assert env["errorCode"] == "CONFLICT"
    assert res_of(rig.handles, "res_full") == (1280, 720, 16, 1.7778, "P")
    assert rig.handles["res_full"].resolution.set_calls == []


def test_revert_of_a_resolution_that_took_everything(rig: Rig) -> None:
    env = write(rig, "res_full", {"width": 800, "height": 600, "bit_depth": 8})
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert res_of(rig.handles, "res_full") == (1920, 1080, 16, 1.7778, "P")


def test_a_resolution_that_changed_nothing_has_nothing_to_revert(rig: Rig) -> None:
    env = write(rig, "res_frozen", {"width": 800})
    assert env["errorCode"] == "SILENT_NOOP"
    outcome = outcome_of(rig, env["journal"]["group"])
    assert outcome["applied"] == [] and outcome["changedKeys"] == []
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is False and back["errorCode"] == "PRECONDITION_UNMET"
    assert states(rig)[env["journal"]["group"]] == "failed"


def test_a_scalar_flame_rounded_to_another_value_is_put_back_by_the_revert(rig: Rig) -> None:
    add_attr(rig.handles["comp"], "gain", ClampAttr(1.0, None, ndigits=1))
    env = rig.do(SET, {"target": COMP, "values": {"gain": 2.26}})
    assert env["errorCode"] == "FLAME_API_ERROR"
    outcome = outcome_of(rig, env["journal"]["group"])
    assert outcome["applied"] == [] and outcome["changedKeys"] == ["gain"]
    assert outcome["failed"]["gain"].startswith("partial:")
    assert outcome["readBack"] == {"gain": 2.3}
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert rig.handles["comp"].gain.get_value() == 1.0


def test_a_scalar_that_is_still_what_it_was_has_no_changed_key(rig: Rig) -> None:
    add_attr(rig.handles["comp"], "dynamic_name", NoopAttr("x"))
    env = rig.do(SET, {"target": COMP, "values": {"dynamic_name": "y"}})
    assert outcome_of(rig, env["journal"]["group"])["changedKeys"] == []


def test_revert_puts_back_the_applied_and_the_rounded_key_of_one_write(rig: Rig) -> None:
    add_attr(rig.handles["comp"], "gain", ClampAttr(1.0, None, ndigits=1))
    env = rig.do(SET, {"target": COMP, "values": {"blend_type": "Gimp", "gain": 2.26}})
    outcome = outcome_of(rig, env["journal"]["group"])
    assert outcome["applied"] == ["blend_type"] and outcome["changedKeys"] == ["blend_type", "gain"]
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert rig.handles["comp"].blend_type.get_value() == "Flame"
    assert rig.handles["comp"].gain.get_value() == 1.0


def set_step(name: str, resolution: dict[str, Any]) -> dict[str, Any]:
    return {"op": SET, "args": {"target": res_node(name), "values": {"resolution": resolution}}}


def test_a_plan_that_stops_at_a_partial_resolution_step_reverts_through_the_values(
    rig: Rig,
) -> None:
    steps = [
        set_step("res_colour", {"width": 1280, "height": 720}),
        set_step("res_colour", BIT10),
        set_step("res_full", {"width": 640}),
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["stoppedAt"] == 1
    assert res_of(rig.handles, "res_colour") == (800, 1080, 16, 1.7778, "P")
    assert res_of(rig.handles, "res_full") == (1920, 1080, 16, 1.7778, "P")  # never reached
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert res_of(rig.handles, "res_colour") == (1920, 1080, 16, 1.7778, "P")


def test_a_bundled_plan_with_a_partial_resolution_step_stops_there_and_reverts_by_restore(
    rig: Rig,
) -> None:
    """The fake's saved setup holds scalar attributes only, so a resolution is not put back by its
    restore here; what is checked is the plan's stop, its error and the restore of the group."""
    steps = [
        {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "b1"}},
        set_step("res_colour", BIT10),
        set_step("res_full", {"width": 640}),
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["stoppedAt"] == 1
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 1}
    assert res_of(rig.handles, "res_full") == (1920, 1080, 16, 1.7778, "P")  # never reached
    assert "b1" in [str(n.name.get_value()) for n in rig.handles["bg1"].nodes]
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert "b1" not in [str(n.name.get_value()) for n in rig.handles["bg1"].nodes]


def test_an_unanswered_resolution_write_that_landed_is_found_by_the_check_and_can_be_reverted(
    rig: Rig,
) -> None:
    rig.exec.fault(SET, "BRIDGE_LOST", when="after")
    env = write(rig, "res_full", {"width": 800})
    assert env["errorCode"] == "BRIDGE_LOST"
    group = env["journal"]["group"]
    intent = next(r for r in rig.records() if r["type"] == "intent")
    assert intent["after"]["resolution"] == {**RES_START, "width": 800}  # the whole object
    entry = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    assert entry["verdict"] == "landed", entry
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert res_of(rig.handles, "res_full") == (1920, 1080, 16, 1.7778, "P")
