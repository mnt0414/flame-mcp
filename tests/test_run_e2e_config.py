"""scripts/run_e2e.sh, scripts/e2e_lib.sh and scripts/regress.sh, without a Flame.

OLD_CONFIGS is what the 25 per-e2e orchestrators that run_e2e.sh replaced said (client, results
directory, result-file glob, whether the stage files are filtered out of the result listing,
whether E2E_EXTRA_ARGS reaches the client, whether the client's exit is followed by a shell.log
crash check, the precondition file, the default TIMEOUT and the stage description).
`run_e2e.sh --print-config <name>` has to say the same.

Nothing here starts Flame: a fake `pgrep` that reports Flame as running stops every run before it
deploys or opens anything, and HOME points into tmp_path."""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
RUN_E2E = SCRIPTS / "run_e2e.sh"
REGRESS = SCRIPTS / "regress.sh"
E2E_LIB = SCRIPTS / "e2e_lib.sh"
E2E_FLAME_ENV = SCRIPTS / "e2e_flame_env.sh"
RUN_WITH_BRIDGE = SCRIPTS / "run_flame_with_bridge.sh"
W3_SETUP_UNSET = "env:FLAME_MCP_W3_SETUP"


class OldConfig(NamedTuple):
    name: str
    client: str
    results_dir: str
    result_glob: str
    stage_filter: str
    extra_args: str
    crash_check: str
    precondition: str | None
    timeout: str
    stages: str


OLD_CONFIGS: list[OldConfig] = [
    OldConfig(
        "fix2_library_batch",
        "scripts/fix2_library_batch_e2e.py",
        "p3",
        "fix2_library_batch_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages l, b, r, d, c, e, g, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "fix3_reel_order",
        "scripts/fix3_reel_order_e2e.py",
        "p3",
        "fix3_reel_order_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages n, c, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "fix5_timeline_selector",
        "scripts/fix5_timeline_selector_e2e.py",
        "p3",
        "fix5_timeline_selector_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages a, t, p, s, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "fix6_same_name_nodes",
        "scripts/fix6_same_name_nodes_e2e.py",
        "p3",
        "fix6_same_name_nodes_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages n, c, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "fix7_small_items",
        "scripts/fix7_small_items_e2e.py",
        "p3",
        "fix7_small_items_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages a, b, c, d, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p1_mcp",
        "scripts/p1_mcp_e2e.py",
        "p1",
        "p1_mcp_e2e_*.json",
        "no",
        "no",
        "no",
        None,
        "600",
        "",
    ),
    OldConfig(
        "p2_1_attr",
        "scripts/p2_1_attr_e2e.py",
        "p2",
        "p2_1_attr_e2e_*.json",
        "no",
        "no",
        "no",
        None,
        "600",
        "",
    ),
    OldConfig(
        "p2_2a_journal",
        "scripts/p2_2a_journal_e2e.py",
        "p2",
        "p2_2a_journal_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(stages a-f, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_2b_plan",
        "scripts/p2_2b_plan_e2e.py",
        "p2",
        "p2_2b_plan_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(stages a-f, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_2c_checkpoint_eval",
        "scripts/p2_2c_checkpoint_eval_e2e.py",
        "p2",
        "p2_2c_checkpoint_eval_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(stages a-e, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_3a_batch",
        "scripts/p2_3a_batch_e2e.py",
        "p2",
        "p2_3a_batch_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(stages a-e, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_3b_node_action",
        "scripts/p2_3b_node_action_e2e.py",
        "p2",
        "p2_3b_node_action_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(stages a-f, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_3c_render",
        "scripts/p2_3c_render_e2e.py",
        "p2",
        "p2_3c_render_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages a-f, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_3d_desktop",
        "scripts/p2_3d_desktop_e2e.py",
        "p2",
        "p2_3d_desktop_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages a-d, f, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_3r_reel_fidelity",
        "scripts/p2_3r_reel_fidelity_e2e.py",
        "p2",
        "p2_3r_reel_fidelity_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(stages a-e, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_4_scenario4",
        "scripts/p2_4_scenario4_e2e.py",
        "p2",
        "p2_4_scenario4_e2e_*.json",
        "yes",
        "yes",
        "yes",
        None,
        "600",
        "(each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p2_4_stress",
        "scripts/p2_4_stress_e2e.py",
        "p2",
        "p2_4_stress_e2e_*.json",
        "yes",
        "yes",
        "yes",
        None,
        "600",
        "(each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p3_1a_read",
        "scripts/p3_1a_read_e2e.py",
        "p3",
        "p3_1a_read_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages a-g, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p3_1b_write",
        "scripts/p3_1b_write_e2e.py",
        "p3",
        "p3_1b_write_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages a, a2, b, c, e, g, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p3_2a_checkpoint",
        "scripts/p3_2a_checkpoint_e2e.py",
        "p3",
        "p3_2a_checkpoint_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages a-k, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p3_2b_plan_delete",
        "scripts/p3_2b_plan_delete_e2e.py",
        "p3",
        "p3_2b_plan_delete_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages p, d, m, k, r, g, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p3_3_segment",
        "scripts/p3_3_segment_e2e.py",
        "p3",
        "p3_3_segment_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages s, r, e, x, p, y, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p3_4_construct",
        "scripts/p3_4_construct_e2e.py",
        "p3",
        "p3_4_construct_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages o, i, k, v, c, p, y, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "p3_5_scenarios",
        "scripts/p3_5_scenarios_e2e.py",
        "p3",
        "p3_5_scenarios_e2e_*.json",
        "yes",
        "no",
        "no",
        None,
        "600",
        "(setup, stages a, n, b, z; each stage starts a flame_mcp subprocess)",
    ),
    OldConfig(
        "w3_timewarp",
        "scripts/w3_timewarp_e2e.py",
        "p3",
        "w3_timewarp_e2e_*.json",
        "yes",
        "no",
        "no",
        W3_SETUP_UNSET,
        "600",
        "(setup, stages t, r, n, z; each stage starts a flame_mcp subprocess)",
    ),
]

DEDICATED = ["p1_bridge", "p1_transport", "p2_4_restart"]
ALL_NAMES = [c.name for c in OLD_CONFIGS] + DEDICATED


def base_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLAME_MCP_", "PHASE0B_"))}
    env["HOME"] = str(home)
    env.pop("E2E_EXTRA_ARGS", None)
    return env


def bash(
    args: list[str], env: dict[str, str], cwd: Path = REPO
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=120, check=False
    )


def parse_config(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        assert sep, f"not a key=value line: {line!r}"
        out[key] = value
    return out


def print_config(name: str, home: Path, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = base_env(home)
    env.update(extra_env or {})
    proc = bash([str(RUN_E2E), "--print-config", name], env)
    assert proc.returncode == 0, proc.stderr
    return parse_config(proc.stdout)


def stage_tokens_old(text: str) -> tuple[bool, list[str]]:
    """Reads ('has a setup step', the stage letters) from a stage description.

    For example "(setup, stages a-g, z)".
    """
    match = re.search(r"stages (.*?)[;)]", text)
    if match is None:
        return ("setup" in text, [])
    return ("setup" in text, [t for t in re.split(r"[, ]+", match.group(1)) if t])


def stage_tokens_new(text: str) -> tuple[bool, list[str]]:
    match = re.search(r"stages (.*)$", text)
    if match is None:
        return (text.startswith("setup"), [])
    return (text.startswith("setup"), [t.strip() for t in match.group(1).split(",")])


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """`pgrep` reports a running Flame; `open` and `sample` only log that they were called."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    calls.mkdir()
    for name, body in {
        "pgrep": 'echo "$@" >> "$FAKE_CALLS/pgrep.log"; echo 4242; exit "${FAKE_PGREP_RC:-0}"',
        "open": 'echo "$@" >> "$FAKE_CALLS/open.log"; exit 0',
    }.items():
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
    return bin_dir


def fake_env(home: Path, fake_bin: Path, tmp_path: Path) -> dict[str, str]:
    env = base_env(home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_CALLS"] = str(tmp_path / "calls")
    return env


def calls(tmp_path: Path, name: str) -> str:
    path = tmp_path / "calls" / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.parametrize("old", OLD_CONFIGS, ids=lambda c: c.name)
def test_print_config_matches_what_the_old_script_said(old: OldConfig, tmp_path: Path) -> None:
    got = print_config(old.name, tmp_path)

    app_support = tmp_path / "Library" / "Application Support" / "flame-mcp"
    assert got["name"] == old.name
    assert got["runner"] == "standard"
    assert got["client"] == str(REPO / old.client)
    assert got["resultsDir"] == str(app_support / old.results_dir)
    assert got["resultGlob"] == old.result_glob
    assert got["stageFilter"] == old.stage_filter
    assert got["extraArgs"] == old.extra_args
    assert got["crashCheck"] == old.crash_check
    assert got["timeout"] == old.timeout
    if old.precondition is None:
        assert got["precondition"] == "none"
    else:
        assert got["precondition"] == old.precondition
    assert stage_tokens_new(got["stages"]) == stage_tokens_old(old.stages)


@pytest.mark.parametrize("old", OLD_CONFIGS, ids=lambda c: c.name)
def test_client_script_exists_for_every_configured_name(old: OldConfig) -> None:
    assert (REPO / old.client).is_file()


def test_only_the_two_p2_4_e2es_take_extra_args_and_crash_check() -> None:
    names = {c.name for c in OLD_CONFIGS if c.extra_args == "yes"}
    assert names == {"p2_4_scenario4", "p2_4_stress"}
    assert names == {c.name for c in OLD_CONFIGS if c.crash_check == "yes"}


def test_list_names_the_25_folded_e2es_and_the_3_dedicated_ones(tmp_path: Path) -> None:
    proc = bash([str(RUN_E2E), "--list"], base_env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    names = proc.stdout.split()
    assert sorted(names) == sorted(ALL_NAMES)
    assert len(names) == 28


@pytest.mark.parametrize("name", DEDICATED)
def test_dedicated_names_resolve_to_their_own_script(name: str, tmp_path: Path) -> None:
    got = print_config(name, tmp_path)
    assert got["name"] == name
    assert got["runner"] == "dedicated"
    assert got["script"] == str(SCRIPTS / f"run_{name}_e2e.sh")
    assert got["client"] == str(SCRIPTS / f"{name}_e2e.py")
    assert Path(got["script"]).is_file()
    assert Path(got["client"]).is_file()


def test_unknown_name_is_refused_with_the_name_in_the_message(tmp_path: Path) -> None:
    for argv in (["no_such_e2e"], ["--print-config", "no_such_e2e"]):
        proc = bash([str(RUN_E2E), *argv], base_env(tmp_path))
        assert proc.returncode != 0
        assert "no_such_e2e" in proc.stderr


def test_non_numeric_timeout_is_refused_before_anything_starts(
    tmp_path: Path, fake_bin: Path
) -> None:
    proc = bash([str(RUN_E2E), "p3_1a_read", "soon"], fake_env(tmp_path, fake_bin, tmp_path))
    assert proc.returncode != 0
    assert "TIMEOUT" in proc.stderr
    assert calls(tmp_path, "pgrep.log") == ""


def test_old_per_e2e_scripts_are_gone() -> None:
    left = [
        f"run_{c.name}_e2e.sh" for c in OLD_CONFIGS if (SCRIPTS / f"run_{c.name}_e2e.sh").exists()
    ]
    assert left == []


def test_w3_setup_path_has_no_default_and_follows_the_env_var(tmp_path: Path) -> None:
    assert print_config("w3_timewarp", tmp_path)["precondition"] == W3_SETUP_UNSET
    empty = print_config("w3_timewarp", tmp_path, {"FLAME_MCP_W3_SETUP": ""})
    assert empty["precondition"] == W3_SETUP_UNSET
    other = tmp_path / "elsewhere.timewarp_node"
    got = print_config("w3_timewarp", tmp_path, {"FLAME_MCP_W3_SETUP": str(other)})
    assert got["precondition"] == f"file:{other}"


def test_w3_without_the_setup_variable_skips_with_exit_0_and_never_touches_flame(
    tmp_path: Path, fake_bin: Path
) -> None:
    home = tmp_path / "home"
    env = fake_env(home, fake_bin, tmp_path)
    env.pop("FLAME_MCP_W3_SETUP", None)
    env["FAKE_PGREP_RC"] = "1"

    proc = bash([str(RUN_E2E), "w3_timewarp"], env)

    assert proc.returncode == 0, proc.stderr
    assert "SKIP: FLAME_MCP_W3_SETUP is not set" in proc.stderr
    assert "Timewarp" in proc.stderr
    assert len(calls(tmp_path, "pgrep.log").splitlines()) == 1
    assert calls(tmp_path, "open.log") == ""
    assert not (home / "Library").exists()


def test_w3_without_its_setup_file_skips_with_exit_0_and_never_touches_flame(
    tmp_path: Path, fake_bin: Path
) -> None:
    home = tmp_path / "home"
    env = fake_env(home, fake_bin, tmp_path)
    env["FLAME_MCP_W3_SETUP"] = str(tmp_path / "missing.timewarp_node")
    env["FAKE_PGREP_RC"] = "1"

    proc = bash([str(RUN_E2E), "w3_timewarp"], env)

    assert proc.returncode == 0, proc.stderr
    assert "SKIP" in proc.stderr
    assert "FLAME_MCP_W3_SETUP" in proc.stderr
    assert str(tmp_path / "missing.timewarp_node") in proc.stderr
    assert len(calls(tmp_path, "pgrep.log").splitlines()) == 1
    assert calls(tmp_path, "open.log") == ""
    assert not (home / "Library").exists()


def test_w3_without_its_setup_file_still_aborts_with_exit_1_when_flame_is_running(
    tmp_path: Path, fake_bin: Path
) -> None:
    """A running Flame is checked for first, the setup file second."""
    home = tmp_path / "home"
    env = fake_env(home, fake_bin, tmp_path)
    env["FLAME_MCP_W3_SETUP"] = str(tmp_path / "missing.timewarp_node")

    proc = bash([str(RUN_E2E), "w3_timewarp"], env)

    assert proc.returncode == 1
    assert "already running" in proc.stderr
    assert "SKIP" not in proc.stderr
    assert calls(tmp_path, "open.log") == ""
    assert not (home / "Library").exists()


def test_w3_with_its_setup_file_goes_on_to_the_flame_running_check(
    tmp_path: Path, fake_bin: Path
) -> None:
    home = tmp_path / "home"
    setup = tmp_path / "setup.timewarp_node"
    setup.write_text("setup", encoding="utf-8")
    env = fake_env(home, fake_bin, tmp_path)
    env["FLAME_MCP_W3_SETUP"] = str(setup)

    proc = bash([str(RUN_E2E), "w3_timewarp"], env)

    assert proc.returncode == 1
    assert "already running" in proc.stderr
    assert "SKIP" not in proc.stderr
    assert calls(tmp_path, "pgrep.log") != ""
    assert calls(tmp_path, "open.log") == ""
    assert not (home / "Library").exists()


@pytest.mark.parametrize("env_value", [None, "", "custom/elsewhere.timewarp_node"])
def test_run_e2e_and_the_w3_client_resolve_the_setup_file_the_same_way(
    env_value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        client = importlib.import_module("w3_timewarp_e2e")
    finally:
        sys.path.remove(str(SCRIPTS))
    monkeypatch.setenv("HOME", str(tmp_path))
    extra: dict[str, str] = {}
    environ: dict[str, str] = {}
    if env_value is not None:
        value = str(tmp_path / env_value) if env_value else ""
        extra = {"FLAME_MCP_W3_SETUP": value}
        environ = {"FLAME_MCP_W3_SETUP": value}

    shell_side = print_config("w3_timewarp", tmp_path, extra)["precondition"]

    if env_value:
        assert client.resolve_setup_src(environ) == tmp_path / env_value
        assert shell_side == f"file:{tmp_path / env_value}"
    else:
        assert client.resolve_setup_src(environ) is None
        assert shell_side == W3_SETUP_UNSET


def test_the_w3_client_reads_the_process_environment_when_given_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        client = importlib.import_module("w3_timewarp_e2e")
    finally:
        sys.path.remove(str(SCRIPTS))
    monkeypatch.setenv("FLAME_MCP_W3_SETUP", str(tmp_path / "from_env.timewarp_node"))

    assert client.resolve_setup_src() == tmp_path / "from_env.timewarp_node"


@pytest.mark.parametrize("name", ["p3_1a_read", "p2_4_stress", "p1_bridge", "p1_transport"])
def test_a_running_flame_stops_every_e2e_before_deploying_or_opening(
    name: str, tmp_path: Path, fake_bin: Path
) -> None:
    home = tmp_path / "home"
    proc = bash([str(RUN_E2E), name, "30"], fake_env(home, fake_bin, tmp_path))

    assert proc.returncode == 1
    assert "already running" in proc.stderr
    assert calls(tmp_path, "open.log") == ""
    assert not (home / "Library").exists()


def only_run(home: Path) -> Path:
    dirs = sorted((home / "Library" / "Application Support" / "flame-mcp" / "p3").glob("regress_*"))
    dirs = [d for d in dirs if d.is_dir()]
    assert len(dirs) == 1
    return dirs[0]


def test_regress_records_a_skip_for_a_running_flame_then_the_clean_up_skip_and_done(
    tmp_path: Path, fake_bin: Path
) -> None:
    home = tmp_path / "home"
    proc = bash([str(REGRESS), "p3_1a_read", "p2_2a_journal"], fake_env(home, fake_bin, tmp_path))

    assert proc.returncode == 0, proc.stderr
    out = only_run(home)
    summary = (out / "summary.txt").read_text(encoding="utf-8").splitlines()
    assert re.fullmatch(
        r"p3_1a_read#1 start=\d\d:\d\d:\d\d SKIPPED: Flame is already running", summary[0]
    )
    assert summary[1:] == ["clean SKIPPED: Flame is already running", "DONE"]
    latest = home / "Library" / "Application Support" / "flame-mcp" / "p3" / "regress_latest.txt"
    assert latest.read_text(encoding="utf-8").strip() == str(out)
    assert calls(tmp_path, "open.log") == ""


def test_regress_without_names_starts_with_the_first_listed_e2e(
    tmp_path: Path, fake_bin: Path
) -> None:
    home = tmp_path / "home"
    listed = bash([str(RUN_E2E), "--list"], base_env(home)).stdout.split()

    bash([str(REGRESS)], fake_env(home, fake_bin, tmp_path))

    first = (only_run(home) / "summary.txt").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith(f"{listed[0]}#1 ")


def test_regress_refuses_an_unknown_name_before_creating_its_output_dir(
    tmp_path: Path, fake_bin: Path
) -> None:
    home = tmp_path / "home"
    proc = bash([str(REGRESS), "p3_1a_read", "no_such_e2e"], fake_env(home, fake_bin, tmp_path))

    assert proc.returncode != 0
    assert "no_such_e2e" in proc.stderr
    assert not (home / "Library").exists()


def lib(snippet: str, home: Path, extra_env: dict[str, str] | None = None) -> str:
    env = base_env(home)
    env.update(extra_env or {})
    script = f'set -euo pipefail\nsource "{E2E_LIB}"\n{snippet}'
    proc = bash(["-c", script], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_check_crashed_looks_only_at_shell_log_lines_added_after_the_snapshot(
    tmp_path: Path,
) -> None:
    shell_log = tmp_path / "shell.log"
    shell_log.write_text("old abnormal termination\n", encoding="utf-8")
    snippet = f"""
e2e_init "{tmp_path / "results"}"
e2e_snapshot_logs
e2e_check_crashed && echo before=crashed || echo before=clean
echo "new abnormal termination" >> "{shell_log}"
e2e_check_crashed && echo after=crashed || echo after=clean
: > "{shell_log}"
echo "short" >> "{shell_log}"
e2e_check_crashed && echo rotated=crashed || echo rotated=clean
echo "abnormal termination" >> "{shell_log}"
e2e_check_crashed && echo rotated_new=crashed || echo rotated_new=clean
"""
    out = lib(snippet, tmp_path, {"PHASE0B_SHELL_LOG": str(shell_log)})
    assert out.split() == [
        "before=clean",
        "after=crashed",
        "rotated=clean",
        "rotated_new=crashed",
    ]


def test_check_crashed_is_false_when_there_is_no_shell_log(tmp_path: Path) -> None:
    snippet = 'e2e_init "$HOME/r"\ne2e_check_crashed && echo crashed || echo clean'
    out = lib(snippet, tmp_path, {"PHASE0B_SHELL_LOG": str(tmp_path / "none.log")})
    assert out.strip() == "clean"


def test_wait_for_exit_reports_exited_crashed_and_timeout(tmp_path: Path) -> None:
    shell_log = tmp_path / "shell.log"
    shell_log.write_text("", encoding="utf-8")
    snippet = f"""
e2e_init "{tmp_path / "results"}"
POLL_INTERVAL=1
e2e_snapshot_logs
sleep 0 & flame_pid=$!
wait "$flame_pid"
e2e_wait_for_exit 5 >/dev/null
echo "gone=$outcome"
sleep 30 & flame_pid=$!
e2e_wait_for_exit 1 >/dev/null
echo "hung=$outcome"
echo "abnormal termination" >> "{shell_log}"
e2e_wait_for_exit 5 >/dev/null
echo "crash=$outcome"
kill "$flame_pid"
"""
    out = lib(snippet, tmp_path, {"PHASE0B_SHELL_LOG": str(shell_log)})
    assert out.split() == ["gone=exited", "hung=timeout", "crash=crashed"]


def test_dispose_samples_then_kills_a_hung_flame(tmp_path: Path) -> None:
    results = tmp_path / "results"
    sample = tmp_path / "fake_sample.sh"
    sample.write_text(
        '#!/bin/sh\necho "$@" > "$4"\necho "$1" > "$FAKE_SAMPLED_PID"\n', encoding="utf-8"
    )
    sample.chmod(0o755)
    snippet = f"""
e2e_init "{results}"
mkdir -p "$RESULTS_DIR"
ts=261013_000000
sleep 300 & flame_pid=$!
outcome=timeout
e2e_dispose_flame
echo "pid=$flame_pid"
wait "$flame_pid" 2>/dev/null || true
if kill -0 "$flame_pid" 2>/dev/null; then echo alive; else echo dead; fi
"""
    out = lib(
        snippet,
        tmp_path,
        {"PHASE0B_SAMPLE_CMD": str(sample), "FAKE_SAMPLED_PID": str(tmp_path / "sampled_pid")},
    )
    assert "dead" in out.split()
    pid = re.search(r"pid=(\d+)", out)
    assert pid is not None
    assert (tmp_path / "sampled_pid").read_text(encoding="utf-8").strip() == pid.group(1)
    assert (results / "timeout_sample_261013_000000.txt").is_file()


def test_dispose_promotes_exited_to_crashed_when_the_crash_probe_saw_one(tmp_path: Path) -> None:
    results = tmp_path / "results"
    snippet = f"""
e2e_init "{results}"
mkdir -p "$RESULTS_DIR"
TERM_WAIT=0
ts=261013_000001
E2E_SAMPLE_SUFFIX=_x
sleep 300 & flame_pid=$!
outcome=exited
crash_seen=1
e2e_dispose_flame
echo "outcome=$outcome"
"""
    sample = tmp_path / "fake_sample.sh"
    sample.write_text('#!/bin/sh\necho "$@" > "$4"\n', encoding="utf-8")
    sample.chmod(0o755)
    out = lib(snippet, tmp_path, {"PHASE0B_SAMPLE_CMD": str(sample)})
    assert "outcome=crashed" in out
    assert (results / "crash_sample_261013_000001_x.txt").is_file()


def test_exit_route_line_carries_the_crash_flag_only_when_the_crash_check_is_on(
    tmp_path: Path,
) -> None:
    snippet = f"""
e2e_init "{tmp_path / "results"}"
outcome=exited; e2e_exit_code=0; crash_seen=0
e2e_report_exit_route "$HOME/x/p3_1a_read_e2e.py"
E2E_CRASH_CHECK=1
e2e_report_exit_route "$HOME/x/p2_4_stress_e2e.py"
crash_seen=1
e2e_report_exit_route "$HOME/x/p2_4_stress_e2e.py"
"""
    out = lib(snippet, tmp_path).splitlines()
    assert out == [
        "=== exit route: exited (p3_1a_read_e2e.py exit=0) ===",
        "=== exit route: exited (p2_4_stress_e2e.py exit=0, shell.log abnormal termination: 0) ===",
        "=== exit route: exited (p2_4_stress_e2e.py exit=0, shell.log abnormal termination: 1) ===",
    ]


@pytest.mark.parametrize(("stage_filter", "expected"), [("yes", "good"), ("no", "stage")])
def test_latest_result_skips_stage_files_only_when_asked(
    stage_filter: str, expected: str, tmp_path: Path
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    good = results / "x_e2e_1.json"
    stage = results / "x_e2e_1_stage_a.json"
    good.write_text('{"name": "good"}', encoding="utf-8")
    stage.write_text('{"name": "stage"}', encoding="utf-8")
    os.utime(good, (1000, 1000))
    os.utime(stage, (2000, 2000))
    out = lib(
        f'e2e_init "{results}"\ne2e_report_latest_result x.py "x_e2e_*.json" {stage_filter}',
        tmp_path,
    )
    assert f'"name": "{expected}"' in out


def test_latest_result_says_so_when_there_is_none(tmp_path: Path) -> None:
    out = lib(
        f'e2e_init "{tmp_path / "none"}"\ne2e_report_latest_result x.py "x_e2e_*.json" yes',
        tmp_path,
    )
    assert "no result file found" in out


# The waiting times are pinned by value; they are the values the former per-e2e scripts used.


def test_e2e_lib_waits_are_the_values_the_old_scripts_had(tmp_path: Path) -> None:
    out = lib(
        'echo "$POLL_INTERVAL $BIN_WAIT_TIMEOUT $SOCKET_WAIT_S $TERM_WAIT"',
        tmp_path,
    )
    assert out.split() == ["5", "60", "120", "10"]


def test_e2e_lib_sleeps_only_for_the_known_waits() -> None:
    sleeps = re.findall(r"^\s*sleep (\S+)$", E2E_LIB.read_text(encoding="utf-8"), re.MULTILINE)
    assert sleeps == ['"$POLL_INTERVAL"', "1", '"$POLL_INTERVAL"', '"$TERM_WAIT"']


@pytest.mark.parametrize(
    ("script", "variable", "value"),
    [
        (RUN_E2E, "DEFAULT_TIMEOUT", "600"),
        (SCRIPTS / "run_p1_bridge_e2e.sh", "TIMEOUT", '"${1:-600}"'),
        (SCRIPTS / "run_p2_4_restart_e2e.sh", "TIMEOUT", '"${1:-600}"'),
        (SCRIPTS / "run_p2_4_restart_e2e.sh", "GONE_WAIT_S", "30"),
        (SCRIPTS / "run_p1_transport_e2e.sh", "POST_RUN_EXIT_WAIT_S", "15"),
        (SCRIPTS / "run_p1_transport_e2e.sh", "POST_RUN_POLL_INTERVAL", "1"),
    ],
    ids=lambda v: v if isinstance(v, str) else v.name,
)
def test_the_timeouts_outside_the_lib_are_the_old_values(
    script: Path, variable: str, value: str
) -> None:
    found = re.findall(rf"^{variable}=(.*)$", script.read_text(encoding="utf-8"), re.MULTILINE)
    assert found == [value]


NEW_SCRIPTS = [E2E_LIB, E2E_FLAME_ENV, RUN_E2E, REGRESS]
ALL_SH = sorted(SCRIPTS.glob("*.sh"))


@pytest.mark.parametrize("script", ALL_SH, ids=lambda p: p.name)
def test_bash_parses_every_script(script: Path) -> None:
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


# Shell scripts that stay in the development repository (release/excluded.txt). Every other
# scripts/*.sh is published and has to be shellcheck-clean, so a new script is checked until it
# is named here on purpose.
UNPUBLISHED_SH = frozenset(
    {
        "make_u13b_media.sh",
        "run_c6_hooks.sh",
        "run_phase0b_auto.sh",
        "run_two_stage.sh",
        "run_u3_commit.sh",
    }
)
PUBLIC_SH = [p for p in ALL_SH if p.name not in UNPUBLISHED_SH]


def shellcheck_or_stop(env: dict[str, str]) -> str:
    """The shellcheck executable; a missing one fails under CI and skips on a developer machine."""
    found = shutil.which("shellcheck", path=env.get("PATH"))
    if found is not None:
        return found
    if env.get("CI", "").lower() in ("true", "1"):
        pytest.fail("shellcheck is not installed, and CI must not skip the shell script checks")
    pytest.skip("shellcheck is not installed")


def test_the_checked_scripts_include_the_installers_and_the_libraries() -> None:
    names = {p.name for p in PUBLIC_SH}
    assert {"install_bridge.sh", "uninstall_bridge.sh", "doctor.sh", "e2e_lib.sh"} <= names
    assert not names & UNPUBLISHED_SH


@pytest.mark.parametrize("script", PUBLIC_SH, ids=lambda p: p.name)
def test_shellcheck_is_clean(script: Path, tmp_path: Path) -> None:
    shellcheck = shellcheck_or_stop(dict(os.environ))
    proc = subprocess.run(
        [shellcheck, "-x", "-f", "gcc", str(script)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout


def test_a_missing_shellcheck_fails_under_ci(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception):
        shellcheck_or_stop({"PATH": str(tmp_path), "CI": "true"})


def test_a_missing_shellcheck_is_skipped_outside_ci(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception):
        shellcheck_or_stop({"PATH": str(tmp_path)})
    with pytest.raises(pytest.skip.Exception):
        shellcheck_or_stop({"PATH": str(tmp_path), "CI": "false"})


def test_an_installed_shellcheck_is_returned_even_under_ci(tmp_path: Path) -> None:
    fake = tmp_path / "shellcheck"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    assert shellcheck_or_stop({"PATH": str(tmp_path), "CI": "true"}) == str(fake)


@pytest.mark.parametrize("script", NEW_SCRIPTS, ids=lambda p: p.name)
def test_new_scripts_have_no_machine_specific_paths(script: Path) -> None:
    assert "/Users/" not in script.read_text(encoding="utf-8")


ENGLISH_ONLY_SCRIPTS = [
    SCRIPTS / name
    for name in (
        "bridge_dev_env.sh",
        "deploy_bridge_dev.sh",
        "doctor.sh",
        "e2e_flame_env.sh",
        "e2e_lib.sh",
        "flame_mcp_server.sh",
        "install_bridge.sh",
        "make_clip_media.sh",
        "make_p2_media.sh",
        "make_p2_tc_media.sh",
        "regress.sh",
        "run_e2e.sh",
        "run_flame_with_bridge.sh",
        "run_p1_bridge_e2e.sh",
        "run_p1_transport_e2e.sh",
        "run_p2_4_restart_e2e.sh",
    )
]


@pytest.mark.parametrize("script", ENGLISH_ONLY_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_have_only_ascii_text(script: Path) -> None:
    assert re.search(r"[^\x00-\x7f]", script.read_text(encoding="utf-8")) is None


def test_regress_reads_the_route_line_that_the_launchers_print() -> None:
    pattern = re.search(
        r"^ROUTE_PATTERN='(.*)'$", REGRESS.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert pattern is not None
    for launcher in (E2E_LIB, SCRIPTS / "run_flame_with_bridge.sh"):
        lines = [
            line
            for line in launcher.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith("echo") and "exit route" in line
        ]
        assert lines, launcher.name
        assert all(re.search(pattern.group(1), line) for line in lines)


@pytest.mark.parametrize("script", [RUN_E2E, REGRESS], ids=lambda p: p.name)
def test_entry_scripts_are_executable(script: Path) -> None:
    assert os.access(script, os.X_OK)


def test_no_file_still_points_at_a_removed_per_e2e_script() -> None:
    removed = "|".join(c.name for c in OLD_CONFIGS)
    pattern = re.compile(rf"run_(?:{removed})_e2e\.sh")
    roots = [SCRIPTS, REPO / "tests", REPO / "docs", REPO / "src", REPO / "stub"]
    hits: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc"} or "__pycache__" in path.parts:
                continue
            if path == Path(__file__).resolve():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            hits += [f"{path.relative_to(REPO)}: {m}" for m in pattern.findall(text)]
    readme = REPO / "README.ja.md"
    if readme.exists():
        hits += pattern.findall(readme.read_text(encoding="utf-8"))
    assert hits == []


def hostname_short() -> str:
    return subprocess.run(
        ["hostname", "-s"], capture_output=True, text=True, check=True
    ).stdout.strip()


SHELL_VALUES = """
echo "FLAME_APP=$FLAME_APP"
echo "PROJECT=$PROJECT"
echo "APP_LOG=$APP_LOG"
echo "SHELL_LOG=$SHELL_LOG"
echo "FLAME_BIN_PATTERN=$FLAME_BIN_PATTERN"
"""


def shell_values(
    source: Path, home: Path, extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    env = base_env(home)
    env.update(extra_env or {})
    script = f'set -euo pipefail\nsource "{source}"\n{SHELL_VALUES}'
    proc = bash(["-c", script], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return parse_config(proc.stdout)


def bin_pattern_matches(pattern: str, command: str) -> bool:
    proc = subprocess.run(
        ["grep", "-Eq", "--", pattern], input=command, text=True, capture_output=True, check=False
    )
    return proc.returncode == 0


FLAME_TAIL = "/bin/flame.app/Contents/MacOS/flame -x"


@pytest.mark.parametrize("source", [E2E_LIB, E2E_FLAME_ENV], ids=lambda p: p.name)
def test_without_the_variables_the_launch_values_are_the_old_ones(
    source: Path, tmp_path: Path
) -> None:
    got = shell_values(source, tmp_path)
    host = hostname_short()

    assert got["FLAME_APP"] == "/opt/Autodesk/flame_2025.2.4/bin/flame.app"
    assert got["PROJECT"] == "flame_mcp_probe"
    assert got["APP_LOG"] == f"/opt/Autodesk/log/flame202524_{host}_app.log"
    assert got["SHELL_LOG"] == f"/opt/Autodesk/log/flame202524_{host}_shell.log"
    assert got["FLAME_BIN_PATTERN"] == (
        r"/opt/Autodesk/(\.flamefamily|flame)_2025\.2\.4/bin/flame\.app/Contents/MacOS/flame( |$)"
    )


@pytest.mark.parametrize("source", [E2E_LIB, E2E_FLAME_ENV], ids=lambda p: p.name)
def test_the_variables_change_the_launch_values(source: Path, tmp_path: Path) -> None:
    got = shell_values(
        source,
        tmp_path,
        {
            "FLAME_MCP_E2E_FLAME_VERSION": "2026.2.3",
            "FLAME_MCP_E2E_PROJECT": "flame_mcp_probe_2026",
        },
    )
    host = hostname_short()

    assert got["FLAME_APP"] == "/opt/Autodesk/flame_2026.2.3/bin/flame.app"
    assert got["PROJECT"] == "flame_mcp_probe_2026"
    assert got["APP_LOG"] == f"/opt/Autodesk/log/flame202623_{host}_app.log"
    assert got["SHELL_LOG"] == f"/opt/Autodesk/log/flame202623_{host}_shell.log"
    pattern = got["FLAME_BIN_PATTERN"]
    for prefix in ("/opt/Autodesk/.flamefamily_", "/opt/Autodesk/flame_"):
        assert bin_pattern_matches(pattern, f"{prefix}2026.2.3{FLAME_TAIL}")
        assert bin_pattern_matches(pattern, f"{prefix}2026.2.3{FLAME_TAIL[:-3]}")
        assert not bin_pattern_matches(pattern, f"{prefix}2025.2.4{FLAME_TAIL}")
        assert not bin_pattern_matches(pattern, f"{prefix}2026x2x3{FLAME_TAIL}")
        assert not bin_pattern_matches(pattern, f"{prefix}2026.2.30{FLAME_TAIL}")


def test_the_default_pattern_still_matches_only_the_default_version(tmp_path: Path) -> None:
    pattern = shell_values(E2E_FLAME_ENV, tmp_path)["FLAME_BIN_PATTERN"]

    assert bin_pattern_matches(pattern, f"/opt/Autodesk/.flamefamily_2025.2.4{FLAME_TAIL}")
    assert not bin_pattern_matches(pattern, f"/opt/Autodesk/.flamefamily_2026.2.3{FLAME_TAIL}")


def test_the_phase0b_overrides_still_win_over_the_derived_values(tmp_path: Path) -> None:
    got = shell_values(
        E2E_FLAME_ENV,
        tmp_path,
        {
            "FLAME_MCP_E2E_FLAME_VERSION": "2026.2.3",
            "PHASE0B_SHELL_LOG": str(tmp_path / "fake_shell.log"),
            "PHASE0B_FLAME_BIN_PATTERN": "fake_marker",
        },
    )

    assert got["SHELL_LOG"] == str(tmp_path / "fake_shell.log")
    assert got["FLAME_BIN_PATTERN"] == "fake_marker"


@pytest.mark.parametrize("name", ["flame_mcp_probe", "flame_mcp_probe_2026"])
def test_a_project_name_that_follows_the_prefix_rule_is_accepted_by_the_env_file(
    name: str, tmp_path: Path
) -> None:
    got = shell_values(E2E_FLAME_ENV, tmp_path, {"FLAME_MCP_E2E_PROJECT": name})

    assert got["PROJECT"] == name


@pytest.mark.parametrize(
    "name",
    [
        "my_show",
        "flame_mcp_probeX",
        "flame_mcp_probe2026",
        "flame_mcp_prob",
        "Flame_mcp_probe_2026",
        "flame_mcp_probe_",
        "xflame_mcp_probe",
        "flame_mcp_probe-2026",
    ],
)
def test_a_project_name_outside_the_prefix_rule_stops_the_env_file_and_the_message_gives_the_rule(
    name: str, tmp_path: Path
) -> None:
    env = base_env(tmp_path)
    env["FLAME_MCP_E2E_PROJECT"] = name

    proc = bash(["-c", f'set -euo pipefail\nsource "{E2E_FLAME_ENV}"\necho reached'], env)

    assert proc.returncode == 2
    assert "reached" not in proc.stdout
    assert "FLAME_MCP_E2E_PROJECT" in proc.stderr
    assert "flame_mcp_probe_" in proc.stderr
    assert proc.stderr.isascii()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("FLAME_MCP_E2E_FLAME_VERSION", "2026"),
        ("FLAME_MCP_E2E_FLAME_VERSION", "2026.2.3/../x"),
        ("FLAME_MCP_E2E_FLAME_VERSION", "a.b"),
        ("FLAME_MCP_E2E_PROJECT", "bad name"),
        ("FLAME_MCP_E2E_PROJECT", "x;y"),
        ("FLAME_MCP_E2E_PROJECT", "-x"),
    ],
)
def test_a_bad_variable_stops_the_script_that_sources_the_env_file(
    variable: str, value: str, tmp_path: Path
) -> None:
    env = base_env(tmp_path)
    env[variable] = value

    proc = bash(["-c", f'set -euo pipefail\nsource "{E2E_LIB}"\necho reached'], env)

    assert proc.returncode == 2
    assert "reached" not in proc.stdout
    assert variable in proc.stderr


@pytest.mark.parametrize(
    ("variable", "value"),
    [("FLAME_MCP_E2E_FLAME_VERSION", "2026"), ("FLAME_MCP_E2E_PROJECT", "bad name")],
)
def test_run_flame_with_bridge_refuses_a_bad_variable_before_it_does_anything(
    variable: str, value: str, tmp_path: Path, fake_bin: Path
) -> None:
    client = tmp_path / "client.py"
    client.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    env = fake_env(home, fake_bin, tmp_path)
    env[variable] = value

    proc = bash([str(RUN_WITH_BRIDGE), str(client)], env)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert variable in proc.stderr
    assert calls(tmp_path, "open.log") == ""
    assert not (home / "Library").exists()


@pytest.mark.parametrize("script", [E2E_LIB, RUN_WITH_BRIDGE], ids=lambda p: p.name)
def test_the_launchers_get_their_flame_values_from_the_env_file(script: Path) -> None:
    text = script.read_text(encoding="utf-8")

    assert re.search(r"^source .*e2e_flame_env\.sh", text, re.MULTILINE)
    for variable in ("FLAME_APP", "PROJECT", "APP_LOG", "SHELL_LOG", "FLAME_BIN_PATTERN"):
        assert not re.search(rf"^{variable}=", text, re.MULTILINE), variable
