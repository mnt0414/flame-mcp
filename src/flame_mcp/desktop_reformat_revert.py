"""`journal.revert` of a `desktop.reformat` group (`journal.check` is `desktop_reformat_check`).

A group holds one intent per clip (`desktop_reformat_run`). The revert goes through them newest
first and puts each back on its own, so a clip that cannot be put back (changed since, stash
purged) leaves the others restored and a second revert does the rest (`Index.intent_reverted`
tells which intents a revert already covered):

  in place   the Timeline restore of `timeline_restore.restore_timeline_checkpoint`: the
             reformatted clip is moved into the stash reel (so the revert can itself be undone)
             and the stashed copy is copied back into its reel, only while the clip's segments /
             effects fingerprint is still the one the call left (`force` overrides). The restored
             clip has a new uid and is at the end of its reel. That fingerprint does not see the
             clip's format, so the format of the restored clip is read back and compared with the
             one recorded before the call (`format.matches`).
  duplicate  `desktop_revert.revert_flow` with `_dm_revert_duplicate`: the copy is deleted only
             while its name, duration and fingerprint are the ones recorded after the reformat.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

from flame_mcp.checkpointed import refuse_if_denied
from flame_mcp.checkpoints import TIMELINE_KIND
from flame_mcp.desktop_reformat_check import observe_items, same_format
from flame_mcp.desktop_reformat_state import DUPLICATE, IN_PLACE, OP_NAME
from flame_mcp.desktop_revert import RevertProfile, chain_of_uid, revert_flow
from flame_mcp.desktop_run import CREATED_ARG, PHASE_ARG, PHASE_REVERT
from flame_mcp.errors import as_error_code
from flame_mcp.journal import KIND_DESKTOP_REFORMAT, Index, IntentView
from flame_mcp.timeline_restore import restore_timeline_checkpoint
from flame_mcp.types import ErrorCode, ServerOpError, ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

INDEPENDENT_CODES: Final[frozenset[str]] = frozenset(
    {"CONFLICT", "PRECONDITION_UNMET", "SELECTOR_EMPTY", "INVALID_ARGS"}
)
"""Failures that touched nothing: the other clips are still put back after one of these."""

IN_PLACE_REVERT_NOTE: Final[str] = (
    "Each clip was restored from its stashed copy: it has a NEW uid and wiretap id and is at the "
    "END of its reel (its old place is not restored); use restored[].to.selector. The "
    "reformatted clip was moved into the stash reel, so a restore can itself be undone "
    "(journal.restore_checkpoint). restored[].format says whether the restored clip has the "
    "format it had before the call."
)
DUPLICATE_REVERT_NOTE: Final[str] = (
    "The reformatted copies were deleted; the original clips were not touched."
)


def _format_check(
    ctx: ServerContext, restored: Mapping[str, Any] | None, view: IntentView, before: Any
) -> dict[str, Any]:
    """The restored clip's format against the one recorded before the call."""
    if not isinstance(restored, Mapping) or not isinstance(before, Mapping):
        return {"matches": None, "error": "the restored clip or the recorded format is unknown"}
    uid = restored.get("uid")
    source = view.intent.get("source") or {}
    rows, env = observe_items(ctx, [{"uid": uid, "uids": [uid], "parent": source.get("parent")}])
    row = (rows or {}).get(str(uid))
    if rows is None or not isinstance(row, Mapping) or not row.get("found"):
        return {
            "matches": None,
            "error": f"the restored clip could not be read back ({env.get('error')})",
        }
    found = row.get("attrs") or {}
    differ = same_format(found, before)
    return {"matches": not differ, "differs": differ, "expected": before, "found": found}


def _format_override(
    ctx: ServerContext, view: IntentView, created: Mapping[str, Any], force: bool
) -> dict[str, Any] | None:
    """Read the clip now and compare its format with the one the call left (`created["after"]`).
    A difference refuses the revert (CONFLICT: a later change would be overwritten) unless `force`;
    then it is returned, for the revert's intent to journal. None when the formats agree, or when
    the clip is not to be found (the revert itself says so) or no format was recorded."""
    left = created.get("after")
    if not isinstance(left, Mapping):
        return None
    uid = created.get("uid")
    rows, env = observe_items(
        ctx,
        [{"uid": uid, "uids": chain_of_uid(ctx, uid) or [uid], "parent": created.get("parent")}],
    )
    if rows is None:
        raise ServerOpError(
            as_error_code(str(env.get("errorCode"))),
            f"nothing was changed: the clip's format could not be read ({env.get('error')})",
        )
    found = rows.get(str(uid)) or {}
    if not found.get("found"):
        return None
    now = found.get("attrs") or {}
    differ = same_format(now, left)
    if not differ:
        return None
    if not force:
        raise ServerOpError(
            "CONFLICT",
            f"nothing was changed: the clip's format is not the one the call left "
            f"({', '.join(differ)} differ), so putting it back would overwrite a later change; "
            "pass force=true to do it anyway (that is journaled)",
            result={"refused": True, "formatLeft": dict(left), "formatNow": now, "differs": differ},
        )
    return {"formatDiffers": differ, "formatLeft": dict(left), "formatNow": now}


def _revert_in_place(
    ctx: ServerContext, index: Index, group: str, view: IntentView, force: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    created = view.created_info() or {}
    checkpoint_id = created.get("checkpoint")
    record = index.checkpoints.get(str(checkpoint_id))
    if record is None:
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"nothing was restored: the journal has no record of checkpoint {checkpoint_id}",
        )
    expected = created.get("fingerprintAfter") or view.fingerprint_after()
    overridden = _format_override(ctx, view, created, force)
    done = restore_timeline_checkpoint(
        ctx,
        record,
        uid=created.get("uid"),
        check_current=True,
        expected_current=expected,
        force=force,
        reverts=group,
        reverts_intent=view.id,
        forced_note=overridden,
    )
    body = dict(done.result) if isinstance(done.result, dict) else {}
    restored = body.get("restored")
    item = {
        "index": view.intent.get("index"),
        "from": view.intent.get("source"),
        "to": restored,
        "checkpoint": checkpoint_id,
        "format": _format_check(ctx, restored, view, created.get("before")),
        "group": body.get("group"),
        "preCheckpoint": body.get("preCheckpoint"),
    }
    return item, dict(done.journal or {})


def _duplicate_args(view: IntentView, created: dict[str, Any]) -> dict[str, Any]:
    return {"targets": [], PHASE_ARG: PHASE_REVERT, CREATED_ARG: created}


def _duplicate_count(view: IntentView, result: Mapping[str, Any]) -> int:
    return int(result.get("deleted") is True)


def _duplicate_notes(view: IntentView, result: Mapping[str, Any]) -> list[str]:
    return [DUPLICATE_REVERT_NOTE] if result.get("deleted") else []


DUPLICATE_REVERT: Final[RevertProfile] = RevertProfile(
    op_name=lambda view: OP_NAME,
    args=_duplicate_args,
    notes=_duplicate_notes,
    count=_duplicate_count,
)


def _revert_duplicate(
    ctx: ServerContext, index: Index, group: str, view: IntentView, force: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    overridden = _format_override(ctx, view, view.created_info() or {}, force)
    profile = (
        DUPLICATE_REVERT
        if overridden is None
        else replace(DUPLICATE_REVERT, intent_extra=lambda _view: {"forced": overridden})
    )
    done = revert_flow(ctx, index, group, [view], profile)
    body = dict(done.result) if isinstance(done.result, dict) else {}
    item = {
        "index": view.intent.get("index"),
        "uid": (view.created_info() or {}).get("uid"),
        "name": body.get("name"),
        "deleted": body.get("deleted"),
        "alreadyGone": body.get("alreadyGone"),
        "group": body.get("group"),
    }
    return item, dict(done.journal or {})


def _nothing_to_revert(group: str) -> ServerOpError:
    return ServerOpError(
        "PRECONDITION_UNMET",
        f"group {group} made nothing that a revert could undo (no clip changed, or no outcome yet)",
    )


def revert_reformat_group(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView], force: bool
) -> ServerResult:
    """`journal.revert` of a group whose intents are `desktop.reformat` ones. The caller holds
    `ctx.write_lock` and has checked that the group is not reverted already."""
    writes = [v for v in views if v.desktop_kind == KIND_DESKTOP_REFORMAT and v.is_revertible()]
    if not writes:
        raise _nothing_to_revert(group)
    todo = [v for v in reversed(writes) if not index.intent_reverted(v)]
    if not todo:
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"group {group}: every clip this call changed was put back already (by "
            f"{', '.join(index.revert_groups_of(group))})",
        )
    needs = (OP_NAME,) if any(_mode(v) == DUPLICATE for v in todo) else TIMELINE_KIND.needed_ops
    try:
        refuse_if_denied(ctx, needs)
    except ServerOpError as exc:
        raise ServerOpError(
            exc.code,
            exc.message.replace("nothing was run", "nothing was reverted"),
            hint=exc.hint,
        ) from exc
    restored: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    groups: list[Any] = []
    stopped_at = len(todo)
    for position, view in enumerate(todo):
        try:
            if _mode(view) == DUPLICATE:
                item, ref = _revert_duplicate(ctx, index, group, view, force)
                deleted.append(item)
            else:
                item, ref = _revert_in_place(ctx, index, group, view, force)
                restored.append(item)
            groups.append(ref.get("group"))
        except ServerOpError as exc:
            failures.append(
                {
                    "index": view.intent.get("index"),
                    "from": view.intent.get("source"),
                    "errorCode": exc.code,
                    "reason": exc.message,
                    "result": exc.result,
                }
            )
            if exc.code not in INDEPENDENT_CODES:
                stopped_at = position + 1
                break
    body = _body(group, restored, deleted, failures, todo, stopped_at)
    done = len(restored) + len(deleted)
    journal = {"group": groups[-1], "groups": groups} if groups else None
    if failures:
        codes = {str(f["errorCode"]) for f in failures}
        code: ErrorCode = failures[0]["errorCode"] if len(codes) == 1 else "CONFLICT"
        raise ServerOpError(
            code,
            "; ".join(f"clip {f['index']}: {f['reason']}" for f in failures)
            + (
                f" -- {done} clip(s) were put back, {len(failures)} were not (result.partial); "
                "a second journal.revert puts back only the rest"
                if done
                else ""
            ),
            result=body,
            affected=done,
            partial={"done": done, "failed": len(failures), "notRun": len(todo) - stopped_at}
            if done
            else None,
            journal=journal,
        )
    return ServerResult(result=body, affected=done, journal=journal)


def _mode(view: IntentView) -> str:
    created = view.created_info() or {}
    return str(created.get("mode") or view.intent.get("mode") or IN_PLACE)


def _body(
    group: str,
    restored: list[dict[str, Any]],
    deleted: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    todo: list[IntentView],
    stopped_at: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reverted": group,
        "restored": restored,
        "deleted": deleted,
        "notRestored": failures,
        "partial": bool(failures and (restored or deleted)),
        "notes": [],
        "degraded": [],
    }
    if restored:
        body["notes"].append(IN_PLACE_REVERT_NOTE)
    if deleted:
        body["notes"].append(DUPLICATE_REVERT_NOTE)
    for item in restored:
        fmt = item.get("format") or {}
        if fmt.get("matches") is False:
            name = (item.get("from") or {}).get("name")
            body["degraded"].append(
                f"the restored clip {name!r} is not the format it had before the call "
                f"({', '.join(fmt.get('differs') or [])} differ): read restored[].format"
            )
        elif fmt.get("matches") is None:
            body["degraded"].append(
                f"the format of restored clip {item.get('index')} could not be checked: "
                f"{fmt.get('error')}"
            )
    if stopped_at < len(todo):
        body["notRun"] = [v.intent.get("index") for v in todo[stopped_at:]]
    return body
