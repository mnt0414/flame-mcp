"""`timeline.list_segments` with `all_tracks=true`: every video track of a version in one call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PySegment, PyTrack, PyVersion, build_default_tree
from flame_mcp import execution, list_fields
from flame_mcp.callargs import apply_param_defaults
from flame_mcp.compact import compact_envelope
from flame_mcp.envelope import MAX_RESPONSE_BYTES, build_envelope
from flame_mcp.journal import Journal
from flame_mcp.ops import timeline
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.runtime import Runtime
from list_fields_helpers import declared_list_fields
from op_test_helpers import run_op

LIST = "timeline.list_segments"


def _segs(prefix: str, count: int) -> list[PySegment]:
    return [PySegment(name=f"{prefix}{i}") for i in range(count)]


def _tree(counts: tuple[int, ...] = (3, 2, 0)) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    flame, h = build_default_tree()
    tracks = [PyTrack(_segs(f"t{n}s", c)) for n, c in enumerate(counts)]
    h["seq"]._versions = [PyVersion(tracks)]
    target = {"kind": "wiretap", "id": h["seq"].get_wiretap_node_id()}
    return flame, h, target


def _run(registry: Registry, flame: Any, target: Any, **extra: Any) -> dict[str, Any]:
    args = {"target": target, "limit": 50, "all_tracks": True, **extra}
    return run_op(registry, LIST, args, flame)


def _precheck(registry: Registry, **extra: Any) -> Any:
    op = registry.get(LIST)
    assert op is not None and op.precheck is not None
    args = {"target": {"kind": "wiretap", "id": "w"}, "limit": 50, **extra}
    apply_param_defaults(op, args)
    return op.precheck(args, None)


# ---- the code without all_tracks does not change ------------------------------------------------


def test_the_code_without_all_tracks_has_none_of_the_all_tracks_names(
    registry: Registry,
) -> None:
    op = registry.get(LIST)
    assert op is not None and op.to_python is not None
    target = {"kind": "wiretap", "id": "w"}
    base = {"target": target, "limit": 50}
    for extra in ({}, {"all_tracks": False}, {"include_connections": True}, {"media": "audio"}):
        args = {**base, **extra}
        apply_param_defaults(op, args)
        code = op.to_python(args)
        for name in ("_rows", "_max_bytes", "_used", "json.dumps", "trackCount"):
            assert name not in code, (extra, name)


# ---- the result ---------------------------------------------------------------------------------


def test_all_tracks_reads_every_track_with_its_own_index(registry: Registry) -> None:
    flame, h, target = _tree()
    result = _run(registry, flame, target)
    assert result["opError"] is None
    value = result["value"]
    assert value["version"] == 0 and value["trackCount"] == 3
    assert [t["track"] for t in value["tracks"]] == [0, 1, 2]
    assert [t["total"] for t in value["tracks"]] == [3, 2, 0]
    assert [i["name"] for i in value["tracks"][1]["items"]] == ["t1s0", "t1s1"]
    assert [i["index"] for i in value["tracks"][0]["items"]] == [0, 1, 2]
    assert value["tracks"][2]["items"] == []
    assert all("truncated" not in t for t in value["tracks"])
    assert value["wiretapId"] == h["seq"].get_wiretap_node_id()
    assert value["uid"] == h["seq"].uid.get_value()
    assert "note" not in value


def test_each_track_matches_a_read_of_that_track_alone(registry: Registry) -> None:
    flame, _h, target = _tree()
    value = _run(registry, flame, target)["value"]
    for entry in value["tracks"]:
        one = run_op(
            registry, LIST, {"target": target, "limit": 50, "track": entry["track"]}, flame
        )["value"]
        assert entry["items"] == one["items"] and entry["total"] == one["total"]


def test_limit_is_per_track_and_marks_only_the_tracks_it_cut(registry: Registry) -> None:
    flame, _h, target = _tree((3, 1, 2))
    tracks = _run(registry, flame, target, limit=2)["value"]["tracks"]
    assert [(t["total"], len(t["items"])) for t in tracks] == [(3, 2), (1, 1), (2, 2)]
    assert [t.get("truncated") for t in tracks] == [True, None, None]


def _item_sizes(registry: Registry, flame: Any, target: Any) -> list[int]:
    value = _run(registry, flame, target, limit=500)["value"]
    return [len(json.dumps(i)) for t in value["tracks"] for i in t["items"]]


def _capped(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, counts: tuple[int, ...], cap_items: int
) -> dict[str, Any]:
    flame, _h, target = _tree(counts)
    sizes = _item_sizes(registry, flame, target)
    monkeypatch.setattr(timeline, "ALL_TRACKS_MAX_BYTES", sum(sizes[:cap_items]))
    value: dict[str, Any] = _run(registry, flame, target)["value"]
    return value


def test_the_byte_cap_stops_in_the_middle_of_a_track(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = _capped(registry, monkeypatch, (3, 3, 2, 0), 4)
    tracks = value["tracks"]
    assert [t["total"] for t in tracks] == [3, 3, 2, 0]
    assert [len(t["items"]) for t in tracks] == [3, 1, 0, 0]
    assert [t.get("truncated") for t in tracks] == [None, True, True, None]
    assert isinstance(value["note"], str) and value["note"].isascii()
    assert str(timeline.ALL_TRACKS_MAX_BYTES) in value["note"]


def test_items_that_fit_exactly_leave_no_note_and_no_truncated(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = _capped(registry, monkeypatch, (3, 3, 2, 0), 8)
    assert [len(t["items"]) for t in value["tracks"]] == [3, 3, 2, 0]
    assert all("truncated" not in t for t in value["tracks"])
    assert "note" not in value


def test_the_first_track_alone_can_reach_the_cap(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = _capped(registry, monkeypatch, (4, 2, 0, 1), 2)
    tracks = value["tracks"]
    assert [len(t["items"]) for t in tracks] == [2, 0, 0, 0]
    assert [t.get("truncated") for t in tracks] == [True, True, None, True]
    assert [t["total"] for t in tracks] == [4, 2, 0, 1]
    assert "note" in value


def test_the_first_item_is_read_even_when_it_is_larger_than_the_cap(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    flame, _h, target = _tree((2, 1))
    monkeypatch.setattr(timeline, "ALL_TRACKS_MAX_BYTES", 10)
    value = _run(registry, flame, target)["value"]
    tracks = value["tracks"]
    assert [len(t["items"]) for t in tracks] == [1, 0]
    assert [t.get("truncated") for t in tracks] == [True, True]
    assert "note" in value


def test_300_segments_of_500_bytes_stay_under_the_response_limit(registry: Registry) -> None:
    flame, h = build_default_tree()
    pad = 100
    tracks = [PyTrack([PySegment(name=f"{'n' * pad}{i}") for i in range(100)]) for _ in range(3)]
    h["seq"]._versions = [PyVersion(tracks)]
    target = {"kind": "wiretap", "id": h["seq"].get_wiretap_node_id()}
    value = _run(registry, flame, target, limit=500)["value"]
    size = len(json.dumps(value["tracks"][0]["items"][0]))
    assert 480 <= size <= 540
    assert sum(len(t["items"]) for t in value["tracks"]) < 300
    assert len(json.dumps(build_envelope(ok=True, result=value))) < MAX_RESPONSE_BYTES


def test_a_version_out_of_range_is_selector_empty(registry: Registry) -> None:
    flame, _h, target = _tree()
    result = _run(registry, flame, target, version=3)
    assert result["opErrorCode"] == "SELECTOR_EMPTY"
    assert "version index 3" in result["opError"]


def test_the_version_argument_picks_the_version(registry: Registry) -> None:
    flame, h, target = _tree()
    h["seq"]._versions.append(PyVersion([PyTrack(_segs("v1s", 1))]))
    value = _run(registry, flame, target, version=1)["value"]
    assert value["version"] == 1 and value["trackCount"] == 1
    assert [i["name"] for i in value["tracks"][0]["items"]] == ["v1s0"]


def test_the_generated_code_is_ascii(registry: Registry) -> None:
    op = registry.get(LIST)
    assert op is not None and op.to_python is not None
    args = {"target": {"kind": "wiretap", "id": "w"}, "limit": 50, "all_tracks": True}
    apply_param_defaults(op, args)
    assert op.to_python(args).isascii()


def test_the_description_of_all_tracks_is_ascii_and_says_limit_is_per_track(
    registry: Registry,
) -> None:
    op = registry.get(LIST)
    assert op is not None
    (param,) = [p for p in op.params if p.name == "all_tracks"]
    assert param.type == "boolean" and param.default is False
    assert param.description.isascii() and op.description.isascii()
    assert "all_tracks" in op.description and "applies to each track" in op.description


# ---- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"track": 1},
        {"media": "audio"},
        {"offset": 5},
        {"include_connections": True},
    ],
)
def test_all_tracks_with_a_conflicting_argument_is_invalid_args(
    registry: Registry, extra: dict[str, Any]
) -> None:
    refusal = _precheck(registry, all_tracks=True, **extra)
    assert refusal is not None and refusal.code == "INVALID_ARGS"
    assert refusal.message.isascii()
    assert "all_tracks" in refusal.message


def test_all_tracks_alone_and_the_defaults_are_not_refused(registry: Registry) -> None:
    assert _precheck(registry, all_tracks=True) is None
    assert _precheck(registry, all_tracks=True, track=0, offset=0) is None
    assert _precheck(registry, track=2, offset=4) is None


# ---- fields and compact -------------------------------------------------------------------------


def test_fields_cut_the_items_of_every_track() -> None:
    items = [{"index": 0, "name": "a", "type": "Gap"}, {"error": "x", "index": 1}]
    result = {
        "version": 0,
        "trackCount": 2,
        "tracks": [
            {"track": 0, "total": 2, "items": items},
            {"track": 1, "total": 0, "items": []},
        ],
    }
    env = build_envelope(ok=True, result=result)
    list_fields.select_fields(declared_list_fields()[LIST], {"fields": ["name"]}, env)
    tracks = env["result"]["tracks"]
    assert tracks[0] == {"track": 0, "total": 2, "items": [{"name": "a"}, {"error": "x"}]}
    assert tracks[1] == {"track": 1, "total": 0, "items": []}
    assert env["result"]["trackCount"] == 2


def test_compact_turns_each_tracks_items_into_a_table(registry: Registry) -> None:
    flame, _h, target = _tree((3, 1, 0))
    value = _run(registry, flame, target)["value"]
    out = compact_envelope(build_envelope(ok=True, result=value))["result"]
    first, second, third = out["tracks"]
    assert first["items"]["columns"][0] == "index"
    assert len(first["items"]["rows"]) == 3
    assert [i["name"] for i in second["items"]] == ["t1s0"]
    assert third["total"] == 0


# ---- through Runtime.call ---------------------------------------------------------------------


TARGET = {"kind": "wiretap", "id": "/p/x"}


def _fake_all_tracks_result() -> dict[str, Any]:
    def item(i: int) -> dict[str, Any]:
        return {key: f"{key}{i}" for key in declared_list_fields()[LIST].fields}

    return {
        "version": 0,
        "trackCount": 2,
        "tracks": [
            {"track": 0, "total": 2, "items": [item(0), {"error": "unreadable"}]},
            {"track": 1, "total": 0, "items": []},
        ],
        "uid": "u",
    }


@pytest.fixture
def runtime(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Runtime, list[str]]:
    calls: list[str] = []

    def fake_run_op(op: Any, args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(op.name)
        return build_envelope(ok=True, result=_fake_all_tracks_result())

    monkeypatch.setattr(execution, "run_op", fake_run_op)
    return Runtime(registry, Policy.from_env({}), Journal(tmp_path / "journal")), calls


def test_fields_cut_every_tracks_items_through_runtime_call(
    runtime: tuple[Runtime, list[str]],
) -> None:
    rt, calls = runtime
    env = rt.call(LIST, {"target": TARGET, "all_tracks": True, "fields": ["name", "type"]})
    assert env["ok"] is True and calls == [LIST]
    tracks = env["result"]["tracks"]
    assert tracks[0]["items"] == [{"name": "name0", "type": "type0"}, {"error": "unreadable"}]
    assert tracks[1] == {"track": 1, "total": 0, "items": []}
    assert env["result"]["uid"] == "u" and env["result"]["trackCount"] == 2


def test_an_unknown_field_with_all_tracks_is_invalid_args_before_anything_runs(
    runtime: tuple[Runtime, list[str]],
) -> None:
    rt, calls = runtime
    env = rt.call(LIST, {"target": TARGET, "all_tracks": True, "fields": ["name", "nmae"]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "nmae" in env["error"] and calls == []


def test_the_track_refusal_says_zero_or_left_out(registry: Registry) -> None:
    refusal = _precheck(registry, all_tracks=True, track=1)
    assert refusal is not None and "must be 0 or left out" in refusal.message
