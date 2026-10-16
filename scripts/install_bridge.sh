#!/usr/bin/env bash
#
# Production installer.
#
#   stub  -> /opt/Autodesk/shared/python/flame_mcp_bridge.py   (real file, mode 0644)
#   body  -> ~/Library/Application Support/flame-mcp/bridge/impl.py (real file, mode 0600)
#
# A symlink breaks Flame's "Reload Python Hooks", so if a target is a symlink the script refuses
# and does nothing. An existing regular file is copied to
# ~/Library/Application Support/flame-mcp/backups/ before it is replaced.
# Each file is written to a temporary file next to its target and moved into place with mv; both
# temporary files are ready (and compared with the source) before either target is replaced, so
# Flame never reads a half-written file. The stub only loads a body that is owned by the user and
# not writable by others, hence mode 0600 and a bridge directory without group/other write.
# A real install refuses to run while Flame is running (it must not swap the stub or body under a
# running Flame). --dry-run writes nothing, so it only warns and goes on.
#
# Usage: install_bridge.sh [--dry-run]
#   --dry-run  write nothing; only print what would be done (allowed while Flame is running)
#
# FLAME_MCP_STUB_DST replaces the stub target (for tests only; leave it unset).
#
# This installer writes under /opt/Autodesk, so run it yourself, except with --dry-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

STUB_SRC="$REPO_ROOT/stub/flame_mcp_bridge.py"
IMPL_SRC="$REPO_ROOT/src/flame_mcp_bridge/impl.py"

STUB_DST="${FLAME_MCP_STUB_DST:-/opt/Autodesk/shared/python/flame_mcp_bridge.py}"
BRIDGE_DIR="$HOME/Library/Application Support/flame-mcp/bridge"
IMPL_DST="$BRIDGE_DIR/impl.py"

BACKUPS_DIR="$HOME/Library/Application Support/flame-mcp/backups"

STUB_MODE=0644
IMPL_MODE=0600

FLAME_ANY_VERSION_PATTERN='flame\.app/Contents/MacOS/flame( |$)'

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--dry-run]" >&2
            exit 1
            ;;
    esac
done

if [ "$DRY_RUN" -eq 1 ]; then
    echo "=== --dry-run: nothing will be written; showing only what would be done ==="
fi

if [ ! -f "$STUB_SRC" ]; then
    echo "Stub not found: $STUB_SRC. Restore stub/flame_mcp_bridge.py in the repository." >&2
    exit 1
fi
if [ ! -f "$IMPL_SRC" ]; then
    echo "Bridge body not found: $IMPL_SRC. Restore src/flame_mcp_bridge/impl.py in the repository." >&2
    exit 1
fi

# The loader refuses a bridge body that is not owned by the user who runs Flame, so a body
# installed as root would never load.
if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -eq 0 ]; then
    echo "Refused: do not run this as root (for example with sudo). Run it as the user who runs Flame." >&2
    exit 1
fi

if pgrep -f "$FLAME_ANY_VERSION_PATTERN" >/dev/null 2>&1; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "Flame is running. This is a dry run, so it goes on; a real install is refused until Flame quits." >&2
    else
        echo "Flame is running. Quit Flame, then run this again." >&2
        exit 1
    fi
fi

# Symlink check (a symlink breaks Reload Python Hooks). If either target is a symlink, write
# neither, so the install never ends up with only the stub or only the body.
refuse=0
if [ -L "$STUB_DST" ]; then
    echo "Refused: $STUB_DST is a symlink. Replace it with a real file by hand, then run this again." >&2
    refuse=1
fi
if [ -L "$IMPL_DST" ]; then
    echo "Refused: $IMPL_DST is a symlink. Replace it with a real file by hand, then run this again." >&2
    refuse=1
fi
if [ "$refuse" -eq 1 ]; then
    exit 1
fi

ts="$(date +%y%m%d_%H%M%S)"

STAGED_FILES=()
STAGED_PATH=""

_remove_staged() {
    local f
    for f in ${STAGED_FILES[@]+"${STAGED_FILES[@]}"}; do
        rm -f "$f"
    done
}
trap _remove_staged EXIT

_backup_if_exists() {
    # $1 = the target path (the caller has checked that it is a regular file)
    local target="$1"
    local base
    base="$(basename "$target")"
    if [ -f "$target" ]; then
        local backup_path="$BACKUPS_DIR/${base}.pre_install_${ts}"
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] would back up: $target -> $backup_path"
        else
            mkdir -p "$BACKUPS_DIR"
            cp -p "$target" "$backup_path"
            echo "Backed up: $target -> $backup_path"
        fi
    fi
}

_sha256() {
    shasum -a 256 "$1" | awk '{print $1}'
}

_stub_pyc_files() {
    local pycache_dir
    pycache_dir="$(dirname "$STUB_DST")/__pycache__"
    [ -d "$pycache_dir" ] || return 0
    find "$pycache_dir" -maxdepth 1 -type f -name 'flame_mcp_bridge.*.pyc'
}

_stage() {
    # $1 = source, $2 = target, $3 = mode. Puts the temporary file's path in STAGED_PATH. The
    # temporary file sits in the target's directory so that mv is a rename on one filesystem.
    local src="$1" dst="$2" mode="$3" tmp
    tmp="$(mktemp "$(dirname "$dst")/.$(basename "$dst").XXXXXX")"
    STAGED_FILES+=("$tmp")
    STAGED_PATH="$tmp"
    cp "$src" "$tmp"
    chmod "$mode" "$tmp"
    if ! cmp -s "$src" "$tmp"; then
        echo "The temporary file differs from its source: $tmp" >&2
        exit 1
    fi
}

echo "=== stub: $STUB_SRC -> $STUB_DST ==="
_backup_if_exists "$STUB_DST"
echo "=== body: $IMPL_SRC -> $IMPL_DST ==="
_backup_if_exists "$IMPL_DST"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would copy via a temporary file and mv (mode $STUB_MODE): $STUB_SRC -> $STUB_DST"
    echo "[dry-run] would copy via a temporary file and mv (mode $IMPL_MODE): $IMPL_SRC -> $IMPL_DST"
    stub_pycs="$(_stub_pyc_files)"
    if [ -n "$stub_pycs" ]; then
        while IFS= read -r pyc; do
            echo "[dry-run] would delete: $pyc"
        done <<< "$stub_pycs"
    else
        echo "[dry-run] no flame_mcp_bridge.*.pyc to delete"
    fi
fi
if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$(dirname "$STUB_DST")"
    mkdir -p "$BRIDGE_DIR"
    chmod go-w "$BRIDGE_DIR"
    _stage "$STUB_SRC" "$STUB_DST" "$STUB_MODE"
    stub_tmp="$STAGED_PATH"
    _stage "$IMPL_SRC" "$IMPL_DST" "$IMPL_MODE"
    impl_tmp="$STAGED_PATH"
    mv -f "$stub_tmp" "$STUB_DST"
    mv -f "$impl_tmp" "$IMPL_DST"
    _stub_pyc_files | while IFS= read -r pyc; do
        rm -f "$pyc"
    done
    rm -rf "$BRIDGE_DIR/__pycache__"
    echo "Copied: $STUB_DST"
    echo "Copied: $IMPL_DST"
fi

echo "--- sha256 (source) ---"
echo "stub: $(_sha256 "$STUB_SRC")  $STUB_SRC"
echo "impl: $(_sha256 "$IMPL_SRC")  $IMPL_SRC"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "--- sha256 (target as it is now; nothing was written because of --dry-run) ---"
else
    echo "--- sha256 (target, after the copy) ---"
fi
if [ -f "$STUB_DST" ]; then
    stub_dst_sha="$(_sha256 "$STUB_DST")"
    echo "stub: $stub_dst_sha  $STUB_DST"
    if [ "$DRY_RUN" -eq 0 ]; then
        if [ "$stub_dst_sha" = "$(_sha256 "$STUB_SRC")" ]; then
            echo "  OK: matches the repository"
        else
            echo "  NG: differs from the repository" >&2
        fi
    fi
else
    echo "stub: (not deployed)"
fi
if [ -f "$IMPL_DST" ]; then
    impl_dst_sha="$(_sha256 "$IMPL_DST")"
    echo "impl: $impl_dst_sha  $IMPL_DST"
    if [ "$DRY_RUN" -eq 0 ]; then
        if [ "$impl_dst_sha" = "$(_sha256 "$IMPL_SRC")" ]; then
            echo "  OK: matches the repository"
        else
            echo "  NG: differs from the repository" >&2
        fi
    fi
else
    echo "impl: (not deployed)"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo "=== --dry-run finished: nothing was written ==="
else
    echo "=== Install finished. Start Flame: the MCP Bridge menu comes up at app_initialized on the first launch ==="
fi
