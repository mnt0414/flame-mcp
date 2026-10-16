"""Tests putting a sequence back.

`journal.revert` of a `segment.trim` group and `journal.restore_checkpoint` of a timeline
checkpoint: the CONFLICT rule and `force`, the new identity, the edited sequence kept as the
pre-restore checkpoint (so a restore can be undone), the Timeline pointed at the restored sequence
again, the rollback when the copy back fails, the uid chain, `journal.check` and
`journal.list_checkpoints`. Run through the real Runtime with a scripted `transport.exec` over the
fake Flame tree."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import timeline_restore
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from timeline_p32a_helpers import (
    S1,
    S1_WT,
    S3_WT,
    a_seg,
    checkpoints,
    fingerprint_of_target,
    make_timeline_rig,
    reel_names,
    revert,
    rows,
    stash_entries,
    stash_library,
    take_checkpoint,
    trim,
    uid,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def s1_in_reel(rig: Rig) -> list[Any]:
    return [c for c in rig.handles["reel"].children if str(c.name.get_value()) == "S1"]


def current_s1(rig: Rig) -> Any:
    [found] = s1_in_reel(rig)
    return found


def restore(rig: Rig, checkpoint_id: str) -> dict[str, Any]:
    return rig.do("journal.restore_checkpoint", {"checkpoint": checkpoint_id})


def edited(rig: Rig) -> dict[str, Any]:
    """trim A's head by 2, behind a checkpoint; the envelope."""
    env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is True, env
    return env


def move_count(rig: Rig) -> int:
    return len(rig.flame.media_panel.move_calls)


def copy_count(rig: Rig) -> int:
    return len(rig.flame.media_panel.copy_calls)


def fp_of(rig: Rig, obj: Any) -> str:
    return fingerprint_of_target(rig, wt(obj.get_wiretap_node_id()))


def test_revert_puts_the_sequence_back_as_it_was_with_a_new_identity(rig: Rig) -> None:
    before_fp = fingerprint_of_target(rig, S1)
    before_rows = rows(rig.handles["s1"])
    old_uid = uid(rig.handles["s1"])
    env = edited(rig)
    back = revert(rig, env)
    assert back["ok"] is True, back
    result = back["result"]
    restored = current_s1(rig)
    assert fp_of(rig, restored) == before_fp and rows(restored) == before_rows
    assert result["reverted"] == env["journal"]["group"] and result["matched"] is True
    assert result["fingerprintMatches"] is True and result["expectedFingerprint"] == before_fp
    assert result["restored"]["uid"] == uid(restored) != old_uid
    assert result["restored"]["selector"] == wt(restored.get_wiretap_node_id())
    assert result["restored"]["selector"] != S1
    assert result["replaced"]["uid"] == old_uid
    assert result["displayedBefore"] is False and result["reopened"] is None
    assert any("uid and the wiretap id" in line for line in result["notRestored"])
    assert back["journal"]["group"] == result["group"] != env["journal"]["group"]


def test_the_old_selector_finds_nothing_any_more(rig: Rig) -> None:
    env = edited(rig)
    revert(rig, env)
    gone = rig.do("timeline.fingerprint", {"target": S1})
    assert gone["ok"] is False and gone["errorCode"] == "SELECTOR_EMPTY"
    assert rig.do("segment.info", {"target": a_seg()})["errorCode"] == "SELECTOR_EMPTY"


def test_the_restored_sequence_is_at_the_end_of_its_reel(rig: Rig) -> None:
    env = edited(rig)
    back = revert(rig, env)
    assert reel_names(rig.handles["reel"]) == ["c1", "S2", "S3", "CLIPX", "S1"]
    assert back["result"]["position"] == {"index": 4, "of": 5}


def test_the_edited_sequence_is_kept_in_the_stash_as_the_pre_restore_checkpoint(rig: Rig) -> None:
    env = edited(rig)
    edited_rows = rows(rig.handles["s1"])
    edited_fp = fingerprint_of_target(rig, S1)
    back = revert(rig, env)
    pre_id = back["result"]["preCheckpoint"]
    assert isinstance(pre_id, str)
    record = next(c for c in checkpoints(rig) if c["id"] == pre_id)
    assert record["kind"] == "timeline" and record["fingerprint"] == edited_fp
    assert record["group"] == back["journal"]["group"]
    assert record["source"]["uid"] == back["result"]["restored"]["uid"]  # what it would replace
    stashed = next(e for e in stash_entries(rig) if uid(e) == record["stash"]["uid"])
    assert rows(stashed) == edited_rows and len(stash_entries(rig)) == 2
    assert back["result"]["preStash"]["uid"] == record["stash"]["uid"]
    assert back["result"]["undo"] == {
        "op": "journal.restore_checkpoint",
        "args": {"checkpoint": pre_id},
    }


def test_the_pre_restore_checkpoint_undoes_the_revert(rig: Rig) -> None:
    env = edited(rig)
    edited_fp = fingerprint_of_target(rig, S1)
    back = revert(rig, env)
    undone = restore(rig, back["result"]["preCheckpoint"])
    assert undone["ok"] is True, undone
    assert fp_of(rig, current_s1(rig)) == edited_fp
    assert undone["result"]["restored"]["uid"] == uid(current_s1(rig))
    assert len(s1_in_reel(rig)) == 1


def test_a_group_is_reverted_once(rig: Rig) -> None:
    env = edited(rig)
    revert(rig, env)
    again = revert(rig, env)
    assert again["ok"] is False and "already reverted" in again["error"]
    states = {i["group"]: i["state"] for i in rig.do("journal.list")["result"]["items"]}
    assert states[env["journal"]["group"]] == "reverted"


def test_the_revert_is_journaled_as_a_group_of_its_own(rig: Rig) -> None:
    env = edited(rig)
    back = revert(rig, env)
    index = Index(rig.records())
    [view] = index.groups[back["journal"]["group"]]
    intent = view.intent
    assert intent["op"] == "journal.restore_checkpoint" and intent["targetKind"] == "timeline"
    assert (
        intent["reverts"] == env["journal"]["group"]
        and intent["checkpoint"] == env["journal"]["checkpoint"]
    )
    assert intent["revertCheckpoint"] == back["result"]["preCheckpoint"]
    assert intent["fingerprintExpected"] == checkpoints(rig)[0]["fingerprint"]
    assert view.outcome is not None and view.outcome["ok"] is True
    assert view.outcome["replacedUid"] and view.outcome["targetUid"] == uid(current_s1(rig))
    assert view.outcome["fingerprintAfter"] == checkpoints(rig)[0]["fingerprint"]
    assert index.group_state(env["journal"]["group"]) == "reverted"
    shown = rig.do("journal.show", {"group": back["journal"]["group"]})["result"]["records"]
    assert [r["type"] for r in shown] == ["intent", "checkpoint", "outcome"]


def test_the_restore_intent_is_flushed_before_the_write_is_sent(rig: Rig) -> None:
    env = edited(rig)
    revert(rig, env)
    calls = [c for c in rig.exec.calls if c.label == "timeline.restore"]
    assert len(calls) == 2  # the read, then the write
    assert calls[1].journal_types[-1] == "intent"
    assert calls[0].journal_types[-1] != "intent"


def test_revert_of_a_revert_returns_to_the_edited_state(rig: Rig) -> None:
    env = edited(rig)
    edited_fp = fingerprint_of_target(rig, S1)
    back = revert(rig, env)
    again = revert(rig, back)
    assert again["ok"] is True, again
    assert fp_of(rig, current_s1(rig)) == edited_fp
    index = Index(rig.records())
    assert index.group_state(back["journal"]["group"]) == "reverted"


def test_two_edits_can_be_reverted_one_after_the_other_through_the_uid_chain(rig: Rig) -> None:
    original_fp = fingerprint_of_target(rig, S1)
    first = edited(rig)
    second = trim(rig, a_seg(), "tail", 3)
    assert second["ok"] is True
    assert revert(rig, second)["ok"] is True
    after_first = revert(rig, first)  # the sequence has another uid now: found through the chain
    assert after_first["ok"] is True, after_first
    assert fp_of(rig, current_s1(rig)) == original_fp and len(s1_in_reel(rig)) == 1


def test_a_changed_sequence_is_a_conflict_and_nothing_is_touched(rig: Rig) -> None:
    env = edited(rig)
    rig.handles["s1"].versions[0].tracks[0].segments[3].trim_tail(2)  # someone else, by transport
    moves, copies = move_count(rig), copy_count(rig)
    refused = revert(rig, env)
    assert refused["ok"] is False and refused["errorCode"] == "CONFLICT"
    assert refused["result"]["refused"] is True
    assert refused["result"]["expectedFingerprint"] != refused["result"]["currentFingerprint"]
    assert "force=true" in refused["error"]
    assert (move_count(rig), copy_count(rig)) == (moves, copies)
    assert uid(current_s1(rig)) == uid(rig.handles["s1"])


def test_force_restores_anyway_and_the_intent_says_so(rig: Rig) -> None:
    env = edited(rig)
    rig.handles["s1"].versions[0].tracks[0].segments[3].trim_tail(2)
    forced = revert(rig, env, force=True)
    assert forced["ok"] is True, forced
    assert forced["result"]["forced"]["used"] is True
    assert fp_of(rig, current_s1(rig)) == checkpoints(rig)[0]["fingerprint"]
    [view] = Index(rig.records()).groups[forced["journal"]["group"]]
    assert view.intent["forced"]["used"] is True and "changed after the run" in str(
        view.intent["forced"]["note"]
    )


def test_a_change_between_the_read_and_the_write_is_caught_inside_flame(rig: Rig) -> None:
    env = edited(rig)
    seen = {"reads": 0}

    def sneak(label: str | None) -> None:
        if label == "timeline.restore":
            seen["reads"] += 1
            if seen["reads"] == 2:  # between the read and the write
                rig.handles["s1"].versions[0].tracks[0].segments[3].trim_tail(2)

    rig.exec.before_run = sneak
    moves = move_count(rig)
    refused = revert(rig, env, force=True)
    assert refused["ok"] is False and refused["errorCode"] == "CONFLICT"
    assert "changed between the read and the write" in refused["error"]
    assert move_count(rig) == moves
    [view] = Index(rig.records()).groups[refused["journal"]["group"]]
    assert view.outcome is not None and view.outcome["ok"] is False
    assert view.outcome["changed"] is False and view.outcome["preCheckpoint"] is None
    again = rig.do("journal.revert", {"group": refused["journal"]["group"]})
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"
    assert "changed nothing" in again["error"]


def test_the_sequence_that_was_shown_is_opened_again_after_the_restore(rig: Rig) -> None:
    rig.flame.timeline.clip = rig.handles["s1"]
    env = edited(rig)
    assert rig.flame.timeline.clip is rig.handles["s1"]
    back = revert(rig, env)
    assert back["result"]["displayedBefore"] is True and back["result"]["reopened"] is True
    assert rig.flame.timeline.clip is current_s1(rig)
    assert uid(rig.flame.timeline.clip) == back["result"]["restored"]["uid"]


def test_the_timeline_moves_to_another_sequence_when_the_shown_one_is_moved_away(rig: Rig) -> None:
    rig.flame.timeline.clip = rig.handles["s1"]
    env = edited(rig)
    revert(rig, env)
    # the move showed another sequence of the reel, open() pointed it back at the restored one
    assert rig.flame.timeline.clip is current_s1(rig)


def shown_name(rig: Rig) -> str | None:
    shown = rig.flame.timeline.clip
    return None if shown is None else str(shown.name.get_value())


def test_the_fake_switches_the_display_only_when_a_sequence_in_front_of_it_is_moved_out(
    rig: Rig,
) -> None:
    """Observed once on a real Flame: reel A B C D, B shown, move(A out) -> C shown;
    copy, trim and the move of a sequence BEHIND the shown one changed nothing."""
    panel, libreel = rig.flame.media_panel, rig.handles["libreel"]
    rig.flame.timeline.clip = rig.handles["s2"]  # reel: c1 S1 S2 S3 CLIPX
    panel.copy(rig.handles["s3"], libreel)
    rig.handles["s3"].versions[0].tracks[0].segments[0].trim_tail(2)
    panel.move(rig.handles["clipx"], libreel)
    assert shown_name(rig) == "S2"
    panel.move(rig.handles["s1"], libreel)
    assert shown_name(rig) == "S3"


def test_a_sequence_in_front_of_the_shown_one_is_restored_and_the_shown_one_is_shown_again(
    rig: Rig,
) -> None:
    rig.flame.timeline.clip = rig.handles["s2"]
    env = edited(rig)  # S1 sits in front of S2
    back = revert(rig, env)
    result = back["result"]
    assert back["ok"] is True, back
    assert result["displayedBefore"] is False and result["reopened"] is None
    assert result["shownBefore"]["name"] == "S2" and result["shownBefore"]["uid"] == uid(
        rig.handles["s2"]
    )
    assert result["shownAfterRestore"]["name"] == "S3"  # the moves switched it
    assert result["shownAfter"]["name"] == "S2" and result["reshown"] is True
    assert rig.flame.timeline.clip is rig.handles["s2"]


def test_a_sequence_behind_the_shown_one_leaves_the_display_alone_and_open_is_not_called(
    rig: Rig,
) -> None:
    rig.flame.timeline.clip = rig.handles["s2"]
    env = trim(rig, a_seg(0, parent=wt(S3_WT)), "tail", 3)
    assert env["ok"] is True, env
    back = revert(rig, env)
    result = back["result"]
    assert back["ok"] is True, back
    assert result["shownAfterRestore"]["name"] == "S2" and result["reshown"] is None
    assert result["shownAfter"]["name"] == "S2" and result["reopened"] is None
    assert rig.flame.timeline.clip is rig.handles["s2"]


def test_a_shown_one_that_was_switched_to_nothing_is_shown_again(rig: Rig) -> None:
    rig.flame.timeline.clip = rig.handles["s2"]
    env = edited(rig)
    original = type(rig.flame.media_panel).move

    def lose_display(self: Any, source: Any, destination: Any) -> Any:
        moved = original(self, source, destination)
        rig.flame.timeline.clip = None
        return moved

    type(rig.flame.media_panel).move = lose_display
    try:
        back = revert(rig, env)
    finally:
        type(rig.flame.media_panel).move = original
    assert back["ok"] is True and back["result"]["shownAfterRestore"] is None
    assert back["result"]["reshown"] is True and rig.flame.timeline.clip is rig.handles["s2"]


def test_a_shown_one_that_is_gone_is_not_chased(rig: Rig) -> None:
    rig.flame.timeline.clip = rig.handles["s2"]
    env = edited(rig)
    rig.flame.delete(rig.handles["s2"])
    rig.flame.timeline.clip = rig.handles["s3"]
    back = revert(rig, env)
    # what was shown (S2) is gone and what is shown now is another sequence: nothing to open
    assert back["ok"] is True and back["result"]["reshown"] is None


def test_the_display_is_put_back_after_a_rollback_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.flame.timeline.clip = rig.handles["s2"]
    env = edited(rig)
    panel = rig.flame.media_panel
    original = type(panel).copy
    reel = rig.handles["reel"]

    def refusing(self: Any, source: Any, destination: Any, duplicate_action: str = "add") -> Any:
        if destination is reel:
            raise RuntimeError("copy refused")
        return original(self, source, destination, duplicate_action)

    monkeypatch.setattr(type(panel), "copy", refusing)
    failed = revert(rig, env)
    assert failed["ok"] is False and failed["result"]["rolledBack"]["done"] is True
    assert failed["result"]["shownAfter"]["name"] == "S2" and failed["result"]["reshown"] is True
    assert rig.flame.timeline.clip is rig.handles["s2"]


def test_a_restore_when_the_timeline_shows_nothing_does_not_open_anything(rig: Rig) -> None:
    assert rig.flame.timeline.clip is None
    env = edited(rig)
    back = revert(rig, env)
    assert back["ok"] is True and rig.flame.timeline.clip is None


def test_an_unreadable_timeline_counts_as_not_shown(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        @property
        def clip(self) -> Any:
            raise RuntimeError("no timeline")

    rig.flame.timeline = Broken()
    env = edited(rig)
    back = revert(rig, env)
    assert back["ok"] is True and back["result"]["displayedBefore"] is False


def test_a_failed_copy_back_moves_the_edited_sequence_back_and_says_so(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = edited(rig)
    edited_fp = fingerprint_of_target(rig, S1)
    old_uid = uid(rig.handles["s1"])
    panel = rig.flame.media_panel
    original = type(panel).copy
    reel = rig.handles["reel"]

    def refusing(self: Any, source: Any, destination: Any, duplicate_action: str = "add") -> Any:
        if destination is reel:
            raise RuntimeError("copy refused")
        return original(self, source, destination, duplicate_action)

    monkeypatch.setattr(type(panel), "copy", refusing)
    failed = revert(rig, env)
    assert failed["ok"] is False and failed["errorCode"] == "FLAME_API_ERROR"
    assert "copy refused" in failed["error"] and "moved back into its reel" in failed["error"]
    rolled = failed["result"]["rolledBack"]
    assert rolled["done"] is True and rolled["object"]["uid"] != old_uid
    assert fp_of(rig, current_s1(rig)) == edited_fp  # the edited sequence is where it was
    assert uid(current_s1(rig)) == rolled["object"]["uid"]
    assert failed["result"]["preCheckpoint"] is None
    [view] = Index(rig.records()).groups[failed["journal"]["group"]]
    assert view.outcome is not None and view.outcome["ok"] is False
    assert view.outcome["replacedUid"] == old_uid and view.outcome["targetUid"] == uid(
        current_s1(rig)
    )
    monkeypatch.setattr(type(panel), "copy", original)
    # the group can still be reverted: the sequence has another uid, found through the chain
    retry = revert(rig, env)
    assert retry["ok"] is True, retry
    assert fp_of(rig, current_s1(rig)) == checkpoints(rig)[0]["fingerprint"]


def test_when_the_rollback_fails_too_the_error_names_where_the_edited_sequence_is(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = edited(rig)
    panel = rig.flame.media_panel
    original_copy, original_move = type(panel).copy, type(panel).move
    reel = rig.handles["reel"]

    def refusing_copy(
        self: Any, source: Any, destination: Any, duplicate_action: str = "add"
    ) -> Any:
        if destination is reel:
            raise RuntimeError("copy refused")
        return original_copy(self, source, destination, duplicate_action)

    def refusing_move(self: Any, source: Any, destination: Any) -> Any:
        if destination is reel:
            raise RuntimeError("move refused")
        return original_move(self, source, destination)

    monkeypatch.setattr(type(panel), "copy", refusing_copy)
    monkeypatch.setattr(type(panel), "move", refusing_move)
    failed = revert(rig, env)
    assert failed["ok"] is False and failed["errorCode"] == "FLAME_API_ERROR"
    assert failed["result"]["rolledBack"]["done"] is False
    stash_uid = failed["result"]["preStash"]["uid"]
    assert stash_uid in failed["error"] and "NOT moved back" in failed["error"]
    assert s1_in_reel(rig) == []  # the sequence is only in the stash reel now
    assert any(uid(e) == stash_uid for e in stash_entries(rig))
    orphan = next(c for c in checkpoints(rig) if c["group"] == failed["journal"]["group"])
    assert orphan["orphaned"] is True and orphan["source"] is None
    assert orphan["stash"]["uid"] == stash_uid


def test_a_restored_copy_with_another_fingerprint_is_a_failure_that_names_the_pre_checkpoint(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = edited(rig)
    edited_fp = fingerprint_of_target(rig, S1)
    reel = rig.handles["reel"]
    original = type(reel).receive

    def lossy(self: Any, sources: list[Any]) -> list[Any]:
        copies: list[Any] = original(self, sources)
        if self is reel:
            copies[0]._versions[0]._tracks[0]._segments[1].source_name = "somewhere else"
        return copies

    monkeypatch.setattr(type(reel), "receive", lossy)
    failed = revert(rig, env)
    assert failed["ok"] is False and failed["errorCode"] == "FLAME_API_ERROR"
    assert "ran but the restored thing's fingerprint" in failed["error"]
    result = failed["result"]
    assert result["fingerprintMatches"] is False and result["matched"] is False
    assert (
        isinstance(result["preCheckpoint"], str) and "journal.restore_checkpoint" in failed["hint"]
    )
    [view] = Index(rig.records()).groups[failed["journal"]["group"]]
    assert view.state() == "failed" and view.outcome is not None
    assert view.outcome["ok"] is False and view.outcome["matched"] is False
    monkeypatch.setattr(type(reel), "receive", original)
    undone = restore(rig, result["preCheckpoint"])  # the edited sequence is in the stash
    assert undone["ok"] is True and fp_of(rig, current_s1(rig)) == edited_fp


def test_restore_checkpoint_puts_a_manually_checkpointed_sequence_back(rig: Rig) -> None:
    saved = take_checkpoint(rig, S1)
    rig.handles["s1"].versions[0].tracks[0].segments[1].trim_tail(3)  # a transport edit
    env = restore(rig, saved["checkpoint"])
    assert env["ok"] is True, env
    assert fp_of(rig, current_s1(rig)) == saved["fingerprint"]
    assert env["result"]["checkpoint"] == saved["checkpoint"] and env["result"]["matched"] is True
    assert env["result"]["preCheckpoint"] != saved["checkpoint"]
    assert env["execution"]["effect"] == "destructive"


def test_the_same_checkpoint_can_be_restored_again_after_another_edit(rig: Rig) -> None:
    saved = take_checkpoint(rig, S1)
    rig.handles["s1"].versions[0].tracks[0].segments[1].trim_tail(3)
    assert restore(rig, saved["checkpoint"])["ok"] is True
    current_s1(rig).versions[0].tracks[0].segments[3].trim_tail(2)  # edit the restored one
    again = restore(rig, saved["checkpoint"])  # its uid is not the checkpoint's source any more
    assert again["ok"] is True, again
    assert fp_of(rig, current_s1(rig)) == saved["fingerprint"] and len(s1_in_reel(rig)) == 1


def test_a_restore_of_an_unknown_checkpoint_is_invalid_args(rig: Rig) -> None:
    env = restore(rig, "c_nope")
    assert env["errorCode"] == "INVALID_ARGS" and move_count(rig) == 0


def test_a_missing_stash_is_refused_before_anything_is_moved(rig: Rig) -> None:
    env = edited(rig)
    [entry] = stash_entries(rig)
    rig.flame.delete(entry)
    moves = move_count(rig)
    refused = revert(rig, env)
    assert refused["ok"] is False and refused["errorCode"] == "PRECONDITION_UNMET"
    assert "stashed copy" in refused["error"] and move_count(rig) == moves
    assert uid(current_s1(rig)) == uid(rig.handles["s1"])


def test_a_stash_that_changed_is_refused_before_anything_is_moved(rig: Rig) -> None:
    env = edited(rig)
    [entry] = stash_entries(rig)
    entry._versions[0]._tracks[0]._segments[1].source_name = "tampered"
    moves = move_count(rig)
    refused = revert(rig, env)
    assert refused["errorCode"] == "PRECONDITION_UNMET" and "has changed since" in refused["error"]
    assert move_count(rig) == moves


def test_a_sequence_that_is_gone_is_refused(rig: Rig) -> None:
    env = edited(rig)
    rig.flame.delete(rig.handles["s1"])
    moves = move_count(rig)
    refused = revert(rig, env)
    assert refused["errorCode"] == "SELECTOR_EMPTY" and "not found by its uid" in refused["error"]
    assert move_count(rig) == moves and stash_library(rig) is not None


def test_a_sequence_that_has_been_moved_into_a_library_is_refused(rig: Rig) -> None:
    env = edited(rig)
    rig.flame.media_panel.move(rig.handles["s1"], rig.handles["libreel"])
    refused = revert(rig, env)
    assert refused["ok"] is False
    # its uid changed with the move: it cannot be found, so nothing is replaced
    assert refused["errorCode"] in ("SELECTOR_EMPTY", "PRECONDITION_UNMET")


def test_the_restore_needs_a_journal_that_can_be_written(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = edited(rig)

    def refuse(record: dict[str, Any]) -> None:
        raise OSError("read-only volume")

    monkeypatch.setattr(rig.journal, "append", refuse)
    moves = move_count(rig)
    refused = revert(rig, env)
    assert refused["ok"] is False and refused["errorCode"] == "POLICY_DENIED"
    assert "without an intent on record" in refused["error"] and move_count(rig) == moves


def test_a_restore_is_refused_up_front_when_a_needed_op_is_denied(rig: Rig) -> None:
    env = edited(rig)
    rig.runtime.policy = Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": "journal"})
    moves = move_count(rig)
    refused = revert(rig, env)
    assert refused["errorCode"] == "POLICY_DENIED" and "timeline.checkpoint" in refused["error"]
    assert move_count(rig) == moves


def open_copy(rig: Rig, env: dict[str, Any], **override: Any) -> str:
    """A copy of a real group's intent with no outcome (what a call whose answer never came
    leaves): journal.check then reads the real Flame."""
    from flame_mcp.journal import new_id, now_iso

    intent = copy.deepcopy(next(r for r in rig.records() if r["type"] == "intent"))
    group = new_id("g")
    intent.update(id=new_id("i"), group=group, ts=now_iso(), **override)
    rig.journal.append(intent)
    return group


def check(rig: Rig, group: str) -> dict[str, Any]:
    entry: dict[str, Any] = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    return entry


def test_check_decides_an_open_edit_by_the_fingerprint(rig: Rig) -> None:
    env = edited(rig)
    before = checkpoints(rig)[0]["fingerprint"]
    now = fingerprint_of_target(rig, S1)
    unchanged = open_copy(rig, env, fingerprintBefore=now, fingerprintExpected=None)
    assert check(rig, unchanged)["verdict"] == "not_landed"
    landed = open_copy(rig, env, fingerprintBefore=before, fingerprintExpected=now)
    assert check(rig, landed)["verdict"] == "landed"
    elsewhere = open_copy(rig, env, fingerprintBefore=before, fingerprintExpected="f" * 64)
    assert check(rig, elsewhere)["verdict"] == "diverged"


def test_check_of_an_intent_whose_sequence_is_gone_is_diverged(rig: Rig) -> None:
    env = edited(rig)
    group = open_copy(rig, env, target=wt("/wt/RG/Reel 1/nothing"))
    entry = check(rig, group)
    assert entry["verdict"] == "diverged" and "not found" in entry["detail"]


def test_a_landed_check_makes_the_group_revertible_and_force_is_needed(rig: Rig) -> None:
    env = edited(rig)
    now = fingerprint_of_target(rig, S1)
    group = open_copy(rig, env, fingerprintExpected=now)
    assert check(rig, group)["verdict"] == "landed"
    refused = rig.do("journal.revert", {"group": group})
    assert refused["errorCode"] == "CONFLICT" and "recorded no fingerprint" in refused["error"]
    forced = rig.do("journal.revert", {"group": group, "force": True})
    assert forced["ok"] is True, forced


def test_an_unknown_end_of_a_restore_blocks_writes_and_check_decides(rig: Rig) -> None:
    env = edited(rig)
    rig.exec.fault(
        "timeline.restore", "TIMEOUT", when="after", skip=1
    )  # the write's answer is lost
    lost = revert(rig, env)
    assert lost["ok"] is False and lost["errorCode"] == "TIMEOUT"
    assert "journal.check" in lost["error"] and rig.runtime.guard.denial() is not None
    entry = rig.do("journal.check")["result"]["checked"][0]
    assert entry["verdict"] == "diverged" and "not found at its recorded address" in entry["detail"]
    assert rig.runtime.guard.denial() is None


def test_check_of_a_restore_that_never_ran_is_not_landed(rig: Rig) -> None:
    env = edited(rig)
    rig.exec.fault("timeline.restore", "TIMEOUT", when="before", skip=1)
    revert(rig, env)
    entry = rig.do("journal.check")["result"]["checked"][0]
    assert entry["verdict"] == "not_landed"


def test_list_checkpoints_shows_the_kind_and_the_timeline_fields(rig: Rig) -> None:
    env = edited(rig)
    back = revert(rig, env)
    listed = rig.do("journal.list_checkpoints")["result"]
    assert listed["total"] == 2
    newest, oldest = listed["items"]
    assert newest["id"] == back["result"]["preCheckpoint"] and newest["kind"] == "timeline"
    assert oldest["id"] == env["journal"]["checkpoint"] and oldest["kind"] == "timeline"
    assert oldest["source"]["name"] == "S1" and oldest["stash"]["library"]
    assert oldest["segmentCount"] == 5 and oldest["fingerprint"] and oldest["displayed"] is False
    assert "path" not in oldest and "batch" not in oldest


def test_list_checkpoints_keeps_a_batch_checkpoint_as_it_was_and_marks_its_kind(rig: Rig) -> None:
    batch = rig.do("batch.checkpoint", {"target": {"kind": "batch", "name": "BG1"}})
    assert batch["ok"] is True
    take_checkpoint(rig, S1)
    items = rig.do("journal.list_checkpoints")["result"]["items"]
    assert [i["kind"] for i in items] == ["timeline", "batch"]
    assert items[1]["path"] and items[1]["fileExists"] is True and items[1]["nodeCount"] == 4


def test_uid_chain_follows_the_replacements_newest_first_and_ends_on_a_cycle() -> None:
    records = [
        {"type": "outcome", "replacedUid": "a", "targetUid": "b"},
        {"type": "outcome", "replacedUid": "b", "targetUid": "c"},
        {"type": "outcome", "replacedUid": "x", "targetUid": "y"},
        {"type": "outcome", "replacedUid": "c", "targetUid": "a"},
        {"type": "intent", "replacedUid": "q", "targetUid": "r"},
    ]
    assert timeline_restore.uid_chain(records, "a") == ["c", "b", "a"]
    assert timeline_restore.uid_chain(records, "z") == ["z"]
    assert timeline_restore.uid_chain(records, "y") == ["y"]


def test_the_restore_op_is_not_in_the_public_catalog(rig: Rig) -> None:
    assert rig.registry.get("timeline.restore") is None
    assert timeline_restore.restore_op().name == "timeline.restore"
    assert timeline_restore.restore_op() is timeline_restore.restore_op()


def test_s1_wiretap_constant_is_what_the_helpers_use() -> None:
    assert wt(S1_WT) == S1
