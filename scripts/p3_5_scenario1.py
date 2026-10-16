"""Stage a of the scenario e2e (see p3_5_scenarios_e2e.py): scenario 1, the EDL
replace with the source timecode kept, as one chain of ops.

  library.list -> desktop.list_children -> mediahub.import (the replacement into a library reel)
  -> desktop.find -> timeline.list_segments -> segment.info -> desktop.copy (out of the library)
  -> plan.run[segment.replace x 2] -> segment.slip x 2 (the source timecode of each segment is
  the one it had) -> segment.create_effect -> segment.effect_save_setup -> segment.set_effect ->
  segment.effect_load_setup (the saved values are back) -> journal.revert of every group, newest
  first, and the fingerprint of the sequence is the first one again.

Nothing here writes through transport: the sequence (two segments of p2_src_tc1h, trimmed to
head / tail 10, and one other clip) is the material; every state is READ BACK by transport, never
through the op that wrote it. The copy and the import are reverted by stage n, after it has tried
what the library protection refuses on the clip this stage brought in."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, judged, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_5_e2e_common import (  # noqa: E402
    HANDLE,
    LATE_BY,
    LETTERBOX,
    MEASURED_EXTENSIONS,
    REPLACEMENT_CLIP,
    REPLACEMENT_FILE,
    SOURCE_CLIP,
    err_text,
    fingerprint,
    fx_index_order,
    fx_types,
    fx_values,
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
    tx_fx,
    tx_seg,
    tx_struct,
    uids_of,
)

S1_PUTS = ((0, SOURCE_CLIP, 1), (1, SOURCE_CLIP, 1), (0, "clip1", 61))
"""Track 0: the source, a Gap, clip1; track 1: the source. Both segments of the source are
trimmed to head / tail 10."""
FRAME_KEYS = ("rin", "rout", "sin", "sout", "head", "tail")
RATIO_A = 1.85
RATIO_B = 2.35


def facts(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else {k: row[k] for k in ("src", *FRAME_KEYS)}


def listing_items(env: dict[str, Any]) -> list[dict[str, Any]]:
    items = _result_of(env).get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


async def build_sequence(ctx: Ctx) -> dict[str, Any] | None:
    made = await make_seq(
        ctx,
        "S1",
        S1_PUTS,
        trims=((0, HANDLE, HANDLE), (1, HANDLE, HANDLE)),
    )
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    first = media_row(struct, 0, SOURCE_CLIP)
    second = media_row(struct, 1, SOURCE_CLIP)
    other = media_row(struct, 0, "clip1")
    if not (first and second and other):
        rec(
            ctx,
            "a0_the_segments_to_replace_are_there",
            False,
            track0=track_rows(struct, 0),
            track1=track_rows(struct, 1),
        )
        return None
    return {
        "wt": made["wt"],
        "struct": struct,
        "tracks": {0: first["rin"], 1: second["rin"]},
        "otherRin": other["rin"],
    }


async def read_in(session: Any, ctx: Ctx, case: dict[str, Any]) -> dict[str, Any] | None:
    """library.list, desktop.list_children, mediahub.import, desktop.find: the replacement is in
    the library reel, found by name, and what the ops say is what transport lists. Returns the
    clip's entry (selector, uid, name) or None."""
    ids = ctx.ids
    lib_name = name_of(ctx, "LIB")
    libraries = await run_call(session, ctx, "a_library_list", {"limit": 500})
    names = [i.get("name") for i in listing_items(libraries)]
    rec(
        ctx,
        "a1_library_list_names_the_library_of_this_run",
        libraries.get("ok") is True and lib_name in names,
        error=libraries.get("error"),
        total=len(names),
    )
    in_library = await run_call(session, ctx, "a_list_children", {"target": sel(ids["libWt"])})
    reels = [i.get("name") for i in listing_items(in_library)]
    empty = await run_call(session, ctx, "a_list_children", {"target": sel(ids["libReelWt"])})
    rec(
        ctx,
        "a2_the_library_holds_its_reel_and_the_reel_is_empty",
        in_library.get("ok") is True
        and "LR" in reels
        and empty.get("ok") is True
        and _result_of(empty).get("total") == 0,
        reels=reels,
        error=empty.get("error"),
    )
    before = await tx_children(ctx, ids["libReelWt"])
    imported = await run_call(
        session,
        ctx,
        "a_import",
        {"paths": [str(REPLACEMENT_FILE)], "destination": sel(ids["libReelWt"])},
    )
    after = await tx_children(ctx, ids["libReelWt"])
    made = (_result_of(imported).get("imported") or [{}])[0]
    ctx.ids["p35Import"] = imported
    rec(
        ctx,
        "a3_import_puts_the_replacement_into_the_library_reel_and_transport_lists_it",
        imported.get("ok") is True
        and made.get("name") == REPLACEMENT_CLIP
        and made.get("uid") in uids_of(after)
        and made.get("uid") not in uids_of(before)
        and len(after or []) == len(before or []) + 1
        and (imported.get("journal") or {}).get("revert") is not None,
        error=imported.get("error"),
        imported=made,
        names=names_of(after),
    )
    if not made.get("uid"):
        return None
    found = await run_call(
        session,
        ctx,
        "a_find",
        {"name": REPLACEMENT_CLIP, "parent": sel(ids["libReelWt"])},
    )
    hits = listing_items(found)
    rec(
        ctx,
        "a4_find_by_name_in_the_library_reel_answers_that_one_clip",
        found.get("ok") is True
        and len(hits) == 1
        and hits[0].get("uid") == made["uid"]
        and (hits[0].get("selector") or {}).get("id") == made["wiretapId"],
        error=found.get("error"),
        hits=hits,
    )
    return {"uid": made["uid"], "selector": made["selector"], "name": made["name"]}


async def read_segments(session: Any, ctx: Ctx, case: dict[str, Any]) -> None:
    """timeline.list_segments and segment.info say what transport read of the segment."""
    wiretap = case["wt"]
    for track in (0, 1):
        rin = case["tracks"][track]
        row = await tx_seg(ctx, wiretap, track, rin)
        listed = await run_call(
            session,
            ctx,
            "a_list_segments",
            {"target": sel(wiretap), "version": 0, "track": track, "limit": 50},
        )
        item = segment_row(listing_items(listed), rin)
        info = await run_call(
            session,
            ctx,
            "a_info",
            {"target": seg_at(sel(wiretap), track, rin, expect=SOURCE_CLIP)},
        )
        res = _result_of(info)
        rec(
            ctx,
            f"a{5 + track}_list_segments_and_info_agree_with_transport_on_track_{track}",
            listed.get("ok") is True
            and info.get("ok") is True
            and row is not None
            and item is not None
            and item.get("sourceName") == SOURCE_CLIP
            and item.get("head") == row["head"] == HANDLE
            and item.get("tail") == row["tail"] == HANDLE
            and res.get("sourceName") == SOURCE_CLIP
            and (res.get("sourceIn") or {}).get("frame") == row["sin"]
            and (res.get("sourceOut") or {}).get("frame") == row["sout"]
            and res.get("head") == HANDLE
            and res.get("tail") == HANDLE,
            error=info.get("error") or listed.get("error"),
            item=item,
            transport=facts(row),
        )


async def copy_out(session: Any, ctx: Ctx, entry: dict[str, Any]) -> dict[str, Any] | None:
    """desktop.copy: the library clip goes to the Desktop (the library keeps it)."""
    ids = ctx.ids
    ccp_before = await tx_children(ctx, ids["ccpWt"])
    lib_before = await tx_children(ctx, ids["libReelWt"])
    env = await run_call(
        session,
        ctx,
        "a_copy_out",
        {"sources": [entry["selector"]], "destination": sel(ids["ccpWt"])},
    )
    ccp_after = await tx_children(ctx, ids["ccpWt"])
    lib_after = await tx_children(ctx, ids["libReelWt"])
    copies = _result_of(env).get("copies") or []
    copy = copies[0] if copies else {}
    ctx.ids["p35Copy"] = env
    rec(
        ctx,
        "a7_desktop_copy_takes_the_clip_out_of_the_library_and_the_library_keeps_it",
        env.get("ok") is True
        and len(copies) == 1
        and copy.get("name") == REPLACEMENT_CLIP
        and copy.get("uid") != entry["uid"]
        and copy.get("uid") in uids_of(ccp_after)
        and copy.get("uid") not in uids_of(ccp_before)
        and len(ccp_after or []) == len(ccp_before or []) + 1
        and uids_of(lib_after) == uids_of(lib_before)
        and _result_of(env).get("sourcesRemain") is True,
        error=env.get("error"),
        copy=copy,
    )
    return copy if env.get("ok") is True and copy.get("selector") else None


async def replace_two(
    session: Any, ctx: Ctx, case: dict[str, Any], copy: dict[str, Any]
) -> dict[str, Any] | None:
    """plan.run[segment.replace x 2]: both segments get the replacement; the record stays and the
    OFFSET from the clip's start is kept: the source timecode is LATE_BY frames late."""
    wiretap = case["wt"]
    before = {t: await tx_seg(ctx, wiretap, t, rin) for t, rin in case["tracks"].items()}
    case["before"] = before
    steps = [
        {
            "op": "segment.replace",
            "args": {
                "target": seg_at(sel(wiretap), track, rin, expect=SOURCE_CLIP),
                "source": copy["selector"],
            },
        }
        for track, rin in case["tracks"].items()
    ]
    env = await run_call(
        session, ctx, "a_plan_replace", {"steps": steps, "label": "p35 scenario 1 replace"}
    )
    after = {t: await tx_seg(ctx, wiretap, t, rin) for t, rin in case["tracks"].items()}
    res = _result_of(env)
    rows = res.get("steps") or []
    ok_steps = [s.get("ok") for s in rows] == [True, True]
    rec(
        ctx,
        "a8_one_plan_replaced_both_segments_and_the_record_stayed_(read_back_by_transport)",
        env.get("ok") is True
        and ok_steps
        and all(before[t] is not None and after[t] is not None for t in case["tracks"])
        and all(after[t]["src"] == REPLACEMENT_CLIP for t in case["tracks"])
        and all(
            (after[t]["rin"], after[t]["rout"]) == (before[t]["rin"], before[t]["rout"])
            for t in case["tracks"]
        )
        and all(after[t]["sin"] - before[t]["sin"] == LATE_BY for t in case["tracks"])
        and (res.get("timeline") or {}).get("changed") is True,
        error=env.get("error"),
        before={t: facts(r) for t, r in before.items()},
        after={t: facts(r) for t, r in after.items()},
        steps=[(s.get("op"), s.get("ok"), s.get("errorCode")) for s in rows],
    )
    other = await tx_seg(ctx, wiretap, 0, case["otherRin"])
    rec(
        ctx,
        "a9_the_segment_that_was_not_named_is_as_it_was",
        other is not None and other["src"] == "clip1",
        other=facts(other),
    )
    if not (env.get("ok") is True and ok_steps):
        return None
    case["replaceSteps"] = rows
    return env


def late_by_from(step: dict[str, Any]) -> int:
    """The difference the replace result states between the new and the old source frame."""
    result = step.get("result") or {}
    new = ((result.get("after") or {}).get("sourceIn") or {}).get("frame")
    old = ((result.get("before") or {}).get("sourceIn") or {}).get("frame")
    return new - old if isinstance(new, int) and isinstance(old, int) else 0


async def slip_back(session: Any, ctx: Ctx, case: dict[str, Any]) -> list[dict[str, Any]]:
    """segment.slip by the difference each replace result states: the source frames of both
    segments are the ones they had before the replace."""
    wiretap = case["wt"]
    slips: list[dict[str, Any]] = []
    for track, step in zip(case["tracks"], case["replaceSteps"], strict=True):
        diff = late_by_from(step)
        selector = ((step.get("result") or {}).get("position") or {}).get("selector")
        env = await run_call(session, ctx, "a_slip", {"target": selector, "offset": diff})
        slips.append(env)
        rin = case["tracks"][track]
        now = await tx_seg(ctx, wiretap, track, rin)
        was = case["before"][track]
        rec(
            ctx,
            f"a{10 + track}_slip_by_{diff}_brings_the_source_timecode_of_track_{track}_back",
            diff == LATE_BY
            and env.get("ok") is True
            and now is not None
            and was is not None
            and (now["sin"], now["sout"]) == (was["sin"], was["sout"])
            and (now["rin"], now["rout"]) == (was["rin"], was["rout"])
            and now["src"] == REPLACEMENT_CLIP,
            error=env.get("error"),
            diff=diff,
            was=facts(was),
            now=facts(now),
        )
    return slips


async def grade(session: Any, ctx: Ctx, case: dict[str, Any]) -> list[dict[str, Any]]:
    """create_effect -> effect_save_setup -> set_effect -> effect_load_setup: the grade is a
    settings file, so what the file holds is what comes back."""
    wiretap, rin = case["wt"], case["tracks"][0]
    target = seg_at(sel(wiretap), 0, rin)
    files = Path(ctx.ids["filesDir"]) / "setups"
    files.mkdir(parents=True, exist_ok=True)
    chain: list[dict[str, Any]] = []
    made = await run_call(session, ctx, "a_create_effect", {"target": target, "type": LETTERBOX})
    chain.append(made)
    rows = await tx_fx(ctx, wiretap, 0, rin)
    start = fx_values(rows, LETTERBOX) or {}
    ok, parts = judged(
        op_ok=made.get("ok") is True,
        transport_holds_only_the_letterbox=fx_types(rows) == [LETTERBOX],
    )
    rec(
        ctx,
        "a12_create_effect_puts_a_Burn-in_Letterbox_on_the_segment",
        ok,
        clauses=parts,
        error=made.get("error"),
        effects=fx_index_order(rows),
        values=start,
    )
    before_files = {p.name for p in files.iterdir()}
    saved = await run_call(
        session,
        ctx,
        "a_save_setup",
        {"target": target, "type": LETTERBOX, "path": str(files / "p35_lb")},
    )
    wrote = sorted(p.name for p in files.iterdir() if p.name not in before_files)
    node_file = next(
        (f["path"] for f in _result_of(saved).get("files") or [] if f["path"].endswith("_node")),
        None,
    )
    rec(
        ctx,
        "a13_save_setup_wrote_the_letterbox_files_and_the_effect_was_not_touched",
        saved.get("ok") is True
        and wrote == sorted("p35_lb" + ext for ext in MEASURED_EXTENSIONS[LETTERBOX])
        and node_file is not None
        and os.path.isfile(node_file)
        and fx_values(await tx_fx(ctx, wiretap, 0, rin), LETTERBOX) == start
        and not (saved.get("journal") or {}).get("group"),
        error=saved.get("error"),
        wrote=wrote,
    )
    new_ratio = RATIO_A if abs(float(start.get("ratio", 0)) - RATIO_A) > 1e-6 else RATIO_B
    changed = await run_call(
        session,
        ctx,
        "a_set_effect",
        {"target": target, "type": LETTERBOX, "values": {"ratio": new_ratio}},
    )
    chain.append(changed)
    moved = fx_values(await tx_fx(ctx, wiretap, 0, rin), LETTERBOX) or {}
    rec(
        ctx,
        "a14_set_effect_changes_the_ratio_and_transport_reads_it",
        changed.get("ok") is True and abs(float(moved.get("ratio", 0)) - new_ratio) < 1e-6,
        error=changed.get("error"),
        was=start.get("ratio"),
        now=moved.get("ratio"),
    )
    if node_file is None:
        rec(ctx, "a15_a_setup_file_to_load_is_there", False)
        return chain
    loaded = await run_call(
        session,
        ctx,
        "a_load_setup",
        {"target": target, "type": LETTERBOX, "path": node_file},
    )
    chain.append(loaded)
    back = fx_values(await tx_fx(ctx, wiretap, 0, rin), LETTERBOX) or {}
    rec(
        ctx,
        "a15_load_setup_brings_the_saved_values_back",
        loaded.get("ok") is True and back == start and back.get("ratio") != moved.get("ratio"),
        error=loaded.get("error"),
        saved=start,
        changed=moved,
        back=back,
    )
    return chain


async def revert_all(
    session: Any, ctx: Ctx, case: dict[str, Any], writes: list[dict[str, Any]], fp0: str | None
) -> None:
    """journal.revert of every group of the sequence, the newest first. A revert puts a stashed
    copy back (a new wiretap id), so only the last answer tells where the sequence is now."""
    wiretap = None
    outs: list[dict[str, Any]] = []
    for env in reversed(writes):
        out = await revert(session, ctx, env)
        outs.append(out)
        wiretap = restored_wt(out) or wiretap
    rec(
        ctx,
        "a17_every_group_of_the_chain_was_reverted",
        len(writes) == 6 and all(o.get("ok") is True for o in outs),
        groups=len(writes),
        errors=[err_text(o) for o in outs if o.get("ok") is not True],
    )
    if wiretap is None:
        rec(ctx, "a18_the_sequence_is_back_as_it_was", False)
        return
    struct = await tx_struct(ctx, wiretap)
    segment_facts = {t: facts(await tx_seg(ctx, wiretap, t, r)) for t, r in case["tracks"].items()}
    fp_now = await fingerprint(session, ctx, wiretap)
    fx_now = fx_index_order(await tx_fx(ctx, wiretap, 0, case["tracks"][0]))
    ok, parts = judged(
        first_fingerprint_was_read=fp0 is not None,
        fingerprint_is_the_first_one=fp_now == fp0,
        structure_is_the_one_before=struct == case["struct"],
        every_segment_is_as_before=all(
            segment_facts[t] == facts(case["before"][t]) for t in case["tracks"]
        ),
        first_segment_holds_no_effect=fx_now == [],
    )
    rec(
        ctx,
        "a18_the_sequence_is_back_as_it_was_the_fingerprint_and_every_segment_(read_by_transport)",
        ok,
        clauses=parts,
        fingerprint=fp0,
        fingerprintNow=fp_now,
        segments=segment_facts,
        segmentsBefore={t: facts(case["before"][t]) for t in case["tracks"]},
        structBefore=case["struct"],
        structNow=struct,
        effectsOfTheFirstSegment=fx_now,
    )
    case["finalWt"] = wiretap


async def stage_a(ctx: Ctx) -> None:
    case = await build_sequence(ctx)
    if case is None:
        return
    ctx.ids.setdefault("case", {})["scenario1"] = case
    async with server_session(ctx, "p35-a") as session:
        fp0 = await fingerprint(session, ctx, case["wt"])
        rec(ctx, "a0_the_first_fingerprint_is_read", fp0 is not None, fingerprint=fp0)
        entry = await read_in(session, ctx, case)
        if entry is None:
            return
        await read_segments(session, ctx, case)
        copy = await copy_out(session, ctx, entry)
        if copy is None:
            return
        ctx.ids["p35LibraryClip"] = entry
        replaced = await replace_two(session, ctx, case, copy)
        if replaced is None:
            return
        slips = await slip_back(session, ctx, case)
        chain = await grade(session, ctx, case)
        observe(
            ctx,
            "a16_the_source_names_after_the_chain",
            names=[
                (await tx_seg(ctx, case["wt"], t, r) or {}).get("src")
                for t, r in case["tracks"].items()
            ],
        )
        await revert_all(session, ctx, case, [replaced, *slips, *chain], fp0)
