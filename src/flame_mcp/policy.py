"""Capability policy: what this server is allowed to do to the project.

Adapted from mcp-aftereffects `src/policy.ts` (kumo.productions, Inc., MIT License; see
THIRD_PARTY_NOTICES.md).

Configured by FLAME_MCP_READONLY / FLAME_MCP_ALLOW_CATEGORIES /
FLAME_MCP_ALLOW_EFFECTS / FLAME_MCP_ENABLE_EVAL. The eval category (`eval.run`, any Python with
the permissions of the user running Flame) is off unless FLAME_MCP_ENABLE_EVAL=1 and
FLAME_MCP_READONLY is unset.

Denials must agree in three places: tool registration, flame_catalog's
advertised list, and flame_do's execution gate. Policy is a value object, so all
three call the same pure functions against the same Policy instance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import assert_never, cast, get_args

from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.types import Effect, Undo


class PolicyConfigError(ValueError):
    """Raised by Policy.from_env on an unusable configuration (fail closed)."""


@dataclass(frozen=True)
class Denial:
    message: str
    hint: str | None = None


def _parse_csv_set(name: str, raw: str | None) -> frozenset[str] | None:
    """Parses a comma-separated allow list: unset or blank means no restriction.

    A value that holds no names at all (",") is a misconfiguration; it fails closed with
    PolicyConfigError rather than lifting the restriction.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    values = frozenset(item.strip() for item in stripped.split(",") if item.strip())
    if not values:
        raise PolicyConfigError(f"{name} holds no names, got {raw!r}")
    return values


def _parse_bool_flag(name: str, raw: str | None) -> bool:
    """Parses a boolean env flag strictly: unset, "", "0" -> False; "1" -> True.

    Anything else (e.g. "true", "yes") is a misconfiguration, not a synonym; it fails
    closed with PolicyConfigError rather than being silently treated as off.
    """
    if raw is None or raw == "" or raw == "0":
        return False
    if raw == "1":
        return True
    raise PolicyConfigError(f"{name} must be unset, '', '0', or '1', got {raw!r}")


@dataclass(frozen=True)
class Policy:
    readonly: bool
    allowed_categories: frozenset[str] | None
    allowed_effects: frozenset[Effect] | None
    eval_enabled: bool

    @staticmethod
    def from_env(env: Mapping[str, str]) -> Policy:
        readonly = _parse_bool_flag("FLAME_MCP_READONLY", env.get("FLAME_MCP_READONLY"))

        allowed_categories = _parse_csv_set(
            "FLAME_MCP_ALLOW_CATEGORIES", env.get("FLAME_MCP_ALLOW_CATEGORIES")
        )

        allowed_effects_raw = _parse_csv_set(
            "FLAME_MCP_ALLOW_EFFECTS", env.get("FLAME_MCP_ALLOW_EFFECTS")
        )
        allowed_effects: frozenset[Effect] | None = None
        if allowed_effects_raw is not None:
            valid_effects = set(get_args(Effect))
            invalid = allowed_effects_raw - valid_effects
            if invalid:
                raise PolicyConfigError(
                    f"FLAME_MCP_ALLOW_EFFECTS contains invalid effect(s): {sorted(invalid)}"
                )
            allowed_effects = cast("frozenset[Effect]", allowed_effects_raw)

        eval_requested = _parse_bool_flag("FLAME_MCP_ENABLE_EVAL", env.get("FLAME_MCP_ENABLE_EVAL"))
        eval_enabled = eval_requested and not readonly

        return Policy(
            readonly=readonly,
            allowed_categories=allowed_categories,
            allowed_effects=allowed_effects,
            eval_enabled=eval_enabled,
        )


def blocked_in_readonly(effect: Effect) -> bool:
    match effect:
        case "read":
            return False
        case "reversible" | "create" | "destructive":
            return True
        case _ as unreachable:
            assert_never(unreachable)


def effect_label(effect: Effect) -> str:
    match effect:
        case "read":
            return (
                "Changes nothing itself. eval.run and plan.run carry this effect but can run "
                "writes: eval.run can run any Python (it is off unless FLAME_MCP_ENABLE_EVAL=1), "
                "plan.run through its steps."
            )
        case "reversible":
            return "Changes something that already exists."
        case "create":
            return "Creates something new: an object in the project, a file or a render's output."
        case "destructive":
            return "Deletes or replaces something that exists."
        case _ as unreachable:
            assert_never(unreachable)


def undo_label(undo: Undo) -> str:
    match undo:
        case "none":
            return "No undo: journal.revert has nothing in the project to put back."
        case "value":
            return (
                "journal.revert puts the previous value back, deletes what the call made, "
                "or puts the thing back where it was."
            )
        case "checkpoint":
            return "journal.revert restores the checkpoint taken before this call."
        case "unsupported":
            return "This operation cannot be undone: journal.revert does not reverse it."
        case _ as unreachable:
            assert_never(unreachable)


def _eval_disabled_denial(subject: str) -> Denial:
    return Denial(
        message=f"{subject} is disabled on this server",
        hint=(
            "eval.run runs any Python with the permissions of the user running Flame. Set "
            "FLAME_MCP_ENABLE_EVAL=1 to enable it, or use a dedicated operation from "
            "flame_catalog."
        ),
    )


def _readonly_eval_denial(op_name: str) -> Denial:
    return Denial(
        message=f"{op_name} cannot be used while FLAME_MCP_READONLY=1 is set",
        hint=(
            "eval.run runs any Python, so declaring effect='read' does not make it read-only. "
            "Use a dedicated read operation from flame_catalog, or unset FLAME_MCP_READONLY."
        ),
    )


def deny_eval_effect(effect: str, policy: Policy) -> Denial | None:
    """Gates one `eval.run` call by the effect the caller declares.

    `eval.run` is hidden and refused as a whole unless FLAME_MCP_ENABLE_EVAL=1 and
    FLAME_MCP_READONLY is unset: its code is any Python, and the scan before sending looks at
    names only, so a declared 'read' cannot be trusted to leave the project, the files or the
    network alone. `deny_operation` does that for the op; this function repeats it for the
    precheck and for the run itself, which see the declared effect, and adds
    FLAME_MCP_ALLOW_EFFECTS for that effect.
    """
    if policy.readonly:
        return _readonly_eval_denial("eval.run")
    if not policy.eval_enabled:
        return _eval_disabled_denial(f"eval.run with effect '{effect}'")
    if policy.allowed_effects is not None and effect not in policy.allowed_effects:
        return Denial(
            message=f"operation effect '{effect}' is not in FLAME_MCP_ALLOW_EFFECTS",
            hint=f"Allowed effects: {', '.join(sorted(policy.allowed_effects))}",
        )
    return None


def deny_category(category: str, policy: Policy) -> Denial | None:
    """Returns a Denial when `category` is withheld by FLAME_MCP_ALLOW_CATEGORIES.

    Used both by deny_operation (per-op gate) and by flame_catalog's category
    filter, so the wording is identical on both paths.
    """
    if policy.allowed_categories is not None and category not in policy.allowed_categories:
        return Denial(
            message=f"operation category '{category}' is not in FLAME_MCP_ALLOW_CATEGORIES",
            hint=f"Allowed categories: {', '.join(sorted(policy.allowed_categories))}",
        )
    return None


def deny_operation(op: RegisteredOperation, policy: Policy) -> Denial | None:
    if policy.readonly and blocked_in_readonly(op.effect):
        return Denial(
            message=f"operation '{op.name}' modifies the project and FLAME_MCP_READONLY=1 is set",
            hint="Unset FLAME_MCP_READONLY to allow mutations, or use a read-only operation.",
        )
    if policy.readonly and op.category == "eval":
        return _readonly_eval_denial(op.name)
    category_denial = deny_category(op.category, policy)
    if category_denial is not None:
        return category_denial
    if policy.allowed_effects is not None and op.effect not in policy.allowed_effects:
        return Denial(
            message=f"operation effect '{op.effect}' is not in FLAME_MCP_ALLOW_EFFECTS",
            hint=f"Allowed effects: {', '.join(sorted(policy.allowed_effects))}",
        )
    if op.category == "eval" and not policy.eval_enabled:
        return _eval_disabled_denial(f"operation '{op.name}'")
    return None


def deny_tool(name: str, blocked_in_readonly_flag: bool, policy: Policy) -> Denial | None:
    if policy.readonly and blocked_in_readonly_flag:
        return Denial(
            message=f"tool '{name}' modifies the project and FLAME_MCP_READONLY=1 is set",
            hint="Unset FLAME_MCP_READONLY to allow mutations.",
        )
    return None


def visible_ops(registry: Registry, policy: Policy) -> tuple[RegisteredOperation, ...]:
    return tuple(op for op in registry.list_ops() if deny_operation(op, policy) is None)


def summary(policy: Policy) -> str:
    parts = [
        "readonly=ON (no project mutations)" if policy.readonly else "readonly=off",
        (
            f"categories={'|'.join(sorted(policy.allowed_categories))}"
            if policy.allowed_categories
            else "categories=all"
        ),
        (
            f"effects={'|'.join(sorted(policy.allowed_effects))}"
            if policy.allowed_effects
            else "effects=all"
        ),
        (
            "eval=disabled (readonly)"
            if policy.readonly
            else ("eval=ENABLED" if policy.eval_enabled else "eval=disabled")
        ),
    ]
    return ", ".join(parts)
