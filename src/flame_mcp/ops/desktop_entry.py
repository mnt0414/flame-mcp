"""`desktop` category, the entry-level ops that work on the Desktop and in a library alike:
listing children, the current selection, rename, and the attribute ops."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.list_fields import fields_note, fields_param
from flame_mcp.ops._attributes import attribute_requires, register_attribute_ops
from flame_mcp.ops._common import (
    build_source,
    limit_param,
    list_result_src,
    offset_param,
)
from flame_mcp.registry import Registry
from flame_mcp.types import (
    JournalSpec,
    ListFields,
    OperationParam,
    OperationSpec,
    SelectorKind,
)

TARGET_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")

_LIST_CHILDREN_FIELDS: Final[ListFields] = ListFields(
    "items",
    (
        "type",
        "name",
        "uid",
        "wiretapId",
        "duration",
        "frameRate",
        "width",
        "height",
        "bitDepth",
    ),
)


def _list_children_to_python(args: Mapping[str, Any]) -> str:
    limit = args["limit"]
    offset = args["offset"]
    types = list(args.get("types") or [])
    target_sel = args["target"]
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        "\n".join(
            [
                "try:",
                "    _children = list(target.children)",
                "except BaseException as __lc_exc:",
                "    raise _OpError('SELECTOR_TYPE', 'target has no children: ' + str(__lc_exc))",
                f"_types = {types!r}",
                "if _types:",
                "    _children = [_x for _x in _children if type(_x).__name__ in _types]",
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_children)",
                "_items = [_describe_entry(_x) for _x in _children[_offset : _offset + _limit]]",
                "for _item, _x in zip(_items, _children[_offset : _offset + _limit]):",
                "    if _item['type'] in _RP_MEDIA_TYPES:",
                "        _item.update(_rp_size_facts(_x))",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
    )


def _selection_to_python(args: Mapping[str, Any]) -> str:
    limit = args["limit"]
    offset = args["offset"]
    return build_source(
        "\n".join(
            [
                "_entries = list(flame.media_panel.selected_entries)",
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_entries)",
                "_items = [_describe_entry(_x) for _x in _entries[_offset : _offset + _limit]]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
    )


_LIBRARY_WRITE_NOTE = (
    "A library, and anything inside one (a folder, a reel, a clip, a sequence, a Batch group, or a "
    "reel group made in a library), is never renamed or written to: such a target is "
    "refused with PRECONDITION_UNMET before anything is read for the write, nothing is written "
    "and nothing is journaled (this holds for a plan.run step too; a library entry can be "
    "taken out with desktop.copy and changed there). The revert of a write made earlier is "
    "not refused."
)


def _rename_to_python(args: Mapping[str, Any]) -> str:
    # Measured on Flame 2025.2.4: a PyAttribute.set_value() write may not be put on Flame's
    # Undo history (on the name of a reel on the Desktop, Ctrl+Z removed the preceding,
    # unrelated operation instead). So the result carries a `revert` (the op/args that call
    # rename again) that stays valid across wiretap IDs. get_wiretap_node_id() should work for
    # an object on the Media Panel; if it cannot be read, `revert` is None (the result does
    # not pretend that Ctrl+Z or a re-call can undo it).
    name = args["name"]
    target_sel = args["target"]
    # `_expected` is set only by the server's journaled write path (journaled_write.py):
    # compare-and-swap on the name it read a moment ago.
    expected = args.get("_expected")
    cas_lines: list[str] = []
    if expected is not None:
        cas_lines = [
            f"_expected_name = {expected.get('name')!r}",
            "if _before != _expected_name:",
            "    _result = {",
            "        'type': type(target).__name__,",
            "        'before': _before,",
            "        'after': _before,",
            "        'applied': [],",
            "        'failed': {'name': 'conflict: expected ' + repr(_expected_name)",
            "                   + ' but the current value is ' + repr(_before)},",
            "        'conflicts': {'name': {'expected': _expected_name, 'current': _before}},",
            "        'revert': None,",
            "    }",
            "    raise _OpError(",
            "        'CONFLICT',",
            "        'rename: nothing written, the name changed between the read and the write '",
            "        '(expected ' + repr(_expected_name) + ', current ' + repr(_before) + ')',",
            "    )",
        ]
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        "\n".join(
            [
                "_before = _v(target.name)",
                *cas_lines,
                f"target.name.set_value({name!r})",
                "_after = _v(target.name)",
                "try:",
                "    _wiretap_id = target.get_wiretap_node_id()",
                "except BaseException:",
                "    _wiretap_id = None",
                "_revert = None",
                "if _wiretap_id is not None:",
                "    _revert = {",
                "        'op': 'desktop.rename',",
                "        'args': {",
                "            'target': {'kind': 'wiretap', 'id': _wiretap_id},",
                "            'name': _before,",
                "        },",
                "    }",
                "_result = {",
                "    'type': type(target).__name__,",
                "    'before': _before,",
                "    'after': _after,",
                "    'applied': ['name'],",
                "    'failed': {},",
                "    'readBack': {'name': _after},",
                "    'wiretapId': _wiretap_id,",
                "    'revert': _revert,",
                "}",
                "_affected = 1",
            ]
        ),
    )


def _rename_verify(args: Mapping[str, Any]) -> str:
    name = args["name"]
    return "\n".join(
        [
            "_actual = _v(target.name)",
            f"_verified = (_actual == {name!r})",
            "if not _verified:",
            f"    _verify_detail = 'expected name ' + repr({name!r}) + ' but got ' + repr(_actual)",
        ]
    )


def register_entry_ops(registry: Registry) -> None:
    target_param = OperationParam(
        name="target",
        type="selector",
        description="wiretap / path / selection selector for the container or entry.",
        required=True,
    )
    list_target_param = OperationParam(
        name="target",
        type="selector",
        description=(
            "wiretap / path / selection selector for the container or entry; {kind: 'desktop'} "
            "for the current Desktop (its reel groups and Batch groups)."
        ),
        required=True,
    )

    registry.register(
        OperationSpec(
            name="desktop.list_children",
            category="desktop",
            description=(
                "List the children of a library/folder/reel group/reel/desktop "
                "(anything with a `.children` attribute), one page at a time: "
                "`offset` skips items, `types` keeps only the named Flame types "
                "(`total` counts the items after that filter). A PyClip / PySequence item "
                "also carries `duration` and `frameRate` and `width`, `height`, `bitDepth` "
                "(each null when it cannot be read). " + fields_note()
            ),
            effect="read",
            params=(
                list_target_param,
                limit_param(),
                offset_param(),
                OperationParam(
                    name="types",
                    type="array",
                    description=(
                        "Flame type names to keep, e.g. ['PyClip', 'PySequence']. "
                        "Omit (or pass []) to list every child."
                    ),
                    required=False,
                    item_type="string",
                ),
                fields_param(_LIST_CHILDREN_FIELDS),
            ),
            accepts=(*TARGET_ACCEPTS, "desktop"),
            to_python=_list_children_to_python,
            list_fields=_LIST_CHILDREN_FIELDS,
            requires=(
                "PyLibrary.children",
                "PyReel.children",
                "PyDesktop.children",
                "PyClip.width",
                "PyClip.height",
                "PyClip.bit_depth",
                "PySequence.width",
                "PySequence.height",
                "PySequence.bit_depth",
            ),
        )
    )

    registry.register(
        OperationSpec(
            name="desktop.selection",
            category="desktop",
            description="List the current Media Panel selection.",
            effect="read",
            params=(limit_param(), offset_param()),
            to_python=_selection_to_python,
            requires=("PyMediaPanel.selected_entries",),
        )
    )

    registry.register(
        OperationSpec(
            name="desktop.rename",
            brief_paths=("applied",),
            category="desktop",
            description=(
                "Rename one Media Panel entry (library/folder/reel/clip/sequence/...). "
                "Do not revert it with Ctrl+Z: a set_value write may not be put on Flame's "
                "Undo history (measured on the name of a reel on the Desktop, where Ctrl+Z "
                "removed an earlier, unrelated operation instead). To revert, use "
                "journal.revert, or call flame_do again with this call's result.revert "
                "(op + args, already "
                "filled in) -- it is None only when no wiretap ID could be obtained. "
                "JOURNAL: the server first reads the current name, records an intent "
                "(envelope `journal` = {group, id}), then renames with a compare-and-swap; "
                "if the name changed in between, nothing is written (errorCode CONFLICT). "
                "journal.revert(group) puts the previous name back. A 'batch' selector "
                "(a Batch group, by uid or name) is accepted as well. " + _LIBRARY_WRITE_NOTE
            ),
            effect="reversible",
            params=(
                target_param,
                OperationParam(
                    name="name",
                    type="string",
                    description="New name (must not be empty).",
                    required=True,
                    non_empty=True,
                ),
            ),
            accepts=(*TARGET_ACCEPTS, "batch"),
            to_python=_rename_to_python,
            verify=_rename_verify,
            journal=JournalSpec(
                read_op="desktop.get_attributes",
                keys=lambda args: ["name"],
                values=lambda args: {"name": args["name"]},
                write_args=lambda target, values: {"target": target, "name": values["name"]},
            ),
            # The way back is the result's `revert` field.
            requires=(
                "PyAttribute.set_value",
                "PyAttribute.get_value",
                "PyReel.get_wiretap_node_id",
                "PyClip.get_wiretap_node_id",
            ),
        )
    )

    register_attribute_ops(
        registry,
        category="desktop",
        noun=(
            "Desktop or Library entry (library / folder / reel group / reel / clip / "
            "sequence / Batch group ...)"
        ),
        accepts=(*TARGET_ACCEPTS, "batch"),
        target_description=(
            "wiretap / path / selection selector for the entry, or a 'batch' selector for "
            "a Batch group (its revert addresses a Batch group that way, since a Batch "
            "group has no wiretap id). A path may start on the Desktop with root='desktop' "
            "(Reel Group, Reel, Batch group)."
        ),
        stable_src=lambda _args: "_stable = _stable_desktop(target)",
        write_guard_src=lambda _args: "_tl_check_not_library(target, 'the target')",
        note=_LIBRARY_WRITE_NOTE,
        revert_kinds=("wiretap", "batch"),
        requires=attribute_requires(
            "PyLibrary.attributes",
            "PyReel.attributes",
            "PyClip.attributes",
            "PyDesktop.children",
            "PyDesktop.reel_groups",
            "PyDesktop.batch_groups",
            "PyReel.get_wiretap_node_id",
            "PyBatch.uid",
        ),
    )
