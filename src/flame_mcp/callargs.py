"""Argument preparation shared by every way an op gets called.

Callers are a `flame_do` call and the server's own internal calls from
`runtime.Runtime`. Fills declared defaults and applies the `limit` rules (layer 2 of
the response limit). Lives apart from server.py so `runtime` and `journaled_write`
can use it without importing the MCP layer.
"""

from __future__ import annotations

import copy
from typing import Any

from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import NO_DEFAULT


def apply_param_defaults(op: RegisteredOperation, args: dict[str, Any]) -> None:
    """Fills every omitted parameter that declares a concrete default.

    An op's `to_python` can then index `args[name]` for any param with one. `limit`
    (declared `default=None`) is left to `apply_limit_default`.
    """
    for param in op.params:
        if param.name in args or param.default is NO_DEFAULT or param.default is None:
            continue
        args[param.name] = copy.deepcopy(param.default)


def apply_limit_default(op: RegisteredOperation, args: dict[str, Any]) -> list[str]:
    """Applies layer 2 of the response limit and returns the `degraded` notes.

    Fills `default_limit` when omitted, clamps to `max_limit` when exceeded, and
    clamps up to 1 when below it (`limit=0` is not "no limit"). Each clamp is noted
    in the returned list. Truncating the *result* itself and reporting total/shown is
    the op's job.

    Called before `execution.run_op`, so these notes are already in the envelope's
    `degraded` list before `_tool_result` calls `degrade_for_size`. The byte-budget
    degrade (layer 3) only appends to that list, so a limit note always precedes any
    layer-3 note, including inside a spilled file.
    """
    has_limit_param = any(param.name == "limit" and param.type == "number" for param in op.params)
    if not has_limit_param:
        return []
    if "limit" not in args or args["limit"] is None:
        args["limit"] = op.default_limit
        return []
    value = args["limit"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return []
    if value < 1:
        args["limit"] = 1
        return [f"requested limit {value!r} raised to 1 (0/negative is not a valid 'no limit')"]
    if value > op.max_limit:
        args["limit"] = op.max_limit
        return [f"requested limit {value!r} clamped to max_limit={op.max_limit}"]
    return []
