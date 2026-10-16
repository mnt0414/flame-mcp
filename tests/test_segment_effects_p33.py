"""Tests the effect ops of a segment.

`segment.create_effect`, `delete_effect`, `set_effect`, `effect_load_setup` (each behind a
Timeline checkpoint) and `effect_save_setup` (a file, no checkpoint). Run through the real Runtime
over the fake Flame tree; the fake follows what was measured on Flame 2025.2.4 (fake_segment_edit:
the two create_effect exceptions, flame.delete(fx), setup files with a per-type extension,
load_setup of a path that is not there)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import flame_mcp.ops.segment_effects as effects_ops
from fake_media_tree import PySegment, PyTimelineFX
from fake_segment_edit import ALREADY_THERE
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import (
    CREATE_FX,
    DELETE_FX,
    LETTERBOX,
    LOAD_FX,
    SAVE_FX,
    SET_FX,
    SLIP,
    a_seg,
    checkpoints,
    effect_names,
    seg_a,
    seg_b,
    sel_at,
    track0,
    untouched_world,
)
from timeline_p32a_helpers import (
    S1,
    fingerprint_of_target,
    library_copy,
    make_timeline_rig,
    seg_sel,
    wt,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def create(rig: Rig, fx_type: str, selector: dict[str, Any] | None = None) -> dict[str, Any]:
    return rig.do(CREATE_FX, {"target": selector or a_seg(), "type": fx_type})


def set_fx(
    rig: Rig,
    values: dict[str, Any],
    fx_type: str = LETTERBOX,
    selector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return rig.do(SET_FX, {"target": selector or a_seg(), "type": fx_type, "values": values})


def fx_of(seg: PySegment, fx_type: str) -> PyTimelineFX:
    return next(fx for fx in seg.effects if fx.type == fx_type)


def attrs(fx: PyTimelineFX) -> dict[str, Any]:
    return {k: getattr(fx, k).get_value() for k in fx.attributes}


def test_create_effect_puts_a_new_effect_on_the_segment_and_reads_it_back(rig: Rig) -> None:
    env = create(rig, "Blur")
    assert env["ok"] is True, env
    assert effect_names(seg_a(rig)) == ["Blur"]
    result = env["result"]
    assert result["type"] == "Blur" and result["requestedType"] == "Blur"
    assert result["effects"]["before"] == [] and result["created"]["type"] == "Blur"
    assert result["effects"]["after"] == [result["created"]]
    assert result["created"]["attributes"] == {"bypass": False}
    assert result["returned"] == {"type": "PyTimelineFX", "effectType": "Blur"}
    assert result["position"]["selector"]["record_in"] == 10


def test_a_type_that_differs_only_in_case_is_taken_as_the_listed_spelling(rig: Rig) -> None:
    env = create(rig, "burn-in letterbox")
    assert env["ok"] is True and effect_names(seg_a(rig)) == [LETTERBOX]
    assert env["result"]["requestedType"] == "burn-in letterbox"
    assert env["result"]["type"] == LETTERBOX
    created = env["result"]["created"]
    assert set(created["attributes"]) == {"bypass", "mode", "ratio", "colour"}


def test_an_unknown_type_is_invalid_args_with_the_types_to_choose_from(rig: Rig) -> None:
    env = create(rig, "Blu")
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "availableTypes" in env["error"]
    assert "Blur" in env["result"]["availableTypes"] and "Blur" in env["result"]["similar"]
    assert len(env["result"]["availableTypes"]) == len(seg_a(rig).effect_types)
    assert untouched_world(rig) and effect_names(seg_a(rig)) == []


def test_a_type_that_is_already_on_the_segment_is_refused_before_the_checkpoint(rig: Rig) -> None:
    env = create(rig, LETTERBOX, sel_at(3))  # B has one
    assert env["errorCode"] == "PRECONDITION_UNMET" and "already has" in env["error"]
    assert env["result"]["effectTypes"] == [LETTERBOX] and ALREADY_THERE in env["error"]
    assert untouched_world(rig)


def test_a_gap_and_a_library_sequence_are_refused(rig: Rig) -> None:
    gap = create(rig, "Blur", sel_at(0))
    assert gap["errorCode"] == "PRECONDITION_UNMET" and "Gap" in gap["error"]
    copy = library_copy(rig)
    lib = create(rig, "Blur", seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1))
    assert lib["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in lib["error"]
    assert "checkpointError" not in lib["result"]  # the op's own check refused, not the checkpoint
    assert checkpoints(rig) == [] and rig.records() == []


def test_the_effects_are_not_listed_in_creation_order_and_both_are_reported(rig: Rig) -> None:
    create(rig, "Blur")
    env = create(rig, "Resize")
    assert env["ok"] is True
    after = [row["type"] for row in env["result"]["effects"]["after"]]
    assert sorted(after) == ["Blur", "Resize"] and after == ["Resize", "Blur"]
    assert env["result"]["created"]["type"] == "Resize"


def test_an_effect_that_did_not_arrive_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PySegment, "create_effect", lambda self, fx_type: PyTimelineFX(fx_type))
    env = create(rig, "Blur")
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert "the segment now has []" in env["error"]
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_an_exception_of_flame_is_a_flame_api_error_with_the_checkpoint(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: PySegment, fx_type: str) -> PyTimelineFX:
        raise RuntimeError("An effect of this type already exists.")

    monkeypatch.setattr(PySegment, "create_effect", boom)
    env = create(rig, "Blur")
    assert env["errorCode"] == "FLAME_API_ERROR" and "already exists" in env["error"]
    assert env["journal"]["checkpoint"] in env["hint"]


def test_a_revert_takes_the_effect_off_again(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = create(rig, "Blur")
    assert fingerprint_of_target(rig, S1) != before
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_create_effect_is_declared_as_a_timeline_checkpointed_write(rig: Rig) -> None:
    op = rig.registry.get(CREATE_FX)
    assert op is not None and op.checkpoint is not None and op.checkpoint.target_kind == "timeline"
    assert (op.effect, op.undo, op.accepts) == ("create", "checkpoint", ("segment",))
    assert [p.name for p in op.params] == ["target", "type"]
    assert "segment.effect_load_setup" in op.description


def test_delete_effect_takes_the_effect_off_and_reads_the_effects_back(rig: Rig) -> None:
    env = rig.do(DELETE_FX, {"target": sel_at(3), "type": LETTERBOX})
    assert env["ok"] is True, env
    assert effect_names(seg_b(rig)) == []
    result = env["result"]
    assert result["effects"]["after"] == [] and result["returned"] is True
    assert [row["type"] for row in result["effects"]["before"]] == [LETTERBOX]
    assert result["effects"]["before"][0]["attributes"]["ratio"] == 1.5


def test_deleting_a_type_the_segment_has_not_is_refused_before_the_checkpoint(rig: Rig) -> None:
    env = rig.do(DELETE_FX, {"target": sel_at(3), "type": "Blur"})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "no effect of type 'Blur'" in env["error"]
    assert env["result"]["effectTypes"] == [LETTERBOX] and untouched_world(rig)


def test_the_type_is_matched_ignoring_case(rig: Rig) -> None:
    env = rig.do(DELETE_FX, {"target": sel_at(3), "type": "burn-in letterbox"})
    assert env["ok"] is True and env["result"]["type"] == LETTERBOX


def test_an_effect_that_is_still_there_after_the_delete_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(type(rig.flame), "delete", lambda self, obj, confirm=True: True)
    env = rig.do(DELETE_FX, {"target": sel_at(3), "type": LETTERBOX})
    assert env["errorCode"] == "SILENT_NOOP" and "still has an effect" in env["error"]


def test_a_revert_brings_the_effect_back_with_its_settings(rig: Rig) -> None:
    before = fingerprint_of_target(rig, S1)
    env = rig.do(DELETE_FX, {"target": sel_at(3), "type": LETTERBOX})
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    restored = rig.flame.find_by_wiretap_node_id(out["result"]["restored"]["selector"]["id"])
    fx = restored.versions[0].tracks[0].segments[3].effects[0]
    assert fx.type == LETTERBOX and attrs(fx)["ratio"] == 1.5
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def auto_resize(rig: Rig, sequence_depth: int | None, source_depth: int | None) -> PySegment:
    """S1's segment `a_seg()` addresses, with a Resize and the bit depths Flame would have
    compared (None: the depth is not known, as when Flame does not answer it)."""
    seg = seg_a(rig)
    resize = PyTimelineFX("Resize")
    resize.parent = seg
    seg.effects.append(resize)
    rig.handles["s1"].bit_depth = sequence_depth
    if source_depth is not None:
        seg.source_bit_depth = source_depth
    return seg


def test_a_resize_flame_added_because_of_the_bit_depth_is_refused_before_the_checkpoint(
    rig: Rig,
) -> None:
    seg = auto_resize(rig, sequence_depth=8, source_depth=10)
    env = rig.do(DELETE_FX, {"target": a_seg(), "type": "Resize"})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "treated as the one Flame adds by itself" in env["error"]
    assert "Flame added this Resize" not in env["error"]
    assert "8-bit sequence" in env["error"] and "10-bit" in env["error"]
    assert "cannot be deleted" in env["error"]
    assert "timeline.create_sequence" in env["hint"] and "bit_depth" in env["hint"]
    assert env["result"]["ran"] is False and env["result"]["sequenceBitDepth"] == 8
    assert env["result"]["sourceBitDepth"] == 10 and "refusalHint" not in env["result"]
    assert untouched_world(rig) and effect_names(seg) == ["Resize"]


def test_the_type_is_matched_ignoring_case_for_the_bit_depth_refusal_too(rig: Rig) -> None:
    auto_resize(rig, sequence_depth=10, source_depth=8)
    env = rig.do(DELETE_FX, {"target": a_seg(), "type": "resize"})
    assert env["errorCode"] == "PRECONDITION_UNMET" and untouched_world(rig)


def test_a_resize_on_a_sequence_of_the_same_bit_depth_is_deleted(rig: Rig) -> None:
    seg = auto_resize(rig, sequence_depth=10, source_depth=10)
    env = rig.do(DELETE_FX, {"target": a_seg(), "type": "Resize"})
    assert env["ok"] is True, env
    assert effect_names(seg) == []


def test_the_resize_is_still_refused_after_the_segment_was_slipped(rig: Rig) -> None:
    seg = auto_resize(rig, sequence_depth=8, source_depth=10)
    assert rig.do(SLIP, {"target": a_seg(), "offset": 1})["ok"] is True
    before = len(checkpoints(rig))
    env = rig.do(DELETE_FX, {"target": a_seg(), "type": "Resize"})
    assert env["errorCode"] == "PRECONDITION_UNMET", env
    assert "treated as the one Flame adds" in env["error"] and env["result"]["sourceBitDepth"] == 10
    assert len(checkpoints(rig)) == before and effect_names(seg) == ["Resize"]


def test_the_resize_is_still_refused_on_both_pieces_of_a_cut_segment(rig: Rig) -> None:
    auto_resize(rig, sequence_depth=8, source_depth=10)
    assert rig.do("timeline.cut", {"sequence": S1, "at": 20, "track": 0})["ok"] is True
    for index in (1, 2):
        piece = track0(rig)[index]
        assert effect_names(piece) == ["Resize"] and piece.source_bit_depth == 10
        env = rig.do(DELETE_FX, {"target": sel_at(index), "type": "Resize"})
        assert env["errorCode"] == "PRECONDITION_UNMET", (index, env)
        assert effect_names(track0(rig)[index]) == ["Resize"]


@pytest.mark.parametrize(("sequence_depth", "source_depth"), [(None, 10), (8, None), (None, None)])
def test_when_a_bit_depth_cannot_be_read_the_delete_is_not_refused(
    rig: Rig, sequence_depth: int | None, source_depth: int | None
) -> None:
    seg = auto_resize(rig, sequence_depth, source_depth)
    env = rig.do(DELETE_FX, {"target": a_seg(), "type": "Resize"})
    assert env["ok"] is True, env
    assert effect_names(seg) == []


def test_only_a_resize_is_judged_by_the_bit_depth(rig: Rig) -> None:
    rig.handles["s1"].bit_depth = 8
    seg_b(rig).source_bit_depth = 10
    env = rig.do(DELETE_FX, {"target": sel_at(3), "type": LETTERBOX})
    assert env["ok"] is True, env
    assert effect_names(seg_b(rig)) == []


@pytest.mark.parametrize("name", ["segment.list_effects", "segment.info", "timeline.list_segments"])
def test_ops_that_return_effects_say_a_resize_may_be_flames_own(rig: Rig, name: str) -> None:
    op = rig.registry.get(name)
    assert op is not None
    assert "Resize" in op.description and "added by Flame itself" in op.description
    assert "bit depth" in op.description and "segment.delete_effect" in op.description


def test_delete_effect_says_it_refuses_the_resize_flame_added(rig: Rig) -> None:
    op = rig.registry.get(DELETE_FX)
    assert op is not None
    assert "sequenceBitDepth" in op.description and "sourceBitDepth" in op.description
    assert "treated as the one Flame adds" in op.description
    assert "PRECONDITION_UNMET" in op.description
    assert "timeline.create_sequence" in op.description
    flat = " ".join(op.description.split())
    assert "Inside plan.run it stops that step" in flat and "can be reverted" in flat
    assert "On Flame 2026.2.3, Flame refused to delete that Resize" in flat
    assert "on every version" in flat and "for safety" in flat


def test_replace_says_an_automatic_resize_is_kept_by_media_and_fx(rig: Rig) -> None:
    op = rig.registry.get("segment.replace")
    assert op is not None
    assert "Resize" in op.description and "stays" in op.description


def test_delete_effect_is_declared_destructive_and_checkpointed(rig: Rig) -> None:
    op = rig.registry.get(DELETE_FX)
    assert op is not None and op.checkpoint is not None
    assert (op.effect, op.undo) == ("destructive", "checkpoint")


def test_set_effect_writes_mode_ratio_and_colour_and_reads_them_back(rig: Rig) -> None:
    create(rig, LETTERBOX)
    env = set_fx(rig, {"mode": "Custom", "ratio": 1.5, "colour": [1.0, 0.0, 0.0]})
    assert env["ok"] is True, env
    fx = fx_of(seg_a(rig), LETTERBOX)
    assert attrs(fx)["mode"] == "Custom" and attrs(fx)["ratio"] == 1.5
    assert attrs(fx)["colour"] == (1.0, 0.0, 0.0)
    result = env["result"]
    assert result["applied"] == ["mode", "ratio", "colour"] and result["failed"] == {}
    assert result["before"]["ratio"] == 2.35 and result["after"]["ratio"] == 1.5
    assert result["after"]["colour"] == [1.0, 0.0, 0.0]
    assert result["unchanged"] == [] and result["effect"]["attributes"]["ratio"] == 1.5


def test_a_value_the_effect_already_holds_is_listed_as_unchanged(rig: Rig) -> None:
    create(rig, LETTERBOX)
    env = set_fx(rig, {"ratio": 2.35, "mode": "1.85:1 (Flat)"})
    assert env["ok"] is True and env["result"]["unchanged"] == ["ratio"]


def test_bypass_is_a_boolean_attribute_of_every_effect(rig: Rig) -> None:
    create(rig, "Blur")
    env = set_fx(rig, {"bypass": True}, "Blur")
    assert env["ok"] is True and fx_of(seg_a(rig), "Blur").bypass.get_value() is True
    bad = set_fx(rig, {"bypass": "yes"}, "Blur")
    assert bad["errorCode"] == "INVALID_ARGS" and "bypass" in bad["result"]["problems"]


@pytest.mark.parametrize(
    ("values", "word"),
    [
        ({"mode": "not a mode"}, "is not one of"),
        ({"ratio": 0.0}, "outside the range"),
        ({"ratio": 500}, "outside the range"),
        ({"ratio": "wide"}, "expected a value shaped like"),
        ({"colour": [2.0, 0.0, 0.0]}, "element 0 (2.0) is outside its range"),
        ({"colour": [0.0, -0.5, 0.0]}, "element 1 (-0.5) is outside its range"),
        ({"colour": [0.5, 0.5]}, "expected 3 numbers"),
        ({"colour": [0.5, "a", 0.5]}, "element 1 is 'a', not a number"),
    ],
)
def test_a_value_outside_the_candidates_or_the_range_is_refused_and_nothing_is_written(
    rig: Rig, values: dict[str, Any], word: str
) -> None:
    create(rig, LETTERBOX)
    records_before = list(rig.records())
    fx = fx_of(seg_a(rig), LETTERBOX)
    before = attrs(fx)
    env = set_fx(rig, values)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert word in env["error"] and set(env["result"]["problems"]) == set(values)
    assert attrs(fx) == before and rig.records() == records_before


def test_an_integer_attribute_takes_integers_only(rig: Rig) -> None:
    from fake_media_tree import FakeAttr

    create(rig, "Blur")
    fx = fx_of(seg_a(rig), "Blur")
    passes = FakeAttr(3)  # an integer attribute (the rule of every set_attributes)
    vars(fx)["passes"] = passes
    fx.attributes.append("passes")
    refused = set_fx(rig, {"passes": 4.0}, "Blur")
    assert refused["errorCode"] == "INVALID_ARGS" and "pass an integer" in refused["error"]
    assert passes.get_value() == 3
    assert set_fx(rig, {"passes": 4}, "Blur")["ok"] is True and passes.get_value() == 4


def test_one_bad_value_stops_the_good_one_next_to_it(rig: Rig) -> None:
    create(rig, LETTERBOX)
    fx = fx_of(seg_a(rig), LETTERBOX)
    env = set_fx(rig, {"ratio": 1.5, "mode": "nope"})
    assert env["errorCode"] == "INVALID_ARGS" and attrs(fx)["ratio"] == 2.35


def test_a_name_the_effect_does_not_list_is_invalid_args_with_the_names_it_has(rig: Rig) -> None:
    create(rig, "Blur")
    env = set_fx(rig, {"amount": 3}, "Blur")
    assert env["errorCode"] == "INVALID_ARGS" and env["result"]["attributes"] == ["bypass"]
    assert "no such attribute" in env["error"] and "['bypass']" in env["error"]


def test_empty_values_and_an_effect_the_segment_has_not_are_refused(rig: Rig) -> None:
    assert set_fx(rig, {})["errorCode"] == "INVALID_ARGS"
    missing = set_fx(rig, {"ratio": 1.5})
    assert missing["errorCode"] == "PRECONDITION_UNMET" and "create_effect" in missing["error"]
    assert untouched_world(rig)


def test_a_write_that_does_not_stick_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(rig, LETTERBOX)
    fx = fx_of(seg_a(rig), LETTERBOX)
    monkeypatch.setattr(type(fx.ratio), "set_value", lambda self, value: None)
    env = set_fx(rig, {"ratio": 1.5})
    assert env["errorCode"] == "SILENT_NOOP" and "silent_noop" in env["error"]
    assert env["result"]["applied"] == [] and "ratio" in env["result"]["failed"]


def test_a_revert_puts_the_old_settings_back(rig: Rig) -> None:
    create(rig, LETTERBOX)
    before = fingerprint_of_target(rig, S1)
    env = set_fx(rig, {"ratio": 1.5, "colour": [0.0, 1.0, 0.0]})
    assert fingerprint_of_target(rig, S1) != before
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    restored = out["result"]["restored"]["selector"]
    assert fingerprint_of_target(rig, restored) == before


def test_set_effect_is_declared_and_describes_what_it_checks(rig: Rig) -> None:
    op = rig.registry.get(SET_FX)
    assert op is not None and op.checkpoint is not None
    assert (op.effect, op.undo) == ("reversible", "checkpoint")
    assert [p.name for p in op.params] == ["target", "type", "values"]
    for word in ("candidates", "range", "result.problems", "INVALID_ARGS", "effect_load_setup"):
        assert word in op.description


def save(rig: Rig, path: Path, fx_type: str = LETTERBOX, **extra: Any) -> dict[str, Any]:
    return rig.do(
        SAVE_FX,
        {"target": extra.pop("selector", a_seg()), "type": fx_type, "path": str(path), **extra},
    )


def test_save_setup_writes_the_files_flame_appends_extensions_to(rig: Rig, tmp_path: Path) -> None:
    create(rig, LETTERBOX)
    create(rig, "Blur")
    records_before = len(rig.records())
    out = tmp_path / "setups"
    out.mkdir()
    env = save(rig, out / "lb")
    assert env["ok"] is True, env
    assert sorted(Path(f["path"]).name for f in env["result"]["files"]) == [
        "lb.letterbox",
        "lb.letterbox_node",
    ]
    assert all(f["size"] > 0 for f in env["result"]["files"])
    assert env["result"]["overwritten"] == [] and env["result"]["returned"] is True
    blur = save(rig, out / "bl", "Blur")
    assert [Path(f["path"]).name for f in blur["result"]["files"]] == ["bl.blur_node"]
    assert len(rig.records()) == records_before  # nothing in the project changed: no journal


def test_save_setup_takes_no_checkpoint_and_is_a_create_without_undo(
    rig: Rig, tmp_path: Path
) -> None:
    create(rig, "Blur")
    before = len(checkpoints(rig))
    env = save(rig, tmp_path / "bl", "Blur")
    assert len(checkpoints(rig)) == before and env["undoSteps"] == 0
    op = rig.registry.get(SAVE_FX)
    assert op is not None and op.checkpoint is None and op.wrap is None
    assert (op.effect, op.undo) == ("create", "none")


def test_existing_files_are_refused_unless_overwrite_is_set(rig: Rig, tmp_path: Path) -> None:
    create(rig, "Blur")
    assert save(rig, tmp_path / "bl", "Blur")["ok"] is True
    again = save(rig, tmp_path / "bl", "Blur")
    assert again["errorCode"] == "PRECONDITION_UNMET" and "overwrite=true" in again["error"]
    over = save(rig, tmp_path / "bl", "Blur", overwrite=True)
    assert over["ok"] is True
    assert [Path(p).name for p in over["result"]["overwritten"]] == ["bl.blur_node"]


def test_a_relative_path_a_missing_directory_and_a_missing_effect_are_refused(
    rig: Rig, tmp_path: Path
) -> None:
    create(rig, "Blur")
    rel = rig.do(SAVE_FX, {"target": a_seg(), "type": "Blur", "path": "setups/bl"})
    assert rel["errorCode"] == "INVALID_ARGS"
    nodir = save(rig, tmp_path / "nowhere" / "bl", "Blur")
    assert nodir["errorCode"] == "PRECONDITION_UNMET" and "does not exist" in nodir["error"]
    noeffect = save(rig, tmp_path / "x", "Resize")
    assert (
        noeffect["errorCode"] == "PRECONDITION_UNMET" and "no effect of type" in noeffect["error"]
    )
    gap = save(rig, tmp_path / "x", "Blur", selector=sel_at(0))
    assert gap["errorCode"] == "PRECONDITION_UNMET" and "Gap" in gap["error"]
    assert list(tmp_path.glob("x*")) == []


def test_a_path_directly_in_a_shared_temp_directory_is_refused_unless_allowed(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(rig, "Blur")
    monkeypatch.setattr(effects_ops, "is_default_output_dir", lambda path: path == str(tmp_path))
    refused = save(rig, tmp_path / "bl", "Blur")
    assert (
        refused["errorCode"] == "PRECONDITION_UNMET" and "shared temp directory" in refused["error"]
    )
    assert list(tmp_path.glob("bl*")) == []
    allowed = save(rig, tmp_path / "bl", "Blur", allow_tmp=True)
    assert allowed["ok"] is True and (tmp_path / "bl.blur_node").exists()


def test_a_save_that_writes_no_file_is_a_silent_noop(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(rig, "Blur")
    monkeypatch.setattr(PyTimelineFX, "save_setup", lambda self, path: True)
    env = save(rig, tmp_path / "bl", "Blur")
    assert env["errorCode"] == "SILENT_NOOP" and env["result"]["files"] == []


def test_a_segment_inside_a_library_can_be_saved(rig: Rig, tmp_path: Path) -> None:
    copy = library_copy(rig)
    env = save(
        rig,
        tmp_path / "lb",
        selector=seg_sel(wt(copy.get_wiretap_node_id()), 0, index=3),
    )
    assert env["ok"] is True and (tmp_path / "lb.letterbox_node").exists()


def test_load_setup_brings_the_saved_attributes_back(rig: Rig, tmp_path: Path) -> None:
    create(rig, LETTERBOX)
    assert set_fx(rig, {"ratio": 1.5, "mode": "1.85:1 (Flat)"})["ok"] is True
    saved = save(rig, tmp_path / "lb")
    path = next(f["path"] for f in saved["result"]["files"] if f["path"].endswith("_node"))
    assert set_fx(rig, {"ratio": 4.0, "mode": "2.39:1"})["ok"] is True
    env = rig.do(LOAD_FX, {"target": a_seg(), "type": LETTERBOX, "path": path})
    assert env["ok"] is True, env
    fx = fx_of(seg_a(rig), LETTERBOX)
    assert attrs(fx)["ratio"] == 1.5 and attrs(fx)["mode"] == "1.85:1 (Flat)"
    result = env["result"]
    assert result["returned"] is True and result["exception"] is None
    assert result["changedKeys"] == ["mode", "ratio"]
    assert result["trackChanged"] is False  # the segment's timing is the same, the effect is not
    assert result["attributes"]["before"]["ratio"] == 4.0
    assert result["attributes"]["after"]["ratio"] == 1.5 and result["size"] > 0


def test_a_parameter_that_is_not_an_attribute_is_loaded_but_cannot_be_read_back(
    rig: Rig, tmp_path: Path
) -> None:
    create(rig, "Blur")
    fx = fx_of(seg_a(rig), "Blur")
    fx.hidden = {"amount": 7.0}
    saved = save(rig, tmp_path / "bl", "Blur")
    path = saved["result"]["files"][0]["path"]
    fx.hidden = {"amount": 1.0}
    env = rig.do(LOAD_FX, {"target": a_seg(), "type": "Blur", "path": path})
    assert env["ok"] is True and fx_of(seg_a(rig), "Blur").hidden == {"amount": 7.0}
    assert env["result"]["changedKeys"] == [] and env["result"]["returned"] is True
    assert "return value" in rig.registry.get(LOAD_FX).description  # type: ignore[union-attr]


def test_a_path_that_is_not_there_is_refused_on_this_machine_before_anything_is_journaled(
    rig: Rig, tmp_path: Path
) -> None:
    create(rig, "Blur")
    records_before = len(rig.records())
    env = rig.do(LOAD_FX, {"target": a_seg(), "type": "Blur", "path": str(tmp_path / "nope.x")})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "no setup file" in env["error"]
    empty = tmp_path / "empty.blur_node"
    empty.write_bytes(b"")
    sent = len(rig.exec.calls)
    zero = rig.do(LOAD_FX, {"target": a_seg(), "type": "Blur", "path": str(empty)})
    assert zero["errorCode"] == "PRECONDITION_UNMET" and "0 bytes" in zero["error"]
    assert len(rig.exec.calls) == sent  # refused by the server: nothing was sent to Flame
    rel = rig.do(LOAD_FX, {"target": a_seg(), "type": "Blur", "path": "x.blur_node"})
    assert rel["errorCode"] == "INVALID_ARGS"
    assert len(rig.records()) == records_before


def test_loading_into_a_type_the_segment_has_not_says_to_create_it_first(
    rig: Rig, tmp_path: Path
) -> None:
    create(rig, "Blur")
    saved = save(rig, tmp_path / "bl", "Blur")
    path = saved["result"]["files"][0]["path"]
    env = rig.do(LOAD_FX, {"target": sel_at(3), "type": "Blur", "path": path})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "create_effect first" in env["error"]


def test_a_setup_of_another_type_is_reported_as_it_comes_with_the_checkpoint(
    rig: Rig, tmp_path: Path
) -> None:
    create(rig, "Blur")
    create(rig, "Resize")
    saved = save(rig, tmp_path / "bl", "Blur")
    blur_file = saved["result"]["files"][0]["path"]
    before = fingerprint_of_target(rig, S1)
    env = rig.do(LOAD_FX, {"target": a_seg(), "type": "Resize", "path": blur_file})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "Could not load the setup" in env["error"] and env["result"]["returned"] is None
    assert env["result"]["trackChanged"] is False and "unchanged" in env["error"]
    assert env["journal"]["checkpoint"] in env["hint"]
    assert fingerprint_of_target(rig, S1) == before


def test_load_setup_is_declared_as_a_checkpointed_write(rig: Rig) -> None:
    op = rig.registry.get(LOAD_FX)
    assert op is not None and op.checkpoint is not None
    assert (op.effect, op.undo) == ("reversible", "checkpoint")
    assert [p.name for p in op.params] == ["target", "type", "path"]
    for word in ("return value", "fingerprint sees attributes only", "exception"):
        assert word in " ".join(op.description.split())


def test_every_effect_op_leaves_a_group_that_one_revert_undoes(rig: Rig) -> None:
    start = fingerprint_of_target(rig, S1)
    groups = [create(rig, "Blur")["journal"]["group"]]
    groups.append(set_fx(rig, {"bypass": True}, "Blur")["journal"]["group"])
    groups.append(rig.do(DELETE_FX, {"target": a_seg(), "type": "Blur"})["journal"]["group"])
    index = Index(rig.records())
    assert all(index.groups[g][0].outcome["ok"] is True for g in groups)  # type: ignore[index]
    out = rig.do("journal.revert", {"group": groups[0]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT"  # later edits came after it
    forced = rig.do("journal.revert", {"group": groups[0], "force": True})
    assert forced["ok"] is True
    assert fingerprint_of_target(rig, forced["result"]["restored"]["selector"]) == start


def test_readonly_denies_every_effect_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    args = {"target": a_seg(), "type": "Blur"}
    for op, extra in (
        (CREATE_FX, {}),
        (DELETE_FX, {}),
        (SET_FX, {"values": {"bypass": True}}),
        (LOAD_FX, {"path": str(tmp_path / "x")}),
        (SAVE_FX, {"path": str(tmp_path / "x")}),
    ):
        assert ro.do(op, {**args, **extra})["errorCode"] == "POLICY_DENIED", op
    assert untouched_world(ro)


# Flame checks again at the moment of the call (the server checked first).


def test_flame_checks_a_setup_file_again_when_it_went_missing_after_the_server_looked(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(rig, "Blur")
    setup = tmp_path / "later.blur_node"
    setup.write_text("{}")
    loads: list[str] = []

    def recording_load(self: PyTimelineFX, path: str) -> bool:
        loads.append(path)
        return True

    monkeypatch.setattr(PyTimelineFX, "load_setup", recording_load)

    def vanish(label: str | None) -> None:
        if label == LOAD_FX and setup.exists():
            setup.unlink()

    rig.exec.before_run = vanish
    env = rig.do(LOAD_FX, {"target": a_seg(), "type": "Blur", "path": str(setup)})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "no setup file" in env["error"]
    assert loads == [] and env["result"]["ran"] is False
    setup.write_text("{}")

    def truncate(label: str | None) -> None:
        if label == LOAD_FX:
            setup.write_bytes(b"")

    rig.exec.before_run = truncate
    empty = rig.do(LOAD_FX, {"target": a_seg(), "type": "Blur", "path": str(setup)})
    assert empty["errorCode"] == "PRECONDITION_UNMET" and "0 bytes" in empty["error"]
    assert loads == []


def test_flame_checks_for_existing_files_again_at_the_moment_of_the_save(
    rig: Rig, tmp_path: Path
) -> None:
    create(rig, "Blur")

    def appear(label: str | None) -> None:
        if label == SAVE_FX:
            (tmp_path / "late.blur_node").write_text("someone else's")

    rig.exec.before_run = appear
    env = save(rig, tmp_path / "late", "Blur")
    assert env["errorCode"] == "PRECONDITION_UNMET" and "overwrite=true" in env["error"]
    assert (tmp_path / "late.blur_node").read_text() == "someone else's"
    assert env["result"]["existing"] == [str(tmp_path / "late.blur_node")]


def test_a_segment_can_be_addressed_by_record_in_and_the_result_gives_the_next_selector(
    rig: Rig,
) -> None:
    first = create(rig, "Blur", seg_sel(S1, 0, record_in=10))
    assert first["ok"] is True, first
    next_selector = first["result"]["position"]["selector"]
    assert next_selector["record_in"] == 10 and next_selector["expect_source_name"] == "clip1"
    second = set_fx(rig, {"bypass": True}, "Blur", next_selector)
    assert second["ok"] is True and fx_of(seg_a(rig), "Blur").bypass.get_value() is True
    third = rig.do(DELETE_FX, {"target": second["result"]["position"]["selector"], "type": "Blur"})
    assert third["ok"] is True and effect_names(seg_a(rig)) == []
    wrong = seg_sel(S1, 0, record_in=10)
    wrong["expect_source_name"] = "clip2"
    assert create(rig, "Blur", wrong)["errorCode"] == "CONFLICT"
