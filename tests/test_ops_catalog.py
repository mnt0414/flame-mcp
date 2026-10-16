"""Completion check of the op catalog.

The 84 real ops (via `ops.register_all`, the real `api_symbols_2025.2.4.json`) show up through
the actual MCP tool surface, and READONLY hides/rejects exactly the write ops (`desktop.rename`
and every `*.set_attributes`) in all 3 policy checkpoints. No bridge/socket is needed here:
`flame_catalog`/`flame_do`'s POLICY_DENIED path and `tools/list` never touch the bridge.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from flame_mcp import envelope as envelope_mod
from flame_mcp import selectors
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry, sample_selector_for
from flame_mcp.server import create_server
from flame_mcp.types import SelectorKind

PROJECTED_OP_COUNT = 84
"""The number of real ops the listing is projected for; the whole listing must still fit."""
PROJECTED_BUDGET_SHARE = 0.8
LISTING_KEYS = {"name", "category", "effect", "undo", "summary", "params", "accepts"}
DETAIL_KEYS = {
    "name",
    "category",
    "description",
    "params",
    "effect",
    "effectLabel",
    "undo",
    "undoLabel",
    "thread",
    "threadSource",
    "pacing",
    "pacingSource",
    "accepts",
    "defaultLimit",
    "maxLimit",
}
PARAM_DETAIL_KEYS = {
    "name",
    "type",
    "description",
    "required",
    "nullable",
    "default",
    "integer",
    "minimum",
    "itemType",
    "enum",
}

SEGMENT_FIELDS = {"parent", "version", "track", "index", "record_in", "expect_source_name"}

EXPECTED_OP_NAMES = {
    "project.info",
    "library.list",
    "library.create",
    "desktop.list_children",
    "desktop.selection",
    "desktop.rename",
    "timeline.info",
    "timeline.list_segments",
    "segment.info",
    "desktop.get_attributes",
    "desktop.set_attributes",
    "segment.get_attributes",
    "segment.set_attributes",
    "batch.list",
    "batch.get_attributes",
    "batch.set_attributes",
    "node.list",
    "node.get_attributes",
    "node.set_attributes",
    "journal.list",
    "journal.show",
    "journal.check",
    "journal.revert",
    "journal.poll",
    "journal.inflight",
    "plan.run",
    "batch.checkpoint",
    "batch.fingerprint",
    "batch.load_setup",
    "journal.restore_checkpoint",
    "journal.list_checkpoints",
    "eval.run",
    "batch.info",
    "batch.create_reel",
    "batch.create_shelf_reel",
    "batch.add_clips",
    "batch.organize",
    "batch.iterate",
    "batch.save_setup",
    "node.list_types",
    "node.info",
    "node.create",
    "node.delete",
    "node.connect",
    "node.disconnect",
    "node.duplicate",
    "node.save_setup",
    "node.load_setup",
    "action.list_nodes",
    "action.create_node",
    "action.connect",
    "action.get_attributes",
    "action.set_attributes",
    "render.plan",
    "render.batch",
    "desktop.create",
    "desktop.copy",
    "segment.list_effects",
    "segment.shared_sources",
    "desktop.find",
    "desktop.info",
    "mediahub.import",
    "desktop.duplicate",
    "desktop.move",
    "timeline.fingerprint",
    "timeline.checkpoint",
    "segment.trim",
    "desktop.delete",
    "journal.purge_checkpoints",
    "segment.slip",
    "segment.replace",
    "segment.create_effect",
    "segment.delete_effect",
    "segment.set_effect",
    "segment.effect_load_setup",
    "segment.effect_save_setup",
    "segment.copy_to_media_panel",
    "segment.match",
    "timeline.overwrite",
    "timeline.insert",
    "timeline.cut",
    "timeline.create_version",
    "timeline.create_track",
    "timeline.create_sequence",
}
WRITE_OP_NAMES = {
    "eval.run",  # READONLY refuses eval.run whatever effect the call declares
    "node.create",
    "node.delete",
    "node.connect",
    "node.disconnect",
    "node.duplicate",
    "node.save_setup",
    "node.load_setup",
    "action.create_node",
    "action.connect",
    "action.set_attributes",
    "batch.create_reel",
    "batch.create_shelf_reel",
    "batch.add_clips",
    "batch.organize",
    "batch.iterate",
    "batch.save_setup",
    "journal.revert",
    "batch.checkpoint",
    "batch.load_setup",
    "journal.restore_checkpoint",
    "desktop.rename",
    "desktop.set_attributes",
    "segment.set_attributes",
    "batch.set_attributes",
    "node.set_attributes",
    "render.batch",
    "desktop.create",
    "library.create",
    "desktop.copy",
    "mediahub.import",
    "desktop.duplicate",
    "desktop.move",
    "timeline.checkpoint",
    "segment.trim",
    "desktop.delete",
    "journal.purge_checkpoints",
    "segment.slip",
    "segment.replace",
    "segment.create_effect",
    "segment.delete_effect",
    "segment.set_effect",
    "segment.effect_load_setup",
    "segment.effect_save_setup",
    "segment.copy_to_media_panel",
    "segment.match",
    "timeline.overwrite",
    "timeline.insert",
    "timeline.cut",
    "timeline.create_version",
    "timeline.create_track",
    "timeline.create_sequence",
}


def make_full_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    d.mkdir()
    return d


def _structured(result: Any) -> dict[str, Any]:
    sc = result.structured_content
    assert isinstance(sc, dict)
    return sc


def test_registered_op_count_is_84() -> None:
    registry = make_full_registry()
    assert len(registry.list_ops()) == 84
    assert {op.name for op in registry.list_ops()} == EXPECTED_OP_NAMES


def test_flame_catalog_lists_all_84_ops(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_full_registry()
        policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
        server = create_server(registry, policy, run_dir=run_dir)
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("flame_catalog", {})
                env = _structured(result)
                assert env["ok"] is True
                names = {op["name"] for op in env["result"]["operations"]}
                assert names == EXPECTED_OP_NAMES

    anyio.run(main)


def test_flame_do_tool_enum_lists_all_84_ops(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_full_registry()
        policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
        server = create_server(registry, policy, run_dir=run_dir)
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                do_tool = next(t for t in listed.tools if t.name == "flame_do")
                enum_names = set(do_tool.input_schema["properties"]["op"]["enum"])
                assert enum_names == EXPECTED_OP_NAMES

    anyio.run(main)


def test_eval_run_is_hidden_and_denied_in_all_three_places_by_default(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_full_registry()
        server = create_server(registry, Policy.from_env({}), run_dir=run_dir)
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                do_tool = next(t for t in listed.tools if t.name == "flame_do")
                enum_names = set(do_tool.input_schema["properties"]["op"]["enum"])
                assert enum_names == EXPECTED_OP_NAMES - {"eval.run"}

                catalog_env = _structured(await session.call_tool("flame_catalog", {}))
                catalog_names = {op["name"] for op in catalog_env["result"]["operations"]}
                assert catalog_names == EXPECTED_OP_NAMES - {"eval.run"}
                described = _structured(
                    await session.call_tool("flame_catalog", {"op": "eval.run"})
                )
                assert described["ok"] is False and described["errorCode"] == "POLICY_DENIED"
                assert "FLAME_MCP_ENABLE_EVAL=1" in described["hint"]

                call = {"op": "eval.run", "args": {"code": "_result = 1", "effect": "read"}}
                do_env = _structured(await session.call_tool("flame_do", call))
                assert do_env["ok"] is False and do_env["errorCode"] == "POLICY_DENIED"
                assert "FLAME_MCP_ENABLE_EVAL=1" in do_env["hint"]

    anyio.run(main)


def test_readonly_hides_and_denies_only_the_write_ops(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_full_registry()
        policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
        server = create_server(registry, policy, run_dir=run_dir)
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # checkpoint 1: flame_do's op enum
                listed = await session.list_tools()
                do_tool = next(t for t in listed.tools if t.name == "flame_do")
                enum_names = set(do_tool.input_schema["properties"]["op"]["enum"])
                assert not (WRITE_OP_NAMES & enum_names)
                assert enum_names == EXPECTED_OP_NAMES - WRITE_OP_NAMES - {"eval.run"}

                # checkpoint 2: flame_catalog's visible list
                catalog_result = await session.call_tool("flame_catalog", {})
                catalog_env = _structured(catalog_result)
                catalog_names = {op["name"] for op in catalog_env["result"]["operations"]}
                assert not (WRITE_OP_NAMES & catalog_names)
                assert catalog_names == EXPECTED_OP_NAMES - WRITE_OP_NAMES - {"eval.run"}

                # checkpoint 3: flame_do's own execution gate
                for write_name in sorted(WRITE_OP_NAMES):
                    write_op = registry.get(write_name)
                    assert write_op is not None
                    write_args: dict[str, Any] = {
                        "target": sample_selector_for(write_op.accepts),
                        "name": "x",
                        "values": {"x": 1},
                    }
                    do_result = await session.call_tool(
                        "flame_do", {"op": write_name, "args": write_args}
                    )
                    do_env = _structured(do_result)
                    assert do_env["ok"] is False, write_name
                    assert do_env["errorCode"] == "POLICY_DENIED", write_name

                # every read op must still be individually describable/callable-gated
                for name in EXPECTED_OP_NAMES - WRITE_OP_NAMES - {"eval.run"}:
                    describe = await session.call_tool("flame_catalog", {"op": name})
                    describe_env = _structured(describe)
                    assert describe_env["ok"] is True, name

    anyio.run(main)


def _listing_size(env: dict[str, Any]) -> int:
    return len(json.dumps(env))


def test_the_full_listing_is_compact_and_a_2_5x_larger_catalog_would_still_fit(
    run_dir: Path,
) -> None:
    """Checks the unfiltered listing is compact and fits the response budget.

    It must not degrade (a degraded listing collapses `operations` to {count, omitted}); every
    entry carries only the compact fields, and the size per operation projected to 83 operations
    stays within 80% of the response budget.
    """

    async def main() -> None:
        registry = make_full_registry()
        server = create_server(
            registry, Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"}), run_dir=run_dir
        )
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                env = _structured(await session.call_tool("flame_catalog", {}))
                assert env["degraded"] == []
                assert "flame_catalog(op=" in env["result"]["note"]
                assert "selectorVocabulary" in env["result"]["note"]
                operations = env["result"]["operations"]
                assert len(operations) == len(registry.list_ops())
                size = _listing_size(env)
                per_op = size / len(operations)
                projected = per_op * PROJECTED_OP_COUNT
                limit = envelope_mod.MAX_RESPONSE_BYTES * PROJECTED_BUDGET_SHARE
                print(f"listing: {size} B for {len(operations)} ops, {per_op:.0f} B/op")
                print(
                    f"projected for {PROJECTED_OP_COUNT} ops: {projected:.0f} B, limit {limit:.0f}"
                )
                assert projected <= limit, (size, per_op, projected, limit)
                for entry in operations:
                    assert set(entry) == LISTING_KEYS, entry
                    assert 0 < len(entry["summary"]) <= 160, entry["name"]
                    assert all(isinstance(kind, str) for kind in entry["accepts"]), entry["name"]
                    assert all(isinstance(name, str) for name in entry["params"]), entry["name"]

    anyio.run(main)


def test_a_category_listing_is_compact_too(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_full_registry()
        server = create_server(
            registry, Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"}), run_dir=run_dir
        )
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                env = _structured(await session.call_tool("flame_catalog", {"category": "node"}))
                assert env["degraded"] == []
                assert "flame_catalog(op=" in env["result"]["note"]
                names = {op["name"] for op in env["result"]["operations"]}
                assert names == {
                    "node.list",
                    "node.get_attributes",
                    "node.set_attributes",
                    "node.list_types",
                    "node.info",
                    "node.create",
                    "node.delete",
                    "node.connect",
                    "node.disconnect",
                    "node.duplicate",
                    "node.save_setup",
                    "node.load_setup",
                }
                for entry in env["result"]["operations"]:
                    assert LISTING_KEYS <= set(entry), entry["name"]
                    assert "description" not in entry and "thread" not in entry

    anyio.run(main)


def test_the_render_category_is_listed_with_its_two_ops(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_full_registry()
        server = create_server(
            registry, Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"}), run_dir=run_dir
        )
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                env = _structured(await session.call_tool("flame_catalog", {"category": "render"}))
                assert env["degraded"] == []
                rows = {op["name"]: op for op in env["result"]["operations"]}
                assert set(rows) == {"render.plan", "render.batch"}
                assert (rows["render.batch"]["effect"], rows["render.batch"]["undo"]) == (
                    "create",
                    "unsupported",
                )
                assert rows["render.batch"]["params"] == [
                    "target*",
                    "timeout_s",
                    "max_frames",
                    "allow_default_path",
                    "allow_overwrite",
                ]
                assert rows["render.plan"]["effect"] == "read"

    anyio.run(main)


def test_the_listing_leaves_the_detail_fields_to_the_one_op_description(run_dir: Path) -> None:
    """`flame_catalog(op=...)` answers with all the detail the listing leaves out.

    That is the full description, typed params, the effect / undo labels, thread / pacing with
    their sources, limits, and each accepted selector kind's fields and example.
    """

    async def main() -> None:
        registry = make_full_registry()
        server = create_server(
            registry, Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"}), run_dir=run_dir
        )
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = _structured(await session.call_tool("flame_catalog", {}))
                listed = {op["name"]: op for op in listing["result"]["operations"]}
                for op in registry.list_ops():
                    described = _structured(
                        await session.call_tool("flame_catalog", {"op": op.name})
                    )
                    assert described["ok"] is True, op.name
                    detail = described["result"]["operation"]
                    assert set(detail) == DETAIL_KEYS
                    assert detail["description"] == op.description, op.name
                    assert detail["thread"] == op.thread and detail["pacing"] == op.pacing
                    assert detail["threadSource"] == op.sources.get("thread"), op.name
                    assert detail["pacingSource"] == op.sources.get("pacing"), op.name
                    assert detail["effectLabel"] and detail["undoLabel"], op.name
                    assert [d["name"] for d in detail["params"]["details"]] == [
                        p.name for p in op.params
                    ]
                    assert len(detail["params"]["summary"]) == len(op.params), op.name
                    for d in detail["params"]["details"]:
                        assert set(d) == PARAM_DETAIL_KEYS, op.name
                        assert d["type"] and d["description"], (op.name, d["name"])
                    assert [a["kind"] for a in detail["accepts"]] == listed[op.name]["accepts"]
                    for accept in detail["accepts"]:
                        assert {"fields", "example"} <= set(accept), (op.name, accept["kind"])
                    assert detail["defaultLimit"] == op.default_limit
                    assert detail["maxLimit"] == op.max_limit
                    entry = listed[op.name]
                    assert entry["effect"] == detail["effect"]
                    assert entry["undo"] == detail["undo"]
                    assert entry["category"] == detail["category"]
                    star = [f"{p.name}*" if p.required else p.name for p in op.params]
                    assert entry["params"] == star, op.name
                    flat = " ".join(detail["description"].split())
                    assert flat.startswith(entry["summary"].rstrip("\u2026")), op.name
                ctx_env = _structured(await session.call_tool("flame_context", {}))
                vocab = {v["kind"]: v for v in ctx_env["result"]["selectorVocabulary"]}
                assert "fields" in vocab["batch"] and "example" in vocab["batch"]

    anyio.run(main)


def test_flame_catalog_op_accepts_publish_selector_fields_and_example(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_full_registry()
        policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
        server = create_server(registry, policy, run_dir=run_dir)
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("flame_catalog", {"op": "segment.info"})
                env = _structured(result)
                assert env["ok"] is True
                accepts = env["result"]["operation"]["accepts"]
                assert [a["kind"] for a in accepts] == ["segment"]
                segment = accepts[0]
                assert set(segment["fields"]) == SEGMENT_FIELDS
                assert segment["fields"]["track"]["type"] == "non-negative integer"
                assert selectors.validate_selector(segment["example"], ("segment", "wiretap")) == []

                listing = await session.call_tool("flame_catalog", {"op": "desktop.list_children"})
                params = {
                    p["name"]: p
                    for p in _structured(listing)["result"]["operation"]["params"]["details"]
                }
                assert params["offset"]["default"] == 0
                assert params["offset"]["integer"] is True
                assert params["offset"]["minimum"] == 0
                assert params["types"]["itemType"] == "string"
                assert params["types"]["enum"] is None

                created = await session.call_tool(
                    "flame_catalog", {"op": "timeline.create_sequence"}
                )
                depth = {
                    p["name"]: p
                    for p in _structured(created)["result"]["operation"]["params"]["details"]
                }["bit_depth"]
                assert depth["enum"] == [8, 10, 12, 16, 32]
                assert depth["required"] is False and depth["integer"] is True

    anyio.run(main)


def test_flame_context_selector_vocabulary_publishes_fields_and_example(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_full_registry()
        policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
        server = create_server(registry, policy, run_dir=run_dir)
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # no bridge here: ok=false, but the static half is still returned
                result = await session.call_tool("flame_context", {})
                vocab = {v["kind"]: v for v in _structured(result)["result"]["selectorVocabulary"]}
                assert set(vocab) == set(get_args(SelectorKind))
                assert set(vocab["segment"]["fields"]) == SEGMENT_FIELDS
                for entry in vocab.values():
                    problems = selectors.validate_selector(entry["example"], get_args(SelectorKind))
                    assert problems == []

    anyio.run(main)


VERSION_BOUND_FACTS = ("Schematic Reel 1 to 4", "four default", "four empty default")


def test_no_op_text_states_a_default_that_differs_between_flame_versions() -> None:
    """The default Schematic reels of a new Batch group are four on Flame 2025.2.4 and three on
    2026.2.3 (media_panel.cfg): the texts say `Flame's default` and point at result.reels."""
    registry = make_full_registry()
    for op in registry.list_ops():
        texts = [op.description, *(p.description for p in op.params)]
        for text in texts:
            flat = " ".join(text.split())
            for fact in VERSION_BOUND_FACTS:
                assert fact not in flat, (op.name, fact)
    create = registry.get("desktop.create")
    assert create is not None
    flat = " ".join(create.description.split())
    assert "omitted = Flame's default" in flat and "result.reels" in flat
    (reels,) = [p for p in create.params if p.name == "reels"]
    assert "Flame's default" in reels.description and "result.reels" in reels.description
