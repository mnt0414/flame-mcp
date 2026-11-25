"""`render.clip`, server side: the Timeline FX of one clip or sequence rendered in the Foreground.

A write-ahead pair like `render.batch` (`render_run.py`), whose shared parts it uses (the client
handle, `_unknown_end`, the result file clean-up), but the thing rendered is read from Flame, not
from the disk, and nothing in the project changes except the clip's render state:

  1. read       one Flame call (`_rc_inspect`): type, name, uid, wiretap id, place (Desktop /
                library / reel of a Batch group), the types of the Timeline FX, `is_rendered()`.
                Every refusal ends the call here, with nothing journaled: a target that is not a
                PyClip / PySequence, one in a library, one without Timeline FX, one that
                `is_rendered()` already reads true for (unless `force`).
  2. intent     `kind: "render_clip"`, with the stable selector, the arguments, `is_rendered()`
                before and the client handle (journaled before the bridge gets the call).
  3. render     one `main-async` Flame call (`_rc_render`): the uid is compared with the one read,
                `is_rendered()` before, `PyClip.render`, `is_rendered()` after. It waits up to
                `timeout_s`.
  4. outcome    judged by `judge_render_clip`: `is_rendered()` true after = ok (a render that did
                not return True is ok with a note; when it already read true before, as with
                force=true, a render that did not return True is FLAME_API_ERROR, because the
                read cannot show what the render did), false = SILENT_NOOP, an exception =
                FLAME_API_ERROR.

A call that ends without an answer (TIMEOUT / BRIDGE_LOST / a `pending` handle) leaves the intent
open and closes the write guard (`WriteGuard`; writes are refused until `journal.check` has
decided, `render_clip_check.check_render_clip_intent`): the bridge's answer for the handle
first, else `is_rendered()` read again.

There is no undo: `flush_renders()` does not make `is_rendered()` false again.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import errors
from flame_mcp.desktop_run import (
    EXPECTED_ARG,
    PHASE_WRITE,
)
from flame_mcp.journal import new_id, now_iso
from flame_mcp.journal_records import IntentRecord, OutcomeRecord
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import PHASE_ARG, ErrorCode
from flame_mcp.write_run import (
    HANDLE_SOURCE_CLIENT,
    Refused,
    RenderJudgement,
    append_intent,
    check_ascii,
    new_client_handle,
    read_facts,
    run_and_close,
    settle_envelope,
)

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

OP_NAME: Final[str] = "render.clip"
RENDER_CLIP_KIND: Final[str] = "render_clip"
"""`kind` of a render.clip intent in the journal (`journal.check` dispatches on it)."""
MEDIA_TYPES: Final[tuple[str, ...]] = ("PyClip", "PySequence")
RENDER_MODES: Final[tuple[str, ...]] = ("Top", "All")
RENDER_QUALITY: Final[str] = "Full Resolution"
"""Fixed: it goes to `render` and to `is_rendered` alike."""
UNDO_NOTE: Final[str] = "unsupported: the rendered Timeline FX stay rendered"


def target_info(facts: Mapping[str, Any], selector: Any) -> dict[str, Any]:
    return {
        "type": facts.get("type"),
        "name": facts.get("name"),
        "uid": facts.get("uid"),
        "selector": selector,
    }


def judge_render_clip(
    *, env_ok: bool, env_code: Any, env_error: Any, flame: Mapping[str, Any]
) -> RenderJudgement:
    """What a finished render of a clip counts as, from Flame's answer (`env_*`, the op's own
    value `flame`).

    The rule is the same for `render.clip` and for a `journal.check` that got the answer through
    the bridge's poll. An error of the render stays an error. `is_rendered()` not true after a
    render that raised nothing is SILENT_NOOP (FLAME_API_ERROR when it returned False, like
    `render_run.judge_render`). `is_rendered()` true with a return value other than True is a
    success with a note. If `is_rendered()` was already true before the render (force=true), the
    read cannot show that anything was rendered and the return value is the only evidence, so a
    return value other than True is FLAME_API_ERROR. A uid that changed during the render is a
    success with a note."""
    if not env_ok:
        return RenderJudgement(False, errors.as_error_code(str(env_code)), str(env_error))
    returned = flame.get("returned")
    after = flame.get("isRenderedAfter")
    if after is not True:
        code: ErrorCode = "FLAME_API_ERROR" if returned is False else "SILENT_NOOP"
        return RenderJudgement(
            False,
            code,
            f"render returned {returned!r} without an exception but is_rendered() reads "
            f"{after!r} afterwards, so the Timeline FX are not rendered",
        )
    degraded: list[str] = []
    if returned is not True and flame.get("isRenderedBefore") is True:
        return RenderJudgement(
            False,
            "FLAME_API_ERROR",
            f"render returned {returned!r}, not True, and is_rendered() already read true before "
            "the render (force=true), so reading it true afterwards cannot tell whether "
            "anything was rendered",
        )
    if returned is not True:
        degraded.append(
            f"render returned {returned!r}, not True; is_rendered() reads true afterwards"
        )
    uid_after = flame.get("uidAfter")
    if uid_after is not None and uid_after != flame.get("uid"):
        degraded.append(
            f"the clip's uid changed during the render ({flame.get('uid')!r} -> {uid_after!r}); "
            "its wiretap id may have changed too: find it again before the next call"
        )
    return RenderJudgement(True, None, None, degraded)


def _read_back_decisive(flame: Mapping[str, Any]) -> bool | None:
    """Whether `is_rendered()` before / after can show that this render did something: only when
    it read false before. None when it was not read before."""
    before = flame.get("isRenderedBefore")
    return None if before is None else before is False


def _read_back_note(flame: Mapping[str, Any]) -> str | None:
    if _read_back_decisive(flame) is not False:
        return None
    return (
        "is_rendered() already read true before the render, so the read-back cannot show whether "
        "this render did anything: only the return value of render() tells"
    )


def _problem(facts: Mapping[str, Any], force: bool) -> tuple[ErrorCode, str, str | None] | None:
    """Why the render is refused before anything is journaled, from what Flame said about the
    target; (code, message, hint), or None when it may go ahead."""
    kind = facts.get("type")
    label = f"the {kind} {facts.get('name')!r}"
    if kind not in MEDIA_TYPES:
        return (
            "SELECTOR_TYPE",
            f"nothing was rendered: the target is a {kind}, not a PyClip / PySequence",
            None,
        )
    where = facts.get("location")
    if where == "library":
        return (
            "PRECONDITION_UNMET",
            f"nothing was rendered: {label} is inside a library, and this server renders "
            "nothing there",
            "Copy it onto the Desktop with desktop.copy, then render the copy.",
        )
    if where not in ("desktop", "batch"):
        return (
            "PRECONDITION_UNMET",
            f"nothing was rendered: {label} is not found to be on the Desktop or in a reel of a "
            "Batch group (its parents could not be followed)",
            None,
        )
    if facts.get("gapBatchFx"):
        return (
            "PRECONDITION_UNMET",
            f"nothing was rendered: {label} has a Gap Batch FX segment, "
            "which this operation does not render",
            "Render it in Flame, or lift those segments from a duplicate and render the "
            "duplicate; result.gapBatchFx lists the segments.",
        )
    if not facts.get("effectCount"):
        return (
            "PRECONDITION_UNMET",
            f"nothing was rendered: {label} has no Timeline FX to render",
            "A clip without Timeline FX has nothing to render; result.effects is empty.",
        )
    if facts.get("isRendered") is True and not force:
        return (
            "PRECONDITION_UNMET",
            f"nothing was rendered: {label} is already rendered (is_rendered() reads true)",
            "Pass force=true to render it again.",
        )
    return None


def _stable_selector(facts: Mapping[str, Any], target: Mapping[str, Any]) -> Any:
    """The selector the render and `journal.check` find the clip by: its wiretap id; a path
    selector can be resolved again too, a selection cannot."""
    selector = facts.get("selector")
    if selector is not None:
        return selector
    return dict(target) if target.get("kind") == "path" else None


def _intent_record(
    op: RegisteredOperation,
    op_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    stable: Any,
    *,
    intent_id: str,
    group_id: str,
    timeout_s: float,
    handle: str | None,
) -> IntentRecord:
    return {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op.name,
        "kind": RENDER_CLIP_KIND,
        "args": dict(op_args),
        "target": stable,
        "targetType": facts.get("type"),
        "name": facts.get("name"),
        "uid": facts.get("uid"),
        "renderMode": op_args["render_mode"],
        "renderOption": "Foreground",
        "renderQuality": RENDER_QUALITY,
        "includeHandles": op_args["include_handles"],
        "force": op_args["force"],
        "isRenderedBefore": facts.get("isRendered"),
        "timeoutS": timeout_s,
        "handle": handle,
        "handleSource": HANDLE_SOURCE_CLIENT if handle is not None else None,
    }


def _outcome_record(
    intent_id: str,
    group_id: str,
    judged: RenderJudgement,
    flame: Mapping[str, Any],
) -> OutcomeRecord:
    return {
        "type": "outcome",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "ok": judged.ok,
        "applied": [],
        "failed": {} if judged.ok else {OP_NAME: judged.message or ""},
        "errorCode": judged.code,
        "readBack": None,
        "verdict": "landed"
        if judged.ok
        else ("not_landed" if judged.code == "SILENT_NOOP" else None),
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "isRenderedBefore": flame.get("isRenderedBefore"),
        "isRenderedAfter": flame.get("isRenderedAfter"),
    }


def _check_facts(
    op: RegisteredOperation, op_args: Mapping[str, Any], facts: Mapping[str, Any], started: float
) -> Any:
    """The stable selector of the target; `Refused` for a target that cannot be rendered or found
    again."""
    problem = _problem(facts, bool(op_args["force"]))
    stable = _stable_selector(facts, op_args["target"])
    shown = {
        "ran": False,
        "target": target_info(facts, stable),
        "location": facts.get("location"),
        "effects": facts.get("effects"),
        "effectCount": facts.get("effectCount"),
        "gapBatchFx": facts.get("gapBatchFx"),
        "gapBatchFxMore": facts.get("gapBatchFxMore"),
        "isRendered": facts.get("isRendered"),
    }
    if problem is not None:
        code, message, hint = problem
        raise Refused(envelope_mod.refusal(op, code, message, started, result=shown, hint=hint))
    if stable is None:
        raise Refused(
            envelope_mod.refusal(
                op,
                "PRECONDITION_UNMET",
                "nothing was rendered: the target has no wiretap id, so it could not be found "
                "again for the render and for journal.check",
                started,
                result=shown,
                hint="Give a wiretap or a path selector.",
            )
        )
    return stable


def run_render_clip(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `render.clip`."""
    started = time.monotonic()
    timeout_s = float(op_args["timeout_s"])
    try:
        facts = read_facts(rt, op, op_args, "rendered")
        stable = _check_facts(op, op_args, facts, started)
        check_ascii(
            op,
            started,
            "rendered",
            target=stable,
            uid=facts.get("uid"),
            wiretapId=facts.get("wiretapId"),
        )
        group_id = group if group is not None else new_id("g")
        intent_id = new_id("i")
        client_handle = new_client_handle(rt)
        append_intent(
            rt,
            op,
            _intent_record(
                op,
                op_args,
                facts,
                stable,
                intent_id=intent_id,
                group_id=group_id,
                timeout_s=timeout_s,
                handle=client_handle,
            ),
            started,
            done="rendered",
            action="render",
        )
    except Refused as stop:
        return stop.env
    run_args = {
        **op_args,
        "target": stable,
        PHASE_ARG: PHASE_WRITE,
        EXPECTED_ARG: facts.get("uid"),
    }
    return run_and_close(
        rt,
        op,
        run_args,
        timeout_s=timeout_s,
        handle=client_handle,
        intent_id=intent_id,
        group_id=group_id,
        finish=lambda env: _finish(
            rt, op, env, op_args, facts, stable, intent_id, group_id, started
        ),
        reads="Flame",
    )


def _clip_result(
    op_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    stable: Any,
    flame: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ran": True,
        "target": target_info(facts, stable),
        "renderMode": op_args["render_mode"],
        "renderOption": "Foreground",
        "renderQuality": RENDER_QUALITY,
        "includeHandles": op_args["include_handles"],
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "isRenderedBefore": flame.get("isRenderedBefore"),
        "isRenderedAfter": flame.get("isRenderedAfter"),
        "effects": facts.get("effects"),
        "effectCount": facts.get("effectCount"),
        "readBackDecisive": _read_back_decisive(flame),
        "readBackNote": _read_back_note(flame),
        "undo": UNDO_NOTE,
    }


def _finish(
    rt: Runtime,
    op: RegisteredOperation,
    env: dict[str, Any],
    op_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    stable: Any,
    intent_id: str,
    group_id: str,
    started: float,
) -> dict[str, Any]:
    flame_raw = env.get("result")
    flame: dict[str, Any] = flame_raw if isinstance(flame_raw, dict) else {}
    judged = judge_render_clip(
        env_ok=env.get("ok") is True,
        env_code=env.get("errorCode"),
        env_error=env.get("error"),
        flame=flame,
    )
    return settle_envelope(
        rt,
        op,
        env,
        judged,
        _outcome_record(intent_id, group_id, judged, flame),
        result=_clip_result(op_args, facts, stable, flame),
        affected=1 if judged.ok else 0,
        partial=None,
        degraded=judged.degraded,
        intent_id=intent_id,
        group_id=group_id,
        started=started,
    )
