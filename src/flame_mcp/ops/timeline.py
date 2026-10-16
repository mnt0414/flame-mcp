"""`timeline` category: sequence/clip info and listing a track's segments
(one segment's detail lives in `segment.py`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.ops._common import AUTO_RESIZE_NOTE, build_source, list_result_src, offset_param
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationParam, OperationSpec, SelectorKind

CATEGORY = Category(
    name="timeline",
    description="Sequence/clip structure: versions, tracks, and segments.",
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)

# desktop's target param kinds (wiretap / path / selection) plus "timeline".
# "wiretap" stays first: the registry's registration-time to_python smoke test
# samples only accepts[0], and that kind exercises the loop-free branch.
_SEQUENCE_TARGET_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection", "timeline")


def _resolved_identity_src() -> list[str]:
    return [
        "_entry = _describe_entry(target)",
        "_result['uid'] = _entry['uid']",
        "_result['wiretapId'] = _entry['wiretapId']",
    ]


def _info_to_python(args: Mapping[str, Any]) -> str:
    target_sel = args["target"]
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        "\n".join(
            [
                "_versions = []",
                "for _ver in target.versions:",
                "    _tracks = []",
                "    for _trk in _ver.tracks:",
                "        _tracks.append({'segmentCount': len(list(_trk.segments))})",
                "    _versions.append({'trackCount': len(_tracks), 'tracks': _tracks})",
                "_result = {",
                "    'name': _v(target.name),",
                "    'duration': _j(target.duration),",
                "    'frameRate': _j(target.frame_rate),",
                "    'versionCount': len(_versions),",
                "    'versions': _versions,",
                "}",
                "_result.update(_rp_media_basics(target))",
                *_resolved_identity_src(),
            ]
        ),
    )


def _segment_item_src() -> str:
    return "\n".join(
        [
            "def _describe_segment(_i, _seg):",
            "    return {",
            "        'index': _i,",
            "        'name': _v(_seg.name),",
            "        'sourceName': _v(_seg.source_name),",
            "        'recordIn': _j(_seg.record_in),",
            "        'recordOut': _j(_seg.record_out),",
            "        'sourceIn': _j(_seg.source_in),",
            "        'sourceOut': _j(_seg.source_out),",
            "        'recordDuration': _j(_seg.record_duration),",
            "        **_rp_segment_facts(_seg),",
            "    }",
        ]
    )


def _list_segments_to_python(args: Mapping[str, Any]) -> str:
    target_sel = args["target"]
    version = args["version"]
    track = args["track"]
    limit = args["limit"]
    offset = args["offset"]
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        _segment_item_src(),
        "\n".join(
            [
                f"_version_idx = {version!r}",
                f"_track_idx = {track!r}",
                "_versions = list(target.versions)",
                "if _version_idx >= len(_versions):",
                "    raise _OpError('SELECTOR_EMPTY', 'version index ' + str(_version_idx) "
                "+ ' out of range (have ' + str(len(_versions)) + ')')",
                "_tracks = list(_versions[_version_idx].tracks)",
                "if _track_idx >= len(_tracks):",
                "    raise _OpError('SELECTOR_EMPTY', 'track index ' + str(_track_idx) "
                "+ ' out of range (have ' + str(len(_tracks)) + ')')",
                "_segments = list(_tracks[_track_idx].segments)",
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_segments)",
                "_items = [",
                "    _describe_segment(_i, _s)",
                "    for _i, _s in enumerate(_segments[_offset : _offset + _limit], start=_offset)",
                "]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
        "\n".join(_resolved_identity_src()),
    )


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)

    target_param = OperationParam(
        name="target",
        type="selector",
        description=(
            "wiretap / path / selection / timeline selector for the sequence or clip "
            "(timeline: the sequence the Timeline is showing)."
        ),
        required=True,
    )
    limit_param = OperationParam(
        name="limit", type="number", description="Max segments to return.", required=False
    )

    registry.register(
        OperationSpec(
            name="timeline.info",
            category="timeline",
            description=(
                "Name/duration/frame rate and version/track/segment counts, plus startTime (a "
                "{timecode, frame} PyTime; its frame is what record_in values count from), "
                "startFrame, width, height and audioTracks (the number of audio tracks); each "
                "of these is read on its own, so one that cannot be read is {'error': ...} "
                "and the rest still come back. "
                "The result's uid / wiretapId are those of the sequence that was read; to read "
                "or edit that same sequence again, point at it with "
                "{kind: 'wiretap', id: <wiretapId>}, because a timeline selector reads what the "
                "Timeline shows at each call."
            ),
            effect="read",
            params=(target_param,),
            accepts=_SEQUENCE_TARGET_ACCEPTS,
            to_python=_info_to_python,
            requires=(
                "PySequence.versions",
                "PyVersion.tracks",
                "PyTrack.segments",
                "PySequence.duration",
                "PySequence.frame_rate",
                "PySequence.name",
                "PySequence.start_time",
                "PySequence.start_frame",
                "PySequence.width",
                "PySequence.height",
                "PySequence.audio_tracks",
            ),
        )
    )

    registry.register(
        OperationSpec(
            name="timeline.list_segments",
            category="timeline",
            description=(
                "List the segments of one (version, track) pair, one page at a time "
                "(`offset` skips segments; each item's `index` is its position in the "
                "track). Each item carries `type` (Flame's segment type, e.g. 'Gap' or "
                "'Video Segment'), `isGap`, and `head` / `tail` (handle frames as an "
                "integer, or the string 'infinite' for a gap) and `effects` (the type names of "
                "its timeline effects, [] for none or when they cannot be read; "
                "segment.list_effects has the attributes). " + AUTO_RESIZE_NOTE + " "
                "The result's uid / wiretapId are those of the sequence that was read; to read "
                "or edit that same sequence again, point at it with "
                "{kind: 'wiretap', id: <wiretapId>}, because a timeline selector reads what the "
                "Timeline shows at each call."
            ),
            effect="read",
            params=(
                target_param,
                OperationParam(
                    name="version",
                    type="number",
                    description="Version index (0-based).",
                    required=False,
                    default=0,
                ),
                OperationParam(
                    name="track",
                    type="number",
                    description="Track index within the version (0-based).",
                    required=False,
                    default=0,
                ),
                limit_param,
                offset_param(),
            ),
            accepts=_SEQUENCE_TARGET_ACCEPTS,
            to_python=_list_segments_to_python,
            requires=(
                "PySegment.record_in",
                "PySegment.record_out",
                "PySegment.source_in",
                "PySegment.source_out",
                "PySegment.record_duration",
                "PySegment.source_name",
                "PySegment.type",
                "PySegment.head",
                "PySegment.tail",
                "PySegment.effects",
                "PyTimelineFX.type",
            ),
        )
    )
