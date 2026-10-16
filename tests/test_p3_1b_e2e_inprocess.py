"""Runs scripts/p3_1b_write_e2e.py against the FAKE Flame in this process.

The script runs end to end (tests/e2e_inprocess.py). NOT evidence about a real Flame: it shows
that the stages, selectors and judgements of the script hang together and agree with what the fake
does, and that the pure parts (the dry run) compile. The fake is modelled on Flame 2025.2.4
measurements of import, duplicate, move and Timeline copy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import e2e_inprocess
from fake_media_tree import FakeFlame
from test_p2_3d_e2e_inprocess import failed_steps, load

FAKE_SEQUENCE = """
import fake_media_tree as _f
_reel = flame.find_by_wiretap_node_id(@REEL@)
_name = @NAME@
_segments = {}
for _track, _clip_wt, _rel in @PUTS@:
    _clip = flame.find_by_wiretap_node_id(_clip_wt)
    _segments.setdefault(_track, []).append(
        _f.PySegment(
            name='s', source_name=_v(_clip.name), record_in=_rel - 1, record_out=_rel + 22,
            record_duration=24,
        )
    )
_tracks = [_f.PyTrack(_segments.get(_i, [])) for _i in range(@TRACKS@)]
_seq = _f.PySequence(
    _name, wiretap_id=_reel.get_wiretap_node_id() + '/' + _name, duration=96,
    versions=[_f.PyVersion(_tracks)],
)
_f.adopt(_reel, [_seq])
_reel._children.append(_seq)
_result = {'wt': _seq.get_wiretap_node_id(), 'uid': _v(_seq.uid), 'puts': [],
           'name': _v(_seq.name), 'note': None}
"""


def run_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], FakeFlame, e2e_inprocess.HarnessExec]:
    module = load("p3_1b_write_e2e")
    common = load("p3_1b_e2e_common")
    stages = [load("p3_1b_e2e_import_dup"), load("p3_1b_e2e_move")]
    media = tmp_path / "media"
    media.mkdir()
    files = []
    for name in ("clip1", "clip2", "clip3"):
        (media / f"{name}.mov").write_bytes(b"x" * 32)
        files.append((name, media / f"{name}.mov"))
    monkeypatch.setattr(common, "CLIP_FILES", tuple(files))
    monkeypatch.setattr(common, "SEQUENCE_TEMPLATE", FAKE_SEQUENCE)
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path / "out")
    flame = FakeFlame()
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
        "_out_path": out / "p3_1b_write_e2e.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261001_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame, scripted


def test_the_write_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, _scripted = run_script(monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    assert len(names) >= 60
    for stage in ("a1_", "a7_", "a10_", "a12_", "a14_", "b1_", "b2_", "b3_", "b5_", "b8_", "c1_"):
        assert any(n.startswith(stage) for n in names), stage
    for stage in ("c3_", "c5_", "c6_", "c7_", "c8_", "c13_", "c15_", "e1_", "e2_", "g_", "z_"):
        assert any(n.startswith(stage) for n in names), stage
    assert flame.exited is True
    assert {"mediahub.import", "desktop.duplicate", "desktop.move"} <= set(results["opsUsed"])
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    module = load("p3_1b_write_e2e")
    assert module.dry_run() == 0
    assert "dry run: OK" in capsys.readouterr().out
    app_support = tmp_path / "Library" / "Application Support"
    placeholders = app_support / "flame-mcp" / "scratch" / "p31b_dry"
    assert (placeholders / "empty.mov").is_file()


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    module = load("p3_1b_write_e2e")
    registry = Registry()
    register_all(registry)
    used = {op for op, _build in module.CALLS.values()}
    assert used <= {o.name for o in registry.list_ops()}
    assert {"mediahub.import", "desktop.duplicate", "desktop.move", "plan.run"} <= used
