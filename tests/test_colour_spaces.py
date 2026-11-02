"""`timeline.colour_spaces` and the colour check of `render.export`.

The judgement is `colour_check.judge` (pure); the Flame-side read (`colourspace_prelude._cs_scan`)
runs here against fake segments that answer `get_colour_space`. Which attribute real Flame answers
it from is measured on real Flame, not here."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PySegment
from flame_mcp.colour_check import judge, mixed_message
from journal_test_helpers import Rig
from timeline_p32a_helpers import CLIPX, add_sequence, gap, make_timeline_rig, media

COLOUR = "timeline.colour_spaces"
EXPORT = "render.export"


class ColourSegment(PySegment):
    """A segment that answers get_colour_space (`space` may be an exception to raise)."""

    space: Any = "ACEScg"

    def get_colour_space(self, time: Any = None) -> Any:
        if isinstance(self.space, Exception):
            raise self.space
        return self.space


def coloured(seg: PySegment, space: Any) -> PySegment:
    seg.__class__ = ColourSegment
    seg.space = space  # type: ignore[attr-defined]
    return seg


def scan(spaces: dict[str, int], *, unreadable: int = 0, complete: bool = True) -> dict[str, Any]:
    return {
        "spaces": {n: {"count": c, "first": []} for n, c in spaces.items()},
        "unreadable": {"count": unreadable, "first": []},
        "segments": sum(spaces.values()) + unreadable,
        "complete": complete,
        "error": None,
    }


def test_two_colour_spaces_are_mixed() -> None:
    verdict = judge(scan({"ACEScg": 3, "Rec.709": 1}))
    assert verdict["mixed"] is True
    assert [s["name"] for s in verdict["spaces"]] == ["ACEScg", "Rec.709"]
    assert "'ACEScg' x3" in mixed_message(verdict)


def test_one_colour_space_is_not_mixed() -> None:
    verdict = judge(scan({"ACEScg": 4}))
    assert verdict["mixed"] is False and verdict["complete"] is True
    assert verdict["warnings"] == []


def test_an_unreadable_segment_is_a_warning_and_never_a_mix() -> None:
    verdict = judge(scan({"ACEScg": 2}, unreadable=1))
    assert verdict["mixed"] is False
    assert verdict["complete"] is False
    assert any("1 segment" in w for w in verdict["warnings"])


def test_a_scan_that_stopped_early_is_incomplete_not_mixed() -> None:
    verdict = judge(scan({"ACEScg": 2}, complete=False))
    assert verdict["mixed"] is False and verdict["complete"] is False
    assert verdict["warnings"]


def test_a_scan_with_no_answer_is_incomplete() -> None:
    verdict = judge(None)
    assert verdict["mixed"] is False and verdict["complete"] is False and verdict["warnings"]


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def make_sequence(rig: Rig, name: str, tracks: list[list[PySegment]]) -> dict[str, Any]:
    reel = rig.handles["reel"]
    add_sequence(reel, name, tracks, wiretap=f"{reel.get_wiretap_node_id()}/{name}")
    return {"kind": "wiretap", "id": f"{reel.get_wiretap_node_id()}/{name}"}


def test_the_op_reads_every_track_and_version_and_counts_a_gap(rig: Rig) -> None:
    target = make_sequence(
        rig,
        "CS1",
        [
            [gap(0, 9), coloured(media("a", 10, 19), "ACEScg")],
            [coloured(media("b", 0, 9), "ACEScg"), coloured(media("c", 10, 19), "Rec.709")],
        ],
    )
    env = rig.do(COLOUR, {"target": target})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["mixed"] is True
    assert {s["name"]: s["count"] for s in result["spaces"]} == {"ACEScg": 2, "Rec.709": 1}
    assert result["gaps"] == 1 and result["segments"] == 3
    rows = {s["name"]: s["first"] for s in result["spaces"]}
    assert {"version": 0, "track": 1, "index": 1} in rows["Rec.709"]


def test_a_segment_whose_read_raises_is_unreadable_not_mixed(rig: Rig) -> None:
    target = make_sequence(
        rig,
        "CS2",
        [[coloured(media("a", 0, 9), "ACEScg"), coloured(media("b", 10, 19), RuntimeError("no"))]],
    )
    result = rig.do(COLOUR, {"target": target})["result"]
    assert result["mixed"] is False
    assert result["unreadable"] == 1 and result["complete"] is False


def test_a_clip_is_a_selector_type_error(rig: Rig) -> None:
    env = rig.do(COLOUR, {"target": {"kind": "wiretap", "id": "/wt/RG/Reel 1/CLIPX"}})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE"


@pytest.fixture
def preset(tmp_path: Path) -> Path:
    path = tmp_path / "image.xml"
    path.write_text("<preset version='14'><type>image</type></preset>", encoding="utf-8")
    return path


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


def test_export_of_a_mixed_sequence_is_refused_before_anything_is_journaled(
    rig: Rig, preset: Path, out: Path
) -> None:
    target = make_sequence(
        rig,
        "CS3",
        [[coloured(media("a", 0, 9), "ACEScg"), coloured(media("b", 10, 19), "Rec.709")]],
    )
    env = rig.do(EXPORT, {"target": target, "preset": str(preset), "output_dir": str(out)})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["colourSpaces"]["mixed"] is True
    assert env["result"]["ran"] is False
    assert [r for r in rig.records() if r.get("type") == "intent"] == []
    assert list(out.iterdir()) == []


def test_export_of_a_single_space_sequence_goes_on_and_keeps_the_check(
    rig: Rig, preset: Path, out: Path
) -> None:
    target = make_sequence(rig, "CS4", [[coloured(media("a", 0, 9), "ACEScg")]])
    env = rig.do(EXPORT, {"target": target, "preset": str(preset), "output_dir": str(out)})
    assert env["errorCode"] != "PRECONDITION_UNMET", env
    assert env["result"]["colourSpaces"]["mixed"] is False


def test_export_with_an_unreadable_segment_is_refused(rig: Rig, preset: Path, out: Path) -> None:
    target = make_sequence(
        rig,
        "CS5",
        [[coloured(media("a", 0, 9), "ACEScg"), coloured(media("b", 10, 19), RuntimeError("x"))]],
    )
    env = rig.do(EXPORT, {"target": target, "preset": str(preset), "output_dir": str(out)})
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["colourSpaces"]["unreadable"] == 1


def test_export_of_a_clip_does_not_run_the_check(rig: Rig, preset: Path, out: Path) -> None:
    env = rig.do(EXPORT, {"target": CLIPX, "preset": str(preset), "output_dir": str(out)})
    assert "colourSpaces" not in (env.get("result") or {})
