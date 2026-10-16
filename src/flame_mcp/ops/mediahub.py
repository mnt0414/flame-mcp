"""`mediahub` category: `mediahub.import`. The server side of the write
is `desktop_media_run`, the Flame side `desktop_media_prelude`."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.desktop_media_run import IMPORT_OP, run_media_write
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
from flame_mcp.types import (
    Category,
    OperationParam,
    OperationSpec,
    Refusal,
    SelectorKind,
)

CATEGORY = Category(
    name="mediahub",
    description=(
        "Importing media from disk into a reel, folder or library of the Desktop or the "
        "Libraries (what the MediaHub's import does)."
    ),
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)

MAX_PATHS: Final[int] = MAX_SOURCES
_DESTINATION_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")
_PATTERN: Final[re.Pattern[str]] = re.compile(r"\[[^\]]*\]")
"""A `[...]` in a path: a frame-number pattern (`clip.[00000001-00000024].tif`), which names files
that are not one file on disk, so it is not looked up."""

_IMPORT_DESCRIPTION = (
    "Import media files from disk into a reel, folder or library with flame.import_clips. "
    f"`paths`: 1 to {MAX_PATHS} ABSOLUTE paths (a file, a directory -- the media in it come in "
    "-- or a frame-number pattern such as clip.[00000001-00000024].tif). The server checks them "
    "first "
    "and writes nothing, journals nothing when one is not absolute (INVALID_ARGS), does not exist "
    "or is a file of 0 bytes (PRECONDITION_UNMET, result.missing / result.empty): Flame itself "
    "answers [] for those WITHOUT an error. A path with a [..] pattern is not looked up, so a "
    "pattern that matches nothing reaches Flame and ends in SILENT_NOOP. "
    "Resolution, caching and the like follow the MediaHub's import settings, which this op "
    "neither reads nor changes. Listing a directory on disk is the caller's job. "
    "A destination that is a reel of a Batch group is refused (INVALID_ARGS, nothing written): "
    "use batch.add_clips. "
    "result: destination, requested (paths) / returned (what Flame answered), imported "
    "[{type, name, uid, wiretapId, selector}] -- the destination's new entries, found by comparing "
    "what it lists before and after, not by the answer -- and the destination's entry names "
    "before / after. When the number of new entries differs from the number of paths (a "
    "directory or a sequence pattern is not one clip per path) the call is still ok and "
    "result.notes says so. "
    "A call after which the destination did not grow is an error (SILENT_NOOP), also in the "
    "journal outcome. The write call waits up to 30 s. "
    "JOURNAL as desktop.copy (intent with the destination's uids -> compare-and-swap -> outcome "
    "with the new clips' uids). Envelope journal = {group, id, revert}. "
    "Undo: journal.revert(group) deletes exactly those clips by uid (flame.delete), only while "
    "every one still has the name, duration and content (segments and effects) it had right "
    "after the call; if any changed, or the record has no content fingerprint, it is CONFLICT "
    "and nothing is deleted. One that is gone already is listed in result.skipped. Native "
    "Ctrl+Z is not used (undoSteps 0). Not allowed in plan.run."
)


def _import_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    paths = args.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS:
        count = len(paths) if isinstance(paths, list) else paths
        return Refusal("INVALID_ARGS", f"paths must hold 1 to {MAX_PATHS} paths, got {count!r}")
    bad = [p for p in paths if not isinstance(p, str) or not os.path.isabs(p)]
    if bad:
        return Refusal(
            "INVALID_ARGS",
            "paths must be absolute (a leading '/'; '~' and relative paths are not expanded): "
            + ", ".join(repr(p) for p in bad),
        )
    missing: list[str] = []
    empty: list[str] = []
    for path in paths:
        if _PATTERN.search(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            missing.append(path)
            continue
        if os.path.isfile(path) and stat.st_size == 0:
            empty.append(path)
    if not missing and not empty:
        return None
    parts = []
    if missing:
        parts.append(f"{len(missing)} path(s) do not exist: " + ", ".join(missing))
    if empty:
        parts.append(
            f"{len(empty)} file(s) have 0 bytes (Flame imports nothing from them and says "
            "nothing): " + ", ".join(empty)
        )
    return Refusal(
        "PRECONDITION_UNMET",
        "nothing was imported: " + "; ".join(parts),
        hint=(
            "Fix the paths and call again. Nothing was written and nothing journaled. A path "
            "with a [..] frame pattern is not checked here."
        ),
        result={"ran": False, "missing": missing, "empty": empty},
    )


def _import_to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_WRITE)
    if phase == PHASE_ALIVE:
        return alive_source(args)
    if phase == PHASE_REVERT:
        return build_source(
            f"_result = _dm_revert_imports({dict(args[CREATED_ARG])!r})",
            raise_on_error_src(),
            "_affected = len(_result['deleted'])",
        )
    resolve = selectors.resolve_code(args["destination"], "dest")
    if phase == PHASE_PROBE:
        return build_source(resolve, probe_source(args, "dest"))
    paths = list(args["paths"])
    if phase == PHASE_READ:
        return build_source(resolve, "_result = _dm_dest_snapshot(dest)", "_affected = 0")
    return build_source(
        resolve,
        f"_result = _dm_import(dest, {paths!r}, {args.get(EXPECTED_ARG)!r})",
        raise_on_error_src(),
        "_affected = len(_result['imported'])",
    )


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)
    registry.register(
        OperationSpec(
            name=IMPORT_OP,
            category="mediahub",
            description=_IMPORT_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="paths",
                    type="array",
                    description=(
                        f"1 to {MAX_PATHS} absolute paths: files, directories or frame-number "
                        "patterns."
                    ),
                    required=True,
                    item_type="string",
                ),
                OperationParam(
                    name="destination",
                    type="selector",
                    description=(
                        "A wiretap / path / selection selector of a Desktop reel, a library, a "
                        "library reel or a library folder. A reel of a Batch group is refused "
                        "(use batch.add_clips)."
                    ),
                    required=True,
                ),
            ),
            accepts=_DESTINATION_KINDS,
            to_python=_import_to_python,
            undo_steps=lambda args, affected: 0,
            precheck=_import_precheck,
            wrap=run_media_write,
            sample_args={
                "paths": ["/sample/a.mov"],
                "destination": {"kind": "wiretap", "id": "d"},
            },
            requires=(
                "flame.import_clips",
                "PyReel.children",
                "PyReel.type",
                "PyLibrary.children",
                "PyFolder.children",
                "flame.delete",
                "flame.find_by_uid",
            ),
        )
    )
