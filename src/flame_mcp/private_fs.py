"""Directories and files that are private to the user, whatever the process umask is.

`make_private_dirs` makes new directories. `harden_app_dir` tightens, at startup, what is already
under the flame-mcp directory: the directories flame-mcp itself uses become 0700 and the files in
them 0600. The bridge has its own copy (`flame_mcp_bridge.impl._harden_app_dir`): it is deployed
alone into Flame and cannot import this package. `tests/test_private_fs.py` holds both to the same
tests.
"""

from __future__ import annotations

import logging
import os
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

PRIVATE_DIR_MODE: Final[int] = 0o700
PRIVATE_FILE_MODE: Final[int] = 0o600
LOGGER_NAME: Final[str] = "flame_mcp.private_fs"

APP_TREES: Final[tuple[str, ...]] = ("run", "journal", "spill", "logs", "bridge")
"""The directories below the flame-mcp directory that flame-mcp itself uses, and that are
tightened with everything in them. Anything else there is left as it is; it is out of other
users' reach once the flame-mcp directory itself is 0700."""
HARDEN_MAX_ENTRIES: Final[int] = 50_000
HARDEN_BUDGET_S: Final[float] = 2.0
MAX_LOGGED_FAILURES: Final[int] = 20

_CHMOD_NOFOLLOW: Final[bool] = os.chmod in os.supports_follow_symlinks


def default_root() -> Path:
    """The flame-mcp directory; the default journal, run and spill directories are below it."""
    return Path.home() / "Library" / "Application Support" / "flame-mcp"


def make_private_dirs(directory: Path) -> None:
    """Make `directory` and every missing parent of it with mode 0700. A directory that already
    exists is not touched. (`Path.mkdir(parents=True, mode=...)` and `os.makedirs(mode=...)`
    apply the mode to the last directory only; the parents get 0777 minus the umask.)"""
    missing: list[Path] = []
    probe = directory
    while not probe.is_dir():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for path in reversed(missing):
        try:
            os.mkdir(path, PRIVATE_DIR_MODE)
        except FileExistsError:
            if path.is_dir():
                continue
            raise
        os.chmod(path, PRIVATE_DIR_MODE)


@dataclass
class HardenReport:
    root: str
    checked: int = 0
    fixed: int = 0
    failed: int = 0
    skipped: int = 0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "checked": self.checked,
            "fixed": self.fixed,
            "failed": self.failed,
            "skipped": self.skipped,
            "truncated": self.truncated,
        }


class _Hardener:
    def __init__(self, root: Path, logger: logging.Logger, max_entries: int, budget_s: float):
        self.report = HardenReport(root=str(root))
        self._log = logger
        self._max_entries = max_entries
        self._deadline = time.monotonic() + budget_s
        self._uid = os.getuid()
        self._changes = 0
        self._device: int | None = None
        self._logged_failures = 0

    def _out_of_budget(self) -> bool:
        if self._changes >= self._max_entries:
            reason = f"{self._max_entries} entries changed"
        elif time.monotonic() > self._deadline:
            reason = "out of time"
        else:
            return False
        if not self.report.truncated:
            self.report.truncated = True
            self._log.warning(
                "permissions: stopped after changing %d entries (%s); the rest is checked at the "
                "next start",
                self._changes,
                reason,
            )
        return True

    def _fail(self, path: str, want: int, exc: OSError) -> None:
        self.report.failed += 1
        if self._logged_failures < MAX_LOGGED_FAILURES:
            self._logged_failures += 1
            self._log.warning("permissions: could not chmod %s to %04o: %s", path, want, exc)

    def _tighten(self, path: str, st: os.stat_result) -> bool:
        """Applies the mode a directory or regular file owned by the user should have. True if
        `path` is a directory this should descend into."""
        if stat.S_ISLNK(st.st_mode) or st.st_uid != self._uid:
            self.report.skipped += 1
            return False
        if stat.S_ISDIR(st.st_mode):
            want = PRIVATE_DIR_MODE
        elif stat.S_ISREG(st.st_mode):
            want = PRIVATE_FILE_MODE
        else:
            return False  # a socket or a pipe: its mode is its own business
        if self._device is None:
            self._device = st.st_dev
        elif st.st_dev != self._device:
            self.report.skipped += 1
            return False
        self.report.checked += 1
        if stat.S_IMODE(st.st_mode) != want:
            self._changes += 1
            try:
                if _CHMOD_NOFOLLOW:
                    os.chmod(path, want, follow_symlinks=False)
                else:
                    os.chmod(path, want)
            except OSError as exc:
                self._fail(path, want, exc)
            else:
                self.report.fixed += 1
        return want == PRIVATE_DIR_MODE

    def enter(self, top: Path, *, recurse: bool) -> bool:
        """Tightens `top`, and with `recurse` everything below it. False if `top` was not a
        directory this could use (missing, a link, not the user's, or left alone)."""
        if self._out_of_budget():
            return False
        try:
            st = os.lstat(top)
        except OSError:
            return False
        descend = self._tighten(str(top), st)
        if descend and recurse:
            self._descend(str(top))
        return descend

    def _descend(self, top: str) -> None:
        stack = [top]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    children = list(entries)
            except OSError as exc:
                self._log.warning("permissions: could not list %s: %s", directory, exc)
                continue
            for child in children:
                if self._out_of_budget():
                    return
                try:
                    st = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if self._tighten(child.path, st) and stat.S_ISDIR(st.st_mode):
                    stack.append(child.path)


def _inside(root: Path, candidate: Path) -> bool:
    root_abs, candidate_abs = os.path.abspath(root), os.path.abspath(candidate)
    return candidate_abs != root_abs and os.path.commonpath([root_abs, candidate_abs]) == root_abs


def _finish(hardener: _Hardener, logger: logging.Logger) -> HardenReport:
    report = hardener.report
    if report.fixed or report.failed:
        logger.info(
            "permissions: fixed %d, failed %d, skipped %d, checked %d under %s",
            report.fixed,
            report.failed,
            report.skipped,
            report.checked,
            report.root,
        )
    return report


def harden_app_dir(
    root: Path,
    *,
    extra_dirs: Sequence[Path] = (),
    max_entries: int = HARDEN_MAX_ENTRIES,
    budget_s: float = HARDEN_BUDGET_S,
    logger: logging.Logger | None = None,
) -> HardenReport:
    """Makes the flame-mcp directory 0700, and the directories in `APP_TREES` (and any of
    `extra_dirs` that lie inside `root`: the directories in use when they have another name)
    0700 with every file in them 0600.

    Only what the user owns is changed. A link is not followed and not changed, a socket or pipe
    is left as it is, nothing outside `root`'s device is entered, and no content is changed. A
    failed chmod is logged and counted, and the walk goes on. The walk stops once `max_entries`
    entries have been changed (what already has the right mode is not counted, so the next start
    gets past what an earlier one fixed) or after `budget_s` seconds, and says so.
    """
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    hardener = _Hardener(root, log, max_entries, budget_s)
    if hardener.enter(root, recurse=False):
        tops = [root / name for name in APP_TREES]
        for extra in extra_dirs:
            if _inside(root, extra) and not any(
                os.path.commonpath([os.path.abspath(t), os.path.abspath(extra)])
                == os.path.abspath(t)
                for t in tops
            ):
                tops.append(extra)
        for top in tops:
            hardener.enter(top, recurse=True)
    return _finish(hardener, log)


def make_private_tree(path: Path, *, logger: logging.Logger | None = None) -> HardenReport:
    """Makes `path` and everything in it 0700 / 0600 by the same rules as `harden_app_dir`."""
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    hardener = _Hardener(path, log, HARDEN_MAX_ENTRIES, HARDEN_BUDGET_S)
    hardener.enter(path, recurse=True)
    return _finish(hardener, log)
