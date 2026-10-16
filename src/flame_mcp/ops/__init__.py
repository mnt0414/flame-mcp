"""Operation registration entry point.

Each op module (e.g. `ops/project.py`) declares its `Category`/`OperationSpec`
values and exposes its own `register(registry: Registry) -> None`;
`register_all` below calls each of those in turn.
"""

from __future__ import annotations

from flame_mcp.ops import (
    action,
    batch,
    checkpoints,
    desktop,
    desktop_delete,
    desktop_media,
    desktop_read,
    eval,
    journal,
    library,
    mediahub,
    node,
    plan,
    project,
    render,
    segment,
    segment_edit,
    segment_effects,
    segment_extract,
    segment_trim,
    timeline,
    timeline_build,
    timeline_checkpoints,
    timeline_create,
)
from flame_mcp.registry import Registry


def register_all(registry: Registry) -> None:
    project.register(registry)
    desktop.register(registry)
    desktop_read.register(registry)
    desktop_media.register(registry)
    desktop_delete.register(registry)
    mediahub.register(registry)
    library.register(registry)
    timeline.register(registry)
    timeline_checkpoints.register(registry)
    timeline_build.register(registry)
    timeline_create.register(registry)
    segment.register(registry)
    segment_trim.register(registry)
    segment_edit.register(registry)
    segment_effects.register(registry)
    segment_extract.register(registry)
    batch.register(registry)
    node.register(registry)
    action.register(registry)
    render.register(registry)
    journal.register(registry)
    checkpoints.register(registry)
    eval.register(registry)
    plan.register(registry)
    registry.validate_references()
