"""Tests `segment.connections`, `segment.create_connection`, `segment.remove_connection`,
`segment.sync_connections` and the connected-segment notice of `segment.replace`.

The writes run with no checkpoint and no journal record (the Timeline checkpoint would cover
only the target's sequence), so what is tested is the read-back (a count that did not move is a
warning, never an error), the presumed inverse call in result.inverse, the notices, and that
nothing is copied or journaled. Run through the real Runtime with a scripted `transport.exec` over
the fake Flame tree (fake_selection_connections.py: what a connection is made of is INVENTED
there; that a replace is propagated to the connected segments, and that journal.revert does not
put those back, was measured on Flame 2025.2.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import fake_selection_connections as fsc
from fake_media_tree import PySegment, share_sources
from flame_mcp.policy import Policy
from journal_test_helpers import Rig
from p33_helpers import REPLACE, add_media_clip, clip_sel, seg_a, track0, untouched_world
from timeline_p32a_helpers import (
    S1,
    S2,
    S2_WT,
    S3,
    a_seg,
    library_copy,
    make_timeline_rig,
    media,
    rows,
    seg_sel,
    wt,
)

CONNECTIONS = "segment.connections"
CREATE = "segment.create_connection"
REMOVE = "segment.remove_connection"
SYNC = "segment.sync_connections"
WRITES = (CREATE, REMOVE, SYNC)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def s2_seg(rig: Rig) -> PySegment:
    seg: PySegment = rig.handles["s2"].versions[0].tracks[0].segments[0]
    return seg


def s3_seg(rig: Rig) -> PySegment:
    seg: PySegment = rig.handles["s3"].versions[0].tracks[0].segments[0]
    return seg


def link(rig: Rig) -> None:
    """S1's segment A (clip1) and S2's only segment (clip1) are connected, as the UI does it."""
    fsc.connect(seg_a(rig), s2_seg(rig))


def target(rig: Rig) -> dict[str, Any]:
    return a_seg()


def test_a_segment_that_is_not_connected_lists_nothing(rig: Rig) -> None:
    env = rig.do(CONNECTIONS, {"target": a_seg()})
    assert env["ok"] is True, env
    assert env["result"]["count"] == 0 and env["result"]["items"] == []
    assert env["result"]["target"]["sourceName"] == "clip1"
    assert env["result"]["truncated"] is False


def test_a_connected_segment_is_listed_with_where_it_is(rig: Rig) -> None:
    link(rig)
    env = rig.do(CONNECTIONS, {"target": a_seg()})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["count"] == result["total"] == 1 and result["shown"] == 1
    [item] = result["items"]
    assert item["sequence"] == {"type": "PySequence", "name": "S2"}
    assert item["uid"] == rig.handles["s2"].uid.get_value() and item["wiretapId"] == S2_WT
    assert (item["version"], item["track"], item["index"]) == (0, 0, 0)
    assert item["name"] == "s" and item["sourceName"] == "clip1"
    assert item["recordIn"]["frame"] == 0 and item["recordOut"]["frame"] == 23
    assert item["selector"] == seg_sel(S2, 0, index=0)
    assert item["isTarget"] is False and item["ambiguous"] is False and "error" not in item


def test_the_selector_of_an_item_addresses_the_connected_segment(rig: Rig) -> None:
    link(rig)
    [item] = rig.do(CONNECTIONS, {"target": a_seg()})["result"]["items"]
    info = rig.do("segment.info", {"target": item["selector"]})
    assert info["ok"] is True and info["result"]["sourceName"] == "clip1"
    assert info["result"]["recordIn"]["frame"] == 0


def test_the_target_itself_that_connected_segments_lists_is_left_out_of_the_count(
    rig: Rig,
) -> None:
    link(rig)
    assert len(seg_a(rig).connected_segments()) == 2  # measured: a pair answers 2 for each
    result = rig.do(CONNECTIONS, {"target": a_seg()})["result"]
    assert result["count"] == result["total"] == 1 and result["targetListed"] is True
    assert [i["isTarget"] for i in result["items"]] == [False]
    assert result["items"][0]["sequence"]["name"] == "S2" and result["warnings"] == []


def test_a_target_that_cannot_be_told_apart_may_be_in_the_count_and_a_warning_says_so(
    rig: Rig,
) -> None:
    twin = media("clip1", 10, 33)
    track = rig.handles["s1"]._versions[0]._tracks[0]
    track._segments.append(twin)
    twin.parent = track
    link(rig)
    result = rig.do(CONNECTIONS, {"target": a_seg()})["result"]
    assert result["targetListed"] is False and result["count"] == 2
    assert "may include the target itself" in result["warnings"][0]


def test_a_segment_with_no_connection_lists_only_itself_and_counts_zero(rig: Rig) -> None:
    assert seg_a(rig).connected_segments() == [seg_a(rig)]  # measured: [self]
    result = rig.do(CONNECTIONS, {"target": a_seg()})["result"]
    assert result["count"] == 0 and result["items"] == [] and result["targetListed"] is True


def test_the_list_is_paged_and_the_count_is_of_all(rig: Rig) -> None:
    link(rig)
    fsc.connect(seg_a(rig), s3_seg(rig))
    first = rig.do(CONNECTIONS, {"target": a_seg(), "limit": 1})["result"]
    assert first["count"] == 2 and first["shown"] == 1 and first["truncated"] is True
    rest = rig.do(CONNECTIONS, {"target": a_seg(), "limit": 1, "offset": 1})["result"]
    assert rest["shown"] == 1 and rest["truncated"] is False and rest["offset"] == 1
    assert {first["items"][0]["sequence"]["name"], rest["items"][0]["sequence"]["name"]} == {
        "S2",
        "S3",
    }


def test_a_connected_segment_that_cannot_be_placed_uniquely_has_no_selector(rig: Rig) -> None:
    twin = media("clip1", 0, 23)
    rig.handles["s2"]._versions[0]._tracks[0]._segments.append(twin)
    twin.parent = rig.handles["s2"]._versions[0]._tracks[0]
    fsc.connect(seg_a(rig), s2_seg(rig))
    [item] = rig.do(CONNECTIONS, {"target": a_seg()})["result"]["items"]
    assert item["ambiguous"] is True and item["selector"] is None
    assert {(c["version"], c["track"]) for c in item["candidates"]} == {(0, 0)}
    assert item["sequence"]["name"] == "S2" and item["index"] is None


def test_a_connected_segment_whose_parent_cannot_be_resolved_reports_what_is_known(
    rig: Rig,
) -> None:
    link(rig)
    s2_seg(rig).parent = None  # the segment is no longer reachable from a sequence
    [item] = rig.do(CONNECTIONS, {"target": a_seg()})["result"]["items"]
    assert item["sequence"] is None and item["uid"] is None and item["wiretapId"] is None
    assert item["sourceName"] == "clip1" and item["name"] == "s" and item["recordIn"]["frame"] == 0
    assert "error" in item and item["selector"] is None


def test_the_read_op_is_a_read_and_works_on_a_readonly_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro = make_timeline_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"})
    )
    assert ro.do(CONNECTIONS, {"target": a_seg()})["ok"] is True
    for name in WRITES:
        assert ro.do(name, {"target": a_seg()})["errorCode"] == "POLICY_DENIED"
    assert untouched_world(ro)


def test_create_connection_reads_the_count_back_and_hands_back_the_inverse(rig: Rig) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    env = rig.do(CREATE, {"target": a_seg()})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["action"] == "create" and result["connected"] == {"before": 0, "after": 1}
    assert result["returned"] is None and result["ran"] is True
    assert result["before"]["sourceName"] == result["after"]["sourceName"] == "clip1"
    assert result["position"]["recordIn"] == 10
    assert result["position"]["selector"]["record_in"] == 10
    assert result["inverse"] == {
        "op": REMOVE,
        "args": {"target": result["position"]["selector"]},
    }
    assert len(seg_a(rig).connected_segments()) == 2  # the list holds the segment itself too
    [notice] = result["notices"]
    assert "other sequences" in notice and "journal.revert" in notice
    assert env["journal"] is None  # no journal record, no checkpoint
    assert [c.label for c in rig.exec.calls] == [CREATE]
    assert untouched_world(rig)


def test_the_presumed_inverse_the_result_names_takes_it_back(rig: Rig) -> None:
    share_sources(seg_a(rig), s2_seg(rig))
    revert = rig.do(CREATE, {"target": a_seg()})["result"]["inverse"]
    undone = rig.do(revert["op"], revert["args"])
    assert undone["ok"] is True, undone
    assert undone["result"]["connected"] == {"before": 1, "after": 0}
    assert seg_a(rig).connected_segments() == [seg_a(rig)]  # not connected: [self]
    again = undone["result"]["inverse"]
    assert again["op"] == CREATE and again["args"]["target"]["record_in"] == 10


def test_a_create_that_leaves_the_count_alone_is_a_warning_not_an_error(rig: Rig) -> None:
    env = rig.do(CREATE, {"target": a_seg()})  # nothing shares its source: the fake connects none
    assert env["ok"] is True and env["errorCode"] is None, env
    result = env["result"]
    assert result["connected"] == {"before": 0, "after": 0} and result["inverse"] is None
    [warning] = result["warnings"]
    assert "create_connection() returned None" in warning and "answers 0 segment(s)" in warning
    assert "lists the segment itself (measured)" in warning


def test_remove_connection_reads_the_count_back(rig: Rig) -> None:
    link(rig)
    env = rig.do(REMOVE, {"target": a_seg()})
    assert env["ok"] is True, env
    assert env["result"]["connected"] == {"before": 1, "after": 0}
    assert env["result"]["inverse"]["op"] == CREATE
    assert s2_seg(rig).connected_segments() == [s2_seg(rig)]
    assert "other sequences" in env["result"]["notices"][0] and env["journal"] is None


def test_a_remove_that_leaves_as_many_is_a_warning_not_an_error(rig: Rig) -> None:
    env = rig.do(REMOVE, {"target": a_seg()})
    assert env["ok"] is True, env
    [warning] = env["result"]["warnings"]
    assert "remove_connection() returned None" in warning and env["result"]["inverse"] is None


def test_a_count_that_moves_the_wrong_way_is_a_warning_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    link(rig)
    monkeypatch.setattr(
        PySegment, "create_connection", lambda self: fsc.seg_remove_connection(self)
    )
    env = rig.do(CREATE, {"target": a_seg()})
    assert env["ok"] is True and env["result"]["connected"] == {"before": 1, "after": 0}
    assert len(env["result"]["warnings"]) == 1 and env["result"]["inverse"] is None


def test_sync_reports_the_count_and_the_answer_and_judges_nothing(rig: Rig) -> None:
    link(rig)
    env = rig.do(SYNC, {"target": a_seg()})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["action"] == "sync" and result["connected"] == {"before": 1, "after": 1}
    assert result["returned"] is None and result["inverse"] is None
    assert "other sequences" in result["notices"][0]
    assert env["journal"] is None and untouched_world(rig)


@pytest.mark.parametrize("op", WRITES)
def test_a_gap_is_refused_before_anything_is_changed(rig: Rig, op: str) -> None:
    env = rig.do(op, {"target": seg_sel(S1, 0, index=0)})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "Gap" in env["error"]
    assert env["result"]["ran"] is False and env["result"]["action"] is not None


@pytest.mark.parametrize("op", WRITES)
def test_a_segment_of_a_sequence_in_a_library_is_refused(rig: Rig, op: str) -> None:
    copy = library_copy(rig)
    env = rig.do(op, {"target": seg_sel(wt(copy.get_wiretap_node_id()), 0, index=1)})
    assert env["errorCode"] == "PRECONDITION_UNMET" and "inside a library" in env["error"]


@pytest.mark.parametrize("op", WRITES)
def test_a_connected_segments_that_raises_stops_the_write(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    def broken(self: PySegment) -> list[PySegment]:
        raise RuntimeError("scripted failure")

    monkeypatch.setattr(PySegment, "connected_segments", broken)
    env = rig.do(op, {"target": a_seg()})
    assert env["errorCode"] == "FLAME_API_ERROR" and "nothing was changed" in env["error"]
    assert "RuntimeError: scripted failure" in env["error"]


@pytest.mark.parametrize("op", WRITES)
def test_a_plan_refuses_the_connection_writes(rig: Rig, op: str) -> None:
    env = rig.do("plan.run", {"steps": [{"op": op, "args": {"target": a_seg()}}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    [issue] = env["result"]["issues"]
    assert "cannot be a plan step" in " ".join(issue["problems"])
    assert untouched_world(rig)


def described(rig: Rig, name: str) -> str:
    op = rig.registry.get(name)
    assert op is not None
    return op.description


def test_the_writes_are_declared_as_unsupported_undo_with_no_checkpoint(rig: Rig) -> None:
    for name in WRITES:
        op = rig.registry.get(name)
        assert op is not None
        assert (op.effect, op.undo, op.category) == ("destructive", "unsupported", "segment")
        assert op.checkpoint is None and op.journal is None and op.wrap is None
        assert op.accepts == ("segment",) and [p.name for p in op.params] == ["target"]
        flat = " ".join(op.description.split())
        for word in (
            "other sequences",
            "journal.revert",
            "no checkpoint",
            "notices",
            "PRESUMED inverse",
            "lists the segment itself (measured)",
        ):
            assert word in flat, (name, word)
        assert "PySegment.connected_segments" in op.requires
    read = rig.registry.get(CONNECTIONS)
    assert read is not None and read.effect == "read" and read.default_limit == 20
    assert [p.name for p in read.params] == ["target", "limit", "offset"]
    sync, create, remove = (" ".join(described(rig, n).split()) for n in (SYNC, CREATE, REMOVE))
    assert "sync_connected_segments" in sync and "makes no judgement of its own" in sync
    assert "inverse" in create and "segment.remove_connection" in create
    assert "segment.create_connection" in remove


def test_a_replace_of_a_connected_segment_says_the_others_follow_and_revert_does_not_cover_them(
    rig: Rig,
) -> None:
    link(rig)
    other = add_media_clip(rig, "len72", start=0, length=72)
    before_s1 = rows(rig.handles["s1"])
    before_s2 = rows(rig.handles["s2"])
    env = rig.do(REPLACE, {"target": a_seg(), "source": clip_sel(other)})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["connected"] == {"count": 1, "error": None}
    [notice] = result["notices"]
    assert "1 segment(s) are connected" in notice and "other sequences" in notice
    assert "journal.revert" in notice and "does not put them back" in notice
    assert s2_seg(rig).source_name == "len72"  # the propagation: measured on Flame 2025.2.4
    revert = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert revert["ok"] is True, revert
    [restored] = [c for c in rig.handles["reel"].children if c.name.get_value() == "S1"]
    assert rows(restored) == before_s1
    assert s2_seg(rig).source_name == "len72"  # NOT put back: the notice was right
    assert rows(rig.handles["s2"]) != before_s2


def test_a_replace_of_a_segment_with_no_connection_has_no_notice(rig: Rig) -> None:
    other = add_media_clip(rig, "len72", start=0, length=72)
    env = rig.do(REPLACE, {"target": a_seg(), "source": clip_sel(other)})
    assert env["ok"] is True, env
    assert env["result"]["connected"] == {"count": 0, "error": None}
    assert env["result"]["notices"] == []  # [self] is not a connection
    assert track0(rig)[1].source_name == "len72"


def test_a_replace_still_works_when_the_connections_cannot_be_read(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: PySegment) -> list[PySegment]:
        raise RuntimeError("scripted failure")

    monkeypatch.setattr(PySegment, "connected_segments", broken)
    other = add_media_clip(rig, "len72", start=0, length=72)
    env = rig.do(REPLACE, {"target": a_seg(), "source": clip_sel(other)})
    assert env["ok"] is True, env
    assert env["result"]["connected"]["count"] is None
    assert "scripted failure" in env["result"]["connected"]["error"]
    assert env["result"]["notices"] == []


def test_the_other_side_of_a_connection_is_listed_the_other_way_round(rig: Rig) -> None:
    link(rig)
    [item] = rig.do(CONNECTIONS, {"target": seg_sel(S2, 0, index=0)})["result"]["items"]
    assert item["sequence"]["name"] == "S1" and item["recordIn"]["frame"] == 10
    assert item["selector"] == seg_sel(S1, 0, index=1) and S3 != S2
