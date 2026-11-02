"""`batch.delete`: inspect, save the setup, journal, delete.

The fake follows what was measured on 2025.2.4 for `PyBatch.save_setup` and `flame.delete` of a
Batch group; which groups real Flame lets go (the current one, an open one, the last one) is what
`_bd_blocked` guesses, measured on real Flame and not here."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr, PyClip
from flame_mcp import server as server_mod
from flame_mcp import setup_work
from flame_mcp.batch_delete_prelude import BATCH_DELETE_HELPER
from journal_test_helpers import Rig, make_rig

DELETE = "batch.delete"
BG1 = {"kind": "batch", "name": "BG1"}
BG2 = {"kind": "batch", "name": "BG2"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    monkeypatch.setattr(setup_work, "_journal_directory", tmp_path / "journal")
    made = make_rig(monkeypatch, tmp_path)
    for key in ("bg1", "bg2"):
        made.handles[key].opened = FakeAttr(False)
    return made


def groups(rig: Rig) -> list[str]:
    desktop = rig.flame.projects.current_project.current_workspace.desktop
    return [str(g.name.get_value()) for g in desktop.batch_groups]


def intents(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "intent"]


def test_a_group_that_is_not_current_is_deleted_after_its_setup_is_saved(rig: Rig) -> None:
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["deleted"] is True and result["wasCurrent"] is False
    assert result["nodeCount"] == 4
    assert result["content"] == {"schematicReelEntries": 0, "shelfReelEntries": 0, "iterations": 0}
    assert groups(rig) == ["BG2"]
    saved = Path(result["setupPath"])
    assert saved.is_file() and saved.stat().st_size == result["setupSize"] > 0
    assert saved.suffix == ".batch" and saved.parent.name == "batch_delete"
    assert len(rig.handles["bg1"].save_calls) == 1
    assert env["journal"]["undo"] == "unsupported" and env["journal"]["revert"] is None


def test_the_journal_records_who_when_which_uid_and_the_setup_path(rig: Rig) -> None:
    env = rig.do(DELETE, {"target": BG1})
    (intent,) = intents(rig)
    assert intent["kind"] == "batch_delete" and intent["uid"] == "uid-b1"
    assert intent["name"] == "BG1" and intent["setupPath"] == env["result"]["setupPath"]
    assert intent["ts"] and intent["pid"] and "user" in intent
    outcome = next(r for r in rig.records() if r.get("type") == "outcome")
    assert outcome["ok"] is True and outcome["verdict"] == "landed"


def test_a_refusal_saves_and_journals_nothing(rig: Rig) -> None:
    env = rig.do(DELETE, {"target": BG2})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert groups(rig) == ["BG1", "BG2"]
    assert rig.handles["bg2"].save_calls == [] and rig.records() == []


def test_allow_current_deletes_the_current_group_too(rig: Rig) -> None:
    env = rig.do(DELETE, {"target": BG2, "allow_current": True})
    assert env["ok"] is True, env
    assert env["result"]["wasCurrent"] is True
    assert groups(rig) == ["BG1"]


def test_an_open_group_is_refused_unless_allow_current(rig: Rig) -> None:
    rig.handles["bg1"].opened = FakeAttr(True)
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is False and "open" in env["error"]
    assert rig.do(DELETE, {"target": BG1, "allow_current": True})["ok"] is True


def test_a_group_whose_openness_cannot_be_read_is_refused(rig: Rig) -> None:
    del rig.handles["bg1"].opened
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "could not be read" in env["error"]
    assert groups(rig) == ["BG1", "BG2"] and rig.records() == []


def test_a_current_group_that_cannot_be_read_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def get_value(self) -> Any:
            raise RuntimeError("no current")

    desktop = rig.flame.projects.current_project.current_workspace.desktop
    monkeypatch.setattr(desktop, "current_batch_group", Broken())
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is False and "could not be read" in env["error"]
    assert groups(rig) == ["BG1", "BG2"]


def test_the_only_group_left_is_never_deleted(rig: Rig) -> None:
    assert rig.do(DELETE, {"target": BG1})["ok"] is True
    env = rig.do(DELETE, {"target": BG2, "allow_current": True})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert groups(rig) == ["BG2"]


def test_a_group_with_reel_content_is_refused_and_counted(rig: Rig) -> None:
    reel = rig.handles["bg1"].create_reel("Sch A")
    reel._children.append(PyClip("c", "/wt/c"))
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert env["result"]["content"]["schematicReelEntries"] == 1
    assert groups(rig) == ["BG1", "BG2"] and rig.records() == []


def test_a_group_with_iterations_is_refused_and_allow_content_loss_deletes_it(rig: Rig) -> None:
    rig.handles["bg1"].iterate()
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is False and env["result"]["content"]["iterations"] == 1
    again = rig.do(DELETE, {"target": BG1, "allow_content_loss": True})
    assert again["ok"] is True and again["result"]["content"]["iterations"] == 1


def test_expect_name_protects_a_group_selected_by_uid(rig: Rig) -> None:
    by_uid = {"kind": "batch", "uid": "uid-b1"}
    env = rig.do(DELETE, {"target": by_uid, "expect_name": "WRONG"})
    assert env["ok"] is False and env["errorCode"] == "CONFLICT"
    assert groups(rig) == ["BG1", "BG2"] and rig.records() == []
    assert rig.do(DELETE, {"target": by_uid, "expect_name": "BG1"})["ok"] is True


def test_nothing_is_deleted_when_the_setup_cannot_be_saved(rig: Rig) -> None:
    rig.handles["bg1"].save_writes_nothing = True
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is False and "nothing was deleted" in env["error"]
    assert groups(rig) == ["BG1", "BG2"] and intents(rig) == []


def test_a_group_that_stays_after_the_delete_is_a_silent_noop(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rig.flame, "delete", lambda obj, confirm=True: True)
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP"
    assert Path(env["result"]["setupPath"]).is_file()
    outcome = next(r for r in rig.records() if r.get("type") == "outcome")
    assert outcome["verdict"] == "not_landed"


def test_a_delete_that_raises_but_leaves_the_group_gone_is_ok_with_a_warning(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rig.flame.delete

    def noisy(obj: Any, confirm: bool = True) -> Any:
        real(obj, confirm)
        raise RuntimeError("late complaint")

    monkeypatch.setattr(rig.flame, "delete", noisy)
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is True, env
    assert env["result"]["deleted"] is True
    assert any("late complaint" in w for w in env["result"]["warnings"])
    assert any("late complaint" in w for w in env["degraded"])
    assert Path(env["result"]["setupPath"]).is_file()
    assert groups(rig) == ["BG2"]


def test_a_delete_that_raises_and_leaves_the_group_is_an_error_with_the_setup_path(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(obj: Any, confirm: bool = True) -> Any:
        raise RuntimeError("cannot")

    monkeypatch.setattr(rig.flame, "delete", refuse)
    env = rig.do(DELETE, {"target": BG1})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert Path(env["result"]["setupPath"]).is_file()


def test_journal_check_decides_an_open_intent_by_the_uid(rig: Rig) -> None:
    env = rig.do(DELETE, {"target": BG1})
    group = env["journal"]["group"]
    rig.journal.append(
        {
            "type": "intent",
            "id": "i_open_gone",
            "group": "g_open",
            "ts": "2026-10-31T00:00:00Z",
            "op": DELETE,
            "kind": "batch_delete",
            "args": {},
            "target": {"kind": "batch", "uid": "uid-b1"},
            "uid": "uid-b1",
            "name": "BG1",
            "setupPath": "/x.batch",
        }
    )
    checked = rig.do("journal.check", {"group": "g_open"})
    assert checked["ok"] is True, checked
    assert "landed" in str(checked["result"]) and group


def test_journal_revert_of_a_delete_says_there_is_nothing_to_revert(rig: Rig) -> None:
    env = rig.do(DELETE, {"target": BG1})
    reverted = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert reverted["ok"] is False and "load_setup" in reverted["error"]


def test_the_confirmation_sentence_of_the_server_names_it() -> None:
    assert "batch.delete" in " ".join(server_mod.INSTRUCTIONS.split())


def test_the_setup_file_name_holds_the_whole_uid() -> None:
    assert "isalnum())[:" not in BATCH_DELETE_HELPER
