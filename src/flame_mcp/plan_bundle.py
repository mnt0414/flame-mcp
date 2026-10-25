"""`plan.run`'s bundle of checkpointed writes: what the Timeline and the Batch bundle share.

A plan that holds a step of a checkpointed op of one kind -- a sequence / clip on the Desktop
(`CheckpointSpec(target_kind="timeline")`: `segment.trim`, `slip`, `replace` and the effect ops,
`timeline.overwrite` ...) or a Batch group (`target_kind="batch"`: `node.create`, `delete`,
`connect` ..., `action.create_node`, `batch.create_reel` ...) -- is not run as separate writes each
with its own checkpoint. It is one checkpointed write on one sequence / clip / group
(`checkpointed.run_checkpointed`, the engine behind every checkpointed write):

    resolve every write step's selector in one Flame read (nothing found / CONFLICT: nothing runs,
    nothing is journaled)  ->  pin the target by its wiretap id / uid  ->  checkpoint once
    ->  intent (fingerprint before, the checkpoint id, the steps)  ->  the steps in order (the
    children take no checkpoint of their own)  ->  fingerprint read back  ->  outcome

and `journal.revert(group)` is one restore of that checkpoint. Because the restore puts the whole
target back, the plan's value writes (`BUNDLED_WRITE_OPS`) come back with it: the plan's group is
not reverted intent by intent.

What may share such a plan: writes to that one target (a checkpointed op of its kind, or one of the
`BUNDLED_WRITE_OPS` of its kind) and reads. Any other write has a revert of its own kind that the
one restore would not undo, so it is refused before anything runs; so is a write of the other kind
of bundle.

The kind-specific halves are `plan_bundle_timeline` and `plan_bundle_batch` (the up-front
resolution of the selectors and what runs one step); `ops/plan.py` picks one by `bundle_kind`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from flame_mcp.checkpointed import (
    ASCII_REFUSAL_HINT,
    CheckpointedRun,
    Judgement,
    PreRunRefusal,
    ascii_refusal_text,
    refuse_if_denied,
    run_checkpointed,
)
from flame_mcp.checkpoints import (
    ATTRIBUTES_SECTION,
    BATCH_KIND,
    TIMELINE_KIND,
    CheckpointKind,
    fingerprint_of,
    kind_named,
    read_fingerprint,
)
from flame_mcp.errors import as_error_code
from flame_mcp.journal import ATTRIBUTE_FINGERPRINT_KEY
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import ServerOpError

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

BUNDLED_WRITE_OPS: Final[Mapping[str, frozenset[str]]] = {
    TIMELINE_KIND.name: frozenset({"segment.set_attributes"}),
    BATCH_KIND.name: frozenset({"node.set_attributes", "action.set_attributes"}),
}
"""Writes that may share a bundled plan of a kind beside its checkpointed ops: they act on one
segment of the bundled sequence / one node of the bundled group and come back with the restore."""
SKIPPED_KEY: Final[str] = "writeSkipped"
"""Key a step runner sets on the envelope of a write step that was refused before it wrote (the
op's own `check`): nothing of that step is applied, so a plan that stops there at its first write
has nothing to revert."""
MIXED_REASON: Final[str] = (
    "the ways of reverting differ: the plan's one group is put back by restoring one checkpoint "
    "of the {subject}, which would not undo this write (it has a revert of its own kind)"
)
KIND_WORDS: Final[Mapping[str, tuple[str, str]]] = {
    TIMELINE_KIND.name: ("Timeline", "sequences / clips"),
    BATCH_KIND.name: ("Batch", "Batch groups"),
}
"""kind -> (what the write is called, what the different targets are called)."""
Runner = Callable[["StepLike"], dict[str, Any]]


class StepLike(Protocol):
    index: int
    op_name: str | None
    op: RegisteredOperation | None
    args: dict[str, Any]
    problems: list[str]
    late: bool


def checkpoint_kind_of(op: RegisteredOperation) -> CheckpointKind | None:
    """The kind of target `op` checkpoints, None for an op that declares no `CheckpointSpec`."""
    return kind_named(op.checkpoint.target_kind) if op.checkpoint is not None else None


def is_bundled_op(op: RegisteredOperation, kind: CheckpointKind) -> bool:
    """`op` may be a write step of a plan bundled on `kind`."""
    return checkpoint_kind_of(op) is kind or op.name in BUNDLED_WRITE_OPS[kind.name]


def bundle_kind(steps: Sequence[StepLike]) -> CheckpointKind | None:
    """The kind of the plan's bundle: the one of its first valid checkpointed step, None when no
    step is checkpointed (the plan then runs step by step). A step that already has a problem (an
    op that owns its journal group) does not decide it: the plan is refused anyway."""
    for step in steps:
        if step.op is not None and not step.problems:
            kind = checkpoint_kind_of(step.op)
            if kind is not None:
                return kind
    return None


def apply_static_rules(steps: Sequence[StepLike]) -> None:
    """The rules that need no Flame, as problems on the steps they concern."""
    kind = bundle_kind(steps)
    names = (
        sorted(
            {
                str(s.op_name)
                for s in steps
                if s.op is not None and not s.problems and checkpoint_kind_of(s.op) is kind
            }
        )
        if kind is not None
        else []
    )
    for step in steps:
        op = step.op
        if op is None:
            continue
        bundled = kind is not None and is_bundled_op(op, kind)
        if step.late and not bundled:
            step.problems.append(
                "`late` applies only to a write of a plan that holds a checkpointed write (a "
                "Timeline write such as segment.trim, or a Batch write such as node.create), "
                "and only to a write of that bundle, not to this step"
            )
        if kind is not None and op.effect != "read" and not bundled:
            step.problems.append(
                f"{op.name} cannot share a plan with {names}: "
                f"{MIXED_REASON.format(subject=kind.subject)}; call it on its own"
            )


@dataclass(frozen=True)
class BundleTarget:
    """The one sequence / clip / Batch group a bundled plan writes to, as the up-front read found
    it."""

    kind: CheckpointKind
    ident: dict[str, Any]
    stable: dict[str, Any]
    uid: str
    resolved: list[dict[str, Any]]


def issue_of(step: StepLike, row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "index": step.index,
        "op": step.op_name,
        "errorCode": row.get("errorCode"),
        "error": row.get("error"),
    }


def unreadable(env: Mapping[str, Any]) -> ServerOpError:
    return ServerOpError(
        as_error_code(str(env.get("errorCode") or "FLAME_API_ERROR")),
        "plan not run (nothing was executed): the up-front resolution of the steps' "
        f"selectors could not be read from Flame ({env.get('error')})",
        result={"ran": False},
        hint=env.get("hint"),
    )


def unresolved(issues: list[dict[str, Any]], hint: str) -> ServerOpError:
    first = issues[0]
    return ServerOpError(
        as_error_code(str(first["errorCode"])),
        f"plan not run (nothing was executed): step {first['index']} ({first['op']}) does "
        f"not resolve: {first['error']}"
        + (f" (and {len(issues) - 1} more step(s))" if len(issues) > 1 else ""),
        result={"ran": False, "preResolution": issues},
        hint=hint,
    )


def single_target(
    kind: CheckpointKind, idents: Sequence[tuple[StepLike, Mapping[str, Any]]]
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """(ident, stable selector, uid) of the one target every write step addresses. Raises
    `ServerOpError` (nothing has been journaled or copied) when they address several, or when
    the one has no uid / stable address to be found again."""
    targets: dict[str, dict[str, Any]] = {}
    for _step, seen in idents:
        targets.setdefault(str(seen.get("uid")), dict(seen))
    if len(targets) != 1:
        write, nouns = KIND_WORDS[kind.name]
        raise ServerOpError(
            "INVALID_ARGS",
            "plan not run (nothing was executed): the plan's writes address "
            f"{len(targets)} different {nouns}, and a plan that holds a {write} "
            "write is one checkpointed write on ONE of them",
            result={
                "ran": False,
                "targets": [
                    {"uid": uid, "name": t.get("name"), "wiretapId": t.get("wiretapId")}
                    for uid, t in targets.items()
                ],
                "steps": [
                    {"index": s.index, "op": s.op_name, "target": t.get("uid")} for s, t in idents
                ],
            },
        )
    ident = next(iter(targets.values()))
    stable = ident.get("selector")
    uid = ident.get("uid")
    if not isinstance(stable, dict) or not isinstance(uid, str):
        has = "wiretap id" if kind is TIMELINE_KIND else "uid"
        raise ServerOpError(
            "PRECONDITION_UNMET",
            f"plan not run (nothing was executed): the {kind.subject} has no {has}, so its "
            "state cannot be found again for the outcome or the revert",
            result={"ran": False},
        )
    return ident, dict(stable), uid


def _refusal(exc: PreRunRefusal) -> ServerOpError:
    kind = exc.kind
    if exc.stage == "checkpoint":
        cp_env = exc.cp_env or {}
        return ServerOpError(
            as_error_code(str(cp_env.get("errorCode"))),
            "plan not run (nothing was executed): the checkpoint that makes the plan revertible "
            f"could not be taken ({cp_env.get('error')})",
            result={"ran": False, "checkpointError": cp_env.get("error")},
            hint=cp_env.get("hint"),
        )
    if exc.stage == "ascii":
        return ServerOpError(
            "INVALID_ARGS",
            f"plan not run (nothing was executed): {ascii_refusal_text(exc)}",
            result={"ran": False},
            hint=ASCII_REFUSAL_HINT,
        )
    if exc.stage == "uid":
        has = "wiretap id" if kind is TIMELINE_KIND else "uid"
        return ServerOpError(
            "PRECONDITION_UNMET",
            f"plan not run (nothing was executed): the {kind.subject} has no {has}, so its "
            "state cannot be found again for the outcome or the revert",
            result={"ran": False},
        )
    return ServerOpError(
        "POLICY_DENIED",
        f"plan not run (nothing was executed): the journal cannot be written ({exc.cause}), and "
        "this server does not run a checkpointed write without an intent on record",
        result={"ran": False},
    )


def run_bundled(
    ctx: ServerContext,
    target: BundleTarget,
    steps: Sequence[StepLike],
    *,
    group: str,
    label: Any,
    on_error: str,
    make_runner: Callable[[ServerContext, BundleTarget, str], Runner],
    execute: Callable[[Runner], dict[str, Any]],
) -> CheckpointedRun:
    """The plan as one checkpointed write. `execute(runner)` runs the steps with the `runner`
    that `make_runner` builds and answers {ok, errorCode, error, steps, stoppedAt}: what the
    engine takes for the run's envelope and records in the outcome. The caller holds
    `ctx.write_lock`."""
    kind = target.kind
    refuse_if_denied(ctx, kind.needed_ops)
    step_rows = [{"index": s.index, "op": s.op_name, "late": s.late, "args": s.args} for s in steps]

    def run(stable: dict[str, Any]) -> dict[str, Any]:
        return execute(make_runner(ctx, target, group))

    def judge(env: dict[str, Any], fingerprint_after: str | None) -> Judgement:
        ok = env.get("ok") is True
        return Judgement(
            ok=ok,
            failed=None if ok else str(env.get("error")),
            error_code=None if ok else str(env.get("errorCode")),
        )

    watch_attributes = kind is BATCH_KIND and any(
        s.op_name in BUNDLED_WRITE_OPS[BATCH_KIND.name] for s in steps
    )

    def outcome_extra(env: dict[str, Any], fingerprint_after: str | None) -> dict[str, Any]:
        extra: dict[str, Any] = {
            "steps": env.get("steps"),
            "stoppedAt": env.get("stoppedAt"),
            "touched": env.get("touched"),
        }
        if watch_attributes:
            after = read_fingerprint(ctx, target.stable, kind, sections=[ATTRIBUTES_SECTION])
            extra[ATTRIBUTE_FINGERPRINT_KEY] = fingerprint_of(after)
        return extra

    try:
        return run_checkpointed(
            ctx,
            selector=target.stable,
            op_name="plan.run",
            kind="plan",
            args={"label": label, "onError": on_error, "steps": step_rows},
            intent_extra={
                "plan": True,
                "resolved": target.resolved,
                **({"watchAttributes": True} if watch_attributes else {}),
            },
            expected_fp=None,
            label=f"before plan.run ({label})" if label else "before plan.run",
            run=run,
            judge=judge,
            failed_key="plan.run",
            outcome_extra=outcome_extra,
            group=group,
            checkpoint_kind=kind,
        )
    except PreRunRefusal as exc:
        raise _refusal(exc) from exc
