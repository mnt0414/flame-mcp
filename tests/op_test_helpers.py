"""Shared helper for the op test files: run one registered op's code.

Runs the generated code directly against a fake `flame` module (no bridge/socket),
exactly the shape `flame_mcp_bridge.impl._main_run_exec` hands to it
(`ns = {"flame": ..., "__name__": ...}`, `exec(compiled, ns)`)."""

from __future__ import annotations

import ast
from typing import Any

from flame_mcp import execution
from flame_mcp.callargs import apply_param_defaults
from flame_mcp.registry import Registry


def run_op(registry: Registry, op_name: str, args: dict[str, Any], flame: Any) -> dict[str, Any]:
    op = registry.get(op_name)
    assert op is not None, f"no such op: {op_name}"
    args = dict(args)
    apply_param_defaults(op, args)  # what flame_do does before to_python
    code = execution.build_exec_code(op, args)
    ast.parse(code)  # the bridge parses before scheduling; this must never fail
    ns: dict[str, Any] = {"flame": flame, "__name__": "__flame_mcp_exec__"}
    exec(compile(code, "<test>", "exec"), ns)
    result: dict[str, Any] = ns["_result"]
    return result
