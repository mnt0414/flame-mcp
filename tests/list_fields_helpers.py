"""The `ListFields` each registered op declares (`OperationSpec.list_fields`)."""

from __future__ import annotations

from functools import cache

from conftest import shared_registry
from flame_mcp.types import ListFields


@cache
def declared_list_fields() -> dict[str, ListFields]:
    return {
        op.name: op.list_fields for op in shared_registry().list_ops() if op.list_fields is not None
    }
