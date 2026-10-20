"""Tests for eval.run.

Covers the gate (off by default, read included; both effects need FLAME_MCP_ENABLE_EVAL=1 and
destructive also a target), the static scan in front of it, the checkpoint / intent / outcome
around a destructive run, journal.check of an eval intent, and journal.revert of an eval group
(fingerprint match, CONFLICT, force). Run through the real Runtime with a scripted
`transport.exec` over the fake Flame tree (journal_test_helpers.py)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import policy as policy_mod
from flame_mcp import server as server_mod
from flame_mcp.eval_run import code_sha256
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import BG1, Rig, make_rig

ENABLED = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
MAKE_PAIR = (
    "a = target.create_node('Mux')\n"
    "b = target.create_node('Mux')\n"
    "target.connect_nodes(a, 'Default', b, 'Default')\n"
    "_result = [_v(a.name), _v(b.name)]"
)


@pytest.fixture
def off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    """A rig whose server runs without FLAME_MCP_ENABLE_EVAL (the default)."""
    return make_rig(monkeypatch, tmp_path)


@pytest.fixture
def ev(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    """A rig whose server runs with FLAME_MCP_ENABLE_EVAL=1."""
    return make_rig(monkeypatch, tmp_path, policy=ENABLED)


@pytest.fixture
def rig(ev: Rig) -> Rig:
    return ev


def run(
    rig: Rig, code: str, effect: str = "read", target: dict[str, Any] | None = BG1, **extra: Any
) -> dict[str, Any]:
    args: dict[str, Any] = {"code": code, "effect": effect, **extra}
    if target is not None:
        args["target"] = target
    return rig.do("eval.run", args)


def names(rig: Rig) -> list[str]:
    return sorted(str(n.name.get_value()) for n in rig.handles["bg1"].nodes)


def fingerprint(rig: Rig) -> str:
    value: str = rig.do("batch.fingerprint", {"target": BG1})["result"]["fingerprint"]
    return value


def sent_labels(rig: Rig) -> list[str | None]:
    return rig.exec.labels()


def test_a_read_returns_the_value_when_eval_is_enabled(rig: Rig) -> None:
    env = run(rig, "_result = [_v(n.name) for n in target.nodes]")
    assert env["ok"] is True and sorted(env["result"]) == names(rig)
    assert env["journal"] is None and env["execution"]["effect"] == "read"


def test_a_read_without_a_target_works_and_flame_is_in_scope(rig: Rig) -> None:
    env = run(rig, "_result = _v(flame.projects.current_project.name)", target=None)
    assert env["ok"] is True and env["result"] == "TestProject"


@pytest.mark.parametrize("effect", ["read", "reversible", "create", "destructive"])
@pytest.mark.parametrize("switch", [False, True])
def test_every_eval_is_refused_under_readonly_whatever_effect_it_declares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, effect: str, switch: bool
) -> None:
    env = {"FLAME_MCP_READONLY": "1", **({"FLAME_MCP_ENABLE_EVAL": "1"} if switch else {})}
    ro = make_rig(monkeypatch, tmp_path, policy=Policy.from_env(env))
    out = run(ro, "_result = 1 + 1", effect)
    assert out["ok"] is False and out["errorCode"] == "POLICY_DENIED", out
    assert "FLAME_MCP_READONLY" in out["error"]
    assert "READONLY" in str(out["hint"])
    assert sent_labels(ro) == []


def test_a_computed_write_declared_as_read_does_not_reach_flame_under_readonly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    code = "n = target.nodes[0]\ngetattr(n.name, 'set_' + 'value')('changed')"
    before = names(ro)
    out = run(ro, code)
    assert out["errorCode"] == "POLICY_DENIED"
    assert sent_labels(ro) == [] and names(ro) == before


def test_eval_run_is_not_offered_to_a_readonly_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    op = ro.registry.get("eval.run")
    assert op is not None
    assert op not in policy_mod.visible_ops(ro.registry, ro.runtime.policy)


@pytest.mark.parametrize("effect", ["read", "destructive"])
def test_every_eval_is_refused_without_the_switch_and_names_it(off: Rig, effect: str) -> None:
    env = run(off, "_result = 1 + 1", effect)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert "disabled" in env["error"] and "FLAME_MCP_ENABLE_EVAL=1" in str(env["hint"])
    assert sent_labels(off) == [] and len(names(off)) == 4


def test_eval_run_is_not_in_the_catalog_or_the_do_enum_without_the_switch(
    off: Rig, ev: Rig
) -> None:
    hidden = {op.name for op in policy_mod.visible_ops(off.registry, off.runtime.policy)}
    shown = {op.name for op in policy_mod.visible_ops(ev.registry, ev.runtime.policy)}
    assert "eval.run" not in hidden and "eval.run" in shown
    off_schema = server_mod._flame_do_input_schema(off.registry, off.runtime.policy)
    assert "eval.run" not in off_schema["properties"]["op"]["enum"]
    ev_schema = server_mod._flame_do_input_schema(ev.registry, ev.runtime.policy)
    assert "eval.run" in ev_schema["properties"]["op"]["enum"]
    listing = server_mod._flame_catalog_sync(off.registry, off.runtime.policy, None, None)
    assert "eval.run" not in [o["name"] for o in listing["result"]["operations"]]
    detail = server_mod._flame_catalog_sync(off.registry, off.runtime.policy, None, "eval.run")
    assert detail["ok"] is False and detail["errorCode"] == "POLICY_DENIED"
    assert "FLAME_MCP_ENABLE_EVAL=1" in str(detail["hint"])


def test_the_policy_summary_says_whether_eval_is_enabled(off: Rig, ev: Rig) -> None:
    assert "eval=disabled" in policy_mod.summary(off.runtime.policy)
    assert "eval=ENABLED" in policy_mod.summary(ev.runtime.policy)


def test_a_read_sees_the_prelude_helpers(rig: Rig) -> None:
    code = "_result = [_v(target.name), _j(target.uid), _ja(target.name), _OpError.__name__]"
    assert run(rig, code)["result"] == ["BG1", "uid-b1", "BG1", "_OpError"]


def test_a_result_that_is_not_json_comes_back_as_its_repr_not_as_a_failure(rig: Rig) -> None:
    env = run(rig, "_result = target")
    assert env["ok"] is True
    assert env["result"]["notJsonSerializable"] is True and "PyBatch" in env["result"]["type"]


def test_a_user_exception_is_an_error_naming_the_code_not_the_wrapper(rig: Rig) -> None:
    env = run(rig, "x = 1\nraise ValueError('boom')")
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "boom" in env["error"] and "<eval.run:code>" in str(env["traceback"])


def test_a_user_op_error_keeps_its_error_code(rig: Rig) -> None:
    env = run(rig, "raise _OpError('SELECTOR_EMPTY', 'nothing here')")
    assert env["errorCode"] == "SELECTOR_EMPTY" and env["error"] == "nothing here"


def test_timeout_s_reaches_the_exec_and_is_bounded(rig: Rig) -> None:
    run(rig, "_result = 1", timeout_s=7)
    assert rig.exec.calls[-1].timeout_s == 7.0
    run(rig, "_result = 1")
    assert rig.exec.calls[-1].timeout_s == 10.0  # the main-thread default
    too_long = run(rig, "_result = 1", timeout_s=1000)
    assert too_long["errorCode"] == "INVALID_ARGS" and "timeout_s" in too_long["error"]
    assert run(rig, "_result = 1", timeout_s=0)["errorCode"] == "INVALID_ARGS"


def test_an_unknown_effect_is_refused(rig: Rig) -> None:
    env = run(rig, "_result = 1", effect="reversible")
    assert env["errorCode"] == "INVALID_ARGS" and "effect must be one of" in env["error"]
    assert sent_labels(rig) == []


@pytest.mark.parametrize(
    ("code", "rule"),
    [
        ("for p in flame.projects:\n    pass", "flame.projects"),
        ("flame.execute_shortcut('Undo')", "shortcut"),
        ("import threading", "thread"),
        ("exec('1')", "dynamic-exec"),
        ("target.create_node('Mux')", "write-in-read"),
        ("def (", "syntax"),
    ],
)
def test_the_scan_refuses_before_anything_is_sent(rig: Rig, code: str, rule: str) -> None:
    env = run(rig, code)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert [v["rule"] for v in env["result"]["violations"]] == [rule]
    assert env["result"]["ran"] is False and "static scan" in env["error"]
    assert sent_labels(rig) == []
    assert rig.handles["bg1"].save_calls == [] and names(rig)[0] == "action1"


def test_the_scan_applies_to_a_destructive_run_too_and_before_its_checkpoint(ev: Rig) -> None:
    env = run(ev, "flame.execute_shortcut('Undo')", "destructive")
    assert env["errorCode"] == "INVALID_ARGS"
    assert sent_labels(ev) == [] and ev.handles["bg1"].save_calls == []


def test_destructive_under_readonly_is_refused_even_with_the_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    both = make_rig(
        monkeypatch,
        tmp_path,
        policy=Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1", "FLAME_MCP_READONLY": "1"}),
    )
    env = run(both, MAKE_PAIR, "destructive")
    assert env["errorCode"] == "POLICY_DENIED" and "READONLY" in env["error"]
    assert sent_labels(both) == []


def test_destructive_needs_a_target(ev: Rig) -> None:
    env = run(ev, "_result = 1", "destructive", target=None)
    assert env["errorCode"] == "INVALID_ARGS" and "target" in env["error"]
    assert sent_labels(ev) == []


def test_destructive_respects_allow_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    limited = make_rig(
        monkeypatch,
        tmp_path,
        policy=Policy.from_env(
            {"FLAME_MCP_ENABLE_EVAL": "1", "FLAME_MCP_ALLOW_EFFECTS": "read,create"}
        ),
    )
    env = run(limited, MAKE_PAIR, "destructive")
    assert env["errorCode"] == "POLICY_DENIED" and "FLAME_MCP_ALLOW_EFFECTS" in env["error"]


@pytest.mark.parametrize("effect", ["read", "destructive"])
def test_the_second_gate_inside_the_run_holds_even_if_the_precheck_is_bypassed(
    off: Rig, effect: str
) -> None:
    op = off.registry.get("eval.run")
    assert op is not None and op.wrap is not None
    env = op.wrap(off.runtime, op, {"code": MAKE_PAIR, "effect": effect, "target": BG1})
    assert env["errorCode"] == "POLICY_DENIED" and sent_labels(off) == []


def test_the_description_carries_the_warnings_the_decision_requires(rig: Rig) -> None:
    op = rig.registry.get("eval.run")
    assert op is not None
    text = " ".join(op.description.split())
    assert "FLAME_MCP_ENABLE_EVAL=1" in text
    assert "permissions of the user running Flame" in text
    assert "files, network and processes" in text
    assert "accident prevention, not a defence" in text
    assert "stop waiting" in text and "does not stop the code" in text
    assert "checkpoint covers the target group only" in text
    assert "smart_replace_media on one crashed Flame" in text and "turned it into a Gap" in text
    assert op.effect == "read" and op.thread == "main"


def test_a_destructive_run_checkpoints_then_journals_the_intent_then_runs(ev: Rig) -> None:
    before = fingerprint(ev)
    env = run(ev, MAKE_PAIR, "destructive")
    assert env["ok"] is True and env["result"] == ["mux1", "mux2"]
    assert len(names(ev)) == 6
    labels = sent_labels(ev)
    assert labels.index("batch.checkpoint") < labels.index("eval.run")
    run_call = next(c for c in ev.exec.calls if c.label == "eval.run")
    assert run_call.journal_types == ["checkpoint", "intent"]  # both flushed before the code runs
    [checkpoint] = [r for r in ev.records() if r["type"] == "checkpoint"]
    [intent] = [r for r in ev.records() if r["type"] == "intent"]
    [outcome] = [r for r in ev.records() if r["type"] == "outcome"]
    assert intent["op"] == "eval.run" and intent["code"] == MAKE_PAIR
    assert intent["codeSha256"] == code_sha256(MAKE_PAIR) and len(intent["codeSha256"]) == 64
    assert intent["checkpoint"] == checkpoint["id"] == intent["revertCheckpoint"]
    assert intent["target"] == {"kind": "batch", "uid": "uid-b1"}
    assert intent["fingerprintBefore"] == before == checkpoint["fingerprint"]
    assert checkpoint["group"] == intent["group"] == outcome["group"]
    assert outcome["ok"] is True and outcome["fingerprintAfter"] == fingerprint(ev) != before
    assert outcome["changed"] is True
    assert env["journal"]["group"] == intent["group"] and env["journal"]["changed"] is True
    assert env["journal"]["revert"] == {"op": "journal.revert", "args": {"group": intent["group"]}}
    assert env["execution"]["effect"] == "destructive" and env["execution"]["undo"] == "checkpoint"


def test_the_run_uses_the_checkpointed_group_even_when_addressed_by_name(ev: Rig) -> None:
    env = run(
        ev, "_result = _v(target.uid)", "destructive", target={"kind": "batch", "name": "BG1"}
    )
    assert env["result"] == "uid-b1"
    [intent] = [r for r in ev.records() if r["type"] == "intent"]
    assert intent["args"]["target"] == {"kind": "batch", "name": "BG1"}
    assert intent["target"] == {"kind": "batch", "uid": "uid-b1"}


def test_no_checkpoint_no_run(ev: Rig) -> None:
    ev.handles["bg1"].save_writes_nothing = True
    env = run(ev, MAKE_PAIR, "destructive")
    assert env["ok"] is False and "nothing was run" in env["error"]
    assert "eval.run" not in sent_labels(ev) and len(names(ev)) == 4
    assert [r for r in ev.records() if r["type"] == "intent"] == []


def test_no_intent_no_run(ev: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ev.journal.append

    def refuse_intents(record: dict[str, Any]) -> None:
        if record["type"] == "intent":
            raise OSError("disk full")
        real(record)

    monkeypatch.setattr(ev.journal, "append", refuse_intents)
    env = run(ev, MAKE_PAIR, "destructive")
    assert env["errorCode"] == "POLICY_DENIED" and "nothing was run" in env["error"]
    assert "eval.run" not in sent_labels(ev) and len(names(ev)) == 4
    assert env["result"]["checkpoint"]  # the checkpoint exists; the caller is told which


def test_a_target_group_that_does_not_exist_runs_nothing(ev: Rig) -> None:
    env = run(ev, MAKE_PAIR, "destructive", target={"kind": "batch", "name": "nope"})
    assert env["errorCode"] == "SELECTOR_EMPTY" and "eval.run" not in sent_labels(ev)


def test_code_that_fails_half_way_still_gets_an_outcome_and_can_be_reverted(ev: Rig) -> None:
    before = fingerprint(ev)
    env = run(ev, "target.create_node('Mux')\nraise RuntimeError('half way')", "destructive")
    assert env["ok"] is False and "half way" in env["error"]
    group = env["journal"]["group"]
    index = Index(ev.records())
    [view] = index.groups[group]
    assert view.outcome is not None and view.outcome["ok"] is False
    assert view.outcome["fingerprintAfter"] != before and view.state() == "failed"
    assert ev.do("journal.revert", {"group": group})["ok"] is True
    assert fingerprint(ev) == before


def test_read_evals_are_not_journaled_and_not_blocked_by_an_unsettled_write(ev: Rig) -> None:
    ev.exec.fault("eval.run", "TIMEOUT", when="after")
    run(ev, MAKE_PAIR, "destructive")
    assert ev.runtime.guard.denial() is not None
    assert run(ev, "_result = 1")["ok"] is True
    blocked = run(ev, MAKE_PAIR, "destructive")
    assert blocked["errorCode"] == "POLICY_DENIED" and "journal.check" in str(blocked["hint"])


def test_an_unknown_end_blocks_writes_and_journal_check_reads_the_fingerprint(ev: Rig) -> None:
    ev.exec.fault("eval.run", "TIMEOUT", when="after")  # it ran; the answer was lost
    env = run(ev, MAKE_PAIR, "destructive")
    assert env["errorCode"] == "TIMEOUT" and "not known whether the code ran" in env["hint"]
    assert env["journal"]["checkpoint"] in env["hint"]
    assert [r["type"] for r in ev.records() if r["type"] == "outcome"] == []
    found = ev.do("journal.check")
    [entry] = found["result"]["checked"]
    assert entry["verdict"] == "diverged"  # changed, and there is no recorded result to match
    assert ev.runtime.guard.denial() is None
    # ... and it can be put back, but only with force: no outcome fingerprint to compare
    group = env["journal"]["group"]
    refused = ev.do("journal.revert", {"group": group})
    assert refused["errorCode"] == "CONFLICT" and len(names(ev)) == 6
    assert ev.do("journal.revert", {"group": group, "force": True})["ok"] is True
    assert len(names(ev)) == 4


def test_an_unknown_end_of_code_that_never_ran_checks_as_not_landed(ev: Rig) -> None:
    ev.exec.fault("eval.run", "BRIDGE_LOST", when="before")
    run(ev, MAKE_PAIR, "destructive")
    [entry] = ev.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "not_landed" and "cannot be ruled out" in entry["detail"]


def test_revert_is_refused_while_the_unsettled_eval_blocks_writes(ev: Rig) -> None:
    ev.exec.fault("eval.run", "TIMEOUT", when="after")
    env = run(ev, MAKE_PAIR, "destructive")
    denied = ev.do("journal.revert", {"group": env["journal"]["group"]})
    assert denied["ok"] is False  # the fail-closed guard is still on: check comes first


def test_revert_restores_the_group_while_its_fingerprint_is_the_one_the_run_left(ev: Rig) -> None:
    before = fingerprint(ev)
    env = run(ev, MAKE_PAIR, "destructive")
    group = env["journal"]["group"]
    back = ev.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert fingerprint(ev) == before and len(names(ev)) == 4
    assert back["result"]["reverted"] == group and back["result"]["matched"] is True
    index = Index(ev.records())
    assert index.group_state(group) == "reverted"
    assert index.group_summary(group)["revertedBy"] == [back["journal"]["group"]]
    [restore_intent] = index.groups[back["journal"]["group"]]
    assert restore_intent.intent["reverts"] == group and "forced" not in restore_intent.intent
    again = ev.do("journal.revert", {"group": group})
    assert again["ok"] is False and "already reverted" in again["error"]


def test_revert_is_a_conflict_when_the_group_changed_after_the_run(ev: Rig) -> None:
    env = run(ev, MAKE_PAIR, "destructive")
    ev.handles["bg1"].create_node("Blur")  # someone else's work
    load_calls = list(ev.handles["bg1"].load_calls)
    refused = ev.do("journal.revert", {"group": env["journal"]["group"]})
    assert refused["ok"] is False and refused["errorCode"] == "CONFLICT"
    assert refused["result"]["refused"] is True
    assert refused["result"]["expectedFingerprint"] != refused["result"]["currentFingerprint"]
    assert ev.handles["bg1"].load_calls == load_calls and len(names(ev)) == 7  # nothing touched
    assert Index(ev.records()).group_state(env["journal"]["group"]) != "reverted"


def test_force_restores_anyway_and_the_journal_records_that_it_was_forced(ev: Rig) -> None:
    before = fingerprint(ev)
    env = run(ev, MAKE_PAIR, "destructive")
    group = env["journal"]["group"]
    ev.handles["bg1"].create_node("Blur")
    forced = ev.do("journal.revert", {"group": group, "force": True})
    assert forced["ok"] is True and fingerprint(ev) == before
    assert forced["result"]["forced"]["used"] is True
    [restore_intent] = Index(ev.records()).groups[forced["journal"]["group"]]
    assert restore_intent.intent["forced"]["used"] is True
    assert restore_intent.intent["forced"]["currentFingerprint"] != before
    assert restore_intent.intent["reverts"] == group


def test_force_when_the_fingerprints_match_is_not_recorded_as_a_force(ev: Rig) -> None:
    env = run(ev, MAKE_PAIR, "destructive")
    back = ev.do("journal.revert", {"group": env["journal"]["group"], "force": True})
    [restore_intent] = Index(ev.records()).groups[back["journal"]["group"]]
    assert back["ok"] is True and "forced" not in restore_intent.intent


def test_a_second_eval_then_extra_nodes_then_revert_conflicts_and_force_restores(ev: Rig) -> None:
    first = run(ev, MAKE_PAIR, "destructive")
    second = run(ev, "_result = _v(target.create_node('Blur').name)", "destructive")
    ev.handles["bg1"].create_node("Blur")
    group = second["journal"]["group"]
    assert ev.do("journal.revert", {"group": group})["errorCode"] == "CONFLICT"
    assert ev.do("journal.revert", {"group": group, "force": True})["ok"] is True
    assert len(names(ev)) == 6  # back to the state after the first eval
    assert first["journal"]["group"] != group


def test_revert_of_an_eval_whose_checkpoint_file_is_gone_changes_nothing(ev: Rig) -> None:
    env = run(ev, MAKE_PAIR, "destructive")
    os.remove(next(r for r in ev.records() if r["type"] == "checkpoint")["path"])
    saves = len(ev.handles["bg1"].save_calls)
    back = ev.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["errorCode"] == "PRECONDITION_UNMET" and ev.handles["bg1"].load_calls == []
    assert len(names(ev)) == 6 and len(ev.handles["bg1"].save_calls) == saves


def test_eval_groups_show_up_in_journal_list_and_show(ev: Rig) -> None:
    env = run(ev, MAKE_PAIR, "destructive")
    group = env["journal"]["group"]
    [item] = [i for i in ev.do("journal.list")["result"]["items"] if i["group"] == group]
    assert item["ops"] == ["eval.run"] and item["state"] == "landed"
    shown = ev.do("journal.show", {"group": group})["result"]["records"]
    assert [r["type"] for r in shown] == ["checkpoint", "intent", "outcome"]


def test_plan_run_refuses_a_destructive_eval_step_and_runs_nothing(ev: Rig) -> None:
    plan = ev.do(
        "plan.run",
        {
            "steps": [
                {"op": "batch.list", "args": {}},
                {
                    "op": "eval.run",
                    "args": {"code": MAKE_PAIR, "effect": "destructive", "target": BG1},
                },
            ]
        },
    )
    assert plan["ok"] is False and plan["errorCode"] == "INVALID_ARGS"
    assert plan["result"]["issues"][0]["index"] == 1 and sent_labels(ev) == []


def test_plan_run_refuses_an_eval_step_without_the_switch(off: Rig) -> None:
    plan = off.do(
        "plan.run",
        {"steps": [{"op": "eval.run", "args": {"code": "_result = 5", "effect": "read"}}]},
    )
    assert plan["ok"] is False and plan["errorCode"] == "POLICY_DENIED"
    assert "FLAME_MCP_ENABLE_EVAL=1" in str(plan["hint"]) and sent_labels(off) == []


def test_plan_run_runs_a_read_eval_step_and_scans_it(rig: Rig) -> None:
    good = rig.do(
        "plan.run",
        {"steps": [{"op": "eval.run", "args": {"code": "_result = 5", "effect": "read"}}]},
    )
    assert good["ok"] is True and good["result"]["steps"][0]["result"] == 5
    bad = rig.do(
        "plan.run",
        {"steps": [{"op": "eval.run", "args": {"code": "x.delete()", "effect": "read"}}]},
    )
    assert bad["errorCode"] == "INVALID_ARGS" and "static scan" in str(bad["result"]["issues"])
