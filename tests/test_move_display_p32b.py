"""Tests for `desktop.move` putting the Timeline back on what it showed.

Moving a sequence out of a reel may switch the Timeline to another sequence (measured on Flame
2025.2.4: the moved one that was shown, and a sequence in front of the shown one -- the rule is not
known), so what is shown is read before and
after, and the sequence shown before is opened again; a moved sequence that WAS the shown one is
opened where it is now (a library one cannot be opened). `desktop.move`'s revert does the same. Run
through the real Runtime with a scripted `transport.exec` over the fake Flame tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import ACTIVE_TIMELINE
from journal_test_helpers import Rig
from p32b_helpers import LIBREEL, MOVE, S1, S2, S3, make_p32b_rig, reel2, show, shown, uid
from timeline_p32a_helpers import reel_names


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def move(rig: Rig, sources: list[dict[str, Any]], destination: dict[str, Any]) -> dict[str, Any]:
    return rig.do(MOVE, {"sources": sources, "destination": destination})


def test_moving_a_sequence_in_front_of_the_shown_one_leaves_the_timeline_where_it_was(
    rig: Rig,
) -> None:
    show(rig, "s2")
    env = move(rig, [S1], reel2(rig))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["shownBefore"]["name"] == "S2"
    # what the move did (the fake follows the measurement)
    assert result["shownAfterMove"]["name"] == "S3"
    assert result["reshown"] is True and result["shownAfter"]["name"] == "S2"
    assert shown(rig) is rig.handles["s2"]
    assert reel_names(rig.handles["reel2"]) == ["S1"]


def test_a_move_that_does_not_touch_the_timeline_reports_it_unchanged(rig: Rig) -> None:
    show(rig, "s2")
    env = move(rig, [S3], reel2(rig))
    result = env["result"]
    assert result["reshown"] is None and result["reshownNote"] is None
    assert result["shownBefore"] == result["shownAfterMove"] == result["shownAfter"]
    assert shown(rig) is rig.handles["s2"]


def test_a_moved_sequence_that_was_shown_is_opened_where_it_is_now(rig: Rig) -> None:
    show(rig, "s2")
    env = move(rig, [S2], reel2(rig))
    result = env["result"]
    moved = result["moved"][0]["to"]
    assert result["shownBefore"]["name"] == "S2" and result["reshown"] is True
    assert result["shownAfter"]["uid"] == moved["uid"]
    assert uid(shown(rig)) == moved["uid"]


def test_a_moved_shown_sequence_that_goes_into_a_library_cannot_be_opened(rig: Rig) -> None:
    show(rig, "s2")
    env = move(rig, [S2], LIBREEL)
    result = env["result"]
    assert env["ok"] is True and result["reshown"] is False
    assert "inside a library cannot be opened" in result["reshownNote"]
    assert result["shownBefore"]["name"] == "S2"


def test_a_clip_that_was_shown_is_not_opened_because_a_clip_has_no_open(rig: Rig) -> None:
    ACTIVE_TIMELINE[0].clip = rig.handles["clipx"]
    env = move(
        rig, [{"kind": "wiretap", "id": rig.handles["clipx"].get_wiretap_node_id()}], reel2(rig)
    )
    result = env["result"]
    assert env["ok"] is True and result["shownBefore"]["type"] == "PyClip"
    assert result["reshown"] is False and "no open()" in result["reshownNote"]


def test_nothing_shown_means_nothing_to_put_back(rig: Rig) -> None:
    ACTIVE_TIMELINE[0].clip = None
    env = move(rig, [S1], reel2(rig))
    result = env["result"]
    assert result["shownBefore"] is None and result["reshown"] is None
    assert result["shownAfter"] is None and ACTIVE_TIMELINE[0].clip is None


def test_the_revert_puts_the_timeline_back_too(rig: Rig) -> None:
    show(rig, "s2")
    env = move(rig, [S1], reel2(rig))
    assert shown(rig) is rig.handles["s2"]
    moved_wt = env["result"]["moved"][0]["to"]["selector"]
    # the revert moves S1 back into Reel 1, in front of nothing: show the moved one to make the
    # Timeline switch (the moved shown sequence is the case that does)
    rig.flame.find_by_wiretap_node_id(moved_wt["id"]).open()
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    restored = out["result"]["reverted"][0]["to"]
    assert out["result"]["shownBefore"]["name"] == "S1"
    assert (
        out["result"]["reshown"] is True and out["result"]["shownAfter"]["uid"] == restored["uid"]
    )
    assert uid(shown(rig)) == restored["uid"]


def test_the_revert_of_a_move_in_front_of_the_shown_one_puts_the_shown_one_back(
    rig: Rig,
) -> None:
    show(rig, "s2")
    env = move(rig, [S1], reel2(rig))
    # S1 comes back to the END of Reel 1: behind the shown S2, so nothing switches
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is True, out
    assert out["result"]["reshown"] is None
    assert shown(rig) is rig.handles["s2"] and out["result"]["shownAfter"]["name"] == "S2"
