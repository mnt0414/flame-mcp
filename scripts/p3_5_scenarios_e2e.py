#!/usr/bin/env python3
"""Real-Flame end-to-end: the two editing scenarios (EDL replace with the source timecode kept; a
shot taken out with its handles) run as a plain chain of ops, with no `eval.run` and no write by
transport inside a scenario (run with the `.venv` Python; `scripts/run_e2e.sh p3_5_scenarios`
starts Flame; `--dry-run` connects to nothing and only lists the calls and compiles the code).

Checks: Two editing scenarios run as a plain chain of ops: replacing media from an EDL while keeping
the source timecode, and taking a shot out with its handles.

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` makes the material (the reel group, the library, the clips, the
sequences) and READS EVERY STATE BACK INDEPENDENTLY of the ops under test; it writes nothing else.
Everything this run makes is named `P35_<ts>_...` and is deleted at the end (the stash library is
`P35_<ts>_CKPT`, passed to every server as FLAME_MCP_CHECKPOINT_LIBRARY; setup files go to
`p3/files_p35_<ts>/`); then flame.exit(). The journal directory is this run's own
(`p3/journal_p35_e2e_<ts>/`). No test hook is set for any server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by what
was measured on a real Flame beforehand. OBSERVED steps (`kind: "observation"`, always ok) record
what Flame did where nothing was measured: the `duration` / `start_frame` / marks of a clip whose
inner segment was extended, and the source names after the chain.

Stages (material: scripts/make_p2_media.sh and make_p2_tc_media.sh)
  a  scenario 1: library.list -> desktop.list_children -> mediahub.import (the replacement into a
     library reel) -> desktop.find -> timeline.list_segments -> segment.info -> desktop.copy (out of
     the library) -> plan.run[segment.replace x 2] -> segment.slip x 2 (the source timecode of
     each segment is the one it had) -> segment.create_effect -> segment.effect_save_setup ->
     segment.set_effect -> segment.effect_load_setup -> journal.revert of every group, newest
     first: the fingerprint is the first one again.
  n  the library protection: desktop.move and desktop.delete of the library clip are
     refused and change nothing; the copy and the import this run made are reverted.
  b  scenario 2: timeline.list_segments -> segment.info -> segment.copy_to_media_panel (the used
     range with soft handles) -> segment.trim x 2 on the clip's inner segment (to the handle
     length) -> desktop.rename -> a read back -> journal.revert.
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
from p3_2a_e2e_common import CKPT_ENV  # noqa: E402
from p3_5_e2e_common import (  # noqa: E402
    CALLS,
    OUTPUT_DIR,
    PREFIX,
    TEMPLATES,
    Results,
    ckpt_library,
    ops_via_plan,
    prepare,
)
from p3_5_library import stage_n  # noqa: E402
from p3_5_scenario1 import stage_a  # noqa: E402
from p3_5_scenario2 import stage_b  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

__all__ = ["server_session"]
STAGES = (("a", stage_a), ("n", stage_n), ("b", stage_b))
WANTED_OPS: Final[frozenset[str]] = frozenset(
    {
        "library.list",
        "desktop.list_children",
        "mediahub.import",
        "desktop.find",
        "timeline.list_segments",
        "segment.info",
        "desktop.copy",
        "desktop.move",
        "desktop.delete",
        "plan.run",
        "segment.replace",
        "segment.slip",
        "segment.create_effect",
        "segment.effect_save_setup",
        "segment.set_effect",
        "segment.effect_load_setup",
        "segment.copy_to_media_panel",
        "segment.trim",
        "desktop.rename",
        "journal.revert",
        "timeline.fingerprint",
    }
)
FORBIDDEN_OPS: Final[frozenset[str]] = frozenset({"eval.run"})
"""The point of the run: the two scenarios are plain op chains."""


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    via_plan = ops_via_plan(ctx.results)
    ctx.results["opsViaPlan"] = via_plan
    covered = set(used) | set(via_plan)
    rec(
        ctx,
        "z_every_op_of_the_two_scenarios_was_called_and_eval_run_never_was",
        WANTED_OPS <= covered and not FORBIDDEN_OPS & covered,
        missing=sorted(WANTED_OPS - covered),
        forbiddenUsed=sorted(FORBIDDEN_OPS & covered),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p35_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p35_left")
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
            for name, stage in STAGES:
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p35_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------

EXPECTED_VALIDATION_FAILURES: Final[frozenset[str]] = frozenset()
EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset({"a_import", "a_save_setup", "a_load_setup"})
"""Calls whose placeholder arguments the server itself refuses: what a stage sends in their place
is what the run judges."""


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
    stages = " ".join(name for name, _stage in STAGES)
    print(f"===== calls: {len(CALLS)}; stages: {stages}, prepare and z =====")
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
    journal_dir = OUTPUT_DIR / f"journal_p35_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p3_5_scenarios_e2e_{ts}.json",
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
