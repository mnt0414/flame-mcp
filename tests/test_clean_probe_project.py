"""scripts/p2_4_clean_probe_project.py: it deletes only what the tests made (a known prefix and a
timestamp in the name) and reports everything else as untouched. The Flame-side delete code is run
against a small fake so that its own refusal of a name that is not a test name is exercised."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load() -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module("p2_4_clean_probe_project")


TEST_NAMES = [
    "P23A_B1_261001_123456",
    "P32A_261001_123456_CKPT",
    "P1O_RG_261001_123456",
    "P24S4_261001_123456",
    "P24ST_P1_261001_123456",
    "P2D_261001_123456_G1 1",
    "UNDO_261001_123456",
    "P34_261001_123456_LIB",
    "P34E_261001_123456_RG",
    "P34E_261001_123456_CKPT",
    "P34E_SEQ_261001_123456",
    "P35_261001_123456_RG",
    "PFX2_261007_211605_BGN",
    "PFX2_261007_211605_LIB 1",
    "PFX3E_261008_103000_GN",
    "PR12_261012_103000_G",
    "PR12_261012_103000_C",
    "PR12E_261012_103000_GN",
    "PR12E_261012_103000_GC",
    "PR12E_261012_103000_CKPT",
    "PR5E_261008_103000_CKPT",
    "PR5E_261008_103000_RG",
    "PR5E_261008_103000_S2",
    "PF7E_261012_103000_RG",
    "PF7E_261012_103000_GB",
    "PF7E_261012_103000_CKPT",
    "PTW3E_261008_103000_CKPT",
    "PTW3E_261008_103000_RG",
    "PW7_261008_103000_RG",
    "PW7_261008_103000_reel_N",
    "PW7_261008_103000_S_N",
    "P35_261001_123456_CKPT",
    "P35_261001_123456_SHOT_H6",
]
OTHER_NAMES = [
    "defalt",
    "Default Library",
    "Grabbed References",
    "Timeline FX",
    "Batch",
    "Reel Group",
    "flame_mcp_checkpoints",
    "U5S_RG_261001",
    "P99_261001_123456",
    "P23A_261001",
    "MyP23A_B1_261001_123456",
    "P23A B1_261001_123456",
    "P23A_(x)_261001_123456",
    "P2_261001_123456",
    "P34X_261001_123456",
    "P34E_261001",
    "P35_261001",
    "P35X_261001_123456",
    "PW7_261008",
    "PW71_261008_103000",
    "PR121_261012_103000",
    "PR12_261012",
    "PR12E_261012",
    "PR12E1_261012_103000",
    "PF7E_261012",
    "PF7E1_261012_103000",
]


@pytest.mark.parametrize("name", TEST_NAMES)
def test_a_name_with_a_known_prefix_and_a_timestamp_is_a_test_name(name: str) -> None:
    assert load().TEST_NAME.match(name)


@pytest.mark.parametrize("name", OTHER_NAMES)
def test_every_other_name_is_not(name: str) -> None:
    assert not load().TEST_NAME.match(name)


def entry(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "uid": "uid-" + name, **extra}


def reel_group(name: str, hits: int = 0) -> dict[str, Any]:
    keep = [{"reel": "R", "type": "PySequence", "name": "U5S_x"}] * hits
    return entry(name, n_reels=1, n_children=hits, keep_hits=keep)


def test_the_plan_deletes_only_test_names_and_lists_everything_else_as_untouched() -> None:
    module = load()
    plan = module.build_plan(
        {
            "libraries": [
                entry("Default Library"),
                entry("defalt"),
                entry("P32A_261001_123456_CKPT"),
                entry("flame_mcp_checkpoints"),
            ],
            "batch_groups": [entry("P23A_B1_261001_123456"), entry("Batch"), entry("Mine")],
            "reel_groups": [
                reel_group("P1O_RG_261001_123456"),
                reel_group("P21_RG_261001_123456", hits=1),
                reel_group("RG1"),
            ],
        }
    )
    assert plan["libraries"]["delete"]["names"] == ["P32A_261001_123456_CKPT"]
    assert plan["batchGroups"]["delete"]["names"] == ["P23A_B1_261001_123456"]
    assert plan["batchGroups"]["create"] == [module.NEW_BATCH_GROUP_NAME]
    assert module.TEST_NAME.match(module.NEW_BATCH_GROUP_NAME)
    assert plan["reelGroups"]["delete"]["names"] == ["P1O_RG_261001_123456"]
    assert plan["reelGroups"]["keep"]["names"] == ["P21_RG_261001_123456"]
    assert plan["reelGroups"]["create"] == []
    assert plan["untouched"] == {
        "libraries": ["Default Library", "defalt", "flame_mcp_checkpoints"],
        "batchGroups": ["Batch", "Mine"],
        "reelGroups": ["P21_RG_261001_123456", "RG1"],
    }


def test_nothing_to_delete_creates_nothing() -> None:
    plan = load().build_plan(
        {
            "libraries": [entry("defalt")],
            "batch_groups": [entry("Batch")],
            "reel_groups": [reel_group("RG1")],
        }
    )
    assert plan["libraries"]["delete"]["count"] == 0
    assert plan["batchGroups"]["create"] == [] and plan["batchGroups"]["delete"]["count"] == 0
    assert plan["reelGroups"]["create"] == [] and plan["reelGroups"]["delete"]["count"] == 0


def test_every_reel_group_deleted_makes_a_new_one_first() -> None:
    plan = load().build_plan(
        {
            "libraries": [],
            "batch_groups": [],
            "reel_groups": [reel_group("P1O_RG_261001_123456")],
        }
    )
    assert plan["reelGroups"]["create"] == ["Reel Group"]


def test_an_unreadable_entry_is_never_deleted() -> None:
    plan = load().build_plan(
        {
            "libraries": [entry("ERR:KeyError('x')", uid="ERR:y")],
            "batch_groups": [],
            "reel_groups": [],
        }
    )
    assert plan["libraries"]["delete"]["count"] == 0
    assert plan["unreadable"]["libraries"] == 1


class FakeLibrary:
    def __init__(self, name: str) -> None:
        self.name = name
        self.uid = "uid-" + name


def run_delete_code(module: ModuleType, names: list[str], items: list[str]) -> dict[str, Any]:
    libraries = [FakeLibrary(n) for n in names]
    workspace = SimpleNamespace(libraries=libraries, desktop=SimpleNamespace())
    project = SimpleNamespace(name=module.TARGET_PROJECT, current_workspace=workspace)
    deleted: list[str] = []

    def delete(obj: FakeLibrary, confirm: bool = True) -> bool:
        libraries.remove(obj)
        deleted.append(obj.name)
        return True

    flame = SimpleNamespace(projects=SimpleNamespace(current_project=project), delete=delete)
    code = module.delete_code("library", [entry(n) for n in items], [])
    scope: dict[str, Any] = {"flame": flame}
    exec(compile(code, "<clean>", "exec"), scope)
    rows = {r["name"]: r for r in scope["_result"]["data"]["rows"]}
    return {"rows": rows, "deleted": deleted, "left": [lib.name for lib in libraries]}


def test_the_flame_side_refuses_a_name_that_is_not_a_test_name_even_when_it_is_asked_for() -> None:
    module = load()
    out = run_delete_code(
        module,
        ["defalt", "Default Library", "P32A_261001_123456_CKPT"],
        ["defalt", "Default Library", "P32A_261001_123456_CKPT"],
    )
    assert out["deleted"] == ["P32A_261001_123456_CKPT"]
    assert out["rows"]["defalt"]["skipped"] == "not_a_test_name"
    assert out["rows"]["Default Library"]["skipped"] == "not_a_test_name"
    assert out["left"] == ["defalt", "Default Library"]


def test_the_flame_side_does_not_delete_what_was_renamed_since_the_plan() -> None:
    module = load()
    libraries = [FakeLibrary("defalt")]
    libraries[0].uid = "uid-P32A_261001_123456_CKPT"
    workspace = SimpleNamespace(libraries=libraries, desktop=SimpleNamespace())
    project = SimpleNamespace(name=module.TARGET_PROJECT, current_workspace=workspace)
    flame = SimpleNamespace(
        projects=SimpleNamespace(current_project=project),
        delete=lambda obj, confirm=True: pytest.fail("deleted"),
    )
    code = module.delete_code("library", [entry("P32A_261001_123456_CKPT")], [])
    scope: dict[str, Any] = {"flame": flame}
    exec(compile(code, "<clean>", "exec"), scope)
    [row] = scope["_result"]["data"]["rows"]
    assert row["skipped"] == "name_changed"


def run_with_bridge(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    project: str | None,
    execute: Callable[[Any, str], Any] = lambda runner, mode: None,
) -> tuple[dict[str, Any], list[str]]:
    candidate = SimpleNamespace(pid=1, flame_version="2025.2.4", project_name=project)
    exited: list[str] = []
    monkeypatch.setattr(module, "_wait_for_bridge", lambda run_dir, wait_s: candidate)
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(module, "execute", execute)
    monkeypatch.setattr(
        module, "exit_flame", lambda run_dir, results, save: exited.append("flame.exit()")
    )
    results: dict[str, Any] = {"ok": True, "stages": {}}
    module.run(tmp_path, 0.0, "dry-run", results)
    return results, exited


def test_a_project_that_is_not_the_probe_stops_the_run_without_exiting_flame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load()
    monkeypatch.setattr(
        module.transport, "exec", lambda *a, **k: pytest.fail("code was sent to Flame")
    )

    results, exited = run_with_bridge(module, monkeypatch, tmp_path, "production_job")

    assert exited == []
    assert "production_job" in results["stopped"]
    assert "exit" not in results


def test_the_probe_project_is_exited_at_the_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load()

    results, exited = run_with_bridge(module, monkeypatch, tmp_path, module.TARGET_PROJECT)

    assert exited == ["flame.exit()"]
    assert results["stopped"] is None


@pytest.mark.parametrize(
    "answer",
    [
        {"aborted": "project_mismatch", "project": "production_job"},
        {"data": {"aborted": "project_changed", "rows": []}},
    ],
)
def test_a_project_change_found_inside_flame_stops_the_run_without_exiting_flame(
    answer: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = load()
    sent: list[str] = []

    def fake_exec(code: str, **kwargs: Any) -> Any:
        sent.append(code)
        return SimpleNamespace(
            ok=True,
            result=answer,
            error_code=None,
            error=None,
            traceback=None,
            duration_ms=0,
        )

    monkeypatch.setattr(module.transport, "exec", fake_exec)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)

    results, exited = run_with_bridge(
        module,
        monkeypatch,
        tmp_path,
        module.TARGET_PROJECT,
        lambda runner, mode: runner.call("inventory", "t", "code", 1.0),
    )

    assert len(sent) == 1
    assert exited == []
    assert "aborted" in results["stopped"]
    assert "exit" not in results
