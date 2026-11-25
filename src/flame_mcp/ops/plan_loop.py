"""`plan.for_each` (thread=server): the same steps for each item of a list.

The item list is given (`items`) or read from one read op (`source`), narrowed by `where` and
named by `key`. The steps are checked once, with the plan.run rules and `{{ item.<path> }}`, before
anything is sent; each item then runs them in order, one item after another, each step through
`ctx.do`.

A loop of reads runs in one call. A loop with a write step expands every item's steps first (a bad
one stops the whole call), then runs in two calls -- `dry_run`, then `approve` with its digest --
unless the server was started with FLAME_MCP_LOOP_UNATTENDED=1 and every step can be put back
(`plan_loop_confirm`); it keeps one parent journal group with a child group per item or per step
(`plan_loop_run`), reverted by `journal.revert(parent)` (`plan_loop_revert`).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from flame_mcp import loop_limits as limits_mod
from flame_mcp import loop_writes, plan_loop_confirm, plan_loop_run, plan_refs
from flame_mcp.conditions import check_conditions, compile_conditions, matches
from flame_mcp.errors import as_error_code
from flame_mcp.journal import new_id
from flame_mcp.loop_limits import DEFAULT_LOOP_LIMITS, LoopLimits
from flame_mcp.loop_writes import Kind
from flame_mcp.opschema import validate_op_args
from flame_mcp.plan_loop_report import (
    NOT_RUN,
    ItemState,
    LoopStep,
    Run,
    below_result,
    loop_refusal,
    parse_return,
    record_step,
    run_with_budget,
    send_step,
    without_return,
)
from flame_mcp.plan_progress import (
    ON_ERROR_ABORT,
    ON_ERROR_CONTINUE,
    ON_ERROR_VALUES,
    journal_refusal,
    runner_for,
)
from flame_mcp.plan_step import (
    MAX_PLAN_STEPS,
    Step,
    args_checker,
    check_step,
    policy_only_denial,
    step_issues,
)
from flame_mcp.policy import deny_operation
from flame_mcp.refs import (
    RefsError,
    RefsResolveError,
    leaves,
    parse_path,
    walk,
)
from flame_mcp.registry import Registry
from flame_mcp.runtime import ServerContext
from flame_mcp.types import OperationParam, OperationSpec, ServerOpError, ServerResult

__all__ = ["below_result", "register"]

_SOURCE_KEYS: Final[frozenset[str]] = frozenset({"op", "args", "path"})
_WRITE_ON_ERROR: Final[str] = (
    "on_error='continue' is only allowed when every step is a read: a write that failed must "
    "stop the loop"
)


def _check_steps(ctx: ServerContext, raw_steps: list[Any]) -> list[LoopStep]:
    checked: list[LoopStep] = []
    for index, raw in enumerate(raw_steps):
        is_dict = isinstance(raw, dict)
        step = check_step(ctx, index, without_return(raw), loop_writes.UP_FRONT)
        spec, problem = parse_return(raw.get("return") if is_dict else None)
        if problem is not None:
            step.problems.append(problem)
        if is_dict and "late" in raw:
            step.problems.append("'late' is not accepted in plan.for_each")
        kind = loop_writes.judge(step, raw.get("args") if is_dict else None)
        checked.append(LoopStep(step, spec, kind))
    plan_refs.check_plan([s.step for s in checked])
    return checked


def _step_problems(checked: list[Step]) -> tuple[list[str], list[dict[str, Any]]]:
    issues = step_issues(checked)
    return [f"step {i['index']} ({i['op']}): {'; '.join(i['problems'])}" for i in issues], issues


def _source_problems(ctx: ServerContext, source: Any) -> tuple[list[str], bool]:
    """The problems of `source`, and whether the only one is a policy denial."""
    if not isinstance(source, dict) or set(source) - _SOURCE_KEYS or "op" not in source:
        return ["source must be {op, args, path}"], False
    if not isinstance(source.get("path"), str):
        return ["source.path is required: a path in the op's envelope, e.g. result.items[*]"], False
    try:
        below_result(parse_path(source["path"]))
    except RefsError as exc:
        return [f"source.path: {exc}"], False
    name = source["op"]
    op = ctx.registry.get(name) if isinstance(name, str) else None
    if op is None:
        return [f"source.op: no operation named {name!r} is registered"], False
    if loop_writes.is_never(op):
        return [f"source.op: {op.name} cannot be the source of a loop"], False
    if op.effect != "read":
        return [f"source.op: {op.name} must be a read (effect=read), it is {op.effect}"], False
    denial = deny_operation(op, ctx.policy)
    if denial is not None:
        return [f"source.op: denied by policy: {denial.message}"], True
    validation = validate_op_args(op, source.get("args"))
    if not validation.ok:
        return [f"source.args: {issue.path}: {issue.message}" for issue in validation.issues], False
    return [], False


def _refuse_a_partial_page(op_name: str, result: Any) -> None:
    """A listing that says more items follow its page (`truncated`, or a `nextOffset`) would loop
    over part of the list without a word; refuse it before any step runs."""
    if not isinstance(result, dict):
        return
    if result.get("truncated") is not True and result.get("nextOffset") is None:
        return
    page = {k: result.get(k) for k in ("total", "offset", "shown", "nextOffset")}
    raise ServerOpError(
        "INVALID_ARGS",
        f"{NOT_RUN}source {op_name} returned {page['shown']} of {page['total']} items "
        "(more pages follow): raise source.args.limit or narrow the source so that one page "
        "holds every item",
        result={"ran": False, "source": {"op": op_name, **page}},
    )


def _read_source(ctx: ServerContext, source: Mapping[str, Any]) -> list[Any]:
    env = ctx.do(source["op"], source.get("args") or {})
    if env.get("ok") is not True:
        raise ServerOpError(
            as_error_code(env.get("errorCode")),
            f"{NOT_RUN}source {source['op']} failed: {env.get('error')}",
            result={"ran": False, "source": {"op": source["op"], "error": env.get("error")}},
            hint=env.get("hint"),
        )
    _refuse_a_partial_page(source["op"], env.get("result"))
    tokens = below_result(parse_path(source["path"]))
    try:
        values = leaves(env.get("result"), tokens)
    except RefsResolveError as exc:
        raise loop_refusal([f"source.path {source['path']!r} cannot be read: {exc}"]) from exc
    if len(values) == 1 and isinstance(values[0], list) and not any(k == "all" for k, _ in tokens):
        return list(values[0])
    return values


def _keys_of(items: list[dict[str, Any]], key_path: str) -> tuple[list[Any], list[str]]:
    tokens = parse_path(key_path)
    keys: list[Any] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        try:
            value = walk(item, tokens)
        except RefsResolveError as exc:
            problems.append(f"item {index} has no key at {key_path!r}: {exc}")
            continue
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            problems.append(f"item {index}: the key {key_path!r} must be a string or a number")
            continue
        if str(value) in seen:
            problems.append(f"items {seen[str(value)]} and {index} have the same key {value!r}")
            continue
        seen[str(value)] = index
        keys.append(value)
    return keys, problems


def _keep(steps: list[LoopStep]) -> frozenset[str]:
    """The ids of the steps some later step refers to."""
    return frozenset(r.step_id for s in steps for r in s.step.refs if not r.item)


@dataclass(frozen=True)
class _Steps:
    """The checked steps of the loop, with what every item's run of them shares."""

    ctx: ServerContext
    plan: list[LoopStep]
    keep: frozenset[str]
    check: Callable[[plan_refs.SendableStep, Any], plan_refs.Checked]


def _run_item(steps: _Steps, key: Any, item: dict[str, Any], run: Run) -> bool:
    """Runs the steps for one item; False when one failed (the item ends there)."""
    envelopes: dict[str, dict[str, Any]] = {}
    reports: list[dict[str, Any]] = []
    entry = {"key": key, "status": ItemState.DONE, "steps": reports}
    run.items.append(entry)
    runner = runner_for(steps.ctx)
    for loop_step in steps.plan:
        own = dataclasses.replace(loop_step.step, args=dict(loop_step.step.args))
        env, _ = send_step(own, runner, envelopes, steps.check, (item,))
        record_step(own, env, loop_step.ret, run, key, reports, envelopes, steps.keep)
        if env.get("ok") is not True:
            entry["status"] = ItemState.FAILED
            return False
    return True


def _run_items(steps: _Steps, items: list[dict[str, Any]], keys: list[Any], abort: bool) -> Run:
    return run_with_budget(
        keys,
        steps.ctx.loop_limits.budget_s,
        lambda run, position: _run_item(steps, keys[position], items[position], run),
        abort=abort,
    )


def _check_limits(count: int, steps: int, limits: LoopLimits) -> None:
    problems: list[str] = []
    if count > limits.max_items:
        problems.append(
            f"{count} items: at most {limits.max_items} may run in one call "
            f"({limits_mod.MAX_ITEMS_ENV} raises it)"
        )
    if count * steps > limits.max_steps:
        problems.append(
            f"{count} items x {steps} steps = {count * steps} steps: at most {limits.max_steps} "
            f"may run in one call ({limits_mod.MAX_STEPS_ENV} raises it)"
        )
    if problems:
        raise loop_refusal(problems, items=count, steps=steps)


def _static_problems(args: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if ("items" in args) == ("source" in args):
        problems.append("give exactly one of 'items' and 'source'")
    if args.get("on_error", ON_ERROR_ABORT) not in ON_ERROR_VALUES:
        problems.append(f"on_error must be one of {list(ON_ERROR_VALUES)}")
    steps = args["steps"]
    if not steps or len(steps) > MAX_PLAN_STEPS:
        problems.append(f"steps must hold 1 to {MAX_PLAN_STEPS} steps, got {len(steps)}")
    try:
        if any(k == "all" for k, _ in parse_path(args["key"])):
            problems.append("key must name one value of the item, without [*]")
        if "where" in args:
            check_conditions(args["where"], "where")
    except RefsError as exc:
        problems.append(str(exc))
    return problems


def _item_list(ctx: ServerContext, args: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = _read_source(ctx, args["source"]) if "source" in args else list(args["items"])
    bad = [i for i, item in enumerate(raw) if not isinstance(item, dict)]
    if bad:
        raise loop_refusal([f"items {bad} are not objects: each item must be an object"])
    if "where" not in args:
        return raw
    conditions = compile_conditions(args["where"])
    return [item for item in raw if matches(conditions, item)]


def _refuse_steps(
    problems: list[str], source_problems: list[str], source_denied: bool, checked: list[LoopStep]
) -> None:
    steps = [s.step for s in checked]
    step_problems, issues = _step_problems(steps)
    if not (problems or source_problems or step_problems):
        return
    only_policy, denied = policy_only_denial(
        steps,
        other_problems=bool(problems) or (bool(source_problems) and not source_denied),
        other_denied=source_denied,
    )
    raise loop_refusal(
        [*problems, *source_problems, *step_problems],
        code="POLICY_DENIED" if only_policy else "INVALID_ARGS",
        hint=denied.hint if only_policy and denied is not None else None,
        issues=issues,
    )


def _select(
    args: Mapping[str, Any], keys: list[Any], items: list[dict[str, Any]]
) -> tuple[list[Any], list[dict[str, Any]]]:
    """The items `only` names, in the order of the list; all of them without `only`."""
    only = args.get("only")
    if only is None:
        return keys, items
    unknown = [k for k in only if k not in keys]
    if unknown:
        raise loop_refusal(
            [f"only names {unknown}, which the list does not hold (it holds {keys})"],
            keys=keys,
        )
    chosen = [(k, i) for k, i in zip(keys, items, strict=True) if k in only]
    return [k for k, _ in chosen], [i for _, i in chosen]


def _check_expect_count(args: Mapping[str, Any], keys: list[Any], count: int) -> None:
    expected = args.get("expect_count")
    if expected is not None and expected != count:
        raise ServerOpError(
            "INVALID_ARGS",
            f"{NOT_RUN}expect_count is {expected} but {count} item(s) remain",
            result={"ran": False, "expectCount": expected, "count": count, "keys": keys},
        )


@dataclass(frozen=True)
class _Prepared:
    """A call that passed every check: the steps, the list after `where` (what the digest
    covers) and the items that will run (after `only`)."""

    steps: list[LoopStep]
    narrowed: list[dict[str, Any]]
    keys: list[Any]
    items: list[dict[str, Any]]

    @property
    def writes(self) -> bool:
        return any(s.kind is not Kind.READ for s in self.steps)


def _prepare(args: Mapping[str, Any], ctx: ServerContext) -> _Prepared:
    """Checks the call and reads the list. Raises a refusal (nothing ran) when anything is wrong."""
    problems = _static_problems(args)
    source_problems, source_denied = (
        _source_problems(ctx, args["source"]) if "source" in args else ([], False)
    )
    checked = _check_steps(ctx, list(args["steps"]))
    if args.get("on_error") == ON_ERROR_CONTINUE and any(s.kind is not Kind.READ for s in checked):
        problems.append(_WRITE_ON_ERROR)
    _refuse_steps(problems, source_problems, source_denied, checked)
    narrowed = _item_list(ctx, args)
    all_keys, key_problems = _keys_of(narrowed, args["key"])
    if key_problems:
        raise loop_refusal(key_problems)
    _check_expect_count(args, all_keys, len(narrowed))
    keys, items = _select(args, all_keys, narrowed)
    _check_limits(len(items), len(checked), ctx.loop_limits)
    return _Prepared(checked, narrowed, keys, items)


def _digest(args: Mapping[str, Any], ctx: ServerContext, prep: _Prepared) -> tuple[str, int, str]:
    """The digest of the call (without the runs started with it), how many runs were, and the
    digest the server accepts now."""
    base = plan_loop_confirm.digest_of(args, prep.narrowed)
    runs = plan_loop_confirm.runs_of(ctx.journal.read_all().records, base)
    return base, runs, plan_loop_confirm.salted(base, runs)


def _for_each(args: Mapping[str, Any], ctx: ServerContext) -> ServerResult:
    prep = _prepare(args, ctx)
    if prep.writes:
        return _write_loop(args, ctx, prep)
    return _read_loop(args, ctx, prep)


def _read_loop(args: Mapping[str, Any], ctx: ServerContext, prep: _Prepared) -> ServerResult:
    if args.get("dry_run") is True:
        digest = _digest(args, ctx, prep)[2]
        shown = [(k, prep.steps) for k in prep.keys]
        return _preview(args, prep, shown, digest, ctx.loop_limits, False)
    steps = _Steps(ctx, prep.steps, _keep(prep.steps), args_checker(ctx))
    abort = args.get("on_error", ON_ERROR_ABORT) == ON_ERROR_ABORT
    return _answer(args, _run_items(steps, prep.items, prep.keys, abort))


def _write_loop(args: Mapping[str, Any], ctx: ServerContext, prep: _Prepared) -> ServerResult:
    base, runs, digest = _digest(args, ctx, prep)
    expanded = plan_loop_run.expand(
        ctx, args["steps"], [s.ret for s in prep.steps], prep.keys, prep.items
    )
    approval = plan_loop_confirm.needs_approval(prep.steps, ctx.loop_limits.unattended)
    shown = [(ex.key, ex.steps) for ex in expanded]
    preview = _preview(args, prep, shown, digest, ctx.loop_limits, approval)
    if args.get("dry_run") is True:
        return preview
    if approval:
        plan_loop_confirm.refuse_without_approval(args, base, runs, preview.result)
    return _run_writes(args, ctx, expanded, prep, base if approval else None, runs, preview)


def _preview(
    args: Mapping[str, Any],
    prep: _Prepared,
    shown: list[tuple[Any, list[LoopStep]]],
    digest: str,
    limits: LoopLimits,
    approval: bool,
) -> ServerResult:
    result = plan_loop_confirm.preview(
        label=args.get("label"),
        keys=prep.keys,
        steps=[str(s.step.op_name) for s in prep.steps],
        expanded=shown,
        digest=digest,
        unattended=limits.unattended,
        approval=approval,
    )
    return ServerResult(result=result)


def _refuse_if_guarded(ctx: ServerContext) -> None:
    denial = ctx.guard.denial()
    if denial is not None:
        raise ServerOpError(
            "POLICY_DENIED",
            f"{NOT_RUN}{denial.message}",
            result={"ran": False},
            hint=denial.hint,
        )


def _refuse_if_approval_used(
    args: Mapping[str, Any], ctx: ServerContext, used: str | None, runs: int, preview: ServerResult
) -> None:
    """Another call with this approval started while this one waited for the lock."""
    if used is None:
        return
    now_runs = plan_loop_confirm.runs_of(ctx.journal.read_all().records, used)
    if now_runs == runs:
        return
    now = plan_loop_confirm.salted(used, now_runs)
    plan_loop_confirm.refuse_without_approval(
        args, used, now_runs, {**preview.result, "digest": now}
    )


def _open_parent_or_refuse(
    ctx: ServerContext, parent: str, label: Any, prep: _Prepared, approval: str | None
) -> None:
    problem = plan_loop_run.open_parent(
        ctx,
        parent,
        label,
        prep.keys,
        [str(s.step.op_name) for s in prep.steps],
        ctx.loop_limits.unattended,
        approval,
    )
    if problem is not None:
        raise journal_refusal(ctx, NOT_RUN, problem)


def _run_writes(
    args: Mapping[str, Any],
    ctx: ServerContext,
    expanded: list[plan_loop_run.ExpandedItem],
    prep: _Prepared,
    used: str | None,
    runs: int,
    preview: ServerResult,
) -> ServerResult:
    label = args.get("label")
    parent = new_id("g")
    loop = plan_loop_run.WriteLoop(ctx, parent, label, _keep(prep.steps))
    with ctx.write_lock:  # nobody else writes (or is classified by journal.check) meanwhile
        _refuse_if_guarded(ctx)
        if not expanded:
            return _answer(args, Run())
        _refuse_if_approval_used(args, ctx, used, runs, preview)
        _open_parent_or_refuse(ctx, parent, label, prep, used)
        run = plan_loop_run.run_items(loop, expanded)
    return _answer(args, run, parent)


def _stop_hint(parent: str | None, run: Run, not_run: list[Any]) -> str:
    if parent is None:
        return ""
    parts = [
        f"journal.revert(group={parent!r}) puts the finished items back, newest first "
        "(a child that made output that cannot be put back stays, result.kept of the revert)."
    ]
    if run.unknown is not None:
        parts.append(f"Run journal.check(group={parent!r}) first: item {run.unknown!r} is unknown.")
    if not_run:
        parts.append(
            "To go on once the cause is fixed, run dry_run again with only=<the keys left> "
            "(result.notRun, and the failed item if it must run again), then approve."
        )
    return " ".join(parts)


def _stop_message(run: Run, step: Mapping[str, Any]) -> str:
    note = f" -- {run.fail_closed['note']}" if run.fail_closed is not None else ""
    return (
        f"loop stopped at item {run.stopped_at!r} step {step['id']} ({step['op']}): "
        f"{step.get('error')}{note}"
    )


def _answer(args: Mapping[str, Any], run: Run, parent: str | None = None) -> ServerResult:
    keys = [i["key"] for i in run.items]
    not_run = [i["key"] for i in run.items if i["status"] == ItemState.NOT_RUN]
    result: dict[str, Any] = {
        "label": args.get("label"),
        "onError": args.get("on_error", ON_ERROR_ABORT),
        "total": len(keys),
        "keys": keys,
        "items": run.items,
        "stoppedAt": run.stopped_at,
        "notRun": not_run,
        "budgetStop": run.budget_stop,
        "resultsOmitted": run.omitted,
    }
    journal = None
    if parent is not None:
        result.update({"group": parent, "unknown": run.unknown, "failClosed": run.fail_closed})
        journal = {"group": parent}
    partial = {"done": run.done, "failed": run.failed, "notRun": len(not_run)}
    failure = run.last_failure
    if run.stopped_at is not None and failure is not None:
        step = failure["step"]
        hint = " ".join(h for h in (failure["hint"], _stop_hint(parent, run, not_run)) if h)
        raise ServerOpError(
            as_error_code(step.get("errorCode")),
            _stop_message(run, step),
            result=result,
            hint=hint or None,
            affected=run.affected,
            partial=partial,
            journal=journal,
        )
    return ServerResult(result=result, affected=run.affected, partial=partial, journal=journal)


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name="plan.for_each",
            category="plan",
            description=(
                "Run the same steps for each item of a list, one item after another: a loop of "
                "reads in one call, or of writes in two (dry_run, then approve). "
                "Arguments: items (or source: {op, args, path}), key, where, expect_count, "
                "steps = [{id, op, args, return}], on_error, label, dry_run, approve, only. "
                "{{ item.<path> }} in a step's args is a value of the item, "
                "{{ steps.<id>.result.<path> }} a value of an earlier step of the same item "
                "(the references of plan.run). source.path and the paths of a step's `return` "
                "start at `result.` (result.items[*].name); return: [] keeps that step's result "
                "out of the answer while references still use it. A source that says more items "
                "follow its page (truncated) is refused before any step: give it a limit that "
                "holds the whole list, or narrow it. Every item's steps are checked "
                "before anything runs (an unknown op, a malformed reference, a refused argument: "
                "nothing runs). The values an item puts into a step's arguments are checked when "
                "that step is sent: in a loop of writes every item is expanded and checked "
                "first, so one bad value and nothing runs; in a loop of reads a bad value "
                "fails that item (the items before it have run), and on_error decides whether "
                "the loop stops or goes on. With on_error='continue' an ok answer can hold "
                "failed items; read partial.failed and each item's status. "
                "Writes are confirmed by default: dry_run=true reads the list and writes nothing "
                "(no journal either) and returns result.keys, result.count, result.firstItem "
                "(its steps with the arguments filled in), result.review (every item's step that "
                "rewrites or cannot be put back; a reference to an earlier step stays as "
                "written) and result.digest. Show that list to the user, wait for the answer, "
                "then call again with the same arguments and approve=<digest>. The server reads "
                "the list again; if the digest differs nothing runs (PRECONDITION_UNMET, the new "
                "list and digest in the result). An approval works once: a second run with it is "
                "refused. No argument turns the confirmation off. Only the server's setting "
                f"{limits_mod.UNATTENDED_ENV}=1 lets a loop whose steps can all be put back run "
                "in one call (result.mode says which mode the server is in). "
                "Write steps, by what a revert can do: any write plan.run accepts (value writes; "
                "checkpointed Timeline / Batch writes, one group per run of them in an item, "
                "never a Timeline and a Batch write in one item); desktop.copy / create / "
                "duplicate / move / reorder / arrange / sync_record_tc, library.create, "
                "mediahub.import, segment.match / copy_to_media_panel, timeline.create_sequence "
                "(put back by their own revert; each step is a group of its own); "
                "desktop.delete / reformat, batch.load_setup, journal.restore_checkpoint "
                "(they rewrite or delete what is there; approve needed even when unattended); "
                "render.batch / clip / export / still, batch.iterate / create_shelf_reel (output "
                "that cannot be put back; approve needed even when unattended). "
                "allow_overwrite, force and overwrite set to true are refused in every mode, "
                "also when a reference brings them in. batch.delete, journal.purge_checkpoints, "
                "journal.revert, segment.create_connection / remove_connection / "
                "sync_connections, project.save, eval.run and plan.* are not steps: call them "
                "on their own. "
                "on_error='continue' is refused for a write loop. result.group is the parent "
                "group (envelope journal): journal.revert(group) puts the items back newest "
                "first, each by its own rule, stops at the first that cannot (result.remaining; "
                "call again to go on) and leaves render output (result.kept). "
                "result.items[].groups are the child groups; journal.revert on one puts back "
                "that item or step only. "
                "A write loop stops at the first failed item, and so does a write with no answer "
                "(TIMEOUT, BRIDGE_LOST, BRIDGE_UNAVAILABLE): that item is 'unknown' and writes "
                "are refused until journal.check. To read a stop: result.stoppedAt, "
                "result.notRun, each item's status (done, failed, unknown, not_run). Fix the "
                "cause, then call again with only=[keys] (dry_run, then approve); only must name "
                "keys that `where` leaves. When the time budget runs out between two items the "
                "answer is still ok: read result.budgetStop and result.notRun. "
                "A call is refused (nothing runs) beyond "
                f"{DEFAULT_LOOP_LIMITS.max_items} items or {DEFAULT_LOOP_LIMITS.max_steps} "
                f"expanded steps; the budget is {DEFAULT_LOOP_LIMITS.budget_s} seconds. "
                f"{limits_mod.MAX_ITEMS_ENV}, {limits_mod.MAX_STEPS_ENV} and "
                f"{limits_mod.BUDGET_ENV} change them."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="items",
                    type="array",
                    description="The items, objects. Give this or `source`.",
                    required=False,
                ),
                OperationParam(
                    name="source",
                    type="object",
                    description="{op, args, path}: one read op; `path` (e.g. result.items[*]) "
                    "picks the items from its envelope. Give this or `items`.",
                    required=False,
                ),
                OperationParam(
                    name="key",
                    type="string",
                    description="Path in an item of its name (a string or a number, unique).",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="where",
                    type="array",
                    description="[{path, equals | in | len}]: keep the items that meet all.",
                    required=False,
                ),
                OperationParam(
                    name="expect_count",
                    type="number",
                    description="Refuse (nothing runs) unless this many items remain.",
                    required=False,
                    integer=True,
                    minimum=0,
                ),
                OperationParam(
                    name="steps",
                    type="array",
                    description="[{id?, op, args, return?}]: per item, in order. `return` is a "
                    "list of paths of the step's result to put in the answer.",
                    required=True,
                ),
                OperationParam(
                    name="on_error",
                    type="string",
                    description="'abort' (default) or 'continue'.",
                    required=False,
                    default=ON_ERROR_ABORT,
                ),
                OperationParam(
                    name="label",
                    type="string",
                    description="A name for the loop.",
                    required=False,
                    non_empty=True,
                ),
                OperationParam(
                    name="dry_run",
                    type="boolean",
                    description="Read and expand only: answer with the list, the steps and the "
                    "digest; nothing is written or journaled.",
                    required=False,
                ),
                OperationParam(
                    name="approve",
                    type="string",
                    description="The digest of the dry_run, after the user has seen it.",
                    required=False,
                    non_empty=True,
                ),
                OperationParam(
                    name="only",
                    type="array",
                    description="Keys of the list (after where) to run; the others do not run.",
                    required=False,
                ),
            ),
            server_handler=_for_each,
        )
    )
