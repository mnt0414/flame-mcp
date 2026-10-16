# shellcheck shell=bash
# Shared parts of the real-Flame e2e orchestrators: scripts/run_e2e.sh and the dedicated
# run_p1_bridge_e2e.sh, run_p1_transport_e2e.sh and run_p2_4_restart_e2e.sh. Source it, do not run
# it. The caller sets `set -euo pipefail` and calls e2e_init first.
#
# What an e2e run does with these:
#   deploy the bridge to the dev directories (never the production bridge/), start Flame with
#   DL_PYTHON_HOOK_PATH and FLAME_MCP_BRIDGE_DIR (nothing under /opt/Autodesk is touched), wait
#   for the bridge socket, run the client, wait for Flame to exit by itself, and on a crash or a
#   timeout take a sample and kill it. Then print the exit route, the leftover sockets, the app.log
#   lines since launch, the bridge.log tail and the latest result file.
#
# State is global on purpose:
#   flame_pid, outcome, e2e_exit_code, crash_seen, ts
#   outcome: unknown | launch_failed | exited | crashed | timeout
#
# Which Flame to start and which project to start it in come from e2e_flame_env.sh
# (FLAME_MCP_E2E_FLAME_VERSION, FLAME_MCP_E2E_PROJECT). Every path and pattern below, and the
# log paths and the process pattern that file derives, can be replaced for a test through the
# PHASE0B_* variables.

E2E_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$E2E_LIB_DIR"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=e2e_flame_env.sh
source "$SCRIPT_DIR/e2e_flame_env.sh"
APP_SUPPORT="$HOME/Library/Application Support/flame-mcp"
DEV_HOOKS_DIR="$APP_SUPPORT/dev_hooks"
# shellcheck source=bridge_dev_env.sh
source "$SCRIPT_DIR/bridge_dev_env.sh"
RUN_DIR="$APP_SUPPORT/run"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

SENDDMP_PATTERN="${PHASE0B_SENDDMP_PATTERN:-CER/senddmp\.app/Contents/MacOS/senddmp}"
SAMPLE_BIN="${PHASE0B_SAMPLE_CMD:-sample}"
FLAME_ANY_VERSION_PATTERN='flame\.app/Contents/MacOS/flame( |$)'

POLL_INTERVAL=5
BIN_WAIT_TIMEOUT=60
TERM_WAIT=10
SOCKET_WAIT_S=120

# Directory for launch logs, samples and the client's result files (set by e2e_init).
RESULTS_DIR=""
# Prefix for the messages of one run of several (the restart e2e sets "[phase 1] ").
E2E_TAG=""
# Appended to the crash/timeout sample file names (the restart e2e sets "_p24_phase1").
E2E_SAMPLE_SUFFIX=""
# 1: the client's exit is followed by a shell.log crash check (see e2e_probe_crash).
E2E_CRASH_CHECK=0

flame_pid=""
outcome="unknown"
e2e_exit_code="N/A"
crash_seen=0
ts=""

app_log_lines_before=0
app_log_inode_before=""
shell_log_lines_before=0
shell_log_inode_before=""
senddmp_pids_before=""

#######################################
# Records the directory for launch logs, samples and the client's result files.
# Globals:
#   RESULTS_DIR (set)
# Arguments:
#   $1: the results directory
#######################################
e2e_init() {
    RESULTS_DIR="$1"
}

#######################################
# Exits the calling script when a Flame of any version is running.
# Globals:
#   FLAME_ANY_VERSION_PATTERN
# Outputs:
#   Writes a message to stderr when Flame is running.
# Returns:
#   0 when no Flame is running; otherwise the calling script exits with 1.
#######################################
e2e_abort_if_flame_running() {
    if pgrep -f "$FLAME_ANY_VERSION_PATTERN" >/dev/null 2>&1; then
        echo "Flame is already running (any version). Aborting. Quit Flame, then run this again." >&2
        exit 1
    fi
}

#######################################
# Deploys the stub to dev_hooks and the bridge body to bridge_dev (never the production bridge/).
# Globals:
#   SCRIPT_DIR
# Outputs:
#   Writes a banner and the output of deploy_bridge_dev.sh to stdout.
# Returns:
#   The exit status of deploy_bridge_dev.sh.
#######################################
e2e_deploy() {
    echo "=== deploying the stub to dev_hooks and the bridge body to bridge_dev ==="
    "$SCRIPT_DIR/deploy_bridge_dev.sh"
}

#######################################
# Removes bridge sockets left by an earlier run.
#
# A leftover socket is harmless (probe-before-bind), but removing it first makes
# "a socket appeared" mean "this launch made it".
# Globals:
#   RUN_DIR
# Returns:
#   0, whether or not there was a socket to remove.
#######################################
e2e_clear_sockets() {
    rm -f "$RUN_DIR"/flame-mcp-*.sock 2>/dev/null || true
}

#######################################
# Records where app.log, shell.log and the senddmp processes stood before Flame is launched.
# Globals:
#   APP_LOG
#   SHELL_LOG
#   SENDDMP_PATTERN
#   app_log_lines_before (set)
#   app_log_inode_before (set)
#   shell_log_lines_before (set)
#   shell_log_inode_before (set)
#   senddmp_pids_before (set)
#######################################
e2e_snapshot_logs() {
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
}

#######################################
# Starts a one-launch run: creates the results directory, deploys, clears sockets and takes the
# log snapshot.
# Globals:
#   RESULTS_DIR (and everything that e2e_deploy, e2e_clear_sockets and e2e_snapshot_logs use)
#######################################
e2e_prepare() {
    mkdir -p "$RESULTS_DIR"
    e2e_deploy
    e2e_clear_sockets
    e2e_snapshot_logs
}

#######################################
# Prints the first line of shell.log that belongs to this run; 1 if the file was rotated.
# Globals:
#   SHELL_LOG
#   shell_log_inode_before
#   shell_log_lines_before
# Outputs:
#   Writes the line number to stdout (0 when shell.log does not exist).
# Returns:
#   0 when shell.log exists, 1 when it does not.
#######################################
_e2e_shell_log_start_line() {
    local lines_now inode_now
    [ -f "$SHELL_LOG" ] || {
        echo 0
        return 1
    }
    lines_now=$(wc -l <"$SHELL_LOG" | tr -d ' ')
    inode_now=$(stat -f "%i" "$SHELL_LOG" 2>/dev/null || echo "")
    if [ "$inode_now" != "$shell_log_inode_before" ] || [ "$lines_now" -lt "$shell_log_lines_before" ]; then
        echo 1
    else
        echo $((shell_log_lines_before + 1))
    fi
}

#######################################
# Tells whether shell.log has "abnormal termination" in the lines added since the snapshot.
#
# /usr/bin/grep on purpose: an interactive shell may alias grep. -a because shell.log can hold
# non-text bytes, and without it grep treats the file as binary and quietly reports no match.
# Globals:
#   SHELL_LOG
# Returns:
#   0 if an abnormal termination was found, 1 if not (or if shell.log does not exist).
#######################################
e2e_check_crashed() {
    local start_line
    start_line=$(_e2e_shell_log_start_line) || return 1
    tail -n "+${start_line}" "$SHELL_LOG" | /usr/bin/grep -a -q 'abnormal termination'
}

#######################################
# Launches Flame with the dev hooks and waits for its process.
#
# Sets outcome to launch_failed when open fails or no process shows up within BIN_WAIT_TIMEOUT.
# Globals:
#   FLAME_APP
#   PROJECT
#   DEV_HOOKS_DIR
#   BRIDGE_DEV_DIR
#   FLAME_BIN_PATTERN
#   BIN_WAIT_TIMEOUT
#   POLL_INTERVAL
#   E2E_TAG
#   flame_pid (set)
#   outcome (set)
#   e2e_exit_code (set)
#   crash_seen (set)
# Arguments:
#   $1: the file that receives the output of open
# Outputs:
#   Writes progress to stdout and the launch-failure messages to stderr.
# Returns:
#   0 always; a failed launch shows in outcome.
#######################################
e2e_launch_flame() {
    local launch_log="$1"
    local open_status=0 bin_wait_elapsed=0 candidate

    flame_pid=""
    outcome="unknown"
    e2e_exit_code="N/A"
    crash_seen=0

    echo "=== ${E2E_TAG}starting Flame (DL_PYTHON_HOOK_PATH=$DEV_HOOKS_DIR, FLAME_MCP_BRIDGE_DIR=$BRIDGE_DEV_DIR, /opt/Autodesk is not touched) ==="
    open -n -a "$FLAME_APP" \
        --env "DL_PYTHON_HOOK_PATH=$DEV_HOOKS_DIR" \
        --env "FLAME_MCP_BRIDGE_DIR=$BRIDGE_DEV_DIR" \
        --env "DL_DEBUG_PYTHON_HOOKS=1" \
        --args \
        --start-project="$PROJECT" \
        --start-workspace=Workspace --create-workspace --no-confirm \
        >"$launch_log" 2>&1 || open_status=$?
    echo "open: exit=${open_status}  launch log: $launch_log"

    if [ "$open_status" -ne 0 ]; then
        echo "open failed (exit=${open_status}). Treating this as a launch failure. See the launch log named above." >&2
        outcome="launch_failed"
        return 0
    fi

    echo "Waiting up to ${BIN_WAIT_TIMEOUT}s for the Flame process."
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
}

#######################################
# Waits for a bridge socket without printing anything.
# Globals:
#   RUN_DIR
#   SOCKET_WAIT_S
# Returns:
#   0 as soon as a bridge socket exists, 1 after SOCKET_WAIT_S seconds.
#######################################
e2e_poll_socket() {
    local waited=0
    while [ "$waited" -lt "$SOCKET_WAIT_S" ]; do
        if compgen -G "$RUN_DIR/flame-mcp-*.sock" >/dev/null; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

#######################################
# Runs e2e_poll_socket with messages; sets outcome to launch_failed when no socket appears.
# Globals:
#   E2E_TAG
#   SOCKET_WAIT_S
#   outcome (set on failure)
# Outputs:
#   Writes a banner to stdout and the failure message to stderr.
# Returns:
#   0 when a socket appeared, 1 when none did.
#######################################
e2e_wait_for_socket() {
    echo "=== ${E2E_TAG}waiting for the bridge socket (up to ${SOCKET_WAIT_S}s) ==="
    if e2e_poll_socket; then
        return 0
    fi
    echo "No socket within ${SOCKET_WAIT_S}s. Treating this as a launch failure. See app.log and bridge.log in the report below." >&2
    outcome="launch_failed"
    return 1
}

#######################################
# Runs a client script with the .venv Python.
# Globals:
#   VENV_PYTHON
#   e2e_exit_code (set)
# Arguments:
#   $1: the client script
#   $2...: arguments for the client
# Outputs:
#   Writes "<client file name> exit=<status>" to stdout.
# Returns:
#   0 always; the client's own status is in e2e_exit_code.
#######################################
e2e_run_client() {
    local client="$1"
    shift
    if "$VENV_PYTHON" "$client" "$@"; then
        e2e_exit_code=0
    else
        e2e_exit_code=$?
    fi
    echo "$(basename "$client") exit=$e2e_exit_code"
}

#######################################
# When E2E_CRASH_CHECK=1, looks for a crash right after the client returned.
#
# e2e_dispose_flame turns a clean exit into "crashed" if this saw one.
# Globals:
#   E2E_CRASH_CHECK
#   crash_seen (set)
# Outputs:
#   Writes the result of the check to stdout.
# Returns:
#   0 always.
#######################################
e2e_probe_crash() {
    [ "$E2E_CRASH_CHECK" = 1 ] || return 0
    if e2e_check_crashed; then
        crash_seen=1
        echo "shell.log abnormal termination (lines after launch): yes"
    else
        echo "shell.log abnormal termination (lines after launch): no"
    fi
}

#######################################
# Waits for Flame to exit by itself.
#
# Sets outcome to exited, crashed (shell.log shows an abnormal termination) or timeout.
# Globals:
#   flame_pid
#   POLL_INTERVAL
#   outcome (set)
# Arguments:
#   $1: the number of seconds to wait
# Outputs:
#   Writes a progress line to stdout.
# Returns:
#   0 always.
#######################################
e2e_wait_for_exit() {
    local timeout="$1" elapsed=0
    echo "Waiting up to ${timeout}s for Flame to exit by itself (flame.exit())."
    outcome="timeout"
    while [ "$elapsed" -lt "$timeout" ]; do
        if ! kill -0 "$flame_pid" 2>/dev/null; then
            outcome="exited"
            break
        fi
        if e2e_check_crashed; then
            outcome="crashed"
            break
        fi
        sleep "$POLL_INTERVAL"
        elapsed=$((elapsed + POLL_INTERVAL))
    done
}

#######################################
# Acts on outcome. Does nothing after a clean exit or a failed launch. After a crash, takes a
# sample, sends TERM, waits TERM_WAIT seconds, and sends KILL if Flame is still alive. After a
# timeout (a possible deadlock), takes a sample and sends KILL at once.
#
# A clean exit is first changed to crashed when crash_seen is 1.
# Globals:
#   flame_pid
#   RESULTS_DIR
#   ts
#   E2E_SAMPLE_SUFFIX
#   SAMPLE_BIN
#   TERM_WAIT
#   crash_seen
#   outcome (read and set)
# Outputs:
#   Writes what it did, and the sample file name, to stdout.
# Returns:
#   0 always.
#######################################
e2e_dispose_flame() {
    local crash_sample_file timeout_sample_file

    if [ "$crash_seen" -eq 1 ] && [ "$outcome" = "exited" ]; then
        outcome="crashed"
    fi

    case "$outcome" in
        exited)
            echo "Flame exited by itself."
            ;;
        crashed)
            echo "Found abnormal termination in shell.log (only lines after launch are checked)."
            crash_sample_file="$RESULTS_DIR/crash_sample_${ts}${E2E_SAMPLE_SUFFIX}.txt"
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
        timeout)
            echo "TIMEOUT (possible deadlock). Taking a sample, then killing Flame."
            if [ -n "$flame_pid" ]; then
                timeout_sample_file="$RESULTS_DIR/timeout_sample_${ts}${E2E_SAMPLE_SUFFIX}.txt"
                "$SAMPLE_BIN" "$flame_pid" 1 -file "$timeout_sample_file" >/dev/null 2>&1 || true
                kill -KILL "$flame_pid" 2>/dev/null || true
            fi
            ;;
        launch_failed)
            :  # only the launcher clean-up below
            ;;
    esac
}

#######################################
# Sends TERM to the senddmp (crash reporter) processes that appeared after launch, so they do
# not send anything. Ones that were already running are left alone.
# Globals:
#   SENDDMP_PATTERN
#   senddmp_pids_before
# Outputs:
#   Writes the pids it signalled to stdout.
# Returns:
#   0 always.
#######################################
e2e_reap_senddmp() {
    local senddmp_pids_after new_senddmp_pids pid
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
}

#######################################
# Prints the exit route line. regress.sh and the tests read this line, so keep its form.
#
# The line is "=== <E2E_TAG>exit route: <outcome> (<client> exit=<status>[, shell.log abnormal
# termination: <0|1>]) ==="; the crash flag is there only when E2E_CRASH_CHECK=1.
# Globals:
#   outcome
#   e2e_exit_code
#   crash_seen
#   E2E_CRASH_CHECK
#   E2E_TAG
# Arguments:
#   $1: the client script
# Outputs:
#   Writes the line to stdout.
# Returns:
#   0 always.
#######################################
e2e_report_exit_route() {
    local detail
    detail="$(basename "$1") exit=${e2e_exit_code}"
    if [ "$E2E_CRASH_CHECK" = 1 ]; then
        detail="$detail, shell.log abnormal termination: $crash_seen"
    fi
    echo "=== ${E2E_TAG}exit route: $outcome ($detail) ==="
}

#######################################
# Prints OK when no bridge socket remains and NG when one does (expected after a crash or kill).
# Globals:
#   RUN_DIR
#   E2E_TAG
# Outputs:
#   Writes the verdict and the leftover sockets to stdout.
# Returns:
#   0 always.
#######################################
e2e_report_sockets_gone() {
    local remaining_socks
    echo "--- ${E2E_TAG}sockets must be gone (app_exited check) ---"
    remaining_socks=$(ls "$RUN_DIR"/flame-mcp-*.sock 2>/dev/null || true)
    if [ -z "$remaining_socks" ]; then
        echo "OK: no sockets remain."
    else
        echo "NG: sockets remain (expected when Flame did not exit cleanly):"
        echo "$remaining_socks"
    fi
}

#######################################
# Prints the DIALOG / [PYTHON HOOK] lines about the MCP bridge written to app.log since launch.
# Globals:
#   APP_LOG
#   app_log_lines_before
#   app_log_inode_before
#   E2E_TAG
# Outputs:
#   Writes the matching lines to stdout.
# Returns:
#   0 always.
#######################################
e2e_report_app_log() {
    local app_log_lines_after app_log_inode_after start_line
    if [ ! -f "$APP_LOG" ]; then
        echo "--- app.log not found: $APP_LOG ---"
        return 0
    fi
    app_log_lines_after=$(wc -l <"$APP_LOG" | tr -d ' ')
    app_log_inode_after=$(stat -f "%i" "$APP_LOG" 2>/dev/null || echo "")
    start_line=$((app_log_lines_before + 1))
    if [ "$app_log_inode_after" != "$app_log_inode_before" ] ||
        [ "$app_log_lines_after" -lt "$app_log_lines_before" ]; then
        echo "(note: app.log seems to have been rotated; showing it from the top)"
        start_line=1
    fi
    echo "--- ${E2E_TAG}app.log since launch: DIALOG / [PYTHON HOOK] lines about the MCP bridge ---"
    # -a: app.log can hold non-text bytes. Alternation with -E, not \|.
    tail -n "+${start_line}" "$APP_LOG" | /usr/bin/grep -aE 'DIALOG :|\[PYTHON HOOK\]' |
        /usr/bin/grep -aiE 'flame_mcp_bridge|MCP Bridge' || echo "(no matching lines)"
}

#######################################
# Prints the last 40 lines of bridge.log.
# Globals:
#   APP_SUPPORT
#   E2E_TAG
# Outputs:
#   Writes the lines to stdout.
# Returns:
#   0 always.
#######################################
e2e_report_bridge_log() {
    local bridge_log="$APP_SUPPORT/logs/bridge.log"
    if [ -f "$bridge_log" ]; then
        echo "--- ${E2E_TAG}last 40 lines of bridge.log ---"
        tail -n 40 "$bridge_log"
    else
        echo "--- bridge.log not found: $bridge_log ---"
    fi
}

#######################################
# Prints the newest "<RESULTS_DIR>/<glob>" file and its content.
#
# With "yes", per-stage copies (..._stage_<x>.json) are skipped so the whole-run result file
# is the one shown.
# Globals:
#   RESULTS_DIR
#   E2E_TAG
# Arguments:
#   $1: the client script
#   $2: the result file glob
#   $3: yes or no (skip the per-stage files)
# Outputs:
#   Writes the file name and content to stdout.
# Returns:
#   0 always.
#######################################
e2e_report_latest_result() {
    local client="$1" glob="$2" skip_stage_files="$3" latest_result
    echo "--- ${E2E_TAG}result file of $(basename "$client") (latest) ---"
    if [ "$skip_stage_files" = yes ]; then
        # shellcheck disable=SC2086,SC2012 # the glob must expand; result names come from our client scripts and never hold a newline
        latest_result=$(ls -t "$RESULTS_DIR"/$glob 2>/dev/null | /usr/bin/grep -v '_stage_' | head -1 || true)
    else
        # shellcheck disable=SC2086,SC2012 # the glob must expand; result names come from our client scripts and never hold a newline
        latest_result=$(ls -t "$RESULTS_DIR"/$glob 2>/dev/null | head -1 || true)
    fi
    if [ -n "$latest_result" ]; then
        echo "$latest_result"
        cat "$latest_result"
    else
        echo "no result file found."
    fi
}

#######################################
# Does everything after the client: disposes of Flame, reaps senddmp, then prints the report.
# The caller adds its own lines in between where its report differs.
# Globals:
#   flame_pid
#   outcome (read and set)
#   crash_seen
#   e2e_exit_code
#   ts
#   RESULTS_DIR
#   RUN_DIR
#   APP_LOG
#   APP_SUPPORT
#   SAMPLE_BIN
#   TERM_WAIT
#   SENDDMP_PATTERN
#   senddmp_pids_before
#   app_log_lines_before
#   app_log_inode_before
#   E2E_SAMPLE_SUFFIX
#   E2E_CRASH_CHECK
#   E2E_TAG
# Arguments:
#   $1: the client script
#   $2: the result file glob
#   $3: yes or no (skip the per-stage files)
# Outputs:
#   Writes the report to stdout.
# Returns:
#   0 always.
#######################################
e2e_finish() {
    e2e_dispose_flame
    e2e_reap_senddmp
    e2e_report_exit_route "$1"
    e2e_report_sockets_gone
    e2e_report_app_log
    e2e_report_bridge_log
    e2e_report_latest_result "$1" "$2" "$3"
}
