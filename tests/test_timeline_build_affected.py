"""`timeline.overwrite` / `insert` / `cut` that end in `partial:` still say what they changed.

The op's code sets `_affected` before the error is raised, so the envelope's `affected` is 1 when
the sequence changed (and the journal can revert it), and 0 when nothing changed. Run through the
real Runtime with a scripted `transport.exec` over the fake Flame tree; each test bends the FAKE
with one misbehaviour."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyTime
from journal_test_helpers import Rig
from p33_helpers import add_media_clip, clip_sel
from timeline_p32a_helpers import S1, make_timeline_rig


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_timeline_rig(monkeypatch, tmp_path)
    made.handles["c12"] = add_media_clip(made, "len12", start=0, length=12)
    return made


def put(rig: Rig, op: str) -> dict[str, Any]:
    return rig.do(op, {"sequence": S1, "source": clip_sel(rig.handles["c12"]), "at": 20})


@pytest.mark.parametrize("op", ["timeline.overwrite", "timeline.insert"])
def test_a_partial_put_reports_the_change_as_affected(rig: Rig, op: str) -> None:
    seq = rig.handles["s1"]
    method = op.split(".")[1]
    real = getattr(seq, method)

    def elsewhere(clip: Any, time: Any, track: Any) -> Any:
        return real(clip, PyTime(time.frame + 2), track)

    setattr(seq, method, elsewhere)
    env = put(rig, op)
    assert env["ok"] is False and str(env["error"]).startswith("partial:"), env
    assert env["affected"] == 1


@pytest.mark.parametrize("op", ["timeline.overwrite", "timeline.insert"])
def test_a_put_that_changes_nothing_reports_nothing_affected(rig: Rig, op: str) -> None:
    setattr(rig.handles["s1"], op.split(".")[1], lambda clip, time, track: None)
    env = put(rig, op)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["affected"] == 0


def test_a_partial_whole_sequence_cut_reports_the_change_as_affected(rig: Rig) -> None:
    seq = rig.handles["s1"]
    seq.cut = lambda time: seq.versions[0].tracks[0].cut(time)
    env = rig.do("timeline.cut", {"sequence": S1, "at": 20, "scope": "all"})
    assert env["ok"] is False and str(env["error"]).startswith("partial:"), env
    assert env["affected"] == 1


def test_a_put_that_lands_where_asked_has_affected_1(rig: Rig) -> None:
    env = put(rig, "timeline.overwrite")
    assert env["ok"] is True, env
    assert env["affected"] == 1
