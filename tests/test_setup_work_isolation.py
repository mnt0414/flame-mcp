"""`setup_work` keeps the journal directory of the last `Runtime` in a module global; conftest
resets it for every test, so a test without a Runtime gets the default journal directory (which
conftest points at the test's own tmp_path) and not the one an earlier test left behind.

The first test leaves the global set on purpose; the second runs after it in the same process."""

from __future__ import annotations

from pathlib import Path

from flame_mcp import setup_work
from flame_mcp.journal import default_journal_dir


def test_a_runtime_sets_the_directory_and_leaves_it_set() -> None:
    setup_work.set_journal_directory(Path("/nonexistent/leaked-by-an-earlier-test"))
    assert setup_work.work_root() == "/nonexistent/leaked-by-an-earlier-test/setups"


def test_a_test_without_a_runtime_gets_the_default_journal_directory() -> None:
    assert setup_work.work_root() == str(default_journal_dir() / setup_work.WORK_DIR_NAME)
