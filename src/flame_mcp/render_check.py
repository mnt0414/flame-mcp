"""`journal.check` for a `render.batch` intent: what became of a render whose answer never
came. See `check_render_intent`. The run itself, its journal records (`intent`, `pending`,
`outcome`) and `judge_render` are in `render_run.py`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import transport
from flame_mcp.journal import (
    STATE_FAILED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    STATE_PARTIAL,
    VERDICT_UNKNOWN,
    IntentView,
    now_iso,
)
from flame_mcp.render_files import OutputSpec, Stat, list_directory, spec_frames_present
from flame_mcp.render_run import BRIDGE_BUSY_CODES, BRIDGE_BUSY_NOTE, frames_verdict, judge_render
from flame_mcp.transport import BridgeProtocolError, BridgeResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"


@dataclass(frozen=True)
class Polled:
    """What asking the bridge for a pending run's result came to. kind: `done` (the run's answer,
    `br`), `running` (the bridge says it is still pending), `unknown_handle` (the bridge has no
    such handle: Flame was restarted, or its result was already handed over), `unreachable`."""

    kind: str
    br: BridgeResult | None = None
    code: str | None = None
    message: str | None = None


_POLL_TRANSPORT_CODES: Final[frozenset[str]] = frozenset(
    {"BRIDGE_UNAVAILABLE", "BRIDGE_LOST", "BRIDGE_AMBIGUOUS", "TIMEOUT", "MAIN_THREAD_UNAVAILABLE"}
)


def poll_handle(ctx: ServerContext, handle: str) -> Polled:
    """`transport.poll(handle)`. An unknown handle is not a BridgeResult: the bridge answers
    `protocolError: "unknown handle: ..."` and `transport` raises `BridgeProtocolError` for it
    (`_send_request`); a BridgeResult with an error code is a transport failure."""
    try:
        br = transport.poll(handle, run_dir=ctx.run_dir)
    except BridgeProtocolError as exc:
        if "unknown handle" in str(exc):
            return Polled("unknown_handle", message=str(exc))
        return Polled("unreachable", code="PROTOCOL_ERROR", message=str(exc))
    if br.error_code in _POLL_TRANSPORT_CODES:
        return Polled("unreachable", br, br.error_code, br.error)
    if br.ok and br.pending_handle is not None and br.result is None:
        return Polled("running", br)
    return Polled("done", br)


def _polled_record(
    ctx: ServerContext, view: IntentView, handle: str, br: BridgeResult
) -> dict[str, Any]:
    op = ctx.registry.get("render.batch")
    args = view.intent.get("args") or {}
    env = envelope_mod.from_bridge_result(op, args, br) if op is not None else {}
    flame = env.get("result")
    return {
        "type": "polled",
        "id": view.id,
        "group": view.group,
        "ts": now_iso(),
        "handle": handle,
        "ok": env.get("ok") is True,
        "errorCode": env.get("errorCode"),
        "error": env.get("error"),
        "flame": flame if isinstance(flame, dict) else None,
        "queuedMs": br.queued_ms,
        "runMs": br.run_ms,
    }


def _disk_rows(intent: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Per Write File node of the intent: the planned frames present now that were written since
    the intent; and the totals (written, expected)."""
    rows: list[dict[str, Any]] = []
    written = 0
    expected = 0
    for node in intent.get("nodes") or []:
        spec_raw = node.get("spec")
        if not isinstance(spec_raw, dict):
            continue
        spec = OutputSpec.from_json(spec_raw)
        before: dict[str, Stat] = {
            str(name): (int(stat[0]), int(stat[1]))
            for name, stat in (node.get("preexisting") or {}).items()
        }
        present, missing = spec_frames_present(spec, before, list_directory(spec.directory))
        written += present
        expected += spec.count
        rows.append(
            {
                "node": node.get("name"),
                "expectedFrames": spec.count,
                "writtenFrames": present,
                "missing": missing,
            }
        )
    return rows, written, expected


def _clip_total(reels: Any) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for row in reels or []:
        if isinstance(row, dict) and isinstance(row.get("clipCount"), int):
            counts[(str(row.get("kind")), str(row.get("name")))] = row["clipCount"]
    return counts


def _reels_grew(before: Any, now: Any) -> bool:
    old = _clip_total(before)
    return any(count > old.get(key, 0) for key, count in _clip_total(now).items())


def _unknown_entry(entry: dict[str, Any], detail: str, code: str | None = None) -> dict[str, Any]:
    if code in BRIDGE_BUSY_CODES:
        detail = f"{detail}. {BRIDGE_BUSY_NOTE}"
    entry.update(verdict=VERDICT_UNKNOWN, detail=detail)
    return entry


def _append_check(
    ctx: ServerContext,
    view: IntentView,
    verdict: str,
    per_key: Mapping[str, Any],
    current: Mapping[str, Any],
    **extra: Any,
) -> None:
    ctx.journal.append(
        {
            "type": "check",
            "id": view.id,
            "group": view.group,
            "ts": now_iso(),
            "verdict": verdict,
            "perKey": dict(per_key),
            "current": dict(current),
            "target": view.intent.get("target"),
            **extra,
        }
    )
    ctx.guard.release(view.id)


def _check_with_result(
    ctx: ServerContext, view: IntentView, entry: dict[str, Any], polled: Mapping[str, Any]
) -> dict[str, Any]:
    """The bridge has handed over the run's answer (`polled` record): the render has ended. The
    files are counted as the check of what it wrote, and the verdict follows `judge_render`:
    landed (all is well), partial (some frames of a render that reported a problem or came out
    short), failed (the render reported a failure and wrote none / all of it)."""
    flame_raw = polled.get("flame")
    flame: dict[str, Any] = flame_raw if isinstance(flame_raw, dict) else {}
    rows, written, expected = _disk_rows(view.intent)
    judged = judge_render(
        env_ok=polled.get("ok") is True,
        env_code=polled.get("errorCode"),
        env_error=polled.get("error"),
        flame=flame,
        written=written,
        expected=expected,
        checked=bool(rows),
    )
    if judged.ok:
        verdict = STATE_LANDED
    elif 0 < written < expected:
        verdict = STATE_PARTIAL
    else:
        verdict = STATE_FAILED
    per_key = {
        str(r["node"]): (
            STATE_LANDED
            if r["writtenFrames"] >= r["expectedFrames"]
            else STATE_PARTIAL
            if r["writtenFrames"]
            else STATE_NOT_LANDED
        )
        for r in rows
    }
    current = {"writtenFrames": written, "expectedFrames": expected, "nodes": rows}
    outcome = {
        "ok": judged.ok,
        "errorCode": judged.code,
        "error": judged.message,
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "newClips": flame.get("newClips"),
    }
    flame_saved = {
        k: flame.get(k) for k in ("returned", "elapsedS", "newClips", "reelsBefore", "reelsAfter")
    }
    entry.update(
        verdict=verdict,
        basis="handle",
        perKey=per_key,
        current=current,
        renderOutcome=outcome,
    )
    if not rows:
        entry["detail"] = (
            "only Render nodes were rendered: nothing to look for on disk (result.newClips of "
            "renderOutcome says what the reels gained)"
        )
    elif not judged.ok:
        entry["detail"] = f"the render ended and reported: {judged.message}"
    _append_check(
        ctx,
        view,
        verdict,
        per_key,
        current,
        basis="handle",
        renderOutcome=outcome,
        flame=flame_saved,
    )
    return entry


_DISK_NOTE: Final[str] = (
    "the end of the render was not confirmed (no answer from the bridge for it): this verdict "
    "is an estimate from the files"
)


def _check_by_disk(
    ctx: ServerContext, view: IntentView, entry: dict[str, Any], reason: str | None
) -> dict[str, Any]:
    """No handle (a TIMEOUT / BRIDGE_LOST carries none) or one the bridge no longer knows: read
    Flame again (the group, its reels), then look at the files the intent planned. landed = every
    planned frame written since the intent, partial = some, not_landed = none. A render of
    Render nodes only has no files: the group's reel clip counts are compared with the
    intent's. This is an estimate -- the files can be complete while render() has not returned
    (measured: 8 s apart) -- so the entry says `basis: "disk"`."""
    intent = view.intent
    env = ctx.do("render.plan", {"target": intent.get("target") or {}})
    group_found = True
    if env.get("ok") is not True:
        if env.get("errorCode") != _NOT_FOUND_CODE:
            unread = f"Flame could not be read ({env.get('errorCode')}: {env.get('error')})"
            return _unknown_entry(entry, unread, env.get("errorCode"))
        group_found = False
    rows, written, expected = _disk_rows(intent)
    verdict: str | None = frames_verdict(written, expected)
    notes = [_DISK_NOTE] if reason is None else [reason, _DISK_NOTE]
    if verdict is None:
        current_reels = (env.get("result") or {}).get("reels") if group_found else None
        if not group_found:
            verdict = "diverged"
            notes.append("the Batch group was not found and the render had no files to look for")
        elif _reels_grew(intent.get("reelsBefore"), current_reels):
            verdict = STATE_LANDED
            notes.append(
                "a reel of the group holds more clips than before the render (Render nodes)"
            )
        else:
            verdict = STATE_NOT_LANDED
            notes.append(
                "no reel of the group gained a clip (Render nodes have no files to look for)"
            )
    per_key = {
        str(r["node"]): frames_verdict(r["writtenFrames"], r["expectedFrames"]) for r in rows
    }
    current = {"writtenFrames": written, "expectedFrames": expected, "nodes": rows}
    entry.update(verdict=verdict, basis="disk", perKey=per_key, current=current)
    entry["detail"] = ". ".join(notes)
    if not group_found:
        entry["groupFound"] = False
    _append_check(ctx, view, verdict, per_key, current, basis="disk")
    return entry


def check_render_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """Decide what became of a `render.batch` intent that has no outcome. Nothing is written to
    the project.

    1. A journaled `polled` record (the bridge's answer, already fetched by an earlier check that
       did not finish): use it. The bridge hands a result over once, so it is never asked again.
    2. A handle to ask with -- the `pending` record's (the bridge's own), else the intent's
       client handle (chosen by this server: it is there even when the run ended in
       BRIDGE_LOST or a bare TIMEOUT and no pending handle ever came): ask the bridge
       (`transport.poll`) first. Its answer is what says the render has ended -- the files being
       complete does not (in one run render() returned 8 s after the last frame was written).
       Answer -> journal it at once, then count the files as a check of what was written
       (`basis: "handle"`). Still pending -> `unknown` (the door stays shut). Bridge unreachable
       -> `unknown`. Unknown handle (Flame restarted, or the result file is gone) -> step 3.
    3. Otherwise the files (and Flame's reels) alone decide (`basis: "disk"`, an estimate)."""
    intent = view.intent
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    polled = view.polled
    handle = view.pending_handle
    from_intent = handle is None and view.client_handle is not None
    if handle is None:
        handle = view.client_handle
    reason: str | None = None
    if polled is None and handle is not None:
        result = poll_handle(ctx, handle)
        if result.kind == "running":
            return _unknown_entry(
                entry,
                "the render is still running (the bridge says the handle is still pending): "
                "run journal.check again later",
            )
        if result.kind == "unreachable":
            return _unknown_entry(
                entry,
                f"the bridge could not be asked for the render's result ({result.code}: "
                f"{result.message})",
                result.code,
            )
        if result.kind == "unknown_handle":
            reason = (
                f"the bridge no longer knows handle {handle} (Flame was restarted, or its "
                "result was already handed over)"
                if not from_intent
                else f"the bridge does not know handle {handle}, which this server gave the "
                "render: Flame was restarted before the render finished, or the result file "
                "was removed"
            )
        elif result.br is not None:
            polled = _polled_record(ctx, view, handle, result.br)
            try:
                ctx.journal.append(polled)
            except OSError:
                pass  # this check still goes on with the answer in hand
    if polled is not None:
        return _check_with_result(ctx, view, entry, polled)
    return _check_by_disk(ctx, view, entry, reason)
