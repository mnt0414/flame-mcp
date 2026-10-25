"""`segment.remove_connection` that ends without an answer shuts the write guard.

The call has no revert, but each step it writes is an intent in the journal (undo unsupported), so
a TIMEOUT / BRIDGE_LOST leaves an open intent, `WriteGuard` refuses later writes, and
`journal.check` reads `segment.connections` again and decides landed / not_landed (or diverged).
Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fake_selection_connections as fsc
from fake_media_tree import PySegment
from journal_test_helpers import Rig
from p33_helpers import seg_a, seg_b
from timeline_p32a_helpers import S2, a_seg, make_timeline_rig, seg_sel

REMOVE = "segment.remove_connection"
CREATE = "segment.create_connection"
CHECK = "journal.check"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def s2_seg(rig: Rig) -> PySegment:
    seg: PySegment = rig.handles["s2"].versions[0].tracks[0].segments[0]
    return seg


def s3_seg(rig: Rig) -> PySegment:
    seg: PySegment = rig.handles["s3"].versions[0].tracks[0].segments[0]
    return seg


def b_sel() -> dict[str, Any]:
    return a_seg(index=3)


def alone(seg: PySegment) -> bool:
    return seg.connected_segments() == [seg]


def connect_pairs(rig: Rig) -> None:
    fsc.connect(seg_a(rig), s2_seg(rig))
    fsc.connect(seg_b(rig), s3_seg(rig))


def intents(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "intent"]


def test_a_targets_step_without_an_answer_shuts_the_guard_until_checked(rig: Rig) -> None:
    connect_pairs(rig)
    rig.exec.fault(REMOVE, "TIMEOUT", when="after", skip=2)  # the second target's call
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel()]})
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert env["result"]["removed"] == [0] and env["result"]["unknown"] == [1]
    [blocked] = rig.runtime.guard.blocked()
    assert blocked.op == REMOVE and blocked.group == env["journal"]["group"]
    denied = rig.do(CREATE, {"target": a_seg()})
    assert denied["ok"] is False and denied["errorCode"] == "POLICY_DENIED", denied
    assert "journal.check" in denied["hint"]

    checked = rig.do(CHECK, {"group": env["journal"]["group"]})
    assert checked["ok"] is True, checked
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "landed" and row["op"] == REMOVE, row
    assert checked["result"]["writesBlocked"] is False and rig.runtime.guard.blocked() == []
    assert alone(seg_b(rig)) and alone(s3_seg(rig))
    again = rig.do(CREATE, {"target": a_seg()})
    assert again["errorCode"] != "POLICY_DENIED", again


def test_a_step_that_never_reached_flame_is_checked_as_not_landed(rig: Rig) -> None:
    connect_pairs(rig)
    rig.exec.fault(REMOVE, "BRIDGE_LOST", when="before", skip=2)
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel()]})
    assert env["ok"] is False and env["errorCode"] == "BRIDGE_LOST", env
    assert not alone(seg_b(rig))
    checked = rig.do(CHECK, {"group": env["journal"]["group"]})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "not_landed", row
    assert rig.runtime.guard.blocked() == []


def test_a_single_target_without_an_answer_shuts_the_guard_until_checked(rig: Rig) -> None:
    connect_pairs(rig)
    rig.exec.fault(REMOVE, "TIMEOUT", when="after", skip=1)  # the check read, then the write
    env = rig.do(REMOVE, {"target": a_seg()})
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert len(rig.runtime.guard.blocked()) == 1
    assert rig.do(CREATE, {"target": a_seg()})["errorCode"] == "POLICY_DENIED"
    checked = rig.do(CHECK, {"group": env["journal"]["group"]})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "landed", row
    assert rig.runtime.guard.blocked() == []


def test_a_step_that_was_answered_is_recorded_and_shuts_nothing(rig: Rig) -> None:
    connect_pairs(rig)
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel()]})
    assert env["ok"] is True, env
    assert env["journal"]["group"] and rig.runtime.guard.blocked() == []
    assert len(intents(rig)) == 2 and all(i["op"] == REMOVE for i in intents(rig))
    outcomes = [r for r in rig.records() if r.get("type") == "outcome"]
    assert [o["ok"] for o in outcomes] == [True, True]
    listed = rig.do("journal.list", {})
    assert listed["ok"] is True, listed
    revert = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert revert["ok"] is False and revert["errorCode"] == "PRECONDITION_UNMET", revert
    assert alone(seg_a(rig))


def test_a_refused_single_target_leaves_no_record(rig: Rig) -> None:
    env = rig.do(REMOVE, {"target": seg_sel(S2, 0, index=99)})
    assert env["ok"] is False, env
    assert rig.records() == [] and rig.runtime.guard.blocked() == []
