"""`plan.run` step ids and references to the result of an earlier step
(`"{{ steps.<id>.result.<path> }}"`): checked before anything runs, replaced just before the step
is sent, and a reference that cannot be resolved stops the plan with the earlier steps applied.
Run through the real Runtime over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp import plan_refs, plan_step, refs
from flame_mcp.types import Category, OperationParam, OperationSpec, Refusal
from journal_test_helpers import BG1, COMP, REEL_WIRETAP, Rig, make_rig
from p32b_helpers import S1, make_p32b_rig
from rig_fixtures import rig_fixture
from timeline_p32a_helpers import TRIM, seg_sel

PLAN = "plan.run"
BATCH_INFO = {"op": "batch.info", "id": "i", "args": {"target": BG1}}
BATCH_LIST = {"op": "batch.list", "id": "a"}
GET_COMP = {"op": "node.get_attributes", "id": "g", "args": {"target": COMP, "keys": ["pos_x"]}}


rig = rig_fixture(make_rig)


def run(rig: Rig, steps: list[Any], **extra: Any) -> dict[str, Any]:
    return rig.do(PLAN, {"steps": steps, **extra})


def rename(name: Any, **extra: Any) -> dict[str, Any]:
    return {"op": "desktop.rename", "args": {"target": REEL_WIRETAP, "name": name}, **extra}


def set_pos(value: Any, **extra: Any) -> dict[str, Any]:
    return {
        "op": "node.set_attributes",
        "args": {"target": COMP, "values": {"pos_x": value}},
        **extra,
    }


def reel_name(rig: Rig) -> Any:
    return rig.handles["reel"].name.get_value()


def comp_pos(rig: Rig) -> Any:
    return rig.handles["comp"].pos_x.get_value()


def node_named(name: Any) -> dict[str, Any]:
    return {"kind": "node", "batch": BG1, "name": name}


def issues(env: dict[str, Any]) -> dict[int, list[str]]:
    return {i["index"]: i["problems"] for i in env["result"]["issues"]}


def refused(env: dict[str, Any], rig: Rig) -> None:
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert env["result"]["ran"] is False and rig.exec.calls == []


# ---- ids -------------------------------------------------------------------------------------


def test_a_step_without_id_is_named_s_and_its_index_and_every_summary_carries_the_id(
    rig: Rig,
) -> None:
    env = run(rig, [{"op": "batch.list"}, {"op": "batch.list", "id": "second"}])
    assert env["ok"] is True, env
    assert [s["id"] for s in env["result"]["steps"]] == ["s0", "second"]


@pytest.mark.parametrize("detail", ["full", "summary"])
def test_the_id_is_in_full_and_summary_steps(rig: Rig, detail: str) -> None:
    env = run(rig, [BATCH_LIST, rename("R9", id="w")], result_detail=detail)
    assert [s["id"] for s in env["result"]["steps"]] == ["a", "w"]


def test_the_id_is_in_mixed_steps_and_in_a_step_whose_result_was_left_out(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.runtime.response_mode = "compact"
    env = run(rig, [BATCH_LIST, rename("R9", id="w")])
    assert env["result"]["detail"] == "mixed"
    assert [s["id"] for s in env["result"]["steps"]] == ["a", "w"]
    monkeypatch.setattr(plan_step, "PLAN_STEP_RESULT_BYTES", 10)
    big = run(rig, [BATCH_LIST], result_detail="full")
    assert big["result"]["resultsOmitted"] == [0]
    assert big["result"]["steps"][0]["id"] == "a"


def test_a_failed_step_summary_has_the_id(rig: Rig) -> None:
    missing = {"kind": "node", "batch": BG1, "name": "nope"}
    step = {"op": "node.get_attributes", "id": "bad", "args": {"target": missing}}
    env = run(rig, [step], result_detail="summary")
    assert env["ok"] is False and env["result"]["steps"][0]["id"] == "bad"


def test_duplicate_explicit_ids_are_refused_before_anything_runs(rig: Rig) -> None:
    env = run(rig, [{"op": "batch.list", "id": "x"}, {"op": "batch.list", "id": "x"}])
    refused(env, rig)
    assert "duplicate" in issues(env)[1][0] and "'x'" in issues(env)[1][0]


def test_an_explicit_id_that_is_the_default_id_of_another_step_is_a_duplicate(rig: Rig) -> None:
    steps = [{"op": "batch.list", "id": "s2"}, {"op": "batch.list"}, {"op": "batch.list"}]
    env = run(rig, steps)
    refused(env, rig)
    assert list(issues(env)) == [2] and "'s2'" in issues(env)[2][0]


@pytest.mark.parametrize("bad", ["", "a b", "a.b", "a-b", 3, None])
def test_an_id_must_be_letters_digits_and_underscores(rig: Rig, bad: Any) -> None:
    env = run(rig, [{"op": "batch.list", "id": bad}])
    refused(env, rig)
    assert "id" in issues(env)[0][0]


def test_the_unknown_key_message_names_id(rig: Rig) -> None:
    env = run(rig, [{"op": "batch.list", "surprise": 1}])
    assert "'id'" in issues(env)[0][0]


# ---- references: replaced values -------------------------------------------------------------


def test_a_whole_string_reference_keeps_the_type_of_a_number(rig: Rig) -> None:
    env = run(rig, [BATCH_INFO, set_pos("{{ steps.i.result.nodeCount }}")])
    assert env["ok"] is True, env
    assert comp_pos(rig) == 4


def test_a_whole_string_reference_puts_in_a_dict_selector(rig: Rig) -> None:
    pick = {"op": "node.get_attributes", "args": {"target": "{{ steps.g.result.stable }}"}}
    env = run(rig, [GET_COMP, pick])
    assert env["ok"] is True, env
    assert (
        env["result"]["steps"][1]["result"]["stable"]
        == env["result"]["steps"][0]["result"]["stable"]
    )


def test_a_reference_inside_a_longer_string_is_embedded_as_text(rig: Rig) -> None:
    name = "R_{{ steps.a.result.total }}_{{ steps.a.result.items[0].name }}"
    env = run(rig, [BATCH_LIST, rename(name)])
    assert env["ok"] is True, env
    assert reel_name(rig) == "R_2_BG1"


def test_a_step_may_refer_to_an_earlier_step_by_its_default_id(rig: Rig) -> None:
    env = run(
        rig,
        [{"op": "batch.info", "args": {"target": BG1}}, set_pos("{{ steps.s0.result.nodeCount }}")],
    )
    assert env["ok"] is True, env
    assert comp_pos(rig) == 4


def test_the_path_may_use_an_index_and_a_star() -> None:
    envelope = {"ok": True, "result": {"items": [{"n": "A"}, {"n": "B"}], "total": 2}}
    seen = {"a": envelope}
    one = plan_refs.substitute_args({"x": "{{ steps.a.result.items[1].n }}"}, seen)
    every = plan_refs.substitute_args({"x": "{{ steps.a.result.items[*].n }}"}, seen)
    assert one == {"x": "B"} and every == {"x": ["A", "B"]}


def test_a_dict_or_none_cannot_be_embedded_in_a_string() -> None:
    envelope = {"ok": True, "result": {"d": {"k": 1}, "none": None, "flag": True}}
    for field in ("d", "none", "flag"):
        with pytest.raises(refs.RefsResolveError):
            plan_refs.substitute_args(f"x{{{{ steps.a.result.{field} }}}}", {"a": envelope})


def test_the_reference_follows_the_whole_result_not_the_brief_of_the_summary(rig: Rig) -> None:
    write = {"op": "desktop.rename", "id": "w", "args": {"target": REEL_WIRETAP, "name": "R7"}}
    ref = {
        "op": "desktop.rename",
        "args": {"target": REEL_WIRETAP, "name": "{{ steps.w.result.after }}x"},
    }
    env = run(rig, [write, ref], result_detail="summary")
    assert env["ok"] is True, env
    assert "result" not in env["result"]["steps"][0]  # the summary has a brief only
    assert reel_name(rig) == "R7x"


def test_a_reference_reaches_a_result_left_out_of_the_answer(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_step, "PLAN_STEP_RESULT_BYTES", 10)
    env = run(rig, [BATCH_LIST, rename("R_{{ steps.a.result.total }}")])
    assert env["ok"] is True, env
    assert env["result"]["resultsOmitted"] == [0, 1] or env["result"]["resultsOmitted"] == [0]
    assert reel_name(rig) == "R_2"


# ---- references: refused before anything runs ------------------------------------------------


def test_a_reference_to_a_missing_id_is_refused_up_front(rig: Rig) -> None:
    env = run(rig, [BATCH_LIST, rename("{{ steps.nope.result.total }}")])
    refused(env, rig)
    assert "nope" in issues(env)[1][0]


@pytest.mark.parametrize("target", ["w", "later"])
def test_a_reference_to_itself_or_a_later_step_is_refused_up_front(rig: Rig, target: str) -> None:
    steps = [
        BATCH_LIST,
        rename("x{{ steps." + target + ".result.total }}", id="w"),
        {"op": "batch.list", "id": "later"},
    ]
    env = run(rig, steps)
    refused(env, rig)
    assert "earlier" in issues(env)[1][0]


@pytest.mark.parametrize(
    "text",
    [
        "{{ steps.a.result.total",
        "steps.a.result.total }}",
        "{{ item.name }}",
        "{{ steps }}",
        "{{ steps.a }}",
        "{{ steps.a.ok }}",
        "{{ steps.a.result..total }}",
        "{{ steps.a.result.items[x] }}",
        "{{ }}",
    ],
)
def test_a_malformed_reference_is_refused_up_front(rig: Rig, text: str) -> None:
    env = run(rig, [BATCH_LIST, rename(text)])
    refused(env, rig)
    assert 1 in issues(env)


def test_a_bare_result_reference_is_valid(rig: Rig) -> None:
    env = run(rig, [BATCH_LIST, set_pos("{{ steps.a.result.total }}")])
    assert env["ok"] is True, env
    assert comp_pos(rig) == 2


def test_a_step_without_references_is_still_checked_up_front(rig: Rig) -> None:
    env = run(rig, [BATCH_LIST, {"op": "desktop.rename", "args": {"name": 3}}])
    refused(env, rig)
    assert 1 in issues(env)


def test_a_step_with_references_is_not_checked_for_its_values_until_it_is_sent(rig: Rig) -> None:
    env = run(
        rig, [BATCH_LIST, {"op": "desktop.rename", "args": {"name": "{{ steps.a.result.items }}"}}]
    )
    assert env["ok"] is False and env["result"]["stoppedAt"] == 1  # up front it was accepted


def test_the_checks_that_do_not_depend_on_the_value_still_run_up_front(rig: Rig) -> None:
    steps = [
        BATCH_LIST,
        {"op": "render.batch", "args": {"target": "{{ steps.a.result.items[0] }}"}},
        {"op": "no.such_op", "args": {"x": "{{ steps.a.result.total }}"}},
        {"op": "plan.run", "args": {"steps": "{{ steps.a.result.items }}"}},
    ]
    env = run(rig, steps)
    refused(env, rig)
    assert sorted(issues(env)) == [1, 2, 3]


# ---- references: failing while the plan runs -------------------------------------------------


def stopped_at(env: dict[str, Any], index: int) -> dict[str, Any]:
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert env["result"]["stoppedAt"] == index
    step: dict[str, Any] = env["result"]["steps"][index]
    assert step["ok"] is False and step["errorCode"] == "INVALID_ARGS"
    return step


def test_a_missing_key_stops_the_plan_with_the_reference_and_how_far_the_path_went(
    rig: Rig,
) -> None:
    env = run(rig, [rename("R_1"), BATCH_LIST, rename("{{ steps.a.result.items[0].nope }}")])
    step = stopped_at(env, 2)
    assert step["result"]["reference"] == "{{ steps.a.result.items[0].nope }}"
    assert step["result"]["reached"] == "result.items[0]"
    assert "nope" in step["error"]
    assert reel_name(rig) == "R_1"  # the step before stays applied
    assert env["partial"] == {"done": 2, "failed": 1, "notRun": 0}


def test_an_index_past_the_end_stops_the_plan(rig: Rig) -> None:
    env = run(rig, [BATCH_LIST, rename("{{ steps.a.result.items[9].name }}")])
    step = stopped_at(env, 1)
    assert step["result"]["reached"] == "result.items"


def test_a_failed_step_has_no_result_to_refer_to_even_with_continue(rig: Rig) -> None:
    missing = {"kind": "node", "batch": BG1, "name": "nope"}
    steps = [
        {"op": "node.get_attributes", "id": "bad", "args": {"target": missing}},
        {"op": "node.get_attributes", "args": {"target": "{{ steps.bad.result.stable }}"}},
        {"op": "batch.list"},
    ]
    env = run(rig, steps, on_error="continue")
    step = stopped_at(env, 1)
    assert "bad" in step["error"]
    assert len(env["result"]["steps"]) == 2  # the third step never ran
    assert env["partial"] == {"done": 0, "failed": 2, "notRun": 1}


def test_an_unresolvable_reference_sends_nothing_for_that_step(rig: Rig) -> None:
    run(rig, [BATCH_LIST])
    one_step = len(rig.exec.calls)
    rig.exec.calls.clear()
    run(rig, [BATCH_LIST, rename("{{ steps.a.result.nope }}")])
    assert len(rig.exec.calls) == one_step and reel_name(rig) == "Reel 1"


def test_a_value_of_the_wrong_type_stops_the_plan_before_the_step_is_sent(rig: Rig) -> None:
    env = run(rig, [BATCH_LIST, rename("{{ steps.a.result.items }}")])
    step = stopped_at(env, 1)
    assert any("name" in p for p in step["result"]["problems"])
    assert reel_name(rig) == "Reel 1"


def test_a_non_ascii_value_after_the_replacement_stops_the_plan_before_the_step_is_sent(
    rig: Rig,
) -> None:
    rig.handles["bg1"].name.set_value("B\u00e9")
    run(rig, [BATCH_LIST])
    one_step = len(rig.exec.calls)
    rig.exec.calls.clear()
    env = run(rig, [BATCH_LIST, rename("{{ steps.a.result.items[0].name }}")])
    step = stopped_at(env, 1)
    assert any("non-ASCII" in p for p in step["result"]["problems"])
    assert len(rig.exec.calls) == one_step and reel_name(rig) == "Reel 1"


def test_an_unknown_argument_after_the_replacement_stops_the_plan(rig: Rig) -> None:
    step = rename("R_{{ steps.a.result.total }}")
    step["args"]["colour"] = 1
    env = run(rig, [BATCH_LIST, step])
    stopped_at(env, 1)
    assert reel_name(rig) == "Reel 1"


def test_a_reference_that_makes_an_op_a_destructive_eval_is_refused_when_it_is_sent(
    rig: Rig,
) -> None:
    probe = {
        "op": "eval.run",
        "args": {"code": "x = 1", "effect": "{{ steps.a.result.items[0].name }}"},
    }
    env = run(rig, [BATCH_LIST, probe])
    assert env["ok"] is False  # neither run nor accepted
    assert rig.exec.calls == [] or env["result"].get("ran") is False or "stoppedAt" in env["result"]


# ---- bundled plans ---------------------------------------------------------------------------


def test_a_write_with_a_reference_in_a_bundled_plan_is_resolved_when_its_turn_comes(
    rig: Rig,
) -> None:
    steps = [
        {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "b1"}},
        GET_COMP,
        {
            "op": "node.set_attributes",
            "args": {
                "target": node_named("{{ steps.g.result.stable.name }}"),
                "values": {"pos_x": 7.0},
            },
        },
    ]
    env = run(rig, steps)
    assert env["ok"] is True, env
    assert comp_pos(rig) == 7
    resolved = env["result"]["batch"]["resolved"]
    by_index = {r["index"]: r for r in resolved}
    assert by_index[0]["late"] is False and by_index[2]["late"] is True
    assert rig.records()[0]["late"] == [2]
    revert = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert revert["ok"] is True, revert
    assert comp_pos(rig) == 0


def test_a_bundled_plan_whose_writes_all_hold_references_is_refused_up_front(rig: Rig) -> None:
    steps = [
        GET_COMP,
        {
            "op": "node.create",
            "args": {"target": BG1, "type": "Blur", "name": "{{ steps.g.result.stable.name }}"},
        },
    ]
    env = run(rig, steps)
    assert env["errorCode"] == "INVALID_ARGS" and "reference" in env["error"]
    assert rig.records() == []


def test_a_reference_that_fails_in_a_bundled_plan_stops_it_with_the_earlier_steps_applied(
    rig: Rig,
) -> None:
    steps = [
        set_pos(9.0),
        {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "b1"}},
        {
            "op": "node.set_attributes",
            "args": {"target": COMP, "values": {"pos_x": "{{ steps.g.result.nope }}"}},
        },
    ]
    steps.insert(2, GET_COMP)
    env = run(rig, steps)
    stopped_at(env, 3)
    assert comp_pos(rig) == 9


# ---- a reference where the plan's write target is named --------------------------------------

STABLE_BATCH = "{{ steps.g.result.stable.batch }}"


def test_a_reference_in_the_batch_part_of_a_node_selector_is_refused_in_a_bundled_plan(
    rig: Rig,
) -> None:
    create = {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "b1"}}
    other_batch = {"kind": "node", "batch": STABLE_BATCH, "name": "comp1"}
    write = {"op": "node.set_attributes", "args": {"target": other_batch, "values": {"pos_x": 1}}}
    env = run(rig, [create, GET_COMP, write])
    refused(env, rig)
    assert list(issues(env)) == [2]
    assert "write target" in issues(env)[2][0]


def test_a_reference_in_the_group_argument_of_a_checkpointed_op_is_refused(rig: Rig) -> None:
    create = {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "b1"}}
    elsewhere = {"op": "node.create", "args": {"target": STABLE_BATCH, "type": "Blur"}}
    env = run(rig, [create, GET_COMP, elsewhere])
    refused(env, rig)
    assert list(issues(env)) == [2]


def test_a_reference_in_the_node_part_of_a_selector_is_allowed_in_a_bundled_plan(rig: Rig) -> None:
    create = {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "b1"}}
    write = {
        "op": "node.set_attributes",
        "args": {"target": node_named("{{ steps.g.result.stable.name }}"), "values": {"pos_x": 3}},
    }
    env = run(rig, [create, GET_COMP, write])
    assert env["ok"] is True, env
    assert comp_pos(rig) == 3


def test_a_reference_in_the_parent_of_a_segment_selector_is_refused_in_a_bundled_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_p32b_rig(monkeypatch, tmp_path)
    target = seg_sel(S1, 0, record_in=10)
    target["parent"] = "{{ steps.a.result.items[0] }}"
    step = {"op": TRIM, "args": {"target": target, "edge": "head", "offset": 2}}
    env = run(rig, [BATCH_LIST, step])
    refused(env, rig)
    assert list(issues(env)) == [1] and "write target" in issues(env)[1][0]


def test_a_reference_outside_the_parent_of_a_segment_selector_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_p32b_rig(monkeypatch, tmp_path)
    step = {
        "op": TRIM,
        "args": {
            "target": seg_sel(S1, 0, record_in=10),
            "edge": "head",
            "offset": "{{ steps.a.result.total }}",
        },
    }
    pinned = {
        "op": TRIM,
        "args": {"target": seg_sel(S1, 0, record_in=40), "edge": "tail", "offset": 2},
    }
    env = run(rig, [BATCH_LIST, pinned, step])
    assert env["ok"] is True, env
    resolved = {r["index"]: r for r in env["result"]["timeline"]["resolved"]}
    assert resolved[1]["late"] is False and resolved[2]["late"] is True


# ---- a policy refusal after the replacement --------------------------------------------------

DENY_HINT = "ask for the ground to be opened"


def _register_picky_op(rig: Rig) -> None:
    rig.registry.register_category(
        Category(
            name="testcat",
            description="t",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )

    def precheck(args: Any, policy: Any) -> Refusal | None:
        if args["value"] == "BG1":
            return Refusal("POLICY_DENIED", "BG1 is off limits", hint=DENY_HINT)
        return None

    rig.registry.register(
        OperationSpec(
            name="testcat.picky",
            category="testcat",
            description="a write that refuses one value",
            effect="create",
            params=(OperationParam(name="value", type="string", description="v", required=True),),
            to_python=lambda args: f"flame._thing = {args['value']!r}\n_affected = 1",
            precheck=precheck,
        )
    )


def test_a_policy_refusal_after_the_replacement_keeps_its_code_and_hint(rig: Rig) -> None:
    _register_picky_op(rig)
    picky = {"op": "testcat.picky", "args": {"value": "{{ steps.a.result.items[0].name }}"}}
    env = run(rig, [BATCH_LIST, picky])
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert env["result"]["stoppedAt"] == 1
    assert DENY_HINT in env["hint"]
    assert not hasattr(rig.flame, "_thing")


# ---- the name of a node a step with references makes -----------------------------------------


def test_a_step_that_names_a_node_made_by_a_step_with_references_needs_late(rig: Rig) -> None:
    make = {
        "op": "node.create",
        "args": {"target": BG1, "type": "Blur", "name": "{{ steps.a.result.items[0].name }}"},
    }
    use = {
        "op": "node.set_attributes",
        "args": {"target": node_named("BG1"), "values": {"pos_x": 2}},
    }
    env = run(rig, [BATCH_LIST, make, use])
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert "late: true" in env["hint"] and rig.records() == []
    again = run(rig, [BATCH_LIST, make, {**use, "late": True}])
    assert again["ok"] is True, again


def test_a_dict_or_list_put_in_is_a_copy_of_the_step_result() -> None:
    envelope = {"ok": True, "result": {"d": {"k": [1]}, "l": [{"a": 1}]}}
    out = plan_refs.substitute_args(
        {"x": "{{ steps.a.result.d }}", "y": "{{ steps.a.result.l }}"}, {"a": envelope}
    )
    out["x"]["k"].append(2)
    out["y"][0]["a"] = 9
    assert envelope["result"] == {"d": {"k": [1]}, "l": [{"a": 1}]}


def test_a_dict_or_list_put_in_from_the_item_is_a_copy_of_the_item() -> None:
    item = {"sel": {"id": [1]}}
    out = plan_refs.substitute_args("{{ item.sel }}", {}, item)
    out["id"].append(2)
    assert item == {"sel": {"id": [1]}}


def test_a_loop_leaves_the_items_it_was_given_as_they_were(rig: Rig) -> None:
    items = [{"name": "BG1", "target": {"kind": "batch", "name": "BG1"}}]
    first = {"id": "a", "op": "batch.info", "args": {"target": "{{ item.target }}"}}
    second = {"op": "batch.info", "args": {"target": "{{ item.target }}"}}
    env = rig.do("plan.for_each", {"items": items, "key": "name", "steps": [first, second]})
    assert env["ok"] is True, env
    assert items[0]["target"] == {"kind": "batch", "name": "BG1"}
