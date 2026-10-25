"""Startup tightening of the permissions under the flame-mcp directory.

The server (`private_fs.harden_app_dir`) and the bridge (`impl._harden_app_dir`, a copy: the bridge
is deployed alone into Flame and cannot import the server package) are held to the same tests.
The function makes the directories flame-mcp itself uses 0700 and the files in them 0600. It
only touches what the user owns, never follows or changes a link, stays inside the directory,
changes no content, and reports what it fixed and what it could not."""

from __future__ import annotations

import logging
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from flame_mcp import private_fs
from flame_mcp_bridge import impl

Harden = Callable[..., Any]


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def loose_file(path: Path, text: str = "x", mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _server_harden(root: Path, **kwargs: Any) -> Any:
    return SimpleNamespace(**private_fs.harden_app_dir(root, **kwargs).as_dict())


def _bridge_harden(root: Path, **kwargs: Any) -> Any:
    logger = logging.getLogger(private_fs.LOGGER_NAME)
    return SimpleNamespace(**impl._harden_app_dir(root, logger, **kwargs))


@pytest.fixture(params=["server", "bridge"])
def harden(request: pytest.FixtureRequest) -> Harden:
    return _server_harden if request.param == "server" else _bridge_harden


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "flame-mcp"
    base.mkdir()
    base.chmod(0o755)
    return base


def build_loose_tree(root: Path) -> dict[str, Path]:
    made = {
        "run_file": loose_file(root / "run" / "results" / "h1.json"),
        "inflight": loose_file(root / "run" / "inflight" / "r1.json", mode=0o666),
        "journal": loose_file(root / "journal" / "journal_20261023.jsonl"),
        "checkpoint": loose_file(root / "journal" / "checkpoints" / "cp" / "deep" / "n.clip_node"),
        "ordered": loose_file(root / "journal" / "checkpoints" / "ordered" / "o_ab" / "x.batch"),
        "setups": loose_file(root / "journal" / "setups" / "s_123" / "a.setup"),
        "spill": loose_file(root / "spill" / ("a" * 32 + ".json")),
        "log": loose_file(root / "logs" / "bridge.log", mode=0o666),
        "impl": loose_file(root / "bridge" / "impl.py", mode=0o644),
    }
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o777)
    return made


def test_directories_become_0700_and_files_0600_in_the_directories_flame_mcp_uses(
    root: Path, harden: Harden
) -> None:
    made = build_loose_tree(root)

    report = harden(root)

    assert mode_of(root) == 0o700
    for name in ("run", "journal", "spill", "logs", "bridge"):
        assert mode_of(root / name) == 0o700, name
    for name in ("results", "inflight"):
        assert mode_of(root / "run" / name) == 0o700, name
    for path in root.rglob("*"):
        assert mode_of(path) == (0o700 if path.is_dir() else 0o600), path
    assert report.failed == 0 and report.truncated is False
    assert (
        report.fixed == len(made) + len([p for p in root.rglob("*") if p.is_dir()]) + 1
    )  # every file, every directory below the root, and the root itself


def test_what_is_already_private_is_not_counted_as_fixed(root: Path, harden: Harden) -> None:
    build_loose_tree(root)
    harden(root)

    again = harden(root)

    assert again.fixed == 0 and again.failed == 0 and again.checked > 0


def test_development_output_beside_the_directories_flame_mcp_uses_is_left_alone(
    root: Path, harden: Harden
) -> None:
    build_loose_tree(root)
    note = loose_file(root / "scratch" / "p3" / "out.txt")
    (root / "scratch").chmod(0o755)
    (root / "scratch" / "p3").chmod(0o755)

    harden(root)

    assert mode_of(root / "scratch") == 0o755 and mode_of(root / "scratch" / "p3") == 0o755
    assert mode_of(note) == 0o644


def test_content_and_modification_time_are_not_changed(root: Path, harden: Harden) -> None:
    made = build_loose_tree(root)
    then = 1_700_000_000
    for path in made.values():
        os.utime(path, (then, then))
    before = {name: path.read_bytes() for name, path in made.items()}

    harden(root)

    for name, path in made.items():
        assert path.read_bytes() == before[name]
        assert int(path.stat().st_mtime) == then


def test_a_link_is_neither_followed_nor_changed(root: Path, tmp_path: Path, harden: Harden) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_dir.chmod(0o755)
    inside_it = loose_file(outside_dir / "keep.txt")
    outside_file = loose_file(tmp_path / "outside.txt")
    (root / "run").mkdir()
    (root / "run" / "dir_link").symlink_to(outside_dir)
    (root / "run" / "file_link").symlink_to(outside_file)
    link_modes = {n: mode_of(root / "run" / n) for n in ("dir_link", "file_link")}

    report = harden(root)

    assert mode_of(outside_dir) == 0o755 and mode_of(inside_it) == 0o644
    assert mode_of(outside_file) == 0o644
    assert {n: mode_of(root / "run" / n) for n in link_modes} == link_modes
    assert report.skipped == 2


def test_a_link_in_place_of_a_directory_flame_mcp_uses_is_not_followed(
    root: Path, tmp_path: Path, harden: Harden
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    inside_it = loose_file(elsewhere / "f.txt")
    (root / "journal").symlink_to(elsewhere)

    harden(root)

    assert mode_of(elsewhere) == 0o755 and mode_of(inside_it) == 0o644


def test_a_root_that_is_a_link_is_left_alone(tmp_path: Path, harden: Harden) -> None:
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o755)
    inside_it = loose_file(real / "run" / "f.txt")
    link = tmp_path / "flame-mcp"
    link.symlink_to(real)

    report = harden(link)

    assert mode_of(real) == 0o755 and mode_of(inside_it) == 0o644
    assert report.fixed == 0 and report.skipped == 1


def test_a_missing_root_is_not_an_error(tmp_path: Path, harden: Harden) -> None:
    report = harden(tmp_path / "never_made")
    assert (report.checked, report.fixed, report.failed) == (0, 0, 0)


def test_only_what_the_user_owns_is_changed(
    root: Path, monkeypatch: pytest.MonkeyPatch, harden: Harden
) -> None:
    made = build_loose_tree(root)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(root).st_uid + 1)

    report = harden(root)

    assert mode_of(made["journal"]) == 0o644 and mode_of(root) == 0o755
    assert report.fixed == 0 and report.skipped >= 1


def test_a_socket_and_other_special_files_are_left_as_they_are(
    root: Path, monkeypatch: pytest.MonkeyPatch, harden: Harden
) -> None:
    (root / "run").mkdir()
    monkeypatch.chdir(root / "run")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind("b.sock")
        os.chmod("b.sock", 0o660)
        os.mkfifo("pipe", 0o666)
        os.chmod("pipe", 0o666)

        harden(root)

        assert mode_of(root / "run" / "b.sock") == 0o660
        assert mode_of(root / "run" / "pipe") == 0o666
        assert mode_of(root / "run") == 0o700
    finally:
        listener.close()


def test_a_chmod_that_fails_is_logged_counted_and_the_rest_goes_on(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, harden: Harden
) -> None:
    made = build_loose_tree(root)
    real_chmod = os.chmod
    refused = str(made["journal"])

    def chmod(path: str | os.PathLike[str], mode: int, **kwargs: object) -> None:
        if str(path) == refused:
            raise PermissionError(1, "Operation not permitted", refused)
        real_chmod(path, mode, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "chmod", chmod)

    with caplog.at_level(logging.WARNING, logger=private_fs.LOGGER_NAME):
        report = harden(root)

    assert report.failed == 1
    assert mode_of(made["journal"]) == 0o644
    assert mode_of(made["spill"]) == 0o600 and mode_of(made["impl"]) == 0o600
    lines = [r.getMessage() for r in caplog.records if refused in r.getMessage()]
    assert len(lines) == 1 and "Operation not permitted" in lines[0]


def test_the_number_of_entries_looked_at_is_capped(
    root: Path, caplog: pytest.LogCaptureFixture, harden: Harden
) -> None:
    for n in range(10):
        loose_file(root / "journal" / f"f{n}.jsonl")

    with caplog.at_level(logging.WARNING, logger=private_fs.LOGGER_NAME):
        report = harden(root, max_entries=4)

    assert report.truncated is True and report.checked <= 4
    assert any("stopped" in r.getMessage() for r in caplog.records)


def test_the_time_spent_is_capped(root: Path, harden: Harden) -> None:
    for n in range(10):
        loose_file(root / "journal" / f"f{n}.jsonl")

    report = harden(root, budget_s=-1.0)

    assert report.truncated is True


def test_a_directory_in_use_that_lives_inside_the_root_under_another_name_is_tightened(
    root: Path, tmp_path: Path, harden: Harden
) -> None:
    custom = root / "my_journal"
    f = loose_file(custom / "j.jsonl")
    custom.chmod(0o755)
    outside = tmp_path / "outside"
    g = loose_file(outside / "j.jsonl")
    outside.chmod(0o755)

    harden(root, extra_dirs=[custom, outside])

    assert mode_of(custom) == 0o700 and mode_of(f) == 0o600
    assert mode_of(outside) == 0o755 and mode_of(g) == 0o644


def test_the_report_is_plain_data_for_flame_version_info(root: Path) -> None:
    build_loose_tree(root)
    report = private_fs.harden_app_dir(root).as_dict()
    assert set(report) == {"root", "checked", "fixed", "failed", "skipped", "truncated"}
    assert report["root"] == str(root) and report["failed"] == 0


def test_what_is_already_private_does_not_use_up_the_entry_limit(
    root: Path, harden: Harden
) -> None:
    journal = root / "journal"
    journal.mkdir()
    journal.chmod(0o700)
    for n in range(30):
        right = loose_file(journal / f"a{n:02d}.jsonl", mode=0o600)
        assert mode_of(right) == 0o600
    loose = [loose_file(journal / f"z{n:02d}.jsonl") for n in range(12)]
    root.chmod(0o700)

    fixed = 0
    runs = 0
    while runs < 20:
        runs += 1
        report = harden(root, max_entries=4)
        fixed += report.fixed
        if not report.truncated:
            break

    assert all(mode_of(path) == 0o600 for path in loose)
    assert fixed == 12 and runs <= 5
