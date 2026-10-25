"""`desktop.move`, `desktop.delete` and `desktop.create` when reading back what they wrote fails.

A write that ran keeps what is known of it: the part that was read before the failure goes into
`result` and into the journal's `created`, so `journal.revert` can still put it back, and the
message says the write may have been written (rule 4 of tests/test_readback_rules.py). A move that
cannot be matched with the entry it made (`pairedBy: position`) is only matched when it is the one
unmatched source of its type. Run through the real Runtime with a scripted `transport.exec` over
the fake Flame tree; each test bends the FAKE with one misbehaviour, it does not describe a
measured Flame."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import transport
from flame_mcp.journal import Index
from journal_test_helpers import Rig, make_rig
from media_p31b_helpers import (
    C1,
    add_clip,
    add_seq,
    make_media_rig,
    mv,
    names,
    reel2_selector,
    revert,
    states,
    wt,
)
from p32b_helpers import S2, S3, delete, make_p32b_rig
from timeline_p32a_helpers import reel_names

DISPLAY_FIX = "def _tl_display_fix(shown_before, was_displayed, new_obj):"
RAISE_DISPLAY = DISPLAY_FIX + "\n    raise RuntimeError('display boom')"


def inject(
    monkeypatch: pytest.MonkeyPatch, rig: Rig, only_in: str, needle: str, replacement: str
) -> Callable[[], None]:
    """Rewrite one line of the Flame-side prelude in the calls that carry `only_in`. Returns the
    function that switches the rewriting off."""
    inner: Callable[..., Any] = rig.exec
    active = {"on": True}

    def run(code: str, *args: Any, **kwargs: Any) -> Any:
        if active["on"] and only_in in code:
            assert needle in code, needle
            code = code.replace(needle, replacement, 1)
        return inner(code, *args, **kwargs)

    monkeypatch.setattr(transport, "exec", run)
    return lambda: active.update(on=False)


def outcome_of(rig: Rig, env: dict[str, Any]) -> dict[str, Any]:
    [view] = Index(rig.records()).groups[env["journal"]["group"]]
    assert view.outcome is not None
    return dict(view.outcome)


def reel3_clip(rig: Rig, name: str) -> dict[str, Any]:
    if "reel3" not in rig.handles:
        rig.handles["reel3"] = rig.handles["reel_group"].create_reel("Reel 3")
    return wt(add_clip(rig.handles["reel3"], name))


@pytest.fixture
def media_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_media_rig(monkeypatch, tmp_path)


# -- desktop.move ---------------------------------------------------------------------------------


def test_a_move_whose_destination_cannot_be_read_back_keeps_the_result_and_says_unknown(
    media_rig: Rig,
) -> None:
    rig = media_rig
    reel2 = rig.handles["reel2"]
    base = type(reel2)
    state = {"moved": False}
    real_move = rig.flame.media_panel.move

    def move(*args: Any, **kwargs: Any) -> Any:
        answer = real_move(*args, **kwargs)
        state["moved"] = True
        return answer

    class Unreadable(base):  # type: ignore[valid-type, misc]
        @property
        def children(self) -> Any:
            if state["moved"]:
                raise RuntimeError("read-back boom")
            return base.children.fget(self)

        @children.setter
        def children(self, value: Any) -> None:
            base.children.fset(self, value)

    rig.flame.media_panel.move = move
    Unreadable.__name__ = base.__name__
    reel2.__class__ = Unreadable
    env = mv(rig, [C1], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "may have been written" in env["error"] and "read-back boom" in env["error"]
    assert "unknown" in env["error"]
    result = env["result"]
    assert result is not None and result["moved"] == []
    [row] = result["notMoved"]
    assert row["from"]["name"] == "c1" and row["uidGone"] is True
    assert env["journal"]["revert"] is None
    assert outcome_of(rig, env)["created"] is None


def test_a_move_whose_display_read_back_fails_keeps_what_moved_and_can_be_reverted(
    media_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = media_rig
    off = inject(monkeypatch, rig, "_result = _dm_move(", DISPLAY_FIX, RAISE_DISPLAY)
    env = mv(rig, [C1], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "may have been written" in env["error"] and "display boom" in env["error"]
    assert [m["from"]["name"] for m in env["result"]["moved"]] == ["c1"]
    assert env["affected"] == 1 and env["journal"]["revert"] is not None
    out = outcome_of(rig, env)
    assert out["ok"] is False and out["verdict"] == "partial"
    assert [m["from"]["name"] for m in out["created"]["moves"]] == ["c1"]
    assert states(rig)[env["journal"]["group"]] == "partial"
    off()
    assert revert(rig, env)["ok"] is True
    assert names(rig.handles["reel_group"].reels[0]) == ["c1"]


def test_a_move_with_a_display_failure_and_nothing_moved_stays_silent_noop(
    media_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = media_rig
    rig.flame.media_panel.move = lambda source, destination: []
    inject(monkeypatch, rig, "_result = _dm_move(", DISPLAY_FIX, RAISE_DISPLAY)
    env = mv(rig, [C1], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["journal"]["revert"] is None


# -- desktop.move: pairing by position ------------------------------------------------------------


def renaming_and_silent(rig: Rig) -> None:
    real = rig.flame.media_panel.move

    def move(source: Any, destination: Any) -> list[Any]:
        for made in real(source, destination):
            made.name.set_value(str(made.name.get_value()) + " 1")
        return []

    rig.flame.media_panel.move = move


def test_two_unmatched_sources_of_one_type_are_not_paired_by_position(media_rig: Rig) -> None:
    rig = media_rig
    second = reel3_clip(rig, "d1")
    renaming_and_silent(rig)
    env = mv(rig, [C1, second], reel2_selector(rig))
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["error"].startswith("partial:")
    result = env["result"]
    assert result["moved"] == []
    assert [(r["from"]["name"], r["uidGone"]) for r in result["notMoved"]] == [
        ("c1", True),
        ("d1", True),
    ]
    assert "journal.revert cannot put them back" in env["error"]
    assert env["journal"]["revert"] is None
    assert sorted(names(rig.handles["reel2"])) == ["c1 1", "d1 1"]


def test_an_unmatched_clip_and_an_unmatched_sequence_are_each_paired_by_position(
    media_rig: Rig,
) -> None:
    rig = media_rig
    seq = add_seq(rig.handles["reel_group"].reels[0], "S1")
    renaming_and_silent(rig)
    env = mv(rig, [C1, wt(seq)], reel2_selector(rig))
    assert env["ok"] is True, env
    assert [m["pairedBy"] for m in env["result"]["moved"]] == ["position", "position"]
    assert env["result"]["notMoved"] == [] and env["journal"]["revert"] is not None


def test_a_matched_source_does_not_count_as_unmatched_for_the_pairing(media_rig: Rig) -> None:
    rig = media_rig
    second = reel3_clip(rig, "d1")
    real = rig.flame.media_panel.move

    def renames_the_second_only(source: Any, destination: Any) -> list[Any]:
        made = real(source, destination)
        made[1].name.set_value("d1 1")
        return []

    rig.flame.media_panel.move = renames_the_second_only
    env = mv(rig, [C1, second], reel2_selector(rig))
    assert env["ok"] is True, env
    by = {m["from"]["name"]: m.get("pairedBy") for m in env["result"]["moved"]}
    assert by == {"c1": None, "d1": "position"}


# -- desktop.move: the revert ---------------------------------------------------------------------


def moved_two(rig: Rig) -> dict[str, Any]:
    second = reel3_clip(rig, "d1")
    env = mv(rig, [C1, second], reel2_selector(rig))
    assert env["ok"] is True, env
    return env


def test_a_revert_that_puts_back_only_some_is_partial(media_rig: Rig) -> None:
    rig = media_rig
    env = moved_two(rig)
    real = rig.flame.media_panel.move
    calls = {"n": 0}

    def second_goes_nowhere(source: Any, destination: Any) -> Any:
        calls["n"] += 1
        return real(source, destination) if calls["n"] == 1 else []

    rig.flame.media_panel.move = second_goes_nowhere
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "FLAME_API_ERROR", out
    assert "partial:" in out["error"]
    assert len(out["result"]["reverted"]) == 1 and len(out["result"]["errors"]) == 1


def test_a_revert_that_puts_back_nothing_stays_silent_noop(media_rig: Rig) -> None:
    rig = media_rig
    env = moved_two(rig)
    rig.flame.media_panel.move = lambda source, destination: []
    out = revert(rig, env)
    assert out["ok"] is False and out["errorCode"] == "SILENT_NOOP", out
    assert out["result"]["reverted"] == []


# -- desktop.delete -------------------------------------------------------------------------------


@pytest.fixture
def delete_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_p32b_rig(monkeypatch, tmp_path)


def test_a_delete_whose_display_read_back_fails_keeps_what_was_deleted_and_can_be_reverted(
    delete_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = delete_rig
    off = inject(monkeypatch, rig, "_result = _dd_delete(", DISPLAY_FIX, RAISE_DISPLAY)
    env = delete(rig, [S2, S3])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "may have been written" in env["error"] and "display boom" in env["error"]
    assert [d["from"]["name"] for d in env["result"]["deleted"]] == ["S2", "S3"]
    assert env["affected"] == 2 and env["journal"]["revert"] is not None
    out = outcome_of(rig, env)
    assert out["ok"] is False and out["verdict"] == "partial"
    assert len(out["created"]["deletes"]) == 2
    assert states(rig)[env["journal"]["group"]] == "partial"
    off()
    back = revert(rig, env)
    assert back["ok"] is True, back
    assert sorted(reel_names(rig.handles["reel"])) == ["CLIPX", "S1", "S2", "S3", "c1"]


def test_a_delete_whose_pre_delete_read_fails_for_one_target_is_partial_and_keeps_the_rest(
    delete_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = delete_rig
    head = "def _dd_delete_one(obj, uid, info, shown_before):"
    inject(
        monkeypatch,
        rig,
        "_result = _dd_delete(",
        head,
        head
        + "\n    if _try(lambda: _v(obj.name)) == 'S3':\n        raise RuntimeError('read boom')",
    )
    env = delete(rig, [S2, S3])
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["error"].startswith("partial:")
    assert [d["from"]["name"] for d in env["result"]["deleted"]] == ["S2"]
    [left] = env["result"]["notDeleted"]
    assert left["from"]["name"] == "S3" and "read boom" in left["readError"]
    assert left["stillThere"] is True
    assert env["journal"]["revert"] is not None
    assert states(rig)[env["journal"]["group"]] == "partial"


def test_a_delete_with_a_display_failure_and_nothing_deleted_stays_silent_noop(
    delete_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = delete_rig
    rig.flame.delete = lambda obj, confirm=True: False
    inject(monkeypatch, rig, "_result = _dd_delete(", DISPLAY_FIX, RAISE_DISPLAY)
    env = delete(rig, [S2])
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["journal"]["revert"] is None


# -- desktop.create -------------------------------------------------------------------------------


def test_a_create_whose_read_back_fails_before_it_names_the_made_thing_still_records_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    head = "def _dc_describe_created(out, kind, parent, made, uids, opts):"
    off = inject(
        monkeypatch,
        rig,
        "_dc_create(",
        head,
        head + "\n    raise RuntimeError('describe boom')",
    )
    env = rig.do("desktop.create", {"kind": "reel_group", "name": "RG2"})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "reading it back failed" in env["error"] and "describe boom" in env["error"]
    assert env["journal"]["revert"] is not None
    made = outcome_of(rig, env)["created"]
    assert made["name"] == "RG2" and made["type"] == "PyReelGroup" and made["uid"]
    off()
    back = rig.do("journal.revert", {"group": env["journal"]["group"]})
    assert back["ok"] is True, back
    groups = rig.flame.projects.current_project.current_workspace.desktop.reel_groups
    assert [str(g.name.get_value()) for g in groups] == ["RG"]


def test_a_create_whose_read_back_fails_and_whose_answer_is_not_a_new_thing_records_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    desktop = rig.flame.projects.current_project.current_workspace.desktop
    real = desktop.create_reel_group
    existing = desktop.reel_groups[0]
    desktop.create_reel_group = lambda name: (real(name), existing)[1]
    head = "def _dc_describe_created(out, kind, parent, made, uids, opts):"
    inject(
        monkeypatch, rig, "_dc_create(", head, head + "\n    raise RuntimeError('describe boom')"
    )
    env = rig.do("desktop.create", {"kind": "reel_group", "name": "RG2"})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "may have been written" in env["error"]
    assert env["journal"]["revert"] is None
