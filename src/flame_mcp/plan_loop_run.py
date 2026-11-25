"""The write half of `plan.for_each`: expanding the steps for every item, and running them.

Expansion (before anything is sent, so a bad item stops the whole call): each item's
`{{ item.<path> }}` is put into the steps' arguments, the steps are checked as `plan.run` checks
them (arguments, policy, the rules of a bundle, the ways of reverting), and the references to
earlier steps of the same item stay as written until the step is sent.

Running: one item after another, under `ctx.write_lock`. The journal gets one parent group for
the loop (a `group` record of kind `plan.for_each`, no intent of its own) and the child groups
below it, each named by a `group` record of its own that holds `parent`, in the order it ran:

  - a run of steps that are reads or writes `plan.run` accepts (value writes, checkpointed
    Timeline / Batch writes) is one child group, with the bundle of `plan.run` when it holds a
    checkpointed write: one checkpoint of that sequence / clip / Batch group, one restore to
    revert it. Several of these in one item are separate groups.
  - a step of an op that has a revert of its own kind (`loop_writes.has_own_group`) is a child
    group of its own: the group the op journals (an op that makes its own group, like
    `desktop.arrange`, gives it in its envelope).

The first failure stops the loop (no `continue`). A write that ends without an answer stops it as
in `plan.run`: the server refuses writes until `journal.check` and the item is `unknown`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Final, Unpack

from flame_mcp import loop_writes, plan_refs
from flame_mcp.journal import LOOP_GROUP_KIND, new_id
from flame_mcp.journal_records import GroupRunFields
from flame_mcp.loop_writes import Kind
from flame_mcp.plan_loop_confirm import mode_name
from flame_mcp.plan_loop_report import (
    ItemState,
    LoopStep,
    Return,
    Run,
    loop_refusal,
    record_step,
    run_with_budget,
    send_step,
    without_return,
)
from flame_mcp.plan_progress import (
    ON_ERROR_ABORT,
    Progress,
    check_bundle,
    run_in_group,
    runner_for,
)
from flame_mcp.plan_step import Step, args_checker, check_step
from flame_mcp.runtime import ServerContext
from flame_mcp.types import ServerOpError

MAX_REPORTED_PROBLEMS: Final[int] = 12
GROUP_KIND_RUN: Final[str] = "plan.for_each.run"
_CLOSED_NOTE: Final[str] = (
    "the write's outcome is unknown: this server refuses writes until journal.check has read "
    "Flame again; the rest of the loop was not run"
)
_BUNDLE_CLOSED_NOTE: Final[str] = (
    "the checkpointed write ended without an answer: this server refuses writes until "
    "journal.check has read Flame again"
)


@dataclass
class ExpandedItem:
    key: Any
    steps: list[LoopStep]


def _item_step(
    ctx: ServerContext, index: int, raw: Mapping[str, Any], item: Mapping[str, Any]
) -> tuple[Step, Any]:
    """The step read with the item's values put in, and the arguments it will be sent with."""
    body = without_return(raw)
    try:
        if isinstance(body.get("args"), dict):
            body["args"] = plan_refs.substitute_args(body["args"], None, item)
    except plan_refs.Unresolved as exc:
        step = Step(index, str(raw.get("op")))
        reason = str(exc) if exc.reference is None else f"{exc.reference}: {exc}"
        step.problems.append(f"a reference cannot be resolved: {reason}")
        return step, None
    return check_step(ctx, index, body, loop_writes.SENT), body.get("args")


def _expand_item(
    ctx: ServerContext,
    raws: Sequence[Mapping[str, Any]],
    returns: Sequence[Return | None],
    key: Any,
    item: Mapping[str, Any],
) -> tuple[ExpandedItem, list[str]]:
    steps: list[LoopStep] = []
    for index, raw in enumerate(raws):
        step, args = _item_step(ctx, index, raw, item)
        steps.append(LoopStep(step, returns[index], loop_writes.judge(step, args)))
    plain = [s.step for s in steps if s.kind in (Kind.READ, Kind.PLAIN)]
    check_bundle([s.step for s in steps], plain)
    problems = [
        f"item {key!r} step {s.step.index} ({s.step.op_name}): {p}"
        for s in steps
        for p in s.step.problems
    ]
    return ExpandedItem(key, steps), problems


def expand(
    ctx: ServerContext,
    raws: Sequence[Mapping[str, Any]],
    returns: Sequence[Return | None],
    keys: Sequence[Any],
    items: Sequence[Mapping[str, Any]],
) -> list[ExpandedItem]:
    """Every item's steps, checked. Raises a refusal (nothing was sent) naming the problems."""
    expanded: list[ExpandedItem] = []
    problems: list[str] = []
    for key, item in zip(keys, items, strict=True):
        one, found = _expand_item(ctx, raws, returns, key, item)
        expanded.append(one)
        problems.extend(found)
    if problems:
        shown = problems[:MAX_REPORTED_PROBLEMS]
        if len(problems) > len(shown):
            shown.append(f"... and {len(problems) - len(shown)} more")
        raise loop_refusal(shown, items=len(keys))
    return expanded


# ---- running -------------------------------------------------------------------------------


def sending_check(
    base: Callable[[plan_refs.SendableStep, Any], plan_refs.Checked],
) -> Callable[[plan_refs.SendableStep, Any], plan_refs.Checked]:
    """`base` and the overwrite refusal: what a reference put into a step's arguments is held to
    just before the step is sent."""

    def check(step: plan_refs.SendableStep, args: Any) -> plan_refs.Checked:
        checked = base(step, args)
        note = (
            loop_writes.overwrite_problem(step.op, checked.args)
            if step.op is not None and not checked.problems
            else None
        )
        if note is None:
            return checked
        return plan_refs.Checked(
            checked.args, [*checked.problems, note], checked.policy_denied, checked.hint
        )

    return check


@dataclass(frozen=True)
class WriteLoop:
    ctx: ServerContext
    parent: str
    label: Any
    keep: frozenset[str]
    """The ids of the steps some later step of the same item refers to."""

    @cached_property
    def check(self) -> Callable[[plan_refs.SendableStep, Any], plan_refs.Checked]:
        return sending_check(args_checker(self.ctx, loop_writes.SENT))


@dataclass
class _Item:
    ex: ExpandedItem
    entry: dict[str, Any]
    envelopes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def progress(self) -> Progress:
        return Progress(envelopes=self.envelopes)


def _name_child(
    loop: WriteLoop,
    group: str,
    item_key: Any,
    ops: list[str],
    step_ids: list[str],
    kind: Kind,
    group_kind: str | None = None,
    **fields: Unpack[GroupRunFields],
) -> str | None:
    """Writes the `group` record that puts `group` under the loop's parent; the reason it could
    not be written, else None."""
    problem = loop.ctx.journal.append_group(
        group,
        group_kind,
        parent=loop.parent,
        key=item_key,
        op=", ".join(dict.fromkeys(ops)),
        step=", ".join(step_ids),
        revertible=loop_writes.is_revertible(kind),
        **fields,
    )
    return None if problem is None else f"the journal cannot be written ({problem})"


def _group_entry(group: str, ops: list[str], kind: Kind) -> dict[str, Any]:
    return {"group": group, "ops": ops, "revertible": loop_writes.is_revertible(kind)}


def _segments(ex: ExpandedItem) -> list[list[int]]:
    """Positions of the steps: a run of reads and plain writes together, a step with a revert of
    its own kind alone."""
    segments: list[list[int]] = []
    for position, item in enumerate(ex.steps):
        if loop_writes.has_own_group(item.kind):
            segments.append([position])
        elif segments and not loop_writes.has_own_group(ex.steps[segments[-1][0]].kind):
            segments[-1].append(position)
        else:
            segments.append([position])
    return segments


def _mark_unknown(run: Run, key: Any, closed: dict[str, Any]) -> None:
    """The item `key` ended a write without an answer: the loop stops and writes are refused
    until `journal.check`."""
    if run.unknown is None:
        run.unknown = key
        run.fail_closed = run.fail_closed or closed


def _item_state(run: Run, progress: Progress) -> ItemState:
    if run.unknown is not None:
        return ItemState.UNKNOWN
    if progress.stopped_at is None and progress.reports:
        return ItemState.DONE
    return ItemState.FAILED


def _with_trouble(env: dict[str, Any], trouble: str) -> dict[str, Any]:
    """`env` with the reason the child group could not be recorded. An answer that was already
    a failure keeps its code (a write with no answer must stay one: the loop and journal.check
    go by it); a success becomes POLICY_DENIED."""
    hint = " ".join(h for h in (env.get("hint"), trouble) if h)
    if env.get("ok") is True:
        return {**env, "ok": False, "errorCode": "POLICY_DENIED", "error": trouble, "hint": hint}
    return {**env, "error": f"{env.get('error')}; {trouble}", "hint": hint}


def _unrecorded(group: str, op: Any, problem: str) -> str:
    return (
        f"{op} already ran, but its group {group} could not be recorded under the loop's parent "
        f"({problem}): the group is not covered by the parent's journal.revert or "
        f"journal.check(parent). journal.revert(group='{group}') puts it back on its own, and "
        f"journal.check(group='{group}') settles it"
    )


def _steps(
    loop: WriteLoop,
    run: Run,
    item: _Item,
    positions: Sequence[int],
    progress: Progress,
    runner: Callable[[Step], dict[str, Any]],
    after: Callable[[Step, dict[str, Any]], str | None] | None = None,
) -> ItemState:
    """Runs the steps at `positions` in order, up to the first failure."""
    for position in positions:
        loop_step = item.ex.steps[position]
        step = loop_step.step
        env, refused = send_step(step, runner, progress.envelopes, loop.check)
        trouble = after(step, env) if after is not None and not refused else None
        if trouble is not None:
            env = _with_trouble(env, trouble)
        summary = record_step(
            step,
            env,
            loop_step.ret,
            run,
            item.ex.key,
            item.entry["steps"],
            progress.envelopes,
            loop.keep,
        )
        where = {"key": item.ex.key, "step": step.id}
        if progress.record(
            step, env, summary, where=where, closed_note=_CLOSED_NOTE, refused=refused
        ):
            break
    if progress.fail_closed is not None:
        _mark_unknown(run, item.ex.key, progress.fail_closed)
    return _item_state(run, progress)


def _fail_before_running(run: Run, item: _Item, step: Step, exc: ServerOpError) -> ItemState:
    summary = {
        "index": step.index,
        "id": step.id,
        "op": step.op_name,
        "ok": False,
        "errorCode": exc.code,
        "error": exc.message,
        "result": exc.result,
    }
    item.entry["steps"].append(summary)
    run.last_failure = {"key": item.ex.key, "step": summary, "hint": exc.hint}
    return ItemState.FAILED


def _own_step(loop: WriteLoop, run: Run, item: _Item, position: int) -> ItemState:
    """A step whose op has a revert of its own kind: a child group of its own. An op that takes
    the group (thread != server) gets one and is recorded under the parent BEFORE it is sent, so
    a write with no answer is still found by `journal.check(parent)`; a group the envelope names
    that is not that one (an op that makes its own, or a server-thread op) is recorded from the
    envelope, after it ran. The groups are listed in the item once the step was sent."""
    loop_step = item.ex.steps[position]
    step, kind, key = loop_step.step, loop_step.kind, item.ex.key
    assert step.op is not None and step.op_name is not None
    group: str | None = None
    if step.op.thread != "server":
        group = new_id("g")
        problem = _name_child(loop, group, key, [step.op_name], [step.id], kind)
        if problem is not None:
            refusal = ServerOpError("POLICY_DENIED", f"nothing of this step was run: {problem}")
            return _fail_before_running(run, item, step, refusal)

    def after(sent: Step, env: dict[str, Any]) -> str | None:
        ops = [str(sent.op_name)]
        if group is not None:
            item.entry["groups"].append(_group_entry(group, ops, kind))
        journal = env.get("journal")
        made = journal.get("group") if isinstance(journal, dict) else None
        if made is None or made == group:
            return None
        item.entry["groups"].append(_group_entry(str(made), ops, kind))
        problem = _name_child(loop, str(made), key, ops, [sent.id], kind)
        return None if problem is None else _unrecorded(str(made), sent.op_name, problem)

    return _steps(loop, run, item, [position], item.progress(), runner_for(loop.ctx, group), after)


def _open_run_group(loop: WriteLoop, item: _Item, steps: list[Step], writes: list[Step]) -> str:
    """The child group of a run of steps that write: its record and its place under the parent.
    Raises the refusal when the journal cannot take them (nothing of the item was run)."""
    group = new_id("g")
    ops = [str(s.op_name) for s in writes]
    problem = _name_child(
        loop,
        group,
        item.ex.key,
        ops,
        [s.id for s in writes],
        Kind.PLAIN,
        GROUP_KIND_RUN,
        label=loop.label,
        steps=[s.op_name for s in steps],
    )
    if problem is not None:
        raise ServerOpError("POLICY_DENIED", f"nothing of this item was run: {problem}")
    item.entry["groups"].append(_group_entry(group, ops, Kind.PLAIN))
    return group


def _plain_run(loop: WriteLoop, run: Run, item: _Item, positions: Sequence[int]) -> ItemState:
    """A run of reads and plain writes: one child group, as one `plan.run` would write it."""
    steps = [item.ex.steps[p].step for p in positions]
    writes = [s for s in steps if s.op is not None and s.op.effect != "read"]
    progress = item.progress()
    if not writes:
        return _steps(loop, run, item, positions, progress, runner_for(loop.ctx))

    def send(runner: Callable[[Step], dict[str, Any]]) -> None:
        _steps(loop, run, item, positions, progress, runner)

    try:
        done = run_in_group(
            loop.ctx,
            steps,
            progress,
            open_group=lambda: _open_run_group(loop, item, steps, writes),
            label=loop.label,
            on_error=ON_ERROR_ABORT,
            run=send,
        )
    except ServerOpError as exc:
        return _fail_before_running(run, item, steps[0], exc)
    if done.facts is not None and done.facts.unknown_end:
        closed = {
            "key": item.ex.key,
            "errorCode": done.facts.env.get("errorCode"),
            "note": _BUNDLE_CLOSED_NOTE,
        }
        _mark_unknown(run, item.ex.key, closed)
    return _item_state(run, progress)


def _run_item(loop: WriteLoop, run: Run, ex: ExpandedItem) -> bool:
    item = _Item(ex, {"key": ex.key, "status": ItemState.DONE, "steps": [], "groups": []})
    run.items.append(item.entry)
    for segment in _segments(ex):
        if loop_writes.has_own_group(ex.steps[segment[0]].kind):
            state = _own_step(loop, run, item, segment[0])
        else:
            state = _plain_run(loop, run, item, segment)
        if state is not ItemState.DONE:
            item.entry["status"] = state
            return False
    return True


def run_items(loop: WriteLoop, expanded: Sequence[ExpandedItem]) -> Run:
    return run_with_budget(
        [ex.key for ex in expanded],
        loop.ctx.loop_limits.budget_s,
        lambda run, position: _run_item(loop, run, expanded[position]),
        abort=True,
        with_groups=True,
    )


def open_parent(
    ctx: ServerContext,
    parent: str,
    label: Any,
    keys: Sequence[Any],
    steps: Sequence[str],
    unattended: bool,
    approval: str | None,
) -> str | None:
    """Writes the parent's group record; the reason it could not be written, else None. A loop
    run with an approval names it (`approval`, the digest before salting): that is what uses it
    up (`plan_loop_confirm.runs_of`)."""
    return ctx.journal.append_group(
        parent,
        LOOP_GROUP_KIND,
        label=label,
        keys=list(keys),
        steps=list(steps),
        mode=mode_name(unattended),
        approval=approval,
    )
