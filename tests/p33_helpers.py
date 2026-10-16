"""Shared rig and helpers for the segment op tests.

Used by test_segment_slip_p33.py, test_segment_replace_p33.py, test_segment_effects_p33.py,
test_segment_extract_p33.py and test_plan_p33.py: the rig of the timeline tests
(`timeline_p32a_helpers`: sequences S1 / S2 / S3 and the clip CLIPX on a Desktop reel, a
library with a reel) plus clips with media of a known start and length, and the readers that look
at the fake directly -- the way the real-Flame e2e reads state back by transport, never through
the ops under test."""

from __future__ import annotations

from typing import Any

from fake_media_tree import PyClip, PySegment, PySequence, PyTrack, PyVersion, adopt
from fake_segment_edit import clip_media
from journal_test_helpers import Rig
from timeline_p32a_helpers import (
    REEL_WT,
    S1,
    a_seg,
    checkpoints,
    media,
    seg_sel,
    stash_library,
    wt,
)

SLIP = "segment.slip"
REPLACE = "segment.replace"
CREATE_FX = "segment.create_effect"
DELETE_FX = "segment.delete_effect"
SET_FX = "segment.set_effect"
LOAD_FX = "segment.effect_load_setup"
SAVE_FX = "segment.effect_save_setup"
COPY_OUT = "segment.copy_to_media_panel"
MATCH = "segment.match"
LETTERBOX = "Burn-in Letterbox"
GAP = "Gap"
VIDEO = "Video Segment"


def untouched_world(rig: Rig) -> bool:
    """Nothing was copied, journaled or touched."""
    return (
        stash_library(rig) is None
        and rig.records() == []
        and rig.flame.media_panel.copy_calls == []
    )


def track0(rig: Rig) -> list[Any]:
    segments: list[Any] = rig.handles["s1"].versions[0].tracks[0].segments
    return segments


def seg_a(rig: Rig) -> PySegment:
    """The segment `a_seg()` addresses in S1 (track 0, index 1: clip1 10-33, src 5-28, h5 t5)."""
    seg: PySegment = track0(rig)[1]
    return seg


def seg_b(rig: Rig) -> PySegment:
    """index 3: clip2 40-63 with a Burn-in Letterbox."""
    seg: PySegment = track0(rig)[3]
    return seg


def seg_row(seg: PySegment) -> list[Any]:
    return [
        seg.source_name,
        seg.record_in.frame,
        seg.record_out.frame,
        seg.source_in.frame,
        seg.source_out.frame,
        seg.head,
        seg.tail,
    ]


def effect_names(seg: PySegment) -> list[str]:
    return [fx.type for fx in seg.effects]


def add_media_clip(
    rig: Rig,
    name: str,
    *,
    start: int = 0,
    length: int = 72,
    reel: str = "reel",
    effects: tuple[Any, ...] = (),
) -> PyClip:
    """A clip of the Desktop reel (or another container of `rig.handles`) whose media starts at the
    absolute source frame `start` and is `length` frames long (see fake_segment_edit)."""
    container = rig.handles[reel]
    clip = PyClip(
        name,
        wiretap_id=f"{container.get_wiretap_node_id()}/{name}",
        duration=length,
        versions=[
            PyVersion([PyTrack([media(name, 0, length - 1, source_in=start, effects=effects)])])
        ],
    )
    clip._media = (start, length)
    adopt(container, [clip])
    container._children.append(clip)
    return clip


def clip_sel(clip: PyClip) -> dict[str, Any]:
    return wt(clip.get_wiretap_node_id())


def sel_at(index: int, track: int = 0, parent: dict[str, Any] = S1) -> dict[str, Any]:
    return seg_sel(parent, track, index=index)


MEDIA_TABLE: dict[str, tuple[int, int]] = {
    "clip1": (0, 24),
    "clip2": (0, 24),
    "clip3": (0, 24),
    "p2_len12": (0, 12),
    "p2_len48": (0, 48),
    "p2_len72": (0, 72),
    "p2_src_tc1h": (86400, 48),
    "p2_rep_tc1h": (86400, 48),
    "p2_rep_tc1h_plus10": (86410, 48),
    "p2_rep_tc2h": (172800, 48),
}
"""(first source frame, length) of the e2e material (scripts/make_p2_media.sh and
make_p2_tc_media.sh: the p2_lenN files have N frames, the clips 24, the TC files 48 frames at
01:00:00:00, 01:00:00:10 and 02:00:00:00)."""


def import_with_media(fake_cls: Any) -> Any:
    """`FakeFlame.import_clips` that gives every clip its media (a table above) and the one
    segment that holds it, the way a real import does."""
    original = fake_cls.import_clips

    def import_clips(self: Any, path: Any, destination: Any) -> list[Any]:
        made: list[Any] = original(self, path, destination)
        for clip in made:
            name = str(clip.name.get_value())
            start, length = MEDIA_TABLE[name]
            clip._versions[0]._tracks[0]._segments.append(
                media(name, 0, length - 1, source_in=start, name=name)
            )
            clip._media = (start, length)
        return made

    return import_clips


def fake_make_sequence(
    flame: Any,
    reel: Any,
    name: str,
    puts: list[list[Any]],
    trims: list[list[int]],
    fxs: list[list[Any]],
) -> dict[str, Any]:
    """What scripts/p3_3_e2e_common.MAKE_SEQ_TEMPLATE does on a real Flame, on the fake: the clips
    put where `overwrite(clip, PyTime(rel), track)` puts them (record_in = rel - 1, the whole media
    as one segment), the first media segment of a track trimmed head then tail without ripple (the
    fake's own trim), the effects created with `create_effect` and their attributes set; the
    sequence has the bit depth of the first clip put, and the result says so as the real code
    does."""
    tracks: list[list[PySegment]] = [[], []]
    depths = [flame.find_by_wiretap_node_id(clip_wt).bit_depth for _track, clip_wt, _rel in puts]
    bit_depth = depths[0] if depths else None
    for track, clip_wt, rel in puts:
        clip = flame.find_by_wiretap_node_id(clip_wt)
        start, length = clip_media(clip)
        label = str(clip.name.get_value())
        tracks[track].append(media(label, rel - 1, rel + length - 2, source_in=start, name=label))
    seq = PySequence(
        name,
        wiretap_id=f"{reel.get_wiretap_node_id()}/{name}",
        duration=max((s.record_out.frame + 1 for t in tracks for s in t), default=1),
        versions=[PyVersion([PyTrack(t) for t in tracks])],
        bit_depth=bit_depth if bit_depth is not None else 10,
    )
    adopt(reel, [seq])
    reel._children.append(seq)
    notes: list[str] = []
    for track, head, tail in trims:
        trk = seq.versions[0].tracks[track]
        first = next(s for s in trk.segments if s.type != "Gap")
        notes.append(repr(first.trim_head(head)))
        first = next(s for s in trk.segments if s.type != "Gap")
        notes.append(repr(first.trim_tail(tail)))
    for track, source, fx_type, attrs in fxs:
        trk = seq.versions[0].tracks[track]
        found = next(s for s in trk.segments if s.type != "Gap" and s.source_name == source)
        fx = found.create_effect(fx_type)
        for key, value in attrs.items():
            getattr(fx, key).set_value(value)
    return {
        "wt": seq.get_wiretap_node_id(),
        "uid": seq.uid.get_value(),
        "name": str(seq.name.get_value()),
        "notes": notes,
        "bitDepth": bit_depth,
        "clipBitDepths": depths,
        "seqBitDepth": seq.bit_depth,
    }


__all__ = [
    "COPY_OUT",
    "CREATE_FX",
    "DELETE_FX",
    "GAP",
    "LETTERBOX",
    "LOAD_FX",
    "MATCH",
    "MEDIA_TABLE",
    "REEL_WT",
    "REPLACE",
    "SAVE_FX",
    "SET_FX",
    "SLIP",
    "VIDEO",
    "a_seg",
    "add_media_clip",
    "checkpoints",
    "clip_sel",
    "effect_names",
    "fake_make_sequence",
    "import_with_media",
    "seg_a",
    "seg_b",
    "seg_row",
    "sel_at",
    "track0",
    "untouched_world",
]
