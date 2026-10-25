"""Tests `segment.remove_connection` with `targets` (1 to MAX_REMOVE_TARGETS segments in one call).

Like the single form it takes no checkpoint and has no revert; each step is an intent in the
journal. Everything that can be refused (a Gap, a library, a selector that does not resolve) is
refused in one read before any connection is removed; then each target is a call of its own.
Removing the connection of one segment also removes it from the segment it was connected to
(measured on Flame 2025.2.4 with a pair of connected segments; a group of three or more was not
measured), so a target whose connection an earlier target already took away is reported in
result.alreadyDisconnected, not failed. Run through the real Runtime with a scripted
`transport.exec` over the fake Flame tree (fake_selection_connections.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fake_selection_connections as fsc
from fake_media_tree import PySegment, share_sources
from flame_mcp import execution
from flame_mcp.ops.segment_connect import MAX_REMOVE_TARGETS
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import seg_a, seg_b, untouched_world
from timeline_p32a_helpers import (
    S1,
    S2,
    S3,
    a_seg,
    library_copy,
    make_timeline_rig,
    media,
    seg_sel,
    wt,
)

REMOVE = "segment.remove_connection"
CREATE = "segment.create_connection"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def s2_seg(rig: Rig) -> PySegment:
    seg: PySegment = rig.handles["s2"].versions[0].tracks[0].segments[0]
    return seg


def s3_seg(rig: Rig) -> PySegment:
    seg: PySegment = rig.handles["s3"].versions[0].tracks[0].segments[0]
    return seg


def seg_c(rig: Rig) -> PySegment:
    seg: PySegment = rig.handles["s1"].versions[0].tracks[1].segments[0]
    return seg


def s2_extra(rig: Rig) -> PySegment:
    """A second segment in S2 (clip3, 24-47), the partner of C."""
    track = rig.handles["s2"]._versions[0]._tracks[0]
    extra = media("clip3", 24, 47, source_in=3)
    track._segments.append(extra)
    extra.parent = track
    return extra


def connect_pairs(rig: Rig) -> None:
    """A-S2 segment, B-S3 segment, C-S2 extra: three separate connections."""
    fsc.connect(seg_a(rig), s2_seg(rig))
    fsc.connect(seg_b(rig), s3_seg(rig))
    fsc.connect(seg_c(rig), s2_extra(rig))


def b_sel() -> dict[str, Any]:
    return a_seg(index=3)


def c_sel() -> dict[str, Any]:
    return a_seg(index=0, track=1)


def s2_sel() -> dict[str, Any]:
    return seg_sel(S2, 0, index=0)


def alone(seg: PySegment) -> bool:
    return seg.connected_segments() == [seg]


def test_two_targets_remove_both_connections_in_one_call(rig: Rig) -> None:
    connect_pairs(rig)
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel()]})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["action"] == "remove" and result["requested"] == 2
    assert result["removed"] == [0, 1] and result["notRemoved"] == [] and result["unknown"] == []
    assert result["alreadyDisconnected"] == [] and result["ran"] is True
    assert [i["connected"] for i in result["items"]] == [
        {"before": 1, "after": 0},
        {"before": 1, "after": 0},
    ]
    assert [i["index"] for i in result["items"]] == [0, 1]
    assert alone(seg_a(rig)) and alone(s2_seg(rig)) and alone(seg_b(rig)) and alone(s3_seg(rig))
    assert not alone(seg_c(rig))  # not a target: still connected
    assert env["journal"]["group"] and len(env["journal"]["ids"]) == 2
    assert env["affected"] == 2 and env["undoSteps"] == 0
    assert [c.label for c in rig.exec.calls] == [REMOVE] * 3  # one check, one call per target
    [notice] = result["notices"]
    assert "other sequences" in notice and "journal.revert" in notice
    assert not rig.flame.media_panel.copy_calls


def test_three_targets_remove_three_connections(rig: Rig) -> None:
    connect_pairs(rig)
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel(), c_sel()]})
    assert env["ok"] is True, env
    assert env["result"]["removed"] == [0, 1, 2] and env["result"]["requested"] == 3
    assert all(alone(s) for s in (seg_a(rig), seg_b(rig), seg_c(rig), s2_extra(rig)))
    assert [c.label for c in rig.exec.calls] == [REMOVE] * 4


def test_both_sides_of_one_connection_are_not_a_failure(rig: Rig) -> None:
    fsc.connect(seg_a(rig), s2_seg(rig))
    env = rig.do(REMOVE, {"targets": [a_seg(), s2_sel()]})
    assert env["ok"] is True and env["errorCode"] is None, env
    result = env["result"]
    assert result["alreadyDisconnected"] == [1] and result["removed"] == [0]
    first, second = result["items"]
    assert first["ran"] is True and first["alreadyDisconnected"] is False
    assert second["ran"] is False and second["alreadyDisconnected"] is True
    assert second["connected"] == {"before": 0, "after": 0} and second["inverse"] is None
    assert result["warnings"] == []
    assert alone(seg_a(rig)) and alone(s2_seg(rig))
    assert env["partial"] is None and env["affected"] == 1
    assert [c.label for c in rig.exec.calls] == [REMOVE] * 3


def test_a_target_that_was_never_connected_is_reported_with_a_warning(rig: Rig) -> None:
    fsc.connect(seg_b(rig), s3_seg(rig))
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel()]})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["alreadyDisconnected"] == [0] and result["removed"] == [1]
    [warning] = result["warnings"]
    assert "targets[0]" in warning and "was not connected before this call" in warning
    assert alone(seg_b(rig))


def test_the_same_segment_twice_is_removed_once(rig: Rig) -> None:
    fsc.connect(seg_a(rig), s2_seg(rig))
    env = rig.do(REMOVE, {"targets": [a_seg(), a_seg()]})
    assert env["ok"] is True, env
    assert env["result"]["alreadyDisconnected"] == [1] and env["result"]["removed"] == [0]


def test_target_and_targets_together_are_refused(rig: Rig) -> None:
    env = rig.do(REMOVE, {"target": a_seg(), "targets": [b_sel()]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "exactly one of" in env["error"]
    assert rig.exec.calls == []


def test_neither_target_nor_targets_is_refused(rig: Rig) -> None:
    env = rig.do(REMOVE, {})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "target" in env["error"] and rig.exec.calls == []


def test_an_empty_targets_is_refused(rig: Rig) -> None:
    env = rig.do(REMOVE, {"targets": []})
    assert env["errorCode"] == "INVALID_ARGS" and f"1 to {MAX_REMOVE_TARGETS}" in env["error"]
    assert rig.exec.calls == []


def test_more_targets_than_the_limit_are_refused(rig: Rig) -> None:
    env = rig.do(REMOVE, {"targets": [a_seg()] * (MAX_REMOVE_TARGETS + 1)})
    assert env["errorCode"] == "INVALID_ARGS" and f"1 to {MAX_REMOVE_TARGETS}" in env["error"]
    assert rig.exec.calls == []


def test_the_limit_itself_is_accepted(rig: Rig) -> None:
    env = rig.do(REMOVE, {"targets": [a_seg()] * MAX_REMOVE_TARGETS})
    assert env["ok"] is True, env
    assert env["result"]["requested"] == MAX_REMOVE_TARGETS
    assert len(rig.exec.calls) == MAX_REMOVE_TARGETS + 1


def test_a_target_that_is_not_a_segment_selector_is_refused(rig: Rig) -> None:
    env = rig.do(REMOVE, {"targets": [a_seg(), wt("x")]})
    assert env["errorCode"] == "INVALID_ARGS" and "targets[1]" in env["error"]
    assert rig.exec.calls == []


def test_a_target_that_does_not_resolve_stops_everything_before_any_write(rig: Rig) -> None:
    connect_pairs(rig)
    env = rig.do(REMOVE, {"targets": [a_seg(), a_seg(index=99), b_sel()]})
    assert env["ok"] is False, env
    assert not alone(seg_a(rig)) and not alone(seg_b(rig))
    assert env["journal"] is None


def test_a_gap_among_the_targets_is_refused_before_anything_is_changed(rig: Rig) -> None:
    connect_pairs(rig)
    env = rig.do(REMOVE, {"targets": [a_seg(), seg_sel(S1, 0, index=0), b_sel()]})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "targets[1]" in env["error"]
    assert "Gap" in env["error"] and env["result"]["ran"] is False
    assert not alone(seg_a(rig)) and not alone(seg_b(rig))


def test_a_segment_of_a_sequence_in_a_library_is_refused(rig: Rig) -> None:
    connect_pairs(rig)
    copy = library_copy(rig)
    inside = seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1)
    env = rig.do(REMOVE, {"targets": [a_seg(), inside]})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert not alone(seg_a(rig))


def test_a_connected_segments_that_raises_stops_before_any_write(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: PySegment) -> list[PySegment]:
        raise RuntimeError("scripted failure")

    monkeypatch.setattr(PySegment, "connected_segments", broken)
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel()]})
    assert env["errorCode"] == "FLAME_API_ERROR" and "nothing was changed" in env["error"]
    assert "RuntimeError: scripted failure" in env["error"]


def test_each_item_hands_back_the_presumed_inverse_and_nothing_can_be_reverted(rig: Rig) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    share_sources(seg_b(rig), s3_seg(rig))
    fsc.connect(seg_a(rig), s2_seg(rig))
    fsc.connect(seg_b(rig), s3_seg(rig))
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel()]})
    assert env["ok"] is True and env["journal"]["group"], env
    [first, second] = env["result"]["items"]
    assert first["inverse"] == {"op": CREATE, "args": {"target": first["position"]["selector"]}}
    assert second["inverse"]["op"] == CREATE
    undone = rig.do(first["inverse"]["op"], first["inverse"]["args"])
    assert undone["ok"] is True and undone["result"]["connected"] == {"before": 0, "after": 1}
    assert not alone(seg_a(rig)) and alone(seg_b(rig))
    revert = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert revert["ok"] is False and revert["errorCode"] == "PRECONDITION_UNMET"
    assert not rig.flame.media_panel.copy_calls


def test_a_plan_refuses_the_targets_form(rig: Rig) -> None:
    connect_pairs(rig)
    env = rig.do("plan.run", {"steps": [{"op": REMOVE, "args": {"targets": [a_seg(), b_sel()]}}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    [issue] = env["result"]["issues"]
    assert "cannot be a plan step" in " ".join(issue["problems"])
    assert not alone(seg_a(rig)) and untouched_world(rig)


def test_a_readonly_server_denies_the_targets_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    fsc.connect(seg_a(ro), s2_seg(ro))
    env = ro.do(REMOVE, {"targets": [a_seg(), s2_sel()]})
    assert env["errorCode"] == "POLICY_DENIED"
    assert not alone(seg_a(ro)) and S3 != S1


def test_the_op_declares_both_forms(rig: Rig) -> None:
    op = rig.registry.get(REMOVE)
    assert op is not None
    params = {p.name: p for p in op.params}
    assert list(params) == ["target", "targets"]
    assert params["target"].required is False and params["targets"].required is False
    assert params["targets"].type == "array" and params["targets"].item_type == "selector"
    assert (op.effect, op.undo) == ("destructive", "unsupported")
    assert op.wrap is not None and op.checkpoint is None and op.journal is None
    flat = " ".join(op.description.split())
    for word in ("targets", "alreadyDisconnected", "plan step", "changes no connection", "pair"):
        assert word in flat, word
    create = rig.registry.get(CREATE)
    assert create is not None and [p.name for p in create.params] == ["target"]


def test_a_call_that_raises_midway_stops_and_reports_what_was_removed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_pairs(rig)
    real = PySegment.remove_connection

    def flaky(self: PySegment) -> Any:
        if self is seg_b(rig):
            raise RuntimeError("scripted failure")
        return real(self)

    monkeypatch.setattr(PySegment, "remove_connection", flaky)
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel(), c_sel()]})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "targets[1]" in env["error"] and "scripted failure" in env["error"]
    assert "1 earlier target(s) were removed" in env["error"]
    result = env["result"]
    assert result["removed"] == [0] and result["notRemoved"] == [1, 2] and result["unknown"] == []
    assert result["partial"] is True and len(result["items"]) == 2
    assert env["affected"] == 1 and env["partial"] == {"removed": 1, "notRemoved": 2}
    assert len(rig.exec.calls) == 3  # the check, A, B: C is not reached, nothing is retried
    assert alone(seg_a(rig)) and not alone(seg_b(rig)) and not alone(seg_c(rig))


def test_a_target_that_does_not_resolve_is_named_by_the_resolver(rig: Rig) -> None:
    env = rig.do(REMOVE, {"targets": [a_seg(), a_seg(index=99)]})
    assert env["ok"] is False and env["result"] is None


def test_a_read_back_that_raises_after_the_call_is_unknown_not_failed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_pairs(rig)
    real = PySegment.remove_connection

    def then_unreadable(self: PySegment) -> Any:
        answer = real(self)
        if self is seg_b(rig):

            def broken(seg: PySegment) -> list[PySegment]:
                raise RuntimeError("unreadable")

            monkeypatch.setattr(PySegment, "connected_segments", broken)
        return answer

    monkeypatch.setattr(PySegment, "remove_connection", then_unreadable)
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel(), c_sel()]})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    result = env["result"]
    assert result["removed"] == [0] and result["unknown"] == [1] and result["notRemoved"] == [2]
    assert result["items"][1]["unknown"] is True and result["items"][0]["unknown"] is False
    assert "targets[1]" in env["error"] and "may be gone" in env["error"]
    assert env["partial"] == {"removed": 2, "notRemoved": 1} and env["affected"] == 2
    assert len(rig.exec.calls) == 3


def test_a_step_without_an_answer_is_unknown_stops_and_is_not_retried(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_pairs(rig)
    real = execution.run_op
    seen: list[str | None] = []

    def scripted(op: Any, args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(args.get("_phase"))
        if len(seen) == 3:  # the second target's call
            return {
                "ok": False,
                "result": None,
                "errorCode": "TIMEOUT",
                "error": "no answer",
            }
        out: dict[str, Any] = real(op, args, **kwargs)
        return out

    monkeypatch.setattr(execution, "run_op", scripted)
    env = rig.do(REMOVE, {"targets": [a_seg(), b_sel(), c_sel()]})
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    result = env["result"]
    assert result["removed"] == [0] and result["unknown"] == [1] and result["notRemoved"] == [2]
    assert seen == ["check", "step", "step"]  # C is not reached and B is not called again
    [blocked] = rig.runtime.guard.blocked()  # B's intent is open until journal.check
    assert blocked.op == REMOVE and blocked.group == env["journal"]["group"]
