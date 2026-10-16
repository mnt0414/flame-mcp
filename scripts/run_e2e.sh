#!/usr/bin/env bash
# Run one real-Flame e2e by name.
#
# Usage:
#   scripts/run_e2e.sh <name> [TIMEOUT]       run it (TIMEOUT: seconds to wait for Flame to exit
#                                             by itself after the client returns; default 600)
#   scripts/run_e2e.sh --list                 the names, in the order scripts/regress.sh runs them
#   scripts/run_e2e.sh --print-config <name>  what <name> resolves to, as key=value lines
#                                             (starts nothing)
#
# <name> is the client script's name without "_e2e.py": scripts/<name>_e2e.py. The run
# deploys the bridge to the dev directories (never the production bridge/), starts Flame with
# DL_PYTHON_HOOK_PATH and FLAME_MCP_BRIDGE_DIR (nothing under /opt/Autodesk is touched), runs the
# client once the bridge socket exists, waits for Flame to exit by itself, and prints the exit
# route and the logs (see e2e_lib.sh). It aborts if any Flame is already running.
#
# Three e2es need their own orchestrator (a different order of steps or two Flame launches):
# p1_bridge, p1_transport and p2_4_restart. They are listed here and started through their own
# run_<name>_e2e.sh, which uses e2e_lib.sh for the steps that are the same.
#
# Per-name differences are the table below and nothing else:
#   resultsDir   results directory under ~/Library/Application Support/flame-mcp/
#   stageFilter  yes: the result listing skips the per-stage copies (..._stage_<x>.json)
#   extraArgs    yes: E2E_EXTRA_ARGS (split on whitespace) is passed on to the client
#   crashCheck   yes: after the client returns, shell.log is checked for an abnormal
#                termination, and a clean exit with one is reported as "crashed"
#   precondition ENV=: after the check that no Flame is running (exit 1 if one is), the run is
#                skipped (exit 0, Flame not started) unless $ENV is set to the path of an
#                existing file. There is no default path.
#   stages       only for the "running ..." line
#
# FLAME_MCP_W3_SETUP (w3_timewarp only): the setup file of a segment with a Timewarp, saved from
# the Flame UI. The client copies it into its working folder and loads it onto a segment. There
# is no default: while the variable is unset or empty, the run is skipped. This script and
# scripts/w3_timewarp_e2e.py read the same variable.
#
# This script starts Flame itself.
set -euo pipefail

# shellcheck source=e2e_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/e2e_lib.sh"

DEFAULT_TIMEOUT=600

# name|runner|resultsDir|stageFilter|extraArgs|crashCheck|precondition|stages
e2e_table() {
    cat <<'EOF'
p3_3_segment|standard|p3|yes|no|no|-|setup, stages s, r, e, x, p, y, z
p3_2b_plan_delete|standard|p3|yes|no|no|-|setup, stages p, d, m, k, r, g, z
p3_2a_checkpoint|standard|p3|yes|no|no|-|setup, stages a-k, z
p3_1b_write|standard|p3|yes|no|no|-|setup, stages a, a2, b, c, e, g, z
p3_1a_read|standard|p3|yes|no|no|-|setup, stages a-g, z
p1_bridge|dedicated|-|-|-|-|-|-
p1_transport|dedicated|-|-|-|-|-|-
p1_mcp|standard|p1|no|no|no|-|-
p2_1_attr|standard|p2|no|no|no|-|-
p2_2a_journal|standard|p2|yes|no|no|-|stages a-f, z
p2_2b_plan|standard|p2|yes|no|no|-|stages a-f, z
p2_2c_checkpoint_eval|standard|p2|yes|no|no|-|stages a-e, z
p2_3a_batch|standard|p2|yes|no|no|-|stages a-e, z
p2_3b_node_action|standard|p2|yes|no|no|-|stages a-f, z
p2_3r_reel_fidelity|standard|p2|yes|no|no|-|stages a-e, z
p2_3c_render|standard|p2|yes|no|no|-|setup, stages a-f, z
p2_3d_desktop|standard|p2|yes|no|no|-|setup, stages a-d, f, z
p2_4_scenario4|standard|p2|yes|yes|yes|-|all stages
p2_4_stress|standard|p2|yes|yes|yes|-|all stages
p2_4_restart|dedicated|-|-|-|-|-|-
p3_4_construct|standard|p3|yes|no|no|-|setup, stages o, i, k, v, c, p, y, z
p3_5_scenarios|standard|p3|yes|no|no|-|setup, stages a, n, b, z
fix2_library_batch|standard|p3|yes|no|no|-|setup, stages l, b, r, d, c, e, g, z
fix3_reel_order|standard|p3|yes|no|no|-|setup, stages n, c, z
fix5_timeline_selector|standard|p3|yes|no|no|-|setup, stages a, t, p, s, z
fix6_same_name_nodes|standard|p3|yes|no|no|-|setup, stages n, c, z
fix7_small_items|standard|p3|yes|no|no|-|setup, stages a, b, c, d, z
w3_timewarp|standard|p3|yes|no|no|FLAME_MCP_W3_SETUP=|setup, stages t, r, n, z
EOF
}

usage() {
    echo "Usage: $0 <name> [TIMEOUT] | --list | --print-config <name>" >&2
    exit 2
}

# lookup <name>: set the cfg_* variables from the table; return 1 for an unknown name.
lookup() {
    local row_name
    while IFS='|' read -r row_name cfg_runner cfg_results_dir cfg_stage_filter cfg_extra_args \
        cfg_crash_check cfg_precondition cfg_stages; do
        if [ "$row_name" = "$1" ]; then
            cfg_name="$row_name"
            return 0
        fi
    done < <(e2e_table)
    return 1
}

# Name of the precondition variable for the looked-up name, empty if it has none.
precondition_var() {
    [ "$cfg_precondition" = "-" ] || echo "${cfg_precondition%%=*}"
}

precondition_path() {
    local var rel
    [ "$cfg_precondition" = "-" ] && return 0
    var="${cfg_precondition%%=*}"
    rel="${cfg_precondition#*=}"
    if [ -z "$rel" ]; then
        echo "${!var:-}"
    else
        echo "${!var:-$APP_SUPPORT/$rel}"
    fi
}

print_config() {
    local pre
    echo "name=$cfg_name"
    echo "runner=$cfg_runner"
    if [ "$cfg_runner" = dedicated ]; then
        echo "script=$SCRIPT_DIR/run_${cfg_name}_e2e.sh"
        echo "client=$SCRIPT_DIR/${cfg_name}_e2e.py"
        return 0
    fi
    pre="$(precondition_path)"
    echo "client=$SCRIPT_DIR/${cfg_name}_e2e.py"
    echo "resultsDir=$APP_SUPPORT/$cfg_results_dir"
    echo "resultGlob=${cfg_name}_e2e_*.json"
    echo "stageFilter=$cfg_stage_filter"
    echo "extraArgs=$cfg_extra_args"
    echo "crashCheck=$cfg_crash_check"
    if [ -n "$pre" ]; then
        echo "precondition=file:$pre"
    elif [ "$cfg_precondition" != "-" ]; then
        echo "precondition=env:$(precondition_var)"
    else
        echo "precondition=none"
    fi
    echo "timeout=$DEFAULT_TIMEOUT"
    if [ "$cfg_stages" = "-" ]; then
        echo "stages="
    else
        echo "stages=$cfg_stages"
    fi
}

unknown_name() {
    echo "Unknown e2e name: $1 (see $0 --list)" >&2
    exit 2
}

run_standard() {
    local timeout="$1"
    local client results_dir pre pre_var stages_note
    local -a extra_args=()

    client="$SCRIPT_DIR/${cfg_name}_e2e.py"
    results_dir="$APP_SUPPORT/$cfg_results_dir"

    e2e_abort_if_flame_running

    pre="$(precondition_path)"
    if [ "$cfg_precondition" != "-" ]; then
        pre_var="$(precondition_var)"
        if [ -z "$pre" ]; then
            echo "SKIP: $pre_var is not set. Flame is not started. Set $pre_var to the setup file of a segment with a Timewarp." >&2
            exit 0
        fi
        if [ ! -f "$pre" ]; then
            echo "SKIP: required file not found: $pre. Flame is not started. Set $pre_var to the path of the file." >&2
            exit 0
        fi
    fi

    e2e_init "$results_dir"
    if [ "$cfg_crash_check" = yes ]; then
        E2E_CRASH_CHECK=1
    fi
    if [ "$cfg_extra_args" = yes ]; then
        read -r -a extra_args <<<"${E2E_EXTRA_ARGS:-}"
    fi

    e2e_prepare
    ts=$(date +%y%m%d_%H%M%S)
    e2e_launch_flame "$results_dir/launch_${ts}.log"

    if [ -n "$flame_pid" ]; then
        echo "Flame: pid=$flame_pid"
        if e2e_wait_for_socket; then
            stages_note=""
            if [ "$cfg_stages" != "-" ]; then
                stages_note=" ($cfg_stages; each stage starts a flame_mcp subprocess)"
            fi
            echo "=== running $(basename "$client")${stages_note} ==="
            e2e_run_client "$client" --run-dir "$RUN_DIR" ${extra_args[@]+"${extra_args[@]}"}
            e2e_probe_crash
            e2e_wait_for_exit "$timeout"
        fi
    fi

    e2e_finish "$client" "${cfg_name}_e2e_*.json" "$cfg_stage_filter"
}

main() {
    local timeout

    case "${1:-}" in
        "" | -h | --help)
            usage
            ;;
        --list)
            e2e_table | cut -d'|' -f1
            return 0
            ;;
        --print-config)
            [ $# -eq 2 ] || usage
            lookup "$2" || unknown_name "$2"
            print_config
            return 0
            ;;
    esac

    [ $# -le 2 ] || usage
    lookup "$1" || unknown_name "$1"
    timeout="${2:-$DEFAULT_TIMEOUT}"
    case "$timeout" in
        '' | *[!0-9]*)
            echo "TIMEOUT must be a number of seconds: $timeout" >&2
            exit 2
            ;;
    esac

    if [ "$cfg_runner" = dedicated ]; then
        exec "$SCRIPT_DIR/run_${cfg_name}_e2e.sh" "$timeout"
    fi
    run_standard "$timeout"
}

main "$@"
