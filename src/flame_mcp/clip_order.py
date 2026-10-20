"""Putting the clips of a restored reel back in their recorded order.

A setup file (`.batch`) holds no reels. `load_setup` rebuilds each schematic reel from the Clip
nodes: `<Base>/<Name>.clip_node` names the clip (`ClipName`) and the reel (`ReelName`), and the
clips go into the reel in the order the Clip `<Node>` elements are written in the `.batch` (not
by `<Number>`, not by whether the group is the current one; measured on Flame 2025.2.4). A
checkpoint whose Clip nodes are written in another order than the reel held its clips therefore
comes back with the clips reordered.

A restore of a checkpoint that records its reel inventory (`reel_fidelity.ReelRecord`) first asks
`prepare` for a fix. Per reel, when the multiset of `ClipName`s equals the recorded clip names, the
reel's Clip nodes are assigned to its recorded order (same-named clips keep their relative order)
and written back into the positions that reel's Clip nodes occupied; every other byte of the
`.batch` stays as it is. A reel that does not match is left as written, with the reason. When
nothing needs to change, the checkpoint is loaded as it is; otherwise a copy is written (the
`.batch` and a copy of its `<base>/` directory, in a directory of its own below `work_root`) and
removed by the caller after the load (`remove_copy`). The checkpoint itself is never written. A
`.batch` that mentions its own name is not touched (the copy keeps the name, but nothing measured
says what such a reference means).

`plan_order` is pure (text and records in, new text and the reasons out); `read_clip_node_texts`
and `write_copy` / `remove_copy` do the file work; `prepare` ties them together.
"""

from __future__ import annotations

import html
import re
import shutil
import tempfile
import time
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

ORDERED_DIR_NAME: Final[str] = "ordered"
"""The directory below the checkpoint directory that holds the copies."""
COPY_PREFIX: Final[str] = "o_"
SWEEP_AGE_S: Final[float] = 24 * 3600.0
"""A working copy older than this is a leftover (a load that ended without an answer keeps its
copy) and is removed by the next `prepare`."""
CLIP_NODE_SUFFIX: Final[str] = ".clip_node"
MAX_LISTED_NAMES: Final[int] = 8

CLIP_ORDER_NOTE: Final[str] = (
    "Clip order: when the checkpoint recorded its reels, the clips of a reel come back in the "
    "recorded order: the load reads a copy of the checkpoint whose Clip nodes are written in "
    "that order (the checkpoint file is not changed; the copy is removed afterwards). A reel "
    "whose clip names differ from the record is loaded as the file has it. result.clipOrder: "
    "needed, reordered, skipped [{reel, reason}], loadedFrom, loaded, error (a copy that could "
    "not be made is reported in error and the checkpoint is loaded as it is; loaded says "
    "whether Flame was asked to load at all); a reel left as written is also named in "
    "result.warnings. If the load ends without an answer the copy is kept (copyKept, copyPath, "
    "copyKeptReason)."
)
"""The description of what a checkpoint restore does about the clip order of the reels."""

_NODE_RE: Final = re.compile(r"<Node>.*?</Node>", re.DOTALL)


@dataclass(frozen=True)
class SkippedReel:
    """Why a reel (or, with `reel` None, the whole file) was left as written."""

    reel: str | None
    reason: str


@dataclass(frozen=True)
class OrderPlan:
    needed: bool
    new_text: str | None
    """The `.batch` text with the Clip nodes in the new order; None when nothing changes."""
    node_order: tuple[str, ...]
    """The names of all Clip nodes, in the order they are written in `new_text`."""
    reordered: tuple[str, ...]
    skipped: tuple[SkippedReel, ...]
    unreadable: tuple[str, ...]
    """Clip nodes whose `.clip_node` file gave no ClipName / ReelName."""


@dataclass(frozen=True)
class OrderedCopy:
    root: Path
    directory: Path
    """Created by `write_copy`, below `root`; the only thing `remove_copy` deletes."""
    batch_path: Path


@dataclass(frozen=True)
class Swept:
    """What `sweep_old` did: the directories it removed and the ones it could not."""

    removed: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Preparation:
    plan: OrderPlan
    copy: OrderedCopy | None
    swept: Swept = Swept()


@dataclass(frozen=True)
class _ClipInfo:
    index: int
    node_name: str
    clip_name: str
    reel_name: str


def _tag(chunk: str, name: str) -> str | None:
    match = re.search(rf"<{name}>(.*?)</{name}>", chunk, re.DOTALL)
    return html.unescape(match.group(1)) if match else None


def _spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _NODE_RE.finditer(text)]


def _brief(names: Sequence[str]) -> str:
    shown = ", ".join(repr(n) for n in names[:MAX_LISTED_NAMES])
    more = len(names) - MAX_LISTED_NAMES
    return f"[{shown}{f', ... +{more}' if more > 0 else ''}]"


def _safe_node_name(name: str | None) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in str(name) and "\\" not in str(name)


def clip_node_names(batch_text: str) -> list[str]:
    """The `<Name>` of every Clip node, in the order they are written."""
    names: list[str] = []
    for a, b in _spans(batch_text):
        chunk = batch_text[a:b]
        name = _tag(chunk, "Name")
        if _tag(chunk, "Type") == "Clip" and name is not None:
            names.append(name)
    return names


def read_clip_node_texts(batch_text: str, base_dir: Path) -> dict[str, str]:
    """`<Name>.clip_node` of every Clip node that has a readable one, by node name. Only tags are
    read from these texts (they are never written back), so non-UTF-8 bytes are replaced."""
    texts: dict[str, str] = {}
    for name in clip_node_names(batch_text):
        path = base_dir / f"{name}{CLIP_NODE_SUFFIX}"
        if _safe_node_name(name) and path.is_file():
            texts[name] = path.read_bytes().decode("utf-8", errors="replace")
    return texts


def _recorded_clips(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str] | None]:
    """Recorded clip names by reel name; None for a reel whose names are unknown or whose name is
    in the record twice."""
    out: dict[str, list[str] | None] = {}
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        clips = row.get("clips")
        known = isinstance(clips, list) and all(isinstance(c, str) for c in clips)
        out[name] = None if name in out or not known else list(clips)  # type: ignore[arg-type]
    return out


def _structure_problem(text: str, chunks: Sequence[str]) -> str | None:
    """Why the `<Node>` elements cannot be told apart reliably (nested, unbalanced or with
    attributes), or None. Only `<Node>...</Node>` without attributes was ever seen."""
    odd = (
        any(chunk.count("<Node>") != 1 for chunk in chunks)
        or text.count("<Node>") != len(chunks)
        or text.count("</Node>") != len(chunks)
        or re.search(r"<Node(?=[\s/])", text) is not None
    )
    if not odd:
        return None
    return (
        "the setup's Node elements cannot be told apart reliably (nested, unbalanced or "
        "attributed): the Clip nodes were not reordered"
    )


def _node_order(chunks: Sequence[str]) -> tuple[str, ...]:
    return tuple(_tag(c, "Name") or "" for c in chunks if _tag(c, "Type") == "Clip")


def _collect_clips(
    chunks: Sequence[str], clip_node_texts: Mapping[str, str]
) -> tuple[list[_ClipInfo], list[str]]:
    """The Clip nodes whose `.clip_node` text names a clip and a reel, and the names of those
    that do not."""
    infos: list[_ClipInfo] = []
    unreadable: list[str] = []
    for index, chunk in enumerate(chunks):
        if _tag(chunk, "Type") != "Clip":
            continue
        name = _tag(chunk, "Name") or ""
        body = clip_node_texts.get(name)
        clip_name = _tag(body, "ClipName") if body is not None else None
        reel_name = _tag(body, "ReelName") if body is not None else None
        if clip_name is None or reel_name is None:
            unreadable.append(name)
        else:
            infos.append(_ClipInfo(index, name, clip_name, reel_name))
    return infos, unreadable


def _assign(members: Sequence[_ClipInfo], want: Sequence[str]) -> list[_ClipInfo]:
    """The reel's nodes in the recorded order; nodes with the same clip name keep their
    relative order. The names of `members` and `want` must be the same multiset."""
    queues: dict[str, deque[_ClipInfo]] = {}
    for member in members:
        queues.setdefault(member.clip_name, deque()).append(member)
    return [queues[name].popleft() for name in want]


def _skip_reason(have: Sequence[str], want: list[str] | None, recorded: bool) -> str | None:
    if not recorded:
        return "the reel is not in the checkpoint's reel record"
    if want is None:
        return "the record has no usable clip names for the reel"
    if Counter(have) != Counter(want):
        return f"the clip names in the file {_brief(have)} differ from the record {_brief(want)}"
    return None


def _order_reels(
    chunks: Sequence[str], infos: Sequence[_ClipInfo], recorded: Mapping[str, list[str] | None]
) -> tuple[list[str], list[str], list[SkippedReel]]:
    """The chunks with each matching reel's Clip nodes put in the recorded order (in the
    positions that reel's Clip nodes had), the reels changed, and the reels left as written."""
    by_reel: dict[str, list[_ClipInfo]] = {}
    for info in infos:
        by_reel.setdefault(info.reel_name, []).append(info)
    out = list(chunks)
    reordered: list[str] = []
    skipped: list[SkippedReel] = []
    for reel, members in by_reel.items():
        want = recorded.get(reel)
        reason = _skip_reason([m.clip_name for m in members], want, reel in recorded)
        if reason is not None:
            skipped.append(SkippedReel(reel, reason))
            continue
        assigned = _assign(members, want or [])
        if [a.index for a in assigned] == [m.index for m in members]:
            continue
        for slot, member in zip(members, assigned, strict=True):
            out[slot.index] = chunks[member.index]
        reordered.append(reel)
    return out, reordered, skipped


def _assemble(text: str, spans: Sequence[tuple[int, int]], chunks: Sequence[str]) -> str:
    parts: list[str] = []
    cursor = 0
    for (a, b), chunk in zip(spans, chunks, strict=True):
        parts.append(text[cursor:a])
        parts.append(chunk)
        cursor = b
    parts.append(text[cursor:])
    return "".join(parts)


def plan_order(
    batch_text: str,
    clip_node_texts: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
    base: str,
) -> OrderPlan:
    """The plan for putting the Clip nodes of `batch_text` in the order `rows` (a checkpoint's
    reel inventory) records. `clip_node_texts` is `read_clip_node_texts`; `base` the checkpoint's
    file stem."""
    spans = _spans(batch_text)
    chunks = [batch_text[a:b] for a, b in spans]
    infos, unreadable = _collect_clips(chunks, clip_node_texts)
    blocked = _structure_problem(batch_text, chunks)
    if blocked is None and base and base in batch_text:
        blocked = (
            f"the .batch file contains a reference to its own name {base!r}: not touched, "
            "a copy could break it"
        )
    if blocked is not None:
        return OrderPlan(
            False,
            None,
            _node_order(chunks),
            (),
            (SkippedReel(None, blocked),),
            tuple(unreadable),
        )
    out, reordered, skipped = _order_reels(chunks, infos, _recorded_clips(rows))
    if unreadable:
        skipped.append(
            SkippedReel(
                None,
                f"{len(unreadable)} Clip node(s) have no readable ClipName / ReelName "
                f"and were left where they are: {_brief(unreadable)}",
            )
        )
    return OrderPlan(
        bool(reordered),
        _assemble(batch_text, spans, out) if reordered else None,
        _node_order(out),
        tuple(reordered),
        tuple(skipped),
        tuple(unreadable),
    )


def write_copy(new_text: str, source: Path, work_root: Path) -> OrderedCopy:
    """Write `new_text` as `<source name>` and copy `<source stem>/` beside it, in a new
    directory below `work_root`. Nothing is left behind if it fails."""
    work_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=COPY_PREFIX, dir=work_root))
    try:
        source_dir = source.parent / source.stem
        if source_dir.is_dir():
            shutil.copytree(source_dir, directory / source.stem, symlinks=True)
        batch_path = directory / source.name
        batch_path.write_bytes(new_text.encode("utf-8"))
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return OrderedCopy(work_root, directory, batch_path)


def remove_copy(copy: OrderedCopy | None) -> None:
    """Delete the directory `write_copy` made, and nothing else. A copy already gone is fine."""
    if copy is None:
        return
    if copy.directory.parent != copy.root or not copy.directory.name.startswith(COPY_PREFIX):
        raise ValueError(f"not a directory made by write_copy: {copy.directory}")
    if copy.directory.exists():
        shutil.rmtree(copy.directory)


def sweep_old(
    work_root: Path, *, now: float | None = None, max_age_s: float = SWEEP_AGE_S
) -> Swept:
    """Remove the directories directly below `work_root` that are named like a working copy
    (`COPY_PREFIX`...), are not links, and were last modified more than `max_age_s` ago.
    Nothing else is touched. A directory that cannot be removed is reported and left."""
    if not work_root.is_dir():
        return Swept()
    cutoff = (time.time() if now is None else now) - max_age_s
    removed: list[str] = []
    errors: list[str] = []
    for entry in sorted(work_root.iterdir()):
        if not entry.name.startswith(COPY_PREFIX) or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry)
            removed.append(str(entry))
        except OSError as exc:
            errors.append(f"{entry}: {type(exc).__name__}: {exc}")
    return Swept(tuple(removed), tuple(errors))


def prepare(
    checkpoint_path: str, rows: Sequence[Mapping[str, Any]], work_root: Path
) -> Preparation:
    """Sweep old working copies, then plan the order of the checkpoint's Clip nodes and, if it
    has to change, write the copy. Reads the checkpoint, never writes it. Raises on a file that
    cannot be read or written."""
    swept = sweep_old(work_root)
    source = Path(checkpoint_path)
    text = source.read_bytes().decode("utf-8")
    texts = read_clip_node_texts(text, source.parent / source.stem)
    plan = plan_order(text, texts, rows, source.stem)
    if not plan.needed or plan.new_text is None:
        return Preparation(plan, None, swept)
    return Preparation(plan, write_copy(plan.new_text, source, work_root), swept)


def report(prep: Preparation | None, checkpoint_path: str, error: str | None) -> dict[str, Any]:
    """The `clipOrder` entry of a restore's result. `prep` is None when `prepare` raised (`error`
    says how; the checkpoint is then loaded as it is)."""
    if prep is None:
        return {
            "needed": None,
            "reordered": [],
            "skipped": [],
            "loadedFrom": checkpoint_path,
            "error": error,
        }
    return {
        "needed": prep.plan.needed,
        "reordered": list(prep.plan.reordered),
        "skipped": [{"reel": s.reel, "reason": s.reason} for s in prep.plan.skipped],
        "loadedFrom": str(prep.copy.batch_path) if prep.copy is not None else checkpoint_path,
        "error": error,
        "sweptOld": list(prep.swept.removed),
        **({"sweepErrors": list(prep.swept.errors)} if prep.swept.errors else {}),
    }


def warnings_of(prep: Preparation) -> list[str]:
    """One line per reel (or whole-file reason) the order could not be put back for."""
    return [
        (f"clip order not restored for reel {s.reel!r}: " if s.reel else "clip order: ") + s.reason
        for s in prep.plan.skipped
    ]


def report_not_run(checkpoint_path: str, reason: str | None) -> dict[str, Any]:
    """The `clipOrder` entry when there was no reel record to order by."""
    return {
        "needed": False,
        "reordered": [],
        "skipped": [{"reel": None, "reason": reason}],
        "loadedFrom": checkpoint_path,
        "error": None,
    }
