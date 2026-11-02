"""`render.export`: write a clip or sequence to files with an export preset. The server side is
`export_run` (and `export_check` for journal.check), the Flame side `export_prelude`. By design it
provides no undo and never overwrites."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.desktop_run import EXPECTED_ARG, PHASE_ARG, PHASE_READ
from flame_mcp.export_run import OP_NAME, run_export
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.registry import Registry
from flame_mcp.render_files import (
    EXPORT_COUNT_DEPTH,
    EXPORT_COUNT_LIMIT,
    EXPORT_SCAN_DEPTH,
    is_default_output_dir,
)
from flame_mcp.render_run import DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")

_DESCRIPTION = (
    "Write one clip or sequence to files with an export preset (PyExporter.export), in the "
    "Foreground. "
    "The target is a wiretap / path / selection selector of a PyClip or PySequence; anything "
    "else is SELECTOR_TYPE. preset is the absolute path of an export preset .xml file, "
    "output_dir an existing absolute directory. The preset decides the names of the files and "
    "whether they go into a sub-folder of output_dir; result.files lists what was written as "
    "paths relative to output_dir. "
    "While it runs Flame is occupied: MCP calls -- reads included -- may not be answered "
    "(BRIDGE_UNAVAILABLE or TIMEOUT). Make timeout_s long enough for the whole export: this "
    "server has no op to stop it. "
    "Because the preset decides the names and the folders of the files, this op writes only "
    "into an empty directory and never overwrites: make a new directory for each export. If "
    "output_dir holds anything that is not hidden the call is PRECONDITION_UNMET and nothing is "
    f"exported or journaled (result.existing: the count and the first 10, listed down to "
    f"{EXPORT_SCAN_DEPTH} folder levels). Hidden entries -- names starting with a dot, such as "
    ".DS_Store, and what is inside a hidden folder -- do not count. A directory that cannot be "
    "listed is not taken for an empty one, and one that is not writable is refused. Right before "
    "the export Flame lists the directory again and exports nothing (CONFLICT) if something has "
    "appeared since. Also refused before anything is exported (PRECONDITION_UNMET): a preset "
    "file or an output_dir that does not exist, and /tmp or /var/tmp unless "
    "allow_default_path=true. "
    "between_marks=true writes the frames from the in mark up to, but not including, the out "
    "mark. "
    "What was written is counted afterwards by listing output_dir down to "
    f"{EXPORT_COUNT_DEPTH} folder levels: the files that are new and not hidden are result.files "
    "(writtenCount); none = SILENT_NOOP, an exception of Flame = FLAME_API_ERROR. For an "
    "image-sequence preset with between_marks false that does not write one poster frame, "
    "result.expectedFrames is the target's frames and is compared with the total number of "
    "files written (not hidden ones): fewer is FLAME_API_ERROR with a message starting "
    "`partial:` (the files stay). When the listing was cut short (more than "
    f"{EXPORT_COUNT_LIMIT} entries, or a folder that cannot be read: result.countComplete false) "
    "or folders appeared but no file was found in them (result.newFolders), the count cannot "
    "settle a result of 0 or one short of expectedFrames: the call is ok with a note in "
    "`degraded`, neither SILENT_NOOP nor partial. "
    "render.export provides no undo: the files stay on disk (undo=unsupported; journal.revert "
    "has nothing to restore). "
    "A TIMEOUT / BRIDGE_LOST is not an answer: the export may still be running (a TIMEOUT does "
    "not stop it), and this server refuses every write until journal.check has decided. When the "
    "wait ended with the bridge's pending handle, or the bridge still holds the result under the "
    "handle this server gave the call, journal.check asks the bridge for the result first (that "
    "says the export has ended; basis 'handle'). Otherwise it lists output_dir again: landed = "
    "every expected file written since the call (any file when the count is not known), partial "
    "= some, not_landed = none (basis 'disk', an estimate); a count that cannot settle it, as "
    "above, leaves the check unknown. "
    "A sequence is checked first with timeline.colour_spaces: one whose segments are in two or "
    "more different colour spaces, or whose colour spaces could not all be read (a scan cut "
    "short, an error, a value that is not a name), is PRECONDITION_UNMET with "
    "result.colourSpaces and nothing is exported or journaled. skip_colour_check=true exports "
    "anyway and says so in `degraded`. A clip is not checked. "
    "Not allowed in plan.run."
)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _to_python(args: Mapping[str, Any]) -> str:
    resolve = selectors.resolve_code(args["target"], "target")
    if args.get(PHASE_ARG) == PHASE_READ:
        return build_source(
            resolve,
            "_result = _ex_inspect(target)",
            raise_on_error_src(),
            "_affected = 0",
        )
    return build_source(
        resolve,
        f"_result = _ex_export(target, {args.get(EXPECTED_ARG)!r}, {args['preset']!r}, "
        f"{args['output_dir']!r}, {bool(args.get('between_marks', False))!r})",
        raise_on_error_src(),
        "_affected = 1",
    )


def _precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    timeout_s = args.get("timeout_s")
    if isinstance(timeout_s, (int, float)) and timeout_s > MAX_TIMEOUT_S:
        return Refusal(
            "INVALID_ARGS", f"timeout_s must be at most {MAX_TIMEOUT_S}, got {timeout_s}"
        )
    preset = str(args.get("preset") or "")
    output_dir = str(args.get("output_dir") or "")
    if not os.path.isabs(preset) or not preset.lower().endswith(".xml"):
        return Refusal(
            "INVALID_ARGS", f"preset must be an absolute path to a .xml file: {preset!r}"
        )
    if not os.path.isabs(output_dir):
        return Refusal("INVALID_ARGS", f"output_dir must be an absolute path: {output_dir!r}")
    if not os.path.isfile(preset):
        return Refusal("PRECONDITION_UNMET", f"nothing was exported: no preset file at {preset!r}")
    if not os.path.isdir(output_dir):
        return Refusal(
            "PRECONDITION_UNMET", f"nothing was exported: {output_dir!r} is not a directory"
        )
    if not os.access(output_dir, os.W_OK):
        return Refusal(
            "PRECONDITION_UNMET", f"nothing was exported: {output_dir!r} is not writable"
        )
    if is_default_output_dir(output_dir) and not args.get("allow_default_path", False):
        return Refusal(
            "PRECONDITION_UNMET",
            f"nothing was exported: {output_dir!r} is a shared temp directory",
            hint="Use a directory of your own, or pass allow_default_path=true.",
        )
    return None


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=OP_NAME,
            category="render",
            description=_DESCRIPTION,
            effect="create",
            undo="unsupported",
            thread="main-async",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description=(
                        "A wiretap / path / selection selector of one PyClip or PySequence."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="preset",
                    type="string",
                    description="Absolute path of an export preset .xml file. It must exist.",
                    required=True,
                ),
                OperationParam(
                    name="output_dir",
                    type="string",
                    description=(
                        "Absolute path of an existing, empty directory to write into (hidden "
                        "entries such as .DS_Store are ignored). Use a new directory for each "
                        "export: nothing is overwritten."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="timeout_s",
                    type="number",
                    description=(
                        f"How long to wait for the export, 1 to {MAX_TIMEOUT_S} seconds "
                        f"(default {DEFAULT_TIMEOUT_S}). Make it long enough for the whole "
                        "export: while it runs the bridge may not answer MCP calls (reads "
                        "included) and this server has no op to stop it. The bridge caps a wait "
                        "at 600 s."
                    ),
                    required=False,
                    default=DEFAULT_TIMEOUT_S,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="allow_default_path",
                    type="boolean",
                    description="Allow an output_dir that is /var/tmp or /tmp.",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="between_marks",
                    type="boolean",
                    description=(
                        "Write only the frames from the in mark up to, but not including, the "
                        "out mark (PyExporter.export_between_marks). Default false: all frames."
                    ),
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="skip_colour_check",
                    type="boolean",
                    description=(
                        "Export a sequence even when its colour spaces are mixed or could not "
                        "all be read (default false). The result's `degraded` says so."
                    ),
                    required=False,
                    default=False,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_to_python,
            undo_steps=_no_undo_steps,
            precheck=_precheck,
            wrap=run_export,
            requires=(
                "flame.PyExporter",
                "PyExporter.export",
                "PyExporter.foreground",
                "PyExporter.export_between_marks",
                "PyClip.name",
                "PyClip.uid",
                "PyClip.duration",
                "PyClip.parent",
                "PyClip.get_wiretap_node_id",
                "PySequence.name",
                "PySequence.uid",
                "PySequence.duration",
                "PySequence.parent",
                "PySequence.get_wiretap_node_id",
                "PyTime.frame",
                "PyReel.type",
            ),
        )
    )
