"""Tests `segment.read_effect_setup` and `node.read_setup`: the value of tags of a setup file, read
from a copy of the setup saved into a work directory that is removed before the call ends.

Run through the real Runtime over the fake Flame tree with the fakes of setup_xml_helpers, whose
setups are text made of tags. The real Flame is checked by scripts/p5_setup_edit_e2e.py."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyNode, PySegment, PyTimelineFX
from flame_mcp import execution, setup_edit
from flame_mcp.ops._setup_edit import work_root
from journal_test_helpers import BG1, Rig, make_rig
from op_test_helpers import run_op
from p32b_helpers import make_p32b_rig
from p33_helpers import a_seg, seg_a, sel_at
from setup_xml_helpers import DEFAULT_XML, XmlBehaviour, install_effect_xml, install_node_xml
from timeline_p32a_helpers import checkpoints, library_copy, seg_sel, wt

SEG_READ = "segment.read_effect_setup"
NODE_READ = "node.read_setup"
TW = "Timewarp"
EXPO = {"kind": "node", "batch": BG1, "name": "exposure1"}


@pytest.fixture
def seg_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


@pytest.fixture
def node_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


@pytest.fixture
def seg_xml(monkeypatch: pytest.MonkeyPatch) -> XmlBehaviour:
    return install_effect_xml(monkeypatch)


@pytest.fixture
def node_xml(monkeypatch: pytest.MonkeyPatch) -> XmlBehaviour:
    return install_node_xml(monkeypatch)


def give_timewarp(seg: PySegment, text: str = DEFAULT_XML) -> PyTimelineFX:
    fx = seg.create_effect(TW)
    fx.hidden["xml"] = text
    return fx


def read_seg(rig: Rig, tags: list[str], selector: dict[str, Any] | None = None) -> Any:
    return rig.do(SEG_READ, {"target": selector or a_seg(), "type": TW, "tags": tags})


def read_node(rig: Rig, tags: list[str], target: dict[str, Any] = EXPO) -> Any:
    return rig.do(NODE_READ, {"target": target, "tags": tags})


def leftovers(rig: Rig) -> list[str]:
    root = Path(rig.journal.directory) / "setups"
    return sorted(str(p) for p in root.rglob("*")) if root.exists() else []


def untouched(rig: Rig) -> bool:
    return rig.records() == [] and not [
        c for c in rig.exec.calls if (c.label or "").endswith("checkpoint")
    ]


# -- registration -------------------------------------------------------------------------------


def test_the_segment_op_is_a_read_with_no_undo_and_takes_target_type_and_tags(
    seg_rig: Rig,
) -> None:
    op = seg_rig.registry.get(SEG_READ)
    assert op is not None and op.checkpoint is None
    assert (op.category, op.effect, op.undo) == ("segment", "read", "none")
    assert op.accepts == ("segment",)
    assert [(p.name, p.type, p.required) for p in op.params] == [
        ("target", "selector", True),
        ("type", "string", True),
        ("tags", "array", True),
    ]
    assert op.params[2].item_type == "string"
    assert "PyTimelineFX.save_setup" in op.requires and "PySegment.effects" in op.requires
    assert "PyTimelineFX.load_setup" not in op.requires


def test_the_node_op_is_a_read_with_no_undo_and_takes_target_and_tags(node_rig: Rig) -> None:
    op = node_rig.registry.get(NODE_READ)
    assert op is not None and op.checkpoint is None
    assert (op.category, op.effect, op.undo) == ("node", "read", "none")
    assert op.accepts == ("node",)
    assert [(p.name, p.type, p.required) for p in op.params] == [
        ("target", "selector", True),
        ("tags", "array", True),
    ]
    assert op.params[1].item_type == "string"
    assert "PyNode.save_node_setup" in op.requires and "PyNode.load_node_setup" not in op.requires


@pytest.mark.parametrize("name", [SEG_READ, NODE_READ])
def test_the_description_starts_with_a_summary_and_names_the_rules(seg_rig: Rig, name: str) -> None:
    op = seg_rig.registry.get(name)
    assert op is not None
    assert op.description.isascii()
    for phrase in (
        "<TAG>value</TAG>",
        "result.problems",
        "result.fileOf",
        "INVALID_ARGS",
        "s_<12 hex>",
        "removed",
        "TW_FrameMode",
    ):
        assert phrase in op.description, phrase
    assert str(setup_edit.SETUP_MAX_VALUES) in op.description


@pytest.mark.parametrize(
    ("name", "args"),
    [
        (SEG_READ, {"target": {"kind": "segment"}, "type": TW, "tags": ["TW_FrameMode"]}),
        (NODE_READ, {"target": EXPO, "tags": ["Red"]}),
    ],
)
def test_the_generated_code_compiles_and_works_in_the_work_root(
    seg_rig: Rig, name: str, args: dict[str, Any]
) -> None:
    op = seg_rig.registry.get(name)
    assert op is not None
    args = dict(args)
    if name == SEG_READ:
        args["target"] = a_seg()
    code = execution.build_exec_code(op, args)
    compile(code, "<read>", "exec")
    call = (
        "_te_read_setup(target, target__parent, 'Timewarp', "
        if name == SEG_READ
        else ("_se_node_read(target__batch, target, ")
    )
    assert call + repr(args["tags"]) + ", " + repr(work_root()) + ")" in code


# -- the refusal before anything is sent --------------------------------------------------------


@pytest.mark.parametrize(
    "tags",
    [["a<b"], ["a b"], ["1A"], [""], ["A", "A"], [], [f"T{i}" for i in range(21)]],
)
def test_bad_tags_are_invalid_args_and_nothing_is_sent_to_flame(
    seg_rig: Rig, seg_xml: XmlBehaviour, tags: list[Any]
) -> None:
    env = read_seg(seg_rig, tags)
    assert env["errorCode"] == "INVALID_ARGS", env
    assert env["error"].startswith("nothing was sent to Flame:")
    assert env["result"]["ran"] is False and env["result"]["problems"]
    assert seg_rig.exec.calls == [] and seg_xml.saved == []


def test_the_node_op_refuses_bad_tags_the_same_way(node_rig: Rig, node_xml: XmlBehaviour) -> None:
    env = read_node(node_rig, ["Red", "Red"])
    assert env["errorCode"] == "INVALID_ARGS"
    assert env["result"]["problems"] == ["tag 'Red' is listed twice"]
    assert node_rig.exec.calls == [] and node_xml.saved == []


def test_the_tags_are_checked_inside_flame_too_when_the_precheck_is_bypassed(
    seg_rig: Rig, seg_xml: XmlBehaviour, node_rig: Rig, node_xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(seg_rig))
    for tags in (["a<b"], [], ["A", "A"]):
        got = run_op(
            seg_rig.registry, SEG_READ, {"target": a_seg(), "type": TW, "tags": tags}, seg_rig.flame
        )
        assert got["opErrorCode"] == "INVALID_ARGS", (tags, got["opError"])
        got = run_op(node_rig.registry, NODE_READ, {"target": EXPO, "tags": tags}, node_rig.flame)
        assert got["opErrorCode"] == "INVALID_ARGS", (tags, got["opError"])
    assert seg_xml.saved == [] and node_xml.saved == []


# -- segment.read_effect_setup ------------------------------------------------------------------


def test_the_value_of_a_tag_is_read_as_the_text_of_the_file(
    seg_rig: Rig, seg_xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["TW_FrameMode", "TW_Bypass"])
    assert env["ok"] is True, env
    result = env["result"]
    assert result["type"] == TW and result["requestedType"] == TW
    assert result["values"] == {"TW_FrameMode": "2", "TW_Bypass": "False"}
    assert result["problems"] == {}
    assert result["tagCounts"] == {"TW_FrameMode": 1, "TW_Bypass": 1}
    assert result["fileOf"] == {"TW_FrameMode": ".timewarp_node", "TW_Bypass": ".timewarp_node"}
    assert result["savedFiles"] == [".timewarp_node"] and result["notText"] == []
    assert "workDir" not in result and "files" not in result and "cleanupError" not in result


def test_a_tag_that_cannot_be_read_is_null_with_the_reason_and_the_call_is_ok(
    seg_rig: Rig, seg_xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["TW_FrameMode", "TW_Nope", "Frame", "TW_Speed"])
    assert env["ok"] is True, env
    result = env["result"]
    assert result["values"] == {
        "TW_FrameMode": "2",
        "TW_Nope": None,
        "Frame": None,
        "TW_Speed": None,
    }
    assert list(result["problems"]) == ["TW_Nope", "Frame", "TW_Speed"]
    assert "not in the setup" in result["problems"]["TW_Nope"]
    assert "2 times" in result["problems"]["Frame"]
    assert "child elements" in result["problems"]["TW_Speed"]
    assert result["tagCounts"] == {"TW_FrameMode": 1, "TW_Nope": 0, "Frame": 2, "TW_Speed": 1}


def test_the_project_is_not_changed_and_no_journal_record_or_checkpoint_is_made(
    seg_rig: Rig, seg_xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["TW_FrameMode"])
    assert env["ok"] is True and not env.get("journal"), env
    assert untouched(seg_rig) and checkpoints(seg_rig) == [] and seg_xml.loads == 0


def test_the_work_directory_is_removed_after_a_read(seg_rig: Rig, seg_xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(seg_rig))
    assert read_seg(seg_rig, ["TW_FrameMode"])["ok"] is True
    assert seg_xml.saved and not Path(seg_xml.saved[0]).exists()
    assert leftovers(seg_rig) == []


def test_the_work_directory_is_private_while_it_exists(
    seg_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[int] = []
    install_effect_xml(monkeypatch)
    inner = PyTimelineFX.save_setup

    def save(self: PyTimelineFX, path: str) -> bool:
        modes.append(os.stat(os.path.dirname(path)).st_mode & 0o777)
        return inner(self, path)

    monkeypatch.setattr(PyTimelineFX, "save_setup", save)
    give_timewarp(seg_a(seg_rig))
    assert read_seg(seg_rig, ["TW_FrameMode"])["ok"] is True
    assert modes == [0o700] and leftovers(seg_rig) == []


def test_a_setup_of_several_files_is_counted_over_all_of_them(
    seg_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_effect_xml(monkeypatch)

    def save(self: PyTimelineFX, path: str) -> bool:
        Path(path + ".timewarp_node").write_text("<A><Mode>1</Mode><Own>5</Own></A>")
        Path(path + ".other_node").write_text("<B><Mode>2</Mode></B>")
        return True

    monkeypatch.setattr(PyTimelineFX, "save_setup", save)
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["Mode", "Own"])
    assert env["ok"] is True, env
    result = env["result"]
    assert result["savedFiles"] == [".other_node", ".timewarp_node"]
    assert result["values"] == {"Mode": None, "Own": "5"}
    assert result["tagCounts"] == {"Mode": 2, "Own": 1}
    assert result["fileOf"] == {"Mode": None, "Own": ".timewarp_node"}
    assert "2 times" in result["problems"]["Mode"] and leftovers(seg_rig) == []


def test_a_file_that_is_not_text_is_named_and_the_others_are_read(
    seg_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyTimelineFX, path: str) -> bool:
        Path(path + ".timewarp_node").write_text("<A><Mode>1</Mode></A>")
        Path(path + ".blob").write_bytes(b"\xff\xfe\x00")
        return True

    monkeypatch.setattr(PyTimelineFX, "save_setup", save)
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["Mode", "Missing"])
    assert env["ok"] is True, env
    result = env["result"]
    assert result["notText"] == [".blob"] and result["savedFiles"] == [".blob", ".timewarp_node"]
    assert result["values"] == {"Mode": "1", "Missing": None}
    assert result["problems"]["Missing"] == (
        "the tag is not in the setup (files not read as text: .blob)"
    )
    assert leftovers(seg_rig) == []


def test_a_setup_none_of_whose_files_is_text_is_precondition_unmet(
    seg_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyTimelineFX, path: str) -> bool:
        Path(path + ".timewarp_node").write_bytes(b"<A><B>1</B>\xff\xfe</A>")
        return True

    monkeypatch.setattr(PyTimelineFX, "save_setup", save)
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["B"])
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "UTF-8" in env["error"] and env["result"]["notText"] == [".timewarp_node"]
    assert leftovers(seg_rig) == []


def test_a_save_that_writes_no_file_is_flame_api_error_not_silent_noop(
    seg_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PyTimelineFX, "save_setup", lambda self, path: True)
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["TW_FrameMode"])
    assert env["errorCode"] == "FLAME_API_ERROR", env
    assert "has no new or changed file" in env["error"]
    assert leftovers(seg_rig) == []


def test_a_save_that_answers_false_is_flame_api_error_and_the_directory_is_removed(
    seg_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyTimelineFX, path: str) -> bool:
        Path(path + ".timewarp_node").write_text(DEFAULT_XML)
        return False

    monkeypatch.setattr(PyTimelineFX, "save_setup", save)
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["TW_FrameMode"])
    assert env["errorCode"] == "FLAME_API_ERROR" and "False" in env["error"], env
    assert leftovers(seg_rig) == []


def test_a_save_that_raises_leaves_no_work_directory(
    seg_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyTimelineFX, path: str) -> bool:
        Path(path + ".timewarp_node").write_text("<A/>")
        raise RuntimeError("the setup could not be saved")

    monkeypatch.setattr(PyTimelineFX, "save_setup", save)
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["TW_FrameMode"])
    assert env["ok"] is False and "could not be saved" in str(env["error"]), env
    assert leftovers(seg_rig) == []


def test_a_directory_that_cannot_be_removed_is_named_in_cleanup_error(
    seg_rig: Rig, seg_xml: XmlBehaviour, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.rmdir

    def refuse(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.path.basename(str(path)).startswith("s_"):
            raise PermissionError("not allowed")
        real(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", refuse)
    give_timewarp(seg_a(seg_rig))
    env = read_seg(seg_rig, ["TW_FrameMode"])
    assert env["ok"] is True and env["result"]["values"] == {"TW_FrameMode": "2"}
    assert "PermissionError" in env["result"]["cleanupError"]


def refuse_reading_work_files(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.open

    def refuse(file: Any, *args: Any, **kwargs: Any) -> Any:
        if "/setups/s_" in str(file):
            raise OSError("cannot open the work file")
        return real(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refuse)


def test_a_failure_while_reading_the_files_leaves_no_work_directory(
    seg_rig: Rig, seg_xml: XmlBehaviour, monkeypatch: pytest.MonkeyPatch
) -> None:
    give_timewarp(seg_a(seg_rig))
    refuse_reading_work_files(monkeypatch)
    env = read_seg(seg_rig, ["TW_FrameMode"])
    assert env["ok"] is False and "cannot open the work file" in str(env["error"]), env
    assert seg_xml.saved and leftovers(seg_rig) == []


def test_an_effect_the_segment_lacks_and_a_gap_are_refused_without_a_work_directory(
    seg_rig: Rig, seg_xml: XmlBehaviour
) -> None:
    missing = read_seg(seg_rig, ["TW_FrameMode"])
    assert missing["errorCode"] == "PRECONDITION_UNMET" and "no effect of type" in missing["error"]
    gap = read_seg(seg_rig, ["TW_FrameMode"], selector=sel_at(0))
    assert gap["errorCode"] == "PRECONDITION_UNMET" and "Gap" in gap["error"]
    assert seg_xml.saved == [] and leftovers(seg_rig) == []


def test_a_segment_of_a_library_sequence_can_be_read(seg_rig: Rig, seg_xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(seg_rig))
    copy = library_copy(seg_rig)
    selector = seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1)
    env = read_seg(seg_rig, ["TW_FrameMode"], selector=selector)
    assert env["ok"] is True and env["result"]["values"] == {"TW_FrameMode": "2"}, env


def test_the_type_is_matched_ignoring_case_and_both_spellings_are_in_the_result(
    seg_rig: Rig, seg_xml: XmlBehaviour
) -> None:
    give_timewarp(seg_a(seg_rig))
    env = seg_rig.do(SEG_READ, {"target": a_seg(), "type": "timewarp", "tags": ["TW_FrameMode"]})
    assert env["ok"] is True and env["result"]["requestedType"] == "timewarp"
    assert env["result"]["type"] == TW


def test_a_plan_runs_the_read_like_any_other_read_step(seg_rig: Rig, seg_xml: XmlBehaviour) -> None:
    give_timewarp(seg_a(seg_rig))
    steps = [{"op": SEG_READ, "args": {"target": a_seg(), "type": TW, "tags": ["TW_FrameMode"]}}]
    env = seg_rig.do("plan.run", {"steps": steps})
    assert env["ok"] is True, env
    assert env["result"]["steps"][0]["result"]["values"] == {"TW_FrameMode": "2"}
    assert leftovers(seg_rig) == []


# -- node.read_setup ----------------------------------------------------------------------------


def test_the_value_of_a_node_tag_is_read_and_the_node_is_named(
    node_rig: Rig, node_xml: XmlBehaviour
) -> None:
    env = read_node(node_rig, ["Red", "Mode", "Nope"])
    assert env["ok"] is True, env
    result = env["result"]
    assert result["values"] == {"Red": "0.0", "Mode": "Auto", "Nope": None}
    assert result["problems"] == {"Nope": "the tag is not in the setup"}
    assert result["tagCounts"] == {"Red": 1, "Mode": 1, "Nope": 0}
    assert result["fileOf"] == {"Red": ".exposure_node", "Mode": ".exposure_node", "Nope": None}
    assert result["savedFiles"] == [".exposure_node"] and result["notText"] == []
    assert result["node"]["name"] == "exposure1"
    assert "workDir" not in result and "files" not in result
    assert untouched(node_rig) and node_xml.loads == 0 and leftovers(node_rig) == []
    assert node_xml.saved and not Path(node_xml.saved[0]).exists()


def test_a_node_of_several_files_is_read_across_all_of_them(
    node_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyNode, path: str) -> bool:
        Path(path + ".output_node").write_text("<O><UseLuts>True</UseLuts></O>")
        Path(path + ".lut_node").write_text("<L><P>137</P><UseLuts>x</UseLuts></L>")
        return True

    monkeypatch.setattr(PyNode, "save_node_setup", save)
    env = read_node(node_rig, ["P", "UseLuts"])
    assert env["ok"] is True, env
    result = env["result"]
    assert result["values"] == {"P": "137", "UseLuts": None}
    assert result["fileOf"] == {"P": ".lut_node", "UseLuts": None}
    assert result["savedFiles"] == [".lut_node", ".output_node"]
    assert leftovers(node_rig) == []


def test_a_node_setup_whose_type_file_is_not_found_is_still_read(
    node_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def save(self: PyNode, path: str) -> bool:
        calls.append(path)
        Path(path + ".aaa_node").write_text("<A><X>1</X></A>")
        Path(path + ".bbb_node").write_text("<B><Y>2</Y></B>")
        return True

    monkeypatch.setattr(PyNode, "save_node_setup", save)
    env = read_node(node_rig, ["X", "Y"])
    assert env["ok"] is True and env["result"]["values"] == {"X": "1", "Y": "2"}, env
    assert env["result"]["fileOf"] == {"X": ".aaa_node", "Y": ".bbb_node"}
    assert leftovers(node_rig) == [] and len(calls) == 1


def test_a_node_file_that_is_not_text_is_named(
    node_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyNode, path: str) -> bool:
        Path(path + ".exposure_node").write_text("<E><Red>1</Red></E>")
        Path(path + ".blob").write_bytes(b"\xff\xfe")
        return True

    monkeypatch.setattr(PyNode, "save_node_setup", save)
    env = read_node(node_rig, ["Red", "Gone"])
    assert env["ok"] is True and env["result"]["notText"] == [".blob"], env
    assert env["result"]["values"] == {"Red": "1", "Gone": None}
    assert "files not read as text: .blob" in env["result"]["problems"]["Gone"]


def test_a_node_setup_none_of_whose_files_is_text_is_precondition_unmet(
    node_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyNode, path: str) -> bool:
        Path(path + ".exposure_node").write_bytes(b"\xff\xfe")
        return True

    monkeypatch.setattr(PyNode, "save_node_setup", save)
    env = read_node(node_rig, ["Red"])
    assert env["errorCode"] == "PRECONDITION_UNMET" and "UTF-8" in env["error"], env
    assert leftovers(node_rig) == []


def test_a_node_save_that_writes_no_file_is_flame_api_error(
    node_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PyNode, "save_node_setup", lambda self, path: True)
    env = read_node(node_rig, ["Red"])
    assert env["errorCode"] == "FLAME_API_ERROR" and "wrote no file" in env["error"], env
    assert leftovers(node_rig) == []


def test_a_node_save_that_answers_false_is_flame_api_error(
    node_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyNode, path: str) -> bool:
        Path(path + ".exposure_node").write_text("<E><Red>1</Red></E>")
        return False

    monkeypatch.setattr(PyNode, "save_node_setup", save)
    env = read_node(node_rig, ["Red"])
    assert env["errorCode"] == "FLAME_API_ERROR" and "returned" in env["error"], env
    assert leftovers(node_rig) == []


def test_a_node_save_that_raises_leaves_no_work_directory(
    node_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def save(self: PyNode, path: str) -> bool:
        Path(path + ".exposure_node").write_text("<E/>")
        raise RuntimeError("the node setup could not be saved")

    monkeypatch.setattr(PyNode, "save_node_setup", save)
    env = read_node(node_rig, ["Red"])
    assert env["ok"] is False and "could not be saved" in str(env["error"]), env
    assert leftovers(node_rig) == []


def test_a_node_directory_that_cannot_be_removed_is_named_in_cleanup_error(
    node_rig: Rig, node_xml: XmlBehaviour, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.rmdir

    def refuse(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.path.basename(str(path)).startswith("s_"):
            raise PermissionError("not allowed")
        real(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", refuse)
    env = read_node(node_rig, ["Red"])
    assert env["ok"] is True and env["result"]["values"] == {"Red": "0.0"}
    assert "PermissionError" in env["result"]["cleanupError"]


def test_a_failure_while_reading_the_node_files_leaves_no_work_directory(
    node_rig: Rig, node_xml: XmlBehaviour, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse_reading_work_files(monkeypatch)
    env = read_node(node_rig, ["Red"])
    assert env["ok"] is False and "cannot open the work file" in str(env["error"]), env
    assert node_xml.saved and leftovers(node_rig) == []


def test_a_node_that_is_not_there_is_selector_empty_and_sends_no_save(
    node_rig: Rig, node_xml: XmlBehaviour
) -> None:
    env = read_node(node_rig, ["Red"], {"kind": "node", "batch": BG1, "name": "nope"})
    assert env["errorCode"] == "SELECTOR_EMPTY" and node_xml.saved == []
