#!/usr/bin/env python3
"""Real-Flame end-to-end: a restore puts the schematic reels back (run with the `.venv`
Python; `scripts/run_e2e.sh p2_3r_reel_fidelity` starts Flame).

Checks: Restoring a Batch checkpoint after `batch.organize`, `eval.run` or `batch.load_setup`: the
schematic reels, their clips and the Clip nodes come back as they were, and a render after the
restore gives the same files.

Same shape and helpers as scripts/p2_3a_batch_e2e.py / p2_3b_node_action_e2e.py (imported from
p2_2a / p2_3a / p1): a real MCP stdio client drives `python -m flame_mcp` against a live Flame +
dev bridge; `flame_mcp.transport` is scaffolding only (create the groups, and READ EVERY STATE
BACK INDEPENDENTLY of the ops under test -- reels and their clips, Clip nodes and their media,
render output). Everything happens in the probe project `flame_mcp_probe`, in Batch groups that
are NOT current (`CUR`, created last, is the current one). The journal directory is this run's own
(`p2/journal_p23r_e2e_<ts>/`, never the production one). No test hook is set for any server.
Everything this script writes to disk goes under `~/Library/Application Support/flame-mcp/p2/`
(never /tmp or /var/tmp: the Write File node's default output is /var/tmp, so its path is changed
BEFORE any render).

Each group `R_<A..E>` is created the same way: reels ["SrcA", "EmptyB", "Keep"] (SrcA holds a clip
copied from a Library clip, so it has a Clip node; EmptyB and Keep are empty), one shelf reel, and
Clip node -> Write File (range 1-8).

JUDGED steps (`ok` decides the run) are the ones the design fixes; OBSERVED steps
(`kind: "observation"`, always ok) record what Flame did where nothing was known. They are
collected in results["observations"] and printed as a table at the end.

Stages
  a  batch.organize (checkpointed) -> journal.revert: reels equal the creation state (names, empty
     or not, clip names), result.reelsRestored is true (it looks at the SCHEMATIC reels only), the
     reelTidy has no error, the fingerprint of the reels section and the default one equal the
     ones before the op (the all-section fingerprint and reelTidy.outsideTheCheckpoint -- shelf
     reels and iterations, which a restore never brings back -- are OBSERVED),
     the Clip node's media_path / duration / clip name are the same. OBSERVED: what the tidy
     created and deleted, the order of the reels, the uids that changed.
  b  eval.run (destructive, FLAME_MCP_ENABLE_EVAL=1; adds nodes) -> journal.revert: the same checks.
  c  batch.save_setup (a setup file of the group) -> transport adds a reel ("Extra") and a node ->
     batch.load_setup of that file -> journal.revert: the reels are the ones before the load
     (Extra included), reelsRestored is true. OBSERVED: what the load_setup itself did to the
     reels (its reelTidy is skipped: a file's own reels are kept).
  d  render Write File -> .exr md5 list; batch.organize -> journal.revert; the Write File's output
     directory is changed (media_path is set to a new directory, written back and read back),
     render again -> the md5 list of the .exr files is the same as before.
  e  a checkpointed write (batch.organize); afterwards transport makes a reel that holds a clip
     (a Clip node comes with it); a plain journal.revert is JUDGED to be refused as CONFLICT (the
     group's nodes changed since the run); then journal.revert(force=true). JUDGED: the tidy did
     not delete that reel. OBSERVED: whether the reel is still there (measured: it is not -- the
     load_setup itself drops what the setup lacks, and the Clip node with it), its clips, its Clip
     node, what the tidy kept.
  z  cross-cutting checks over every envelope; nothing left undecided

Cleanup: no Batch group is deleted (flame.delete on a PyBatch is not used); checkpoint files,
setup files and renders are never deleted; then flame.exit().
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Final

import anyio
import e2e_guard
from p1_mcp_e2e import (
    MEDIA_DIR,
    RUN_DIR_DEFAULT,
    _br_dict,
    _result_of,
    _tx,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (
    V_PRELUDE,
    Ctx,
    do,
    guarded,
    rec,
    save,
    save_stage,
    server_session,
    tx,
    version_info,
)
from p2_3a_batch_e2e import ALL_SECTIONS, EVAL_MAKE_PAIR, _locate, observe, tx_world

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
ENABLE_EVAL: Final[str] = "FLAME_MCP_ENABLE_EVAL"
GROUPS: Final[tuple[str, ...]] = ("A", "B", "C", "D", "E")
REELS: Final[tuple[str, ...]] = ("SrcA", "EmptyB", "Keep")
SRC_REEL: Final[str] = "SrcA"
USER_REEL: Final[str] = "UserFilled"
EXTRA_REEL: Final[str] = "Extra"
RANGE_START: Final[int] = 1
RANGE_END: Final[int] = 8
RENDER_TIMEOUT_S: Final[float] = 300.0
GROUP_TIMEOUT_S: Final[float] = 90.0
FRAME_GLOB: Final[str] = "*.exr"

Results = dict[str, Any]
State = dict[str, Any]


# Generated transport code (pure: also compiled by --dry-run)
def code_library_clip(lib_name: str, clip_path: str) -> str:
    return "\n".join(
        [
            "ws = flame.projects.current_project.current_workspace",
            f"lib = ws.create_library({lib_name!r})",
            "reel = lib.create_reel('R1')",
            f"_c = flame.import_clips({clip_path!r}, reel)[0]",
            "_result = {'name': _v(_c.name), 'wiretap': _c.get_wiretap_node_id()}",
        ]
    )


def code_make_group(name: str, clip_wiretap: str, out_dir: str) -> str:
    return "\n".join(
        [
            "_desktop = flame.projects.current_project.current_workspace.desktop",
            f"_bg = _desktop.create_batch_group({name!r}, nb_reels={len(REELS)}, "
            f"reels={list(REELS)!r}, nb_shelf_reels=1)",
            f"_src = [r for r in _bg.reels if _v(r.name) == {SRC_REEL!r}][0]",
            f"_clip = flame.find_by_wiretap_node_id({clip_wiretap!r})",
            "flame.media_panel.copy(_clip, _src)",
            "_cn = [n for n in _bg.nodes if type(n).__name__ == 'PyClipNode']",
            "_wf = _bg.create_node('Write File')",
            "_connected = _bg.connect_nodes(_cn[0], 'Default', _wf, 'Default')",
            f"_wf.media_path.set_value({out_dir!r})",
            f"_wf.range_start.set_value({RANGE_START})",
            f"_wf.range_end.set_value({RANGE_END})",
            "_result = {'uid': _v(_bg.uid), 'name': _v(_bg.name), 'clipNodes': len(_cn),",
            "           'writeFile': _v(_wf.name), 'connected': _connected,",
            "           'reels': [_v(r.name) for r in _bg.reels]}",
        ]
    )


def code_clip_nodes(uid: str) -> str:
    return "\n".join(
        [
            _locate(uid),
            "def _plain(x):",
            "    x = _v(x)",
            "    return x if isinstance(x, (str, int, float, bool, type(None))) else repr(x)",
            "_rows = []",
            "for _n in _bg.nodes:",
            "    if type(_n).__name__ != 'PyClipNode':",
            "        continue",
            "    _row = {'name': _v(_n.name)}",
            "    for _k in ('media_path', 'duration', 'clip_name'):",
            "        try:",
            "            _row[_k] = _plain(getattr(_n, _k))",
            "        except BaseException as _e:",
            "            _row[_k] = 'ERR ' + type(_e).__name__ + ': ' + str(_e)",
            "    try:",
            "        _row['clip'] = [_v(_n.clip.name), None]",
            "        _row['clip'][1] = _v(_n.clip.uid)",
            "    except BaseException as _e:",
            "        if not isinstance(_row.get('clip'), list):",
            "            _row['clip'] = 'ERR ' + type(_e).__name__ + ': ' + str(_e)",
            "        else:",
            "            _row['clip'][1] = 'ERR ' + type(_e).__name__ + ': ' + str(_e)",
            "    _rows.append(_row)",
            "_result = sorted(_rows, key=lambda r: r['name'])",
        ]
    )


def code_reel_uids(uid: str) -> str:
    return "\n".join(
        [
            _locate(uid),
            "def _uid(o):",
            "    try:",
            "        return _v(o.uid)",
            "    except BaseException as _e:",
            "        return 'ERR ' + type(_e).__name__ + ': ' + str(_e)",
            "_result = {_v(r.name): [_uid(r), [_uid(c) for c in r.clips]] for r in _bg.reels}",
        ]
    )


def code_set_write_file(uid: str, out_dir: str) -> str:
    return "\n".join(
        [
            _locate(uid),
            "_w = [n for n in _bg.nodes if _v(n.type) == 'Write File'][0]",
            f"_w.media_path.set_value({out_dir!r})",
            f"_w.range_start.set_value({RANGE_START})",
            f"_w.range_end.set_value({RANGE_END})",
            "_result = {'name': _v(_w.name), 'media_path': _v(_w.media_path),",
            "           'range_start': _v(_w.range_start), 'range_end': _v(_w.range_end)}",
        ]
    )


def code_read_write_file(uid: str) -> str:
    return "\n".join(
        [
            _locate(uid),
            "_w = [n for n in _bg.nodes if _v(n.type) == 'Write File'][0]",
            "_result = {'name': _v(_w.name), 'media_path': _v(_w.media_path),",
            "           'range_start': _v(_w.range_start), 'range_end': _v(_w.range_end)}",
        ]
    )


def code_render(uid: str, out_dir: str, files_root: str) -> str:
    return "\n".join(
        [
            _locate(uid),
            "import glob, hashlib, os, time",
            "_w = [n for n in _bg.nodes if _v(n.type) == 'Write File'][0]",
            f"_dir = {out_dir!r}",
            f"_root = {files_root!r}",
            "_mp = _v(_w.media_path)",
            "_guard = {",
            "    'path_ok': isinstance(_mp, str)",
            "    and os.path.normpath(_mp) == os.path.normpath(_dir),",
            "    'under_root': os.path.normpath(_dir)",
            "    .startswith(os.path.normpath(_root) + os.sep),",
            "}",
            "_out = {'guard': _guard, 'media_path': _mp}",
            "if all(_guard.values()):",
            "    os.makedirs(_dir, exist_ok=True)",
            "    _t0 = time.monotonic()",
            "    try:",
            "        _ret = _bg.render(render_option='Foreground')",
            "        _out['render'] = {'ok': True, 'ret': repr(_ret)}",
            "    except BaseException as _e:",
            "        _out['render'] = {'ok': False, 'error': type(_e).__name__ + ': ' + str(_e)}",
            "    _out['elapsed_s'] = round(time.monotonic() - _t0, 3)",
            f"    _pat = os.path.join(_dir, '**', {FRAME_GLOB!r})",
            "    _files = sorted(glob.glob(_pat, recursive=True))",
            "    _out['files'] = [",
            "        [os.path.relpath(_f, _dir),",
            "         hashlib.md5(open(_f, 'rb').read()).hexdigest(), os.path.getsize(_f)]",
            "        for _f in _files",
            "    ]",
            "else:",
            "    _out['skipped'] = 'guard_failed_not_rendered'",
            "_result = _out",
        ]
    )


def code_add_reel(uid: str, name: str, clip_wiretap: str | None) -> str:
    lines = [_locate(uid), f"_r = _bg.create_reel({name!r})"]
    if clip_wiretap is not None:
        lines += [
            f"_clip = flame.find_by_wiretap_node_id({clip_wiretap!r})",
            "flame.media_panel.copy(_clip, _r)",
        ]
    lines += ["_result = _v(_r.name)"]
    return "\n".join(lines)


def code_add_node(uid: str) -> str:
    return "\n".join([_locate(uid), "_result = _v(_bg.create_node('Mux').name)"])


DRY_RUN_CODE: Final[dict[str, str]] = {
    "library_clip": code_library_clip("LIB", "/x/clip1.mov"),
    "make_group": code_make_group("G", "/w/clip", "/o/"),
    "clip_nodes": code_clip_nodes("uid"),
    "reel_uids": code_reel_uids("uid"),
    "set_write_file": code_set_write_file("uid", "/o/"),
    "read_write_file": code_read_write_file("uid"),
    "render": code_render("uid", "/o/x", "/o"),
    "add_reel": code_add_reel("uid", "R", "/w/clip"),
    "add_node": code_add_node("uid"),
}


async def run_code(ctx: Ctx, code: str, label: str, timeout_s: float = 40.0) -> Any:
    br = await tx(ctx, code, label=label, timeout_s=timeout_s)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


def files_dir(ctx: Ctx) -> Path:
    return Path(str(ctx.ids["filesDir"]))


def uid_of(ctx: Ctx, key: str) -> str:
    return str(ctx.ids["groups"][key]["uid"])


def target_of(ctx: Ctx, key: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid_of(ctx, key)}


def reel_view(world: State) -> list[Any]:
    """Names, whether empty, clip and sequence names -- what a restore is to put back (uids are
    not: reels that hold clips are rebuilt with new ones)."""
    return sorted(
        (r["name"], not r["clips"] and not r["sequences"], list(r["clips"]), list(r["sequences"]))
        for r in world.get("reels", [])
    )


def clip_node_view(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    return [(r["name"], r.get("media_path"), r.get("duration"), r.get("clip_name")) for r in rows]


def clip_names_of(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    return [
        (
            r["name"],
            (r.get("clip") or [None])[0] if isinstance(r.get("clip"), list) else r.get("clip"),
        )
        for r in rows
    ]


async def fingerprints(session: Any, ctx: Ctx, target: dict[str, Any]) -> dict[str, Any]:
    plain = await do(session, ctx, "batch.fingerprint", {"target": target})
    reels = await do(session, ctx, "batch.fingerprint", {"target": target, "sections": ["reels"]})
    every = await do(
        session, ctx, "batch.fingerprint", {"target": target, "sections": ALL_SECTIONS}
    )
    return {
        "plain": _result_of(plain).get("fingerprint"),
        "reels": _result_of(reels).get("fingerprint"),
        "allSections": _result_of(every).get("fingerprint"),
    }


async def snapshot(session: Any, ctx: Ctx, key: str) -> State:
    uid = uid_of(ctx, key)
    return {
        "world": await tx_world(ctx, uid),
        "reelUids": await run_code(ctx, code_reel_uids(uid), "p23r_reel_uids"),
        "clipNodes": await run_code(ctx, code_clip_nodes(uid), "p23r_clip_nodes"),
        "writeFile": await run_code(ctx, code_read_write_file(uid), "p23r_write_file"),
        "fp": await fingerprints(session, ctx, target_of(ctx, key)),
    }


async def prepare(ctx: Ctx) -> None:
    root = files_dir(ctx)
    root.mkdir(parents=True, exist_ok=True)
    lib_name = f"P23R_LIB_{ctx.ts}"
    clip = await tx(
        ctx,
        code_library_clip(lib_name, str(MEDIA_DIR / "clip1.mov")),
        label="p23r_library",
        timeout_s=80.0,
    )
    rec(ctx, "prepare_library_clip", clip.ok, resp=_br_dict(clip))
    if not (clip.ok and isinstance(clip.result, dict)):
        return
    ctx.ids["libClip"] = clip.result["wiretap"]
    groups: dict[str, Any] = {}
    for key in GROUPS:
        made = await tx(
            ctx,
            code_make_group(
                f"P23R_{key}_{ctx.ts}",
                str(ctx.ids["libClip"]),
                str(root / f"render_init_{key}") + "/",
            ),
            label=f"p23r_group_{key}",
            timeout_s=GROUP_TIMEOUT_S,
        )
        ok = (
            made.ok
            and isinstance(made.result, dict)
            and made.result.get("clipNodes") == 1
            and made.result.get("connected") is not False
            and sorted(made.result.get("reels", [])) == sorted(REELS)
        )
        rec(ctx, f"prepare_group_{key}", ok, resp=_br_dict(made))
        if made.ok and isinstance(made.result, dict):
            groups[key] = made.result
    ctx.ids["groups"] = groups
    decoy = await tx(
        ctx,
        "\n".join(
            [
                "_d = flame.projects.current_project.current_workspace.desktop",
                f"_x = _d.create_batch_group('P23R_CUR_{ctx.ts}', nb_reels=1, nb_shelf_reels=1)",
                "_cur = _v(_d.current_batch_group)",
                "_result = {'decoy': _v(_x.uid), 'current': _v(_cur.uid) if _cur else None}",
            ]
        ),
        label="p23r_decoy",
        timeout_s=GROUP_TIMEOUT_S,
    )
    others = {v["uid"] for v in groups.values()}
    rec(
        ctx,
        "prepare_the_decoy_is_current_and_no_test_group_is",
        decoy.ok
        and isinstance(decoy.result, dict)
        and decoy.result["current"] == decoy.result["decoy"]
        and decoy.result["current"] not in others,
        resp=_br_dict(decoy),
    )


# The shared check after a revert
def tidy_of(env: dict[str, Any]) -> dict[str, Any]:
    tidy = _result_of(env).get("reelTidy")
    return tidy if isinstance(tidy, dict) else {}


def check_reels_back(
    ctx: Ctx, prefix: str, before: State, after: State, revert: dict[str, Any]
) -> None:
    tidy = tidy_of(revert)
    result = _result_of(revert)
    rec(ctx, f"{prefix}_the_revert_is_ok", revert.get("ok") is True, env=revert)
    rec(
        ctx,
        f"{prefix}_reels_restored_is_true",
        result.get("reelsRestored") is True,
        reelsRestored=result.get("reelsRestored"),
        differences=tidy.get("differences"),
        tidy={k: tidy.get(k) for k in ("ran", "skipped", "reason", "created", "deleted", "kept")},
    )
    rec(
        ctx,
        f"{prefix}_the_tidy_reported_no_error",
        tidy.get("ran") is True and tidy.get("errors") == [],
        errors=tidy.get("errors"),
    )
    rec(
        ctx,
        f"{prefix}_reels_equal_the_state_before_the_op",
        reel_view(after["world"]) == reel_view(before["world"]),
        before=reel_view(before["world"]),
        after=reel_view(after["world"]),
    )
    rec(
        ctx,
        f"{prefix}_the_reels_section_fingerprint_equals_the_one_before",
        isinstance(before["fp"]["reels"], str) and before["fp"]["reels"] == after["fp"]["reels"],
        before=before["fp"],
        after=after["fp"],
    )
    rec(
        ctx,
        f"{prefix}_the_default_fingerprint_equals_the_one_before",
        isinstance(before["fp"]["plain"], str) and before["fp"]["plain"] == after["fp"]["plain"],
    )
    rec(
        ctx,
        f"{prefix}_the_clip_node_media_is_the_same",
        clip_node_view(before["clipNodes"]) == clip_node_view(after["clipNodes"])
        and isinstance(before["clipNodes"], list)
        and len(before["clipNodes"]) >= 1,
        before=before["clipNodes"],
        after=after["clipNodes"],
    )
    observe(
        ctx,
        f"{prefix}_observation_what_the_tidy_did_and_what_is_not_the_same",
        created=tidy.get("created"),
        deleted=tidy.get("deleted"),
        kept=tidy.get("kept"),
        recordedReels=tidy.get("recordedReels"),
        reelsAfter=tidy.get("reelsAfter"),
        orderMatches=tidy.get("orderMatches"),
        outsideTheCheckpoint=tidy.get("outsideTheCheckpoint"),
        allSectionFingerprintBackToBefore=before["fp"]["allSections"] == after["fp"]["allSections"],
        reelUidsBefore=before["reelUids"],
        reelUidsAfter=after["reelUids"],
        clipNodeUidsBefore=clip_names_of(before["clipNodes"]),
        clipNodeUidsAfter=clip_names_of(after["clipNodes"]),
        writeFileBefore=before["writeFile"],
        writeFileAfter=after["writeFile"],
    )


async def stage_a(ctx: Ctx) -> None:
    async with server_session(ctx, "a-plain") as session:
        before = await snapshot(session, ctx, "A")
        env = await do(session, ctx, "batch.organize", {"target": target_of(ctx, "A")})
        rec(
            ctx,
            "a_organize_ran_and_names_a_group",
            env.get("ok") is True and isinstance((env.get("journal") or {}).get("group"), str),
            env=env,
        )
        mid = await snapshot(session, ctx, "A")
        observe(ctx, "a_observation_the_reels_right_after_the_op", reels=reel_view(mid["world"]))
        revert = await do(
            session, ctx, "journal.revert", {"group": (env.get("journal") or {}).get("group")}
        )
        after = await snapshot(session, ctx, "A")
        check_reels_back(ctx, "a", before, after, revert)


async def stage_b(ctx: Ctx) -> None:
    async with server_session(ctx, "b-eval", hooks={ENABLE_EVAL: "1"}) as session:
        before = await snapshot(session, ctx, "B")
        env = await do(
            session,
            ctx,
            "eval.run",
            {"code": EVAL_MAKE_PAIR, "effect": "destructive", "target": target_of(ctx, "B")},
        )
        journal = env.get("journal") or {}
        rec(
            ctx,
            "b_destructive_eval_ran_and_names_a_group",
            env.get("ok") is True and isinstance(journal.get("group"), str),
            env=env,
        )
        revert = await do(session, ctx, "journal.revert", {"group": journal.get("group")})
        after = await snapshot(session, ctx, "B")
        check_reels_back(ctx, "b", before, after, revert)


async def stage_c(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "C"), target_of(ctx, "C")
    setup_base = str(files_dir(ctx) / "c_setup")
    async with server_session(ctx, "c-plain") as session:
        saved = await do(session, ctx, "batch.save_setup", {"target": target, "path": setup_base})
        path = _result_of(saved).get("path")
        rec(
            ctx,
            "c_the_setup_was_saved",
            saved.get("ok") is True and isinstance(path, str),
            env=saved,
        )
        made = [
            await run_code(ctx, code_add_reel(uid, EXTRA_REEL, None), "p23r_c_reel"),
            await run_code(ctx, code_add_node(uid), "p23r_c_node"),
        ]
        rec(
            ctx,
            "c_transport_added_a_reel_and_a_node",
            made[0] == EXTRA_REEL and isinstance(made[1], str),
            made=made,
        )
        before = await snapshot(session, ctx, "C")
        loaded = await do(session, ctx, "batch.load_setup", {"target": target, "path": path})
        rec(
            ctx,
            "c_load_setup_ran_and_says_its_reels_were_not_tidied",
            loaded.get("ok") is True
            and tidy_of(loaded).get("skipped") is True
            and _result_of(loaded).get("reelsRestored") is None,
            env=loaded,
        )
        mid = await snapshot(session, ctx, "C")
        observe(
            ctx,
            "c_observation_what_load_setup_itself_did_to_the_reels",
            before=reel_view(before["world"]),
            after=reel_view(mid["world"]),
        )
        revert = await do(
            session, ctx, "journal.revert", {"group": (loaded.get("journal") or {}).get("group")}
        )
        after = await snapshot(session, ctx, "C")
        check_reels_back(ctx, "c", before, after, revert)
        rec(
            ctx,
            "c_the_extra_reel_that_was_there_before_the_load_is_back",
            EXTRA_REEL in [r[0] for r in reel_view(after["world"])],
            reels=reel_view(after["world"]),
        )


# md5 of the rendered files
def md5_rows(render: Any) -> list[Any]:
    files = render.get("files") if isinstance(render, dict) else None
    return files if isinstance(files, list) else []


async def stage_d(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "D"), target_of(ctx, "D")
    root = files_dir(ctx)
    before_dir, after_dir = str(root / "render_d_before") + "/", str(root / "render_d_after") + "/"
    set_before = await run_code(ctx, code_set_write_file(uid, before_dir), "p23r_d_path_before")
    rec(
        ctx,
        "d_the_write_file_output_directory_is_ours_and_reads_back",
        isinstance(set_before, dict)
        and os.path.normpath(str(set_before.get("media_path"))) == os.path.normpath(before_dir)
        and set_before.get("range_end") == RANGE_END,
        resp=set_before,
    )
    first = await run_code(
        ctx, code_render(uid, before_dir, str(root)), "p23r_d_render_before", RENDER_TIMEOUT_S
    )
    rec(
        ctx,
        f"d_the_render_before_wrote_{RANGE_END}_frames",
        isinstance(first, dict)
        and (first.get("render") or {}).get("ok") is True
        and len(md5_rows(first)) == RANGE_END,
        resp=first,
    )
    async with server_session(ctx, "d-plain") as session:
        before = await snapshot(session, ctx, "D")
        env = await do(session, ctx, "batch.organize", {"target": target})
        revert = await do(
            session, ctx, "journal.revert", {"group": (env.get("journal") or {}).get("group")}
        )
        after = await snapshot(session, ctx, "D")
        check_reels_back(ctx, "d", before, after, revert)
    restored_wf = after["writeFile"]
    observe(ctx, "d_observation_the_write_file_after_the_restore", writeFile=restored_wf)
    set_after = await run_code(ctx, code_set_write_file(uid, after_dir), "p23r_d_path_after")
    rec(
        ctx,
        "d_the_output_directory_after_the_restore_is_ours_and_reads_back",
        isinstance(set_after, dict)
        and os.path.normpath(str(set_after.get("media_path"))) == os.path.normpath(after_dir),
        resp=set_after,
    )
    second = await run_code(
        ctx, code_render(uid, after_dir, str(root)), "p23r_d_render_after", RENDER_TIMEOUT_S
    )
    rec(
        ctx,
        f"d_the_render_after_wrote_{RANGE_END}_frames",
        isinstance(second, dict)
        and (second.get("render") or {}).get("ok") is True
        and len(md5_rows(second)) == RANGE_END,
        resp=second,
    )
    rec(
        ctx,
        "d_the_exr_files_before_and_after_the_restore_are_byte_identical_by_md5",
        len(md5_rows(first)) == RANGE_END and md5_rows(first) == md5_rows(second),
        before=md5_rows(first),
        after=md5_rows(second),
    )


async def stage_e(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "E"), target_of(ctx, "E")
    async with server_session(ctx, "e-plain") as session:
        env = await do(session, ctx, "batch.organize", {"target": target})
        group = (env.get("journal") or {}).get("group")
        rec(
            ctx,
            "e_organize_ran_and_names_a_group",
            env.get("ok") is True and isinstance(group, str),
            env=env,
        )
        made = await run_code(
            ctx, code_add_reel(uid, USER_REEL, str(ctx.ids["libClip"])), "p23r_e_user_reel"
        )
        rec(ctx, "e_transport_made_a_reel_that_holds_a_clip", made == USER_REEL, resp=made)
        mid = await snapshot(session, ctx, "E")
        user_rows = [r for r in mid["world"]["reels"] if r["name"] == USER_REEL]
        rec(
            ctx,
            "e_the_user_reel_holds_a_clip_before_the_revert",
            len(user_rows) == 1 and len(user_rows[0]["clips"]) >= 1,
            rows=user_rows,
        )
        plain = await do(session, ctx, "journal.revert", {"group": group})
        rec(
            ctx,
            "e_a_plain_revert_is_refused_as_conflict_after_the_user_changed_the_group",
            plain.get("ok") is False and plain.get("errorCode") == "CONFLICT",
            env=plain,
        )
        revert = await do(session, ctx, "journal.revert", {"group": group, "force": True})
        after = await snapshot(session, ctx, "E")
        tidy = tidy_of(revert)
        deleted = [d.get("name") for d in tidy.get("deleted") or []]
        rec(ctx, "e_the_revert_is_ok", revert.get("ok") is True, env=revert)
        rec(
            ctx,
            "e_the_tidy_did_not_delete_the_user_reel",
            USER_REEL not in deleted and tidy.get("ran") is True,
            deleted=deleted,
            kept=tidy.get("kept"),
            errors=tidy.get("errors"),
        )
        after_rows = [r for r in after["world"]["reels"] if r["name"] == USER_REEL]
        observe(
            ctx,
            "e_observation_a_forced_revert_loses_the_reel_and_clip_added_after_the_checkpoint",
            userReelStillInTheGroup=len(after_rows) == 1,
            reelsAfter=reel_view(after["world"]),
            reelsRestored=_result_of(revert).get("reelsRestored"),
            userReelBefore=user_rows,
            userReelAfter=after_rows,
            kept=tidy.get("kept"),
            deleted=tidy.get("deleted"),
            differences=tidy.get("differences"),
            clipNodesBefore=clip_names_of(mid["clipNodes"]),
            clipNodesAfter=clip_names_of(after["clipNodes"]),
        )


async def stage_z(ctx: Ctx) -> None:
    async with server_session(ctx, "z-plain") as session:
        info = await version_info(session, ctx)
        journal = _result_of(info).get("journal", {})
        rec(
            ctx,
            "z_nothing_is_left_undecided_and_no_test_hook_is_active",
            journal.get("unresolvedIntents") == 0
            and journal.get("writesBlocked") is False
            and journal.get("testHooks") == [],
            journal=journal,
        )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def print_observations(results: Results) -> None:
    print("--- observations (nothing here is a pass/fail) ---")
    for obs in results.get("observations", []):
        name = obs.get("name")
        print(f"{name}: { {k: v for k, v in obs.items() if k not in ('name', 'stage')} }")


def cleanup(ctx: Ctx) -> None:
    ctx.stage = "cleanup"
    ctx.results["cleanup"] = {
        "deleted": [],
        "kept": {
            "batchGroups": ctx.ids.get("groups"),
            "checkpointFiles": str(ctx.journal_dir / "checkpoints"),
            "filesDir": str(ctx.ids.get("filesDir")),
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them); checkpoint files, setup files and "
                "renders are never deleted"
            ),
        },
    }
    exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label="p23r_cleanup_exit")
    rec(
        ctx,
        "cleanup_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
    )


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "groups" in ctx.ids and len(ctx.ids["groups"]) == len(GROUPS):
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("e", stage_e),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_stages_a_to_e_skipped", False, ids=ctx.ids)
        await guarded(ctx, "z", stage_z)
    finally:
        cleanup(ctx)
        save_stage(ctx)
        save(ctx.results)


def dry_run() -> int:
    bad = 0
    for name, code in DRY_RUN_CODE.items():
        try:
            compile(V_PRELUDE + "\n" + code, f"<dry:{name}>", "exec")
            print(f"compile ok: {name}")
        except SyntaxError as exc:
            bad += 1
            print(f"compile FAILED: {name}: {exc}")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument(
        "--dry-run", action="store_true", help="compile the generated Flame code; touch nothing"
    )
    args = parser.parse_args()
    if args.dry_run:
        return dry_run()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    os.environ.pop(ENABLE_EVAL, None)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_p23r_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_3r_reel_fidelity_e2e_{ts}.json",
    }
    ctx = Ctx(run_dir, results, ts, journal_dir)
    ctx.ids["filesDir"] = str(OUTPUT_DIR / f"files_p23r_{ts}")
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
    sys.exit(main())
