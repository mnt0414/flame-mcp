#!/usr/bin/env bash
# Run the real-Flame regression: the e2es one after another through scripts/run_e2e.sh, then clean
# up what the test project was left with. Each e2e starts and stops Flame itself.
#
# Usage: scripts/regress.sh [name ...]
#   Without names, every e2e that `scripts/run_e2e.sh --list` prints (28), in that order.
#
# Output goes to ~/Library/Application Support/flame-mcp/p3/regress_<yymmdd_HHMMSS>/:
#   <name>_<n>.log   the full output of one run
#   summary.txt      one line per run: <name>#<n> start=<HH:MM:SS> rc=<exit code>
#                    abnormal=<lines reporting an abnormal termination> <the exit route line>
#                    a run with no exit route line reads "SKIPPED: <reason>" when run_e2e.sh
#                    skipped it on its own (a SKIP line, e.g. w3_timewarp without its setup
#                    file) and "route=NONE" otherwise (e.g. aborted because Flame was running)
#                    a run that was not started because Flame was already running reads
#                    "<name>#<n> start=<time> SKIPPED: Flame is already running" and ends the
#                    loop; the last lines are the clean-up result and DONE
#   clean.log        the output of the clean-up
# regress_latest.txt in the p3 directory holds the path of the newest output directory.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
P3="$HOME/Library/Application Support/flame-mcp/p3"
FLAME_RUNNING_PATTERN='flame.app/Contents/MacOS/flame'
TIMEOUT_S=900
# The route line is "exit route", printed by e2e_lib.sh and by run_flame_with_bridge.sh (the
# clean-up).
ROUTE_PATTERN='exit route'

# route_of <log>: the last route line of a run; "SKIPPED: <reason>" when run_e2e.sh skipped the run
# (a SKIP line, e.g. a missing precondition file); "route=NONE" when the log has neither.
route_of() {
    local route skip
    route="$(grep -hE "$ROUTE_PATTERN" "$1" | tail -1)"
    if [ -n "$route" ]; then
        echo "$route"
        return 0
    fi
    skip="$(grep -m1 '^SKIP: ' "$1" || true)"
    if [ -n "$skip" ]; then
        echo "SKIPPED: ${skip#SKIP: }"
    else
        echo "route=NONE"
    fi
}

cd "$REPO" || exit 1

if [ "$#" -gt 0 ]; then
    NAMES=("$@")
else
    NAMES=()
    while IFS= read -r listed; do
        NAMES+=("$listed")
    done < <(bash scripts/run_e2e.sh --list)
fi
for n in "${NAMES[@]}"; do
    if ! bash scripts/run_e2e.sh --print-config "$n" >/dev/null 2>&1; then
        echo "Unknown e2e name: $n (see scripts/run_e2e.sh --list)" >&2
        exit 2
    fi
done

TS="$(date +%y%m%d_%H%M%S)"
OUT="$P3/regress_$TS"
mkdir -p "$OUT"
echo "$OUT" >"$P3/regress_latest.txt"

for n in "${NAMES[@]}"; do
    i=1
    while [ -e "$OUT/${n}_$i.log" ]; do i=$((i + 1)); done
    log="$OUT/${n}_$i.log"
    start="$(date +%H:%M:%S)"
    if pgrep -f "$FLAME_RUNNING_PATTERN" >/dev/null; then
        echo "$n#$i start=$start SKIPPED: Flame is already running" >>"$OUT/summary.txt"
        break
    fi
    bash scripts/run_e2e.sh "$n" "$TIMEOUT_S" >"$log" 2>&1
    rc=$?
    route="$(route_of "$log")"
    abn="$(grep -cE 'abnormal termination \(lines after launch\): yes|abnormal termination: [1-9]' "$log")"
    echo "$n#$i start=$start rc=$rc abnormal=$abn $route" >>"$OUT/summary.txt"
done

# Always clean up the test project leftovers last (the script checks the project name first).
if pgrep -f "$FLAME_RUNNING_PATTERN" >/dev/null; then
    echo "clean SKIPPED: Flame is already running" >>"$OUT/summary.txt"
else
    bash scripts/run_flame_with_bridge.sh scripts/p2_4_clean_probe_project.py "$TIMEOUT_S" -- --mode apply >"$OUT/clean.log" 2>&1
    clean_rc=$?
    echo "clean rc=$clean_rc $(route_of "$OUT/clean.log")" >>"$OUT/summary.txt"
fi
echo DONE >>"$OUT/summary.txt"
