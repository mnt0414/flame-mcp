"""What a render is going to write, and what it did, seen from the disk.

Pure server-side helpers -- no Flame. A Write File node says where it writes through
`get_resolved_media_path()` (measured on Flame 2025.2.4, in two shapes:
`/var/tmp/writefile1.[000001-000100].exr` with a dot before the frame range,
`.../write_a[000001-000008].exr` without one) and how
much through `range_start` / `range_end` (the frame count of a render is the Write File's range,
default 1-100, whatever the group's duration is). From those this module derives the file
names a render is to write (`OutputSpec`), checks the directory, and compares the directory
before and after a run.

Everything that looks at the filesystem lives here so `render.plan`, `render.batch` and
`journal.check` share one reading of it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

DEFAULT_OUTPUT_DIRS: Final[tuple[str, ...]] = ("/var/tmp", "/tmp")
"""A Write File's media_path out of the box is `/var/tmp/`: a render there writes into
a shared temp directory. `render.batch` refuses it unless `allow_default_path` says so."""
MAX_EXPANDED_FRAMES: Final[int] = 100_000
"""Most frame names one node's spec is expanded into (a range beyond this is refused by
`render.batch` long before: `max_frames` may not exceed it)."""
NAME_LIST_CAP: Final[int] = 200
"""Directory entries the journal's intent lists by name (the count is always exact)."""
OTHER_FILES_CAP: Final[int] = 50
MISSING_CAP: Final[int] = 20

_SEQUENCE_RE: Final = re.compile(
    r"^(?P<dir>.*)/(?P<head>[^/]*)\[(?P<first>\d+)-(?P<last>\d+)\](?P<tail>[^/]*)$"
)
"""`<dir>/<head>[<first>-<last>]<tail>`: the bracket must be in the file name (after the last
`/`), so a `[` in a directory name is never taken for a range. Measured forms:
`writefile1.[000001-000100].exr` (head `writefile1.`, tail `.exr`) and
`write_a[000001-000008].exr` (head `write_a`, tail `.exr`, the files being `write_a000001.exr`)."""

Stat = tuple[int, int]
"""(size, mtime_ns) of a file: what tells a file a render wrote from one that was already there."""


@dataclass(frozen=True)
class OutputSpec:
    """The files one Write File node will write: `head + <number padded to `padding`> + tail`
    for each number in `first`..`last`. `sequence` False: a single file `path` (a resolved path
    with no `[first-last]` range)."""

    directory: str
    sequence: bool
    head: str = ""
    tail: str = ""
    first: int = 0
    last: int = -1
    padding: int = 0
    path: str | None = None

    @property
    def count(self) -> int:
        if not self.sequence:
            return 1
        return max(0, self.last - self.first + 1)

    def name_of(self, number: int) -> str:
        return f"{self.head}{number:0{self.padding}d}{self.tail}"

    def to_json(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "sequence": self.sequence,
            "head": self.head,
            "tail": self.tail,
            "first": self.first,
            "last": self.last,
            "padding": self.padding,
            "path": self.path,
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> OutputSpec:
        """Also reads the first form of a record (`prefix` / `ext`, files named
        `<prefix>.<number>.<ext>`): head = prefix + ".", tail = "." + ext."""
        last = data.get("last")
        if "head" in data or "tail" in data:
            head, tail = str(data.get("head") or ""), str(data.get("tail") or "")
        else:
            prefix, ext = str(data.get("prefix") or ""), str(data.get("ext") or "")
            head, tail = f"{prefix}.", f".{ext}"
        return OutputSpec(
            directory=str(data.get("directory") or ""),
            sequence=bool(data.get("sequence")),
            head=head,
            tail=tail,
            first=int(data.get("first") or 0),
            last=-1 if last is None else int(last),
            padding=int(data.get("padding") or 0),
            path=data.get("path") if isinstance(data.get("path"), str) else None,
        )

    def names(self) -> list[str]:
        """File names (no directory) the run is to write; empty when the count is beyond
        `MAX_EXPANDED_FRAMES` (it would not be verified either way)."""
        if not self.sequence:
            return [os.path.basename(self.path)] if self.path else []
        if self.count > MAX_EXPANDED_FRAMES:
            return []
        return [self.name_of(n) for n in range(self.first, self.last + 1)]

    def paths(self) -> list[str]:
        return [os.path.join(self.directory, name) for name in self.names()]


def parse_resolved_path(resolved: str | None) -> OutputSpec | None:
    """`<dir>/<head>[<first>-<last>]<tail>` -> a sequence spec; any other path -> a single-file
    spec; None for something that is not a path."""
    if not isinstance(resolved, str) or not resolved.strip() or "/" not in resolved:
        return None
    match = _SEQUENCE_RE.match(resolved)
    if match is None:
        return OutputSpec(directory=os.path.dirname(resolved), sequence=False, path=resolved)
    return OutputSpec(
        directory=match["dir"] or "/",
        sequence=True,
        head=match["head"],
        tail=match["tail"],
        first=int(match["first"]),
        last=int(match["last"]),
        padding=len(match["first"]),
    )


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def is_default_output_dir(path: str | None) -> bool:
    """True when `path` is `/var/tmp` or `/tmp` (compared after resolving symlinks: on macOS
    `/tmp` is `/private/tmp`)."""
    if not isinstance(path, str) or not path.strip():
        return False
    real = _real(path)
    return any(real == _real(default) for default in DEFAULT_OUTPUT_DIRS)


def list_directory(directory: str) -> dict[str, Stat]:
    """{file name: (size, mtime_ns)} of the regular files directly inside `directory`; empty
    when it is not a directory (or not readable)."""
    found: dict[str, Stat] = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        st = entry.stat()
                        found[entry.name] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    continue
    except OSError:
        return {}
    return found


def written_since(before: Mapping[str, Stat], after: Mapping[str, Stat]) -> list[str]:
    """Names in `after` that are new or whose (size, mtime) differ from `before`."""
    return sorted(name for name, stat in after.items() if before.get(name) != stat)


def stat_of(path: str) -> Stat | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def spec_frames_present(
    spec: OutputSpec, before: Mapping[str, Stat], now: Mapping[str, Stat]
) -> tuple[int, list[str]]:
    """(how many of the spec's files exist now and were written since `before`, the first
    `MISSING_CAP` names that were not). `before` / `now`: `list_directory` of the spec's
    directory at the two moments."""
    written = 0
    missing: list[str] = []
    for name in spec.names():
        stat = now.get(name)
        if stat is not None and before.get(name) != stat:
            written += 1
        elif len(missing) < MISSING_CAP:
            missing.append(name)
    return written, missing


def existing_planned(spec: OutputSpec, listing: Mapping[str, Stat]) -> dict[str, Stat]:
    """The spec's files that are in `listing` already (a render would overwrite them)."""
    return {name: listing[name] for name in spec.names() if name in listing}


def directory_summary(listing: Mapping[str, Stat]) -> dict[str, Any]:
    names = sorted(listing)
    return {
        "count": len(names),
        "names": names[:NAME_LIST_CAP],
        "truncated": len(names) > NAME_LIST_CAP,
    }
