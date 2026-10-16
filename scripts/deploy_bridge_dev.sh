#!/usr/bin/env bash
#
# Copies the stub and the bridge body to the development directories as real files (no
# symlinks, because a symlink breaks Flame's "Reload Python Hooks"). Nothing under /opt/Autodesk
# is touched, and Flame is not started: it only prepares the targets for the development flow
# that loads the hooks through DL_PYTHON_HOOK_PATH.
#
# Targets (development and production stay completely apart):
#   stub -> ~/Library/Application Support/flame-mcp/dev_hooks/flame_mcp_bridge.py
#   body -> ~/Library/Application Support/flame-mcp/bridge_dev/impl.py
# This script never touches the production body, ~/Library/Application Support/flame-mcp/bridge/
# impl.py (scripts/install_bridge.sh owns it). The Flame started for development gets
# FLAME_MCP_BRIDGE_DIR=<bridge_dev>, so the stub reads this copy (the path is defined in one
# place, scripts/bridge_dev_env.sh).
#
# If BRIDGE_IMPL_SRC is set, the bridge body is taken from that path instead of the repository
# (to deploy an older body and run the same load test against it). By default it is the
# repository's src/flame_mcp_bridge/impl.py.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_SUPPORT="$HOME/Library/Application Support/flame-mcp"
DEV_HOOKS_DIR="$APP_SUPPORT/dev_hooks"
# shellcheck source=bridge_dev_env.sh
source "$SCRIPT_DIR/bridge_dev_env.sh"

STUB_SRC="$REPO_ROOT/stub/flame_mcp_bridge.py"
IMPL_SRC="${BRIDGE_IMPL_SRC:-$REPO_ROOT/src/flame_mcp_bridge/impl.py}"

if [ ! -f "$STUB_SRC" ]; then
    echo "Stub not found: $STUB_SRC. Restore stub/flame_mcp_bridge.py in the repository." >&2
    exit 1
fi
if [ ! -f "$IMPL_SRC" ]; then
    echo "Bridge body not found: $IMPL_SRC. Restore src/flame_mcp_bridge/impl.py, or fix BRIDGE_IMPL_SRC." >&2
    exit 1
fi
if [ -n "${BRIDGE_IMPL_SRC:-}" ]; then
    echo "BRIDGE_IMPL_SRC is set. Deploying the bridge body from it, not from the repository: $IMPL_SRC"
fi

mkdir -p "$DEV_HOOKS_DIR" "$BRIDGE_DEV_DIR"

for target in "$DEV_HOOKS_DIR/flame_mcp_bridge.py" "$BRIDGE_DEV_DIR/impl.py"; do
    if [ -L "$target" ]; then
        echo "The existing $target is a symlink. Deleting it and replacing it with a real file."
        rm -f "$target"
    fi
done

cp "$STUB_SRC" "$DEV_HOOKS_DIR/flame_mcp_bridge.py"
cp "$IMPL_SRC" "$BRIDGE_DEV_DIR/impl.py"

find "$DEV_HOOKS_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find "$BRIDGE_DEV_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo "Deployed:"
echo "  stub -> $DEV_HOOKS_DIR/flame_mcp_bridge.py"
echo "  impl -> $BRIDGE_DEV_DIR/impl.py"
echo "--- sha256 (targets) ---"
shasum -a 256 "$DEV_HOOKS_DIR/flame_mcp_bridge.py"
shasum -a 256 "$BRIDGE_DEV_DIR/impl.py"
echo "--- sha256 (sources; they should match the targets; the repository files unless BRIDGE_IMPL_SRC is set) ---"
shasum -a 256 "$STUB_SRC"
shasum -a 256 "$IMPL_SRC"
