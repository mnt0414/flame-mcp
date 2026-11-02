"""Judge the colour-space scan of a sequence (`colourspace_prelude._cs_scan`).

Pure Python, no Flame. A sequence is `mixed` only when two or more different colour spaces were
read. A value that could not be read is never counted as a colour space and never makes a sequence
mixed: it makes the scan incomplete and is named in `warnings`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def judge(scan: Any) -> dict[str, Any]:
    """`{mixed, complete, spaces [{name, count, first}], segments, unreadable, warnings}`.

    `mixed` is True when two or more colour spaces were read, False when at most one was, and
    the scan being incomplete is a warning, not a reason for True."""
    if not isinstance(scan, Mapping):
        return {
            "mixed": False,
            "complete": False,
            "spaces": [],
            "segments": 0,
            "unreadable": 0,
            "warnings": ["the colour spaces of the sequence could not be read"],
        }
    raw = scan.get("spaces")
    spaces = [
        {"name": name, "count": entry.get("count"), "first": entry.get("first")}
        for name, entry in (raw.items() if isinstance(raw, Mapping) else [])
        if isinstance(entry, Mapping)
    ]
    spaces.sort(key=lambda s: (-(s["count"] or 0), s["name"]))
    unreadable_raw = scan.get("unreadable")
    unreadable = int(unreadable_raw.get("count") or 0) if isinstance(unreadable_raw, Mapping) else 0
    warnings: list[str] = []
    complete = scan.get("complete") is True
    if scan.get("error"):
        warnings.append(f"the scan stopped on an error: {scan['error']}")
    elif not complete:
        warnings.append("the scan stopped at its time limit, so segments were not read")
    if unreadable:
        warnings.append(
            f"{unreadable} segment(s) gave no readable colour space and are not counted"
        )
    if not spaces and not unreadable:
        warnings.append("no segment with a colour space was found")
    return {
        "mixed": len(spaces) >= 2,
        "complete": complete and not unreadable,
        "spaces": spaces,
        "segments": int(scan.get("segments") or 0),
        "unreadable": unreadable,
        "warnings": warnings,
    }


def mixed_message(verdict: Mapping[str, Any]) -> str:
    names = ", ".join(f"{s['name']!r} x{s['count']}" for s in verdict["spaces"])
    return (
        f"nothing was exported: the sequence has segments in {len(verdict['spaces'])} different "
        f"colour spaces ({names})"
    )


def incomplete_message(verdict: Mapping[str, Any]) -> str:
    return (
        "nothing was exported: the colour spaces of the sequence could not all be read "
        f"({'; '.join(verdict['warnings'])})"
    )


def problem_of(verdict: Mapping[str, Any]) -> str | None:
    """Why a sequence is not exported without `skip_colour_check`, else None."""
    if verdict["mixed"]:
        return mixed_message(verdict)
    if not verdict["complete"]:
        return incomplete_message(verdict)
    return None
