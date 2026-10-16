"""Tests for the journal ops that help after a CONFLICT or a lost answer.

`journal.revert` names the newer groups to put back first when it ends in CONFLICT, `journal.poll`
asks the bridge for a pending handle, `journal.inflight` reads the `run/inflight/` records. Run
through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import transport
from flame_mcp.journal import Index
from journal_test_helpers import BG1, Rig, bridge_result, make_rig
from timeline_p32a_helpers import a_seg, make_timeline_rig, trim


@pytest.fixture
def tl_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def edit(rig: Rig, edge: str, offset: int) -> dict[str, Any]:
    env = trim(rig, a_seg(), edge, offset)
    assert env["ok"] is True, env
    return env


def newer_of(env: dict[str, Any]) -> list[dict[str, Any]]:
    result = env["result"] or {}
    found: list[dict[str, Any]] = result.get("newerGroups") or []
    return found


def test_timeline_conflict_names_the_later_group_and_says_to_revert_it_first(tl_rig: Rig) -> None:
    first = edit(tl_rig, "head", 2)
    second = edit(tl_rig, "tail", 2)
    refused = tl_rig.do("journal.revert", {"group": first["journal"]["group"]})
    assert refused["ok"] is False and refused["errorCode"] == "CONFLICT"
    [row] = newer_of(refused)
    assert row["group"] == second["journal"]["group"] and row["op"] == "segment.trim"
    assert isinstance(row["ts"], str)
    assert second["journal"]["group"] in refused["error"] and "newest first" in refused["error"]
    assert ". Later group(s)" in refused["error"] and ".." not in refused["error"]
    assert refused["result"]["refused"] is True  # the existing fields stay


def test_timeline_newer_groups_come_newest_first(tl_rig: Rig) -> None:
    first = edit(tl_rig, "head", 2)
    second = edit(tl_rig, "tail", 2)
    third = edit(tl_rig, "tail", 1)
    refused = tl_rig.do("journal.revert", {"group": first["journal"]["group"]})
    assert [r["group"] for r in newer_of(refused)] == [
        third["journal"]["group"],
        second["journal"]["group"],
    ]


def test_following_the_advice_lets_the_older_revert_through(tl_rig: Rig) -> None:
    first = edit(tl_rig, "head", 2)
    second = edit(tl_rig, "tail", 2)
    assert tl_rig.do("journal.revert", {"group": first["journal"]["group"]})["ok"] is False
    assert tl_rig.do("journal.revert", {"group": second["journal"]["group"]})["ok"] is True
    again = tl_rig.do("journal.revert", {"group": first["journal"]["group"]})
    assert again["ok"] is True, again


def test_a_newer_group_that_was_reverted_is_not_listed(tl_rig: Rig) -> None:
    first = edit(tl_rig, "head", 2)
    second = edit(tl_rig, "tail", 2)
    third = edit(tl_rig, "tail", 1)
    assert tl_rig.do("journal.revert", {"group": third["journal"]["group"]})["ok"] is True
    refused = tl_rig.do("journal.revert", {"group": first["journal"]["group"]})
    assert refused["errorCode"] == "CONFLICT"
    assert [r["group"] for r in newer_of(refused)] == [second["journal"]["group"]]


def test_a_conflict_with_no_later_group_has_no_newer_groups(tl_rig: Rig) -> None:
    first = edit(tl_rig, "head", 2)
    tl_rig.handles["s1"].versions[0].tracks[0].segments[3].trim_tail(2)  # outside the journal
    refused = tl_rig.do("journal.revert", {"group": first["journal"]["group"]})
    assert refused["errorCode"] == "CONFLICT"
    assert "newerGroups" not in refused["result"]
    assert "newest first" not in refused["error"]


def test_a_later_group_on_another_sequence_is_not_listed(tl_rig: Rig) -> None:
    first = edit(tl_rig, "head", 2)
    other = trim(tl_rig, a_seg(0, parent={"kind": "wiretap", "id": "/wt/RG/Reel 1/S2"}), "tail", 1)
    assert other["ok"] is True, other
    tl_rig.handles["s1"].versions[0].tracks[0].segments[3].trim_tail(2)
    refused = tl_rig.do("journal.revert", {"group": first["journal"]["group"]})
    assert refused["errorCode"] == "CONFLICT" and "newerGroups" not in refused["result"]


def test_batch_conflict_names_the_later_batch_group(rig: Rig) -> None:
    saved = rig.do("batch.checkpoint", {"target": BG1})["result"]
    rig.handles["bg1"].create_node("Mux")
    load = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert load["ok"] is True, load
    created = rig.do("node.create", {"target": BG1, "type": "Blur"})
    assert created["ok"] is True, created
    refused = rig.do("journal.revert", {"group": load["journal"]["group"]})
    assert refused["errorCode"] == "CONFLICT"
    [row] = newer_of(refused)
    assert row["group"] == created["journal"]["group"] and row["op"] == "node.create"
    assert "newest first" in refused["error"]
    assert rig.do("journal.revert", {"group": created["journal"]["group"]})["ok"] is True
    assert rig.do("journal.revert", {"group": load["journal"]["group"]})["ok"] is True


def test_batch_conflict_without_a_journaled_later_write_has_no_newer_groups(rig: Rig) -> None:
    saved = rig.do("batch.checkpoint", {"target": BG1})["result"]
    rig.handles["bg1"].create_node("Mux")
    load = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    rig.handles["bg1"].create_node("Blur")  # by hand, not through this server
    refused = rig.do("journal.revert", {"group": load["journal"]["group"]})
    assert refused["errorCode"] == "CONFLICT" and "newerGroups" not in refused["result"]


def test_batch_groups_of_another_group_are_not_listed(rig: Rig) -> None:
    saved = rig.do("batch.checkpoint", {"target": BG1})["result"]
    rig.handles["bg1"].create_node("Mux")
    load = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    other = rig.do("node.create", {"target": {"kind": "batch", "name": "BG2"}, "type": "Blur"})
    assert other["ok"] is True, other
    rig.handles["bg1"].create_node("Blur")
    refused = rig.do("journal.revert", {"group": load["journal"]["group"]})
    assert refused["errorCode"] == "CONFLICT" and "newerGroups" not in refused["result"]


def test_the_journal_still_holds_one_group_per_write_after_a_refusal(tl_rig: Rig) -> None:
    first = edit(tl_rig, "head", 2)
    edit(tl_rig, "tail", 2)
    before = len(Index(tl_rig.records()).groups)
    tl_rig.do("journal.revert", {"group": first["journal"]["group"]})
    assert len(Index(tl_rig.records()).groups) == before


HANDLE = "ab" * 16


def test_poll_of_a_finished_handle_gives_the_bridges_raw_result(rig: Rig) -> None:
    rig.bridge.results[HANDLE] = bridge_result(result={"value": [1, 2], "affected": 0}, run_ms=7)
    env = rig.do("journal.poll", {"handle": HANDLE})
    assert env["ok"] is True, env
    assert env["result"]["state"] == "done" and env["result"]["handle"] == HANDLE
    assert env["result"]["result"] == {"value": [1, 2], "affected": 0}
    assert env["result"]["ok"] is True and env["result"]["runMs"] == 7
    assert env["execution"]["effect"] == "read"


def test_poll_hands_the_result_over_once(rig: Rig) -> None:
    rig.bridge.results[HANDLE] = bridge_result(result={"value": 1})
    assert rig.do("journal.poll", {"handle": HANDLE})["ok"] is True
    again = rig.do("journal.poll", {"handle": HANDLE})
    assert again["ok"] is False and again["errorCode"] == "PRECONDITION_UNMET"


def test_poll_of_a_run_still_going_says_pending(rig: Rig) -> None:
    rig.bridge.running.add(HANDLE)
    env = rig.do("journal.poll", {"handle": HANDLE})
    assert env["ok"] is True and env["result"]["state"] == "pending"
    assert env["result"]["result"] is None and env["result"]["handle"] == HANDLE


def test_poll_of_an_unknown_handle_is_precondition_unmet(rig: Rig) -> None:
    env = rig.do("journal.poll", {"handle": HANDLE})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "restart" in env["error"] and HANDLE in env["error"]


def test_poll_reports_a_failed_run_with_its_error(rig: Rig) -> None:
    rig.bridge.results[HANDLE] = bridge_result(
        ok=False, result=None, error_code="FLAME_API_ERROR", error="boom", traceback="tb"
    )
    env = rig.do("journal.poll", {"handle": HANDLE})
    assert env["ok"] is True
    body = env["result"]
    assert body["state"] == "done" and body["ok"] is False
    assert body["errorCode"] == "FLAME_API_ERROR" and body["error"] == "boom"


def test_poll_when_the_bridge_cannot_be_reached_ends_in_that_error(rig: Rig) -> None:
    rig.bridge.failure = bridge_result(ok=False, result=None, error_code="BRIDGE_UNAVAILABLE")
    env = rig.do("journal.poll", {"handle": HANDLE})
    assert env["ok"] is False and env["errorCode"] == "BRIDGE_UNAVAILABLE"


def test_poll_refuses_a_handle_that_belongs_to_a_journal_intent(rig: Rig) -> None:
    rig.journal.append(
        {
            "type": "intent",
            "id": "i_render",
            "group": "g_render",
            "ts": "2026-10-12T00:00:00.000+00:00",
            "op": "render.batch",
            "kind": "render",
            "handleSource": "client",
            "handle": HANDLE,
        }
    )
    rig.bridge.results[HANDLE] = bridge_result(result={"value": 1})
    env = rig.do("journal.poll", {"handle": HANDLE})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "journal.check" in env["error"] and "g_render" in env["error"]
    assert rig.bridge.calls == []  # the result was not consumed
    assert HANDLE in rig.bridge.results


def test_poll_refuses_a_pending_handle_recorded_by_a_run(rig: Rig) -> None:
    rig.journal.append(
        {
            "type": "intent",
            "id": "i_render",
            "group": "g_render",
            "ts": "2026-10-12T00:00:00.000+00:00",
            "op": "render.batch",
            "kind": "render",
        }
    )
    rig.journal.append(
        {
            "type": "pending",
            "id": "i_render",
            "group": "g_render",
            "ts": "2026-10-12T00:00:01.000+00:00",
            "handle": HANDLE,
            "timeoutS": 10,
        }
    )
    env = rig.do("journal.poll", {"handle": HANDLE})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "journal.check" in env["error"] and rig.bridge.calls == []


@pytest.mark.parametrize(
    "bad",
    ["../x", "a/b", "ab cd" + "e" * 27, "ab" * 16 + "0", "AB" * 16, "ab" * 15, "../../etc/passwd"],
)
def test_poll_refuses_a_handle_that_is_not_32_lowercase_hex_and_never_asks_the_bridge(
    rig: Rig, bad: str
) -> None:
    env = rig.do("journal.poll", {"handle": bad})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "32" in env["error"] and rig.bridge.calls == []


def test_poll_asks_the_bridge_for_a_well_formed_handle(rig: Rig) -> None:
    rig.bridge.running.add(HANDLE)
    assert rig.do("journal.poll", {"handle": HANDLE})["ok"] is True
    assert rig.bridge.calls == [HANDLE]


def test_poll_of_the_handle_of_a_decided_group_says_so_and_still_refuses(rig: Rig) -> None:
    for record in (
        {
            "type": "intent",
            "id": "i_done",
            "group": "g_done",
            "ts": "2026-10-12T00:00:00.000+00:00",
            "op": "render.batch",
            "kind": "render",
            "handleSource": "client",
            "handle": HANDLE,
        },
        {"type": "outcome", "id": "i_done", "group": "g_done", "ts": "x", "ok": True},
    ):
        rig.journal.append(record)
    env = rig.do("journal.poll", {"handle": HANDLE})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "already decided" in env["error"] and "no result" in env["error"]
    assert "g_done" in env["error"] and rig.bridge.calls == []


def test_poll_needs_a_handle(rig: Rig) -> None:
    env = rig.do("journal.poll", {})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert rig.do("journal.poll", {"handle": ""})["errorCode"] == "INVALID_ARGS"


def test_a_read_timeout_points_at_journal_poll_and_a_write_at_journal_check() -> None:
    from flame_mcp import envelope

    br = bridge_result(
        ok=False,
        result=None,
        error_code="TIMEOUT",
        timeout_phase="running",
        pending_handle=HANDLE,
    )
    text, pending = envelope.timeout_error_and_pending(br)
    assert pending == {"handle": HANDLE} and text is not None
    assert "journal.poll" in text and "journal.check" in text
    assert "poll pending.handle" not in text


def write_inflight(run_dir: Path, request_id: str, sent_at: str, **extra: Any) -> Path:
    directory = run_dir / "inflight"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "id": request_id,
        "label": "eval.run",
        "thread": "main",
        "timeoutS": 10.0,
        "sentAt": sent_at,
        "socket": "/x/bridge.sock",
        "bridgePid": 123,
        "handle": None,
        "codeSha256": "00" * 32,
        "code": "_result = 1",
        **extra,
    }
    path = directory / f"{request_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_inflight_lists_the_records_newest_first_as_written_plus_elapsed(
    rig: Rig, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    now = time.time()
    older = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 100))
    newer = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 10))
    write_inflight(run_dir, "r_old", older, label="old")
    write_inflight(run_dir, "r_new", newer, label="new", handle=HANDLE)
    env = rig.do("journal.inflight", {})
    assert env["ok"] is True, env
    result = env["result"]
    assert result["total"] == 2 and result["shown"] == 2 and result["truncated"] is False
    assert [r["id"] for r in result["items"]] == ["r_new", "r_old"]
    newest = result["items"][0]
    assert newest["label"] == "new" and newest["handle"] == HANDLE
    assert newest["bridgePid"] == 123 and newest["codeSha256"] == "00" * 32
    assert "code" not in newest and newest["codeChars"] == len("_result = 1")
    assert 5 <= newest["elapsedS"] <= 60 and result["items"][1]["elapsedS"] >= 90
    assert env["execution"]["effect"] == "read"


def test_inflight_stays_within_the_response_budget_for_records_of_real_size(
    rig: Rig, tmp_path: Path
) -> None:
    """A real record's `code` is the whole generated script (hundreds of kilobytes)."""
    big = "x = 1\n" * 10_000
    for n in range(3):
        write_inflight(tmp_path / "run", f"r{n}", f"2026-10-12T00:00:0{n}+00:00", code=big)
    env = rig.do("journal.inflight", {})
    assert len(json.dumps(env)) < 20_000 and env["degraded"] == []
    assert [r["codeChars"] for r in env["result"]["items"]] == [len(big)] * 3


def test_inflight_with_no_directory_is_empty(rig: Rig) -> None:
    env = rig.do("journal.inflight", {})
    assert env["ok"] is True
    assert env["result"]["total"] == 0 and env["result"]["items"] == []


def test_inflight_limit_and_offset_page_the_list(rig: Rig, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    for n in range(5):
        write_inflight(run_dir, f"r{n}", f"2026-10-12T00:00:0{n}+00:00")
    page = rig.do("journal.inflight", {"limit": 2, "offset": 1})["result"]
    assert [r["id"] for r in page["items"]] == ["r3", "r2"]
    assert page["total"] == 5 and page["shown"] == 2 and page["truncated"] is True


def test_inflight_counts_a_file_it_cannot_read_but_does_not_list_it(
    rig: Rig, tmp_path: Path
) -> None:
    run_dir = tmp_path / "run"
    write_inflight(run_dir, "good", "2026-10-12T00:00:00+00:00")
    (run_dir / "inflight" / "bad.json").write_text("{not json", encoding="utf-8")
    result = rig.do("journal.inflight", {})["result"]
    assert [r["id"] for r in result["items"]] == ["good"]
    assert result["total"] == 2 and result["unreadable"] == 1


def test_inflight_gives_only_the_known_keys_of_a_record(rig: Rig, tmp_path: Path) -> None:
    write_inflight(tmp_path / "run", "r1", "2026-10-12T00:00:00+00:00", secret="s3", extra=[1])
    [item] = rig.do("journal.inflight", {})["result"]["items"]
    assert set(item) == {
        "id",
        "label",
        "thread",
        "timeoutS",
        "sentAt",
        "socket",
        "bridgePid",
        "handle",
        "codeSha256",
        "codeChars",
        "elapsedS",
    }


def test_inflight_does_not_touch_the_records(rig: Rig, tmp_path: Path) -> None:
    path = write_inflight(tmp_path / "run", "keep", "2026-10-12T00:00:00+00:00")
    rig.do("journal.inflight", {})
    assert os.path.isfile(path)


def test_the_written_inflight_record_is_what_the_op_reads(tmp_path: Path) -> None:
    """The writer (`transport._write_inflight`) and the reader agree on the keys."""

    candidate = transport.BridgeCandidate(
        path=Path("/x/bridge.sock"),
        pid=4321,
        flame_version="2025.2.4",
        project_name=None,
        bridge_abi=1,
    )
    transport._write_inflight(
        tmp_path, "rid", "lbl", "main", 10.0, candidate, "_result = 2", "cd" * 16
    )
    rows = transport.read_inflight_records(tmp_path)
    assert [r.record["id"] for r in rows.items] == ["rid"]
    assert rows.items[0].record["handle"] == "cd" * 16 and rows.items[0].record["code"]


def test_revert_description_says_the_listed_groups_are_the_landed_later_ones(rig: Rig) -> None:
    op = rig.registry.get("journal.revert")
    assert op is not None and "landed" in op.description.split("newerGroups")[0][-200:]


def test_the_silent_noop_hint_limits_the_modal_remark_to_media_panel_writes() -> None:
    from flame_mcp import errors

    hint = errors.hint_for("SILENT_NOOP")
    assert "Media Panel" in hint and "modal" in hint
