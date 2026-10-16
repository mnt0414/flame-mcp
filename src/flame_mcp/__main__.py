"""Runs the 4 tools over stdio (`python -m flame_mcp`).

Fails closed at startup: an unusable Policy configuration (or a setting with a non-ASCII
character that would be sent to Flame) prints its reason to
stderr and exits non-zero before the stdio loop (and therefore before any MCP
traffic) starts. Logging goes to stderr only, because stdout is reserved for the
JSON-RPC stream once the loop is running.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server

from flame_mcp import envelope, transport
from flame_mcp.journal import JOURNAL_DIR_ENV, Journal
from flame_mcp.ops import register_all
from flame_mcp.opschema import find_non_ascii
from flame_mcp.policy import Policy, PolicyConfigError
from flame_mcp.registry import Registry
from flame_mcp.runtime import TestHooks, TestHooksConfigError, warn_if_test_hooks
from flame_mcp.server import create_server
from flame_mcp.timeline_checkpoints import LIBRARY_ENV, checkpoint_library_name


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _non_ascii_setting(journal: Journal) -> str | None:
    """The reason a setting that ends up in the code sent to Flame cannot be used, or None.

    The journal directory (the checkpoint files live under it) and the checkpoint library name
    are put into that code, and Flame takes ASCII only.
    """
    settings = (
        (
            "the journal directory",
            str(journal.directory),
            f"Set {JOURNAL_DIR_ENV} to an ASCII path.",
        ),
        (
            LIBRARY_ENV,
            checkpoint_library_name(),
            f"Set {LIBRARY_ENV} to an ASCII name, or unset it.",
        ),
    )
    for what, value, advice in settings:
        issue = find_non_ascii(value, what)
        if issue is not None:
            return f"{issue.path}: {issue.message} {advice}"
    return None


def build_server(*, run_dir: Path | None = None) -> Server[Any]:
    try:
        policy = Policy.from_env(os.environ)
    except PolicyConfigError as exc:
        print(f"flame-mcp: invalid policy configuration: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    # Swept once per startup, before any MCP traffic: a crash-recovery record left
    # by a Flame session that later crashed has no other cleanup path.
    transport.cleanup_stale_inflight(run_dir)
    envelope.cleanup_old_spills()
    journal = Journal()
    problem = _non_ascii_setting(journal)
    if problem is not None:
        print(f"flame-mcp: invalid configuration: {problem}", file=sys.stderr)
        raise SystemExit(1)
    try:
        journal.cleanup_old()  # journal files not touched for 30 days
    except OSError as exc:
        print(f"flame-mcp: journal cleanup failed: {exc}", file=sys.stderr)
    try:
        hooks = TestHooks.from_env(os.environ)
    except TestHooksConfigError as exc:
        print(f"flame-mcp: invalid test hook configuration: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    warn_if_test_hooks(hooks)
    registry = Registry()
    register_all(registry)
    return create_server(registry, policy, run_dir=run_dir, journal=journal, hooks=hooks)


async def _serve(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    _configure_logging()
    server = build_server()
    anyio.run(_serve, server)


if __name__ == "__main__":
    main()
