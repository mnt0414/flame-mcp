#!/usr/bin/env python3
"""Real-Flame end-to-end: `render.clip` renders the Timeline FX of a clip (run with the `.venv`
Python; `scripts/run_e2e.sh p5_render_clip` starts Flame; `--dry-run` connects to nothing, lists the
calls, compiles the code sent to the server and to Flame).

Checks: `render.clip` renders the Blur of a Desktop clip and of a Batch reel clip. It refuses a
clip without Timeline FX, a clip that is already rendered (unless `force=true`) and a clip in a
library. A render that outlasts `timeout_s` is decided by `journal.check`.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (for each clip: its effect types, `is_rendered()`, uid, wiretap
id, bit depth; the clip count of its reel; for the Batch group: the bit depth of every Clip node and
the node count). Everything this run makes is named `PP5E_<ts>_...` (scripts/
p2_4_clean_probe_project.py knows the prefix) and is deleted at the end by that prefix (the Desktop
reel group, the Batch group and the library; a Batch group is deleted with `flame.delete`, like the
other e2e that make one); then flame.exit(). The stash library name (`PP5E_<ts>_CKPT`, passed to
every server as FLAME_MCP_CHECKPOINT_LIBRARY) carries the prefix too. The journal directory is this
run's own (`p3/journal_p5rc_e2e_<ts>/`). No test hook is set for any server. The first step refuses
to run anywhere but in the project `flame_mcp_probe`.

Material (transport): a Desktop reel group `PP5E_<ts>_RG` with a reel SRC that holds four imported
clips -- A (p2_len48, one Blur), E (p2_len48, one Blur; it is copied into the library below), B
(p2_len72, no effect) and T (p2_len72, one Blur, for the stage that renders with timeout_s=1) --; a
library `PP5E_<ts>_LIB` with a reel LR holding a copy of E made before anything is rendered; a Batch
group `PP5E_<ts>_B` whose first schematic reel holds p2_len48 imported with
`PyBatch.import_clip`, with one Blur made on it inside the group.

Stage r
  1. render.clip of A with the defaults. JUDGED: ok, result.isRenderedBefore false and
     isRenderedAfter true, the effects [Blur]; the transport reads A rendered, with the same uid
     and wiretap id and the same clip count in SRC; the result file the bridge kept under the
     intent's handle was removed by the server's clean-up poll. OBSERVED: elapsed time, what
     `returned` was, the degraded notes.
  2. render.clip of A again. JUDGED: PRECONDITION_UNMET with result.isRendered true and a hint
     that names force=true; A is still rendered (transport); nothing was journaled.
  3. render.clip of A with force=true. JUDGED: ok with isRenderedBefore true and isRenderedAfter
     true.
  4. render.clip of B (no Timeline FX). JUDGED: PRECONDITION_UNMET whose message says there is no
     Timeline FX and does not say "already rendered"; nothing was journaled; B reads as before.
  5. render.clip of the library copy of E. JUDGED: PRECONDITION_UNMET naming the library; the copy
     reads as before (transport); nothing was journaled.
  6. render.clip of the Batch reel clip (by its wiretap id, else by a path selector). JUDGED: ok;
     the transport reads the clip rendered, its uid and the clip count of the reel unchanged, the
     node count of the group unchanged, and the Clip node of the clip at the bit depth of the clip
     (the node read at the bit depth Flame gave an unrendered clip before). OBSERVED: the node's
     bit depth before.
Stage t
  1. render.clip of T with timeout_s=1. OBSERVED: how it ended (a pending handle, BRIDGE_LOST, a
     bare TIMEOUT, or in time: a Blur render is quick, and when it finishes in time the TIMEOUT
     path is not exercised and nothing more is decided; the step is named `..._not_exercised`).
     If it did not finish in time, JUDGED: the server refuses writes, a second
     render.clip is POLICY_DENIED, journal.check (retried every 3 s for up to 180 s while its
     verdict is `unknown`) reaches `landed` with basis `handle`, the `polled` record names the
     intent's handle and holds isRenderedAfter true, the transport reads T rendered, the writes
     resume and the journal state follows the verdict.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
from collections.abc import Callable
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
    _result_of,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do,
    guarded,
    journal_index,
    judged,
    rec,
    save,
    save_stage,
    server_session,
    tx,
    version_info,
)
from p2_4_common import (  # noqa: E402
    flame_exit,
    journal_records,
    journal_states,
    observe,
    ops_used,
    print_observations,
)
from p3_1a_read_e2e import fill, is_error  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import CKPT_ENV  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.desktop_run import PHASE_ARG, PHASE_READ  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
NAME_PREFIX: Final[str] = "PP5E"
FILE_48: Final[Path] = MEDIA_DIR / "p2" / "p2_len48.mov"
FILE_72: Final[Path] = MEDIA_DIR / "p2" / "p2_len72.mov"
GOOD_STATES: Final[frozenset[str]] = frozenset({"landed", "not_landed", "failed", "conflict"})
MAKE_TIMEOUT_S: Final[float] = 180.0
CHECK_RETRY_EVERY_S: Final[float] = 3.0
CHECK_RETRY_TOTAL_S: Final[float] = 180.0

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def call_for(call_id: str, op: str) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Any]:
    def register(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


def wt(wiretap_id: str) -> dict[str, Any]:
    return {"kind": "wiretap", "id": wiretap_id}


@call_for("r1_render_clip_a", "render.clip")
def _r1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["wtA"])}


@call_for("r2_render_clip_a_again", "render.clip")
def _r2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["wtA"])}


@call_for("r3_render_clip_a_forced", "render.clip")
def _r3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["wtA"]), "force": True}


@call_for("r4_render_clip_b_without_fx", "render.clip")
def _r4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["wtB"])}


@call_for("r5_render_the_library_copy", "render.clip")
def _r5(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["wtLib"])}


@call_for("r6_render_the_batch_reel_clip", "render.clip")
def _r6(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": ids["batchSel"]}


@call_for("t1_render_clip_t_with_timeout_1", "render.clip")
def _t1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["wtT"]), "timeout_s": 1}


@call_for("t2_render_again_while_unresolved", "render.clip")
def _t2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["wtA"]), "force": True}


@call_for("t3_journal_check", "journal.check")
def _t3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["group"]}


# -- transport scaffolding (never through the op under test) ------------------------------------

PRELUDE: Final[str] = "\n".join(
    [
        "def _g(fn):",
        "    try:",
        "        return fn()",
        "    except BaseException as _e:",
        "        return 'ERR:' + type(_e).__name__ + ': ' + str(_e)",
        "def _seg_of(_clip):",
        "    return list(list(list(_clip.versions)[0].tracks)[0].segments)[0]",
        "def _fx_types(_clip):",
        "    _types = []",
        "    for _ver in list(_clip.versions):",
        "        for _trk in list(_ver.tracks):",
        "            for _s in list(_trk.segments):",
        "                for _f in list(_s.effects):",
        "                    _types.append(_v(_f.type))",
        "    return _types",
        "def _row(_clip):",
        "    return {",
        "        'type': type(_clip).__name__,",
        "        'name': _v(_clip.name),",
        "        'uid': _v(_clip.uid),",
        "        'wt': _g(lambda: _clip.get_wiretap_node_id()),",
        "        'fx': _fx_types(_clip),",
        "        'rendered': _g(lambda: _clip.is_rendered()),",
        "        'bd': _g(lambda: _clip.bit_depth),",
        "    }",
    ]
)

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

DESKTOP_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_rg = _d.create_reel_group(@RG@)",
        "_reel = _rg.create_reel('SRC')",
        "_rows = {}",
        "for _key, _path, _count in @CLIPS@:",
        "    _clip = flame.import_clips(_path, _reel)[0]",
        "    _errors = []",
        "    for _i in range(_count):",
        "        try:",
        "            _seg_of(_clip).create_effect('Blur')",
        "        except BaseException as _e:",
        "            _errors.append(type(_e).__name__ + ': ' + str(_e))",
        "    _one = _row(_clip)",
        "    _one['effectErrors'] = _errors",
        "    _rows[_key] = _one",
        "_result = {",
        "    'rows': _rows,",
        "    'reelWt': _reel.get_wiretap_node_id(),",
        "    'clipCount': len(list(_reel.clips)),",
        "}",
    ]
)

LIBRARY_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_lib = _ws.create_library(@LIB@)",
        "_lreel = _lib.create_reel('LR')",
        "_src = flame.find_by_wiretap_node_id(@WT@)",
        "_ret = flame.media_panel.copy(_src, _lreel)",
        "_copies = [_row(_c) for _c in list(_lreel.clips)]",
        "_result = {'copies': _copies, 'returned': repr(_ret), 'libraryName': _v(_lib.name)}",
    ]
)

BATCH_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = _d.create_batch_group(@NAME@)",
        "_rn = _v(_bg.reels[0].name)",
        "_bg.import_clip(@PATH@, _rn)",
        "_reel = [_r for _r in list(_bg.reels) if _v(_r.name) == _rn][0]",
        "_clip = list(_reel.clips)[0]",
        "_errors = []",
        "try:",
        "    _seg_of(_clip).create_effect('Blur')",
        "except BaseException as _e:",
        "    _errors.append(type(_e).__name__ + ': ' + str(_e))",
        "_result = {'uid': _v(_bg.uid), 'name': _v(_bg.name), 'reel': _rn,",
        "           'effectErrors': _errors}",
    ]
)

READ_CLIP_CODE: Final[str] = "\n".join(
    [
        "_rwt = @RWT@",
        "_c = flame.find_by_wiretap_node_id(@WT@)",
        "_r = flame.find_by_wiretap_node_id(_rwt) if _rwt is not None else None",
        "_result = None",
        "if _c is not None:",
        "    _result = {'clip': _row(_c),",
        "               'reelClips': None if _r is None else len(list(_r.clips))}",
    ]
)

READ_BATCH_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_result = None",
        "if _hit:",
        "    _bg = _hit[0]",
        "    _clips = []",
        "    for _r in list(_bg.reels):",
        "        for _c in list(_r.clips):",
        "            _one = _row(_c)",
        "            _one['reel'] = _v(_r.name)",
        "            _clips.append(_one)",
        "    _nodes = []",
        "    for _n in list(_bg.nodes):",
        "        _res = _g(lambda: _v(_n.resolution))",
        "        _nodes.append({",
        "            'name': _v(_n.name),",
        "            'pyType': type(_n).__name__,",
        "            'type': _g(lambda: _v(_n.type)),",
        "            'bd': _g(lambda: _res.bit_depth),",
        "        })",
        "    _result = {'clips': _clips, 'nodes': _nodes}",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "desktop": DESKTOP_CODE,
    "library": LIBRARY_CODE,
    "batch": BATCH_CODE,
    "read_clip": READ_CLIP_CODE,
    "read_batch": READ_BATCH_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}_{tail}"


async def tx_val(ctx: Ctx, code: str, label: str, timeout_s: float = 60.0) -> Any:
    br = await tx(ctx, PRELUDE + "\n" + code, label=label, timeout_s=timeout_s)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_clip(ctx: Ctx, wiretap_id: str, reel_wt: str | None = None) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(READ_CLIP_CODE, WT=wiretap_id, RWT=reel_wt), "p5rc_read_clip")
    return None if is_error(got) or got is None else got


async def tx_batch(ctx: Ctx) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(READ_BATCH_CODE, UID=ctx.ids["batchUid"]), "p5rc_read_batch")
    return None if is_error(got) or got is None else got


def clip_of(read: dict[str, Any] | None) -> dict[str, Any]:
    return (read or {}).get("clip") or {}


def batch_clip(read: dict[str, Any] | None) -> dict[str, Any]:
    clips = (read or {}).get("clips") or []
    return clips[0] if len(clips) == 1 else {}


def node_depths(read: dict[str, Any] | None, clip_name: Any) -> list[Any]:
    """The bit depth of every Clip node of the group that carries the clip's name."""
    return [n.get("bd") for n in (read or {}).get("nodes") or [] if n.get("name") == clip_name]


def group_count(ctx: Ctx) -> int:
    return len(journal_states(ctx))


async def run_call(session: Any, ctx: Ctx, call_id: str) -> dict[str, Any]:
    op, build = CALLS[call_id]
    return await do(session, ctx, op, build(ctx.ids))


def intent_handle(ctx: Ctx, group: Any) -> Any:
    for record in journal_records(ctx):
        if record.get("group") == group and record.get("type") == "intent":
            return record.get("handle")
    return None


# -- prepare ------------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    ids = ctx.ids
    project = await tx_val(ctx, PROJECT_CODE, "p5rc_project")
    rec(
        ctx,
        "prepare_the_project_is_the_probe_project",
        project == TARGET_PROJECT,
        project=project,
    )
    if project != TARGET_PROJECT:
        return
    missing = [str(p) for p in (FILE_48, FILE_72) if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    clips = [
        ["A", str(FILE_48), 1],
        ["E", str(FILE_48), 1],
        ["B", str(FILE_72), 0],
        ["T", str(FILE_72), 1],
    ]
    desktop = await tx_val(
        ctx,
        fill(DESKTOP_CODE, RG=name_of(ctx, "RG"), CLIPS=clips),
        "p5rc_desktop",
        MAKE_TIMEOUT_S,
    )
    made = isinstance(desktop, dict) and set(desktop.get("rows", {})) == {"A", "E", "B", "T"}
    rec(ctx, "prepare_the_four_desktop_clips_are_imported", made, desktop=desktop)
    if not made:
        return
    rows = desktop["rows"]
    ids.update(
        wtA=rows["A"]["wt"],
        wtB=rows["B"]["wt"],
        wtE=rows["E"]["wt"],
        wtT=rows["T"]["wt"],
        reelWt=desktop["reelWt"],
    )
    ok, parts = judged(
        a_blur_unrendered=rows["A"]["fx"] == ["Blur"] and rows["A"]["rendered"] is False,
        e_blur_unrendered=rows["E"]["fx"] == ["Blur"] and rows["E"]["rendered"] is False,
        b_plain_reads_rendered=rows["B"]["fx"] == [] and rows["B"]["rendered"] is True,
        t_blur_unrendered=rows["T"]["fx"] == ["Blur"] and rows["T"]["rendered"] is False,
        wiretap_ids=all(isinstance(ids[k], str) for k in ("wtA", "wtB", "wtE", "wtT")),
    )
    rec(ctx, "prepare_the_clips_have_the_effects_the_stages_need", ok, clauses=parts, rows=rows)
    library = await tx_val(
        ctx,
        fill(LIBRARY_CODE, LIB=name_of(ctx, "LIB"), WT=ids["wtE"]),
        "p5rc_library",
        MAKE_TIMEOUT_S,
    )
    copies = library.get("copies") if isinstance(library, dict) else None
    rec(
        ctx,
        "prepare_the_library_holds_one_copy_of_clip_e",
        isinstance(copies, list) and len(copies) == 1 and isinstance(copies[0].get("wt"), str),
        library=library,
    )
    if isinstance(copies, list) and len(copies) == 1:
        ids["wtLib"] = copies[0]["wt"]
        ids["libBefore"] = copies[0]
    batch = await tx_val(
        ctx,
        fill(BATCH_CODE, NAME=name_of(ctx, "B"), PATH=str(FILE_48)),
        "p5rc_batch",
        MAKE_TIMEOUT_S,
    )
    rec(
        ctx,
        "prepare_the_batch_group_is_made_and_its_clip_has_a_blur",
        isinstance(batch, dict) and batch.get("effectErrors") == [],
        batch=batch,
    )
    if not isinstance(batch, dict):
        return
    ids["batchUid"] = batch["uid"]
    ids["batchName"] = batch["name"]
    ids["batchReel"] = batch["reel"]
    read = await tx_batch(ctx)
    clip = batch_clip(read)
    ok, parts = judged(
        one_clip_in_the_reels=bool(clip),
        blur=clip.get("fx") == ["Blur"],
        unrendered=clip.get("rendered") is False,
        has_a_clip_node=bool(node_depths(read, clip.get("name"))),
    )
    rec(ctx, "prepare_the_batch_clip_has_an_unrendered_blur", ok, clauses=parts, read=read)
    if not clip:
        return
    ids["batchBefore"] = read
    wiretap = clip.get("wt")
    if isinstance(wiretap, str):
        ids["batchSel"] = wt(wiretap)
    else:
        ids["batchSel"] = {
            "kind": "path",
            "root": "desktop",
            "path": [ids["batchName"], ids["batchReel"], clip["name"]],
        }
    observe(
        ctx,
        "prepare_how_the_batch_reel_clip_is_addressed",
        selector=ids["batchSel"]["kind"],
        wiretapId=wiretap,
    )


# -- stage r ------------------------------------------------------------------------------------


def clean_poll(ctx: Ctx, group: Any) -> tuple[Any, bool]:
    """Returns (the handle the intent carries, whether the result file the bridge kept under it is
    gone)."""
    handle = intent_handle(ctx, group)
    kept = Path(ctx.run_dir) / "results" / f"{handle}.json"
    return handle, isinstance(handle, str) and not kept.exists()


async def r_first(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    before = await tx_clip(ctx, ids["wtA"], ids["reelWt"])
    env = await run_call(session, ctx, "r1_render_clip_a")
    result = _result_of(env)
    after = await tx_clip(ctx, ids["wtA"], ids["reelWt"])
    group = (env.get("journal") or {}).get("group")
    handle, removed = clean_poll(ctx, group)
    ok, parts = judged(
        op_ok=env.get("ok") is True,
        ran=result.get("ran") is True,
        before_false=result.get("isRenderedBefore") is False,
        after_true=result.get("isRenderedAfter") is True,
        effects=result.get("effects") == ["Blur"],
        transport_before=clip_of(before).get("rendered") is False,
        transport_after=clip_of(after).get("rendered") is True,
        uid_same=clip_of(after).get("uid") == clip_of(before).get("uid") is not None,
        wiretap_same=clip_of(after).get("wt") == ids["wtA"],
        reel_clips_same=(after or {}).get("reelClips") == (before or {}).get("reelClips"),
    )
    rec(
        ctx,
        "r1_a_defaults_render_the_blur_of_a_desktop_clip",
        ok,
        clauses=parts,
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        result=result,
        before=before,
        after=after,
    )
    rec(
        ctx,
        "r1_b_the_result_file_the_bridge_kept_under_the_handle_was_removed",
        removed,
        handle=handle,
        degraded=env.get("degraded"),
    )
    observe(
        ctx,
        "r1_what_the_render_answered",
        returned=result.get("returned"),
        elapsedS=result.get("elapsedS"),
        degraded=env.get("degraded"),
        durationMs=env.get("durationMs"),
    )


async def r_again(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    groups = group_count(ctx)
    env = await run_call(session, ctx, "r2_render_clip_a_again")
    result = _result_of(env)
    after = await tx_clip(ctx, ids["wtA"])
    ok, parts = judged(
        refused=env.get("errorCode") == "PRECONDITION_UNMET",
        already_rendered=result.get("isRendered") is True and result.get("ran") is False,
        names_force=("force=true" in str(env.get("hint"))),
        transport_still_rendered=clip_of(after).get("rendered") is True,
        nothing_journaled=group_count(ctx) == groups,
    )
    rec(
        ctx,
        "r2_a_clip_that_is_already_rendered_is_refused_and_the_hint_names_force",
        ok,
        clauses=parts,
        error=env.get("error"),
        hint=env.get("hint"),
        result=result,
    )
    forced = await run_call(session, ctx, "r3_render_clip_a_forced")
    forced_result = _result_of(forced)
    ok, parts = judged(
        op_ok=forced.get("ok") is True,
        before_true=forced_result.get("isRenderedBefore") is True,
        after_true=forced_result.get("isRenderedAfter") is True,
    )
    rec(
        ctx,
        "r3_force_true_renders_a_clip_that_is_already_rendered",
        ok,
        clauses=parts,
        error=forced.get("error"),
        errorCode=forced.get("errorCode"),
        result=forced_result,
    )


async def r_refusals(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    groups = group_count(ctx)
    plain_before = await tx_clip(ctx, ids["wtB"])
    env = await run_call(session, ctx, "r4_render_clip_b_without_fx")
    plain_after = await tx_clip(ctx, ids["wtB"])
    message = str(env.get("error"))
    ok, parts = judged(
        refused=env.get("errorCode") == "PRECONDITION_UNMET",
        says_no_fx="no Timeline FX" in message,
        not_called_rendered="already rendered" not in message,
        nothing_journaled=group_count(ctx) == groups,
        reads_as_before=clip_of(plain_after) == clip_of(plain_before) != {},
    )
    rec(
        ctx,
        "r4_a_clip_without_timeline_fx_is_refused_and_not_called_rendered",
        ok,
        clauses=parts,
        error=env.get("error"),
        before=plain_before,
        after=plain_after,
    )
    lib_before = await tx_clip(ctx, ids["wtLib"])
    lib_env = await run_call(session, ctx, "r5_render_the_library_copy")
    lib_after = await tx_clip(ctx, ids["wtLib"])
    ok, parts = judged(
        refused=lib_env.get("errorCode") == "PRECONDITION_UNMET",
        names_library="library" in str(lib_env.get("error")),
        nothing_journaled=group_count(ctx) == groups,
        reads_as_before=clip_of(lib_after) == clip_of(lib_before) != {},
    )
    rec(
        ctx,
        "r5_a_clip_in_a_library_is_refused_and_left_as_it_was",
        ok,
        clauses=parts,
        error=lib_env.get("error"),
        before=lib_before,
        after=lib_after,
    )


async def r_batch(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    before = ids.get("batchBefore")
    clip_before = batch_clip(before)
    observe(
        ctx,
        "r6_the_clip_node_before_the_render",
        clipBitDepth=clip_before.get("bd"),
        nodeBitDepths=node_depths(before, clip_before.get("name")),
        nodes=(before or {}).get("nodes"),
    )
    env = await run_call(session, ctx, "r6_render_the_batch_reel_clip")
    result = _result_of(env)
    after = await tx_batch(ctx)
    clip_after = batch_clip(after)
    ok, parts = judged(
        op_ok=env.get("ok") is True,
        before_false=result.get("isRenderedBefore") is False,
        after_true=result.get("isRenderedAfter") is True,
        transport_rendered=clip_after.get("rendered") is True,
        uid_same=clip_after.get("uid") == clip_before.get("uid") is not None,
        reel_clips_same=len((after or {}).get("clips") or []) == 1,
        node_count_same=len((after or {}).get("nodes") or [])
        == len((before or {}).get("nodes") or []),
        node_at_the_clip_bit_depth=node_depths(after, clip_after.get("name"))
        == [clip_before.get("bd")],
    )
    rec(
        ctx,
        "r6_a_batch_reel_clip_is_rendered_and_its_clip_node_follows_the_clips_bit_depth",
        ok,
        clauses=parts,
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        result=result,
        after=after,
    )


async def stage_r(ctx: Ctx) -> None:
    async with server_session(ctx, "p5rc-r") as session:
        await r_first(session, ctx)
        await r_again(session, ctx)
        await r_refusals(session, ctx)
        if "batchSel" in ctx.ids:
            await r_batch(session, ctx)
        else:
            rec(ctx, "r6_skipped_there_is_no_batch_clip", False)


# -- stage t ------------------------------------------------------------------------------------


async def stage_t(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p5rc-t") as session:
        started = time.monotonic()
        env = await run_call(session, ctx, "t1_render_clip_t_with_timeout_1")
        wall = round(time.monotonic() - started, 2)
        result = _result_of(env)
        timed_out = env.get("errorCode") in ("TIMEOUT", "BRIDGE_LOST")
        observe(
            ctx,
            "t1_render_with_timeout_1_second",
            ok=env.get("ok"),
            errorCode=env.get("errorCode"),
            error=env.get("error"),
            pending=env.get("pending"),
            endedWith=(
                "pending" if env.get("pending") else env.get("errorCode") or "finished in time"
            ),
            wallS=wall,
            elapsedS=result.get("elapsedS"),
            timedOut=timed_out,
        )
        group = (env.get("journal") or {}).get("group")
        handle, removed = clean_poll(ctx, group)
        rec(
            ctx,
            "t1_the_intent_carries_the_handle_the_server_chose",
            isinstance(handle, str) and len(handle) == 32,
            handle=handle,
        )
        if not timed_out:
            observe(
                ctx,
                "t1_finished_in_time_the_timeout_path_was_not_exercised",
                ok=env.get("ok"),
                isRenderedAfter=result.get("isRenderedAfter"),
                resultFileRemoved=removed,
            )
            return
        ids["group"] = group
        version = _result_of(await version_info(session, ctx))
        journal = version.get("journal") or {}
        index = journal_index(ctx)
        ok, parts = judged(
            writes_blocked=journal.get("writesBlocked") is True,
            an_open_intent=journal.get("unresolvedIntents", 0) >= 1,
            no_outcome=group in index.groups and index.groups[group][0].outcome is None,
        )
        rec(
            ctx,
            "t2_a_timeout_left_the_intent_open_and_the_server_refuses_writes",
            ok,
            clauses=parts,
        )
        again = await run_call(session, ctx, "t2_render_again_while_unresolved")
        rec(
            ctx,
            "t3_a_second_render_is_refused_while_the_first_is_unresolved",
            again.get("errorCode") == "POLICY_DENIED",
            errorCode=again.get("errorCode"),
        )
        attempts: list[dict[str, Any]] = []
        deadline = time.monotonic() + CHECK_RETRY_TOTAL_S
        while True:
            checked = await run_call(session, ctx, "t3_journal_check")
            entries = _result_of(checked).get("checked") or [{}]
            entry = entries[0]
            attempts.append(
                {
                    "at": time.strftime("%H:%M:%S"),
                    "ok": checked.get("ok"),
                    "verdict": entry.get("verdict"),
                    "basis": entry.get("basis"),
                    "detail": entry.get("detail"),
                }
            )
            if entry.get("verdict") not in (None, "unknown") or time.monotonic() >= deadline:
                break
            await anyio.sleep(CHECK_RETRY_EVERY_S)
        observe(ctx, "t4_check_attempts", count=len(attempts), attempts=attempts)
        read = await tx_clip(ctx, ids["wtT"])
        records = [r for r in journal_records(ctx) if r.get("group") == group]
        polled = next((r for r in records if r.get("type") == "polled"), {})
        ok, parts = judged(
            check_ok=checked.get("ok") is True,
            landed=entry.get("verdict") == "landed",
            by_the_handle=entry.get("basis") == "handle",
            polled_names_the_handle=polled.get("handle") == handle,
            polled_after_true=(polled.get("flame") or {}).get("isRenderedAfter") is True,
            transport_rendered=clip_of(read).get("rendered") is True,
            writes_resume=_result_of(checked).get("writesBlocked") is False,
            state_follows=journal_index(ctx).group_state(group) == entry.get("verdict"),
        )
        rec(
            ctx,
            "t5_journal_check_reads_landed_from_the_bridges_answer_and_lifts_the_refusal",
            ok,
            clauses=parts,
            entry=entry,
            polledFlame=polled.get("flame"),
            transport=read,
        )


# -- z / driver ---------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    rec(
        ctx,
        "z_render_clip_was_called",
        "render.clip" in used,
        used=used,
    )
    own = journal_states(ctx)
    rec(
        ctx,
        "z_every_group_of_this_run_is_decided_none_is_unknown",
        bool(own) and set(own.values()) <= GOOD_STATES,
        states=sorted(set(own.values())),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{NAME_PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p5rc_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p5rc_left")
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
    os.environ[CKPT_ENV] = name_of(ctx, "CKPT")
    ctx.results["env"] = {CKPT_ENV: os.environ[CKPT_ENV]}
    try:
        await guarded(ctx, "prepare", prepare)
        if {"wtA", "wtB", "wtT", "wtLib", "reelWt"} <= set(ctx.ids):
            await guarded(ctx, "r", stage_r)
            await guarded(ctx, "t", stage_t)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p5rc_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------


def selftest_helpers() -> list[str]:
    """The comparison helpers on made-up reads (client side only). Returns the problems."""
    read = {
        "clips": [{"name": "p2_len48", "bd": 10, "rendered": True, "uid": "u1"}],
        "nodes": [{"name": "p2_len48", "bd": 10}, {"name": "other", "bd": 16}],
    }
    problems = []
    if batch_clip(read).get("uid") != "u1":
        problems.append("batch_clip of one clip: expected that clip (uid u1)")
    if batch_clip({"clips": [{}, {}]}) != {}:
        problems.append("batch_clip of two clips: expected {}")
    if node_depths(read, "p2_len48") != [10]:
        problems.append("node_depths: expected [10], the node of the clip only")
    if clip_of(None) != {} or clip_of({"clip": {"uid": "x"}}) != {"uid": "x"}:
        problems.append("clip_of: expected {} for None and the inner clip for {clip: ...}")
    return problems


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    ids: dict[str, Any] = {
        "wtA": "wt-a",
        "wtB": "wt-b",
        "wtLib": "wt-lib",
        "wtT": "wt-t",
        "batchSel": {"kind": "path", "root": "desktop", "path": ["B", "Reel", "clip"]},
        "group": "g_dry",
    }
    print("===== calls through flame_do =====")
    for call_id, (op_name, build) in CALLS.items():
        op = registry.get(op_name)
        problems: list[str] = []
        if op is None:
            print(f"NG {call_id}: unknown op {op_name}")
            failures += 1
            continue
        validation = validate_op_args(op, build(dict(ids)))
        if validation.ok:
            checked = dict(validation.value or {})
            apply_param_defaults(op, checked)
            apply_limit_default(op, checked)
            if op.precheck is not None:
                refusal = op.precheck(checked, None)
                if refusal is not None:
                    problems.append(f"precheck refused: {refusal.message}")
            if op.thread != "server":
                phases = [checked]
                if op_name == "render.clip":
                    phases.append({**checked, PHASE_ARG: PHASE_READ})
                for phase_args in phases:
                    code = execution.build_exec_code(op, phase_args)
                    if not code.isascii():
                        problems.append("the code for Flame is not ASCII")
                    try:
                        compile(code, f"<{call_id}>", "exec")
                    except SyntaxError as exc:
                        problems.append(f"compile FAILED: {exc}")
        else:
            problems.append(
                "invalid args: " + "; ".join(f"{i.path}: {i.message}" for i in validation.issues)
            )
        print(f"{'NG ' + '; '.join(problems) if problems else 'OK':3} {call_id}: {op_name}")
        failures += bool(problems)
    print("===== transport code (compiled with every @NAME@ filled) =====")
    for name, template in TEMPLATES.items():
        code = PRELUDE + "\n" + re.sub(r"@[A-Z_0-9]+@", "'x'", template)
        try:
            compile(code, f"<{name}>", "exec")
            note = "" if code.isascii() else " (NOT ASCII)"
            failures += bool(note)
            print(f"{'NG ' if note else 'OK '} {name} ({len(code)} chars){note}")
        except SyntaxError as exc:
            print(f"NG  {name}: {exc}")
            failures += 1
    print("===== comparison helpers on made-up reads =====")
    problems = selftest_helpers()
    print("OK  helpers" if not problems else f"NG  helpers: {problems}")
    failures += len(problems)
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
    journal_dir = OUTPUT_DIR / f"journal_p5rc_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p5_render_clip_e2e_{ts}.json",
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
