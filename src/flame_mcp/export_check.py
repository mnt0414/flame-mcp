"""`journal.check` for a `render.export` intent: what became of an export whose answer never came.
See `check_export_intent`. The run, its journal records and `judge_export` are in
`export_run.py`; asking the bridge for the result is `render_check.fetch_answer`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.export_run import (
    NOTHING_WRITTEN,
    OP_NAME,
    Written,
    before_stats,
    count_written,
    disk_verdict,
    file_rows,
    judge_export,
    unsure_reason,
)
from flame_mcp.journal import (
    STATE_DIVERGED,
    STATE_FAILED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    STATE_PARTIAL,
    IntentView,
)
from flame_mcp.render_check import _append_check, _unknown_entry, fetch_answer
from flame_mcp.render_files import EXPORT_COUNT_DEPTH, EXPORT_COUNT_LIMIT

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

NOUN: Final[str] = "export"
_DISK_NOTE: Final[str] = (
    "no answer from the bridge says the export has ended: this verdict is an estimate from the "
    "files in the output directory"
)
_GONE_NOTE: Final[str] = "the output directory is not there any more"
_PERSON_NOTE: Final[str] = (
    "a person has to look at the output directory: this server has no operation that records a "
    "person's answer, so writes stay refused until journal.check can count it (for example "
    "after files have been moved out of the directory)"
)


def _output_dir(intent: Mapping[str, Any]) -> str:
    return str(intent.get("outputDir") or "")


def _expected(intent: Mapping[str, Any]) -> int | None:
    expected = intent.get("expectedFrames")
    return expected if isinstance(expected, int) else None


def _count(intent: Mapping[str, Any]) -> Written:
    """The files below the output directory that are new since the intent (not hidden ones)."""
    return count_written(
        _output_dir(intent), before_stats(intent), EXPORT_COUNT_DEPTH, EXPORT_COUNT_LIMIT
    )


def _current(found: Written, expected: int | None) -> dict[str, Any]:
    shown, truncated = file_rows(found)
    return {
        "writtenCount": found.count,
        "expectedFrames": expected,
        "countComplete": found.complete,
        "files": shown,
        "filesTruncated": truncated,
    }


def _check_with_result(
    ctx: ServerContext, view: IntentView, entry: dict[str, Any], polled: Mapping[str, Any]
) -> dict[str, Any]:
    """The bridge has handed over the run's answer (`polled` record): the export has ended. The
    files are counted as the check of what it wrote, and the verdict follows `judge_export`:
    landed, partial (some files of an export that reported a problem or came out short),
    not_landed (it answered and wrote nothing), failed (it reported an error and wrote nothing),
    diverged (the output directory is gone). An export that stopped before it started (CONFLICT)
    wrote nothing, whatever is in the directory now."""
    intent = view.intent
    flame_raw = polled.get("flame")
    flame: dict[str, Any] = flame_raw if isinstance(flame_raw, dict) else {}
    expected = _expected(intent)
    gone = not os.path.isdir(_output_dir(intent))
    before_start = polled.get("errorCode") == "CONFLICT"
    found = NOTHING_WRITTEN if before_start or gone else _count(intent)
    unsure = unsure_reason(found, expected)
    judged = judge_export(
        env_ok=polled.get("ok") is True,
        env_code=polled.get("errorCode"),
        env_error=polled.get("error"),
        flame=flame,
        written=found.count,
        expected=expected,
        unsure=unsure,
    )
    if gone:
        verdict = STATE_DIVERGED
    elif judged.ok:
        verdict = STATE_LANDED
    elif found.count > 0:
        verdict = STATE_PARTIAL
    elif judged.code == "SILENT_NOOP":
        verdict = STATE_NOT_LANDED
    else:
        verdict = STATE_FAILED
    per_key = {"files": verdict}
    current = _current(found, expected)
    outcome = {
        "ok": judged.ok,
        "errorCode": judged.code,
        "error": judged.message,
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
    }
    entry.update(
        verdict=verdict, basis="handle", perKey=per_key, current=current, exportOutcome=outcome
    )
    notes = [_GONE_NOTE] if gone else []
    if not judged.ok:
        notes.append(f"the export ended and reported: {judged.message}")
    if judged.ok and unsure is not None:
        notes.append(unsure)
    if notes:
        entry["detail"] = ". ".join(notes)
    _append_check(
        ctx, view, verdict, per_key, current, basis="handle", exportOutcome=outcome, flame=outcome
    )
    return entry


def _check_by_disk(
    ctx: ServerContext, view: IntentView, entry: dict[str, Any], reason: str | None
) -> dict[str, Any]:
    """No handle (a TIMEOUT / BRIDGE_LOST carries none) or one the bridge no longer knows: the
    output directory alone decides. Every expected file (any file when the intent could not say
    how many) written since the intent = landed, some = partial, none = not_landed; a directory
    that is gone is diverged. A count that cannot settle it (the listing was cut short) leaves the
    check unknown, with a note that a person has to look at the output directory: this server has
    no operation that records a person's answer. Folders with no file in them are not_landed. This
    is an estimate -- nothing says the export has ended -- so the entry says `basis: "disk"`.
    Flame is not asked: it may still be busy with the export."""
    intent = view.intent
    expected = _expected(intent)
    notes = [_DISK_NOTE] if reason is None else [reason, _DISK_NOTE]
    if not os.path.isdir(_output_dir(intent)):
        found = NOTHING_WRITTEN
        verdict = STATE_DIVERGED
        notes.append(_GONE_NOTE)
    else:
        found = _count(intent)
        unsure = unsure_reason(found, expected)
        if unsure is not None:
            return _unknown_entry(entry, ". ".join([*notes, unsure, _PERSON_NOTE]), noun=NOUN)
        verdict = disk_verdict(found.count, expected)
    per_key = {"files": verdict}
    current = _current(found, expected)
    entry.update(verdict=verdict, basis="disk", perKey=per_key, current=current)
    entry["detail"] = ". ".join(notes)
    _append_check(ctx, view, verdict, per_key, current, basis="disk")
    return entry


def check_export_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """Decide what became of a `render.export` intent that has no outcome. Nothing is written to
    the project or to the output directory.

    1. A journaled `polled` record, or the bridge's answer for a handle to ask with (the
       `pending` record's, else the intent's client handle): `render_check.fetch_answer`. An
       answer says the export has ended (`basis: "handle"`, `_check_with_result`). An export still
       running, or a bridge that cannot be asked, is `unknown` (the write guard stays shut:
       writes are refused until `journal.check`).
    2. Otherwise -- no handle, or one the bridge no longer knows -- the output directory alone
       decides (`basis: "disk"`, an estimate)."""
    intent = view.intent
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    answer = fetch_answer(ctx, view, entry, OP_NAME, NOUN)
    if answer.stop is not None:
        return answer.stop
    if answer.polled is not None:
        return _check_with_result(ctx, view, entry, answer.polled)
    return _check_by_disk(ctx, view, entry, answer.reason)
