"""Entry check shared by the real-Flame e2e clients.

A client run on its own (not through scripts/run_e2e.sh) talks to whatever Flame owns the bridge
socket, possibly one with a production project open. Every client calls `require_probe_project`
before it creates anything; when the bridge's hello does not report the test project, the client
sends nothing to Flame (no flame.exit(), no kill) and exits.

The test project and the Flame version the e2e scripts expect come from two environment variables,
read by `probe_project_name()` and `e2e_flame_version()`. Their defaults are defined here and,
for the shell scripts, in scripts/e2e_flame_env.sh.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from flame_mcp import transport  # noqa: E402

E2E_PROJECT_ENV: Final[str] = "FLAME_MCP_E2E_PROJECT"
E2E_FLAME_VERSION_ENV: Final[str] = "FLAME_MCP_E2E_FLAME_VERSION"
DEFAULT_PROBE_PROJECT_NAME: Final[str] = "flame_mcp_probe"
DEFAULT_E2E_FLAME_VERSION: Final[str] = "2025.2.4"
_PROJECT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    re.escape(DEFAULT_PROBE_PROJECT_NAME) + r"(_[A-Za-z0-9_-]+)?"
)
_FLAME_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9]+(\.[0-9]+)+")
DEFAULT_WAIT_S: Final[float] = 10.0
POLL_INTERVAL_S: Final[float] = 1.0
REFUSED_EXIT_CODE: Final[int] = 2
OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
RUN_DIR_DEFAULT: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "run"
DEFAULT_WAIT_SOCKET_S: Final[float] = 120.0
STAGE_WAIT_S: Final[float] = 1.0
EXIT_TIMEOUT_S: Final[float] = 5.0
FATAL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"BRIDGE_LOST", "BRIDGE_UNAVAILABLE", "BRIDGE_AMBIGUOUS"}
)


def _env_value(name: str, default: str, pattern: re.Pattern[str], what: str) -> str:
    value = os.environ.get(name) or default
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{name} must be {what} (got {value!r})")
    return value


def probe_project_name() -> str:
    """The Flame project the e2e scripts may act in: $FLAME_MCP_E2E_PROJECT or the default.

    The name must be the default or start with the default followed by '_' and at least one more
    character, so that the scripts, which create and delete things, cannot be pointed at a project
    that is not a test project. Only ASCII letters, digits, '_' and '-' follow: the name goes into
    a command line and into code sent to Flame, which takes ASCII only.
    """
    return _env_value(
        E2E_PROJECT_ENV,
        DEFAULT_PROBE_PROJECT_NAME,
        _PROJECT_NAME_PATTERN,
        f"{DEFAULT_PROBE_PROJECT_NAME!r}, or {DEFAULT_PROBE_PROJECT_NAME + '_'!r} followed by "
        "one or more of the ASCII characters A-Z a-z 0-9 '_' '-'",
    )


def e2e_flame_version() -> str:
    """The Flame version the e2e scripts start: $FLAME_MCP_E2E_FLAME_VERSION or the default."""
    return _env_value(
        E2E_FLAME_VERSION_ENV,
        DEFAULT_E2E_FLAME_VERSION,
        _FLAME_VERSION_PATTERN,
        "digits separated by dots, like 2025.2.4",
    )


def _candidate_refusal(candidate: transport.BridgeCandidate) -> str | None:
    expected = probe_project_name()
    if candidate.project_name == expected:
        return None
    return (
        f"the bridge (pid {candidate.pid}) reports project {candidate.project_name!r}, "
        f"not {expected!r}"
    )


def probe_project_refusal(run_dir: Path, wait_s: float = DEFAULT_WAIT_S) -> str | None:
    """Returns why the bridge under `run_dir` is not a probe-project Flame, or None if it is.

    Only the bridge's hello is used: nothing is executed in Flame. While no live bridge answers,
    discovery is repeated until `wait_s` has passed.
    """
    deadline = time.monotonic() + wait_s
    while True:
        found = transport._discover(run_dir)
        if isinstance(found, transport.BridgeCandidate):
            return _candidate_refusal(found)
        if time.monotonic() >= deadline:
            return f"no bridge to check ({found.error_code}: {found.message})"
        time.sleep(POLL_INTERVAL_S)


def require_probe_project(run_dir: Path, wait_s: float = DEFAULT_WAIT_S) -> int | None:
    """Returns None when the client may go on, else the exit code it must return.

    The reason is printed to stderr.
    """
    refusal = probe_project_refusal(run_dir, wait_s)
    if refusal is None:
        return None
    print(
        f"REFUSED: {refusal}. Nothing was created and nothing (flame.exit(), kill) was sent to "
        f"Flame. Open the project {probe_project_name()!r} and run this through "
        "scripts/run_e2e.sh.",
        file=sys.stderr,
    )
    return REFUSED_EXIT_CODE


def reconfirm_candidate(run_dir: Path, candidate: transport.BridgeCandidate) -> str | None:
    """Returns why `candidate` must not be acted on again, or None if it is still the probe Flame.

    For a step that cannot be undone (a kill): the same pid must still answer hello with the probe
    project's name.
    """
    found = transport._discover(run_dir)
    if not isinstance(found, transport.BridgeCandidate):
        return f"the bridge no longer answers ({found.error_code}: {found.message})"
    if found.pid != candidate.pid:
        return f"the bridge pid changed from {candidate.pid} to {found.pid}"
    return _candidate_refusal(found)


def _br_dict(br: transport.BridgeResult) -> dict[str, Any]:
    return {
        "ok": br.ok,
        "result": br.result,
        "errorCode": br.error_code,
        "error": br.error,
        "traceback": br.traceback,
        "durationMs": br.duration_ms,
    }


def _wait_for_bridge(run_dir: Path, timeout_s: float) -> transport.BridgeCandidate:
    deadline = time.monotonic() + timeout_s
    while True:
        candidate = transport._discover(run_dir)
        if isinstance(candidate, transport.BridgeCandidate):
            return candidate
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"no live bridge socket appeared under {run_dir} within {timeout_s}s "
                f"({candidate.error_code}: {candidate.message})"
            )
        time.sleep(POLL_INTERVAL_S)


def _save(results: dict[str, Any], out_path: Path) -> None:
    out_path.write_text(json.dumps(results, indent=2, default=str, ensure_ascii=False), "utf-8")
