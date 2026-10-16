"""Tests the segment edits inside `plan.run`'s bundle.

Slip, replace, trim and create_effect / set_effect / delete_effect / effect_load_setup on one
sequence are ONE checkpointed write -- one stashed copy, one revert -- while the ops that make a
clip (match, copy_to_media_panel) and the file ops cannot be mixed in. Run through the real
Runtime over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from journal_test_helpers import Rig
from p32b_helpers import PLAN, S1, make_p32b_rig, uid
from p33_helpers import (
    COPY_OUT,
    CREATE_FX,
    DELETE_FX,
    LETTERBOX,
    LOAD_FX,
    MATCH,
    REPLACE,
    SAVE_FX,
    SET_FX,
    SLIP,
    add_media_clip,
    clip_sel,
    effect_names,
    seg_a,
    seg_row,
    track0,
)
from timeline_p32a_helpers import (
    TRIM,
    checkpoints,
    fingerprint_of_target,
    seg_sel,
    stash_entries,
    stash_library,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def at(record_in: int, track: int = 0, parent: dict[str, Any] = S1) -> dict[str, Any]:
    return seg_sel(parent, track, record_in=record_in)


def step(op: str, record_in: int = 10, **args: Any) -> dict[str, Any]:
    return {"op": op, "args": {"target": at(record_in), **args}}


def test_slip_replace_trim_and_effects_in_one_plan_are_one_checkpointed_write(rig: Rig) -> None:
    new = add_media_clip(rig, "newclip", start=0, length=48)
    before = fingerprint_of_target(rig, S1)
    steps = [
        step(SLIP, 10, offset=2),
        step(REPLACE, 40, source=clip_sel(new), mode="media"),
        step(TRIM, 10, edge="tail", offset=3),
        step(CREATE_FX, 10, type="Blur"),
        step(SET_FX, 40, type=LETTERBOX, values={"ratio": 1.85}),
    ]
    env = rig.do(PLAN, {"steps": steps, "label": "p33 mix"})
    assert env["ok"] is True, env
    assert [s["ok"] for s in env["result"]["steps"]] == [True] * 5
    assert len(checkpoints(rig)) == 1 and len(stash_entries(rig)) == 1
    labels = [c.label for c in rig.exec.calls]
    assert labels.count("timeline.checkpoint") == 1 and env["undoSteps"] == 0
    assert env["partial"] == {"done": 5, "failed": 0, "notRun": 0}
    assert seg_row(seg_a(rig))[0:3] == ["clip1", 10, 30]  # trimmed (tail 3) after the slip
    assert seg_row(seg_a(rig))[3] == 3  # slip(2): source_in 5 -> 3
    assert effect_names(seg_a(rig)) == ["Blur"]
    assert track0(rig)[3].source_name == "newclip"
    timeline = env["result"]["timeline"]
    assert timeline["fingerprintBefore"] == before and timeline["changed"] is True
    assert timeline["target"]["uid"] == uid(rig.handles["s1"])
    group = env["result"]["group"]
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True, out
    restored = out["result"]["restored"]["selector"]
    assert fingerprint_of_target(rig, restored) == before


def test_the_effect_steps_that_read_a_file_run_in_the_bundle_too(rig: Rig, tmp_path: Path) -> None:
    saved = rig.do(
        SAVE_FX,
        {"target": at(40), "type": LETTERBOX, "path": str(tmp_path / "lb")},
    )
    assert saved["ok"] is True, saved
    path = next(f["path"] for f in saved["result"]["files"] if f["path"].endswith("_node"))
    steps = [
        step(DELETE_FX, 40, type=LETTERBOX),
        step(CREATE_FX, 40, type=LETTERBOX),
        step(LOAD_FX, 40, type=LETTERBOX, path=path),
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is True, env
    assert len(checkpoints(rig)) == 1


def test_each_new_op_is_checked_up_front_so_a_bad_step_stops_everything(rig: Rig) -> None:
    steps = [step(SLIP, 10, offset=1), step(SLIP, 999, offset=1)]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and "does not resolve" in env["error"]
    assert stash_library(rig) is None and rig.records() == []


def test_a_step_refused_by_its_own_check_when_its_turn_comes_stops_the_plan_and_reverts(
    rig: Rig,
) -> None:
    before = fingerprint_of_target(rig, S1)
    steps = [step(SLIP, 10, offset=2), step(SLIP, 10, offset=9)]  # head handle 5 -> 3 left
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["stoppedAt"] == 1 and env["result"]["steps"][0]["ok"] is True
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_an_automatic_resize_is_refused_inside_a_plan_only_after_the_plan_checkpoint(
    rig: Rig,
) -> None:
    from fake_media_tree import PyTimelineFX

    seg = seg_a(rig)
    resize = PyTimelineFX("Resize")
    resize.parent = seg
    seg.effects.append(resize)
    seg.source_bit_depth = 10
    rig.handles["s1"].bit_depth = 8
    before = fingerprint_of_target(rig, S1)
    steps = [step(SLIP, 10, offset=1), step(DELETE_FX, 10, type="Resize")]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "treated as the one Flame adds" in env["error"]
    assert env["result"]["stoppedAt"] == 1 and env["result"]["steps"][0]["ok"] is True
    assert len(checkpoints(rig)) == 1 and rig.records() != []
    assert effect_names(seg_a(rig)) == ["Resize"]
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


@pytest.mark.parametrize("op", [MATCH, COPY_OUT])
def test_the_clip_making_ops_cannot_share_a_bundled_plan(rig: Rig, op: str) -> None:
    steps = [
        step(SLIP, 10, offset=1),
        step(op, 10, destination=wt("/wt/RG/Reel 1")),
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and "cannot be a plan step" in str(env["result"]["issues"])
    assert stash_library(rig) is None and rig.records() == []


def test_the_save_op_cannot_share_a_bundled_plan(rig: Rig, tmp_path: Path) -> None:
    steps = [
        step(SLIP, 10, offset=1),
        step(SAVE_FX, 40, type=LETTERBOX, path=str(tmp_path / "x")),
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and "cannot share a plan" in str(env["result"]["issues"])
    assert rig.records() == [] and list(tmp_path.glob("x*")) == []


def test_a_plan_of_a_slip_on_another_sequence_and_one_here_is_refused(rig: Rig) -> None:
    other = seg_sel(wt("/wt/RG/Reel 1/S2"), 0, index=0)
    steps = [step(SLIP, 10, offset=1), {"op": SLIP, "args": {"target": other, "offset": 1}}]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and "different sequences" in env["error"]
    assert rig.records() == []
