"""Tests for `desktop.duplicate` and its journal records.

Also `journal.revert` and `journal.check`. See media_p31b_helpers.py for the rig and the fake."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import (
    PyTime,
)
from journal_test_helpers import Rig
from media_p31b_helpers import (
    C1,
    DUP,
    LIB_WT,
    REEL1,
    add_clip,
    add_seq,
    batch_reel,
    drop_recorded_fingerprints,
    dup,
    make_media_rig,
    names,
    names_to_uid,
    revert,
    states,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_media_rig(monkeypatch, tmp_path)


def test_duplicate_a_clip_puts_a_same_named_clip_beside_it_and_keeps_the_source(rig: Rig) -> None:
    clip = rig.handles["reel"].children[0]
    env = dup(rig, C1)
    assert env["ok"] is True, env
    result = env["result"]
    made = result["duplicate"]
    assert made["type"] == "PyClip" and made["name"] == "c1" and made["uid"]
    assert made["uid"] != clip.uid.get_value()
    assert made["selector"] == {"kind": "wiretap", "id": made["wiretapId"]}
    assert result["source"]["uid"] == clip.uid.get_value()
    assert result["source"]["parent"]["name"] == "Reel 1"
    assert names(rig.handles["reel_group"].reels[0]) == ["c1", "c1"]
    info = rig.do("desktop.info", {"target": made["selector"]})
    assert info["ok"] is True and info["result"]["uid"] == made["uid"]
    assert rig.flame.find_by_uid(clip.uid.get_value()) is clip
    assert env["affected"] == 1 and env["undoSteps"] == 0
    assert result["state"]["name"] == "c1" and "duration" in result["state"]


def test_duplicate_a_sequence_finds_it_by_the_list_even_though_the_answer_has_no_name(
    rig: Rig,
) -> None:
    seq = add_seq(rig.handles["reel2"])
    env = dup(rig, wt(seq))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["returned"] == {"type": "PyClip", "name": None, "uid": None}
    made = result["duplicate"]
    assert made["type"] == "PySequence" and made["name"] == "S1" and made["uid"]
    assert made["uid"] != seq.uid.get_value()
    assert names(rig.handles["reel2"]) == ["S1", "S1"]
    assert rig.do("desktop.info", {"target": made["selector"]})["ok"] is True


def test_duplicate_a_reel_makes_a_copy_with_its_contents(rig: Rig) -> None:
    reel = rig.handles["reel2"]
    add_clip(reel, "k")
    add_seq(reel, "S9")
    env = dup(rig, wt(reel))
    assert env["ok"] is True, env
    made = env["result"]["duplicate"]
    assert made["type"] == "PyReel" and made["name"] == "Reel 2 copy"
    assert env["result"]["state"] == {"name": "Reel 2 copy", "entries": 2}
    copy = rig.flame.find_by_wiretap_node_id(made["wiretapId"])
    assert names(copy) == ["k", "S9"]
    assert [r.name.get_value() for r in rig.handles["reel_group"].reels][-1] == "Reel 2 copy"
    assert env["result"]["parent"]["name"] == "RG"


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "wiretap", "id": "/wt/RG"},
        {"kind": "wiretap", "id": LIB_WT},
    ],
)
def test_duplicate_refuses_a_type_that_was_not_measured(rig: Rig, target: dict[str, Any]) -> None:
    env = dup(rig, target)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "only a clip, a sequence and a reel" in env["error"]
    assert rig.records() == [] and rig.flame.duplicate_calls == []


def test_duplicate_refuses_a_reel_of_a_batch_group_and_a_clip_inside_one(rig: Rig) -> None:
    reel = batch_reel(rig)
    clip = add_clip(reel, "bc")
    for target in (wt(reel), wt(clip)):
        env = dup(rig, target)
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
        assert "Batch group" in env["error"]
    assert rig.records() == [] and rig.flame.duplicate_calls == []


def test_a_duplicate_that_adds_nothing_is_a_silent_noop(rig: Rig) -> None:
    rig.flame.duplicate = lambda obj: obj
    env = dup(rig, C1)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert env["journal"]["revert"] is None and rig.records()[-1]["created"] is None
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]


def test_two_new_entries_that_cannot_be_told_apart_are_an_error_that_records_nothing(
    rig: Rig,
) -> None:
    real = rig.flame.duplicate

    def noisy(obj: Any) -> Any:
        add_clip(obj.parent, "other-1")
        add_clip(obj.parent, "other-2")
        return real(obj)

    rig.flame.duplicate = noisy
    env = dup(rig, C1)
    assert env["ok"] is True  # exactly one new entry carries the source's name
    assert env["result"]["duplicate"]["name"] == "c1"
    assert env["result"]["duplicate"]["uid"] == names_to_uid(rig, "c1")[-1]
    rig.flame.duplicate = lambda obj: (add_clip(obj.parent, "x"), add_clip(obj.parent, "y"))[0]
    again = dup(rig, C1)
    assert again["ok"] is False and again["errorCode"] == "FLAME_API_ERROR"
    assert "2 new entries" in again["error"] and again["journal"]["revert"] is None


def test_duplicate_journals_the_source_and_its_parent(rig: Rig) -> None:
    env = dup(rig, C1)
    intent, outcome = rig.records()
    assert intent["kind"] == "desktop_duplicate" and intent["op"] == DUP
    assert intent["target"] == REEL1
    assert intent["source"]["name"] == "c1" and intent["source"]["parent"]["name"] == "Reel 1"
    assert outcome["ok"] is True and outcome["created"]["uid"] == env["result"]["duplicate"]["uid"]
    assert outcome["created"]["state"]["name"] == "c1"
    assert outcome["created"]["parent"]["selector"] == REEL1


def test_duplicate_conflicts_when_the_siblings_changed_between_the_read_and_the_write(
    rig: Rig,
) -> None:
    def sneak(label: str | None) -> None:
        if label == DUP and len(rig.exec.calls) == 2:
            add_clip(rig.handles["reel_group"].reels[0], "sneaked")

    rig.exec.before_run = sneak
    env = dup(rig, C1)
    assert env["errorCode"] == "CONFLICT" and rig.flame.duplicate_calls == []


def test_revert_of_a_duplicate_deletes_it_while_it_is_as_it_was_made(rig: Rig) -> None:
    env = dup(rig, C1)
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert out["result"]["deleted"] is True and out["result"]["conflicts"] == []
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]
    assert rig.flame.find_by_uid(rig.handles["reel"].children[0].uid.get_value()) is not None
    assert states(rig)[env["journal"]["group"]] == "reverted"
    assert revert(rig, env)["errorCode"] == "PRECONDITION_UNMET"


def test_revert_of_a_duplicated_sequence_and_reel(rig: Rig) -> None:
    reel = rig.handles["reel2"]
    seq = add_seq(reel)
    made_seq = dup(rig, wt(seq))
    assert revert(rig, made_seq)["ok"] is True and names(reel) == ["S1"]
    made_reel = dup(rig, wt(reel))
    assert revert(rig, made_reel)["ok"] is True
    assert [r.name.get_value() for r in rig.handles["reel_group"].reels] == ["Reel 1", "Reel 2"]


def test_revert_of_a_duplicated_clip_conflicts_when_it_was_changed_and_deletes_nothing(
    rig: Rig,
) -> None:
    env = dup(rig, C1)
    made = rig.flame.find_by_uid(env["result"]["duplicate"]["uid"])
    original = env["result"]["state"]["duration"]
    made.duration = PyTime(original + 77)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert "duration" in " ".join(out["result"]["conflicts"])
    assert names(rig.handles["reel_group"].reels[0]) == ["c1", "c1"]
    assert states(rig)[env["journal"]["group"]] == "landed"  # not reverted: it can be retried
    made.duration = PyTime(original)
    made.name.set_value("renamed")
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and "name" in " ".join(out["result"]["conflicts"])
    made.name.set_value("c1")
    assert revert(rig, env)["ok"] is True


def test_revert_of_a_duplicated_sequence_conflicts_when_its_content_was_edited(rig: Rig) -> None:
    reel = rig.handles["reel2"]
    env = dup(rig, wt(add_seq(reel)))
    made = rig.flame.find_by_uid(env["result"]["duplicate"]["uid"])
    segment = made.versions[0].tracks[0]._segments[0]
    segment._set_record(0, segment.record_out.frame - 5)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "fingerprint" in " ".join(out["result"]["conflicts"])
    assert names(reel) == ["S1", "S1"]


def test_revert_of_a_duplicate_recorded_without_a_fingerprint_is_a_conflict(rig: Rig) -> None:
    env = dup(rig, C1)
    drop_recorded_fingerprints(rig)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "no fingerprint" in " ".join(out["result"]["conflicts"])
    assert names(rig.handles["reel_group"].reels[0]) == ["c1", "c1"]


def test_a_duplicated_reel_needs_no_fingerprint_to_be_reverted(rig: Rig) -> None:
    reel = rig.handles["reel2"]
    env = dup(rig, wt(reel))
    drop_recorded_fingerprints(rig)
    assert revert(rig, env)["ok"] is True


def test_revert_of_a_duplicated_reel_conflicts_when_its_contents_changed(rig: Rig) -> None:
    reel = rig.handles["reel2"]
    add_clip(reel, "k")
    env = dup(rig, wt(reel))
    copy = rig.flame.find_by_wiretap_node_id(env["result"]["duplicate"]["wiretapId"])
    add_clip(copy, "extra")
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and "entries" in " ".join(out["result"]["conflicts"])
    assert len(rig.handles["reel_group"].reels) == 3
    copy._children.pop()
    assert revert(rig, env)["ok"] is True and len(rig.handles["reel_group"].reels) == 2


def test_revert_of_a_duplicate_that_is_gone_already_is_ok_and_says_so(rig: Rig) -> None:
    env = dup(rig, C1)
    made = rig.flame.find_by_uid(env["result"]["duplicate"]["uid"])
    rig.flame.delete(made, confirm=False)
    out = revert(rig, env)
    assert out["ok"] is True and out["result"]["alreadyGone"] is True
    assert out["result"]["deleted"] is False


def test_a_delete_that_does_nothing_leaves_the_duplicate_revertible(rig: Rig) -> None:
    env = dup(rig, C1)
    real = rig.flame.delete
    rig.flame.delete = lambda obj, confirm=True: True
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "SILENT_NOOP"
    assert "still there" in out["error"]
    rig.flame.delete = real
    assert revert(rig, env)["ok"] is True


def test_a_lost_answer_of_a_duplicate_is_decided_by_check_and_revert_then_deletes_it(
    rig: Rig,
) -> None:
    rig.exec.fault(DUP, "TIMEOUT", when="after", skip=1)
    env = dup(rig, C1)
    assert env["errorCode"] == "TIMEOUT"
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "landed" and row["created"]["name"] == "c1"
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]


def test_check_of_a_duplicate_that_never_ran_is_not_landed_and_two_new_are_diverged(
    rig: Rig,
) -> None:
    rig.exec.fault(DUP, "BRIDGE_LOST", when="before", skip=1)
    env = dup(rig, C1)
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "not_landed"
    rig.exec.fault(DUP, "TIMEOUT", when="after", skip=1)
    second = dup(rig, C1)
    add_clip(rig.handles["reel_group"].reels[0], "someone")
    checked = rig.do("journal.check", {"group": second["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "diverged"


def test_a_lost_answer_of_the_revert_of_a_duplicate_is_decided_by_check(rig: Rig) -> None:
    env = dup(rig, C1)
    rig.exec.fault(DUP, "BRIDGE_LOST", when="after")
    out = revert(rig, env)
    assert out["errorCode"] == "BRIDGE_LOST"
    checked = rig.do("journal.check", {"group": out["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "landed"
    assert states(rig)[env["journal"]["group"]] == "reverted"
