"""Where the work files of `segment.edit_effect_setup` and `node.edit_setup` go:
`<journal dir>/setups/s_<12 hex>`.

The directory is the journal directory the running `Runtime` was made with (`Runtime` calls
`set_journal_directory`), the same value `__main__` checks for ASCII at start. An op's `to_python`
has no runtime at hand, so the value is kept here; code generated without a runtime (a script's
dry run) falls back to the default journal directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from flame_mcp.journal import default_journal_dir

WORK_DIR_NAME: Final[str] = "setups"

_journal_directory: Path | None = None


def set_journal_directory(directory: Path) -> None:
    global _journal_directory
    _journal_directory = directory


def work_root() -> str:
    base = _journal_directory if _journal_directory is not None else default_journal_dir()
    return str(base / WORK_DIR_NAME)
