#!/usr/bin/env python3
"""Real-Flame end-to-end: plan.run's bundle of Timeline writes (one checkpoint, segment
selectors resolved up front, `late`, mixed writes refused, one revert), `desktop.delete` (and its
revert), `desktop.move`'s Timeline handling, `journal.purge_checkpoints`, and `journal.check` of the
new intents (run with the `.venv` Python; `scripts/run_e2e.sh p3_2b_plan_delete` starts Flame;
`--dry-run` connects to nothing and only lists the calls and compiles the code).

Checks: `plan.run` with Timeline writes (one checkpoint, one revert), `desktop.delete` and its
revert, how `desktop.move` handles a Timeline, `journal.purge_checkpoints`, and `journal.check` of
the new intents.

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

The material and helpers are imported from p3_2a_e2e_common.py under the prefix `P32B`. A real MCP
stdio client drives `python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport`
is scaffolding only -- it builds the material and READS EVERY STATE BACK INDEPENDENTLY of the ops
under test (segments, a reel's children, the stash library, `flame.timeline.clip`, find_by_uid).
Everything this run makes is named `P32B_<ts>_...` and is deleted at the end (the stash library is
`P32B_<ts>_CKPT`, passed to every server as FLAME_MCP_CHECKPOINT_LIBRARY); then flame.exit(). The
journal directory is this run's own (`p3/journal_p32b_e2e_<ts>/`). No test hook is set for any
server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by
behaviour already measured on a real Flame. OBSERVED steps (`kind: "observation"`, always ok) record
what Flame did where nothing was measured: what the Timeline shows after a delete and after a move.

Stages
  p  plan.run bundle: three trims -> ONE stashed copy, every trim where transport reads it, ONE
     revert (new uid, old fingerprint); a step that does not resolve (record_in /
     expect_source_name) stops everything and leaves no trace; `late`; another sequence / a
     rename in the plan refused; a set_attributes and a trim in one plan come back with one
     revert.
  d  desktop.delete: a clip, a sequence, a list of two (gone, stashed, revert puts them back: new
     uid, same fingerprint, end of the reel); a library clip is refused; a shown sequence is shown
     again after the revert; another shown sequence stays shown; a reel / reel group / Batch reel
     clip / stash entry / the same thing twice / a plan are refused; a gone parent is a CONFLICT.
  m  desktop.move: the sequence in front of the shown one is moved, the Timeline stays on it.
  g  journal.purge_checkpoints: dry run by default, the unknown copy is reported and kept, a real
     run (given the dry run's token) deletes the recorded copies, a purged checkpoint cannot be
     restored, the emptied date reel goes.
  k  journal.check of delete / plan intents with no outcome (landed / not_landed / diverged).
  r  a READONLY server denies the delete, the purge and a plan that holds a trim.
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
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do,
    guarded,
    rec,
    save,
    save_stage,
    server_session,
)
from p2_4_common import flame_exit, journal_states, ops_used, print_observations  # noqa: E402
from p3_1a_read_e2e import PRELUDE, fill, tx_val  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    CKPT_ENV,
    GOOD_STATES,
    OUTPUT_DIR,
    prepare,
)
from p3_2b_e2e_common import (  # noqa: E402
    CALLS,
    CLEANUP_CODE,
    LEFT_CODE,
    PREFIX,
    TEMPLATES,
    Results,
    check_group,
    ckpt_library,
    copy_into,
    expect_error,
    fingerprint,
    intent_of,
    run_call,
    state_snapshot,
    synthetic_open_intent,
)
from p3_2b_e2e_delete import stage_d, stage_g, stage_m  # noqa: E402
from p3_2b_e2e_plan import stage_p  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

# -- k: journal.check of the new intents


async def stage_k(ctx: Ctx) -> None:
    ids = ctx.ids
    group = next((g["group"] for g in ids.get("deleteGroups", []) if g.get("group")), None)
    live = (
        await copy_into(ctx, ids["clipWt"]["clip1"], ids["delReelWt"])
        if "delReelWt" in ids
        else None
    )
    if group is None or live is None:
        rec(ctx, "k0_a_delete_group_and_a_live_clip_to_check_against_are_there", False)
        return
    async with server_session(ctx, "p32b-k") as session:
        source = (intent_of(ctx, group).get("sources") or [{}])[0]
        live_source = {**source, "uid": live["uid"], "name": live["name"], "wiretapId": live["wt"]}
        landed = await check_group(session, ctx, synthetic_open_intent(ctx, group))
        not_landed = await check_group(
            session, ctx, synthetic_open_intent(ctx, group, sources=[live_source])
        )
        diverged = await check_group(
            session, ctx, synthetic_open_intent(ctx, group, sources=[source, live_source])
        )
        rec(
            ctx,
            "k1_check_of_a_delete_every_old_uid_gone_landed_all_there_not_landed_else_diverged",
            landed.get("verdict") == "landed"
            and not_landed.get("verdict") == "not_landed"
            and diverged.get("verdict") == "diverged",
            landed=landed.get("verdict"),
            notLanded=not_landed.get("verdict"),
            diverged=diverged.get("verdict"),
        )
        plan = (ids.get("planBundle") or {}).get("group")
        wiretap = ids["s1Wt"]
        now = await fingerprint(session, ctx, wiretap)
        if plan is None or now is None:
            rec(ctx, "k2_a_plan_group_and_a_live_sequence_are_there", False)
            return
        unchanged = await check_group(
            session,
            ctx,
            synthetic_open_intent(
                ctx,
                plan,
                target={"kind": "wiretap", "id": wiretap},
                fingerprintBefore=now,
                fingerprintExpected=None,
            ),
        )
        elsewhere = await check_group(
            session,
            ctx,
            synthetic_open_intent(
                ctx,
                plan,
                target={"kind": "wiretap", "id": wiretap},
                fingerprintBefore="0" * 64,
                fingerprintExpected=None,
            ),
        )
        rec(
            ctx,
            "k2_check_of_a_bundled_plan_reads_the_fingerprint_before_not_landed_else_diverged",
            unchanged.get("verdict") == "not_landed" and elsewhere.get("verdict") == "diverged",
            unchanged=unchanged.get("verdict"),
            elsewhere=elsewhere.get("verdict"),
        )
        listing = await do(session, ctx, "journal.list", {"limit": 300})
        own = journal_states(ctx)
        rec(
            ctx,
            "k3_every_group_of_this_run_is_decided_none_is_unknown",
            listing.get("ok") is True
            and bool(own)
            and set(own.values()) <= GOOD_STATES
            and "unknown" not in own.values(),
            groups=len(own),
            states=sorted(set(own.values())),
        )


# -- r: the read-only policy


async def stage_r(ctx: Ctx) -> None:
    ids = ctx.ids
    case = {"wt": ids["s1Wt"], "rin": 10}
    async with server_session(ctx, "p32b-r", readonly=True) as session:
        before = await state_snapshot(ctx)
        deleted = await run_call(session, ctx, "r_readonly_delete", case=case)
        purged = await run_call(session, ctx, "r_readonly_purge")
        planned = await run_call(session, ctx, "r_readonly_plan_trim", case=case)
        rec(
            ctx,
            "r_a_readonly_server_denies_the_delete_the_purge_and_a_plan_with_a_trim",
            expect_error(deleted, "POLICY_DENIED")
            and expect_error(purged, "POLICY_DENIED")
            and expect_error(planned, "POLICY_DENIED")
            and await state_snapshot(ctx) == before,
            delete=deleted.get("errorCode"),
            purge=purged.get("errorCode"),
            plan=planned.get("errorCode"),
        )


# -- z / driver


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {
        "plan.run",
        "desktop.delete",
        "desktop.move",
        "journal.purge_checkpoints",
        "journal.revert",
        "journal.check",
        "journal.list_checkpoints",
        "journal.restore_checkpoint",
        "timeline.checkpoint",
        "timeline.fingerprint",
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
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p32b_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p32b_left")
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
        if "s1cWt" in ctx.ids and "fWt" in ctx.ids and "ccWt" in ctx.ids:
            for name, stage in (
                ("p", stage_p),
                ("d", stage_d),
                ("m", stage_m),
                ("k", stage_k),
                ("r", stage_r),
                ("g", stage_g),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p32b_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run

EXPECTED_VALIDATION_FAILURES: Final[frozenset[str]] = frozenset()
EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset()


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
    print(f"===== calls: {len(CALLS)}; stages: p d m k r g, prepare and z =====")
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
    journal_dir = OUTPUT_DIR / f"journal_p32b_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p3_2b_plan_delete_e2e_{ts}.json",
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
