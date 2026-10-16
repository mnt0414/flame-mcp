"""A segment addressed by record_in goes through the journaled write path.

The pre-read's stable address, the intent, the write and `journal.revert` all keep the form the
caller used (record_in), and an `expect_source_name` that does not match stops the call before
anything is journaled or written."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr
from journal_test_helpers import Rig, make_rig

SEQ = {"kind": "wiretap", "id": "/projects/p/ws/P1O/Seq1"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    made = make_rig(monkeypatch, tmp_path)
    seg1 = made.flame.find_by_wiretap_node_id(SEQ["id"])._versions[0]._tracks[0]._segments[1]
    seg1.comment = FakeAttr("old")
    seg1.attributes.append("comment")
    made.handles["seg1"] = seg1
    return made


def at(record_in: int, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "segment",
        "parent": SEQ,
        "version": 0,
        "track": 0,
        "record_in": record_in,
        **extra,
    }


def test_a_record_in_write_is_journaled_and_reverted_through_a_record_in_address(
    rig: Rig,
) -> None:
    target = at(10, expect_source_name="clip2")
    env = rig.do("segment.set_attributes", {"target": target, "values": {"comment": "new"}})
    assert env["ok"] is True, env
    assert rig.handles["seg1"].comment.get_value() == "new"
    intent = rig.records()[0]
    assert intent["target"] == target
    assert intent["before"] == {"comment": "old"}
    assert env["result"]["revert"]["args"]["target"] == target

    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    assert rig.handles["seg1"].comment.get_value() == "old"
    shown = rig.do("journal.show", {"group": env["journal"]["group"]})
    assert shown["result"]["state"] == "reverted"


def test_a_source_name_that_differs_stops_the_write_before_it_is_journaled(rig: Rig) -> None:
    env = rig.do(
        "segment.set_attributes",
        {"target": at(10, expect_source_name="clip1"), "values": {"comment": "new"}},
    )
    assert env["ok"] is False and env["errorCode"] == "CONFLICT"
    assert "'clip2'" in env["error"] and "nothing was written" in env["error"]
    assert rig.records() == []
    assert rig.handles["seg1"].comment.get_value() == "old"
    assert [c.label for c in rig.exec.calls] == ["segment.get_attributes"]


def test_a_record_in_that_does_not_exist_names_the_nearest_ones(rig: Rig) -> None:
    env = rig.do("segment.set_attributes", {"target": at(11), "values": {"comment": "new"}})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"
    assert "[10, 0]" in env["error"]
    assert rig.records() == []
