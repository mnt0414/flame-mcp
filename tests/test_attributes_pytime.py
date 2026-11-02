"""`current_time` of a clip is written through the generic attribute ops; every other PyTime
attribute is refused.

Measured on Flame 2025.2.4: `PyTime(frame, fps)` and `PyTime(timecode, fps)` built
with the clip's own `frame_rate` write and read back as asked; a frame outside the clip's range
is moved to the nearest end without an error, so the range is checked before the write; raising
`start_time` drags `current_time` along and lowering it does not, so it is not written."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr, PyTime, build_default_tree
from flame_mcp.registry import Registry
from journal_test_helpers import Rig, make_rig
from test_ops_attributes import (
    SpyAttr,
    add_attr,
    add_resolution_nodes,
    call,
    make_registry,
    res_node,
    set_call,
)

FPS = "23.976 fps"
START = 86400
DURATION = 100
LAST = START + DURATION - 1
CLIP_ID = "/projects/p/ws/P1O/R1/clip1"
TARGET = {"kind": "wiretap", "id": CLIP_ID}
DESKTOP_TARGET = {"kind": "wiretap", "id": "/wt/c1"}
TC_START = "01:00:00+00"
TC_10 = "01:00:00+10"


def timed(clip: Any) -> Any:
    clip.start_time = FakeAttr(PyTime(START, FPS))
    add_attr(clip, "current_time", SpyAttr(PyTime(START, FPS)))
    return clip


@pytest.fixture
def world() -> tuple[Registry, Any, Any]:
    flame, handles = build_default_tree()
    return make_registry(), flame, timed(handles["clip1"])


def write(registry: Registry, flame: Any, value: Any, key: str = "current_time") -> dict[str, Any]:
    return set_call(registry, "desktop.set_attributes", TARGET, {key: value}, flame)


def test_current_time_is_written_from_a_frame_with_the_clips_frame_rate(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world
    result = write(registry, flame, {"frame": START + 10})
    assert result["opError"] is None
    assert result["value"]["applied"] == ["current_time"]
    assert result["value"]["before"]["current_time"] == {"timecode": TC_START, "frame": START}
    assert result["value"]["after"]["current_time"] == {"timecode": TC_10, "frame": START + 10}
    (written,) = clip.current_time.set_calls
    assert (written.frame, written.frame_rate) == (START + 10, FPS)
    assert clip.current_time.get_value().frame == START + 10


def test_current_time_is_written_from_a_timecode(world: tuple[Registry, Any, Any]) -> None:
    registry, flame, clip = world
    result = write(registry, flame, {"timecode": TC_10})
    assert result["opError"] is None
    assert result["value"]["after"]["current_time"] == {"timecode": TC_10, "frame": START + 10}
    assert clip.current_time.get_value().frame == START + 10


def test_a_frame_and_a_timecode_that_agree_are_written(world: tuple[Registry, Any, Any]) -> None:
    registry, flame, clip = world
    result = write(registry, flame, {"frame": START + 10, "timecode": TC_10})
    assert result["opError"] is None
    assert clip.current_time.get_value().frame == START + 10


def test_a_frame_and_a_timecode_that_disagree_are_refused(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world
    result = write(registry, flame, {"frame": START + 10, "timecode": "01:00:00+11"})
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert "timecode" in result["value"]["failed"]["current_time"]
    assert clip.current_time.set_calls == []


def test_the_fps_of_the_clip_is_accepted(world: tuple[Registry, Any, Any]) -> None:
    registry, flame, clip = world
    result = write(registry, flame, {"frame": START + 3, "fps": FPS})
    assert result["opError"] is None
    assert clip.current_time.get_value().frame == START + 3


def test_another_fps_is_refused_because_it_would_change_the_meaning(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world
    result = write(registry, flame, {"frame": START + 3, "fps": "25 fps"})
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert "fps" in result["value"]["failed"]["current_time"]
    assert clip.current_time.set_calls == []


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"frame": START - 1}, id="frame-before-the-start"),
        pytest.param({"frame": LAST + 1}, id="frame-after-the-end"),
        pytest.param({"frame": -1}, id="negative-frame"),
        pytest.param({"timecode": "01:00:04+05"}, id="timecode-after-the-end"),
        pytest.param({"timecode": "00:59:59+23"}, id="timecode-before-the-start"),
    ],
)
def test_a_time_outside_the_clips_range_is_refused_and_nothing_is_written(
    world: tuple[Registry, Any, Any], value: dict[str, Any]
) -> None:
    registry, flame, clip = world
    result = write(registry, flame, value)
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert "range" in result["value"]["failed"]["current_time"]
    assert clip.current_time.set_calls == []
    assert clip.current_time.get_value().frame == START


@pytest.mark.parametrize("frame", [START, LAST])
def test_both_ends_of_the_range_are_written(world: tuple[Registry, Any, Any], frame: int) -> None:
    registry, flame, clip = world
    assert write(registry, flame, {"frame": frame})["opError"] is None
    assert clip.current_time.get_value().frame == frame


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(5, id="bare-integer"),
        pytest.param("01:00:00+10", id="bare-string"),
        pytest.param({}, id="empty-object"),
        pytest.param({"fps": FPS}, id="fps-only"),
        pytest.param({"frame": "86410"}, id="frame-as-string"),
        pytest.param({"frame": True}, id="frame-as-bool"),
        pytest.param({"frame": 86410.0}, id="frame-as-float"),
        pytest.param({"timecode": 5}, id="timecode-as-number"),
        pytest.param({"frame": 86410, "fps": 24}, id="fps-as-number"),
        pytest.param({"frame": 86410, "relative_frame": 5}, id="unknown-key"),
        pytest.param({"timecode": "not a timecode"}, id="timecode-flame-cannot-build"),
    ],
)
def test_a_value_of_the_wrong_shape_is_refused(
    world: tuple[Registry, Any, Any], value: Any
) -> None:
    registry, flame, clip = world
    result = write(registry, flame, value)
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert result["value"]["failed"]["current_time"].startswith("rejected:")
    assert clip.current_time.set_calls == []


def test_the_reason_for_a_refused_value_names_the_shape_that_is_accepted(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, _clip = world
    reason = write(registry, flame, 5)["value"]["failed"]["current_time"]
    assert "frame" in reason and "timecode" in reason


def test_start_time_is_refused_and_the_reason_says_only_current_time_is_written(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world
    result = write(registry, flame, {"frame": START + 5}, key="start_time")
    assert result["opErrorCode"] == "INVALID_ARGS"
    reason = result["value"]["failed"]["start_time"]
    assert "only current_time can be written" in reason
    assert "start_time moves current_time along when raised and not when lowered" in reason
    assert clip.start_time.get_value().frame == START


def test_a_refused_key_stops_the_others_of_the_same_call(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world
    clip.name = SpyAttr("clip1")
    clip.attributes.append("name")
    result = set_call(
        registry,
        "desktop.set_attributes",
        TARGET,
        {"name": "renamed", "start_time": {"frame": START + 1}},
        flame,
    )
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert clip.name.set_calls == []


def test_the_returned_revert_puts_the_previous_time_back(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world
    result = write(registry, flame, {"frame": START + 10})
    revert = result["value"]["revert"]
    assert revert["args"]["values"] == {"current_time": {"timecode": TC_START, "frame": START}}
    back = call(registry, revert["op"], revert["args"], flame)
    assert back["opError"] is None
    assert clip.current_time.get_value().frame == START
    assert back["value"]["after"]["current_time"] == {"timecode": TC_START, "frame": START}


def test_writing_the_time_it_already_has_is_not_an_error_and_has_nothing_to_revert(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, _clip = world
    result = write(registry, flame, {"frame": START})
    assert result["opError"] is None
    assert result["value"]["revert"] is None


def test_a_write_that_lands_on_another_frame_is_partial_not_applied(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world

    class Clamping(SpyAttr):
        def set_value(self, value: Any) -> None:
            self.set_calls.append(value)
            self._value = PyTime(value.frame - 1, FPS)

    Clamping.__name__ = "PyAttribute"
    clip.current_time = Clamping(PyTime(START, FPS))
    result = write(registry, flame, {"frame": START + 10})
    assert result["opErrorCode"] == "FLAME_API_ERROR"
    assert result["value"]["failed"]["current_time"].startswith("partial:")
    assert result["value"]["revert"]["args"]["values"]["current_time"]["frame"] == START


def test_a_write_that_changes_nothing_is_a_silent_noop(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world

    class Frozen(SpyAttr):
        def set_value(self, value: Any) -> None:
            self.set_calls.append(value)

    Frozen.__name__ = "PyAttribute"
    clip.current_time = Frozen(PyTime(START, FPS))
    result = write(registry, flame, {"frame": START + 10})
    assert result["opErrorCode"] == "SILENT_NOOP"


def test_compare_and_swap_accepts_the_json_form_of_the_time(
    world: tuple[Registry, Any, Any],
) -> None:
    from op_test_helpers import run_op

    registry, flame, clip = world
    expected = {"current_time": {"timecode": TC_START, "frame": START}}
    args = {"target": TARGET, "values": {"current_time": {"frame": START + 10}}}
    stale = {"current_time": {"timecode": TC_10, "frame": START + 10}}
    conflict = run_op(registry, "desktop.set_attributes", {**args, "_expected": stale}, flame)
    assert conflict["opErrorCode"] == "CONFLICT"
    assert conflict["value"]["conflicts"]["current_time"]["current"]["frame"] == START
    assert clip.current_time.set_calls == []
    ok = run_op(registry, "desktop.set_attributes", {**args, "_expected": expected}, flame)
    assert ok["opError"] is None
    assert clip.current_time.get_value().frame == START + 10


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_rig(monkeypatch, tmp_path)
    timed(made.handles["reel"].children[0])
    return made


def current_frame(rig: Rig) -> int:
    frame: int = rig.handles["reel"].children[0].current_time.get_value().frame
    return frame


def test_journal_revert_puts_the_previous_time_back(rig: Rig) -> None:
    env = rig.do(
        "desktop.set_attributes",
        {"target": DESKTOP_TARGET, "values": {"current_time": {"frame": START + 10}}},
    )
    assert env["ok"] is True, env
    assert current_frame(rig) == START + 10
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert current_frame(rig) == START


def test_journal_revert_is_refused_when_the_time_moved_since_the_write(rig: Rig) -> None:
    env = rig.do(
        "desktop.set_attributes",
        {"target": DESKTOP_TARGET, "values": {"current_time": {"timecode": TC_10}}},
    )
    assert env["ok"] is True, env
    rig.handles["reel"].children[0].current_time.set_value(PyTime(START + 20, FPS))
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is False and back["errorCode"] == "CONFLICT"
    assert current_frame(rig) == START + 20


def intent_of(rig: Rig, group: str) -> dict[str, Any]:
    return next(r for r in rig.records() if r["type"] == "intent" and r["group"] == group)


def timed_write_lost(rig: Rig, value: dict[str, Any], *, when: str) -> dict[str, Any]:
    rig.exec.fault("desktop.set_attributes", "TIMEOUT", when=when)
    env = rig.do(
        "desktop.set_attributes",
        {"target": DESKTOP_TARGET, "values": {"current_time": value}},
    )
    assert env["errorCode"] == "TIMEOUT", env
    return env


@pytest.mark.parametrize(
    ("value", "recorded"),
    [
        pytest.param({"frame": START + 10}, {"frame": START + 10}, id="frame"),
        pytest.param({"timecode": TC_10}, {"timecode": TC_10}, id="timecode"),
        pytest.param(
            {"frame": START + 10, "timecode": TC_10, "fps": FPS},
            {"frame": START + 10},
            id="frame-timecode-and-fps",
        ),
    ],
)
def test_the_intent_of_a_time_write_holds_only_what_can_be_compared(
    rig: Rig, value: dict[str, Any], recorded: dict[str, Any]
) -> None:
    env = timed_write_lost(rig, value, when="before")
    assert intent_of(rig, env["journal"]["group"])["after"] == {"current_time": recorded}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"frame": START + 10}, id="frame"),
        pytest.param({"timecode": TC_10}, id="timecode"),
        pytest.param({"frame": START + 10, "fps": FPS}, id="frame-and-fps"),
    ],
)
def test_check_finds_a_time_write_landed_when_the_answer_was_lost(
    rig: Rig, value: dict[str, Any]
) -> None:
    env = timed_write_lost(rig, value, when="after")
    assert current_frame(rig) == START + 10
    found = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert found["ok"] is True, found
    assert found["result"]["checked"][0]["verdict"] == "landed"


@pytest.mark.parametrize(
    "value", [pytest.param({"frame": START + 10}, id="frame"), pytest.param({"timecode": TC_10})]
)
def test_check_finds_a_time_write_not_landed_when_the_time_is_still_the_old_one(
    rig: Rig, value: dict[str, Any]
) -> None:
    env = timed_write_lost(rig, value, when="before")
    assert current_frame(rig) == START
    found = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert found["result"]["checked"][0]["verdict"] == "not_landed"


def test_check_calls_a_time_that_is_neither_the_old_nor_the_asked_one_diverged(rig: Rig) -> None:
    env = timed_write_lost(rig, {"frame": START + 10}, when="before")
    rig.handles["reel"].children[0].current_time.set_value(PyTime(START + 20, FPS))
    found = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert found["result"]["checked"][0]["verdict"] == "diverged"


def test_revert_after_a_check_puts_back_a_time_written_by_timecode(rig: Rig) -> None:
    env = timed_write_lost(rig, {"timecode": TC_10}, when="after")
    group = env["journal"]["group"]
    assert rig.do("journal.check", {"group": group})["ok"] is True
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert current_frame(rig) == START


@pytest.fixture
def res_rig(rig: Rig) -> Rig:
    add_resolution_nodes(rig.handles)
    return rig


def lost_resolution_write(rig: Rig, *, when: str) -> dict[str, Any]:
    rig.exec.fault("node.set_attributes", "TIMEOUT", when=when)
    env = rig.do(
        "node.set_attributes",
        {"target": res_node("res_full"), "values": {"resolution": {"width": 800}}},
    )
    assert env["errorCode"] == "TIMEOUT", env
    return env


def test_the_intent_of_a_resolution_write_still_names_every_field(res_rig: Rig) -> None:
    env = lost_resolution_write(res_rig, when="before")
    after = intent_of(res_rig, env["journal"]["group"])["after"]["resolution"]
    assert after == {
        "type": "PyResolution",
        "width": 800,
        "height": 1080,
        "bit_depth": 16,
        "frame_ratio": 1.7778,
        "scan_mode": "P",
    }


def test_check_still_finds_a_resolution_write_landed(res_rig: Rig) -> None:
    env = lost_resolution_write(res_rig, when="after")
    found = res_rig.do("journal.check", {"group": env["journal"]["group"]})
    assert found["result"]["checked"][0]["verdict"] == "landed"


def test_check_still_finds_a_resolution_write_not_landed(res_rig: Rig) -> None:
    env = lost_resolution_write(res_rig, when="before")
    found = res_rig.do("journal.check", {"group": env["journal"]["group"]})
    assert found["result"]["checked"][0]["verdict"] == "not_landed"


def test_another_pytime_attribute_is_refused_without_the_start_time_remark(
    world: tuple[Registry, Any, Any],
) -> None:
    registry, flame, clip = world
    add_attr(clip, "record_in", SpyAttr(PyTime(0, FPS)))
    result = write(registry, flame, {"frame": 5}, key="record_in")
    assert result["opErrorCode"] == "INVALID_ARGS"
    reason = result["value"]["failed"]["record_in"]
    assert "only current_time can be written" in reason and "record_in" in reason
    assert "moves current_time along" not in reason
    assert clip.record_in.set_calls == []


class _Unreadable:
    @property
    def frame(self) -> int:
        raise RuntimeError("cannot read")


@pytest.mark.parametrize("what", ["start_time", "duration"])
def test_a_range_that_cannot_be_read_refuses_the_write(
    world: tuple[Registry, Any, Any], what: str
) -> None:
    registry, flame, clip = world
    if what == "start_time":

        class Broken(SpyAttr):
            def get_value(self) -> Any:
                raise RuntimeError("cannot read")

        Broken.__name__ = "PyAttribute"
        clip.start_time = Broken(PyTime(START, FPS))
    else:
        clip.duration = _Unreadable()
    result = write(registry, flame, {"frame": START + 1})
    assert result["opErrorCode"] == "INVALID_ARGS"
    assert "cannot be read" in result["value"]["failed"]["current_time"]
    assert clip.current_time.set_calls == []


MALFORMED = [
    pytest.param("current_time", {}, id="empty-object"),
    pytest.param("current_time", {"fps": FPS}, id="fps-only"),
    pytest.param("current_time", {"relative_frame": 3}, id="relative-frame"),
    pytest.param("start_time", {"foo": 1}, id="start-time-unknown-key"),
    pytest.param("current_time", {"frame": "x"}, id="frame-not-an-int"),
    pytest.param("current_time", {"timecode": 5}, id="timecode-not-a-string"),
]


@pytest.mark.parametrize(("key", "value"), MALFORMED)
def test_a_malformed_time_through_the_journal_is_invalid_args_and_changes_nothing(
    rig: Rig, key: str, value: Any
) -> None:
    env = rig.do("desktop.set_attributes", {"target": DESKTOP_TARGET, "values": {key: value}})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    clip = rig.handles["reel"].children[0]
    assert clip.current_time.set_calls == [] and clip.start_time.get_value().frame == START
    assert current_frame(rig) == START
    # a value Flame's own preflight refuses is a journaled failure, like any other rejected value
    states = [i["state"] for i in rig.do("journal.list")["result"]["items"]]
    assert states == ["failed"]
