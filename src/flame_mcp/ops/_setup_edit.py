"""Shared pieces of `segment.edit_effect_setup` and `node.edit_setup`: the two parameters they both
take, the check of `values` that runs before anything is sent to Flame, where the work files go,
and the paragraph of the description that says what an edit of a setup file is."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import setup_edit
from flame_mcp.setup_work import WORK_DIR_NAME, work_root
from flame_mcp.types import OperationParam, Refusal

__all__ = ["EDIT_NOTE", "keep_files_param", "values_param", "values_precheck", "work_root"]


def values_param(noun: str) -> OperationParam:
    return OperationParam(
        name="values",
        type="object",
        description=(
            f"{{tag: value}} for 1 to {setup_edit.SETUP_MAX_VALUES} tags of the {noun}'s setup "
            "file. A value is a string, an integer, a float or a boolean (written True / False); "
            "no <, >, & or quote, and no control character."
        ),
        required=True,
    )


def keep_files_param() -> OperationParam:
    return OperationParam(
        name="keep_files",
        type="boolean",
        description="Keep the work files (the setup before, the edited one and the read-back).",
        required=False,
        default=False,
    )


def values_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    """Refuse with INVALID_ARGS a `values` argument that could change the structure of the setup
    file or that is not {tag: value} of 1 to 20 plain values; nothing is sent to Flame."""
    problems = setup_edit.check_values(args.get("values"))
    if problems:
        return Refusal(
            "INVALID_ARGS",
            "nothing was sent to Flame: " + "; ".join(problems),
            result={"ran": False, "problems": problems},
        )
    return None


EDIT_NOTE: Final[str] = (
    "What it replaces: the value of a tag that occurs exactly once in the setup file as "
    "<TAG>value</TAG>, such as Timewarp's TW_FrameMode. `values` is {tag: value} for 1 to "
    f"{setup_edit.SETUP_MAX_VALUES} tags; a bool is written True / False as the file does. A tag "
    "that is absent, occurs more than once, has attributes or child elements, or is written "
    "self-closing is PRECONDITION_UNMET (result.tagCounts, result.tagProblems), so a value inside "
    "a curve cannot be set here. A tag name must match [A-Za-z_][A-Za-z0-9_]* and a value must "
    "hold no <, >, & or quote and no control character (newline, carriage return and tab "
    "included): anything else is INVALID_ARGS and nothing is sent to Flame. If every value "
    "already equals the one in the file the call is PRECONDITION_UNMET ('nothing to change'), "
    "decided before anything is written: that is not SILENT_NOOP, which is a write that had no "
    "effect. How it runs: the setup is saved to a work file, the values are replaced in a copy, "
    "the copy is loaded, and the setup is saved again to read it back. The call is ok when "
    "every tag reads back as asked (a number written another way counts: 0.5 and 0.500000), and "
    "FLAME_API_ERROR otherwise, with result.tags[tag] = {before, requested, after} and "
    "result.mismatched. A load that raises, or answers other than True, is FLAME_API_ERROR too. "
    "The read-back shows that Flame took the value, not that the value means anything: Flame "
    "accepted 99 for TW_FrameMode and read 99 back. A difference elsewhere in the read-back "
    "file is not an error: the call is ok with a note in `degraded` and the tags that differ in "
    "result.otherDifferences. The setup must be one file of UTF-8 text; a setup of several files "
    f"is PRECONDITION_UNMET (result.savedFiles). A value may be at most "
    f"{setup_edit.SETUP_MAX_VALUE_LENGTH} characters. The work files are in "
    f"<journal dir>/{WORK_DIR_NAME}/s_<12 hex>, readable by the owner only. They are removed "
    "after a success and after a refusal unless keep_files=true, and kept after a failure that "
    "comes after the edited file was written (a call that raised after that point is "
    "FLAME_API_ERROR, and result.ran says the write was tried). Where they are kept, "
    "result.files {original, edited, readback}, result.workDir and result.filesKept name them; "
    f"where they are removed those keys are left out. A work directory more than "
    f"{setup_edit.WORKDIR_MAX_AGE_DAYS} days old is removed when a new one is made."
)
