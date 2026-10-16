"""Tests for the `action` category.

Covers `list_nodes`, `create_node`, `connect` (checkpointed) and the generic `get_attributes` /
`set_attributes` on an `action_node` selector (value-journaled).

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree. The fake's
Action node follows what Flame 2025.2.4 does (`create_node('3D Path')` adds an `axis` too;
`connect_nodes` fills `parents` / `children`); what a real Flame answers is measured by
scripts/p2_3b_node_action_e2e.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr, PyActionNode, PyBatch, PyCoNode
from flame_mcp.journal import Index
from flame_mcp.node_prelude import ACTION_LINK_TYPES
from flame_mcp.types import SelectorKind
from journal_test_helpers import BG1, Rig, make_rig
from op_test_helpers import run_op

ACTION = {"kind": "node", "batch": BG1, "name": "action1"}
BLUR = {"kind": "node", "batch": BG1, "name": "blur1"}


def inner(name: str, action: dict[str, Any] = ACTION) -> dict[str, Any]:
    return {"kind": "action_node", "node": action, "name": name}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def action_node(rig: Rig, name: str = "action1") -> PyActionNode:
    found = next(n for n in rig.handles["bg1"].nodes if n.name.get_value() == name)
    assert isinstance(found, PyActionNode)
    return found


def inner_names(rig: Rig, name: str = "action1") -> list[str]:
    return [str(c.name.get_value()) for c in action_node(rig, name).nodes]


def fingerprint(rig: Rig) -> str:
    env = rig.do("batch.fingerprint", {"target": BG1})
    assert env["ok"] is True, env
    value: str = env["result"]["fingerprint"]
    return value


def sent(rig: Rig) -> list[str | None]:
    return rig.exec.labels()


def left_nothing_behind(rig: Rig) -> bool:
    return rig.records() == [] and "batch.checkpoint" not in sent(rig)


def second_action(rig: Rig) -> dict[str, Any]:
    batch: PyBatch = rig.handles["bg1"]
    made = batch.create_node("Action")
    return {"kind": "node", "batch": BG1, "name": str(made.name.get_value())}


def test_list_nodes_describes_the_inside_of_an_action_node(rig: Rig) -> None:
    env = rig.do("action.list_nodes", {"target": ACTION})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["node"]["name"] == "action1" and result["node"]["pyType"] == "PyActionNode"
    assert [r["name"] for r in result["nodes"]] == ["axis1", "Default"]
    assert all(r["pyType"] == "PyCoNode" for r in result["nodes"])
    assert "3D Path" in result["nodeTypes"] and result["outputTypes"] == [
        "Comp",
        "Matte",
        "Z-Depth",
    ]
    assert result["mediaLayers"] == [] and result["unreadable"] == []
    assert env["execution"]["effect"] == "read" and env["journal"] is None


def test_list_nodes_of_a_node_that_is_not_an_action_is_selector_type(rig: Rig) -> None:
    env = rig.do("action.list_nodes", {"target": BLUR})
    assert (
        env["errorCode"] == "SELECTOR_TYPE"
        and "not an Action / Image / GMask Tracer" in env["error"]
    )


def test_list_nodes_names_the_field_a_kind_of_node_does_not_have(rig: Rig) -> None:
    class PyImageNode(PyActionNode):
        pass

    image = PyImageNode("image1", [PyCoNode("Default")])
    image.output_types = None  # type: ignore[assignment]
    image._owner = rig.handles["bg1"]
    rig.handles["bg1"].nodes.append(image)
    env = rig.do("action.list_nodes", {"target": {"kind": "node", "batch": BG1, "name": "image1"}})
    assert env["ok"] is True and env["result"]["outputTypes"] is None
    assert any("output_types" in u for u in env["result"]["unreadable"])


def test_create_node_adds_the_node_and_reports_what_flame_added_on_its_own(rig: Rig) -> None:
    env = rig.do("action.create_node", {"target": ACTION, "type": "3D Path"})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["created"]["name"] == "path1" and result["created"]["type"] == "3D Path"
    assert [r["name"] for r in result["added"]] == ["axis1"[:4] + "2", "path1"] or {
        r["name"] for r in result["added"]
    } == {"axis2", "path1"}
    assert result["alsoAdded"] == ["axis2"]
    assert result["nodesBefore"] == ["axis1", "Default"]
    assert result["nodesAfter"] == ["axis1", "Default", "axis2", "path1"]
    assert inner_names(rig) == ["axis1", "Default", "axis2", "path1"]
    assert env["execution"]["effect"] == "create" and env["journal"]["changed"] is True


def test_create_node_of_a_type_the_action_does_not_have_is_refused_before_a_checkpoint(
    rig: Rig,
) -> None:
    env = rig.do("action.create_node", {"target": ACTION, "type": "Blur"})
    assert env["errorCode"] == "INVALID_ARGS" and "not a valid inner node type" in env["error"]
    assert env["result"]["ran"] is False
    assert left_nothing_behind(rig) and "action.create_node" not in sent(rig)
    assert inner_names(rig) == ["axis1", "Default"]


def test_create_node_matches_the_type_ignoring_case_and_passes_the_listed_spelling(
    rig: Rig,
) -> None:
    asked: list[str] = []
    node = action_node(rig)
    real = node.create_node

    def spy(node_type: str) -> PyCoNode:
        asked.append(node_type)
        return real(node_type)

    node.create_node = spy  # type: ignore[method-assign]
    env = rig.do("action.create_node", {"target": ACTION, "type": "3d path"})
    assert env["ok"] is True, env
    assert asked == ["3D Path"]  # the spelling the Action lists, not the one asked for
    assert env["result"]["requestedType"] == "3d path"
    assert env["result"]["canonicalType"] == "3D Path"
    assert env["result"]["created"]["name"] == "path1"


def test_create_node_refuses_a_type_that_matches_several_ignoring_case(rig: Rig) -> None:
    action_node(rig).node_types = ["3D Path", "3d path", "Axis"]
    env = rig.do("action.create_node", {"target": ACTION, "type": "3D PATH"})
    assert env["errorCode"] == "INVALID_ARGS" and "matches 2" in env["error"]
    assert env["result"]["similar"] == ["3D Path", "3d path"] and left_nothing_behind(rig)
    exact = rig.do("action.create_node", {"target": ACTION, "type": "3d path"})
    assert exact["ok"] is True and exact["result"]["canonicalType"] == "3d path"


def test_create_node_suggests_similar_types(rig: Rig) -> None:
    env = rig.do("action.create_node", {"target": ACTION, "type": "path"})
    assert env["result"]["similar"] == ["3D Path"]


def test_create_node_on_a_node_that_is_not_an_action_runs_nothing(rig: Rig) -> None:
    env = rig.do("action.create_node", {"target": BLUR, "type": "3D Path"})
    assert env["errorCode"] == "SELECTOR_TYPE" and left_nothing_behind(rig)


def test_create_node_that_adds_nothing_is_a_silent_noop_with_a_journal(rig: Rig) -> None:
    action_node(rig).create_makes_nothing = True
    env = rig.do("action.create_node", {"target": ACTION, "type": "3D Path"})
    assert env["errorCode"] == "SILENT_NOOP" and "gained no inner node" in env["error"]
    assert env["journal"]["group"] and env["journal"]["checkpoint"] in env["hint"]


def test_create_node_runs_on_the_checkpointed_group_by_uid(rig: Rig) -> None:
    rig.do("action.create_node", {"target": ACTION, "type": "Axis"})
    call = next(c for c in rig.exec.calls if c.label == "action.create_node")
    assert "_resolve_batch('uid-b1', None)" in call.code
    assert "_resolve_batch(None, 'BG1')" not in call.code


def test_revert_of_create_node_removes_the_inner_nodes_and_the_fingerprint_matches(
    rig: Rig,
) -> None:
    before = fingerprint(rig)
    env = rig.do("action.create_node", {"target": ACTION, "type": "3D Path"})
    assert fingerprint(rig) != before  # the fingerprint sees inner nodes
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert fingerprint(rig) == before and inner_names(rig) == ["axis1", "Default"]


def two_inner(rig: Rig) -> None:
    rig.do("action.create_node", {"target": ACTION, "type": "3D Path"})


def test_connect_links_two_inner_nodes_and_reads_the_link_back(rig: Rig) -> None:
    two_inner(rig)
    env = rig.do("action.connect", {"parent": inner("axis2"), "child": inner("path1")})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["linked"] is True and result["readBackAvailable"] is True
    assert (
        result["parent"] == "axis2"
        and result["child"] == "path1"
        and result["linkType"] == "Default"
    )
    assert result["linkedBy"] == [
        "child.parents('Default')",
        "child.parents()",
        "parent.children('Default')",
        "parent.children()",
    ]
    assert result["alreadyLinked"] is False and result["connectReturned"] is True
    assert [r["call"] for r in result["before"]["childParents"]] == [
        "parents('Default')",
        "parents()",
    ]
    assert all(r["ok"] and r["names"] == [] for r in result["before"]["childParents"])
    assert [r["names"] for r in result["after"]["childParents"]] == [["axis2"], ["axis2"]]
    assert [r["names"] for r in result["after"]["parentChildren"]] == [["path1"], ["path1"]]
    assert action_node(rig).connect_calls == [("axis2", "path1", "Default")]
    assert env["execution"]["effect"] == "reversible"


def test_connect_accepts_every_documented_link_type_in_any_case(rig: Rig) -> None:
    two_inner(rig)
    for link in ("Look At", "GMASK EXCLUSIVE", "light", "mimic"):
        env = rig.do(
            "action.connect",
            {"parent": inner("axis1"), "child": inner("Default"), "link_type": link},
        )
        assert env["ok"] is True, (link, env)
    assert [c[2] for c in action_node(rig).connect_calls] == [
        "Look At",
        "GMASK EXCLUSIVE",
        "light",
        "mimic",
    ]


def test_connect_refuses_a_link_type_that_is_not_documented_before_a_checkpoint(rig: Rig) -> None:
    env = rig.do(
        "action.connect",
        {"parent": inner("axis1"), "child": inner("Default"), "link_type": "hierarchy"},
    )
    assert env["errorCode"] == "INVALID_ARGS" and "link_type must be one of" in env["error"]
    assert left_nothing_behind(rig) and action_node(rig).connect_calls == []


def test_the_link_types_of_the_server_and_of_the_flame_side_agree() -> None:
    from flame_mcp.node_prelude import NODE_HELPER

    flat = " ".join(NODE_HELPER.split())
    for link in ACTION_LINK_TYPES:
        assert f"'{link}'" in flat, link
    assert len(ACTION_LINK_TYPES) == 7


def test_connect_refuses_inner_nodes_of_different_actions_before_a_checkpoint(rig: Rig) -> None:
    other = second_action(rig)
    env = rig.do("action.connect", {"parent": inner("axis1"), "child": inner("Default", other)})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "different Action" in env["error"]
    assert left_nothing_behind(rig)


def test_connect_refuses_an_inner_node_joined_to_itself(rig: Rig) -> None:
    env = rig.do("action.connect", {"parent": inner("axis1"), "child": inner("axis1")})
    assert (
        env["errorCode"] == "INVALID_ARGS" and "itself" in env["error"] and left_nothing_behind(rig)
    )


def test_connect_of_an_inner_node_that_is_not_there_runs_nothing(rig: Rig) -> None:
    env = rig.do("action.connect", {"parent": inner("axis1"), "child": inner("nope")})
    assert env["errorCode"] == "SELECTOR_EMPTY" and "the child inner node" in env["error"]
    assert left_nothing_behind(rig)


def test_connect_that_leaves_no_link_in_parents_or_children_is_a_silent_noop(rig: Rig) -> None:
    action_node(rig).connect_makes_nothing = True
    env = rig.do("action.connect", {"parent": inner("axis1"), "child": inner("Default")})
    assert env["errorCode"] == "SILENT_NOOP" and "none of the 4 link reads" in env["error"]
    assert env["result"]["linked"] is False and env["journal"]["group"]


def use_nodes(rig: Rig, cls: type[PyCoNode], *names: str) -> None:
    action_node(rig).nodes = [cls(n) for n in names]


def test_connect_claims_nothing_when_no_link_read_works_and_keeps_the_raw_errors(rig: Rig) -> None:
    class Unreadable(PyCoNode):
        def parents(self, link_type: str = "Default") -> list[PyCoNode]:
            raise TypeError("'builtin_function_or_method' object is not iterable")

        def children(self, link_type: str = "Default") -> list[PyCoNode]:
            raise TypeError("no children")

    use_nodes(rig, Unreadable, "u1", "u2")
    env = rig.do("action.connect", {"parent": inner("u1"), "child": inner("u2")})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["linked"] is None and result["readBackAvailable"] is False
    assert result["linkedBy"] == []
    rows = result["after"]["childParents"] + result["after"]["parentChildren"]
    assert [r["ok"] for r in rows] == [False] * 4
    assert rows[0]["error"] == "TypeError: 'builtin_function_or_method' object is not iterable"
    assert rows[2]["error"] == "TypeError: no children"


def test_connect_reads_the_link_by_calling_with_the_link_type_and_with_no_argument(
    rig: Rig,
) -> None:
    """The reads are CALLS (`parents(link_type)` / `children(link_type)`).

    Measured on Flame 2025.2.4: a read as an attribute is a TypeError. The calls are made with the
    link type passed to connect first, then with no argument. A no-argument call that Flame refuses
    is kept raw and does not stop the ones that work."""

    class NeedsALinkType(PyCoNode):
        def parents(self, link_type: str | None = None) -> list[PyCoNode]:
            if link_type is None:
                raise TypeError("parents() missing 1 required argument: 'link_type'")
            return super().parents(link_type)

        def children(self, link_type: str | None = None) -> list[PyCoNode]:
            if link_type is None:
                raise TypeError("children() missing 1 required argument: 'link_type'")
            return super().children(link_type)

    use_nodes(rig, NeedsALinkType, "u1", "u2")
    env = rig.do("action.connect", {"parent": inner("u1"), "child": inner("u2")})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["linked"] is True
    assert result["linkedBy"] == ["child.parents('Default')", "parent.children('Default')"]
    no_arg = result["after"]["childParents"][1]
    assert no_arg["call"] == "parents()" and no_arg["ok"] is False
    assert no_arg["error"] == "TypeError: parents() missing 1 required argument: 'link_type'"


def test_connect_with_another_link_type_reads_that_link_type_first(rig: Rig) -> None:
    env = rig.do(
        "action.connect",
        {"parent": inner("axis1"), "child": inner("Default"), "link_type": "Look At"},
    )
    assert env["ok"] is True, env
    result = env["result"]
    assert result["linkedBy"] == ["child.parents('Look At')", "parent.children('Look At')"]
    assert [r["names"] for r in result["after"]["childParents"]] == [
        ["axis1"],
        [],
    ]  # 'Default' sees none
    assert result["readBackAvailable"] is True and result["linked"] is True


def test_connect_runs_both_inner_nodes_against_the_checkpointed_group_by_uid(rig: Rig) -> None:
    rig.do("action.connect", {"parent": inner("axis1"), "child": inner("Default")})
    call = next(c for c in rig.exec.calls if c.label == "action.connect")
    assert call.code.count("_resolve_batch('uid-b1', None)") == 2
    assert "_resolve_batch(None, 'BG1')" not in call.code


def test_revert_of_a_connect_restores_the_group_and_says_the_fingerprint_could_not_tell(
    rig: Rig,
) -> None:
    before = fingerprint(rig)
    env = rig.do("action.connect", {"parent": inner("axis1"), "child": inner("Default")})
    assert env["journal"]["changed"] is False  # inner links are not in the fingerprint
    assert fingerprint(rig) == before
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True
    op = rig.registry.get("action.connect")
    assert op is not None and "`changed` reads false" in op.description


def give_position(rig: Rig, name: str = "axis1") -> PyCoNode:
    found = next(c for c in action_node(rig).nodes if c.name.get_value() == name)
    setattr(found, "pos_x", FakeAttr(10.0, (-100000.0, 100000.0)))  # noqa: B010
    found.attributes.append("pos_x")
    return found


def test_get_attributes_of_an_inner_node_returns_a_stable_action_node_selector(rig: Rig) -> None:
    give_position(rig)
    env = rig.do("action.get_attributes", {"target": inner("axis1"), "include_values": True})
    assert env["ok"] is True, env
    assert env["result"]["type"] == "PyCoNode"
    assert env["result"]["attributes"]["pos_x"] == {"value": 10.0, "values": [-100000.0, 100000.0]}
    assert env["result"]["stable"] == {
        "kind": "action_node",
        "node": {"kind": "node", "batch": {"kind": "batch", "uid": "uid-b1"}, "name": "action1"},
        "name": "axis1",
    }


def test_set_attributes_writes_journals_and_hands_back_a_revert(rig: Rig) -> None:
    give_position(rig)
    env = rig.do("action.set_attributes", {"target": inner("axis1"), "values": {"pos_x": 25.5}})
    assert env["ok"] is True, env
    assert env["result"]["before"] == {"pos_x": 10.0} and env["result"]["after"] == {"pos_x": 25.5}
    revert = env["result"]["revert"]
    assert revert["op"] == "action.set_attributes" and revert["args"]["values"] == {"pos_x": 10.0}
    assert revert["args"]["target"]["kind"] == "action_node"
    assert env["execution"]["undo"] == "value" and env["journal"]["group"]
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert action_node(rig).nodes[0].pos_x.get_value() == 10.0


def test_set_attributes_refuses_a_value_of_the_wrong_shape_before_writing(rig: Rig) -> None:
    give_position(rig)
    env = rig.do("action.set_attributes", {"target": inner("axis1"), "values": {"pos_x": "left"}})
    assert env["errorCode"] == "INVALID_ARGS"
    assert action_node(rig).nodes[0].pos_x.get_value() == 10.0


def test_set_attributes_refuses_a_name_another_inner_node_already_has(rig: Rig) -> None:
    env = rig.do("action.set_attributes", {"target": inner("axis1"), "values": {"name": "Default"}})
    assert env["errorCode"] == "INVALID_ARGS" and "already named" in env["error"]
    assert inner_names(rig) == ["axis1", "Default"]


def test_a_renamed_inner_node_is_found_again_by_the_journal_and_reverted(rig: Rig) -> None:
    env = rig.do(
        "action.set_attributes", {"target": inner("axis1"), "values": {"name": "axis_main"}}
    )
    assert env["ok"] is True and inner_names(rig) == ["axis_main", "Default"]
    [intent] = [r for r in rig.records() if r["type"] == "intent"]
    assert (
        intent["targetAfter"]["name"] == "axis_main"
        and intent["targetAfter"]["kind"] == "action_node"
    )
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True and inner_names(rig) == ["axis1", "Default"]


def test_set_attributes_of_an_inner_node_of_a_node_that_is_not_an_action_is_selector_type(
    rig: Rig,
) -> None:
    env = rig.do("action.get_attributes", {"target": inner("axis1", BLUR)})
    assert env["errorCode"] == "SELECTOR_TYPE"


def test_the_action_node_selector_is_taken_by_exactly_the_action_ops(rig: Rig) -> None:
    kinds: SelectorKind = "action_node"
    accepting = sorted(op.name for op in rig.registry.list_ops() if kinds in op.accepts)
    assert accepting == ["action.connect", "action.get_attributes", "action.set_attributes"]


def test_the_write_ops_of_the_category_are_in_the_journal_as_such(rig: Rig) -> None:
    rig.do("action.create_node", {"target": ACTION, "type": "Axis"})
    rig.do("action.connect", {"parent": inner("axis1"), "child": inner("Default")})
    ops = [r["op"] for r in rig.records() if r["type"] == "intent"]
    assert ops == ["action.create_node", "action.connect"]
    groups = Index(rig.records()).newest_first()
    assert len(groups) == 2


def test_the_flame_side_of_create_node_checks_the_type_too(rig: Rig) -> None:
    result = run_op(
        rig.registry, "action.create_node", {"target": ACTION, "type": "Blur"}, rig.flame
    )
    assert (
        result["opErrorCode"] == "INVALID_ARGS" and "unknown inner node type" in result["opError"]
    )
    assert inner_names(rig) == ["axis1", "Default"]


def test_the_flame_side_of_connect_checks_the_link_type_and_the_action_too(rig: Rig) -> None:
    bad = run_op(
        rig.registry,
        "action.connect",
        {"parent": inner("axis1"), "child": inner("Default"), "link_type": "nope"},
        rig.flame,
    )
    assert bad["opErrorCode"] == "INVALID_ARGS"
    other = second_action(rig)
    mixed = run_op(
        rig.registry,
        "action.connect",
        {"parent": inner("axis1"), "child": inner("Default", other)},
        rig.flame,
    )
    assert mixed["opErrorCode"] == "PRECONDITION_UNMET"
    assert action_node(rig).connect_calls == []
