"""Tests `timeline.move_track`.

There is no native call that moves a track (measured on Flame 2025.2.4), so the op rebuilds it: the
track is copied to the stash reel, a new track is created at the target, the clip is overwritten
at the first media record_in, the source track is deleted and the by-product clip is deleted. The
tests cover what is refused before anything is copied or journaled, the track index sent to
`create_track`, the order read back, the by-product clean-up, the attributes copy, the audio
report and the checkpoint. Run through the real Runtime with a scripted `transport.exec` over the
fake Flame tree; what the fake does for `PyTrack.copy_to_media_panel`, `create_track(track_index)`,
`flame.delete(track)` and the overwrite of a copied track follows the measurements
(fake_timeline_build.py), the rest is marked INVENTED there."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import fake_media_tree as fmt
import fake_timeline_build as ftb
from fake_media_tree import (
    FakeAttr,
    PyAudioChannel,
    PyAudioTrack,
    PySegment,
    PySequence,
    PyTimelineFX,
    PyTrack,
    PyTransition,
    PyVersion,
    adopt,
)
from fake_selection_connections import connect
from flame_mcp import opschema
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import untouched_world
from timeline_p32a_helpers import (
    CLIPX,
    S1,
    S2,
    checkpoints,
    fingerprint_of_target,
    gap,
    library_copy,
    make_timeline_rig,
    media,
    rows,
    stash_entries,
    stash_library,
    wt,
)

OP = "timeline.move_track"
M4_WT = "/wt/RG/Reel 1/M4"
M4 = wt(M4_WT)
GAP = "Gap"
VIDEO = "Video Segment"


def m4_tracks() -> list[list[PySegment]]:
    """track 0: Gap 0-9 | clipA 10-33 | Gap 34-39 | clipB 40-63; track 1: clipC 0-47 | Gap 48-63;
    track 2: Gap 0-63 (no media); track 3: Gap 0-4 | clipD 5-20 | Gap 21-63."""
    return [
        [
            gap(0, 9),
            media("clipA", 10, 33, name="A"),
            gap(34, 39),
            media("clipB", 40, 63, name="B"),
        ],
        [media("clipC", 0, 47, name="C"), gap(48, 63)],
        [gap(0, 63)],
        [gap(0, 4), media("clipD", 5, 20, name="D"), gap(21, 63)],
    ]


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    r = make_timeline_rig(monkeypatch, tmp_path)
    reel = r.handles["reel"]
    seq = PySequence(
        "M4",
        wiretap_id=M4_WT,
        duration=64,
        versions=[PyVersion([PyTrack(t) for t in m4_tracks()])],
    )
    adopt(reel, [seq])
    reel._children.append(seq)
    r.handles["m4"] = seq
    return r


def move(rig: Rig, track: int, to: int, sequence: dict[str, Any] = M4, **extra: Any) -> Any:
    return rig.do(OP, {"sequence": sequence, "track": track, "to": to, **extra})


def signature(seq: PySequence, track: int) -> list[Any]:
    return [
        [
            s.type,
            s.record_in.frame,
            s.record_out.frame,
            s.source_in.frame,
            s.source_out.frame,
            s.source_name,
            str(s.name.get_value()),
            [fx.type for fx in s.effects],
        ]
        for s in seq.versions[0].tracks[track].segments
    ]


def order(seq: PySequence) -> list[list[Any]]:
    return [signature(seq, t) for t in range(len(seq.versions[0].tracks))]


def starting_order(rig: Rig) -> list[list[Any]]:
    return order(rig.handles["m4"])


def expected_after(before: list[list[Any]], track: int, to: int) -> list[list[Any]]:
    moved = list(before)
    moved.insert(to, moved.pop(track))
    return moved


@pytest.mark.parametrize(
    ("track", "to"),
    [(3, 0), (0, 3), (3, 1), (0, 2), (1, 2), (2, 0), (1, 0)],
    ids=[
        "top-to-bottom",
        "bottom-to-top",
        "down-middle",
        "up-middle",
        "up-one",
        "gap-only",
        "down-one",
    ],
)
def test_a_track_ends_up_at_the_target_and_the_others_keep_their_order(
    rig: Rig, track: int, to: int
) -> None:
    before = starting_order(rig)
    env = move(rig, track, to)
    assert env["ok"] is True, env
    assert order(rig.handles["m4"]) == expected_after(before, track, to)
    result = env["result"]
    assert result["moved"]["from"] == track and result["moved"]["to"] == to
    assert result["verification"]["ok"] is True and result["verification"]["problems"] == []
    assert result["changed"] is True and result["warnings"] == []
    assert [t["track"] for t in result["after"]["tracks"]] == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("track", "to", "k"),
    [(3, 0, 1), (3, 1, 2), (2, 0, 1), (0, 2, 4), (1, 2, 4), (0, 3, -1), (1, 3, -1), (2, 3, -1)],
)
def test_the_one_based_track_index_sent_to_create_track_puts_the_new_track_at_the_target(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, track: int, to: int, k: int
) -> None:
    sent: list[Any] = []
    original = PyVersion.create_track

    def spy(self: PyVersion, track_index: int | None = None) -> PyTrack:
        sent.append(track_index)
        return original(self, track_index)

    monkeypatch.setattr(PyVersion, "create_track", spy)
    env = move(rig, track, to)
    assert env["ok"] is True, env
    assert sent == [k] and env["result"]["moved"]["k"] == k


def test_the_moved_track_equals_the_source_row_for_row_and_keeps_its_connections(
    rig: Rig,
) -> None:
    seq = rig.handles["m4"]
    other = media("clipA", 0, 5, name="X")
    seq.versions[0]._tracks[2]._segments[:] = [other, gap(6, 63)]
    adopt(seq.versions[0]._tracks[2], seq.versions[0]._tracks[2]._segments)
    source = seq.versions[0].tracks[0].segments[1]
    connect(source, other)
    assert len(source.connected_segments()) == 2  # the list holds the segment itself
    env = move(rig, 0, 3)
    assert env["ok"] is True, env
    top = seq.versions[0].tracks[3]
    placed = [s for s in top.segments if s.type != GAP]
    assert [s.source_name for s in placed] == ["clipA", "clipB"]
    assert [len(s.connected_segments()) for s in placed] == [2, 1]  # each lists itself too
    assert len(other.connected_segments()) == 2


def test_the_by_product_clip_is_found_reported_and_deleted_and_nothing_else_is(rig: Rig) -> None:
    keep = len(stash_entries(rig))
    env = move(rig, 3, 0)
    assert env["ok"] is True, env
    result = env["result"]
    assert (
        result["clip"]["duration"]["frame"] == 16
        and result["clip"]["name"]
        and result["clip"]["uid"]
    )
    assert result["cleanup"]["clip"]["attempted"] is True
    assert result["cleanup"]["clip"]["deleted"] is True
    assert result["cleanup"]["clip"]["error"] is None and result["cleanup"]["track"] is None
    assert len(stash_entries(rig)) == keep + 1  # only the sequence checkpoint stays there
    assert all("track" not in str(e.name.get_value()) for e in stash_entries(rig))
    assert result["stash"]["library"] and result["stash"]["reel"]
    assert result["display"] is not None


def test_a_by_product_that_cannot_be_deleted_is_a_warning_not_a_failure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fmt.FakeFlame.delete

    def refuses_clips(self: fmt.FakeFlame, obj: Any, confirm: bool = True) -> bool:
        if type(obj).__name__ == "PyClip" and getattr(obj, "_track_copy", False):
            return False
        return original(self, obj, confirm)

    monkeypatch.setattr(fmt.FakeFlame, "delete", refuses_clips)
    env = move(rig, 3, 0)
    assert env["ok"] is True, env
    assert env["result"]["cleanup"]["clip"]["deleted"] is False
    assert any("by-product" in w for w in env["result"]["warnings"])


def test_a_track_of_gaps_only_is_moved_by_creating_the_empty_track_and_deleting_the_source(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        PySequence,
        "overwrite",
        lambda *a, **k: calls.append("overwrite"),
    )
    monkeypatch.setattr(PyTrack, "copy_to_media_panel", lambda *a, **k: calls.append("copy"))
    before = starting_order(rig)
    env = move(rig, 2, 0)
    assert env["ok"] is True, env
    assert calls == []
    assert order(rig.handles["m4"]) == expected_after(before, 2, 0)
    result = env["result"]
    assert result["clip"] is None and result["stash"] is None
    assert result["cleanup"] == {"clip": None, "track": None}
    assert stash_library(rig) is not None  # the checkpoint of the sequence made it
    assert any("only Gap" in n and "measured on Flame 2025.2.4" in n for n in result["notices"])


def test_the_audio_tracks_are_read_before_and_after_and_do_not_change(rig: Rig) -> None:
    seq = rig.handles["m4"]
    seg = PySegment(
        name="AUD",
        source_name="aud",
        record_in=0,
        record_out=63,
        source_in=0,
        source_out=63,
        record_duration=64,
        type="Audio Segment",
    )
    seq.audio_tracks = [PyAudioTrack(channels=[PyAudioChannel([seg]), PyAudioChannel([])])]
    env = move(rig, 3, 0)
    assert env["ok"] is True, env
    audio = env["result"]["audio"]
    assert audio["changed"] is False and audio["before"] == audio["after"]
    assert audio["before"]["segmentCount"] == 1 and audio["before"]["channelCount"] == 2
    assert any("audio track" in n for n in env["result"]["notices"])


def test_a_sequence_without_audio_tracks_still_moves(rig: Rig) -> None:
    del rig.handles["m4"].audio_tracks
    env = move(rig, 3, 0)
    assert env["ok"] is True, env
    assert env["result"]["audio"]["before"]["trackCount"] is None


def test_the_name_hidden_and_locked_of_the_source_are_put_on_the_new_track(rig: Rig) -> None:
    source = rig.handles["m4"].versions[0].tracks[3]
    source.name.set_value("top")
    source.hidden.set_value(True)
    source.locked.set_value(True)
    env = move(rig, 3, 0)
    assert env["ok"] is True, env
    new = rig.handles["m4"].versions[0].tracks[0]
    assert new.name.get_value() == "top" and new.hidden.get_value() is True
    assert new.locked.get_value() is True
    attrs = env["result"]["attributes"]
    assert sorted(attrs["copied"]) == ["hidden", "locked", "name"] and attrs["failed"] == []
    assert env["result"]["warnings"] == []


def test_an_attribute_that_does_not_stay_is_a_warning_and_never_fails_the_op(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Ignoring(FakeAttr):
        def set_value(self, value: Any) -> None:
            return None

    Ignoring.__name__ = "PyAttribute"
    original = PyVersion.create_track

    def made(self: PyVersion, track_index: int | None = None) -> PyTrack:
        track = original(self, track_index)
        track.hidden = Ignoring(False)
        return track

    monkeypatch.setattr(PyVersion, "create_track", made)
    rig.handles["m4"].versions[0].tracks[3].hidden.set_value(True)
    env = move(rig, 3, 0)
    assert env["ok"] is True, env
    attrs = env["result"]["attributes"]
    assert [f["key"] for f in attrs["failed"]] == ["hidden"]
    assert attrs["failed"][0]["wanted"] is True and attrs["failed"][0]["got"] is False
    assert any("hidden" in w for w in env["result"]["warnings"])


def test_a_copy_that_does_not_match_the_source_stops_before_the_source_is_deleted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySequence, "overwrite", lambda self, clip, time, track: True)
    before = starting_order(rig)
    env = move(rig, 3, 0)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "source track was NOT deleted" in env["error"]
    assert order(rig.handles["m4"]) == before  # the source is still there, the new track is gone
    assert "the track create_track added was removed again" in env["error"]
    assert env["result"]["verification"]["ok"] is False  # it is not the moved order
    assert env["journal"]["revert"]["op"] == "journal.revert"
    cleanup = env["result"]["cleanup"]
    assert cleanup["clip"]["deleted"] is True  # the by-product goes in any case
    assert cleanup["track"]["attempted"] is True and cleanup["track"]["deleted"] is True


def test_an_overwrite_that_raises_removes_the_new_track_and_keeps_the_source(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raises(self: PySequence, clip: Any, time: Any, track: Any) -> Any:
        raise RuntimeError("scripted overwrite failure")

    monkeypatch.setattr(PySequence, "overwrite", raises)
    before = starting_order(rig)
    env = move(rig, 3, 0)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "overwrite" in env["error"] and "scripted overwrite failure" in env["error"]
    assert order(rig.handles["m4"]) == before and env["result"]["changed"] is False
    cleanup = env["result"]["cleanup"]
    assert cleanup["track"]["deleted"] is True and cleanup["clip"]["deleted"] is True
    assert len(stash_entries(rig)) == 1  # the sequence checkpoint only


def test_a_new_track_that_cannot_be_removed_is_a_warning_on_top_of_the_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySequence, "overwrite", lambda self, clip, time, track: True)
    monkeypatch.setattr(ftb, "delete_track", lambda track: False)
    env = move(rig, 3, 0)
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["cleanup"]["track"]["deleted"] is False
    assert any("could not be removed again" in w for w in env["result"]["warnings"])
    assert len(rig.handles["m4"].versions[0].tracks) == 5


def test_a_copy_that_raises_after_making_the_clip_has_its_by_product_deleted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ftb.track_copy_to_media_panel

    def makes_then_raises(track: PyTrack, destination: Any) -> Any:
        original(track, destination)
        raise RuntimeError("scripted copy failure")

    monkeypatch.setattr(PyTrack, "copy_to_media_panel", makes_then_raises)
    before = starting_order(rig)
    env = move(rig, 3, 0)
    assert env["errorCode"] == "FLAME_API_ERROR" and "copy_to_media_panel" in env["error"]
    assert order(rig.handles["m4"]) == before
    assert env["result"]["cleanup"]["clip"]["deleted"] is True
    assert env["result"]["cleanup"]["track"] is None and len(stash_entries(rig)) == 1


def test_a_source_track_that_cannot_be_deleted_is_a_failure_and_the_new_track_stays(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raises(track: PyTrack) -> bool:
        raise RuntimeError("scripted delete failure")

    monkeypatch.setattr(ftb, "delete_track", raises)
    env = move(rig, 3, 0)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "stopped at delete" in env["error"] and "scripted delete failure" in env["error"]
    assert "was removed again" not in env["error"]
    result = env["result"]
    assert result["stage"] == "delete" and result["cleanup"]["track"] is None
    assert result["cleanup"]["clip"]["deleted"] is True and result["changed"] is True
    assert result["verification"]["ok"] is False
    assert len(rig.handles["m4"].versions[0].tracks) == 5  # the new track and the source
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_a_delete_that_answers_true_and_leaves_the_track_is_a_failure_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ftb, "delete_track", lambda track: True)
    env = move(rig, 3, 0)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    result = env["result"]
    assert result["stage"] == "delete" and result["returned"]["delete"] is True
    assert result["cleanup"]["track"] is None and result["verification"]["ok"] is False
    assert any("track(s)" in p for p in result["verification"]["problems"])
    assert len(rig.handles["m4"].versions[0].tracks) == 5
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_the_new_track_is_left_alone_when_the_other_tracks_are_not_as_they_were(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def spoils_another_track(self: PySequence, clip: Any, time: Any, track: Any) -> bool:
        other = self.versions[0].tracks[2]  # the source (list index 3) is now at 4
        other._segments[:] = [gap(0, 31)]
        adopt(other, other._segments)
        return True

    monkeypatch.setattr(PySequence, "overwrite", spoils_another_track)
    env = move(rig, 3, 0)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    cleanup = env["result"]["cleanup"]["track"]
    assert cleanup["attempted"] is False and cleanup["deleted"] is False
    assert "nothing was removed" in cleanup["error"]
    assert "the track create_track added was removed again" not in env["error"]
    assert any("could not be removed again" in w for w in env["result"]["warnings"])
    assert len(rig.handles["m4"].versions[0].tracks) == 5


def test_audio_that_changes_after_the_overwrite_keeps_the_source_and_removes_the_new_track(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = rig.handles["m4"]
    seq.audio_tracks = [PyAudioTrack(channels=[PyAudioChannel([]), PyAudioChannel([])])]
    monkeypatch.setattr(ftb, "TRACK_COPY_TOUCHES_AUDIO", True)  # INVENTED switch of the fake
    before = starting_order(rig)
    env = move(rig, 3, 0)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "audio" in env["error"] and "source track was kept" in env["error"]
    assert order(seq) == before  # the source is still there, the new track is gone
    result = env["result"]
    assert result["audio"]["changed"] is True and result["stage"] == "audio"
    assert result["cleanup"]["track"]["deleted"] is True and result["cleanup"]["clip"]["deleted"]
    assert any("audio tracks changed" in w for w in result["warnings"])
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_an_order_that_is_not_the_expected_one_is_flame_api_error_and_revertible(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ftb, "delete_track", lambda track: True)
    before = fingerprint_of_target(rig, M4)
    env = move(rig, 3, 0)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    problems = env["result"]["verification"]["problems"]
    assert problems and env["result"]["verification"]["ok"] is False
    assert env["result"]["changed"] is True
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_a_create_track_that_raises_leaves_the_sequence_and_cleans_the_by_product(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raises(self: PyVersion, track_index: int | None = None) -> PyTrack:
        raise RuntimeError("Invalid track index.")

    monkeypatch.setattr(PyVersion, "create_track", raises)
    before = starting_order(rig)
    env = move(rig, 3, 0)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "Invalid track index." in env["error"] and "create_track" in env["error"]
    assert order(rig.handles["m4"]) == before
    assert env["result"]["cleanup"]["clip"]["deleted"] is True
    assert env["result"]["cleanup"]["track"] is None  # create_track never added one


def test_the_move_is_reverted_with_the_fingerprint_back(rig: Rig) -> None:
    before = fingerprint_of_target(rig, M4)
    env = move(rig, 3, 0)
    assert env["ok"] is True and fingerprint_of_target(rig, M4) != before
    ref = env["journal"]
    assert ref["changed"] is True and ref["revert"]["args"] == {"group": ref["group"]}
    [record] = checkpoints(rig)
    assert record["id"] == ref["checkpoint"] and record["fingerprint"] == before
    out = rig.do("journal.revert", {"group": ref["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before
    [view] = Index(rig.records()).groups[ref["group"]]
    assert view.intent["op"] == OP and view.intent["args"]["to"] == 0


def test_a_plan_bundles_a_move_with_other_timeline_writes(rig: Rig) -> None:
    env = rig.do(
        "plan.run",
        {
            "steps": [
                {"op": OP, "args": {"sequence": M4, "track": 3, "to": 0}},
                {"op": "timeline.cut", "args": {"sequence": M4, "at": 20, "track": 0}},
            ]
        },
    )
    assert env["ok"] is True, env
    assert len(checkpoints(rig)) == 1
    assert len(rig.handles["m4"].versions[0].tracks[0].segments) == 4  # D's track, cut at 20


def test_it_works_on_the_sequence_shown_in_the_timeline(rig: Rig) -> None:
    fmt.ACTIVE_TIMELINE[0].clip = rig.handles["m4"]
    before = starting_order(rig)
    env = rig.do(OP, {"sequence": {"kind": "timeline"}, "track": 3, "to": 0})
    assert env["ok"] is True, env
    assert order(rig.handles["m4"]) == expected_after(before, 3, 0)


@pytest.mark.parametrize(
    "extra",
    [
        {"track": 1, "to": 1},
        {"track": -1, "to": 0},
        {"track": 1.5, "to": 0},
        {"track": 1, "to": -2},
    ],
    ids=["same", "negative-track", "fractional", "negative-to"],
)
def test_arguments_that_do_not_fit_are_invalid_args(rig: Rig, extra: dict[str, Any]) -> None:
    env = rig.do(OP, {"sequence": M4, **extra})
    assert env["errorCode"] == "INVALID_ARGS"
    assert untouched_world(rig)


@pytest.mark.parametrize("missing", ["track", "to"])
def test_track_and_to_are_required(rig: Rig, missing: str) -> None:
    args = {"sequence": M4, "track": 1, "to": 0}
    del args[missing]
    assert rig.do(OP, args)["errorCode"] == "INVALID_ARGS"
    assert untouched_world(rig)


@pytest.mark.parametrize(
    "extra",
    [
        {"track": 9, "to": 0},
        {"track": 0, "to": 9},
        {"track": 0, "to": 4},
        {"track": 0, "to": 1, "version": 3},
    ],
    ids=["track", "to", "to-count", "version"],
)
def test_a_track_or_version_that_does_not_exist_is_refused_before_anything_is_copied(
    rig: Rig, extra: dict[str, Any]
) -> None:
    env = rig.do(OP, {"sequence": M4, **extra})
    assert env["errorCode"] == "SELECTOR_EMPTY"
    assert untouched_world(rig)


def test_a_sequence_with_one_track_is_refused(rig: Rig) -> None:
    env = rig.do(OP, {"sequence": S2, "track": 0, "to": 1})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "single track" in env["error"]
    assert untouched_world(rig)


def test_cut_transitions_at_every_boundary_are_not_a_reason_to_refuse(rig: Rig) -> None:
    source = rig.handles["m4"].versions[0].tracks[0]
    listed = source.transitions  # measured: one Cut of duration 0 at each boundary
    assert len(listed) == len(source.segments) - 1 == 3
    assert {(t.type.get_value(), t.duration, t.in_offset, t.alignment) for t in listed} == {
        ("Cut", 0, 0, "Custom")
    }
    before = starting_order(rig)
    env = move(rig, 0, 3)
    assert env["ok"] is True, env
    assert order(rig.handles["m4"]) == expected_after(before, 0, 3)


def test_cut_transitions_given_as_plain_values_are_fine_too(rig: Rig) -> None:
    class Plain(PyTransition):
        def __init__(self) -> None:
            super().__init__()
            self.type = "Cut"  # type: ignore[assignment]

    rig.handles["m4"].versions[0]._tracks[0].transitions = [Plain(), Plain(), Plain()]
    assert move(rig, 0, 3)["ok"] is True


@pytest.mark.parametrize(
    ("kind", "duration"),
    [("Dissolve", 12), ("Dissolve", 0), ("Cut", 4)],
    ids=["dissolve", "dissolve-zero", "cut-with-duration"],
)
def test_a_transition_that_is_not_a_cut_of_duration_zero_is_refused(
    rig: Rig, kind: str, duration: int
) -> None:
    track = rig.handles["m4"].versions[0]._tracks[3]
    track.transitions = [*track.transitions, PyTransition(kind, duration)]
    before = starting_order(rig)
    env = move(rig, 3, 0)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "not a Cut of duration 0" in env["error"] and kind in env["error"]
    assert order(rig.handles["m4"]) == before and untouched_world(rig)


def test_a_transition_whose_type_or_duration_cannot_be_read_refuses(rig: Rig) -> None:
    class Unreadable(PyTransition):
        @property
        def duration(self) -> Any:
            raise RuntimeError("no duration")

        @duration.setter
        def duration(self, _value: Any) -> None:
            return None

    track = rig.handles["m4"].versions[0]._tracks[3]
    track.transitions = [Unreadable()]
    env = move(rig, 3, 0)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "cannot be read" in env["error"] and "no duration" in env["error"]
    assert untouched_world(rig)


def test_transitions_that_cannot_be_read_refuse_it_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(PyTrack):
        @property
        def transitions(self) -> Any:
            raise RuntimeError("no transitions")

        @transitions.setter
        def transitions(self, _value: Any) -> None:
            return None

    broken = Broken(m4_tracks()[3])
    broken.parent = rig.handles["m4"].versions[0]
    rig.handles["m4"].versions[0]._tracks[3] = broken
    env = move(rig, 3, 0)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "cannot be read" in env["error"]
    assert untouched_world(rig)


def test_a_stereo_linked_track_is_refused(rig: Rig) -> None:
    rig.handles["m4"].versions[0]._tracks[3].stereo_linked.set_value(True)
    env = move(rig, 3, 0)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "stereo_linked" in env["error"]
    assert untouched_world(rig)


def give_effect(seg: PySegment) -> None:
    fx = PyTimelineFX("Resize")
    fx.parent = seg
    seg.effects.append(fx)


def test_a_trailing_gap_with_an_effect_is_refused_before_anything_is_copied(rig: Rig) -> None:
    track = rig.handles["m4"].versions[0]._tracks[3]
    assert track.segments[-1].type == GAP
    give_effect(track.segments[-1])
    before = starting_order(rig)
    env = move(rig, 3, 0)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "Gap" in env["error"] and "effect" in env["error"] and "is refused" in env["error"]
    assert order(rig.handles["m4"]) == before and untouched_world(rig)


def test_the_gap_of_a_gap_only_track_with_an_effect_is_refused_before_anything_is_made(
    rig: Rig,
) -> None:
    track = rig.handles["m4"].versions[0]._tracks[2]
    give_effect(track.segments[0])
    before = starting_order(rig)
    env = move(rig, 2, 0)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "Gap" in env["error"] and "effect" in env["error"] and "is refused" in env["error"]
    assert order(rig.handles["m4"]) == before and untouched_world(rig)


def test_a_gap_whose_effects_cannot_be_read_is_refused_too(rig: Rig) -> None:
    class Unreadable(PySegment):
        @property
        def effects(self) -> Any:
            raise RuntimeError("no effects")

        @effects.setter
        def effects(self, _value: Any) -> None:
            return None

    track = rig.handles["m4"].versions[0]._tracks[2]
    broken = Unreadable(
        name="",
        source_name="",
        record_in=0,
        record_out=63,
        source_in=0,
        source_out=63,
        record_duration=64,
        type=GAP,
    )
    track._segments[:] = [broken]
    adopt(track, track._segments)
    env = move(rig, 2, 0)
    assert env["errorCode"] == "PRECONDITION_UNMET"
    assert "cannot be read" in env["error"] and "no effects" in env["error"]
    assert untouched_world(rig)


def test_a_leading_gap_with_an_effect_is_caught_by_the_comparison_not_refused_up_front(
    rig: Rig,
) -> None:
    track = rig.handles["m4"].versions[0]._tracks[0]
    assert [s.type for s in track.segments] == [GAP, VIDEO, GAP, VIDEO]
    give_effect(track.segments[0])  # the copy does not hold the leading Gap (measured)
    before = starting_order(rig)
    env = move(rig, 0, 3)
    assert env["errorCode"] == "FLAME_API_ERROR" and env["result"]["ran"] is True
    assert "source track was NOT deleted" in env["error"]
    assert order(rig.handles["m4"]) == before


def test_a_sequence_in_a_library_is_refused_and_not_touched(rig: Rig) -> None:
    copy = library_copy(rig, "m4")
    before = rows(copy)
    env = move(rig, 3, 0, sequence=wt(copy.get_wiretap_node_id()))
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert rows(copy) == before and checkpoints(rig) == [] and rig.records() == []


def test_a_clip_as_the_target_is_refused(rig: Rig) -> None:
    env = move(rig, 1, 0, sequence=CLIPX)
    assert env["errorCode"] == "SELECTOR_TYPE" and "not a PySequence" in env["error"]
    assert untouched_world(rig)


def test_readonly_denies_the_move_and_nothing_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    assert ro.do(OP, {"sequence": S1, "track": 1, "to": 0})["errorCode"] == "POLICY_DENIED"
    assert untouched_world(ro)


def test_an_answer_that_never_came_is_diverged_when_checked(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    env = move(rig, 3, 0)
    assert env["errorCode"] == "TIMEOUT" and rig.runtime.guard.denial() is not None
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "diverged" and rig.runtime.guard.denial() is None


def test_the_op_is_a_timeline_checkpointed_write(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None and op.checkpoint is not None
    assert op.checkpoint.target_kind == "timeline" and op.checkpoint.batch_arg == "sequence"
    assert (op.effect, op.undo, op.thread, op.category) == (
        "destructive",
        "checkpoint",
        "main",
        "timeline",
    )
    required = [p.name for p in op.params if p.required]
    assert required == ["sequence", "track", "to"]
    for symbol in (
        "PyTrack.copy_to_media_panel",
        "PyVersion.create_track",
        "PySequence.overwrite",
        "PyTrack.transitions",
        "PyTrack.stereo_linked",
        "PySequence.audio_tracks",
        "PyAudioTrack.channels",
        "flame.delete",
    ):
        assert symbol in op.requires


def test_the_description_states_what_was_measured_and_what_the_code_does(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None
    text = " ".join(op.description.split())
    for word in (
        "no native call moves a track",
        "copy_to_media_panel",
        "create_track",
        "1-based",
        "first media segment",
        "flame.delete",
        "by-product",
        "stash reel",
        "journal.revert",
        "SILENT_NOOP",
        "FLAME_API_ERROR",
        "transitions",
        "'Cut' transition of duration 0 at every segment boundary",
        "tracks that hold only those were moved in the measurement",
        "a dissolve etc.",
        "stereo_linked",
        "because the readback leaves those Gaps out",
        "the create_track result itself does not check out",
        "the delete of the source track fails",
        "measured on Flame 2025.2.4, top to bottom",
        "only Gap",
        "result.attributes",
        "No method to move or reorder audio tracks was found on PySequence or PyAudioTrack",
        "The audio tracks are not touched",
        "result.cleanup.track",
        "effect parameters",
        "markers and colours are not compared",
        "audio tracks are read again",
        "result.audio",
        "connections",
        "-1",
    ):
        assert word in text, word
    assert not re.search(r"plan/\d+|訂正\d+|correction \d+|P\d-\d", text)
    assert text.isascii()
    first = opschema.first_sentence(op.description)
    assert first == (
        "Move one video track of a version to another position, by rebuilding it: the track is "
        "copied onto a new track at the target and the original is deleted."
    )
    assert 0 < len(first) <= opschema.SUMMARY_MAX_CHARS and not first.endswith("…")
    assert all(p.description.isascii() for p in op.params)
