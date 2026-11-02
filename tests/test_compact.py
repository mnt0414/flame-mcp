"""`compact.compact_envelope`: the pure transform behind FLAME_MCP_RESPONSE=compact, and the
reading of that variable."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from fake_media_tree import build_default_tree
from flame_mcp import compact
from flame_mcp.compact import ResponseModeConfigError, compact_envelope, response_mode_from_env
from flame_mcp.envelope import build_envelope
from flame_mcp.ops import register_all
from flame_mcp.registry import Registry
from op_test_helpers import run_op
from test_ops_desktop_list_all import build_world


def _registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


def _env(**kwargs: Any) -> dict[str, Any]:
    return build_envelope(ok=kwargs.pop("ok", True), **kwargs)


def _rows(n: int) -> list[dict[str, Any]]:
    return [{"name": f"c{i}", "frame": i} for i in range(n)]


# ---- outer fields -----------------------------------------------------------------------------


def test_outer_empty_values_are_left_out_and_ok_stays() -> None:
    out = compact_envelope(_env(result={"a": 1}, duration_ms=7))
    assert out == {"ok": True, "result": {"a": 1}, "durationMs": 7}


def test_outer_empty_string_list_dict_and_none_are_left_out() -> None:
    env = _env(result={"a": 1})
    env.update({"logsNote": "", "logs": [], "crash": {}, "hint": None, "extra": [], "kept": "x"})
    out = compact_envelope(env)
    for key in ("logsNote", "logs", "crash", "hint", "extra", "error", "journal", "degraded"):
        assert key not in out
    assert out["kept"] == "x"


def test_affected_and_undo_steps_are_left_out_at_zero_and_kept_otherwise() -> None:
    assert "affected" not in compact_envelope(_env(result=1, affected=0, undo_steps=0))
    assert "undoSteps" not in compact_envelope(_env(result=1, affected=0, undo_steps=0))
    out = compact_envelope(_env(result=1, affected=3, undo_steps=2))
    assert out["affected"] == 3 and out["undoSteps"] == 2


def test_ok_is_always_present_even_for_a_bare_failure() -> None:
    out = compact_envelope({"ok": False})
    assert out == {"ok": False}


def test_false_and_zero_values_that_are_not_listed_are_kept() -> None:
    env = _env(result=1, error_code="TIMEOUT", error="x")
    assert env["retryable"] is not None
    out = compact_envelope(env)
    assert out["retryable"] is env["retryable"]
    assert out["durationMs"] == 0


def test_execution_is_left_out_on_success_and_kept_on_failure() -> None:
    meta = {"op": "desktop.find", "thread": "main"}
    assert "execution" not in compact_envelope(_env(result=1, execution=meta))
    failed = compact_envelope(_env(ok=False, error="boom", error_code="TIMEOUT", execution=meta))
    assert failed["execution"] == meta
    assert failed["error"] == "boom" and failed["errorCode"] == "TIMEOUT"


def test_context_is_kept_when_it_has_a_value() -> None:
    ctx = {"project": "p", "workspace": "w", "selected": 0}
    assert compact_envelope(_env(result=1, context=ctx))["context"] == ctx


def test_failure_fields_with_values_survive() -> None:
    env = _env(
        ok=False,
        error="e",
        error_code="CONFLICT",
        traceback="tb",
        partial={"done": 1},
        pending={"handle": "h"},
        journal={"group": "g", "id": "i"},
        logs=["l"],
    )
    out = compact_envelope(env)
    for key in ("error", "errorCode", "hint", "retryable", "traceback", "partial", "pending"):
        assert out[key] == env[key], key
    assert out["journal"] == {"group": "g", "id": "i"} and out["logs"] == ["l"]


def test_outer_items_are_not_touched_inside_other_than_result() -> None:
    env = _env(result=1, partial={"a": 0, "b": None}, context={"project": None, "n": 0})
    out = compact_envelope(env)
    assert out["partial"] == {"a": 0, "b": None}
    assert out["context"] == {"project": None, "n": 0}


def test_partial_with_a_table_shaped_list_is_not_turned_into_a_table() -> None:
    env = _env(result=1)
    env["partial"] = {"list": _rows(4)}
    assert compact_envelope(env)["partial"] == {"list": _rows(4)}


# ---- result: nulls and wiretapId --------------------------------------------------------------


def test_result_none_values_are_left_out_recursively() -> None:
    result = {"a": None, "b": {"c": None, "d": 1}, "e": [{"f": None, "g": 2}], "z": 0}
    out = compact_envelope(_env(result=result))
    assert out["result"] == {"b": {"d": 1}, "e": [{"g": 2}], "z": 0}


def test_result_that_is_only_none_values_leaves_no_outer_result() -> None:
    out = compact_envelope(_env(result={"a": None}))
    assert "result" not in out


def test_result_none_inside_a_list_stays_as_an_element() -> None:
    out = compact_envelope(_env(result={"a": [1, None, 2]}))
    assert out["result"] == {"a": [1, None, 2]}


def test_wiretap_id_equal_to_selector_id_is_left_out() -> None:
    item = {"selector": {"kind": "wiretap", "id": "/p/x"}, "wiretapId": "/p/x", "name": "n"}
    out = compact_envelope(_env(result=item))
    assert out["result"] == {"selector": {"kind": "wiretap", "id": "/p/x"}, "name": "n"}


def test_wiretap_id_that_differs_from_selector_id_stays() -> None:
    item = {"selector": {"kind": "wiretap", "id": "/p/x"}, "wiretapId": "/p/y"}
    out = compact_envelope(_env(result=item))
    assert out["result"]["wiretapId"] == "/p/y"


def test_wiretap_id_stays_when_the_selector_is_not_a_wiretap() -> None:
    item = {"selector": {"kind": "path", "id": "/p/x"}, "wiretapId": "/p/x"}
    assert compact_envelope(_env(result=item))["result"]["wiretapId"] == "/p/x"


def test_wiretap_id_stays_when_there_is_no_selector() -> None:
    assert compact_envelope(_env(result={"wiretapId": "/p/x"}))["result"] == {"wiretapId": "/p/x"}


def test_wiretap_id_in_a_nested_dict_is_deduplicated_too() -> None:
    result = {"restored": {"selector": {"kind": "wiretap", "id": "w"}, "wiretapId": "w"}}
    out = compact_envelope(_env(result=result))
    assert out["result"] == {"restored": {"selector": {"kind": "wiretap", "id": "w"}}}


# ---- result: tables ---------------------------------------------------------------------------


def test_three_alike_dicts_become_columns_and_rows() -> None:
    out = compact_envelope(_env(result={"items": _rows(3)}))
    assert out["result"]["items"] == {
        "columns": ["name", "frame"],
        "rows": [["c0", 0], ["c1", 1], ["c2", 2]],
    }


def test_two_dicts_are_not_a_table() -> None:
    out = compact_envelope(_env(result={"items": _rows(2)}))
    assert out["result"]["items"] == _rows(2)


def test_result_that_is_itself_a_list_can_become_a_table() -> None:
    out = compact_envelope(_env(result=_rows(3)))
    assert out["result"] == {
        "columns": ["name", "frame"],
        "rows": [["c0", 0], ["c1", 1], ["c2", 2]],
    }


def test_nested_dicts_are_flattened_with_dots() -> None:
    items = [
        {"selector": {"kind": "wiretap", "id": f"/w{i}"}, "recordIn": {"frame": i, "tc": "t"}}
        for i in range(3)
    ]
    out = compact_envelope(_env(result={"items": items}))
    table = out["result"]["items"]
    assert table["columns"] == ["selector.kind", "selector.id", "recordIn.frame", "recordIn.tc"]
    assert table["rows"][1] == ["wiretap", "/w1", 1, "t"]


def test_wiretap_id_is_dropped_before_the_table_is_built() -> None:
    items = [
        {"selector": {"kind": "wiretap", "id": f"/w{i}"}, "wiretapId": f"/w{i}", "name": "n"}
        for i in range(3)
    ]
    table = compact_envelope(_env(result={"items": items}))["result"]["items"]
    assert table["columns"] == ["selector.kind", "selector.id", "name"]


def test_none_is_dropped_before_the_table_so_columns_come_from_what_is_left() -> None:
    items = [{"a": 1, "b": None}, {"a": 2, "b": None}, {"a": 3, "b": None}]
    table = compact_envelope(_env(result={"items": items}))["result"]["items"]
    assert table == {"columns": ["a"], "rows": [[1], [2], [3]]}


def test_missing_columns_are_filled_with_none_in_first_seen_order() -> None:
    items = [{"a": 1}, {"b": 2, "a": 3}, {"c": 4}]
    table = compact_envelope(_env(result={"items": items}))["result"]["items"]
    assert table == {
        "columns": ["a", "b", "c"],
        "rows": [[1, None, None], [3, 2, None], [None, None, 4]],
    }


def test_a_list_member_holding_a_dict_keeps_the_original_form_and_recurses() -> None:
    items: list[dict[str, Any]] = [
        {"a": 1, "tags": [{"k": 1, "z": None}], "n": None},
        {"a": 2, "tags": []},
        {"a": 3, "tags": [{"k": 2}]},
    ]
    out = compact_envelope(_env(result={"items": items}))
    assert out["result"]["items"] == [
        {"a": 1, "tags": [{"k": 1}]},
        {"a": 2, "tags": []},
        {"a": 3, "tags": [{"k": 2}]},
    ]


def test_an_empty_nested_dict_keeps_the_original_form() -> None:
    items: list[dict[str, Any]] = [{"a": {}}, {"a": {}}, {"a": {}}]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items


def test_a_list_with_a_non_dict_element_keeps_the_original_form() -> None:
    items: list[Any] = [{"a": 1}, {"a": 2}, 3]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items


def test_a_key_with_a_dot_keeps_the_original_form() -> None:
    items = [{"a.b": 1}, {"a.b": 2}, {"a.b": 3}]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items


def test_a_dotted_key_in_a_nested_dict_keeps_the_original_form() -> None:
    items = [{"o": {"x.y": i}} for i in range(3)]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items


def test_elements_with_no_columns_keep_the_original_form() -> None:
    items: list[dict[str, Any]] = [{}, {}, {}]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items


def test_elements_that_become_empty_after_dropping_none_keep_the_original_form() -> None:
    items = [{"a": None}, {"a": None}, {"a": None}]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == [{}, {}, {}]


def test_a_list_of_lists_is_left_alone_but_its_dicts_recurse() -> None:
    result = {"m": [[1, 2], [3]], "n": [[{"a": None, "b": 1}]]}
    assert compact_envelope(_env(result=result))["result"] == {
        "m": [[1, 2], [3]],
        "n": [[{"b": 1}]],
    }


def test_tables_nest_inside_a_dict_inside_a_list_that_is_not_a_table() -> None:
    result = {"groups": [{"items": _rows(3)}, {"items": _rows(3)}]}
    out = compact_envelope(_env(result=result))
    groups = out["result"]["groups"]
    assert isinstance(groups, dict) is False
    assert groups[0]["items"] == {
        "columns": ["name", "frame"],
        "rows": [["c0", 0], ["c1", 1], ["c2", 2]],
    }


def test_false_and_zero_cells_stay_as_values() -> None:
    items = [{"a": False, "b": 0}, {"a": True, "b": 1}, {"a": False, "b": 2}]
    table = compact_envelope(_env(result={"items": items}))["result"]["items"]
    assert table["rows"] == [[False, 0], [True, 1], [False, 2]]


# ---- arrays of plain values as cells -----------------------------------------------------------


def _with_path(n: int = 3, **extra: Any) -> list[dict[str, Any]]:
    return [{"name": f"c{i}", "path": ["lib", "reel", f"c{i}"], **extra} for i in range(n)]


def test_an_array_of_strings_is_a_cell_value() -> None:
    table = compact_envelope(_env(result={"items": _with_path()}))["result"]["items"]
    assert table["columns"] == ["name", "path"]
    assert table["rows"][1] == ["c1", ["lib", "reel", "c1"]]


def test_an_empty_array_is_a_cell_value() -> None:
    items = [{"a": i, "tags": []} for i in range(3)]
    table = compact_envelope(_env(result={"items": items}))["result"]["items"]
    assert table == {"columns": ["a", "tags"], "rows": [[0, []], [1, []], [2, []]]}


def test_arrays_of_numbers_null_and_bool_are_cell_values() -> None:
    items = [
        {"a": [1, 2.5]},
        {"a": [None, True, False]},
        {"a": ["x", 0, None]},
    ]
    table = compact_envelope(_env(result={"items": items}))["result"]["items"]
    assert table == {
        "columns": ["a"],
        "rows": [[[1, 2.5]], [[None, True, False]], [["x", 0, None]]],
    }


def test_an_array_in_a_nested_dict_is_a_cell_under_a_dotted_name() -> None:
    items = [{"parent": {"names": ["a", "b"], "type": "T"}} for _ in range(3)]
    table = compact_envelope(_env(result={"items": items}))["result"]["items"]
    assert table["columns"] == ["parent.names", "parent.type"]
    assert table["rows"][0] == [["a", "b"], "T"]


def test_an_array_holding_a_dict_keeps_the_original_form() -> None:
    items: list[dict[str, Any]] = [{"fx": [{"type": "Resize"}]} for _ in range(3)]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items


def test_an_array_holding_an_array_keeps_the_original_form() -> None:
    items: list[dict[str, Any]] = [{"m": [[1, 2], [3]]} for _ in range(3)]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items


def test_one_element_with_a_dict_in_its_array_stops_the_table_for_all() -> None:
    items: list[dict[str, Any]] = [{"fx": ["a"]}, {"fx": ["b"]}, {"fx": [{"t": 1}]}]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items


def test_a_passthrough_key_still_stops_the_table_even_with_plain_arrays() -> None:
    items = [{"name": "n", "path": ["a"], "args": {"x": 1}} for _ in range(3)]
    assert compact_envelope(_env(result={"items": items}))["result"]["items"] == items
    revert_items = [{"name": "n", "path": ["a"], "revert": None} for _ in range(3)]
    out = compact_envelope(_env(result={"items": revert_items}))["result"]["items"]
    assert out["columns"] == ["name", "path"]


def test_the_array_in_a_cell_is_not_the_input_array() -> None:
    items = _with_path()
    env = _env(result={"items": items})
    table = compact_envelope(env)["result"]["items"]
    table["rows"][0][1].append("changed")
    assert items[0]["path"] == ["lib", "reel", "c0"]
    assert env["result"]["items"][0]["path"] == ["lib", "reel", "c0"]


def test_the_same_array_object_in_two_elements_gives_two_cells() -> None:
    shared = ["a"]
    items = [{"p": shared} for _ in range(3)]
    rows = compact_envelope(_env(result={"items": items}))["result"]["items"]["rows"]
    rows[0][0].append("x")
    assert rows[1][0] == ["a"] and shared == ["a"]


def test_the_items_of_a_real_list_all_walk_become_a_table() -> None:
    flame, _h = build_world()
    value = run_op(_registry(), "desktop.list_all", {"limit": 500}, flame)["value"]
    assert len(value["items"]) >= 3 and all("path" in item for item in value["items"])
    table = compact_envelope(_env(result=value))["result"]["items"]
    assert table["columns"][:3] == ["type", "name", "uid"]
    assert "path" in table["columns"] and "selector.id" in table["columns"]
    assert "wiretapId" not in table["columns"]
    path_at = table["columns"].index("path")
    assert [row[path_at] for row in table["rows"]][:2] == [["RG1"], ["RG1", "Reel A"]]


def test_list_all_items_with_an_error_entry_still_become_a_table() -> None:
    items = [
        {"type": "PyReel", "name": "R", "uid": "u", "path": ["R"], "location": "desktop"},
        {"error": "folder cannot be read", "path": ["F"], "location": "library", "library": "L"},
        {"type": "PyClip", "name": "c", "uid": "v", "path": ["R", "c"], "location": "desktop"},
    ]
    table = compact_envelope(_env(result={"items": items}))["result"]["items"]
    assert table["columns"] == ["type", "name", "uid", "path", "location", "error", "library"]
    assert table["rows"][1] == [None, None, None, ["F"], "library", "folder cannot be read", "L"]


def test_the_items_of_a_real_list_segments_with_effects_become_a_table() -> None:
    flame, h = build_default_tree()
    target = {"kind": "wiretap", "id": h["seq"].get_wiretap_node_id()}
    value = run_op(
        _registry(),
        "timeline.list_segments",
        {"target": target, "version": 0, "track": 0, "limit": 50},
        flame,
    )["value"]
    base = value["items"][0]
    assert base["effects"] == [] and isinstance(base["recordIn"], dict)
    items = [{**base, "index": i, "effects": ["Resize"] * i} for i in range(3)]
    table = compact_envelope(_env(result={**value, "items": items}))["result"]["items"]
    assert "effects" in table["columns"] and "recordIn.frame" in table["columns"]
    effects_at = table["columns"].index("effects")
    assert [row[effects_at] for row in table["rows"]] == [[], ["Resize"], ["Resize", "Resize"]]


# ---- arguments that are handed back to a call -------------------------------------------------


def test_the_passthrough_keys_are_one_constant() -> None:
    assert set(compact.PASSTHROUGH_KEYS) == {"revert", "inverse", "args"}


def test_null_arguments_under_revert_and_inverse_are_kept() -> None:
    revert = {
        "op": "node.set_attributes",
        "args": {"target": {"kind": "wiretap", "id": "w"}, "values": {"a": None, "b": 1}},
    }
    inverse = {
        "op": "segment.remove_connection",
        "args": {"target": {"kind": "segment"}, "x": None},
    }
    result = {"revert": revert, "inverse": inverse, "before": None, "note": None}
    out = compact_envelope(_env(result=result))["result"]
    assert out == {"revert": revert, "inverse": inverse}


def test_a_revert_that_is_null_is_still_left_out() -> None:
    assert compact_envelope(_env(result={"revert": None, "a": 1}))["result"] == {"a": 1}


def test_the_wiretap_id_inside_revert_args_is_not_deduplicated() -> None:
    revert = {"op": "x", "args": {"selector": {"kind": "wiretap", "id": "w"}, "wiretapId": "w"}}
    assert compact_envelope(_env(result={"revert": revert}))["result"] == {"revert": revert}


def test_args_of_recorded_intents_are_kept_verbatim_and_never_tabled() -> None:
    records = [
        {
            "id": f"i{n}",
            "op": "desktop.rename",
            "args": {"target": {"kind": "wiretap", "id": "w"}, "name": None},
        }
        for n in range(4)
    ]
    out = compact_envelope(_env(result={"records": records}))["result"]
    assert out == {"records": records}


def test_a_revert_inside_a_nested_result_is_kept_too() -> None:
    step = {
        "index": 0,
        "journal": {
            "group": "g",
            "revert": {"op": "journal.revert", "args": {"group": "g", "x": None}},
        },
    }
    out = compact_envelope(_env(result={"steps": [step]}))["result"]
    assert out == {"steps": [step]}


def test_the_passthrough_copy_does_not_share_the_input() -> None:
    revert = {"op": "x", "args": {"v": [1, 2]}}
    env = _env(result={"revert": revert})
    out = compact_envelope(env)
    out["result"]["revert"]["args"]["v"].append(3)
    assert revert == {"op": "x", "args": {"v": [1, 2]}}


# ---- purity -----------------------------------------------------------------------------------


def test_the_input_is_not_modified() -> None:
    env = _env(
        result={
            "items": [
                {"selector": {"kind": "wiretap", "id": f"/w{i}"}, "wiretapId": f"/w{i}", "x": None}
                for i in range(4)
            ],
            "n": None,
        },
        context={"project": "p"},
        partial={"a": 1},
        execution={"op": "x"},
    )
    before = copy.deepcopy(env)
    out = compact_envelope(env)
    assert env == before
    out["context"]["project"] = "changed"
    out["partial"]["a"] = 2
    assert env == before


# ---- response mode from the environment -------------------------------------------------------


@pytest.mark.parametrize("env", [{}, {"FLAME_MCP_RESPONSE": "full"}])
def test_mode_is_full_when_unset_or_full(env: dict[str, str]) -> None:
    assert response_mode_from_env(env) == "full"


def test_mode_compact() -> None:
    assert response_mode_from_env({"FLAME_MCP_RESPONSE": "compact"}) == "compact"


@pytest.mark.parametrize("value", ["", "Compact", "FULL", "small", "1", " compact"])
def test_any_other_value_is_a_config_error_that_names_the_variable_and_the_value(
    value: str,
) -> None:
    with pytest.raises(ResponseModeConfigError, match="FLAME_MCP_RESPONSE") as info:
        response_mode_from_env({"FLAME_MCP_RESPONSE": value})
    assert repr(value) in str(info.value)


def test_the_module_exposes_the_variable_name() -> None:
    assert compact.RESPONSE_ENV == "FLAME_MCP_RESPONSE"
