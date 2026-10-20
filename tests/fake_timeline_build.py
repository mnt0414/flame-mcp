"""Fake timeline-building calls, kept in a module of their own.

Adds to the fake Flame tree (`fake_media_tree`) what 2025.2.4 does for `PySequence.overwrite` /
`insert` / `cut`, `PyTrack.cut`, `PySequence.create_version`, `PyVersion.create_track` and
`PyReel.create_sequence`, and `PyAudioChannel.cut`. Measured on Flame 2025.2.4 unless a docstring
says INVENTED.

A time is `PyTime(frame, seq.frame_rate)`: the ABSOLUTE record frame. record_out counts the last
frame in (24 frames from 0 are 0-23).
"""

from __future__ import annotations

from typing import Any

import fake_media_tree as fmt
from fake_media_tree import (
    FRAME_RATES,
    FakeAttr,
    PyAudioChannel,
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
from fake_segment_edit import clip_media, new_effect, put_into

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
    segment's first frame or outside does nothing. A Gap is split like a segment (measured on
    Flame 2025.2.4: PyTrack.cut(360) on a Gap 106-719 gave 106-359 | 360-719).
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
        type=seg.type,
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
    audio = [
        s
        for a in getattr(seq, "audio_tracks", ())
        if hasattr(a, "_all_segments")
        for s in a._all_segments()
    ]
    ins += [s.record_in.frame for s in audio]  # measured: audio can set the length
    outs += [s.record_out.frame for s in audio]
    if ins:
        seq.duration = PyTime(max(outs) - min(ins) + 1)
    for version in seq._versions:
        version.parent = seq
        for track in version._tracks:
            track.parent = version
            adopt(track, track._segments)


def _has_media(seq: PySequence) -> bool:
    return any(s.type != "Gap" for v in seq._versions for t in v._tracks for s in t._segments)


def _put_audio(seq: PySequence, clip: PyClip, at: int, length: int, *, insert: bool) -> None:
    """INVENTED where it was not measured. Measured on Flame 2025.2.4: an overwrite of a clip that
    has audio onto a video track replaced the audio of the sequence (two channels, the source_name
    of their segments changed). Here: the channel of the same index of the audio track of the same
    index puts a copy of each segment of the clip's channel, moved to `at`; an overwrite first
    drops the segments that lie inside [at, at + length - 1], an insert moves the segments from
    `at` on later by `length`. A sequence audio track the clip has no counterpart for stays."""
    for ai, source_track in enumerate(getattr(clip, "audio_tracks", ())):
        if ai >= len(seq.audio_tracks):
            break
        for ci, source_channel in enumerate(source_track.channels):
            if ci >= len(seq.audio_tracks[ai].channels):
                break
            segs = seq.audio_tracks[ai].channels[ci]._segments
            if insert:
                for seg in segs:
                    if seg.record_in.frame >= at:
                        seg._set_record(seg.record_in.frame + length, seg.record_out.frame + length)
            else:
                segs[:] = [
                    s
                    for s in segs
                    if not (s.record_in.frame >= at and s.record_out.frame <= at + length - 1)
                ]
            for seg in source_channel.segments:
                shift = at - source_channel.segments[0].record_in.frame
                segs.append(
                    PySegment(
                        name=str(seg.name.get_value()),
                        source_name=seg.source_name,
                        record_in=seg.record_in.frame + shift,
                        record_out=seg.record_out.frame + shift,
                        source_in=seg.source_in.frame,
                        source_out=seg.source_out.frame,
                        record_duration=seg.record_duration.frame,
                        type=seg.type,
                    )
                )
            segs.sort(key=lambda s: s.record_in.frame)


def seq_put(seq: PySequence, clip: PyClip, time: Any, track: PyTrack, *, insert: bool) -> Any:
    _locked(seq)
    at = _check_time(time)
    _locate(seq, track)
    if getattr(clip, "_track_copy", False) and not insert:
        _put_track_copy(seq, clip, at, track)
        return True
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
    _put_audio(seq, clip, at, length, insert=insert)
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


def channel_cut(channel: Any, time: Any) -> Any:
    """PyAudioChannel.cut: splits the segment `time` falls inside, like PyTrack.cut (measured
    on the channels of a sequence whose length was shortened). INVENTED: the channel
    belongs to a sequence through its audio track; one that has no sequence is cut all the same."""
    track = channel.parent
    seq = track.parent if track is not None else None
    if seq is not None and in_library(seq):
        raise RuntimeError(LOCKED)
    _split(channel, _check_time(time))
    if seq is not None:
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

    Measured on Flame 2025.2.4: `track_index` is 1-BASED. 1 inserts at the bottom
    (list index 0), k puts the new track at list index k - 1 and the tracks at list index k - 1
    and above move up by one; -1 appends at the top (the same as no index); 0 raises
    RuntimeError 'Invalid track index.'. INVENTED: any other value out of range (below -1, or
    above the track count + 1) raises the same error.
    """
    seq = version.parent
    if seq is None or in_library(seq):
        raise RuntimeError(LOCKED)
    low, high = _span(version._tracks, _start_of(seq))
    track = PyTrack([PySegment._new_gap(low, high)])
    count = len(version._tracks)
    if track_index is None or track_index == -1:
        version._tracks.append(track)
    elif 1 <= track_index <= count + 1:
        version._tracks.insert(track_index - 1, track)
    else:
        raise RuntimeError("Invalid track index.")
    _refresh(seq)
    return track


def track_copy_to_media_panel(track: PyTrack, destination: Any) -> PyClip:
    """Copies the track into a new clip of `destination`.

    Measured on Flame 2025.2.4: the clip holds the track's segments from its FIRST
    media segment to its LAST (internal Gaps kept, the leading and trailing Gap are not in it).
    INVENTED: the record frames in the clip start at 0, the clip is named after the sequence and
    the track, a track with no media gives a clip of no segments, and every copy remembers the
    segment it came from (`_origins`) so a later overwrite can keep the connections.
    """
    seq = track.parent.parent if track.parent is not None else None
    if seq is None or in_library(seq):
        raise RuntimeError(LOCKED)
    media_segs = [s for s in track._segments if s.type != "Gap"]
    if media_segs:
        first = media_segs[0].record_in.frame
        last = media_segs[-1].record_out.frame
        taken = [
            s for s in track._segments if first <= s.record_in.frame and s.record_out.frame <= last
        ]
    else:
        first, last, taken = 0, -1, []
    copies = []
    for seg in taken:
        copy = fmt.clone_segment(seg)
        copy._set_record(seg.record_in.frame - first, seg.record_out.frame - first)
        copies.append(copy)
    name = f"{seq.name.get_value()} track"
    made = PyClip(
        name,
        wiretap_id=f"{destination.get_wiretap_node_id()}/{name}.{next_uid('trk')}",
        uid=next_uid("clip"),
        duration=max(last - first + 1, 0),
        versions=[PyVersion([PyTrack(copies)])],
    )
    made._origins = list(taken)  # type: ignore[attr-defined]
    made._track_copy = True  # type: ignore[attr-defined]
    put_into(destination, made)
    return made


TRACK_COPY_TOUCHES_AUDIO = False
"""INVENTED switch (never seen on Flame): when True, overwriting a copied track also adds an audio
segment to the first channel of the first audio track, so the audio gate of timeline.move_track
can be tested."""


def _put_track_copy(seq: PySequence, clip: PyClip, at: int, track: PyTrack) -> None:
    """INVENTED where it was not measured. Measured: overwriting the clip a track was
    copied to at the track's first media record_in makes the track equal the source row for row
    and keeps connected_segments counts. Here: the clip's segments, moved to `at`, replace what
    the track holds in [at, at + length - 1]; each copy joins the connection group of its origin."""
    length = clip.duration.frame
    _split(track, at)
    _split(track, at + length)
    track._segments = [
        s
        for s in track._segments
        if not (s.record_in.frame >= at and s.record_out.frame <= at + length - 1)
    ]
    origins = getattr(clip, "_origins", [])
    for index, seg in enumerate(clip.versions[0].tracks[0]._segments):
        placed = fmt.clone_segment(seg)
        placed._set_record(seg.record_in.frame + at, seg.record_out.frame + at)
        track._segments.append(placed)
        if index < len(origins) and origins[index]._link_group:
            group = origins[index]._link_group
            group.append(placed)
            placed._link_group = group
    track._segments.sort(key=lambda s: s.record_in.frame)
    adopt(track, track._segments)
    if TRACK_COPY_TOUCHES_AUDIO and seq.audio_tracks and seq.audio_tracks[0].channels:
        seq.audio_tracks[0].channels[0]._segments.append(
            PySegment(
                name="AUD",
                source_name="from the copied track",
                record_in=at,
                record_out=at + length - 1,
                record_duration=length,
                type="Audio Segment",
            )
        )
    _refresh(seq)


def delete_track(track: PyTrack) -> bool:
    """`flame.delete(track, confirm=False)` answers True and removes the track; the other tracks
    are unchanged (measured on Flame 2025.2.4). INVENTED: the segments of the track
    leave their connection groups."""
    version = track.parent
    seq = version.parent if version is not None else None
    if seq is None or in_library(seq):
        raise RuntimeError(LOCKED)
    if not any(t is track for t in version._tracks):
        raise RuntimeError("the track is not a track of this version")
    version._tracks = [t for t in version._tracks if t is not track]
    for seg in track._segments:
        group = seg._link_group
        if group:
            group[:] = [s for s in group if s is not seg]
            seg._link_group = None
            if len(group) == 1:
                group[0]._link_group = None
    _refresh(seq)
    return True


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

    `bit_depth` (measured on Flame 2025.2.4 and 2026.2.3): only the integers 8, 10,
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
        (PyAudioChannel, "cut", channel_cut),
        (PyTrack, "copy_to_media_panel", track_copy_to_media_panel),
        (PyVersion, "create_track", version_create_track),
        (PyReel, "create_sequence", reel_create_sequence),
    ):
        setattr(owner, name, body)


install()
