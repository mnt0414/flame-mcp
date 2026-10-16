# shellcheck shell=bash
#
# The one definition, on the shell side, of which Flame and which project the real-Flame e2e
# scripts use. Source it, do not run it. scripts/e2e_guard.py holds the same two defaults for the
# Python clients, and a unit test checks that they agree.
#
#   FLAME_MCP_E2E_FLAME_VERSION   the Flame version to start (default below)
#   FLAME_MCP_E2E_PROJECT         the test project to start Flame in (default below)
#
# Sets FLAME_APP, PROJECT, APP_LOG, SHELL_LOG and FLAME_BIN_PATTERN. A version that is not a plain
# dotted version, or a project name that is not the default or the default followed by '_' and one
# or more of A-Z a-z 0-9 '_' '-', stops the calling script with exit status 2. The version ends up
# in a path and a regular expression, the name in a Flame command line, and the name rule keeps
# these scripts, which create and delete things, away from a project that is not a test project.
#
# PHASE0B_SHELL_LOG and PHASE0B_FLAME_BIN_PATTERN replace the derived values for a test.

E2E_DEFAULT_FLAME_VERSION="2025.2.4"
E2E_DEFAULT_PROJECT="flame_mcp_probe"

E2E_FLAME_VERSION="${FLAME_MCP_E2E_FLAME_VERSION:-$E2E_DEFAULT_FLAME_VERSION}"
PROJECT="${FLAME_MCP_E2E_PROJECT:-$E2E_DEFAULT_PROJECT}"

_e2e_version_pattern='^[0-9]+(\.[0-9]+)+$'
_e2e_project_pattern="^${E2E_DEFAULT_PROJECT}(_[A-Za-z0-9_-]+)?\$"
if ! [[ "$E2E_FLAME_VERSION" =~ $_e2e_version_pattern ]]; then
    echo "FLAME_MCP_E2E_FLAME_VERSION must be digits separated by dots, like 2025.2.4 (got '$E2E_FLAME_VERSION')." >&2
    exit 2
fi
if ! [[ "$PROJECT" =~ $_e2e_project_pattern ]]; then
    echo "FLAME_MCP_E2E_PROJECT must be '$E2E_DEFAULT_PROJECT', or '${E2E_DEFAULT_PROJECT}_' followed by one or more of the ASCII characters A-Z a-z 0-9 '_' '-' (got '$PROJECT')." >&2
    exit 2
fi

_e2e_version_slug="${E2E_FLAME_VERSION//./}"
_e2e_version_regex="${E2E_FLAME_VERSION//./\\.}"

# shellcheck disable=SC2034 # a library: used by the scripts that source this file
FLAME_APP="/opt/Autodesk/flame_${E2E_FLAME_VERSION}/bin/flame.app"
# shellcheck disable=SC2034 # a library: used by the scripts that source this file
APP_LOG="/opt/Autodesk/log/flame${_e2e_version_slug}_$(hostname -s)_app.log"
# shellcheck disable=SC2034 # a library: used by the scripts that source this file
SHELL_LOG="${PHASE0B_SHELL_LOG:-/opt/Autodesk/log/flame${_e2e_version_slug}_$(hostname -s)_shell.log}"
# shellcheck disable=SC2034 # a library: used by the scripts that source this file
FLAME_BIN_PATTERN="${PHASE0B_FLAME_BIN_PATTERN:-/opt/Autodesk/(\.flamefamily|flame)_${_e2e_version_regex}/bin/flame\.app/Contents/MacOS/flame( |\$)}"
