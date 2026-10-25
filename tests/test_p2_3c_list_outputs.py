"""`list_outputs` of scripts/p2_3c_render_e2e.py: the paging of batch.list(outputs=true), against a
fake `do` (no Flame, no MCP server)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str) -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


script = load("p2_3c_render_e2e")


def page(items: list[str], total: int, read: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "items": [{"uid": uid, "outputs": []} for uid in items],
        "total": total,
    }
    if read is not None:
        result["stoppedEarly"] = {"read": read}
    return {"ok": True, "result": result}


def run_list(
    monkeypatch: pytest.MonkeyPatch, pages: dict[int, dict[str, Any]], wanted: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[int]]:
    offsets: list[int] = []

    async def fake_do(_session: Any, _ctx: Any, op: str, args: dict[str, Any]) -> dict[str, Any]:
        assert op == "batch.list"
        offsets.append(args["offset"])
        return pages.get(args["offset"], {"ok": True, "result": {"items": [], "total": 0}})

    monkeypatch.setattr(script, "do", fake_do)
    found, facts = anyio.run(script.list_outputs, None, None, wanted)
    return found, facts, offsets


def test_offsets_advance_by_read_after_two_stopped_early_pages_and_every_group_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        0: page(["u1", "u2"], total=10, read=3),
        3: page(["u3"], total=10, read=4),
        7: page(["u4", "u5"], total=10),
    }
    wanted = {"A": "u1", "B": "u3", "C": "u5"}
    found, facts, offsets = run_list(monkeypatch, pages, wanted)
    assert offsets == [0, 3, 7]
    assert sorted(found) == ["A", "B", "C"]
    assert facts["pages"] == 3
    assert [s["offset"] for s in facts["stoppedEarly"]] == [0, 3]
    assert facts["errors"] == []


def test_a_uid_that_never_appears_stops_at_total_and_its_group_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        0: page(["u1", "u2"], total=4),
        2: page(["u3", "u4"], total=4),
    }
    wanted = {"A": "u1", "Z": "never"}
    found, facts, offsets = run_list(monkeypatch, pages, wanted)
    assert offsets == [0, 2]
    assert sorted(found) == ["A"]
    assert facts["pages"] == 2
    assert facts["errors"] == []


def test_an_empty_page_stops_the_paging_and_is_recorded_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        0: page(["u1"], total=5),
        1: {"ok": True, "result": {"items": [], "total": 5}},
    }
    wanted = {"A": "u1", "B": "u9"}
    found, facts, offsets = run_list(monkeypatch, pages, wanted)
    assert offsets == [0, 1]
    assert sorted(found) == ["A"]
    assert facts["errors"] == ["an empty page"]
