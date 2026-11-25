"""With compact responses, flame_catalog(op=...) without `detail` answers with 'args'; the
result says which form it is, and the tool texts follow the response mode."""

from __future__ import annotations

import re
from typing import Any

import pytest

from conftest import shared_registry
from flame_mcp import catalog as catalog_mod
from flame_mcp import compact as compact_mod
from flame_mcp import server as server_mod
from flame_mcp.policy import Policy
from list_fields_helpers import declared_list_fields

FULL_HINT = "detail='full'"


def _sync(
    op: Any = None,
    detail: Any = None,
    mode: Any = "full",
    category: Any = None,
) -> dict[str, Any]:
    policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
    return server_mod._flame_catalog_sync(
        shared_registry(), policy, category, op, detail, response_mode=mode
    )


def _args_shaped(entry: dict[str, Any]) -> bool:
    return "summary" in entry and "description" not in entry


def test_compact_without_detail_answers_with_args() -> None:
    env = _sync("project.info", mode="compact")
    result = env["result"]
    assert _args_shaped(result["operation"])
    assert result["detail"] == "args"
    assert FULL_HINT in result["note"]
    assert catalog_mod.CATALOG_DETAIL_NOTE in result["note"]


def test_compact_with_explicit_full_answers_with_full() -> None:
    result = _sync("project.info", "full", "compact")["result"]
    assert "description" in result["operation"] and "summary" not in result["operation"]
    assert result["detail"] == "full"
    assert result["note"] == catalog_mod.CATALOG_DETAIL_NOTE
    assert FULL_HINT not in result["note"]


def test_compact_with_explicit_args_answers_with_args() -> None:
    result = _sync("project.info", "args", "compact")["result"]
    assert _args_shaped(result["operation"]) and result["detail"] == "args"


def test_full_mode_without_detail_answers_with_full_as_before() -> None:
    result = _sync("project.info", mode="full")["result"]
    assert "description" in result["operation"]
    assert result["detail"] == "full"
    assert result["note"] == catalog_mod.CATALOG_DETAIL_NOTE
    default = server_mod._flame_catalog_sync(
        shared_registry(), Policy.from_env({}), None, "project.info"
    )
    assert default["result"]["operation"] == result["operation"]


def test_op_list_follows_the_same_rules() -> None:
    names = ["project.info", "segment.info"]
    compact = _sync(names, mode="compact")["result"]
    assert compact["detail"] == "args" and FULL_HINT in compact["note"]
    assert all(_args_shaped(entry) for entry in compact["operations"])
    explicit = _sync(names, "full", "compact")["result"]
    assert explicit["detail"] == "full" and FULL_HINT not in explicit["note"]
    assert all("description" in entry for entry in explicit["operations"])
    plain = _sync(names, mode="full")["result"]
    assert plain["detail"] == "full" and FULL_HINT not in plain["note"]


@pytest.mark.parametrize("mode", ["full", "compact"])
def test_listing_is_the_same_in_both_modes_apart_from_the_note(mode: str) -> None:
    base = _sync(mode="full")["result"]
    listing = _sync(mode=mode)["result"]
    assert "detail" not in listing
    assert listing["operations"] == base["operations"]
    by_category = _sync(category="project", mode=mode)["result"]
    assert "detail" not in by_category


@pytest.mark.parametrize("mode", ["full", "compact"])
def test_detail_args_without_op_is_still_invalid_args(mode: str) -> None:
    env = _sync(detail="args", mode=mode)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"


def test_listing_note_and_tool_texts_follow_the_response_mode() -> None:
    full_note = catalog_mod.catalog_listing_note("full")
    compact_note = catalog_mod.catalog_listing_note("compact")
    assert full_note == catalog_mod.CATALOG_LISTING_NOTE
    assert full_note != compact_note and "default" in compact_note and FULL_HINT in compact_note
    assert _sync(mode="full")["result"]["note"] == full_note
    assert _sync(mode="compact")["result"]["note"] == compact_note
    assert _sync(category="project", mode="compact")["result"]["note"] == compact_note

    def catalog_tool(mode: Any) -> Any:
        tools = server_mod._tool_definitions(
            shared_registry(), Policy.from_env({}), response_mode=mode
        )
        return next(tool for tool in tools if tool.name == server_mod.TOOL_CATALOG)

    full_tool, compact_tool = catalog_tool("full"), catalog_tool("compact")
    default_tool = server_mod._tool_definitions(shared_registry(), Policy.from_env({}))[1]
    assert default_tool.description == full_tool.description
    assert "detail='full' (default)" in full_tool.description
    assert "detail='full' (default)" not in compact_tool.description
    assert "SILENT_NOOP" in compact_tool.description
    assert "PRECONDITION_UNMET" in compact_tool.description
    full_detail = full_tool.input_schema["properties"]["detail"]["description"]
    compact_detail = compact_tool.input_schema["properties"]["detail"]["description"]
    assert "'full' (default)" in full_detail
    assert "'args' (default)" in compact_detail
    full_schema = dict(full_tool.input_schema["properties"])
    compact_schema = dict(compact_tool.input_schema["properties"])
    full_schema.pop("detail"), compact_schema.pop("detail")
    assert full_schema == compact_schema


def test_response_format_says_the_op_detail_default() -> None:
    assert "'args'" in compact_mod.RESPONSE_FORMAT
    assert "flame_catalog" in compact_mod.RESPONSE_FORMAT


def test_instructions_stay_within_the_limit() -> None:
    assert len(server_mod.INSTRUCTIONS.encode("utf-8")) <= 2048
    assert len(server_mod.instructions_for("compact").encode("utf-8")) <= 2048


@pytest.mark.parametrize("op_name", sorted(declared_list_fields()))
def test_args_summary_of_fields_names_every_member(op_name: str) -> None:
    entry = _sync(op_name, "args", "compact")["result"]["operation"]
    param = next(p for p in entry["params"] if p["name"] == "fields")
    for member in declared_list_fields()[op_name].fields:
        assert re.search(rf"\b{member}\b", param["summary"]), (member, param["summary"])
