"""The guard is checked after `write_lock` is taken, so a write queued behind one that ended
without an answer (TIMEOUT) is refused instead of running after the guard shut."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from journal_test_helpers import COMP, Rig, make_rig

QUEUE_WAIT_S = 0.8
JOIN_S = 10


def test_revert_queued_behind_a_write_that_goes_unknown_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig: Rig = make_rig(monkeypatch, tmp_path)
    made = rig.do("desktop.create", {"kind": "batch_group", "name": "RACE1"})
    assert made["ok"] is True, made
    group = made["journal"]["group"]

    def names() -> list[str]:
        desktop = rig.flame.projects.current_project.current_workspace.desktop
        return [str(g.name.get_value()) for g in desktop.batch_groups]

    assert "RACE1" in names()

    rig.exec.fault("node.set_attributes", "TIMEOUT", when="after")
    out: dict[str, object] = {}
    started: list[int] = []

    def revert_in_thread() -> None:
        out["b"] = rig.do("journal.revert", {"group": group})

    def hook(label: str | None) -> None:
        if label == "node.set_attributes" and not started:
            started.append(1)
            thread = threading.Thread(target=revert_in_thread)
            thread.start()
            out["t"] = thread
            time.sleep(QUEUE_WAIT_S)

    rig.exec.before_run = hook
    a = rig.do("node.set_attributes", {"target": COMP, "values": {"blend_type": "Gimp"}})
    thread = out["t"]
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=JOIN_S)

    assert a["errorCode"] == "TIMEOUT"
    assert rig.runtime.guard.blocked()
    b = out["b"]
    assert isinstance(b, dict)
    assert b["ok"] is False, "the revert wrote after the guard shut"
    assert b["errorCode"] == "POLICY_DENIED"
    assert "RACE1" in names()
