"""One judging rule for the writes that read back what they wrote.

1. nothing changed on read-back                      -> SILENT_NOOP
2. part took effect (or an extra effect appeared)    -> FLAME_API_ERROR, message starts with
                                                        `partial:`, the result keeps what took
                                                        and what did not, journal.revert works
3. all took effect but Flame's return value is odd   -> ok with a `degraded` note
4. the read-back itself raised after the write       -> FLAME_API_ERROR, "may have been written"

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree. The fakes
are bent in each test (a wrapper around one Flame call), so each test describes the FAKE with that
one misbehaviour, not a measured Flame."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_media_tree import (
    FakeAttr,
    PyBatch,
    PyBatchIteration,
    PyClip,
    PyCoNode,
    PyNode,
    PyRenderNode,
    PyTime,
    PyWriteFileNode,
)
from flame_mcp import checkpoints
from flame_mcp.journal import Index
from flame_mcp.render_run import judge_render, missing_outputs
from journal_test_helpers import BG1, Rig, make_rig
from timeline_p32a_helpers import S1, make_timeline_rig

BG2 = {"kind": "batch", "name": "BG2"}
BLUR = {"kind": "node", "batch": BG1, "name": "blur1"}
COMP = {"kind": "node", "batch": BG1, "name": "comp1"}
ACTION = {"kind": "node", "batch": BG1, "name": "action1"}
CLIP1 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip1"}
CLIP2 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip2"}
SCHEMATIC = "Schematic Reel 1"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def bg(rig: Rig, key: str = "bg1") -> PyBatch:
    batch: PyBatch = rig.handles[key]
    return batch


def names(rig: Rig, key: str = "bg1") -> list[str]:
    return sorted(str(n.name.get_value()) for n in bg(rig, key).nodes)


def node(rig: Rig, name: str, key: str = "bg1") -> PyNode:
    found: PyNode = next(n for n in bg(rig, key).nodes if n.name.get_value() == name)
    return found


def fingerprint(rig: Rig, target: dict[str, Any] = BG1) -> str:
    env = rig.do("batch.fingerprint", {"target": target})
    assert env["ok"] is True, env
    value: str = env["result"]["fingerprint"]
    return value


def outcome_of(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.outcome is not None
    return dict(view.outcome)


def is_partial(env: dict[str, Any]) -> bool:
    return (
        env["ok"] is False
        and env["errorCode"] == "FLAME_API_ERROR"
        and str(env["error"]).startswith("partial:")
    )


# -- node.delete / connect / disconnect: the effect is right, the return value is not (rule 3) ---


def test_delete_that_removes_the_node_but_does_not_return_true_is_ok_with_a_note(rig: Rig) -> None:
    victim = node(rig, "blur1")
    real = victim.delete
    victim.delete = lambda confirm=True: (real(confirm), None)[1]  # type: ignore[method-assign,assignment,return-value]
    env = rig.do("node.delete", {"target": BLUR})
    assert env["ok"] is True, env
    assert "blur1" not in names(rig) and env["result"]["deleteReturned"] is None
    assert any("expected True" in note for note in env["degraded"]), env["degraded"]


def test_delete_that_takes_other_nodes_with_it_is_partial_and_can_be_reverted(rig: Rig) -> None:
    victim = node(rig, "blur1")
    other = node(rig, "comp1")
    real = victim.delete

    def greedy(confirm: bool = True) -> bool:
        real(confirm)
        bg(rig).remove_node(other)
        return True

    victim.delete = greedy  # type: ignore[method-assign]
    before = fingerprint(rig)
    env = rig.do("node.delete", {"target": BLUR})
    assert is_partial(env), env
    assert "comp1" in env["error"] and env["result"]["deleted"] == "blur1"
    assert outcome_of(rig, env)["ok"] is False
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and fingerprint(rig) == before


def test_connect_that_links_but_does_not_return_true_is_ok_with_a_note(rig: Rig) -> None:
    real = bg(rig).connect_nodes

    def quiet(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)

    bg(rig).connect_nodes = quiet  # type: ignore[method-assign,assignment]
    env = rig.do("node.connect", {"source": BLUR, "target": COMP})
    assert env["ok"] is True, env
    assert env["result"]["linkedInputSockets"] == ["Input_0"]
    assert any("expected True" in note for note in env["degraded"]), env["degraded"]


def test_connect_that_links_nothing_is_still_silent_noop(rig: Rig) -> None:
    bg(rig).connect_makes_nothing = True
    env = rig.do("node.connect", {"source": BLUR, "target": COMP})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"


def test_disconnect_that_cuts_but_does_not_return_true_is_ok_with_a_note(rig: Rig) -> None:
    bg(rig).connect_nodes(node(rig, "blur1"), "Default", node(rig, "comp1"), "Default")
    real = bg(rig).disconnect_node

    def quiet(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)

    bg(rig).disconnect_node = quiet  # type: ignore[method-assign,assignment]
    env = rig.do("node.disconnect", {"target": COMP})
    assert env["ok"] is True, env
    assert env["result"]["hadLinks"] is True
    assert any("expected True" in note for note in env["degraded"]), env["degraded"]


# -- node.duplicate / node.create / action.create_node: an extra or half effect (rule 2) ----------


def test_duplicate_that_makes_two_copies_is_partial_and_the_result_keeps_them(rig: Rig) -> None:
    group = bg(rig, "bg2")
    real = group.duplicate_node

    def twice(source: PyNode, keep: bool) -> PyNode:
        first = real(source, keep)
        real(source, keep)
        return first

    group.duplicate_node = twice  # type: ignore[method-assign,assignment]
    before = fingerprint(rig, BG2)
    target = {"kind": "node", "batch": BG2, "name": "blur1"}
    env = rig.do("node.duplicate", {"target": target})
    assert is_partial(env), env
    assert len(env["result"]["addedNames"]) == 2 and env["result"]["error"] == env["error"]
    assert outcome_of(rig, env)["ok"] is False
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and fingerprint(rig, BG2) == before


def test_duplicate_that_makes_no_copy_stays_silent_noop(rig: Rig) -> None:
    bg(rig, "bg2").duplicate_makes_nothing = True
    target = {"kind": "node", "batch": BG2, "name": "blur1"}
    env = rig.do("node.duplicate", {"target": target})
    assert env["errorCode"] == "SILENT_NOOP" and "0 new node" in env["error"]


def test_create_whose_naming_does_nothing_is_partial_the_node_exists_and_revert_removes_it(
    rig: Rig,
) -> None:
    real = bg(rig).create_node

    def create_with_a_name_that_will_not_stick(node_type: str) -> PyNode:
        made = real(node_type)

        class Ignoring(FakeAttr):
            def set_value(self, value: Any) -> None:
                return None

        Ignoring.__name__ = "PyAttribute"
        made.name = Ignoring(made.name.get_value())
        return made

    bg(rig).create_node = create_with_a_name_that_will_not_stick  # type: ignore[method-assign]
    env = rig.do("node.create", {"target": BG1, "type": "Blur", "name": "wanted"})
    assert is_partial(env), env
    assert env["result"]["createdAs"] in names(rig) and "wanted" not in names(rig)
    assert outcome_of(rig, env)["ok"] is False
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and len(names(rig)) == 4


def test_create_whose_naming_raises_is_partial_too(rig: Rig) -> None:
    real = bg(rig).create_node

    def create_with_a_name_flame_refuses(node_type: str) -> PyNode:
        made = real(node_type)

        class Refusing(FakeAttr):
            def set_value(self, value: Any) -> None:
                raise RuntimeError("Could not set Batch node name, name is already being used.")

        Refusing.__name__ = "PyAttribute"
        made.name = Refusing(made.name.get_value())
        return made

    bg(rig).create_node = create_with_a_name_flame_refuses  # type: ignore[method-assign]
    env = rig.do("node.create", {"target": BG1, "type": "Blur", "name": "wanted"})
    assert is_partial(env), env
    assert "already being used" in env["error"]


def test_create_that_adds_two_nodes_and_returns_neither_is_partial(rig: Rig) -> None:
    real = bg(rig).create_node

    def make_two(node_type: str) -> PyNode:
        real(node_type)
        real(node_type)
        return PyNode("ghost", node_type)

    bg(rig).create_node = make_two  # type: ignore[method-assign]
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert is_partial(env), env
    assert len(env["result"]["addedNames"]) == 2
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and len(names(rig)) == 4


def make_two_return_first(rig: Rig) -> None:
    real = bg(rig).create_node

    def make_two(node_type: str) -> PyNode:
        first = real(node_type)
        real(node_type)
        return first

    bg(rig).create_node = make_two  # type: ignore[method-assign]


def test_create_that_adds_two_nodes_and_returns_one_of_them_is_partial(rig: Rig) -> None:
    make_two_return_first(rig)
    before = fingerprint(rig)
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert is_partial(env), env
    added = env["result"]["addedNames"]
    assert len(added) == 2 and env["result"]["createdAs"] == added[0]
    assert env["error"].startswith("partial: create_node left 2 new nodes ")
    assert repr(added[0]) in env["error"] and "(the others are extra)" in env["error"]
    assert "renamed" not in env["error"]
    assert env["result"]["error"] == env["error"] and env["result"]["node"]["name"] == added[0]
    assert len(names(rig)) == 6
    assert outcome_of(rig, env)["ok"] is False
    assert rig.do("journal.list", {})["result"]["items"][0]["state"] == "partial"
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and len(names(rig)) == 4 and fingerprint(rig) == before


def test_create_with_a_name_that_adds_two_nodes_names_the_returned_one_and_is_partial(
    rig: Rig,
) -> None:
    make_two_return_first(rig)
    env = rig.do("node.create", {"target": BG1, "type": "Blur", "name": "wanted"})
    assert is_partial(env), env
    assert "wanted" in names(rig) and env["result"]["node"]["name"] == "wanted"
    assert len(env["result"]["addedNames"]) == 2 and "(the others are extra)" in env["error"]
    created = env["result"]["createdAs"]
    assert f"the one it returned is {created!r} (renamed to 'wanted')" in env["error"]
    assert env["result"]["nodeCountAfter"] == 6
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and len(names(rig)) == 4


def test_a_failed_naming_is_not_hidden_by_the_extra_node(rig: Rig) -> None:
    real = bg(rig).create_node

    def make_two_and_refuse_the_name(node_type: str) -> PyNode:
        first = real(node_type)
        real(node_type)

        class Refusing(FakeAttr):
            def set_value(self, value: Any) -> None:
                raise RuntimeError("Could not set Batch node name, name is already being used.")

        Refusing.__name__ = "PyAttribute"
        first.name = Refusing(first.name.get_value())
        return first

    bg(rig).create_node = make_two_and_refuse_the_name  # type: ignore[method-assign]
    env = rig.do("node.create", {"target": BG1, "type": "Blur", "name": "wanted"})
    assert is_partial(env), env
    assert "naming it 'wanted' failed" in env["error"] and "already being used" in env["error"]
    assert "left 2 new nodes" in env["error"] and "(the others are extra)" in env["error"]
    assert "wanted" not in names(rig) and len(env["result"]["addedNames"]) == 2


def add_blend_types(rig: Rig) -> None:
    bg(rig).node_types += ["Colour Blend", "Matte Blend"]


@pytest.mark.parametrize("blend", ["Colour Blend", "Matte Blend"])
def test_create_of_a_type_flame_makes_a_companion_for_is_ok_and_lists_the_companion(
    rig: Rig, blend: str
) -> None:
    add_blend_types(rig)
    before = fingerprint(rig)
    env = rig.do("node.create", {"target": BG1, "type": blend})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["companions"] == ["groupinput1"]
    assert result["nodeCountAfter"] == result["nodeCountBefore"] + 2
    assert len(result["addedNames"]) == 2 and "groupinput1" in names(rig)
    assert result["node"]["type"] == blend and result["createdAs"] != "groupinput1"
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and len(names(rig)) == 4 and fingerprint(rig) == before


def test_create_of_a_type_with_a_companion_and_a_name_names_the_returned_node(rig: Rig) -> None:
    add_blend_types(rig)
    env = rig.do("node.create", {"target": BG1, "type": "Colour Blend", "name": "wanted"})
    assert env["ok"] is True, env
    assert env["result"]["node"]["name"] == "wanted" and env["result"]["companions"] == [
        "groupinput1"
    ]


def test_create_of_a_type_with_a_companion_that_also_adds_another_node_is_partial(
    rig: Rig,
) -> None:
    add_blend_types(rig)
    real = bg(rig).create_node

    def blend_and_a_blur(node_type: str) -> PyNode:
        first = real(node_type)
        real("Blur")
        return first

    bg(rig).create_node = blend_and_a_blur  # type: ignore[method-assign]
    env = rig.do("node.create", {"target": BG1, "type": "Matte Blend"})
    assert is_partial(env), env
    assert env["result"]["companions"] == ["groupinput1"] and len(env["result"]["addedNames"]) == 3
    assert "groupinput1" not in env["error"]
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and len(names(rig)) == 4


def test_a_group_input_node_that_appears_beside_another_type_is_not_a_companion(
    rig: Rig,
) -> None:
    real = bg(rig).create_node

    def blur_and_a_group_input(node_type: str) -> PyNode:
        made = real(node_type)
        stray = PyNode("groupinput1", "Group Input", pos=(-186.0, 0.0))
        stray._owner = bg(rig)
        bg(rig).nodes.append(stray)
        return made

    bg(rig).create_node = blur_and_a_group_input  # type: ignore[method-assign]
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert is_partial(env), env
    assert env["result"]["companions"] == [] and "groupinput1" in env["error"]


def test_create_that_makes_nothing_stays_silent_noop(rig: Rig) -> None:
    bg(rig).create_node_makes_nothing = True
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert env["errorCode"] == "SILENT_NOOP" and "no new node appeared" in env["error"]


def test_action_create_node_that_adds_two_inner_nodes_and_returns_neither_is_partial(
    rig: Rig,
) -> None:
    action = node(rig, "action1")
    real = action.create_node

    def add_two(node_type: str) -> PyCoNode:
        real(node_type)
        real(node_type)
        return PyCoNode("ghost", node_type)

    action.create_node = add_two  # type: ignore[attr-defined]
    before = fingerprint(rig)
    env = rig.do("action.create_node", {"target": ACTION, "type": "Camera"})
    assert is_partial(env), env
    assert len(env["result"]["added"]) == 2
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and fingerprint(rig) == before


def test_action_create_node_with_one_added_node_and_an_odd_return_is_ok_with_a_note(
    rig: Rig,
) -> None:
    action = node(rig, "action1")
    real = action.create_node

    def odd_return(node_type: str) -> PyCoNode:
        real(node_type)
        return PyCoNode("ghost", node_type)

    action.create_node = odd_return  # type: ignore[attr-defined]
    env = rig.do("action.create_node", {"target": ACTION, "type": "Camera"})
    assert env["ok"] is True, env
    assert any("ghost" in note for note in env["degraded"]), env["degraded"]


# -- batch.add_clips -----------------------------------------------------------------------------


@pytest.fixture
def batch_rig(rig: Rig) -> Rig:
    bg(rig).create_reel(SCHEMATIC)
    return rig


def add_clips(rig: Rig, clips: list[dict[str, Any]]) -> dict[str, Any]:
    return rig.do("batch.add_clips", {"target": BG1, "reel": SCHEMATIC, "clips": clips})


def test_add_clips_where_only_some_arrive_is_partial_and_says_which(batch_rig: Rig) -> None:
    rig = batch_rig
    real = rig.flame.media_panel.copy

    def first_only(source: Any, destination: Any, *rest: Any) -> Any:
        return real(source[:1] if isinstance(source, list) else source, destination, *rest)

    rig.flame.media_panel.copy = first_only
    before = fingerprint(rig)
    env = add_clips(rig, [CLIP1, CLIP2])
    assert is_partial(env), env
    assert env["result"]["applied"] == ["clip1"] and env["result"]["notApplied"] == ["clip2"]
    assert env["result"]["reelClipsAfter"] == ["clip1"]
    assert outcome_of(rig, env)["ok"] is False
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and fingerprint(rig) == before


def test_add_clips_that_adds_more_than_asked_is_partial(batch_rig: Rig) -> None:
    rig = batch_rig
    real = rig.flame.media_panel.copy

    def double(source: Any, destination: Any, *rest: Any) -> Any:
        real(source, destination, *rest)
        return real(source, destination, *rest)

    rig.flame.media_panel.copy = double
    env = add_clips(rig, [CLIP1])
    assert is_partial(env), env
    assert "gained 2" in env["error"]


def test_add_clips_into_a_reel_that_cannot_be_read_writes_nothing(
    batch_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = batch_rig
    reel = bg(rig).reels[0]

    def unreadable(self: Any) -> list[Any]:
        raise RuntimeError("reel is busy")

    monkeypatch.setattr(type(reel), "clips", property(unreadable))
    calls: list[Any] = []
    real = rig.flame.media_panel.copy

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real(*args, **kwargs)

    rig.flame.media_panel.copy = spy
    env = add_clips(rig, [CLIP1])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert calls == []


def test_add_clips_whose_reel_cannot_be_read_back_says_it_may_have_been_written(
    batch_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = batch_rig
    reel = bg(rig).reels[0]
    real = rig.flame.media_panel.copy
    state = {"broken": False}

    def break_after(self: Any) -> list[Any]:
        if state["broken"]:
            raise RuntimeError("reel is busy")
        return [c for c in self._children if type(c).__name__ == "PyClip"]

    def copy_then_break(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        state["broken"] = True
        return out

    monkeypatch.setattr(type(reel), "clips", property(break_after))
    rig.flame.media_panel.copy = copy_then_break
    env = add_clips(rig, [CLIP1])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "may have been written" in env["error"]
    assert not str(env["error"]).startswith("partial:")


def test_add_clips_that_adds_nothing_stays_silent_noop(batch_rig: Rig) -> None:
    batch_rig.flame.media_panel.copy = lambda source, destination: []
    env = add_clips(batch_rig, [CLIP1, CLIP2])
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"


# -- batch.iterate -------------------------------------------------------------------------------


def iteration_numbers(rig: Rig) -> list[int]:
    return [int(it.iteration_number) for it in bg(rig).batch_iterations]


def test_iterate_that_makes_a_different_number_than_asked_is_partial(rig: Rig) -> None:
    real = bg(rig).iterate
    bg(rig).iterate = lambda index=-1: real(index=7)  # type: ignore[method-assign]
    env = rig.do("batch.iterate", {"target": BG1, "index": 2})
    assert is_partial(env), env
    assert iteration_numbers(rig) == [7] and env["result"]["iteration"]["number"] == 7


def test_iterate_that_makes_two_iterations_is_partial(rig: Rig) -> None:
    real = bg(rig).iterate

    def twice(index: int = -1) -> PyBatchIteration:
        real()
        return real()

    bg(rig).iterate = twice  # type: ignore[method-assign]
    env = rig.do("batch.iterate", {"target": BG1})
    assert is_partial(env), env
    assert iteration_numbers(rig) == [1, 2]


def test_iterate_with_no_number_in_the_return_is_ok_with_a_note_when_one_appeared(
    rig: Rig,
) -> None:
    real = bg(rig).iterate

    def numberless(index: int = -1) -> Any:
        made = real(index)
        return SimpleNamespace(name=made.name, uid=made.uid, iteration_number=None)

    bg(rig).iterate = numberless  # type: ignore[method-assign]
    env = rig.do("batch.iterate", {"target": BG1})
    assert env["ok"] is True, env
    assert iteration_numbers(rig) == [1] and env["result"]["iteration"]["number"] == 1
    assert any("number" in note for note in env["degraded"]), env["degraded"]


def test_iterate_that_returns_an_iteration_the_list_does_not_have_is_silent_noop(rig: Rig) -> None:
    bg(rig).iterate = lambda index=-1: PyBatchIteration("x", "u", 9)  # type: ignore[method-assign]
    env = rig.do("batch.iterate", {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert iteration_numbers(rig) == []


# -- render.batch ----------------------------------------------------------------------------------


def add_writer(rig: Rig, tmp_path: Path, last: int = 8) -> PyWriteFileNode:
    out = tmp_path / "out_writefile1"
    out.mkdir(parents=True, exist_ok=True)
    writer = PyWriteFileNode("writefile1", media_path=str(out) + "/", first=1, last=last)
    batch = bg(rig)
    batch.nodes.append(writer)
    writer._owner = batch
    batch.connect_nodes(node(rig, "blur1"), "Default", writer, "Default")
    return writer


def add_render_node(rig: Rig) -> None:
    render_node = PyRenderNode("render1", 1, 24)
    bg(rig).nodes.append(render_node)
    render_node._owner = bg(rig)
    bg(rig).connect_nodes(node(rig, "blur1"), "Default", render_node, "Default")
    bg(rig).create_shelf_reel("Batch Renders")


def test_a_render_that_wrote_some_of_the_frames_is_partial(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_frames_limit = 5
    env = rig.do("render.batch", {"target": BG1})
    assert is_partial(env), env
    assert "5 of 8" in env["error"] and env["partial"] == {"written": 5, "expected": 8}
    assert env["result"]["writtenFrames"] == 5
    outcome = outcome_of(rig, env)
    assert outcome["ok"] is False and outcome["errorCode"] == "FLAME_API_ERROR"
    assert outcome["verdict"] == "partial"


def test_a_render_that_wrote_no_frame_stays_silent_noop(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_frames_limit = 0
    env = rig.do("render.batch", {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env


def test_a_render_node_only_render_is_ok_when_a_clip_landed_in_a_reel(rig: Rig) -> None:
    add_render_node(rig)
    shelf = bg(rig).shelf_reels[0]
    bg(rig).render_before_write = lambda batch: shelf._children.append(
        PyClip("render1", wiretap_id="/w/render1")
    )
    env = rig.do("render.batch", {"target": BG1})
    assert env["ok"] is True, env
    assert env["result"]["newClips"] == [
        {"kind": "shelf", "name": "Batch Renders", "clips": ["render1"]}
    ]


def test_a_render_node_only_render_that_gained_no_clip_is_silent_noop(rig: Rig) -> None:
    add_render_node(rig)
    env = rig.do("render.batch", {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["newClips"] == [] and "gained a clip" in env["error"]
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.state() == "failed"


def add_second_render_node(rig: Rig, destination: Any = None) -> PyRenderNode:
    second = PyRenderNode("render2", 1, 24)
    bg(rig).nodes.append(second)
    second._owner = bg(rig)
    bg(rig).connect_nodes(node(rig, "blur1"), "Default", second, "Default")
    if destination is not None:
        second.destination = destination
    return second


def land_clips(rig: Rig, *names: str) -> None:
    shelf = bg(rig).shelf_reels[0]
    bg(rig).render_before_write = lambda batch: shelf._children.extend(
        PyClip(n, wiretap_id=f"/w/{n}") for n in names
    )


def test_fewer_clips_than_render_nodes_for_a_reel_is_reported_as_missing_outputs(
    rig: Rig,
) -> None:
    add_render_node(rig)
    add_second_render_node(rig)
    land_clips(rig, "render1")
    env = rig.do("render.batch", {"target": BG1})
    assert env["ok"] is True, env
    assert env["result"]["missingOutputs"] == [
        {"reel": "Batch Renders", "expected": 2, "gained": 1, "nodes": ["render1", "render2"]}
    ]
    assert any("missingOutputs" in note for note in env["degraded"]), env["degraded"]


def test_as_many_clips_as_render_nodes_leaves_no_missing_outputs(rig: Rig) -> None:
    add_render_node(rig)
    add_second_render_node(rig)
    land_clips(rig, "render1", "render2")
    env = rig.do("render.batch", {"target": BG1})
    assert env["ok"] is True, env
    assert not env["result"].get("missingOutputs")
    assert not any("missingOutputs" in note for note in env["degraded"])


def test_a_render_node_whose_destination_cannot_be_read_is_not_counted(rig: Rig) -> None:
    add_render_node(rig)
    add_second_render_node(rig, destination=("Other Kind", "x"))
    land_clips(rig, "render1")
    env = rig.do("render.batch", {"target": BG1})
    assert env["ok"] is True, env
    assert not env["result"].get("missingOutputs")
    assert not any("missingOutputs" in note for note in env["degraded"]), env["degraded"]


def plan_node(name: str, reel: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "render",
        "effective": True,
        "destinationReel": reel,
        **extra,
    }


def desktop_dest(group: str = "RG", reel: str = "Reel 1", **extra: Any) -> dict[str, Any]:
    return {"kind": "desktop", "group": group, "reel": reel, "exists": True, **extra}


def test_a_desktop_reel_that_gained_fewer_clips_than_its_nodes_is_missing_outputs() -> None:
    plan = {"nodes": [plan_node("a", desktop_dest()), plan_node("b", desktop_dest())]}
    gained = [{"kind": "desktop", "name": "RG/Reel 1", "clips": ["a"]}]
    assert missing_outputs(plan, gained) == [
        {"reel": "RG/Reel 1", "expected": 2, "gained": 1, "nodes": ["a", "b"]}
    ]


def test_a_desktop_reel_that_gained_as_many_clips_as_its_nodes_is_not_listed() -> None:
    plan = {"nodes": [plan_node("a", desktop_dest()), plan_node("b", desktop_dest())]}
    gained = [{"kind": "desktop", "name": "RG/Reel 1", "clips": ["a", "b"]}]
    assert missing_outputs(plan, gained) == []


def test_a_clip_gained_by_a_reel_of_another_kind_with_the_same_name_is_not_counted() -> None:
    plan = {"nodes": [plan_node("a", desktop_dest())]}
    gained = [{"kind": "schematic", "name": "RG/Reel 1", "clips": ["a"]}]
    assert missing_outputs(plan, gained)[0]["gained"] == 0


def test_a_node_whose_reel_group_is_missing_is_not_counted() -> None:
    plan = {"nodes": [plan_node("a", desktop_dest(groupMissing=True, exists=False))]}
    assert missing_outputs(plan, []) == []


def test_a_node_whose_reel_could_not_be_read_is_not_counted() -> None:
    unread = plan_node("a", desktop_dest(exists=None))
    listed_unknown = plan_node("b", desktop_dest("RG", "Reel 2"))
    plan = {"nodes": [unread, listed_unknown]}
    unknown = [{"kind": "desktop", "name": "RG/Reel 2"}]
    assert missing_outputs(plan, [], unknown) == []
    assert len(missing_outputs(plan, [])) == 1


def test_a_reel_that_could_not_be_read_after_the_render_is_not_counted() -> None:
    plan = {"nodes": [plan_node("a", desktop_dest())]}
    unread = [{"kind": "desktop", "name": "RG/Reel 1", "clipCount": None}]
    readable = [{"kind": "desktop", "name": "RG/Reel 1", "clipCount": 0}]
    assert missing_outputs(plan, [], None, unread) == []
    assert len(missing_outputs(plan, [], None, readable)) == 1


def test_judge_render_rules() -> None:
    kwargs: dict[str, Any] = {"env_ok": True, "env_code": None, "env_error": None}
    some = judge_render(**kwargs, flame={"returned": True}, written=5, expected=8, checked=True)
    assert (some.ok, some.code) == (False, "FLAME_API_ERROR")
    assert some.message is not None and some.message.startswith("partial:")
    none = judge_render(**kwargs, flame={"returned": True}, written=0, expected=8, checked=True)
    assert (none.ok, none.code) == (False, "SILENT_NOOP")
    gained = {"returned": True, "newClips": [{"kind": "shelf", "name": "r", "clips": ["a"]}]}
    assert judge_render(**kwargs, flame=gained, written=0, expected=0, checked=False).ok is True
    empty = {"returned": True, "newClips": [], "newClipsUnknown": [], "destinationUnsupported": []}
    noop = judge_render(**kwargs, flame=empty, written=0, expected=0, checked=False)
    assert (noop.ok, noop.code) == (False, "SILENT_NOOP")
    unknown = {**empty, "newClipsUnknown": [{"kind": "shelf", "name": "r"}]}
    cannot = judge_render(**kwargs, flame=unknown, written=0, expected=0, checked=False)
    assert cannot.ok is True and cannot.degraded
    unsupported = {**empty, "destinationUnsupported": [{"node": "r", "destination": ["X", "y"]}]}
    assert judge_render(**kwargs, flame=unsupported, written=0, expected=0, checked=False).ok
    unread = judge_render(**kwargs, flame={"returned": True}, written=0, expected=0, checked=False)
    assert unread.ok is True


# -- render.clip -----------------------------------------------------------------------------------


@pytest.fixture
def clip_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def fx_clip(rig: Rig) -> PyClip:
    from fake_media_tree import PyTimelineFX, PyTrack, PyVersion, adopt
    from timeline_p32a_helpers import media

    container = rig.handles["reel"]
    segment = media("clip1", 0, 23, effects=(PyTimelineFX("Blur"),))
    made = PyClip(
        "BLURRED",
        wiretap_id=f"{container.get_wiretap_node_id()}/BLURRED",
        duration=24,
        versions=[PyVersion([PyTrack([segment])])],
    )
    adopt(container, [made])
    container._children.append(made)
    return made


def test_a_forced_render_of_a_rendered_clip_that_does_not_return_true_is_an_error(
    clip_rig: Rig,
) -> None:
    clip = fx_clip(clip_rig)
    target = {"kind": "wiretap", "id": clip.get_wiretap_node_id()}
    assert clip_rig.do("render.clip", {"target": target})["ok"] is True
    clip.render_returns = False
    env = clip_rig.do("render.clip", {"target": target, "force": True})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "already read true" in env["error"] and "cannot tell" in env["error"]
    result = env["result"]
    assert result["isRenderedBefore"] is True and result["readBackDecisive"] is False
    assert "read-back" in result["readBackNote"]


def test_a_forced_render_of_a_rendered_clip_that_returns_true_is_ok_and_says_the_read_back_is_blind(
    clip_rig: Rig,
) -> None:
    clip = fx_clip(clip_rig)
    target = {"kind": "wiretap", "id": clip.get_wiretap_node_id()}
    clip_rig.do("render.clip", {"target": target})
    env = clip_rig.do("render.clip", {"target": target, "force": True})
    assert env["ok"] is True, env
    assert env["result"]["readBackDecisive"] is False


def test_a_first_render_that_returns_false_but_reads_rendered_is_ok_with_a_note(
    clip_rig: Rig,
) -> None:
    clip = fx_clip(clip_rig)
    clip.render_returns = False
    target = {"kind": "wiretap", "id": clip.get_wiretap_node_id()}
    env = clip_rig.do("render.clip", {"target": target})
    assert env["ok"] is True and env["result"]["readBackDecisive"] is True
    (note,) = env["degraded"]
    assert "not True" in note


# -- timeline.overwrite / insert / cut -------------------------------------------------------------


@pytest.fixture
def tl_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    from p33_helpers import add_media_clip

    made = make_timeline_rig(monkeypatch, tmp_path)
    made.handles["c12"] = add_media_clip(made, "len12", start=0, length=12)
    return made


def put(rig: Rig, op: str, at: int) -> dict[str, Any]:
    from p33_helpers import clip_sel

    return rig.do(op, {"sequence": S1, "source": clip_sel(rig.handles["c12"]), "at": at})


def placed_wrongly(rig: Rig, op: str, how: str) -> dict[str, Any]:
    seq = rig.handles["s1"]
    method = op.split(".")[1]
    real = getattr(seq, method)

    def wrapper(clip: Any, time: Any, track: Any) -> Any:
        if how == "elsewhere":
            return real(clip, PyTime(time.frame + 2), track)
        out = real(clip, time, track)
        placed = next(
            s for s in track._segments if s.record_in.frame == time.frame and s.type != "Gap"
        )
        if how == "renamed":
            placed.source_name = "someone_else"
        else:  # "short"
            placed._set_record(placed.record_in.frame, placed.record_out.frame - 3)
        return out

    setattr(seq, method, wrapper)
    return put(rig, op, 20)


@pytest.mark.parametrize("op", ["timeline.overwrite", "timeline.insert"])
@pytest.mark.parametrize("how", ["elsewhere", "renamed", "short"])
def test_a_put_that_changed_the_sequence_but_not_where_asked_is_partial(
    tl_rig: Rig, op: str, how: str
) -> None:
    env = placed_wrongly(tl_rig, op, how)
    assert is_partial(env), env
    assert env["result"]["changed"] is True
    assert outcome_of(tl_rig, env)["ok"] is False
    back = tl_rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back


@pytest.mark.parametrize("op", ["timeline.overwrite", "timeline.insert"])
def test_a_put_that_lands_where_asked_stays_ok(tl_rig: Rig, op: str) -> None:
    env = put(tl_rig, op, 20)
    assert env["ok"] is True, env
    assert env["result"]["warnings"] == [] and env["result"]["placed"]["recordIn"] == 20


def test_a_put_of_a_clip_with_marks_is_judged_on_the_marked_length(tl_rig: Rig) -> None:
    clip: PyClip = tl_rig.handles["c12"]
    clip._media = (0, 24)
    clip.in_mark, clip.out_mark = PyTime(4), PyTime(16)
    env = put(tl_rig, "timeline.overwrite", 20)
    assert env["ok"] is True, env


def test_a_whole_sequence_cut_that_splits_only_some_tracks_is_partial(tl_rig: Rig) -> None:
    seq = tl_rig.handles["s1"]

    def one_track_only(time: Any) -> None:
        seq.versions[0].tracks[0].cut(time)

    seq.cut = one_track_only
    env = tl_rig.do("timeline.cut", {"sequence": S1, "at": 20, "scope": "all"})
    assert is_partial(env), env
    assert env["result"]["split"] == [{"version": 0, "track": 0}]
    assert env["result"]["notSplit"] == [{"version": 0, "track": 1}]
    assert outcome_of(tl_rig, env)["ok"] is False
    back = tl_rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back


# -- batch.load_setup: the fingerprint read-back after the load (rule 4) ---------------------------


def test_load_setup_whose_group_cannot_be_read_back_says_it_may_have_been_replaced(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = rig.do("batch.checkpoint", {"target": BG1})["result"]
    bg(rig).create_node("Mux")
    real = checkpoints.read_fingerprint

    def broken(ctx: Any, stable: Any, kind: Any = None, **kw: Any) -> dict[str, Any]:
        return {"ok": False, "error": "group is busy", "errorCode": "FLAME_API_ERROR"}

    monkeypatch.setattr("flame_mcp.checkpointed.read_fingerprint", broken)
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    monkeypatch.setattr("flame_mcp.checkpointed.read_fingerprint", real)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "fingerprint" in env["error"] and "could not be read" in env["error"]
    assert "may have been" in env["error"]
    outcome = outcome_of(rig, env)
    assert outcome["ok"] is False and outcome["fingerprintAfter"] is None


def test_load_setup_with_a_readable_group_is_ok(rig: Rig) -> None:
    saved = rig.do("batch.checkpoint", {"target": BG1})["result"]
    bg(rig).create_node("Mux")
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["ok"] is True, env
