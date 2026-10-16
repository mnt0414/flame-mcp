"""Closed enums and declaration-time data shapes.

Stdlib-only: this module is shared with the in-Flame bridge (Python 3.11.5).
Do not add third-party imports or 3.12-only syntax here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

ParamType = Literal["string", "number", "boolean", "array", "object", "any", "selector"]
Effect = Literal["read", "reversible", "create", "destructive"]
Thread = Literal["main", "main-async", "server"]
Pacing = Literal["none", "structural", "media"]
OnError = Literal["abort", "continue"]
Undo = Literal["none", "value", "checkpoint", "unsupported"]
Depth = Literal["library", "reel", "clip", "version", "track", "segment"]
SelectorKind = Literal[
    "selection", "wiretap", "path", "segment", "batch", "node", "action_node", "desktop", "timeline"
]
ErrorCode = Literal[
    "SELECTOR_EMPTY",
    "SELECTOR_TYPE",
    "SELECTOR_AMBIGUOUS",
    "FLAME_API_ERROR",
    "SILENT_NOOP",
    "TIMEOUT",
    "MAIN_THREAD_UNAVAILABLE",
    "BRIDGE_LOST",
    "BRIDGE_UNAVAILABLE",
    "BRIDGE_AMBIGUOUS",
    "PRECONDITION_UNMET",
    "UNKNOWN_OPERATION",
    "INVALID_ARGS",
    "POLICY_DENIED",
    "CONFLICT",
]


class _NoDefault:
    """Sentinel distinguishing 'no default declared' from 'default=None'."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NO_DEFAULT"


NO_DEFAULT: Final = _NoDefault()


@dataclass(frozen=True)
class OperationParam:
    name: str
    type: ParamType
    description: str
    required: bool = False
    default: Any = NO_DEFAULT
    nullable: bool = False
    non_empty: bool = False
    """string-only: reject "" at validate_op_args time (INVALID_ARGS)."""
    integer: bool = False
    """number-only: reject non-integers at validate_op_args time (INVALID_ARGS)."""
    minimum: float | None = None
    """number-only: reject values below this at validate_op_args time (INVALID_ARGS)."""
    item_type: ParamType | None = None
    """array-only: every element must match this type (INVALID_ARGS)."""
    enum: tuple[Any, ...] | None = None
    """string / number: the only values accepted (INVALID_ARGS for any other); published by
    flame_catalog as `enum`."""


@dataclass(frozen=True)
class ServerResult:
    """What a `server_handler` returns on success (a failure raises `ServerOpError`).

    `undo_steps` None means "ask the op's own `undo_steps`"; `journal` is the envelope's
    `{group, id}` when the call left journal records (plan.run: its group).
    """

    result: Any
    affected: int = 0
    partial: Mapping[str, int] | None = None
    undo_steps: int | None = None
    journal: Mapping[str, Any] | None = None


class ServerOpError(Exception):
    """Raised by a `server_handler` to end in a specific ErrorCode.

    `result` is kept beside the error (like an `_OpError` raised after `_result` was assigned) and
    `hint`, when given, replaces the generic hint of `code`. `affected` / `undo_steps` /
    `partial` / `journal` fill the envelope the same way `ServerResult` does (a plan that
    stopped half way still reports what its finished steps did).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        result: Any = None,
        hint: str | None = None,
        affected: int = 0,
        undo_steps: int | None = None,
        partial: Mapping[str, int] | None = None,
        journal: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.result = result
        self.hint = hint
        self.affected = affected
        self.undo_steps = undo_steps
        self.partial = partial
        self.journal = journal


ServerHandler = Callable[[Mapping[str, Any], Any], ServerResult]
"""`(args, ctx) -> ServerResult`. `ctx` is the `runtime.ServerContext` (typed `Any` here so
this module stays stdlib-only and free of import cycles)."""


@dataclass(frozen=True)
class Refusal:
    """What an op's `precheck` returns to end a call before anything is generated or sent.

    Holds the ErrorCode, the message, an optional replacement hint and an optional `result`.
    """

    code: ErrorCode
    message: str
    hint: str | None = None
    result: Any = None


Precheck = Callable[[Mapping[str, Any], Any], Refusal | None]
"""`(validated args, policy) -> Refusal | None`: the checks that need more than a parameter's
type (`eval.run`: the static scan of its code, the effect-dependent gate). `policy` is the
`policy.Policy` (typed `Any` for the same reason as `ServerHandler`'s ctx)."""

WrapHandler = Callable[..., dict[str, Any]]
"""`(runtime, op, args, *, group) -> envelope`: an op that runs in Flame but whose call the
server wraps (`batch.checkpoint` journals the checkpoint it took; `eval.run` takes a
checkpoint and journals an intent / outcome around a destructive run). The handler runs
the Flame side itself through `execution.run_op`."""


@dataclass(frozen=True)
class JournalSpec:
    """How a `value`-undo write op is journaled.

    The server runs the op in two round trips: a pre-read through `read_op` (a read op
    taking `target` and `keys`) that yields the previous values and a stable address of
    the target, an intent record, then the op itself with a compare-and-swap on those
    values.

    `keys(args)` / `values(args)`: the attribute names the call writes and the values it
    writes (JSON-form; both dicts share the same keys). `write_args(target, values)`
    rebuilds the op's own arguments for a given target address, for writing through the
    stable address and for reverting.
    """

    read_op: str
    keys: Callable[[Mapping[str, Any]], list[str]]
    values: Callable[[Mapping[str, Any]], dict[str, Any]]
    write_args: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


CheckpointCheck = Callable[[Any, Mapping[str, Any]], Refusal | None]
"""`(runtime, validated args) -> Refusal | None`: a check that needs Flame or the journal and
must end the call BEFORE a checkpoint is taken (`batch.iterate`: the iteration to overwrite
exists). It runs under the write lock, once the fail-closed gate has been passed. `runtime`
is the `runtime.Runtime` (typed `Any` for the same reason as `ServerHandler`'s ctx)."""


@dataclass(frozen=True)
class CheckpointSpec:
    """A Batch write that runs behind an automatic checkpoint.

    The server takes a checkpoint of the target group first, records an intent
    (fingerprint before, the checkpoint id), runs the op, reads the group's fingerprint
    back and records the outcome.
    `journal.revert(group)` then restores that checkpoint, but only while the group's fingerprint
    is still the one the run left. Declared instead of `journal` / `wrap`; the op needs no
    other server code (`checkpointed.run_checkpointed`).

    `batch_arg`: the name of the selector-typed parameter that addresses the Batch group.
    `group_selector` / `rebind`: for an op whose `batch_arg` holds a `node` /
    `action_node` selector (the group is then the selector's `batch`, however deeply nested):
    `group_selector(args)` is the `batch` selector of the group to checkpoint, and
    `rebind(args, stable)` returns `args` with every selector that names that group re-pointed
    at `stable` (the group addressed by uid), so the run acts on the group that was
    checkpointed. Give both or neither; without them `args[batch_arg]` is the group selector.
    `label`: the note stored on the checkpoint (default `before <op name>`).
    `check`: see `CheckpointCheck`.
    `timeout_s`: the exec timeout of the run itself (default: the thread's).
    `target_kind`: `"timeline"` makes the checkpointed thing a sequence / clip on the
    Desktop (`timeline.checkpoint` / `timeline.fingerprint`) instead of a Batch group. `batch_arg`
    then holds a wiretap / path / selection selector of it, or (with `group_selector` /
    `rebind`) a `segment` selector whose parent it is.
    """

    batch_arg: str = "target"
    label: Callable[[Mapping[str, Any]], str] | None = None
    check: CheckpointCheck | None = None
    timeout_s: float | None = None
    group_selector: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    rebind: Callable[[Mapping[str, Any], dict[str, Any]], dict[str, Any]] | None = None
    target_kind: Literal["batch", "timeline"] = "batch"


@dataclass(frozen=True)
class Category:
    name: str
    description: str
    default_thread: Thread
    default_pacing: Pacing
    allowed_thread: frozenset[Thread]
    allowed_pacing: frozenset[Pacing]
    preconditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationSpec:
    """Declaration an op author writes.

    `None` on the inheriting fields means 'resolve through category, then global
    default' (the 3-stage default).
    """

    name: str
    category: str
    description: str
    effect: Effect
    params: tuple[OperationParam, ...] = ()
    thread: Thread | None = None
    pacing: Pacing | None = None
    on_error: OnError | None = None
    undo: Undo | None = None
    phase_size: int | None = None
    default_depth: Depth | None = None
    default_limit: int | None = None
    max_limit: int | None = None
    accepts: tuple[SelectorKind, ...] = ()
    requires: tuple[str, ...] = ()
    diagnose: tuple[str, ...] = ()
    undoable: bool = True
    to_python: Callable[[Mapping[str, Any]], str] | None = None
    verify: Callable[[Mapping[str, Any]], str | None] | None = None
    undo_steps: Callable[[Mapping[str, Any], int], int] | None = None
    sample_args: Mapping[str, Any] | None = None
    server_handler: ServerHandler | None = None
    journal: JournalSpec | None = None
    precheck: Precheck | None = None
    wrap: WrapHandler | None = None
    checkpoint: CheckpointSpec | None = None
