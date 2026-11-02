"""Tests for `desktop.reorder`: a reel put into another order by the fewest moves to its end.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree (see
media_p31b_helpers.py). The fake follows what was measured on Flame 2025.2.4: a move into the reel
an entry is in puts it at the end with a new uid, and an entry that is not moved keeps its uid."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyFolder, adopt
from flame_mcp.desktop_reorder_run import MAX_REORDER_MOVES
from flame_mcp.journal import Index
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from journal_test_helpers import Rig, make_rig
from media_p31b_helpers import (
    C1,
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

REORDER = "desktop.reorder"
MOVE = "desktop.move"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_media_rig(monkeypatch, tmp_path)


def reel2(rig: Rig) -> Any:
    return rig.handles["reel2"]


def fill(rig: Rig, *clip_names: str) -> dict[str, dict[str, Any]]:
    """Reel 2 holds the clips in this order; their wiretap selectors by name."""
    return {n: wt(add_clip(reel2(rig), n)) for n in clip_names}


def uid_of(rig: Rig) -> dict[str, str]:
    return {str(c.name.get_value()): str(c.uid.get_value()) for c in reel2(rig).children}


def reorder(rig: Rig, order: list[Any], destination: Any = None) -> dict[str, Any]:
    return rig.do(REORDER, {"destination": destination or reel2_selector(rig), "order": order})


def by_name(rig: Rig, *wanted: str) -> list[str]:
    """The uids of the named clips of reel 2, in the order named."""
    uids = uid_of(rig)
    return [uids[n] for n in wanted]


def test_one_entry_that_goes_last_is_the_only_one_moved_and_the_others_keep_their_uid(
    rig: Rig,
) -> None:
    fill(rig, "a", "b", "c")
    before = uid_of(rig)
    env = reorder(rig, by_name(rig, "b", "c", "a"))
    assert env["ok"] is True, env
    assert names(reel2(rig)) == ["b", "c", "a"]
    after = uid_of(rig)
    assert after["b"] == before["b"] and after["c"] == before["c"]
    assert after["a"] != before["a"]
    result = env["result"]
    assert result["moved"] == 1 and result["orderMatches"] is True
    assert [r["name"] for r in result["before"]] == ["a", "b", "c"]
    assert [r["name"] for r in result["after"]] == ["b", "c", "a"]
    assert [r["uid"] for r in result["after"]] == [after["b"], after["c"], after["a"]]
    (mapped,) = result["uidMap"]
    assert mapped["name"] == "a" and mapped["from"] == before["a"] and mapped["to"] == after["a"]
    assert mapped["selector"]["kind"] == "wiretap"
    assert env["affected"] == 1 and env["undoSteps"] == 0
    assert env["execution"]["effect"] == "create"
    assert any("uid" in note for note in result["notes"])
    assert len(rig.flame.media_panel.move_calls) == 1


def test_several_moves_are_one_call_in_the_wanted_order(rig: Rig) -> None:
    fill(rig, "a", "b", "c", "d")
    env = reorder(rig, by_name(rig, "d", "a", "b", "c"))
    assert env["ok"] is True, env
    assert names(reel2(rig)) == ["d", "a", "b", "c"]
    assert env["result"]["moved"] == 3
    (call,) = rig.flame.media_panel.move_calls
    assert [str(s.name.get_value()) for s in call[0]] == ["a", "b", "c"]
    assert [m["name"] for m in env["result"]["uidMap"]] == ["a", "b", "c"]


def test_an_order_that_is_already_right_writes_nothing_and_is_ok_with_zero_moved(rig: Rig) -> None:
    fill(rig, "a", "b", "c")
    before = uid_of(rig)
    env = reorder(rig, by_name(rig, "a", "b", "c"))
    assert env["ok"] is True, env
    assert env["result"]["moved"] == 0 and env["result"]["uidMap"] == []
    assert env["result"]["orderMatches"] is True
    assert env["affected"] == 0 and env.get("journal") is None
    assert uid_of(rig) == before
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []


def test_the_order_may_name_entries_by_wiretap_selector_or_uid(rig: Rig) -> None:
    sel = fill(rig, "a", "b", "c")
    uids = uid_of(rig)
    env = reorder(rig, [sel["c"], uids["a"], sel["b"]])
    assert env["ok"] is True, env
    assert names(reel2(rig)) == ["c", "a", "b"]


@pytest.mark.parametrize(
    ("make_order", "needle"),
    [
        (lambda u: [u["a"], u["b"]], "missing"),
        (lambda u: [u["a"], u["b"], u["c"], "nope"], "unknown"),
        (lambda u: [u["a"], u["b"], "nope"], "unknown"),
        (lambda u: [u["a"], u["b"], u["b"]], "twice"),
    ],
)
def test_an_order_that_is_not_exactly_the_entries_of_the_reel_is_refused_before_any_write(
    rig: Rig, make_order: Any, needle: str
) -> None:
    fill(rig, "a", "b", "c")
    before = names(reel2(rig))
    env = reorder(rig, make_order(uid_of(rig)))
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert needle in env["error"]
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []
    assert names(reel2(rig)) == before


def test_bad_arguments_are_refused_by_the_precheck(rig: Rig) -> None:
    fill(rig, "a")
    for order in ([], "a", [1], [{"kind": "selection"}], [{"kind": "path", "path": []}], [""]):
        env = reorder(rig, order)  # type: ignore[arg-type]
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", (order, env)
    assert rig.exec.calls == []


def test_more_entries_to_move_than_one_move_takes_is_refused_and_the_edge_goes_through(
    rig: Rig,
) -> None:
    count = MAX_REORDER_MOVES + 2
    fill(rig, *[f"k{n:02d}" for n in range(count)])
    uids = [str(c.uid.get_value()) for c in reel2(rig).children]
    too_many = [uids[-1], *uids[:-1]]
    env = reorder(rig, too_many)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert str(MAX_REORDER_MOVES) in env["error"]
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []
    edge = [*uids[1 : MAX_REORDER_MOVES + 1], *uids[:1], *uids[MAX_REORDER_MOVES + 1 :]]
    ok = reorder(rig, edge)
    assert ok["ok"] is True, ok
    assert ok["result"]["moved"] <= MAX_REORDER_MOVES


def test_the_destination_must_be_a_desktop_reel_and_not_a_batch_reel(rig: Rig) -> None:
    folder = PyFolder("F", "uid-f", f"{LIB_WT}/F")
    rig.handles["lib"]._children.append(folder)
    adopt(rig.handles["lib"], [folder])
    batch = batch_reel(rig)
    for destination, code, needle in (
        (wt(batch), "INVALID_ARGS", "Batch group"),
        (LIBREEL, "PRECONDITION_UNMET", "library"),
        ({"kind": "wiretap", "id": f"{LIB_WT}/F"}, "INVALID_ARGS", "PyFolder"),
        (C1, "INVALID_ARGS", "PyClip"),
    ):
        env = reorder(rig, ["x"], destination)
        assert env["ok"] is False and env["errorCode"] == code, (destination, env)
        assert needle in env["error"], env["error"]
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []


def test_the_write_is_one_journal_group_with_one_move_intent(rig: Rig) -> None:
    fill(rig, "a", "b", "c")
    env = reorder(rig, by_name(rig, "c", "a", "b"))
    assert env["ok"] is True, env
    records = rig.records()
    first = records[0]
    assert first["type"] == "group" and first["kind"] == REORDER
    group = env["journal"]["group"]
    assert first["group"] == group
    intents = [r for r in records if r.get("type") == "intent"]
    assert [r["op"] for r in intents] == [MOVE] and intents[0]["group"] == group
    assert env["journal"]["revert"] == {"op": "journal.revert", "args": {"group": group}}
    assert env["journal"]["ids"] == [intents[0]["id"]]


def test_revert_puts_the_old_order_back_with_new_uids_for_what_it_moves(rig: Rig) -> None:
    fill(rig, "a", "b", "c", "d")
    env = reorder(rig, by_name(rig, "c", "a", "d", "b"))
    assert env["ok"] is True, env
    assert names(reel2(rig)) == ["c", "a", "d", "b"]
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert names(reel2(rig)) == ["a", "b", "c", "d"]
    assert states(rig)[env["journal"]["group"]] == "reverted"
    again = revert(rig, env)
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"
    assert Index(rig.records()).is_reverted(env["journal"]["group"])


def test_revert_moves_entries_the_reorder_did_not_move_when_that_is_the_fewest(rig: Rig) -> None:
    fill(rig, "a", "b", "c")
    env = reorder(rig, by_name(rig, "b", "c", "a"))
    out = revert(rig, env)
    assert out["ok"] is True, out
    assert names(reel2(rig)) == ["a", "b", "c"]
    assert out["affected"] == 2


def test_revert_refuses_a_reel_that_no_longer_holds_the_entries_and_moves_nothing(
    rig: Rig,
) -> None:
    fill(rig, "a", "b", "c")
    env = reorder(rig, by_name(rig, "b", "c", "a"))
    gone = rig.flame.find_by_wiretap_node_id(wt(reel2(rig).children[0])["id"])
    reel2(rig)._children.remove(gone)
    rig.flame.forget_wiretap(gone)
    calls = len(rig.flame.media_panel.move_calls)
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert len(rig.flame.media_panel.move_calls) == calls
    assert not Index(rig.records()).is_reverted(env["journal"]["group"])


def test_a_reel_that_comes_back_in_another_order_is_a_flame_api_error_with_both_orders(
    rig: Rig,
) -> None:
    fill(rig, "a", "b", "c")
    reads = {"n": 0}

    def flame_reorders(label: str | None) -> None:
        if label == REORDER:
            reads["n"] += 1
            if reads["n"] == 2:  # the read-back after the write
                reel2(rig)._children.reverse()

    rig.exec.before_run = flame_reorders
    env = reorder(rig, by_name(rig, "b", "c", "a"))
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    result = env["result"]
    assert result["orderMatches"] is False
    assert [r["name"] for r in result["after"]] == ["a", "c", "b"]
    assert [r["name"] for r in result["expected"]] == ["b", "c", "a"]
    group = env["journal"]["group"]
    assert env["journal"]["revert"] == {"op": "journal.revert", "args": {"group": group}}
    assert group in env["hint"]


def test_a_move_that_reaches_nothing_is_passed_on_with_its_code_and_nothing_is_journaled_as_moved(
    rig: Rig,
) -> None:
    fill(rig, "a", "b", "c")
    rig.flame.media_panel.move = lambda source, destination: []
    env = reorder(rig, by_name(rig, "b", "c", "a"))
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert names(reel2(rig)) == ["a", "b", "c"]


def test_a_timeout_is_not_retried_and_shuts_the_write_door(rig: Rig) -> None:
    fill(rig, "a", "b", "c")
    rig.exec.fault(MOVE, "TIMEOUT", when="after", skip=1)
    env = reorder(rig, by_name(rig, "b", "c", "a"))
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert len(rig.flame.media_panel.move_calls) == 1
    assert "journal.check" in env["hint"]
    again = reorder(rig, by_name(rig, "a", "b", "c"))
    assert again["errorCode"] == "POLICY_DENIED"


def test_reorder_is_refused_in_plan_run(rig: Rig) -> None:
    args = {"destination": REEL1, "order": ["x"]}
    env = rig.do("plan.run", {"steps": [{"op": REORDER, "args": args}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert f"{REORDER} cannot be a plan step" in env["result"]["issues"][0]["problems"][0]
    assert rig.exec.calls == []


def test_readonly_policy_denies_reorder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    made = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = reorder(made, ["x"], REEL1)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert made.exec.calls == [] and made.records() == []


def test_the_write_door_shut_by_an_unknown_write_refuses_reorder(rig: Rig) -> None:
    fill(rig, "a", "b")
    rig.runtime.guard.block("i_x", "g_x", MOVE, "TIMEOUT")
    env = reorder(rig, by_name(rig, "b", "a"))
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert rig.records() == [] and rig.flame.media_panel.move_calls == []


def test_the_flame_code_has_no_loop_and_is_ascii(rig: Rig) -> None:
    fill(rig, "a", "b", "c")
    reorder(rig, by_name(rig, "b", "c", "a"))
    loops = (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    for call in rig.exec.calls:
        if call.label == REORDER:
            assert not any(isinstance(n, loops) for n in ast.walk(ast.parse(own_source(call.code))))
        assert call.code.isascii()


def test_declaration_of_reorder() -> None:
    registry = Registry()
    register_all(registry)
    op = registry.get(REORDER)
    assert op is not None
    assert (op.effect, op.thread, op.undo, op.category) == ("create", "main", "value", "desktop")
    assert op.undo_steps({}, 1) == 0
    assert op.precheck is not None and op.wrap is not None and op.to_python is not None
    assert {p.name for p in op.params} == {"destination", "order"}
    for word in ("journal.revert", "plan.run", "uid", "NEW uid", "SILENT_NOOP"):
        assert word in op.description, word
    args = {"destination": REEL1, "order": ["x"]}
    ast.parse(op.to_python(args))
