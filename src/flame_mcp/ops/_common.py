"""Shared code-generation pieces for the op modules.

Every op's `to_python` returns one self-contained script: this
module holds the bits more than one op needs -- an assembler and the
list-result shape -- so category modules only write the logic specific to
their own op. `_describe_entry` is defined in execution.py's `DESCRIBE_ENTRY_HELPER`
prelude (alongside the `_resolve_*` selector functions); ops call it directly.
"""

from __future__ import annotations

from typing import Final

from flame_mcp.types import OperationParam

AUTO_RESIZE_NOTE: Final[str] = (
    "A Resize among the effects may be one added by Flame itself because the sequence's bit "
    "depth differs from the media's, and segment.delete_effect refuses to delete a Resize in "
    "that situation."
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


def raise_on_error_src() -> str:
    """The op's code after a helper that answers `{error, errorCode}`: turn it into `_OpError` so
    the helper's partial `_result` stays in the envelope (desktop writes)."""
    return "\n".join(
        [
            "if _result['error'] is not None:",
            "    raise _OpError(_result['errorCode'], _result['error'])",
        ]
    )
