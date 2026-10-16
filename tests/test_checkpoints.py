"""Tests for Batch checkpoints.

Covers batch.checkpoint (the real path save_setup wrote), the fingerprint, batch.load_setup (never
called on a missing file), journal.restore_checkpoint (file check, the pre-restore checkpoint, load
+ read-back), journal.list_checkpoints and the fail-closed / policy edges. Run through the real
Runtime with a scripted `transport.exec` over the fake Flame tree (journal_test_helpers.py); the
fake's save_setup / load_setup rewrite paths and empty the group on a failed load like the measured
Flame."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import restore as restore_mod
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import BG1, Rig, make_rig


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def names(rig: Rig) -> list[str]:
    return sorted(str(n.name.get_value()) for n in rig.handles["bg1"].nodes)


def checkpoint(rig: Rig, label: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"target": BG1}
    if label is not None:
        args["label"] = label
    env = rig.do("batch.checkpoint", args)
    assert env["ok"] is True, env
    result: dict[str, Any] = env["result"]
    return result


def add_wired_pair(rig: Rig) -> None:
    bg = rig.handles["bg1"]
    a, b = bg.create_node("Mux"), bg.create_node("Mux")
    bg.connect_nodes(a, "Default", b, "Default")


def fingerprint(rig: Rig, target: dict[str, Any] = BG1) -> str:
    env = rig.do("batch.fingerprint", {"target": target})
    assert env["ok"] is True, env
    value: str = env["result"]["fingerprint"]
    return value


def checkpoint_records(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r["type"] == "checkpoint"]


def test_checkpoint_records_the_path_save_setup_really_wrote(rig: Rig, tmp_path: Path) -> None:
    result = checkpoint(rig, "first")
    bg = rig.handles["bg1"]
    requested = bg.save_calls[0]
    path = result["path"]
    assert path != requested  # save_setup rewrote it: the recorded path is the globbed one
    assert path.endswith(".batch") and os.path.isfile(path)
    assert Path(path).parent == tmp_path / "journal" / "checkpoints"
    assert "." not in os.path.basename(requested)  # nothing for save_setup to turn into "_"
    assert result["written"] and path in result["written"]
    [record] = checkpoint_records(rig)
    assert record["id"] == result["checkpoint"] and record["path"] == path
    assert record["batch"] == {"uid": "uid-b1", "name": "BG1"}
    assert record["fingerprint"] == result["fingerprint"] == fingerprint(rig)
    assert record["nodeCount"] == 4 and record["label"] == "first"


def test_checkpoint_files_are_private_even_when_flame_runs_with_umask_zero(
    rig: Rig, tmp_path: Path
) -> None:
    previous = os.umask(0)
    try:
        result = checkpoint(rig, "private")
    finally:
        os.umask(previous)
    directory = tmp_path / "journal" / "checkpoints"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(result["path"]).stat().st_mode) == 0o600
    siblings = [Path(p) for p in result["written"] if Path(p).is_dir()]
    assert siblings
    for sibling in siblings:
        assert stat.S_IMODE(sibling.stat().st_mode) == 0o700


def test_checkpoint_leaves_the_permissions_of_an_existing_directory_alone(
    rig: Rig, tmp_path: Path
) -> None:
    directory = tmp_path / "journal" / "checkpoints"
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    checkpoint(rig)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_checkpoint_finds_the_file_even_when_save_setup_renames_it_unpredictably(rig: Rig) -> None:
    rig.handles["bg1"].save_stem_suffix = "_renamed"
    result = checkpoint(rig)
    assert result["path"].endswith("_renamed.batch") and os.path.isfile(result["path"])
    restored = rig.do("journal.restore_checkpoint", {"checkpoint": result["checkpoint"]})
    assert restored["ok"] is True


def test_checkpoint_changes_nothing_in_flame(rig: Rig) -> None:
    before = names(rig)
    checkpoint(rig)
    assert names(rig) == before and rig.handles["bg1"].load_calls == []


def test_checkpoint_of_a_non_current_group_by_uid(rig: Rig) -> None:
    env = rig.do("batch.checkpoint", {"target": {"kind": "batch", "uid": "uid-b2"}})
    assert env["ok"] is True and env["result"]["batch"]["name"] == "BG2"


def test_checkpoint_without_a_written_file_fails_and_records_nothing(rig: Rig) -> None:
    rig.handles["bg1"].save_writes_nothing = True
    env = rig.do("batch.checkpoint", {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "no single .batch file" in env["error"]
    assert checkpoint_records(rig) == []


def test_checkpoint_of_a_missing_group_is_selector_empty(rig: Rig) -> None:
    env = rig.do("batch.checkpoint", {"target": {"kind": "batch", "name": "nope"}})
    assert env["errorCode"] == "SELECTOR_EMPTY" and checkpoint_records(rig) == []


def test_checkpoint_when_the_journal_cannot_take_the_record(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(record: dict[str, Any]) -> None:
        raise OSError("read-only volume")

    monkeypatch.setattr(rig.journal, "append", refuse)
    env = rig.do("batch.checkpoint", {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert os.path.isfile(env["result"]["path"]) and "cannot be restored by id" in env["error"]


def test_unknown_reserved_args_are_rejected_from_a_caller(rig: Rig) -> None:
    env = rig.do("batch.checkpoint", {"target": BG1, "_dir": "/etc", "_base": "x"})
    assert env["errorCode"] == "INVALID_ARGS"


def test_fingerprint_is_stable_and_sees_new_nodes(rig: Rig) -> None:
    first = fingerprint(rig)
    assert fingerprint(rig) == first
    rig.handles["bg1"].create_node("Mux")
    assert fingerprint(rig) != first


def test_fingerprint_sees_a_connection_between_the_same_nodes(rig: Rig) -> None:
    bg = rig.handles["bg1"]
    a, b = bg.create_node("Mux"), bg.create_node("Mux")
    unwired = fingerprint(rig)
    bg.connect_nodes(a, "Default", b, "Default")
    assert fingerprint(rig) != unwired


def test_fingerprint_sees_which_input_socket_is_wired(rig: Rig) -> None:
    bg = rig.handles["bg1"]
    a, b = bg.create_node("Mux"), bg.create_node("Mux")
    bg.connect_nodes(a, "Default", b, "Input_0")
    on_input = fingerprint(rig)
    b._sockets["input"]["Input_0"].clear()
    bg.connect_nodes(a, "Default", b, "Matte_0")
    assert fingerprint(rig) != on_input


def test_fingerprint_sees_a_renamed_node_but_not_a_moved_one(rig: Rig) -> None:
    first = fingerprint(rig)
    rig.handles["comp"].pos_x.set_value(50.0)
    assert fingerprint(rig) == first  # documented: positions and values are not covered
    rig.handles["comp"].name.set_value("comp_renamed")
    assert fingerprint(rig) != first


def test_fingerprint_is_independent_of_node_order(rig: Rig) -> None:
    first = fingerprint(rig)
    rig.handles["bg1"].nodes.reverse()
    assert fingerprint(rig) == first


def test_fingerprint_counts_nodes_whose_sockets_cannot_be_read(rig: Rig) -> None:
    rig.handles["blur"].sockets_error = RuntimeError("no sockets")
    env = rig.do("batch.fingerprint", {"target": BG1, "include_nodes": True})
    assert env["ok"] is True
    assert env["result"]["unreadableNodes"] == 1 and env["result"]["nodeCount"] == 4
    blur = next(n for n in env["result"]["nodes"] if n["name"] == "blur1")
    assert blur["inputs"] is None
    assert env["result"]["stable"] == {"kind": "batch", "uid": "uid-b1"}


def test_fingerprint_include_nodes_lists_the_wiring(rig: Rig) -> None:
    add_wired_pair(rig)
    env = rig.do("batch.fingerprint", {"target": BG1, "include_nodes": True})
    assert env["result"]["connectionCount"] == 1
    wired = [n for n in env["result"]["nodes"] if ["Input_0", ["mux1"]] in (n["inputs"] or [])]
    assert [n["name"] for n in wired] == ["mux2"]


def test_load_setup_of_a_missing_file_never_calls_flames_load_setup(rig: Rig) -> None:
    before = names(rig)
    env = rig.do("batch.load_setup", {"target": BG1, "path": "/nonexistent/x.batch"})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    bg = rig.handles["bg1"]
    assert bg.load_calls == [] and names(rig) == before
    assert env["result"]["loadSetupCalled"] is False
    assert bg.save_calls == []  # no checkpoint for a change that will not happen
    assert env["journal"] is None and [r for r in rig.records() if r["type"] == "intent"] == []


def test_the_flame_side_file_check_holds_even_when_the_server_side_one_is_bypassed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _AlwaysThere:
        class path:
            @staticmethod
            def isfile(_: str) -> bool:
                return True

            @staticmethod
            def basename(p: str) -> str:
                return p

    monkeypatch.setattr(restore_mod, "os", _AlwaysThere)
    before = names(rig)
    env = rig.do("batch.load_setup", {"target": BG1, "path": "/nonexistent/x.batch"})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert rig.handles["bg1"].load_calls == [] and names(rig) == before  # Flame's own check


def test_load_setup_of_a_directory_is_not_a_setup_file(rig: Rig, tmp_path: Path) -> None:
    env = rig.do("batch.load_setup", {"target": BG1, "path": str(tmp_path)})
    assert env["errorCode"] == "PRECONDITION_UNMET" and rig.handles["bg1"].load_calls == []


def test_load_setup_replaces_the_contents_after_checkpointing_the_current_state(rig: Rig) -> None:
    saved = checkpoint(rig)
    rig.handles["bg1"].create_node("Mux")
    changed = fingerprint(rig)
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["ok"] is True, env
    assert env["result"]["load"]["returned"] is True and len(names(rig)) == 4
    pre = env["result"]["preCheckpoint"]
    assert pre["fingerprint"] == changed and os.path.isfile(pre["path"])
    bg = rig.handles["bg1"]
    assert len(bg.save_calls) == 2 and len(bg.load_calls) == 1  # checkpoint, pre-load, loaded
    load_call = next(c for c in rig.exec.calls if c.label == "batch.load_setup")
    assert (
        load_call.journal_types.count("checkpoint") == 2 and load_call.journal_types[-1] == "intent"
    )
    labels = [c.label for c in rig.exec.calls]
    assert labels.count("batch.load_setup") == 1  # not a second, restore-style, load
    assert env["execution"]["undo"] == "checkpoint"


def test_load_setup_is_journaled_with_fingerprints_and_revert_undoes_it(rig: Rig) -> None:
    saved = checkpoint(rig)
    rig.handles["bg1"].create_node("Mux")
    changed = fingerprint(rig)
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    group = env["journal"]["group"]
    [view] = Index(rig.records()).groups[group]
    assert view.intent["op"] == "batch.load_setup" and view.intent["path"] == saved["path"]
    assert view.intent["fingerprintBefore"] == changed
    assert view.intent["revertCheckpoint"] == env["result"]["preCheckpoint"]["id"]
    assert view.outcome is not None and view.outcome["fingerprintAfter"] == saved["fingerprint"]
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True and fingerprint(rig) == changed and len(names(rig)) == 5
    again = rig.do("journal.revert", {"group": group})
    assert again["ok"] is False and "already reverted" in again["error"]


def test_revert_of_a_load_setup_is_a_conflict_when_the_group_changed_since(rig: Rig) -> None:
    saved = checkpoint(rig)
    rig.handles["bg1"].create_node("Mux")
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    rig.handles["bg1"].create_node("Blur")
    refused = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert refused["errorCode"] == "CONFLICT" and len(rig.handles["bg1"].load_calls) == 1


def test_a_failed_load_setup_reports_the_checkpoint_that_restores_the_emptied_group(
    rig: Rig,
) -> None:
    saved = checkpoint(rig)
    rig.handles["bg1"].create_node("Mux")
    Path(saved["path"]).write_text("not json")  # exists; the fake's load raises on it
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    pre = env["result"]["preCheckpoint"]
    assert pre["id"] and os.path.isfile(pre["path"]) and pre["path"] in env["error"]
    assert "journal.restore_checkpoint" in str(env["hint"])
    back = rig.do("journal.restore_checkpoint", {"checkpoint": pre["id"]})
    assert back["ok"] is True and len(names(rig)) == 5


def test_a_failed_load_that_emptied_the_group_can_be_restored_from_the_reported_checkpoint(
    rig: Rig,
) -> None:
    saved = checkpoint(rig)
    bg = rig.handles["bg1"]
    bg.create_node("Mux")
    bg.load_return = False  # load_setup answers False: the group holds the loaded (old) setup
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    pre = env["result"]["preCheckpoint"]
    bg.load_return = True
    back = rig.do("journal.restore_checkpoint", {"checkpoint": pre["id"]})
    assert back["ok"] is True and len(names(rig)) == 5
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.state() == "failed"


def test_load_setup_with_no_possible_checkpoint_changes_nothing(rig: Rig) -> None:
    saved = checkpoint(rig)
    rig.handles["bg1"].save_writes_nothing = True
    rig.handles["bg1"].create_node("Mux")
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["ok"] is False and "nothing was replaced" in env["error"]
    assert rig.handles["bg1"].load_calls == [] and len(names(rig)) == 5


def test_unknown_end_of_a_load_setup_blocks_writes_until_check(rig: Rig) -> None:
    saved = checkpoint(rig)
    rig.handles["bg1"].create_node("Mux")
    rig.exec.fault("batch.load_setup", "TIMEOUT", when="after")
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["errorCode"] == "TIMEOUT" and env["result"]["preCheckpoint"]["path"] in env["error"]
    assert rig.runtime.guard.denial() is not None
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "diverged" and rig.runtime.guard.denial() is None


def test_load_setup_of_a_missing_group_touches_nothing(rig: Rig) -> None:
    saved = checkpoint(rig)
    env = rig.do(
        "batch.load_setup", {"target": {"kind": "batch", "name": "nope"}, "path": saved["path"]}
    )
    assert env["errorCode"] == "SELECTOR_EMPTY" and rig.handles["bg1"].load_calls == []


def test_plan_run_refuses_ops_that_own_their_journal_group(rig: Rig) -> None:
    saved = checkpoint(rig)
    for step in (
        {"op": "batch.load_setup", "args": {"target": BG1, "path": saved["path"]}},
        {"op": "journal.restore_checkpoint", "args": {"checkpoint": saved["checkpoint"]}},
    ):
        env = rig.do("plan.run", {"steps": [step]})
        assert env["errorCode"] == "INVALID_ARGS" and "cannot be a plan step" in str(env["result"])
    assert rig.handles["bg1"].load_calls == []


def test_load_setup_is_declared_as_a_checkpointed_destructive_op(rig: Rig) -> None:
    op = rig.registry.get("batch.load_setup")
    assert op is not None
    assert (op.effect, op.undo, op.thread) == ("destructive", "checkpoint", "main")
    assert op.wrap is not None


def test_restore_puts_the_group_back_and_verifies_the_fingerprint(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    assert fingerprint(rig) != saved["fingerprint"]
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True, env
    assert fingerprint(rig) == saved["fingerprint"] == env["result"]["fingerprintAfter"]
    assert env["result"]["matched"] is True and len(names(rig)) == 4
    assert env["journal"]["group"] == env["result"]["group"]


def test_restore_takes_a_checkpoint_of_the_current_state_before_loading(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    changed = fingerprint(rig)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    pre = env["result"]["preCheckpoint"]
    assert pre["fingerprint"] == changed and os.path.isfile(pre["path"])
    assert pre["id"] in {r["id"] for r in checkpoint_records(rig)}
    labels = [c.label for c in rig.exec.calls]
    assert labels.index("batch.load_setup") > max(
        i for i, label in enumerate(labels) if label == "batch.checkpoint"
    )
    bg = rig.handles["bg1"]
    assert len(bg.save_calls) == 2 and len(bg.load_calls) == 1  # saved, pre-restore, loaded
    load_call = next(c for c in rig.exec.calls if c.label == "batch.load_setup")
    assert load_call.journal_types.count("checkpoint") == 2  # both on record before the load
    assert load_call.journal_types[-1] == "intent"  # and the intent flushed right before it


def test_restore_can_itself_be_undone_by_restoring_the_pre_restore_checkpoint(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    changed = fingerprint(rig)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    undo = env["result"]["undo"]
    back = rig.do(undo["op"], undo["args"])
    assert back["ok"] is True and fingerprint(rig) == changed and len(names(rig)) == 6


def test_restore_of_a_missing_file_is_refused_without_calling_load_setup(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    os.remove(saved["path"])
    saves_before = list(rig.handles["bg1"].save_calls)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["fileExists"] is False and env["result"]["loadSetupCalled"] is False
    bg = rig.handles["bg1"]
    assert bg.load_calls == [] and len(names(rig)) == 6  # nothing changed
    assert bg.save_calls == saves_before  # not even the pre-restore checkpoint
    assert "batch.load_setup" not in [c.label for c in rig.exec.calls]


def test_restore_of_an_unknown_checkpoint_id(rig: Rig) -> None:
    env = rig.do("journal.restore_checkpoint", {"checkpoint": "c_nope"})
    assert env["errorCode"] == "INVALID_ARGS" and rig.handles["bg1"].load_calls == []


def test_restore_of_a_group_that_is_gone_touches_nothing(rig: Rig) -> None:
    saved = checkpoint(rig)
    rig.handles["bg1"].uid.set_value("uid-other")
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["errorCode"] == "SELECTOR_EMPTY" and rig.handles["bg1"].load_calls == []
    assert "nothing was replaced" in env["error"]


def test_restore_whose_result_is_not_the_saved_group_is_a_failure_with_the_pre_checkpoint(
    rig: Rig,
) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    # the file now holds something else than what was fingerprinted (a stale / tampered setup)
    setup = Path(saved["path"])
    payload = json.loads(setup.read_text())
    payload["nodes"] = payload["nodes"][:-1]
    setup.write_text(json.dumps(payload))
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["matched"] is False and env["result"]["load"]["returned"] is True
    assert env["result"]["preCheckpoint"]["path"] in env["error"]
    assert "journal.restore_checkpoint" in str(env["hint"])
    index = Index(rig.records())
    [view] = index.groups[env["journal"]["group"]]
    assert view.state() == "failed"


def test_restore_when_load_setup_fails_reports_the_pre_restore_checkpoint(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    Path(saved["path"]).write_text("not json")  # exists, but the fake's load raises on it
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    pre = env["result"]["preCheckpoint"]
    assert pre["id"] and os.path.isfile(pre["path"]) and pre["path"] in env["error"]
    # ... and the state before the failed restore is exactly what that checkpoint restores
    again = rig.do("journal.restore_checkpoint", {"checkpoint": pre["id"]})
    assert again["ok"] is True and len(names(rig)) == 6


def test_restore_records_an_intent_and_an_outcome_with_fingerprints(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    index = Index(rig.records())
    [view] = index.groups[env["journal"]["group"]]
    assert view.intent["op"] == "journal.restore_checkpoint"
    assert view.intent["fingerprintExpected"] == saved["fingerprint"]
    assert view.intent["revertCheckpoint"] == env["result"]["preCheckpoint"]["id"]
    assert view.outcome is not None and view.outcome["fingerprintAfter"] == saved["fingerprint"]
    assert view.state() == "landed"


def test_unknown_end_of_load_setup_blocks_writes_until_check(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    rig.exec.fault("batch.load_setup", "TIMEOUT", when="after")
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    assert env["result"]["preCheckpoint"]["path"] in env["error"]
    assert rig.runtime.guard.denial() is not None
    denied = rig.do("batch.checkpoint", {"target": BG1})
    assert denied["errorCode"] == "POLICY_DENIED"
    found = rig.do("journal.check")
    assert found["ok"] is True
    [entry] = found["result"]["checked"]
    assert entry["verdict"] == "landed"  # the load did run: the fingerprint is the checkpoint's
    assert rig.runtime.guard.denial() is None


def test_check_of_a_restore_that_did_not_run_is_not_landed(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    rig.exec.fault("batch.load_setup", "TIMEOUT", when="before")
    rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "not_landed"


def test_revert_of_a_restore_restores_the_pre_restore_checkpoint(rig: Rig) -> None:
    saved = checkpoint(rig)
    add_wired_pair(rig)
    changed = fingerprint(rig)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and fingerprint(rig) == changed
    again = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert again["ok"] is False and "already reverted" in again["error"]


def test_list_checkpoints_newest_first_with_file_existence(rig: Rig) -> None:
    first, second = checkpoint(rig, "one"), checkpoint(rig, "two")
    os.remove(first["path"])
    env = rig.do("journal.list_checkpoints")
    items = env["result"]["items"]
    assert [i["id"] for i in items] == [second["checkpoint"], first["checkpoint"]]
    assert [i["fileExists"] for i in items] == [True, False]
    assert env["result"]["total"] == 2 and items[0]["label"] == "two"
    paged = rig.do("journal.list_checkpoints", {"limit": 1, "offset": 1})["result"]
    assert paged["shown"] == 1 and paged["items"][0]["id"] == first["checkpoint"]


def test_show_of_a_restore_group_includes_its_pre_restore_checkpoint(rig: Rig) -> None:
    saved = checkpoint(rig)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    shown = rig.do("journal.show", {"group": env["journal"]["group"]})
    types = [r["type"] for r in shown["result"]["records"]]
    assert types == ["checkpoint", "intent", "outcome"]


def test_readonly_hides_checkpoint_load_and_restore_but_not_the_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    for op, args in (
        ("batch.checkpoint", {"target": BG1}),
        ("batch.load_setup", {"target": BG1, "path": "/x.batch"}),
        ("journal.restore_checkpoint", {"checkpoint": "c_x"}),
    ):
        assert ro.do(op, args)["errorCode"] == "POLICY_DENIED", op
    assert ro.do("batch.fingerprint", {"target": BG1})["ok"] is True
    assert ro.do("journal.list_checkpoints")["ok"] is True
    assert ro.handles["bg1"].save_calls == []


def test_a_restore_is_refused_up_front_when_a_needed_op_is_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    open_rig = make_rig(monkeypatch, tmp_path)
    saved = checkpoint(open_rig)
    open_rig.runtime.policy = Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,create"})
    env = open_rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["errorCode"] == "POLICY_DENIED"  # journal.restore_checkpoint is destructive
    open_rig.runtime.policy = Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": "journal"})
    env = open_rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["errorCode"] == "POLICY_DENIED" and "batch.checkpoint" in env["error"]
    assert open_rig.handles["bg1"].load_calls == [] and len(open_rig.handles["bg1"].save_calls) == 1


def _spec(**extra: Any) -> Any:
    from flame_mcp.types import OperationSpec

    return OperationSpec(
        name="batch.wrapped",
        category="batch",
        description="x",
        effect="read",
        to_python=lambda args: "_result = 1",
        **extra,
    )


def test_registry_refuses_a_wrap_on_a_server_op_and_a_wrap_beside_a_journal_spec() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import RegistrationError, Registry
    from flame_mcp.types import JournalSpec, OperationSpec

    registry = Registry()
    register_all(registry)
    with pytest.raises(RegistrationError, match="wrapped"):
        registry.register(
            OperationSpec(
                name="journal.wrapped",
                category="journal",
                description="x",
                effect="read",
                server_handler=lambda args, ctx: None,  # type: ignore[arg-type,return-value]
                wrap=lambda *a, **k: {},
            )
        )
    journal_spec = JournalSpec("batch.get_attributes", lambda a: [], lambda a: {}, lambda t, v: {})
    with pytest.raises(RegistrationError):
        registry.register(_spec(wrap=lambda *a, **k: {}, journal=journal_spec))


def test_the_checkpoint_ops_are_declared_as_the_decision_says() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    registry = Registry()
    register_all(registry)
    checkpoint_op = registry.get("batch.checkpoint")
    restore_op = registry.get("journal.restore_checkpoint")
    assert checkpoint_op is not None and restore_op is not None
    assert (checkpoint_op.effect, checkpoint_op.undo, checkpoint_op.thread) == (
        "create",
        "none",
        "main",
    )
    assert (restore_op.effect, restore_op.thread) == ("destructive", "server")
    assert restore_op.server_handler is not None
