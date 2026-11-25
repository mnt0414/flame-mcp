"""What `plan.for_each` reports for its steps and its items: the `return` paths, the state of a
run, the time budget between items, the refusal that ends a call before anything ran. Shared by
the read loop (`plan_loop`) and the write loop (`plan_loop_run`)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from flame_mcp import plan_bundle, plan_refs
from flame_mcp.loop_writes import Kind
from flame_mcp.plan_step import Step, step_summary
from flame_mcp.refs import (
    RefsError,
    RefsResolveError,
    Token,
    format_path,
    parse_path,
    walk,
)
from flame_mcp.types import ErrorCode, ServerOpError

RESULT_KEY: Final[str] = "result"
NOT_RUN: Final[str] = "plan.for_each not run (nothing was executed): "


class ItemState(StrEnum):
    """The `status` of an item in the answer."""

    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_RUN = "not_run"


def _now() -> float:
    return time.monotonic()


def loop_refusal(
    problems: list[str], *, code: ErrorCode = "INVALID_ARGS", hint: str | None = None, **found: Any
) -> ServerOpError:
    return ServerOpError(
        code,
        f"{NOT_RUN}{len(problems)} problem(s): {'; '.join(problems)}",
        result={"ran": False, "problems": problems, **found},
        hint=hint,
    )


@dataclass(frozen=True)
class Return:
    """What a step's `return` asks for: the paths as written and as tokens."""

    paths: tuple[tuple[str, tuple[Token, ...]], ...]


@dataclass(frozen=True)
class LoopStep:
    """One step of an item: the checked step, what its `return` asks for, how the loop treats
    its op."""

    step: Step
    ret: Return | None
    kind: Kind


def without_return(raw: Any) -> Any:
    """A raw step without its `return` (the rest is what `check_step` reads)."""
    return {k: v for k, v in raw.items() if k != "return"} if isinstance(raw, dict) else raw


def below_result(tokens: list[Token]) -> list[Token]:
    """The path below the envelope's `result`: only the leading `result` key is dropped, so
    `result.result.x` reads the key `result` of the result. Raises `RefsError` without it."""
    if tokens and tokens[0] == ("key", RESULT_KEY):
        return tokens[1:]
    raise RefsError(
        f"the path {format_path(tokens).lstrip('.')!r} must start with 'result.' "
        "(e.g. result.items[*])"
    )


def parse_return(raw: Any) -> tuple[Return | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        return None, "'return' must be a list of paths of the step's result"
    paths: list[tuple[str, tuple[Token, ...]]] = []
    for text in raw:
        try:
            paths.append((text, tuple(below_result(parse_path(text)))))
        except RefsError as exc:
            return None, f"'return': {exc}"
    return Return(tuple(paths)), None


@dataclass
class Run:
    """What the items have done so far."""

    items: list[dict[str, Any]] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    used_bytes: int = 0
    affected: int = 0
    done: int = 0
    failed: int = 0
    stopped_at: Any = None
    last_failure: dict[str, Any] | None = None
    budget_stop: dict[str, Any] | None = None
    fail_closed: dict[str, Any] | None = None
    unknown: Any = None
    """The key of the item whose write ended without an answer."""


def run_with_budget(
    keys: Sequence[Any],
    budget_s: float,
    run_item: Callable[[Run, int], bool],
    *,
    abort: bool,
    with_groups: bool = False,
) -> Run:
    """Runs the items one after another with `run_item(run, position)` (False: the item failed).
    Stops at the first failure when `abort`, and between two items once `budget_s` is spent; the
    items that did not run are listed as `not_run`."""
    run = Run()
    started = _now()
    for position, key in enumerate(keys):
        elapsed = _now() - started
        if elapsed > budget_s:
            run.budget_stop = {
                "budgetS": budget_s,
                "elapsedS": round(elapsed, 3),
                "after": keys[position - 1] if position else None,
            }
            break
        if run_item(run, position):
            run.done += 1
            continue
        run.failed += 1
        if abort:
            run.stopped_at = key
            break
    for key in keys[len(run.items) :]:
        entry: dict[str, Any] = {"key": key, "status": ItemState.NOT_RUN, "steps": []}
        if with_groups:
            entry["groups"] = []
        run.items.append(entry)
    return run


def _picked(env: Mapping[str, Any], spec: Return) -> tuple[dict[str, Any], list[str]]:
    result = env.get("result")
    picked: dict[str, Any] = {}
    missing: list[str] = []
    for text, tokens in spec.paths:
        try:
            picked[text] = walk(result, list(tokens))
        except RefsResolveError:
            missing.append(text)
    return picked, missing


def report(
    step: Step, env: Mapping[str, Any], spec: Return | None, used_bytes: int
) -> tuple[dict[str, Any], int, bool]:
    """One step as the answer shows it: a read in full (or the paths of `return`), a write that
    succeeded as its brief."""
    if spec is None or env.get("ok") is not True:
        return step_summary(step, env, used_bytes, "mixed")
    if not spec.paths:
        short = {"index": step.index, "id": step.id, "op": step.op_name, "ok": True}
        return {**short, "errorCode": env.get("errorCode")}, 0, False
    picked, missing = _picked(env, spec)
    summary, kept, omitted = step_summary(step, {**env, "result": picked}, used_bytes, "full")
    summary.pop("journal", None)
    if missing:
        summary["returnMissing"] = missing
    return summary, kept, omitted


def send_step(
    step: Step,
    runner: Callable[[Step], dict[str, Any]],
    envelopes: Mapping[str, Mapping[str, Any]],
    check: Callable[[plan_refs.SendableStep, Any], plan_refs.Checked],
    item: tuple[Any, ...] = (),
) -> tuple[dict[str, Any], bool]:
    """Sends one step of an item: the references are put in first (`item`, when given, is the
    item whose `{{ item.* }}` still stand), and a step that cannot be sent is answered by the
    envelope that says why. Returns (envelope, whether the step was refused before it was sent)."""
    refused = (
        plan_refs.prepare(step, envelopes, check, plan_bundle.SKIPPED_KEY, *item)
        if step.has_refs
        else None
    )
    return (runner(step) if refused is None else refused), refused is not None


def record_step(
    step: Step,
    env: dict[str, Any],
    spec: Return | None,
    run: Run,
    key: Any,
    entry_steps: list[dict[str, Any]],
    envelopes: dict[str, dict[str, Any]],
    keep: frozenset[str],
) -> dict[str, Any]:
    """What an item's step answered: kept for the references that follow it, reported in the
    item, counted in the run. Returns the step's report."""
    if step.id in keep:
        envelopes[step.id] = env
    summary, kept, was_omitted = report(step, env, spec, run.used_bytes)
    run.used_bytes += kept
    if was_omitted:
        run.omitted.append({"key": key, "step": step.id})
    entry_steps.append(summary)
    run.affected += int(env.get("affected") or 0)
    if env.get("ok") is not True:
        run.last_failure = {"key": key, "step": summary, "hint": env.get("hint")}
    return summary
