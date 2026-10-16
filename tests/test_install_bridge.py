"""scripts/install_bridge.sh: what it does when Flame is running, and that --dry-run writes nothing.

A fake `pgrep` on PATH reports Flame as running or not, and HOME points into tmp_path so any write
would show there. Only --dry-run is ever run with Flame absent: a real install writes under /opt.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install_bridge.sh"

FAKE_PGREP_RUNNING = "#!/bin/sh\necho 4242\nexit 0\n"
FAKE_PGREP_ABSENT = "#!/bin/sh\nexit 1\n"


def run_installer(
    tmp_path: Path, *args: str, flame_running: bool
) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pgrep = bin_dir / "pgrep"
    pgrep.write_text(FAKE_PGREP_RUNNING if flame_running else FAKE_PGREP_ABSENT, encoding="utf-8")
    pgrep.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLAME_MCP_")}
    env.update(HOME=str(home), PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", LC_ALL="C")
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc, home


def written_under(home: Path) -> list[str]:
    return sorted(str(p.relative_to(home)) for p in home.rglob("*"))


def test_dry_run_goes_on_with_a_warning_while_flame_is_running(tmp_path: Path) -> None:
    proc, home = run_installer(tmp_path, "--dry-run", flame_running=True)

    assert proc.returncode == 0, proc.stderr
    assert "Flame is running" in proc.stderr
    assert "dry run" in proc.stderr
    assert "[dry-run] would copy" in proc.stdout
    assert "--dry-run finished: nothing was written" in proc.stdout
    assert written_under(home) == []


def test_install_is_refused_while_flame_is_running(tmp_path: Path) -> None:
    proc, home = run_installer(tmp_path, flame_running=True)

    assert proc.returncode == 1
    assert "Quit Flame" in proc.stderr
    assert "[dry-run]" not in proc.stdout
    assert written_under(home) == []


def test_dry_run_without_flame_writes_nothing(tmp_path: Path) -> None:
    proc, home = run_installer(tmp_path, "--dry-run", flame_running=False)

    assert proc.returncode == 0, proc.stderr
    assert "Flame is running" not in proc.stderr
    assert "--dry-run finished: nothing was written" in proc.stdout
    assert written_under(home) == []


@pytest.mark.parametrize("flame_running", [True, False])
def test_dry_run_never_creates_the_backups_directory(tmp_path: Path, flame_running: bool) -> None:
    proc, home = run_installer(tmp_path, "--dry-run", flame_running=flame_running)

    assert proc.returncode == 0, proc.stderr
    assert not (home / "Library" / "Application Support" / "flame-mcp" / "backups").exists()


def test_install_as_root_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("pgrep", FAKE_PGREP_ABSENT),
        ("id", '#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi\n'),
    ):
        tool = bin_dir / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)
    stub_dst = tmp_path / "opt" / "flame_mcp_bridge.py"
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLAME_MCP_")}
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        LC_ALL="C",
        FLAME_MCP_STUB_DST=str(stub_dst),
    )

    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )

    assert proc.returncode == 1
    assert "root" in proc.stderr
    assert written_under(home) == [] and not stub_dst.exists()
