"""Which clips of a Batch group's reels lost their Timeline FX in a restore.

A Batch setup (`save_setup`) holds no Timeline FX of the clips in the group's reels. Measured on
Flame 2025.2.4: a clip whose FX were changed or rendered while it was in the group comes back
from `load_setup` without them; a clip that was only copied in can lose them too (seen after its
source clip on the Desktop had been trimmed). A checkpoint records,
per clip of every schematic and shelf reel, the FX types (`checkpoint_prelude._clip_fx_read`);
right after a restore's `load_setup`, in the same Flame call, the same read is taken again. This
module is the server's half: it pairs the two reads and names the clips whose FX got fewer
(`result.timelineFxLost`).

Rows are paired by (reel kind, reel name, clip name, the clip's order among clips of that name in
that reel). A row whose FX could not be read, or that has no partner after the restore, is not
judged: it is listed in `result.timelineFxUnchecked`, so an empty `timelineFxLost` does not
hide it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

NO_RECORD_REASON: Final[str] = (
    "the checkpoint holds no record of the reel clips' Timeline FX (taken before checkpoints "
    "recorded them): which clips lost theirs was NOT checked"
)
READ_FAILED_REASON: Final[str] = (
    "the reel clips' Timeline FX could not be read after the load: which clips lost theirs was "
    "NOT checked"
)
UNREADABLE_REASON: Final[str] = "unreadable"
MISSING_REASON: Final[str] = "missing after the restore"


@dataclass(frozen=True)
class FxRecord:
    """What a checkpoint knows of its reel clips' Timeline FX: the rows, or why there are none."""

    rows: list[dict[str, Any]] | None
    reason: str | None


def record_of(checkpoint: Mapping[str, Any]) -> FxRecord:
    """The recorded clip FX rows of a checkpoint journal record."""
    rows = checkpoint.get("clipFx")
    if isinstance(rows, list) and all(isinstance(r, dict) for r in rows):
        return FxRecord([dict(r) for r in rows], None)
    error = checkpoint.get("clipFxError")
    if isinstance(error, str) and error:
        return FxRecord(None, f"{NO_RECORD_REASON} ({error})")
    return FxRecord(None, NO_RECORD_REASON)


def after_of(load_result: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
    """The clip FX rows the load call read after `load_setup`, None when it did not read them."""
    read = load_result.get("clipFx") if isinstance(load_result, Mapping) else None
    rows = read.get("rows") if isinstance(read, Mapping) else None
    if isinstance(rows, list) and all(isinstance(r, dict) for r in rows):
        return [dict(r) for r in rows]
    return None


def read_error_of(load_result: Mapping[str, Any] | None) -> str | None:
    """Why the read after `load_setup` gave no rows, when it said."""
    read = load_result.get("clipFx") if isinstance(load_result, Mapping) else None
    error = read.get("error") if isinstance(read, Mapping) else None
    return error if isinstance(error, str) and error else None


def _keyed(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[Any, ...], Mapping[str, Any]]:
    seen: Counter[tuple[Any, ...]] = Counter()
    out: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for row in rows:
        base = (row.get("reelKind"), row.get("reel"), row.get("clip"))
        out[(*base, seen[base])] = row
        seen[base] += 1
    return out


@dataclass(frozen=True)
class Comparison:
    lost: list[dict[str, Any]]
    """The clips whose FX types are fewer after the restore than in the record (some or all)."""
    unchecked: list[dict[str, Any]]
    """The recorded clips that could not be judged: `reason` is `unreadable` (either read has no
    FX list) or `missing after the restore` (no clip of that name and place came back)."""


def compare(
    recorded: Sequence[Mapping[str, Any]] | None, after: Sequence[Mapping[str, Any]] | None
) -> Comparison | None:
    """Pairs the record with the read after the restore; None when either side is missing."""
    if recorded is None or after is None:
        return None
    now = _keyed(after)
    lost: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    for key, row in _keyed(recorded).items():
        partner = now.get(key)
        before, left = row.get("effects"), partner.get("effects") if partner else None
        where = {"reelKind": key[0], "reel": key[1], "clip": key[2]}
        if not isinstance(before, list) or (partner is not None and not isinstance(left, list)):
            unchecked.append({**where, "reason": UNREADABLE_REASON})
        elif partner is None:
            unchecked.append({**where, "reason": MISSING_REASON})
        elif Counter(before) - Counter(left):
            lost.append({**where, "before": before, "after": left})
    return Comparison(lost, unchecked)


def note(entries: Sequence[Mapping[str, Any]]) -> str:
    """The warning for a restore that lost Timeline FX."""
    count = len(entries)
    return (
        "Timeline FX lost: the Batch setup does not contain the Timeline FX of the clips in the "
        "group's reels, and some clips came back from this restore without them "
        f"({count} clip{'s' if count != 1 else ''}, listed in "
        "result.timelineFxLost). Put the FX back by adding the original clip from the Desktop "
        "again (batch.add_clips)."
    )


def unchecked_note(entries: Sequence[Mapping[str, Any]]) -> str:
    """The warning for a restore after which some clips' Timeline FX could not be compared."""
    count = len(entries)
    return (
        f"Timeline FX of {count} clip{'s' if count != 1 else ''} could not be compared with the "
        "checkpoint (unreadable, or not back under the same name), so they may have lost theirs: "
        "see result.timelineFxUnchecked."
    )


def report(fx: FxRecord, load_result: Mapping[str, Any] | None) -> dict[str, Any]:
    """The result keys of a restore: `timelineFxLost` and `timelineFxUnchecked` (lists, or None
    when nothing could be compared) and, when None, `timelineFxNotCompared` (why)."""
    after = after_of(load_result)
    found = compare(fx.rows, after)
    if found is not None:
        return {"timelineFxLost": found.lost, "timelineFxUnchecked": found.unchecked}
    if fx.rows is None:
        reason = fx.reason
    elif load_result is None:
        reason = "load_setup did not complete, so the reel clips' Timeline FX were not read"
    else:
        reason = READ_FAILED_REASON
        error = read_error_of(load_result)
        if error:
            reason = f"{reason} ({error})"
    return {
        "timelineFxLost": None,
        "timelineFxUnchecked": None,
        "timelineFxNotCompared": reason,
    }


def warnings_of(report_: Mapping[str, Any]) -> list[str]:
    """The `result.warnings` lines a `report` calls for."""
    out: list[str] = []
    if report_.get("timelineFxLost"):
        out.append(note(report_["timelineFxLost"]))
    if report_.get("timelineFxUnchecked"):
        out.append(unchecked_note(report_["timelineFxUnchecked"]))
    return out
