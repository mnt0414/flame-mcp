"""Published prose (op, parameter and category descriptions, flame_context, CHANGELOG.md, docs/*.md,
README.md, and the strings the server returns to callers) carries no calendar dates, no wording of
how a measurement was verified in a development run, and no statement of what was NOT measured or
verified."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from flame_mcp import server as server_mod
from flame_mcp.ops import register_all
from flame_mcp.registry import Registry

ROOT = Path(__file__).resolve().parent.parent
MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DATE = re.compile(
    r"\b20\d\d[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|\b20\d\d\.(?:0[1-9]|1[0-2])\.(?:0[1-9]|[12]\d|3[01])\b"
    r"|\b20\d\d-\d{2}\b"
    rf"|\b{MONTH}\.? \d{{1,2}}\b"
    rf"|\b\d{{1,2}} {MONTH}\b"
)
RUN_NARRATIVE = re.compile(
    r"(?i)old judgment|red overall|stage d\b|passed as a whole|runs? of the (real )?e2e|"
    r"judged version|e2e p\d_\d|\bm9-m11|"
    r"\btwice\b[^.]{0,40}\b(?:identical|same)|(?<!of )\b(?:two|three|\d+) runs\b|same values"
)
UNMEASURED = re.compile(
    r"(?i)\bnot (?:yet )?(?:been )?(?:measured|verified|tried|confirmed)\b"
    r"|\b(?:unmeasured|unverified|unconfirmed|untried)\b"
    r"|\bnever (?:been )?(?:measured|tried)\b|\bsays nothing about\b"
    r"|\b(?:was|were|has|have) not (?:been )?recorded\b|\bnot the one measured\b"
    r"|\b(?:hasn't|haven't|wasn't|weren't|isn't|aren't) (?:yet )?(?:been )?"
    r"(?:measured|verified|tried|confirmed)\b"
    r"|\byet to be (?:measured|verified|tried|confirmed)\b"
)
VERSION_FEATURE = re.compile(
    r"(?i)untestedFlameVersion|not a tested version|untested (?:Flame )?version"
)
MARKER_CONSTANTS = frozenset({"UNDO UNCONFIRMED"})
LITERAL_JOINT = re.compile(r"""['"]\s*\+?\s*['"]""")
RUNTIME_STATE = re.compile(r"(?i)end of the render was not confirmed")
PROSE_FILES = [
    ROOT / "CHANGELOG.md",
    ROOT / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
]
SRC_FILES = sorted((ROOT / "src").rglob("*.py"))


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = Registry()
    register_all(reg)
    return reg


def _texts(reg: Registry) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for op in reg.list_ops():
        texts.append((f"op {op.name}", op.description))
        texts += [(f"param {op.name}.{p.name}", p.description) for p in op.params]
    texts += [(f"category {name}", c.description) for name, c in reg.categories().items()]
    texts.append(("flame_context", json.dumps(server_mod._static_context(reg))))
    return texts


def _flagged(where: str, text: str) -> list[str]:
    return [f"{where}: {m.group(0)!r}" for rx in (DATE, RUN_NARRATIVE) for m in rx.finditer(text)]


def _unmeasured(where: str, text: str) -> list[str]:
    found = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if VERSION_FEATURE.search(sentence) or RUNTIME_STATE.search(sentence):
            continue
        found += [
            f"{where}: {m.group(0)!r} in {sentence.strip()[:160]!r}"
            for m in UNMEASURED.finditer(sentence)
        ]
    return found


def _runtime_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
        and isinstance(n.body[0].value.value, str)
    }
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            if n.value in MARKER_CONSTANTS:
                continue
            out.append((n.lineno, _code_text(n.value)))
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            joined = _joined_literals(n)
            if joined is not None:
                out.append((n.lineno, joined))
    return out


def _code_text(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return LITERAL_JOINT.sub("", "\n".join(lines))


def _joined_literals(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _joined_literals(node.left), _joined_literals(node.right)
        return None if left is None or right is None else left + right
    return None


def _scan_source(path: Path) -> list[str]:
    return [
        line
        for lineno, text in _runtime_strings(path)
        for line in _unmeasured(f"{path.name}:{lineno}", text)
    ]


def test_published_descriptions_have_no_date_and_no_run_narrative(registry: Registry) -> None:
    found = [line for where, text in _texts(registry) for line in _flagged(where, text)]
    assert not found, "\n".join(found)


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_changelog_and_docs_have_no_date_and_no_run_narrative(path: Path) -> None:
    found = _flagged(path.name, path.read_text(encoding="utf-8"))
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    "sample",
    [
        "measured 2026-10-18",
        "on 2026/10/18",
        "on 2026.10.18",
        "in 2026-10",
        "on Oct 18",
        "on October 18",
        "on 18 October",
        "the audio values were the same in three runs",
        "measured on Flame 2025.2.4, twice, identical",
        "in 2 runs",
        "with the same values",
        "the first red overall through an old judgment",
    ],
)
def test_the_checks_flag_known_bad_wording(sample: str) -> None:
    assert _flagged("sample", sample), sample


@pytest.mark.parametrize(
    "sample",
    [
        "Measured on Flame 2025.2.4 and 2026.2.3: a Gap",
        "Flame 2026.2.13 and 2025.2.4",
        "3 of 3 runs (two segmentation faults and one bus error)",
        "0 of 2 runs, each with 100 edits",
        "listed twice",
    ],
)
def test_the_checks_pass_flame_versions_and_measured_results(sample: str) -> None:
    assert not _flagged("sample", sample), sample


def test_descriptions_and_context_say_nothing_is_unmeasured(registry: Registry) -> None:
    found = [line for where, text in _texts(registry) for line in _unmeasured(where, text)]
    assert not found, "\n".join(found)


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_changelog_readme_and_docs_say_nothing_is_unmeasured(path: Path) -> None:
    found = _unmeasured(path.name, path.read_text(encoding="utf-8"))
    assert not found, "\n".join(found)


@pytest.mark.parametrize("path", SRC_FILES, ids=lambda p: p.name)
def test_strings_returned_to_callers_say_nothing_is_unmeasured(path: Path) -> None:
    found = _scan_source(path)
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    "sample",
    [
        "Moving a Gap with an effect was not measured.",
        "NOT measured, and refused before anything is copied",
        "Whether a revert puts an audio-only change back is not verified.",
        "result.inverse is a presumed inverse call (not measured)",
        "What it changes has not been measured, so this reports the count.",
        "Rebuilding an audio track was not tried.",
        "Not measured with an accepted clip on a segment of a reel clip.",
        "this says nothing about a revert of an audio change",
        "an unmeasured case",
        "the effect is unverified",
        "the result is not yet confirmed",
        "Linux has never been tried",
        "Moving it hasn't been measured.",
        "Moving it has yet to be measured.",
        "Moving it is yet to be measured.",
        "It has never been measured.",
        "X was not measured on other Flame versions.",
        "Starting on a version other than these two has not been tried.",
        "Other Flame versions have not been tried.",
    ],
)
def test_the_unmeasured_check_flags_known_bad_wording(sample: str) -> None:
    assert _unmeasured("sample", sample), sample


@pytest.mark.parametrize(
    "sample",
    [
        "flame_version_info reports untestedFlameVersion and adds a notice.",
        "The warning says the version is not a tested version.",
        "The bridge warns that the version is untested.",
        "the end of the render was not confirmed (no answer from the bridge for it)",
        "Measured on Flame 2025.2.4: a cut inside a Gap splits it in two.",
        "Refused: a sequence with two or more versions.",
        "result.verified is null when only Render nodes ran.",
        "The measured values were identical.",
    ],
)
def test_the_unmeasured_check_passes_the_version_feature_and_normal_text(sample: str) -> None:
    assert not _unmeasured("sample", sample), sample


SPLIT_LITERAL = (
    "raise E('PRECONDITION_UNMET', 'the move was not '\n    'measured: nothing was done')\n"
)
PSEUDO_SOURCES = {
    "split_literals": SPLIT_LITERAL,
    "plus_concatenated": "msg = 'the move was not ' + 'measured'\n",
    "split_inside_big_string": 'SRC = """\nraise E(\'the move was not \'\n  \'measured\')\n"""\n',
    "plus_inside_big_string": 'SRC = """\nraise E(\'the move was not \' + \'measured\')\n"""\n',
    "hasnt": 'msg = "moving it hasn\'t been measured"\n',
    "yet_to_be": "msg = 'moving it has yet to be measured'\n",
    "all_caps_message": "msg = 'NOT MEASURED'\n",
    "other_all_caps_constant": "MARKER = 'UNDO UNCONFIRMED BY ANYONE'\n",
}
CLEAN_SOURCES = {
    "plain_message": "msg = 'nothing was done'\n",
    "comment_in_string": 'SRC = """\n# was not measured\nx = 1\n"""\n',
    "python_comment": "# was not measured\nmsg = 'ok'\n",
    "docstring": 'def f():\n    """was not measured"""\n    return 1\n',
    "marker_constant": "UNDO_UNCONFIRMED = 'UNDO UNCONFIRMED'\n",
}


@pytest.mark.parametrize("name", sorted(PSEUDO_SOURCES))
def test_the_source_scan_flags_unmeasured_wording_in_a_written_file(
    tmp_path: Path, name: str
) -> None:
    path = tmp_path / f"{name}.py"
    path.write_text(PSEUDO_SOURCES[name], encoding="utf-8")
    assert _scan_source(path), name


@pytest.mark.parametrize("name", sorted(CLEAN_SOURCES))
def test_the_source_scan_passes_comments_docstrings_and_the_marker_constant(
    tmp_path: Path, name: str
) -> None:
    path = tmp_path / f"{name}.py"
    path.write_text(CLEAN_SOURCES[name], encoding="utf-8")
    assert not _scan_source(path), name
