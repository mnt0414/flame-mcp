"""Tests for `desktop.delete`.

Clips / sequences are copied into the stash and deleted, journal.revert copies them back; the
refusals that leave nothing behind, a partial delete, a parent that is gone, the Timeline put back
on what it showed, journal.check of an answer that was lost. Run through the real Runtime with a
scripted `transport.exec` over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyTime
from journal_test_helpers import Rig
from media_p31b_helpers import add_clip
from p32b_helpers import (
    CLIPX,
    DELETE,
    LIB_CLIP1,
    LIBREEL,
    REEL1,
    S1,
    S2,
    S3,
    delete,
    make_p32b_rig,
    reel2,
    show,
    shown,
    uid,
    wt,
)
from timeline_p32a_helpers import (
    checkpoints,
    fingerprint_of_target,
    reel_names,
    reel_uids,
    rows,
    stash_entries,
    stash_library,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def reel(rig: Rig) -> Any:
    return rig.handles["reel"]


def revert(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    return rig.do("journal.revert", {"group": env["journal"]["group"]})


def untouched_world(rig: Rig) -> bool:
    return (
        stash_library(rig) is None
        and rig.records() == []
        and rig.flame.media_panel.copy_calls == []
    )


def test_deletes_a_clip_after_stashing_it(rig: Rig) -> None:
    before = fingerprint_of_target(rig, CLIPX)
    old_uid = uid(rig.handles["clipx"])
    env = delete(rig, [CLIPX])
    assert env["ok"] is True, env
    assert "CLIPX" not in reel_names(reel(rig))
    stash = stash_entries(rig)
    assert [str(e.name.get_value()) for e in stash] == ["CLIPX"]
    row = env["result"]["deleted"][0]
    assert row["from"]["uid"] == old_uid and row["from"]["name"] == "CLIPX"
    assert row["from"]["parent"]["name"] == "Reel 1"
    assert row["stash"]["uid"] == uid(stash[0]) and row["fingerprint"] == before
    assert row["checkpoint"] == checkpoints(rig)[0]["id"]
    assert env["result"]["partial"] is False and env["result"]["notDeleted"] == []
    assert env["journal"]["revert"] == {
        "op": "journal.revert",
        "args": {"group": env["journal"]["group"]},
    }
    assert env["undoSteps"] == 0 and env["affected"] == 1


def test_the_checkpoint_is_recorded_before_the_intent_and_the_intent_before_the_write(
    rig: Rig,
) -> None:
    delete(rig, [CLIPX])
    kinds = [r["type"] + ":" + str(r.get("kind", "")) for r in rig.records()]
    assert kinds == ["checkpoint:timeline", "intent:desktop_delete", "outcome:"]
    write = [c for c in rig.exec.calls if c.label == DELETE][-1]
    assert write.journal_types == ["checkpoint", "intent"]
    intent = rig.records()[1]
    assert intent["checkpoints"] == [rig.records()[0]["id"]]
    assert (
        intent["sources"][0]["name"] == "CLIPX"
        and intent["stashes"][0]["uid"] == intent["sources"][0]["uid"]
    )


def test_deletes_a_sequence_and_several_targets_at_once(rig: Rig) -> None:
    env = delete(rig, [S2, CLIPX, S3])
    assert env["ok"] is True and env["result"]["requested"] == 3
    assert reel_names(reel(rig)) == ["c1", "S1"]
    assert sorted(str(e.name.get_value()) for e in stash_entries(rig)) == ["CLIPX", "S2", "S3"]
    assert [d["from"]["name"] for d in env["result"]["deleted"]] == ["S2", "CLIPX", "S3"]
    assert len(checkpoints(rig)) == 3
    assert {r["group"] for r in checkpoints(rig)} == {env["journal"]["group"]}


def test_a_library_clip_is_refused_and_nothing_is_copied_or_journaled(rig: Rig) -> None:
    lib_reel = rig.handles["libreel"]
    before = reel_names(lib_reel)
    env = delete(rig, [LIB_CLIP1])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "inside a library" in env["error"] and "never deleted" in env["error"]
    assert "targets[0]" in env["error"]
    assert reel_names(lib_reel) == before
    assert untouched_world(rig)


def test_anything_in_a_library_is_refused_whatever_the_folder_or_reel(rig: Rig) -> None:
    lib = rig.handles["lib"]
    folder = lib.create_folder("F")
    in_folder = add_clip(folder, "in_folder")
    direct = add_clip(lib, "direct")
    for target in (in_folder, direct):
        env = delete(rig, [wt(target.get_wiretap_node_id())])
        assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    mixed = delete(rig, [CLIPX, LIB_CLIP1])
    assert mixed["errorCode"] == "PRECONDITION_UNMET" and "targets[1]" in mixed["error"]
    assert "CLIPX" in reel_names(reel(rig))
    assert untouched_world(rig)


def test_a_desktop_clip_that_was_moved_into_a_library_is_refused_too(rig: Rig) -> None:
    moved = rig.flame.media_panel.move(rig.handles["clipx"], rig.handles["libreel"])
    env = delete(rig, [wt(moved[0].get_wiretap_node_id())])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert untouched_world(rig)


def test_the_target_can_be_addressed_by_path_and_by_selection(rig: Rig) -> None:
    env = delete(rig, [{"kind": "path", "path": ["RG", "Reel 1", "CLIPX"], "root": "desktop"}])
    assert env["ok"] is True, env
    rig.flame.media_panel.selected_entries = [rig.handles["s3"]]
    assert delete(rig, [{"kind": "selection"}])["ok"] is True
    assert reel_names(reel(rig)) == ["c1", "S1", "S2"]


def test_revert_copies_a_clip_back_into_its_reel_with_a_new_uid(rig: Rig) -> None:
    before = fingerprint_of_target(rig, CLIPX)
    old = uid(rig.handles["clipx"])
    env = delete(rig, [CLIPX])
    out = revert(rig, env)
    assert out["ok"] is True, out
    back = out["result"]["reverted"][0]
    assert back["from"]["name"] == "CLIPX" and back["fingerprintMatches"] is True
    assert reel_names(reel(rig))[-1] == "CLIPX"
    assert back["to"]["uid"] == uid(reel(rig).children[-1]) != old
    assert fingerprint_of_target(rig, back["to"]["selector"]) == before
    assert any("NEW uid" in n for n in out["result"]["notes"])
    assert len(stash_entries(rig)) == 1  # the stash is kept


def test_revert_puts_back_a_list_and_the_group_reads_as_reverted(rig: Rig) -> None:
    env = delete(rig, [S2, S3])
    out = revert(rig, env)
    assert out["ok"] is True and len(out["result"]["reverted"]) == 2
    assert reel_names(reel(rig)) == ["c1", "S1", "CLIPX", "S2", "S3"]
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert states[env["journal"]["group"]] == "reverted"
    again = revert(rig, env)
    assert again["errorCode"] == "PRECONDITION_UNMET" and "already reverted" in again["error"]


def test_revert_when_the_old_parent_is_gone_is_a_conflict_and_nothing_is_copied(rig: Rig) -> None:
    rig.flame.media_panel.move(rig.handles["clipx"], rig.handles["reel2"])
    moved = wt(rig.handles["reel2"].children[-1].get_wiretap_node_id())
    env = delete(rig, [moved])
    rig.handles["reel_group"].reels.remove(rig.handles["reel2"])
    rig.handles["reel_group"].sync_children()
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert "is gone" in out["error"]
    assert out["result"]["notReverted"][0]["errorCode"] == "CONFLICT"
    assert "partial" not in out or out["partial"] is None


def test_a_partial_revert_restores_what_it_can_and_a_second_one_does_not_copy_twice(
    rig: Rig,
) -> None:
    rig.flame.media_panel.move(rig.handles["clipx"], rig.handles["reel2"])
    moved = wt(rig.handles["reel2"].children[-1].get_wiretap_node_id())
    env = delete(rig, [S2, moved])
    group = rig.handles["reel_group"]
    group.reels.remove(rig.handles["reel2"])
    group.sync_children()
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert out["partial"] == {"done": 1, "failed": 1, "notRun": 0}
    assert [r["from"]["name"] for r in out["result"]["reverted"]] == ["S2"]
    assert reel_names(reel(rig)).count("S2") == 1
    # the parent comes back: only what the first revert did not put back is copied
    new_reel = group.create_reel("Reel 2")
    assert new_reel is not None
    second = revert(rig, env)
    assert second["errorCode"] == "CONFLICT"  # still the old parent (a new reel has a new uid)
    assert reel_names(reel(rig)).count("S2") == 1
    assert second["result"]["notReverted"][0]["from"]["name"] == "CLIPX"


def test_a_second_revert_after_everything_was_restored_in_two_steps_is_refused(
    rig: Rig,
) -> None:
    env = delete(rig, [S2, S3])
    first = revert(rig, env)
    assert first["ok"] is True
    again = revert(rig, env)
    assert again["ok"] is False


def test_revert_refuses_when_the_stash_copy_was_removed(rig: Rig) -> None:
    env = delete(rig, [S2])
    lib = stash_library(rig)
    assert lib is not None
    stash_reel = lib.reels[0]
    for entry in list(stash_reel.children):
        rig.flame.delete(entry, confirm=False)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert "stashed copy is gone" in out["error"]
    assert "S2" not in reel_names(reel(rig))


@pytest.mark.parametrize(
    "selector_of",
    [
        lambda rig: REEL1,
        lambda rig: wt(rig.handles["reel_group"].get_wiretap_node_id()),
        lambda rig: wt(rig.handles["lib"].get_wiretap_node_id()),
        lambda rig: {"kind": "path", "path": ["BG1"], "root": "desktop"},
    ],
    ids=["reel", "reel group", "library", "batch group"],
)
def test_a_container_is_refused_and_nothing_is_copied_or_journaled(
    rig: Rig, selector_of: Any
) -> None:
    env = delete(rig, [selector_of(rig)])
    assert env["ok"] is False and env["errorCode"] in ("PRECONDITION_UNMET", "SELECTOR_TYPE")
    assert untouched_world(rig)


def test_a_container_among_valid_targets_stops_the_whole_call(rig: Rig) -> None:
    env = delete(rig, [CLIPX, REEL1])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "only a clip or a sequence" in env["error"]
    assert untouched_world(rig)
    assert "CLIPX" in reel_names(reel(rig))


def test_something_inside_a_batch_reel_is_refused(rig: Rig) -> None:
    batch_reel = rig.handles["bg1"].create_reel("Schematic Reel 1")
    clip = batch_reel.children[0] if batch_reel.children else None
    if clip is None:
        rig.flame.media_panel.copy(rig.handles["clipx"], batch_reel)
        clip = batch_reel.children[-1]
    env = delete(rig, [wt(clip.get_wiretap_node_id())])
    assert env["errorCode"] == "INVALID_ARGS" and "Batch group" in env["error"]
    assert untouched_world(rig) or stash_library(rig) is None


def test_something_inside_the_stash_is_refused_and_points_at_the_purge(rig: Rig) -> None:
    env = delete(rig, [S2])
    stashed = stash_entries(rig)[0]
    out = delete(rig, [wt(stashed.get_wiretap_node_id())])
    assert out["errorCode"] == "PRECONDITION_UNMET"
    assert "journal.purge_checkpoints" in out["error"]
    assert env["ok"] is True and len(stash_entries(rig)) == 1


def test_the_same_thing_twice_is_invalid_even_through_two_selectors(rig: Rig) -> None:
    env = delete(rig, [S2, {"kind": "path", "path": ["RG", "Reel 1", "S2"], "root": "desktop"}])
    assert env["errorCode"] == "INVALID_ARGS" and "listed twice" in env["error"]
    assert untouched_world(rig)


@pytest.mark.parametrize("targets", [[], [S1] * 51])
def test_the_number_of_targets_is_bounded(rig: Rig, targets: list[dict[str, Any]]) -> None:
    env = delete(rig, targets)
    assert env["errorCode"] == "INVALID_ARGS"
    assert untouched_world(rig)


def test_a_selector_that_finds_nothing_leaves_nothing_behind(rig: Rig) -> None:
    env = delete(rig, [CLIPX, wt("/wt/RG/Reel 1/NOPE")])
    assert env["errorCode"] == "SELECTOR_EMPTY"
    assert untouched_world(rig)


def test_readonly_policy_denies_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from flame_mcp.policy import Policy

    rig = make_p32b_rig(monkeypatch, tmp_path)
    rig.runtime.policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    env = delete(rig, [CLIPX])
    assert env["errorCode"] == "POLICY_DENIED"
    assert untouched_world(rig)


def test_plan_run_refuses_it(rig: Rig) -> None:
    env = rig.do("plan.run", {"steps": [{"op": DELETE, "args": {"targets": [CLIPX]}}]})
    assert env["errorCode"] == "INVALID_ARGS" and "cannot be a plan step" in str(env["result"])
    assert untouched_world(rig)


def test_a_stash_copy_that_fails_ends_the_call_before_anything_is_deleted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rig.flame.media_panel.copy
    calls = []

    def flaky(source: Any, destination: Any, duplicate_action: str = "add") -> Any:
        calls.append(source)
        if len(calls) == 2:
            raise RuntimeError("scripted copy failure")
        return real(source, destination, duplicate_action)

    monkeypatch.setattr(rig.flame.media_panel, "copy", flaky)
    env = delete(rig, [S2, S3])
    assert env["ok"] is False and "scripted copy failure" in env["error"]
    assert "nothing was deleted" in env["error"]
    assert reel_names(reel(rig)) == ["c1", "S1", "S2", "S3", "CLIPX"]
    cps = checkpoints(rig)
    assert len(cps) == 1 and cps[0]["source"]["name"] == "S2"
    assert [r["type"] for r in rig.records() if r["type"] != "checkpoint"] == []


def test_a_stash_copy_with_another_fingerprint_is_not_trusted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rig.flame.media_panel.copy

    def tampering(source: Any, destination: Any, duplicate_action: str = "add") -> Any:
        made = real(source, destination, duplicate_action)
        made[0].versions[0].tracks[0].segments[0].record_out = PyTime(999)
        return made

    monkeypatch.setattr(rig.flame.media_panel, "copy", tampering)
    env = delete(rig, [S2])
    assert env["ok"] is False and "fingerprint" in env["error"]
    assert "S2" in reel_names(reel(rig))
    assert checkpoints(rig) == []


def test_a_target_that_changed_between_the_read_and_the_write_is_a_conflict(rig: Rig) -> None:
    def edit(label: str | None) -> None:
        writes = [c for c in rig.exec.calls if c.label == DELETE]
        if label == DELETE and len(writes) == 3:  # the call about to run is the write
            seg = rig.handles["s2"].versions[0].tracks[0].segments[0]
            seg.trim_head(2)

    rig.exec.before_run = edit
    env = delete(rig, [S2])
    assert env["ok"] is False and env["errorCode"] == "CONFLICT"
    assert "changed between the read and the write" in env["error"]
    assert "S2" in reel_names(reel(rig))
    outcome = next(r for r in rig.records() if r["type"] == "outcome")
    assert outcome["ok"] is False and outcome["created"] is None


def test_nothing_deleted_is_a_silent_noop(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rig.flame, "delete", lambda obj, confirm=True: False)
    env = delete(rig, [S2])
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "S2" in reel_names(reel(rig))
    states = {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}
    assert list(states.values()) == ["failed"]


def test_some_deleted_is_ok_with_partial(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = rig.flame.delete

    def picky(obj: Any, confirm: bool = True) -> bool:
        if str(obj.name.get_value()) == "S3":
            return False
        return bool(real(obj, confirm))

    monkeypatch.setattr(rig.flame, "delete", picky)
    env = delete(rig, [S2, S3])
    assert env["ok"] is True and env["result"]["partial"] is True
    assert env["partial"] == {"deleted": 1, "notDeleted": 1}
    assert [d["from"]["name"] for d in env["result"]["deleted"]] == ["S2"]
    assert env["result"]["notDeleted"][0]["from"]["name"] == "S3"
    assert reel_names(reel(rig)) == ["c1", "S1", "S3", "CLIPX"]
    assert any("notDeleted" in d for d in env.get("degraded", []))
    out = revert(rig, env)
    assert out["ok"] is True and len(out["result"]["reverted"]) == 1


def test_deleting_the_shown_sequence_is_reported_and_reverting_opens_it_again(rig: Rig) -> None:
    show(rig, "s2")
    env = delete(rig, [S2])
    assert env["ok"] is True
    row = env["result"]["deleted"][0]
    assert row["wasDisplayed"] is True
    assert env["result"]["shownBefore"]["name"] == "S2"
    # measured on Flame 2025.2.4: the Timeline shows nothing, not another one
    assert shown(rig) is None
    assert env["result"]["shownAfterDelete"] is None and env["result"]["shownAfter"] is None
    out = revert(rig, env)
    assert out["ok"] is True
    restored = out["result"]["reverted"][0]
    assert restored["wasDisplayed"] is True and out["result"]["reshown"] is True
    assert uid(shown(rig)) == restored["to"]["uid"]
    assert out["result"]["shownAfter"]["uid"] == restored["to"]["uid"]


def test_the_timeline_is_put_back_when_deleting_another_sequence_switches_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    show(rig, "s2")
    real = rig.flame.delete

    def switching(obj: Any, confirm: bool = True) -> bool:
        result = bool(real(obj, confirm))
        rig.handles["s3"].open()  # what a delete may do to the Timeline: not measured
        return result

    monkeypatch.setattr(rig.flame, "delete", switching)
    env = delete(rig, [S1])
    assert env["ok"] is True
    assert env["result"]["shownAfterDelete"]["name"] == "S3"
    assert env["result"]["reshown"] is True
    assert shown(rig) is rig.handles["s2"] and env["result"]["shownAfter"]["name"] == "S2"


def test_deleting_another_sequence_leaves_the_timeline_alone(rig: Rig) -> None:
    show(rig, "s2")
    env = delete(rig, [S1])
    assert env["result"]["reshown"] is None
    assert shown(rig) is rig.handles["s2"]
    assert env["result"]["shownBefore"]["name"] == env["result"]["shownAfter"]["name"] == "S2"


def test_check_of_a_delete_whose_answer_was_lost_and_which_ran_is_landed(rig: Rig) -> None:
    rig.exec.fault(DELETE, "TIMEOUT", when="after", skip=2)  # read, stash, then the write
    env = delete(rig, [S2])
    assert env["errorCode"] == "TIMEOUT"
    assert "S2" not in reel_names(reel(rig))
    out = rig.do("journal.check", {})
    entry = out["result"]["checked"][0]
    assert entry["verdict"] == "landed" and entry["perKey"] == {"desktop": "after"}
    check = next(r for r in rig.records() if r["type"] == "check")
    assert check["created"]["deletes"][0]["from"]["name"] == "S2"
    back = revert(rig, env)
    assert back["ok"] is True, back
    assert "S2" in reel_names(reel(rig))


def test_check_of_a_delete_that_never_ran_is_not_landed(rig: Rig) -> None:
    rig.exec.fault(DELETE, "TIMEOUT", when="before", skip=2)
    env = delete(rig, [S2])
    assert env["errorCode"] == "TIMEOUT"
    out = rig.do("journal.check", {})
    assert out["result"]["checked"][0]["verdict"] == "not_landed"
    assert "S2" in reel_names(reel(rig))
    # the door is open again
    assert delete(rig, [S3])["ok"] is True


def test_check_with_some_gone_is_diverged_and_names_the_stashed_copies(rig: Rig) -> None:
    rig.exec.fault(DELETE, "TIMEOUT", when="before", skip=2)
    delete(rig, [S2, S3])
    rig.flame.delete(rig.handles["s2"], confirm=False)
    out = rig.do("journal.check", {})
    entry = out["result"]["checked"][0]
    assert entry["verdict"] == "diverged"
    assert entry["current"]["detail"].startswith("some of the things are gone")


def test_the_stash_reel_takes_every_target_in_one_reel(rig: Rig) -> None:
    delete(rig, [S2, S3])
    lib = stash_library(rig)
    assert lib is not None and len(lib.reels) == 1
    assert reel_uids(lib.reels[0]) == [uid(e) for e in stash_entries(rig)]


def test_two_deletes_stack_in_the_stash_and_revert_one_by_one(rig: Rig) -> None:
    first = delete(rig, [S2])
    second = delete(rig, [S3])
    assert len(stash_entries(rig)) == 2
    assert revert(rig, first)["ok"] is True
    assert reel_names(reel(rig)) == ["c1", "S1", "CLIPX", "S2"]
    assert revert(rig, second)["ok"] is True
    assert rows(rig.handles["reel"].children[-1])[0][1:3] == [0, 23]
    assert reel2(rig)["kind"] == "wiretap" and LIBREEL["kind"] == "wiretap"
