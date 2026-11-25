"""`plan.for_each` with write steps: the classes of ops (loop_writes), the confirmation (dry_run /
approve / the unattended mode), the parent and child journal groups and their revert, a stop and
the continuation with `only`, the time budget, the lock and the code of a policy refusal. Run
through the real Runtime over the fake Flame tree."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import loop_limits, loop_writes, plan_loop_confirm, plan_loop_report
from flame_mcp import plan_loop_run as loop_run
from flame_mcp.journal import LOOP_GROUP_KIND, Index, Journal, new_id
from flame_mcp.loop_limits import LoopLimits
from flame_mcp.plan_step import args_checker
from flame_mcp.policy import Policy
from flame_mcp.registry import RegisteredOperation
from flame_mcp.runtime import Runtime
from journal_test_helpers import BG1, Rig
from media_p31b_helpers import C1, REEL1, add_clip, make_media_rig, names, reel2_selector, wt
from p32b_helpers import make_p32b_rig, reel2
from plan_use_helpers import KEEPS_OUTPUT_OPS, NEVER_OPS, REWRITE_OPS, ROLLBACK_OPS
from rig_fixtures import rig_fixture

LOOP = "plan.for_each"
UNATTENDED = LoopLimits(unattended=True)


rig = rig_fixture(make_media_rig)


def rename_items(rig: Rig) -> list[dict[str, Any]]:
    return [
        {"name": "r1", "target": REEL1, "new": "A"},
        {"name": "r2", "target": reel2_selector(rig), "new": "B"},
    ]


RENAME = {
    "id": "ren",
    "op": "desktop.rename",
    "args": {"target": "{{ item.target }}", "name": "{{ item.new }}"},
}


def loop(rig: Rig, **args: Any) -> dict[str, Any]:
    args.setdefault("key", "name")
    return rig.do(LOOP, args)


def attended(rig: Rig, **args: Any) -> dict[str, Any]:
    """The two calls of a write loop: the preview, then the run with its digest."""
    preview = loop(rig, dry_run=True, **args)
    assert preview["ok"] is True, preview
    return loop(rig, approve=preview["result"]["digest"], **args)


def reel_names(rig: Rig) -> list[str]:
    return [str(r.name.get_value()) for r in rig.handles["reel_group"].reels]


def parent_of(env: dict[str, Any]) -> str:
    return str(env["journal"]["group"])


def child_groups(env: dict[str, Any]) -> list[str]:
    return [g["group"] for item in env["result"]["items"] for g in item["groups"]]


def refused(env: dict[str, Any], rig: Rig, code: str = "INVALID_ARGS") -> None:
    assert env["ok"] is False and env["errorCode"] == code, env
    assert rig.exec.calls == [], rig.exec.labels()
    assert rig.records() == []


# ---- the classes of ops ----------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted(NEVER_OPS))
def test_ops_that_cannot_be_put_back_are_not_loop_steps(rig: Rig, op: str) -> None:
    env = loop(rig, items=[{"name": "a"}], steps=[{"op": op, "args": {}}])
    refused(env, rig)
    assert "cannot be a plan.for_each step" in str(env)


@pytest.mark.parametrize(
    "op",
    sorted(ROLLBACK_OPS | REWRITE_OPS | KEEPS_OUTPUT_OPS),
)
def test_the_other_classes_are_not_refused_for_being_plan_run_refusals(rig: Rig, op: str) -> None:
    env = loop(rig, items=[{"name": "a"}], steps=[{"op": op, "args": {}}], dry_run=True)
    assert "cannot be a plan step" not in str(env), env
    assert "cannot be a plan.for_each step" not in str(env), env


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("render.batch", {"allow_overwrite": True}),
        ("render.clip", {"force": True}),
        ("batch.iterate", {"overwrite": True}),
    ],
)
def test_an_overwrite_written_in_the_step_is_refused_before_anything_runs(
    rig: Rig, op: str, args: dict[str, Any]
) -> None:
    env = loop(rig, items=[{"name": "a"}], steps=[{"op": op, "args": args}], dry_run=True)
    refused(env, rig)
    assert "overwrite" in str(env).lower()


def test_an_overwrite_that_an_item_brings_in_is_refused_when_the_loop_is_expanded(rig: Rig) -> None:
    step = {
        "op": "render.batch",
        "args": {"target": BG1, "allow_overwrite": "{{ item.over }}"},
    }
    env = loop(rig, items=[{"name": "a", "over": False}, {"name": "b", "over": True}], steps=[step])
    refused(env, rig)
    assert "allow_overwrite" in str(env)


def test_the_overwrite_flags_are_every_such_param_of_a_write_op_of_the_registry(rig: Rig) -> None:
    found = {
        (o.name, p.name)
        for o in rig.registry.list_ops()
        if o.effect != "read" and o.name not in loop_writes.NOT_OVERWRITE
        for p in o.params
        if p.name in loop_writes.OVERWRITE_PARAMS
    }
    assert {
        ("render.batch", "allow_overwrite"),
        ("render.clip", "force"),
        ("batch.iterate", "overwrite"),
        ("batch.save_setup", "overwrite"),
        ("node.save_setup", "overwrite"),
        ("segment.effect_save_setup", "overwrite"),
    } <= found
    for name, flag in found:
        op = rig.registry.get(name)
        assert op is not None
        assert loop_writes.overwrite_problem(op, {flag: True}) is not None, name
        assert loop_writes.overwrite_problem(op, {flag: False}) is None, name
        param = next(p for p in op.params if p.name == flag)
        assert param.default is not True, name
    rename = rig.registry.get("desktop.rename")
    assert rename is not None
    assert loop_writes.overwrite_problem(rename, {"force": True}) is None


def test_what_is_not_an_overwrite_is_listed_with_its_reason(rig: Rig) -> None:
    assert set(loop_writes.NOT_OVERWRITE) == {"journal.revert"}
    assert all(loop_writes.NOT_OVERWRITE.values())


@pytest.mark.parametrize("unattended", [False, True])
@pytest.mark.parametrize("op", ["batch.save_setup", "node.save_setup", "segment.effect_save_setup"])
def test_a_save_that_overwrites_a_file_is_refused_in_every_mode(
    rig: Rig, op: str, unattended: bool
) -> None:
    if unattended:
        rig.runtime.loop_limits = UNATTENDED
    step = {"op": op, "args": {"target": BG1, "overwrite": "{{ item.over }}"}}
    env = loop(rig, items=[{"name": "a", "over": True}], steps=[step], dry_run=True)
    refused(env, rig)
    assert "overwrite" in str(env)
    literal = {"op": op, "args": {"target": BG1, "overwrite": True}}
    refused(loop(rig, items=[{"name": "a"}], steps=[literal], dry_run=True), rig)


# ---- confirmation ----------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_lists_the_items_and_the_first_resolved_step(
    rig: Rig,
) -> None:
    env = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["dryRun"] is True and result["count"] == 2 and result["keys"] == ["r1", "r2"]
    assert result["steps"] == ["desktop.rename"]
    first = result["firstItem"]
    assert first["key"] == "r1"
    assert first["steps"][0]["args"] == {"target": REEL1, "name": "A"}
    assert result["mode"] == "attended" and len(result["digest"]) == 64
    assert rig.exec.calls == [] and rig.records() == []
    assert reel_names(rig) == ["Reel 1", "Reel 2"]


def test_a_write_loop_without_approve_runs_nothing_and_returns_the_preview(rig: Rig) -> None:
    env = loop(rig, items=rename_items(rig), steps=[RENAME])
    refused(env, rig, "PRECONDITION_UNMET")
    assert env["result"]["needsApproval"] is True and env["result"]["digest"]


def test_the_digest_of_the_dry_run_lets_the_loop_run(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    assert env["ok"] is True, env
    assert reel_names(rig) == ["A", "B"]
    assert env["result"]["total"] == 2 and env["partial"] == {"done": 2, "failed": 0, "notRun": 0}


def test_a_changed_list_gives_a_new_digest_and_runs_nothing(rig: Rig) -> None:
    preview = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    other = rename_items(rig)
    other[1]["new"] = "C"
    env = loop(rig, items=other, steps=[RENAME], approve=preview["result"]["digest"])
    refused(env, rig, "PRECONDITION_UNMET")
    assert env["result"]["digest"] != preview["result"]["digest"]
    assert env["result"]["keys"] == ["r1", "r2"]


def test_the_digest_follows_the_items_a_source_read_gives_now(rig: Rig) -> None:
    source = {"op": "desktop.list_children", "args": {"target": REEL1}, "path": "result.items"}
    step = {"op": "desktop.rename", "args": {"target": REEL1, "name": "x"}}
    first = loop(rig, source=source, key="name", steps=[step], dry_run=True)
    assert first["ok"] is True, first
    add_clip(rig.handles["reel_group"].reels[0], "extra")
    again = loop(rig, source=source, key="name", steps=[step], approve=first["result"]["digest"])
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET", again


def test_the_digest_differs_when_only_narrows_the_list(rig: Rig) -> None:
    both = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    one = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True, only=["r1"])
    assert both["result"]["digest"] != one["result"]["digest"]


def test_the_dry_run_of_a_loop_with_a_rewriting_step_lists_it_for_every_item(rig: Rig) -> None:
    steps = [
        {**RENAME, "id": "first"},
        {
            "op": "desktop.delete",
            "args": {
                "targets": ["{{ item.target }}"],
                "without_backup": ["{{ steps.first.result.x }}"],
            },
        },
    ]
    env = loop(rig, items=rename_items(rig), steps=steps, dry_run=True)
    result = env["result"]
    review = result["review"]
    assert [r["key"] for r in review] == ["r1", "r2"]
    assert all(r["op"] == "desktop.delete" and r["class"] == "rewrite" for r in review)
    assert review[0]["args"]["targets"] == [REEL1]
    assert review[0]["args"]["without_backup"] == ["{{ steps.first.result.x }}"]
    assert result["needsApproval"] is True


def test_a_rewriting_step_needs_approval_even_in_the_unattended_mode(rig: Rig) -> None:
    rig.runtime.loop_limits = UNATTENDED
    step = {"op": "desktop.delete", "args": {"targets": ["{{ item.target }}"]}}
    env = loop(rig, items=[{"name": "r2", "target": reel2_selector(rig)}], steps=[step])
    refused(env, rig, "PRECONDITION_UNMET")
    assert reel_names(rig) == ["Reel 1", "Reel 2"]


def test_an_output_step_needs_approval_even_in_the_unattended_mode(rig: Rig) -> None:
    rig.runtime.loop_limits = UNATTENDED
    step = {"op": "render.batch", "args": {"target": BG1}}
    env = loop(rig, items=[{"name": "a"}], steps=[step])
    refused(env, rig, "PRECONDITION_UNMET")


def test_the_unattended_mode_runs_a_loop_of_put_back_steps_in_one_call(rig: Rig) -> None:
    rig.runtime.loop_limits = UNATTENDED
    env = loop(rig, items=rename_items(rig), steps=[RENAME])
    assert env["ok"] is True, env
    assert reel_names(rig) == ["A", "B"]


def test_the_dry_run_still_answers_in_the_unattended_mode_and_names_the_mode(rig: Rig) -> None:
    rig.runtime.loop_limits = UNATTENDED
    env = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    assert env["result"]["mode"] == "unattended" and env["result"]["needsApproval"] is False
    assert rig.records() == []


def test_no_argument_turns_the_confirmation_off(rig: Rig) -> None:
    env = loop(rig, items=rename_items(rig), steps=[RENAME], approve="skip")
    refused(env, rig, "PRECONDITION_UNMET")
    env = loop(rig, items=rename_items(rig), steps=[RENAME], unattended=True)
    refused(env, rig)


def test_the_environment_variable_opens_the_unattended_mode_only_with_one() -> None:
    name = loop_limits.UNATTENDED_ENV
    assert loop_limits.loop_limits_from_env({}).unattended is False
    assert loop_limits.loop_limits_from_env({name: "1"}).unattended is True
    assert loop_limits.loop_limits_from_env({name: "0"}).unattended is False


@pytest.mark.parametrize("value", ["", "true", "yes", "2", " 1", "1 ", "on", "١"])
def test_any_other_value_of_the_variable_is_a_config_error(value: str) -> None:
    with pytest.raises(loop_limits.LoopLimitsConfigError, match=loop_limits.UNATTENDED_ENV):
        loop_limits.loop_limits_from_env({loop_limits.UNATTENDED_ENV: value})


def test_a_read_loop_still_runs_in_one_call(rig: Rig) -> None:
    step = {"op": "batch.info", "args": {"target": {"kind": "batch", "name": "{{ item.name }}"}}}
    env = loop(rig, items=[{"name": "BG1"}], steps=[step])
    assert env["ok"] is True and env["result"].get("group") is None, env


# ---- the parent and the child groups ---------------------------------------------------------


def test_the_loop_has_one_parent_and_one_child_group_per_item_in_the_order_run(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    parent = parent_of(env)
    children = child_groups(env)
    assert len(children) == 2 and parent not in children
    index = Index(rig.records())
    assert index.is_loop_parent(parent)
    rows = index.loop_child_records(parent)
    assert [r["group"] for r in rows] == children
    assert [r["key"] for r in rows] == ["r1", "r2"]
    assert all(r["revertible"] is True for r in rows)
    for child in children:
        assert [v.intent["op"] for v in index.groups[child]] == ["desktop.rename"]


def test_reverting_the_parent_puts_the_children_back_newest_first(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    parent = parent_of(env)
    out = rig.do("journal.revert", {"group": parent})
    assert out["ok"] is True, out
    assert reel_names(rig) == ["Reel 1", "Reel 2"]
    rows = out["result"]["children"]
    assert [r["key"] for r in rows] == ["r2", "r1"] and all(r["reverted"] for r in rows)
    index = Index(rig.records())
    assert all(index.is_reverted(c) for c in child_groups(env))
    again = rig.do("journal.revert", {"group": parent})
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"


def test_a_child_reverted_alone_puts_back_only_that_item(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    first = env["result"]["items"][0]["groups"][0]["group"]
    out = rig.do("journal.revert", {"group": first})
    assert out["ok"] is True, out
    assert reel_names(rig) == ["Reel 1", "B"]
    rest = rig.do("journal.revert", {"group": parent_of(env)})
    assert rest["ok"] is True, rest
    assert [r["key"] for r in rest["result"]["children"] if r.get("reverted")] == ["r2"]
    assert reel_names(rig) == ["Reel 1", "Reel 2"]


def test_a_conflict_in_the_middle_stops_the_revert_and_a_second_call_goes_on(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    parent = parent_of(env)
    assert rig.do("desktop.rename", {"target": REEL1, "name": "edited"})["ok"] is True
    out = rig.do("journal.revert", {"group": parent})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    result = out["result"]
    assert [c["key"] for c in result["children"] if c.get("reverted")] == ["r2"]
    assert [c["key"] for c in result["remaining"]] == ["r1"]
    assert result["stoppedAt"]["key"] == "r1"
    assert reel_names(rig) == ["edited", "Reel 2"]
    assert rig.do("desktop.rename", {"target": REEL1, "name": "A"})["ok"] is True
    finish = rig.do("journal.revert", {"group": parent})
    assert finish["ok"] is True, finish
    assert [c["key"] for c in finish["result"]["children"] if c.get("reverted")] == ["r1"]
    assert reel_names(rig) == ["Reel 1", "Reel 2"]


def test_a_conflict_on_the_newest_child_leaves_every_child_to_go(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    assert rig.do("desktop.rename", {"target": reel2_selector(rig), "name": "edited"})["ok"]
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert [c["key"] for c in out["result"]["remaining"]] == ["r2", "r1"]
    assert reel_names(rig) == ["A", "edited"]


def test_a_child_that_keeps_its_output_is_skipped_and_listed_as_what_stays(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig)[:1], steps=[RENAME])
    parent = parent_of(env)
    intent = next(r for r in rig.records() if r.get("type") == "intent")
    outcome = next(r for r in rig.records() if r.get("type") == "outcome")
    rig.journal.append({**intent, "id": "i_render", "group": "g_render"})
    rig.journal.append({**outcome, "id": "i_render", "group": "g_render"})
    rig.journal.append(
        {
            "type": "group",
            "parent": parent,
            "group": "g_render",
            "key": "r1",
            "op": "render.batch",
            "step": "render",
            "revertible": False,
        }
    )
    out = rig.do("journal.revert", {"group": parent})
    assert out["ok"] is True, out
    kept = out["result"]["kept"]
    assert kept == [{"group": "g_render", "key": "r1", "op": "render.batch", "step": "render"}]
    assert reel_names(rig) == ["Reel 1", "Reel 2"]


def test_journal_check_of_a_parent_reads_its_children(rig: Rig) -> None:
    rig.exec.fault("desktop.rename", "TIMEOUT", when="after", skip=1)
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    parent = parent_of(env)
    checked = rig.do("journal.check", {"group": parent})
    assert checked["ok"] is True, checked
    assert checked["result"]["inDoubt"] == 1
    assert checked["result"]["writesBlocked"] is False


def test_journal_show_of_a_parent_lists_its_children(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    shown = rig.do("journal.show", {"group": parent_of(env)})
    assert shown["ok"] is True, shown
    assert [c["key"] for c in shown["result"]["children"]] == ["r1", "r2"]


def test_a_node_checkpoint_step_makes_one_bundled_child_group_per_item(rig: Rig) -> None:
    items = [{"name": "i1", "node": "m1"}, {"name": "i2", "node": "m2"}]
    step = {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "{{ item.node }}"}}
    before = [str(n.name.get_value()) for n in rig.handles["bg1"].nodes]
    env = attended(rig, items=items, steps=[step])
    assert env["ok"] is True, env
    assert [len(i["groups"]) for i in env["result"]["items"]] == [1, 1]
    index = Index(rig.records())
    for child in child_groups(env):
        assert any(v.revert_checkpoint() for v in index.groups[child])
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is True, out
    assert [str(n.name.get_value()) for n in rig.handles["bg1"].nodes] == before


def test_steps_of_an_op_with_its_own_revert_are_one_child_group_each(rig: Rig) -> None:
    c2 = add_clip(rig.handles["reel_group"].reels[0], "c2")
    items = [{"name": "a", "src": C1}, {"name": "b", "src": wt(c2)}]
    copy = {
        "op": "desktop.copy",
        "args": {"sources": ["{{ item.src }}"], "destination": reel2_selector(rig)},
    }
    env = attended(rig, items=items, steps=[copy, copy])
    assert env["ok"] is True, env
    assert [len(i["groups"]) for i in env["result"]["items"]] == [2, 2]
    assert len(names(rig.handles["reel2"])) >= 2
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is True, out
    assert names(rig.handles["reel2"]) == []


def test_a_group_of_a_op_that_makes_its_own_group_becomes_the_child(rig: Rig) -> None:
    c2 = add_clip(rig.handles["reel_group"].reels[0], "c2")
    step = {
        "op": "desktop.arrange",
        "args": {
            "destination": reel2_selector(rig),
            "items": [{"action": "copy", "source": "{{ item.src }}"}],
        },
    }
    env = attended(rig, items=[{"name": "a", "src": wt(c2)}], steps=[step])
    assert env["ok"] is True, env
    child = env["result"]["items"][0]["groups"][0]["group"]
    index = Index(rig.records())
    assert index.group_meta[child]["kind"] == "desktop.arrange"
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is True, out
    assert names(rig.handles["reel2"]) == []


def test_a_plain_write_and_an_own_revert_step_in_one_item_go_in_the_order_run(rig: Rig) -> None:
    c2 = add_clip(rig.handles["reel_group"].reels[0], "c2")
    items = [{"name": "a", "src": wt(c2), "target": REEL1, "new": "A"}]
    copy = {
        "op": "desktop.copy",
        "args": {"sources": ["{{ item.src }}"], "destination": reel2_selector(rig)},
    }
    env = attended(rig, items=items, steps=[RENAME, copy, {**RENAME, "id": "again"}])
    assert env["ok"] is True, env
    assert len(env["result"]["items"][0]["groups"]) == 3
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is True, out
    assert reel_names(rig) == ["Reel 1", "Reel 2"] and names(rig.handles["reel2"]) == []


def test_a_timeline_and_a_batch_write_in_one_item_are_refused(rig: Rig) -> None:
    steps = [
        {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "m1"}},
        {"op": "segment.trim", "args": {"target": {"kind": "wiretap", "id": "/x"}}},
    ]
    env = loop(rig, items=[{"name": "a"}], steps=steps, dry_run=True)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env


# ---- a stop and the continuation -------------------------------------------------------------


def three_items(rig: Rig) -> list[dict[str, Any]]:
    gone = {"kind": "wiretap", "id": "/wt/RG/nothing"}
    return [
        {"name": "r1", "target": REEL1, "new": "A"},
        {"name": "bad", "target": gone, "new": "X"},
        {"name": "r2", "target": reel2_selector(rig), "new": "B"},
    ]


def test_a_write_loop_stops_at_the_first_failed_item_and_names_the_rest(rig: Rig) -> None:
    env = attended(rig, items=three_items(rig), steps=[RENAME])
    assert env["ok"] is False, env
    result = env["result"]
    assert {i["key"]: i["status"] for i in result["items"]} == {
        "r1": "done",
        "bad": "failed",
        "r2": "not_run",
    }
    assert result["stoppedAt"] == "bad" and result["notRun"] == ["r2"]
    assert env["partial"] == {"done": 1, "failed": 1, "notRun": 1}
    assert reel_names(rig) == ["A", "Reel 2"]
    assert env["journal"]["group"] == result["group"]


def test_only_runs_the_chosen_items_of_the_narrowed_list(rig: Rig) -> None:
    env = attended(rig, items=three_items(rig), steps=[RENAME], only=["r2"])
    assert env["ok"] is True, env
    assert env["result"]["keys"] == ["r2"]
    assert reel_names(rig) == ["Reel 1", "B"]


def test_a_key_that_is_not_in_the_list_is_refused_before_anything_runs(rig: Rig) -> None:
    refused(
        loop(rig, items=rename_items(rig), steps=[RENAME], only=["nope"], dry_run=True),
        rig,
        "INVALID_ARGS",
    )


def test_on_error_continue_is_refused_for_a_write_loop(rig: Rig) -> None:
    refused(
        loop(rig, items=rename_items(rig), steps=[RENAME], on_error="continue", dry_run=True),
        rig,
    )


def test_a_write_with_no_answer_stops_the_loop_marks_the_item_unknown_and_shuts_the_door(
    rig: Rig,
) -> None:
    rig.exec.fault("desktop.rename", "TIMEOUT", when="after", skip=1)
    env = attended(
        rig, items=[*rename_items(rig), {"name": "r3", "target": REEL1, "new": "C"}], steps=[RENAME]
    )
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    statuses = {i["key"]: i["status"] for i in env["result"]["items"]}
    assert statuses == {"r1": "done", "r2": "unknown", "r3": "not_run"}
    assert env["result"]["failClosed"]["errorCode"] == "TIMEOUT"
    blocked = rig.do("desktop.rename", {"target": REEL1, "name": "z"})
    assert blocked["ok"] is False and blocked["errorCode"] == "POLICY_DENIED"
    again = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    assert again["ok"] is True, again


def test_a_write_loop_is_refused_while_the_door_is_shut(rig: Rig) -> None:
    rig.exec.fault("desktop.rename", "TIMEOUT", when="after")
    attended(rig, items=rename_items(rig), steps=[RENAME])
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert env["result"]["ran"] is False


# ---- time, the lock, the policy --------------------------------------------------------------


def test_a_spent_budget_stops_a_write_loop_between_items(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter([0, 0, 121, 999, 999])
    monkeypatch.setattr(plan_loop_report, "_now", lambda: next(ticks))
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    assert env["ok"] is True, env
    result = env["result"]
    assert result["notRun"] == ["r2"] and result["budgetStop"]["after"] == "r1"
    assert reel_names(rig) == ["A", "Reel 2"]


def test_the_write_lock_is_held_while_the_items_run(rig: Rig) -> None:
    held: list[bool] = []

    def probe(label: str | None) -> None:
        if label != "desktop.rename":
            return
        got: list[bool] = []

        def other() -> None:
            ok = rig.runtime.write_lock.acquire(blocking=False)
            got.append(ok)
            if ok:
                rig.runtime.write_lock.release()

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        held.append(not got[0])

    rig.exec.before_run = probe
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    assert env["ok"] is True, env
    assert held and all(held)


def test_a_policy_refusal_is_policy_denied_like_plan_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_media_rig(monkeypatch, tmp_path)
    ro.runtime.policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    env = loop(ro, items=rename_items(ro), steps=[RENAME], dry_run=True)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env


def test_a_rewriting_loop_runs_after_approval_and_reverts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    made = make_p32b_rig(monkeypatch, tmp_path)
    step = {"op": "desktop.delete", "args": {"targets": ["{{ item.target }}"]}}
    env = attended(made, items=[{"name": "r2", "target": reel2(made)}], steps=[step])
    assert env["ok"] is True, env
    assert [str(r.name.get_value()) for r in made.handles["reel_group"].reels] == ["Reel 1"]
    out = made.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is True, out
    assert [str(r.name.get_value()) for r in made.handles["reel_group"].reels] == [
        "Reel 1",
        "Reel 2",
    ]


def test_a_step_that_makes_output_is_a_child_the_revert_leaves_and_lists(rig: Rig) -> None:
    shelf = {"op": "batch.create_shelf_reel", "args": {"target": BG1, "name": "{{ item.shelf }}"}}
    items = [{"name": "r1", "target": REEL1, "new": "A", "shelf": "Shelf A"}]
    env = attended(rig, items=items, steps=[RENAME, shelf])
    assert env["ok"] is True, env
    groups = env["result"]["items"][0]["groups"]
    assert [g["revertible"] for g in groups] == [True, False]
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is True, out
    assert [k["op"] for k in out["result"]["kept"]] == ["batch.create_shelf_reel"]
    assert reel_names(rig) == ["Reel 1", "Reel 2"]
    assert "Shelf A" in [str(r.name.get_value()) for r in rig.handles["bg1"].shelf_reels]


def test_a_loop_whose_children_all_keep_their_output_has_nothing_to_put_back(rig: Rig) -> None:
    shelf = {"op": "batch.create_shelf_reel", "args": {"target": BG1, "name": "{{ item.shelf }}"}}
    env = attended(rig, items=[{"name": "a", "shelf": "S1"}], steps=[shelf])
    assert env["ok"] is True, env
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is False and out["errorCode"] == "PRECONDITION_UNMET", out
    assert [k["op"] for k in out["result"]["kept"]] == ["batch.create_shelf_reel"]


@dataclass
class _Sent:
    index: int
    op_name: str | None
    op: RegisteredOperation | None
    args: dict[str, Any]


def test_the_check_before_sending_refuses_a_value_a_reference_brought_in(rig: Rig) -> None:
    op = rig.registry.get("render.batch")
    assert op is not None
    check = loop_run.sending_check(args_checker(rig.runtime, loop_writes.SENT))
    sent = _Sent(0, op.name, op, {})
    checked = check(sent, {"target": BG1, "allow_overwrite": True})
    assert any("overwrite" in p for p in checked.problems)
    clean = check(sent, {"target": BG1})
    assert not any("overwrite" in p for p in clean.problems)


def _copy_loop(rig: Rig) -> dict[str, Any]:
    copy = {
        "op": "desktop.copy",
        "args": {"sources": ["{{ item.src }}"], "destination": reel2_selector(rig)},
    }
    return {"items": [{"name": "a", "src": C1}], "steps": [copy]}


def _fail_child_records(rig: Rig, monkeypatch: pytest.MonkeyPatch, *, after: int = 0) -> None:
    """The journal refuses the records that name a child under the parent beyond the first
    `after` ones."""
    real = rig.journal.append
    seen: list[int] = []

    def append(record: dict[str, Any]) -> None:
        if record.get("type") == "group" and record.get("parent") is not None:
            seen.append(1)
            if len(seen) > after:
                raise OSError("disk full")
        real(record)

    monkeypatch.setattr(rig.journal, "append", append)


def _own_groups(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, *, journal_on: str | None = None
) -> None:
    """desktop.copy makes a group of its own (the group the loop passes is dropped), and its
    envelope names it -- also when it ended with `journal_on` as the error code."""
    real = rig.runtime.do

    def do(op: str, args: Any, **kw: Any) -> dict[str, Any]:
        kw.pop("link", None)
        env = real(op, args, **kw)
        if (
            op == "desktop.copy"
            and env.get("journal") is None
            and journal_on is not None
            and env.get("errorCode") == journal_on
        ):
            env["journal"] = {"group": "g_own"}
        return env

    monkeypatch.setattr(rig.runtime, "do", do)


def test_a_group_an_op_made_itself_is_put_under_the_parent_after_it_ran(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _own_groups(rig, monkeypatch)
    env = attended(rig, **_copy_loop(rig))
    assert env["ok"] is True, env
    groups = [g["group"] for g in env["result"]["items"][0]["groups"]]
    made = [g for g in groups if g in Index(rig.records()).groups]
    assert len(made) == 1
    rows = Index(rig.records()).loop_child_records(parent_of(env))
    assert made[0] in [r["group"] for r in rows]
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is True, out
    assert names(rig.handles["reel2"]) == []


def test_a_child_that_could_not_be_recorded_is_named_with_how_to_revert_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _own_groups(rig, monkeypatch)
    _fail_child_records(rig, monkeypatch, after=1)
    env = attended(rig, **_copy_loop(rig))
    assert env["ok"] is False, env
    made = [r["group"] for r in rig.records() if r.get("type") == "intent"]
    assert len(made) == 1
    text = env["error"] + " " + str(env["hint"])
    assert "already ran" in text and made[0] in text
    assert f"journal.revert(group='{made[0]}')" in text
    assert "not covered by the parent" in text


def test_a_failed_record_does_not_hide_a_write_that_ended_without_an_answer(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _own_groups(rig, monkeypatch, journal_on="TIMEOUT")
    _fail_child_records(rig, monkeypatch, after=1)
    rig.exec.fault("desktop.copy", "TIMEOUT", when="after", skip=1)
    env = attended(rig, **_copy_loop(rig))
    assert env["errorCode"] == "TIMEOUT", env
    assert env["result"]["items"][0]["status"] == "unknown"
    assert env["result"]["failClosed"]["errorCode"] == "TIMEOUT"
    assert "journal.check" in str(env["hint"]) and "disk full" in env["error"]


def test_an_own_group_step_with_no_answer_is_unknown_and_journal_check_reads_it(rig: Rig) -> None:
    rig.exec.fault("desktop.copy", "TIMEOUT", when="after", skip=1)
    env = attended(rig, **_copy_loop(rig))
    assert env["errorCode"] == "TIMEOUT", env
    result = env["result"]
    assert result["items"][0]["status"] == "unknown" and result["unknown"] == "a"
    assert result["failClosed"]["errorCode"] == "TIMEOUT"
    assert "journal.check" in str(env["hint"])
    blocked = rig.do("desktop.rename", {"target": REEL1, "name": "z"})
    assert blocked["errorCode"] == "POLICY_DENIED"
    checked = rig.do("journal.check", {"group": parent_of(env)})
    assert checked["ok"] is True and checked["result"]["inDoubt"] >= 1, checked


def test_a_parent_with_a_child_in_doubt_is_not_reverted_at_all(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    parent = parent_of(env)
    intent = next(r for r in rig.records() if r.get("type") == "intent")
    rig.journal.append({**intent, "id": "i_doubt", "group": "g_doubt"})
    rig.journal.append(
        {
            "type": "group",
            "parent": parent,
            "group": "g_doubt",
            "key": "r3",
            "op": "desktop.rename",
            "step": "ren",
            "revertible": True,
        }
    )
    out = rig.do("journal.revert", {"group": parent})
    assert out["ok"] is False and out["errorCode"] == "PRECONDITION_UNMET", out
    assert "journal.check" in str(out["hint"]) and "g_doubt" in str(out)
    assert reel_names(rig) == ["A", "B"]
    single = rig.do("journal.revert", {"group": "g_doubt"})
    assert single["errorCode"] == out["errorCode"]


def test_an_approval_is_used_once(rig: Rig) -> None:
    preview = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    digest = preview["result"]["digest"]
    first = loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)
    assert first["ok"] is True, first
    again = loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET", again
    assert "already used" in again["error"] and again["result"]["digest"] != digest
    third = loop(rig, items=rename_items(rig), steps=[RENAME], approve=again["result"]["digest"])
    assert third["ok"] is True, third


def test_a_used_approval_is_read_from_the_journal_so_a_restart_does_not_give_it_back(
    rig: Rig,
) -> None:
    preview = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    digest = preview["result"]["digest"]
    assert loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)["ok"] is True
    old = rig.runtime
    rig.runtime = Runtime(  # a restarted server: nothing in memory, the same journal on disk
        registry=old.registry,
        policy=old.policy,
        journal=Journal(rig.journal.directory),
        run_dir=old.run_dir,
        hooks=old.hooks,
    )
    again = loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)
    assert again["ok"] is False and "already used" in again["error"], again
    assert plan_loop_confirm.runs_of(rig.runtime.journal.read_all().records, digest) == 1


def test_only_goes_on_after_an_approval_was_used(rig: Rig) -> None:
    first = attended(rig, items=three_items(rig), steps=[RENAME])
    assert first["ok"] is False
    rest = attended(rig, items=three_items(rig), steps=[RENAME], only=["r2"])
    assert rest["ok"] is True, rest


def test_a_bridged_own_group_step_is_recorded_before_it_is_sent(rig: Rig) -> None:
    rig.exec.fault("desktop.copy", "TIMEOUT", when="after", skip=1)
    env = attended(rig, **_copy_loop(rig))
    index = Index(rig.records())
    rows = index.loop_child_records(parent_of(env))
    assert len(rows) == 1 and rows[0]["group"] in index.groups
    assert index.in_doubt(parent_of(env))


def test_a_bridged_step_whose_child_cannot_be_recorded_is_not_sent(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_child_records(rig, monkeypatch)
    env = attended(rig, **_copy_loop(rig))
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert "nothing of this step was run" in env["error"]
    assert not any(r.get("type") == "intent" for r in rig.records())


def test_an_approval_that_another_call_used_between_the_check_and_the_lock_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    digest = preview["result"]["digest"]
    real = new_id

    def racing(prefix: str) -> str:
        # the other call got the lock first: its parent names the approval
        rig.journal.append_group("g_other", LOOP_GROUP_KIND, approval=digest)
        return real(prefix)

    monkeypatch.setattr("flame_mcp.ops.plan_loop.new_id", racing)
    env = loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "already used" in env["error"] and env["result"]["digest"] != digest
    assert rig.exec.calls == [] and [r["group"] for r in rig.records()] == ["g_other"]
    assert reel_names(rig) == ["Reel 1", "Reel 2"]


def test_an_approval_is_not_used_up_when_the_parent_could_not_be_written(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)
    digest = preview["result"]["digest"]
    real = loop_run.open_parent
    monkeypatch.setattr(loop_run, "open_parent", lambda *a, **k: "disk full")
    first = loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)
    assert first["ok"] is False and first["errorCode"] == "POLICY_DENIED", first
    monkeypatch.setattr(loop_run, "open_parent", real)
    again = loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)
    assert again["ok"] is True, again


def test_a_step_that_was_not_sent_leaves_no_child_to_keep_or_to_report(rig: Rig) -> None:
    look = {"id": "x", "op": "batch.info", "args": {"target": BG1}}
    shelf = {
        "op": "batch.create_shelf_reel",
        "args": {"target": BG1, "name": "{{ steps.x.result.nothing }}"},
    }
    env = attended(rig, items=[{"name": "a"}], steps=[look, shelf])
    assert env["ok"] is False, env
    assert env["result"]["items"][0]["groups"] == []
    out = rig.do("journal.revert", {"group": parent_of(env)})
    assert out["ok"] is False and out["errorCode"] == "PRECONDITION_UNMET", out
    assert out["result"]["kept"] == []
    assert [k["op"] for k in out["result"]["skipped"]] == ["batch.create_shelf_reel"]


# ---- the journal cannot take a group record; a child recorded twice ------------------------------


def _refuse_group_records(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str | None = None
) -> Callable[[dict[str, Any]], None]:
    """The journal refuses the group records (of `kind`); returns its own `append`."""
    real = rig.journal.append

    def append(record: dict[str, Any]) -> None:
        if record.get("type") == "group" and (kind is None or record.get("kind") == kind):
            raise OSError("disk full")
        real(record)

    monkeypatch.setattr(rig.journal, "append", append)
    return real


def test_a_loop_whose_parent_cannot_be_journaled_runs_nothing_and_keeps_its_approval(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = loop(rig, items=rename_items(rig), steps=[RENAME], dry_run=True)["result"]["digest"]
    real = _refuse_group_records(rig, monkeypatch)
    first = loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)
    assert first["ok"] is False and first["errorCode"] == "POLICY_DENIED", first
    assert "the journal cannot be written (disk full)" in first["error"]
    assert "writable" in first["hint"] and first["result"] == {"ran": False}
    assert rig.exec.calls == []
    monkeypatch.setattr(rig.journal, "append", real)
    again = loop(rig, items=rename_items(rig), steps=[RENAME], approve=digest)
    assert again["ok"] is True, again


def test_an_item_whose_run_group_cannot_be_journaled_is_failed_and_stops_the_loop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_group_records(rig, monkeypatch, kind=loop_run.GROUP_KIND_RUN)
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert "nothing of this item was run: the journal cannot be written (disk full)" in env["error"]
    assert [i["status"] for i in env["result"]["items"]] == ["failed", "not_run"]
    assert env["result"]["stoppedAt"] == "r1"
    assert reel_names(rig) == ["Reel 1", "Reel 2"]


def test_a_child_named_twice_is_listed_and_reverted_once(rig: Rig) -> None:
    env = attended(rig, items=rename_items(rig), steps=[RENAME])
    parent = parent_of(env)
    first = next(r for r in rig.records() if r.get("parent") == parent)
    rig.journal.append(dict(first))
    assert len(Index(rig.records()).loop_child_records(parent)) == 2
    shown = rig.do("journal.show", {"group": parent})["result"]
    assert [c["group"] for c in shown["children"]] == child_groups(env)
    out = rig.do("journal.revert", {"group": parent})
    assert out["ok"] is True and [r["key"] for r in out["result"]["children"]] == ["r2", "r1"]
