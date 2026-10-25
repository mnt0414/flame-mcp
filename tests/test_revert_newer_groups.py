"""`journal.revert` of a checkpointed group stops at a newer group of the same target.

A checkpoint restore overwrites the target with what the group's checkpoint held, so a later write
to the same target (a value write, a `desktop.reformat`, another checkpointed write) would vanish
without a word. The revert ends in CONFLICT before it restores and names those groups in
`result.newerGroups` (newest first); `force=true` restores anyway and still names them. Run through
the real Runtime with a scripted `transport.exec` over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr
from flame_mcp.journal import Index
from flame_mcp.newer_groups import newer_groups
from journal_test_helpers import BG1, Rig, make_rig
from p32b_helpers import MOVE, delete, make_p32b_rig, reel2
from reformat_helpers import CLIPX, clip_format, make_reformat_rig, reel, reformat
from timeline_p32a_helpers import REEL_WT, S1, a_seg, make_timeline_rig, seg_sel, trim, wt

BG2 = {"kind": "batch", "name": "BG2"}


@pytest.fixture
def brig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


@pytest.fixture
def trig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def node(name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": BG1, "name": name}


def group_of(env: dict[str, Any]) -> str:
    assert env["ok"] is True, env
    return str(env["journal"]["group"])


def newer_of(env: dict[str, Any]) -> list[str]:
    rows = (env.get("result") or {}).get("newerGroups") or []
    return [str(row["group"]) for row in rows]


def pos_x(rig: Rig, index: int = 1) -> float:
    return float(rig.handles["bg1"].nodes[index].pos_x.get_value())


def states(rig: Rig) -> dict[str, str]:
    return {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}


def test_batch_revert_stops_at_a_later_value_write_to_the_same_group(brig: Rig) -> None:
    made = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b1"}))
    value = group_of(
        brig.do("node.set_attributes", {"target": node("comp1"), "values": {"pos_x": 77.0}})
    )
    refused = brig.do("journal.revert", {"group": made})
    assert refused["errorCode"] == "CONFLICT", refused
    assert newer_of(refused) == [value]
    assert "newest first" in refused["error"] and refused["result"]["refused"] is True
    assert pos_x(brig) == 77.0
    assert [n.name.get_value() for n in brig.handles["bg1"].nodes].count("b1") == 1


def test_batch_revert_goes_through_once_the_newer_value_write_is_reverted(brig: Rig) -> None:
    made = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b1"}))
    value = group_of(
        brig.do("node.set_attributes", {"target": node("comp1"), "values": {"pos_x": 77.0}})
    )
    assert brig.do("journal.revert", {"group": value})["ok"] is True
    assert brig.do("journal.revert", {"group": made})["ok"] is True, "newer one reverted"


def test_batch_force_restores_anyway_and_still_names_the_newer_groups(brig: Rig) -> None:
    made = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b1"}))
    value = group_of(
        brig.do("node.set_attributes", {"target": node("comp1"), "values": {"pos_x": 77.0}})
    )
    out = brig.do("journal.revert", {"group": made, "force": True})
    assert out["ok"] is True, out
    assert newer_of(out) == [value]
    assert pos_x(brig) != 77.0


def test_batch_newer_groups_are_listed_newest_first_across_kinds(brig: Rig) -> None:
    made = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b1"}))
    first = group_of(
        brig.do("node.set_attributes", {"target": node("comp1"), "values": {"pos_x": 77.0}})
    )
    second = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b2"}))
    refused = brig.do("journal.revert", {"group": made})
    assert newer_of(refused) == [second, first]


def test_batch_write_to_another_batch_group_is_not_a_newer_group(brig: Rig) -> None:
    made = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b1"}))
    other = brig.do("node.create", {"target": BG2, "type": "Blur", "name": "o1"})
    assert other["ok"] is True, other
    assert brig.do("journal.revert", {"group": made})["ok"] is True


def test_batch_a_refused_write_and_a_read_are_not_newer_groups(brig: Rig) -> None:
    made = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b1"}))
    refused = brig.do("node.set_attributes", {"target": node("nope"), "values": {"pos_x": 5.0}})
    assert refused["ok"] is False, refused
    assert brig.do("node.get_attributes", {"target": node("comp1"), "keys": ["pos_x"]})["ok"]
    assert brig.do("journal.revert", {"group": made})["ok"] is True


def test_batch_force_is_journaled_with_the_groups_it_restored_over(brig: Rig) -> None:
    made = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b1"}))
    value = group_of(
        brig.do("node.set_attributes", {"target": node("comp1"), "values": {"pos_x": 77.0}})
    )
    assert brig.do("journal.revert", {"group": made, "force": True})["ok"] is True
    forced = [r["forced"] for r in brig.records() if r.get("type") == "intent" and r.get("forced")]
    assert [f["newerGroups"] for f in forced] == [[value]]


def test_timeline_revert_stops_at_a_later_segment_attribute_write(trig: Rig) -> None:
    seg = trig.handles["s1"].versions[0].tracks[0].segments[1]
    seg.comment = FakeAttr("old")
    seg.attributes.append("comment")
    edit = group_of(trim(trig, a_seg(1), "head", 2))
    value = group_of(
        trig.do("segment.set_attributes", {"target": a_seg(1), "values": {"comment": "mine"}})
    )
    refused = trig.do("journal.revert", {"group": edit})
    assert refused["errorCode"] == "CONFLICT", refused
    assert newer_of(refused) == [value]
    assert trig.handles["s1"].versions[0].tracks[0].segments[1].comment.get_value() == "mine"


def test_timeline_force_is_journaled_with_the_groups_it_restored_over(trig: Rig) -> None:
    edit = group_of(trim(trig, a_seg(1), "head", 2))
    later = group_of(trim(trig, a_seg(3), "tail", 1))
    assert trig.do("journal.revert", {"group": edit, "force": True})["ok"] is True
    forced = [r["forced"] for r in trig.records() if r.get("type") == "intent" and r.get("forced")]
    assert [f["newerGroups"] for f in forced] == [[later]]


def test_timeline_force_restores_anyway_and_names_the_newer_groups(trig: Rig) -> None:
    seg = trig.handles["s1"].versions[0].tracks[0].segments[1]
    seg.comment = FakeAttr("old")
    seg.attributes.append("comment")
    edit = group_of(trim(trig, a_seg(1), "head", 2))
    value = group_of(
        trig.do("segment.set_attributes", {"target": a_seg(1), "values": {"comment": "mine"}})
    )
    out = trig.do("journal.revert", {"group": edit, "force": True})
    assert out["ok"] is True, out
    assert newer_of(out) == [value]


def test_timeline_value_write_to_another_sequence_is_not_a_newer_group(trig: Rig) -> None:
    edit = group_of(trim(trig, a_seg(1), "head", 2))
    other_parent = {"kind": "wiretap", "id": "/wt/RG/Reel 1/S2"}
    other = trim(trig, a_seg(0, parent=other_parent), "tail", 1)
    assert other["ok"] is True, other
    assert trig.do("journal.revert", {"group": edit})["ok"] is True


def test_revert_of_an_edit_stops_at_a_later_reformat_of_the_same_clip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_reformat_rig(monkeypatch, tmp_path)
    edit = group_of(
        rig.do("segment.trim", {"target": seg_sel(CLIPX, 0, index=0), "edge": "head", "offset": 2})
    )
    resized = group_of(reformat(rig, [CLIPX], width=960, height=540))
    refused = rig.do("journal.revert", {"group": edit})
    assert refused["errorCode"] == "CONFLICT", refused
    assert newer_of(refused) == [resized]
    clip = [c for c in reel(rig).children if str(c.name.get_value()) == "CLIPX"][-1]
    assert clip_format(clip)["width"] == 960
    assert rig.do("journal.revert", {"group": resized})["ok"] is True
    assert rig.do("journal.revert", {"group": edit})["ok"] is True


def test_a_reformat_that_made_a_duplicate_is_not_a_newer_group_of_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_reformat_rig(monkeypatch, tmp_path)
    edit = group_of(
        rig.do("segment.trim", {"target": seg_sel(CLIPX, 0, index=0), "edge": "head", "offset": 2})
    )
    made = reformat(rig, [CLIPX], width=960, height=540, duplicate=True)
    assert made["ok"] is True, made
    assert rig.do("journal.revert", {"group": edit})["ok"] is True


def test_a_copy_of_the_edited_sequence_is_not_a_newer_group(trig: Rig) -> None:
    edit = group_of(trim(trig, a_seg(1), "head", 2))
    copied = trig.do("desktop.copy", {"sources": [S1], "destination": wt(REEL_WT)})
    assert copied["ok"] is True, copied
    assert trig.do("journal.revert", {"group": edit})["ok"] is True


def test_batch_a_later_rename_of_the_group_is_a_newer_group(brig: Rig) -> None:
    made = group_of(brig.do("node.create", {"target": BG1, "type": "Blur", "name": "b1"}))
    renamed = group_of(brig.do("desktop.rename", {"target": BG1, "name": "BG1x"}))
    assert newer_of(brig.do("journal.revert", {"group": made})) == [renamed]


def test_timeline_a_later_move_of_the_sequence_is_a_newer_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_p32b_rig(monkeypatch, tmp_path)
    edit = group_of(trim(rig, a_seg(1), "head", 2))
    moved = group_of(rig.do(MOVE, {"sources": [S1], "destination": reel2(rig)}))
    assert newer_of(rig.do("journal.revert", {"group": edit})) == [moved]


def test_timeline_a_later_delete_of_the_sequence_is_a_newer_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_p32b_rig(monkeypatch, tmp_path)
    edit = group_of(trim(rig, a_seg(1), "head", 2))
    gone = group_of(delete(rig, [S1]))
    assert newer_of(rig.do("journal.revert", {"group": edit})) == [gone]


def test_the_ops_say_that_restore_checkpoint_and_load_setup_do_not_compare(brig: Rig) -> None:
    for name in ("journal.restore_checkpoint", "batch.load_setup"):
        op = brig.registry.get(name)
        assert op is not None
        assert "without comparing it with the current state" in op.description
        assert "preCheckpoint" in op.description


BATCH_UID = {"kind": "batch", "uid": "U1"}
NODE_U1 = {"kind": "node", "batch": BATCH_UID, "name": "n"}


def journal_of_a_checkpointed_group() -> list[dict[str, Any]]:
    return [
        {
            "type": "intent",
            "id": "i1",
            "group": "g1",
            "ts": "t1",
            "op": "node.create",
            "target": BATCH_UID,
            "revertCheckpoint": "c1",
            "targetKind": "batch",
            "fingerprintBefore": "a",
        },
        {
            "type": "outcome",
            "id": "i1",
            "group": "g1",
            "ok": True,
            "fingerprintAfter": "b",
            "touched": True,
        },
        {"type": "checkpoint", "id": "c1", "group": "g1"},
    ]


def later_value_write(verdict: str | None, outcome: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [
        {
            "type": "intent",
            "id": "i2",
            "group": "g2",
            "ts": "t2",
            "op": "node.set_attributes",
            "target": NODE_U1,
            "before": {"pos_x": 1.0},
            "after": {"pos_x": 2.0},
        }
    ]
    if outcome:
        records.append(
            {
                "type": "outcome",
                "id": "i2",
                "group": "g2",
                "ok": True,
                "applied": ["pos_x"],
                "changedKeys": [],
                "readBack": {"pos_x": 2.0},
            }
        )
    if verdict is not None:
        records.append({"type": "check", "id": "i2", "group": "g2", "verdict": verdict})
    return records


def newer_after(extra: list[dict[str, Any]]) -> list[str]:
    records = journal_of_a_checkpointed_group() + extra
    return [row["group"] for row in newer_groups(records, Index(records), "g1")]


@pytest.mark.parametrize("verdict", ["diverged", "unknown"])
def test_a_later_write_whose_result_is_not_known_still_counts(verdict: str) -> None:
    assert newer_after(later_value_write(verdict, outcome=False)) == ["g2"]


def test_a_later_write_with_no_outcome_and_no_check_counts() -> None:
    assert newer_after(later_value_write(None, outcome=False)) == ["g2"]


def test_a_later_write_found_not_landed_does_not_count() -> None:
    assert newer_after(later_value_write("not_landed", outcome=False)) == []


def created_selector(rig: Rig, group: str) -> dict[str, Any]:
    outcome = next(
        r for r in rig.records() if r["type"] == "outcome" and r["group"] == group and r["ok"]
    )
    selector: dict[str, Any] = outcome["created"]["selector"]
    return selector


def test_a_match_of_a_segment_is_not_a_newer_group_of_the_sequence(trig: Rig) -> None:
    edit = group_of(trim(trig, a_seg(1), "head", 2))
    matched = trig.do("segment.match", {"target": a_seg(1), "destination": wt(REEL_WT)})
    assert matched["ok"] is True, matched
    assert trig.do("journal.revert", {"group": edit})["ok"] is True


def test_reformat_revert_stops_at_a_later_value_write_to_the_new_clip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_reformat_rig(monkeypatch, tmp_path)
    resized = group_of(reformat(rig, [CLIPX], width=960, height=540))
    clip = [c for c in reel(rig).children if str(c.name.get_value()) == "CLIPX"][-1]
    seg = clip.versions[0].tracks[0].segments[0]
    seg.comment = FakeAttr("old")
    seg.attributes.append("comment")
    parent = created_selector(rig, resized)
    value = group_of(
        rig.do(
            "segment.set_attributes",
            {"target": seg_sel(parent, 0, index=0), "values": {"comment": "mine"}},
        )
    )
    refused = rig.do("journal.revert", {"group": resized})
    assert refused["errorCode"] == "CONFLICT", refused
    assert newer_of(refused) == [value]
    assert seg.comment.get_value() == "mine"
    forced = rig.do("journal.revert", {"group": resized, "force": True})
    assert forced["ok"] is True and newer_of(forced) == [value], forced


def test_reformat_revert_goes_through_when_the_newer_write_was_reverted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_reformat_rig(monkeypatch, tmp_path)
    resized = group_of(reformat(rig, [CLIPX], width=960, height=540))
    clip = [c for c in reel(rig).children if str(c.name.get_value()) == "CLIPX"][-1]
    seg = clip.versions[0].tracks[0].segments[0]
    seg.comment = FakeAttr("old")
    seg.attributes.append("comment")
    value = group_of(
        rig.do(
            "segment.set_attributes",
            {
                "target": seg_sel(created_selector(rig, resized), 0, index=0),
                "values": {"comment": "mine"},
            },
        )
    )
    assert rig.do("journal.revert", {"group": value})["ok"] is True
    assert rig.do("journal.revert", {"group": resized})["ok"] is True


def test_create_sequence_revert_stops_at_a_later_rename_of_the_new_sequence(trig: Rig) -> None:
    made = trig.do(
        "timeline.create_sequence",
        {
            "parent": wt(REEL_WT),
            "name": "NEWSEQ",
            "frame_rate": "23.976 fps",
            "width": 1920,
            "height": 1080,
        },
    )
    group = group_of(made)
    renamed = group_of(
        trig.do("desktop.rename", {"target": created_selector(trig, group), "name": "NEWSEQ2"})
    )
    refused = trig.do("journal.revert", {"group": group})
    assert refused["errorCode"] == "CONFLICT" and newer_of(refused) == [renamed], refused
    assert trig.do("journal.revert", {"group": renamed})["ok"] is True
    assert trig.do("journal.revert", {"group": group})["ok"] is True


def test_match_revert_stops_at_a_later_rename_of_the_new_clip(trig: Rig) -> None:
    matched = group_of(trig.do("segment.match", {"target": a_seg(1), "destination": wt(REEL_WT)}))
    renamed = group_of(
        trig.do("desktop.rename", {"target": created_selector(trig, matched), "name": "renamed"})
    )
    refused = trig.do("journal.revert", {"group": matched})
    assert refused["errorCode"] == "CONFLICT" and newer_of(refused) == [renamed], refused


def test_move_revert_stops_at_a_later_rename_of_the_moved_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_p32b_rig(monkeypatch, tmp_path)
    moved = group_of(rig.do(MOVE, {"sources": [S1], "destination": reel2(rig)}))
    outcome = next(r for r in rig.records() if r["type"] == "outcome" and r["group"] == moved)
    new_place = outcome["created"]["moves"][0]["to"]["selector"]
    renamed = group_of(rig.do("desktop.rename", {"target": new_place, "name": "S1b"}))
    refused = rig.do("journal.revert", {"group": moved})
    assert refused["errorCode"] == "CONFLICT" and newer_of(refused) == [renamed], refused
    forced = rig.do("journal.revert", {"group": moved, "force": True})
    assert forced["ok"] is True, forced
