"""Tests the environment the e2e clients give the `python -m flame_mcp` servers they start.

eval.run exists only on a server with FLAME_MCP_ENABLE_EVAL=1, so the clients' servers turn it on
(the catalog checks compare against every registered op), except a READONLY server, which is
never given it, and a server a client asks to start without it.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

journal_e2e = importlib.import_module("p2_2a_journal_e2e")
mcp_e2e = importlib.import_module("p1_mcp_e2e")

ENABLE_EVAL = "FLAME_MCP_ENABLE_EVAL"


class FakeSession:
    def __init__(self, read: Any, write: Any) -> None:
        pass

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def initialize(self) -> None:
        return None


def capture_env(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    @asynccontextmanager
    async def fake_stdio_client(params: Any) -> AsyncIterator[tuple[None, None]]:
        seen.append(dict(params.env))
        yield None, None

    monkeypatch.setattr(module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(module, "ClientSession", FakeSession)
    return seen


def journal_ctx(tmp_path: Path) -> Any:
    return journal_e2e.Ctx(tmp_path / "run", {"steps": [], "ok": True}, "ts", tmp_path / "journal")


def run_journal_session(tmp_path: Path, **kwargs: Any) -> None:
    async def go() -> None:
        async with journal_e2e.server_session(journal_ctx(tmp_path), "t", **kwargs):
            pass

    anyio.run(go)


def run_mcp_session(tmp_path: Path, **kwargs: Any) -> None:
    async def go() -> None:
        async with mcp_e2e.mcp_session(tmp_path, **kwargs):
            pass

    anyio.run(go)


def test_journal_server_gets_the_eval_switch_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENABLE_EVAL, raising=False)
    seen = capture_env(journal_e2e, monkeypatch)

    run_journal_session(tmp_path)

    assert seen[0][ENABLE_EVAL] == "1"
    assert "FLAME_MCP_READONLY" not in seen[0]


def test_journal_server_can_be_started_without_the_eval_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENABLE_EVAL, "1")
    seen = capture_env(journal_e2e, monkeypatch)

    run_journal_session(tmp_path, eval_enabled=False)

    assert ENABLE_EVAL not in seen[0]


def test_journal_readonly_server_never_gets_the_eval_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENABLE_EVAL, "1")
    seen = capture_env(journal_e2e, monkeypatch)

    run_journal_session(tmp_path, readonly=True)

    assert ENABLE_EVAL not in seen[0]
    assert seen[0]["FLAME_MCP_READONLY"] == "1"


def test_the_recorded_server_says_whether_eval_was_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture_env(journal_e2e, monkeypatch)
    ctx = journal_ctx(tmp_path)

    async def go() -> None:
        async with journal_e2e.server_session(ctx, "on"):
            pass
        async with journal_e2e.server_session(ctx, "off", eval_enabled=False):
            pass
        async with journal_e2e.server_session(ctx, "ro", readonly=True):
            pass

    anyio.run(go)

    assert [s["evalEnabled"] for s in ctx.results["servers"]] == [True, False, False]


def test_mcp_server_gets_the_eval_switch_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENABLE_EVAL, raising=False)
    seen = capture_env(mcp_e2e, monkeypatch)

    run_mcp_session(tmp_path)

    assert seen[0][ENABLE_EVAL] == "1"


def test_mcp_readonly_server_never_gets_the_eval_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENABLE_EVAL, "1")
    seen = capture_env(mcp_e2e, monkeypatch)

    run_mcp_session(tmp_path, readonly=True)

    assert ENABLE_EVAL not in seen[0]


def test_mcp_server_can_be_started_without_the_eval_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENABLE_EVAL, "1")
    seen = capture_env(mcp_e2e, monkeypatch)

    run_mcp_session(tmp_path, eval_enabled=False)

    assert ENABLE_EVAL not in seen[0]
