"""`plan.run`'s bundle of Timeline writes.

A plan that holds a step of a Timeline-checkpointed op (`CheckpointSpec(target_kind="timeline")`:
`segment.trim`, `slip`, `replace` and the effect ops, `timeline.overwrite`, `insert`, `cut`,
`create_version` and `create_track`) is not run as separate writes each with its own
checkpoint. It is one checkpointed write on one sequence / clip
(`checkpointed.run_checkpointed`, the engine behind every checkpointed write):

    resolve every write step's selector in one Flame read (nothing found / CONFLICT: nothing runs,
    nothing is journaled)  ->  pin the sequence by its wiretap id  ->  `timeline.checkpoint` once
    ->  intent (fingerprint before, the checkpoint id, the steps)  ->  the steps in order (the
    children take no checkpoint of their own)  ->  fingerprint read back  ->  outcome

and `journal.revert(group)` is one restore of that checkpoint (`timeline_restore`). Because the
restore puts the whole sequence back, the plan's `segment.set_attributes` steps come back with it:
the plan's group is not reverted intent by intent.

What may share such a plan: writes to that one sequence / clip (a Timeline-checkpointed op, or
`segment.set_attributes` of one of its segments) and reads. Any other write has a revert of its own
kind that the one restore would not undo, so it is refused before anything runs.

`late`: a step that points at a segment an earlier step makes or moves cannot be
resolved up front; `late: true` leaves it out of the up-front resolution (its sequence is still
resolved) and resolves it when its turn comes. If that fails the plan stops there with what the
earlier steps did still applied (and revertible).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Protocol

from flame_mcp import execution, selectors
from flame_mcp.checkpointed import (
    ASCII_REFUSAL_HINT,
    CheckpointedRun,
    Judgement,
    PreRunRefusal,
    ascii_refusal_text,
    declared_run_args,
    refuse_if_denied,
    run_checkpointed,
)
from flame_mcp.checkpoints import TIMELINE_KIND
from flame_mcp.errors import as_error_code
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.types import Category, OperationSpec, ServerOpError

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

BUNDLED_WRITE_OPS: Final[frozenset[str]] = frozenset({"segment.set_attributes"})
"""Writes that may share a bundled plan beside the Timeline-checkpointed ones: they act on one
segment of the bundled sequence and come back with the restore."""
SPECS_ARG: Final[str] = "_specs"
MIXED_REASON: Final[str] = (
    "the ways of reverting differ: the plan's one group is put back by restoring one checkpoint "
    "of the sequence, which would not undo this write (it has a revert of its own kind)"
)
LATE_HINT: Final[str] = (
    "A step that points at a segment an earlier step of the plan makes or moves (a head trim "
    "without ripple moves its record_in) is not found by the up-front check: give it "
    "`late: true` and it is resolved when its turn comes."
)


class StepLike(Protocol):
    index: int
    op_name: str | None
    op: RegisteredOperation | None
    args: dict[str, Any]
    problems: list[str]
    late: bool


def is_timeline_op(op: RegisteredOperation) -> bool:
    return op.checkpoint is not None and op.checkpoint.target_kind == "timeline"


def _is_bundled_write(op: RegisteredOperation) -> bool:
    return is_timeline_op(op) or op.name in BUNDLED_WRITE_OPS


def has_timeline_step(steps: Sequence[StepLike]) -> bool:
    return any(s.op is not None and is_timeline_op(s.op) for s in steps)


def apply_static_rules(steps: Sequence[StepLike]) -> None:
    """The rules that need no Flame, as problems on the steps they concern."""
    bundled = has_timeline_step(steps)
    names = sorted({str(s.op_name) for s in steps if s.op is not None and is_timeline_op(s.op)})
    for step in steps:
        op = step.op
        if op is None:
            continue
        if step.late and not (bundled and _is_bundled_write(op)):
            step.problems.append(
                "`late` applies only to a Timeline write of a plan that holds one (a step of "
                f"{sorted(BUNDLED_WRITE_OPS)} or of a Timeline-checkpointed op such as "
                "segment.trim), not to this step"
            )
        if bundled and op.effect != "read" and not _is_bundled_write(op):
            step.problems.append(
                f"{op.name} cannot share a plan with {names}: {MIXED_REASON}; call it on its own"
            )


def _arg_name(op: RegisteredOperation) -> str:
    return op.checkpoint.batch_arg if op.checkpoint is not None else "target"


def _selector_parts(step: StepLike) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(the selector of the sequence / clip the step writes to, its segment selector or None)."""
    assert step.op is not None
    sel: dict[str, Any] = step.args[_arg_name(step.op)]
    spec = step.op.checkpoint
    if spec is not None and spec.group_selector is not None:
        parent = dict(spec.group_selector(step.args))
    elif sel.get("kind") == "segment":
        parent = dict(sel["parent"])
    else:
        parent = dict(sel)
    return parent, (sel if sel.get("kind") == "segment" else None)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _preresolve_to_python(args: Mapping[str, Any]) -> str:
    blocks = ["_rows = []"]
    for i, spec in enumerate(args.get(SPECS_ARG) or []):
        resolve = "\n".join(
            "    " + line for line in selectors.resolve_code(spec["parent"], "_p").splitlines()
        )
        blocks.append(f"def _pr{i}():\n{resolve}\n    return _p")
        seg = spec.get("segment") or {}
        blocks.append(
            f"_rows.append(_tl_pre_row(_pr{i}, {spec['late']!r}, {seg.get('version', 0)!r}, "
            f"{seg.get('track', 0)!r}, {seg.get('index')!r}, {seg.get('record_in')!r}, "
            f"{seg.get('expect_source_name')!r}))"
        )
    blocks.append("_result = {'rows': _rows}")
    return "\n\n".join(blocks)


@cache
def preresolve_op() -> RegisteredOperation:
    """The Flame-side read as an op of a registry of its own (never in the public catalog)."""
    registry = Registry()
    registry.register_category(
        Category(
            name="timeline",
            description="internal",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    return registry.register(
        OperationSpec(
            name="timeline.preresolve",
            category="timeline",
            description="internal: resolve a bundled plan's segment selectors up front",
            effect="read",
            to_python=_preresolve_to_python,
            undo_steps=_no_undo_steps,
            requires=("PySequence.parent", "PyClip.parent", "PyTrack.segments"),
        )
    )


@dataclass(frozen=True)
class TimelineTarget:
    """The one sequence / clip a bundled plan writes to, as the up-front read found it."""

    ident: dict[str, Any]
    stable: dict[str, Any]
    uid: str
    resolved: list[dict[str, Any]]


def _issue(step: StepLike, row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "index": step.index,
        "op": step.op_name,
        "errorCode": row.get("errorCode"),
        "error": row.get("error"),
    }


def preresolve(ctx: ServerContext, steps: Sequence[StepLike]) -> TimelineTarget:
    """Resolve every write step's selector in one Flame read. Raises `ServerOpError` (and nothing
    has been journaled or copied) when one does not resolve, when the writes are not all to one
    sequence / clip, or when that one is not on the Desktop."""
    writes = [s for s in steps if s.op is not None and _is_bundled_write(s.op)]
    specs = []
    for step in writes:
        parent, segment = _selector_parts(step)
        specs.append({"parent": parent, "segment": segment, "late": step.late})
    env = execution.run_op(preresolve_op(), {SPECS_ARG: specs}, run_dir=ctx.run_dir)
    raw = env.get("result")
    value: dict[str, Any] = raw if isinstance(raw, dict) else {}
    rows = value.get("rows")
    if env.get("ok") is not True or not isinstance(rows, list) or len(rows) != len(writes):
        raise ServerOpError(
            as_error_code(str(env.get("errorCode") or "FLAME_API_ERROR")),
            "plan not run (nothing was executed): the up-front resolution of the steps' "
            f"selectors could not be read from Flame ({env.get('error')})",
            result={"ran": False},
            hint=env.get("hint"),
        )
    issues = [_issue(step, row) for step, row in zip(writes, rows, strict=True) if not row["ok"]]
    if issues:
        first = issues[0]
        raise ServerOpError(
            as_error_code(str(first["errorCode"])),
            f"plan not run (nothing was executed): step {first['index']} ({first['op']}) does "
            f"not resolve: {first['error']}"
            + (f" (and {len(issues) - 1} more step(s))" if len(issues) > 1 else ""),
            result={"ran": False, "preResolution": issues},
            hint=LATE_HINT,
        )
    targets: dict[str, dict[str, Any]] = {}
    for row in rows:
        ident = row["target"]
        targets.setdefault(str(ident.get("uid")), ident)
    if len(targets) != 1:
        raise ServerOpError(
            "INVALID_ARGS",
            "plan not run (nothing was executed): the plan's writes address "
            f"{len(targets)} different sequences / clips, and a plan that holds a Timeline "
            "write is one checkpointed write on ONE of them",
            result={
                "ran": False,
                "targets": [
                    {"uid": uid, "name": t.get("name"), "wiretapId": t.get("wiretapId")}
                    for uid, t in targets.items()
                ],
                "steps": [
                    {"index": s.index, "op": s.op_name, "target": r["target"].get("uid")}
                    for s, r in zip(writes, rows, strict=True)
                ],
            },
        )
    ident = next(iter(targets.values()))
    stable = ident.get("selector")
    uid = ident.get("uid")
    if not isinstance(stable, dict) or not isinstance(uid, str):
        raise ServerOpError(
            "PRECONDITION_UNMET",
            "plan not run (nothing was executed): the sequence has no uid / wiretap id, so its "
            "state cannot be found again for the outcome or the revert",
            result={"ran": False},
        )
    resolved = [
        {"index": s.index, "op": s.op_name, "late": s.late, "segment": r.get("segment")}
        for s, r in zip(writes, rows, strict=True)
    ]
    return TimelineTarget(ident=ident, stable=stable, uid=uid, resolved=resolved)


def step_runner(
    ctx: ServerContext, target: TimelineTarget, group: str
) -> Callable[[StepLike], dict[str, Any]]:
    """What runs one step of a bundled plan: a Timeline-checkpointed step without its own
    checkpoint, a `segment.set_attributes` and every other step through the normal do path, the
    sequence's selector replaced by the one pinned by wiretap id."""

    def run(step: StepLike) -> dict[str, Any]:
        op = step.op
        assert op is not None and step.op_name is not None
        if op.checkpoint is not None and is_timeline_op(op):
            run_args = declared_run_args(op.checkpoint, step.args, target.stable)
            env = execution.run_op(
                op, run_args, run_dir=ctx.run_dir, timeout_s=op.checkpoint.timeout_s
            )
            env["journal"] = {"group": group, "bundled": True}
            return env
        args = step.args
        if op.name in BUNDLED_WRITE_OPS:
            sel = args["target"]
            args = {**args, "target": {**sel, "parent": target.stable}}
        return ctx.do(step.op_name, args, group=group)

    return run


def _refusal(exc: PreRunRefusal) -> ServerOpError:
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
        return ServerOpError(
            "PRECONDITION_UNMET",
            "plan not run (nothing was executed): the sequence has no wiretap id, so its state "
            "cannot be found again for the outcome or the revert",
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
    target: TimelineTarget,
    steps: Sequence[StepLike],
    *,
    group: str,
    label: Any,
    on_error: str,
    execute: Callable[[Callable[[StepLike], dict[str, Any]]], dict[str, Any]],
) -> CheckpointedRun:
    """The plan as one checkpointed write. `execute(runner)` runs the steps with `runner` and
    answers {ok, errorCode, error, steps, stoppedAt}: what the engine takes for the run's
    envelope and records in the outcome. The caller holds `ctx.write_lock`."""
    refuse_if_denied(ctx, TIMELINE_KIND.needed_ops)
    step_rows = [{"index": s.index, "op": s.op_name, "late": s.late, "args": s.args} for s in steps]

    def run(stable: dict[str, Any]) -> dict[str, Any]:
        return execute(step_runner(ctx, target, group))

    def judge(env: dict[str, Any], fingerprint_after: str | None) -> Judgement:
        ok = env.get("ok") is True
        return Judgement(
            ok=ok,
            failed=None if ok else str(env.get("error")),
            error_code=None if ok else str(env.get("errorCode")),
        )

    def outcome_extra(env: dict[str, Any], fingerprint_after: str | None) -> dict[str, Any]:
        return {
            "steps": env.get("steps"),
            "stoppedAt": env.get("stoppedAt"),
        }

    try:
        return run_checkpointed(
            ctx,
            selector=target.stable,
            op_name="plan.run",
            kind="plan",
            args={"label": label, "onError": on_error, "steps": step_rows},
            intent_extra={"plan": True, "resolved": target.resolved},
            expected_fp=None,
            label=f"before plan.run ({label})" if label else "before plan.run",
            run=run,
            judge=judge,
            failed_key="plan.run",
            outcome_extra=outcome_extra,
            group=group,
            checkpoint_kind=TIMELINE_KIND,
        )
    except PreRunRefusal as exc:
        raise _refusal(exc) from exc
