"""Tests the pure parts of the scripts/p2_4_*.py e2e scripts (no Flame, no MCP server).

They are the helpers the two e2e scripts share (scripts/p2_4_common.py,
scripts/p2_4_scenario4_e2e.py, scripts/p2_4_stress_e2e.py).

What the e2e scripts do against a Flame is measured by running them; a test here says only what
the helpers compute."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str) -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


common = load("p2_4_common")
scenario = load("p2_4_scenario4_e2e")
stress = load("p2_4_stress_e2e")


def envelope(
    op: str | None, ok: bool, *, steps: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    env: dict[str, Any] = {"ok": ok, "result": None}
    if op is not None:
        env["execution"] = {"op": op}
    if steps is not None:
        env["result"] = {"steps": steps}
    return {"tool": "flame_do", "env": env}


def test_summarize_ms_of_nothing_is_all_none() -> None:
    assert common.summarize_ms([]) == {
        "count": 0,
        "min": None,
        "median": None,
        "p95": None,
        "max": None,
        "mean": None,
    }


def test_summarize_ms_uses_nearest_rank_for_p95() -> None:
    values = [float(n) for n in range(1, 101)]
    summary = common.summarize_ms(list(reversed(values)))
    assert (summary["count"], summary["min"], summary["max"]) == (100, 1.0, 100.0)
    assert summary["p95"] == 95.0
    assert summary["median"] == 50.5
    assert common.summarize_ms([7.0, 1.0, 3.0])["median"] == 3.0
    assert common.summarize_ms([2.5])["p95"] == 2.5
    assert common.summarize_ms([1.0, 2.0])["p95"] == 2.0


def test_value_like_writes_the_type_the_attribute_holds() -> None:
    assert common.value_like(3, 7.0) == 7 and isinstance(common.value_like(3, 7.0), int)
    assert isinstance(common.value_like(3.5, 7), float)
    assert common.value_like(False, 1) is True
    assert common.value_like("x", 9) == "9"


def test_ops_used_lists_direct_flame_do_calls_only() -> None:
    results = {
        "_envelopes": [
            envelope("node.list", True),
            envelope("node.list", False),
            {"tool": "flame_catalog", "env": {"execution": {"op": "batch.list"}}},
            envelope(None, True),
        ]
    }
    assert common.ops_used(results) == ["node.list"]


def test_local_list_of_a_missing_directory_is_none(tmp_path: Path) -> None:
    assert common.local_list(tmp_path / "nope") is None
    (tmp_path / "b").write_text("")
    (tmp_path / "a").write_text("")
    assert common.local_list(tmp_path) == ["a", "b"]


def test_scenario_frames_are_the_numbered_files_without_a_dot() -> None:
    names = ["s4_write_a000001.exr", "s4_write_a000002.exr", "s4_write_a.clip", "other000001.exr"]
    assert scenario.frames_of(names, "s4_write_a") == names[:2]
    assert (
        scenario.frames_of(["s4_write_a.000001.exr", "s4_write_ab000001.exr"], "s4_write_a") == []
    )
    assert scenario.frames_of(None, "x") == []
    assert scenario.expected_frames("b") == [f"s4_write_b{n:06d}.exr" for n in range(1, 5)]


def test_stress_timing_table_groups_by_kind() -> None:
    samples = [
        stress.Sample("a", 10.0, True, None, 4, {}),
        stress.Sample("a", 30.0, True, None, 6, {}),
        stress.Sample("b", 5.0, False, "INVALID_ARGS", None, {"error": "bad"}),
    ]
    table = stress.timing_table(samples)
    assert table["overall"]["clientMs"]["count"] == 3
    assert table["a"]["clientMs"]["max"] == 30.0
    assert table["b"]["serverMs"]["count"] == 0
    assert stress.failures(samples) == [{"kind": "b", "errorCode": "INVALID_ARGS", "error": "bad"}]
    assert stress.compact(table) == table["overall"]


def test_stress_fatal_codes_are_the_three_bridge_losses() -> None:
    assert stress.FATAL_CODES == {"BRIDGE_LOST", "BRIDGE_UNAVAILABLE", "TIMEOUT"}
