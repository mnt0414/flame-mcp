"""One step of `plan.run` / `plan.for_each`: reading it (`check_step`), checking its arguments
(`validate_step_args`, `args_checker`), and reporting what it did (`step_summary`).

The plan ops (`ops/plan`, `ops/plan_loop`) and the loop engine (`plan_loop_*`) both depend on this
module, not on each other's ops.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from flame_mcp import opschema, plan_refs
from flame_mcp.list_fields import unknown_fields_message
from flame_mcp.opschema import validate_op_args
from flame_mcp.policy import deny_operation
from flame_mcp.refs import RefsError
from flame_mcp.registry import RegisteredOperation
from flame_mcp.runtime import ServerContext
from flame_mcp.types import PlanKind

MAX_PLAN_STEPS: Final[int] = 50
"""Most steps one plan may hold."""
PLAN_STEP_RESULT_BYTES: Final[int] = 4_000
"""A step's `result` larger than this is left out of the plan's answer (the answer as a
whole is still held to the response byte budget by `degrade_for_size`)."""
PLAN_TOTAL_RESULT_BYTES: Final[int] = 30_000
"""All the step results one plan answer may carry together; later ones are left out."""
_ERROR_TEXT_CHARS: Final[int] = 600
MIXED_DETAIL: Final[str] = "mixed"
"""What `result.detail` says when compact responses made 'auto' decide per step: a read step in
full, a successful write step as a brief, a failed step in full."""
BRIEF_LIST_LIMIT: Final[int] = 20
"""Most items of a list value a brief keeps; `<key>Omitted` counts the rest."""
_DEGRADED_NOTE_CHARS: Final[int] = 200
"""Longest a step's degraded note is in a plan's summary."""
_STEP_KEYS: Final[frozenset[str]] = frozenset({"id", "op", "args", "late"})


@dataclass(frozen=True)
class StepMode:
    """What a step may hold beyond what `plan.run` accepts."""

    item_refs: bool = False
    """`{{ item.<path> }}` in the arguments (`plan.for_each` up front, before an item is put in)."""
    own_kinds: frozenset[PlanKind] = frozenset()
    """Ops of these kinds may be a step although `plan.run` refuses them (`plan.for_each` runs
    each as a child group of its own)."""


PLAN_RUN: Final[StepMode] = StepMode()


@dataclass
class Step:
    index: int
    op_name: str | None = None
    op: RegisteredOperation | None = None
    args: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    policy_denied: bool = False
    hint: str | None = None
    late: bool = False
    id: str = ""
    refs: list[plan_refs.Ref] = field(default_factory=list)
    """The references in `args`; until the step is sent `args` holds them as written."""
    mode: StepMode = PLAN_RUN

    def __post_init__(self) -> None:
        if not self.id:
            self.id = plan_refs.default_id(self.index)

    @property
    def has_refs(self) -> bool:
        return bool(self.refs)


def owns_journal_group(op: RegisteredOperation, args: Mapping[str, Any]) -> bool:
    """`op` called with `args` takes its own checkpoint and journal group (a destructive eval,
    `PlanUse.own_group`). One that declares a `CheckpointSpec` otherwise does not count: a plan
    bundles it."""
    if op.category == "eval":
        return args.get("effect") != "read"
    return op.plan_use is not None and op.plan_use.own_group


def _is_own_step(step: Step, op: RegisteredOperation) -> bool:
    """`op` may be this step although `plan.run` refuses it (the step's mode allows its kind)."""
    return op.plan_use is not None and op.plan_use.kind in step.mode.own_kinds


def _owns_group(step: Step) -> Callable[[RegisteredOperation, Any], bool]:
    def owns(op: RegisteredOperation, args: Any) -> bool:
        return not _is_own_step(step, op) and owns_journal_group(op, args)

    return owns


def _read_keys(step: Step, raw: Mapping[str, Any]) -> None:
    """The step's `id` and `late`, and the keys it may not have, as problems on `step`."""
    extra = sorted(set(raw) - _STEP_KEYS)
    if extra:
        step.problems.append(
            f"unknown key(s) {extra} (a step has only 'id', 'op', 'args' and 'late')"
        )
    if "id" in raw:
        step_id = raw["id"]
        if isinstance(step_id, str) and plan_refs.STEP_ID_RE.match(step_id):
            step.id = step_id
        else:
            step.problems.append("'id' must be a string of letters, digits and '_'")
    late = raw.get("late", False)
    if isinstance(late, bool):
        step.late = late
    else:
        step.problems.append("'late' must be a boolean")


def _refuse_op(ctx: ServerContext, step: Step, op: RegisteredOperation) -> bool:
    """Whether `op` cannot be this step at all (a problem is on `step` then)."""
    if op.plan_use is not None and op.plan_use.refusal is not None and not _is_own_step(step, op):
        step.problems.append(op.plan_use.refusal)
        return True
    if op.category == "plan":
        step.problems.append("plan.run cannot be nested: a step must not be a plan.* operation")
        return True
    denial = deny_operation(op, ctx.policy)
    if denial is None:
        return False
    step.policy_denied = True
    step.hint = denial.hint
    step.problems.append(f"denied by policy: {denial.message}")
    return True


def _step_refs(step: Step, op: RegisteredOperation, raw_args: Any) -> bool:
    """Reads the references in the arguments. True when the step is settled: a malformed one, or
    references whose values are checked only when the step is sent."""
    if not isinstance(raw_args, dict):
        return False
    try:
        refs = plan_refs.collect(raw_args, allow_item=step.mode.item_refs)
    except RefsError as exc:
        step.problems.append(f"args: {exc}")
        return True
    if not refs:
        return False
    step.refs = refs
    step.args = dict(raw_args)
    problem = plan_refs.own_group_problem(op, step.args, _owns_group(step))
    if problem is not None:
        step.problems.append(problem)
    return True


def check_step(ctx: ServerContext, index: int, raw: Any, mode: StepMode = PLAN_RUN) -> Step:
    step = Step(index, mode=mode)
    if not isinstance(raw, dict):
        step.problems.append("a step must be an object {op, args}")
        return step
    _read_keys(step, raw)
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
    if _refuse_op(ctx, step, op):
        return step
    raw_args = raw.get("args")
    if not _step_refs(step, op, raw_args):
        validate_step_args(ctx, step, raw_args)
    return step


def validate_step_args(ctx: ServerContext, step: Step, raw_args: Any) -> None:
    """What a step's arguments must pass, as problems on `step`; `step.args` is the validated
    value. The same checks run up front, or, for a step that holds references, when it is sent."""
    op = step.op
    assert op is not None
    validation = validate_op_args(op, raw_args)
    if not validation.ok:
        step.problems.extend(f"{issue.path}: {issue.message}" for issue in validation.issues)
        return
    step.args = dict(validation.value or {})
    unknown_fields = unknown_fields_message(op.list_fields, step.args)
    if unknown_fields is not None:
        step.problems.append(unknown_fields)
        return
    problem = plan_refs.own_group_problem(op, step.args, _owns_group(step))
    if problem is not None:
        step.problems.append(problem)
        return
    if op.precheck is not None:
        refusal = op.precheck(step.args, ctx.policy)
        if refusal is not None:
            step.problems.append(refusal.message)
            step.policy_denied = refusal.code == "POLICY_DENIED"
            step.hint = refusal.hint


def args_checker(
    ctx: ServerContext, mode: StepMode = PLAN_RUN
) -> Callable[[plan_refs.SendableStep, Any], plan_refs.Checked]:
    """The check `plan_refs.prepare` runs on a step's arguments once the references are in."""

    def check(step: plan_refs.SendableStep, args: Any) -> plan_refs.Checked:
        scratch = Step(step.index, step.op_name, step.op, mode=mode)
        validate_step_args(ctx, scratch, args)
        return plan_refs.Checked(
            scratch.args, scratch.problems, scratch.policy_denied, scratch.hint
        )

    return check


def step_issues(checked: list[Step]) -> list[dict[str, Any]]:
    """`{index, op, problems}` of every step that has problems."""
    return [
        {"index": s.index, "op": s.op_name, "problems": s.problems} for s in checked if s.problems
    ]


def policy_only_denial(
    checked: list[Step], *, other_problems: bool, other_denied: bool = False
) -> tuple[bool, Step | None]:
    """Whether a refusal before anything ran is a policy denial and nothing else: no problem but
    `other_problems`, no step invalid for another reason, and a denial (`other_denied`, or a
    step). With the first step the policy denied."""
    only = (
        not other_problems
        and not any(s.problems and not s.policy_denied for s in checked)
        and (other_denied or any(s.policy_denied for s in checked))
    )
    denied = next((s for s in checked if s.policy_denied), None)
    return only, denied


def _brief(op: RegisteredOperation | None, result: Any) -> dict[str, Any] | None:
    """The few facts of `result` that the op's `brief_paths` names, or None. A dict value
    keeps its scalar members only (a position loses its selectors); a list is cut at
    `BRIEF_LIST_LIMIT`. An op without `brief_paths` has no brief (only ok / errorCode)."""
    keys = op.brief_paths if op is not None else ()
    if not keys or not isinstance(result, Mapping):
        return None
    brief: dict[str, Any] = {}
    for key in keys:
        value = result.get(key)
        if isinstance(value, Mapping):
            value = {k: v for k, v in value.items() if not isinstance(v, Mapping | list)}
        elif isinstance(value, list) and len(value) > BRIEF_LIST_LIMIT:
            brief[f"{key}Omitted"] = len(value) - BRIEF_LIST_LIMIT
            value = value[:BRIEF_LIST_LIMIT]
        if value is not None:
            brief[key] = value
    return brief or None


def _degraded_notes(env: Mapping[str, Any], limit: int | None) -> list[str]:
    """The step's `degraded` notes (what it did differently from the request without failing),
    each cut at `limit` characters when one is given."""
    raw = env.get("degraded")
    notes = [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []
    return [n if limit is None else n[:limit] for n in notes]


def is_brief_step(step: Step, env: Mapping[str, Any], detail: str) -> bool:
    """The step is reported as `{index, op, ok, errorCode, brief}`: a successful step of a
    'summary' plan, or a successful write step of a 'mixed' one."""
    if env.get("ok") is not True:
        return False
    if detail == MIXED_DETAIL:
        return step.op is not None and step.op.effect != "read"
    return detail == "summary"


def _brief_summary(step: Step, env: Mapping[str, Any], result: Any) -> dict[str, Any]:
    short: dict[str, Any] = {
        "index": step.index,
        "id": step.id,
        "op": step.op_name,
        "ok": True,
        "errorCode": env.get("errorCode"),
    }
    brief = _brief(step.op, result)
    if brief is not None:
        short["brief"] = brief
    notes = _degraded_notes(env, _DEGRADED_NOTE_CHARS)
    if notes:
        short["degraded"] = notes
    return short


def step_summary(
    step: Step, env: Mapping[str, Any], used_bytes: int, detail: str = "full"
) -> tuple[dict[str, Any], int, bool]:
    """One executed step as the plan reports it. `detail='full'`: its result, left out when it is
    too big for the plan's answer. `detail='summary'`: {index, op, ok, errorCode, brief}, and for
    a failed step its error and its result (under the same size limits, so the reason of a
    partial application can be read). `detail='mixed'`: a successful write step as in
    'summary', every other step as in 'full'. Returns (summary, bytes of result kept, was it left
    out). A result left out keeps its `brief` inside the `omitted` marker."""
    result = env.get("result")
    if is_brief_step(step, env, detail):
        return _brief_summary(step, env, result), 0, False
    size = len(json.dumps(result, default=str)) if result is not None else 0
    omitted = size > PLAN_STEP_RESULT_BYTES or used_bytes + size > PLAN_TOTAL_RESULT_BYTES
    kept: Any = result
    if omitted:
        kept = {
            "omitted": True,
            "bytes": size,
            "note": "too big for a plan's answer; run this op on its own for the full result",
        }
        brief = _brief(step.op, result)
        if brief is not None:
            kept["brief"] = brief
    error = env.get("error")
    summary = {
        "index": step.index,
        "id": step.id,
        "op": step.op_name,
        "ok": bool(env.get("ok")),
        "errorCode": env.get("errorCode"),
        "error": str(error)[:_ERROR_TEXT_CHARS] if error is not None else None,
        "result": kept,
        "journal": env.get("journal"),
    }
    notes = _degraded_notes(env, _DEGRADED_NOTE_CHARS if detail == "summary" else None)
    if notes:
        summary["degraded"] = notes
    if detail == "summary":
        del summary["journal"]
        if result is None:
            del summary["result"]
    return summary, 0 if omitted else size, omitted
