"""`journal.purge_checkpoints`, server side.

The stash (the checkpoint library, `timeline_checkpoints`) only grows: every Timeline checkpoint,
every restore's pre-restore copy and every `desktop.delete` leaves a copy in it. This is the one
place that deletes from it, and it deletes only what the journal records:

  scan     one Flame read (`_tp_scan` of `desktop_delete_prelude`): what the stash library holds,
           and for every candidate (a recorded timeline checkpoint older than the cutoff) whether
           its copy is still there, with the uid and the name the journal recorded. What the
           library holds that no recorded checkpoint accounts for is `unknown`: reported, never
           deleted.
  purge    (dry_run false) Flame deletes each candidate again checking, in Flame, that it is
           inside the library with the recorded uid and the recorded name; then the date reels
           (`timeline_<YYMMDD>`, directly in the library) this left empty. A `purge` record lists
           the checkpoint ids deleted (`purged`) and the ones whose copy was already gone (`gone`):
           from then on they are shown as purged and cannot be restored, and the journal's own
           cleanup (`Journal.cleanup_old`) may drop the file that holds their records.

The Flame side is an op of a registry of its own (like `timeline_restore.restore_op`): it gets the
registration checks and the execution path of every op but is not in the public catalog.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import execution
from flame_mcp.checkpoints import CHECKPOINT_TIMEOUT_S, TIMELINE_KIND, kind_named
from flame_mcp.errors import as_error_code
from flame_mcp.journal import Index, new_id, now_iso
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.timeline_checkpoints import (
    LIBRARY_ARG,
    REEL_PREFIX,
    checkpoint_library_name,
)
from flame_mcp.types import Category, OperationSpec, ServerOpError, ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

PHASE_ARG: Final[str] = "_phase"
PHASE_SCAN: Final[str] = "scan"
PHASE_PURGE: Final[str] = "purge"
CANDIDATES_ARG: Final[str] = "_candidates"
KNOWN_ARG: Final[str] = "_known"
ITEMS_ARG: Final[str] = "_items"
BATCH_SIZE: Final[int] = 50
"""Most stashed copies one Flame call deletes (each call waits up to `CHECKPOINT_TIMEOUT_S`)."""
DEFAULT_OLDER_THAN_DAYS: Final[int] = 30
TOKEN_LENGTH: Final[int] = 32


_RAISE_ON_ERROR: Final[str] = (
    "if _result['error'] is not None:\n    raise _OpError(_result['errorCode'], _result['error'])"
)


def _source(*blocks: str) -> str:
    return "\n\n".join(blocks)


def _purge_to_python(args: Mapping[str, Any]) -> str:
    library = args.get(LIBRARY_ARG)
    if args.get(PHASE_ARG) == PHASE_PURGE:
        return _source(
            f"_result = _tp_purge({library!r}, {list(args.get(ITEMS_ARG) or [])!r}, "
            f"{REEL_PREFIX!r})",
            _RAISE_ON_ERROR,
            "_affected = len(_result['deleted'])",
        )
    return _source(
        f"_result = _tp_scan({library!r}, {list(args.get(CANDIDATES_ARG) or [])!r}, "
        f"{list(args.get(KNOWN_ARG) or [])!r})",
        "_affected = 0",
    )


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


@cache
def purge_op() -> RegisteredOperation:
    registry = Registry()
    registry.register_category(
        Category(
            name="timeline",
            description="internal",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    return registry.register(
        OperationSpec(
            name="timeline.purge",
            category="timeline",
            description="internal: the Flame side of journal.purge_checkpoints",
            effect="destructive",
            to_python=_purge_to_python,
            undo_steps=_no_undo_steps,
            requires=(
                "flame.delete",
                "PyWorkspace.libraries",
                "PyLibrary.reels",
                "PyLibrary.children",
                "PyReel.children",
                "flame.find_by_uid",
            ),
        )
    )


def _flame(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    env = execution.run_op(purge_op(), args, run_dir=ctx.run_dir, timeout_s=CHECKPOINT_TIMEOUT_S)
    if env.get("ok") is not True:
        raw = env.get("result")
        raise ServerOpError(
            as_error_code(str(env.get("errorCode"))),
            f"the checkpoint library could not be read or cleaned: {env.get('error')}",
            result=raw if isinstance(raw, dict) else None,
            hint=env.get("hint"),
        )
    raw = env.get("result")
    return dict(raw) if isinstance(raw, dict) else {}


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def recorded_timeline_checkpoints(index: Index) -> list[dict[str, Any]]:
    return [r for r in index.checkpoints.values() if kind_named(r.get("kind")) is TIMELINE_KIND]


def _stash_uid(record: Mapping[str, Any]) -> str | None:
    stash = record.get("stash")
    uid = stash.get("uid") if isinstance(stash, dict) else None
    return uid if isinstance(uid, str) and uid else None


def _candidates(index: Index, cutoff: datetime) -> list[dict[str, Any]]:
    """The recorded, not yet purged timeline checkpoints older than `cutoff`, oldest first."""
    found = []
    for record in recorded_timeline_checkpoints(index):
        ts = _parse_ts(record.get("ts"))
        uid = _stash_uid(record)
        if record.get("id") in index.purged or ts is None or uid is None or ts >= cutoff:
            continue
        found.append(record)
    return sorted(found, key=lambda r: str(r.get("ts")))


def _candidate_row(record: Mapping[str, Any], found: Mapping[str, Any]) -> dict[str, Any]:
    stash = record.get("stash") or {}
    source = record.get("source") or {}
    return {
        "id": record.get("id"),
        "ts": record.get("ts"),
        "label": record.get("label"),
        "group": record.get("group"),
        "sourceName": source.get("name"),
        "stash": {
            "uid": stash.get("uid"),
            "name": stash.get("name"),
            "library": stash.get("library"),
            "reel": found.get("reel") or stash.get("reel"),
        },
        "exists": found.get("exists") is True,
        "nameMatches": found.get("nameMatches") is True,
    }


def _scan(
    ctx: ServerContext, index: Index, library: str, cutoff: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = _candidates(index, cutoff)
    known = [
        uid for r in recorded_timeline_checkpoints(index) if (uid := _stash_uid(r)) is not None
    ]
    candidates = [
        {
            "id": r.get("id"),
            "uid": _stash_uid(r),
            "name": (r.get("stash") or {}).get("name"),
        }
        for r in records
    ]
    scan = _flame(
        ctx,
        {
            PHASE_ARG: PHASE_SCAN,
            LIBRARY_ARG: library,
            CANDIDATES_ARG: candidates,
            KNOWN_ARG: known,
        },
    )
    found = scan.get("candidates") or {}
    rows = [_candidate_row(r, found.get(str(r.get("id"))) or {}) for r in records]
    return rows, scan


def _purge_batches(
    ctx: ServerContext, library: str, items: list[dict[str, Any]]
) -> tuple[dict[str, Any], ServerOpError | None]:
    done: dict[str, Any] = {"deleted": [], "skipped": [], "reelsDeleted": []}
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start : start + BATCH_SIZE]
        try:
            value = _flame(ctx, {PHASE_ARG: PHASE_PURGE, LIBRARY_ARG: library, ITEMS_ARG: batch})
        except ServerOpError as exc:
            return done, exc
        for key in done:
            done[key] += value.get(key) or []
    return done, None


def purge_token(rows: list[dict[str, Any]]) -> str:
    """A digest of the candidate list (id, recorded uid and name, whether the copy is there and
    still has that name): the dry run hands it out, and a real run is only allowed when the list
    it would delete from still digests to the same value."""
    material = [
        [r["id"], r["stash"]["uid"], r["stash"]["name"], r["exists"], r["nameMatches"]]
        for r in rows
    ]
    return hashlib.sha256(json.dumps(material).encode("utf-8")).hexdigest()[:TOKEN_LENGTH]


def _token_refusal(given: Any, current: str, count: int) -> ServerOpError | None:
    if given is None:
        reason = "a real run (dry_run=false) needs the `token` that a dry run returned"
    elif given != current:
        reason = (
            "the `token` is not the one for what would be deleted now (the candidates changed "
            "since that dry run, or it was run with another older_than_days)"
        )
    else:
        return None
    return ServerOpError(
        "PRECONDITION_UNMET",
        f"nothing was deleted: {reason}. Run it again with dry_run=true, look at the {count} "
        "candidate(s) it lists, and pass its `token` with dry_run=false",
        result={"ran": False, "count": count},
        hint="Nothing was changed.",
    )


def purge_checkpoints(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    days = int(args["older_than_days"])
    dry_run = bool(args["dry_run"])
    library = checkpoint_library_name()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with ctx.write_lock:
        index = Index(ctx.journal.read_all().records)
        rows, scan = _scan(ctx, index, library, cutoff)
        token = purge_token(rows)
        if not dry_run:
            refusal = _token_refusal(args.get("token"), token, len(rows))
            if refusal is not None:
                raise refusal
        deletable = [r for r in rows if r["exists"] and r["nameMatches"]]
        gone = [r for r in rows if not r["exists"]]
        base: dict[str, Any] = {
            "dryRun": dry_run,
            "olderThanDays": days,
            "library": library,
            "libraryFound": scan.get("libraryFound"),
            "candidates": rows,
            "token": token,
            "count": len(deletable),
            "missing": [r["id"] for r in gone],
            "unknown": scan.get("unknown") or [],
            "note": (
                "unknown = what the checkpoint library holds that no recorded checkpoint "
                "accounts for: it is never deleted here. A candidate whose name is not the "
                "recorded one is not deleted either."
            ),
        }
        if scan.get("librariesOpened"):
            base["librariesOpened"] = scan["librariesOpened"]
            base["notes"] = scan.get("notes") or []
        if dry_run:
            return ServerResult(result=base)
        items = [
            {"id": r["id"], "uid": r["stash"]["uid"], "name": r["stash"]["name"]}
            for r in rows
            if r["exists"]
        ]
        done, failure = _purge_batches(ctx, library, items)
        purged = [d["id"] for d in done["deleted"]]
        record_id = new_id("p")
        record = {
            "type": "purge",
            "id": record_id,
            "ts": now_iso(),
            "olderThanDays": days,
            "library": library,
            "purged": purged,
            "gone": [r["id"] for r in gone] if failure is None else [],
            "skipped": done["skipped"],
            "reelsDeleted": done["reelsDeleted"],
        }
        journal_error: str | None = None
        if purged or record["gone"]:
            try:
                ctx.journal.append(record)
            except OSError as exc:
                journal_error = str(exc)
        result = {
            **base,
            "purged": done["deleted"],
            "gone": record["gone"],
            "skipped": done["skipped"],
            "reelsDeleted": done["reelsDeleted"],
            "purgeRecord": record_id if journal_error is None else None,
        }
        if journal_error is not None:
            result["journalError"] = (
                f"the journal could not take the purge record ({journal_error}): the checkpoints "
                "stay listed as not purged; a purge again finds their copies gone"
            )
        if failure is not None:
            raise ServerOpError(
                failure.code,
                f"{failure.message} -- {len(purged)} of {len(items)} stashed copies were deleted "
                "before it (a purge with dry_run=true shows what is left)",
                result=result,
                hint=failure.hint,
                affected=len(purged),
            )
        return ServerResult(result=result, affected=len(purged))
