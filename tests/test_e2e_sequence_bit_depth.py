"""The test sequences of the real-Flame e2e scripts are made with the bit depth of their media.

Flame puts a Resize on each segment of a sequence whose bit depth differs from the media's, and a
sequence made without a bit depth gets the project's own (10 in one test project, 8 in another).
These tests run the REAL make-sequence code of the scripts (the in-process e2e runs swap in a fake
version of it) against the fake Flame, with a project default that differs from the media."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import fake_timeline_build
from fake_media_tree import FakeFlame, PySegment
from p33_helpers import import_with_media
from test_p2_3d_e2e_inprocess import load

FPS = "23.976 fps"
PROJECT_DEFAULT = 8
CLIPS = ("clip1", "p2_len72")


def run_code(template: str, flame: FakeFlame, **values: Any) -> dict[str, Any]:
    journal = load("p2_2a_journal_e2e")
    read = load("p3_1a_read_e2e")
    code = "\n".join([journal.V_PRELUDE, read.PRELUDE, read.fill(template, **values)])
    namespace: dict[str, Any] = {"flame": flame, "__name__": "__flame_mcp_exec__"}
    exec(compile(code, "<test>", "exec"), namespace)
    result: dict[str, Any] = namespace["_result"]
    return result


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    monkeypatch.setattr(fake_timeline_build, "PROJECT_BIT_DEPTH", PROJECT_DEFAULT)
    monkeypatch.setattr(FakeFlame, "import_clips", import_with_media(FakeFlame))
    flame = FakeFlame()
    group = flame.projects.current_project.current_workspace.desktop.create_reel_group("RG")
    source, sequences = group.create_reel("SRC"), group.create_reel("SEQ")
    clips = {}
    for name in CLIPS:
        path = tmp_path / f"{name}.mov"
        path.write_bytes(b"x")
        clips[name] = flame.import_clips(str(path), source)[0]
    return SimpleNamespace(flame=flame, reel=sequences, clips=clips)


def segment_effects(flame: FakeFlame, wiretap: str) -> list[list[str]]:
    seq = flame.find_by_wiretap_node_id(wiretap)
    segments: list[PySegment] = [
        s for t in seq.versions[0].tracks for s in t.segments if s.type != "Gap"
    ]
    return [[fx.type for fx in s.effects] for s in segments]


def puts(world: SimpleNamespace, *names: str) -> list[list[Any]]:
    return [[i, world.clips[n].get_wiretap_node_id(), 1] for i, n in enumerate(names)]


def make_seq(world: SimpleNamespace) -> dict[str, Any]:
    template = load("p3_3_e2e_common").MAKE_SEQ_TEMPLATE
    return run_code(
        template,
        world.flame,
        REEL=world.reel.get_wiretap_node_id(),
        NAME="S",
        FPS=FPS,
        WIDTH=1920,
        HEIGHT=1080,
        PUTS=puts(world, *CLIPS),
        TRIMS=[],
        FXS=[],
    )


def make_s1(world: SimpleNamespace) -> dict[str, Any]:
    template = load("p3_2a_e2e_common").MAKE_S1_TEMPLATE
    return run_code(
        template,
        world.flame,
        REEL=world.reel.get_wiretap_node_id(),
        NAME="S",
        FPS=FPS,
        WIDTH=1920,
        HEIGHT=1080,
        PUTS=puts(world, *CLIPS),
        TRIM=False,
        HANDLE=10,
        FX=False,
        RATIO=1.5,
    )


def make_sequence(world: SimpleNamespace) -> dict[str, Any]:
    template = load("p3_1a_read_e2e").MAKE_SEQUENCE_CODE
    return run_code(
        template,
        world.flame,
        REEL=world.reel.get_wiretap_node_id(),
        NAME="S",
        FPS=FPS,
        WIDTH=1920,
        HEIGHT=1080,
        START="",
        TRACKS=2,
        PUTS=puts(world, *CLIPS),
    )


MAKERS = (make_seq, make_s1, make_sequence)


def test_the_fake_project_default_differs_from_the_media_so_a_default_sequence_gets_a_resize(
    world: SimpleNamespace,
) -> None:
    seq = world.reel.create_sequence(name="X", video_tracks=1, frame_rate=FPS)
    clip = world.clips["clip1"]
    seq.overwrite(clip, world.flame.PyTime(1), seq.versions[0].tracks[0])
    assert (seq.bit_depth, clip.bit_depth) == (PROJECT_DEFAULT, 10)
    assert segment_effects(world.flame, seq.get_wiretap_node_id()) == [["Resize"]]


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
def test_the_sequence_is_made_with_the_bit_depth_of_the_clips_not_the_project_default(
    world: SimpleNamespace, make: Any
) -> None:
    result = make(world)
    seq = world.flame.find_by_wiretap_node_id(result["wt"])
    assert seq.bit_depth == 10 != PROJECT_DEFAULT
    assert result["bitDepth"] == 10 and result["seqBitDepth"] == 10
    assert result["clipBitDepths"] == [10, 10]
    assert segment_effects(world.flame, result["wt"]) == [[], []]
    read = load("p3_1a_read_e2e")
    assert read.bit_depth_ok(result) is True


@pytest.mark.parametrize("make", MAKERS, ids=lambda f: f.__name__)
def test_a_clip_of_another_bit_depth_gives_the_sequence_that_bit_depth(
    world: SimpleNamespace, make: Any
) -> None:
    for clip in world.clips.values():
        clip.bit_depth = 12
    result = make(world)
    assert result["bitDepth"] == 12 and result["seqBitDepth"] == 12
    assert segment_effects(world.flame, result["wt"]) == [[], []]


def test_clips_of_two_bit_depths_cannot_both_match_and_the_make_step_is_judged_not_ok(
    world: SimpleNamespace,
) -> None:
    world.clips["p2_len72"].bit_depth = 12
    result = make_seq(world)
    assert result["clipBitDepths"] == [10, 12] and result["seqBitDepth"] == 10
    assert load("p3_1a_read_e2e").bit_depth_ok(result) is False
    assert segment_effects(world.flame, result["wt"]) == [[], ["Resize"]]


@pytest.mark.parametrize(
    "result",
    [None, {"wt": "x"}, {"bitDepth": None, "seqBitDepth": 8, "clipBitDepths": []}],
    ids=["no result", "no bit depth keys", "no clip put"],
)
def test_a_make_result_with_nothing_to_compare_is_not_judged(result: Any) -> None:
    assert load("p3_1a_read_e2e").bit_depth_ok(result) is True


def test_a_sequence_that_reads_back_another_depth_than_asked_is_judged_not_ok() -> None:
    result = {"bitDepth": 10, "seqBitDepth": 8, "clipBitDepths": [10]}
    assert load("p3_1a_read_e2e").bit_depth_ok(result) is False
