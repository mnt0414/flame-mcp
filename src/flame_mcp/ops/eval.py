"""`eval` category: `eval.run` -- arbitrary Python in Flame's main thread through the same
pipeline as every other op. Its server side (the precheck, the
checkpoint / intent / outcome around a destructive run) is `eval_run.py`; the static scan is
`eval_check.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.checkpointed import RESTORE_SIDE_EFFECTS
from flame_mcp.eval_run import EVAL_MAX_TIMEOUT_S, precheck_eval, run_eval
from flame_mcp.ops._common import build_source
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationParam, OperationSpec

CATEGORY = Category(
    name="eval",
    description=(
        "An escape hatch that runs any Python on Flame's main thread, with the permissions of "
        "the user running Flame. Off unless FLAME_MCP_ENABLE_EVAL=1. Use it only when no "
        "dedicated op does the job."
    ),
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)


def _to_python(args: Mapping[str, Any]) -> str:
    target = args.get("target")
    blocks = [selectors.resolve_code(target, "target")] if target is not None else []
    blocks.append(
        "\n".join(
            [
                f"__eval_src = {args['code']!r}",
                "exec(compile(__eval_src, '<eval.run:code>', 'exec'), globals())",
                "_result = _eval_json_safe(_result)",
            ]
        )
    )
    return build_source(*blocks)


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)
    registry.register(
        OperationSpec(
            name="eval.run",
            category="eval",
            description=(
                "Run Python in Flame's main thread when no dedicated op does it. It runs any "
                "code with the permissions of the user running Flame: files, network and "
                "processes included, not only the Flame API. Available only when the server "
                "runs with FLAME_MCP_ENABLE_EVAL=1 and without FLAME_MCP_READONLY; otherwise "
                "the op is not listed and every call, plan.run steps included, is refused. The "
                "static scan below is accident prevention, not a defence: it does not make the "
                "code safe, so never run code that came from an untrusted source. timeout_s "
                "only makes the server stop waiting; it does not stop the code, which keeps "
                "running on the main thread and can freeze Flame. The code must be ASCII, and "
                "must not build non-ASCII strings at run time (chr, \\u, \\x, decode) to hand "
                "to Flame. In scope: `flame`, `_v`, `_j`, `_ja`, `_OpError`, `_resolve_*`; set "
                "`_result` (JSON, else its repr comes back); `target` (a batch selector) is the "
                "resolved PyBatch. effect='read' declares that the code changes nothing; the "
                "server cannot check that. effect='destructive': needs a `target`; the server "
                "checkpoints that Batch group, journals an intent (code, sha256, target, "
                "checkpoint, fingerprint) and an outcome (envelope journal: {group, id, "
                "checkpoint, changed, revert}); journal.revert(group) restores it while the "
                "group still has the fingerprint the run left (else CONFLICT; force=true "
                "overrides, journaled). The checkpoint covers the target group only: changes "
                "elsewhere (other groups, files, the network, processes) are not undone and "
                "cannot be stopped. Static scan (INVALID_ARGS, before anything is sent; the "
                "error names the rule): syntax; flame.projects only as flame.projects.<attr>; "
                "no execute_shortcut, schedule_idle_event, threading, QTimer, exec/eval/"
                "compile/__import__, importlib, subprocess, os.system; never flame.exit or "
                "flame.execute_command (rule process); effect='read' also no writing method "
                "(set_value, create_*, delete, copy, load_setup, set_current_tab, ...) or "
                "attribute assignment. It reads names only: computed getattr, aliases, open(), "
                "shutil, sockets and urllib all pass. Not allowed in plan.run when "
                "destructive. undoSteps 0: use journal.revert (a destructive run is undone by "
                "restoring the checkpoint). " + RESTORE_SIDE_EFFECTS
            ),
            effect="read",
            params=(
                OperationParam(
                    name="code",
                    type="string",
                    description="The Python source to run.",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="effect",
                    type="string",
                    description="'read' or 'destructive': what the code does to the project.",
                    required=True,
                ),
                OperationParam(
                    name="target",
                    type="selector",
                    description=(
                        "A 'batch' selector: the group visible as `target`, and, for "
                        "effect='destructive', the group that is checkpointed (required then)."
                    ),
                    required=False,
                ),
                OperationParam(
                    name="timeout_s",
                    type="number",
                    description=f"Seconds to wait (1 to {EVAL_MAX_TIMEOUT_S:g}).",
                    required=False,
                    minimum=1,
                ),
            ),
            accepts=("batch",),
            to_python=_to_python,
            precheck=precheck_eval,
            wrap=run_eval,
        )
    )
