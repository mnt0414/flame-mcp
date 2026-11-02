"""Which clips `desktop.sync_record_tc` may write, decided from facts read in Flame.

Pure Python, no Flame. The rule it follows is flame-tc-sync's: a clip of exactly one segment gets
its Record TC (the segment's `record_in`) set to its Source TC (the segment's `source_in`). What
that tool was never checked against is turned into a `skipped` row with a reason, never written
silently: footage that has no TC, DPX (the header TC is not read by anything here), a gap before
the segment, more than one version or segment, and a frame rate that differs between clip and
segment. `include_risky=true` writes the DPX / no-TC rows too."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

STATUS_SYNC: Final[str] = "sync"
STATUS_IN_SYNC: Final[str] = "in_sync"
STATUS_SKIPPED: Final[str] = "skipped"

RISKY_EXTENSIONS: Final[frozenset[str]] = frozenset({"dpx"})
"""Source files whose TC (a header field) neither flame-tc-sync nor this op has been checked on."""

_RATE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")
RATE_TOLERANCE: Final[float] = 0.001


def parse_rate(value: Any) -> float | None:
    """`'23.976 fps'` / `'24'` / 24 -> 23.976 / 24.0 / 24.0, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _RATE.match(value)
        if match:
            return float(match.group(1))
    return None


def rates_agree(clip_rate: Any, source_rate: Any) -> bool | None:
    """True / False when both rates read; None when either does not."""
    a, b = parse_rate(clip_rate), parse_rate(source_rate)
    if a is None or b is None:
        return None
    return abs(a - b) <= RATE_TOLERANCE


def _row(
    status: str, reason: str | None, facts: Mapping[str, Any], note: str = ""
) -> dict[str, Any]:
    return {"status": status, "reason": reason, "note": note or None}


def _structure(facts: Mapping[str, Any]) -> dict[str, Any] | None:
    if facts.get("type") != "PyClip":
        return _row(STATUS_SKIPPED, "not_a_clip", facts, f"the target is a {facts.get('type')}")
    location = facts.get("location")
    if location == "library":
        return _row(STATUS_SKIPPED, "in_library", facts, "library contents are never written")
    if location != "desktop":
        return _row(STATUS_SKIPPED, "not_on_desktop", facts, f"the clip is in a {location!r} place")
    if facts.get("versionCount") != 1:
        return _row(
            STATUS_SKIPPED, "multiple_versions", facts, "only a clip of one version is judged"
        )
    count = facts.get("segmentCount")
    if count == 0:
        return _row(STATUS_SKIPPED, "no_segment", facts)
    if count != 1:
        if facts.get("firstSegmentType") == "Gap":
            return _row(STATUS_SKIPPED, "gap_at_head", facts, "a Gap comes before the media")
        return _row(STATUS_SKIPPED, "multi_segment", facts, f"{count} segments")
    return None


def judge(facts: Mapping[str, Any], *, include_risky: bool = False) -> dict[str, Any]:
    """`{status, reason, note, risky}` for one clip's facts (see `tc_prelude._rtc_facts`)."""
    refused = _structure(facts)
    if refused is not None:
        return {**refused, "risky": False}
    record, source = facts.get("recordInFrame"), facts.get("sourceInFrame")
    if not isinstance(record, int) or not isinstance(source, int):
        return {
            **_row(STATUS_SKIPPED, "unreadable", facts, "record_in / source_in did not read"),
            "risky": False,
        }
    agree = rates_agree(facts.get("frameRate"), facts.get("sourceFrameRate"))
    if agree is None:
        return {
            **_row(STATUS_SKIPPED, "frame_rate_unreadable", facts, "a frame rate did not read"),
            "risky": False,
        }
    if not agree:
        return {
            **_row(
                STATUS_SKIPPED,
                "frame_rate_mismatch",
                facts,
                f"clip {facts.get('frameRate')!r}, source {facts.get('sourceFrameRate')!r}",
            ),
            "risky": False,
        }
    if record == source:
        return {**_row(STATUS_IN_SYNC, None, facts), "risky": False}
    reason = _risky_reason(facts, source)
    if reason is not None and not include_risky:
        return {
            **_row(STATUS_SKIPPED, reason[0], facts, reason[1]),
            "risky": True,
        }
    return {
        **_row(STATUS_SYNC, None, facts, reason[1] if reason else ""),
        "risky": reason is not None,
    }


def _risky_reason(facts: Mapping[str, Any], source: int) -> tuple[str, str] | None:
    ext = facts.get("sourceExtension")
    if isinstance(ext, str) and ext.lower() in RISKY_EXTENSIONS:
        return "dpx_source", "a DPX source: its header TC has not been checked"
    start = facts.get("startFrame")
    if source == 0 and isinstance(start, int) and start not in (0, 1):
        return "no_source_tc", "source_in is 0 while the media starts at a later frame"
    return None
