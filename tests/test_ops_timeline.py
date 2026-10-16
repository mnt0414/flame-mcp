"""`timeline.*` tests: info / list_segments,
run directly against `fake_media_tree.FakeFlame` (no bridge)."""

from __future__ import annotations

import json
from typing import Any

from fake_media_tree import (
    PySegment,
    PySequence,
    PyTimelineFX,
    PyTrack,
    PyVersion,
    build_default_tree,
)
from flame_mcp.callargs import apply_param_defaults
from flame_mcp.ops import register_all
from flame_mcp.opschema import validate_op_args
from flame_mcp.registry import Registry
from op_test_helpers import run_op


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


def _seq_target(h: dict[str, object]) -> dict[str, object]:
    seq = h["seq"]
    return {"kind": "wiretap", "id": seq.get_wiretap_node_id()}  # type: ignore[attr-defined]


def test_info_via_wiretap() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(registry, "timeline.info", {"target": _seq_target(h)}, flame)
    assert result["opError"] is None
    assert result["value"]["name"] == "Seq1"
    assert result["value"]["duration"] == {"timecode": "00:00:04+04", "frame": 100}
    assert result["value"]["frameRate"] == "23.976 fps"
    assert result["value"]["versionCount"] == 1
    assert result["value"]["versions"] == [{"trackCount": 1, "tracks": [{"segmentCount": 2}]}]
    assert result["value"]["wiretapId"] == h["seq"].get_wiretap_node_id()
    assert result["value"]["uid"] == h["seq"].uid.get_value()


def test_info_via_path() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    result = run_op(
        registry,
        "timeline.info",
        {"target": {"kind": "path", "path": ["P1O", "R1", "clip1"]}},
        flame,
    )
    assert result["opError"] is None
    assert result["value"]["name"] == "clip1"


def test_info_via_selection() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    flame.media_panel.selected_entries = [h["seq"]]
    result = run_op(registry, "timeline.info", {"target": {"kind": "selection"}}, flame)
    assert result["value"]["name"] == "Seq1"


def test_info_unknown_wiretap_is_selector_empty() -> None:
    registry = make_registry()
    flame, _h = build_default_tree()
    result = run_op(registry, "timeline.info", {"target": {"kind": "wiretap", "id": "nope"}}, flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"


def test_list_segments_default_version_and_track() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "version": 0, "track": 0, "limit": 50},
        flame,
    )
    assert result["opError"] is None
    items = result["value"]["items"]
    assert [i["name"] for i in items] == ["seg0", "seg1"]
    assert items[0]["index"] == 0
    assert items[1]["sourceName"] == "clip2"
    assert items[1]["recordIn"] == {"timecode": "00:00:00+10", "frame": 10}
    assert items[1]["recordOut"] == {"timecode": "00:00:00+19", "frame": 19}
    assert items[1]["recordDuration"] == {"timecode": "00:00:00+10", "frame": 10}
    assert result["value"]["wiretapId"] == h["seq"].get_wiretap_node_id()
    assert result["value"]["uid"] == h["seq"].uid.get_value()
    assert sorted(result["value"]) == [
        "items",
        "offset",
        "shown",
        "total",
        "truncated",
        "uid",
        "wiretapId",
    ]


def test_list_segments_omitted_version_and_track_use_declared_defaults() -> None:
    registry = make_registry()
    op = registry.get("timeline.list_segments")
    assert op is not None
    flame, h = build_default_tree()
    validated = validate_op_args(op, {"target": _seq_target(h)})
    assert validated.ok is True
    args = dict(validated.value or {})
    args["limit"] = 50
    apply_param_defaults(op, args)
    assert (args["version"], args["track"]) == (0, 0)
    result = run_op(registry, "timeline.list_segments", args, flame)
    assert result["opError"] is None
    assert [i["name"] for i in result["value"]["items"]] == ["seg0", "seg1"]


def test_list_segments_limit_truncates() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "version": 0, "track": 0, "limit": 1},
        flame,
    )
    assert result["value"]["total"] == 2
    assert result["value"]["shown"] == 1
    assert result["value"]["truncated"] is True


def test_list_segments_version_out_of_range_is_selector_empty() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "version": 5, "track": 0, "limit": 50},
        flame,
    )
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
    assert "version index 5" in result["opError"]


def test_list_segments_track_out_of_range_is_selector_empty() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "version": 0, "track": 5, "limit": 50},
        flame,
    )
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
    assert "track index 5" in result["opError"]


def test_list_segments_multiple_tracks_selects_the_right_one() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    seg_a = PySegment(name="a")
    seg_b = PySegment(name="b")
    seq = h["seq"]
    seq._versions = [PyVersion([PyTrack([seg_a]), PyTrack([seg_b])])]
    result = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "version": 0, "track": 1, "limit": 50},
        flame,
    )
    assert [i["name"] for i in result["value"]["items"]] == ["b"]


def _gap_tree() -> tuple[Any, dict[str, Any]]:
    """Default tree, but Seq1's only track is: m0 (media), gap, m2 (media), gap."""
    flame, h = build_default_tree()
    segments = [
        PySegment(name="m0", source_name="clip1", head=5, tail=7),
        PySegment(name="", source_name="", type="Gap", head="infinite", tail="infinite"),
        PySegment(name="m2", source_name="clip2", head=0, tail=3),
        PySegment(name="", source_name="", type="Gap", head="infinite", tail="infinite"),
    ]
    h["seq"]._versions = [PyVersion([PyTrack(segments)])]
    return flame, h


def test_list_segments_reports_type_and_isgap_for_gaps_and_media() -> None:
    registry = make_registry()
    flame, h = _gap_tree()
    items = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "limit": 50},
        flame,
    )["value"]["items"]
    assert [(i["type"], i["isGap"]) for i in items] == [
        ("Video Segment", False),
        ("Gap", True),
        ("Video Segment", False),
        ("Gap", True),
    ]


def test_list_segments_reports_head_and_tail_as_int_or_infinite() -> None:
    registry = make_registry()
    flame, h = _gap_tree()
    items = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "limit": 50},
        flame,
    )["value"]["items"]
    assert [(i["head"], i["tail"]) for i in items] == [
        (5, 7),
        ("infinite", "infinite"),
        (0, 3),
        ("infinite", "infinite"),
    ]
    json.dumps(items)  # every field must survive JSON encoding


def test_list_segments_isgap_follows_type_not_the_name() -> None:
    # A media segment with an empty name must not look like a gap.
    registry = make_registry()
    flame, h = build_default_tree()
    h["seq"]._versions = [PyVersion([PyTrack([PySegment(name="", source_name="")])])]
    item = run_op(
        registry, "timeline.list_segments", {"target": _seq_target(h), "limit": 50}, flame
    )["value"]["items"][0]
    assert item["name"] == ""
    assert item["isGap"] is False


def test_list_segments_offset_returns_the_second_page_with_true_indexes() -> None:
    registry = make_registry()
    flame, h = _gap_tree()
    value = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "limit": 2, "offset": 2},
        flame,
    )["value"]
    assert [i["name"] for i in value["items"]] == ["m2", ""]
    # `index` is the position in the track, so it feeds a `segment` selector directly
    assert [i["index"] for i in value["items"]] == [2, 3]
    assert (value["total"], value["offset"], value["shown"], value["truncated"]) == (4, 2, 2, False)


def test_list_segments_offset_first_page_is_truncated() -> None:
    registry = make_registry()
    flame, h = _gap_tree()
    value = run_op(
        registry,
        "timeline.list_segments",
        {"target": _seq_target(h), "limit": 2, "offset": 0},
        flame,
    )["value"]
    assert [i["index"] for i in value["items"]] == [0, 1]
    assert (value["offset"], value["truncated"]) == (0, True)


def test_list_segments_invalid_args_negative_offset() -> None:
    registry = make_registry()
    op = registry.get("timeline.list_segments")
    assert op is not None
    result = validate_op_args(op, {"target": {"kind": "selection"}, "offset": -1})
    assert result.ok is False


# timeline.info takes a sequence or a clip, not a 'segment' selector (INVALID_ARGS).


def test_info_invalid_args_rejects_segment_kind() -> None:
    registry = make_registry()
    op = registry.get("timeline.info")
    assert op is not None
    sel = {"kind": "segment", "parent": {"kind": "selection"}, "version": 0, "track": 0, "index": 0}
    result = validate_op_args(op, {"target": sel})
    assert result.ok is False


def _list(flame: Any, h: dict[str, Any], **extra: Any) -> dict[str, Any]:
    args = {"target": _seq_target(h), "limit": 50, **extra}
    value: dict[str, Any] = run_op(make_registry(), "timeline.list_segments", args, flame)["value"]
    return value


def test_list_segments_items_carry_the_effect_type_names() -> None:
    flame, h = _gap_tree()
    segs = h["seq"]._versions[0]._tracks[0]._segments
    segs[0].effects = [PyTimelineFX("Blur"), PyTimelineFX("Burn-in Letterbox")]
    segs[2].effects = [PyTimelineFX("Timewarp")]
    items = _list(flame, h)["items"]
    assert [i["effects"] for i in items] == [["Blur", "Burn-in Letterbox"], [], ["Timewarp"], []]
    json.dumps(items)


def test_list_segments_effects_of_a_segment_whose_effects_cannot_be_read_is_empty() -> None:
    class _NoFx(PySegment):
        @property
        def effects(self) -> Any:
            raise RuntimeError("a gap has none to read")

        @effects.setter
        def effects(self, _value: Any) -> None:
            pass

    flame, h = build_default_tree()
    h["seq"]._versions = [PyVersion([PyTrack([_NoFx(name="g", type="Gap")])])]
    items = _list(flame, h)["items"]
    assert items[0]["effects"] == []
    assert items[0]["isGap"] is True


def test_list_segments_effects_only_hold_the_type_names_so_the_page_stays_small() -> None:
    flame, h = _gap_tree()
    h["seq"]._versions[0]._tracks[0]._segments[0].effects = [
        PyTimelineFX("Burn-in Letterbox", {"mode": "x" * 5000})
    ]
    assert len(json.dumps(_list(flame, h))) < 3000


# timeline.info reads the start, the size and the audio.


def _timeline_info(flame: Any, h: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = run_op(
        make_registry(), "timeline.info", {"target": _seq_target(h)}, flame
    )["value"]
    return info


def test_timeline_info_reads_start_time_start_frame_size_and_audio_tracks() -> None:
    from fake_media_tree import PyTime

    flame, h = build_default_tree()
    seq = h["seq"]
    seq.start_time.set_value(PyTime(86400))
    seq.start_frame = 86401
    seq.audio_tracks = [object(), object()]
    value = _timeline_info(flame, h)
    assert value["startTime"] == {"timecode": "01:00:00+00", "frame": 86400}
    assert (value["startFrame"], value["width"], value["height"]) == (86401, 1920, 1080)
    assert value["audioTracks"] == 2


def test_timeline_info_keeps_the_keys_it_had() -> None:
    flame, h = build_default_tree()
    value = _timeline_info(flame, h)
    assert {"name", "duration", "frameRate", "versionCount", "versions"} <= set(value)
    assert value["startTime"] == {"timecode": "00:00:00+00", "frame": 0}
    assert value["audioTracks"] == 0
    assert value["versions"] == [{"trackCount": 1, "tracks": [{"segmentCount": 2}]}]
    json.dumps(value)


def test_timeline_info_reads_each_new_item_on_its_own() -> None:
    class _Flaky(PySequence):
        @property
        def width(self) -> Any:
            raise RuntimeError("no width")

        @width.setter
        def width(self, _value: Any) -> None:
            pass

        @property
        def audio_tracks(self) -> Any:
            raise RuntimeError("no audio")

        @audio_tracks.setter
        def audio_tracks(self, _value: Any) -> None:
            pass

    flame, _h = build_default_tree()
    flaky = _Flaky("F", wiretap_id="/wt/F")
    flame._wiretap_index["/wt/F"] = flaky
    result = run_op(
        make_registry(), "timeline.info", {"target": {"kind": "wiretap", "id": "/wt/F"}}, flame
    )
    assert result["opError"] is None
    value = result["value"]
    assert value["width"] == {"error": "no width"}
    assert value["audioTracks"] == {"error": "no audio"}
    assert value["height"] == 1080 and value["startFrame"] == 1
    assert value["name"] == "F"


def test_timeline_info_works_on_a_clip_too() -> None:
    flame, h = build_default_tree()
    result = run_op(
        make_registry(),
        "timeline.info",
        {"target": {"kind": "wiretap", "id": h["clip1"].get_wiretap_node_id()}},
        flame,
    )
    assert result["value"]["startFrame"] == 1 and result["value"]["audioTracks"] == 0
