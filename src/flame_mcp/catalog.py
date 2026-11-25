"""How `flame_catalog` presents operations: the notes and tool descriptions, one catalog entry in
its three forms (listing, args, full), and the answers to an unknown operation name. The
assembly of a whole `flame_catalog` call is `server._flame_catalog_sync`."""

from __future__ import annotations

from typing import Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import opschema, selectors
from flame_mcp.compact import ResponseMode
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.transport import elapsed_ms
from flame_mcp.types import NO_DEFAULT, OperationParam

CATALOG_LISTING_NOTE: Final[str] = (
    "Details of an operation (full description, argument types, selector fields and example): "
    "flame_catalog(op=<name>), or op=[<name>, ...] for up to 10; detail='args' for the "
    "argument shapes only. Selector syntax: flame_context's selectorVocabulary. "
    "In params, a trailing * marks a required argument."
)
CATALOG_LISTING_NOTE_COMPACT: Final[str] = (
    "Details of an operation (argument shapes, selector fields and example): "
    "flame_catalog(op=<name>), or op=[<name>, ...] for up to 10. The default is detail='args', "
    "the argument shapes only; read detail='full' (the whole description) when an operation does "
    "not behave as you expected, after SILENT_NOOP or PRECONDITION_UNMET, and the first time "
    "you use one that cannot be undone. Selector syntax: flame_context's selectorVocabulary. "
    "In params, a trailing * marks a required argument."
)
CATALOG_DETAIL_NOTE: Final[str] = (
    "Descriptions of the selector fields are in flame_context's selectorVocabulary."
)
CATALOG_ARGS_FORM_NOTE: Final[str] = (
    "This is the short form (detail='args'): the preconditions, how to read the result and how "
    "to put it back are in the whole description, detail='full'."
)
CATALOG_DETAILS: Final[tuple[str, ...]] = ("full", "args")
CATALOG_TOOL_DESCRIPTION: Final[str] = (
    "List the operations visible under the current policy, compactly: name, category, "
    "effect, undo, one-sentence summary, argument names (* = required) and accepted "
    "selector kinds. Pass `category` to list one category, or `op` for the detail of "
    "one operation (or a list of up to 10): full description, argument types, "
    "thread / pacing, selector field names and example. Use detail='full' (default) "
    "the first time you use an operation, and detail='args' to only check the "
    "argument shapes. Selector field descriptions: flame_context's "
    "selectorVocabulary."
)
CATALOG_TOOL_DESCRIPTION_COMPACT: Final[str] = (
    "List the operations visible under the current policy, compactly: name, category, "
    "effect, undo, one-sentence summary, argument names (* = required) and accepted "
    "selector kinds. Pass `category` to list one category, or `op` for the detail of "
    "one operation (or a list of up to 10). The default is detail='args': argument types "
    "and one-sentence summaries, selector field names and example. Read detail='full' (the "
    "whole description, with preconditions, how to read the result and how to put it back) "
    "when an operation does not behave as you expected, after SILENT_NOOP or "
    "PRECONDITION_UNMET, and the first time you use one that cannot be undone. Selector field "
    "descriptions: flame_context's selectorVocabulary."
)
CATALOG_DETAIL_PARAM_DESCRIPTION: Final[str] = (
    "With `op`: 'full' (default) is the whole description; 'args' is "
    "the argument shapes only (one-sentence summaries)."
)
CATALOG_DETAIL_PARAM_DESCRIPTION_COMPACT: Final[str] = (
    "With `op`: 'args' (default) is the argument shapes only (one-sentence summaries); 'full' "
    "is the whole description."
)
MAX_CATALOG_OPS: Final[int] = 10
CATALOG_OMITTED_NOTE: Final[str] = (
    "Entries past the size budget were left out; ask for them one at a time or with detail='args'."
)


def catalog_listing_note(mode: ResponseMode) -> str:
    return CATALOG_LISTING_NOTE_COMPACT if mode == "compact" else CATALOG_LISTING_NOTE


def catalog_detail_note(detail: str) -> str:
    if detail == "args":
        return f"{CATALOG_DETAIL_NOTE} {CATALOG_ARGS_FORM_NOTE}"
    return CATALOG_DETAIL_NOTE


def catalog_entry(
    op: RegisteredOperation,
    *,
    detailed: bool = False,
    detail: str = "full",
) -> dict[str, Any]:
    """Returns one operation as flame_catalog lists it.

    A LISTING (all operations, or a category) is
    compact -- name, category, effect, undo, a one-sentence summary, argument names (`*` =
    required) and the accepted selector kind names -- so the whole catalog stays far below the
    response budget as operations are added. `detailed` (flame_catalog(op=...)) is the full
    description: argument types, thread / pacing, and each accepted selector kind's field names,
    types and example (the field descriptions are in flame_context's `selectorVocabulary`).
    `detail="args"` with `detailed` is the argument shapes only: name, effect, undo, a
    one-sentence summary, each argument's type, required flag and one-sentence summary (plus
    default, enum, minimum and itemType when it has one), and the accepted selector kinds.
    """
    entry: dict[str, Any]
    if not detailed:
        entry = {
            "name": op.name,
            "category": op.category,
            "effect": op.effect,
            "undo": op.undo,
            "summary": opschema.first_sentence(op.description),
            "params": opschema.compact_params(op.params),
            "accepts": list(op.accepts),
        }
    elif detail == "args":
        entry = {
            "name": op.name,
            "effect": op.effect,
            "undo": op.undo,
            "summary": opschema.first_sentence(op.description),
            "params": [_args_param(param) for param in op.params],
            "accepts": [selectors.describe_kind_brief(kind) for kind in op.accepts],
        }
    else:
        entry = {
            "name": op.name,
            "category": op.category,
            "description": op.description,
            "params": {
                "summary": opschema.summarize_params(op.params),
                "details": [
                    {
                        "name": param.name,
                        "type": param.type,
                        "description": param.description,
                        "required": param.required,
                        "nullable": param.nullable,
                        "default": None if param.default is NO_DEFAULT else param.default,
                        "integer": param.integer,
                        "minimum": param.minimum,
                        "itemType": param.item_type,
                        "enum": None if param.enum is None else list(param.enum),
                    }
                    for param in op.params
                ],
            },
            "effect": op.effect,
            "undo": op.undo,
            "thread": op.thread,
            "pacing": op.pacing,
            "accepts": [selectors.describe_kind_brief(kind) for kind in op.accepts],
            "defaultLimit": op.default_limit,
            "maxLimit": op.max_limit,
        }
    return entry


def _args_param(param: OperationParam) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": param.name,
        "type": param.type,
        "required": param.required,
        "summary": opschema.first_sentence(param.description, limit=None),
    }
    if param.default is not NO_DEFAULT:
        item["default"] = param.default
    if param.enum is not None:
        item["enum"] = list(param.enum)
    if param.minimum is not None:
        item["minimum"] = param.minimum
    if param.item_type is not None:
        item["itemType"] = param.item_type
    return item


def unknown_operation_message(registry: Registry, op_name: str) -> tuple[str, str | None]:
    names = [op.name for op in registry.list_ops()]
    suggestion = opschema.suggest_name(op_name, names)
    message = f"no operation named '{op_name}' is registered"
    if suggestion is not None:
        message += f" (did you mean '{suggestion}'?)"
    return message, suggestion


def suggest_unknown_operation(registry: Registry, op_name: str) -> dict[str, Any]:
    message, _ = unknown_operation_message(registry, op_name)
    return envelope_mod.from_pre_bridge_error(
        error=message,
        error_code="UNKNOWN_OPERATION",
        op=None,
        duration_ms=0,
    )


def catalog_invalid_args(message: str, started: float) -> dict[str, Any]:
    return envelope_mod.from_pre_bridge_error(
        error=message,
        error_code="INVALID_ARGS",
        op=None,
        duration_ms=elapsed_ms(started),
    )
