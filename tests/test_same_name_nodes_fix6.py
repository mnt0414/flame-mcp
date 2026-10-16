"""Tests nodes of one Batch group that share a name.

A `node` selector takes an optional `pos: [x, y]`; an ambiguous selector ends in
SELECTOR_AMBIGUOUS (with candidates); node rows carry `pos` and
`sameNameCount`; `node.info` names the sockets' ambiguous partner names. Run against the fake
Flame (two `dup` nodes at different positions)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, get_args

import pytest

from fake_media_tree import FakeAttr, PyActionNode, PyBatch, PyCoNode, PyNode
from flame_mcp import errors, execution, selectors
from flame_mcp.node_prelude import NODE_HELPER
from flame_mcp.types import ErrorCode, SelectorKind
from journal_test_helpers import BG1, Rig, make_rig
from op_test_helpers import run_op
from test_ops_attributes import call, make_batch_world
from test_ops_batch_node import make_registry, resolve

DUP_A = (100, 100)
DUP_B = (400, 200)


def dup(pos: tuple[float, float], note: str = "", node_type: str = "Clip") -> PyNode:
    return PyNode("dup", node_type, extra={"note": FakeAttr(note)}, pos=pos)


def add_dups(batch: PyBatch, *nodes: PyNode) -> None:
    for node in nodes:
        node._owner = batch
        batch.nodes.append(node)


def sel(pos: tuple[float, float] | None = None, name: str = "dup") -> dict[str, Any]:
    out: dict[str, Any] = {"kind": "node", "batch": BG1, "name": name}
    if pos is not None:
        out["pos"] = list(pos)
    return out


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_rig(monkeypatch, tmp_path)
    add_dups(made.handles["bg1"], dup(DUP_A, "a"), dup(DUP_B, "b"))
    return made


def note_of(rig: Rig, pos: tuple[float, float]) -> str:
    for n in rig.handles["bg1"].nodes:
        if n.name.get_value() == "dup" and (n.pos_x.get_value(), n.pos_y.get_value()) == pos:
            return str(n.note.get_value())
    raise AssertionError(f"no dup at {pos}")


def test_node_selector_takes_an_optional_pos_of_two_integers() -> None:
    assert selectors.validate_selector(sel(DUP_A), get_args(SelectorKind)) == []
    assert selectors.validate_selector(sel(), get_args(SelectorKind)) == []


@pytest.mark.parametrize(
    "bad",
    [[1], [1, 2, 3], "1,2", [1.5, 2], [True, 2], [1, False], None, {"x": 1, "y": 2}, [-1.0, 2.5]],
)
def test_node_selector_rejects_a_pos_that_is_not_two_integers(bad: object) -> None:
    bad_sel = {**sel(), "pos": bad}
    issues = selectors.validate_selector(bad_sel, get_args(SelectorKind))
    assert any("pos" in i for i in issues), issues


def test_pos_is_accepted_on_a_node_selector_only() -> None:
    issues = selectors.validate_selector(
        {"kind": "batch", "name": "BG1", "pos": [1, 2]}, get_args(SelectorKind)
    )
    assert any("unexpected key" in i for i in issues)


def test_pos_is_an_optional_field_described_for_node() -> None:
    fields = selectors.describe_kind("node")["fields"]
    assert fields["pos"]["required"] is False
    assert fields["pos"]["type"] == "integer[2]"
    assert (
        "pos_x" in fields["pos"]["description"] and "SELECTOR_EMPTY" in fields["pos"]["description"]
    )


def test_resolve_code_passes_pos_on_and_is_unchanged_without_it() -> None:
    assert selectors.resolve_code(sel(DUP_A), "t").splitlines()[-1] == (
        "t = _resolve_node(t__batch, 'dup', [100, 100])"
    )
    assert selectors.resolve_code(sel(), "t").splitlines()[-1] == (
        "t = _resolve_node(t__batch, 'dup')"
    )


def dup_world() -> tuple[Any, dict[str, Any]]:
    flame, h = make_batch_world()
    add_dups(h["bg1"], dup(DUP_A, "a"), dup(DUP_B, "b", "Blur"))
    return flame, h


def test_a_name_that_matches_two_nodes_is_selector_ambiguous_with_the_candidates() -> None:
    flame, _h = dup_world()
    result = resolve(sel(), flame)
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"
    message = result["opError"]
    assert "2" in message and "'dup'" in message
    assert "type='Clip'" in message and "type='Blur'" in message
    assert "pos=[100, 100]" in message and "pos=[400, 200]" in message


def test_at_most_ten_candidates_are_listed() -> None:
    flame, h = make_batch_world()
    add_dups(h["bg1"], *[dup((i * 10, 0)) for i in range(12)])
    message = resolve(sel(), flame)["opError"]
    assert message.count("pos=[") == 10
    assert "12" in message and "2 more" in message


@pytest.mark.parametrize("pos,note", [(DUP_A, "a"), (DUP_B, "b")])
def test_pos_picks_the_node_at_that_position(pos: tuple[float, float], note: str) -> None:
    registry = make_registry()
    flame, _h = dup_world()
    got = call(registry, "node.get_attributes", {"target": sel(pos), "keys": ["note"]}, flame)
    assert got["opError"] is None
    assert got["value"]["attributes"]["note"] == {"value": note}


def test_a_float_position_with_the_same_value_matches() -> None:
    flame, h = make_batch_world()
    add_dups(h["bg1"], dup((100.0, 100.0), "a"), dup(DUP_B, "b"))
    assert resolve(sel(DUP_A), flame)["opError"] is None


def test_a_pos_that_matches_none_of_the_same_named_nodes_is_selector_empty() -> None:
    flame, _h = dup_world()
    result = resolve(sel((1, 1)), flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
    assert "[1, 1]" in result["opError"]
    assert "[100, 100]" in result["opError"] and "[400, 200]" in result["opError"]


def test_a_unique_name_with_a_wrong_pos_is_selector_empty_and_with_the_right_pos_resolves() -> None:
    flame, h = make_batch_world()
    blur = h["blur"]
    here = [blur.pos_x.get_value(), blur.pos_y.get_value()]
    right = {"kind": "node", "batch": BG1, "name": "blur1", "pos": here}
    wrong = {"kind": "node", "batch": BG1, "name": "blur1", "pos": [here[0] + 1, here[1]]}
    assert resolve(right, flame)["opError"] is None
    assert resolve(wrong, flame)["opErrorCode"] == "SELECTOR_EMPTY"


def test_a_pos_with_a_missing_name_is_selector_empty() -> None:
    flame, _h = dup_world()
    assert resolve(sel(DUP_A, "nope"), flame)["opErrorCode"] == "SELECTOR_EMPTY"


def test_two_same_named_nodes_at_one_position_stay_ambiguous() -> None:
    flame, h = make_batch_world()
    add_dups(h["bg1"], dup(DUP_A), dup(DUP_A))
    assert resolve(sel(DUP_A), flame)["opErrorCode"] == "SELECTOR_AMBIGUOUS"


def test_two_inner_nodes_of_one_name_are_selector_ambiguous() -> None:
    flame, h = make_batch_world()
    h["action"].nodes.append(PyCoNode("axis1"))
    inner = {"kind": "action_node", "node": {"kind": "node", "batch": BG1, "name": "action1"}}
    result = resolve({**inner, "name": "axis1"}, flame)
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"


def test_two_batch_groups_of_one_name_are_selector_ambiguous_with_their_uids() -> None:
    flame, _h = make_batch_world()
    flame.projects.current_project.current_workspace.desktop.batch_groups.append(
        PyBatch("BG1", "uid-dup")
    )
    result = resolve({"kind": "batch", "name": "BG1"}, flame)
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"
    assert "uid-b1" in result["opError"] and "uid-dup" in result["opError"]


def test_every_ambiguity_in_the_selector_prelude_uses_selector_ambiguous() -> None:
    tree = ast.parse(execution.SELECTOR_HELPER)
    checked = []
    for func in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        source = ast.get_source_segment(execution.SELECTOR_HELPER, func) or ""
        if "ambiguous" in source:
            checked.append(func.name)
            assert "SELECTOR_AMBIGUOUS" in source, func.name
            assert "SELECTOR_TYPE" not in source, func.name
    assert {"_pick_one", "_resolve_path"} <= set(checked)


def test_selector_ambiguous_is_an_error_code_with_a_hint_and_is_not_retryable() -> None:
    assert "SELECTOR_AMBIGUOUS" in get_args(ErrorCode)
    hint = errors.hint_for("SELECTOR_AMBIGUOUS")
    assert "more than one" in hint and "pos" in hint and "uid" in hint
    assert errors.is_retryable("SELECTOR_AMBIGUOUS") is False
    assert errors.as_error_code("SELECTOR_AMBIGUOUS") == "SELECTOR_AMBIGUOUS"


def test_node_list_rows_carry_pos_and_same_name_count(rig: Rig) -> None:
    items = rig.do("node.list", {"target": BG1})["result"]["items"]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_name.setdefault(item["name"], []).append(item)
    assert [(i["pos"], i["sameNameCount"]) for i in by_name["dup"]] == [
        ([100, 100], 2),
        ([400, 200], 2),
    ]
    assert by_name["blur1"][0]["sameNameCount"] == 1
    assert by_name["dup"][0]["posX"] == 100 and by_name["dup"][0]["posY"] == 100


def test_node_list_counts_names_over_the_whole_group_not_the_page(rig: Rig) -> None:
    total = rig.do("node.list", {"target": BG1})["result"]["total"]
    page = rig.do("node.list", {"target": BG1, "limit": 1, "offset": total - 1})["result"]["items"]
    assert [(i["name"], i["sameNameCount"]) for i in page] == [("dup", 2)]


def test_node_list_pos_is_null_when_it_cannot_be_read(rig: Rig) -> None:
    rig.handles["bg1"].nodes[0].pos_x = None
    first = rig.do("node.list", {"target": BG1})["result"]["items"][0]
    assert first["pos"] is None


def test_a_created_node_row_has_pos_and_same_name_count(rig: Rig) -> None:
    env = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert env["ok"] is True, env.get("error")
    row = env["result"]["node"]
    assert row["sameNameCount"] == 1 and row["pos"] == [0, 0]


def test_node_row_counts_names_from_a_list_it_is_given_and_never_reads_the_group() -> None:
    tree = ast.parse(NODE_HELPER)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    attrs = {a.attr for a in ast.walk(funcs["_node_row"]) if isinstance(a, ast.Attribute)}
    assert "nodes" not in attrs
    assert "_name_counts" in funcs


def test_action_list_inner_rows_have_no_pos_or_same_name_count(rig: Rig) -> None:
    action = next(n for n in rig.handles["bg1"].nodes if isinstance(n, PyActionNode))
    action.nodes.append(PyCoNode("Default"))
    env = rig.do("action.list_nodes", {"target": {"kind": "node", "batch": BG1, "name": "action1"}})
    assert env["ok"] is True, env.get("error")
    for row in env["result"]["nodes"]:
        assert "pos" not in row and "sameNameCount" not in row, row
    assert env["result"]["node"]["sameNameCount"] == 1 and "pos" in env["result"]["node"]


def wire_dup_into_blur(rig: Rig, pos: tuple[float, float] = DUP_B) -> None:
    batch = rig.handles["bg1"]
    source = next(
        n
        for n in batch.nodes
        if n.name.get_value() == "dup" and (n.pos_x.get_value(), n.pos_y.get_value()) == pos
    )
    blur = next(n for n in batch.nodes if n.name.get_value() == "blur1")
    batch.connect_nodes(source, "Default", blur, "Default")


def test_node_info_names_the_sockets_partners_that_match_several_nodes(rig: Rig) -> None:
    wire_dup_into_blur(rig)
    blur = rig.do("node.info", {"target": {"kind": "node", "batch": BG1, "name": "blur1"}})
    assert blur["result"]["sockets"]["input"]["Input_0"] == ["dup"]
    assert blur["result"]["ambiguousSocketNames"] == ["dup"]
    chosen = rig.do("node.info", {"target": sel(DUP_B)})["result"]
    assert chosen["ambiguousSocketNames"] == []
    assert chosen["sockets"]["output"]["Result"] == ["blur1"]


def test_node_info_ambiguous_socket_names_is_empty_when_every_partner_is_unique(rig: Rig) -> None:
    batch = rig.handles["bg1"]
    names = {n.name.get_value(): n for n in batch.nodes}
    batch.connect_nodes(names["blur1"], "Default", names["comp1"], "Default")
    info = rig.do("node.info", {"target": {"kind": "node", "batch": BG1, "name": "comp1"}})
    assert info["result"]["ambiguousSocketNames"] == []


def test_node_info_ambiguous_socket_names_is_null_when_sockets_cannot_be_read(rig: Rig) -> None:
    rig.handles["bg1"].nodes[1].sockets_error = RuntimeError("no sockets")
    env = rig.do("node.info", {"target": {"kind": "node", "batch": BG1, "name": "comp1"}})
    assert env["result"]["sockets"] is None
    assert env["result"]["ambiguousSocketNames"] is None


def test_node_info_without_pos_on_a_shared_name_is_selector_ambiguous(rig: Rig) -> None:
    env = rig.do("node.info", {"target": sel()})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_AMBIGUOUS"
    assert "pos=[100, 100]" in env["error"] and "pos=[400, 200]" in env["error"]
    assert "pos" in env["hint"]


def test_the_stable_address_of_a_shared_name_carries_pos_and_a_unique_one_does_not(
    rig: Rig,
) -> None:
    shared = rig.do("node.info", {"target": sel(DUP_B)})["result"]["stable"]
    assert shared["name"] == "dup" and shared["pos"] == [400, 200]
    unique = rig.do("node.info", {"target": {"kind": "node", "batch": BG1, "name": "comp1"}})
    assert "pos" not in unique["result"]["stable"]


def test_set_attributes_through_pos_changes_only_that_node_and_revert_puts_it_back(
    rig: Rig,
) -> None:
    env = rig.do("node.set_attributes", {"target": sel(DUP_B), "values": {"note": "hello"}})
    assert env["ok"] is True, env.get("error")
    assert (note_of(rig, DUP_A), note_of(rig, DUP_B)) == ("a", "hello")
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert (note_of(rig, DUP_A), note_of(rig, DUP_B)) == ("a", "b")


def test_set_attributes_without_pos_on_a_shared_name_writes_nothing(rig: Rig) -> None:
    env = rig.do("node.set_attributes", {"target": sel(), "values": {"note": "x"}})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_AMBIGUOUS"
    assert (note_of(rig, DUP_A), note_of(rig, DUP_B)) == ("a", "b")
    assert rig.records() == []


def test_moving_a_shared_name_node_can_still_be_reverted(rig: Rig) -> None:
    env = rig.do("node.set_attributes", {"target": sel(DUP_B), "values": {"pos_x": 900}})
    assert env["ok"] is True, env.get("error")
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    moved = [n.pos_x.get_value() for n in rig.handles["bg1"].nodes if n.name.get_value() == "dup"]
    assert moved == [100, 400]


def test_connect_through_pos_wires_that_node_and_the_target_reports_the_ambiguous_name(
    rig: Rig,
) -> None:
    env = rig.do(
        "node.connect",
        {"source": sel(DUP_A), "target": {"kind": "node", "batch": BG1, "name": "blur1"}},
    )
    assert env["ok"] is True, env.get("error")
    info = rig.do("node.info", {"target": {"kind": "node", "batch": BG1, "name": "blur1"}})
    assert info["result"]["ambiguousSocketNames"] == ["dup"]


def test_delete_through_pos_removes_that_node_and_reports_no_error(rig: Rig) -> None:
    env = rig.do("node.delete", {"target": sel(DUP_B)})
    assert env["ok"] is True, env.get("error")
    left = [
        (n.pos_x.get_value(), n.pos_y.get_value())
        for n in rig.handles["bg1"].nodes
        if n.name.get_value() == "dup"
    ]
    assert left == [DUP_A]


def test_a_wrong_pos_changes_nothing_and_is_selector_empty(rig: Rig) -> None:
    env = rig.do("node.set_attributes", {"target": sel((1, 2)), "values": {"note": "x"}})
    assert env["errorCode"] == "SELECTOR_EMPTY"
    assert (note_of(rig, DUP_A), note_of(rig, DUP_B)) == ("a", "b")
    assert rig.records() == []


BG2 = {"kind": "batch", "name": "BG2"}


def sel2(pos: tuple[float, float] | None = None, name: str = "dup") -> dict[str, Any]:
    out: dict[str, Any] = {"kind": "node", "batch": BG2, "name": name}
    if pos is not None:
        out["pos"] = list(pos)
    return out


@pytest.fixture
def rig2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    """Two same-named nodes in the CURRENT group (BG2) as well."""
    made = make_rig(monkeypatch, tmp_path)
    add_dups(made.handles["bg2"], dup(DUP_A, "a"), dup(DUP_B, "b"))
    return made


def nodes_named(rig: Rig, key: str, name: str = "dup") -> list[PyNode]:
    return [n for n in rig.handles[key].nodes if n.name.get_value() == name]


class _RenewedOnLoad(PyNode):
    """A node whose wrapper is stale after load_node_setup: Flame hands out a fresh one."""

    def load_node_setup(self, path: str) -> bool:
        super().load_node_setup(path)
        owner = self._owner
        assert owner is not None
        fresh = PyNode(
            str(self.name.get_value()),
            str(self.type.get_value()),
            extra={"note": FakeAttr(self.note.get_value())},
            pos=(self.pos_x.get_value(), self.pos_y.get_value()),
        )
        fresh._owner = owner
        owner.nodes[owner.nodes.index(self)] = fresh
        self._stale = True
        return True

    @property
    def attributes(self) -> list[str]:
        if getattr(self, "_stale", False):
            raise RuntimeError("the wrapper is stale")
        return ["name", "type", "pos_x", "pos_y", "bypass", "note"]

    @attributes.setter
    def attributes(self, value: list[str]) -> None:
        pass


def test_load_setup_on_a_shared_name_reads_back_the_node_it_loaded_into(
    rig: Rig, tmp_path: Path
) -> None:
    path = str(tmp_path / "dupnode")
    assert rig.do("node.save_setup", {"target": sel(DUP_A), "path": path})["ok"] is True
    a, b = nodes_named(rig, "bg1")
    a.note.set_value("changed")
    a.name.set_value("renamed")
    a.pos_x.set_value(900)
    file = next(tmp_path.glob("dupnode*"))
    env = rig.do("node.load_setup", {"target": sel((900, 100), "renamed"), "path": str(file)})
    assert env["ok"] is True, env.get("error")
    assert a.name.get_value() == "dup" and a.pos_x.get_value() == 100
    assert a.note.get_value() == "a" and b.note.get_value() == "b"
    assert "note" in env["result"]["changedKeys"]


def test_load_setup_falls_back_to_the_position_in_the_group_when_the_wrapper_is_stale(
    rig: Rig, tmp_path: Path
) -> None:
    batch = rig.handles["bg1"]
    batch.nodes[:] = [n for n in batch.nodes if n.name.get_value() != "dup"]
    first = _RenewedOnLoad("dup", "Clip", extra={"note": FakeAttr("a")}, pos=DUP_A)
    second = dup(DUP_B, "b")
    add_dups(batch, first, second)
    path = str(tmp_path / "dupnode")
    assert rig.do("node.save_setup", {"target": sel(DUP_A), "path": path})["ok"] is True
    first.note.set_value("changed")
    file = next(tmp_path.glob("dupnode*"))
    env = rig.do("node.load_setup", {"target": sel(DUP_A), "path": str(file)})
    assert env["ok"] is True, env.get("error")
    assert env["result"]["changedKeys"] == ["note"]
    assert [n.note.get_value() for n in nodes_named(rig, "bg1")] == ["a", "b"]


def test_duplicate_of_a_shared_name_node_that_gets_the_same_name_reads_the_copy(
    rig2: Rig,
) -> None:
    rig2.handles["bg2"].duplicate_keeps_name = True
    env = rig2.do("node.duplicate", {"target": sel2(DUP_B)})
    assert env["ok"] is True, env.get("error")
    row = env["result"]["node"]
    assert row["name"] == "dup" and row["pos"] == [450, 250] and row["sameNameCount"] == 3
    assert len(nodes_named(rig2, "bg2")) == 3


def test_create_that_lands_on_a_shared_name_reads_the_new_node(rig2: Rig) -> None:
    rig2.handles["bg2"].create_name_collides_with = "dup"
    env = rig2.do("node.create", {"target": BG2, "type": "Blur"})
    assert env["ok"] is True, env.get("error")
    row = env["result"]["node"]
    assert row is not None and row["name"] == "dup" and row["type"] == "Blur"
    assert row["sameNameCount"] == 3 and row["pos"] == [0, 0]


def test_connecting_two_same_named_nodes_by_pos_is_not_a_self_connection(rig: Rig) -> None:
    env = rig.do("node.connect", {"source": sel(DUP_A), "target": sel(DUP_B)})
    assert env["ok"] is True, env.get("error")


def test_connecting_a_node_to_itself_is_still_refused(rig: Rig) -> None:
    env = rig.do("node.connect", {"source": sel(DUP_A), "target": sel(DUP_A)})
    assert env["ok"] is False and "itself" in env["error"]
    assert rig.records() == []


def test_the_prelude_refuses_a_self_connection_of_a_shared_name_too(rig: Rig) -> None:
    result = run_op(
        rig.registry,
        "node.connect",
        {"source": sel(DUP_A), "target": sel(DUP_A)},
        rig.flame,
    )
    assert "itself" in str(result["opError"]) and result["opErrorCode"] == "INVALID_ARGS"


def add_blurs(rig: Rig, *, shared: bool) -> None:
    batch = rig.handles["bg1"]
    for index, pos in enumerate(((700, 100), (700, 300))):
        node = PyNode("tgt" if shared else f"tgt{index}", "Blur", pos=pos)
        add_dups(batch, node)


def test_connect_read_back_is_ambiguous_when_both_names_are_shared(rig: Rig) -> None:
    add_blurs(rig, shared=True)
    env = rig.do("node.connect", {"source": sel(DUP_A), "target": sel((700, 300), "tgt")})
    assert env["ok"] is True, env.get("error")
    assert env["result"]["readbackAmbiguous"] is True


def test_connect_read_back_is_not_ambiguous_when_one_end_has_a_unique_name(rig: Rig) -> None:
    add_blurs(rig, shared=False)
    target = {"kind": "node", "batch": BG1, "name": "tgt1"}
    first = rig.do("node.connect", {"source": sel(DUP_A), "target": target})
    second = rig.do(
        "node.connect",
        {"source": sel(DUP_B), "target": target, "target_socket": "Matte_0"},
    )
    assert first["result"]["readbackAmbiguous"] is False
    assert second["ok"] is True and second["result"]["readbackAmbiguous"] is False
    assert second["result"]["alreadyLinked"] is False


def test_connect_that_links_nothing_is_silent_noop_even_when_a_sibling_is_linked(
    rig: Rig,
) -> None:
    add_blurs(rig, shared=False)
    target = {"kind": "node", "batch": BG1, "name": "tgt1"}
    assert rig.do("node.connect", {"source": sel(DUP_A), "target": target})["ok"] is True
    rig.handles["bg1"].connect_makes_nothing = True
    env = rig.do(
        "node.connect",
        {"source": sel(DUP_B), "target": target, "target_socket": "Matte_0"},
    )
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"


def test_connect_that_links_nothing_is_silent_noop_when_both_names_are_shared(rig: Rig) -> None:
    add_blurs(rig, shared=True)
    rig.handles["bg1"].connect_makes_nothing = True
    env = rig.do("node.connect", {"source": sel(DUP_A), "target": sel((700, 300), "tgt")})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"


def test_reverting_the_rename_of_one_of_two_same_named_nodes_is_not_refused_by_the_server(
    rig: Rig,
) -> None:
    env = rig.do("node.set_attributes", {"target": sel(DUP_B), "values": {"name": "renamed"}})
    assert env["ok"] is True, env.get("error")
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert [n.pos_x.get_value() for n in nodes_named(rig, "bg1")] == [100, 400]


def test_renaming_a_node_to_a_name_the_group_holds_is_still_refused(rig: Rig) -> None:
    env = rig.do("node.set_attributes", {"target": sel(DUP_B), "values": {"name": "comp1"}})
    assert env["ok"] is False and "already named" in str(env["error"])
    assert (note_of(rig, DUP_A), note_of(rig, DUP_B)) == ("a", "b")


def test_a_revert_the_flame_side_refuses_is_returned_as_a_failure(rig2: Rig) -> None:
    node = nodes_named(rig2, "bg2")[1]
    env = rig2.do("node.set_attributes", {"target": sel2(DUP_B), "values": {"name": "renamed"}})
    assert env["ok"] is True, env.get("error")
    original_set = node.name.set_value

    def refuse(value: Any) -> None:
        if value == "dup":
            raise RuntimeError("Could not set Batch node name, name is already being used.")
        original_set(value)

    node.name.set_value = refuse  # type: ignore[method-assign]
    back = rig2.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is False and "already being used" in back["error"]
    assert node.name.get_value() == "renamed"


def test_delete_reports_the_names_whose_stale_links_cannot_be_told_apart(rig: Rig) -> None:
    add_blurs(rig, shared=False)
    target = {"kind": "node", "batch": BG1, "name": "tgt1"}
    assert rig.do("node.connect", {"source": sel(DUP_A), "target": target})["ok"] is True
    env = rig.do("node.delete", {"target": sel(DUP_A)})
    assert env["ok"] is True, env.get("error")
    assert env["result"]["staleLinksAmbiguousNames"] == ["dup"]


def test_delete_of_a_unique_name_reports_no_ambiguous_stale_names(rig: Rig) -> None:
    env = rig.do("node.delete", {"target": {"kind": "node", "batch": BG1, "name": "comp1"}})
    assert env["ok"] is True, env.get("error")
    assert env["result"]["staleLinksAmbiguousNames"] == []


def test_the_address_after_a_move_says_why_it_cannot_be_decided() -> None:
    from flame_mcp.journaled_write import _address_after

    stable = {"kind": "node", "batch": {"kind": "batch", "uid": "u"}, "name": "dup", "pos": [1, 2]}
    after, problem = _address_after(stable, {"pos_x": 1.5})
    assert after is None and problem is not None
    assert "shares its name" in problem and "two integers" in problem
    moved, none = _address_after(stable, {"pos_x": 9.0})
    assert none is None and moved is not None and moved["pos"] == [9, 2]
    nothing, no_problem = _address_after(stable, {"bypass": True})
    assert nothing is None and no_problem is None


def test_a_multiple_selection_and_an_ambiguous_reel_name_are_selector_ambiguous() -> None:
    assert "'SELECTOR_AMBIGUOUS'" in execution.SELECTOR_HELPER.split("def _resolve_wiretap")[0]
    from flame_mcp.batch_prelude import BATCH_HELPER

    source = BATCH_HELPER.split("def _find_reel")[1].split("def _add_clips")[0]
    assert "SELECTOR_AMBIGUOUS" in source and "SELECTOR_TYPE" not in source


def test_comments_no_longer_say_only_empty_and_type() -> None:
    from flame_mcp import envelope

    assert "SELECTOR_EMPTY/SELECTOR_TYPE today" not in execution.OP_ERROR_HELPER
    assert "SELECTOR_TYPE today" not in (Path(envelope.__file__).read_text())


def test_new_links_of_a_second_connect_from_a_shared_name_is_the_socket_it_took(rig: Rig) -> None:
    add_blurs(rig, shared=False)
    target = {"kind": "node", "batch": BG1, "name": "tgt1"}
    assert rig.do("node.connect", {"source": sel(DUP_A), "target": target})["ok"] is True
    env = rig.do(
        "node.connect",
        {"source": sel(DUP_B), "target": target, "target_socket": "Matte_0"},
    )
    assert env["result"]["newLinks"] == ["Matte_0"]
