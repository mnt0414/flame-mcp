"""The confirmation of a `plan.for_each` write loop: the preview (`dry_run`) and the digest that
`approve` must repeat.

The digest is a SHA-256 over the call's arguments (all but `dry_run` and `approve`, so `only` is
in it) and the items that remain after `where`. A call with `approve` makes the server read the
list and expand the steps again; if the digest differs nothing runs and the new preview comes
back.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from flame_mcp import loop_writes
from flame_mcp.journal import LOOP_GROUP_KIND
from flame_mcp.plan_loop_report import NOT_RUN, LoopStep
from flame_mcp.types import ServerOpError

_LEFT_OUT: Final[frozenset[str]] = frozenset({"dry_run", "approve"})
MODE_ATTENDED: Final[str] = "attended"
MODE_UNATTENDED: Final[str] = "unattended"
APPROVE_NOTE: Final[str] = (
    "Show this list (result.keys, result.review) to the user and wait for the answer; then call "
    "again with the same arguments and approve=<result.digest>."
)


def digest_of(args: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> str:
    body = {"args": {k: v for k, v in args.items() if k not in _LEFT_OUT}, "items": list(items)}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def runs_of(records: Sequence[Mapping[str, Any]], base: str) -> int:
    """How many loops were started with the approval of `base`: the parent group records in the
    journal that name it. Kept on disk, so a restart of the server does not give it back."""
    return sum(
        1
        for r in records
        if r.get("type") == "group"
        and r.get("kind") == LOOP_GROUP_KIND
        and r.get("approval") == base
    )


def salted(base: str, runs: int) -> str:
    """The digest the server accepts now: the one of the call as it stands, changed after every
    run that was started with it, so an approval is used once."""
    if runs == 0:
        return base
    return hashlib.sha256(f"{base}:{runs}".encode()).hexdigest()


def mode_name(unattended: bool) -> str:
    return MODE_UNATTENDED if unattended else MODE_ATTENDED


def needs_approval(steps: Sequence[LoopStep], unattended: bool) -> bool:
    """Whether the loop must run in two calls: always in the attended mode, and in the unattended
    one when a step rewrites what is there or makes output that cannot be put back."""
    return not unattended or any(loop_writes.needs_review(s.kind) for s in steps)


def _step_row(item: LoopStep) -> dict[str, Any]:
    step = item.step
    return {"id": step.id, "op": step.op_name, "class": item.kind.value, "args": step.args}


def preview(
    *,
    label: Any,
    keys: Sequence[Any],
    steps: Sequence[str],
    expanded: Sequence[tuple[Any, Sequence[LoopStep]]],
    digest: str,
    unattended: bool,
    approval: bool,
) -> dict[str, Any]:
    """`expanded` is (key, steps) per item, in the order they would run."""
    review = [
        {
            "key": key,
            "step": item.step.id,
            "op": item.step.op_name,
            "class": item.kind.value,
            "args": item.step.args,
        }
        for key, item_steps in expanded
        for item in item_steps
        if loop_writes.needs_review(item.kind)
    ]
    first: dict[str, Any] | None = None
    if expanded:
        key, first_steps = expanded[0]
        first = {"key": key, "steps": [_step_row(item) for item in first_steps]}
    return {
        "dryRun": True,
        "ran": False,
        "label": label,
        "mode": mode_name(unattended),
        "needsApproval": approval,
        "count": len(keys),
        "keys": list(keys),
        "steps": list(steps),
        "firstItem": first,
        "review": review,
        "digest": digest,
        "next": APPROVE_NOTE if approval else "Call again without dry_run to run it.",
    }


def refuse_without_approval(
    args: Mapping[str, Any], base: str, runs: int, shown: dict[str, Any]
) -> None:
    """Raises when `approve` is missing or is not the digest of the list read now."""
    given = args.get("approve")
    if given == shown["digest"]:
        return
    if given is None:
        reason = "approve is missing"
    elif runs > 0 and given == salted(base, runs - 1):
        reason = "approve was already used: this loop ran once with it, ask the user again"
    else:
        reason = "approve is the digest of another list or other arguments (the list changed)"
    raise ServerOpError(
        "PRECONDITION_UNMET",
        f"{NOT_RUN}{reason}; this is the list as it is now "
        "(result.keys, result.review, result.digest)",
        result={**shown, "dryRun": False, "reason": reason},
        hint=APPROVE_NOTE,
    )
