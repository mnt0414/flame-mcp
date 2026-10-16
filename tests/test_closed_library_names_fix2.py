"""A library named by a NAME STRING argument (the checkpoint library, `_library`) is opened for the
call and closed again like one named by a selector; a read that cannot tell a closed library from
an empty one stops instead of reporting "gone"; the notes the library open / close adds are never
lost (the notes of the other write runs)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp import execution, library_open, timeline_checkpoints
from flame_mcp.library_open import library_refs, merge_into_envelope
from journal_test_helpers import Rig
from p32b_helpers import PURGE, S1, S2, make_p32b_rig
from timeline_p32a_helpers import stash_entries, stash_library, take_checkpoint

OLD = "2026-08-01T00:00:00.000+00:00"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def old_checkpoint(rig: Rig, monkeypatch: pytest.MonkeyPatch, target: dict[str, Any]) -> str:
    with monkeypatch.context() as patch:
        patch.setattr(timeline_checkpoints, "now_iso", lambda: OLD)
        return str(take_checkpoint(rig, target)["checkpoint"])


def purge(rig: Rig, **args: Any) -> dict[str, Any]:
    """A real run (dry_run=False) first asks for the dry run's token, like a caller must."""
    if args.get("dry_run") is False and "token" not in args:
        dry = rig.do(PURGE, {k: v for k, v in args.items() if k != "dry_run"})
        if dry["ok"] is not True:
            return dry
        args = {**args, "token": dry["result"]["token"]}
    return rig.do(PURGE, args)


def purge_records(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r["type"] == "purge"]


def closed_stash(rig: Rig) -> Any:
    lib = stash_library(rig)
    assert lib is not None
    lib.close()
    return lib


def test_a_library_name_argument_is_a_reference() -> None:
    refs = library_refs({"_library": "flame_mcp_checkpoints", "x": {"_library": "other"}})
    assert refs["names"] == ["flame_mcp_checkpoints", "other"]
    assert library_refs({"_library": ""}) == {"uids": [], "names": [], "wiretapIds": []}


def test_a_dry_purge_of_a_closed_checkpoint_library_does_not_call_the_copies_gone(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = old_checkpoint(rig, monkeypatch, S1)
    lib = closed_stash(rig)
    env = purge(rig)
    assert env["ok"] is True, env
    result = env["result"]
    assert [c["id"] for c in result["candidates"]] == [first]
    assert result["candidates"][0]["exists"] is True and result["missing"] == []
    assert lib.opened.get_value() is False and purge_records(rig) == []
    assert result["librariesOpened"][0]["closedAgain"] is True
    assert any("closed again" in n for n in result["notes"])


def test_a_purge_of_a_closed_checkpoint_library_deletes_what_it_should_and_closes_it_again(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = old_checkpoint(rig, monkeypatch, S1)
    second = old_checkpoint(rig, monkeypatch, S2)
    lib = closed_stash(rig)
    env = purge(rig, dry_run=False)
    assert env["ok"] is True, env
    record = purge_records(rig)[0]
    assert sorted(record["purged"]) == sorted([first, second]) and record.get("gone") in (None, [])
    assert lib.opened.get_value() is False
    lib.open()
    assert stash_entries(rig) == []


def test_a_checkpoint_into_a_closed_checkpoint_library_works(rig: Rig) -> None:
    take_checkpoint(rig, S1)
    lib = closed_stash(rig)
    take_checkpoint(rig, S2)
    assert lib.opened.get_value() is False
    lib.open()
    assert len(stash_entries(rig)) == 2


def test_the_flame_side_stops_instead_of_reading_a_closed_library_as_empty(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop: even when no reference is found (so the library is not opened), a purge does
    not take a closed library's empty listing for "every copy is gone"."""
    old_checkpoint(rig, monkeypatch, S1)
    lib = closed_stash(rig)
    monkeypatch.setattr(
        execution, "library_refs", lambda args: {"uids": [], "names": [], "wiretapIds": []}
    )
    env = purge(rig, dry_run=False)
    assert env["ok"] is False, env
    assert purge_records(rig) == [] and lib.opened.get_value() is False
    lib.open()
    assert len(stash_entries(rig)) == 1


def test_a_stash_library_whose_state_cannot_be_read_stops_the_purge(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    lib = stash_library(rig)
    assert lib is not None
    lib.opened.set_value(None)
    env = purge(rig, dry_run=False)
    assert env["ok"] is False and purge_records(rig) == []
    lib.opened.set_value(True)
    assert len(stash_entries(rig)) == 1


@pytest.mark.parametrize(
    "module_name",
    ["desktop_delete_run", "timeline_create_run", "segment_extract_run", "desktop_media_run"],
)
def test_the_other_write_runs_keep_the_notes_that_are_already_in_the_result(
    module_name: str,
) -> None:
    import importlib

    module = importlib.import_module(f"flame_mcp.{module_name}")
    notes_fn = module._notes
    result: dict[str, Any] = {
        "deleted": [1],
        "created": {"uid": "u"},
        "imported": [{"uid": "u"}] * 2,
        "requested": 1,
        "returned": 2,
        "notes": ["library note"],
    }
    env = {"result": result}

    class Op:
        name = "mediahub.import"

    notes_fn(Op(), {}, env)
    assert result["notes"][0] == "library note" and len(result["notes"]) >= 2


def test_merge_into_envelope_makes_a_result_when_there_is_none() -> None:
    env: dict[str, Any] = {"ok": False, "result": None}
    merge_into_envelope(env, {"librariesOpened": [{"uid": "u", "name": "L"}], "notes": ["n"]})
    assert env["result"] == {"librariesOpened": [{"uid": "u", "name": "L"}], "notes": ["n"]}
    plain: dict[str, Any] = {"ok": False, "result": None}
    merge_into_envelope(plain, {})
    assert plain["result"] is None
    _ = library_open
