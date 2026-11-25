"""`OperationSpec.plan_use`: how `plan.run` / `plan.for_each` treat an op, read off its
registration."""

from __future__ import annotations

from typing import Any

import pytest

from flame_mcp import loop_writes
from flame_mcp.registry import RegistrationError
from flame_mcp.types import PlanKind, PlanUse
from plan_use_helpers import (
    KEEPS_OUTPUT_OPS,
    LISTED_OPS,
    NEVER_OPS,
    OPS,
    OWN_GROUP_OPS,
    REFUSED_IN_PLAN,
    REWRITE_OPS,
    ROLLBACK_OPS,
)
from test_registry import make_registry, widget_category, write_op


def test_the_classes_do_not_overlap() -> None:
    groups = [ROLLBACK_OPS, REWRITE_OPS, KEEPS_OUTPUT_OPS, NEVER_OPS]
    assert sum(len(g) for g in groups) == len(set().union(*groups))
    assert len(ROLLBACK_OPS) == 12
    assert len(REWRITE_OPS) == 4
    assert len(KEEPS_OUTPUT_OPS) == 6
    assert len(NEVER_OPS) == 8


def test_every_op_plan_run_refuses_is_in_a_class() -> None:
    assert set(REFUSED_IN_PLAN) | OWN_GROUP_OPS <= LISTED_OPS
    assert all(op.plan_use is not None for op in OPS if op.name in REFUSED_IN_PLAN)


def test_every_listed_op_is_registered() -> None:
    assert LISTED_OPS <= {op.name for op in OPS}


def test_the_op_kinds_that_loops_run_all_say_why_plan_run_refuses_them() -> None:
    for op in OPS:
        use = op.plan_use
        if use is not None and use.kind in loop_writes.LOOP_KINDS:
            assert use.refusal or use.own_group, op.name


def test_an_op_without_a_plan_use_is_plain_or_read() -> None:
    for op in OPS:
        if op.plan_use is None and not loop_writes.is_never(op):
            expected = loop_writes.Kind.READ if op.effect == "read" else loop_writes.Kind.PLAIN
            assert loop_writes.classify(op) is expected, op.name


@pytest.mark.parametrize(
    "use",
    [
        PlanUse(PlanKind.ROLLBACK),
        PlanUse(PlanKind.REWRITE),
        PlanUse(PlanKind.KEEPS_OUTPUT),
    ],
)
def test_registration_refuses_a_loop_kind_with_no_reason_plan_run_refuses_it(use: PlanUse) -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    spec: Any = write_op(plan_use=use)
    with pytest.raises(RegistrationError, match="refusal or own_group"):
        registry.register(spec)


def test_registration_refuses_a_plan_use_on_a_read_op() -> None:
    registry = make_registry()
    registry.register_category(widget_category())
    spec: Any = write_op(effect="read", plan_use=PlanUse(PlanKind.NEVER))
    with pytest.raises(RegistrationError, match="no plan_use"):
        registry.register(spec)


def test_a_wrapped_non_read_op_without_plan_use_is_only_one_of_the_two_checkpoint_ops() -> None:
    bare = {
        op.name for op in OPS if op.wrap is not None and op.effect != "read" and op.plan_use is None
    }
    assert bare == {"timeline.checkpoint", "batch.checkpoint"}
