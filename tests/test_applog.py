"""Tests for the app.log window (no Flame required)."""

from __future__ import annotations

import inspect
import socket
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from flame_mcp import applog


@pytest.fixture
def scratch_dir(tmp_path: Path) -> Iterator[Path]:
    # AF_UNIX path-length concerns from test_bridge_impl.py don't apply here
    # (plain file I/O), so pytest's own tmp_path is fine.
    yield tmp_path


def test_app_log_path_matches_observed_naming(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(applog.APP_LOG_ENV, raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "example-host.local")
    path = applog.app_log_path("2025.2.4")
    assert path == Path("/opt/Autodesk/log/flame202524_example-host_app.log")


def test_shell_log_path_matches_observed_naming(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(applog.SHELL_LOG_ENV, raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "example-host.local")
    path = applog.shell_log_path("2025.2.4")
    assert path == Path("/opt/Autodesk/log/flame202524_example-host_shell.log")


def test_app_log_path_env_override(monkeypatch: pytest.MonkeyPatch, scratch_dir: Path) -> None:
    override = scratch_dir / "custom_app.log"
    monkeypatch.setenv(applog.APP_LOG_ENV, str(override))
    assert applog.app_log_path("2025.2.4") == override


def test_shell_log_path_env_override(monkeypatch: pytest.MonkeyPatch, scratch_dir: Path) -> None:
    override = scratch_dir / "custom_shell.log"
    monkeypatch.setenv(applog.SHELL_LOG_ENV, str(override))
    assert applog.shell_log_path("2025.2.4") == override


def test_log_paths_take_the_version_they_are_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(applog.APP_LOG_ENV, raising=False)
    monkeypatch.delenv(applog.SHELL_LOG_ENV, raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "example-host.local")
    assert applog.app_log_path("2026.2.3") == Path(
        "/opt/Autodesk/log/flame202623_example-host_app.log"
    )
    assert applog.shell_log_path("2026.2.3") == Path(
        "/opt/Autodesk/log/flame202623_example-host_shell.log"
    )


@pytest.mark.parametrize("log_path", [applog.app_log_path, applog.shell_log_path])
def test_log_paths_require_a_version(log_path: Callable[..., Path]) -> None:
    assert inspect.signature(log_path).parameters["version"].default is inspect.Parameter.empty


def test_read_returns_only_lines_appended_since_mark(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("[notice] before mark\n")
    window = applog.LogWindow(path)
    mark = window.mark()

    with open(path, "a") as f:
        f.write("[error] boom happened\n")
        f.write("[warn] careful\n")

    result = window.read(mark)
    assert result.lines == ("[error] boom happened", "[warn] careful")
    assert result.rotated is False
    assert result.reason is None


def test_read_before_any_write_is_empty(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("[error] already there\n")
    window = applog.LogWindow(path)
    mark = window.mark()
    result = window.read(mark)
    assert result.lines == ()
    assert result.rotated is False


def test_level_filtering_default_is_error_and_warn(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("")
    window = applog.LogWindow(path)
    mark = window.mark()
    with open(path, "a") as f:
        f.write("[notice] routine\n")
        f.write("[error] bad\n")
        f.write("[user] hi\n")
        f.write("[warn] hmm\n")

    result = window.read(mark)
    assert result.lines == ("[error] bad", "[warn] hmm")


def test_level_filtering_custom_levels(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("")
    window = applog.LogWindow(path)
    mark = window.mark()
    with open(path, "a") as f:
        f.write("[notice] routine\n")
        f.write("[error] bad\n")

    result = window.read(mark, levels=("notice",))
    assert result.lines == ("[notice] routine",)


def test_levels_none_returns_every_line_unfiltered(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("")
    window = applog.LogWindow(path)
    mark = window.mark()
    with open(path, "a") as f:
        f.write("[notice] routine\n")
        f.write("plain line with no bracket\n")
        f.write("[error] bad\n")

    result = window.read(mark, levels=None)
    assert result.lines == ("[notice] routine", "plain line with no bracket", "[error] bad")


def test_rotation_via_rename_reads_new_file_from_start(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("[error] old file line\n")
    window = applog.LogWindow(path)
    mark = window.mark()

    rotated_away = scratch_dir / "app.log.1"
    path.rename(rotated_away)
    path.write_text("[error] new file line\n")

    result = window.read(mark)
    assert result.rotated is True
    assert result.lines == ("[error] new file line",)


def test_rotation_via_truncation_same_inode(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    with open(path, "a") as f:
        f.write("[error] a long line that makes the mark size non-trivial\n")
    window = applog.LogWindow(path)
    mark = window.mark()

    # Truncate in place (same inode, smaller size) rather than rename+recreate.
    with open(path, "w") as f:
        f.write("[error] short\n")

    result = window.read(mark)
    assert result.rotated is True
    assert result.lines == ("[error] short",)


def test_mark_on_missing_file_does_not_raise(scratch_dir: Path) -> None:
    path = scratch_dir / "does_not_exist.log"
    window = applog.LogWindow(path)
    mark = window.mark()
    assert mark.inode is None
    assert mark.size == 0


def test_read_after_file_deleted_returns_reason_not_exception(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("[error] here for now\n")
    window = applog.LogWindow(path)
    mark = window.mark()
    path.unlink()

    result = window.read(mark)
    assert result.lines == ()
    assert result.reason is not None


def test_tail_on_missing_file_returns_empty(scratch_dir: Path) -> None:
    window = applog.LogWindow(scratch_dir / "nope.log")
    assert window.tail(10) == ()


def test_max_lines_caps_to_the_most_recent(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("")
    window = applog.LogWindow(path)
    mark = window.mark()
    with open(path, "a") as f:
        for i in range(10):
            f.write(f"[error] line {i}\n")

    result = window.read(mark, max_lines=3)
    assert result.lines == ("[error] line 7", "[error] line 8", "[error] line 9")


def test_max_bytes_caps_the_window(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_text("")
    window = applog.LogWindow(path)
    mark = window.mark()
    line = "[error] " + ("x" * 100) + "\n"
    with open(path, "a") as f:
        for _ in range(20):
            f.write(line)

    result = window.read(mark, max_bytes=250, max_lines=100)
    # Only the tail of the appended bytes survives the byte cap, so the
    # oldest lines are gone and (at most) the last few remain.
    assert len(result.lines) < 20
    assert all(line.startswith("[error]") or line == "" for line in result.lines)


def test_binary_mixed_content_does_not_raise(scratch_dir: Path) -> None:
    path = scratch_dir / "app.log"
    path.write_bytes(b"")
    window = applog.LogWindow(path)
    mark = window.mark()
    with open(path, "ab") as f:
        f.write(b"[error] before binary\n")
        f.write(b"\xff\xfe\x00\x01 not valid utf-8 \n")
        f.write(b"[warn] after binary\n")

    result = window.read(mark)
    assert "[error] before binary" in result.lines
    assert "[warn] after binary" in result.lines


def test_crash_lines_since_finds_abnormal_termination(scratch_dir: Path) -> None:
    path = scratch_dir / "shell.log"
    path.write_text("startup line\n")
    window = applog.LogWindow(path)
    mark = window.mark()
    with open(path, "a") as f:
        f.write("2026-09-27 something normal\n")
        f.write("2026-09-27 abnormal termination detected, pid=123\n")
        f.write("2026-09-27 cleanup\n")

    method_result = window.crash_lines_since(mark)
    assert method_result == ("2026-09-27 abnormal termination detected, pid=123",)

    function_result = applog.crash_lines_since(mark)
    assert function_result == method_result


def test_crash_lines_since_empty_when_no_crash(scratch_dir: Path) -> None:
    path = scratch_dir / "shell.log"
    path.write_text("")
    window = applog.LogWindow(path)
    mark = window.mark()
    with open(path, "a") as f:
        f.write("nothing bad happened\n")

    assert window.crash_lines_since(mark) == ()
