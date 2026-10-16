"""`desktop.*` tests: list_children / selection / rename, run directly
against `fake_media_tree.FakeFlame` (no bridge)."""

from __future__ import annotations

from typing import Any

import pytest

from fake_media_tree import (
    FakeAttr,
    FakeFlame,
    PyClip,
    PyDesktop,
    PyLibrary,
    PyReel,
    PyReelGroup,
    PySequence,
    PyTime,
    PyTrack,
    PyVersion,
    build_default_tree,
)
from flame_mcp.ops import register_all
from flame_mcp.opschema import validate_op_args
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from journal_test_helpers import Rig, make_rig
from op_test_helpers import run_op
from test_ops_attributes import make_batch_world


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


def test_list_children_via_path_lists_reel_contents() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    result = run_op(
        registry,
        "desktop.list_children",
        {"target": {"kind": "path", "path": ["P1O", "R1"]}, "limit": 50},
        flame,
    )
    assert result["opError"] is None
    names = {item["name"] for item in result["value"]["items"]}
    assert names == {"clip1", "clip2", "clip3"}
    assert all(item["type"] == "PyClip" for item in result["value"]["items"])
    assert all("duration" in item and "frameRate" in item for item in result["value"]["items"])


def test_list_children_via_wiretap() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(
        registry,
        "desktop.list_children",
        {"target": {"kind": "wiretap", "id": h["lib"].get_wiretap_node_id()}, "limit": 50},
        flame,
    )
    assert result["value"]["total"] == 1
    assert result["value"]["items"][0]["name"] == "R1"


def test_list_children_of_a_clip_is_selector_type() -> None:
    # A PyClip has no `.children` -- accessing it raises, which the op must
    # map to SELECTOR_TYPE, not let escape as a raw traceback.
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(
        registry,
        "desktop.list_children",
        {"target": {"kind": "wiretap", "id": h["clip1"].get_wiretap_node_id()}, "limit": 50},
        flame,
    )
    assert result["opErrorCode"] == "SELECTOR_TYPE"


def test_list_children_truncated_reports_total_and_shown() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    result = run_op(
        registry,
        "desktop.list_children",
        {"target": {"kind": "path", "path": ["P1O", "R1"]}, "limit": 2},
        flame,
    )
    assert result["value"]["total"] == 3
    assert result["value"]["shown"] == 2
    assert result["value"]["truncated"] is True


def _mixed_container() -> tuple[FakeFlame, dict[str, Any]]:
    """Library `Mix` holding c0 (PyClip), s1 (PySequence), c2 (PyClip),
    r3 (PyReel), c4 (PyClip), s5 (PySequence) in that order."""
    kids = [
        PyClip("c0", wiretap_id="/m/c0"),
        PySequence("s1", wiretap_id="/m/s1"),
        PyClip("c2", wiretap_id="/m/c2"),
        PyReel("r3", uid="u3", wiretap_id="/m/r3"),
        PyClip("c4", wiretap_id="/m/c4"),
        PySequence("s5", wiretap_id="/m/s5"),
    ]
    lib = PyLibrary("Mix", uid="uid-mix", wiretap_id="/m", children=kids)
    flame = FakeFlame(libraries=[lib])
    return flame, {"lib": lib}


_MIX_TARGET = {"kind": "path", "path": ["Mix"]}


def test_list_children_offset_returns_the_second_page() -> None:
    registry = make_registry()
    flame, _h = _mixed_container()
    first = run_op(
        registry,
        "desktop.list_children",
        {"target": _MIX_TARGET, "limit": 4, "offset": 0},
        flame,
    )["value"]
    second = run_op(
        registry,
        "desktop.list_children",
        {"target": _MIX_TARGET, "limit": 4, "offset": 4},
        flame,
    )["value"]
    assert [i["name"] for i in first["items"]] == ["c0", "s1", "c2", "r3"]
    assert [i["name"] for i in second["items"]] == ["c4", "s5"]
    assert (first["total"], first["offset"], first["shown"], first["truncated"]) == (6, 0, 4, True)
    assert (second["total"], second["offset"], second["shown"], second["truncated"]) == (
        6,
        4,
        2,
        False,
    )


def test_list_children_offset_defaults_to_zero() -> None:
    registry = make_registry()
    flame, _h = _mixed_container()
    value = run_op(registry, "desktop.list_children", {"target": _MIX_TARGET, "limit": 2}, flame)[
        "value"
    ]
    assert value["offset"] == 0
    assert [i["name"] for i in value["items"]] == ["c0", "s1"]


def test_list_children_offset_past_the_end_is_empty_not_truncated() -> None:
    registry = make_registry()
    flame, _h = _mixed_container()
    value = run_op(
        registry,
        "desktop.list_children",
        {"target": _MIX_TARGET, "limit": 4, "offset": 99},
        flame,
    )["value"]
    assert value == {"total": 6, "offset": 99, "shown": 0, "truncated": False, "items": []}


def test_list_children_types_keeps_only_the_named_types() -> None:
    registry = make_registry()
    flame, _h = _mixed_container()
    value = run_op(
        registry,
        "desktop.list_children",
        {"target": _MIX_TARGET, "limit": 50, "types": ["PyReel"]},
        flame,
    )["value"]
    assert [i["name"] for i in value["items"]] == ["r3"]
    assert value["total"] == 1


def test_list_children_types_accepts_several_names() -> None:
    registry = make_registry()
    flame, _h = _mixed_container()
    value = run_op(
        registry,
        "desktop.list_children",
        {"target": _MIX_TARGET, "limit": 50, "types": ["PyClip", "PySequence"]},
        flame,
    )["value"]
    assert [i["name"] for i in value["items"]] == ["c0", "s1", "c2", "c4", "s5"]
    assert value["total"] == 5


def test_list_children_total_and_paging_count_the_filtered_items() -> None:
    registry = make_registry()
    flame, _h = _mixed_container()
    value = run_op(
        registry,
        "desktop.list_children",
        {"target": _MIX_TARGET, "limit": 2, "offset": 2, "types": ["PyClip"]},
        flame,
    )["value"]
    assert [i["name"] for i in value["items"]] == ["c4"]
    assert (value["total"], value["shown"], value["truncated"]) == (3, 1, False)


def test_list_children_types_naming_nothing_present_is_empty() -> None:
    registry = make_registry()
    flame, _h = _mixed_container()
    value = run_op(
        registry,
        "desktop.list_children",
        {"target": _MIX_TARGET, "limit": 50, "types": ["PyBatchGroup"]},
        flame,
    )["value"]
    assert (value["total"], value["items"]) == (0, [])


def test_list_children_empty_types_means_no_filter() -> None:
    registry = make_registry()
    flame, _h = _mixed_container()
    value = run_op(
        registry, "desktop.list_children", {"target": _MIX_TARGET, "limit": 50, "types": []}, flame
    )["value"]
    assert value["total"] == 6


def test_list_children_invalid_args_offset_and_types() -> None:
    registry = make_registry()
    op = _op(registry, "desktop.list_children")
    target = {"kind": "selection"}
    assert validate_op_args(op, {"target": target, "offset": 3, "types": ["PyClip"]}).ok is True
    for bad in (
        {"offset": -1},
        {"offset": 1.5},
        {"offset": "2"},
        {"types": "PyClip"},
        {"types": [1]},
    ):
        assert validate_op_args(op, {"target": target, **bad}).ok is False, bad


def test_selection_empty() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    result = run_op(registry, "desktop.selection", {"limit": 50}, flame)
    assert result["value"] == {
        "total": 0,
        "offset": 0,
        "shown": 0,
        "truncated": False,
        "items": [],
    }


def test_selection_reflects_current_selection() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    flame.media_panel.selected_entries = [h["clip2"]]
    result = run_op(registry, "desktop.selection", {"limit": 50}, flame)
    assert result["value"]["total"] == 1
    assert result["value"]["items"][0]["name"] == "clip2"


def test_selection_offset_returns_the_second_page() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    flame.media_panel.selected_entries = [h["clip1"], h["clip2"], h["clip3"]]
    value = run_op(registry, "desktop.selection", {"limit": 2, "offset": 2}, flame)["value"]
    assert [i["name"] for i in value["items"]] == ["clip3"]
    assert (value["total"], value["offset"], value["truncated"]) == (3, 2, False)


def test_rename_via_path_updates_name_and_verifies() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(
        registry,
        "desktop.rename",
        {"target": {"kind": "path", "path": ["P1O", "R1"]}, "name": "R1_new"},
        flame,
    )
    assert result["opError"] is None
    assert result["verified"] is True
    wiretap_id = h["reel"].get_wiretap_node_id()
    assert result["value"] == {
        "type": "PyReel",
        "before": "R1",
        "after": "R1_new",
        "applied": ["name"],
        "failed": {},
        "readBack": {"name": "R1_new"},
        "wiretapId": wiretap_id,
        "revert": {
            "op": "desktop.rename",
            "args": {"target": {"kind": "wiretap", "id": wiretap_id}, "name": "R1"},
        },
    }
    assert h["reel"].name.get_value() == "R1_new"


def test_rename_via_wiretap_updates_name() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(
        registry,
        "desktop.rename",
        {"target": {"kind": "wiretap", "id": h["clip1"].get_wiretap_node_id()}, "name": "renamed"},
        flame,
    )
    assert result["verified"] is True
    assert h["clip1"].name.get_value() == "renamed"


def test_rename_via_selection_updates_name() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    flame.media_panel.selected_entries = [h["clip3"]]
    result = run_op(
        registry, "desktop.rename", {"target": {"kind": "selection"}, "name": "renamed3"}, flame
    )
    assert result["verified"] is True
    assert h["clip3"].name.get_value() == "renamed3"


def test_rename_result_includes_wiretap_id_and_revert() -> None:
    # A rename through set_value() is not undone by Ctrl+Z, so the result carries a revert
    # (the op/args that call rename again) that stays valid across wiretap IDs.
    registry = make_registry()
    flame, h = build_default_tree()
    wiretap_id = h["clip1"].get_wiretap_node_id()
    result = run_op(
        registry,
        "desktop.rename",
        {"target": {"kind": "wiretap", "id": wiretap_id}, "name": "renamed"},
        flame,
    )
    assert result["value"]["wiretapId"] == wiretap_id
    assert result["value"]["revert"] == {
        "op": "desktop.rename",
        "args": {"target": {"kind": "wiretap", "id": wiretap_id}, "name": "clip1"},
    }


class _NoWiretapEntry:
    """A Media Panel-shaped entry with no `get_wiretap_node_id` at all --
    like `fake_media_tree.PySegment`, this mirrors a real Flame class that
    genuinely lacks the method, not merely one returning None."""

    def __init__(self, name: str) -> None:
        self.name = FakeAttr(name)


def test_rename_revert_is_none_when_wiretap_id_unavailable() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    entry = _NoWiretapEntry("no_wiretap")
    flame.media_panel.selected_entries = [entry]

    result = run_op(
        registry, "desktop.rename", {"target": {"kind": "selection"}, "name": "renamed"}, flame
    )

    assert result["opError"] is None
    assert result["value"]["wiretapId"] is None
    assert result["value"]["revert"] is None


def test_rename_description_warns_that_ctrl_z_does_not_revert_it() -> None:
    registry = make_registry()
    op = _op(registry, "desktop.rename")
    assert "set_value" in op.description
    assert "revert" in op.description


def test_fake_attr_str_is_quoted_unlike_get_value() -> None:
    # str(PyAttribute) is quoted, unlike repr()/get_value().
    # Ops must only ever read a name through `_v()`, never `str()` -- this
    # fixture check guards the double the other rename tests rely on.
    _flame, h = build_default_tree()
    assert str(h["reel"].name) == "'R1'"
    assert h["reel"].name.get_value() == "R1"


class _NoopAttr(FakeAttr):
    def set_value(self, value: object) -> None:
        pass  # silently ignores the write, like a set_value that "doesn't stick"


_NoopAttr.__name__ = "PyAttribute"


def test_rename_silent_noop_when_set_value_does_not_stick() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    h["reel"].name = _NoopAttr("R1")

    result = run_op(
        registry,
        "desktop.rename",
        {"target": {"kind": "wiretap", "id": h["reel"].get_wiretap_node_id()}, "name": "R1_new"},
        flame,
    )

    assert result["opError"] is None  # no exception -- set_value() just didn't stick
    assert result["verified"] is False
    assert "R1_new" in result["verifyDetail"]
    assert "R1" in result["verifyDetail"]


def test_rename_unknown_wiretap_id_is_selector_empty() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    args = {"target": {"kind": "wiretap", "id": "nope"}, "name": "x"}
    result = run_op(registry, "desktop.rename", args, flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"


def test_rename_unknown_path_is_selector_empty() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    result = run_op(
        registry,
        "desktop.rename",
        {"target": {"kind": "path", "path": ["NoSuchLib"]}, "name": "x"},
        flame,
    )
    assert result["opErrorCode"] == "SELECTOR_EMPTY"


def test_rename_selection_of_zero_is_selector_empty() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    flame.media_panel.selected_entries = []
    args = {"target": {"kind": "selection"}, "name": "x"}
    result = run_op(registry, "desktop.rename", args, flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"


def test_rename_selection_of_two_is_selector_ambiguous() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    flame.media_panel.selected_entries = [h["clip1"], h["clip2"]]
    args = {"target": {"kind": "selection"}, "name": "x"}
    result = run_op(registry, "desktop.rename", args, flame)
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"


def test_list_children_ambiguous_path_is_selector_ambiguous() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    dup_reel = PyReel("R1", uid="uid-dup", wiretap_id="/dup/R1")
    h["lib"]._children.append(dup_reel)
    result = run_op(
        registry,
        "desktop.list_children",
        {"target": {"kind": "path", "path": ["P1O", "R1"]}, "limit": 50},
        flame,
    )
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"


def _op(registry: Registry, name: str) -> Any:
    op = registry.get(name)
    assert op is not None
    return op


def test_rename_invalid_args_wrong_selector_shape() -> None:
    registry = make_registry()
    op = _op(registry, "desktop.rename")
    result = validate_op_args(op, {"target": "not-an-object", "name": "x"})
    assert result.ok is False


def test_rename_invalid_args_empty_name_rejected() -> None:
    registry = make_registry()
    op = _op(registry, "desktop.rename")
    result = validate_op_args(op, {"target": {"kind": "selection"}, "name": ""})
    assert result.ok is False
    assert any("must not be empty" in issue.message for issue in result.issues)


def test_rename_invalid_args_missing_name() -> None:
    registry = make_registry()
    op = _op(registry, "desktop.rename")
    result = validate_op_args(op, {"target": {"kind": "selection"}})
    assert result.ok is False


def test_list_children_invalid_args_unaccepted_selector_kind() -> None:
    # list_children accepts wiretap/path/selection -- not "segment".
    registry = make_registry()
    op = _op(registry, "desktop.list_children")
    result = validate_op_args(
        op,
        {
            "target": {
                "kind": "segment",
                "parent": {"kind": "selection"},
                "version": 0,
                "track": 0,
                "index": 0,
            }
        },
    )
    assert result.ok is False
    assert any("not accepted" in issue.message for issue in result.issues)


def test_rename_valid_args_accepted() -> None:
    registry = make_registry()
    op = _op(registry, "desktop.rename")
    result = validate_op_args(op, {"target": {"kind": "wiretap", "id": "x"}, "name": "ok"})
    assert result.ok is True


@pytest.mark.parametrize("op_name", ["desktop.list_children", "desktop.selection"])
def test_read_ops_have_zero_undo_steps(op_name: str) -> None:
    registry = make_registry()
    op = _op(registry, op_name)
    assert op.effect == "read"
    assert op.undo_steps({}, 3) == 0


def test_rename_undo_steps_is_always_zero() -> None:
    # Writes through set_value() are not recorded in Flame's undo history, so the op always
    # reports 0 steps whatever the value of affected (Ctrl+Z does not undo them).
    registry = make_registry()
    op = _op(registry, "desktop.rename")
    assert op.undo_steps({}, 0) == 0
    assert op.undo_steps({}, 1) == 0
    assert op.undo == "value"


def _find_world() -> tuple[FakeFlame, dict[str, Any]]:
    """The default tree's library holds clip1 / clip2 / clip3 in R1; the Desktop's reel RR (in
    reel group RG) holds ANOTHER clip1 and a sequence S1."""
    flame, h = build_default_tree()
    dup = PyClip("clip1", wiretap_id="/wt/RG/RR/clip1", uid="uid-dup")
    seq = PySequence("S1", wiretap_id="/wt/RG/RR/S1", uid="uid-s1")
    rr = PyReel("RR", uid="uid-rr", wiretap_id="/wt/RG/RR", children=[dup, seq])
    rg = PyReelGroup("RG", "/wt/RG", reels=[rr])
    flame.projects.current_project.current_workspace.desktop = PyDesktop(reel_groups=[rg])
    h.update(dup=dup, seq1=seq, rr=rr, rg=rg)
    return flame, h


def _find(flame: Any, **args: Any) -> dict[str, Any]:
    return run_op(make_registry(), "desktop.find", {"limit": 50, "offset": 0, **args}, flame)


def test_find_by_name_returns_the_desktop_and_the_library_match_together() -> None:
    flame, h = _find_world()
    result = _find(flame, name="clip1")
    assert result["opError"] is None
    value = result["value"]
    assert (value["total"], value["shown"], value["truncated"]) == (2, 2, False)
    by_uid = {i["uid"]: i for i in value["items"]}
    assert set(by_uid) == {"uid-dup", h["clip1"].uid.get_value()}
    dup = by_uid["uid-dup"]
    assert dup == {
        "type": "PyClip",
        "name": "clip1",
        "uid": "uid-dup",
        "selector": {"kind": "wiretap", "id": "/wt/RG/RR/clip1"},
        "parent": {"type": "PyReel", "name": "RR"},
    }
    lib = by_uid[h["clip1"].uid.get_value()]
    assert lib["parent"] == {"type": "PyReel", "name": "R1"}
    assert lib["selector"] == {"kind": "wiretap", "id": h["clip1"].get_wiretap_node_id()}


def test_find_by_name_with_a_parent_keeps_only_that_parents_match() -> None:
    flame, _h = _find_world()
    in_desktop = _find(flame, name="clip1", parent={"kind": "wiretap", "id": "/wt/RG/RR"})
    assert [i["uid"] for i in in_desktop["value"]["items"]] == ["uid-dup"]
    in_library = _find(flame, name="clip1", parent={"kind": "path", "path": ["P1O", "R1"]})
    assert [i["parent"]["name"] for i in in_library["value"]["items"]] == ["R1"]
    assert in_library["value"]["total"] == 1


def test_find_by_name_that_nothing_carries_is_an_empty_page_not_an_error() -> None:
    flame, _h = _find_world()
    result = _find(flame, name="no such name")
    assert result["opError"] is None
    assert result["value"] == {"total": 0, "offset": 0, "shown": 0, "truncated": False, "items": []}


def test_find_by_uid_returns_that_one_object() -> None:
    flame, _h = _find_world()
    result = _find(flame, uid="uid-s1")
    assert [(i["type"], i["name"]) for i in result["value"]["items"]] == [("PySequence", "S1")]
    assert result["value"]["total"] == 1


def test_find_by_an_unknown_uid_is_an_empty_page_not_an_error() -> None:
    flame, _h = _find_world()
    result = _find(flame, uid="uid-nobody")
    assert result["opError"] is None
    assert (result["value"]["total"], result["value"]["items"]) == (0, [])


def test_find_types_keeps_only_the_named_types_and_total_counts_after_it() -> None:
    flame, _h = _find_world()
    assert _find(flame, name="clip1", types=["PySequence"])["value"]["total"] == 0
    both = _find(flame, name="clip1", types=["PyClip", "PySequence"])
    assert both["value"]["total"] == 2
    assert _find(flame, uid="uid-s1", types=["PyClip"])["value"]["items"] == []
    assert _find(flame, uid="uid-s1", types=["PySequence"])["value"]["total"] == 1


def test_find_pages_with_limit_and_offset() -> None:
    flame, _h = _find_world()
    first = _find(flame, name="clip1", limit=1, offset=0)["value"]
    second = _find(flame, name="clip1", limit=1, offset=1)["value"]
    assert (first["total"], first["shown"], first["truncated"]) == (2, 1, True)
    assert (second["total"], second["shown"], second["truncated"], second["offset"]) == (
        2,
        1,
        False,
        1,
    )
    assert first["items"][0]["uid"] != second["items"][0]["uid"]


def test_find_an_object_without_a_wiretap_id_has_no_selector() -> None:
    flame, _h = make_batch_world()
    result = _find(flame, name="BG1")
    batch = result["value"]["items"][0]
    assert batch["type"] == "PyBatch" and batch["selector"] is None


def test_find_is_a_read_that_accepts_wiretap_and_path_parents_only() -> None:
    op = make_registry().get("desktop.find")
    assert op is not None
    assert op.effect == "read" and op.accepts == ("wiretap", "path")
    rejected = validate_op_args(op, {"name": "x", "parent": {"kind": "selection"}})
    assert rejected.ok is False
    assert "across the Desktop and the libraries" in op.description
    assert "parent" in op.description


def test_find_needs_exactly_one_of_name_and_uid_and_a_parent_only_with_a_name() -> None:
    op = make_registry().get("desktop.find")
    assert op is not None and op.precheck is not None
    policy = Policy.from_env({})
    parent = {"kind": "wiretap", "id": "x"}
    assert op.precheck({"name": "a"}, policy) is None
    assert op.precheck({"uid": "a"}, policy) is None
    assert op.precheck({"name": "a", "parent": parent}, policy) is None
    for bad in ({}, {"name": "a", "uid": "b"}, {"uid": "a", "parent": parent}):
        refusal = op.precheck(bad, policy)
        assert refusal is not None and refusal.code == "INVALID_ARGS", bad


def test_find_refuses_a_bad_combination_before_anything_reaches_flame(rig: Rig) -> None:
    for args in ({}, {"name": "a", "uid": "b"}):
        env = rig.do("desktop.find", args)
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert rig.exec.calls == []


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def test_find_through_the_runtime_returns_the_page(rig: Rig) -> None:
    env = rig.do("desktop.find", {"name": "clip1"})
    assert env["ok"] is True, env
    assert env["result"]["total"] == 1
    assert env["undoSteps"] == 0


def _info(flame: Any, target: dict[str, Any]) -> dict[str, Any]:
    return run_op(make_registry(), "desktop.info", {"target": target}, flame)


def _media_world() -> tuple[FakeFlame, dict[str, Any]]:
    flame, h = build_default_tree()
    seq = PySequence(
        "S1H",
        wiretap_id="/projects/p/ws/P1O/R1/S1H",
        uid="uid-s1h",
        duration=PyTime(96),
        start_frame=86401,
        start_time=PyTime(86400),
        versions=[PyVersion([PyTrack(), PyTrack()]), PyVersion([PyTrack()])],
        audio_tracks=[object()],
    )
    h["reel"]._children.append(seq)
    seq.parent = h["reel"]
    h["s1h"] = seq
    return flame, h


def test_info_of_a_sequence_reads_every_media_field() -> None:
    flame, h = _media_world()
    result = _info(flame, {"kind": "wiretap", "id": h["s1h"].get_wiretap_node_id()})
    assert result["opError"] is None
    value = result["value"]
    assert value["type"] == "PySequence" and value["name"] == "S1H"
    assert value["uid"] == "uid-s1h"
    assert value["wiretapId"] == "/projects/p/ws/P1O/R1/S1H"
    assert value["parent"] == {"type": "PyReel", "name": "R1"}
    assert value["duration"] == {"timecode": "00:00:04+00", "frame": 96}
    assert value["startFrame"] == 86401
    assert value["startTime"] == {"timecode": "01:00:00+00", "frame": 86400}
    assert value["frameRate"] == "23.976 fps"
    assert (value["width"], value["height"], value["bitDepth"]) == (1920, 1080, 10)
    assert value["ratio"] == pytest.approx(16 / 9)
    assert (value["scanMode"], value["cached"], value["unlinked"]) == ("P", "Cached", "")
    assert value["versions"] == [2, 1]
    assert value["audioTracks"] == 1
    assert "childCounts" not in value


def test_info_of_a_clip_is_the_same_shape() -> None:
    flame, h = build_default_tree()
    value = _info(flame, {"kind": "wiretap", "id": h["clip2"].get_wiretap_node_id()})["value"]
    assert value["type"] == "PyClip" and value["name"] == "clip2"
    assert value["versions"] == [1] and value["audioTracks"] == 0
    assert value["parent"] == {"type": "PyReel", "name": "R1"}


def test_info_reads_each_item_on_its_own_and_reports_the_unreadable_one() -> None:
    class _Flaky(PySequence):
        @property
        def width(self) -> Any:
            raise RuntimeError("no width")

        @width.setter
        def width(self, _value: Any) -> None:
            pass

        @property
        def versions(self) -> list[PyVersion]:
            raise RuntimeError("no versions")

    _Flaky.__name__ = "PySequence"  # the op dispatches on the type name, like Flame's classes
    flame, _h = build_default_tree()
    flaky = _Flaky("flaky", wiretap_id="/wt/flaky", uid="uid-f")
    flame._wiretap_index["/wt/flaky"] = flaky
    result = _info(flame, {"kind": "wiretap", "id": "/wt/flaky"})
    assert result["opError"] is None
    value = result["value"]
    assert value["width"] == {"error": "no width"}
    assert value["versions"] == {"error": "no versions"}
    assert value["height"] == 1080 and value["name"] == "flaky" and value["audioTracks"] == 0


def test_info_of_a_reel_a_library_and_a_reel_group_counts_children_by_type() -> None:
    flame, h = _find_world()
    reel = _info(flame, {"kind": "wiretap", "id": h["reel"].get_wiretap_node_id()})["value"]
    assert reel["type"] == "PyReel" and reel["childCounts"] == {"PyClip": 3}
    assert reel["parent"] == {"type": "PyLibrary", "name": "P1O"}
    assert "duration" not in reel
    lib = _info(flame, {"kind": "wiretap", "id": h["lib"].get_wiretap_node_id()})["value"]
    assert lib["childCounts"] == {"PyReel": 1}
    rr = _info(flame, {"kind": "wiretap", "id": "/wt/RG/RR"})["value"]
    assert rr["childCounts"] == {"PyClip": 1, "PySequence": 1}
    group = _info(flame, {"kind": "wiretap", "id": "/wt/RG"})["value"]
    assert group["type"] == "PyReelGroup" and group["childCounts"] == {"PyReel": 1}
    assert group["parent"] == {"type": "PyDesktop", "name": "Desktop"}


def test_info_of_the_desktop_counts_its_groups() -> None:
    flame, _h = _find_world()
    flame.media_panel.selected_entries = [flame.projects.current_project.current_workspace.desktop]
    value = _info(flame, {"kind": "selection"})["value"]
    assert value["type"] == "PyDesktop" and value["childCounts"] == {"PyReelGroup": 1}
    assert value["parent"] == {"type": "PyWorkspace", "name": "Workspace"}


def test_info_reports_unreadable_children_without_failing() -> None:
    flame, _h = _find_world()
    desktop = flame.projects.current_project.current_workspace.desktop
    desktop.children = None  # not a list: reading its entries fails
    flame.media_panel.selected_entries = [desktop]
    value = _info(flame, {"kind": "selection"})["value"]
    assert "error" in value["childCounts"]
    assert value["name"] == "Desktop"


def test_info_of_a_type_it_has_no_values_for_gives_the_common_fields_only() -> None:
    flame, _h = make_batch_world()
    value = _info(flame, {"kind": "path", "path": ["BG1"], "root": "desktop"})["value"]
    assert value["type"] == "PyBatch" and value["name"] == "BG1"
    assert value["wiretapId"] is None
    assert not {"childCounts", "duration", "versions"} & set(value)


def test_info_accepts_wiretap_path_selection_and_timeline_selectors_only() -> None:
    op = make_registry().get("desktop.info")
    assert op is not None and op.effect == "read"
    assert op.accepts == ("wiretap", "path", "selection", "timeline")
    rejected = validate_op_args(op, {"target": {"kind": "batch", "name": "x"}})
    assert rejected.ok is False


def test_info_unknown_wiretap_is_selector_empty() -> None:
    flame, _h = build_default_tree()
    result = _info(flame, {"kind": "wiretap", "id": "nope"})
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
