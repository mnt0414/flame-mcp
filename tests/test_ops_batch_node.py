"""Tests for the `batch` / `node` / `action_node` / `path` selectors.

They are resolved by the prelude in execution.py. Also `batch.list` and `node.list`. Run against
the fake tree (no bridge)."""

from __future__ import annotations

from typing import Any

import pytest

from fake_media_tree import (
    FakeAttr,
    FakeFlame,
    PyBatch,
    PyClip,
    PyDesktop,
    PyNode,
    PyReel,
    PyReelGroup,
)
from flame_mcp import selectors
from flame_mcp.ops import register_all
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationParam, OperationSpec
from op_test_helpers import run_op
from test_ops_attributes import BG1, make_batch_world


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


def resolver_registry() -> Registry:
    """A throwaway op that resolves any selector kind and reports what it found."""
    registry = Registry(symbols=frozenset())
    registry.register_category(
        Category(
            name="probe",
            description="probe",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    registry.register(
        OperationSpec(
            name="probe.resolve",
            category="probe",
            description="resolve a selector",
            effect="read",
            params=(
                OperationParam(name="target", type="selector", description="t", required=True),
            ),
            accepts=("selection", "wiretap", "path", "segment", "batch", "node", "action_node"),
            to_python=lambda args: "\n".join(
                [
                    selectors.resolve_code(args["target"], "target"),
                    "_result = {'type': type(target).__name__, 'name': _v(target.name)}",
                ]
            ),
        )
    )
    return registry


def resolve(selector: dict[str, Any], flame: Any) -> dict[str, Any]:
    return run_op(resolver_registry(), "probe.resolve", {"target": selector}, flame)


def test_batch_resolves_by_name_and_by_uid_without_being_current() -> None:
    flame, h = make_batch_world()
    desktop = flame.projects.current_project.current_workspace.desktop
    assert h["bg1"] is not desktop.current_batch_group.get_value()
    by_name = resolve({"kind": "batch", "name": "BG1"}, flame)
    by_uid = resolve({"kind": "batch", "uid": "uid-b1"}, flame)
    assert by_name["value"] == {"type": "PyBatch", "name": "BG1"}
    assert by_uid["value"] == by_name["value"]


@pytest.mark.parametrize("selector", [{"name": "Nope"}, {"uid": "nope"}])
def test_batch_no_match_is_selector_empty(selector: dict[str, str]) -> None:
    flame, _h = make_batch_world()
    result = resolve({"kind": "batch", **selector}, flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"


def test_batch_duplicate_name_is_selector_ambiguous() -> None:
    flame, h = make_batch_world()
    desktop = flame.projects.current_project.current_workspace.desktop
    desktop.batch_groups.append(PyBatch("BG1", "uid-dup"))
    result = resolve({"kind": "batch", "name": "BG1"}, flame)
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"
    assert "2" in result["opError"]
    assert resolve({"kind": "batch", "uid": "uid-dup"}, flame)["opError"] is None
    assert h  # unused


def test_node_resolves_in_the_named_group_only() -> None:
    flame, h = make_batch_world()
    in_bg1 = resolve({"kind": "node", "batch": BG1, "name": "blur1"}, flame)
    in_bg2 = resolve(
        {"kind": "node", "batch": {"kind": "batch", "name": "BG2"}, "name": "blur1"}, flame
    )
    assert in_bg1["opError"] is None and in_bg2["opError"] is None
    assert h["bg1"].nodes[0].name.get_value() == "blur1"


def test_node_no_match_is_selector_empty_and_missing_group_too() -> None:
    flame, _h = make_batch_world()
    assert (
        resolve({"kind": "node", "batch": BG1, "name": "zzz"}, flame)["opErrorCode"]
        == "SELECTOR_EMPTY"
    )
    missing_group = {"kind": "node", "batch": {"kind": "batch", "name": "Nope"}, "name": "blur1"}
    assert resolve(missing_group, flame)["opErrorCode"] == "SELECTOR_EMPTY"


def test_node_duplicate_name_in_one_group_is_selector_ambiguous() -> None:
    flame, h = make_batch_world()
    h["bg1"].nodes.append(PyNode("blur1", "Blur"))
    result = resolve({"kind": "node", "batch": BG1, "name": "blur1"}, flame)
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"


def test_node_is_matched_by_the_name_attribute_not_get_node() -> None:
    # get_node on a PyFlameObject can answer None for anything (catch-all): the
    # resolver must read each node's own name
    flame, h = make_batch_world()
    assert h["bg1"].get_node is None  # the trap
    assert resolve({"kind": "node", "batch": BG1, "name": "comp1"}, flame)["opError"] is None


ACTION = {"kind": "node", "batch": BG1, "name": "action1"}


def test_action_node_resolves_an_inner_node_by_name() -> None:
    flame, _h = make_batch_world()
    result = resolve({"kind": "action_node", "node": ACTION, "name": "axis1"}, flame)
    assert result["opError"] is None
    assert result["value"] == {"type": "PyCoNode", "name": "axis1"}


def test_action_node_unknown_inner_name_is_selector_empty() -> None:
    flame, _h = make_batch_world()
    result = resolve({"kind": "action_node", "node": ACTION, "name": "nope"}, flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"


def test_action_node_on_a_node_without_inner_nodes_is_selector_type() -> None:
    # PyNode.nodes answers None (catch-all) -> not iterable -> SELECTOR_TYPE
    flame, _h = make_batch_world()
    blur = {"kind": "node", "batch": BG1, "name": "blur1"}
    result = resolve({"kind": "action_node", "node": blur, "name": "axis1"}, flame)
    assert result["opErrorCode"] == "SELECTOR_TYPE"
    assert "no inner nodes" in result["opError"]


def test_action_node_duplicate_inner_names_are_selector_ambiguous() -> None:
    flame, h = make_batch_world()
    h["action"].nodes.append(h["action"].nodes[0])
    result = resolve({"kind": "action_node", "node": ACTION, "name": "axis1"}, flame)
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"


def test_path_root_desktop_reaches_a_reel_group_then_a_reel_then_a_clip() -> None:
    flame, _h = make_batch_world()
    group = resolve({"kind": "path", "path": ["RG"], "root": "desktop"}, flame)
    assert group["value"]["type"] == "PyReelGroup"
    reel = resolve({"kind": "path", "path": ["RG", "Reel 1"], "root": "desktop"}, flame)
    assert reel["value"] == {"type": "PyReel", "name": "Reel 1"}
    clip = resolve({"kind": "path", "path": ["RG", "Reel 1", "c1"], "root": "desktop"}, flame)
    assert clip["value"] == {"type": "PyClip", "name": "c1"}


def test_path_root_desktop_reaches_a_batch_group() -> None:
    flame, _h = make_batch_world()
    result = resolve({"kind": "path", "path": ["BG2"], "root": "desktop"}, flame)
    assert result["value"] == {"type": "PyBatch", "name": "BG2"}


def test_path_root_desktop_falls_back_to_reels_when_children_lacks_them() -> None:
    flame, h = make_batch_world()
    reel = PyReel("Reel 9", "u", "/wt/RG9/Reel 9", children=[PyClip("c9", "/wt/c9")])
    group = PyReelGroup("RG9", "/wt/RG9", reels=[reel], reels_in_children=False)
    assert group.children is None  # a case not settled on a real Flame
    flame.projects.current_project.current_workspace.desktop.reel_groups.append(group)
    desktop = flame.projects.current_project.current_workspace.desktop
    assert desktop.children is not None
    desktop.children.append(group)
    result = resolve({"kind": "path", "path": ["RG9", "Reel 9", "c9"], "root": "desktop"}, flame)
    assert result["opError"] is None
    assert result["value"]["name"] == "c9"
    assert h  # unused


def test_path_root_desktop_falls_back_to_the_group_accessors_when_children_is_absent() -> None:
    flame, _h = make_batch_world()
    desktop = flame.projects.current_project.current_workspace.desktop
    desktop.children = None
    assert resolve({"kind": "path", "path": ["RG"], "root": "desktop"}, flame)["opError"] is None
    assert resolve({"kind": "path", "path": ["BG1"], "root": "desktop"}, flame)["opError"] is None


def test_path_root_desktop_no_match_and_ambiguity() -> None:
    flame, h = make_batch_world()
    missing = resolve({"kind": "path", "path": ["RG", "Nope"], "root": "desktop"}, flame)
    assert missing["opErrorCode"] == "SELECTOR_EMPTY"
    assert "path step 1" in missing["opError"]
    desktop = flame.projects.current_project.current_workspace.desktop
    desktop.batch_groups.append(PyBatch("BG1", "dup"))
    assert desktop.children is not None
    desktop.children.append(desktop.batch_groups[-1])
    ambiguous = resolve({"kind": "path", "path": ["BG1"], "root": "desktop"}, flame)
    assert ambiguous["opErrorCode"] == "SELECTOR_AMBIGUOUS"
    assert h  # unused


def test_path_default_root_is_libraries_and_ignores_the_desktop() -> None:
    flame, _h = make_batch_world()
    only_on_desktop = resolve({"kind": "path", "path": ["RG"]}, flame)
    assert only_on_desktop["opErrorCode"] == "SELECTOR_EMPTY"
    in_library = resolve({"kind": "path", "path": ["P1O", "R1"]}, flame)
    assert in_library["value"] == {"type": "PyReel", "name": "R1"}


def test_desktop_list_children_accepts_a_desktop_rooted_path() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = run_op(
        registry,
        "desktop.list_children",
        {"target": {"kind": "path", "path": ["RG", "Reel 1"], "root": "desktop"}, "limit": 50},
        flame,
    )
    assert result["opError"] is None
    assert [i["name"] for i in result["value"]["items"]] == ["c1"]


def test_batch_list_reports_name_uid_and_current_without_node_count_by_default() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = run_op(registry, "batch.list", {"limit": 50}, flame)
    assert result["opError"] is None
    items = result["value"]["items"]
    assert items == [
        {"name": "BG1", "uid": "uid-b1", "isCurrent": False},
        {"name": "BG2", "uid": "uid-b2", "isCurrent": True},
    ]
    assert result["value"]["total"] == 2 and result["value"]["truncated"] is False
    assert "stoppedEarly" not in result["value"] and "nextOffset" not in result["value"]


def test_batch_list_node_counts_true_reads_each_groups_node_count() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = run_op(registry, "batch.list", {"limit": 50, "node_counts": True}, flame)
    assert result["opError"] is None
    assert [(i["name"], i["nodeCount"]) for i in result["value"]["items"]] == [
        ("BG1", 4),
        ("BG2", 1),
    ]
    assert result["value"]["truncated"] is False
    assert "stoppedEarly" not in result["value"] and "nextOffset" not in result["value"]


class _ReadTrackingBatch(PyBatch):
    """A group whose `nodes` read is observable: `on_read` runs on every read (a real Flame loads
    a non-current group on the first one)."""

    on_read: Any = None

    @property
    def nodes(self) -> list[PyNode]:
        if self.on_read is not None:
            self.on_read(self)
        return self._tracked_nodes

    @nodes.setter
    def nodes(self, value: list[PyNode]) -> None:
        self._tracked_nodes = value


def _tracked_world(count: int, on_read: Any) -> FakeFlame:
    groups = []
    for i in range(count):
        group = _ReadTrackingBatch(f"G{i}", f"u{i}", nodes=[PyNode("n", "Blur")] * (i + 1))
        group.on_read = on_read
        groups.append(group)
    return FakeFlame(desktop=PyDesktop(batch_groups=groups))


def _run_with_clock(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any], count: int, seconds_per_read: float
) -> tuple[dict[str, Any], list[str]]:
    clock = [1000.0]
    reads: list[str] = []

    def on_read(group: PyBatch) -> None:
        reads.append(group.name.get_value())
        clock[0] += seconds_per_read

    flame = _tracked_world(count, on_read)
    with monkeypatch.context() as patch:
        patch.setattr("time.monotonic", lambda: clock[0])
        result = run_op(make_registry(), "batch.list", args, flame)
    assert result["opError"] is None
    value: dict[str, Any] = result["value"]
    return value, reads


def test_batch_list_by_default_never_reads_a_groups_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, reads = _run_with_clock(monkeypatch, {"limit": 50}, count=4, seconds_per_read=3.0)
    assert reads == []
    assert [i["name"] for i in value["items"]] == ["G0", "G1", "G2", "G3"]


def test_batch_list_node_counts_stops_after_the_time_budget_and_says_where_to_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, reads = _run_with_clock(
        monkeypatch, {"limit": 50, "node_counts": True}, count=4, seconds_per_read=3.0
    )
    assert reads == ["G0", "G1"]  # 3 s after the first, 6 s (> 5 s) after the second
    assert [(i["name"], i["nodeCount"]) for i in value["items"]] == [("G0", 1), ("G1", 2)]
    assert value["total"] == 4 and value["shown"] == 2
    assert value["truncated"] is True and value["nextOffset"] == 2
    assert value["stoppedEarly"] == {"reason": "time budget", "budgetS": 5.0, "read": 2}


def test_batch_list_node_counts_resumes_from_next_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    value, reads = _run_with_clock(
        monkeypatch, {"limit": 50, "offset": 2, "node_counts": True}, count=4, seconds_per_read=3.0
    )
    assert reads == ["G2", "G3"]
    assert [i["name"] for i in value["items"]] == ["G2", "G3"]
    assert value["truncated"] is False and "stoppedEarly" not in value


def test_batch_list_node_counts_always_reads_the_first_group_however_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, reads = _run_with_clock(
        monkeypatch, {"limit": 50, "node_counts": True}, count=3, seconds_per_read=100.0
    )
    assert reads == ["G0"]
    assert [i["name"] for i in value["items"]] == ["G0"]
    assert value["nextOffset"] == 1
    assert value["stoppedEarly"] == {"reason": "time budget", "budgetS": 5.0, "read": 1}


def test_batch_list_over_budget_on_the_last_group_of_a_page_is_not_a_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, reads = _run_with_clock(
        monkeypatch, {"limit": 2, "node_counts": True}, count=4, seconds_per_read=3.0
    )
    assert reads == ["G0", "G1"]
    assert value["truncated"] is True  # more groups follow this page, but nothing was skipped
    assert "stoppedEarly" not in value and "nextOffset" not in value


def test_batch_list_pages_with_offset_and_limit() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = run_op(registry, "batch.list", {"limit": 1, "offset": 1}, flame)
    assert [i["name"] for i in result["value"]["items"]] == ["BG2"]
    first = run_op(registry, "batch.list", {"limit": 1}, flame)
    assert first["value"]["truncated"] is True


def test_batch_list_without_a_current_group_or_any_group() -> None:
    registry = make_registry()
    flame = FakeFlame(desktop=PyDesktop(batch_groups=[PyBatch("Only", "u1")]))
    result = run_op(registry, "batch.list", {"limit": 50}, flame)
    assert result["value"]["items"][0]["isCurrent"] is False
    empty = run_op(registry, "batch.list", {"limit": 50}, FakeFlame())
    assert empty["value"]["items"] == [] and empty["value"]["total"] == 0


def test_node_list_reports_name_type_position_and_bypass() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    h["blur"].pos_x = FakeAttr(-288.0)
    h["blur"].pos_y = FakeAttr(12)
    result = run_op(registry, "node.list", {"target": BG1, "limit": 50}, flame)
    assert result["opError"] is None
    items = {i["name"]: i for i in result["value"]["items"]}
    assert set(items) == {"blur1", "comp1", "exposure1", "action1"}
    assert items["blur1"] == {
        "name": "blur1",
        "pyType": "PyNode",
        "type": "Blur",
        "posX": -288.0,
        "posY": 12,
        "pos": [-288, 12],
        "sameNameCount": 1,
        "bypass": False,
    }
    assert items["action1"]["pyType"] == "PyActionNode"


def test_node_list_paging_and_missing_group() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    page = run_op(registry, "node.list", {"target": BG1, "limit": 2, "offset": 3}, flame)
    assert page["value"]["shown"] == 1 and page["value"]["total"] == 4
    missing = run_op(
        registry, "node.list", {"target": {"kind": "batch", "name": "Nope"}, "limit": 50}, flame
    )
    assert missing["opErrorCode"] == "SELECTOR_EMPTY"


def test_node_list_tolerates_a_node_whose_attribute_cannot_be_read() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    h["blur"].pos_x = None  # not a PyAttribute: _try answers None instead of failing the list
    result = run_op(registry, "node.list", {"target": BG1, "limit": 50}, flame)
    assert result["opError"] is None
    blur = next(i for i in result["value"]["items"] if i["name"] == "blur1")
    assert blur["posX"] is None or blur["posX"] == "None"


def test_node_ops_do_not_touch_the_current_group() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    desktop = flame.projects.current_project.current_workspace.desktop
    current_before = desktop.current_batch_group.get_value()
    run_op(registry, "node.list", {"target": BG1, "limit": 50}, flame)
    run_op(
        registry,
        "node.set_attributes",
        {"target": {"kind": "node", "batch": BG1, "name": "comp1"}, "values": {"bypass": True}},
        flame,
    )
    assert desktop.current_batch_group.get_value() is current_before


def test_fake_desktop_models_current_batch_group_as_a_pyattribute_holding_the_batch() -> None:
    flame, h = make_batch_world()
    current = flame.projects.current_project.current_workspace.desktop.current_batch_group
    assert type(current).__name__ == "PyAttribute"
    assert current.get_value() is h["bg2"]
    assert not hasattr(current, "uid")  # the real PyAttribute has no .uid either


def test_batch_list_current_flag_comes_from_the_unwrapped_batch() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = run_op(registry, "batch.list", {"limit": 50}, flame)
    assert result["value"]["currentError"] is None
    assert {i["name"]: i["isCurrent"] for i in result["value"]["items"]} == {
        "BG1": False,
        "BG2": True,
    }


class _UnreadableCurrent(FakeAttr):
    def get_value(self) -> Any:
        raise RuntimeError("no current group readable")


_UnreadableCurrent.__name__ = "PyAttribute"


def test_batch_list_reports_why_current_could_not_be_read_instead_of_hiding_it() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    desktop = flame.projects.current_project.current_workspace.desktop
    desktop.current_batch_group = _UnreadableCurrent(None)
    result = run_op(registry, "batch.list", {"limit": 50}, flame)
    assert result["opError"] is None
    assert "RuntimeError: no current group readable" in result["value"]["currentError"]
    assert [i["isCurrent"] for i in result["value"]["items"]] == [False, False]
    assert result["value"]["total"] == 2  # the listing itself still works


def test_batch_list_with_no_current_group_has_no_error() -> None:
    registry = make_registry()
    flame = FakeFlame(desktop=PyDesktop(batch_groups=[PyBatch("Only", "u1")]))
    result = run_op(registry, "batch.list", {"limit": 50}, flame)
    assert result["value"]["currentError"] is None
    assert result["value"]["items"][0]["isCurrent"] is False
