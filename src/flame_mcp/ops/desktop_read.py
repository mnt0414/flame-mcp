"""`desktop.find` and `desktop.info`: the read ops that look things up on the Desktop and in the
libraries (the rest of the `desktop` category lives in `desktop.py`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.ops._common import build_source, list_result_src, offset_param
from flame_mcp.registry import Registry
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_FIND_PARENT_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path")
_INFO_TARGET_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection", "timeline")

_FIND_DESCRIPTION = (
    "Find entries by name or by uid, one page at a time. A name search goes across the Desktop "
    "and the libraries and returns every entry of that name, so add `parent` (a wiretap / path "
    "selector of the reel to look in) to narrow it. A uid names "
    "one entry (or none: an unknown uid, like an unknown name, is an empty page, not an "
    "error). Give exactly one of name / uid; `parent` goes with a name only. `types` keeps "
    "only the named Flame types (e.g. ['PyClip', 'PySequence']; `total` counts after it). "
    "Each item: type, name, uid, `selector` (wiretap; null when the entry has no wiretap id, "
    "e.g. a Batch group) and `parent` {type, name}. A search lists nothing from inside a closed "
    "library (a closed library reads as empty; library.list says which are closed); a library "
    "that `parent` names is opened for the call and closed again."
)

_INFO_DESCRIPTION = (
    "Describe one Desktop or Library entry. Always: type, name, uid, wiretapId, parent {type, "
    "name}. A clip or sequence adds duration, startFrame, startTime, frameRate, width, height, "
    "ratio, bitDepth, scanMode, cached, unlinked, `versions` (the number of tracks of each "
    "version) and `audioTracks` (the number of audio tracks); each is read on its own, so one "
    "that cannot be read is {'error': ...} and the rest still come back. A reel, reel group, "
    "folder, library or the Desktop adds `childCounts` {Flame type name: count}. Any other "
    "type (a Batch group, ...) answers the common fields only. A target inside a closed library "
    "given by a wiretap or path selector is opened for the call and closed again."
)


def _find_to_python(args: Mapping[str, Any]) -> str:
    uid = args.get("uid")
    name = None if uid is not None else args.get("name")
    parent = args.get("parent")
    types = list(args.get("types") or [])
    resolve = (
        selectors.resolve_code(parent, "parent")
        if parent is not None and uid is None
        else "parent = None"
    )
    return build_source(
        resolve,
        "\n".join(
            [
                f"_found = _rp_find({name!r}, {uid!r}, parent)",
                f"_types = {types!r}",
                "if _types:",
                "    _found = [_x for _x in _found if type(_x).__name__ in _types]",
                f"_limit = {args['limit']!r}",
                f"_offset = {args['offset']!r}",
                "_total = len(_found)",
                "_items = [_rp_find_item(_x) for _x in _found[_offset : _offset + _limit]]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
    )


def _find_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    given = [key for key in ("name", "uid") if args.get(key) is not None]
    if len(given) != 1:
        return Refusal("INVALID_ARGS", "give exactly one of name / uid")
    if given == ["uid"] and "parent" in args:
        return Refusal(
            "INVALID_ARGS", "parent only narrows a search by name (a uid names one entry)"
        )
    return None


def _info_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "_result = _rp_info(target)",
    )


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name="desktop.find",
            category="desktop",
            description=_FIND_DESCRIPTION,
            effect="read",
            params=(
                OperationParam(
                    name="name",
                    type="string",
                    description="Entry name (exact). Give exactly one of name / uid.",
                    required=False,
                    non_empty=True,
                ),
                OperationParam(
                    name="uid",
                    type="string",
                    description="Entry uid. Give exactly one of name / uid.",
                    required=False,
                    non_empty=True,
                ),
                OperationParam(
                    name="parent",
                    type="selector",
                    description=(
                        "With name only: a wiretap / path selector of the entry to look in; "
                        "omit it to search the Desktop and every library."
                    ),
                    required=False,
                ),
                OperationParam(
                    name="types",
                    type="array",
                    description=(
                        "Flame type names to keep, e.g. ['PyClip', 'PySequence']. "
                        "Omit (or pass []) to keep every type."
                    ),
                    required=False,
                    item_type="string",
                ),
                OperationParam(
                    name="limit", type="number", description="Max items to return.", required=False
                ),
                offset_param(),
            ),
            accepts=_FIND_PARENT_KINDS,
            precheck=_find_precheck,
            to_python=_find_to_python,
            requires=("flame.find_by_name", "flame.find_by_uid", "PyReel.parent"),
        )
    )
    registry.register(
        OperationSpec(
            name="desktop.info",
            category="desktop",
            description=_INFO_DESCRIPTION,
            effect="read",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description=(
                        "wiretap / path / selection / timeline selector for the entry "
                        "(timeline: the sequence the Timeline is showing)."
                    ),
                    required=True,
                ),
            ),
            accepts=_INFO_TARGET_KINDS,
            to_python=_info_to_python,
            requires=(
                "PyClip.duration",
                "PyClip.start_frame",
                "PyClip.start_time",
                "PyClip.frame_rate",
                "PyClip.width",
                "PyClip.height",
                "PyClip.ratio",
                "PyClip.bit_depth",
                "PyClip.scan_mode",
                "PyClip.cached",
                "PyClip.unlinked",
                "PyClip.versions",
                "PyClip.audio_tracks",
                "PyClip.parent",
                "PySequence.duration",
                "PySequence.start_frame",
                "PySequence.start_time",
                "PySequence.frame_rate",
                "PySequence.width",
                "PySequence.height",
                "PySequence.ratio",
                "PySequence.bit_depth",
                "PySequence.scan_mode",
                "PySequence.cached",
                "PySequence.unlinked",
                "PySequence.versions",
                "PySequence.audio_tracks",
                "PySequence.parent",
                "PyReel.children",
                "PyReel.parent",
                "PyReelGroup.children",
                "PyReelGroup.parent",
                "PyFolder.children",
                "PyFolder.parent",
                "PyLibrary.children",
                "PyLibrary.parent",
                "PyDesktop.children",
                "PyDesktop.parent",
            ),
        )
    )
