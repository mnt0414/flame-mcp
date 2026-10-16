"""Tests that every real-Flame e2e client refuses a Flame whose project is not flame_mcp_probe.

The clients are not run against Flame here: `transport._discover` answers with a bridge whose
hello names another project, and `transport.exec` fails the test if it is called. A refused
client must return before it creates its output directory, execs anything, sends flame.exit() or
kills a process.
"""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from flame_mcp import transport
from flame_mcp_bridge.impl import BRIDGE_ABI

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

e2e_guard = importlib.import_module("e2e_guard")

ORCHESTRATED = subprocess.run(
    [str(SCRIPTS_DIR / "run_e2e.sh"), "--list"], capture_output=True, text=True, check=True
).stdout.split()
STANDALONE = ["p1_menu", "p1_stress"]
CLIENTS = ORCHESTRATED + STANDALONE
EXTRA_ARGV = {"p2_4_restart": ["--phase", "1"], "p1_stress": ["--label", "t"]}
OUTPUT_DIR_NAMES = ("OUTPUT_DIR", "SCRATCH_DIR")


def candidate(project: str | None, pid: int = 4242) -> transport.BridgeCandidate:
    return transport.BridgeCandidate(
        path=Path("/nonexistent/flame-mcp-4242.sock"),
        pid=pid,
        flame_version="2025.2.4",
        project_name=project,
        bridge_abi=BRIDGE_ABI,
    )


def test_probe_project_is_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate("flame_mcp_probe"))

    assert e2e_guard.probe_project_refusal(tmp_path, 0.0) is None
    assert e2e_guard.require_probe_project(tmp_path, 0.0) is None


@pytest.mark.parametrize("project", ["production_job", "", None, "flame_mcp_probe2"])
def test_other_project_is_refused_with_its_name_in_the_reason(
    project: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate(project))

    reason = e2e_guard.probe_project_refusal(tmp_path, 0.0)
    code = e2e_guard.require_probe_project(tmp_path, 0.0)

    assert reason is not None
    assert repr(project) in reason
    assert code == e2e_guard.REFUSED_EXIT_CODE
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "flame.exit()" in err


def test_no_bridge_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    failure = transport._DiscoveryFailure("BRIDGE_UNAVAILABLE", "no bridge socket found")
    monkeypatch.setattr(transport, "_discover", lambda run_dir: failure)

    reason = e2e_guard.probe_project_refusal(tmp_path, 0.0)

    assert reason is not None
    assert "BRIDGE_UNAVAILABLE" in reason


def test_waits_for_a_bridge_that_appears_late(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers: list[Any] = [
        transport._DiscoveryFailure("BRIDGE_UNAVAILABLE", "not yet"),
        candidate("flame_mcp_probe"),
    ]
    monkeypatch.setattr(transport, "_discover", lambda run_dir: answers.pop(0))
    monkeypatch.setattr(e2e_guard, "POLL_INTERVAL_S", 0.0)

    assert e2e_guard.probe_project_refusal(tmp_path, 5.0) is None


def test_reconfirm_accepts_the_same_probe_flame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate("flame_mcp_probe"))

    assert e2e_guard.reconfirm_candidate(tmp_path, candidate("flame_mcp_probe")) is None


def test_reconfirm_refuses_a_changed_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate("production_job"))

    reason = e2e_guard.reconfirm_candidate(tmp_path, candidate("flame_mcp_probe"))

    assert reason is not None
    assert "production_job" in reason


def test_reconfirm_refuses_a_changed_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        transport, "_discover", lambda run_dir: candidate("flame_mcp_probe", pid=999)
    )

    reason = e2e_guard.reconfirm_candidate(tmp_path, candidate("flame_mcp_probe"))

    assert reason is not None
    assert "pid" in reason


def test_reconfirm_refuses_a_bridge_that_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = transport._DiscoveryFailure("BRIDGE_UNAVAILABLE", "gone")
    monkeypatch.setattr(transport, "_discover", lambda run_dir: failure)

    assert e2e_guard.reconfirm_candidate(tmp_path, candidate("flame_mcp_probe")) is not None


def test_project_named_by_the_env_is_accepted_and_the_default_one_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLAME_MCP_E2E_PROJECT", "flame_mcp_probe_2026")
    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate("flame_mcp_probe_2026"))
    assert e2e_guard.probe_project_refusal(tmp_path, 0.0) is None
    assert e2e_guard.reconfirm_candidate(tmp_path, candidate("flame_mcp_probe_2026")) is None

    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate("flame_mcp_probe"))
    reason = e2e_guard.probe_project_refusal(tmp_path, 0.0)
    assert reason is not None
    assert "'flame_mcp_probe_2026'" in reason
    assert e2e_guard.reconfirm_candidate(tmp_path, candidate("flame_mcp_probe_2026")) is not None


def test_the_name_must_match_exactly_when_the_env_names_the_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FLAME_MCP_E2E_PROJECT", "flame_mcp_probe_2026")
    for other in ("flame_mcp_probe_2026b", "flame_mcp_probe_202", "FLAME_MCP_PROBE_2026"):
        monkeypatch.setattr(transport, "_discover", lambda run_dir, o=other: candidate(o))
        assert e2e_guard.probe_project_refusal(tmp_path, 0.0) is not None

    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate("flame_mcp_probe"))
    assert e2e_guard.require_probe_project(tmp_path, 0.0) == e2e_guard.REFUSED_EXIT_CODE
    assert "'flame_mcp_probe_2026'" in capsys.readouterr().err


def test_the_client_list_is_the_28_of_run_e2e() -> None:
    assert len(ORCHESTRATED) == 28


@pytest.mark.parametrize("name", CLIENTS)
def test_client_refuses_before_creating_or_sending_anything(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module: ModuleType = importlib.import_module(f"{name}_e2e")
    out_dir = tmp_path / "out"
    for attr in OUTPUT_DIR_NAMES:
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, out_dir)
    sent: list[str] = []

    def forbidden_exec(code: str, *args: Any, **kwargs: Any) -> Any:
        sent.append(code)
        raise AssertionError(f"{name} sent code to Flame: {code!r}")

    def forbidden_socket(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{name} opened a socket")

    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate("production_job"))
    monkeypatch.setattr(transport, "exec", forbidden_exec)
    monkeypatch.setattr(socket.socket, "connect", forbidden_socket)
    monkeypatch.setattr(e2e_guard, "POLL_INTERVAL_S", 0.0)
    argv = [f"{name}_e2e.py", "--run-dir", str(tmp_path / "run"), *EXTRA_ARGV.get(name, [])]
    monkeypatch.setattr(sys, "argv", argv)

    code = module.main()

    assert code == e2e_guard.REFUSED_EXIT_CODE
    assert sent == []
    assert not out_dir.exists()
    assert "REFUSED" in capsys.readouterr().err


def test_p1_transport_does_not_kill_when_the_project_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = importlib.import_module("p1_transport_e2e")
    killed: list[Any] = []
    monkeypatch.setattr(module, "is_flame_process", lambda pid: (True, "flame"))
    monkeypatch.setattr(transport, "_discover", lambda run_dir: candidate("production_job"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(
        transport, "exec", lambda *a, **k: pytest.fail("exec must not be sent before a kill")
    )
    results: dict[str, Any] = {"ok": True, "steps": []}

    module.step_d_sigkill(tmp_path, candidate("flame_mcp_probe"), results)

    assert killed == []
    step = results["steps"][-1]
    assert step["name"] == "d_sigkill"
    assert step["ok"] is False
    assert step["aborted"] is True
    assert "production_job" in step["reason"]


def test_wait_for_bridge_returns_the_candidate_once_one_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = iter([transport._DiscoveryFailure("BRIDGE_UNAVAILABLE", "none yet"), candidate("p")])
    monkeypatch.setattr(transport, "_discover", lambda run_dir: next(answers))
    monkeypatch.setattr(e2e_guard, "POLL_INTERVAL_S", 0.0)

    found = e2e_guard._wait_for_bridge(tmp_path, 5.0)

    assert found.project_name == "p"


def test_wait_for_bridge_times_out_naming_the_last_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = transport._DiscoveryFailure("BRIDGE_UNAVAILABLE", "no bridge socket found")
    monkeypatch.setattr(transport, "_discover", lambda run_dir: failure)
    monkeypatch.setattr(e2e_guard, "POLL_INTERVAL_S", 0.0)

    with pytest.raises(TimeoutError, match="BRIDGE_UNAVAILABLE: no bridge socket found"):
        e2e_guard._wait_for_bridge(tmp_path, 0.0)


def test_br_dict_carries_the_traceback_and_the_error_fields() -> None:
    result = SimpleNamespace(
        ok=False,
        result=None,
        error_code="TIMEOUT",
        error="slow",
        traceback="tb",
        duration_ms=12.5,
    )

    assert e2e_guard._br_dict(result) == {
        "ok": False,
        "result": None,
        "errorCode": "TIMEOUT",
        "error": "slow",
        "traceback": "tb",
        "durationMs": 12.5,
    }


def test_save_writes_json_with_non_json_values_as_strings(tmp_path: Path) -> None:
    out = tmp_path / "r.json"

    e2e_guard._save({"path": Path("a"), "n": 1}, out)

    assert json.loads(out.read_text("utf-8")) == {"path": "a", "n": 1}


def test_shared_constants_have_the_values_the_cleaner_used() -> None:
    assert e2e_guard.FATAL_ERROR_CODES == {"BRIDGE_LOST", "BRIDGE_UNAVAILABLE", "BRIDGE_AMBIGUOUS"}
    assert (e2e_guard.DEFAULT_WAIT_SOCKET_S, e2e_guard.STAGE_WAIT_S) == (120.0, 1.0)
    assert e2e_guard.EXIT_TIMEOUT_S == 5.0
    assert e2e_guard.OUTPUT_DIR.parts[-2:] == ("flame-mcp", "p2")
    assert e2e_guard.RUN_DIR_DEFAULT.parts[-2:] == ("flame-mcp", "run")
