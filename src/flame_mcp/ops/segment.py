"""`segment` category: one segment of a sequence (selected by a `segment`
selector)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.ops._attributes import attribute_requires, register_attribute_ops
from flame_mcp.ops._common import (
    AUTO_RESIZE_NOTE,
    REEL_CLIP_NOTE,
    build_source,
    list_result_src,
    offset_param,
)
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationParam, OperationSpec

CATEGORY = Category(
    name="segment",
    description="Segments of a sequence's or a reel clip's tracks.",
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)


def _info_to_python(args: Mapping[str, Any]) -> str:
    target_sel = args["target"]
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        "\n".join(
            [
                "try:",
                "    _attr_names = list(target.attributes)",
                "except BaseException:",
                "    _attr_names = []",
                "_attrs = {}",
                "for _key in _attr_names:",
                "    try:",
                "        _attrs[_key] = _ja(getattr(target, _key))",
                "    except BaseException as _attr_exc:",
                "        _attrs[_key] = {'error': str(_attr_exc) or type(_attr_exc).__name__}",
                "_result = {",
                "    'name': _v(target.name),",
                "    'sourceName': _v(target.source_name),",
                "    'recordIn': _j(target.record_in),",
                "    'recordOut': _j(target.record_out),",
                "    'sourceIn': _j(target.source_in),",
                "    'sourceOut': _j(target.source_out),",
                "    'recordDuration': _j(target.record_duration),",
                "    'sourceWidth': _j(getattr(target, 'source_width', None)),",
                "    'sourceHeight': _j(getattr(target, 'source_height', None)),",
                "    'sourceFrameRate': _j(getattr(target, 'source_frame_rate', None)),",
                "    'attributes': _attrs,",
                "}",
                "_result.update(_rp_segment_facts(target))",
                "_result.update(_rp_segment_source(target))",
            ]
        ),
    )


def _segment_stable_src(args: Mapping[str, Any]) -> str:
    sel = args["target"]
    return (
        f"_stable = _stable_segment(target__parent, {sel['version']!r}, {sel['track']!r}, "
        f"{sel.get('index')!r}, {sel.get('record_in')!r}, {sel.get('expect_source_name')!r})"
    )


def _list_effects_to_python(args: Mapping[str, Any]) -> str:
    include_available = bool(args.get("include_available", False))
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        f"_result = _rp_effects(target, {include_available!r})",
    )


def _shared_sources_to_python(args: Mapping[str, Any]) -> str:
    sel = args["target"]
    version, track = sel["version"], sel["track"]
    return build_source(
        selectors.resolve_code(sel, "target"),
        "\n".join(
            [
                f"_tpos = _rp_position(target__parent, {version!r}, {track!r}, target)",
                "_tsel = _wiretap_selector(target__parent)",
                "_tid = None if _tsel is None else _tsel['id']",
                "_segs = list(target.shared_source_segments())",
                f"_limit = {args['limit']!r}",
                f"_offset = {args['offset']!r}",
                "_total = len(_segs)",
                "_cache = {}",
                "_page = _segs[_offset : _offset + _limit]",
                "_items = [_rp_shared_item(_s, _tid, _tpos, _cache) for _s in _page]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
    )


_SEGMENT_TARGET = (
    "A 'segment' selector identifying one segment: by index, or by record_in (the record_in "
    "frame; it stays put while other segments are edited without ripple)."
)

_LIST_EFFECTS_DESCRIPTION = (
    "List the timeline effects (TimelineFX) on one segment: `effects` = [{index, type, "
    "attributes: {name: value}}], each attribute read on its own (one that cannot be read is "
    "{'error': ...} and the call still succeeds). A segment holds at most one effect of each "
    "type (Flame refuses a second one), and the list is not in the order "
    "the effects were created. Most effect types list only `bypass` as an attribute. "
    "`include_available` also returns `availableTypes`: the effect type names this segment "
    "accepts (PySegment.effect_types). " + AUTO_RESIZE_NOTE + " " + REEL_CLIP_NOTE
)

_SHARED_SOURCES_DESCRIPTION = (
    "List the segments that share the target segment's source (other sequences, copies on the "
    "Desktop, and the segments inside a reel clip; not copies in a library), "
    "one page at a time. Each item: container {type, name, "
    "selector (wiretap, null when it has none), "
    "parent {type, name}}, recordIn / recordOut / sourceIn / sourceOut (recordOut / sourceOut are "
    "the last frame, inclusive), sourceName, "
    "`isTarget`, `selector` (a 'segment' selector that addresses it) and `ambiguous`. A "
    "segment has no id, so its position is found by the container's segments that have the "
    "same record_in, record_out, source_name and source_in: exactly one gives `selector`; "
    "several (the same content on two tracks) give selector null, ambiguous true and up to 5 "
    "`candidates` {version, track, index}; `isTarget` is true only for a unique position in "
    "the target's own container. To bound the time Flame's main thread is held, only the "
    "page's containers are walked (default 20 per call, at most 100): page with offset. "
    "Scope: from a segment on the Desktop it returns the Desktop's "
    "segments of the same source (other sequences, copies on the Desktop, the inside of a reel "
    "clip) and not a copy made in a library; from a segment inside a library it returns "
    "that segment only, none of the Desktop's. The target can be the segment of a reel clip "
    "too (measured on Flame 2025.2.4): the sequences' segments of the same source, the other "
    "clips' segments and the clip itself (isTarget true) come back."
)


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)

    registry.register(
        OperationSpec(
            name="segment.info",
            category="segment",
            description=(
                "Full detail for one segment, with the same type / isGap / head / tail / effects "
                "that timeline.list_segments gives. `attributes` maps every attribute name "
                "to its value, converted as segment.get_attributes converts it (a colour is "
                "[r, g, b], an empty tag list [], a Flame object {type, name}); an attribute "
                "that cannot be read is reported as {'error': ...} without failing the call. "
                "Where its media comes from: `filePath`, `tapeName`, `sourceUid` and "
                "`sourceUnlinked` (PySegment.file_path / tape_name / source_uid / "
                "source_unlinked), returned as Flame gives them (for a segment of an imported "
                "clip sourceUid and sourceUnlinked are the empty string ''). A value is null when "
                "it cannot be read, and all four are null for a gap. "
                + AUTO_RESIZE_NOTE
                + " "
                + REEL_CLIP_NOTE
            ),
            effect="read",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A 'segment' selector identifying one segment.",
                    required=True,
                ),
            ),
            accepts=("segment",),
            to_python=_info_to_python,
            requires=(
                "PySegment.attributes",
                "PySegment.name",
                "PySegment.type",
                "PySegment.head",
                "PySegment.tail",
                "PySegment.effects",
                "PySegment.file_path",
                "PySegment.tape_name",
                "PySegment.source_uid",
                "PySegment.source_unlinked",
                "PyTimelineFX.type",
            ),
        )
    )

    register_attribute_ops(
        registry,
        category="segment",
        noun="segment",
        accepts=("segment",),
        target_description="A 'segment' selector identifying one segment. " + REEL_CLIP_NOTE,
        stable_src=_segment_stable_src,
        revert_kinds=("segment",),
        requires=attribute_requires("PySegment.attributes", "PyClip.get_wiretap_node_id"),
    )

    target_param = OperationParam(
        name="target", type="selector", description=_SEGMENT_TARGET, required=True
    )
    registry.register(
        OperationSpec(
            name="segment.list_effects",
            category="segment",
            description=_LIST_EFFECTS_DESCRIPTION,
            effect="read",
            params=(
                target_param,
                OperationParam(
                    name="include_available",
                    type="boolean",
                    description="Also return `availableTypes` (default false).",
                    required=False,
                    default=False,
                ),
            ),
            accepts=("segment",),
            to_python=_list_effects_to_python,
            requires=(
                "PySegment.effects",
                "PySegment.effect_types",
                "PyTimelineFX.type",
                "PyTimelineFX.attributes",
            ),
        )
    )
    registry.register(
        OperationSpec(
            name="segment.shared_sources",
            category="segment",
            description=_SHARED_SOURCES_DESCRIPTION,
            effect="read",
            params=(
                target_param,
                OperationParam(
                    name="limit",
                    type="number",
                    description="Max items (containers walked) per call.",
                    required=False,
                ),
                offset_param(),
            ),
            accepts=("segment",),
            default_limit=20,
            max_limit=100,
            to_python=_shared_sources_to_python,
            requires=(
                "PySegment.shared_source_segments",
                "PySegment.parent",
                "PyTrack.parent",
                "PyVersion.parent",
                "PyClip.get_wiretap_node_id",
                "PySequence.get_wiretap_node_id",
                "PyTrack.segments",
                "PyVersion.tracks",
                "PySegment.record_in",
                "PySegment.record_out",
                "PySegment.source_in",
                "PySegment.source_out",
                "PySegment.source_name",
            ),
        )
    )
