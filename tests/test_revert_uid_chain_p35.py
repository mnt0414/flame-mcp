"""The revert of a write that MADE a clip follows the Timeline-restore uid chain.

A clip made by `segment.copy_to_media_panel` / `segment.match` / `desktop.copy` / `mediahub.import`
/ `desktop.duplicate`, then edited behind a Timeline checkpoint (`segment.trim` inside the clip),
then that edit reverted, is a NEW object (a restore always gives a new uid and the outcome records
`replacedUid` -> `targetUid`). The revert of the make must delete THAT clip: it looks the made thing
up by the newest uid of its chain that resolves (`result.foundUid` / `uidChain`), applies the
content check (name, duration, fingerprint recorded at creation) to it, and says `alreadyGone`
only when no uid of the chain resolves. A clip edited and NOT reverted is a CONFLICT.
Run through the real Runtime over the fake Flame tree, like the other op tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeFlame
from journal_test_helpers import Rig
from p33_helpers import COPY_OUT, MATCH, REEL_WT, a_seg, import_with_media
from timeline_p32a_helpers import CLIPX_WT, make_timeline_rig, seg_sel, uid, wt

DEST = wt(REEL_WT)
CLIPX = wt(CLIPX_WT)
KINDS = ("copy_to_media_panel", "match", "desktop.copy", "mediahub.import", "desktop.duplicate")


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    monkeypatch.setattr(FakeFlame, "import_clips", import_with_media(FakeFlame))
    return make_timeline_rig(monkeypatch, tmp_path)


def made_by(rig: Rig, tmp_path: Path, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the write's envelope, the made clip's {uid, selector})."""
    if kind in ("copy_to_media_panel", "match"):
        op = COPY_OUT if kind == "copy_to_media_panel" else MATCH
        env = rig.do(op, {"target": a_seg(), "destination": DEST})
        made = env["result"]["created"]
    elif kind == "desktop.copy":
        env = rig.do("desktop.copy", {"sources": [CLIPX], "destination": DEST})
        made = env["result"]["copies"][0]
    elif kind == "mediahub.import":
        (tmp_path / "clip1.mov").write_bytes(b"x" * 32)
        env = rig.do(
            "mediahub.import", {"paths": [str(tmp_path / "clip1.mov")], "destination": DEST}
        )
        made = env["result"]["imported"][0]
    else:
        env = rig.do("desktop.duplicate", {"target": CLIPX})
        made = env["result"]["duplicate"]
    assert env["ok"] is True, env
    return env, made


def found_of(out: dict[str, Any], kind: str) -> dict[str, Any]:
    """The part of a revert's answer that names what was deleted (one thing)."""
    if kind in ("desktop.copy", "mediahub.import"):
        assert len(out["result"]["deleted"]) == 1, out
        row: dict[str, Any] = out["result"]["deleted"][0]
        return row
    return dict(out["result"])


def edit(rig: Rig, selector: dict[str, Any]) -> dict[str, Any]:
    env = rig.do(
        "segment.trim", {"target": seg_sel(selector, 0, index=0), "edge": "tail", "offset": 2}
    )
    assert env["ok"] is True, env
    return env


def reel_uids(rig: Rig) -> list[str]:
    return [uid(c) for c in rig.handles["reel"].children]


@pytest.mark.parametrize("kind", KINDS)
def test_made_then_edited_then_the_edit_reverted_the_revert_deletes_the_restored_clip(
    rig: Rig, tmp_path: Path, kind: str
) -> None:
    base = reel_uids(rig)
    write, made = made_by(rig, tmp_path, kind)
    original = made["uid"]
    trimmed = edit(rig, made["selector"])
    restored = rig.do("journal.revert", {"group": trimmed["journal"]["group"]})
    assert restored["ok"] is True, restored
    now = [u for u in reel_uids(rig) if u not in base]
    assert len(now) == 1 and original not in reel_uids(rig)
    out = rig.do("journal.revert", {"group": write["journal"]["group"]})
    assert out["ok"] is True, out
    row = found_of(out, kind)
    assert row["foundUid"] == now[0] != original
    assert row["uidChain"][0] == now[0] and original in row["uidChain"]
    assert row.get("alreadyGone") in (None, False)
    assert now[0] not in reel_uids(rig)


@pytest.mark.parametrize("kind", KINDS)
def test_made_then_edited_and_not_reverted_is_a_conflict_and_nothing_is_deleted(
    rig: Rig, tmp_path: Path, kind: str
) -> None:
    write, made = made_by(rig, tmp_path, kind)
    edit(rig, made["selector"])
    out = rig.do("journal.revert", {"group": write["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "CONFLICT", out
    assert made["uid"] in reel_uids(rig)


@pytest.mark.parametrize("kind", KINDS)
def test_a_clip_that_is_gone_whatever_its_uid_is_already_gone(
    rig: Rig, tmp_path: Path, kind: str
) -> None:
    write, made = made_by(rig, tmp_path, kind)
    obj = rig.flame.find_by_uid(made["uid"])
    assert rig.flame.delete(obj, confirm=False) is True
    out = rig.do("journal.revert", {"group": write["journal"]["group"]})
    assert out["ok"] is True, out
    if kind in ("desktop.copy", "mediahub.import"):
        assert [s["uid"] for s in out["result"]["skipped"]] == [made["uid"]]
    else:
        assert out["result"]["alreadyGone"] is True and out["result"]["foundUid"] is None


OP_OF: dict[str, str] = {
    "copy_to_media_panel": COPY_OUT,
    "match": MATCH,
    "desktop.copy": "desktop.copy",
    "mediahub.import": "mediahub.import",
    "desktop.duplicate": "desktop.duplicate",
}


@pytest.mark.parametrize("kind", KINDS)
def test_journal_check_of_a_revert_that_never_ran_finds_the_clip_by_its_chain(
    rig: Rig, tmp_path: Path, kind: str
) -> None:
    write, made = made_by(rig, tmp_path, kind)
    trimmed = edit(rig, made["selector"])
    assert rig.do("journal.revert", {"group": trimmed["journal"]["group"]})["ok"] is True
    rig.exec.fault(OP_OF[kind], "BRIDGE_LOST", when="before")
    out = rig.do("journal.revert", {"group": write["journal"]["group"]})
    assert out["ok"] is False
    checked = rig.do("journal.check")["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["not_landed"], checked


@pytest.mark.parametrize("kind", KINDS)
def test_journal_check_of_a_revert_whose_answer_was_lost_sees_the_restored_clip_gone(
    rig: Rig, tmp_path: Path, kind: str
) -> None:
    write, made = made_by(rig, tmp_path, kind)
    trimmed = edit(rig, made["selector"])
    assert rig.do("journal.revert", {"group": trimmed["journal"]["group"]})["ok"] is True
    rig.exec.fault(OP_OF[kind], "TIMEOUT", when="after")
    out = rig.do("journal.revert", {"group": write["journal"]["group"]})
    assert out["ok"] is False and out["errorCode"] == "TIMEOUT"
    checked = rig.do("journal.check")["result"]["checked"]
    assert [c["verdict"] for c in checked] == ["landed"], checked
