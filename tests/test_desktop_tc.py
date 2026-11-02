"""`desktop.sync_record_tc`, `desktop.set_record_tc` and `desktop.get_record_tc`.

Rules are `tc_rules.judge` (pure); the ops run through the real Runtime over the fake Flame tree.
In the fake a clip's single segment starts at the clip's `start_time` (as flame-tc-sync relies on:
`record_in` follows `start_time`); whether real Flame does so, and how it treats DPX, a Gap, or a
frame-rate difference, is measured on real Flame, not here."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr, PyClip, PySegment, PyTime, PyTrack, PyVersion, adopt
from flame_mcp.tc_rules import judge
from journal_test_helpers import Rig
from timeline_p32a_helpers import gap, make_timeline_rig

SYNC = "desktop.sync_record_tc"
RATE = "23.976 fps"


class StartAttr(FakeAttr):
    """`start_time`: writing it moves the clip's first segment (`record_in`)."""

    def __init__(self, clip: PyClip, frame: int) -> None:
        super().__init__(PyTime(frame, RATE))
        self.clip = clip

    def set_value(self, value: Any) -> None:
        super().set_value(value)
        first = self.clip._versions[0].tracks[-1].segments[0]
        if first.type != "Gap":
            first.record_in = PyTime(value.frame, RATE)


StartAttr.__name__ = "PyAttribute"  # `_v` unwraps by the type's name


def make_clip(
    rig: Rig,
    name: str,
    *,
    record: int,
    source: int,
    start_frame: int = 1,
    path: str | None = "/media/a.mov",
    source_rate: str = RATE,
    extra: list[PySegment] | None = None,
    gap_first: bool = False,
) -> dict[str, Any]:
    seg = PySegment(
        name=name,
        source_name=name,
        record_in=record,
        record_out=record + 9,
        source_in=source,
        source_out=source + 9,
        source_frame_rate=source_rate,
    )
    seg.file_path = path
    segments = [gap(record - 10, record - 1), seg] if gap_first else [seg]
    clip = PyClip(
        name,
        wiretap_id=f"/wt/RG/Reel 1/{name}",
        duration=10,
        start_frame=start_frame,
        versions=[PyVersion([PyTrack([*segments, *(extra or [])])])],
    )
    clip.start_time = StartAttr(clip, record)
    clip.current_time = FakeAttr(PyTime(record, RATE))
    reel = rig.handles["reel"]
    adopt(reel, [clip])
    reel._children.append(clip)
    return {"kind": "wiretap", "id": f"/wt/RG/Reel 1/{name}"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def clip_of(rig: Rig, name: str) -> PyClip:
    clip: PyClip = next(c for c in rig.handles["reel"]._children if c.name.get_value() == name)
    return clip


def record_in(rig: Rig, name: str) -> int:
    return int(clip_of(rig, name)._versions[0].tracks[0].segments[0].record_in.frame)


def rows_by_name(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in env["result"]["rows"]}


def test_a_clip_whose_record_differs_is_written_to_the_source_tc(rig: Rig) -> None:
    target = make_clip(rig, "A", record=100, source=500)
    env = rig.do(SYNC, {"targets": [target]})
    assert env["ok"] is True, (env["error"], env["result"]["rows"])
    row = rows_by_name(env)["A"]
    assert row["status"] == "synced"
    assert row["before"]["recordTcFrame"] == 100 and row["after"]["recordTcFrame"] == 500
    assert record_in(rig, "A") == 500
    assert clip_of(rig, "A").current_time.get_value().frame == 500
    assert env["affected"] == 1 and env["journal"]["revert"]["args"] == {
        "group": env["journal"]["group"]
    }


def test_several_clips_in_one_call_each_have_before_and_after(rig: Rig) -> None:
    targets = [
        make_clip(rig, "A", record=100, source=500),
        make_clip(rig, "B", record=7, source=7),
        make_clip(rig, "C", record=3, source=40),
    ]
    env = rig.do(SYNC, {"targets": targets})
    rows = rows_by_name(env)
    assert [rows[n]["status"] for n in "ABC"] == ["synced", "in_sync", "synced"]
    assert rows["B"]["after"] is None
    assert env["result"]["counts"] == {"synced": 2, "in_sync": 1}


def test_dry_run_reads_and_writes_nothing(rig: Rig) -> None:
    target = make_clip(rig, "A", record=100, source=500)
    env = rig.do(SYNC, {"targets": [target], "dry_run": True})
    assert env["ok"] is True and env["result"]["rows"][0]["status"] == "sync"
    assert record_in(rig, "A") == 100
    assert rig.records() == []


def test_revert_puts_the_earlier_record_tc_back(rig: Rig) -> None:
    targets = [
        make_clip(rig, "A", record=100, source=500),
        make_clip(rig, "C", record=3, source=40),
    ]
    env = rig.do(SYNC, {"targets": targets})
    reverted = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert reverted["ok"] is True, reverted
    assert (record_in(rig, "A"), record_in(rig, "C")) == (100, 3)
    assert clip_of(rig, "A").current_time.get_value().frame == 100


def test_revert_is_a_conflict_when_the_record_tc_changed_since(rig: Rig) -> None:
    target = make_clip(rig, "A", record=100, source=500)
    env = rig.do(SYNC, {"targets": [target]})
    clip_of(rig, "A").start_time.set_value(PyTime(9, RATE))
    reverted = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert reverted["ok"] is False
    assert record_in(rig, "A") == 9


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"path": "/media/a.0001.dpx"}, "dpx_source"),
        ({"source": 0, "start_frame": 86500}, "no_source_tc"),
        ({"source_rate": "25 fps"}, "frame_rate_mismatch"),
        ({"gap_first": True}, "gap_at_head"),
    ],
)
def test_risky_material_is_skipped_with_a_reason_and_not_written(
    rig: Rig, kwargs: dict[str, Any], reason: str
) -> None:
    kwargs = {"record": 100, "source": 500, **kwargs}
    target = make_clip(rig, "A", **kwargs)
    env = rig.do(SYNC, {"targets": [target]})
    assert env["ok"] is True and env["affected"] == 0
    row = rows_by_name(env)["A"]
    assert (row["status"], row["reason"]) == ("skipped", reason)
    assert clip_of(rig, "A").start_time.get_value().frame == 100
    assert rig.records() == []


def test_a_skipped_row_does_not_stop_the_others(rig: Rig) -> None:
    targets = [
        make_clip(rig, "A", record=100, source=500, path="/media/a.0001.dpx"),
        make_clip(rig, "B", record=100, source=500),
    ]
    env = rig.do(SYNC, {"targets": targets})
    assert env["ok"] is True
    assert [r["status"] for r in env["result"]["rows"]] == ["skipped", "synced"]


def test_include_risky_writes_dpx_and_flags_the_row(rig: Rig) -> None:
    target = make_clip(rig, "A", record=100, source=500, path="/media/a.0001.dpx")
    env = rig.do(SYNC, {"targets": [target], "include_risky": True})
    row = rows_by_name(env)["A"]
    assert env["ok"] is True and row["status"] == "synced" and row["risky"] is True


def test_a_sequence_and_a_clip_listed_twice_are_skipped(rig: Rig) -> None:
    target = make_clip(rig, "A", record=100, source=500)
    seq = {"kind": "wiretap", "id": "/wt/RG/Reel 1/S1"}
    env = rig.do(SYNC, {"targets": [target, target, seq]})
    assert [r["reason"] for r in env["result"]["rows"]] == [None, "duplicate", "not_a_clip"]


def test_a_write_that_fails_stops_the_call_and_what_was_written_is_journaled(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = [
        make_clip(rig, "A", record=100, source=500),
        make_clip(rig, "B", record=3, source=40),
    ]
    second = clip_of(rig, "B")

    def boom(value: Any) -> None:
        raise RuntimeError("refused")

    monkeypatch.setattr(second.start_time, "set_value", boom)
    env = rig.do(SYNC, {"targets": targets})
    assert env["ok"] is False and env["error"].startswith("partial:")
    assert [r["status"] for r in env["result"]["rows"]] == ["synced", "failed"]
    assert env["partial"] == {"synced": 1, "notSynced": 1}
    assert env["journal"]["revert"] is not None


def test_set_record_tc_writes_and_reverts_by_itself(rig: Rig) -> None:
    target = make_clip(rig, "A", record=100, source=500)
    env = rig.do("desktop.set_record_tc", {"target": target, "start_time_frame": 42})
    assert env["ok"] is True, env
    assert record_in(rig, "A") == 42
    assert rig.do("journal.revert", {"group": env["journal"]["group"]})["ok"] is True
    assert record_in(rig, "A") == 100


def test_get_record_tc_reads_the_frames(rig: Rig) -> None:
    target = make_clip(rig, "A", record=100, source=500)
    env = rig.do("desktop.get_record_tc", {"target": target, "keys": ["start_time_frame"]})
    assert env["result"]["attributes"] == {"start_time_frame": {"value": 100}}


@pytest.mark.parametrize(
    ("facts", "status", "reason"),
    [
        ({"location": "library"}, "skipped", "in_library"),
        ({"location": "batch"}, "skipped", "not_on_desktop"),
        ({"versionCount": 2}, "skipped", "multiple_versions"),
        ({"segmentCount": 3}, "skipped", "multi_segment"),
        ({"sourceFrameRate": None}, "skipped", "frame_rate_unreadable"),
        ({"recordInFrame": None}, "skipped", "unreadable"),
        ({"recordInFrame": 500}, "in_sync", None),
        ({}, "sync", None),
    ],
)
def test_judge(facts: dict[str, Any], status: str, reason: str | None) -> None:
    base = {
        "type": "PyClip",
        "location": "desktop",
        "versionCount": 1,
        "segmentCount": 1,
        "firstSegmentType": "Video Segment",
        "recordInFrame": 100,
        "sourceInFrame": 500,
        "frameRate": RATE,
        "sourceFrameRate": RATE,
        "startFrame": 1,
        "sourceExtension": "mov",
    }
    verdict = judge({**base, **facts})
    assert (verdict["status"], verdict["reason"]) == (status, reason)
