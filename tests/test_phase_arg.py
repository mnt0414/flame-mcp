from __future__ import annotations

import re
from pathlib import Path

from flame_mcp.types import PHASE_ARG

_SRC = Path(__file__).resolve().parent.parent / "src" / "flame_mcp"


def test_phase_arg_is_defined_in_one_place() -> None:
    defined = [
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if re.search(r"^PHASE_ARG\b.*=", path.read_text(), re.MULTILINE)
    ]
    assert defined == ["types.py"]
    assert PHASE_ARG == "_phase"
