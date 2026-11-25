"""Shared code-generation pieces for the op modules.

Every op's `to_python` returns one self-contained script: this
module holds the bits more than one op needs -- an assembler and the
list-result shape -- so category modules only write the logic specific to
their own op. `_describe_entry` is defined in execution.py's `DESCRIBE_ENTRY_HELPER`
prelude (alongside the `_resolve_*` selector functions); ops call it directly.
"""

from __future__ import annotations

from typing import Final

from flame_mcp.execution import raise_on_error_src as raise_on_error_src
from flame_mcp.types import OperationParam

AUTO_RESIZE_NOTE: Final[str] = (
    "A Resize among the effects may be one added by Flame itself because the sequence's bit "
    "depth differs from the media's, and segment.delete_effect refuses to delete a Resize in "
    "that situation."
)

REEL_CLIP_NOTE: Final[str] = (
    "The segment can also be the one inside a reel clip on the Desktop: parent = the clip's "
    "wiretap, version 0, track 0, index 0 (a clip with one segment), with no sequence involved. "
    "Measured on Flame 2025.2.4."
)


def raise_if_error(code: str) -> str:
    return "\n".join(
        [
            "if _result['error'] is not None:",
            f"    raise _OpError(_result.get('errorCode') or {code!r}, _result['error'])",
        ]
    )


def batch_target_param() -> OperationParam:
    return OperationParam(
        name="target",
        type="selector",
        description="A 'batch' selector (uid or name). The group need not be current.",
        required=True,
    )


def build_source(*blocks: str) -> str:
    """Join generated-code blocks with blank lines between them.

    Leading and trailing blank lines of each block are stripped first.
    """
    return "\n\n".join(block.strip("\n") for block in blocks if block)


def list_result_src(items_var: str, total_var: str, offset_var: str) -> str:
    """`{"total", "offset", "shown", "truncated", "items"}`,
    shared by every listing op. `total` is the count before paging (after any
    type filter); `truncated` means more items follow this page."""
    return "\n".join(
        [
            "_result = {",
            f"    'total': {total_var},",
            f"    'offset': {offset_var},",
            f"    'shown': len({items_var}),",
            f"    'truncated': {offset_var} + len({items_var}) < {total_var},",
            f"    'items': {items_var},",
            "}",
        ]
    )


def limit_param(description: str = "Max items to return.") -> OperationParam:
    return OperationParam(name="limit", type="number", description=description, required=False)


def offset_param() -> OperationParam:
    return OperationParam(
        name="offset",
        type="number",
        description="Number of items to skip before the page starts (0-based, default 0).",
        required=False,
        default=0,
        integer=True,
        minimum=0,
    )
