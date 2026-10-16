"""Runs scripts/p3_5_scenarios_e2e.py against the FAKE Flame in this process.

The script runs end to end (tests/e2e_inprocess.py). NOT evidence about a real Flame: it shows
that the stages, selectors and judgements of the script hang together and agree with what the fake
does, that no scenario writes through transport, and that the pure parts (the dry run) compile.
The fake is modelled on Flame 2025.2.4 measurements of segment replace, copy and match,
TimelineFX, and Timeline checkpoint and restore."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

import e2e_inprocess
from fake_media_tree import FakeFlame
from p33_helpers import MEDIA_TABLE, import_with_media
from test_p2_3d_e2e_inprocess import failed_steps, load
from test_p3_3_e2e_inprocess import FAKE_MAKE_SEQ

SCENARIO_OPS = {
    "library.list",
    "desktop.list_children",
    "mediahub.import",
    "desktop.find",
    "timeline.list_segments",
    "segment.info",
    "desktop.copy",
    "desktop.move",
    "desktop.delete",
    "plan.run",
    "segment.replace",
    "segment.slip",
    "segment.create_effect",
    "segment.effect_save_setup",
    "segment.set_effect",
    "segment.effect_load_setup",
    "segment.copy_to_media_panel",
    "segment.trim",
    "desktop.rename",
    "journal.revert",
}
ALLOWED_LABELS = re.compile(
    r"^(p35_(prepare|cleanup|left|cleanup_exit)|p33_make_\w+"
    r"|p32a_(struct|seg_facts|children|find_uid|stash)|p33_(fx_rows|clip_facts)"
    r"|p31a_struct|timeline\.(preresolve|restore))$"
)
"""The labels of the script's own transport calls: the material (prepare, the sequences), the
cleanup, and reads. A label of a write (a rename, a trim, a slip, a copy, a delete) is not among
them. (The ops' own calls carry the op's name as label; `timeline.preresolve` / `timeline.restore`
are the server's internal steps of a plan and of a revert.)"""


def run_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], FakeFlame, e2e_inprocess.HarnessExec]:
    module = load("p3_5_scenarios_e2e")
    common = load("p3_5_e2e_common")
    common_3 = load("p3_3_e2e_common")
    common_a = load("p3_2a_e2e_common")
    stages = [load("p3_5_scenario1"), load("p3_5_scenario2"), load("p3_5_library")]
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    files = {}
    for name in MEDIA_TABLE:
        (media_dir / f"{name}.mov").write_bytes(b"x" * 32)
        files[name] = media_dir / f"{name}.mov"
    monkeypatch.setattr(common, "CLIP_FILES", tuple((n, files[n]) for n, _p in common.CLIP_FILES))
    monkeypatch.setattr(common, "REPLACEMENT_FILE", files[common.REPLACEMENT_CLIP])
    for stage in stages:
        if hasattr(stage, "REPLACEMENT_FILE"):
            monkeypatch.setattr(stage, "REPLACEMENT_FILE", files[common.REPLACEMENT_CLIP])
    monkeypatch.setattr(common_3, "MAKE_SEQ_TEMPLATE", FAKE_MAKE_SEQ)
    for owner in (common, common_3, common_a):
        monkeypatch.setattr(owner, "OUTPUT_DIR", tmp_path / "out")
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
        "_out_path": out / "p3_5_scenarios_e2e.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261006_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame, scripted


def test_the_scenarios_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, scripted = run_script(monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    assert len(names) >= 40
    for stage in ("a1_", "a3_", "a7_", "a8_", "a10_", "a11_", "a15_", "a18_"):
        assert any(n.startswith(stage) for n in names), stage
    for stage in ("n1_", "n2_", "n3_", "n4_", "b1_", "b2_", "b4_", "b6_", "b7_", "b8_", "z_"):
        assert any(n.startswith(stage) for n in names), stage
    assert flame.exited is True
    assert SCENARIO_OPS <= set(results["opsUsed"]) | set(results["opsViaPlan"])
    assert "segment.replace" in results["opsViaPlan"]
    assert "eval.run" not in results["opsUsed"] and "eval.run" not in results["opsViaPlan"]
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []
    assert results["env"] == {"FLAME_MCP_CHECKPOINT_LIBRARY": "P35_261006_000000_CKPT"}
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    registry = Registry()
    register_all(registry)
    op_names = {o.name for o in registry.list_ops()}
    labels = {c.label for c in scripted.calls if c.label}
    stray = sorted(
        label for label in labels if label not in op_names and not ALLOWED_LABELS.match(label)
    )
    assert stray == [], f"transport calls that are neither material, cleanup nor a read: {stray}"


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("p3_5_scenarios_e2e")
    assert module.dry_run() == 0
    out = capsys.readouterr().out
    assert "dry run: OK" in out and "a_plan_replace: plan.run" in out
    assert "b_copy_out: segment.copy_to_media_panel" in out and "make_seq (" in out


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    module = load("p3_5_scenarios_e2e")
    registry = Registry()
    register_all(registry)
    used = {op for op, _build in module.CALLS.values()}
    assert used <= {o.name for o in registry.list_ops()}
    assert SCENARIO_OPS - {"journal.revert", "segment.replace"} <= used
    assert "eval.run" not in used
