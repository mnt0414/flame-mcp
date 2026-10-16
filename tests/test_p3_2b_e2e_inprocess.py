"""Runs scripts/p3_2b_plan_delete_e2e.py against the FAKE Flame in this process.

The script runs end to end (tests/e2e_inprocess.py). NOT evidence about a real Flame: it shows
that the stages, selectors and judgements of the script hang together and agree with what the fake
does, and that the pure parts (the dry run) compile. The fake is modelled on Flame 2025.2.4
measurements of segment trim, Timeline copy, move, restore and delete, and of the Timeline display
switching after a move."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import e2e_inprocess
from fake_media_tree import FakeFlame
from test_p2_3d_e2e_inprocess import failed_steps, load
from test_p3_2a_e2e_inprocess import FAKE_S1
from timeline_p32a_helpers import media


def run_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], FakeFlame, e2e_inprocess.HarnessExec]:
    module = load("p3_2b_plan_delete_e2e")
    common_a = load("p3_2a_e2e_common")
    common_b = load("p3_2b_e2e_common")
    stages = [load("p3_2b_e2e_plan"), load("p3_2b_e2e_delete")]
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    files = []
    for name in ("clip1", "clip2", "p2_len48", "p2_len72"):
        (media_dir / f"{name}.mov").write_bytes(b"x" * 32)
        files.append((name, media_dir / f"{name}.mov"))
    preset = tmp_path / "preset.xml"
    preset.write_text("<preset/>")
    monkeypatch.setattr(common_a, "CLIP_FILES", tuple(files))
    monkeypatch.setattr(common_a, "MAKE_S1_TEMPLATE", FAKE_S1)
    monkeypatch.setattr(common_a, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(common_a, "PRESET_SOURCE", preset)
    monkeypatch.setattr(common_b, "COMMENT_ATTR", "colour")
    monkeypatch.setattr(common_b, "COMMENT", [0.5, 0.5, 0.5])
    monkeypatch.setenv(common_a.CKPT_ENV, "")
    flame = FakeFlame()
    original_import = FakeFlame.import_clips

    def import_with_a_segment(self: FakeFlame, path: Any, destination: Any) -> list[Any]:
        made: list[Any] = original_import(self, path, destination)
        for clip in made:
            clip._versions[0]._tracks[0]._segments.append(media(str(clip.name.get_value()), 0, 23))
        return made

    monkeypatch.setattr(FakeFlame, "import_clips", import_with_a_segment)
    scripted = e2e_inprocess.install(module, monkeypatch, flame, tmp_path)
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
        "_out_path": out / "p3_2b_plan_delete_e2e.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261002_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame, scripted


def test_the_plan_delete_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, _scripted = run_script(monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    assert len(names) >= 40
    for stage in (
        "p1_three_trims",
        "p2_a_step",
        "p4_with_late",
        "p5_a_late_step",
        "p6_",
        "p7_",
        "p8_one_revert",
        "d1_clip_deleted",
        "d3_list_of_two_deleted",
        "d4_a_clip_in_a_library_is_refused",
        "d5_a_shown_sequence",
        "d6_",
        "d7_a_reel_",
        "d8_",
        "m1_",
        "m2_",
        "k1_",
        "k2_",
        "k3_",
        "r_",
        "g1_",
        "g2_",
        "g3_",
        "g4_",
        "g5_",
    ):
        assert any(n.startswith(stage) for n in names), stage
    assert flame.exited is True
    assert {
        "plan.run",
        "desktop.delete",
        "desktop.move",
        "journal.purge_checkpoints",
        "journal.check",
        "journal.revert",
    } <= set(results["opsUsed"])
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []
    assert results["env"] == {"FLAME_MCP_CHECKPOINT_LIBRARY": "P32B_261002_000000_CKPT"}


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("p3_2b_plan_delete_e2e")
    assert module.dry_run() == 0
    out = capsys.readouterr().out
    assert "dry run: OK" in out and "d_delete: desktop.delete" in out


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    module = load("p3_2b_plan_delete_e2e")
    registry = Registry()
    register_all(registry)
    used = {op for op, _build in module.CALLS.values()}
    assert used <= {o.name for o in registry.list_ops()}
    assert {"desktop.delete", "journal.purge_checkpoints", "plan.run", "desktop.move"} <= used
