"""The `partial:` prefix the server looks for is the one the Flame-side preludes write, and the
three run checks (render, export, still) share one verdict rule."""

from __future__ import annotations

import pytest

from flame_mcp import errors
from flame_mcp.desktop_reformat_prelude import REFORMAT_HELPER
from flame_mcp.journal import STATE_FAILED, STATE_LANDED, STATE_NOT_LANDED, STATE_PARTIAL
from flame_mcp.node_prelude import NODE_HELPER
from flame_mcp.render_check import verdict_after_run


def test_the_prefix_is_written_by_the_preludes() -> None:
    assert f"'{errors.PARTIAL_PREFIX} " in REFORMAT_HELPER
    assert f"'{errors.PARTIAL_PREFIX} " in NODE_HELPER


def test_a_partial_message_is_recognised_and_stripped() -> None:
    assert errors.is_partial_message("partial: 1 of 2")
    assert not errors.is_partial_message("failed: partial: x")
    assert not errors.is_partial_message(None)
    assert errors.without_partial_prefix("partial: 1 of 2") == "1 of 2"
    assert errors.without_partial_prefix("other") == "other"


@pytest.mark.parametrize(
    ("ok", "code", "wrote_part", "expected"),
    [
        (True, None, False, STATE_LANDED),
        (True, None, True, STATE_LANDED),
        (False, "FLAME_API_ERROR", True, STATE_PARTIAL),
        (False, "SILENT_NOOP", False, STATE_NOT_LANDED),
        (False, "FLAME_API_ERROR", False, STATE_FAILED),
        (False, None, False, STATE_FAILED),
    ],
)
def test_the_run_verdict_follows_ok_then_partial_then_silent_noop(
    ok: bool, code: str | None, wrote_part: bool, expected: str
) -> None:
    assert verdict_after_run(ok=ok, code=code, wrote_part=wrote_part) == expected
