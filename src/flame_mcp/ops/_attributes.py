"""The generic attribute ops: one generator, registered once per category.

`<category>.get_attributes` / `<category>.set_attributes` read and write a target's
`PyAttribute`s. The generated code only calls the prelude functions in
`attribute_prelude.py` (`_get_attributes`, `_set_attributes`, `_set_result`, ...), so
it stays loop-free and every category gets identical behaviour; a category module
only says what it targets and how its `revert` addresses the target after the write
(`stable_src`: source lines that assign `_stable`, a selector dict or None).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.ops._common import build_source
from flame_mcp.registry import RegistrationError, Registry
from flame_mcp.types import JournalSpec, OperationParam, OperationSpec, SelectorKind

StableSrc = Callable[[Mapping[str, Any]], str]

_ATTRIBUTE_SYMBOLS = ("PyAttribute.get_value", "PyAttribute.set_value", "PyAttribute.values")


def _get_to_python(stable_src: StableSrc) -> Callable[[Mapping[str, Any]], str]:
    def to_python(args: Mapping[str, Any]) -> str:
        keys = args.get("keys")
        include_values = bool(args.get("include_values", False))
        return build_source(
            selectors.resolve_code(args["target"], "target"),
            "\n".join(
                [
                    f"_result = _get_attributes(target, {keys!r}, {include_values!r})",
                    stable_src(args),
                    "_result['stable'] = _stable",
                ]
            ),
        )

    return to_python


def _set_to_python(
    op_name: str, stable_src: StableSrc, group_var: str | None
) -> Callable[[Mapping[str, Any]], str]:
    group_arg = group_var if group_var is not None else "None"

    def to_python(args: Mapping[str, Any]) -> str:
        # `_expected` is not a declared parameter: only the server's journaled write path
        # (journaled_write.py) puts it there -- validation rejects it from a caller.
        expected = args.get("_expected")
        restoring = bool(args.get("_restoring", False))
        return build_source(
            selectors.resolve_code(args["target"], "target"),
            "\n".join(
                [
                    f"_out = _set_attributes(target, {dict(args['values'])!r}, "
                    f"{group_arg}, {expected!r}, {restoring!r})",
                    stable_src(args),
                    f"_result = _set_result({op_name!r}, target, _out, _stable)",
                    "_affected = len(_out['applied'])",
                    "_partial = _set_partial(_out)",
                    "_set_raise_if_failed(_out)",
                ]
            ),
        )

    return to_python


def register_attribute_ops(
    registry: Registry,
    *,
    category: str,
    noun: str,
    accepts: tuple[SelectorKind, ...],
    target_description: str,
    stable_src: StableSrc,
    revert_kinds: tuple[SelectorKind, ...],
    requires: tuple[str, ...],
    group_var: str | None = None,
    note: str = "",
) -> None:
    """Register `<category>.get_attributes` and `<category>.set_attributes`.
    `noun` is what the target is called in descriptions (e.g. "Batch group").
    `revert_kinds` are the selector kinds `stable_src` can produce for the
    `revert`; every one must be in `accepts`, or the revert the op hands back
    would be refused by the op itself (INVALID_ARGS) -- checked here.
    `group_var` names the resolved Batch group the target belongs to (nodes only):
    `name` is then checked against that group's other nodes before any write.
    `note` is appended to the set op's description."""
    not_accepted = [kind for kind in revert_kinds if kind not in accepts]
    if not_accepted:
        raise RegistrationError(
            f"'{category}.set_attributes': revert selector kind(s) {not_accepted} "
            f"are not in accepts {list(accepts)}"
        )
    target_param = OperationParam(
        name="target", type="selector", description=target_description, required=True
    )
    get_name = f"{category}.get_attributes"
    set_name = f"{category}.set_attributes"

    registry.register(
        OperationSpec(
            name=get_name,
            category=category,
            description=(
                f"Read the attributes of a {noun}: `attributes` maps each name to "
                "{'value': ...} (or {'error': ...} when that one cannot be read -- the call "
                "still succeeds). Omit `keys` (or pass []) for every attribute the object "
                "lists. `include_values` adds each attribute's `values`: the candidate list "
                "for an enumerated attribute, [min, max] for a numeric one, null for free "
                "text -- what set_attributes checks a write against. `stable` is a selector "
                "that keeps addressing the target (a wiretap id / batch uid / node name, with pos "
                "when several nodes of the group share the name). A value "
                "that is a Flame object (a PyTrack, a list of PySegment, ...) comes back as "
                "{type, name} (name only when it can be read), never as a repr."
            ),
            effect="read",
            params=(
                target_param,
                OperationParam(
                    name="keys",
                    type="array",
                    description="Attribute names to read. Omit (or []) for all of them.",
                    required=False,
                    item_type="string",
                ),
                OperationParam(
                    name="include_values",
                    type="boolean",
                    description="Also return each attribute's `values` (allowed values / range).",
                    required=False,
                    default=False,
                ),
            ),
            accepts=accepts,
            to_python=_get_to_python(stable_src),
            requires=requires,
        )
    )

    registry.register(
        OperationSpec(
            name=set_name,
            category=category,
            description=(
                f"Write attributes of a {noun}: `values` maps attribute name -> new value. "
                "Every key is checked before anything is written (it must be an attribute "
                "of the target; an enumerated attribute needs one of its `values`, a "
                "numeric one a number inside its range, and an integer attribute takes an "
                "integer -- 123.0 is refused; PyTime attributes are not "
                "supported yet); if any key is rejected nothing is written. Keys are then "
                "written in order, each re-read through a freshly fetched attribute: a "
                "write that raises nothing but does not stick (e.g. PySegment.dynamic_*) "
                "counts as failed (errorCode SILENT_NOOP) and stops the call, leaving "
                "later keys unwritten. The response has `partial` = {applied, failed} "
                "whenever a key failed, and the result always carries before / after / "
                "applied / failed. Do not revert with Ctrl+Z: a set_value write may not be put "
                "on Flame's Undo history (measured on the name of a reel on the Desktop and "
                "on a Batch node attribute, where Ctrl+Z removed an earlier, unrelated "
                "operation instead; flame_context: undoContract). To revert, use "
                "journal.revert, or call "
                "flame_do with result.revert (op + args, already filled in; it addresses "
                "the target by its post-write name / id); revert is null when nothing "
                "changed or no stable address could be obtained. JOURNAL: the server first reads "
                "the current values, records an intent (envelope `journal` = {group, id}), then "
                "writes with a compare-and-swap; if a value changed in between, nothing is "
                "written (errorCode CONFLICT, `conflicts` = {key: {expected, current}}). "
                "journal.revert(group) puts the previous values back. For colour_label and "
                "schematic_colour_label Flame ignores letter case and reports the table's "
                "spelling in `after` ('green' becomes 'Green'); spaces count ('lightgrey' is "
                "refused); '' clears the label. The labels are the names in Flame's colour "
                "coding table (colour_coding.cfg in Flame's cfg directory, section MediaPanel "
                "for a clip or a segment): read it instead of guessing, match the user's word "
                "to a label yourself (translate it; ignore case, spaces and spelling "
                "variants), and ask the user to confirm the label before writing it. A colour "
                "not in the table is a custom colour: tell the user, and once they confirm "
                "write its RGB to `colour` (three numbers from 0 to 1)."
                + (" " + note if note else "")
            ),
            effect="reversible",
            params=(
                target_param,
                OperationParam(
                    name="values",
                    type="object",
                    description="{attribute name: new value}. A tuple attribute takes a list.",
                    required=True,
                ),
            ),
            accepts=accepts,
            to_python=_set_to_python(set_name, stable_src, group_var),
            undo_steps=lambda args, affected: 0,
            requires=requires,
            journal=JournalSpec(
                read_op=get_name,
                keys=lambda args: list(args["values"]),
                values=lambda args: dict(args["values"]),
                write_args=lambda target, values: {"target": target, "values": values},
            ),
        )
    )


def attribute_requires(*symbols: str) -> tuple[str, ...]:
    return (*symbols, *_ATTRIBUTE_SYMBOLS)
