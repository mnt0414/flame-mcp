"""`desktop.reformat`: one clip at a time.

Each clip has an intent, one or two Flame calls and an outcome of its own in the call's journal
group (`desktop_reformat_run` describes the flow). A call that ends without an answer stops the
run (`Run.stop`) and leaves the intent open.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from flame_mcp import execution
from flame_mcp.checkpointed import note_unresolved
from flame_mcp.desktop_reformat_state import (
    DUPLICATE,
    IN_PLACE,
    OP_NAME,
    PHASE_DUP,
    REFORMAT_WAIT_S,
    SPEC_ARG,
    Run,
    ident,
    source_ref,
)
from flame_mcp.desktop_run import (
    EXPECTED_ARG,
    PHASE_ARG,
    PHASE_WRITE,
    WRITE_TIMEOUT_S,
)
from flame_mcp.journal import KIND_DESKTOP_REFORMAT, new_id, now_iso
from flame_mcp.journaled_write import UNKNOWN_END_CODES


def _intent_record(run: Run, index: int, row: dict[str, Any], intent_id: str) -> dict[str, Any]:
    stash = run.stashed.get(index)
    parent = row.get("parent") or {}
    return {
        "type": "intent",
        "id": intent_id,
        "group": run.group,
        "ts": now_iso(),
        "op": OP_NAME,
        "kind": KIND_DESKTOP_REFORMAT,
        "args": {"target": row.get("selector"), "duplicate": run.duplicate, **run.requested},
        "target": row.get("selector"),
        "mode": DUPLICATE if run.duplicate else IN_PLACE,
        "index": index,
        "of": len(run.rows),
        "source": source_ref(row),
        "before": row.get("attrs"),
        "requested": run.requested,
        "fingerprintBefore": row.get("fingerprint"),
        "existing": (
            list(run.siblings.get(parent.get("uid"), []))
            if run.duplicate
            else list(row.get("siblings") or [])
        ),
        "checkpoint": stash.get("checkpoint") if stash else None,
        "stash": stash.get("stash") if stash else None,
    }


def _result_of(env: Mapping[str, Any]) -> dict[str, Any]:
    raw = env.get("result")
    return dict(raw) if isinstance(raw, dict) else {}


def _unknown_end(run: Run, env: dict[str, Any], intent_id: str, index: int) -> bool:
    """Whether `env` is a call that ended without an answer; if so the run stops here, the write
    door is shut and the intent stays open."""
    code = env.get("errorCode")
    pending = env.get("pending")
    if code not in UNKNOWN_END_CODES and not (env.get("ok") is True and pending):
        return False
    shut = str(code) if code in UNKNOWN_END_CODES else "TIMEOUT"
    run.rt.guard.block(intent_id, run.group, OP_NAME, shut)
    noted = dict(env)
    if code not in UNKNOWN_END_CODES:
        noted["errorCode"] = shut
        noted["error"] = "the call was still running when the wait ended (a pending handle)"
    note_unresolved(run.rt, intent_id, run.group, noted)
    name = run.rows[index].get("name")
    run.stop = {
        "code": shut,
        "message": f"targets[{index}] ({name!r}): the Flame call ended without an answer "
        f"({env.get('error') or 'pending'})",
        "intent": intent_id,
        "index": index,
        "pending": pending,
        "hint": env.get("hint"),
    }
    run.last_env = env
    return True


def _append(run: Run, record: dict[str, Any], index: int, what: str) -> bool:
    """Append a journal record; False when the journal cannot be written (the run then stops)."""
    try:
        run.rt.journal.append(record)
    except OSError as exc:
        if record.get("type") == "outcome":
            run.rt.guard.block(
                str(record.get("id")), run.group, OP_NAME, "JOURNAL_OUTCOME_NOT_WRITTEN"
            )
            run.degraded.append(
                f"the outcome of targets[{index}] could not be journaled ({exc}); the answer "
                "stands but this server now refuses writes until journal.check has settled the "
                "intent"
            )
        run.stop = {
            "code": "POLICY_DENIED",
            "message": f"targets[{index}]: the journal cannot be written ({exc}), and this "
            f"server does not {what} without it",
            "index": index,
        }
        return False
    return True


def _ident_after(
    result: Mapping[str, Any], before: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The clip's name / type / uid / wiretap id / selector as Flame has them after the write:
    `PyClip.reformat` gives the clip a NEW uid and wiretap id (measured on Flame 2025.2.4), so
    what was read before the write finds nothing any more. `before` when the write did not read
    the clip afterwards, or read the same uid."""
    after = ident(result.get("identAfter"))
    if after is None or not isinstance(after.get("uid"), str) or after.get("selector") is None:
        return ident(before)
    return after


def _uid_link(old_uid: Any, result: Mapping[str, Any]) -> dict[str, str]:
    """`replacedUid` -> `targetUid` for the outcome when the write gave the clip another uid, so a
    later revert finds the clip through `timeline_restore.uid_chain` even after more writes."""
    new_uid = (_ident_after(result, None) or {}).get("uid")
    if isinstance(old_uid, str) and isinstance(new_uid, str) and new_uid != old_uid:
        return {"replacedUid": old_uid, "targetUid": new_uid}
    return {}


def _created_in_place(
    run: Run, index: int, row: dict[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    stash = run.stashed.get(index) or {}
    me = _ident_after(result, row) or {}
    return {
        "mode": IN_PLACE,
        "index": index,
        "checkpoint": stash.get("checkpoint"),
        "stash": stash.get("stash"),
        **me,
        "parent": row.get("parent"),
        "before": row.get("attrs"),
        "after": result.get("after"),
        "fingerprintBefore": row.get("fingerprint"),
        "fingerprintAfter": result.get("fingerprintAfter"),
    }


def _created_duplicate(
    row: dict[str, Any], index: int, made: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "mode": DUPLICATE,
        "index": index,
        **(_ident_after(result, made) or {}),
        "parent": row.get("parent"),
        "state": result.get("state"),
        "source": source_ref(row),
        "before": row.get("attrs"),
        "after": result.get("after"),
    }


def _clip_row(
    run: Run,
    index: int,
    row: dict[str, Any],
    result: Mapping[str, Any],
    *,
    made: Mapping[str, Any] | None,
    seconds: float | None,
) -> dict[str, Any]:
    stash = run.stashed.get(index)
    out: dict[str, Any] = {
        "index": index,
        "from": source_ref(row),
        "to": _ident_after(result, made if made is not None else row),
        "before": row.get("attrs"),
        "after": result.get("after"),
        "applied": result.get("applied"),
        "changed": bool(result.get("changed")),
        "skipped": bool(result.get("skipped")),
        "checkpoint": stash.get("checkpoint") if stash else None,
        "stash": stash.get("stash") if stash else None,
        "fingerprintBefore": result.get("fingerprintBefore"),
        "fingerprintAfter": result.get("fingerprintAfter"),
        "seconds": round(seconds, 3) if seconds is not None else None,
        "flameSeconds": {
            "reformat": result.get("reformatSeconds"),
            "changeStartFrame": result.get("startFrameSeconds"),
        },
        "warnings": list(result.get("warnings") or []),
    }
    return out


def _file_result(
    run: Run,
    index: int,
    row: dict[str, Any],
    env: dict[str, Any],
    result: Mapping[str, Any],
    *,
    made: Mapping[str, Any] | None,
    seconds: float | None,
) -> None:
    clip = _clip_row(run, index, row, result, made=made, seconds=seconds)
    name = row.get("name")
    for warning in clip["warnings"]:
        run.warnings.append(f"{name}: {warning}")
    if env.get("ok") is True:
        run.reformatted.append(clip)
    else:
        clip["errorCode"] = env.get("errorCode")
        clip["reason"] = env.get("error")
        if made is not None:
            clip["reason"] = (
                f"{env.get('error')} (the duplicate remains; journal.revert deletes it)"
            )
        run.failed.append(clip)
    if clip["changed"] or made is not None:
        run.changed += 1


def _outcome_record(
    run: Run,
    intent_id: str,
    env: dict[str, Any],
    result: Mapping[str, Any],
    created: dict[str, Any] | None,
    old_uid: Any = None,
) -> dict[str, Any]:
    ok = env.get("ok") is True
    return {
        **_uid_link(old_uid, result),
        "type": "outcome",
        "id": intent_id,
        "group": run.group,
        "ts": now_iso(),
        "ok": ok,
        "applied": [],
        "failed": {} if ok else {OP_NAME: str(env.get("error"))},
        "errorCode": None if ok else env.get("errorCode"),
        "readBack": {"before": result.get("before"), "after": result.get("after")},
        "created": created,
        "fingerprintAfter": result.get("fingerprintAfter"),
    }


def _write_args(target: Mapping[str, Any], spec: dict[str, Any], expected: Any) -> dict[str, Any]:
    return {PHASE_ARG: PHASE_WRITE, "target": dict(target), SPEC_ARG: spec, EXPECTED_ARG: expected}


def _fail_unanswered_row(
    run: Run, index: int, row: dict[str, Any], env: dict[str, Any], *, made: Any = None
) -> None:
    run.failed.append(
        {
            "index": index,
            "from": source_ref(row),
            "to": ident(made) if made is not None else None,
            "errorCode": run.stop["code"] if run.stop else env.get("errorCode"),
            "reason": run.stop["message"] if run.stop else env.get("error"),
            "before": row.get("attrs"),
            "after": None,
            "unanswered": True,
        }
    )


def clip_in_place(run: Run, index: int, row: dict[str, Any]) -> bool:
    """Reformat one clip in place; False when the run must stop."""
    intent_id = new_id("i")
    if not _append(run, _intent_record(run, index, row, intent_id), index, "reformat"):
        return False
    run.intent_ids.append(intent_id)
    expected = {"fingerprint": row.get("fingerprint"), "attrs": row.get("attrs")}
    began = time.monotonic()
    env = execution.run_op(
        run.op,
        _write_args(row["selector"], run.spec, expected),
        run_dir=run.rt.run_dir,
        timeout_s=REFORMAT_WAIT_S,
    )
    seconds = time.monotonic() - began
    if _unknown_end(run, env, intent_id, index):
        _fail_unanswered_row(run, index, row, env)
        return False
    run.last_env = env
    result = _result_of(env)
    changed = bool(result.get("changed"))
    created = _created_in_place(run, index, row, result) if changed else None
    run.created_any = run.created_any or created is not None
    _file_result(run, index, row, env, result, made=None, seconds=seconds)
    outcome = _outcome_record(run, intent_id, env, result, created, row.get("uid"))
    return _append(run, outcome, index, "go on")


def _expected_siblings(run: Run, row: dict[str, Any]) -> list[Any]:
    parent_uid = (row.get("parent") or {}).get("uid")
    return run.siblings.setdefault(parent_uid, list(row.get("siblings") or []))


def clip_duplicate(run: Run, index: int, row: dict[str, Any]) -> bool:
    """Duplicate one clip and reformat the copy; False when the run must stop."""
    expected = _expected_siblings(run, row)
    intent_id = new_id("i")
    if not _append(run, _intent_record(run, index, row, intent_id), index, "duplicate"):
        return False
    run.intent_ids.append(intent_id)
    began = time.monotonic()
    dup_env = execution.run_op(
        run.op,
        {PHASE_ARG: PHASE_DUP, "target": row["selector"], EXPECTED_ARG: list(expected)},
        run_dir=run.rt.run_dir,
        timeout_s=WRITE_TIMEOUT_S,
    )
    if _unknown_end(run, dup_env, intent_id, index):
        _fail_unanswered_row(run, index, row, dup_env)
        return False
    run.last_env = dup_env
    dup = _result_of(dup_env)
    made = dup.get("duplicate") if dup_env.get("ok") is True else None
    if not isinstance(made, dict) or not isinstance(made.get("selector"), dict):
        result = {"before": row.get("attrs"), "after": None, "changed": False}
        failed_env = dict(dup_env)
        if dup_env.get("ok") is True:
            failed_env.update(
                ok=False, errorCode="FLAME_API_ERROR", error="the duplicate was not found"
            )
        _file_result(
            run, index, row, failed_env, result, made=None, seconds=time.monotonic() - began
        )
        return _append(
            run, _outcome_record(run, intent_id, failed_env, result, None), index, "go on"
        )
    write_env = execution.run_op(
        run.op,
        _write_args(made["selector"], run.spec, None),
        run_dir=run.rt.run_dir,
        timeout_s=REFORMAT_WAIT_S,
    )
    seconds = time.monotonic() - began
    if _unknown_end(run, write_env, intent_id, index):
        _fail_unanswered_row(run, index, row, write_env, made=made)
        return False
    run.last_env = write_env
    result = _result_of(write_env)
    if result.get("state") is None:
        result["state"] = dup.get("state")
    created = _created_duplicate(row, index, made, result)
    expected.append(created.get("uid"))
    run.created_any = True
    result["changed"] = bool(result.get("changed"))
    _file_result(run, index, row, write_env, result, made=made, seconds=seconds)
    outcome = _outcome_record(run, intent_id, write_env, result, created, made.get("uid"))
    return _append(run, outcome, index, "go on")
