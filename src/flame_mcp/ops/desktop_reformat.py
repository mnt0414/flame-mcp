"""`desktop.reformat`. The server side of the write is `desktop_reformat_run` (and
`desktop_reformat_revert` for journal.revert / journal.check), the Flame side
`desktop_reformat_prelude`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.desktop_delete_run import PHASE_STASH
from flame_mcp.desktop_reformat_run import run_reformat
from flame_mcp.desktop_reformat_state import (
    OP_NAME,
    PHASE_DUP,
    PHASE_OBSERVE,
    RATIO_TOLERANCE,
    REFORMAT_WAIT_S,
    SPEC_ARG,
    build_spec,
)
from flame_mcp.desktop_run import (
    CREATED_ARG,
    EXPECTED_ARG,
    MAX_SOURCES,
    PHASE_ALIVE,
    PHASE_ARG,
    PHASE_PROBE,
    PHASE_READ,
    PHASE_REVERT,
    PHASE_WRITE,
)
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._media_phases import alive_source, probe_source
from flame_mcp.registry import Registry
from flame_mcp.timeline_checkpoints import (
    DEFAULT_LIBRARY,
    LIBRARY_ARG,
    LIBRARY_ENV,
    REEL_ARG,
    REEL_PREFIX,
    checkpoint_library_name,
    stash_reel_name,
)
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")
MIN_SIZE: Final[int] = 24
MAX_SIZE: Final[int] = 16384
MIN_RATIO: Final[float] = 0.01
MAX_RATIO: Final[float] = 100.0
MAX_START_FRAME: Final[int] = 999_999_999
RESIZE_MODES: Final[tuple[str, ...]] = ("Letterbox", "Crop Edges", "Fill", "Centre")
SCAN_MODES: Final[tuple[str, ...]] = ("F1", "F2", "P")
BIT_DEPTHS: Final[tuple[int, ...]] = (8, 10, 12, 16, 32)

_DESCRIPTION = (
    "Reformat clips on the Desktop in place or on a copy. "
    f"`targets`: 1 to {MAX_SOURCES} wiretap / path / selection selectors, each one PyClip in a "
    f"Desktop reel. width and height are required, even, {MIN_SIZE} to {MAX_SIZE} (an odd size "
    "is refused, never rounded). Flame gets ratio = round(width / height, 6) unless `ratio` is "
    "passed, resize_mode 'Fill' unless given, and bit_depth / scan_mode / frame_rate only when "
    "given; start_frame is applied with PyClip.change_start_frame. "
    "Refused before anything is written: a target that is not a PyClip (a sequence, a reel ...) "
    "or is inside a library (PRECONDITION_UNMET), inside a reel of a Batch group or listed twice "
    "(INVALID_ARGS), an odd or out-of-range size (INVALID_ARGS), and -- unless "
    "allow_aspect_change=true -- a width / height ratio that differs from a clip's PyClip.ratio "
    f"by more than {RATIO_TOLERANCE} (PRECONDITION_UNMET; result.aspect lists every clip's "
    "ratio). "
    "In place, each clip that needs a write is first copied into the stash reel "
    f"('{DEFAULT_LIBRARY}', reel '{REEL_PREFIX}<YYMMDD>'; the environment variable {LIBRARY_ENV} "
    "names another library) so that journal.revert can restore it; duplicate=true reformats a "
    "flame.duplicate copy instead, leaves the original alone and stashes nothing. The clips "
    f"are done one after another, one Flame call each (a call waits up to {REFORMAT_WAIT_S:g} s, "
    "so a call takes about the sum). A clip that already has the requested format is skipped. "
    "Each clip is judged by reading it back, never by the answer (PyClip.reformat answers None): "
    "not as asked and unchanged = SILENT_NOOP; changed but not as asked, or an exception of "
    "Flame = FLAME_API_ERROR; a clip changed since it was read = CONFLICT (not touched). "
    "result: mode, reformatted / notReformatted [{index, from, to, before, after, applied, "
    "changed, skipped, checkpoint, seconds, flameSeconds, warnings, ...}], partial, notRun, "
    "warnings. Some clips done and some not: ok with result.partial = true and the envelope's "
    "partial {reformatted, notReformatted}. A Flame call that ends without an answer (TIMEOUT / "
    "BRIDGE_LOST / pending) stops the call at that clip and this server refuses writes until "
    "journal.check. "
    "Undo: envelope journal = {group, id, ids, revert}; journal.revert(group) goes through the "
    "clips newest first. In place it restores each stashed copy (the clip gets a NEW uid and "
    "wiretap id and goes to the END of its reel: result.restored[].to.selector; "
    "result.restored[].format says whether it has the format it had before); with "
    "duplicate=true it deletes the copy. It is a CONFLICT, and nothing is restored or deleted, "
    "while the clip's format or its segments / effects differ from what the call left (a later "
    "change); force=true overrides, journaled. A clip that cannot be put back leaves the others "
    "restored (result.partial); a second revert puts back only the rest. A clip that did not "
    "change has nothing to revert. Stashed copies stay in the checkpoint library until "
    "journal.purge_checkpoints removes them. Native Ctrl+Z is not used (undoSteps 0). "
    "Not allowed in plan.run: its journal group holds one intent per clip and is reverted by "
    "restoring stashed copies (or deleting duplicates), which a plan's group (reverted by "
    "putting attribute values back) does not do."
)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _reformat_to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_READ)
    if phase == PHASE_ALIVE:
        return alive_source(args)
    if phase == PHASE_REVERT:
        return build_source(
            f"_result = _dm_revert_duplicate({dict(args[CREATED_ARG])!r})",
            raise_on_error_src(),
            "_affected = 1 if _result['deleted'] else 0",
        )
    if phase == PHASE_OBSERVE:
        items = list(args[CREATED_ARG]["items"])
        return build_source(f"_result = _rf_observe({items!r})", "_affected = 0")
    if phase == PHASE_PROBE:
        resolve = selectors.resolve_code(args["target"], "target")
        return build_source(resolve, probe_source(args, "target"))
    if phase == PHASE_DUP:
        resolve = selectors.resolve_code(args["target"], "target")
        return build_source(
            resolve,
            f"_result = _dm_duplicate(target, {args.get(EXPECTED_ARG)!r})",
            raise_on_error_src(),
            "_affected = 1",
        )
    if phase == PHASE_WRITE:
        resolve = selectors.resolve_code(args["target"], "target")
        return build_source(
            resolve,
            f"_result = _rf_write(target, {dict(args[SPEC_ARG])!r}, {args.get(EXPECTED_ARG)!r})",
            raise_on_error_src(),
            "_affected = 1 if _result['changed'] else 0",
        )
    targets = list(args["targets"])
    blocks = [selectors.resolve_code(sel, f"_t{i}") for i, sel in enumerate(targets)]
    listed = "[" + ", ".join(f"_t{i}" for i in range(len(targets))) + "]"
    if phase == PHASE_STASH:
        library = args.get(LIBRARY_ARG) or checkpoint_library_name()
        reel = args.get(REEL_ARG) or stash_reel_name()
        return build_source(
            *blocks,
            f"_result = _rf_stash({listed}, {library!r}, {reel!r})",
            raise_on_error_src(),
            "_affected = 0",
        )
    return build_source(
        *blocks,
        f"_result = _rf_snapshot({listed}, {build_spec(args)!r})",
        raise_on_error_src(),
        "_affected = 0",
    )


def _size_problem(name: str, value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not MIN_SIZE <= value <= MAX_SIZE:
        return f"{name} must be {MIN_SIZE} to {MAX_SIZE}, got {value!r}"
    if int(value) % 2 != 0:
        return f"{name} must be even, got {value!r} (an odd size is not rounded)"
    return None


def _reformat_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    targets = args.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_SOURCES:
        count = len(targets) if isinstance(targets, list) else targets
        return Refusal(
            "INVALID_ARGS", f"targets must hold 1 to {MAX_SOURCES} selectors, got {count!r}"
        )
    problems = [
        f"targets[{i}]: {issue}"
        for i, sel in enumerate(targets)
        for issue in selectors.validate_selector(sel, _ACCEPTS)
    ]
    for i, sel in enumerate(targets):
        if sel in targets[:i]:
            problems.append(f"targets[{i}]: listed twice")
    for name in ("width", "height"):
        problem = _size_problem(name, args.get(name))
        if problem is not None:
            problems.append(problem)
    ratio = args.get("ratio")
    if ratio is not None and not MIN_RATIO <= ratio <= MAX_RATIO:
        problems.append(f"ratio must be {MIN_RATIO} to {MAX_RATIO:g}, got {ratio!r}")
    start = args.get("start_frame")
    if start is not None and start > MAX_START_FRAME:
        problems.append(f"start_frame must be 1 to {MAX_START_FRAME}, got {start!r}")
    if problems:
        return Refusal("INVALID_ARGS", "invalid arguments: " + "; ".join(problems))
    return None


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=OP_NAME,
            category="desktop",
            description=_DESCRIPTION,
            effect="destructive",
            undo="value",
            thread="main-async",
            params=(
                OperationParam(
                    name="targets",
                    type="array",
                    description=(
                        f"1 to {MAX_SOURCES} selectors of clips: wiretap, path "
                        "(root='desktop' for the Desktop) or selection; each must resolve to "
                        "one PyClip in a reel on the Desktop, and none may be listed twice."
                    ),
                    required=True,
                    item_type="selector",
                ),
                OperationParam(
                    name="width",
                    type="number",
                    description=f"New width in pixels: an even integer, {MIN_SIZE} to {MAX_SIZE}.",
                    required=True,
                    integer=True,
                    minimum=MIN_SIZE,
                ),
                OperationParam(
                    name="height",
                    type="number",
                    description=f"New height in pixels: an even integer, {MIN_SIZE} to {MAX_SIZE}.",
                    required=True,
                    integer=True,
                    minimum=MIN_SIZE,
                ),
                OperationParam(
                    name="resize_mode",
                    type="string",
                    description=(
                        "How the picture is fitted: 'Letterbox', 'Crop Edges', 'Fill' or "
                        "'Centre' (default 'Fill')."
                    ),
                    required=False,
                    default="Fill",
                    enum=RESIZE_MODES,
                ),
                OperationParam(
                    name="bit_depth",
                    type="number",
                    description="New bit depth: 8, 10, 12, 16 or 32 (default: not changed).",
                    required=False,
                    integer=True,
                    enum=BIT_DEPTHS,
                ),
                OperationParam(
                    name="ratio",
                    type="number",
                    description=(
                        f"Frame aspect ratio given to Flame, {MIN_RATIO} to {MAX_RATIO:g} "
                        "(default round(width / height, 6))."
                    ),
                    required=False,
                ),
                OperationParam(
                    name="scan_mode",
                    type="string",
                    description="New scan mode: 'F1', 'F2' or 'P' (default: not changed).",
                    required=False,
                    enum=SCAN_MODES,
                ),
                OperationParam(
                    name="frame_rate",
                    type="string",
                    description=(
                        "New frame rate in Flame's notation, e.g. '23.976 fps' (default: not "
                        "changed). Flame validates it; the clip is read back and reported."
                    ),
                    required=False,
                    non_empty=True,
                ),
                OperationParam(
                    name="duplicate",
                    type="boolean",
                    description=(
                        "true: reformat a copy made with flame.duplicate and leave the original "
                        "alone (default false: reformat the clip in place)."
                    ),
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="start_frame",
                    type="number",
                    description=(
                        "New start frame (PyClip.change_start_frame): an integer, 1 to "
                        f"{MAX_START_FRAME} (default: not changed)."
                    ),
                    required=False,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="use_segment_connections",
                    type="boolean",
                    description=(
                        "The use_segment_connections argument of change_start_frame (default "
                        "true). Means nothing without start_frame."
                    ),
                    required=False,
                    default=True,
                ),
                OperationParam(
                    name="allow_aspect_change",
                    type="boolean",
                    description=(
                        "false (default): refuse when width / height differs from a clip's "
                        "current ratio by more than 0.001. true: reformat to another aspect "
                        "ratio."
                    ),
                    required=False,
                    default=False,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_reformat_to_python,
            undo_steps=_no_undo_steps,
            precheck=_reformat_precheck,
            wrap=run_reformat,
            sample_args={
                "targets": [{"kind": "wiretap", "id": "s"}],
                "width": 1920,
                "height": 1080,
            },
            requires=(
                "PyClip.reformat",
                "PyClip.change_start_frame",
                "flame.duplicate",
                "flame.delete",
                "PyMediaPanel.copy",
                "PyMediaPanel.move",
                "PyClip.width",
                "PyClip.height",
                "PyClip.ratio",
                "PyClip.bit_depth",
                "PyClip.scan_mode",
                "PyClip.frame_rate",
                "PyClip.start_frame",
                "PyClip.duration",
                "PyClip.parent",
                "PyReel.children",
                "PyReel.type",
                "PyLibrary.children",
                "PyLibrary.reels",
                "PyLibrary.create_reel",
                "PyWorkspace.libraries",
                "PyWorkspace.create_library",
                "PySequence.versions",
                "PyVersion.tracks",
                "PyTrack.segments",
                "flame.find_by_uid",
                "flame.find_by_wiretap_node_id",
            ),
        )
    )
