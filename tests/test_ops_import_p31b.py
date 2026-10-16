"""Tests for `mediahub.import` and its journal records.

Also `journal.revert` and `journal.check`, and the checks of the fake's import / move / duplicate (a
fake more tolerant than Flame would hide real failures). See media_p31b_helpers.py for the rig."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import (
    PyClip,
    PyFolder,
    PySegment,
    PyTime,
    adopt,
)
from flame_mcp import desktop_run
from journal_test_helpers import Rig
from media_p31b_helpers import (
    CLIP1,
    IMPORT,
    LIB,
    LIB_WT,
    LIBREEL,
    REEL1,
    add_clip,
    add_seq,
    batch_reel,
    drop_recorded_fingerprints,
    imp,
    make_media_rig,
    media,
    names,
    op_write_calls,
    revert,
    states,
    types_in,
    write_codes,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_media_rig(monkeypatch, tmp_path)


# The fake itself is checked: a fake more tolerant than Flame would hide real failures.


def test_the_fake_import_answers_empty_for_a_missing_path_and_a_zero_byte_file(
    rig: Rig, tmp_path: Path
) -> None:
    empty = tmp_path / "empty.mov"
    empty.write_bytes(b"")
    reel = rig.handles["reel2"]
    assert rig.flame.import_clips(str(tmp_path / "nope.mov"), reel) == []
    assert rig.flame.import_clips(str(empty), reel) == []
    assert rig.flame.import_clips(str(tmp_path / "clip.[00000001-00000024].tif"), reel) == []
    assert reel.children == []
    [one] = media(tmp_path, "a.mov")
    made = rig.flame.import_clips(one, reel)
    assert [type(m).__name__ for m in made] == ["PyClip"] and names(reel) == ["a"]


def test_the_fake_import_of_a_directory_and_a_pattern(rig: Rig, tmp_path: Path) -> None:
    folder = tmp_path / "dir"
    folder.mkdir()
    media(folder, "x.mov", "y.mov")
    reel = rig.handles["reel2"]
    assert len(rig.flame.import_clips(str(folder), reel)) == 2
    seq_dir = tmp_path / "seq"
    seq_dir.mkdir()
    media(seq_dir, "clip.00000001.tif", "clip.00000002.tif")
    got = rig.flame.import_clips(str(seq_dir / "clip.[00000001-00000002].tif"), reel)
    assert [str(c.name.get_value()) for c in got] == ["clip"]


def test_the_fake_move_gives_a_new_uid_and_wiretap_id_in_the_order_given(rig: Rig) -> None:
    clip1, clip2 = rig.handles["clip1"], rig.handles["clip2"]
    old = [(c.uid.get_value(), c.get_wiretap_node_id()) for c in (clip2, clip1)]
    dest = rig.handles["reel2"]
    moved = rig.flame.media_panel.move([clip2, clip1], dest)
    assert moved == [clip2, clip1]
    for obj, (uid, wiretap) in zip(moved, old, strict=True):
        assert obj.uid.get_value() != uid and obj.get_wiretap_node_id() != wiretap
        assert rig.flame.find_by_uid(uid) is None
        assert rig.flame.find_by_wiretap_node_id(wiretap) is None
    assert names(dest) == ["clip2", "clip1"] and "clip1" not in names(rig.handles["libreel"])


def test_the_fake_duplicate_of_a_sequence_answers_an_unreadable_wrapper(rig: Rig) -> None:
    seq = add_seq(rig.handles["reel2"])
    made = rig.flame.duplicate(seq)
    assert type(made).__name__ == "PyClip" and made.name is None and made.uid is None
    assert names(rig.handles["reel2"]) == ["S1", "S1"]
    clip = rig.flame.duplicate(rig.handles["clip1"])
    assert clip.name.get_value() == "clip1" and clip.uid.get_value() is not None
    reel = rig.flame.duplicate(rig.handles["reel2"])
    assert reel.name.get_value() == "Reel 2 copy" and names(reel) == ["S1", "S1"]
    assert [r.name.get_value() for r in rig.handles["reel_group"].reels][-1] == "Reel 2 copy"


def test_import_one_path_makes_a_clip_and_says_what_it_made(rig: Rig, tmp_path: Path) -> None:
    paths = media(tmp_path, "a.mov")
    env = imp(rig, paths, REEL1)
    assert env["ok"] is True, env
    result = env["result"]
    assert (result["requested"], result["returned"]) == (1, 1)
    [made] = result["imported"]
    assert made["type"] == "PyClip" and made["name"] == "a" and made["uid"]
    assert made["selector"] == {"kind": "wiretap", "id": made["wiretapId"]}
    assert result["destinationEntriesBefore"] == ["c1"]
    assert result["destinationEntriesAfter"] == ["c1", "a"]
    assert names(rig.handles["reel_group"].reels[0]) == ["c1", "a"]
    assert env["affected"] == 1 and env["undoSteps"] == 0 and "notes" not in result
    assert env["execution"]["effect"] == "create"
    assert env["journal"]["revert"] == {
        "op": "journal.revert",
        "args": {"group": env["journal"]["group"]},
    }
    assert rig.flame.import_calls == [paths]


def test_import_a_list_of_paths_makes_the_clips_in_the_order_given(
    rig: Rig, tmp_path: Path
) -> None:
    paths = media(tmp_path, "c.mov", "a.mov", "b.mov")
    env = imp(rig, paths, REEL1)
    assert env["ok"] is True, env
    assert [m["name"] for m in env["result"]["imported"]] == ["c", "a", "b"]
    assert (env["result"]["requested"], env["result"]["returned"]) == (3, 3)
    assert env["affected"] == 3 and "notes" not in env["result"]


def test_import_a_directory_makes_more_clips_than_paths_and_says_so_in_notes(
    rig: Rig, tmp_path: Path
) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    media(folder, "x.mov", "y.mov")
    env = imp(rig, [str(folder)], REEL1)
    assert env["ok"] is True, env
    assert [m["name"] for m in env["result"]["imported"]] == ["x", "y"]
    assert (env["result"]["requested"], env["result"]["returned"]) == (1, 2)
    [note] = env["result"]["notes"]
    assert "1 path(s)" in note and "2 clip(s)" in note


def test_import_into_a_library_reel_a_folder_and_a_library(rig: Rig, tmp_path: Path) -> None:
    folder = PyFolder("F", "uid-f", f"{LIB_WT}/F")
    rig.handles["lib"]._children.append(folder)
    adopt(rig.handles["lib"], [folder])
    (a, b, c) = media(tmp_path, "a.mov", "b.mov", "c.mov")
    in_reel = imp(rig, [a], LIBREEL)
    in_folder = imp(rig, [b], {"kind": "wiretap", "id": f"{LIB_WT}/F"})
    in_lib = imp(rig, [c], LIB)
    for env in (in_reel, in_folder, in_lib):
        assert env["ok"] is True, env
        assert env["result"]["imported"][0]["selector"]["kind"] == "wiretap"
    assert "a" in names(rig.handles["libreel"]) and names(folder) == ["b"]
    assert "c" in names(rig.handles["lib"])
    assert in_folder["result"]["destination"]["type"] == "PyFolder"


def test_a_destination_given_by_path_or_selection_is_re_addressed_by_wiretap(
    rig: Rig, tmp_path: Path
) -> None:
    (a, b) = media(tmp_path, "a.mov", "b.mov")
    by_path = imp(rig, [a], {"kind": "path", "root": "desktop", "path": ["RG", "Reel 1"]})
    rig.flame.media_panel.selected_entries = [rig.handles["reel_group"].reels[0]]
    by_selection = imp(rig, [b], {"kind": "selection"})
    assert by_path["ok"] is True and by_selection["ok"] is True
    intents = [r for r in rig.records() if r["type"] == "intent"]
    assert [i["target"] for i in intents] == [REEL1, REEL1]
    assert intents[0]["args"]["destination"]["kind"] == "path"
    for code in write_codes(rig, IMPORT):
        assert "_resolve_wiretap('/wt/RG/Reel 1')" in code
        assert "_resolve_path" not in code and "_resolve_selection" not in code


def test_a_missing_path_is_refused_before_flame_with_the_paths_listed(
    rig: Rig, tmp_path: Path
) -> None:
    [good] = media(tmp_path, "a.mov")
    gone = str(tmp_path / "gone.mov")
    env = imp(rig, [good, gone], REEL1)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert gone in env["error"] and good not in env["error"]
    assert env["result"]["missing"] == [gone] and env["result"]["ran"] is False
    assert rig.exec.calls == [] and rig.records() == []


def test_a_zero_byte_file_is_refused_before_flame(rig: Rig, tmp_path: Path) -> None:
    [empty] = media(tmp_path, "empty.mov", size=0)
    [good] = media(tmp_path, "a.mov")
    env = imp(rig, [good, empty], REEL1)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["empty"] == [empty] and "0 bytes" in env["error"]
    assert rig.exec.calls == [] and rig.records() == []


def test_missing_and_empty_are_both_listed(rig: Rig, tmp_path: Path) -> None:
    [empty] = media(tmp_path, "empty.mov", size=0)
    gone = str(tmp_path / "gone.mov")
    env = imp(rig, [empty, gone], REEL1)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["missing"] == [gone] and env["result"]["empty"] == [empty]


@pytest.mark.parametrize("path", ["relative/a.mov", "a.mov", "~/a.mov", "./a.mov"])
def test_a_path_that_is_not_absolute_is_invalid_args(rig: Rig, path: str) -> None:
    env = imp(rig, [path], REEL1)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "absolute" in env["error"] and path in env["error"]
    assert rig.exec.calls == [] and rig.records() == []


@pytest.mark.parametrize("count", [0, 51])
def test_import_takes_one_to_fifty_paths(rig: Rig, count: int) -> None:
    env = imp(rig, ["/x/a.mov"] * count, REEL1)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "1 to 50" in env["error"] and rig.exec.calls == []


def test_a_sequence_pattern_is_not_checked_on_disk_and_that_matches_nothing_is_a_silent_noop(
    rig: Rig, tmp_path: Path
) -> None:
    pattern = str(tmp_path / "ghost.[00000001-00000024].tif")
    env = imp(rig, [pattern], REEL1)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert rig.flame.import_calls == [[pattern]]
    assert env["result"]["requested"] == 1 and env["result"]["returned"] == 0
    assert env["journal"]["revert"] is None
    _intent, outcome = rig.records()
    assert outcome["ok"] is False and outcome["errorCode"] == "SILENT_NOOP"
    assert outcome["created"] is None
    assert outcome["readBack"] == {"requested": 1, "returned": 0, "imported": 0}
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]


def test_a_sequence_pattern_that_matches_files_is_one_clip(rig: Rig, tmp_path: Path) -> None:
    media(tmp_path, "clip.00000001.tif", "clip.00000002.tif")
    env = imp(rig, [str(tmp_path / "clip.[00000001-00000002].tif")], REEL1)
    assert env["ok"] is True, env
    assert [m["name"] for m in env["result"]["imported"]] == ["clip"]


def test_a_directory_with_nothing_importable_is_a_silent_noop(rig: Rig, tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    env = imp(rig, [str(empty_dir)], REEL1)
    assert env["errorCode"] == "SILENT_NOOP" and "did not grow" in env["error"]


def test_import_into_a_batch_group_reel_is_refused_with_a_pointer_to_add_clips(
    rig: Rig, tmp_path: Path
) -> None:
    [path] = media(tmp_path, "a.mov")
    reel = batch_reel(rig)
    env = imp(rig, [path], wt(reel))
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "batch.add_clips" in env["error"] and "batch.add_clips" in env["hint"]
    assert rig.flame.import_calls == [] and rig.records() == []
    assert op_write_calls(rig, IMPORT) == 0


def test_import_refuses_a_destination_that_is_not_a_container(rig: Rig, tmp_path: Path) -> None:
    [path] = media(tmp_path, "a.mov")
    env = imp(rig, [path], CLIP1)
    assert env["errorCode"] == "SELECTOR_TYPE" and "destination is a PyClip" in env["error"]
    assert rig.records() == [] and rig.flame.import_calls == []


def test_import_journals_an_intent_before_the_write_and_an_outcome_with_what_it_made(
    rig: Rig, tmp_path: Path
) -> None:
    [path] = media(tmp_path, "a.mov")
    env = imp(rig, [path], REEL1)
    intent, outcome = rig.records()
    assert intent["kind"] == "desktop_import" and intent["op"] == IMPORT
    assert intent["target"] == REEL1 and intent["existing"] == [
        rig.handles["reel"].children[0].uid.get_value()
    ]
    assert intent["args"]["paths"] == [path]
    assert outcome["ok"] is True
    assert [m["uid"] for m in outcome["created"]["imports"]] == [
        env["result"]["imported"][0]["uid"]
    ]
    assert outcome["created"]["destination"]["name"] == "Reel 1"
    assert outcome["readBack"] == {"requested": 1, "returned": 1, "imported": 1}
    phases = [c for c in rig.exec.calls if c.label == IMPORT]
    assert len(phases) == 2 and phases[0].journal_types == []
    assert phases[1].journal_types == ["intent"]
    assert states(rig)[env["journal"]["group"]] == "landed"


def test_import_uses_the_thirty_second_write_timeout_not_the_ten_second_main_one(
    rig: Rig, tmp_path: Path
) -> None:
    [path] = media(tmp_path, "a.mov")
    imp(rig, [path], REEL1)
    read_call, write_call = [c for c in rig.exec.calls if c.label == IMPORT]
    assert write_call.timeout_s == desktop_run.WRITE_TIMEOUT_S == 30.0
    assert read_call.timeout_s is None or read_call.timeout_s < write_call.timeout_s


def test_import_conflicts_when_the_destination_changed_between_the_read_and_the_write(
    rig: Rig, tmp_path: Path
) -> None:
    [path] = media(tmp_path, "a.mov")

    def sneak(label: str | None) -> None:
        if label == IMPORT and len(rig.exec.calls) == 2:
            add_clip(rig.handles["reel_group"].reels[0], "sneaked")

    rig.exec.before_run = sneak
    env = imp(rig, [path], REEL1)
    assert env["ok"] is False and env["errorCode"] == "CONFLICT"
    assert "changed between" in env["error"] and rig.flame.import_calls == []
    assert env["journal"]["revert"] is None


def test_an_import_whose_answer_is_not_in_the_destination_is_a_silent_noop(
    rig: Rig, tmp_path: Path
) -> None:
    [path] = media(tmp_path, "a.mov")
    stray = PyClip("stray", wiretap_id="/wt/stray", uid="stray-uid")
    rig.flame.import_clips = lambda p, d: [stray]
    env = imp(rig, [path], REEL1)
    assert env["errorCode"] == "SILENT_NOOP" and env["result"]["returned"] == 1
    assert env["journal"]["revert"] is None and rig.records()[-1]["created"] is None


def test_a_library_that_does_not_list_the_new_clips_is_taken_from_the_answer_with_a_warning(
    rig: Rig, tmp_path: Path
) -> None:
    [path] = media(tmp_path, "a.mov")
    holder = rig.handles["libreel"]
    real = rig.flame.import_clips

    def elsewhere(p: Any, destination: Any) -> list[Any]:
        return list(real(p, holder))  # Flame placed the clip somewhere the library does not list

    rig.flame.import_clips = elsewhere
    library = imp(rig, [path], LIB)
    assert library["ok"] is True, library
    assert [m["name"] for m in library["result"]["imported"]] == ["a"]
    assert any("does not list" in note for note in library["degraded"])
    assert library["journal"]["revert"] is not None
    assert rig.do("journal.revert", {"group": library["journal"]["group"]})["ok"] is True
    assert "a" not in names(holder)
    reel = imp(rig, [path], REEL1)  # a REEL that does not list what Flame answered is an error
    assert reel["ok"] is False and reel["errorCode"] == "SILENT_NOOP"
    assert reel["journal"]["revert"] is None


def test_revert_of_an_import_deletes_what_it_made_and_nothing_else(
    rig: Rig, tmp_path: Path
) -> None:
    paths = media(tmp_path, "a.mov", "b.mov")
    env = imp(rig, paths, REEL1)
    reel = rig.handles["reel_group"].reels[0]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert [d["name"] for d in out["result"]["deleted"]] == ["a", "b"]
    assert out["result"]["skipped"] == [] and out["affected"] == 2
    assert names(reel) == ["c1"]
    assert states(rig)[env["journal"]["group"]] == "reverted"
    again = revert(rig, env)
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"


def test_revert_of_an_import_skips_a_clip_that_is_gone_already(rig: Rig, tmp_path: Path) -> None:
    paths = media(tmp_path, "a.mov", "b.mov")
    env = imp(rig, paths, REEL1)
    reel = rig.handles["reel_group"].reels[0]
    rig.flame.delete(reel.children[1], confirm=False)
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert [s["name"] for s in out["result"]["skipped"]] == ["a"]
    assert [d["name"] for d in out["result"]["deleted"]] == ["b"]
    assert names(reel) == ["c1"]


def edit_content(clip: Any) -> None:
    clip.versions[0].tracks[0]._segments.append(PySegment(name="added by hand"))


@pytest.mark.parametrize(
    "edit",
    [
        lambda clip: clip.name.set_value("renamed by hand"),
        lambda clip: setattr(clip, "duration", PyTime(7)),
        edit_content,
    ],
    ids=["name", "duration", "content"],
)
def test_revert_of_an_import_is_a_conflict_and_deletes_nothing_when_one_clip_was_edited(
    rig: Rig, tmp_path: Path, edit: Any
) -> None:
    env = imp(rig, media(tmp_path, "a.mov", "b.mov"), REEL1)
    reel = rig.handles["reel_group"].reels[0]
    edit(reel.children[2])
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "nothing was deleted" in out["error"] and "'b'" in out["error"]
    assert out["result"]["deleted"] == []
    assert len(reel.children) == 3
    assert states(rig)[env["journal"]["group"]] == "landed"


def test_revert_of_an_import_deletes_after_an_edit_is_undone_by_hand(
    rig: Rig, tmp_path: Path
) -> None:
    env = imp(rig, media(tmp_path, "a.mov"), REEL1)
    clip = rig.handles["reel_group"].reels[0].children[1]
    clip.name.set_value("renamed")
    assert revert(rig, env)["errorCode"] == "CONFLICT"
    clip.name.set_value("a")
    assert revert(rig, env)["ok"] is True


def test_revert_of_an_import_recorded_without_a_fingerprint_is_a_conflict(
    rig: Rig, tmp_path: Path
) -> None:
    env = imp(rig, media(tmp_path, "a.mov"), REEL1)
    drop_recorded_fingerprints(rig)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "no fingerprint" in out["error"]
    assert names(rig.handles["reel_group"].reels[0]) == ["c1", "a"]


def test_the_outcome_of_an_import_records_name_duration_and_fingerprint_of_each_clip(
    rig: Rig, tmp_path: Path
) -> None:
    imp(rig, media(tmp_path, "a.mov"), REEL1)
    [made] = rig.records()[-1]["created"]["imports"]
    assert made["state"]["name"] == "a" and made["state"]["duration"] == 100
    assert len(made["state"]["fingerprint"]) == 64


def test_a_lost_answer_of_an_import_is_decided_by_check_and_revert_then_deletes_it(
    rig: Rig, tmp_path: Path
) -> None:
    paths = media(tmp_path, "a.mov", "b.mov")
    rig.exec.fault(IMPORT, "TIMEOUT", when="after", skip=1)
    env = imp(rig, paths, REEL1)
    assert env["errorCode"] == "TIMEOUT" and "journal.check" in env["hint"]
    group = env["journal"]["group"]
    assert types_in(rig) == ["intent", "unresolved"]
    denied = imp(rig, paths, REEL1)
    assert denied["errorCode"] == "POLICY_DENIED"
    checked = rig.do("journal.check", {"group": group})
    [row] = checked["result"]["checked"]
    assert row["verdict"] == "landed"
    assert [c["name"] for c in row["created"]["imports"]] == ["a", "b"]
    assert checked["result"]["writesBlocked"] is False
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True and len(out["result"]["deleted"]) == 2
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]


def test_check_of_an_import_that_never_reached_flame_is_not_landed(
    rig: Rig, tmp_path: Path
) -> None:
    paths = media(tmp_path, "a.mov")
    rig.exec.fault(IMPORT, "BRIDGE_LOST", when="before", skip=1)
    env = imp(rig, paths, REEL1)
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "not_landed"
    assert imp(rig, paths, REEL1)["ok"] is True  # the door is open again


def test_a_lost_answer_of_the_revert_of_an_import_is_decided_by_check(
    rig: Rig, tmp_path: Path
) -> None:
    env = imp(rig, media(tmp_path, "a.mov"), REEL1)
    rig.exec.fault(IMPORT, "BRIDGE_LOST", when="after")
    out = revert(rig, env)
    assert out["errorCode"] == "BRIDGE_LOST"
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]
    checked = rig.do("journal.check", {"group": out["journal"]["group"]})
    assert checked["result"]["checked"][0]["verdict"] == "landed"
    assert states(rig)[env["journal"]["group"]] == "reverted"
