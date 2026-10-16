#!/usr/bin/env python3
"""Real-Flame end-to-end: `mediahub.import`, `desktop.duplicate` and `desktop.move`, with
their journal groups, `journal.revert` and `journal.check` (run with the `.venv` Python;
`scripts/run_e2e.sh p3_1b_write` starts Flame; `--dry-run` connects to nothing and only lists the
calls and compiles the code).

Checks: `mediahub.import`, `desktop.duplicate` and `desktop.move`, with their journal groups,
`journal.revert` and `journal.check`.

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (the children of a reel / library, `find_by_uid`,
`find_by_wiretap_node_id`, the segments of a sequence). Everything this run makes is named
`P31B_<ts>_...` and is deleted at the end; then flame.exit(). The journal directory is this run's
own (`p3/journal_p31b_e2e_<ts>/`), the files the imports read are in `p3/files_p31b_<ts>/`. No test
hook is set for any server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by
behaviour already measured on a real Flame. OBSERVED steps (`kind: "observation"`, always ok) record
what Flame did where nothing was measured: a move from a library to the Desktop (2025: not
measured), an import into a library itself / a library folder, a duplicate inside a library, and
what a journal state that no real fault can make (`journal.check`) says -- the check steps append an
intent record without an outcome to THIS RUN'S journal directory (a copy of a real group's intent,
or one whose `existing` is what Flame holds now) and let `journal.check` read the real Flame.

Material (transport)
  desktop reel group RG with reels SRC (clip1, clip2, clip3 imported), SEQ (sequence S1: 2 tracks),
  A B C D E (empty), DREL (clip3 and a copy of S1, for the reel duplicate); library LIB with reels
  LR and LR2; a Batch group BG with one clip in its schematic reel (when that reel has a wiretap
  id); files: empty.mov (0 bytes), dir2/ (two media files), ghost.[00000001-00000024].tif (not
  on disk).

Stages
  a  import: one path / a list of 3 (order) / a directory (more clips than paths, notes) / a library
     reel; into a library and a folder (observed); missing path, 0-byte file and relative path
     refused before Flame with nothing journaled; a [..] pattern that matches nothing -> SILENT_NOOP
     and the outcome in the journal; a Batch reel refused; revert deletes what came in; a
     `journal.check` of an open import intent (landed with the clips / not_landed).
  b  duplicate: a clip / a sequence (the answer's name is not used; the segments equal the
     source's) / a reel with a clip and a sequence; selectors of the new things resolve; revert
     deletes them; a changed duplicate is a CONFLICT and stays; a reel group, a library and a Batch
     reel are refused; a duplicate in a library (observed); `journal.check`.
  c  move: a clip to another Desktop reel (new uid and wiretap id, old selector SELECTOR_EMPTY, new
     selector works, END of the destination) / a sequence (same segments) / a list of 2 (order) / a
     library reel / library -> Desktop (observed); revert puts them back with new selectors; a
     thing removed from the destination or an old parent that is gone = CONFLICT and nothing is
     moved; Batch reels refused; same reel / selection destination / reel source refused;
     `journal.check`.
  e  plan.run refuses the three ops; a READONLY server denies them.
  g  the journal: every group of this run is landed / reverted / conflict / failed / not_landed,
     none unknown.
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
from p3_1a_read_e2e import (  # noqa: E402
    PRELUDE,
    fill,
    tx_val,
)
from p3_1b_e2e_common import (  # noqa: E402
    CALLS,
    CLEANUP_CODE,
    GOOD_STATES,
    LEFT_CODE,
    OUTPUT_DIR,
    TEMPLATES,
    Results,
    expect_error,
    prepare,
    run_call,
    sel,
)
from p3_1b_e2e_import_dup import stage_a, stage_a2, stage_b  # noqa: E402
from p3_1b_e2e_move import stage_c  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

# -- e: plan.run and the read-only policy


async def stage_e(ctx: Ctx) -> None:
    ids = ctx.ids
    ids.setdefault("mvSrcWt", ids["clip1Wt"])
    ids.setdefault("mvDestWt", ids["eWt"])
    ids.setdefault("mvSrcReelWt", ids["dWt"])
    async with server_session(ctx, "p31b-e") as session:
        for call_id, op in (
            ("e1_plan_import", "mediahub.import"),
            ("e1_plan_duplicate", "desktop.duplicate"),
            ("e1_plan_move", "desktop.move"),
        ):
            env = await run_call(session, ctx, call_id)
            problems = " ".join(
                p for i in _result_of(env).get("issues") or [] for p in i.get("problems") or []
            )
            rec(
                ctx,
                f"{call_id}_plan_run_refuses_{op.replace('.', '_')}",
                expect_error(env, "INVALID_ARGS") and f"{op} cannot be a plan step" in problems,
                error=env.get("error"),
            )
    async with server_session(ctx, "p31b-e-readonly", readonly=True) as session:
        for call_id, op in (
            ("a1_import_one", "mediahub.import"),
            ("b1_duplicate_clip", "desktop.duplicate"),
            ("c1_move_one", "desktop.move"),
        ):
            env = await run_call(session, ctx, call_id)
            rec(
                ctx,
                f"e2_a_readonly_server_denies_{op.replace('.', '_')}",
                expect_error(env, "POLICY_DENIED"),
                error=env.get("error"),
            )


# -- g: the journal


async def stage_g(ctx: Ctx) -> None:
    async with server_session(ctx, "p31b-g") as session:
        listing = await do(session, ctx, "journal.list", {"limit": 100})
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


# -- z / driver


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {
        "mediahub.import",
        "desktop.duplicate",
        "desktop.move",
        "journal.revert",
        "journal.check",
        "desktop.info",
        "plan.run",
        "timeline.list_segments",
    }
    rec(
        ctx,
        "z_every_op_of_this_milestone_was_called",
        wanted <= set(used),
        missing=sorted(wanted - set(used)),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"P31B_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p31b_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p31b_left")
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
        if "s1Wt" in ctx.ids and "lr2Wt" in ctx.ids:
            for name, stage in (
                ("a", stage_a),
                ("a2", stage_a2),
                ("b", stage_b),
                ("c", stage_c),
                ("e", stage_e),
                ("g", stage_g),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p31b_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run

EXPECTED_VALIDATION_FAILURES: Final[frozenset[str]] = frozenset()
EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset(
    {
        "a7_import_missing_path",
        "a8_import_zero_byte",
        "a9_import_relative_path",
        "c11_move_to_a_selection",
    }
)


def placeholder_ids(files_dir: Path) -> dict[str, Any]:
    files_dir.mkdir(parents=True, exist_ok=True)
    real = files_dir / "dry.mov"
    real.write_bytes(b"x")
    ids: dict[str, Any] = {
        key: f"/wt/{key}"
        for key in (
            "aWt",
            "bWt",
            "cWt",
            "dWt",
            "eWt",
            "lrWt",
            "libWt",
            "folderWt",
            "rgWt",
            "drelWt",
            "s1Wt",
            "clip1Wt",
            "libClipWt",
            "batchReelWt",
            "batchClipWt",
            "mvSrcWt",
            "mvDestWt",
            "mvSrcReelWt",
        )
    }
    for key in ("path1", "path2", "path3", "dir2", "ghost"):
        ids[key] = str(real) if key != "dir2" else str(files_dir)
    ids["ghost"] = str(files_dir / "ghost.[00000001-00000024].tif")
    ids["empty"] = str(files_dir / "empty.mov")
    ids["missing"] = str(files_dir / "gone.mov")
    (files_dir / "empty.mov").write_bytes(b"")
    ids["selector"] = sel("/wt/x")
    return ids


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    files_dir = (
        Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "p31b_dry"
    )
    ids = placeholder_ids(files_dir)
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
    print(f"===== stages with calls: {stages}; the run also has prepare, a2, g and z =====")
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
    journal_dir = OUTPUT_DIR / f"journal_p31b_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p3_1b_write_e2e_{ts}.json",
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
