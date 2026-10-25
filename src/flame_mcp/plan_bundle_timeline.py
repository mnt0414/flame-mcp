"""The Timeline half of `plan.run`'s bundle (`plan_bundle`): the up-front resolution of the
segment selectors of a plan that holds a Timeline-checkpointed write, and what runs one of its
steps.

`late`: a step that points at a segment an earlier step makes or moves cannot be
resolved up front; `late: true` leaves it out of the up-front resolution (its sequence is still
resolved) and resolves it when its turn comes. If that fails the plan stops there with what the
earlier steps did still applied (and revertible).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import execution, selectors
from flame_mcp.checkpointed import declared_run_args
from flame_mcp.checkpoints import TIMELINE_KIND
from flame_mcp.plan_bundle import (
    BUNDLED_WRITE_OPS,
    SKIPPED_KEY,
    BundleTarget,
    StepLike,
    is_bundled_op,
    issue_of,
    single_target,
    unreadable,
    unresolved,
)
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.types import Category, OperationSpec

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

SPECS_ARG: Final[str] = "_specs"
LATE_HINT: Final[str] = (
    "A step that points at a segment an earlier step of the plan makes or moves (a head trim "
    "without ripple moves its record_in) is not found by the up-front check: give it "
    "`late: true` and it is resolved when its turn comes."
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


def preresolve(ctx: ServerContext, steps: Sequence[StepLike]) -> BundleTarget:
    """Resolve every write step's selector in one Flame read. Raises `ServerOpError` (and nothing
    has been journaled or copied) when one does not resolve, when the writes are not all to one
    sequence / clip, or when that one is not on the Desktop."""
    writes = [s for s in steps if s.op is not None and is_bundled_op(s.op, TIMELINE_KIND)]
    specs = []
    for step in writes:
        parent, segment = _selector_parts(step)
        specs.append({"parent": parent, "segment": segment, "late": step.late})
    env = execution.run_op(preresolve_op(), {SPECS_ARG: specs}, run_dir=ctx.run_dir)
    raw = env.get("result")
    value: dict[str, Any] = raw if isinstance(raw, dict) else {}
    rows = value.get("rows")
    if env.get("ok") is not True or not isinstance(rows, list) or len(rows) != len(writes):
        raise unreadable(env)
    issues = [issue_of(step, row) for step, row in zip(writes, rows, strict=True) if not row["ok"]]
    if issues:
        raise unresolved(issues, LATE_HINT)
    ident, stable, uid = single_target(
        TIMELINE_KIND, [(s, r["target"]) for s, r in zip(writes, rows, strict=True)]
    )
    resolved = [
        {"index": s.index, "op": s.op_name, "late": s.late, "segment": r.get("segment")}
        for s, r in zip(writes, rows, strict=True)
    ]
    return BundleTarget(TIMELINE_KIND, ident, stable, uid, resolved)


def step_runner(
    ctx: ServerContext, target: BundleTarget, group: str
) -> Callable[[StepLike], dict[str, Any]]:
    """What runs one step of a bundled plan: a Timeline-checkpointed step without its own
    checkpoint, a `segment.set_attributes` and every other step through the normal do path, the
    sequence's selector replaced by the one pinned by wiretap id. A checkpointed step whose
    generated code refused before it wrote (`result.ran` is False) carries `SKIPPED_KEY`: a plan
    that stops there at its first write has nothing to revert."""

    def run(step: StepLike) -> dict[str, Any]:
        op = step.op
        assert op is not None and step.op_name is not None
        if op.checkpoint is not None and op.checkpoint.target_kind == TIMELINE_KIND.name:
            run_args = declared_run_args(op.checkpoint, step.args, target.stable)
            env = execution.run_op(
                op, run_args, run_dir=ctx.run_dir, timeout_s=op.checkpoint.timeout_s
            )
            env["journal"] = {"group": group, "bundled": True}
            result = env.get("result")
            if (
                env.get("ok") is not True
                and isinstance(result, dict)
                and result.get("ran") is False
            ):
                env[SKIPPED_KEY] = True
            return env
        args = step.args
        if op.name in BUNDLED_WRITE_OPS[TIMELINE_KIND.name]:
            sel = args["target"]
            args = {**args, "target": {**sel, "parent": target.stable}}
        return ctx.do(step.op_name, args, group=group)

    return run
