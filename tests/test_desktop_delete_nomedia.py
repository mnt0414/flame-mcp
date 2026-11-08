"""`desktop.delete` refuses, before anything is copied, a clip that holds only 'Gap Batch FX'
segments (and 'Gap'): the stash copy of it raises 'Invalid source entries supplied.' on Flame
2025.2.4. `without_backup` deletes it with no copy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyClip, PySegment, PyTrack, PyVersion, adopt
from journal_test_helpers import Rig
from p32b_helpers import CLIPX, delete, make_p32b_rig, wt
from timeline_p32a_helpers import (
    add_sequence,
    checkpoints,
    media,
    reel_names,
    stash_entries,
    stash_library,
)

DELETE = "desktop.delete"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def seg(kind: str, record_in: int = 0, record_out: int = 23) -> PySegment:
    return PySegment(
        name="",
        source_name="",
        record_in=record_in,
        record_out=record_out,
        source_in=0,
        source_out=record_out - record_in,
        record_duration=record_out - record_in + 1,
        type=kind,
    )


def add_clip_of(rig: Rig, name: str, segments: list[PySegment]) -> dict[str, Any]:
    reel = rig.handles["reel"]
    clip = PyClip(
        name,
        wiretap_id=f"/wt/RG/Reel 1/{name}",
        duration=24,
        versions=[PyVersion([PyTrack(segments)])],
    )
    adopt(reel, [clip])
    reel._children.append(clip)
    return wt(clip.get_wiretap_node_id())


def nothing_copied_or_journaled(rig: Rig) -> bool:
    return (
        stash_library(rig) is None
        and rig.records() == []
        and rig.flame.media_panel.copy_calls == []
    )


def test_a_clip_of_only_gap_batch_fx_is_refused_and_nothing_is_copied_or_journaled(
    rig: Rig,
) -> None:
    gbfx = add_clip_of(rig, "GBFX", [seg("Gap Batch FX")])
    env = delete(rig, [gbfx])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "Gap Batch FX" in env["error"] and "Invalid source entries supplied." in env["error"]
    assert "nothing was copied or deleted" in env["error"]
    assert env["result"]["ran"] is False
    [row] = env["result"]["unstashable"]
    assert row["index"] == 0 and row["name"] == "GBFX" and row["uid"]
    assert "GBFX" in reel_names(rig.handles["reel"])
    assert nothing_copied_or_journaled(rig)
    assert "without_backup" in str(env.get("hint"))


def test_the_same_clip_named_in_without_backup_is_deleted_with_no_stash(rig: Rig) -> None:
    gbfx = add_clip_of(rig, "GBFX", [seg("Gap Batch FX")])
    env = rig.do(DELETE, {"targets": [gbfx], "without_backup": [gbfx]})
    assert env["ok"] is True, env
    assert "GBFX" not in reel_names(rig.handles["reel"])
    assert env["result"]["deleted"][0]["withoutBackup"] is True
    assert stash_library(rig) is None and checkpoints(rig) == []


def test_a_mix_refuses_the_whole_call_and_copies_nothing(rig: Rig) -> None:
    gbfx = add_clip_of(rig, "GBFX", [seg("Gap Batch FX")])
    env = delete(rig, [CLIPX, gbfx])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert [r["name"] for r in env["result"]["unstashable"]] == ["GBFX"]
    assert env["result"]["unstashable"][0]["index"] == 1
    assert "CLIPX" in reel_names(rig.handles["reel"])
    assert nothing_copied_or_journaled(rig)


def test_a_clip_with_media_and_a_gap_batch_fx_segment_is_stashed_as_before(rig: Rig) -> None:
    mixed = add_clip_of(
        rig, "MIXED", [seg("Gap Batch FX", 0, 11), media("clip1", 12, 23, name="m")]
    )
    env = delete(rig, [mixed])
    assert env["ok"] is True, env
    assert [str(e.name.get_value()) for e in stash_entries(rig)] == ["MIXED"]


def test_a_clip_of_only_plain_gap_segments_is_not_flagged(rig: Rig) -> None:
    plain = add_clip_of(rig, "PLAINGAP", [seg("Gap")])
    env = delete(rig, [plain])
    assert env["ok"] is True, env
    assert [str(e.name.get_value()) for e in stash_entries(rig)] == ["PLAINGAP"]


def test_gap_and_gap_batch_fx_together_are_flagged(rig: Rig) -> None:
    both = add_clip_of(rig, "BOTH", [seg("Gap", 0, 11), seg("Gap Batch FX", 12, 23)])
    env = delete(rig, [both])
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["ran"] is False


def test_two_flagged_clips_one_named_in_without_backup_still_refuse_for_the_other(
    rig: Rig,
) -> None:
    first = add_clip_of(rig, "GB1", [seg("Gap Batch FX")])
    second = add_clip_of(rig, "GB2", [seg("Gap Batch FX")])
    env = rig.do(DELETE, {"targets": [first, second], "without_backup": [first]})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert [r["name"] for r in env["result"]["unstashable"]] == ["GB2"]
    assert "GB2" not in env["error"] and "1 clip(s)" in env["error"]
    assert nothing_copied_or_journaled(rig)


class UnreadableType(PySegment):
    @property
    def type(self) -> str:
        raise RuntimeError("type boom")

    @type.setter
    def type(self, value: str) -> None:
        pass


def test_a_segment_type_that_cannot_be_read_does_not_flag_the_clip(rig: Rig) -> None:
    bad = UnreadableType(name="", source_name="", record_in=0, record_out=23)
    clip = add_clip_of(rig, "UNREAD", [bad])
    env = delete(rig, [clip])
    # not refused by this rule: the call went on to the stash phase (where the fake's own copy
    # also reads the type and raises, so the call ends there)
    assert "unstashable" not in str(env.get("result")), env
    assert "type boom" in env["error"] and "nothing was deleted" in env["error"]
    assert "UNREAD" in reel_names(rig.handles["reel"])


class UnreadableSourceIn(PySegment):
    @property
    def source_in(self) -> Any:
        raise RuntimeError("source_in boom")

    @source_in.setter
    def source_in(self, value: Any) -> None:
        pass


def test_a_gap_batch_fx_clip_with_another_attribute_unreadable_is_still_refused(
    rig: Rig,
) -> None:
    odd = UnreadableSourceIn(
        name="", source_name="", record_in=0, record_out=23, type="Gap Batch FX"
    )
    clip = add_clip_of(rig, "ODD", [odd])
    env = delete(rig, [clip])
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["ran"] is False, env
    assert [r["name"] for r in env["result"]["unstashable"]] == ["ODD"]
    assert nothing_copied_or_journaled(rig)


def test_a_sequence_of_only_gap_batch_fx_is_not_refused_by_this_rule(rig: Rig) -> None:
    add_sequence(
        rig.handles["reel"], "SGBFX", [[seg("Gap Batch FX")]], wiretap="/wt/RG/Reel 1/SGBFX"
    )
    env = delete(rig, [wt("/wt/RG/Reel 1/SGBFX")])
    assert env["ok"] is True, env
    assert "SGBFX" not in reel_names(rig.handles["reel"])
    assert [str(e.name.get_value()) for e in stash_entries(rig)] == ["SGBFX"]
