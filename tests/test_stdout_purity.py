"""Tests stdio hygiene: `python -m flame_mcp` puts only JSON-RPC on stdout.

logging.basicConfig points at stderr (see __main__.py), but this test is the actual proof:
launch the real entry point as a subprocess, send one `initialize` request, and check the
first line back is valid JSON-RPC --- not a stray print()/log line that would corrupt the
stdio transport.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from flame_mcp import transport
from flame_mcp.__main__ import build_server

REPO_ROOT = Path(__file__).resolve().parent.parent
READ_TIMEOUT_S = 15.0


def _clean_env(home: Path) -> dict[str, str]:
    """The server's environment, with HOME in `home` so that its startup sweep of the run
    directory's in-flight records never reaches the real one."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    for var in (
        "FLAME_MCP_READONLY",
        "FLAME_MCP_ALLOW_CATEGORIES",
        "FLAME_MCP_ALLOW_EFFECTS",
        "FLAME_MCP_ENABLE_EVAL",
    ):
        env.pop(var, None)
    return env


def _read_one_line(stream: object, box: queue.Queue[str]) -> None:
    line = stream.readline()  # type: ignore[attr-defined]
    box.put(line)


def test_stdout_carries_only_jsonrpc_on_initialize(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "flame_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=_clean_env(tmp_path),
        text=True,
        bufsize=1,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.1"},
            },
        }
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        assert proc.stdout is not None
        line_box: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=_read_one_line, args=(proc.stdout, line_box), daemon=True)
        reader.start()
        reader.join(timeout=READ_TIMEOUT_S)

        stderr_tail = ""
        if reader.is_alive():
            # Still blocked reading -- capture what stderr has to say before
            # failing, since that's normally where a startup error would go.
            proc.kill()
            proc.wait(timeout=5)
            if proc.stderr is not None:
                stderr_tail = proc.stderr.read()
            raise AssertionError(
                f"no line arrived on stdout within {READ_TIMEOUT_S}s; stderr was:\n{stderr_tail}"
            )

        line = line_box.get_nowait()
        assert line, "stdout closed before any response was written"

        # The core assertion: the very first bytes on stdout must be one
        # complete JSON-RPC object. Any leaked print()/log text here would
        # make this json.loads raise, which is exactly the failure mode
        # this test exists to catch.
        parsed = json.loads(line)
        assert parsed.get("jsonrpc") == "2.0"
        assert parsed.get("id") == 1
        assert "result" in parsed or "error" in parsed
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_build_server_sweeps_stale_inflight_records_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sweeps stale inflight records once, at startup.

    `build_server()` (called once, before the stdio loop
    starts) must sweep `inflight/` records older than
    `transport.INFLIGHT_MAX_AGE_S` so a record from a Flame session that
    crashed months ago does not sit there forever."""
    run_dir = tmp_path / "run"
    inflight_dir = run_dir / "inflight"
    inflight_dir.mkdir(parents=True)
    old_path = inflight_dir / "old.json"
    old_path.write_text(json.dumps({"id": "old", "label": None, "sentAt": "x"}))
    old_time = time.time() - (transport.INFLIGHT_MAX_AGE_S + 3600)
    os.utime(old_path, (old_time, old_time))
    fresh_path = inflight_dir / "fresh.json"
    fresh_path.write_text(json.dumps({"id": "fresh", "label": None, "sentAt": "y"}))

    for var in (
        "FLAME_MCP_READONLY",
        "FLAME_MCP_ALLOW_CATEGORIES",
        "FLAME_MCP_ALLOW_EFFECTS",
        "FLAME_MCP_ENABLE_EVAL",
    ):
        monkeypatch.delenv(var, raising=False)
    build_server(run_dir=run_dir)

    assert not old_path.exists()
    assert fresh_path.exists()


def test_invalid_policy_env_fails_closed_with_no_stdout(tmp_path: Path) -> None:
    # An unusable Policy configuration must exit non-zero,
    # explain why on stderr, and --- because this happens before the stdio
    # loop starts --- never write a single byte to stdout.
    env = _clean_env(tmp_path)
    env["FLAME_MCP_READONLY"] = "yes"  # only unset/""/"0"/"1" are accepted
    proc = subprocess.run(
        [sys.executable, "-m", "flame_mcp"],
        input="",
        capture_output=True,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "FLAME_MCP_READONLY" in proc.stderr


def test_the_server_subprocess_sweeps_the_run_dir_under_its_own_home(tmp_path: Path) -> None:
    inflight_dir = tmp_path / "Library" / "Application Support" / "flame-mcp" / "run" / "inflight"
    inflight_dir.mkdir(parents=True)
    old_path = inflight_dir / "old.json"
    old_path.write_text(json.dumps({"id": "old", "label": None, "sentAt": "x"}))
    old_time = time.time() - (transport.INFLIGHT_MAX_AGE_S + 3600)
    os.utime(old_path, (old_time, old_time))
    env = _clean_env(tmp_path)
    env.pop("FLAME_MCP_RUN_DIR", None)

    proc = subprocess.run(
        [sys.executable, "-m", "flame_mcp"],
        input="",
        capture_output=True,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        timeout=15,
    )

    assert proc.returncode == 0, proc.stderr
    assert not old_path.exists()
