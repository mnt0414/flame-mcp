"""Fake timeline-building calls, kept in a module of their own.

Adds to the fake Flame tree (`fake_media_tree`) what 2025.2.4 does for `PySequence.overwrite` /
`insert` / `cut`, `PyTrack.cut`, `PySequence.create_version`, `PyVersion.create_track` and
`PyReel.create_sequence`. Measured on
Flame 2025.2.4 (run twice, with identical results) unless a docstring says
INVENTED.

A time is `PyTime(frame, seq.frame_rate)`: the ABSOLUTE record frame. record_out counts the last
frame in (24 frames from 0 are 0-23).
"""

from __future__ import annotations

from typing import Any

import fake_media_tree as fmt
from fake_media_tree import (
    FRAME_RATES,
    FakeAttr,
    PyClip,
    PyReel,
    PySegment,
    PySequence,
    PyTime,
    PyTrack,
    PyVersion,
    adopt,
    in_library,
    next_uid,
)
from fake_segment_edit import clip_media, new_effect

LOCKED = "Clip is locked."
BIT_DEPTHS = (8, 10, 12, 16, 32)
PROJECT_BIT_DEPTH = 10


def _check_time(time: Any) -> int:
    if not isinstance(time, PyTime):
        raise TypeError(f"expected a PyTime, got {type(time).__name__}")
    return int(time.frame)


def _locked(seq: PySequence) -> None:
    if in_library(seq):
        raise RuntimeError(LOCKED)


def _mark(clip: PyClip, name: str) -> int | None:
    raw = getattr(clip, name, None)
    value = raw.get_value() if isinstance(raw, FakeAttr) else raw
    if value is None:
        return None
    return int(value.frame) if isinstance(value, PyTime) else int(value)


def _used_range(clip: PyClip) -> tuple[int, int, int, int]:
    """Returns (source_in, source_out, head, tail) of what an overwrite puts in.

    That is the whole media, or the range between the in and out marks (the out mark is not
    included); source_out is the last frame.
    """
    start, length = clip_media(clip)
    in_mark, out_mark = _mark(clip, "in_mark"), _mark(clip, "out_mark")
    first = start + (in_mark if in_mark is not None else 0)
    end = start + (out_mark - 1 if out_mark is not None else length - 1)
    return first, end, first - start, start + length - 1 - end


def _segment_for(clip: PyClip, at: int) -> PySegment:
    first, last, head, tail = _used_range(clip)
    return PySegment(
        name=str(clip.name.get_value()),
        source_name=str(clip.name.get_value()),
        record_in=at,
        record_out=at + (last - first),
        source_in=first,
        source_out=last,
        record_duration=last - first + 1,
        head=head,
        tail=tail,
        source_bit_depth=getattr(clip, "bit_depth", None),
    )


def _locate(seq: PySequence, track: PyTrack) -> tuple[int, int]:
    for vi, version in enumerate(seq.versions):
        for ti, candidate in enumerate(version.tracks):
            if candidate is track:
                return vi, ti
    raise RuntimeError("the track is not a track of this sequence")


def _split(track: PyTrack, at: int) -> None:
    """Splits the segment (or Gap) that `at` falls inside.

    `at` becomes the first frame of the right part (record_in < at <= record_out); a cut at a
    segment's first frame or outside does nothing.
    """
    segs = track._segments
    for i, seg in enumerate(segs):
        rin, rout = seg.record_in.frame, seg.record_out.frame
        if not rin < at <= rout:
            continue
        if seg.type == "Gap":
            left, right = PySegment._new_gap(rin, at - 1), PySegment._new_gap(at, rout)
        else:
            left = _piece(seg, rin, at - 1, trim_tail=rout - (at - 1))
            right = _piece(seg, at, rout, trim_head=at - rin)
        segs[i : i + 1] = [left, right]
        adopt(track, [left, right])
        return


def _piece(
    seg: PySegment, rin: int, rout: int, *, trim_head: int = 0, trim_tail: int = 0
) -> PySegment:
    made = PySegment(
        name=str(seg.name.get_value()),
        source_name=seg.source_name,
        record_in=rin,
        record_out=rout,
        source_in=seg.source_in.frame + trim_head,
        source_out=seg.source_out.frame - trim_tail,
        record_duration=rout - rin + 1,
        head=seg.head if seg.head == "infinite" else int(seg.head) + trim_head,
        tail=seg.tail if seg.tail == "infinite" else int(seg.tail) + trim_tail,
        effects=[fmt.clone_effect(fx) for fx in seg.effects],
        source_bit_depth=seg.source_bit_depth,
    )
    return made


def _end_of(track: PyTrack, base: int) -> int:
    return max((s.record_out.frame for s in track._segments), default=base - 1) + 1


def _start_of(seq: PySequence) -> int:
    start = seq.start_time.get_value()
    return int(start.frame) if isinstance(start, PyTime) else 0


def _fill_to(track: PyTrack, at: int, base: int) -> None:
    """A put after the end of the track leaves a Gap in between."""
    end = _end_of(track, base)
    if at > end:
        track._segments.append(PySegment._new_gap(end, at - 1))


def _refresh(seq: PySequence) -> None:
    ins = [s.record_in.frame for v in seq._versions for t in v._tracks for s in t._segments]
    outs = [s.record_out.frame for v in seq._versions for t in v._tracks for s in t._segments]
    if ins:
        seq.duration = PyTime(max(outs) - min(ins) + 1)
    for version in seq._versions:
        version.parent = seq
        for track in version._tracks:
            track.parent = version
            adopt(track, track._segments)


def _has_media(seq: PySequence) -> bool:
    return any(s.type != "Gap" for v in seq._versions for t in v._tracks for s in t._segments)


def seq_put(seq: PySequence, clip: PyClip, time: Any, track: PyTrack, *, insert: bool) -> Any:
    _locked(seq)
    at = _check_time(time)
    _locate(seq, track)
    first, last, _head, _tail = _used_range(clip)
    length = last - first + 1
    was_empty = not _has_media(seq)
    base = _start_of(seq)
    in_mark = _mark(clip, "in_mark")
    segs = track._segments
    if insert:
        _split(track, at)
        _fill_to(track, at, base)
        for seg in segs:
            if seg.record_in.frame >= at:
                seg._set_record(seg.record_in.frame + length, seg.record_out.frame + length)
    else:
        _split(track, at)
        _split(track, at + length)
        _fill_to(track, at, base)
        track._segments = [
            s
            for s in segs
            if not (s.record_in.frame >= at and s.record_out.frame <= at + length - 1)
        ]
    made = _segment_for(clip, at)
    seq_depth = getattr(seq, "bit_depth", None)
    if (
        made.source_bit_depth is not None
        and seq_depth is not None
        and made.source_bit_depth != seq_depth
    ):
        resize = new_effect("Resize")  # Flame puts it on by itself (2025.2.4 and 2026.2.3)
        resize.parent = made
        made.effects.append(resize)
    track._segments.append(made)
    track._segments.sort(key=lambda s: s.record_in.frame)
    if was_empty and not insert and in_mark:
        seq.start_frame += in_mark  # an overwrite into an empty sequence moves start_frame
    _refresh(seq)
    return None


def seq_overwrite(seq: PySequence, clip: PyClip, time: Any, track: PyTrack) -> Any:
    return seq_put(seq, clip, time, track, insert=False)


def seq_insert(seq: PySequence, clip: PyClip, time: Any, track: PyTrack) -> Any:
    return seq_put(seq, clip, time, track, insert=True)


def track_cut(track: PyTrack, time: Any) -> Any:
    seq = track.parent.parent if track.parent is not None else None
    if seq is None or in_library(seq):
        raise RuntimeError(LOCKED)
    _split(track, _check_time(time))
    _refresh(seq)
    return None


def seq_cut(seq: PySequence, time: Any) -> Any:
    """Cuts every track of every version.

    INVENTED where it was not measured: the probe ran it on a sequence of one track.
    """
    _locked(seq)
    at = _check_time(time)
    for version in seq._versions:
        for track in version._tracks:
            _split(track, at)
    _refresh(seq)
    return None


def _span(tracks: list[PyTrack], fallback: int) -> tuple[int, int]:
    ins = [s.record_in.frame for t in tracks for s in t._segments]
    outs = [s.record_out.frame for t in tracks for s in t._segments]
    return (min(ins), max(outs)) if ins else (fallback, fallback)


def seq_create_version(seq: PySequence) -> PyVersion:
    """Adds a version at the END: one track, one Gap as long as the sequence."""
    _locked(seq)
    every = [t for v in seq._versions for t in v._tracks]
    low, high = _span(every, _start_of(seq))
    version = PyVersion([PyTrack([PySegment._new_gap(low, high)])])
    seq._versions.append(version)
    _refresh(seq)
    return version


def version_create_track(version: PyVersion, track_index: int | None = None) -> PyTrack:
    """Adds a track, one Gap as long as the version.

    With no index the track goes at the END (a new track given an index goes to index - 1,
    which the op does not use).
    """
    seq = version.parent
    if seq is None or in_library(seq):
        raise RuntimeError(LOCKED)
    low, high = _span(version._tracks, _start_of(seq))
    track = PyTrack([PySegment._new_gap(low, high)])
    if track_index is None:
        version._tracks.append(track)
    else:
        version._tracks.insert(max(0, track_index - 1), track)
    _refresh(seq)
    return track


def reel_create_sequence(
    reel: PyReel,
    name: str,
    video_tracks: int = 1,
    start_at: Any = None,
    frame_rate: str = "23.976 fps",
    width: int = 1920,
    height: int = 1080,
    bit_depth: Any = None,
    **_ignored: Any,
) -> PySequence:
    """Adds a PySequence at the end of the reel (the same name can be used twice).

    A frame_rate that is not one of the ten raises RuntimeError; the sequence is one Gap one
    frame long at its start timecode, start_frame 1; the Timeline then shows it.

    `bit_depth` (measured on Flame 2025.2.4 and 2026.2.3, 2026-10-16): only the integers 8, 10,
    12, 16 and 32 are taken and read back as given; any other type is "Wrong input type.", any
    other integer "Bit depth should be either 8, 10, 12, 16 or 32."; omitted = the project's
    default (`PROJECT_BIT_DEPTH` here).
    """
    if in_library(reel):
        raise RuntimeError(LOCKED)
    if frame_rate not in FRAME_RATES:
        raise RuntimeError(f"Frame rate should be one of the following: {', '.join(FRAME_RATES)}.")
    if bit_depth is None:
        bit_depth = PROJECT_BIT_DEPTH
    elif not isinstance(bit_depth, int):
        raise RuntimeError("Wrong input type. Check values property for help.")
    elif bit_depth not in BIT_DEPTHS:
        raise RuntimeError("Bit depth should be either 8, 10, 12, 16 or 32.")
    start = start_at.frame if isinstance(start_at, PyTime) else 0
    uid = next_uid("seq")
    seq = PySequence(
        name,
        wiretap_id=f"{reel.get_wiretap_node_id()}/{name}.{uid}",
        duration=1,
        frame_rate=frame_rate,
        versions=[
            PyVersion([PyTrack([PySegment._new_gap(start, start)]) for _ in range(video_tracks)])
        ],
        uid=uid,
        start_time=PyTime(start),
        width=width,
        height=height,
        bit_depth=bit_depth,
    )
    adopt(reel, [seq])
    reel._children.append(seq)
    _refresh(seq)
    if fmt.ACTIVE_TIMELINE:
        fmt.ACTIVE_TIMELINE[0].clip = seq
    return seq


def install() -> None:
    for owner, name, body in (
        (PySequence, "overwrite", seq_overwrite),
        (PySequence, "insert", seq_insert),
        (PySequence, "cut", seq_cut),
        (PySequence, "create_version", seq_create_version),
        (PyTrack, "cut", track_cut),
        (PyVersion, "create_track", version_create_track),
        (PyReel, "create_sequence", reel_create_sequence),
    ):
        setattr(owner, name, body)


install()
