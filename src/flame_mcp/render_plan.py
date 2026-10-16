"""`render.plan`, server side.

`render.plan` runs in Flame (`render_prelude._render_plan`) and is completed here with what the
disk says (`render_files`): the directory, the files a render would write, whether some are
there already. Its result is the one place that says what a `render.batch` call would do, and
`preflight_problems` is what `render_run.run_render` refuses with before it renders.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import execution
from flame_mcp.registry import RegisteredOperation
from flame_mcp.render_files import (
    Stat,
    existing_planned,
    is_default_output_dir,
    list_directory,
    parse_resolved_path,
)

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

DEFAULT_MAX_FRAMES: Final[int] = 1000


def _range_count(start: Any, end: Any) -> int | None:
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        return None
    return end - start + 1


def _skip_reason(row: Mapping[str, Any]) -> str | None:
    if row.get("bypass") is True:
        return "bypassed"
    wired = row.get("wiredInputs")
    if wired is None:
        return "its input wiring could not be read"
    if wired == 0:
        return "no input is connected"
    return None


def _describe_writer(
    node: dict[str, Any], row: Mapping[str, Any], listings: dict[str, dict[str, Stat]]
) -> None:
    spec = parse_resolved_path(row.get("resolvedMediaPath"))
    media_path = row.get("mediaPath")
    node["outputSpec"] = spec.to_json() if spec is not None else None
    directory = spec.directory if spec is not None else None
    node["outputDir"] = directory
    node["sequence"] = spec.sequence if spec is not None else None
    node["defaultPath"] = is_default_output_dir(
        media_path if isinstance(media_path, str) else None
    ) or is_default_output_dir(directory)
    node["dirExists"] = None
    node["plannedFiles"] = spec.count if spec is not None else None
    node["firstFile"] = None
    node["lastFile"] = None
    node["existingPlannedFiles"] = None
    node["notes"] = []
    if spec is None:
        node["notes"].append(
            "get_resolved_media_path() gave no usable path, so the files this node would write "
            "are not known"
        )
        return
    if spec.sequence:
        node["firstFile"] = spec.name_of(spec.first)
        node["lastFile"] = spec.name_of(spec.last)
        counted = _range_count(row.get("rangeStart"), row.get("rangeEnd"))
        if counted is not None and counted != spec.count:
            node["notes"].append(
                f"the resolved path names frames {spec.first}-{spec.last} ({spec.count}) but "
                f"range_start / range_end say {counted}: the larger is counted against "
                "max_frames, the resolved path is what is checked on disk"
            )
    else:
        node["firstFile"] = node["lastFile"] = spec.path.rsplit("/", 1)[-1] if spec.path else None
        node["notes"].append(
            "the resolved path is not a numbered sequence: one file is expected and checked"
        )
    if directory is None:
        return
    exists = os.path.isdir(directory)
    node["dirExists"] = exists
    if exists:
        if directory not in listings:
            listings[directory] = list_directory(directory)
        node["existingPlannedFiles"] = len(existing_planned(spec, listings[directory]))


def frames_of(node: Mapping[str, Any]) -> int | None:
    counted = node.get("plannedFrames")
    files = node.get("plannedFiles")
    if counted is None:
        return None
    return max(counted, files) if isinstance(files, int) else int(counted)


def preflight_problems(
    plan: Mapping[str, Any],
    *,
    max_frames: int,
    allow_default_path: bool,
    allow_overwrite: bool = False,
) -> list[dict[str, Any]]:
    """What stops a `render.batch` before anything is written, from an enriched plan. Each
    problem is {code, node, message}; empty means the render may go."""
    problems: list[dict[str, Any]] = []
    effective = [n for n in plan.get("nodes") or [] if n.get("effective")]
    if not effective:
        problems.append(
            {
                "code": "no_effective_node",
                "node": None,
                "message": (
                    "the group has no Write File / Render node that is not bypassed and has an "
                    "input connected, so a render would write nothing"
                ),
            }
        )
    total = 0
    for node in effective:
        name = node.get("name")
        frames = frames_of(node)
        if frames is None or frames < 1:
            problems.append(
                {
                    "code": "range_unreadable",
                    "node": name,
                    "message": (
                        f"{name!r}: range_start / range_end are not a usable range "
                        f"({node.get('rangeStart')!r}-{node.get('rangeEnd')!r}), so the number "
                        "of frames it would write is not known"
                    ),
                }
            )
        else:
            total += frames
        if node.get("kind") != "write_file":
            continue
        if node.get("outputSpec") is None:
            problems.append(
                {
                    "code": "output_unknown",
                    "node": name,
                    "message": (
                        f"{name!r}: get_resolved_media_path() gave no path "
                        f"({node.get('resolvedMediaPath')!r}), so where it writes is not known"
                    ),
                }
            )
            continue
        if node.get("defaultPath") and not allow_default_path:
            problems.append(
                {
                    "code": "default_path",
                    "node": name,
                    "message": (
                        f"{name!r}: media_path is the default output directory "
                        f"({node.get('mediaPath')!r}; /var/tmp and /tmp are shared temp "
                        "directories): set media_path on the node (node.set_attributes) or "
                        "pass allow_default_path=true"
                    ),
                }
            )
        existing = node.get("existingPlannedFiles")
        if isinstance(existing, int) and existing > 0 and not allow_overwrite:
            problems.append(
                {
                    "code": "would_overwrite",
                    "node": name,
                    "message": (
                        f"{name!r}: {existing} of the files it would write already exist in "
                        f"{node.get('outputDir')!r} and a render replaces them for good: use "
                        "another output directory, or pass allow_overwrite=true"
                    ),
                }
            )
        if node.get("dirExists") is False:
            problems.append(
                {
                    "code": "output_dir_missing",
                    "node": name,
                    "message": (
                        f"{name!r}: the output directory {node.get('outputDir')!r} does not "
                        "exist (create it first)"
                    ),
                }
            )
    if total > max_frames:
        problems.append(
            {
                "code": "too_many_frames",
                "node": None,
                "message": (
                    f"the effective nodes would write {total} frames, more than max_frames="
                    f"{max_frames}: narrow range_start / range_end on the Write File nodes "
                    "(the default range is 1-100) or raise max_frames"
                ),
            }
        )
    return problems


def enrich_plan(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Flame's `_render_plan` result, completed with the disk and the reading of it."""
    listings: dict[str, dict[str, Stat]] = {}
    nodes: list[dict[str, Any]] = []
    for row in raw.get("nodes") or []:
        node: dict[str, Any] = dict(row)
        reason = _skip_reason(row)
        node["effective"] = reason is None
        node["skipReason"] = reason
        node["plannedFrames"] = _range_count(row.get("rangeStart"), row.get("rangeEnd"))
        if row.get("kind") == "write_file":
            _describe_writer(node, row, listings)
        else:
            node["output"] = (
                "a clip in the destination reel(s) "
                f"{row.get('destination')!r}; a Render node has no file path attribute, so "
                "what it wrote cannot be checked on disk"
            )
        nodes.append(node)
    plan: dict[str, Any] = {
        "batch": raw.get("batch"),
        "stable": raw.get("stable"),
        "nodes": nodes,
        "reels": raw.get("reels"),
    }
    effective = [n for n in nodes if n["effective"]]
    plan["summary"] = {
        "nodeCount": len(nodes),
        "effectiveNodes": len(effective),
        "skippedNodes": [
            {"name": n["name"], "reason": n["skipReason"]} for n in nodes if n["skipReason"]
        ],
        "plannedFrames": sum(frames_of(n) or 0 for n in effective),
        "wouldRefuse": preflight_problems(
            plan, max_frames=DEFAULT_MAX_FRAMES, allow_default_path=False
        ),
        "busyNote": (
            f"a render.batch of these {sum(frames_of(n) or 0 for n in effective)} planned "
            "frames occupies Flame for as long as it runs: nothing else in Flame responds and "
            "the bridge may not answer MCP calls (reads too) until it ends"
        ),
        "wouldRefuseNote": (
            f"what render.batch would refuse with its defaults (max_frames={DEFAULT_MAX_FRAMES}, "
            "allow_default_path=false, allow_overwrite=false); empty = it would render"
        ),
    }
    return plan


def run_plan(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `render.plan`: the Flame read, then the disk."""
    env = execution.run_op(op, op_args, run_dir=rt.run_dir)
    raw = env.get("result")
    if env.get("ok") is not True or not isinstance(raw, dict):
        return env
    env["result"] = enrich_plan(raw)
    return env
