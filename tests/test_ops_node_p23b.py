"""Tests for the `node` category's ops.

Covers `list_types`, `info`, and the writes `create`, `delete`, `connect`, `disconnect`,
`duplicate`, `save_setup`, `load_setup` (the ones that change the project run behind an automatic
checkpoint, `CheckpointSpec`).

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py). The fake's `load_setup` brings back nodes, wiring, settings and an
Action's inner nodes; what the real Flame brings back of reels / iterations is measured by
scripts/p2_3a_batch_e2e.py, and what these ops do to a real Flame by
scripts/p2_3b_node_action_e2e.py, so a test here describes the FAKE where it looks at a
restore."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import (
    CLIP_NODE_SETUP_SUFFIXES,
    FakeAttr,
    PyBatch,
    PyClipNode,
    PyNode,
)
from flame_mcp.checkpointed import run_declared
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from flame_mcp.registry import RegistrationError, Registry
from flame_mcp.types import CheckpointSpec, OperationParam, OperationSpec
from journal_test_helpers import BG1, Rig, make_rig

BG2 = {"kind": "batch", "name": "BG2"}
BLUR = {"kind": "node", "batch": BG1, "name": "blur1"}
COMP = {"kind": "node", "batch": BG1, "name": "comp1"}
EXPO = {"kind": "node", "batch": BG1, "name": "exposure1"}
ACTION = {"kind": "node", "batch": BG1, "name": "action1"}
NODE_WRITES = (
    "node.create",
    "node.delete",
    "node.connect",
    "node.disconnect",
    "node.duplicate",
    "node.load_setup",
)
CHECKPOINTED = {
    "node.create": "checkpoint",
    "node.delete": "checkpoint",
    "node.connect": "checkpoint",
    "node.disconnect": "checkpoint",
    "node.duplicate": "checkpoint",
    "node.load_setup": "checkpoint",
    "action.create_node": "checkpoint",
    "action.connect": "checkpoint",
}


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


def sent(rig: Rig) -> list[str | None]:
    return rig.exec.labels()


def left_nothing_behind(rig: Rig) -> bool:
    return rig.records() == [] and "batch.checkpoint" not in sent(rig)


def test_list_types_gives_the_whole_list_of_a_group_that_is_not_current(rig: Rig) -> None:
    env = rig.do("node.list_types", {"target": BG1})
    assert env["ok"] is True, env
    assert env["result"]["count"] == 7 and "Write File" in env["result"]["types"]
    assert env["result"]["types"] == list(bg(rig).node_types)
    assert env["execution"]["effect"] == "read" and env["journal"] is None


def test_info_of_a_plain_node_has_type_attributes_sockets_and_a_stable_selector(rig: Rig) -> None:
    env = rig.do("node.info", {"target": COMP})
    assert env["ok"] is True, env
    info = env["result"]
    assert (info["name"], info["pyType"], info["type"]) == ("comp1", "PyNode", "Comp")
    assert info["attributes"]["blend_type"] == {"value": "Flame"}
    assert "values" not in info["attributes"]["blend_type"]
    assert info["sockets"] == {
        "input": {"Input_0": [], "Matte_0": []},
        "output": {"Result": [], "OutMatte": []},
    }
    assert info["clip"] is None and info["render"] is None and info["innerNodeCount"] is None
    assert info["stable"] == {
        "kind": "node",
        "batch": {"kind": "batch", "uid": "uid-b1"},
        "name": "comp1",
    }
    assert info["unreadable"] == []


def test_info_with_include_values_adds_the_allowed_values(rig: Rig) -> None:
    env = rig.do("node.info", {"target": COMP, "include_values": True})
    assert env["result"]["attributes"]["blend_type"]["values"] == ["Flame", "Photoshop", "Gimp"]


def test_info_shows_the_wiring_of_both_ends(rig: Rig) -> None:
    bg(rig).connect_nodes(node(rig, "blur1"), "Default", node(rig, "comp1"), "Default")
    blur = rig.do("node.info", {"target": BLUR})["result"]["sockets"]
    comp = rig.do("node.info", {"target": COMP})["result"]["sockets"]
    assert blur["output"]["Result"] == ["comp1"] and comp["input"]["Input_0"] == ["blur1"]


def test_info_of_a_clip_node_describes_its_clip(rig: Rig) -> None:
    source = rig.flame.find_by_wiretap_node_id("/projects/p/ws/P1O/R1/clip1")
    bg(rig).create_reel("Schematic Reel 1")
    bg(rig).reels[0].receive([source])
    info = rig.do("node.info", {"target": {"kind": "node", "batch": BG1, "name": "clip1"}})[
        "result"
    ]
    assert info["type"] == "Clip" and info["pyType"] == "PyClipNode"
    assert info["clip"]["name"] == "clip1" and info["clip"]["pyType"] == "PyClip"
    assert info["clip"]["mediaPath"] == "/media/clip1.mov" and info["clip"]["duration"] == 24
    assert (
        info["clip"]["resolution"]["width"] == 1920 and info["clip"]["resolution"]["height"] == 1080
    )


def test_info_of_a_write_file_node_gives_the_resolved_path_and_range(rig: Rig) -> None:
    class PyWriteFileNode(PyNode):
        def get_resolved_media_path(self) -> str:
            return "/var/tmp/writefile1.[000001-000024].exr"

    writer: Any = PyWriteFileNode("writefile1", "Write File")
    writer.range_start = FakeAttr(1)
    writer.range_end = FakeAttr(24)
    bg(rig).nodes.append(writer)
    info = rig.do("node.info", {"target": {"kind": "node", "batch": BG1, "name": "writefile1"}})[
        "result"
    ]
    assert info["render"] == {
        "resolvedMediaPath": "/var/tmp/writefile1.[000001-000024].exr",
        "rangeStart": 1,
        "rangeEnd": 24,
    }


def test_info_of_an_action_node_counts_its_inner_nodes(rig: Rig) -> None:
    assert rig.do("node.info", {"target": ACTION})["result"]["innerNodeCount"] == 2


def test_info_reports_a_socket_read_that_fails_and_still_succeeds(rig: Rig) -> None:
    node(rig, "comp1").sockets_error = RuntimeError("no sockets")
    env = rig.do("node.info", {"target": COMP})
    assert env["ok"] is True and env["result"]["sockets"] is None
    assert any("sockets" in u for u in env["result"]["unreadable"])


def test_info_of_a_missing_node_is_selector_empty(rig: Rig) -> None:
    env = rig.do("node.info", {"target": {"kind": "node", "batch": BG1, "name": "nope"}})
    assert env["errorCode"] == "SELECTOR_EMPTY"


def test_every_write_declares_a_checkpoint_and_says_so(rig: Rig) -> None:
    for name, undo in CHECKPOINTED.items():
        op = rig.registry.get(name)
        assert op is not None and op.checkpoint is not None, name
        assert op.undo == undo, name
        assert "automatic checkpoint" in op.description, name
        assert "journal.revert" in op.description, name
        assert "Restore side effects" in op.description, name
        assert op.undo_steps({}, 1) == 0, name


def test_the_setup_saver_is_not_checkpointed_and_says_undo_none(rig: Rig) -> None:
    op = rig.registry.get("node.save_setup")
    assert op is not None and op.checkpoint is None and op.undo == "none"


def test_a_checkpointed_node_op_runs_after_its_checkpoint_and_intent_on_the_group_by_uid(
    rig: Rig,
) -> None:
    before = fingerprint(rig)
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert env["ok"] is True, env
    call = next(c for c in rig.exec.calls if c.label == "node.create")
    assert call.journal_types == ["checkpoint", "intent"]  # both flushed before it ran
    assert "_resolve_batch('uid-b1', None)" in call.code
    [intent] = [r for r in rig.records() if r["type"] == "intent"]
    assert intent["op"] == "node.create" and intent["kind"] == "create"
    assert intent["fingerprintBefore"] == before
    assert intent["target"] == {"kind": "batch", "uid": "uid-b1"}
    assert env["journal"]["revert"] == {"op": "journal.revert", "args": {"group": intent["group"]}}
    assert env["execution"]["undo"] == "checkpoint" and env["undoSteps"] == 0


def test_a_node_selector_runs_against_the_checkpointed_group_addressed_by_uid(rig: Rig) -> None:
    rig.do("node.delete", {"target": BLUR})
    call = next(c for c in rig.exec.calls if c.label == "node.delete")
    assert "_resolve_batch('uid-b1', None)" in call.code
    assert "_resolve_batch(None, 'BG1')" not in call.code


def test_create_makes_a_node_of_the_type_and_reads_it_back(rig: Rig) -> None:
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["node"] == {"name": "blur1", "pyType": "PyNode", "type": "Blur"} or (
        result["node"]["type"] == "Blur" and result["node"]["name"].startswith("blur")
    )
    assert result["typeAsRequested"] is True and result["requestedName"] is None
    assert result["nodeCountAfter"] == result["nodeCountBefore"] + 1
    assert len(names(rig)) == 5 and result["createdAs"] in names(rig)
    assert env["affected"] == 1


def test_create_with_a_name_names_the_node(rig: Rig) -> None:
    env = rig.do("node.create", {"target": BG1, "type": "Write File", "name": "my_writer"})
    assert env["ok"] is True, env
    assert env["result"]["node"]["name"] == "my_writer" and "my_writer" in names(rig)
    assert env["result"]["node"]["type"] == "Write File"
    assert env["result"]["createdAs"].startswith("writefile")


def test_create_of_an_unknown_type_is_refused_before_anything_is_checkpointed(rig: Rig) -> None:
    env = rig.do("node.create", {"target": BG1, "type": "Blurr"})
    assert env["errorCode"] == "INVALID_ARGS" and "nothing was run" in env["error"]
    assert "Blurr" in env["error"] and env["result"]["ran"] is False
    assert env["result"]["similar"] == ["Blur"] or env["result"]["similar"] == []
    assert left_nothing_behind(rig) and "node.create" not in sent(rig)
    assert len(names(rig)) == 4


def test_create_matches_the_type_ignoring_case_and_passes_the_listed_spelling(rig: Rig) -> None:
    """The op settles the spelling itself and hands Flame the listed one.

    Measured on Flame 2025.2.4: Flame takes `create_node('Mux')` although node_types lists
    'MUX'."""
    asked: list[str] = []
    real = bg(rig).create_node

    def spy(node_type: str) -> PyNode:
        asked.append(node_type)
        return real(node_type)

    bg(rig).create_node = spy  # type: ignore[method-assign]
    for asked_type in ("Mux", "mux", "MUX"):
        env = rig.do("node.create", {"target": BG1, "type": asked_type})
        assert env["ok"] is True, (asked_type, env)
        assert env["result"]["requestedType"] == asked_type
        assert env["result"]["canonicalType"] == "MUX"
        assert env["result"]["node"]["type"] == "MUX" and env["result"]["typeAsCanonical"] is True
        assert env["result"]["typeAsRequested"] is (asked_type == "MUX")
    assert asked == ["MUX", "MUX", "MUX"]
    assert names(rig).count("mux1") == 1 and "mux2" in names(rig)


def test_create_refuses_a_type_that_matches_several_ignoring_case_but_takes_the_exact_one(
    rig: Rig,
) -> None:
    bg(rig).node_types = ["Blur", "BLUR", "Comp"]
    env = rig.do("node.create", {"target": BG1, "type": "bLuR"})
    assert env["errorCode"] == "INVALID_ARGS" and "matches 2 node types" in env["error"]
    assert env["result"]["similar"] == ["Blur", "BLUR"] and left_nothing_behind(rig)
    exact = rig.do("node.create", {"target": BG1, "type": "BLUR"})
    assert exact["ok"] is True and exact["result"]["canonicalType"] == "BLUR"


def test_the_flame_side_of_create_settles_the_spelling_too(rig: Rig) -> None:
    from op_test_helpers import run_op

    result = run_op(rig.registry, "node.create", {"target": BG1, "type": "mUx"}, rig.flame)
    assert result["opError"] is None
    assert result["value"]["canonicalType"] == "MUX" and result["value"]["requestedType"] == "mUx"
    nothing = run_op(rig.registry, "node.create", {"target": BG1, "type": "Nope"}, rig.flame)
    assert nothing["opErrorCode"] == "INVALID_ARGS" and "unknown node type" in nothing["opError"]


def test_create_offers_similar_types(rig: Rig) -> None:
    env = rig.do("node.create", {"target": BG1, "type": "write"})
    assert env["errorCode"] == "INVALID_ARGS" and env["result"]["similar"] == ["Write File"]


def test_create_with_a_name_the_group_already_has_is_refused_before_a_checkpoint(rig: Rig) -> None:
    env = rig.do("node.create", {"target": BG1, "type": "Blur", "name": "comp1"})
    assert env["errorCode"] == "INVALID_ARGS" and "comp1" in env["error"]
    assert left_nothing_behind(rig) and len(names(rig)) == 4


def test_create_allows_a_name_only_the_current_group_has_as_far_as_the_checks_go(rig: Rig) -> None:
    # BG2 (current) has blur1; BG1 does not have "unique1": a name BG1 lacks passes the pre-check
    env = rig.do("node.create", {"target": BG1, "type": "Blur", "name": "unique1"})
    assert env["ok"] is True and "unique1" in names(rig)


def test_create_whose_naming_flame_refuses_keeps_the_node_and_the_result(rig: Rig) -> None:
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
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "already being used" in env["error"] and "CURRENT" in env["error"]
    assert env["result"]["createdAs"] in names(rig) and "wanted" not in names(rig)
    group = env["journal"]["group"]
    assert rig.do("journal.revert", {"group": group})["ok"] is True
    assert len(names(rig)) == 4


def test_create_that_makes_nothing_is_a_silent_noop_with_a_journal_and_a_way_back(
    rig: Rig,
) -> None:
    bg(rig).create_node_makes_nothing = True
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert env["errorCode"] == "SILENT_NOOP" and "no new node appeared" in env["error"]
    assert env["journal"]["group"] and env["journal"]["checkpoint"] in env["hint"]
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.outcome is not None and view.outcome["ok"] is False


def test_journal_revert_of_a_create_removes_the_node_and_the_fingerprint_matches(rig: Rig) -> None:
    before = fingerprint(rig)
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert fingerprint(rig) != before and env["journal"]["changed"] is True
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert fingerprint(rig) == before and len(names(rig)) == 4
    assert back["result"]["matched"] is True


def test_revert_of_a_create_is_a_conflict_when_the_group_changed_afterwards(rig: Rig) -> None:
    group = rig.do("node.create", {"target": BG1, "type": "Blur"})["journal"]["group"]
    bg(rig).create_node("Mux")  # somebody else's work
    refused = rig.do("journal.revert", {"group": group})
    assert refused["errorCode"] == "CONFLICT"
    assert rig.do("journal.revert", {"group": group, "force": True})["ok"] is True


def test_create_in_a_group_that_is_not_there_runs_nothing(rig: Rig) -> None:
    env = rig.do("node.create", {"target": {"kind": "batch", "name": "nope"}, "type": "Blur"})
    assert env["errorCode"] == "SELECTOR_EMPTY" and "node.create" not in sent(rig)


def wire(
    rig: Rig, source: str, target: str, source_socket: str = "Default", sock: str = "Default"
) -> None:
    bg(rig).connect_nodes(node(rig, source), source_socket, node(rig, target), sock)


def test_delete_removes_the_node_and_its_links_and_reads_that_back(rig: Rig) -> None:
    wire(rig, "blur1", "comp1")
    env = rig.do("node.delete", {"target": BLUR})
    assert env["ok"] is True, env
    assert "blur1" not in names(rig) and env["result"]["deleted"] == "blur1"
    assert env["result"]["nodeCountBefore"] == 4 and env["result"]["nodeCountAfter"] == 3
    assert env["result"]["wasWiredTo"]["output"]["Result"] == ["comp1"]
    assert env["result"]["staleLinksInNeighbours"] == {}
    assert node(rig, "comp1").sockets["input"]["Input_0"] == []  # gone from the upstream side too
    assert env["execution"]["effect"] == "destructive"


def test_delete_of_a_node_that_is_not_there_runs_nothing(rig: Rig) -> None:
    env = rig.do("node.delete", {"target": {"kind": "node", "batch": BG1, "name": "nope"}})
    assert env["errorCode"] == "SELECTOR_EMPTY" and "nothing was run" in env["error"]
    assert left_nothing_behind(rig) and len(names(rig)) == 4


def test_delete_that_leaves_the_node_there_is_a_silent_noop(rig: Rig) -> None:
    node(rig, "blur1").delete_makes_nothing = True
    env = rig.do("node.delete", {"target": BLUR})
    assert env["errorCode"] == "SILENT_NOOP" and "still there" in env["error"]
    assert env["journal"]["group"]


def test_revert_of_a_delete_brings_the_node_and_its_wiring_back(rig: Rig) -> None:
    wire(rig, "blur1", "comp1")
    before = fingerprint(rig)
    env = rig.do("node.delete", {"target": BLUR})
    assert fingerprint(rig) != before
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and fingerprint(rig) == before
    assert "blur1" in names(rig) and node(rig, "comp1").sockets["input"]["Input_0"] == ["blur1"]


def test_connect_links_the_nodes_and_reads_both_ends_back(rig: Rig) -> None:
    env = rig.do("node.connect", {"source": BLUR, "target": COMP})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["linkedInputSockets"] == ["Input_0"] and result["linkedOutputSockets"] == [
        "Result"
    ]
    assert result["newLinks"] == ["Input_0"] and result["alreadyLinked"] is False
    assert result["sourceSocket"] == "Default" and result["targetSocket"] == "Default"
    assert node(rig, "comp1").sockets["input"]["Input_0"] == ["blur1"]
    assert env["execution"]["effect"] == "reversible" and env["journal"]["changed"] is True


def test_connect_uses_the_sockets_it_is_given(rig: Rig) -> None:
    env = rig.do(
        "node.connect",
        {"source": BLUR, "target": COMP, "source_socket": "OutMatte", "target_socket": "Matte_0"},
    )
    assert env["ok"] is True, env
    assert env["result"]["linkedInputSockets"] == ["Matte_0"]
    assert bg(rig).connect_calls == [("blur1", "OutMatte", "comp1", "Matte_0")]
    assert node(rig, "blur1").sockets["output"]["OutMatte"] == ["comp1"]


def test_connect_runs_both_nodes_against_the_checkpointed_group_by_uid(rig: Rig) -> None:
    rig.do("node.connect", {"source": BLUR, "target": COMP})
    call = next(c for c in rig.exec.calls if c.label == "node.connect")
    assert call.code.count("_resolve_batch('uid-b1', None)") == 2
    assert "_resolve_batch(None, 'BG1')" not in call.code


def test_connect_refuses_a_socket_the_node_does_not_have_before_a_checkpoint(rig: Rig) -> None:
    wrong_target = rig.do(
        "node.connect", {"source": BLUR, "target": COMP, "target_socket": "Front"}
    )
    assert (
        wrong_target["errorCode"] == "INVALID_ARGS"
        and "input socket 'Front'" in wrong_target["error"]
    )
    assert wrong_target["result"]["targetSockets"]["input"] == {"Input_0": [], "Matte_0": []}
    wrong_source = rig.do("node.connect", {"source": BLUR, "target": COMP, "source_socket": "Nope"})
    assert (
        wrong_source["errorCode"] == "INVALID_ARGS"
        and "output socket 'Nope'" in wrong_source["error"]
    )
    assert left_nothing_behind(rig) and bg(rig).connect_calls == []


def test_connect_refuses_nodes_of_different_groups_before_a_checkpoint(rig: Rig) -> None:
    other = {"kind": "node", "batch": BG2, "name": "blur1"}
    env = rig.do("node.connect", {"source": other, "target": COMP})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "different Batch groups" in env["error"]
    assert env["result"]["sourceGroup"] == "uid-b2" and env["result"]["targetGroup"] == "uid-b1"
    assert left_nothing_behind(rig)


def test_connect_sees_two_selectors_of_one_group_as_one_group(rig: Rig) -> None:
    by_uid = {"kind": "node", "batch": {"kind": "batch", "uid": "uid-b1"}, "name": "blur1"}
    assert rig.do("node.connect", {"source": by_uid, "target": COMP})["ok"] is True


def test_connect_refuses_a_node_joined_to_itself(rig: Rig) -> None:
    env = rig.do("node.connect", {"source": BLUR, "target": BLUR})
    assert (
        env["errorCode"] == "INVALID_ARGS" and "itself" in env["error"] and left_nothing_behind(rig)
    )


def test_connect_refuses_a_node_that_is_not_there(rig: Rig) -> None:
    missing = {"kind": "node", "batch": BG1, "name": "nope"}
    env = rig.do("node.connect", {"source": BLUR, "target": missing})
    assert env["errorCode"] == "SELECTOR_EMPTY" and "the target node" in env["error"]
    assert left_nothing_behind(rig)


def test_connect_that_leaves_no_link_is_a_silent_noop_whatever_the_return_says(rig: Rig) -> None:
    bg(rig).connect_makes_nothing = True  # connect_nodes answers True and does nothing
    env = rig.do("node.connect", {"source": BLUR, "target": COMP})
    assert env["errorCode"] == "SILENT_NOOP" and "shows no link" in env["error"]
    assert env["result"]["connectReturned"] is True
    assert env["journal"]["group"] and node(rig, "comp1").sockets["input"]["Input_0"] == []


def test_connect_of_an_existing_link_is_reported_as_already_linked(rig: Rig) -> None:
    wire(rig, "blur1", "comp1")
    env = rig.do("node.connect", {"source": BLUR, "target": COMP})
    assert env["result"]["alreadyLinked"] is True and env["result"]["newLinks"] == []


def test_revert_of_a_connect_removes_the_link(rig: Rig) -> None:
    before = fingerprint(rig)
    env = rig.do("node.connect", {"source": BLUR, "target": COMP})
    assert fingerprint(rig) != before
    assert rig.do("journal.revert", {"group": env["journal"]["group"]})["ok"] is True
    assert fingerprint(rig) == before
    assert node(rig, "comp1").sockets["input"]["Input_0"] == []


def test_disconnect_with_a_socket_cuts_that_socket_only(rig: Rig) -> None:
    wire(rig, "blur1", "comp1", sock="Input_0")
    wire(rig, "exposure1", "comp1", sock="Matte_0")
    env = rig.do("node.disconnect", {"target": COMP, "socket": "Input_0"})
    assert env["ok"] is True, env
    assert (
        env["result"]["disconnected"] == {"Input_0": ["blur1"]}
        and env["result"]["hadLinks"] is True
    )
    assert node(rig, "comp1").sockets["input"] == {"Input_0": [], "Matte_0": ["exposure1"]}
    assert node(rig, "blur1").sockets["output"]["Result"] == []
    assert bg(rig).disconnect_calls == [("comp1", "Input_0")]


def test_disconnect_without_a_socket_cuts_every_input(rig: Rig) -> None:
    wire(rig, "blur1", "comp1", sock="Input_0")
    wire(rig, "exposure1", "comp1", sock="Matte_0")
    env = rig.do("node.disconnect", {"target": COMP})
    assert env["ok"] is True and env["result"]["scope"] == ["Input_0", "Matte_0"]
    assert env["result"]["disconnected"] == {"Input_0": ["blur1"], "Matte_0": ["exposure1"]}
    assert node(rig, "comp1").sockets["input"] == {"Input_0": [], "Matte_0": []}
    assert bg(rig).disconnect_calls == [("comp1", None)]


def test_disconnect_refuses_a_socket_the_node_does_not_have_before_a_checkpoint(rig: Rig) -> None:
    env = rig.do("node.disconnect", {"target": COMP, "socket": "Front"})
    assert env["errorCode"] == "INVALID_ARGS" and "'Front'" in env["error"]
    assert left_nothing_behind(rig)


def test_disconnect_of_nothing_says_there_was_nothing_to_cut(rig: Rig) -> None:
    env = rig.do("node.disconnect", {"target": COMP})
    assert (
        env["ok"] is True
        and env["result"]["hadLinks"] is False
        and env["result"]["disconnected"] == {}
    )
    assert env["journal"]["changed"] is False


def test_disconnect_that_leaves_the_link_is_a_silent_noop(rig: Rig) -> None:
    wire(rig, "blur1", "comp1")
    bg(rig).disconnect_makes_nothing = True
    env = rig.do("node.disconnect", {"target": COMP})
    assert env["errorCode"] == "SILENT_NOOP" and "still has links" in env["error"]


def test_revert_of_a_disconnect_puts_the_links_back(rig: Rig) -> None:
    wire(rig, "blur1", "comp1")
    before = fingerprint(rig)
    env = rig.do("node.disconnect", {"target": COMP})
    assert fingerprint(rig) != before
    assert rig.do("journal.revert", {"group": env["journal"]["group"]})["ok"] is True
    assert fingerprint(rig) == before and node(rig, "comp1").sockets["input"]["Input_0"] == [
        "blur1"
    ]


def test_duplicate_in_a_group_that_is_not_current_is_refused_before_anything_is_written(
    rig: Rig,
) -> None:
    env = rig.do("node.duplicate", {"target": BLUR})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "not the current one" in env["error"]
    assert "Nodes need to be in the current batch" in env["error"]
    assert env["result"]["ran"] is False
    assert env["result"]["group"] == {"name": "BG1", "uid": "uid-b1"}
    assert left_nothing_behind(rig) and "node.duplicate" not in sent(rig)
    assert len(names(rig)) == 4 and bg(rig).nodes[0].name.get_value() == "blur1"


def test_duplicate_in_the_current_group_makes_a_copy_and_reads_it_back(rig: Rig) -> None:
    target = {"kind": "node", "batch": BG2, "name": "blur1"}
    env = rig.do("node.duplicate", {"target": target})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["node"] == {
        "name": "blur2",
        "pyType": "PyNode",
        "type": "Blur",
        "pos": [0, 0],
        "sameNameCount": 1,
    }
    assert result["addedNames"] == ["blur2"] and result["source"] == "blur1"
    assert names(rig, "bg2") == ["blur1", "blur2"] and result["keepConnections"] is False
    assert env["execution"]["effect"] == "create" and env["journal"]["changed"] is True


def test_duplicate_can_keep_the_connections(rig: Rig) -> None:
    b2 = bg(rig, "bg2")
    mux = b2.create_node("Mux")
    b2.connect_nodes(node(rig, "blur1", "bg2"), "Default", mux, "Default")
    target = {"kind": "node", "batch": BG2, "name": "mux1"}
    env = rig.do("node.duplicate", {"target": target, "keep_connections": True})
    assert env["ok"] is True and env["result"]["keepConnections"] is True
    assert env["result"]["sockets"]["input"]["Input_0"] == ["blur1"]


def test_revert_of_a_duplicate_removes_the_copy(rig: Rig) -> None:
    before = fingerprint(rig, BG2)
    target = {"kind": "node", "batch": BG2, "name": "blur1"}
    env = rig.do("node.duplicate", {"target": target})
    assert fingerprint(rig, BG2) != before
    assert rig.do("journal.revert", {"group": env["journal"]["group"]})["ok"] is True
    assert fingerprint(rig, BG2) == before and names(rig, "bg2") == ["blur1"]


def test_duplicate_that_makes_no_copy_is_reported_in_the_flame_side_check_too(rig: Rig) -> None:
    bg(rig, "bg2").duplicate_makes_nothing = True
    target = {"kind": "node", "batch": BG2, "name": "blur1"}
    env = rig.do("node.duplicate", {"target": target})
    assert env["errorCode"] == "SILENT_NOOP" and "0 new node" in env["error"]


def test_duplicate_flame_side_says_precondition_unmet_when_flame_itself_refuses(
    rig: Rig,
) -> None:
    """The up-front check is the first line of defence; if the group turns out not to be current
    inside Flame after all (it changed in between), Flame's own refusal is translated."""
    from op_test_helpers import run_op

    result = run_op(rig.registry, "node.duplicate", {"target": BLUR}, rig.flame)
    assert result["opErrorCode"] == "PRECONDITION_UNMET"
    assert "Nodes need to be in the current batch" in result["opError"]
    assert len(names(rig)) == 4


def test_save_setup_reports_the_file_flame_really_wrote_with_its_type_extension(
    rig: Rig, tmp_path: Path
) -> None:
    target = tmp_path / "setups" / "expo"
    target.parent.mkdir()
    env = rig.do("node.save_setup", {"target": EXPO, "path": str(target)})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["requestedPath"] == str(target)
    assert result["path"] == f"{target}.exposure_node" and os.path.isfile(result["path"])
    assert result["extension"] == ".exposure_node" and result["size"] > 0
    assert result["written"] == [result["path"]] and result["overwritten"] == []
    assert result["node"]["type"] == "Exposure"
    assert env["journal"] is None and env["execution"]["undo"] == "none"
    assert "batch.checkpoint" not in sent(rig) and rig.records() == []  # not a project write


def test_save_setup_needs_an_absolute_path_and_an_existing_directory(
    rig: Rig, tmp_path: Path
) -> None:
    relative = rig.do("node.save_setup", {"target": EXPO, "path": "expo"})
    assert relative["errorCode"] == "INVALID_ARGS" and "absolute" in relative["error"]
    missing = rig.do("node.save_setup", {"target": EXPO, "path": str(tmp_path / "nope" / "expo")})
    assert missing["errorCode"] == "PRECONDITION_UNMET" and "does not exist" in missing["error"]
    assert node(rig, "exposure1").save_node_setup_writes_nothing is False
    assert not (tmp_path / "nope").exists()


def test_save_setup_refuses_to_overwrite_unless_told_to(rig: Rig, tmp_path: Path) -> None:
    target = str(tmp_path / "expo")
    first = rig.do("node.save_setup", {"target": EXPO, "path": target})
    again = rig.do("node.save_setup", {"target": EXPO, "path": target})
    assert again["errorCode"] == "PRECONDITION_UNMET" and "overwrite=true" in again["error"]
    forced = rig.do("node.save_setup", {"target": EXPO, "path": target, "overwrite": True})
    assert forced["ok"] is True and forced["result"]["path"] == first["result"]["path"]
    assert forced["result"]["overwritten"] == [first["result"]["path"]]


def test_save_setup_that_writes_nothing_is_an_error(rig: Rig, tmp_path: Path) -> None:
    node(rig, "exposure1").save_node_setup_writes_nothing = True
    env = rig.do("node.save_setup", {"target": EXPO, "path": str(tmp_path / "expo")})
    assert env["ok"] is False and "wrote no file" in env["error"]


CLIP = {"kind": "node", "batch": BG1, "name": "clip1"}
CLIP_SUFFIXES = CLIP_NODE_SETUP_SUFFIXES


def add_clip_node(rig: Rig) -> None:
    source = rig.flame.find_by_wiretap_node_id("/projects/p/ws/P1O/R1/clip1")
    bg(rig).create_reel("Schematic Reel 1")
    bg(rig).reels[0].receive([source])


def test_save_setup_of_a_node_that_writes_several_files_lists_them_all_and_names_the_main_one(
    rig: Rig, tmp_path: Path
) -> None:
    add_clip_node(rig)
    target = tmp_path / "clipnode"
    env = rig.do("node.save_setup", {"target": CLIP, "path": str(target)})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["path"] == f"{target}.clip_node" and result["extension"] == ".clip_node"
    assert result["files"] == [f"{target}{suffix}" for suffix in sorted(CLIP_SUFFIXES)]
    assert result["written"] == result["files"] and result["overwritten"] == []
    assert all(os.path.isfile(f) for f in result["files"])
    assert result["size"] == os.path.getsize(result["path"])


def test_the_main_file_of_a_several_file_save_loads_into_the_same_node(
    rig: Rig, tmp_path: Path
) -> None:
    add_clip_node(rig)
    saved = rig.do("node.save_setup", {"target": CLIP, "path": str(tmp_path / "clipnode")})
    assert rig.do("node.set_attributes", {"target": CLIP, "values": {"bypass": True}})["ok"]
    env = rig.do("node.load_setup", {"target": CLIP, "path": saved["result"]["path"]})
    assert env["ok"] is True, env
    assert env["result"]["changedKeys"] == ["bypass"]
    assert node(rig, "clip1").bypass.get_value() is False


def test_saving_again_over_several_files_needs_overwrite(rig: Rig, tmp_path: Path) -> None:
    add_clip_node(rig)
    target = str(tmp_path / "clipnode")
    assert rig.do("node.save_setup", {"target": CLIP, "path": target})["ok"] is True
    again = rig.do("node.save_setup", {"target": CLIP, "path": target})
    assert again["errorCode"] == "PRECONDITION_UNMET" and "overwrite=true" in again["error"]
    forced = rig.do("node.save_setup", {"target": CLIP, "path": target, "overwrite": True})
    assert forced["ok"] is True, forced
    assert forced["result"]["overwritten"] == forced["result"]["files"]


def test_several_files_with_none_of_the_nodes_own_extension_is_an_error_that_lists_them(
    rig: Rig, tmp_path: Path
) -> None:
    node(rig, "exposure1").save_node_setup_suffixes = (".part_a", ".part_b")
    env = rig.do("node.save_setup", {"target": EXPO, "path": str(tmp_path / "expo")})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "part_a" in env["error"] and "part_b" in env["error"]
    assert len(env["result"]["files"]) == 2 and env["result"]["path"] is None


def test_files_of_a_save_do_not_take_in_a_neighbour_whose_name_only_starts_the_same(
    rig: Rig, tmp_path: Path
) -> None:
    node(rig, "exposure1").save_node_setup_suffixes = (".exposure_node", ".exposure_aux")
    (tmp_path / "shot2.exr").write_text("x")
    original = PyNode.save_node_setup

    def save_and_drop(self: PyNode, path: str) -> bool:
        done = original(self, path)
        (tmp_path / "shot2.exr").write_text("changed while saving")
        (tmp_path / "shot_other.txt").write_text("new while saving")
        return done

    PyNode.save_node_setup = save_and_drop  # type: ignore[method-assign]
    try:
        env = rig.do("node.save_setup", {"target": EXPO, "path": str(tmp_path / "shot")})
    finally:
        PyNode.save_node_setup = original  # type: ignore[method-assign]
    assert env["ok"] is True, env
    names_ = sorted(os.path.basename(f) for f in env["result"]["files"])
    assert names_ == ["shot.exposure_aux", "shot.exposure_node"]
    assert "shot2.exr" not in "".join(names_)


def test_unrelated_new_files_are_not_taken_for_the_save_when_several_appeared(
    rig: Rig, tmp_path: Path
) -> None:
    node(rig, "exposure1").save_node_setup_writes_nothing = True
    original = PyNode.save_node_setup

    def write_strangers(self: PyNode, path: str) -> bool:
        done = original(self, path)
        (tmp_path / "other1.exr").write_text("x")
        (tmp_path / "other2.exr").write_text("x")
        return done

    PyNode.save_node_setup = write_strangers  # type: ignore[method-assign]
    try:
        env = rig.do("node.save_setup", {"target": EXPO, "path": str(tmp_path / "shot")})
    finally:
        PyNode.save_node_setup = original  # type: ignore[method-assign]
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["path"] is None


def test_save_setup_description_claims_only_what_was_measured_about_load() -> None:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    op = registry.get("node.save_setup")
    assert op is not None
    assert "loads back into the node it was saved from" not in op.description
    assert "note" in op.description


def saved_setup(rig: Rig, tmp_path: Path) -> str:
    env = rig.do("node.save_setup", {"target": EXPO, "path": str(tmp_path / "expo")})
    assert env["ok"] is True, env
    path: str = env["result"]["path"]
    return path


def exposure_red(rig: Rig) -> Any:
    return node(rig, "exposure1").exposure_red.get_value()


def test_load_setup_puts_the_settings_back_and_reports_the_difference(
    rig: Rig, tmp_path: Path
) -> None:
    path = saved_setup(rig, tmp_path)
    assert rig.do("node.set_attributes", {"target": EXPO, "values": {"exposure_red": 0.5}})["ok"]
    assert exposure_red(rig) == 0.5
    env = rig.do("node.load_setup", {"target": EXPO, "path": path})
    assert env["ok"] is True, env
    assert exposure_red(rig) == 0.0 and env["result"]["noChange"] is False
    assert env["result"]["changedKeys"] == ["exposure_red"]
    assert env["result"]["changed"]["exposure_red"] == {"before": 0.5, "after": 0.0}
    assert env["result"]["loadReturned"] is True
    assert env["execution"]["effect"] == "reversible" and env["journal"]["checkpoint"]


def test_load_setup_of_a_setup_the_node_already_matches_says_no_change(
    rig: Rig, tmp_path: Path
) -> None:
    path = saved_setup(rig, tmp_path)
    env = rig.do("node.load_setup", {"target": EXPO, "path": path})
    assert env["ok"] is True and env["result"]["noChange"] is True
    assert env["result"]["changed"] == {} and env["result"]["changedKeys"] == []


def test_load_setup_of_a_path_that_is_not_there_is_refused_on_this_side_first(
    rig: Rig, tmp_path: Path
) -> None:
    missing = str(tmp_path / "nothing.exposure_node")
    assert rig.do("node.set_attributes", {"target": EXPO, "values": {"exposure_red": 0.5}})["ok"]
    calls_before = len(rig.exec.calls)
    env = rig.do("node.load_setup", {"target": EXPO, "path": missing})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "no setup file" in env["error"]
    assert "would answer True for a missing path" in env["error"]
    assert len(rig.exec.calls) == calls_before  # not even a read went to Flame
    assert exposure_red(rig) == 0.5 and left_nothing_behind_except_the_set(rig)


def left_nothing_behind_except_the_set(rig: Rig) -> bool:
    return not any(r["type"] == "checkpoint" for r in rig.records())


def test_load_setup_needs_an_absolute_path(rig: Rig) -> None:
    env = rig.do("node.load_setup", {"target": EXPO, "path": "expo.exposure_node"})
    assert env["errorCode"] == "INVALID_ARGS" and "absolute" in env["error"]


def test_load_setup_checks_the_file_inside_flame_too_and_then_does_not_call_load(
    rig: Rig, tmp_path: Path
) -> None:
    """Checks the Flame side tests the setup file again before load_node_setup.

    The server-side check can be raced (the file may go between the two): the Flame side
    checks again, and calls load_node_setup only for a file that is there.
    """
    from op_test_helpers import run_op

    expo = node(rig, "exposure1")
    calls: list[str] = []

    def spy(path: str) -> bool:
        calls.append(path)
        return True

    expo.load_node_setup = spy  # type: ignore[method-assign]
    expo.exposure_red.set_value(0.5)
    missing = str(tmp_path / "gone.exposure_node")
    result = run_op(rig.registry, "node.load_setup", {"target": EXPO, "path": missing}, rig.flame)
    assert result["opErrorCode"] == "PRECONDITION_UNMET" and "was NOT called" in result["opError"]
    assert calls == [] and exposure_red(rig) == 0.5


def test_load_setup_of_a_node_that_is_not_there_runs_nothing(rig: Rig, tmp_path: Path) -> None:
    path = saved_setup(rig, tmp_path)
    missing = {"kind": "node", "batch": BG1, "name": "nope"}
    env = rig.do("node.load_setup", {"target": missing, "path": path})
    assert env["errorCode"] == "SELECTOR_EMPTY" and "checkpoint" not in [
        r["type"] for r in rig.records()
    ]


def test_revert_of_a_load_setup_restores_the_group_as_it_was_before_the_load(
    rig: Rig, tmp_path: Path
) -> None:
    path = saved_setup(rig, tmp_path)
    rig.do("node.set_attributes", {"target": EXPO, "values": {"exposure_red": 0.5}})
    before = fingerprint(rig)
    env = rig.do("node.load_setup", {"target": EXPO, "path": path})
    assert exposure_red(rig) == 0.0
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and fingerprint(rig) == before
    assert exposure_red(rig) == 0.5  # the checkpoint held the setting


def test_readonly_hides_every_new_write_but_not_the_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    args: dict[str, Any] = {
        "target": COMP,
        "source": BLUR,
        "type": "Blur",
        "path": "/x",
        "values": {"pos_x": 1},
    }
    for name in (*NODE_WRITES, "node.save_setup"):
        env = ro.do(name, {**args, "target": BG1} if name == "node.create" else args)
        assert env["errorCode"] == "POLICY_DENIED", name
    assert ro.do("node.list_types", {"target": BG1})["ok"] is True
    assert ro.do("node.info", {"target": COMP})["ok"] is True


def test_plan_run_refuses_a_step_that_takes_its_own_checkpoint(rig: Rig) -> None:
    for op_name, args in (
        ("node.create", {"target": BG1, "type": "Blur"}),
        ("node.delete", {"target": BLUR}),
        ("node.connect", {"source": BLUR, "target": COMP}),
        ("node.disconnect", {"target": COMP}),
        ("node.duplicate", {"target": BLUR}),
        ("node.load_setup", {"target": EXPO, "path": "/x"}),
    ):
        env = rig.do("plan.run", {"steps": [{"op": op_name, "args": args}]})
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", op_name
        assert "cannot be a plan step" in str(env["result"]["issues"]), op_name
    assert len(names(rig)) == 4 and sent(rig) == [] and rig.records() == []


def test_the_plan_run_description_names_every_refused_node_op(rig: Rig) -> None:
    op = rig.registry.get("plan.run")
    assert op is not None
    text = " ".join(op.description.split())
    for name in (*NODE_WRITES, "action.create_node", "action.connect"):
        assert name in text, name


def test_the_engine_needs_checkpoint_and_fingerprint_to_be_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    limited = make_rig(
        monkeypatch,
        tmp_path,
        policy=Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,create,reversible,destructive"}),
    )
    assert limited.do("node.delete", {"target": BLUR})["ok"] is True
    denied = make_rig(
        monkeypatch,
        tmp_path / "b",
        policy=Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,destructive"}),
    )
    env = denied.do("node.delete", {"target": BLUR})
    assert env["errorCode"] == "POLICY_DENIED" and "batch.checkpoint" in env["error"]
    assert "node.delete" not in [c.label for c in denied.exec.calls]


def test_a_write_queued_behind_an_unsettled_one_is_refused_inside_the_lock(rig: Rig) -> None:
    op = rig.registry.get("node.delete")
    assert op is not None
    rig.runtime.guard.block("i_x", "g_x", "node.delete", "TIMEOUT")
    env = run_declared(rig.runtime, op, {"target": BLUR})
    assert env["errorCode"] == "POLICY_DENIED" and "batch.checkpoint" not in sent(rig)


def _registry_with(spec_checkpoint: CheckpointSpec, accepts: tuple[Any, ...]) -> Registry:
    from flame_mcp.ops import register_all

    registry = Registry()
    register_all(registry)
    registry.register(
        OperationSpec(
            name="node.probe_write",
            category="node",
            description="probe",
            effect="reversible",
            undo="checkpoint",
            params=(
                OperationParam(name="target", type="selector", description="t", required=True),
            ),
            accepts=accepts,
            to_python=lambda args: "_result = 1",
            undo_steps=lambda args, affected: 0,
            checkpoint=spec_checkpoint,
        )
    )
    return registry


def test_a_group_selector_without_a_rebind_is_refused_at_registration() -> None:
    with pytest.raises(RegistrationError, match="go together"):
        _registry_with(CheckpointSpec(group_selector=lambda a: a["target"]["batch"]), ("node",))
    with pytest.raises(RegistrationError, match="go together"):
        _registry_with(CheckpointSpec(rebind=lambda a, s: dict(a)), ("node",))


def test_a_group_selector_needs_a_node_or_action_node_selector() -> None:
    both = CheckpointSpec(group_selector=lambda a: a["target"], rebind=lambda a, s: dict(a))
    with pytest.raises(RegistrationError, match="'node' or 'action_node'"):
        _registry_with(both, ("batch",))
    _registry_with(both, ("action_node",))


def test_without_a_group_selector_the_op_must_still_accept_a_batch() -> None:
    with pytest.raises(RegistrationError, match="must accept 'batch'"):
        _registry_with(CheckpointSpec(), ("node",))


def test_the_clip_node_helper_module_is_wired() -> None:
    assert PyClipNode("x").type.get_value() == "Clip"  # the fake the info tests rely on
