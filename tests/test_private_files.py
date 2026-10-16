"""Files and directories the server writes (journal, spill, in-flight records) are private:
0600 for files, 0700 for directories it creates, whatever the process umask is. An existing
file or directory keeps the permissions it has. Spill files older than a week are swept at
startup."""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import envelope, transport
from flame_mcp.__main__ import build_server
from flame_mcp.journal import Journal


@pytest.fixture
def umask_zero() -> Iterator[None]:
    previous = os.umask(0)
    yield
    os.umask(previous)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def record(rid: str) -> dict[str, Any]:
    return {"type": "intent", "id": rid, "group": "g", "ts": "2026-10-14T00:00:00.000+00:00"}


# -- journal ----------------------------------------------------------------------------------


def test_new_journal_file_is_0600_and_its_new_directory_0700(
    tmp_path: Path, umask_zero: None
) -> None:
    journal = Journal(tmp_path / "journal")
    journal.append(record("i1"))
    journal.close()
    [path] = journal.files()
    assert mode_of(path) == 0o600
    assert mode_of(tmp_path / "journal") == 0o700


def test_existing_journal_file_and_directory_keep_their_permissions(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    directory.mkdir()
    directory.chmod(0o755)
    journal = Journal(directory)
    journal.append(record("i1"))
    journal.close()
    [path] = journal.files()
    path.chmod(0o644)
    journal = Journal(directory)
    journal.append(record("i2"))
    journal.close()
    assert mode_of(path) == 0o644
    assert mode_of(directory) == 0o755


# -- spill ------------------------------------------------------------------------------------


def test_spill_file_is_0600_and_its_new_directory_0700(tmp_path: Path, umask_zero: None) -> None:
    env = envelope.build_envelope(ok=True, result="x" * 5000, duration_ms=1)
    spill_dir = tmp_path / "spill"
    out = envelope.degrade_for_size(env, max_bytes=200, spill_dir=spill_dir)
    assert mode_of(Path(out["result"]["spilledTo"])) == 0o600
    assert mode_of(spill_dir) == 0o700


def test_existing_spill_directory_keeps_its_permissions(tmp_path: Path) -> None:
    spill_dir = tmp_path / "spill"
    spill_dir.mkdir()
    spill_dir.chmod(0o755)
    env = envelope.build_envelope(ok=True, result="x" * 5000, duration_ms=1)
    envelope.degrade_for_size(env, max_bytes=200, spill_dir=spill_dir)
    assert mode_of(spill_dir) == 0o755


def age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_spill_retention_is_seven_days() -> None:
    assert envelope.SPILL_MAX_AGE_S == 7 * 24 * 60 * 60


def test_cleanup_old_spills_removes_only_json_older_than_the_retention(tmp_path: Path) -> None:
    old = tmp_path / ("a" * 32 + ".json")
    old.write_text("{}")
    age(old, envelope.SPILL_MAX_AGE_S + 3600)
    fresh = tmp_path / ("b" * 32 + ".json")
    fresh.write_text("{}")
    age(fresh, envelope.SPILL_MAX_AGE_S - 3600)
    stray = tmp_path / "notes.txt"
    stray.write_text("keep")
    age(stray, 10 * envelope.SPILL_MAX_AGE_S)

    assert envelope.cleanup_old_spills(tmp_path) == 1

    assert not old.exists() and fresh.exists() and stray.exists()


def test_cleanup_old_spills_leaves_json_files_it_did_not_name(tmp_path: Path) -> None:
    own = tmp_path / ("e" * 32 + ".json")
    others = [tmp_path / "settings.json", tmp_path / ("g" * 32 + ".json"), tmp_path / "1.json"]
    for path in [own, *others]:
        path.write_text("{}")
        age(path, envelope.SPILL_MAX_AGE_S + 3600)

    assert envelope.cleanup_old_spills(tmp_path) == 1

    assert not own.exists() and all(path.exists() for path in others)


def test_cleanup_old_spills_does_not_raise_when_the_directory_cannot_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, pattern: str) -> Iterator[Path]:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "glob", refuse)

    assert envelope.cleanup_old_spills(tmp_path) == 0


def test_cleanup_old_spills_on_a_missing_directory_is_a_noop(tmp_path: Path) -> None:
    assert envelope.cleanup_old_spills(tmp_path / "never_made") == 0


def test_cleanup_old_spills_defaults_to_the_spill_directory_of_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(envelope.SPILL_DIR_ENV, str(tmp_path))
    old = tmp_path / ("c" * 32 + ".json")
    old.write_text("{}")
    age(old, envelope.SPILL_MAX_AGE_S + 3600)
    assert envelope.cleanup_old_spills() == 1
    assert not old.exists()


def test_server_startup_sweeps_old_spill_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spill_dir = tmp_path / "spill"
    spill_dir.mkdir()
    monkeypatch.setenv(envelope.SPILL_DIR_ENV, str(spill_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    old = spill_dir / ("d" * 32 + ".json")
    old.write_text("{}")
    age(old, envelope.SPILL_MAX_AGE_S + 3600)

    build_server(run_dir=tmp_path / "run")

    assert not old.exists()


# -- in-flight records ------------------------------------------------------------------------


def test_inflight_record_is_0600_and_its_directory_0700(tmp_path: Path, umask_zero: None) -> None:
    candidate = transport.BridgeCandidate(
        path=Path("/x/bridge.sock"),
        pid=4321,
        flame_version="2025.2.4",
        project_name=None,
        bridge_abi=1,
    )
    path = transport._write_inflight(
        tmp_path / "run", "rid", "lbl", "main", 10.0, candidate, "_result = 2", None
    )
    assert mode_of(path) == 0o600
    assert mode_of(path.parent) == 0o700
