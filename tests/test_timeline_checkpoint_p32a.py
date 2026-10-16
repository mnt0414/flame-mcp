"""Tests `timeline.fingerprint` and `timeline.checkpoint`.

What the fingerprint sees and does not, the stash library / reel made on first use and found
again, what a checkpoint refuses, and the journal record. Run through the real Runtime with a
scripted `transport.exec` over the fake Flame tree (journal_test_helpers.py, fake_media_tree.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyTimelineFX
from flame_mcp import timeline_checkpoints
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from timeline_p32a_helpers import (
    CHECKPOINT,
    CLIPX,
    FINGERPRINT,
    LIB_NAME,
    REEL_WT,
    S1,
    S1_WT,
    S2,
    checkpoints,
    fingerprint_of_target,
    library_copy,
    make_timeline_rig,
    reel_names,
    rows,
    stash_entries,
    stash_library,
    take_checkpoint,
    uid,
)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def fingerprint(rig: Rig, target: dict[str, Any] = S1) -> dict[str, Any]:
    env = rig.do(FINGERPRINT, {"target": target})
    assert env["ok"] is True, env
    result: dict[str, Any] = env["result"]
    return result


def test_fingerprint_is_stable_and_summarises_the_sequence(rig: Rig) -> None:
    first = fingerprint(rig)
    assert fingerprint(rig)["fingerprint"] == first["fingerprint"]
    assert len(first["fingerprint"]) == 64
    assert first["versions"] == [[4, 1]]
    assert first["segmentCount"] == 5 and first["effectCount"] == 1 and first["unreadable"] == 0


def test_fingerprint_names_what_it_read(rig: Rig) -> None:
    result = fingerprint(rig)
    target = result["target"]
    assert target["type"] == "PySequence" and target["name"] == "S1"
    assert target["uid"] == uid(rig.handles["s1"]) and target["wiretapId"] == S1_WT
    assert target["parent"]["name"] == "Reel 1" and target["parent"]["type"] == "PyReel"
    assert result["stable"] == S1


def test_fingerprint_sees_a_trim(rig: Rig) -> None:
    before = fingerprint(rig)["fingerprint"]
    rig.handles["s1"].versions[0].tracks[0].segments[1].trim_tail(3)  # a transport edit
    assert fingerprint(rig)["fingerprint"] != before


def test_fingerprint_sees_a_trim_on_the_second_track_and_a_moved_record_in(rig: Rig) -> None:
    before = fingerprint(rig)["fingerprint"]
    rig.handles["s1"].versions[0].tracks[1].segments[0].trim_head(2)
    assert fingerprint(rig)["fingerprint"] != before


def test_fingerprint_does_not_see_a_segments_name_or_attributes_or_the_sequences_name(
    rig: Rig,
) -> None:
    before = fingerprint(rig)["fingerprint"]
    segment = rig.handles["s1"].versions[0].tracks[0].segments[1]
    segment.name.set_value("renamed")
    segment.colour.set_value((1.0, 0.0, 0.0))
    segment.tags.set_value(["a"])
    rig.handles["s1"].name.set_value("S1 renamed")
    assert fingerprint(rig)["fingerprint"] == before


def test_fingerprint_sees_an_effect_attribute_and_an_added_effect(rig: Rig) -> None:
    before = fingerprint(rig)["fingerprint"]
    effect = rig.handles["s1"].versions[0].tracks[0].segments[3].effects[0]
    effect.ratio.set_value(2.35)
    changed = fingerprint(rig)["fingerprint"]
    assert changed != before
    other = rig.handles["s1"].versions[0].tracks[0].segments[1]
    other.effects.append(PyTimelineFX("Blur"))
    assert fingerprint(rig)["fingerprint"] != changed
    assert fingerprint(rig)["effectCount"] == 2


def test_fingerprint_sees_the_order_of_segments_and_of_tracks(rig: Rig) -> None:
    before = fingerprint(rig)["fingerprint"]
    version = rig.handles["s1"]._versions[0]
    version._tracks.reverse()
    assert fingerprint(rig)["fingerprint"] != before


def test_fingerprint_counts_what_it_could_not_read_and_still_answers(rig: Rig) -> None:
    class Unreadable(Exception):
        pass

    segment = rig.handles["s1"].versions[0].tracks[0].segments[1]

    class Broken(type(segment)):  # type: ignore[misc]
        @property
        def head(self) -> Any:
            raise Unreadable("no head")

    segment.__class__ = Broken
    result = fingerprint(rig)
    assert result["unreadable"] >= 1 and len(result["fingerprint"]) == 64


def test_fingerprint_of_a_clip_and_of_something_that_is_not_media(rig: Rig) -> None:
    assert fingerprint(rig, CLIPX)["segmentCount"] == 1
    env = rig.do(FINGERPRINT, {"target": {"kind": "wiretap", "id": REEL_WT}})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE"


def test_fingerprint_is_a_read_and_changes_nothing(rig: Rig) -> None:
    op = rig.registry.get(FINGERPRINT)
    assert op is not None and (op.effect, op.undo, op.category, op.thread) == (
        "read",
        "none",
        "timeline",
        "main",
    )
    fingerprint(rig)
    assert rig.records() == [] and stash_library(rig) is None


def test_fingerprint_description_names_what_it_does_not_see(rig: Rig) -> None:
    op = rig.registry.get(FINGERPRINT)
    assert op is not None
    for word in ("markers", "transitions", "audio", "playhead", "selection", "names"):
        assert word in op.description


def test_the_first_checkpoint_makes_the_stash_library_and_reel_and_copies_into_it(rig: Rig) -> None:
    assert stash_library(rig) is None
    result = take_checkpoint(rig, S1, "first")
    library = stash_library(rig)
    assert library is not None and reel_names(library) == [timeline_checkpoints.stash_reel_name()]
    assert result["libraryCreated"] is True and result["reelCreated"] is True
    [entry] = stash_entries(rig)
    assert result["stash"]["uid"] == uid(entry) and result["stash"]["uid"] != uid(rig.handles["s1"])
    assert result["stash"]["library"] == LIB_NAME
    assert result["stash"]["reel"] == timeline_checkpoints.stash_reel_name()
    assert (
        result["stashMatches"] is True and result["fingerprint"] == fingerprint(rig)["fingerprint"]
    )
    assert rows(entry) == rows(rig.handles["s1"]) and rows(entry, 1) == rows(rig.handles["s1"], 1)


def test_the_second_checkpoint_reuses_the_library_and_reel(rig: Rig) -> None:
    take_checkpoint(rig, S1)
    second = take_checkpoint(rig, S2)
    assert second["libraryCreated"] is False and second["reelCreated"] is False
    assert len(libraries_named(rig)) == 1 and len(stash_entries(rig)) == 2
    assert len(reel_names(stash_library(rig))) == 1  # one reel for the day


def libraries_named(rig: Rig) -> list[Any]:
    return [
        lib
        for lib in rig.flame.projects.current_project.current_workspace.libraries
        if str(lib.name.get_value()) == LIB_NAME
    ]


def test_the_stash_library_name_comes_from_the_environment_or_the_default(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(timeline_checkpoints.LIBRARY_ENV)
    take_checkpoint(rig, S1)
    names = [
        str(lib.name.get_value())
        for lib in rig.flame.projects.current_project.current_workspace.libraries
    ]
    assert timeline_checkpoints.DEFAULT_LIBRARY == "flame_mcp_checkpoints"
    assert "flame_mcp_checkpoints" in names and LIB_NAME not in names


def test_the_journal_record_has_the_source_the_stash_and_the_fingerprint(rig: Rig) -> None:
    result = take_checkpoint(rig, S1, "before edit")
    [record] = checkpoints(rig)
    assert record["kind"] == "timeline" and record["id"] == result["checkpoint"]
    assert record["label"] == "before edit" and record["group"] is None
    assert record["source"] == {
        "type": "PySequence",
        "name": "S1",
        "uid": uid(rig.handles["s1"]),
        "wiretapId": S1_WT,
        "parent": {
            "type": "PyReel",
            "name": "Reel 1",
            "uid": "uid-reel",
            "wiretapId": REEL_WT,
        },
    }
    assert record["stash"]["uid"] == result["stash"]["uid"]
    assert record["stash"]["wiretapId"] == result["stash"]["wiretapId"]
    assert record["stash"]["library"] == LIB_NAME
    assert record["fingerprint"] == result["fingerprint"] and record["segmentCount"] == 5
    assert record["displayed"] is False


def test_a_checkpoint_of_the_displayed_sequence_says_so(rig: Rig) -> None:
    rig.flame.timeline.clip = rig.handles["s1"]
    assert take_checkpoint(rig, S1)["displayed"] is True
    assert take_checkpoint(rig, S2)["displayed"] is False
    assert [c["displayed"] for c in checkpoints(rig)] == [True, False]


def test_a_checkpoint_of_a_clip_on_the_desktop(rig: Rig) -> None:
    result = take_checkpoint(rig, CLIPX)
    assert result["source"]["type"] == "PyClip" and result["segmentCount"] == 1
    assert result["stash"]["type"] == "PyClip"


def test_a_checkpoint_changes_nothing_in_the_target(rig: Rig) -> None:
    before = rows(rig.handles["s1"])
    uid_before = uid(rig.handles["s1"])
    take_checkpoint(rig, S1)
    assert rows(rig.handles["s1"]) == before and uid(rig.handles["s1"]) == uid_before
    assert rig.flame.media_panel.move_calls == []


def test_the_checkpoint_works_from_a_path_or_a_selection_selector(rig: Rig) -> None:
    rig.flame.media_panel.selected_entries = [rig.handles["s2"]]
    result = take_checkpoint(rig, {"kind": "selection"})
    assert result["source"]["name"] == "S2"


def test_a_target_inside_a_library_is_refused_and_leaves_nothing_behind(rig: Rig) -> None:
    copy = library_copy(rig)
    env = rig.do(CHECKPOINT, {"target": {"kind": "wiretap", "id": copy.get_wiretap_node_id()}})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "inside a library" in env["error"]
    assert stash_library(rig) is None and checkpoints(rig) == []


def test_a_target_inside_a_batch_group_is_refused(rig: Rig) -> None:
    batch_reel = rig.handles["bg1"].create_reel("Schematic Reel 1")
    [copy] = rig.flame.media_panel.copy(rig.handles["s1"], batch_reel)
    env = rig.do(CHECKPOINT, {"target": {"kind": "wiretap", "id": copy.get_wiretap_node_id()}})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "Batch group" in env["error"]
    assert stash_library(rig) is None


def test_a_target_whose_parents_cannot_be_followed_is_refused_not_guessed_at(rig: Rig) -> None:
    rig.handles["s1"].parent = None
    env = rig.do(CHECKPOINT, {"target": S1})
    assert (
        env["errorCode"] == "PRECONDITION_UNMET"
        and "not found to be on the Desktop" in env["error"]
    )
    assert stash_library(rig) is None


def test_something_that_is_not_a_sequence_or_clip_is_refused(rig: Rig) -> None:
    env = rig.do(CHECKPOINT, {"target": {"kind": "wiretap", "id": REEL_WT}})
    assert env["errorCode"] == "SELECTOR_TYPE" and checkpoints(rig) == []


def test_a_copy_that_does_not_have_the_fingerprint_is_an_error_that_names_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = type(rig.handles["libreel"]).receive

    def lossy(self: Any, sources: list[Any]) -> list[Any]:
        copies: list[Any] = original(self, sources)
        copies[0]._versions[0]._tracks[0]._segments[1].source_name = "somewhere else"
        return copies

    monkeypatch.setattr(type(rig.handles["libreel"]), "receive", lossy)
    env = rig.do(CHECKPOINT, {"target": S1})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "NOT deleted" in env["error"] and "no checkpoint was recorded" in env["error"]
    [entry] = stash_entries(rig)
    assert env["result"]["stash"]["uid"] == uid(entry) and env["result"]["stashMatches"] is False
    assert checkpoints(rig) == []


def test_a_copy_that_leaves_no_new_entry_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(type(rig.handles["libreel"]), "receive", lambda self, sources: [])
    env = rig.do(CHECKPOINT, {"target": S1})
    assert env["errorCode"] == "SILENT_NOOP" and checkpoints(rig) == []


def test_a_checkpoint_the_journal_cannot_take_says_where_the_copy_is(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(record: dict[str, Any]) -> None:
        raise OSError("read-only volume")

    monkeypatch.setattr(rig.journal, "append", refuse)
    env = rig.do(CHECKPOINT, {"target": S1})
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    [entry] = stash_entries(rig)
    assert str(uid(entry)) in env["error"] and "cannot be restored by id" in env["error"]


def test_reserved_arguments_are_rejected_from_a_caller(rig: Rig) -> None:
    env = rig.do(CHECKPOINT, {"target": S1, "_library": "evil", "_reel": "x"})
    assert env["errorCode"] == "INVALID_ARGS"


def test_the_checkpoint_is_declared_as_a_create_with_no_undo_of_its_own(rig: Rig) -> None:
    op = rig.registry.get(CHECKPOINT)
    assert op is not None
    assert (op.effect, op.undo, op.thread, op.category) == ("create", "none", "main", "timeline")
    assert op.wrap is not None and op.checkpoint is None


def test_readonly_hides_the_checkpoint_but_not_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    assert ro.do(CHECKPOINT, {"target": S1})["errorCode"] == "POLICY_DENIED"
    assert ro.do(FINGERPRINT, {"target": S1})["ok"] is True
    assert stash_library(ro) is None


def test_the_fingerprint_of_the_stash_equals_the_original_and_a_later_edit_diverges(
    rig: Rig,
) -> None:
    result = take_checkpoint(rig, S1)
    assert fingerprint_of_target(rig, S1) == result["fingerprint"]
    rig.handles["s1"].versions[0].tracks[0].segments[1].trim_tail(3)
    assert fingerprint_of_target(rig, S1) != result["fingerprint"]
    stash = {"kind": "wiretap", "id": result["stash"]["wiretapId"]}
    assert fingerprint_of_target(rig, stash) == result["fingerprint"]


def _checkpointed_spec(**over: Any) -> Any:
    from flame_mcp.types import CheckpointSpec, OperationParam, OperationSpec

    spec: dict[str, Any] = {
        "name": "segment.checkpointed_probe",
        "category": "segment",
        "description": "x",
        "effect": "reversible",
        "undo": "checkpoint",
        "params": (OperationParam(name="target", type="selector", description="x", required=True),),
        "accepts": ("segment",),
        "to_python": lambda args: "_result = 1",
        "undo_steps": lambda args, affected: 0,
        "checkpoint": CheckpointSpec(
            target_kind="timeline",
            group_selector=lambda args: args["target"]["parent"],
            rebind=lambda args, stable: dict(args),
        ),
    }
    spec.update(over)
    return OperationSpec(**spec)


def test_registration_accepts_a_timeline_checkpointed_op_and_checks_its_selectors() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import RegistrationError, Registry
    from flame_mcp.types import CheckpointSpec

    registry = Registry()
    register_all(registry)
    registry.register(_checkpointed_spec())
    with pytest.raises(RegistrationError, match="through a 'segment' selector"):
        registry.register(_checkpointed_spec(name="segment.probe_b", accepts=("wiretap",)))
    with pytest.raises(RegistrationError, match="'wiretap', 'path' or 'selection'"):
        registry.register(
            _checkpointed_spec(
                name="segment.probe_c",
                accepts=("segment",),
                checkpoint=CheckpointSpec(target_kind="timeline"),
            )
        )
    with pytest.raises(RegistrationError, match="go together"):
        registry.register(
            _checkpointed_spec(
                name="segment.probe_d",
                checkpoint=CheckpointSpec(
                    target_kind="timeline", group_selector=lambda args: args["target"]
                ),
            )
        )
    with pytest.raises(RegistrationError, match="must accept 'batch'"):
        registry.register(
            _checkpointed_spec(
                name="segment.probe_e",
                checkpoint=CheckpointSpec(),
            )
        )
