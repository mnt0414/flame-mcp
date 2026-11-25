"""`plan.run`'s step ids and references: `"{{ steps.<id>.result.<path> }}"` in a step's `args`
stands for a value of an earlier step's envelope.

`plan.for_each` also takes `"{{ item.<path> }}"`, a value of the item the step is run for
(`allow_item`, `item=`); the path is read from the item itself, and `{{ item }}` is the item.

Two moments, as `plan.run` has them: before anything runs (`collect` reads the references of one
step, `check_plan` judges the ids and the order of the whole plan: a mistake there refuses the
plan), and just before a step is sent (`substitute_args` puts the values in; a reference that
cannot be resolved stops the plan there, with what the earlier steps did still applied).

The path is read as the job runner reads it (`flame_mcp.refs`): dotted keys, `[n]` and `[*]`,
nothing evaluated. A string that is one reference becomes the value itself, type kept; a
reference inside a longer string is embedded as text.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from flame_mcp import envelope as envelope_mod
from flame_mcp.refs import (
    RefsError,
    RefsResolveError,
    Token,
    format_path,
    split_ref,
    substitute,
    walk,
)
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import ErrorCode

STEP_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+$")
HEAD: Final[str] = "steps"
RESULT_KEY: Final[str] = "result"
REF_FORM: Final[str] = "{{ steps.<id>.result.<path> }}"
ITEM_HEAD: Final[str] = "item"
ITEM_REF_FORM: Final[str] = "{{ item.<path> }}"
_NO_ITEM: Final[Any] = object()


def default_id(index: int) -> str:
    return f"s{index}"


@dataclass(frozen=True)
class Ref:
    text: str
    """The reference as written, normalised: `{{ steps.a.result.x }}`."""
    step_id: str
    tokens: tuple[Token, ...]
    """The path below the step's envelope; it starts with the `result` key."""
    item: bool = False
    """A `{{ item.<path> }}` reference: `tokens` is the path in the item and `step_id` is empty."""


class Unresolved(RefsResolveError):
    """A reference that could not be put in: `reference` is the one that failed and `reached` how
    far its path went (`result.items[0]`), None when the failure was not about one reference."""

    def __init__(self, message: str, reference: str | None, reached: str | None) -> None:
        super().__init__(message)
        self.reference = reference
        self.reached = reached

    def result(self) -> dict[str, Any]:
        found: dict[str, Any] = {"ran": False, "error": str(self)}
        if self.reference is not None:
            found["reference"] = self.reference
        if self.reached is not None:
            found["reached"] = self.reached
        return found


class PlanStep(Protocol):
    index: int
    id: str
    refs: list[Ref]
    problems: list[str]


def parse_ref(expr: str, *, allow_item: bool = False) -> Ref:
    """The reference `expr` (the text between the braces). Raises `RefsError` when it is not
    `steps.<id>.result.<path>` (or `item.<path>` when `allow_item`)."""
    head, tokens = split_ref(expr)
    text = "{{ " + expr.strip() + " }}"
    if head == ITEM_HEAD and allow_item:
        return Ref(text, "", tuple(tokens), item=True)
    if head != HEAD:
        allowed = f"{REF_FORM} or {ITEM_REF_FORM}" if allow_item else REF_FORM
        raise RefsError(f"{text}: only {allowed} can be referred to")
    if len(tokens) < 2 or tokens[0][0] != "key" or tokens[1] != ("key", RESULT_KEY):
        raise RefsError(f"{text}: write it as {REF_FORM}")
    return Ref(text, str(tokens[0][1]), tuple(tokens[1:]))


def collect(args: Any, *, allow_item: bool = False) -> list[Ref]:
    """The references in the strings of `args`, in order. Raises `RefsError` for a malformed one
    (unpaired braces, a head other than `steps`, a bad path, no `result`)."""
    found: list[Ref] = []

    def lookup(expr: str) -> str:
        found.append(parse_ref(expr, allow_item=allow_item))
        return ""

    substitute(args, lookup)
    return found


def check_plan(steps: Sequence[PlanStep]) -> None:
    """Adds to `problems` of the steps: an id that another step has (explicit or the default
    `s<index>`), a reference to an id no step has, and one to the step itself or a later one."""
    first: dict[str, int] = {}
    for step in steps:
        if step.id in first:
            step.problems.append(
                f"duplicate step id '{step.id}' (step {first[step.id]} has it; a step without an "
                "id is named s<index>)"
            )
        else:
            first[step.id] = step.index
    for step in steps:
        for ref in step.refs:
            if ref.item:
                continue
            target = first.get(ref.step_id)
            if target is None:
                step.problems.append(f"{ref.text}: no step has the id '{ref.step_id}'")
            elif target >= step.index:
                step.problems.append(
                    f"{ref.text}: a step can refer to an earlier step only (step {target} is "
                    f"not before step {step.index})"
                )


def _reached(envelope: Any, tokens: Sequence[Token]) -> str:
    """The longest prefix of `tokens` that `envelope` has, as a path."""
    best = 0
    for end in range(1, len(tokens) + 1):
        try:
            walk(envelope, list(tokens[:end]))
        except RefsError:
            break
        best = end
    return format_path(list(tokens[:best])).lstrip(".")


def _copied(value: Any) -> Any:
    """`value` as its own: a container is copied so a step cannot change what it was read from."""
    return copy.deepcopy(value) if isinstance(value, dict | list) else value


def _read(source: Any, ref: Ref, seen: dict[str, str | None]) -> Any:
    """The value of `ref` in `source`; a failure leaves in `seen["reached"]` how far it got."""
    try:
        return walk(source, list(ref.tokens))
    except RefsResolveError:
        seen["reached"] = _reached(source, ref.tokens)
        raise


def substitute_args(
    value: Any, envelopes: Mapping[str, Mapping[str, Any]] | None, item: Any = _NO_ITEM
) -> Any:
    """`value` with every reference replaced by the value it points at in `envelopes` (step id ->
    the envelope the step answered), or in `item`. Without `envelopes` only the `{{ item.* }}`
    references are replaced and a reference to a step stays as written. Raises `Unresolved`: the
    step has no envelope, it failed, a key or an element of the path is missing, or the value
    cannot be embedded in a string."""
    seen: dict[str, str | None] = {"reference": None, "reached": None}

    def lookup(expr: str) -> Any:
        ref = parse_ref(expr, allow_item=item is not _NO_ITEM)
        seen["reference"], seen["reached"] = ref.text, None
        if ref.item:
            return _copied(_read(item, ref, seen))
        if envelopes is None:
            return ref.text
        env = envelopes.get(ref.step_id)
        if env is None:
            raise RefsResolveError(f"step '{ref.step_id}' has not answered")
        if env.get("ok") is not True:
            raise RefsResolveError(f"step '{ref.step_id}' failed, so it has no result to use")
        found = _read(env, ref, seen)
        seen["reached"] = format_path(list(ref.tokens)).lstrip(".")
        return _copied(found)

    try:
        return substitute(value, lookup)
    except RefsError as exc:
        raise Unresolved(str(exc), seen["reference"], seen["reached"]) from exc


def refused_envelope(
    op: RegisteredOperation,
    message: str,
    result: Mapping[str, Any],
    skipped_key: str,
    *,
    code: ErrorCode = "INVALID_ARGS",
    hint: str | None = None,
) -> dict[str, Any]:
    """The envelope of a step that was not sent: nothing of it written."""
    env = envelope_mod.build_envelope(
        ok=False,
        result=dict(result),
        error=message,
        error_code=code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=0,
        hint_override=hint
        or (
            "Nothing of this step was sent and the plan stopped here; the steps before it stay "
            "applied (journal.revert(group) puts them back)."
        ),
    )
    env[skipped_key] = True
    return env


def own_group_problem(
    op: RegisteredOperation,
    args: Mapping[str, Any],
    owns: Callable[[RegisteredOperation, Any], bool],
) -> str | None:
    """Why `op` called with `args` cannot be a plan step because it keeps a journal group of its
    own (`owns` says so), None when it can."""
    if not owns(op, args):
        return None
    return (
        f"{op.name} cannot be a plan step{' (destructive)' if op.category == 'eval' else ''}: "
        "it takes its own checkpoint and journal group, which a plan's single group cannot "
        "revert; call it on its own"
    )


@dataclass(frozen=True)
class Checked:
    """What the checks of a step's arguments made of the values after the replacement."""

    args: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    policy_denied: bool = False
    hint: str | None = None


class SendableStep(Protocol):
    index: int
    op_name: str | None
    op: RegisteredOperation | None
    args: dict[str, Any]


def prepare(
    step: SendableStep,
    envelopes: Mapping[str, Mapping[str, Any]],
    check: Callable[[SendableStep, Any], Checked],
    skipped_key: str,
    item: Any = _NO_ITEM,
) -> dict[str, Any] | None:
    """Puts the earlier steps' values into `step.args` (they held the references until now) and
    has `check` judge them as `flame_do` would. None when `step.args` is ready to be sent, else
    the envelope of the step that is not to be sent: INVALID_ARGS (a reference that cannot be
    resolved, values that fail the checks), or POLICY_DENIED with the hint the op's own
    `precheck` gave."""
    op = step.op
    assert op is not None
    try:
        args = substitute_args(step.args, envelopes, item)
    except Unresolved as exc:
        message = f"a reference cannot be resolved: {exc}"
        return refused_envelope(op, message, {**exc.result(), "error": message}, skipped_key)
    checked = check(step, args)
    if checked.problems:
        message = "the arguments are invalid once the references are in: " + "; ".join(
            checked.problems
        )
        result = {"ran": False, "error": message, "problems": checked.problems}
        code: ErrorCode = "POLICY_DENIED" if checked.policy_denied else "INVALID_ARGS"
        return refused_envelope(op, message, result, skipped_key, code=code, hint=checked.hint)
    step.args = checked.args
    return None
