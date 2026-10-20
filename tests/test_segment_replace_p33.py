"""Tests `segment.replace`.

smart_replace_media / smart_replace keep the OFFSET FROM THE START OF THE CLIP (not the source
timecode), a clip too short for the range changes nothing and raises nothing (SILENT_NOOP), the
TimelineFX stay or go with the mode, and a replacement is brought back to a wanted source timecode
with `segment.slip`. Run through the real Runtime over the fake Flame tree
(fake_segment_edit.smart_replace_with follows what was measured on Flame 2025.2.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyTimelineFX
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import (
    REPLACE,
    SLIP,
    a_seg,
    add_media_clip,
    checkpoints,
    clip_sel,
    effect_names,
    seg_a,
    seg_b,
    seg_row,
    sel_at,
    untouched_world,
)
from timeline_p32a_helpers import (
    S1,
    S1_WT,
    S2,
    fingerprint_of_target,
    library_copy,
    make_timeline_rig,
    seg_sel,
    stash_entries,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def replace(
    rig: Rig, selector: dict[str, Any], source: dict[str, Any], mode: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"target": selector, "source": source}
    if mode is not None:
        args["mode"] = mode
    return rig.do(REPLACE, args)


# A = clip1 record 10-34, source 5-29, head 5, tail 5 (so its media is 34 frames from frame 0)


def test_the_range_counted_from_the_clip_start_is_kept_and_the_source_name_changes(
    rig: Rig,
) -> None:
    new = add_media_clip(rig, "newclip", start=0, length=48)
    env = replace(rig, a_seg(), clip_sel(new))
    assert env["ok"] is True, env
    assert seg_row(seg_a(rig)) == ["newclip", 10, 33, 5, 28, 5, 19]
    result = env["result"]
    assert result["mode"] == "media" and result["returned"] is None
    assert result["changed"] == ["sourceName", "tail"]
    assert result["before"]["sourceName"] == "clip1" and result["after"]["sourceName"] == "newclip"
    assert result["after"]["recordIn"] == result["before"]["recordIn"]
    assert result["source"]["name"] == "newclip" and result["source"]["type"] == "PyClip"


def test_the_offset_from_the_clip_start_is_kept_not_the_timecode(rig: Rig) -> None:
    shifted = add_media_clip(rig, "tc_plus_100", start=100, length=48)
    env = replace(rig, a_seg(), clip_sel(shifted))
    assert env["ok"] is True, env
    after = env["result"]["after"]
    assert (after["sourceIn"]["frame"], after["sourceOut"]["frame"]) == (105, 128)
    assert env["result"]["before"]["sourceIn"]["frame"] == 5  # timecode 100 frames later
    assert after["sourceIn"]["timecode"] == "00:00:04+09"  # the timecode comes with the frame


def test_the_effects_stay_in_media_mode_and_are_replaced_in_media_and_fx_mode(rig: Rig) -> None:
    own = PyTimelineFX("Blur")
    new = add_media_clip(rig, "withblur", start=0, length=48, effects=(own,))
    media_only = replace(rig, sel_at(3), clip_sel(new))  # B: Burn-in Letterbox
    assert media_only["ok"] is True, media_only
    assert effect_names(seg_b(rig)) == ["Burn-in Letterbox"]
    assert media_only["result"]["effects"] == {
        "before": ["Burn-in Letterbox"],
        "after": ["Burn-in Letterbox"],
    }
    other = add_media_clip(rig, "withblur2", start=0, length=48, effects=(PyTimelineFX("Blur"),))
    both = replace(rig, sel_at(3), clip_sel(other), "media_and_fx")
    assert both["ok"] is True, both
    assert effect_names(seg_b(rig)) == ["Blur"]
    assert both["result"]["effects"] == {"before": ["Burn-in Letterbox"], "after": ["Blur"]}


def test_a_clip_too_short_for_the_range_is_a_silent_noop_and_nothing_changes(rig: Rig) -> None:
    short = add_media_clip(rig, "short", start=0, length=12)
    before = seg_row(seg_a(rig))
    env = replace(rig, a_seg(), clip_sel(short))
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "too short" in env["error"] and "offset from the start of the clip" in env["error"]
    assert seg_row(seg_a(rig)) == before and env["result"]["returned"] is None
    assert env["result"]["changed"] == []
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.state() == "failed" and env["journal"]["revert"]["op"] == "journal.revert"


def test_the_clip_the_segment_already_has_changes_nothing_too(rig: Rig) -> None:
    same = add_media_clip(rig, "clip1", start=0, length=34)
    env = replace(rig, a_seg(), clip_sel(same))
    assert env["errorCode"] == "SILENT_NOOP" and "already has" in env["error"]


def test_a_replacement_is_lined_up_to_a_source_timecode_with_slip(rig: Rig) -> None:
    """A replacement whose timecode starts 5 frames later is lined up with slip(5).

    The offset rule lands it 5 frames late (measured on Flame 2025.2.4); slip(5) -- the
    difference of the two source timecodes -- puts it right."""
    later = add_media_clip(rig, "tc_plus_5", start=5, length=48)
    env = replace(rig, a_seg(), clip_sel(later))
    assert env["ok"] is True, env
    got = env["result"]["after"]["sourceIn"]["frame"]
    assert got == 10 and env["result"]["before"]["sourceIn"]["frame"] == 5
    slipped = rig.do(SLIP, {"target": env["result"]["position"]["selector"], "offset": got - 5})
    assert slipped["ok"] is True, slipped
    assert seg_row(seg_a(rig)) == ["tc_plus_5", 10, 33, 5, 28, 0, 24]


def test_the_position_names_the_new_source_so_the_next_edit_can_be_safeguarded(rig: Rig) -> None:
    new = add_media_clip(rig, "newclip", start=0, length=48)
    position = replace(rig, a_seg(), clip_sel(new))["result"]["position"]
    assert position["selector"] == {
        "kind": "segment",
        "parent": S1,
        "version": 0,
        "track": 0,
        "record_in": 10,
        "expect_source_name": "newclip",
    }
    assert rig.do("segment.info", {"target": position["selector"]})["ok"] is True


def test_the_source_can_be_a_selection_or_a_clip_in_a_library(rig: Rig) -> None:
    new = add_media_clip(rig, "viaselection", start=0, length=48)
    rig.flame.media_panel.selected_entries = [new]
    env = replace(rig, a_seg(), {"kind": "selection"})
    assert env["ok"] is True and seg_row(seg_a(rig))[0] == "viaselection"
    lib_clip = rig.flame.media_panel.copy(new, rig.handles["libreel"])[0]
    env = replace(rig, sel_at(3), clip_sel(lib_clip))
    assert env["ok"] is True and seg_row(seg_b(rig))[0] == "viaselection"


def test_a_gap_is_refused(rig: Rig) -> None:
    new = add_media_clip(rig, "newclip")
    env = replace(rig, sel_at(0), clip_sel(new))
    assert env["errorCode"] == "PRECONDITION_UNMET" and "Gap" in env["error"]
    assert untouched_world(rig)


def test_a_source_that_is_not_a_clip_is_refused(rig: Rig) -> None:
    env = replace(rig, a_seg(), S2)  # a PySequence
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "PySequence" in env["error"] and "not a PyClip" in env["error"]
    assert untouched_world(rig)


def test_a_source_that_does_not_resolve_is_refused(rig: Rig) -> None:
    env = replace(rig, a_seg(), wt("/no/such/clip"))
    assert env["errorCode"] == "SELECTOR_EMPTY" and untouched_world(rig)


def test_a_segment_of_a_sequence_in_a_library_is_refused(rig: Rig) -> None:
    new = add_media_clip(rig, "newclip")
    copy = library_copy(rig)
    env = replace(rig, seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1), clip_sel(new))
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]
    assert "checkpointError" not in env["result"]  # the op's own check refused, not the checkpoint
    assert checkpoints(rig) == [] and rig.records() == []


def test_a_bad_mode_or_a_segment_selector_as_the_source_is_invalid_args(rig: Rig) -> None:
    new = add_media_clip(rig, "newclip")
    bad_mode = replace(rig, a_seg(), clip_sel(new), "everything")
    assert bad_mode["errorCode"] == "INVALID_ARGS" and "'media'" in bad_mode["error"]
    assert rig.exec.calls == []  # refused by the server: nothing was sent to Flame
    as_segment = replace(rig, a_seg(), sel_at(3))
    assert as_segment["errorCode"] == "INVALID_ARGS" and "source" in as_segment["error"]
    not_a_segment = replace(rig, S1, clip_sel(new))
    assert (
        not_a_segment["errorCode"] == "INVALID_ARGS"
        and "'segment' selector" in not_a_segment["error"]
    )
    assert untouched_world(rig)


def test_a_wrong_expected_source_name_is_a_conflict_before_anything_is_copied(rig: Rig) -> None:
    new = add_media_clip(rig, "newclip")
    wrong = seg_sel(S1, 0, index=1)
    wrong["expect_source_name"] = "clip2"
    assert replace(rig, wrong, clip_sel(new))["errorCode"] == "CONFLICT"
    assert untouched_world(rig)


def test_a_replace_that_moves_the_record_is_reported_with_a_warning(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_media_tree import PySegment

    real = PySegment.smart_replace_media

    def moves(self: PySegment, clip: Any) -> None:
        real(self, clip)
        self._set_record(self.record_in.frame, self.record_out.frame + 2)

    monkeypatch.setattr(PySegment, "smart_replace_media", moves)
    new = add_media_clip(rig, "newclip", start=0, length=48)
    env = replace(rig, a_seg(), clip_sel(new))
    assert env["ok"] is True and "recordOut changed from 33 to 35" in env["result"]["warnings"][0]


def test_the_return_value_is_reported_but_never_used(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_media_tree import PySegment

    real = PySegment.smart_replace_media

    def says_true(self: PySegment, clip: Any) -> bool:
        real(self, clip)
        return True

    monkeypatch.setattr(PySegment, "smart_replace_media", says_true)
    env = replace(rig, a_seg(), clip_sel(add_media_clip(rig, "newclip", length=48)))
    assert env["ok"] is True and env["result"]["returned"] is True


def test_a_replace_is_journaled_behind_a_checkpoint_and_a_revert_puts_the_sequence_back(
    rig: Rig,
) -> None:
    before = fingerprint_of_target(rig, S1)
    new = add_media_clip(rig, "newclip", start=0, length=48)
    env = replace(rig, a_seg(), clip_sel(new), "media_and_fx")
    ref = env["journal"]
    [record] = checkpoints(rig)
    assert record["id"] == ref["checkpoint"] and record["fingerprint"] == before
    [view] = Index(rig.records()).groups[ref["group"]]
    assert view.intent["op"] == REPLACE and view.intent["kind"] == "replace"
    assert view.intent["args"]["mode"] == "media_and_fx" and view.intent["target"] == wt(S1_WT)
    labels = [c.label for c in rig.exec.calls]
    assert labels.count(REPLACE) == 2 and "timeline.checkpoint" in labels  # the check, the write
    out = rig.do("journal.revert", {"group": ref["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before
    assert seg_row(track0_of(rig, out))[0] == "clip1"
    assert stash_entries(rig)


def track0_of(rig: Rig, revert_env: dict[str, Any]) -> Any:
    restored = rig.flame.find_by_wiretap_node_id(revert_env["result"]["restored"]["selector"]["id"])
    return restored.versions[0].tracks[0].segments[1]


def test_segment_replace_is_declared_as_a_timeline_checkpointed_write(rig: Rig) -> None:
    op = rig.registry.get(REPLACE)
    assert op is not None and op.checkpoint is not None
    assert op.checkpoint.target_kind == "timeline"
    assert (op.effect, op.undo, op.thread) == ("reversible", "checkpoint", "main")
    assert [p.name for p in op.params] == ["target", "source", "mode"]
    for word in (
        "keeps the offset from the start of the clip",
        "does not match the source timecode",
        "Invalid source_clip",
        "segment.slip",
        "slip(n) takes source_in down by n",
        "SILENT_NOOP",
        "smart_replace_media",
        "smart_replace",
    ):
        assert word in op.description


def test_readonly_denies_the_replace_and_nothing_is_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    new = add_media_clip(ro, "newclip")
    assert replace(ro, a_seg(), clip_sel(new))["errorCode"] == "POLICY_DENIED"
    assert untouched_world(ro)
