#!/usr/bin/env bash
#
# Orchestrator for the real-Flame end-to-end test of the bridge.
#
# It deploys the stub to dev_hooks and the bridge body to bridge_dev with
# scripts/deploy_bridge_dev.sh (the production bridge/ is neither used nor written), and starts
# Flame with DL_PYTHON_HOOK_PATH and FLAME_MCP_BRIDGE_DIR (nothing under /opt/Autodesk is
# touched). The bridge starts by itself at app_initialized, so --execute-python-script is not
# used. Once the socket exists, it runs scripts/p1_bridge_e2e.py (with the .venv Python), waits
# for the flame.exit() that step g of that script sends, and cleans up.
#
# Usage: run_p1_bridge_e2e.sh [TIMEOUT]
#   [TIMEOUT]  seconds to wait for Flame to exit by itself after the client returns (default 600)
#
# This script starts Flame itself.
set -euo pipefail

# shellcheck source=e2e_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/e2e_lib.sh"

P1_DIR="$APP_SUPPORT/p1"
E2E_CLIENT="$SCRIPT_DIR/p1_bridge_e2e.py"
TIMEOUT="${1:-600}"

e2e_init "$P1_DIR"
e2e_abort_if_flame_running
e2e_prepare

ts=$(date +%y%m%d_%H%M%S)
e2e_launch_flame "$P1_DIR/launch_${ts}.log"

# The client waits for the socket itself (--wait-socket-s), so there is no socket wait here, and
# the wait for Flame to exit starts whatever the client returned.
if [ -n "$flame_pid" ]; then
    echo "Flame: pid=$flame_pid"
    echo "=== running $(basename "$E2E_CLIENT") (it waits up to ${SOCKET_WAIT_S}s for the socket) ==="
    e2e_run_client "$E2E_CLIENT" --wait-socket-s "$SOCKET_WAIT_S"
    e2e_wait_for_exit "$TIMEOUT"
fi

e2e_finish "$E2E_CLIENT" "p1_bridge_e2e_*.json" no
