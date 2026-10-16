"""`desktop.delete`, server side.

It goes through the same write-ahead engine as `desktop.move` (`desktop_run.run_write_flow`:
pre-read -> intent -> compare-and-swap write -> outcome with `created`), with one step more in
front of the intent (`WriteProfile.prepare`): everything about to be deleted is copied into the
stash reel of the checkpoint library (`desktop_delete_prelude._dd_stash`, the copy's fingerprint
must be the original's) and each copy is recorded as a timeline `checkpoint`
(`timeline_checkpoints`), so that `journal.purge_checkpoints` knows what the stash holds and the
revert knows where to copy from.

  write   `created` = {deletes: [{from: {parent, name, type, uid}, checkpoint, stash, fingerprint,
          segmentCount, wasDisplayed}]}; what could not be deleted is in result.notDeleted.
  revert  copies each stash back into the original parent (a new uid, the end of the reel). A thing
          whose parent is gone (or whose stash was purged / changed) stays a CONFLICT and the others
          are put back (`partial`); a second revert puts back only what the first one did not.
  check   an intent with no outcome: the sources looked up by their old uid -- all gone = landed,
          all there = not landed, anything else = diverged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.checkpoints import CHECKPOINT_TIMEOUT_S
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
    PHASE_ALIVE,
    PHASE_ARG,
    PHASE_REVERT,
    WriteProfile,
    rows_uids,
    run_write_flow,
)
from flame_mcp.journal import (
    KIND_DESKTOP_DELETE,
    KIND_DESKTOP_REVERT,
    STATE_DIVERGED,
    STATE_LANDED,
    VERDICT_UNKNOWN,
    Index,
    IntentView,
    new_id,
    now_iso,
)
from flame_mcp.registry import RegisteredOperation
from flame_mcp.timeline_checkpoints import (
    LIBRARY_ARG,
    REEL_ARG,
    checkpoint_library_name,
    checkpoint_record,
    stash_reel_name,
)
from flame_mcp.types import ServerOpError, ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

DELETE_OP: Final[str] = "desktop.delete"
SNAPSHOT_ARG: Final[str] = "_snapshot"
PHASE_STASH: Final[str] = "stash"
"""Reserved args key / phase only this module puts into the op's args (a caller cannot send them:
`validate_op_args` rejects an unknown key)."""

DELETE_NOTE: Final[str] = (
    "Each thing was copied into the stash reel before it was deleted (result.deleted[].stash, the "
    "checkpoint id in result.deleted[].checkpoint). journal.revert(group) copies it back into its "
    "old parent: it then has a NEW uid and wiretap id and is at the END of the reel (its old place "
    "is not restored). The stashed copies stay in the checkpoint library until "
    "journal.purge_checkpoints removes them."
)
DELETE_REVERT_NOTE: Final[str] = (
    "The things were copied back into their old parents from their stashed copies: each has a NEW "
    "uid and wiretap id and is at the END of its reel (their old place is not restored); use "
    "reverted[].to.selector. The stashed copies are still in the checkpoint library."
)


def is_delete_intent(view: IntentView) -> bool:
    """An intent of `desktop.delete`, or the revert of one."""
    if view.desktop_kind == KIND_DESKTOP_DELETE:
        return True
    return (
        view.desktop_kind == KIND_DESKTOP_REVERT
        and view.intent.get("revertOf") == KIND_DESKTOP_DELETE
    )


def _no_refusal(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any], started: float
) -> dict[str, Any] | None:
    return None


def _refuse(
    op: RegisteredOperation, code: Any, message: str, *, result: Any = None, hint: str | None = None
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=False,
        result=result,
        error=message,
        error_code=code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=0,
        hint_override=hint,
    )


def _stash_record(rt: Any, op: RegisteredOperation, group_id: str, stash: Mapping[str, Any]) -> str:
    """Journal one stashed copy as a timeline checkpoint; its id. Raises OSError."""
    checkpoint_id = new_id("c")
    rt.journal.append(
        checkpoint_record(
            checkpoint_id,
            group=group_id,
            label=f"before {op.name}",
            source=stash.get("source"),
            stash=stash.get("stash"),
            fingerprint=stash.get("fingerprint"),
            segment_count=stash.get("segmentCount"),
            displayed=stash.get("displayed"),
            effectCount=stash.get("effectCount"),
            display=stash.get("display"),
            by=op.name,
        )
    )
    return checkpoint_id


def _prepare(
    rt: Any, op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any], group_id: str
) -> dict[str, Any] | None:
    """Copy every target into the stash and record the copies as checkpoints (nothing is deleted
    yet). A copy that failed ends the call; the copies made before it stay recorded."""
    rows = pre.get("sources") or []
    args = dict(op_args)
    args["targets"] = [
        row.get("selector") if isinstance(row.get("selector"), dict) else original
        for original, row in zip(op_args["targets"], rows, strict=True)
    ]
    args.update({PHASE_ARG: PHASE_STASH, LIBRARY_ARG: checkpoint_library_name()})
    args[REEL_ARG] = stash_reel_name()
    env = execution.run_op(op, args, run_dir=rt.run_dir, timeout_s=CHECKPOINT_TIMEOUT_S)
    raw = env.get("result")
    value: dict[str, Any] = raw if isinstance(raw, dict) else {}
    stashes = [s for s in value.get("stashes") or [] if isinstance(s, dict)]
    ids: list[str] = []
    try:
        for stash in stashes:
            ids.append(_stash_record(rt, op, group_id, stash))
    except OSError as exc:
        return _refuse(
            op,
            "POLICY_DENIED",
            f"nothing was deleted: the journal cannot be written ({exc}), and this server does "
            "not delete without the stashed copy on record",
            result={"ran": False, "stashed": len(stashes)},
            hint=f"Make {rt.journal.directory} writable, then retry. The copies already made "
            "are in the checkpoint library (journal.purge_checkpoints lists them as unknown).",
        )
    if env.get("ok") is not True or len(stashes) != len(rows):
        failure = dict(env)
        failure["execution"] = envelope_mod.execution_meta(op)
        failure["error"] = (
            f"nothing was deleted: the copy that makes the delete revertible failed "
            f"({env.get('error')}); {len(ids)} copy(ies) made before it are recorded as "
            f"checkpoint(s) {ids}"
        )
        failure["ok"] = False
        if not failure.get("errorCode"):
            failure["errorCode"] = "FLAME_API_ERROR"
        return failure
    pre["stashed"] = [{**stash, "checkpoint": cp} for stash, cp in zip(stashes, ids, strict=True)]
    return None


def _stable_args(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """Every target re-addressed by wiretap id, and what the write checks it against (the
    fingerprint read first) and records (where the stashed copy is)."""
    args = dict(op_args)
    rows = pre.get("sources") or []
    stashed = pre.get("stashed") or []
    args["targets"] = [
        row.get("selector") if isinstance(row.get("selector"), dict) else original
        for original, row in zip(op_args["targets"], rows, strict=True)
    ]
    args[SNAPSHOT_ARG] = {
        row.get("uid"): {
            "fingerprint": row.get("fingerprint"),
            "segmentCount": row.get("segmentCount"),
            "checkpoint": st.get("checkpoint"),
            "stash": st.get("stash"),
        }
        for row, st in zip(rows, stashed, strict=True)
    }
    return args, None, None


def _source_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "uid": row.get("uid"),
        "name": row.get("name"),
        "type": row.get("type"),
        "wiretapId": row.get("wiretapId"),
        "selector": row.get("selector"),
        "parent": row.get("parent"),
        "fingerprint": row.get("fingerprint"),
        "segmentCount": row.get("segmentCount"),
        "displayed": row.get("displayed"),
    }


def _intent_record(
    op: RegisteredOperation,
    op_args: dict[str, Any],
    pre: dict[str, Any],
    stable: dict[str, Any] | None,
    *,
    intent_id: str,
    group_id: str,
) -> dict[str, Any]:
    stashed = pre.get("stashed") or []
    return {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op.name,
        "kind": KIND_DESKTOP_DELETE,
        "args": op_args,
        "target": stable,
        "existing": rows_uids(pre.get("existing")),
        "sources": [_source_row(s) for s in pre.get("sources") or []],
        "stashes": [
            {
                "checkpoint": st.get("checkpoint"),
                "uid": (st.get("source") or {}).get("uid"),
                "stash": st.get("stash"),
            }
            for st in stashed
        ],
        "checkpoints": [st.get("checkpoint") for st in stashed],
        "shown": pre.get("shown"),
    }


def created_record(op_name: str, result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a `desktop.delete` result says was deleted, in the form the journal keeps and
    `journal.revert` works from. None when nothing is known to have been deleted."""
    if not isinstance(result, Mapping):
        return None
    deletes = [d for d in result.get("deleted") or [] if isinstance(d, dict)]
    return {"deletes": deletes} if deletes else None


def _read_back(env: Mapping[str, Any]) -> dict[str, Any] | None:
    result = env.get("result")
    if not isinstance(result, Mapping) or "requested" not in result:
        return None
    return {"requested": result["requested"], "deleted": len(result.get("deleted") or [])}


def _notes(op: RegisteredOperation, op_args: Mapping[str, Any], env: dict[str, Any]) -> None:
    result = env.get("result")
    if not isinstance(result, dict):
        return
    if result.get("deleted"):
        result["notes"] = [*(result.get("notes") or []), DELETE_NOTE]
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warnings:
        env["degraded"] = [*env.get("degraded", []), *[f"read-back differs: {w}" for w in warnings]]


def _pre_hint(op: RegisteredOperation, pre: Mapping[str, Any]) -> str | None:
    if pre.get("errorCode") == "INVALID_ARGS":
        return (
            "A reel of a Batch group is not deleted from here. Nothing was written and nothing "
            "journaled."
        )
    return None


DELETE_PROFILE: Final[WriteProfile] = WriteProfile(
    refuse=_no_refusal,
    stable_args=_stable_args,
    intent=_intent_record,
    created=created_record,
    notes=_notes,
    pre_hint=_pre_hint,
    read_back=_read_back,
    prepare=_prepare,
)


def run_delete(
    rt: Any, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `desktop.delete`."""
    return run_write_flow(rt, op, op_args, DELETE_PROFILE, group=group)


def _revert_args(view: IntentView, created: dict[str, Any]) -> dict[str, Any]:
    return {"targets": [], PHASE_ARG: PHASE_REVERT, CREATED_ARG: created}


def _revert_notes(view: IntentView, result: Mapping[str, Any]) -> list[str]:
    return [DELETE_REVERT_NOTE] if result.get("reverted") else []


def _revert_count(view: IntentView, result: Mapping[str, Any]) -> int:
    return len(result.get("reverted") or [])


def _revert_extra(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "restored": [
            {"checkpoint": r.get("checkpoint"), "stash": r.get("stash"), "to": r.get("to")}
            for r in result.get("reverted") or []
        ],
        "notRestored": result.get("notReverted") or [],
    }


def _revert_partial(result: Mapping[str, Any]) -> Mapping[str, int] | None:
    done, failed = len(result.get("reverted") or []), len(result.get("notReverted") or [])
    return {"done": done, "failed": failed, "notRun": 0} if done and failed else None


DELETE_REVERT: Final[RevertProfile] = RevertProfile(
    op_name=lambda view: DELETE_OP,
    args=_revert_args,
    notes=_revert_notes,
    count=_revert_count,
    extra=_revert_extra,
    partial=_revert_partial,
)


def _restored_checkpoints(index: Index, group: str) -> set[str]:
    """The checkpoints (stashed copies) an earlier revert of `group` already copied back."""
    done: set[str] = set()
    for other in index.revert_groups_of(group):
        for view in index.groups.get(other, []):
            restored = (view.outcome or {}).get("restored")
            for row in restored if isinstance(restored, list) else []:
                if isinstance(row, dict) and isinstance(row.get("checkpoint"), str):
                    done.add(row["checkpoint"])
    return done


def revert_delete_group(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView]
) -> ServerResult:
    """`journal.revert` of a group whose intent is a `desktop.delete`. What an earlier, partial
    revert put back is not put back again."""
    done = _restored_checkpoints(index, group)
    if done and len(views) == 1:
        view = views[0]
        created = view.created_info()
        if created is not None:
            rest = [d for d in created.get("deletes") or [] if d.get("checkpoint") not in done]
            if not rest:
                raise ServerOpError(
                    "PRECONDITION_UNMET",
                    f"group {group}: every thing this delete removed was put back already (by "
                    f"{', '.join(index.revert_groups_of(group))})",
                )
            outcome = {**(view.outcome or {}), "created": {**created, "deletes": rest}}
            views = [
                IntentView(
                    intent=view.intent,
                    outcome=outcome,
                    unresolved=view.unresolved,
                    checks=view.checks,
                )
            ]
    return revert_flow(ctx, index, group, views, DELETE_REVERT)


def _alive_args(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"targets": [], PHASE_ARG: PHASE_ALIVE, CREATED_ARG: {"items": items}}


def _created_from_intent(view: IntentView) -> dict[str, Any]:
    intent = view.intent
    stashes = {s.get("uid"): s for s in intent.get("stashes") or [] if isinstance(s, dict)}
    deletes = []
    for source in intent.get("sources") or []:
        stash = stashes.get(source.get("uid")) or {}
        deletes.append(
            {
                "from": {
                    "parent": source.get("parent"),
                    "name": source.get("name"),
                    "type": source.get("type"),
                    "uid": source.get("uid"),
                },
                "checkpoint": stash.get("checkpoint"),
                "stash": stash.get("stash"),
                "fingerprint": source.get("fingerprint"),
                "segmentCount": source.get("segmentCount"),
                "wasDisplayed": source.get("displayed"),
            }
        )
    return {"deletes": deletes}


def _check_write_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    sources = [s for s in view.intent.get("sources") or [] if isinstance(s, dict)]
    items = [{"uid": s.get("uid"), "parent": s.get("parent")} for s in sources]
    env = observe(ctx, DELETE_OP, _alive_args(items))
    verdict, finding, extra = classify_alive(env)
    if verdict == VERDICT_UNKNOWN:
        return verdict, finding, extra
    if verdict == STATE_DIVERGED:
        extra = {
            **extra,
            "detail": "some of the things are gone and some are not; the stashed copies are in "
            f"checkpoints {view.intent.get('checkpoints')}",
        }
        return verdict, finding, extra
    if verdict == STATE_LANDED:
        extra = {**extra, "created": _created_from_intent(view)}
    return verdict, finding, extra


def _check_revert_intent(ctx: ServerContext, view: IntentView) -> CheckVerdict:
    return (
        STATE_DIVERGED,
        "other",
        {
            "detail": "a revert that ended without an answer: the things it copies back have "
            "new uids that nothing recorded, so what it did cannot be told by uid; look for "
            "them (by name) in their old parents. The stashed copies are still in the "
            "checkpoint library."
        },
    )


def check_delete_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """`journal.check` of a `desktop.delete` intent with no outcome (or of the revert of one)."""
    return check_flow(ctx, view, _check_write_intent, _check_revert_intent)
