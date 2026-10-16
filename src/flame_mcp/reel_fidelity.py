"""Putting a Batch group's schematic reels back after a restore.

`load_setup` restores the group's contents but not its reel layout: empty reels disappear,
Flame's default empty reels are added, and reels that hold clips are rebuilt with new uids. A
checkpoint therefore records the reel inventory (`checkpoint_prelude._reel_inventory`) and the
group's fingerprint over all sections; right after the restore's `load_setup`, in the same
Flame call (`checkpoint_prelude._tidy_reels`):

  (a) an empty reel the record has and the group lacks is created (`create_reel`; a name Flame
      changed is reported);
  (b) then an empty reel the record does not have is deleted (`flame.delete(reel, confirm=False)`).
      A reel that holds a clip or a sequence is never deleted, whatever the record says: deleting
      it deletes the Clip nodes that use it. The order is (a) then (b): the last reel of a group
      cannot be deleted without Flame creating its default ones;
  (c) the group's reel sections are read back (all sections, one fingerprint) and the schematic
      reels are compared with the recorded ones: `reelsRestored` looks at them only.

Everything here is an after-the-fact correction: nodes `load_setup` brought back stay as they
are whatever this does. This module is the server's half: which record can be tidied from, and
what the read-back means (`report`). Shelf reels and iterations are never restored, so they are
not part of `reelsRestored`; a difference there is reported apart, as `outsideTheCheckpoint`.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCHEMATIC_SECTION = "reels"
OUTSIDE_SECTIONS = {"shelf_reels": "shelfReels", "iterations": "iterations"}
"""The sections a restore does not bring back, and the result key of their differences."""

NO_INVENTORY_REASON = (
    "the checkpoint holds no reel inventory (taken before checkpoints recorded the reel "
    "inventory, or the reels could not be read when it was taken): the reels were NOT tidied"
)
NOT_A_CHECKPOINT_REASON = (
    "load_setup of a setup file, not of a checkpoint: the file's own reels are kept as Flame "
    "loaded them"
)


@dataclass(frozen=True)
class ReelRecord:
    """What a checkpoint knows of the group's reels."""

    rows: list[dict[str, Any]]
    fingerprint: str | None
    section_data: dict[str, Any] | None


def record_of(checkpoint: Mapping[str, Any]) -> tuple[ReelRecord | None, str | None]:
    """The reel record of a checkpoint journal record, or (None, why) when it has none."""
    rows = checkpoint.get("reels")
    if not isinstance(rows, list):
        error = checkpoint.get("reelsError")
        if isinstance(error, str) and error:
            return None, f"{NO_INVENTORY_REASON} ({error})"
        return None, NO_INVENTORY_REASON
    if not all(isinstance(r, dict) and isinstance(r.get("name"), str) for r in rows):
        return None, f"{NO_INVENTORY_REASON} (its reel rows are malformed)"
    fingerprint = checkpoint.get("fingerprintFull")
    data = checkpoint.get("sectionData")
    return ReelRecord(
        rows=[dict(r) for r in rows],
        fingerprint=fingerprint if isinstance(fingerprint, str) else None,
        section_data=data if isinstance(data, dict) else None,
    ), None


def _key(row: Any) -> str:
    return json.dumps(row, sort_keys=True)


def differences(
    recorded: Mapping[str, Any] | None, now: Mapping[str, Any] | None
) -> dict[str, dict[str, list[Any]]]:
    """Per section whose rows differ: the rows only the record has, and the rows only the group
    has now (the sections are sorted row lists, so this is a multiset difference)."""
    if not isinstance(recorded, Mapping) or not isinstance(now, Mapping):
        return {}
    out: dict[str, dict[str, list[Any]]] = {}
    for section in sorted(set(recorded) | set(now)):
        before, after = recorded.get(section), now.get(section)
        if not isinstance(before, list) or not isinstance(after, list):
            if before != after:
                out[section] = {"onlyRecorded": [before], "onlyNow": [after]}
            continue
        left = Counter(_key(r) for r in before)
        right = Counter(_key(r) for r in after)
        if left == right:
            continue
        out[section] = {
            "onlyRecorded": [json.loads(k) for k in (left - right).elements()],
            "onlyNow": [json.loads(k) for k in (right - left).elements()],
        }
    return out


def report(
    record: ReelRecord | None, skipped_reason: str | None, load_result: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The `reelTidy` entry of a restore's result. `load_result` is what the Flame call answered
    (None when the load did not complete). `reelsRestored` looks at the schematic reels only
    (names, clip and sequence names): True / False when the group's reels after the tidy could be
    compared with the recorded ones, else None. Shelf reels and iterations, which a restore never
    brings back, are not part of it: their differences are in `outsideTheCheckpoint`."""
    if record is None:
        return {"ran": False, "skipped": True, "reason": skipped_reason, "reelsRestored": None}
    tidy = load_result.get("reelTidy") if isinstance(load_result, Mapping) else None
    if not isinstance(tidy, Mapping):
        return {
            "ran": False,
            "skipped": False,
            "reason": "load_setup did not complete, so the reels were not tidied",
            "reelsRestored": None,
        }
    after = tidy.get("after") if isinstance(tidy.get("after"), Mapping) else None
    now_fp = after.get("fingerprint") if after is not None else None
    now_data = after.get("sectionData") if after is not None else None
    reel_diff = differences(
        _only(record.section_data, SCHEMATIC_SECTION), _only(now_data, SCHEMATIC_SECTION)
    )
    comparable = _has_reels(record.section_data) and _has_reels(now_data)
    restored: bool | None = None
    if comparable:
        restored = not reel_diff
    names_after = tidy.get("namesAfter")
    recorded_names = [r["name"] for r in record.rows]
    out: dict[str, Any] = {
        "ran": True,
        "skipped": False,
        "reelsRestored": restored,
        "created": tidy.get("created", []),
        "deleted": tidy.get("deleted", []),
        "kept": tidy.get("kept", []),
        "errors": tidy.get("errors", []),
        "recordedReels": recorded_names,
        "reelsAfter": names_after,
        "orderMatches": names_after == recorded_names if isinstance(names_after, list) else None,
        "fingerprintRecorded": record.fingerprint,
        "fingerprintAfter": now_fp,
        "outsideTheCheckpoint": _outside(record.section_data, now_data),
    }
    if restored is None:
        out["comparison"] = "not compared: " + (
            "the checkpoint holds no reel section data"
            if not _has_reels(record.section_data)
            else "the group could not be read back after the tidy"
        )
    if restored is False:
        out["differences"] = reel_diff
    if after is not None:
        out["unreadableSections"] = after.get("unreadableSections")
    return out


def _only(data: Any, section: str) -> dict[str, Any] | None:
    if isinstance(data, Mapping) and section in data:
        return {section: data[section]}
    return None


def _has_reels(data: Any) -> bool:
    return isinstance(data, Mapping) and isinstance(data.get(SCHEMATIC_SECTION), list)


def _outside(recorded: Any, now: Any) -> dict[str, Any]:
    """Differences in the parts a restore does not bring back (shelf reels, iterations)."""
    out: dict[str, Any] = {}
    for section, key in OUTSIDE_SECTIONS.items():
        diff = differences(_only(recorded, section), _only(now, section))
        if diff:
            out[key] = diff[section]
    return out
