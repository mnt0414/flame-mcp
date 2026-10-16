# shellcheck shell=bash
#
# The one definition of where the development bridge body lives. Source it, do not run it.
#
# The production body (scripts/install_bridge.sh) is
#   ~/Library/Application Support/flame-mcp/bridge/impl.py
# and the development one always lives in a different directory. The stub reads impl.py from
# FLAME_MCP_BRIDGE_DIR when it is set, so every orchestrator that starts Flame for development
# must pass --env "FLAME_MCP_BRIDGE_DIR=$BRIDGE_DEV_DIR" to open.
# shellcheck disable=SC2034 # a library: used by the scripts that source this file
BRIDGE_DEV_DIR="$HOME/Library/Application Support/flame-mcp/bridge_dev"
