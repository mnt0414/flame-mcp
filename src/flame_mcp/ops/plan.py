"""`plan` category (thread=server): `plan.run` -- several ops in one call.

The loop runs on the server; every child goes through `ctx.do`, the same path a `flame_do`
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

A plan that holds a step of a checkpointed op is one checkpointed write on one target instead
(`plan_bundle`): a Timeline-checkpointed op (`segment.trim`, slip, replace and the effect ops) on
one sequence / clip, or a Batch-checkpointed op (`node.create`, `node.connect`, ...) on one Batch
group. The selectors are resolved up front, the target is checkpointed once, the steps run without
checkpoints of their own and `journal.revert(group)` is one restore. The step's optional
`late: true` leaves a step out of the up-front resolution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from flame_mcp import plan_bundle, plan_refs
from flame_mcp.checkpointed import CheckpointedRun
from flame_mcp.checkpoints import BATCH_KIND, TIMELINE_KIND
from flame_mcp.errors import as_error_code
from flame_mcp.journal import new_id
from flame_mcp.journaled_write import outcome_not_journaled_note
from flame_mcp.plan_progress import (
    ON_ERROR_ABORT,
    ON_ERROR_CONTINUE,
    ON_ERROR_VALUES,
    Progress,
    check_bundle,
    journal_refusal,
    run_in_group,
    runner_for,
)
from flame_mcp.plan_step import (
    MAX_PLAN_STEPS,
    MIXED_DETAIL,
    PLAN_STEP_RESULT_BYTES,
    PLAN_TOTAL_RESULT_BYTES,
    Step,
    args_checker,
    check_step,
    is_brief_step,
    policy_only_denial,
    step_issues,
    step_summary,
)
from flame_mcp.registry import Registry
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

BUNDLE_NOTES: Final[Mapping[str, str]] = {
    TIMELINE_KIND.name: (
        "This plan held a Timeline write, so it ran as one checkpointed write on this sequence / "
        "clip: one stashed copy before the first step (result.timeline.checkpoint, stash), the "
        "steps without checkpoints of their own, and journal.revert(group) is one restore of "
        "that copy (only while the sequence still has fingerprintAfter; force overrides). The "
        "restore also brings back the plan's segment.set_attributes steps. A plan that stopped "
        "before any write ran has nothing to revert. The restored "
        "sequence has a new uid and wiretap id and is at the end of its reel."
    ),
    BATCH_KIND.name: (
        "This plan held a Batch write, so it ran as one checkpointed write on this Batch group: "
        "one saved setup before the first step (result.batch.checkpoint, checkpointPath), the "
        "steps without checkpoints of their own, and journal.revert(group) is one restore of "
        "that setup (only while the group still has fingerprintAfter; force overrides). The "
        "restore also brings back the plan's node.set_attributes / action.set_attributes steps. "
        "The fingerprint does not see attribute values, positions, reels or iterations, so "
        "`changed` reads false for a plan made only of those and the revert still runs. A plan "
        "with a node.set_attributes / action.set_attributes step also records, right after the "
        "run, a fingerprint that covers every node's attribute values, and the revert refuses "
        "(CONFLICT) while an attribute value of the group differs from it (force overrides); "
        "values changed by other steps (positions set by batch.organize, node.edit_setup) are "
        "not watched that way. A plan that stopped before any write ran has nothing to revert. A "
        "restore gives the group's Clip nodes and reel clips NEW uids and can bring a reel clip "
        "back without its Timeline FX (result.timelineFxLost of the revert). Shelf reels and "
        "iterations are not put back, so a plan holds no step that makes them (batch.iterate, "
        "batch.create_shelf_reel, batch.add_clips into a shelf reel are refused)."
    ),
}
RESULT_DETAILS: Final[tuple[str, ...]] = ("auto", "full", "summary")
MIXED_NOTE: Final[str] = (
    "A successful write step is only its brief here (mixed detail); a read step and a failed "
    "step keep their result. result_detail='full' gives every step's whole result."
)
AUTO_SUMMARY_WRITE_STEPS: Final[int] = 10
"""A plan with more write steps than this answers with `result_detail='summary'` by default
(unless responses are compact: then 'auto' decides per step)."""


def _plan_problems(steps: list[Any], on_error: Any, checked: list[Step]) -> list[str]:
    problems: list[str] = []
    if not steps:
        problems.append("a plan needs at least one step")
    if len(steps) > MAX_PLAN_STEPS:
        problems.append(f"a plan may hold at most {MAX_PLAN_STEPS} steps, got {len(steps)}")
    if on_error not in ON_ERROR_VALUES:
        problems.append(f"on_error must be one of {list(ON_ERROR_VALUES)}, got {on_error!r}")
    elif on_error == ON_ERROR_CONTINUE:
        writes = [s.index for s in checked if s.op is not None and s.op.effect != "read"]
        if writes:
            problems.append(
                "on_error='continue' is only allowed when every step is a read (effect=read); "
                f"steps {writes} write -- a write that fails half way must stop the plan"
            )
    return problems


def _refusal(problems: list[str], checked: list[Step]) -> ServerOpError | None:
    """The error that ends a plan before anything ran, or None when the plan is valid."""
    issues = step_issues(checked)
    if not problems and not issues:
        return None
    result = {"ran": False, "planProblems": problems, "issues": issues}
    only_policy, denied = policy_only_denial(checked, other_problems=bool(problems))
    if not only_policy or denied is None:
        return ServerOpError(
            "INVALID_ARGS",
            f"plan not run (nothing was executed): {len(problems)} plan problem(s), "
            f"{len(issues)} invalid step(s)",
            result=result,
        )
    return ServerOpError(
        "POLICY_DENIED",
        f"plan not run (nothing was executed): step {denied.index} ({denied.op_name}) is "
        f"denied by policy",
        result=result,
        hint=denied.hint,
    )


_CLOSED_NOTE: Final[str] = (
    "the write's outcome is unknown: this server refuses writes until journal.check has read "
    "Flame again; the rest of the plan was not run"
)


def _loop(
    checked: list[Step],
    on_error: str,
    progress: Progress,
    runner: Callable[[Step], dict[str, Any]],
    resolve: Callable[[Step, Mapping[str, Mapping[str, Any]]], dict[str, Any] | None],
) -> None:
    """Run the steps in order with `runner` until the end or the first stop. A step that holds
    references is first given its values by `resolve`, which answers None, or the envelope of the
    step that is not sent: the plan then stops, whatever `on_error` says."""
    for step in checked:
        assert step.op is not None and step.op_name is not None
        refused = resolve(step, progress.envelopes) if step.has_refs else None
        env = runner(step) if refused is None else refused
        if step.id in progress.keep:
            progress.envelopes[step.id] = env
        summary, kept, was_omitted = step_summary(step, env, progress.used_bytes, progress.detail)
        progress.used_bytes += kept
        if was_omitted:
            progress.omitted.append(step.index)
        if progress.detail == MIXED_DETAIL and is_brief_step(step, env, progress.detail):
            progress.briefed_writes = True
        if progress.record(
            step,
            env,
            summary,
            where={"step": step.index},
            closed_note=_CLOSED_NOTE,
            refused=refused is not None,
            abort=on_error == ON_ERROR_ABORT,
        ):
            return


def _bundle_result(
    facts: CheckpointedRun, target: plan_bundle.BundleTarget, detail: str
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "target": {
            "type": target.ident.get("type"),
            "name": target.ident.get("name"),
            "uid": target.uid,
            "selector": target.stable,
        },
        "checkpoint": facts.pre_id,
    }
    if target.kind is TIMELINE_KIND:
        info["stash"] = facts.pre.get("stash")
    else:
        info["checkpointPath"] = facts.pre_path
    info.update(
        {
            "fingerprintBefore": facts.fingerprint_before,
            "fingerprintAfter": facts.fingerprint_after,
            "changed": facts.changed,
            "resolved": target.resolved,
            "note": BUNDLE_NOTES[target.kind.name],
        }
    )
    if detail in ("summary", MIXED_DETAIL):
        del info["resolved"]
    if facts.after_error is not None:
        info["fingerprintAfterError"] = facts.after_error
    if facts.journal_error is not None:
        info["journalError"] = outcome_not_journaled_note(facts.journal_error, None)
    return info


def _result_of(
    checked: list[Step],
    args: Mapping[str, Any],
    progress: Progress,
    group: str | None,
    facts: CheckpointedRun | None,
    target: plan_bundle.BundleTarget | None,
    detail: str,
) -> tuple[dict[str, Any], str | None]:
    """The plan's result, and the hint of its failure."""
    result: dict[str, Any] = {
        "group": group,
        "label": args.get("label"),
        "onError": args.get("on_error", ON_ERROR_ABORT),
        "total": len(checked),
        "detail": detail,
        "stoppedAt": progress.stopped_at,
        "failClosed": progress.fail_closed,
        "steps": progress.reports,
        "resultsOmitted": progress.omitted,
    }
    if progress.briefed_writes:
        result["resultsNote"] = MIXED_NOTE
    hint = progress.last_failure["hint"] if progress.last_failure is not None else None
    if facts is not None and target is not None:
        result[target.kind.name] = _bundle_result(facts, target, detail)
        if progress.stopped_at is not None and progress.touched and not facts.unknown_end:
            hint = (
                f"{hint or ''} The steps before it stay applied: journal.revert(group="
                f"{group!r}) restores the {target.kind.subject} to what it was before the plan "
                "(one restore)."
            ).strip()
    return result, hint


def _answer(
    checked: list[Step],
    args: Mapping[str, Any],
    progress: Progress,
    group: str | None,
    facts: CheckpointedRun | None,
    target: plan_bundle.BundleTarget | None,
    detail: str,
) -> ServerResult:
    executed = len(progress.reports)
    partial = {
        "done": executed - progress.failed,
        "failed": progress.failed,
        "notRun": len(checked) - executed,
    }
    journal_ref = {"group": group} if group is not None else None
    result, hint = _result_of(checked, args, progress, group, facts, target, detail)
    if progress.stopped_at is not None and progress.last_failure is not None:
        failing = progress.last_failure["step"]
        fail_closed = progress.fail_closed
        raise ServerOpError(
            as_error_code(failing["errorCode"]),
            f"plan stopped at step {progress.stopped_at} ({failing['op']}): {failing.get('error')}"
            + (f" -- {fail_closed['note']}" if fail_closed is not None else ""),
            result=result,
            hint=hint,
            affected=progress.affected,
            partial=partial,
            journal=journal_ref,
        )
    return ServerResult(
        result=result,
        affected=progress.affected,
        partial=partial,
        journal=journal_ref,
    )


def _run(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    steps: list[Any] = list(args["steps"])
    on_error = args.get("on_error", ON_ERROR_ABORT)
    label = args.get("label")
    checked = [check_step(ctx, i, raw) for i, raw in enumerate(steps)]
    check_bundle(checked)
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
    detail = args.get("result_detail", "auto")
    if detail == "auto":
        writes = sum(1 for s in checked if s.op is not None and s.op.effect != "read")
        if ctx.response_mode == "compact":
            detail = MIXED_DETAIL
        else:
            detail = "summary" if writes > AUTO_SUMMARY_WRITE_STEPS else "full"
    progress = Progress(detail=detail, keep=frozenset(r.step_id for s in checked for r in s.refs))

    check = args_checker(ctx)

    def resolve(step: Step, envelopes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
        return plan_refs.prepare(step, envelopes, check, plan_bundle.SKIPPED_KEY)

    def run(runner: Callable[[Step], dict[str, Any]]) -> None:
        _loop(checked, on_error, progress, runner, resolve)

    if not has_writes:
        run(runner_for(ctx))
        return _answer(checked, args, progress, None, None, None, detail)

    def open_group() -> str:
        group = new_id("g")
        problem = ctx.journal.append_group(
            group,
            "plan.run",
            label=label,
            onError=on_error,
            steps=[s.op_name for s in checked],
            late=[s.index for s in checked if s.late or s.has_refs],
        )
        if problem is not None:
            raise journal_refusal(ctx, "plan not run (nothing was executed): ", problem)
        return group

    with ctx.write_lock:  # nobody else writes (or is classified by journal.check) while it runs
        done = run_in_group(
            ctx, checked, progress, open_group=open_group, label=label, on_error=on_error, run=run
        )
    return _answer(checked, args, progress, done.group, done.facts, done.target, detail)


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
                "every step's {index, op, ok, errorCode, error, result, journal} "
                "(result_detail='summary': {index, op, ok, errorCode, brief}, a failed step "
                "{index, op, ok, errorCode, error, result}), "
                "partial = {done, failed, notRun}; steps before it stay applied. "
                "on_error='continue' is allowed only when every step is a read. If a write "
                "ends without an answer (TIMEOUT / BRIDGE_LOST / ...) the plan stops at once "
                "whatever on_error says (result.failClosed) and this server refuses writes "
                "until journal.check. Ops that take their own checkpoint and journal group (a "
                "destructive eval.run, batch.load_setup, journal.restore_checkpoint), and "
                "batch.iterate and batch.create_shelf_reel (shelf reels and iterations are "
                "outside a Batch setup, so a restore would leave them), are refused as steps -- "
                "call them on their own. So is render.batch: a "
                "Foreground "
                "render occupies Flame for as long as it runs and its output "
                "(files on disk, clips in the shelf reel) cannot be undone. So is render.clip "
                "(the Timeline FX it renders cannot be undone either), and so is render.export "
                "(the files it writes stay), and render.still. So is project.save "
                "(a project save cannot be undone). So are "
                "desktop.create and desktop.copy: their revert deletes what they made, which a "
                "plan's group (reverted by putting values back) does not do. Nor can "
                "mediahub.import, desktop.duplicate, desktop.move, desktop.arrange, "
                "desktop.reorder, desktop.delete and desktop.reformat (deleting / moving back / "
                "copying back / restoring a stashed copy), segment.copy_to_media_panel and "
                "segment.match (deleting the clip they made), timeline.create_sequence (deleting "
                "the sequence it made), journal.purge_checkpoints, or "
                "segment.create_connection / remove_connection / sync_connections (their "
                "connections may reach other sequences; each journals an intent but has no "
                "revert). "
                "All the plan's writes share one "
                "journal group "
                "(envelope journal.group, `label` recorded on it): journal.revert(group) puts "
                "them all back, in reverse order. "
                "Timeline writes: a plan that holds a step of a Timeline-checkpointed op "
                "(segment.trim, segment.slip, segment.replace, segment.create_effect / "
                "delete_effect / set_effect / effect_load_setup / edit_effect_setup, "
                "timeline.overwrite / insert / "
                "cut / create_version / create_track / delete_segments / move_track / shorten) is "
                "one checkpointed write "
                "on one sequence / clip instead. Every "
                "write step must be to that sequence (a Timeline-checkpointed op, or "
                "segment.set_attributes of one of its segments); a write to another sequence, or "
                "of another kind (desktop.rename, batch / node writes, timeline.checkpoint ...), "
                "is refused before anything runs (INVALID_ARGS: the ways of reverting differ, so "
                "one restore could not undo it; this includes a Batch write) -- reads of any "
                "sequence are fine. "
                "The segment "
                "selectors of "
                "every write step are first resolved in one Flame read (index / record_in / "
                "expect_source_name; the sequence may be given by any selector kind, it is pinned "
                "by its wiretap id): if one does not resolve nothing runs, nothing is copied and "
                "nothing is journaled (SELECTOR_EMPTY / CONFLICT, result.preResolution = "
                "[{index, op, errorCode, error}]). A step may carry `late: true` (next to op and "
                "args): it is left out of that resolution and resolved when the step runs -- use "
                "it for a step that points at a segment an earlier step makes or moves (a head "
                "trim without ripple moves record_in); if it does not resolve then, the plan stops "
                "there and the steps before it stay applied (revertible). The sequence is "
                "copied into the stash once (timeline.checkpoint), the steps run without "
                "checkpoints of their own, and journal.revert(group) is one restore of that copy "
                "(only while the sequence still has result.timeline.fingerprintAfter; force "
                "overrides): it also brings back the plan's segment.set_attributes steps, which "
                "are not written back value by value. The restored sequence has a new uid and "
                "wiretap id and is at the end of its reel. result.timeline = {target, checkpoint, "
                "stash, fingerprintBefore, fingerprintAfter, changed, resolved, note}. A plan "
                "that stopped before any write ran (its first write step's own check refused "
                "it) is not revertible (PRECONDITION_UNMET). A write "
                "step that ends without an answer stops the plan as above, and journal.check "
                "decides the plan's intent from the sequence's fingerprint (before = not_landed, "
                "anything else = diverged). "
                "Batch writes: a plan that holds a step of a Batch-checkpointed op (node.create, "
                "node.delete, node.connect, node.disconnect, node.duplicate, node.load_setup, "
                "node.edit_setup, "
                "action.create_node, action.connect, batch.create_reel, "
                "batch.add_clips, batch.organize) is one "
                "checkpointed write on one Batch group instead (result.batch). Every write step "
                "must be to that group (such an op, or node.set_attributes / "
                "action.set_attributes of one of its nodes); a write to another group, or of "
                "another kind (a Timeline write, desktop.rename, batch.set_attributes ...), is "
                "refused before anything runs (INVALID_ARGS). The node selectors of every write "
                "step are first resolved in one Flame read: if one does not resolve, nothing "
                "runs, nothing is saved and nothing is journaled (SELECTOR_EMPTY / "
                "SELECTOR_AMBIGUOUS, result.preResolution), and a node.create `name` that "
                "another node of the group has is refused the same way unless an earlier step "
                "deletes or renames a node. A step that points at a node an earlier step makes "
                "is left to be resolved when the step runs: by itself "
                "(result.batch.resolved[].late = 'implied') when "
                "an earlier node.create of the same group, addressed by the same selector, gave "
                "that node `name`; with `late: true` for a node left to Flame's default name, a "
                "node.duplicate or a rename. The check each op makes before it writes (type, "
                "sockets, current group) runs when the step runs, so a step that fails it "
                "stops the plan there with the earlier steps applied. A node.create that makes "
                "the node under another name than `name` stops the plan the same way (the hint "
                "names it: point later steps at it, or use `late: true`). The group is saved "
                "once (batch.checkpoint), the steps run without checkpoints of their own, and "
                "journal.revert(group) is one restore of that setup (only while the group still "
                "has result.batch.fingerprintAfter; force overrides): it also brings back the "
                "plan's node.set_attributes / action.set_attributes steps, and it is a Batch "
                "restore with its side effects (new uids for reel clips and Clip nodes; "
                "result.timelineFxLost); with a value-write step it also refuses (CONFLICT) "
                "when an attribute value of the group changed after the plan, and a plan that "
                "stopped before any write ran is not revertible (PRECONDITION_UNMET). A "
                "batch.add_clips step into a shelf reel is refused "
                "when the step runs (PRECONDITION_UNMET, nothing written; shelf reels are outside "
                "a setup, so the revert would leave the clips). A plan of "
                "node.set_attributes steps alone is not bundled. result.batch = {target, "
                "checkpoint, checkpointPath, fingerprintBefore, fingerprintAfter, changed, "
                "resolved, note}. A write step that ends without an answer stops the plan as "
                "above, and journal.check decides the plan's intent from the group's fingerprint. "
                "A step's result too big for the answer "
                "(over "
                f"{PLAN_STEP_RESULT_BYTES} bytes, or beyond {PLAN_TOTAL_RESULT_BYTES} in "
                "total) is replaced by {omitted, bytes, note}, plus the brief of an op that has "
                "one (the same brief 'summary' gives); result.resultsOmitted lists them. "
                "A big plan should use result_detail='summary' (the default 'auto' does so above "
                f"{AUTO_SUMMARY_WRITE_STEPS} write steps; with compact responses 'auto' decides "
                "per step instead: a read step keeps its result, a successful write step is "
                "its brief only, a failed step keeps its error and result, result.detail is "
                "'mixed' and result.resultsNote says so): each step is then only its ok, "
                "errorCode and a small brief (the position of the segment it changed, the "
                "attributes it applied, the node a node.create / node.duplicate made, or whether "
                "batch.compare_setup found a change and in how many files), with "
                "no per-step result or journal, except that a "
                "failed step keeps its error and result (result.detail says which was used; "
                "the timeline / batch block stays, less its `resolved` list (also in a 'mixed' "
                "plan), and "
                "journal.revert(group) works as ever); 'full' keeps every step's result. A "
                "brief's position is "
                "{version, track, index, recordIn} without selectors: build a selector from the "
                "sequence, the track and recordIn. "
                "References: a step may carry `id` (letters, digits and _; s0, s1 ... by "
                "default, and the answer names each step by it). In a later step's args, a "
                'string "{{ steps.<id>.result.<path> }}" stands for a value of that step\'s '
                "result (path: dotted keys, [n], [*]; nothing is evaluated). A string that is "
                "only the reference becomes the value itself, type kept (a selector, a number, a "
                "list); a reference inside a longer string is put in as text. A repeated or "
                "unknown id, or a reference to the step itself or a later one, is INVALID_ARGS "
                "and nothing runs. A reference with no value when its step is due stops the plan "
                "before that step is sent (that step's result has reference and reached); the "
                "earlier steps stay applied. In a Timeline or Batch plan a reference cannot point "
                "at the write's target (the sequence or the Batch): refused before anything runs. "
                "To run the same steps for each item of a list use plan.for_each. "
                "A plan buys one up-front check of the whole plan and one group to undo. "
                "affected is the sum over the steps."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="steps",
                    type="array",
                    description="[{id?, op, args, late?}, ...] -- at most "
                    f"{MAX_PLAN_STEPS}; each step's op and args as flame_do takes them. `late: "
                    "true` leaves a Timeline or Batch write out of the up-front resolution of the "
                    "plan's segment / node selectors: use it when the step points at a segment "
                    "or node an earlier step of the plan makes or moves (a Batch node left to "
                    "Flame's default name, a duplicate, a rename; a node an earlier node.create "
                    "gave `name` needs no `late`). It is resolved when the step runs; if it "
                    "does not resolve then the plan stops there, the earlier steps stay applied "
                    "and journal.revert(group) puts the sequence / group back.",
                    required=True,
                    # no item_type: a step that is not an object is reported by the plan's own
                    # check, with the others, as {index, problems}
                ),
                OperationParam(
                    name="on_error",
                    type="string",
                    description="'abort' (default) or 'continue' (only for all-read plans).",
                    required=False,
                    default=ON_ERROR_ABORT,
                ),
                OperationParam(
                    name="result_detail",
                    type="string",
                    description="'auto' (default): 'summary' when the plan has more than "
                    f"{AUTO_SUMMARY_WRITE_STEPS} write steps, else 'full'; with compact "
                    "responses it is per step ('mixed'): reads in full, successful writes as "
                    "their brief, failures in full. 'full': every step "
                    "with its result (cut at a size budget) and journal. 'summary': each step "
                    "is {index, op, ok, errorCode, brief} only (a failed step: error and result, "
                    "not brief).",
                    required=False,
                    default="auto",
                    enum=RESULT_DETAILS,
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
