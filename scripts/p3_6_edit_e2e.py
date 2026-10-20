#!/usr/bin/env python3
"""Real-Flame end-to-end: editing a sequence that is already built -- `timeline.delete_segments`,
`timeline.move_track`, `timeline.shorten`, `timeline.compare`, `segment.connections`, and the audio
reads of `timeline.info` and `timeline.list_segments` (run with the `.venv` Python;
`scripts/run_e2e.sh p3_6_edit` starts Flame; `--dry-run` connects to nothing and only lists the
calls and compiles the code).

Checks: Editing a built sequence: `timeline.delete_segments` (ripple and lift, a Gap, the by-product
clip left out of the stash reel), `timeline.move_track` (a track to the bottom and to the top, an
empty track, the audio left alone), the audio reads of `timeline.info` and
`timeline.list_segments`, `timeline.shorten` on a sequence with sound, `timeline.compare` of
copies, and `segment.connections` of a segment that has none.

Material: scripts/make_clip_media.sh, scripts/make_p2_media.sh and scripts/make_p2_av_media.sh
(`p2_av72`: 72 frames with PCM audio, the one clip of the tests that has sound). The test sequences
are made with the bit depth of the media, read from the clip.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (the video rows with their source range and effect types, the
audio channels and their segments, a segment's connected_segments(), the stash library). Everything
this run makes is named `P36E_<ts>_...` and is deleted at the end (the stash library is
`P36E_<ts>_CKPT`, passed to every server as FLAME_MCP_CHECKPOINT_LIBRARY); then flame.exit(). The
journal directory is this run's own (`p3/journal_p36e_e2e_<ts>/`). No test hook is set for any
server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by what
was measured on a real Flame beforehand. Every step of this run is JUDGED; none is only recorded
(`kind: "observation"`, always ok). These steps are judged on a measurement (Flame
2025.2.4 and 2026.2.3): moving the track that holds the audio clip leaves the audio as it was (m11)
and its revert, and the revert after moving another track (m10b, m11b); the lift that builds the
measured case of `timeline.shorten` (s1), what a revert of a shorten brings back (s9b); a shorten
where the tracks end in media (s10, s10b); and the audio differences between a sequence and a copy
of its shortened self (q3b).

Not called: `segment.create_connection`, `segment.remove_connection` and
`segment.sync_connections`. A segment's connections may reach segments in other sequences, which
no Timeline checkpoint covers, and what the calls do there has not been measured, so this run only
reads connections.

Not measured, and so not built here: a `start_time` other than 0, a sequence in which every
track, the audio channels included, ends in a Gap, a transition other than a Cut, and whether
`journal.revert` puts back a change that touched only the audio. A sequence with two or more
versions is only refused (s13, m15), never shortened or moved.

Stages (every case is a fresh sequence; `make_s`, `make_contiguous`, `make_effect`, `make_av`)
  d  timeline.delete_segments: ripple moves the later media of that track left and not the content
     of the other track, the sequence ends where the longest content ends and the Gap at the end of
     every track (the audio's too) follows it (measured), ripple=false leaves a Gap of the same
     length, a Gap is closed, the by-product clip is not left in the stash reel, the journal,
     reverts, refusals (a record_in no segment has, a source name that differs, arguments that do
     not fit).
  m  timeline.move_track: the top media track to the bottom, the bottom one to the top, an empty
     track, the effect types and source ranges carried, reverts, the audio unchanged when the track
     moved holds no audio clip (m9, JUDGED on a measurement: a track of two media
     segments and a Gap of a sequence with sound moved to the bottom left result.audio as it was)
     and when it does (m11, JUDGED on a measurement), the audio as it was after a
     revert (m10b, m11b), refusals (to == track, a single-track version, a track or target that
     is not there).
  a  the audio reads: timeline.info and timeline.list_segments(media='audio') against transport;
     `channel` with media='video' and a track or channel that is not there are refused.
  s  timeline.shorten: the measured case (video tracks ending in a Gap, audio crossing the new
     end; when it cannot be built s1 and s1b are failures, not skips), what its revert brings
     back (s9b), a sequence whose tracks end in media and its revert (s10, s10b; JUDGED on a
     measurement), refusals (a duration that does not shorten, two versions).
  q  timeline.compare: copies are equal, a deleted segment shows where it was, a shortened
     sequence differs from its original only at or after the new end and, in the audio, only by
     the cut ends of the segments that cross it (q3b), an extra track and the empty tracks.
  c  segment.connections of segments with no connection.
  y  a READONLY server denies the three writes and answers the two reads.
  z  cross-cutting checks (the JUDGED steps s2, s9b, q3 and q3b were recorded, by name: an
     observation of that name does not count); everything this run made is deleted; flame.exit().
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
from p3_6_e2e_audio import S2_NAME, S9B_NAME, stage_a, stage_s  # noqa: E402
from p3_6_e2e_common import (  # noqa: E402
    CALLS,
    OUTPUT_DIR,
    PREFIX,
    TEMPLATES,
    Results,
    ckpt_library,
    prepare,
)
from p3_6_e2e_compare import Q3_NAME, Q3B_NAME, stage_c, stage_q, stage_y  # noqa: E402
from p3_6_e2e_delete import stage_d  # noqa: E402
from p3_6_e2e_move import stage_m  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

__all__ = ["server_session"]
STAGES = (
    ("d", stage_d),
    ("m", stage_m),
    ("a", stage_a),
    ("s", stage_s),
    ("q", stage_q),
    ("c", stage_c),
    ("y", stage_y),
)
WANTED_OPS: Final[frozenset[str]] = frozenset(
    {
        "timeline.delete_segments",
        "timeline.move_track",
        "timeline.shorten",
        "timeline.compare",
        "timeline.info",
        "timeline.list_segments",
        "segment.connections",
        "timeline.create_track",
        "timeline.create_version",
        "journal.revert",
        "timeline.fingerprint",
    }
)


MEASURED_CASE_JUDGED: Final[tuple[str, ...]] = (S2_NAME, S9B_NAME, Q3_NAME, Q3B_NAME)
"""JUDGED steps that only exist when the measured case of timeline.shorten was built (stage s
records them after s1b); a run in which they are not there is not green."""


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    rec(
        ctx,
        "z_every_op_of_this_milestone_was_called",
        WANTED_OPS <= set(used),
        missing=sorted(WANTED_OPS - set(used)),
    )
    missing_judged = [
        name
        for name in MEASURED_CASE_JUDGED
        if not any(
            s["name"] == name and s.get("kind") != "observation" for s in ctx.results["steps"]
        )
    ]
    rec(
        ctx,
        "z_the_judged_steps_of_the_measured_cases_were_recorded",
        not missing_judged,
        missing=missing_judged,
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p36e_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p36e_left")
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
        flame_exit(ctx, "p36e_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------

EXPECTED_VALIDATION_FAILURES: Final[frozenset[str]] = frozenset()
EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset()
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
    journal_dir = OUTPUT_DIR / f"journal_p36e_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p3_6_edit_e2e_{ts}.json",
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
