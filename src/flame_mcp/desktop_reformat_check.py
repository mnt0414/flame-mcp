"""`journal.check` of a `desktop.reformat` intent with no outcome (or of the revert of one).

It reads the clip's format again: as asked = landed (the check carries the `created` it found, so
the group can be reverted), as before = not landed, anything else = diverged (the detail names the
stashed copy, which `journal.restore_checkpoint` puts back by hand). A duplicate call is looked
for beside the clip: no new entry = not landed, one that is reformatted as asked = landed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.desktop_reformat_state import (
    DUPLICATE,
    IN_PLACE,
    OP_NAME,
    PHASE_OBSERVE,
    RATIO_TOLERANCE,
)
from flame_mcp.desktop_revert import (
    CheckVerdict,
    check_flow,
    classify_alive,
    observe,
    observe_alive,
)
from flame_mcp.desktop_run import CREATED_ARG, EXPECTED_ARG, PHASE_ARG, PHASE_PROBE
from flame_mcp.journal import (
    KIND_DESKTOP_REFORMAT,
    KIND_DESKTOP_REVERT,
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    IntentView,
)
from flame_mcp.timeline_restore import uid_chain

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

FORMAT_KEYS: Final[tuple[str, ...]] = (
    "width",
    "height",
    "bitDepth",
    "ratio",
    "scanMode",
    "frameRate",
    "startFrame",
    "duration",
)


def is_reformat_intent(view: IntentView) -> bool:
    """An intent of `desktop.reformat`, or the revert of one."""
    if view.desktop_kind == KIND_DESKTOP_REFORMAT:
        return True
    return (
        view.desktop_kind == KIND_DESKTOP_REVERT
        and view.intent.get("revertOf") == KIND_DESKTOP_REFORMAT
    )


def same_format(found: Mapping[str, Any], wanted: Mapping[str, Any]) -> list[str]:
    """The keys of `wanted` (a recorded format) that `found` does not have; a ratio within
    `RATIO_TOLERANCE` is the same."""
    differ = []
    for key in FORMAT_KEYS:
        if key not in wanted:
            continue
        a, b = found.get(key), wanted.get(key)
        if key == "ratio" and isinstance(a, (int, float)) and isinstance(b, (int, float)):
            same = abs(a - b) <= RATIO_TOLERANCE
        else:
            same = a == b
        if not same:
            differ.append(key)
    return differ


def matches_request(found: Mapping[str, Any], requested: Mapping[str, Any]) -> bool:
    asked = {
        "width": requested.get("width"),
        "height": requested.get("height"),
        "ratio": requested.get("ratio"),
        "bitDepth": requested.get("bit_depth"),
        "scanMode": requested.get("scan_mode"),
        "frameRate": requested.get("frame_rate"),
        "startFrame": requested.get("start_frame"),
    }
    return not same_format(found, {k: v for k, v in asked.items() if v is not None})


def observe_items(
    ctx: ServerContext, items: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The Flame read of `items` ({uid, uids, parent}): (the rows by uid, the failed envelope)."""
    env = observe(
        ctx, OP_NAME, {"targets": [], PHASE_ARG: PHASE_OBSERVE, CREATED_ARG: {"items": items}}
    )
    if env.get("ok") is not True:
        return None, env
    raw = (env.get("result") or {}).get("items")
    return (raw if isinstance(raw, dict) else {}), env


def _chain(ctx: ServerContext, uid: Any) -> list[str]:
    return uid_chain(ctx.journal.read_all().records, uid) if isinstance(uid, str) else []


def _created_in_place(
    view: IntentView,
    found: Mapping[str, Any],
    attrs: Mapping[str, Any],
    now: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    intent = view.intent
    source = intent.get("source") or {}
    here = now or source
    return {
        "mode": IN_PLACE,
        "index": intent.get("index"),
        "checkpoint": intent.get("checkpoint"),
        "stash": intent.get("stash"),
        "name": source.get("name"),
        "type": source.get("type"),
        "uid": here.get("uid"),
        "wiretapId": here.get("wiretapId"),
        "selector": here.get("selector"),
        "parent": source.get("parent"),
        "before": intent.get("before"),
        "after": dict(attrs),
        "fingerprintBefore": intent.get("fingerprintBefore"),
        "fingerprintAfter": found.get("fingerprint"),
    }


def _stash_advice(view: IntentView) -> str:
    return (
        f"the stashed copy is checkpoint {view.intent.get('checkpoint')}: "
        "journal.restore_checkpoint(checkpoint=<that id>) puts it back by hand"
    )


def _moved_clip(ctx: ServerContext, intent: Mapping[str, Any]) -> dict[str, Any] | None:
    """`PyClip.reformat` gives the clip a new uid, and a write that ended without an answer left
    no outcome that says which: the one entry of the clip's reel that was not there when the call
    began and has the clip's name. None when there is not exactly one."""
    source = intent.get("source") or {}
    parent = source.get("parent") or {}
    env = observe(
        ctx,
        OP_NAME,
        {
            "target": parent.get("selector"),
            PHASE_ARG: PHASE_PROBE,
            EXPECTED_ARG: list(intent.get("existing") or []),
            CREATED_ARG: {"withState": False},
        },
    )
    if env.get("ok") is not True:
        return None
    new = [
        r
        for r in (env.get("result") or {}).get("new") or []
        if isinstance(r, dict) and r.get("name") == source.get("name")
    ]
    return new[0] if len(new) == 1 else None


def _check_in_place(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    intent = view.intent
    source = intent.get("source") or {}
    uid = source.get("uid")
    rows, env = observe_items(
        ctx, [{"uid": uid, "uids": _chain(ctx, uid) or [uid], "parent": source.get("parent")}]
    )
    if rows is None:
        return VERDICT_UNKNOWN, str(env.get("errorCode")), {"error": env.get("error")}
    found = rows.get(str(uid)) or {}
    now: dict[str, Any] | None = None
    if not found.get("found"):
        now = _moved_clip(ctx, intent)
        if now is not None:
            rows, env = observe_items(
                ctx,
                [{"uid": now.get("uid"), "uids": [now.get("uid")], "parent": source.get("parent")}],
            )
            found = (rows or {}).get(str(now.get("uid"))) or {}
    if not found.get("found"):
        return (
            STATE_DIVERGED,
            "other",
            {"detail": f"the clip is gone from its recorded place; {_stash_advice(view)}"},
        )
    attrs = found.get("attrs") or {}
    asked = matches_request(attrs, intent.get("requested") or {})
    unchanged = not same_format(attrs, intent.get("before") or {})
    if asked:
        extra: dict[str, Any] = {"format": attrs}
        if not unchanged:
            extra["created"] = _created_in_place(view, found, attrs, now)
        return STATE_LANDED, "after", extra
    if unchanged:
        return STATE_NOT_LANDED, "before", {"format": attrs}
    return (
        STATE_DIVERGED,
        "other",
        {
            "format": attrs,
            "detail": "the clip's format is neither the one before the call nor the one asked "
            f"for; {_stash_advice(view)}",
        },
    )


def _check_duplicate(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    intent = view.intent
    parent = (intent.get("source") or {}).get("parent") or {}
    env = observe(
        ctx,
        OP_NAME,
        {
            "target": parent.get("selector"),
            PHASE_ARG: PHASE_PROBE,
            EXPECTED_ARG: list(intent.get("existing") or []),
            CREATED_ARG: {"withState": True},
        },
    )
    if env.get("ok") is not True:
        return VERDICT_UNKNOWN, str(env.get("errorCode")), {"error": env.get("error")}
    new = [r for r in (env.get("result") or {}).get("new") or [] if isinstance(r, dict)]
    if not new:
        return STATE_NOT_LANDED, "before", {"newEntries": []}
    if len(new) != 1:
        return (
            STATE_DIVERGED,
            "other",
            {
                "newEntries": new,
                "detail": f"{len(new)} new entries appeared beside the clip; none is recorded as "
                "made, look for them by name",
            },
        )
    made = new[0]
    uid = made.get("uid")
    rows, read = observe_items(ctx, [{"uid": uid, "uids": [uid], "parent": parent}])
    if rows is None:
        return VERDICT_UNKNOWN, str(read.get("errorCode")), {"error": read.get("error")}
    found = rows.get(str(uid)) or {}
    attrs = found.get("attrs") or {}
    if not matches_request(attrs, intent.get("requested") or {}):
        return (
            STATE_DIVERGED,
            "other",
            {
                "newEntries": [made],
                "format": attrs,
                "detail": "the duplicate was made but is not reformatted as asked (uid "
                f"{uid}): reformat it with desktop.reformat, or delete it by hand",
            },
        )
    source = intent.get("source") or {}
    created = {
        "mode": DUPLICATE,
        "index": intent.get("index"),
        "uid": uid,
        "name": made.get("name"),
        "type": made.get("type"),
        "wiretapId": made.get("wiretapId"),
        "selector": made.get("selector"),
        "parent": source.get("parent"),
        "state": found.get("state"),
        "source": source,
        "before": intent.get("before"),
        "after": dict(attrs),
    }
    return STATE_LANDED, "after", {"newEntries": [made], "created": created}


def _check_write_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    if view.intent.get("mode") == DUPLICATE:
        return _check_duplicate(ctx, view)
    return _check_in_place(ctx, view)


def _check_revert_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    """The revert of a duplicate call that ended without an answer: is the copy still there?"""
    created = view.intent.get("created") or {}
    env = observe_alive(
        ctx,
        OP_NAME,
        {"targets": []},
        [{"uid": created.get("uid"), "parent": created.get("parent")}],
    )
    return classify_alive(env)


def check_reformat_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """`journal.check` of a `desktop.reformat` intent with no outcome (or of the revert of one)."""
    return check_flow(ctx, view, _check_write_intent, _check_revert_intent)
