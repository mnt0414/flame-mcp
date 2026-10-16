"""`timeline.create_sequence`, server side.

It makes a new sequence in a reel of the Desktop, which nothing but a delete undoes, so it goes
through the same write-ahead engine as `desktop.duplicate` (`desktop_run.run_write_flow`: pre-read
-> intent -> compare-and-swap write -> outcome with `created`; `journal.revert` and
`journal.check` read Flame again). This module is only what is theirs:

  write   `created` = {uid, name, type, wiretapId, selector, parent (the reel), state {name,
          duration, fingerprint}, shownBefore (what the Timeline showed before the call)}. What a
          call made is the reel's entries that were not there before.
  revert  deletes the sequence, only while its name, duration and fingerprint are still what they
          were right after the call; else CONFLICT and nothing is deleted. A Timeline restore of
          the sequence (the revert of an edit) gives it another uid and records `replacedUid` ->
          `targetUid`, so it is looked up by the newest uid of that chain (`uid_chain`) that
          resolves; `alreadyGone` (ok) only when no uid of the chain does. When the sequence was
          the one the Timeline showed, the sequence shown before the create is opened again.
  check   an intent with no outcome: the reel's entries the intent's `existing` did not list --
          exactly one = landed (with the `created` it found), none = not landed. The check of a
          revert's intent follows the same chain.

The intent has the journal kind `desktop_sequence`. Not allowed in plan.run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.desktop_revert import (
    CheckVerdict,
    RevertProfile,
    check_flow,
    classify_alive,
    observe,
    revert_flow,
)
from flame_mcp.desktop_run import (
    CREATED_ARG,
    EXPECTED_ARG,
    PHASE_ALIVE,
    PHASE_ARG,
    PHASE_PROBE,
    PHASE_REVERT,
    WriteProfile,
    rows_uids,
    run_write_flow,
)
from flame_mcp.journal import (
    KIND_DESKTOP_REVERT,
    KIND_DESKTOP_SEQUENCE,
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    Index,
    IntentView,
    now_iso,
)
from flame_mcp.registry import RegisteredOperation
from flame_mcp.timeline_restore import uid_chain
from flame_mcp.types import ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

CREATE_SEQUENCE_OP: Final[str] = "timeline.create_sequence"
_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"

CREATED_NOTE: Final[str] = (
    "A NEW sequence was made in the reel (result.created.selector addresses it). "
    "journal.revert(group) deletes it, only while its name, duration and content are still what "
    "they were right after the call."
)
REVERT_NOTE: Final[str] = (
    "The sequence the call made was deleted (flame.delete). When it was the one the Timeline "
    "showed, the sequence shown before the call is opened again (result.reshown)."
)


def is_sequence_intent(view: IntentView) -> bool:
    """An intent of `timeline.create_sequence`, or the revert of one."""
    if view.desktop_kind == KIND_DESKTOP_SEQUENCE:
        return True
    return (
        view.desktop_kind == KIND_DESKTOP_REVERT
        and view.intent.get("revertOf") == KIND_DESKTOP_SEQUENCE
    )


def _no_refusal(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any], started: float
) -> dict[str, Any] | None:
    return None


def _stable_args(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """The reel re-addressed by wiretap id, so the write acts on what the pre-read saw even if a
    `selection` / `path` moved meanwhile."""
    args = dict(op_args)
    stable = (pre.get("parent") or {}).get("selector")
    if not isinstance(stable, dict):
        return args, None, "the reel has no wiretap id, so it cannot be found again"
    args["parent"] = stable
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
    return {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op.name,
        "kind": KIND_DESKTOP_SEQUENCE,
        "args": op_args,
        "target": stable,
        "existing": rows_uids(pre.get("existing")),
        "shownBefore": pre.get("shown"),
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
        "parent": result.get("parent"),
        "state": result.get("state"),
        "shownBefore": result.get("shownBefore"),
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
        result["notes"] = [*(result.get("notes") or []), CREATED_NOTE]
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warnings:
        env["degraded"] = [*env.get("degraded", []), *warnings]


def _pre_hint(op: RegisteredOperation, pre: Mapping[str, Any]) -> str | None:
    if pre.get("errorCode") != "INVALID_ARGS":
        return None
    return (
        "A sequence is made in a reel of the Desktop (a reel of a reel group), not in a Batch "
        "group. Nothing was written and nothing journaled."
    )


SEQUENCE_PROFILE: Final[WriteProfile] = WriteProfile(
    refuse=_no_refusal,
    stable_args=_stable_args,
    intent=_intent_record,
    created=created_record,
    notes=_notes,
    pre_hint=_pre_hint,
    read_back=_read_back,
)


def run_create_sequence(
    rt: Any, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `timeline.create_sequence`."""
    return run_write_flow(rt, op, op_args, SEQUENCE_PROFILE, group=group)


def _revert_op_name(view: IntentView) -> str:
    return CREATE_SEQUENCE_OP


def chain_of(ctx: ServerContext, created: Mapping[str, Any]) -> list[str]:
    """The uids the created sequence has had, newest first (a Timeline restore gives it another)."""
    uid = created.get("uid")
    if not isinstance(uid, str):
        return []
    return uid_chain(ctx.journal.read_all().records, uid)


def _revert_args_for(
    chain_of_created: Callable[[Mapping[str, Any]], list[str]],
) -> Callable[..., Any]:
    def revert_args(view: IntentView, created: dict[str, Any]) -> dict[str, Any]:
        target = view.intent.get("target") or {"kind": "selection"}
        marked = {**created, "uidChain": chain_of_created(created)}
        return {"parent": target, PHASE_ARG: PHASE_REVERT, CREATED_ARG: marked}

    return revert_args


def _revert_notes(view: IntentView, result: Mapping[str, Any]) -> list[str]:
    return [REVERT_NOTE] if result.get("deleted") else []


def _revert_count(view: IntentView, result: Mapping[str, Any]) -> int:
    return int(result.get("deleted") is True)


def _revert_extra(result: Mapping[str, Any]) -> dict[str, Any]:
    return {}


def _sequence_revert(ctx: ServerContext) -> RevertProfile:
    return RevertProfile(
        op_name=_revert_op_name,
        args=_revert_args_for(lambda created: chain_of(ctx, created)),
        notes=_revert_notes,
        count=_revert_count,
        extra=_revert_extra,
    )


def revert_sequence_group(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView]
) -> ServerResult:
    """`journal.revert` of a group whose intent is a timeline.create_sequence."""
    return revert_flow(ctx, index, group, views, _sequence_revert(ctx))


def _check_revert_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    """The sequence the revert was to delete: alive when any uid of its chain still resolves."""
    created = view.intent.get("created") or {}
    chain = chain_of(ctx, created) or [str(created.get("uid"))]
    env = observe(
        ctx,
        CREATE_SEQUENCE_OP,
        {
            "parent": {"kind": "selection"},
            PHASE_ARG: PHASE_ALIVE,
            CREATED_ARG: {
                "items": [{"uid": uid, "parent": created.get("parent")} for uid in chain]
            },
        },
    )
    if env.get("ok") is not True:
        return classify_alive(env)
    alive = (env.get("result") or {}).get("alive") or {}
    return classify_alive({"ok": True, "result": {"alive": {"chain": any(alive.values())}}})


def _check_write_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    """The reel read again: its entries the intent's `existing` did not list."""
    intent = view.intent
    target = intent.get("target")
    env = observe(
        ctx,
        CREATE_SEQUENCE_OP,
        {
            "parent": target if isinstance(target, dict) else {"kind": "selection"},
            PHASE_ARG: PHASE_PROBE,
            EXPECTED_ARG: list(intent.get("existing") or []),
            CREATED_ARG: {"withState": True, "uids": []},
        },
    )
    if env.get("ok") is not True:
        if env.get("errorCode") == _NOT_FOUND_CODE:
            return STATE_DIVERGED, "other", {"detail": "the reel was not found"}
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
        "shownBefore": intent.get("shownBefore"),
    }
    return STATE_LANDED, "after", {**detail, "created": created}


def check_sequence_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """`journal.check` of a timeline.create_sequence intent with no outcome."""
    return check_flow(ctx, view, _check_write_intent, _check_revert_intent)
