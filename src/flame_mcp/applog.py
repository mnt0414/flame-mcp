"""Reads what Flame logged during a call, as a diagnostic channel of the envelope.

Reads Flame's own on-disk logs from the server process, so they stay readable
even after Flame has crashed. The log files live outside the Flame process and
this module only reads them.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DEFAULT_LEVELS: Final[tuple[str, ...]] = ("error", "warn")
DEFAULT_MAX_LINES: Final[int] = 50
DEFAULT_MAX_BYTES: Final[int] = 64 * 1024
CRASH_MARKER: Final[str] = "abnormal termination"

APP_LOG_ENV: Final[str] = "FLAME_MCP_APP_LOG"
SHELL_LOG_ENV: Final[str] = "FLAME_MCP_SHELL_LOG"


def _hostname_short() -> str:
    return socket.gethostname().split(".")[0]


def _version_slug(version: str) -> str:
    return version.replace(".", "")


def app_log_path(version: str) -> Path:
    override = os.environ.get(APP_LOG_ENV)
    if override:
        return Path(override)
    return Path(f"/opt/Autodesk/log/flame{_version_slug(version)}_{_hostname_short()}_app.log")


def shell_log_path(version: str) -> Path:
    override = os.environ.get(SHELL_LOG_ENV)
    if override:
        return Path(override)
    return Path(f"/opt/Autodesk/log/flame{_version_slug(version)}_{_hostname_short()}_shell.log")


@dataclass(frozen=True)
class LogMark:
    """A saved (inode, size) taken before an operation.

    Lets `read` / `crash_lines_since` afterward identify exactly what was appended
    since, or that the file rotated.
    """

    path: Path
    inode: int | None
    size: int


@dataclass(frozen=True)
class LogReadResult:
    lines: tuple[str, ...]
    rotated: bool
    reason: str | None


def _line_level(line: str) -> str | None:
    if not line.startswith("["):
        return None
    end = line.find("]")
    if end == -1:
        return None
    return line[1:end]


class LogWindow:
    """A window onto a single log file, anchored by `mark()`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def mark(self) -> LogMark:
        try:
            st = self._path.stat()
        except OSError:
            return LogMark(path=self._path, inode=None, size=0)
        return LogMark(path=self._path, inode=st.st_ino, size=st.st_size)

    def read(
        self,
        mark: LogMark,
        levels: tuple[str, ...] | None = DEFAULT_LEVELS,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> LogReadResult:
        raw, rotated, reason = self._read_new_bytes(mark, max_bytes)
        if reason is not None:
            return LogReadResult(lines=(), rotated=rotated, reason=reason)
        text = raw.decode("utf-8", errors="replace")
        candidate_lines = text.splitlines()
        if levels is None:
            filtered = candidate_lines
        else:
            filtered = [line for line in candidate_lines if _line_level(line) in levels]
        return LogReadResult(lines=tuple(filtered[-max_lines:]), rotated=rotated, reason=None)

    def crash_lines_since(
        self,
        mark: LogMark,
        max_lines: int = DEFAULT_MAX_LINES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> tuple[str, ...]:
        raw, _rotated, reason = self._read_new_bytes(mark, max_bytes)
        if reason is not None:
            return ()
        text = raw.decode("utf-8", errors="replace")
        matches = [line for line in text.splitlines() if CRASH_MARKER in line]
        return tuple(matches[-max_lines:])

    def tail(self, n: int) -> tuple[str, ...]:
        try:
            with open(self._path, "rb") as f:
                data = f.read()
        except OSError:
            return ()
        text = data.decode("utf-8", errors="replace")
        return tuple(text.splitlines()[-n:])

    def _read_new_bytes(self, mark: LogMark, max_bytes: int) -> tuple[bytes, bool, str | None]:
        try:
            st = self._path.stat()
        except OSError as exc:
            return b"", False, f"cannot stat {self._path}: {exc}"

        rotated = (mark.inode is not None and st.st_ino != mark.inode) or st.st_size < mark.size
        start = 0 if rotated else mark.size

        try:
            with open(self._path, "rb") as f:
                f.seek(start)
                data = f.read()
        except OSError as exc:
            return b"", rotated, f"cannot read {self._path}: {exc}"

        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data, rotated, None


def crash_lines_since(
    mark: LogMark,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[str, ...]:
    """Returns shell.log lines containing `abnormal termination` appended since `mark`.

    `mark.path` identifies the file.
    """
    return LogWindow(mark.path).crash_lines_since(mark, max_lines=max_lines, max_bytes=max_bytes)
