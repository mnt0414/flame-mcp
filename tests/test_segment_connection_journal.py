"""The connection writes are judged by what is read back, and each is an intent in the journal.

`segment.create_connection`, `segment.remove_connection` and `segment.sync_connections` are judged
by the read-back. A count that did not move is SILENT_NOOP, one that moved the wrong way is
FLAME_API_ERROR, and a read-back that raised leaves the intent open (unknown). Create, remove and
sync each write an intent, so an unanswered call shuts the write guard until `journal.check` has
read the segment's connections again. The intent records the segment's stable
address (wiretap id and position), not the caller's selector. Run through the real Runtime with a
scripted `transport.exec` over the fake Flame tree (fake_selection_connections.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fake_selection_connections as fsc
from fake_media_tree import PySegment, share_sources
from journal_test_helpers import Rig
from p33_helpers import seg_a
from timeline_p32a_helpers import S1, S1_WT, a_seg, make_timeline_rig, seg_sel, wt

CREATE = "segment.create_connection"
REMOVE = "segment.remove_connection"
SYNC = "segment.sync_connections"
CHECK = "journal.check"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def s2_seg(rig: Rig) -> PySegment:
    seg: PySegment = rig.handles["s2"].versions[0].tracks[0].segments[0]
    return seg


def link(rig: Rig) -> None:
    fsc.connect(seg_a(rig), s2_seg(rig))


def alone(seg: PySegment) -> bool:
    return seg.connected_segments() == [seg]


def intents(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "intent"]


def outcomes(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "outcome"]


def by_path() -> dict[str, Any]:
    parent = {"kind": "path", "path": ["RG", "Reel 1", "S1"], "root": "desktop"}
    return seg_sel(parent, 0, index=1)


def test_a_create_that_connects_nothing_is_a_silent_noop(rig: Rig) -> None:
    env = rig.do(CREATE, {"target": a_seg()})  # nothing shares its source: the fake connects none
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    result = env["result"]
    assert result["connected"] == {"before": 0, "after": 0} and result["inverse"] is None
    assert "create_connection() returned None" in env["error"]
    assert rig.runtime.guard.blocked() == []
    [outcome] = outcomes(rig)
    assert outcome["ok"] is False and outcome["errorCode"] == "SILENT_NOOP"


def test_a_remove_that_removes_nothing_is_a_silent_noop(rig: Rig) -> None:
    env = rig.do(REMOVE, {"target": a_seg()})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["inverse"] is None and rig.runtime.guard.blocked() == []


def test_a_create_that_leaves_fewer_connected_is_a_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    link(rig)
    monkeypatch.setattr(
        PySegment, "create_connection", lambda self: fsc.seg_remove_connection(self)
    )
    env = rig.do(CREATE, {"target": a_seg()})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["connected"] == {"before": 1, "after": 0}
    assert env["result"]["inverse"] is None and rig.runtime.guard.blocked() == []


def test_a_remove_that_leaves_more_connected_is_a_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    monkeypatch.setattr(
        PySegment, "remove_connection", lambda self: fsc.seg_create_connection(self)
    )
    env = rig.do(REMOVE, {"target": a_seg()})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["connected"] == {"before": 0, "after": 1}
    assert rig.runtime.guard.blocked() == []


def test_a_sync_that_changes_something_is_ok(rig: Rig) -> None:
    link(rig)
    before = s2_seg(rig).source_in.frame
    env = rig.do(SYNC, {"target": a_seg()})
    assert env["ok"] is True, env
    assert s2_seg(rig).source_in.frame != before
    assert env["result"]["syncedSegments"] == 1 and env["result"]["inverse"] is None
    assert env["journal"]["group"] and len(intents(rig)) == 1


def test_a_sync_that_changes_nothing_is_a_silent_noop(rig: Rig) -> None:
    link(rig)
    assert rig.do(SYNC, {"target": a_seg()})["ok"] is True
    again = rig.do(SYNC, {"target": a_seg()})
    assert again["ok"] is False and again["errorCode"] == "SILENT_NOOP", again
    assert again["result"]["syncedSegments"] == 0 and rig.runtime.guard.blocked() == []


def test_a_sync_of_a_segment_with_no_connection_is_a_silent_noop(rig: Rig) -> None:
    env = rig.do(SYNC, {"target": a_seg()})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env


@pytest.mark.parametrize("op", [CREATE, SYNC])
def test_a_create_or_sync_is_an_intent_with_an_outcome(rig: Rig, op: str) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    link(rig) if op == SYNC else None
    env = rig.do(op, {"target": a_seg()})
    assert env["ok"] is True, env
    [intent] = intents(rig)
    assert intent["op"] == op and intent["group"] == env["journal"]["group"]
    assert intent["connectedBefore"] == (1 if op == SYNC else 0)
    assert [o["ok"] for o in outcomes(rig)] == [True]
    revert = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert revert["ok"] is False and revert["errorCode"] == "PRECONDITION_UNMET", revert


def test_a_create_without_an_answer_shuts_the_guard_and_check_finds_it_landed(rig: Rig) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    rig.exec.fault(CREATE, "TIMEOUT", when="after", skip=1)  # the check read, then the write
    env = rig.do(CREATE, {"target": a_seg()})
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert "journal.check" in env["hint"]
    [blocked] = rig.runtime.guard.blocked()
    assert blocked.op == CREATE and blocked.group == env["journal"]["group"]
    denied = rig.do(SYNC, {"target": a_seg()})
    assert denied["ok"] is False and denied["errorCode"] == "POLICY_DENIED", denied

    checked = rig.do(CHECK, {})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "landed" and row["op"] == CREATE, row
    assert rig.runtime.guard.blocked() == [] and checked["result"]["writesBlocked"] is False


def test_a_create_that_never_reached_flame_is_checked_as_not_landed(rig: Rig) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    rig.exec.fault(CREATE, "BRIDGE_LOST", when="before", skip=1)
    env = rig.do(CREATE, {"target": a_seg()})
    assert env["errorCode"] == "BRIDGE_LOST", env
    assert alone(seg_a(rig))
    [row] = rig.do(CHECK, {})["result"]["checked"]
    assert row["verdict"] == "not_landed", row
    assert rig.runtime.guard.blocked() == []


def test_a_sync_without_an_answer_is_checked_as_diverged_and_the_guard_is_released(
    rig: Rig,
) -> None:
    link(rig)
    rig.exec.fault(SYNC, "TIMEOUT", when="after", skip=1)
    env = rig.do(SYNC, {"target": a_seg()})
    assert env["errorCode"] == "TIMEOUT" and len(rig.runtime.guard.blocked()) == 1, env
    assert rig.do(CREATE, {"target": a_seg()})["errorCode"] == "POLICY_DENIED"
    checked = rig.do(CHECK, {"group": env["journal"]["group"]})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "diverged" and row["op"] == SYNC, row
    assert "cannot be judged" in row["detail"] and "segment.connections" in row["detail"]
    assert rig.runtime.guard.blocked() == []
    assert rig.do(CREATE, {"target": a_seg()})["errorCode"] != "POLICY_DENIED"


def test_a_read_back_that_raises_after_the_call_leaves_the_intent_open(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    original = PySegment.create_connection
    broken = [False]

    def create_then_break(self: PySegment) -> Any:
        result = original(self)
        broken[0] = True
        return result

    def connected(self: PySegment) -> list[PySegment]:
        if broken[0]:
            raise RuntimeError("scripted failure")
        return fsc.seg_connected_segments(self)

    monkeypatch.setattr(PySegment, "create_connection", create_then_break)
    monkeypatch.setattr(PySegment, "connected_segments", connected)
    env = rig.do(CREATE, {"target": a_seg()})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "scripted failure" in env["error"]
    assert len(rig.runtime.guard.blocked()) == 1
    [row] = rig.do(CHECK, {})["result"]["checked"]
    assert row["verdict"] == "unknown", row  # Flame cannot be read: the intent stays open
    assert len(rig.runtime.guard.blocked()) == 1
    broken[0] = False
    [again] = rig.do(CHECK, {})["result"]["checked"]
    assert again["verdict"] == "landed", again
    assert rig.runtime.guard.blocked() == []


@pytest.mark.parametrize("op", [CREATE, REMOVE, SYNC])
def test_the_intent_holds_the_stable_address_not_the_callers_selector(rig: Rig, op: str) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    link(rig)
    rig.do(op, {"target": by_path()})
    [intent] = intents(rig)
    target = intent["target"]
    assert target["kind"] == "segment" and target["parent"] == wt(S1_WT)
    assert target["record_in"] == 10 and target["expect_source_name"] == "clip1"
    assert intent["args"]["target"] == target and "path" not in repr(target)


def test_the_targets_form_records_the_stable_address_of_every_step(rig: Rig) -> None:
    link(rig)
    env = rig.do(REMOVE, {"targets": [by_path()]})
    assert env["ok"] is True, env
    [intent] = intents(rig)
    assert intent["target"]["parent"] == wt(S1_WT) and intent["target"]["record_in"] == 10


def test_check_reads_the_recorded_segment_even_when_the_callers_path_no_longer_resolves(
    rig: Rig,
) -> None:
    link(rig)
    rig.exec.fault(REMOVE, "TIMEOUT", when="after", skip=1)
    env = rig.do(REMOVE, {"target": by_path()})
    assert env["errorCode"] == "TIMEOUT", env
    rig.handles["s1"].name.set_value("S1 renamed")  # the path ["RG", "Reel 1", "S1"] is gone now
    [row] = rig.do(CHECK, {})["result"]["checked"]
    assert row["verdict"] == "landed", row
    assert S1 == wt(S1_WT)


@pytest.mark.parametrize("op", [CREATE, REMOVE, SYNC])
def test_a_refused_call_leaves_no_record(rig: Rig, op: str) -> None:
    env = rig.do(op, {"target": seg_sel(S1, 0, index=0)})  # a Gap
    assert env["errorCode"] == "PRECONDITION_UNMET", env
    assert rig.records() == [] and rig.runtime.guard.blocked() == []


@pytest.mark.parametrize("op", [CREATE, REMOVE, SYNC])
def test_a_plan_still_refuses_the_connection_writes(rig: Rig, op: str) -> None:
    env = rig.do("plan.run", {"steps": [{"op": op, "args": {"target": a_seg()}}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
