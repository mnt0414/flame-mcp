"""Helpers shared by the Timeline checkpoint, restore and trim op tests.

They are used by test_timeline_checkpoint_p32a.py, test_timeline_restore_p32a.py and
test_segment_trim_p32a.py: the rig (the fake Flame tree of journal_test_helpers.py with a few
sequences on the Desktop and a library copy of one), selectors and the helpers that read the fake
directly -- the way the real-Flame e2e reads state back by transport, never through the ops under
test. The fake follows what was measured on Flame 2025.2.4 for segment trim and for Timeline copy,
move and restore (fake_media_tree.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import (
    PyClip,
    PySegment,
    PySequence,
    PyTimelineFX,
    PyTrack,
    PyVersion,
    adopt,
    uid_of,
)
from flame_mcp import timeline_checkpoints
from journal_test_helpers import Rig, make_rig

REEL_WT = "/wt/RG/Reel 1"
S1_WT = f"{REEL_WT}/S1"
S2_WT = f"{REEL_WT}/S2"
S3_WT = f"{REEL_WT}/S3"
CLIPX_WT = f"{REEL_WT}/CLIPX"
LIB_NAME = "P32A_TEST_CKPT"
TRIM = "segment.trim"
CHECKPOINT = "timeline.checkpoint"
FINGERPRINT = "timeline.fingerprint"


def wt(wiretap_id: str) -> dict[str, Any]:
    return {"kind": "wiretap", "id": wiretap_id}


S1 = wt(S1_WT)
S2 = wt(S2_WT)
S3 = wt(S3_WT)
CLIPX = wt(CLIPX_WT)


def seg_sel(
    parent: dict[str, Any],
    track: int,
    *,
    index: int | None = None,
    record_in: int | None = None,
    version: int = 0,
) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": "segment", "parent": parent, "version": version, "track": track}
    if index is not None:
        out["index"] = index
    if record_in is not None:
        out["record_in"] = record_in
    return out


def media(
    source: str,
    record_in: int,
    record_out: int,
    *,
    source_in: int = 0,
    head: int = 0,
    tail: int = 0,
    effects: tuple[PyTimelineFX, ...] = (),
    name: str = "s",
) -> PySegment:
    length = record_out - record_in + 1
    return PySegment(
        name=name,
        source_name=source,
        record_in=record_in,
        record_out=record_out,
        source_in=source_in,
        source_out=source_in + length - 1,
        record_duration=length,
        head=head,
        tail=tail,
        effects=effects,
    )


def gap(record_in: int, record_out: int) -> PySegment:
    return PySegment(
        name="",
        source_name="",
        record_in=record_in,
        record_out=record_out,
        source_in=0,
        source_out=record_out - record_in,
        record_duration=record_out - record_in + 1,
        type="Gap",
        head="infinite",
        tail="infinite",
    )


def letterbox(ratio: float = 1.5) -> PyTimelineFX:
    return PyTimelineFX("Burn-in Letterbox", {"mode": "Custom", "ratio": ratio})


def make_s1() -> list[list[PySegment]]:
    """track 0: Gap 0-9 | A clip1 10-33 (src 5-28, head 5, tail 5) | Gap 34-39 | B clip2 40-63
    (Burn-in Letterbox, handles 0); track 1: C clip3 0-47 (head 3, tail 2). record_out and
    source_out count the last frame in, as Flame does."""
    return [
        [
            gap(0, 9),
            media("clip1", 10, 33, source_in=5, head=5, tail=5, name="A"),
            gap(34, 39),
            media("clip2", 40, 63, effects=(letterbox(),), name="B"),
        ],
        [media("clip3", 0, 47, source_in=3, head=3, tail=2, name="C")],
    ]


def add_sequence(reel: Any, name: str, tracks: list[list[PySegment]], *, wiretap: str) -> Any:
    seq = PySequence(
        name,
        wiretap_id=wiretap,
        duration=max(seg.record_out.frame for track in tracks for seg in track) + 1,
        versions=[PyVersion([PyTrack(track) for track in tracks])],
    )
    adopt(reel, [seq])
    reel._children.append(seq)
    return seq


def make_timeline_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: Any) -> Rig:
    monkeypatch.setenv(timeline_checkpoints.LIBRARY_ENV, LIB_NAME)
    rig = make_rig(monkeypatch, tmp_path, **kwargs)
    reel = rig.handles["reel"]
    rig.handles["s1"] = add_sequence(reel, "S1", make_s1(), wiretap=S1_WT)
    rig.handles["s2"] = add_sequence(reel, "S2", [[media("clip1", 0, 23)]], wiretap=S2_WT)
    rig.handles["s3"] = add_sequence(reel, "S3", [[media("clip2", 0, 23)]], wiretap=S3_WT)
    clip = PyClip(
        "CLIPX",
        wiretap_id=CLIPX_WT,
        duration=24,
        versions=[PyVersion([PyTrack([media("clip1", 0, 23, source_in=4, head=4, tail=4)])])],
    )
    adopt(reel, [clip])
    reel._children.append(clip)
    rig.handles["clipx"] = clip
    rig.handles["lib"] = rig.flame.find_by_wiretap_node_id("/projects/p/ws/P1O")
    rig.handles["libreel"] = rig.flame.find_by_wiretap_node_id("/projects/p/ws/P1O/R1")
    return rig


def rows(seq: Any, track: int = 0, version: int = 0) -> list[list[Any]]:
    """[type, record_in, record_out, source_in, source_out, head, tail] of every segment."""
    return [
        [
            s.type,
            s.record_in.frame,
            s.record_out.frame,
            s.source_in.frame,
            s.source_out.frame,
            s.head,
            s.tail,
        ]
        for s in seq.versions[version].tracks[track].segments
    ]


def uid(obj: Any) -> str:
    value = uid_of(obj)
    assert isinstance(value, str)
    return value


def libraries(rig: Rig) -> list[Any]:
    return list(rig.flame.projects.current_project.current_workspace.libraries)


def stash_library(rig: Rig) -> Any | None:
    found = [lib for lib in libraries(rig) if str(lib.name.get_value()) == LIB_NAME]
    assert len(found) <= 1
    return found[0] if found else None


def stash_entries(rig: Rig) -> list[Any]:
    lib = stash_library(rig)
    if lib is None:
        return []
    return [entry for reel in lib.reels for entry in reel.children]


def reel_names(container: Any) -> list[str]:
    return [str(c.name.get_value()) for c in container.children]


def reel_uids(container: Any) -> list[str]:
    return [uid(c) for c in container.children]


def fingerprint_of_target(rig: Rig, target: dict[str, Any]) -> str:
    env = rig.do(FINGERPRINT, {"target": target})
    assert env["ok"] is True, env
    value: str = env["result"]["fingerprint"]
    return value


def checkpoints(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r["type"] == "checkpoint"]


def take_checkpoint(
    rig: Rig, target: dict[str, Any] = S1, label: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"target": target}
    if label is not None:
        args["label"] = label
    env = rig.do(CHECKPOINT, args)
    assert env["ok"] is True, env
    result: dict[str, Any] = env["result"]
    return result


def trim(
    rig: Rig,
    segment: dict[str, Any],
    edge: str = "head",
    offset: int = 2,
    ripple: bool | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {"target": segment, "edge": edge, "offset": offset}
    if ripple is not None:
        args["ripple"] = ripple
    return rig.do(TRIM, args)


def a_seg(index: int = 1, track: int = 0, parent: dict[str, Any] = S1) -> dict[str, Any]:
    return seg_sel(parent, track, index=index)


def revert(rig: Rig, env: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return rig.do("journal.revert", {"group": env["journal"]["group"], **extra})


def library_copy(rig: Rig, source: str = "s1") -> Any:
    copies = rig.flame.media_panel.copy(rig.handles[source], rig.handles["libreel"])
    return copies[0]
