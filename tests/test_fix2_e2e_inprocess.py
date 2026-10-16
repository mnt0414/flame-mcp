"""Runs scripts/fix2_library_batch_e2e.py end to end against the FAKE Flame.

Uses tests/e2e_inprocess.py. NOT evidence about a real Flame: it shows that the stages, selectors
and judgements of the script hang together and agree with what the fake (and so the measurement
on Flame) does, and that the pure parts (the dry run) compile."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import e2e_inprocess
from fake_media_tree import FakeFlame
from test_p2_3d_e2e_inprocess import failed_steps, load


def run_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], FakeFlame, e2e_inprocess.HarnessExec]:
    monkeypatch.setenv("FLAME_MCP_CHECKPOINT_LIBRARY", "restored-by-monkeypatch")
    module = load("fix2_library_batch_e2e")
    common = load("fix2_e2e_common")
    stages = load("fix2_e2e_stages")
    media = tmp_path / "media"
    media.mkdir()
    files = []
    for name, stem in (("c1", "clip1"), ("c2", "clip2"), ("c3", "clip3")):
        (media / f"{stem}.mov").write_bytes(b"x" * 32)
        files.append((name, media / f"{stem}.mov"))
    monkeypatch.setattr(common, "CLIP_FILES", tuple(files))
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path / "out")
    flame = FakeFlame()
    flame.projects.current_project.name = "flame_mcp_probe"
    scripted = e2e_inprocess.install(module, monkeypatch, flame, tmp_path)
    monkeypatch.setattr(stages, "server_session", module.server_session)
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
        "_out_path": out / "fix2_library_batch_e2e.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261001_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame, scripted


def test_the_fix2_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, _scripted = run_script(monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    assert len(names) >= 45
    for stage in ("l1_", "l2_", "l3_", "l6_", "b1_", "b3_", "b4_", "b5_", "b6_"):
        assert any(n.startswith(stage) for n in names), stage
    for stage in (
        "r1_",
        "r2_",
        "r3_",
        "r4_",
        "r3b_",
        "r5_",
        "r6_",
        "o2_",
        "o4_",
        "o5_",
        "o6_",
        "d1_",
        "s1_",
        "s2_",
        "l0_",
        "c1_",
        "c2_",
        "e1_",
        "g_",
        "z_",
    ):
        assert any(n.startswith(stage) for n in names), stage
    assert flame.exited is True
    assert {"library.create", "desktop.copy", "desktop.list_children"} <= set(results["opsUsed"])
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("fix2_library_batch_e2e")
    assert module.dry_run() == 0
    assert "dry run: OK" in capsys.readouterr().out


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    module = load("fix2_library_batch_e2e")
    registry = Registry()
    register_all(registry)
    used = {op for op, _build in module.CALLS.values()}
    assert used <= {o.name for o in registry.list_ops()}
    assert {"library.create", "desktop.copy", "desktop.list_children", "plan.run"} <= used
