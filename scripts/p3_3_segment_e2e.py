#!/usr/bin/env python3
"""Real-Flame end-to-end: the remaining segment edits -- `segment.slip`, `segment.replace`, the
effect ops (`create_effect`, `delete_effect`, `set_effect`, `effect_save_setup`,
`effect_load_setup`), `segment.copy_to_media_panel` / `segment.match`, and plan.run holding the new
edits (run with the `.venv` Python; `scripts/run_e2e.sh p3_3_segment` starts Flame; `--dry-run`
connects to nothing and only lists the calls and compiles the code).

Checks: `segment.slip`, `segment.replace`, the effect ops (`create_effect`, `delete_effect`,
`set_effect`, `effect_save_setup`, `effect_load_setup`), `segment.copy_to_media_panel`,
`segment.match`, a `plan.run` that holds the new edits, and the Resize Flame adds to a segment when
the bit depth of the sequence differs from that of its media (`timeline.create_sequence` with
`bit_depth`; `segment.delete_effect` refuses that Resize).

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

The helpers of p3_2a_e2e_common.py are imported under the prefix `P33`. A real MCP stdio client
drives `python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport` is scaffolding
only -- it builds the material and READS EVERY STATE BACK INDEPENDENTLY of the ops under test
(segments, effects and their attribute candidates, the clips a call made, the stash library,
find_by_uid). Everything this run makes is named `P33_<ts>_...` and is deleted at the end (the stash
library is `P33_<ts>_CKPT`, passed to every server as FLAME_MCP_CHECKPOINT_LIBRARY; setup files go
to `p3/files_p33_<ts>/`); then flame.exit(). The journal directory is this run's own
(`p3/journal_p33_e2e_<ts>/`). No test hook is set for any server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by
behaviour already measured on a real Flame. OBSERVED steps (`kind: "observation"`, always ok) record
what Flame did where nothing was measured: what smart_replace does to a segment's effects, the setup
files of the effect types whose setup files were not measured, what load_setup does with the setup
of another type, what match(include_timeline_fx=True) makes, and taking a segment out of a sequence
inside a library.

Stages (material: scripts/make_p2_media.sh and make_p2_tc_media.sh)
  s  segment.slip: +3 / -5 on a head 10 / tail 10 segment, read back by transport; past either
     handle, a segment with no handles, a Gap, a library copy and offset 0 refused with nothing
     copied or journaled; a revert puts the fingerprint and the segment back.
  r  segment.replace: media / media_and_fx; a clip too short for the range is SILENT_NOOP and
     nothing changes; the scenario-1 shrink -- p2_src_tc1h replaced by p2_rep_tc1h_plus10 lands 10
     frames late (the OFFSET is kept, not the timecode) and slip(10) brings the source timecode
     back; refusals; a revert.
  e  the effect ops: create (Blur, Burn-in Letterbox, a case-insensitive type, a duplicate, an
     unknown type), set_effect (mode / ratio / colour read back; values outside the candidates or
     the range refused), save_setup (the files per type), load_setup (save, change, load: the
     values are back), delete_effect, the reverts.
  b  the bit depth: a sequence made with `timeline.create_sequence(bit_depth=)` other than its
     media's gets a Resize on the segment (read through `segment.list_effects` and by transport),
     `segment.delete_effect` refuses it and changes nothing, journals nothing; a sequence of the
     media's bit depth gets none, and a Resize `create_effect` put there is deleted.
  x  copy_to_media_panel / match (3 shapes), their reverts, a renamed / edited clip is a
     CONFLICT, a Batch reel is refused.
  p  plan.run: slip + replace + trim + create_effect on one sequence = one checkpoint, one revert;
     a match in a plan is refused.
  y  a READONLY server denies the writes.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Final

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import e2e_guard  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import Ctx, guarded, rec, save, save_stage, server_session  # noqa: E402
from p2_4_common import flame_exit, ops_used, print_observations  # noqa: E402
from p3_1a_read_e2e import PRELUDE, fill, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import CKPT_ENV, ckpt_library  # noqa: E402
from p3_3_e2e_bitdepth import stage_b  # noqa: E402
from p3_3_e2e_common import (  # noqa: E402
    CALLS,
    OUTPUT_DIR,
    PREFIX,
    TEMPLATES,
    Results,
    prepare,
    run_call,
    seg_at,
    sel,
    state_snapshot,
)
from p3_3_e2e_edit import stage_r, stage_s  # noqa: E402
from p3_3_e2e_effects import stage_e  # noqa: E402
from p3_3_e2e_extract import stage_p, stage_x  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

# -- y: the read-only policy


async def stage_y(ctx: Ctx) -> None:
    case = ctx.ids.get("case", {}).get("slip")
    if not case:
        rec(ctx, "y0_a_sequence_to_aim_the_denied_calls_at_is_there", False)
        return
    target = seg_at(sel(case["wt"]), 1, case["rin"])
    files = Path(ctx.ids["filesDir"])
    calls = {
        "y_readonly_slip": {"target": target, "offset": 1},
        "y_readonly_replace": {"target": target, "source": sel(ctx.ids["clipWt"]["clip1"])},
        "y_readonly_create_effect": {"target": target, "type": "Blur"},
        "y_readonly_match": {"target": target, "destination": sel(ctx.ids["ccpWt"])},
        "y_readonly_save": {"target": target, "type": "Blur", "path": str(files / "y_denied")},
    }
    async with server_session(ctx, "p33-y", readonly=True) as session:
        before = await state_snapshot(ctx)
        denied = {
            call_id: (await run_call(session, ctx, call_id, args)).get("errorCode")
            for call_id, args in calls.items()
        }
        rec(
            ctx,
            "y_a_readonly_server_denies_slip_replace_create_effect_match_and_save_setup",
            set(denied.values()) == {"POLICY_DENIED"}
            and await state_snapshot(ctx) == before
            and not list(files.glob("y_denied*")),
            denied=denied,
        )


# -- z / driver


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {
        "segment.slip",
        "segment.replace",
        "segment.create_effect",
        "segment.delete_effect",
        "segment.set_effect",
        "segment.effect_save_setup",
        "segment.effect_load_setup",
        "segment.copy_to_media_panel",
        "segment.match",
        "plan.run",
        "journal.revert",
        "timeline.fingerprint",
        "segment.info",
        "segment.list_effects",
        "timeline.create_sequence",
    }
    rec(
        ctx,
        "z_every_op_of_this_milestone_was_called",
        wanted <= set(used),
        missing=sorted(wanted - set(used)),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p33_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p33_left")
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
            for name, stage in (
                ("s", stage_s),
                ("r", stage_r),
                ("e", stage_e),
                ("b", stage_b),
                ("x", stage_x),
                ("p", stage_p),
                ("y", stage_y),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p33_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run

EXPECTED_VALIDATION_FAILURES: Final[frozenset[str]] = frozenset()
EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset(
    {"s_slip_bad_offset", "e_save", "e_load", "y_readonly_save"}
)
"""Calls whose placeholder arguments the server itself refuses (offset 0, a path that is not there):
what a stage sends in their place is what the run judges."""


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
        elif call_id not in EXPECTED_VALIDATION_FAILURES:
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
    print(f"===== calls: {len(CALLS)}; stages: s r e b x p y, prepare and z =====")
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
    journal_dir = OUTPUT_DIR / f"journal_p33_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p3_3_segment_e2e_{ts}.json",
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
