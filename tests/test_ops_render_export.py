"""Tests for `render.export`: a clip or sequence written to files by `PyExporter.export`.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py). The fake `PyExporter` (fake_media_tree.FakeExport) writes one
`<name>.<frame:08d>.tif` per frame into the directory it is given (or a sub-folder of it), can
write nothing, fewer files or raise. What a real Flame writes -- the names, the sub-folders a
preset makes -- is not modelled here."""

from __future__ import annotations

import ast
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyClip, PyTrack, PyVersion, adopt, uid_of
from flame_mcp import export_check, export_run, render_files
from flame_mcp import server as server_mod
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from journal_test_helpers import Rig, bridge_result, make_rig
from timeline_p32a_helpers import CLIPX, S1, make_timeline_rig, media, wt

OP = "render.export"
HEX32 = "0123456789abcdef"
IMAGE_PRESET = "<preset version='14'><type>image</type></preset>"
MOVIE_PRESET = "<preset version='14'><type>movie</type></preset>"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


@pytest.fixture
def preset(tmp_path: Path) -> Path:
    path = tmp_path / "image.xml"
    path.write_text(IMAGE_PRESET, encoding="utf-8")
    return path


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


def clipx(rig: Rig) -> PyClip:
    clip: PyClip = rig.handles["clipx"]
    assert clip.duration.frame == 24
    return clip


def make_clip(rig: Rig, name: str, duration: int) -> PyClip:
    reel = rig.handles["reel"]
    clip = PyClip(
        name,
        wiretap_id=f"{reel.get_wiretap_node_id()}/{name}",
        duration=duration,
        versions=[PyVersion([PyTrack([media("clip1", 0, duration - 1)])])],
    )
    adopt(reel, [clip])
    reel._children.append(clip)
    return clip


def export(rig: Rig, preset: Path, out: Path, target: Any = CLIPX, **extra: Any) -> dict[str, Any]:
    args = {"target": target, "preset": str(preset), "output_dir": str(out), **extra}
    return rig.do(OP, args)


def intents(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "intent"]


def outcomes(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "outcome"]


def record_types(rig: Rig) -> list[str]:
    return [str(r["type"]) for r in rig.records()]


def states(rig: Rig) -> dict[str, str]:
    env = rig.do("journal.list", {})
    return {item["group"]: item["state"] for item in env["result"]["items"]}


def blocked_groups(rig: Rig) -> list[str]:
    return [b.group for b in rig.runtime.guard.blocked()]


def op_calls(rig: Rig, phase: str) -> list[Any]:
    marker = {"read": "_result = _ex_inspect(", "run": "_result = _ex_export("}[phase]
    return [c for c in rig.exec.calls if c.label == OP and marker in c.code]


def check(rig: Rig, group: str) -> dict[str, Any]:
    env = rig.do("journal.check", {"group": group})
    assert env["ok"] is True, env
    checked: list[dict[str, Any]] = env["result"]["checked"]
    return checked[0] if checked else {}


def intent_handle(rig: Rig) -> str:
    (intent,) = intents(rig)
    handle = intent["handle"]
    assert isinstance(handle, str) and len(handle) == 32 and set(handle) <= set(HEX32)
    return handle


def old_bridge(rig: Rig) -> None:
    rig.bridge.features = ()


def listing(out: Path) -> list[str]:
    return sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())


def refused_untouched(rig: Rig, env: dict[str, Any], code: str) -> None:
    assert env["ok"] is False and env["errorCode"] == code, env
    assert rig.records() == [] and rig.flame.export.calls == []
    assert op_calls(rig, "run") == []


# -- an export that works ----------------------------------------------------------------------


def test_an_image_sequence_is_exported_and_the_new_files_are_listed(
    rig: Rig, preset: Path, out: Path
) -> None:
    clip = clipx(rig)
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["ran"] is True and result["writtenCount"] == 24 and result["expectedFrames"] == 24
    names = [f"CLIPX.{n:08d}.tif" for n in range(24)]
    assert [f["name"] for f in result["files"]] == names
    assert all(f["size"] == 5 for f in result["files"]) and result["filesTruncated"] is False
    assert result["preset"] == str(preset) and result["outputDir"] == str(out)
    assert result["betweenMarks"] is False and "unsupported" in result["undo"]
    assert result["target"]["name"] == "CLIPX" and result["target"]["uid"] == uid_of(clip)
    assert result["target"]["selector"] == CLIPX and result["elapsedS"] is not None
    assert env["affected"] == 24 and env["undoSteps"] == 0 and env["degraded"] == []
    assert env["partial"] is None and env["journal"]["undo"] == "unsupported"
    assert env["journal"]["revert"] is None
    (call,) = rig.flame.export.calls
    assert call["sources"] is clip and call["preset"] == str(preset)
    assert call["output_dir"] == str(out) and call["foreground"] is True
    assert call["between_marks"] is False
    assert listing(out) == names


def test_a_sequence_is_exported_like_a_clip(rig: Rig, preset: Path, out: Path) -> None:
    env = export(rig, preset, out, S1)
    assert env["ok"] is True, env
    assert env["result"]["target"]["type"] == "PySequence"
    assert env["result"]["writtenCount"] == 64 and env["result"]["expectedFrames"] == 64


def test_files_a_preset_puts_into_a_sub_folder_are_counted(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.subdir = "CLIPX"
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 24
    assert env["result"]["files"][0]["name"] == "CLIPX/CLIPX.00000000.tif"
    assert outcomes(rig)[0]["verdict"] == "landed"


def test_a_movie_preset_has_no_expected_count_and_one_file_is_enough(
    rig: Rig, tmp_path: Path, out: Path
) -> None:
    movie = tmp_path / "movie.xml"
    movie.write_text(MOVIE_PRESET, encoding="utf-8")
    rig.flame.export.limit = 1
    env = export(rig, movie, out)
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 1 and env["result"]["expectedFrames"] is None
    assert env["partial"] is None and intents(rig)[0]["expectedFrames"] is None


def test_the_file_list_is_cut_at_a_cap(rig: Rig, preset: Path, out: Path) -> None:
    make_clip(rig, "BIG", 250)
    env = export(rig, preset, out, wt("/wt/RG/Reel 1/BIG"))
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 250
    assert len(env["result"]["files"]) == render_files.NAME_LIST_CAP
    assert env["result"]["filesTruncated"] is True


def test_between_marks_reaches_the_exporter_and_no_count_is_expected(
    rig: Rig, preset: Path, out: Path
) -> None:
    env = export(rig, preset, out, between_marks=True)
    assert env["ok"] is True, env
    (call,) = rig.flame.export.calls
    assert call["between_marks"] is True and call["foreground"] is True
    assert env["result"]["betweenMarks"] is True and env["result"]["writtenCount"] == 23
    assert env["result"]["expectedFrames"] is None
    assert intents(rig)[0]["betweenMarks"] is True


def test_the_flame_code_is_read_once_then_run_once_without_loops(
    rig: Rig, preset: Path, out: Path
) -> None:
    export(rig, preset, out)
    (read,) = op_calls(rig, "read")
    (run,) = op_calls(rig, "run")
    assert read.journal_types == [] and "intent" in run.journal_types
    for call in (read, run):
        ast.parse(call.code)
        assert call.code.isascii()
    own = run.code.split("__op_src = ", 1)[1].split("\n", 1)[0]
    tree = ast.parse(ast.literal_eval(own))
    banned = (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    assert not [n for n in ast.walk(tree) if isinstance(n, banned)]


def test_a_path_selector_is_turned_into_the_wiretap_selector_for_the_run(
    rig: Rig, preset: Path, out: Path
) -> None:
    path = {"kind": "path", "root": "desktop", "path": ["RG", "Reel 1", "CLIPX"]}
    env = export(rig, preset, out, path)
    assert env["ok"] is True, env
    assert intents(rig)[0]["target"] == CLIPX
    assert env["result"]["target"]["selector"] == CLIPX


# -- the journal ------------------------------------------------------------------------------


def test_the_intent_is_flushed_before_the_export_and_the_outcome_lands(
    rig: Rig, preset: Path, out: Path
) -> None:
    (out / ".DS_Store").write_text("abc", encoding="utf-8")
    env = export(rig, preset, out, timeout_s=60)
    (intent,) = intents(rig)
    assert intent["op"] == OP and intent["kind"] == "export" and intent["target"] == CLIPX
    assert intent["preset"] == str(preset) and intent["outputDir"] == str(out)
    assert intent["expectedFrames"] == 24 and intent["timeoutS"] == 60
    assert intent["name"] == "CLIPX" and intent["uid"] == uid_of(clipx(rig))
    assert list(intent["directoryBefore"]) == [".DS_Store"]
    assert intent["directoryBefore"][".DS_Store"][0] == 3
    assert intent["handleSource"] == "client" and intent["handle"] == intent_handle(rig)
    (run,) = op_calls(rig, "run")
    assert run.handle == intent["handle"] and run.timeout_s == 60
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is True and outcome["verdict"] == "landed"
    assert outcome["writtenCount"] == 24 and outcome["expectedFrames"] == 24
    assert states(rig)[env["journal"]["group"]] == "landed"
    assert record_types(rig) == ["intent", "outcome"]


def test_a_run_answered_in_band_has_its_result_file_removed_by_one_poll(
    rig: Rig, preset: Path, out: Path
) -> None:
    env = export(rig, preset, out)
    assert rig.bridge.calls == [intent_handle(rig)] and rig.bridge.results == {}
    assert env["ok"] is True and env["degraded"] == []


def test_an_old_bridge_gets_no_handle_and_the_intent_says_so(
    rig: Rig, preset: Path, out: Path
) -> None:
    old_bridge(rig)
    assert export(rig, preset, out)["ok"] is True
    (intent,) = intents(rig)
    assert intent["handle"] is None and intent["handleSource"] is None
    assert rig.bridge.calls == []


# -- judging ----------------------------------------------------------------------------------


def test_an_export_that_writes_nothing_is_silent_noop(rig: Rig, preset: Path, out: Path) -> None:
    rig.flame.export.writes = False
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["writtenCount"] == 0 and env["result"]["files"] == []
    assert env["affected"] == 0 and env["journal"]["undo"] == "unsupported"
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is False and outcome["errorCode"] == "SILENT_NOOP"
    assert outcome["verdict"] == "not_landed"
    assert states(rig)[env["journal"]["group"]] == "failed" and blocked_groups(rig) == []


def test_an_export_that_returns_false_and_writes_nothing_is_a_flame_api_error(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.writes = False
    rig.flame.export.returns = False
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["returned"] is False


def test_an_export_that_raises_is_a_flame_api_error(rig: Rig, preset: Path, out: Path) -> None:
    rig.flame.export.raises = "disk full"
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "disk full" in env["error"] and env["result"]["ran"] is True
    assert env["result"]["writtenCount"] == 0
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is False and outcome["errorCode"] == "FLAME_API_ERROR"
    assert blocked_groups(rig) == []


def test_fewer_files_than_frames_is_a_partial_and_the_files_stay(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.limit = 10
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["error"].startswith("partial:") and "10 of 24" in env["error"]
    assert env["result"]["writtenCount"] == 10 and len(listing(out)) == 10
    assert env["partial"] == {"written": 10, "expected": 24} and env["affected"] == 10
    (outcome,) = outcomes(rig)
    assert outcome["verdict"] == "partial" and outcome["writtenCount"] == 10
    assert states(rig)[env["journal"]["group"]] == "partial"


def test_a_false_return_with_all_the_files_there_is_ok_with_a_note(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.returns = False
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    (note,) = env["degraded"]
    assert "False" in note and "on disk" in note


def test_a_clip_that_is_not_the_one_read_is_a_conflict_and_nothing_is_exported(
    rig: Rig, preset: Path, out: Path
) -> None:
    def swap(label: str | None) -> None:
        if label == OP and "_result = _ex_export(" in rig.exec.calls[-1].code:
            clipx(rig).uid.set_value("uid-other")

    rig.exec.before_run = swap
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert rig.flame.export.calls == [] and listing(out) == []
    assert outcomes(rig)[0]["errorCode"] == "CONFLICT" and blocked_groups(rig) == []


# -- refusals before Flame is touched ---------------------------------------------------------


def test_arguments_that_cannot_work_are_refused_before_anything_is_sent(
    rig: Rig, preset: Path, tmp_path: Path, out: Path
) -> None:
    text = tmp_path / "preset.txt"
    text.write_text(IMAGE_PRESET, encoding="utf-8")
    cases: list[tuple[dict[str, Any], str]] = [
        ({"preset": "presets/image.xml"}, "INVALID_ARGS"),
        ({"preset": str(text)}, "INVALID_ARGS"),
        ({"output_dir": "out"}, "INVALID_ARGS"),
        ({"preset": str(tmp_path / "missing.xml")}, "PRECONDITION_UNMET"),
        ({"output_dir": str(tmp_path / "missing")}, "PRECONDITION_UNMET"),
        ({"output_dir": str(preset)}, "PRECONDITION_UNMET"),
        ({"timeout_s": 3601}, "INVALID_ARGS"),
        ({"timeout_s": 0}, "INVALID_ARGS"),
        ({"between_marks": "yes"}, "INVALID_ARGS"),
    ]
    base = {"target": CLIPX, "preset": str(preset), "output_dir": str(out)}
    for extra, code in cases:
        refused_untouched(rig, rig.do(OP, {**base, **extra}), code)
    assert rig.exec.calls == []


def test_a_non_ascii_path_is_refused(rig: Rig, preset: Path, tmp_path: Path) -> None:
    odd = tmp_path / "café"
    odd.mkdir()
    refused_untouched(rig, export(rig, preset, odd), "INVALID_ARGS")


def test_a_shared_temp_directory_is_refused_unless_allowed(
    rig: Rig, preset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(render_files, "DEFAULT_OUTPUT_DIRS", (str(shared),))
    env = export(rig, preset, shared)
    refused_untouched(rig, env, "PRECONDITION_UNMET")
    assert "allow_default_path" in env["hint"]
    allowed = export(rig, preset, shared, allow_default_path=True)
    assert allowed["ok"] is True, allowed
    assert allowed["result"]["writtenCount"] == 24


def test_a_target_that_is_not_a_clip_or_sequence_is_selector_type(
    rig: Rig, preset: Path, out: Path
) -> None:
    env = export(rig, preset, out, wt(rig.handles["reel"].get_wiretap_node_id()))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE", env
    assert "PyReel" in env["error"] and rig.records() == []
    assert rig.flame.export.calls == []


def test_a_target_nothing_matches_is_selector_empty(rig: Rig, preset: Path, out: Path) -> None:
    env = export(rig, preset, out, wt("/wt/RG/Reel 1/NOWHERE"))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert rig.records() == []


def test_a_selector_kind_the_op_does_not_take_is_invalid_args(
    rig: Rig, preset: Path, out: Path
) -> None:
    env = export(rig, preset, out, {"kind": "batch", "name": "BG1"})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env


# -- only an empty directory is written to ---------------------------------------------------


def assert_existing_refusal(rig: Rig, env: dict[str, Any], count: int) -> None:
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "not empty" in env["error"]
    assert env["result"]["ran"] is False and env["result"]["existing"]["count"] == count
    assert rig.records() == [] and rig.flame.export.calls == []
    assert op_calls(rig, "run") == []


def test_any_file_in_the_directory_stops_the_export_whatever_its_name(
    rig: Rig, preset: Path, out: Path
) -> None:
    (out / "unrelated.tif").write_bytes(b"old")
    env = export(rig, preset, out)
    assert_existing_refusal(rig, env, 1)
    assert env["result"]["existing"]["first"] == ["unrelated.tif"]
    assert "new" in env["hint"] and "empty" in env["hint"]
    assert (out / "unrelated.tif").read_bytes() == b"old"


def test_a_file_in_a_sub_folder_and_an_empty_folder_stop_it(
    rig: Rig, preset: Path, out: Path
) -> None:
    (out / "a").mkdir()
    (out / "a" / "x.mov").write_bytes(b"old")
    (out / "empty_folder").mkdir()
    env = export(rig, preset, out)
    assert_existing_refusal(rig, env, 3)
    assert env["result"]["existing"]["first"] == ["a/", "a/x.mov", "empty_folder/"]


def test_existing_shows_the_count_and_only_the_first_ten(rig: Rig, preset: Path, out: Path) -> None:
    for n in range(12):
        (out / f"f.{n:04d}.tif").write_bytes(b"old")
    env = export(rig, preset, out)
    assert_existing_refusal(rig, env, 12)
    assert env["result"]["existing"]["first"] == [f"f.{n:04d}.tif" for n in range(10)]


def test_hidden_entries_alone_count_as_empty(rig: Rig, preset: Path, out: Path) -> None:
    (out / ".DS_Store").write_bytes(b"mac")
    (out / ".cache").mkdir()
    (out / ".cache" / "index").write_bytes(b"x")
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 24
    assert not [f for f in env["result"]["files"] if f["name"].startswith(".")]
    assert sorted(intents(rig)[0]["directoryBefore"]) == [
        ".DS_Store"
    ]  # a hidden folder is not entered


def test_a_visible_file_next_to_hidden_ones_still_stops_it(
    rig: Rig, preset: Path, out: Path
) -> None:
    (out / ".DS_Store").write_bytes(b"mac")
    (out / "shot.mov").write_bytes(b"old")
    env = export(rig, preset, out)
    assert_existing_refusal(rig, env, 1)
    assert env["result"]["existing"]["first"] == ["shot.mov"]


def test_the_scan_goes_down_to_the_depth_limit_and_no_further(out: Path) -> None:
    levels = render_files.EXPORT_SCAN_DEPTH
    deep = out.joinpath(*[f"d{n}" for n in range(levels)])
    deep.mkdir(parents=True)
    (deep / "last_level.mov").write_bytes(b"x")  # one level deeper than the scan reaches
    inside = out.joinpath(*[f"d{n}" for n in range(levels - 1)])
    (inside / "reached.mov").write_bytes(b"x")
    tree = render_files.scan_tree(str(out))
    assert list(tree.files) == ["/".join([*[f"d{n}" for n in range(levels - 1)], "reached.mov"])]
    assert len(tree.dirs) == levels and tree.truncated is False


def test_a_directory_with_too_many_entries_is_refused(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_run, "EXPORT_SCAN_LIMIT", 3)
    for n in range(5):
        (out / f"f{n}.txt").write_bytes(b"x")
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "too many entries" in env["error"] and rig.records() == []
    assert rig.flame.export.calls == []


# -- an answer that never came ----------------------------------------------------------------


def test_a_timeout_closes_the_write_door_and_leaves_the_intent_open(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)  # the export ran, the answer is lost
    env = export(rig, preset, out, timeout_s=1)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    group = env["journal"]["group"]
    handle = intent_handle(rig)
    assert "journal.check" in env["hint"] and handle in env["hint"]
    assert "may not answer at all" in env["hint"]
    assert "export" in env["hint"] and "render" not in env["hint"].lower()
    assert group in blocked_groups(rig) and env["journal"]["undo"] == "unsupported"
    assert outcomes(rig) == [] and record_types(rig) == ["intent", "unresolved"]
    again = export(rig, preset, out)
    assert again["errorCode"] == "POLICY_DENIED" and len(rig.flame.export.calls) == 1


def test_check_asks_the_bridge_for_the_intents_handle_and_counts_the_files(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    handle = intent_handle(rig)
    reads_before = len(op_calls(rig, "read"))
    entry = check(rig, group)
    assert rig.bridge.calls == [handle]
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert entry["exportOutcome"]["ok"] is True and entry["current"]["writtenCount"] == 24
    assert len(op_calls(rig, "read")) == reads_before  # Flame is not read
    assert blocked_groups(rig) == [] and states(rig)[group] == "landed"
    types = record_types(rig)
    assert types.index("polled") < types.index("check")
    checked = next(r for r in rig.records() if r["type"] == "check")
    assert checked["basis"] == "handle" and checked["verdict"] == "landed"
    assert check(rig, group) == {}


def test_check_by_the_handle_reads_fewer_files_than_frames_as_partial(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.limit = 7
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "partial" and entry["basis"] == "handle"
    assert entry["current"]["writtenCount"] == 7 and entry["current"]["expectedFrames"] == 24


def test_check_by_the_handle_reads_a_silent_noop_as_not_landed(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.writes = False
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "not_landed" and entry["basis"] == "handle"
    assert blocked_groups(rig) == []


def test_check_by_the_handle_reads_an_export_that_raised_as_failed(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.raises = "disk full"
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "failed" and "disk full" in entry["exportOutcome"]["error"]


def test_an_export_still_running_is_unknown_and_the_door_stays_shut(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    handle = intent_handle(rig)
    held = rig.bridge.results.pop(handle)
    rig.bridge.running.add(handle)
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "the export is still running" in entry["detail"]
    assert "render" not in entry["detail"].lower() and blocked_groups(rig) == [group]
    rig.bridge.running.discard(handle)
    rig.bridge.results[handle] = held
    assert check(rig, group)["verdict"] == "landed" and blocked_groups(rig) == []


def test_a_pending_handle_of_the_bridge_is_journaled_and_used(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flame_mcp import transport
    from flame_mcp.transport import BridgeResult

    scripted = rig.exec
    rig.bridge.features = ()

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
        if label != OP or "_result = _ex_export(" not in code:
            return real
        rig.bridge.results["h123"] = real
        return bridge_result(pending_handle="h123")

    monkeypatch.setattr(transport, "exec", pending)
    env = export(rig, preset, out, timeout_s=1)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert env["pending"] == {"handle": "h123"}
    assert "export was still running" in env["error"] and "render" not in env["error"]
    assert record_types(rig) == ["intent", "unresolved", "pending"]
    (view,) = Index(rig.journal.read_all().records).groups[env["journal"]["group"]]
    assert view.pending_handle == "h123"
    entry = check(rig, env["journal"]["group"])
    assert rig.bridge.calls == ["h123"]
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"


def test_check_without_a_handle_counts_the_files_and_says_it_is_an_estimate(
    rig: Rig, preset: Path, out: Path
) -> None:
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    assert record_types(rig) == ["intent", "unresolved"] and rig.bridge.calls == []
    reads_before = len(op_calls(rig, "read"))
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "disk"
    assert "estimate" in entry["detail"] and entry["current"]["writtenCount"] == 24
    assert len(op_calls(rig, "read")) == reads_before
    assert blocked_groups(rig) == [] and states(rig)[group] == "landed"
    checked = next(r for r in rig.records() if r["type"] == "check")
    assert checked["basis"] == "disk" and checked["verdict"] == "landed"


def test_check_without_a_handle_reads_some_files_as_partial_and_none_as_not_landed(
    rig: Rig, preset: Path, out: Path
) -> None:
    old_bridge(rig)
    rig.flame.export.limit = 5
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    assert check(rig, group)["verdict"] == "partial"
    rig.exec.fault(OP, "TIMEOUT", when="before", skip=1)  # the export never started
    other = tmp_out = out / "second"
    tmp_out.mkdir()
    group2 = export(rig, preset, other, wt("/wt/RG/Reel 1/CLIPX"), timeout_s=1)
    assert group2["errorCode"] == "TIMEOUT"
    entry = check(rig, group2["journal"]["group"])
    assert entry["verdict"] == "not_landed" and entry["basis"] == "disk"


def test_check_without_a_handle_and_without_a_count_takes_any_file_as_landed(
    rig: Rig, tmp_path: Path, out: Path
) -> None:
    movie = tmp_path / "movie.xml"
    movie.write_text(MOVIE_PRESET, encoding="utf-8")
    old_bridge(rig)
    rig.flame.export.limit = 1
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, movie, out, timeout_s=1)["journal"]["group"]
    assert check(rig, group)["verdict"] == "landed"


def test_check_without_a_handle_finds_a_directory_that_is_gone_diverged(
    rig: Rig, preset: Path, tmp_path: Path
) -> None:
    old_bridge(rig)
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, preset, doomed, timeout_s=1)["journal"]["group"]
    for path in doomed.iterdir():
        path.unlink()
    doomed.rmdir()
    entry = check(rig, group)
    assert entry["verdict"] == "diverged" and entry["basis"] == "disk"
    assert blocked_groups(rig) == []


def test_an_intents_handle_the_bridge_never_saw_falls_back_to_the_disk_and_says_why(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    handle = intent_handle(rig)
    entry = check(rig, group)
    assert rig.bridge.calls == [handle]
    assert entry["verdict"] == "not_landed" and entry["basis"] == "disk"
    assert handle in entry["detail"] and "before the export finished" in entry["detail"]
    assert "render" not in entry["detail"].lower()


def test_the_disk_check_ignores_files_that_were_there_before(
    rig: Rig, preset: Path, out: Path
) -> None:
    old_bridge(rig)
    (out / ".old").write_text("x", encoding="utf-8")
    rig.exec.fault(OP, "TIMEOUT", when="before", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "not_landed" and entry["current"]["writtenCount"] == 0


# -- a directory that cannot be read or written ----------------------------------------------


@pytest.fixture
def restore_modes() -> Iterator[list[Path]]:
    paths: list[Path] = []
    yield paths
    for path in paths:
        path.chmod(stat.S_IRWXU)


needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads and writes whatever the mode says"
)


@needs_permissions
def test_a_directory_that_cannot_be_read_is_not_taken_for_empty(
    rig: Rig, preset: Path, out: Path, restore_modes: list[Path]
) -> None:
    restore_modes.append(out)
    out.chmod(0o300)  # writable, not readable: the listing fails
    assert render_files.scan_tree(str(out)).unreadable == ("",)
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "could not be read" in env["error"] and env["result"]["ran"] is False
    assert rig.records() == [] and rig.flame.export.calls == [] and op_calls(rig, "run") == []


@needs_permissions
def test_a_directory_that_cannot_be_written_is_refused_before_anything_is_sent(
    rig: Rig, preset: Path, out: Path, restore_modes: list[Path]
) -> None:
    restore_modes.append(out)
    for mode in (0o500, 0o000):
        out.chmod(mode)
        env = export(rig, preset, out)
        refused_untouched(rig, env, "PRECONDITION_UNMET")
        assert "not writable" in env["error"]
    assert rig.exec.calls == []


@needs_permissions
def test_a_folder_that_cannot_be_read_stops_the_count_and_the_empty_check(
    out: Path, restore_modes: list[Path]
) -> None:
    inner = out / "inner"
    inner.mkdir()
    (inner / "f.tif").write_bytes(b"x")
    restore_modes.append(inner)
    inner.chmod(0o000)
    tree = render_files.scan_tree(str(out))
    assert tree.unreadable == ("inner",) and tree.dirs == ("inner",)
    assert render_files.visible_entries(tree) == ["inner/"]


# -- entries that are neither files nor folders ----------------------------------------------


def make_other(kind: str, where: Path, elsewhere: Path) -> None:
    if kind == "symlink_to_dir":
        elsewhere.mkdir()
        os.symlink(elsewhere, where / "link")
    elif kind == "symlink_to_file":
        elsewhere.write_bytes(b"x")
        os.symlink(elsewhere, where / "link")
    elif kind == "dangling_symlink":
        os.symlink(elsewhere / "nowhere", where / "link")
    else:
        os.mkfifo(where / "link")


@pytest.mark.parametrize("kind", ["symlink_to_dir", "symlink_to_file", "dangling_symlink", "fifo"])
def test_a_link_or_a_pipe_is_seen_and_stops_the_export(
    rig: Rig, preset: Path, out: Path, tmp_path: Path, kind: str
) -> None:
    make_other(kind, out, tmp_path / "elsewhere")
    tree = render_files.scan_tree(str(out))
    assert tree.others == ("link",) and tree.files == {} and tree.dirs == ()
    env = export(rig, preset, out)
    assert_existing_refusal(rig, env, 1)
    assert env["result"]["existing"]["first"] == ["link"]


def test_a_hidden_link_alone_counts_as_empty(
    rig: Rig, preset: Path, out: Path, tmp_path: Path
) -> None:
    os.symlink(tmp_path / "nowhere", out / ".link")
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 24


# -- counting what was written ----------------------------------------------------------------


def test_a_preset_that_writes_five_levels_down_is_counted(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.subdir = "CLIPX/tape/1920x1080/a"
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 24 and env["result"]["countComplete"] is True
    assert env["result"]["files"][0]["name"] == "CLIPX/tape/1920x1080/a/CLIPX.00000000.tif"
    assert env["degraded"] == [] and outcomes(rig)[0]["verdict"] == "landed"


def test_hidden_files_an_export_writes_are_not_counted(rig: Rig, preset: Path, out: Path) -> None:
    rig.flame.export.hidden = [".export_lock"]
    rig.flame.export.limit = 23
    env = export(rig, preset, out)
    assert env["ok"] is False and env["error"].startswith("partial:"), env
    assert env["result"]["writtenCount"] == 23
    assert not [f for f in env["result"]["files"] if f["name"].startswith(".")]
    assert (out / ".export_lock").is_file()


def test_folders_without_files_are_a_silent_noop(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_run, "EXPORT_COUNT_DEPTH", 2)
    rig.flame.export.subdir = "a/b"  # the files are on level 3, below what is counted
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["writtenCount"] == 0 and env["result"]["newFolders"] == ["a/", "a/b/"]
    assert env["result"]["countComplete"] is True and env["degraded"] == []
    assert outcomes(rig)[0]["verdict"] == "not_landed"


def test_folders_without_files_and_a_false_return_are_a_flame_api_error(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.subdir = "only_a_folder"
    rig.flame.export.limit = 0  # the folder is made, no file is written
    rig.flame.export.returns = False
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["newFolders"] == ["only_a_folder/"]


def test_sixteen_levels_are_counted_with_the_real_constants(
    rig: Rig, preset: Path, out: Path
) -> None:
    levels = render_files.EXPORT_COUNT_DEPTH
    rig.flame.export.subdir = "/".join(f"d{n}" for n in range(levels - 1))  # the files: level 16
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 24 and env["result"]["countComplete"] is True
    assert env["result"]["files"][0]["name"].count("/") == levels - 1


def test_files_on_level_seventeen_are_not_counted_with_the_real_constants(
    rig: Rig, preset: Path, out: Path
) -> None:
    levels = render_files.EXPORT_COUNT_DEPTH
    rig.flame.export.subdir = "/".join(f"d{n}" for n in range(levels))  # the files: level 17
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["writtenCount"] == 0 and env["result"]["newFolders"][0] == "d0/"


def test_a_count_cut_short_by_its_limit_is_not_called_partial(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_run, "EXPORT_COUNT_LIMIT", 10)
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert env["result"]["countComplete"] is False and env["result"]["writtenCount"] == 10
    assert env["partial"] is None
    (note,) = env["degraded"]
    assert "could not be counted" in note
    assert outcomes(rig)[0]["verdict"] is None


def test_hidden_entries_do_not_count_toward_the_limit(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_run, "EXPORT_COUNT_LIMIT", 30)
    rig.flame.export.hidden = [f".h{n}" for n in range(10)]  # 24 + 10 entries, 24 count
    env = export(rig, preset, out)
    assert env["ok"] is True and env["result"]["writtenCount"] == 24, env
    assert env["result"]["countComplete"] is True and env["degraded"] == []
    assert outcomes(rig)[0]["verdict"] == "landed"


def test_nothing_written_and_a_count_cut_short_is_not_a_silent_noop(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_run, "EXPORT_COUNT_LIMIT", 1)
    rig.flame.export.limit = 0
    rig.flame.export.subdir = "a/b"  # two folders, no file: the second entry is past the limit
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 0 and env["result"]["countComplete"] is False
    assert outcomes(rig)[0]["verdict"] is None


def test_a_cut_short_count_does_not_hide_a_false_return(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_run, "EXPORT_COUNT_LIMIT", 1)
    rig.flame.export.limit = 0
    rig.flame.export.subdir = "a/b"
    rig.flame.export.returns = False
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert env["result"]["countComplete"] is False


def test_a_cut_short_count_with_all_the_files_and_a_false_return_is_ok_with_the_note(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_run, "EXPORT_COUNT_LIMIT", 30)
    rig.flame.export.returns = False
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert [d for d in env["degraded"] if "False" in d]


# -- a folder the server cannot read ---------------------------------------------------------


def test_only_a_folder_that_is_not_hidden_makes_a_listing_incomplete() -> None:
    def tree(*unreadable: str) -> render_files.Tree:
        return render_files.Tree({}, (), (), unreadable, False)

    assert tree(".Trashes", ".Spotlight-V100").incomplete is False
    assert tree(".Trashes", "inner").incomplete is True
    assert tree("").incomplete is True
    assert render_files.Tree({}, (), (), (), True).incomplete is True


@needs_permissions
def test_a_hidden_folder_that_cannot_be_read_does_not_turn_a_failure_into_ok(
    rig: Rig, preset: Path, out: Path, restore_modes: list[Path]
) -> None:
    locked = out / ".Trashes"
    locked.mkdir()
    restore_modes.append(locked)
    locked.chmod(0)
    tree = render_files.scan_tree(str(out))
    assert tree.incomplete is False and render_files.visible_entries(tree) == []
    rig.flame.export.writes = False
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["countComplete"] is True


@needs_permissions
def test_a_hidden_folder_that_cannot_be_read_does_not_hide_a_short_count(
    rig: Rig, preset: Path, out: Path, restore_modes: list[Path]
) -> None:
    locked = out / ".fseventsd"
    locked.mkdir()
    restore_modes.append(locked)
    locked.chmod(0)
    rig.flame.export.limit = 10
    env = export(rig, preset, out)
    assert env["ok"] is False and env["error"].startswith("partial:"), env


@needs_permissions
def test_a_visible_folder_that_cannot_be_read_makes_the_count_unsure(
    rig: Rig, preset: Path, out: Path, restore_modes: list[Path]
) -> None:
    restore_modes.append(out / "locked")
    rig.flame.export.writes = True
    rig.flame.export.limit = 0
    rig.flame.export.locked_dirs = ["locked"]
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    assert env["result"]["countComplete"] is False and env["result"]["writtenCount"] == 0
    (note,) = env["degraded"]
    assert "cannot be read" in note and outcomes(rig)[0]["verdict"] is None


def test_the_intent_keeps_hidden_names_up_to_a_cap_and_counts_them_all(
    rig: Rig, preset: Path, out: Path
) -> None:
    total = render_files.NAME_LIST_CAP + 5
    for n in range(total):
        (out / f".h{n:04d}").write_bytes(b"x")
    env = export(rig, preset, out)
    assert env["ok"] is True, env
    (intent,) = intents(rig)
    assert len(intent["directoryBefore"]) == render_files.NAME_LIST_CAP
    assert intent["hiddenBefore"] == total


# -- the preset: what it says about the files -------------------------------------------------


def preset_file(tmp_path: Path, text: str, name: str = "p.xml") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_preset_type_reads_the_type_element(tmp_path: Path) -> None:
    assert export_run.preset_type(preset_file(tmp_path, IMAGE_PRESET)) == "image"
    assert export_run.preset_type(preset_file(tmp_path, MOVIE_PRESET, "m.XML")) == "movie"


def test_preset_type_is_none_for_a_file_that_says_nothing(tmp_path: Path) -> None:
    assert export_run.preset_type(preset_file(tmp_path, "this is not xml")) is None
    assert (
        export_run.preset_type(preset_file(tmp_path, "<preset><comment>x</comment></preset>"))
        is None
    )
    assert export_run.preset_type(preset_file(tmp_path, "<preset><type> </type></preset>")) is None
    assert export_run.preset_type(str(tmp_path / "missing.xml")) is None
    big = "<preset><type>image</type><!--" + "x" * export_run.PRESET_MAX_BYTES + "--></preset>"
    assert export_run.preset_type(preset_file(tmp_path, big, "big.xml")) is None


def test_expected_frames_is_the_duration_for_an_image_sequence_only(tmp_path: Path) -> None:
    image = preset_file(tmp_path, IMAGE_PRESET)
    movie = preset_file(tmp_path, MOVIE_PRESET, "m.xml")
    assert export_run.expected_frames(image, 24, False) == 24
    assert export_run.expected_frames(image, 24, True) is None
    assert export_run.expected_frames(image, 0, False) is None
    assert export_run.expected_frames(image, None, False) is None
    assert export_run.expected_frames(movie, 24, False) is None
    assert export_run.expected_frames(preset_file(tmp_path, "<oops", "x.xml"), 24, False) is None


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ("<posterFrame>True</posterFrame>", None),
        ("<posterFrame>true</posterFrame>", None),
        ("<useFrameAsPoster>1</useFrameAsPoster>", None),
        (
            "<video><posterFrame>False</posterFrame><useFrameAsPoster>0</useFrameAsPoster></video>",
            24,
        ),
        ("<video><posterFrame>False</posterFrame></video>", 24),
    ],
)
def test_a_poster_frame_preset_has_no_expected_count(
    tmp_path: Path, tags: str, expected: int | None
) -> None:
    text = f"<preset><type>image</type>{tags}</preset>"
    assert export_run.expected_frames(preset_file(tmp_path, text), 24, False) == expected


def test_a_poster_frame_export_writing_one_file_is_ok(rig: Rig, tmp_path: Path, out: Path) -> None:
    poster = preset_file(
        tmp_path,
        "<preset><type>image</type><video><posterFrame>True</posterFrame></video></preset>",
    )
    rig.flame.export.limit = 1
    env = export(rig, Path(poster), out)
    assert env["ok"] is True, env
    assert env["result"]["writtenCount"] == 1 and env["result"]["expectedFrames"] is None


def test_an_upper_case_extension_is_accepted(rig: Rig, tmp_path: Path, out: Path) -> None:
    env = export(rig, Path(preset_file(tmp_path, IMAGE_PRESET, "image.XML")), out)
    assert env["ok"] is True and env["result"]["expectedFrames"] == 24, env


# -- the directory changes between the check and the export ----------------------------------


def test_a_file_that_appears_before_the_export_starts_is_a_conflict_and_nothing_is_exported(
    rig: Rig, preset: Path, out: Path
) -> None:
    def appear(label: str | None) -> None:
        if label == OP and "_result = _ex_export(" in rig.exec.calls[-1].code:
            (out / "someone_elses.mov").write_bytes(b"theirs")

    rig.exec.before_run = appear
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert "not empty" in env["error"] and "nothing was exported" in env["error"]
    assert rig.flame.export.calls == []
    assert env["result"]["writtenCount"] == 0 and env["result"]["files"] == []
    assert env["affected"] == 0 and listing(out) == ["someone_elses.mov"]
    (outcome,) = outcomes(rig)
    assert outcome["errorCode"] == "CONFLICT" and outcome["writtenCount"] == 0
    assert blocked_groups(rig) == []


def test_a_hidden_file_that_appears_before_the_export_does_not_stop_it(
    rig: Rig, preset: Path, out: Path
) -> None:
    def appear(label: str | None) -> None:
        if label == OP and "_result = _ex_export(" in rig.exec.calls[-1].code:
            (out / ".DS_Store").write_bytes(b"mac")

    rig.exec.before_run = appear
    assert export(rig, preset, out)["ok"] is True


def test_the_recheck_is_in_the_prelude_and_the_generated_code_has_no_loop(
    rig: Rig, preset: Path, out: Path
) -> None:
    export(rig, preset, out)
    (run,) = op_calls(rig, "run")
    assert "def _ex_dir_is_empty(" in run.code and run.code.isascii()


# -- the journal cannot be written, a write ends without an answer ----------------------------


def test_the_journal_being_unwritable_refuses_the_export(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_record: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.journal, "append", broken)
    env = export(rig, preset, out)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert "journal cannot be written" in env["error"] and env["result"] == {"ran": False}
    assert rig.flame.export.calls == [] and op_calls(rig, "run") == [] and listing(out) == []


def test_an_outcome_that_cannot_be_written_closes_the_door_but_keeps_the_answer(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_append = rig.journal.append

    def failing_outcome(record: dict[str, Any]) -> None:
        if record["type"] == "outcome":
            raise OSError("disk full")
        real_append(record)

    monkeypatch.setattr(rig.journal, "append", failing_outcome)
    env = export(rig, preset, out)
    assert env["ok"] is True and env["result"]["writtenCount"] == 24, env
    assert any("outcome could not be journaled" in d for d in env["degraded"])
    assert rig.runtime.guard.denial() is not None
    assert [b.reason for b in rig.runtime.guard.blocked()] == ["JOURNAL_OUTCOME_NOT_WRITTEN"]


def test_a_bridge_that_became_unavailable_leaves_the_intent_open_like_a_timeout(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.exec.fault(OP, "BRIDGE_UNAVAILABLE", when="after", skip=1)
    env = export(rig, preset, out, timeout_s=1)
    assert env["ok"] is False and env["errorCode"] == "BRIDGE_UNAVAILABLE", env
    group = env["journal"]["group"]
    assert group in blocked_groups(rig) and outcomes(rig) == []
    assert record_types(rig) == ["intent", "unresolved"]
    assert "journal.check" in env["hint"] and "render" not in env["hint"].lower()
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert blocked_groups(rig) == []


# -- journal.check: more cases ----------------------------------------------------------------


def test_check_by_the_handle_finds_a_directory_that_is_gone_diverged(
    rig: Rig, preset: Path, tmp_path: Path
) -> None:
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, doomed, timeout_s=1)["journal"]["group"]
    for path in doomed.iterdir():
        path.unlink()
    doomed.rmdir()
    entry = check(rig, group)
    assert entry["verdict"] == "diverged" and entry["basis"] == "handle"
    assert "not there any more" in entry["detail"] and blocked_groups(rig) == []


def test_check_by_the_handle_does_not_count_a_file_that_came_after_a_conflict(
    rig: Rig, preset: Path, out: Path
) -> None:
    def appear(label: str | None) -> None:
        if label == OP and "_result = _ex_export(" in rig.exec.calls[-1].code:
            (out / "someone_elses.mov").write_bytes(b"theirs")

    rig.exec.before_run = appear
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "failed" and entry["basis"] == "handle"
    assert entry["current"]["writtenCount"] == 0
    assert entry["exportOutcome"]["errorCode"] == "CONFLICT"


def test_check_by_the_handle_counts_files_deep_in_the_tree(
    rig: Rig, preset: Path, out: Path
) -> None:
    rig.flame.export.subdir = "CLIPX/tape/1920x1080/a"
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["current"]["writtenCount"] == 24


def test_check_without_a_handle_does_not_count_hidden_files(
    rig: Rig, preset: Path, out: Path
) -> None:
    old_bridge(rig)
    rig.flame.export.hidden = [".lock"]
    rig.flame.export.limit = 0
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "not_landed" and entry["current"]["writtenCount"] == 0


def test_check_without_a_handle_is_unknown_when_the_count_is_cut_short(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    monkeypatch.setattr(export_check, "EXPORT_COUNT_LIMIT", 5)
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "could not be counted" in entry["detail"]
    assert blocked_groups(rig) == [group]


def test_check_without_a_handle_reads_folders_without_files_as_not_landed(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_bridge(rig)
    rig.flame.export.subdir = "a/b"
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    monkeypatch.setattr(export_check, "EXPORT_COUNT_DEPTH", 2)
    entry = check(rig, group)
    assert entry["verdict"] == "not_landed" and entry["basis"] == "disk"
    assert blocked_groups(rig) == []


def test_an_unknown_check_tells_a_person_to_look_at_the_directory(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    monkeypatch.setattr(export_check, "EXPORT_COUNT_LIMIT", 5)
    entry = check(rig, group)
    assert entry["verdict"] == "unknown"
    assert "person" in entry["detail"] and "output directory" in entry["detail"]
    assert "no operation" in entry["detail"]


def test_check_by_the_handle_with_a_count_cut_short_says_landed_with_a_note(
    rig: Rig, preset: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    monkeypatch.setattr(export_check, "EXPORT_COUNT_LIMIT", 5)
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert "could not be counted" in entry["detail"] and blocked_groups(rig) == []


def test_check_messages_name_the_export_not_the_render(rig: Rig, preset: Path, out: Path) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = export(rig, preset, out, timeout_s=1)["journal"]["group"]
    rig.bridge.failure = bridge_result(ok=False, error_code="TIMEOUT", error="no reply")
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "the export's result" in entry["detail"]
    assert "render" not in entry["detail"].lower()


# -- no undo, no plan, policy -----------------------------------------------------------------


def test_an_export_group_cannot_be_reverted(rig: Rig, preset: Path, out: Path) -> None:
    group = export(rig, preset, out)["journal"]["group"]
    env = rig.do("journal.revert", {"group": group})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "cannot be undone" in env["error"] and "export" in env["error"]
    assert len(listing(out)) == 24


def test_plan_run_refuses_render_export_with_the_reason_and_runs_nothing(
    rig: Rig, preset: Path, out: Path
) -> None:
    args = {"target": CLIPX, "preset": str(preset), "output_dir": str(out)}
    env = rig.do("plan.run", {"steps": [{"op": OP, "args": args}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    (issue,) = env["result"]["issues"]
    assert issue["op"] == OP and "occupies Flame" in issue["problems"][0]
    assert "cannot be undone" in issue["problems"][0]
    assert rig.flame.export.calls == [] and rig.exec.labels() == []


def test_readonly_hides_render_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, preset: Path, out: Path
) -> None:
    readonly = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = readonly.do(OP, {"target": wt("/wt/c1"), "preset": str(preset), "output_dir": str(out)})
    assert env["errorCode"] == "POLICY_DENIED"


def test_the_instructions_ask_for_confirmation_before_render_export() -> None:
    text = server_mod.INSTRUCTIONS
    assert "render.export" in text and "ask the user to confirm" in text
    assert len(text.encode("utf-8")) <= server_mod.MAX_INSTRUCTIONS_BYTES


def test_the_declarations(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None
    assert (op.category, op.effect, op.undo, op.thread) == (
        "render",
        "create",
        "unsupported",
        "main-async",
    )
    assert op.accepts == ("wiretap", "path", "selection")
    assert [p.name for p in op.params] == [
        "target",
        "preset",
        "output_dir",
        "timeout_s",
        "allow_default_path",
        "between_marks",
        "skip_colour_check",
    ]
    assert "allow_overwrite" not in {p.name for p in op.params}
    assert op.undo_steps is not None and op.undo_steps({}, 5) == 0
