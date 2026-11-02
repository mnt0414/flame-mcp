"""`fields` on the four list ops: the server keeps only the named top-level members of each item
after the op has run (the code that runs in Flame is the same with or without it)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import build_default_tree
from flame_mcp import execution, list_fields
from flame_mcp.callargs import apply_param_defaults
from flame_mcp.envelope import build_envelope
from flame_mcp.journal import Journal
from flame_mcp.ops import register_all
from flame_mcp.opschema import validate_op_args
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.runtime import Runtime
from op_test_helpers import run_op
from test_ops_desktop_list_all import build_world

OPS = ("desktop.list_children", "desktop.list_all", "desktop.find", "timeline.list_segments")
TARGET = {"kind": "wiretap", "id": "/p/x"}
BASE_ARGS: dict[str, dict[str, Any]] = {
    "desktop.list_children": {"target": TARGET},
    "desktop.list_all": {},
    "desktop.find": {"name": "x"},
    "timeline.list_segments": {"target": TARGET},
}


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry()
    register_all(reg)
    return reg


def _item(op: str, i: int) -> dict[str, Any]:
    return {key: f"{key}{i}" for key in list_fields.LIST_FIELDS[op].fields}


def _fake_result(op: str) -> dict[str, Any]:
    items = [_item(op, 0), {"error": "unreadable", "path": ["p"]}, _item(op, 2)]
    return {"total": 3, "offset": 0, "shown": 3, "truncated": False, "items": items, "uid": "u"}


class RunLog:
    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture
def runtime(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Runtime, RunLog]:
    log = RunLog()

    def fake_run_op(op: Any, args: Any, **kwargs: Any) -> dict[str, Any]:
        log.calls.append(op.name)
        return build_envelope(ok=True, result=_fake_result(op.name))

    monkeypatch.setattr(execution, "run_op", fake_run_op)
    return Runtime(registry, Policy.from_env({}), Journal(tmp_path / "journal")), log


# ---- the declaration ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_name", OPS)
def test_each_list_op_declares_an_optional_string_array_fields_param(
    registry: Registry, op_name: str
) -> None:
    op = registry.get(op_name)
    assert op is not None
    (param,) = [p for p in op.params if p.name == "fields"]
    assert param.type == "array" and param.item_type == "string" and param.required is False
    assert param.description.isascii()
    for name in list_fields.LIST_FIELDS[op_name].fields:
        assert name in param.description


@pytest.mark.parametrize("op_name", OPS)
def test_the_op_description_says_what_fields_does(registry: Registry, op_name: str) -> None:
    op = registry.get(op_name)
    assert op is not None
    assert "`fields`" in op.description


def test_the_table_covers_exactly_the_four_ops() -> None:
    assert set(list_fields.LIST_FIELDS) == set(OPS)
    assert {spec.items_key for spec in list_fields.LIST_FIELDS.values()} == {"items"}


# ---- the table matches what the ops emit -------------------------------------------------------


def _emitted_keys(items: list[dict[str, Any]]) -> set[str]:
    return {key for item in items for key in item}


def test_list_children_items_use_only_listed_fields(registry: Registry) -> None:
    flame, _h = build_default_tree()
    result = run_op(
        registry,
        "desktop.list_children",
        {"target": {"kind": "path", "path": ["P1O", "R1"]}, "limit": 50},
        flame,
    )
    keys = _emitted_keys(result["value"]["items"])
    allowed = set(list_fields.LIST_FIELDS["desktop.list_children"].fields)
    assert keys and keys <= allowed | {"error"}
    assert {"type", "name", "uid", "wiretapId", "width", "height", "bitDepth"} <= keys


def test_list_all_items_use_only_listed_fields(registry: Registry) -> None:
    flame, _h = build_world()
    result = run_op(registry, "desktop.list_all", {"limit": 500}, flame)
    keys = _emitted_keys(result["value"]["items"])
    allowed = set(list_fields.LIST_FIELDS["desktop.list_all"].fields)
    assert keys <= allowed | {"error"}
    assert allowed <= keys


def test_find_items_use_only_listed_fields(registry: Registry) -> None:
    flame, _h = build_default_tree()
    result = run_op(registry, "desktop.find", {"name": "clip1", "limit": 50, "offset": 0}, flame)
    keys = _emitted_keys(result["value"]["items"])
    allowed = set(list_fields.LIST_FIELDS["desktop.find"].fields)
    assert keys == allowed


def test_list_segments_items_use_only_listed_fields(registry: Registry) -> None:
    flame, h = build_default_tree()
    target = {"kind": "wiretap", "id": h["seq"].get_wiretap_node_id()}
    result = run_op(
        registry,
        "timeline.list_segments",
        {"target": target, "version": 0, "track": 0, "limit": 50, "include_connections": True},
        flame,
    )
    assert result["opError"] is None
    keys = _emitted_keys(result["value"]["items"])
    assert keys == set(list_fields.LIST_FIELDS["timeline.list_segments"].fields)


# ---- selecting ---------------------------------------------------------------------------------


@pytest.mark.parametrize("op_name", OPS)
def test_only_the_chosen_members_stay_and_an_error_item_keeps_its_error(
    runtime: tuple[Runtime, RunLog], op_name: str
) -> None:
    rt, log = runtime
    first = list_fields.LIST_FIELDS[op_name].fields[0]
    env = rt.call(op_name, {**BASE_ARGS[op_name], "fields": [first, "type"]})
    assert env["ok"] is True and log.calls == [op_name]
    items = env["result"]["items"]
    wanted = {first, "type"} & set(list_fields.LIST_FIELDS[op_name].fields)
    assert items[0] == {key: f"{key}0" for key in wanted}
    assert set(items[2]) == wanted
    assert items[1]["error"] == "unreadable"
    assert set(items[1]) <= wanted | {"error"}
    assert {k: v for k, v in env["result"].items() if k != "items"} == {
        k: v for k, v in _fake_result(op_name).items() if k != "items"
    }


@pytest.mark.parametrize("op_name", OPS)
def test_items_keep_their_own_order_whatever_the_order_of_fields(
    runtime: tuple[Runtime, RunLog], op_name: str
) -> None:
    rt, _log = runtime
    names = list_fields.LIST_FIELDS[op_name].fields
    env = rt.call(op_name, {**BASE_ARGS[op_name], "fields": [names[1], names[0], names[1]]})
    assert list(env["result"]["items"][0]) == [names[0], names[1]]


@pytest.mark.parametrize("op_name", OPS)
def test_omitted_or_empty_fields_change_nothing(
    runtime: tuple[Runtime, RunLog], op_name: str
) -> None:
    rt, _log = runtime
    plain = rt.call(op_name, BASE_ARGS[op_name])
    empty = rt.call(op_name, {**BASE_ARGS[op_name], "fields": []})
    assert plain["result"] == _fake_result(op_name)
    assert empty["result"] == _fake_result(op_name)


@pytest.mark.parametrize("op_name", OPS)
def test_an_unknown_field_is_invalid_args_before_anything_runs(
    runtime: tuple[Runtime, RunLog], op_name: str
) -> None:
    rt, log = runtime
    first = list_fields.LIST_FIELDS[op_name].fields[0]
    env = rt.call(op_name, {**BASE_ARGS[op_name], "fields": [first, "nmae"]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "nmae" in env["error"] and first in env["error"]
    assert log.calls == []


def test_a_field_of_another_list_op_is_unknown_here(runtime: tuple[Runtime, RunLog]) -> None:
    rt, log = runtime
    env = rt.call("desktop.find", {"name": "x", "fields": ["recordIn"]})
    assert env["errorCode"] == "INVALID_ARGS" and log.calls == []


def test_error_is_not_a_field_that_can_be_asked_for(runtime: tuple[Runtime, RunLog]) -> None:
    rt, log = runtime
    env = rt.call("desktop.find", {"name": "x", "fields": ["error"]})
    assert env["errorCode"] == "INVALID_ARGS" and log.calls == []


def test_fields_that_are_not_strings_are_invalid_args(runtime: tuple[Runtime, RunLog]) -> None:
    rt, log = runtime
    env = rt.call("desktop.find", {"name": "x", "fields": [1]})
    assert env["errorCode"] == "INVALID_ARGS" and log.calls == []


def test_a_failed_op_result_is_left_alone(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = build_envelope(
        ok=False, result={"items": [{"name": "a", "uid": "b"}]}, error="x", error_code="TIMEOUT"
    )
    monkeypatch.setattr(execution, "run_op", lambda op, args, **kw: dict(failed))
    rt = Runtime(registry, Policy.from_env({}), Journal(tmp_path / "journal"))
    env = rt.call("desktop.find", {"name": "x", "fields": ["name"]})
    assert env["result"] == {"items": [{"name": "a", "uid": "b"}]}


def test_the_code_sent_to_flame_is_the_same_with_and_without_fields(registry: Registry) -> None:
    for op_name in OPS:
        op = registry.get(op_name)
        assert op is not None and op.to_python is not None
        args: dict[str, Any] = {"limit": 5, **BASE_ARGS[op_name]}
        apply_param_defaults(op, args)
        assert op.to_python(args) == op.to_python({**args, "fields": ["name"]})


def test_fields_are_validated_by_the_ordinary_argument_check_too(registry: Registry) -> None:
    op = registry.get("desktop.find")
    assert op is not None
    assert validate_op_args(op, {"name": "x", "fields": "name"}).ok is False
