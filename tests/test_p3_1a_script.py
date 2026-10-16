"""The pure parts of scripts/p3_1a_read_e2e.py (no Flame, no MCP server): the calls it makes
are valid for the registered ops and the code it sends compiles. What the script does against a
Flame is measured by running it."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str) -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


script = load("p3_1a_read_e2e")


def test_the_dry_run_compiles_every_call_and_every_transport_snippet(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert script.dry_run() == 0
    out = capsys.readouterr().out
    assert "dry run: OK" in out


def test_every_call_names_a_registered_op() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    registry = Registry()
    register_all(registry)
    assert {op for op, _build in script.CALLS.values()} <= {o.name for o in registry.list_ops()}
    assert {
        "segment.list_effects",
        "segment.shared_sources",
        "desktop.find",
        "desktop.info",
    } <= {op for op, _build in script.CALLS.values()}


def test_info_matches_compares_media_and_container_values() -> None:
    truth = {
        "type": "PySequence",
        "name": "S",
        "uid": "u",
        "parent": {"type": "PyReel", "name": "R"},
        "duration": 96,
        "startFrame": 1,
        "startTime": 0,
        "frameRate": "23.976 fps",
        "width": 1920,
        "height": 1080,
        "bitDepth": 10,
        "scanMode": "P",
        "cached": "Cached",
        "unlinked": "",
        "versions": [2],
        "audioTracks": 0,
    }
    got = {
        **truth,
        "duration": {"frame": 96, "timecode": "x"},
        "startTime": {"frame": 0, "timecode": "y"},
    }
    assert script.info_matches(got, truth, "media") is True
    assert script.info_matches({**got, "width": 1280}, truth, "media") is False
    box = {"type": "PyReel", "name": "R", "uid": "u", "parent": None, "childCounts": {"PyClip": 2}}
    assert script.info_matches(dict(box), box, "container") is True
    assert script.info_matches({**box, "childCounts": {"PyClip": 3}}, box, "container") is False
