"""Write-ahead journal for the value-undo writes.

Never touches Flame: this module is a JSONL file per day plus the pure functions that
turn its records into states. Records (one JSON object per line, append-only):

  intent      {type, id, group, ts, op, args, target, targetAfter, readOp, before, after,
               reverts?}              written and flushed before the write is sent
  outcome     {type, id, group, ts, ok, applied, failed, errorCode, readBack}
                                     written after the write's answer is in
  unresolved  {type, id, group, ts, errorCode, error}
                                     the answer never came (TIMEOUT / BRIDGE_LOST /
                                     BRIDGE_UNAVAILABLE): informational, not an outcome
  pending     {type, id, group, ts, handle, timeoutS}
                                     a `render.batch` whose wait ended with the bridge's pending
                                     handle: the run may still be going; `journal.check`
                                     asks the bridge for its result (`poll`) with this handle
  polled      {type, id, group, ts, handle, ok, errorCode, error, flame, queuedMs, runMs}
                                     what the bridge's `poll` answered when the run had finished:
                                     the bridge hands a result over once (it deletes it), so this
                                     record is the only copy; `flame` is the op's own value
                                     (returned, elapsedS, newClips, reelsBefore / reelsAfter)
  check       {type, id, group, ts, verdict, perKey, current, target}
                                     what `journal.check` found by reading Flame again
  group       {type, group, ts, kind, label, steps, onError}
                                     written once when a `plan.run` starts: the
                                     group's label and the ops it is about to run
  checkpoint  {type, id, group, ts, batch: {uid, name}, label, path, size, fingerprint,
               nodeCount}            a Batch setup saved by `batch.checkpoint`: `path` is
                                     the file `save_setup` really wrote, `fingerprint` the group's
                                     node / wiring hash at that moment. `group` is the journal
                                     group the checkpoint was taken for (an eval run, a restore),
                                     None for a manual one.

A "checkpoint intent" (an `eval.run` destructive run, a `journal.restore_checkpoint`, a
`batch.load_setup`, and every op that declares a `CheckpointSpec` -- `checkpointed.py`) is
an ordinary intent / outcome pair whose intent names `revertCheckpoint` (the checkpoint whose
restoring puts the group back), `fingerprintBefore` and, for a restore, `fingerprintExpected`;
its outcome carries `fingerprintAfter` (and `changed`: what the fingerprint saw). Such an intent
has no `before` / `after` keys. A checkpoint record also carries `fingerprintSections`, the
optional fingerprint parts it was taken with (fingerprints of different sections do not
compare equal).

A "render intent" (`render.batch`) is an intent with `kind: "render"` that lists the
Write File nodes' planned files (`nodes[].spec`) and the files that were already there
(`nodes[].preexisting`); its outcome carries `verdict` (landed / partial / not_landed, from the
files found), `expectedFrames` and `writtenFrames`. Nothing brings a render back: it has no
`revertCheckpoint` and no `before`, so it is never revertible. `journal.check` decides an open one
(`render_check.check_render_intent`): when the run ended with a pending handle the bridge is
asked for its result first (the end of the render is known from that, `basis: "handle"`) and
the output files are then counted as the check of what it wrote; without a handle (or one the
bridge no longer knows) the files alone decide (`basis: "disk"`: an estimate, the end is not
confirmed). Every planned frame written since the intent = `landed`, some = `partial`, none =
`not_landed` (`failed` when the render itself reported a failure and did not write everything).
`partial` is therefore both a state an outcome can carry and a verdict a check can reach.

A "desktop intent" (`desktop.create` / `desktop.copy`) is an intent with `kind:
"desktop_create"` / `"desktop_copy"`; it has no `before` / `after` and no `revertCheckpoint`. It
lists `existing` (the uids of what the parent / destination held before) and, for a create, the
parent in `target` and the current Batch group in `previousCurrent`. Its outcome carries `created`:
what the call made ({kind, uid, name, wiretapId, selector, parent, state} for a create,
{destination, copies: [{uid, name, type, wiretapId}]} for a copy). `journal.revert` deletes what
`created` names -- a create only while what it made is still as it was left (`state`) -- as a new
group whose intent has `kind: "desktop_revert"` (it is not itself revertible). `journal.check`
decides an open one by reading the parent / destination again
(`desktop_revert.check_desktop_intent`): exactly one new entry (a create) or as many new clips as
were copied = `landed`, and the check record then carries the `created` it found.

`mediahub.import` / `desktop.duplicate` / `desktop.move` journal the same way, with kinds
`desktop_import` / `desktop_duplicate` / `desktop_move`. Their outcome's `created` is
`{destination, imports: [...]}`, `{uid, name, type, parent, state, ...}` and `{destination, moves:
[{from, to}]}`; an import's revert deletes the clips by uid, a duplicate's only while it is still as
made, a move's puts the things back into their old parents (all or nothing, new uids again).
`journal.check` of an open one reads the destination / parent again (`desktop_media_run`).

`desktop.delete` journals the same way with kind `desktop_delete`. Before the write each
thing is copied into the stash reel of the checkpoint library and recorded as a timeline
`checkpoint` (the copy's uid, the source's parent and name); the intent lists them (`stashes`), the
outcome's `created` is `{deletes: [{from: {parent, name, type, uid}, checkpoint, stash, fingerprint,
...}]}`. Its revert copies each stash back into the original parent (new uid, end of the reel); a
parent that is gone leaves that one as a conflict and restores the others (`partial`).
`journal.check` of an open one looks the old uids up: all gone = landed, all there = not landed.

`desktop.reformat` writes one intent per clip into one group, kind `desktop_reformat`, `mode`
`in_place` or `duplicate`, with the clip's format before (`before`) and the format asked for
(`requested`). Its outcome's `created` is `{mode: "in_place", checkpoint, stash, uid, before, after,
fingerprintAfter, ...}` (revert: the stashed copy is restored) or `{mode: "duplicate", uid, name,
type, parent, state, ...}` (revert: the duplicate is deleted while it is as left). `journal.check`
of an open one reads the clip's format again (`desktop_reformat_check`).

`segment.copy_to_media_panel` / `segment.match` journal the same way with kind
`desktop_extract` (`op` says which). Their outcome's `created` is `{uid, name, type, wiretapId,
selector, parent, state: {name, duration, fingerprint}, sourceUid}`; the revert deletes the clip by
uid, only while its name, duration and fingerprint are still the recorded ones.
`journal.check` of an open one reads the destination again (`segment_extract_run`).

`purge` {type, id, ts, olderThanDays, library, purged: [checkpoint ids], gone: [checkpoint ids],
skipped, reelsDeleted} is written by `journal.purge_checkpoints` after it deleted stashed copies
(`purged`) or found them already gone (`gone`); `Index.purged` maps those checkpoint ids to the
record. A purged checkpoint cannot be restored. `cleanup_old` keeps a journal file that holds a
timeline checkpoint no `purge` record covers: dropping the record while the stash is still in the
library would leave the stash with nothing that says what it is (reported as `unknown`).

`id` is the intent's id; `group` is one `flame_do` call or one `plan.run`; a revert is a new
group whose intents carry `reverts: <original group>` (and `revertsIntent: <intent id>`, the one
intent each of them puts back).

Durability: every line is flushed (not fsynced) -- a dead server process leaves
the page cache intact; a power cut may lose the tail, which is accepted. A reader drops
every line that does not parse (a torn tail line included).

Invariant: a journal is a record of what was attempted, not evidence of what the
project contains. Everything here that decides something from it (`journal.check`,
`journal.revert`) reads Flame again first.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Final

from flame_mcp.attribute_prelude import CASE_FOLDED_ATTRIBUTES

JOURNAL_DIR_ENV: Final[str] = "FLAME_MCP_JOURNAL_DIR"
JOURNAL_MAX_AGE_S: Final[float] = 30 * 24 * 60 * 60
JOURNAL_FILE_MODE: Final[int] = 0o600
JOURNAL_DIR_MODE: Final[int] = 0o700
_FILE_PREFIX: Final[str] = "journal_"
_FILE_SUFFIX: Final[str] = ".jsonl"
_NUMBER_TOLERANCE: Final[float] = 1e-6  # same tolerance as the in-Flame `_attr_equal`

Record = dict[str, Any]

# What an intent (or a group of them) can be, from its outcome / check / reverts.
STATE_LANDED: Final[str] = "landed"
STATE_NOT_LANDED: Final[str] = "not_landed"
STATE_FAILED: Final[str] = "failed"
STATE_PARTIAL: Final[str] = "partial"
STATE_CONFLICT: Final[str] = "conflict"
STATE_UNKNOWN: Final[str] = "unknown"
STATE_DIVERGED: Final[str] = "diverged"
STATE_REVERTED: Final[str] = "reverted"
STATE_PARTIALLY_REVERTED: Final[str] = "partially_reverted"

VERDICT_UNKNOWN: Final[str] = "unknown"

KIND_DESKTOP_CREATE: Final[str] = "desktop_create"
KIND_DESKTOP_COPY: Final[str] = "desktop_copy"
KIND_DESKTOP_IMPORT: Final[str] = "desktop_import"
KIND_DESKTOP_DUPLICATE: Final[str] = "desktop_duplicate"
KIND_DESKTOP_MOVE: Final[str] = "desktop_move"
KIND_DESKTOP_DELETE: Final[str] = "desktop_delete"
KIND_DESKTOP_EXTRACT: Final[str] = "desktop_extract"
KIND_DESKTOP_SEQUENCE: Final[str] = "desktop_sequence"
KIND_DESKTOP_REFORMAT: Final[str] = "desktop_reformat"
KIND_DESKTOP_REVERT: Final[str] = "desktop_revert"
MEDIA_WRITE_KINDS: Final[frozenset[str]] = frozenset(
    {KIND_DESKTOP_IMPORT, KIND_DESKTOP_DUPLICATE, KIND_DESKTOP_MOVE}
)
"""`mediahub.import`, `desktop.duplicate`, `desktop.move` (`desktop_media_run`)."""
DESKTOP_WRITE_KINDS: Final[frozenset[str]] = (
    frozenset(
        {
            KIND_DESKTOP_CREATE,
            KIND_DESKTOP_COPY,
            KIND_DESKTOP_DELETE,
            KIND_DESKTOP_EXTRACT,
            KIND_DESKTOP_SEQUENCE,
            KIND_DESKTOP_REFORMAT,
        }
    )
    | MEDIA_WRITE_KINDS
)
"""`kind` of an intent that made something on the Desktop or in a reel (a revert deletes it, or
for a move puts it back, or for a delete copies it back from its stash)."""
DESKTOP_KINDS: Final[frozenset[str]] = DESKTOP_WRITE_KINDS | {KIND_DESKTOP_REVERT}

# A group of several intents shows its most worrying member first.
_STATE_PRIORITY: Final[tuple[str, ...]] = (
    STATE_UNKNOWN,
    STATE_DIVERGED,
    STATE_PARTIAL,
    STATE_CONFLICT,
    STATE_FAILED,
    STATE_LANDED,
    STATE_NOT_LANDED,
)


def default_journal_dir() -> Path:
    override = os.environ.get(JOURNAL_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "flame-mcp" / "journal"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def values_equal(a: Any, b: Any) -> bool:
    """JSON-form value equality for the server-side comparisons against what a write
    was meant to leave behind (numbers within `_NUMBER_TOLERANCE`, bool never equal to
    a number, lists element-wise)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= _NUMBER_TOLERANCE
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(values_equal(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def attribute_values_equal(key: str, a: Any, b: Any) -> bool:
    """`values_equal` for attribute `key`: a colour label Flame stored in its colour coding
    table's spelling equals the one that was written (the rule of the in-Flame read-back)."""
    if key in CASE_FOLDED_ATTRIBUTES and isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return values_equal(a, b)


@dataclass(frozen=True)
class ReadResult:
    records: list[Record]
    skipped_lines: int


class Journal:
    """Append-only JSONL files `journal_YYYYMMDD.jsonl` (local date) under `directory`."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory if directory is not None else default_journal_dir()
        self._lock = threading.Lock()
        self._fh: IO[str] | None = None
        self._fh_path: Path | None = None

    @property
    def directory(self) -> Path:
        return self._dir

    def _today_path(self) -> Path:
        return self._dir / f"{_FILE_PREFIX}{datetime.now().astimezone():%Y%m%d}{_FILE_SUFFIX}"

    def _handle(self) -> IO[str]:
        path = self._today_path()
        if self._fh is not None and self._fh_path == path and not self._fh.closed:
            return self._fh
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._dir.mkdir(parents=True, exist_ok=True, mode=JOURNAL_DIR_MODE)
        # A file whose last line was torn (no trailing newline) must not swallow the next
        # record: start on a fresh line, leaving the torn one to be dropped by the reader.
        torn = False
        if path.exists() and path.stat().st_size > 0:
            with open(path, "rb") as probe:
                probe.seek(-1, os.SEEK_END)
                torn = probe.read(1) != b"\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, JOURNAL_FILE_MODE)
        fh = os.fdopen(fd, "a", encoding="utf-8")
        if torn:
            fh.write("\n")
        self._fh = fh
        self._fh_path = path
        return fh

    def append(self, record: Record) -> None:
        """Write one record as one line and flush it. Raises OSError when the journal cannot
        be written (the caller decides what that means -- for a write op, refuse it)."""
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            fh = self._handle()
            fh.write(line + "\n")
            fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
                self._fh_path = None

    def files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(self._dir.glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}"))

    def read_all(self) -> ReadResult:
        records: list[Record] = []
        skipped = 0
        for path in self.files():
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            for line in raw.decode("utf-8", errors="replace").split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if isinstance(data, dict) and isinstance(data.get("type"), str):
                    records.append(data)
                else:
                    skipped += 1
        return ReadResult(records=records, skipped_lines=skipped)

    def cleanup_old(self, *, now: float | None = None, max_age_s: float = JOURNAL_MAX_AGE_S) -> int:
        """Remove journal files not modified for `max_age_s` (default 30 days; the same
        mtime rule the in-flight records use), except a file that holds a timeline checkpoint
        no `purge` record covers (its stash is still in the checkpoint library, and the record
        is what says what it is). Called once at server startup."""
        cutoff = (now if now is not None else time.time()) - max_age_s
        old: list[Path] = []
        for path in self.files():
            try:
                if path.stat().st_mtime < cutoff:
                    old.append(path)
            except OSError:
                pass
        held = self._stash_holding_files(old) if old else set()
        removed = 0
        for path in old:
            if path in held:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def _stash_holding_files(self, candidates: list[Path]) -> set[Path]:
        """Those of `candidates` that hold a timeline `checkpoint` record whose id no `purge`
        record (in any file) lists."""
        purged: set[str] = set()
        holders: dict[Path, list[str]] = {}
        for path in self.files():
            try:
                lines = path.read_bytes().decode("utf-8", errors="replace").split("\n")
            except OSError:
                continue
            for line in lines:
                if '"purge"' not in line and '"checkpoint"' not in line:
                    continue
                record = _parse(line)
                if record is None:
                    continue
                if record.get("type") == "purge":
                    purged.update(_purge_ids(record))
                elif (
                    path in candidates
                    and record.get("type") == "checkpoint"
                    and record.get("kind") == "timeline"
                ):
                    holders.setdefault(path, []).append(str(record.get("id")))
        return {path for path, ids in holders.items() if any(i not in purged for i in ids)}


def _parse(line: str) -> Record | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _purge_ids(record: Record) -> list[str]:
    """The checkpoint ids a `purge` record lets go of: the ones it deleted and the ones it found
    already gone."""
    ids: list[str] = []
    for key in ("purged", "gone"):
        listed = record.get(key)
        ids += [str(i) for i in listed] if isinstance(listed, list) else []
    return ids


def purge_records(records: list[Record]) -> dict[str, Record]:
    """Checkpoint id -> the `purge` record that let it go."""
    found: dict[str, Record] = {}
    for record in records:
        if record.get("type") == "purge":
            for checkpoint_id in _purge_ids(record):
                found[checkpoint_id] = record
    return found


@dataclass
class IntentView:
    intent: Record
    outcome: Record | None = None
    unresolved: Record | None = None
    checks: list[Record] = field(default_factory=list)
    pending: Record | None = None
    polled: Record | None = None

    @property
    def pending_handle(self) -> str | None:
        """The bridge's pending handle of a run that had not finished when its wait ended
        (`render.batch`), None when there is none."""
        value = self.pending.get("handle") if self.pending is not None else None
        return value if isinstance(value, str) and value else None

    @property
    def client_handle(self) -> str | None:
        """The handle this server chose for the run and sent to the bridge (`render.batch`:
        `handleSource == "client"`), None when the intent has none."""
        if self.intent.get("handleSource") != "client":
            return None
        value = self.intent.get("handle")
        return value if isinstance(value, str) and value else None

    @property
    def id(self) -> str:
        return str(self.intent["id"])

    @property
    def group(self) -> str:
        return str(self.intent["group"])

    def resolving_check(self) -> Record | None:
        """The newest check that decided something (an `unknown` verdict decides nothing)."""
        for record in reversed(self.checks):
            if record.get("verdict") != VERDICT_UNKNOWN:
                return record
        return None

    def is_in_doubt(self) -> bool:
        return self.outcome is None and self.resolving_check() is None

    def state(self) -> str:
        outcome = self.outcome
        if outcome is not None:
            if outcome.get("ok") is True:
                return STATE_LANDED
            if outcome.get("errorCode") == "CONFLICT":
                return STATE_CONFLICT
            if outcome.get("verdict") == STATE_PARTIAL:
                return STATE_PARTIAL
            if outcome.get("applied"):
                return STATE_PARTIAL
            return STATE_FAILED
        check = self.resolving_check()
        if check is not None:
            return str(check["verdict"])
        return STATE_UNKNOWN

    def revert_checkpoint(self) -> str | None:
        """The checkpoint whose restoring puts this intent's group back (eval / restore
        intents), None for an attribute write."""
        value = self.intent.get("revertCheckpoint")
        return value if isinstance(value, str) and value else None

    @property
    def desktop_kind(self) -> str | None:
        """`desktop_create` / `desktop_copy` / `desktop_revert` for a desktop intent, else None."""
        kind = self.intent.get("kind")
        return kind if isinstance(kind, str) and kind in DESKTOP_KINDS else None

    def created_info(self) -> dict[str, Any] | None:
        """What a desktop write made, from its outcome, or (no outcome) from the check that found
        it had landed. None when nothing is known to have been made."""
        for record in (self.outcome, self.resolving_check()):
            if record is None:
                continue
            created = record.get("created")
            if isinstance(created, dict) and created:
                if record is self.outcome or record.get("verdict") == STATE_LANDED:
                    return created
        return None

    def fingerprint_after(self) -> str | None:
        """The group's fingerprint right after the run, when its outcome recorded one."""
        if self.outcome is None:
            return None
        value = self.outcome.get("fingerprintAfter")
        return value if isinstance(value, str) and value else None

    def is_revertible(self) -> bool:
        """A revert has something to put back: attribute keys that were applied, or a
        checkpoint intent that ran (an outcome, or a check that found the group changed)."""
        if self.revertible_keys():
            return True
        if self.desktop_kind in DESKTOP_WRITE_KINDS:
            return self.created_info() is not None
        if self.revert_checkpoint() is None:
            return False
        if self.outcome is not None:
            return True
        check = self.resolving_check()
        return check is not None and check.get("verdict") in (STATE_LANDED, STATE_DIVERGED)

    def revertible_keys(self) -> list[str]:
        """The attribute keys a revert would put back: the ones the outcome says were applied,
        or (no outcome) all of them when a check found the write had landed."""
        keys = [str(k) for k in (self.intent.get("before") or {})]
        if self.outcome is not None:
            applied = self.outcome.get("applied")
            return [k for k in keys if isinstance(applied, list) and k in applied]
        check = self.resolving_check()
        if check is not None and check.get("verdict") == STATE_LANDED:
            return keys
        return []

    def expected_after(self, key: str) -> Any:
        """The value `key` should hold now if nobody touched it since the write: what Flame
        read back after writing when the outcome has it, else what the intent meant to write."""
        if self.outcome is not None:
            read_back = self.outcome.get("readBack")
            if isinstance(read_back, dict) and key in read_back:
                return read_back[key]
        return (self.intent.get("after") or {}).get(key)


class Index:
    """The journal's records grouped for state queries. Built once per call from
    `Journal.read_all().records`; records for an unknown intent id are ignored."""

    def __init__(self, records: list[Record]) -> None:
        self.intents: dict[str, IntentView] = {}
        self.groups: dict[str, list[IntentView]] = {}
        self._reverts_of: dict[str, list[str]] = {}
        self.group_meta: dict[str, Record] = {}
        self.checkpoints: dict[str, Record] = {}
        self.purged: dict[str, Record] = purge_records(records)
        for record in records:
            kind = record.get("type")
            if kind == "group":
                self.group_meta[str(record.get("group"))] = record
                continue
            if kind == "purge":
                continue
            if kind == "checkpoint":
                self.checkpoints[str(record.get("id"))] = record
                continue
            if kind == "intent":
                view = IntentView(intent=record)
                self.intents[view.id] = view
                self.groups.setdefault(view.group, []).append(view)
                target_group = record.get("reverts")
                if isinstance(target_group, str):
                    revert_groups = self._reverts_of.setdefault(target_group, [])
                    if view.group not in revert_groups:
                        revert_groups.append(view.group)
                continue
            view_of = self.intents.get(str(record.get("id")))
            if view_of is None:
                continue
            if kind == "outcome":
                view_of.outcome = record
            elif kind == "unresolved":
                view_of.unresolved = record
            elif kind == "pending":
                view_of.pending = record
            elif kind == "polled":
                view_of.polled = record
            elif kind == "check":
                view_of.checks.append(record)
        self._reverted_memo: dict[str, bool] = {}

    def in_doubt(self, group: str | None = None) -> list[IntentView]:
        views = self.groups.get(group, []) if group is not None else list(self.intents.values())
        return [view for view in views if view.is_in_doubt()]

    def base_state(self, group: str) -> str:
        states = [view.state() for view in self.groups[group]]
        distinct = set(states)
        if len(distinct) == 1:
            return states[0]
        if STATE_LANDED in distinct and distinct & {STATE_FAILED, STATE_CONFLICT, STATE_NOT_LANDED}:
            return STATE_PARTIAL
        for state in _STATE_PRIORITY:
            if state in distinct:
                return state
        return STATE_UNKNOWN

    def _revert_views_of(self, group: str) -> list[IntentView]:
        """The intents (in any group) that put back what `group` wrote."""
        return [
            view
            for other in self._reverts_of.get(group, [])
            if other in self.groups
            for view in self.groups[other]
        ]

    def intent_reverted(self, view: IntentView) -> bool:
        """`view`'s write has been put back: a revert intent that landed and stands (its own
        group not reverted since). A revert intent that names no `revertsIntent` (records
        written before that key existed) covers every intent of the group it reverts."""
        for revert in self._revert_views_of(view.group):
            names = revert.intent.get("revertsIntent")
            if names not in (None, view.id):
                continue
            if revert.state() == STATE_LANDED and not self.is_reverted(revert.group):
                return True
        return False

    def _revertible_views(self, group: str) -> list[IntentView]:
        return [view for view in self.groups[group] if view.is_revertible()]

    def reverted_by(self, group: str) -> list[str]:
        """The revert groups aimed at `group` that stand (some intent of theirs landed and the
        group was not itself reverted since)."""
        return [
            other
            for other in self._reverts_of.get(group, [])
            if other in self.groups
            and any(view.state() == STATE_LANDED for view in self.groups[other])
            and not self.is_reverted(other)
        ]

    def is_reverted(self, group: str) -> bool:
        """Every intent of `group` that applied something has been put back."""
        cached = self._reverted_memo.get(group)
        if cached is None:
            self._reverted_memo[group] = False  # a malformed cycle in the file ends here
            views = self._revertible_views(group)
            cached = bool(views) and all(self.intent_reverted(view) for view in views)
            self._reverted_memo[group] = cached
        return cached

    def is_partially_reverted(self, group: str) -> bool:
        """Some, not all, of the intents that applied something were put back (a revert that
        stopped part way: plan.run groups have several intents)."""
        if self.is_reverted(group):
            return False
        return any(self.intent_reverted(view) for view in self._revertible_views(group))

    def group_state(self, group: str) -> str:
        if self.is_reverted(group):
            return STATE_REVERTED
        if self.is_partially_reverted(group):
            return STATE_PARTIALLY_REVERTED
        return self.base_state(group)

    def revert_groups_of(self, group: str) -> list[str]:
        return list(self._reverts_of.get(group, []))

    def group_summary(self, group: str) -> dict[str, Any]:
        views = self.groups[group]
        first = views[0].intent
        reverts = first.get("reverts")
        return {
            "group": group,
            "ts": first.get("ts"),
            "ops": [str(view.intent.get("op")) for view in views],
            "state": self.group_state(group),
            "intents": len(views),
            "label": self.group_meta.get(group, {}).get("label"),
            "reverts": reverts if isinstance(reverts, str) else None,
            "revertedBy": self.reverted_by(group),
        }

    def newest_first(self) -> list[str]:
        return list(reversed(list(self.groups)))
