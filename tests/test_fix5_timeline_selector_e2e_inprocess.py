"""scripts/fix5_timeline_selector_e2e.py run end to end against the FAKE Flame in this process
(tests/e2e_inprocess.py). NOT evidence about a real Flame: it shows that the stages, selectors and
judgements of the script hang together and agree with what the fake does, and that the pure parts
(the dry run) work."""

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


def run_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict[str, Any], FakeFlame]:
    module = load("fix5_timeline_selector_e2e")
    common = load("p3_3_e2e_common")
    common_a = load("p3_2a_e2e_common")
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    files = []
    for name in MEDIA_TABLE:
        (media_dir / f"{name}.mov").write_bytes(b"x" * 32)
        files.append((name, media_dir / f"{name}.mov"))
    wanted = dict(files)
    monkeypatch.setattr(module, "CLIP_FILES", tuple((n, wanted[n]) for n in ("clip1", "p2_len72")))
    monkeypatch.setattr(common, "MAKE_SEQ_TEMPLATE", FAKE_MAKE_SEQ)
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(common_a, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setenv(common_a.CKPT_ENV, "")
    flame = FakeFlame()
    monkeypatch.setattr(FakeFlame, "import_clips", import_with_media(FakeFlame))
    e2e_inprocess.install(module, monkeypatch, flame, tmp_path)
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
        "_out_path": out / "fix5_timeline_selector_e2e.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261008_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame


def test_the_timeline_selector_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame = run_script(monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    for stage in ("a1_", "a2_", "t2_", "t3_", "t4_", "t6_", "p2_", "p3_", "s_", "z_"):
        assert any(n.startswith(stage) for n in names), stage
    assert flame.exited is True
    assert {
        "desktop.info",
        "timeline.info",
        "timeline.list_segments",
        "timeline.fingerprint",
        "timeline.checkpoint",
        "timeline.create_version",
        "timeline.overwrite",
        "timeline.insert",
        "segment.trim",
        "plan.run",
        "journal.revert",
    } <= set(results["opsUsed"])
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []
    assert results["env"] == {"FLAME_MCP_CHECKPOINT_LIBRARY": "PR5E_261008_000000_CKPT"}


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("fix5_timeline_selector_e2e")
    assert module.dry_run() == 0
    out = capsys.readouterr().out
    assert "dry run: OK" in out and "t_trim: segment.trim" in out and "open (" in out


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    module = load("fix5_timeline_selector_e2e")
    registry = Registry()
    register_all(registry)
    used = {op for op, _build in module.CALLS.values()}
    assert used <= {o.name for o in registry.list_ops()}
    assert {"segment.trim", "plan.run", "timeline.fingerprint", "desktop.info"} <= used
