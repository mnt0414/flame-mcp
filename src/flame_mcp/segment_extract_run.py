"""`segment.copy_to_media_panel` and `segment.match`, server side.

They make a new clip in a reel / folder / library out of one segment of a sequence, and nothing but
a delete undoes that, so they go through the same write-ahead engine as `desktop.duplicate`
(`desktop_run.run_write_flow`: pre-read -> intent -> compare-and-swap write -> outcome with
`created`; `journal.revert` and `journal.check` read Flame again). This module is only what is
theirs:

  write   `created` = {uid, name, type, wiretapId, selector, parent (the destination), state {name,
          duration, fingerprint}, sourceUid (the sequence)}. What a call made is the destination's
          entries that were not there before: both calls answer "a PyClip or a list of PyClips",
          so the answer is only recorded (result.returned).
  revert  deletes the clip, only while its name, duration and fingerprint (segments and effects)
          are still what they were right after the call; else CONFLICT and nothing is deleted. A
          Timeline restore of the clip (the revert of an edit of it) gives it another uid and
          records `replacedUid` -> `targetUid`, so it is looked up by the newest uid of that chain
          (`uid_chain`) that resolves (result.foundUid, result.uidChain); `alreadyGone` (ok) only
          when no uid of the chain does. The check of a revert's intent follows the same chain.
  check   an intent with no outcome: the destination's entries the intent's `existing` did not
          list -- exactly one = landed (with the `created` it found), none = not landed.

The intent has the journal kind `desktop_extract` for both ops (`op` says which). Not allowed in
plan.run (its revert deletes a clip, which a plan's group does not do).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.desktop_revert import (
    CheckVerdict,
    RevertProfile,
    check_flow,
    classify_alive,
    observe,
    observe_alive,
    revert_flow,
)
from flame_mcp.desktop_run import (
    CREATED_ARG,
    EXPECTED_ARG,
    PHASE_ARG,
    PHASE_PROBE,
    PHASE_REVERT,
    WriteProfile,
    rows_uids,
    run_write_flow,
)
from flame_mcp.journal import (
    KIND_DESKTOP_EXTRACT,
    KIND_DESKTOP_REVERT,
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    Index,
    IntentView,
    now_iso,
)
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

COPY_OP: Final[str] = "segment.copy_to_media_panel"
MATCH_OP: Final[str] = "segment.match"
EXTRACT_OPS: Final[tuple[str, ...]] = (COPY_OP, MATCH_OP)
_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"

EXTRACT_NOTE: Final[str] = (
    "A NEW clip was made in the destination (result.created.selector addresses it). journal."
    "revert(group) deletes it, only while its name, duration and content (segments and effects) "
    "are still what they were right after the call."
)
EXTRACT_REVERT_NOTE: Final[str] = (
    "The clip the call made was deleted (flame.delete). Nothing in the sequence was touched."
)


def is_extract_intent(view: IntentView) -> bool:
    """An intent of `segment.copy_to_media_panel` / `segment.match`, or the revert of one."""
    if view.desktop_kind == KIND_DESKTOP_EXTRACT:
        return True
    return (
        view.desktop_kind == KIND_DESKTOP_REVERT
        and view.intent.get("revertOf") == KIND_DESKTOP_EXTRACT
    )


def _no_refusal(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any], started: float
) -> dict[str, Any] | None:
    return None


def _stable_args(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """The destination re-addressed by wiretap id, and the segment's sequence too, so the write
    acts on what the pre-read saw even if a `selection` / `path` moved meanwhile."""
    args = dict(op_args)
    stable = (pre.get("destination") or {}).get("selector")
    if not isinstance(stable, dict):
        return args, None, "the destination has no wiretap id, so it cannot be found again"
    args["destination"] = stable
    sequence = ((pre.get("source") or {}).get("sequence") or {}).get("selector")
    if isinstance(sequence, dict) and isinstance(op_args.get("target"), dict):
        args["target"] = {**op_args["target"], "parent": sequence}
    return args, stable, None


def _intent_record(
    op: RegisteredOperation,
    op_args: dict[str, Any],
    pre: dict[str, Any],
    stable: dict[str, Any] | None,
    *,
    intent_id: str,
    group_id: str,
) -> dict[str, Any]:
    source = pre.get("source") or {}
    return {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op.name,
        "kind": KIND_DESKTOP_EXTRACT,
        "args": op_args,
        "target": stable,
        "existing": rows_uids(pre.get("existing")),
        "source": {
            "sequence": source.get("sequence"),
            "segment": source.get("segment"),
            "location": source.get("location"),
        },
    }


def created_record(op_name: str, result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the result says was made, in the form the journal keeps and `journal.revert` works
    from. None when nothing is known to have been made."""
    if not isinstance(result, Mapping):
        return None
    made = result.get("created")
    if not isinstance(made, Mapping) or not made.get("uid"):
        return None
    return {
        "uid": made.get("uid"),
        "name": made.get("name"),
        "type": made.get("type"),
        "wiretapId": made.get("wiretapId"),
        "selector": made.get("selector"),
        "parent": result.get("destination"),
        "state": result.get("state"),
        "sourceUid": ((result.get("source") or {}).get("sequence") or {}).get("uid"),
    }


def _read_back(env: Mapping[str, Any]) -> dict[str, Any] | None:
    result = env.get("result")
    if not isinstance(result, Mapping) or "returned" not in result:
        return None
    return {"returned": result["returned"], "made": result.get("created") is not None}


def _notes(op: RegisteredOperation, op_args: Mapping[str, Any], env: dict[str, Any]) -> None:
    result = env.get("result")
    if not isinstance(result, dict):
        return
    if result.get("created") is not None:
        result["notes"] = [*(result.get("notes") or []), EXTRACT_NOTE]
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warnings:
        env["degraded"] = [*env.get("degraded", []), *[f"read-back differs: {w}" for w in warnings]]


def _pre_hint(op: RegisteredOperation, pre: Mapping[str, Any]) -> str | None:
    if pre.get("errorCode") != "INVALID_ARGS":
        return None
    return (
        "A reel of a Batch group takes clips through batch.add_clips (it checkpoints the group and "
        f"checks the Clip nodes); {op.name} is for Desktop and library places. Nothing was "
        "written and nothing journaled."
    )


EXTRACT_PROFILE: Final[WriteProfile] = WriteProfile(
    refuse=_no_refusal,
    stable_args=_stable_args,
    intent=_intent_record,
    created=created_record,
    notes=_notes,
    pre_hint=_pre_hint,
    read_back=_read_back,
)


def run_extract(
    rt: Any, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `segment.copy_to_media_panel` and `segment.match`."""
    return run_write_flow(rt, op, op_args, EXTRACT_PROFILE, group=group)


def _revert_op_name(view: IntentView) -> str:
    return str(view.intent.get("op"))


def _revert_args(view: IntentView, created: dict[str, Any]) -> dict[str, Any]:
    target = view.intent.get("target") or {"kind": "selection"}
    return {
        "target": {"kind": "selection"},
        "destination": target,
        PHASE_ARG: PHASE_REVERT,
        CREATED_ARG: created,
    }


def _revert_notes(view: IntentView, result: Mapping[str, Any]) -> list[str]:
    return [EXTRACT_REVERT_NOTE] if result.get("deleted") else []


def _revert_count(view: IntentView, result: Mapping[str, Any]) -> int:
    return int(result.get("deleted") is True)


def _revert_extra(result: Mapping[str, Any]) -> dict[str, Any]:
    return {}


EXTRACT_REVERT: Final[RevertProfile] = RevertProfile(
    op_name=_revert_op_name,
    args=_revert_args,
    notes=_revert_notes,
    count=_revert_count,
    extra=_revert_extra,
)


def revert_extract_group(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView]
) -> ServerResult:
    """`journal.revert` of a group whose intent is a segment.copy_to_media_panel / match."""
    return revert_flow(ctx, index, group, views, EXTRACT_REVERT)


def _check_revert_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    """The clip the revert was to delete: alive when any uid of its chain still resolves."""
    created = view.intent.get("created") or {}
    env = observe_alive(
        ctx,
        COPY_OP,
        {"target": {"kind": "selection"}, "destination": {"kind": "selection"}},
        [{"uid": created.get("uid"), "parent": created.get("parent")}],
    )
    return classify_alive(env)


def _check_write_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    """The destination read again: its entries the intent's `existing` did not list."""
    intent = view.intent
    target = intent.get("target")
    env = observe(
        ctx,
        str(intent.get("op")),
        {
            "target": {"kind": "selection"},
            "destination": target if isinstance(target, dict) else {"kind": "selection"},
            PHASE_ARG: PHASE_PROBE,
            EXPECTED_ARG: list(intent.get("existing") or []),
            CREATED_ARG: {"withState": True, "uids": []},
        },
    )
    if env.get("ok") is not True:
        if env.get("errorCode") == _NOT_FOUND_CODE:
            return STATE_DIVERGED, "other", {"detail": "the destination was not found"}
        return VERDICT_UNKNOWN, str(env.get("errorCode")), {"error": env.get("error")}
    probe = env.get("result") or {}
    new = list(probe.get("new") or [])
    detail: dict[str, Any] = {"newEntries": new}
    if not new:
        return STATE_NOT_LANDED, "before", detail
    if len(new) != 1:
        return STATE_DIVERGED, "other", {**detail, "expected": 1}
    row = new[0]
    created: dict[str, Any] = {
        "uid": row.get("uid"),
        "name": row.get("name"),
        "type": row.get("type"),
        "wiretapId": row.get("wiretapId"),
        "selector": row.get("selector"),
        "parent": probe.get("container"),
        "state": row.get("state"),
        "sourceUid": ((intent.get("source") or {}).get("sequence") or {}).get("uid"),
    }
    return STATE_LANDED, "after", {**detail, "created": created}


def check_extract_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """`journal.check` of a segment.copy_to_media_panel / segment.match intent with no outcome."""
    return check_flow(ctx, view, _check_write_intent, _check_revert_intent)
