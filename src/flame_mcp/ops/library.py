"""`library` category: operations specific to Libraries, Folders and Reels
inside a Library (their contents cannot be edited: `Clip is locked`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp.desktop_run import run_library_create
from flame_mcp.ops._common import build_source, list_result_src, offset_param
from flame_mcp.ops.desktop import _create_to_python, _no_undo_steps
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationParam, OperationSpec

CATEGORY = Category(
    name="library",
    description=(
        "Listing and creating libraries; operations specific to a Library, a Folder and a reel "
        "inside a library."
    ),
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)


def _list_to_python(args: Mapping[str, Any]) -> str:
    limit = args["limit"]
    offset = args["offset"]
    return build_source(
        "\n".join(
            [
                "_libs = list(flame.projects.current_project.current_workspace.libraries)",
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_libs)",
                "_items = [_describe_library(_x) for _x in _libs[_offset : _offset + _limit]]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
    )


def _create_library_to_python(args: Mapping[str, Any]) -> str:
    return _create_to_python({**args, "kind": "library"})


_CREATE_DESCRIPTION = (
    "Create an empty library in the current workspace. "
    "A same-named library that already exists refuses the call before anything is made "
    "(PRECONDITION_UNMET, result.existingNames; nothing written, nothing journaled) unless "
    "allow_rename=true: then Flame renames the new one (a number is appended, or a trailing "
    "number goes up); result.name / "
    "result.renamed / result.requestedName say what the name is. result.selector is a wiretap "
    "selector of the new library. "
    "JOURNAL: the server reads the list of libraries first, records an intent (the uids it "
    "held), creates with a compare-and-swap on that list (changed = CONFLICT, nothing created) "
    "and records the outcome. Envelope journal = {group, id, revert}. "
    "Undo: journal.revert(group) deletes the library (flame.delete) only while it is still "
    "empty: one that holds anything (a clip, a reel, a folder, a Batch group) is CONFLICT "
    "and nothing is deleted. A library that is closed (it reads as empty) is opened for the "
    "revert and closed again afterwards (result.librariesOpened); one that cannot be opened "
    "stops it (PRECONDITION_UNMET). "
    "Native Ctrl+Z is not used (undoSteps 0). Not allowed in plan.run (it owns its journal "
    "group)."
)


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)

    registry.register(
        OperationSpec(
            name="library.create",
            category="library",
            description=_CREATE_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="name",
                    type="string",
                    description="Name of the new library.",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="allow_rename",
                    type="boolean",
                    description=(
                        "Let Flame rename the new library when the name is taken (default "
                        "false: a taken name refuses the call)."
                    ),
                    required=False,
                    default=False,
                ),
            ),
            to_python=_create_library_to_python,
            undo_steps=_no_undo_steps,
            wrap=run_library_create,
            sample_args={"name": "sample"},
            requires=(
                "flame.projects",
                "PyProject.current_workspace",
                "PyWorkspace.create_library",
                "PyWorkspace.libraries",
                "PyLibrary.uid",
                "PyLibrary.children",
                "flame.delete",
            ),
        )
    )

    registry.register(
        OperationSpec(
            name="library.list",
            category="library",
            description=(
                "List the current workspace's libraries. Each item has `opened` (false = the "
                "library is closed: a closed library reads as empty until a call that reads "
                "inside it by selector opens it for the call and closes it again; reading this "
                "list does not open it)."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="limit", type="number", description="Max items to return.", required=False
                ),
                offset_param(),
            ),
            to_python=_list_to_python,
            requires=(
                "flame.projects",
                "PyProject.current_workspace",
                "PyWorkspace.libraries",
                "PyLibrary.name",
                "PyLibrary.attributes",
                "PyLibrary.get_wiretap_node_id",
            ),
        )
    )
