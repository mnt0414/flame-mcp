"""Tests for `library.create` and copying a Batch group into a library and back.

Covers `library.create`, `desktop.copy` of a Batch group into a library / a library folder and
back onto the Desktop, and the `{kind: 'desktop'}` selector.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py). The fake follows what was measured on
Flame 2025.2.4 (see tests/fake_library_batch.py): a test here describes the fake; whether the real
Flame does the same is what scripts/fix2_library_batch_e2e.py measures."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from fake_library_batch import LibraryBatch
from fake_media_tree import PyBatch
from flame_mcp import desktop_run, execution
from flame_mcp.desktop_run import CURRENT_DELETED_NOTE, RESTORE_NOTE
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from journal_test_helpers import Rig, make_rig

LIB_WT = "/projects/p/ws/P1O"
LIB = {"kind": "wiretap", "id": LIB_WT}
DESKTOP = {"kind": "desktop"}
SRC = {"kind": "batch", "name": "SRCBG"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_rig(monkeypatch, tmp_path)
    for key, wiretap in (
        ("lib", LIB_WT),
        ("clip1", "/projects/p/ws/P1O/R1/clip1"),
        ("clip2", "/projects/p/ws/P1O/R1/clip2"),
        ("clip3", "/projects/p/ws/P1O/R1/clip3"),
    ):
        made.handles[key] = made.flame.find_by_wiretap_node_id(wiretap)
    return made


def desktop(rig: Rig) -> Any:
    return rig.flame.projects.current_project.current_workspace.desktop


def workspace(rig: Rig) -> Any:
    return rig.flame.projects.current_project.current_workspace


def make_group(rig: Rig, name: str = "SRCBG", *, current: bool = False) -> PyBatch:
    """A Batch group with Blur -> Comp wired and three clips in its first schematic reel, made
    the way a user does (so Flame makes it current; `current=False` gives that back to BG2)."""
    group: PyBatch = desktop(rig).create_batch_group(name, nb_reels=2, nb_shelf_reels=1)
    blur, comp = group.create_node("Blur"), group.create_node("Comp")
    group.connect_nodes(blur, "Result", comp, "Default")
    for key in ("clip1", "clip2", "clip3"):
        rig.flame.media_panel.copy(rig.handles[key], group.reels[0])
    if not current:
        desktop(rig).current_batch_group.set_value(rig.handles["bg2"])
    return group


def library(rig: Rig, name: str) -> Any:
    return workspace(rig).create_library(name)


def group_by_uid(rig: Rig, uid: str) -> PyBatch:
    found: PyBatch = next(g for g in desktop(rig).batch_groups if g.uid.get_value() == uid)
    return found


def lib_selector(lib: Any) -> dict[str, Any]:
    return {"kind": "wiretap", "id": lib.get_wiretap_node_id()}


def current_name(rig: Rig) -> Any:
    cur = desktop(rig).current_batch_group.get_value()
    return None if cur is None else str(cur.name.get_value())


def lib_names(rig: Rig) -> list[str]:
    return [str(lib.name.get_value()) for lib in workspace(rig).libraries]


def batches_in(container: Any) -> list[Any]:
    return [c for c in container.children if type(c).__name__ == "PyBatch"]


def revert(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    return rig.do("journal.revert", {"group": env["journal"]["group"]})


def copy(rig: Rig, sources: list[Any], destination: Any) -> dict[str, Any]:
    return rig.do("desktop.copy", {"sources": sources, "destination": destination})


def create_library(rig: Rig, name: str, **extra: Any) -> dict[str, Any]:
    return rig.do("library.create", {"name": name, **extra})


def write_calls(rig: Rig) -> int:
    return sum(
        1
        for c in rig.exec.calls
        if "_result = _dc_create(" in c.code or "_result = _dc_copy(" in c.code
    )


def states(rig: Rig) -> dict[str, str]:
    return {i["group"]: i["state"] for i in rig.do("journal.list", {})["result"]["items"]}


def test_the_fake_renames_a_taken_library_name_like_flame(rig: Rig) -> None:
    first = library(rig, "L")
    second = library(rig, "L")
    assert first.name.get_value() == "L" and second.name.get_value() == "L 1"
    assert first.uid.get_value() != second.uid.get_value()


def test_the_fake_reads_a_batch_group_in_a_library_without_its_nodes(rig: Rig) -> None:
    group = make_group(rig)
    lib = library(rig, "L")
    returned = rig.flame.media_panel.copy(group, lib)
    held = returned[0]
    assert isinstance(held, LibraryBatch) and type(held).__name__ == "PyBatch"
    assert held.nodes == [] and len(group.nodes) == 5
    assert [c.name.get_value() for c in held.reels[0].clips] == ["clip1", "clip2", "clip3"]
    with pytest.raises(RuntimeError):
        held.duration  # noqa: B018
    with pytest.raises(TypeError):
        held.get_wiretap_node_id()
    assert held.uid.get_value() != group.uid.get_value() and batches_in(lib) == [held]


def test_the_fake_puts_the_clips_not_a_group_into_a_library_reel(rig: Rig) -> None:
    group = make_group(rig)
    lib = library(rig, "L")
    reel = lib.create_reel("LR")
    returned = rig.flame.media_panel.copy(group, reel)
    assert [type(r).__name__ for r in returned] == ["PyClip"] * 3 and batches_in(lib) == []


def test_the_fake_does_not_copy_what_was_not_measured(rig: Rig) -> None:
    group = make_group(rig)
    with pytest.raises(RuntimeError, match="not measured"):
        rig.flame.media_panel.copy(group, desktop(rig))
    with pytest.raises(RuntimeError, match="not measured"):
        rig.flame.media_panel.copy([group, rig.handles["bg1"]], library(rig, "L"))


def test_library_create_makes_an_empty_library_and_journals_it(rig: Rig) -> None:
    env = create_library(rig, "BACKUPS")
    assert env["ok"] is True, env
    result = env["result"]
    assert result["name"] == "BACKUPS" and result["renamed"] is False
    assert result["selector"]["kind"] == "wiretap"
    assert "BACKUPS" in lib_names(rig)
    assert env["execution"]["effect"] == "create" and env["undoSteps"] == 0
    intent, outcome = rig.records()
    assert intent["kind"] == "desktop_create" and intent["op"] == "library.create"
    assert intent["createKind"] == "library" and outcome["ok"] is True
    assert outcome["created"]["uid"] == result["uid"]
    assert env["journal"]["revert"] == {
        "op": "journal.revert",
        "args": {"group": env["journal"]["group"]},
    }


def test_library_create_refuses_a_taken_name_and_writes_nothing(rig: Rig) -> None:
    before = lib_names(rig)
    env = create_library(rig, "P1O")
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "P1O" in env["result"]["existingNames"] and "allow_rename" in env["hint"]
    assert lib_names(rig) == before and rig.records() == [] and write_calls(rig) == 0


def test_library_create_allow_rename_reports_the_name_flame_gave(rig: Rig) -> None:
    env = create_library(rig, "P1O", allow_rename=True)
    assert env["ok"] is True, env
    assert env["result"]["name"] == "P1O 1" and env["result"]["renamed"] is True
    assert env["result"]["requestedName"] == "P1O"
    assert lib_names(rig).count("P1O 1") == 1


def test_library_create_revert_deletes_a_library_that_is_still_empty(rig: Rig) -> None:
    env = create_library(rig, "BACKUPS")
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert out["result"]["deleted"] is True and "BACKUPS" not in lib_names(rig)
    assert "P1O" in lib_names(rig)
    assert states(rig)[env["journal"]["group"]] == "reverted"
    assert revert(rig, env)["errorCode"] == "PRECONDITION_UNMET"


def test_library_create_revert_is_a_conflict_when_the_library_got_something(rig: Rig) -> None:
    env = create_library(rig, "BACKUPS")
    lib = next(lib for lib in workspace(rig).libraries if lib.name.get_value() == "BACKUPS")
    rig.flame.media_panel.copy(rig.handles["clip1"], lib)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "nothing was deleted" in out["error"] and "BACKUPS" in lib_names(rig)
    assert states(rig)[env["journal"]["group"]] == "landed"


def test_library_create_revert_is_a_conflict_when_only_a_batch_group_went_in(rig: Rig) -> None:
    env = create_library(rig, "BACKUPS")
    lib = next(lib for lib in workspace(rig).libraries if lib.name.get_value() == "BACKUPS")
    rig.flame.media_panel.copy(make_group(rig), lib)
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and "BACKUPS" in lib_names(rig)


def test_library_create_check_finds_a_library_whose_answer_was_lost(rig: Rig) -> None:
    rig.exec.fault("library.create", "TIMEOUT", when="after", skip=1)
    env = create_library(rig, "LOST")
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    group = env["journal"]["group"]
    checked = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    assert checked["verdict"] == "landed" and checked["created"]["name"] == "LOST"
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True and "LOST" not in lib_names(rig)


def test_library_create_is_not_a_plan_step_and_a_readonly_server_denies_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = rig.do("plan.run", {"steps": [{"op": "library.create", "args": {"name": "P"}}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "library.create cannot be a plan step" in env["result"]["issues"][0]["problems"][0]
    ro = make_rig(monkeypatch, tmp_path / "ro", policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    assert create_library(ro, "P")["errorCode"] == "POLICY_DENIED"


def test_the_descriptions_tell_what_a_caller_must_know_and_follow_the_rules() -> None:
    import re

    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    copy_op, create_op = registry.get("desktop.copy"), registry.get("library.create")
    assert copy_op is not None and create_op is not None
    for phrase in (
        "A reel destination, in a library or on the Desktop, is refused (INVALID_ARGS",
        "puts the group's clips into a reel instead of the group",
        "reads no nodes (nodes is an empty list)",
        "as a new Batch group",
        "the current Batch group does not change",
        "{kind: 'desktop'}",
        "still as created",
    ):
        assert phrase in copy_op.description, phrase
    for op in (copy_op, create_op):
        text = op.description
        # \u672a\u6e2c is the Japanese for "not measured".
        assert not re.search(r"plan/\d+|\bP\d-\d|not measured|\u672a\u6e2c", text)
        assert not re.search(r"\d+\s*(ms|seconds)\b", text)
        assert not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text)


def test_library_create_declaration() -> None:
    registry = Registry()
    from flame_mcp.ops import register_all

    register_all(registry)
    op = registry.get("library.create")
    assert op is not None
    assert (op.category, op.effect, op.thread, op.undo) == ("library", "create", "main", "value")
    assert op.undo_steps({}, 1) == 0 and op.wrap is not None
    for word in ("journal.revert", "plan.run", "allow_rename", "CONFLICT"):
        assert word in op.description, word


def test_copy_a_batch_group_into_a_library(rig: Rig) -> None:
    group = make_group(rig)
    lib = library(rig, "BK")
    before = (group.uid.get_value(), len(group.nodes), current_name(rig))
    env = copy(rig, [SRC], lib_selector(lib))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["requested"] == 1 and result["returned"] == 1
    row = result["copies"][0]
    assert row["type"] == "PyBatch" and row["name"] == "SRCBG" and row["uid"] != before[0]
    assert row["selector"] == {"kind": "path", "path": ["BK", "SRCBG"]}
    assert row["inDestination"] is True and result["sourcesRemain"] is True
    assert result["warnings"] == [] and env["degraded"] == []
    held = batches_in(lib)
    assert [b.uid.get_value() for b in held] == [row["uid"]]
    assert (group.uid.get_value(), len(group.nodes), current_name(rig)) == before
    assert result["destinationEntriesAfter"] == ["SRCBG"]
    assert row["state"]["inLibrary"] is True and "fingerprint" not in row["state"]
    assert [r["clips"] for r in row["state"]["reels"][:1]] == [["clip1", "clip2", "clip3"]]
    assert any("nodes" in n for n in result["notes"])


def test_copy_a_current_batch_group_into_a_library_folder(rig: Rig) -> None:
    group = make_group(rig, current=True)
    folder = library(rig, "BK").create_folder("F1")
    env = copy(rig, [SRC], {"kind": "wiretap", "id": folder.get_wiretap_node_id()})
    assert env["ok"] is True, env
    assert env["result"]["copies"][0]["selector"] == {"kind": "path", "path": ["BK", "F1", "SRCBG"]}
    assert len(batches_in(folder)) == 1 and current_name(rig) == "SRCBG"
    assert desktop(rig).current_batch_group.get_value() is group


def test_the_selector_of_a_library_copy_is_none_when_the_name_is_not_unique(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    assert copy(rig, [SRC], lib_selector(lib))["ok"] is True
    second = copy(rig, [SRC], lib_selector(lib))
    assert second["ok"] is True and second["result"]["copies"][0]["selector"] is None
    assert len(batches_in(lib)) == 2


def test_copy_a_batch_group_by_uid_selector(rig: Rig) -> None:
    group = make_group(rig)
    lib = library(rig, "BK")
    env = copy(rig, [{"kind": "batch", "uid": group.uid.get_value()}], lib_selector(lib))
    assert env["ok"] is True and len(batches_in(lib)) == 1


def test_copy_a_batch_group_into_a_library_reel_is_refused_and_writes_nothing(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    reel = lib.create_reel("LR")
    env = copy(rig, [SRC], {"kind": "wiretap", "id": reel.get_wiretap_node_id()})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert "reel" in env["error"] and "clips" in env["error"]
    assert reel.children == [] and rig.records() == [] and write_calls(rig) == 0


def test_copy_a_batch_group_into_a_desktop_reel_is_refused(rig: Rig) -> None:
    make_group(rig)
    reel = {"kind": "wiretap", "id": "/wt/RG/Reel 1"}
    env = copy(rig, [SRC], reel)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert (
        rig.handles["reel"].clips[0].name.get_value() == "c1"
        and len(rig.handles["reel"].clips) == 1
    )
    assert rig.records() == []


def test_copy_a_batch_group_into_a_batch_reel_is_refused(rig: Rig) -> None:
    group = make_group(rig)
    other = rig.handles["bg1"].create_reel("Schematic Reel 1")
    env = copy(rig, [SRC], {"kind": "wiretap", "id": other.get_wiretap_node_id()})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS" and group is not None
    assert rig.records() == []


def test_a_batch_group_cannot_be_copied_onto_the_desktop(rig: Rig) -> None:
    make_group(rig)
    env = copy(rig, [SRC], DESKTOP)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "library" in env["error"] and rig.exec.calls == []


def test_a_batch_group_cannot_be_mixed_with_clips_or_repeated(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    clip = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip1"}
    mixed = copy(rig, [SRC, clip], lib_selector(lib))
    assert mixed["ok"] is False and mixed["errorCode"] == "INVALID_ARGS"
    assert "one Batch group" in mixed["error"] and rig.exec.calls == []
    twice = copy(rig, [SRC, {"kind": "batch", "name": "BG1"}], lib_selector(lib))
    assert twice["errorCode"] == "INVALID_ARGS" and rig.exec.calls == []
    by_path = copy(rig, [{"kind": "path", "path": ["P1O", "R1", "clip1"]}], lib_selector(lib))
    assert by_path["ok"] is True
    group_by_clip = copy(rig, [SRC], lib_selector(lib))
    assert group_by_clip["ok"] is True and len(batches_in(lib)) == 1


def test_a_library_batch_group_cannot_be_copied_into_a_library(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    first = copy(rig, [SRC], lib_selector(lib))
    path = first["result"]["copies"][0]["selector"]
    other = library(rig, "BK2")
    env = copy(rig, [path], lib_selector(other))
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert "Desktop" in env["error"] and batches_in(other) == []


def test_copy_journals_a_batch_copy_and_conflicts_when_the_library_changed(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    ok = copy(rig, [SRC], lib_selector(lib))
    intent, outcome = rig.records()
    assert intent["kind"] == "desktop_copy" and intent["existing"] == []
    assert intent["sources"][0]["type"] == "PyBatch"
    assert outcome["created"]["copies"][0]["type"] == "PyBatch"
    assert ok["journal"]["revert"]["args"]["group"] == ok["journal"]["group"]

    def sneak(label: str | None) -> None:
        if label == "desktop.copy" and len(rig.exec.calls) == 4:  # the second call's write
            lib._children.append(LibraryBatch("x", "uid-x"))

    rig.exec.before_run = sneak
    refused = copy(rig, [SRC], lib_selector(lib))
    assert refused["errorCode"] == "CONFLICT" and "changed between" in refused["error"]
    assert len(batches_in(lib)) == 2  # the first copy and the one that was slipped in


def test_revert_of_a_batch_copy_in_a_library_deletes_it_when_unchanged(rig: Rig) -> None:
    group = make_group(rig)
    lib = library(rig, "BK")
    env = copy(rig, [SRC], lib_selector(lib))
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert [d["name"] for d in out["result"]["deleted"]] == ["SRCBG"] and out["affected"] == 1
    assert batches_in(lib) == [] and group in desktop(rig).batch_groups
    assert states(rig)[env["journal"]["group"]] == "reverted"


def test_revert_of_a_batch_copy_is_a_conflict_when_it_was_renamed(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    env = copy(rig, [SRC], lib_selector(lib))
    batches_in(lib)[0].name.set_value("renamed by hand")
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "name is" in out["error"] and len(batches_in(lib)) == 1
    assert states(rig)[env["journal"]["group"]] == "landed"


def test_revert_of_a_batch_copy_is_a_conflict_when_a_reel_changed(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    env = copy(rig, [SRC], lib_selector(lib))
    reel = batches_in(lib)[0].reels[1]
    reel._children.append(rig.handles["clip1"])
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and "reels is" in out["error"]
    assert len(batches_in(lib)) == 1


def test_revert_of_a_batch_copy_that_is_gone_is_skipped(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    env = copy(rig, [SRC], lib_selector(lib))
    rig.flame.delete(batches_in(lib)[0], confirm=False)
    out = revert(rig, env)
    assert out["ok"] is True and [s["name"] for s in out["result"]["skipped"]] == ["SRCBG"]


def test_check_finds_a_batch_copy_whose_answer_was_lost(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    rig.exec.fault("desktop.copy", "TIMEOUT", when="after", skip=1)
    env = copy(rig, [SRC], lib_selector(lib))
    assert env["errorCode"] == "TIMEOUT"
    group = env["journal"]["group"]
    checked = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    assert checked["verdict"] == "landed", checked
    assert checked["created"]["copies"][0]["type"] == "PyBatch"
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True and batches_in(lib) == []


def backed_up(rig: Rig, name: str = "SRCBG") -> tuple[PyBatch, Any, dict[str, Any]]:
    group = make_group(rig, name)
    lib = library(rig, "BK")
    env = copy(rig, [{"kind": "batch", "name": name}], lib_selector(lib))
    assert env["ok"] is True, env
    return group, lib, env["result"]["copies"][0]["selector"]


def node_rows(group: Any) -> list[tuple[Any, ...]]:
    return [
        (n.name.get_value(), n.type.get_value(), n.sockets["input"], n.sockets["output"])
        for n in group.nodes
    ]


def reel_rows(group: Any) -> list[tuple[str, list[str]]]:
    return [
        (r.name.get_value(), [c.name.get_value() for c in r.children])
        for r in [*group.reels, *group.shelf_reels]
    ]


def test_restore_a_library_batch_group_onto_the_desktop(rig: Rig) -> None:
    group, lib, path = backed_up(rig)
    held = batches_in(lib)[0]
    before = list(desktop(rig).batch_groups)
    env = copy(rig, [path], DESKTOP)
    assert env["ok"] is True, env
    result = env["result"]
    row = result["copies"][0]
    assert result["destination"]["type"] == "PyDesktop"
    assert row["type"] == "PyBatch" and row["name"] == "SRCBG"
    assert row["selector"] == {"kind": "batch", "uid": row["uid"]}
    restored = [g for g in desktop(rig).batch_groups if g not in before]
    assert [g.uid.get_value() for g in restored] == [row["uid"]]
    new = restored[0]
    assert node_rows(new) == node_rows(group) and reel_rows(new) == reel_rows(group)
    assert new.uid.get_value() not in (group.uid.get_value(), held.uid.get_value())
    assert current_name(rig) == "BG2" and result["current"]["name"] == "BG2"
    assert result["currentMoved"] is False and RESTORE_NOTE in result["notes"]
    assert row["state"]["inLibrary"] is False and row["state"]["fingerprint"]
    assert held in batches_in(lib) and result["sourcesRemain"] is True


def test_restore_by_selection_selector(rig: Rig) -> None:
    _group, lib, _path = backed_up(rig)
    rig.flame.media_panel.selected_entries = [batches_in(lib)[0]]
    env = copy(rig, [{"kind": "selection"}], DESKTOP)
    assert env["ok"] is True, env


def test_restore_refuses_a_clip_and_other_destinations(rig: Rig) -> None:
    _group, _lib, path = backed_up(rig)
    clip = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip1"}
    env = copy(rig, [clip], DESKTOP)
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE", env
    assert "PyDesktop" in env["error"]
    reel_group = {"kind": "wiretap", "id": "/wt/RG"}
    refused = copy(rig, [path], reel_group)
    assert refused["ok"] is False and refused["errorCode"] in ("INVALID_ARGS", "SELECTOR_TYPE")
    reel = {"kind": "wiretap", "id": "/wt/RG/Reel 1"}
    onto_reel = copy(rig, [path], reel)
    assert onto_reel["ok"] is False and onto_reel["errorCode"] == "INVALID_ARGS"
    assert len(desktop(rig).batch_groups) == 3  # BG1, BG2, SRCBG: nothing was added


def test_restore_refuses_a_desktop_batch_group_as_source(rig: Rig) -> None:
    make_group(rig)
    env = copy(rig, [{"kind": "path", "root": "desktop", "path": ["SRCBG"]}], DESKTOP)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert "library" in env["error"]


def test_revert_of_a_restore_deletes_the_group_when_nothing_changed(rig: Rig) -> None:
    group, _lib, path = backed_up(rig)
    env = copy(rig, [path], DESKTOP)
    new_uid = env["result"]["copies"][0]["uid"]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert [d["uid"] for d in out["result"]["deleted"]] == [new_uid]
    assert [g.uid.get_value() for g in desktop(rig).batch_groups].count(new_uid) == 0
    assert group in desktop(rig).batch_groups and current_name(rig) == "BG2"
    assert "notes" not in out["result"]
    assert states(rig)[env["journal"]["group"]] == "reverted"


def test_revert_of_a_restore_is_a_conflict_when_a_node_was_added(rig: Rig) -> None:
    _group, _lib, path = backed_up(rig)
    env = copy(rig, [path], DESKTOP)
    new = group_by_uid(rig, env["result"]["copies"][0]["uid"])
    new.create_node("Exposure")
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "nothing was deleted" in out["error"] and new in desktop(rig).batch_groups


def test_revert_of_a_restore_is_a_conflict_when_a_connection_changed(rig: Rig) -> None:
    _group, _lib, path = backed_up(rig)
    env = copy(rig, [path], DESKTOP)
    new = group_by_uid(rig, env["result"]["copies"][0]["uid"])
    blur, comp = new.nodes[0], new.nodes[1]
    comp._sockets["input"]["Input_0"].remove(str(blur.name.get_value()))
    blur._sockets["output"]["Result"].remove(str(comp.name.get_value()))
    out = revert(rig, env)
    assert out["errorCode"] == "CONFLICT" and new in desktop(rig).batch_groups


def test_revert_of_a_restore_that_became_current_says_current_moved(rig: Rig) -> None:
    _group, _lib, path = backed_up(rig)
    env = copy(rig, [path], DESKTOP)
    new = group_by_uid(rig, env["result"]["copies"][0]["uid"])
    desktop(rig).current_batch_group.set_value(new)
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert out["result"]["currentChanged"] is True
    assert CURRENT_DELETED_NOTE in out["result"]["notes"]
    assert out["result"]["current"]["uid"] != env["result"]["copies"][0]["uid"]


def test_check_finds_a_restore_whose_answer_was_lost(rig: Rig) -> None:
    _group, _lib, path = backed_up(rig)
    rig.exec.fault("desktop.copy", "TIMEOUT", when="after", skip=1)
    env = copy(rig, [path], DESKTOP)
    assert env["errorCode"] == "TIMEOUT", env
    group = env["journal"]["group"]
    checked = rig.do("journal.check", {"group": group})["result"]["checked"][0]
    assert checked["verdict"] == "landed", checked
    out = rig.do("journal.revert", {"group": group})
    assert out["ok"] is True and len(desktop(rig).batch_groups) == 3


def test_list_children_of_the_desktop_lists_reel_groups_and_batch_groups(rig: Rig) -> None:
    env = rig.do("desktop.list_children", {"target": DESKTOP})
    assert env["ok"] is True, env
    items = env["result"]["items"]
    assert [(i["type"], i["name"]) for i in items] == [
        ("PyReelGroup", "RG"),
        ("PyBatch", "BG1"),
        ("PyBatch", "BG2"),
    ]
    only = rig.do("desktop.list_children", {"target": DESKTOP, "types": ["PyBatch"]})
    assert only["result"]["total"] == 2


def test_the_desktop_selector_is_accepted_where_the_ops_declare_it(rig: Rig) -> None:
    for name in ("desktop.list_children", "desktop.copy"):
        op = rig.registry.get(name)
        assert op is not None and "desktop" in op.accepts, name
    refused = rig.do("desktop.create", {"kind": "reel", "name": "x", "parent": DESKTOP})
    assert refused["ok"] is False and refused["errorCode"] == "INVALID_ARGS"
    assert "not accepted here" in refused["error"]


def test_a_desktop_source_is_not_accepted_and_a_batch_destination_neither(rig: Rig) -> None:
    env = copy(rig, [DESKTOP], LIB)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "not accepted here" in env["error"]
    env = copy(rig, [{"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip1"}], SRC)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "destination" in env["error"] and rig.exec.calls == []


LIB_PHASES: list[dict[str, Any]] = [
    {"name": "n"},
    {"name": "n", "allow_rename": True},
    {"name": "n", "kind": "library", "_phase": "read"},
    {"name": "n", "kind": "library", "_phase": "write", "_expected": ["u1"]},
    {"name": "n", "kind": "library", "_phase": "revert", "_created": {"uid": "u"}},
    {
        "name": "n",
        "kind": "library",
        "_phase": "alive",
        "_created": {"items": [{"uid": "u", "parent": None}]},
    },
]
COPY_BATCH_PHASES: list[dict[str, Any]] = [
    {"sources": [SRC], "destination": LIB},
    {"sources": [SRC], "destination": LIB, "_phase": "read"},
    {
        "sources": [],
        "destination": DESKTOP,
        "_phase": "read",
        "_created": {"known": ["u"], "batch": True},
    },
    {"sources": [SRC], "destination": LIB, "_phase": "write", "_expected": ["u"]},
    {
        "sources": [],
        "destination": DESKTOP,
        "_phase": "revert",
        "_created": {"copies": [{"uid": "u", "type": "PyBatch", "state": {}}]},
    },
    {
        "sources": [],
        "destination": DESKTOP,
        "_phase": "alive",
        "_created": {"items": [{"uid": "u", "parent": {"type": "PyDesktop", "uid": "d"}}]},
    },
]


@pytest.mark.parametrize(
    ("op_name", "args"),
    [("library.create", a) for a in LIB_PHASES] + [("desktop.copy", a) for a in COPY_BATCH_PHASES],
)
def test_every_phase_generates_valid_loop_free_code(op_name: str, args: dict[str, Any]) -> None:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    op = registry.get(op_name)
    assert op is not None and op.to_python is not None
    tree = ast.parse(op.to_python(args))
    loops = (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    assert not any(isinstance(node, loops) for node in ast.walk(tree))
    ast.parse(execution.build_exec_code(op, args))


def test_the_batch_copy_only_ever_asks_flame_for_add() -> None:
    from flame_mcp.desktop_batch_prelude import BATCH_COPY_HELPER

    assert "duplicate_action='add'" in BATCH_COPY_HELPER
    assert "'replace'" not in BATCH_COPY_HELPER and "'skip'" not in BATCH_COPY_HELPER


def test_the_run_module_names_the_new_notes() -> None:
    assert "Desktop" in desktop_run.RESTORE_NOTE and "current" in desktop_run.RESTORE_NOTE
    assert "current" in desktop_run.CURRENT_DELETED_NOTE


def test_index_treats_a_landed_library_create_as_revertible() -> None:
    records: list[dict[str, Any]] = [
        {
            "type": "intent",
            "id": "i1",
            "group": "g1",
            "kind": "desktop_create",
            "op": "library.create",
            "createKind": "library",
        },
        {"type": "outcome", "id": "i1", "group": "g1", "ok": True, "created": {"uid": "u"}},
    ]
    assert Index(records).intents["i1"].is_revertible() is True


def restored_group(rig: Rig) -> tuple[dict[str, Any], PyBatch, PyBatch]:
    original, _lib, path = backed_up(rig)
    env = copy(rig, [path], DESKTOP)
    assert env["ok"] is True, env
    return env, group_by_uid(rig, env["result"]["copies"][0]["uid"]), original


def test_the_attributes_section_sees_a_value_the_default_fingerprint_does_not(rig: Rig) -> None:
    group = make_group(rig)
    target = {"kind": "batch", "uid": group.uid.get_value()}

    def fp(**extra: Any) -> str:
        env = rig.do("batch.fingerprint", {"target": target, **extra})
        assert env["ok"] is True, env
        return str(env["result"]["fingerprint"])

    plain, attrs = fp(), fp(sections=["attributes"])
    group.nodes[0].bypass.set_value(True)
    assert fp() == plain and fp(sections=["attributes"]) != attrs


def test_revert_of_a_restore_is_a_conflict_when_only_a_node_value_changed(rig: Rig) -> None:
    env, new, _orig = restored_group(rig)
    new.nodes[0].bypass.set_value(True)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "attribute" in out["error"] and new in desktop(rig).batch_groups
    assert [r for r in rig.records() if r.get("type") == "checkpoint"] == []


def test_revert_of_a_restore_saves_the_setup_before_it_deletes_the_group(rig: Rig) -> None:
    env, new, _orig = restored_group(rig)
    out = revert(rig, env)
    assert out["ok"] is True, out
    saved = out["result"]["savedSetups"]
    assert len(saved) == 1 and saved[0]["name"] == "SRCBG"
    assert Path(saved[0]["path"]).is_file() and saved[0]["checkpoint"]
    assert (
        "batch.load_setup" in saved[0]["restoreWith"]
        and "desktop.create" in saved[0]["restoreWith"]
    )
    checkpoints = [r for r in rig.records() if r.get("type") == "checkpoint"]
    assert [c["id"] for c in checkpoints] == [saved[0]["checkpoint"]]
    assert checkpoints[0]["path"] == saved[0]["path"]
    assert new not in desktop(rig).batch_groups


def test_revert_of_a_restore_does_not_delete_when_the_setup_cannot_be_saved(rig: Rig) -> None:
    env, new, _orig = restored_group(rig)
    new.save_writes_nothing = True
    out = revert(rig, env)
    assert out["ok"] is False, out
    assert "nothing was deleted" in out["error"] and new in desktop(rig).batch_groups
    assert states(rig)[env["journal"]["group"]] == "landed"


def test_a_revert_of_a_copy_into_a_library_saves_no_setup(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    out = revert(rig, copy(rig, [SRC], lib_selector(lib)))
    assert out["ok"] is True and "savedSetups" not in out["result"]
    assert [r for r in rig.records() if r.get("type") == "checkpoint"] == []


def test_check_does_not_call_a_library_landed_when_the_new_one_has_another_name(rig: Rig) -> None:
    rig.exec.fault("library.create", "TIMEOUT", when="after", skip=1)
    env = create_library(rig, "LOST")
    next(lib for lib in workspace(rig).libraries if lib.name.get_value() == "LOST").name.set_value(
        "SOMETHING ELSE"
    )
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})["result"]["checked"][0]
    assert checked["verdict"] != "landed", checked


def test_check_accepts_the_name_flame_gives_a_taken_library_name(rig: Rig) -> None:
    rig.exec.fault("library.create", "TIMEOUT", when="after", skip=1)
    env = create_library(rig, "P1O", allow_rename=True)
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})["result"]["checked"][0]
    assert checked["verdict"] == "landed" and checked["created"]["name"] == "P1O 1"


def test_check_does_not_call_a_batch_copy_landed_when_the_new_group_has_another_name(
    rig: Rig,
) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    rig.exec.fault("desktop.copy", "TIMEOUT", when="after", skip=1)
    env = copy(rig, [SRC], lib_selector(lib))
    batches_in(lib)[0].name.set_value("ANOTHER")
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})["result"]["checked"][0]
    assert checked["verdict"] != "landed", checked


def edit_journal_state(rig: Rig, change: Any) -> None:
    path = next(rig.journal.directory.glob("journal_*.jsonl"))
    lines = []
    for line in path.read_text().splitlines():
        record = json.loads(line)
        for item in ((record.get("created") or {}).get("copies")) or []:
            change(item["state"])
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (lambda st: st.update(reels=None), "could not be read"),
        (lambda st: st["reels"][0].update(clips=None), "could not be read"),
        (lambda st: st.update(name=None), "could not be read"),
        (lambda st: st.update(inLibrary=False), "location"),
    ],
    ids=["reels-none", "clips-none", "name-none", "in-library"],
)
def test_a_value_that_could_not_be_read_is_a_problem_not_a_match(
    rig: Rig, change: Any, needle: str
) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    env = copy(rig, [SRC], lib_selector(lib))
    edit_journal_state(rig, change)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert needle in out["error"] and len(batches_in(lib)) == 1


def test_the_results_carry_node_count_and_fingerprint_of_the_source_and_the_copy(
    rig: Rig,
) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    backup = copy(rig, [SRC], lib_selector(lib))["result"]
    assert backup["sourceNodeCount"] == 5 and backup["sourceFingerprint"]
    assert backup["copyNodeCount"] is None and backup["copyFingerprint"] is None
    path = backup["copies"][0]["selector"]
    restore = copy(rig, [path], DESKTOP)["result"]
    assert restore["copyNodeCount"] == 5 and restore["sourceNodeCount"] is None
    assert restore["copyFingerprint"] == backup["sourceFingerprint"]


def opened(lib: Any) -> Any:
    return lib.opened.get_value()


def used_libraries(env: dict[str, Any]) -> list[dict[str, Any]]:
    value = env.get("result", {}).get("librariesOpened")
    return value if isinstance(value, list) else []


def test_library_create_revert_of_a_closed_library_with_content_is_a_conflict_and_stays_closed(
    rig: Rig,
) -> None:
    env = create_library(rig, "BACKUPS")
    lib = next(lib for lib in workspace(rig).libraries if lib.name.get_value() == "BACKUPS")
    rig.flame.media_panel.copy(rig.handles["clip1"], lib)
    lib.close()
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert "BACKUPS" in lib_names(rig) and opened(lib) is False
    assert used_libraries(out) == [
        {"name": "BACKUPS", "uid": lib.uid.get_value(), "closedAgain": True}
    ]
    lib.open()
    assert len(lib.children) == 1


def test_library_create_revert_of_an_empty_closed_library_deletes_it(rig: Rig) -> None:
    env = create_library(rig, "BACKUPS")
    lib = next(lib for lib in workspace(rig).libraries if lib.name.get_value() == "BACKUPS")
    lib.close()
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert "BACKUPS" not in lib_names(rig)
    assert [u["name"] for u in used_libraries(out)] == ["BACKUPS"]


@pytest.mark.parametrize("where", ["library", "folder"])
def test_a_batch_copy_into_a_closed_library_opens_it_and_closes_it_again(
    rig: Rig, where: str
) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    dest = lib.create_folder("F") if where == "folder" else lib
    lib.close()
    env = copy(rig, [SRC], {"kind": "wiretap", "id": dest.get_wiretap_node_id()})
    assert env["ok"] is True, env
    assert opened(lib) is False
    assert used_libraries(env) == [{"name": "BK", "uid": lib.uid.get_value(), "closedAgain": True}]
    assert any("closed" in n and "BK" in n for n in env["result"]["notes"])
    lib.open()
    assert len(batches_in(dest)) == 1


def test_a_clip_copy_into_a_closed_library_works_too(rig: Rig) -> None:
    lib = library(rig, "BK")
    reel = lib.create_reel("LR")
    lib.close()
    clip = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip1"}
    env = copy(rig, [clip], {"kind": "wiretap", "id": reel.get_wiretap_node_id()})
    assert env["ok"] is True, env
    assert opened(lib) is False and used_libraries(env)[0]["closedAgain"] is True
    lib.open()
    assert [c.name.get_value() for c in reel.children] == ["clip1"]


def test_a_library_that_cannot_be_opened_stops_the_call_before_anything_is_written(
    rig: Rig,
) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    lib.close()
    lib.open_fails = True
    env = copy(rig, [SRC], lib_selector(lib))
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "could not be opened" in env["error"] and rig.records() == []
    assert opened(lib) is False


def test_a_library_whose_open_state_cannot_be_read_stops_the_call(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    lib.opened.set_value(None)
    env = copy(rig, [SRC], lib_selector(lib))
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "could not be read" in env["error"] and rig.records() == []


def test_a_library_that_cannot_be_closed_again_is_reported_not_failed(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    lib.close()
    lib.close_fails = True
    env = copy(rig, [SRC], lib_selector(lib))
    assert env["ok"] is True, env
    assert used_libraries(env)[0]["closedAgain"] is False
    assert any("could not be closed again" in n for n in env["result"]["notes"])
    assert opened(lib) is True and len(batches_in(lib)) == 1


def test_a_restore_from_a_closed_library_works_and_closes_it_again(rig: Rig) -> None:
    _group, lib, path = backed_up(rig)
    lib.close()
    env = copy(rig, [path], DESKTOP)
    assert env["ok"] is True, env
    assert opened(lib) is False and used_libraries(env)[0]["name"] == "BK"
    assert len(desktop(rig).batch_groups) == 4


def test_revert_of_a_batch_copy_in_a_closed_library_deletes_it_and_leaves_it_closed(
    rig: Rig,
) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    env = copy(rig, [SRC], lib_selector(lib))
    lib.close()
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert opened(lib) is False and used_libraries(out)[0]["closedAgain"] is True
    lib.open()
    assert batches_in(lib) == []


def test_a_revert_of_a_copy_in_a_folder_of_a_closed_library_deletes_it(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    folder = lib.create_folder("F")
    env = copy(rig, [SRC], {"kind": "wiretap", "id": folder.get_wiretap_node_id()})
    assert env["ok"] is True, env
    lib.close()
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert opened(lib) is False
    lib.open()
    assert batches_in(folder) == []


def test_a_revert_that_cannot_open_the_library_does_not_call_the_copy_gone(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    env = copy(rig, [SRC], lib_selector(lib))
    lib.close()
    lib.open_fails = True
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "PRECONDITION_UNMET", out
    lib.open_fails = False
    lib.open()
    assert len(batches_in(lib)) == 1


def test_list_children_of_a_closed_library_opens_it_for_the_call(rig: Rig) -> None:
    lib = library(rig, "BK")
    lib.create_reel("LR")
    lib.close()
    env = rig.do("desktop.list_children", {"target": lib_selector(lib)})
    assert env["ok"] is True and [i["name"] for i in env["result"]["items"]] == ["LR"]
    assert opened(lib) is False and used_libraries(env)[0]["closedAgain"] is True


def test_a_path_selector_opens_the_library_it_starts_with(rig: Rig) -> None:
    lib = library(rig, "BK")
    lib.create_reel("LR")
    lib.close()
    env = rig.do("desktop.list_children", {"target": {"kind": "path", "path": ["BK"]}})
    assert env["ok"] is True and [i["name"] for i in env["result"]["items"]] == ["LR"]
    assert opened(lib) is False and used_libraries(env)[0]["name"] == "BK"


def test_a_call_that_touches_only_open_libraries_reports_nothing(rig: Rig) -> None:
    env = rig.do("desktop.list_children", {"target": LIB})
    assert env["ok"] is True and "librariesOpened" not in env["result"]


def test_the_fake_closed_library_looks_empty_and_wiretap_lookup_opens_it(rig: Rig) -> None:
    lib = library(rig, "BK")
    rig.flame.media_panel.copy(make_group(rig), lib)
    assert lib.close() is True
    assert lib.children == [] and lib.batch_groups == [] and lib.reels == []
    assert opened(lib) is False
    assert [x.name.get_value() for x in workspace(rig).libraries if x is lib] == ["BK"]
    assert opened(lib) is False  # listing name and uid does not open it
    assert rig.flame.find_by_wiretap_node_id(lib.get_wiretap_node_id()) is lib
    assert opened(lib) is True and len(batches_in(lib)) == 1
    assert lib.close() is True and lib.open() is True


@pytest.mark.parametrize(("first", "second"), [("X", "X 1"), ("X1", "X2"), ("X 1", "X 2")])
def test_the_fake_renames_a_taken_library_name_two_ways(rig: Rig, first: str, second: str) -> None:
    library(rig, first)
    assert library(rig, first).name.get_value() == second


@pytest.mark.parametrize(
    ("requested", "made", "allow", "landed"),
    [
        ("L", "L 1", True, True),
        ("L1", "L2", True, True),
        ("L", "L", True, True),
        ("L", "L", False, True),
        ("L", "L 1", False, False),
        ("L1", "L2", False, False),
        ("L", "Other", True, False),
        ("L1", "M2", True, False),
    ],
)
def test_check_matches_a_library_name_exactly_or_by_its_base_when_a_rename_was_allowed(
    rig: Rig, requested: str, made: str, allow: bool, landed: bool
) -> None:
    rig.exec.fault("library.create", "TIMEOUT", when="after", skip=1)
    env = create_library(rig, requested, allow_rename=allow)
    next(
        lib for lib in workspace(rig).libraries if lib.name.get_value() == requested
    ).name.set_value(made)
    checked = rig.do("journal.check", {"group": env["journal"]["group"]})["result"]["checked"][0]
    assert (checked["verdict"] == "landed") is landed, checked


def test_the_fake_refuses_duplicate_action_replace_for_a_batch_group(rig: Rig) -> None:
    group = make_group(rig)
    with pytest.raises(RuntimeError, match="not measured"):
        rig.flame.media_panel.copy(group, library(rig, "L"), duplicate_action="replace")


def test_an_early_refusal_of_a_write_still_tells_which_library_was_left_open(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    lib.close()
    lib.close_fails = True

    def broken(record: dict[str, Any]) -> None:
        raise OSError("disk full")

    rig.journal.append = broken  # type: ignore[method-assign]
    env = copy(rig, [SRC], lib_selector(lib))
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert used_libraries(env)[0]["closedAgain"] is False
    assert any("could not be closed again" in n for n in env["result"]["notes"])


def test_an_answer_that_never_came_still_tells_which_library_was_left_open(rig: Rig) -> None:
    make_group(rig)
    lib = library(rig, "BK")
    lib.close()
    lib.close_fails = True
    rig.exec.fault("desktop.copy", "BRIDGE_LOST", when="before", skip=1)
    env = copy(rig, [SRC], lib_selector(lib))
    assert env["errorCode"] == "BRIDGE_LOST", env
    assert used_libraries(env)[0]["closedAgain"] is False


def test_an_exception_while_closing_does_not_lose_the_result_of_the_op(rig: Rig) -> None:
    lib = library(rig, "BK")
    lib.create_reel("LR")
    lib.close()

    def boom() -> bool:
        raise RuntimeError("close exploded")

    lib.close = boom
    env = rig.do("desktop.list_children", {"target": lib_selector(lib)})
    assert env["ok"] is True and [i["name"] for i in env["result"]["items"]] == ["LR"]
    assert used_libraries(env)[0]["closedAgain"] is False
    assert any("could not be closed again" in n for n in env["result"]["notes"])


def prelude_namespace(rig: Rig) -> dict[str, Any]:
    from flame_mcp.library_open_prelude import LIBRARY_OPEN_HELPER

    def try_(fn: Any) -> Any:
        try:
            return fn()
        except BaseException:
            return None

    ns: dict[str, Any] = {
        "flame": rig.flame,
        "_v": lambda x: x.get_value() if hasattr(x, "get_value") else x,
        "_ja": lambda x: x,
        "_try": try_,
        "_OpError": type("_OpError", (Exception,), {"__init__": lambda s, c, m: None}),
    }
    exec(LIBRARY_OPEN_HELPER, ns)
    return ns


@pytest.mark.parametrize("notes", ["a note", None, 7, ["one"]])
def test_a_report_keeps_notes_that_are_a_string_whole(rig: Rig, notes: Any) -> None:
    ns = prelude_namespace(rig)
    state = {"opened": [{"name": "L", "uid": "u", "closedAgain": True}]}
    value = ns["_lo_report"]({"notes": notes}, state)
    expected = (
        [notes] if isinstance(notes, str) else (list(notes) if isinstance(notes, list) else [])
    )
    assert value["notes"][: len(expected)] == expected
    assert len(value["notes"]) == len(expected) + 1


def test_a_library_the_op_deleted_is_reported_as_deleted_not_as_closed_again(rig: Rig) -> None:
    env = create_library(rig, "BACKUPS")
    lib = next(lib for lib in workspace(rig).libraries if lib.name.get_value() == "BACKUPS")
    lib.close()
    out = revert(rig, env)
    assert out["ok"] is True, out
    row = used_libraries(out)[0]
    assert row["deleted"] is True and row["closedAgain"] is None
    assert any("deleted" in n and "BACKUPS" in n for n in out["result"]["notes"])


def test_library_list_says_which_libraries_are_closed(rig: Rig) -> None:
    lib = library(rig, "BK")
    lib.close()
    env = rig.do("library.list", {})
    rows = {i["name"]: i for i in env["result"]["items"]}
    assert rows["BK"]["opened"] is False and rows["P1O"]["opened"] is True
    assert opened(lib) is False  # reading the list does not open it


def test_the_read_descriptions_say_what_a_closed_library_looks_like() -> None:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    for name in ("library.list", "desktop.find", "desktop.info"):
        op = registry.get(name)
        assert op is not None and "closed library" in op.description, name
    find = registry.get("desktop.find")
    assert find is not None and "lists nothing from inside a closed library" in find.description
