"""Runs scripts/p3_2a_checkpoint_e2e.py against the FAKE Flame in this process.

The script runs end to end (tests/e2e_inprocess.py). NOT evidence about a real Flame: it shows
that the stages, selectors and judgements of the script hang together and agree with what the fake
does, and that the pure parts (the dry run) compile. The fake is modelled on Flame 2025.2.4
measurements of segment trim and of Timeline copy, move and restore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import e2e_inprocess
from fake_media_tree import FakeFlame
from test_p2_3d_e2e_inprocess import failed_steps, load
from timeline_p32a_helpers import media

FAKE_S1 = """
import timeline_p32a_helpers as _h
import fake_media_tree as _f
_reel = flame.find_by_wiretap_node_id(@REEL@)
_name = @NAME@
_puts = @PUTS@
if len(_puts) == 1:
    _src = _v(flame.find_by_wiretap_node_id(_puts[0][1]).name)
    _tracks = [[_h.media(_src, 0, 23)]]
else:
    _letterbox = _f.PyTimelineFX('Burn-in Letterbox', {'mode': 'Custom', 'ratio': 1.5})
    _tracks = [
        [
            _h.media('clip1', 0, 23),
            _h.gap(24, 47),
            _h.media('clip2', 48, 71, effects=(_letterbox,)),
            _h.media('p2_len48', 72, 119),
        ],
        [
            _h.gap(0, 9),
            _h.media('p2_len72', 10, 61, source_in=10, head=10, tail=10),
            _h.gap(62, 71),
        ],
    ]
_seq = _f.PySequence(
    _name, wiretap_id=_reel.get_wiretap_node_id() + '/' + _name, duration=120,
    versions=[_f.PyVersion([_f.PyTrack(_t) for _t in _tracks])],
)
_f.adopt(_reel, [_seq])
_reel._children.append(_seq)
_result = {'wt': _seq.get_wiretap_node_id(), 'uid': _v(_seq.uid), 'puts': [],
           'name': _v(_seq.name), 'notes': []}
"""


class FakeExporter:
    """`flame.PyExporter`: one file per frame whose bytes say which source frames the sequence
    shows there (so an edit changes some files and a faithful restore changes none)."""

    def __init__(self) -> None:
        self.foreground = False

    def export(self, seq: Any, preset: str, out_dir: str) -> None:
        name = str(seq.name.get_value())
        total = max(s.record_out.frame for t in seq.versions[0].tracks for s in t.segments) + 1
        for frame in range(total):
            shown = [
                (s.source_name, s.source_in.frame + frame - s.record_in.frame)
                for t in seq.versions[0].tracks
                for s in t.segments
                if s.type != "Gap" and s.record_in.frame <= frame <= s.record_out.frame
            ]
            (Path(out_dir) / f"{name}.{frame:08d}.tif").write_text(repr(shown))


def file_md5_listing(root: Path) -> dict[str, Any]:
    import hashlib

    files = [
        {"rel": str(p.relative_to(root)), "md5": hashlib.md5(p.read_bytes()).hexdigest()}
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]
    return {"dir": str(root), "count": len(files), "files": files, "md5_kind": "file md5 (fake)"}


def run_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], FakeFlame, e2e_inprocess.HarnessExec]:
    module = load("p3_2a_checkpoint_e2e")
    common = load("p3_2a_e2e_common")
    stages = [load("p3_2a_e2e_stages"), load("p3_2a_e2e_trim")]
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    files = []
    for name in ("clip1", "clip2", "p2_len48", "p2_len72"):
        (media_dir / f"{name}.mov").write_bytes(b"x" * 32)
        files.append((name, media_dir / f"{name}.mov"))
    preset = tmp_path / "preset.xml"
    preset.write_text("<preset/>")
    monkeypatch.setattr(common, "CLIP_FILES", tuple(files))
    monkeypatch.setattr(common, "MAKE_S1_TEMPLATE", FAKE_S1)
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(common, "PRESET_SOURCE", preset)
    monkeypatch.setattr(stages[0], "FP_ATTR", ("colour", [0.5, 0.5, 0.5]))
    monkeypatch.setattr(module, "pixel_hash_dir", file_md5_listing)
    monkeypatch.setenv(common.CKPT_ENV, "")
    flame = FakeFlame()
    flame.PyExporter = FakeExporter  # type: ignore[attr-defined]
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
        "_out_path": out / "p3_2a_checkpoint_e2e.json",
    }
    ctx = module.Ctx(tmp_path / "run", results, "261002_000000", journal_dir)
    (tmp_path / "run").mkdir()
    e2e_inprocess.run(module, ctx)
    return results, flame, scripted


def test_the_checkpoint_e2e_runs_to_the_end_against_the_fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results, flame, _scripted = run_script(monkeypatch, tmp_path)
    assert failed_steps(results) == [], json.dumps(failed_steps(results), indent=1, default=str)
    assert results["ok"] is True and "fatal_error" not in results
    names = [s["name"] for s in results["steps"]]
    assert len(names) >= 90
    for stage in ("a1_", "a5_", "b0_", "b1_", "b3_", "c1_", "c4_", "c8_", "c9_", "c14_", "d1_"):
        assert any(n.startswith(stage) for n in names), stage
    for stage in (
        "d6_",
        "d8_",
        "e2_",
        "e4_",
        "e5_",
        "f2_",
        "g1_",
        "g2_",
        "h2_",
        "i1_",
        "j1_",
        "j2_",
        "k_",
    ):
        assert any(n.startswith(stage) for n in names), stage
    assert flame.exited is True
    assert {
        "timeline.fingerprint",
        "timeline.checkpoint",
        "segment.trim",
        "journal.revert",
        "journal.restore_checkpoint",
        "journal.check",
        "journal.list_checkpoints",
    } <= set(results["opsUsed"])
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.batch_groups == [] and desktop.reel_groups == []
    assert flame.projects.current_project.current_workspace.libraries == []
    assert results["env"] == {"FLAME_MCP_CHECKPOINT_LIBRARY": "P32A_261002_000000_CKPT"}


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load("p3_2a_checkpoint_e2e")
    assert module.dry_run() == 0
    out = capsys.readouterr().out
    assert "dry run: OK" in out and "c8_tail_extend_ripple: segment.trim" in out


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    module = load("p3_2a_checkpoint_e2e")
    registry = Registry()
    register_all(registry)
    used = {op for op, _build in module.CALLS.values()}
    assert used <= {o.name for o in registry.list_ops()}
    assert {"timeline.fingerprint", "timeline.checkpoint", "segment.trim", "plan.run"} <= used
