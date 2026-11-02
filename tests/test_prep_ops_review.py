"""Review fixes for the prep ops: plan refusal, the colour check's incomplete scan and
`skip_colour_check`, `desktop.sync_record_tc` with nothing to write, `desktop.set_record_tc`'s
current_time read-back, and where `reel_reorder` lives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp.ops.plan import REFUSED_IN_PLAN
from journal_test_helpers import Rig
from test_colour_spaces import coloured, make_sequence
from test_desktop_tc import SYNC, clip_of, make_clip
from timeline_p32a_helpers import CLIPX, make_timeline_rig, media

EXPORT = "render.export"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


@pytest.fixture
def preset(tmp_path: Path) -> Path:
    path = tmp_path / "image.xml"
    path.write_text("<preset version='14'><type>image</type></preset>", encoding="utf-8")
    return path


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


@pytest.mark.parametrize("op", ["batch.delete", "desktop.sync_record_tc"])
def test_plan_run_refuses_the_new_wrapped_ops(rig: Rig, op: str) -> None:
    assert op in REFUSED_IN_PLAN
    env = rig.do("plan.run", {"steps": [{"op": op, "args": {}}]})
    assert env["errorCode"] == "INVALID_ARGS" and "cannot be a plan step" in str(env["result"])


def export(rig: Rig, preset: Path, out: Path, target: Any, **extra: Any) -> dict[str, Any]:
    args = {"target": target, "preset": str(preset), "output_dir": str(out), **extra}
    return rig.do(EXPORT, args)


def unreadable_sequence(rig: Rig, name: str) -> dict[str, Any]:
    return make_sequence(
        rig,
        name,
        [[coloured(media("a", 0, 9), "ACEScg"), coloured(media("b", 10, 19), RuntimeError("x"))]],
    )


def mixed_sequence(rig: Rig, name: str) -> dict[str, Any]:
    return make_sequence(
        rig,
        name,
        [[coloured(media("a", 0, 9), "ACEScg"), coloured(media("b", 10, 19), "Rec.709")]],
    )


def test_export_is_refused_when_the_colour_scan_is_incomplete(
    rig: Rig, preset: Path, out: Path
) -> None:
    env = export(rig, preset, out, unreadable_sequence(rig, "R1"))
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["colourSpaces"]["complete"] is False
    assert env["result"]["ran"] is False and list(out.iterdir()) == []
    assert [r for r in rig.records() if r.get("type") == "intent"] == []


def test_skip_colour_check_lets_an_incomplete_scan_through_and_says_so(
    rig: Rig, preset: Path, out: Path
) -> None:
    env = export(rig, preset, out, unreadable_sequence(rig, "R2"), skip_colour_check=True)
    assert env["errorCode"] != "PRECONDITION_UNMET", env
    assert any(n.startswith("colour check skipped") for n in env["degraded"])
    assert env["result"]["colourSpaces"]["complete"] is False


def test_skip_colour_check_lets_a_mixed_sequence_through_and_says_so(
    rig: Rig, preset: Path, out: Path
) -> None:
    env = export(rig, preset, out, mixed_sequence(rig, "R3"), skip_colour_check=True)
    assert env["errorCode"] != "PRECONDITION_UNMET", env
    assert any("different colour spaces" in n for n in env["degraded"])


def test_the_default_still_refuses_a_mixed_sequence(rig: Rig, preset: Path, out: Path) -> None:
    env = export(rig, preset, out, mixed_sequence(rig, "R4"))
    assert env["errorCode"] == "PRECONDITION_UNMET"


def test_a_clip_export_is_not_checked_and_takes_the_flag(rig: Rig, preset: Path, out: Path) -> None:
    env = export(rig, preset, out, CLIPX, skip_colour_check=True)
    assert env["errorCode"] != "PRECONDITION_UNMET", env


def test_all_in_sync_is_ok(rig: Rig) -> None:
    target = make_clip(rig, "A", record=7, source=7)
    env = rig.do(SYNC, {"targets": [target]})
    assert env["ok"] is True and env.get("errorCode") is None
    assert env["result"]["rows"][0]["status"] == "in_sync"
    assert rig.records() == []


def test_only_skipped_rows_are_ok_with_a_note(rig: Rig) -> None:
    target = make_clip(rig, "A", record=100, source=500, path="/media/a.0001.dpx")
    env = rig.do(SYNC, {"targets": [target]})
    assert env["ok"] is True and env.get("errorCode") is None
    assert env["result"]["rows"][0]["reason"] == "dpx_source"
    assert any("skipped" in n and "dpx_source" in n for n in env["degraded"])
    assert env["affected"] == 0


def test_a_current_time_that_reads_back_differently_is_a_warning_not_an_error(
    rig: Rig,
) -> None:
    target = make_clip(rig, "A", record=100, source=500)
    clip = clip_of(rig, "A")
    real = clip.current_time.set_value

    def clamp(value: Any) -> None:
        real(type(value)(value.frame + 3, value.frame_rate))

    clip.current_time.set_value = clamp
    env = rig.do(
        "desktop.set_record_tc",
        {"target": target, "start_time_frame": 42, "current_time_frame": 42},
    )
    assert env["ok"] is True, env
    assert any("current_time_frame" in w for w in env["result"]["warnings"])
    assert clip.start_time.get_value().frame == 42


def test_a_start_time_that_reads_back_differently_is_still_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_clip(rig, "A", record=100, source=500)
    clip = clip_of(rig, "A")
    monkeypatch.setattr(clip.start_time, "set_value", lambda value: None)
    env = rig.do("desktop.set_record_tc", {"target": target, "start_time_frame": 42})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"


def test_reel_reorder_lives_in_the_package_and_the_script_reexports_it() -> None:
    root = Path(__file__).resolve().parent.parent
    assert (root / "src" / "flame_mcp" / "reel_reorder.py").is_file()
    assert "flame_mcp.reel_reorder" in (root / "scripts" / "reel_reorder.py").read_text()
