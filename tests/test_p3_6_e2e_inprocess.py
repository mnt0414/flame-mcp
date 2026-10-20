"""Runs scripts/p3_6_edit_e2e.py against the FAKE Flame in this process.

The script runs end to end (tests/e2e_inprocess.py). NOT evidence about a real Flame: it shows
that the stages, selectors and judgements of the script hang together and agree with what the fake
does, and that the pure parts (the dry run, the row arithmetic) work. The fake is modelled on the
Flame 2025.2.4 measurements of delete_segments, move_track, shorten, compare and the audio reads,
among them (Flame 2025.2.4 and 2026.2.3) the audio after a revert, moving the
track that holds the audio clip, a shorten where the tracks end in media and the audio differences
of a shortened copy, which are JUDGED steps now. The pure parts have their own tests below; what
the fake does where nothing was measured is its own invention."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import e2e_inprocess
from fake_media_tree import FakeFlame
from p33_helpers import MEDIA_TABLE
from p36_helpers import (
    AV_LENGTH,
    FAKE_MAKE_SEQ_WITH_SOUND,
    copy_drops_audio,
    extract_keeps_every_length,
    full_reads_fail,
    import_with_sound,
    lift_drops_audio,
    move_touches_audio,
)
from test_p2_3d_e2e_inprocess import failed_steps, load


def run_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    switch: Callable[[pytest.MonkeyPatch], None] | None = None,
) -> tuple[dict[str, Any], FakeFlame, e2e_inprocess.HarnessExec]:
    module = load("p3_6_edit_e2e")
    common = load("p3_6_e2e_common")
    common_3 = load("p3_3_e2e_common")
    common_a = load("p3_2a_e2e_common")
    stages = [
        load("p3_6_e2e_delete"),
        load("p3_6_e2e_move"),
        load("p3_6_e2e_audio"),
        load("p3_6_e2e_compare"),
    ]
    monkeypatch.setitem(MEDIA_TABLE, "p2_av72", (0, AV_LENGTH))
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    files = []
    for name, _wanted in common.CLIP_FILES:
        (media_dir / f"{name}.mov").write_bytes(b"x" * 32)
        files.append((name, media_dir / f"{name}.mov"))
    monkeypatch.setattr(common, "CLIP_FILES", tuple(files))
    monkeypatch.setattr(common_3, "MAKE_SEQ_TEMPLATE", FAKE_MAKE_SEQ_WITH_SOUND)
    monkeypatch.setattr(common_3, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(common_a, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setenv(common_a.CKPT_ENV, "")
    flame = FakeFlame()
    monkeypatch.setattr(FakeFlame, "import_clips", import_with_sound(FakeFlame))
    scripted = e2e_inprocess.install(module, monkeypatch, flame, tmp_path)
    if switch is not None:
        switch(monkeypatch)
    for stage in stages:
        monkeypatch.setattr(stage, "server_session", module.server_session)
    out = tmp_path / "out"
    out.mkdir()
    journal_dir = tmp_path / "e2e_journal"
    journal_dir.mkdir()
    results: dict[str, Any] = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": out / "p3_6_edit_e2e.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261018_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame, scripted


def judged_steps(results: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """The steps whose name starts with `prefix` that decide the run (an observation does not)."""
    return [
        s
        for s in results["steps"]
        if s["name"].startswith(prefix) and s.get("kind") != "observation"
    ]


def failed_names(results: dict[str, Any]) -> list[str]:
    return [s["name"] for s in results["steps"] if not s["ok"]]


def test_the_edit_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, _scripted = run_script(monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    assert len(names) >= 60
    for stage in (
        "prepare_the_audio_clip_was_imported_with_audio",
        "d1_",
        "d5_",
        "d6_",
        "d7_",
        "d8_",
        "d10_",
        "d12_",
        "d14_",
        "m1_",
        "m4_",
        "m5_",
        "m7_",
        "m9_",
        "m10b_",
        "m11_",
        "m11b_",
        "m12_",
        "m15_",
        "a1_",
        "a2_",
        "a3_",
        "a4_",
        "a6_",
        "s1_",
        "s3_",
        "s4_",
        "s5_",
        "s7_",
        "s9_",
        "s9b_",
        "s10_",
        "s10b_",
        "s11_",
        "s13_",
        "q1_",
        "q2_",
        "q3b_",
        "q4_",
        "c1_",
        "c2_",
        "y1_",
        "y2_",
        "z_",
    ):
        assert any(n.startswith(stage) for n in names), stage
    for judged in (
        "d0_",
        "d0b_",
        "s1_",
        "s2_",
        "s9b_",
        "s10_",
        "s10b_",
        "q3_",
        "q3b_",
        "m10b_",
        "m11_",
        "m11b_",
        "z_the_judged_steps_of_the_measured_cases",
    ):
        assert judged_steps(results, judged), f"no JUDGED step {judged}"
    assert [s["name"] for s in results["steps"] if s.get("kind") == "observation"] == []
    assert results["observations"] == []
    assert flame.exited is True
    assert set(results["opsUsed"]) >= {
        "timeline.delete_segments",
        "timeline.move_track",
        "timeline.shorten",
        "timeline.compare",
        "segment.connections",
        "journal.revert",
    }
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []
    assert results["env"] == {"FLAME_MCP_CHECKPOINT_LIBRARY": "P36E_261018_000000_CKPT"}


def test_a_lift_that_takes_the_audio_off_turns_the_run_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, _flame, _scripted = run_script(monkeypatch, tmp_path, lift_drops_audio)
    failed = failed_names(results)
    assert results["ok"] is False
    assert any(n.startswith("s1b_") for n in failed), failed
    assert any(n.startswith("q3_") for n in failed), failed
    assert any(n.startswith("z_the_judged_steps_of_the_measured_cases") for n in failed), failed
    assert not judged_steps(results, "s2_")
    s1b = next(s for s in results["steps"] if s["name"].startswith("s1b_"))
    for key in ("liftOk", "gapOnly", "hasAudio"):
        assert key in s1b, key
    assert s1b["hasAudio"] is False and s1b["gapOnly"] is True
    s1 = next(s for s in results["steps"] if s["name"].startswith("s1_"))
    assert s1.get("kind") != "observation" and s1["ok"] is False
    assert s1["hasAudio"] is False and s1["gapOnly"] is True


def step_named(results: dict[str, Any], prefix: str) -> dict[str, Any]:
    return next(s for s in results["steps"] if s["name"].startswith(prefix))


def test_the_steps_judged_on_the_audio_measurements_record_the_measured_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, _flame, _scripted = run_script(monkeypatch, tmp_path)
    m10b = step_named(results, "m10b_")
    assert m10b["ok"] is True and m10b["rowsEqual"] is True
    m11 = step_named(results, "m11_")
    assert m11["ok"] is True and m11["audioRowsEqual"] is True
    assert m11["resultAudio"] == {"changed": False, "complete": True}
    m11b = step_named(results, "m11b_")
    assert m11b["ok"] is True
    for key in ("revertOk", "fingerprintEqual", "rowsEqual", "audioRowsEqual"):
        assert m11b[key] is True, key
    s1 = step_named(results, "s1_")
    assert s1["ok"] is True and s1["gapOnly"] is True and s1["hasAudio"] is True
    s9b = step_named(results, "s9b_")
    assert s9b["ok"] is True and s9b["durationBack"] == s9b["durationBefore"] == 72
    assert s9b["videoRowsEqual"] is True and s9b["audioRowsEqual"] is True
    s10 = step_named(results, "s10_")
    assert s10["ok"] is True and (s10["end"], s10["durationAfter"]) == (60, 60)
    assert s10["videoAsExpected"] is True and s10["audioAsExpected"] is True
    assert [r[:3] for r in s10["after"][0]] == [["Video Segment", 0, 59]]
    assert [(r["rin"], r["rout"]) for r in s10["audioAfter"]] == [(0, 59), (0, 59)]
    s10b = step_named(results, "s10b_")
    assert s10b["ok"] is True and s10b["durationBack"] == 72
    for key in ("revertOk", "fingerprintEqual", "videoRowsEqual", "audioRowsEqual"):
        assert s10b[key] is True, key
    q3b = step_named(results, "q3b_")
    assert q3b["ok"] is True
    assert sorted((d["where"], d["field"], d["a"], d["b"]) for d in q3b["audioDifferences"]) == [
        (f"audio track 0 channel {channel} segment 0", field, 71, 35)
        for channel in (0, 1)
        for field in ("recordOut", "sourceOut")
    ]


def test_a_move_that_changes_the_audio_turns_the_audio_clip_track_step_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, _flame, _scripted = run_script(monkeypatch, tmp_path, move_touches_audio)
    failed = failed_names(results)
    assert results["ok"] is False
    assert any(n.startswith("m11_") for n in failed), failed


def test_a_copy_that_loses_the_audio_turns_the_revert_and_compare_steps_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, _flame, _scripted = run_script(monkeypatch, tmp_path, copy_drops_audio)
    failed = failed_names(results)
    assert results["ok"] is False
    for prefix in ("m10b_", "m11b_", "s9b_", "s10b_", "q3b_"):
        assert any(n.startswith(prefix) for n in failed), (prefix, failed)
    assert not any(n.startswith(("s1_", "s2_", "s10_")) for n in failed), failed


def test_the_expected_audio_differences_are_the_cut_ends_of_the_crossing_segments() -> None:
    compare = load("p3_6_e2e_compare")

    def row(channel: int, index: int, rin: int, rout: int, sin: int, sout: int) -> dict[str, Any]:
        return {
            "track": 0,
            "channel": channel,
            "index": index,
            "type": "Audio Segment",
            "src": "x",
            "rin": rin,
            "rout": rout,
            "sin": sin,
            "sout": sout,
        }

    rows = [row(0, 0, 0, 71, 0, 71), row(1, 0, 0, 23, 0, 23), row(1, 1, 24, 71, 5, 52)]
    expected = compare.expected_audio_cut_differences(rows, 60)
    assert expected == {
        ("audio track 0 channel 0 segment 0", "recordOut", 71, 59),
        ("audio track 0 channel 0 segment 0", "sourceOut", 71, 59),
        ("audio track 0 channel 1 segment 1", "recordOut", 71, 59),
        ("audio track 0 channel 1 segment 1", "sourceOut", 52, 40),
    }
    assert compare.expected_audio_cut_differences(rows, 100) == set()


def test_the_delete_stage_reproduces_the_two_measured_lengths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, _flame, _scripted = run_script(monkeypatch, tmp_path)
    steps = {s["name"].split("_")[0]: s for s in results["steps"]}
    d2 = steps["d2"]
    assert d2["ok"] is True and (d2["durationBefore"], d2["expectedEnd"], d2["duration"]) == (
        120,
        96,
        96,
    )
    assert d2["video"][0][-1][:3] == ["Video Segment", 48, 95]  # no Gap behind it
    assert d2["video"][1][-1] == ["Gap", 72, 95]  # the other track's Gap shrank
    d8 = steps["d8"]
    assert d8["ok"] is True and (d8["expectedEnd"], d8["duration"]) == (72, 72)
    assert [r[0] for r in d8["after"][1]] == ["Video Segment"]  # its Gap is gone
    q2 = steps["q2"]
    assert q2["ok"] is True and "video track 1 segment 1" in q2["wheres"]
    assert not [w for w in q2["wheres"] if w.startswith("video track 1 segment 0")]


def test_an_extract_that_leaves_the_lengths_alone_turns_the_delete_stage_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, _flame, _scripted = run_script(monkeypatch, tmp_path, extract_keeps_every_length)
    failed = failed_names(results)
    assert results["ok"] is False
    for prefix in ("d1_", "d2_", "d8_"):
        assert any(n.startswith(prefix) for n in failed), (prefix, failed)


def test_a_transport_read_that_fails_turns_the_delete_stage_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, _flame, _scripted = run_script(monkeypatch, tmp_path, full_reads_fail)
    failed = failed_names(results)
    assert results["ok"] is False
    for prefix in ("d0_", "d0b_", "d1_", "d6b_", "d7_", "d11b_", "d12_", "d13_", "d14_", "d15_"):
        assert any(n.startswith(prefix) for n in failed), (prefix, failed)
    for prefix in ("d16_", "d17_", "m12_", "m13_", "m14_", "s11_", "y1_"):
        assert any(n.startswith(prefix) for n in failed), (prefix, failed)


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("p3_6_edit_e2e")
    assert module.dry_run() == 0
    out = capsys.readouterr().out
    assert "dry run: OK" in out and "d_delete: timeline.delete_segments" in out
    assert "full (" in out and "m_move: timeline.move_track" in out


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    module = load("p3_6_edit_e2e")
    registry = Registry()
    register_all(registry)
    used = {op for op, _build in module.CALLS.values()}
    assert used <= {o.name for o in registry.list_ops()}
    assert {
        "timeline.delete_segments",
        "timeline.move_track",
        "timeline.shorten",
        "timeline.compare",
        "segment.connections",
    } <= used


def test_the_write_connection_ops_are_never_called() -> None:
    module = load("p3_6_edit_e2e")
    used = {op for op, _build in module.CALLS.values()}
    assert not used & {
        "segment.create_connection",
        "segment.remove_connection",
        "segment.sync_connections",
    }


def test_shorten_expect_keeps_cuts_and_drops_the_tail() -> None:
    common = load("p3_6_e2e_common")
    rows = [
        ["Video Segment", 0, 23, 0, 23, "a", []],
        ["Gap", 24, 47],
        ["Video Segment", 48, 71, 0, 23, "b", ["Blur"]],
        ["Video Segment", 72, 95, 0, 23, "c", []],
    ]
    expected = common.shorten_expect(rows, 60)
    assert expected == [
        ["Video Segment", 0, 23, 0, 23, "a", []],
        ["Gap", 24, 47],
        ["Video Segment", 48, 59, 0, None, "b", ["Blur"]],
    ]
    assert common.shorten_expect(rows, 48)[-1] == ["Gap", 24, 47]
    assert common.masked(expected, 60) == expected


def test_media_after_delete_moves_only_what_follows_the_removed_segment() -> None:
    common = load("p3_6_e2e_common")
    rows = [
        {"type": "Video Segment", "src": "a", "rin": 0, "rout": 23, "sin": 0, "sout": 23, "fx": []},
        {"type": "Gap", "src": "", "rin": 24, "rout": 47, "sin": 0, "sout": 23, "fx": []},
        {
            "type": "Video Segment",
            "src": "b",
            "rin": 48,
            "rout": 71,
            "sin": 0,
            "sout": 23,
            "fx": [],
        },
        {
            "type": "Video Segment",
            "src": "c",
            "rin": 72,
            "rout": 95,
            "sin": 5,
            "sout": 28,
            "fx": [],
        },
    ]
    closed = common.media_after_delete(rows, [48], ripple=True)
    assert [r[:3] for r in closed] == [["Video Segment", 0, 23], ["Video Segment", 48, 71]]
    lifted = common.media_after_delete(rows, [48], ripple=False)
    assert [r[:3] for r in lifted] == [["Video Segment", 0, 23], ["Video Segment", 72, 95]]
    gap_closed = common.media_after_delete(rows, [24], ripple=True)
    assert [r[1] for r in gap_closed] == [0, 24, 48]


def test_strip_trailing_gaps_only_takes_the_end() -> None:
    common = load("p3_6_e2e_common")
    rows = [["Gap", 0, 9], ["Video Segment", 10, 19], ["Gap", 20, 29], ["Gap", 30, 39]]
    assert common.strip_trailing_gaps(rows) == rows[:2]
    assert common.strip_trailing_gaps([["Gap", 0, 9]]) == []


def full_of(
    video: list[list[list[Any]]], duration: int, audio: list[list[Any]] | None = None
) -> Any:
    def rows(track: list[list[Any]]) -> list[dict[str, Any]]:
        return [
            {"type": r[0], "src": "x", "rin": r[1], "rout": r[2], "sin": 0, "sout": 0, "fx": []}
            for r in track
        ]

    channels = [[len(audio)]] if audio is not None else []
    audio_rows = [
        {
            "track": 0,
            "channel": 0,
            "index": i,
            "type": r[0],
            "src": "",
            "rin": r[1],
            "rout": r[2],
            "sin": 0,
            "sout": 0,
        }
        for i, r in enumerate(audio or [])
    ]
    return {
        "video": [[rows(t) for t in video]],
        "audio": {"channels": channels, "rows": audio_rows},
        "duration": duration,
        "startTime": 0,
    }


def test_end_after_delete_takes_the_longest_content_of_every_track() -> None:
    common = load("p3_6_e2e_common")
    video = [
        [
            ["Video Segment", 0, 23],
            ["Gap", 24, 47],
            ["Video Segment", 48, 71],
            ["Video Segment", 72, 119],
        ],
        [["Video Segment", 0, 71], ["Gap", 72, 119]],
    ]
    full = full_of(video, 120, [["Gap", 0, 119]])
    assert common.end_after_delete(full, [48]) == 96
    assert common.end_after_delete(full, [24]) == 96  # a Gap of 24 closes: 120 - 24
    longer = full_of(video, 120, [["Audio Segment", 0, 119]])
    assert common.end_after_delete(longer, [48]) == 120  # the audio decides
    assert common.end_after_delete(None, [48]) is None


def test_gaps_follow_needs_the_length_and_every_gap_at_the_end() -> None:
    common = load("p3_6_e2e_common")
    good = full_of(
        [
            [["Video Segment", 0, 23], ["Video Segment", 24, 95]],
            [["Video Segment", 0, 71], ["Gap", 72, 95]],
        ],
        96,
        [["Gap", 0, 95]],
    )
    assert common.gaps_follow(good, 96) is True
    assert common.gaps_follow(good, 100) is False  # the length is another
    stale_video = full_of(
        [[["Video Segment", 0, 95]], [["Video Segment", 0, 71], ["Gap", 72, 119]]], 96
    )
    assert common.gaps_follow(stale_video, 96) is False
    stale_audio = full_of([[["Video Segment", 0, 95]]], 96, [["Gap", 0, 119]])
    assert common.gaps_follow(stale_audio, 96) is False
    assert common.gaps_follow(None, 96) is False and common.gaps_follow(good, None) is False


def test_two_reads_that_failed_are_not_the_same_video() -> None:
    common = load("p3_6_e2e_common")
    failed = {"video": "ERR:RuntimeError: boom", "audio": None}
    good = {"video": [[[{"type": "Gap", "rin": 0, "rout": 9}]]], "audio": None}
    assert common.video_tracks(None) == common.video_tracks(failed) == []
    assert common.same_video(None, None) is False
    assert common.same_video(failed, failed) is False
    assert common.same_video(good, None) is False
    assert common.same_video(good, good) is True
    assert common.video_shape(None) is None and common.video_shape(failed) is None
