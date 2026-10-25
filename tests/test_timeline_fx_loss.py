"""Tests that a restore of a Batch checkpoint says which reel clips lost their Timeline FX.

A Batch setup holds no Timeline FX of the clips in the group's reels: a clip whose FX were changed
or rendered inside the group comes back from `load_setup` without them (measured on Flame
2025.2.4), and a clip that was only copied in can lose them too. A
checkpoint therefore records, per clip of every schematic and shelf reel, the FX types; the
restore reads the same after its `load_setup` and reports the clips whose FX got fewer as
`result.timelineFxLost`, and the clips it could not compare as `result.timelineFxUnchecked`.
Through the real Runtime over the fake tree (`PyClip.fx_lost_on_load` stands for the lost case)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyBatch, PyClip, PyReel, PySegment, PyTimelineFX, PyTrack, PyVersion
from flame_mcp import timeline_fx_loss
from flame_mcp.checkpoint_prelude import CLIP_FX_READ_BUDGET_S
from flame_mcp.policy import Policy
from journal_test_helpers import BG1, Rig, make_rig
from test_reel_fidelity import CLIP1_ID, CLIP2_ID, checkpoint, restore, strip_from_records

ENABLED = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path, policy=ENABLED)


class UnreadableFxSegment(PySegment):
    @property
    def effects(self) -> list[PyTimelineFX]:
        raise RuntimeError("cannot read the effects")

    @effects.setter
    def effects(self, value: Any) -> None:
        pass


def bg(rig: Rig) -> PyBatch:
    batch: PyBatch = rig.handles["bg1"]
    return batch


def give_fx(clip: PyClip, *types: str, segment_class: type[PySegment] = PySegment) -> None:
    effects = [PyTimelineFX(t) for t in types]
    clip._versions = [PyVersion([PyTrack([segment_class(effects=effects)])])]


def make_group(rig: Rig) -> tuple[PyClip, PyClip]:
    """SrcA (schematic) holds a copy of clip1, the shelf reel Shelf1 a copy of clip2."""
    batch = bg(rig)
    src = batch.create_reel("SrcA")
    shelf = batch.create_shelf_reel("Shelf1")
    assert src is not None and shelf is not None
    (on_reel,) = src.receive([rig.flame.find_by_wiretap_node_id(CLIP1_ID)])
    (on_shelf,) = shelf.receive([rig.flame.find_by_wiretap_node_id(CLIP2_ID)])
    return on_reel, on_shelf


def record_of(rig: Rig, checkpoint_id: str) -> dict[str, Any]:
    [record] = [r for r in rig.records() if r["type"] == "checkpoint" and r["id"] == checkpoint_id]
    return record


def test_a_checkpoint_records_the_fx_types_of_every_reel_clip(rig: Rig) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur", "Timewarp")
    saved = checkpoint(rig)
    record = record_of(rig, saved["checkpoint"])
    assert record["clipFx"] == [
        {
            "reelKind": "schematic",
            "reel": "SrcA",
            "clip": "clip1",
            "effects": ["Blur", "Timewarp"],
            "error": None,
        },
        {
            "reelKind": "shelf",
            "reel": "Shelf1",
            "clip": "clip2",
            "effects": [],
            "error": None,
        },
    ]
    assert record["clipFxError"] is None
    assert "clipFx" not in saved


def test_an_unreadable_clip_is_recorded_with_its_error_and_the_checkpoint_still_succeeds(
    rig: Rig,
) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur", segment_class=UnreadableFxSegment)
    saved = checkpoint(rig)
    [row] = [r for r in record_of(rig, saved["checkpoint"])["clipFx"] if r["clip"] == "clip1"]
    assert row["effects"] is None
    assert "cannot read the effects" in row["error"]


def test_a_clip_that_keeps_its_fx_through_the_restore_is_not_reported(rig: Rig) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Timewarp")
    saved = checkpoint(rig)
    bg(rig).create_node("Mux")
    env = restore(rig, saved)
    assert env["ok"] is True, env
    assert env["result"]["timelineFxLost"] == []
    assert not [w for w in env["result"].get("warnings", []) if "Timeline FX" in w]
    assert env["result"]["matched"] is True


def test_a_clip_that_loses_its_fx_in_the_restore_is_reported_and_noted_without_failing_it(
    rig: Rig,
) -> None:
    on_reel, on_shelf = make_group(rig)
    give_fx(on_reel, "Blur", "Timewarp")
    give_fx(on_shelf, "Timewarp")
    on_reel.fx_lost_on_load = True
    saved = checkpoint(rig)
    bg(rig).create_node("Mux")
    env = restore(rig, saved)
    assert env["ok"] is True and env["errorCode"] is None, env
    assert env["result"]["matched"] is True
    assert env["result"]["timelineFxLost"] == [
        {
            "reelKind": "schematic",
            "reel": "SrcA",
            "clip": "clip1",
            "before": ["Blur", "Timewarp"],
            "after": [],
        }
    ]
    [note] = [w for w in env["result"]["warnings"] if "Timeline FX" in w]
    assert "batch.add_clips" in note and "1 clip" in note
    assert note.isascii()


def test_a_revert_reports_the_lost_fx_too(rig: Rig) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur")
    on_reel.fx_lost_on_load = True
    env = rig.do("batch.organize", {"target": BG1})
    assert env["ok"] is True, env
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert [r["clip"] for r in back["result"]["timelineFxLost"]] == ["clip1"]
    assert any("Timeline FX" in w for w in back["result"]["warnings"])


def test_a_checkpoint_without_the_fx_record_gives_null_and_says_why(rig: Rig) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur")
    on_reel.fx_lost_on_load = True
    saved = checkpoint(rig)
    strip_from_records(rig, saved["checkpoint"], clipFx=..., clipFxError=...)
    env = restore(rig, saved)
    assert env["ok"] is True, env
    assert env["result"]["timelineFxLost"] is None
    assert env["result"]["timelineFxNotCompared"] == timeline_fx_loss.NO_RECORD_REASON
    load = next(c for c in rig.exec.calls if c.label == "batch.load_setup")
    assert "_clip_fx_read(" not in load.code.split("__op_src = ")[1]


def test_a_checkpoint_whose_fx_could_not_be_read_gives_null_and_says_why(rig: Rig) -> None:
    make_group(rig)
    saved = checkpoint(rig)
    strip_from_records(rig, saved["checkpoint"], clipFx=None, clipFxError="RuntimeError: nope")
    env = restore(rig, saved)
    assert env["ok"] is True, env
    assert env["result"]["timelineFxLost"] is None
    assert env["result"]["timelineFxNotCompared"].endswith("(RuntimeError: nope)")


def test_the_fx_read_after_the_load_runs_in_the_same_flame_call(rig: Rig) -> None:
    make_group(rig)
    saved = checkpoint(rig)
    rig.exec.calls.clear()
    restore(rig, saved)
    loads = [c for c in rig.exec.calls if c.label == "batch.load_setup"]
    assert len(loads) == 1
    assert "_result['clipFx'] = _clip_fx_read(target)" in loads[0].code


def test_the_raw_fx_read_is_not_left_in_the_load_result(rig: Rig) -> None:
    make_group(rig)
    saved = checkpoint(rig)
    env = restore(rig, saved)
    assert "clipFx" not in env["result"]["load"]


def test_batch_load_setup_of_a_file_does_not_report_fx(rig: Rig) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur")
    on_reel.fx_lost_on_load = True
    saved = checkpoint(rig)
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["ok"] is True, env
    assert "timelineFxLost" not in env["result"]


def row(kind: str, reel: str, clip: str, effects: list[str] | None) -> dict[str, Any]:
    return {
        "reelKind": kind,
        "reel": reel,
        "clip": clip,
        "effects": effects,
        "error": None,
    }


def lost_of(recorded: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = timeline_fx_loss.compare(recorded, after)
    assert found is not None
    return found.lost


def test_compare_reports_a_clip_that_kept_only_some_of_its_fx() -> None:
    recorded = [row("schematic", "A", "c", ["Blur", "Timewarp"])]
    after = [row("schematic", "A", "c", ["Timewarp"])]
    assert lost_of(recorded, after) == [
        {
            "reelKind": "schematic",
            "reel": "A",
            "clip": "c",
            "before": ["Blur", "Timewarp"],
            "after": ["Timewarp"],
        }
    ]


def test_compare_keeps_the_reel_kind_of_a_shelf_reel_clip() -> None:
    recorded = [row("shelf", "Shelf1", "c", ["Blur"])]
    after = [row("shelf", "Shelf1", "c", [])]
    [entry] = lost_of(recorded, after)
    assert entry["reelKind"] == "shelf" and entry["reel"] == "Shelf1"


def test_compare_pairs_clips_of_the_same_name_by_their_order_in_the_reel() -> None:
    recorded = [row("schematic", "A", "c", ["Blur"]), row("schematic", "A", "c", [])]
    after = [row("schematic", "A", "c", ["Blur"]), row("schematic", "A", "c", [])]
    assert lost_of(recorded, after) == []
    swapped = [row("schematic", "A", "c", []), row("schematic", "A", "c", ["Blur"])]
    assert [r["before"] for r in lost_of(recorded, swapped)] == [["Blur"]]


def test_compare_does_not_report_more_fx_or_a_reorder_as_lost() -> None:
    recorded = [
        row("schematic", "A", "more", ["Blur"]),
        row("schematic", "A", "order", ["Blur", "Timewarp"]),
    ]
    after = [
        row("schematic", "A", "more", ["Blur", "Timewarp"]),
        row("schematic", "A", "order", ["Timewarp", "Blur"]),
    ]
    found = timeline_fx_loss.compare(recorded, after)
    assert found is not None and found.lost == [] and found.unchecked == []


def test_compare_lists_the_rows_it_could_not_judge_instead_of_dropping_them() -> None:
    recorded = [
        row("schematic", "A", "gone", ["Blur"]),
        row("shelf", "S", "unread", None),
        row("schematic", "A", "unread_after", ["Blur"]),
        row("schematic", "A", "fine", ["Blur"]),
    ]
    after = [
        row("shelf", "S", "unread", []),
        row("schematic", "A", "unread_after", None),
        row("schematic", "A", "fine", ["Blur"]),
    ]
    found = timeline_fx_loss.compare(recorded, after)
    assert found is not None and found.lost == []
    assert found.unchecked == [
        {
            "reelKind": "schematic",
            "reel": "A",
            "clip": "gone",
            "reason": timeline_fx_loss.MISSING_REASON,
        },
        {"reelKind": "shelf", "reel": "S", "clip": "unread", "reason": "unreadable"},
        {"reelKind": "schematic", "reel": "A", "clip": "unread_after", "reason": "unreadable"},
    ]
    assert timeline_fx_loss.MISSING_REASON == "missing after the restore"


def test_compare_is_null_without_a_record_or_without_the_read_after() -> None:
    rows = [row("schematic", "A", "c", ["Blur"])]
    assert timeline_fx_loss.compare(None, rows) is None
    assert timeline_fx_loss.compare(rows, None) is None


def test_report_is_null_for_both_keys_when_nothing_could_be_compared() -> None:
    fx = timeline_fx_loss.FxRecord([row("schematic", "A", "c", ["Blur"])], None)
    out = timeline_fx_loss.report(fx, {"clipFx": {"rows": None, "error": "boom"}})
    assert out["timelineFxLost"] is None and out["timelineFxUnchecked"] is None
    assert out["timelineFxNotCompared"] == f"{timeline_fx_loss.READ_FAILED_REASON} (boom)"


def test_report_says_so_when_the_load_gave_no_answer() -> None:
    fx = timeline_fx_loss.FxRecord([row("schematic", "A", "c", ["Blur"])], None)
    out = timeline_fx_loss.report(fx, None)
    assert out["timelineFxLost"] is None and out["timelineFxUnchecked"] is None
    assert "load_setup did not complete" in out["timelineFxNotCompared"]


# Faults that appear only while the restore's own load call reads the clips again.


def arm_after_load(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """`armed["on"]` turns True once `load_setup` has run inside a Flame call and False when the
    next call starts, so a fault keyed on it hits the read taken right after the load only."""
    armed = {"on": False}
    original = PyBatch.load_setup

    def load_then_arm(self: PyBatch, path: str) -> Any:
        result = original(self, path)
        armed["on"] = True
        return result

    monkeypatch.setattr(PyBatch, "load_setup", load_then_arm)
    rig.exec.before_run = lambda label: armed.update(on=False)
    return armed


def test_a_clip_whose_fx_cannot_be_read_after_the_restore_is_unchecked_not_clean(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Timewarp")
    saved = checkpoint(rig)
    armed = arm_after_load(rig, monkeypatch)
    versions = PyClip.versions

    def versions_or_fail(self: PyClip) -> Any:
        if armed["on"] and self.name.get_value() == "clip1":
            raise RuntimeError("cannot read the versions")
        return versions.fget(self)  # type: ignore[attr-defined]

    monkeypatch.setattr(PyClip, "versions", property(versions_or_fail))
    env = restore(rig, saved)
    assert env["ok"] is True, env
    assert env["result"]["timelineFxLost"] == []
    assert env["result"]["timelineFxUnchecked"] == [
        {"reelKind": "schematic", "reel": "SrcA", "clip": "clip1", "reason": "unreadable"}
    ]
    [note] = [w for w in env["result"]["warnings"] if "could not be compared" in w]
    assert "1 clip" in note and "result.timelineFxUnchecked" in note and note.isascii()


def test_a_clip_missing_after_the_restore_is_unchecked_not_clean(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_group(rig)
    saved = checkpoint(rig)
    armed = arm_after_load(rig, monkeypatch)
    clips = PyReel.clips

    def clips_without_clip2(self: PyReel) -> Any:
        found = clips.fget(self)  # type: ignore[attr-defined]
        if not armed["on"]:
            return found
        return [c for c in found if c.name.get_value() != "clip2"]

    monkeypatch.setattr(PyReel, "clips", property(clips_without_clip2))
    env = restore(rig, saved)
    assert env["ok"] is True, env
    assert env["result"]["timelineFxLost"] == []
    assert env["result"]["timelineFxUnchecked"] == [
        {
            "reelKind": "shelf",
            "reel": "Shelf1",
            "clip": "clip2",
            "reason": timeline_fx_loss.MISSING_REASON,
        }
    ]


def test_a_restore_with_nothing_unchecked_has_an_empty_list_and_no_such_warning(rig: Rig) -> None:
    make_group(rig)
    saved = checkpoint(rig)
    env = restore(rig, saved)
    assert env["result"]["timelineFxUnchecked"] == []
    assert not [w for w in env["result"].get("warnings", []) if "could not be compared" in w]


def test_a_read_after_the_load_that_fails_gives_null_and_says_why(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur")
    saved = checkpoint(rig)
    armed = arm_after_load(rig, monkeypatch)

    def shelf_reels_or_fail(self: PyBatch) -> Any:
        if armed["on"]:
            raise RuntimeError("shelf reels are gone")
        return self.__dict__["shelf_reels"]

    monkeypatch.setattr(PyBatch, "shelf_reels", property(shelf_reels_or_fail), raising=False)
    env = restore(rig, saved)
    assert env["ok"] is True, env
    assert env["result"]["timelineFxLost"] is None
    assert env["result"]["timelineFxUnchecked"] is None
    reason = env["result"]["timelineFxNotCompared"]
    assert reason.startswith(timeline_fx_loss.READ_FAILED_REASON)
    assert "shelf reels are gone" in reason


def test_a_load_that_fails_gives_null_and_says_the_load_did_not_complete(rig: Rig) -> None:
    on_reel, _ = make_group(rig)
    give_fx(on_reel, "Blur")
    saved = checkpoint(rig)
    rig.exec.fault("batch.load_setup", "FLAME_API_ERROR", when="before")
    env = restore(rig, saved)
    assert env["ok"] is False, env
    assert env["result"]["timelineFxLost"] is None
    assert env["result"]["timelineFxUnchecked"] is None
    assert "load_setup did not complete" in env["result"]["timelineFxNotCompared"]


# The read stops at its time budget, so it cannot use up the Flame call's limit.


def slow_clock(
    monkeypatch: pytest.MonkeyPatch, armed: dict[str, bool], seconds_per_clip: float
) -> None:
    offset = [0.0]
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + offset[0])
    versions = PyClip.versions

    def slow_versions(self: PyClip) -> Any:
        if armed["on"]:
            offset[0] += seconds_per_clip
        return versions.fget(self)  # type: ignore[attr-defined]

    monkeypatch.setattr(PyClip, "versions", property(slow_versions))


def test_a_fx_read_over_its_time_budget_stops_and_the_restore_still_succeeds(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_reel, on_shelf = make_group(rig)
    give_fx(on_reel, "Blur")
    give_fx(on_shelf, "Blur")
    saved = checkpoint(rig)
    armed = arm_after_load(rig, monkeypatch)
    slow_clock(monkeypatch, armed, CLIP_FX_READ_BUDGET_S + 1)
    env = restore(rig, saved)
    assert env["ok"] is True and env["errorCode"] is None, env
    assert env["result"]["matched"] is True
    assert env["result"]["timelineFxLost"] is None
    assert env["result"]["timelineFxUnchecked"] is None
    reason = env["result"]["timelineFxNotCompared"]
    assert f"time budget of {CLIP_FX_READ_BUDGET_S} s exceeded after 1 clips" in reason


def test_a_checkpoint_whose_fx_read_goes_over_the_budget_still_succeeds_and_says_so(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_group(rig)
    slow_clock(monkeypatch, {"on": True}, CLIP_FX_READ_BUDGET_S + 1)
    saved = checkpoint(rig)
    record = record_of(rig, saved["checkpoint"])
    assert record["clipFx"] is None
    assert "time budget" in record["clipFxError"]
