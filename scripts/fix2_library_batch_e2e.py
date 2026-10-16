#!/usr/bin/env python3
"""Real-Flame end-to-end: `library.create`, `desktop.copy` of a Batch group into a
library / a library folder and back onto the Desktop, the `{kind: 'desktop'}` selector, with their
journal groups, `journal.revert` and `journal.check` (run with the `.venv` Python;
`scripts/run_e2e.sh fix2_library_batch` starts Flame; `--dry-run` connects to nothing and only lists
the calls and compiles the code).

Checks: `library.create`, `desktop.copy` of a Batch group into a library, into a library folder and
back onto the Desktop, and the `{kind: 'desktop'}` selector, with their journal groups, reverts and
`journal.check`.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (the libraries and their children, the Batch groups' nodes /
sockets / reels, the current group). Everything this run makes is named `PFX2_<ts>_...`
(scripts/p2_4_clean_probe_project.py already knows the prefix) and is deleted at the end by that
prefix; then flame.exit(). The journal directory is this run's own (`p3/journal_fix2_e2e_<ts>/`).
No test hook is set for any server. The first step refuses to run anywhere but in the project
`flame_mcp_probe`.

Material (transport): reel group RG with a reel; library SRC with a reel holding three clips named
`c1 c2 c3`; Batch groups BGK (a spare, never touched), BGN (Blur -> Comp wired, c1 c2 c3 copied
into its first schematic reel in that order) and BGC (the same, made last so it is the current
group).

Stages
  l  library.create: the library is made empty / a taken name is refused with nothing made or
     journaled / allow_rename gives `<name> 1` / revert deletes an empty library and cannot be
     repeated / revert is a CONFLICT when the library got a clip; two backup libraries are made
     (with `library.create`) and a reel and a folder go into one of them.
  b  a Batch group into a library: the copy lists its reels and clips but reads no nodes; the source
     and the current group do not change; a current group copies the same way; into a library
     folder; revert deletes the copy; a reel (library / Desktop), a clip beside the group and the
     Desktop itself are refused before anything is written; a renamed copy makes the revert a
     CONFLICT (observed: whether a library group can be renamed at all).
  r  a library Batch group onto the Desktop: nodes (names, types, connections), schematic reel clips
     (in order) and shelf reels equal the original's; current does not move; revert deletes the
     new group; a node added by hand makes the revert a CONFLICT; a restored group that became
     current moves current when it is deleted (observed: whether it can be made current); a library
     group cannot be copied into a library.
  d  `desktop.list_children({kind: 'desktop'})` lists the reel groups and Batch groups.
  s  the checkpoint library (named by a string, `FLAME_MCP_CHECKPOINT_LIBRARY`, set by this run):
     closed, a dry `journal.purge_checkpoints` still sees its copy (not "gone") and leaves it
     closed.
  o  (inside b) a closed library is opened for the call and closed again; what `open()` answers and
     whether `opened` reads true right after it, and (inside l) whether every library's `opened`
     reads, are observed.
  c  `journal.check` finds a library.create and a Batch copy whose outcome is missing.
  e  plan.run refuses library.create; a READONLY server denies it.
  g  the journal: every group of this run is decided, none unknown.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Final

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import e2e_guard  # noqa: E402
from fix2_e2e_common import (  # noqa: E402
    CALLS,
    CLEANUP_CODE,
    GOOD_STATES,
    LEFT_CODE,
    NAME_PREFIX,
    OUTPUT_DIR,
    TEMPLATES,
    Results,
    expect_error,
    prepare,
    run_call,
)
from fix2_e2e_stages import stage_b, stage_c, stage_d, stage_l, stage_r, stage_s  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
    _result_of,
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
from p2_4_common import (  # noqa: E402
    flame_exit,
    journal_states,
    ops_used,
    print_observations,
)
from p3_1a_read_e2e import PRELUDE, fill, tx_val  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

# -- e: plan.run and the read-only policy --------------------------------------------------------


async def stage_e(ctx: Ctx) -> None:
    async with server_session(ctx, "fix2-e") as session:
        env = await run_call(session, ctx, "e1_plan_library_create")
        problems = " ".join(
            p for i in _result_of(env).get("issues") or [] for p in i.get("problems") or []
        )
        rec(
            ctx,
            "e1_plan_run_refuses_library_create",
            expect_error(env, "INVALID_ARGS")
            and "library.create cannot be a plan step" in problems,
            error=env.get("error"),
        )
    async with server_session(ctx, "fix2-e-readonly", readonly=True) as session:
        env = await run_call(session, ctx, "l2_create_taken_name")
        rec(
            ctx,
            "e2_a_readonly_server_denies_library_create",
            expect_error(env, "POLICY_DENIED"),
            error=env.get("error"),
        )


# -- g: the journal ------------------------------------------------------------------------------


async def stage_g(ctx: Ctx) -> None:
    async with server_session(ctx, "fix2-g") as session:
        listing = await do(session, ctx, "journal.list", {"limit": 200})
        own = journal_states(ctx)
        rec(
            ctx,
            "g_every_group_of_this_run_is_decided_none_is_unknown",
            listing.get("ok") is True
            and bool(own)
            and set(own.values()) <= GOOD_STATES
            and "unknown" not in own.values(),
            groups=len(own),
            states=sorted(set(own.values())),
        )


# -- z / driver ----------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {
        "library.create",
        "desktop.copy",
        "desktop.list_children",
        "journal.revert",
        "journal.check",
        "plan.run",
    }
    rec(
        ctx,
        "z_every_op_of_this_fix_was_called",
        wanted <= set(used),
        missing=sorted(wanted - set(used)),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{NAME_PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "fix2_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "fix2_left")
    ctx.results["cleanup"] = {"deleted": gone, "stillThere": left}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted",
        isinstance(gone, dict)
        and all(ok is True for rows_ in gone.values() for _n, ok in rows_)
        and left == {"reelGroups": [], "batchGroups": [], "libraries": []},
        deleted=gone,
        stillThere=left,
    )
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "bgcUid" in ctx.ids:
            for name, stage in (
                ("l", stage_l),
                ("b", stage_b),
                ("r", stage_r),
                ("d", stage_d),
                ("s", stage_s),
                ("c", stage_c),
                ("e", stage_e),
                ("g", stage_g),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "fix2_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run -------------------------------------------------------------------------------------

EXPECTED_VALIDATION_FAILURES: Final[frozenset[str]] = frozenset()
EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset(
    {"b5_copy_group_with_a_clip", "b5_copy_desktop_group_onto_the_desktop"}
)
"""The calls whose own precheck refuses them before Flame is asked."""


def placeholder_ids() -> dict[str, Any]:
    ids: dict[str, Any] = {
        key: f"/wt/{key}"
        for key in (
            "lib3Wt",
            "lib4Wt",
            "lib5Wt",
            "libReelWt",
            "deskReelWt",
            "folderWt",
            "rgWt",
            "srcLibWt",
        )
    }
    ids.update(
        lib1Name="PFX2_dry_LIB1",
        lib2Name="PFX2_dry_LIB2",
        lib3Name="PFX2_dry_LIB3",
        lib4Name="PFX2_dry_LIB4",
        lib5Name="PFX2_dry_LIB5",
        ckptName="PFX2_dry_CKPT",
        bgnUid="uid-bgn",
        bgcUid="uid-bgc",
        clipWt={"c1": "/wt/c1", "c2": "/wt/c2", "c3": "/wt/c3"},
        restoreSel={"kind": "path", "path": ["PFX2_dry_LIB3", "PFX2_dry_BGN"]},
        restoreCurrentSel={"kind": "path", "path": ["PFX2_dry_LIB3", "PFX2_dry_BGC"]},
    )
    return ids


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    ids = placeholder_ids()
    print("===== calls through flame_do =====")
    for call_id, (op_name, build) in CALLS.items():
        op = registry.get(op_name)
        problems: list[str] = []
        if op is None:
            print(f"NG {call_id}: unknown op {op_name}")
            failures += 1
            continue
        args = build(dict(ids))
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
        if refusal is None and call_id in EXPECTED_REFUSALS:
            problems.append("expected the precheck to refuse this call")
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
    stages = sorted({cid[0] for cid in CALLS})
    print(f"===== stages with calls: {stages}; the run also has prepare, c, g and z =====")
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
    journal_dir = OUTPUT_DIR / f"journal_fix2_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"fix2_library_batch_e2e_{ts}.json",
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
