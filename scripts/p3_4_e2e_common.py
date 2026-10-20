"""Shared parts of the construction e2e (scripts/p3_4_construct_e2e.py): the calls made through
the MCP surface (one table the run and `--dry-run` read), the transport scaffolding -- which READS
STATE BACK INDEPENDENTLY of the ops under test -- and the material helpers. The stages are in
p3_4_e2e_build.py (overwrite, insert, cut, versions and tracks) and p3_4_e2e_create.py
(create_sequence, plan.run, the read-only policy); the driver, the cleanup and `--dry-run` are in
p3_4_construct_e2e.py."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p2_2a_journal_e2e import Ctx, do  # noqa: E402
from p3_1a_read_e2e import UID_CODE, fill, is_error, tx_val  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    DISPLAYED_CODE,
    OPEN_CODE,
    S1_PUTS,
    names_of,
    tx_children,
    tx_displayed,
    uids_of,
    written_groups,
)
from p3_3_e2e_common import (  # noqa: E402
    OUTPUT_DIR,
    Results,
    check_group,
    ckpt_library,
    err_text,
    expect_error,
    fingerprint,
    groups_now,
    intent_of,
    make_seq,
    outcome_of,
    prepare,
    restored_wt,
    revert,
    sel,
    stash_uids,
    state_snapshot,
    track_rows,
    tx_stash,
    tx_struct,
)

PREFIX: Final[str] = "P34E"
FRAME_RATES: Final[tuple[str, ...]] = (
    "23.976 fps",
    "24 fps",
    "25 fps",
    "29.97 fps DF",
    "29.97 fps NDF",
    "30 fps",
    "50 fps",
    "59.94 fps DF",
    "59.94 fps NDF",
    "60 fps",
)
START_TC: Final[str] = "01:00:00:00"
START_FRAMES: Final[dict[str, int]] = {"23.976 fps": 86400, "25 fps": 90000}
"""The absolute frame of 01:00:00:00 at the rates this run uses."""

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def declare(call_id: str, op: str, sample: dict[str, Any]) -> None:
    """`CALLS[call_id] = (op, build)`: `build(ids)` answers `ids["args"]` when a stage supplies the
    arguments and `sample` (a well-formed set, for `--dry-run`) otherwise."""

    def build(ids: dict[str, Any]) -> dict[str, Any]:
        args = ids.get("args")
        return dict(args) if isinstance(args, dict) else dict(sample)

    CALLS[call_id] = (op, build)


_SEQ = sel("/wt/s")
_CLIP = sel("/wt/c")
_REEL = sel("/wt/r")
_NEW = {
    "parent": _REEL,
    "name": "P34E_x",
    "frame_rate": "25 fps",
    "width": 1920,
    "height": 1080,
}

declare("o_overwrite", "timeline.overwrite", {"sequence": _SEQ, "source": _CLIP, "at": 30})
declare("o_overwrite_refused", "timeline.overwrite", {"sequence": _SEQ, "source": _CLIP, "at": 0})
declare("i_insert", "timeline.insert", {"sequence": _SEQ, "source": _CLIP, "at": 30})
declare("i_insert_refused", "timeline.insert", {"sequence": _SEQ, "source": _CLIP, "at": 0})
declare("k_cut", "timeline.cut", {"sequence": _SEQ, "at": 10, "track": 0})
declare("k_cut_all", "timeline.cut", {"sequence": _SEQ, "at": 40, "scope": "all"})
declare("k_cut_gap", "timeline.cut", {"sequence": _SEQ, "at": 25, "track": 0})
declare("k_cut_refused", "timeline.cut", {"sequence": _SEQ, "at": 0, "track": 0})
declare("v_create_version", "timeline.create_version", {"sequence": _SEQ})
declare("v_create_track", "timeline.create_track", {"sequence": _SEQ})
declare("c_create", "timeline.create_sequence", {**_NEW, "start_tc": START_TC, "video_tracks": 2})
declare("c_create_refused", "timeline.create_sequence", dict(_NEW))
declare(
    "p_plan", "plan.run", {"steps": [{"op": "timeline.create_track", "args": {"sequence": _SEQ}}]}
)
declare(
    "p_plan_with_create",
    "plan.run",
    {"steps": [{"op": "timeline.create_sequence", "args": dict(_NEW)}]},
)
declare("y_readonly_overwrite", "timeline.overwrite", {"sequence": _SEQ, "source": _CLIP, "at": 1})
declare("y_readonly_insert", "timeline.insert", {"sequence": _SEQ, "source": _CLIP, "at": 1})
declare("y_readonly_cut", "timeline.cut", {"sequence": _SEQ, "at": 10, "track": 0})
declare("y_readonly_create_version", "timeline.create_version", {"sequence": _SEQ})
declare("y_readonly_create_track", "timeline.create_track", {"sequence": _SEQ})
declare("y_readonly_create_sequence", "timeline.create_sequence", dict(_NEW))


async def run_call(
    session: Any, ctx: Ctx, call_id: str, args: dict[str, Any], *, track: bool = True
) -> dict[str, Any]:
    """The call `call_id` of the table with `args` (what the stage built)."""
    op, _build = CALLS[call_id]
    env = await do(session, ctx, op, args)
    group = (env.get("journal") or {}).get("group")
    if track and isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


# -- transport scaffolding (never through the op under test) ------------------------------------

SEQ_FACTS_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "def _fr(_x):",
        "    _x = _v(_x)",
        "    return _x.frame if type(_x).__name__ == 'PyTime' else _x",
        "if _o is None:",
        "    _result = None",
        "else:",
        "    _vers = list(_o.versions)",
        "    _result = {",
        "        'type': type(_o).__name__, 'name': _v(_o.name), 'uid': _v(_o.uid),",
        "        'frameRate': _g(lambda: _o.frame_rate), 'width': _g(lambda: _o.width),",
        "        'height': _g(lambda: _o.height),",
        "        'startFrame': _g(lambda: _fr(_o.start_frame)),",
        "        'startTime': _g(lambda: _fr(_o.start_time)),",
        "        'duration': _g(lambda: _fr(_o.duration)),",
        "        'tracks': [len(list(_ve.tracks)) for _ve in _vers],",
        "        'parent': _g(lambda: _v(_o.parent.name)),",
        "    }",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "seq_facts": SEQ_FACTS_CODE,
    "displayed": DISPLAYED_CODE,
    "open": OPEN_CODE,
    "uid": UID_CODE,
}


async def tx_seq(ctx: Ctx, wiretap_id: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(SEQ_FACTS_CODE, WT=wiretap_id), "p34e_seq_facts")
    return None if is_error(got) or not isinstance(got, dict) else got


async def tx_exists(ctx: Ctx, uid: str) -> bool:
    got = await tx_val(ctx, fill(UID_CODE, UID=uid), "p34e_find_uid")
    return not is_error(got) and got is not None


async def tx_open(ctx: Ctx, wiretap_id: str) -> Any:
    return await tx_val(ctx, fill(OPEN_CODE, WT=wiretap_id), "p34e_open")


def lay(struct: Any, track: int, version: int = 0) -> list[list[Any]]:
    """[type, record_in, record_out, source_name] of every segment of one track, as transport read
    them (the shape `result.*.layout` of the ops uses)."""
    return [[r["type"], r["rin"], r["rout"], r["src"]] for r in track_rows(struct, track, version)]


def media_rows(struct: Any, track: int, version: int = 0) -> list[list[Any]]:
    return [r for r in lay(struct, track, version) if r[0] != "Gap"]


def layout_matches(op_layout: Any, struct: Any, track: int, version: int = 0) -> bool:
    """The op's own `layout` (a list of [type, recordIn, recordOut, sourceName]) against what
    transport read (equal to it, or a part of it when the op listed a window only)."""
    if not isinstance(op_layout, list):
        return False
    mine = [list(r) for r in op_layout]
    theirs = lay(struct, track, version)
    return any(theirs[i : i + len(mine)] == mine for i in range(len(theirs) - len(mine) + 1))


def new_tracks(struct: Any) -> list[int]:
    """How many tracks each version has."""
    return [len(v) for v in struct or []]


async def make_s(ctx: Ctx, tag: str) -> dict[str, Any] | None:
    """A fresh sequence of this run (`P34E_<ts>_<tag>`): track 0 = clip1 0-23, a Gap, clip2 48-71,
    p2_len48 72-119; track 1 = p2_len72 0-71 (the material `S1_PUTS` puts)."""
    return await make_seq(ctx, tag, S1_PUTS)


__all__ = [
    "CALLS",
    "FRAME_RATES",
    "OUTPUT_DIR",
    "PREFIX",
    "START_FRAMES",
    "START_TC",
    "TEMPLATES",
    "Ctx",
    "Results",
    "check_group",
    "ckpt_library",
    "err_text",
    "expect_error",
    "fingerprint",
    "groups_now",
    "intent_of",
    "lay",
    "layout_matches",
    "make_s",
    "media_rows",
    "names_of",
    "new_tracks",
    "outcome_of",
    "prepare",
    "restored_wt",
    "revert",
    "run_call",
    "sel",
    "stash_uids",
    "state_snapshot",
    "track_rows",
    "tx_children",
    "tx_displayed",
    "tx_exists",
    "tx_open",
    "tx_seq",
    "tx_stash",
    "tx_struct",
    "uids_of",
]
