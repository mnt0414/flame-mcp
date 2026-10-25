"""Tests for `desktop.arrange`: a reel built one copy / move at a time, in the order given.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree (see
media_p31b_helpers.py). The fake follows what was measured on Flame 2025.2.4: a test here
describes the fake, and whether the real Flame does the same is what scripts/p2_3d_desktop_e2e.py
measures."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyFolder, adopt
from flame_mcp import execution
from flame_mcp.desktop_arrange_run import MAX_ARRANGE_ITEMS
from flame_mcp.journal import Index
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from journal_test_helpers import Rig, make_rig
from media_p31b_helpers import (
    C1,
    CLIP1,
    LIB,
    LIB_WT,
    LIBREEL,
    REEL1,
    add_clip,
    batch_reel,
    make_media_rig,
    names,
    own_source,
    reel2_selector,
    revert,
    states,
    wt,
)

ARRANGE = "desktop.arrange"
COPY = "desktop.copy"
MOVE = "desktop.move"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_media_rig(monkeypatch, tmp_path)


def item(action: str, source: dict[str, Any]) -> dict[str, Any]:
    return {"action": action, "source": source}


def arrange(rig: Rig, items: list[dict[str, Any]], destination: dict[str, Any]) -> dict[str, Any]:
    return rig.do(ARRANGE, {"destination": destination, "items": items})


def reel1(rig: Rig) -> Any:
    return rig.handles["reel_group"].reels[0]


def three_clips(rig: Rig) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reel 1 holds c1, c2, c3; the selectors of the three."""
    c2 = add_clip(reel1(rig), "c2")
    c3 = add_clip(reel1(rig), "c3")
    return C1, wt(c2), wt(c3)


def order(env: dict[str, Any]) -> list[str]:
    return [row["name"] for row in env["result"]["order"]]


def kinds(rig: Rig) -> list[str]:
    return [str(r.get("kind")) for r in rig.records() if r.get("type") == "intent"]


def test_arrange_moves_three_clips_into_the_reel_in_the_order_given(rig: Rig) -> None:
    c1, c2, c3 = three_clips(rig)
    env = arrange(rig, [item("move", c3), item("move", c1), item("move", c2)], reel2_selector(rig))
    assert env["ok"] is True, env
    assert names(rig.handles["reel2"]) == ["c3", "c1", "c2"]
    assert names(reel1(rig)) == []
    result = env["result"]
    assert result["requested"] == 3 and result["orderMatches"] is True
    assert order(env) == ["c3", "c1", "c2"]
    assert [a["action"] for a in result["applied"]] == ["move", "move", "move"]
    assert [a["made"]["name"] for a in result["applied"]] == ["c3", "c1", "c2"]
    assert env["affected"] == 3 and env["undoSteps"] == 0
    assert env["execution"]["effect"] == "create"
    assert kinds(rig) == ["desktop_move"] * 3
    group = env["journal"]["group"]
    assert {r["group"] for r in rig.records() if r.get("type") == "intent"} == {group}
    assert env["journal"]["revert"] == {"op": "journal.revert", "args": {"group": group}}


def test_arrange_mixes_copy_and_move_and_the_copy_source_stays(rig: Rig) -> None:
    c1, c2, c3 = three_clips(rig)
    items = [item("move", c3), item("copy", c1), item("move", c2), item("copy", c1)]
    env = arrange(rig, items, reel2_selector(rig))
    assert env["ok"] is True, env
    assert names(rig.handles["reel2"]) == ["c3", "c1", "c2", "c1"]
    assert names(reel1(rig)) == ["c1"]
    assert kinds(rig) == ["desktop_move", "desktop_copy", "desktop_move", "desktop_copy"]
    uids = [row["uid"] for row in env["result"]["order"]]
    assert len(set(uids)) == 4


def test_arrange_copies_the_same_source_twice_as_two_separate_clips(rig: Rig) -> None:
    c1, c2, _c3 = three_clips(rig)
    env = arrange(rig, [item("copy", c1), item("copy", c2), item("copy", c1)], reel2_selector(rig))
    assert env["ok"] is True, env
    assert names(rig.handles["reel2"]) == ["c1", "c2", "c1"]
    assert names(reel1(rig)) == ["c1", "c2", "c3"]
    calls = rig.flame.media_panel.copy_calls
    assert len(calls) == 3 and all(not isinstance(src, list) for src, _dest in calls)


def test_arrange_puts_the_items_after_what_the_reel_already_holds(rig: Rig) -> None:
    c1, c2, c3 = three_clips(rig)
    add_clip(rig.handles["reel2"], "keep1")
    add_clip(rig.handles["reel2"], "keep2")
    env = arrange(rig, [item("move", c3), item("copy", c1)], reel2_selector(rig))
    assert env["ok"] is True, env
    assert names(rig.handles["reel2"]) == ["keep1", "keep2", "c3", "c1"]
    assert order(env) == ["keep1", "keep2", "c3", "c1"]
    assert env["result"]["existing"] == 2
    assert c2 is not None


def test_each_item_is_its_own_flame_write_and_the_generated_code_has_no_loop(rig: Rig) -> None:
    c1, c2, c3 = three_clips(rig)
    arrange(rig, [item("move", c3), item("copy", c1), item("move", c2)], reel2_selector(rig))
    writes = [
        own_source(c.code)
        for c in rig.exec.calls
        if "_result = _dm_move(" in own_source(c.code)
        or "_result = _dc_copy(" in own_source(c.code)
    ]
    assert len(writes) == 3
    assert all("_src1" not in source for source in writes)
    loops = (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    for call in rig.exec.calls:
        if call.label == ARRANGE:
            assert not any(isinstance(n, loops) for n in ast.walk(ast.parse(own_source(call.code))))
        assert call.code.isascii()


def test_arrange_writes_a_group_record_and_every_step_into_the_same_group(rig: Rig) -> None:
    c1, c2, _c3 = three_clips(rig)
    env = arrange(rig, [item("copy", c1), item("move", c2)], reel2_selector(rig))
    records = rig.records()
    first = records[0]
    assert first["type"] == "group" and first["kind"] == ARRANGE
    assert first["group"] == env["journal"]["group"]
    assert [r["op"] for r in records if r.get("type") == "intent"] == [COPY, MOVE]
    assert env["journal"]["ids"] == [r["id"] for r in records if r.get("type") == "intent"]


def test_arrange_stops_at_the_first_failing_item_and_the_earlier_ones_stay_applied(
    rig: Rig,
) -> None:
    c1, c2, c3 = three_clips(rig)
    c2_object = rig.flame.find_by_wiretap_node_id(c2["id"])
    gone = {"done": False}

    def user_deletes_c2(label: str | None) -> None:
        if label == MOVE and len(rig.flame.media_panel.move_calls) == 1 and not gone["done"]:
            gone["done"] = True
            reel1(rig)._children.remove(c2_object)
            rig.flame.forget_wiretap(c2_object)

    rig.exec.before_run = user_deletes_c2
    env = arrange(rig, [item("move", c3), item("copy", c1), item("move", c2)], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    result = env["result"]
    assert result["stoppedAt"] == 2 and result["failed"]["index"] == 2
    assert result["failed"]["action"] == "move"
    assert [a["index"] for a in result["applied"]] == [0, 1]
    assert env["partial"] == {"done": 2, "failed": 1, "notRun": 0}
    assert names(rig.handles["reel2"]) == ["c3", "c1"]
    group = env["journal"]["group"]
    assert env["journal"]["revert"] == {"op": "journal.revert", "args": {"group": group}}
    assert "journal.revert" in env["hint"] and group in env["hint"]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert names(rig.handles["reel2"]) == [] and names(reel1(rig)) == ["c1", "c3"]


def test_a_timeout_stops_the_run_without_a_retry_and_shuts_the_write_door(rig: Rig) -> None:
    c1, c2, c3 = three_clips(rig)
    # reads of desktop.move: the pre-pass for item 1 (#0), then item 1's own read (#1) and write
    # (#2): the write of item 1 is the one whose answer is lost after it ran.
    rig.exec.fault(MOVE, "TIMEOUT", when="after", skip=2)
    env = arrange(rig, [item("copy", c1), item("move", c2), item("copy", c3)], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert env["result"]["stoppedAt"] == 1
    assert [a["index"] for a in env["result"]["applied"]] == [0]
    assert len(rig.flame.media_panel.move_calls) == 1  # never retried
    assert len(rig.flame.media_panel.copy_calls) == 1  # item 2 never started
    assert "journal.check" in env["hint"]
    again = arrange(rig, [item("copy", c1)], reel2_selector(rig))
    assert again["errorCode"] == "POLICY_DENIED"
    group = env["journal"]["group"]
    check = rig.do("journal.check", {"group": group})
    assert check["ok"] is True, check
    after = arrange(rig, [item("copy", c1)], reel2_selector(rig))
    assert after["ok"] is True, after


def test_a_move_inside_the_destination_reel_is_refused_and_nothing_is_written(rig: Rig) -> None:
    c1, c2, c3 = three_clips(rig)
    env = arrange(rig, [item("copy", c3), item("move", c1)], REEL1)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "items[1]" in env["error"] and "already in the destination" in env["error"]
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []
    assert rig.flame.media_panel.copy_calls == [] and c2 is not None


def test_a_copy_inside_the_destination_reel_is_allowed(rig: Rig) -> None:
    c1, _c2, _c3 = three_clips(rig)
    env = arrange(rig, [item("copy", c1)], REEL1)
    assert env["ok"] is True, env
    assert names(reel1(rig)) == ["c1", "c2", "c3", "c1"]


def test_moving_one_source_twice_or_using_it_after_its_move_is_refused(rig: Rig) -> None:
    c1, c2, _c3 = three_clips(rig)
    by_path = {"kind": "path", "root": "desktop", "path": ["RG", "Reel 1", "c1"]}
    for items in (
        [item("move", c1), item("move", c1)],
        [item("move", c1), item("copy", c1)],
        [item("move", c1), item("move", by_path)],
        [item("copy", c2), item("move", c1), item("copy", by_path)],
    ):
        env = arrange(rig, items, reel2_selector(rig))
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", (items, env)
        assert "after it was moved" in env["error"] or "moved twice" in env["error"]
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []


def test_copying_a_source_and_then_moving_it_is_allowed(rig: Rig) -> None:
    c1, _c2, _c3 = three_clips(rig)
    env = arrange(rig, [item("copy", c1), item("move", c1)], reel2_selector(rig))
    assert env["ok"] is True, env
    assert names(rig.handles["reel2"]) == ["c1", "c1"] and names(reel1(rig)) == ["c2", "c3"]


def test_the_item_count_is_limited(rig: Rig) -> None:
    assert MAX_ARRANGE_ITEMS == 100
    for items in ([], [item("copy", C1)] * (MAX_ARRANGE_ITEMS + 1)):
        env = arrange(rig, items, reel2_selector(rig))
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
        assert f"1 to {MAX_ARRANGE_ITEMS}" in env["error"]
    assert rig.exec.calls == []


@pytest.mark.parametrize(
    ("items", "needle"),
    [
        ([{"action": "paste", "source": C1}], "items[0].action"),
        ([{"action": "copy"}], "items[0].source"),
        ([{"action": "copy", "source": C1, "at": 3}], "items[0]"),
        (["copy"], "items[0]"),
        ([{"action": "copy", "source": {"kind": "batch", "name": "BG1"}}], "items[0].source"),
    ],
)
def test_bad_items_are_refused_before_anything_runs(
    rig: Rig, items: list[Any], needle: str
) -> None:
    env = arrange(rig, items, reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert needle in env["error"]
    assert rig.exec.calls == []


def test_the_destination_must_be_a_desktop_reel_and_not_a_batch_reel(rig: Rig) -> None:
    folder = PyFolder("F", "uid-f", f"{LIB_WT}/F")
    rig.handles["lib"]._children.append(folder)
    adopt(rig.handles["lib"], [folder])
    batch = batch_reel(rig)
    for destination, code, needle in (
        (wt(batch), "INVALID_ARGS", "Batch group"),
        (LIBREEL, "PRECONDITION_UNMET", "library"),
        (LIB, "INVALID_ARGS", "PyLibrary"),
        ({"kind": "wiretap", "id": f"{LIB_WT}/F"}, "INVALID_ARGS", "PyFolder"),
        (C1, "INVALID_ARGS", "PyClip"),
    ):
        env = arrange(rig, [item("copy", C1)], destination)
        assert env["ok"] is False and env["errorCode"] == code, (destination, env)
        assert needle in env["error"], env["error"]
    assert rig.records() == [] and rig.flame.media_panel.copy_calls == []


def test_a_copy_may_take_its_source_out_of_a_library_but_a_move_may_not(rig: Rig) -> None:
    ok = arrange(rig, [item("copy", CLIP1)], reel2_selector(rig))
    assert ok["ok"] is True, ok
    assert names(rig.handles["reel2"]) == ["clip1"]
    refused = arrange(rig, [item("copy", C1), item("move", CLIP1)], reel2_selector(rig))
    assert refused["ok"] is False and refused["errorCode"] == "PRECONDITION_UNMET"
    assert "items[1]" in refused["error"] and "inside a library" in refused["error"]
    assert names(rig.handles["reel2"]) == ["clip1"] and len(rig.flame.media_panel.copy_calls) == 1


def test_a_source_that_does_not_resolve_stops_the_call_before_any_write(rig: Rig) -> None:
    c1, _c2, _c3 = three_clips(rig)
    missing = {"kind": "wiretap", "id": "/wt/nothing"}
    env = arrange(rig, [item("copy", c1), item("copy", missing)], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert "items[1]" in env["error"]
    assert rig.records() == [] and rig.flame.media_panel.copy_calls == []


def test_a_reel_that_comes_back_in_another_order_is_a_flame_api_error_with_the_order(
    rig: Rig,
) -> None:
    c1, c2, c3 = three_clips(rig)
    reads = {"arrange": 0}

    def flame_reorders(label: str | None) -> None:
        if label == ARRANGE:
            reads["arrange"] += 1
            if reads["arrange"] == 2:  # the read-back after the writes
                rig.handles["reel2"]._children.reverse()

    rig.exec.before_run = flame_reorders
    env = arrange(rig, [item("move", c3), item("move", c1), item("copy", c2)], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    result = env["result"]
    assert result["orderMatches"] is False
    assert order(env) == ["c2", "c1", "c3"]
    assert [row["name"] for row in result["expected"]] == ["c3", "c1", "c2"]
    assert len(result["applied"]) == 3
    group = env["journal"]["group"]
    assert env["journal"]["revert"] == {"op": "journal.revert", "args": {"group": group}}
    assert group in env["hint"]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert names(rig.handles["reel2"]) == [] and sorted(names(reel1(rig))) == ["c1", "c2", "c3"]


def test_revert_goes_newest_first_and_the_source_keeps_its_order_when_its_last_clips_moved(
    rig: Rig,
) -> None:
    c1, c2, c3 = three_clips(rig)
    items = [item("move", c3), item("copy", c1), item("move", c2), item("copy", c1)]
    env = arrange(rig, items, reel2_selector(rig))
    assert env["ok"] is True, env
    group = env["journal"]["group"]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert names(rig.handles["reel2"]) == []
    assert names(reel1(rig)) == ["c1", "c2", "c3"]
    result = out["result"]
    assert result["reverted"] == group
    assert [s["index"] for s in result["steps"]] == [3, 2, 1, 0]
    assert [s["action"] for s in result["steps"]] == ["copy", "move", "copy", "move"]
    assert out["affected"] == 4
    assert states(rig)[group] == "reverted"
    again = revert(rig, env)
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"
    index = Index(rig.records())
    assert index.is_reverted(group) and not index.is_partially_reverted(group)


def test_a_revert_that_hits_a_changed_copy_puts_back_the_rest_and_a_second_revert_finishes(
    rig: Rig,
) -> None:
    c1, c2, c3 = three_clips(rig)
    env = arrange(rig, [item("move", c3), item("copy", c1), item("move", c2)], reel2_selector(rig))
    copy_selector = env["result"]["applied"][1]["made"]["selector"]
    assert rig.do("desktop.rename", {"target": copy_selector, "name": "edited"})["ok"] is True
    group = env["journal"]["group"]
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert out["partial"] == {"done": 2, "failed": 1, "notRun": 0}
    assert names(rig.handles["reel2"]) == ["edited"]
    assert states(rig)[group] == "partially_reverted"
    assert rig.do("desktop.rename", {"target": copy_selector, "name": "c1"})["ok"] is True
    finish = revert(rig, env)
    assert finish["ok"] is True, finish
    assert [s["index"] for s in finish["result"]["steps"]] == [1]
    assert names(rig.handles["reel2"]) == [] and sorted(names(reel1(rig))) == ["c1", "c2", "c3"]
    assert states(rig)[group] == "reverted"


def test_a_single_item_is_one_step_and_reverts_like_it(rig: Rig) -> None:
    c1, _c2, _c3 = three_clips(rig)
    env = arrange(rig, [item("copy", c1)], reel2_selector(rig))
    assert env["ok"] is True and names(rig.handles["reel2"]) == ["c1"]
    out = revert(rig, env)
    assert out["ok"] is True and names(rig.handles["reel2"]) == []


def test_arrange_is_refused_in_plan_run(rig: Rig) -> None:
    args = {"destination": REEL1, "items": [item("copy", C1)]}
    env = rig.do("plan.run", {"steps": [{"op": ARRANGE, "args": args}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert f"{ARRANGE} cannot be a plan step" in env["result"]["issues"][0]["problems"][0]
    assert env["result"]["ran"] is False and rig.exec.calls == []


def test_readonly_policy_denies_arrange(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    made = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = arrange(made, [item("copy", C1)], REEL1)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert made.exec.calls == [] and made.records() == []


def test_the_write_door_shut_by_an_unknown_write_refuses_arrange(rig: Rig) -> None:
    rig.runtime.guard.block("i_x", "g_x", COPY, "TIMEOUT")
    env = arrange(rig, [item("copy", C1)], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert "journal.check" in env["hint"]
    assert rig.records() == [] and rig.flame.media_panel.copy_calls == []


def test_declaration_of_arrange() -> None:
    registry = Registry()
    register_all(registry)
    op = registry.get(ARRANGE)
    assert op is not None
    assert (op.effect, op.thread, op.undo, op.category) == ("create", "main", "value", "desktop")
    assert op.undo_steps({}, 3) == 0
    assert op.precheck is not None and op.wrap is not None and op.to_python is not None
    for word in ("journal.revert", "plan.run", "FLAME_API_ERROR", "Native Ctrl+Z"):
        assert word in op.description, word
    own = op.to_python({"destination": REEL1, "items": [item("copy", C1)]})
    ast.parse(own)
    ast.parse(execution.build_exec_code(op, {"destination": REEL1, "items": [item("copy", C1)]}))


def test_a_revert_with_a_conflict_and_a_timeout_reports_the_timeout_and_names_its_group(
    rig: Rig,
) -> None:
    c1, c2, c3 = three_clips(rig)
    env = arrange(rig, [item("move", c3), item("copy", c1), item("move", c2)], reel2_selector(rig))
    group = env["journal"]["group"]
    copy_selector = env["result"]["applied"][1]["made"]["selector"]
    assert rig.do("desktop.rename", {"target": copy_selector, "name": "edited"})["ok"] is True
    # the revert goes: move of item 2 (MOVE call 0), the copy of item 1 (a CONFLICT, no MOVE
    # call), then the move of item 0 (MOVE call 1) whose answer is lost
    rig.exec.fault(MOVE, "TIMEOUT", when="after", skip=1)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "TIMEOUT", out
    unresolved = out["result"]["unresolved"]
    assert len(unresolved) == 1 and unresolved[0]["errorCode"] == "TIMEOUT"
    lost = unresolved[0]["group"]
    assert lost in out["journal"]["groups"] and out["journal"]["group"] == lost
    assert [f["errorCode"] for f in out["result"]["notReverted"]] == ["CONFLICT", "TIMEOUT"]
    assert out["partial"] == {"done": 1, "failed": 2, "notRun": 0}
    assert rig.runtime.guard.denial() is not None
    check = rig.do("journal.check", {"group": lost})
    assert check["ok"] is True, check
    assert rig.runtime.guard.denial() is None
    assert group != lost


def test_moving_the_first_clip_of_a_reel_and_reverting_puts_it_at_the_end_of_that_reel(
    rig: Rig,
) -> None:
    c1, _c2, _c3 = three_clips(rig)
    env = arrange(rig, [item("move", c1)], reel2_selector(rig))
    assert env["ok"] is True and names(reel1(rig)) == ["c2", "c3"]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert names(reel1(rig)) == ["c2", "c3", "c1"]  # the end of the reel, not its old place
    assert any("END" in note for note in out["result"]["notes"])


@pytest.mark.parametrize("action", ["copy", "move"])
def test_a_reel_or_a_library_as_a_source_is_refused_before_anything_is_written(
    rig: Rig, action: str
) -> None:
    c1, _c2, _c3 = three_clips(rig)
    for source in (REEL1, LIB):
        env = arrange(rig, [item("copy", c1), item(action, source)], reel2_selector(rig))
        assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE", (source, env)
        assert "items[1]" in env["error"]
    assert rig.records() == [] and rig.flame.media_panel.copy_calls == []


def test_a_selection_source_is_pinned_by_wiretap_before_the_first_write(rig: Rig) -> None:
    c1, c2, _c3 = three_clips(rig)
    first = rig.flame.find_by_wiretap_node_id(c1["id"])
    other = rig.flame.find_by_wiretap_node_id(c2["id"])
    rig.flame.media_panel.selected_entries = [first]

    def selection_moves_on(label: str | None) -> None:
        if label == COPY and len(rig.flame.media_panel.copy_calls) == 1:
            rig.flame.media_panel.selected_entries = [other]

    rig.exec.before_run = selection_moves_on
    sel = {"kind": "selection"}
    env = arrange(rig, [item("copy", sel), item("copy", sel)], reel2_selector(rig))
    assert env["ok"] is True, env
    assert names(rig.handles["reel2"]) == ["c1", "c1"]
    intents = [r for r in rig.records() if r.get("type") == "intent"]
    assert [i["args"]["sources"] for i in intents] == [[c1], [c1]]
    codes = [own_source(c.code) for c in rig.exec.calls if "_dc_copy(" in own_source(c.code)]
    assert len(codes) == 2
    assert all("_resolve_selection" not in code and "_resolve_wiretap" in code for code in codes)


def test_a_failure_in_the_middle_leaves_the_items_after_it_not_run(rig: Rig) -> None:
    c1, c2, c3 = three_clips(rig)
    c2_object = rig.flame.find_by_wiretap_node_id(c2["id"])
    gone = {"done": False}

    def user_deletes_c2(label: str | None) -> None:
        if label == MOVE and len(rig.flame.media_panel.copy_calls) == 1 and not gone["done"]:
            gone["done"] = True
            reel1(rig)._children.remove(c2_object)
            rig.flame.forget_wiretap(c2_object)

    rig.exec.before_run = user_deletes_c2
    items = [item("copy", c1), item("move", c2), item("copy", c3), item("copy", c1)]
    env = arrange(rig, items, reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    result = env["result"]
    assert result["stoppedAt"] == 1 and result["notRun"] == [2, 3]
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 2}
    assert [a["index"] for a in result["applied"]] == [0]
    assert names(rig.handles["reel2"]) == ["c1"] and len(rig.flame.media_panel.copy_calls) == 1


def test_reverting_a_reverted_group_is_refused_and_changes_nothing(rig: Rig) -> None:
    c1, c2, _c3 = three_clips(rig)
    env = arrange(rig, [item("copy", c1), item("move", c2)], reel2_selector(rig))
    assert revert(rig, env)["ok"] is True
    records, calls = len(rig.records()), len(rig.exec.calls)
    reel1_names, reel2_names = names(reel1(rig)), names(rig.handles["reel2"])
    again = revert(rig, env)
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"
    assert len(rig.records()) == records and len(rig.exec.calls) == calls
    assert names(reel1(rig)) == reel1_names and names(rig.handles["reel2"]) == reel2_names


def _keeps_the_uid(rig: Rig) -> None:
    """A move that leaves the thing with its old uid: the move's own answer carries a note."""

    def keeps_the_uid(source: Any, destination: Any) -> list[Any]:
        sources = list(source) if isinstance(source, list) else [source]
        for obj in sources:
            obj.parent._children = [c for c in obj.parent._children if c is not obj]
            adopt(destination, [obj])
            destination._children.append(obj)
        return sources

    rig.flame.media_panel.move = keeps_the_uid


def test_the_notes_of_the_steps_are_kept_when_a_later_step_fails(rig: Rig) -> None:
    c1, c2, c3 = three_clips(rig)
    _keeps_the_uid(rig)
    c3_object = rig.flame.find_by_wiretap_node_id(c3["id"])
    gone = {"done": False}

    def user_deletes_c3(label: str | None) -> None:
        # the first desktop.copy call after the group record is item 1's own read
        if label == COPY and rig.records() and not gone["done"]:
            gone["done"] = True
            reel1(rig)._children.remove(c3_object)
            rig.flame.forget_wiretap(c3_object)

    rig.exec.before_run = user_deletes_c3
    env = arrange(rig, [item("move", c2), item("copy", c3)], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert any(note.startswith("items[0]: ") for note in env["degraded"]), env["degraded"]
    assert c1 is not None


def test_the_notes_of_the_steps_are_kept_when_the_order_does_not_match(rig: Rig) -> None:
    c1, c2, _c3 = three_clips(rig)
    _keeps_the_uid(rig)
    reads = {"arrange": 0}

    def flame_reorders(label: str | None) -> None:
        if label == ARRANGE:
            reads["arrange"] += 1
            if reads["arrange"] == 2:
                rig.handles["reel2"]._children.reverse()

    rig.exec.before_run = flame_reorders
    env = arrange(rig, [item("move", c2), item("copy", c1)], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert any(note.startswith("items[0]: ") for note in env["degraded"]), env["degraded"]
