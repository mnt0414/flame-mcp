#!/usr/bin/env bash
#
# Removes what install_bridge.sh deployed.
#
#   stub  /opt/Autodesk/shared/python/flame_mcp_bridge.py and the flame_mcp_bridge.*.pyc files
#         in its __pycache__
#   body  impl.py and __pycache__ in ~/Library/Application Support/flame-mcp/bridge/ (the
#         directory itself only when nothing else is left in it)
#
# Regular files are copied to ~/Library/Application Support/flame-mcp/backups/ first (the same
# place as install_bridge.sh, with the suffix pre_uninstall_). A symlink is never removed, only
# reported, and the exit status is then 1. A real uninstall refuses to run while Flame is running;
# --dry-run removes nothing, so it only warns and goes on.
# Data (journal, checkpoints, spill, run, logs, backups, ...) is never removed; its location is
# printed so you can decide.
#
# Usage: uninstall_bridge.sh [--dry-run]
#   --dry-run  remove nothing; only print what would be done (allowed while Flame is running)
#
# FLAME_MCP_STUB_DST replaces the stub target (for tests only; leave it unset).
set -euo pipefail

STUB_DST="${FLAME_MCP_STUB_DST:-/opt/Autodesk/shared/python/flame_mcp_bridge.py}"
APP_SUPPORT="$HOME/Library/Application Support/flame-mcp"
BRIDGE_DIR="$APP_SUPPORT/bridge"
IMPL_DST="$BRIDGE_DIR/impl.py"
BACKUPS_DIR="$APP_SUPPORT/backups"

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
    echo "=== --dry-run: nothing will be removed; showing only what would be done ==="
fi

if pgrep -f "$FLAME_ANY_VERSION_PATTERN" >/dev/null 2>&1; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "Flame is running. This is a dry run, so it goes on; a real uninstall is refused until Flame quits." >&2
    else
        echo "Flame is running. Quit Flame, then run this again." >&2
        exit 1
    fi
fi

ts="$(date +%y%m%d_%H%M%S)"
left_over=0

_backup() {
    # $1 = a regular file
    local target="$1"
    local backup_path
    backup_path="$BACKUPS_DIR/$(basename "$target").pre_uninstall_${ts}"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would back up: $target -> $backup_path"
    else
        mkdir -p "$BACKUPS_DIR"
        cp -p "$target" "$backup_path"
        echo "Backed up: $target -> $backup_path"
    fi
}

_remove_file() {
    # $1 = path, $2 = 1 to back it up first, 0 not to
    local target="$1" backup="$2"
    if [ -L "$target" ]; then
        echo "Not removed: $target is a symlink. Check it by hand." >&2
        left_over=1
        return 0
    fi
    if [ ! -e "$target" ]; then
        echo "Not found: $target"
        return 0
    fi
    if [ ! -f "$target" ]; then
        echo "Not removed: $target is not a regular file." >&2
        left_over=1
        return 0
    fi
    if [ "$backup" -eq 1 ]; then
        _backup "$target"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would remove: $target"
    else
        rm -f "$target"
        echo "Removed: $target"
    fi
}

echo "=== stub: $STUB_DST ==="
_remove_file "$STUB_DST" 1
pycache_dir="$(dirname "$STUB_DST")/__pycache__"
if [ -d "$pycache_dir" ] && [ ! -L "$pycache_dir" ]; then
    while IFS= read -r pyc; do
        [ -n "$pyc" ] || continue
        _remove_file "$pyc" 0
    done <<< "$(find "$pycache_dir" -maxdepth 1 -name 'flame_mcp_bridge.*.pyc')"
fi

echo "=== body: $BRIDGE_DIR ==="
if [ -L "$BRIDGE_DIR" ]; then
    echo "Not removed: $BRIDGE_DIR is a symlink. Check it by hand." >&2
    left_over=1
elif [ ! -d "$BRIDGE_DIR" ]; then
    echo "Not found: $BRIDGE_DIR"
else
    _remove_file "$IMPL_DST" 1
    if [ -d "$BRIDGE_DIR/__pycache__" ] && [ ! -L "$BRIDGE_DIR/__pycache__" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[dry-run] would remove: $BRIDGE_DIR/__pycache__"
        else
            rm -rf "$BRIDGE_DIR/__pycache__"
            echo "Removed: $BRIDGE_DIR/__pycache__"
        fi
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would remove if empty: $BRIDGE_DIR"
    elif rmdir "$BRIDGE_DIR" 2>/dev/null; then
        echo "Removed: $BRIDGE_DIR"
    else
        echo "Kept (it still holds other files): $BRIDGE_DIR" >&2
        ls -A "$BRIDGE_DIR" >&2
        left_over=1
    fi
fi

echo "--- Data not removed (delete it by hand if you no longer need it) ---"
for name in journal run logs spill scratch backups; do
    if [ -e "$APP_SUPPORT/$name" ]; then
        echo "  $APP_SUPPORT/$name"
    fi
done

if [ "$DRY_RUN" -eq 1 ]; then
    echo "=== --dry-run finished: nothing was removed ==="
elif [ "$left_over" -eq 1 ]; then
    echo "=== Some items were not removed (see above) ===" >&2
    exit 1
else
    echo "=== Uninstall finished ==="
fi
