"""Runs scripts/p3_3_segment_e2e.py against the FAKE Flame in this process.

The script runs end to end (tests/e2e_inprocess.py). NOT evidence about a real Flame: it shows
that the stages, selectors and judgements of the script hang together and agree with what the fake
does, and that the pure parts (the dry run) compile. The fake is modelled on Flame 2025.2.4
measurements of segment slip, replace, TimelineFX and extraction (copy_to_media_panel, match)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import e2e_inprocess
from fake_media_tree import FakeFlame
from p33_helpers import MEDIA_TABLE, import_with_media
from test_p2_3d_e2e_inprocess import failed_steps, load

FAKE_MAKE_SEQ = """
import p33_helpers as _h
_reel = flame.find_by_wiretap_node_id(@REEL@)
_result = _h.fake_make_sequence(flame, _reel, @NAME@, @PUTS@, @TRIMS@, @FXS@)
"""


def run_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], FakeFlame, e2e_inprocess.HarnessExec]:
    module = load("p3_3_segment_e2e")
    common = load("p3_3_e2e_common")
    common_a = load("p3_2a_e2e_common")
    stages = [
        load("p3_3_e2e_edit"),
        load("p3_3_e2e_effects"),
        load("p3_3_e2e_bitdepth"),
        load("p3_3_e2e_extract"),
    ]
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    files = []
    for name in MEDIA_TABLE:
        (media_dir / f"{name}.mov").write_bytes(b"x" * 32)
        files.append((name, media_dir / f"{name}.mov"))
    monkeypatch.setattr(common, "CLIP_FILES", tuple(files))
    monkeypatch.setattr(common, "MAKE_SEQ_TEMPLATE", FAKE_MAKE_SEQ)
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(common_a, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setenv(common_a.CKPT_ENV, "")
    flame = FakeFlame()
    monkeypatch.setattr(FakeFlame, "import_clips", import_with_media(FakeFlame))
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
        "_out_path": out / "p3_3_segment_e2e.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261002_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame, scripted


def test_the_segment_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, _scripted = run_script(monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    assert len(names) >= 120
    for stage in (
        "s1_",
        "s4_",
        "s7_",
        "s10_",
        "s12_",
        "r1_",
        "r4_",
        "r5_",
        "r6_",
        "r7_",
        "r8_",
        "r9_",
        "r13_",
        "e1_",
        "e4_",
        "e6_",
        "e7_",
        "e13_",
        "e16_",
        "e17_",
        "e19_",
        "e22_",
        "e23_",
        "e26_",
        "b1_",
        "b2_",
        "b3_",
        "b4_",
        "b5_",
        "b6_",
        "x1_",
        "x2_",
        "x5_",
        "x8_",
        "x10_",
        "x12_",
        "x13_",
        "x15_",
        "x20_",
        "p1_",
        "p3_",
        "p4_",
        "y_",
        "z_",
    ):
        assert any(n.startswith(stage) for n in names), stage
    assert flame.exited is True
    assert {
        "segment.slip",
        "segment.replace",
        "segment.create_effect",
        "segment.delete_effect",
        "segment.set_effect",
        "segment.effect_save_setup",
        "segment.effect_load_setup",
        "segment.copy_to_media_panel",
        "segment.match",
        "segment.list_effects",
        "timeline.create_sequence",
        "plan.run",
        "journal.revert",
    } <= set(results["opsUsed"])
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []
    assert results["env"] == {"FLAME_MCP_CHECKPOINT_LIBRARY": "P33_261002_000000_CKPT"}


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("p3_3_segment_e2e")
    assert module.dry_run() == 0
    out = capsys.readouterr().out
    assert "dry run: OK" in out and "s_slip: segment.slip" in out
    assert "x_match_preserve_handle: segment.match" in out and "make_seq (" in out


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    module = load("p3_3_segment_e2e")
    registry = Registry()
    register_all(registry)
    used = {op for op, _build in module.CALLS.values()}
    assert used <= {o.name for o in registry.list_ops()}
    assert {
        "segment.slip",
        "segment.replace",
        "segment.create_effect",
        "segment.delete_effect",
        "segment.set_effect",
        "segment.effect_load_setup",
        "segment.effect_save_setup",
        "segment.copy_to_media_panel",
        "segment.match",
        "plan.run",
    } <= used
