"""Tests `segment.edit_effect_setup`: one value of a setup file replaced, loaded, saved again and
read back.

Run through the real Runtime over the fake Flame tree with a fake whose effect setups are text made
of tags (setup_xml_helpers: one line, `True` / `False`, a read-back equal to the file loaded).
scripts/p5_setup_edit_e2e.py checks what the real Flame does with such a file. Where a test here
looks at what Flame answers, it describes the FAKE."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PySegment, PyTimelineFX
from flame_mcp import setup_edit
from flame_mcp.ops import plan as plan_mod
from journal_test_helpers import Rig
from p32b_helpers import PLAN, make_p32b_rig
from p33_helpers import LETTERBOX, a_seg, seg_a, seg_b, sel_at, untouched_world
from setup_xml_helpers import DEFAULT_XML, XmlBehaviour, install_effect_xml
from timeline_p32a_helpers import S1, checkpoints, fingerprint_of_target, library_copy, seg_sel, wt

EDIT = "segment.edit_effect_setup"
TW = "Timewarp"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


@pytest.fixture
def xml(monkeypatch: pytest.MonkeyPatch) -> XmlBehaviour:
    return install_effect_xml(monkeypatch)


def give_timewarp(seg: PySegment, text: str = DEFAULT_XML) -> PyTimelineFX:
    fx = seg.create_effect(TW)
    fx.hidden["xml"] = text
    return fx


def current_xml(seg: PySegment, fx_type: str = TW) -> str:
    fx = next(fx for fx in seg.effects if fx.type == fx_type)
    text: str = fx.hidden["xml"]
    return text


def edit(
    rig: Rig,
    values: dict[str, Any],
    selector: dict[str, Any] | None = None,
    fx_type: str = TW,
    **extra: Any,
) -> dict[str, Any]:
    args = {"target": selector or a_seg(), "type": fx_type, "values": values, **extra}
    return rig.do(EDIT, args)


def work_root(rig: Rig) -> Path:
    return Path(rig.journal.directory) / "setups"


def leftovers(rig: Rig) -> list[str]:
    root = work_root(rig)
    return sorted(str(p) for p in root.rglob("*")) if root.exists() else []


def mode(path: str | Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_value_is_replaced_loaded_and_read_back(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0})
    assert env["ok"] is True, env
    assert current_xml(seg_a(rig)) == DEFAULT_XML.replace("<TW_FrameMode>2<", "<TW_FrameMode>0<")
    result = env["result"]
    assert result["type"] == TW and result["requestedType"] == TW
    assert result["tags"] == {"TW_FrameMode": {"before": "2", "requested": "0", "after": "0"}}
    assert result["unchanged"] == [] and result["tagCounts"] == {"TW_FrameMode": 1}
    assert result["returned"] is True and result["filesKept"] is False
    assert result["position"]["selector"]["record_in"] == 10
    assert result["ran"] is True
    assert "files" not in result and "workDir" not in result
    assert env["journal"]["revert"]["op"] == "journal.revert"
    assert len(checkpoints(rig)) == 1
    assert "otherDifferences" not in result and env["degraded"] == []


def test_the_work_files_are_removed_after_a_success(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    assert edit(rig, {"TW_FrameMode": 0})["ok"] is True
    assert leftovers(rig) == []
    assert not Path(xml.saved[0]).exists()


def test_keep_files_leaves_private_files_under_the_journal_directory(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0}, keep_files=True)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["filesKept"] is True
    files = result["files"]
    for key in ("original", "edited", "readback"):
        assert files[key].startswith(str(work_root(rig))), files
        assert files[key].endswith(".timewarp_node")
        assert mode(files[key]) == 0o600, key
    workdir = Path(files["original"]).parent
    assert mode(workdir) == 0o700 and mode(work_root(rig)) == 0o700
    assert Path(files["original"]).read_text() == DEFAULT_XML
    assert "<TW_FrameMode>0<" in Path(files["edited"]).read_text()
    assert Path(files["readback"]).read_text() == Path(files["edited"]).read_text()


def test_the_work_directory_is_not_in_tmp(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    edit(rig, {"TW_FrameMode": 0})
    code = " ".join(call.code for call in rig.exec.calls)
    assert str(work_root(rig)) in code
    assert "/tmp/" not in code and "/var/tmp" not in code
    assert code.isascii()


def test_a_revert_puts_the_old_setup_back(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    before = fingerprint_of_target(rig, S1)
    env = edit(rig, {"TW_FrameMode": 0})
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    restored = rig.flame.find_by_wiretap_node_id(out["result"]["restored"]["selector"]["id"])
    assert current_xml(restored.versions[0].tracks[0].segments[1]) == DEFAULT_XML
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_the_intent_is_journaled_before_the_write(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    edit(rig, {"TW_FrameMode": 0})
    records = rig.records()
    assert [r["type"] for r in records] == ["checkpoint", "intent", "outcome"]
    assert records[1]["op"] == EDIT and records[1]["args"]["values"] == {"TW_FrameMode": 0}
    assert records[2]["ok"] is True


def test_a_bool_is_written_as_true_or_false(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_Bypass": True})
    assert env["ok"] is True, env
    assert "<TW_Bypass>True</TW_Bypass>" in current_xml(seg_a(rig))
    assert env["result"]["tags"]["TW_Bypass"] == {
        "before": "False",
        "requested": "True",
        "after": "True",
    }


def test_a_string_with_regex_replacement_syntax_goes_in_as_it_is(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": r"\1\0"})
    assert env["ok"] is True, env
    assert "<TW_FrameMode>\\1\\0</TW_FrameMode>" in current_xml(seg_a(rig))


def test_several_tags_are_set_and_one_that_already_matches_is_listed_as_unchanged(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0, "TW_Bypass": False})
    assert env["ok"] is True, env
    assert env["result"]["unchanged"] == ["TW_Bypass"]
    assert set(env["result"]["tags"]) == {"TW_FrameMode", "TW_Bypass"}
    assert "<TW_FrameMode>0<" in current_xml(seg_a(rig))


def test_nothing_to_change_is_refused_before_anything_is_written(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 2, "TW_Bypass": False})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "nothing to change" in env["error"]
    assert env["result"]["unchanged"] == ["TW_FrameMode", "TW_Bypass"]
    assert "workDir" not in env["result"] and "files" not in env["result"]
    assert untouched_world(rig) and xml.loads == 0 and leftovers(rig) == []
    assert current_xml(seg_a(rig)) == DEFAULT_XML


def test_a_number_equal_to_the_current_one_counts_as_no_change(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 2.0})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "nothing to change" in env["error"]


@pytest.mark.parametrize(
    ("tag", "word"),
    [
        ("TW_Nope", "not in the setup"),
        ("Frame", "2 times"),
        ("TW_Speed", "child"),
        ("Range", "attributes"),
    ],
)
def test_a_tag_that_is_not_one_plain_scalar_is_refused_with_the_counts(
    rig: Rig, xml: XmlBehaviour, tag: str, word: str
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0, tag: 1})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert env["result"]["tagProblems"][tag].count(word) == 1
    assert tag in env["result"]["tagCounts"] and env["result"]["tagCounts"]["TW_FrameMode"] == 1
    assert untouched_world(rig) and xml.loads == 0
    assert current_xml(seg_a(rig)) == DEFAULT_XML
    assert leftovers(rig) == []


@pytest.mark.parametrize(
    "value",
    ["a<b", "a>b", "a&b", 'a"b', "a\nb", "a\rb", "a\tb", "a\x00b", "0</TW_FrameMode><X>1"],
)
def test_a_value_that_could_change_the_structure_is_refused_and_nothing_is_sent(
    rig: Rig, xml: XmlBehaviour, value: str
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": value})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert rig.exec.calls == [] and rig.records() == [] and untouched_world(rig)
    assert current_xml(seg_a(rig)) == DEFAULT_XML


@pytest.mark.parametrize("tag", ["", "1abc", "a-b", "a b", "a/b", "x>", "a\n", "TW.Frame"])
def test_a_tag_name_of_another_shape_is_refused_and_nothing_is_sent(
    rig: Rig, xml: XmlBehaviour, tag: str
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {tag: 1})
    assert env["errorCode"] == "INVALID_ARGS", env
    assert rig.exec.calls == [] and untouched_world(rig)


def test_the_number_and_the_type_of_the_values_are_checked(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    too_many = {f"T{i}": i for i in range(21)}
    for values in ({}, too_many, {"TW_FrameMode": [1]}, {"TW_FrameMode": None}):
        env = edit(rig, values)
        assert env["errorCode"] == "INVALID_ARGS", (values, env)
    assert rig.exec.calls == []


def test_a_non_ascii_value_never_reaches_flame(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": "café"})
    assert env["errorCode"] == "INVALID_ARGS" and env["error"].isascii()
    assert rig.exec.calls == []


def test_the_setup_check_runs_before_the_checkpoint_and_leaves_nothing_behind(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    edit(rig, {"TW_FrameMode": 0})
    labels = [c.label for c in rig.exec.calls]
    assert labels.index(EDIT) < labels.index("timeline.checkpoint") < len(labels) - 1
    assert labels.count(EDIT) == 2  # the check phase, then the write


def test_an_effect_the_segment_has_not_a_gap_and_a_library_sequence_are_refused(
    rig: Rig, xml: XmlBehaviour
) -> None:
    missing = edit(rig, {"TW_FrameMode": 0})
    assert missing["errorCode"] == "PRECONDITION_UNMET" and "create_effect" in missing["error"]
    gap = edit(rig, {"TW_FrameMode": 0}, selector=sel_at(0))
    assert gap["errorCode"] == "PRECONDITION_UNMET" and "Gap" in gap["error"]
    copy = library_copy(rig)
    lib = edit(
        rig, {"TW_FrameMode": 0}, selector=seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1)
    )
    assert lib["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in lib["error"]
    assert checkpoints(rig) == [] and rig.records() == [] and xml.loads == 0


def test_a_setup_of_more_than_one_file_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_effect_xml(monkeypatch, XmlBehaviour(files=2))
    env = edit(rig, {"mode": "x"}, selector=sel_at(3), fx_type=LETTERBOX)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "2 files" in env["error"]
    assert len(env["result"]["savedFiles"]) == 2
    assert untouched_world(rig) and state.loads == 0 and leftovers(rig) == []


def test_a_setup_that_is_not_text_is_refused(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    give_timewarp(seg_a(rig))

    def save(self: PyTimelineFX, path: str) -> bool:
        Path(path + ".timewarp_node").write_bytes(b"<A><B>1</B>\xff\xfe</A>")
        return True

    monkeypatch.setattr(PyTimelineFX, "save_setup", save)
    env = edit(rig, {"B": 2})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "UTF-8" in env["error"]
    assert untouched_world(rig)


def test_a_load_flame_ignores_is_flame_api_error_with_the_values_it_left(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_effect_xml(monkeypatch, XmlBehaviour(load="ignore"))
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["tags"]["TW_FrameMode"] == {
        "before": "2",
        "requested": "0",
        "after": "2",
    }
    assert env["result"]["mismatched"] == ["TW_FrameMode"]
    assert env["result"]["filesKept"] is True
    assert Path(env["result"]["files"]["edited"]).exists() and state.loads == 1
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_a_load_that_raises_keeps_the_files_and_the_revert(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_effect_xml(monkeypatch, XmlBehaviour(load="raise"))
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0})
    assert env["errorCode"] == "FLAME_API_ERROR" and "Could not load the setup" in env["error"]
    assert env["result"]["filesKept"] is True and env["result"]["exception"]
    assert env["journal"]["revert"]["op"] == "journal.revert"
    assert current_xml(seg_a(rig)) == DEFAULT_XML


def test_a_load_that_answers_false_is_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_effect_xml(monkeypatch, XmlBehaviour(load="false"))
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0})
    assert env["errorCode"] == "FLAME_API_ERROR" and "False" in env["error"]
    assert env["result"]["filesKept"] is True


def test_a_readback_that_cannot_be_saved_is_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_effect_xml(monkeypatch, XmlBehaviour(save_nothing_after_load=True))
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0})
    assert env["errorCode"] == "FLAME_API_ERROR" and "read it back" in env["error"]
    assert env["result"]["filesKept"] is True
    assert "<TW_FrameMode>0<" in current_xml(seg_a(rig))  # the load did happen


def test_another_difference_in_the_readback_is_reported_not_failed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_effect_xml(monkeypatch, XmlBehaviour(load="normalize"))
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0})
    assert env["ok"] is True, env
    assert env["result"]["otherDifferences"] == {
        "Frame": {"edited": ["1", "2"], "readback": ["9", "2"]}
    }
    assert len(env["degraded"]) == 1 and "read-back" in env["degraded"][0]
    assert "degradedNotes" not in env["result"]
    assert env["result"]["filesKept"] is False and leftovers(rig) == []


def test_a_value_flame_writes_in_another_number_format_still_counts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_effect_xml(monkeypatch, XmlBehaviour(load="reformat"))
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0})
    assert env["ok"] is True, env
    assert env["result"]["tags"]["TW_FrameMode"]["after"] == "0.000000"
    assert len(env["degraded"]) == 1


def test_a_plan_bundles_two_edits_into_one_checkpointed_write(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    give_timewarp(seg_b(rig))
    before = fingerprint_of_target(rig, S1)
    steps = [
        {"op": EDIT, "args": {"target": sel_at(1), "type": TW, "values": {"TW_FrameMode": 0}}},
        {"op": EDIT, "args": {"target": sel_at(3), "type": TW, "values": {"TW_FrameMode": 1}}},
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is True, env
    assert [s["ok"] for s in env["result"]["steps"]] == [True, True]
    assert len(checkpoints(rig)) == 1
    assert [c.label for c in rig.exec.calls].count("timeline.checkpoint") == 1
    assert "<TW_FrameMode>0<" in current_xml(seg_a(rig))
    assert "<TW_FrameMode>1<" in current_xml(seg_b(rig))
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    restored = rig.flame.find_by_wiretap_node_id(out["result"]["restored"]["selector"]["id"])
    segments = restored.versions[0].tracks[0].segments
    assert current_xml(segments[1]) == DEFAULT_XML and current_xml(segments[3]) == DEFAULT_XML
    assert fingerprint_of_target(rig, out["result"]["restored"]["selector"]) == before


def test_a_step_whose_tag_is_not_unique_stops_the_plan_and_it_can_be_reverted(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    give_timewarp(seg_b(rig))
    steps = [
        {"op": EDIT, "args": {"target": sel_at(1), "type": TW, "values": {"TW_FrameMode": 0}}},
        {"op": EDIT, "args": {"target": sel_at(3), "type": TW, "values": {"Frame": 5}}},
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False
    assert [s["ok"] for s in env["result"]["steps"]] == [True, False]
    assert env["result"]["steps"][1]["errorCode"] == "PRECONDITION_UNMET"
    assert "<TW_FrameMode>0<" in current_xml(seg_a(rig))
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out


def test_a_plan_refuses_a_bad_value_before_it_runs_anything(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    steps = [
        {"op": EDIT, "args": {"target": sel_at(1), "type": TW, "values": {"TW_FrameMode": "<"}}},
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert rig.exec.calls == [] and untouched_world(rig)


def test_a_plan_summary_gives_the_position_of_the_edited_segment(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    steps = [{"op": EDIT, "args": {"target": sel_at(1), "type": TW, "values": {"TW_FrameMode": 0}}}]
    env = rig.do(PLAN, {"steps": steps, "result_detail": "summary"})
    assert env["ok"] is True, env
    brief = env["result"]["steps"][0]["brief"]
    assert brief["position"]["version"] == 0 and brief["position"]["index"] == 1


def test_the_op_is_declared_as_a_reversible_checkpointed_segment_edit(rig: Rig) -> None:
    op = rig.registry.get(EDIT)
    assert op is not None and op.checkpoint is not None
    assert (op.category, op.effect, op.undo) == ("segment", "reversible", "checkpoint")
    assert op.checkpoint.target_kind == "timeline" and op.accepts == ("segment",)
    assert [p.name for p in op.params] == ["target", "type", "values", "keep_files"]
    assert "nothing to change" in op.description
    assert "PRECONDITION_UNMET" in op.description and "SILENT_NOOP" in op.description
    assert setup_edit.SETUP_MAX_VALUES == 20


def test_keep_files_keeps_the_work_files_of_a_refusal_before_the_load_too(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    same = edit(rig, {"TW_FrameMode": 2}, keep_files=True)
    assert same["errorCode"] == "PRECONDITION_UNMET" and "nothing to change" in same["error"]
    files = same["result"]["files"]
    assert (
        same["result"]["filesKept"] is True and Path(files["original"]).read_text() == DEFAULT_XML
    )
    assert mode(files["original"]) == 0o600 and mode(Path(files["original"]).parent) == 0o700
    missing = edit(rig, {"TW_Nope": 1}, keep_files=True)
    assert missing["errorCode"] == "PRECONDITION_UNMET" and missing["result"]["filesKept"] is True
    assert Path(missing["result"]["files"]["original"]).exists()
    assert untouched_world(rig) and xml.loads == 0


def test_a_refusal_before_the_load_leaves_no_files_without_keep_files(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 2})
    assert "files" not in env["result"] and leftovers(rig) == []


def test_a_setup_of_several_files_is_kept_for_a_look_with_keep_files(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_effect_xml(monkeypatch, XmlBehaviour(files=2))
    env = edit(rig, {"mode": "x"}, selector=sel_at(3), fx_type=LETTERBOX, keep_files=True)
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["filesKept"] is True
    assert len(env["result"]["savedFiles"]) == 2 and all(
        os.path.exists(p) for p in env["result"]["savedFiles"]
    )


def test_an_exception_after_the_load_is_reported_with_the_files_and_the_revert(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_effect_xml(monkeypatch, XmlBehaviour(save_raises_after_load=True))
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 0})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "could not be saved" in env["error"] and "load" in env["error"]
    result = env["result"]
    assert result["ran"] is True and result["filesKept"] is True
    assert (
        Path(result["files"]["edited"]).exists() and result["tags"]["TW_FrameMode"]["after"] is None
    )
    assert "<TW_FrameMode>0<" in current_xml(seg_a(rig))  # the load did happen
    assert env["journal"]["revert"]["op"] == "journal.revert"


@pytest.mark.parametrize("detail", ["full", "summary"])
def test_a_difference_in_the_readback_survives_inside_a_plan(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, detail: str
) -> None:
    install_effect_xml(monkeypatch, XmlBehaviour(load="normalize"))
    give_timewarp(seg_a(rig))
    steps = [{"op": EDIT, "args": {"target": sel_at(1), "type": TW, "values": {"TW_FrameMode": 0}}}]
    env = rig.do(PLAN, {"steps": steps, "result_detail": detail})
    assert env["ok"] is True, env
    row = env["result"]["steps"][0]
    assert len(row["degraded"]) == 1 and "read-back" in row["degraded"][0], row


def test_a_long_degraded_note_is_cut_in_a_plan_summary_and_whole_in_a_full_one() -> None:
    step = plan_mod._Step(0, op_name=EDIT)
    env: dict[str, Any] = {
        "ok": True,
        "errorCode": None,
        "result": {},
        "degraded": ["x" * 500, "short"],
    }
    short, _, _ = plan_mod._step_summary(step, env, 0, "summary")
    assert short["degraded"] == ["x" * plan_mod._DEGRADED_NOTE_CHARS, "short"]
    full, _, _ = plan_mod._step_summary(step, env, 0, "full")
    assert full["degraded"] == ["x" * 500, "short"]
    none, _, _ = plan_mod._step_summary(step, {**env, "degraded": []}, 0, "full")
    assert "degraded" not in none


def test_the_values_are_checked_inside_flame_too_when_the_precheck_is_bypassed(
    rig: Rig, xml: XmlBehaviour
) -> None:
    from op_test_helpers import run_op

    give_timewarp(seg_a(rig))
    for values in ({"TW_FrameMode": "0<"}, {"a b": 1}, {}, {"T": "x" * 500}):
        args = {"target": a_seg(), "type": TW, "values": values}
        got = run_op(rig.registry, EDIT, args, rig.flame)
        assert got["opErrorCode"] == "INVALID_ARGS", (values, got["opError"])
    assert current_xml(seg_a(rig)) == DEFAULT_XML and xml.saved == []


def test_a_number_written_another_way_is_not_written_back(rig: Rig, xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": 2.0, "TW_Bypass": True})
    assert env["ok"] is True, env
    assert env["result"]["unchanged"] == ["TW_FrameMode"]
    assert "<TW_FrameMode>2</TW_FrameMode>" in current_xml(seg_a(rig))
    assert "<TW_Bypass>True</TW_Bypass>" in current_xml(seg_a(rig))


def test_a_string_request_is_not_taken_for_the_number_it_looks_like(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": "2.0"})
    assert env["ok"] is True, env  # the text differs from "2": it is written
    assert "<TW_FrameMode>2.0</TW_FrameMode>" in current_xml(seg_a(rig))


def test_old_work_directories_are_removed_when_a_new_one_is_made(
    rig: Rig, xml: XmlBehaviour
) -> None:
    root = work_root(rig)
    root.mkdir(parents=True, mode=0o700)
    old_time = time.time() - (setup_edit.WORKDIR_MAX_AGE_DAYS + 1) * 86400
    made = {}
    for name, age in (
        ("s_aaaaaaaaaaaa", old_time),
        ("s_bbbbbbbbbbbb", time.time()),
        ("keep_me", old_time),
    ):
        directory = root / name
        directory.mkdir(mode=0o700)
        (directory / "file").write_text("x")
        os.utime(directory / "file", (age, age))
        os.utime(directory, (age, age))
        made[name] = directory
    give_timewarp(seg_a(rig))
    assert edit(rig, {"TW_FrameMode": 0})["ok"] is True
    assert not made["s_aaaaaaaaaaaa"].exists()
    assert made["s_bbbbbbbbbbbb"].exists() and made["keep_me"].exists()


def test_a_value_longer_than_the_limit_is_refused_and_nothing_is_sent(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    env = edit(rig, {"TW_FrameMode": "x" * (setup_edit.SETUP_MAX_VALUE_LENGTH + 1)})
    assert env["errorCode"] == "INVALID_ARGS" and rig.exec.calls == []


def test_the_work_directory_follows_the_journal_directory_the_runtime_was_made_with(
    rig: Rig, xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(rig))
    edit(rig, {"TW_FrameMode": 0}, keep_files=True)
    assert os.listdir(work_root(rig))
    assert str(rig.journal.directory) in " ".join(c.code for c in rig.exec.calls)
