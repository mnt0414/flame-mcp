"""Fake selection extraction and segment connections, kept in a module of their own.

Adds to the fake Flame tree (`fake_media_tree`) `PySequence.extract_selection_to_media_panel` /
`lift_selection_to_media_panel` and the connections of `PySegment` (`connected_segments`,
`create_connection`, `remove_connection`, `sync_connected_segments`). `PySegment.selected` itself
is in `fake_media_tree`.

Measured on Flame 2025.2.4:
  - extract_selection_to_media_panel(destination) acts on the segments that have `selected` True:
    it REMOVES them and closes the hole by moving the later segments of THAT TRACK left (the other
    tracks stay in place). It returns the new PyClip / PySequence it made of the selection in the
    destination; a selected Gap is closed the same way and what comes back is an empty 0-frame
    sequence, not a clip. Without a destination it returns None.
  - the length of the sequence afterwards (measured on a probe project; the
    sequence had two video tracks and an audio track of one Gap, 120 frames) is the end of the
    longest CONTENT (everything but a Gap, video and audio tracks alike), and the trailing Gap of
    EVERY track follows it, the tracks that were not selected from included:
      1. removing clip2 (48-71) from track 0 = clip1 0-23, Gap 24-47, clip2 48-71, p2_len48
         72-119 left clip1 0-23, Gap 24-47, p2_len48 48-95 (NO Gap at the end: it ends in content
         at the new end), track 1 = p2_len72 0-71 | Gap 72-119 became Gap 72-95, the end of the
         audio Gap went from 119 to 95 and the sequence from 120 to 96 frames;
      2. removing the Gap 24-47 from clip1 0-23, Gap 24-47, p2_len48 48-95 (track 1 = p2_len72
         0-71 | Gap 72-95) left clip1 0-23, p2_len48 24-71, track 1 lost its Gap (it would start
         at 72, the new end) and the sequence became 72 frames;
      3. (a sequence whose audio ran to 720 and decided the length) closing
         a hole in a video track appended a Gap to that track, the sequence stayed at 720 frames
         and the other tracks did not change.
    All three are the same rule. Modelled in `_fit_lengths`.
  - lift_selection_to_media_panel(destination) (measured) removes the selected segment
    and leaves a Gap of the same length where it was; nothing after it moves and the other tracks
    are untouched. Both calls work on a sequence that is not shown in the Timeline, a Gap can be
    selected and extracted (closed), several segments that are not next to each other work in one
    call, and journal.revert after it restored the sequence (the fingerprint matched; the
    connections were kept).
  - replacing the media of one connected segment (smart_replace*) replaces the media of the
    segments connected to it, in other sequences too, and neither journal.revert nor a Timeline
    checkpoint of the target's sequence puts those back. The fake's propagation
    (`fake_segment_edit.smart_replace_with`) is modelled on this measured behaviour.
  - connected_segments() lists the segment itself (measured): a connected pair answers
    2 for each of them, a segment with no connection answers [self] (1).
  - timeline.delete_segments on a copy whose segments were connected to an original left the
    original sequence unchanged (segment count and positions read back), also after the op
    deleted its extract by-product clips. NOT measured: whether flame.delete of a segment deletes
    the segments connected to it (the fake's `delete_track` removes them from their group only).
INVENTED (not measured): the selected audio segments of `audio_tracks` are taken like the video
ones (the call acts on the whole selection of the sequence); selected transitions and markers
are ignored; that the Gap a lift leaves is not merged with its neighbours, an extract that
sets a trailing Gap that is already there to the new end instead of adding a second one, the
length of a sequence that has no content left (it stays as long as it was), a sequence that was
longer than its longest content before the call (every track ends in a Gap: the fake gives it the
same rule, the length becomes the end of the longest content; a real Flame was not measured), a
sequence whose start_time is not 0 (the same rule on absolute record frames), a track that was not
selected and does not end in a Gap that reached the old end (left as it is), a call with a
destination and no selected segment (returns None, changes nothing),
what a connection is made of (`create_connection` connects the segment with the segments that share
its source), the answer
of create_connection / remove_connection / sync_connected_segments (None) and what a sync changes
(the source range, head and tail of the others become the segment's own).
"""

from __future__ import annotations

from typing import Any

import fake_media_tree as fmt
from fake_media_tree import (
    PyClip,
    PySegment,
    PySequence,
    PyTrack,
    PyVersion,
    adopt,
    clone_segment,
    in_library,
    next_uid,
)
from fake_segment_edit import LOCKED, put_into
from fake_timeline_build import _refresh

INCLUDE_SELF = True
"""Whether `connected_segments()` lists the segment itself: it does (measured on Flame 2025.2.4);
False is a test switch only."""
EXTRACT_MOVES_DISPLAY = False
"""When True the new object an extract makes is shown in the Timeline (like a copy of a sequence
can be); a test flips it to cover the display being put back."""


def _is(seg: PySegment, others: list[PySegment]) -> bool:
    return any(seg is other for other in others)


def _picked(seq: PySequence) -> list[tuple[Any, list[PySegment]]]:
    """The tracks (video and audio) that hold selected segments, with those segments: the call
    acts on the whole selection of the sequence."""
    found: list[tuple[Any, list[PySegment]]] = []
    tracks = [t for version in seq._versions for t in version._tracks] + [
        channel for audio in seq.audio_tracks for channel in audio.channels
    ]
    for track in tracks:
        picks = [s for s in track._segments if bool(s._selected.get_value())]
        if picks:
            found.append((track, picks))
    return found


def _all_tracks(seq: PySequence) -> list[Any]:
    return [t for version in seq._versions for t in version._tracks] + [
        channel for audio in seq.audio_tracks for channel in audio.channels
    ]


def _length(seq: PySequence) -> int:
    ends = [s.record_out.frame + 1 for t in _all_tracks(seq) for s in t._segments]
    return max(ends, default=0)


def _ripple_out(track: Any, picks: list[PySegment]) -> list[PySegment]:
    """Removes the picked segments and moves the later ones left (the trailing Gaps are set by
    `_fit_lengths` afterwards)."""
    kept: list[PySegment] = []
    shift = 0
    for seg in track._segments:
        if _is(seg, picks):
            shift += seg.record_duration.frame
        else:
            if shift:
                seg._set_record(seg.record_in.frame - shift, seg.record_out.frame - shift)
            kept.append(seg)
    return kept


def _fit_lengths(seq: PySequence, selected: list[tuple[Any, list[PySegment]]], floor: int) -> None:
    """Measured (see the module docstring): the sequence is as long as the end of the
    longest content of any track, and the trailing Gap of every track follows. A track that ends
    in a Gap has it set to the new end, or loses it when it would start at or after the end; a
    track that was selected and ends in content short of the new end gets a Gap. INVENTED: a
    track that was not selected is only fitted when it ends in a Gap that reached the old end
    (`floor`), any other is left alone; with no content left anywhere the sequence stays `floor`
    frames long."""
    tracks = _all_tracks(seq)
    ends = [s.record_out.frame + 1 for t in tracks for s in t._segments if s.type != "Gap"]
    length = max(ends, default=floor)
    for track in tracks:
        segs = track._segments
        picks = next((p for t, p in selected if t is track), None)
        if not segs and picks is None:
            continue
        last = segs[-1] if segs else None
        if last is not None and last.type == "Gap":
            if picks is None and last.record_out.frame + 1 != floor:
                continue  # INVENTED: a Gap that did not reach the old end is not a trailing Gap
            if last.record_in.frame >= length:
                segs.pop()
            else:
                last._set_record(last.record_in.frame, length - 1)
        elif picks is not None:
            start = last.record_out.frame + 1 if last is not None else picks[0].record_in.frame
            if start < length:
                segs.append(PySegment._new_gap(start, length - 1))
        adopt(track, segs)


def _lift_out(track: Any, picks: list[PySegment]) -> list[PySegment]:
    kept: list[PySegment] = []
    for seg in track._segments:
        if _is(seg, picks) and seg.type != "Gap":
            kept.append(PySegment._new_gap(seg.record_in.frame, seg.record_out.frame))
        else:
            kept.append(seg)
    return kept


def _made_of(seq: PySequence, destination: Any, taken: list[list[PySegment]]) -> Any:
    media = [[s for s in picks if s.type != "Gap"] for picks in taken]
    name = f"{seq.name.get_value()} extract"
    wiretap = f"{destination.get_wiretap_node_id()}/{name}.{next_uid('xt')}"
    if not any(media):
        return PySequence(
            name,
            wiretap_id=wiretap,
            uid=next_uid("seq"),
            duration=0,
            versions=[PyVersion([PyTrack([])])],
        )
    base = min(s.record_in.frame for picks in media for s in picks)
    tracks: list[PyTrack] = []
    for picks in media:
        copies = []
        for seg in picks:
            copy = clone_segment(seg)
            copy._set_record(seg.record_in.frame - base, seg.record_out.frame - base)
            copies.append(copy)
        tracks.append(PyTrack(copies))
    length = max(s.record_out.frame for t in tracks for s in t._segments) + 1
    return PyClip(
        name,
        wiretap_id=wiretap,
        uid=next_uid("clip"),
        duration=length,
        versions=[PyVersion(tracks)],
    )


def take_selection(seq: PySequence, destination: Any, *, ripple: bool) -> Any:
    """extract (ripple) or lift (not) the selected segments into `destination`."""
    if in_library(seq):
        raise RuntimeError(LOCKED)
    if destination is None:
        return None
    picked = _picked(seq)
    if not picked:
        return None
    taken: list[list[PySegment]] = []
    floor = _length(seq)
    for track, picks in picked:
        kept = _ripple_out(track, picks) if ripple else _lift_out(track, picks)
        track._segments[:] = kept  # in place: a one-channel audio track shares the list
        adopt(track, kept)
        taken.append(picks)
    if ripple:
        _fit_lengths(seq, picked, floor)
    made = _made_of(seq, destination, taken)
    _refresh(seq)
    put_into(destination, made)
    if EXTRACT_MOVES_DISPLAY and fmt.ACTIVE_TIMELINE:
        fmt.ACTIVE_TIMELINE[0].clip = made
    return made


def seq_extract(self: PySequence, destination: Any = None, duplicate_action: str = "add") -> Any:
    return take_selection(self, destination, ripple=True)


def seq_lift(self: PySequence, destination: Any = None, duplicate_action: str = "add") -> Any:
    return take_selection(self, destination, ripple=False)


def connect(*segments: PySegment) -> None:
    """Puts the segments (and the groups they are already in) into one connection group."""
    group: list[PySegment] = []
    for seg in segments:
        for member in seg._link_group or [seg]:
            if not _is(member, group):
                group.append(member)
    for member in group:
        member._link_group = group


def seg_connected_segments(self: PySegment) -> list[PySegment]:
    group = self._link_group
    if not group:
        return [self] if INCLUDE_SELF else []
    return [s for s in group if INCLUDE_SELF or s is not self]


def seg_create_connection(self: PySegment) -> Any:
    """Connects the segment with the segments that share its source."""
    connect(self, *[s for s in self.shared_source_segments() if s is not self])
    return None


def seg_remove_connection(self: PySegment) -> Any:
    group = self._link_group
    if not group:
        return None
    group[:] = [s for s in group if s is not self]
    self._link_group = None
    if len(group) == 1:
        group[0]._link_group = None
    return None


def seg_sync_connected_segments(self: PySegment) -> Any:
    for other in seg_connected_segments(self):
        if other is self:
            continue
        other.source_name = self.source_name
        other.source_in = self.source_in
        other.source_out = self.source_out
        other.head = self.head
        other.tail = self.tail
    return None


def install() -> None:
    PySequence.extract_selection_to_media_panel = seq_extract  # type: ignore[method-assign]
    PySequence.lift_selection_to_media_panel = seq_lift  # type: ignore[method-assign]
    PySegment.connected_segments = seg_connected_segments  # type: ignore[method-assign]
    PySegment.create_connection = seg_create_connection  # type: ignore[method-assign]
    PySegment.remove_connection = seg_remove_connection  # type: ignore[method-assign]
    PySegment.sync_connected_segments = seg_sync_connected_segments  # type: ignore[method-assign]


install()
