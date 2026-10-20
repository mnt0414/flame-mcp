"""`desktop.reformat`, server side.

A write-ahead run like `desktop.delete` (`desktop_delete_run`), except that the write is one Flame
call per clip, each with an intent and an outcome of its own in one journal group:

  1. pre-read   one Flame call reads every target (`_rf_snapshot`): type, place, uid, wiretap id,
                parent, fingerprint, format (width, height, bit depth, ratio, scan mode, frame
                rate, start frame). Every refusal that needs no write ends the call here: a target
                that is not a clip on the Desktop, one listed twice, a ratio that differs from the
                clip's (`allow_aspect_change`). Nothing is copied, journaled or written before.
  2. stash      (in place) every clip that needs a write is copied into the stash reel of the
                checkpoint library, `STASH_BATCH` clips to a Flame call, and each copy is
                recorded as a timeline `checkpoint` (`desktop_delete_run.stash_record`) as soon as
                its call answers. A copy that fails, or whose format is not the clip's, ends the
                call before any clip is touched.
  3. per clip   intent -> one Flame call (`_rf_write`: compare-and-swap, `PyClip.reformat`,
                `PyClip.change_start_frame`, read back) -> outcome with `created`. With
                duplicate=true the call before it is `flame.duplicate` (`_dm_duplicate`).
                A call waits up to `REFORMAT_WAIT_S`, and the clips are not sent together.
  4. answer     `reformatted` / `notReformatted` per clip; some of each = ok with `partial`.

A call that ends without an answer (TIMEOUT / BRIDGE_LOST / BRIDGE_UNAVAILABLE, or a `pending`
handle: the thread is `main-async`) stops the run at that clip, leaves its intent open and shuts
the write door (`WriteGuard`) until `journal.check` has decided (`desktop_reformat_revert`).
No clip after it is started.

`created` of an outcome is what `journal.revert` works from:
  in place   {mode: "in_place", checkpoint, stash, uid, name, type, wiretapId, selector, parent,
              before, after, fingerprintBefore, fingerprintAfter}: set when the clip's format
              changed (also when the call failed after changing it)
  duplicate  {mode: "duplicate", uid, name, type, wiretapId, selector, parent, state, source,
              before, after}: set as soon as the duplicate exists
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.desktop_delete_run import PHASE_STASH, stash_record
from flame_mcp.desktop_reformat_answer import finish
from flame_mcp.desktop_reformat_check import same_format
from flame_mcp.desktop_reformat_clips import clip_duplicate, clip_in_place
from flame_mcp.desktop_reformat_state import (
    OP_NAME,
    RATIO_TOLERANCE,
    REFORMAT_WAIT_S,
    STASH_BATCH,
    Run,
    build_spec,
    elapsed_ms,
    requested_of,
)
from flame_mcp.desktop_run import PHASE_ARG, PHASE_READ
from flame_mcp.journal import new_id
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.library_open import merge_into_envelope
from flame_mcp.registry import RegisteredOperation
from flame_mcp.timeline_checkpoints import (
    LIBRARY_ARG,
    REEL_ARG,
    checkpoint_library_name,
    stash_reel_name,
)
from flame_mcp.types import ErrorCode

__all__ = ["OP_NAME", "run_reformat"]


def _refuse(
    op: RegisteredOperation,
    code: ErrorCode,
    message: str,
    started: float,
    *,
    result: Any = None,
    hint: str | None = None,
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=False,
        result=result,
        error=message,
        error_code=code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=elapsed_ms(started),
        hint_override=hint,
    )


def aspect_rows(rows: list[dict[str, Any]], width: int, height: int) -> list[dict[str, Any]]:
    requested = width / height
    out = []
    for index, row in enumerate(rows):
        ratio = (row.get("attrs") or {}).get("ratio")
        matches = (
            isinstance(ratio, (int, float))
            and not isinstance(ratio, bool)
            and round(abs(ratio - requested), 6) <= RATIO_TOLERANCE
        )
        out.append(
            {
                "index": index,
                "name": row.get("name"),
                "uid": row.get("uid"),
                "clipRatio": ratio,
                "requestedRatio": round(requested, 6),
                "matches": bool(matches),
            }
        )
    return out


def _aspect_refusal(
    op: RegisteredOperation, op_args: Mapping[str, Any], rows: list[dict[str, Any]], started: float
) -> dict[str, Any] | None:
    if op_args.get("allow_aspect_change", False):
        return None
    width, height = int(op_args["width"]), int(op_args["height"])
    table = aspect_rows(rows, width, height)
    wrong = [r for r in table if not r["matches"]]
    if not wrong:
        return None
    listed = ", ".join(
        f"targets[{r['index']}] {r['name']!r} has ratio {r['clipRatio']}" for r in wrong
    )
    return _refuse(
        op,
        "PRECONDITION_UNMET",
        f"nothing was written: the requested {width}x{height} has ratio "
        f"{round(width / height, 6)}, which differs by more than {RATIO_TOLERANCE} from the "
        f"current ratio of {len(wrong)} clip(s): {listed}. Pass allow_aspect_change=true to "
        "reformat them to another aspect ratio",
        started,
        result={"ran": False, "aspect": table},
        hint=(
            "Nothing was copied, written or journaled. Pick a width and height with the clips' "
            "ratio, or pass allow_aspect_change=true."
        ),
    )


def _fail_pre_read(op: RegisteredOperation, pre_env: dict[str, Any]) -> dict[str, Any]:
    failure = dict(pre_env)
    failure["execution"] = envelope_mod.execution_meta(op)
    failure["error"] = f"nothing was written: {pre_env.get('error')}"
    failure["ok"] = False
    if not failure.get("errorCode"):
        failure["errorCode"] = "FLAME_API_ERROR"
    return failure


def _stash_failure(
    run: Run, message: str, code: str | None, env: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    failure: dict[str, Any] = dict(env) if env is not None else {}
    failure.update(
        ok=False,
        error=message,
        errorCode=code or "FLAME_API_ERROR",
        execution=envelope_mod.execution_meta(run.op),
    )
    return failure


def _stash_batch(run: Run, indexes: list[int]) -> dict[str, Any] | None:
    """Copy the clips at `indexes` into the stash in one Flame call and record the copies; the
    envelope that ends the call (nothing is reformatted) or None."""
    rt, op = run.rt, run.op
    args = {
        **run.op_args,
        "targets": [run.rows[i]["selector"] for i in indexes],
        PHASE_ARG: PHASE_STASH,
        LIBRARY_ARG: checkpoint_library_name(),
        REEL_ARG: stash_reel_name(),
    }
    env = execution.run_op(op, args, run_dir=rt.run_dir, timeout_s=REFORMAT_WAIT_S)
    raw = env.get("result")
    value: dict[str, Any] = raw if isinstance(raw, dict) else {}
    stashes = [s for s in value.get("stashes") or [] if isinstance(s, dict)]
    ids: list[str] = []
    try:
        for stash in stashes:
            ids.append(stash_record(rt, op, run.group, stash))
    except OSError as exc:
        return _stash_failure(
            run,
            f"nothing was reformatted: the journal cannot be written ({exc}), and this server "
            "does not reformat without the stashed copy on record",
            "POLICY_DENIED",
        )
    for stash, cp in zip(stashes, ids, strict=True):
        run.stashed[indexes[int(stash["index"])]] = {**stash, "checkpoint": cp}
    unanswered = env.get("errorCode") in UNKNOWN_END_CODES or (
        env.get("ok") is True and env.get("pending")
    )
    if unanswered:
        return _stash_failure(
            run,
            "nothing was reformatted: the call that copies the clips into the stash ended without "
            "an answer, so the copies it made are not on record; journal.purge_checkpoints lists "
            f"them as unknown ({env.get('error') or 'pending'})",
            str(env.get("errorCode") or "TIMEOUT"),
            env,
        )
    if env.get("ok") is not True or len(stashes) != len(indexes):
        return _stash_failure(
            run,
            f"nothing was reformatted: the copy that makes the reformat revertible failed "
            f"({env.get('error')}); {len(ids)} copy(ies) of this call are recorded as "
            f"checkpoint(s) {ids}",
            str(env.get("errorCode") or ""),
            env,
        )
    for stash in stashes:
        differ = same_format(stash.get("stashAttrs") or {}, stash.get("sourceAttrs") or {})
        if stash.get("stashAttrs") is None or differ:
            name = (stash.get("source") or {}).get("name")
            return _stash_failure(
                run,
                f"nothing was reformatted: the stashed copy of {name!r} does not have the "
                f"format of the clip ({', '.join(differ) or 'it could not be read'}), so it "
                "could not put the clip back as it was; the copy is recorded as checkpoint "
                f"{run.stashed[indexes[int(stash['index'])]]['checkpoint']}",
                "FLAME_API_ERROR",
            )
    return None


def _stash_all(run: Run) -> dict[str, Any] | None:
    """Copy the clips that need a write into the stash, `STASH_BATCH` to a Flame call, and record
    the copies. A failure ends the call before any clip is touched; the copies made before it stay
    recorded as checkpoints."""
    need = [i for i, row in enumerate(run.rows) if row.get("needsWrite")]
    for start in range(0, len(need), STASH_BATCH):
        failure = _stash_batch(run, need[start : start + STASH_BATCH])
        if failure is not None:
            return failure
    return None


def run_reformat(
    rt: Any, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `desktop.reformat`."""
    started = time.monotonic()
    with rt.write_lock:
        denial = rt.guard.denial()
        if denial is not None:  # this call queued behind a write that ended unknown
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=elapsed_ms(started),
                hint=denial.hint,
            )
        pre_env = execution.run_op(op, {**op_args, PHASE_ARG: PHASE_READ}, run_dir=rt.run_dir)
        if pre_env.get("ok") is not True or not isinstance(pre_env.get("result"), dict):
            return _fail_pre_read(op, pre_env)
        pre: dict[str, Any] = pre_env["result"]
        rows = [r for r in pre.get("sources") or [] if isinstance(r, dict)]
        wanted = len(op_args["targets"])
        if len(rows) != wanted:
            answer = f"the read answered for {len(rows)} of {wanted} targets"
            return _fail_pre_read(
                op, {**pre_env, "ok": False, "errorCode": "FLAME_API_ERROR", "error": answer}
            )
        refused = _aspect_refusal(op, op_args, rows, started)
        if refused is not None:
            merge_into_envelope(refused, pre)
            return refused
        spec = build_spec(op_args)
        run = Run(
            rt=rt,
            op=op,
            op_args=op_args,
            group=group if group is not None else new_id("g"),
            rows=rows,
            spec=spec,
            requested=requested_of(spec),
            duplicate=bool(op_args.get("duplicate", False)),
        )
        if not run.duplicate:
            stashed = _stash_all(run)
            if stashed is not None:
                merge_into_envelope(stashed, pre)
                return stashed
        for index, row in enumerate(rows):
            run.next_index = index
            clip = clip_duplicate if run.duplicate else clip_in_place
            if not clip(run, index, row):
                break
        env = finish(run, started)
        merge_into_envelope(env, pre)
        return env
