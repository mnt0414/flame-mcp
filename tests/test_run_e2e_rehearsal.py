"""Whole e2e runs through scripts/run_e2e.sh and the orchestrators, against fakes.

A sandbox holds a copy of the shell scripts and a fake client per e2e; `open` starts a stand-in
for Flame (a sleeping process) and drops a bridge socket; the fake client reports what it was
called with, writes a result file, and ends the stand-in the way the real client does (flame.exit()
or SIGKILL). HOME points into tmp_path, so deploy_bridge_dev.sh writes there. No Flame is started.

This is the check on the order of steps, the exit routes and the files an orchestrator leaves,
that the per-name config test (test_run_e2e_config.py) cannot make.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
FLAME_MARKER = "fake_flame_marker"

FAKE_CLIENT = """\
import json, os, subprocess, sys, time
from pathlib import Path

home = Path(os.environ["HOME"])
app = home / "Library" / "Application Support" / "flame-mcp"
name = Path(__file__).name.removesuffix("_e2e.py")
with (Path(os.environ["FAKE_CALLS"]) / "client_argv.log").open("a") as fh:
    fh.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
sockets = list((app / "run").glob("flame-mcp-*.sock"))
(Path(os.environ["FAKE_CALLS"]) / "sockets_seen.log").open("a").write(f"{name} {len(sockets)}\\n")

results = app / os.environ["FAKE_RESULTS_SUBDIR"]
results.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
if "--phase" in args:
    phase = args[args.index("--phase") + 1]
    tag = args[args.index("--tag") + 1]
    (results / f"p2_4_restart_e2e_{tag}_phase{phase}.json").write_text('{"phase": ' + phase + "}")
    state = Path(args[args.index("--state-file") + 1])
    if phase == "1":
        state.write_text('{"ready": true}')
else:
    stamp = str(int(time.time()))
    (results / f"{name}_e2e_{stamp}.json").write_text('{"kind": "whole"}')
    time.sleep(1.1)
    (results / f"{name}_e2e_{stamp}_stage_a.json").write_text('{"kind": "stage"}')

if os.environ.get("FAKE_CLIENT_CRASH") == "1":
    with open(os.environ["PHASE0B_SHELL_LOG"], "a") as fh:
        fh.write("abnormal termination\\n")
if os.environ.get("FAKE_CLIENT_ENDS_FLAME", "1") == "1":
    for sock in sockets:
        sock.unlink()
    subprocess.run(["pkill", "-f", os.environ["FAKE_MARKER"]], check=False)
    time.sleep(0.3)
sys.exit(int(os.environ.get("FAKE_CLIENT_EXIT", "0")))
"""

FAKE_STANDIN = """\
import os, signal, sys, time

log = os.path.join(os.environ["FAKE_CALLS"], "flame_signals.log")


def on_term(signum, frame):
    with open(log, "a") as fh:
        fh.write("TERM\\n")
    if os.environ.get("FAKE_FLAME_IGNORE_TERM") != "1":
        sys.exit(0)


signal.signal(signal.SIGTERM, on_term)
deadline = time.time() + 300
while time.time() < deadline:
    time.sleep(0.2)
"""

FAKE_OPEN = """\
#!/bin/sh
dump='import json, sys; print(json.dumps(sys.argv[1:]))'
"$FAKE_PYTHON" -c "$dump" "$@" >> "$FAKE_CALLS/open_argv.log"
echo "$@" >> "$FAKE_CALLS/open.log"
touch "$FAKE_CALLS/open_ran"
mkdir -p "$HOME/Library/Application Support/flame-mcp/run"
touch "$HOME/Library/Application Support/flame-mcp/run/flame-mcp-1.sock"
nohup "$FAKE_PYTHON" "$FAKE_STANDIN" "$FAKE_MARKER.sh" >/dev/null 2>&1 &
echo $! > "$FAKE_CALLS/flame.pid"
exit 0
"""

FAKE_SENDDMP_PATTERN = "fake_senddmp_pattern"
FAKE_PGREP = """\
#!/bin/sh
case "$*" in
    *Contents/MacOS/flame*) exit 1 ;;
    *fake_senddmp_pattern*)
        if [ -f "$FAKE_CALLS/open_ran" ]; then
            cat "$FAKE_CALLS/senddmp_after" 2>/dev/null
        else
            cat "$FAKE_CALLS/senddmp_before" 2>/dev/null
        fi
        exit 0
        ;;
esac
exec /usr/bin/pgrep "$@"
"""

FAKE_SAMPLE = """\
#!/bin/sh
echo "sample $@" >> "$FAKE_CALLS/sample.log"
echo sampled > "$4"
"""


def replace_once(text: str, old: str, new: str) -> str:
    """str.replace that fails the test when `old` is not there exactly once."""
    assert text.count(old) == 1, (
        f"{old!r} found {text.count(old)} times; the test's speed-up is stale"
    )
    return text.replace(old, new)


class Sandbox:
    def __init__(self, root: Path, clients: list[str]) -> None:
        self.root = root
        self.marker = f"{FLAME_MARKER}_{root.name}"
        self.home = root / "home"
        self.calls = root / "calls"
        self.repo = root / "repo"
        self.shell_log = root / "shell.log"
        self.app_support = self.home / "Library" / "Application Support" / "flame-mcp"
        self.calls.mkdir(parents=True)
        self.home.mkdir()
        (self.repo / "scripts").mkdir(parents=True)
        for sh in SCRIPTS.glob("*.sh"):
            shutil.copy(sh, self.repo / "scripts" / sh.name)
        (self.repo / "stub").mkdir()
        (self.repo / "stub" / "flame_mcp_bridge.py").write_text("# stub\n", encoding="utf-8")
        impl = self.repo / "src" / "flame_mcp_bridge"
        impl.mkdir(parents=True)
        (impl / "impl.py").write_text("# impl\n", encoding="utf-8")
        venv_bin = self.repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        for name in clients:
            (self.repo / "scripts" / f"{name}_e2e.py").write_text(FAKE_CLIENT, encoding="utf-8")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in {"open": FAKE_OPEN, "pgrep": FAKE_PGREP, "sample": FAKE_SAMPLE}.items():
            (bin_dir / name).write_text(body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        self.bin_dir = bin_dir
        self.standin = root / "standin.py"
        self.standin.write_text(FAKE_STANDIN, encoding="utf-8")
        self.shell_log.write_text("an old line\n", encoding="utf-8")
        self.speed_up()

    def speed_up(self) -> None:
        """Poll every second and wait 1 s (not 10) after TERM. Fails if a constant is not found."""
        lib = self.repo / "scripts" / "e2e_lib.sh"
        text = lib.read_text(encoding="utf-8")
        text = replace_once(text, "POLL_INTERVAL=5\n", "POLL_INTERVAL=1\n")
        text = replace_once(text, "TERM_WAIT=10\n", "TERM_WAIT=1\n")
        lib.write_text(text, encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FLAME_MCP_", "PHASE0B_"))}
        env.update(
            HOME=str(self.home),
            PATH=f"{self.bin_dir}{os.pathsep}{env['PATH']}",
            FAKE_CALLS=str(self.calls),
            FAKE_MARKER=self.marker,
            FAKE_PYTHON=sys.executable,
            FAKE_STANDIN=str(self.standin),
            LC_ALL="C",
            PHASE0B_SHELL_LOG=str(self.shell_log),
            PHASE0B_FLAME_BIN_PATTERN=f"{self.marker}\\.sh",
            PHASE0B_SAMPLE_CMD=str(self.bin_dir / "sample"),
            PHASE0B_SENDDMP_PATTERN="no_such_senddmp_process",
        )
        env.update(extra)
        return env

    def run(
        self, argv: list[str], *, reap: bool = True, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        """Run a script. reap=False leaves the stand-in alone (call reap_standin() afterwards)."""
        proc = subprocess.run(
            ["bash", *argv],
            env=self.env(**extra),
            cwd=self.repo,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )
        if reap:
            self.reap_standin()
        return proc

    def reap_standin(self) -> None:
        subprocess.run(["pkill", "-9", "-f", self.marker], check=False)

    def flame_pid(self) -> int:
        return int((self.calls / "flame.pid").read_text(encoding="utf-8"))

    def flame_alive(self) -> bool:
        try:
            os.kill(self.flame_pid(), 0)
        except ProcessLookupError:
            return False
        return True

    def flame_signals(self) -> list[str]:
        path = self.calls / "flame_signals.log"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def open_argv(self) -> list[list[str]]:
        path = self.calls / "open_argv.log"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def client_calls(self) -> list[list[str]]:
        path = self.calls / "client_argv.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def files(self, subdir: str) -> list[str]:
        base = self.app_support / subdir
        return sorted(p.name for p in base.iterdir()) if base.exists() else []


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(
        tmp_path,
        ["p3_1a_read", "p1_mcp", "p2_4_stress", "p1_bridge", "p1_transport", "p2_4_restart"],
    )


def result_section(out: str) -> str:
    return out.split("(latest) ---", 1)[1]


def test_standard_e2e_runs_the_client_and_reports_a_clean_exit(sandbox: Sandbox) -> None:
    proc = sandbox.run(["scripts/run_e2e.sh", "p3_1a_read", "30"], FAKE_RESULTS_SUBDIR="p3")

    assert proc.returncode == 0, proc.stderr
    assert sandbox.client_calls() == [["p3_1a_read", "--run-dir", str(sandbox.app_support / "run")]]
    assert (sandbox.calls / "sockets_seen.log").read_text(encoding="utf-8") == "p3_1a_read 1\n"
    out = proc.stdout
    assert "=== exit route: exited (p3_1a_read_e2e.py exit=0) ===" in out
    assert "OK: no sockets remain." in out
    assert "no new senddmp process after launch." in out.lower()
    assert '"kind": "whole"' in result_section(out)
    assert '"kind": "stage"' not in out
    launch = (sandbox.calls / "open.log").read_text(encoding="utf-8")
    assert "-n -a /opt/Autodesk/flame_2025.2.4/bin/flame.app" in launch
    assert f"DL_PYTHON_HOOK_PATH={sandbox.app_support}/dev_hooks" in launch
    assert f"FLAME_MCP_BRIDGE_DIR={sandbox.app_support}/bridge_dev" in launch
    assert "--start-project=flame_mcp_probe" in launch
    assert (sandbox.app_support / "dev_hooks" / "flame_mcp_bridge.py").is_file()
    assert (sandbox.app_support / "bridge_dev" / "impl.py").is_file()
    assert [n for n in sandbox.files("p3") if n.startswith("launch_")] != []
    assert not any(n.startswith(("crash_sample", "timeout_sample")) for n in sandbox.files("p3"))


def test_a_failing_client_does_not_change_the_exit_code_of_the_run(sandbox: Sandbox) -> None:
    proc = sandbox.run(
        ["scripts/run_e2e.sh", "p3_1a_read", "30"], FAKE_RESULTS_SUBDIR="p3", FAKE_CLIENT_EXIT="3"
    )

    assert proc.returncode == 0
    assert "=== exit route: exited (p3_1a_read_e2e.py exit=3) ===" in proc.stdout


def test_stage_files_are_listed_only_for_the_e2es_that_do_not_filter_them(
    sandbox: Sandbox,
) -> None:
    proc = sandbox.run(["scripts/run_e2e.sh", "p1_mcp", "30"], FAKE_RESULTS_SUBDIR="p1")

    assert proc.returncode == 0, proc.stderr
    assert '"kind": "stage"' in result_section(proc.stdout)
    assert "=== running p1_mcp_e2e.py ===" in proc.stdout


def test_extra_args_and_the_crash_check_apply_to_the_two_p2_4_e2es(sandbox: Sandbox) -> None:
    proc = sandbox.run(
        ["scripts/run_e2e.sh", "p2_4_stress", "30"],
        FAKE_RESULTS_SUBDIR="p2",
        FAKE_CLIENT_CRASH="1",
        E2E_EXTRA_ARGS="--quick  --n 3",
    )

    assert proc.returncode == 0, proc.stderr
    assert sandbox.client_calls() == [
        ["p2_4_stress", "--run-dir", str(sandbox.app_support / "run"), "--quick", "--n", "3"]
    ]
    out = proc.stdout
    assert "shell.log abnormal termination (lines after launch): yes" in out
    assert (
        "=== exit route: crashed (p2_4_stress_e2e.py exit=0, "
        "shell.log abnormal termination: 1) ===" in out
    )
    assert [n for n in sandbox.files("p2") if n.startswith("crash_sample_")] != []


def test_extra_args_are_ignored_by_the_e2es_that_never_took_them(sandbox: Sandbox) -> None:
    proc = sandbox.run(
        ["scripts/run_e2e.sh", "p3_1a_read", "30"],
        FAKE_RESULTS_SUBDIR="p3",
        E2E_EXTRA_ARGS="--quick",
    )

    assert proc.returncode == 0, proc.stderr
    assert sandbox.client_calls() == [["p3_1a_read", "--run-dir", str(sandbox.app_support / "run")]]


def test_a_flame_that_does_not_exit_is_sampled_and_killed_after_the_timeout(
    sandbox: Sandbox,
) -> None:
    try:
        proc = sandbox.run(
            ["scripts/run_e2e.sh", "p3_1a_read", "2"],
            reap=False,
            FAKE_RESULTS_SUBDIR="p3",
            FAKE_CLIENT_ENDS_FLAME="0",
        )

        assert proc.returncode == 0, proc.stderr
        assert "=== exit route: timeout (p3_1a_read_e2e.py exit=0) ===" in proc.stdout
        assert "TIMEOUT" in proc.stdout
        assert [n for n in sandbox.files("p3") if n.startswith("timeout_sample_")] != []
        sample_calls = (sandbox.calls / "sample.log").read_text(encoding="utf-8").split()
        assert sample_calls[:2] == ["sample", str(sandbox.flame_pid())]
        assert not sandbox.flame_alive()
    finally:
        sandbox.reap_standin()


def test_bridge_orchestrator_has_the_client_wait_for_the_socket(sandbox: Sandbox) -> None:
    proc = sandbox.run(["scripts/run_e2e.sh", "p1_bridge", "30"], FAKE_RESULTS_SUBDIR="p1")

    assert proc.returncode == 0, proc.stderr
    assert sandbox.client_calls() == [["p1_bridge", "--wait-socket-s", "120"]]
    assert "=== exit route: exited (p1_bridge_e2e.py exit=0) ===" in proc.stdout
    assert '"kind": "stage"' in result_section(proc.stdout)


def test_transport_orchestrator_expects_the_client_to_have_killed_flame_and_keeps_inflight_records(
    sandbox: Sandbox,
) -> None:
    inflight = sandbox.app_support / "run" / "inflight"
    inflight.mkdir(parents=True)
    (inflight / "old.json").write_text("{}", encoding="utf-8")

    proc = sandbox.run(["scripts/run_e2e.sh", "p1_transport", "30"], FAKE_RESULTS_SUBDIR="p1")

    assert proc.returncode == 0, proc.stderr
    assert sandbox.client_calls() == [
        ["p1_transport", "--run-dir", str(sandbox.app_support / "run")]
    ]
    assert "=== exit route: killed_or_exited (p1_transport_e2e.py exit=0) ===" in proc.stdout
    assert (inflight / "old.json").read_text(encoding="utf-8") == "{}"
    added = proc.stdout.split("--- inflight files added by this run (not deleted) ---")[1]
    assert "old.json" not in added.split("---")[0]
    assert [n for n in sandbox.files("p1") if n.startswith("launch_transport_")] != []


def test_transport_orchestrator_stops_a_flame_the_client_left_running(sandbox: Sandbox) -> None:
    script = sandbox.repo / "scripts" / "run_p1_transport_e2e.sh"
    script.write_text(
        replace_once(
            script.read_text(encoding="utf-8"),
            "POST_RUN_EXIT_WAIT_S=15\n",
            "POST_RUN_EXIT_WAIT_S=2\n",
        ),
        encoding="utf-8",
    )

    proc = sandbox.run(
        ["scripts/run_e2e.sh", "p1_transport"],
        FAKE_RESULTS_SUBDIR="p1",
        FAKE_CLIENT_ENDS_FLAME="0",
    )

    assert proc.returncode == 0, proc.stderr
    assert "=== exit route: still_alive_after_e2e (p1_transport_e2e.py exit=0) ===" in proc.stdout


def test_restart_orchestrator_runs_two_phases_and_exits_zero_when_both_pass(
    sandbox: Sandbox,
) -> None:
    proc = sandbox.run(["scripts/run_e2e.sh", "p2_4_restart", "30"], FAKE_RESULTS_SUBDIR="p2")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = sandbox.client_calls()
    assert [c[:3] for c in calls] == [
        ["p2_4_restart", "--phase", "1"],
        ["p2_4_restart", "--phase", "2"],
    ]
    out = proc.stdout
    assert "=== [phase 1] exit route: exited (p2_4_restart_e2e.py exit=0) ===" in out
    assert "=== [phase 2] exit route: exited (p2_4_restart_e2e.py exit=0) ===" in out
    assert "=== overall: phase1 outcome=exited e2e=0 / phase2 outcome=exited e2e=0 ===" in out
    names = sandbox.files("p2")
    assert sum(n.startswith("launch_") and "_p24_phase1" in n for n in names) == 1
    assert sum(n.startswith("launch_") and "_p24_phase2" in n for n in names) == 1
    assert len((sandbox.calls / "open.log").read_text(encoding="utf-8").splitlines()) == 2


def test_restart_orchestrator_does_not_start_phase_2_when_phase_1_failed(
    sandbox: Sandbox,
) -> None:
    proc = sandbox.run(
        ["scripts/run_e2e.sh", "p2_4_restart", "2"],
        FAKE_RESULTS_SUBDIR="p2",
        FAKE_CLIENT_ENDS_FLAME="0",
    )

    assert proc.returncode == 1
    assert "Not starting the second run." in proc.stderr
    assert len(sandbox.client_calls()) == 1
    assert [n for n in sandbox.files("p2") if "timeout_sample" in n and "_p24_phase1" in n] != []


def test_a_launch_that_fails_is_reported_without_running_the_client(sandbox: Sandbox) -> None:
    (sandbox.bin_dir / "open").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    proc = sandbox.run(["scripts/run_e2e.sh", "p3_1a_read", "30"], FAKE_RESULTS_SUBDIR="p3")

    assert proc.returncode == 0, proc.stderr
    assert sandbox.client_calls() == []
    assert "=== exit route: launch_failed (p3_1a_read_e2e.py exit=N/A) ===" in proc.stdout
    assert "no result file found." in proc.stdout


def test_w3_runs_when_its_setup_file_is_there(sandbox: Sandbox) -> None:
    setup = sandbox.root / "setup.timewarp_node"
    setup.write_text("x", encoding="utf-8")
    (sandbox.repo / "scripts" / "w3_timewarp_e2e.py").write_text(FAKE_CLIENT, encoding="utf-8")

    proc = sandbox.run(
        ["scripts/run_e2e.sh", "w3_timewarp", "30"],
        FAKE_RESULTS_SUBDIR="p3",
        FLAME_MCP_W3_SETUP=str(setup),
    )

    assert proc.returncode == 0, proc.stderr
    assert "SKIP" not in proc.stderr
    assert sandbox.client_calls() == [
        ["w3_timewarp", "--run-dir", str(sandbox.app_support / "run")]
    ]
    assert "=== exit route: exited (w3_timewarp_e2e.py exit=0) ===" in proc.stdout


@pytest.mark.parametrize("name", ["p3_1a_read", "p1_bridge", "p1_transport", "p2_4_restart"])
def test_the_orchestrators_also_run_under_a_utf8_locale(name: str, sandbox: Sandbox) -> None:
    """Checks the orchestrators run under a UTF-8 locale.

    Under a UTF-8 locale, bash 3.2 reads "$VAR" followed by a non-ASCII character as a longer
    variable name and stops with "unbound variable". The orchestrators print only ASCII; this
    guards against that coming back.
    """
    proc = sandbox.run(
        ["scripts/run_e2e.sh", name, "30"], FAKE_RESULTS_SUBDIR="p1", LC_ALL="en_US.UTF-8"
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unbound variable" not in proc.stderr
    assert len(sandbox.client_calls()) == (2 if name == "p2_4_restart" else 1)


def test_regress_runs_the_named_e2es_then_the_clean_up_and_summarises_them(
    sandbox: Sandbox,
) -> None:
    (sandbox.repo / "scripts" / "p2_4_clean_probe_project.py").write_text(
        FAKE_CLIENT, encoding="utf-8"
    )

    proc = sandbox.run(
        ["scripts/regress.sh", "p3_1a_read", "p1_transport", "p2_4_stress"],
        FAKE_RESULTS_SUBDIR="p3",
        FAKE_CLIENT_CRASH="1",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    runs = sorted((sandbox.app_support / "p3").glob("regress_2*"))
    assert len(runs) == 1
    summary = (runs[0] / "summary.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split(" start=")[0] for line in summary[:3]] == [
        "p3_1a_read#1",
        "p1_transport#1",
        "p2_4_stress#1",
    ]
    assert " rc=0 abnormal=0 === exit route: exited (p3_1a_read_e2e.py exit=0) ===" in summary[0]
    assert "exit route: killed_or_exited (p1_transport_e2e.py exit=0)" in summary[1]
    assert " rc=0 abnormal=2 " in summary[2]
    assert (
        "exit route: crashed (p2_4_stress_e2e.py exit=0, shell.log abnormal termination: 1)"
        in summary[2]
    )
    assert summary[3].startswith("clean rc=0 ")
    assert summary[4:] == ["DONE"]
    assert (runs[0] / "p3_1a_read_1.log").is_file()
    assert (runs[0] / "clean.log").is_file()
    assert (sandbox.app_support / "p3" / "regress_latest.txt").read_text(
        encoding="utf-8"
    ).strip() == str(runs[0])
    assert [c[0] for c in sandbox.client_calls()] == [
        "p3_1a_read",
        "p1_transport",
        "p2_4_stress",
        "p2_4_clean_probe_project.py",
    ]


EXPECTED_OPEN_ARGV_TAIL = [
    "--args",
    "--start-project=flame_mcp_probe",
    "--start-workspace=Workspace",
    "--create-workspace",
    "--no-confirm",
]


@pytest.mark.parametrize(
    ("name", "opens"),
    [("p3_1a_read", 1), ("p1_bridge", 1), ("p1_transport", 1), ("p2_4_restart", 2)],
)
def test_every_launch_passes_open_the_exact_arguments_the_old_scripts_did(
    name: str, opens: int, sandbox: Sandbox
) -> None:
    """The list is the `open -n -a ...` line of the per-e2e orchestrators that run_e2e.sh
    replaced (the same in all of them), with the paths of this sandbox filled in."""
    proc = sandbox.run(["scripts/run_e2e.sh", name, "30"], FAKE_RESULTS_SUBDIR="p1")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    expected = [
        "-n",
        "-a",
        "/opt/Autodesk/flame_2025.2.4/bin/flame.app",
        "--env",
        f"DL_PYTHON_HOOK_PATH={sandbox.app_support}/dev_hooks",
        "--env",
        f"FLAME_MCP_BRIDGE_DIR={sandbox.app_support}/bridge_dev",
        "--env",
        "DL_DEBUG_PYTHON_HOOKS=1",
        *EXPECTED_OPEN_ARGV_TAIL,
    ]
    assert sandbox.open_argv() == [expected] * opens


@pytest.mark.parametrize(
    ("name", "opens"),
    [("p3_1a_read", 1), ("p1_bridge", 1), ("p1_transport", 1), ("p2_4_restart", 2)],
)
def test_the_variables_change_the_app_and_the_project_every_launch_passes_to_open(
    name: str, opens: int, sandbox: Sandbox
) -> None:
    proc = sandbox.run(
        ["scripts/run_e2e.sh", name, "30"],
        FAKE_RESULTS_SUBDIR="p1",
        FLAME_MCP_E2E_FLAME_VERSION="2026.2.3",
        FLAME_MCP_E2E_PROJECT="flame_mcp_probe_2026",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    expected = [
        "-n",
        "-a",
        "/opt/Autodesk/flame_2026.2.3/bin/flame.app",
        "--env",
        f"DL_PYTHON_HOOK_PATH={sandbox.app_support}/dev_hooks",
        "--env",
        f"FLAME_MCP_BRIDGE_DIR={sandbox.app_support}/bridge_dev",
        "--env",
        "DL_DEBUG_PYTHON_HOOKS=1",
        "--args",
        "--start-project=flame_mcp_probe_2026",
        "--start-workspace=Workspace",
        "--create-workspace",
        "--no-confirm",
    ]
    assert sandbox.open_argv() == [expected] * opens


def test_a_senddmp_that_appeared_after_launch_gets_TERM_and_an_older_one_does_not(
    sandbox: Sandbox,
) -> None:
    older = subprocess.Popen(["sleep", "300"])
    newer = subprocess.Popen(["sleep", "300"])
    try:
        (sandbox.calls / "senddmp_before").write_text(f"{older.pid}\n", encoding="utf-8")
        (sandbox.calls / "senddmp_after").write_text(
            f"{older.pid}\n{newer.pid}\n", encoding="utf-8"
        )

        proc = sandbox.run(
            ["scripts/run_e2e.sh", "p3_1a_read", "30"],
            FAKE_RESULTS_SUBDIR="p3",
            PHASE0B_SENDDMP_PATTERN=FAKE_SENDDMP_PATTERN,
        )

        assert proc.returncode == 0, proc.stderr
        assert f"  senddmp pid={newer.pid}: TERM" in proc.stdout
        assert f"senddmp pid={older.pid}" not in proc.stdout
        assert newer.wait(timeout=10) == -signal.SIGTERM
        assert older.poll() is None
    finally:
        for child in (older, newer):
            child.kill()
            child.wait()


@pytest.mark.parametrize("ignores_term", [False, True])
def test_a_crashed_flame_is_sampled_then_sent_TERM_and_killed_if_it_stays(
    ignores_term: bool, sandbox: Sandbox
) -> None:
    try:
        proc = sandbox.run(
            ["scripts/run_e2e.sh", "p2_4_stress", "30"],
            reap=False,
            FAKE_RESULTS_SUBDIR="p2",
            FAKE_CLIENT_CRASH="1",
            FAKE_CLIENT_ENDS_FLAME="0",
            FAKE_FLAME_IGNORE_TERM="1" if ignores_term else "0",
        )

        assert proc.returncode == 0, proc.stderr
        assert "exit route: crashed" in proc.stdout
        sample_calls = (sandbox.calls / "sample.log").read_text(encoding="utf-8").split()
        assert sample_calls[:2] == ["sample", str(sandbox.flame_pid())]
        assert sandbox.flame_signals() == ["TERM"]
        assert not sandbox.flame_alive()
    finally:
        sandbox.reap_standin()


def regress_summary(sandbox: Sandbox) -> list[str]:
    runs = sorted((sandbox.app_support / "p3").glob("regress_2*"))
    assert len(runs) == 1
    return (runs[0] / "summary.txt").read_text(encoding="utf-8").splitlines()


def test_regress_records_why_a_run_has_no_route_line_when_w3_has_no_setup_file(
    sandbox: Sandbox,
) -> None:
    (sandbox.repo / "scripts" / "w3_timewarp_e2e.py").write_text(FAKE_CLIENT, encoding="utf-8")
    (sandbox.repo / "scripts" / "p2_4_clean_probe_project.py").write_text(
        FAKE_CLIENT, encoding="utf-8"
    )
    missing = sandbox.root / "missing.timewarp_node"

    proc = sandbox.run(
        ["scripts/regress.sh", "w3_timewarp", "p3_1a_read"],
        FAKE_RESULTS_SUBDIR="p3",
        FLAME_MCP_W3_SETUP=str(missing),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = regress_summary(sandbox)
    assert re.fullmatch(
        r"w3_timewarp#1 start=\d\d:\d\d:\d\d rc=0 abnormal=0 "
        rf"SKIPPED: required file not found: {re.escape(str(missing))}\. .*",
        summary[0],
    )
    assert "route=NONE" not in summary[0]
    assert "exit route: exited (p3_1a_read_e2e.py exit=0)" in summary[1]
    assert [c[0] for c in sandbox.client_calls()] == ["p3_1a_read", "p2_4_clean_probe_project.py"]


def test_regress_marks_a_run_with_no_route_line_and_no_skip_as_route_none(
    sandbox: Sandbox,
) -> None:
    (sandbox.repo / "scripts" / "p2_4_clean_probe_project.py").write_text(
        FAKE_CLIENT, encoding="utf-8"
    )
    stub = sandbox.repo / "scripts" / "run_e2e.sh"
    stub.write_text(
        '#!/bin/bash\ncase "$1" in --print-config | --list) exit 0 ;; esac\n'
        'echo "died before any route line"\nexit 4\n',
        encoding="utf-8",
    )

    proc = sandbox.run(["scripts/regress.sh", "p3_1a_read"], FAKE_RESULTS_SUBDIR="p3")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = regress_summary(sandbox)
    assert re.fullmatch(r"p3_1a_read#1 start=\d\d:\d\d:\d\d rc=4 abnormal=0 route=NONE", summary[0])
    assert summary[1].startswith("clean rc=0 ")
