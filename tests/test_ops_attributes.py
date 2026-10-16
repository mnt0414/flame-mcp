"""Tests for the generic attribute ops (`*.get_attributes` / `*.set_attributes`).

Run the generated code against the fake tree in `fake_media_tree` (no bridge).

The failure modes the doubles below stand in for (measured on Flame 2025.2.4): an enumerated
attribute whose invalid value would raise `RuntimeError('This is not an option')`
(checked BEFORE the write), a numeric attribute with a `(min, max)` range, a write
that raises nothing but does not stick (PySegment.dynamic_*), and a PyAttribute
wrapper that is stale after a write (so the read-back must go through a freshly
fetched `getattr(obj, key)`)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from fake_media_tree import (
    FakeAttr,
    FakeFlame,
    PyActionNode,
    PyBatch,
    PyClip,
    PyCoNode,
    PyDesktop,
    PyNode,
    PyReel,
    PyReelGroup,
    PySegment,
    build_default_tree,
)
from flame_mcp import envelope as envelope_mod
from flame_mcp.ops import register_all
from flame_mcp.ops._attributes import register_attribute_ops
from flame_mcp.opschema import validate_op_args
from flame_mcp.registry import RegistrationError, Registry
from flame_mcp.transport import BridgeResult
from flame_mcp.types import Category
from op_test_helpers import run_op

BLEND = ("Flame", "Photoshop", "Gimp")


class SpyAttr(FakeAttr):
    """Records every set_value call (a rejected value must never reach it)."""

    def __init__(self, value: Any, values: tuple[Any, ...] | None = None) -> None:
        super().__init__(value, values)
        self.set_calls: list[Any] = []

    def set_value(self, value: Any) -> None:
        self.set_calls.append(value)
        super().set_value(value)


class NoopAttr(SpyAttr):
    def set_value(self, value: Any) -> None:
        self.set_calls.append(value)  # accepted, ignored: no exception, no effect


class ClampAttr(SpyAttr):
    """Stores a rounded value, like an attribute that quantises on write."""

    def __init__(self, value: Any, values: tuple[Any, ...] | None, ndigits: int) -> None:
        super().__init__(value, values)
        self._ndigits = ndigits

    def set_value(self, value: Any) -> None:
        self.set_calls.append(value)
        self._value = round(value, self._ndigits)


class RaisingAttr(SpyAttr):
    def set_value(self, value: Any) -> None:
        self.set_calls.append(value)
        raise RuntimeError("This is not an option")


for _cls in (SpyAttr, NoopAttr, ClampAttr, RaisingAttr):
    _cls.__name__ = "PyAttribute"


class _View:
    """One freshly fetched wrapper over a shared store."""

    def __init__(self, store: dict[str, Any], mode: str) -> None:
        self._store = store
        self._mode = mode
        self._snapshot = store["v"]
        self._local: Any = None
        self._local_set = False
        self.values = None

    def get_value(self) -> Any:
        if self._mode == "stale-after-write":  # real write, but this wrapper never refreshes
            return self._snapshot
        if self._local_set:  # "phantom": this wrapper claims the write, the store never got it
            return self._local
        return self._store["v"]

    def set_value(self, value: Any) -> None:
        if self._mode == "stale-after-write":
            self._store["v"] = value
        else:
            self._local, self._local_set = value, True


_View.__name__ = "PyAttribute"


class _FreshViewNode(PyNode):
    """`getattr(node, 'dynamic_name')` builds a NEW wrapper every time, like Flame."""

    def __init__(self, mode: str) -> None:
        super().__init__("comp1", "Comp")
        self._store = {"v": "before"}
        self._mode = mode
        self.attributes.append("dynamic_name")

    @property
    def dynamic_name(self) -> _View:
        return _View(self._store, self._mode)


class _FakePyTime:
    def __init__(self, timecode: str, frame: int) -> None:
        self.timecode = timecode
        self.frame = frame


_FakePyTime.__name__ = "PyTime"


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


def call(registry: Registry, op: str, args: dict[str, Any], flame: Any) -> dict[str, Any]:
    """Validate like flame_do does, then run: the arguments must be a legal call."""
    result = validate_op_args(registry.get(op), args)  # type: ignore[arg-type]
    assert result.ok, result.issues
    return run_op(registry, op, args, flame)


def add_attr(obj: Any, key: str, attr: FakeAttr) -> None:
    setattr(obj, key, attr)
    obj.attributes.append(key)


def set_call(
    registry: Registry, op: str, target: dict[str, Any], values: dict[str, Any], flame: Any
) -> dict[str, Any]:
    return call(registry, op, {"target": target, "values": values}, flame)


def to_envelope(registry: Registry, op_name: str, args: dict[str, Any], raw: dict[str, Any]) -> Any:
    op = registry.get(op_name)
    assert op is not None
    br = BridgeResult(
        ok=True,
        result=raw,
        error_code=None,
        error=None,
        traceback=None,
        queued_ms=0,
        run_ms=0,
        timeout_phase=None,
        pending_handle=None,
        app_log=(),
        app_log_note=None,
        crash=None,
        inflight_path=None,
        duration_ms=1,
    )
    return envelope_mod.from_bridge_result(op, args, br)


def make_batch_world() -> tuple[FakeFlame, dict[str, Any]]:
    comp = PyNode("comp1", "Comp", extra={"blend_type": SpyAttr("Flame", BLEND)})
    comp.pos_x = SpyAttr(0.0, (-100000.0, 100000.0))
    expo = PyNode(
        "exposure1", "Exposure", extra={"exposure_red": SpyAttr(0.0, (-10000.0, 10000.0))}
    )
    blur = PyNode("blur1", "Blur")
    action = PyActionNode("action1", [PyCoNode("axis1"), PyCoNode("Default")])
    bg1 = PyBatch("BG1", "uid-b1", nodes=[blur, comp, expo, action])
    bg2 = PyBatch("BG2", "uid-b2", nodes=[PyNode("blur1", "Blur")])
    reel = PyReel("Reel 1", "uid-reel", "/wt/RG/Reel 1", children=[PyClip("c1", "/wt/c1")])
    reel_group = PyReelGroup("RG", "/wt/RG", reels=[reel])
    desktop = PyDesktop(batch_groups=[bg1, bg2], reel_groups=[reel_group], current_batch_group=bg2)
    flame, _handles = build_default_tree()
    flame.projects.current_project.current_workspace.desktop = desktop
    return flame, {
        "bg1": bg1,
        "bg2": bg2,
        "comp": comp,
        "expo": expo,
        "blur": blur,
        "action": action,
        "reel": reel,
        "reel_group": reel_group,
    }


BG1 = {"kind": "batch", "name": "BG1"}
COMP = {"kind": "node", "batch": BG1, "name": "comp1"}
EXPO = {"kind": "node", "batch": BG1, "name": "exposure1"}


def test_get_attributes_returns_every_listed_attribute() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = call(registry, "node.get_attributes", {"target": COMP}, flame)
    assert result["opError"] is None
    value = result["value"]
    assert value["type"] == "PyNode"
    assert set(value["attributes"]) == {"name", "type", "pos_x", "pos_y", "bypass", "blend_type"}
    assert value["attributes"]["blend_type"] == {"value": "Flame"}
    assert value["attributes"]["bypass"] == {"value": False}


def test_get_attributes_include_values_adds_candidates_and_ranges() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = call(
        registry,
        "node.get_attributes",
        {"target": COMP, "keys": ["blend_type", "pos_x", "name"], "include_values": True},
        flame,
    )
    attrs = result["value"]["attributes"]
    assert attrs["blend_type"] == {"value": "Flame", "values": ["Flame", "Photoshop", "Gimp"]}
    assert attrs["pos_x"]["values"] == [-100000.0, 100000.0]
    assert attrs["name"] == {"value": "comp1", "values": None}


def test_get_attributes_unknown_key_is_a_per_key_error_not_a_failure() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = call(
        registry, "node.get_attributes", {"target": COMP, "keys": ["blend_type", "nope"]}, flame
    )
    assert result["opError"] is None
    assert result["value"]["attributes"]["blend_type"] == {"value": "Flame"}
    assert "error" in result["value"]["attributes"]["nope"]


def test_get_attributes_tuple_and_pytime_values_are_json_safe() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "colour", FakeAttr((0.1, 0.2, 0.3)))
    add_attr(h["comp"], "start", FakeAttr(_FakePyTime("00:00:01+00", 24)))
    result = call(
        registry, "node.get_attributes", {"target": COMP, "keys": ["colour", "start"]}, flame
    )
    attrs = result["value"]["attributes"]
    assert attrs["colour"] == {"value": [0.1, 0.2, 0.3]}
    assert attrs["start"] == {"value": {"timecode": "00:00:01+00", "frame": 24}}


def test_set_enumerated_value_reads_back_and_revert_restores_it() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(registry, "node.set_attributes", COMP, {"blend_type": "Photoshop"}, flame)
    assert result["opError"] is None
    value = result["value"]
    assert value["before"] == {"blend_type": "Flame"}
    assert value["after"] == {"blend_type": "Photoshop"}
    assert value["applied"] == ["blend_type"]
    assert value["failed"] == {}
    assert h["comp"].blend_type.get_value() == "Photoshop"
    assert result["partial"] is None
    assert result["affected"] == 1
    assert value["revert"] == {
        "op": "node.set_attributes",
        "args": {
            "target": {
                "kind": "node",
                "batch": {"kind": "batch", "uid": "uid-b1"},
                "name": "comp1",
            },
            "values": {"blend_type": "Flame"},
        },
    }

    back = call(registry, value["revert"]["op"], value["revert"]["args"], flame)
    assert back["opError"] is None
    assert h["comp"].blend_type.get_value() == "Flame"


def test_set_numeric_value_within_range_and_tolerance() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["expo"], "gain", ClampAttr(0.0, (-10.0, 10.0), ndigits=9))
    result = set_call(registry, "node.set_attributes", EXPO, {"gain": 0.1234567891}, flame)
    assert result["opError"] is None, result["opError"]
    assert result["value"]["applied"] == ["gain"]


def test_set_multiple_keys_writes_in_order_and_reports_each() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(
        registry,
        "node.set_attributes",
        COMP,
        {"blend_type": "Gimp", "pos_x": 100.0, "bypass": True},
        flame,
    )
    assert result["opError"] is None
    assert result["value"]["applied"] == ["blend_type", "pos_x", "bypass"]
    assert h["comp"].pos_x.get_value() == 100.0
    assert h["comp"].bypass.get_value() is True
    assert result["value"]["revert"]["args"]["values"] == {
        "blend_type": "Flame",
        "pos_x": 0.0,
        "bypass": False,
    }


def test_set_to_the_current_value_succeeds_with_no_revert() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = set_call(registry, "node.set_attributes", COMP, {"blend_type": "Flame"}, flame)
    assert result["opError"] is None
    assert result["value"]["applied"] == ["blend_type"]
    assert result["value"]["revert"] is None


def test_set_tuple_attribute_accepts_a_list_and_reverts_as_a_list() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "colour", SpyAttr((0.1, 0.2, 0.3)))
    result = set_call(registry, "node.set_attributes", COMP, {"colour": [0.5, 0.6, 0.7]}, flame)
    assert result["opError"] is None, result["opError"]
    assert h["comp"].colour.get_value() == (0.5, 0.6, 0.7)
    assert h["comp"].colour.set_calls == [(0.5, 0.6, 0.7)]  # converted to a tuple
    assert result["value"]["revert"]["args"]["values"] == {"colour": [0.1, 0.2, 0.3]}
    back = call(registry, "node.set_attributes", result["value"]["revert"]["args"], flame)
    assert back["opError"] is None
    assert h["comp"].colour.get_value() == (0.1, 0.2, 0.3)


def test_value_outside_the_enumeration_is_rejected_and_never_set() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(registry, "node.set_attributes", COMP, {"blend_type": "NoSuchMode"}, flame)
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert h["comp"].blend_type.set_calls == []
    assert h["comp"].blend_type.get_value() == "Flame"
    failed = result["value"]["failed"]
    assert "not one of" in failed["blend_type"] and failed["blend_type"].startswith("rejected")
    assert result["value"]["applied"] == []
    assert result["value"]["revert"] is None
    assert result["partial"] == {"applied": 0, "failed": 1}
    assert result["affected"] == 0


@pytest.mark.parametrize("bad", [10000.5, -10001.0])
def test_numeric_value_outside_the_range_is_rejected_and_never_set(bad: float) -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(registry, "node.set_attributes", EXPO, {"exposure_red": bad}, flame)
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert h["expo"].exposure_red.set_calls == []
    assert "outside the range" in result["value"]["failed"]["exposure_red"]


def test_range_boundaries_are_inclusive() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    for edge in (10000.0, -10000.0):
        result = set_call(registry, "node.set_attributes", EXPO, {"exposure_red": edge}, flame)
        assert result["opError"] is None, edge


def test_unknown_attribute_is_rejected() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = set_call(registry, "node.set_attributes", COMP, {"not_an_attribute": 1}, flame)
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert "no such attribute" in result["value"]["failed"]["not_an_attribute"]


def test_type_mismatch_is_rejected_and_never_set() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "colour", SpyAttr((0.1, 0.2, 0.3)))
    for key, bad in (("pos_x", "wide"), ("bypass", 1), ("blend_type", 3), ("colour", 0.5)):
        result = set_call(registry, "node.set_attributes", COMP, {key: bad}, flame)
        assert result["opErrorCode"] == "INVALID_ARGS", key
        assert "expected a value shaped like" in result["value"]["failed"][key], key
    assert h["comp"].pos_x.set_calls == []


def test_a_bool_is_not_a_number() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(registry, "node.set_attributes", COMP, {"pos_x": True}, flame)
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert h["comp"].pos_x.set_calls == []


def test_pytime_attribute_is_rejected_for_now() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "start", SpyAttr(_FakePyTime("00:00:01+00", 24)))
    result = set_call(registry, "node.set_attributes", COMP, {"start": "00:00:02+00"}, flame)
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert "PyTime" in result["value"]["failed"]["start"]
    assert h["comp"].start.set_calls == []


def test_empty_values_is_invalid_args() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    result = set_call(registry, "node.set_attributes", COMP, {}, flame)
    assert result["opErrorCode"] == "INVALID_ARGS"


def test_any_rejected_key_means_nothing_is_written() -> None:
    # Validation finishes before any key is written: if a later key is invalid, nothing is
    # written, even when the first key is valid.
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(
        registry,
        "node.set_attributes",
        COMP,
        {"blend_type": "Gimp", "bogus": 1, "pos_x": 5.0},
        flame,
    )
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert h["comp"].blend_type.set_calls == []
    assert h["comp"].pos_x.set_calls == []
    assert set(result["value"]["failed"]) == {"bogus"}
    assert result["value"]["applied"] == []
    assert result["partial"] == {"applied": 0, "failed": 1}


def test_silent_noop_is_detected_by_reading_back() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "dynamic_name", NoopAttr("x"))
    result = set_call(registry, "node.set_attributes", COMP, {"dynamic_name": "y"}, flame)
    assert result["opErrorCode"] == "SILENT_NOOP"
    assert result["value"]["failed"]["dynamic_name"].startswith("silent_noop")
    assert result["value"]["applied"] == []
    assert result["value"]["after"] == {"dynamic_name": "x"}
    assert result["value"]["revert"] is None  # nothing changed
    assert h["comp"].dynamic_name.set_calls == ["y"]  # it was attempted


def _swap_in_fresh_view_node(h: dict[str, Any], mode: str) -> None:
    h["bg1"].nodes[1] = _FreshViewNode(mode)


def test_read_back_goes_through_a_freshly_fetched_attribute() -> None:
    # "phantom": the wrapper that was written claims the new value, the store never
    # changed -- a read-back through that same wrapper would say OK
    registry = make_registry()
    flame, h = make_batch_world()
    _swap_in_fresh_view_node(h, "phantom")
    result = set_call(registry, "node.set_attributes", COMP, {"dynamic_name": "after"}, flame)
    assert result["opErrorCode"] == "SILENT_NOOP"
    assert result["value"]["after"] == {"dynamic_name": "before"}


def test_a_stale_wrapper_does_not_cause_a_false_failure() -> None:
    # the write really landed, but the wrapper we wrote through never refreshes
    registry = make_registry()
    flame, h = make_batch_world()
    _swap_in_fresh_view_node(h, "stale-after-write")
    result = set_call(registry, "node.set_attributes", COMP, {"dynamic_name": "after"}, flame)
    assert result["opError"] is None, result["opError"]
    assert result["value"]["applied"] == ["dynamic_name"]
    assert result["value"]["after"] == {"dynamic_name": "after"}


def test_a_value_clamped_by_flame_counts_as_failed_and_stays_revertable() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "gain", ClampAttr(1.0, None, ndigits=1))
    result = set_call(registry, "node.set_attributes", COMP, {"gain": 2.26}, flame)
    assert result["opErrorCode"] == "SILENT_NOOP"
    assert result["value"]["after"] == {"gain": 2.3}
    # the value did change (to something else): the revert must still put it back
    assert result["value"]["revert"]["args"]["values"] == {"gain": 1.0}


def test_failure_in_the_middle_stops_and_leaves_later_keys_unwritten() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "dynamic_name", NoopAttr("x"))
    result = set_call(
        registry,
        "node.set_attributes",
        COMP,
        {"blend_type": "Gimp", "dynamic_name": "y", "pos_x": 9.0},
        flame,
    )
    assert result["opErrorCode"] == "SILENT_NOOP"
    value = result["value"]
    assert value["applied"] == ["blend_type"]
    assert set(value["failed"]) == {"dynamic_name"}
    assert h["comp"].blend_type.get_value() == "Gimp"  # the earlier write stays
    assert h["comp"].pos_x.get_value() == 0.0  # the later one was never attempted
    assert result["partial"] == {"applied": 1, "failed": 1}
    assert result["affected"] == 1
    assert value["revert"]["args"]["values"] == {"blend_type": "Flame"}  # only what changed


def test_set_value_raising_is_an_error_that_also_stops() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "mode", RaisingAttr("a"))
    result = set_call(
        registry, "node.set_attributes", COMP, {"mode": "b", "blend_type": "Gimp"}, flame
    )
    assert result["opErrorCode"] == "FLAME_API_ERROR"
    assert result["value"]["failed"]["mode"].startswith("error: RuntimeError")
    assert h["comp"].blend_type.set_calls == []


def test_a_partial_failure_reaches_the_envelope_with_result_and_partial() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    add_attr(h["comp"], "dynamic_name", NoopAttr("x"))
    args = {"target": COMP, "values": {"blend_type": "Gimp", "dynamic_name": "y"}}
    raw = call(registry, "node.set_attributes", args, flame)
    env = to_envelope(registry, "node.set_attributes", args, raw)
    assert env["ok"] is False
    assert env["errorCode"] == "SILENT_NOOP"
    assert env["partial"] == {"applied": 1, "failed": 1}
    assert env["affected"] == 1
    assert env["undoSteps"] == 0
    assert env["result"]["applied"] == ["blend_type"]
    assert env["result"]["revert"]["op"] == "node.set_attributes"


def test_a_clean_success_reaches_the_envelope_without_partial() -> None:
    registry = make_registry()
    flame, _h = make_batch_world()
    args = {"target": COMP, "values": {"blend_type": "Gimp"}}
    env = to_envelope(
        registry, "node.set_attributes", args, call(registry, "node.set_attributes", args, flame)
    )
    assert env["ok"] is True
    assert env["partial"] is None
    assert env["result"]["applied"] == ["blend_type"]


def test_node_revert_addresses_the_node_by_its_new_name() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(registry, "node.set_attributes", COMP, {"name": "comp_renamed"}, flame)
    assert result["opError"] is None
    revert = result["value"]["revert"]
    assert revert["args"]["target"]["name"] == "comp_renamed"
    assert revert["args"]["values"] == {"name": "comp1"}
    back = call(registry, revert["op"], revert["args"], flame)
    assert back["opError"] is None
    assert h["comp"].name.get_value() == "comp1"


def test_batch_set_attributes_rename_and_revert_by_uid() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(registry, "batch.set_attributes", BG1, {"name": "BG1_new"}, flame)
    assert result["opError"] is None
    revert = result["value"]["revert"]
    assert revert["args"]["target"] == {"kind": "batch", "uid": "uid-b1"}
    back = call(registry, revert["op"], revert["args"], flame)
    assert back["opError"] is None
    assert h["bg1"].name.get_value() == "BG1"


def test_desktop_set_attributes_reaches_a_library_entry_and_reverts_by_wiretap() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    target = {"kind": "path", "path": ["P1O", "R1"]}
    result = set_call(registry, "desktop.set_attributes", target, {"name": "R1_new"}, flame)
    assert result["opError"] is None
    assert h["reel"].name.get_value() == "R1_new"
    revert = result["value"]["revert"]
    assert revert["op"] == "desktop.set_attributes"
    assert revert["args"]["target"] == {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1"}
    back = call(registry, revert["op"], revert["args"], flame)
    assert back["opError"] is None
    assert h["reel"].name.get_value() == "R1"


def test_desktop_set_attributes_on_a_batch_group_reverts_by_batch_uid() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    target = {"kind": "path", "path": ["BG1"], "root": "desktop"}
    result = set_call(registry, "desktop.set_attributes", target, {"name": "BG1_x"}, flame)
    assert result["opError"] is None
    assert result["value"]["type"] == "PyBatch"
    revert = result["value"]["revert"]
    assert revert["args"]["target"] == {"kind": "batch", "uid": "uid-b1"}
    # the revert is a legal call of desktop.set_attributes itself (call() validates it) ...
    back = call(registry, revert["op"], revert["args"], flame)
    assert back["opError"] is None
    assert h["bg1"].name.get_value() == "BG1"
    # ... and desktop.get_attributes can read the group back through the same selector
    read = call(registry, "desktop.get_attributes", {"target": revert["args"]["target"]}, flame)
    assert attr_of(read, "name") == "BG1"


def attr_of(env: dict[str, Any], key: str) -> Any:
    return env["value"]["attributes"][key]["value"]


def test_desktop_revert_is_none_when_no_wiretap_id_can_be_obtained() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    entry = PyClip("solo", wiretap_id="")  # empty id: not a usable address
    flame.media_panel.selected_entries = [entry]
    result = set_call(
        registry, "desktop.set_attributes", {"kind": "selection"}, {"name": "n"}, flame
    )
    assert result["opError"] is None
    assert h  # unused handles
    assert result["value"]["revert"] is None
    assert result["value"]["after"] == {"name": "n"}


def _segment_world() -> tuple[FakeFlame, dict[str, Any], dict[str, Any]]:
    flame, h = build_default_tree()
    seg = h["seg1"]
    add_attr_list(seg, "shot_name", SpyAttr(""))
    add_attr_list(seg, "comment", SpyAttr("old"))
    add_attr_list(seg, "dynamic_name", NoopAttr("d"))
    selector = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": h["seq"].get_wiretap_node_id()},
        "version": 0,
        "track": 0,
        "index": 1,
    }
    return flame, h, selector


def add_attr_list(obj: PySegment, key: str, attr: FakeAttr) -> None:
    setattr(obj, key, attr)
    obj.attributes.append(key)


def test_segment_set_attributes_and_revert_selector_uses_the_sequence_wiretap() -> None:
    registry = make_registry()
    flame, h, selector = _segment_world()
    result = set_call(
        registry,
        "segment.set_attributes",
        selector,
        {"shot_name": "SH010", "comment": "checked"},
        flame,
    )
    assert result["opError"] is None, result["opError"]
    assert h["seg1"].shot_name.get_value() == "SH010"
    revert = result["value"]["revert"]
    assert revert["args"]["target"] == selector
    assert revert["args"]["values"] == {"shot_name": "", "comment": "old"}
    back = call(registry, revert["op"], revert["args"], flame)
    assert back["opError"] is None
    assert h["seg1"].shot_name.get_value() == ""
    assert h["seg1"].comment.get_value() == "old"


def test_segment_dynamic_attribute_is_a_silent_noop_failure() -> None:
    registry = make_registry()
    flame, h, selector = _segment_world()
    result = set_call(registry, "segment.set_attributes", selector, {"dynamic_name": "x"}, flame)
    assert result["opErrorCode"] == "SILENT_NOOP"
    assert h["seg1"].dynamic_name.get_value() == "d"


def test_segment_get_attributes_lists_the_attributes() -> None:
    registry = make_registry()
    flame, _h, selector = _segment_world()
    result = call(registry, "segment.get_attributes", {"target": selector}, flame)
    attrs = result["value"]["attributes"]
    assert {"name", "shot_name", "comment", "dynamic_name"} <= set(attrs)
    assert "type" not in attrs  # PySegment.type is a plain str, not an attribute key
    assert attrs["comment"] == {"value": "old"}


@pytest.mark.parametrize("category", ["desktop", "segment", "batch", "node"])
def test_set_attributes_is_a_reversible_value_undo_zero_op(category: str) -> None:
    registry = make_registry()
    op = registry.get(f"{category}.set_attributes")
    assert op is not None
    assert (op.effect, op.undo, op.thread, op.pacing) == ("reversible", "value", "main", "none")
    assert op.undo_steps({}, 5) == 0
    assert "set_value" in op.description and "revert" in op.description
    read = registry.get(f"{category}.get_attributes")
    assert read is not None and read.effect == "read"


def test_batch_and_node_categories_do_not_require_current() -> None:
    registry = make_registry()
    cats = registry.categories()
    for name in ("batch", "node"):
        assert cats[name].preconditions == ()
        assert (cats[name].default_thread, cats[name].default_pacing) == ("main", "none")


def test_set_attributes_rejects_unknown_params_and_wrong_selector_kinds() -> None:
    registry = make_registry()
    node_set = registry.get("node.set_attributes")
    assert node_set is not None
    ok = validate_op_args(node_set, {"target": COMP, "values": {"a": 1}})
    assert ok.ok
    wrong = validate_op_args(node_set, {"target": BG1, "values": {"a": 1}})
    assert not wrong.ok and any("not accepted" in i.message for i in wrong.issues)
    missing = validate_op_args(node_set, {"target": COMP})
    assert not missing.ok
    not_object = validate_op_args(node_set, {"target": COMP, "values": ["a"]})
    assert not not_object.ok


def test_action_node_selector_is_accepted_only_by_the_action_category() -> None:
    registry = make_registry()
    accepting = sorted(op.name for op in registry.list_ops() if "action_node" in op.accepts)
    assert accepting == ["action.connect", "action.get_attributes", "action.set_attributes"]


def test_every_revert_selector_is_accepted_by_the_op_that_returned_it() -> None:
    registry = make_registry()
    # a fresh world per case: several of them rename the same object
    cases: list[tuple[str, dict[str, Any], str, Any]] = [
        (
            "desktop.set_attributes",
            {"kind": "path", "path": ["P1O", "R1"]},
            "name",
            build_default_tree()[0],
        ),
        (
            "desktop.set_attributes",
            {"kind": "path", "path": ["BG1"], "root": "desktop"},
            "name",
            make_batch_world()[0],
        ),
        ("batch.set_attributes", BG1, "name", make_batch_world()[0]),
        ("node.set_attributes", COMP, "name", make_batch_world()[0]),
        ("segment.set_attributes", {}, "comment", None),
    ]
    seen_kinds: set[tuple[str, str]] = set()
    for op_name, target, key, world in cases:
        if world is None:  # the segment case needs its own selector's world
            world, _h, target = _segment_world()
        result = set_call(registry, op_name, target, {key: "changed_by_test"}, world)
        assert result["opError"] is None, (op_name, result["opError"])
        revert = result["value"]["revert"]
        assert revert is not None, op_name
        op = registry.get(revert["op"])
        assert op is not None
        validation = validate_op_args(op, revert["args"])
        assert validation.ok, (op_name, validation.issues)
        assert revert["args"]["target"]["kind"] in op.accepts
        seen_kinds.add((op_name, revert["args"]["target"]["kind"]))
    assert seen_kinds == {
        ("desktop.set_attributes", "wiretap"),
        ("desktop.set_attributes", "batch"),
        ("batch.set_attributes", "batch"),
        ("node.set_attributes", "node"),
        ("segment.set_attributes", "segment"),
    }


def test_registering_attribute_ops_whose_revert_kind_is_not_accepted_is_refused() -> None:
    registry = Registry()
    registry.register_category(
        Category(
            name="probe",
            description="p",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    with pytest.raises(RegistrationError, match="revert selector kind"):
        register_attribute_ops(
            registry,
            category="probe",
            noun="thing",
            accepts=("wiretap",),
            target_description="t",
            stable_src=lambda _args: "_stable = None",
            revert_kinds=("wiretap", "batch"),
            requires=(),
        )


def test_desktop_attribute_ops_accept_a_batch_selector() -> None:
    registry = make_registry()
    for name in ("desktop.get_attributes", "desktop.set_attributes"):
        op = registry.get(name)
        assert op is not None and "batch" in op.accepts, name
    # A journaled write goes through the target's STABLE address, and a Batch group's
    # is a 'batch' selector -- so desktop.rename accepts it too.
    other = registry.get("desktop.rename")
    assert other is not None and "batch" in other.accepts
    listing = registry.get("desktop.list_children")
    assert listing is not None and "batch" not in listing.accepts  # the read ops do not widen


class _GuardedName(SpyAttr):
    """A node's `name` that behaves like Flame's set_value.

    The duplicate
    check runs against the nodes of the CURRENT Batch group, whatever group the node
    is in -- and a duplicate inside a non-current group is silently accepted.
    """

    def __init__(self, value: str, desktop: PyDesktop) -> None:
        super().__init__(value)
        self._desktop = desktop

    def set_value(self, value: Any) -> None:
        self.set_calls.append(value)
        current = self._desktop.current_batch_group.get_value()
        if value in [n.name.get_value() for n in current.nodes]:
            raise RuntimeError("Could not set Batch node name, name is already being used.")
        self._value = value


_GuardedName.__name__ = "PyAttribute"


def make_guarded_world() -> tuple[FakeFlame, dict[str, Any]]:
    """BG1 (not current): comp1 / blur1 / exposure1 / action1.  BG2 (current): blur1 / onlyB."""
    flame, h = make_batch_world()
    desktop = flame.projects.current_project.current_workspace.desktop
    h["bg2"].nodes.append(PyNode("onlyB", "Mux"))
    for group in (h["bg1"], h["bg2"]):
        for node in group.nodes:
            node.name = _GuardedName(node.name.get_value(), desktop)
    return flame, h


def node_names(group: Any) -> list[str]:
    return [n.name.get_value() for n in group.nodes]


def test_renaming_to_another_nodes_name_in_the_same_group_is_refused_before_any_write() -> None:
    registry = make_registry()
    flame, h = make_guarded_world()
    # BG1 is not current and BG2 has no "exposure1": without the check Flame would
    # silently make two of them
    result = set_call(
        registry,
        "node.set_attributes",
        COMP,
        {"pos_x": 5.0, "name": "exposure1", "bypass": True},
        flame,
    )
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert set(result["value"]["failed"]) == {"name"}
    assert "already named 'exposure1'" in result["value"]["failed"]["name"]
    assert result["value"]["applied"] == []
    assert node_names(h["bg1"]) == ["blur1", "comp1", "exposure1", "action1"]  # nothing written
    assert h["comp"].pos_x.set_calls == [] and h["comp"].name.set_calls == []
    assert result["partial"] == {"applied": 0, "failed": 1}


def test_without_the_group_check_a_same_group_duplicate_would_be_written_silently() -> None:
    # documents what the check prevents: the guarded fake (Flame's behaviour) accepts it
    _flame, h = make_guarded_world()
    h["comp"].name.set_value("exposure1")
    assert node_names(h["bg1"]).count("exposure1") == 2


def test_the_group_check_counts_the_target_itself_out() -> None:
    registry = make_registry()
    flame, h = make_guarded_world()
    same = set_call(registry, "node.set_attributes", COMP, {"name": "comp1"}, flame)
    assert same["opError"] is None  # its own name is not a duplicate
    fresh = set_call(registry, "node.set_attributes", COMP, {"name": "comp_fresh"}, flame)
    assert fresh["opError"] is None
    assert node_names(h["bg1"])[1] == "comp_fresh"
    back = call(registry, fresh["value"]["revert"]["op"], fresh["value"]["revert"]["args"], flame)
    assert back["opError"] is None and node_names(h["bg1"])[1] == "comp1"


def test_a_name_that_only_exists_in_another_non_current_group_is_not_refused_here() -> None:
    registry = make_registry()
    flame, h = make_guarded_world()
    h["bg1"].nodes.append(PyNode("only_elsewhere", "Mux"))  # another node of BG1, not BG2
    result = set_call(
        registry,
        "node.set_attributes",
        {"kind": "node", "batch": {"kind": "batch", "name": "BG2"}, "name": "onlyB"},
        {"name": "comp1"},  # comp1 exists in BG1 only; BG2 is the current group
        flame,
    )
    assert result["opError"] is None
    assert "comp1" in node_names(h["bg2"])


def test_flames_current_group_conflict_is_reported_with_an_explanation() -> None:
    registry = make_registry()
    flame, h = make_guarded_world()
    result = set_call(registry, "node.set_attributes", COMP, {"name": "onlyB"}, flame)
    assert result["opErrorCode"] == "FLAME_API_ERROR"
    reason = result["value"]["failed"]["name"]
    assert reason.startswith("error: RuntimeError")
    assert "name is already being used" in reason
    assert "CURRENT Batch group" in reason and "node's own group" in reason
    assert "plan/" not in reason
    assert h["comp"].name.get_value() == "comp1"  # unchanged
    assert result["value"]["applied"] == [] and result["value"]["revert"] is None


def test_node_set_attributes_description_warns_about_the_current_group_rule() -> None:
    registry = make_registry()
    op = registry.get("node.set_attributes")
    assert op is not None
    assert "current Batch group" in op.description and "revert" in op.description
    assert "already has that name" in op.description
    other = registry.get("batch.set_attributes")
    assert other is not None and "already has that name" not in other.description


def test_only_node_ops_check_names_against_a_group() -> None:
    registry = make_registry()
    flame, h = make_batch_world()
    result = set_call(registry, "batch.set_attributes", BG1, {"name": "BG2"}, flame)
    assert result["opError"] is None  # batch names are Flame's business, not this check
    assert h["bg1"].name.get_value() == "BG2"


# pos_x is an int attribute with a +-2147483647 range.


class IntAttr(SpyAttr):
    """An integer attribute that, like the one seen on a real node, quietly ignores a float."""

    def set_value(self, value: Any) -> None:
        self.set_calls.append(value)
        if isinstance(value, int) and not isinstance(value, bool):
            self._value = value


IntAttr.__name__ = "PyAttribute"


def _int_world() -> tuple[FakeFlame, dict[str, Any]]:
    flame, h = make_batch_world()
    h["comp"].pos_x = IntAttr(0, (-2147483647, 2147483647))
    return flame, h


@pytest.mark.parametrize("bad", [123.0, 1.5, -2.0, 0.0])
def test_a_float_for_an_integer_attribute_is_refused_up_front_even_when_it_is_whole(
    bad: float,
) -> None:
    registry = make_registry()
    flame, h = _int_world()
    result = set_call(registry, "node.set_attributes", COMP, {"pos_x": bad}, flame)
    assert result["opErrorCode"] == "INVALID_ARGS"  # not a SILENT_NOOP found after the write
    reason = result["value"]["failed"]["pos_x"]
    assert reason.startswith("rejected") and "integer" in reason and repr(bad) in reason
    assert h["comp"].pos_x.set_calls == []  # never reached Flame
    assert result["partial"] == {"applied": 0, "failed": 1}


def test_an_integer_is_accepted_for_an_integer_attribute_and_reverts_as_an_integer() -> None:
    registry = make_registry()
    flame, h = _int_world()
    result = set_call(registry, "node.set_attributes", COMP, {"pos_x": 123}, flame)
    assert result["opError"] is None, result["opError"]
    assert h["comp"].pos_x.get_value() == 123
    assert result["value"]["revert"]["args"]["values"] == {"pos_x": 0}
    assert isinstance(result["value"]["revert"]["args"]["values"]["pos_x"], int)


def test_an_integer_out_of_an_integer_attributes_range_is_still_refused_by_range() -> None:
    registry = make_registry()
    flame, h = _int_world()
    result = set_call(registry, "node.set_attributes", COMP, {"pos_x": 2147483648}, flame)
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert "outside the range" in result["value"]["failed"]["pos_x"]
    assert h["comp"].pos_x.set_calls == []


def test_a_float_attribute_still_takes_both_floats_and_integers() -> None:
    registry = make_registry()
    flame, h = make_batch_world()  # exposure_red is a float attribute (0.0)
    for value in (0.5, 1):
        result = set_call(registry, "node.set_attributes", EXPO, {"exposure_red": value}, flame)
        assert result["opError"] is None, (value, result["opError"])
    assert h["expo"].exposure_red.get_value() == 1


def _sequence_target(h: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "wiretap", "id": h["seq"].get_wiretap_node_id()}


def test_get_attributes_returns_a_wrapper_value_as_its_type_and_name() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = call(registry, "desktop.get_attributes", {"target": _sequence_target(h)}, flame)
    attrs = result["value"]["attributes"]
    assert attrs["primary_track"] == {"value": {"type": "PyTrack", "name": "*"}}
    assert attrs["secondary_track"] == {"value": None}
    assert attrs["subtitles_track"] == {"value": None}
    assert "0x" not in json.dumps(result["value"])  # no address (it changes on every call)


def test_get_attributes_returns_the_wrappers_inside_a_list_the_same_way() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = call(registry, "desktop.get_attributes", {"target": _sequence_target(h)}, flame)
    selected = result["value"]["attributes"]["selected_segments"]["value"]
    assert selected == [
        {"type": "PySegment", "name": "seg0"},
        {"type": "PySegment", "name": "seg1"},
    ]


def test_a_wrapper_whose_name_cannot_be_read_is_just_its_type() -> None:
    class PyMarker:  # a Flame wrapper with no readable name
        @property
        def name(self) -> Any:
            raise RuntimeError("no name")

    registry = make_registry()
    flame, h = build_default_tree()
    add_attr(h["seq"], "marker", FakeAttr(PyMarker()))
    result = call(
        registry,
        "desktop.get_attributes",
        {"target": _sequence_target(h), "keys": ["marker"]},
        flame,
    )
    assert result["value"]["attributes"]["marker"] == {"value": {"type": "PyMarker"}}


def test_a_wrapper_whose_name_is_none_is_just_its_type() -> None:
    # PyFlameObject subclasses answer None for a name they do not define
    from fake_media_tree import PyFlameObject

    class PyBatchIteration(PyFlameObject):
        pass

    registry = make_registry()
    flame, h = build_default_tree()
    add_attr(h["seq"], "iteration", FakeAttr(PyBatchIteration()))
    result = call(
        registry,
        "desktop.get_attributes",
        {"target": _sequence_target(h), "keys": ["iteration"]},
        flame,
    )
    assert result["value"]["attributes"]["iteration"] == {"value": {"type": "PyBatchIteration"}}


def test_get_attributes_still_converts_pytime_tuples_lists_and_scalars_as_before() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    seg = h["seg0"]
    result = call(
        registry,
        "segment.get_attributes",
        {
            "target": {
                "kind": "segment",
                "parent": _sequence_target(h),
                "version": 0,
                "track": 0,
                "index": 0,
            }
        },
        flame,
    )
    attrs = result["value"]["attributes"]
    assert attrs["colour"] == {"value": [0.0, 0.0, 0.0]}
    assert attrs["tags"] == {"value": []}
    assert attrs["name"] == {"value": "seg0"}
    start = call(
        registry,
        "desktop.get_attributes",
        {"target": _sequence_target(h), "keys": ["start_time"]},
        flame,
    )
    assert start["value"]["attributes"]["start_time"] == {
        "value": {"timecode": "00:00:00+00", "frame": 0}
    }
    assert seg is not None


def test_a_non_flame_object_value_still_falls_back_to_its_repr() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    add_attr(h["seq"], "odd", FakeAttr({"not": "a scalar"}))
    result = call(
        registry,
        "desktop.get_attributes",
        {"target": _sequence_target(h), "keys": ["odd"]},
        flame,
    )
    assert result["value"]["attributes"]["odd"] == {"value": repr({"not": "a scalar"})}
