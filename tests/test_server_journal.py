"""The journal through the MCP surface: `flame_do` on server-side ops, the `journal` section of
flame_version_info, and the startup wiring in `__main__.build_server`."""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from flame_mcp import __main__ as main_mod
from flame_mcp import applog, transport
from flame_mcp.journal import JOURNAL_DIR_ENV, JOURNAL_MAX_AGE_S, Journal, now_iso
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.runtime import TestHooks
from flame_mcp.server import create_server


@pytest.fixture(autouse=True)
def _inert_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(applog.APP_LOG_ENV, str(tmp_path / "app.log"))
    monkeypatch.setenv(applog.SHELL_LOG_ENV, str(tmp_path / "shell.log"))
    monkeypatch.delenv(transport.BRIDGE_SOCKET_ENV, raising=False)


def structured(result: Any) -> dict[str, Any]:
    sc = result.structured_content
    assert isinstance(sc, dict)
    return sc


def open_intent_record(group: str = "g_old") -> dict[str, Any]:
    return {
        "type": "intent",
        "id": "i_old",
        "group": group,
        "ts": now_iso(),
        "op": "node.set_attributes",
        "args": {},
        "target": {},
        "before": {"a": 1},
        "after": {"a": 2},
    }


async def _run(
    tmp_path: Path,
    body: Any,
    *,
    journal: Journal | None = None,
    hooks: TestHooks | None = None,
) -> None:
    registry = Registry()
    register_all(registry)
    server = create_server(
        registry,
        Policy.from_env({}),
        run_dir=tmp_path / "run",
        journal=journal if journal is not None else Journal(tmp_path / "journal"),
        hooks=hooks,
    )
    async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await body(session)


def test_flame_do_runs_a_server_side_op_without_a_bridge(tmp_path: Path) -> None:
    async def body(session: ClientSession) -> None:
        env = structured(await session.call_tool("flame_do", {"op": "journal.list"}))
        assert env["ok"] is True
        assert env["result"]["total"] == 0
        assert env["execution"]["thread"] == "server" and env["execution"]["op"] == "journal.list"
        assert env["journal"] is None and env["context"] is None

    anyio.run(_run, tmp_path, body)


def test_flame_do_validates_server_op_arguments_like_any_other(tmp_path: Path) -> None:
    async def body(session: ClientSession) -> None:
        env = structured(await session.call_tool("flame_do", {"op": "journal.show", "args": {}}))
        assert env["errorCode"] == "INVALID_ARGS" and "group" in env["error"]
        env = structured(
            await session.call_tool("flame_do", {"op": "journal.show", "args": {"group": ""}})
        )
        assert env["errorCode"] == "INVALID_ARGS"

    anyio.run(_run, tmp_path, body)


def test_the_catalog_lists_the_journal_ops_with_their_thread_and_effect(tmp_path: Path) -> None:
    async def body(session: ClientSession) -> None:
        env = structured(await session.call_tool("flame_catalog", {"category": "journal"}))
        ops = {op["name"]: op for op in env["result"]["operations"]}
        assert set(ops) == {
            "journal.list",
            "journal.show",
            "journal.check",
            "journal.revert",
            "journal.poll",
            "journal.inflight",
            "journal.restore_checkpoint",
            "journal.list_checkpoints",
            "journal.purge_checkpoints",
        }
        threads = set()
        for name in ops:
            described = structured(await session.call_tool("flame_catalog", {"op": name}))
            threads.add(described["result"]["operation"]["thread"])
        assert threads == {"server"}
        assert ops["journal.revert"]["effect"] == "reversible"
        assert ops["journal.check"]["effect"] == "read"
        assert ops["journal.poll"]["effect"] == "read"
        assert ops["journal.inflight"]["effect"] == "read"

    anyio.run(_run, tmp_path, body)


def test_version_info_has_a_journal_section(tmp_path: Path) -> None:
    async def body(session: ClientSession) -> None:
        env = structured(await session.call_tool("flame_version_info", {}))
        journal = env["result"]["journal"]
        assert journal["dir"] == str(tmp_path / "journal")
        assert journal["unresolvedIntents"] == 0 and journal["writesBlocked"] is False
        assert journal["testHooks"] == [] and journal["blocked"] == []

    anyio.run(_run, tmp_path, body)


def test_version_info_counts_intents_that_were_never_decided(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal")
    journal.append(open_intent_record("g_a"))
    other = open_intent_record("g_b")
    other["id"] = "i_other"
    journal.append(other)
    journal.append(
        {"type": "outcome", "id": "i_other", "group": "g_b", "ok": True, "applied": ["a"]}
    )

    async def body(session: ClientSession) -> None:
        env = structured(await session.call_tool("flame_version_info", {}))
        assert env["result"]["journal"]["unresolvedIntents"] == 1

    anyio.run(_run, tmp_path, body)


def test_version_info_shows_test_hooks_only_when_they_are_on(tmp_path: Path) -> None:
    async def body(session: ClientSession) -> None:
        env = structured(await session.call_tool("flame_version_info", {}))
        assert env["result"]["journal"]["testHooks"] == ["FLAME_MCP_TEST_EXIT_AFTER_WRITE=1"]

    anyio.run(partial(_run, tmp_path, body, hooks=TestHooks(exit_after_write=True)))


def test_build_server_sweeps_journal_files_older_than_30_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "journal"
    directory.mkdir()
    stale = directory / "journal_20240101.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    old = stale.stat().st_mtime - JOURNAL_MAX_AGE_S - 3600
    os.utime(stale, (old, old))
    keep = directory / "journal_20990101.jsonl"
    keep.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv(JOURNAL_DIR_ENV, str(directory))
    for name in list(os.environ):
        if name.startswith("FLAME_MCP_TEST_"):
            monkeypatch.delenv(name)
    main_mod.build_server(run_dir=tmp_path / "run")
    assert not stale.exists() and keep.exists()


def test_build_server_has_no_test_hooks_unless_the_environment_sets_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in list(os.environ):
        if name.startswith("FLAME_MCP_TEST_"):
            monkeypatch.delenv(name)
    main_mod.build_server(run_dir=tmp_path / "run")
    assert "test hooks" not in capsys.readouterr().err
    monkeypatch.setenv("FLAME_MCP_TEST_EXIT_AFTER_WRITE", "1")
    main_mod.build_server(run_dir=tmp_path / "run")
    assert "test hooks are ON" in capsys.readouterr().err


def test_build_server_refuses_a_malformed_hook_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLAME_MCP_TEST_PAUSE_AFTER_PREREAD", "soon")
    with pytest.raises(SystemExit):
        main_mod.build_server(run_dir=tmp_path / "run")


def test_the_production_entry_script_never_sets_a_test_hook() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "flame_mcp_server.sh"
    text = script.read_text(encoding="utf-8")
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("FLAME_MCP_TEST_" in ln for ln in code_lines)
    assert "FLAME_MCP_TEST_" in text  # the comment that says never to set them
