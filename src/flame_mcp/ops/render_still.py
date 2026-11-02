"""`render.still`: one frame of a clip as a small PNG. The server side is `still_run` (and
`still_check` for journal.check), the image side `still_proxy`, the Flame side `still_prelude`. By
design it provides no undo and never overwrites."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.desktop_run import EXPECTED_ARG, PHASE_ARG, PHASE_READ
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.registry import Registry
from flame_mcp.render_files import is_default_output_dir
from flame_mcp.render_run import DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S
from flame_mcp.still_proxy import (
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_MAX_WIDTH,
    FFMPEG_ENV,
    MAX_IMAGE_BYTES,
    MAX_WIDTH,
    MIN_IMAGE_BYTES,
    MIN_WIDTH,
    SOURCE_EXTENSIONS,
    STILLS_DIR_ENV,
    resolve_ffmpeg,
)
from flame_mcp.still_run import (
    OP_NAME,
    PHASE_CLEANUP,
    PRESETS_DIR_ENV,
    STILL_ARG,
    TEMP_REEL,
    run_still,
)
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")
_EXTENSIONS = ", ".join(SOURCE_EXTENSIONS)

_DESCRIPTION = (
    "One frame of a clip as a small PNG for checking it: Flame writes the frame, ffmpeg makes the "
    "PNG. The PNG is a derived check image, not a deliverable -- it is scaled down and converted "
    "to 8 bit RGB, and nothing about the colors of the original is carried over. "
    "source is exactly one of two forms. {target, frame}: target is a wiretap / path / selection "
    "selector of a PyClip, frame the frame to take, counted from 1 at the head of the clip "
    "whatever its start_frame (1 to the duration; anything else is INVALID_ARGS before anything "
    "is copied). A PySequence or anything that is not a clip is SELECTOR_TYPE. "
    "{path}: an absolute path of an existing, readable file "
    f"({_EXTENSIONS}); nothing is sent to Flame, only the PNG is made. "
    "For {target, frame}, Flame makes a temporary reference copy of the clip in the reel "
    f"{TEMP_REEL!r} of the first reel group of the Desktop (the reel is made when it is not "
    "there), gives the copy the in and out mark of that frame, writes it with the preset "
    "'Tiff (16-bit LZW)' in the Foreground, and deletes the copy again, and the reel when this "
    "call made it and only while it holds no clip, sequence or other entry. The clip itself is "
    "not changed. The copy is deleted only while it still has the uid it was made with and the "
    "name it was last seen with; result.copy gives its uid, name and wiretap id and "
    "result.tempRemoved says whether it is gone, and a note in `degraded` says where it is when "
    "it is not (also when the reel gained no clip or more than one from the copy). "
    "output_dir is an existing absolute directory for the original TIFF. It must be empty "
    "(hidden entries are ignored) because the preset decides the names of the files; without it "
    f"a new directory below the stills directory ({STILLS_DIR_ENV}; by default a folder of "
    "this server in the user's Library) is used. The PNG always goes to the stills directory. "
    "Exactly one new file must appear: none is SILENT_NOOP, more than one is FLAME_API_ERROR. "
    "While Flame writes the frame it is occupied: MCP calls -- reads included -- may not be "
    "answered (BRIDGE_UNAVAILABLE or TIMEOUT). "
    f"The PNG is made by ffmpeg, found through {FFMPEG_ENV}, then the PATH, then the "
    "imageio_ffmpeg package; without one the call is PRECONDITION_UNMET before anything is done "
    "(flame_version_info says whether ffmpeg is found). max_width (default "
    f"{DEFAULT_MAX_WIDTH}, at most {MAX_WIDTH}) is the widest the PNG gets; it is never "
    "enlarged. An ffmpeg failure is judged by its exit code and its output file, not by what it "
    "prints. One ffmpeg run may take 15 s and the whole PNG stage, the narrower re-runs included, "
    "30 s; this server keeps other writes waiting meanwhile, and a stage that runs past its limit "
    "is a failure like any other ffmpeg failure below. The PNG is named proxy.png, one per run "
    "directory. If ffmpeg fails after Flame wrote "
    "the frame, the call stays ok: result.proxy is null and a note in `degraded` names the "
    "original, which a new call with a {path} source can turn into the PNG (calling again with "
    "{target, frame} would export another TIFF and make another copy). With a {path} source an "
    "ffmpeg failure is PRECONDITION_UNMET. "
    "inline=true also returns the PNG as an image next to the text answer, when it is at most "
    f"max_image_bytes (default {DEFAULT_MAX_IMAGE_BYTES}); a larger one is made again 0.75 "
    "times as wide, up to 4 times, and when it still does not fit it is not attached and a note "
    "in `degraded` says so. The text answer never holds image data: result.attachments names "
    "the file. "
    "The preset file is looked for under the presets directory of the Flame version in use "
    f"({PRESETS_DIR_ENV} overrides the root); a missing one is PRECONDITION_UNMET. "
    "result: source, original {path, width, height, format}, proxy {path, width, height, bytes, "
    "mimeType}, ffmpeg {path}, frame, exportElapsedS, tempRemoved. "
    "render.still provides no undo: the files stay on disk (undo=unsupported). A TIMEOUT / "
    "BRIDGE_LOST is not an answer: the export may still be running, and this server refuses every "
    "write until journal.check has decided. journal.check counts the file as render.export does "
    "and also deletes a leftover copy of this call -- only one whose name, duration and marks "
    "match the call's record; any other is left alone and reported as CONFLICT. journal.check "
    "makes no PNG: for a frame that landed, call this op again with {path}. "
    "Not allowed in plan.run."
)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG)
    if phase == PHASE_CLEANUP:
        return build_source(f"_result = _st_cleanup({args[STILL_ARG]!r})", "_affected = 0")
    resolve = selectors.resolve_code(args["source"]["target"], "target")
    if phase == PHASE_READ:
        return build_source(
            resolve,
            f"_result = _st_inspect(target, {TEMP_REEL!r})",
            raise_on_error_src(),
            "_affected = 0",
        )
    return build_source(
        resolve,
        f"_result = _st_run(target, {args.get(EXPECTED_ARG)!r}, {args.get(STILL_ARG)!r})",
        raise_on_error_src(),
        "_affected = 1",
    )


def _is_frame(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _source_refusal(source: Any) -> Refusal | None:
    if not isinstance(source, Mapping):
        return Refusal("INVALID_ARGS", "source must be an object: {target, frame} or {path}")
    keys = set(source)
    if keys == {"path"}:
        return _path_refusal(source["path"])
    if keys != {"target", "frame"}:
        return Refusal(
            "INVALID_ARGS",
            "source must be exactly one of {target, frame} and {path}, got keys "
            f"{sorted(keys)}",
        )
    issues = selectors.validate_selector(source["target"], _ACCEPTS)
    if issues:
        return Refusal("INVALID_ARGS", "source.target: " + "; ".join(issues))
    if not _is_frame(source["frame"]) or source["frame"] < 1:
        return Refusal(
            "INVALID_ARGS", f"source.frame must be an integer >= 1, got {source['frame']!r}"
        )
    return None


def _path_refusal(path: Any) -> Refusal | None:
    if not isinstance(path, str) or not os.path.isabs(path):
        return Refusal("INVALID_ARGS", f"source.path must be an absolute path: {path!r}")
    if os.path.splitext(path)[1].lower().lstrip(".") not in SOURCE_EXTENSIONS:
        return Refusal("INVALID_ARGS", f"source.path must end in one of {_EXTENSIONS}: {path!r}")
    if not os.path.isfile(path):
        return Refusal("PRECONDITION_UNMET", f"nothing was made: {path!r} is not a file")
    if not os.access(path, os.R_OK):
        return Refusal("PRECONDITION_UNMET", f"nothing was made: {path!r} is not readable")
    return None


def _output_dir_refusal(args: Mapping[str, Any], source: Mapping[str, Any]) -> Refusal | None:
    output_dir = args.get("output_dir")
    if output_dir is None:
        return None
    if "path" in source:
        return Refusal(
            "INVALID_ARGS", "output_dir means nothing for a {path} source: nothing is exported"
        )
    if not os.path.isabs(output_dir):
        return Refusal("INVALID_ARGS", f"output_dir must be an absolute path: {output_dir!r}")
    if not os.path.isdir(output_dir):
        return Refusal("PRECONDITION_UNMET", f"nothing was made: {output_dir!r} is not a directory")
    if not os.access(output_dir, os.W_OK):
        return Refusal("PRECONDITION_UNMET", f"nothing was made: {output_dir!r} is not writable")
    if is_default_output_dir(output_dir):
        return Refusal(
            "PRECONDITION_UNMET",
            f"nothing was made: {output_dir!r} is a shared temp directory",
            hint="Use a directory of your own, or leave output_dir out.",
        )
    return None


def _precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    timeout_s = args.get("timeout_s")
    if isinstance(timeout_s, (int, float)) and timeout_s > MAX_TIMEOUT_S:
        return Refusal(
            "INVALID_ARGS", f"timeout_s must be at most {MAX_TIMEOUT_S}, got {timeout_s}"
        )
    width = args.get("max_width", DEFAULT_MAX_WIDTH)
    if not _is_frame(width) or not MIN_WIDTH <= width <= MAX_WIDTH:
        return Refusal(
            "INVALID_ARGS",
            f"max_width must be an integer {MIN_WIDTH} to {MAX_WIDTH}, got {width!r}",
        )
    limit = args.get("max_image_bytes", DEFAULT_MAX_IMAGE_BYTES)
    if not _is_frame(limit) or not MIN_IMAGE_BYTES <= limit <= MAX_IMAGE_BYTES:
        return Refusal(
            "INVALID_ARGS",
            f"max_image_bytes must be an integer {MIN_IMAGE_BYTES} to {MAX_IMAGE_BYTES}, "
            f"got {limit!r}",
        )
    source = args.get("source")
    refused = _source_refusal(source)
    if refused is not None:
        return refused
    assert isinstance(source, Mapping)
    refused = _output_dir_refusal(args, source)
    if refused is not None:
        return refused
    if resolve_ffmpeg() is None:
        return Refusal(
            "PRECONDITION_UNMET",
            "nothing was made: ffmpeg was not found",
            hint=f"Install ffmpeg, or set {FFMPEG_ENV} to the path of an ffmpeg executable.",
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
                    name="source",
                    type="object",
                    description=(
                        "Exactly one of {target: a wiretap / path / selection selector of one "
                        "PyClip, frame: integer from 1} and {path: absolute path of an image "
                        f"file ({_EXTENSIONS})}}."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="output_dir",
                    type="string",
                    description=(
                        "Absolute path of an existing, empty directory for the original TIFF "
                        "(hidden entries are ignored). Default: a new directory below the "
                        "stills directory. Not for a {path} source."
                    ),
                    required=False,
                ),
                OperationParam(
                    name="max_width",
                    type="number",
                    description=(
                        f"The widest the PNG gets, {MIN_WIDTH} to {MAX_WIDTH} "
                        f"(default {DEFAULT_MAX_WIDTH}). A smaller source is not enlarged."
                    ),
                    required=False,
                    default=DEFAULT_MAX_WIDTH,
                    integer=True,
                    minimum=MIN_WIDTH,
                ),
                OperationParam(
                    name="inline",
                    type="boolean",
                    description="Also return the PNG as an image block of the answer.",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="max_image_bytes",
                    type="number",
                    description=(
                        f"With inline: the most bytes the attached PNG may have, "
                        f"{MIN_IMAGE_BYTES} to {MAX_IMAGE_BYTES} "
                        f"(default {DEFAULT_MAX_IMAGE_BYTES})."
                    ),
                    required=False,
                    default=DEFAULT_MAX_IMAGE_BYTES,
                    integer=True,
                    minimum=MIN_IMAGE_BYTES,
                ),
                OperationParam(
                    name="timeout_s",
                    type="number",
                    description=(
                        f"How long to wait for Flame to write the frame, 1 to {MAX_TIMEOUT_S} "
                        f"seconds (default {DEFAULT_TIMEOUT_S}). The bridge caps a wait at "
                        "600 s."
                    ),
                    required=False,
                    default=DEFAULT_TIMEOUT_S,
                    integer=True,
                    minimum=1,
                ),
            ),
            accepts=_ACCEPTS,
            sample_args={
                "source": {"target": {"kind": "wiretap", "id": "/sample/clip"}, "frame": 1}
            },
            to_python=_to_python,
            undo_steps=_no_undo_steps,
            precheck=_precheck,
            wrap=run_still,
            requires=(
                "flame.PyExporter",
                "PyExporter.export",
                "PyExporter.foreground",
                "PyExporter.export_between_marks",
                "flame.get_version",
                "flame.delete",
                "flame.media_panel",
                "PyMediaPanel.copy",
                "PyReelGroup.create_reel",
                "PyClip.name",
                "PyClip.uid",
                "PyClip.duration",
                "PyClip.start_frame",
                "PyClip.in_mark",
                "PyClip.out_mark",
                "PyClip.get_wiretap_node_id",
                "PyReel.clips",
                "PyTime.frame",
            ),
        )
    )
