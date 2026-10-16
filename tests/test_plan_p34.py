"""Tests the construction ops inside `plan.run`'s bundle.

create_track, create_version, overwrite, insert and cut on one sequence are ONE checkpointed write
-- one stashed copy, one revert -- and they mix with the segment edits, while
`timeline.create_sequence` cannot be a plan step. Run through the real Runtime over the fake Flame
tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from journal_test_helpers import Rig
from p32b_helpers import PLAN, S1, make_p32b_rig, uid
from p33_helpers import add_media_clip, clip_sel, seg_row, track0
from timeline_p32a_helpers import (
    TRIM,
    checkpoints,
    fingerprint_of_target,
    rows,
    seg_sel,
    stash_entries,
    stash_library,
    wt,
)

OVERWRITE = "timeline.overwrite"
INSERT = "timeline.insert"
CUT = "timeline.cut"
CREATE_VERSION = "timeline.create_version"
CREATE_TRACK = "timeline.create_track"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    r = make_p32b_rig(monkeypatch, tmp_path)
    r.handles["c12"] = add_media_clip(r, "len12", start=0, length=12)
    return r


def test_a_plan_builds_a_track_fills_it_and_cuts_as_one_checkpointed_write(rig: Rig) -> None:
    clip = clip_sel(rig.handles["c12"])
    before = fingerprint_of_target(rig, S1)
    steps = [
        {"op": CREATE_TRACK, "args": {"sequence": S1}},
        {"op": OVERWRITE, "args": {"sequence": S1, "source": clip, "at": 8, "track": 2}},
        {"op": INSERT, "args": {"sequence": S1, "source": clip, "at": 20, "track": 0}},
        {"op": CUT, "args": {"sequence": S1, "at": 4, "scope": "all"}},
        {"op": CREATE_VERSION, "args": {"sequence": S1}},
    ]
    env = rig.do(PLAN, {"steps": steps, "label": "p34 build"})
    assert env["ok"] is True, env
    assert [s["ok"] for s in env["result"]["steps"]] == [True] * 5
    assert len(checkpoints(rig)) == 1 and len(stash_entries(rig)) == 1
    assert [c.label for c in rig.exec.calls].count("timeline.checkpoint") == 1
    s1 = rig.handles["s1"]
    assert len(s1.versions) == 2 and len(s1.versions[0].tracks) == 3
    assert [r[:3] for r in rows(s1, 2)] == [
        ["Gap", 0, 3],
        ["Gap", 4, 7],
        ["Video Segment", 8, 19],
        ["Gap", 20, 63],
    ]
    timeline = env["result"]["timeline"]
    assert timeline["fingerprintBefore"] == before and timeline["changed"] is True
    assert timeline["target"]["uid"] == uid(s1)
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_the_new_ops_mix_with_a_segment_edit_in_one_plan(rig: Rig) -> None:
    steps = [
        {"op": TRIM, "args": {"target": seg_sel(S1, 0, record_in=10), "edge": "tail", "offset": 3}},
        {"op": CUT, "args": {"sequence": S1, "at": 20, "track": 0}},
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is True, env
    assert len(checkpoints(rig)) == 1
    assert seg_row(track0(rig)[1])[1:3] == [10, 19] and seg_row(track0(rig)[2])[1:3] == [20, 30]


def test_a_step_that_does_not_apply_stops_the_plan_and_the_revert_puts_it_all_back(
    rig: Rig,
) -> None:
    before = fingerprint_of_target(rig, S1)
    steps = [
        {"op": CREATE_TRACK, "args": {"sequence": S1}},
        {"op": CUT, "args": {"sequence": S1, "at": 10, "track": 0}},  # a segment's first frame
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["stoppedAt"] == 1 and env["result"]["steps"][0]["ok"] is True
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_a_plan_with_a_construction_step_on_two_sequences_is_refused(rig: Rig) -> None:
    other = wt("/wt/RG/Reel 1/S2")
    steps = [
        {"op": CREATE_TRACK, "args": {"sequence": S1}},
        {"op": CREATE_TRACK, "args": {"sequence": other}},
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and "different sequences" in env["error"]
    assert rig.records() == [] and stash_library(rig) is None


def test_create_sequence_cannot_be_a_plan_step_and_cannot_share_a_bundled_plan(rig: Rig) -> None:
    make = {
        "op": "timeline.create_sequence",
        "args": {
            "parent": wt("/wt/RG/Reel 1"),
            "name": "X",
            "frame_rate": "25 fps",
            "width": 10,
            "height": 10,
        },
    }
    steps = [{"op": CREATE_TRACK, "args": {"sequence": S1}}, make]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and "cannot be a plan step" in str(env["result"]["issues"])
    assert rig.records() == [] and stash_library(rig) is None


def test_a_step_cannot_address_its_track_before_it_exists(rig: Rig) -> None:
    steps = [
        {
            "op": OVERWRITE,
            "args": {"sequence": S1, "source": clip_sel(rig.handles["c12"]), "at": 8, "track": 2},
        }
    ]
    env: Any = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert len(rig.handles["s1"].versions[0].tracks) == 2
