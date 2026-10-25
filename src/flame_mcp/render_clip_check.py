"""`journal.check` for a `render.clip` intent: what became of a render whose answer never came.
See `check_render_clip_intent`. The run, its journal records and `judge_render_clip` are in
`render_clip_run.py`; asking the bridge for the result is `render_check.fetch_answer`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.desktop_revert import observe
from flame_mcp.desktop_run import PHASE_ARG, PHASE_READ
from flame_mcp.journal import (
    STATE_DIVERGED,
    STATE_FAILED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    IntentView,
)
from flame_mcp.render_check import _append_check, _unknown_entry, fetch_answer
from flame_mcp.render_clip_run import MEDIA_TYPES, OP_NAME, judge_render_clip

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"
_FLAME_NOTE: Final[str] = (
    "no answer from the bridge says the render ended: this verdict is read from Flame "
    "(is_rendered()) and is an estimate"
)
_ALREADY_RENDERED_NOTE: Final[str] = (
    "the clip was already rendered when the call was made, so is_rendered() reading true does "
    "not show that this render finished"
)


def _check_with_result(
    ctx: ServerContext, view: IntentView, entry: dict[str, Any], polled: Mapping[str, Any]
) -> dict[str, Any]:
    """The bridge has handed over the run's answer (`polled` record): the render has ended. The
    verdict follows `judge_render_clip`: landed (is_rendered() read true afterwards), not_landed
    (the render answered and is_rendered() did not read true), failed (the render reported an
    error)."""
    flame_raw = polled.get("flame")
    flame: dict[str, Any] = flame_raw if isinstance(flame_raw, dict) else {}
    judged = judge_render_clip(
        env_ok=polled.get("ok") is True,
        env_code=polled.get("errorCode"),
        env_error=polled.get("error"),
        flame=flame,
    )
    if judged.ok:
        verdict = STATE_LANDED
    elif judged.code == "SILENT_NOOP":
        verdict = STATE_NOT_LANDED
    else:
        verdict = STATE_FAILED
    per_key = {"timelineFx": verdict}
    current = {"isRendered": flame.get("isRenderedAfter")}
    outcome = {
        "ok": judged.ok,
        "errorCode": judged.code,
        "error": judged.message,
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "isRenderedBefore": flame.get("isRenderedBefore"),
        "isRenderedAfter": flame.get("isRenderedAfter"),
    }
    entry.update(
        verdict=verdict, basis="handle", perKey=per_key, current=current, renderOutcome=outcome
    )
    if not judged.ok:
        entry["detail"] = f"the render ended and reported: {judged.message}"
    _append_check(
        ctx, view, verdict, per_key, current, basis="handle", renderOutcome=outcome, flame=outcome
    )
    return entry


def _check_by_flame(
    ctx: ServerContext, view: IntentView, entry: dict[str, Any], reason: str | None
) -> dict[str, Any]:
    """No handle (a TIMEOUT / BRIDGE_LOST carries none) or one the bridge no longer knows: read
    Flame again (the clip, `is_rendered()` as the render was asked to be judged). True = landed,
    false = not_landed; a clip that is gone, another one, or one without Timeline FX any more is
    diverged. This is an estimate -- nothing says the render has ended -- so the entry says
    `basis: "flame"`."""
    intent = view.intent
    env = observe(
        ctx,
        OP_NAME,
        {
            "target": intent.get("target") or {},
            "render_mode": intent.get("renderMode") or "All",
            PHASE_ARG: PHASE_READ,
        },
    )
    notes = [_FLAME_NOTE] if reason is None else [reason, _FLAME_NOTE]
    rendered: Any = None
    uid_now: Any = None
    if env.get("ok") is True and isinstance(env.get("result"), dict):
        facts = env["result"]
        rendered = facts.get("isRendered")
        uid_now = facts.get("uid")
        if facts.get("type") not in MEDIA_TYPES or (
            intent.get("uid") is not None and uid_now != intent.get("uid")
        ):
            verdict = STATE_DIVERGED
            notes.append("the clip at the recorded address is not the one that was rendered")
        elif not facts.get("effectCount"):
            verdict = STATE_DIVERGED
            notes.append("the clip has no Timeline FX any more")
        elif rendered is True:
            verdict = STATE_LANDED
            if intent.get("isRenderedBefore") is True:
                notes.append(_ALREADY_RENDERED_NOTE)
        else:
            verdict = STATE_NOT_LANDED
    elif env.get("errorCode") == _NOT_FOUND_CODE:
        verdict = STATE_DIVERGED
        notes.append("the clip was not found at its recorded address")
    elif isinstance(env.get("pending"), Mapping):
        unread = (
            "the read of the clip had not answered when its wait ended (the bridge gave the "
            f"pending handle {env['pending'].get('handle')}): run journal.check again later"
        )
        return _unknown_entry(entry, unread, "TIMEOUT")
    else:
        unread = f"Flame could not be read ({env.get('errorCode')}: {env.get('error')})"
        return _unknown_entry(entry, unread, env.get("errorCode"))
    per_key = {"timelineFx": verdict}
    current = {"isRendered": rendered, "uid": uid_now}
    entry.update(verdict=verdict, basis="flame", perKey=per_key, current=current)
    entry["detail"] = ". ".join(notes)
    _append_check(ctx, view, verdict, per_key, current, basis="flame")
    return entry


def check_render_clip_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """Decide what became of a `render.clip` intent that has no outcome. Nothing is written to
    the project.

    1. A journaled `polled` record, or the bridge's answer for a handle to ask with (the
       `pending` record's, else the intent's client handle): `render_check.fetch_answer`. An
       answer says the render has ended (`basis: "handle"`, `_check_with_result`). A render still
       running, or a bridge that cannot be asked, is `unknown` (the write guard stays shut:
       writes are refused until `journal.check`).
    2. Otherwise -- no handle, or one the bridge no longer knows -- Flame is read again and
       `is_rendered()` decides (`basis: "flame"`, an estimate)."""
    intent = view.intent
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    answer = fetch_answer(ctx, view, entry, OP_NAME)
    if answer.stop is not None:
        return answer.stop
    if answer.polled is not None:
        return _check_with_result(ctx, view, entry, answer.polled)
    return _check_by_flame(ctx, view, entry, answer.reason)
