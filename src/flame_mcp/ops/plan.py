"""`plan` category (thread=server): `plan.run` -- several ops in one call.

The loop runs on the server; every child goes through `ctx.do`, the same door a `flame_do`
call uses (policy, argument validation, defaults, the journaled write with its compare-and-swap,
the fail-closed gate). What the plan adds:

  - the whole plan is checked before anything runs; one bad step and nothing is executed
  - one journal group for the whole plan: every write's intent lands in it, so
    `journal.revert(group)` puts the plan back in one call (reverse order)
  - a stop at the first failure (`abort`, the default) -- `continue` is only for plans that
    read, because a write that failed half way is exactly the state a caller must look at
  - a stop, whatever `on_error` says, when a write ends without an answer (the server then
    refuses writes until `journal.check`)

It does not make anything faster (the wait after each Flame call dominates), and it opens no
undo group: Flame's Python API has none.

A plan that holds a step of a Timeline-checkpointed op (`segment.trim`, slip, replace and
the effect ops) is one checkpointed write on one sequence / clip instead (`plan_timeline`):
the segment selectors are resolved up front, the sequence is checkpointed once, the steps run
without checkpoints of their own and `journal.revert(group)` is one restore. The step's optional
`late: true` leaves a step out of the up-front resolution.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from flame_mcp import opschema, plan_timeline
from flame_mcp.checkpointed import CheckpointedRun
from flame_mcp.errors import as_error_code
from flame_mcp.journal import new_id, now_iso
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.opschema import validate_op_args
from flame_mcp.policy import deny_operation
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.runtime import ServerContext
from flame_mcp.types import Category, OperationParam, OperationSpec, ServerOpError, ServerResult

CATEGORY = Category(
    name="plan",
    description=(
        "Runs several ops in one call. A server-side loop: each child goes through the ordinary "
        "do path, and the wrapper itself does not touch Flame."
    ),
    default_thread="server",
    default_pacing="none",
    allowed_thread=frozenset({"server"}),
    allowed_pacing=frozenset({"none"}),
)

OWN_GROUP_OPS: Final[frozenset[str]] = frozenset({"batch.load_setup", "journal.restore_checkpoint"})
"""Ops that checkpoint and journal into a group of their own; a plan cannot revert them. So does
every op that declares a `CheckpointSpec` (`owns_journal_group`).

Decision: a plan keeps refusing them (the Batch ones; a Timeline
checkpointed op is bundled, see `plan_timeline`). A plan's single journal group is
reverted intent by intent in reverse order, each intent by its previous values; an intent that
restores a checkpoint would have to be reverted by restoring its own checkpoint (and only while the
group's fingerprint is the one it left), and a group mixing the two kinds of intent has no
tested revert. The other way -- one checkpoint per Batch group at the start of the plan, the
whole plan reverted by restoring it -- would make every `value` write in the plan (whose own
revert is exact and cheap) share the coarse, fingerprint-blind restore. Each such op is one
call on its own, each with its own group and its own revert."""
REFUSED_IN_PLAN: Final[Mapping[str, str]] = {
    "render.batch": (
        "render.batch cannot be a plan step: a Foreground render occupies Flame for "
        "as long as it runs (no MCP call gets through meanwhile) and its output -- files on "
        "disk, a clip in the shelf reel -- cannot be undone, so it has no place in a group "
        "that journal.revert is meant to put back; call it on its own"
    ),
    "desktop.create": (
        "desktop.create cannot be a plan step: it journals a `desktop_create` intent that is "
        "reverted by deleting what it made (with its own compare-and-swap), and a plan's single "
        "group is reverted by putting attribute values back -- it would skip such an intent "
        "without saying so. Call it on its own"
    ),
    "library.create": (
        "library.create cannot be a plan step: it journals a `desktop_create` intent that is "
        "reverted by deleting the library it made (only while it is empty), and a plan's single "
        "group is reverted by putting attribute values back -- it would skip such an intent "
        "without saying so. Call it on its own"
    ),
    "desktop.copy": (
        "desktop.copy cannot be a plan step: it journals a `desktop_copy` intent that is "
        "reverted by deleting the copies it made, and a plan's single group is reverted by "
        "putting attribute values back -- it would skip such an intent without saying so. Call "
        "it on its own"
    ),
    "mediahub.import": (
        "mediahub.import cannot be a plan step: it journals a `desktop_import` intent that is "
        "reverted by deleting the clips it made, and a plan's single group is reverted by "
        "putting attribute values back -- it would skip such an intent without saying so. Call "
        "it on its own"
    ),
    "desktop.duplicate": (
        "desktop.duplicate cannot be a plan step: it journals a `desktop_duplicate` intent that "
        "is reverted by deleting the duplicate (only while it is as made), and a plan's single "
        "group is reverted by putting attribute values back -- it would skip such an intent "
        "without saying so. Call it on its own"
    ),
    "desktop.move": (
        "desktop.move cannot be a plan step: it journals a `desktop_move` intent that is "
        "reverted by moving the things back (they get new uids), and a plan's single group is "
        "reverted by putting attribute values back -- it would skip such an intent without "
        "saying so. Call it on its own"
    ),
    "segment.copy_to_media_panel": (
        "segment.copy_to_media_panel cannot be a plan step: it journals a `desktop_extract` "
        "intent that is reverted by deleting the clip it made (only while it is as made), and a "
        "plan's single group is reverted by putting attribute values back -- it would skip such "
        "an intent without saying so. Call it on its own"
    ),
    "segment.match": (
        "segment.match cannot be a plan step: it journals a `desktop_extract` intent that is "
        "reverted by deleting the clip it made (only while it is as made), and a plan's single "
        "group is reverted by putting attribute values back -- it would skip such an intent "
        "without saying so. Call it on its own"
    ),
    "timeline.create_sequence": (
        "timeline.create_sequence cannot be a plan step: it journals a `desktop_sequence` intent "
        "that is reverted by deleting the sequence it made (only while it is as made), and a "
        "plan's single group is reverted by putting attribute values back -- it would skip such "
        "an intent without saying so. Call it on its own"
    ),
    "journal.purge_checkpoints": (
        "journal.purge_checkpoints cannot be a plan step: it deletes the stashed copies that "
        "journal.revert restores from and cannot itself be undone, so it has no place in a group "
        "that journal.revert is meant to put back; call it on its own"
    ),
    "desktop.delete": (
        "desktop.delete cannot be a plan step: it journals a `desktop_delete` intent that is "
        "reverted by copying the stashed copies back (they get new uids), and a plan's single "
        "group is reverted by putting attribute values back -- it would skip such an intent "
        "without saying so. Call it on its own"
    ),
}
"""Ops a plan refuses for a reason of their own (not `owns_journal_group`)."""
BUNDLE_NOTE: Final[str] = (
    "This plan held a Timeline write, so it ran as ONE checkpointed write on this sequence / "
    "clip: one stashed copy before the first step (result.timeline.checkpoint, stash), the steps "
    "without checkpoints of their own, and journal.revert(group) is ONE restore of that copy "
    "(only while the sequence still has fingerprintAfter; force overrides). The restore also "
    "brings back the plan's segment.set_attributes steps. The restored sequence has a NEW uid "
    "and wiretap id and is at the END of its reel."
)
MAX_PLAN_STEPS: Final[int] = 50
"""Most steps one plan may hold."""
PLAN_STEP_RESULT_BYTES: Final[int] = 4_000
"""A step's `result` larger than this is left out of the plan's answer (the answer as a
whole is still held to the response byte budget by `degrade_for_size`)."""
PLAN_TOTAL_RESULT_BYTES: Final[int] = 30_000
"""All the step results one plan answer may carry together; later ones are left out."""
_ERROR_TEXT_CHARS: Final[int] = 600
_ON_ERROR_VALUES: Final[tuple[str, ...]] = ("abort", "continue")
_STEP_KEYS: Final[frozenset[str]] = frozenset({"op", "args", "late"})


@dataclass
class _Step:
    index: int
    op_name: str | None = None
    op: RegisteredOperation | None = None
    args: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    policy_denied: bool = False
    hint: str | None = None
    late: bool = False


def owns_journal_group(op: RegisteredOperation, args: Mapping[str, Any]) -> bool:
    """`op` called with `args` takes its own checkpoint and journal group (a destructive eval, an
    op that declares a Batch `CheckpointSpec`, `OWN_GROUP_OPS`). One that declares a Timeline
    `CheckpointSpec` does not count: a plan bundles it."""
    if op.category == "eval":
        return args.get("effect") != "read"
    return (
        op.checkpoint is not None and op.checkpoint.target_kind == "batch"
    ) or op.name in OWN_GROUP_OPS


def _check_step(ctx: ServerContext, index: int, raw: Any) -> _Step:
    step = _Step(index)
    if not isinstance(raw, dict):
        step.problems.append("a step must be an object {op, args}")
        return step
    extra = sorted(set(raw) - _STEP_KEYS)
    if extra:
        step.problems.append(f"unknown key(s) {extra} (a step has only 'op', 'args' and 'late')")
    late = raw.get("late", False)
    if isinstance(late, bool):
        step.late = late
    else:
        step.problems.append("'late' must be a boolean")
    name = raw.get("op")
    if not isinstance(name, str) or not name:
        step.problems.append("'op' is required and must be a non-empty string")
        return step
    step.op_name = name
    op = ctx.registry.get(name)
    if op is None:
        message = f"no operation named '{name}' is registered"
        suggestion = opschema.suggest_name(name, [o.name for o in ctx.registry.list_ops()])
        step.problems.append(message + (f" (did you mean '{suggestion}'?)" if suggestion else ""))
        return step
    step.op = op
    if op.name in REFUSED_IN_PLAN:
        step.problems.append(REFUSED_IN_PLAN[op.name])
        return step
    if op.category == "plan":
        step.problems.append("plan.run cannot be nested: a step must not be a plan.* operation")
        return step
    denial = deny_operation(op, ctx.policy)
    if denial is not None:
        step.policy_denied = True
        step.hint = denial.hint
        step.problems.append(f"denied by policy: {denial.message}")
        return step
    raw_args = raw.get("args")
    validation = validate_op_args(op, raw_args)
    if not validation.ok:
        step.problems.extend(f"{issue.path}: {issue.message}" for issue in validation.issues)
        return step
    step.args = dict(validation.value or {})
    if owns_journal_group(op, step.args):
        step.problems.append(
            f"{op.name} cannot be a plan step{' (destructive)' if op.category == 'eval' else ''}: "
            "it takes its own checkpoint and journal group, which a plan's single group cannot "
            "revert; call it on its own"
        )
        return step
    if op.precheck is not None:
        refusal = op.precheck(step.args, ctx.policy)
        if refusal is not None:
            step.problems.append(refusal.message)
            step.policy_denied = refusal.code == "POLICY_DENIED"
            step.hint = refusal.hint
    return step


def _plan_problems(steps: list[Any], on_error: Any, checked: list[_Step]) -> list[str]:
    problems: list[str] = []
    if not steps:
        problems.append("a plan needs at least one step")
    if len(steps) > MAX_PLAN_STEPS:
        problems.append(f"a plan may hold at most {MAX_PLAN_STEPS} steps, got {len(steps)}")
    if on_error not in _ON_ERROR_VALUES:
        problems.append(f"on_error must be one of {list(_ON_ERROR_VALUES)}, got {on_error!r}")
    elif on_error == "continue":
        writes = [s.index for s in checked if s.op is not None and s.op.effect != "read"]
        if writes:
            problems.append(
                "on_error='continue' is only allowed when every step is a read (effect=read); "
                f"steps {writes} write -- a write that fails half way must stop the plan"
            )
    return problems


def _refusal(problems: list[str], checked: list[_Step]) -> ServerOpError | None:
    """The error that ends a plan before anything ran, or None when the plan is valid."""
    issues = [
        {"index": s.index, "op": s.op_name, "problems": s.problems} for s in checked if s.problems
    ]
    result = {"ran": False, "planProblems": problems, "issues": issues}
    if problems or any(s.problems and not s.policy_denied for s in checked):
        count = len(issues)
        return ServerOpError(
            "INVALID_ARGS",
            f"plan not run (nothing was executed): {len(problems)} plan problem(s), "
            f"{count} invalid step(s)",
            result=result,
        )
    if issues:
        denied = next(s for s in checked if s.policy_denied)
        return ServerOpError(
            "POLICY_DENIED",
            f"plan not run (nothing was executed): step {denied.index} ({denied.op_name}) is "
            f"denied by policy",
            result=result,
            hint=denied.hint,
        )
    return None


def _step_summary(
    step: _Step, env: Mapping[str, Any], used_bytes: int
) -> tuple[dict[str, Any], int, bool]:
    """One executed step as the plan reports it, with its result left out when it is too big
    for the plan's answer. Returns (summary, bytes of result kept, was it left out)."""
    result = env.get("result")
    size = len(json.dumps(result, default=str)) if result is not None else 0
    omitted = size > PLAN_STEP_RESULT_BYTES or used_bytes + size > PLAN_TOTAL_RESULT_BYTES
    kept: Any = result
    if omitted:
        kept = {
            "omitted": True,
            "bytes": size,
            "note": "too big for a plan's answer; run this op on its own for the full result",
        }
    error = env.get("error")
    summary = {
        "index": step.index,
        "op": step.op_name,
        "ok": bool(env.get("ok")),
        "errorCode": env.get("errorCode"),
        "error": str(error)[:_ERROR_TEXT_CHARS] if error is not None else None,
        "result": kept,
        "journal": env.get("journal"),
    }
    return summary, 0 if omitted else size, omitted


@dataclass
class _Progress:
    """What the steps of a plan have done so far."""

    reports: list[dict[str, Any]] = field(default_factory=list)
    omitted: list[int] = field(default_factory=list)
    affected: int = 0
    undo_steps: int = 0
    failed: int = 0
    used_bytes: int = 0
    stopped_at: int | None = None
    fail_closed: dict[str, Any] | None = None
    last_failure: dict[str, Any] | None = None


def _loop(
    checked: list[_Step],
    on_error: str,
    progress: _Progress,
    runner: Callable[[_Step], dict[str, Any]],
) -> None:
    """Run the steps in order with `runner` until the end or the first stop."""
    for step in checked:
        assert step.op is not None and step.op_name is not None
        env = runner(step)
        summary, kept, was_omitted = _step_summary(step, env, progress.used_bytes)
        progress.used_bytes += kept
        if was_omitted:
            progress.omitted.append(step.index)
        progress.reports.append(summary)
        progress.affected += int(env.get("affected") or 0)
        progress.undo_steps += int(env.get("undoSteps") or 0)
        if env.get("ok") is True:
            continue
        progress.failed += 1
        progress.last_failure = {"step": summary, "hint": env.get("hint"), "env": env}
        if step.op.effect != "read" and env.get("errorCode") in UNKNOWN_END_CODES:
            progress.fail_closed = {
                "step": step.index,
                "errorCode": env.get("errorCode"),
                "journal": env.get("journal"),
                "note": "the write's outcome is unknown: this server refuses writes until "
                "journal.check has read Flame again; the rest of the plan was not run",
            }
            progress.stopped_at = step.index
            return
        if on_error == "abort":
            progress.stopped_at = step.index
            return


def _bundle_env(progress: _Progress) -> dict[str, Any]:
    """The steps' run as the one envelope the checkpointed engine judges and journals."""
    rows = [{k: r.get(k) for k in ("index", "op", "ok", "errorCode")} for r in progress.reports]
    env: dict[str, Any] = {"ok": progress.stopped_at is None, "steps": rows}
    env["stoppedAt"] = progress.stopped_at
    if progress.stopped_at is not None and progress.last_failure is not None:
        failing = progress.last_failure["step"]
        env["errorCode"] = failing["errorCode"]
        env["error"] = f"step {progress.stopped_at} ({failing['op']}): {failing['error']}"
    return env


def _timeline_result(
    facts: CheckpointedRun, target: plan_timeline.TimelineTarget
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "target": {
            "type": target.ident.get("type"),
            "name": target.ident.get("name"),
            "uid": target.uid,
            "selector": target.stable,
        },
        "checkpoint": facts.pre_id,
        "stash": facts.pre.get("stash"),
        "fingerprintBefore": facts.fingerprint_before,
        "fingerprintAfter": facts.fingerprint_after,
        "changed": facts.changed,
        "resolved": target.resolved,
        "note": BUNDLE_NOTE,
    }
    if facts.after_error is not None:
        info["fingerprintAfterError"] = facts.after_error
    if facts.journal_error is not None:
        info["journalError"] = (
            f"the outcome could not be journaled ({facts.journal_error}); this server now "
            "refuses writes until journal.check has settled the intent"
        )
    return info


def _run(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    steps: list[Any] = list(args["steps"])
    on_error = args.get("on_error", "abort")
    label = args.get("label")
    checked = [_check_step(ctx, i, raw) for i, raw in enumerate(steps)]
    plan_timeline.apply_static_rules(checked)
    problems = _plan_problems(steps, on_error, checked)
    refusal = _refusal(problems, checked)
    if refusal is not None:
        raise refusal

    has_writes = any(s.op is not None and s.op.effect != "read" for s in checked)
    if has_writes:
        denial = ctx.guard.denial()
        if denial is not None:
            raise ServerOpError(
                "POLICY_DENIED",
                f"plan not run (nothing was executed): {denial.message}",
                result={"ran": False},
                hint=denial.hint,
            )
    progress = _Progress()
    group: str | None = None
    facts: CheckpointedRun | None = None
    target: plan_timeline.TimelineTarget | None = None
    lock = ctx.write_lock if has_writes else contextlib.nullcontext()
    with lock:  # nobody else writes (or is classified by journal.check) while the plan runs
        if plan_timeline.has_timeline_step(checked):
            target = plan_timeline.preresolve(ctx, checked)  # a refusal: nothing is journaled
        group = new_id("g") if has_writes else None
        if group is not None:
            try:
                ctx.journal.append(
                    {
                        "type": "group",
                        "group": group,
                        "ts": now_iso(),
                        "kind": "plan.run",
                        "label": label,
                        "onError": on_error,
                        "steps": [s.op_name for s in checked],
                        "late": [s.index for s in checked if s.late],
                    }
                )
            except OSError as exc:
                raise ServerOpError(
                    "POLICY_DENIED",
                    f"plan not run (nothing was executed): the journal cannot be written ({exc})",
                    result={"ran": False},
                    hint=f"Make {ctx.journal.directory} writable, then retry.",
                ) from exc
        if target is not None and group is not None:

            def execute(runner: Callable[[_Step], dict[str, Any]]) -> dict[str, Any]:
                _loop(checked, on_error, progress, runner)
                return _bundle_env(progress)

            facts = plan_timeline.run_bundled(
                ctx, target, checked, group=group, label=label, on_error=on_error, execute=execute
            )
        else:
            _loop(
                checked,
                on_error,
                progress,
                lambda step: ctx.do(str(step.op_name), step.args, group=group),
            )

    executed = len(progress.reports)
    partial = {
        "done": executed - progress.failed,
        "failed": progress.failed,
        "notRun": len(checked) - executed,
    }
    journal_ref = {"group": group} if group is not None else None
    result: dict[str, Any] = {
        "group": group,
        "label": label,
        "onError": on_error,
        "total": len(checked),
        "stoppedAt": progress.stopped_at,
        "failClosed": progress.fail_closed,
        "steps": progress.reports,
        "resultsOmitted": progress.omitted,
    }
    hint = progress.last_failure["hint"] if progress.last_failure is not None else None
    if facts is not None and target is not None:
        result["timeline"] = _timeline_result(facts, target)
        if progress.stopped_at is not None and not facts.unknown_end:
            hint = (
                f"{hint or ''} The steps before it stay applied: journal.revert(group="
                f"{group!r}) restores the sequence to what it was before the plan (one restore)."
            ).strip()
    if progress.stopped_at is not None and progress.last_failure is not None:
        failing = progress.last_failure["step"]
        fail_closed = progress.fail_closed
        raise ServerOpError(
            as_error_code(failing["errorCode"]),
            f"plan stopped at step {progress.stopped_at} ({failing['op']}): {failing['error']}"
            + (f" -- {fail_closed['note']}" if fail_closed is not None else ""),
            result=result,
            hint=hint,
            affected=progress.affected,
            undo_steps=progress.undo_steps,
            partial=partial,
            journal=journal_ref,
        )
    return ServerResult(
        result=result,
        affected=progress.affected,
        partial=partial,
        undo_steps=progress.undo_steps,
        journal=journal_ref,
    )


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)
    registry.register(
        OperationSpec(
            name="plan.run",
            category="plan",
            description=(
                "Run several operations in one call: steps = [{op, args}, ...], each one exactly "
                "what a flame_do call would take. The steps may write (the effect read above "
                "is that of the wrapper itself). The whole plan is checked first -- an unknown "
                "op, an op the policy hides (READONLY: any write), bad arguments, a nested "
                "plan.run, more than "
                f"{MAX_PLAN_STEPS} steps -- and if any step is invalid nothing runs "
                "(INVALID_ARGS, result.issues = [{index, op, problems}]; POLICY_DENIED when the "
                "only trouble is policy). Steps then run in order, each through the normal do "
                "path (policy, defaults, journaled writes with their compare-and-swap). "
                "on_error='abort' (default) stops at the first failing step: result.stoppedAt, "
                "every step's {index, op, ok, errorCode, error, result, journal}, "
                "partial = {done, failed, notRun}; steps before it stay applied. "
                "on_error='continue' is allowed only when every step is a read. If a write "
                "ends without an answer (TIMEOUT / BRIDGE_LOST / ...) the plan stops at once "
                "whatever on_error says (result.failClosed) and this server refuses writes "
                "until journal.check. Ops that take their own checkpoint and journal group (a "
                "destructive eval.run, batch.load_setup, journal.restore_checkpoint, and every "
                "Batch write that runs behind an automatic checkpoint: batch.create_reel, "
                "batch.create_shelf_reel, batch.add_clips, batch.organize, batch.iterate, "
                "node.create, node.delete, node.connect, node.disconnect, node.duplicate, "
                "node.load_setup, action.create_node, action.connect) are "
                "refused as steps -- call them on their own. So is render.batch: a Foreground "
                "render occupies Flame for as long as it runs and its output "
                "(files on disk, clips in the shelf reel) cannot be undone. So are "
                "desktop.create and desktop.copy: their revert deletes what they made, which a "
                "plan's group (reverted by putting values back) does not do. Nor can "
                "mediahub.import, desktop.duplicate, desktop.move and desktop.delete "
                "(deleting / moving back / copying back), segment.copy_to_media_panel and "
                "segment.match (deleting the clip they made), timeline.create_sequence (deleting "
                "the sequence it made), or journal.purge_checkpoints. "
                "All the plan's writes share one "
                "journal group "
                "(envelope journal.group, `label` recorded on it): journal.revert(group) puts "
                "them all back, in reverse order. "
                "Timeline writes: a plan that holds a step of a Timeline-checkpointed op "
                "(segment.trim, segment.slip, segment.replace, segment.create_effect / "
                "delete_effect / set_effect / effect_load_setup, timeline.overwrite / insert / "
                "cut / create_version / create_track) is one checkpointed write "
                "on one sequence / clip instead. Every "
                "write step must be to that sequence (a Timeline-checkpointed op, or "
                "segment.set_attributes of one of its segments); a write to another sequence, or "
                "of another kind (desktop.rename, batch / node writes, timeline.checkpoint ...), "
                "is refused before anything runs (INVALID_ARGS; the ways of reverting differ: one "
                "restore could not undo it) -- reads of any sequence are fine. The segment "
                "selectors of "
                "every write step are first resolved in one Flame read (index / record_in / "
                "expect_source_name; the sequence may be given by any selector kind, it is pinned "
                "by its wiretap id): if one does not resolve nothing runs, nothing is copied and "
                "nothing is journaled (SELECTOR_EMPTY / CONFLICT, result.preResolution = "
                "[{index, op, errorCode, error}]). A step may carry `late: true` (next to op and "
                "args): it is left out of that resolution and resolved when its turn comes -- use "
                "it for a step that points at a segment an earlier step makes or moves (a head "
                "trim without ripple moves record_in); if it does not resolve then, the plan stops "
                "there and the steps before it stay applied (revertible). The sequence is "
                "copied into the stash once (timeline.checkpoint), the steps run without "
                "checkpoints of their own, and journal.revert(group) is one restore of that copy "
                "(only while the sequence still has result.timeline.fingerprintAfter; force "
                "overrides): it also brings back the plan's segment.set_attributes steps, which "
                "are not written back value by value. The restored sequence has a new uid and "
                "wiretap id and is at the end of its reel. result.timeline = {target, checkpoint, "
                "stash, fingerprintBefore, fingerprintAfter, changed, resolved, note}. A write "
                "step that ends without an answer stops the plan as above, and journal.check "
                "decides the plan's intent from the sequence's fingerprint (before = not_landed, "
                "anything else = diverged). "
                "A step's result too big for the answer "
                "(over "
                f"{PLAN_STEP_RESULT_BYTES} bytes, or beyond {PLAN_TOTAL_RESULT_BYTES} in "
                "total) is replaced by {omitted, bytes} (result.resultsOmitted lists them). "
                "A plan buys one up-front check of the whole plan and one group to undo. "
                "affected and undoSteps are sums over the steps."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="steps",
                    type="array",
                    description="[{op, args, late?}, ...] -- at most "
                    f"{MAX_PLAN_STEPS}; each step's op and args as flame_do takes them. `late: "
                    "true` leaves a Timeline write out of the up-front resolution of the "
                    "plan's segment selectors: use it when the step points at a segment an "
                    "earlier step of the plan makes or moves (it is resolved when its turn "
                    "comes; if it does not resolve then the plan stops there, the earlier "
                    "steps stay applied and journal.revert(group) puts the sequence back).",
                    required=True,
                    # no item_type: a step that is not an object is reported by the plan's own
                    # check, with the others, as {index, problems}
                ),
                OperationParam(
                    name="on_error",
                    type="string",
                    description="'abort' (default) or 'continue' (only for all-read plans).",
                    required=False,
                    default="abort",
                ),
                OperationParam(
                    name="label",
                    type="string",
                    description="A name for the plan, recorded on its journal group.",
                    required=False,
                    non_empty=True,
                ),
            ),
            server_handler=_run,
        )
    )
