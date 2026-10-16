#!/usr/bin/env bash
#
# Orchestrator for the real-Flame e2e that tells journal entries apart across a Flame restart.
# It starts Flame twice.
#
#   First run:  scripts/p2_4_restart_e2e.py --phase 1
#               (W1: a write where the server dies and only the intent is left. R1 and R2: renders
#                with timeout_s=1, which give pending handles. The result file of R2 is moved from
#                run/results/ to p2/moved_results_<tag>/. The client ends with flame.exit().)
#               Then wait for Flame to exit by itself.
#   Second run: start Flame with the same project and run scripts/p2_4_restart_e2e.py --phase 2
#               (a new server tells W1, R1 and R2 apart with journal.check. It ends with
#                flame.exit().)
#
# Between the two runs, Flame's run/results/ (where the bridge keeps result files) is not
# deleted. The state that the phases share (group uid, journal group, handles, expected values)
# is passed in p2/p2_4_restart_state_<tag>.json. Both runs use the same journal,
# p2/journal_p24_e2e_<tag>/ (the production journal/ is not written).
#
# It deploys the stub to dev_hooks and the bridge body to bridge_dev with
# scripts/deploy_bridge_dev.sh (the production bridge/ is neither used nor written), and starts
# Flame with DL_PYTHON_HOOK_PATH and FLAME_MCP_BRIDGE_DIR (nothing under /opt/Autodesk is
# touched).
#
# If a Flame is already running (any version), it aborts without doing anything. The same check
# runs before the second launch.
#
# Usage: run_p2_4_restart_e2e.sh [TIMEOUT]
#   [TIMEOUT]  seconds to wait for Flame to exit by itself after the client returns (default 600)
#
# This script starts Flame twice itself.
set -euo pipefail

# shellcheck source=e2e_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/e2e_lib.sh"

P2_DIR="$APP_SUPPORT/p2"
E2E_CLIENT="$SCRIPT_DIR/p2_4_restart_e2e.py"

TIMEOUT="${1:-600}"
GONE_WAIT_S=30

e2e_init "$P2_DIR"
e2e_abort_if_flame_running

mkdir -p "$P2_DIR"

tag=$(date +%y%m%d_%H%M%S)
STATE_FILE="$P2_DIR/p2_4_restart_state_${tag}.json"

e2e_deploy

# One phase: start Flame, wait for the socket, run the e2e (--phase $1), wait for Flame to exit
# by itself, clean up after a crash or timeout, and TERM senddmp. The result is in ${outcome} and
# ${e2e_exit_code}.
run_flame_phase() {
    local phase="$1"

    ts=$(date +%y%m%d_%H%M%S)
    E2E_TAG="[phase ${phase}] "
    E2E_SAMPLE_SUFFIX="_p24_phase${phase}"

    e2e_snapshot_logs
    # Leave run/results/ (the result files) alone.
    e2e_clear_sockets

    e2e_launch_flame "$P2_DIR/launch_${ts}_p24_phase${phase}.log"

    if [ -n "$flame_pid" ]; then
        echo "Flame: pid=$flame_pid"
        if e2e_wait_for_socket; then
            echo "=== [phase ${phase}] running $(basename "$E2E_CLIENT") --phase ${phase} ==="
            e2e_run_client "$E2E_CLIENT" --phase "$phase" --run-dir "$RUN_DIR" \
                --tag "$tag" --state-file "$STATE_FILE"
            e2e_wait_for_exit "$TIMEOUT"
        fi
    fi

    e2e_dispose_flame
    e2e_reap_senddmp
    e2e_report_exit_route "$E2E_CLIENT"
}

report_phase() {
    local phase="$1"
    local phase_result

    E2E_TAG="[phase ${phase}] "
    e2e_report_sockets_gone
    e2e_report_app_log
    e2e_report_bridge_log

    echo "--- [phase ${phase}] result file ---"
    phase_result="$P2_DIR/p2_4_restart_e2e_${tag}_phase${phase}.json"
    if [ -f "$phase_result" ]; then
        echo "$phase_result"
        cat "$phase_result"
    else
        echo "Result file not found: $phase_result"
    fi
}

state_is_ready() {
    [ -f "$STATE_FILE" ] && "$VENV_PYTHON" -c \
        'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])).get("ready") is True else 1)' \
        "$STATE_FILE"
}

run_flame_phase 1
report_phase 1
phase1_outcome="$outcome"
phase1_exit="$e2e_exit_code"

if [ "$phase1_outcome" != "exited" ]; then
    echo "Flame did not exit by itself in the first run (${phase1_outcome}). Not starting the second run. See the report of the first run above." >&2
    exit 1
fi
if ! state_is_ready; then
    echo "The state file of the first run (${STATE_FILE}) is not ready (W1, R1 or R2 could not be made). Not starting the second run. See the report of the first run above." >&2
    exit 1
fi

# Before the second launch: make sure the first Flame is really gone.
gone_elapsed=0
while pgrep -f "$FLAME_ANY_VERSION_PATTERN" >/dev/null 2>&1; do
    if [ "$gone_elapsed" -ge "$GONE_WAIT_S" ]; then
        echo "Flame is still running. Not starting the second run. Quit Flame, then run this again." >&2
        exit 1
    fi
    sleep 1
    gone_elapsed=$((gone_elapsed + 1))
done

run_flame_phase 2
report_phase 2

echo "=== overall: phase1 outcome=$phase1_outcome e2e=$phase1_exit / phase2 outcome=$outcome e2e=$e2e_exit_code ==="
if [ "$outcome" = "exited" ] && [ "$phase1_exit" = "0" ] && [ "$e2e_exit_code" = "0" ]; then
    exit 0
fi
exit 1
