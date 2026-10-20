"""`segment.*` tests: info, run directly against `fake_media_tree.FakeFlame`
(no bridge)."""

from __future__ import annotations

import json
from typing import Any

from fake_media_tree import (
    EFFECT_TYPES,
    FakeAttr,
    PyClip,
    PySegment,
    PySequence,
    PyTimelineFX,
    PyTrack,
    PyVersion,
    build_default_tree,
    share_sources,
)
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


def _segment_selector(h: dict[str, object], index: int) -> dict[str, object]:
    return {
        "kind": "segment",
        "parent": _seq_target(h),
        "version": 0,
        "track": 0,
        "index": index,
    }


def test_segment_info_returns_full_detail() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(registry, "segment.info", {"target": _segment_selector(h, 1)}, flame)
    assert result["opError"] is None
    value = result["value"]
    assert value["name"] == "seg1"
    assert value["sourceName"] == "clip2"
    assert value["sourceWidth"] == 1920
    assert value["sourceHeight"] == 1080
    assert value["sourceFrameRate"] == "23.976 fps"
    assert value["attributes"] == {
        "name": "seg1",
        "colour": [0.0, 0.0, 0.0],
        "tags": [],
        "selected_markers": [],
    }


def test_segment_info_index_out_of_range_is_selector_empty() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(registry, "segment.info", {"target": _segment_selector(h, 99)}, flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
    assert "segment index 99" in result["opError"]


def test_segment_info_via_selection_parent() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    flame.media_panel.selected_entries = [h["seq"]]
    sel = {"kind": "segment", "parent": {"kind": "selection"}, "version": 0, "track": 0, "index": 0}
    result = run_op(registry, "segment.info", {"target": sel}, flame)
    assert result["value"]["name"] == "seg0"


# segment.info accepts only a 'segment' selector; anything else is INVALID_ARGS.


def test_segment_info_invalid_args_rejects_non_segment_kind() -> None:
    registry = make_registry()
    op = registry.get("segment.info")
    assert op is not None
    result = validate_op_args(op, {"target": {"kind": "wiretap", "id": "x"}})
    assert result.ok is False
    assert any("not accepted" in issue.message for issue in result.issues)


def test_segment_info_invalid_args_rejects_wrong_shape() -> None:
    registry = make_registry()
    op = registry.get("segment.info")
    assert op is not None
    result = validate_op_args(op, {"target": {"kind": "segment", "parent": {"kind": "selection"}}})
    assert result.ok is False


# Attributes are {key: value}; one bad key must not sink the call.


class _FlakySegment(PySegment):
    """Lists 'broken' among its attributes, but reading it raises."""

    def __init__(self) -> None:
        super().__init__(name="flaky")
        self.attributes = ["name", "broken", "shot_name"]
        self.shot_name = FakeAttr("SH010")

    @property
    def broken(self) -> Any:
        raise RuntimeError("boom")


def _flaky_selector(h: dict[str, object]) -> dict[str, object]:
    h["seq"]._versions = [PyVersion([PyTrack([_FlakySegment()])])]  # type: ignore[attr-defined]
    return {"kind": "segment", "parent": _seq_target(h), "version": 0, "track": 0, "index": 0}


def test_segment_info_attributes_is_a_dict_of_values() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(registry, "segment.info", {"target": _segment_selector(h, 0)}, flame)
    attributes = result["value"]["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["name"] == "seg0"
    assert isinstance(attributes["name"], str)


def test_segment_info_attribute_error_is_reported_per_key() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(registry, "segment.info", {"target": _flaky_selector(h)}, flame)
    assert result["opError"] is None
    attributes = result["value"]["attributes"]
    assert attributes["broken"] == {"error": "boom"}
    # the keys around the failing one are still read
    assert attributes["name"] == "flaky"
    assert attributes["shot_name"] == "SH010"


def test_segment_info_attribute_values_are_json_safe() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    seg = h["seg0"]
    seg.attributes = ["name", "comment", "record_in"]
    seg.comment = FakeAttr({"not": "a scalar"})  # falls back to repr(), never raw objects
    result = run_op(registry, "segment.info", {"target": _segment_selector(h, 0)}, flame)
    json.dumps(result["value"]["attributes"])
    assert result["value"]["attributes"]["comment"] == repr({"not": "a scalar"})


def test_segment_info_unreadable_attribute_list_gives_empty_dict() -> None:
    registry = make_registry()
    flame, h = build_default_tree()

    class _NoAttrs(PySegment):
        @property
        def attributes(self) -> Any:
            raise RuntimeError("no attribute list")

        @attributes.setter
        def attributes(self, _value: Any) -> None:
            pass

    h["seq"]._versions = [PyVersion([PyTrack([_NoAttrs(name="x")])])]
    result = run_op(registry, "segment.info", {"target": _segment_selector(h, 0)}, flame)
    assert result["opError"] is None
    assert result["value"]["attributes"] == {}


def _track_world(record_ins: list[int], *, start: int = 0) -> tuple[Any, dict[str, Any]]:
    """Default tree whose Seq1 holds one track of 24-frame segments at `record_ins` (each
    `start` frames later) -- seg<n> sourced from clip<n>."""
    flame, h = build_default_tree()
    segments = [
        PySegment(
            name=f"seg{n}",
            source_name=f"clip{n}",
            record_in=start + rec,
            record_out=start + rec + 23,
            record_duration=24,
        )
        for n, rec in enumerate(record_ins)
    ]
    h["seq"]._versions = [PyVersion([PyTrack(segments)])]
    return flame, h


def _at(h: dict[str, object], record_in: int, **extra: object) -> dict[str, object]:
    return {
        "kind": "segment",
        "parent": _seq_target(h),
        "version": 0,
        "track": 0,
        "record_in": record_in,
        **extra,
    }


def test_record_in_selector_resolves_the_same_segment_as_index() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    by_index = run_op(registry, "segment.info", {"target": _segment_selector(h, 1)}, flame)
    by_record_in = run_op(registry, "segment.info", {"target": _at(h, 10)}, flame)
    assert by_record_in["opError"] is None
    assert by_record_in["value"] == by_index["value"]
    assert by_record_in["value"]["sourceName"] == "clip2"


def test_record_in_is_the_absolute_frame_of_a_sequence_that_starts_at_one_hour() -> None:
    registry = make_registry()
    flame, h = _track_world([0, 24], start=86400)
    result = run_op(registry, "segment.info", {"target": _at(h, 86424)}, flame)
    assert result["opError"] is None
    assert result["value"]["sourceName"] == "clip1"
    assert result["value"]["recordIn"]["frame"] == 86424
    # the first segment is NOT at record_in 0 there
    missing = run_op(registry, "segment.info", {"target": _at(h, 0)}, flame)
    assert missing["opErrorCode"] == "SELECTOR_EMPTY"


def test_record_in_not_found_lists_the_five_nearest_record_ins_closest_first() -> None:
    registry = make_registry()
    flame, h = _track_world([0, 24, 48, 72, 96, 120, 144])
    result = run_op(registry, "segment.info", {"target": _at(h, 50)}, flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
    assert "no segment with record_in 50" in result["opError"]
    assert "[48, 72, 24, 96, 0]" in result["opError"]
    assert "144" not in result["opError"]


def test_record_in_not_found_breaks_a_tie_by_the_lower_frame() -> None:
    registry = make_registry()
    flame, h = _track_world([24, 48])
    result = run_op(registry, "segment.info", {"target": _at(h, 36)}, flame)
    assert "[24, 48]" in result["opError"]


def test_record_in_not_found_in_a_one_segment_track_lists_that_one() -> None:
    registry = make_registry()
    flame, h = _track_world([0])
    result = run_op(registry, "segment.info", {"target": _at(h, 7)}, flame)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
    assert "[0]" in result["opError"]


def test_record_in_is_matched_by_frame_not_by_position() -> None:
    registry = make_registry()
    flame, h = _track_world([0, 24, 48])
    # index 1 is record_in 24; record_in 1 is not index 1
    assert (
        run_op(registry, "segment.info", {"target": _at(h, 24)}, flame)["value"]["sourceName"]
        == "clip1"
    )
    assert (
        run_op(registry, "segment.info", {"target": _at(h, 1)}, flame)["opErrorCode"]
        == "SELECTOR_EMPTY"
    )


def test_record_in_version_and_track_out_of_range_keep_their_messages() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    bad_version = {**_at(h, 0), "version": 3}
    bad_track = {**_at(h, 0), "track": 4}
    version = run_op(registry, "segment.info", {"target": bad_version}, flame)
    track = run_op(registry, "segment.info", {"target": bad_track}, flame)
    assert (version["opErrorCode"], track["opErrorCode"]) == ("SELECTOR_EMPTY", "SELECTOR_EMPTY")
    assert "version index 3" in version["opError"] and "track index 4" in track["opError"]


def test_two_segments_with_one_record_in_in_a_track_are_ambiguous_not_guessed() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    twin = PySegment(name="twin", source_name="clip9", record_in=10, record_out=19)
    h["seq"]._versions = [PyVersion([PyTrack([h["seg0"], h["seg1"], twin])])]
    result = run_op(registry, "segment.info", {"target": _at(h, 10)}, flame)
    assert result["opErrorCode"] == "SELECTOR_AMBIGUOUS"
    assert "ambiguous" in result["opError"]


def test_record_in_works_with_a_selection_parent_and_with_a_clip_parent() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    flame.media_panel.selected_entries = [h["seq"]]
    selection = {
        "kind": "segment",
        "parent": {"kind": "selection"},
        "version": 0,
        "track": 0,
        "record_in": 10,
    }
    assert run_op(registry, "segment.info", {"target": selection}, flame)["value"]["name"] == (
        "seg1"
    )
    inner = PySegment(name="inner", source_name="clip1", record_in=0, record_out=23)
    clip = h["clip1"]
    clip._versions = [PyVersion([PyTrack([inner])])]
    in_clip = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": clip.get_wiretap_node_id()},
        "version": 0,
        "track": 0,
        "record_in": 0,
    }
    assert run_op(registry, "segment.info", {"target": in_clip}, flame)["value"]["name"] == "inner"


def test_the_catalog_says_a_reel_clip_can_hold_the_segment_and_that_trim_works_on_it() -> None:
    """An AI reads only the catalog: a text that names the sequence alone made one conclude that
    a clip cannot be trimmed. Both texts must name the clip."""
    from flame_mcp.selectors import describe_kind

    parent = describe_kind("segment")["fields"]["parent"]["description"]
    assert "reel clip" in parent
    registry = make_registry()
    trim = registry.get("segment.trim")
    assert trim is not None
    assert "reel clip" in trim.description and "trims the clip itself" in trim.description
    measured = (
        "info",
        "list_effects",
        "shared_sources",
        "slip",
        "create_effect",
        "delete_effect",
        "set_effect",
        "effect_load_setup",
        "effect_save_setup",
        "copy_to_media_panel",
        "match",
    )
    for name in measured:
        op = registry.get(f"segment.{name}")
        assert op is not None and "reel clip" in op.description, name
    for name in ("get_attributes", "set_attributes"):
        op = registry.get(f"segment.{name}")
        assert op is not None
        assert "reel clip" in " ".join(p.description for p in op.params), name
    replace = registry.get("segment.replace")
    assert replace is not None
    assert "Invalid source_clip" in replace.description
    assert "contains" in replace.description and "1-frame clip" in replace.description


def test_a_selector_with_both_index_and_record_in_or_neither_is_invalid() -> None:
    registry = make_registry()
    op = registry.get("segment.info")
    assert op is not None
    _flame, h = build_default_tree()
    both = {**_segment_selector(h, 0), "record_in": 0}
    neither = {k: v for k, v in _segment_selector(h, 0).items() if k != "index"}
    for bad in (both, neither):
        result = validate_op_args(op, {"target": bad})
        assert result.ok is False
        assert any("exactly one of index / record_in" in i.message for i in result.issues)
    ok = validate_op_args(op, {"target": _at(h, 3, expect_source_name="clip1")})
    assert ok.ok is True


def test_expect_source_name_mismatch_is_a_conflict_that_names_the_actual_source() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    for target in (
        _at(h, 10, expect_source_name="clip1"),
        {**_segment_selector(h, 1), "expect_source_name": "clip1"},
    ):
        result = run_op(registry, "segment.info", {"target": target}, flame)
        assert result["opErrorCode"] == "CONFLICT"
        assert "'clip2'" in result["opError"] and "'clip1'" in result["opError"]
        assert "nothing was done" in result["opError"]


def test_expect_source_name_that_matches_passes() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    for target in (
        _at(h, 10, expect_source_name="clip2"),
        {**_segment_selector(h, 1), "expect_source_name": "clip2"},
    ):
        result = run_op(registry, "segment.info", {"target": target}, flame)
        assert result["opError"] is None
        assert result["value"]["sourceName"] == "clip2"


def test_a_conflict_on_expect_source_name_writes_nothing() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    seg = h["seg1"]
    seg.comment = FakeAttr("old")
    seg.attributes.append("comment")
    target = _at(h, 10, expect_source_name="clip1")
    result = run_op(
        registry, "segment.set_attributes", {"target": target, "values": {"comment": "x"}}, flame
    )
    assert result["opErrorCode"] == "CONFLICT"
    assert seg.comment.get_value() == "old"


# The address a revert uses keeps the form it was given.


def _commented_world() -> tuple[Any, dict[str, Any]]:
    flame, h = build_default_tree()
    seg = h["seg1"]
    seg.comment = FakeAttr("old")
    seg.attributes.append("comment")
    return flame, h


def test_set_attributes_by_record_in_reverts_through_a_record_in_address() -> None:
    registry = make_registry()
    flame, h = _commented_world()
    target = _at(h, 10)
    result = run_op(
        registry, "segment.set_attributes", {"target": target, "values": {"comment": "x"}}, flame
    )
    assert result["opError"] is None
    revert = result["value"]["revert"]
    assert revert["args"]["target"] == target
    assert "index" not in revert["args"]["target"]
    back = run_op(registry, revert["op"], revert["args"], flame)
    assert back["opError"] is None
    assert h["seg1"].comment.get_value() == "old"


def test_the_revert_address_keeps_expect_source_name_and_the_index_form() -> None:
    registry = make_registry()
    flame, h = _commented_world()
    for target in (
        _at(h, 10, expect_source_name="clip2"),
        {**_segment_selector(h, 1), "expect_source_name": "clip2"},
        _segment_selector(h, 1),
    ):
        result = run_op(
            registry,
            "segment.set_attributes",
            {"target": target, "values": {"comment": "y"}},
            flame,
        )
        assert result["value"]["revert"]["args"]["target"] == target
        h["seg1"].comment.set_value("old")


def test_get_attributes_returns_a_stable_address_in_the_given_form() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    by_record_in = run_op(registry, "segment.get_attributes", {"target": _at(h, 10)}, flame)
    assert by_record_in["value"]["stable"] == _at(h, 10)
    by_index = run_op(
        registry, "segment.get_attributes", {"target": _segment_selector(h, 1)}, flame
    )
    assert by_index["value"]["stable"] == _segment_selector(h, 1)


def _fx_selector(h: dict[str, object], index: int = 0) -> dict[str, object]:
    return _segment_selector(h, index)


def test_list_effects_reports_index_type_and_every_attribute() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    blur = PyTimelineFX("Blur")
    letterbox = PyTimelineFX(
        "Burn-in Letterbox",
        {"mode": "2.35:1 (CinemaScope)", "ratio": 2.35, "colour": (0.0, 0.0, 0.0)},
    )
    h["seg0"].effects = [blur, letterbox]
    result = run_op(registry, "segment.list_effects", {"target": _fx_selector(h)}, flame)
    assert result["opError"] is None
    assert result["value"] == {
        "effects": [
            {"index": 0, "type": "Blur", "attributes": {"bypass": False}},
            {
                "index": 1,
                "type": "Burn-in Letterbox",
                "attributes": {
                    "bypass": False,
                    "mode": "2.35:1 (CinemaScope)",
                    "ratio": 2.35,
                    "colour": [0.0, 0.0, 0.0],
                },
            },
        ]
    }
    json.dumps(result["value"])


def test_list_effects_keeps_the_order_flame_answers() -> None:
    # Measured on Flame 2025.2.4: effects come back in processing order, not creation order
    # (a Resize made after a Blur sits before it); the op does not sort.
    registry = make_registry()
    flame, h = build_default_tree()
    h["seg0"].effects = [PyTimelineFX("Resize"), PyTimelineFX("Blur"), PyTimelineFX("Action")]
    result = run_op(registry, "segment.list_effects", {"target": _fx_selector(h)}, flame)
    assert [e["type"] for e in result["value"]["effects"]] == ["Resize", "Blur", "Action"]
    assert [e["index"] for e in result["value"]["effects"]] == [0, 1, 2]


def test_list_effects_of_a_segment_without_effects_is_empty() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    result = run_op(registry, "segment.list_effects", {"target": _fx_selector(h)}, flame)
    assert result["value"] == {"effects": []}


def test_list_effects_available_types_only_when_asked_for() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    plain = run_op(registry, "segment.list_effects", {"target": _fx_selector(h)}, flame)
    assert "availableTypes" not in plain["value"]
    asked = run_op(
        registry,
        "segment.list_effects",
        {"target": _fx_selector(h), "include_available": True},
        flame,
    )
    assert asked["value"]["availableTypes"] == EFFECT_TYPES
    off = run_op(
        registry,
        "segment.list_effects",
        {"target": _fx_selector(h), "include_available": False},
        flame,
    )
    assert "availableTypes" not in off["value"]


class _FlakyEffect(PyTimelineFX):
    def __init__(self) -> None:
        super().__init__("Colour Correct", {"gain": 1.5})
        self.attributes.append("broken")

    @property
    def broken(self) -> Any:
        raise RuntimeError("cannot read")


def test_list_effects_reads_attributes_one_by_one() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    h["seg0"].effects = [_FlakyEffect()]
    result = run_op(registry, "segment.list_effects", {"target": _fx_selector(h)}, flame)
    assert result["opError"] is None
    attributes = result["value"]["effects"][0]["attributes"]
    assert attributes["broken"] == {"error": "cannot read"}
    assert attributes["gain"] == 1.5 and attributes["bypass"] is False


def test_list_effects_with_an_unreadable_attribute_list_gives_an_empty_dict() -> None:
    registry = make_registry()
    flame, h = build_default_tree()

    class _NoList(PyTimelineFX):
        @property
        def attributes(self) -> Any:
            raise RuntimeError("no list")

        @attributes.setter
        def attributes(self, _value: Any) -> None:
            pass

    h["seg0"].effects = [_NoList("Flip")]
    result = run_op(registry, "segment.list_effects", {"target": _fx_selector(h)}, flame)
    assert result["value"]["effects"] == [{"index": 0, "type": "Flip", "attributes": {}}]


def test_list_effects_accepts_a_record_in_selector_and_checks_the_source() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    h["seg1"].effects = [PyTimelineFX("Blur")]
    ok = run_op(registry, "segment.list_effects", {"target": _at(h, 10)}, flame)
    assert [e["type"] for e in ok["value"]["effects"]] == ["Blur"]
    bad = run_op(
        registry,
        "segment.list_effects",
        {"target": _at(h, 10, expect_source_name="clip1")},
        flame,
    )
    assert bad["opErrorCode"] == "CONFLICT"


def test_list_effects_is_a_read_that_only_accepts_a_segment_selector() -> None:
    registry = make_registry()
    op = registry.get("segment.list_effects")
    assert op is not None and op.effect == "read"
    result = validate_op_args(op, {"target": {"kind": "wiretap", "id": "x"}})
    assert result.ok is False
    assert "at most one effect of each type" in op.description
    assert "not in the order the effects were created" in op.description


def _seg(src: str, rec: int, **over: Any) -> PySegment:
    fields: dict[str, Any] = {
        "name": src,
        "source_name": src,
        "record_in": rec,
        "record_out": rec + 23,
        "source_in": 0,
        "source_out": 23,
        "record_duration": 24,
    }
    fields.update(over)
    return PySegment(**fields)


def _gap(rec: int) -> PySegment:
    return PySegment(
        name="",
        source_name="",
        record_in=rec,
        record_out=rec + 23,
        record_duration=24,
        type="Gap",
        head="infinite",
        tail="infinite",
    )


def _seq(flame: Any, name: str, tracks: list[list[PySegment]], cls: type = PySequence) -> Any:
    seq = cls(name, wiretap_id=f"/wt/{name}", versions=[PyVersion([PyTrack(t) for t in tracks])])
    flame._wiretap_index[f"/wt/{name}"] = seq
    return seq


def _sources_world() -> tuple[Any, dict[str, Any]]:
    """Builds a world where clip1 is used in several places.

    clip1 sits in S1 (track 0 and, later, track 1), in S2, in a copy of S1 and inside the
    reel clip SRC. Measured on Flame 2025.2.4: shared_source_segments() reaches the other
    sequences and the segments inside clips on the Desktop, but not copies in the library."""
    flame, h = build_default_tree()
    a, b = _seg("clip1", 0), _seg("clip2", 48)
    a2 = _seg("clip1", 30)
    s1 = _seq(flame, "S1", [[a, _gap(24), b], [a2]])
    s2a = _seg("clip1", 0)
    s2 = _seq(flame, "S2", [[s2a]])
    a_copy, a2_copy = _seg("clip1", 0), _seg("clip1", 30)
    s1_copy = _seq(flame, "S1copy", [[a_copy, _gap(24), _seg("clip2", 48)], [a2_copy]])
    inner = _seg("clip1", 0)
    src = _seq(flame, "SRC", [[inner]], cls=PyClip)
    src.parent = h["reel"]
    share_sources(a, a2, s2a, a_copy, a2_copy, inner)
    h.update(s1=s1, s2=s2, s1_copy=s1_copy, src=src, a=a, a2=a2, inner=inner)
    return flame, h


def _wt(seq: Any) -> dict[str, str]:
    return {"kind": "wiretap", "id": seq.get_wiretap_node_id()}


def _shared(flame: Any, h: dict[str, Any], **extra: Any) -> dict[str, Any]:
    target = {
        "kind": "segment",
        "parent": _wt(h["s1"]),
        "version": 0,
        "track": 0,
        "index": 0,
    }
    args = {"target": target, "limit": 50, "offset": 0, **extra}
    return run_op(make_registry(), "segment.shared_sources", args, flame)


def test_shared_sources_returns_every_segment_with_its_container_and_a_selector() -> None:
    flame, h = _sources_world()
    result = _shared(flame, h)
    assert result["opError"] is None
    value = result["value"]
    assert (value["total"], value["shown"], value["truncated"]) == (6, 6, False)
    containers = [(i["container"]["type"], i["container"]["name"]) for i in value["items"]]
    assert containers == [
        ("PySequence", "S1"),
        ("PySequence", "S1"),
        ("PySequence", "S2"),
        ("PySequence", "S1copy"),
        ("PySequence", "S1copy"),
        ("PyClip", "SRC"),
    ]
    first = value["items"][0]
    assert first["container"]["selector"] == _wt(h["s1"])
    assert first["selector"] == {
        "kind": "segment",
        "parent": _wt(h["s1"]),
        "version": 0,
        "track": 0,
        "index": 0,
    }
    assert first["sourceName"] == "clip1"
    assert first["recordIn"]["frame"] == 0 and first["recordOut"]["frame"] == 23
    assert first["sourceIn"]["frame"] == 0 and first["sourceOut"]["frame"] == 23
    json.dumps(value)


def test_shared_sources_marks_only_the_target_itself() -> None:
    flame, h = _sources_world()
    items = _shared(flame, h)["value"]["items"]
    assert [i["isTarget"] for i in items] == [True, False, False, False, False, False]
    assert all(i["ambiguous"] is False for i in items)


def test_shared_sources_other_track_of_the_same_sequence_gets_its_own_position() -> None:
    flame, h = _sources_world()
    second = _shared(flame, h)["value"]["items"][1]
    assert second["selector"]["track"] == 1 and second["selector"]["index"] == 0
    assert second["isTarget"] is False


def test_shared_sources_selectors_resolve_back_to_the_same_segment() -> None:
    flame, h = _sources_world()
    registry = make_registry()
    for item in _shared(flame, h)["value"]["items"]:
        info = run_op(registry, "segment.info", {"target": item["selector"]}, flame)
        assert info["opError"] is None, item
        assert info["value"]["sourceName"] == "clip1"
        assert info["value"]["recordIn"]["frame"] == item["recordIn"]["frame"]


def test_shared_sources_gives_the_parent_of_the_container() -> None:
    flame, h = _sources_world()
    items = _shared(flame, h)["value"]["items"]
    assert items[5]["container"]["parent"] == {"type": "PyReel", "name": "R1"}
    assert items[0]["container"]["parent"] is None  # S1 stands in no reel


def test_shared_sources_without_the_target_in_the_answer_marks_nobody() -> None:
    flame, h = _sources_world()
    share_sources(*[s for s in h["a"]._shared if s is not h["a"]])
    h["a"]._shared = [s for s in h["a2"]._shared]
    result = _shared(flame, h)
    assert result["opError"] is None
    assert [i["isTarget"] for i in result["value"]["items"]] == [False] * 5


def test_shared_sources_identical_content_on_two_tracks_is_ambiguous_with_candidates() -> None:
    flame, h = _sources_world()
    t0, t1 = _seg("clip1", 0), _seg("clip1", 0)
    s3 = _seq(flame, "S3", [[t0], [t1]])
    share_sources(h["a"], t0, t1)
    items = _shared(flame, h)["value"]["items"]
    assert len(items) == 3
    assert items[0]["selector"] is not None and items[0]["isTarget"] is True
    for twin in items[1:]:
        assert twin["selector"] is None
        assert twin["ambiguous"] is True
        assert twin["isTarget"] is False
        assert twin["candidates"] == [
            {"version": 0, "track": 0, "index": 0},
            {"version": 0, "track": 1, "index": 0},
        ]
        assert twin["container"]["selector"] == _wt(s3)


def test_shared_sources_a_duplicated_target_is_not_marked_either() -> None:
    flame, _h = _sources_world()
    t0, t1 = _seg("clip1", 0), _seg("clip1", 0)
    s3 = _seq(flame, "S3", [[t0], [t1]])
    share_sources(t0, t1)
    target = {"kind": "segment", "parent": _wt(s3), "version": 0, "track": 0, "index": 0}
    result = run_op(
        make_registry(),
        "segment.shared_sources",
        {"target": target, "limit": 50, "offset": 0},
        flame,
    )
    assert [i["isTarget"] for i in result["value"]["items"]] == [False, False]
    assert all(i["ambiguous"] for i in result["value"]["items"])


def test_shared_sources_candidates_are_capped_at_five() -> None:
    flame, h = _sources_world()
    many = [_seg("clip1", 0) for _ in range(7)]
    _seq(flame, "S4", [[m] for m in many])
    share_sources(h["a"], many[0])
    items = _shared(flame, h)["value"]["items"]
    assert len(items[1]["candidates"]) == 5


def test_shared_sources_pages_and_scans_only_the_containers_of_the_page() -> None:
    flame, h = _sources_world()
    scanned: list[str] = []

    class _Counting(PySequence):
        @property
        def versions(self) -> list[PyVersion]:
            scanned.append(str(self.name.get_value()))
            return super().versions

    far = _seg("clip1", 0)
    counting = _seq(flame, "FAR", [[far]], cls=_Counting)
    assert counting is not None
    share_sources(h["a"], h["a2"], far)
    page = _shared(flame, h, limit=2, offset=0)["value"]
    assert (page["total"], page["shown"], page["offset"], page["truncated"]) == (3, 2, 0, True)
    assert scanned == []  # FAR is on page 2: nothing walked it
    last = _shared(flame, h, limit=2, offset=2)["value"]
    assert (last["total"], last["shown"], last["offset"], last["truncated"]) == (3, 1, 2, False)
    assert last["items"][0]["container"]["name"] == "FAR" and scanned == ["FAR"]


def test_shared_sources_scans_a_container_once_for_several_of_its_segments() -> None:
    flame, h = _sources_world()
    scanned: list[str] = []

    class _Counting(PySequence):
        @property
        def versions(self) -> list[PyVersion]:
            scanned.append("scan")
            return super().versions

    x, y, z = _seg("clip1", 0), _seg("clip1", 30), _seg("clip1", 60)
    _seq(flame, "TWO", [[x, y, z]], cls=_Counting)
    share_sources(h["a"], x, y, z)
    result = _shared(flame, h)
    assert len(result["value"]["items"]) == 4 and scanned == ["scan"]
    assert [i["selector"]["index"] for i in result["value"]["items"][1:]] == [0, 1, 2]


def test_shared_sources_reports_a_candidate_it_cannot_place_without_failing() -> None:
    flame, h = _sources_world()
    orphan = _seg("clip1", 0)  # never read through a track: no parent chain
    share_sources(h["a"], orphan)
    items = _shared(flame, h)["value"]["items"]
    assert items[1]["selector"] is None and items[1]["container"] is None
    assert "error" in items[1]
    assert items[0]["isTarget"] is True


def test_shared_sources_without_a_wiretap_id_has_no_selector_but_still_lists() -> None:
    flame, h = _sources_world()

    class _NoWiretap(PySequence):
        def get_wiretap_node_id(self) -> str:
            raise RuntimeError("no wiretap id")

    lone = _seg("clip1", 0)
    _NoWiretap("NW", wiretap_id="x", versions=[PyVersion([PyTrack([lone])])])
    share_sources(h["a"], lone)
    item = _shared(flame, h)["value"]["items"][1]
    assert item["container"]["name"] == "NW" and item["container"]["selector"] is None
    assert item["selector"] is None and item["ambiguous"] is False and item["isTarget"] is False


def test_shared_sources_target_by_record_in_gets_the_same_answer() -> None:
    flame, h = _sources_world()
    by_index = _shared(flame, h)["value"]
    target = {
        "kind": "segment",
        "parent": _wt(h["s1"]),
        "version": 0,
        "track": 0,
        "record_in": 0,
    }
    by_record_in = run_op(
        make_registry(),
        "segment.shared_sources",
        {"target": target, "limit": 50, "offset": 0},
        flame,
    )["value"]
    assert by_record_in == by_index
    assert by_record_in["items"][0]["isTarget"] is True


def test_shared_sources_target_with_expect_source_name_conflict_reads_nothing() -> None:
    flame, h = _sources_world()
    target = {
        "kind": "segment",
        "parent": _wt(h["s1"]),
        "version": 0,
        "track": 0,
        "index": 0,
        "expect_source_name": "clip2",
    }
    result = run_op(
        make_registry(),
        "segment.shared_sources",
        {"target": target, "limit": 50, "offset": 0},
        flame,
    )
    assert result["opErrorCode"] == "CONFLICT"


def test_shared_sources_registration_is_a_bounded_read() -> None:
    op = make_registry().get("segment.shared_sources")
    assert op is not None
    assert op.effect == "read" and op.accepts == ("segment",)
    assert op.default_limit <= 20 and op.max_limit <= 100
    assert {p.name for p in op.params} == {"target", "limit", "offset"}


# segment.info carries what list_segments carries.


def _list_item(flame: Any, h: dict[str, Any], index: int) -> dict[str, Any]:
    args = {"target": _seq_target(h), "version": 0, "track": 0, "limit": 50, "offset": 0}
    items: list[dict[str, Any]] = run_op(make_registry(), "timeline.list_segments", args, flame)[
        "value"
    ]["items"]
    return items[index]


def test_segment_info_has_type_isgap_head_tail_and_effects_like_list_segments() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    gap = PySegment(name="", source_name="", type="Gap", head="infinite", tail="infinite")
    media = PySegment(name="m", source_name="clip1", head=5, tail=7, effects=[PyTimelineFX("Blur")])
    h["seq"]._versions = [PyVersion([PyTrack([media, gap])])]
    for index in (0, 1):
        info = run_op(registry, "segment.info", {"target": _segment_selector(h, index)}, flame)
        listed = _list_item(flame, h, index)
        assert info["opError"] is None
        for key in ("type", "isGap", "head", "tail", "effects"):
            assert info["value"][key] == listed[key], key
    first = run_op(registry, "segment.info", {"target": _segment_selector(h, 0)}, flame)["value"]
    assert (first["type"], first["isGap"], first["head"], first["tail"]) == (
        "Video Segment",
        False,
        5,
        7,
    )
    assert first["effects"] == ["Blur"]
    second = run_op(registry, "segment.info", {"target": _segment_selector(h, 1)}, flame)["value"]
    assert (second["isGap"], second["head"], second["effects"]) == (True, "infinite", [])


def test_segment_info_effects_of_a_segment_whose_effects_cannot_be_read_is_empty() -> None:
    class _NoFx(PySegment):
        @property
        def effects(self) -> Any:
            raise RuntimeError("nothing to read")

        @effects.setter
        def effects(self, _value: Any) -> None:
            pass

    flame, h = build_default_tree()
    h["seq"]._versions = [PyVersion([PyTrack([_NoFx(name="g", type="Gap")])])]
    value = run_op(make_registry(), "segment.info", {"target": _segment_selector(h, 0)}, flame)[
        "value"
    ]
    assert value["effects"] == [] and value["isGap"] is True


# Attribute values in segment.info have the same structure get_attributes gives.


def test_segment_info_attributes_are_structured_not_strings() -> None:
    flame, h = build_default_tree()
    value = run_op(make_registry(), "segment.info", {"target": _segment_selector(h, 0)}, flame)[
        "value"
    ]
    attributes = value["attributes"]
    assert attributes["colour"] == [0.0, 0.0, 0.0]
    assert attributes["tags"] == [] and attributes["selected_markers"] == []
    assert attributes["name"] == "seg0"  # still name -> value, not wrapped in {value: ...}
    json.dumps(attributes)


def test_segment_info_attributes_equal_the_values_get_attributes_returns() -> None:
    registry = make_registry()
    flame, h = build_default_tree()
    seg = h["seg0"]
    seg.comment = FakeAttr("note")
    seg.attributes.append("comment")
    target = {"target": _segment_selector(h, 0)}
    info = run_op(registry, "segment.info", target, flame)["value"]["attributes"]
    got = run_op(registry, "segment.get_attributes", target, flame)["value"]["attributes"]
    assert set(info) == set(got)
    assert info == {key: entry["value"] for key, entry in got.items()}


def test_segment_info_attribute_that_holds_a_flame_object_is_its_type_not_its_repr() -> None:
    flame, h = build_default_tree()
    seg = h["seg0"]
    seg.owner_track = FakeAttr(PyTrack([]))
    seg.attributes.append("owner_track")
    value = run_op(make_registry(), "segment.info", {"target": _segment_selector(h, 0)}, flame)[
        "value"
    ]
    assert value["attributes"]["owner_track"] == {"type": "PyTrack", "name": "*"}


class _SourcedSegment(PySegment):
    def __init__(self, **sourced: Any) -> None:
        super().__init__(name="sourced")
        for key, value in sourced.items():
            setattr(self, key, value)


def _info_of(segment: PySegment) -> dict[str, Any]:
    registry = make_registry()
    flame, h = build_default_tree()
    h["seq"]._versions = [PyVersion([PyTrack([segment])])]
    sel = {"kind": "segment", "parent": _seq_target(h), "version": 0, "track": 0, "index": 0}
    result = run_op(registry, "segment.info", {"target": sel}, flame)
    assert result["opError"] is None, result
    value: dict[str, Any] = result["value"]
    return value


def test_segment_info_gives_the_source_file_tape_uid_and_whether_it_is_offline() -> None:
    value = _info_of(
        _SourcedSegment(
            file_path="/media/shot/A001.mov",
            tape_name="A001",
            source_uid="src-uid-1",
            source_unlinked=False,
        )
    )
    assert value["filePath"] == "/media/shot/A001.mov"
    assert value["tapeName"] == "A001"
    assert value["sourceUid"] == "src-uid-1"
    assert value["sourceUnlinked"] is False


def test_segment_info_marks_an_offline_source() -> None:
    value = _info_of(_SourcedSegment(file_path="/gone.mov", source_unlinked=True))
    assert value["sourceUnlinked"] is True and value["filePath"] == "/gone.mov"


def test_segment_info_gives_null_for_what_the_segment_does_not_have() -> None:
    value = _info_of(PySegment(name="plain"))  # the fake has none of the four
    for key in ("filePath", "tapeName", "sourceUid", "sourceUnlinked"):
        assert key in value and value[key] is None, key


def test_segment_info_gives_null_where_reading_one_raises() -> None:
    class _Broken(PySegment):
        @property
        def file_path(self) -> str:
            raise RuntimeError("boom")

    segment = _Broken(name="broken")
    segment.tape_name = "T1"  # type: ignore[attr-defined]
    value = _info_of(segment)
    assert value["filePath"] is None and value["tapeName"] == "T1"


def test_segment_info_gives_null_for_a_gap() -> None:
    gap = _SourcedSegment(
        file_path="/not/a/source", tape_name="x", source_uid="u", source_unlinked=False
    )
    gap.type = "Gap"
    value = _info_of(gap)
    for key in ("filePath", "tapeName", "sourceUid", "sourceUnlinked"):
        assert value[key] is None, key


def test_segment_info_reads_an_attribute_object_value() -> None:
    value = _info_of(_SourcedSegment(file_path=FakeAttr("/media/b.mov"), tape_name=FakeAttr("B")))
    assert value["filePath"] == "/media/b.mov" and value["tapeName"] == "B"


def test_segment_info_description_does_not_promise_null_for_an_empty_value() -> None:
    op = make_registry().get("segment.info")
    assert op is not None
    text = op.description
    assert "empty string" in text and "offline" not in text
    assert "all four are null for a gap" in text
