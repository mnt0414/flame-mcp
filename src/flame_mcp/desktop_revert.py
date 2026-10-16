"""`journal.revert` and `journal.check` of a desktop write, the shared engine.

`revert_flow` writes the revert's own journal group (`kind: desktop_revert`, `reverts` = the
original group) around one Flame call, `check_flow` decides an intent that has no outcome by
reading Flame again. What differs between the desktop writes is a `RevertProfile` / a pair of check
functions each of `desktop_run` (create / copy), `desktop_media_run`, `desktop_delete_run` and
`segment_extract_run` brings.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import execution
from flame_mcp.checkpointed import note_unresolved, refuse_if_denied
from flame_mcp.checkpoints import batch_target, take_checkpoint
from flame_mcp.desktop_run import (
    COPY_OP,
    CREATED_ARG,
    CURRENT_DELETED_NOTE,
    CURRENT_NOT_RESTORED_NOTE,
    LAST_REEL_NOTE,
    PHASE_ALIVE,
    PHASE_ARG,
    PHASE_READ,
    PHASE_REVERT,
    PHASE_REVERT_PLAN,
    WRITE_TIMEOUT_S,
    create_op_name,
)
from flame_mcp.errors import as_error_code
from flame_mcp.journal import (
    KIND_DESKTOP_COPY,
    KIND_DESKTOP_CREATE,
    KIND_DESKTOP_REVERT,
    STATE_DIVERGED,
    STATE_LANDED,
    STATE_NOT_LANDED,
    VERDICT_UNKNOWN,
    Index,
    IntentView,
    new_id,
    now_iso,
)
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.library_open import merge_library_reports
from flame_mcp.timeline_restore import uid_chain
from flame_mcp.types import ServerOpError, ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext


def chain_of_uid(ctx: ServerContext, uid: Any) -> list[str]:
    """The uids a made thing has had, newest first: a Timeline restore (the revert of an edit of
    a clip / sequence) gives it another uid and records `replacedUid` -> `targetUid`
    (`uid_chain`). A uid that is not a string has no chain."""
    return uid_chain(ctx.journal.read_all().records, uid) if isinstance(uid, str) else []


def with_chains(ctx: ServerContext, created: Mapping[str, Any]) -> dict[str, Any]:
    """`created` (what the journal kept of a write) with `uidChain` added to the made thing and to
    every entry of `copies` / `imports`, so the Flame code of a revert looks each one up by the
    newest uid of its chain that resolves. The journal's own record is not changed."""
    marked: dict[str, Any] = dict(created)
    if isinstance(created.get("uid"), str):
        marked["uidChain"] = chain_of_uid(ctx, created["uid"])
    for key in ("copies", "imports"):
        entries = created.get(key)
        if isinstance(entries, list):
            marked[key] = [
                {**e, "uidChain": chain_of_uid(ctx, e.get("uid"))} if isinstance(e, Mapping) else e
                for e in entries
            ]
    return marked


def observe_alive(
    ctx: ServerContext, op_name: str, args: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, Any]:
    """The op's `alive` phase for `items` ({uid, parent?}), each looked up by every uid of its
    chain: an item is alive when any uid of its chain resolves. The answer's `alive` is keyed by
    the item's own (first) uid."""
    chains = [
        (item, chain_of_uid(ctx, item.get("uid")) or [str(item.get("uid"))]) for item in items
    ]
    expanded = [{**item, "uid": uid} for item, chain in chains for uid in chain]
    env = observe(ctx, op_name, {**args, PHASE_ARG: PHASE_ALIVE, CREATED_ARG: {"items": expanded}})
    if env.get("ok") is not True:
        return env
    result = dict(env.get("result") or {})
    alive = result.get("alive") or {}
    result["alive"] = {
        str(item.get("uid")): any(bool(alive.get(uid)) for uid in chain) for item, chain in chains
    }
    return {**env, "result": result}


def _revert_op_name(view: IntentView) -> str:
    if view.desktop_kind == KIND_DESKTOP_COPY:
        return COPY_OP
    return create_op_name(view.intent.get("createKind"))


def _revert_args(view: IntentView, created: dict[str, Any]) -> dict[str, Any]:
    if view.desktop_kind == KIND_DESKTOP_COPY:
        return {
            "sources": [],
            "destination": view.intent.get("target") or {"kind": "selection"},
            PHASE_ARG: PHASE_REVERT,
            CREATED_ARG: created,
        }
    return {
        "kind": view.intent.get("createKind"),
        "name": created.get("name") or "",
        PHASE_ARG: PHASE_REVERT,
        CREATED_ARG: created,
    }


def _revert_notes(view: IntentView, result: Mapping[str, Any]) -> list[str]:
    notes: list[str] = []
    if view.desktop_kind == KIND_DESKTOP_CREATE:
        kind = view.intent.get("createKind")
        if kind == "batch_group" and result.get("deleted"):
            notes.append(CURRENT_NOT_RESTORED_NOTE)
        before = result.get("parentReelsBefore")
        if (
            kind == "reel"
            and result.get("deleted")
            and isinstance(before, list)
            and len(before) == 1
        ):
            notes.append(LAST_REEL_NOTE)
    elif view.desktop_kind == KIND_DESKTOP_COPY and result.get("currentChanged"):
        notes.append(CURRENT_DELETED_NOTE)
    return notes


def _revert_count(view: IntentView, result: Mapping[str, Any]) -> int:
    if view.desktop_kind == KIND_DESKTOP_COPY:
        return len(result.get("deleted") or [])
    return int(result.get("deleted") is True)


def _revert_no_extra(result: Mapping[str, Any]) -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class PreRevert:
    """What a revert does on the server before its Flame call: `failure` is the envelope that ends
    the revert (nothing was deleted), `result` is merged into the answer of a revert that ran."""

    failure: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    libraries: dict[str, Any] = field(default_factory=dict)
    """`librariesOpened` / `notes` of the Flame call that planned the revert (merged into the
    answer: `library_open.merge_library_reports`)."""


def _restore_hint(name: Any, path: Any) -> str:
    return (
        f"desktop.create(kind='batch_group', name={name!r}), then batch.load_setup(target="
        f"{{kind: 'batch', uid: <the new group's uid>}}, path={path!r})"
    )


def pre_revert_copy(
    ctx: ServerContext, view: IntentView, created: dict[str, Any], new_group: str
) -> PreRevert | None:
    """Before the revert of a copy of a Batch group deletes a group of the Desktop (a library
    group that was restored), its setup is saved as a checkpoint (`batch.checkpoint`: the
    journal's checkpoint directory and record). Flame is first asked what the revert would delete
    (`PHASE_REVERT_PLAN`, which also refuses a changed group: CONFLICT); a setup that cannot be
    saved ends the revert with nothing deleted."""
    if view.desktop_kind != KIND_DESKTOP_COPY:
        return None
    if not any(c.get("type") == "PyBatch" for c in created.get("copies") or []):
        return None
    env = observe(
        ctx,
        COPY_OP,
        {
            "sources": [],
            "destination": view.intent.get("target") or {"kind": "selection"},
            PHASE_ARG: PHASE_REVERT_PLAN,
            CREATED_ARG: created,
        },
    )
    if env.get("ok") is not True:
        return PreRevert(failure=env)
    saved: list[dict[str, Any]] = []
    for item in (env.get("result") or {}).get("plan") or []:
        if not item.get("onDesktop"):
            continue
        name = item.get("name")
        checkpoint = take_checkpoint(
            ctx,
            batch_target(str(item.get("foundUid"))),
            label=f"before the revert of {view.group}: {name}",
            group=new_group,
        )
        result = checkpoint.get("result")
        path = result.get("path") if isinstance(result, dict) else None
        if checkpoint.get("ok") is not True or not isinstance(path, str) or not path:
            return PreRevert(
                failure={
                    "ok": False,
                    "errorCode": checkpoint.get("errorCode") or "PRECONDITION_UNMET",
                    "error": (
                        f"nothing was deleted: the setup of Batch group {name!r} could not be "
                        f"saved first ({checkpoint.get('error')})"
                    ),
                    "result": {"deleted": [], "skipped": [], "savedSetups": saved},
                    "hint": "Fix what stopped the checkpoint, then revert again.",
                }
            )
        saved.append(
            {
                "name": name,
                "uid": item.get("foundUid"),
                "checkpoint": (result or {}).get("checkpoint"),
                "path": path,
                "restoreWith": _restore_hint(name, path),
            }
        )
    return PreRevert(
        result={"savedSetups": saved} if saved else {},
        libraries=dict(env.get("result") or {}),
    )


@dataclass(frozen=True)
class RevertProfile:
    """What differs between the desktop reverts that share `revert_flow`: which op's Flame code
    does the work (and with which arguments), what the answer says to the caller, how many things
    it touched, what else the revert's outcome records."""

    op_name: Callable[[IntentView], str]
    args: Callable[[IntentView, dict[str, Any]], dict[str, Any]]
    notes: Callable[[IntentView, Mapping[str, Any]], list[str]]
    count: Callable[[IntentView, Mapping[str, Any]], int]
    extra: Callable[[Mapping[str, Any]], dict[str, Any]] = _revert_no_extra
    """Fields added to the revert's outcome record (beside `deleted` / `skipped`)."""
    partial: Callable[[Mapping[str, Any]], Mapping[str, int] | None] | None = None
    """The envelope's `partial` of a revert that failed after it had put some things back."""
    pre_revert: (
        Callable[[ServerContext, IntentView, dict[str, Any], str], PreRevert | None] | None
    ) = None
    """Server-side work before the Flame call (`pre_revert_copy`); it may end the revert."""


DEFAULT_REVERT: Final[RevertProfile] = RevertProfile(
    op_name=_revert_op_name,
    args=_revert_args,
    notes=_revert_notes,
    count=_revert_count,
    pre_revert=pre_revert_copy,
)


def revert_flow(
    ctx: ServerContext,
    index: Index,
    group: str,
    views: list[IntentView],
    profile: RevertProfile,
) -> ServerResult:
    """`journal.revert` of a group whose intent is a desktop write. The caller holds
    `ctx.write_lock` and has checked that the group is not reverted already."""
    if len(views) != 1 or not views[0].is_revertible():
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"group {group} made nothing that a revert could undo (no outcome yet, or the call "
            "made nothing)",
        )
    view = views[0]
    created = view.created_info()
    assert created is not None  # is_revertible()
    op_name = profile.op_name(view)
    op = ctx.registry.get(op_name)
    if op is None:
        raise ServerOpError("UNKNOWN_OPERATION", f"'{op_name}' is not registered")
    try:
        refuse_if_denied(ctx, (op_name,))
    except ServerOpError as exc:
        raise ServerOpError(
            exc.code,
            exc.message.replace("nothing was run", "nothing was reverted"),
            hint=exc.hint,
        ) from exc

    new_group = new_id("g")
    intent_id = new_id("i")
    try:
        ctx.journal.append(
            {
                "type": "intent",
                "id": intent_id,
                "group": new_group,
                "ts": now_iso(),
                "op": "journal.revert",
                "kind": KIND_DESKTOP_REVERT,
                "args": {"group": group},
                "target": view.intent.get("target"),
                "reverts": group,
                "revertsIntent": view.id,
                "revertOf": view.desktop_kind,
                "createKind": view.intent.get("createKind"),
                "created": created,
            }
        )
    except OSError as exc:
        raise ServerOpError(
            "POLICY_DENIED",
            f"nothing was reverted: the journal cannot be written ({exc}), and this server does "
            "not change the project without an intent on record",
            hint=f"Make {ctx.journal.directory} writable, then retry.",
        ) from exc

    marked = with_chains(ctx, created)
    pre = (
        profile.pre_revert(ctx, view, marked, new_group) if profile.pre_revert is not None else None
    )
    if pre is not None and pre.failure is not None:
        env = pre.failure
    else:
        env = execution.run_op(
            op, profile.args(view, marked), run_dir=ctx.run_dir, timeout_s=WRITE_TIMEOUT_S
        )
    end_code = env.get("errorCode")
    journal_ref = {"group": new_group}
    if end_code in UNKNOWN_END_CODES:
        ctx.guard.block(intent_id, new_group, "journal.revert", str(end_code))
        note_unresolved(ctx, intent_id, new_group, env)
        raise ServerOpError(
            as_error_code(str(end_code)),
            f"the revert ended without an answer ({env.get('error')}): it is not known whether "
            f"it landed; writes are refused until journal.check (group={new_group!r}) has read "
            "Flame again",
            journal=journal_ref,
        )
    ok = env.get("ok") is True
    raw = env.get("result")
    result: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    touched = profile.count(view, result)
    outcome: dict[str, Any] = {
        "type": "outcome",
        "id": intent_id,
        "group": new_group,
        "ts": now_iso(),
        "ok": ok,
        "applied": [],
        "failed": {} if ok else {"revert": str(env.get("error"))},
        "errorCode": None if ok else end_code,
        "readBack": None,
        "deleted": result.get("deleted"),
        "skipped": result.get("skipped"),
        **profile.extra(result),
    }
    try:
        ctx.journal.append(outcome)
    except OSError as exc:
        ctx.guard.block(intent_id, new_group, "journal.revert", "JOURNAL_OUTCOME_NOT_WRITTEN")
        result["degraded"] = [
            f"the outcome could not be journaled ({exc}); this server now refuses writes until "
            "journal.check has settled the intent"
        ]
    if not ok:
        raise ServerOpError(
            as_error_code(str(end_code)),
            f"the revert of {view.id} failed: {env.get('error')}",
            result={"reverts": group, "group": new_group, **result},
            hint=env.get("hint"),
            affected=touched,
            partial=profile.partial(result) if profile.partial is not None else None,
            journal=journal_ref,
        )
    result = {"reverted": group, "group": new_group, **result, **(pre.result if pre else {})}
    if pre is not None:
        merge_library_reports(result, pre.libraries)
    notes = profile.notes(view, result)
    if notes:
        result["notes"] = [*(result.get("notes") or []), *notes]
    return ServerResult(result=result, affected=touched, journal=journal_ref)


def revert_desktop_group(
    ctx: ServerContext, index: Index, group: str, views: list[IntentView]
) -> ServerResult:
    """`journal.revert` of a group whose intent is a `desktop.create` / `desktop.copy`."""
    return revert_flow(ctx, index, group, views, DEFAULT_REVERT)


_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"


def observe(ctx: ServerContext, op_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run the op's Flame code in a read-only phase (its own; nothing is written). Not gated by
    the policy: it only reads, like `journal.check`."""
    op = ctx.registry.get(op_name)
    if op is None:
        return {"ok": False, "errorCode": "UNKNOWN_OPERATION", "error": f"'{op_name}' is missing"}
    return execution.run_op(op, args, run_dir=ctx.run_dir)


def _check_revert_intent(ctx: ServerContext, view: IntentView) -> tuple[str, str, dict[str, Any]]:
    intent = view.intent
    created = intent.get("created") or {}
    if intent.get("revertOf") == KIND_DESKTOP_COPY:
        env = observe_alive(
            ctx,
            COPY_OP,
            {"sources": [], "destination": {"kind": "selection"}},
            [
                {"uid": c.get("uid"), "parent": created.get("destination")}
                for c in created.get("copies") or []
            ],
        )
    else:
        env = observe_alive(
            ctx,
            create_op_name(intent.get("createKind")),
            {"kind": intent.get("createKind"), "name": ""},
            [{"uid": created.get("uid"), "parent": created.get("parent")}],
        )
    return classify_alive(env)


def classify_alive(env: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if env.get("ok") is not True:
        return VERDICT_UNKNOWN, str(env.get("errorCode")), {"error": env.get("error")}
    alive = (env.get("result") or {}).get("alive") or {}
    living = [uid for uid, flag in alive.items() if flag]
    if not living:
        return STATE_LANDED, "after", {"alive": alive}
    if len(living) == len(alive):
        return STATE_NOT_LANDED, "before", {"alive": alive}
    return STATE_DIVERGED, "other", {"alive": alive}


def _name_base(name: str) -> str:
    """A name without its trailing number and the space before it. Flame renames a taken name two
    ways (measured): `X` -> `X 1`, and `X1` -> `X2` (the number goes up)."""
    return re.sub(r"\s?\d+$", "", name)


def _name_mismatch(
    intent: Mapping[str, Any], args: Mapping[str, Any], new_rows: list[dict[str, Any]]
) -> str | None:
    """Why the new entries are not what the intent meant to make, by name (a library: the
    requested name, or with allow_rename a renamed form of it (same base: `_name_base`); a Batch
    group copy: the source's name), None when they are, or when the intent is not of a kind
    checked by name."""
    names = [row.get("name") for row in new_rows]
    if intent.get("createKind") == "library":
        wanted = str(args.get("name") or "")
        base = _name_base(wanted)
        renamed_ok = bool(args.get("allow_rename"))
        if all(
            isinstance(n, str) and (n == wanted or (renamed_ok and _name_base(n) == base))
            for n in names
        ):
            return None
        if not renamed_ok:
            return f"the new library is named {names!r}, not {wanted!r} (a rename was not allowed)"
        return (
            f"the new library is named {names!r}, not {wanted!r} or a renamed form of it "
            f"(`{base} <number>` or the same name with its number counted up)"
        )
    sources = [s for s in intent.get("sources") or [] if isinstance(s, Mapping)]
    if sources and all(s.get("type") == "PyBatch" for s in sources):
        wanted_names = sorted(str(s.get("name")) for s in sources)
        if sorted(str(n) for n in names) == wanted_names:
            return None
        return f"the new Batch group is named {names!r}, not {wanted_names!r}"
    return None


def _check_write_intent(ctx: ServerContext, view: IntentView) -> tuple[str, str, dict[str, Any]]:
    """What a create / copy with no outcome made: the entries of the parent / destination that
    the intent's `existing` did not list."""
    intent = view.intent
    args = intent.get("args") or {}
    is_create = view.desktop_kind == KIND_DESKTOP_CREATE
    target = intent.get("target")
    if is_create:
        read_args: dict[str, Any] = {
            "kind": intent.get("createKind"),
            "name": args.get("name") or "",
            PHASE_ARG: PHASE_READ,
        }
        if isinstance(target, dict):
            read_args["parent"] = target
        env = observe(ctx, create_op_name(intent.get("createKind")), read_args)
    else:
        batch = any(s.get("type") == "PyBatch" for s in intent.get("sources") or [])
        env = observe(
            ctx,
            COPY_OP,
            {
                "sources": [],
                "destination": target if isinstance(target, dict) else {"kind": "selection"},
                PHASE_ARG: PHASE_READ,
                CREATED_ARG: {"known": list(intent.get("existing") or []), "batch": batch},
            },
        )
    if env.get("ok") is not True:
        if env.get("errorCode") == _NOT_FOUND_CODE:
            return STATE_DIVERGED, "other", {"detail": "the parent / destination was not found"}
        return VERDICT_UNKNOWN, str(env.get("errorCode")), {"error": env.get("error")}
    result = env.get("result") or {}
    before = set(intent.get("existing") or [])
    rows = result.get("existing") or []
    new_rows = [row for row in rows if row.get("uid") not in before]
    expected = 1 if is_create else len(args.get("sources") or [])
    if not new_rows:
        return STATE_NOT_LANDED, "before", {"newEntries": []}
    if len(new_rows) != expected:
        return STATE_DIVERGED, "other", {"newEntries": new_rows, "expected": expected}
    mismatch = _name_mismatch(intent, args, new_rows)
    if mismatch is not None:
        return STATE_DIVERGED, "other", {"newEntries": new_rows, "detail": mismatch}
    if is_create:
        row = new_rows[0]
        created: dict[str, Any] = {
            "kind": intent.get("createKind"),
            "uid": row.get("uid"),
            "name": row.get("name"),
            "parent": result.get("parent"),
            "state": None,
            "previousCurrent": intent.get("previousCurrent"),
        }
    else:
        created = {
            "destination": result.get("destination"),
            "copies": [
                {
                    "uid": r.get("uid"),
                    "name": r.get("name"),
                    "state": r.get("state"),
                    **({"type": r["type"]} if r.get("type") == "PyBatch" else {}),
                }
                for r in new_rows
            ],
        }
    return STATE_LANDED, "after", {"newEntries": new_rows, "created": created}


CheckVerdict = tuple[str, str, dict[str, Any]]
"""(verdict, the per-key finding, extra fields): what `check_flow` decides from."""


def check_flow(
    ctx: ServerContext,
    view: IntentView,
    write_check: Callable[[ServerContext, IntentView], CheckVerdict],
    revert_check: Callable[[ServerContext, IntentView], CheckVerdict],
) -> dict[str, Any]:
    """`journal.check` of a desktop intent with no outcome: read Flame again and decide. Writes
    nothing to the project; the finding is appended to the journal and lifts the write door."""
    intent = view.intent
    entry: dict[str, Any] = {"group": view.group, "id": view.id, "op": intent.get("op")}
    if view.desktop_kind == KIND_DESKTOP_REVERT:
        verdict, finding, extra = revert_check(ctx, view)
    else:
        verdict, finding, extra = write_check(ctx, view)
    if verdict == VERDICT_UNKNOWN:
        entry.update(
            verdict=VERDICT_UNKNOWN,
            detail=f"Flame could not be read ({finding}: {extra.get('error')})",
        )
        return entry
    per_key = {"desktop": finding}
    current = {k: v for k, v in extra.items() if k != "created"}
    record: dict[str, Any] = {
        "type": "check",
        "id": view.id,
        "group": view.group,
        "ts": now_iso(),
        "verdict": verdict,
        "perKey": per_key,
        "current": current,
        "target": intent.get("target"),
    }
    if "created" in extra:
        record["created"] = extra["created"]
    ctx.journal.append(record)
    ctx.guard.release(view.id)
    entry.update(verdict=verdict, perKey=per_key, current=current)
    if "created" in extra:
        entry["created"] = extra["created"]
    return entry


def check_desktop_intent(ctx: ServerContext, view: IntentView) -> dict[str, Any]:
    """`journal.check` of a `desktop.create` / `desktop.copy` intent with no outcome."""
    return check_flow(ctx, view, _check_write_intent, _check_revert_intent)
