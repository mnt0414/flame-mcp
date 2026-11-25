"""Which write ops `plan.for_each` runs, and how: read off each op's `PlanUse` (its registration).

The ops `plan.run` refuses (`PlanUse.refusal`, `PlanUse.own_group`) carry a `PlanKind` by what
`journal.revert` can do with them; an op without a `PlanUse` is `PLAIN` (a value write or a
checkpointed one, put back by the group the loop opens for it). The registration refuses a kind
other than NEVER without a refusal or own_group, so an op cannot be left out of a class.

  ROLLBACK     put back by their own revert (delete what they made, move things back, ...)
  REWRITE      put back, but they change or delete what is there now: a confirmed loop only
  KEEPS_OUTPUT cannot be put back, the data they read stays: a confirmed loop only; the
               parent's revert leaves them and lists them
  NEVER        not run by a loop (every `plan.*` and `eval.*` op too)
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Final

from flame_mcp.plan_step import Step, StepMode
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import PlanKind


class Kind(Enum):
    READ = "read"
    PLAIN = "plain"
    ROLLBACK = "rollback"
    REWRITE = "rewrite"
    KEEPS_OUTPUT = "output"
    NEVER = "never"


_FROM_PLAN_KIND: Final[Mapping[PlanKind, Kind]] = {
    PlanKind.ROLLBACK: Kind.ROLLBACK,
    PlanKind.REWRITE: Kind.REWRITE,
    PlanKind.KEEPS_OUTPUT: Kind.KEEPS_OUTPUT,
    PlanKind.NEVER: Kind.NEVER,
}
LOOP_KINDS: Final[frozenset[PlanKind]] = frozenset(
    {PlanKind.ROLLBACK, PlanKind.REWRITE, PlanKind.KEEPS_OUTPUT}
)
"""The kinds a loop step may be although plan.run refuses them."""


def is_never(op: RegisteredOperation) -> bool:
    """`op` is never a loop step (and never the source of one): a `plan.*` or `eval.*` op, or one
    registered as NEVER."""
    if op.category in ("plan", "eval"):
        return True
    return op.plan_use is not None and op.plan_use.kind is PlanKind.NEVER


UP_FRONT: Final[StepMode] = StepMode(item_refs=True, own_kinds=LOOP_KINDS)
"""How a step is read before the items are put in: `{{ item.* }}` is accepted."""
SENT: Final[StepMode] = StepMode(own_kinds=LOOP_KINDS)
"""How a step is read once the item's values are in (or are checked just before it is sent)."""
NEVER_REASON: Final[str] = (
    "{op} cannot be a plan.for_each step: it cannot be put back and changes what is there, or it "
    "can run anything; call it on its own"
)
OVERWRITE_PARAMS: Final[frozenset[str]] = frozenset({"overwrite", "force", "allow_overwrite"})
"""Names of the params that let a write op write over what is there (render output, a setup
file on disk). Every write op that has one is checked, found in the registry, not listed by hand."""
NOT_OVERWRITE: Final[Mapping[str, str]] = {
    "journal.revert": "force skips the fingerprint check of a revert; it overwrites nothing on "
    "disk (and the op is never a loop step)",
}
"""Write ops that have such a param without it meaning an overwrite, and why."""
_REVIEWED: Final[frozenset[Kind]] = frozenset({Kind.REWRITE, Kind.KEEPS_OUTPUT})
_OWN_STEP: Final[frozenset[Kind]] = frozenset({Kind.ROLLBACK, Kind.REWRITE, Kind.KEEPS_OUTPUT})


def classify(op: RegisteredOperation) -> Kind:
    """How `plan.for_each` treats a step of `op`."""
    if is_never(op):
        return Kind.NEVER
    if op.effect == "read":
        return Kind.READ
    if op.plan_use is None:
        return Kind.PLAIN
    return _FROM_PLAN_KIND[op.plan_use.kind]


def has_own_group(kind: Kind) -> bool:
    """The step is its own child group of the loop (it has a revert of its own kind)."""
    return kind in _OWN_STEP


def needs_review(kind: Kind) -> bool:
    """The loop must be confirmed, whatever the mode, and the preview lists the step."""
    return kind in _REVIEWED


def is_revertible(kind: Kind) -> bool:
    return kind is not Kind.KEEPS_OUTPUT


def overwrite_problem(op: RegisteredOperation, args: Mapping[str, Any]) -> str | None:
    """Why `args` cannot be sent: they ask `op` to write over what is there. A loop refuses it in
    every mode (the overwritten data cannot be put back)."""
    if op.effect == "read" or op.name in NOT_OVERWRITE:
        return None
    for param in op.params:
        if param.name in OVERWRITE_PARAMS and args.get(param.name) is True:
            return (
                f"{op.name} {param.name}=true is not accepted in a loop: it overwrites what is "
                "there and that cannot be put back; call it on its own"
            )
    return None


def judge(step: Step, args: Any) -> Kind:
    """The class of the step's op, with its problems on `step`: an op that is never a loop step,
    and `args` (the step's arguments as they will be sent) that ask to overwrite."""
    if step.op is None:
        return Kind.NEVER
    kind = classify(step.op)
    if kind is Kind.NEVER and step.op.category != "plan":
        step.problems.append(NEVER_REASON.format(op=step.op.name))
        step.policy_denied = False  # refused for what it is, whatever the policy says
    note = overwrite_problem(step.op, args) if isinstance(args, dict) else None
    if note is not None:
        step.problems.append(note)
    return kind
