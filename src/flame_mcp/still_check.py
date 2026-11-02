"""`journal.check` for a `render.still` intent: what became of a still whose answer never came,
and what it left in Flame. See `check_still_intent`. The run and its journal records are in
`still_run.py`; asking the bridge for the result is `render_check.fetch_answer`.

Unlike `render.export`, a dead call can leave something in Flame besides the file: the reference
copy of the clip (named `tempName`, in the reel `flame_mcp_still` of the first reel group of the
Desktop) and the reel itself. The check therefore asks Flame once more (`_st_cleanup`) and
deletes the copies it finds -- only those that look like the one the call made: the name of the
intent, the duration of the clip, and in and out marks that are unset or the intent's. A copy
that does not match is left alone and reported (CONFLICT)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import execution
from flame_mcp.desktop_run import PHASE_ARG
from flame_mcp.export_run import (
    NOTHING_WRITTEN,
    Written,
    before_stats,
    count_written,
    file_rows,
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
from flame_mcp.still_run import (
    OP_NAME,
    PHASE_CLEANUP,
    STILL_ARG,
    cleanup_params,
    judge_still,
)

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

NOUN: Final[str] = "still"
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
_CLEANUP_KEYS: Final[tuple[str, ...]] = ("found", "removed", "conflicts", "reelRemoved")


def _output_dir(intent: Mapping[str, Any]) -> str:
    return str(intent.get("outputDir") or "")


def _count(intent: Mapping[str, Any]) -> Written:
    return count_written(
        _output_dir(intent), before_stats(intent), EXPORT_COUNT_DEPTH, EXPORT_COUNT_LIMIT
    )


def _current(found: Written) -> dict[str, Any]:
    shown, truncated = file_rows(found)
    return {
        "writtenCount": found.count,
        "expectedFrames": 1,
        "countComplete": found.complete,
        "files": shown,
        "filesTruncated": truncated,
    }


def _verdict_of_count(count: int) -> str:
    if count == 1:
        return STATE_LANDED
    return STATE_PARTIAL if count > 1 else STATE_NOT_LANDED


def _clean_up(ctx: ServerContext, view: IntentView, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Ask Flame for the copies the call left and delete the ones that match the intent.
    Returns the unknown entry that ends the check when Flame could not be asked, else None (the
    outcome is in `entry["cleanup"]`)."""
    op = ctx.registry.get(OP_NAME)
    if op is None:
        return None
    args = {
        "source": {},
        PHASE_ARG: PHASE_CLEANUP,
        STILL_ARG: cleanup_params(view.intent),
    }
    env = execution.run_op(op, args, run_dir=ctx.run_dir)
    raw = env.get("result")
    if env.get("ok") is not True or not isinstance(raw, dict):
        code = env.get("errorCode")
        return _unknown_entry(
            entry,
            f"Flame could not be asked for the temporary copy of the clip ({code}: "
            f"{env.get('error')}), so it was neither looked for nor deleted",
            str(code) if code else None,
            NOUN,
        )
    entry["cleanup"] = {key: raw.get(key) for key in _CLEANUP_KEYS}
    conflicts = raw.get("conflicts") or []
    if conflicts:
        names = ", ".join(f"{row.get('name')} ({row.get('why')})" for row in conflicts)
        entry["errorCode"] = "CONFLICT"
        entry["detail"] = ". ".join(
            [
                *([str(entry["detail"])] if entry.get("detail") else []),
                f"not deleted, because it is not what the intent describes: {names}",
            ]
        )
    return None


def _finish(
    ctx: ServerContext,
    view: IntentView,
    entry: dict[str, Any],
    verdict: str,
    found: Written,
    basis: str,
    **extra: Any,
) -> dict[str, Any]:
    stop = _clean_up(ctx, view, entry)
    if stop is not None:
        return stop
    per_key = {"files": verdict}
    current = _current(found)
    entry.update(verdict=verdict, basis=basis, perKey=per_key, current=current)
    _append_check(ctx, view, verdict, per_key, current, basis=basis, **extra)
    return entry


def _check_with_result(
    ctx: ServerContext, view: IntentView, entry: dict[str, Any], polled: Mapping[str, Any]
) -> dict[str, Any]:
    """The bridge has handed over the run's answer: the still has ended. The files are counted
    and judged as in `still_run` (`judge_still`). A run that stopped before it copied anything
    (CONFLICT) wrote nothing, whatever is in the directory now."""
    intent = view.intent
    flame_raw = polled.get("flame")
    flame: dict[str, Any] = flame_raw if isinstance(flame_raw, dict) else {}
    gone = not os.path.isdir(_output_dir(intent))
    before_start = polled.get("errorCode") == "CONFLICT"
    found = NOTHING_WRITTEN if before_start or gone else _count(intent)
    unsure = unsure_reason(found, 1)
    judged = judge_still(
        env_ok=polled.get("ok") is True,
        env_code=polled.get("errorCode"),
        env_error=polled.get("error"),
        flame=flame,
        written=found.count,
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
    outcome = {
        "ok": judged.ok,
        "errorCode": judged.code,
        "error": judged.message,
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "tempRemoved": flame.get("tempRemoved"),
    }
    entry["stillOutcome"] = outcome
    notes = [_GONE_NOTE] if gone else []
    if not judged.ok:
        notes.append(f"the still ended and reported: {judged.message}")
    if judged.ok and unsure is not None:
        notes.append(unsure)
    if notes:
        entry["detail"] = ". ".join(notes)
    return _finish(ctx, view, entry, verdict, found, "handle", stillOutcome=outcome, flame=outcome)


def _check_by_disk(
    ctx: ServerContext, view: IntentView, entry: dict[str, Any], reason: str | None
) -> dict[str, Any]:
    """No handle (a TIMEOUT / BRIDGE_LOST carries none) or one the bridge no longer knows: the
    output directory alone decides. One new file = landed, none = not_landed, two or more =
    partial; a directory that is gone is diverged; a listing cut short leaves the check unknown.
    This is an estimate -- nothing says the export has ended -- so the entry says `basis:
    "disk"`."""
    intent = view.intent
    notes = [_DISK_NOTE] if reason is None else [reason, _DISK_NOTE]
    if not os.path.isdir(_output_dir(intent)):
        found = NOTHING_WRITTEN
        verdict = STATE_DIVERGED
        notes.append(_GONE_NOTE)
    else:
        found = _count(intent)
        unsure = unsure_reason(found, 1)
        if unsure is not None:
            return _unknown_entry(entry, ". ".join([*notes, unsure, _PERSON_NOTE]), noun=NOUN)
        verdict = _verdict_of_count(found.count)
    entry["detail"] = ". ".join(notes)
    return _finish(ctx, view, entry, verdict, found, "disk")


def check_still_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """Decide what became of a `render.still` intent that has no outcome, and clean up after it.

    1. The verdict, as for `render.export` (`export_check.check_export_intent`): the bridge's
       answer for the handle first (`basis: "handle"`), else the output directory (`basis:
       "disk"`, an estimate). A still that is still running, or a bridge that cannot be asked, is
       `unknown`.
    2. Then Flame is asked once for the reference copies the call left (`_clean_up`). If it
       cannot be asked the check stays `unknown` and writes stay refused. Nothing is written to
       the clip itself."""
    intent = view.intent
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    answer = fetch_answer(ctx, view, entry, OP_NAME, NOUN)
    if answer.stop is not None:
        return answer.stop
    if answer.polled is not None:
        return _check_with_result(ctx, view, entry, answer.polled)
    return _check_by_disk(ctx, view, entry, answer.reason)
