"""docs/ops.md is generated from the registry; it must match what the generator produces now."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"


def _generator() -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module("build_ops_index")


def test_ops_index_is_up_to_date() -> None:
    generator = _generator()
    expected = generator.render(generator._build_registry())
    actual = (ROOT / "docs" / "ops.md").read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/ops.md is stale: run `.venv/bin/python scripts/build_ops_index.py`"
    )


def test_the_index_shows_each_summary_sentence_whole() -> None:
    generator = _generator()
    rendered = generator.render(generator._build_registry())
    rows = [line for line in rendered.splitlines() if line.startswith("| `")]
    assert len(rows) == 93
    assert not [row for row in rows if row.endswith("… |")]
