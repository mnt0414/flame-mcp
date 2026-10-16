"""Fake segment-editing calls, kept in a module of their own.

Adds to the fake Flame tree (`fake_media_tree`) what 2025.2.4 does for `PySegment.slip`,
`smart_replace_media` / `smart_replace`, `create_effect`, `copy_to_media_panel`, `match` and for a
`PyTimelineFX` (`save_setup`, `load_setup`, `flame.delete(fx)`). Measured on
Flame 2025.2.4 unless a docstring says INVENTED: where nothing was measured the fake
does something plain and a test that leans on it says so.

MEDIA MODEL (the fake knows no media file): a segment's clip is the range
`[source_in - head, source_out + tail]` of absolute source frames; a clip's own media is its first
segment's range the same way (or `clip._media = (start_tc, length)` when a test wants a clip whose
timecode starts somewhere else). `smart_replace*` keeps the OFFSET from the start of the clip
(= the segment's head), never the timecode. `source_out` and `record_out` count the last frame in
(source 10-61 is 52 frames), so a length is `source_out - source_in + 1` everywhere.
"""

from __future__ import annotations

import json
import os
from typing import Any

import fake_media_tree as fmt
from fake_media_tree import (
    FakeAttr,
    PyClip,
    PySegment,
    PyTime,
    PyTimelineFX,
    PyTrack,
    PyVersion,
    adopt,
    in_library,
    next_uid,
)

LETTERBOX_MODES = (
    "Custom",
    "1.33:1 (4:3)",
    "1.37:1 (Academy)",
    "1.43:1 (IMAX)",
    "1.66:1",
    "1.78:1 (16:9)",
    "1.85:1 (Flat)",
    "1.90:1",
    "2.00:1",
    "2.20:1 (70mm)",
    "2.35:1 (CinemaScope)",
    "2.39:1",
    "2.40:1",
    "2.55:1",
    "2.59:1",
    "2.76:1 (Ultra Panavision)",
    "1:1 (Square)",
    "0.56:1 (9:16 Vertical)",
)
"""18 candidates (Flame 2025.2.4 reports 18; the first and the last are named, the rest are
placeholders)."""

SETUP_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "Blur": (".blur_node",),
    "Resize": (".resize_node",),
    "Colour Correct": (".correct_node",),
    "2D Transform": (".2dtransform_node",),
    "Flip": (".flip_node",),
    "Timewarp": (".timewarp_node",),
    "Burn-in Letterbox": (".letterbox", ".letterbox_node"),
}
"""The files `save_setup` writes next to the path it is given, by effect type (measured)."""

MATCH_ANSWERS_LIST = True
"""What `PySegment.match` answers: a list of clips (True) or the clip itself (False). The docstring
says "a PyClip or a list"; the measurement on Flame 2025.2.4 did not record which -- a test flips
this to cover both."""

LOCKED = "Clip is locked."
NO_SUCH_TYPE = (
    "Could not create an effect of given effect type. Check effect_types property for help."
)
ALREADY_THERE = "An effect of this type already exists."
BAD_SETUP_PATH = "Could not load the setup. Check the filepath."


def with_values(attr: FakeAttr, values: tuple[Any, ...]) -> FakeAttr:
    attr.values = values
    return attr


def letterbox_attributes(fx: PyTimelineFX) -> None:
    """Gives a Burn-in Letterbox its mode / ratio / colour attributes.

    Each carries the `values` Flame 2025.2.4 reports (an enumeration, (min, max), the (low, high)
    corners of a colour), with the defaults Flame showed."""
    fx.mode = with_values(FakeAttr("2.35:1 (CinemaScope)"), LETTERBOX_MODES)
    fx.ratio = with_values(FakeAttr(2.35), (0.01, 100.0))
    fx.colour = with_values(FakeAttr((0.0, 0.0, 0.0)), ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)))
    for name in ("mode", "ratio", "colour"):
        if name not in fx.attributes:
            fx.attributes.append(name)


def new_effect(fx_type: str) -> PyTimelineFX:
    fx = PyTimelineFX(fx_type)
    if fx_type == "Burn-in Letterbox":
        letterbox_attributes(fx)
    else:
        fx.hidden = {"amount": 1.0}  # a parameter that is NOT an attribute
    return fx


def effect_setup(fx: PyTimelineFX) -> dict[str, Any]:
    attrs = {k: getattr(fx, k).get_value() for k in fx.attributes}
    return {"type": fx.type, "attributes": attrs, "hidden": dict(getattr(fx, "hidden", {}))}


def fx_save_setup(self: PyTimelineFX, path: str) -> bool:
    """Writes one setup file per extension of the type.

    Each holds the effect's attributes and its hidden parameters as JSON."""
    extensions = SETUP_EXTENSIONS.get(self.type, (f".{self.type.lower().replace(' ', '')}_node",))
    blob = json.dumps(effect_setup(self), sort_keys=True)
    for extension in extensions:
        with open(path + extension, "w", encoding="utf-8") as out:
            out.write(blob)
    return True


def fx_load_setup(self: PyTimelineFX, path: str) -> bool:
    """Loads an effect setup; a path that is not there raises and changes nothing.

    A setup of ANOTHER type or a damaged file raises the same error here: INVENTED, not measured.
    A Timewarp setup with a speed shortens the source -- a simplification of the real Flame, which
    also moves head / tail and source_in to the setup's Timing; the fake leaves those alone."""
    if not os.path.isfile(path):
        raise RuntimeError(BAD_SETUP_PATH)
    try:
        with open(path, encoding="utf-8") as src:
            data = json.load(src)
    except ValueError as exc:
        raise RuntimeError(BAD_SETUP_PATH) from exc
    if data.get("type") != self.type:
        raise RuntimeError(BAD_SETUP_PATH)
    for key, value in data["attributes"].items():
        if key in self.attributes:
            getattr(self, key).set_value(tuple(value) if isinstance(value, list) else value)
    self.hidden = dict(data.get("hidden", {}))
    seg = self.parent
    if self.type == "Timewarp" and seg is not None and self.hidden.get("speed") is not None:
        used = seg.record_out.frame - seg.record_in.frame + 1
        seg.source_out = PyTime(seg.source_in.frame + round(used * seg.speed()) - 1)
    return True


def clone_effect(fx: PyTimelineFX) -> PyTimelineFX:
    """Copies an effect as a copy of a sequence carries it over.

    The same attributes (with their `values`), the same settings, the hidden parameters too."""
    made = PyTimelineFX(fx.type)
    for key in fx.attributes:
        source = getattr(fx, key)
        if key == "bypass":
            made.bypass.set_value(source.get_value())
            continue
        setattr(made, key, FakeAttr(source.get_value(), getattr(source, "values", None)))
        made.attributes.append(key)
    made.hidden = dict(getattr(fx, "hidden", {}))
    return made


def locked_track(seg: PySegment) -> Any:
    track = seg.parent
    if track is None or in_library(track):
        raise RuntimeError(LOCKED)
    return track


def seg_slip(self: PySegment, offset: int, *args: Any, **kwargs: Any) -> bool:
    """Slips the source of a segment by `offset` frames, as Flame 2025.2.4 does.

    source_in goes DOWN by n (a positive n takes the source toward its head), record does not
    move, head goes by -n and tail by +n; an amount past the handle stops at the handle (+ at the
    head, - at the tail) WITHOUT an error; the answer is False whatever happened. A segment with a
    Timewarp moves its source range at the speed, not by n (`source_step`); head and tail still go
    by n."""
    locked_track(self)
    n = offset
    if n > 0 and self.head != "infinite":
        n = min(n, int(self.head))
    if n < 0 and self.tail != "infinite":
        n = -min(-n, int(self.tail))
    if n == 0:
        return False
    self.source_in = PyTime(self.source_in.frame - self.source_step(n))
    self.source_out = PyTime(
        self.source_out.frame - self.source_step(n, rounds=self._slip_out_rounds)
    )
    if self.head != "infinite":
        self.head = int(self.head) - n
    if self.tail != "infinite":
        self.tail = int(self.tail) + n
    return False


def clip_media(clip: PyClip) -> tuple[int, int]:
    """(first absolute source frame, length) of a clip's own media."""
    explicit = getattr(clip, "_media", None)
    if explicit is not None:
        return int(explicit[0]), int(explicit[1])
    segments = clip.versions[0].tracks[0].segments if clip.versions[0].tracks else []
    if not segments:
        return 0, 0
    seg = segments[0]
    head = 0 if seg.head == "infinite" else int(seg.head)
    tail = 0 if seg.tail == "infinite" else int(seg.tail)
    used = seg.source_out.frame - seg.source_in.frame + 1
    return seg.source_in.frame - head, head + used + tail


def smart_replace_with(self: PySegment, clip: PyClip, with_fx: bool) -> None:
    """Replaces the media of a segment, as Flame 2025.2.4 does.

    The same range counted from the START OF THE CLIP (the segment's head), not the same
    timecode; record and head stay, tail follows the new clip's length, source_name becomes
    the clip's name. A clip too short for the range changes NOTHING and raises nothing. `smart_
    replace` also replaces the effects with the clip's own."""
    locked_track(self)
    start, length = clip_media(clip)
    offset = 0 if self.head == "infinite" else int(self.head)
    used = self.source_out.frame - self.source_in.frame + 1
    if offset + used > length:
        return
    self.source_name = str(clip.name.get_value())
    self.source_in = PyTime(start + offset)
    self.source_out = PyTime(start + offset + used - 1)
    self.head = offset
    self.tail = length - offset - used
    if with_fx:
        own = clip.versions[0].tracks[0].segments[0].effects if clip.versions[0].tracks else []
        self.effects = [clone_effect(fx) for fx in own]
        for fx in self.effects:
            fx.parent = self


def seg_smart_replace_media(self: PySegment, clip: PyClip) -> None:
    smart_replace_with(self, clip, False)


def seg_smart_replace(self: PySegment, clip: PyClip) -> None:
    smart_replace_with(self, clip, True)


def seg_create_effect(self: PySegment, fx_type: str) -> PyTimelineFX:
    """Creates an effect on a segment, as Flame 2025.2.4 does.

    A type that is not in `effect_types` and a second effect of one type raise RuntimeError; the
    list is NOT in creation order (the new one goes in front here). A segment inside a library is
    locked."""
    locked_track(self)
    if fx_type not in self.effect_types:
        raise RuntimeError(NO_SUCH_TYPE)
    if any(fx.type == fx_type for fx in self.effects):
        raise RuntimeError(ALREADY_THERE)
    fx = new_effect(fx_type)
    fx.parent = self
    self.effects.insert(0, fx)
    return fx


def media_segment(
    seg: PySegment, *, record: tuple[int, int], source: tuple[int, int], head: Any, tail: Any
) -> PySegment:
    return PySegment(
        name=str(seg.name.get_value()),
        source_name=seg.source_name,
        record_in=record[0],
        record_out=record[1],
        source_in=source[0],
        source_out=source[1],
        record_duration=record[1] - record[0] + 1,
        head=head,
        tail=tail,
        source_bit_depth=seg.source_bit_depth,
    )


def extracted_clip(
    seg: PySegment, destination: Any, *, kind: str, preserve_handle: bool, with_fx: bool
) -> PyClip:
    """Makes the clip `copy_to_media_panel` / `match` make.

    Measured on Flame 2025.2.4 for a Timewarp segment: used range 52 frames, head 10, tail 10,
    record 112-163 -- here `used`, `head`, `tail`, `record_in`:
      copy              duration used, start_frame 1 + head, the segment as it is (record,
                        source range, head, tail) and its effects
      match             duration used (the marked range), start_frame 1, in_mark head / out_mark
                        head + used, the WHOLE media (source 0..length, head 0, tail 0), no effect
      match preserve    duration used, start_frame 1 + head, record = source offsets, head, tail,
                        no effect
      match with fx     duration = length, source head..head+length, head, tail 'infinite',
                        effects kept (observed as it is: a shape unlike the others)
    No handles: all four are a handle-0 clip, no error."""
    head = 0 if seg.head == "infinite" else int(seg.head)
    tail = 0 if seg.tail == "infinite" else int(seg.tail)
    s_in, s_out = seg.source_in.frame, seg.source_out.frame
    used = s_out - s_in + 1
    length = head + used + tail
    start_tc = s_in - head
    in_mark: PyTime | None = None
    out_mark: PyTime | None = None
    start_frame = 1 + head
    if kind == "copy":
        inner = media_segment(
            seg,
            record=(seg.record_in.frame, seg.record_out.frame),
            source=(s_in, s_out),
            head=seg.head,
            tail=seg.tail,
        )
        duration = used
    elif with_fx:
        inner = media_segment(
            seg,
            record=(head, head + length - 1),
            source=(head, head + length - 1),
            head=head,
            tail="infinite",
        )
        duration = length
    elif preserve_handle:
        inner = media_segment(
            seg, record=(head, head + used - 1), source=(s_in, s_out), head=head, tail=tail
        )
        duration = used
    else:
        inner = media_segment(
            seg, record=(0, length - 1), source=(start_tc, start_tc + length - 1), head=0, tail=0
        )
        duration = used
        start_frame = 1
        in_mark, out_mark = PyTime(head), PyTime(head + used)
    if kind == "copy" or with_fx:
        inner.effects = [clone_effect(fx) for fx in seg.effects]
        for fx in inner.effects:
            fx.parent = inner
    name = str(seg.name.get_value()) or str(seg.source_name)
    clip = PyClip(
        name,
        wiretap_id=f"{destination.get_wiretap_node_id()}/{name}.{next_uid('ex')}",
        uid=next_uid("clip"),
        duration=duration,
        start_frame=start_frame,
        versions=[PyVersion([PyTrack([inner])])],
    )
    clip.in_mark = in_mark
    clip.out_mark = out_mark
    clip._media = (start_tc, length)
    return clip


def put_into(destination: Any, clip: PyClip) -> None:
    adopt(destination, [clip])
    destination._children.append(clip)


def seg_copy_to_media_panel(self: PySegment, destination: Any) -> PyClip:
    clip = extracted_clip(self, destination, kind="copy", preserve_handle=False, with_fx=False)
    put_into(destination, clip)
    return clip


def seg_match(
    self: PySegment,
    destination: Any,
    preserve_handle: bool = False,
    use_sequence_info: bool = True,
    include_nested_content: bool = False,
    include_timeline_fx: bool = False,
) -> Any:
    """See `extracted_clip` for the shapes; the answer follows `MATCH_ANSWERS_LIST`."""
    clip = extracted_clip(
        self,
        destination,
        kind="match",
        preserve_handle=preserve_handle,
        with_fx=include_timeline_fx,
    )
    put_into(destination, clip)
    return [clip] if MATCH_ANSWERS_LIST else clip


def install() -> None:
    PySegment.slip = seg_slip  # type: ignore[method-assign]
    PySegment.smart_replace_media = seg_smart_replace_media  # type: ignore[method-assign]
    PySegment.smart_replace = seg_smart_replace  # type: ignore[method-assign]
    PySegment.create_effect = seg_create_effect  # type: ignore[method-assign]
    PySegment.copy_to_media_panel = seg_copy_to_media_panel  # type: ignore[method-assign]
    PySegment.match = seg_match  # type: ignore[method-assign]
    PyTimelineFX.save_setup = fx_save_setup  # type: ignore[method-assign]
    PyTimelineFX.load_setup = fx_load_setup  # type: ignore[method-assign]
    fmt.clone_effect = clone_effect
    original_delete = fmt.FakeFlame.delete

    def delete(self: Any, obj: Any, confirm: bool = True) -> bool:
        """Deletes like `flame.delete`; an effect of a segment in a library is locked.

        `flame.delete(fx, confirm=False)` answers True and the effect leaves `seg.effects`."""
        if type(obj).__name__ == "PyTimelineFX":
            seg = obj.parent
            if seg is None or seg.parent is None or in_library(seg.parent):
                raise RuntimeError(LOCKED)
            seg.effects = [fx for fx in seg.effects if fx is not obj]
            return True
        return bool(original_delete(self, obj, confirm))

    fmt.FakeFlame.delete = delete  # type: ignore[method-assign]


install()  # importing the module is what puts these behaviours on the fake classes
