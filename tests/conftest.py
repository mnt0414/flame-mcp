"""Every test gets its own journal and spill directories: nothing here may write to, or sweep,
the real `~/Library/Application Support/flame-mcp/journal/` or `spill/` (a `Journal()` built
without an explicit directory reads FLAME_MCP_JOURNAL_DIR, and the startup sweep of old spill
files reads FLAME_MCP_SPILL_DIR)."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from flame_mcp import envelope, setup_work
from flame_mcp import journal as journal_mod
from flame_mcp.ops import register_all
from flame_mcp.registry import Registry


@pytest.fixture(autouse=True)
def _isolated_journal_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(journal_mod.JOURNAL_DIR_ENV, str(tmp_path / "journal"))
    monkeypatch.setenv(envelope.SPILL_DIR_ENV, str(tmp_path / "spill"))


@pytest.fixture(autouse=True)
def _no_e2e_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The e2e switches are set by the test that needs them, never by the shell running pytest."""
    monkeypatch.delenv("FLAME_MCP_E2E_FLAME_VERSION", raising=False)
    monkeypatch.delenv("FLAME_MCP_E2E_PROJECT", raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_setup_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """`setup_work` keeps the journal directory of the last `Runtime` made: a test that makes none
    must not pick up the one an earlier test left."""
    monkeypatch.setattr(setup_work, "_journal_directory", None)


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


@cache
def shared_registry() -> Registry:
    """The registry of every public op, built once for the whole run: for a test that only reads
    it. A test that registers into a registry or changes one makes its own with `make_registry`."""
    return make_registry()


@pytest.fixture(scope="module")
def registry() -> Registry:
    return make_registry()
