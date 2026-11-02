"""Operation registration entry point.

Each op module (e.g. `ops/project.py`) declares its `Category`/`OperationSpec`
values and exposes its own `register(registry: Registry) -> None`;
`register_all` below calls each of those in turn.
"""

from __future__ import annotations

from flame_mcp.ops import (
    action,
    batch,
    batch_delete,
    checkpoints,
    desktop,
    desktop_arrange,
    desktop_delete,
    desktop_list,
    desktop_media,
    desktop_read,
    desktop_reformat,
    desktop_reorder,
    desktop_tc,
    eval,
    journal,
    library,
    mediahub,
    node,
    node_edit_setup,
    plan,
    project,
    render,
    render_clip,
    render_export,
    render_still,
    segment,
    segment_connect,
    segment_edit,
    segment_effects,
    segment_extract,
    segment_trim,
    setup_read,
    timeline,
    timeline_build,
    timeline_checkpoints,
    timeline_colour,
    timeline_create,
    timeline_shorten,
    timeline_usage,
)
from flame_mcp.registry import Registry


def register_all(registry: Registry) -> None:
    project.register(registry)
    desktop.register(registry)
    desktop_read.register(registry)
    desktop_list.register(registry)
    desktop_media.register(registry)
    desktop_arrange.register(registry)
    desktop_delete.register(registry)
    desktop_reformat.register(registry)
    desktop_reorder.register(registry)
    desktop_tc.register(registry)
    mediahub.register(registry)
    library.register(registry)
    timeline.register(registry)
    timeline_checkpoints.register(registry)
    timeline_colour.register(registry)
    timeline_build.register(registry)
    timeline_create.register(registry)
    timeline_shorten.register(registry)
    timeline_usage.register(registry)
    segment.register(registry)
    segment_trim.register(registry)
    segment_connect.register(registry)
    segment_edit.register(registry)
    segment_effects.register(registry)
    segment_extract.register(registry)
    batch.register(registry)
    batch_delete.register(registry)
    node.register(registry)
    node_edit_setup.register(registry)
    setup_read.register(registry)
    action.register(registry)
    render.register(registry)
    render_clip.register(registry)
    render_export.register(registry)
    render_still.register(registry)
    journal.register(registry)
    checkpoints.register(registry)
    eval.register(registry)
    plan.register(registry)
    registry.validate_references()
