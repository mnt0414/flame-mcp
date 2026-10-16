"""FLAME_MCP_E2E_FLAME_VERSION and FLAME_MCP_E2E_PROJECT on the Python side of the real-Flame e2e.

The two defaults live once in scripts/e2e_guard.py (Python) and once in scripts/e2e_flame_env.sh
(shell). Everything the clients derive from them (the expected hello version, the export preset
path, the pattern a kill target must match, the project name each client compares with) has to
follow the variables. A fresh interpreter imports the client modules for each setting, because
those values are module-level constants."""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

e2e_guard = importlib.import_module("e2e_guard")

DEFAULT_PROJECT = "flame_mcp_probe"
DEFAULT_VERSION = "2025.2.4"
OTHER_PROJECT = "flame_mcp_probe_2026"
OTHER_VERSION = "2026.2.3"

PUBLISHED_PROJECT_ATTRS = [
    ("p1_mcp_e2e", "PROBE_PROJECT_NAME"),
    ("fix2_e2e_common", "TARGET_PROJECT"),
    ("fix3_reel_order_e2e", "TARGET_PROJECT"),
    ("fix6_same_name_nodes_e2e", "TARGET_PROJECT"),
    ("fix7_small_items_e2e", "TARGET_PROJECT"),
    ("p2_4_clean_probe_project", "TARGET_PROJECT"),
]

# Exploratory scripts that only the development checkout has; checked where they exist.
DEV_ONLY_PROJECT_ATTRS = [
    ("w3_timewarp_probe", "TARGET_PROJECT"),
    ("fix2_library_batch_probe", "TARGET_PROJECT"),
    ("fix3_reel_order_probe", "TARGET_PROJECT"),
    ("r12_same_name_probe", "TARGET_PROJECT"),
    ("w7_display_probe", "TARGET_PROJECT"),
    ("w7_display_replay", "TARGET_PROJECT"),
    ("fix2_closed_library_probe", "TARGET_PROJECT"),
]

PROJECT_ATTRS = PUBLISHED_PROJECT_ATTRS + [
    (mod, attr) for mod, attr in DEV_ONLY_PROJECT_ATTRS if (SCRIPTS / f"{mod}.py").exists()
]

PROBE_CODE = """
import importlib, json, sys
sys.path.insert(0, {scripts!r})
sys.path.insert(0, {src!r})
out = {{"projects": {{}}}}
for mod, attr in {project_attrs!r}:
    out["projects"][mod] = getattr(importlib.import_module(mod), attr)
cleaner = importlib.import_module("p2_4_clean_probe_project")
out["clean_target_lines"] = [
    line for line in cleaner.stage_code("pass").splitlines() if line.startswith("_TARGET")
]
bridge = importlib.import_module("p1_bridge_e2e")
out["expected_version"] = bridge.EXPECTED_VERSION
common = importlib.import_module("p3_2a_e2e_common")
out["preset_source"] = str(common.PRESET_SOURCE)
transport = importlib.import_module("p1_transport_e2e")
tail = "/bin/flame.app/Contents/MacOS/flame -x"
out["bin_pattern"] = {{
    version: [bool(transport.FLAME_BIN_PATTERN.match("/opt/Autodesk/" + prefix + version + tail))
              for prefix in (".flamefamily_", "flame_")]
    for version in ("2025.2.4", "2026.2.3", "2026x2x3")
}}
print(json.dumps(out))
"""


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("FLAME_MCP_", "PHASE0B_"))}


def probe(extra_env: dict[str, str]) -> dict[str, Any]:
    code = PROBE_CODE.format(
        scripts=str(SCRIPTS), src=str(REPO / "src"), project_attrs=PROJECT_ATTRS
    )
    env = clean_env()
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    loaded: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return loaded


def test_without_the_variables_the_clients_use_the_old_values() -> None:
    got = probe({})

    assert got["projects"] == {mod: DEFAULT_PROJECT for mod, _ in PROJECT_ATTRS}
    assert got["clean_target_lines"] == [f"_TARGET = {DEFAULT_PROJECT!r}"]
    assert got["expected_version"] == DEFAULT_VERSION
    assert got["preset_source"] == (
        f"/opt/Autodesk/presets/{DEFAULT_VERSION}"
        "/export/presets/flame/file_sequence/Tiff/Tiff (16-bit LZW).xml"
    )
    assert got["bin_pattern"] == {
        "2025.2.4": [True, True],
        "2026.2.3": [False, False],
        "2026x2x3": [False, False],
    }


def test_the_variables_change_every_value_the_clients_derive() -> None:
    got = probe(
        {"FLAME_MCP_E2E_FLAME_VERSION": OTHER_VERSION, "FLAME_MCP_E2E_PROJECT": OTHER_PROJECT}
    )

    assert got["projects"] == {mod: OTHER_PROJECT for mod, _ in PROJECT_ATTRS}
    assert got["clean_target_lines"] == [f"_TARGET = {OTHER_PROJECT!r}"]
    assert got["expected_version"] == OTHER_VERSION
    assert got["preset_source"] == (
        f"/opt/Autodesk/presets/{OTHER_VERSION}"
        "/export/presets/flame/file_sequence/Tiff/Tiff (16-bit LZW).xml"
    )
    assert got["bin_pattern"] == {
        "2025.2.4": [False, False],
        "2026.2.3": [True, True],
        "2026x2x3": [False, False],
    }


def test_defaults_are_the_old_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(e2e_guard.E2E_PROJECT_ENV, raising=False)
    monkeypatch.delenv(e2e_guard.E2E_FLAME_VERSION_ENV, raising=False)

    assert e2e_guard.E2E_PROJECT_ENV == "FLAME_MCP_E2E_PROJECT"
    assert e2e_guard.E2E_FLAME_VERSION_ENV == "FLAME_MCP_E2E_FLAME_VERSION"
    assert e2e_guard.probe_project_name() == DEFAULT_PROJECT
    assert e2e_guard.e2e_flame_version() == DEFAULT_VERSION


def test_the_variables_override_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(e2e_guard.E2E_PROJECT_ENV, OTHER_PROJECT)
    monkeypatch.setenv(e2e_guard.E2E_FLAME_VERSION_ENV, OTHER_VERSION)

    assert e2e_guard.probe_project_name() == OTHER_PROJECT
    assert e2e_guard.e2e_flame_version() == OTHER_VERSION


def test_an_empty_variable_means_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(e2e_guard.E2E_PROJECT_ENV, "")
    monkeypatch.setenv(e2e_guard.E2E_FLAME_VERSION_ENV, "")

    assert e2e_guard.probe_project_name() == DEFAULT_PROJECT
    assert e2e_guard.e2e_flame_version() == DEFAULT_VERSION


@pytest.mark.parametrize(
    "name", ["a b", "x;y", "x/y", "../x", "-x", "x\ny", "caf" + chr(0xE9), chr(0x30C6) * 3, "x$y"]
)
def test_a_project_name_that_is_not_plain_ascii_is_rejected(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(e2e_guard.E2E_PROJECT_ENV, name)

    with pytest.raises(ValueError, match=e2e_guard.E2E_PROJECT_ENV):
        e2e_guard.probe_project_name()


ACCEPTED_PROJECTS = ["flame_mcp_probe", "flame_mcp_probe_2026", "flame_mcp_probe_a-b_C"]
PREFIX_REJECTED_PROJECTS = [
    "my_show",
    "flame_mcp_probeX",
    "flame_mcp_probe2026",
    "flame_mcp_prob",
    "Flame_mcp_probe_2026",
    "flame_mcp_probe_",
    "xflame_mcp_probe",
    "flame_mcp_probe-2026",
]


@pytest.mark.parametrize("name", ACCEPTED_PROJECTS)
def test_a_project_name_that_follows_the_prefix_rule_is_accepted(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(e2e_guard.E2E_PROJECT_ENV, name)

    assert e2e_guard.probe_project_name() == name


@pytest.mark.parametrize("name", PREFIX_REJECTED_PROJECTS)
def test_a_project_name_outside_the_prefix_rule_is_rejected_with_the_rule_in_the_message(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(e2e_guard.E2E_PROJECT_ENV, name)

    with pytest.raises(ValueError, match=e2e_guard.E2E_PROJECT_ENV) as raised:
        e2e_guard.probe_project_name()

    message = str(raised.value)
    assert "flame_mcp_probe" in message
    assert "flame_mcp_probe_" in message
    assert message.isascii()


@pytest.mark.parametrize("version", ["2026", "2026.", "a.b", "2026.2.3 ", "2026.2.3/..", "20.2$"])
def test_a_flame_version_that_is_not_dotted_digits_is_rejected(
    version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(e2e_guard.E2E_FLAME_VERSION_ENV, version)

    with pytest.raises(ValueError, match=e2e_guard.E2E_FLAME_VERSION_ENV):
        e2e_guard.e2e_flame_version()


def test_the_shell_and_the_python_defaults_are_the_same() -> None:
    text = (SCRIPTS / "e2e_flame_env.sh").read_text(encoding="utf-8")
    shell_version = re.search(r'^E2E_DEFAULT_FLAME_VERSION="([^"]+)"$', text, re.MULTILINE)
    shell_project = re.search(r'^E2E_DEFAULT_PROJECT="([^"]+)"$', text, re.MULTILINE)

    assert shell_version is not None
    assert shell_project is not None
    assert shell_version.group(1) == e2e_guard.DEFAULT_E2E_FLAME_VERSION
    assert shell_project.group(1) == e2e_guard.DEFAULT_PROBE_PROJECT_NAME


# Scripts that are allowed to hold the default project name or Flame version in code: the one
# definition (e2e_guard.py), the text of the generated doc (it names the version the runs were
# made on), and the phase 0 investigation tools that deliberately run only on 2025.2.4.
LITERAL_ALLOWED = {
    "e2e_guard",
    "build_testing_doc",
    "build_api_symbols",
    "p3_0_restore_probe",
    "run_phase0b_auto",
    "run_two_stage",
    "run_c6_hooks",
    "run_u3_commit",
}
LITERAL_ALLOWED_PREFIXES = ("phase0",)
PROJECT_LITERAL = re.compile(r"(?<![A-Za-z0-9_])flame_mcp_probe(?![A-Za-z0-9_])")
VERSION_LITERAL = re.compile(r"2025\.2\.4|2025\\\.2\\\.4|flame202524")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _code_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


def _scripts_in_scope() -> list[Path]:
    return sorted(
        p
        for p in SCRIPTS.glob("*.py")
        if p.stem not in LITERAL_ALLOWED and not p.name.startswith(LITERAL_ALLOWED_PREFIXES)
    )


@pytest.mark.parametrize("path", _scripts_in_scope(), ids=lambda p: p.name)
def test_no_client_script_holds_the_default_project_or_version_in_code(path: Path) -> None:
    hits = [
        s for s in _code_strings(path) if PROJECT_LITERAL.search(s) or VERSION_LITERAL.search(s)
    ]

    assert hits == []


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in sorted(SCRIPTS.glob("*.sh"))
        if p.stem not in LITERAL_ALLOWED and p.name != "e2e_flame_env.sh"
    ],
    ids=lambda p: p.name,
)
def test_no_shell_script_but_the_env_file_holds_the_default_project_or_version(
    path: Path,
) -> None:
    code_lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    hits = [
        line for line in code_lines if PROJECT_LITERAL.search(line) or VERSION_LITERAL.search(line)
    ]

    assert hits == []
