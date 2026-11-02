"""`project` category: `info`, a read-only op reporting identity that every other op's rider
already reads piecemeal (project/workspace name, library count, current desktop name), and `save`,
which saves the project the way Ctrl+S (Save Project) does."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationSpec

CATEGORY = Category(
    name="project",
    description="Current project/workspace identity.",
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)


def _info_to_python(_args: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            "_result = {",
            "    'projectName': flame.projects.current_project.name,",
            "    'workspaceName': _v(_ws.name),",
            "    'libraryCount': len(_ws.libraries),",
            "    'currentDesktopName': _v(_ws.desktop.name),",
            "}",
        ]
    )


def _save_to_python(_args: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "import time as _time",
            "_project = flame.projects.current_project",
            "_ws = _project.current_workspace",
            "_t0 = _time.monotonic()",
            "_returned = _ws.commit()",
            "_elapsed = _time.monotonic() - _t0",
            "_result = {",
            "    'projectName': _v(_project.name),",
            "    'workspaceName': _v(_ws.name),",
            "    'elapsedS': round(_elapsed, 3),",
            "    'returned': repr(_returned),",
            "    'readBackDecisive': False,",
            "}",
        ]
    )


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)
    registry.register(
        OperationSpec(
            name="project.info",
            category="project",
            description=(
                "Current project name, current workspace name, the workspace's "
                "library count, and the current desktop's name."
            ),
            effect="read",
            to_python=_info_to_python,
            requires=(
                "flame.projects",
                "PyProject.current_workspace",
                "PyProject.name",
                "PyWorkspace.name",
                "PyWorkspace.libraries",
                "PyWorkspace.desktop",
                "PyDesktop.name",
            ),
        )
    )
    registry.register(
        OperationSpec(
            name="project.save",
            category="project",
            description=(
                "Save the project, like Flame's Ctrl+S (Save Project): PyWorkspace.commit() of "
                "the current workspace. This server does not read the save back, so the call is "
                "ok when commit returns without an exception and result.readBackDecisive is "
                "false; result has projectName, workspaceName, elapsedS and returned (the repr "
                "of what commit returned). Runs main-async: a save that outlives the wait comes "
                "back ok with pending.handle, and journal.poll(handle) gives its result. After "
                "a TIMEOUT or BRIDGE_LOST it is not known whether the project was saved; calling "
                "project.save again saves the current state. "
                "Undo: unsupported -- this server has no op that moves the saved point back. "
                "This is not Save Desktop (PyDesktop.save), which puts a copy of the Desktop "
                "into a library. Not allowed in plan.run."
            ),
            effect="create",
            undo="unsupported",
            thread="main-async",
            to_python=_save_to_python,
            undo_steps=_no_undo_steps,
            requires=(
                "flame.projects",
                "PyProject.current_workspace",
                "PyProject.name",
                "PyWorkspace.name",
                "PyWorkspace.commit",
            ),
        )
    )
