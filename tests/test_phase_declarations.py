"""An op whose `to_python` writes different code for a `_phase` must declare that phase in
`phases`: registration generates and checks every declared phase, an undeclared one is checked
by nobody."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from conftest import make_registry
from flame_mcp import plan_bundle_batch, plan_bundle_timeline, timeline_purge, timeline_restore
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import PHASE_ARG

SRC = Path(__file__).resolve().parent.parent / "src" / "flame_mcp"
_PHASE_CONSTANT = re.compile(r'^\s*PHASE_[A-Z_]+(?:: Final\[str\])? = "(\w+)"', re.MULTILINE)
INTERNAL_ARGS: tuple[dict[str, Any], ...] = (
    {},
    {"_created": {"items": []}},
    {"_created": {}},
    {"_skip_stash": []},
    {"_still": {}},
    {"_spec": {}},
)


def _phase_values() -> frozenset[str]:
    return frozenset(
        value for path in SRC.rglob("*.py") for value in _PHASE_CONSTANT.findall(path.read_text())
    )


def _generate(op: RegisteredOperation, args: dict[str, Any]) -> str | None:
    assert op.to_python is not None
    try:
        return op.to_python(args)
    except Exception:
        return None


def _undeclared_phases(op: RegisteredOperation) -> list[str]:
    declared = {phase.name for phase in op.phases}
    found: list[str] = []
    for phase in sorted(_phase_values() - declared):
        for extra in INTERNAL_ARGS:
            base = {**op.sample_args, **extra}
            default = _generate(op, base)
            phased = _generate(op, {**base, PHASE_ARG: phase})
            if phased is not None and phased != default:
                found.append(phase)
                break
    return found


def _all_ops() -> list[RegisteredOperation]:
    ops = list(make_registry().list_ops())
    ops += [
        plan_bundle_batch.preresolve_op(),
        plan_bundle_timeline.preresolve_op(),
        timeline_purge.purge_op(),
        timeline_restore.restore_op(),
    ]
    return [op for op in ops if op.to_python is not None]


@pytest.mark.parametrize("op", _all_ops(), ids=lambda op: op.name)
def test_every_phase_that_changes_the_code_is_declared(op: RegisteredOperation) -> None:
    assert _undeclared_phases(op) == []
