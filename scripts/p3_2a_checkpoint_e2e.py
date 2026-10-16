#!/usr/bin/env python3
"""Real-Flame end-to-end: `timeline.fingerprint`, `timeline.checkpoint`, `segment.trim` (the
first write behind a Timeline checkpoint), `journal.revert` / `journal.restore_checkpoint` of a
sequence and `journal.check` (run with the `.venv` Python; `scripts/run_e2e.sh p3_2a_checkpoint`
starts Flame; `--dry-run` connects to nothing and only lists the calls and compiles the code).

Checks: `timeline.fingerprint`, `timeline.checkpoint`, `segment.trim` (the first write behind a
Timeline checkpoint), `journal.revert` and `journal.restore_checkpoint` of a sequence, and
`journal.check`.

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (the segments of a sequence, a reel's children, the stash
library, `flame.timeline.clip`, find_by_uid, exported pixels). Everything this run makes is named
`P32A_<ts>_...` and is deleted at the end (the stash library is `P32A_<ts>_CKPT`, passed to every
server as FLAME_MCP_CHECKPOINT_LIBRARY: the first checkpoint of every run makes it); then
flame.exit(). The journal directory is this run's own (`p3/journal_p32a_e2e_<ts>/`), the files
(presets, exports) are in `p3/files_p32a_<ts>/`. No test hook is set for any server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by
behaviour already measured on a real Flame. OBSERVED steps (`kind: "observation"`, always ok) record
what Flame did where nothing was measured (the track before and after each trim).

Material (transport)
  desktop reel group RG with reels SRC (clip1, clip2, p2_len48, p2_len72 imported), SEQ and CCP;
  library LIB with reel LR. S1: 2 tracks; track 0 clip1 | gap | clip2 [Burn-in Letterbox, ratio
  1.5] | p2_len48; track 1 p2_len72 trimmed to head 10 / tail 10 (a gap on each side). S2, S3 (one
  clip each: order and display), F (a copy of S1's layout to edit by transport), S1's copy in LR,
  a copy of clip1 in CCP. Every trim case below makes a fresh S1-like sequence.

Stages
  a  fingerprint: stable, counts what transport counts, names what it read; a trim by transport
     changes it, a segment attribute does not, an effect attribute does (and back again).
  b  checkpoint: the first one makes the stash library and the day's reel, the second reuses
     them; the copy has S1's segments; the journal record; a library target and a reel are refused
     with nothing added; journal.list_checkpoints shows kind timeline.
  c  segment.trim: head / tail x shorten / extend x ripple / no ripple, each read back by
     transport (before, after, the other track, the position selectors, trackEnds); refusals
     (beyond the handle, the whole length, a Gap, a library, a bad edge, offset 0) leave nothing.
  d  revert: the fingerprint and the segments are the old ones, a new uid and wiretap id, the old
     selector is SELECTOR_EMPTY, the restored sequence is at the end of the reel, the edited one
     stays in the stash (preCheckpoint), restoring that gives the edited state back, a group is
     reverted once, a revert whose result was changed since is a conflict.
  e  the Timeline: a displayed sequence is displayed again after a revert (opened), another one
     stays shown; the order of the other sequences is kept.
  f  conflict: a transport edit after the trim -> CONFLICT and nothing moves, force restores.
  g  pixels: export (Tiff 16-bit LZW, foreground) before, edited (the control) and after a revert:
     the pixel md5 (ffmpeg) of every frame after equals the one before.
  h  a segment inside a clip on the Desktop: trim -> revert.
  i  journal.check of open intents (not_landed / landed / diverged), a sequence that is not found.
  j  plan.run bundles segment.trim; a READONLY server denies the checkpoint and the trim.
  k  the journal: every group of this run is decided, none unknown.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import os
import re
import subprocess
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
from p3_1a_read_e2e import PRELUDE, fill, tx_val  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    CALLS,
    CKPT_ENV,
    CLEANUP_CODE,
    EXPORT_CODE,
    GOOD_STATES,
    LEFT_CODE,
    OUTPUT_DIR,
    TEMPLATES,
    Results,
    ckpt_library,
    expect_error,
    fingerprint,
    groups_now,
    media_row,
    prepare,
    revert,
    run_call,
    stash_uids,
    tx_stash,
)
from p3_2a_e2e_stages import (  # noqa: E402
    make_for,
    stage_a,
    stage_b,
    stage_d,
    stage_e,
    stage_f,
    stage_h,
    stage_i,
)
from p3_2a_e2e_trim import stage_c  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

EXPORT_TIMEOUT_S: Final[float] = 300.0
FFMPEG_FALLBACK = "/opt/homebrew/bin/ffmpeg"
FFMPEG_TIMEOUT_S = 60.0
HASH_CHUNK = 1 << 16

# -- g: pixels


@functools.cache
def _ffmpeg_exe() -> tuple[str, str]:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe(), "imageio_ffmpeg"
    except Exception as exc:
        return FFMPEG_FALLBACK, f"fallback ({exc!r})"


def _pixel_md5(path: Path) -> str:
    exe = _ffmpeg_exe()[0]
    cp = subprocess.run(
        [exe, "-v", "error", "-i", str(path), "-f", "md5", "-"],
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT_S,
        check=False,
    )
    line = cp.stdout.strip()
    if line.startswith("MD5="):
        return line.removeprefix("MD5=")
    return f"ERR:{cp.returncode}:{cp.stderr.strip()[:200]}"


def _hash_dir(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for p in sorted(x for x in root.rglob("*") if x.is_file()) if root.is_dir() else []:
        h = hashlib.md5(usedforsecurity=False)
        with p.open("rb") as f:
            while chunk := f.read(HASH_CHUNK):
                h.update(chunk)
        files.append(
            {"rel": str(p.relative_to(root)), "bytes": p.stat().st_size, "md5": h.hexdigest()}
        )
    total = sum(f["bytes"] for f in files)
    return {
        "dir": str(root),
        "exists": root.is_dir(),
        "count": len(files),
        "total_bytes": total,
        "files": files,
    }


def _pix_hash_dir(root: Path) -> dict[str, Any]:
    """Adds the pixel md5 to the file listing of `_hash_dir`: `md5` is the pixel value and the
    file md5 is kept as `file_md5`."""
    out = _hash_dir(root)
    files = [{**f, "file_md5": f["md5"], "md5": _pixel_md5(root / f["rel"])} for f in out["files"]]
    return {**out, "files": files, "md5_kind": "pixel (ffmpeg -f md5)"}


def pixel_hash_dir(root: Path) -> dict[str, Any]:
    """The pixel md5 (ffmpeg decodes the TIFF) of every file under `root`: a TIFF's file md5 cannot
    be used (the last bytes of the file differ between exports of the same frame).
    tests/test_p3_2a_e2e_inprocess.py swaps this for a file md5."""
    return _pix_hash_dir(root)


async def export_pixels(ctx: Ctx, wiretap_id: str, tag: str) -> dict[str, Any] | None:
    out = Path(ctx.ids["exportRoot"]) / tag
    code = fill(EXPORT_CODE, WT=wiretap_id, OUT=str(out), PRESET=ctx.ids["presetPath"])
    done = await tx_val(ctx, PRELUDE + "\n" + code, f"p32a_export_{tag}", EXPORT_TIMEOUT_S)
    if not isinstance(done, dict):
        return None
    return {"export": done, **pixel_hash_dir(out)}


def md5s(listing: dict[str, Any] | None) -> list[str]:
    return [f["md5"] for f in (listing or {}).get("files") or []]


def mismatches(a: list[str], b: list[str]) -> int:
    return sum(x != y for x, y in zip(a, b, strict=False)) + abs(len(a) - len(b))


async def stage_g(ctx: Ctx) -> None:
    if not ctx.ids.get("presetPath"):
        rec(ctx, "g0_the_export_preset_is_there", False, source="/opt/Autodesk/presets/...")
        return
    made = await make_for(ctx, "PIX", "g1_trim_for_pixels")
    if made is None:
        return
    async with server_session(ctx, "p32a-g") as session:
        wiretap = made["wt"]
        fp0 = await fingerprint(session, ctx, wiretap)
        base = await export_pixels(ctx, wiretap, "base")
        env = await run_call(session, ctx, "g1_trim_for_pixels")
        edited = await export_pixels(ctx, wiretap, "edited")
        back = await revert(session, ctx, env)
        restored = (_result_of(back).get("restored") or {}).get("wiretapId", "/wt/none")
        after = await export_pixels(ctx, restored, "restored")
        fp2 = await fingerprint(session, ctx, restored)
        count = len(md5s(base))
        rec(
            ctx,
            "g1_the_pixels_after_a_revert_are_the_pixels_before_(md5_of_every_decoded_frame)",
            env.get("ok") is True
            and back.get("ok") is True
            and fp2 == fp0
            and count > 0
            and len(md5s(after)) == count
            and mismatches(md5s(after), md5s(base)) == 0,
            frames=count,
            framesAfter=len(md5s(after)),
            mismatches=mismatches(md5s(after), md5s(base)),
            md5Kind=(base or {}).get("md5_kind"),
            error=back.get("error"),
        )
        rec(
            ctx,
            "g2_the_control_the_edited_sequence_has_other_pixels",
            mismatches(md5s(edited), md5s(base)) > 0,
            mismatches=mismatches(md5s(edited), md5s(base)),
            framesEdited=len(md5s(edited)),
        )


# -- j: plan.run and the read-only policy


async def stage_j(ctx: Ctx) -> None:
    ids = ctx.ids
    row = media_row(ids.get("s1Struct"), 1, "p2_len72") or {"rin": 10}
    ids.setdefault("case", {})["j1_plan_trim"] = {"wt": ids["s1Wt"], "rin": row["rin"]}
    async with server_session(ctx, "p32a-j") as session:
        groups = groups_now(ctx)
        stash_before = stash_uids(await tx_stash(ctx))
        env = await run_call(session, ctx, "j1_plan_trim")
        stash0 = stash_uids(await tx_stash(ctx))
        timeline = _result_of(env).get("timeline") or {}
        rec(
            ctx,
            "j1_plan_run_bundles_segment_trim_as_one_checkpointed_write",
            env.get("ok") is True
            and bool(timeline.get("checkpoint"))
            and groups_now(ctx) == groups + 1
            and len(stash0) == len(stash_before) + 1,
            error=env.get("error"),
        )
    async with server_session(ctx, "p32a-j-readonly", readonly=True) as session:
        trimmed = await run_call(session, ctx, "j2_readonly_trim")
        checkpoint = await run_call(session, ctx, "j2_readonly_checkpoint")
        read = await run_call(session, ctx, "j2_readonly_fingerprint")
        rec(
            ctx,
            "j2_a_readonly_server_denies_the_trim_and_the_checkpoint_but_reads",
            expect_error(trimmed, "POLICY_DENIED")
            and expect_error(checkpoint, "POLICY_DENIED")
            and read.get("ok") is True
            and stash_uids(await tx_stash(ctx)) == stash0,
            trim=trimmed.get("errorCode"),
            checkpoint=checkpoint.get("errorCode"),
        )


# -- k: the journal


async def stage_k(ctx: Ctx) -> None:
    async with server_session(ctx, "p32a-k") as session:
        listing = await do(session, ctx, "journal.list", {"limit": 200})
        own = journal_states(ctx)
        rec(
            ctx,
            "k_every_group_of_this_run_is_decided_none_is_unknown",
            listing.get("ok") is True
            and bool(own)
            and set(own.values()) <= GOOD_STATES
            and "unknown" not in own.values(),
            groups=len(own),
            states=sorted(set(own.values())),
        )
        rec(
            ctx,
            "k_a_revert_group_is_landed_and_what_it_reverted_is_reverted",
            "reverted" in own.values() and "landed" in own.values(),
            states=sorted(set(own.values())),
        )


# -- z / driver


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {
        "timeline.fingerprint",
        "timeline.checkpoint",
        "segment.trim",
        "journal.revert",
        "journal.restore_checkpoint",
        "journal.check",
        "journal.list_checkpoints",
        "journal.list",
        "segment.info",
        "segment.set_attributes",
        "plan.run",
    }
    rec(
        ctx,
        "z_every_op_of_this_milestone_was_called",
        wanted <= set(used),
        missing=sorted(wanted - set(used)),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"P32A_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p32a_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p32a_left")
    ctx.results["cleanup"] = {"deleted": gone, "stillThere": left}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted_the_stash_library_too",
        isinstance(gone, dict)
        and all(ok is True for rows_ in gone.values() for _n, ok in rows_)
        and left == {"reelGroups": [], "libraries": []}
        and ckpt_library(ctx) in [n for n, _ok in (gone or {}).get("libraries", [])],
        deleted=gone,
        stillThere=left,
    )
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    os.environ[CKPT_ENV] = ckpt_library(ctx)
    ctx.results["env"] = {CKPT_ENV: os.environ[CKPT_ENV]}
    try:
        await guarded(ctx, "prepare", prepare)
        if "s1cWt" in ctx.ids and "fWt" in ctx.ids and "ccWt" in ctx.ids:
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("e", stage_e),
                ("f", stage_f),
                ("g", stage_g),
                ("h", stage_h),
                ("i", stage_i),
                ("j", stage_j),
                ("k", stage_k),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p32a_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run

EXPECTED_VALIDATION_FAILURES: Final[frozenset[str]] = frozenset()
EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset({"c13_bad_edge", "c14_zero_offset"})


def placeholder_ids() -> dict[str, Any]:
    ids: dict[str, Any] = {
        key: f"/wt/{key}" for key in ("fWt", "s1Wt", "s2Wt", "s1cWt", "seqReelWt", "ccWt")
    }
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
    print(f"===== calls: {len(CALLS)}; stages: a b c d e f g h i j k, prepare and z =====")
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
    journal_dir = OUTPUT_DIR / f"journal_p32a_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p3_2a_checkpoint_e2e_{ts}.json",
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
