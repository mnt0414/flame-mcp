"""Tests `batch.compare_setup`: the Batch group's setup is saved into a work directory of the
server's own, compared there with an earlier `.batch` file (`setup_compare`) and the directory is
removed. Run through the real Runtime over the fake Flame tree (journal_test_helpers.py); the
fake's save_setup writes the `.batch` file as JSON and, with `save_setup_extra_files`, node files.
The real Flame is not touched here.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

import journal_test_helpers
from flame_mcp import checkpoints
from flame_mcp.policy import Policy
from flame_mcp.setup_edit import WORKDIR_MAX_AGE_DAYS
from flame_mcp.setup_work import work_root
from journal_test_helpers import BG1, Rig, make_rig

OP = "batch.compare_setup"
DAY_S = 86400


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def save_baseline(rig: Rig, tmp_path: Path, name: str = "baseline") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    assert rig.handles["bg1"].save_setup(str(directory / "snap")) is True
    return directory / "snap.batch"


def compare(rig: Rig, baseline: Path | str, target: dict[str, Any] = BG1) -> dict[str, Any]:
    return rig.do(OP, {"target": target, "baseline": str(baseline)})


def leftovers(rig: Rig) -> list[str]:
    root = Path(work_root())
    return sorted(str(p) for p in root.rglob("*")) if root.exists() else []


def workdirs(rig: Rig) -> list[Path]:
    root = Path(work_root())
    return sorted(root.glob("cmp_*")) if root.exists() else []


def sent_to_flame(rig: Rig) -> list[str]:
    return [c.code for c in rig.exec.calls if c.label == OP]


# -- registration -------------------------------------------------------------------------------


def test_the_op_is_a_wrapped_read_with_no_undo_that_takes_target_and_baseline(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None and op.checkpoint is None and op.journal is None
    assert (op.category, op.effect, op.undo) == ("batch", "read", "none")
    assert op.accepts == ("batch",)
    assert op.wrap is not None and op.precheck is not None
    assert [(p.name, p.type, p.required, p.non_empty) for p in op.params] == [
        ("target", "selector", True, False),
        ("baseline", "string", True, True),
    ]
    assert set(op.requires) == {"PyBatch.save_setup", "PyBatch.uid", "PyBatch.name"}


def test_the_description_follows_the_rules(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None
    text = op.description
    assert text.isascii()
    assert not re.search(r"plan/\d+|P\d-\d|not measured", text)
    for phrase in ("baseline", "Timeline FX", "changed", "hunks", "tooLargeToList", "project"):
        assert phrase in text, phrase


@pytest.mark.parametrize("name", ["batch.fingerprint", "batch.checkpoint"])
def test_the_fingerprint_ops_point_to_the_comparison_for_values(rig: Rig, name: str) -> None:
    op = rig.registry.get(name)
    assert op is not None
    assert OP in op.description
    assert op.description.isascii()


# -- the comparison -----------------------------------------------------------------------------


def test_nothing_changed_is_reported_as_unchanged_and_the_work_directory_is_gone(
    rig: Rig, tmp_path: Path
) -> None:
    baseline = save_baseline(rig, tmp_path)
    env = compare(rig, baseline)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["changed"] is False
    assert result["files"] == [] and result["changedFiles"] == 0
    assert result["filesCompared"] == 1 and result["truncated"] is False
    assert result["baseline"] == str(baseline)
    assert result["batch"] == {"uid": "uid-b1", "name": "BG1"}
    assert result["stable"] == {"kind": "batch", "uid": "uid-b1"}
    assert "cleanupError" not in result
    assert workdirs(rig) == []


def test_a_changed_node_is_reported_with_the_lines_that_differ(rig: Rig, tmp_path: Path) -> None:
    baseline = save_baseline(rig, tmp_path)
    rig.handles["blur"].name.set_value("blur_renamed")
    result = compare(rig, baseline)["result"]
    assert result["changed"] is True
    [entry] = result["files"]
    assert entry["file"] == ".batch" and entry["status"] == "changed"
    [hunk] = entry["hunks"]
    assert "blur_renamed" in hunk["after"][0] and "blur_renamed" not in hunk["before"][0]
    assert workdirs(rig) == []


def test_a_node_file_that_differs_is_reported_by_name(rig: Rig, tmp_path: Path) -> None:
    bg = rig.handles["bg1"]
    bg.save_setup_extra_files = {
        "axis1.action": "Name axis1\nChannel position/x\nValue 0\n",
        "MV.json": '{"zoom": 1}',
    }
    baseline = save_baseline(rig, tmp_path)
    bg.save_setup_extra_files = {
        "axis1.action": "Name axis1\nChannel position/x\nValue 4\n",
        "MV.json": '{"zoom": 2}',
        "extra.output_node": "x",
    }
    result = compare(rig, baseline)["result"]
    assert result["changedFiles"] == 2
    action, extra = result["files"]
    assert action["file"] == "axis1.action"
    assert action["hunks"] == [
        {
            "where": {"node": "axis1", "channel": "position/x"},
            "before": ["Value 0"],
            "after": ["Value 4"],
        }
    ]
    assert extra == {"file": "extra.output_node", "status": "added"}
    assert workdirs(rig) == []


def test_the_group_need_not_be_current(rig: Rig, tmp_path: Path) -> None:
    baseline = save_baseline(rig, tmp_path)
    before = rig.flame.projects.current_project.current_workspace.desktop.current_batch_group
    env = rig.do(OP, {"target": {"kind": "batch", "uid": "uid-b1"}, "baseline": str(baseline)})
    assert env["ok"] is True and env["result"]["changed"] is False
    after = rig.flame.projects.current_project.current_workspace.desktop.current_batch_group
    assert after is before


def test_it_changes_nothing_in_flame_and_records_nothing(rig: Rig, tmp_path: Path) -> None:
    baseline = save_baseline(rig, tmp_path)
    bg = rig.handles["bg1"]
    saves = len(bg.save_calls)
    nodes = sorted(str(n.name.get_value()) for n in bg.nodes)
    compare(rig, baseline)
    assert len(bg.save_calls) == saves + 1
    assert bg.load_calls == []
    assert sorted(str(n.name.get_value()) for n in bg.nodes) == nodes
    assert rig.records() == []


def test_the_baseline_is_not_sent_to_flame_and_the_code_is_ascii(rig: Rig, tmp_path: Path) -> None:
    baseline = save_baseline(rig, tmp_path, "unique_baseline_dir")
    compare(rig, baseline)
    [code] = sent_to_flame(rig)
    assert code.isascii()
    assert "unique_baseline_dir" not in code and str(baseline) not in code
    assert "_save_for_compare" in code
    assert "/setups/cmp_" in code


def test_the_work_directory_is_private(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = save_baseline(rig, tmp_path)
    seen: list[int] = []
    original = rig.handles["bg1"].save_setup

    def spy(path: str) -> bool:
        seen.append(os.stat(os.path.dirname(path)).st_mode & 0o777)
        return bool(original(path))

    monkeypatch.setattr(rig.handles["bg1"], "save_setup", spy)
    compare(rig, baseline)
    assert seen == [0o700]


# -- refusals before Flame ----------------------------------------------------------------------


def test_a_baseline_that_is_not_usable_is_refused_and_nothing_is_sent(
    rig: Rig, tmp_path: Path
) -> None:
    good = save_baseline(rig, tmp_path)
    no_dir = tmp_path / "lonely.batch"
    no_dir.write_text("<Setup></Setup>")
    not_batch = tmp_path / "baseline" / "snap.txt"
    not_batch.write_text("x")
    (tmp_path / "baseline" / "snap_txt").mkdir()
    cases = [
        ("snap.batch", "INVALID_ARGS"),
        (str(not_batch), "INVALID_ARGS"),
        (str(tmp_path / "missing.batch"), "PRECONDITION_UNMET"),
        (str(no_dir), "PRECONDITION_UNMET"),
        (str(good.parent), "INVALID_ARGS"),
    ]
    saves = len(rig.handles["bg1"].save_calls)
    for baseline, code in cases:
        env = compare(rig, baseline)
        assert env["ok"] is False and env["errorCode"] == code, (baseline, env)
    assert sent_to_flame(rig) == []
    assert len(rig.handles["bg1"].save_calls) == saves
    assert workdirs(rig) == []


def test_an_empty_baseline_is_invalid(rig: Rig) -> None:
    env = rig.do(OP, {"target": BG1, "baseline": ""})
    assert env["errorCode"] == "INVALID_ARGS"


def test_a_reserved_argument_cannot_be_given_by_a_caller(rig: Rig, tmp_path: Path) -> None:
    baseline = save_baseline(rig, tmp_path)
    env = rig.do(OP, {"target": BG1, "baseline": str(baseline), "_dir": str(tmp_path)})
    assert env["errorCode"] == "INVALID_ARGS"
    assert sent_to_flame(rig) == []


# -- failures and the work directory ------------------------------------------------------------


def test_a_save_setup_that_returns_false_fails_and_the_directory_is_removed(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = save_baseline(rig, tmp_path)
    monkeypatch.setattr(rig.handles["bg1"], "save_setup", lambda path: False)
    env = compare(rig, baseline)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert "False" in env["error"]
    assert workdirs(rig) == []


def test_a_save_that_writes_no_batch_file_fails_and_the_directory_is_removed(
    rig: Rig, tmp_path: Path
) -> None:
    baseline = save_baseline(rig, tmp_path)
    rig.handles["bg1"].save_writes_nothing = True
    env = compare(rig, baseline)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    assert ".batch" in env["error"]
    assert workdirs(rig) == []


def test_a_group_that_cannot_be_found_fails_and_the_directory_is_removed(
    rig: Rig, tmp_path: Path
) -> None:
    baseline = save_baseline(rig, tmp_path)
    env = compare(rig, baseline, {"kind": "batch", "name": "NoSuchGroup"})
    assert env["ok"] is False
    assert env["errorCode"] in {"SELECTOR_EMPTY", "SELECTOR_TYPE"}
    assert workdirs(rig) == []


def script_timeout_phase(monkeypatch: pytest.MonkeyPatch, phase: str | None) -> None:
    original = journal_test_helpers._error_result

    def scripted(code: str) -> Any:
        result = original(code)
        return dataclasses.replace(result, timeout_phase=phase) if code == "TIMEOUT" else result

    monkeypatch.setattr(journal_test_helpers, "_error_result", scripted)


@pytest.mark.parametrize("phase", ["running", None])
def test_the_work_directory_stays_after_a_timeout_that_may_have_started(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str | None
) -> None:
    baseline = save_baseline(rig, tmp_path)
    script_timeout_phase(monkeypatch, phase)
    rig.exec.fault(OP, "TIMEOUT", when="after")
    env = compare(rig, baseline)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    assert env["timeoutPhase"] == phase
    [kept] = workdirs(rig)
    assert re.fullmatch(r"cmp_[0-9a-f]{12}", kept.name)
    assert any(p.suffix == ".batch" for p in kept.iterdir())


def test_the_work_directory_stays_after_bridge_lost(rig: Rig, tmp_path: Path) -> None:
    baseline = save_baseline(rig, tmp_path)
    rig.exec.fault(OP, "BRIDGE_LOST", when="after")
    env = compare(rig, baseline)
    assert env["ok"] is False and env["errorCode"] == "BRIDGE_LOST"
    [kept] = workdirs(rig)
    assert re.fullmatch(r"cmp_[0-9a-f]{12}", kept.name)
    assert any(p.suffix == ".batch" for p in kept.iterdir())


def test_the_work_directory_is_removed_after_a_timeout_that_never_started(
    rig: Rig, tmp_path: Path
) -> None:
    baseline = save_baseline(rig, tmp_path)
    rig.exec.fault(OP, "TIMEOUT", when="before")
    env = compare(rig, baseline)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    assert env["timeoutPhase"] == "queued"
    assert workdirs(rig) == []


def test_a_work_directory_that_cannot_be_made_fails_in_the_envelope(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = save_baseline(rig, tmp_path)

    def refuse(directory: Path) -> None:
        raise PermissionError(13, "Permission denied", str(directory))

    monkeypatch.setattr(checkpoints, "make_private_dirs", refuse)
    env = compare(rig, baseline)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED"
    assert "Permission denied" in env["error"]
    assert sent_to_flame(rig) == []
    assert workdirs(rig) == []


@pytest.mark.parametrize("gone", ["directory", "batch"])
def test_a_baseline_that_vanishes_after_the_check_is_precondition_unmet_and_not_all_added(
    rig: Rig, tmp_path: Path, gone: str
) -> None:
    baseline = save_baseline(rig, tmp_path)

    def remove(label: str | None) -> None:
        if label == OP and gone == "directory":
            shutil.rmtree(baseline.with_suffix(""))
        elif label == OP:
            baseline.unlink()

    rig.exec.before_run = remove
    env = compare(rig, baseline)
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert str(baseline.parent) in env["error"]
    assert workdirs(rig) == []


def test_a_directory_older_than_the_limit_is_removed_by_the_next_call_and_only_that(
    rig: Rig, tmp_path: Path
) -> None:
    baseline = save_baseline(rig, tmp_path)
    root = Path(work_root())
    root.mkdir(parents=True, exist_ok=True)
    old_time = time.time() - (WORKDIR_MAX_AGE_DAYS + 1) * DAY_S
    fresh_time = time.time() - (WORKDIR_MAX_AGE_DAYS - 1) * DAY_S

    def make(name: str, when: float) -> Path:
        directory = root / name
        directory.mkdir()
        (directory / "snap.batch").write_text("x")
        (directory / "snap").mkdir()
        (directory / "snap" / "n.action").write_text("y")
        os.utime(directory, (when, when))
        return directory

    stale = make("cmp_aaaaaaaaaaaa", old_time)
    fresh = make("cmp_bbbbbbbbbbbb", fresh_time)
    other_prefix = make("s_cccccccccccc", old_time)
    bad_name = make("cmp_zzzzzzzzzzzz", old_time)
    short_name = make("cmp_abc", old_time)
    target = make("target_dir", old_time)
    link = root / "cmp_dddddddddddd"
    os.symlink(target, link)
    compare(rig, baseline)
    assert not stale.exists()
    for kept in (fresh, other_prefix, bad_name, short_name, target):
        assert kept.exists(), kept
    assert link.is_symlink() and (target / "snap.batch").exists()
    assert workdirs(rig) == sorted([fresh, bad_name, short_name, link])


# -- the server around it -----------------------------------------------------------------------


def test_a_read_only_server_runs_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ro = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    baseline = save_baseline(ro, tmp_path)
    env = compare(ro, baseline)
    assert env["ok"] is True and env["result"]["changed"] is False


def test_a_plan_runs_it_as_a_step(rig: Rig, tmp_path: Path) -> None:
    baseline = save_baseline(rig, tmp_path)
    steps = [
        {"op": OP, "args": {"target": BG1, "baseline": str(baseline)}},
        {"op": "batch.fingerprint", "args": {"target": BG1}},
    ]
    env = rig.do("plan.run", {"steps": steps})
    assert env["ok"] is True, env
    assert env["result"]["steps"][0]["result"]["changed"] is False
    assert workdirs(rig) == []


def test_a_timeout_leaves_the_guard_clear_and_the_next_call_succeeds(
    rig: Rig, tmp_path: Path
) -> None:
    baseline = save_baseline(rig, tmp_path)
    rig.exec.fault(OP, "TIMEOUT", when="after")
    assert compare(rig, baseline)["errorCode"] == "TIMEOUT"
    assert rig.runtime.guard.denial() is None
    assert compare(rig, baseline)["ok"] is True


def test_a_plan_step_whose_result_is_too_big_keeps_changed_and_changed_files(
    rig: Rig, tmp_path: Path
) -> None:
    bg = rig.handles["bg1"]
    count = 30
    bg.save_setup_extra_files = {
        f"n{i:02d}.action": f"Name n{i}\nValue old\n" for i in range(count)
    }
    baseline = save_baseline(rig, tmp_path)
    bg.save_setup_extra_files = {
        f"n{i:02d}.action": f"Name n{i}\nValue new {'x' * 150}\n" for i in range(count)
    }
    for detail in ("full", "summary"):
        env = rig.do(
            "plan.run",
            {
                "steps": [{"op": OP, "args": {"target": BG1, "baseline": str(baseline)}}],
                "result_detail": detail,
            },
        )
        assert env["ok"] is True, env
        [step] = env["result"]["steps"]
        want = {"changed": True, "changedFiles": count}
        if detail == "full":
            assert step["result"]["omitted"] is True and step["result"]["bytes"] > 4_000
            assert step["result"]["brief"] == want
        else:
            assert step["brief"] == want
