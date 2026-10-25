"""A call that ended without an answer and has no journal intent.

A stash / checkpoint copy (`batch.checkpoint`, `timeline.checkpoint`, the copy `desktop.delete` and
`desktop.reformat` make before they write) and a `journal.purge_checkpoints` batch are not journal
writes: when one of them ends in TIMEOUT / BRIDGE_LOST / BRIDGE_UNAVAILABLE (or answers `pending`)
there is no intent for `journal.check` to decide, yet the call may still be running, or have
copied / deleted something. `shut_if_unanswered` shuts `WriteGuard` with a block that has no
intent; `journal.check` lifts it once Flame has been read again
(`timeline_purge.settle_unrecorded`).
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any

from flame_mcp.journaled_write import UNKNOWN_END_CODES

if TYPE_CHECKING:
    from flame_mcp.runtime import WriteGuard


LIFT_HINT = (
    "No journal intent covers this call, so writes are refused until journal.check (without a "
    "group) has read Flame again."
)


def unanswered(env: Mapping[str, Any]) -> bool:
    """True when `env` is the envelope of a call whose end is not known."""
    if env.get("ok") is True:
        return bool(env.get("pending"))
    return env.get("errorCode") in UNKNOWN_END_CODES


def shut_if_unanswered(
    guard: WriteGuard, env: MutableMapping[str, Any], op_name: str, group: str | None
) -> bool:
    """Shuts the guard (a block with no journal intent) when `env` did not end with an answer,
    and adds to its hint how to lift the block."""
    if not unanswered(env):
        return False
    guard.block_unrecorded(group, op_name, str(env.get("errorCode") or "TIMEOUT"))
    hint = env.get("hint")
    env["hint"] = f"{hint} {LIFT_HINT}" if isinstance(hint, str) and hint else LIFT_HINT
    return True
