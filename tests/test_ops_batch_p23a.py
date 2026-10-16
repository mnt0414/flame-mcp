"""Tests for the checkpointed-write mechanism and the `batch` ops built on it.

The mechanism is `CheckpointSpec` in `checkpointed.py`; the ops are `info`, `create_reel`,
`create_shelf_reel`, `add_clips`, `organize` and `iterate`. Also covered: `save_setup` (a file, not
the project) and the fingerprint's parts for inner nodes of Action nodes and for the optional reel /
shelf-reel / iteration sections.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py). The fake's `load_setup` brings back NODES only; what the real Flame
brings back of reels / iterations is measured by scripts/p2_3a_batch_e2e.py, so a test here that
looks at a reel after a revert describes the FAKE, not Flame."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyActionNode, PyBatch, PyCoNode
from flame_mcp import checkpoints as checkpoints_mod
from flame_mcp.checkpointed import run_declared
from flame_mcp.journal import Index
from flame_mcp.ops.batch import MAX_CLIPS, UNDO_UNCONFIRMED
from flame_mcp.policy import Policy
from flame_mcp.registry import RegistrationError, Registry
from flame_mcp.types import CheckpointSpec, OperationParam, OperationSpec
from journal_test_helpers import BG1, Rig, make_rig
from op_test_helpers import run_op

CLIP1 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip1"}
CLIP2 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip2"}
CLIP3 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1/clip3"}
SEQ1 = {"kind": "wiretap", "id": "/projects/p/ws/P1O/Seq1"}
SCHEMATIC = "Schematic Reel 1"
SHELF = "Batch Renders"
CHECKPOINTED_OPS = (
    "batch.create_reel",
    "batch.create_shelf_reel",
    "batch.add_clips",
    "batch.organize",
    "batch.iterate",
)
SETTLED_UNDO = {
    "batch.create_reel": "checkpoint",
    "batch.create_shelf_reel": "unsupported",
    "batch.add_clips": "checkpoint",
    "batch.organize": "checkpoint",
    "batch.iterate": "unsupported",
}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_rig(monkeypatch, tmp_path)
    bg = made.handles["bg1"]
    bg.create_reel(SCHEMATIC)
    bg.create_shelf_reel(SHELF)
    return made


def bg1(rig: Rig) -> PyBatch:
    batch: PyBatch = rig.handles["bg1"]
    return batch


def node_names(rig: Rig) -> list[str]:
    return sorted(str(n.name.get_value()) for n in bg1(rig).nodes)


def fingerprint(rig: Rig, **extra: Any) -> str:
    env = rig.do("batch.fingerprint", {"target": BG1, **extra})
    assert env["ok"] is True, env
    value: str = env["result"]["fingerprint"]
    return value


def group_records(rig: Rig, group: str) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("group") == group]


def sent(rig: Rig) -> list[str | None]:
    return rig.exec.labels()


def test_info_lists_reels_shelf_reels_and_iterations_of_a_group_that_is_not_current(
    rig: Rig,
) -> None:
    bg = bg1(rig)
    bg.reels[0].receive([rig.flame.find_by_wiretap_node_id(CLIP1["id"])])
    bg.iterate()
    bg.iterate()
    env = rig.do("batch.info", {"target": BG1})
    assert env["ok"] is True, env
    info = env["result"]
    assert info["name"] == "BG1" and info["uid"] == "uid-b1" and info["isCurrent"] is False
    assert info["nodeCount"] == 5  # 4 + the Clip node the copy made
    assert info["duration"] == 100 and info["startFrame"] == 1
    [reel] = info["reels"]
    assert (reel["name"], reel["type"], reel["clipCount"], reel["clips"]) == (
        SCHEMATIC,
        "Schematic",
        1,
        ["clip1"],
    )
    [shelf] = info["shelfReels"]
    assert (shelf["name"], shelf["type"], shelf["clipCount"]) == (SHELF, "Shelf", 0)
    assert [(i["name"], i["number"]) for i in info["iterations"]] == [
        ("BG1_001", 1),
        ("BG1_002", 2),
    ]
    assert info["currentIterationNumber"] == 2 and info["unreadable"] == []
    assert env["journal"] is None and env["execution"]["effect"] == "read"


def test_info_of_the_current_group_says_so(rig: Rig) -> None:
    env = rig.do("batch.info", {"target": {"kind": "batch", "name": "BG2"}})
    assert env["result"]["isCurrent"] is True and env["result"]["reels"] == []


def test_info_reports_a_field_it_cannot_read_and_still_succeeds(rig: Rig) -> None:
    class Broken:
        def get_value(self) -> Any:
            raise RuntimeError("no duration")

    Broken.__name__ = "PyAttribute"
    bg1(rig).duration = Broken()  # type: ignore[assignment]
    env = rig.do("batch.info", {"target": BG1})
    assert env["ok"] is True and env["result"]["duration"] is None
    assert any("duration" in u for u in env["result"]["unreadable"])
    assert env["result"]["nodeCount"] == 4


def test_info_of_a_missing_group_is_selector_empty(rig: Rig) -> None:
    env = rig.do("batch.info", {"target": {"kind": "batch", "name": "nope"}})
    assert env["errorCode"] == "SELECTOR_EMPTY"


def test_info_shortens_a_long_clip_list_and_says_so(rig: Rig) -> None:
    source = rig.flame.find_by_wiretap_node_id(CLIP1["id"])
    shelf = bg1(rig).shelf_reels[0]
    shelf.receive([source] * 130)
    [row] = rig.do("batch.info", {"target": BG1})["result"]["shelfReels"]
    assert row["clipCount"] == 130 and len(row["clips"]) == 100 and row["clipsTruncated"] is True


# The checkpointed-write mechanism is shown through batch.add_clips: the fingerprint sees its
# clip nodes.


def add_clips(rig: Rig, clips: list[dict[str, Any]], reel: str = SCHEMATIC, **extra: Any) -> Any:
    return rig.do("batch.add_clips", {"target": BG1, "reel": reel, "clips": clips, **extra})


def test_a_checkpointed_op_checkpoints_first_then_journals_the_intent_then_runs(rig: Rig) -> None:
    before = fingerprint(rig)
    env = add_clips(rig, [CLIP1, CLIP2])
    assert env["ok"] is True, env
    labels = [x for x in sent(rig) if x in ("batch.info", "batch.checkpoint", "batch.add_clips")]
    assert labels[-2:] == ["batch.checkpoint", "batch.add_clips"]
    run_call = next(c for c in rig.exec.calls if c.label == "batch.add_clips")
    assert run_call.journal_types == ["checkpoint", "intent"]  # both flushed before it ran
    [checkpoint] = [r for r in rig.records() if r["type"] == "checkpoint"]
    [intent] = [r for r in rig.records() if r["type"] == "intent"]
    [outcome] = [r for r in rig.records() if r["type"] == "outcome"]
    assert intent["op"] == "batch.add_clips" and intent["kind"] == "add_clips"
    assert intent["checkpoint"] == checkpoint["id"] == intent["revertCheckpoint"]
    assert intent["fingerprintBefore"] == before == checkpoint["fingerprint"]
    assert intent["target"] == {"kind": "batch", "uid": "uid-b1"}
    assert intent["args"]["reel"] == SCHEMATIC and len(intent["args"]["clips"]) == 2
    assert checkpoint["group"] == intent["group"] == outcome["group"]
    assert outcome["ok"] is True and outcome["fingerprintAfter"] == fingerprint(rig) != before
    assert outcome["changed"] is True
    journal = env["journal"]
    assert journal["group"] == intent["group"] and journal["id"] == intent["id"]
    assert journal["checkpoint"] == checkpoint["id"] and journal["changed"] is True
    assert journal["checkpointPath"] == checkpoint["path"]
    assert journal["revert"] == {"op": "journal.revert", "args": {"group": intent["group"]}}
    assert env["execution"]["undo"] == "checkpoint" and env["undoSteps"] == 0


def test_the_op_runs_against_the_checkpointed_group_addressed_by_uid(rig: Rig) -> None:
    add_clips(rig, [CLIP1])
    call = next(c for c in rig.exec.calls if c.label == "batch.add_clips")
    assert "_resolve_batch('uid-b1', None)" in call.code
    assert "_resolve_batch(None, 'BG1')" not in call.code


def test_journal_revert_restores_the_checkpoint_while_the_fingerprint_is_the_one_left(
    rig: Rig,
) -> None:
    before = fingerprint(rig)
    env = add_clips(rig, [CLIP1, CLIP2])
    group = env["journal"]["group"]
    assert node_names(rig) == sorted(["action1", "blur1", "clip1", "clip2", "comp1", "exposure1"])
    back = rig.do("journal.revert", {"group": group})
    assert back["ok"] is True, back
    assert fingerprint(rig) == before and node_names(rig) == [
        "action1",
        "blur1",
        "comp1",
        "exposure1",
    ]
    assert back["result"]["matched"] is True and back["result"]["reverted"] == group
    assert Index(rig.records()).group_state(group) == "reverted"
    again = rig.do("journal.revert", {"group": group})
    assert again["ok"] is False and "already reverted" in again["error"]


def test_revert_is_a_conflict_when_the_group_changed_after_the_op_and_force_overrides(
    rig: Rig,
) -> None:
    before = fingerprint(rig)
    group = add_clips(rig, [CLIP1])["journal"]["group"]
    bg1(rig).create_node("Mux")  # somebody else's work
    loads = list(bg1(rig).load_calls)
    refused = rig.do("journal.revert", {"group": group})
    assert refused["errorCode"] == "CONFLICT" and refused["result"]["refused"] is True
    assert bg1(rig).load_calls == loads and "mux1" in node_names(rig)
    forced = rig.do("journal.revert", {"group": group, "force": True})
    assert forced["ok"] is True and fingerprint(rig) == before
    [restore] = Index(rig.records()).groups[forced["journal"]["group"]]
    assert restore.intent["forced"]["used"] is True and restore.intent["reverts"] == group


def test_no_checkpoint_no_run(rig: Rig) -> None:
    bg1(rig).save_writes_nothing = True
    env = add_clips(rig, [CLIP1])
    assert env["ok"] is False and "nothing was run" in env["error"]
    assert env["result"]["ran"] is False
    assert "batch.add_clips" not in sent(rig) and not any(
        r["type"] == "intent" for r in rig.records()
    )
    assert "clip1" not in node_names(rig)


def test_no_intent_no_run(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = rig.journal.append

    def refuse_intents(record: dict[str, Any]) -> None:
        if record["type"] == "intent":
            raise OSError("disk full")
        real(record)

    monkeypatch.setattr(rig.journal, "append", refuse_intents)
    env = add_clips(rig, [CLIP1])
    assert env["errorCode"] == "POLICY_DENIED" and "nothing was run" in env["error"]
    assert "batch.add_clips" not in sent(rig) and "clip1" not in node_names(rig)
    assert env["result"]["checkpoint"]  # the checkpoint exists; the caller is told which


def test_a_group_that_does_not_exist_runs_nothing(rig: Rig) -> None:
    env = rig.do("batch.organize", {"target": {"kind": "batch", "name": "nope"}})
    assert env["errorCode"] == "SELECTOR_EMPTY" and "batch.organize" not in sent(rig)


def test_an_op_that_fails_after_its_checkpoint_still_gets_an_outcome_and_a_revert(rig: Rig) -> None:
    before = fingerprint(rig)
    real_copy = rig.flame.media_panel.copy

    def copy_then_fail(source: Any, destination: Any) -> Any:
        real_copy(source, destination)
        raise RuntimeError("copy died half way")

    rig.flame.media_panel.copy = copy_then_fail
    env = add_clips(rig, [CLIP1, CLIP2])
    assert env["ok"] is False and "copy died half way" in env["error"]
    group = env["journal"]["group"]
    assert env["journal"]["checkpoint"] in env["hint"]  # says where the way back is
    [view] = Index(rig.records()).groups[group]
    assert view.outcome is not None and view.outcome["ok"] is False
    assert view.outcome["fingerprintAfter"] != before and view.state() == "failed"
    assert rig.do("journal.revert", {"group": group})["ok"] is True
    assert fingerprint(rig) == before


def test_an_unknown_end_blocks_writes_and_journal_check_reads_the_fingerprint(rig: Rig) -> None:
    rig.exec.fault("batch.add_clips", "TIMEOUT", when="after")  # it ran; the answer was lost
    env = add_clips(rig, [CLIP1])
    assert env["errorCode"] == "TIMEOUT" and "not known whether the operation ran" in env["hint"]
    assert env["journal"]["checkpoint"] in env["hint"]
    assert not [r for r in rig.records() if r["type"] == "outcome"]
    denied = rig.do("batch.organize", {"target": BG1})
    assert denied["errorCode"] == "POLICY_DENIED" and "journal.check" in str(denied["hint"])
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "diverged"  # changed, and no recorded result to match
    assert rig.runtime.guard.denial() is None
    group = env["journal"]["group"]
    assert rig.do("journal.revert", {"group": group})["errorCode"] == "CONFLICT"
    assert rig.do("journal.revert", {"group": group, "force": True})["ok"] is True
    assert "clip1" not in node_names(rig)


def test_an_unknown_end_of_a_call_that_never_ran_checks_as_not_landed(rig: Rig) -> None:
    rig.exec.fault("batch.add_clips", "BRIDGE_LOST", when="before")
    add_clips(rig, [CLIP1])
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "not_landed"


def test_a_write_queued_behind_an_unsettled_one_is_refused_inside_the_lock(rig: Rig) -> None:
    op = rig.registry.get("batch.organize")
    assert op is not None
    rig.runtime.guard.block("i_x", "g_x", "batch.add_clips", "TIMEOUT")
    env = run_declared(rig.runtime, op, {"target": BG1})  # past Runtime.execute's own check
    assert env["errorCode"] == "POLICY_DENIED" and "batch.checkpoint" not in sent(rig)
    assert env["error"].startswith("writes are refused")  # the guard's own answer, not a
    assert "checkpoint that makes" not in env["error"]  # checkpoint failure derived from it


def test_the_engine_needs_checkpoint_and_fingerprint_to_be_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    limited = make_rig(
        monkeypatch,
        tmp_path,
        policy=Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,reversible"}),
    )
    env = limited.do("batch.organize", {"target": BG1})
    assert env["errorCode"] == "POLICY_DENIED" and "batch.checkpoint" in env["error"]
    assert "batch.organize" not in [c.label for c in limited.exec.calls]


def test_readonly_hides_every_new_write_but_not_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    for name in (*CHECKPOINTED_OPS, "batch.save_setup"):
        args: dict[str, Any] = {"target": BG1, "name": "x", "reel": "r", "path": "/x", "clips": []}
        assert ro.do(name, args)["errorCode"] == "POLICY_DENIED", name
    assert ro.do("batch.info", {"target": BG1})["ok"] is True


def test_add_clips_to_a_schematic_reel_makes_clip_nodes(rig: Rig) -> None:
    env = add_clips(rig, [CLIP1, CLIP2, CLIP3])
    result = env["result"]
    assert result["clipNodesAdded"] == ["clip1", "clip2", "clip3"]
    assert result["clipNodesAsExpected"] is True and result["expectedClipNodes"] == 3
    assert (result["requested"], result["returned"]) == (3, 3)
    assert result["reelClipsBefore"] == [] and result["reelClipsAfter"] == [
        "clip1",
        "clip2",
        "clip3",
    ]
    assert result["nodesBefore"] == 4 and result["nodesAfter"] == 7
    assert env["affected"] == 1


def test_add_clips_passes_one_clip_as_itself_and_several_as_a_list(rig: Rig) -> None:
    add_clips(rig, [CLIP1])
    add_clips(rig, [CLIP2, CLIP3])
    single, several = rig.flame.media_panel.copy_calls
    assert not isinstance(single[0], list) and isinstance(several[0], list) and len(several[0]) == 2


def test_add_clips_says_whether_a_revert_covers_the_call(rig: Rig) -> None:
    schematic = add_clips(rig, [CLIP1])
    shelf = add_clips(rig, [CLIP2], reel=SHELF)
    assert schematic["result"]["revertCovers"] is True
    assert shelf["result"]["revertCovers"] is False
    op = rig.registry.get("batch.add_clips")
    assert op is not None
    assert "revertCovers" in op.description and "not undone" in op.description


def test_create_shelf_reel_is_declared_unsupported_and_create_reel_checkpoint(rig: Rig) -> None:
    shelf, reel = (rig.registry.get(n) for n in ("batch.create_shelf_reel", "batch.create_reel"))
    assert shelf is not None and reel is not None
    assert shelf.undo == "unsupported" and "does not remove the shelf reel" in shelf.description
    assert (
        reel.undo == "checkpoint" and "removes the new reel while it is still" in reel.description
    )


def test_add_clips_to_a_shelf_reel_makes_no_node(rig: Rig) -> None:
    before = fingerprint(rig)
    env = add_clips(rig, [CLIP1], reel=SHELF)
    assert env["ok"] is True and env["result"]["clipNodesAdded"] == []
    assert env["result"]["expectedClipNodes"] == 0 and env["result"]["clipNodesAsExpected"] is True
    assert env["result"]["reelClipsAfter"] == ["clip1"]
    assert fingerprint(rig) == before  # nothing the fingerprint can see
    assert env["journal"]["changed"] is False


def test_add_clips_takes_a_library_sequence_too(rig: Rig) -> None:
    env = add_clips(rig, [SEQ1])
    assert env["ok"] is True and env["result"]["clipNodesAdded"] == ["Seq1"]
    assert env["result"]["reelClipsAfter"] == ["Seq1"]  # counted from clips AND sequences
    [reel] = rig.do("batch.info", {"target": BG1})["result"]["reels"]
    assert (reel["clipCount"], reel["sequenceCount"], reel["sequences"]) == (0, 1, ["Seq1"])


def test_add_clips_refuses_something_that_is_not_a_clip_after_the_checkpoint(rig: Rig) -> None:
    reel_wiretap = {"kind": "wiretap", "id": "/projects/p/ws/P1O/R1"}
    env = add_clips(rig, [CLIP1, reel_wiretap])
    assert env["errorCode"] == "SELECTOR_TYPE" and "PyReel" in env["error"]
    assert rig.flame.media_panel.copy_calls == [] and node_names(rig).count("clip1") == 0
    assert env["journal"]["group"]  # it was checkpointed and journaled: the group is there


def test_add_clips_to_a_reel_that_is_not_there_leaves_nothing_behind(rig: Rig) -> None:
    env = add_clips(rig, [CLIP1], reel="No Such Reel")
    assert env["errorCode"] == "SELECTOR_EMPTY" and env["result"]["ran"] is False
    assert env["result"]["reels"] == [SCHEMATIC] and env["result"]["shelfReels"] == [SHELF]
    assert "batch.checkpoint" not in sent(rig) and rig.records() == []


def test_add_clips_reel_kind_picks_between_two_reels_with_one_name(rig: Rig) -> None:
    bg1(rig).create_shelf_reel(SCHEMATIC)  # the same name as the schematic reel
    ambiguous = add_clips(rig, [CLIP1])
    assert ambiguous["errorCode"] == "SELECTOR_AMBIGUOUS" and rig.records() == []
    shelf = add_clips(rig, [CLIP1], reel_kind="shelf")
    assert shelf["ok"] is True and shelf["result"]["reel"] == {"name": SCHEMATIC, "kind": "shelf"}
    assert shelf["result"]["clipNodesAdded"] == []
    only_shelf = add_clips(rig, [CLIP2], reel="Batch Renders", reel_kind="schematic")
    assert only_shelf["errorCode"] == "SELECTOR_EMPTY"


@pytest.mark.parametrize(
    "clips",
    [
        [],
        [{"kind": "batch", "name": "BG1"}],
        [{"kind": "wiretap"}],
        [CLIP1] * (MAX_CLIPS + 1),
        ["not a selector"],
    ],
)
def test_add_clips_refuses_bad_clip_lists_before_anything_is_sent(
    rig: Rig, clips: list[Any]
) -> None:
    env = add_clips(rig, clips)
    assert env["errorCode"] == "INVALID_ARGS"
    assert sent(rig) == [] and rig.records() == []


def test_add_clips_refuses_an_unknown_reel_kind(rig: Rig) -> None:
    env = add_clips(rig, [CLIP1], reel_kind="film")
    assert env["errorCode"] == "INVALID_ARGS" and sent(rig) == []


def test_add_clips_whose_copy_adds_nothing_is_silent_noop(rig: Rig) -> None:
    rig.flame.media_panel.copy = lambda source, destination: []
    env = add_clips(rig, [CLIP1, CLIP2])
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "gained 0 clip(s) / sequence(s), expected 2" in env["error"]
    assert env["result"]["reelClipsAfter"] == []  # the result is kept beside the error
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.state() == "failed"


def test_add_clips_reports_when_the_clip_nodes_are_not_what_was_expected(rig: Rig) -> None:
    bg1(rig).reels[0].owner = None  # a copy that puts the clips in the reel but makes no node
    env = add_clips(rig, [CLIP1])
    assert env["ok"] is True and env["result"]["clipNodesAsExpected"] is False


def test_the_clip_selectors_are_resolved_inside_flame_by_the_normal_selector_code(
    rig: Rig,
) -> None:
    add_clips(rig, [CLIP1, CLIP2])
    code = next(c for c in rig.exec.calls if c.label == "batch.add_clips").code
    assert "_clip0 = _resolve_wiretap(" in code and "_clip1 = _resolve_wiretap(" in code
    missing = add_clips(rig, [{"kind": "wiretap", "id": "/no/such/clip"}])
    assert missing["errorCode"] == "SELECTOR_EMPTY"


@pytest.mark.parametrize(
    ("op", "listing", "kind"),
    [
        ("batch.create_reel", "reels", "Schematic"),
        ("batch.create_shelf_reel", "shelf_reels", "Shelf"),
    ],
)
def test_create_reel_makes_the_reel_and_journals_it(
    rig: Rig, op: str, listing: str, kind: str
) -> None:
    env = rig.do(op, {"target": BG1, "name": "Mine"})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["reel"]["name"] == "Mine" and result["reel"]["type"] == kind
    assert result["renamed"] is False and "Mine" in result["reelsAfter"]
    assert "Mine" not in result["reelsBefore"]
    assert [r.name.get_value() for r in getattr(bg1(rig), listing)][-1] == "Mine"
    assert env["journal"]["changed"] is False  # reels are outside the fingerprint (documented)
    assert env["journal"]["checkpoint"]


def test_create_reel_that_returns_but_makes_nothing_is_silent_noop(rig: Rig) -> None:
    bg1(rig).create_reel_makes_nothing = True
    env = rig.do("batch.create_reel", {"target": BG1, "name": "Ghost"})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "no new reel appeared" in env["error"]


def test_create_reel_needs_a_name(rig: Rig) -> None:
    assert rig.do("batch.create_reel", {"target": BG1, "name": ""})["errorCode"] == "INVALID_ARGS"
    assert sent(rig) == []


def test_revert_of_a_create_reel_removes_the_empty_reel_it_made(rig: Rig) -> None:
    """Reverting a create_reel removes the empty reel it made.

    The checkpoint recorded the reels, so the tidy deletes the default reels the record lacks.
    The restore's load_setup drops the empty reel the call made and adds four default ones; the
    tidy then deletes the default reels the record does not have, and reports it."""
    before = fingerprint(rig)
    env = rig.do("batch.create_reel", {"target": BG1, "name": "Mine"})
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and fingerprint(rig) == before
    assert [r.name.get_value() for r in bg1(rig).reels] == [SCHEMATIC]
    assert back["result"]["reelsRestored"] is True
    deleted = [d["name"] for d in back["result"]["reelTidy"]["deleted"]]
    assert deleted == ["Schematic Reel 2", "Schematic Reel 3", "Schematic Reel 4"]


def test_organize_reports_which_nodes_moved(rig: Rig) -> None:
    env = rig.do("batch.organize", {"target": BG1})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["returned"] is True and result["nodeCount"] == 4
    assert result["moved"] == 3  # blur1 was at 0 and stays at 0; the other three move
    assert result["positionsBefore"]["comp1"] == [0.0, 0.0]
    assert result["positionsAfter"]["exposure1"] == [-576, 0]
    assert bg1(rig).organize_calls == 1 and env["journal"]["changed"] is False


def test_organize_that_does_not_return_true_is_an_error_with_the_result(rig: Rig) -> None:
    bg1(rig).organize_returns = False
    env = rig.do("batch.organize", {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "expected True" in env["error"] and env["result"]["returned"] is False


def test_organize_is_declared_reversible_with_a_checkpoint_undo(rig: Rig) -> None:
    op = rig.registry.get("batch.organize")
    assert op is not None and (op.effect, op.undo, op.thread) == (
        "reversible",
        "checkpoint",
        "main",
    )


def numbers(rig: Rig) -> list[int]:
    return [it.iteration_number for it in bg1(rig).batch_iterations]


def test_iterate_makes_the_next_iteration(rig: Rig) -> None:
    first = rig.do("batch.iterate", {"target": BG1})
    second = rig.do("batch.iterate", {"target": BG1})
    assert first["ok"] is True and second["ok"] is True, (first, second)
    assert (
        first["result"]["iteration"]["number"] == 1 and second["result"]["iteration"]["number"] == 2
    )
    assert first["result"]["iteration"]["name"] == "BG1_001"
    assert [i["number"] for i in second["result"]["iterationsBefore"]] == [1]
    assert [i["number"] for i in second["result"]["iterationsAfter"]] == [1, 2]
    assert second["result"]["overwrote"] is False
    assert numbers(rig) == [1, 2]


def test_iterate_with_a_free_index_makes_that_iteration(rig: Rig) -> None:
    env = rig.do("batch.iterate", {"target": BG1, "index": 5})
    assert env["ok"] is True and env["result"]["iteration"]["number"] == 5
    assert numbers(rig) == [5]


def test_iterate_over_an_existing_index_is_refused_before_anything_is_checkpointed(
    rig: Rig,
) -> None:
    bg1(rig).iterate()
    bg1(rig).iterate()
    old_uid = bg1(rig).batch_iterations[0].uid.get_value()
    env = rig.do("batch.iterate", {"target": BG1, "index": 1})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "overwrite=true" in env["error"] and env["result"]["existingIterations"] == [1, 2]
    assert bg1(rig).batch_iterations[0].uid.get_value() == old_uid and numbers(rig) == [1, 2]
    assert "batch.checkpoint" not in sent(rig) and "batch.iterate" not in sent(rig)
    assert rig.records() == [] and bg1(rig).save_calls == []
    explicit_false = rig.do("batch.iterate", {"target": BG1, "index": 2, "overwrite": False})
    assert explicit_false["errorCode"] == "INVALID_ARGS"


def test_iterate_over_an_existing_index_with_overwrite_replaces_it(rig: Rig) -> None:
    bg1(rig).iterate()
    bg1(rig).iterate()
    old_uid = bg1(rig).batch_iterations[0].uid.get_value()
    env = rig.do("batch.iterate", {"target": BG1, "index": 1, "overwrite": True})
    assert env["ok"] is True, env
    assert env["result"]["overwrote"] is True and env["result"]["iteration"]["number"] == 1
    assert bg1(rig).batch_iterations[0].uid.get_value() != old_uid and numbers(rig) == [1, 2]
    assert env["journal"]["checkpoint"]  # journaled like every checkpointed write


def test_iterate_index_must_be_a_whole_number_of_one_or_more(rig: Rig) -> None:
    for bad in (0, -1, 1.5):
        env = rig.do("batch.iterate", {"target": BG1, "index": bad})
        assert env["errorCode"] == "INVALID_ARGS", bad
    assert sent(rig) == []


def test_the_flame_side_of_iterate_refuses_an_overwrite_too(rig: Rig) -> None:
    """The second gate: the server-side check reads the iterations first, but a group can change
    in between; the generated code checks again."""
    bg1(rig).iterate()
    op = rig.registry.get("batch.iterate")
    assert op is not None
    raw = run_op(rig.registry, "batch.iterate", {"target": BG1, "index": 1}, rig.flame)
    assert raw["opErrorCode"] == "INVALID_ARGS" and "overwrite=true" in raw["opError"]
    assert numbers(rig) == [1]


def test_iterate_that_makes_no_iteration_is_silent_noop(rig: Rig) -> None:
    bg1(rig).iterate = lambda index=-1: None  # type: ignore[method-assign,assignment,return-value]
    env = rig.do("batch.iterate", {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"


def test_iterate_is_declared_unsupported_and_says_why(rig: Rig) -> None:
    op = rig.registry.get("batch.iterate")
    assert op is not None and (op.effect, op.undo) == ("create", "unsupported")
    assert "Undo: unsupported" in op.description


def test_revert_of_an_iterate_does_not_remove_the_iteration_in_the_fake(rig: Rig) -> None:
    """Describes the FAKE: a setup holds nodes only."""
    env = rig.do("batch.iterate", {"target": BG1})
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and numbers(rig) == [1]


def test_save_setup_writes_a_file_and_returns_its_real_path(rig: Rig, tmp_path: Path) -> None:
    target = tmp_path / "mysetup"
    env = rig.do("batch.save_setup", {"target": BG1, "path": str(target)})
    assert env["ok"] is True, env
    real = env["result"]["path"]
    assert real == str(tmp_path / "mysetup.batch") and os.path.isfile(real)
    assert env["result"]["requestedPath"] == str(target) and env["result"]["overwritten"] == []
    assert env["result"]["size"] == os.path.getsize(real) and env["result"]["written"] == [real]
    assert env["result"]["batch"] == {"uid": "uid-b1", "name": "BG1"}
    assert env["journal"] is None and env["execution"]["undo"] == "none"
    assert rig.records() == [] and env["undoSteps"] == 0  # a file, not the project: no journal


def test_save_setup_with_a_dot_in_the_name_reports_the_rewritten_path(
    rig: Rig, tmp_path: Path
) -> None:
    env = rig.do("batch.save_setup", {"target": BG1, "path": str(tmp_path / "a.b")})
    assert env["ok"] is True and env["result"]["path"] == str(tmp_path / "a_b.batch")


def test_save_setup_refuses_to_overwrite_unless_asked(rig: Rig, tmp_path: Path) -> None:
    path = str(tmp_path / "keep")
    assert rig.do("batch.save_setup", {"target": BG1, "path": path})["ok"] is True
    calls = len(bg1(rig).save_calls)
    refused = rig.do("batch.save_setup", {"target": BG1, "path": path})
    assert refused["errorCode"] == "PRECONDITION_UNMET" and "overwrite=true" in refused["error"]
    assert len(bg1(rig).save_calls) == calls
    bg1(rig).create_node("Mux")
    forced = rig.do("batch.save_setup", {"target": BG1, "path": path, "overwrite": True})
    assert forced["ok"] is True and forced["result"]["overwritten"] == [
        str(tmp_path / "keep.batch")
    ]


def test_save_setup_refuses_a_relative_path_and_a_missing_directory(
    rig: Rig, tmp_path: Path
) -> None:
    relative = rig.do("batch.save_setup", {"target": BG1, "path": "rel/setup"})
    missing = rig.do("batch.save_setup", {"target": BG1, "path": str(tmp_path / "no" / "x")})
    assert relative["errorCode"] == "INVALID_ARGS"
    assert missing["errorCode"] == "PRECONDITION_UNMET" and "does not exist" in missing["error"]
    assert bg1(rig).save_calls == []
    assert not (tmp_path / "no").exists()  # no directory is made for the caller


def test_save_setup_that_writes_nothing_is_an_error(rig: Rig, tmp_path: Path) -> None:
    bg1(rig).save_writes_nothing = True
    env = rig.do("batch.save_setup", {"target": BG1, "path": str(tmp_path / "ghost")})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "no single .batch file" in env["error"]


def test_save_setup_of_a_non_current_group_by_uid(rig: Rig, tmp_path: Path) -> None:
    env = rig.do(
        "batch.save_setup",
        {"target": {"kind": "batch", "uid": "uid-b1"}, "path": str(tmp_path / "byuid")},
    )
    assert env["ok"] is True


def _legacy_fingerprint(entries: list[list[Any]]) -> str:
    entries = sorted(entries, key=lambda e: json.dumps(e, sort_keys=True))
    blob = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def test_a_group_without_action_nodes_keeps_the_fingerprint_earlier_versions_computed(
    rig: Rig,
) -> None:
    group = bg1(rig)
    group.nodes = [n for n in group.nodes if not isinstance(n, PyActionNode)]
    expected = _legacy_fingerprint(
        [
            [str(n.name.get_value()), str(n.type.get_value()), [["Input_0", []], ["Matte_0", []]]]
            for n in group.nodes
        ]
    )
    assert fingerprint(rig) == expected


def test_the_fingerprint_sees_the_inner_nodes_of_an_action_node(rig: Rig) -> None:
    first = fingerprint(rig)
    rig.handles["action"].nodes.append(PyCoNode("light1"))
    added = fingerprint(rig)
    assert added != first
    del rig.handles["action"].nodes[-1]
    assert fingerprint(rig) == first
    rig.handles["action"].nodes[0].name.set_value("axis_renamed")
    assert fingerprint(rig) != first


def test_the_fingerprint_lists_inner_nodes_and_sorts_them(rig: Rig) -> None:
    env = rig.do("batch.fingerprint", {"target": BG1, "include_nodes": True})
    action = next(n for n in env["result"]["nodes"] if n["name"] == "action1")
    assert action["inner"] == [["Default", None], ["axis1", None]]
    blur = next(n for n in env["result"]["nodes"] if n["name"] == "blur1")
    assert "inner" not in blur
    rig.handles["action"].nodes.reverse()
    assert fingerprint(rig) == env["result"]["fingerprint"]


def test_an_unreadable_inner_node_list_is_counted(rig: Rig) -> None:
    class Broken(list[Any]):
        def __iter__(self) -> Any:
            raise RuntimeError("inner list unreadable")

    rig.handles["action"].nodes = Broken()
    env = rig.do("batch.fingerprint", {"target": BG1, "include_nodes": True})
    assert env["ok"] is True and env["result"]["unreadableNodes"] == 1


def test_a_checkpoint_restores_the_inner_nodes_it_saved(rig: Rig) -> None:
    saved = rig.do("batch.checkpoint", {"target": BG1})["result"]
    rig.handles["action"].nodes.append(PyCoNode("light1"))
    assert fingerprint(rig) != saved["fingerprint"]
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True and fingerprint(rig) == saved["fingerprint"]


def test_reels_shelf_reels_and_iterations_are_outside_the_default_fingerprint(rig: Rig) -> None:
    assert checkpoints_mod.DEFAULT_FINGERPRINT_SECTIONS == ()
    first = fingerprint(rig)
    bg1(rig).create_reel("Another")
    bg1(rig).create_shelf_reel("Another shelf")
    bg1(rig).iterate()
    assert fingerprint(rig) == first


def test_sections_make_the_fingerprint_see_reels_shelf_reels_and_iterations(rig: Rig) -> None:
    every = list(checkpoints_mod.FINGERPRINT_SECTIONS)
    plain, base = fingerprint(rig), fingerprint(rig, sections=every)
    assert base != plain  # a different blob, so a different hash
    bg1(rig).create_reel("Another")
    assert fingerprint(rig, sections=["reels"]) != fingerprint(rig, sections=[])
    assert fingerprint(rig, sections=every) != base
    after_reel = fingerprint(rig, sections=every)
    bg1(rig).create_shelf_reel("Another shelf")
    assert fingerprint(rig, sections=every) != after_reel
    after_shelf = fingerprint(rig, sections=every)
    bg1(rig).iterate()
    assert fingerprint(rig, sections=every) != after_shelf
    with_clip = fingerprint(rig, sections=["shelf_reels"])
    bg1(rig).shelf_reels[0].receive([rig.flame.find_by_wiretap_node_id(CLIP1["id"])])
    assert fingerprint(rig, sections=["shelf_reels"]) != with_clip  # a clip in a shelf reel
    with_seq = fingerprint(rig, sections=["shelf_reels"])
    bg1(rig).shelf_reels[0].receive([rig.flame.find_by_wiretap_node_id(SEQ1["id"])])
    assert fingerprint(rig, sections=["shelf_reels"]) != with_seq  # ... and a sequence
    assert fingerprint(rig, sections=[]) == plain  # asking for none is the plain fingerprint


def test_sections_are_reported_and_the_section_data_is_listed_on_request(rig: Rig) -> None:
    bg1(rig).iterate()
    env = rig.do(
        "batch.fingerprint",
        {"target": BG1, "include_nodes": True, "sections": ["iterations", "reels"]},
    )
    result = env["result"]
    assert result["sections"] == ["iterations", "reels"] and result["unreadableSections"] == []
    assert result["sectionData"]["iterations"] == [["BG1_001", 1]]
    assert result["sectionData"]["reels"] == [[SCHEMATIC, [], []]]


def test_an_unknown_section_is_refused(rig: Rig) -> None:
    env = rig.do("batch.fingerprint", {"target": BG1, "sections": ["nodes_and_more"]})
    assert env["errorCode"] == "INVALID_ARGS" and "unknown fingerprint section" in env["error"]
    assert sent(rig) == []


def test_the_default_sections_reach_the_checkpoint_and_its_record(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checkpoints_mod, "DEFAULT_FINGERPRINT_SECTIONS", ("reels",))
    from flame_mcp.ops import checkpoints as ops_checkpoints

    monkeypatch.setattr(ops_checkpoints, "DEFAULT_FINGERPRINT_SECTIONS", ("reels",))
    saved = rig.do("batch.checkpoint", {"target": BG1})["result"]
    [record] = [r for r in rig.records() if r["type"] == "checkpoint"]
    assert record["fingerprintSections"] == ["reels"] and saved["fingerprint"] == fingerprint(rig)
    bg1(rig).create_reel("Another")  # now visible
    assert fingerprint(rig) != saved["fingerprint"]


def test_the_checkpointed_ops_are_declared_as_the_decision_says(rig: Rig) -> None:
    for name in CHECKPOINTED_OPS:
        op = rig.registry.get(name)
        assert op is not None, name
        assert op.checkpoint is not None and op.wrap is None and op.journal is None, name
        assert op.undo in ("checkpoint", "unsupported") and op.thread == "main", name
        assert op.effect != "read" and op.accepts == ("batch",), name
        assert op.undo_steps({}, 1) == 0, name


def test_no_op_carries_the_placeholder_undo_marker_any_more(rig: Rig) -> None:
    marked = {op.name for op in rig.registry.list_ops() if UNDO_UNCONFIRMED in op.description}
    assert marked == set()


def test_the_undo_declarations_are_the_settled_ones(rig: Rig) -> None:
    declared = {name: rig.registry.get(name).undo for name in SETTLED_UNDO}  # type: ignore[union-attr]
    assert declared == SETTLED_UNDO


def test_every_restoring_op_describes_the_restore_side_effects(rig: Rig) -> None:
    restoring = [
        *CHECKPOINTED_OPS,
        "journal.revert",
        "journal.restore_checkpoint",
        "batch.load_setup",
        "eval.run",
    ]
    for name in restoring:
        op = rig.registry.get(name)
        assert op is not None, name
        text = " ".join(op.description.split())
        for phrase in (
            "adds Flame's default empty reels",
            "the recorded empty reels are created again",
            "the empty reels the record lacks are deleted",
            "is never deleted",
            "result.reelsRestored",
            "new uids",
            "no longer works",
            "shelf reels and iterations are not restored",
            "not tidied",
            "Force: a restore with force=true",
            "loses everything added to the group after the checkpoint",
            "nodes, reels and the clips in those reels",
            "does not create a reel the setup lacks",
        ):
            assert phrase in text, (name, phrase)
    for name in ("batch.info", "batch.save_setup", "batch.checkpoint", "batch.fingerprint"):
        op = rig.registry.get(name)
        assert op is not None and "Restore side effects" not in op.description, name


def test_save_setup_and_info_are_not_checkpointed(rig: Rig) -> None:
    save = rig.registry.get("batch.save_setup")
    info = rig.registry.get("batch.info")
    assert save is not None and info is not None
    assert save.checkpoint is None and (save.effect, save.undo) == ("create", "none")
    assert info.checkpoint is None and info.effect == "read"


def _spec(**kwargs: Any) -> OperationSpec:
    base: dict[str, Any] = {
        "name": "batch.probe_write",
        "category": "batch",
        "description": "x",
        "effect": "create",
        "undo": "checkpoint",
        "params": (OperationParam(name="target", type="selector", description="t", required=True),),
        "accepts": ("batch",),
        "to_python": lambda args: "_result = 1",
        "undo_steps": lambda args, affected: 0,
        "checkpoint": CheckpointSpec(),
    }
    base.update(kwargs)
    return OperationSpec(**base)


def _registry() -> Registry:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    return registry


def test_registering_a_checkpointed_op_needs_only_the_declaration() -> None:
    registry = _registry()
    op = registry.register(_spec())
    assert op.checkpoint == CheckpointSpec()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"effect": "read", "undo": None}, "never checkpointed"),
        ({"undo": "value"}, "undo='checkpoint' or 'unsupported'"),
        ({"undo": "none"}, "undo='checkpoint' or 'unsupported'"),
        ({"checkpoint": CheckpointSpec(batch_arg="nope")}, "must name a selector-typed"),
        ({"accepts": ("wiretap",)}, "must accept 'batch'"),
        ({"wrap": lambda *a, **k: {}}, "one of them"),
    ],
)
def test_registry_refuses_a_bad_checkpoint_declaration(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(RegistrationError, match=message):
        _registry().register(_spec(**changes))


def test_registry_refuses_a_checkpointed_server_op() -> None:
    registry = _registry()
    with pytest.raises(RegistrationError, match="run in Flame"):
        registry.register(
            OperationSpec(
                name="journal.probe",
                category="journal",
                description="x",
                effect="create",
                undo="checkpoint",
                params=(
                    OperationParam(name="target", type="selector", description="t", required=True),
                ),
                accepts=("batch",),
                server_handler=lambda args, ctx: None,  # type: ignore[arg-type,return-value]
                undo_steps=lambda args, affected: 0,
                checkpoint=CheckpointSpec(),
            )
        )


def test_the_generated_code_of_the_new_writes_has_no_loops(rig: Rig) -> None:
    """Registration already parses each op's to_python for loops (pacing 'none'); this pins that
    the Flame-side work lives in the prelude, not in the op's own source."""
    import ast

    for name in (*CHECKPOINTED_OPS, "batch.save_setup"):
        op = rig.registry.get(name)
        assert op is not None and op.to_python is not None
        tree = ast.parse(op.to_python(op.sample_args))
        assert not [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While, ast.ListComp))]


@pytest.mark.parametrize(
    "step",
    [
        {"op": "batch.create_reel", "args": {"target": BG1, "name": "x"}},
        {"op": "batch.create_shelf_reel", "args": {"target": BG1, "name": "x"}},
        {"op": "batch.add_clips", "args": {"target": BG1, "reel": SCHEMATIC, "clips": [CLIP1]}},
        {"op": "batch.organize", "args": {"target": BG1}},
        {"op": "batch.iterate", "args": {"target": BG1}},
    ],
)
def test_plan_run_refuses_ops_that_take_their_own_checkpoint_and_runs_nothing(
    rig: Rig, step: dict[str, Any]
) -> None:
    env = rig.do(
        "plan.run",
        {"steps": [{"op": "batch.info", "args": {"target": BG1}}, step]},
    )
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert env["result"]["issues"][0]["index"] == 1
    assert "cannot be a plan step" in str(env["result"]["issues"])
    assert sent(rig) == [] and rig.records() == []


def test_plan_run_still_runs_batch_info_and_save_setup(rig: Rig, tmp_path: Path) -> None:
    env = rig.do(
        "plan.run",
        {
            "steps": [
                {"op": "batch.info", "args": {"target": BG1}},
                {"op": "batch.save_setup", "args": {"target": BG1, "path": str(tmp_path / "p")}},
            ]
        },
    )
    assert env["ok"] is True, env
    assert [s["ok"] for s in env["result"]["steps"]] == [True, True]


def test_the_plan_run_description_names_the_refused_ops(rig: Rig) -> None:
    op = rig.registry.get("plan.run")
    assert op is not None
    text = " ".join(op.description.split())
    for name in CHECKPOINTED_OPS:
        assert name in text


def test_the_force_parameter_of_revert_says_what_a_forced_restore_loses(rig: Rig) -> None:
    op = rig.registry.get("journal.revert")
    assert op is not None
    force = next(p for p in op.params if p.name == "force")
    text = " ".join(force.description.split())
    assert "loses the nodes, reels and reel clips added to the group after the checkpoint" in text
    assert "does not create a reel the setup lacks" in text
