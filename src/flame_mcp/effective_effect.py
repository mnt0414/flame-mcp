"""The effective effect of one call: an op keeps the effect it declares, but a call that
overwrites or replaces something counts as `destructive` for FLAME_MCP_ALLOW_EFFECTS.

The gate is an op's `precheck`, the same hook `eval.run` uses for its declared-effect gate, so
`Runtime.call` and `plan.run`'s up-front check of every step apply it without a second code
path. FLAME_MCP_READONLY needs no handling here: `deny_operation` already refuses every
non-read op before the precheck runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp.policy import Policy
from flame_mcp.types import Precheck, Refusal


def deny_destructive_call(op_name: str, trigger: str, policy: Policy) -> Refusal | None:
    """A POLICY_DENIED refusal when `policy` withholds `destructive` and the call carries
    `trigger` (e.g. "overwrite=true"), else None."""
    allowed = policy.allowed_effects
    if allowed is None or "destructive" in allowed:
        return None
    return Refusal(
        "POLICY_DENIED",
        f"{op_name} with {trigger} counts as a destructive call and 'destructive' is not in "
        "FLAME_MCP_ALLOW_EFFECTS",
        hint=(
            f"Allowed effects: {', '.join(sorted(allowed))}. Add 'destructive' to "
            f"FLAME_MCP_ALLOW_EFFECTS, or call {op_name} without {trigger}."
        ),
    )


def overwrite_precheck(op_name: str, flag: str = "overwrite") -> Precheck:
    """A `precheck` for an op whose only extra rule is that `flag=true` makes the call
    destructive."""

    def precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
        if args.get(flag) is True:
            return deny_destructive_call(op_name, f"{flag}=true", policy)
        return None

    return precheck
