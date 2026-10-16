"""Tests `segment.copy_to_media_panel` and `segment.match`.

A new clip in a reel out of one segment (the three shapes measured on Flame 2025.2.4: plain
`match` makes the whole source with in/out marks, `match(preserve_handle=True)` and
`copy_to_media_panel` make the used range plus soft handles), found by a diff of the destination,
journaled as a `desktop_extract` write whose revert deletes the clip only while it is as made,
`journal.check` of an answer that was lost, and what plan.run says. Run through the real Runtime
over the fake Flame tree (fake_segment_edit.extracted_clip follows those measurements)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fake_segment_edit
from fake_media_tree import PyBatch, PyClip, PySegment
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import (
    COPY_OUT,
    MATCH,
    REEL_WT,
    a_seg,
    add_media_clip,
    clip_sel,
    seg_a,
    sel_at,
    untouched_world,
)
from timeline_p32a_helpers import (
    S1,
    fingerprint_of_target,
    library_copy,
    make_timeline_rig,
    seg_sel,
    stash_library,
    uid,
    wt,
)

LIB_REEL_WT = "/projects/p/ws/P1O/R1"
DEST = wt(REEL_WT)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def take(
    rig: Rig, op: str, selector: dict[str, Any], dest: dict[str, Any] = DEST, **extra: Any
) -> Any:
    return rig.do(op, {"target": selector, "destination": dest, **extra})


def reel_entries(rig: Rig, reel: str = "reel") -> list[Any]:
    return list(rig.handles[reel].children)


def made_clip(rig: Rig, env: dict[str, Any], reel: str = "reel") -> PyClip:
    found = [c for c in reel_entries(rig, reel) if uid(c) == env["result"]["created"]["uid"]]
    assert len(found) == 1
    clip: PyClip = found[0]
    return clip


def inner(clip: PyClip) -> PySegment:
    seg: PySegment = clip.versions[0].tracks[0].segments[0]
    return seg


def seg_numbers(seg: PySegment) -> dict[str, Any]:
    return {
        "record": (seg.record_in.frame, seg.record_out.frame),
        "source": (seg.source_in.frame, seg.source_out.frame),
        "head": seg.head,
        "tail": seg.tail,
        "effects": [fx.type for fx in seg.effects],
    }


# A = clip1, record 10-33, source 5-28, head 5, tail 5, no effect; B has a Burn-in Letterbox


def test_copy_to_media_panel_makes_the_used_range_with_its_handles_and_effects(rig: Rig) -> None:
    before = len(reel_entries(rig))
    env = take(rig, COPY_OUT, sel_at(3))
    assert env["ok"] is True, env
    assert len(reel_entries(rig)) == before + 1
    clip = made_clip(rig, env)
    assert seg_numbers(inner(clip)) == {
        "record": (40, 63),
        "source": (0, 23),
        "head": 0,
        "tail": 0,
        "effects": ["Burn-in Letterbox"],
    }
    seg = env["result"]["clip"]["segments"][0]
    assert seg["effects"] == ["Burn-in Letterbox"] and env["result"]["clip"]["effects"] == [
        "Burn-in Letterbox"
    ]
    handles = take(rig, COPY_OUT, a_seg())
    inner_a = handles["result"]["clip"]
    assert inner_a["duration"]["frame"] == 24 and inner_a["startFrame"] == 6
    assert inner_a["inMark"] is None and inner_a["outMark"] is None
    row = inner_a["segments"][0]
    assert (row["sourceIn"]["frame"], row["sourceOut"]["frame"]) == (5, 28)
    assert (row["head"], row["tail"]) == (5, 5) and row["recordIn"]["frame"] == 10


def test_match_by_default_makes_the_whole_source_with_marks_and_no_effect(rig: Rig) -> None:
    env = take(rig, MATCH, a_seg())
    assert env["ok"] is True, env
    clip = env["result"]["clip"]
    assert (clip["duration"]["frame"], clip["startFrame"]) == (24, 1)
    assert (clip["inMark"]["frame"], clip["outMark"]["frame"]) == (5, 29)
    row = clip["segments"][0]
    assert (row["sourceIn"]["frame"], row["sourceOut"]["frame"]) == (0, 33)
    assert (row["head"], row["tail"]) == (0, 0) and clip["effects"] == []
    assert env["result"]["mode"] == "match" and env["result"]["preserveHandle"] is False


def test_match_with_preserve_handle_keeps_the_used_range_and_its_handles_without_effects(
    rig: Rig,
) -> None:
    env = take(rig, MATCH, sel_at(3), preserve_handle=True)
    assert env["ok"] is True
    clip = env["result"]["clip"]
    assert clip["effects"] == [] and clip["inMark"] is None
    plain = take(rig, MATCH, a_seg(), preserve_handle=True)["result"]["clip"]
    assert (plain["duration"]["frame"], plain["startFrame"]) == (24, 6)
    row = plain["segments"][0]
    assert (row["head"], row["tail"]) == (5, 5)
    assert (row["sourceIn"]["frame"], row["sourceOut"]["frame"]) == (5, 28)
    assert (row["recordIn"]["frame"], row["recordOut"]["frame"]) == (5, 28)


def test_match_with_include_timeline_fx_keeps_the_effects_in_the_odd_shape(rig: Rig) -> None:
    env = take(rig, MATCH, sel_at(3), include_timeline_fx=True)
    assert env["ok"] is True
    clip = env["result"]["clip"]
    assert clip["effects"] == ["Burn-in Letterbox"]
    assert clip["segments"][0]["tail"] == "infinite"
    assert env["result"]["includeTimelineFx"] is True


def test_no_handles_make_a_handle_zero_clip_by_every_route_without_an_error(rig: Rig) -> None:
    routes: list[tuple[str, dict[str, Any]]] = [
        (COPY_OUT, {}),
        (MATCH, {}),
        (MATCH, {"preserve_handle": True}),
    ]
    for op, extra in routes:
        env = take(rig, op, sel_at(3), **extra)
        assert env["ok"] is True, env
        row = env["result"]["clip"]["segments"][0]
        assert (row["head"], row["tail"]) == (0, 0)


def test_the_answer_may_be_a_list_or_the_clip_itself(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    as_list = take(rig, MATCH, a_seg())
    assert as_list["result"]["returned"] == {"shape": "list", "count": 1, "types": ["PyClip"]}
    monkeypatch.setattr(fake_segment_edit, "MATCH_ANSWERS_LIST", False)
    single = take(rig, MATCH, a_seg())
    assert single["ok"] is True
    assert single["result"]["returned"] == {"shape": "single", "count": 1, "types": ["PyClip"]}
    copy = take(rig, COPY_OUT, a_seg())
    assert copy["result"]["returned"]["shape"] == "single"


def test_a_new_clip_is_found_by_the_diff_even_when_the_answer_says_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = PySegment.copy_to_media_panel

    def silent(self: PySegment, destination: Any) -> None:
        real(self, destination)

    monkeypatch.setattr(PySegment, "copy_to_media_panel", silent)
    env = take(rig, COPY_OUT, a_seg())
    assert env["ok"] is True and env["result"]["returned"]["shape"] == "none"
    assert made_clip(rig, env)


def test_the_source_name_the_destination_and_the_state_are_reported(rig: Rig) -> None:
    env = take(rig, COPY_OUT, a_seg())
    result = env["result"]
    assert (
        result["source"]["sequence"]["name"] == "S1" and result["source"]["location"] == "desktop"
    )
    assert result["source"]["segment"]["sourceName"] == "clip1"
    assert result["destination"]["name"] == "Reel 1"
    created = result["created"]
    assert created["type"] == "PyClip" and created["selector"]["kind"] == "wiretap"
    assert result["state"]["name"] == "A" and result["state"]["duration"] == 24
    assert len(result["state"]["fingerprint"]) == 64


def test_a_library_reel_is_a_destination_and_a_library_segment_is_a_source(rig: Rig) -> None:
    into_library = take(rig, COPY_OUT, a_seg(), wt(LIB_REEL_WT))
    assert into_library["ok"] is True, into_library
    assert made_clip(rig, into_library, "libreel")
    copy = library_copy(rig)
    from_library = take(rig, MATCH, seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1))
    assert from_library["ok"] is True, from_library
    assert from_library["result"]["source"]["location"] == "library"


def test_a_gap_a_batch_reel_and_a_destination_that_is_not_a_container_are_refused(
    rig: Rig,
) -> None:
    gap = take(rig, COPY_OUT, sel_at(0))
    assert gap["errorCode"] == "PRECONDITION_UNMET" and "Gap" in gap["error"]
    batch: PyBatch = rig.handles["bg1"]
    batch_reel = batch.create_reel("Schematic Reel X")
    assert batch_reel is not None
    refused = take(rig, MATCH, a_seg(), wt(batch_reel.get_wiretap_node_id()))
    assert refused["errorCode"] == "INVALID_ARGS" and "batch.add_clips" in refused["hint"]
    not_container = take(rig, MATCH, a_seg(), S1)
    assert not_container["errorCode"] == "SELECTOR_TYPE"
    assert [r for r in rig.records() if r["type"] == "intent"] == []
    assert len(batch_reel.children) == 0
    assert stash_library(rig) is None and rig.flame.media_panel.copy_calls == []


def test_a_target_that_is_not_a_segment_and_a_destination_that_is_one_are_invalid_args(
    rig: Rig,
) -> None:
    not_segment = take(rig, COPY_OUT, S1)
    assert (
        not_segment["errorCode"] == "INVALID_ARGS" and "'segment' selector" in not_segment["error"]
    )
    as_segment = take(rig, COPY_OUT, a_seg(), a_seg())
    assert as_segment["errorCode"] == "INVALID_ARGS" and "destination" in as_segment["error"]
    assert untouched_world(rig)


def test_a_segment_that_is_not_there_is_refused_and_nothing_is_journaled(rig: Rig) -> None:
    env = take(rig, COPY_OUT, sel_at(9))
    assert env["errorCode"] == "SELECTOR_EMPTY" and rig.records() == []


def test_a_call_after_which_the_destination_did_not_grow_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySegment, "copy_to_media_panel", lambda self, destination: None)
    env = take(rig, COPY_OUT, a_seg())
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "did not grow" in env["error"]
    [intent] = [r for r in rig.records() if r["type"] == "intent"]
    [outcome] = [r for r in rig.records() if r["type"] == "outcome"]
    assert intent["kind"] == "desktop_extract" and outcome["ok"] is False
    assert outcome["created"] is None and env["journal"]["revert"] is None


def test_two_new_entries_that_cannot_be_told_apart_record_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = PySegment.copy_to_media_panel

    def two(self: PySegment, destination: Any) -> None:
        real(self, destination)
        real(self, destination)

    monkeypatch.setattr(PySegment, "copy_to_media_panel", two)
    env = take(rig, COPY_OUT, a_seg())
    assert env["errorCode"] == "FLAME_API_ERROR" and len(env["result"]["candidates"]) == 2
    assert env["journal"]["revert"] is None


def test_the_write_is_journaled_as_an_extract_with_the_clip_that_was_made(rig: Rig) -> None:
    env = take(rig, MATCH, a_seg())
    ref = env["journal"]
    assert ref["revert"] == {"op": "journal.revert", "args": {"group": ref["group"]}}
    [view] = Index(rig.records()).groups[ref["group"]]
    assert view.intent["kind"] == "desktop_extract" and view.intent["op"] == MATCH
    assert view.intent["target"] == DEST and view.intent["args"]["destination"] == DEST
    assert view.intent["source"]["sequence"]["name"] == "S1"
    created = view.created_info()
    assert created is not None and created["uid"] == env["result"]["created"]["uid"]
    assert created["parent"]["name"] == "Reel 1" and created["sourceUid"] == uid(rig.handles["s1"])
    assert created["state"]["name"] == "A"
    assert env["execution"]["undo"] == "value" and env["undoSteps"] == 0
    assert any("journal.revert" in n for n in env["result"]["notes"])


def test_the_sequence_is_not_touched(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    for op in (COPY_OUT, MATCH):
        take(rig, op, sel_at(3))
    assert fingerprint_of_target(rig, S1) == before and stash_library(rig) is None


def test_a_revert_deletes_the_clip_the_call_made(rig: Rig) -> None:
    before = [uid(c) for c in reel_entries(rig)]
    env = take(rig, COPY_OUT, sel_at(3))
    assert len(reel_entries(rig)) == len(before) + 1
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert [uid(c) for c in reel_entries(rig)] == before
    assert out["result"]["deleted"] is True and out["result"]["notes"]
    again = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert again["ok"] is False


def test_a_clip_that_was_renamed_is_a_conflict_and_nothing_is_deleted(rig: Rig) -> None:
    env = take(rig, COPY_OUT, a_seg())
    clip = made_clip(rig, env)
    clip.name.set_value("renamed by hand")
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"
    assert any("name is 'renamed by hand'" in c for c in out["result"]["conflicts"])
    assert clip in reel_entries(rig)


def test_a_clip_whose_content_was_edited_is_a_conflict(rig: Rig) -> None:
    env = take(rig, COPY_OUT, a_seg())
    clip = made_clip(rig, env)
    trimmed = rig.do(
        "segment.trim",
        {
            "target": seg_sel(wt(clip.get_wiretap_node_id()), 0, index=0),
            "edge": "tail",
            "offset": 2,
        },
    )
    assert trimmed["ok"] is True, trimmed
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["errorCode"] == "CONFLICT" and "fingerprint" in str(out["result"]["conflicts"])


def test_a_clip_that_is_gone_already_is_ok(rig: Rig) -> None:
    env = take(rig, COPY_OUT, a_seg())
    rig.flame.delete(made_clip(rig, env), confirm=False)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True and out["result"]["alreadyGone"] is True


def test_the_revert_is_journaled_as_a_group_of_its_own(rig: Rig) -> None:
    env = take(rig, MATCH, a_seg())
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    index = Index(rig.records())
    [view] = index.groups[out["journal"]["group"]]
    assert view.intent["kind"] == "desktop_revert" and view.intent["revertOf"] == "desktop_extract"
    assert view.intent["reverts"] == env["journal"]["group"]


def test_a_lost_answer_is_decided_by_journal_check_from_what_the_destination_holds(
    rig: Rig,
) -> None:
    rig.exec.fault(COPY_OUT, "TIMEOUT", when="after", skip=1)  # the pre-read passes
    env = take(rig, COPY_OUT, a_seg())
    assert env["errorCode"] == "TIMEOUT" and rig.runtime.guard.denial() is not None
    assert len(reel_entries(rig)) > 0
    checked = rig.do("journal.check")["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["landed"]
    assert checked[0]["created"]["name"] == "A" and rig.runtime.guard.denial() is None


def test_a_lost_answer_of_a_call_that_never_ran_is_not_landed(rig: Rig) -> None:
    rig.exec.fault(MATCH, "BRIDGE_LOST", when="before", skip=1)
    before = len(reel_entries(rig))
    env = take(rig, MATCH, a_seg())
    assert env["ok"] is False and len(reel_entries(rig)) == before
    checked = rig.do("journal.check")["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["not_landed"]


def test_a_revert_that_was_lost_is_decided_by_whether_the_clip_is_still_there(rig: Rig) -> None:
    env = take(rig, COPY_OUT, a_seg())
    rig.exec.fault(COPY_OUT, "TIMEOUT", when="after")
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "TIMEOUT"
    checked = rig.do("journal.check")["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["landed"]


@pytest.mark.parametrize("op", [COPY_OUT, MATCH])
def test_plan_run_refuses_a_step_that_makes_a_clip(rig: Rig, op: str) -> None:
    env = rig.do(
        "plan.run",
        {"steps": [{"op": op, "args": {"target": a_seg(), "destination": DEST}}]},
    )
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "cannot be a plan step" in str(env["result"]) and untouched_world(rig)


def test_a_bundled_plan_may_not_hold_a_match_either(rig: Rig) -> None:
    env = rig.do(
        "plan.run",
        {
            "steps": [
                {"op": "segment.slip", "args": {"target": a_seg(), "offset": 1}},
                {"op": MATCH, "args": {"target": a_seg(), "destination": DEST}},
            ]
        },
    )
    assert env["ok"] is False and untouched_world(rig)


def test_the_two_ops_are_declared_as_wrapped_creates_and_say_how_they_differ(rig: Rig) -> None:
    for name in (COPY_OUT, MATCH):
        op = rig.registry.get(name)
        assert op is not None and op.wrap is not None and op.checkpoint is None
        assert (op.effect, op.undo, op.thread) == ("create", "value", "main")
        for word in ("soft handles", "inMark", "preserve_handle", "include_timeline_fx", "revert"):
            assert word in op.description, (name, word)
    match = rig.registry.get(MATCH)
    assert match is not None
    assert [p.name for p in match.params] == [
        "target",
        "destination",
        "preserve_handle",
        "include_timeline_fx",
    ]


def test_readonly_denies_both(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    for op in (COPY_OUT, MATCH):
        assert take(ro, op, a_seg())["errorCode"] == "POLICY_DENIED"
    assert untouched_world(ro)


def test_a_clip_is_not_a_destination(rig: Rig) -> None:
    clip = rig.handles["clipx"]
    env = take(rig, COPY_OUT, a_seg(), clip_sel(clip))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE"
    assert seg_a(rig).source_name == "clip1"


def test_a_selection_parent_is_pinned_to_the_sequence_the_pre_read_saw(rig: Rig) -> None:
    rig.flame.media_panel.selected_entries = [rig.handles["s1"]]
    seen = {"calls": 0}

    def switch(label: str | None) -> None:
        if label == MATCH:
            seen["calls"] += 1
            if seen["calls"] == 2:  # the write: the selection has moved on since the pre-read
                rig.flame.media_panel.selected_entries = [rig.handles["s2"]]

    rig.exec.before_run = switch
    env = take(rig, MATCH, seg_sel({"kind": "selection"}, 0, index=1))
    assert env["ok"] is True, env  # S2 has no segment 1: the unpinned write would not resolve
    assert env["result"]["source"]["sequence"]["name"] == "S1"
    assert env["result"]["source"]["segment"]["sourceName"] == "clip1"
    write = [c.code for c in rig.exec.calls if c.label == MATCH][-1]
    assert repr(REEL_WT + "/S1") in write.split("__op_src = ")[1]


def test_a_destination_that_changed_between_the_pre_read_and_the_write_is_a_conflict(
    rig: Rig,
) -> None:
    seen = {"calls": 0}

    def intruder(label: str | None) -> None:
        if label == COPY_OUT:
            seen["calls"] += 1
            if seen["calls"] == 2:  # the write: somebody put a clip into the reel after the read
                add_media_clip(rig, "late_arrival")

    rig.exec.before_run = intruder
    before = len(reel_entries(rig))
    env = take(rig, COPY_OUT, a_seg())
    assert env["ok"] is False and env["errorCode"] == "CONFLICT"
    assert "changed between the read and the write" in env["error"]
    assert len(reel_entries(rig)) == before + 1  # only the intruder: the call made nothing
    assert env["journal"]["revert"] is None


def test_a_segment_can_be_addressed_by_record_in(rig: Rig) -> None:
    env = take(rig, MATCH, seg_sel(S1, 0, record_in=40))
    assert env["ok"] is True, env
    assert env["result"]["source"]["segment"]["sourceName"] == "clip2"
    assert env["result"]["source"]["segment"]["recordIn"]["frame"] == 40
    wrong = seg_sel(S1, 0, record_in=40)
    wrong["expect_source_name"] = "clip1"
    assert take(rig, MATCH, wrong)["errorCode"] == "CONFLICT"
