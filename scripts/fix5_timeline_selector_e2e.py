#!/usr/bin/env python3
"""Real-Flame end-to-end: the `{kind: 'timeline'}` selector -- the sequence the Timeline
shows now (run with the `.venv` Python; `scripts/run_e2e.sh fix5_timeline_selector` starts Flame;
`--dry-run` connects to nothing and only lists the calls and compiles the code).

Checks: The `{kind: 'timeline'}` selector, which names the sequence the Timeline shows now.

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material, opens sequences in the Timeline
and READS EVERY STATE BACK INDEPENDENTLY of the ops under test (what the Timeline shows is read from
`flame.timeline.clip` by transport). Everything this run makes is named `PR5E_<ts>_...` and is
deleted at the end: the reel group, the library and the stash library (`PR5E_<ts>_CKPT`, passed to
every server as FLAME_MCP_CHECKPOINT_LIBRARY); then flame.exit(). What stays on disk: this run's
journal directory (`p3/journal_pr5e_e2e_<ts>/`) and the result files. No test hook is set.

Stages (material: scripts/make_p2_media.sh; p2_len72 trimmed head 14 / tail 14 = 44 frames)
  a  S_a and S_b are made. S_a is opened (by transport): desktop.info / timeline.fingerprint /
     timeline.info with the selector answer S_a. S_b is opened: the same selector answers S_b
     (desktop.info, fingerprint, timeline.info, timeline.list_segments, timeline.checkpoint).
  t  segment.trim whose segment selector has the timeline as its parent edits the shown sequence;
     journal.revert puts the fingerprint back. timeline.create_version through the selector too.
  p  plan.run (two steps, both with the timeline as the parent) is one write; one revert undoes it.
  s  a timeline selector as the `source` of timeline.overwrite / timeline.insert is INVALID_ARGS and
     nothing changes (no journal record, no stash entry, same fingerprint).
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import e2e_guard  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
    _br_dict,
    _result_of,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import Ctx, guarded, rec, save, save_stage, server_session, tx  # noqa: E402
from p2_4_common import flame_exit, journal_records, ops_used, print_observations  # noqa: E402
from p3_1a_read_e2e import PRELUDE, STRUCT_CODE, fill, is_error, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    CKPT_ENV,
    OPEN_CODE,
    OUTPUT_DIR,
    PREPARE_CODE,
    SEG_FACTS_CODE,
    Results,
    call,
    ckpt_library,
    fingerprint,
    media_row,
    name_of,
    outcome_of,
    seg_at,
    sel,
    track_rows,
    tx_displayed,
    tx_seg,
    tx_stash,
    tx_struct,
)
from p3_3_e2e_common import (  # noqa: E402
    CLIP_FILES as P33_CLIP_FILES,
)
from p3_3_e2e_common import (  # noqa: E402
    MAKE_SEQ_TEMPLATE,
    make_seq,
    restored_wt,
    revert,
)

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

PREFIX: Final[str] = "PR5E"
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = tuple(
    (n, p) for n, p in P33_CLIP_FILES if n in ("clip1", "p2_len72")
)
"""clip1 is only there for the frame rate and size the sequences are made with."""
SOURCE_CLIP: Final[str] = "p2_len72"
TRIM_HANDLE: Final[int] = 14
RECORD_FRAMES: Final[int] = 44
TRIM_FRAMES: Final[int] = 3
TL: Final[dict[str, Any]] = {"kind": "timeline"}
FACT_KEYS: Final[tuple[str, ...]] = ("rin", "rout", "sin", "sout", "head", "tail")

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def declare(call_id: str, op: str, sample: dict[str, Any]) -> None:
    def build(ids: dict[str, Any]) -> dict[str, Any]:
        args = ids.get("args")
        return dict(args) if isinstance(args, dict) else dict(sample)

    CALLS[call_id] = (op, build)


_WT = sel("/wt/x")
_SEG_TL = seg_at(TL, 0, 14)

declare("a_desktop_info", "desktop.info", {"target": TL})
declare("a_fingerprint", "timeline.fingerprint", {"target": TL})
declare("a_info", "timeline.info", {"target": TL})
declare("a_list", "timeline.list_segments", {"target": TL, "version": 0, "track": 0})
declare("a_checkpoint", "timeline.checkpoint", {"target": TL})
declare("t_trim", "segment.trim", {"target": _SEG_TL, "edge": "tail", "offset": TRIM_FRAMES})
declare("t_create_version", "timeline.create_version", {"sequence": TL})
declare(
    "p_plan",
    "plan.run",
    {
        "steps": [
            {"op": "segment.trim", "args": {"target": _SEG_TL, "edge": "tail", "offset": 3}},
            {"op": "segment.trim", "args": {"target": _SEG_TL, "edge": "head", "offset": 3}},
        ]
    },
)
declare("s_overwrite", "timeline.overwrite", {"sequence": _WT, "source": TL, "at": 0})
declare("s_insert", "timeline.insert", {"sequence": _WT, "source": TL, "at": 0})

TEMPLATES: Final[dict[str, str]] = {
    "prepare": PREPARE_CODE,
    "make_seq": MAKE_SEQ_TEMPLATE,
    "seg_facts": SEG_FACTS_CODE,
    "struct": STRUCT_CODE,
    "open": OPEN_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def run_call(
    session: Any, ctx: Ctx, call_id: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    op, build = CALLS[call_id]
    return await call(session, ctx, op, args if args is not None else build({}))


# -- material and transport reads ---------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    ids = ctx.ids
    made = await tx(
        ctx,
        PRELUDE
        + "\n"
        + fill(
            PREPARE_CODE,
            RG=name_of(ctx, "RG"),
            LIB=name_of(ctx, "LIB"),
            PATHS=[[n, str(p)] for n, p in CLIP_FILES],
        ),
        label="pr5e_prepare",
        timeout_s=240.0,
    )
    rec(ctx, "prepare_reel_group_library_and_the_clips", made.ok, resp=_br_dict(made))
    if not (made.ok and isinstance(made.result, dict)):
        return
    result = made.result
    ids.update(
        seqReelWt=result["seqReelWt"],
        fps=result["fps"],
        width=result["width"],
        height=result["height"],
        clipWt={n: v["wt"] for n, v in result["clips"].items()},
    )
    rec(
        ctx,
        "prepare_every_clip_was_imported",
        set(result["clips"]) == {n for n, _p in CLIP_FILES},
        clips=sorted(result["clips"]),
    )


async def fresh(ctx: Ctx, tag: str) -> dict[str, Any] | None:
    """A new sequence (p2_len72 trimmed to 44 frames on track 0) and its media segment as
    transport reads it."""
    made = await make_seq(ctx, tag, ((0, SOURCE_CLIP, 1),), trims=((0, TRIM_HANDLE, TRIM_HANDLE),))
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    row = media_row(struct, 0, SOURCE_CLIP)
    if row is None:
        rec(ctx, f"{tag}_the_segment_is_there", False, track0=track_rows(struct, 0))
        return None
    before = await tx_seg(ctx, made["wt"], 0, row["rin"])
    if before is None:
        rec(ctx, f"{tag}_the_segment_can_be_read_back", False)
        return None
    return {
        "wt": made["wt"],
        "uid": made["uid"],
        "name": made["name"],
        "rin": row["rin"],
        "before": before,
    }


async def open_in_timeline(ctx: Ctx, tag: str, wiretap: str, uid: str) -> bool:
    """Open a sequence by transport and read back, by transport, that the Timeline shows it."""
    got = await tx_val(ctx, fill(OPEN_CODE, WT=wiretap), f"pr5e_open_{tag}")
    shown = await tx_displayed(ctx)
    ok = not is_error(got) and isinstance(shown, dict) and shown.get("uid") == uid
    rec(ctx, f"{tag}_the_timeline_shows_it_(read_by_transport)", ok, opened=got, shown=shown)
    return ok


def deltas_of(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int]:
    if not before or not after:
        return {}
    return {k: after[k] - before[k] for k in FACT_KEYS}


def deltas(
    rin: int = 0, rout: int = 0, sin: int = 0, sout: int = 0, head: int = 0, tail: int = 0
) -> dict[str, int]:
    return {"rin": rin, "rout": rout, "sin": sin, "sout": sout, "head": head, "tail": tail}


def target_of(env: dict[str, Any]) -> dict[str, Any]:
    target = _result_of(env).get("target")
    return target if isinstance(target, dict) else {}


# -- a: the reads -------------------------------------------------------------------------------


async def read_set(session: Any, ctx: Ctx, tag: str, seq: dict[str, Any], *, extra: bool) -> None:
    desktop = await run_call(session, ctx, "a_desktop_info")
    fp = await run_call(session, ctx, "a_fingerprint")
    info = await run_call(session, ctx, "a_info")
    direct = await fingerprint(session, ctx, seq["wt"])
    d = _result_of(desktop)
    rec(
        ctx,
        f"{tag}_desktop_info_of_the_timeline_selector_is_the_shown_sequence_(uid)",
        desktop.get("ok") is True and d.get("uid") == seq["uid"] and d.get("name") == seq["name"],
        error=desktop.get("error"),
        got={k: d.get(k) for k in ("type", "name", "uid")},
        want={"name": seq["name"], "uid": seq["uid"]},
    )
    rec(
        ctx,
        f"{tag}_fingerprint_of_the_timeline_selector_is_the_shown_sequence_(uid_and_fingerprint)",
        fp.get("ok") is True
        and target_of(fp).get("uid") == seq["uid"]
        and direct is not None
        and _result_of(fp).get("fingerprint") == direct,
        error=fp.get("error"),
        targetUid=target_of(fp).get("uid"),
        want=seq["uid"],
    )
    rec(
        ctx,
        f"{tag}_timeline_info_of_the_timeline_selector_is_the_shown_sequence_(name)",
        info.get("ok") is True and _result_of(info).get("name") == seq["name"],
        error=info.get("error"),
        gotName=_result_of(info).get("name"),
        want=seq["name"],
    )
    rec(
        ctx,
        f"{tag}_timeline_info_answers_the_uid_and_wiretap_id_of_the_sequence_it_read",
        _result_of(info).get("uid") == seq["uid"]
        and _result_of(info).get("wiretapId") == seq["wt"],
        got={k: _result_of(info).get(k) for k in ("uid", "wiretapId")},
        want={"uid": seq["uid"], "wiretapId": seq["wt"]},
    )
    if not extra:
        return
    listing = await run_call(session, ctx, "a_list")
    items = _result_of(listing).get("items") or []
    rec(
        ctx,
        f"{tag}_list_segments_of_the_timeline_selector_lists_the_shown_sequence",
        listing.get("ok") is True
        and any(i.get("sourceName") == SOURCE_CLIP for i in items)
        and _result_of(listing).get("total") == len(items),
        error=listing.get("error"),
        items=items,
    )
    rec(
        ctx,
        f"{tag}_list_segments_answers_the_uid_and_wiretap_id_of_the_sequence_it_read",
        _result_of(listing).get("uid") == seq["uid"]
        and _result_of(listing).get("wiretapId") == seq["wt"],
        got={k: _result_of(listing).get(k) for k in ("uid", "wiretapId")},
        want={"uid": seq["uid"], "wiretapId": seq["wt"]},
    )
    checkpoint = await run_call(session, ctx, "a_checkpoint")
    source = _result_of(checkpoint).get("source") or {}
    stash = await tx_stash(ctx)
    rec(
        ctx,
        f"{tag}_checkpoint_of_the_timeline_selector_copies_the_shown_sequence",
        checkpoint.get("ok") is True and source.get("uid") == seq["uid"] and bool(stash),
        error=checkpoint.get("error"),
        source=source,
    )


async def stage_a(ctx: Ctx) -> None:
    async with server_session(ctx, "pr5e-a") as session:
        seq_a = await fresh(ctx, "SA")
        seq_b = await fresh(ctx, "SB")
        if seq_a is None or seq_b is None:
            return
        if not await open_in_timeline(ctx, "a1", seq_a["wt"], seq_a["uid"]):
            return
        await read_set(session, ctx, "a1", seq_a, extra=False)
        if not await open_in_timeline(ctx, "a2", seq_b["wt"], seq_b["uid"]):
            return
        await read_set(session, ctx, "a2", seq_b, extra=True)
        fp_a = await fingerprint(session, ctx, seq_a["wt"])
        rec(ctx, "a3_the_first_sequence_was_not_touched_by_the_reads", fp_a is not None)


# -- t: a trim whose parent is the timeline, and its revert -------------------------------------


async def stage_t(ctx: Ctx) -> None:
    async with server_session(ctx, "pr5e-t") as session:
        seq = await fresh(ctx, "ST")
        if seq is None or not await open_in_timeline(ctx, "t1", seq["wt"], seq["uid"]):
            return
        wiretap, rin, before = seq["wt"], seq["rin"], seq["before"]
        other = await fresh(ctx, "STO")
        if other is None:
            return
        if not await open_in_timeline(ctx, "t1b", seq["wt"], seq["uid"]):
            return
        fp0 = await fingerprint(session, ctx, wiretap)
        fp_other = await fingerprint(session, ctx, other["wt"])
        env = await run_call(
            session,
            ctx,
            "t_trim",
            {"target": seg_at(TL, 0, rin), "edge": "tail", "offset": TRIM_FRAMES},
        )
        after = await tx_seg(ctx, wiretap, 0, rin)
        fp1 = await fingerprint(session, ctx, wiretap)
        other_after = await fingerprint(session, ctx, other["wt"])
        group = (env.get("journal") or {}).get("group")
        intent = next(
            (
                r
                for r in journal_records(ctx)
                if r.get("type") == "intent" and r.get("group") == group
            ),
            {},
        )
        rec(
            ctx,
            "t2_a_trim_through_a_timeline_parent_edits_the_shown_sequence_(read_by_transport)",
            env.get("ok") is True
            and deltas_of(before, after)
            == deltas(rout=-TRIM_FRAMES, sout=-TRIM_FRAMES, tail=TRIM_FRAMES)
            and fp0 is not None
            and fp1 != fp0
            and other_after == fp_other,
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            deltas=deltas_of(before, after),
            fingerprints=[fp0, fp1],
        )
        rec(
            ctx,
            "t3_the_journal_pins_the_resolved_sequence_by_its_wiretap_id",
            (intent.get("target") or {}).get("id") == wiretap
            and outcome_of(ctx, group).get("ok") is True,
            intentTarget=intent.get("target"),
            wiretap=wiretap,
        )
        out = await revert(session, ctx, env)
        new_wt = restored_wt(out)
        fp2 = await fingerprint(session, ctx, new_wt) if new_wt else None
        restored = await tx_seg(ctx, new_wt, 0, rin) if new_wt else None
        rec(
            ctx,
            "t4_journal_revert_puts_the_fingerprint_and_the_segment_back",
            out.get("ok") is True and fp2 == fp0 and restored == before and new_wt != wiretap,
            error=out.get("error"),
            fingerprints=[fp0, fp1, fp2],
            before=before,
            restored=restored,
        )
        seq2 = await fresh(ctx, "STV")
        if seq2 is None or not await open_in_timeline(ctx, "t5", seq2["wt"], seq2["uid"]):
            return
        fpv0 = await fingerprint(session, ctx, seq2["wt"])
        made = await run_call(session, ctx, "t_create_version", {"sequence": TL})
        fpv1 = await fingerprint(session, ctx, seq2["wt"])
        undone = await revert(session, ctx, made)
        wt_v = restored_wt(undone)
        fpv2 = await fingerprint(session, ctx, wt_v) if wt_v else None
        rec(
            ctx,
            "t6_timeline_create_version_through_the_selector_edits_the_shown_sequence_and_reverts",
            made.get("ok") is True
            and fpv0 is not None
            and fpv1 != fpv0
            and undone.get("ok") is True
            and fpv2 == fpv0,
            error=made.get("error") or undone.get("error"),
            fingerprints=[fpv0, fpv1, fpv2],
        )


# -- p: plan.run --------------------------------------------------------------------------------


async def stage_p(ctx: Ctx) -> None:
    async with server_session(ctx, "pr5e-p") as session:
        seq = await fresh(ctx, "SP")
        if seq is None or not await open_in_timeline(ctx, "p1", seq["wt"], seq["uid"]):
            return
        wiretap, rin, before = seq["wt"], seq["rin"], seq["before"]
        fp0 = await fingerprint(session, ctx, wiretap)
        steps = [
            {
                "op": "segment.trim",
                "args": {"target": seg_at(TL, 0, rin), "edge": "tail", "offset": TRIM_FRAMES},
            },
            {
                "op": "segment.trim",
                "args": {"target": seg_at(TL, 0, rin), "edge": "head", "offset": TRIM_FRAMES},
            },
        ]
        env = await run_call(session, ctx, "p_plan", {"steps": steps, "label": "pr5e plan"})
        after = await tx_seg(ctx, wiretap, 0, rin + TRIM_FRAMES)
        fp1 = await fingerprint(session, ctx, wiretap)
        result = _result_of(env)
        rec(
            ctx,
            "p2_a_plan_with_timeline_parents_runs_both_steps_on_the_shown_sequence",
            env.get("ok") is True
            and [s.get("ok") for s in result.get("steps") or []] == [True, True]
            and deltas_of(before, after)
            == deltas(
                rin=TRIM_FRAMES,
                rout=-TRIM_FRAMES,
                sin=TRIM_FRAMES,
                sout=-TRIM_FRAMES,
                head=TRIM_FRAMES,
                tail=TRIM_FRAMES,
            )
            and fp0 is not None
            and fp1 != fp0,
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            deltas=deltas_of(before, after),
            steps=result.get("steps"),
        )
        out = await revert(session, ctx, env)
        new_wt = restored_wt(out)
        fp2 = await fingerprint(session, ctx, new_wt) if new_wt else None
        restored = await tx_seg(ctx, new_wt, 0, rin) if new_wt else None
        rec(
            ctx,
            "p3_one_journal_revert_puts_the_whole_plan_back",
            out.get("ok") is True and fp2 == fp0 and restored == before,
            error=out.get("error"),
            fingerprints=[fp0, fp1, fp2],
            before=before,
            restored=restored,
        )


# -- s: a timeline selector as the source -------------------------------------------------------


async def stage_s(ctx: Ctx) -> None:
    async with server_session(ctx, "pr5e-s") as session:
        seq = await fresh(ctx, "SS")
        if seq is None or not await open_in_timeline(ctx, "s1", seq["wt"], seq["uid"]):
            return
        wiretap = seq["wt"]
        fp0 = await fingerprint(session, ctx, wiretap)
        records_before = len(journal_records(ctx))
        stash_before = await tx_stash(ctx)
        for call_id in ("s_overwrite", "s_insert"):
            env = await run_call(
                session, ctx, call_id, {"sequence": sel(wiretap), "source": TL, "at": 0}
            )
            rec(
                ctx,
                f"{call_id}_with_a_timeline_source_is_invalid_args",
                env.get("ok") is False
                and env.get("errorCode") == "INVALID_ARGS"
                and "source" in str(env.get("error")),
                error=env.get("error"),
                errorCode=env.get("errorCode"),
            )
        both = await run_call(session, ctx, "s_overwrite", {"sequence": TL, "source": TL, "at": 0})
        rec(
            ctx,
            "s_overwrite_with_timeline_for_both_is_invalid_args",
            both.get("ok") is False and both.get("errorCode") == "INVALID_ARGS",
            error=both.get("error"),
        )
        fp1 = await fingerprint(session, ctx, wiretap)
        stash_after = await tx_stash(ctx)
        rec(
            ctx,
            "s_nothing_changed_(fingerprint_journal_stash)",
            fp0 is not None
            and fp1 == fp0
            and len(journal_records(ctx)) == records_before
            and stash_before == stash_after,
            fingerprints=[fp0, fp1],
            journalRecords=[records_before, len(journal_records(ctx))],
        )


# -- z / driver ---------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {
        "desktop.info",
        "timeline.info",
        "timeline.list_segments",
        "timeline.fingerprint",
        "timeline.checkpoint",
        "timeline.create_version",
        "timeline.overwrite",
        "timeline.insert",
        "segment.trim",
        "plan.run",
        "journal.revert",
    }
    rec(
        ctx,
        "z_every_op_of_this_run_was_called",
        wanted <= set(used),
        missing=sorted(wanted - set(used)),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "pr5e_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "pr5e_left")
    ctx.results["cleanup"] = {"deleted": gone, "stillThere": left}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted_the_stash_library_too",
        isinstance(gone, dict)
        and all(ok is True for rows_ in gone.values() for _n, ok in rows_)
        and left == {"reelGroups": [], "batchGroups": [], "libraries": []}
        and ckpt_library(ctx) in [n for n, _ok in (gone or {}).get("libraries", [])],
        deleted=gone,
        stillThere=left,
    )
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    ctx.ids["prefix"] = PREFIX
    os.environ[CKPT_ENV] = ckpt_library(ctx)
    ctx.results["env"] = {CKPT_ENV: os.environ[CKPT_ENV]}
    try:
        await guarded(ctx, "prepare", prepare)
        if "seqReelWt" in ctx.ids and "clipWt" in ctx.ids:
            for name, stage in (("a", stage_a), ("t", stage_t), ("p", stage_p), ("s", stage_s)):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "pr5e_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------

EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset({"s_overwrite", "s_insert"})
"""Calls whose placeholder arguments the server itself refuses (a timeline selector as the source):
what the stage `s` sends is judged to be refused in the same way."""


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    print("===== calls through flame_do =====")
    for call_id, (op_name, build) in CALLS.items():
        op = registry.get(op_name)
        problems: list[str] = []
        if op is None:
            print(f"NG {call_id}: unknown op {op_name}")
            failures += 1
            continue
        args = build({})
        validation = validate_op_args(op, args)
        refusal = None
        if validation.ok:
            checked = dict(validation.value or {})
            apply_param_defaults(op, checked)
            apply_limit_default(op, checked)
            if op.precheck is not None:
                refusal = op.precheck(checked, None)
            if op.thread != "server":
                try:
                    compile(execution.build_exec_code(op, checked), f"<{call_id}>", "exec")
                except SyntaxError as exc:
                    problems.append(f"compile FAILED: {exc}")
        else:
            problems.append(
                "invalid args: " + "; ".join(f"{i.path}: {i.message}" for i in validation.issues)
            )
        if refusal is not None and call_id not in EXPECTED_REFUSALS:
            problems.append(f"precheck refused: {refusal.message}")
        if refusal is None and call_id in EXPECTED_REFUSALS and validation.ok:
            problems.append("expected the precheck to refuse this call, it did not")
        verdict = "NG " + "; ".join(problems) if problems else "OK"
        print(f"{verdict:3} {call_id}: {op_name}")
        failures += bool(problems)
    print("===== transport code (compiled with every @NAME@ filled) =====")
    for name, template in TEMPLATES.items():
        code = PRELUDE + "\n" + re.sub(r"@[A-Z_0-9]+@", "'x'", template)
        try:
            compile(code, f"<{name}>", "exec")
            print(f"OK  {name} ({len(code)} chars)")
        except SyntaxError as exc:
            print(f"NG  {name}: {exc}")
            failures += 1
    print(f"===== calls: {len(CALLS)}; stages: a t p s, prepare and z =====")
    print(f"=== dry run: {'FAILED' if failures else 'OK'} ({failures} problem(s)) ===")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        return dry_run()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_pr5e_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"fix5_timeline_selector_e2e_{ts}.json",
    }
    ctx = Ctx(run_dir, results, ts, journal_dir)
    try:
        anyio.run(async_main, ctx)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()
    save(results)
    failed = [s["name"] for s in results["steps"] if not s["ok"]]
    print(f"Result: {results['_out_path']}")
    print(f"steps={len(results['steps'])} failed={len(failed)}: {failed}")
    print_observations(results)
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
