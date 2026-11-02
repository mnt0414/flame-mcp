"""`timeline.usage`: unlinked segments, shared sources and clips no scanned sequence refers to,
run against `fake_media_tree.FakeFlame` (no bridge)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from fake_media_tree import (
    FakeFlame,
    PyBatch,
    PyClip,
    PyDesktop,
    PyLibrary,
    PyReel,
    PyReelGroup,
    PySegment,
    PySequence,
    PyTrack,
    PyVersion,
)
from flame_mcp.ops import register_all
from flame_mcp.opschema import validate_op_args
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from op_test_helpers import run_op

OP = "timeline.usage"


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


class UsageSegment(PySegment):
    """A segment that carries the source facts of a real one (the base fake has none)."""

    def __init__(
        self,
        source_name: str,
        file_path: str,
        *,
        kind: str = "Video Segment",
        unlinked: Any = False,
        tape: str = "",
    ) -> None:
        super().__init__(name=source_name or "seg", source_name=source_name, type=kind)
        self.file_path = file_path
        self.tape_name = tape
        self.source_unlinked = unlinked


def numbered(tracks: list[list[PySegment]]) -> list[PyVersion]:
    for track in tracks:
        for i, seg in enumerate(track):
            seg.record_in = type(seg.record_in)(10 * i)
            seg.record_out = type(seg.record_out)(10 * i + 9)
    return [PyVersion([PyTrack(segs) for segs in tracks])]


def make_seq(
    name: str,
    wiretap: str,
    tracks: list[list[PySegment]],
    cls: type[PySequence] = PySequence,
) -> PySequence:
    return cls(name, wiretap, versions=numbered(tracks), uid=f"u-{name}")


def make_clip(name: str, wiretap: str, segment: PySegment | None) -> PyClip:
    versions = [PyVersion([PyTrack([] if segment is None else [segment])])]
    return PyClip(name, wiretap_id=wiretap, versions=versions, uid=f"u-{name}")


def gap() -> UsageSegment:
    return UsageSegment("orph", "", kind="Gap")


def build_world(*, closed: bool = False) -> tuple[FakeFlame, dict[str, Any]]:
    seq_a = make_seq(
        "seqA",
        "/wt/rg1/a/seqA",
        [
            [
                UsageSegment("a", "/m/a.mov"),
                UsageSegment("b", "/m/b.mov"),
                gap(),
                UsageSegment("orph", ""),
                UsageSegment("", ""),
            ],
            [UsageSegment("c", "/m/c.mov")],
        ],
    )
    seq_b = make_seq(
        "seqB",
        "/wt/rg1/b/seqB",
        [
            [
                UsageSegment("a", "/m/a.mov"),
                UsageSegment("gone", "/m/gone.mov", unlinked=True, tape="T9"),
                UsageSegment("x", "/m/x.mov", kind="Unlinked Segment"),
                UsageSegment("orph", ""),
                UsageSegment("", ""),
            ]
        ],
    )
    reel_a = PyReel(
        "Reel A",
        "u-ra",
        "/wt/rg1/a",
        [
            seq_a,
            make_clip("cUsed", "/wt/rg1/a/cUsed", UsageSegment("b", "/m/b.mov")),
            make_clip("cFree", "/wt/rg1/a/cFree", UsageSegment("free", "/m/free.mov")),
            make_clip("cNoKey", "/wt/rg1/a/cNoKey", UsageSegment("", "")),
            make_clip("cOrph", "/wt/rg1/a/cOrph", UsageSegment("orph", "")),
        ],
    )
    reel_b = PyReel("Reel B", "u-rb", "/wt/rg1/b", [seq_b])
    rg1 = PyReelGroup("RG1", "/wt/rg1", [reel_a, reel_b], uid="u-rg1")
    seq_l = make_seq(
        "seqL",
        "/wt/lo/lr/seqL",
        [[UsageSegment("lib", "/m/lib.mov"), UsageSegment("c", "/m/c.mov")]],
    )
    lib_reel = PyReel(
        "LR",
        "u-lr",
        "/wt/lo/lr",
        [
            seq_l,
            make_clip("cLibFree", "/wt/lo/lr/cLibFree", UsageSegment("lf", "/m/libfree.mov")),
            make_clip("cLibUsed", "/wt/lo/lr/cLibUsed", UsageSegment("lib", "/m/lib.mov")),
        ],
    )
    lib_open = PyLibrary("LibOpen", "u-lo", "/wt/lo", [lib_reel])
    libraries = [lib_open]
    handles: dict[str, Any] = {"seq_a": seq_a, "seq_b": seq_b, "seq_l": seq_l, "reel_a": reel_a}
    if closed:
        seq_c = make_seq(
            "seqC",
            "/wt/lc/rc/seqC",
            [[UsageSegment("gone", "/m/gone2.mov", unlinked="V")]],
        )
        lib_closed: Any = PyLibrary(
            "LibClosed", "u-lc", "/wt/lc", [PyReel("RC", "u-rc", "/wt/lc/rc", [seq_c])]
        )
        lib_closed.close()
        libraries.append(lib_closed)
        handles["lib_closed"] = lib_closed
    desktop = PyDesktop(batch_groups=[PyBatch("B1", "u-b1")], reel_groups=[rg1])
    return FakeFlame(libraries=libraries, desktop=desktop), handles


def wiretap(seq: PySequence) -> dict[str, str]:
    return {"kind": "wiretap", "id": seq.get_wiretap_node_id()}


def usage(flame: FakeFlame, report: str, **args: Any) -> dict[str, Any]:
    result = run_op(make_registry(), OP, {"limit": 500, "report": report, **args}, flame)
    assert result["opError"] is None, result
    value: dict[str, Any] = result["value"]
    return value


def usage_error(flame: FakeFlame, report: str, **args: Any) -> dict[str, Any]:
    return run_op(make_registry(), OP, {"limit": 500, "report": report, **args}, flame)


def ticking(monkeypatch: pytest.MonkeyPatch, seconds_per_call: float) -> None:
    calls = itertools.count()
    monkeypatch.setattr("time.monotonic", lambda: next(calls) * seconds_per_call)


# ---- shape of the op -------------------------------------------------------------------------


def test_the_op_is_a_main_thread_read_without_undo_in_the_timeline_category() -> None:
    op = make_registry().get(OP)
    assert op is not None
    assert (op.category, op.effect, op.undo, op.thread) == ("timeline", "read", "none", "main")


def test_report_is_required_and_one_of_three_values() -> None:
    op = make_registry().get(OP)
    assert op is not None
    assert validate_op_args(op, {}).ok is False
    assert validate_op_args(op, {"report": "unused"}).ok is False
    for report in ("unlinked", "shared", "unreferenced"):
        assert validate_op_args(op, {"report": report}).ok is True, report


def test_sequences_must_be_selectors() -> None:
    op = make_registry().get(OP)
    assert op is not None
    assert validate_op_args(op, {"report": "shared", "sequences": ["x"]}).ok is False
    ok = validate_op_args(op, {"report": "shared", "sequences": [{"kind": "wiretap", "id": "x"}]})
    assert ok.ok is True


def test_sequence_offset_is_refused_for_unreferenced_and_a_big_max_sequences_too() -> None:
    op = make_registry().get(OP)
    assert op is not None and op.precheck is not None
    policy = Policy.from_env({})
    assert op.precheck({"report": "unreferenced"}, policy) is None
    assert op.precheck({"report": "unlinked", "sequence_offset": 3}, policy) is None
    assert op.precheck({"report": "unlinked", "max_sequences": 500}, policy) is None
    for bad in (
        {"report": "unreferenced", "sequence_offset": 0},
        {"report": "unreferenced", "sequence_offset": 2},
        {"report": "unlinked", "max_sequences": 501},
    ):
        refusal = op.precheck(bad, policy)
        assert refusal is not None and refusal.code == "INVALID_ARGS", bad


# ---- unlinked --------------------------------------------------------------------------------


def test_unlinked_lists_the_flagged_segment_and_the_one_whose_type_says_unlinked() -> None:
    flame, _h = build_world()
    value = usage(flame, "unlinked")
    by_name = {i["sourceName"]: i for i in value["items"]}
    assert sorted(by_name) == ["gone", "x"]
    gone = by_name["gone"]
    assert gone == {
        "sequence": {"name": "seqB", "selector": {"kind": "wiretap", "id": "/wt/rg1/b/seqB"}},
        "version": 0,
        "track": 0,
        "index": 1,
        "recordIn": {"timecode": gone["recordIn"]["timecode"], "frame": 10},
        "type": "Video Segment",
        "sourceName": "gone",
        "filePath": "/m/gone.mov",
        "tapeName": "T9",
        "sourceUnlinked": True,
        "selector": {
            "kind": "segment",
            "parent": {"kind": "wiretap", "id": "/wt/rg1/b/seqB"},
            "version": 0,
            "track": 0,
            "record_in": 10,
        },
    }
    typed = by_name["x"]
    assert typed["type"] == "Unlinked Segment" and typed["sourceUnlinked"] is False
    assert value["total"] == 2 and value["truncated"] is False


def test_an_unlinked_item_selector_goes_to_segment_ops_as_it_is() -> None:
    flame, _h = build_world()
    item = next(i for i in usage(flame, "unlinked")["items"] if i["sourceName"] == "gone")
    info = run_op(make_registry(), "segment.info", {"target": item["selector"]}, flame)
    assert info["opError"] is None, info
    assert info["value"]["sourceName"] == "gone" and info["value"]["sourceUnlinked"] is True


def test_an_unlinked_flag_that_is_an_attribute_object_is_read() -> None:
    from fake_media_tree import FakeAttr

    seq = make_seq(
        "s",
        "/wt/s",
        [[UsageSegment("n", "/m/n.mov", unlinked=FakeAttr(True))]],
    )
    reel = PyReel("R", "u-r", "/wt/r", [seq])
    flame = FakeFlame(desktop=PyDesktop(reel_groups=[PyReelGroup("G", "/wt/g", [reel], uid="u-g")]))
    assert [i["sourceName"] for i in usage(flame, "unlinked")["items"]] == ["n"]


def test_a_gap_is_never_unlinked_and_never_counted() -> None:
    flame, _h = build_world()
    value = usage(flame, "unlinked")
    assert all(i["type"] != "Gap" for i in value["items"])
    # seqA 6 + seqB 5 + seqL 2, the Gap of seqA left out
    assert value["segmentsScanned"] == 12


def test_the_common_result_names_the_scanned_sequences_and_the_batch_limit() -> None:
    flame, _h = build_world(closed=True)
    value = usage(flame, "unlinked")
    assert value["scannedSequences"] == [
        {"name": "seqA", "selector": {"kind": "wiretap", "id": "/wt/rg1/a/seqA"}},
        {"name": "seqB", "selector": {"kind": "wiretap", "id": "/wt/rg1/b/seqB"}},
        {"name": "seqL", "selector": {"kind": "wiretap", "id": "/wt/lo/lr/seqL"}},
    ]
    assert value["closedLibraries"] == ["LibClosed"]
    assert value["batchUsageNotCounted"] is True
    assert "nextSequenceOffset" not in value and "stoppedEarly" not in value


# ---- shared ----------------------------------------------------------------------------------


def test_shared_groups_by_file_path_then_by_source_name_and_skips_what_has_no_key() -> None:
    flame, _h = build_world()
    value = usage(flame, "shared")
    rows = [(i["key"], i["matchedBy"], i["count"]) for i in value["items"]]
    assert rows == [
        ("/m/a.mov", "filePath", 2),
        ("orph", "sourceName", 2),
        ("/m/c.mov", "filePath", 2),
    ]


def test_a_shared_item_has_its_uses_with_a_selector_each() -> None:
    flame, _h = build_world()
    item = usage(flame, "shared")["items"][0]
    assert item["sourceName"] == "a" and item["filePath"] == "/m/a.mov"
    assert item["usesTruncated"] is False
    first, second = item["uses"]
    assert first["sequence"] == {
        "name": "seqA",
        "selector": {"kind": "wiretap", "id": "/wt/rg1/a/seqA"},
    }
    assert (first["version"], first["track"], first["index"]) == (0, 0, 0)
    assert first["recordIn"]["frame"] == 0
    assert first["selector"] == {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "/wt/rg1/a/seqA"},
        "version": 0,
        "track": 0,
        "record_in": 0,
    }
    assert second["sequence"]["name"] == "seqB" and second["selector"]["record_in"] == 0
    assert set(first) == {"sequence", "version", "track", "index", "recordIn", "selector"}


def test_the_file_path_wins_over_the_source_name() -> None:
    seq = make_seq(
        "s",
        "/wt/s",
        [
            [
                UsageSegment("one", "/m/same.mov"),
                UsageSegment("two", "/m/same.mov"),
                UsageSegment("one", "/m/other.mov"),
            ]
        ],
    )
    reel = PyReel("R", "u-r", "/wt/r", [seq])
    flame = FakeFlame(desktop=PyDesktop(reel_groups=[PyReelGroup("G", "/wt/g", [reel], uid="u-g")]))
    items = usage(flame, "shared")["items"]
    assert [(i["key"], i["matchedBy"], i["count"]) for i in items] == [
        ("/m/same.mov", "filePath", 2)
    ]


def test_a_segment_without_a_file_path_attribute_falls_back_to_the_source_name() -> None:
    plain_a, plain_b = (
        PySegment(name="p", source_name="same"),
        PySegment(name="q", source_name="same"),
    )
    seq = make_seq("s", "/wt/s", [[plain_a, plain_b]])
    reel = PyReel("R", "u-r", "/wt/r", [seq])
    flame = FakeFlame(desktop=PyDesktop(reel_groups=[PyReelGroup("G", "/wt/g", [reel], uid="u-g")]))
    items = usage(flame, "shared")["items"]
    assert [(i["key"], i["matchedBy"], i["count"]) for i in items] == [("same", "sourceName", 2)]


def many_world() -> tuple[FakeFlame, PySequence]:
    seq = make_seq("many", "/wt/many", [[UsageSegment("m", "/m/many.mov") for _ in range(25)]])
    reel = PyReel("R", "u-r", "/wt/r", [seq])
    flame = FakeFlame(desktop=PyDesktop(reel_groups=[PyReelGroup("G", "/wt/g", [reel], uid="u-g")]))
    return flame, seq


def test_uses_stop_at_20_while_count_keeps_counting() -> None:
    flame, seq = many_world()
    item = usage(flame, "shared", sequences=[wiretap(seq)])["items"][0]
    assert item["count"] == 25 and len(item["uses"]) == 20 and item["usesTruncated"] is True
    assert [u["index"] for u in item["uses"]] == list(range(20))


# ---- sequences, scope, paging ----------------------------------------------------------------


def test_sequences_limits_the_scan_to_the_ones_named() -> None:
    flame, h = build_world()
    value = usage(flame, "unlinked", sequences=[wiretap(h["seq_a"])])
    assert [s["name"] for s in value["scannedSequences"]] == ["seqA"]
    assert value["items"] == []


def test_sequences_are_scanned_in_the_order_given_and_a_repeat_is_read_once() -> None:
    flame, h = build_world()
    value = usage(
        flame,
        "shared",
        sequences=[wiretap(h["seq_b"]), wiretap(h["seq_a"]), wiretap(h["seq_b"])],
    )
    assert [s["name"] for s in value["scannedSequences"]] == ["seqB", "seqA"]
    assert [i["count"] for i in value["items"]] == [2, 2]


def test_a_selector_of_a_clip_is_selector_type() -> None:
    flame, h = build_world()
    clip = h["reel_a"].children[1]
    result = usage_error(flame, "unlinked", sequences=[wiretap(clip)])
    assert result["opErrorCode"] == "SELECTOR_TYPE", result
    assert "PyClip" in result["opError"]


def test_scope_desktop_and_libraries_split_the_walk() -> None:
    flame, _h = build_world()
    on_desktop = usage(flame, "unlinked", scope="desktop")
    assert [s["name"] for s in on_desktop["scannedSequences"]] == ["seqA", "seqB"]
    in_libraries = usage(flame, "unlinked", scope="libraries")
    assert [s["name"] for s in in_libraries["scannedSequences"]] == ["seqL"]


def test_max_sequences_pages_the_sequences_and_gives_the_next_offset() -> None:
    flame, _h = build_world()
    first = usage(flame, "shared", max_sequences=2)
    assert [s["name"] for s in first["scannedSequences"]] == ["seqA", "seqB"]
    assert first["nextSequenceOffset"] == 2 and first["truncated"] is True
    assert [i["key"] for i in first["items"]] == ["/m/a.mov", "orph"]
    second = usage(flame, "shared", max_sequences=2, sequence_offset=first["nextSequenceOffset"])
    assert [s["name"] for s in second["scannedSequences"]] == ["seqL"]
    assert "nextSequenceOffset" not in second and second["items"] == []


def test_max_sequences_pages_a_list_of_selectors_too() -> None:
    flame, h = build_world()
    chosen = [wiretap(h["seq_a"]), wiretap(h["seq_b"]), wiretap(h["seq_l"])]
    first = usage(flame, "unlinked", sequences=chosen, max_sequences=1)
    assert [s["name"] for s in first["scannedSequences"]] == ["seqA"]
    assert first["nextSequenceOffset"] == 1
    last = usage(flame, "unlinked", sequences=chosen, max_sequences=5, sequence_offset=1)
    assert [s["name"] for s in last["scannedSequences"]] == ["seqB", "seqL"]
    assert "nextSequenceOffset" not in last


def test_limit_and_offset_page_the_items() -> None:
    flame, _h = build_world()
    first = usage(flame, "shared", limit=2)
    assert (first["total"], first["shown"], first["offset"], first["truncated"]) == (3, 2, 0, True)
    second = usage(flame, "shared", limit=2, offset=2)
    assert (second["shown"], second["truncated"]) == (1, False)
    assert [i["key"] for i in first["items"] + second["items"]] == ["/m/a.mov", "orph", "/m/c.mov"]


# ---- time budget -----------------------------------------------------------------------------


def test_the_scan_stops_after_the_budget_and_gives_the_next_sequence_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flame, h = build_world()
    chosen = [wiretap(h["seq_a"]), wiretap(h["seq_b"]), wiretap(h["seq_l"])]
    ticking(monkeypatch, 3.0)
    value = usage(flame, "unlinked", sequences=chosen)
    assert value["stoppedEarly"] == {"reason": "time budget", "budgetS": 5.0, "read": 2}
    assert [s["name"] for s in value["scannedSequences"]] == ["seqA", "seqB"]
    assert value["nextSequenceOffset"] == 2 and value["truncated"] is True


def test_the_first_sequence_is_read_even_when_the_budget_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flame, h = build_world()
    chosen = [wiretap(h["seq_a"]), wiretap(h["seq_b"])]
    ticking(monkeypatch, 100.0)
    value = usage(flame, "unlinked", sequences=chosen)
    assert value["stoppedEarly"]["read"] == 1
    assert [s["name"] for s in value["scannedSequences"]] == ["seqA"]
    assert value["nextSequenceOffset"] == 1


# ---- unreferenced ----------------------------------------------------------------------------


def test_unreferenced_lists_the_clips_no_scanned_sequence_refers_to() -> None:
    flame, _h = build_world()
    value = usage(flame, "unreferenced")
    assert value["judged"] is True
    assert value["judgedAgainst"] == {
        "sequences": 3,
        "scope": "all",
        "closedLibrariesSkipped": [],
        "batchChecked": False,
        "segmentsWithoutKey": 2,
    }
    assert value["items"] == [
        {
            "clip": {
                "type": "PyClip",
                "name": "cFree",
                "uid": "u-cFree",
                "selector": {"kind": "wiretap", "id": "/wt/rg1/a/cFree"},
                "path": ["RG1", "Reel A", "cFree"],
            },
            "key": "/m/free.mov",
            "matchedBy": "filePath",
        },
        {
            "clip": {
                "type": "PyClip",
                "name": "cLibFree",
                "uid": "u-cLibFree",
                "selector": {"kind": "wiretap", "id": "/wt/lo/lr/cLibFree"},
                "path": ["LibOpen", "LR", "cLibFree"],
            },
            "key": "/m/libfree.mov",
            "matchedBy": "filePath",
        },
    ]
    assert value["total"] == 2 and value["batchUsageNotCounted"] is True
    assert value["clipsWithoutKey"] == 1
    assert "reason" not in value


def test_a_clip_that_matches_by_source_name_is_referenced() -> None:
    flame, _h = build_world()
    names = [i["clip"]["name"] for i in usage(flame, "unreferenced")["items"]]
    assert "cOrph" not in names and "cUsed" not in names and "cLibUsed" not in names


def test_unreferenced_ignores_max_sequences_and_reads_every_sequence() -> None:
    flame, _h = build_world()
    value = usage(flame, "unreferenced", max_sequences=1)
    assert value["judged"] is True and value["judgedAgainst"]["sequences"] == 3
    assert "nextSequenceOffset" not in value


def test_unreferenced_is_not_judged_for_a_scope_that_is_not_all() -> None:
    flame, _h = build_world()
    value = usage(flame, "unreferenced", scope="desktop")
    assert value["judged"] is False and value["reason"] == "scope"
    assert value["items"] == [] and value["total"] == 0
    assert value["judgedAgainst"]["scope"] == "desktop"
    assert usage(flame, "unreferenced", scope="libraries")["reason"] == "scope"


def test_unreferenced_is_not_judged_when_sequences_are_named() -> None:
    flame, h = build_world()
    value = usage(flame, "unreferenced", sequences=[wiretap(h["seq_a"])])
    assert value["judged"] is False and value["reason"] == "sequences"
    assert value["items"] == []


def test_unreferenced_is_not_judged_while_a_library_is_closed() -> None:
    flame, h = build_world(closed=True)
    value = usage(flame, "unreferenced")
    assert value["judged"] is False and value["reason"] == "closedLibraries"
    assert value["items"] == [] and value["closedLibraries"] == ["LibClosed"]
    assert value["judgedAgainst"]["closedLibrariesSkipped"] == ["LibClosed"]
    assert h["lib_closed"].opened.get_value() is False


def test_unreferenced_is_not_judged_when_the_walk_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flame, _h = build_world()
    ticking(monkeypatch, 100.0)
    value = usage(flame, "unreferenced")
    assert value["judged"] is False and value["reason"] == "budget"
    assert value["items"] == [] and value["stoppedEarly"]["reason"] == "time budget"


def test_unreferenced_is_not_judged_when_collecting_the_keys_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    class Slow(PySequence):
        @property
        def versions(self) -> list[PyVersion]:
            clock[0] += 10.0
            return PySequence.versions.fget(self)  # type: ignore[attr-defined,no-any-return]

    Slow.__name__ = "PySequence"
    slow_a = make_seq("sa", "/wt/g/r/sa", [[UsageSegment("a", "/m/a.mov")]], Slow)
    slow_b = make_seq("sb", "/wt/g/r/sb", [[UsageSegment("b", "/m/b.mov")]], Slow)
    clip = make_clip("c", "/wt/g/r/c", UsageSegment("z", "/m/z.mov"))
    reel = PyReel("R", "u-r", "/wt/g/r", [slow_a, slow_b, clip])
    flame = FakeFlame(desktop=PyDesktop(reel_groups=[PyReelGroup("G", "/wt/g", [reel], uid="u-g")]))
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    value = usage(flame, "unreferenced")
    assert value["judged"] is False and value["reason"] == "budget"
    assert value["items"] == []
    clock[0] = 0.0
    monkeypatch.setattr("time.monotonic", lambda: 0.0)
    assert usage(flame, "unreferenced")["judged"] is True


def test_unreferenced_is_not_judged_when_a_sequence_cannot_be_read() -> None:
    class Broken(PySequence):
        @property
        def versions(self) -> list[PyVersion]:
            raise RuntimeError("versions cannot be read")

    Broken.__name__ = "PySequence"
    broken = make_seq("bad", "/wt/g/r/bad", [[UsageSegment("a", "/m/a.mov")]], Broken)
    clip = make_clip("c", "/wt/g/r/c", UsageSegment("z", "/m/z.mov"))
    reel = PyReel("R", "u-r", "/wt/g/r", [broken, clip])
    flame = FakeFlame(desktop=PyDesktop(reel_groups=[PyReelGroup("G", "/wt/g", [reel], uid="u-g")]))
    value = usage(flame, "unreferenced")
    assert value["judged"] is False and value["reason"] == "unreadable"
    assert value["items"] == []
    other = usage(flame, "unlinked")
    assert other["sequenceErrors"][0]["sequence"]["name"] == "bad"
    assert "versions cannot be read" in other["sequenceErrors"][0]["error"]


def test_unreferenced_pages_its_items_with_limit_and_offset() -> None:
    flame, _h = build_world()
    first = usage(flame, "unreferenced", limit=1)
    assert (first["total"], first["shown"], first["truncated"]) == (2, 1, True)
    second = usage(flame, "unreferenced", limit=1, offset=1)
    assert second["items"][0]["clip"]["name"] == "cLibFree" and second["truncated"] is False


# ---- closed libraries ------------------------------------------------------------------------


def test_a_sequence_inside_a_closed_library_is_opened_for_the_call_and_closed_again() -> None:
    flame, h = build_world(closed=True)
    selector = {"kind": "path", "path": ["LibClosed", "RC", "seqC"]}
    assert h["lib_closed"].opened.get_value() is False
    value = usage(flame, "unlinked", sequences=[selector])
    assert [i["sourceName"] for i in value["items"]] == ["gone"]
    assert value["librariesOpened"][0]["name"] == "LibClosed"
    assert value["librariesOpened"][0]["closedAgain"] is True
    assert h["lib_closed"].opened.get_value() is False


# ---- review fixes: unreadable and empty keys, cross-key matching, clip budget, dedupe --------


def one_group(children: list[Any]) -> FakeFlame:
    reel = PyReel("R", "u-r", "/wt/g/r", children)
    return FakeFlame(desktop=PyDesktop(reel_groups=[PyReelGroup("G", "/wt/g", [reel], uid="u-g")]))


class UnreadablePathSegment(UsageSegment):
    @property
    def file_path(self) -> str:
        raise RuntimeError("file_path cannot be read")

    @file_path.setter
    def file_path(self, value: str) -> None:
        pass


def unreadable_world() -> FakeFlame:
    seq = make_seq("bad", "/wt/g/r/bad", [[UnreadablePathSegment("a", "/m/a.mov")]])
    clip = make_clip("c", "/wt/g/r/c", UsageSegment("z", "/m/z.mov"))
    return one_group([seq, clip])


def test_an_attribute_that_cannot_be_read_makes_the_sequence_unreadable_not_keyless() -> None:
    value = usage(unreadable_world(), "unreferenced")
    assert value["judged"] is False and value["reason"] == "unreadable"
    assert value["items"] == []
    assert value["sequenceErrors"][0]["sequence"]["name"] == "bad"
    assert "file_path cannot be read" in value["sequenceErrors"][0]["error"]


@pytest.mark.parametrize("report", ["unlinked", "shared"])
def test_the_other_reports_list_the_unreadable_sequence_in_sequence_errors(report: str) -> None:
    value = usage(unreadable_world(), report)
    assert [e["sequence"]["name"] for e in value["sequenceErrors"]] == ["bad"]
    assert "file_path cannot be read" in value["sequenceErrors"][0]["error"]


def test_a_segment_that_really_has_no_key_is_counted_and_does_not_stop_the_judgement() -> None:
    flame, _h = build_world()
    value = usage(flame, "unreferenced")
    assert value["judged"] is True
    assert value["segmentsWithoutKey"] == 2  # a4 and b4; the Gap is not counted
    assert value["judgedAgainst"]["segmentsWithoutKey"] == 2
    assert usage(flame, "unlinked")["segmentsWithoutKey"] == 2
    assert usage(flame, "shared")["segmentsWithoutKey"] == 2


def test_a_clip_is_referenced_when_any_one_of_its_identities_is_in_a_scanned_segment() -> None:
    both = make_clip("byName", "/wt/g/r/byName", UsageSegment("shared name", "/m/clip.mov"))
    both_path = make_clip("byPath", "/wt/g/r/byPath", UsageSegment("other", "/m/seg.mov"))
    stranger = make_clip("stranger", "/wt/g/r/stranger", UsageSegment("nobody", "/m/none.mov"))
    seq = make_seq(
        "s",
        "/wt/g/r/s",
        [[UsageSegment("shared name", ""), UsageSegment("seg name", "/m/seg.mov")]],
    )
    value = usage(one_group([seq, both, both_path, stranger]), "unreferenced")
    assert value["judged"] is True
    assert [(i["clip"]["name"], i["key"], i["matchedBy"]) for i in value["items"]] == [
        ("stranger", "/m/none.mov", "filePath")
    ]


def test_unreferenced_stops_when_reading_the_clips_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    class SlowClip(PyClip):
        @property
        def versions(self) -> list[PyVersion]:
            clock[0] += 10.0
            return PySequence.versions.fget(self)  # type: ignore[attr-defined,no-any-return]

    SlowClip.__name__ = "PyClip"
    seq = make_seq("s", "/wt/g/r/s", [[UsageSegment("a", "/m/a.mov")]])
    clips = [
        make_clip(f"c{i}", f"/wt/g/r/c{i}", UsageSegment(f"z{i}", f"/m/z{i}.mov")) for i in range(3)
    ]
    slow = [
        SlowClip(c.name.get_value(), wiretap_id=c.get_wiretap_node_id(), versions=c._versions)
        for c in clips
    ]
    flame = one_group([seq, *slow])
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    value = usage(flame, "unreferenced")
    assert value["judged"] is False and value["reason"] == "budget"
    assert value["items"] == [] and value["stoppedEarly"]["reason"] == "time budget"


def test_items_are_made_for_the_page_only_and_total_counts_them_all() -> None:
    reads: list[str] = []

    class Spy(PyClip):
        @property
        def attributes(self) -> list[str]:
            reads.append(str(self.name.get_value()))
            return ["name", "uid"]

        @attributes.setter
        def attributes(self, value: list[str]) -> None:
            pass

    Spy.__name__ = "PyClip"
    seq = make_seq("s", "/wt/g/r/s", [[UsageSegment("a", "/m/a.mov")]])
    spies = []
    for i in range(3):
        base = make_clip(f"c{i}", f"/wt/g/r/c{i}", UsageSegment(f"z{i}", f"/m/z{i}.mov"))
        spies.append(Spy(f"c{i}", wiretap_id=f"/wt/g/r/c{i}", versions=base._versions, uid=f"u{i}"))
    flame = one_group([seq, *spies])
    value = usage(flame, "unreferenced", limit=1, offset=1)
    assert value["total"] == 3 and value["shown"] == 1 and value["truncated"] is True
    assert [i["clip"]["name"] for i in value["items"]] == ["c1"]
    assert set(reads) == {"c1"}


class FreshWrapper:
    """A new object on every lookup, as Flame's wrappers are."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, key: str) -> Any:
        return getattr(self._inner, key)


FreshWrapper.__name__ = "PySequence"


def test_one_sequence_given_by_two_different_selectors_is_read_once() -> None:
    flame, h = build_world()
    original = flame.find_by_wiretap_node_id
    flame.find_by_wiretap_node_id = lambda node_id: (  # type: ignore[method-assign]
        FreshWrapper(original(node_id)) if node_id.endswith("seqA") else original(node_id)
    )
    by_path = {"kind": "path", "root": "desktop", "path": ["RG1", "Reel A", "seqA"]}
    value = usage(flame, "shared", sequences=[wiretap(h["seq_a"]), by_path, wiretap(h["seq_b"])])
    assert [s["name"] for s in value["scannedSequences"]] == ["seqA", "seqB"]
    assert [i["count"] for i in value["items"]] == [2, 2]
    assert "nextSequenceOffset" not in usage(
        flame,
        "shared",
        sequences=[wiretap(h["seq_a"]), by_path, wiretap(h["seq_b"])],
        max_sequences=2,
    )


def test_limit_is_at_least_1() -> None:
    op = make_registry().get(OP)
    assert op is not None
    assert validate_op_args(op, {"report": "shared", "limit": 0}).ok is False
    assert validate_op_args(op, {"report": "shared", "limit": 1}).ok is True


def test_the_description_warns_about_what_is_not_counted_and_about_a_stopped_walk() -> None:
    op = make_registry().get(OP)
    assert op is not None
    text = op.description
    assert "audio" in text and "Action" in text
    assert "scope" in text and "narrow" in text
    limit = next(p for p in op.params if p.name == "limit")
    assert "default 50" in limit.description and "shared" in limit.description
