"""Tests for projects where the Timeline moves to the new copy of a sequence.

In some projects the Timeline moves to the new copy at every `media_panel.copy` of
a sequence until the sequence has been opened through the API. Everything that copies a sequence --
the checkpoint behind every Timeline write (trim, plan.run, the checkpoint a revert takes first,
desktop.delete's stash), `desktop.copy`, `desktop.duplicate` -- reads what the Timeline shows
before and after and opens what was shown again. The fake reproduces the project with
`PyTimeline.copy_moves_display_until_open` (fake_media_tree.py). The shown sequence is set
directly (as the UI does): `PySequence.open()` is what stops the moving in that project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import ACTIVE_TIMELINE, PySequence
from journal_test_helpers import Rig
from media_p31b_helpers import add_seq
from p32b_helpers import DELETE, PLAN, S1, S2, make_p32b_rig, reel2, shown, uid, wt
from timeline_p32a_helpers import CHECKPOINT, TRIM, a_seg, checkpoints, seg_sel, trim


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def show_in_ui(rig: Rig, name: str | None) -> None:
    ACTIVE_TIMELINE[0].clip = rig.handles[name] if name is not None else None


def project_moves_display() -> None:
    ACTIVE_TIMELINE[0].copy_moves_display_until_open = True


def opened_by_api() -> bool:
    return ACTIVE_TIMELINE[0].opened_by_api


def copy_op(rig: Rig, source: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
    return rig.do("desktop.copy", {"sources": [source], "destination": destination})


def test_trim_leaves_the_timeline_on_the_target_when_the_project_moves_it_to_the_copy(
    rig: Rig,
) -> None:
    project_moves_display()
    show_in_ui(rig, "s1")
    env = trim(rig, a_seg(1))
    assert env["ok"] is True, env
    assert shown(rig) is rig.handles["s1"]
    assert opened_by_api() is True


def test_the_stash_result_and_the_checkpoint_record_say_what_was_put_back(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s1")
    env = rig.do(CHECKPOINT, {"target": S1})
    assert env["ok"] is True, env
    display = env["result"]["display"]
    assert display["shownBefore"]["uid"] == uid(rig.handles["s1"])
    assert display["shownAfterCopy"]["uid"] != display["shownBefore"]["uid"]
    assert display["reshown"] is True and display["note"] is None
    assert display["shownAfter"] == display["shownBefore"]
    assert shown(rig) is rig.handles["s1"]
    [record] = checkpoints(rig)
    assert record["display"] == display


def test_a_checkpointed_write_reports_the_display_of_its_checkpoint(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s1")
    env = trim(rig, a_seg(1))
    [record] = checkpoints(rig)
    assert record["display"]["reshown"] is True
    assert env["journal"]["display"] == record["display"]


def test_another_sequence_that_was_shown_stays_shown(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s2")
    env = trim(rig, a_seg(1))
    assert env["ok"] is True, env
    assert shown(rig) is rig.handles["s2"]
    [record] = checkpoints(rig)
    assert record["display"]["shownBefore"]["name"] == "S2"


def test_nothing_shown_before_means_nothing_is_opened(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, None)
    env = trim(rig, a_seg(1))
    assert env["ok"] is True, env
    [record] = checkpoints(rig)
    assert record["display"]["shownBefore"] is None and record["display"]["reshown"] is None
    assert opened_by_api() is False


def test_a_project_that_does_not_move_the_timeline_gets_no_extra_open(rig: Rig) -> None:
    show_in_ui(rig, "s1")
    env = trim(rig, a_seg(1))
    assert env["ok"] is True, env
    assert shown(rig) is rig.handles["s1"]
    [record] = checkpoints(rig)
    display = record["display"]
    assert display["reshown"] is None and display["note"] is None
    assert display["shownAfterCopy"] == display["shownBefore"] == display["shownAfter"]
    assert opened_by_api() is False


def test_plan_run_keeps_the_timeline_on_the_target(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s1")
    steps = [
        {"op": TRIM, "args": {"target": seg_sel(S1, 0, record_in=10), "edge": "head", "offset": 2}}
    ]
    env = rig.do(PLAN, {"steps": steps})
    assert env["ok"] is True, env
    assert shown(rig) is rig.handles["s1"]


def test_the_revert_keeps_the_timeline_where_it_was(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s2")
    env = trim(rig, a_seg(1))
    assert env["ok"] is True, env
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert shown(rig) is rig.handles["s2"]


def test_desktop_delete_stashes_without_moving_the_timeline(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s2")
    env = rig.do(DELETE, {"targets": [S1]})
    assert env["ok"] is True, env
    assert shown(rig) is rig.handles["s2"]
    [record] = checkpoints(rig)
    assert record["display"]["reshown"] is True


def test_desktop_copy_of_the_shown_sequence_leaves_the_timeline_on_it(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s1")
    env = copy_op(rig, S1, reel2(rig))
    assert env["ok"] is True, env
    display = env["result"]["display"]
    assert display["shownBefore"]["name"] == "S1" and display["reshown"] is True
    assert display["shownAfterCopy"]["uid"] == env["result"]["copies"][0]["uid"]
    assert display["shownAfter"] == display["shownBefore"]
    assert shown(rig) is rig.handles["s1"]


def test_desktop_copy_into_a_library_reel_leaves_the_timeline_on_what_it_showed(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s2")
    env = copy_op(rig, S1, wt(rig.handles["libreel"].get_wiretap_node_id()))
    assert env["ok"] is True, env
    assert env["result"]["display"]["reshown"] is True
    assert shown(rig) is rig.handles["s2"]


def test_desktop_copy_with_nothing_shown_opens_nothing(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, None)
    env = copy_op(rig, S1, reel2(rig))
    assert env["ok"] is True, env
    assert env["result"]["display"]["reshown"] is None
    assert opened_by_api() is False


def test_desktop_copy_in_a_project_that_does_not_move_it_opens_nothing(rig: Rig) -> None:
    show_in_ui(rig, "s1")
    env = copy_op(rig, S1, reel2(rig))
    assert env["ok"] is True, env
    assert env["result"]["display"]["reshown"] is None
    assert shown(rig) is rig.handles["s1"] and opened_by_api() is False


def test_desktop_duplicate_of_a_sequence_leaves_the_timeline_on_what_it_showed(rig: Rig) -> None:
    ACTIVE_TIMELINE[0].duplicate_moves_display_until_open = True
    seq = add_seq(rig.handles["reel2"], "D1")
    rig.handles["d1"] = seq
    show_in_ui(rig, "d1")
    env = rig.do("desktop.duplicate", {"target": wt(seq.get_wiretap_node_id())})
    assert env["ok"] is True, env
    display = env["result"]["display"]
    assert display["shownBefore"]["name"] == "D1" and display["reshown"] is True
    assert display["shownAfterCopy"]["uid"] == env["result"]["duplicate"]["uid"]
    assert display["shownAfter"] == display["shownBefore"]
    assert shown(rig) is seq


def test_desktop_duplicate_in_a_project_that_does_not_move_it_opens_nothing(rig: Rig) -> None:
    seq = add_seq(rig.handles["reel2"], "D1")
    rig.handles["d1"] = seq
    show_in_ui(rig, "d1")
    env = rig.do("desktop.duplicate", {"target": wt(seq.get_wiretap_node_id())})
    assert env["ok"] is True, env
    assert env["result"]["display"]["reshown"] is None and opened_by_api() is False


def test_the_second_copy_after_an_open_does_not_move_the_timeline_at_all(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s1")
    first = copy_op(rig, S1, reel2(rig))
    assert first["result"]["display"]["reshown"] is True
    second = copy_op(rig, S2, reel2(rig))
    assert second["result"]["display"]["reshown"] is None
    assert second["result"]["display"]["shownAfterCopy"]["name"] == "S1"


def stash_display(rig: Rig) -> dict[str, Any]:
    env = trim(rig, a_seg(1))
    assert env["ok"] is True, env
    [record] = checkpoints(rig)
    display: dict[str, Any] = record["display"]
    return display


def test_a_library_sequence_that_was_shown_cannot_be_opened_again_and_the_note_says_so(
    rig: Rig,
) -> None:
    project_moves_display()
    in_library = rig.flame.media_panel.copy(rig.handles["s2"], rig.handles["libreel"])[0]
    ACTIVE_TIMELINE[0].clip = in_library
    ACTIVE_TIMELINE[0].opened_by_api = False
    display = stash_display(rig)
    assert display["reshown"] is False
    assert "inside a library cannot be opened" in display["note"]
    assert display["shownAfter"]["uid"] == display["shownAfterCopy"]["uid"]
    assert display["shownAfter"]["uid"] != display["shownBefore"]["uid"]


def test_a_clip_that_was_shown_cannot_be_opened_again_and_the_note_says_so(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "clipx")
    display = stash_display(rig)
    assert display["shownBefore"]["type"] == "PyClip"
    assert display["reshown"] is False and "no open()" in display["note"]
    assert display["shownAfter"]["uid"] == display["shownAfterCopy"]["uid"]


def test_a_shown_sequence_that_cannot_be_found_any_more_is_told_in_the_note(rig: Rig) -> None:
    project_moves_display()
    ACTIVE_TIMELINE[0].clip = PySequence("Orphan", wiretap_id="/wt/nowhere/Orphan")
    display = stash_display(rig)
    assert display["shownBefore"]["name"] == "Orphan"
    assert display["reshown"] is None
    assert "not found" in display["note"]
    assert display["shownAfter"] == display["shownAfterCopy"]
    assert opened_by_api() is False


def raise_after_copy(rig: Rig) -> None:
    original = rig.flame.media_panel.copy

    def failing(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("scripted: the copy raised after it moved the Timeline")

    rig.flame.media_panel.copy = failing


def test_a_stash_copy_that_raises_still_puts_the_timeline_back(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s1")
    raise_after_copy(rig)
    env = trim(rig, a_seg(1))
    assert env["ok"] is False
    assert shown(rig) is rig.handles["s1"]


def test_a_desktop_copy_that_raises_still_puts_the_timeline_back(rig: Rig) -> None:
    project_moves_display()
    show_in_ui(rig, "s1")
    raise_after_copy(rig)
    env = copy_op(rig, S1, reel2(rig))
    assert env["ok"] is False
    assert shown(rig) is rig.handles["s1"]
