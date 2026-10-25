"""`desktop.list_all`: the walk over the Desktop and the libraries, run against
`fake_media_tree.FakeFlame` (no bridge)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from fake_media_tree import (
    FakeFlame,
    PyBatch,
    PyClip,
    PyDesktop,
    PyFolder,
    PyLibrary,
    PyReel,
    PyReelGroup,
    PySequence,
)
from flame_mcp.ops import register_all
from flame_mcp.opschema import validate_op_args
from flame_mcp.registry import Registry
from op_test_helpers import run_op

OP = "desktop.list_all"


def make_registry() -> Registry:
    registry = Registry()
    register_all(registry)
    return registry


class WatchedBatch(PyBatch):
    """A Batch group that records every read of what is inside it."""

    INSIDE = ("nodes", "reels", "shelf_reels", "batch_iterations")

    def __init__(self, name: str, uid: str) -> None:
        self.inside_reads: list[str] = []
        super().__init__(name, uid)
        self.inside_reads.clear()

    def __getattribute__(self, key: str) -> Any:
        if key in WatchedBatch.INSIDE:
            object.__getattribute__(self, "inside_reads").append(key)
        return object.__getattribute__(self, key)


WatchedBatch.__name__ = "PyBatch"


class WatchedLibrary(PyLibrary):
    """A library that records every read of what is inside it."""

    INSIDE = ("children", "reels", "clips", "sequences", "batch_groups")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.inside_reads: list[str] = []
        super().__init__(*args, **kwargs)
        self.inside_reads.clear()

    def __getattribute__(self, key: str) -> Any:
        if key in WatchedLibrary.INSIDE:
            object.__getattribute__(self, "inside_reads").append(key)
        return object.__getattribute__(self, key)


WatchedLibrary.__name__ = "PyLibrary"


class UnreadableOpenedLibrary(WatchedLibrary):
    """A library whose `opened` flag cannot be read."""

    @property
    def opened(self) -> Any:
        raise RuntimeError("opened cannot be read")

    @opened.setter
    def opened(self, value: Any) -> None:
        pass


UnreadableOpenedLibrary.__name__ = "PyLibrary"


class BrokenFolder(PyFolder):
    @property
    def children(self) -> list[Any]:
        raise RuntimeError("folder cannot be read")


BrokenFolder.__name__ = "PyFolder"


def build_world() -> tuple[FakeFlame, dict[str, Any]]:
    c_a1 = PyClip("cA1", wiretap_id="/wt/rg1/a/cA1", uid="u-ca1")
    s_a2 = PySequence("sA2", wiretap_id="/wt/rg1/a/sA2", uid="u-sa2")
    c_b1 = PyClip("cB1", wiretap_id="/wt/rg1/b/cB1", uid="u-cb1")
    reel_a = PyReel("Reel A", "u-ra", "/wt/rg1/a", [c_a1, s_a2])
    reel_b = PyReel("Reel B", "u-rb", "/wt/rg1/b", [c_b1])
    rg1 = PyReelGroup("RG1", "/wt/rg1", [reel_a, reel_b], uid="u-rg1")
    rg2 = PyReelGroup("RG2", "/wt/rg2", [PyReel("R2", "u-r2", "/wt/rg2/r2")], uid="u-rg2")
    b1 = WatchedBatch("B1", "u-b1")
    b2 = WatchedBatch("B2", "u-b2")

    dc1 = PyClip("dc1", wiretap_id="/wt/lo/f1/f2/deep/dc1", uid="u-dc1")
    deep = PyReel("DeepR", "u-deep", "/wt/lo/f1/f2/deep", [dc1])
    folder2 = PyFolder("F2", "u-f2", "/wt/lo/f1/f2", [deep])
    fc1 = PyClip("fc1", wiretap_id="/wt/lo/f1/fr/fc1", uid="u-fc1")
    folder_reel = PyReel("FR", "u-fr", "/wt/lo/f1/fr", [fc1])
    folder1 = PyFolder("F1", "u-f1", "/wt/lo/f1", [folder_reel, folder2])
    lc1 = PyClip("lc1", wiretap_id="/wt/lo/lr/lc1", uid="u-lc1")
    lib_reel = PyReel("LR", "u-lr", "/wt/lo/lr", [lc1])
    lib_batch = PyBatch("LB", "u-lb")
    lib_open = PyLibrary("LibOpen", "u-lo", "/wt/lo", [folder1, lib_reel, lib_batch])

    cc1 = PyClip("cc1", wiretap_id="/wt/lc/cr/cc1", uid="u-cc1")
    lib_closed = WatchedLibrary(
        "LibClosed", "u-lc", "/wt/lc", [PyReel("CR", "u-cr", "/wt/lc/cr", [cc1])]
    )
    lib_closed.close()

    desktop = PyDesktop(batch_groups=[b1, b2], reel_groups=[rg1, rg2])
    flame = FakeFlame(libraries=[lib_open, lib_closed], desktop=desktop)
    return flame, {"b1": b1, "b2": b2, "lib_open": lib_open, "lib_closed": lib_closed}


FULL_ORDER = [
    ("PyReelGroup", ["RG1"]),
    ("PyReel", ["RG1", "Reel A"]),
    ("PyClip", ["RG1", "Reel A", "cA1"]),
    ("PySequence", ["RG1", "Reel A", "sA2"]),
    ("PyReel", ["RG1", "Reel B"]),
    ("PyClip", ["RG1", "Reel B", "cB1"]),
    ("PyReelGroup", ["RG2"]),
    ("PyReel", ["RG2", "R2"]),
    ("PyBatch", ["B1"]),
    ("PyBatch", ["B2"]),
    ("PyLibrary", ["LibOpen"]),
    ("PyFolder", ["LibOpen", "F1"]),
    ("PyReel", ["LibOpen", "F1", "FR"]),
    ("PyClip", ["LibOpen", "F1", "FR", "fc1"]),
    ("PyFolder", ["LibOpen", "F1", "F2"]),
    ("PyReel", ["LibOpen", "F1", "F2", "DeepR"]),
    ("PyClip", ["LibOpen", "F1", "F2", "DeepR", "dc1"]),
    ("PyReel", ["LibOpen", "LR"]),
    ("PyClip", ["LibOpen", "LR", "lc1"]),
    ("PyBatch", ["LibOpen", "LB"]),
    ("PyLibrary", ["LibClosed"]),
]


def list_all(flame: FakeFlame, **args: Any) -> dict[str, Any]:
    result = run_op(make_registry(), OP, {"limit": 500, **args}, flame)
    assert result["opError"] is None, result
    value: dict[str, Any] = result["value"]
    return value


def shape(value: dict[str, Any]) -> list[tuple[str, list[str]]]:
    return [(item["type"], item["path"]) for item in value["items"]]


def test_walks_the_desktop_then_the_batch_groups_then_each_library_in_a_fixed_order() -> None:
    flame, _h = build_world()
    value = list_all(flame)
    assert shape(value) == FULL_ORDER
    assert value["total"] == len(FULL_ORDER) and value["truncated"] is False
    assert shape(list_all(flame)) == FULL_ORDER


def test_an_item_has_the_find_shape_plus_path_location_and_library() -> None:
    flame, _h = build_world()
    items = {tuple(i["path"]): i for i in list_all(flame)["items"]}
    clip = items[("RG1", "Reel A", "cA1")]
    assert clip == {
        "type": "PyClip",
        "name": "cA1",
        "uid": "u-ca1",
        "selector": {"kind": "wiretap", "id": "/wt/rg1/a/cA1"},
        "parent": {"type": "PyReel", "name": "Reel A"},
        "path": ["RG1", "Reel A", "cA1"],
        "location": "desktop",
        "library": None,
    }
    deep = items[("LibOpen", "F1", "F2", "DeepR", "dc1")]
    assert deep["location"] == "library" and deep["library"] == "LibOpen"
    assert deep["parent"] == {"type": "PyReel", "name": "DeepR"}
    assert items[("LibOpen",)]["library"] == "LibOpen"
    batch = items[("B1",)]
    assert (batch["name"], batch["uid"], batch["selector"]) == ("B1", "u-b1", None)
    assert batch["location"] == "desktop" and batch["library"] is None


def test_types_keeps_the_named_types_but_still_walks_into_containers() -> None:
    flame, _h = build_world()
    value = list_all(flame, types=["PyClip", "PySequence"])
    assert [i["name"] for i in value["items"]] == ["cA1", "sA2", "cB1", "fc1", "dc1", "lc1"]
    assert value["total"] == 6
    only_batch = list_all(flame, types=["PyBatch"])
    assert [i["path"] for i in only_batch["items"]] == [["B1"], ["B2"], ["LibOpen", "LB"]]


def test_scope_desktop_stops_before_the_libraries() -> None:
    flame, _h = build_world()
    value = list_all(flame, scope="desktop")
    assert shape(value) == FULL_ORDER[:10]
    assert value["closedLibraries"] == []
    assert {i["location"] for i in value["items"]} == {"desktop"}


def test_scope_libraries_starts_at_the_first_library() -> None:
    flame, _h = build_world()
    value = list_all(flame, scope="libraries")
    assert shape(value) == FULL_ORDER[10:]
    assert {i["location"] for i in value["items"]} == {"library"}
    assert value["batchGroupsSkipped"] == 1


def test_scope_defaults_to_all() -> None:
    flame, _h = build_world()
    assert shape(list_all(flame)) == shape(list_all(flame, scope="all"))


def test_paging_slices_the_fixed_order() -> None:
    flame, _h = build_world()
    first = list_all(flame, limit=8, offset=0)
    second = list_all(flame, limit=8, offset=8)
    third = list_all(flame, limit=8, offset=16)
    assert (first["total"], first["shown"], first["truncated"]) == (21, 8, True)
    assert (second["offset"], second["shown"], second["truncated"]) == (8, 8, True)
    assert (third["shown"], third["truncated"]) == (5, False)
    assert shape(first) + shape(second) + shape(third) == FULL_ORDER
    assert "stoppedEarly" not in first and "nextOffset" not in first


def test_a_closed_library_is_listed_empty_and_named_without_being_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flame, handles = build_world()
    closed = handles["lib_closed"]
    calls: list[str] = []

    def spy_open() -> bool:
        calls.append("open")
        return True

    monkeypatch.setattr(closed, "open", spy_open)
    before = closed.opened.get_value()
    value = list_all(flame)
    assert before is False and closed.opened.get_value() is False
    assert calls == []
    assert value["closedLibraries"] == ["LibClosed"]
    assert not [i for i in value["items"] if i["path"][0] == "LibClosed" and len(i["path"]) > 1]
    assert handles["lib_open"].opened.get_value() is True


def test_the_inside_of_a_closed_library_is_never_read() -> None:
    flame, handles = build_world()
    list_all(flame)
    assert handles["lib_closed"].inside_reads == []
    list_all(flame, scope="libraries", types=["PyClip"])
    assert handles["lib_closed"].inside_reads == []


def test_the_inside_of_an_open_library_is_read() -> None:
    flame, handles = build_world()
    handles["lib_closed"].open()
    list_all(flame)
    assert "children" in handles["lib_closed"].inside_reads


def test_a_library_whose_opened_flag_cannot_be_read_is_closed_and_not_read() -> None:
    lib = UnreadableOpenedLibrary(
        "LibUnknown",
        "u-lu",
        "/wt/lu",
        [PyReel("UR", "u-ur", "/wt/lu/ur", [PyClip("uc", wiretap_id="/wt/lu/ur/uc", uid="u-uc")])],
    )
    flame = FakeFlame(libraries=[lib], desktop=PyDesktop())
    value = list_all(flame)
    assert lib.inside_reads == []
    assert value["closedLibraries"] == ["LibUnknown"]
    assert shape(value) == [("PyLibrary", ["LibUnknown"])]


def test_no_library_is_closed_in_a_world_with_open_libraries_only() -> None:
    flame, handles = build_world()
    handles["lib_closed"].open()
    value = list_all(flame)
    assert value["closedLibraries"] == []
    assert ("PyClip", ["LibClosed", "CR", "cc1"]) in shape(value)


def test_batch_groups_are_listed_by_name_and_counted_and_never_read_inside() -> None:
    flame, handles = build_world()
    value = list_all(flame)
    assert value["batchGroupsSkipped"] == 3
    assert handles["b1"].inside_reads == [] and handles["b2"].inside_reads == []
    assert list_all(flame, types=["PyClip"])["batchGroupsSkipped"] == 3


def test_a_failure_inside_a_container_is_an_item_and_the_walk_goes_on() -> None:
    flame, handles = build_world()
    broken = BrokenFolder("Broken", "u-broken", "/wt/lo/broken")
    handles["lib_open"]._children.insert(0, broken)
    value = list_all(flame, scope="libraries")
    names = [i["path"] for i in value["items"]]
    assert ["LibOpen", "Broken"] in names
    errors = [i for i in value["items"] if "error" in i]
    assert len(errors) == 1
    assert (
        errors[0]["path"] == ["LibOpen", "Broken"] and "folder cannot be read" in errors[0]["error"]
    )
    assert errors[0]["location"] == "library" and errors[0]["library"] == "LibOpen"
    assert ["LibOpen", "LR", "lc1"] in names


def test_an_error_item_survives_the_types_filter() -> None:
    flame, handles = build_world()
    handles["lib_open"]._children.insert(0, BrokenFolder("Broken", "u-broken", "/wt/lo/broken"))
    value = list_all(flame, types=["PyClip"])
    assert [i for i in value["items"] if "error" in i]


def ticking(monkeypatch: pytest.MonkeyPatch, seconds_per_call: float) -> list[float]:
    """`time.monotonic` that moves `seconds_per_call` on every call."""
    calls = itertools.count()
    seen: list[float] = []

    def monotonic() -> float:
        now = next(calls) * seconds_per_call
        seen.append(now)
        return now

    monkeypatch.setattr("time.monotonic", monotonic)
    return seen


def test_the_walk_stops_after_the_time_budget_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flame, _h = build_world()
    ticking(monkeypatch, 3.0)
    value = list_all(flame)
    assert value["stoppedEarly"] == {"reason": "time budget", "budgetS": 5.0, "read": 2}
    assert shape(value) == FULL_ORDER[:5]
    assert value["total"] == 5 and value["truncated"] is True
    assert "nextOffset" not in value


def test_a_stopped_walk_gives_next_offset_while_listed_items_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flame, _h = build_world()
    ticking(monkeypatch, 3.0)
    value = list_all(flame, limit=2)
    assert shape(value) == FULL_ORDER[:2]
    assert value["truncated"] is True and value["nextOffset"] == 2
    assert value["stoppedEarly"]["read"] == 2


def test_a_stopped_walk_called_again_from_its_next_offset_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flame, _h = build_world()
    ticking(monkeypatch, 3.0)
    first = list_all(flame, limit=2)
    ticking(monkeypatch, 3.0)
    second = list_all(flame, limit=2, offset=first["nextOffset"])
    assert shape(second) == FULL_ORDER[2:4]
    assert second["nextOffset"] == 4 and "offsetBeyondReached" not in second
    ticking(monkeypatch, 3.0)
    third = list_all(flame, limit=2, offset=second["nextOffset"])
    assert shape(third) == FULL_ORDER[4:5]
    assert "nextOffset" not in third and "offsetBeyondReached" not in third


def test_an_offset_past_a_walk_that_stopped_earlier_than_the_last_one_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flame, _h = build_world()
    ticking(monkeypatch, 3.0)
    first = list_all(flame, limit=4)
    assert first["total"] == 5 and first["nextOffset"] == 4
    ticking(monkeypatch, 100.0)
    second = list_all(flame, limit=4, offset=first["nextOffset"])
    assert second["total"] == 2 and second["shown"] == 0 and second["items"] == []
    assert second["truncated"] is True and "nextOffset" not in second
    assert second["offsetBeyondReached"] is True
    assert "scope" in second["hint"] and "types" in second["hint"]
    assert second["stoppedEarly"]["read"] == 1


def test_an_offset_past_the_end_of_a_whole_walk_is_an_empty_page_without_the_flag() -> None:
    flame, _h = build_world()
    value = list_all(flame, offset=100)
    assert value["total"] == len(FULL_ORDER) and value["items"] == []
    assert value["truncated"] is False
    for key in ("offsetBeyondReached", "hint", "nextOffset", "stoppedEarly"):
        assert key not in value


def test_the_first_container_is_read_however_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    flame, _h = build_world()
    ticking(monkeypatch, 100.0)
    value = list_all(flame)
    assert value["stoppedEarly"] == {"reason": "time budget", "budgetS": 5.0, "read": 1}
    assert shape(value) == FULL_ORDER[:2]


def test_a_walk_inside_the_budget_has_no_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    flame, _h = build_world()
    ticking(monkeypatch, 0.01)
    value = list_all(flame)
    assert "stoppedEarly" not in value and shape(value) == FULL_ORDER


def test_the_op_is_a_main_thread_read_that_cannot_be_undone() -> None:
    op = make_registry().get(OP)
    assert op is not None
    assert (op.category, op.effect, op.undo, op.thread) == ("desktop", "read", "none", "main")


@pytest.mark.parametrize(
    "bad",
    [
        {"types": "PyClip"},
        {"types": [1]},
        {"types": ["PyClip", None]},
        {"scope": "everything"},
        {"scope": 1},
        {"offset": -1},
    ],
)
def test_bad_arguments_are_invalid_args(bad: dict[str, Any]) -> None:
    op = make_registry().get(OP)
    assert op is not None
    assert validate_op_args(op, bad).ok is False, bad


def test_good_arguments_pass_validation() -> None:
    op = make_registry().get(OP)
    assert op is not None
    good = {"types": ["PyClip"], "scope": "libraries", "limit": 5, "offset": 2}
    assert validate_op_args(op, good).ok is True
    assert validate_op_args(op, {}).ok is True
