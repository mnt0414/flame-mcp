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

import json
import os
import platform
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, get_args

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from flame_mcp import envelope as envelope_mod
from flame_mcp import opschema, selectors, transport
from flame_mcp import policy as policy_mod
from flame_mcp import registry as registry_mod
from flame_mcp.execution import V_HELPER
from flame_mcp.journal import Journal
from flame_mcp.policy import Policy, deny_category, deny_operation, deny_tool
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.runtime import Runtime, TestHooks
from flame_mcp.types import NO_DEFAULT, SelectorKind
from flame_mcp_bridge.impl import BRIDGE_ABI

SERVER_VERSION: Final[str] = "0.1.0"
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
        "overwrite=true, journal.purge_checkpoints, desktop.delete, journal.revert force=true, "
        "eval.run), ask the user to confirm.",
    ]
)

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


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _tool_result(env: Mapping[str, Any]) -> types.CallToolResult:
    degraded_env = envelope_mod.degrade_for_size(env)
    text = json.dumps(degraded_env, default=str)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=degraded_env,
        is_error=not bool(degraded_env["ok"]),
    )


CATALOG_LISTING_NOTE: Final[str] = (
    "Details of one operation (arguments, sources, selector fields, full description): "
    "flame_catalog(op=<name>). Selector syntax: flame_context's selectorVocabulary. "
    "In params, a trailing * marks a required argument."
)


def _catalog_entry(
    op: RegisteredOperation, policy: Policy, *, detailed: bool = False
) -> dict[str, Any]:
    """Returns one operation as flame_catalog lists it.

    A LISTING (all operations, or a category) is
    compact -- name, category, effect, undo, a one-sentence summary, argument names (`*` =
    required) and the accepted selector kind names -- so the whole catalog stays far below the
    response budget as operations are added. `detailed` (flame_catalog(op=...)) is the full
    description: argument types, sources for thread / pacing, selector fields and example. The
    selector vocabulary is also in flame_context's `selectorVocabulary`, once.
    """
    entry: dict[str, Any]
    if not detailed:
        entry = {
            "name": op.name,
            "category": op.category,
            "effect": op.effect,
            "undo": op.undo,
            "summary": opschema.first_sentence(op.description),
            "params": opschema.compact_params(op.params),
            "accepts": list(op.accepts),
        }
    else:
        entry = {
            "name": op.name,
            "category": op.category,
            "description": op.description,
            "params": {
                "summary": opschema.summarize_params(op.params),
                "details": [
                    {
                        "name": param.name,
                        "type": param.type,
                        "description": param.description,
                        "required": param.required,
                        "nullable": param.nullable,
                        "default": None if param.default is NO_DEFAULT else param.default,
                        "integer": param.integer,
                        "minimum": param.minimum,
                        "itemType": param.item_type,
                        "enum": None if param.enum is None else list(param.enum),
                    }
                    for param in op.params
                ],
            },
            "effect": op.effect,
            "effectLabel": policy_mod.effect_label(op.effect),
            "undo": op.undo,
            "undoLabel": policy_mod.undo_label(op.undo),
            "thread": op.thread,
            "threadSource": op.sources.get("thread"),
            "pacing": op.pacing,
            "pacingSource": op.sources.get("pacing"),
            "accepts": [selectors.describe_kind(kind) for kind in op.accepts],
            "defaultLimit": op.default_limit,
            "maxLimit": op.max_limit,
        }
    return entry


def _suggest_unknown_operation(registry: Registry, op_name: str) -> dict[str, Any]:
    names = [op.name for op in registry.list_ops()]
    suggestion = opschema.suggest_name(op_name, names)
    message = f"no operation named '{op_name}' is registered"
    if suggestion is not None:
        message += f" (did you mean '{suggestion}'?)"
    return envelope_mod.from_pre_bridge_error(
        error=message,
        error_code="UNKNOWN_OPERATION",
        op=None,
        duration_ms=0,
    )


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


def _tool_definitions(registry: Registry, policy: Policy) -> list[types.Tool]:
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
                "List the operations visible under the current policy, compactly: name, category, "
                "effect, undo, one-sentence summary, argument names (* = required) and accepted "
                "selector kinds. Pass `category` to list one category, or `op` for one "
                "operation's full description (argument types, thread / pacing, selector fields "
                "and example, full text). Selector syntax: flame_context's selectorVocabulary."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Restrict the listing to one category.",
                    },
                    "op": {
                        "type": "string",
                        "description": "Describe exactly one operation by name.",
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


PITFALLS: Final[tuple[Mapping[str, str], ...]] = (
    {
        "text": "smart_replace (segment.replace mode=media_and_fx) drops the segment's effects "
        "when the replacing media has none of its own (a Resize that Flame added by itself "
        "because of a bit depth difference stays: measured on Flame 2026.2.3). To keep them "
        "use mode=media (smart_replace_media).",
    },
    {
        "text": "A sequence whose bit depth differs from its media gets a Resize on those segments "
        "from Flame, automatically. On Flame 2026.2.3, Flame refused to delete that Resize; on "
        "every version segment.delete_effect refuses it up front, for safety. "
        "timeline.create_sequence takes bit_depth (8, 10, 12, 16 or 32) so a "
        "sequence can be made with the bit depth of its media; left out, the project's default "
        "applies and it differs per project.",
    },
    {
        "text": "replace matches by offset from the clip start, not by source timecode. "
        "To line up TC, replace then slip by the difference.",
    },
    {
        "text": "PyTime(int) is relative (the sequence start is frame 1). "
        "An absolute position is PyTime(frame, fps) or PyTime(timecode, fps).",
    },
    {
        "text": "A PyAttribute.set_value write may not be put on Ctrl+Z history (seen on the name "
        "of a reel on the Desktop and on a Batch node attribute: Ctrl+Z removed an earlier, "
        "unrelated operation). Do not undo it with Ctrl+Z. Read it back; revert with "
        "journal.revert.",
    },
    {
        "text": "create_batch_group moves the current Batch group. Deleting the current Batch "
        "group moves current to an unpredictable other group.",
    },
    {
        "text": "Deleting a reel that holds clips also deletes the Clip nodes that reference it.",
    },
    {
        "text": "shared_source_segments() does not return copies inside a library "
        "(from the desktop it returns desktop copies only).",
    },
    {
        "text": "Deleting the displayed sequence can leave the Timeline empty "
        "(flame.timeline.clip is None); in other runs it went back to a sequence shown "
        "before it within 3 s.",
    },
    {
        "text": "In at least one project, a sequence copy moved the Timeline to the new copy. "
        "This server reads what the Timeline shows before and after every sequence copy it "
        "makes (checkpoints, desktop.copy, desktop.duplicate) and opens what was shown before "
        "again when it changed (result.display).",
    },
    {
        "text": "trim_head / trim_tail / slip return False even when they worked. "
        "Judge by reading the segment back.",
    },
    {
        "text": "cut outside the sequence and replace with a shorter clip raise nothing "
        "and change nothing.",
    },
    {
        "text": "Flame can crash on a 2-byte character. Every string sent to it (names, paths, "
        "eval.run code) must be ASCII; anything else is refused with INVALID_ARGS before "
        "it is sent.",
    },
)


def _static_context(registry: Registry) -> dict[str, Any]:
    return {
        "undoContract": (
            "Revert with journal.revert(group): the group is in the response's `journal`. Do not "
            "press Ctrl+Z. Every writing op reports undoSteps=0 -- this server does not use "
            "Flame's Undo -- and Flame's Undo has two faults here. A set_value "
            "write may not be put on its history (seen on the name of a reel on the Desktop "
            "and on a Batch node attribute: Ctrl+Z removed an earlier, unrelated operation "
            "instead), so never take a write back with Ctrl+Z; use journal.revert. And the "
            "journal does not know about a Ctrl+Z: after one, a later journal.revert can come "
            "back CONFLICT or alreadyGone. "
            "Writes of attribute values are journaled: the envelope's `journal` ({group, id}) "
            "names the record, journal.list / journal.show read it, and journal.revert(group) "
            "puts the previous values back -- only while each still holds what the write left. "
            "A write whose value changed between the server's read and its write is refused "
            "with errorCode CONFLICT (nothing written); the response's `revert` field is a "
            "ready-to-call op + args. "
            "Batch writes that run behind an automatic checkpoint (a destructive eval.run, "
            "batch.load_setup, batch.create_reel / add_clips / organize) hand back `journal` = "
            "{group, id, checkpoint, revert}: journal.revert(group) restores the checkpoint "
            "while the group's fingerprint (nodes, wiring and the inner nodes of Action / "
            "Image / GMask Tracer nodes) is still the one the call left; what a restore does "
            "not bring back is in each op's description. batch.create_shelf_reel and "
            "batch.iterate cannot be reverted (shelf reels and iterations are outside a setup). "
            "desktop.create, desktop.copy, mediahub.import, desktop.duplicate, desktop.move, "
            "segment.copy_to_media_panel and segment.match hand back `journal` = {group, id, "
            "revert}: journal.revert(group) deletes what they made, only while it is still as "
            "made (a create: still empty; a copy, an import, a duplicate or a clip taken out of "
            "a segment: the same name, duration and content), else CONFLICT and nothing is "
            "deleted; for desktop.move it moves the things back (they get new uids again). "
            "library.create has the same journal and revert: it deletes the library only while "
            "it is empty. desktop.copy of a Batch group (into a library, or back from a "
            "library onto the Desktop) deletes the copy only while it is as made (in a "
            "library its name and reels; on the Desktop also its nodes, connections and node "
            "attribute values), and before it deletes a group on the Desktop it saves the "
            "group's setup as a checkpoint (result.savedSetups: path and how to bring it "
            "back). A closed library reads as empty, so any call whose selectors (a wiretap "
            "id, the first name of a path) or recorded entries name one opens it first and "
            "closes it again afterwards (result.librariesOpened and a note); a library that "
            "cannot be opened, or whose state cannot be read, stops the call "
            "(PRECONDITION_UNMET) before anything is done. "
            "Library contents are never deleted or moved out by this server: desktop.delete "
            "refuses a clip or sequence inside a library and desktop.move refuses a source "
            "inside one (desktop.copy takes something out of a library); only the revert of "
            "its own write and journal.purge_checkpoints remove anything there. A write to a "
            "sequence's segments (segment.trim, slip, replace, "
            "create_effect, delete_effect, set_effect and effect_load_setup) runs behind a "
            "checkpoint of the whole sequence (timeline.checkpoint: a copy in the stash "
            "library flame_mcp_checkpoints) and hands back `journal` = {group, id, checkpoint, "
            "stash, display, revert}: journal.revert(group) restores the copy while the "
            "sequence still has the fingerprint the call left; the restored sequence has a new "
            "uid and "
            "wiretap id and sits at the end of its reel (result.restored.selector). A plan.run "
            "that holds such a write is one checkpointed write on one sequence: one copy, one "
            "journal.revert for the whole plan. desktop.delete copies what it deletes into the "
            "same stash first and journal.revert(group) copies it back (new uid, end of the "
            "reel). journal.purge_checkpoints deletes stashed copies the journal records "
            "(dry_run by default; a real run needs the token of the dry run just before); a "
            "purged copy cannot be restored. "
            "segment.effect_save_setup only writes setup files (undo none: delete them by hand)."
        ),
        "threadContract": (
            "Every Flame API call happens on Flame's own main thread via "
            "schedule_idle_event, reached only through the bridge; this server never "
            "imports or calls the Flame API itself."
        ),
        "selectorVocabulary": [selectors.describe_kind(kind) for kind in get_args(SelectorKind)],
        "defaultLimit": registry_mod.DEFAULT_LIMIT,
        "maxLimit": registry_mod.MAX_LIMIT,
        "categories": list(registry.list_categories()),
        "pitfalls": [dict(pitfall) for pitfall in PITFALLS],
    }


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
    }
    env = envelope_mod.build_envelope(ok=True, result=result, duration_ms=_elapsed_ms(started))
    env["notices"].extend(notices)
    return env


def _flame_catalog_sync(
    registry: Registry,
    policy: Policy,
    category: str | None,
    op_name: str | None,
) -> dict[str, Any]:
    started = time.monotonic()

    if op_name is not None:
        op = registry.get(op_name)
        if op is None:
            env = _suggest_unknown_operation(registry, op_name)
            env["durationMs"] = _elapsed_ms(started)
            return env
        denial = deny_operation(op, policy)
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=_elapsed_ms(started),
                hint=denial.hint,
            )
        by_name_result: dict[str, Any] = {
            "categories": list(registry.list_categories()),
            "operation": _catalog_entry(op, policy, detailed=True),
        }
        return envelope_mod.build_envelope(
            ok=True, result=by_name_result, duration_ms=_elapsed_ms(started)
        )

    if category is not None:
        denial = deny_category(category, policy)
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=None,
                duration_ms=_elapsed_ms(started),
                hint=denial.hint,
            )
        category_ops = [
            op for op in registry.list_ops(category) if deny_operation(op, policy) is None
        ]
        by_category_result: dict[str, Any] = {
            "note": CATALOG_LISTING_NOTE,
            "categories": list(registry.list_categories()),
            "operations": [_catalog_entry(op, policy) for op in category_ops],
        }
        return envelope_mod.build_envelope(
            ok=True, result=by_category_result, duration_ms=_elapsed_ms(started)
        )

    visible = policy_mod.visible_ops(registry, policy)
    all_result: dict[str, Any] = {
        "note": CATALOG_LISTING_NOTE,
        "categories": list(registry.list_categories()),
        "operations": [_catalog_entry(op, policy) for op in visible],
    }
    return envelope_mod.build_envelope(ok=True, result=all_result, duration_ms=_elapsed_ms(started))


def _flame_do_sync(runtime: Runtime, args: Mapping[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    op_name = args.get("op")
    if not isinstance(op_name, str):
        return envelope_mod.from_pre_bridge_error(
            error="'op' is required and must be a string",
            error_code="INVALID_ARGS",
            op=None,
            duration_ms=_elapsed_ms(started),
        )
    return runtime.call(op_name, args.get("args"))


def _flame_context_sync(
    registry: Registry, _policy: Policy, run_dir: Path | None
) -> dict[str, Any]:
    static = _static_context(registry)
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
) -> Server[Any]:
    runtime = Runtime(
        registry=registry,
        policy=policy,
        journal=journal if journal is not None else Journal(),
        run_dir=run_dir,
        hooks=hooks if hooks is not None else TestHooks(),
    )

    async def on_list_tools(
        _ctx: Any, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tool_definitions(registry, policy))

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        args = params.arguments or {}
        if name == TOOL_VERSION_INFO:
            env = await anyio.to_thread.run_sync(_flame_version_info_sync, runtime)
            return _tool_result(env)
        if name == TOOL_CATALOG:
            env = await anyio.to_thread.run_sync(
                _flame_catalog_sync, registry, policy, args.get("category"), args.get("op")
            )
            return _tool_result(env)
        if name == TOOL_DO:
            env = await anyio.to_thread.run_sync(_flame_do_sync, runtime, args)
            return _tool_result(env)
        if name == TOOL_CONTEXT:
            env = await anyio.to_thread.run_sync(_flame_context_sync, registry, policy, run_dir)
            return _tool_result(env)
        raise MCPError(types.METHOD_NOT_FOUND, f"unknown tool: {name}")

    return Server(
        "flame-mcp",
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
