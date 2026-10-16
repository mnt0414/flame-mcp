"""Tests for `desktop.move` and its journal records.

Also `journal.revert` and `journal.check`. See media_p31b_helpers.py for the rig and the fake."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import (
    PyFolder,
    adopt,
)
from journal_test_helpers import Rig
from media_p31b_helpers import (
    C1,
    CLIP1,
    LIB,
    LIB_WT,
    LIBREEL,
    MOVE,
    REEL1,
    add_clip,
    add_seq,
    batch_reel,
    make_media_rig,
    mv,
    names,
    reel2_selector,
    revert,
    states,
    types_in,
    write_codes,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_media_rig(monkeypatch, tmp_path)


def desk_clips(rig: Rig, *clip_names: str) -> list[dict[str, Any]]:
    """Clips of a third Desktop reel (the second place a source can come from)."""
    if "reel3" not in rig.handles:
        rig.handles["reel3"] = rig.handles["reel_group"].create_reel("Reel 3")
    return [wt(add_clip(rig.handles["reel3"], n)) for n in clip_names]


def reel3_selector(rig: Rig) -> dict[str, Any]:
    return wt(rig.handles["reel3"])


def test_move_a_clip_to_another_desktop_reel_gives_the_new_selector_and_kills_the_old(
    rig: Rig,
) -> None:
    clip = rig.handles["reel"].children[0]
    old_uid = clip.uid.get_value()
    env = mv(rig, [C1], reel2_selector(rig))
    assert env["ok"] is True, env
    result = env["result"]
    assert (result["requested"], result["returned"]) == (1, 1)
    [row] = result["moved"]
    assert row["from"]["uid"] == old_uid and row["from"]["name"] == "c1"
    assert row["from"]["type"] == "PyClip"
    assert row["from"]["parent"]["name"] == "Reel 1"
    assert row["from"]["parent"]["selector"] == REEL1
    assert row["to"]["uid"] != old_uid and row["to"]["name"] == "c1"
    assert row["to"]["selector"] == {"kind": "wiretap", "id": row["to"]["wiretapId"]}
    assert "partial" not in result or result["partial"] is False
    assert names(rig.handles["reel_group"].reels[0]) == [] and names(rig.handles["reel2"]) == ["c1"]
    old = rig.do("desktop.info", {"target": C1})
    assert old["ok"] is False and old["errorCode"] == "SELECTOR_EMPTY"
    new = rig.do("desktop.info", {"target": row["to"]["selector"]})
    assert new["ok"] is True and new["result"]["uid"] == row["to"]["uid"]
    assert env["affected"] == 1 and env["undoSteps"] == 0
    assert rig.flame.find_by_uid(old_uid) is None


def test_move_a_sequence_and_a_list_in_the_order_given_to_the_end_of_the_reel(rig: Rig) -> None:
    reel2 = rig.handles["reel2"]
    add_clip(reel2, "existing")
    seq = add_seq(rig.handles["reel_group"].reels[0], "S1")
    env = mv(rig, [wt(seq), C1], reel2_selector(rig))
    assert env["ok"] is True, env
    assert [m["from"]["name"] for m in env["result"]["moved"]] == ["S1", "c1"]
    assert [m["to"]["type"] for m in env["result"]["moved"]] == ["PySequence", "PyClip"]
    assert names(reel2) == ["existing", "S1", "c1"]
    assert env["affected"] == 2


def test_move_a_desktop_clip_into_a_library_reel_is_allowed(rig: Rig) -> None:
    into_library = mv(rig, [C1], LIBREEL)
    assert into_library["ok"] is True, into_library
    assert "c1" in names(rig.handles["libreel"])
    assert into_library["journal"]["revert"] is not None


def test_move_refuses_a_source_inside_a_library_and_writes_nothing(rig: Rig) -> None:
    folder = PyFolder("F", "uid-f", f"{LIB_WT}/F")
    rig.handles["lib"]._children.append(folder)
    adopt(rig.handles["lib"], [folder])
    in_folder = add_clip(folder, "in_folder")
    for source in (CLIP1, wt(in_folder)):
        for destination in (reel2_selector(rig), LIBREEL, LIB):
            env = mv(rig, [source], destination)
            assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
            assert "inside a library" in env["error"] and "desktop.copy" in env["error"]
            assert "sources[0]" in env["error"]
    library_before = names(rig.handles["libreel"])
    mixed = mv(rig, [C1, CLIP1], reel2_selector(rig))
    assert mixed["errorCode"] == "PRECONDITION_UNMET" and "sources[1]" in mixed["error"]
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []
    assert names(rig.handles["libreel"]) == library_before and names(rig.handles["reel2"]) == []
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]


def test_a_clip_moved_into_a_library_cannot_be_moved_out_again_by_move(rig: Rig) -> None:
    mv(rig, [C1], LIBREEL)
    moved = next(c for c in rig.handles["libreel"].children if str(c.name.get_value()) == "c1")
    env = mv(rig, [wt(moved)], reel2_selector(rig))
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert "c1" in names(rig.handles["libreel"]) and names(rig.handles["reel2"]) == []


def test_revert_of_a_move_into_a_library_takes_it_back_out_of_the_library(rig: Rig) -> None:
    library_before = names(rig.handles["libreel"])
    env = mv(rig, [C1], LIBREEL)
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]
    assert names(rig.handles["libreel"]) == library_before


def test_move_into_a_folder_and_a_library(rig: Rig) -> None:
    folder = PyFolder("F", "uid-f", f"{LIB_WT}/F")
    rig.handles["lib"]._children.append(folder)
    adopt(rig.handles["lib"], [folder])
    [second] = desk_clips(rig, "d1")
    in_folder = mv(rig, [second], {"kind": "wiretap", "id": f"{LIB_WT}/F"})
    in_library = mv(rig, [C1], LIB)
    assert in_folder["ok"] is True and in_library["ok"] is True
    assert names(folder) == ["d1"] and "c1" in names(rig.handles["lib"])


def test_move_sources_by_path_and_selection_are_re_addressed_by_wiretap(rig: Rig) -> None:
    rig.flame.media_panel.selected_entries = [rig.handles["reel"].children[0]]
    env = mv(
        rig,
        [{"kind": "selection"}],
        {"kind": "path", "root": "desktop", "path": ["RG", "Reel 2"]},
    )
    assert env["ok"] is True, env
    intent = rig.records()[0]
    assert intent["target"] == reel2_selector(rig)
    assert intent["sources"][0]["selector"] == C1
    [code] = write_codes(rig, MOVE)
    assert "_resolve_wiretap('/wt/c1')" in code and "_resolve_wiretap('/wt/RG/Reel 2')" in code
    assert "_resolve_selection" not in code and "_resolve_path" not in code


def test_move_journals_every_sources_old_parent_name_and_type(rig: Rig) -> None:
    [second_clip] = desk_clips(rig, "d1")
    mv(rig, [C1, second_clip], reel2_selector(rig))
    intent, outcome = rig.records()
    assert intent["kind"] == "desktop_move" and intent["op"] == MOVE
    assert intent["target"] == reel2_selector(rig) and intent["existing"] == []
    first, second = intent["sources"]
    assert (first["name"], first["type"]) == ("c1", "PyClip")
    assert first["parent"]["selector"] == REEL1 and first["parent"]["name"] == "Reel 1"
    assert second["parent"]["selector"] == reel3_selector(rig)
    assert second["parent"]["name"] == "Reel 3"
    assert [m["from"]["name"] for m in outcome["created"]["moves"]] == ["c1", "d1"]
    assert outcome["created"]["destination"]["name"] == "Reel 2"
    assert outcome["readBack"] == {"requested": 2, "returned": 2, "moved": 2}
    phases = [c for c in rig.exec.calls if c.label == MOVE]
    assert (
        len(phases) == 2 and phases[0].journal_types == [] and phases[1].journal_types == ["intent"]
    )


def test_move_refuses_a_batch_group_reel_as_destination_or_as_source(rig: Rig) -> None:
    reel = batch_reel(rig)
    clip = add_clip(reel, "bc")
    as_dest = mv(rig, [C1], wt(reel))
    as_source = mv(rig, [wt(clip)], reel2_selector(rig))
    for env in (as_dest, as_source):
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
        assert "Batch group" in env["error"]
    assert "batch.add_clips" in as_dest["hint"]
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []


def test_move_refuses_the_cases_that_were_not_measured_or_make_no_sense(rig: Rig) -> None:
    same_reel = mv(rig, [C1], REEL1)
    assert same_reel["errorCode"] == "PRECONDITION_UNMET" and "already in" in same_reel["error"]
    twice = mv(rig, [C1, C1], reel2_selector(rig))
    assert twice["errorCode"] == "INVALID_ARGS" and "twice" in twice["error"]
    reel_source = mv(rig, [REEL1], reel2_selector(rig))
    assert reel_source["errorCode"] == "SELECTOR_TYPE" and "sources[0]" in reel_source["error"]
    clip_dest = mv(rig, [C1], CLIP1)
    assert (
        clip_dest["errorCode"] == "SELECTOR_TYPE"
        and "destination is a PyClip" in (clip_dest["error"])
    )
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []


def test_move_argument_checks(rig: Rig) -> None:
    for sources, needle in (([], "1 to 50"), ([C1] * 51, "1 to 50")):
        env = mv(rig, sources, reel2_selector(rig))
        assert env["errorCode"] == "INVALID_ARGS" and needle in env["error"]
    env = mv(rig, [C1], {"kind": "selection"})
    assert env["errorCode"] == "INVALID_ARGS" and "destination" in env["error"]
    env = mv(rig, [{"kind": "batch", "name": "BG1"}], reel2_selector(rig))
    assert env["errorCode"] == "INVALID_ARGS" and "not accepted here" in env["error"]
    assert rig.exec.calls == []


def test_a_move_that_moves_nothing_is_a_silent_noop_with_nothing_to_revert(rig: Rig) -> None:
    rig.flame.media_panel.move = lambda source, destination: []
    env = mv(rig, [C1], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert env["result"]["returned"] == 0 and env["result"]["moved"] == []
    assert env["journal"]["revert"] is None
    _intent, outcome = rig.records()
    assert outcome["created"] is None and outcome["errorCode"] == "SILENT_NOOP"
    assert outcome["readBack"] == {"requested": 1, "returned": 0, "moved": 0}
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]


def test_a_move_that_moves_only_some_is_partial_and_its_revert_puts_back_only_those(
    rig: Rig,
) -> None:
    real = rig.flame.media_panel.move
    rig.flame.media_panel.move = lambda source, destination: real(source[0], destination)
    [second] = desk_clips(rig, "d1")
    env = mv(rig, [C1, second], reel2_selector(rig))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["partial"] is True and env["partial"] == {"moved": 1, "notMoved": 1}
    assert [m["from"]["name"] for m in result["moved"]] == ["c1"]
    assert [m["from"]["name"] for m in result["notMoved"]] == ["d1"]
    assert any("notMoved" in note for note in env["degraded"])
    assert "d1" in names(rig.handles["reel3"])
    rig.flame.media_panel.move = real
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert len(out["result"]["reverted"]) == 1 and names(rig.handles["reel_group"].reels[0]) == [
        "c1"
    ]


def test_two_sources_with_the_same_name_are_paired_by_what_flame_answered(rig: Rig) -> None:
    first = rig.handles["reel"].children[0]  # c1 in Reel 1
    twin = add_clip(rig.handles["reel_group"].create_reel("Reel 3"), "c1")
    real = rig.flame.media_panel.move

    def reversed_in_the_destination(source: Any, destination: Any) -> list[Any]:
        real(list(source)[::-1], destination)  # lands as [twin, first], answers as given
        return list(source)

    rig.flame.media_panel.move = reversed_in_the_destination
    env = mv(rig, [wt(first), wt(twin)], reel2_selector(rig))
    assert env["ok"] is True, env
    assert [m["to"]["uid"] for m in env["result"]["moved"]] == [
        first.uid.get_value(),
        twin.uid.get_value(),
    ]
    assert [m["from"]["parent"]["name"] for m in env["result"]["moved"]] == ["Reel 1", "Reel 3"]


def test_a_source_that_still_has_its_old_uid_after_the_move_is_reported(rig: Rig) -> None:
    def keeps_the_uid(source: Any, destination: Any) -> list[Any]:
        obj = source
        obj.parent._children = [c for c in obj.parent._children if c is not obj]
        adopt(destination, [obj])
        destination._children.append(obj)
        return [obj]

    rig.flame.media_panel.move = keeps_the_uid
    env = mv(rig, [C1], reel2_selector(rig))
    assert env["ok"] is True, env
    assert any("old uid" in note for note in env["degraded"])


def test_move_conflicts_when_the_destination_changed_between_the_read_and_the_write(
    rig: Rig,
) -> None:
    def sneak(label: str | None) -> None:
        if label == MOVE and len(rig.exec.calls) == 2:
            add_clip(rig.handles["reel2"], "sneaked")

    rig.exec.before_run = sneak
    env = mv(rig, [C1], reel2_selector(rig))
    assert env["errorCode"] == "CONFLICT" and rig.flame.media_panel.move_calls == []


def test_revert_of_a_move_puts_the_things_back_in_their_old_parents_with_new_selectors(
    rig: Rig,
) -> None:
    [second] = desk_clips(rig, "d1")
    env = mv(rig, [C1, second], reel2_selector(rig))
    moved_uids = [m["to"]["uid"] for m in env["result"]["moved"]]
    out = revert(rig, env)
    assert out["ok"] is True, out
    result = out["result"]
    assert result["reverted"] == out["result"]["reverted"] and len(result["reverted"]) == 2
    for row, name in zip(result["reverted"], ("c1", "d1"), strict=True):
        assert row["to"]["name"] == name and row["to"]["selector"]["kind"] == "wiretap"
        assert row["to"]["uid"] not in moved_uids  # the uid changed AGAIN
        assert rig.do("desktop.info", {"target": row["to"]["selector"]})["ok"] is True
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]
    assert names(rig.handles["reel3"])[-1] == "d1" and names(rig.handles["reel2"]) == []
    assert out["affected"] == 2
    assert any("NEW uids" in note for note in result["notes"])
    assert states(rig)[env["journal"]["group"]] == "reverted"
    assert revert(rig, env)["errorCode"] == "PRECONDITION_UNMET"
    for uid in moved_uids:
        assert rig.flame.find_by_uid(uid) is None


def test_revert_of_a_move_goes_to_the_end_of_the_old_parent_not_the_old_place(rig: Rig) -> None:
    add_clip(rig.handles["reel_group"].reels[0], "second")
    env = mv(rig, [C1], reel2_selector(rig))
    assert revert(rig, env)["ok"] is True
    assert names(rig.handles["reel_group"].reels[0]) == ["second", "c1"]


def test_a_move_into_a_library_that_does_not_list_the_thing_is_taken_from_the_answer(
    rig: Rig,
) -> None:
    real = rig.flame.media_panel.move
    holder = rig.handles["reel2"]
    rig.flame.media_panel.move = lambda source, destination: real(source, holder)
    env = mv(rig, [C1], LIB)
    assert env["ok"] is True, env
    assert [m["to"]["name"] for m in env["result"]["moved"]] == ["c1"]
    assert any("does not list" in note for note in env["degraded"])
    assert env["journal"]["revert"] is not None


def test_revert_of_a_move_conflicts_when_a_moved_thing_left_the_destination(rig: Rig) -> None:
    [second] = desk_clips(rig, "d1")
    env = mv(rig, [C1, second], reel2_selector(rig))
    gone = rig.handles["reel2"].children[0]
    rig.flame.delete(gone, confirm=False)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert "not in the destination" in " ".join(out["result"]["conflicts"])
    assert names(rig.handles["reel2"]) == ["d1"]  # the other one was NOT moved back
    assert states(rig)[env["journal"]["group"]] == "landed"


def test_revert_of_a_move_conflicts_when_the_old_parent_is_gone_and_moves_nothing(
    rig: Rig,
) -> None:
    [second] = desk_clips(rig, "d1")
    env = mv(rig, [C1, second], reel2_selector(rig))
    rig.flame.delete(rig.handles["reel_group"].reels[0], confirm=False)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert "Reel 1" in " ".join(out["result"]["conflicts"]) and "gone" in out["error"]
    assert names(rig.handles["reel2"]) == ["c1", "d1"]


def test_revert_of_a_move_whose_destination_is_gone_conflicts(rig: Rig) -> None:
    env = mv(rig, [C1], reel2_selector(rig))
    group = rig.handles["reel_group"]
    group.reels = [r for r in group.reels if r is not rig.handles["reel2"]]
    group.sync_children()
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and "destination" in " ".join(out["result"]["conflicts"])


def test_a_move_that_reverts_into_nothing_is_a_silent_noop_and_can_be_retried(rig: Rig) -> None:
    env = mv(rig, [C1], reel2_selector(rig))
    real = rig.flame.media_panel.move
    rig.flame.media_panel.move = lambda source, destination: []
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "SILENT_NOOP"
    assert states(rig)[env["journal"]["group"]] == "landed"
    rig.flame.media_panel.move = real
    assert revert(rig, env)["ok"] is True


def test_a_lost_answer_of_a_move_is_decided_by_check_and_revert_then_moves_back(
    rig: Rig,
) -> None:
    rig.exec.fault(MOVE, "TIMEOUT", when="after", skip=1)
    [second, third] = desk_clips(rig, "d1", "d2")
    env = mv(rig, [C1, second], reel2_selector(rig))
    assert env["errorCode"] == "TIMEOUT" and "journal.check" in env["hint"]
    group = env["journal"]["group"]
    assert types_in(rig) == ["intent", "unresolved"]
    assert mv(rig, [third], reel2_selector(rig))["errorCode"] == "POLICY_DENIED"
    checked = rig.do("journal.check", {"group": group})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "landed", row
    moves = row["created"]["moves"]
    assert [m["from"]["name"] for m in moves] == ["c1", "d1"]
    assert [m["to"]["name"] for m in moves] == ["c1", "d1"]
    assert all(m["to"]["uid"] for m in moves)
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True, out
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"] and names(rig.handles["reel2"]) == []


def test_check_of_a_move_that_never_ran_is_not_landed_and_a_half_done_one_is_diverged(
    rig: Rig,
) -> None:
    rig.exec.fault(MOVE, "BRIDGE_LOST", when="before", skip=1)
    env = mv(rig, [C1], reel2_selector(rig))
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "not_landed"
    assert mv(rig, [C1], reel2_selector(rig))["ok"] is True  # the door is open again

    real = rig.flame.media_panel.move
    rig.flame.media_panel.move = lambda source, destination: real(source[0], destination)
    rig.exec.fault(MOVE, "TIMEOUT", when="after", skip=1)
    half = mv(rig, desk_clips(rig, "d1", "d2"), LIB)
    assert half["errorCode"] == "TIMEOUT"
    checked = rig.do("journal.check", {"group": half["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "diverged"


def test_check_of_a_move_that_never_ran_is_diverged_when_look_alikes_appeared(rig: Rig) -> None:
    rig.exec.fault(MOVE, "BRIDGE_LOST", when="before", skip=1)
    env = mv(rig, [C1], reel2_selector(rig))
    add_clip(rig.handles["reel2"], "c1")  # someone else made a clip of that name
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "diverged"
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]  # the source never left


def test_a_lost_answer_of_the_revert_of_a_move_is_decided_by_check(rig: Rig) -> None:
    env = mv(rig, [C1], reel2_selector(rig))
    rig.exec.fault(MOVE, "BRIDGE_LOST", when="after")
    out = revert(rig, env)
    assert out["errorCode"] == "BRIDGE_LOST"
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]  # the move did run
    checked = rig.do("journal.check", {"group": out["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "landed"
    assert states(rig)[env["journal"]["group"]] == "reverted"
