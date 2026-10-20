"""The journaled write path (journaled_write.py + Runtime): intent-before-write, the
compare-and-swap, the outcome, and the fail-closed guard. Run against the real Runtime with a
scripted `transport.exec` over a fake Flame tree (see journal_test_helpers.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp.policy import Policy
from flame_mcp.runtime import TestHooks, TestHooksConfigError
from journal_test_helpers import COMP, REEL_WIRETAP, TREE_REEL_ID, Rig, make_rig
from test_ops_attributes import ColourLabelAttr, add_attr


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def set_blend(rig: Rig, value: str = "Gimp") -> dict[str, Any]:
    return rig.do("node.set_attributes", {"target": COMP, "values": {"blend_type": value}})


def test_intent_is_flushed_to_the_file_before_the_write_is_sent(rig: Rig) -> None:
    env = set_blend(rig)
    assert env["ok"] is True, env
    write_call = next(c for c in rig.exec.calls if c.label == "node.set_attributes")
    assert write_call.journal_types == ["intent"]  # on disk, complete, no outcome yet
    read_call = next(c for c in rig.exec.calls if c.label == "node.get_attributes")
    assert read_call.journal_types == []  # the pre-read happens before anything is journaled


def test_records_are_intent_then_outcome_with_the_previous_and_written_values(rig: Rig) -> None:
    env = set_blend(rig)
    intent, outcome = rig.records()
    assert (intent["type"], outcome["type"]) == ("intent", "outcome")
    assert intent["id"] == outcome["id"] == env["journal"]["id"]
    assert intent["group"] == outcome["group"] == env["journal"]["group"]
    assert intent["op"] == "node.set_attributes"
    assert intent["before"] == {"blend_type": "Flame"}
    assert intent["after"] == {"blend_type": "Gimp"}
    assert intent["args"] == {"target": COMP, "values": {"blend_type": "Gimp"}}
    assert intent["target"] == {
        "kind": "node",
        "batch": {"kind": "batch", "uid": "uid-b1"},
        "name": "comp1",
    }
    assert (outcome["ok"], outcome["applied"], outcome["failed"], outcome["errorCode"]) == (
        True,
        ["blend_type"],
        {},
        None,
    )
    assert outcome["readBack"] == {"blend_type": "Gimp"}
    assert rig.handles["comp"].blend_type.get_value() == "Gimp"


def test_the_envelope_names_its_journal_records_and_keeps_the_revert(rig: Rig) -> None:
    env = set_blend(rig)
    assert set(env["journal"]) == {"group", "id"}
    assert env["result"]["revert"]["op"] == "node.set_attributes"
    assert env["undoSteps"] == 0
    read = rig.do("node.get_attributes", {"target": COMP, "keys": ["blend_type"]})
    assert read["journal"] is None


def test_the_write_goes_to_the_stable_address_not_the_callers_selector(rig: Rig) -> None:
    env = rig.do(
        "desktop.rename", {"target": {"kind": "path", "path": ["P1O", "R1"]}, "name": "R1b"}
    )
    assert env["ok"] is True, env
    intent = rig.records()[0]
    assert intent["target"] == REEL_WIRETAP
    write_call = next(c for c in rig.exec.calls if c.label == "desktop.rename")
    assert "target = _resolve_path(" not in write_call.code  # the op's own source, not the prelude
    assert f"target = _resolve_wiretap({TREE_REEL_ID!r})" in write_call.code


def test_a_call_may_join_an_existing_group(rig: Rig) -> None:
    first = set_blend(rig, "Gimp")
    second = rig.runtime.call(
        "node.set_attributes",
        {"target": COMP, "values": {"blend_type": "Photoshop"}},
        group=first["journal"]["group"],
    )
    assert second["journal"]["group"] == first["journal"]["group"]
    assert second["journal"]["id"] != first["journal"]["id"]


def test_rename_is_journaled_and_reports_what_a_journal_needs(rig: Rig) -> None:
    env = rig.do("desktop.rename", {"target": REEL_WIRETAP, "name": "R1b"})
    assert env["ok"] is True, env
    intent, outcome = rig.records()
    assert (intent["before"], intent["after"]) == ({"name": "R1"}, {"name": "R1b"})
    assert outcome["applied"] == ["name"] and outcome["readBack"] == {"name": "R1b"}


@pytest.mark.parametrize(
    ("op", "target", "values"),
    [
        ("desktop.set_attributes", REEL_WIRETAP, {"name": "R1c"}),
        ("batch.set_attributes", {"kind": "batch", "name": "BG1"}, {"name": "BG1c"}),
        ("node.set_attributes", COMP, {"pos_x": 5.0, "blend_type": "Gimp"}),
        (
            "segment.set_attributes",
            {
                "kind": "segment",
                "parent": {"kind": "wiretap", "id": "/projects/p/ws/P1O/Seq1"},
                "version": 0,
                "track": 0,
                "index": 1,
            },
            {"name": "renamed segment"},
        ),
    ],
)
def test_every_set_attributes_category_journals_and_reverts(
    rig: Rig, op: str, target: dict[str, Any], values: dict[str, Any]
) -> None:
    env = rig.do(op, {"target": target, "values": values})
    assert env["ok"] is True, env
    assert rig.records()[0]["op"] == op
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    landed = rig.do("journal.show", {"group": env["journal"]["group"]})
    assert landed["result"]["state"] == "reverted"


def change_behind_the_servers_back(rig: Rig, value: str = "Photoshop") -> TestHooks:
    """A test hook whose 'pause' is the moment somebody else changes the value in Flame."""

    def sleeper(_seconds: float) -> None:
        rig.handles["comp"].blend_type._value = value

    return TestHooks(pause_after_preread_s=3.0, sleep=sleeper)


def test_a_value_changed_between_read_and_write_is_a_conflict_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.runtime.hooks = change_behind_the_servers_back(rig)
    env = set_blend(rig, "Gimp")
    assert env["ok"] is False and env["errorCode"] == "CONFLICT"
    assert env["retryable"] is True
    assert rig.handles["comp"].blend_type.set_calls == []  # set_value was never called
    assert rig.handles["comp"].blend_type.get_value() == "Photoshop"  # the other writer's value
    conflicts = env["result"]["conflicts"]
    assert conflicts == {"blend_type": {"expected": "Flame", "current": "Photoshop"}}
    assert "expected 'Flame'" in env["error"] and "Photoshop" in env["error"]
    assert env["partial"] == {"applied": 0, "failed": 1}
    assert env["journal"] is not None


def test_a_conflict_is_journaled_as_an_outcome_with_the_conflict_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    rig.runtime.hooks = change_behind_the_servers_back(rig)
    env = set_blend(rig)
    intent, outcome = rig.records()
    assert intent["before"] == {"blend_type": "Flame"}  # what the server had read
    assert (outcome["ok"], outcome["errorCode"], outcome["applied"]) == (False, "CONFLICT", [])
    listed = rig.do("journal.list")["result"]["items"]
    assert listed[0]["group"] == env["journal"]["group"] and listed[0]["state"] == "conflict"


def test_one_conflicting_key_stops_every_key_of_the_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    comp = rig.handles["comp"]

    def sleeper(_seconds: float) -> None:
        comp.pos_x._value = 42.0  # only the SECOND key changes

    rig.runtime.hooks = TestHooks(pause_after_preread_s=1.0, sleep=sleeper)
    env = rig.do(
        "node.set_attributes",
        {"target": COMP, "values": {"blend_type": "Gimp", "pos_x": 7.0}},
    )
    assert env["errorCode"] == "CONFLICT"
    assert list(env["result"]["conflicts"]) == ["pos_x"]
    assert comp.blend_type.set_calls == [] and comp.pos_x.set_calls == []
    assert comp.blend_type.get_value() == "Flame" and comp.pos_x.get_value() == 42.0


def test_rename_conflict_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    reel = rig.handles["tree_reel"]

    def sleeper(_seconds: float) -> None:
        reel.name.set_value("renamed by someone else")

    rig.runtime.hooks = TestHooks(pause_after_preread_s=1.0, sleep=sleeper)
    env = rig.do("desktop.rename", {"target": REEL_WIRETAP, "name": "mine"})
    assert env["ok"] is False and env["errorCode"] == "CONFLICT"
    assert reel.name.get_value() == "renamed by someone else"
    assert env["result"]["conflicts"] == {
        "name": {"expected": "R1", "current": "renamed by someone else"}
    }


def test_no_change_in_between_means_no_conflict(rig: Rig) -> None:
    assert set_blend(rig)["ok"] is True


def test_a_caller_cannot_smuggle_in_its_own_expected_values(rig: Rig) -> None:
    env = rig.do(
        "node.set_attributes",
        {"target": COMP, "values": {"blend_type": "Gimp"}, "_expected": {"blend_type": "x"}},
    )
    assert env["errorCode"] == "INVALID_ARGS" and "_expected" in env["error"]
    assert rig.exec.calls == []


def test_a_failed_pre_read_writes_nothing_and_journals_nothing(rig: Rig) -> None:
    rig.exec.fault("node.get_attributes", "BRIDGE_UNAVAILABLE")
    env = set_blend(rig)
    assert env["ok"] is False and env["errorCode"] == "BRIDGE_UNAVAILABLE"
    assert env["error"].startswith("pre-read failed, nothing was written")
    assert env["execution"]["op"] == "node.set_attributes"
    assert rig.records() == [] and rig.write_labels() == []
    assert rig.runtime.guard.blocked() == []  # certainly nothing was written: no fail-closed


def test_an_attribute_that_cannot_be_read_first_is_refused_before_any_intent(rig: Rig) -> None:
    env = rig.do("node.set_attributes", {"target": COMP, "values": {"pos_x": 1.0, "no_such": 1}})
    assert env["errorCode"] == "INVALID_ARGS" and "no such attribute" in env["error"]
    # the same shape the op's own preflight rejection has
    assert list(env["result"]["failed"]) == ["no_such"]
    assert env["result"]["failed"]["no_such"].startswith("rejected:")
    assert env["partial"] == {"applied": 0, "failed": 1}
    assert rig.records() == [] and rig.write_labels() == []
    assert rig.handles["comp"].pos_x.set_calls == []


def test_empty_values_are_refused_without_reading_flame(rig: Rig) -> None:
    env = rig.do("node.set_attributes", {"target": COMP, "values": {}})
    assert env["errorCode"] == "INVALID_ARGS"
    assert rig.exec.calls == []


def test_a_target_that_does_not_resolve_reports_the_selector_error(rig: Rig) -> None:
    missing = {"kind": "node", "batch": BG1_SELECTOR, "name": "nope"}
    env = rig.do("node.set_attributes", {"target": missing, "values": {"pos_x": 1.0}})
    assert env["errorCode"] == "SELECTOR_EMPTY"
    assert rig.records() == []


BG1_SELECTOR = {"kind": "batch", "name": "BG1"}


def test_a_value_flame_rejects_is_a_journaled_failure(rig: Rig) -> None:
    env = set_blend(rig, "NoSuchMode")
    assert env["errorCode"] == "INVALID_ARGS"
    intent, outcome = rig.records()
    assert outcome["ok"] is False and outcome["applied"] == []
    assert "NoSuchMode" in str(outcome["failed"])
    assert rig.handles["comp"].blend_type.set_calls == []
    assert rig.do("journal.list")["result"]["items"][0]["state"] == "failed"
    assert intent["id"] == outcome["id"]


def test_the_journal_being_unwritable_refuses_the_write(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_record: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.journal, "append", broken)
    env = set_blend(rig)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert "journal cannot be written" in env["error"]
    assert rig.write_labels() == []
    assert rig.handles["comp"].blend_type.get_value() == "Flame"


def test_an_outcome_that_cannot_be_written_closes_the_door_but_keeps_the_answer(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_append = rig.journal.append

    def failing_outcome(record: dict[str, Any]) -> None:
        if record["type"] == "outcome":
            raise OSError("disk full")
        real_append(record)

    monkeypatch.setattr(rig.journal, "append", failing_outcome)
    env = set_blend(rig)
    assert env["ok"] is True and any("outcome could not be journaled" in d for d in env["degraded"])
    assert rig.runtime.guard.denial() is not None


@pytest.mark.parametrize("code", ["TIMEOUT", "BRIDGE_LOST", "BRIDGE_UNAVAILABLE"])
def test_a_write_that_ends_without_an_answer_leaves_an_open_intent_and_closes_the_door(
    rig: Rig, code: str
) -> None:
    rig.exec.fault("node.set_attributes", code, when="before")
    env = set_blend(rig)
    assert env["ok"] is False and env["errorCode"] == code
    assert env["journal"] is not None
    assert "journal.check" in env["hint"]
    types = [r["type"] for r in rig.records()]
    assert types == ["intent", "unresolved"]  # no outcome
    assert rig.records()[1]["errorCode"] == code
    blocked = rig.runtime.guard.blocked()
    assert len(blocked) == 1 and blocked[0].group == env["journal"]["group"]

    denied = rig.do("node.set_attributes", {"target": COMP, "values": {"pos_x": 1.0}})
    assert denied["ok"] is False and denied["errorCode"] == "POLICY_DENIED"
    assert env["journal"]["group"] in denied["error"]
    assert "journal.check" in denied["hint"]
    assert rig.write_labels().count("node.set_attributes") == 1  # only the first was ever sent


def test_reads_still_work_while_writes_are_refused(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "TIMEOUT")
    set_blend(rig)
    calls: list[tuple[str, dict[str, Any]]] = [
        ("node.get_attributes", {"target": COMP}),
        ("node.list", {"target": BG1_SELECTOR}),
        ("journal.list", {}),
    ]
    for op, args in calls:
        assert rig.do(op, args)["ok"] is True, op


def test_every_kind_of_write_is_refused_while_the_door_is_closed(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "TIMEOUT")
    first = set_blend(rig)
    writes: list[tuple[str, dict[str, Any]]] = [
        ("desktop.rename", {"target": REEL_WIRETAP, "name": "x"}),
        ("batch.set_attributes", {"target": BG1_SELECTOR, "values": {"name": "y"}}),
        ("journal.revert", {"group": first["journal"]["group"]}),
    ]
    for op, args in writes:
        env = rig.do(op, args)
        assert env["errorCode"] == "POLICY_DENIED", op


def test_a_write_queued_behind_the_one_that_went_unknown_is_refused_too(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "TIMEOUT")
    set_blend(rig)
    op = rig.registry.get("node.set_attributes")
    assert op is not None
    env = rig.runtime.execute(op, {"target": COMP, "values": {"pos_x": 1.0}})  # past the first gate
    assert env["errorCode"] == "POLICY_DENIED"


def test_check_lifts_the_refusal_whatever_it_finds(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "BRIDGE_LOST", when="after")  # landed, answer lost
    env = set_blend(rig)
    assert rig.handles["comp"].blend_type.get_value() == "Gimp"
    assert rig.do("node.set_attributes", {"target": COMP, "values": {"pos_x": 1.0}})[
        "errorCode"
    ] == ("POLICY_DENIED")
    found = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert found["ok"] is True
    assert found["result"]["checked"][0]["verdict"] == "landed"
    assert rig.runtime.guard.blocked() == []
    again = rig.do("node.set_attributes", {"target": COMP, "values": {"pos_x": 1.0}})
    assert again["ok"] is True, again


def test_check_finds_a_colour_label_write_landed_when_flame_stored_the_tables_spelling(
    rig: Rig,
) -> None:
    add_attr(rig.handles["comp"], "colour_label", ColourLabelAttr("Red"))
    rig.exec.fault("node.set_attributes", "BRIDGE_LOST", when="after")  # landed, answer lost
    env = rig.do("node.set_attributes", {"target": COMP, "values": {"colour_label": "green"}})
    assert rig.handles["comp"].colour_label.get_value() == "Green"
    found = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert found["ok"] is True, found
    assert found["result"]["checked"][0]["verdict"] == "landed"


def test_revert_after_a_check_puts_back_a_colour_label_stored_in_the_tables_spelling(
    rig: Rig,
) -> None:
    add_attr(rig.handles["comp"], "colour_label", ColourLabelAttr("Red"))
    rig.exec.fault("node.set_attributes", "BRIDGE_LOST", when="after")
    env = rig.do("node.set_attributes", {"target": COMP, "values": {"colour_label": "green"}})
    group = env["journal"]["group"]
    assert rig.do("journal.check", {"group": group})["ok"] is True
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert rig.handles["comp"].colour_label.get_value() == "Red"


def test_a_check_that_could_not_reach_flame_keeps_the_door_closed(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "TIMEOUT")
    env = set_blend(rig)
    rig.exec.fault("node.get_attributes", "BRIDGE_UNAVAILABLE")
    found = rig.do("journal.check", {"group": env["journal"]["group"]})
    assert found["ok"] is True
    assert found["result"]["checked"][0]["verdict"] == "unknown"
    assert found["result"]["stillUnknown"] == 1 and found["result"]["writesBlocked"] is True
    assert rig.runtime.guard.denial() is not None
    assert [r["type"] for r in rig.records()] == ["intent", "unresolved"]  # nothing learned


def test_a_new_process_starts_with_the_door_open_but_reports_the_open_intent(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig.exec.fault("node.set_attributes", "TIMEOUT")
    set_blend(rig)
    fresh = make_rig(monkeypatch, tmp_path)  # same journal directory: the "restarted" server
    assert fresh.runtime.guard.blocked() == []
    summary = fresh.runtime.journal_summary()
    assert summary["unresolvedIntents"] == 1 and summary["writesBlocked"] is False
    assert summary["dir"] == str(tmp_path / "journal")


def test_hooks_are_off_by_default() -> None:
    assert TestHooks().active() == []
    assert TestHooks.from_env({}).active() == []
    assert TestHooks.from_env({"FLAME_MCP_TEST_EXIT_AFTER_WRITE": "0"}).active() == []


def test_hooks_turn_on_from_the_environment_only() -> None:
    hooks = TestHooks.from_env(
        {
            "FLAME_MCP_TEST_PAUSE_AFTER_PREREAD": "3",
            "FLAME_MCP_TEST_EXIT_AFTER_WRITE": "1",
            "FLAME_MCP_TEST_WRITE_TIMEOUT_S": "0.001",
        }
    )
    assert hooks.pause_after_preread_s == 3.0 and hooks.exit_after_write is True
    assert hooks.write_timeout_s == 0.001
    assert len(hooks.active()) == 3


@pytest.mark.parametrize(
    "env",
    [
        {"FLAME_MCP_TEST_PAUSE_AFTER_PREREAD": "soon"},
        {"FLAME_MCP_TEST_PAUSE_AFTER_PREREAD": "-1"},
        {"FLAME_MCP_TEST_EXIT_AFTER_WRITE": "yes"},
        {"FLAME_MCP_TEST_WRITE_TIMEOUT_S": "0"},
    ],
)
def test_a_malformed_hook_setting_is_an_error_not_a_silent_default(env: dict[str, str]) -> None:
    with pytest.raises(TestHooksConfigError):
        TestHooks.from_env(env)


def test_without_hooks_the_write_is_sent_at_once_with_the_normal_timeout(rig: Rig) -> None:
    set_blend(rig)
    write_call = next(c for c in rig.exec.calls if c.label == "node.set_attributes")
    assert write_call.timeout_s == 10.0 and rig.exit_calls == []


def test_the_write_timeout_hook_applies_to_the_first_write_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, hooks=TestHooks(write_timeout_s=0.001))
    set_blend(rig, "Gimp")
    set_blend(rig, "Photoshop")
    write_timeouts = [c.timeout_s for c in rig.exec.calls if c.label == "node.set_attributes"]
    assert write_timeouts == [0.001, 10.0]


def test_the_write_timeout_hook_only_touches_the_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, hooks=TestHooks(write_timeout_s=0.001))
    set_blend(rig)
    timeouts = {c.label: c.timeout_s for c in rig.exec.calls}
    assert timeouts == {"node.get_attributes": 10.0, "node.set_attributes": 0.001}


def test_the_exit_hook_ends_the_process_after_the_write_and_before_the_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Exited(BaseException):
        pass

    def exit_now(code: int) -> None:
        raise Exited(code)

    rig = make_rig(monkeypatch, tmp_path, hooks=TestHooks(exit_after_write=True, exit_fn=exit_now))
    with pytest.raises(Exited):
        set_blend(rig)
    assert rig.handles["comp"].blend_type.get_value() == "Gimp"  # the write did happen
    assert [r["type"] for r in rig.records()] == ["intent"]  # and no outcome was written


def test_the_pause_hook_sleeps_after_the_intent_and_before_the_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[float, list[str]]] = []
    rig = make_rig(monkeypatch, tmp_path)

    def sleeper(seconds: float) -> None:
        seen.append((seconds, [str(r["type"]) for r in rig.records()]))

    rig.runtime.hooks = TestHooks(pause_after_preread_s=3.0, sleep=sleeper)
    set_blend(rig)
    assert seen == [(3.0, ["intent"])]  # after the intent is on disk ...
    assert rig.exec.labels() == ["node.get_attributes", "node.set_attributes"]  # ... write follows


def test_read_only_policy_refuses_the_write_before_anything_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = set_blend(rig)
    assert env["errorCode"] == "POLICY_DENIED" and rig.exec.calls == []


# The compare-and-swap inside Flame, run without the server around it.


def run_generated(rig: Rig, op: str, args: dict[str, Any]) -> dict[str, Any]:
    from op_test_helpers import run_op

    return run_op(rig.registry, op, args, rig.flame)


def test_generated_code_writes_nothing_when_a_current_value_differs_from_expected(rig: Rig) -> None:
    result = run_generated(
        rig,
        "node.set_attributes",
        {
            "target": COMP,
            "values": {"blend_type": "Gimp", "pos_x": 3.0},
            "_expected": {"blend_type": "Flame", "pos_x": 1.0},  # pos_x really is 0.0
        },
    )
    assert result["opErrorCode"] == "CONFLICT"
    assert result["value"]["conflicts"] == {"pos_x": {"expected": 1.0, "current": 0.0}}
    comp = rig.handles["comp"]
    assert comp.blend_type.set_calls == [] and comp.pos_x.set_calls == []


def test_generated_code_writes_when_every_expected_value_matches(rig: Rig) -> None:
    result = run_generated(
        rig,
        "node.set_attributes",
        {
            "target": COMP,
            "values": {"blend_type": "Gimp", "pos_x": 3.0},
            "_expected": {"blend_type": "Flame", "pos_x": 0.0},
        },
    )
    assert result["opError"] is None and result["value"]["applied"] == ["blend_type", "pos_x"]
    assert result["value"]["conflicts"] == {}


def test_generated_code_treats_a_missing_expected_key_as_a_conflict(rig: Rig) -> None:
    result = run_generated(
        rig,
        "node.set_attributes",
        {"target": COMP, "values": {"pos_x": 3.0}, "_expected": {}},
    )
    assert result["opErrorCode"] == "CONFLICT"
    assert rig.handles["comp"].pos_x.set_calls == []


def test_generated_code_without_expected_values_is_the_plain_write(rig: Rig) -> None:
    result = run_generated(rig, "node.set_attributes", {"target": COMP, "values": {"pos_x": 3.0}})
    assert result["opError"] is None and rig.handles["comp"].pos_x.get_value() == 3.0


def test_generated_rename_compares_the_name_before_writing(rig: Rig) -> None:
    reel = rig.handles["tree_reel"]
    result = run_generated(
        rig,
        "desktop.rename",
        {"target": REEL_WIRETAP, "name": "R1b", "_expected": {"name": "something else"}},
    )
    assert result["opErrorCode"] == "CONFLICT"
    assert reel.name.get_value() == "R1"
    assert result["value"]["conflicts"] == {"name": {"expected": "something else", "current": "R1"}}
    ok = run_generated(
        rig, "desktop.rename", {"target": REEL_WIRETAP, "name": "R1b", "_expected": {"name": "R1"}}
    )
    assert ok["opError"] is None and ok["verified"] is True and reel.name.get_value() == "R1b"


def test_the_write_is_sent_with_the_values_that_were_read(rig: Rig) -> None:
    set_blend(rig)
    write_call = next(c for c in rig.exec.calls if c.label == "node.set_attributes")
    assert "'blend_type': 'Flame'" in write_call.code  # the expected previous value, in the code
