"""What a plan (or a `plan.for_each` item) has done so far, and the two kinds of checkpointed
bundle it can run as (`plan_bundle`): the state `plan.run` and `plan.for_each` share."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import plan_bundle, plan_bundle_batch, plan_bundle_timeline, plan_refs
from flame_mcp.checkpoints import BATCH_KIND, TIMELINE_KIND, CheckpointKind
from flame_mcp.journal import JournalLink
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.plan_step import Step
from flame_mcp.types import ServerOpError

if TYPE_CHECKING:
    from flame_mcp.checkpointed import CheckpointedRun
    from flame_mcp.runtime import ServerContext

ON_ERROR_ABORT: Final[str] = "abort"
ON_ERROR_CONTINUE: Final[str] = "continue"
ON_ERROR_VALUES: Final[tuple[str, ...]] = (ON_ERROR_ABORT, ON_ERROR_CONTINUE)


@dataclass
class Progress:
    """What the steps of a plan have done so far."""

    reports: list[dict[str, Any]] = field(default_factory=list)
    omitted: list[int] = field(default_factory=list)
    affected: int = 0
    failed: int = 0
    used_bytes: int = 0
    detail: str = "full"
    stopped_at: int | None = None
    fail_closed: dict[str, Any] | None = None
    last_failure: dict[str, Any] | None = None
    touched: bool = False
    """A write step ran: it answered ok, or failed without saying it was refused before writing."""
    briefed_writes: bool = False
    """A successful write step of a 'mixed' plan was reported as its brief only."""
    keep: frozenset[str] = frozenset()
    """The ids of the steps some later step refers to."""
    envelopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    """What those steps answered, whole: the references follow these, not the plan's answer."""

    def record(
        self,
        step: Step,
        env: Mapping[str, Any],
        summary: dict[str, Any],
        *,
        where: Mapping[str, Any],
        closed_note: str,
        refused: bool = False,
        abort: bool = True,
    ) -> bool:
        """Counts a step that has answered (`env`, reported as `summary`). True when the run stops
        here: a step that was refused before it was sent, a write that ended without an answer
        (`fail_closed`, named by `where` and `closed_note`), or a failure when `abort`."""
        assert step.op is not None
        self.reports.append(summary)
        if step.op.effect != "read" and env.get(plan_bundle.SKIPPED_KEY) is not True:
            self.touched = True
        self.affected += int(env.get("affected") or 0)
        if env.get("ok") is True:
            return False
        self.failed += 1
        self.last_failure = {"step": summary, "hint": env.get("hint"), "env": env}
        unknown = (
            not refused and step.op.effect != "read" and env.get("errorCode") in UNKNOWN_END_CODES
        )
        if unknown:
            self.fail_closed = {
                **where,
                "errorCode": env.get("errorCode"),
                "journal": env.get("journal"),
                "note": closed_note,
            }
        if refused or unknown or abort:
            self.stopped_at = step.index
            return True
        return False


def runner_for(ctx: ServerContext, group: str | None = None) -> Callable[[Step], dict[str, Any]]:
    """Sends a step through the ordinary do path, into `group` when one is given."""

    def run(step: Step) -> dict[str, Any]:
        return ctx.do(str(step.op_name), step.args, link=JournalLink(group))

    return run


def check_bundle(
    steps: Sequence[Step], bundled: Sequence[Step] | None = None
) -> CheckpointKind | None:
    """The up-front rules of a plan's steps: ids and references, then the static rules of a
    bundle and no reference to its target over `bundled` (the steps a bundle may take; all of them
    by default). Returns the kind of the bundle, None for a plan that runs step by step. The
    problems are on the steps."""
    plan_refs.check_plan(steps)
    among = steps if bundled is None else bundled
    plan_bundle.apply_static_rules(among)
    kind = plan_bundle.bundle_kind(among)
    if kind is not None:
        plan_bundle.reject_target_refs(among, kind, BUNDLERS[kind.name].rebound)
    return kind


def journal_refusal(ctx: ServerContext, prefix: str, problem: str) -> ServerOpError:
    """The refusal of a call whose journal group could not be written: nothing ran."""
    return ServerOpError(
        "POLICY_DENIED",
        f"{prefix}the journal cannot be written ({problem})",
        result={"ran": False},
        hint=f"Make {ctx.journal.directory} writable, then retry.",
    )


def bundle_env(progress: Progress) -> dict[str, Any]:
    """The steps' run as the one envelope the checkpointed engine judges and journals."""
    rows = [{k: r.get(k) for k in ("index", "op", "ok", "errorCode")} for r in progress.reports]
    env: dict[str, Any] = {"ok": progress.stopped_at is None, "steps": rows}
    env["stoppedAt"] = progress.stopped_at
    env["touched"] = progress.touched
    if progress.stopped_at is not None and progress.last_failure is not None:
        failing = progress.last_failure["step"]
        env["errorCode"] = failing["errorCode"]
        env["error"] = f"step {progress.stopped_at} ({failing['op']}): {failing.get('error')}"
    return env


@dataclass(frozen=True)
class Bundler:
    """The kind-specific half of a bundled plan: the up-front resolution and the step runner."""

    preresolve: Callable[[ServerContext, Sequence[Any]], plan_bundle.BundleTarget]
    step_runner: Callable[
        [ServerContext, plan_bundle.BundleTarget, str], Callable[[Any], dict[str, Any]]
    ]
    rebound: plan_bundle.Rebound


BUNDLERS: Final[Mapping[str, Bundler]] = {
    TIMELINE_KIND.name: Bundler(
        plan_bundle_timeline.preresolve,
        plan_bundle_timeline.step_runner,
        plan_bundle_timeline.rebound,
    ),
    BATCH_KIND.name: Bundler(
        plan_bundle_batch.preresolve, plan_bundle_batch.step_runner, plan_bundle_batch.rebound
    ),
}


@dataclass(frozen=True)
class GroupRun:
    """What `run_in_group` did: the journal group, the bundle's target and facts (None when the
    steps ran one by one)."""

    group: str
    target: plan_bundle.BundleTarget | None
    facts: CheckpointedRun | None


def run_in_group(
    ctx: ServerContext,
    steps: Sequence[Step],
    progress: Progress,
    *,
    open_group: Callable[[], str],
    label: Any,
    on_error: str,
    run: Callable[[Callable[[Step], dict[str, Any]]], None],
) -> GroupRun:
    """Runs writing `steps` in one journal group, as `plan.run` does: a bundle's target is
    resolved first (a refusal journals nothing), then `open_group` records the group and names
    it, then `run(runner)` sends the steps -- through one checkpointed write when they are a
    bundle, else one by one into the group. The caller holds `ctx.write_lock`; the refusals of
    `open_group` and the bundle come out as they are."""
    kind = plan_bundle.bundle_kind(steps)
    bundler = BUNDLERS[kind.name] if kind is not None else None
    target = bundler.preresolve(ctx, steps) if bundler is not None else None
    group = open_group()
    if bundler is None or target is None:
        run(runner_for(ctx, group))
        return GroupRun(group, target, None)

    def execute(runner: Callable[[Step], dict[str, Any]]) -> dict[str, Any]:
        run(runner)
        return bundle_env(progress)

    facts = plan_bundle.run_bundled(
        ctx,
        target,
        steps,
        group=group,
        label=label,
        on_error=on_error,
        make_runner=bundler.step_runner,
        execute=execute,
    )
    return GroupRun(group, target, facts)
