"""Shared fake Flame object tree for the op tests (no Flame, no socket).

Mirrors the object shapes measured on Flame 2025.2.4:

  - container classes (PyLibrary / PyReel) expose `.children`
  - PySegment/PyTrack/PyVersion genuinely have neither `.uid` nor
    `.get_wiretap_node_id` (not "unset" -- the attribute simply isn't
    defined on the fake, exactly like the real classes)
  - `.name` is `FakeAttr` (get_value()/set_value(), str() quoted unlike repr())
    almost everywhere, except `PyProject.name`, which is a plain str -- both
    shapes are exercised by the op tests via `_v()`
"""

from __future__ import annotations

import itertools
import json
import os
import re
from collections.abc import Iterable
from typing import Any

_UID_SEQ = itertools.count(1)


def next_uid(kind: str) -> str:
    """A fresh uid for an object the fake makes itself (create_batch_group, copy, ...)."""
    return f"fake-{kind}-{next(_UID_SEQ)}"


def uid_of(obj: Any) -> Any:
    """The uid of a fake object (a FakeAttr or a plain string), None when it has none."""
    raw = getattr(obj, "uid", None)
    return raw.get_value() if isinstance(raw, FakeAttr) else raw


def numbered_name(name: str, taken: Iterable[str]) -> str:
    """Returns what Flame makes of a taken reel / reel group name.

    The trailing number goes up (`~RG1` -> `~RG2`, measured on Flame 2025.2.4); the fake appends
    ' 1' to a name with no number."""
    taken_set = set(taken)
    if name not in taken_set:
        return name
    match = re.fullmatch(r"(.*?)(\d+)", name)
    prefix, number = (match.group(1), int(match.group(2))) if match else (f"{name} ", 0)
    while True:
        number += 1
        candidate = f"{prefix}{number}"
        if candidate not in taken_set:
            return candidate


class FakeAttr:
    """Stand-in for Flame's PyAttribute (`__name__` is set to `PyAttribute`
    below: `_v` matches on the type name, not on `hasattr(x, 'get_value')`).
    `values` mirrors PyAttribute.values: the candidate tuple of an enumerated
    attribute, `(min, max)` of a numeric one, or None."""

    def __init__(self, value: Any, values: tuple[Any, ...] | None = None) -> None:
        self._value = value
        self.values = values

    def get_value(self) -> Any:
        return self._value

    def set_value(self, value: Any) -> None:
        self._value = value

    def __str__(self) -> str:
        # str(PyAttribute) is quoted, unlike repr(). Ops must
        # only ever read a name through `_v()`, never `str()`.
        return repr(self._value)

    def __repr__(self) -> str:
        return f"FakeAttr({self._value!r})"


FakeAttr.__name__ = "PyAttribute"


def adopt(parent: Any, children: Iterable[Any]) -> None:
    """Flame answers `parent` on every entry of a container (a clip's is its reel)."""
    for child in children:
        child.parent = parent


def in_library(obj: Any) -> bool:
    """Tells whether the object sits inside a library (up its `parent` chain).

    A library entry cannot be edited: a segment edit there raises `Clip is locked.`, and
    `PySequence.open()` answers False (both measured on Flame 2025.2.4)."""
    current = getattr(obj, "parent", None)
    for _ in range(12):
        if current is None:
            return False
        if type(current).__name__ == "PyLibrary":
            return True
        current = getattr(current, "parent", None)
    return False


class PyTimeline:
    """`flame.timeline`: `clip` is the sequence the Timeline shows.

    It is None when the Timeline shows nothing (measured on Flame 2025.2.4: after the shown
    sequence is deleted)."""

    def __init__(self) -> None:
        self.clip: Any = None
        self.copy_moves_display_until_open = False
        """The behaviour of a production project (measured): until a sequence has
        been opened through the API (`PySequence.open()`), every `media_panel.copy` of a sequence
        moves the Timeline to the new copy. Off by default (what the other projects do)."""
        self.duplicate_moves_display_until_open = False
        """The same for `flame.duplicate` of a sequence (not measured: for the guard's test)."""
        self.opened_by_api = False

    def show_new_copy(self, made: Any, flag: bool) -> None:
        if flag and not self.opened_by_api and type(made).__name__ == "PySequence":
            self.clip = made


ACTIVE_TIMELINE: list[PyTimeline] = []
"""The Timeline of the FakeFlame built last (`PySequence.open()` points it at the sequence)."""
ACTIVE_FLAME: list[Any] = []
"""The FakeFlame built last: a clip that gets a new wiretap id makes it forget the old one."""


class PyLibrary:
    def __init__(self, name: str, uid: str, wiretap_id: str, children: Iterable[Any] = ()) -> None:
        self.name = FakeAttr(name)
        self.uid = FakeAttr(uid)
        self._wiretap_id = wiretap_id
        self._children = list(children)
        self.attributes = ["name", "uid"]
        self.parent: Any = None
        adopt(self, self._children)

    @property
    def children(self) -> list[Any]:
        return list(self._children)

    @property
    def reels(self) -> list[Any]:
        return [c for c in self._children if type(c).__name__ == "PyReel"]

    @property
    def clips(self) -> list[Any]:
        return [c for c in self._children if type(c).__name__ == "PyClip"]

    @property
    def sequences(self) -> list[Any]:
        return [c for c in self._children if type(c).__name__ == "PySequence"]

    def get_wiretap_node_id(self) -> str:
        return self._wiretap_id

    def create_folder(self, name: str) -> PyFolder:
        folder = PyFolder(name, next_uid("folder"), f"{self._wiretap_id}/{name}")
        adopt(self, [folder])
        self._children.append(folder)
        return folder

    def create_reel(self, name: str) -> PyReel:
        """A reel at the end of the library's children (a taken name goes up by a number)."""
        unique = numbered_name(name, [str(r.name.get_value()) for r in self.reels])
        reel = PyReel(unique, next_uid("reel"), f"{self._wiretap_id}/{unique}", reel_type="Reel")
        adopt(self, [reel])
        self._children.append(reel)
        return reel

    def receive(self, sources: list[Any]) -> list[Any]:
        """What a copy into a library does in the fake (it was not measured on a real Flame)."""
        copies: list[Any] = []
        for source in sources:
            name = str(source.name.get_value())
            copy = copy_media(
                source, name, f"{self._wiretap_id}/{name}.{next_uid('c')}", next_uid("clip")
            )
            adopt(self, [copy])
            self._children.append(copy)
            copies.append(copy)
        return copies


class PyReel:
    """A reel of the Desktop, a library or a Batch group.

    `reel_type` ('Schematic' / 'Shelf') and `owner` (the PyBatch) are set for a reel of a Batch
    group (`reel.type` answers 'Schematic' / 'Shelf' on Flame 2025.2.4); a Desktop / Library reel
    has neither. `receive` is what `flame.media_panel.copy(clips, reel)` does to the destination."""

    def __init__(
        self,
        name: str,
        uid: str,
        wiretap_id: str,
        children: Iterable[Any] = (),
        *,
        reel_type: str | None = None,
        owner: Any = None,
    ) -> None:
        self.name = FakeAttr(name)
        self.uid = FakeAttr(uid)
        self._wiretap_id = wiretap_id
        self._children = list(children)
        self.attributes = ["name", "uid"]
        self.type = reel_type
        self.owner = owner
        self._copies = 0
        self.parent: Any = None
        adopt(self, self._children)

    @property
    def children(self) -> list[Any]:
        return list(self._children)

    @property
    def clips(self) -> list[Any]:
        return [c for c in self._children if type(c).__name__ == "PyClip"]

    @property
    def sequences(self) -> list[Any]:
        """A sequence copied into a reel lands here, not in `clips` (measured)."""
        return [c for c in self._children if type(c).__name__ == "PySequence"]

    def get_wiretap_node_id(self) -> str:
        return self._wiretap_id

    def create_sequence(self, name: str, **kwargs: Any) -> PySequence:
        raise NotImplementedError

    def receive(self, sources: list[Any]) -> list[Any]:
        copies: list[Any] = []
        for source in sources:
            self._copies += 1
            name = str(source.name.get_value())
            copy = copy_media(
                source, name, f"{self._wiretap_id}/copy{self._copies}", next_uid("clip")
            )
            adopt(self, [copy])
            self._children.append(copy)
            copies.append(copy)
            if self.owner is not None and self.type == "Schematic":
                self.owner.add_clip_node(name, copy)
        return copies


class PyFolder:
    """A folder of a library.

    It holds clips, sequences, reels and folders like a library does. A copy / import / move
    into it lands in `_children`."""

    def __init__(self, name: str, uid: str, wiretap_id: str, children: Iterable[Any] = ()) -> None:
        self.name = FakeAttr(name)
        self.uid = FakeAttr(uid)
        self._wiretap_id = wiretap_id
        self._children = list(children)
        self.attributes = ["name", "uid"]
        self.parent: Any = None
        adopt(self, self._children)

    @property
    def children(self) -> list[Any]:
        return list(self._children)

    @property
    def reels(self) -> list[Any]:
        return [c for c in self._children if type(c).__name__ == "PyReel"]

    @property
    def clips(self) -> list[Any]:
        return [c for c in self._children if type(c).__name__ == "PyClip"]

    @property
    def sequences(self) -> list[Any]:
        return [c for c in self._children if type(c).__name__ == "PySequence"]

    def get_wiretap_node_id(self) -> str:
        return self._wiretap_id

    def receive(self, sources: list[Any]) -> list[Any]:
        copies: list[Any] = []
        for source in sources:
            name = str(source.name.get_value())
            copy = copy_media(
                source, name, f"{self._wiretap_id}/{name}.{next_uid('c')}", next_uid("clip")
            )
            adopt(self, [copy])
            self._children.append(copy)
            copies.append(copy)
        return copies


FRAME_RATES = (
    "23.976 fps",
    "24 fps",
    "25 fps",
    "29.97 fps DF",
    "29.97 fps NDF",
    "30 fps",
    "50 fps",
    "59.94 fps DF",
    "59.94 fps NDF",
    "60 fps",
)


class PyTime:
    """Flame's PyTime, as measured on Flame 2025.2.4.

    `.frame` is the ABSOLUTE frame -- the first segment of a sequence that starts at 01:00:00:00
    has 86400 -- and `.timecode` reads 'hh:mm:ss+ff' (a negative frame gets a '-', as the measured
    '-00:00:00+01'). `relative_frame` is `frame + 1`. Every time a real PySegment or a real
    sequence answers (record_in, source_in, duration, ...) is one of these, never an int."""

    def __init__(self, frame: Any, fps: Any = 24) -> None:
        """Builds a relative or an absolute time.

        `PyTime(frame)` is relative; `PyTime(frame, '23.976 fps')` and `PyTime('01:00:00:00',
        '23.976 fps')` are absolute (`PyTime(frame, seq.frame_rate)` is what overwrite / insert /
        cut take). An int `fps` is only the rate the timecode text is read at.
        """
        rate = fps if isinstance(fps, str) else "23.976 fps"
        nominal = round(float(rate.split()[0])) if isinstance(fps, str) else int(fps)
        if isinstance(fps, str) and fps not in FRAME_RATES:
            raise RuntimeError(
                f"Frame rate should be one of the following: {', '.join(FRAME_RATES)}."
            )
        if isinstance(frame, str):
            hours, minutes, seconds, frames = (int(part) for part in frame.split(":"))
            frame = ((hours * 60 + minutes) * 60 + seconds) * nominal + frames
        self.frame = frame
        self.frame_rate = rate
        self.relative_frame = frame + 1
        self._fps = nominal

    @property
    def timecode(self) -> str:
        sign = "-" if self.frame < 0 else ""
        n = abs(self.frame)
        fps = self._fps
        return (
            f"{sign}{n // (3600 * fps):02d}:{n // (60 * fps) % 60:02d}:"
            f"{n // fps % 60:02d}+{n % fps:02d}"
        )

    def __repr__(self) -> str:
        return self.timecode

    def __str__(self) -> str:
        return repr(self.timecode)  # str(PyTime) is quoted


def as_time(value: Any) -> PyTime:
    return value if isinstance(value, PyTime) else PyTime(int(value))


EFFECT_TYPES = [
    "Source Colour Mgmt",
    "Source Image",
    "Source Look",
    "Source Pybox",
    "2D Transform",
    "Action",
    "Blur",
    "Burn-in Letterbox",
    "Colour Correct",
    "Flip",
    "Resize",
    "Timewarp",
]


class PyTimelineFX:
    """A timeline effect; `type` is a plain str.

    `attributes` lists `bypass` and, for a type that has them, its own (Burn-in Letterbox: mode /
    ratio / colour). Each is a PyAttribute."""

    mode: FakeAttr  # Burn-in Letterbox only (fake_segment_edit.letterbox_attributes)
    ratio: FakeAttr
    colour: FakeAttr

    def __init__(self, fx_type: str, attributes: dict[str, Any] | None = None) -> None:
        self.type = fx_type
        self.hidden: dict[str, Any] = {}  # parameters that are not attributes
        self.bypass = FakeAttr(False)
        self.attributes = ["bypass"]
        for key, value in (attributes or {}).items():
            setattr(self, key, FakeAttr(value))
            self.attributes.append(key)
        self.parent: Any = None

    # the bodies are put on the class by fake_segment_edit (imported at the bottom)
    def save_setup(self, path: str) -> bool:
        raise NotImplementedError

    def load_setup(self, path: str) -> bool:
        raise NotImplementedError


class PySegment:
    """A segment of a track.

    PySegment has neither `.uid` nor `.get_wiretap_node_id` -- both are simply absent from this
    class, not merely returning None. record_in / record_out / source_in / source_out /
    record_duration are PyTime (an int given here is wrapped, as Flame would answer it).
    record_out and source_out are the LAST frame of the range, not the one after it (measured on
    Flame 2025.2.4: 24 frames from record 0 are 0-23), so a length is `out - in + 1`. `parent` is
    the PyTrack, set when the track is read.
    `shared_source_segments()` answers whatever `share_sources` put in `_shared` (the real one
    walks the project; the fake is told)."""

    def __init__(
        self,
        name: str = "seg",
        source_name: str = "src",
        record_in: Any = 0,
        record_out: Any = 9,
        source_in: Any = 0,
        source_out: Any = 9,
        record_duration: Any = 10,
        source_width: int = 1920,
        source_height: int = 1080,
        source_frame_rate: str = "23.976 fps",
        type: str = "Video Segment",
        head: Any = 0,
        tail: Any = 0,
        effects: Iterable[PyTimelineFX] = (),
        source_bit_depth: int | None = None,
    ) -> None:
        self.name = FakeAttr(name)
        self.type = type  # plain str on a real PySegment (not an attribute key, api graph)
        self.head = head  # int, or the str "infinite" for a Gap
        self.tail = tail
        self.source_name = source_name  # plain str, like the real PySegment
        self.record_in = as_time(record_in)
        self.record_out = as_time(record_out)
        self.source_in = as_time(source_in)
        self.source_out = as_time(source_out)
        self.record_duration = as_time(record_duration)
        self.source_width = source_width
        self.source_height = source_height
        self.source_frame_rate = source_frame_rate
        self.source_bit_depth = source_bit_depth  # None = not known (a real segment always has it)
        # what Flame lists besides `name`: values that are tuples / lists, which an op must hand
        # back as structure ([0.0, 0.0, 0.0], []), not as their repr
        self.attributes = ["name", "colour", "tags", "selected_markers"]
        self.colour = FakeAttr((0.0, 0.0, 0.0))
        self.tags = FakeAttr([])
        self.selected_markers = FakeAttr([])
        fx_list = list(effects)
        self.effects = fx_list
        self.effect_types = list(EFFECT_TYPES)
        self.parent: Any = None
        self._shared: list[PySegment] = []
        self.trim_calls: list[tuple[str, int, bool]] = []
        self._slip_out_rounds = False
        self._selected = FakeAttr(False)
        self._link_group: list[PySegment] | None = None
        for fx in fx_list:
            fx.parent = self

    def shared_source_segments(self) -> list[PySegment]:
        return list(self._shared)

    selected_is_attribute = True
    """`selected` answers a PyAttribute (True) or a plain bool (False): not recorded which one
    Flame's PySegment does, so both are covered. Either way it takes `seg.selected = x`, and the
    attribute also takes `seg.selected.set_value(x)`."""

    @property
    def selected(self) -> Any:
        return self._selected if self.selected_is_attribute else bool(self._selected.get_value())

    @selected.setter
    def selected(self, value: Any) -> None:
        self._selected.set_value(bool(value))

    def speed(self) -> float | None:
        """Returns the speed of a Timewarp made in the UI, None when the segment is 1:1.

        Measured on Flame 2025.2.4: a 0.8 Timewarp shows 35 source frames for 44 record frames.
        The fake keeps the speed in the effect's `hidden["speed"]`."""
        for fx in self.effects:
            if fx.type == "Timewarp" and fx.hidden.get("speed") is not None:
                return float(fx.hidden["speed"])
        return None

    def source_step(self, n: int, *, rounds: bool = True) -> int:
        """Returns how far the SOURCE moves for `n` record frames.

        Measured on Flame 2025.2.4 at speed 0.8: 3 -> 2, 2 -> 1 or 2. Head, tail and the record side
        always move by `n`. The rounding is not known from two examples: a trim rounds to the
        nearest, a slip's source_out truncates (`rounds` False) unless the segment says
        `_slip_out_rounds` (the other case measured)."""
        speed = self.speed()
        if speed is None:
            return n
        scaled = abs(n) * speed
        moved = round(scaled) if rounds else int(scaled)
        return moved if n > 0 else -moved

    # the bodies are put on the class by fake_segment_edit (imported at the bottom)
    def slip(self, offset: int, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError

    def smart_replace_media(self, clip: Any) -> None:
        raise NotImplementedError

    def smart_replace(self, clip: Any) -> None:
        raise NotImplementedError

    def create_effect(self, fx_type: str) -> PyTimelineFX:
        raise NotImplementedError

    def copy_to_media_panel(self, destination: Any) -> Any:
        raise NotImplementedError

    def match(self, destination: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    # the bodies are put on the class by fake_selection_connections (imported at the bottom)
    def connected_segments(self) -> list[PySegment]:
        raise NotImplementedError

    def create_connection(self) -> Any:
        raise NotImplementedError

    def remove_connection(self) -> Any:
        raise NotImplementedError

    def sync_connected_segments(self) -> Any:
        raise NotImplementedError

    def trim_head(self, offset: int, ripple: bool = False, sync: bool = False) -> bool:
        return self._trim("head", offset, ripple)

    def trim_tail(self, offset: int, ripple: bool = False, sync: bool = False) -> bool:
        return self._trim("tail", offset, ripple)

    def _set_record(self, record_in: int, record_out: int) -> None:
        self.record_in = as_time(record_in)
        self.record_out = as_time(record_out)
        self.record_duration = as_time(record_out - record_in + 1)
        if self.type == "Gap":
            self.source_out = as_time(record_out - record_in)  # a Gap's source starts at 0

    def _trim(self, edge: str, offset: int, ripple: bool) -> bool:
        """Trims one edge as Flame 2025.2.4 does.

        The return value is False whatever happened. n > 0
        shortens, n < 0 extends as far as the handle goes. A head trim moves source_in and head by
        n, a tail trim source_out (-n) and tail (+n); a tail trim always moves record_out by -n, a
        head trim record_in by +n without ripple and record_out by -n with ripple. Without ripple
        a Gap takes the freed frames (made, or grown) before the head / after the tail; with
        ripple the segments after it in the SAME track shift by -n and a Gap of n closes the track
        up, other tracks do not move. Extending a segment into frames that are not a Gap was not
        measured: the fake changes nothing then. A segment inside a library raises `Clip is
        locked.`."""
        track = self.parent
        if track is None or in_library(track):
            raise RuntimeError("Clip is locked.")
        self.trim_calls.append((edge, offset, ripple))
        handle = self.head if edge == "head" else self.tail
        n = offset
        if n < 0 and handle != "infinite":
            n = -min(-n, int(handle))
        if n == 0:
            return False
        segs: list[PySegment] = track._segments
        i = next(k for k, seg in enumerate(segs) if seg is self)
        rin, rout = self.record_in.frame, self.record_out.frame
        before = segs[i - 1] if i > 0 and segs[i - 1].type == "Gap" else None
        after = segs[i + 1] if i + 1 < len(segs) and segs[i + 1].type == "Gap" else None
        if not ripple and n < 0:
            room = before if edge == "head" else after
            if room is None or room.record_duration.frame < -n:
                return False
        if edge == "head":
            self.source_in = as_time(self.source_in.frame + self.source_step(n))
            if self.head != "infinite":
                self.head = int(self.head) + n
            new_in, new_out = (rin, rout - n) if ripple else (rin + n, rout)
        else:
            self.source_out = as_time(self.source_out.frame - self.source_step(n))
            if self.tail != "infinite":
                self.tail = int(self.tail) + n
            new_in, new_out = rin, rout - n
        self._set_record(new_in, new_out)
        if ripple:
            self._ripple(segs, i, n)
        else:
            self._free_frames(segs, i, edge, n, rin, rout, before, after)
        return False

    @staticmethod
    def _new_gap(record_in: int, record_out: int) -> PySegment:
        return PySegment(
            name="",
            source_name="",
            record_in=record_in,
            record_out=record_out,
            source_in=0,
            source_out=record_out - record_in,
            record_duration=record_out - record_in + 1,
            type="Gap",
            head="infinite",
            tail="infinite",
        )

    def _free_frames(
        self,
        segs: list[PySegment],
        i: int,
        edge: str,
        n: int,
        rin: int,
        rout: int,
        before: PySegment | None,
        after: PySegment | None,
    ) -> None:
        """Without ripple: a Gap takes the `n` frames the trim freed (made, or grown) or gives
        back the `-n` frames an extension used (removed when none is left)."""
        if edge == "head":
            if n > 0 and before is None:
                segs.insert(i, self._new_gap(rin, rin + n - 1))
            elif before is not None:
                before._set_record(before.record_in.frame, before.record_out.frame + n)
                if before.record_duration.frame == 0:
                    segs.remove(before)
        elif n > 0 and after is None:
            segs.insert(i + 1, self._new_gap(rout - n + 1, rout))
        elif after is not None:
            after._set_record(after.record_in.frame - n, after.record_out.frame)
            if after.record_duration.frame == 0:
                segs.remove(after)

    def _ripple(self, segs: list[PySegment], i: int, n: int) -> None:
        """With ripple: the segments after this one in the SAME track follow by -n, and the end of
        the track keeps its place (a Gap of n closes it up, or the Gap that was there shrinks)."""
        for seg in segs[i + 1 :]:
            seg._set_record(seg.record_in.frame - n, seg.record_out.frame - n)
        last = segs[-1]
        has_gap_end = last.type == "Gap" and last is not self
        if n > 0:
            if has_gap_end:
                last._set_record(last.record_in.frame, last.record_out.frame + n)
            else:
                segs.append(self._new_gap(last.record_out.frame + 1, last.record_out.frame + n))
        elif has_gap_end and last.record_duration.frame >= -n:
            last._set_record(last.record_in.frame, last.record_out.frame + n)
            if last.record_duration.frame == 0:
                segs.remove(last)


def share_sources(*segments: PySegment) -> None:
    """Make every segment answer the same `shared_source_segments()` (itself included)."""
    group = list(segments)
    for seg in group:
        seg._shared = group


class PyTransition:
    """A transition as PyTrack.transitions lists it. Measured: a Cut of duration 0,
    in_offset 0, alignment 'Custom'. `type` is a PyAttribute here; a test can pass a plain value
    for either field."""

    def __init__(
        self,
        kind: Any = "Cut",
        duration: Any = 0,
        in_offset: int = 0,
        alignment: str = "Custom",
    ) -> None:
        self.type = FakeAttr(kind)
        self.duration = duration
        self.in_offset = in_offset
        self.alignment = alignment


class PyTrack:
    def __init__(self, segments: Iterable[PySegment] = ()) -> None:
        self._segments = list(segments)
        self.attributes: list[str] = []
        self.name = FakeAttr("*")  # the name of a track Flame makes
        # PyTrack attributes measured on Flame 2025.2.4: name, hidden, locked, stereo_linked,
        # selected_segments, selected_transitions, selection. The defaults here are INVENTED.
        self.hidden = FakeAttr(False)
        self.locked = FakeAttr(False)
        self.stereo_linked = FakeAttr(False)
        self._transitions: list[Any] | None = None  # None: a Cut at every segment boundary
        self._selected_transitions: list[Any] = []
        self.parent: Any = None
        adopt(self, self._segments)

    @property
    def selected_transitions(self) -> Any:
        return selection_member(self._selected_transitions)

    @property
    def selected_segments(self) -> Any:
        return selection_member([s for s in self._segments if s._selected.get_value()])

    @property
    def segments(self) -> list[PySegment]:
        for seg in self._segments:
            seg.parent = self
        return list(self._segments)

    @property
    def transitions(self) -> list[Any]:
        """Measured on Flame 2025.2.4: PyTrack.transitions lists a PyTransition at
        EVERY segment boundary, type 'Cut', duration 0, in_offset 0, alignment 'Custom' (a track
        of 13 segments lists 12, one of 4 lists 3). A test can set the list."""
        if self._transitions is not None:
            return list(self._transitions)
        return [PyTransition() for _ in self._segments[1:]]

    @transitions.setter
    def transitions(self, value: list[Any]) -> None:
        self._transitions = list(value)

    # the bodies are put on the class by fake_timeline_build (imported at the bottom)
    def cut(self, time: Any) -> Any:
        raise NotImplementedError

    def copy_to_media_panel(self, destination: Any) -> Any:
        raise NotImplementedError


SELECTION_MEMBERS_AS_ATTRIBUTES = True
"""`selected_segments`, `selected_transitions` and `selected_markers` (PySequence, PyTrack,
PyAudioTrack) are PyAttributes on Flame 2025.2.4: iterating one raises TypeError and `get_value()`
holds the list (measured). False answers the plain list (a test switch only)."""


def selection_member(values: list[Any]) -> Any:
    return FakeAttr(list(values)) if SELECTION_MEMBERS_AS_ATTRIBUTES else list(values)


class PyAudioChannel:
    """A channel of an audio track: the only place an audio track's segments are listed
    (`seq.audio_tracks[].channels[].segments`, measured on Flame 2025.2.4).

    Measured on Flame 2025.2.4 (`seq.audio_tracks[0].channels[0]`): the type of a real
    channel is named `PyTrack` (this class keeps the name PyAudioChannel; nothing reads the type
    name), its public members are attributes, copy_to_media_panel, cut, insert_transition, parent,
    segments and transitions, and `transitions` is a list (`[]` for a channel with one segment).
    Modelled here: `segments`, `parent`, `transitions` and (in fake_timeline_build) `cut`; the other
    members are not. INVENTED: what `transitions` lists for a channel with two or more segments
    (a Cut of duration 0 at every boundary, as PyTrack.transitions does; not measured for audio)."""

    def __init__(self, segments: list[PySegment] | None = None) -> None:
        self._segments: list[PySegment] = segments if segments is not None else []
        self._transitions: list[Any] | None = None  # None: a Cut at every segment boundary
        self.parent: Any = None

    @property
    def segments(self) -> list[PySegment]:
        for seg in self._segments:
            seg.parent = self
        return list(self._segments)

    @property
    def transitions(self) -> list[Any]:
        """A list, as on Flame 2025.2.4 (measured: `[]` for a channel with one segment). INVENTED
        for two or more segments: the same as PyTrack.transitions (a Cut of duration 0 at every
        segment boundary). A test can set the list."""
        if self._transitions is not None:
            return list(self._transitions)
        return [PyTransition() for _ in self._segments[1:]]

    @transitions.setter
    def transitions(self, value: list[Any]) -> None:
        self._transitions = list(value)

    # the body is put on the class by fake_timeline_build (imported at the bottom)
    def cut(self, time: Any) -> Any:
        raise NotImplementedError


class PyAudioTrack:
    """An audio track. It has no `segments` of its own in the API: what it exposes of its segments
    is `selected_segments` (and `selected_transitions`) and its `channels`, each with `segments`.
    Given only `segments`, the track has one channel that holds that very list."""

    def __init__(
        self, segments: Iterable[PySegment] = (), channels: Iterable[PyAudioChannel] | None = None
    ) -> None:
        self._segments = list(segments)
        self._channels = (
            list(channels) if channels is not None else [PyAudioChannel(self._segments)]
        )
        self._selected_transitions: list[Any] = []
        self.attributes: list[str] = []
        self.parent: Any = None
        adopt(self, self._segments)
        adopt(self, self._channels)

    @property
    def channels(self) -> list[PyAudioChannel]:
        return list(self._channels)

    def _all_segments(self) -> list[PySegment]:
        """The segments of every channel (the track lists them only through its channels)."""
        return [seg for channel in self._channels for seg in channel._segments]

    @property
    def selected_segments(self) -> Any:
        return selection_member([seg for seg in self._all_segments() if seg._selected.get_value()])

    @property
    def selected_transitions(self) -> Any:
        return selection_member(self._selected_transitions)


class PyVersion:
    def __init__(self, tracks: Iterable[PyTrack] = ()) -> None:
        self._tracks = list(tracks)
        self.attributes: list[str] = []
        self.parent: Any = None
        adopt(self, self._tracks)

    @property
    def tracks(self) -> list[PyTrack]:
        for track in self._tracks:
            track.parent = self
        return list(self._tracks)

    def create_track(self, track_index: int | None = None) -> PyTrack:
        raise NotImplementedError


class PySequence:
    """Fake sequence with the attributes the timeline.* ops read.

    The shape is name/duration/frame_rate/versions. `PyClip` below is a bare subclass so
    `type(obj).__name__` differs while reusing this shape verbatim (both are real
    clip/sequence-shaped objects in Flame's own API). `duration` is a PyTime (an int is
    wrapped); `start_time` is a PyAttribute, the rest plain values; `parent` is the reel.
    """

    def __init__(
        self,
        name: str,
        wiretap_id: str,
        duration: Any = 100,
        frame_rate: str = "23.976 fps",
        versions: Iterable[PyVersion] | None = None,
        uid: str | None = None,
        *,
        start_frame: int = 1,
        start_time: Any = None,
        width: int = 1920,
        height: int = 1080,
        ratio: float = 1.7777777777777777,
        bit_depth: int = 10,
        scan_mode: str = "P",
        cached: str = "Cached",
        unlinked: str = "",
        audio_tracks: Iterable[Any] = (),
    ) -> None:
        self.name = FakeAttr(name)
        self.uid = FakeAttr(uid if uid is not None else next_uid("seq"))
        self._wiretap_id = wiretap_id
        self.duration = as_time(duration)
        self.frame_rate = frame_rate
        self._versions = list(versions) if versions is not None else [PyVersion([PyTrack([])])]
        self.attributes = [
            "name",
            "uid",
            "start_time",
            "primary_track",
            "secondary_track",
            "subtitles_track",
            "selected_segments",
        ]
        self.secondary_track: Any = None  # null on a real sequence
        self.subtitles_track: Any = None
        self.start_frame = start_frame
        self.start_time = FakeAttr(start_time if start_time is not None else PyTime(0))
        self.width = width
        self.height = height
        self.ratio = ratio
        self.bit_depth = bit_depth
        self.scan_mode = scan_mode
        self.cached = cached
        self.unlinked = unlinked
        self.audio_tracks = list(audio_tracks)
        self._selected_transitions: list[Any] = []  # what the API reports as selected
        self._selected_markers: list[Any] = []
        self.in_mark: PyTime | None = None  # set by `match`
        self.out_mark: PyTime | None = None
        self._media: tuple[int, int] | None = None  # (first source frame, length), see
        # fake_segment_edit's media model; None = derived from the first segment
        self.parent: Any = None
        adopt(self, self._versions)

    @property
    def versions(self) -> list[PyVersion]:
        for version in self._versions:
            version.parent = self
        return list(self._versions)

    @property
    def primary_track(self) -> Any:
        """A PyTrack wrapper (its repr carries an address, which is what an op must not return)."""
        tracks = self.versions[0].tracks if self._versions else []
        return tracks[0] if tracks else None

    @property
    def selected_segments(self) -> Any:
        """The segments whose `selected` is True, video and audio (a PyAttribute, see
        `SELECTION_MEMBERS_AS_ATTRIBUTES`)."""
        video = [s for ver in self.versions for trk in ver.tracks for s in trk.segments]
        audio = [s for track in self.audio_tracks for s in track._all_segments()]
        return selection_member([seg for seg in [*video, *audio] if seg._selected.get_value()])

    @property
    def selected_transitions(self) -> Any:
        found = list(self._selected_transitions)
        for ver in self.versions:
            for trk in ver._tracks:
                found += trk._selected_transitions
        for track in self.audio_tracks:
            found += track._selected_transitions
        return selection_member(found)

    @property
    def selected_markers(self) -> Any:
        return selection_member(self._selected_markers)

    def get_wiretap_node_id(self) -> str:
        return self._wiretap_id

    def open(self) -> bool:
        """Shows the sequence in the Timeline and answers True.

        A sequence inside a library is not opened (False, nothing changes; measured on Flame
        2025.2.4)."""
        if in_library(self) or not ACTIVE_TIMELINE:
            return False
        ACTIVE_TIMELINE[0].clip = self
        ACTIVE_TIMELINE[0].opened_by_api = True
        return True

    def overwrite(self, clip: Any, time: Any, track: Any) -> Any:
        raise NotImplementedError

    def insert(self, clip: Any, time: Any, track: Any) -> Any:
        raise NotImplementedError

    def cut(self, time: Any) -> Any:
        raise NotImplementedError

    def create_version(self) -> PyVersion:
        raise NotImplementedError

    # the bodies are put on the class by fake_selection_connections (imported at the bottom)
    def extract_selection_to_media_panel(
        self, destination: Any = None, duplicate_action: str = "add"
    ) -> Any:
        raise NotImplementedError

    def lift_selection_to_media_panel(
        self, destination: Any = None, duplicate_action: str = "add"
    ) -> Any:
        raise NotImplementedError


REFORMAT_RESIZE_MODES = ("Letterbox", "Crop Edges", "Fill", "Centre")
REFORMAT_FRAME_RATES = ("23.976 fps", "24 fps", "25 fps", "29.97 fps NDF", "30 fps", "60 fps")


class PyClip(PySequence):
    """A clip. `reformat` and `change_start_frame` follow what was measured on Flame 2025.2.4
    (the signature, the ranges, `None` as the answer, the change in place: no new clip, but the
    clip gets a NEW uid and wiretap id when a reformat changes it -- `reformat_changes_uid`).
    `reformat_behavior` scripts a failure: "apply" (default), "noop" (answers, changes
    nothing), "raise", "wrong_size" (applies a width two wider). `start_frame_behavior` is "apply",
    "noop" or "raise"."""

    open = None  # type: ignore[assignment]  # PyClip has no open() (PyClip.open_as_sequence does)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reformat_calls: list[dict[str, Any]] = []
        self.start_frame_calls: list[tuple[int, bool]] = []
        self.reformat_behavior = "apply"
        self.start_frame_behavior = "apply"
        self.reformat_changes_uid = True
        """Flame 2025.2.4: a reformat that changes the clip gives it a new uid and wiretap id."""
        self.reformat_resets_start_frame: int | None = None
        """When set, a reformat that applies also moves the start frame to this value."""
        self.copy_overrides: dict[str, Any] = {}
        """Attributes a copy of this clip gets instead of the clip's own (a copy that differs)."""

    def reformat(
        self,
        width: int = 0,
        height: int = 0,
        ratio: float = 0.0,
        bit_depth: int = 0,
        scan_mode: str = "",
        frame_rate: str = "",
        resize_mode: str = "Letterbox",
    ) -> None:
        self.reformat_calls.append(
            {
                "width": width,
                "height": height,
                "ratio": ratio,
                "bit_depth": bit_depth,
                "scan_mode": scan_mode,
                "frame_rate": frame_rate,
                "resize_mode": resize_mode,
            }
        )
        if self.reformat_behavior == "raise":
            raise RuntimeError("scripted reformat failure")
        if resize_mode not in REFORMAT_RESIZE_MODES:
            raise RuntimeError(f"Invalid resize_mode {resize_mode!r}")
        if (width and not 24 <= width <= 16384) or (height and not 24 <= height <= 16384):
            raise RuntimeError("width and height must be 24 to 16384")
        if bit_depth not in (0, 8, 10, 12, 16, 32):
            raise RuntimeError(f"Invalid bit_depth {bit_depth!r}")
        if scan_mode not in ("", "F1", "F2", "P"):
            raise RuntimeError(f"Invalid scan_mode {scan_mode!r}")
        if frame_rate and frame_rate not in REFORMAT_FRAME_RATES:
            raise RuntimeError(f"Invalid frame_rate {frame_rate!r}")
        if self.reformat_behavior == "noop":
            return
        if width:
            self.width = width + (2 if self.reformat_behavior == "wrong_size" else 0)
        if height:
            self.height = height
        if ratio:
            self.ratio = ratio
        elif width and height:
            self.ratio = width / height
        if bit_depth:
            self.bit_depth = bit_depth
        if scan_mode:
            self.scan_mode = scan_mode
        if frame_rate:
            self.frame_rate = frame_rate
        if self.reformat_resets_start_frame is not None:
            self.start_frame = self.reformat_resets_start_frame
        if self.reformat_changes_uid:
            self._take_new_identity()

    def _take_new_identity(self) -> None:
        if ACTIVE_FLAME:
            ACTIVE_FLAME[0].forget_wiretap(self)
        self.uid.set_value(next_uid("reformatted"))
        parent_id = self.parent.get_wiretap_node_id()
        self._wiretap_id = f"{parent_id}/{self.name.get_value()}.{next_uid('rf')}"

    def change_start_frame(self, start_frame: int, use_segment_connections: bool = True) -> None:
        self.start_frame_calls.append((start_frame, use_segment_connections))
        if self.start_frame_behavior == "raise":
            raise RuntimeError("scripted change_start_frame failure")
        if self.start_frame_behavior == "apply":
            self.start_frame = start_frame


def clone_effect(fx: PyTimelineFX) -> PyTimelineFX:
    attributes = {k: getattr(fx, k).get_value() for k in fx.attributes if k != "bypass"}
    made = PyTimelineFX(fx.type, attributes)
    made.bypass.set_value(fx.bypass.get_value())
    return made


def clone_segment(seg: PySegment) -> PySegment:
    """Copies a segment with everything a copy of a sequence carries over.

    Its times, handles, name and attributes, and its effects with their settings."""
    made = PySegment(
        name=str(seg.name.get_value()),
        source_name=seg.source_name,
        record_in=seg.record_in.frame,
        record_out=seg.record_out.frame,
        source_in=seg.source_in.frame,
        source_out=seg.source_out.frame,
        record_duration=seg.record_duration.frame,
        type=seg.type,
        head=seg.head,
        tail=seg.tail,
        effects=[clone_effect(fx) for fx in seg.effects],
        source_bit_depth=seg.source_bit_depth,
    )
    for key in seg.attributes:
        if key == "name":
            continue
        value = getattr(seg, key).get_value()
        if key in made.attributes:
            getattr(made, key).set_value(value)
        else:
            made.attributes.append(key)
            setattr(made, key, FakeAttr(value))
    return made


def clone_versions(entry: PySequence) -> list[PyVersion]:
    return [
        PyVersion(
            [PyTrack([clone_segment(seg) for seg in track.segments]) for track in version.tracks]
        )
        for version in entry.versions
    ]


def _carry_audio_segment(seg: PySegment) -> PySegment:
    """A copy of an audio segment; one a test made unreadable (a cell that raises `RuntimeError`
    when read) is carried over as it is. Any other failure of the copy is raised."""
    try:
        return clone_segment(seg)
    except RuntimeError:
        return seg


def clone_audio_tracks(entry: PySequence) -> list[PyAudioTrack]:
    """A test may delete `audio_tracks` from an object to model a Flame without the member."""
    return [
        PyAudioTrack(
            channels=[
                PyAudioChannel([_carry_audio_segment(seg) for seg in channel._segments])
                for channel in track.channels
            ]
        )
        for track in getattr(entry, "audio_tracks", ())
    ]


def copy_media(source: PySequence, name: str, wiretap_id: str, uid: str) -> PySequence:
    """Makes what `media_panel.copy` of a clip / sequence makes.

    A new uid and wiretap id, the same versions, tracks, segments and effect settings (on Flame
    2025.2.4 the fingerprint and the pixels are the original's), and the audio tracks with their
    channels and segments (measured: a copy of a sequence with sound has the same audio rows, and a
    revert from the copy in a checkpoint puts them back)."""
    made = type(source)(
        name,
        wiretap_id=wiretap_id,
        uid=uid,
        duration=source.duration.frame,
        frame_rate=source.frame_rate,
        versions=clone_versions(source),
        bit_depth=source.bit_depth,
        width=source.width,
        height=source.height,
        ratio=source.ratio,
        scan_mode=source.scan_mode,
        start_frame=source.start_frame,
    )
    made.audio_tracks = clone_audio_tracks(source)
    for key, value in getattr(source, "copy_overrides", {}).items():
        setattr(made, key, value)
    return made


class PyWorkspace:
    def __init__(
        self,
        libraries: Iterable[PyLibrary] = (),
        desktop: PyDesktop | None = None,
        name: str = "Workspace",
    ) -> None:
        self.name = FakeAttr(name)
        self._libraries = list(libraries)
        self._desktop = desktop if desktop is not None else PyDesktop()
        adopt(self, self._libraries)
        self._desktop.parent = self

    @property
    def desktop(self) -> PyDesktop:
        return self._desktop

    @desktop.setter
    def desktop(self, desktop: PyDesktop) -> None:
        desktop.parent = self
        self._desktop = desktop

    @property
    def libraries(self) -> list[PyLibrary]:
        return list(self._libraries)

    def create_library(self, name: str) -> PyLibrary:
        library = PyLibrary(name, next_uid("lib"), f"/wt/lib/{name}")
        adopt(self, [library])
        self._libraries.append(library)
        return library


class PyProject:
    def __init__(self, name: str, workspace: PyWorkspace) -> None:
        self.name = name  # plain str, not FakeAttr
        self.current_workspace = workspace


class PyProjects:
    def __init__(self, current_project: PyProject) -> None:
        self.current_project = current_project


class PyFlameObject:
    """Mimics Flame's PyFlameObject subclasses.

    An attribute name the class does not define answers None instead of raising AttributeError,
    so `hasattr(x, anything)` is always true (measured on Flame 2025.2.4)."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return None


class PyNode(PyFlameObject):
    """A Batch node: `attributes` lists names; each is a `FakeAttr` instance
    attribute (pass `extra` to add type-specific ones)."""

    def __init__(
        self,
        name: str,
        node_type: str = "Blur",
        extra: dict[str, FakeAttr] | None = None,
        pos: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self.name = FakeAttr(name)
        self.type = FakeAttr(node_type)
        self.pos_x = FakeAttr(pos[0], (-100000.0, 100000.0))
        self.pos_y = FakeAttr(pos[1], (-100000.0, 100000.0))
        self.bypass = FakeAttr(False)
        self.attributes = ["name", "type", "pos_x", "pos_y", "bypass"]
        for key, attr in (extra or {}).items():
            setattr(self, key, attr)
            self.attributes.append(key)
        # `sockets` as a real node answers it: {'input': {socket: [names of the
        # nodes wired to it]}, 'output': {...}}. `sockets_error` makes the read raise.
        self._sockets: dict[str, dict[str, list[str]]] = {
            "input": {"Input_0": [], "Matte_0": []},
            "output": {"Result": [], "OutMatte": []},
        }
        self.sockets_error: Exception | None = None
        self._owner: PyBatch | None = None
        self.delete_makes_nothing = False
        self.save_node_setup_writes_nothing = False
        self.save_node_setup_suffixes: tuple[str, ...] | None = None
        """Suffixes of the files `save_node_setup` writes instead of the one type extension: a
        node of several parts (the Clip node writes six). The first carries the
        node's values."""

    @property
    def sockets(self) -> dict[str, dict[str, list[str]]]:
        if self.sockets_error is not None:
            raise self.sockets_error
        return {
            side: {k: list(v) for k, v in table.items()} for side, table in self._sockets.items()
        }

    def delete(self, confirm: bool = True) -> bool:
        """Removes the node from its group and from its neighbours' sockets."""
        if self._owner is None:
            raise RuntimeError("the node is not in a Batch group")
        if not self.delete_makes_nothing:
            self._owner.remove_node(self)
        return True

    def duplicate(self, keep_node_connections: bool = False) -> PyNode:
        """Duplicates in the CURRENT group only.

        The copy gets the next free `<base><n>` name.
        """
        if self._owner is None:
            raise RuntimeError("the node is not in a Batch group")
        return self._owner.duplicate_node(self, keep_node_connections)

    def node_setup_path(self, path: str) -> str:
        """Flame appends an extension that depends on the node's type (`.exposure_node`)."""
        return f"{path}.{str(self.type.get_value()).lower().replace(' ', '_')}_node"

    def save_node_setup(self, path: str) -> bool:
        if not self.save_node_setup_writes_nothing:
            values = {
                key: getattr(self, key).get_value()
                for key in self.attributes
                if isinstance(getattr(self, key).get_value(), (str, int, float, bool))
            }
            suffixes = self.save_node_setup_suffixes
            targets = (
                [self.node_setup_path(path)]
                if suffixes is None
                else [
                    os.path.join(os.path.dirname(path), os.path.basename(path).replace(".", "_"))
                    + suffix
                    for suffix in suffixes
                ]
            )
            for position, target in enumerate(targets):
                with open(target, "w", encoding="utf-8") as fh:
                    json.dump(values if position == 0 else {}, fh)
        return True

    def load_node_setup(self, path: str) -> bool:
        """A path that is not a file answers True and changes nothing (measured)."""
        if not os.path.isfile(path):
            return True
        with open(path, encoding="utf-8") as fh:
            values = json.load(fh)
        for key, value in values.items():
            if key in self.attributes:
                getattr(self, key).set_value(value)
        return True


class PyCoNode(PyFlameObject):
    """An inner node of an Action. `parents(link_type)` / `children(link_type)` are METHODS (the
    real Flame answers a TypeError when they are read as attributes); they list the nodes linked
    with that link type (default 'Default', compared ignoring case)."""

    def __init__(self, name: str, node_type: str | None = None) -> None:
        self.name = FakeAttr(name)
        self.attributes = ["name"]
        self.type = node_type
        self._parents: list[tuple[PyCoNode, str]] = []
        self._children: list[tuple[PyCoNode, str]] = []

    def parents(self, link_type: str = "Default") -> list[PyCoNode]:
        return [n for n, kind in self._parents if kind.lower() == link_type.lower()]

    def children(self, link_type: str = "Default") -> list[PyCoNode]:
        return [n for n, kind in self._children if kind.lower() == link_type.lower()]


AUTO_AXIS_TYPES = ("3D Path", "3D Shape", "3D Text")


def canonical_type_of(node_types: list[str], wanted: str) -> str:
    """Returns the node type Flame's `create_node` takes for `wanted`, ignoring case.

    Measured on Flame 2025.2.4: `create_node('Mux')` works for the listed 'MUX'; a type nothing
    matches raises."""
    if wanted in node_types:
        return wanted
    hits = [t for t in node_types if t.lower() == wanted.lower()]
    if len(hits) != 1:
        raise RuntimeError(f"Unknown node type {wanted!r}")
    return hits[0]


class PyActionNode(PyNode):
    def __init__(self, name: str, inner: Iterable[PyCoNode] = ()) -> None:
        super().__init__(name, node_type="Action")
        self.nodes = list(inner)
        self.node_types = ["3D Path", "3D Shape", "3D Text", "Axis", "Camera", "GMask", "Light"]
        self.output_types = ["Comp", "Matte", "Z-Depth"]
        self.media_layers: list[Any] = []
        self.connect_calls: list[tuple[str, str, str]] = []
        self.connect_makes_nothing = False
        self.create_makes_nothing = False

    def _unique(self, base: str) -> str:
        taken = {str(n.name.get_value()) for n in self.nodes}
        count = 1
        while f"{base}{count}" in taken:
            count += 1
        return f"{base}{count}"

    def create_node(self, node_type: str) -> PyCoNode:
        """Creates an inner node.

        Measured on Flame 2025.2.4: `create_node('3D Path')` answers a PyCoNode and an `axis`
        comes with it."""
        node_type = canonical_type_of(self.node_types, node_type)
        base = node_type.lower().replace(" ", "")
        if self.create_makes_nothing:
            return PyCoNode(f"{base}0", node_type)
        if node_type in AUTO_AXIS_TYPES:
            self.nodes.append(PyCoNode(self._unique("axis"), "Axis"))
        inner = PyCoNode(self._unique(base.replace("3d", "")), node_type)
        self.nodes.append(inner)
        return inner

    def connect_nodes(self, parent: PyCoNode, child: PyCoNode, link_type: str = "Default") -> bool:
        self.connect_calls.append(
            (str(parent.name.get_value()), str(child.name.get_value()), link_type)
        )
        if not self.connect_makes_nothing:
            parent._children.append((child, link_type))
            child._parents.append((parent, link_type))
        return True


class PyResolution:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.bit_depth = 10
        self.scan_mode = "P"
        self.frame_ratio = 1.7778


CLIP_NODE_SETUP_SUFFIXES: tuple[str, ...] = (
    ".clip_node",
    ".clip_node_clip",
    ".clip_node_format",
    ".lut_node",
    ".resize",
    ".resize_node",
)
"""The six files one `save_node_setup` of a Clip node writes on Flame 2025.2.4."""


class PyClipNode(PyNode):
    """The node Flame makes when a clip is copied into a SCHEMATIC reel."""

    def __init__(self, name: str, clip: Any = None) -> None:
        super().__init__(name, node_type="Clip")
        self.save_node_setup_suffixes = CLIP_NODE_SETUP_SUFFIXES
        self.clip = clip
        self.media_path = FakeAttr(f"/media/{name}.mov")
        self.duration = FakeAttr(24)
        self.resolution = FakeAttr(PyResolution(1920, 1080))


class PyWriteFileNode(PyNode):
    """A Write File node: where and what it writes are attributes.

    `get_resolved_media_path()` names the files. As measured on Flame 2025.2.4 (pattern `<name>`,
    padding 6): `<media_path><name>[<first>-<last>].<ext>` -- the frames are `<name>000001.exr`
    (NO dot before the number) and a `<name>.clip` beside them. With `dotted` True it is the other
    form measured on Flame 2025.2.4: `writefile1.[000001-000100].exr` and frames
    `writefile1.000001.exr`. The range is `range_start` / `range_end` (default 1-100)."""

    def __init__(
        self,
        name: str,
        media_path: str = "/var/tmp/",
        first: int = 1,
        last: int = 100,
        file_type: str = "OpenEXR",
        ext: str = "exr",
    ) -> None:
        self.dotted = False
        super().__init__(
            name,
            node_type="Write File",
            extra={
                "range_start": FakeAttr(first, (-2147483647, 2147483647)),
                "range_end": FakeAttr(last, (-2147483647, 2147483647)),
                "media_path": FakeAttr(media_path),
                "media_path_pattern": FakeAttr("<name>"),
                "file_type": FakeAttr(file_type),
                "format_extension": FakeAttr(ext),
                "frame_padding": FakeAttr(6),
                "create_clip": FakeAttr(True),
                "note": FakeAttr(""),
            },
        )

    def _attr(self, key: str) -> Any:
        return getattr(self, key).get_value()

    def get_resolved_media_path(self) -> str:
        pad = int(self._attr("frame_padding"))
        directory = str(self._attr("media_path"))
        if not directory.endswith("/"):
            directory += "/"
        first, last = int(self._attr("range_start")), int(self._attr("range_end"))
        stem = f"{self.name.get_value()}{'.' if self.dotted else ''}"
        return f"{directory}{stem}[{first:0{pad}d}-{last:0{pad}d}].{self._attr('format_extension')}"

    def is_effective(self) -> bool:
        wired = any(self._sockets["input"].values())
        return not self._attr("bypass") and wired

    def write_frames(self, limit: int | None) -> int:
        """What a render does for this node: the numbered frames of its range (at most `limit`)
        and a `.clip` beside them. A directory that is not there is an error."""
        pad = int(self._attr("frame_padding"))
        directory = str(self._attr("media_path"))
        if not os.path.isdir(directory):
            raise RuntimeError(f"cannot write to {directory}")
        first, last = int(self._attr("range_start")), int(self._attr("range_end"))
        frames = list(range(first, last + 1))
        if limit is not None:
            frames = frames[:limit]
        for frame in frames:
            path = os.path.join(
                directory,
                f"{self.name.get_value()}{'.' if self.dotted else ''}{frame:0{pad}d}"
                f".{self._attr('format_extension')}",
            )
            with open(path, "wb") as fh:
                fh.write(b"x" * 16)
        with open(os.path.join(directory, f"{self.name.get_value()}.clip"), "w") as fh:
            fh.write("<clip/>")
        return len(frames)


class PyRenderNode(PyNode):
    """A Render node: 23 attributes, no path (measured on Flame 2025.2.4).

    It writes a clip into its destination reel(s). No `get_resolved_media_path`."""

    def __init__(self, name: str, first: int = 1, last: int = 100) -> None:
        super().__init__(
            name,
            node_type="Render",
            extra={
                "range_start": FakeAttr(first, (-2147483647, 2147483647)),
                "range_end": FakeAttr(last, (-2147483647, 2147483647)),
                "destination": FakeAttr(["Batch Reels", "Batch Renders"]),
                "tape_name": FakeAttr("Batch"),
            },
        )


DEFAULT_REEL_COUNT = 4


class PyBatchIteration(PyFlameObject):
    def __init__(self, name: str, uid: str, number: int) -> None:
        self.name = FakeAttr(name)
        self.uid = FakeAttr(uid)
        self.iteration_number = number
        self.attributes = ["name", "uid"]


class PyBatch(PyFlameObject):
    """A Batch group; `save_setup` / `load_setup` behave as measured on 2025.2.4.

    `save_setup(path)` writes `<dir>/<stem>.batch` (dots of the name become underscores, a
    `.batch` is appended) plus a directory `<dir>/<stem>/`, and returns True whatever path it
    was given; `load_setup(path)` of a path that is not a file EMPTIES the group and raises
    `RuntimeError('Could not load the Batch setup. Check the filepath.')`.

    It also has `reels` / `shelf_reels` / `batch_iterations`, `create_reel` / `create_shelf_reel`
    / `iterate(index=-1)` (an existing index is overwritten without warning: a new uid, the same
    count) / `organize()` (positions only, returns True).

    What the real Flame does with schematic reels (measured on 2025.2.4): `load_setup` restores the
    nodes and only the reels that hold a clip or a sequence, REBUILT with new uids (the Clip nodes
    are re-linked to the new clips); empty reels are gone and four default empty reels
    (`Schematic Reel 1` to `4`) are added; shelf reels and iterations are not touched. `create_reel`
    renames a name the group already has (`SrcA` -> `SrcA 1`). `flame.delete(reel, confirm=False)`
    (`FakeFlame.delete` -> `delete_reel`) removes the reel; deleting a reel that holds clips also
    deletes the Clip nodes that use them; deleting the LAST reel makes Flame create the four
    default reels again."""

    def __init__(self, name: str, uid: str, nodes: Iterable[PyNode] = ()) -> None:
        self.name = FakeAttr(name)
        self.uid = FakeAttr(uid)
        self.nodes = list(nodes)
        for adopted in self.nodes:
            adopted._owner = self
        self._desktop: PyDesktop | None = None
        self.node_types = ["Action", "Blur", "Comp", "Exposure", "MUX", "Render", "Write File"]
        self.connect_calls: list[tuple[str, str, str, str]] = []
        self.connect_makes_nothing = False
        self.disconnect_calls: list[tuple[str, str | None]] = []
        self.disconnect_makes_nothing = False
        self.create_node_makes_nothing = False
        self.duplicate_makes_nothing = False
        self.duplicate_keeps_name = False  # the copy gets the original's name (a Clip node's copy)
        self.create_name_collides_with: str | None = None  # a new node takes this existing name
        self.attributes = ["name", "uid"]
        self.save_calls: list[str] = []
        self.load_calls: list[str] = []
        self.load_return: Any = True
        self.save_writes_nothing = False
        self.save_stem_suffix = ""  # a rewrite of the name the caller could not predict
        self._created = 0
        self.reels: list[PyReel] = []
        self.shelf_reels: list[PyReel] = []
        self.batch_iterations: list[PyBatchIteration] = []
        self.current_iteration_number = FakeAttr(-1)
        self.duration = FakeAttr(100)
        self.start_frame = FakeAttr(1)
        self.organize_calls = 0
        self.organize_returns: Any = True
        self.create_reel_makes_nothing = False
        self.delete_reel_raises: str | None = None
        self.create_reel_suffix = ""  # a rename of the name asked for, like a taken name's " 1"
        self.load_keeps_unsaved_filled_reels = False  # a reel with clips the setup lacks survives
        self.delete_reel_calls: list[str] = []
        self._uid_seq = 0
        self.render_calls: list[str] = []
        self.render_returns: Any = True
        self.render_raises: str | None = None
        self.render_frames_limit: int | None = None  # write only this many frames per node
        self.render_before_write: Any = None  # called at the start of a render (test hook)

    def _next_uid(self, kind: str) -> str:
        self._uid_seq += 1
        return f"{self.uid.get_value()}-{kind}{self._uid_seq}"

    def create_reel(self, name: str) -> PyReel | None:
        if self.create_reel_makes_nothing:
            return None
        taken = {str(r.name.get_value()) for r in self.reels}
        unique = f"{name}{self.create_reel_suffix}"
        if unique in taken:
            unique = f"{name} 1"
        reel = PyReel(
            unique,
            self._next_uid("reel"),
            f"/wt/{self.name.get_value()}/{unique}",
            reel_type="Schematic",
            owner=self,
        )
        self.reels.append(reel)
        return reel

    def add_default_reels(self, taken: Iterable[str] = ()) -> list[PyReel]:
        """The four empty reels Flame makes for a group with none (and after a load_setup)."""
        made: list[PyReel] = []
        for number in range(1, DEFAULT_REEL_COUNT + 1):
            name = f"Schematic Reel {number}"
            if name in taken:
                continue
            made.append(
                PyReel(
                    name,
                    self._next_uid("reel"),
                    f"/wt/{self.name.get_value()}/{name}",
                    reel_type="Schematic",
                    owner=self,
                )
            )
        return made

    def delete_reel(self, reel: PyReel) -> bool:
        self.delete_reel_calls.append(str(reel.name.get_value()))
        if self.delete_reel_raises is not None:
            raise RuntimeError(self.delete_reel_raises)
        gone = {id(c) for c in reel.children}
        self.reels = [r for r in self.reels if r is not reel]
        for node in [n for n in self.nodes if type(n) is PyClipNode and id(n.clip) in gone]:
            self.remove_node(node)
        if not self.reels:
            self.reels = self.add_default_reels()
        return True

    def create_shelf_reel(self, name: str) -> PyReel | None:
        if self.create_reel_makes_nothing:
            return None
        reel = PyReel(
            name,
            self._next_uid("shelf"),
            f"/wt/{self.name.get_value()}/{name}",
            reel_type="Shelf",
            owner=self,
        )
        self.shelf_reels.append(reel)
        return reel

    def add_clip_node(self, name: str, clip: Any = None) -> PyClipNode:
        taken = {str(n.name.get_value()) for n in self.nodes}
        unique, count = name, 1
        while unique in taken:
            count += 1
            unique = f"{name}{count}"
        node = PyClipNode(unique, clip)
        node._owner = self
        self.nodes.append(node)
        return node

    def remove_node(self, node: PyNode) -> None:
        gone = str(node.name.get_value())
        self.nodes = [n for n in self.nodes if n is not node]
        for other in self.nodes:
            for table in other._sockets.values():
                for linked in table.values():
                    while gone in linked:
                        linked.remove(gone)
        node._owner = None

    def duplicate_node(self, node: PyNode, keep_connections: bool) -> PyNode:
        desktop = self._desktop
        if desktop is None or desktop.current_batch_group.get_value() is not self:
            raise RuntimeError("Nodes need to be in the current batch to be duplicated.")
        if self.duplicate_makes_nothing:
            return node
        base = str(node.name.get_value()).rstrip("0123456789") or "node"
        taken = {str(n.name.get_value()) for n in self.nodes}
        count = 1
        while f"{base}{count}" in taken:
            count += 1
        copy_name = str(node.name.get_value()) if self.duplicate_keeps_name else f"{base}{count}"
        copy: PyNode
        if type(node) is PyActionNode:
            copy = PyActionNode(copy_name, [PyCoNode(str(c.name.get_value())) for c in node.nodes])
        else:
            copy = PyNode(copy_name, str(node.type.get_value()))
        for key in node.attributes:
            if key not in copy.attributes:  # an attribute only the original type carries
                setattr(copy, key, FakeAttr(getattr(node, key).get_value()))
                copy.attributes.append(key)
            if key not in ("name", "type") and hasattr(copy, key):
                getattr(copy, key).set_value(getattr(node, key).get_value())
        if self.duplicate_keeps_name:  # the copy lands beside the original
            copy.pos_x.set_value(node.pos_x.get_value() + 50)
            copy.pos_y.set_value(node.pos_y.get_value() + 50)
        copy._owner = self
        self.nodes.append(copy)
        if keep_connections:
            for socket, linked in node._sockets["input"].items():
                for name in linked:
                    upstream = next(n for n in self.nodes if str(n.name.get_value()) == name)
                    self.connect_nodes(upstream, "Default", copy, socket)
        return copy

    def disconnect_node(self, node: PyNode, input_socket_name: str = "") -> bool:
        self.disconnect_calls.append((str(node.name.get_value()), input_socket_name or None))
        if self.disconnect_makes_nothing:
            return True
        sockets = [input_socket_name] if input_socket_name else list(node._sockets["input"])
        me = str(node.name.get_value())
        for socket in sockets:
            for upstream_name in list(node._sockets["input"][socket]):
                node._sockets["input"][socket].remove(upstream_name)
                for other in self.nodes:
                    if str(other.name.get_value()) == upstream_name:
                        for linked in other._sockets["output"].values():
                            while me in linked:
                                linked.remove(me)
        return True

    def organize(self) -> Any:
        self.organize_calls += 1
        for position, node in enumerate(self.nodes):
            node.pos_x.set_value(-288 * position)
            node.pos_y.set_value(0)
        return self.organize_returns

    def iterate(self, index: int = -1) -> PyBatchIteration:
        numbers = [it.iteration_number for it in self.batch_iterations]
        number = index if index != -1 else (max(numbers) + 1 if numbers else 1)
        made = PyBatchIteration(
            f"{self.name.get_value()}_{number:03d}", self._next_uid("iter"), number
        )
        if number in numbers:
            self.batch_iterations[numbers.index(number)] = made
        else:
            self.batch_iterations.append(made)
        self.current_iteration_number.set_value(number)
        return made

    def create_node(self, node_type: str) -> PyNode:
        node_type = canonical_type_of(self.node_types, node_type)
        base = node_type.lower().replace(" ", "")
        taken = {str(n.name.get_value()) for n in self.nodes}
        self._created += 1
        name = f"{base}{self._created}"
        while name in taken:  # Flame names a new node with the first free number
            self._created += 1
            name = f"{base}{self._created}"
        if self.create_name_collides_with is not None:
            name = self.create_name_collides_with
        node: PyNode
        if node_type == "Action":  # an Action node starts with its two inner nodes
            node = PyActionNode(name, [PyCoNode("Default"), PyCoNode("Perspective")])
        elif node_type == "Write File":
            node = PyWriteFileNode(name)
        elif node_type == "Render":
            node = PyRenderNode(name)
        else:
            node = PyNode(name, node_type)
        node._owner = self
        if not self.create_node_makes_nothing:
            self.nodes.append(node)
        return node

    def connect_nodes(
        self, out_node: PyNode, out_socket: str, in_node: PyNode, in_socket: str
    ) -> bool:
        self.connect_calls.append(
            (str(out_node.name.get_value()), out_socket, str(in_node.name.get_value()), in_socket)
        )
        out_name = (
            next(iter(out_node._sockets["output"])) if out_socket == "Default" else out_socket
        )
        in_name = next(iter(in_node._sockets["input"])) if in_socket == "Default" else in_socket
        if self.connect_makes_nothing:
            return True
        if out_name not in out_node._sockets["output"] or in_name not in in_node._sockets["input"]:
            raise RuntimeError("unknown socket")
        out_node._sockets["output"][out_name].append(str(in_node.name.get_value()))
        in_node._sockets["input"][in_name].append(str(out_node.name.get_value()))
        return True

    def render(self, render_option: str = "Foreground") -> Any:
        """Renders; a group that is not current renders too (measured on Flame 2025.2.4).

        Every effective Write File writes its range, and a clip of it lands in the shelf reel
        `Batch Renders`."""
        self.render_calls.append(render_option)
        if self.render_before_write is not None:
            self.render_before_write(self)
        if self.render_raises is not None:
            raise RuntimeError(self.render_raises)
        for node in self.nodes:
            if isinstance(node, PyWriteFileNode) and node.is_effective():
                node.write_frames(self.render_frames_limit)
                self._add_render_clip(str(node.name.get_value()))
        return self.render_returns

    def _add_render_clip(self, name: str) -> None:
        shelf = next((r for r in self.shelf_reels if r.name.get_value() == "Batch Renders"), None)
        if shelf is None:
            shelf = self.create_shelf_reel("Batch Renders")
        assert shelf is not None
        shelf._children.append(
            PyClip(name, wiretap_id=f"{shelf.get_wiretap_node_id()}/render{len(shelf._children)}")
        )

    def save_setup(self, path: str) -> bool:
        self.save_calls.append(path)
        if self.save_writes_nothing:
            return True
        directory, name = os.path.split(path)
        stem = name.replace(".", "_") + self.save_stem_suffix
        os.makedirs(os.path.join(directory, stem), exist_ok=True)
        nodes = [
            {
                "name": n.name.get_value(),
                "type": n.type.get_value(),
                "sockets": n._sockets,
                "cls": type(n).__name__,
                "inner": [
                    [c.name.get_value(), c.type, [[p.name.get_value(), k] for p, k in c._parents]]
                    for c in n.nodes
                ]
                if type(n) is PyActionNode
                else [],
                "attrs": {
                    key: getattr(n, key).get_value()
                    for key in n.attributes
                    if key not in ("name", "type")
                    and isinstance(getattr(n, key).get_value(), (str, int, float, bool))
                },
                "clip_of": self._clip_position(n) if type(n) is PyClipNode else None,
            }
            for n in self.nodes
        ]
        reels = [
            {
                "name": r.name.get_value(),
                "children": [
                    {"cls": type(c).__name__, "name": c.name.get_value()} for c in r.children
                ],
            }
            for r in self.reels
            if r.children
        ]
        payload = {"nodes": nodes, "reels": reels}
        with open(os.path.join(directory, stem + ".batch"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return True

    def _clip_position(self, node: PyClipNode) -> list[Any] | None:
        for reel in self.reels:
            for index, child in enumerate(reel.children):
                if child is node.clip:
                    return [reel.name.get_value(), index]
        return None

    def _rebuild_reels(self, saved: list[dict[str, Any]]) -> dict[tuple[str, int], Any]:
        """Rebuilds the schematic reels as load_setup does on Flame 2025.2.4.

        Only the reels that hold clips come back, rebuilt with new uids, after four fresh default
        empty ones."""
        kinds = {"PyClip": PyClip, "PySequence": PySequence}
        rebuilt: list[PyReel] = []
        clips: dict[tuple[str, int], Any] = {}
        for entry in saved:
            name = str(entry["name"])
            reel = PyReel(
                name,
                self._next_uid("reel"),
                f"/wt/{self.name.get_value()}/{name}",
                reel_type="Schematic",
                owner=self,
            )
            for index, child in enumerate(entry["children"]):
                reel._copies += 1
                made = kinds[child["cls"]](
                    child["name"], wiretap_id=f"{reel._wiretap_id}/copy{reel._copies}"
                )
                reel._children.append(made)
                clips[(name, index)] = made
            rebuilt.append(reel)
        kept = [
            r
            for r in self.reels
            if self.load_keeps_unsaved_filled_reels
            and r.children
            and str(r.name.get_value()) not in {str(e["name"]) for e in saved}
        ]
        taken = {str(r.name.get_value()) for r in [*rebuilt, *kept]}
        self.reels = [*self.add_default_reels(taken), *rebuilt, *kept]
        return clips

    def load_setup(self, path: str) -> Any:
        """Nodes come back as they were saved: type, wiring, settings, an Action's inner nodes
        and their links. A node that is still there under its name is reused (its attributes
        are put back), so a test's spy attributes survive."""
        self.load_calls.append(path)
        if not os.path.isfile(path):
            self.nodes = []
            raise RuntimeError("Could not load the Batch setup. Check the filepath.")
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        clips = self._rebuild_reels(payload["reels"])
        existing = {str(n.name.get_value()): n for n in self.nodes}
        loaded: list[PyNode] = []
        for entry in payload["nodes"]:
            node: PyNode
            kept = existing.get(entry["name"])
            if entry.get("cls") == "PyClipNode":
                position = entry.get("clip_of")
                node = PyClipNode(entry["name"], clips.get(tuple(position)) if position else None)
            elif kept is not None and type(kept).__name__ == entry.get("cls"):
                node = kept
            elif entry.get("cls") == "PyActionNode":
                node = PyActionNode(entry["name"])
            else:
                node = PyNode(entry["name"], entry["type"])
            if isinstance(node, PyActionNode):
                inner: dict[str, PyCoNode] = {}
                for cname, ctype, _parents in entry["inner"]:
                    inner[cname] = PyCoNode(cname, ctype)
                for cname, _ctype, parents in entry["inner"]:
                    for pname, kind in parents:
                        inner[pname]._children.append((inner[cname], kind))
                        inner[cname]._parents.append((inner[pname], kind))
                node.nodes = list(inner.values())
            for key, value in entry.get("attrs", {}).items():
                if key in node.attributes:
                    getattr(node, key).set_value(value)
            node._sockets = entry["sockets"]
            node._owner = self
            loaded.append(node)
        self.nodes = loaded
        return self.load_return


class PyReelGroup(PyFlameObject):
    """Fake reel group; `children` holds the reels unless `reels_in_children=False`.

    In that case only `.reels` does (a case not settled on a real Flame).

    Measured on Flame 2025.2.4: `create_reel(name, sequence=False)` adds a reel at
    the END (type `Reel`, or `Sequences`); a taken name goes up by a number. A new group made by
    `PyDesktop.create_reel_group` starts with `Reel 1` to `4` and `Sequences`."""

    def __init__(
        self,
        name: str,
        wiretap_id: str,
        reels: Iterable[PyReel] = (),
        reels_in_children: bool = True,
        uid: str | None = None,
    ) -> None:
        self.name = FakeAttr(name)
        self.uid = FakeAttr(uid if uid is not None else "uid-" + name)
        self._wiretap_id = wiretap_id
        self.reels = list(reels)
        self._reels_in_children = reels_in_children
        self.children = list(self.reels) if reels_in_children else None
        self.attributes = ["name", "uid"]
        self.parent: Any = None
        adopt(self, self.reels)

    def get_wiretap_node_id(self) -> str:
        return self._wiretap_id

    def sync_children(self) -> None:
        adopt(self, self.reels)
        if self._reels_in_children:
            self.children = list(self.reels)

    def add_default_reels(self) -> None:
        """Reel 1 to 4 and Sequences, what Flame makes for a new group (and again after its last
        reel was deleted)."""
        names = [("Reel 1", "Reel"), ("Reel 2", "Reel"), ("Reel 3", "Reel"), ("Reel 4", "Reel")]
        for name, kind in [*names, ("Sequences", "Sequences")]:
            self.reels.append(
                PyReel(
                    name,
                    next_uid("reel"),
                    f"{self._wiretap_id}/{name}",
                    reel_type=kind,
                )
            )
        self.sync_children()

    def create_reel(self, name: str, sequence: bool = False) -> PyReel:
        unique = numbered_name(name, [str(r.name.get_value()) for r in self.reels])
        reel = PyReel(
            unique,
            next_uid("reel"),
            f"{self._wiretap_id}/{unique}",
            reel_type="Sequences" if sequence else "Reel",
        )
        self.reels.append(reel)
        self.sync_children()
        return reel


class PyDesktop:
    """The Desktop, as measured on Flame 2025.2.4.

    `create_batch_group` returns a new PyBatch at the END of `batch_groups` and ALWAYS makes it the
    current group; a taken name gets ' 1' (' 2' ...) appended. `reels=[...]` = exactly those
    schematic reels (it wins over nb_reels); `shelf_reels=[...]` = exactly those shelf reels, no
    `Batch Renders`; without them `Schematic Reel 1..nb_reels` (nb_reels=0 still makes one,
    `Schematic Reel`) and `nb_shelf_reels` shelf reels (the first is `Batch Renders`, and
    nb_shelf_reels=0 still makes one). start_frame default 1, duration default 100.
    `create_reel_group` makes a group with its default reels; a taken name goes up by a number."""

    def __init__(
        self,
        name: str = "Desktop",
        batch_groups: Iterable[PyBatch] = (),
        reel_groups: Iterable[PyReelGroup] = (),
        current_batch_group: PyBatch | None = None,
        children_include_groups: bool = True,
    ) -> None:
        self.name = FakeAttr(name)
        self.batch_groups = list(batch_groups)
        self.reel_groups = list(reel_groups)
        # a PyAttribute holding the PyBatch (or None), like the real desktop (api graph)
        self.current_batch_group = FakeAttr(current_batch_group)
        self._children_include_groups = children_include_groups
        self.children = [*self.reel_groups, *self.batch_groups] if children_include_groups else None
        for group in self.batch_groups:
            group._desktop = self
        self.parent: Any = None
        adopt(self, self.reel_groups)

    def sync_children(self) -> None:
        adopt(self, self.reel_groups)
        if self._children_include_groups:
            self.children = [*self.reel_groups, *self.batch_groups]

    def create_batch_group(
        self,
        name: str,
        nb_reels: int = 4,
        nb_shelf_reels: int = 1,
        reels: list[str] | None = None,
        shelf_reels: list[str] | None = None,
        start_frame: int = 1,
        duration: int | None = None,
    ) -> PyBatch:
        taken = {str(g.name.get_value()) for g in self.batch_groups}
        unique, count = name, 0
        while unique in taken:
            count += 1
            unique = f"{name} {count}"
        group = PyBatch(unique, next_uid("bg"))
        group._desktop = self
        if reels:
            reel_names = list(reels)
        elif nb_reels <= 0:
            reel_names = ["Schematic Reel"]
        else:
            reel_names = [f"Schematic Reel {i}" for i in range(1, nb_reels + 1)]
        if shelf_reels:
            shelf_names = list(shelf_reels)
        else:
            shelf_names = [
                "Batch Renders",
                *(f"Shelf Reel {i}" for i in range(2, nb_shelf_reels + 1)),
            ]
        for reel_name in reel_names:
            group.create_reel(reel_name)
        for shelf_name in shelf_names:
            group.create_shelf_reel(shelf_name)
        group.start_frame.set_value(start_frame)
        group.duration.set_value(100 if duration is None else duration)
        self.batch_groups.append(group)
        self.current_batch_group.set_value(group)
        self.sync_children()
        return group

    def create_reel_group(self, name: str) -> PyReelGroup:
        taken = [str(g.name.get_value()) for g in self.reel_groups]
        unique = numbered_name(name, taken)
        group = PyReelGroup(unique, f"/wt/desktop/{unique}", uid=next_uid("rg"))
        group.add_default_reels()
        self.reel_groups.append(group)
        self.sync_children()
        return group

    def remove_batch_group(self, group: PyBatch) -> None:
        """`flame.delete(batch)`. Deleting the CURRENT group makes another existing group current
        (measured: not the one that was current before it -- the fake takes the first that is
        left)."""
        was_current = self.current_batch_group.get_value() is group
        self.batch_groups = [g for g in self.batch_groups if g is not group]
        group._desktop = None
        if was_current:
            self.current_batch_group.set_value(self.batch_groups[0] if self.batch_groups else None)
        self.sync_children()


class PyMediaPanel:
    def __init__(self, selected_entries: Iterable[Any] = (), flame: Any = None) -> None:
        self.selected_entries = list(selected_entries)
        self.copy_calls: list[tuple[Any, Any]] = []
        self.move_calls: list[tuple[Any, Any]] = []
        self.duplicate_actions: list[str] = []
        self._flame = flame

    def copy(self, source: Any, destination: Any, duplicate_action: str = "add") -> list[Any]:
        """Copies a clip or a list of them into a reel and returns the copies.

        Measured on Flame 2025.2.4: `duplicate_action` is `add` or `replace` (anything else is a
        RuntimeError) and `replace` replaces nothing -- it adds like `add`."""
        if duplicate_action not in ("add", "replace"):
            raise RuntimeError("Invalid duplicate_action, expecting add or replace")
        self.duplicate_actions.append(duplicate_action)
        self.copy_calls.append((source, destination))
        sources = list(source) if isinstance(source, (list, tuple)) else [source]
        received: list[Any] = destination.receive(sources)
        if ACTIVE_TIMELINE:
            timeline = ACTIVE_TIMELINE[0]
            for made in received:
                timeline.show_new_copy(made, timeline.copy_moves_display_until_open)
        return received

    def move(self, source: Any, destination: Any) -> list[Any]:
        """Moves a clip / sequence or a list of them into a reel, folder or library.

        Measured on Flame 2025.2.4: the objects come back in the order given, and a moved object
        ALWAYS has a new uid and a new wiretap id (an id it had before finds nothing any more); it
        goes to the END of the destination."""
        self.move_calls.append((source, destination))
        sources = list(source) if isinstance(source, (list, tuple)) else [source]
        moved: list[Any] = []
        for obj in sources:
            old_parent = obj.parent
            before = list(old_parent._children)
            old_parent._children = [c for c in before if c is not obj]
            shown = ACTIVE_TIMELINE[0].clip if ACTIVE_TIMELINE else None
            if shown is obj:
                # the Timeline shows another sequence of the same reel afterwards
                others = [c for c in old_parent._children if type(c).__name__ == "PySequence"]
                ACTIVE_TIMELINE[0].clip = others[0] if others else None
            elif shown is not None and shown in before and before.index(obj) < before.index(shown):
                # moving out a sequence that sits IN FRONT of the shown one, from its reel, moves
                # the Timeline on to the sequence after the shown one (observed once: reel A B C D,
                # B shown, A moved out -> C shown); one behind it changes nothing
                after = [
                    c
                    for c in old_parent._children[old_parent._children.index(shown) + 1 :]
                    if type(c).__name__ == "PySequence"
                ]
                if after:
                    ACTIVE_TIMELINE[0].clip = after[0]
            name = str(obj.name.get_value())
            if self._flame is not None:
                self._flame.forget_wiretap(obj)
            obj.uid.set_value(next_uid("moved"))
            obj._wiretap_id = f"{destination.get_wiretap_node_id()}/{name}.{next_uid('mv')}"
            adopt(destination, [obj])
            destination._children.append(obj)
            moved.append(obj)
        return moved


_PATTERN = re.compile(r"\[\d+-\d+\]")


def importable_names(path: str) -> list[str]:
    """Returns the clip names `flame.import_clips(path, ...)` makes.

    A missing path or a 0-byte file makes none."""
    if _PATTERN.search(path):
        directory, base = os.path.split(path)
        rx = re.compile("^" + r"\d+".join(re.escape(part) for part in _PATTERN.split(base)) + "$")
        hits = [
            f
            for f in (os.listdir(directory) if os.path.isdir(directory) else [])
            if rx.match(f) and os.path.getsize(os.path.join(directory, f)) > 0
        ]
        return [_PATTERN.sub("", os.path.splitext(base)[0]).rstrip(".")] if hits else []
    if os.path.isdir(path):
        return [
            os.path.splitext(f)[0]
            for f in sorted(os.listdir(path))
            if os.path.isfile(os.path.join(path, f)) and os.path.getsize(os.path.join(path, f)) > 0
        ]
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return [os.path.splitext(os.path.basename(path))[0]]
    return []


class UnreadableWrapper:
    """The result of `flame.duplicate(sequence)` on 2025.2.4.

    It is a typed PyClip whose name / uid / parent read None.
    """

    name: Any = None
    uid: Any = None
    parent: Any = None


UnreadableWrapper.__name__ = "PyClip"


def clone_entry(entry: Any, parent: Any) -> Any:
    """A new clip / sequence of the same name, shape and media (a new uid and wiretap id)."""
    name = str(entry.name.get_value())
    return copy_media(
        entry, name, f"{parent.get_wiretap_node_id()}/{name}.{next_uid('dup')}", next_uid("clip")
    )


class FakeFlame:
    def __init__(
        self,
        *,
        libraries: Iterable[PyLibrary] = (),
        selected: Iterable[Any] = (),
        wiretap_index: dict[str, Any] | None = None,
        desktop: PyDesktop | None = None,
    ) -> None:
        workspace = PyWorkspace(list(libraries), desktop=desktop)
        self.projects = PyProjects(PyProject("TestProject", workspace))
        self.media_panel = PyMediaPanel(selected, flame=self)
        self.timeline = PyTimeline()
        self.PyTime = PyTime
        ACTIVE_TIMELINE[:] = [self.timeline]
        ACTIVE_FLAME[:] = [self]
        self._wiretap_index = dict(wiretap_index) if wiretap_index else {}
        self.import_calls: list[list[str]] = []
        self.duplicate_calls: list[Any] = []
        self.exited = False

    def forget_wiretap(self, obj: Any) -> None:
        """The wiretap id an object had before a move finds nothing any more."""
        self._wiretap_index = {k: v for k, v in self._wiretap_index.items() if v is not obj}

    @property
    def _desktop(self) -> PyDesktop:
        return self.projects.current_project.current_workspace.desktop

    def all_objects(self) -> list[Any]:
        """Everything the fake holds that has a uid: libraries, Desktop reel groups, Batch
        groups, their reels and what the reels hold."""
        out: list[Any] = []
        workspace = self.projects.current_project.current_workspace
        containers: list[Any] = [*workspace.libraries, *self._desktop.reel_groups]
        for batch in self._desktop.batch_groups:
            out.append(batch)
            containers.extend([*batch.reels, *batch.shelf_reels])
        for container in containers:
            out.append(container)
            for child in container.children if container.children is not None else []:
                out.append(child)
                if type(child).__name__ == "PyReel":
                    out.extend(child.children)
                elif type(child).__name__ == "PyFolder":
                    out.extend(self._folder_objects(child))
        for group in self._desktop.reel_groups:
            for reel in group.reels:
                out.append(reel)
                out.extend(reel.children)
        return out

    def _folder_objects(self, folder: Any) -> list[Any]:
        found: list[Any] = []
        for child in folder.children:
            found.append(child)
            kind = type(child).__name__
            if kind == "PyReel":
                found.extend(child.children)
            elif kind == "PyFolder":
                found.extend(self._folder_objects(child))
        return found

    def find_by_wiretap_node_id(self, node_id: str) -> Any:
        if node_id in self._wiretap_index:
            return self._wiretap_index[node_id]
        for obj in self.all_objects():
            getter = getattr(obj, "get_wiretap_node_id", None)
            if callable(getter) and type(obj).__name__ != "PyBatch" and getter() == node_id:
                return obj
        return None

    def find_by_uid(self, uid: str) -> Any:
        for obj in self.all_objects():
            if uid_of(obj) == uid:
                return obj
        return None

    def find_by_name(self, name: str, parent: Any = None) -> list[Any]:
        """Returns a LIST of the objects of that name, on the Desktop and in the libraries.

        `parent=` keeps the direct children of that object; a name nothing carries
        gives []. An object is listed once."""
        pool = self.all_objects() if parent is None else list(parent.children or [])
        seen: set[int] = set()
        found: list[Any] = []
        for obj in pool:
            attr = getattr(obj, "name", None)
            label = attr.get_value() if isinstance(attr, FakeAttr) else attr
            if label == name and id(obj) not in seen:
                seen.add(id(obj))
                found.append(obj)
        return found

    def import_clips(self, path: Any, destination: Any) -> list[Any]:
        """Imports clips as Flame 2025.2.4 does.

        A path or a list of paths into a reel / folder / library; a file is a clip named after it, a
        directory brings its media files in, a sequence pattern (`name.[00000001-00000024].tif`) is
        one clip when files match it. A path that does not exist and a 0-byte file make NO clip and
        raise NOTHING: the answer is [], without an error."""
        paths = list(path) if isinstance(path, (list, tuple)) else [path]
        self.import_calls.append(paths)
        made: list[Any] = []
        for one in paths:
            for name in importable_names(str(one)):
                clip = PyClip(
                    name,
                    wiretap_id=f"{destination.get_wiretap_node_id()}/{name}.{next_uid('imp')}",
                    uid=next_uid("clip"),
                )
                adopt(destination, [clip])
                destination._children.append(clip)
                made.append(clip)
        return made

    def duplicate(self, obj: Any) -> Any:
        """Duplicates as `flame.duplicate` does (measured on Flame 2025.2.4).

        A clip -> a PyClip with a readable name and
        uid, in the same reel; a reel -> a PyReel `<name> copy` at the end of its parent, its
        contents copied; a SEQUENCE -> a wrapper typed PyClip whose name, uid and parent read
        None (the new sequence is in the reel all the same)."""
        self.duplicate_calls.append(obj)
        kind = type(obj).__name__
        parent = obj.parent
        if kind == "PyReel":
            name = f"{obj.name.get_value()} copy"
            made = PyReel(
                name, next_uid("reel"), f"{parent.get_wiretap_node_id()}/{name}", reel_type="Reel"
            )
            for child in obj.children:
                made._children.append(clone_entry(child, made))
            adopt(made, made._children)
            if type(parent).__name__ == "PyReelGroup":
                parent.reels.append(made)
                parent.sync_children()
            else:
                adopt(parent, [made])
                parent._children.append(made)
            return made
        copy = clone_entry(obj, parent)
        adopt(parent, [copy])
        parent._children.append(copy)
        if ACTIVE_TIMELINE:
            timeline = ACTIVE_TIMELINE[0]
            timeline.show_new_copy(copy, timeline.duplicate_moves_display_until_open)
        return copy if kind == "PyClip" else UnreadableWrapper()

    def exit(self) -> None:
        self.exited = True

    def _container_of(self, entry: Any) -> Any:
        for obj in self.all_objects():
            if type(obj).__name__ in ("PyReel", "PyLibrary", "PyFolder") and entry in obj.children:
                return obj
        return None

    def delete(self, obj: Any, confirm: bool = True) -> bool:
        """Deletes as `flame.delete(obj, confirm=False)` does (measured on Flame 2025.2.4).

        True for a Batch group, a reel group, a reel, a clip. A schematic reel of a Batch group also
        takes the Clip nodes that use its clips; deleting the LAST reel of a group makes Flame
        create the default reels again."""
        kind = type(obj).__name__
        owner = getattr(obj, "owner", None)
        if kind == "PyReel" and isinstance(owner, PyBatch) and obj in owner.reels:
            return owner.delete_reel(obj)
        if kind == "PyBatch":
            self._desktop.remove_batch_group(obj)
            return True
        if kind == "PyReelGroup":
            self._desktop.reel_groups = [g for g in self._desktop.reel_groups if g is not obj]
            self._desktop.sync_children()
            return True
        if kind == "PyLibrary":
            workspace = self.projects.current_project.current_workspace
            workspace._libraries = [lib for lib in workspace._libraries if lib is not obj]
            return True
        if kind == "PyReel":
            for group in self._desktop.reel_groups:
                if obj in group.reels:
                    group.reels = [r for r in group.reels if r is not obj]
                    if not group.reels:
                        group.add_default_reels()
                    group.sync_children()
                    return True
            for library in self.projects.current_project.current_workspace.libraries:
                if obj in library._children:
                    library._children = [c for c in library._children if c is not obj]
                    return True
        if kind == "PyTrack":
            from fake_timeline_build import delete_track

            return delete_track(obj)
        if kind in ("PyClip", "PySequence"):
            holder = self._container_of(obj)
            if holder is None:
                for batch in self._desktop.batch_groups:
                    for reel in [*batch.reels, *batch.shelf_reels]:
                        if obj in reel._children:
                            holder = reel
            if holder is not None:
                holder._children = [c for c in holder._children if c is not obj]
                shown = ACTIVE_TIMELINE[0].clip if ACTIVE_TIMELINE else None
                if shown is obj:
                    # measured on Flame 2025.2.4: deleting the sequence the Timeline shows leaves
                    # it showing nothing -- `flame.timeline.clip` is None, never another sequence
                    ACTIVE_TIMELINE[0].clip = None
                if isinstance(getattr(holder, "owner", None), PyBatch):
                    batch_owner = holder.owner
                    for node in [
                        n for n in batch_owner.nodes if type(n) is PyClipNode and n.clip is obj
                    ]:
                        batch_owner.remove_node(node)
                return True
        raise RuntimeError(f"the fake cannot delete {kind}")


def build_default_tree() -> tuple[FakeFlame, dict[str, Any]]:
    """library P1O -> reel R1 -> clip1/clip2/clip3, plus a standalone
    sequence Seq1 (1 version / 1 track / 2 segments). Returns
    `(flame, handles)`; `handles` keys: lib, reel, clip1, clip2, clip3, seq,
    seg0, seg1."""
    clip1 = PyClip("clip1", wiretap_id="/projects/p/ws/P1O/R1/clip1")
    clip2 = PyClip("clip2", wiretap_id="/projects/p/ws/P1O/R1/clip2")
    clip3 = PyClip("clip3", wiretap_id="/projects/p/ws/P1O/R1/clip3")
    reel = PyReel(
        "R1", uid="uid-r1", wiretap_id="/projects/p/ws/P1O/R1", children=[clip1, clip2, clip3]
    )
    library = PyLibrary("P1O", uid="uid-lib", wiretap_id="/projects/p/ws/P1O", children=[reel])

    seg0 = PySegment(
        name="seg0", source_name="clip1", record_in=0, record_out=9, record_duration=10
    )
    seg1 = PySegment(
        name="seg1", source_name="clip2", record_in=10, record_out=19, record_duration=10
    )
    seg0.selected = True
    seg1.selected = True  # PySequence.selected_segments answers these two
    track = PyTrack([seg0, seg1])
    version = PyVersion([track])
    seq = PySequence("Seq1", wiretap_id="/projects/p/ws/P1O/Seq1", versions=[version])

    wiretap_index = {
        library.get_wiretap_node_id(): library,
        reel.get_wiretap_node_id(): reel,
        clip1.get_wiretap_node_id(): clip1,
        clip2.get_wiretap_node_id(): clip2,
        clip3.get_wiretap_node_id(): clip3,
        seq.get_wiretap_node_id(): seq,
    }
    flame = FakeFlame(libraries=[library], wiretap_index=wiretap_index)
    handles = {
        "lib": library,
        "reel": reel,
        "clip1": clip1,
        "clip2": clip2,
        "clip3": clip3,
        "seq": seq,
        "seg0": seg0,
        "seg1": seg1,
    }
    return flame, handles


import fake_library_batch  # noqa: E402,F401  (create_library rename, Batch group copies)
import fake_segment_edit  # noqa: E402,F401  (slip, smart_replace*, create_effect, match, ...)
import fake_selection_connections  # noqa: E402,F401  (extract / lift, segment connections)
import fake_timeline_build  # noqa: E402,F401  (overwrite, insert, cut, create_*)
