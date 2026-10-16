#!/usr/bin/env bash
#
# Generic real-Flame orchestrator: runs one client script with the .venv Python against a Flame
# that has the dev bridge. The client waits for the bridge socket itself; this script does not.
#
# It deploys the stub to dev_hooks and the bridge body to bridge_dev with
# scripts/deploy_bridge_dev.sh (the production bridge/ is neither used nor written), and starts
# Flame with DL_PYTHON_HOOK_PATH and FLAME_MCP_BRIDGE_DIR (nothing under /opt/Autodesk is
# touched). When the Flame process is up, it runs
# `PYTHONPATH=src .venv/bin/python <client.py> [client args...]` and, once the client returns,
# waits for Flame to exit by itself (for example through flame.exit()).
#
# If BRIDGE_IMPL_SRC is set, it is passed on to deploy_bridge_dev.sh as it is (to deploy an
# older bridge body and run the same load test against it).
#
# When the client exits with 3 (the "suspected hang" code of scripts/p1_stress_e2e.py), or when
# this script's own wait (TIMEOUT below) runs out, it takes a sample and then kills Flame. If
# the sample contains both scheduleIdleEvent and take_gil, the output reports a deadlock where
# the handler thread is stuck in scheduleIdleEvent and the main thread in take_gil. That alone
# does not prove a deadlock; the raw sample is kept so you can check it later.
#
# Usage: run_flame_with_bridge.sh <client.py> [TIMEOUT] [-- client args...]
#   <client.py>   client script to run with the .venv Python (required)
#   [TIMEOUT]     seconds to wait for Flame to exit by itself (default 600)
#   [-- args...]  everything after `--` is passed to the client unchanged
#
# This script starts Flame itself.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <client.py> [TIMEOUT] [-- client args...]" >&2
    exit 1
fi

CLIENT_PY="$1"
shift

TIMEOUT=600
if [ $# -gt 0 ] && [ "$1" != "--" ]; then
    case "$1" in
        ''|*[!0-9]*) : ;;  # Not all digits, so this is not a TIMEOUT: keep the default and do not shift.
        *) TIMEOUT="$1"; shift ;;
    esac
fi

CLIENT_ARGS=()
if [ $# -gt 0 ] && [ "$1" = "--" ]; then
    shift
    CLIENT_ARGS=("$@")
fi

if [ ! -f "$CLIENT_PY" ]; then
    echo "Client script not found: $CLIENT_PY. Pass the path of an existing client script." >&2
    exit 1
fi

# shellcheck source=e2e_flame_env.sh
source "$SCRIPT_DIR/e2e_flame_env.sh"
APP_SUPPORT="$HOME/Library/Application Support/flame-mcp"
DEV_HOOKS_DIR="$APP_SUPPORT/dev_hooks"
# shellcheck source=bridge_dev_env.sh
source "$SCRIPT_DIR/bridge_dev_env.sh"
RUN_DIR="$APP_SUPPORT/run"
P1_DIR="$APP_SUPPORT/p1"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

# The PHASE0B_* variables are set only in tests (see e2e_flame_env.sh for the log paths and the
# process pattern): /opt/Autodesk is not writable there, so they point at fakes.
SENDDMP_PATTERN="${PHASE0B_SENDDMP_PATTERN:-CER/senddmp\.app/Contents/MacOS/senddmp}"
SAMPLE_BIN="${PHASE0B_SAMPLE_CMD:-sample}"
FLAME_ANY_VERSION_PATTERN='flame\.app/Contents/MacOS/flame( |$)'

POLL_INTERVAL=5
BIN_WAIT_TIMEOUT=60
TERM_WAIT=10

if pgrep -f "$FLAME_ANY_VERSION_PATTERN" >/dev/null 2>&1; then
    echo "Flame is already running (any version). Aborting. Quit Flame, then run this again." >&2
    exit 1
fi

mkdir -p "$P1_DIR"

client_base="$(basename "$CLIENT_PY")"

echo "=== deploying the stub to dev_hooks and the bridge body to bridge_dev ==="
if [ -n "${BRIDGE_IMPL_SRC:-}" ]; then
    echo "Passing BRIDGE_IMPL_SRC=$BRIDGE_IMPL_SRC to deploy_bridge_dev.sh."
fi
BRIDGE_IMPL_SRC="${BRIDGE_IMPL_SRC:-}" "$SCRIPT_DIR/deploy_bridge_dev.sh"

# A socket left by an earlier run is harmless (probe-before-bind), but removing it first makes
# "a socket appeared" mean "this launch made it".
rm -f "$RUN_DIR"/flame-mcp-*.sock 2>/dev/null || true

app_log_lines_before=0
app_log_inode_before=""
if [ -f "$APP_LOG" ]; then
    app_log_lines_before=$(wc -l <"$APP_LOG" | tr -d ' ')
    app_log_inode_before=$(stat -f "%i" "$APP_LOG" 2>/dev/null || echo "")
fi
shell_log_lines_before=0
shell_log_inode_before=""
if [ -f "$SHELL_LOG" ]; then
    shell_log_lines_before=$(wc -l <"$SHELL_LOG" | tr -d ' ')
    shell_log_inode_before=$(stat -f "%i" "$SHELL_LOG" 2>/dev/null || echo "")
fi
senddmp_pids_before=$(pgrep -f "$SENDDMP_PATTERN" 2>/dev/null || true)

ts=$(date +%y%m%d_%H%M%S)
launch_log="$P1_DIR/launch_${client_base}_${ts}.log"

echo "=== starting Flame (DL_PYTHON_HOOK_PATH=${DEV_HOOKS_DIR}, FLAME_MCP_BRIDGE_DIR=${BRIDGE_DEV_DIR}, /opt/Autodesk is not touched) ==="
open_status=0
open -n -a "$FLAME_APP" \
    --env "DL_PYTHON_HOOK_PATH=$DEV_HOOKS_DIR" \
    --env "FLAME_MCP_BRIDGE_DIR=$BRIDGE_DEV_DIR" \
    --env "DL_DEBUG_PYTHON_HOOKS=1" \
    --args \
    --start-project="$PROJECT" \
    --start-workspace=Workspace --create-workspace --no-confirm \
    >"$launch_log" 2>&1 || open_status=$?
echo "open: exit=${open_status}  launch log: $launch_log"

flame_pid=""
outcome="unknown"

if [ "$open_status" -ne 0 ]; then
    echo "open failed (exit=${open_status}). Treating this as a launch failure. See the launch log named above." >&2
    outcome="launch_failed"
else
    echo "Waiting up to ${BIN_WAIT_TIMEOUT}s for the Flame process."
    bin_wait_elapsed=0
    while [ "$bin_wait_elapsed" -lt "$BIN_WAIT_TIMEOUT" ]; do
        candidate=$(pgrep -f "$FLAME_BIN_PATTERN" 2>/dev/null | head -1 || true)
        if [ -n "$candidate" ]; then
            flame_pid="$candidate"
            break
        fi
        sleep "$POLL_INTERVAL"
        bin_wait_elapsed=$((bin_wait_elapsed + POLL_INTERVAL))
    done
    if [ -z "$flame_pid" ]; then
        echo "No Flame process within ${BIN_WAIT_TIMEOUT}s. Treating this as a launch failure. See the launch log named above and app.log." >&2
        outcome="launch_failed"
    fi
fi

# First line of shell.log that belongs to this run; 1 if the file was rotated.
_shell_log_start_line() {
    local lines_now inode_now
    [ -f "$SHELL_LOG" ] || { echo 0; return 1; }
    lines_now=$(wc -l <"$SHELL_LOG" | tr -d ' ')
    inode_now=$(stat -f "%i" "$SHELL_LOG" 2>/dev/null || echo "")
    if [ "$inode_now" != "$shell_log_inode_before" ] || [ "$lines_now" -lt "$shell_log_lines_before" ]; then
        echo 1
    else
        echo $((shell_log_lines_before + 1))
    fi
}

check_crashed() {
    local start_line
    start_line=$(_shell_log_start_line) || return 1
    # /usr/bin/grep on purpose: an interactive shell may alias grep. -a because shell.log can hold
    # non-text bytes, and without it grep treats the file as binary and quietly reports no match.
    tail -n "+${start_line}" "$SHELL_LOG" | /usr/bin/grep -a -q 'abnormal termination'
}

# The sample taken after a timeout or a suspected hang is raw evidence, not a verdict. This only
# detects the handler thread stuck in scheduleIdleEvent and the main thread stuck in take_gil,
# which is not enough to call it a deadlock. -a for the same reason as in check_crashed:
# the sample can hold non-text bytes.
_check_deadlock_signature() {
    local sample_file="$1"
    [ -f "$sample_file" ] || return 1
    /usr/bin/grep -a -q 'scheduleIdleEvent' "$sample_file" && \
        /usr/bin/grep -a -q 'take_gil' "$sample_file"
}

client_exit_code="N/A"
if [ -n "$flame_pid" ]; then
    echo "Flame: pid=$flame_pid"
    echo "=== running client: $CLIENT_PY ${CLIENT_ARGS[*]:-} ==="
    # In bash 3.2 (the macOS default) "${arr[@]}" on an empty array expands to one empty string,
    # not to zero words (`set -u` does not stop it), so the client's argparse would get an empty
    # positional argument. Branch on whether the array is empty.
    client_exit_code=0
    if [ ${#CLIENT_ARGS[@]} -gt 0 ]; then
        PYTHONPATH="$REPO_ROOT/src" "$VENV_PYTHON" "$CLIENT_PY" "${CLIENT_ARGS[@]}" || client_exit_code=$?
    else
        PYTHONPATH="$REPO_ROOT/src" "$VENV_PYTHON" "$CLIENT_PY" || client_exit_code=$?
    fi
    echo "client exit=$client_exit_code"

    if [ "$client_exit_code" = "3" ]; then
        # The client itself (for example scripts/p1_stress_e2e.py) found a stall (BRIDGE_LOST,
        # TIMEOUT, or a request over 15s) and exited with 3 as agreed. Flame is still running, so
        # take a sample instead of waiting TIMEOUT for an exit that will not come.
        outcome="stuck_suspected"
    else
        echo "Waiting up to ${TIMEOUT}s for Flame to exit by itself."
        elapsed=0
        outcome="timeout"
        while [ "$elapsed" -lt "$TIMEOUT" ]; do
            if ! kill -0 "$flame_pid" 2>/dev/null; then
                outcome="exited"
                break
            fi
            if check_crashed; then
                outcome="crashed"
                break
            fi
            sleep "$POLL_INTERVAL"
            elapsed=$((elapsed + POLL_INTERVAL))
        done
    fi
fi

case "$outcome" in
    exited)
        echo "Flame exited by itself."
        ;;
    crashed)
        echo "Found abnormal termination in shell.log (only lines after launch are checked)."
        crash_sample_file="$P1_DIR/crash_sample_${client_base}_${ts}.txt"
        if [ -n "$flame_pid" ] && "$SAMPLE_BIN" "$flame_pid" 1 -file "$crash_sample_file" >/dev/null 2>&1; then
            echo "Sample taken: $crash_sample_file"
        else
            echo "Taking the sample failed (the process may already be gone)."
        fi
        if [ -n "$flame_pid" ]; then
            kill -TERM "$flame_pid" 2>/dev/null || true
            sleep "$TERM_WAIT"
            if kill -0 "$flame_pid" 2>/dev/null; then
                kill -KILL "$flame_pid" 2>/dev/null || true
            fi
        fi
        ;;
    stuck_suspected)
        echo "The client reported a suspected hang (exit 3). Taking a sample, then killing Flame."
        stuck_sample_file="$P1_DIR/stuck_sample_${client_base}_${ts}.txt"
        if [ -n "$flame_pid" ] && "$SAMPLE_BIN" "$flame_pid" 1 -file "$stuck_sample_file" >/dev/null 2>&1; then
            echo "Sample taken: $stuck_sample_file"
            if _check_deadlock_signature "$stuck_sample_file"; then
                echo "Deadlock: the handler thread is stuck in scheduleIdleEvent and the main thread in take_gil (both found in the sample)."
            else
                echo "Sample taken, but it does not show the deadlock pattern (scheduleIdleEvent and take_gil together)."
            fi
        else
            echo "Taking the sample failed (the process may already be gone)."
        fi
        if [ -n "$flame_pid" ]; then
            kill -KILL "$flame_pid" 2>/dev/null || true
        fi
        ;;
    timeout)
        echo "TIMEOUT (possible deadlock). Taking a sample, then killing Flame."
        if [ -n "$flame_pid" ]; then
            timeout_sample_file="$P1_DIR/timeout_sample_${client_base}_${ts}.txt"
            "$SAMPLE_BIN" "$flame_pid" 1 -file "$timeout_sample_file" >/dev/null 2>&1 || true
            if _check_deadlock_signature "$timeout_sample_file"; then
                echo "Deadlock: the handler thread is stuck in scheduleIdleEvent and the main thread in take_gil (both found in the sample)."
            fi
            kill -KILL "$flame_pid" 2>/dev/null || true
        fi
        ;;
    launch_failed)
        :  # only the launcher clean-up below
        ;;
esac

# TERM only the senddmp (crash reporter) processes that appeared after launch, so they do not send
# anything. Ones that were already running are left alone.
senddmp_pids_after=$(pgrep -f "$SENDDMP_PATTERN" 2>/dev/null || true)
new_senddmp_pids=$(comm -13 \
    <(printf '%s\n' "$senddmp_pids_before" | sort) \
    <(printf '%s\n' "$senddmp_pids_after" | sort) | /usr/bin/grep -a -v '^$' || true)
if [ -n "$new_senddmp_pids" ]; then
    echo "Sending TERM to senddmp processes that appeared after launch (so they do not send):"
    for pid in $new_senddmp_pids; do
        echo "  senddmp pid=$pid: TERM"
        kill -TERM "$pid" 2>/dev/null || true
    done
else
    echo "No new senddmp process after launch."
fi

echo "=== exit route: $outcome (client exit=${client_exit_code}) ==="

echo "--- sockets must be gone (app_exited check) ---"
remaining_socks=$(ls "$RUN_DIR"/flame-mcp-*.sock 2>/dev/null || true)
if [ -z "$remaining_socks" ]; then
    echo "OK: no sockets remain."
else
    echo "NG: sockets remain (expected when Flame did not exit cleanly):"
    echo "$remaining_socks"
fi

if [ -f "$APP_LOG" ]; then
    app_log_lines_after=$(wc -l <"$APP_LOG" | tr -d ' ')
    app_log_inode_after=$(stat -f "%i" "$APP_LOG" 2>/dev/null || echo "")
    start_line=$((app_log_lines_before + 1))
    if [ "$app_log_inode_after" != "$app_log_inode_before" ] || \
       [ "$app_log_lines_after" -lt "$app_log_lines_before" ]; then
        echo "(note: app.log seems to have been rotated; showing it from the top)"
        start_line=1
    fi
    echo "--- app.log since launch: HOTKEY / PythonAPI: set attribute / DIALOG : lines ---"
    # /usr/bin/grep -a because app.log can hold non-text bytes. Alternation with -E, not \|.
    tail -n "+${start_line}" "$APP_LOG" | \
        /usr/bin/grep -aE 'HOTKEY|PythonAPI: set attribute|DIALOG :' || echo "(no matching lines)"
else
    echo "--- app.log not found: $APP_LOG ---"
fi

bridge_log="$APP_SUPPORT/logs/bridge.log"
if [ -f "$bridge_log" ]; then
    echo "--- last 40 lines of bridge.log ---"
    tail -n 40 "$bridge_log"
else
    echo "--- bridge.log not found: $bridge_log ---"
fi
