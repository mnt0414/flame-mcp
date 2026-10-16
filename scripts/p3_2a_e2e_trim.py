"""Stage c (segment.trim) of the checkpoint / trim real-Flame e2e; see p3_2a_checkpoint_e2e.py.

Every case is a fresh S1-like sequence, so one edit never starts from another's result. The segment
trimmed is p2_len72 on track 1 (head 10 / tail 10 handles, a Gap on each side): it can be shortened
and, as far as its handles go, extended. What the call did is read back by transport (the segment's
record_in / out, source_in / out, head, tail and the other tracks) and compared with the op's own
`before` / `after` / `deltas`; `expected_deltas` below states the measured behaviour again so the
op's idea of it is checked against a second statement."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, do, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    CALLS,
    TRIM_CASES,
    TRIM_REFUSALS,
    checkpoint_records,
    err_text,
    expect_error,
    groups_now,
    make_s1,
    media_row,
    outcome_of,
    run_call,
    stash_uids,
    track_rows,
    tx_seg,
    tx_stash,
    tx_struct,
)

FACT_KEYS = ("rin", "rout", "sin", "sout", "head", "tail")
OP_FACT_KEYS = {
    "rin": ("recordIn", True),
    "rout": ("recordOut", True),
    "sin": ("sourceIn", True),
    "sout": ("sourceOut", True),
    "head": ("head", False),
    "tail": ("tail", False),
}


def expected_deltas(edge: str, offset: int, ripple: bool) -> dict[str, int]:
    if edge == "head":
        return {
            "rin": 0 if ripple else offset,
            "rout": -offset if ripple else 0,
            "sin": offset,
            "sout": 0,
            "head": offset,
            "tail": 0,
        }
    return {"rin": 0, "rout": -offset, "sin": 0, "sout": -offset, "head": 0, "tail": offset}


def deltas_of(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    return {k: after[k] - before[k] for k in FACT_KEYS}


def op_facts_equal(op_side: dict[str, Any] | None, facts: dict[str, Any] | None) -> bool:
    """The op's `before` / `after` (PyTime as {timecode, frame}) against transport's facts."""
    if not isinstance(op_side, dict) or not isinstance(facts, dict):
        return False
    for key, (name, is_time) in OP_FACT_KEYS.items():
        value = op_side.get(name)
        if is_time:
            value = value.get("frame") if isinstance(value, dict) else None
        if value != facts.get(key):
            return False
    return True


async def trim_case(session: Any, ctx: Ctx, tag: str, edge: str, offset: int, ripple: bool) -> None:
    made = await make_s1(ctx, tag)
    if made is None:
        return
    struct0 = await tx_struct(ctx, made["wt"])
    row0 = media_row(struct0, 1, "p2_len72")
    if row0 is None:
        rec(ctx, f"{tag}_the_segment_to_trim_is_there", False, track1=track_rows(struct0, 1))
        return
    case = {"wt": made["wt"], "uid": made["uid"], "rin": row0["rin"]}
    ctx.ids.setdefault("case", {})[tag] = case
    before = await tx_seg(ctx, made["wt"], 1, row0["rin"])
    env = await run_call(session, ctx, tag)
    case["group"] = (env.get("journal") or {}).get("group")
    res = _result_of(env)
    position = res.get("position") or {}
    new_rin = position.get("recordIn")
    after = await tx_seg(ctx, made["wt"], 1, new_rin) if isinstance(new_rin, int) else None
    struct1 = await tx_struct(ctx, made["wt"])
    want = expected_deltas(edge, offset, ripple)
    got = deltas_of(before, after) if before and after else None
    rec(
        ctx,
        f"{tag}_the_segment_changed_the_way_it_is_measured_to_(read_back_by_transport)",
        env.get("ok") is True and got == want and after is not None and after["type"] != "Gap",
        error=env.get("error"),
        want=want,
        got=got,
        before=before,
        after=after,
    )
    rec(
        ctx,
        f"{tag}_the_result_says_what_transport_read",
        env.get("ok") is True
        and op_facts_equal(res.get("before"), before)
        and op_facts_equal(res.get("after"), after)
        and res.get("deltas")
        == {
            "recordIn": want["rin"],
            "recordOut": want["rout"],
            "sourceIn": want["sin"],
            "sourceOut": want["sout"],
            "head": want["head"],
            "tail": want["tail"],
        },
        result={k: res.get(k) for k in ("before", "after", "deltas", "expectedDeltas", "returned")},
    )
    info = await do(session, ctx, "segment.info", {"target": position.get("selector")})
    by_index = await do(session, ctx, "segment.info", {"target": position.get("selectorByIndex")})
    rec(
        ctx,
        f"{tag}_the_position_selectors_resolve_to_the_trimmed_segment",
        info.get("ok") is True
        and by_index.get("ok") is True
        and (_result_of(info).get("recordIn") or {}).get("frame") == new_rin
        and (_result_of(by_index).get("recordIn") or {}).get("frame") == new_rin
        and position.get("index") == (after or {}).get("index"),
        error=info.get("error"),
        position=position,
    )
    ends1 = max((r["rout"] for r in track_rows(struct1, 1)), default=None)
    ends0 = max((r["rout"] for r in track_rows(struct1, 0)), default=None)
    track_ends = {t.get("track"): t.get("endFrame") for t in res.get("trackEnds") or []}
    rec(
        ctx,
        f"{tag}_the_other_track_did_not_move_and_trackEnds_says_where_each_ends",
        track_rows(struct1, 0) == track_rows(struct0, 0)
        and track_ends.get(0) == ends0
        and track_ends.get(1) == ends1
        and (res.get("track") or {}).get("endFrame") == ends1,
        trackEnds=res.get("trackEnds"),
        transportEnds=[ends0, ends1],
    )
    journal = env.get("journal") or {}
    outcome = outcome_of(ctx, journal.get("group"))
    rec(
        ctx,
        f"{tag}_the_journal_holds_the_fingerprints_and_a_checkpoint",
        bool(journal.get("checkpoint"))
        and journal.get("changed") is True
        and outcome.get("ok") is True
        and outcome.get("changed") is True
        and outcome.get("fingerprintAfter") is not None,
        journal=journal,
    )
    observe(
        ctx,
        f"{tag}_the_track_before_and_after",
        track1Before=track_rows(struct0, 1),
        track1After=track_rows(struct1, 1),
        track0Same=track_rows(struct1, 0) == track_rows(struct0, 0),
        returned=res.get("returned"),
        ripple=ripple,
    )


async def trim_refusals(session: Any, ctx: Ctx) -> None:
    made = await make_s1(ctx, "R")
    if made is None:
        return
    struct0 = await tx_struct(ctx, made["wt"])
    row = media_row(struct0, 1, "p2_len72")
    if row is None:
        rec(ctx, "c9_the_segment_to_refuse_is_there", False)
        return
    stash0 = stash_uids(await tx_stash(ctx))
    for tag, _edge, _offset in TRIM_REFUSALS:
        ctx.ids.setdefault("case", {})[tag] = {"wt": made["wt"], "rin": row["rin"]}
    ctx.ids.setdefault("case", {})["c11_trim_a_gap"] = {
        "wt": made["wt"],
        "rin": row["rin"],
        "gapRin": track_rows(struct0, 1)[0]["rin"],
    }
    refusals = [
        ("c9_extension_beyond_the_handle", "PRECONDITION_UNMET", "handle"),
        ("c10_trim_the_whole_length", "PRECONDITION_UNMET", "leave nothing"),
        ("c11_trim_a_gap", "PRECONDITION_UNMET", "Gap"),
        ("c12_trim_in_a_library", "PRECONDITION_UNMET", "inside a library"),
        ("c13_bad_edge", "INVALID_ARGS", "edge"),
        ("c14_zero_offset", "INVALID_ARGS", "offset"),
    ]
    for call_id, code, needle in refusals:
        groups = groups_now(ctx)
        checkpoints = len(checkpoint_records(ctx))
        env = await run_call(session, ctx, call_id)
        stash1 = stash_uids(await tx_stash(ctx))
        res = _result_of(env)
        extra = {"handle": res.get("handle")} if call_id == "c9_extension_beyond_the_handle" else {}
        rec(
            ctx,
            f"{call_id}_is_refused_before_anything_is_copied_or_journaled",
            expect_error(env, code)
            and needle in err_text(env)
            and groups_now(ctx) == groups
            and len(checkpoint_records(ctx)) == checkpoints
            and stash1 == stash0
            and (call_id != "c9_extension_beyond_the_handle" or res.get("handle") == 10),
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            **extra,
        )
    struct1 = await tx_struct(ctx, made["wt"])
    rec(
        ctx,
        "c9_c14_none_of_the_refused_calls_changed_the_sequence",
        track_rows(struct1, 0) == track_rows(struct0, 0)
        and track_rows(struct1, 1) == track_rows(struct0, 1),
    )


async def stage_c(ctx: Ctx) -> None:
    async with server_session(ctx, "p32a-c") as session:
        for tag, edge, offset, ripple in TRIM_CASES:
            await trim_case(session, ctx, tag, edge, offset, ripple)
        await trim_refusals(session, ctx)
        first = ctx.ids.get("case", {}).get(TRIM_CASES[0][0]) or {}
        rec(
            ctx,
            "c15_every_trim_call_is_in_the_call_table",
            {t for t, *_ in TRIM_CASES} <= set(CALLS) and "wt" in first,
        )
