"""Runs the scripts/p2_3d_*.py real-Flame e2e scripts against the FAKE Flame.

They run end to end in this process (tests/e2e_inprocess.py). NOT evidence about a real Flame: it
shows that the stages, selectors and judgements of the scripts hang together and agree with what
the fake (and so the probe) does."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import e2e_inprocess
from fake_media_tree import FakeFlame

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str) -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


def run_script(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], FakeFlame, e2e_inprocess.HarnessExec]:
    module = load(name)
    flame = FakeFlame()
    scripted = e2e_inprocess.install(module, monkeypatch, flame, tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    journal_dir = tmp_path / "e2e_journal"
    journal_dir.mkdir()
    results: dict[str, Any] = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "gaps": list(getattr(module, "GAPS", ())),
        "journalDir": str(journal_dir),
        "_out_path": out / f"{name}.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261001_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame, scripted


def failed_steps(results: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {k: v for k, v in s.items() if k in ("name", "error", "traceback", "conflicts")}
        for s in results["steps"]
        if not s["ok"]
    ]


def test_the_desktop_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, _scripted = run_script("p2_3d_desktop_e2e", monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    assert len(names) >= 45
    for stage in (
        "a1_",
        "a5_",
        "a6_",
        "b1_",
        "b5_",
        "c1_",
        "c6_",
        "c7_",
        "c8_",
        "d1_",
        "d3_",
        "d10_",
    ):
        assert any(n.startswith(stage) for n in names), stage
    assert "z_everything_this_run_made_is_deleted" in names and flame.exited is True
    assert results["opsUsed"] == [
        "desktop.copy",
        "desktop.create",
        "journal.list",
        "journal.revert",
    ]
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []


def test_scenario_4_runs_with_ops_only_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, scripted = run_script("p2_4_scenario4_e2e", monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    assert results["gaps"] == []
    used = set(results["opsUsed"])
    assert {"desktop.create", "desktop.copy", "batch.add_clips", "render.batch"} <= used
    scaffolding = [c for c in scripted.calls if (c.label or "").startswith("p24s4")]
    assert scaffolding, "the scenario reads back through transport"
    forbidden = ("create_batch_group", "create_reel_group", "media_panel.copy", "desktop.create")
    assert [c.label for c in scaffolding if any(w in c.code for w in forbidden)] == []
    # the one reel transport makes is the material's (in the library), not a Desktop reel
    assert [c.label for c in scaffolding if ".create_reel(" in c.code] == ["p24s4_prepare_material"]
    imported = [c for c in scaffolding if "import_clips" in c.code]
    assert [c.label for c in imported] == ["p24s4_prepare_material"]
    # 4 creates (reel group, reel, Batch group, decoy) and 1 copy, each a read and a write trip
    assert len([c for c in scripted.calls if c.label == "desktop.create"]) == 8
    assert len([c for c in scripted.calls if c.label == "desktop.copy"]) == 2
    names = [s["name"] for s in results["steps"]]
    assert "prepare_desktop_reel_with_3_clips_by_desktop_create_and_desktop_copy" in names
    assert "f_the_write_ops_were_journaled_in_the_order_they_ran" in names
    desktop = flame.projects.current_project.current_workspace.desktop
    assert len(desktop.batch_groups) == 2 and len(desktop.reel_groups) == 1
