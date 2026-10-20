"""Shared rig and helpers for the `desktop.reformat` tests.

Used by test_desktop_reformat.py and test_desktop_reformat_journal.py: the timeline rig (the fake
Flame tree with CLIPX, a few sequences and a library clip) plus extra clips of other formats, and
the helpers that read the fake directly -- the way the real-Flame e2e reads state back, never
through the op under test. The fake clip follows what was measured on Flame 2025.2.4 for
`PyClip.reformat` and `change_start_frame` (fake_media_tree.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyClip, PyTrack, PyVersion, adopt
from journal_test_helpers import Rig
from p32b_helpers import CLIPX, DELETE, LIB_CLIP1, REEL1, S1, reel2, wt
from timeline_p32a_helpers import (
    checkpoints,
    make_timeline_rig,
    media,
    reel_names,
    stash_entries,
    stash_library,
    uid,
)

OP = "desktop.reformat"
DUP = "desktop.duplicate"

__all__ = [
    "CLIPX",
    "DELETE",
    "DUP",
    "LIB_CLIP1",
    "OP",
    "REEL1",
    "S1",
    "add_media_clip",
    "calls_of",
    "checkpoints",
    "clip_format",
    "first_record",
    "make_reformat_rig",
    "reel",
    "reel2",
    "reel_names",
    "reformat",
    "revert",
    "stash_entries",
    "stash_library",
    "touched_clips",
    "uid",
    "untouched_world",
    "wt",
]


def make_reformat_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: Any) -> Rig:
    rig = make_timeline_rig(monkeypatch, tmp_path, **kwargs)
    rig.handles["reel2"] = rig.handles["reel_group"].create_reel("Reel 2")
    rig.handles["a"] = add_media_clip(reel(rig), "A", width=1280, height=720, ratio=1280 / 720)
    rig.handles["b"] = add_media_clip(reel(rig), "B", width=3840, height=2160, bit_depth=16)
    rig.handles["sq"] = add_media_clip(reel(rig), "SQ", width=1000, height=1000, ratio=1.0)
    return rig


def reel(rig: Rig) -> Any:
    return rig.handles["reel"]


def add_media_clip(container: Any, name: str, **fields: Any) -> PyClip:
    fields.setdefault("versions", [PyVersion([PyTrack([media("clip1", 0, 23)])])])
    clip = PyClip(name, wiretap_id=f"{container.get_wiretap_node_id()}/{name}", **fields)
    adopt(container, [clip])
    container._children.append(clip)
    return clip


def clip_format(clip: Any) -> dict[str, Any]:
    return {
        "width": clip.width,
        "height": clip.height,
        "ratio": clip.ratio,
        "bit_depth": clip.bit_depth,
        "scan_mode": clip.scan_mode,
        "frame_rate": clip.frame_rate,
        "start_frame": clip.start_frame,
    }


def reformat(
    rig: Rig,
    targets: list[dict[str, Any]] | None = None,
    width: int = 960,
    height: int = 540,
    **extra: Any,
) -> dict[str, Any]:
    return rig.do(
        OP,
        {
            "targets": targets if targets is not None else [CLIPX],
            "width": width,
            "height": height,
            **extra,
        },
    )


def revert(rig: Rig, env: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return rig.do("journal.revert", {"group": env["journal"]["group"], **extra})


CALL_MARKERS = {
    "snapshot": "_result = _rf_snapshot(",
    "stash": "_result = _rf_stash(",
    "write": "_result = _rf_write(",
    "duplicate": "_result = _dm_duplicate(",
}


def first_record(rig: Rig, kind: str) -> dict[str, Any]:
    return next(r for r in rig.records() if r["type"] == kind)


def calls_of(rig: Rig, kind: str) -> list[Any]:
    """The Flame calls of the op that are of `kind` (`snapshot`, `stash`, `write`, `duplicate`):
    the op's own line in the generated code, not the prelude that defines the functions."""
    marker = CALL_MARKERS[kind]
    return [c for c in rig.exec.calls if c.label == OP and marker in c.code]


def touched_clips(rig: Rig) -> list[Any]:
    return [
        clip
        for clip in [rig.handles[name] for name in ("clipx", "a", "b", "sq")]
        if clip.reformat_calls or clip.start_frame_calls
    ]


def untouched_world(rig: Rig) -> bool:
    return (
        stash_library(rig) is None
        and rig.records() == []
        and rig.flame.media_panel.copy_calls == []
        and rig.flame.duplicate_calls == []
        and touched_clips(rig) == []
    )
