"""Completion check of the op catalog.

The real ops (via `ops.register_all`, the real `api_symbols_2025.2.4.json`) show up through
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

from flame_mcp import catalog as catalog_mod
from flame_mcp import compact as compact_mod
from flame_mcp import envelope as envelope_mod
from flame_mcp import opschema, selectors
from flame_mcp import server as server_mod
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry, sample_selector_for
from flame_mcp.server import create_server
from flame_mcp.types import NO_DEFAULT, SelectorKind

REGISTERED_OP_COUNT = 112
"""The number of registered ops: the one place that counts them. Other tests import it."""
PROJECTED_BUDGET_SHARE = 0.8
LISTING_KEYS = {"name", "category", "effect", "undo", "summary", "params", "accepts"}
DETAIL_KEYS = {
    "name",
    "category",
    "description",
    "params",
    "effect",
    "undo",
    "thread",
    "pacing",
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
    "project.save",
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
    "plan.for_each",
    "batch.checkpoint",
    "batch.fingerprint",
    "batch.compare_setup",
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
    "node.edit_setup",
    "node.read_setup",
    "action.list_nodes",
    "action.create_node",
    "action.connect",
    "action.get_attributes",
    "action.set_attributes",
    "render.plan",
    "render.batch",
    "render.clip",
    "render.export",
    "render.still",
    "batch.delete",
    "desktop.sync_record_tc",
    "desktop.get_record_tc",
    "desktop.set_record_tc",
    "timeline.colour_spaces",
    "desktop.create",
    "desktop.copy",
    "segment.list_effects",
    "segment.shared_sources",
    "desktop.find",
    "desktop.info",
    "desktop.list_all",
    "mediahub.import",
    "desktop.duplicate",
    "desktop.move",
    "desktop.arrange",
    "desktop.reorder",
    "timeline.fingerprint",
    "timeline.checkpoint",
    "segment.trim",
    "desktop.delete",
    "desktop.reformat",
    "journal.purge_checkpoints",
    "segment.slip",
    "segment.replace",
    "segment.create_effect",
    "segment.delete_effect",
    "segment.set_effect",
    "segment.effect_load_setup",
    "segment.edit_effect_setup",
    "segment.read_effect_setup",
    "segment.effect_save_setup",
    "segment.copy_to_media_panel",
    "segment.match",
    "timeline.overwrite",
    "timeline.insert",
    "timeline.cut",
    "timeline.create_version",
    "timeline.create_track",
    "timeline.create_sequence",
    "timeline.delete_segments",
    "timeline.move_track",
    "timeline.shorten",
    "timeline.compare",
    "timeline.usage",
    "segment.connections",
    "segment.create_connection",
    "segment.remove_connection",
    "segment.sync_connections",
}
WRITE_OP_NAMES = {
    "project.save",
    "eval.run",  # READONLY refuses eval.run whatever effect the call declares
    "node.create",
    "node.delete",
    "node.connect",
    "node.disconnect",
    "node.duplicate",
    "node.save_setup",
    "node.load_setup",
    "node.edit_setup",
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
    "render.clip",
    "render.export",
    "render.still",
    "batch.delete",
    "desktop.sync_record_tc",
    "desktop.set_record_tc",
    "desktop.create",
    "library.create",
    "desktop.copy",
    "mediahub.import",
    "desktop.duplicate",
    "desktop.move",
    "desktop.arrange",
    "desktop.reorder",
    "timeline.checkpoint",
    "segment.trim",
    "desktop.delete",
    "desktop.reformat",
    "journal.purge_checkpoints",
    "segment.slip",
    "segment.replace",
    "segment.create_effect",
    "segment.delete_effect",
    "segment.set_effect",
    "segment.effect_load_setup",
    "segment.edit_effect_setup",
    "segment.effect_save_setup",
    "segment.copy_to_media_panel",
    "segment.match",
    "timeline.overwrite",
    "timeline.insert",
    "timeline.cut",
    "timeline.create_version",
    "timeline.create_track",
    "timeline.create_sequence",
    "timeline.delete_segments",
    "timeline.move_track",
    "timeline.shorten",
    "segment.create_connection",
    "segment.remove_connection",
    "segment.sync_connections",
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


def test_registered_op_count() -> None:
    registry = make_full_registry()
    assert len(registry.list_ops()) == REGISTERED_OP_COUNT
    assert {op.name for op in registry.list_ops()} == EXPECTED_OP_NAMES


def test_flame_catalog_lists_all_ops(run_dir: Path) -> None:
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


def test_flame_do_tool_enum_lists_all_ops(run_dir: Path) -> None:
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
                projected = per_op * REGISTERED_OP_COUNT
                limit = envelope_mod.MAX_RESPONSE_BYTES * PROJECTED_BUDGET_SHARE
                print(f"listing: {size} B for {len(operations)} ops, {per_op:.0f} B/op")
                print(
                    f"projected for {REGISTERED_OP_COUNT} ops: {projected:.0f} B, limit {limit:.0f}"
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
                    "node.edit_setup",
                    "node.read_setup",
                }
                for entry in env["result"]["operations"]:
                    assert LISTING_KEYS <= set(entry), entry["name"]
                    assert "description" not in entry and "thread" not in entry

    anyio.run(main)


def test_the_render_category_is_listed_with_its_three_ops(run_dir: Path) -> None:
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
                assert set(rows) == {
                    "render.plan",
                    "render.batch",
                    "render.clip",
                    "render.export",
                    "render.still",
                }
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

    That is the full description, typed params, thread / pacing, limits, and each accepted
    selector kind's field names, types and example.
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
                    assert [d["name"] for d in detail["params"]["details"]] == [
                        p.name for p in op.params
                    ]
                    assert len(detail["params"]["summary"]) == len(op.params), op.name
                    for d in detail["params"]["details"]:
                        assert set(d) == PARAM_DETAIL_KEYS, op.name
                        assert d["type"] and d["description"], (op.name, d["name"])
                    assert [a["kind"] for a in detail["accepts"]] == listed[op.name]["accepts"]
                    for accept in detail["accepts"]:
                        assert set(accept) == {"kind", "fields", "example"}, (op.name, accept)
                        for field in accept["fields"].values():
                            assert set(field) == {"type", "required"}, (op.name, accept["kind"])
                    assert described["result"]["note"] == catalog_mod.CATALOG_DETAIL_NOTE
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
                assert vocab["batch"]["label"]
                assert all(
                    "description" in f and "type" in f and "required" in f
                    for f in vocab["segment"]["fields"].values()
                )

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


def _sync(registry: Registry, op: Any = None, detail: Any = None, category: Any = None) -> Any:
    policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
    return server_mod._flame_catalog_sync(registry, policy, category, op, detail)


def _bytes(value: Any) -> int:
    return len(json.dumps(value).encode("utf-8"))


def test_describe_kind_brief_drops_labels_and_descriptions() -> None:
    for kind in get_args(SelectorKind):
        full = selectors.describe_kind(kind)
        brief = selectors.describe_kind_brief(kind)
        assert set(brief) == {"kind", "fields", "example"}
        assert brief["example"] == full["example"]
        assert set(brief["fields"]) == set(full["fields"])
        for name, field in brief["fields"].items():
            assert field == {
                "type": full["fields"][name]["type"],
                "required": full["fields"][name]["required"],
            }


def test_args_detail_is_a_small_fraction_of_full_for_all_ops() -> None:
    registry = make_full_registry()
    full_total = args_total = 0
    for op in registry.list_ops():
        full = _sync(registry, op.name)["result"]["operation"]
        args = _sync(registry, op.name, "args")["result"]["operation"]
        assert _bytes(args) < _bytes(full), op.name
        full_total += _bytes(full)
        args_total += _bytes(args)
        assert set(args) == {"name", "effect", "undo", "summary", "params", "accepts"}
        assert args["summary"] == opschema.first_sentence(op.description)
        assert [a["kind"] for a in args["accepts"]] == list(op.accepts)
        assert [p["name"] for p in args["params"]] == [p.name for p in op.params]
        for item, param in zip(args["params"], op.params, strict=True):
            assert {"name", "type", "required", "summary"} <= set(item)
            assert set(item) <= {
                "name",
                "type",
                "required",
                "summary",
                "default",
                "enum",
                "minimum",
                "itemType",
            }
            assert item["required"] is param.required
            assert len(item["summary"]) <= len(param.description), (op.name, param.name)
            assert ("default" in item) is (param.default is not NO_DEFAULT)
            assert ("enum" in item) is (param.enum is not None)
            assert ("minimum" in item) is (param.minimum is not None)
            assert ("itemType" in item) is (param.item_type is not None)
    assert args_total < full_total * 0.4


def test_args_detail_has_no_full_description() -> None:
    registry = make_full_registry()
    text = json.dumps(_sync(registry, "segment.replace", "args"))
    op = registry.get("segment.replace")
    assert op is not None
    assert op.description not in text
    env = _sync(registry, "desktop.list_children", "args")
    params = {p["name"]: p for p in env["result"]["operation"]["params"]}
    assert params["offset"]["default"] == 0 and params["offset"]["minimum"] == 0
    assert params["types"]["itemType"] == "string"


def test_detail_errors() -> None:
    registry = make_full_registry()
    bad = _sync(registry, "segment.info", "brief")
    assert bad["ok"] is False and bad["errorCode"] == "INVALID_ARGS"
    no_op = _sync(registry, None, "args")
    assert no_op["ok"] is False and no_op["errorCode"] == "INVALID_ARGS"
    assert "op" in no_op["error"]
    assert _sync(registry, None, "full")["ok"] is True
    assert _sync(registry)["ok"] is True


@pytest.mark.parametrize("bad_op", [[], ["a.b"] * 11, [1], ["segment.info", 2], 5, {}])
def test_op_list_input_errors(bad_op: Any) -> None:
    env = _sync(make_full_registry(), bad_op)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"


def test_op_list_returns_entries_in_order_without_duplicates() -> None:
    registry = make_full_registry()
    names = ["segment.info", "project.info", "segment.info", "desktop.list_children"]
    env = _sync(registry, names)
    assert env["ok"] is True
    result = env["result"]
    assert "operation" not in result and "unavailable" not in result and "omitted" not in result
    assert [e["name"] for e in result["operations"]] == [
        "segment.info",
        "project.info",
        "desktop.list_children",
    ]
    assert set(result) == {"categories", "detail", "note", "operations"}
    assert result["note"] == catalog_mod.CATALOG_DETAIL_NOTE
    assert result["operations"][0] == _sync(registry, "segment.info")["result"]["operation"]
    args = _sync(registry, names, "args")["result"]["operations"]
    assert set(args[0]) == {"name", "effect", "undo", "summary", "params", "accepts"}


def test_op_list_reports_unavailable_names_and_returns_the_rest() -> None:
    registry = make_full_registry()
    policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    env = server_mod._flame_catalog_sync(
        registry, policy, None, ["segment.infoo", "project.info", "desktop.rename"]
    )
    assert env["ok"] is True
    result = env["result"]
    assert [e["name"] for e in result["operations"]] == ["project.info"]
    by_name = {u["name"]: u for u in result["unavailable"]}
    assert by_name["segment.infoo"]["errorCode"] == "UNKNOWN_OPERATION"
    assert by_name["segment.infoo"]["suggestion"] == "segment.info"
    assert by_name["desktop.rename"]["errorCode"] == "POLICY_DENIED"
    assert by_name["desktop.rename"]["error"]
    assert "suggestion" not in by_name["desktop.rename"]


def test_op_list_of_only_unavailable_names_is_invalid_args() -> None:
    env = _sync(make_full_registry(), ["segment.infoo", "zzz.nothing"])
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "segment.infoo" in env["error"] and "zzz.nothing" in env["error"]
    assert "segment.info" in env["error"]


def test_op_list_omits_entries_past_the_size_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = make_full_registry()
    names = ["segment.replace", "plan.run", "segment.info", "project.info"]
    first = _sync(registry, names[:1])["result"]
    monkeypatch.setattr(server_mod, "CATALOG_RESULT_BUDGET_BYTES", _bytes(first) + 200)
    result = _sync(registry, names)["result"]
    assert [e["name"] for e in result["operations"]] == ["segment.replace"]
    assert result["omitted"] == names[1:]
    assert "detail" in result["omittedNote"]
    monkeypatch.setattr(server_mod, "CATALOG_RESULT_BUDGET_BYTES", 10)
    result = _sync(registry, names)["result"]
    assert [e["name"] for e in result["operations"]] == ["segment.replace"]
    assert result["omitted"] == names[1:]


def test_flame_catalog_tool_schema_accepts_op_arrays_and_detail(run_dir: Path) -> None:
    async def main() -> None:
        server = create_server(
            make_full_registry(), Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"}), run_dir=run_dir
        )
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                props = tools["flame_catalog"].input_schema["properties"]
                assert props["detail"]["enum"] == ["full", "args"]
                variants = props["op"]["anyOf"]
                assert {v["type"] for v in variants} == {"string", "array"}
                (array,) = [v for v in variants if v["type"] == "array"]
                assert array["maxItems"] == catalog_mod.MAX_CATALOG_OPS
                env = _structured(
                    await session.call_tool(
                        "flame_catalog",
                        {"op": ["segment.info", "project.info"], "detail": "args"},
                    )
                )
                assert env["ok"] is True
                assert [e["name"] for e in env["result"]["operations"]] == [
                    "segment.info",
                    "project.info",
                ]

    anyio.run(main)


def test_op_list_of_only_denied_names_is_policy_denied_with_hint() -> None:
    registry = make_full_registry()
    policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    env = server_mod._flame_catalog_sync(
        registry, policy, None, ["desktop.rename", "desktop.rename"]
    )
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert "desktop.rename" in env["error"]
    assert env["hint"]
    mixed = server_mod._flame_catalog_sync(
        registry, policy, None, ["desktop.rename", "zzz.nothing"]
    )
    assert mixed["ok"] is False and mixed["errorCode"] == "INVALID_ARGS"


def test_op_list_puts_a_policy_denied_eval_run_in_unavailable() -> None:
    registry = make_full_registry()
    assert registry.get("eval.run") is not None
    policy = Policy.from_env({})
    env = server_mod._flame_catalog_sync(registry, policy, None, ["eval.run", "project.info"])
    assert env["ok"] is True
    result = env["result"]
    assert [e["name"] for e in result["operations"]] == ["project.info"]
    assert [(u["name"], u["errorCode"]) for u in result["unavailable"]] == [
        ("eval.run", "POLICY_DENIED")
    ]


def test_op_list_stays_a_list_through_the_compact_response() -> None:
    registry = make_full_registry()
    env = _sync(registry, ["segment.info", "project.info", "desktop.list_children"], "args")
    compacted = compact_mod.compact_envelope(env)
    operations = compacted["result"]["operations"]
    assert isinstance(operations, list)
    assert [e["name"] for e in operations] == [
        "segment.info",
        "project.info",
        "desktop.list_children",
    ]
