"""A stash / checkpoint copy or a purge batch that ends without an answer (TIMEOUT / BRIDGE_LOST)
leaves no journal record, so the guard is shut with a block that has no intent: every later write
is refused, `journal.check` reads Flame again (it lists the stash copies no checkpoint accounts
for) and then lifts it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from journal_test_helpers import BG1, Rig
from p32b_helpers import DELETE, PURGE, S2, delete, make_p32b_rig
from reformat_helpers import OP as REFORMAT_OP
from reformat_helpers import make_reformat_rig, reformat
from test_purge_p32b import old_checkpoint
from timeline_p32a_helpers import S1, a_seg, stash_entries, trim


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


LIFT_HINT = "journal.check (without a group)"


def assert_shut_then_check_lifts_it(rig: Rig, op_name: str) -> dict[str, Any]:
    blocked = rig.runtime.guard.blocked()
    assert [b.op for b in blocked] == [op_name], blocked
    refused = rig.do("batch.checkpoint", {"target": BG1})
    assert refused["errorCode"] == "POLICY_DENIED", refused
    assert "journal.check" in refused["hint"]
    checked = rig.do("journal.check", {})
    assert checked["ok"] is True, checked
    assert rig.runtime.guard.blocked() == []
    assert rig.do("batch.checkpoint", {"target": BG1})["ok"] is True
    return checked


def test_timeline_checkpoint_that_timed_out_shuts_the_guard_and_check_lists_the_copy(
    rig: Rig,
) -> None:
    rig.exec.fault("timeline.checkpoint", "TIMEOUT", when="after")
    env = trim(rig, a_seg(), "head", 2)
    assert env["errorCode"] == "TIMEOUT", env
    assert LIFT_HINT in env["hint"], env["hint"]
    assert len(stash_entries(rig)) == 1 and not [
        r for r in rig.records() if r["type"] == "checkpoint"
    ]

    checked = assert_shut_then_check_lifts_it(rig, "timeline.checkpoint")

    unrecorded = checked["result"]["unrecorded"]
    assert len(unrecorded) == 1 and unrecorded[0]["released"] is True
    assert len(unrecorded[0]["unknownInStash"]) == 1


def test_batch_checkpoint_that_timed_out_shuts_the_guard(rig: Rig) -> None:
    rig.exec.fault("batch.checkpoint", "BRIDGE_LOST", when="after")
    env = rig.do("batch.checkpoint", {"target": BG1})
    assert env["errorCode"] == "BRIDGE_LOST", env
    assert LIFT_HINT in env["hint"], env["hint"]

    assert_shut_then_check_lifts_it(rig, "batch.checkpoint")


def test_desktop_delete_whose_stash_copy_timed_out_shuts_the_guard(rig: Rig) -> None:
    rig.exec.fault(DELETE, "TIMEOUT", when="after", skip=1)  # the read, then the stash copy
    env = delete(rig, [S2])
    assert env["errorCode"] == "TIMEOUT", env
    assert LIFT_HINT in env["hint"], env["hint"]
    assert len(stash_entries(rig)) == 1

    checked = assert_shut_then_check_lifts_it(rig, DELETE)

    assert len(checked["result"]["unrecorded"][0]["unknownInStash"]) == 1


def test_reformat_whose_stash_copy_timed_out_shuts_the_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_reformat_rig(monkeypatch, tmp_path)
    rig.exec.fault(REFORMAT_OP, "TIMEOUT", when="after", skip=1)  # snapshot, then the stash copy
    env = reformat(rig)
    assert env["errorCode"] == "TIMEOUT", env

    assert_shut_then_check_lifts_it(rig, REFORMAT_OP)


def test_purge_batch_that_timed_out_shuts_the_guard(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_checkpoint(rig, monkeypatch, S1)
    token = rig.do(PURGE, {})["result"]["token"]
    rig.exec.fault("timeline.purge", "TIMEOUT", when="after", skip=1)  # the scan, then the batch
    env = rig.do(PURGE, {"dry_run": False, "token": token})
    assert env["errorCode"] == "TIMEOUT", env

    assert_shut_then_check_lifts_it(rig, PURGE)


def test_a_batch_that_answered_with_a_flame_error_does_not_shut_the_guard(rig: Rig) -> None:
    old_checkpoint_missing = rig.do(
        "batch.checkpoint", {"target": {"kind": "batch", "name": "nope"}}
    )
    assert old_checkpoint_missing["ok"] is False
    assert rig.runtime.guard.blocked() == []


def test_check_keeps_the_block_while_flame_still_cannot_be_read(rig: Rig) -> None:
    rig.exec.fault("timeline.checkpoint", "TIMEOUT", when="after")
    trim(rig, a_seg(), "head", 2)
    rig.exec.fault("timeline.purge", "TIMEOUT", when="before")

    checked = rig.do("journal.check", {})

    assert checked["result"]["unrecorded"][0]["released"] is False
    assert len(rig.runtime.guard.blocked()) == 1
    assert checked["result"]["writesBlocked"] is True
