"""The server's execution runtime: the one place an op call is resolved, gated and run.

`flame_do` (server.py) and the server's own internal calls (a `server_handler` op such as
`journal.check` calling `desktop.get_attributes`, or `plan.run`) all go through
`Runtime.call`, so they share one policy check, one validation, one fail-closed gate and one
envelope shape.

  - `thread == "server"` ops run their `server_handler(args, ctx)` here; `ctx` is this
    Runtime (see `ServerContext`): a handler never touches Flame itself, it calls other ops
    through `ctx.do`.
  - a `value`-undo write op that carries a `JournalSpec` runs through
    `journaled_write.run_journaled_write` (pre-read, intent, compare-and-swap write, outcome).
  - an op that carries a `wrap` (`batch.checkpoint`, `eval.run`) runs its own server-side
    wrapper around the Flame call (`checkpoints.run_checkpoint`, `eval_run.run_eval`).
  - an op that declares a `CheckpointSpec` runs behind an automatic checkpoint, an intent and an
    outcome (`checkpointed.run_declared`).
  - everything else is `execution.run_op`.
  - an op's `precheck` (`eval.run`: the static scan and the effect-dependent gate) runs after
    argument validation and before anything is generated.

Fail closed: when a journaled write ends without an answer (TIMEOUT / BRIDGE_LOST /
BRIDGE_UNAVAILABLE) `WriteGuard` refuses every later non-read op until `journal.check` has
settled the intent -- whatever it finds. The state lives in this process only.

Test hooks (`TestHooks`) exist so a real-Flame e2e can force the two situations a healthy run
never produces (a value changing between the read and the write; the server dying after the
write). They are OFF unless the environment variables below are set, and
scripts/flame_mcp_server.sh (the production entry point) must never set them.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution, opschema
from flame_mcp.callargs import apply_limit_default, apply_param_defaults
from flame_mcp.checkpointed import run_declared
from flame_mcp.journal import Index, Journal
from flame_mcp.journaled_write import run_journaled_write
from flame_mcp.opschema import validate_op_args
from flame_mcp.policy import Denial, Policy, deny_operation
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.types import ServerOpError

TEST_PAUSE_ENV: Final[str] = "FLAME_MCP_TEST_PAUSE_AFTER_PREREAD"
TEST_EXIT_ENV: Final[str] = "FLAME_MCP_TEST_EXIT_AFTER_WRITE"
TEST_WRITE_TIMEOUT_ENV: Final[str] = "FLAME_MCP_TEST_WRITE_TIMEOUT_S"
_EXIT_CODE_AFTER_WRITE: Final[int] = 75
_WRITE_TIMEOUT_LOCK: Final = threading.Lock()


class TestHooksConfigError(ValueError):
    __test__ = False


def _parse_seconds(name: str, raw: str | None) -> float | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise TestHooksConfigError(f"{name} must be a number of seconds, got {raw!r}") from exc
    if not value > 0:
        raise TestHooksConfigError(f"{name} must be > 0, got {raw!r}")
    return value


@dataclass(frozen=True)
class TestHooks:
    """Fault-injection points for the real-Flame e2e. All off by default.

    pause_after_preread_s   sleep between the intent's flush and the write is sent (a window in
                            which the e2e changes the value behind the server's back, or kills it)
    exit_after_write        after the write's answer is in, before the outcome is journaled,
                            end the process at once (os._exit: no buffers flushed)
    write_timeout_s         the exec timeout of the FIRST journaled write only (a tiny value makes
                            it end in TIMEOUT -- an outcome-unknown write inside one process; the
                            writes after it, and every read, use their normal timeouts)
    """

    __test__ = False

    pause_after_preread_s: float | None = None
    exit_after_write: bool = False
    write_timeout_s: float | None = None
    sleep: Callable[[float], None] = time.sleep
    exit_fn: Callable[[int], None] = os._exit
    _write_timeout_used: list[bool] = field(default_factory=list, repr=False, compare=False)

    @staticmethod
    def from_env(env: Mapping[str, str]) -> TestHooks:
        exit_raw = env.get(TEST_EXIT_ENV)
        if exit_raw not in (None, "", "0", "1"):
            raise TestHooksConfigError(f"{TEST_EXIT_ENV} must be unset, '', '0' or '1'")
        return TestHooks(
            pause_after_preread_s=_parse_seconds(TEST_PAUSE_ENV, env.get(TEST_PAUSE_ENV)),
            exit_after_write=exit_raw == "1",
            write_timeout_s=_parse_seconds(TEST_WRITE_TIMEOUT_ENV, env.get(TEST_WRITE_TIMEOUT_ENV)),
        )

    def active(self) -> list[str]:
        names: list[str] = []
        if self.pause_after_preread_s is not None:
            names.append(f"{TEST_PAUSE_ENV}={self.pause_after_preread_s}")
        if self.exit_after_write:
            names.append(f"{TEST_EXIT_ENV}=1")
        if self.write_timeout_s is not None:
            names.append(f"{TEST_WRITE_TIMEOUT_ENV}={self.write_timeout_s}")
        return names

    def take_write_timeout(self) -> float | None:
        """Returns `write_timeout_s` the first time it is asked for, None afterwards."""
        with _WRITE_TIMEOUT_LOCK:
            if self.write_timeout_s is None or self._write_timeout_used:
                return None
            self._write_timeout_used.append(True)
            return self.write_timeout_s

    def pause_after_preread(self) -> None:
        if self.pause_after_preread_s is not None:
            self.sleep(self.pause_after_preread_s)

    def exit_after_write_answer(self) -> None:
        if self.exit_after_write:
            self.exit_fn(_EXIT_CODE_AFTER_WRITE)


@dataclass(frozen=True)
class BlockedIntent:
    intent_id: str
    group: str
    op: str
    reason: str


class WriteGuard:
    """The in-process fail-closed state: the intents whose write ended without an answer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocked: dict[str, BlockedIntent] = {}

    def block(self, intent_id: str, group: str, op: str, reason: str) -> None:
        with self._lock:
            self._blocked[intent_id] = BlockedIntent(intent_id, group, op, reason)

    def release(self, intent_id: str) -> bool:
        with self._lock:
            return self._blocked.pop(intent_id, None) is not None

    def blocked(self) -> list[BlockedIntent]:
        with self._lock:
            return list(self._blocked.values())

    def denial(self) -> Denial | None:
        blocked = self.blocked()
        if not blocked:
            return None
        first = blocked[0]
        more = f" (and {len(blocked) - 1} more)" if len(blocked) > 1 else ""
        return Denial(
            message=(
                f"writes are refused: it is not known whether journaled write group "
                f"{first.group} ({first.op}) landed ({first.reason}){more}"
            ),
            hint=(
                f"Run journal.check (group={first.group!r}) to read Flame again and find out "
                "whether it landed, did not land or diverged. Writes resume once every such "
                "group has been checked, whatever the finding. Reads are not affected."
            ),
        )


class ServerContext(Protocol):
    """What a `server_handler` may use. `do` is the only way to reach Flame."""

    registry: Registry
    policy: Policy
    journal: Journal
    guard: WriteGuard
    write_lock: threading.RLock
    run_dir: Path | None

    def do(
        self,
        op_name: str,
        args: Mapping[str, Any],
        *,
        group: str | None = None,
        reverts: str | None = None,
        reverts_intent: str | None = None,
        precondition: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


@dataclass
class Runtime:
    registry: Registry
    policy: Policy
    journal: Journal
    run_dir: Path | None = None
    hooks: TestHooks = field(default_factory=TestHooks)
    guard: WriteGuard = field(default_factory=WriteGuard)
    write_lock: threading.RLock = field(default_factory=threading.RLock)

    def call(
        self,
        op_name: str,
        raw_args: Any,
        *,
        group: str | None = None,
        reverts: str | None = None,
        reverts_intent: str | None = None,
        precondition: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolves, gates, validates and runs one op, and returns its envelope."""
        started = time.monotonic()
        op = self.registry.get(op_name)
        if op is None:
            return _unknown_operation(self.registry, op_name, started)

        denial = deny_operation(op, self.policy)
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=_elapsed_ms(started),
                hint=denial.hint,
            )

        validation = validate_op_args(op, raw_args)
        if not validation.ok:
            issues = "; ".join(f"{issue.path}: {issue.message}" for issue in validation.issues)
            return envelope_mod.from_pre_bridge_error(
                error=f"invalid arguments: {issues}",
                error_code="INVALID_ARGS",
                op=op,
                duration_ms=_elapsed_ms(started),
            )

        op_args: dict[str, Any] = dict(validation.value or {})
        apply_param_defaults(op, op_args)
        limit_notes = apply_limit_default(op, op_args)

        if op.precheck is not None:
            refusal = op.precheck(op_args, self.policy)
            if refusal is not None:
                return envelope_mod.build_envelope(
                    ok=False,
                    result=refusal.result,
                    error=refusal.message,
                    error_code=refusal.code,
                    execution=envelope_mod.execution_meta(op),
                    duration_ms=_elapsed_ms(started),
                    hint_override=refusal.hint,
                )

        env = self.execute(
            op,
            op_args,
            group=group,
            reverts=reverts,
            reverts_intent=reverts_intent,
            precondition=precondition,
        )
        if limit_notes:
            env["degraded"] = [*env.get("degraded", []), *limit_notes]
        return env

    do = call

    def execute(
        self,
        op: RegisteredOperation,
        op_args: dict[str, Any],
        *,
        group: str | None = None,
        reverts: str | None = None,
        reverts_intent: str | None = None,
        precondition: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        if op.effect != "read":
            denial = self.guard.denial()
            if denial is not None:
                return envelope_mod.from_pre_bridge_error(
                    error=denial.message,
                    error_code="POLICY_DENIED",
                    op=op,
                    duration_ms=_elapsed_ms(started),
                    hint=denial.hint,
                )
        if op.thread == "server":
            return self._run_server_op(op, op_args, started)
        if op.wrap is not None:
            return op.wrap(self, op, op_args, group=group)
        if op.checkpoint is not None:
            return run_declared(self, op, op_args, group=group)
        if op.journal is not None:
            return run_journaled_write(
                self,
                op,
                op_args,
                group=group,
                reverts=reverts,
                reverts_intent=reverts_intent,
                precondition=precondition,
            )
        return execution.run_op(op, op_args, run_dir=self.run_dir)

    def _run_server_op(
        self, op: RegisteredOperation, op_args: dict[str, Any], started: float
    ) -> dict[str, Any]:
        handler = op.server_handler
        if handler is None:  # register() guarantees one for every server-thread op
            raise RuntimeError(f"'{op.name}': server-thread operation without a server_handler")
        meta = envelope_mod.execution_meta(op)
        try:
            outcome = handler(op_args, self)
        except ServerOpError as exc:
            return envelope_mod.build_envelope(
                ok=False,
                result=exc.result,
                error=exc.message,
                error_code=exc.code,
                affected=exc.affected,
                undo_steps=exc.undo_steps if exc.undo_steps is not None else 0,
                partial=exc.partial,
                journal=exc.journal,
                execution=meta,
                duration_ms=_elapsed_ms(started),
                hint_override=exc.hint,
            )
        except Exception as exc:  # a bug in the handler: report it, do not take the tool down
            return envelope_mod.build_envelope(
                ok=False,
                error=f"server handler for '{op.name}' failed (a server bug, not a Flame "
                f"error): {type(exc).__name__}: {exc}",
                error_code="FLAME_API_ERROR",
                traceback=traceback.format_exc(),
                execution=meta,
                duration_ms=_elapsed_ms(started),
            )
        return envelope_mod.build_envelope(
            ok=True,
            result=outcome.result,
            affected=outcome.affected,
            undo_steps=(
                outcome.undo_steps
                if outcome.undo_steps is not None
                else op.undo_steps(op_args, outcome.affected)
            ),
            partial=outcome.partial,
            journal=outcome.journal,
            execution=meta,
            duration_ms=_elapsed_ms(started),
        )

    def journal_summary(self) -> dict[str, Any]:
        """Returns the `journal` section of flame_version_info.

        It reports where the journal is, how many intents are still undecided, and
        whether this process refuses writes right now.
        """
        summary: dict[str, Any] = {
            "dir": str(self.journal.directory),
            "unresolvedIntents": None,
            "skippedLines": None,
            "error": None,
        }
        try:
            read = self.journal.read_all()
            summary["unresolvedIntents"] = len(Index(read.records).in_doubt())
            summary["skippedLines"] = read.skipped_lines
        except OSError as exc:
            summary["error"] = f"journal could not be read: {exc}"
        blocked = self.guard.blocked()
        summary["writesBlocked"] = bool(blocked)
        summary["blocked"] = [
            {"group": b.group, "op": b.op, "reason": b.reason, "id": b.intent_id} for b in blocked
        ]
        summary["testHooks"] = self.hooks.active()
        return summary


def _unknown_operation(registry: Registry, op_name: str, started: float) -> dict[str, Any]:
    names = [op.name for op in registry.list_ops()]
    suggestion = opschema.suggest_name(op_name, names)
    message = f"no operation named '{op_name}' is registered"
    if suggestion is not None:
        message += f" (did you mean '{suggestion}'?)"
    env = envelope_mod.from_pre_bridge_error(
        error=message, error_code="UNKNOWN_OPERATION", op=None, duration_ms=0
    )
    env["durationMs"] = _elapsed_ms(started)
    return env


def warn_if_test_hooks(hooks: TestHooks) -> None:
    active = hooks.active()
    if active:
        print(
            "flame-mcp: WARNING: test hooks are ON (" + ", ".join(active) + "). "
            "They exist only for the real-Flame e2e; never set them for a production server.",
            file=sys.stderr,
        )
