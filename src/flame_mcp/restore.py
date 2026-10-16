"""Restoring a Batch group from a checkpoint (`journal.restore_checkpoint`, and the revert of an
eval run / a restore through `journal.revert`). `checkpoint` + `load_setup` may roll back
automatically -- it deletes nothing and keeps the group's identity -- but only under these
rules, each one a measured trap:

  1. the checkpoint's file must exist, checked on the server first and again inside Flame
     right before the call. A `load_setup` that fails empties the group (measured on Flame
     2025.2.4, twice, with a path that did not exist), so it is never called on a path that is
     not there;
  2. a new checkpoint of the group's current state is taken right before the load, so the
     restore can itself be undone. Its id and path are in every result, failures included;
  3. `load_setup`'s return value and a read-back of the group afterwards are checked: the
     restore succeeded only if the group's fingerprint now equals the checkpoint's.

The restore is journaled as an ordinary intent / outcome pair (op `journal.restore_checkpoint`)
whose intent names `revertCheckpoint` = the pre-restore checkpoint, so `journal.revert` can
undo it the same way it undoes an eval run.

3'. `load_setup` does not restore the reel layout (measured on Flame 2025.2.4: empty reels are
     gone afterwards, four empty default reels have been added, reels that hold clips are rebuilt
     with new uids): a restore of a checkpoint that holds a reel inventory tidies the reels right
     after the load, in the same Flame call (`reel_fidelity`): the recorded empty reels that are
     missing are created, then the empty reels the record lacks are deleted (never one that holds
     a clip or a sequence), then the group's all-section fingerprint is compared with the
     recorded one: `reelsRestored`, `reelTidy` in the result. The tidy is an after-the-fact
     correction: whatever happens in it, the nodes stay as `load_setup` brought them. A
     checkpoint without an inventory is not tidied, and the result says so.

3''. `load_setup` rebuilds each reel from the Clip nodes in the order they are written in the
     `.batch` (measured on Flame 2025.2.4, twice: writing the nodes in reverse order reversed
     the loaded reel, renumbering them did not), which can differ from the order the reel held
     its clips. A restore of a checkpoint with a reel inventory first asks `clip_order.prepare`
     for a copy of the checkpoint (the `.batch` and its `<base>/` directory, in a directory of
     its own below `<journal>/checkpoints/ordered/`) whose Clip nodes are written in the
     recorded order, loads that copy instead, and removes it afterwards, success or failure --
     except when the load ended without an answer: Flame may still be reading it, so it stays
     (`clipOrder.copyKept`, `copyPath`, `copyKeptReason`). The checkpoint itself is never
     written; a preparation that fails loads the checkpoint as it is. `clipOrder` is in the
     result: needed / reordered / skipped and why / loadedFrom / loaded (Flame was asked to
     load) / sweptOld / error; a reel that could not be put in order is also in
     `result.warnings`. `batch.load_setup` does none of this.

`batch.load_setup` (any setup file, not only a checkpoint's) goes through the same replace
(`replace_group`, via its `wrap`): the same file check, the same checkpoint of the current state
first, the same intent / outcome with fingerprints; there is just no fingerprint it is expected
to reach. Both call Flame's `load_setup` through `run_load` (the bare Flame op, never `ctx.do`,
which would checkpoint a second time).
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from flame_mcp import clip_order, execution, reel_fidelity, transport
from flame_mcp import envelope as envelope_mod
from flame_mcp.checkpointed import (
    ASCII_REFUSAL_HINT,
    Judgement,
    PreRunRefusal,
    ascii_refusal_text,
    run_checkpointed,
)
from flame_mcp.checkpoints import (
    CHECKPOINT_TIMEOUT_S,
    REELS_ARG,
    TIMELINE_KIND,
    batch_target,
    checkpoint_dir,
    kind_named,
    read_fingerprint,
)
from flame_mcp.errors import as_error_code
from flame_mcp.journal import Index
from flame_mcp.policy import deny_operation
from flame_mcp.registry import RegisteredOperation
from flame_mcp.runtime import ServerContext
from flame_mcp.timeline_restore import restore_timeline_checkpoint
from flame_mcp.types import ServerOpError, ServerResult

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

OP_NAME = "journal.restore_checkpoint"
LOAD_OP_NAME = "batch.load_setup"
_NEEDED_OPS = ("batch.checkpoint", "batch.fingerprint", LOAD_OP_NAME)


def _short(fingerprint: str | None) -> str:
    return fingerprint[:12] if fingerprint else "none"


def _refuse_if_denied(ctx: ServerContext) -> None:
    for name in _NEEDED_OPS:
        op = ctx.registry.get(name)
        if op is None:
            raise ServerOpError("UNKNOWN_OPERATION", f"'{name}' is not registered")
        denial = deny_operation(op, ctx.policy)
        if denial is not None:
            raise ServerOpError(
                "POLICY_DENIED",
                f"nothing was restored: a restore needs '{name}' and it is denied "
                f"({denial.message})",
                hint=denial.hint,
            )


def _pre_summary(pre: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": pre.get("checkpoint"),
        "path": pre.get("path"),
        "fingerprint": pre.get("fingerprint"),
        "nodeCount": pre.get("nodeCount"),
    }


def run_load_setup(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` handler of `batch.load_setup`: the same replace as a checkpoint restore, for
    any setup file. The file must exist (checked here, and again inside Flame right before the
    call); the group's current state is checkpointed first, the run journaled with fingerprints,
    so `journal.revert` puts the group back. A failed load_setup empties the group:
    the failure's result carries `preCheckpoint` {id, path} to restore it from."""
    started = time.monotonic()
    meta = envelope_mod.execution_meta(op)
    path = str(op_args["path"])

    def failed(exc: ServerOpError) -> dict[str, Any]:
        return envelope_mod.build_envelope(
            ok=False,
            result=exc.result,
            error=exc.message,
            error_code=exc.code,
            journal=exc.journal,
            execution=meta,
            duration_ms=int((time.monotonic() - started) * 1000),
            hint_override=exc.hint,
        )

    if not os.path.isfile(path):
        return failed(
            ServerOpError(
                "PRECONDITION_UNMET",
                f"no setup file at {path!r}: nothing was changed and load_setup was not called "
                "(a load_setup that fails empties the group)",
                result={"path": path, "fileExists": False, "loadSetupCalled": False},
            )
        )
    try:
        _refuse_if_denied(rt)
        resolved = read_fingerprint(rt, op_args["target"])
        stable = resolved.get("result", {}).get("stable") if resolved.get("ok") is True else None
        if not isinstance(stable, dict):
            raise ServerOpError(
                as_error_code(str(resolved.get("errorCode"))),
                "nothing was replaced: the Batch group could not be read "
                f"({resolved.get('error')})",
                hint=resolved.get("hint"),
            )
        outcome = replace_group(
            rt,
            target=stable,
            path=path,
            op_name=LOAD_OP_NAME,
            kind="load_setup",
            args={"target": op_args["target"], "path": path},
            expected_fp=None,
            label=f"before load_setup of {os.path.basename(path)}",
            extra_intent={},
            extra_result={},
            reverts=None,
            reverts_intent=None,
            forced=None,
            group=group,
            reel_record=None,
            reel_skip_reason=reel_fidelity.NOT_A_CHECKPOINT_REASON,
        )
    except ServerOpError as exc:
        return failed(exc)
    return envelope_mod.build_envelope(
        ok=True,
        result=outcome.result,
        affected=outcome.affected,
        undo_steps=0,
        journal=outcome.journal,
        execution=meta,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def run_load(
    ctx: ServerContext,
    target: Mapping[str, Any],
    path: str,
    reel_record: reel_fidelity.ReelRecord | None = None,
) -> dict[str, Any]:
    """Flame's `load_setup` through the bare `batch.load_setup` op: its Flame-side file check,
    no checkpoint of its own (the caller has taken one). Policy for it was checked up front.
    With `reel_record` the reels are tidied in the same Flame call, right after the load."""
    op = ctx.registry.get(LOAD_OP_NAME)
    if op is None:
        raise ServerOpError("UNKNOWN_OPERATION", f"'{LOAD_OP_NAME}' is not registered")
    args: dict[str, Any] = {"target": dict(target), "path": path}
    if reel_record is not None:
        args[REELS_ARG] = reel_record.rows
    return execution.run_op(
        op,
        args,
        run_dir=ctx.run_dir,
        timeout_s=CHECKPOINT_TIMEOUT_S,
    )


@dataclass
class LoadProgress:
    """How far `replace_group` got with the load, for a caller that has to clean up after it."""

    started: bool = False
    """Flame was asked to load."""
    unanswered: bool = False
    """The load ended without an answer: Flame may still be reading the file."""


@dataclass(frozen=True)
class _ClipOrderStep:
    report: dict[str, Any]
    copy: clip_order.OrderedCopy | None
    warnings: list[str]


def _journal_record(ctx: ServerContext, checkpoint_id: str) -> Mapping[str, Any]:
    record = Index(ctx.journal.read_all().records).checkpoints.get(checkpoint_id)
    if record is None:
        raise ServerOpError(
            "INVALID_ARGS",
            f"no checkpoint {checkpoint_id!r} in the journal (journal.list_checkpoints lists them)",
        )
    return record


def _checkpoint_fields(
    ctx: ServerContext, checkpoint_id: str, record: Mapping[str, Any]
) -> tuple[str, str, str]:
    """(file path, group uid, fingerprint) of a Batch checkpoint record, once its file is known to
    exist and the ops a restore needs are allowed. Raises `ServerOpError` otherwise."""
    path = record.get("path")
    batch = record.get("batch")
    uid = batch.get("uid") if isinstance(batch, dict) else None
    expected_fp = record.get("fingerprint")
    if not isinstance(path, str) or not isinstance(uid, str) or not isinstance(expected_fp, str):
        raise ServerOpError(
            "INVALID_ARGS", f"checkpoint {checkpoint_id!r} has no path, group uid or fingerprint"
        )
    _refuse_if_denied(ctx)
    if not os.path.isfile(path):
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"the file of checkpoint {checkpoint_id} is missing ({path}): nothing was changed "
            "and load_setup was not called (a load_setup that fails empties the group)",
            result={
                "checkpoint": checkpoint_id,
                "path": path,
                "fileExists": False,
                "loadSetupCalled": False,
            },
        )
    return path, uid, expected_fp


def _prepare_clip_order(
    ctx: ServerContext,
    path: str,
    reel_record: reel_fidelity.ReelRecord | None,
    reel_skip_reason: str | None,
) -> _ClipOrderStep:
    """The `clipOrder` report, the copy to load instead of the checkpoint (None: load the
    checkpoint itself) and the warnings. A preparation that raises is reported in `error`, not
    swallowed."""
    if reel_record is None:
        return _ClipOrderStep(clip_order.report_not_run(path, reel_skip_reason), None, [])
    work_root = checkpoint_dir(ctx.journal) / clip_order.ORDERED_DIR_NAME
    try:
        prep = clip_order.prepare(path, reel_record.rows, work_root)
    except Exception as exc:
        return _ClipOrderStep(
            clip_order.report(None, path, f"{type(exc).__name__}: {exc}"), None, []
        )
    return _ClipOrderStep(
        clip_order.report(prep, path, None), prep.copy, clip_order.warnings_of(prep)
    )


def _settle_clip_order(step: _ClipOrderStep, progress: LoadProgress, result: Any) -> None:
    """After the replace, whatever way it ended: say whether Flame was asked to load, remove the
    copy -- unless the load ended without an answer, then it stays -- and put the warnings in
    the result."""
    step.report["loaded"] = progress.started
    if progress.unanswered and step.copy is not None:
        step.report.update(
            copyKept=True,
            copyPath=str(step.copy.directory),
            copyKeptReason=(
                "load_setup ended without an answer, so Flame may still be reading the "
                "copy; it was not removed (a load_setup that fails empties the group)"
            ),
        )
    else:
        try:
            clip_order.remove_copy(step.copy)
        except (OSError, ValueError) as exc:
            step.report["cleanupError"] = f"{type(exc).__name__}: {exc}"
    if step.warnings and isinstance(result, dict):
        result["warnings"] = [*result.get("warnings", []), *step.warnings]


def restore_checkpoint(
    ctx: ServerContext,
    checkpoint_id: str,
    *,
    reverts: str | None = None,
    reverts_intent: str | None = None,
    forced: Mapping[str, Any] | None = None,
) -> ServerResult:
    """Restore the group of `checkpoint_id` from its saved setup, as described above (a timeline
    checkpoint goes to `timeline_restore.restore_timeline_checkpoint`). Raises `ServerOpError` on
    every refusal and failure; the result of a failure that happened after the pre-restore
    checkpoint was taken always carries `preCheckpoint`."""
    record = _journal_record(ctx, checkpoint_id)
    if kind_named(record.get("kind")) is TIMELINE_KIND:
        return restore_timeline_checkpoint(ctx, record)
    path, uid, expected_fp = _checkpoint_fields(ctx, checkpoint_id, record)
    reel_record, reel_skip_reason = reel_fidelity.record_of(record)
    step = _prepare_clip_order(ctx, path, reel_record, reel_skip_reason)
    progress = LoadProgress()
    result: Any = None
    try:
        outcome = replace_group(
            ctx,
            target=batch_target(uid),
            path=path,
            op_name=OP_NAME,
            kind="restore",
            args={"checkpoint": checkpoint_id},
            expected_fp=expected_fp,
            label=f"before restoring {checkpoint_id}",
            extra_intent={"checkpoint": checkpoint_id},
            extra_result={"checkpoint": checkpoint_id, "clipOrder": step.report},
            reverts=reverts,
            reverts_intent=reverts_intent,
            forced=forced,
            group=None,
            reel_record=reel_record,
            reel_skip_reason=reel_skip_reason,
            load_path=str(step.copy.batch_path) if step.copy is not None else None,
            progress=progress,
        )
        result = outcome.result
        return outcome
    except ServerOpError as exc:
        result = exc.result
        raise
    finally:
        _settle_clip_order(step, progress, result)


def replace_group(
    ctx: ServerContext,
    *,
    target: Mapping[str, Any],
    path: str,
    op_name: str,
    kind: str,
    args: Mapping[str, Any],
    expected_fp: str | None,
    label: str,
    extra_intent: Mapping[str, Any],
    extra_result: Mapping[str, Any],
    reverts: str | None,
    reverts_intent: str | None,
    forced: Mapping[str, Any] | None,
    group: str | None,
    reel_record: reel_fidelity.ReelRecord | None = None,
    reel_skip_reason: str | None = None,
    load_path: str | None = None,
    progress: LoadProgress | None = None,
) -> ServerResult:
    """Replace a Batch group's contents from the setup file `path` (which the caller has seen
    exist) through the checkpointed engine (`checkpointed.run_checkpointed`): checkpoint the
    current state, journal the intent, load, read the group back, journal the outcome. With
    `expected_fp` the replace succeeds only if the group's fingerprint ends up equal to it;
    without, only the load's own answer counts. Raises `ServerOpError` on every failure; a
    failure after the checkpoint carries `preCheckpoint` in its result. With `reel_record` (a
    checkpoint's reel inventory) the reels are tidied after the load and `reelTidy` /
    `reelsRestored` are in the result; without, `reelTidy` says why not (`reel_skip_reason`).
    `load_path` is the file Flame loads when it is not `path` (the ordered copy of a checkpoint,
    `clip_order`); `path` stays what the journal and the result name. `progress` is told when
    Flame is asked to load and when the load ended without an answer (Flame may still be
    reading `load_path`)."""

    def run(stable: dict[str, Any]) -> dict[str, Any]:
        if progress is not None:
            progress.started = True
        return run_load(ctx, stable, load_path or path, reel_record)

    def tidy_of(load_env: Mapping[str, Any]) -> dict[str, Any]:
        raw = load_env.get("result") if load_env.get("ok") is True else None
        return reel_fidelity.report(reel_record, reel_skip_reason, raw)

    def matched_of(load_env: Mapping[str, Any], after_fp: str | None) -> bool:
        return load_env.get("ok") is True and (expected_fp is None or after_fp == expected_fp)

    def judge(load_env: dict[str, Any], after_fp: str | None) -> Judgement:
        if matched_of(load_env, after_fp):
            return Judgement(ok=True)
        return Judgement(
            ok=False,
            failed=_failure_text(load_env, after_fp, expected_fp),
            error_code=_failure_code(load_env),
        )

    def outcome_extra(load_env: dict[str, Any], after_fp: str | None) -> dict[str, Any]:
        load_result = load_env.get("result") if isinstance(load_env.get("result"), dict) else {}
        return {
            "loadReturned": (load_result or {}).get("returned"),
            "matched": matched_of(load_env, after_fp),
            "reelsRestored": tidy_of(load_env)["reelsRestored"],
        }

    with ctx.write_lock:
        try:
            facts = run_checkpointed(
                ctx,
                selector=target,
                op_name=op_name,
                kind=kind,
                args=args,
                intent_extra={"path": path, **extra_intent},
                expected_fp=expected_fp,
                label=label,
                run=run,
                judge=judge,
                failed_key="restore",
                outcome_extra=outcome_extra,
                group=group,
                reverts=reverts,
                reverts_intent=reverts_intent,
                forced=forced,
            )
        except PreRunRefusal as exc:
            raise _pre_run_error(ctx, exc, extra_result) from exc

    pre_summary = facts.pre_summary()
    load_env = facts.env
    if facts.unknown_end:
        if progress is not None:
            progress.unanswered = True
        load_code = load_env.get("errorCode")
        raise ServerOpError(
            as_error_code(str(load_code)),
            f"load_setup ended without an answer ({load_code}); it is not known whether the "
            f"group was replaced. The state before it is saved in checkpoint {facts.pre_id} "
            f"({pre_summary['path']}). This server now refuses writes until journal.check "
            f"(group={facts.group!r}) has read Flame again.",
            result={**extra_result, "group": facts.group, "preCheckpoint": pre_summary},
            hint=load_env.get("hint"),
            journal={"group": facts.group, "id": facts.intent_id},
        )
    raw_load = load_env.get("result") if isinstance(load_env.get("result"), dict) else {}
    load_result = {k: v for k, v in (raw_load or {}).items() if k != "reelTidy"}
    tidy = tidy_of(load_env)
    after_fp = facts.fingerprint_after
    matched = facts.ok
    result: dict[str, Any] = {
        **extra_result,
        "group": facts.group,
        "path": path,
        "matched": matched,
        "expectedFingerprint": expected_fp,
        "fingerprintBefore": facts.fingerprint_before,
        "fingerprintAfter": after_fp,
        "load": load_result,
        "reelsRestored": tidy["reelsRestored"],
        "reelTidy": tidy,
        "preCheckpoint": pre_summary,
        "undo": {"op": OP_NAME, "args": {"checkpoint": facts.pre_id}},
    }
    if forced is not None:
        result["forced"] = dict(forced)
    if facts.journal_error is not None:
        result["journalError"] = (
            f"the outcome could not be journaled ({facts.journal_error}); this server now refuses "
            "writes until journal.check has settled the intent"
        )
    journal_ref = {"group": facts.group, "id": facts.intent_id}
    if matched:
        return ServerResult(result=result, affected=1, journal=journal_ref)
    raise ServerOpError(
        as_error_code(_failure_code(load_env)),
        _failure_message(load_env, after_fp, expected_fp, facts.pre_id, pre_summary),
        result=result,
        hint=(
            f"The state before this change is saved: journal.restore_checkpoint "
            f"(checkpoint={facts.pre_id!r}) puts it back."
        ),
        journal=journal_ref,
    )


def _pre_run_error(
    ctx: ServerContext, exc: PreRunRefusal, extra_result: Mapping[str, Any]
) -> ServerOpError:
    """The error of a replace that stopped before the load: nothing was replaced."""
    if exc.stage == "checkpoint":
        cp_env = exc.cp_env or {}
        return ServerOpError(
            as_error_code(str(cp_env.get("errorCode"))),
            "nothing was replaced: the current state of the group could not be "
            "checkpointed first, so the change would not be undoable "
            f"({cp_env.get('error')})",
            result={**extra_result, "loadSetupCalled": False},
            hint=cp_env.get("hint"),
        )
    if exc.stage == "ascii":
        return ServerOpError(
            "INVALID_ARGS",
            f"nothing was replaced: {ascii_refusal_text(exc)}",
            result={**extra_result, "loadSetupCalled": False},
            hint=ASCII_REFUSAL_HINT,
        )
    cp = exc.cp or {}
    pre_summary = _pre_summary(cp)
    if exc.stage == "uid":
        return ServerOpError(
            "PRECONDITION_UNMET",
            "nothing was replaced: the target group has no uid, so it cannot be addressed "
            "again after the checkpoint",
            result={**extra_result, "preCheckpoint": pre_summary},
        )
    return ServerOpError(
        "POLICY_DENIED",
        f"nothing was replaced: the journal cannot be written ({exc.cause}), and this server "
        "does not replace a Batch group without an intent on record",
        result={**extra_result, "preCheckpoint": pre_summary},
        hint=f"Make {ctx.journal.directory} writable, then retry.",
    )


def _failure_code(load_env: Mapping[str, Any]) -> str:
    if load_env.get("ok") is not True:
        return str(load_env.get("errorCode"))
    return "FLAME_API_ERROR"


def _failure_text(
    load_env: Mapping[str, Any], after_fp: str | None, expected_fp: str | None
) -> str:
    if load_env.get("ok") is not True:
        return f"load_setup failed: {load_env.get('error')}"
    return (
        f"load_setup returned True but the group's fingerprint {_short(after_fp)} is not the "
        f"checkpoint's {_short(expected_fp)}"
    )


def _failure_message(
    load_env: Mapping[str, Any],
    after_fp: str | None,
    expected_fp: str | None,
    pre_id: str,
    pre_summary: Mapping[str, Any],
) -> str:
    detail = _failure_text(load_env, after_fp, expected_fp)
    if transport.was_refused_before_sending(load_env):
        return (
            f"{detail}. The group was not changed; the state before this change is saved in "
            f"checkpoint {pre_id} ({pre_summary.get('path')})"
        )
    return (
        f"{detail}. The group may have been emptied or replaced by something else; the state "
        f"before this change is saved in checkpoint {pre_id} ({pre_summary.get('path')})"
    )
