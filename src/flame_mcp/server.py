"""The 4 MCP tools: flame_version_info / flame_catalog / flame_do / flame_context.

Policy must agree in three places: tool registration (this module's
`_flame_do_input_schema`, which enums only visible op names into
`flame_do`'s `op` parameter), `flame_catalog` (`policy.visible_ops`), and
`flame_do`'s own execution gate (`policy.deny_operation`). All three read
the same `Registry`/`Policy` instances passed to `create_server`, so there
is exactly one place that can go stale.

Every handler that touches `transport` (bridge I/O, blocking sockets) runs
in a worker thread via `anyio.to_thread.run_sync` so the asyncio event loop
driving the stdio transport is never blocked.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from flame_mcp import agent_context, still_proxy, transport
from flame_mcp import compact as compact_mod
from flame_mcp import envelope as envelope_mod
from flame_mcp import policy as policy_mod
from flame_mcp.catalog import (
    CATALOG_DETAIL_PARAM_DESCRIPTION,
    CATALOG_DETAIL_PARAM_DESCRIPTION_COMPACT,
    CATALOG_DETAILS,
    CATALOG_OMITTED_NOTE,
    CATALOG_TOOL_DESCRIPTION,
    CATALOG_TOOL_DESCRIPTION_COMPACT,
    MAX_CATALOG_OPS,
    catalog_detail_note,
    catalog_entry,
    catalog_invalid_args,
    catalog_listing_note,
    suggest_unknown_operation,
    unknown_operation_message,
)
from flame_mcp.compact import ResponseMode
from flame_mcp.execution import V_HELPER
from flame_mcp.journal import Journal
from flame_mcp.loop_limits import DEFAULT_LOOP_LIMITS, LoopLimits
from flame_mcp.policy import Policy, deny_category, deny_operation, deny_tool
from flame_mcp.registry import Registry
from flame_mcp.runtime import Runtime, TestHooks
from flame_mcp.transport import elapsed_ms
from flame_mcp_bridge.impl import BRIDGE_ABI

SERVER_VERSION: Final[str] = "0.5.0"
TESTED_FLAME_VERSIONS: Final[tuple[str, ...]] = ("2025.2.4", "2026.2.3")

TOOL_VERSION_INFO: Final[str] = "flame_version_info"
TOOL_CATALOG: Final[str] = "flame_catalog"
TOOL_DO: Final[str] = "flame_do"
TOOL_CONTEXT: Final[str] = "flame_context"

MAX_INSTRUCTIONS_BYTES: Final[int] = 2048
INSTRUCTIONS: Final[str] = "\n".join(
    [
        "Read before you write: info / list_* / fingerprint first. Never guess values or names.",
        "Find an op with flame_catalog first. eval.run (off unless FLAME_MCP_ENABLE_EVAL=1) "
        "is the last resort, for what no op can do.",
        "To undo, use the result's journal.revert. Never press Ctrl+Z: it can remove an "
        "unrelated operation, and the journal does not know about it (flame_context: "
        "undoContract).",
        "After TIMEOUT or BRIDGE_LOST do not retry: run journal.check first (a read that timed "
        "out with pending.handle: journal.poll). SILENT_NOOP means "
        "the call (or step) had no effect you can observe; if part of it took effect, read "
        "result.applied / partial.",
        "While a render runs, calls may return BRIDGE_UNAVAILABLE. Wait for the render through "
        "its pending handle (journal.check), not by retrying.",
        "Every string sent to Flame (names, paths, eval.run code) must be ASCII; non-ASCII is "
        "refused with INVALID_ARGS.",
        "flame_context lists known pitfalls. Read them before writing eval.run code.",
        "Before an operation that cannot be undone or that overwrites (render.batch, "
        "render.clip, render.export, overwrite=true, journal.purge_checkpoints, "
        "desktop.delete, desktop.reformat (in place), journal.revert force=true, "
        "journal.restore_checkpoint, batch.load_setup, batch.delete, eval.run), ask the user to "
        "confirm.",
        "To run the same steps for each item of a list, use plan.for_each, not one call per item.",
        "For a write loop, show the user the dry_run list and wait for the answer before you "
        "approve.",
    ]
)


def instructions_for(mode: ResponseMode) -> str:
    """INSTRUCTIONS as the client gets them: compact adds the one sentence about its format."""
    if mode == "compact":
        return INSTRUCTIONS + "\n" + compact_mod.INSTRUCTIONS_NOTE
    return INSTRUCTIONS


# None of the 4 dedicated tools mutates the project (only the op a flame_do call
# names does, gated separately). The map exists so tool registration still runs
# every tool name through deny_tool, per the three-places rule in the module
# docstring.
_TOOL_BLOCKED_IN_READONLY: Final[dict[str, bool]] = {
    TOOL_VERSION_INFO: False,
    TOOL_CATALOG: False,
    TOOL_DO: False,
    TOOL_CONTEXT: False,
}

_READ_ONLY_ANNOTATIONS: Final[types.ToolAnnotations] = types.ToolAnnotations(read_only_hint=True)
# flame_do runs whatever op it is given, some of which delete or overwrite (the op's own effect
# is in flame_catalog), so it is declared destructive as a whole.
_DO_ANNOTATIONS: Final[types.ToolAnnotations] = types.ToolAnnotations(
    read_only_hint=False, destructive_hint=True
)

_EMPTY_OBJECT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

# One exec on the main thread gathers the two pieces of "expensive state" flame_context
# adds on top of its static knowledge, plus the rider.
# It reuses V_HELPER rather than redefining `_v`, which must exist in one place only.
_CONTEXT_CODE = "\n".join(
    [
        V_HELPER,
        "",
        "__libraries = None",
        "try:",
        "    __libraries = ["
        "_v(lib.name) for lib in flame.projects.current_project.current_workspace.libraries"
        "]",
        "except BaseException:",
        "    __libraries = None",
        "",
        "__current_tab = None",
        "try:",
        "    __current_tab = flame.get_current_tab()",
        "except BaseException:",
        "    __current_tab = None",
        "",
        "__ctx_project = None",
        "try:",
        "    __ctx_project = flame.projects.current_project.name",
        "except BaseException:",
        "    __ctx_project = None",
        "",
        "__ctx_workspace = None",
        "try:",
        "    __ctx_workspace = _v(flame.projects.current_project.current_workspace.name)",
        "except BaseException:",
        "    __ctx_workspace = None",
        "",
        "__ctx_selected = None",
        "try:",
        "    __ctx_selected = len(flame.media_panel.selected_entries)",
        "except BaseException:",
        "    __ctx_selected = None",
        "",
        "_result = {",
        "    'libraries': __libraries,",
        "    'currentTab': __current_tab,",
        "    'context': {",
        "        'project': __ctx_project,",
        "        'workspace': __ctx_workspace,",
        "        'selected': __ctx_selected,",
        "    },",
        "}",
    ]
)


def _compact_within_limit(env: Mapping[str, Any]) -> dict[str, Any]:
    """The compact envelope when it fits the response limit, as it is.

    Over the limit, the size limit is applied to the full envelope first (its lists collapse to
    {count, omitted}, and a spill file holds the full envelope) and that result is compacted.
    Compacting first would make `{columns, rows}` of a list the collapse no longer sees.
    """
    compacted = compact_mod.compact_envelope(env)
    if envelope_mod.byte_len(compacted) <= envelope_mod.MAX_RESPONSE_BYTES:
        return compacted
    return compact_mod.compact_envelope(envelope_mod.degrade_for_size(env))


def _tool_result(
    env: Mapping[str, Any], mode: ResponseMode = compact_mod.DEFAULT_RESPONSE_MODE
) -> types.CallToolResult:
    degraded_env = (
        _compact_within_limit(env) if mode == "compact" else envelope_mod.degrade_for_size(env)
    )
    text = json.dumps(degraded_env, default=str)
    content: list[types.ContentBlock] = [types.TextContent(type="text", text=text)]
    for data, mime in still_proxy.attachment_images(env):
        content.append(
            types.ImageContent(
                type="image", data=base64.b64encode(data).decode("ascii"), mime_type=mime
            )
        )
    return types.CallToolResult(
        content=content,
        structured_content=degraded_env,
        is_error=not bool(degraded_env["ok"]),
    )


CATALOG_RESULT_BUDGET_BYTES: Final[int] = 50_000


def _flame_do_input_schema(registry: Registry, policy: Policy) -> dict[str, Any]:
    visible_names = sorted(op.name for op in policy_mod.visible_ops(registry, policy))
    op_schema: dict[str, Any] = {
        "type": "string",
        "description": "Operation name, as listed by flame_catalog.",
    }
    if visible_names:
        op_schema["enum"] = visible_names
    return {
        "type": "object",
        "properties": {
            "op": op_schema,
            "args": {
                "type": ["object", "null"],
                "description": "Operation-specific arguments; see flame_catalog for the shape.",
            },
        },
        "required": ["op"],
        "additionalProperties": False,
    }


def _tool_definitions(
    registry: Registry,
    policy: Policy,
    response_mode: ResponseMode = compact_mod.DEFAULT_RESPONSE_MODE,
) -> list[types.Tool]:
    is_compact = response_mode == "compact"
    for name, blocked in _TOOL_BLOCKED_IN_READONLY.items():
        # Structural checkpoint (the tool-registration place of the three-places rule):
        # it denies nothing for these 4 tools, but registration runs every name
        # through the same gate flame_catalog/flame_do use.
        deny_tool(name, blocked, policy)
    return [
        types.Tool(
            name=TOOL_VERSION_INFO,
            description=(
                "Server/bridge identity, policy summary, and operation counts. "
                "Touches no Flame project state."
            ),
            input_schema=_EMPTY_OBJECT_SCHEMA,
            annotations=_READ_ONLY_ANNOTATIONS,
        ),
        types.Tool(
            name=TOOL_CATALOG,
            description=(
                CATALOG_TOOL_DESCRIPTION_COMPACT if is_compact else CATALOG_TOOL_DESCRIPTION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Restrict the listing to one category.",
                    },
                    "op": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": MAX_CATALOG_OPS,
                            },
                        ],
                        "description": (
                            "Describe one operation by name, or up to 10 operations as a list."
                        ),
                    },
                    "detail": {
                        "type": "string",
                        "enum": list(CATALOG_DETAILS),
                        "description": (
                            CATALOG_DETAIL_PARAM_DESCRIPTION_COMPACT
                            if is_compact
                            else CATALOG_DETAIL_PARAM_DESCRIPTION
                        ),
                    },
                },
                "additionalProperties": False,
            },
            annotations=_READ_ONLY_ANNOTATIONS,
        ),
        types.Tool(
            name=TOOL_DO,
            description="Run one registered operation against Flame; returns a result envelope.",
            input_schema=_flame_do_input_schema(registry, policy),
            annotations=_DO_ANNOTATIONS,
        ),
        types.Tool(
            name=TOOL_CONTEXT,
            description=(
                "Static conventions (undo/thread/selector vocabulary/limits, known pitfalls) "
                "plus a live snapshot (library names, current tab, selection count)."
            ),
            input_schema=_EMPTY_OBJECT_SCHEMA,
            annotations=_READ_ONLY_ANNOTATIONS,
        ),
    ]


def _flame_version_info_sync(runtime: Runtime) -> dict[str, Any]:
    registry, policy, run_dir = runtime.registry, runtime.policy, runtime.run_dir
    started = time.monotonic()
    effective_run_dir = run_dir if run_dir is not None else transport.default_run_dir()
    candidate = transport._discover(effective_run_dir)  # hello-only; never touches main
    bridge: dict[str, Any]
    notices: list[str] = []
    if isinstance(candidate, transport.BridgeCandidate):
        bridge = {
            "reachable": True,
            "pid": candidate.pid,
            "flameVersion": candidate.flame_version,
            "projectName": candidate.project_name,
            "bridgeAbi": candidate.bridge_abi,
            "untestedFlameVersion": candidate.flame_version not in TESTED_FLAME_VERSIONS,
        }
        if bridge["untestedFlameVersion"]:
            notices.append(
                f"flame-mcp: Flame {candidate.flame_version} is not a tested version "
                f"(tested: {', '.join(TESTED_FLAME_VERSIONS)}); operations may behave differently"
            )
    else:
        bridge = {
            "reachable": False,
            "errorCode": candidate.error_code,
            "error": candidate.message,
        }
    ops = registry.list_ops()
    visible = policy_mod.visible_ops(registry, policy)
    inflight = transport.read_inflight_summary(effective_run_dir)
    result = {
        "serverVersion": SERVER_VERSION,
        "serverPid": os.getpid(),
        "bridgeAbi": BRIDGE_ABI,
        "pythonVersion": platform.python_version(),
        "policy": policy_mod.summary(policy),
        "registeredOps": len(ops),
        "visibleOps": len(visible),
        "bridge": bridge,
        "inflight": {"count": inflight.count, "newest": inflight.newest},
        "journal": runtime.journal_summary(),
        "ffmpeg": still_proxy.ffmpeg_info(),
    }
    if runtime.permissions is not None:
        result["permissions"] = runtime.permissions
    env = envelope_mod.build_envelope(ok=True, result=result, duration_ms=elapsed_ms(started))
    env["notices"].extend(notices)
    return env


def _catalog_by_names(
    registry: Registry,
    policy: Policy,
    names: list[str],
    detail: str,
    started: float,
) -> dict[str, Any]:
    entries: list[tuple[str, dict[str, Any]]] = []
    unavailable: list[dict[str, Any]] = []
    for name in dict.fromkeys(names):
        op = registry.get(name)
        if op is None:
            message, suggestion = unknown_operation_message(registry, name)
            row: dict[str, Any] = {
                "name": name,
                "errorCode": "UNKNOWN_OPERATION",
                "error": message,
            }
            if suggestion is not None:
                row["suggestion"] = suggestion
            unavailable.append(row)
            continue
        denial = deny_operation(op, policy)
        if denial is not None:
            row = {"name": name, "errorCode": "POLICY_DENIED", "error": denial.message}
            if denial.hint is not None:
                row["hint"] = denial.hint
            unavailable.append(row)
            continue
        entries.append((name, catalog_entry(op, detailed=True, detail=detail)))
    if not entries:
        message = "none of the requested operations is available: " + "; ".join(
            row["error"] for row in unavailable
        )
        if all(row["errorCode"] == "POLICY_DENIED" for row in unavailable):
            return envelope_mod.from_pre_bridge_error(
                error=message,
                error_code="POLICY_DENIED",
                op=None,
                duration_ms=elapsed_ms(started),
                hint=unavailable[0].get("hint"),
            )
        return catalog_invalid_args(message, started)
    result: dict[str, Any] = {
        "categories": list(registry.list_categories()),
        "detail": detail,
        "note": catalog_detail_note(detail),
        "operations": [],
    }
    if unavailable:
        result["unavailable"] = unavailable
    for index, (_, entry) in enumerate(entries):
        result["operations"].append(entry)
        if index > 0 and envelope_mod.byte_len(result) > CATALOG_RESULT_BUDGET_BYTES:
            result["operations"].pop()
            result["omitted"] = [name for name, _ in entries[index:]]
            result["omittedNote"] = CATALOG_OMITTED_NOTE
            break
    return envelope_mod.build_envelope(ok=True, result=result, duration_ms=elapsed_ms(started))


def _flame_catalog_sync(
    registry: Registry,
    policy: Policy,
    category: str | None,
    op_name: Any,
    detail: Any = None,
    response_mode: ResponseMode = compact_mod.DEFAULT_RESPONSE_MODE,
) -> dict[str, Any]:
    started = time.monotonic()

    if detail is None:
        detail = "args" if response_mode == "compact" and op_name is not None else "full"
    if detail not in CATALOG_DETAILS:
        return catalog_invalid_args(
            f"'detail' must be one of {list(CATALOG_DETAILS)}, got {detail!r}", started
        )
    if detail == "args" and op_name is None:
        return catalog_invalid_args(
            "detail='args' needs 'op'; the listing is already compact", started
        )

    if isinstance(op_name, list):
        if not 1 <= len(op_name) <= MAX_CATALOG_OPS or not all(
            isinstance(name, str) for name in op_name
        ):
            return catalog_invalid_args(
                f"'op' as a list must hold 1 to {MAX_CATALOG_OPS} operation names (strings)",
                started,
            )
        return _catalog_by_names(registry, policy, op_name, detail, started)
    if op_name is not None and not isinstance(op_name, str):
        return catalog_invalid_args(
            "'op' must be an operation name or a list of operation names", started
        )

    if op_name is not None:
        op = registry.get(op_name)
        if op is None:
            env = suggest_unknown_operation(registry, op_name)
            env["durationMs"] = elapsed_ms(started)
            return env
        denial = deny_operation(op, policy)
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=elapsed_ms(started),
                hint=denial.hint,
            )
        by_name_result: dict[str, Any] = {
            "categories": list(registry.list_categories()),
            "detail": detail,
            "note": catalog_detail_note(detail),
            "operation": catalog_entry(op, detailed=True, detail=detail),
        }
        return envelope_mod.build_envelope(
            ok=True, result=by_name_result, duration_ms=elapsed_ms(started)
        )

    if category is not None:
        denial = deny_category(category, policy)
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=None,
                duration_ms=elapsed_ms(started),
                hint=denial.hint,
            )
        category_ops = [
            op for op in registry.list_ops(category) if deny_operation(op, policy) is None
        ]
        by_category_result: dict[str, Any] = {
            "note": catalog_listing_note(response_mode),
            "categories": list(registry.list_categories()),
            "operations": [catalog_entry(op) for op in category_ops],
        }
        return envelope_mod.build_envelope(
            ok=True, result=by_category_result, duration_ms=elapsed_ms(started)
        )

    visible = policy_mod.visible_ops(registry, policy)
    all_result: dict[str, Any] = {
        "note": catalog_listing_note(response_mode),
        "categories": list(registry.list_categories()),
        "operations": [catalog_entry(op) for op in visible],
    }
    return envelope_mod.build_envelope(ok=True, result=all_result, duration_ms=elapsed_ms(started))


def _flame_do_sync(runtime: Runtime, args: Mapping[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    op_name = args.get("op")
    if not isinstance(op_name, str):
        return envelope_mod.from_pre_bridge_error(
            error="'op' is required and must be a string",
            error_code="INVALID_ARGS",
            op=None,
            duration_ms=elapsed_ms(started),
        )
    return runtime.call(op_name, args.get("args"))


def _flame_context_sync(
    registry: Registry,
    _policy: Policy,
    run_dir: Path | None,
    mode: ResponseMode = compact_mod.DEFAULT_RESPONSE_MODE,
) -> dict[str, Any]:
    static = agent_context.static_context(registry)
    if mode == "compact":
        static["responseFormat"] = compact_mod.RESPONSE_FORMAT
    br = transport.exec(
        _CONTEXT_CODE,
        thread="main",
        timeout_s=transport.timeout_for("main"),
        label=TOOL_CONTEXT,
        run_dir=run_dir,
    )

    if br.error_code is not None:
        # A bridge-level failure (in particular BRIDGE_LOST, which means Flame may
        # have just crashed) must not be buried in `degraded` under an `ok=true`
        # envelope: it gets the same ok=false/errorCode/hint/retryable/crash
        # treatment flame_do gives it. The static knowledge (undo/thread contract
        # etc.) needs no bridge at all, so it is still returned as-is in `result`.
        error_text, pending = envelope_mod.timeout_error_and_pending(br)
        return envelope_mod.build_envelope(
            ok=False,
            result=static,
            error=error_text,
            error_code=br.error_code,
            traceback=br.traceback,
            duration_ms=br.duration_ms,
            logs=list(br.app_log),
            logs_note=br.app_log_note,
            crash=envelope_mod.crash_meta(br.crash) if br.crash is not None else None,
            timeout_phase=br.timeout_phase,
            pending=pending,
            hint_override=br.hint,
        )

    raw = br.result if isinstance(br.result, dict) else {}
    result: dict[str, Any] = dict(static)
    result["libraries"] = raw.get("libraries")
    result["currentTab"] = raw.get("currentTab")
    raw_context = raw.get("context")
    context = raw_context if isinstance(raw_context, dict) else None
    return envelope_mod.build_envelope(
        ok=True,
        result=result,
        context=context,
        duration_ms=br.duration_ms,
        logs=list(br.app_log),
        logs_note=br.app_log_note,
    )


def create_server(
    registry: Registry,
    policy: Policy,
    *,
    run_dir: Path | None = None,
    journal: Journal | None = None,
    hooks: TestHooks | None = None,
    permissions: dict[str, Any] | None = None,
    response_mode: ResponseMode = compact_mod.DEFAULT_RESPONSE_MODE,
    loop_limits: LoopLimits = DEFAULT_LOOP_LIMITS,
) -> Server[Any]:
    runtime = Runtime(
        registry=registry,
        policy=policy,
        journal=journal if journal is not None else Journal(),
        run_dir=run_dir,
        hooks=hooks if hooks is not None else TestHooks(),
        permissions=permissions,
        response_mode=response_mode,
        loop_limits=loop_limits,
    )

    async def on_list_tools(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tool_definitions(registry, policy, response_mode))

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        args = params.arguments or {}
        if name == TOOL_VERSION_INFO:
            env = await anyio.to_thread.run_sync(_flame_version_info_sync, runtime)
            return _tool_result(env, response_mode)
        if name == TOOL_CATALOG:
            env = await anyio.to_thread.run_sync(
                _flame_catalog_sync,
                registry,
                policy,
                args.get("category"),
                args.get("op"),
                args.get("detail"),
                response_mode,
            )
            return _tool_result(env, response_mode)
        if name == TOOL_DO:
            env = await anyio.to_thread.run_sync(_flame_do_sync, runtime, args)
            return _tool_result(env, response_mode)
        if name == TOOL_CONTEXT:
            env = await anyio.to_thread.run_sync(
                _flame_context_sync, registry, policy, run_dir, response_mode
            )
            return _tool_result(env, response_mode)
        raise MCPError(types.METHOD_NOT_FOUND, f"unknown tool: {name}")

    return Server(
        "flame-mcp",
        version=SERVER_VERSION,
        instructions=instructions_for(response_mode),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
