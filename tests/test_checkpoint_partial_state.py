"""A checkpointed write that ends in `partial:` is journaled as `partial`, not `failed`.

The engine behind every checkpointed write (`checkpointed.run_checkpointed`) writes the outcome;
an outcome with `verdict: "partial"` makes `IntentView.state` say `partial`, which is what lets
`journal.list` show it as something that changed the project and `newer_groups` name it as a later
group of the same target. Run through the real Runtime with a scripted `transport.exec` over the
fake Flame tree; each test bends the FAKE with one misbehaviour."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyTime
from flame_mcp.journal import Index
from flame_mcp.newer_groups import newer_groups
from journal_test_helpers import BG1, Rig, make_rig
from p33_helpers import add_media_clip, clip_sel
from timeline_p32a_helpers import S1, a_seg, make_timeline_rig, trim

BLUR = {"kind": "node", "batch": BG1, "name": "blur1"}


def states(rig: Rig) -> dict[str, str]:
    return {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}


def outcome_of(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.outcome is not None
    return dict(view.outcome)


def is_partial(env: dict[str, Any]) -> bool:
    return (
        env["ok"] is False
        and env["errorCode"] == "FLAME_API_ERROR"
        and str(env["error"]).startswith("partial:")
    )


def test_a_node_delete_that_ends_partial_is_journaled_as_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    batch = rig.handles["bg1"]
    victim = next(n for n in batch.nodes if n.name.get_value() == "blur1")
    other = next(n for n in batch.nodes if n.name.get_value() == "comp1")
    real = victim.delete

    def greedy(confirm: bool = True) -> bool:
        real(confirm)
        batch.remove_node(other)
        return True

    victim.delete = greedy
    env = rig.do("node.delete", {"target": BLUR})
    assert is_partial(env), env
    assert outcome_of(rig, env)["verdict"] == "partial"
    assert states(rig)[env["journal"]["group"]] == "partial"


def test_a_node_delete_that_fails_without_a_trace_stays_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    victim = next(n for n in rig.handles["bg1"].nodes if n.name.get_value() == "blur1")
    victim.delete = lambda confirm=True: True
    env = rig.do("node.delete", {"target": BLUR})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert "verdict" not in outcome_of(rig, env)
    assert states(rig)[env["journal"]["group"]] == "failed"


def test_a_partial_put_is_a_later_group_of_the_same_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_timeline_rig(monkeypatch, tmp_path)
    rig.handles["c12"] = add_media_clip(rig, "len12", start=0, length=12)
    edit = trim(rig, a_seg(1), "head", 2)
    assert edit["ok"] is True, edit
    seq = rig.handles["s1"]
    real = seq.overwrite

    def elsewhere(clip: Any, time: Any, track: Any) -> Any:
        return real(clip, PyTime(time.frame + 2), track)

    seq.overwrite = elsewhere
    put = rig.do(
        "timeline.overwrite",
        {"sequence": S1, "source": clip_sel(rig.handles["c12"]), "at": 20},
    )
    assert is_partial(put), put
    assert outcome_of(rig, put)["verdict"] == "partial"
    assert states(rig)[put["journal"]["group"]] == "partial"
    records = rig.records()
    later = newer_groups(records, Index(records), edit["journal"]["group"])
    assert [row["group"] for row in later] == [put["journal"]["group"]]
