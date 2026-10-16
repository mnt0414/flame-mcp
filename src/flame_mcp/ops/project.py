"""`project` category: a single read-only op reporting identity that
every other op's rider already reads piecemeal (project/workspace name,
library count, current desktop name)."""

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
