"""Tests `node.edit_setup`: one value of a Batch node's setup file replaced, loaded, saved again and
read back, behind a Batch checkpoint.

Run through the real Runtime over the fake Flame tree; the fake node writes its `exposure_red` and
`bypass` as tags (setup_xml_helpers), so a restore of the group's checkpoint puts the setup back."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyNode
from flame_mcp import setup_edit
from journal_test_helpers import BG1, Rig, make_rig
from setup_xml_helpers import XmlBehaviour, install_node_xml

EDIT = "node.edit_setup"
EXPO = {"kind": "node", "batch": BG1, "name": "exposure1"}
BLUR = {"kind": "node", "batch": BG1, "name": "blur1"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


@pytest.fixture
def xml(monkeypatch: pytest.MonkeyPatch) -> XmlBehaviour:
    return install_node_xml(monkeypatch)


def expo(rig: Rig) -> PyNode:
    found: PyNode = next(n for n in rig.handles["bg1"].nodes if n.name.get_value() == "exposure1")
    return found


def red(rig: Rig) -> Any:
    return expo(rig).exposure_red.get_value()


def edit(rig: Rig, values: dict[str, Any], target: dict[str, Any] = EXPO, **extra: Any) -> Any:
    return rig.do(EDIT, {"target": target, "values": values, **extra})


def leftovers(rig: Rig) -> list[str]:
    root = Path(rig.journal.directory) / "setups"
    return sorted(str(p) for p in root.rglob("*")) if root.exists() else []


def untouched(rig: Rig) -> bool:
    return rig.records() == [] and not [c for c in rig.exec.calls if c.label == "batch.checkpoint"]


def test_the_value_is_replaced_loaded_and_read_back(rig: Rig, xml: XmlBehaviour) -> None:
    env = edit(rig, {"Red": 0.5})
    assert env["ok"] is True, env
    assert red(rig) == 0.5
    result = env["result"]
    assert result["tags"] == {"Red": {"before": "0.0", "requested": "0.5", "after": "0.5"}}
    assert result["unchanged"] == [] and result["tagCounts"] == {"Red": 1}
    assert result["node"]["name"] == "exposure1" and result["filesKept"] is False
    assert "files" not in result
    assert env["journal"]["revert"]["op"] == "journal.revert" and env["degraded"] == []
    assert leftovers(rig) == []


def test_the_intent_is_journaled_and_a_revert_puts_the_group_back(
    rig: Rig, xml: XmlBehaviour
) -> None:
    env = edit(rig, {"Red": 0.5})
    types = [r["type"] for r in rig.records()]
    assert types[:2] == ["checkpoint", "intent"] and types[-1] == "outcome"
    assert rig.records()[1]["op"] == EDIT
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert red(rig) == 0.0


def test_keep_files_leaves_private_files(rig: Rig, xml: XmlBehaviour) -> None:
    env = edit(rig, {"Red": 0.5}, keep_files=True)
    assert env["ok"] is True, env
    files = env["result"]["files"]
    root = Path(rig.journal.directory) / "setups"
    for key in ("original", "edited", "readback"):
        assert files[key].startswith(str(root)) and files[key].endswith(".exposure_node")
        assert stat.S_IMODE(os.stat(files[key]).st_mode) == 0o600, key
    assert stat.S_IMODE(os.stat(Path(files["original"]).parent).st_mode) == 0o700
    assert Path(files["readback"]).read_text() == Path(files["edited"]).read_text()
    assert "<Red>0.5</Red>" in Path(files["edited"]).read_text()
    assert env["result"]["filesKept"] is True


def test_a_bool_is_written_as_true_or_false(rig: Rig, xml: XmlBehaviour) -> None:
    env = edit(rig, {"Bypass": True})
    assert env["ok"] is True, env
    assert expo(rig).bypass.get_value() is True
    assert env["result"]["tags"]["Bypass"]["requested"] == "True"


def test_nothing_to_change_is_refused_before_the_checkpoint(rig: Rig, xml: XmlBehaviour) -> None:
    env = edit(rig, {"Red": 0.0, "Bypass": False})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "nothing to change" in env["error"]
    assert untouched(rig) and xml.loads == 0 and leftovers(rig) == []


def test_a_tag_that_is_not_there_is_refused_before_the_checkpoint(
    rig: Rig, xml: XmlBehaviour
) -> None:
    env = edit(rig, {"Red": 0.5, "Green": 1})
    assert env["errorCode"] == "PRECONDITION_UNMET", env
    assert env["result"]["tagCounts"] == {"Green": 0, "Red": 1}
    assert "not in the setup" in env["result"]["tagProblems"]["Green"]
    assert untouched(rig) and red(rig) == 0.0 and leftovers(rig) == []


@pytest.mark.parametrize("value", ["a<b", "a&b", 'a"b', "a\nb", "a\rb", "a\tb", "a>b"])
def test_a_value_that_could_change_the_structure_is_refused_and_nothing_is_sent(
    rig: Rig, xml: XmlBehaviour, value: str
) -> None:
    env = edit(rig, {"Red": value})
    assert env["errorCode"] == "INVALID_ARGS", env
    assert rig.exec.calls == [] and rig.records() == []


def test_a_tag_name_of_another_shape_is_refused_and_nothing_is_sent(
    rig: Rig, xml: XmlBehaviour
) -> None:
    env = edit(rig, {"a b": 1})
    assert env["errorCode"] == "INVALID_ARGS" and rig.exec.calls == []


def test_a_setup_of_several_files_is_refused(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_node_xml(monkeypatch, XmlBehaviour(files=6))
    env = edit(rig, {"Red": 0.5})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "6 files" in env["error"]
    assert len(env["result"]["savedFiles"]) == 6
    assert untouched(rig) and state.loads == 0 and leftovers(rig) == []


def test_a_node_that_is_not_there_is_refused_before_the_checkpoint(
    rig: Rig, xml: XmlBehaviour
) -> None:
    env = edit(rig, {"Red": 0.5}, target={"kind": "node", "batch": BG1, "name": "nope"})
    assert env["errorCode"] == "SELECTOR_EMPTY" and untouched(rig)


def test_a_load_flame_ignores_is_flame_api_error_with_the_values_it_left(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_xml(monkeypatch, XmlBehaviour(load="ignore"))
    env = edit(rig, {"Red": 0.5})
    assert env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["tags"]["Red"] == {"before": "0.0", "requested": "0.5", "after": "0.0"}
    assert env["result"]["mismatched"] == ["Red"] and env["result"]["filesKept"] is True
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_a_load_that_answers_false_or_raises_is_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_xml(monkeypatch, XmlBehaviour(load="false"))
    env = edit(rig, {"Red": 0.5})
    assert env["errorCode"] == "FLAME_API_ERROR" and env["result"]["filesKept"] is True


def test_a_readback_that_cannot_be_saved_is_flame_api_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_xml(monkeypatch, XmlBehaviour(save_nothing_after_load=True))
    env = edit(rig, {"Red": 0.5})
    assert env["errorCode"] == "FLAME_API_ERROR" and "read it back" in env["error"]
    assert red(rig) == 0.5


def test_a_plan_bundles_the_edit_with_a_node_create_into_one_checkpointed_write(
    rig: Rig, xml: XmlBehaviour
) -> None:
    steps = [
        {"op": "node.create", "args": {"target": BG1, "type": "Blur", "name": "b2"}},
        {"op": EDIT, "args": {"target": EXPO, "values": {"Red": 0.25}}},
    ]
    env = rig.do("plan.run", {"steps": steps})
    assert env["ok"] is True, env
    assert [s["ok"] for s in env["result"]["steps"]] == [True, True]
    assert env["result"]["batch"]["checkpoint"]
    assert [c.label for c in rig.exec.calls].count("batch.checkpoint") == 1
    assert red(rig) == 0.25
    out = rig.do("journal.revert", {"group": env["result"]["group"]})
    assert out["ok"] is True, out
    assert red(rig) == 0.0
    assert "b2" not in [str(n.name.get_value()) for n in rig.handles["bg1"].nodes]


def test_a_plan_summary_names_the_node(rig: Rig, xml: XmlBehaviour) -> None:
    steps = [{"op": EDIT, "args": {"target": EXPO, "values": {"Red": 0.25}}}]
    env = rig.do("plan.run", {"steps": steps, "result_detail": "summary"})
    assert env["ok"] is True, env
    assert env["result"]["steps"][0]["brief"]["node"]["name"] == "exposure1"


def test_the_op_is_declared_as_a_reversible_checkpointed_node_edit(rig: Rig) -> None:
    op = rig.registry.get(EDIT)
    assert op is not None and op.checkpoint is not None
    assert (op.category, op.effect, op.undo) == ("node", "reversible", "checkpoint")
    assert op.checkpoint.target_kind == "batch" and op.accepts == ("node",)
    assert [p.name for p in op.params] == ["target", "values", "keep_files"]
    assert "PRECONDITION_UNMET" in op.description and "nothing to change" in op.description


def test_a_successful_edit_says_it_ran_and_does_not_name_the_removed_files(
    rig: Rig, xml: XmlBehaviour
) -> None:
    env = edit(rig, {"Red": 0.5})
    assert env["result"]["ran"] is True
    assert "files" not in env["result"] and "workDir" not in env["result"]


def test_keep_files_keeps_the_work_files_of_a_refusal_before_the_load_too(
    rig: Rig, xml: XmlBehaviour
) -> None:
    env = edit(rig, {"Red": 0.0}, keep_files=True)
    assert env["errorCode"] == "PRECONDITION_UNMET" and env["result"]["filesKept"] is True
    original = Path(env["result"]["files"]["original"])
    assert original.exists() and stat.S_IMODE(os.stat(original).st_mode) == 0o600
    assert untouched(rig) and xml.loads == 0


def test_a_setup_of_several_files_keeps_flames_own_error_text_and_the_files_on_request(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_xml(monkeypatch, XmlBehaviour(files=2))
    env = edit(rig, {"Red": 0.5}, keep_files=True)
    assert env["errorCode"] == "PRECONDITION_UNMET" and "2 files" in env["error"]
    assert env["result"]["filesKept"] is True and len(env["result"]["savedFiles"]) == 2


def test_the_error_of_a_save_that_wrote_nothing_is_not_thrown_away(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def writes_nothing(self: PyNode, path: str) -> bool:
        return True

    monkeypatch.setattr(PyNode, "save_node_setup", writes_nothing)
    env = edit(rig, {"Red": 0.5})
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "wrote no file" in env["error"] and "save_node_setup" in env["error"]


def test_an_exception_of_the_load_is_reported_with_the_files_and_the_revert(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_xml(monkeypatch, XmlBehaviour(load="raise"))
    env = edit(rig, {"Red": 0.5})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "Could not load the node setup" in env["error"]
    assert env["result"]["ran"] is True and env["result"]["filesKept"] is True
    assert Path(env["result"]["files"]["edited"]).exists()
    assert env["journal"]["revert"]["op"] == "journal.revert"


def test_an_exception_of_the_readback_save_is_reported_with_the_files(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_xml(monkeypatch, XmlBehaviour(save_raises_after_load=True))
    env = edit(rig, {"Red": 0.5})
    assert env["errorCode"] == "FLAME_API_ERROR" and "could not be saved" in env["error"]
    assert env["result"]["ran"] is True and env["result"]["filesKept"] is True
    assert red(rig) == 0.5


def test_a_node_the_load_replaced_is_found_again_by_its_place_in_the_group(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_node_xml(monkeypatch, XmlBehaviour(replace_node_on_load=True))
    before = expo(rig)
    env = edit(rig, {"Red": 0.5})
    assert env["ok"] is True, env
    assert expo(rig) is not before and red(rig) == 0.5
    assert env["result"]["tags"]["Red"]["after"] == "0.5" and state.loads == 1


def test_a_number_written_another_way_is_not_written_back(rig: Rig, xml: XmlBehaviour) -> None:
    env = edit(rig, {"Red": 0, "Bypass": True})
    assert env["ok"] is True, env
    assert env["result"]["unchanged"] == ["Red"]


def test_the_values_are_checked_inside_flame_too_when_the_precheck_is_bypassed(
    rig: Rig, xml: XmlBehaviour
) -> None:
    from op_test_helpers import run_op

    for values in ({"Red": "0<"}, {"a b": 1}, {}, {"Red": "x" * 500}):
        got = run_op(rig.registry, EDIT, {"target": EXPO, "values": values}, rig.flame)
        assert got["opErrorCode"] == "INVALID_ARGS", (values, got["opError"])
    assert xml.saved == [] and red(rig) == 0.0


def test_old_work_directories_are_removed_when_a_new_one_is_made(
    rig: Rig, xml: XmlBehaviour
) -> None:
    root = Path(rig.journal.directory) / "setups"
    root.mkdir(parents=True, mode=0o700)
    old = root / "s_aaaaaaaaaaaa"
    old.mkdir(mode=0o700)
    aged = time.time() - (setup_edit.WORKDIR_MAX_AGE_DAYS + 1) * 86400
    os.utime(old, (aged, aged))
    assert edit(rig, {"Red": 0.5})["ok"] is True
    assert not old.exists()


@pytest.mark.parametrize("values", [{"kind": "node"}, {"kind": "node", "name": "zz"}])
def test_a_values_dict_that_looks_like_a_node_selector_is_not_one(
    rig: Rig, xml: XmlBehaviour, values: dict[str, Any]
) -> None:
    env = rig.do("plan.run", {"steps": [{"op": EDIT, "args": {"target": EXPO, "values": values}}]})
    assert "preResolution" not in (env.get("result") or {}), env
    assert "server bug" not in str(env.get("error")), env
    assert env["result"]["steps"][0]["op"] == EDIT
