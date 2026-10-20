"""`desktop.reformat`: the constants, the notes and the state one call carries.

`Run` is what the pieces of a call share (`desktop_reformat_run` reads and stashes,
`desktop_reformat_clips` does the clips one by one, `desktop_reformat_answer` builds the envelope).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from flame_mcp.registry import RegisteredOperation

OP_NAME: Final[str] = "desktop.reformat"
PHASE_DUP: Final[str] = "dup"
PHASE_OBSERVE: Final[str] = "observe"
SPEC_ARG: Final[str] = "_spec"
"""Reserved args keys / phases only this module and its revert put into the op's args (a caller
cannot send them: `validate_op_args` rejects an unknown key)."""
REFORMAT_WAIT_S: Final[float] = 180.0
"""How long the server waits for ONE clip's Flame call."""
STASH_BATCH: Final[int] = 10
"""Most clips one Flame call copies into the stash."""
RATIO_TOLERANCE: Final[float] = 0.001
"""Most two ratios may differ and still count as the same: a requested width / height against a
clip's `PyClip.ratio` (also in the Flame code of `desktop_reformat_prelude`), and a clip's read-back
ratio against the one recorded."""

IN_PLACE: Final[str] = "in_place"
DUPLICATE: Final[str] = "duplicate"

IN_PLACE_NOTE: Final[str] = (
    "Each clip that needed a write was copied into the stash reel before it was reformatted "
    "(result.reformatted[].checkpoint, stash). journal.revert(group) restores the stashed copy: "
    "the restored clip has a NEW uid and wiretap id and is at the END of its reel (its old "
    "place is not restored). The stashed copies stay in the checkpoint library until "
    "journal.purge_checkpoints removes them."
)
DUPLICATE_NOTE: Final[str] = (
    "The reformatted clips are COPIES made with flame.duplicate (result.reformatted[].to); the "
    "originals were not touched. journal.revert(group) deletes the copies."
)
UNCHANGED_NOTE: Final[str] = (
    "Every clip already had the requested format, so nothing was reformatted and nothing was "
    "changed."
)
UNKNOWN_HINT: Final[str] = (
    "It is not known whether the clip was reformatted: this server now refuses every write until "
    "journal.check (group={group!r}) has read Flame again. The clips after it were not started."
)


@dataclass
class Run:
    rt: Any
    op: RegisteredOperation
    op_args: dict[str, Any]
    group: str
    rows: list[dict[str, Any]]
    spec: dict[str, Any]
    requested: dict[str, Any]
    duplicate: bool
    stashed: dict[int, dict[str, Any]] = field(default_factory=dict)
    """The stash row and checkpoint id of each clip that was copied, by index."""
    siblings: dict[Any, list[Any]] = field(default_factory=dict)
    reformatted: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    intent_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    created_any: bool = False
    changed: int = 0
    last_env: dict[str, Any] | None = None
    next_index: int = 0
    stop: dict[str, Any] | None = None
    """Why the run ended early: {code, message, intent, index, pending?} or {code, message}."""


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def build_spec(op_args: Mapping[str, Any]) -> dict[str, Any]:
    """What Flame is asked to do: the `PyClip.reformat` keyword arguments and the start frame."""
    width, height = int(op_args["width"]), int(op_args["height"])
    ratio = op_args.get("ratio")
    kwargs: dict[str, Any] = {
        "width": width,
        "height": height,
        "ratio": float(ratio) if ratio is not None else round(width / height, 6),
        "resize_mode": str(op_args.get("resize_mode") or "Fill"),
    }
    if op_args.get("bit_depth") is not None:
        kwargs["bit_depth"] = int(op_args["bit_depth"])
    if op_args.get("scan_mode"):
        kwargs["scan_mode"] = str(op_args["scan_mode"])
    if op_args.get("frame_rate"):
        kwargs["frame_rate"] = str(op_args["frame_rate"])
    start = op_args.get("start_frame")
    return {
        "reformat": kwargs,
        "start_frame": int(start) if start is not None else None,
        "use_segment_connections": bool(op_args.get("use_segment_connections", True)),
    }


def requested_of(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The request as the journal keeps it (and `journal.check` compares a clip with)."""
    requested = dict(spec["reformat"])
    if spec.get("start_frame") is not None:
        requested["start_frame"] = spec["start_frame"]
        requested["use_segment_connections"] = spec["use_segment_connections"]
    return requested


def source_ref(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "parent": row.get("parent"),
        "name": row.get("name"),
        "type": row.get("type"),
        "uid": row.get("uid"),
        "wiretapId": row.get("wiretapId"),
        "selector": row.get("selector"),
    }


def ident(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    return {
        "name": entry.get("name"),
        "type": entry.get("type"),
        "uid": entry.get("uid"),
        "wiretapId": entry.get("wiretapId"),
        "selector": entry.get("selector"),
    }
