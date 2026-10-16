#!/usr/bin/env bash
#
# Diagnoses the production install, as a table. It writes nothing.
#
# What it checks:
#   - stub: whether /opt/Autodesk/shared/python/flame_mcp_bridge.py is a regular file and its
#     sha256 equals the repository stub's (a mismatch or a missing file is a FAIL)
#   - impl: the sha256 of the production bridge body,
#     ~/Library/Application Support/flame-mcp/bridge/impl.py
#   - impl_dev: the sha256 of the development bridge body,
#     ~/Library/Application Support/flame-mcp/bridge_dev/impl.py (where
#     scripts/deploy_bridge_dev.sh deploys), on its own row
#   - socket: whether sockets exist in run/ and whether they answer (hello)
#   - inflight: how many in-flight records (run/inflight/*.json) there are
#   - dev_hooks: whether ~/Library/Application Support/flame-mcp/dev_hooks/flame_mcp_bridge.py
#     exists. If it does, its sha256 is compared with the production stub's (INFO if equal, WARN
#     if different or not comparable: when Flame is started for development, only one of the two
#     modules named flame_mcp_bridge gets loaded)
#
# Exit status: 0 when there is no FAIL (WARN and INFO do not count), 1 otherwise.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

STUB_REPO="$REPO_ROOT/stub/flame_mcp_bridge.py"
IMPL_REPO="$REPO_ROOT/src/flame_mcp_bridge/impl.py"

STUB_DST="/opt/Autodesk/shared/python/flame_mcp_bridge.py"
APP_SUPPORT="$HOME/Library/Application Support/flame-mcp"
IMPL_DST="$APP_SUPPORT/bridge/impl.py"
# shellcheck source=bridge_dev_env.sh
source "$SCRIPT_DIR/bridge_dev_env.sh"
IMPL_DEV_DST="$BRIDGE_DEV_DIR/impl.py"
RUN_DIR="$APP_SUPPORT/run"
INFLIGHT_DIR="$RUN_DIR/inflight"
DEV_HOOKS_STUB="$APP_SUPPORT/dev_hooks/flame_mcp_bridge.py"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

overall_status=0  # 0 = all OK, 1 = at least one FAIL (WARN alone does not raise it)

_sha256() {
    shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
}

_row() {
    # $1=item $2=status $3=detail
    printf '%-10s %-6s %s\n' "$1" "$2" "$3"
}

echo "=== flame-mcp doctor ==="
printf '%-10s %-6s %s\n' "Item" "Status" "Detail"
printf '%-10s %-6s %s\n' "----" "----" "----"

# -- stub ------------------------------------------------------------------------------------------
if [ -L "$STUB_DST" ]; then
    _row "stub" "FAIL" "symlink: ${STUB_DST} (hooks must be real files, not symlinks)"
    overall_status=1
elif [ ! -e "$STUB_DST" ]; then
    _row "stub" "FAIL" "not found: $STUB_DST"
    overall_status=1
elif [ ! -f "$STUB_DST" ]; then
    _row "stub" "FAIL" "not a regular file: $STUB_DST"
    overall_status=1
else
    stub_dst_sha="$(_sha256 "$STUB_DST")"
    stub_repo_sha="$(_sha256 "$STUB_REPO")"
    if [ "$stub_dst_sha" = "$stub_repo_sha" ]; then
        _row "stub" "OK" "sha256 matches ($stub_dst_sha)"
    else
        _row "stub" "FAIL" "sha256 differs (deployed=$stub_dst_sha repository=$stub_repo_sha)"
        overall_status=1
    fi
fi

# -- impl ------------------------------------------------------------------------------------------
if [ -L "$IMPL_DST" ]; then
    _row "impl" "WARN" "symlink: ${IMPL_DST} (the default is to deploy by copying)"
elif [ -f "$IMPL_DST" ]; then
    impl_dst_sha="$(_sha256 "$IMPL_DST")"
    impl_repo_sha="$(_sha256 "$IMPL_REPO")"
    if [ "$impl_dst_sha" = "$impl_repo_sha" ]; then
        _row "impl" "OK" "sha256=${impl_dst_sha} (matches the repository)"
    else
        _row "impl" "INFO" "sha256=${impl_dst_sha} (differs from the repository: $impl_repo_sha; you may need to redeploy)"
    fi
else
    _row "impl" "WARN" "not found: $IMPL_DST"
fi

if [ -L "$IMPL_DEV_DST" ]; then
    _row "impl_dev" "WARN" "symlink: ${IMPL_DEV_DST} (the development target should be a copy)"
elif [ -f "$IMPL_DEV_DST" ]; then
    impl_dev_sha="$(_sha256 "$IMPL_DEV_DST")"
    impl_repo_sha="$(_sha256 "$IMPL_REPO")"
    if [ "$impl_dev_sha" = "$impl_repo_sha" ]; then
        _row "impl_dev" "OK" "sha256=${impl_dev_sha} (matches the repository; development copy: ${IMPL_DEV_DST})"
    else
        _row "impl_dev" "INFO" "sha256=${impl_dev_sha} (differs from the repository: ${impl_repo_sha}; expected after deploying an older body with BRIDGE_IMPL_SRC; development copy: ${IMPL_DEV_DST})"
    fi
else
    _row "impl_dev" "INFO" "none: ${IMPL_DEV_DST} (the development target that scripts/deploy_bridge_dev.sh creates)"
fi

# -- sockets in run/ -------------------------------------------------------------------------------
sock_matches=()
if [ -d "$RUN_DIR" ]; then
    while IFS= read -r -d '' f; do
        sock_matches+=("$f")
    done < <(find "$RUN_DIR" -maxdepth 1 -name "flame-mcp-*.sock" -print0 2>/dev/null)
fi
if [ "${#sock_matches[@]}" -eq 0 ]; then
    _row "socket" "WARN" "no sockets in $RUN_DIR (the bridge is not running)"
else
    for sock in "${sock_matches[@]}"; do
        if [ -x "$VENV_PYTHON" ]; then
            hello_out="$("$VENV_PYTHON" - "$sock" <<'PYEOF' 2>&1
import json
import socket
import sys
import uuid

sock_path = sys.argv[1]
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2.0)
        s.connect(sock_path)
        s.sendall((json.dumps({"v": 1, "id": uuid.uuid4().hex, "kind": "hello"}) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    resp = json.loads(buf.decode("utf-8"))
    if resp.get("ok") is True:
        print(f"ALIVE pid={resp.get('pid')} flameVersion={resp.get('flameVersion')} project={resp.get('projectName')}")
    else:
        print(f"DEAD unexpected response: {resp!r}")
except OSError as exc:
    print(f"DEAD {exc}")
PYEOF
)"
            if [[ "$hello_out" == ALIVE* ]]; then
                _row "socket" "OK" "$(basename "$sock"): ${hello_out#ALIVE }"
            else
                _row "socket" "WARN" "$(basename "$sock"): $hello_out"
            fi
        else
            _row "socket" "INFO" "$(basename "$sock") exists (no .venv, so the liveness check was skipped: ${VENV_PYTHON})"
        fi
    done
fi

# -- in-flight records -----------------------------------------------------------------------------
inflight_count=0
if [ -d "$INFLIGHT_DIR" ]; then
    inflight_count=$(find "$INFLIGHT_DIR" -maxdepth 1 -name "*.json" | wc -l | tr -d ' ')
fi
if [ "$inflight_count" -eq 0 ]; then
    _row "inflight" "OK" "none"
else
    _row "inflight" "INFO" "$inflight_count found (records older than 7 days are removed when the server next starts)"
fi

# -- stub in dev_hooks (warns about loading it twice next to the production one) -------------------
if [ ! -e "$DEV_HOOKS_STUB" ] && [ ! -L "$DEV_HOOKS_STUB" ]; then
    _row "dev_hooks" "OK" "none"
elif [ -L "$DEV_HOOKS_STUB" ] || [ ! -f "$DEV_HOOKS_STUB" ]; then
    _row "dev_hooks" "WARN" "not a regular file (a symlink, for example): $DEV_HOOKS_STUB"
else
    dev_stub_sha="$(_sha256 "$DEV_HOOKS_STUB")"
    if [ -L "$STUB_DST" ] || [ ! -f "$STUB_DST" ]; then
        _row "dev_hooks" "WARN" "$DEV_HOOKS_STUB exists, but the production stub ($STUB_DST) is not a regular file, so the sha256 values cannot be compared (dev=$dev_stub_sha)"
    else
        prod_stub_sha="$(_sha256 "$STUB_DST")"
        if [ "$dev_stub_sha" = "$prod_stub_sha" ]; then
            _row "dev_hooks" "INFO" "$DEV_HOOKS_STUB has the same sha256 as the production stub ($dev_stub_sha), so either module named flame_mcp_bridge has the same content"
        else
            _row "dev_hooks" "WARN" "$DEV_HOOKS_STUB has a different sha256 from the production stub (dev=$dev_stub_sha production=$prod_stub_sha). When Flame is started for development, only one of the two modules named flame_mcp_bridge gets loaded"
        fi
    fi
fi

echo "----"
if [ "$overall_status" -eq 0 ]; then
    echo "Overall: OK (no FAIL; check each WARN and INFO row)"
else
    echo "Overall: FAIL (see the FAIL rows above)"
fi

exit "$overall_status"
