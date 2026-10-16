"""Stage b of the scenario e2e (see p3_5_scenarios_e2e.py): scenario 2, a shot taken
out of a sequence with its handles, as one chain of ops.

  timeline.list_segments -> segment.info -> segment.copy_to_media_panel (the used range with its
  soft handles, into a Desktop reel) -> segment.trim x 2 on the INNER segment of the clip it made
  (each edge extended by the handle length: the soft handle becomes part of the clip) ->
  desktop.rename -> a read back of the clip -> journal.revert of the rename and the two trims
  (newest first: the clip is as the copy made it) -> journal.revert of the copy (the restored clip
  is deleted).

The segment is p2_len72 on track 1 of a sequence, trimmed to head / tail 10 (52 frames used of 72).
Nothing here writes through transport; every state is READ BACK by transport. What is JUDGED: the
shape copy_to_media_panel returns, the source range and the handles of the inner
segment after the two trims, the record length of that segment (used range + 2 x the handle
length), the new name, the reverts of the edits, and (`b8`) the revert of the COPY once the clip's
edits are reverted: a revert of an edit puts a stashed copy back with a NEW uid, and the copy's
revert follows the uid chain of the Timeline restores to the newest uid that resolves, so it
deletes the restored clip (`foundUid` is the restored uid, `alreadyGone` is false). What is only
OBSERVED: the clip's own `duration`, `start_frame` and marks after the trims (nothing measured says
what Flame makes of them for a trimmed inner segment; the value the arithmetic gives is recorded
next to them)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_5_e2e_common import (  # noqa: E402
    HANDLE,
    WANT_HANDLE,
    err_text,
    fingerprint,
    make_seq,
    media_row,
    name_of,
    names_of,
    restored_wt,
    revert,
    run_call,
    seg_at,
    segment_row,
    sel,
    track_rows,
    tx_children,
    tx_clip,
    tx_find_uid,
    tx_seg,
    tx_struct,
    uids_of,
)

SOURCE = "p2_len72"
S2_PUTS = ((0, "clip1", 1), (1, SOURCE, 1), (0, "clip2", 25))
RANGE_KEYS = ("rin", "rout", "sin", "sout", "head", "tail")


def inner_of(facts: dict[str, Any] | None) -> dict[str, Any]:
    segments = (facts or {}).get("segments") or []
    return dict(segments[0]) if segments else {}


def listing_items(env: dict[str, Any]) -> list[dict[str, Any]]:
    items = _result_of(env).get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


async def build_sequence(ctx: Ctx) -> dict[str, Any] | None:
    made = await make_seq(ctx, "S2", S2_PUTS, trims=((1, HANDLE, HANDLE),))
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    row = media_row(struct, 1, SOURCE)
    if row is None:
        rec(ctx, "b0_the_segment_to_take_out_is_there", False, track1=track_rows(struct, 1))
        return None
    facts = await tx_seg(ctx, made["wt"], 1, row["rin"])
    if facts is None:
        rec(ctx, "b0_the_segment_to_take_out_can_be_read", False, row=row)
        return None
    return {"wt": made["wt"], "rin": row["rin"], "row": facts}


async def read_segment(session: Any, ctx: Ctx, case: dict[str, Any]) -> None:
    wiretap, rin, row = case["wt"], case["rin"], case["row"]
    listed = await run_call(
        session,
        ctx,
        "b_list_segments",
        {"target": sel(wiretap), "version": 0, "track": 1, "limit": 50},
    )
    item = segment_row(listing_items(listed), rin)
    info = await run_call(
        session, ctx, "b_info", {"target": seg_at(sel(wiretap), 1, rin, expect=SOURCE)}
    )
    res = _result_of(info)
    rec(
        ctx,
        "b1_list_segments_and_info_say_the_used_range_and_the_handles_transport_reads",
        listed.get("ok") is True
        and info.get("ok") is True
        and item is not None
        and item.get("sourceName") == SOURCE
        and item.get("head") == HANDLE
        and item.get("tail") == HANDLE
        and (res.get("sourceIn") or {}).get("frame") == row["sin"]
        and (res.get("sourceOut") or {}).get("frame") == row["sout"]
        and (res.get("recordIn") or {}).get("frame") == row["rin"]
        and res.get("head") == HANDLE
        and res.get("tail") == HANDLE,
        error=info.get("error") or listed.get("error"),
        item=item,
        transport=row,
    )


async def take_out(session: Any, ctx: Ctx, case: dict[str, Any]) -> dict[str, Any] | None:
    """segment.copy_to_media_panel: the used range with its soft handles, a new clip."""
    wiretap, row = case["wt"], case["row"]
    used = row["sout"] - row["sin"] + 1
    case["used"] = used
    dest = ctx.ids["ccpWt"]
    before = uids_of(await tx_children(ctx, dest))
    fp0 = await fingerprint(session, ctx, wiretap)
    env = await run_call(
        session,
        ctx,
        "b_copy_out",
        {"target": seg_at(sel(wiretap), 1, case["rin"]), "destination": sel(dest)},
    )
    created = _result_of(env).get("created") or {}
    selector = created.get("selector") or {}
    facts = await tx_clip(ctx, selector["id"]) if selector.get("id") else None
    inner = inner_of(facts)
    after = uids_of(await tx_children(ctx, dest))
    rec(
        ctx,
        "b2_a_new_clip_of_the_used_range_with_the_soft_handles_is_in_the_reel",
        env.get("ok") is True
        and facts is not None
        and facts.get("type") == "PyClip"
        and created.get("uid") in after
        and created.get("uid") not in before
        and facts.get("duration") == used
        and facts.get("startFrame") == HANDLE + 1
        and inner.get("head") == HANDLE
        and inner.get("tail") == HANDLE
        and all(inner.get(k) == row[k] for k in RANGE_KEYS)
        and fp0 is not None
        and await fingerprint(session, ctx, wiretap) == fp0,
        error=env.get("error"),
        used=used,
        clip=facts,
    )
    if env.get("ok") is not True or not facts:
        return None
    case.update(clipWt=selector["id"], clipUid=created.get("uid"), clipName=facts.get("name"))
    case["inner0"] = inner
    return env


async def trim_inner(session: Any, ctx: Ctx, case: dict[str, Any]) -> list[dict[str, Any]]:
    """segment.trim x 2 on the inner segment: each edge extended by WANT_HANDLE frames."""
    clip_wt = case["clipWt"]
    inner = case["inner0"]
    head = await run_call(
        session,
        ctx,
        "b_trim_head",
        {
            "target": seg_at(sel(clip_wt), 0, inner["rin"]),
            "edge": "head",
            "offset": -WANT_HANDLE,
            "ripple": True,
        },
    )
    position = (_result_of(head).get("position") or {}).get("selector")
    mid = inner_of(await tx_clip(ctx, clip_wt))
    rec(
        ctx,
        "b3_trimming_the_head_by_minus_the_handle_length_extends_the_source_into_the_handle",
        head.get("ok") is True
        and mid.get("sin") == inner["sin"] - WANT_HANDLE
        and mid.get("head") == HANDLE - WANT_HANDLE
        and mid.get("sout") == inner["sout"]
        and isinstance(position, dict),
        error=head.get("error"),
        before=inner,
        after=mid,
        position=position,
    )
    if head.get("ok") is not True or not isinstance(position, dict):
        return [head]
    tail = await run_call(
        session,
        ctx,
        "b_trim_tail",
        {"target": position, "edge": "tail", "offset": -WANT_HANDLE, "ripple": True},
    )
    facts = await tx_clip(ctx, clip_wt)
    now = inner_of(facts)
    length = now.get("rout", 0) - now.get("rin", 0) + 1
    source_length = now.get("sout", 0) - now.get("sin", 0) + 1
    want = case["used"] + 2 * WANT_HANDLE
    rec(
        ctx,
        "b4_the_inner_segment_is_the_used_range_plus_the_handle_length_on_each_side",
        tail.get("ok") is True
        and now.get("sin") == inner["sin"] - WANT_HANDLE
        and now.get("sout") == inner["sout"] + WANT_HANDLE
        and now.get("head") == HANDLE - WANT_HANDLE
        and now.get("tail") == HANDLE - WANT_HANDLE
        and length == want
        and source_length == want,
        error=tail.get("error"),
        want=want,
        recordLength=length,
        sourceLength=source_length,
        before=inner,
        after=now,
    )
    observe(
        ctx,
        "b5_the_clip_after_the_trims_(duration_start_frame_marks_not_measured_before)",
        duration=(facts or {}).get("duration"),
        startFrame=(facts or {}).get("startFrame"),
        inMark=(facts or {}).get("inMark"),
        outMark=(facts or {}).get("outMark"),
        usedPlusTwoHandles=want,
        durationIsThat=(facts or {}).get("duration") == want,
        segment=now,
    )
    return [head, tail]


async def rename_clip(session: Any, ctx: Ctx, case: dict[str, Any]) -> dict[str, Any]:
    new_name = name_of(ctx, f"SHOT_H{WANT_HANDLE}")
    env = await run_call(
        session, ctx, "b_rename", {"target": sel(case["clipWt"]), "name": new_name}
    )
    facts = await tx_clip(ctx, case["clipWt"])
    inner = inner_of(facts)
    want = case["used"] + 2 * WANT_HANDLE
    rec(
        ctx,
        "b6_rename_gives_the_clip_its_delivery_name_and_the_trimmed_segment_is_as_it_was",
        env.get("ok") is True
        and (facts or {}).get("name") == new_name
        and inner.get("rout", 0) - inner.get("rin", 0) + 1 == want
        and names_of(await tx_children(ctx, ctx.ids["ccpWt"])).count(new_name) == 1,
        error=env.get("error"),
        clipName=(facts or {}).get("name"),
    )
    case["deliveryName"] = new_name
    return env


async def revert_all(
    session: Any,
    ctx: Ctx,
    case: dict[str, Any],
    copied: dict[str, Any],
    edits: list[dict[str, Any]],
) -> None:
    """journal.revert of the edits of the clip, the newest first (the rename, the two trims): a
    trim's revert puts a stashed copy of the clip back, so it has a NEW uid and wiretap id, and
    the clip is the one `copy_to_media_panel` made, as it was right after that call. Then the
    revert of the copy follows the uid chain to the restored clip and deletes it (`b8`, JUDGED)."""
    outs = [await revert(session, ctx, env) for env in reversed(edits)]
    wiretap = next((w for w in (restored_wt(o) for o in reversed(outs)) if w), None)
    facts = await tx_clip(ctx, wiretap) if wiretap else None
    inner = inner_of(facts)
    rec(
        ctx,
        "b7_reverting_the_edits_puts_the_clip_back_as_the_copy_made_it",
        all(o.get("ok") is True for o in outs)
        and wiretap is not None
        and facts is not None
        and facts.get("name") == case["clipName"]
        and facts.get("duration") == case["used"]
        and all(inner.get(k) == case["inner0"].get(k) for k in RANGE_KEYS),
        errors=[err_text(o) for o in outs if o.get("ok") is not True],
        restored=facts,
        wanted=case["inner0"],
    )
    reel = ctx.ids["ccpWt"]
    restored_uid = (facts or {}).get("uid")
    out = await revert(session, ctx, copied)
    rows = await tx_children(ctx, reel)
    res = _result_of(out)
    rec(
        ctx,
        "b8_reverting_the_copy_follows_the_uid_chain_and_deletes_the_restored_clip",
        out.get("ok") is True
        and res.get("deleted") is True
        and res.get("alreadyGone") is False
        and isinstance(restored_uid, str)
        and res.get("foundUid") == restored_uid
        and res.get("uidChain", [None])[0] == restored_uid
        and case["clipUid"] in (res.get("uidChain") or [])
        and case["clipName"] not in names_of(rows)
        and restored_uid not in uids_of(rows)
        and await tx_find_uid(ctx, restored_uid) is None,
        error=out.get("error"),
        foundUid=res.get("foundUid"),
        uidChain=res.get("uidChain"),
        restoredUid=restored_uid,
        alreadyGone=res.get("alreadyGone"),
        namesInReel=names_of(rows),
    )


async def stage_b(ctx: Ctx) -> None:
    case = await build_sequence(ctx)
    if case is None:
        return
    ctx.ids.setdefault("case", {})["scenario2"] = case
    async with server_session(ctx, "p35-b") as session:
        await read_segment(session, ctx, case)
        copied = await take_out(session, ctx, case)
        if copied is None:
            return
        trims = await trim_inner(session, ctx, case)
        if len(trims) != 2:
            return
        renamed = await rename_clip(session, ctx, case)
        await revert_all(session, ctx, case, copied, [*trims, renamed])
