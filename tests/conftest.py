"""Every test gets its own journal and spill directories: nothing here may write to, or sweep,
the real `~/Library/Application Support/flame-mcp/journal/` or `spill/` (a `Journal()` built
without an explicit directory reads FLAME_MCP_JOURNAL_DIR, and the startup sweep of old spill
files reads FLAME_MCP_SPILL_DIR)."""

from __future__ import annotations

from pathlib import Path

import pytest

from flame_mcp import envelope
from flame_mcp import journal as journal_mod


@pytest.fixture(autouse=True)
def _isolated_journal_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(journal_mod.JOURNAL_DIR_ENV, str(tmp_path / "journal"))
    monkeypatch.setenv(envelope.SPILL_DIR_ENV, str(tmp_path / "spill"))


@pytest.fixture(autouse=True)
def _no_e2e_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The e2e switches are set by the test that needs them, never by the shell running pytest."""
    monkeypatch.delenv("FLAME_MCP_E2E_FLAME_VERSION", raising=False)
    monkeypatch.delenv("FLAME_MCP_E2E_PROJECT", raising=False)
