"""scripts/install_bridge.sh and scripts/uninstall_bridge.sh, run against a fake HOME, a fake
stub destination (FLAME_MCP_STUB_DST) and a fake `pgrep`. Nothing here touches /opt or the real
Application Support directory."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL = REPO_ROOT / "scripts" / "install_bridge.sh"
UNINSTALL = REPO_ROOT / "scripts" / "uninstall_bridge.sh"
STUB_SRC = REPO_ROOT / "stub" / "flame_mcp_bridge.py"
IMPL_SRC = REPO_ROOT / "src" / "flame_mcp_bridge" / "impl.py"
OLD = "# old content\n"


class Env:
    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.home.mkdir()
        self.fake_bin = root / "bin"
        self.fake_bin.mkdir()
        self.stub_dir = root / "opt" / "python"
        self.stub = self.stub_dir / "flame_mcp_bridge.py"
        self.app = self.home / "Library" / "Application Support" / "flame-mcp"
        self.bridge_dir = self.app / "bridge"
        self.impl = self.bridge_dir / "impl.py"
        self.backups = self.app / "backups"
        self.set_flame_running(False)

    def set_flame_running(self, running: bool) -> None:
        pgrep = self.fake_bin / "pgrep"
        pgrep.write_text(f"#!/bin/sh\nexit {0 if running else 1}\n")
        pgrep.chmod(0o755)

    def run(
        self, script: Path, *args: str, umask: str = "022", override_stub: bool = True
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        }
        if override_stub:
            env["FLAME_MCP_STUB_DST"] = str(self.stub)
        return subprocess.run(
            ["/bin/bash", "-c", f'umask {umask}; exec "$0" "$@"', str(script), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def deploy_old(self) -> None:
        self.stub_dir.mkdir(parents=True)
        self.bridge_dir.mkdir(parents=True)
        self.stub.write_text(OLD)
        self.impl.write_text(OLD)
        pycache = self.stub_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "flame_mcp_bridge.cpython-311.pyc").write_bytes(b"pyc")
        (pycache / "other_hook.cpython-311.pyc").write_bytes(b"keep")
        (self.bridge_dir / "__pycache__").mkdir()
        (self.bridge_dir / "__pycache__" / "impl.cpython-311.pyc").write_bytes(b"pyc")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("."))


# -- install_bridge.sh ---------------------------------------------------------------------------


def test_install_writes_both_files_with_their_modes(env: Env) -> None:
    result = env.run(INSTALL)
    assert result.returncode == 0, result.stderr
    assert env.stub.read_bytes() == STUB_SRC.read_bytes()
    assert env.impl.read_bytes() == IMPL_SRC.read_bytes()
    assert mode_of(env.stub) == 0o644
    assert mode_of(env.impl) == 0o600
    assert mode_of(env.bridge_dir) & 0o022 == 0
    assert leftovers(env.stub_dir) == [] and leftovers(env.bridge_dir) == []


def test_install_modes_do_not_depend_on_the_umask(env: Env) -> None:
    result = env.run(INSTALL, umask="0")
    assert result.returncode == 0, result.stderr
    assert mode_of(env.stub) == 0o644
    assert mode_of(env.impl) == 0o600
    assert mode_of(env.bridge_dir) & 0o022 == 0


def test_install_backs_up_replaces_and_clears_the_stub_bytecode(env: Env) -> None:
    env.deploy_old()
    result = env.run(INSTALL)
    assert result.returncode == 0, result.stderr
    saved = {p.name.split(".pre_install_")[0]: p for p in env.backups.iterdir()}
    assert set(saved) == {"flame_mcp_bridge.py", "impl.py"}
    assert all(p.read_text() == OLD for p in saved.values())
    assert env.stub.read_bytes() == STUB_SRC.read_bytes()
    assert not (env.stub_dir / "__pycache__" / "flame_mcp_bridge.cpython-311.pyc").exists()
    assert (env.stub_dir / "__pycache__" / "other_hook.cpython-311.pyc").exists()
    assert not (env.bridge_dir / "__pycache__").exists()


def test_install_replaces_by_rename_and_never_rewrites_the_old_file_in_place(env: Env) -> None:
    env.deploy_old()
    held_stub = env.stub_dir / "held_stub"
    held_impl = env.bridge_dir / "held_impl"
    os.link(env.stub, held_stub)
    os.link(env.impl, held_impl)
    result = env.run(INSTALL)
    assert result.returncode == 0, result.stderr
    assert held_stub.read_text() == OLD
    assert held_impl.read_text() == OLD
    assert env.stub.read_bytes() == STUB_SRC.read_bytes()


def test_install_leaves_the_stub_alone_when_the_body_cannot_be_staged(env: Env) -> None:
    env.deploy_old()
    env.bridge_dir.chmod(0o500)
    try:
        result = env.run(INSTALL)
    finally:
        env.bridge_dir.chmod(0o700)
    assert result.returncode != 0
    assert env.stub.read_text() == OLD
    assert env.impl.read_text() == OLD
    assert leftovers(env.stub_dir) == []


def test_install_dry_run_writes_nothing(env: Env) -> None:
    result = env.run(INSTALL, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not env.stub_dir.exists()
    assert not env.app.exists()
    assert "0600" in result.stdout and "0644" in result.stdout


def test_install_refuses_while_flame_runs(env: Env) -> None:
    env.set_flame_running(True)
    result = env.run(INSTALL)
    assert result.returncode == 1
    assert not env.stub_dir.exists() and not env.app.exists()


def test_install_refuses_a_symlinked_destination_and_writes_neither(env: Env) -> None:
    env.deploy_old()
    target = env.stub_dir / "elsewhere.py"
    target.write_text("# target\n")
    env.impl.unlink()
    env.impl.symlink_to(target)
    result = env.run(INSTALL)
    assert result.returncode == 1
    assert env.stub.read_text() == OLD
    assert target.read_text() == "# target\n"


# -- uninstall_bridge.sh -------------------------------------------------------------------------


def make_data(env: Env) -> list[Path]:
    paths = []
    for name in ("journal", "run", "logs", "spill", "scratch"):
        directory = env.app / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "keep.txt").write_text("data")
        paths.append(directory)
    return paths


def test_uninstall_removes_the_stub_the_body_and_their_bytecode_after_backing_up(
    env: Env,
) -> None:
    env.deploy_old()
    data = make_data(env)
    result = env.run(UNINSTALL)
    assert result.returncode == 0, result.stderr
    assert not env.stub.exists() and not env.bridge_dir.exists()
    assert not (env.stub_dir / "__pycache__" / "flame_mcp_bridge.cpython-311.pyc").exists()
    assert (env.stub_dir / "__pycache__" / "other_hook.cpython-311.pyc").exists()
    saved = {p.name.split(".pre_uninstall_")[0]: p for p in env.backups.iterdir()}
    assert set(saved) == {"flame_mcp_bridge.py", "impl.py"}
    assert all(p.read_text() == OLD for p in saved.values())
    for directory in data:
        assert (directory / "keep.txt").read_text() == "data"
        assert str(directory) in result.stdout


def test_uninstall_dry_run_removes_nothing(env: Env) -> None:
    env.deploy_old()
    result = env.run(UNINSTALL, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert env.stub.read_text() == OLD and env.impl.read_text() == OLD
    assert not env.backups.exists()
    assert "[dry-run]" in result.stdout


def test_uninstall_refuses_while_flame_runs(env: Env) -> None:
    env.deploy_old()
    env.set_flame_running(True)
    result = env.run(UNINSTALL)
    assert result.returncode == 1
    assert env.stub.exists() and env.impl.exists()


def test_uninstall_dry_run_goes_on_with_a_warning_while_flame_runs(env: Env) -> None:
    env.deploy_old()
    env.set_flame_running(True)
    result = env.run(UNINSTALL, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Flame is running" in result.stderr
    assert env.stub.exists() and env.impl.exists() and not env.backups.exists()


def test_uninstall_keeps_and_reports_a_symlinked_stub(env: Env) -> None:
    env.deploy_old()
    target = env.stub_dir / "elsewhere.py"
    target.write_text("# target\n")
    env.stub.unlink()
    env.stub.symlink_to(target)
    result = env.run(UNINSTALL)
    assert result.returncode == 1
    assert env.stub.is_symlink() and target.read_text() == "# target\n"
    assert "symlink" in result.stderr
    assert not env.impl.exists()


def test_uninstall_keeps_the_body_directory_when_something_else_is_in_it(env: Env) -> None:
    env.deploy_old()
    (env.bridge_dir / "mine.txt").write_text("mine")
    result = env.run(UNINSTALL)
    assert result.returncode == 1
    assert not env.impl.exists()
    assert (env.bridge_dir / "mine.txt").read_text() == "mine"
    assert "mine.txt" in result.stderr


def test_uninstall_with_nothing_installed_is_a_clean_noop(env: Env) -> None:
    env.stub_dir.mkdir(parents=True)
    result = env.run(UNINSTALL)
    assert result.returncode == 0, result.stderr
    assert not env.backups.exists()


def test_uninstall_dry_run_names_the_real_stub_location_without_an_override(env: Env) -> None:
    result = env.run(UNINSTALL, "--dry-run", override_stub=False)
    assert result.returncode == 0, result.stderr
    assert "/opt/Autodesk/shared/python/flame_mcp_bridge.py" in result.stdout
    assert "[dry-run]" in result.stdout or "Not found:" in result.stdout


def test_uninstall_rejects_unknown_arguments(env: Env) -> None:
    result = env.run(UNINSTALL, "--force")
    assert result.returncode == 1
