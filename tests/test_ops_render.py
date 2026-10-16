"""Tests for the `render` category: `render.plan` and `render.batch`.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py).

The fake's `PyBatch.render` writes the numbered frames of every effective Write File into the
directory the node's `media_path` names (plus a `.clip`) and puts a clip into the shelf reel
`Batch Renders`, as measured on Flame 2025.2.4. What a real Flame writes is measured
by scripts/p2_3c_render_e2e.py; a test here describes the FAKE where it looks at the output."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyBatch, PyClip, PyRenderNode, PyWriteFileNode
from flame_mcp import render_files, transport
from flame_mcp.journal import Index, IntentView
from flame_mcp.policy import Policy
from flame_mcp.transport import BridgeProtocolError, BridgeResult
from journal_test_helpers import BG1, FakeBridge, Rig, bridge_result, make_rig

BG2 = {"kind": "batch", "name": "BG2"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def bg(rig: Rig, key: str = "bg1") -> PyBatch:
    batch: PyBatch = rig.handles[key]
    return batch


def add_writer(
    rig: Rig,
    tmp_path: Path,
    name: str = "writefile1",
    *,
    first: int = 1,
    last: int = 8,
    directory: Path | None = None,
    connected: bool = True,
    bypass: bool = False,
    make_dir: bool = True,
    key: str = "bg1",
) -> PyWriteFileNode:
    out = directory if directory is not None else tmp_path / f"out_{name}"
    if make_dir:
        out.mkdir(parents=True, exist_ok=True)
    writer = PyWriteFileNode(name, media_path=str(out) + "/", first=first, last=last)
    writer.bypass.set_value(bypass)
    batch = bg(rig, key)
    batch.nodes.append(writer)
    writer._owner = batch
    if connected:
        source = next(n for n in batch.nodes if n.name.get_value() == "blur1")
        batch.connect_nodes(source, "Default", writer, "Default")
    return writer


def render_records(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("kind") == "render" or r.get("type") == "outcome"]


def intents(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "intent"]


def outcomes(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "outcome"]


def states(rig: Rig) -> dict[str, str]:
    env = rig.do("journal.list", {})
    return {item["group"]: item["state"] for item in env["result"]["items"]}


def render(rig: Rig, **extra: Any) -> dict[str, Any]:
    return rig.do("render.batch", {"target": BG1, **extra})


def frames(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir() if p.name.endswith(".exr"))


def test_the_plan_of_a_write_file_says_what_a_render_would_write(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)
    env = rig.do("render.plan", {"target": BG1})
    assert env["ok"] is True, env
    plan = env["result"]
    assert plan["batch"] == {"uid": "uid-b1", "name": "BG1"}
    assert plan["stable"] == {"kind": "batch", "uid": "uid-b1"}
    node = plan["nodes"][0]
    out = str(tmp_path / "out_writefile1")
    assert node["name"] == "writefile1" and node["kind"] == "write_file"
    assert node["bypass"] is False and node["wiredInputs"] == 1
    assert node["effective"] is True and node["skipReason"] is None
    assert node["mediaPath"] == out + "/" and node["mediaPathPattern"] == "<name>"
    assert node["fileType"] == "OpenEXR" and node["formatExtension"] == "exr"
    assert (node["rangeStart"], node["rangeEnd"], node["plannedFrames"]) == (1, 8, 8)
    assert node["resolvedMediaPath"] == f"{out}/writefile1[000001-000008].exr"
    assert node["outputDir"] == out and node["dirExists"] is True
    assert node["defaultPath"] is False and node["sequence"] is True
    assert node["plannedFiles"] == 8
    assert (node["firstFile"], node["lastFile"]) == (
        "writefile1000001.exr",
        "writefile1000008.exr",
    )
    assert node["existingPlannedFiles"] == 0
    assert plan["summary"]["effectiveNodes"] == 1 and plan["summary"]["plannedFrames"] == 8
    assert plan["summary"]["wouldRefuse"] == []


def test_the_plan_counts_a_range_not_the_duration_and_lists_the_group_reels(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, first=1, last=100)  # the default range: 100 frames
    bg(rig).create_reel("Schematic Reel 1")
    plan = rig.do("render.plan", {"target": BG1})["result"]
    assert plan["nodes"][0]["plannedFrames"] == 100
    assert {"kind": "schematic", "name": "Schematic Reel 1", "clipCount": 0} in plan["reels"]
    over = [p for p in plan["summary"]["wouldRefuse"] if p["code"] == "too_many_frames"]
    assert over == [] and plan["summary"]["plannedFrames"] == 100  # 100 <= the default 1000


def test_the_plan_marks_bypassed_and_unconnected_nodes_ineffective(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, "w_ok")
    add_writer(rig, tmp_path, "w_bypass", bypass=True)
    add_writer(rig, tmp_path, "w_loose", connected=False)
    plan = rig.do("render.plan", {"target": BG1})["result"]
    by_name = {n["name"]: n for n in plan["nodes"]}
    assert by_name["w_ok"]["effective"] is True
    assert by_name["w_bypass"]["skipReason"] == "bypassed"
    assert by_name["w_loose"]["skipReason"] == "no input is connected"
    assert plan["summary"]["effectiveNodes"] == 1
    assert plan["summary"]["plannedFrames"] == 8
    assert {s["name"] for s in plan["summary"]["skippedNodes"]} == {"w_bypass", "w_loose"}


def test_the_plan_of_a_render_node_says_where_its_clip_goes(rig: Rig) -> None:
    node = PyRenderNode("render1", 1, 24)
    bg(rig).nodes.append(node)
    node._owner = bg(rig)
    source = next(n for n in bg(rig).nodes if n.name.get_value() == "blur1")
    bg(rig).connect_nodes(source, "Default", node, "Default")
    row = rig.do("render.plan", {"target": BG1})["result"]["nodes"][0]
    assert row["kind"] == "render" and row["pyType"] == "PyRenderNode"
    assert row["destination"] == ["Batch Reels", "Batch Renders"] and row["tapeName"] == "Batch"
    assert row["plannedFrames"] == 24 and row["effective"] is True
    assert "resolvedMediaPath" not in row and "outputSpec" not in row
    assert "cannot be checked on disk" in row["output"] or "cannot" in row["output"]


def test_the_plan_reports_a_directory_that_is_not_there_and_files_that_would_be_overwritten(
    rig: Rig, tmp_path: Path
) -> None:
    gone = add_writer(rig, tmp_path, "w_gone", directory=tmp_path / "nowhere", make_dir=False)
    there = add_writer(rig, tmp_path, "w_there")
    out = Path(str(there.media_path.get_value()))
    (out / "w_there000002.exr").write_bytes(b"old")
    plan = rig.do("render.plan", {"target": BG1})["result"]
    by_name = {n["name"]: n for n in plan["nodes"]}
    assert by_name["w_gone"]["dirExists"] is False and gone is not None
    assert by_name["w_there"]["existingPlannedFiles"] == 1
    assert [p["code"] for p in plan["summary"]["wouldRefuse"]] == [
        "output_dir_missing",
        "would_overwrite",
    ]


def test_the_plan_and_a_render_also_work_with_the_dotted_form_plan_48_recorded(
    rig: Rig, tmp_path: Path
) -> None:
    """`writefile1.[000001-000004].exr` (frames `writefile1.000001.exr`) is the other resolved-path
    form; the same derivation reads it."""
    writer = add_writer(rig, tmp_path, last=4)
    writer.dotted = True
    node = rig.do("render.plan", {"target": BG1})["result"]["nodes"][0]
    out = str(tmp_path / "out_writefile1")
    assert node["resolvedMediaPath"] == f"{out}/writefile1.[000001-000004].exr"
    assert node["sequence"] is True and node["plannedFiles"] == 4
    assert (node["firstFile"], node["lastFile"]) == (
        "writefile1.000001.exr",
        "writefile1.000004.exr",
    )
    env = render(rig)
    assert env["ok"] is True, env
    assert frames(tmp_path / "out_writefile1") == [f"writefile1.{n:06d}.exr" for n in range(1, 5)]
    assert env["result"]["writtenFrames"] == 4


def test_the_plan_says_a_render_of_these_frames_occupies_flame_without_estimating_a_time(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=8)
    summary = rig.do("render.plan", {"target": BG1})["result"]["summary"]
    note = summary["busyNote"]
    assert "these 8 planned frames" in note and "occupies Flame" in note
    assert "the bridge may not answer MCP calls (reads too) until it ends" in note
    assert " s " not in note and "second" not in note and "minute" not in note


def test_the_plan_flags_the_default_output_directory(rig: Rig, tmp_path: Path) -> None:
    writer = add_writer(rig, tmp_path)
    writer.media_path.set_value("/var/tmp/")
    node = rig.do("render.plan", {"target": BG1})["result"]["nodes"][0]
    assert node["defaultPath"] is True
    assert node["resolvedMediaPath"] == "/var/tmp/writefile1[000001-000008].exr"


def test_the_plan_of_a_group_without_render_nodes_is_empty_and_says_a_render_would_refuse(
    rig: Rig,
) -> None:
    env = rig.do("render.plan", {"target": BG2})
    assert env["ok"] is True and env["result"]["nodes"] == []
    assert [p["code"] for p in env["result"]["summary"]["wouldRefuse"]] == ["no_effective_node"]


def test_the_plan_notes_a_resolved_path_that_disagrees_with_the_range(
    rig: Rig, tmp_path: Path
) -> None:
    writer = add_writer(rig, tmp_path, last=8)
    writer.get_resolved_media_path = lambda: str(  # type: ignore[method-assign]
        tmp_path / "out_writefile1" / "writefile1[000001-000010].exr"
    )
    node = rig.do("render.plan", {"target": BG1})["result"]["nodes"][0]
    assert node["plannedFrames"] == 8 and node["plannedFiles"] == 10
    assert any("range_start / range_end say 8" in n for n in node["notes"])
    assert rig.do("render.plan", {"target": BG1})["result"]["summary"]["plannedFrames"] == 10


def test_the_plan_of_a_missing_group_is_selector_empty(rig: Rig) -> None:
    env = rig.do("render.plan", {"target": {"kind": "batch", "name": "nope"}})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY"


def test_the_plan_writes_nothing_and_journals_nothing(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path)
    before = sorted(os.listdir(tmp_path / "out_writefile1"))
    rig.do("render.plan", {"target": BG1})
    assert sorted(os.listdir(tmp_path / "out_writefile1")) == before
    assert bg(rig).render_calls == [] and rig.records() == []


def assert_nothing_happened(rig: Rig, env: dict[str, Any]) -> None:
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert env["result"]["ran"] is False
    assert bg(rig).render_calls == []
    assert rig.records() == []
    assert rig.exec.labels().count("render.batch") == 0
    assert not rig.runtime.guard.blocked()


def test_a_write_file_on_the_default_path_is_refused_before_anything_is_written(
    rig: Rig, tmp_path: Path
) -> None:
    writer = add_writer(rig, tmp_path)
    writer.media_path.set_value("/var/tmp/")
    env = render(rig)
    assert_nothing_happened(rig, env)
    assert [p["code"] for p in env["result"]["problems"]] == ["default_path"]
    assert "allow_default_path" in env["error"] and "/var/tmp" in env["error"]
    assert env["result"]["plan"]["nodes"][0]["name"] == "writefile1"


def test_the_default_path_is_allowed_when_asked(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory is treated as the default one here (`/var/tmp` itself is never written)."""
    writer = add_writer(rig, tmp_path)
    monkeypatch.setattr(
        render_files, "DEFAULT_OUTPUT_DIRS", (str(Path(str(writer.media_path.get_value()))),)
    )
    assert_nothing_happened(rig, render(rig))
    env = render(rig, allow_default_path=True)
    assert env["ok"] is True, env
    assert len(frames(tmp_path / "out_writefile1")) == 8


def test_planned_files_that_already_exist_are_refused_before_anything_is_written(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=4)
    out = tmp_path / "out_writefile1"
    (out / "writefile1000002.exr").write_bytes(b"old")
    (out / "writefile1000003.exr").write_bytes(b"old")
    env = render(rig)
    assert_nothing_happened(rig, env)
    (problem,) = env["result"]["problems"]
    assert problem["code"] == "would_overwrite" and problem["node"] == "writefile1"
    assert "2" in problem["message"] and "allow_overwrite=true" in env["error"]
    assert (out / "writefile1000002.exr").read_bytes() == b"old"
    assert frames(out) == ["writefile1000002.exr", "writefile1000003.exr"]


def test_allow_overwrite_false_is_the_same_as_leaving_it_out(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=4)
    (tmp_path / "out_writefile1" / "writefile1000001.exr").write_bytes(b"old")
    assert_nothing_happened(rig, render(rig, allow_overwrite=False))


def test_allow_overwrite_renders_over_the_existing_files(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=4)
    out = tmp_path / "out_writefile1"
    (out / "writefile1000002.exr").write_bytes(b"old")
    env = render(rig, allow_overwrite=True)
    assert env["ok"] is True, env
    assert env["result"]["nodes"][0]["overwrote"] == 1
    assert (out / "writefile1000002.exr").read_bytes() != b"old"
    assert len(frames(out)) == 4


def test_the_overwrite_check_ignores_nodes_that_are_not_effective(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, "w_ok", last=3)
    skipped = add_writer(rig, tmp_path, "w_bypass", last=3, bypass=True)
    (Path(str(skipped.media_path.get_value())) / "w_bypass000001.exr").write_bytes(b"old")
    env = render(rig)
    assert env["ok"] is True, env


def test_the_plan_says_render_batch_would_refuse_existing_files(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=4)
    (tmp_path / "out_writefile1" / "writefile1000001.exr").write_bytes(b"old")
    summary = rig.do("render.plan", {"target": BG1})["result"]["summary"]
    assert [p["code"] for p in summary["wouldRefuse"]] == ["would_overwrite"]
    assert "allow_overwrite=false" in summary["wouldRefuseNote"]


def test_an_overwriting_render_is_denied_when_destructive_is_not_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restricted = make_rig(
        monkeypatch,
        tmp_path,
        policy=Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,reversible,create"}),
    )
    add_writer(restricted, tmp_path, last=4)
    (tmp_path / "out_writefile1" / "writefile1000001.exr").write_bytes(b"old")
    denied = render(restricted, allow_overwrite=True)
    assert denied["ok"] is False and denied["errorCode"] == "POLICY_DENIED", denied
    assert "FLAME_MCP_ALLOW_EFFECTS" in denied["error"]
    assert bg(restricted).render_calls == [] and restricted.records() == []
    refused = render(restricted)
    assert refused["errorCode"] == "PRECONDITION_UNMET"


def test_a_render_that_overwrites_nothing_needs_no_flag_under_that_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restricted = make_rig(
        monkeypatch,
        tmp_path,
        policy=Policy.from_env({"FLAME_MCP_ALLOW_EFFECTS": "read,reversible,create"}),
    )
    add_writer(restricted, tmp_path, last=4)
    assert render(restricted)["ok"] is True


def test_more_frames_than_max_frames_is_refused_and_the_default_range_counts(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, first=1, last=100)
    env = render(rig, max_frames=50)
    assert_nothing_happened(rig, env)
    assert [p["code"] for p in env["result"]["problems"]] == ["too_many_frames"]
    assert "100 frames" in env["error"] and "max_frames=50" in env["error"]


def test_exactly_max_frames_is_allowed(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, first=1, last=8)
    assert render(rig, max_frames=8)["ok"] is True


def test_the_default_max_frames_is_1000(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, first=1, last=1001)
    env = render(rig)
    assert_nothing_happened(rig, env)
    assert "max_frames=1000" in env["error"]


def test_a_group_without_an_effective_node_is_refused(rig: Rig, tmp_path: Path) -> None:
    assert_nothing_happened(rig, rig.do("render.batch", {"target": BG2}))
    add_writer(rig, tmp_path, "w_bypass", bypass=True)
    add_writer(rig, tmp_path, "w_loose", connected=False)
    env = render(rig)
    assert_nothing_happened(rig, env)
    assert [p["code"] for p in env["result"]["problems"]] == ["no_effective_node"]


def test_a_missing_output_directory_is_refused(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, directory=tmp_path / "nowhere", make_dir=False)
    env = render(rig)
    assert_nothing_happened(rig, env)
    assert [p["code"] for p in env["result"]["problems"]] == ["output_dir_missing"]
    assert str(tmp_path / "nowhere") in env["error"]


def test_bypassed_and_unconnected_nodes_are_not_counted_towards_max_frames_or_paths(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, "w_ok", first=1, last=8)
    big = add_writer(rig, tmp_path, "w_bypass", first=1, last=100, bypass=True)
    big.media_path.set_value("/var/tmp/")  # would be refused twice over if it counted
    loose = add_writer(rig, tmp_path, "w_loose", first=1, last=100, connected=False)
    loose.media_path.set_value("/nowhere/at/all/")
    env = render(rig, max_frames=10)
    assert env["ok"] is True, env
    assert [n["node"] for n in env["result"]["nodes"]] == ["w_ok"]
    assert env["result"]["expectedFrames"] == 8


def test_every_problem_is_listed_not_just_the_first(rig: Rig, tmp_path: Path) -> None:
    a = add_writer(rig, tmp_path, "a", first=1, last=100)
    a.media_path.set_value("/var/tmp/")
    add_writer(rig, tmp_path, "b", directory=tmp_path / "nowhere", make_dir=False)
    env = render(rig, max_frames=10)
    assert {p["code"] for p in env["result"]["problems"]} == {
        "default_path",
        "output_dir_missing",
        "too_many_frames",
    }


def test_bad_arguments_are_invalid_args(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path)
    for extra in ({"timeout_s": 0}, {"timeout_s": 3601}, {"max_frames": 0}, {"max_frames": 100001}):
        env = render(rig, **extra)
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", extra
    assert render(rig, timeout_s=3600, max_frames=100000)["errorCode"] != "INVALID_ARGS"
    assert render(rig, timeout_s=1.5)["errorCode"] == "INVALID_ARGS"
    assert len(bg(rig).render_calls) == 1  # only the valid call rendered


def test_a_missing_group_is_selector_empty_and_nothing_is_journaled(rig: Rig) -> None:
    env = rig.do("render.batch", {"target": {"kind": "batch", "name": "nope"}})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert env["result"]["ran"] is False and rig.records() == []


def test_a_render_writes_the_frames_and_reports_and_journals_them(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)
    env = render(rig)
    assert env["ok"] is True, env
    out = tmp_path / "out_writefile1"
    assert len(frames(out)) == 8 and (out / "writefile1.clip").exists()
    result = env["result"]
    assert result["ran"] is True and result["renderOption"] == "Foreground"
    assert result["returned"] is True and result["expectedFrames"] == 8
    assert result["writtenFrames"] == 8 and result["verified"] is True
    (row,) = result["nodes"]
    assert row["node"] == "writefile1" and row["complete"] is True and row["missing"] == []
    assert row["otherNewFiles"] == ["writefile1.clip"] and row["overwrote"] == 0
    assert result["newClips"] == [
        {"kind": "shelf", "name": "Batch Renders", "clips": ["writefile1"]}
    ]
    assert env["affected"] == 8 and env["undoSteps"] == 0
    assert env["execution"]["op"] == "render.batch" and env["execution"]["undo"] == "unsupported"
    assert env["journal"]["revert"] is None and env["journal"]["undo"] == "unsupported"
    assert env["partial"] == {"written": 8, "expected": 8}


def test_the_intent_is_flushed_before_the_render_and_lists_what_was_there(
    rig: Rig, tmp_path: Path
) -> None:
    writer = add_writer(rig, tmp_path, last=4)
    out = tmp_path / "out_writefile1"
    (out / "notes.txt").write_bytes(b"x")
    (out / "writefile1000002.exr").write_bytes(b"old")
    env = render(rig, allow_overwrite=True)
    assert env["ok"] is True and writer is not None
    call = next(c for c in rig.exec.calls if c.label == "render.batch")
    assert "intent" in call.journal_types and "outcome" not in call.journal_types
    assert call.timeout_s == 120.0
    (intent,) = intents(rig)
    assert intent["op"] == "render.batch" and intent["kind"] == "render"
    assert intent["target"] == {"kind": "batch", "uid": "uid-b1"}
    assert intent["renderOption"] == "Foreground" and intent["expectedFrames"] == 4
    (node,) = intent["nodes"]
    assert node["name"] == "writefile1" and node["plannedFrames"] == 4
    assert node["spec"]["directory"] == str(out) and node["spec"]["last"] == 4
    assert list(node["preexisting"]) == ["writefile1000002.exr"]
    assert node["directory"]["count"] == 2
    assert node["directory"]["names"] == ["notes.txt", "writefile1000002.exr"]
    assert env["result"]["nodes"][0]["overwrote"] == 1
    assert env["result"]["writtenFrames"] == 4  # the overwritten frame counts: it was rewritten


def test_the_outcome_is_journaled_and_the_group_reads_landed(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=6)
    env = render(rig)
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is True and outcome["verdict"] == "landed"
    assert (outcome["expectedFrames"], outcome["writtenFrames"]) == (6, 6)
    assert outcome["returned"] is True and outcome["newClips"][0]["clips"] == ["writefile1"]
    assert states(rig)[env["journal"]["group"]] == "landed"
    assert rig.runtime.guard.blocked() == []


def test_the_timeout_reaches_the_bridge(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path)
    render(rig, timeout_s=7)
    call = next(c for c in rig.exec.calls if c.label == "render.batch")
    assert call.timeout_s == 7.0
    assert bg(rig).render_calls == ["Foreground"]


def test_the_run_is_sent_by_the_groups_uid_with_no_loop_in_the_generated_code(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path)
    render(rig)
    code = next(c.code for c in rig.exec.calls if c.label == "render.batch")
    assert (
        "_render_run(target)" in code
        and "'uid-b1'" in code
        and "'BG1'" not in code.split("__op_src =")[1].split("\n")[0]
    )


def test_two_write_files_are_both_counted_and_checked(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, "wa", first=1, last=5)
    add_writer(rig, tmp_path, "wb", first=3, last=6)
    env = render(rig)
    assert env["ok"] is True, env
    assert env["result"]["expectedFrames"] == 9 and env["result"]["writtenFrames"] == 9
    assert {n["node"] for n in env["result"]["nodes"]} == {"wa", "wb"}
    assert len(env["result"]["newClips"][0]["clips"]) == 2


def test_two_write_files_in_one_directory_do_not_report_each_others_frames_as_other_files(
    rig: Rig, tmp_path: Path
) -> None:
    shared = tmp_path / "shared"
    add_writer(rig, tmp_path, "wa", first=1, last=3, directory=shared)
    add_writer(rig, tmp_path, "wb", first=1, last=3, directory=shared)
    env = render(rig)
    assert env["ok"] is True, env
    others = [f for row in env["result"]["nodes"] for f in row["otherNewFiles"]]
    assert sorted(others) == ["wa.clip", "wb.clip"]


def test_a_render_of_a_render_node_only_is_not_verified_on_disk(rig: Rig) -> None:
    node = PyRenderNode("render1", 1, 24)
    bg(rig).nodes.append(node)
    node._owner = bg(rig)
    source = next(n for n in bg(rig).nodes if n.name.get_value() == "blur1")
    bg(rig).connect_nodes(source, "Default", node, "Default")
    env = render(rig)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["verified"] is None and result["expectedFrames"] == 0
    assert "Render nodes" in result["verifiedNote"]
    assert result["nodes"] == [{"node": "render1", "kind": "render", "checked": False}]
    assert env["partial"] is None


def test_fewer_frames_than_planned_is_silent_noop_and_the_outcome_says_partial(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_frames_limit = 5
    env = render(rig)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert "5 of 8" in env["error"]
    result = env["result"]
    assert result["writtenFrames"] == 5 and result["verified"] is False
    row = result["nodes"][0]
    assert row["complete"] is False and row["missing"] == [
        f"writefile1{n:06d}.exr" for n in (6, 7, 8)
    ]
    assert env["partial"] == {"written": 5, "expected": 8}
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is False and outcome["verdict"] == "partial"
    assert outcome["errorCode"] == "SILENT_NOOP"
    assert states(rig)[env["journal"]["group"]] == "partial"
    assert rig.runtime.guard.blocked() == []  # an answer, not a lost call


def test_no_frames_at_all_is_silent_noop_and_the_group_reads_failed(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_frames_limit = 0
    env = render(rig)
    assert env["errorCode"] == "SILENT_NOOP" and env["result"]["writtenFrames"] == 0
    assert outcomes(rig)[0]["verdict"] == "not_landed"
    assert states(rig)[env["journal"]["group"]] == "failed"


def test_a_render_that_returns_false_and_is_short_is_a_flame_api_error(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_frames_limit = 2
    bg(rig).render_returns = False
    env = render(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["returned"] is False


def test_a_render_that_returns_false_but_wrote_everything_succeeds_with_a_note(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=4)
    bg(rig).render_returns = False
    env = render(rig)
    assert env["ok"] is True and env["result"]["writtenFrames"] == 4
    assert any("returned False" in d for d in env["degraded"])


def test_a_render_that_raises_is_a_flame_api_error_with_the_files_it_left(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=4)
    bg(rig).render_raises = "out of disk"
    env = render(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "out of disk" in env["error"]
    assert env["result"]["ran"] is True and env["result"]["writtenFrames"] == 0
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is False and outcome["errorCode"] == "FLAME_API_ERROR"
    assert rig.runtime.guard.blocked() == []


def test_a_render_that_raises_after_some_frames_reads_partial(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)

    def crash_after_three(batch: PyBatch) -> None:
        for node in batch.nodes:
            if isinstance(node, PyWriteFileNode):
                node.write_frames(3)
        raise RuntimeError("stopped")

    bg(rig).render_before_write = crash_after_three
    env = render(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert env["result"]["writtenFrames"] == 3
    assert outcomes(rig)[0]["verdict"] == "partial"


def old_bridge(rig: Rig) -> None:
    """Fake older bridge that does not announce `client_handle`.

    The server gives the run no handle, so a TIMEOUT / BRIDGE_LOST leaves nothing to ask the
    bridge with and the files decide.
    """
    rig.bridge.features = ()


def blocked_groups(rig: Rig) -> list[str]:
    return [b.group for b in rig.runtime.guard.blocked()]


def test_a_timeout_closes_the_write_door_and_leaves_the_intent_open(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "TIMEOUT", when="after")  # the render ran, the answer is lost
    env = render(rig, timeout_s=1)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert "journal.check" in env["hint"] and "keeps" in env["hint"]
    assert "may not answer at all" in env["hint"] and "again" in env["hint"]
    assert env["journal"]["group"] in blocked_groups(rig)
    assert outcomes(rig) == [] and len(intents(rig)) == 1
    assert [r["type"] for r in rig.records()] == ["intent", "unresolved"]
    denied = rig.do(
        "node.set_attributes",
        {
            "target": {"kind": "node", "batch": BG1, "name": "comp1"},
            "values": {"blend_type": "Photoshop"},
        },
    )
    assert denied["errorCode"] == "POLICY_DENIED"
    assert render(rig)["errorCode"] == "POLICY_DENIED"
    assert bg(rig).render_calls == ["Foreground"]  # the second one never reached Flame
    assert rig.do("render.plan", {"target": BG1})["ok"] is True  # reads still work


def test_check_after_a_timeout_reads_landed_when_every_frame_is_there_and_reopens_writes(
    rig: Rig, tmp_path: Path
) -> None:
    old_bridge(rig)
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    env = rig.do("journal.check", {"group": group})
    assert env["ok"] is True, env
    (entry,) = env["result"]["checked"]
    assert entry["verdict"] == "landed" and entry["perKey"] == {"writefile1": "landed"}
    assert entry["current"]["writtenFrames"] == 8 and entry["current"]["expectedFrames"] == 8
    assert env["result"]["writesBlocked"] is False and blocked_groups(rig) == []
    assert states(rig)[group] == "landed"
    assert rig.exec.labels().count("render.plan") >= 2  # Flame was read again by the check
    ok = rig.do(
        "node.set_attributes",
        {
            "target": {"kind": "node", "batch": BG1, "name": "comp1"},
            "values": {"blend_type": "Photoshop"},
        },
    )
    assert ok["ok"] is True, ok


def test_check_reads_partial_when_only_some_frames_were_written(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_frames_limit = 3
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    env = rig.do("journal.check", {"group": group})
    (entry,) = env["result"]["checked"]
    assert entry["verdict"] == "partial" and entry["perKey"] == {"writefile1": "partial"}
    assert entry["current"]["writtenFrames"] == 3 and entry["current"]["expectedFrames"] == 8
    assert entry["current"]["nodes"][0]["missing"][0] == "writefile1000004.exr"
    assert states(rig)[group] == "partial"
    assert blocked_groups(rig) == []  # a finding of any kind lifts the door


def test_check_reads_not_landed_when_the_render_never_started(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="before")
    env = render(rig, timeout_s=1)
    assert env["errorCode"] == "BRIDGE_LOST" and bg(rig).render_calls == []
    group = env["journal"]["group"]
    (entry,) = rig.do("journal.check", {"group": group})["result"]["checked"]
    assert entry["verdict"] == "not_landed" and entry["current"]["writtenFrames"] == 0
    assert states(rig)[group] == "not_landed"


def test_check_does_not_count_a_file_that_was_there_before_the_render(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=3)
    out = tmp_path / "out_writefile1"
    for n in (1, 2, 3):
        (out / f"writefile1{n:06d}.exr").write_bytes(b"old-contents")
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="before")  # nothing rewrites them
    group = render(rig, timeout_s=1, allow_overwrite=True)["journal"]["group"]
    (entry,) = rig.do("journal.check", {"group": group})["result"]["checked"]
    assert entry["verdict"] == "not_landed" and entry["current"]["writtenFrames"] == 0


def test_check_sees_frames_rewritten_over_files_that_were_there(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=3)
    out = tmp_path / "out_writefile1"
    for n in (1, 2, 3):
        (out / f"writefile1{n:06d}.exr").write_bytes(b"old-contents")
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    group = render(rig, timeout_s=1, allow_overwrite=True)["journal"]["group"]
    (entry,) = rig.do("journal.check", {"group": group})["result"]["checked"]
    assert entry["verdict"] == "landed" and entry["current"]["writtenFrames"] == 3


def test_check_stays_unknown_and_the_door_stays_shut_while_flame_cannot_be_read(
    rig: Rig, tmp_path: Path
) -> None:
    old_bridge(rig)
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    rig.exec.fault("render.plan", "BRIDGE_UNAVAILABLE", when="before")
    env = rig.do("journal.check", {"group": group})
    (entry,) = env["result"]["checked"]
    assert entry["verdict"] == "unknown" and "Flame could not be read" in entry["detail"]
    assert "may not answer at all" in entry["detail"] and "journal.check again" in entry["detail"]
    assert env["result"]["stillUnknown"] == 1 and env["result"]["writesBlocked"] is True
    assert blocked_groups(rig) == [group]
    assert rig.do("journal.check", {"group": group})["result"]["writesBlocked"] is False


def test_check_says_the_same_about_a_read_that_timed_out_but_not_about_other_failures(
    rig: Rig, tmp_path: Path
) -> None:
    old_bridge(rig)
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    rig.exec.fault("render.plan", "TIMEOUT", when="before")
    (entry,) = rig.do("journal.check", {"group": group})["result"]["checked"]
    assert entry["verdict"] == "unknown" and "may not answer at all" in entry["detail"]
    rig.exec.fault("render.plan", "FLAME_API_ERROR", when="before")
    (other,) = rig.do("journal.check", {"group": group})["result"]["checked"]
    assert other["verdict"] == "unknown" and "may not answer at all" not in other["detail"]


def test_check_decides_from_the_files_when_the_group_is_gone(rig: Rig, tmp_path: Path) -> None:
    old_bridge(rig)
    add_writer(rig, tmp_path, last=4)
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    rig.flame.projects.current_project.current_workspace.desktop.batch_groups.remove(bg(rig))
    (entry,) = rig.do("journal.check", {"group": group})["result"]["checked"]
    assert entry["verdict"] == "landed" and entry["groupFound"] is False


@pytest.fixture
def bridge(rig: Rig) -> FakeBridge:
    return rig.bridge


def pending_render(
    rig: Rig,
    bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    *,
    handle: str = "h123",
    finished: bool = True,
    client: bool = False,
    ignores_client_handle: bool = False,
) -> dict[str, Any]:
    """`render.batch(timeout_s=1)` whose wait ended with the bridge's pending handle, as the real
    main-async bridge answers. The render itself ran (the fake Flame writes its files); with
    `finished` the bridge holds its answer for `poll`, else the handle is still running.
    By default the bridge is an OLD one: it announces no `client_handle`, so no handle is sent and
    `handle` is its own. With `client` it announces the feature and answers pending with the
    handle the server sent (with `ignores_client_handle` it answers with `handle` anyway)."""
    scripted = rig.exec
    bridge.features = ("client_handle",) if client else ()
    bridge_own = handle

    def pending(
        code: str,
        thread: str,
        timeout_s: float | None = None,
        label: str | None = None,
        *,
        run_dir: Path | None = None,
        handle: str | None = None,
    ) -> BridgeResult:
        real = scripted(code, thread, timeout_s, label, run_dir=run_dir, handle=handle)
        if label != "render.batch":
            return real
        answered = handle if client and handle and not ignores_client_handle else bridge_own
        if finished:
            bridge.results[answered] = real
        else:
            bridge.running.add(answered)
        return bridge_result(pending_handle=answered)

    monkeypatch.setattr(transport, "exec", pending)
    return render(rig, timeout_s=1)


def check(rig: Rig, group: str) -> dict[str, Any]:
    env = rig.do("journal.check", {"group": group})
    assert env["ok"] is True, env
    checked: list[dict[str, Any]] = env["result"]["checked"]
    return checked[0] if checked else {}


def record_types(rig: Rig) -> list[str]:
    return [str(r["type"]) for r in rig.records()]


def test_a_bridge_pending_handle_is_a_timeout_that_closes_the_door_and_the_handle_is_journaled(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)
    env = pending_render(rig, bridge, monkeypatch)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert "pending handle" in env["error"] and env["pending"] == {"handle": "h123"}
    assert env["journal"]["group"] in blocked_groups(rig)
    assert record_types(rig) == ["intent", "unresolved", "pending"]
    pending = rig.records()[-1]
    assert pending["handle"] == "h123" and pending["timeoutS"] == 1.0
    assert pending["id"] == intents(rig)[0]["id"] and pending["group"] == env["journal"]["group"]
    (view,) = Index(rig.journal.read_all().records).groups[env["journal"]["group"]]
    assert view.pending_handle == "h123" and view.polled is None
    assert bridge.calls == []  # nothing polled by the render itself


def test_a_timeout_without_a_handle_journals_no_pending_record(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    render(rig, timeout_s=1)
    assert record_types(rig) == ["intent", "unresolved"]


def test_check_asks_the_bridge_first_and_counts_the_files_once_the_result_is_in(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    plan_reads_before = rig.exec.labels().count("render.plan")
    entry = check(rig, group)
    assert bridge.calls == ["h123"]
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert entry["perKey"] == {"writefile1": "landed"}
    assert entry["current"]["writtenFrames"] == 8 and entry["current"]["expectedFrames"] == 8
    assert entry["renderOutcome"]["ok"] is True and entry["renderOutcome"]["returned"] is True
    assert entry["renderOutcome"]["newClips"][0]["clips"] == ["writefile1"]
    assert rig.exec.labels().count("render.plan") == plan_reads_before  # Flame is not read
    assert blocked_groups(rig) == [] and states(rig)[group] == "landed"
    types = record_types(rig)
    assert types.index("polled") < types.index("check")
    polled = next(r for r in rig.records() if r["type"] == "polled")
    assert polled["handle"] == "h123" and polled["ok"] is True
    assert polled["flame"]["returned"] is True and polled["flame"]["elapsedS"] is not None
    checked = next(r for r in rig.records() if r["type"] == "check")
    assert checked["basis"] == "handle" and checked["verdict"] == "landed"
    assert checked["flame"]["newClips"][0]["clips"] == ["writefile1"]
    assert set(checked["flame"]) == {
        "returned",
        "elapsedS",
        "newClips",
        "reelsBefore",
        "reelsAfter",
    }


def test_complete_frames_do_not_end_the_render_while_the_bridge_says_pending(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The e2e: the last frame was on disk about 8 s before render() returned."""
    add_writer(rig, tmp_path, last=8)
    group = pending_render(rig, bridge, monkeypatch, finished=False)["journal"]["group"]
    assert len(frames(tmp_path / "out_writefile1")) == 8  # every planned frame is there
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "still running" in entry["detail"]
    assert "journal.check again" in entry["detail"] and "basis" not in entry
    assert blocked_groups(rig) == [group] and states(rig)[group] == "unknown"
    assert "check" not in record_types(rig) and "polled" not in record_types(rig)
    # the render ends: the bridge now has its answer
    bridge.running.clear()
    bridge.results["h123"] = bridge_result(result=_wrapper_result_of_last_render(rig))
    entry2 = check(rig, group)
    assert entry2["verdict"] == "landed" and entry2["basis"] == "handle"
    assert blocked_groups(rig) == []


def _wrapper_result_of_last_render(rig: Rig) -> dict[str, Any]:
    """A wrapper result for a finished render (what the bridge's result file holds)."""
    return {
        "value": {
            "renderOption": "Foreground",
            "returned": True,
            "elapsedS": 9.273,
            "newClips": [{"kind": "shelf", "name": "Batch Renders", "clips": ["writefile1"]}],
            "reelsBefore": [],
            "reelsAfter": [],
        },
        "affected": 1,
        "partial": None,
        "opError": None,
        "opErrorCode": None,
        "opTraceback": None,
        "verified": None,
        "verifyDetail": None,
        "context": None,
    }


def test_a_result_that_was_read_once_is_used_again_by_the_next_check(
    rig: Rig,
    tmp_path: Path,
    bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge deletes a result when it hands it over. If the check that fetched it does not
    finish (here: its own journal write fails), the next check must not poll again -- the bridge
    would answer `unknown handle` and the estimate from the files would replace the answer."""
    from flame_mcp import render_check

    add_writer(rig, tmp_path, last=8)
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    real_append = render_check._append_check
    calls = {"n": 0}

    def failing_once(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the journal broke")
        real_append(*args, **kwargs)

    monkeypatch.setattr(render_check, "_append_check", failing_once)
    first = rig.do("journal.check", {"group": group})
    assert first["ok"] is False and "the journal broke" in first["error"]
    assert bridge.calls == ["h123"] and "polled" in record_types(rig)
    assert "check" not in record_types(rig) and blocked_groups(rig) == [group]
    entry = check(rig, group)  # the bridge would now say `unknown handle`
    assert bridge.calls == ["h123"]  # not asked again
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert entry["renderOutcome"]["elapsedS"] is not None
    assert blocked_groups(rig) == [] and states(rig)[group] == "landed"
    assert rig.do("journal.check", {"group": group})["result"]["checked"] == []


def test_a_handle_the_bridge_does_not_know_falls_back_to_the_files_and_says_so(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    bridge.results.clear()  # Flame was restarted: no result file, no pending entry
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "disk"
    assert "no longer knows handle h123" in entry["detail"]
    assert "not confirmed" in entry["detail"] and "estimate from the files" in entry["detail"]
    assert "renderOutcome" not in entry and bridge.calls == ["h123"]
    assert blocked_groups(rig) == []
    checked = next(r for r in rig.records() if r["type"] == "check")
    assert checked["basis"] == "disk" and "polled" not in record_types(rig)


def test_a_handle_the_bridge_does_not_know_and_some_frames_reads_partial_by_disk(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_frames_limit = 3
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    bridge.results.clear()
    entry = check(rig, group)
    assert entry["verdict"] == "partial" and entry["basis"] == "disk"
    assert entry["current"]["writtenFrames"] == 3


def test_the_disk_only_check_says_it_is_an_estimate_too(rig: Rig, tmp_path: Path) -> None:
    old_bridge(rig)
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "disk"
    assert "estimate from the files" in entry["detail"]
    assert "no longer knows" not in entry["detail"]


@pytest.mark.parametrize("code", ["BRIDGE_UNAVAILABLE", "TIMEOUT"])
def test_a_bridge_that_cannot_be_asked_is_unknown_with_the_wait_a_little_note(
    rig: Rig,
    tmp_path: Path,
    bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    add_writer(rig, tmp_path, last=8)
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    bridge.failure = bridge_result(ok=False, error_code=code, error="no reply")
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and code in entry["detail"]
    assert "may not answer at all" in entry["detail"] and "journal.check again" in entry["detail"]
    assert blocked_groups(rig) == [group]
    assert "polled" not in record_types(rig) and "check" not in record_types(rig)
    bridge.failure = None  # the bridge is back, and its answer was never taken
    assert check(rig, group)["verdict"] == "landed"


def test_another_protocol_error_from_the_bridge_is_unknown_not_the_disk_fallback(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    bridge.failure = BridgeProtocolError("corrupt result file: boom")
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "corrupt result file" in entry["detail"]
    assert "basis" not in entry and blocked_groups(rig) == [group]


def test_a_render_that_raised_reads_failed_by_the_handle_even_with_no_frames(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_raises = "out of disk"
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "failed" and entry["basis"] == "handle"
    assert entry["renderOutcome"]["ok"] is False
    assert entry["renderOutcome"]["errorCode"] == "FLAME_API_ERROR"
    assert "out of disk" in entry["renderOutcome"]["error"]
    assert states(rig)[group] == "failed" and blocked_groups(rig) == []


def test_a_render_that_raised_after_some_frames_reads_partial_by_the_handle(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)

    def crash_after_three(batch: PyBatch) -> None:
        for node in batch.nodes:
            if isinstance(node, PyWriteFileNode):
                node.write_frames(3)
        raise RuntimeError("stopped")

    bg(rig).render_before_write = crash_after_three
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "partial" and entry["basis"] == "handle"
    assert entry["current"]["writtenFrames"] == 3


def test_a_render_that_returned_short_reads_like_render_batch_does(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same judgement as `render.batch`'s own: returned True, fewer frames -> SILENT_NOOP;
    returned False -> FLAME_API_ERROR; here as the check's renderOutcome."""
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_frames_limit = 5
    group = pending_render(rig, bridge, monkeypatch, handle="ha")["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "partial"
    assert entry["renderOutcome"]["ok"] is False
    assert entry["renderOutcome"]["errorCode"] == "SILENT_NOOP"
    assert "5 of 8" in entry["renderOutcome"]["error"]


def test_the_check_and_the_run_judge_a_render_alike() -> None:
    from flame_mcp.render_run import judge_render

    done = {"returned": True}
    kwargs: dict[str, Any] = {"env_ok": True, "env_code": None, "env_error": None}
    short = judge_render(**kwargs, flame=done, written=5, expected=8, checked=True)
    assert (short.ok, short.code) == (False, "SILENT_NOOP")
    false_short = judge_render(
        **kwargs, flame={"returned": False}, written=5, expected=8, checked=True
    )
    assert (false_short.ok, false_short.code) == (False, "FLAME_API_ERROR")
    false_whole = judge_render(
        **kwargs, flame={"returned": False}, written=8, expected=8, checked=True
    )
    assert false_whole.ok is True and "returned False" in false_whole.degraded[0]
    errored = judge_render(
        env_ok=False,
        env_code="FLAME_API_ERROR",
        env_error="x",
        flame={},
        written=8,
        expected=8,
        checked=True,
    )
    assert (errored.ok, errored.code, errored.message) == (False, "FLAME_API_ERROR", "x")
    only_render = judge_render(**kwargs, flame=done, written=0, expected=0, checked=False)
    assert only_render.ok is True


def test_a_render_node_only_intent_with_a_handle_is_landed_by_the_answer(
    rig: Rig, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = PyRenderNode("render1", 1, 24)
    bg(rig).nodes.append(node)
    node._owner = bg(rig)
    source = next(n for n in bg(rig).nodes if n.name.get_value() == "blur1")
    bg(rig).connect_nodes(source, "Default", node, "Default")
    group = pending_render(rig, bridge, monkeypatch)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert entry["current"] == {"writtenFrames": 0, "expectedFrames": 0, "nodes": []}
    assert "Render nodes" in entry["detail"] and "nothing to look for on disk" in entry["detail"]


def test_a_render_node_only_intent_is_decided_from_the_reel_clip_counts(rig: Rig) -> None:
    node = PyRenderNode("render1", 1, 24)
    bg(rig).nodes.append(node)
    node._owner = bg(rig)
    source = next(n for n in bg(rig).nodes if n.name.get_value() == "blur1")
    bg(rig).connect_nodes(source, "Default", node, "Default")
    bg(rig).create_shelf_reel("Batch Renders")
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="before")
    group = render(rig)["journal"]["group"]
    (entry,) = rig.do("journal.check", {"group": group})["result"]["checked"]
    assert entry["verdict"] == "not_landed" and "no reel" in entry["detail"]
    bg(rig).shelf_reels[0]._children.append(PyClip("x", wiretap_id="/w/x"))  # a reel gained a clip
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="before")
    group2 = render(rig)["journal"]["group"]
    bg(rig).shelf_reels[0]._children.append(PyClip("y", wiretap_id="/w/y"))
    (entry2,) = rig.do("journal.check", {"group": group2})["result"]["checked"]
    assert entry2["verdict"] == "landed" and "more clips" in entry2["detail"]


# FAKE bridge throughout (journal_test_helpers.FakeBridge): these say what the SERVER does with a
# bridge that announces `client_handle`; what the real bridge does is in test_bridge_impl.py and
# what a real Flame does is scripts/p2_3c_render_e2e.py / p2_4_restart_e2e.py.

HEX32 = "0123456789abcdef"
CLIENT_HANDLE_FOR_VIEW = "0123456789abcdef0123456789abcdef"


def intent_handle(rig: Rig) -> str:
    (intent,) = intents(rig)
    handle = intent["handle"]
    assert isinstance(handle, str) and len(handle) == 32 and set(handle) <= set(HEX32)
    return handle


def render_call(rig: Rig) -> Any:
    return next(c for c in rig.exec.calls if c.label == "render.batch")


def test_the_intent_carries_a_client_handle_that_was_journaled_before_the_bridge_got_it(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=4)
    env = render(rig)
    assert env["ok"] is True, env
    handle = intent_handle(rig)
    (intent,) = intents(rig)
    assert intent["handleSource"] == "client"
    call = render_call(rig)
    assert call.handle == handle and "intent" in call.journal_types
    (view,) = Index(rig.journal.read_all().records).groups[env["journal"]["group"]]
    assert view.client_handle == handle


def test_a_run_answered_in_band_has_its_result_file_removed_by_one_poll(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=4)
    env = render(rig)
    handle = intent_handle(rig)
    assert rig.bridge.calls == [handle] and rig.bridge.results == {}
    assert env["ok"] is True and env["degraded"] == []


def test_a_failed_clean_up_poll_is_a_degraded_note_and_nothing_else(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=4)
    rig.bridge.failure = bridge_result(ok=False, error_code="BRIDGE_LOST", error="gone")
    env = render(rig)
    assert env["ok"] is True and env["result"]["writtenFrames"] == 4
    (note,) = env["degraded"]
    assert intent_handle(rig) in note and "BRIDGE_LOST" in note and "24 hours" in note
    assert states(rig)[env["journal"]["group"]] == "landed" and blocked_groups(rig) == []


def test_a_clean_up_poll_that_finds_no_such_handle_says_nothing(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=4)
    rig.bridge.failure = BridgeProtocolError("unknown handle: x")
    env = render(rig)
    assert env["ok"] is True and env["degraded"] == []
    assert len(rig.bridge.calls) == 1


def test_no_clean_up_poll_when_the_bridge_was_never_reached(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=4)
    rig.exec.fault("render.batch", "BRIDGE_AMBIGUOUS")
    env = render(rig)
    assert env["errorCode"] == "BRIDGE_AMBIGUOUS"
    assert rig.bridge.calls == []


def test_an_old_bridge_gets_no_handle_and_the_intent_says_so(rig: Rig, tmp_path: Path) -> None:
    old_bridge(rig)
    add_writer(rig, tmp_path, last=4)
    env = render(rig)
    assert env["ok"] is True
    (intent,) = intents(rig)
    assert intent["handle"] is None and intent["handleSource"] is None
    assert render_call(rig).handle is None
    assert rig.bridge.calls == []  # no clean-up poll either
    (view,) = Index(rig.journal.read_all().records).groups[env["journal"]["group"]]
    assert view.client_handle is None


def test_only_a_handle_marked_as_the_servers_own_counts_as_the_intents_client_handle() -> None:
    base = {"id": "i1", "group": "g1", "handle": CLIENT_HANDLE_FOR_VIEW}
    assert IntentView(intent={**base, "handleSource": "client"}).client_handle == base["handle"]
    assert IntentView(intent={**base, "handleSource": None}).client_handle is None
    assert IntentView(intent={**base, "handleSource": "bridge"}).client_handle is None
    assert IntentView(intent={"id": "i1", "group": "g1"}).client_handle is None
    assert IntentView(intent={**base, "handleSource": "client", "handle": ""}).client_handle is None


def test_bridge_lost_after_the_render_ran_is_settled_by_the_intents_handle(
    rig: Rig, tmp_path: Path
) -> None:
    """The render finished but no answer and no pending handle came back."""
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="after")
    env = render(rig, timeout_s=1)
    group = env["journal"]["group"]
    handle = intent_handle(rig)
    assert env["errorCode"] == "BRIDGE_LOST" and env.get("pending") is None
    assert handle in env["hint"] and group in blocked_groups(rig)
    assert record_types(rig) == ["intent", "unresolved"]  # no pending record: none was given
    plan_reads_before = rig.exec.labels().count("render.plan")
    entry = check(rig, group)
    assert rig.bridge.calls == [handle]
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert entry["renderOutcome"]["ok"] is True and entry["current"]["writtenFrames"] == 8
    assert rig.exec.labels().count("render.plan") == plan_reads_before  # Flame is not read
    polled = next(r for r in rig.records() if r["type"] == "polled")
    assert polled["handle"] == handle and polled["flame"]["returned"] is True
    assert blocked_groups(rig) == [] and states(rig)[group] == "landed"


def test_a_bare_timeout_is_settled_by_the_intents_handle_too(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "TIMEOUT", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert rig.bridge.calls == [intent_handle(rig)]


def test_a_render_that_raised_and_lost_its_answer_reads_failed_by_the_intents_handle(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=8)
    bg(rig).render_raises = "out of disk"
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "failed" and entry["basis"] == "handle"
    assert "out of disk" in entry["renderOutcome"]["error"]


def test_the_intents_handle_still_running_is_unknown_and_asks_again_later(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    handle = intent_handle(rig)
    held = rig.bridge.results.pop(handle)
    rig.bridge.running.add(handle)
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "still running" in entry["detail"]
    assert blocked_groups(rig) == [group]
    rig.bridge.running.discard(handle)
    rig.bridge.results[handle] = held
    assert check(rig, group)["verdict"] == "landed" and blocked_groups(rig) == []


def test_a_bridge_that_cannot_be_asked_leaves_the_intents_handle_unknown(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="after")
    group = render(rig, timeout_s=1)["journal"]["group"]
    rig.bridge.failure = bridge_result(ok=False, error_code="TIMEOUT", error="no reply")
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "TIMEOUT" in entry["detail"]
    assert "may not answer at all" in entry["detail"] and blocked_groups(rig) == [group]


def test_an_intents_handle_the_bridge_never_saw_falls_back_to_the_files_and_says_why(
    rig: Rig, tmp_path: Path
) -> None:
    """The call was lost before the bridge took it (or Flame restarted and its results are gone):
    no result under the handle, so the files decide -- an estimate, and the detail says the
    handle was the server's own."""
    add_writer(rig, tmp_path, last=8)
    rig.exec.fault("render.batch", "BRIDGE_LOST", when="before")
    group = render(rig, timeout_s=1)["journal"]["group"]
    handle = intent_handle(rig)
    entry = check(rig, group)
    assert rig.bridge.calls == [handle]
    assert entry["verdict"] == "not_landed" and entry["basis"] == "disk"
    assert "Flame was restarted before the render finished" in entry["detail"]
    assert handle in entry["detail"] and "estimate" in entry["detail"]


def test_a_pending_answer_with_the_servers_own_handle_is_journaled_and_used(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)
    env = pending_render(rig, bridge, monkeypatch, client=True)
    handle = intent_handle(rig)
    assert env["errorCode"] == "TIMEOUT" and env["pending"] == {"handle": handle}
    assert env["degraded"] == []
    assert record_types(rig) == ["intent", "unresolved", "pending"]
    assert rig.records()[-1]["handle"] == handle
    entry = check(rig, env["journal"]["group"])
    assert bridge.calls == [handle]
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"


def test_a_pending_handle_that_is_not_the_one_sent_wins_and_is_noted(
    rig: Rig, tmp_path: Path, bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_writer(rig, tmp_path, last=8)
    env = pending_render(rig, bridge, monkeypatch, client=True, ignores_client_handle=True)
    handle = intent_handle(rig)
    assert env["pending"] == {"handle": "h123"}
    (note,) = env["degraded"]
    assert "h123" in note and handle in note
    assert rig.records()[-1]["handle"] == "h123"
    entry = check(rig, env["journal"]["group"])
    assert bridge.calls == ["h123"]  # the bridge's own handle, not the intent's
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"


def test_a_render_group_cannot_be_reverted(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path, last=4)
    group = render(rig)["journal"]["group"]
    env = rig.do("journal.revert", {"group": group})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "cannot be undone" in env["error"] and "render" in env["error"]
    assert len(frames(tmp_path / "out_writefile1")) == 4


def test_plan_run_refuses_render_batch_with_the_reason_and_runs_nothing(
    rig: Rig, tmp_path: Path
) -> None:
    add_writer(rig, tmp_path)
    env = rig.do(
        "plan.run",
        {
            "steps": [
                {"op": "render.plan", "args": {"target": BG1}},
                {"op": "render.batch", "args": {"target": BG1}},
            ]
        },
    )
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    (issue,) = env["result"]["issues"]
    assert issue["index"] == 1 and issue["op"] == "render.batch"
    assert "occupies Flame" in issue["problems"][0]
    assert "cannot be undone" in issue["problems"][0]
    assert bg(rig).render_calls == [] and rig.exec.labels() == []


def test_plan_run_may_read_a_render_plan(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path)
    env = rig.do("plan.run", {"steps": [{"op": "render.plan", "args": {"target": BG1}}]})
    assert env["ok"] is True
    assert env["result"]["steps"][0]["result"]["summary"]["effectiveNodes"] == 1


def test_readonly_hides_render_batch_but_not_render_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from flame_mcp.policy import Policy

    rig = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    add_writer(rig, tmp_path)
    assert rig.do("render.plan", {"target": BG1})["ok"] is True
    env = render(rig)
    assert env["errorCode"] == "POLICY_DENIED" and bg(rig).render_calls == []


def test_the_declarations(rig: Rig) -> None:
    plan = rig.registry.get("render.plan")
    batch = rig.registry.get("render.batch")
    assert plan is not None and batch is not None
    assert (plan.effect, plan.undo, plan.category) == ("read", "none", "render")
    assert (batch.effect, batch.undo, batch.category) == ("create", "unsupported", "render")
    assert batch.thread == "main-async" and plan.thread == "main"
    assert batch.checkpoint is None and batch.journal is None and batch.wrap is not None
    assert batch.accepts == ("batch",) and plan.accepts == ("batch",)
    assert batch.undo_steps({}, 1) == 0
    text = batch.description
    for needle in (
        "Flame is occupied",
        "reads included",
        "this server has no op to stop a render",
        "render.batch provides no undo",
        "range_start / range_end (default 1-100)",
        "plan.run",
        "journal.check",
        "/var/tmp",
    ):
        assert needle in text, needle
    params = {p.name: p for p in batch.params}
    assert (params["timeout_s"].default, params["timeout_s"].minimum) == (120, 1)
    assert (params["max_frames"].default, params["max_frames"].minimum) == (1000, 1)
    assert params["allow_default_path"].default is False
    assert params["timeout_s"].integer and params["max_frames"].integer
    assert "the bridge may not answer MCP calls" in params["timeout_s"].description
    assert "reads included" in params["timeout_s"].description
    assert "this server has no op to stop it" in params["timeout_s"].description


def test_a_single_journal_group_per_render(rig: Rig, tmp_path: Path) -> None:
    add_writer(rig, tmp_path)
    render(rig)
    render(rig, allow_overwrite=True)
    groups = Index(rig.journal.read_all().records).groups
    assert len(groups) == 2 and all(len(v) == 1 for v in groups.values())
