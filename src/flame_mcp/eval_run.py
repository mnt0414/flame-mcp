"""The server side of `eval.run`.

Provides the precheck every call gets, and the wrapper that puts a destructive run
behind a checkpoint and a journal intent / outcome.

`eval.run(code, effect, target?, timeout_s?)` is one op, registered wherever the `eval`
category is, but hidden and refused as a whole unless FLAME_MCP_ENABLE_EVAL=1 and
FLAME_MCP_READONLY is unset (`policy.deny_operation`; `precheck_eval` and `run_eval` check it
again). It runs any Python with the permissions of the user running Flame. What the caller
declares in `effect` decides the rest of the gate:

  effect='read'         The code is scanned (`eval_check`: accident prevention, not a sandbox)
                        and run in Flame's main thread inside the usual wrapper. Not a claim
                        that it is safe: reads that crash Flame are reported, and the scan does
                        not stop file, network or process access.
  effect='destructive'  needs a `target` Batch group. Before the code runs, the server takes a
                        checkpoint of that group and journals an intent (the full code and its
                        sha256, the target, the checkpoint id, the group's fingerprint);
                        afterwards it journals the outcome with the group's new fingerprint.
                        `journal.revert(group)` then restores the checkpoint -- while the
                        group still has the fingerprint the run left.

The checkpoint covers the `target` group only. Code that changes anything else (another Batch
group, the Desktop, a Library, a clip's media, files) is not undone by it, and nothing stops it.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution, transport
from flame_mcp.checkpointed import (
    Judgement,
    PreRunRefusal,
    Words,
    refusal_envelope,
    run_checkpointed,
    run_envelope,
)
from flame_mcp.eval_check import check_eval_code
from flame_mcp.policy import Policy, deny_eval_effect
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import ErrorCode, Refusal

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

OP_NAME: Final[str] = "eval.run"
EFFECTS: Final[tuple[str, ...]] = ("read", "destructive")
EVAL_MAX_TIMEOUT_S: Final[float] = 120.0
"""Longest `timeout_s` a call may ask for. The code runs on Flame's main thread: a long one
freezes the UI for as long as it runs."""


def precheck_eval(args: Mapping[str, Any], policy: Policy) -> Refusal | None:
    """Returns a Refusal for anything about an `eval.run` call refused before Flame is touched.

    The order matters: the policy first, so a server with the switch off says so
    whatever the code looks like.
    """
    effect = args.get("effect")
    if effect not in EFFECTS:
        return Refusal("INVALID_ARGS", f"effect must be one of {list(EFFECTS)}, got {effect!r}")
    denial = deny_eval_effect(str(effect), policy)
    if denial is not None:
        return Refusal("POLICY_DENIED", denial.message, hint=denial.hint)
    if effect == "destructive" and args.get("target") is None:
        return Refusal(
            "INVALID_ARGS",
            "a destructive eval needs a `target` (a batch selector): the checkpoint that makes "
            "it revertible covers that Batch group only",
            hint="Pass target={'kind': 'batch', 'name': ...} (or uid), or run the code with "
            "effect='read'.",
        )
    timeout = args.get("timeout_s")
    if timeout is not None and timeout > EVAL_MAX_TIMEOUT_S:
        return Refusal(
            "INVALID_ARGS",
            f"timeout_s must be at most {EVAL_MAX_TIMEOUT_S:g}, got {timeout!r}: the code runs on "
            "Flame's main thread",
        )
    violations = check_eval_code(str(args.get("code", "")), str(effect))
    if violations:
        shown = "; ".join(str(v) for v in violations)
        return Refusal(
            "INVALID_ARGS",
            f"the code was refused by the static scan (nothing was sent to Flame): {shown}",
            hint=(
                "The scan only stops plain violations of what was declared; passing it proves "
                "nothing about the code."
            ),
            result={"ran": False, "violations": [v.as_dict() for v in violations]},
        )
    return None


def code_sha256(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _execution_meta(op: RegisteredOperation) -> dict[str, Any]:
    meta = envelope_mod.execution_meta(op)
    meta["effect"] = "destructive"
    meta["undo"] = "checkpoint"
    return meta


def _refused(
    op: RegisteredOperation,
    code: ErrorCode,
    message: str,
    started: float,
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=False,
        error=message,
        error_code=code,
        execution=_execution_meta(op),
        duration_ms=_elapsed_ms(started),
        hint_override=hint,
    )


def run_eval(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` handler of `eval.run`."""
    started = time.monotonic()
    timeout = op_args.get("timeout_s")
    timeout_s = float(timeout) if timeout is not None else transport.timeout_for(op.thread)
    denial = deny_eval_effect(str(op_args["effect"]), rt.policy)
    if denial is not None:  # the second of the two gates (the first is the precheck)
        return _refused(op, "POLICY_DENIED", denial.message, started, hint=denial.hint)
    if op_args["effect"] == "read":
        return execution.run_op(op, op_args, run_dir=rt.run_dir, timeout_s=timeout_s)

    with rt.write_lock:
        guard_denial = rt.guard.denial()
        if guard_denial is not None:  # this call queued behind a write that ended unknown
            return _refused(
                op, "POLICY_DENIED", guard_denial.message, started, hint=guard_denial.hint
            )
        return _run_destructive(rt, op, op_args, group, timeout_s, started)


def _judge(env: dict[str, Any], fingerprint_after: str | None) -> Judgement:
    ok = env.get("ok") is True
    return Judgement(
        ok=ok,
        failed=None if ok else str(env.get("error")),
        error_code=None if ok else str(env.get("errorCode")),
    )


def _run_destructive(
    rt: Runtime,
    op: RegisteredOperation,
    op_args: dict[str, Any],
    group: str | None,
    timeout_s: float,
    started: float,
) -> dict[str, Any]:
    code = str(op_args["code"])
    sha = code_sha256(code)
    meta = _execution_meta(op)

    def run(stable: dict[str, Any]) -> dict[str, Any]:
        return execution.run_op(
            op, {**op_args, "target": stable}, run_dir=rt.run_dir, timeout_s=timeout_s
        )

    try:
        facts = run_checkpointed(
            rt,
            selector=op_args["target"],
            op_name=OP_NAME,
            kind="eval",
            args={
                "effect": op_args["effect"],
                "target": op_args["target"],
                "timeout_s": op_args.get("timeout_s"),
            },
            intent_extra={"code": code, "codeSha256": sha},
            expected_fp=None,
            label=f"before eval {sha[:12]}",
            run=run,
            judge=_judge,
            failed_key="eval",
            group=group,
        )
    except PreRunRefusal as exc:
        return refusal_envelope(op, exc, _WORDS, meta, started, journal_dir=rt.journal.directory)
    return run_envelope(facts, _WORDS, meta, failure_hint=False)


_WORDS = Words(what="this eval", ran="the code", refuses="destructive code")
