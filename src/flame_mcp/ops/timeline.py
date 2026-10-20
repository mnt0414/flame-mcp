"""`timeline` category: sequence/clip info and listing a track's segments
(one segment's detail lives in `segment.py`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.ops._common import AUTO_RESIZE_NOTE, build_source, list_result_src, offset_param
from flame_mcp.registry import Registry
from flame_mcp.types import Category, OperationParam, OperationSpec, Refusal, SelectorKind

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
_COMPARE_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")
DEFAULT_COMPARE_LIMIT: Final[int] = 50


def _resolved_identity_src() -> list[str]:
    return [
        "_entry = _describe_entry(target)",
        "_result['uid'] = _entry['uid']",
        "_result['wiretapId'] = _entry['wiretapId']",
    ]


def _audio_info_src() -> str:
    return "\n".join(
        [
            "def _audio_track_info(_atrk):",
            "    _chans = list(_atrk.channels)",
            "    return {",
            "        'channelCount': len(_chans),",
            "        'channels': [",
            "            _rp_try(lambda _c=_c: {'segmentCount': len(list(_c.segments))})",
            "            for _c in _chans",
            "        ],",
            "    }",
        ]
    )


def _info_to_python(args: Mapping[str, Any]) -> str:
    target_sel = args["target"]
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        _audio_info_src(),
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
                "_result['audio'] = _rp_try(lambda: [",
                "    _rp_try(lambda _atrk=_atrk: _audio_track_info(_atrk))",
                "    for _atrk in list(target.audio_tracks)",
                "])",
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


def _audio_segment_item_src() -> str:
    # The audio segment of a channel: type, source_name and the record / source frames are
    # measured readable (Flame 2025.2.4); name and record_duration are read on their own, and
    # head, tail and effects are not read (null).
    return "\n".join(
        [
            "def _describe_audio_segment(_i, _seg):",
            "    _kind = _j(_seg.type)",
            "    return {",
            "        'index': _i,",
            "        'name': _rp_try(lambda: _v(_seg.name)),",
            "        'sourceName': _v(_seg.source_name),",
            "        'recordIn': _j(_seg.record_in),",
            "        'recordOut': _j(_seg.record_out),",
            "        'sourceIn': _j(_seg.source_in),",
            "        'sourceOut': _j(_seg.source_out),",
            "        'recordDuration': _rp_try(lambda: _j(_seg.record_duration)),",
            "        'type': _kind,",
            "        'isGap': _kind == 'Gap',",
            "        'head': None,",
            "        'tail': None,",
            "        'effects': None,",
            "    }",
        ]
    )


def _video_pick_src(version: int, track: int) -> list[str]:
    return [
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
        "_describe = _describe_segment",
    ]


def _audio_pick_src(track: int, channel: int) -> list[str]:
    return [
        f"_track_idx = {track!r}",
        f"_channel_idx = {channel!r}",
        "_atracks = list(target.audio_tracks)",
        "if _track_idx >= len(_atracks):",
        "    raise _OpError('SELECTOR_EMPTY', 'audio track index ' + str(_track_idx) "
        "+ ' out of range (have ' + str(len(_atracks)) + ')')",
        "_channels = list(_atracks[_track_idx].channels)",
        "if _channel_idx >= len(_channels):",
        "    raise _OpError('SELECTOR_EMPTY', 'audio channel index ' + str(_channel_idx) "
        "+ ' out of range (have ' + str(len(_channels)) + ')')",
        "_segments = list(_channels[_channel_idx].segments)",
        "_describe = _describe_audio_segment",
    ]


def _list_segments_to_python(args: Mapping[str, Any]) -> str:
    target_sel = args["target"]
    track = args["track"]
    limit = args["limit"]
    offset = args["offset"]
    if args.get("media", "video") == "audio":
        pick = _audio_pick_src(track, args.get("channel", 0))
    else:
        pick = _video_pick_src(args["version"], track)
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        _segment_item_src(),
        _audio_segment_item_src(),
        "\n".join(
            [
                *pick,
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_segments)",
                "_items = [",
                "    _describe(_i, _s)",
                "    for _i, _s in enumerate(_segments[_offset : _offset + _limit], start=_offset)",
                "]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
        "\n".join(_resolved_identity_src()),
    )


def _compare_to_python(args: Mapping[str, Any]) -> str:
    limit = int(args.get("limit", DEFAULT_COMPARE_LIMIT))
    return build_source(
        selectors.resolve_code(args["a"], "seq_a"),
        selectors.resolve_code(args["b"], "seq_b"),
        "_read_a = _tc_read(seq_a, 'a')",
        "_read_b = _tc_read(seq_b, 'b')",
        f"_result = _tc_compare(_read_a, _read_b, {limit!r})",
    )


def _list_segments_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    if "channel" in args and args.get("media", "video") != "audio":
        return Refusal(
            "INVALID_ARGS",
            "channel only applies to media='audio' (a video track has no channels)",
        )
    return None


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
                "and the rest still come back. `audio` lists the audio tracks as "
                "[{channelCount, channels: [{segmentCount}]}] (the segments of an audio track "
                "are reachable only through its channels), each track and each channel read on "
                "its own: one that cannot be read is {'error': ...}; the whole list is "
                "{'error': ...} when the audio tracks cannot be read. "
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
                "PyAudioTrack.channels",
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
                "segment.list_effects has the attributes). "
                "media='audio' lists the segments of one audio channel instead: `track` is the "
                "index in PySequence.audio_tracks and `channel` the index of the channel "
                "(default 0) in that track's channels -- the only way Flame lists an audio "
                "segment; `version` is not used. The items have the same keys "
                "(index, name, sourceName, recordIn, recordOut, sourceIn, sourceOut, "
                "recordDuration, type, isGap) and recordIn.frame etc. count the same way; "
                "head, tail and effects are not read for audio and are null. (Measured on Flame "
                "2025.2.4: type, source_name, record_in / record_out and source_in / source_out "
                "can be read on an Audio Segment; name and record_duration are read on their own, "
                "{'error': ...} when they cannot be read.) A track or "
                "channel index that does not exist is SELECTOR_EMPTY; `channel` with "
                "media='video' is INVALID_ARGS. There is no selector for an audio segment: the "
                "segment.* ops and the segment selector work on video segments only. "
                + AUTO_RESIZE_NOTE
                + " "
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
                    description=(
                        "Track index (0-based): within the version for media='video', in "
                        "PySequence.audio_tracks for media='audio'."
                    ),
                    required=False,
                    default=0,
                ),
                OperationParam(
                    name="media",
                    type="string",
                    description=(
                        "'video' (default): a video track of `version`. 'audio': one channel of "
                        "an audio track."
                    ),
                    required=False,
                    default="video",
                    enum=("video", "audio"),
                ),
                OperationParam(
                    name="channel",
                    type="number",
                    description=(
                        "media='audio' only: index of the channel within the audio track "
                        "(0-based, default 0)."
                    ),
                    required=False,
                    integer=True,
                    minimum=0,
                ),
                limit_param,
                offset_param(),
            ),
            accepts=_SEQUENCE_TARGET_ACCEPTS,
            to_python=_list_segments_to_python,
            precheck=_list_segments_precheck,
            requires=(
                "PySequence.audio_tracks",
                "PyAudioTrack.channels",
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

    registry.register(
        OperationSpec(
            name="timeline.compare",
            category="timeline",
            description=(
                "Compare the edit of two sequences (or clips) and list where they differ. "
                "Compared: the length, the version count, the frame rate and the size "
                "(frame_rate, width, height; one that cannot be read is a warning and is left "
                "out), the number of video tracks of version 0 "
                "and every segment of each (type, record range, source range, source_name, name "
                "and the type names of its timeline effects), the number of audio tracks, the "
                "channels of each and every segment of each channel (type, record range, source "
                "range, source_name; the audio is read through PySequence.audio_tracks[]."
                "channels[].segments, the only way Flame lists it). Record frames are counted "
                "from each sequence's own start (start_time.frame), so the same edit at another "
                "start timecode is equal; the source frames are compared as they are. "
                "Two Gaps are compared by type, record range and effect types only: their "
                "source range and name change without the edit changing (measured on Flame "
                "2025.2.4: the same edit in two sequences gave source 177-192 "
                "against 90-105 and name '' against 'untitled', and a cut Gap counts its "
                "source_in from 0 again and loses its name). A Gap against a media segment is "
                "still a difference. "
                "NOT compared: the parameters of effects (only their types), markers, colours, "
                "transitions, segment connections, the tracks of versions other than 0, handles "
                "(head / tail). "
                "result: equal, differences [{where, field, a, b}] (at most `limit`, default 50; "
                "`where` counts from 0 like timeline.list_segments: 'sequence', 'video track 2', "
                "'video track 2 segment 3', 'audio track 0 channel 1 segment 0'; field is one of "
                "duration, versionCount, videoTrackCount, audioTrackCount, frameRate, width, "
                "height, channelCount, "
                "segmentCount, presence (a track, channel or segment only one side has: 'absent' "
                "on the other), type, recordIn, recordOut, sourceIn, sourceOut, sourceName, name, "
                "effects; recordIn / recordOut are relative to the start of that sequence), "
                "differenceCount (all of them), truncated, summary [{where, equal, "
                "differences}] (the sequence, each video track, each audio track and channel), "
                "emptyTracks {a: [...], b: [...]} (the indexes of the video tracks that hold "
                "nothing but Gaps), a / b {name, uid, wiretapId, startFrame, duration, "
                "versionCount, videoTrackCount, audioTrackCount, frameRate, width, height}, "
                "warnings (a start_time that is not the first record_in of the video tracks; an "
                "audio channel "
                "whose first record_in is not start_time; a frame_rate, width or height that "
                "cannot be read). "
                "A track that only one sequence "
                "has is a difference. a and b are read now, each by its own selector (wiretap / "
                "path / selection; not 'timeline', which would read the sequence the Timeline "
                "shows for both: point at it with the wiretapId that timeline.info gives)."
            ),
            effect="read",
            params=(
                OperationParam(
                    name="a",
                    type="selector",
                    description="First sequence or clip (wiretap / path / selection selector).",
                    required=True,
                ),
                OperationParam(
                    name="b",
                    type="selector",
                    description="Second sequence or clip (wiretap / path / selection selector).",
                    required=True,
                ),
                OperationParam(
                    name="limit",
                    type="number",
                    description=(
                        f"Max differences to list (default {DEFAULT_COMPARE_LIMIT}); "
                        "differenceCount counts all of them."
                    ),
                    required=False,
                    integer=True,
                    minimum=1,
                ),
            ),
            accepts=_COMPARE_ACCEPTS,
            to_python=_compare_to_python,
            sample_args={
                "a": {"kind": "wiretap", "id": "sample-a"},
                "b": {"kind": "wiretap", "id": "sample-b"},
            },
            requires=(
                "PySequence.versions",
                "PyVersion.tracks",
                "PyTrack.segments",
                "PySequence.duration",
                "PySequence.name",
                "PySequence.start_time",
                "PySequence.frame_rate",
                "PySequence.width",
                "PySequence.height",
                "PySequence.audio_tracks",
                "PyAudioTrack.channels",
                "PySegment.record_in",
                "PySegment.record_out",
                "PySegment.source_in",
                "PySegment.source_out",
                "PySegment.source_name",
                "PySegment.type",
                "PySegment.name",
                "PySegment.effects",
                "PyTimelineFX.type",
            ),
        )
    )
