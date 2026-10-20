"""Tests `journal.purge_checkpoints`.

Only what the journal records and only what is older than the cutoff is deleted, a dry run (the
default) deletes nothing, what the stash library holds that the journal does not account for is
reported and never deleted, Flame is asked again about the uid and the name, a purged checkpoint
cannot be restored, and the journal's own cleanup keeps the file of a checkpoint that was not
purged. Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import timeline_checkpoints
from flame_mcp.journal import JOURNAL_MAX_AGE_S, Journal
from flame_mcp.types import NO_DEFAULT
from journal_test_helpers import Rig
from p32b_helpers import PURGE, S1, S2, S3, delete, make_p32b_rig, uid
from timeline_p32a_helpers import (
    a_seg,
    checkpoints,
    reel_names,
    revert,
    stash_entries,
    stash_library,
    take_checkpoint,
    trim,
)

OLD = "2026-08-01T00:00:00.000+00:00"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def old_checkpoint(rig: Rig, monkeypatch: pytest.MonkeyPatch, target: dict[str, Any]) -> str:
    """A checkpoint the journal says was taken long ago (at OLD)."""
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


def stash_names(rig: Rig) -> list[str]:
    return sorted(str(e.name.get_value()) for e in stash_entries(rig))


def plant(rig: Rig, source: str = "s3") -> Any:
    """A copy in the stash reel that no checkpoint accounts for (made by hand)."""
    lib = stash_library(rig)
    assert lib is not None
    return rig.flame.media_panel.copy(rig.handles[source], lib.reels[0])[0]


class DeleteSpy:
    def __init__(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        self.names: list[str] = []
        real = rig.flame.delete

        def spy(obj: Any, confirm: bool = True) -> bool:
            self.names.append(str(obj.name.get_value()))
            return bool(real(obj, confirm))

        monkeypatch.setattr(rig.flame, "delete", spy)


def test_dry_run_is_the_default_and_deletes_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = old_checkpoint(rig, monkeypatch, S1)
    second = old_checkpoint(rig, monkeypatch, S2)
    recent = str(take_checkpoint(rig, S3)["checkpoint"])
    spy = DeleteSpy(rig, monkeypatch)
    before = stash_names(rig)
    moves = len(rig.flame.media_panel.move_calls)
    env = purge(rig)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["dryRun"] is True and result["olderThanDays"] == 30
    assert [c["id"] for c in result["candidates"]] == [first, second]
    assert result["count"] == 2 and result["missing"] == [] and result["unknown"] == []
    row = result["candidates"][0]
    assert row["ts"] == OLD and row["sourceName"] == "S1" and row["exists"] is True
    assert row["stash"]["library"] == timeline_checkpoints.checkpoint_library_name()
    assert row["stash"]["reel"].startswith("timeline_") and row["nameMatches"] is True
    assert recent not in [c["id"] for c in result["candidates"]]
    assert spy.names == [] and stash_names(rig) == before
    assert len(rig.flame.media_panel.move_calls) == moves and purge_records(rig) == []


def test_dry_run_reports_what_the_library_holds_that_no_checkpoint_accounts_for(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    stray = plant(rig)
    result = purge(rig, older_than_days=0)["result"]
    assert result["count"] == 1
    assert [u["uid"] for u in result["unknown"]] == [uid(stray)]
    assert result["unknown"][0]["name"] == "S3" and result["unknown"][0]["reel"].startswith(
        "timeline_"
    )
    assert result["unknown"][0]["type"] == "PySequence"


def test_nothing_is_a_candidate_before_the_cutoff(rig: Rig) -> None:
    take_checkpoint(rig, S1)
    result = purge(rig)["result"]
    assert result["candidates"] == [] and result["count"] == 0
    assert purge(rig, older_than_days=0)["result"]["count"] == 1


def test_without_a_stash_library_there_is_nothing_to_do(rig: Rig) -> None:
    result = purge(rig, older_than_days=0)["result"]
    assert (
        result["libraryFound"] is False and result["candidates"] == [] and result["unknown"] == []
    )


def test_the_arguments_are_checked(rig: Rig) -> None:
    assert purge(rig, older_than_days=-1)["errorCode"] == "INVALID_ARGS"
    assert purge(rig, older_than_days=1.5)["errorCode"] == "INVALID_ARGS"
    assert purge(rig, dry_run="no")["errorCode"] == "INVALID_ARGS"


def test_the_dry_run_returns_a_token_that_does_not_change_while_the_candidates_do_not(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    first = purge(rig)["result"]["token"]
    assert isinstance(first, str) and len(first) >= 16
    assert purge(rig)["result"]["token"] == first
    old_checkpoint(rig, monkeypatch, S2)
    assert purge(rig)["result"]["token"] != first


def test_a_real_run_without_a_token_is_refused_and_deletes_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    spy = DeleteSpy(rig, monkeypatch)
    before = stash_names(rig)
    env = rig.do(PURGE, {"dry_run": False})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "dry_run" in env["error"] and "token" in env["error"]
    assert spy.names == [] and stash_names(rig) == before and purge_records(rig) == []


def test_a_real_run_with_a_token_that_is_not_the_current_one_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    spy = DeleteSpy(rig, monkeypatch)
    env = rig.do(PURGE, {"dry_run": False, "token": "0" * 32})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "dry run" in env["error"]
    assert spy.names == [] and purge_records(rig) == []


def test_a_token_from_before_the_candidates_changed_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    token = purge(rig)["result"]["token"]
    old_checkpoint(rig, monkeypatch, S2)
    spy = DeleteSpy(rig, monkeypatch)
    env = rig.do(PURGE, {"dry_run": False, "token": token})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert spy.names == [] and stash_names(rig) == ["S1", "S2"] and purge_records(rig) == []
    fresh = rig.do(PURGE, {"dry_run": False, "token": purge(rig)["result"]["token"]})
    assert fresh["ok"] is True and len(fresh["result"]["purged"]) == 2


def test_a_token_of_another_age_cutoff_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    take_checkpoint(rig, S2)
    token = purge(rig)["result"]["token"]
    env = rig.do(PURGE, {"older_than_days": 0, "dry_run": False, "token": token})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert stash_names(rig) == ["S1", "S2"]


def test_the_token_must_be_a_string(rig: Rig) -> None:
    env = rig.do(PURGE, {"dry_run": False, "token": 5})
    assert env["errorCode"] == "INVALID_ARGS"


def test_with_nothing_to_purge_the_dry_runs_token_still_lets_a_real_run_through(rig: Rig) -> None:
    token = purge(rig)["result"]["token"]
    env = rig.do(PURGE, {"dry_run": False, "token": token})
    assert env["ok"] is True and env["result"]["purged"] == [], env


def test_only_old_recorded_copies_are_deleted_and_the_rest_stays(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = old_checkpoint(rig, monkeypatch, S1)
    recent = str(take_checkpoint(rig, S2)["checkpoint"])
    stray = plant(rig)
    env = purge(rig, dry_run=False)
    assert env["ok"] is True, env
    result = env["result"]
    assert [p["id"] for p in result["purged"]] == [old] and result["skipped"] == []
    assert result["dryRun"] is False and env["affected"] == 1
    assert stash_names(rig) == ["S2", "S3"]  # the recent one and the unknown one
    assert uid(stray) in [uid(e) for e in stash_entries(rig)]
    assert result["reelsDeleted"] == []  # the date reel is not empty
    assert [u["name"] for u in result["unknown"]] == ["S3"]
    record = purge_records(rig)[0]
    assert record["purged"] == [old] and record["gone"] == [] and record["olderThanDays"] == 30
    listed = {i["id"]: i for i in rig.do("journal.list_checkpoints", {})["result"]["items"]}
    assert listed[old]["purged"] is True and listed[old]["purgedAt"] == record["ts"]
    assert listed[recent]["purged"] is False and listed[recent]["purgedAt"] is None
    assert result["purgeRecord"] == record["id"]


def test_a_date_reel_that_this_left_empty_is_deleted_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    old_checkpoint(rig, monkeypatch, S2)
    lib = stash_library(rig)
    assert lib is not None and len(lib.reels) == 1
    result = purge(rig, dry_run=False)["result"]
    assert [p["name"] for p in result["purged"]] == ["S1", "S2"]
    assert len(result["reelsDeleted"]) == 1 and result["reelsDeleted"][0].startswith("timeline_")
    assert lib.reels == [] and stash_library(rig) is lib


def test_a_reel_that_still_holds_an_unknown_copy_stays(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    stray = plant(rig)
    result = purge(rig, dry_run=False)["result"]
    assert result["reelsDeleted"] == []
    assert stash_names(rig) == ["S3"] and uid(stash_entries(rig)[0]) == uid(stray)


def test_a_copy_that_was_renamed_is_not_deleted(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    old = old_checkpoint(rig, monkeypatch, S1)
    stash_entries(rig)[0].name.set_value("something else")
    dry = purge(rig)["result"]["candidates"][0]
    assert dry["exists"] is True and dry["nameMatches"] is False
    assert purge(rig)["result"]["count"] == 0
    result = purge(rig, dry_run=False)["result"]
    assert result["purged"] == [] and result["skipped"][0]["id"] == old
    assert "something else" in result["skipped"][0]["reason"]
    assert stash_names(rig) == ["something else"] and purge_records(rig) == []


def test_a_copy_that_left_the_library_is_never_touched_and_is_recorded_as_gone(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = old_checkpoint(rig, monkeypatch, S1)
    rig.flame.media_panel.move(stash_entries(rig)[0], rig.handles["reel2"])
    spy = DeleteSpy(rig, monkeypatch)
    dry = purge(rig)["result"]
    assert dry["missing"] == [old] and dry["candidates"][0]["exists"] is False
    result = purge(rig, dry_run=False)["result"]
    assert spy.names == [] and result["purged"] == [] and result["gone"] == [old]
    assert "S1" in reel_names(rig.handles["reel2"])
    assert purge_records(rig)[0]["gone"] == [old]
    listed = {i["id"]: i for i in rig.do("journal.list_checkpoints", {})["result"]["items"]}
    assert listed[old]["purged"] is True


def test_a_copy_that_has_another_uid_than_the_record_is_not_the_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = old_checkpoint(rig, monkeypatch, S1)
    lib = stash_library(rig)
    assert lib is not None
    original = stash_entries(rig)[0]
    stand_in = rig.flame.media_panel.copy(original, lib.reels[0])[0]
    rig.flame.delete(original, confirm=False)  # the recorded copy is gone, a look-alike remains
    result = purge(rig, dry_run=False)["result"]
    assert result["purged"] == [] and result["gone"] == [old]
    assert [uid(e) for e in stash_entries(rig)] == [uid(stand_in)]
    assert [u["uid"] for u in result["unknown"]] == [uid(stand_in)]


def test_a_second_purge_has_nothing_left_to_do(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    assert purge(rig, dry_run=False)["result"]["count"] == 1
    again = purge(rig, dry_run=False)
    assert again["ok"] is True and again["result"]["candidates"] == []
    assert len(purge_records(rig)) == 1


def test_a_big_purge_goes_to_flame_in_batches(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    for _ in range(51):
        old_checkpoint(rig, monkeypatch, S2)
    token = purge(rig, older_than_days=0)["result"]["token"]
    before = [c.label for c in rig.exec.calls].count("timeline.purge")
    env = purge(rig, older_than_days=0, dry_run=False, token=token)
    assert env["ok"] is True and len(env["result"]["purged"]) == 51
    assert [c.label for c in rig.exec.calls].count("timeline.purge") - before == 3  # scan + 2
    assert stash_entries(rig) == []


def test_the_stashes_of_a_delete_and_of_a_restore_are_purgeable_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = delete(rig, [S2])
    edit = trim(rig, a_seg(), "head", 2)
    revert(rig, edit)  # a restore: its pre-restore copy is recorded as a checkpoint as well
    cps = checkpoints(rig)
    assert len(cps) == 3
    result = purge(rig, older_than_days=0)["result"]
    assert result["count"] == 3 and result["unknown"] == []
    done = purge(rig, older_than_days=0, dry_run=False)["result"]
    assert len(done["purged"]) == 3 and stash_entries(rig) == []
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is False and back["errorCode"] == "CONFLICT"
    assert "purged" in back["error"] or "gone" in back["error"]


def test_a_purged_checkpoint_cannot_be_restored(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    old = old_checkpoint(rig, monkeypatch, S1)
    purge(rig, dry_run=False)
    moves = len(rig.flame.media_panel.move_calls)
    out = rig.do("journal.restore_checkpoint", {"checkpoint": old})
    assert out["ok"] is False and out["errorCode"] == "PRECONDITION_UNMET"
    assert "was purged" in out["error"] and "journal.purge_checkpoints" in out["error"]
    assert len(rig.flame.media_panel.move_calls) == moves


def test_a_group_whose_checkpoint_was_purged_cannot_be_reverted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(timeline_checkpoints, "now_iso", lambda: OLD)
        env = trim(rig, a_seg(), "head", 2)
    assert env["ok"] is True
    purge(rig, dry_run=False)
    out = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert out["ok"] is False and "was purged" in out["error"]
    assert out["errorCode"] == "PRECONDITION_UNMET"


def test_a_checkpoint_that_was_not_purged_still_restores(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S2)
    kept = take_checkpoint(rig, S1)["checkpoint"]
    purge(rig, dry_run=False)
    assert rig.do("journal.restore_checkpoint", {"checkpoint": kept})["ok"] is True


def test_a_flame_failure_deletes_nothing_and_records_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    token = purge(rig)["result"]["token"]
    rig.exec.fault("timeline.purge", "TIMEOUT", when="before", skip=1)  # the scan passes
    env = purge(rig, dry_run=False, token=token)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    assert env["result"]["purged"] == [] and stash_names(rig) == ["S1"]
    assert purge_records(rig) == []


def test_a_journal_that_cannot_take_the_record_says_so(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    real = rig.journal.append

    def refusing(record: dict[str, Any]) -> None:
        if record.get("type") == "purge":
            raise OSError("disk full")
        real(record)

    monkeypatch.setattr(rig.journal, "append", refusing)
    env = purge(rig, dry_run=False)
    assert env["ok"] is True and "disk full" in env["result"]["journalError"]
    assert env["result"]["purgeRecord"] is None and stash_entries(rig) == []


def test_it_is_a_destructive_op_that_readonly_and_plans_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from flame_mcp.policy import Policy

    rig = make_p32b_rig(monkeypatch, tmp_path)
    plan = rig.do("plan.run", {"steps": [{"op": PURGE, "args": {}}]})
    assert plan["errorCode"] == "INVALID_ARGS" and "cannot be a plan step" in str(plan["result"])
    rig.runtime.policy = Policy.from_env({"FLAME_MCP_READONLY": "1"})
    assert purge(rig)["errorCode"] == "POLICY_DENIED"


def test_the_op_is_declared_destructive_and_not_undoable() -> None:
    from flame_mcp.ops import register_all
    from flame_mcp.registry import Registry

    registry = Registry()
    register_all(registry)
    op = registry.get(PURGE)
    assert op is not None and op.effect == "destructive" and op.undo == "unsupported"
    assert op.thread == "server" and {p.name: p.default for p in op.params} == {
        "older_than_days": 30,
        "dry_run": True,
        "token": NO_DEFAULT,
    }


def write_day(directory: Path, day: str, records: list[dict[str, Any]], age_days: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"journal_{day}.jsonl"
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    stamp = 1_800_000_000 - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def timeline_cp(checkpoint_id: str) -> dict[str, Any]:
    return {"type": "checkpoint", "id": checkpoint_id, "kind": "timeline", "ts": OLD}


def test_the_cleanup_keeps_the_file_of_a_checkpoint_that_was_not_purged(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    journal = Journal(tmp_path / "j")
    plain = write_day(journal.directory, "20260101", [{"type": "intent", "id": "i1"}], 40)
    holder = write_day(journal.directory, "20260102", [timeline_cp("c1")], 40)
    batch = write_day(journal.directory, "20260103", [{"type": "checkpoint", "id": "b1"}], 40)
    fresh = write_day(journal.directory, "20260104", [timeline_cp("c2")], 2)
    assert journal.cleanup_old(now=now) == 2
    assert not plain.exists() and not batch.exists()
    assert holder.exists() and fresh.exists()
    assert JOURNAL_MAX_AGE_S == 30 * 86400


def test_the_cleanup_lets_go_of_a_file_once_every_checkpoint_in_it_is_purged(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000.0
    journal = Journal(tmp_path / "j")
    holder = write_day(journal.directory, "20260102", [timeline_cp("c1"), timeline_cp("c2")], 40)
    purge_day = write_day(
        journal.directory, "20260201", [{"type": "purge", "id": "p1", "purged": ["c1"]}], 5
    )
    assert journal.cleanup_old(now=now) == 0 and holder.exists()
    with purge_day.open("a") as fh:
        fh.write(json.dumps({"type": "purge", "id": "p2", "gone": ["c2"]}) + "\n")
    os.utime(purge_day, (now - 5 * 86400, now - 5 * 86400))
    assert journal.cleanup_old(now=now) == 1
    assert not holder.exists() and purge_day.exists()


def test_the_cleanup_copes_with_a_torn_line(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    journal = Journal(tmp_path / "j")
    path = write_day(journal.directory, "20260102", [timeline_cp("c1")], 40)
    with path.open("a") as fh:
        fh.write('{"type":"checkpoint","id":"c9","kind":"timel')
    assert journal.cleanup_old(now=now) == 0 and path.exists()


def test_the_index_knows_which_checkpoints_were_purged() -> None:
    from flame_mcp.journal import Index

    index = Index(
        [
            timeline_cp("c1"),
            timeline_cp("c2"),
            {"type": "purge", "id": "p", "ts": "t", "purged": ["c1"], "gone": ["c2"]},
        ]
    )
    assert sorted(index.purged) == ["c1", "c2"] and index.purged["c1"]["id"] == "p"
