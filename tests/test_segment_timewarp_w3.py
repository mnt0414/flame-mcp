"""Tests `segment.trim` / `segment.slip` on a segment with a Timewarp.

A Timewarp made in the UI makes Flame keep the segment's SOURCE range at the speed (44 record
frames show 35 source frames at 0.8), so a trim or a slip moves source_in / source_out by the
speed times the record change, rounded -- while the record side, head and tail move 1:1 as without
it (on Flame 2025.2.4: trim +-3 -> source +-2, slip +-2 -> source_in -+2 and source_out -+1 or
-+2). The ops judge the record side, head and tail exactly as for a segment without a Timewarp,
leave the source side unjudged for a segment with a Timewarp (and say so), and keep judging it
for one without. Run through the real Runtime with a scripted `transport.exec` over the fake
Flame tree (fake_media_tree.PySegment.source_step)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PySegment, PyTime, PyTimelineFX
from flame_mcp.errors import hint_for
from journal_test_helpers import Rig
from p33_helpers import SLIP, seg_a, seg_row, untouched_world
from timeline_p32a_helpers import S1, TRIM, a_seg, make_timeline_rig, seg_sel, trim

PLAN = "plan.run"
SPEED = 0.8
A_ROW = ["clip1", 10, 33, 5, 23, 5, 5]
"""Segment A of the rig (record 10-33, source 5-28, handles 5 / 5) after `with_timewarp`: its 24
record frames show 19 source frames (24 x 0.8 = 19.2), source 5-23."""


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_timeline_rig(monkeypatch, tmp_path)
    with_timewarp(seg_a(made))
    return made


def with_timewarp(seg: PySegment, speed: float = SPEED) -> None:
    fx = PyTimelineFX("Timewarp")
    fx.hidden = {"speed": speed}
    fx.parent = seg
    seg.effects.append(fx)
    used = seg.record_out.frame - seg.record_in.frame + 1
    seg.source_out = PyTime(seg.source_in.frame + round(used * speed) - 1)


def slip(rig: Rig, offset: int, selector: dict[str, Any] | None = None) -> dict[str, Any]:
    return rig.do(SLIP, {"target": selector or a_seg(), "offset": offset})


def deltas(
    rin: int = 0, rout: int = 0, sin: int = 0, sout: int = 0, head: int = 0, tail: int = 0
) -> dict[str, int]:
    return {
        "recordIn": rin,
        "recordOut": rout,
        "sourceIn": sin,
        "sourceOut": sout,
        "head": head,
        "tail": tail,
    }


def assert_unjudged(result: dict[str, Any], want: dict[str, int]) -> None:
    assert result["timewarp"] is True and result["sourceJudged"] is False
    assert str(result["sourceNotJudgedBecause"]).startswith("Timewarp, source length ")
    assert result["deltas"] == want
    assert result["expectedDeltas"] == {**want, "sourceIn": None, "sourceOut": None}


def test_a_tail_trim_moves_the_source_by_the_speed_and_is_ok(rig: Rig) -> None:
    env = trim(rig, a_seg(), "tail", 3)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 10, 30, 5, 21, 5, 8]
    assert_unjudged(env["result"], deltas(rout=-3, sout=-2, tail=3))


def test_a_tail_extension_moves_the_source_by_the_speed_and_is_ok(rig: Rig) -> None:
    env = trim(rig, a_seg(), "tail", -3)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 10, 36, 5, 25, 5, 2]
    assert_unjudged(env["result"], deltas(rout=3, sout=2, tail=-3))


def test_a_head_trim_moves_the_source_by_the_speed_and_is_ok(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", 3)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 13, 33, 7, 23, 8, 5]
    assert_unjudged(env["result"], deltas(rin=3, sin=2, head=3))


def test_a_head_extension_moves_the_source_by_the_speed_and_is_ok(rig: Rig) -> None:
    env = trim(rig, a_seg(), "head", -3)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 7, 33, 3, 23, 2, 5]
    assert_unjudged(env["result"], deltas(rin=-3, sin=-2, head=-3))


def test_a_ripple_trim_is_ok_too(rig: Rig) -> None:
    head = trim(rig, a_seg(), "head", 3, ripple=True)
    assert head["ok"] is True, head
    assert seg_row(seg_a(rig)) == ["clip1", 10, 30, 7, 23, 8, 5]
    assert_unjudged(head["result"], deltas(rout=-3, sin=2, head=3))
    tail = trim(rig, a_seg(), "tail", 3, ripple=True)
    assert tail["ok"] is True, tail
    assert seg_row(seg_a(rig)) == ["clip1", 10, 27, 7, 21, 8, 8]
    assert_unjudged(tail["result"], deltas(rout=-3, sout=-2, tail=3))


def test_the_handle_check_is_still_in_record_frames(rig: Rig) -> None:
    env = trim(rig, a_seg(), "tail", -6)  # tail handle 5
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "tail handle is 5" in env["error"] and untouched_world(rig)
    assert seg_row(seg_a(rig)) == A_ROW


def test_a_trim_that_moves_the_record_side_wrongly_is_still_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PySegment._trim

    def odd(self: PySegment, edge: str, offset: int, ripple: bool) -> bool:
        original(self, edge, offset, ripple)
        self._set_record(self.record_in.frame, self.record_out.frame + 1)  # one frame too many
        return False

    monkeypatch.setattr(PySegment, "_trim", odd)
    env = trim(rig, a_seg(), "tail", 3)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "not the way" in env["error"]
    result = env["result"]
    assert result["timewarp"] is True and result["sourceJudged"] is False
    assert result["deltas"]["recordOut"] == -2 and result["expectedDeltas"]["recordOut"] == -3
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_a_trim_that_moves_head_wrongly_is_still_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PySegment._trim

    def odd(self: PySegment, edge: str, offset: int, ripple: bool) -> bool:
        original(self, edge, offset, ripple)
        self.head = int(self.head) + 1
        return False

    monkeypatch.setattr(PySegment, "_trim", odd)
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["deltas"]["head"] == 3 and env["result"]["expectedDeltas"]["head"] == 2


def test_without_a_timewarp_the_source_side_is_judged_as_before(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PySegment._trim

    def odd(self: PySegment, edge: str, offset: int, ripple: bool) -> bool:
        original(self, edge, offset, ripple)
        self.source_out = PyTime(self.source_out.frame + 1)  # only the source is off
        return False

    monkeypatch.setattr(PySegment, "_trim", odd)
    env = trim(rig, seg_sel(S1, 1, index=0), "tail", 3)  # C: no Timewarp
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    result = env["result"]
    assert result["timewarp"] is False and result["sourceJudged"] is True
    assert result["deltas"]["sourceOut"] == -2 and result["expectedDeltas"]["sourceOut"] == -3


def test_without_a_timewarp_the_result_says_the_source_was_judged(rig: Rig) -> None:
    env = trim(rig, seg_sel(S1, 1, index=0), "tail", 3)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["timewarp"] is False and result["sourceJudged"] is True
    assert result["deltas"] == result["expectedDeltas"] == deltas(rout=-3, sout=-3, tail=3)


def test_other_effects_are_not_a_timewarp(rig: Rig) -> None:
    env = trim(rig, seg_sel(S1, 0, index=3), "tail", 3)  # B: a Burn-in Letterbox
    assert env["ok"] is True, env
    assert env["result"]["timewarp"] is False and env["result"]["sourceJudged"] is True


def test_a_positive_slip_moves_the_source_by_the_speed_and_is_ok(rig: Rig) -> None:
    env = slip(rig, 2)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 10, 33, 3, 22, 3, 7]
    result = env["result"]
    assert_unjudged(result, deltas(sin=-2, sout=-1, head=-2, tail=2))
    assert result["moved"] == 2 and result["requested"] == 2


def test_a_slip_whose_source_out_rounds_the_other_way_is_ok_too(rig: Rig) -> None:
    seg_a(rig)._slip_out_rounds = True
    env = slip(rig, 2)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 10, 33, 3, 21, 3, 7]
    assert_unjudged(env["result"], deltas(sin=-2, sout=-2, head=-2, tail=2))


def test_a_negative_slip_moves_the_source_by_the_speed_and_is_ok(rig: Rig) -> None:
    env = slip(rig, -2)
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["clip1", 10, 33, 7, 24, 7, 3]
    assert_unjudged(env["result"], deltas(sin=2, sout=1, head=2, tail=-2))


def test_the_slip_handle_check_is_still_in_record_frames(rig: Rig) -> None:
    env = slip(rig, 6)  # head handle 5
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["available"] == 5 and untouched_world(rig)


def test_a_slip_that_moves_head_wrongly_is_still_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PySegment.slip

    def odd(self: PySegment, offset: int, *args: Any, **kwargs: Any) -> bool:
        original(self, offset)
        self.head = int(self.head) + 1
        return False

    monkeypatch.setattr(PySegment, "slip", odd)
    env = slip(rig, 2)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["timewarp"] is True and env["result"]["sourceJudged"] is False
    assert env["result"]["deltas"]["head"] == -1 and env["result"]["expectedDeltas"]["head"] == -2


def test_a_slip_that_moves_the_record_is_still_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PySegment.slip

    def odd(self: PySegment, offset: int, *args: Any, **kwargs: Any) -> bool:
        original(self, offset)
        self._set_record(self.record_in.frame + 1, self.record_out.frame + 1)
        return False

    monkeypatch.setattr(PySegment, "slip", odd)
    env = slip(rig, 2)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "no segment starts at record_in 10" in env["error"]


def test_without_a_timewarp_a_slip_is_judged_on_the_source_as_before(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PySegment.slip

    def odd(self: PySegment, offset: int, *args: Any, **kwargs: Any) -> bool:
        original(self, offset)
        self.source_out = PyTime(self.source_out.frame + 1)
        return False

    monkeypatch.setattr(PySegment, "slip", odd)
    env = slip(rig, 2, seg_sel(S1, 1, index=0))  # C: head 3, tail 2, no Timewarp
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    result = env["result"]
    assert result["timewarp"] is False and result["sourceJudged"] is True
    assert result["deltas"]["sourceOut"] == -1 and result["expectedDeltas"]["sourceOut"] == -2


def test_without_a_timewarp_a_slip_says_the_source_was_judged(rig: Rig) -> None:
    env = slip(rig, 2, seg_sel(S1, 1, index=0))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["timewarp"] is False and result["sourceJudged"] is True
    assert result["deltas"] == result["expectedDeltas"] == deltas(sin=-2, sout=-2, head=-2, tail=2)


def step(op: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"op": op, "args": args}


def test_a_plan_with_a_trim_and_a_slip_on_the_timewarp_segment_is_ok(rig: Rig) -> None:
    at = seg_sel(S1, 0, record_in=10)
    env = rig.do(
        PLAN,
        {
            "steps": [
                step(TRIM, {"target": at, "edge": "tail", "offset": 3}),
                step(SLIP, {"target": at, "offset": 2}),
            ]
        },
    )
    assert env["ok"] is True, env
    first, second = (s["result"] for s in env["result"]["steps"])
    assert_unjudged(first, deltas(rout=-3, sout=-2, tail=3))
    assert_unjudged(second, deltas(sin=-2, sout=-1, head=-2, tail=2))
    assert seg_row(seg_a(rig)) == ["clip1", 10, 30, 3, 20, 3, 10]


def test_a_plan_step_that_moves_the_record_wrongly_stops_the_plan(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PySegment._trim

    def odd(self: PySegment, edge: str, offset: int, ripple: bool) -> bool:
        original(self, edge, offset, ripple)
        self.tail = int(self.tail) + 1
        return False

    monkeypatch.setattr(PySegment, "_trim", odd)
    at = seg_sel(S1, 0, record_in=10)
    env = rig.do(PLAN, {"steps": [step(TRIM, {"target": at, "edge": "tail", "offset": 3})]})
    assert env["ok"] is False, env
    failed = env["result"]["steps"][0]
    assert failed["ok"] is False and failed["errorCode"] == "FLAME_API_ERROR"


# Only a Timewarp whose source length differs from its record length is not judged.


def make_same_length(seg: PySegment) -> None:
    """A Timewarp that left the source as long as the record (100%, bypassed, made through the
    API ...): source 5-28 for the record 10-33."""
    for fx in seg.effects:
        fx.hidden = {"speed": 1.0}
    seg.source_out = PyTime(seg.source_in.frame + (seg.record_out.frame - seg.record_in.frame))


def drift_source_out(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    original = getattr(PySegment, method)

    def odd(self: PySegment, *args: Any, **kwargs: Any) -> bool:
        original(self, *args, **kwargs)
        self.source_out = PyTime(self.source_out.frame + 1)
        return False

    monkeypatch.setattr(PySegment, method, odd)


def test_the_reason_the_source_is_not_judged_is_in_the_result(rig: Rig) -> None:
    env = trim(rig, a_seg(), "tail", 3)
    assert env["ok"] is True, env
    assert (
        env["result"]["sourceNotJudgedBecause"] == "Timewarp, source length 19 != record length 24"
    )


def test_a_timewarp_that_left_the_source_as_long_as_the_record_is_judged_one_to_one(
    rig: Rig,
) -> None:
    make_same_length(seg_a(rig))
    env = trim(rig, a_seg(), "tail", 3)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["timewarp"] is True and result["sourceJudged"] is True
    assert result["sourceNotJudgedBecause"] is None
    assert result["deltas"] == result["expectedDeltas"] == deltas(rout=-3, sout=-3, tail=3)


def test_a_timewarp_of_the_same_length_with_a_source_that_drifts_is_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_same_length(seg_a(rig))
    drift_source_out(monkeypatch, "_trim")
    env = trim(rig, a_seg(), "tail", 3)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    result = env["result"]
    assert result["timewarp"] is True and result["sourceJudged"] is True
    assert result["deltas"]["sourceOut"] == -2 and result["expectedDeltas"]["sourceOut"] == -3


def test_a_slip_on_a_timewarp_of_the_same_length_is_judged_one_to_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_same_length(seg_a(rig))
    ok = slip(rig, 2)
    assert ok["ok"] is True, ok
    assert ok["result"]["sourceJudged"] is True and ok["result"]["sourceNotJudgedBecause"] is None
    assert ok["result"]["deltas"] == ok["result"]["expectedDeltas"]
    drift_source_out(monkeypatch, "slip")
    env = slip(rig, 1)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["sourceJudged"] is True


def test_without_a_timewarp_a_source_of_another_length_is_judged_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seg = seg_sel(S1, 1, index=0)  # C: record 0-47, source 3-50
    c = rig.handles["s1"].versions[0].tracks[1].segments[0]
    c.source_out = PyTime(c.source_out.frame - 5)  # 43 source frames for 48 record frames
    env = trim(rig, seg, "tail", 3)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["timewarp"] is False and result["sourceJudged"] is True
    assert result["sourceNotJudgedBecause"] is None
    assert result["deltas"] == result["expectedDeltas"]
    drift_source_out(monkeypatch, "_trim")
    bad = trim(rig, seg, "tail", 3)
    assert bad["ok"] is False and bad["errorCode"] == "FLAME_API_ERROR"


@pytest.mark.parametrize("fx_type", ["Timewarp ML", "TimeWarp", "timewarp", "Timewarp "])
def test_only_an_effect_named_exactly_timewarp_counts(rig: Rig, fx_type: str) -> None:
    seg = seg_a(rig)
    seg.effects[0].type = fx_type
    env = trim(rig, a_seg(), "tail", 3)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["timewarp"] is False and result["sourceJudged"] is True
    assert result["sourceNotJudgedBecause"] is None


def test_effects_that_cannot_be_read_stop_everything_before_anything_is_written(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: PySegment) -> list[Any]:
        raise RuntimeError("effects are not readable")

    monkeypatch.setattr(PySegment, "effects", property(broken), raising=False)
    for env in (trim(rig, a_seg(), "tail", 3), slip(rig, 2)):
        assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
        assert "effects" in env["error"] and "nothing was written" in env["error"]
        assert untouched_world(rig)
    assert seg_row(seg_a(rig)) == A_ROW


def test_the_api_error_hint_allows_for_a_result_that_differed_from_the_measured_one() -> None:
    hint = hint_for("FLAME_API_ERROR")
    assert "or what Flame did differed" in hint and "traceback" in hint
