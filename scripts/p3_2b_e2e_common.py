"""Shared parts of the plan / delete real-Flame e2e (scripts/p3_2b_plan_delete_e2e.py): the calls
made through the MCP surface, the transport scaffolding (which READS STATE BACK INDEPENDENTLY of the
ops under test) and the helpers the stages use. The material (`prepare`: reel group, library, clips,
S1, S2, S3 ...) is the one of p3_2a_e2e_common.py under the prefix `P32B`; the stages are in
p3_2b_e2e_plan.py (plan.run's bundle) and p3_2b_e2e_delete.py (desktop.delete, the move's display,
the purge)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, do, rec  # noqa: E402
from p3_1a_read_e2e import (  # noqa: E402
    COPY_CODE,
    REEL_CLIPS_CODE,
    STRUCT_CODE,
    UID_CODE,
    fill,
    is_error,
    tx_val,
)
from p3_1b_e2e_common import (  # noqa: E402
    BATCH_CODE,
    CLEANUP_CODE,
    DELETE_WT_CODE,
    LEFT_CODE,
    MAKE_REEL_CODE,
)
from p3_2a_e2e_common import (  # noqa: E402
    DELETE_UID_CODE,
    OPEN_CODE,
    STASH_CODE,
    Results,
    call,
    check_group,
    checkpoint_records,
    ckpt_library,
    copy_into,
    err_text,
    expect_error,
    fingerprint,
    groups_now,
    intent_of,
    media_row,
    name_of,
    names_of,
    outcome_of,
    reel_name_today,
    revert,
    seg_at,
    sel,
    stash_uids,
    synthetic_open_intent,
    track_rows,
    tx_children,
    tx_displayed,
    tx_find_uid,
    tx_stash,
    tx_struct,
    uids_of,
    written_groups,
)

__all__ = [
    "BATCH_CODE",
    "CALLS",
    "CLEANUP_CODE",
    "DELETE_UID_CODE",
    "DELETE_WT_CODE",
    "LEFT_CODE",
    "MAKE_REEL_CODE",
    "OPEN_CODE",
    "PREFIX",
    "TEMPLATES",
    "Results",
    "call",
    "check_group",
    "checkpoint_records",
    "ckpt_library",
    "copy_into",
    "err_text",
    "expect_error",
    "fingerprint",
    "groups_now",
    "intent_of",
    "media_row",
    "name_of",
    "names_of",
    "outcome_of",
    "reel_name_today",
    "revert",
    "run_call",
    "sel",
    "stash_uids",
    "synthetic_open_intent",
    "track_rows",
    "tx_children",
    "tx_comment",
    "tx_displayed",
    "tx_find_uid",
    "tx_name",
    "tx_stash",
    "tx_struct",
    "uids_of",
]

PREFIX: Final[str] = "P32B"
TAIL_TRIM: Final[int] = 3
HEAD_TRIM: Final[int] = 3
COMMENT_ATTR: Final[str] = "comment"
COMMENT: Any = "p32b comment"
"""The segment attribute the plan's set_attributes step writes (the in-process test swaps in one the
fake has) and the value."""

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one)

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def call_for(call_id: str, op: str) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Any]:
    def register(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


PLACEHOLDER_PURGE_TOKEN: Final[str] = "0" * 32
"""The token a --dry-run listing shows for a real purge; a run passes the dry run's own."""


def case(ids: dict[str, Any]) -> dict[str, Any]:
    """The case a call is built for (`run_call(..., case={...})`); a placeholder for --dry-run."""
    return ids.get("case") or {
        "wt": "/wt/x",
        "rin": 10,
        "rins": [10, 40, 70],
        "otherWt": "/wt/y",
        "destWt": "/wt/z",
        "otherRin": 0,
        "bogus": 999999,
        "targets": ["/wt/a", "/wt/b"],
        "checkpoint": "c_x",
        "group": "g_x",
    }


def trim_step(
    wt: str,
    record_in: int,
    edge: str = "tail",
    offset: int = TAIL_TRIM,
    *,
    track: int = 0,
    expect: str | None = None,
    late: bool = False,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "op": "segment.trim",
        "args": {
            "target": seg_at(sel(wt), track, record_in, expect=expect),
            "edge": edge,
            "offset": offset,
        },
    }
    if late:
        step["late"] = True
    return step


@call_for("p1_three_trims", "plan.run")
def _p1(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    srcs = c.get("srcs") or [None] * len(c["rins"])
    steps = [trim_step(c["wt"], r, expect=s) for r, s in zip(c["rins"], srcs, strict=True)]
    return {"steps": steps, "label": "p32b three trims"}


@call_for("p2_one_record_in_does_not_exist", "plan.run")
def _p2(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    return {"steps": [trim_step(c["wt"], c["rins"][0]), trim_step(c["wt"], c["bogus"])]}


@call_for("p3_wrong_expected_source_name", "plan.run")
def _p3(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    steps = [trim_step(c["wt"], c["rins"][0]), trim_step(c["wt"], c["rin"], expect="no-such-src")]
    return {"steps": steps}


@call_for("p4_head_then_tail", "plan.run")
def _p4(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    steps = [
        trim_step(c["wt"], c["rin"], "head", HEAD_TRIM),
        trim_step(c["wt"], c["rin"] + HEAD_TRIM, "tail", 2, late=bool(c.get("late"))),
    ]
    return {"steps": steps, "label": "p32b head then tail"}


@call_for("p5_late_step_that_does_not_resolve", "plan.run")
def _p5(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    steps = [
        trim_step(c["wt"], c["rin"], "head", HEAD_TRIM),
        trim_step(c["wt"], c["bogus"], late=True),
        trim_step(c["wt"], c["rins"][0]),
    ]
    return {"steps": steps}


@call_for("p6_trim_of_another_sequence", "plan.run")
def _p6(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    return {
        "steps": [
            trim_step(c["wt"], c["rin"]),
            trim_step(c["otherWt"], c["otherRin"], "tail", 1),
        ]
    }


@call_for("p7_rename_in_the_plan", "plan.run")
def _p7(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    rename = {"op": "desktop.rename", "args": {"target": sel(c["otherWt"]), "name": "p32b_x"}}
    return {"steps": [trim_step(c["wt"], c["rin"]), rename]}


@call_for("p8_comment_and_trim", "plan.run")
def _p8(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    attrs = {
        "op": "segment.set_attributes",
        "args": {"target": seg_at(sel(c["wt"]), 0, c["rin"]), "values": {COMMENT_ATTR: COMMENT}},
    }
    return {"steps": [attrs, trim_step(c["wt"], c["rin"])]}


@call_for("d_delete", "desktop.delete")
def _d(ids: dict[str, Any]) -> dict[str, Any]:
    return {"targets": [sel(w) for w in case(ids)["targets"]]}


@call_for("d_delete_twice", "desktop.delete")
def _d2(ids: dict[str, Any]) -> dict[str, Any]:
    wt = case(ids)["targets"][0]
    return {"targets": [sel(wt), sel(wt)]}


@call_for("d_plan_with_delete", "plan.run")
def _d3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"steps": [{"op": "desktop.delete", "args": {"targets": [sel(case(ids)["wt"])]}}]}


@call_for("m_move", "desktop.move")
def _m(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    return {"sources": [sel(c["wt"])], "destination": sel(c["destWt"])}


@call_for("g_purge_dry_run_by_default", "journal.purge_checkpoints")
def _g1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"older_than_days": 0}


@call_for("g_purge_real", "journal.purge_checkpoints")
def _g2(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "older_than_days": 0,
        "dry_run": False,
        "token": case(ids).get("token") or PLACEHOLDER_PURGE_TOKEN,
    }


@call_for("g_restore_a_purged_checkpoint", "journal.restore_checkpoint")
def _g3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"checkpoint": case(ids)["checkpoint"]}


@call_for("g_checkpoint", "timeline.checkpoint")
def _g4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": sel(case(ids)["wt"]), "label": "p32b purge"}


@call_for("r_readonly_delete", "desktop.delete")
def _r1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"targets": [sel(case(ids)["wt"])]}


@call_for("r_readonly_purge", "journal.purge_checkpoints")
def _r2(ids: dict[str, Any]) -> dict[str, Any]:
    return {}


@call_for("r_readonly_plan_trim", "plan.run")
def _r3(ids: dict[str, Any]) -> dict[str, Any]:
    c = case(ids)
    return {"steps": [trim_step(c["wt"], c["rin"])]}


# -- transport scaffolding (never through the op under test)

NAME_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else _v(_o.name)",
    ]
)

COMMENT_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "_result = _g(lambda: _v(getattr(_hit[0], @ATTR@)))",
    ]
)

PLANT_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_lib = [_l for _l in _ws.libraries if _v(_l.name) == @LIB@][0]",
        "_reel = list(_lib.reels)[0]",
        "_made = flame.media_panel.copy(flame.find_by_wiretap_node_id(@SRC@), _reel)[0]",
        "_result = {'uid': _v(_made.uid), 'wt': _made.get_wiretap_node_id(),",
        "           'name': _v(_made.name)}",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "name": NAME_CODE,
    "comment": COMMENT_CODE,
    "plant": PLANT_CODE,
    "make_reel": MAKE_REEL_CODE,
    "batch": BATCH_CODE,
    "delete_wt": DELETE_WT_CODE,
    "delete_uid": DELETE_UID_CODE,
    "open": OPEN_CODE,
    "stash": STASH_CODE,
    "copy": COPY_CODE,
    "struct": STRUCT_CODE,
    "uid": UID_CODE,
    "reel_clips": REEL_CLIPS_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def tx_name(ctx: Ctx, wiretap_id: str) -> Any:
    got = await tx_val(ctx, fill(NAME_CODE, WT=wiretap_id), "p32b_name")
    return None if is_error(got) else got


async def tx_comment(ctx: Ctx, wiretap_id: str, track: int, record_in: int) -> Any:
    code = fill(COMMENT_CODE, WT=wiretap_id, TRACK=track, RECORD_IN=record_in, ATTR=COMMENT_ATTR)
    got = await tx_val(ctx, code, "p32b_comment")
    return list(got) if isinstance(got, tuple) else got


async def run_call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    env = await do(session, ctx, op, build({**ctx.ids, **extra}))
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


async def make_reel(ctx: Ctx, tail: str) -> str | None:
    code = fill(MAKE_REEL_CODE, RG=ctx.ids["rgWt"], NAME=name_of(ctx, tail))
    got = await tx_val(ctx, code, "p32b_make_reel")
    rec(ctx, f"prepare_reel_{tail}", not is_error(got) and isinstance(got, str), resp=got)
    return got if isinstance(got, str) else None


async def state_snapshot(ctx: Ctx) -> dict[str, Any]:
    """What 'nothing happened' is judged against: the journal's groups and checkpoint records and
    the stash's entries."""
    return {
        "groups": groups_now(ctx),
        "checkpoints": len(checkpoint_records(ctx)),
        "stash": stash_uids(await tx_stash(ctx)),
    }


def mine(env: dict[str, Any]) -> dict[str, Any]:
    return _result_of(env)
