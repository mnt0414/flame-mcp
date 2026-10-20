"""Tests the 4 MCP tools through an in-memory MCP client.

The client talks to the real `Server` from `flame_mcp.server`, backed by the real
`flame_mcp_bridge.impl.Bridge` (+ FakeMainLoop) exactly as test_transport.py
exercises it. No real Flame, no real socket beyond the in-process AF_UNIX
bridge socket.

mcp 2.2.0 APIs used here:
  - `mcp.client._memory.InMemoryTransport` -- an in-memory client/server
    transport pair built for exactly this kind of test (its own docstring:
    "In-memory transport for testing MCP servers without network overhead").
  - `mcp.client.session.ClientSession` -- `initialize()`, `list_tools()`,
    `call_tool(name, arguments)`.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, get_args

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from flame_mcp import applog, errors, selectors, transport
from flame_mcp import server as server_mod
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.server import create_server
from flame_mcp.types import Category, OperationParam, OperationSpec, SelectorKind
from flame_mcp_bridge import impl
from test_bridge_impl import FakeFlame, FakeMainLoop, FakeTimer
from test_transport import FakeRawBridge, _close_and_write_crash_handler

SCRATCH_ROOT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "srv"


@pytest.fixture
def scratch_dir() -> Iterator[Path]:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    d = SCRATCH_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def run_dir(scratch_dir: Path) -> Path:
    d = scratch_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(autouse=True)
def _fake_logs(monkeypatch: pytest.MonkeyPatch, scratch_dir: Path) -> None:
    # transport.exec always marks/reads the app.log window; point it at an
    # inert scratch file instead of this machine's real /opt/Autodesk/log.
    monkeypatch.setenv(applog.APP_LOG_ENV, str(scratch_dir / "app.log"))
    monkeypatch.setenv(applog.SHELL_LOG_ENV, str(scratch_dir / "shell.log"))
    monkeypatch.delenv(transport.BRIDGE_SOCKET_ENV, raising=False)


@pytest.fixture
def loop() -> FakeMainLoop:
    return FakeMainLoop()


def _bridge_for_version(
    version: str, run_dir: Path, scratch_dir: Path, loop: FakeMainLoop
) -> Iterator[impl.Bridge]:
    b = impl.Bridge(
        schedule=loop.schedule,
        flame_module=FakeFlame(),
        version=version,
        project_name="proj",
        run_dir=run_dir,
        log_path=scratch_dir / "bridge.log",
        make_timer=lambda: FakeTimer(loop),
    )
    b.start()
    yield b
    try:
        b.stop()
    except Exception:
        pass


@pytest.fixture
def bridge(run_dir: Path, scratch_dir: Path, loop: FakeMainLoop) -> Iterator[impl.Bridge]:
    yield from _bridge_for_version("2025.2.4", run_dir, scratch_dir, loop)


@pytest.fixture
def second_tested_bridge(
    run_dir: Path, scratch_dir: Path, loop: FakeMainLoop
) -> Iterator[impl.Bridge]:
    yield from _bridge_for_version("2026.2.3", run_dir, scratch_dir, loop)


@pytest.fixture
def untested_bridge(run_dir: Path, scratch_dir: Path, loop: FakeMainLoop) -> Iterator[impl.Bridge]:
    yield from _bridge_for_version("2026.1", run_dir, scratch_dir, loop)


def _write_to_python(args: Mapping[str, Any]) -> str:
    value = args["value"]
    return "\n".join(
        [
            "_result = {'previous': getattr(flame, '_thing', None)}",
            f"flame._thing = {value!r}",
            "_affected = 1",
        ]
    )


def _write_verify(args: Mapping[str, Any]) -> str:
    value = args["value"]
    return f"_verified = (getattr(flame, '_thing', None) == {value!r})"


def _list_things_to_python(args: Mapping[str, Any]) -> str:
    # Nested two levels deep on purpose: layer 3's stage 2 only collapses
    # a top-level list or a dict's *immediate* list-valued keys, so this
    # shape survives stage 2 untouched and forces stage 3 (spill) once large
    # enough -- exactly what the limit-note ordering test needs to exercise.
    n = int(args["limit"])
    return (
        "_result = {'outer': {'items': [{'i': i, 'pad': 'x' * 50} for i in range(" + str(n) + ")]}}"
    )


def make_test_registry() -> Registry:
    """One read op and one reversible op, against the real FakeFlame double."""
    registry = Registry(symbols=frozenset())
    registry.register_category(
        Category(
            name="testcat",
            description="test category",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    registry.register(
        OperationSpec(
            name="testcat.read_marker",
            category="testcat",
            description="read the fake flame module's marker attribute",
            effect="read",
            to_python=lambda args: "_result = flame.marker",
        )
    )
    registry.register(
        OperationSpec(
            name="testcat.write_thing",
            category="testcat",
            description="set a value on the fake flame module and verify it stuck",
            effect="reversible",
            params=(
                OperationParam(name="value", type="string", description="new value", required=True),
            ),
            to_python=_write_to_python,
            verify=_write_verify,
            undo_steps=lambda args, affected: affected,
        )
    )
    registry.register(
        OperationSpec(
            name="testcat.list_things",
            category="testcat",
            description="return `limit` small items -- exercises C4 layer 2 (limit clamping)",
            effect="read",
            params=(
                OperationParam(
                    name="limit", type="number", description="how many items", required=False
                ),
            ),
            default_limit=5,
            max_limit=2000,
            to_python=_list_things_to_python,
        )
    )
    return registry


async def _pump_until(loop: FakeMainLoop, event: anyio.Event, timeout: float = 10.0) -> None:
    """Drains FakeMainLoop's queue on *this* task's thread until `event` is set.

    The event loop's own thread is the process main thread under plain `anyio.run`, exactly
    where `impl.Bridge._main_run_exec`'s `threading.main_thread()` check requires it to run.

    Also fires `loop`'s registered `FakeTimer`s each iteration, which is what moves a request
    out of `Bridge._relay_queue` and into `loop._queue` (see test_bridge_impl.py's
    FakeMainLoop/FakeTimer docstrings). This pump is the one place in the test suite that
    drains `loop._queue` itself instead of going through `FakeMainLoop.run_until`/`drain_for`,
    so it needs the same call here too.
    """
    deadline = time.monotonic() + timeout
    while not event.is_set():
        loop._fire_timers()
        try:
            cb = loop._queue.get_nowait()
        except queue.Empty:
            if time.monotonic() > deadline:
                raise TimeoutError("pump timed out waiting for the call to finish") from None
            await anyio.sleep(0.005)
            continue
        cb()
    while True:
        loop._fire_timers()
        try:
            cb = loop._queue.get_nowait()
        except queue.Empty:
            return
        cb()


async def _call_tool(
    session: ClientSession, loop: FakeMainLoop, name: str, arguments: dict[str, Any] | None = None
) -> Any:
    done = anyio.Event()
    box: dict[str, Any] = {}

    async def _do() -> None:
        box["result"] = await session.call_tool(name, arguments)
        done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_do)
        await _pump_until(loop, done)
    return box["result"]


@asynccontextmanager
async def _connected_session(
    registry: Registry, policy: Policy, run_dir: Path
) -> AsyncIterator[ClientSession]:
    server = create_server(registry, policy, run_dir=run_dir)
    async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _structured(result: Any) -> dict[str, Any]:
    sc = result.structured_content
    assert isinstance(sc, dict)
    return sc


def test_list_tools_has_exactly_the_4_dedicated_tools(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {
                "flame_version_info",
                "flame_catalog",
                "flame_do",
                "flame_context",
            }

    anyio.run(main)


def test_tool_annotations_mark_the_three_readers_read_only_and_flame_do_destructive(
    run_dir: Path,
) -> None:
    async def main() -> None:
        async with _connected_session(make_test_registry(), Policy.from_env({}), run_dir) as s:
            tools = {tool.name: tool for tool in (await s.list_tools()).tools}
            for name in ("flame_version_info", "flame_catalog", "flame_context"):
                annotations = tools[name].annotations
                assert annotations is not None and annotations.read_only_hint is True, name
            do_annotations = tools["flame_do"].annotations
            assert do_annotations is not None
            assert do_annotations.read_only_hint is False
            assert do_annotations.destructive_hint is True

    anyio.run(main)


def test_flame_do_input_schema_enums_visible_op_names(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            listed = await session.list_tools()
            do_tool = next(t for t in listed.tools if t.name == "flame_do")
            op_schema = do_tool.input_schema["properties"]["op"]
            assert set(op_schema["enum"]) == {
                "testcat.read_marker",
                "testcat.write_thing",
                "testcat.list_things",
            }

    anyio.run(main)


def test_flame_version_info_reports_bridge_and_op_counts(
    bridge: impl.Bridge, run_dir: Path
) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_version_info", {})
            env = _structured(result)
            assert env["ok"] is True
            assert result.is_error is False
            assert env["result"]["registeredOps"] == 3
            assert env["result"]["visibleOps"] == 3
            assert env["result"]["bridge"]["reachable"] is True
            assert env["result"]["bridge"]["pid"] == os.getpid()

    anyio.run(main)


def test_flame_version_info_flags_untested_flame_version(
    untested_bridge: impl.Bridge, run_dir: Path
) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_version_info", {})
            env = _structured(result)
            assert env["result"]["bridge"]["reachable"] is True
            assert env["result"]["bridge"]["flameVersion"] == "2026.1"
            assert env["result"]["bridge"]["untestedFlameVersion"] is True
            assert any(
                "2026.1" in n
                and "not a tested version" in n
                and "(tested: 2025.2.4, 2026.2.3)" in n
                for n in env["notices"]
            )

    anyio.run(main)


def test_flame_version_info_does_not_flag_tested_flame_version(
    bridge: impl.Bridge, run_dir: Path
) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_version_info", {})
            env = _structured(result)
            assert env["result"]["bridge"]["flameVersion"] == "2025.2.4"
            assert env["result"]["bridge"]["untestedFlameVersion"] is False
            assert not any("not a tested version" in n for n in env["notices"])

    anyio.run(main)


def test_flame_version_info_does_not_flag_the_second_tested_flame_version(
    second_tested_bridge: impl.Bridge, run_dir: Path
) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_version_info", {})
            env = _structured(result)
            assert env["result"]["bridge"]["flameVersion"] == "2026.2.3"
            assert env["result"]["bridge"]["untestedFlameVersion"] is False
            assert not any("not a tested version" in n for n in env["notices"])

    anyio.run(main)


def test_flame_version_info_reports_unreachable_bridge_error_code(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_version_info", {})
            env = _structured(result)
            # the tool itself succeeds; only the nested bridge status fails
            assert env["ok"] is True
            assert env["result"]["bridge"]["reachable"] is False
            assert env["result"]["bridge"]["errorCode"] == "BRIDGE_UNAVAILABLE"

    anyio.run(main)


def test_flame_version_info_reports_empty_inflight_by_default(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_version_info", {})
            env = _structured(result)
            assert env["result"]["inflight"] == {"count": 0, "newest": None}

    anyio.run(main)


def test_flame_version_info_reports_leftover_inflight_record(run_dir: Path) -> None:
    """Reports an inflight record left behind by a crashed session.

    A record left behind by a session that crashed before this server ever
    observed BRIDGE_LOST must still be visible here."""

    async def main() -> None:
        d = run_dir / "inflight"
        d.mkdir(parents=True, exist_ok=True)
        (d / "abc123.json").write_text(
            json.dumps(
                {
                    "id": "abc123",
                    "label": "readback_name",
                    "thread": "main",
                    "timeoutS": 10.0,
                    "sentAt": "2026-09-28T10:52:35+00:00",
                    "socket": str(run_dir / "flame-mcp-2025.2.4.sock"),
                    "bridgePid": 4242,
                    "codeSha256": "deadbeef",
                    "code": "_result = 1",
                }
            ),
            encoding="utf-8",
        )
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_version_info", {})
            env = _structured(result)
            assert env["result"]["inflight"] == {
                "count": 1,
                "newest": {
                    "id": "abc123",
                    "label": "readback_name",
                    "sentAt": "2026-09-28T10:52:35+00:00",
                },
            }

    anyio.run(main)


def test_flame_catalog_lists_all_visible_ops_with_details(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_catalog", {})
            env = _structured(result)
            assert env["ok"] is True
            names = {op["name"] for op in env["result"]["operations"]}
            assert names == {"testcat.read_marker", "testcat.write_thing", "testcat.list_things"}
            assert "testcat" in env["result"]["categories"]
            write_entry = next(
                op for op in env["result"]["operations"] if op["name"] == "testcat.write_thing"
            )
            assert write_entry["effect"] == "reversible"
            assert write_entry["undo"] == "value"
            assert write_entry["params"] == ["value*"]
            assert "thread" not in write_entry and "description" not in write_entry
            described = _structured(
                await session.call_tool("flame_catalog", {"op": "testcat.write_thing"})
            )
            assert described["result"]["operation"]["params"]["details"][0]["name"] == "value"

    anyio.run(main)


def test_flame_catalog_by_op_name(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_catalog", {"op": "testcat.read_marker"})
            env = _structured(result)
            assert env["ok"] is True
            assert env["result"]["operation"]["name"] == "testcat.read_marker"

    anyio.run(main)


def test_flame_catalog_unknown_op_name(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_catalog", {"op": "testcat.read_markerx"})
            env = _structured(result)
            assert env["ok"] is False
            assert env["errorCode"] == "UNKNOWN_OPERATION"
            assert "read_marker" in env["error"]
            assert result.is_error is True

    anyio.run(main)


def test_flame_catalog_unknown_category_is_policy_denied(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": "othercat"})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_catalog", {"category": "testcat"})
            env = _structured(result)
            assert env["ok"] is False
            assert env["errorCode"] == "POLICY_DENIED"

    anyio.run(main)


def test_flame_do_read_op_success(bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await _call_tool(session, loop, "flame_do", {"op": "testcat.read_marker"})
            env = _structured(result)
            assert env["ok"] is True
            assert env["result"] == "fake-flame"
            assert env["affected"] == 0
            assert env["execution"]["thread"] == "main"
            assert result.is_error is False

    anyio.run(main)


def test_flame_do_write_op_success_and_undo_steps(
    bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path
) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await _call_tool(
                session, loop, "flame_do", {"op": "testcat.write_thing", "args": {"value": "hello"}}
            )
            env = _structured(result)
            assert env["ok"] is True
            assert env["affected"] == 1
            assert env["undoSteps"] == 1
            # FakeFlame has no .projects/.media_panel: every rider field must
            # fail independently (None), never raise out of the wrapper.
            assert env["context"] == {"project": None, "workspace": None, "selected": None}

    anyio.run(main)


def test_flame_do_unknown_operation(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_do", {"op": "no.such.op"})
            env = _structured(result)
            assert env["ok"] is False
            assert env["errorCode"] == "UNKNOWN_OPERATION"
            assert result.is_error is True

    anyio.run(main)


def test_flame_do_invalid_args(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_do", {"op": "testcat.write_thing", "args": {}})
            env = _structured(result)
            assert env["ok"] is False
            assert env["errorCode"] == "INVALID_ARGS"

    anyio.run(main)


def test_flame_do_bridge_unreachable_is_bridge_unavailable(run_dir: Path) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_do", {"op": "testcat.read_marker"})
            env = _structured(result)
            assert env["ok"] is False
            assert env["errorCode"] == "BRIDGE_UNAVAILABLE"
            assert env["context"] is None

    anyio.run(main)


def test_flame_do_limit_below_1_is_clamped_to_1(
    bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path
) -> None:
    # 0 (or negative) is not "no limit" -- it's raised to the minimum of 1,
    # and that clamp is recorded.
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await _call_tool(
                session, loop, "flame_do", {"op": "testcat.list_things", "args": {"limit": 0}}
            )
            env = _structured(result)
            assert env["ok"] is True
            assert env["result"] == {"outer": {"items": [{"i": 0, "pad": "x" * 50}]}}
            assert any("raised to 1" in note for note in env["degraded"])

    anyio.run(main)


def test_flame_do_limit_note_precedes_and_survives_spill(
    bridge: impl.Bridge,
    loop: FakeMainLoop,
    run_dir: Path,
    scratch_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The limit clamp is known before execution.run_op ever
    # runs, so it must sit before any layer-3 (byte-budget) note in
    # `degraded` -- including inside a spilled file, when the response is
    # large enough to be spilled to disk rather than degraded in place.
    # testcat.list_things nests its list two levels deep (outer.items), so
    # layer-3 stage 2 (which only collapses a top-level list or a dict's
    # *immediate* list-valued keys) cannot shrink it -- requesting well past
    # max_limit=2000 both forces the clamp note and (2000 padded items,
    # comfortably over the 60,000-byte budget) forces stage 3's spill.
    spill_dir = scratch_dir / "spill"
    monkeypatch.setenv("FLAME_MCP_SPILL_DIR", str(spill_dir))

    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await _call_tool(
                session,
                loop,
                "flame_do",
                {"op": "testcat.list_things", "args": {"limit": 999999}},
            )
            env = _structured(result)
            assert env["ok"] is True
            assert len(env["degraded"]) >= 2, env["degraded"]
            assert "clamped to max_limit=2000" in env["degraded"][0]
            assert any("spilled" in note for note in env["degraded"][1:])
            assert isinstance(env["result"], dict) and "spilledTo" in env["result"]

            spill_path = Path(env["result"]["spilledTo"])
            assert spill_path.parent == spill_dir
            written = json.loads(spill_path.read_text())
            # the limit note must have been written to disk too, in the same
            # position -- not just present in the in-memory envelope. The
            # nested list itself survives stage 2 untouched (that's what
            # forced the spill), so the full 2000 items are on disk too.
            assert "clamped to max_limit=2000" in written["degraded"][0]
            assert len(written["result"]["outer"]["items"]) == 2000

    anyio.run(main)


def test_static_context_undo_contract_warns_about_set_value() -> None:
    # Writes through set_value() (desktop.rename and others) are not covered by Ctrl+Z.
    # Check that the static context warns about this, explains what undoSteps==0 means,
    # and says how to undo with revert (`_static_context` needs no bridge).
    registry = make_test_registry()
    ctx = server_mod._static_context(registry)
    assert "set_value" in ctx["undoContract"]
    assert "revert" in ctx["undoContract"]
    assert "undoSteps" in ctx["undoContract"]


def test_static_context_undo_contract_claims_only_what_was_measured() -> None:
    # Measured: Ctrl+Z after a set_value write removed an earlier, unrelated operation, on a
    # reel's name on the Desktop and on a Batch node attribute -- nothing wider. The instruction
    # to the AI (no Ctrl+Z, journal.revert only) stays whole.
    ctx = server_mod._static_context(make_test_registry())
    contract = ctx["undoContract"]
    assert "Do not press Ctrl+Z" in contract
    assert "journal.revert" in contract
    assert "may not be put on its history" in contract
    assert "reel" in contract and "Batch node attribute" in contract
    assert "every *.set_attributes" not in contract and "desktop.rename and every" not in contract
    assert "are not put on its history" not in contract


def test_static_context_set_value_pitfall_claims_only_what_was_measured() -> None:
    pitfalls = [p["text"] for p in server_mod._static_context(make_test_registry())["pitfalls"]]
    (text,) = [t for t in pitfalls if "set_value" in t]
    assert "may not" in text and "reel" in text and "Batch node attribute" in text
    assert "Ctrl+Z" in text and "journal.revert" in text
    assert "is not pushed" not in text


def test_static_context_has_a_pitfall_about_the_bit_depth_resize() -> None:
    pitfalls = [p["text"] for p in server_mod._static_context(make_test_registry())["pitfalls"]]
    (text,) = [t for t in pitfalls if "bit depth" in t and "timeline.create_sequence" in t]
    assert "Resize" in text and "automatically" in text
    assert "cannot be deleted" not in text
    assert "On Flame 2026.2.3, Flame refused to delete that Resize" in text
    assert "every version" in text and "for safety" in text
    assert "bit_depth" in text and "8, 10, 12, 16 or 32" in text


def test_static_context_smart_replace_pitfall_says_the_automatic_resize_stays() -> None:
    pitfalls = [p["text"] for p in server_mod._static_context(make_test_registry())["pitfalls"]]
    (text,) = [t for t in pitfalls if "smart_replace (segment.replace" in t]
    assert "drops the segment's effects" in text and "mode=media" in text
    assert "Resize" in text and "stays" in text and "Flame 2026.2.3" in text


def test_static_context_has_the_measured_audio_pitfalls() -> None:
    pitfalls = [p["text"] for p in server_mod._static_context(make_test_registry())["pitfalls"]]
    (reach,) = [t for t in pitfalls if "audio_tracks[].channels[].segments" in t]
    assert "PyAudioTrack has no .segments" in reach and "video tracks only" in reach
    assert "timeline.list_segments with media='audio'" in reach
    (crash,) = [t for t in pitfalls if "smart_replace_media on an audio segment" in t]
    assert "crashed Flame" in crash and "signal 11" in crash and "2025.2.4" in crash
    assert "timeline.overwrite" in crash and "clip that has audio" in crash
    (gap,) = [t for t in pitfalls if "trim_tail on an audio segment" in t]
    assert "into a Gap" in gap and "ripple True" in gap and "ripple False" in gap
    assert "cut the audio and Gap tracks at the end frame and extract the tail pieces" in gap
    assert "timeline.shorten does that" in gap
    (silent,) = [t for t in pitfalls if "replaced the audio of the sequence" in t]
    assert "same source" in silent and "result.audio" in silent and "SILENT_NOOP" in silent
    assert "journal.changed" in silent


def test_static_context_has_a_pitfall_about_moving_tracks() -> None:
    pitfalls = [p["text"] for p in server_mod._static_context(make_test_registry())["pitfalls"]]
    (text,) = [t for t in pitfalls if "reorders a track" in t]
    assert "timeline.move_track" in text and "rebuilds" in text and "2025.2.4" in text
    assert "measured by listing them" in text
    assert "No method to move or reorder audio tracks was found" in text
    assert "left the audio rows as they were" in text


def test_static_context_has_the_pitfall_about_the_length_an_extract_leaves() -> None:
    pitfalls = [p["text"] for p in server_mod._static_context(make_test_registry())["pitfalls"]]
    (text,) = [t for t in pitfalls if "extract_selection_to_media_panel" in t]
    assert "timeline.delete_segments" in text and "timeline.shorten" in text
    assert "longest content of ANY video track or audio channel" in text
    assert "also on tracks that were not selected" in text
    assert "a track that ends in content at the new end has no Gap" in text
    assert "keeps its length" not in text and "(Flame 2025.2.4). Deleting the last content" in text


def test_the_checkpoint_sentence_lists_the_ops_without_a_doubled_and() -> None:
    contract = server_mod._static_context(make_test_registry())["undoContract"]
    assert (
        "effect_load_setup, timeline.delete_segments, timeline.move_track and timeline.shorten)"
        in contract
    )
    assert "effect_load_setup and timeline.delete_segments and" not in contract


def test_static_context_has_no_depth_ladder() -> None:
    # No op declares a default depth and none takes a depth argument.
    assert "depthLadder" not in server_mod._static_context(make_test_registry())


def test_static_context_selector_vocabulary_publishes_fields_and_examples() -> None:
    # Each kind's shape and a minimal valid example are published, so the fields the segment
    # selector takes (parent/version/track/index) are discoverable without an error message.
    ctx = server_mod._static_context(make_test_registry())
    vocab = {entry["kind"]: entry for entry in ctx["selectorVocabulary"]}
    assert set(vocab) == set(get_args(SelectorKind))
    segment = vocab["segment"]
    assert set(segment["fields"]) == {
        "parent",
        "version",
        "track",
        "index",
        "record_in",
        "expect_source_name",
    }
    assert segment["fields"]["parent"]["type"] == "selector"
    assert segment["fields"]["index"]["type"] == "non-negative integer"
    assert segment["fields"]["record_in"]["required"] is False
    assert all(
        field["description"] for entry in vocab.values() for field in entry["fields"].values()
    )
    assert vocab["selection"]["fields"] == {}
    for entry in vocab.values():
        assert selectors.validate_selector(entry["example"], get_args(SelectorKind)) == []


def test_instructions_stay_within_2_kib_and_name_the_core_rules() -> None:
    size = len(server_mod.INSTRUCTIONS.encode("utf-8"))
    assert 0 < size <= server_mod.MAX_INSTRUCTIONS_BYTES == 2048
    for needle in ("flame_catalog", "journal.revert", "journal.check", "SILENT_NOOP", "pitfalls"):
        assert needle in server_mod.INSTRUCTIONS


def test_instructions_tell_the_model_to_ask_the_user_before_irreversible_operations() -> None:
    text = server_mod.INSTRUCTIONS
    for needle in (
        "render.batch",
        "overwrite=true",
        "journal.purge_checkpoints",
        "desktop.delete",
        "desktop.reformat",
        "force=true",
        "eval.run",
        "ask the user",
    ):
        assert needle in text, needle


def test_instructions_reach_the_client_on_initialize(run_dir: Path) -> None:
    async def main() -> None:
        server = create_server(make_test_registry(), Policy.from_env({}), run_dir=run_dir)
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.instructions == server_mod.INSTRUCTIONS

    anyio.run(main)


def test_static_context_pitfalls_each_carry_only_text() -> None:
    pitfalls = server_mod._static_context(make_test_registry())["pitfalls"]
    assert len(pitfalls) >= 8
    for pitfall in pitfalls:
        assert set(pitfall) == {"text"}
        assert pitfall["text"].strip()


def test_flame_context_description_mentions_pitfalls(run_dir: Path) -> None:
    async def main() -> None:
        async with _connected_session(
            make_test_registry(), Policy.from_env({}), run_dir
        ) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert "pitfalls" in (tools["flame_context"].description or "")

    anyio.run(main)


def test_flame_context_reports_live_state_and_static_knowledge(
    bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path
) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await _call_tool(session, loop, "flame_context", {})
            env = _structured(result)
            assert env["ok"] is True
            assert env["result"]["undoContract"]
            assert env["result"]["pitfalls"]
            assert env["result"]["threadContract"]
            assert "depthLadder" not in env["result"]
            assert env["result"]["defaultLimit"] > 0
            assert "testcat" in env["result"]["categories"]
            # FakeFlame has no .projects/.media_panel/.get_current_tab -- every
            # expensive/rider field must fail independently, not blow up the call.
            assert env["result"]["libraries"] is None
            assert env["result"]["currentTab"] is None
            assert env["context"] == {"project": None, "workspace": None, "selected": None}

    anyio.run(main)


def test_flame_context_bridge_unavailable_is_ok_false_with_static_result(run_dir: Path) -> None:
    # A bridge-level failure must not hide behind
    # ok=true/degraded -- it gets the normal ok=false/errorCode/hint/
    # retryable treatment, same as flame_do. The static knowledge (which
    # needs no bridge at all) is still readable in `result`.
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({})
        async with _connected_session(registry, policy, run_dir) as session:
            result = await session.call_tool("flame_context", {})
            env = _structured(result)
            assert env["ok"] is False
            assert env["errorCode"] == "BRIDGE_UNAVAILABLE"
            assert env["hint"] == errors.hint_for("BRIDGE_UNAVAILABLE")
            assert env["retryable"] is True
            assert env["context"] is None
            assert env["crash"] is None
            # the static half of flame_context needs no bridge at all
            assert env["result"]["undoContract"]
            assert env["result"]["threadContract"]
            assert "testcat" in env["result"]["categories"]
            assert "libraries" not in env["result"]
            assert "currentTab" not in env["result"]

    anyio.run(main)


def test_flame_context_bridge_lost_is_ok_false_with_crash(run_dir: Path, scratch_dir: Path) -> None:
    # Same contract, but for BRIDGE_LOST specifically -- Flame may
    # have just crashed, so `crash` must come through, not get swallowed.
    shell_log_path = scratch_dir / "shell.log"
    shell_log_path.write_text("")
    fake_bridge = FakeRawBridge(
        run_dir / "flame-mcp-fake.sock", _close_and_write_crash_handler(shell_log_path)
    )
    try:

        async def main() -> None:
            registry = make_test_registry()
            policy = Policy.from_env({})
            async with _connected_session(registry, policy, run_dir) as session:
                result = await session.call_tool("flame_context", {})
                env = _structured(result)
                assert env["ok"] is False
                assert env["errorCode"] == "BRIDGE_LOST"
                assert env["hint"] == errors.hint_for("BRIDGE_LOST")
                assert env["crash"] is not None
                assert any(
                    "abnormal termination" in line for line in env["crash"]["shellCrashLines"]
                )
                assert env["result"]["undoContract"]  # static half survives the crash too

        anyio.run(main)
    finally:
        fake_bridge.close()


def test_readonly_denies_write_op_in_all_3_places(
    bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path
) -> None:
    async def main() -> None:
        registry = make_test_registry()
        policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
        async with _connected_session(registry, policy, run_dir) as session:
            # 1. tool registration: flame_do's op enum must not offer the write op
            listed = await session.list_tools()
            do_tool = next(t for t in listed.tools if t.name == "flame_do")
            enum_names = set(do_tool.input_schema["properties"]["op"]["enum"])
            assert "testcat.write_thing" not in enum_names
            assert "testcat.read_marker" in enum_names

            # 2. flame_catalog: visible_ops must not list the write op
            catalog_result = await session.call_tool("flame_catalog", {})
            catalog_env = _structured(catalog_result)
            catalog_names = {op["name"] for op in catalog_env["result"]["operations"]}
            assert "testcat.write_thing" not in catalog_names
            assert "testcat.read_marker" in catalog_names

            # 3. flame_do: calling the write op directly by name must be denied
            do_result = await session.call_tool(
                "flame_do", {"op": "testcat.write_thing", "args": {"value": "x"}}
            )
            do_env = _structured(do_result)
            assert do_env["ok"] is False
            assert do_env["errorCode"] == "POLICY_DENIED"

            # the read op must pass all 3 checkpoints
            read_result = await _call_tool(session, loop, "flame_do", {"op": "testcat.read_marker"})
            read_env = _structured(read_result)
            assert read_env["ok"] is True

    anyio.run(main)


def test_allow_categories_denies_the_excluded_category_in_all_3_places(
    bridge: impl.Bridge, loop: FakeMainLoop, run_dir: Path
) -> None:
    async def main() -> None:
        registry = make_test_registry()
        registry.register_category(
            Category(
                name="othercat",
                description="another category",
                default_thread="main",
                default_pacing="none",
                allowed_thread=frozenset({"main"}),
                allowed_pacing=frozenset({"none"}),
            )
        )
        registry.register(
            OperationSpec(
                name="othercat.read_other",
                category="othercat",
                description="read op in a category that will be excluded",
                effect="read",
                to_python=lambda args: "_result = 'other'",
            )
        )
        policy = Policy.from_env({"FLAME_MCP_ALLOW_CATEGORIES": "testcat"})
        async with _connected_session(registry, policy, run_dir) as session:
            listed = await session.list_tools()
            do_tool = next(t for t in listed.tools if t.name == "flame_do")
            enum_names = set(do_tool.input_schema["properties"]["op"]["enum"])
            assert "othercat.read_other" not in enum_names
            assert "testcat.read_marker" in enum_names

            catalog_result = await session.call_tool("flame_catalog", {})
            catalog_env = _structured(catalog_result)
            catalog_names = {op["name"] for op in catalog_env["result"]["operations"]}
            assert "othercat.read_other" not in catalog_names

            denied_result = await session.call_tool("flame_do", {"op": "othercat.read_other"})
            denied_env = _structured(denied_result)
            assert denied_env["ok"] is False
            assert denied_env["errorCode"] == "POLICY_DENIED"

            allowed_result = await _call_tool(
                session, loop, "flame_do", {"op": "testcat.read_marker"}
            )
            allowed_env = _structured(allowed_result)
            assert allowed_env["ok"] is True

    anyio.run(main)
