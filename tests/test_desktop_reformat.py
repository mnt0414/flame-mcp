"""Tests for `desktop.reformat`: the write, its refusals and its read-back.

Clips on the Desktop are reformatted in place (checkpointed first) or, with duplicate=true, on a
copy; every target is checked before anything is written, one Flame call per clip does the work,
and the result is judged by reading the clip back. Run through the real Runtime with a scripted
`transport.exec` over the fake Flame tree. The journal, revert and check are in
test_desktop_reformat_journal.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp.desktop_reformat_state import REFORMAT_WAIT_S, STASH_BATCH
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from reformat_helpers import (
    CLIPX,
    LIB_CLIP1,
    OP,
    REEL1,
    S1,
    add_media_clip,
    calls_of,
    checkpoints,
    clip_format,
    first_record,
    make_reformat_rig,
    reel,
    reel_names,
    reformat,
    stash_entries,
    touched_clips,
    uid,
    untouched_world,
    wt,
)
from timeline_p32a_helpers import fingerprint_of_target


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_reformat_rig(monkeypatch, tmp_path)


def test_reformats_a_clip_in_place_and_reads_the_new_size_back(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    old_uid = uid(clip)
    count = len(reel_names(reel(rig)))
    env = reformat(rig)
    assert env["ok"] is True, env
    assert (clip.width, clip.height) == (960, 540)
    assert uid(clip) != old_uid and len(reel_names(reel(rig))) == count
    row = env["result"]["reformatted"][0]
    assert row["from"]["uid"] == old_uid and row["from"]["name"] == "CLIPX"
    assert row["to"]["uid"] == uid(clip) and row["to"]["selector"] == wt(clip.get_wiretap_node_id())
    assert (row["before"]["width"], row["before"]["height"]) == (1920, 1080)
    assert (row["after"]["width"], row["after"]["height"]) == (960, 540)
    assert row["after"]["ratio"] == pytest.approx(960 / 540)
    assert row["changed"] is True and row["skipped"] is False
    assert env["result"]["mode"] == "in_place" and env["result"]["partial"] is False
    assert env["result"]["requested"] == 1 and env["result"]["notReformatted"] == []
    assert env["affected"] == 1 and env["undoSteps"] == 0
    assert env["journal"]["revert"] == {
        "op": "journal.revert",
        "args": {"group": env["journal"]["group"]},
    }


def test_reports_before_and_after_for_every_attribute(rig: Rig) -> None:
    env = reformat(rig, [wt(rig.handles["b"].get_wiretap_node_id())], 1920, 1080, bit_depth=10)
    row = env["result"]["reformatted"][0]
    assert row["before"] == {
        "width": 3840,
        "height": 2160,
        "bitDepth": 16,
        "ratio": pytest.approx(1.7777777777777777),
        "scanMode": "P",
        "frameRate": "23.976 fps",
        "startFrame": 1,
        "duration": 100,
    }
    assert row["after"]["width"] == 1920 and row["after"]["bitDepth"] == 10
    assert row["after"]["scanMode"] == "P" and row["after"]["startFrame"] == 1


def test_the_call_uses_the_preset_defaults_fill_and_a_ratio_of_width_over_height(
    rig: Rig,
) -> None:
    env = reformat(rig, width=1280, height=720)
    clip = rig.handles["clipx"]
    assert clip.reformat_calls == [
        {
            "width": 1280,
            "height": 720,
            "ratio": round(1280 / 720, 6),
            "bit_depth": 0,
            "scan_mode": "",
            "frame_rate": "",
            "resize_mode": "Fill",
        }
    ]
    assert env["result"]["reformatted"][0]["applied"] == {
        "width": 1280,
        "height": 720,
        "ratio": round(1280 / 720, 6),
        "resize_mode": "Fill",
    }
    assert clip.start_frame_calls == []


def test_every_optional_argument_reaches_flame_and_is_read_back(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    env = reformat(
        rig,
        resize_mode="Crop Edges",
        bit_depth=16,
        ratio=1.7778,
        scan_mode="F1",
        frame_rate="25 fps",
    )
    assert env["ok"] is True, env
    assert clip.reformat_calls == [
        {
            "width": 960,
            "height": 540,
            "ratio": 1.7778,
            "bit_depth": 16,
            "scan_mode": "F1",
            "frame_rate": "25 fps",
            "resize_mode": "Crop Edges",
        }
    ]
    assert clip_format(clip) == {
        "width": 960,
        "height": 540,
        "ratio": 1.7778,
        "bit_depth": 16,
        "scan_mode": "F1",
        "frame_rate": "25 fps",
        "start_frame": 1,
    }
    row = env["result"]["reformatted"][0]
    assert row["applied"]["bit_depth"] == 16 and row["applied"]["frame_rate"] == "25 fps"
    assert row["after"]["scanMode"] == "F1" and row["after"]["frameRate"] == "25 fps"
    assert env["degraded"] == []


def test_a_frame_rate_flame_refuses_is_a_flame_error_and_changes_nothing(rig: Rig) -> None:
    env = reformat(rig, frame_rate="12.5 fps")
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "frame_rate" in env["error"]
    assert clip_format(rig.handles["clipx"])["width"] == 1920


def test_start_frame_is_applied_with_change_start_frame_and_read_back(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    env = reformat(rig, start_frame=1001)
    assert env["ok"] is True, env
    assert clip.start_frame_calls == [(1001, True)] and clip.start_frame == 1001
    row = env["result"]["reformatted"][0]
    assert row["before"]["startFrame"] == 1 and row["after"]["startFrame"] == 1001
    assert row["applied"]["start_frame"] == 1001
    assert row["applied"]["use_segment_connections"] is True


def test_use_segment_connections_false_reaches_change_start_frame(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    reformat(rig, start_frame=500, use_segment_connections=False)
    assert clip.start_frame_calls == [(500, False)]


def test_use_segment_connections_means_nothing_without_a_start_frame(rig: Rig) -> None:
    env = reformat(rig, use_segment_connections=False)
    assert env["ok"] is True and rig.handles["clipx"].start_frame_calls == []
    assert "use_segment_connections" not in env["result"]["reformatted"][0]["applied"]


def test_a_clip_already_at_the_requested_format_is_not_reformatted(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    env = reformat(rig, width=1920, height=1080)
    assert env["ok"] is True, env
    assert clip.reformat_calls == [] and clip.start_frame_calls == []
    row = env["result"]["reformatted"][0]
    assert row["skipped"] is True and row["changed"] is False
    assert row["before"] == row["after"] and env["affected"] == 0


def test_only_the_start_frame_is_changed_when_the_size_is_already_right(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    env = reformat(rig, width=1920, height=1080, start_frame=1001)
    assert env["ok"] is True, env
    assert clip.reformat_calls == [] and clip.start_frame_calls == [(1001, True)]
    row = env["result"]["reformatted"][0]
    assert row["changed"] is True and row["skipped"] is False


def test_duplicate_reformats_a_copy_and_leaves_the_original_alone(rig: Rig) -> None:
    original = rig.handles["clipx"]
    names = reel_names(reel(rig))
    env = reformat(rig, duplicate=True)
    assert env["ok"] is True, env
    assert (original.width, original.height) == (1920, 1080) and original.reformat_calls == []
    assert len(reel_names(reel(rig))) == len(names) + 1
    made = reel(rig).children[-1]
    assert made is not original and uid(made) != uid(original)
    assert (made.width, made.height) == (960, 540)
    row = env["result"]["reformatted"][0]
    assert env["result"]["mode"] == "duplicate"
    assert row["from"]["uid"] == uid(original) and row["to"]["uid"] == uid(made)
    assert row["to"]["selector"] == wt(made.get_wiretap_node_id())
    assert row["checkpoint"] is None and stash_entries(rig) == []
    assert (row["before"]["width"], row["after"]["width"]) == (1920, 960)
    assert rig.flame.duplicate_calls == [original]


def test_a_duplicate_call_counts_the_copies_it_made_as_affected(rig: Rig) -> None:
    env = reformat(rig, [CLIPX, wt(rig.handles["a"].get_wiretap_node_id())], duplicate=True)
    assert env["ok"] is True and env["affected"] == 2, env


def test_a_duplicate_of_a_clip_already_at_the_format_is_still_made(rig: Rig) -> None:
    count = reel_names(reel(rig)).count("CLIPX")
    env = reformat(rig, width=1920, height=1080, duplicate=True)
    made = reel(rig).children[-1]
    assert env["ok"] is True, env
    assert reel_names(reel(rig)).count("CLIPX") == count + 1
    assert made.reformat_calls == []
    assert env["result"]["reformatted"][0]["skipped"] is True and env["affected"] == 1


def test_duplicate_applies_the_start_frame_to_the_copy_only(rig: Rig) -> None:
    original = rig.handles["clipx"]
    env = reformat(rig, duplicate=True, start_frame=1001)
    made = reel(rig).children[-1]
    assert env["ok"] is True and made.start_frame == 1001 and original.start_frame == 1
    assert original.start_frame_calls == []


def test_several_targets_are_reformatted_one_flame_call_each(rig: Rig) -> None:
    a, b = rig.handles["a"], rig.handles["b"]
    env = reformat(rig, [CLIPX, wt(a.get_wiretap_node_id()), wt(b.get_wiretap_node_id())])
    assert env["ok"] is True, env
    assert [r["from"]["name"] for r in env["result"]["reformatted"]] == ["CLIPX", "A", "B"]
    assert [r["index"] for r in env["result"]["reformatted"]] == [0, 1, 2]
    assert [(c.width, c.height) for c in (rig.handles["clipx"], a, b)] == [(960, 540)] * 3
    ops = [c for c in rig.exec.calls if c.label == OP]
    assert len(ops) == 2 + 3  # one read, one stash, then one write per clip
    assert len(calls_of(rig, "snapshot")) == 1 and len(calls_of(rig, "stash")) == 1
    writes = calls_of(rig, "write")
    assert len(writes) == 3 and ops.index(writes[0]) == 2
    assert len(checkpoints(rig)) == 3 and len(stash_entries(rig)) == 3
    assert len({r["group"] for r in rig.records()}) == 1


def test_each_clips_flame_call_waits_up_to_the_long_wait(rig: Rig) -> None:
    reformat(rig, [CLIPX, wt(rig.handles["a"].get_wiretap_node_id())])
    timeouts = [c.timeout_s for c in calls_of(rig, "write")]
    assert len(timeouts) == 2 and all(t == REFORMAT_WAIT_S for t in timeouts)


def test_every_clip_reports_how_long_it_took(rig: Rig) -> None:
    env = reformat(rig, start_frame=1001)
    row = env["result"]["reformatted"][0]
    assert isinstance(row["seconds"], float) and row["seconds"] >= 0
    assert set(row["flameSeconds"]) == {"reformat", "changeStartFrame"}
    assert all(isinstance(v, float) for v in row["flameSeconds"].values())


def test_the_checkpoint_is_recorded_before_the_intent_and_the_intent_before_the_write(
    rig: Rig,
) -> None:
    reformat(rig)
    kinds = [r["type"] + ":" + str(r.get("kind", "")) for r in rig.records()]
    assert kinds == ["checkpoint:timeline", "intent:desktop_reformat", "outcome:"]
    write = calls_of(rig, "write")[0]
    assert write.journal_types == ["checkpoint", "intent"]
    stash_call = calls_of(rig, "stash")[0]
    assert stash_call.journal_types == []


def test_the_stash_holds_the_clip_as_it_was(rig: Rig) -> None:
    before = fingerprint_of_target(rig, CLIPX)
    reformat(rig)
    stash = stash_entries(rig)
    assert [str(e.name.get_value()) for e in stash] == ["CLIPX"]
    assert (stash[0].width, stash[0].height) == (1920, 1080)
    assert checkpoints(rig)[0]["fingerprint"] == before


def test_the_target_can_be_addressed_by_path_and_by_selection(rig: Rig) -> None:
    path = {"kind": "path", "path": ["RG", "Reel 1", "CLIPX"], "root": "desktop"}
    assert reformat(rig, [path])["ok"] is True
    rig.flame.media_panel.selected_entries = [rig.handles["a"]]
    assert reformat(rig, [{"kind": "selection"}])["ok"] is True
    assert (rig.handles["a"].width, rig.handles["clipx"].width) == (960, 960)


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"width": 961}, "even"),
        ({"height": 541}, "even"),
        ({"width": 22}, "24"),
        ({"height": 22}, "24"),
        ({"width": 16386}, "16384"),
        ({"height": 16386}, "16384"),
        ({"ratio": 0.001}, "0.01"),
        ({"ratio": 101}, "100"),
        ({"bit_depth": 9}, "bit_depth"),
        ({"scan_mode": "X"}, "scan_mode"),
        ({"resize_mode": "Zoom"}, "resize_mode"),
        ({"start_frame": 0}, "start_frame"),
        ({"start_frame": 1_000_000_000}, "999999999"),
        ({"frame_rate": ""}, "frame_rate"),
        ({"width": 960.5}, "integer"),
    ],
)
def test_an_invalid_argument_is_refused_before_anything_is_written(
    rig: Rig, fields: dict[str, Any], fragment: str
) -> None:
    env = reformat(rig, **fields)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert fragment in env["error"]
    assert rig.exec.calls == [] and untouched_world(rig)


def test_an_odd_size_is_not_rounded(rig: Rig) -> None:
    env = reformat(rig, width=1921, height=1080)
    assert env["errorCode"] == "INVALID_ARGS" and "1921" in env["error"]
    assert rig.handles["clipx"].width == 1920 and untouched_world(rig)


def test_a_non_ascii_frame_rate_is_refused_and_nothing_is_sent(rig: Rig) -> None:
    env = reformat(rig, frame_rate="24　fps")
    assert env["errorCode"] == "INVALID_ARGS" and "ASCII" in env["error"]
    assert rig.exec.calls == []


@pytest.mark.parametrize("count", [0, 51])
def test_the_number_of_targets_is_one_to_fifty(rig: Rig, count: int) -> None:
    targets = [wt(f"/wt/RG/Reel 1/none{i}") for i in range(count)]
    env = reformat(rig, targets)
    assert env["errorCode"] == "INVALID_ARGS" and "1 to 50" in env["error"]
    assert rig.exec.calls == []


def test_a_target_that_is_not_a_selector_is_refused(rig: Rig) -> None:
    env = rig.do(OP, {"targets": [{"kind": "segment"}], "width": 960, "height": 540})
    assert env["errorCode"] == "INVALID_ARGS" and "targets[0]" in env["error"]
    assert rig.exec.calls == []


def test_a_wider_ratio_is_refused_with_each_clips_ratio_and_nothing_is_written(rig: Rig) -> None:
    env = reformat(rig, [CLIPX, wt(rig.handles["sq"].get_wiretap_node_id())], 1000, 1000)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "allow_aspect_change" in env["error"]
    rows = env["result"]["aspect"]
    assert [r["name"] for r in rows] == ["CLIPX", "SQ"]
    assert [r["matches"] for r in rows] == [False, True]
    assert rows[0]["clipRatio"] == pytest.approx(1920 / 1080)
    assert rows[0]["requestedRatio"] == pytest.approx(1.0)
    assert env["result"]["ran"] is False
    assert untouched_world(rig)


def test_allow_aspect_change_lets_a_different_ratio_through(rig: Rig) -> None:
    env = reformat(rig, width=1000, height=1000, allow_aspect_change=True)
    clip = rig.handles["clipx"]
    assert env["ok"] is True, env
    assert (clip.width, clip.height, clip.ratio) == (1000, 1000, 1.0)


def test_the_aspect_check_allows_a_difference_of_0_001_and_not_more(rig: Rig) -> None:
    near = add_media_clip(reel(rig), "NEAR", width=3840, height=2160, ratio=1.778)
    far = add_media_clip(reel(rig), "FAR", width=3840, height=2160, ratio=1.778)
    allowed = reformat(rig, [wt(near.get_wiretap_node_id())], 3558, 2000)  # 1.779: 0.001 away
    refused = reformat(rig, [wt(far.get_wiretap_node_id())], 3560, 2000)  # 1.78: 0.002 away
    assert allowed["ok"] is True, allowed
    assert near.width == 3558 and near.ratio == pytest.approx(1.779)
    assert refused["errorCode"] == "PRECONDITION_UNMET", refused
    assert far.reformat_calls == []


def test_the_aspect_check_runs_for_every_target_before_any_is_written(rig: Rig) -> None:
    env = reformat(rig, [CLIPX, wt(rig.handles["sq"].get_wiretap_node_id())])
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert rig.records() == [] and touched_clips(rig) == [] and stash_entries(rig) == []


@pytest.mark.parametrize("target", [S1, REEL1])
def test_a_sequence_or_a_reel_is_refused_and_nothing_is_written(rig: Rig, target: Any) -> None:
    env = reformat(rig, [target])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "targets[0]" in env["error"] and "PyClip" in env["error"]
    assert untouched_world(rig)


def test_a_clip_inside_a_library_is_refused_and_nothing_is_written(rig: Rig) -> None:
    env = reformat(rig, [LIB_CLIP1])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "inside a library" in env["error"]
    assert untouched_world(rig)


def test_a_clip_that_was_moved_into_a_library_is_refused_too(rig: Rig) -> None:
    moved = rig.flame.media_panel.move(rig.handles["clipx"], rig.handles["libreel"])
    env = reformat(rig, [wt(moved[0].get_wiretap_node_id())])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert moved[0].reformat_calls == []


def test_a_clip_inside_a_reel_of_a_batch_group_is_refused(rig: Rig) -> None:
    batch_reel = rig.handles["bg1"].create_reel("Schematic Reel 1")
    rig.flame.media_panel.copy(rig.handles["clipx"], batch_reel)
    inside = batch_reel.children[-1]
    env = reformat(rig, [wt(inside.get_wiretap_node_id())])
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert "Batch group" in env["error"]
    assert inside.reformat_calls == [] and stash_entries(rig) == []


def test_one_bad_target_among_good_ones_refuses_the_whole_call(rig: Rig) -> None:
    a = wt(rig.handles["a"].get_wiretap_node_id())
    env = reformat(rig, [CLIPX, a, S1])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "targets[2]" in env["error"]
    assert rig.records() == [] and touched_clips(rig) == [] and stash_entries(rig) == []


def test_the_same_selector_twice_is_refused(rig: Rig) -> None:
    env = reformat(rig, [CLIPX, CLIPX])
    assert env["errorCode"] == "INVALID_ARGS" and "twice" in env["error"]
    assert rig.exec.calls == [] and untouched_world(rig)


def test_the_same_clip_by_two_selectors_is_refused_by_flame_before_any_write(rig: Rig) -> None:
    path = {"kind": "path", "path": ["RG", "Reel 1", "CLIPX"], "root": "desktop"}
    env = reformat(rig, [CLIPX, path])
    assert env["errorCode"] == "INVALID_ARGS" and "twice" in env["error"]
    assert untouched_world(rig)


def test_readonly_refuses_the_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ro = make_reformat_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    env = reformat(ro)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert "FLAME_MCP_READONLY" in env["error"] and ro.exec.calls == []


def test_the_effect_gate_counts_it_as_a_destructive_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gated = make_reformat_rig(
        monkeypatch,
        tmp_path,
        policy=Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,create,reversible"}),
    )
    env = reformat(gated)
    assert env["errorCode"] == "POLICY_DENIED" and "destructive" in env["error"]
    assert gated.exec.calls == []


def test_a_reformat_that_changes_nothing_is_a_silent_noop(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    clip.reformat_behavior = "noop"
    env = reformat(rig)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert clip.reformat_calls and clip.width == 1920
    assert env["result"]["reformatted"] == []
    only = env["result"]["notReformatted"][0]
    assert only["errorCode"] == "SILENT_NOOP" and only["from"]["name"] == "CLIPX"
    outcome = first_record(rig, "outcome")
    assert outcome["ok"] is False and outcome["errorCode"] == "SILENT_NOOP"
    assert outcome["created"] is None


def test_another_size_than_the_one_asked_for_is_a_flame_api_error(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    clip.reformat_behavior = "wrong_size"
    env = reformat(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "962" in env["error"] and "960" in env["error"]
    only = env["result"]["notReformatted"][0]
    assert only["after"]["width"] == 962 and only["before"]["width"] == 1920
    assert env["journal"]["revert"] is not None  # the clip did change: it can be put back


def test_a_reformat_that_raises_is_a_flame_api_error_with_flames_text(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    clip.reformat_behavior = "raise"
    env = reformat(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "scripted reformat failure" in env["error"]
    assert clip.width == 1920 and env["journal"]["revert"] is None


def test_a_start_frame_flame_did_not_apply_is_an_error_though_the_size_changed(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    clip.start_frame_behavior = "noop"
    env = reformat(rig, start_frame=1001)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "start_frame" in env["error"] and clip.width == 960
    assert env["journal"]["revert"] is not None


def test_a_change_start_frame_that_raises_is_reported(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    clip.start_frame_behavior = "raise"
    env = reformat(rig, start_frame=1001)
    assert env["errorCode"] == "FLAME_API_ERROR" and "scripted change_start_frame" in env["error"]


def test_some_clips_failing_gives_a_partial_result_and_the_others_stay_reformatted(
    rig: Rig,
) -> None:
    a, b = rig.handles["a"], rig.handles["b"]
    a.reformat_behavior = "noop"
    env = reformat(rig, [CLIPX, wt(a.get_wiretap_node_id()), wt(b.get_wiretap_node_id())])
    assert env["ok"] is True, env
    assert env["result"]["partial"] is True
    assert env["partial"] == {"reformatted": 2, "notReformatted": 1}
    assert [r["from"]["name"] for r in env["result"]["reformatted"]] == ["CLIPX", "B"]
    assert [r["from"]["name"] for r in env["result"]["notReformatted"]] == ["A"]
    assert env["result"]["notReformatted"][0]["errorCode"] == "SILENT_NOOP"
    assert (rig.handles["clipx"].width, a.width, b.width) == (960, 1280, 960)
    assert env["affected"] == 2 and env["degraded"]


def test_all_clips_failing_with_different_causes_is_a_flame_api_error(rig: Rig) -> None:
    rig.handles["clipx"].reformat_behavior = "noop"
    rig.handles["a"].reformat_behavior = "raise"
    env = reformat(rig, [CLIPX, wt(rig.handles["a"].get_wiretap_node_id())])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert [r["errorCode"] for r in env["result"]["notReformatted"]] == [
        "SILENT_NOOP",
        "FLAME_API_ERROR",
    ]


def test_a_clip_whose_segments_changed_between_the_read_and_the_write_is_a_conflict(
    rig: Rig,
) -> None:
    clip = rig.handles["clipx"]
    seen = {"n": 0}

    def edit_before_the_write(label: str | None) -> None:
        if label != OP:
            return
        seen["n"] += 1
        if seen["n"] == 3:  # read, stash, then the write
            clip.versions[0].tracks[0].segments[0].head = 9

    rig.exec.before_run = edit_before_the_write
    env = reformat(rig)
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert clip.reformat_calls == [] and clip.width == 1920


def test_a_clip_resized_between_the_read_and_the_write_is_a_conflict(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    seen = {"n": 0}

    def resize_before_the_write(label: str | None) -> None:
        if label != OP:
            return
        seen["n"] += 1
        if seen["n"] == 3:
            clip.width = 1280

    rig.exec.before_run = resize_before_the_write
    env = reformat(rig)
    assert env["errorCode"] == "CONFLICT" and "width" in env["error"]
    assert clip.reformat_calls == []


def test_a_failed_stash_copy_stops_the_call_before_any_clip_is_changed(rig: Rig) -> None:
    rig.exec.fault(OP, "FLAME_API_ERROR", when="before", skip=1)
    env = reformat(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "nothing was reformatted" in env["error"]
    assert touched_clips(rig) == [] and [r["type"] for r in rig.records()] == []


def test_a_call_that_names_a_clip_gone_before_the_read_fails_without_writing(rig: Rig) -> None:
    env = reformat(rig, [wt("/wt/RG/Reel 1/not_there")])
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert "nothing was written" in env["error"]
    assert untouched_world(rig)


def _has(rig: Rig, name: str) -> Any:
    return rig.handles[name]


def test_the_smallest_and_the_largest_size_are_accepted(rig: Rig) -> None:
    small = reformat(rig, width=24, height=24, allow_aspect_change=True)
    big = reformat(
        rig, [wt(_has(rig, "a").get_wiretap_node_id())], 16384, 9216
    )  # 16:9, the largest even size
    assert small["ok"] is True, small
    assert big["ok"] is True, big
    assert (rig.handles["clipx"].width, rig.handles["clipx"].height) == (24, 24)
    assert (_has(rig, "a").width, _has(rig, "a").height) == (16384, 9216)


def test_the_largest_square_size_is_accepted_with_allow_aspect_change(rig: Rig) -> None:
    env = reformat(rig, width=16384, height=16384, allow_aspect_change=True)
    assert env["ok"] is True, env


def test_the_aspect_check_allows_exactly_0_001_and_refuses_0_0012(rig: Rig) -> None:
    on_the_edge = add_media_clip(reel(rig), "EDGE", width=1000, height=1000, ratio=1.0)
    past_it = add_media_clip(reel(rig), "PAST", width=1000, height=1000, ratio=1.0)
    allowed = reformat(rig, [wt(on_the_edge.get_wiretap_node_id())], 10010, 10000)  # 1.0010
    refused = reformat(rig, [wt(past_it.get_wiretap_node_id())], 10012, 10000)  # 1.0012
    assert allowed["ok"] is True, allowed
    assert on_the_edge.width == 10010
    assert refused["errorCode"] == "PRECONDITION_UNMET", refused
    assert past_it.reformat_calls == []


def test_a_clip_that_needs_no_write_is_not_stashed(rig: Rig) -> None:
    a = wt(_has(rig, "a").get_wiretap_node_id())
    env = reformat(rig, [CLIPX, a], width=1920, height=1080)  # CLIPX already is 1920x1080
    assert env["ok"] is True, env
    skipped, written = env["result"]["reformatted"]
    assert skipped["skipped"] is True and skipped["checkpoint"] is None and skipped["stash"] is None
    assert written["checkpoint"] is not None
    assert [str(e.name.get_value()) for e in stash_entries(rig)] == ["A"]
    assert len(checkpoints(rig)) == 1


def test_when_no_clip_needs_a_write_nothing_is_stashed(rig: Rig) -> None:
    env = reformat(rig, width=1920, height=1080)
    assert env["ok"] is True and calls_of(rig, "stash") == []
    assert stash_entries(rig) == [] and checkpoints(rig) == []


def test_the_stash_copies_are_made_ten_clips_to_a_call(rig: Rig) -> None:
    clips = [add_media_clip(reel(rig), f"M{i}", width=1280, height=720) for i in range(25)]
    env = reformat(rig, [wt(c.get_wiretap_node_id()) for c in clips])
    assert env["ok"] is True, env
    assert STASH_BATCH == 10
    assert len(calls_of(rig, "stash")) == 3 and len(calls_of(rig, "write")) == 25
    assert len(checkpoints(rig)) == 25 and len(stash_entries(rig)) == 25
    first_write = [c for c in rig.exec.calls if c.label == OP].index(calls_of(rig, "write")[0])
    assert first_write == 1 + 3  # the read and three stash calls come first


def test_a_stash_copy_in_another_format_stops_the_call_before_any_clip_is_written(
    rig: Rig,
) -> None:
    rig.handles["clipx"].copy_overrides = {"width": 1900}
    env = reformat(rig, [CLIPX, wt(_has(rig, "a").get_wiretap_node_id())])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "stashed copy" in env["error"] and "width" in env["error"]
    assert touched_clips(rig) == []
    assert [r["type"] for r in rig.records()] == [
        "checkpoint",
        "checkpoint",
    ]  # the copies are on record


def test_a_stash_call_without_an_answer_ends_the_call_and_names_the_purge(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    env = reformat(rig)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert "journal.purge_checkpoints" in env["error"]
    assert touched_clips(rig) == []


def test_a_bit_depth_flame_ignored_is_a_silent_noop(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    clip.reformat_behavior = "noop"
    env = reformat(rig, width=1920, height=1080, bit_depth=16)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert clip.reformat_calls and clip.bit_depth == 10


def test_a_frame_rate_flame_ignored_is_a_silent_noop(rig: Rig) -> None:
    rig.handles["clipx"].reformat_behavior = "noop"
    env = reformat(rig, width=1920, height=1080, frame_rate="25 fps")
    assert env["errorCode"] == "SILENT_NOOP", env


def test_a_scan_mode_only_reformat_is_applied_and_read_back(rig: Rig) -> None:
    env = reformat(rig, width=1920, height=1080, scan_mode="F1")
    assert env["ok"] is True, env
    assert rig.handles["clipx"].scan_mode == "F1"
    assert env["result"]["reformatted"][0]["after"]["scanMode"] == "F1"


def test_a_reformat_that_moves_the_start_frame_is_followed_by_change_start_frame(
    rig: Rig,
) -> None:
    clip = add_media_clip(reel(rig), "S", start_frame=1001)
    clip.reformat_resets_start_frame = 1
    env = reformat(rig, [wt(clip.get_wiretap_node_id())], start_frame=1001)
    assert env["ok"] is True, env
    assert clip.start_frame == 1001 and clip.start_frame_calls == [(1001, True)]
    assert (clip.width, clip.height) == (960, 540)
    assert env["result"]["reformatted"][0]["after"]["startFrame"] == 1001


def test_a_start_frame_the_reformat_moved_and_flame_would_not_put_back_is_an_error(
    rig: Rig,
) -> None:
    clip = add_media_clip(reel(rig), "S", start_frame=1001)
    clip.reformat_resets_start_frame = 1
    clip.start_frame_behavior = "noop"
    env = reformat(rig, [wt(clip.get_wiretap_node_id())], start_frame=1001)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "start_frame" in env["error"] and clip.start_frame == 1


def test_a_start_frame_already_right_and_kept_calls_nothing(rig: Rig) -> None:
    clip = add_media_clip(reel(rig), "S", start_frame=1001)
    env = reformat(rig, [wt(clip.get_wiretap_node_id())], start_frame=1001)
    assert env["ok"] is True and clip.start_frame_calls == []
