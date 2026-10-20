"""Tests the `{kind: 'timeline'}` selector.

It names the sequence the Timeline shows (`flame.timeline.clip`). Run through the real Runtime
with a scripted `transport.exec` over the fake Flame tree (the fake's Timeline is
`fake_media_tree.ACTIVE_TIMELINE`; showing nothing is its `clip = None`, which the real Flame
cannot be put into on demand: measured on Flame 2025.2.4, after the shown sequence was deleted the
Timeline was empty for under 3 s and then showed a sequence again). Whether the real
Flame does the same is what scripts/fix5_timeline_selector_e2e.py measures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import ACTIVE_TIMELINE, PyTimeline
from journal_test_helpers import Rig
from p32b_helpers import PLAN, S1, S2, make_p32b_rig, show, uid
from p33_helpers import add_media_clip, clip_sel, untouched_world
from timeline_p32a_helpers import (
    CHECKPOINT,
    FINGERPRINT,
    TRIM,
    checkpoints,
    fingerprint_of_target,
    rows,
    seg_sel,
    stash_entries,
    stash_library,
)

TL: dict[str, Any] = {"kind": "timeline"}
OVERWRITE = "timeline.overwrite"
INSERT = "timeline.insert"
CUT = "timeline.cut"
CREATE_VERSION = "timeline.create_version"
CREATE_TRACK = "timeline.create_track"
DELETE_SEGMENTS = "timeline.delete_segments"
MOVE_TRACK = "timeline.move_track"
SHORTEN = "timeline.shorten"

ACCEPTING_OPS = [
    "desktop.info",
    CHECKPOINT,
    CREATE_TRACK,
    CREATE_VERSION,
    CUT,
    DELETE_SEGMENTS,
    FINGERPRINT,
    INSERT,
    MOVE_TRACK,
    OVERWRITE,
    SHORTEN,
    "timeline.info",
    "timeline.list_segments",
]


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_p32b_rig(monkeypatch, tmp_path)
    made.handles["c12"] = add_media_clip(made, "len12", start=0, length=12)
    return made


def target_uid(env: dict[str, Any]) -> str:
    value: str = env["result"]["target"]["uid"]
    return value


def test_exactly_the_sequence_and_clip_arguments_accept_timeline(rig: Rig) -> None:
    accepting = sorted(op.name for op in rig.registry.list_ops() if "timeline" in op.accepts)
    assert accepting == sorted(ACCEPTING_OPS)


def sequence_argument_name(op: Any) -> str:
    """The op's own sequence argument: the registry declares it as the checkpoint's `batch_arg`
    for a checkpointed op; every other op that accepts a timeline selector (the reads) has a
    single selector parameter, named `target`."""
    names = [p.name for p in op.params if p.type == "selector"]
    if op.checkpoint is not None:
        assert op.checkpoint.batch_arg in names, op.name
        return str(op.checkpoint.batch_arg)
    assert names == ["target"], f"{op.name}: cannot tell which selector argument is the sequence"
    return "target"


def test_a_timeline_selector_in_any_argument_but_the_sequence_is_invalid_args(rig: Rig) -> None:
    exercised: list[str] = []
    for op in rig.registry.list_ops():
        if "timeline" not in op.accepts:
            continue
        target = sequence_argument_name(op)
        others = [p.name for p in op.params if p.type == "selector" and p.name != target]
        for name in others:
            assert op.precheck is not None, f"{op.name}.{name} accepts timeline with no precheck"
            args = {**op.sample_args, name: TL}
            refusal = op.precheck(args, None)
            assert refusal is not None, f"{op.name}.{name}: a timeline selector got through"
            assert refusal.code == "INVALID_ARGS", (op.name, name, refusal)
            assert name in refusal.message and "timeline" in refusal.message, refusal.message
            exercised.append(f"{op.name}.{name}")
    assert exercised == [f"{OVERWRITE}.source", f"{INSERT}.source"]


@pytest.mark.parametrize(
    "name",
    [
        "desktop.copy",
        "desktop.move",
        "desktop.delete",
        "desktop.list_children",
        "desktop.create",
        "desktop.rename",
        "mediahub.import",
        "timeline.create_sequence",
        "segment.replace",
    ],
)
def test_a_container_or_destination_argument_does_not_accept_timeline(rig: Rig, name: str) -> None:
    op = rig.registry.get(name)
    assert op is not None and "timeline" not in op.accepts


def test_a_timeline_as_the_destination_is_refused_before_flame(rig: Rig) -> None:
    show(rig, "s1")
    env = rig.do("desktop.copy", {"sources": [S2], "destination": TL})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "not accepted here" in env["error"] and rig.exec.calls == []


def test_info_of_the_timeline_is_the_shown_sequence_and_follows_what_is_opened(rig: Rig) -> None:
    show(rig, "s1")
    first = rig.do("timeline.info", {"target": TL})
    assert first["ok"] is True, first
    assert first["result"]["name"] == "S1" and first["result"]["duration"]["frame"] == 64
    show(rig, "s2")
    second = rig.do("timeline.info", {"target": TL})
    assert second["result"]["name"] == "S2" and second["result"]["duration"]["frame"] == 24


def test_fingerprint_of_the_timeline_names_the_shown_sequence_by_uid(rig: Rig) -> None:
    show(rig, "s2")
    env = rig.do(FINGERPRINT, {"target": TL})
    assert env["ok"] is True, env
    assert target_uid(env) == uid(rig.handles["s2"])
    assert env["result"]["fingerprint"] == fingerprint_of_target(rig, S2)
    assert env["result"]["stable"]["id"] == rig.handles["s2"].get_wiretap_node_id()


def test_list_segments_of_the_timeline_lists_the_shown_sequence(rig: Rig) -> None:
    show(rig, "s1")
    env = rig.do("timeline.list_segments", {"target": TL, "version": 0, "track": 1})
    assert env["ok"] is True, env
    assert [i["index"] for i in env["result"]["items"]] == [0]
    assert env["result"]["total"] == 1


def test_desktop_info_of_the_timeline_is_the_shown_sequence(rig: Rig) -> None:
    show(rig, "s2")
    env = rig.do("desktop.info", {"target": TL})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["type"] == "PySequence" and result["name"] == "S2"
    assert result["uid"] == uid(rig.handles["s2"])


def test_checkpoint_of_the_timeline_copies_the_shown_sequence(rig: Rig) -> None:
    show(rig, "s2")
    env = rig.do(CHECKPOINT, {"target": TL})
    assert env["ok"] is True, env
    assert env["result"]["source"]["uid"] == uid(rig.handles["s2"])
    [record] = checkpoints(rig)
    assert record["source"]["uid"] == uid(rig.handles["s2"])
    assert len(stash_entries(rig)) == 1


def test_it_is_resolved_on_every_call_so_a_later_open_changes_the_target(rig: Rig) -> None:
    show(rig, "s1")
    a = rig.do(FINGERPRINT, {"target": TL})
    show(rig, "s3")
    b = rig.do(FINGERPRINT, {"target": TL})
    assert target_uid(a) == uid(rig.handles["s1"])
    assert target_uid(b) == uid(rig.handles["s3"])


def test_nothing_shown_is_selector_empty_for_a_read(rig: Rig) -> None:
    ACTIVE_TIMELINE[0].clip = None
    env = rig.do("timeline.info", {"target": TL})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert "shows nothing" in env["error"]


def test_a_read_that_raises_is_selector_empty_with_the_reason(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: PyTimeline) -> Any:
        raise RuntimeError("timeline gone")

    monkeypatch.setattr(PyTimeline, "clip", property(boom), raising=False)
    env = rig.do("timeline.info", {"target": TL})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert "timeline gone" in env["error"]


def test_nothing_shown_copies_and_journals_nothing_for_a_checkpoint(rig: Rig) -> None:
    ACTIVE_TIMELINE[0].clip = None
    env = rig.do(CHECKPOINT, {"target": TL})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert untouched_world(rig)


def test_nothing_shown_stops_a_checkpointed_edit_before_anything_is_copied(rig: Rig) -> None:
    ACTIVE_TIMELINE[0].clip = None
    env = rig.do(CREATE_VERSION, {"sequence": TL})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert untouched_world(rig)
    assert stash_library(rig) is None


def test_nothing_shown_stops_a_plan_before_anything_runs(rig: Rig) -> None:
    ACTIVE_TIMELINE[0].clip = None
    steps = [
        {
            "op": TRIM,
            "args": {
                "target": seg_sel(TL, 0, record_in=10),
                "edge": "head",
                "offset": 2,
            },
        }
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert env["result"]["ran"] is False
    assert untouched_world(rig)


@pytest.mark.parametrize(
    ("op", "extra"),
    [
        (OVERWRITE, {"at": 30}),
        (INSERT, {"at": 20}),
        (CUT, {"at": 20, "track": 0}),
        (DELETE_SEGMENTS, {"segments": [10], "track": 0}),
        (MOVE_TRACK, {"track": 1, "to": 0}),
        (SHORTEN, {"duration": 40}),
        (CREATE_VERSION, {}),
        (CREATE_TRACK, {}),
    ],
)
def test_a_build_op_edits_the_shown_sequence_and_the_revert_puts_it_back(
    rig: Rig, op: str, extra: dict[str, Any]
) -> None:
    show(rig, "s1")
    args: dict[str, Any] = {"sequence": TL, **extra}
    if op in (OVERWRITE, INSERT):
        args["source"] = clip_sel(rig.handles["c12"])
    before = fingerprint_of_target(rig, S1)
    env = rig.do(op, args)
    assert env["ok"] is True, env
    assert fingerprint_of_target(rig, S1) != before
    [view_record] = [r for r in rig.records() if r["type"] == "intent"]
    assert view_record["target"] == {
        "kind": "wiretap",
        "id": rig.handles["s1"].get_wiretap_node_id(),
    }
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_the_edit_goes_to_the_second_sequence_after_it_is_opened(rig: Rig) -> None:
    show(rig, "s1")
    show(rig, "s2")
    before_s1 = fingerprint_of_target(rig, S1)
    env = rig.do(CREATE_VERSION, {"sequence": TL})
    assert env["ok"] is True, env
    assert len(rig.handles["s2"].versions) == 2 and len(rig.handles["s1"].versions) == 1
    assert fingerprint_of_target(rig, S1) == before_s1


def test_a_timeline_source_is_invalid_args_and_nothing_is_touched(rig: Rig) -> None:
    show(rig, "s1")
    for op in (OVERWRITE, INSERT):
        env = rig.do(op, {"sequence": S1, "source": TL, "at": 0})
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
        assert "source" in env["error"] and "timeline" in env["error"]
    both = rig.do(OVERWRITE, {"sequence": TL, "source": TL, "at": 0})
    assert both["errorCode"] == "INVALID_ARGS"
    assert rig.exec.calls == [] and untouched_world(rig)


def test_a_timeline_source_inside_a_plan_is_refused_before_anything_runs(rig: Rig) -> None:
    show(rig, "s1")
    steps = [{"op": OVERWRITE, "args": {"sequence": S1, "source": TL, "at": 0}}]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert untouched_world(rig)


def trim_args(parent: dict[str, Any], record_in: int = 10, track: int = 0) -> dict[str, Any]:
    return {
        "target": seg_sel(parent, track, record_in=record_in),
        "edge": "head",
        "offset": 2,
    }


def test_a_trim_through_a_timeline_parent_edits_the_shown_sequence_and_reverts(rig: Rig) -> None:
    show(rig, "s1")
    before = fingerprint_of_target(rig, S1)
    env = rig.do(TRIM, trim_args(TL))
    assert env["ok"] is True, env
    assert rows(rig.handles["s1"])[1][1:3] == [12, 33]
    assert fingerprint_of_target(rig, S1) != before
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_a_trim_follows_the_shown_sequence_not_the_one_shown_at_the_first_call(rig: Rig) -> None:
    show(rig, "s1")
    show(rig, "s2")
    before_s1 = fingerprint_of_target(rig, S1)
    before_s2 = fingerprint_of_target(rig, S2)
    env = rig.do(TRIM, trim_args(TL, record_in=0))
    assert env["ok"] is True, env
    assert fingerprint_of_target(rig, S2) != before_s2
    assert fingerprint_of_target(rig, S1) == before_s1


def test_the_segment_selector_still_validates_with_a_timeline_parent(rig: Rig) -> None:
    show(rig, "s1")
    env = rig.do("segment.get_attributes", {"target": seg_sel(TL, 0, index=1), "keys": ["name"]})
    assert env["ok"] is True, env


def test_a_two_step_plan_with_timeline_parents_is_one_revert(rig: Rig) -> None:
    show(rig, "s1")
    before = fingerprint_of_target(rig, S1)
    steps = [
        {"op": TRIM, "args": trim_args(TL, 10)},
        {"op": TRIM, "args": {**trim_args(TL, 40), "edge": "tail"}},
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is True, env
    assert [s["ok"] for s in env["result"]["steps"]] == [True, True]
    assert len(checkpoints(rig)) == 1
    assert fingerprint_of_target(rig, S1) != before
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_a_plan_records_the_uid_of_the_sequence_it_resolved(rig: Rig) -> None:
    show(rig, "s1")
    steps = [
        {"op": TRIM, "args": trim_args(TL, 10)},
        {"op": TRIM, "args": trim_args(TL, 0, track=1)},
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is True, env
    assert env["result"]["timeline"]["target"]["uid"] == uid(rig.handles["s1"])
    [intent] = [r for r in rig.records() if r["type"] == "intent" and r["op"] == PLAN]
    assert intent["target"] == {"kind": "wiretap", "id": rig.handles["s1"].get_wiretap_node_id()}


# The timeline selector is read at each call; a revert follows the recorded uid.


@pytest.mark.parametrize("how", ["create_version", "trim_with_timeline_parent"])
def test_a_revert_after_another_sequence_is_opened_restores_the_edited_one_only(
    rig: Rig, how: str
) -> None:
    show(rig, "s1")
    before_s1 = fingerprint_of_target(rig, S1)
    before_s2 = fingerprint_of_target(rig, S2)
    env = (
        rig.do(CREATE_VERSION, {"sequence": TL})
        if how == "create_version"
        else rig.do(TRIM, trim_args(TL))
    )
    assert env["ok"] is True, env
    assert fingerprint_of_target(rig, S1) != before_s1
    show(rig, "s2")
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before_s1
    assert fingerprint_of_target(rig, S2) == before_s2


# The reads name the sequence they read.


@pytest.mark.parametrize("name", ["timeline.info", "timeline.list_segments"])
def test_the_reads_carry_the_uid_and_wiretap_id_of_the_sequence_they_read(
    rig: Rig, name: str
) -> None:
    show(rig, "s2")
    env = rig.do(name, {"target": TL})
    assert env["ok"] is True, env
    assert env["result"]["uid"] == uid(rig.handles["s2"])
    assert env["result"]["wiretapId"] == rig.handles["s2"].get_wiretap_node_id()
    show(rig, "s1")
    again = rig.do(name, {"target": {"kind": "wiretap", "id": env["result"]["wiretapId"]}})
    assert again["result"]["uid"] == uid(rig.handles["s2"])
