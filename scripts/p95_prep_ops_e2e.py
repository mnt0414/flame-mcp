#!/usr/bin/env python3
"""Real-Flame end-to-end of the prep ops (colour spaces, record timecode, batch.delete,
segment.replace, usage, still, reorder) through a real MCP stdio client (run with the `.venv`
Python; `scripts/run_e2e.sh p95_prep_ops` starts Flame).

Checks: `timeline.colour_spaces` and `render.export` of a sequence of one colour space,
`desktop.set_record_tc`, `desktop.get_record_tc` and `desktop.sync_record_tc` (dry run, sync,
in_sync, `journal.revert`), `batch.delete` (the current group refused, `expect_name`, a group with a
clip refused, `allow_current` deletes and saves the setup, `journal.revert` refused),
`segment.replace` (a 24-frame TIFF sequence refused with the segment unchanged, a clip trimmed at
the head by 30 frames, another clip of the same media answered with `verified` true, the same clip
again with `verified` false), `timeline.usage` on a sequence with a trimmed second use of one
source, `render.still` on a clip whose start_frame is 1001, and `desktop.reorder`.

Material: probe_project/media/p2/p2_len48.mov (scripts/make_p2_media.sh) and the 24 TIFF files of
probe_project/media/u13b_src (scripts/make_u13b_media.sh) under ~/Library/Application Support/
flame-mcp. They are not checked before Flame starts. Not run by the unit tests.

Every name starts with mcp_test_p95e_<ts>; the end deletes only those. `--stage` runs some stages
only (the default is all of them, in this order).
  c  timeline.colour_spaces and render.export (a sequence of one colour space is written)
  t  desktop.set_record_tc / get_record_tc / sync_record_tc: dry run, sync, in_sync, journal.revert
  b  batch.delete: the current group refused, expect_name, a group with a clip refused,
     allow_current deletes and saves the setup, journal.revert refused
  r  segment.replace: a TIFF sequence of 24 frames (refused, the segment is unchanged), a clip
     trimmed at the head (replaced or refused; the answer must match what the segment shows), the
     same media under another name (verified), the same clip again (verified false)
  u  timeline.usage on a sequence with a trimmed second use of one source
  s  render.still on a clip whose start_frame is 1001
  o  desktop.reorder: the order and the uids, journal.revert, many moves by uid, journal.check,
     entries that share a name
"""

from __future__ import annotations

import argparse
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Final

import anyio
import e2e_guard
from p1_mcp_e2e import RUN_DIR_DEFAULT
from p2_2a_journal_e2e import Ctx, do, guarded, rec, save, save_stage, server_session, tx
from p2_4_common import OUTPUT_DIR, flame_exit

APP: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp"
MOV: Final[Path] = APP / "probe_project" / "media" / "p2" / "p2_len48.mov"
TIFS: Final[Path] = APP / "probe_project" / "media" / "u13b_src"
SCRATCH: Final[Path] = APP / "scratch" / "p95"
FIRST_NUMBER: Final[int] = 1001
FLAME_VERSION: Final[str] = e2e_guard.e2e_flame_version()
PRESET_REL: Final[str] = "export/presets/flame/file_sequence/Tiff/Tiff (16-bit LZW).xml"
PRESET: Final[str] = str(Path("/opt/Autodesk/presets") / FLAME_VERSION / PRESET_REL)

PREFIX_CODE: Final[str] = """
_d = flame.projects.current_project.current_workspace.desktop
"""


def wt(wiretap: str) -> dict[str, str]:
    return {"kind": "wiretap", "id": wiretap}


def fill(code: str, **values: Any) -> str:
    for key, value in values.items():
        code = code.replace(f"@{key}@", repr(value))
    return PREFIX_CODE + code


async def val(ctx: Ctx, code: str, label: str, timeout_s: float = 60.0) -> Any:
    br = await tx(ctx, code, label=label, timeout_s=timeout_s)
    if not br.ok:
        raise RuntimeError(f"{label}: {br.error_code}: {br.error}")
    return br.result


def base(ctx: Ctx) -> str:
    return f"mcp_test_p95e_{ctx.ts}"


PREPARE: Final[str] = """
_rg = _d.create_reel_group(@BASE@ + '_RG')
_R = list(_rg.reels)[0]
_R.name = @BASE@ + '_R'
_seqreel = list(_rg.reels)[1]
_seqreel.name = @BASE@ + '_S'
_a = flame.import_clips(@MOV@, _R)[0]
_a.name = @BASE@ + '_a'
_fps = str(_v(_a.frame_rate))
_short = flame.media_panel.copy([_a], _R)[0]
_short.name = @BASE@ + '_short'
_seq = _seqreel.create_sequence(name=@BASE@ + '_seq', video_tracks=1, frame_rate=_fps)
_t0 = list(list(_seq.versions)[0].tracks)[0]
_seq.overwrite(_a, flame.PyTime(1, _fps), _t0)
_c = flame.media_panel.copy([_a], _R)[0]
_c.name = @BASE@ + '_tc'
_result = {
    'reel': _R.get_wiretap_node_id(), 'seqReel': _seqreel.get_wiretap_node_id(),
    'a': _a.get_wiretap_node_id(), 'short': _short.get_wiretap_node_id(),
    'seq': _seq.get_wiretap_node_id(), 'tc': _c.get_wiretap_node_id(), 'fps': _fps,
}
"""


async def prepare(ctx: Ctx) -> None:
    made = await val(ctx, fill(PREPARE, BASE=base(ctx), MOV=str(MOV)), "p95e_prepare", 120.0)
    ctx.ids.update(made)
    rec(ctx, "prepare", True, made=made)


async def stage_c(ctx: Ctx) -> None:
    async with server_session(ctx, "c") as s:
        env = await do(s, ctx, "timeline.colour_spaces", {"target": wt(ctx.ids["seq"])})
        res = env.get("result") or {}
        rec(ctx, "c_scan_one_space", env.get("ok") and res.get("mixed") is False, env=env)
        out = SCRATCH / f"e2e_export_{ctx.ts}"
        out.mkdir(parents=True)
        env = await do(
            s,
            ctx,
            "render.export",
            {
                "target": wt(ctx.ids["seq"]),
                "preset": PRESET,
                "output_dir": str(out),
                "allow_default_path": True,
            },
        )
        written = [p for p in out.rglob("*") if p.is_file()] if out.exists() else []
        rec(
            ctx,
            "c_export_writes",
            env.get("ok") and len(written) > 0,
            env=env,
            written=len(written),
        )


async def stage_t(ctx: Ctx) -> None:
    clip = wt(ctx.ids["tc"])
    async with server_session(ctx, "t") as s:
        env = await do(
            s,
            ctx,
            "desktop.set_record_tc",
            {"target": clip, "start_time_frame": 1000, "current_time_frame": 1000},
        )
        rec(ctx, "t_set", env.get("ok"), env=env)
        env = await do(s, ctx, "desktop.sync_record_tc", {"targets": [clip], "dry_run": True})
        rows = (env.get("result") or {}).get("rows") or []
        rec(ctx, "t_dry_run_sees_it", env.get("ok") and rows[0].get("status") == "sync", env=env)
        env = await do(s, ctx, "desktop.sync_record_tc", {"targets": [clip]})
        rows = (env.get("result") or {}).get("rows") or []
        rec(ctx, "t_synced", env.get("ok") and rows[0].get("status") == "synced", env=env)
        group = (env.get("journal") or {}).get("group")
        again = await do(s, ctx, "desktop.sync_record_tc", {"targets": [clip]})
        rows = (again.get("result") or {}).get("rows") or []
        rec(ctx, "t_in_sync", again.get("ok") and rows[0].get("status") == "in_sync", env=again)
        back = await do(s, ctx, "journal.revert", {"group": group})
        read = await do(
            s,
            ctx,
            "desktop.get_record_tc",
            {"target": clip, "keys": ["start_time_frame", "current_time_frame"]},
        )
        value = str(read.get("result"))
        rec(ctx, "t_revert", back.get("ok") and "1000" in value, back=back, read=read)


NEW_GROUPS: Final[str] = """
_x = _d.create_batch_group(@BASE@ + '_X')
_full = _d.create_batch_group(@BASE@ + '_FULL')
_clip = flame.find_by_wiretap_node_id(@A@)
flame.media_panel.copy([_clip], list(_full.reels)[0])
_y = _d.create_batch_group(@BASE@ + '_Y')
_result = {'x': _v(_x.uid), 'full': _v(_full.uid), 'y': _v(_y.uid)}
"""


async def stage_b(ctx: Ctx) -> None:
    b = base(ctx)
    uids = await val(ctx, fill(NEW_GROUPS, BASE=b, A=ctx.ids["a"]), "p95e_groups")
    ctx.ids["groups"] = uids
    by = {k: {"kind": "batch", "uid": v} for k, v in uids.items()}
    async with server_session(ctx, "b") as s:
        env = await do(s, ctx, "batch.delete", {"target": by["y"]})
        rec(ctx, "b_current_refused", env.get("errorCode") == "PRECONDITION_UNMET", env=env)
        env = await do(
            s,
            ctx,
            "batch.delete",
            {"target": by["x"], "allow_current": True, "expect_name": "other"},
        )
        rec(ctx, "b_expect_name", env.get("errorCode") == "CONFLICT", env=env)
        env = await do(s, ctx, "batch.delete", {"target": by["full"], "allow_current": True})
        rec(ctx, "b_content_refused", env.get("errorCode") == "PRECONDITION_UNMET", env=env)
        env = await do(
            s,
            ctx,
            "batch.delete",
            {"target": by["x"], "allow_current": True, "expect_name": f"{b}_X"},
        )
        res = env.get("result") or {}
        setup = res.get("setupPath") or ""
        rec(
            ctx,
            "b_deleted_with_setup",
            env.get("ok") and res.get("deleted") is True and Path(setup).is_file(),
            env=env,
        )
        group = (env.get("journal") or {}).get("group")
        back = await do(s, ctx, "journal.revert", {"group": group})
        rec(ctx, "b_revert_refused", back.get("ok") is False, env=back)
        env = await do(
            s,
            ctx,
            "batch.delete",
            {"target": by["full"], "allow_current": True, "allow_content_loss": True},
        )
        rec(ctx, "b_content_loss_allowed", env.get("ok"), env=env)
        env = await do(
            s,
            ctx,
            "batch.delete",
            {"target": by["y"], "allow_current": True, "expect_name": f"{b}_Y"},
        )
        rec(ctx, "b_current_deleted", env.get("ok"), env=env)


NEW_FOR_REPLACE: Final[str] = """
_R = flame.find_by_wiretap_node_id(@REEL@)
_S = flame.find_by_wiretap_node_id(@SEQREEL@)
_a = flame.find_by_wiretap_node_id(@A@)
_fps = @FPS@
_other = flame.media_panel.copy([_a], _R)[0]
_other.name = @BASE@ + '_other'
_tiny = flame.import_clips(@TIFS@, _R)[0]
_tiny.name = @BASE@ + '_tiny'
_cut = flame.media_panel.copy([_a], _R)[0]
_cut.name = @BASE@ + '_cut'
list(list(list(_cut.versions)[0].tracks)[0].segments)[0].trim_head(30)
_seq = _S.create_sequence(name=@BASE@ + '_rseq', video_tracks=1, frame_rate=_fps)
_t0 = list(list(_seq.versions)[0].tracks)[0]
_seq.overwrite(_a, flame.PyTime(1, _fps), _t0)
_seg = [x for x in _t0.segments if str(_v(x.type)) != 'Gap'][0]
_result = {
    'rseq': _seq.get_wiretap_node_id(), 'other': _other.get_wiretap_node_id(),
    'tiny': _tiny.get_wiretap_node_id(), 'cut': _cut.get_wiretap_node_id(),
    'segs': [str(_v(x.type)) for x in _t0.segments],
}
"""

SEG_INFO: Final[str] = """
_seq = flame.find_by_wiretap_node_id(@SEQ@)
_t0 = list(list(_seq.versions)[0].tracks)[0]
_seg = [x for x in _t0.segments if str(_v(x.type)) != 'Gap'][0]
_result = {'name': str(_v(_seg.name)), 'source': str(_v(_seg.source_name)),
           'path': str(_v(_seg.file_path))}
"""


async def stage_r(ctx: Ctx) -> None:
    made = await val(
        ctx,
        fill(
            NEW_FOR_REPLACE,
            BASE=base(ctx),
            REEL=ctx.ids["reel"],
            SEQREEL=ctx.ids["seqReel"],
            A=ctx.ids["a"],
            FPS=ctx.ids["fps"],
            TIFS=str(TIFS),
        ),
        "p95e_replace_setup",
        120.0,
    )
    ctx.ids.update(made)
    index = made["segs"].index("Video Segment")
    segment = {
        "kind": "segment",
        "parent": wt(made["rseq"]),
        "version": 0,
        "track": 0,
        "index": index,
    }
    info = fill(SEG_INFO, SEQ=made["rseq"])
    async with server_session(ctx, "r") as s:
        env = await do(s, ctx, "segment.replace", {"target": segment, "source": wt(made["tiny"])})
        read = await val(ctx, info, "p95e_seg")
        rec(
            ctx,
            "r_refused_clip_leaves_it_unchanged",
            env.get("ok") is False and read["name"].endswith("_a"),
            env=env,
            read=read,
        )
        env = await do(s, ctx, "segment.replace", {"target": segment, "source": wt(made["cut"])})
        read = await val(ctx, info, "p95e_seg")
        moved = not read["name"].endswith("_a")
        rec(
            ctx,
            "r_trimmed_clip_answers_what_happened",
            (env.get("ok") is True) == moved,
            code=env.get("errorCode"),
            env=env,
            read=read,
        )
        env = await do(s, ctx, "segment.replace", {"target": segment, "source": wt(made["other"])})
        res = env.get("result") or {}
        rec(
            ctx,
            "r_same_media_other_name",
            env.get("ok") is True,
            verified=res.get("verified"),
            env=env,
            read=await val(ctx, info, "p95e_seg"),
        )
        env = await do(s, ctx, "segment.replace", {"target": segment, "source": wt(made["other"])})
        res = env.get("result") or {}
        rec(
            ctx,
            "r_same_clip_again_is_not_verified",
            env.get("ok") is True and res.get("verified") is False,
            env=env,
        )


NEW_FOR_USAGE: Final[str] = """
_S = flame.find_by_wiretap_node_id(@SEQREEL@)
_a = flame.find_by_wiretap_node_id(@TINY@)
_fps = str(_v(_a.frame_rate))
_seq = _S.create_sequence(name=@BASE@ + '_useq', video_tracks=1, frame_rate=_fps)
_t0 = list(list(_seq.versions)[0].tracks)[0]
_seq.overwrite(_a, flame.PyTime(1, _fps), _t0)
_a.in_mark = 5
_a.out_mark = 10
_seq.overwrite(_a, flame.PyTime(49, _fps), _t0)
_a.in_mark = None
_a.out_mark = None
_result = {
    'useq': _seq.get_wiretap_node_id(),
    'paths': [str(_v(x.file_path)) for x in _t0.segments],
    'types': [str(_v(x.type)) for x in _t0.segments],
}
"""


async def stage_u(ctx: Ctx) -> None:
    if "tiny" not in ctx.ids:
        await stage_r(ctx)
    made = await val(
        ctx,
        fill(NEW_FOR_USAGE, BASE=base(ctx), SEQREEL=ctx.ids["seqReel"], TINY=ctx.ids["tiny"]),
        "p95e_usage_setup",
    )
    async with server_session(ctx, "u") as s:
        env = await do(
            s,
            ctx,
            "timeline.usage",
            {"report": "shared", "sequences": [wt(made["useq"])]},
        )
        rec(ctx, "u_shared_runs", env.get("ok"), env=env, paths=made["paths"], types=made["types"])
        env = await do(
            s,
            ctx,
            "timeline.usage",
            {"report": "unlinked", "sequences": [wt(made["useq"])]},
        )
        rec(ctx, "u_unlinked_runs", env.get("ok"), env=env)


IMPORT_SHIFTED: Final[str] = """
_R = flame.find_by_wiretap_node_id(@REEL@)
_c = flame.import_clips(@PATTERN@, _R)[0]
_c.name = @BASE@ + '_sf'
_result = {'clip': _c.get_wiretap_node_id(), 'start_frame': _v(_c.start_frame)}
"""


async def stage_s(ctx: Ctx) -> None:
    copy = SCRATCH / f"e2e_sf_{ctx.ts}"
    copy.mkdir(parents=True)
    for offset, tif in enumerate(sorted(TIFS.glob("*.tif"))):
        shutil.copyfile(tif, copy / f"sf.{FIRST_NUMBER + offset:08d}.tif")
    last = FIRST_NUMBER + len(list(copy.glob("*.tif"))) - 1
    pattern = str(copy / f"sf.[{FIRST_NUMBER:08d}-{last:08d}].tif")
    made = await val(
        ctx,
        fill(IMPORT_SHIFTED, BASE=base(ctx), REEL=ctx.ids["reel"], PATTERN=pattern),
        "p95e_import_sf",
    )
    out = SCRATCH / f"e2e_still_{ctx.ts}"
    out.mkdir(parents=True)
    async with server_session(ctx, "s") as s:
        env = await do(
            s,
            ctx,
            "render.still",
            {"source": {"target": wt(made["clip"]), "frame": 8}, "output_dir": str(out)},
        )
        names = sorted(p.name for p in out.rglob("*") if p.is_file()) if out.exists() else []
        rec(
            ctx,
            "s_still_start_frame_1001",
            env.get("ok")
            and made["start_frame"] == FIRST_NUMBER
            and any("1008" in n for n in names),
            env=env,
            names=names,
            made=made,
        )


REORDER_SETUP: Final[str] = """
_R = flame.find_by_wiretap_node_id(@REEL@)
_rg = _R.parent
_new = _rg.create_reel(@BASE@ + '_ro')
_a = flame.find_by_wiretap_node_id(@A@)
for _n in ('a1', 'a2', 'a3', 'a4', 'a5'):
    _x = flame.media_panel.copy([_a], _new)[0]
    _x.name = _n
_result = {
    'reel': _new.get_wiretap_node_id(),
    'rows': [[str(_v(x.name)), str(_v(x.uid)), x.get_wiretap_node_id()] for x in _new.clips],
}
"""

REORDER_READ: Final[str] = """
_R = flame.find_by_wiretap_node_id(@REEL@)
_result = [[str(_v(x.name)), str(_v(x.uid)), x.get_wiretap_node_id()] for x in _R.clips]
"""


RENAME_TWINS: Final[str] = """
_R = flame.find_by_wiretap_node_id(@REEL@)
for _x in _R.clips:
    _x.name = 'twin'
_result = True
"""


async def stage_o(ctx: Ctx) -> None:
    made = await val(
        ctx,
        fill(REORDER_SETUP, BASE=base(ctx), REEL=ctx.ids["reel"], A=ctx.ids["a"]),
        "p95e_reorder_setup",
    )
    rows = {r[0]: r for r in made["rows"]}
    order = [wt(rows[n][2]) for n in ("a1", "a2", "a4", "a5", "a3")]
    read = fill(REORDER_READ, REEL=made["reel"])
    async with server_session(ctx, "o") as s:
        env = await do(s, ctx, "desktop.reorder", {"destination": wt(made["reel"]), "order": order})
        now = await val(ctx, read, "p95e_reorder_read")
        kept = {r[0]: r[1] == rows[r[0]][1] for r in now}
        rec(
            ctx,
            "o_order_and_uids",
            env.get("ok")
            and [r[0] for r in now] == ["a1", "a2", "a4", "a5", "a3"]
            and kept == {"a1": True, "a2": True, "a4": True, "a5": True, "a3": False},
            env=env,
            now=now,
        )
        group = (env.get("journal") or {}).get("group")
        back = await do(s, ctx, "journal.revert", {"group": group})
        again = await val(ctx, read, "p95e_reorder_read2")
        rec(
            ctx,
            "o_revert",
            back.get("ok") and [r[0] for r in again] == ["a1", "a2", "a3", "a4", "a5"],
            env=back,
            now=again,
        )
        by_name = {r[0]: r for r in again}
        wanted = ["a4", "a1", "a5", "a2", "a3"]
        env = await do(
            s,
            ctx,
            "desktop.reorder",
            {"destination": wt(made["reel"]), "order": [by_name[n][1] for n in wanted]},
        )
        now = await val(ctx, read, "p95e_reorder_read3")
        rec(
            ctx,
            "o_many_moves_by_uid",
            env.get("ok") and [r[0] for r in now] == wanted,
            env=env,
            now=now,
        )
        group = (env.get("journal") or {}).get("group")
        check = await do(s, ctx, "journal.check", {"group": group})
        rec(ctx, "o_journal_check", check.get("ok"), env=check)
        back = await do(s, ctx, "journal.revert", {"group": group})
        again = await val(ctx, read, "p95e_reorder_read4")
        rec(
            ctx,
            "o_many_moves_revert",
            back.get("ok") and [r[0] for r in again] == ["a1", "a2", "a3", "a4", "a5"],
            env=back,
            now=again,
        )
        await val(ctx, fill(RENAME_TWINS, REEL=made["reel"]), "p95e_twins")
        twins = await val(ctx, read, "p95e_reorder_read5")
        env = await do(
            s,
            ctx,
            "desktop.reorder",
            {
                "destination": wt(made["reel"]),
                "order": [twins[i][1] for i in (1, 0, 2, 4, 3)],
            },
        )
        now = await val(ctx, read, "p95e_reorder_read6")
        uid_map = (env.get("result") or {}).get("uidMap") or []
        readable = {r[1] for r in now}
        rec(
            ctx,
            "o_same_names_uid_map_is_real",
            env.get("ok") and all(m.get("to") in readable for m in uid_map),
            env=env,
            now=now,
        )


CLEANUP: Final[str] = """
_gone = []
for _g in list(_d.batch_groups):
    if str(_v(_g.name)).startswith(@BASE@):
        _gone.append(str(_v(_g.name)))
        flame.delete(_g, confirm=False)
for _g in list(_d.reel_groups):
    if str(_v(_g.name)).startswith(@BASE@):
        _gone.append(str(_v(_g.name)))
        flame.delete(_g, confirm=False)
_result = _gone
"""


async def stage_z(ctx: Ctx) -> None:
    gone = await val(ctx, fill(CLEANUP, BASE=base(ctx)), "p95e_cleanup", 120.0)
    rec(ctx, "z_cleanup", True, deleted=gone)


async def async_main(ctx: Ctx, stages: str) -> None:
    table = {
        "c": stage_c,
        "t": stage_t,
        "b": stage_b,
        "r": stage_r,
        "u": stage_u,
        "s": stage_s,
        "o": stage_o,
    }
    try:
        await guarded(ctx, "prepare", prepare)
        if "a" in ctx.ids:
            for key in stages:
                await guarded(ctx, key, table[key])
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p95e_exit")
        save_stage(ctx)
        save(ctx.results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--stage", default="ctbruso")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir, e2e_guard.DEFAULT_WAIT_SOCKET_S)
    if refused is not None:
        return refused
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_p95_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p95_prep_ops_e2e_{ts}.json",
    }
    ctx = Ctx(run_dir, results, ts, journal_dir)
    try:
        anyio.run(async_main, ctx, args.stage)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()
    save(results)
    failed = [s["name"] for s in results["steps"] if not s["ok"]]
    print(f"Result: {results['_out_path']}")
    print(f"steps={len(results['steps'])} failed={len(failed)}: {failed}")
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
