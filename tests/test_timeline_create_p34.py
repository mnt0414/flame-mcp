"""Tests `timeline.create_sequence`.

The arguments checked on the server, what is refused before anything is written, the journaled
create (intent -> compare-and-swap -> outcome with `created`), the display read before and after,
the revert that deletes only a sequence still as made and opens again what the Timeline showed,
`journal.check` of a lost answer. Run through the real Runtime with a scripted `transport.exec`
over the fake Flame tree. The fake follows what was measured on Flame 2025.2.4: create_sequence
reads back the frame rate, size and start TC it was given, rejects an unknown frame rate with an
exception, and allows two sequences with the same name."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import ACTIVE_TIMELINE, PyReel
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import add_media_clip, clip_sel
from timeline_p32a_helpers import (
    REEL_WT,
    S1,
    fingerprint_of_target,
    make_timeline_rig,
    rows,
    uid,
    wt,
)

CREATE = "timeline.create_sequence"
REEL = wt(REEL_WT)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    r = make_timeline_rig(monkeypatch, tmp_path)
    r.handles["c12"] = add_media_clip(r, "len12", start=0, length=12)
    return r


def create(rig: Rig, parent: dict[str, Any] = REEL, **extra: Any) -> dict[str, Any]:
    args = {
        "parent": parent,
        "name": "NEWSEQ",
        "frame_rate": "23.976 fps",
        "width": 1920,
        "height": 1080,
        **extra,
    }
    return rig.do(CREATE, args)


def names(rig: Rig) -> list[str]:
    return [str(c.name.get_value()) for c in rig.handles["reel"].children]


def untouched(rig: Rig) -> bool:
    return rig.records() == [] and "NEWSEQ" not in names(rig)


def shown_name() -> str | None:
    clip = ACTIVE_TIMELINE[0].clip
    return None if clip is None else str(clip.name.get_value())


def test_a_sequence_is_made_in_the_reel_and_read_back(rig: Rig) -> None:
    env = create(rig, start_tc="01:00:00:00", video_tracks=2)
    assert env["ok"] is True, env
    result = env["result"]
    assert names(rig).count("NEWSEQ") == 1
    made = rig.handles["reel"].children[-1]
    assert result["created"]["uid"] == uid(made) and result["created"]["type"] == "PySequence"
    assert result["created"]["selector"] == wt(made.get_wiretap_node_id())
    assert result["sequence"]["frameRate"] == "23.976 fps"
    assert (result["sequence"]["width"], result["sequence"]["height"]) == (1920, 1080)
    assert result["sequence"]["startTime"]["frame"] == 86400
    assert result["sequence"]["videoTracks"] == 2 and result["warnings"] == []
    assert len(made.versions[0].tracks) == 2
    assert result["state"]["name"] == "NEWSEQ" and result["state"]["fingerprint"]
    assert result["returned"]["type"] == "PySequence"
    assert env["execution"]["effect"] == "create" and env["undoSteps"] == 0


def test_the_defaults_are_one_track_from_zero(rig: Rig) -> None:
    env = create(rig)
    assert env["ok"] is True, env
    made = rig.handles["reel"].children[-1]
    assert len(made.versions[0].tracks) == 1
    assert env["result"]["sequence"]["startTime"]["frame"] == 0


def test_the_timeline_shows_the_new_sequence_and_both_readings_are_in_the_result(rig: Rig) -> None:
    rig.handles["s1"].open()
    env = create(rig)
    result = env["result"]
    assert result["shownBefore"]["name"] == "S1" and result["shownAfter"]["name"] == "NEWSEQ"
    assert shown_name() == "NEWSEQ"


def test_the_same_name_can_be_used_twice(rig: Rig) -> None:
    assert create(rig)["ok"] is True
    assert create(rig)["ok"] is True
    assert names(rig).count("NEWSEQ") == 2


def test_the_new_sequence_can_be_filled(rig: Rig) -> None:
    env = create(rig, start_tc="01:00:00:00")
    sel = env["result"]["created"]["selector"]
    out = rig.do(
        "timeline.overwrite", {"sequence": sel, "source": clip_sel(rig.handles["c12"]), "at": 86400}
    )
    assert out["ok"] is True, out
    assert [r[:3] for r in rows(rig.handles["reel"].children[-1])] == [
        ["Video Segment", 86400, 86411]
    ]


def test_journal_holds_an_intent_with_the_reel_and_what_was_shown(rig: Rig) -> None:
    rig.handles["s1"].open()
    env = create(rig)
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.intent["op"] == CREATE and view.intent["kind"] == "desktop_sequence"
    assert view.intent["target"] == REEL and view.intent["shownBefore"]["name"] == "S1"
    assert view.outcome is not None and view.outcome["ok"] is True
    assert view.outcome["created"]["uid"] == env["result"]["created"]["uid"]
    assert env["journal"]["revert"] == {"op": "journal.revert", "args": {"group": view.group}}


def test_a_reel_in_a_library_is_refused(rig: Rig) -> None:
    env = create(rig, wt("/projects/p/ws/P1O/R1"))
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert rig.records() == []
    assert [c.name.get_value() for c in rig.handles["libreel"].children] == [
        "clip1",
        "clip2",
        "clip3",
    ]


def test_a_reel_of_a_batch_group_is_refused(rig: Rig) -> None:
    batch_reel = rig.handles["bg1"].create_reel("Schematic Reel 1")
    env = create(rig, wt(batch_reel.get_wiretap_node_id()))
    assert env["errorCode"] == "INVALID_ARGS" and "reel of a Batch group" in env["error"]
    assert "Desktop" in env["hint"] and rig.records() == []


def test_something_that_is_not_a_reel_is_refused(rig: Rig) -> None:
    env = create(rig, S1)
    assert env["errorCode"] == "SELECTOR_TYPE" and "not a reel" in env["error"]
    assert rig.records() == []


@pytest.mark.parametrize(
    ("extra", "word"),
    [
        ({"frame_rate": "99 fps"}, "frame_rate must be one of"),
        ({"frame_rate": "25fps"}, "frame_rate must be one of"),
        ({"start_tc": "1:00:00:00"}, "HH:MM:SS:FF"),
        ({"start_tc": "01:00:00"}, "HH:MM:SS:FF"),
        ({"start_tc": "01:00:00:24"}, "out of range"),
        ({"start_tc": "24:00:00:00"}, "out of range"),
        ({"start_tc": "00:60:00:00"}, "out of range"),
        ({"video_tracks": 0}, "video_tracks"),
        ({"video_tracks": 9}, "video_tracks"),
        ({"width": 0}, "width"),
        ({"height": 1080.5}, "height"),
        ({"name": ""}, "name"),
    ],
)
def test_arguments_that_flame_would_not_take_are_refused_on_the_server(
    rig: Rig, extra: dict[str, Any], word: str
) -> None:
    env = create(rig, **extra)
    assert env["errorCode"] == "INVALID_ARGS" and word in env["error"]
    assert untouched(rig)


@pytest.fixture
def seen_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The keyword arguments every `PyReel.create_sequence` call of the test received."""
    original = PyReel.create_sequence
    calls: list[dict[str, Any]] = []

    def spy(self: PyReel, **kwargs: Any) -> Any:
        calls.append(dict(kwargs))
        return original(self, **kwargs)

    monkeypatch.setattr(PyReel, "create_sequence", spy)
    return calls


@pytest.mark.parametrize("depth", [8, 10, 12, 16, 32])
def test_bit_depth_is_handed_to_flame_and_read_back(
    rig: Rig, seen_kwargs: list[dict[str, Any]], depth: int
) -> None:
    env = create(rig, bit_depth=depth)
    assert env["ok"] is True, env
    assert seen_kwargs[0]["bit_depth"] == depth
    assert rig.handles["reel"].children[-1].bit_depth == depth
    assert env["result"]["sequence"]["bitDepth"] == depth and env["result"]["warnings"] == []
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.intent["args"]["bit_depth"] == depth


def test_a_whole_number_written_as_a_float_reaches_flame_as_an_integer(
    rig: Rig, seen_kwargs: list[dict[str, Any]]
) -> None:
    env = create(rig, bit_depth=12.0)
    assert env["ok"] is True, env
    assert type(seen_kwargs[0]["bit_depth"]) is int and seen_kwargs[0]["bit_depth"] == 12
    assert rig.handles["reel"].children[-1].bit_depth == 12
    assert env["result"]["sequence"]["bitDepth"] == 12 and env["result"]["warnings"] == []


def test_an_omitted_bit_depth_is_not_sent_so_the_project_default_applies(
    rig: Rig, seen_kwargs: list[dict[str, Any]]
) -> None:
    env = create(rig)
    assert env["ok"] is True, env
    assert "bit_depth" not in seen_kwargs[0]
    assert env["result"]["sequence"]["bitDepth"] == 10 and env["result"]["warnings"] == []


@pytest.mark.parametrize("bad", [0, 7, 11, 24, -8])
def test_an_integer_that_is_not_a_flame_bit_depth_is_refused_on_the_server(
    rig: Rig, seen_kwargs: list[dict[str, Any]], bad: int
) -> None:
    env = create(rig, bit_depth=bad)
    assert env["errorCode"] == "INVALID_ARGS"
    assert "bit_depth: must be one of 8, 10, 12, 16, 32" in env["error"]
    assert untouched(rig) and seen_kwargs == []


@pytest.mark.parametrize("bad", [10.5, "10", "10-bit", True])
def test_a_bit_depth_of_another_type_is_refused_on_the_server(
    rig: Rig, seen_kwargs: list[dict[str, Any]], bad: Any
) -> None:
    env = create(rig, bit_depth=bad)
    assert env["errorCode"] == "INVALID_ARGS" and "bit_depth: must be" in env["error"]
    assert untouched(rig) and seen_kwargs == []


def test_a_bit_depth_that_reads_back_differently_is_a_warning(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PyReel.create_sequence

    def other_depth(self: PyReel, **kwargs: Any) -> Any:
        made = original(self, **kwargs)
        made.bit_depth = 8
        return made

    monkeypatch.setattr(PyReel, "create_sequence", other_depth)
    env = create(rig, bit_depth=16)
    assert env["ok"] is True, env
    assert env["result"]["sequence"]["bitDepth"] == 8
    assert env["result"]["warnings"] == ["read-back differs: bit_depth asked for 16, read back 8"]
    assert env["degraded"] == env["result"]["warnings"]


def test_the_description_says_what_the_bit_depth_decides() -> None:
    from flame_mcp.ops.timeline_create import _DESCRIPTION

    for word in ("bit_depth", "project's default", "Resize"):
        assert word in _DESCRIPTION, word
    assert "cannot be deleted" not in _DESCRIPTION
    assert "On Flame 2026.2.3 Flame refused to delete" in _DESCRIPTION
    assert "on every version" in _DESCRIPTION and "for safety" in _DESCRIPTION


def test_the_frame_numbers_depend_on_the_frame_rate(rig: Rig) -> None:
    assert create(rig, frame_rate="25 fps", start_tc="00:00:00:24")["ok"] is True
    assert create(rig, frame_rate="25 fps", start_tc="00:00:00:25")["errorCode"] == "INVALID_ARGS"
    assert create(rig, frame_rate="60 fps", start_tc="00:00:00:59")["ok"] is True


@pytest.mark.parametrize(
    "rate",
    [
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
    ],
)
def test_all_ten_frame_rates_are_taken(rig: Rig, rate: str) -> None:
    env = create(rig, frame_rate=rate)
    assert env["ok"] is True, env
    assert env["result"]["sequence"]["frameRate"] == rate


def test_a_reel_that_changed_between_the_read_and_the_write_creates_nothing(rig: Rig) -> None:
    def before_run(label: str | None) -> None:
        if label == CREATE and rig.exec.calls[-1].journal_types.count("intent") == 1:
            add_media_clip(rig, "intruder", start=0, length=3)
            rig.exec.before_run = None

    rig.exec.before_run = before_run
    env = create(rig)
    assert env["errorCode"] == "CONFLICT" and "NEWSEQ" not in names(rig)


def test_a_call_after_which_the_reel_did_not_grow_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_media_tree import PySequence, PyTime

    fake = PySequence("ghost", wiretap_id="/nowhere", duration=1, uid="ghost-uid")
    fake.start_time.set_value(PyTime(0))
    monkeypatch.setattr(PyReel, "create_sequence", lambda self, **kw: fake)
    env = create(rig)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "did not grow" in env["error"]
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.outcome is not None and view.outcome["ok"] is False
    assert env["journal"]["revert"] is None


def test_a_revert_deletes_the_sequence_and_opens_what_the_timeline_showed(rig: Rig) -> None:
    rig.handles["s1"].open()
    env = create(rig)
    assert shown_name() == "NEWSEQ"
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert "NEWSEQ" not in names(rig) and shown_name() == "S1"
    result = out["result"]
    assert result["deleted"] is True and result["reshown"] is True
    assert result["shownBefore"]["name"] == "NEWSEQ" and result["shownAfter"]["name"] == "S1"
    assert out["affected"] == 1


def test_a_revert_leaves_an_empty_timeline_empty_when_nothing_was_shown_before(rig: Rig) -> None:
    ACTIVE_TIMELINE[0].clip = None
    env = create(rig)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True and out["result"]["reshown"] is None and shown_name() is None


def test_a_revert_does_not_touch_the_display_when_the_new_sequence_was_not_shown(rig: Rig) -> None:
    env = create(rig)
    rig.handles["s2"].open()
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True and out["result"]["reshown"] is None and shown_name() == "S2"


def test_a_revert_of_a_sequence_that_was_edited_is_a_conflict_and_deletes_nothing(rig: Rig) -> None:
    env = create(rig)
    sel = env["result"]["created"]["selector"]
    edit = rig.do(
        "timeline.overwrite", {"sequence": sel, "source": clip_sel(rig.handles["c12"]), "at": 0}
    )
    assert edit["ok"] is True
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert names(rig).count("NEWSEQ") == 1


def edit_created(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    sel = env["result"]["created"]["selector"]
    edit = rig.do(
        "timeline.overwrite", {"sequence": sel, "source": clip_sel(rig.handles["c12"]), "at": 0}
    )
    assert edit["ok"] is True, edit
    return edit


def test_a_revert_follows_the_sequence_through_the_revert_of_an_edit(rig: Rig) -> None:
    env = create(rig)
    made_uid = env["result"]["created"]["uid"]
    edit = edit_created(rig, env)
    undone = rig.do("journal.revert", {"group": edit["journal"]["group"]})
    assert undone["ok"] is True, undone
    restored_uid = undone["result"]["restored"]["uid"]
    assert restored_uid != made_uid and names(rig).count("NEWSEQ") == 1
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    result = out["result"]
    assert result["deleted"] is True and result["alreadyGone"] is False
    assert result["foundUid"] == restored_uid and result["uidChain"] == [restored_uid, made_uid]
    assert "NEWSEQ" not in names(rig)


def test_a_revert_after_an_edit_that_was_not_reverted_is_a_conflict_and_deletes_nothing(
    rig: Rig,
) -> None:
    env = create(rig)
    edit_created(rig, env)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT" and "fingerprint" in out["error"]
    assert names(rig).count("NEWSEQ") == 1


def test_a_restored_sequence_that_was_edited_again_is_a_conflict(rig: Rig) -> None:
    env = create(rig)
    edit = edit_created(rig, env)
    undone = rig.do("journal.revert", {"group": edit["journal"]["group"]})
    again = rig.do(
        "timeline.overwrite",
        {
            "sequence": undone["result"]["restored"]["selector"],
            "source": clip_sel(rig.handles["c12"]),
            "at": 4,
        },
    )
    assert again["ok"] is True
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert names(rig).count("NEWSEQ") == 1


def test_a_revert_is_alreadygone_only_when_no_uid_of_the_chain_resolves(rig: Rig) -> None:
    env = create(rig)
    edit = edit_created(rig, env)
    undone = rig.do("journal.revert", {"group": edit["journal"]["group"]})
    rig.flame.delete(rig.flame.find_by_uid(undone["result"]["restored"]["uid"]), confirm=False)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True and out["result"]["alreadyGone"] is True
    assert out["result"]["deleted"] is False


def test_the_check_of_a_revert_whose_answer_was_lost_follows_the_chain(rig: Rig) -> None:
    env = create(rig)
    edit = edit_created(rig, env)
    rig.do("journal.revert", {"group": edit["journal"]["group"]})
    rig.exec.fault(CREATE, "TIMEOUT", when="after")
    lost = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert lost["ok"] is False and lost["errorCode"] == "TIMEOUT"
    assert "NEWSEQ" not in names(rig)  # the fake did run the delete
    checked = rig.do("journal.check")["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["landed"]


def test_the_check_of_a_revert_that_never_ran_sees_the_restored_sequence_still_there(
    rig: Rig,
) -> None:
    env = create(rig)
    edit = edit_created(rig, env)
    rig.do("journal.revert", {"group": edit["journal"]["group"]})
    rig.exec.fault(CREATE, "BRIDGE_LOST", when="before")
    lost = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert lost["ok"] is False
    assert names(rig).count("NEWSEQ") == 1
    checked = rig.do("journal.check")["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["not_landed"]


def test_a_revert_of_a_sequence_that_was_renamed_is_a_conflict(rig: Rig) -> None:
    env = create(rig)
    made = rig.handles["reel"].children[-1]
    made.name.set_value("renamed")
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT" and "name" in out["error"]
    assert "renamed" in names(rig)


def test_a_sequence_that_is_gone_already_is_ok_to_revert(rig: Rig) -> None:
    env = create(rig)
    rig.flame.delete(rig.handles["reel"].children[-1], confirm=False)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True and out["result"]["alreadyGone"] is True


def test_revert_the_revert_is_refused(rig: Rig) -> None:
    env = create(rig)
    rig.do("journal.revert", {"group": env["journal"]["group"]})
    again = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert again["ok"] is False


def test_the_fingerprint_of_what_was_made_is_the_one_recorded(rig: Rig) -> None:
    env = create(rig)
    sel = env["result"]["created"]["selector"]
    assert fingerprint_of_target(rig, sel) == env["result"]["state"]["fingerprint"]


def test_an_answer_that_never_came_is_landed_when_checked_and_can_be_reverted(rig: Rig) -> None:
    rig.exec.fault(CREATE, "TIMEOUT", when="after", skip=1)
    env = create(rig)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    assert "journal.check" in env["hint"] and rig.runtime.guard.denial() is not None
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "landed" and rig.runtime.guard.denial() is None
    assert entry["created"]["type"] == "PySequence"
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True and "NEWSEQ" not in names(rig)


def test_a_create_that_never_ran_is_not_landed_when_checked(rig: Rig) -> None:
    rig.exec.fault(CREATE, "BRIDGE_LOST", when="before", skip=1)
    assert create(rig)["ok"] is False
    [entry] = rig.do("journal.check")["result"]["checked"]
    assert entry["verdict"] == "not_landed" and "NEWSEQ" not in names(rig)


def test_it_cannot_be_a_plan_step(rig: Rig) -> None:
    env = rig.do(
        "plan.run",
        {
            "steps": [
                {
                    "op": CREATE,
                    "args": {
                        "parent": REEL,
                        "name": "X",
                        "frame_rate": "25 fps",
                        "width": 10,
                        "height": 10,
                    },
                }
            ]
        },
    )
    assert env["ok"] is False and "cannot be a plan step" in str(env)
    assert "NEWSEQ" not in names(rig) and "X" not in names(rig)


def test_readonly_denies_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    assert create(ro)["errorCode"] == "POLICY_DENIED" and untouched(ro)


def test_it_is_declared_as_a_journaled_create(rig: Rig) -> None:
    op = rig.registry.get(CREATE)
    assert op is not None and op.wrap is not None and op.checkpoint is None
    assert (op.effect, op.undo, op.thread, op.category) == ("create", "value", "main", "timeline")
    assert [p.name for p in op.params] == [
        "parent",
        "name",
        "frame_rate",
        "width",
        "height",
        "start_tc",
        "video_tracks",
        "bit_depth",
    ]
