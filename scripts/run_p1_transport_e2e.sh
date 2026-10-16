#!/usr/bin/env bash
#
# Orchestrator for the real-Flame end-to-end test of the server transport.
#
# It deploys the stub to dev_hooks and the bridge body to bridge_dev with
# scripts/deploy_bridge_dev.sh (the production bridge/ is neither used nor written), and starts
# Flame with DL_PYTHON_HOOK_PATH and FLAME_MCP_BRIDGE_DIR (nothing under /opt/Autodesk is
# touched). The bridge starts by itself at app_initialized, so --execute-python-script is not
# used. Once the socket exists, it runs scripts/p1_transport_e2e.py (with the .venv Python,
# through flame_mcp.transport).
#
# In step d, p1_transport_e2e.py sends SIGKILL to the Flame process itself (to confirm
# BRIDGE_LOST on a real Flame). So this script does not wait for Flame to exit through
# flame.exit(); it checks whether Flame is still alive after p1_transport_e2e.py returns and
# cleans up accordingly. After a SIGKILL the socket file stays behind, which is normal, so the
# check that the socket is gone gives no OK or NG (a leftover socket is not an error).
#
# This script starts Flame itself.
set -euo pipefail

# shellcheck source=e2e_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/e2e_lib.sh"

P1_DIR="$APP_SUPPORT/p1"
E2E_CLIENT="$SCRIPT_DIR/p1_transport_e2e.py"

# When p1_transport_e2e.py returns, its step d should already have sent SIGKILL. Here we only
# wait for the OS to show that the process is gone.
POST_RUN_EXIT_WAIT_S=15
POST_RUN_POLL_INTERVAL=1

e2e_init "$P1_DIR"
e2e_abort_if_flame_running
e2e_prepare
# The run directory is shared with other uses of the bridge, so nothing in it is deleted; the
# records that exist now are remembered and only the ones this run adds are reported at the end.
inflight_before=$(ls "$RUN_DIR"/inflight/*.json 2>/dev/null || true)

ts=$(date +%y%m%d_%H%M%S)
e2e_launch_flame "$P1_DIR/launch_transport_${ts}.log"

if [ -n "$flame_pid" ]; then
    echo "Flame: pid=$flame_pid"
    echo "=== waiting for the socket, then running p1_transport_e2e.py (waits up to ${SOCKET_WAIT_S}s) ==="
    # p1_transport_e2e.py looks for the socket itself, so all we do here is wait for it to appear.
    # The client runs even if none appears.
    e2e_poll_socket || true

    e2e_run_client "$E2E_CLIENT" --run-dir "$RUN_DIR"

    echo "Step d of p1_transport_e2e.py should have sent SIGKILL. Checking for up to ${POST_RUN_EXIT_WAIT_S}s that the process is gone."
    elapsed=0
    outcome="still_alive_after_e2e"
    while [ "$elapsed" -lt "$POST_RUN_EXIT_WAIT_S" ]; do
        if ! kill -0 "$flame_pid" 2>/dev/null; then
            outcome="killed_or_exited"
            break
        fi
        sleep "$POST_RUN_POLL_INTERVAL"
        elapsed=$((elapsed + POST_RUN_POLL_INTERVAL))
    done
fi

case "$outcome" in
    killed_or_exited)
        echo "Flame has exited (SIGKILL from step d of p1_transport_e2e.py, or another cause)."
        if e2e_check_crashed; then
            echo "(for reference) Found abnormal termination in shell.log. This is expected after a SIGKILL."
        fi
        ;;
    still_alive_after_e2e)
        echo "Flame is still alive after p1_transport_e2e.py returned (step d may have been aborted). Sending TERM, then KILL, to be safe."
        if [ -n "$flame_pid" ]; then
            kill -TERM "$flame_pid" 2>/dev/null || true
            sleep "$TERM_WAIT"
            if kill -0 "$flame_pid" 2>/dev/null; then
                kill -KILL "$flame_pid" 2>/dev/null || true
            fi
        fi
        ;;
    launch_failed)
        :  # only the launcher clean-up below
        ;;
esac

# The crash reporter normally does not start after a SIGKILL, but the TERM/KILL in the
# still_alive case can start it, so the same guard applies here.
e2e_reap_senddmp

e2e_report_exit_route "$E2E_CLIENT"

echo "--- leftover sockets (normal after a SIGKILL; no OK or NG is given) ---"
remaining_socks=$(ls "$RUN_DIR"/flame-mcp-*.sock 2>/dev/null || true)
if [ -z "$remaining_socks" ]; then
    echo "(none)"
else
    echo "$remaining_socks"
fi

echo "--- inflight files added by this run (not deleted) ---"
# shellcheck disable=SC2010 # a line-set difference, not a name match; inflight names are uuid hex, never hold a newline
remaining_inflight=$(ls "$RUN_DIR"/inflight/*.json 2>/dev/null | grep -vxF -e "$inflight_before" || true)
if [ -z "$remaining_inflight" ]; then
    echo "(none)"
else
    echo "$remaining_inflight"
fi

e2e_report_app_log
e2e_report_bridge_log
e2e_report_latest_result "$E2E_CLIENT" "p1_transport_e2e_*.json" no
