"""A `journal.check` read that the bridge answers with `pending` (ok, no result: the main thread
is still busy with the write being checked) is not a finding. The guard stays shut and the verdict
is unknown, for every check that reads Flame through `desktop_revert.observe`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flame_mcp import transport
from journal_test_helpers import Rig, bridge_result
from reformat_helpers import OP, make_reformat_rig, reformat

PENDING_WRITE = "f" * 32
PENDING_READ = "e" * 32


def test_check_while_reformat_still_runs_keeps_the_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig: Rig = make_reformat_rig(monkeypatch, tmp_path)
    real = rig.exec
    busy: list[int] = []

    def exec_(
        code: str,
        thread: str,
        timeout_s: float | None = None,
        label: str | None = None,
        *,
        run_dir: Any = None,
        handle: Any = None,
    ) -> Any:
        if label == OP and busy:
            return bridge_result(result=None, pending_handle=PENDING_READ)
        if label == OP and "_result = _rf_write(" in code:
            busy.append(1)
            return bridge_result(result=None, pending_handle=PENDING_WRITE)
        return real(code, thread, timeout_s, label, run_dir=run_dir, handle=handle)

    monkeypatch.setattr(transport, "exec", exec_)
    env = reformat(rig)
    assert env["errorCode"] == "TIMEOUT", env
    assert len(rig.runtime.guard.blocked()) == 1

    checked = rig.do("journal.check", {})
    row = checked["result"]["checked"][0]

    assert row["verdict"] == "unknown", row
    assert len(rig.runtime.guard.blocked()) == 1


def test_check_whose_second_read_cannot_be_answered_stays_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The clip is gone from its recorded uid (a reformat gives it a new one), so the check looks
    for it by a probe of its reel. A probe that gets no answer is not "the clip is gone"."""
    rig: Rig = make_reformat_rig(monkeypatch, tmp_path)
    real = rig.exec
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=2)  # snapshot, stash, then the write
    env = reformat(rig)
    assert env["errorCode"] == "TIMEOUT", env
    assert len(rig.runtime.guard.blocked()) == 1

    def exec_(
        code: str,
        thread: str,
        timeout_s: float | None = None,
        label: str | None = None,
        *,
        run_dir: Any = None,
        handle: Any = None,
    ) -> Any:
        if label == OP and "_result = _dm_probe(" in code:
            return bridge_result(result=None, pending_handle=PENDING_READ)
        return real(code, thread, timeout_s, label, run_dir=run_dir, handle=handle)

    monkeypatch.setattr(transport, "exec", exec_)
    row = rig.do("journal.check", {})["result"]["checked"][0]

    assert row["verdict"] == "unknown", row
    assert len(rig.runtime.guard.blocked()) == 1
