"""How much one `plan.for_each` call may do: items, expanded steps and seconds.

Configured by FLAME_MCP_LOOP_MAX_ITEMS / FLAME_MCP_LOOP_MAX_STEPS / FLAME_MCP_LOOP_BUDGET_S, each
unset (the default) or a positive integer; anything else fails closed at startup.

FLAME_MCP_LOOP_UNATTENDED opens the mode in which a write loop of ops that can be put back runs
without the dry_run / approve round. It is `1` (open) or `0` / unset (closed, the default);
anything else fails closed at startup. Nothing in a call can open it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from flame_mcp.policy import parse_flag

MAX_ITEMS_ENV: Final[str] = "FLAME_MCP_LOOP_MAX_ITEMS"
MAX_STEPS_ENV: Final[str] = "FLAME_MCP_LOOP_MAX_STEPS"
BUDGET_ENV: Final[str] = "FLAME_MCP_LOOP_BUDGET_S"
UNATTENDED_ENV: Final[str] = "FLAME_MCP_LOOP_UNATTENDED"
MAX_DIGITS: Final[int] = 9
_SHOWN_CHARS: Final[int] = 40


class LoopLimitsConfigError(ValueError):
    """Raised by loop_limits_from_env on an unusable value (fail closed)."""


@dataclass(frozen=True)
class LoopLimits:
    max_items: int = 50
    max_steps: int = 200
    budget_s: int = 120
    unattended: bool = False


DEFAULT_LOOP_LIMITS: Final[LoopLimits] = LoopLimits()


def _shown(raw: str) -> str:
    return raw if len(raw) <= _SHOWN_CHARS else raw[:_SHOWN_CHARS] + "..."


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    if raw.isascii() and raw.isdigit() and len(raw) <= MAX_DIGITS and int(raw) > 0:
        return int(raw)
    raise LoopLimitsConfigError(
        f"{name} must be unset or a positive integer of at most {MAX_DIGITS} digits, "
        f"got {_shown(raw)!r}"
    )


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name)
    flag = parse_flag(raw, empty_is_off=False)
    if flag is None:
        raise LoopLimitsConfigError(f"{name} must be unset, 0 or 1, got {_shown(str(raw))!r}")
    return flag


def loop_limits_from_env(env: Mapping[str, str]) -> LoopLimits:
    return LoopLimits(
        max_items=_positive_int(env, MAX_ITEMS_ENV, DEFAULT_LOOP_LIMITS.max_items),
        max_steps=_positive_int(env, MAX_STEPS_ENV, DEFAULT_LOOP_LIMITS.max_steps),
        budget_s=_positive_int(env, BUDGET_ENV, DEFAULT_LOOP_LIMITS.budget_s),
        unattended=_flag(env, UNATTENDED_ENV),
    )
