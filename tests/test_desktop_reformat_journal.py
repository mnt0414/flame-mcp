"""Tests for the journal side of `desktop.reformat`: journal.revert, journal.check, a call whose
answer was lost, and plan.run.

An in-place reformat is put back by restoring the stashed copy of the clip (a new uid, the end of
its reel); a reformat of a duplicate is put back by deleting the duplicate. Run through the real
Runtime with a scripted `transport.exec` over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp import transport
from journal_test_helpers import Rig, bridge_result
from reformat_helpers import (
    CLIPX,
    OP,
    add_media_clip,
    clip_format,
    first_record,
    make_reformat_rig,
    reel,
    reel_names,
    reformat,
    revert,
    stash_entries,
    uid,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_reformat_rig(monkeypatch, tmp_path)


def sel(rig: Rig, name: str) -> dict[str, Any]:
    return wt(rig.handles[name].get_wiretap_node_id())


def edit_segments(clip: Any) -> None:
    clip.versions[0].tracks[0].segments[0].head = 9


def state_of(rig: Rig, env: dict[str, Any]) -> str:
    items = rig.do("journal.list", {})["result"]["items"]
    return str({i["group"]: i["state"] for i in items}[env["journal"]["group"]])


def test_revert_restores_the_stashed_clip_with_its_old_format_and_a_new_uid(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    before = clip_format(clip)
    old_uid = uid(clip)
    env = reformat(rig, bit_depth=16, scan_mode="F1", start_frame=1001)
    assert clip_format(clip) != before
    out = revert(rig, env)
    assert out["ok"] is True, out
    back = reel(rig).children[-1]
    assert str(back.name.get_value()) == "CLIPX" and uid(back) != old_uid
    assert clip_format(back) == before
    row = out["result"]["restored"][0]
    assert row["to"]["uid"] == uid(back) and row["to"]["selector"] == wt(back.get_wiretap_node_id())
    assert row["format"]["matches"] is True and row["format"]["expected"]["width"] == 1920
    assert out["result"]["reverted"] == env["journal"]["group"]
    assert any("NEW uid" in n for n in out["result"]["notes"])
    assert out["affected"] == 1 and state_of(rig, env) == "reverted"


def test_revert_keeps_the_reformatted_clip_in_the_stash_so_the_revert_can_be_undone(
    rig: Rig,
) -> None:
    reformat(rig)
    env_group = first_record(rig, "intent")["group"]
    out = rig.do("journal.revert", {"group": env_group})
    assert out["ok"] is True
    widths = sorted(e.width for e in stash_entries(rig))
    assert widths == [960, 1920]  # the checkpoint copy and the reformatted clip, moved aside


def test_revert_says_so_when_the_restored_clip_does_not_have_the_old_format(rig: Rig) -> None:
    env = reformat(rig)
    stash = stash_entries(rig)[0]
    stash.width = 1900  # the stashed copy does not hold the format the clip had
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert out["result"]["restored"][0]["format"]["matches"] is False
    assert any("not the format" in d for d in out["result"]["degraded"])


def test_revert_twice_is_refused_as_already_reverted(rig: Rig) -> None:
    env = reformat(rig)
    revert(rig, env)
    again = revert(rig, env)
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"
    assert "already reverted" in again["error"]


def test_revert_is_a_conflict_when_the_clip_was_edited_after_the_reformat(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    env = reformat(rig)
    edit_segments(clip)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert clip.width == 960 and clip in reel(rig).children
    assert "force=true" in out["error"]
    forced = revert(rig, env, force=True)
    assert forced["ok"] is True, forced
    assert clip_format(reel(rig).children[-1])["width"] == 1920


def test_revert_is_a_conflict_when_a_later_reformat_changed_the_format_again(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    first = reformat(rig, width=1280, height=720)
    reformat(rig, [wt(clip.get_wiretap_node_id())], width=640, height=360)
    out = revert(rig, first)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "format" in out["error"] and (clip.width, clip.height) == (640, 360)
    assert "force=true" in out["error"]
    assert first["journal"]["group"] not in [
        r.get("reverts") for r in rig.records() if r["type"] == "intent"
    ]


def test_revert_of_the_later_reformat_first_then_the_earlier_one_works(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    first = reformat(rig, width=1280, height=720)
    second = reformat(rig, [wt(clip.get_wiretap_node_id())], width=640, height=360)
    assert revert(rig, second)["ok"] is True
    out = revert(rig, first)
    assert out["ok"] is True, out
    assert clip_format(reel(rig).children[-1])["width"] == 1920


def test_a_clip_whose_format_was_changed_by_hand_is_not_restored_without_force(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    env = reformat(rig)
    clip.width = 800
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert clip.width == 800 and clip in reel(rig).children
    forced = revert(rig, env, force=True)
    assert forced["ok"] is True, forced
    assert clip_format(reel(rig).children[-1])["width"] == 1920
    restore = next(
        r
        for r in rig.records()
        if r["type"] == "intent" and r.get("reverts") == env["journal"]["group"]
    )
    assert "width" in restore["forced"]["formatDiffers"]


def test_a_duplicate_reformatted_again_is_not_deleted_without_force(rig: Rig) -> None:
    env = reformat(rig, duplicate=True)
    made = reel(rig).children[-1]
    made.reformat_changes_uid = False  # a change by hand that leaves the uid alone
    made.reformat(width=640, height=360, ratio=640 / 360, resize_mode="Fill")
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert made in reel(rig).children and "format" in out["error"]
    forced = revert(rig, env, force=True)
    assert forced["ok"] is True, forced
    assert made not in reel(rig).children
    deleting = next(
        r
        for r in rig.records()
        if r["type"] == "intent" and r.get("reverts") == env["journal"]["group"]
    )
    assert "width" in deleting["forced"]["formatDiffers"]


def test_revert_puts_back_every_clip_of_a_call(rig: Rig) -> None:
    targets = [CLIPX, sel(rig, "a"), sel(rig, "b")]
    before = [clip_format(rig.handles[n]) for n in ("clipx", "a", "b")]
    env = reformat(rig, targets)
    out = revert(rig, env)
    assert out["ok"] is True and len(out["result"]["restored"]) == 3, out
    assert [r["index"] for r in out["result"]["restored"]] == [2, 1, 0]  # newest first
    by_name = {str(c.name.get_value()): c for c in reel(rig).children}
    assert [clip_format(by_name[n]) for n in ("CLIPX", "A", "B")] == before
    assert out["affected"] == 3 and state_of(rig, env) == "reverted"


def test_a_partial_revert_puts_back_what_it_can_and_the_next_one_only_the_rest(rig: Rig) -> None:
    a = rig.handles["a"]
    env = reformat(rig, [CLIPX, sel(rig, "a"), sel(rig, "b")])
    edit_segments(a)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert out["partial"] == {"done": 2, "failed": 1, "notRun": 0}
    assert [r["index"] for r in out["result"]["restored"]] == [2, 0]
    assert [r["index"] for r in out["result"]["notRestored"]] == [1]
    assert state_of(rig, env) == "partially_reverted"
    a.versions[0].tracks[0].segments[0].head = 0  # put the edit back by hand
    rest = revert(rig, env)
    assert rest["ok"] is True and [r["index"] for r in rest["result"]["restored"]] == [1], rest
    assert state_of(rig, env) == "reverted"
    assert revert(rig, env)["errorCode"] == "PRECONDITION_UNMET"


def test_revert_of_a_duplicate_call_deletes_the_copy_and_keeps_the_original(rig: Rig) -> None:
    original = rig.handles["clipx"]
    env = reformat(rig, duplicate=True)
    made = reel(rig).children[-1]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert made not in reel(rig).children and original in reel(rig).children
    assert (original.width, original.reformat_calls) == (1920, [])
    row = out["result"]["deleted"][0]
    assert row["uid"] == uid(made) and row["index"] == 0
    assert stash_entries(rig) == [] and state_of(rig, env) == "reverted"
    assert revert(rig, env)["errorCode"] == "PRECONDITION_UNMET"


def test_revert_of_a_duplicate_reformatted_with_a_start_frame_still_matches(rig: Rig) -> None:
    env = reformat(rig, duplicate=True, start_frame=1001, bit_depth=16)
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert out["result"]["deleted"][0]["uid"] is not None


def test_revert_of_a_duplicate_is_a_conflict_when_the_copy_was_changed(rig: Rig) -> None:
    env = reformat(rig, duplicate=True)
    made = reel(rig).children[-1]
    made.name.set_value("renamed")
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert made in reel(rig).children


def test_revert_of_duplicates_of_several_clips_deletes_each_copy(rig: Rig) -> None:
    env = reformat(rig, [CLIPX, sel(rig, "a")], duplicate=True)
    names = reel_names(reel(rig))
    assert names.count("CLIPX") == 2 and names.count("A") == 2
    out = revert(rig, env)
    assert out["ok"] is True and len(out["result"]["deleted"]) == 2
    assert reel_names(reel(rig)).count("CLIPX") == 1 and reel_names(reel(rig)).count("A") == 1


def test_a_call_that_changed_nothing_has_nothing_to_revert(rig: Rig) -> None:
    env = reformat(rig, width=1920, height=1080)
    assert env["ok"] is True and env["journal"]["revert"] is None
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "PRECONDITION_UNMET"
    assert "made nothing" in out["error"]


def test_a_failed_reformat_that_left_the_clip_unchanged_has_nothing_to_revert(rig: Rig) -> None:
    rig.handles["clipx"].reformat_behavior = "noop"
    env = reformat(rig)
    assert env["journal"]["revert"] is None
    assert revert(rig, env)["errorCode"] == "PRECONDITION_UNMET"


def test_a_wrong_size_can_still_be_reverted(rig: Rig) -> None:
    rig.handles["clipx"].reformat_behavior = "wrong_size"
    env = reformat(rig)
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert clip_format(reel(rig).children[-1])["width"] == 1920


def test_the_envelope_names_every_intent_of_the_call(rig: Rig) -> None:
    env = reformat(rig, [CLIPX, sel(rig, "a")])
    intents = [r for r in rig.records() if r["type"] == "intent"]
    assert env["journal"]["ids"] == [i["id"] for i in intents] and len(intents) == 2
    assert env["journal"]["id"] == intents[0]["id"]
    assert {i["group"] for i in intents} == {env["journal"]["group"]}
    assert [i["index"] for i in intents] == [0, 1] and {i["of"] for i in intents} == {2}
    assert intents[0]["kind"] == "desktop_reformat" and intents[0]["mode"] == "in_place"
    assert intents[0]["before"]["width"] == 1920 and intents[0]["requested"]["width"] == 960


def test_the_outcome_records_what_the_revert_works_from(rig: Rig) -> None:
    env = reformat(rig)
    outcome = first_record(rig, "outcome")
    created = outcome["created"]
    assert outcome["ok"] is True and created["mode"] == "in_place"
    assert created["checkpoint"] == first_record(rig, "checkpoint")["id"]
    assert created["before"]["width"] == 1920 and created["after"]["width"] == 960
    assert isinstance(outcome["fingerprintAfter"], str)
    assert env["journal"]["revert"] is not None


def test_an_unanswered_write_stops_the_call_and_shuts_the_write_door(rig: Rig) -> None:
    a = rig.handles["a"]
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=3)  # the write of the second clip
    env = reformat(rig, [CLIPX, sel(rig, "a"), sel(rig, "b")])
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert [r["from"]["name"] for r in env["result"]["reformatted"]] == ["CLIPX"]
    assert rig.handles["b"].reformat_calls == []  # the third clip was never started
    assert a.width == 960  # the second one did run: the answer was lost
    assert "journal.check" in env["hint"]
    assert env["partial"] == {"reformatted": 1, "notReformatted": 2}
    blocked = reformat(rig, [sel(rig, "b")])
    assert blocked["errorCode"] == "POLICY_DENIED" and rig.handles["b"].reformat_calls == []


def test_check_finds_that_the_unanswered_clip_landed_and_the_group_can_then_be_reverted(
    rig: Rig,
) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=3)
    env = reformat(rig, [CLIPX, sel(rig, "a")])
    group = first_record(rig, "intent")["group"]
    checked = rig.do("journal.check", {"group": group})
    assert checked["ok"] is True, checked
    assert [c["verdict"] for c in checked["result"]["checked"]] == ["landed"]
    assert checked["result"]["writesBlocked"] is False
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True and len(out["result"]["restored"]) == 2, out
    assert env["errorCode"] == "TIMEOUT"
    by_name = {str(c.name.get_value()): c for c in reel(rig).children}
    assert (by_name["CLIPX"].width, by_name["A"].width) == (1920, 1280)


def test_check_of_a_write_that_never_ran_is_not_landed_and_lifts_the_block(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="before", skip=2)
    env = reformat(rig)
    assert env["errorCode"] == "TIMEOUT" and rig.handles["clipx"].width == 1920
    checked = rig.do("journal.check", {})
    assert [c["verdict"] for c in checked["result"]["checked"]] == ["not_landed"]
    assert reformat(rig)["ok"] is True


def test_check_of_a_write_that_ran_but_not_as_asked_is_diverged(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="before", skip=2)
    reformat(rig)
    rig.handles["clipx"].width = 1000
    checked = rig.do("journal.check", {})
    assert [c["verdict"] for c in checked["result"]["checked"]] == ["diverged"]


def test_check_names_the_stashed_copy_to_restore_by_hand_when_the_state_is_unclear(
    rig: Rig,
) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="before", skip=2)
    reformat(rig)
    rig.handles["clipx"].width = 1000
    checked = rig.do("journal.check", {})["result"]["checked"][0]
    checkpoint = first_record(rig, "checkpoint")["id"]
    detail = checked["current"]["detail"]
    assert checkpoint in detail and "journal.restore_checkpoint" in detail


def test_a_pending_answer_is_an_unknown_end_too(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = rig.exec

    def pending_on_write(
        code: str,
        thread: str,
        timeout_s: float | None = None,
        label: str | None = None,
        *,
        run_dir: Path | None = None,
        handle: str | None = None,
    ) -> Any:
        result = real(code, thread, timeout_s, label, run_dir=run_dir, handle=handle)
        if label == OP and "_result = _rf_write(" in code:
            return bridge_result(result=None, pending_handle="f" * 32)
        return result

    monkeypatch.setattr(transport, "exec", pending_on_write)
    env = reformat(rig)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert env["pending"] == {"handle": "f" * 32}
    assert reformat(rig)["errorCode"] == "POLICY_DENIED"
    checked = rig.do("journal.check", {})
    assert [c["verdict"] for c in checked["result"]["checked"]] == ["landed"]


def test_check_of_a_duplicate_call_that_ran_whole_is_landed_and_revertible(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=2)  # the reformat of the copy
    env = reformat(rig, duplicate=True)
    assert env["errorCode"] == "TIMEOUT"
    group = first_record(rig, "intent")["group"]
    checked = rig.do("journal.check", {"group": group})["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["landed"]
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True and len(out["result"]["deleted"]) == 1, out
    assert reel_names(reel(rig)).count("CLIPX") == 1


def test_check_of_a_duplicate_made_but_not_reformatted_is_diverged(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)  # the duplicate, then the answer is lost
    reformat(rig, duplicate=True)
    checked = rig.do("journal.check", {})["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["diverged"]
    assert "not reformatted" in checked[0]["current"]["detail"]


def test_check_of_a_duplicate_call_that_never_ran_is_not_landed(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="before", skip=1)
    reformat(rig, duplicate=True)
    checked = rig.do("journal.check", {})["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["not_landed"]
    assert reel_names(reel(rig)).count("CLIPX") == 1


def test_check_of_a_duplicate_revert_whose_answer_was_lost_finds_the_copy_gone(rig: Rig) -> None:
    env = reformat(rig, duplicate=True)
    made = reel(rig).children[-1]
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)  # the format read, then the revert's call
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "TIMEOUT", out
    assert made not in reel(rig).children
    checked = rig.do("journal.check", {})["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["landed"]
    assert checked[0]["op"] == "journal.revert"


def test_the_journal_lists_the_group_as_landed_with_one_intent_per_clip(rig: Rig) -> None:
    env = reformat(rig, [CLIPX, sel(rig, "a")])
    items = rig.do("journal.list", {})["result"]["items"]
    row = next(i for i in items if i["group"] == env["journal"]["group"])
    assert row["state"] == "landed" and row["intents"] == 2
    assert row["ops"] == [OP, OP]


def test_plan_run_refuses_it_with_the_reason(rig: Rig) -> None:
    env = rig.do(
        "plan.run",
        {"steps": [{"op": OP, "args": {"targets": [CLIPX], "width": 960, "height": 540}}]},
    )
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    problem = env["result"]["issues"][0]["problems"][0]
    assert "desktop.reformat cannot be a plan step" in problem
    assert "desktop_reformat" in problem and "stashed copy" in problem
    assert env["result"]["ran"] is False and rig.exec.calls == []
    assert rig.handles["clipx"].reformat_calls == []


def test_the_outcome_records_the_uid_the_reformat_gave_the_clip(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    old_uid = uid(clip)
    env = reformat(rig)
    outcome = first_record(rig, "outcome")
    assert uid(clip) != old_uid
    assert outcome["replacedUid"] == old_uid and outcome["targetUid"] == uid(clip)
    created = outcome["created"]
    assert created["uid"] == uid(clip) and created["selector"] == wt(clip.get_wiretap_node_id())
    assert env["result"]["reformatted"][0]["to"]["uid"] == uid(clip)


def test_revert_finds_the_clip_by_the_uid_the_reformat_gave_it(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    env = reformat(rig, width=1280, height=720)
    assert clip_format(clip)["width"] == 1280
    out = revert(rig, env)
    assert out["ok"] is True and out["errorCode"] is None, out
    assert clip_format(reel(rig).children[-1])["width"] == 1920


def test_revert_of_a_duplicate_finds_the_copy_by_the_uid_the_reformat_gave_it(rig: Rig) -> None:
    env = reformat(rig, duplicate=True)
    made = reel(rig).children[-1]
    assert uid(made) == env["result"]["reformatted"][0]["to"]["uid"]
    out = revert(rig, env)
    assert out["ok"] is True and out["result"]["deleted"][0]["deleted"] is True, out
    assert made not in reel(rig).children


def test_duplicates_of_two_clips_in_one_reel_are_both_made_and_reverted(rig: Rig) -> None:
    env = reformat(rig, [CLIPX, sel(rig, "a")], duplicate=True)
    assert env["ok"] is True and env["affected"] == 2, env
    out = revert(rig, env)
    assert out["ok"] is True and [d["deleted"] for d in out["result"]["deleted"]] == [True, True]


def test_check_finds_a_clip_whose_write_was_lost_under_its_new_uid(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    old_uid = uid(clip)
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=2)
    reformat(rig)
    assert uid(clip) != old_uid  # the write ran and moved the clip's uid; no outcome says so
    group = first_record(rig, "intent")["group"]
    checked = rig.do("journal.check", {"group": group})
    assert [c["verdict"] for c in checked["result"]["checked"]] == ["landed"], checked
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True and clip_format(reel(rig).children[-1])["width"] == 1920, out


def test_check_does_not_guess_when_two_new_entries_have_the_clips_name(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=2)
    reformat(rig)
    twin = add_media_clip(reel(rig), "CLIPX")
    assert twin is not clip
    group = first_record(rig, "intent")["group"]
    checked = rig.do("journal.check", {"group": group})
    assert [c["verdict"] for c in checked["result"]["checked"]] == ["diverged"], checked
