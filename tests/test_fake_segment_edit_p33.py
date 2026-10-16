"""Tests for the segment-editing fake (fake_segment_edit.py) against Flame.

What the real PySegment / PyTimelineFX did on Flame 2025.2.4, stated in the measured numbers (the
fake counts record_out / source_out the way Flame does, the last frame in, so a measured
`src 10-61` is 10-61 here too). The ops' tests lean on these behaviours; this file is what keeps
the fake honest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fake_segment_edit as fse
from fake_media_tree import (
    FakeFlame,
    PyClip,
    PyLibrary,
    PyReel,
    PySegment,
    PySequence,
    PyTrack,
    PyVersion,
    adopt,
)
from timeline_p32a_helpers import media, rows


def sequence_with(segment: PySegment, reel: PyReel | None = None) -> PySequence:
    seq = PySequence("S", wiretap_id="/wt/S", versions=[PyVersion([PyTrack([segment])])])
    if reel is not None:
        adopt(reel, [seq])
        reel._children.append(seq)
    return seq


def len72() -> PySegment:
    """A segment of p2_len72 as measured: used range 10-61, head 10, tail 10."""
    seg = media("p2_len72", 10, 61, source_in=10, head=10, tail=10)
    sequence_with(seg)
    return seg


def row(seg: PySegment) -> tuple[int, int, int, int, Any, Any]:
    return (
        seg.record_in.frame,
        seg.record_out.frame,
        seg.source_in.frame,
        seg.source_out.frame,
        seg.head,
        seg.tail,
    )


@pytest.mark.parametrize(
    ("offset", "want"),
    [
        (3, (10, 61, 7, 58, 7, 13)),  # slip(3)    -> src 7-58  h7  t13
        (-5, (10, 61, 15, 66, 15, 5)),  # slip(-5)   -> source toward the tail
        (100, (10, 61, 0, 51, 0, 20)),  # slip(100)  -> src 0-51  h0  t20 (stops at the handle)
        (-100, (10, 61, 20, 71, 20, 0)),  # slip(-100) -> src 20-71 h20 t0
    ],
)
def test_slip_takes_source_in_down_by_n_and_stops_at_the_handle_without_an_error(
    offset: int, want: tuple[int, ...]
) -> None:
    seg = len72()
    assert seg.slip(offset) is False  # the answer is False whatever happened
    assert row(seg) == want  # record never moves


def test_a_trim_counts_the_last_frame_in_the_way_plan_47_measured() -> None:
    """As measured, overwrite at PyTime(0) puts the clip at rec -1..22 (24 frames)."""
    seg = media("clip1", -1, 22)
    seq = sequence_with(seg)
    assert seq.versions[0].tracks[0].segments == [seg]  # a track read gives its segments a parent
    assert seg.record_duration.frame == 24
    assert (seg.source_in.frame, seg.source_out.frame) == (0, 23)
    seg.trim_head(5)
    assert row(seg) == (4, 22, 5, 23, 5, 0)
    assert rows(seq)[0] == ["Gap", -1, 3, 0, 4, "infinite", "infinite"]
    seg.trim_tail(5)
    assert row(seg) == (4, 17, 5, 18, 5, 5)


def test_a_ripple_trim_closes_the_track_up_and_leaves_the_gap_plan_60_measured() -> None:
    """A rippled tail trim closes the track up and leaves a Gap at the end.

    track0 = [clip1 0-23][clip2 24-47][len48 48-95]; trim_tail(5, ripple=True) on clip2
    -> clip2 24-42, len48 43-90 and a Gap 91-95 at the end."""
    first, second, third = media("clip1", 0, 23), media("clip2", 24, 47, tail=5), media("l", 48, 95)
    seq = sequence_with(first)
    track = seq.versions[0].tracks[0]
    track._segments[:] = [first, second, third]
    assert track.segments == [first, second, third]
    second.trim_tail(5, ripple=True)
    assert [r[:3] + r[3:5] for r in rows(seq)] == [
        ["Video Segment", 0, 23, 0, 23],
        ["Video Segment", 24, 42, 0, 18],
        ["Video Segment", 43, 90, 0, 47],
        ["Gap", 91, 95, 0, 4],
    ]


def test_a_segment_inside_a_library_is_locked() -> None:
    library = PyLibrary("L", "uid-l", "/wt/L")
    reel = PyReel("R", "uid-r", "/wt/L/R")
    adopt(library, [reel])
    library._children.append(reel)
    seg = media("m", 0, 23, source_in=5, head=5, tail=5)
    sequence_with(seg, reel)
    for call in (
        lambda: seg.slip(1),
        lambda: seg.create_effect("Blur"),
        lambda: seg.trim_head(1),
    ):
        with pytest.raises(RuntimeError, match=r"Clip is locked\."):
            call()


def clip_of(name: str, start: int, length: int) -> PyClip:
    clip = PyClip(
        name,
        wiretap_id=f"/wt/{name}",
        versions=[PyVersion([PyTrack([media(name, 0, length - 1, source_in=start)])])],
    )
    clip._media = (start, length)
    return clip


def test_smart_replace_media_keeps_the_offset_from_the_clip_start_not_the_timecode() -> None:
    seg = media("p2_src_tc1h", 0, 27, source_in=86410, head=10, tail=10)
    sequence_with(seg)
    seg.smart_replace_media(clip_of("p2_rep_tc1h_plus10", 86410, 48))  # answers None
    # 10 frames in from a clip that starts 10 frames later: the source timecode moved by 10
    assert row(seg) == (0, 27, 86420, 86447, 10, 10)
    assert seg.source_name == "p2_rep_tc1h_plus10"
    assert seg.slip(10) is False and row(seg)[2:4] == (86410, 86437)  # back where it was


def test_a_clip_whose_timecode_does_not_overlap_at_all_is_still_taken_by_offset() -> None:
    seg = media("p2_src_tc1h", 0, 27, source_in=86410, head=10, tail=10)
    sequence_with(seg)
    seg.smart_replace_media(clip_of("p2_rep_tc2h", 172800, 48))
    assert row(seg)[2:4] == (172810, 172837)  # 02:00:00+10


def test_a_clip_too_short_for_the_range_changes_nothing_and_raises_nothing() -> None:
    seg = media("p2_len72", 10, 61, source_in=10, head=10, tail=10)
    sequence_with(seg)
    before = (row(seg), seg.source_name)
    seg.smart_replace_media(clip_of("p2_len12", 0, 12))  # answers None, raises nothing
    assert (row(seg), seg.source_name) == before


def test_smart_replace_replaces_the_effects_and_the_media_variant_keeps_them() -> None:
    own = fse.new_effect("Blur")
    new = PyClip(
        "withblur",
        wiretap_id="/wt/withblur",
        versions=[PyVersion([PyTrack([media("withblur", 0, 47, effects=(own,))])])],
    )
    new._media = (0, 48)
    keep = media("a", 0, 23, source_in=5, head=5, tail=5, effects=(fse.new_effect("Flip"),))
    sequence_with(keep)
    keep.smart_replace_media(new)
    assert [fx.type for fx in keep.effects] == ["Flip"]
    keep.smart_replace(new)
    assert [fx.type for fx in keep.effects] == ["Blur"]


def test_create_effect_raises_for_an_unknown_type_and_for_a_second_one_of_a_type() -> None:
    seg = media("m", 0, 23)
    sequence_with(seg)
    with pytest.raises(RuntimeError, match="Could not create an effect of given effect type"):
        seg.create_effect("NoSuchFx")
    fx = seg.create_effect("Blur")
    assert fx.type == "Blur" and fx.attributes == ["bypass"]
    with pytest.raises(RuntimeError, match=r"An effect of this type already exists\."):
        seg.create_effect("Blur")
    seg.create_effect("Resize")
    assert [e.type for e in seg.effects] == ["Resize", "Blur"]  # not in creation order


def test_a_burn_in_letterbox_has_mode_ratio_and_colour_with_their_candidates() -> None:
    seg = media("m", 0, 23)
    sequence_with(seg)
    fx = seg.create_effect(fse_type := "Burn-in Letterbox")
    assert fx.type == fse_type and fx.attributes == ["bypass", "mode", "ratio", "colour"]
    assert (
        fx.mode.values is not None
        and len(fx.mode.values) == 18
        and fx.ratio.values == (0.01, 100.0)
    )
    assert fx.colour.values == ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))


@pytest.mark.parametrize(("fx_type", "extensions"), sorted(fse.SETUP_EXTENSIONS.items()))
def test_save_setup_writes_the_files_plan_59_listed_for_each_type(
    tmp_path: Path, fx_type: str, extensions: tuple[str, ...]
) -> None:
    fx = fse.new_effect(fx_type)
    assert fx.save_setup(str(tmp_path / "setup")) is True
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted("setup" + e for e in extensions)


def test_load_setup_of_a_path_that_is_not_there_raises_and_changes_nothing(tmp_path: Path) -> None:
    fx = fse.new_effect("Blur")
    fx.hidden = {"amount": 7.0}
    with pytest.raises(RuntimeError, match=r"Could not load the setup\. Check the filepath\."):
        fx.load_setup(str(tmp_path / "nope.blur_node"))
    assert fx.hidden == {"amount": 7.0}


def test_load_setup_brings_back_attributes_and_the_hidden_parameters(tmp_path: Path) -> None:
    fx = fse.new_effect("Burn-in Letterbox")
    fx.ratio.set_value(1.5)
    fx.hidden = {"extra": 3}
    fx.save_setup(str(tmp_path / "lb"))
    fx.ratio.set_value(9.0)
    fx.hidden = {}
    assert fx.load_setup(str(tmp_path / "lb.letterbox_node")) is True
    assert fx.ratio.get_value() == 1.5 and fx.hidden == {"extra": 3}


def test_flame_delete_takes_an_effect_off_the_segment() -> None:
    seg = media("m", 0, 23)
    sequence_with(seg)
    fx = seg.create_effect("Blur")
    assert FakeFlame().delete(fx, confirm=False) is True
    assert seg.effects == []


def test_an_effect_survives_a_copy_with_its_settings() -> None:
    seg = media("m", 0, 23)
    sequence_with(seg)
    fx = seg.create_effect("Burn-in Letterbox")
    fx.ratio.set_value(1.5)
    copy = fse.clone_effect(fx)
    assert copy.ratio.get_value() == 1.5 and copy.ratio.values == (0.01, 100.0)
    assert copy.attributes == fx.attributes


# The segment is a Timewarp one: used 52 frames, head 10, tail 10.


def shapes() -> dict[str, dict[str, Any]]:
    reel = PyReel("R", "uid-r", "/wt/R")
    seg = media("p2_len72", 112, 163, source_in=10, head=10, tail=10)
    seg.effects = [fse.new_effect("Timewarp")]
    sequence_with(seg)
    out: dict[str, dict[str, Any]] = {}
    made = {
        "copy": seg.copy_to_media_panel(reel),
        "match": seg.match(reel)[0],
        "preserve": seg.match(reel, preserve_handle=True)[0],
        "fx": seg.match(reel, include_timeline_fx=True)[0],
    }
    for key, clip in made.items():
        inner = clip.versions[0].tracks[0].segments[0]
        out[key] = {
            "duration": clip.duration.frame,
            "start_frame": clip.start_frame,
            "marks": (
                None if clip.in_mark is None else clip.in_mark.frame,
                None if clip.out_mark is None else clip.out_mark.frame,
            ),
            "head": inner.head,
            "tail": inner.tail,
            "effects": [fx.type for fx in inner.effects],
            "source": (inner.source_in.frame, inner.source_out.frame),
        }
    assert len(reel.children) == 4
    return out


def test_the_three_shapes_and_the_odd_one_follow_plan_59() -> None:
    got = shapes()
    assert got["copy"] == {
        "duration": 52,
        "start_frame": 11,
        "marks": (None, None),
        "head": 10,
        "tail": 10,
        "effects": ["Timewarp"],
        "source": (10, 61),
    }
    assert got["match"] == {
        "duration": 52,
        "start_frame": 1,
        "marks": (10, 62),
        "head": 0,
        "tail": 0,
        "effects": [],
        "source": (0, 71),
    }
    assert got["preserve"] == {
        "duration": 52,
        "start_frame": 11,
        "marks": (None, None),
        "head": 10,
        "tail": 10,
        "effects": [],
        "source": (10, 61),
    }
    assert got["fx"]["duration"] == 72 and got["fx"]["tail"] == "infinite"
    assert got["fx"]["head"] == 10 and got["fx"]["effects"] == ["Timewarp"]


def test_a_media_without_handles_gives_a_handle_zero_clip_by_every_route() -> None:
    reel = PyReel("R", "uid-r", "/wt/R")
    seg = media("clip1", 0, 23)
    sequence_with(seg)
    for clip in (
        seg.copy_to_media_panel(reel),
        seg.match(reel)[0],
        seg.match(reel, preserve_handle=True)[0],
    ):
        inner = clip.versions[0].tracks[0].segments[0]
        assert (inner.head, inner.tail) == (0, 0)


def test_a_flame_whose_match_answers_the_clip_itself_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reel = PyReel("R", "uid-r", "/wt/R")
    seg = media("clip1", 0, 23)
    sequence_with(seg)
    monkeypatch.setattr(fse, "MATCH_ANSWERS_LIST", False)
    assert isinstance(seg.match(reel), PyClip)


def test_every_clip_made_out_of_a_segment_keeps_the_bit_depth_of_its_source() -> None:
    reel = PyReel("R", "uid-r", "/wt/R")
    seg = media("clip1", 0, 23)
    seg.source_bit_depth = 12
    sequence_with(seg)
    for clip in (
        seg.copy_to_media_panel(reel),
        seg.match(reel)[0],
        seg.match(reel, preserve_handle=True)[0],
        seg.match(reel, include_nested_content=True)[0],
    ):
        assert clip.versions[0].tracks[0].segments[0].source_bit_depth == 12
