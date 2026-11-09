"""Compare two saved Batch setups as text, leaving out what changes while no value changed.

`batch.compare_setup` saves the current setup into a work directory and compares it with an
earlier one; this module is that comparison. It needs nothing from Flame, so it runs in the
server and does not hold Flame's main thread.

A saved setup is the `.batch` file and, beside it, a directory of the same stem that holds one
file per node (`.action`, `.timewarp_node`, `.output_node` ...). The stem differs between two
saves, so a file is named by its name inside the directory, and the `.batch` file is called
`.batch`. Only regular files are compared: a symbolic link or anything else in the directory is
neither read nor listed.

Two files with the same bytes are the same. Otherwise both are read as UTF-8 text, the lines that
change without a change of value are left out (the constants below say which and why), and what
is left is compared line by line. A file that is not UTF-8, or that holds a control character other
than tab, line feed and carriage return, stays a comparison of bytes. What is ignored is limited to
what was measured to shake, or what is plainly the state of the screen: ignoring too much hides a
change, while keeping too much only shows a person more lines.

Both setups must exist: a missing `.batch` file or directory raises `OSError` and is not read as a
setup with no files, which would report every file of the other side as added.
"""

from __future__ import annotations

import difflib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

BATCH_FILE: Final[str] = ".batch"
"""How the result names the `.batch` file, whatever its stem."""

IGNORED_FILES: Final[frozenset[str]] = frozenset({"MV.json"})
"""Files that are not compared. `MV.json` is the layout of the views (pan, zoom ...): it differed
in 18 of 58 pairs of saves of a Batch whose fingerprint over every section was the same."""

IGNORED_LINE_KEYS: Final[tuple[str, ...]] = (
    "CreationDate",
    "CurrentFrame",
    "FrontClipUID",
    "MatteClipUID",
    "Uncollapsed",
    "MiniUITabId",
)
"""Keys of the line format (`Key value`, any indent) whose line is dropped. `CreationDate` is the
time of the save (also as the comment `# CreationDate` of a `.lut`), `CurrentFrame` the frame the
Batch was on, `FrontClipUID` / `MatteClipUID` ids that only live in memory, and `Uncollapsed` /
`MiniUITabId` whether a section of a panel was open and which tab. `Uncollapsed` was 37 lines and
`MiniUITabId` 2 lines in the measured differences."""

IGNORED_XML_TAGS: Final[tuple[str, ...]] = (
    "CreationDate",
    "Current_Time",
    "Expanded",
    "ScrollBar",
    "CUIDsec",
    "CUIDusec",
    "ClipUIDPath",
)
"""Tags of the XML format, `<TAG>value</TAG>`, that are dropped: the time of the save, the frame
the node was on, whether a section was open, the scroll position of a panel, and the two halves of
an id that is made again on every load (`CUIDsec` and `CUIDusec`: 20 lines), and the path of a
Clip node's clip (`ClipUIDPath`), whose `H_..._S_..._U_...` ids are made again on every load: empty
in a checkpoint's save, filled after a load, and different between two loads. A Clip node switched
to another clip of the same name shows only there, so that switch is not found."""

IGNORED_XML_EMPTY_TAGS: Final[tuple[str, ...]] = ("Uncollapsed", "ClipUIDPath")
"""Tags of the XML format that are dropped when they are empty, written `<TAG/>` or `<TAG></TAG>`
(white space inside is allowed): `<Uncollapsed/>`, whether a section was open, and an empty
`<ClipUIDPath/>`."""

BATCH_IGNORED_TAGS: Final[tuple[str, ...]] = (
    "BatchCurrent",
    "BatchDate",
    "PosX",
    "PosY",
    "Collapsed",
    "IconWidth",
)
"""Tags dropped in the `.batch` file only: the frame the Batch was on, the date of the save (changed
in 21 pairs), the position of a node in the schematic and whether its icon is collapsed and how wide
it is. A position is the layout, not a value; the same tags inside another file are kept, because
there they cannot be told from a value."""

BATCH_IGNORED_ATTRIBUTE_TAGS: Final[tuple[str, ...]] = ("NotePosition",)
"""Tags written `<TAG attributes/>` and dropped in the `.batch` file only: the position of a note
in the schematic."""

NOISE_FLAG_BITS: Final[tuple[int, ...]] = (6, 7, 18, 19, 24, 26)
"""Bits of `<Flags>n</Flags>` of the `.batch` file that change without a change of value (13
lines). `Flags` is not dropped as a whole, because a bypass may live in it: the 861 nodes measured
had no bypass and showed only these six bits, so they are cleared and any other bit that differs is
a change."""

MAX_DIFF_LINES: Final[int] = 20_000
"""A file is compared, but its lines are not listed (`tooLargeToList`), when the middle of `a` or of
`b` (the lines left once the common head and tail are removed) is longer than this. The cost of
`SequenceMatcher` is what limits it, not the size of the file: it is close to quadratic on lines
that repeat, and on a synthetic Action 14,000 lines took 1.9 seconds, 35,000 lines 11.5 seconds
and 70,000 lines 46 seconds. The largest real Action at hand has 11,598 lines."""

MAX_LINE_CHARS: Final[int] = 200
"""Characters of a line kept in a hunk."""

MAX_HUNK_LINES: Final[int] = 20
"""Lines of `before` and of `after` kept in one hunk, each cut on its own; a hunk that was cut says
`truncated`, and `lineCount` of the file still counts every line."""

MAX_HUNKS_PER_FILE: Final[int] = 20
"""Hunks listed for one file. The rest are counted in `lineCount` and `truncated` says so."""

MAX_LISTED_FILES: Final[int] = 50
"""Files listed in the result. `changedFiles` counts them all and `truncated` says so."""

_NOISE_FLAG_MASK: Final[int] = sum(1 << bit for bit in NOISE_FLAG_BITS)

_LINE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"#?\s*(?:" + "|".join(IGNORED_LINE_KEYS) + r")(?:\s|$)"
)
_VALUE_LINE: Final[re.Pattern[str]] = re.compile(r"Value\s")
_SIZE_LINE: Final[re.Pattern[str]] = re.compile(r"Size [1-9]")
_XML_DROP: Final[re.Pattern[str]] = re.compile(
    "|".join(
        [f"<{tag}>[^<]*</{tag}>" for tag in IGNORED_XML_TAGS]
        + [rf"<{tag}\s*/>|<{tag}>\s*</{tag}>" for tag in IGNORED_XML_EMPTY_TAGS]
    )
)
_XML_LEADING_VALUE: Final[re.Pattern[str]] = re.compile(r"<Value>[^<]*</Value>(?=\s*<Size>[1-9])")
_BATCH_DROP: Final[re.Pattern[str]] = re.compile(
    "|".join(
        [f"<{tag}>[^<]*</{tag}>" for tag in BATCH_IGNORED_TAGS]
        + [rf"<{tag}\b[^>]*/>" for tag in BATCH_IGNORED_ATTRIBUTE_TAGS]
    )
)
_XML_EMPTY: Final[re.Pattern[str]] = re.compile(r"<(\w+)((?:\s[^<>]*?)?)>\s*</\1>")
"""An element with nothing but white space inside, written `<X></X>`: the same as `<X/>`, which is
how the save after a load writes it (`<Channel Name=...></Channel>` of a resize node)."""
_FLAGS: Final[re.Pattern[str]] = re.compile(r"<Flags>(\d+)</Flags>")
_NODE_LINE: Final[re.Pattern[str]] = re.compile(r"Name\s+(\S.*)")
_NODE_XML: Final[re.Pattern[str]] = re.compile(r"<(NAME|Name)>([^<]*)</\1>")
_CHANNEL_LINE: Final[re.Pattern[str]] = re.compile(r"Channel(?:\s+(.*))?")
_CHANNEL_XML: Final[re.Pattern[str]] = re.compile(r'<Channel Name="([^"]*)"')
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clear_noise_bits(found: re.Match[str]) -> str:
    return f"<Flags>{int(found.group(1)) & ~_NOISE_FLAG_MASK}</Flags>"


def normalize(name: str, text: str) -> list[str]:
    """The lines of `text` (the file called `name`) that stand for its values: each stripped, the
    blank ones and the ones that change without a change of value left out. A line that starts with
    `<` is split between its tags, so that a hunk names the tag and not the whole line."""
    text = _XML_DROP.sub("", text)
    text = _XML_LEADING_VALUE.sub("", text)
    if name == BATCH_FILE:
        text = _BATCH_DROP.sub("", text)
        text = _FLAGS.sub(_clear_noise_bits, text)
    text = _XML_EMPTY.sub(r"<\1\2/>", text)
    raw = [line.strip() for line in text.splitlines()]
    lines: list[str] = []
    for index, line in enumerate(raw):
        if not line or _LINE_KEY_PATTERN.match(line):
            continue
        if _VALUE_LINE.match(line) and index + 1 < len(raw) and _SIZE_LINE.match(raw[index + 1]):
            continue
        lines.append(line)
    split: list[str] = []
    for line in lines:
        if line.startswith("<"):
            split.extend(line.replace("><", ">\n<").split("\n"))
        else:
            split.append(line)
    return split


def _nearest(
    lines: list[str], end: int, find: Callable[[str], str | None], floor: int = 0
) -> tuple[int, str | None]:
    """(index, value) of the nearest line before `end` and not before `floor` that `find` reads;
    (-1, None) when there is none. An empty value is None."""
    for index in range(end - 1, floor - 1, -1):
        found = find(lines[index])
        if found is not None:
            return index, found or None
    return -1, None


def _node_of(line: str) -> str | None:
    found = _NODE_LINE.fullmatch(line)
    if found:
        return found.group(1)
    found = _NODE_XML.search(line)
    return found.group(2) if found else None


def _channel_of(line: str) -> str | None:
    found = _CHANNEL_LINE.fullmatch(line)
    if found:
        return found.group(1) or ""
    found = _CHANNEL_XML.search(line)
    return found.group(1) if found else None


def _cut(lines: list[str]) -> list[str]:
    return [line[:MAX_LINE_CHARS] for line in lines[:MAX_HUNK_LINES]]


def _common_ends(a: list[str], b: list[str]) -> tuple[int, int]:
    """(head, tail): how many lines `a` and `b` share at the start, and then at the end."""
    limit = min(len(a), len(b))
    head = 0
    while head < limit and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < limit - head and a[len(a) - 1 - tail] == b[len(b) - 1 - tail]:
        tail += 1
    return head, tail


def _opcodes(
    a: list[str], b: list[str], head: int, tail: int
) -> list[tuple[str, int, int, int, int]]:
    """The opcodes of `a` against `b` that are not `equal`, with the common head and tail left to
    the caller, so that SequenceMatcher only sees the part that differs."""
    matcher = difflib.SequenceMatcher(
        None, a[head : len(a) - tail], b[head : len(b) - tail], autojunk=False
    )
    return [
        (tag, i1 + head, i2 + head, j1 + head, j2 + head)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _where(a: list[str], index: int) -> dict[str, str | None]:
    """The node and the channel for a change at line `index` of `a`: the nearest `Name` before it,
    and the nearest `Channel` after that node's line (a channel of an earlier node is not one of
    this node)."""
    node_index, node = _nearest(a, index, _node_of)
    _channel_index, channel = _nearest(a, index, _channel_of, node_index + 1)
    return {"node": node, "channel": channel}


def _named_inside(lines: list[str]) -> str | None:
    """The `<Name>` / `<NAME>` of a node that the lines themselves hold, else None. Only the XML
    form is read; a `Name` line of the line format is not."""
    for line in lines:
        found = _NODE_XML.fullmatch(line)
        if found and found.group(2):
            return found.group(2)
    return None


def _hunk_where(
    a: list[str], b: list[str], opcode: tuple[str, int, int, int, int]
) -> dict[str, str | None]:
    """`_where`, except that a hunk which holds a `<Name>` (a node added or removed as a whole)
    names that node and the first channel inside the hunk, not the node before it."""
    _tag, i1, i2, j1, j2 = opcode
    inside = a[i1:i2] + b[j1:j2]
    node = _named_inside(inside)
    if node is None:
        return _where(a, i1)
    channel = next((c for c in map(_channel_of, inside) if c is not None), None)
    return {"node": node, "channel": channel}


def _hunk(a: list[str], b: list[str], opcode: tuple[str, int, int, int, int]) -> dict[str, Any]:
    _tag, i1, i2, j1, j2 = opcode
    hunk: dict[str, Any] = {
        "where": _hunk_where(a, b, opcode),
        "before": _cut(a[i1:i2]),
        "after": _cut(b[j1:j2]),
    }
    if i2 - i1 > MAX_HUNK_LINES or j2 - j1 > MAX_HUNK_LINES:
        hunk["truncated"] = True
    return hunk


def _hunks(
    a: list[str], b: list[str], head: int, tail: int
) -> tuple[list[dict[str, Any]], int, bool]:
    """(the hunks that are listed, the number of lines that differ in all of them, whether some
    hunks or some of their lines were left out)."""
    opcodes = _opcodes(a, b, head, tail)
    line_count = sum((i2 - i1) + (j2 - j1) for _tag, i1, i2, j1, j2 in opcodes)
    hunks = [_hunk(a, b, opcode) for opcode in opcodes[:MAX_HUNKS_PER_FILE]]
    cut = len(opcodes) > MAX_HUNKS_PER_FILE or any("truncated" in hunk for hunk in hunks)
    return hunks, line_count, cut


def _text_of(data: bytes) -> str | None:
    """`data` as text, or None when it is not UTF-8 or holds a control character other than tab,
    line feed and carriage return."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if _CONTROL_CHARS.search(text) else text


def _compare_file(
    name: str, before: Path | None, after: Path | None
) -> tuple[dict[str, Any] | None, bool]:
    """(the entry of the file, or None when it is the same; whether hunks were left out)."""
    if before is None:
        return {"file": name, "status": "added"}, False
    if after is None:
        return {"file": name, "status": "removed"}, False
    data_before, data_after = before.read_bytes(), after.read_bytes()
    if data_before == data_after:
        return None, False
    text_before, text_after = _text_of(data_before), _text_of(data_after)
    if text_before is None or text_after is None:
        return {"file": name, "status": "changed", "notText": True}, False
    a, b = normalize(name, text_before), normalize(name, text_after)
    if a == b:
        return None, False
    head, tail = _common_ends(a, b)
    if max(len(a), len(b)) - head - tail > MAX_DIFF_LINES:
        return {"file": name, "status": "changed", "tooLargeToList": True}, False
    hunks, line_count, cut = _hunks(a, b, head, tail)
    entry = {"file": name, "status": "changed", "lineCount": line_count, "hunks": hunks}
    return entry, cut


def _files_of(batch: Path) -> dict[str, Path]:
    """{name: path} of the setup whose `.batch` file is `batch`: the file itself and the regular
    files of the directory with the same stem. A directory that is not there raises `OSError`;
    so does a `.batch` file that is not there, when it is read."""
    found: dict[str, Path] = {BATCH_FILE: batch}
    directory = batch.parent / batch.name.removesuffix(BATCH_FILE)
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name in IGNORED_FILES or entry.name == BATCH_FILE:
                continue
            if stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                found[entry.name] = Path(entry.path)
    return found


def compare_setups(baseline: Path, current: Path) -> dict[str, Any]:
    """Compare the setup whose `.batch` file is `baseline` with the one at `current`. The result is
    `{changed, filesCompared, changedFiles, files, truncated}`; `files` holds the files that
    differ, the `.batch` first and the others by name: `{file, status}` with `status` `changed`,
    `added` or `removed`, and for a changed file `lineCount` and `hunks`
    (`{where: {node, channel}, before, after}` and `truncated` when `before` or `after` was cut to
    `MAX_HUNK_LINES`), or `tooLargeToList`, or `notText`. It raises `OSError` when the `.batch` file
    or the directory of a setup cannot be read."""
    before, after = _files_of(baseline), _files_of(current)
    names = sorted(set(before) | set(after), key=lambda name: (name != BATCH_FILE, name))
    differing: list[dict[str, Any]] = []
    truncated = False
    for name in names:
        entry, cut = _compare_file(name, before.get(name), after.get(name))
        if entry is not None:
            differing.append(entry)
        truncated = truncated or cut
    return {
        "changed": bool(differing),
        "filesCompared": len(names),
        "changedFiles": len(differing),
        "files": differing[:MAX_LISTED_FILES],
        "truncated": truncated or len(differing) > MAX_LISTED_FILES,
    }
