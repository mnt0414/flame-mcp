#!/usr/bin/env bash
#
# Entry point for registering the MCP server with Claude Code.
# stdout is for JSON-RPC only (the same constraint as test_stdout_purity.py), so nothing is
# echoed here.
#
# NEVER set FLAME_MCP_TEST_PAUSE_AFTER_PREREAD, FLAME_MCP_TEST_EXIT_AFTER_WRITE or
# FLAME_MCP_TEST_WRITE_TIMEOUT_S. They are fault-injection hooks that the journal's real-Flame
# e2e (scripts/p2_2a_journal_e2e.py) sets per subprocess; when enabled, a write stops halfway or
# the server dies right after a write. Do not put them in this script (the production MCP
# registration) or in the environment that calls it (.zshrc, the env of the MCP registration,
# and so on). They are off by default.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"
PYTHONPATH=src exec .venv/bin/python -m flame_mcp "$@"
