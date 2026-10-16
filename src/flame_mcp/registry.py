"""Operation/Category registry: the single source of truth for catalog and do.

Adapted from mcp-aftereffects `src/registry.ts` (kumo.productions, Inc., MIT License; see
THIRD_PARTY_NOTICES.md).

Registration-time validation is the safety net for the paths mypy strict and the
exhaustiveness tests cannot see: an OperationSpec built from data at runtime rather
than typed literally in code.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, assert_never, get_args

from flame_mcp import api_symbols
from flame_mcp.types import (
    NO_DEFAULT,
    Category,
    CheckpointSpec,
    Depth,
    Effect,
    JournalSpec,
    OnError,
    OperationParam,
    OperationSpec,
    Pacing,
    ParamType,
    Precheck,
    SelectorKind,
    ServerHandler,
    Thread,
    Undo,
    WrapHandler,
)

Source = Literal["op", "category", "global"]

# Defaults for the `limit` parameter (layer 2 of the response limit; not derived from
# measured response sizes).
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

GLOBAL_DEFAULT_THREAD: Thread = "main"
GLOBAL_DEFAULT_PACING: Pacing = "none"
GLOBAL_DEFAULT_ON_ERROR: OnError = "abort"

_REPRESENTATIVE_VALUES: dict[ParamType, Any] = {
    "string": "sample",
    "number": 1,
    "boolean": True,
    "array": [],
    "object": {},
    "any": "sample",
}

_LOOP_NODE_TYPES = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


class RegistrationError(ValueError):
    """Raised by Registry.register / register_category on a bad declaration."""


def default_undo_for(effect: Effect) -> Undo:
    """Effect -> undo strategy table."""
    match effect:
        case "read":
            return "none"
        case "reversible":
            return "value"
        case "create" | "destructive":
            return "checkpoint"
        case _ as unreachable:
            assert_never(unreachable)


def default_phase_size(pacing: Pacing) -> int:
    match pacing:
        case "none":
            return 50
        case "structural" | "media":
            return 5
        case _ as unreachable:
            assert_never(unreachable)


def touches_flame(thread: Thread) -> bool:
    match thread:
        case "main" | "main-async":
            return True
        case "server":
            return False
        case _ as unreachable:
            assert_never(unreachable)


def continues_on_error(on_error: OnError) -> bool:
    match on_error:
        case "abort":
            return False
        case "continue":
            return True
        case _ as unreachable:
            assert_never(unreachable)


def _always_zero_undo_steps(args: Mapping[str, Any], target_count: int) -> int:
    return 0


@dataclass(frozen=True)
class RegisteredOperation:
    """Every field of the spec, resolved to its effective value (op, category, global default)."""

    name: str
    category: str
    description: str
    effect: Effect
    params: tuple[OperationParam, ...]
    thread: Thread
    pacing: Pacing
    on_error: OnError
    accepts: tuple[SelectorKind, ...]
    requires: tuple[str, ...]
    diagnose: tuple[str, ...]
    undoable: bool
    undo: Undo
    phase_size: int
    default_depth: Depth | None
    default_limit: int
    max_limit: int
    to_python: Callable[[Mapping[str, Any]], str] | None
    verify: Callable[[Mapping[str, Any]], str | None] | None
    undo_steps: Callable[[Mapping[str, Any], int], int]
    sample_args: Mapping[str, Any]
    sources: Mapping[str, Source]
    server_handler: ServerHandler | None = None
    journal: JournalSpec | None = None
    precheck: Precheck | None = None
    wrap: WrapHandler | None = None
    checkpoint: CheckpointSpec | None = None


def _check_member(op_name: str, field_name: str, value: str, literal_type: Any) -> None:
    if value not in get_args(literal_type):
        raise RegistrationError(
            f"'{op_name}': {field_name} '{value}' is not a member of {literal_type}"
        )


def _validate_literal_members(
    cat_name: str, field_name: str, values: frozenset[str], literal_type: Any
) -> None:
    valid = set(get_args(literal_type))
    invalid = set(values) - valid
    if invalid:
        raise RegistrationError(
            f"category '{cat_name}': {field_name} has invalid member(s) {sorted(invalid)}"
        )


def _is_well_formed_symbol(symbol: str) -> bool:
    return bool(_SYMBOL_RE.match(symbol))


def sample_selector_for(accepts: tuple[SelectorKind, ...]) -> dict[str, Any]:
    """Returns the smallest well-formed selector of `accepts`'s first kind.

    Used as the default `op.sample_args` entry for a selector param (the selector
    ParamType), and as the SelectorKind exhaustiveness probe in
    tests/test_exhaustiveness.py.

    Registration's own pacing/loop smoke test does not rely on this covering
    every kind by itself: `register()` separately calls this once per kind in
    `spec.accepts` (see `_sample_args_for_kind`) because an op is free to branch on
    which kind it was handed, so `accepts[0]` alone is not a sufficient sample.
    """
    kind = accepts[0] if accepts else "selection"
    match kind:
        case "selection":
            return {"kind": "selection"}
        case "wiretap":
            return {"kind": "wiretap", "id": "sample"}
        case "path":
            return {"kind": "path", "path": ["sample"]}
        case "segment":
            return {
                "kind": "segment",
                "parent": {"kind": "wiretap", "id": "sample"},
                "version": 0,
                "track": 0,
                "index": 0,
            }
        case "batch":
            return {"kind": "batch", "name": "sample"}
        case "node":
            return {"kind": "node", "batch": {"kind": "batch", "name": "sample"}, "name": "sample"}
        case "action_node":
            return {
                "kind": "action_node",
                "node": {
                    "kind": "node",
                    "batch": {"kind": "batch", "name": "sample"},
                    "name": "sample",
                },
                "name": "sample",
            }
        case "desktop":
            return {"kind": "desktop"}
        case "timeline":
            return {"kind": "timeline"}
        case _ as unreachable:
            assert_never(unreachable)


def _sample_value(param: OperationParam, accepts: tuple[SelectorKind, ...]) -> Any:
    # Never hand a nullable/absent param a None sample: the registration-time
    # to_python smoke test should exercise real values, not the None branch.
    if param.default is not NO_DEFAULT and param.default is not None:
        return param.default
    if param.type == "selector":
        return sample_selector_for(accepts)
    return _REPRESENTATIVE_VALUES[param.type]


def _sample_args_for_kind(spec: OperationSpec, kind: SelectorKind) -> dict[str, Any]:
    """Returns `sample_args` with every selector-typed param forced to `kind`.

    Non-selector params keep whatever `spec.sample_args` declares, falling back to
    `_sample_value` as in the default sample derivation. Used to smoke-test each of
    an op's `accepts` kinds individually.
    """
    base: dict[str, Any]
    if spec.sample_args is not None:
        base = dict(spec.sample_args)
    else:
        base = {param.name: _sample_value(param, spec.accepts) for param in spec.params}
    for param in spec.params:
        if param.type == "selector":
            base[param.name] = sample_selector_for((kind,))
    return base


def _check_to_python_output(
    name: str,
    generated: str,
    *,
    effect: Effect,
    pacing: Pacing,
    kind: SelectorKind | None = None,
) -> None:
    """Runs ast.parse and the write-op pacing/loop guard on `generated`.

    Shared by the default sample_args smoke test and the per-`accepts`-kind smoke test.
    `kind` is only for the error message (which sample produced `generated`).
    """
    context = f" (accepts kind {kind!r})" if kind is not None else ""
    try:
        tree = ast.parse(generated)
    except SyntaxError as exc:
        raise RegistrationError(
            f"'{name}': to_python output is not valid Python{context}: {exc}"
        ) from exc

    # Loop protection only guards writes: the pacing gate exists to phase
    # repeated *writes* (a SEGV was observed after repeated writes in one
    # call), so a read op that loops is out of its scope.
    if effect != "read" and pacing == "none":
        has_loop = any(isinstance(node, _LOOP_NODE_TYPES) for node in ast.walk(tree))
        if has_loop:
            raise RegistrationError(
                f"'{name}': generated code contains a loop/comprehension{context} but "
                "pacing is 'none' (write operations must declare a pacing "
                "that phases repeated calls)"
            )


class Registry:
    def __init__(self, symbols: frozenset[str] | None = None) -> None:
        self._categories: dict[str, Category] = {}
        self._operations: dict[str, RegisteredOperation] = {}
        self._symbols = symbols if symbols is not None else api_symbols.default_symbols()

    def register_category(self, category: Category) -> None:
        name = category.name
        if name in self._categories:
            raise RegistrationError(f"category '{name}': already registered")
        if not category.allowed_thread:
            raise RegistrationError(f"category '{name}': allowed_thread must not be empty")
        if not category.allowed_pacing:
            raise RegistrationError(f"category '{name}': allowed_pacing must not be empty")

        _validate_literal_members(name, "allowed_thread", category.allowed_thread, Thread)
        _validate_literal_members(name, "allowed_pacing", category.allowed_pacing, Pacing)

        touches = {touches_flame(thread) for thread in category.allowed_thread}
        if len(touches) != 1:
            raise RegistrationError(
                f"category '{name}': allowed_thread mixes Flame-touching and "
                f"non-touching threads, got {sorted(category.allowed_thread)} "
                "(a category must not straddle touches_flame)"
            )

        if category.default_thread not in category.allowed_thread:
            raise RegistrationError(f"category '{name}': default_thread not in allowed_thread")
        if category.default_pacing not in category.allowed_pacing:
            raise RegistrationError(f"category '{name}': default_pacing not in allowed_pacing")

        self._categories[name] = category

    def register(self, spec: OperationSpec) -> RegisteredOperation:
        name = spec.name
        if name in self._operations:
            raise RegistrationError(f"'{name}': already registered")
        if spec.category not in self._categories:
            raise RegistrationError(f"'{name}': unregistered category '{spec.category}'")
        prefix = f"{spec.category}."
        if not name.startswith(prefix):
            raise RegistrationError(f"'{name}': name must start with '{prefix}'")

        param_names = [p.name for p in spec.params]
        if len(param_names) != len(set(param_names)):
            raise RegistrationError(f"'{name}': duplicate parameter name in params")

        _check_member(name, "effect", spec.effect, Effect)
        for param in spec.params:
            _check_member(name, f"params.{param.name}.type", param.type, ParamType)
        for kind in spec.accepts:
            _check_member(name, "accepts", kind, SelectorKind)
        if spec.thread is not None:
            _check_member(name, "thread", spec.thread, Thread)
        if spec.pacing is not None:
            _check_member(name, "pacing", spec.pacing, Pacing)
        if spec.on_error is not None:
            _check_member(name, "on_error", spec.on_error, OnError)
        if spec.undo is not None:
            _check_member(name, "undo", spec.undo, Undo)
        if spec.default_depth is not None:
            _check_member(name, "default_depth", spec.default_depth, Depth)

        category = self._categories[spec.category]
        sources: dict[str, Source] = {}

        thread: Thread
        if spec.thread is not None:
            thread = spec.thread
            sources["thread"] = "op"
        else:
            thread = category.default_thread
            sources["thread"] = "category"

        pacing: Pacing
        if spec.pacing is not None:
            pacing = spec.pacing
            sources["pacing"] = "op"
        else:
            pacing = category.default_pacing
            sources["pacing"] = "category"

        on_error: OnError
        if spec.on_error is not None:
            on_error = spec.on_error
            sources["on_error"] = "op"
        else:
            on_error = GLOBAL_DEFAULT_ON_ERROR
            sources["on_error"] = "global"

        undo: Undo
        if spec.undo is not None:
            undo = spec.undo
            sources["undo"] = "op"
        else:
            undo = default_undo_for(spec.effect)
            sources["undo"] = "global"

        phase_size: int
        if spec.phase_size is not None:
            phase_size = spec.phase_size
            sources["phase_size"] = "op"
        else:
            phase_size = default_phase_size(pacing)
            sources["phase_size"] = "global"

        default_limit: int
        if spec.default_limit is not None:
            default_limit = spec.default_limit
            sources["default_limit"] = "op"
        else:
            default_limit = DEFAULT_LIMIT
            sources["default_limit"] = "global"

        max_limit: int
        if spec.max_limit is not None:
            max_limit = spec.max_limit
            sources["max_limit"] = "op"
        else:
            max_limit = MAX_LIMIT
            sources["max_limit"] = "global"

        sources["default_depth"] = "op" if spec.default_depth is not None else "global"

        if thread not in category.allowed_thread:
            raise RegistrationError(
                f"'{name}': effective thread '{thread}' not in category "
                f"'{category.name}' allowed_thread {sorted(category.allowed_thread)}"
            )
        if pacing not in category.allowed_pacing:
            raise RegistrationError(
                f"'{name}': effective pacing '{pacing}' not in category "
                f"'{category.name}' allowed_pacing {sorted(category.allowed_pacing)}"
            )

        if phase_size < 1:
            raise RegistrationError(f"'{name}': phase_size must be >= 1")
        if default_limit < 1:
            raise RegistrationError(f"'{name}': default_limit must be >= 1")
        if max_limit < 1:
            raise RegistrationError(f"'{name}': max_limit must be >= 1")
        if default_limit > max_limit:
            raise RegistrationError(
                f"'{name}': default_limit ({default_limit}) must be <= max_limit ({max_limit})"
            )

        if touches_flame(thread):
            if spec.to_python is None:
                raise RegistrationError(f"'{name}': thread='{thread}' operations require to_python")
        elif spec.to_python is not None:
            raise RegistrationError(
                f"'{name}': thread='{thread}' operations must not declare to_python "
                "(touches_flame is False for this thread)"
            )

        if touches_flame(thread):
            if spec.server_handler is not None:
                raise RegistrationError(
                    f"'{name}': thread='{thread}' operations must not declare server_handler "
                    "(it runs on the server, not in Flame)"
                )
        elif spec.server_handler is None:
            raise RegistrationError(
                f"'{name}': thread='{thread}' operations require a server_handler "
                "(there is no to_python to run in Flame)"
            )

        has_selector_param = any(param.type == "selector" for param in spec.params)
        if has_selector_param and not spec.accepts:
            raise RegistrationError(
                f"'{name}': has a selector-typed param but declares no accepts kinds"
            )

        sample_args: Mapping[str, Any]
        if spec.sample_args is not None:
            sample_args = MappingProxyType(dict(spec.sample_args))
        else:
            sample_args = MappingProxyType(
                {param.name: _sample_value(param, spec.accepts) for param in spec.params}
            )

        if spec.to_python is not None:
            try:
                generated = spec.to_python(sample_args)
            except Exception as exc:  # registration-time smoke test, any failure is fatal
                raise RegistrationError(f"'{name}': to_python(sample_args) raised {exc!r}") from exc
            _check_to_python_output(name, generated, effect=spec.effect, pacing=pacing)

            # A selector-typed param's resolved *kind* can change which branch of an
            # op's to_python runs (an op is free to branch on the shape it was handed).
            # Smoke-testing only accepts[0]'s sample (the `sample_args` built above)
            # would leave a detection gap: a loop hidden behind a *later* accepts kind
            # would never be exercised, so it would never hit the pacing guard below.
            # Test every declared kind.
            if has_selector_param:
                for kind in spec.accepts:
                    kind_args = _sample_args_for_kind(spec, kind)
                    try:
                        kind_generated = spec.to_python(kind_args)
                    except Exception as exc:
                        raise RegistrationError(
                            f"'{name}': to_python(sample_args) raised {exc!r} "
                            f"for accepts kind {kind!r}"
                        ) from exc
                    _check_to_python_output(
                        name, kind_generated, effect=spec.effect, pacing=pacing, kind=kind
                    )

        if spec.journal is not None:
            self._check_journal_spec(spec, thread, undo, sample_args)

        if spec.wrap is not None:
            if not touches_flame(thread):
                raise RegistrationError(
                    f"'{name}': only operations that run in Flame can be wrapped (wrap)"
                )
            if spec.journal is not None:
                raise RegistrationError(f"'{name}': an operation is either journaled or wrapped")

        if spec.checkpoint is not None:
            self._check_checkpoint_spec(spec, thread, undo)

        undo_steps: Callable[[Mapping[str, Any], int], int]
        if spec.effect == "read":
            undo_steps = spec.undo_steps if spec.undo_steps is not None else _always_zero_undo_steps
        else:
            if spec.undo_steps is None:
                raise RegistrationError(f"'{name}': non-read operations must declare undo_steps")
            undo_steps = spec.undo_steps

        for symbol in (*spec.requires, *spec.diagnose):
            if not _is_well_formed_symbol(symbol):
                raise RegistrationError(f"'{name}': malformed symbol reference '{symbol}'")
            if not api_symbols.has_symbol(symbol, self._symbols):
                raise RegistrationError(f"'{name}': unknown API symbol '{symbol}'")

        registered = RegisteredOperation(
            name=name,
            category=spec.category,
            description=spec.description,
            effect=spec.effect,
            params=spec.params,
            thread=thread,
            pacing=pacing,
            on_error=on_error,
            accepts=spec.accepts,
            requires=spec.requires,
            diagnose=spec.diagnose,
            undoable=spec.undoable,
            undo=undo,
            phase_size=phase_size,
            default_depth=spec.default_depth,
            default_limit=default_limit,
            max_limit=max_limit,
            to_python=spec.to_python,
            verify=spec.verify,
            undo_steps=undo_steps,
            sample_args=sample_args,
            sources=MappingProxyType(dict(sources)),
            server_handler=spec.server_handler,
            journal=spec.journal,
            precheck=spec.precheck,
            wrap=spec.wrap,
            checkpoint=spec.checkpoint,
        )
        self._operations[name] = registered
        return registered

    @staticmethod
    def _check_journal_spec(
        spec: OperationSpec, thread: Thread, undo: Undo, sample_args: Mapping[str, Any]
    ) -> None:
        name = spec.name
        journal = spec.journal
        assert journal is not None
        if spec.effect == "read":
            raise RegistrationError(f"'{name}': a read operation is never journaled")
        if not touches_flame(thread):
            raise RegistrationError(f"'{name}': only operations that run in Flame are journaled")
        if undo != "value":
            raise RegistrationError(f"'{name}': a journaled operation must have undo='value'")
        try:
            keys = journal.keys(sample_args)
            values = journal.values(sample_args)
            rebuilt = journal.write_args({"kind": "wiretap", "id": "sample"}, values)
        except Exception as exc:  # registration-time smoke test, any failure is fatal
            raise RegistrationError(f"'{name}': journal spec raised {exc!r}") from exc
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise RegistrationError(f"'{name}': journal.keys must be a list of str")
        if set(values) != set(keys) or len(set(keys)) != len(keys):
            raise RegistrationError(f"'{name}': journal.values keys must equal journal.keys")
        if not isinstance(rebuilt, dict) or "target" not in rebuilt:
            raise RegistrationError(f"'{name}': journal.write_args must return args with 'target'")

    @staticmethod
    def _check_checkpoint_spec(spec: OperationSpec, thread: Thread, undo: Undo) -> None:
        name = spec.name
        checkpoint = spec.checkpoint
        assert checkpoint is not None
        if spec.effect == "read":
            raise RegistrationError(f"'{name}': a read operation is never checkpointed")
        if not touches_flame(thread):
            raise RegistrationError(f"'{name}': only operations that run in Flame are checkpointed")
        if spec.journal is not None or spec.wrap is not None:
            raise RegistrationError(
                f"'{name}': an operation is checkpointed, journaled or wrapped -- one of them"
            )
        if undo not in ("checkpoint", "unsupported"):
            raise RegistrationError(
                f"'{name}': a checkpointed operation must have undo='checkpoint' or "
                f"'unsupported', got '{undo}'"
            )
        batch_params = [p for p in spec.params if p.name == checkpoint.batch_arg]
        if len(batch_params) != 1 or batch_params[0].type != "selector":
            raise RegistrationError(
                f"'{name}': checkpoint.batch_arg '{checkpoint.batch_arg}' must name a "
                "selector-typed parameter"
            )
        if (checkpoint.group_selector is None) != (checkpoint.rebind is None):
            raise RegistrationError(
                f"'{name}': checkpoint.group_selector and checkpoint.rebind go together"
            )
        if checkpoint.target_kind == "timeline":
            Registry._check_timeline_accepts(spec, checkpoint)
        elif checkpoint.group_selector is None:
            if "batch" not in spec.accepts:
                raise RegistrationError(f"'{name}': a checkpointed operation must accept 'batch'")
        elif not {"node", "action_node"} & set(spec.accepts):
            raise RegistrationError(
                f"'{name}': a checkpointed operation with a group_selector addresses the group "
                "through a 'node' or 'action_node' selector"
            )

    @staticmethod
    def _check_timeline_accepts(spec: OperationSpec, checkpoint: CheckpointSpec) -> None:
        name = spec.name
        if checkpoint.group_selector is None:
            if not {"wiretap", "path", "selection"} & set(spec.accepts):
                raise RegistrationError(
                    f"'{name}': a timeline-checkpointed operation must accept a 'wiretap', "
                    "'path' or 'selection' selector"
                )
        elif "segment" not in spec.accepts:
            raise RegistrationError(
                f"'{name}': a timeline-checkpointed operation with a group_selector addresses "
                "the sequence through a 'segment' selector"
            )

    def validate_references(self) -> None:
        """Runs cross-operation checks that cannot run at `register` time.

        The other operation may be registered later (a journaled write op's read op).
        """
        for op in self._operations.values():
            if op.journal is None:
                continue
            read = self._operations.get(op.journal.read_op)
            if read is None:
                raise RegistrationError(
                    f"'{op.name}': journal read_op '{op.journal.read_op}' is not registered"
                )
            if read.effect != "read":
                raise RegistrationError(f"'{op.name}': journal read_op '{read.name}' is not a read")
            param_names = {param.name for param in read.params}
            if not {"target", "keys"} <= param_names:
                raise RegistrationError(
                    f"'{op.name}': journal read_op '{read.name}' must take 'target' and 'keys'"
                )
            missing = [kind for kind in op.accepts if kind not in read.accepts]
            if missing:
                raise RegistrationError(
                    f"'{op.name}': journal read_op '{read.name}' does not accept {missing}"
                )

    def get(self, name: str) -> RegisteredOperation | None:
        return self._operations.get(name)

    def list_ops(self, category: str | None = None) -> tuple[RegisteredOperation, ...]:
        ops = tuple(self._operations.values())
        if category is None:
            return ops
        return tuple(op for op in ops if op.category == category)

    def list_categories(self) -> tuple[str, ...]:
        return tuple(sorted(self._categories))

    def categories(self) -> Mapping[str, Category]:
        return dict(self._categories)
