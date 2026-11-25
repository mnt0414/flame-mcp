"""The plan classes of the registered ops, read off the registry (`OperationSpec.plan_use`)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from conftest import shared_registry
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import PlanKind


def own_group_ops(ops: Iterable[RegisteredOperation]) -> frozenset[str]:
    """Ops that checkpoint and journal into a group of their own, or whose result a restore does
    not put back; a plan cannot revert them (`PlanUse.own_group`). A destructive `eval.run`
    counts too (`owns_journal_group`).

    Decision: a plan keeps refusing them. The other ops that declare a `CheckpointSpec` are
    bundled (see `plan_bundle`): a plan with one of them is one checkpointed write on one
    sequence / clip / Batch group, reverted by one restore. `batch.load_setup` and
    `journal.restore_checkpoint` are restores themselves, each reverted by restoring its own
    checkpoint; `batch.iterate` and `batch.create_shelf_reel` write an iteration / a shelf reel,
    which are outside a Batch setup, so the restore of the plan would leave them
    (`batch.add_clips` into a shelf reel is refused when the step runs, for the same reason).
    Each such op is one call on its own, with its own group and its own revert."""
    return frozenset(op.name for op in ops if op.plan_use is not None and op.plan_use.own_group)


def refused_in_plan(ops: Iterable[RegisteredOperation]) -> Mapping[str, str]:
    """Ops a plan refuses for a reason of their own (not `owns_journal_group`): op name -> the
    problem `plan.run` reports (`PlanUse.refusal`)."""
    return {
        op.name: op.plan_use.refusal
        for op in ops
        if op.plan_use is not None and op.plan_use.refusal is not None
    }


def ops_of_kind(ops: Iterable[RegisteredOperation], kind: PlanKind) -> frozenset[str]:
    """The names of the ops registered with `kind`."""
    return frozenset(op.name for op in ops if op.plan_use is not None and op.plan_use.kind is kind)


OPS: Final[tuple[RegisteredOperation, ...]] = shared_registry().list_ops()
REFUSED_IN_PLAN: Final = refused_in_plan(OPS)
OWN_GROUP_OPS: Final = own_group_ops(OPS)
ROLLBACK_OPS: Final = ops_of_kind(OPS, PlanKind.ROLLBACK)
REWRITE_OPS: Final = ops_of_kind(OPS, PlanKind.REWRITE)
KEEPS_OUTPUT_OPS: Final = ops_of_kind(OPS, PlanKind.KEEPS_OUTPUT)
NEVER_OPS: Final = ops_of_kind(OPS, PlanKind.NEVER) | frozenset(
    op.name for op in OPS if op.category == "eval"
)
LISTED_OPS: Final = ROLLBACK_OPS | REWRITE_OPS | KEEPS_OUTPUT_OPS | NEVER_OPS
