"""`segment.read_effect_setup` and `node.read_setup`: read the value of tags in a setup file.

Both only read: the setup is saved into a work directory of the server's own
(`<journal dir>/setups/s_<12 hex>`), the tags are read, and the directory is removed before the
call ends. Nothing in the project changes, so there is no checkpoint and no journal record. The
Flame side is `setup_edit_prelude` (`_te_read_setup`, `_se_node_read`) and the text functions are
`setup_edit` (`check_tags`, `read_tags`), the same rule the edit ops read back with."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors, setup_edit
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._segment_edit import no_undo_steps, segment_target_param
from flame_mcp.ops._setup_edit import work_root
from flame_mcp.ops.node_writes import NODE_REQUIRES
from flame_mcp.ops.segment_effects import EFFECT_REQUIRES
from flame_mcp.registry import Registry
from flame_mcp.setup_work import WORK_DIR_NAME
from flame_mcp.types import OperationParam, OperationSpec, Refusal

SEGMENT_READ_OP: Final[str] = "segment.read_effect_setup"
NODE_READ_OP: Final[str] = "node.read_setup"

_READ_NOTE = (
    "What it reads: the value of a tag that occurs exactly once in the setup file as "
    "<TAG>value</TAG>, the same rule segment.edit_effect_setup and node.edit_setup read back "
    "with. The value comes back as a string, the text of the file as it is (no conversion: a "
    "number is the text Flame wrote, a bool is True / False). A tag that is absent, occurs "
    "more than once, has attributes or child elements, or is written self-closing is null in "
    "result.values, with the reason in result.problems; the call is still ok, because it only "
    f"reads. `tags` is 1 to {setup_edit.SETUP_MAX_VALUES} tag names, each matching "
    "[A-Za-z_][A-Za-z0-9_]*, none listed twice: anything else is INVALID_ARGS and nothing is "
    "sent to Flame. A setup of several files is counted over all of them together, so a tag "
    "that occurs once in each of two files occurs twice; result.fileOf names the file a value "
    "came from by its extension ('.lut_node'). A file that is not UTF-8 text is not read and is "
    "named in result.notText; if no file is text the call is PRECONDITION_UNMET. The setup is "
    f"saved to a work file under <journal dir>/{WORK_DIR_NAME}/s_<12 hex>, readable by the owner "
    "only, and that directory is removed when the call ends on the Flame side, whether the read "
    "worked, was refused or raised there (result.cleanupError says so if it could not be "
    "removed). If Flame dies, or the call ends in TIMEOUT or BRIDGE_LOST, it can stay; a "
    "folder older than 7 days is removed the next time a work folder is made. The project is "
    "not changed: no checkpoint, no journal record, nothing to undo. With "
    "FLAME_MCP_RESPONSE=compact the null members of result.values and result.fileOf are left "
    "out, so tell the tags that could not be read by result.problems. A file listed in "
    "result.notText is not counted, so tagCounts and the values are those of the files that "
    "could be read. result: "
    "values {tag: string or null}, problems {tag: reason} (only the tags that are null), "
    "tagCounts {tag: occurrences over all the files}, fileOf {tag: extension of the file or "
    "null}, savedFiles (the extensions of the files the save wrote), notText. "
)

_SEGMENT_DESCRIPTION = (
    "Read the value of tags in one Timeline FX's setup file (PyTimelineFX.save_setup into a work "
    "file); the project is not changed. "
    "Example: type 'Timewarp', tags ['TW_FrameMode']. `type` is the effect type name as "
    "segment.list_effects lists it (case does not matter); the effect must already be on the "
    "segment. Works on the same segments as segment.effect_save_setup: a clip or sequence on "
    "the Desktop, a sequence inside a library, a clip of a Desktop reel. A Gap, or a segment "
    "without an effect of that type, is PRECONDITION_UNMET. "
    + _READ_NOTE
    + "result also has type and requestedType."
)

_NODE_DESCRIPTION = (
    "Read the value of tags in a Batch node's setup file (PyNode.save_node_setup into a work "
    "file); the project is not changed. The group need not be current. Example: a Timewarp "
    "node, tags ['TW_FrameMode']. A Render node writes "
    "several files; its `.lut_node` holds "
    "LogLinTargetPixelFormat (137 for 10-bit). " + _READ_NOTE + "result also has node."
)


def tags_param(noun: str) -> OperationParam:
    return OperationParam(
        name="tags",
        type="array",
        item_type="string",
        description=(
            f"Names of 1 to {setup_edit.SETUP_MAX_VALUES} tags of the {noun}'s setup file, none "
            "listed twice; each matches [A-Za-z_][A-Za-z0-9_]*."
        ),
        required=True,
    )


def tags_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    """Refuse with INVALID_ARGS a `tags` argument that is not 1 to 20 distinct tag names; nothing
    is sent to Flame."""
    problems = setup_edit.check_tags(args.get("tags"))
    if problems:
        return Refusal(
            "INVALID_ARGS",
            "nothing was sent to Flame: " + "; ".join(problems),
            result={"ran": False, "problems": problems},
        )
    return None


def _segment_to_python(args: Mapping[str, Any]) -> str:
    tags, fx_type = list(args["tags"]), args["type"]
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        f"_result = _te_read_setup(target, target__parent, {fx_type!r}, {tags!r}, {work_root()!r})",
        raise_on_error_src(),
        "_affected = 0",
    )


def _node_to_python(args: Mapping[str, Any]) -> str:
    tags = list(args["tags"])
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        f"_result = _se_node_read(target__batch, target, {tags!r}, {work_root()!r})",
        raise_on_error_src(),
        "_affected = 0",
    )


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=SEGMENT_READ_OP,
            category="segment",
            description=_SEGMENT_DESCRIPTION,
            effect="read",
            undo="none",
            params=(
                segment_target_param(),
                OperationParam(
                    name="type",
                    type="string",
                    description="The effect type, e.g. 'Timewarp' (case does not matter).",
                    required=True,
                    non_empty=True,
                ),
                tags_param("effect"),
            ),
            accepts=("segment",),
            to_python=_segment_to_python,
            undo_steps=no_undo_steps,
            precheck=tags_precheck,
            requires=("PyTimelineFX.save_setup", *EFFECT_REQUIRES),
        )
    )
    registry.register(
        OperationSpec(
            name=NODE_READ_OP,
            category="node",
            description=_NODE_DESCRIPTION,
            effect="read",
            undo="none",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description=(
                        "A 'node' selector (batch selector + node name; add pos when several "
                        "nodes of the group share the name)."
                    ),
                    required=True,
                ),
                tags_param("node"),
            ),
            accepts=("node",),
            to_python=_node_to_python,
            undo_steps=no_undo_steps,
            precheck=tags_precheck,
            requires=("PyNode.save_node_setup", *NODE_REQUIRES),
        )
    )
