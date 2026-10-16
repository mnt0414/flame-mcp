#!/usr/bin/env python3
"""Real-Flame end-to-end check of `segment.trim` / `segment.slip` with a Timewarp.

Checks: `segment.trim` and `segment.slip` on a segment that has a Timewarp.

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

Run it with the `.venv` Python; `scripts/run_e2e.sh w3_timewarp` starts Flame; `--dry-run`
connects to nothing and only lists the calls and compiles the code.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test. Everything this run makes is named `PTW3E_<ts>_...` and is
deleted at the end: the reel group, the library and the stash library (`PTW3E_<ts>_CKPT`, passed
to every server as FLAME_MCP_CHECKPOINT_LIBRARY); then flame.exit(). What stays on disk: the copy
of the setup (`p3/files_ptw3e_<ts>/setup/`), this run's journal directory
(`p3/journal_ptw3e_e2e_<ts>/`) and the result files. No test hook is set for any server.

The setup is a Timewarp made in the UI (Timing curve, speed 80, Range End 44), read from
`$FLAME_MCP_W3_SETUP` (there is no default) and COPIED into the run's folder first (the
original is only read, and is not part of the repository). Without that variable, or without the
file it names, the run stops with SKIP (exit code 3) and touches nothing.

JUDGED steps: the Timewarp setup shrinks the source of a 44-frame segment to 35 frames; every
trim / slip below is ok, says `timewarp: true` / `sourceJudged: false`, moves the record side,
head and tail 1:1 (read back by transport) and reports the deltas as transport read them; one
revert puts the sequence back; the negative control (no Timewarp) still judges the source side.
OBSERVED steps (`kind: "observation"`, always ok) record how far source_in / source_out really
moved (the rounding is not known).

Stages (material: scripts/make_p2_media.sh; p2_len72 trimmed head 14 / tail 14 = 44 frames)
  t  six cases, each on a fresh sequence: segment.create_effect Timewarp, segment.effect_load_setup,
     segment.info (source 35 frames), then trim tail +3 / -3, head +3 / -3, slip +2 / -2.
  r  trim tail +3 then journal.revert: the sequence and the segment (source range too) are back.
  n  the negative control: no Timewarp, trim tail +3 and slip +2 are judged on the source side.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import e2e_guard  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    MEDIA_DIR,
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
    _br_dict,
    _result_of,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import Ctx, guarded, rec, save, save_stage, server_session, tx  # noqa: E402
from p2_4_common import flame_exit, observe, ops_used, print_observations  # noqa: E402
from p3_1a_read_e2e import PRELUDE, STRUCT_CODE, fill, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    CKPT_ENV,
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
    tx_seg,
    tx_struct,
)
from p3_3_e2e_common import (  # noqa: E402
    MAKE_SEQ_TEMPLATE,
    make_seq,
    op_time,
    restored_wt,
    revert,
)

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

PREFIX: Final[str] = "PTW3E"
SKIP_EXIT_CODE: Final[int] = 3
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("clip1", MEDIA_DIR / "clip1.mov"),
    ("p2_len72", MEDIA_DIR / "p2" / "p2_len72.mov"),
)
"""clip1 is only there for the frame rate and size the sequences are made with."""
SETUP_ENV: Final[str] = "FLAME_MCP_W3_SETUP"
"""Names the setup file; scripts/run_e2e.sh reads the same variable for its SKIP check."""


def resolve_setup_src(environ: Mapping[str, str] | None = None) -> Path | None:
    """Returns $FLAME_MCP_W3_SETUP as a path, or None if it is unset or empty."""
    value = (os.environ if environ is None else environ).get(SETUP_ENV, "")
    return Path(value) if value else None


SETUP_SRC: Path | None = resolve_setup_src()
SOURCE_CLIP: Final[str] = "p2_len72"
TRIM_HANDLE: Final[int] = 14
"""trim_head / trim_tail left on p2_len72 (72 frames): 72 - 14 - 14 = 44 record frames, the length
of the segment the setup was made on."""
RECORD_FRAMES: Final[int] = 44
SOURCE_FRAMES_AFTER_LOAD: Final[int] = 35
"""What the setup does to a 44-frame segment (source 14-57 -> 16-50)."""
TRIM_FRAMES: Final[int] = 3
SLIP_FRAMES: Final[int] = 2
FX_TYPE: Final[str] = "Timewarp"
FACT_KEYS: Final[tuple[str, ...]] = ("rin", "rout", "sin", "sout", "head", "tail")
OP_KEYS: Final[dict[str, str]] = {
    "rin": "recordIn",
    "rout": "recordOut",
    "sin": "sourceIn",
    "sout": "sourceOut",
    "head": "head",
    "tail": "tail",
}
OP_TIME_KEYS: Final[frozenset[str]] = frozenset({"rin", "rout", "sin", "sout"})


def deltas(
    rin: int = 0, rout: int = 0, sin: int = 0, sout: int = 0, head: int = 0, tail: int = 0
) -> dict[str, int]:
    return {"rin": rin, "rout": rout, "sin": sin, "sout": sout, "head": head, "tail": tail}


CASES: Final[tuple[tuple[str, str, int, dict[str, int]], ...]] = (
    ("t1_tail_plus_3", "trim_tail", TRIM_FRAMES, deltas(rout=-3, tail=3)),
    ("t2_tail_minus_3", "trim_tail", -TRIM_FRAMES, deltas(rout=3, tail=-3)),
    ("t3_head_plus_3", "trim_head", TRIM_FRAMES, deltas(rin=3, head=3)),
    ("t4_head_minus_3", "trim_head", -TRIM_FRAMES, deltas(rin=-3, head=-3)),
    ("t5_slip_plus_2", "slip", SLIP_FRAMES, deltas(head=-2, tail=2)),
    ("t6_slip_minus_2", "slip", -SLIP_FRAMES, deltas(head=2, tail=-2)),
)
"""(case, kind, offset, the change of the record side, head and tail -- source_in / source_out are
left out: they are not judged). Each case runs on a fresh sequence, so one edit never starts from
another's result."""
RECORD_SIDE: Final[tuple[str, ...]] = ("rin", "rout", "head", "tail")

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def declare(call_id: str, op: str, sample: dict[str, Any]) -> None:
    """`CALLS[call_id] = (op, build)`: `build(ids)` answers `ids["args"]` when a stage supplies the
    arguments and `sample` (a well-formed set, for `--dry-run`) otherwise."""

    def build(ids: dict[str, Any]) -> dict[str, Any]:
        args = ids.get("args")
        return dict(args) if isinstance(args, dict) else dict(sample)

    CALLS[call_id] = (op, build)


_WT = sel("/wt/x")
_SEG = seg_at(_WT, 0, 14)

declare("t_create_effect", "segment.create_effect", {"target": _SEG, "type": FX_TYPE})
declare(
    "t_load",
    "segment.effect_load_setup",
    {"target": _SEG, "type": FX_TYPE, "path": "/x/setup.timewarp_node"},
)
declare("t_info", "segment.info", {"target": _SEG})
declare("t_trim", "segment.trim", {"target": _SEG, "edge": "tail", "offset": 3})
declare("t_slip", "segment.slip", {"target": _SEG, "offset": 2})

TEMPLATES: Final[dict[str, str]] = {
    "prepare": PREPARE_CODE,
    "make_seq": MAKE_SEQ_TEMPLATE,
    "seg_facts": SEG_FACTS_CODE,
    "struct": STRUCT_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def run_call(session: Any, ctx: Ctx, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
    op, _build = CALLS[call_id]
    return await call(session, ctx, op, args)


# -- reading and judging ------------------------------------------------------------------------


def facts_deltas(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int]:
    if not before or not after:
        return {}
    return {k: after[k] - before[k] for k in FACT_KEYS}


def op_deltas_as_facts(result: dict[str, Any]) -> dict[str, Any]:
    """The op's `deltas` under the keys transport uses."""
    got = result.get("deltas") or {}
    return {k: got.get(name) for k, name in OP_KEYS.items()}


def op_side_as_facts(side: Any) -> dict[str, Any] | None:
    """The op's `before` / `after` (PyTime as {timecode, frame}) under transport's keys."""
    if not isinstance(side, dict):
        return None
    return {
        k: op_time(side.get(name)) if k in OP_TIME_KEYS else side.get(name)
        for k, name in OP_KEYS.items()
    }


def numbers_of(facts: dict[str, Any] | None) -> dict[str, Any] | None:
    """The six numbers of transport's facts (it also reads the index, the type, ...)."""
    return None if facts is None else {k: facts[k] for k in FACT_KEYS}


def source_frames(facts: dict[str, Any] | None) -> int | None:
    return None if not facts else facts["sout"] - facts["sin"] + 1


def record_frames(facts: dict[str, Any] | None) -> int | None:
    return None if not facts else facts["rout"] - facts["rin"] + 1


# -- material -----------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    files_dir = OUTPUT_DIR / f"files_ptw3e_{ctx.ts}"
    setup_dir = files_dir / "setup"
    setup_dir.mkdir(parents=True, exist_ok=True)
    setup_copy = setup_dir / SETUP_SRC.name
    shutil.copy2(SETUP_SRC, setup_copy)
    ids = ctx.ids
    ids.update(filesDir=str(files_dir), setupCopy=str(setup_copy))
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
        label="ptw3e_prepare",
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


async def target(session: Any, ctx: Ctx, tag: str, *, timewarp: bool) -> dict[str, Any] | None:
    """A fresh sequence: p2_len72 on track 0 trimmed to 44 frames (head / tail 14), with a Timewarp
    set up from the setup file when `timewarp` (create_effect, then effect_load_setup)."""
    made = await make_seq(ctx, tag, ((0, SOURCE_CLIP, 1),), trims=((0, TRIM_HANDLE, TRIM_HANDLE),))
    if made is None:
        return None
    wiretap = made["wt"]
    struct = await tx_struct(ctx, wiretap)
    row = media_row(struct, 0, SOURCE_CLIP)
    if row is None:
        rec(ctx, f"{tag}_the_segment_is_there", False, track0=track_rows(struct, 0))
        return None
    rin = row["rin"]
    seg = seg_at(sel(wiretap), 0, rin)
    if timewarp:
        created = await run_call(session, ctx, "t_create_effect", {"target": seg, "type": FX_TYPE})
        loaded = await run_call(
            session,
            ctx,
            "t_load",
            {"target": seg, "type": FX_TYPE, "path": ctx.ids["setupCopy"]},
        )
        rec(
            ctx,
            f"{tag}_the_timewarp_is_created_and_its_setup_loaded",
            created.get("ok") is True and loaded.get("ok") is True,
            created=created.get("error"),
            loaded=loaded.get("error"),
        )
        if created.get("ok") is not True or loaded.get("ok") is not True:
            return None
    facts = await tx_seg(ctx, wiretap, 0, rin)
    if facts is None:
        rec(ctx, f"{tag}_the_segment_can_be_read_back", False)
        return None
    return {"wt": wiretap, "rin": rin, "seg": seg, "before": facts}


def call_for_case(kind: str, offset: int, seg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if kind == "slip":
        return "t_slip", {"target": seg, "offset": offset}
    edge = "tail" if kind == "trim_tail" else "head"
    return "t_trim", {"target": seg, "edge": edge, "offset": offset}


# -- t: the six cases ---------------------------------------------------------------------------


async def check_shrunk(
    session: Any, ctx: Ctx, tag: str, seg: dict[str, Any], before: dict[str, Any]
) -> None:
    info = await run_call(session, ctx, "t_info", {"target": seg})
    info_result = _result_of(info)
    rec(
        ctx,
        f"{tag}_the_setup_shrank_the_source_to_35_frames_and_the_segment_has_a_timewarp",
        info.get("ok") is True
        and info_result.get("effects") == [FX_TYPE]
        and record_frames(before) == RECORD_FRAMES
        and source_frames(before) == SOURCE_FRAMES_AFTER_LOAD
        and op_time(info_result.get("sourceIn")) == before["sin"]
        and op_time(info_result.get("sourceOut")) == before["sout"]
        and min(before["head"], before["tail"]) >= TRIM_FRAMES,
        before=before,
        info={k: info_result.get(k) for k in ("effects", "sourceIn", "sourceOut", "head", "tail")},
        error=info.get("error"),
    )


def check_result(
    ctx: Ctx,
    tag: str,
    env: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any] | None,
    want: dict[str, int],
) -> dict[str, int]:
    """The two judged steps on what the op answered; answers transport's deltas."""
    res = _result_of(env)
    got = facts_deltas(before, after)
    want_record = {k: want[k] for k in RECORD_SIDE}
    rec(
        ctx,
        f"{tag}_is_ok_and_the_record_side_head_and_tail_moved_1_to_1_(read_back_by_transport)",
        env.get("ok") is True and bool(got) and {k: got.get(k) for k in RECORD_SIDE} == want_record,
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        want=want_record,
        got=got,
        before=before,
        after=after,
    )
    expected = res.get("expectedDeltas") or {}
    rec(
        ctx,
        f"{tag}_the_result_says_timewarp_and_the_source_side_was_not_judged",
        env.get("ok") is True
        and res.get("timewarp") is True
        and res.get("sourceJudged") is False
        and expected.get("sourceIn") is None
        and expected.get("sourceOut") is None
        and op_deltas_as_facts(res) == got
        and op_side_as_facts(res.get("before")) == numbers_of(before)
        and op_side_as_facts(res.get("after")) == numbers_of(after),
        result={k: res.get(k) for k in ("timewarp", "sourceJudged", "deltas", "expectedDeltas")},
        transport=got,
    )
    return got


def check_journal(ctx: Ctx, tag: str, env: dict[str, Any]) -> None:
    journal = env.get("journal") or {}
    outcome = outcome_of(ctx, journal.get("group"))
    rec(
        ctx,
        f"{tag}_the_journal_holds_a_checkpoint_and_the_fingerprints",
        bool(journal.get("checkpoint"))
        and journal.get("changed") is True
        and outcome.get("ok") is True
        and outcome.get("fingerprintAfter") is not None,
        journal=journal,
    )


async def timewarp_case(
    session: Any,
    ctx: Ctx,
    tag: str,
    kind: str,
    offset: int,
    want: dict[str, int],
) -> None:
    case = await target(session, ctx, tag, timewarp=True)
    if case is None:
        return
    wiretap, rin, seg, before = case["wt"], case["rin"], case["seg"], case["before"]
    await check_shrunk(session, ctx, tag, seg, before)
    call_id, args = call_for_case(kind, offset, seg)
    env = await run_call(session, ctx, call_id, args)
    after = await tx_seg(ctx, wiretap, 0, rin + (offset if kind == "trim_head" else 0))
    got = check_result(ctx, tag, env, before, after, want)
    check_journal(ctx, tag, env)
    observe(
        ctx,
        f"{tag}_how_far_the_source_moved",
        offset=offset,
        sourceIn=got.get("sin"),
        sourceOut=got.get("sout"),
        returned=_result_of(env).get("returned"),
    )


async def stage_t(ctx: Ctx) -> None:
    async with server_session(ctx, "ptw3e-t") as session:
        for tag, kind, offset, want in CASES:
            await timewarp_case(session, ctx, tag, kind, offset, want)


# -- r: a revert --------------------------------------------------------------------------------


async def stage_r(ctx: Ctx) -> None:
    async with server_session(ctx, "ptw3e-r") as session:
        case = await target(session, ctx, "TWR", timewarp=True)
        if case is None:
            return
        wiretap, rin, seg, before = case["wt"], case["rin"], case["seg"], case["before"]
        fp0 = await fingerprint(session, ctx, wiretap)
        env = await run_call(
            session, ctx, "t_trim", {"target": seg, "edge": "tail", "offset": TRIM_FRAMES}
        )
        fp1 = await fingerprint(session, ctx, wiretap)
        out = await revert(session, ctx, env)
        new_wt = restored_wt(out)
        after = await tx_seg(ctx, new_wt, 0, rin) if new_wt else None
        fp2 = await fingerprint(session, ctx, new_wt) if new_wt else None
        rec(
            ctx,
            "r1_a_revert_puts_the_timewarp_sequence_back_the_fingerprint_and_the_segment_too",
            env.get("ok") is True
            and out.get("ok") is True
            and fp0 is not None
            and fp1 != fp0
            and fp2 == fp0
            and after == before
            and new_wt != wiretap,
            error=out.get("error"),
            fingerprints=[fp0, fp1, fp2],
            before=before,
            after=after,
        )


# -- n: the negative control --------------------------------------------------------------------


async def stage_n(ctx: Ctx) -> None:
    async with server_session(ctx, "ptw3e-n") as session:
        case = await target(session, ctx, "TWN", timewarp=False)
        if case is None:
            return
        wiretap, rin, seg, before = case["wt"], case["rin"], case["seg"], case["before"]
        info = await run_call(session, ctx, "t_info", {"target": seg})
        rec(
            ctx,
            "n1_the_control_has_no_timewarp_and_its_source_is_as_long_as_its_record",
            info.get("ok") is True
            and _result_of(info).get("effects") == []
            and source_frames(before) == record_frames(before) == RECORD_FRAMES,
            before=before,
            effects=_result_of(info).get("effects"),
        )
        steps = (
            ("n2_trim_tail_plus_3", "t_trim", {"edge": "tail", "offset": TRIM_FRAMES}),
            ("n3_slip_plus_2", "t_slip", {"offset": SLIP_FRAMES}),
        )
        previous = before
        for tag, call_id, extra in steps:
            env = await run_call(session, ctx, call_id, {"target": seg, **extra})
            after = await tx_seg(ctx, wiretap, 0, rin)
            res = _result_of(env)
            got = facts_deltas(previous, after)
            want = (
                deltas(rout=-TRIM_FRAMES, sout=-TRIM_FRAMES, tail=TRIM_FRAMES)
                if call_id == "t_trim"
                else deltas(
                    sin=-SLIP_FRAMES, sout=-SLIP_FRAMES, head=-SLIP_FRAMES, tail=SLIP_FRAMES
                )
            )
            rec(
                ctx,
                f"{tag}_is_ok_and_judged_on_the_source_side_as_before",
                env.get("ok") is True
                and got == want
                and res.get("timewarp") is False
                and res.get("sourceJudged") is True
                and op_deltas_as_facts(res) == got
                and all(v is not None for v in (res.get("expectedDeltas") or {}).values()),
                error=env.get("error"),
                want=want,
                got=got,
                result={
                    k: res.get(k) for k in ("timewarp", "sourceJudged", "deltas", "expectedDeltas")
                },
            )
            previous = after or previous


# -- z / driver ---------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {
        "segment.trim",
        "segment.slip",
        "segment.create_effect",
        "segment.effect_load_setup",
        "segment.info",
        "journal.revert",
        "timeline.fingerprint",
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
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "ptw3e_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "ptw3e_left")
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
        if SETUP_SRC is None:
            ctx.results["skipped"] = (
                f"SKIP: {SETUP_ENV} is not set. Point it at the setup file of a segment with a "
                "Timewarp."
            )
            print(ctx.results["skipped"])
            return
        if not SETUP_SRC.is_file():
            ctx.results["skipped"] = (
                f"SKIP: the Timewarp setup is not there: {SETUP_SRC}. Check the path in "
                f"{SETUP_ENV}."
            )
            print(ctx.results["skipped"])
            return
        await guarded(ctx, "prepare", prepare)
        if "seqReelWt" in ctx.ids and "clipWt" in ctx.ids:
            for name, stage in (("t", stage_t), ("r", stage_r), ("n", stage_n)):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "ptw3e_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------

EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset({"t_load"})
"""Calls whose placeholder arguments the server itself refuses (a path that is not there): what a
stage sends in their place is what the run judges."""


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
    if SETUP_SRC is None:
        setup_state = f"NOT SET ({SETUP_ENV})"
    else:
        setup_state = f"{SETUP_SRC} ({'there' if SETUP_SRC.is_file() else 'NOT THERE'})"
    print(f"===== setup file: {setup_state} =====")
    print(f"===== calls: {len(CALLS)}; stages: t r n, prepare and z =====")
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
    journal_dir = OUTPUT_DIR / f"journal_ptw3e_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"w3_timewarp_e2e_{ts}.json",
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
    if "skipped" in results:
        return SKIP_EXIT_CODE
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
