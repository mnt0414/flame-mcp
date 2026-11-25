"""The `brief_paths` each registered op declares (`OperationSpec.brief_paths`)."""

from __future__ import annotations

from functools import cache
from typing import Any

from conftest import shared_registry
from flame_mcp import plan_step


@cache
def declared_brief_paths() -> dict[str, tuple[str, ...]]:
    return {op.name: op.brief_paths for op in shared_registry().list_ops() if op.brief_paths}


def brief_of(op_name: str, result: Any) -> dict[str, Any] | None:
    return plan_step._brief(shared_registry().get(op_name), result)
