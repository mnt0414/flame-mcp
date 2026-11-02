"""Tests how `segment.replace` judges that a replace took effect.

Measured on Flame 2026 (261029_nitto): a replace of a clip with the same source name and range
changed only the segment's `name` and `sourceWidth/Height`, and a comp swapped for a same-name,
same-size comp changed nothing a read can see. sourceName / range / head / tail alone called 57 of
66 real replaces SILENT_NOOP. The verdict is predicted: something moved -> ok and verified; nothing
moved and the new clip differs from the segment -> a real no-op; nothing moved and it does not ->
ok, verified false."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr, PyClip, PySegment
from journal_test_helpers import Rig
from p33_helpers import REPLACE, a_seg, add_media_clip, clip_sel, seg_a
from timeline_p32a_helpers import make_timeline_rig


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def replace(rig: Rig, clip: PyClip) -> dict[str, Any]:
    return rig.do(REPLACE, {"target": a_seg(), "source": clip_sel(clip)})


def flame_changes_only(seg: PySegment, **attrs: Any) -> None:
    """Flame's answer measured on 261029_nitto: the call returns None and only these change."""

    def smart_replace_media(clip: PyClip) -> None:
        for key, value in attrs.items():
            setattr(seg, key, FakeAttr(value) if key == "name" else value)

    seg.smart_replace_media = smart_replace_media  # type: ignore[method-assign]


def test_a_changed_segment_name_is_a_replace_that_took_effect(rig: Rig) -> None:
    seg = seg_a(rig)
    flame_changes_only(seg, name="newclip")
    env = replace(rig, add_media_clip(rig, "newclip", start=0, length=48))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["changed"] == ["name"]
    assert result["verified"] is True
    assert result["before"]["name"] == "A" and result["after"]["name"] == "newclip"


def test_a_changed_source_size_alone_is_a_replace_that_took_effect(rig: Rig) -> None:
    flame_changes_only(seg_a(rig), source_width=3840, source_height=2160)
    env = replace(rig, add_media_clip(rig, "bigger", start=0, length=48))
    assert env["ok"] is True, env
    assert env["result"]["changed"] == ["sourceWidth", "sourceHeight"]
    assert env["result"]["verified"] is True


def test_a_changed_source_height_alone_is_a_replace_that_took_effect(rig: Rig) -> None:
    flame_changes_only(seg_a(rig), source_height=2160)
    env = replace(rig, add_media_clip(rig, "taller", start=0, length=48))
    assert env["ok"] is True, env
    assert env["result"]["changed"] == ["sourceHeight"]
    assert env["result"]["verified"] is True


def test_a_changed_source_uid_alone_is_shown_but_not_judged(rig: Rig) -> None:
    seg = seg_a(rig)
    seg.source_uid = "uid-old"  # type: ignore[attr-defined]
    flame_changes_only(seg, source_uid="uid-new")
    env = replace(rig, add_media_clip(rig, "other", start=0, length=48))
    assert env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["changed"] == []
    assert env["result"]["after"]["sourceUid"] == "uid-new"


def test_a_changed_file_path_alone_with_a_look_alike_clip_is_not_verified(rig: Rig) -> None:
    seg = seg_a(rig)
    seg.name = FakeAttr("comp")
    seg.file_path = "/a.exr"
    flame_changes_only(seg, file_path="/b.exr")
    env = replace(rig, add_media_clip(rig, "comp", start=0, length=48))
    assert env["ok"] is True and env["result"]["changed"] == [], env
    assert env["result"]["verified"] is False


def test_media_and_fx_mode_is_judged_the_same_way(rig: Rig) -> None:
    seg = seg_a(rig)

    def smart_replace(clip: PyClip) -> None:
        seg.name = FakeAttr("newclip")

    seg.smart_replace = smart_replace  # type: ignore[method-assign]
    clip = add_media_clip(rig, "newclip", start=0, length=48)
    env = rig.do(REPLACE, {"target": a_seg(), "source": clip_sel(clip), "mode": "media_and_fx"})
    assert env["ok"] is True, env
    assert env["result"]["changed"] == ["name"] and env["result"]["verified"] is True


def test_an_attribute_that_raises_on_read_is_null_and_left_out(rig: Rig) -> None:
    seg = seg_a(rig)

    class Raising(type(seg)):  # type: ignore[misc]
        @property
        def source_height(self) -> int:
            raise RuntimeError("cannot read")

    seg.__class__ = Raising
    flame_changes_only(seg, name="newclip")
    env = replace(rig, add_media_clip(rig, "newclip", start=0, length=48))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["changed"] == ["name"] and result["verified"] is True
    assert result["before"]["sourceHeight"] is None and result["after"]["sourceHeight"] is None


def test_the_clip_the_segment_already_has_is_ok_but_not_verified(rig: Rig) -> None:
    seg = seg_a(rig)
    seg.name = FakeAttr("clip1")
    env = replace(rig, add_media_clip(rig, "clip1", start=0, length=34))
    assert env["ok"] is True, env
    assert env["result"]["changed"] == [] and env["result"]["verified"] is False


def test_nothing_moved_and_the_clip_differs_from_the_segment_is_a_real_noop(rig: Rig) -> None:
    flame_changes_only(seg_a(rig))
    env = replace(rig, add_media_clip(rig, "different", start=0, length=48))
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["changed"] == []
    assert "name 'A'" in env["error"] and "source size 1920x1080" in env["error"]


def test_nothing_moved_and_a_clip_of_another_size_is_a_real_noop(rig: Rig) -> None:
    seg = seg_a(rig)
    seg.name = FakeAttr("same")
    flame_changes_only(seg)
    clip = add_media_clip(rig, "same", start=0, length=48)
    clip.width = 3840
    env = replace(rig, clip)
    assert env["errorCode"] == "SILENT_NOOP", env


def test_nothing_moved_and_the_clip_looks_like_the_segment_is_ok_but_not_verified(
    rig: Rig,
) -> None:
    seg = seg_a(rig)
    seg.name = FakeAttr("comp_sc16")
    flame_changes_only(seg)
    env = replace(rig, add_media_clip(rig, "comp_sc16", start=0, length=48))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["changed"] == [] and result["verified"] is False
    assert any("export" in n and "compare" in n for n in map(str, result["notices"])), result


def test_an_unreadable_attribute_is_left_out_of_the_comparison(rig: Rig) -> None:
    seg = seg_a(rig)
    del seg.source_width  # a name Flame answers None for
    flame_changes_only(seg)
    env = replace(rig, add_media_clip(rig, "different", start=0, length=48))
    assert env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["before"]["sourceWidth"] is None


def test_an_unreadable_attribute_does_not_make_a_change(rig: Rig) -> None:
    seg = seg_a(rig)
    seg.name = FakeAttr("comp_sc16")
    del seg.source_width

    def smart_replace_media(clip: PyClip) -> None:
        seg.source_width = 1920  # unreadable before, readable after: not a comparison

    seg.smart_replace_media = smart_replace_media  # type: ignore[method-assign]
    env = replace(rig, add_media_clip(rig, "comp_sc16", start=0, length=48))
    assert env["ok"] is True and env["result"]["changed"] == []
    assert env["result"]["verified"] is False


def test_the_existing_sourcename_signal_is_still_verified(rig: Rig) -> None:
    env = replace(rig, add_media_clip(rig, "newclip", start=0, length=48))
    assert env["ok"] is True, env
    assert env["result"]["changed"] == ["sourceName", "tail"]
    assert env["result"]["verified"] is True
