"""Shared rig and helpers for the media op tests.

Used by test_ops_import_p31b.py, test_ops_duplicate_p31b.py, test_ops_move_p31b.py and
test_ops_media_surface_p31b.py: the rig (the fake Flame tree of journal_test_helpers.py with a
second Desktop reel), selectors and the helpers that build material. The fake follows what was
measured on Flame 2025.2.4 (see fake_media_tree.py)."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import (
    PyClip,
    PySegment,
    PySequence,
    PyTrack,
    PyVersion,
    adopt,
)
from journal_test_helpers import Rig, make_rig

REEL1_WT = "/wt/RG/Reel 1"
LIB_WT = "/projects/p/ws/P1O"
LIBREEL_WT = "/projects/p/ws/P1O/R1"
CLIP1_WT = "/projects/p/ws/P1O/R1/clip1"
CLIP2_WT = "/projects/p/ws/P1O/R1/clip2"
C1_WT = "/wt/c1"
REEL1 = {"kind": "wiretap", "id": REEL1_WT}
LIBREEL = {"kind": "wiretap", "id": LIBREEL_WT}
LIB = {"kind": "wiretap", "id": LIB_WT}
CLIP1 = {"kind": "wiretap", "id": CLIP1_WT}
CLIP2 = {"kind": "wiretap", "id": CLIP2_WT}
C1 = {"kind": "wiretap", "id": C1_WT}
IMPORT = "mediahub.import"
DUP = "desktop.duplicate"
MOVE = "desktop.move"
ALL_OPS = (IMPORT, DUP, MOVE)


def make_media_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_rig(monkeypatch, tmp_path)
    made.handles["lib"] = made.flame.find_by_wiretap_node_id(LIB_WT)
    made.handles["libreel"] = made.flame.find_by_wiretap_node_id(LIBREEL_WT)
    made.handles["clip1"] = made.flame.find_by_wiretap_node_id(CLIP1_WT)
    made.handles["clip2"] = made.flame.find_by_wiretap_node_id(CLIP2_WT)
    made.handles["reel2"] = made.handles["reel_group"].create_reel("Reel 2")
    return made


def wt(obj: Any) -> dict[str, Any]:
    return {"kind": "wiretap", "id": obj.get_wiretap_node_id()}


def names(container: Any) -> list[str]:
    return [str(c.name.get_value()) for c in container.children]


def uids(container: Any) -> list[str]:
    return [str(c.uid.get_value()) for c in container.children]


def add_seq(reel: Any, name: str = "S1", frames: int = 48) -> PySequence:
    seg = PySegment(
        name="s", source_name="clip1", record_in=0, record_out=frames - 1, record_duration=frames
    )
    seq = PySequence(
        name,
        wiretap_id=f"{reel.get_wiretap_node_id()}/{name}",
        duration=frames,
        versions=[PyVersion([PyTrack([seg])])],
    )
    adopt(reel, [seq])
    reel._children.append(seq)
    return seq


def names_to_uid(rig: Any, name: str) -> list[str]:
    reel = rig.handles["reel_group"].reels[0]
    return [str(c.uid.get_value()) for c in reel.children if str(c.name.get_value()) == name]


def add_clip(reel: Any, name: str) -> PyClip:
    clip = PyClip(name, wiretap_id=f"{reel.get_wiretap_node_id()}/{name}")
    adopt(reel, [clip])
    reel._children.append(clip)
    return clip


def batch_reel(rig: Rig) -> Any:
    return rig.handles["bg1"].create_reel("Schematic Reel 1")


def media(tmp_path: Path, *file_names: str, size: int = 16) -> list[str]:
    made = []
    for file_name in file_names:
        path = tmp_path / file_name
        path.write_bytes(b"x" * size)
        made.append(str(path))
    return made


def revert(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    return rig.do("journal.revert", {"group": env["journal"]["group"]})


def states(rig: Rig) -> dict[str, str]:
    return {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}


def types_in(rig: Rig) -> list[str]:
    return [str(r.get("type")) for r in rig.records()]


def op_write_calls(rig: Rig, label: str) -> int:
    return len(write_codes(rig, label))


def own_source(code: str) -> str:
    """The op's own generated source inside the wrapped script (`__op_src = '...'`)."""
    line = next(ln for ln in code.splitlines() if ln.startswith("__op_src = "))
    value = ast.literal_eval(line.removeprefix("__op_src = "))
    assert isinstance(value, str)
    return value


def write_codes(rig: Rig, label: str) -> list[str]:
    """The op's own source of every call of `label` that carried the WRITE code."""
    needles = ("_result = _dm_import(", "_result = _dm_duplicate(", "_result = _dm_move(")
    return [
        own_source(c.code)
        for c in rig.exec.calls
        if c.label == label and any(n in own_source(c.code) for n in needles)
    ]


def imp(rig: Rig, paths: list[str], destination: dict[str, Any]) -> dict[str, Any]:
    return rig.do(IMPORT, {"paths": paths, "destination": destination})


def dup(rig: Rig, target: dict[str, Any]) -> dict[str, Any]:
    return rig.do(DUP, {"target": target})


def mv(rig: Rig, sources: list[dict[str, Any]], destination: dict[str, Any]) -> dict[str, Any]:
    return rig.do(MOVE, {"sources": sources, "destination": destination})


def reel2_selector(rig: Rig) -> dict[str, Any]:
    return wt(rig.handles["reel2"])


def drop_recorded_fingerprints(rig: Rig) -> None:
    """Rewrite the journal as an older server wrote it: `created` has no content fingerprint."""

    def strip(node: Any) -> None:
        if isinstance(node, dict):
            if "fingerprint" in node and "name" in node:
                node.pop("fingerprint")
            for value in node.values():
                strip(value)
        elif isinstance(node, list):
            for value in node:
                strip(value)

    path = next(rig.journal.directory.glob("journal_*.jsonl"))
    lines = []
    for line in path.read_text().splitlines():
        record = json.loads(line)
        strip(record.get("created"))
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")
