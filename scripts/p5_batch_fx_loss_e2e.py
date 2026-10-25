#!/usr/bin/env python3
"""Real-Flame end-to-end: a restore of a Batch checkpoint says which clips of the group's reels
lost their Timeline FX (run with the `.venv` Python; `scripts/run_e2e.sh p5_batch_fx_loss` starts
Flame; `--dry-run` connects to nothing, lists the calls, compiles the code and checks the
comparison on made-up reads).

Checks: A restore of a Batch checkpoint names, in `result.timelineFxLost`, the reel clips whose
Timeline FX were fewer after the restore, and leaves the clips that kept theirs out of it.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (for every clip of every schematic and shelf reel of the
group: the types of the effects of its segments; the group's nodes and whether it is current).
Everything this run makes is named `PP5E_<ts>_...` (scripts/p2_4_clean_probe_project.py knows the
prefix) and is deleted at the end by that prefix; the stash library name
(`PP5E_<ts>_CKPT`, passed to every server as FLAME_MCP_CHECKPOINT_LIBRARY) carries the prefix too,
so a stash library, if one is made, is deleted with the rest; then flame.exit(). The journal
directory is this run's own (`p3/journal_p5fx_e2e_<ts>/`). No test hook is set for any server. The
first step refuses to run anywhere but in the project `flame_mcp_probe`.

Material (transport): the Batch group `PP5E_<ts>_B` (made after the Desktop reel group, so the
current one); `clip1.mov` and `clip2.mov` imported into its first schematic reel by
`PyBatch.import_clip`; a Desktop reel group `PP5E_<ts>_RG` with p2_len48.mov imported into a reel
and a Timewarp (the default) made on its segment; that Desktop clip copied
(`flame.media_panel.copy`) into a second schematic reel of the group; then a Blur made on the
segment of clip1 inside the group. clip2 has no FX.

Stage f
  1. Every clip of every reel of the group is read through the transport (before). JUDGED: clip1
     has [Blur], the copied clip has [Timewarp], clip2 has none, and the clip names are unique.
  2. `node.create` Blur in the group (a checkpointed write). JUDGED: ok, the journal holds a
     checkpoint.
  3. `journal.revert` of the write. JUDGED: ok and result.matched is true.
  4. The same read (after).
  JUDGED (a): the clips whose effect types are fewer in the transport's after than in its before
     are exactly the (reel, clip) pairs of result.timelineFxLost, and each row's before / after
     are the transport's lists; result.timelineFxUnchecked is [].
  JUDGED (b): clip1 is in timelineFxLost with before [Blur] and after [], and the transport reads
     no FX on it.
  JUDGED (c): clip2 is in neither timelineFxLost nor timelineFxUnchecked.
  JUDGED (d): result.warnings has a line starting with "Timeline FX lost" when timelineFxLost is
     not empty, and none when it is empty.
  JUDGED (e), the control for the side that keeps its FX: the Timewarp clip that was only copied
     in has [Timewarp] in the transport's before AND after, and is in neither timelineFxLost nor
     timelineFxUnchecked. The Desktop source of the copy is not changed in this run; if this step
     fails, suspect a change in what Flame does, not the op.
  OBSERVED (nothing is decided): the copied clip's FX before and after (the same read as (e),
     recorded whatever its result), and the group's nodes around the write and the revert.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
from collections import Counter
from collections.abc import Mapping, Sequence
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
    rec,
    save,
    save_stage,
    server_session,
)
from p2_4_common import (  # noqa: E402
    flame_exit,
    journal_states,
    observe,
    ops_used,
    print_observations,
)
from p3_1a_read_e2e import PRELUDE, fill, is_error, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import CKPT_ENV  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
NAME_PREFIX: Final[str] = "PP5E"
BATCH_CLIPS: Final[tuple[str, ...]] = ("clip1", "clip2")
"""The clips imported into the group's first schematic reel (files `<name>.mov` in MEDIA_DIR)."""
DESKTOP_FILE: Final[Path] = MEDIA_DIR / "p2" / "p2_len48.mov"
COPY_REEL: Final[str] = "PP5E Copy Reel"
BLUR_CLIP: Final[str] = "clip1"
PLAIN_CLIP: Final[str] = "clip2"
LOST_NOTE_START: Final[str] = "Timeline FX lost"
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed"}
)
MAKE_TIMEOUT_S: Final[float] = 180.0

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Any]] = {}


def call_for(call_id: str, op: str) -> Any:
    def register(builder: Any) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


def batch_sel(uid: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid}


@call_for("f1_write_a_node", "node.create")
def _f1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": batch_sel(ids["uid"]), "type": "Blur"}


@call_for("f2_revert_the_write", "journal.revert")
def _f2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["group"]}


# -- transport scaffolding (never through the op under test) ------------------------------------

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

DESKTOP_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_rg = _d.create_reel_group(@RG@)",
        "_reel = _rg.create_reel('SRC')",
        "_clip = flame.import_clips(@PATH@, _reel)[0]",
        "_seg = list(list(list(_clip.versions)[0].tracks)[0].segments)[0]",
        "_ret = _seg.create_effect(@FX@)",
        "_result = {",
        "    'wt': _clip.get_wiretap_node_id(), 'name': _v(_clip.name), 'returned': repr(_ret),",
        "    'fx': [_v(_f.type) for _f in list(_seg.effects)],",
        "}",
    ]
)

BATCH_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = _d.create_batch_group(@NAME@)",
        "_rn = _v(_bg.reels[0].name)",
        "for _p in @PATHS@:",
        "    _bg.import_clip(_p, _rn)",
        "_result = {'uid': _v(_bg.uid), 'reel': _rn}",
    ]
)

LOCATE_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@][0]",
    ]
)

COPY_CODE: Final[str] = "\n".join(
    [
        LOCATE_CODE,
        "_reel = _bg.create_reel(@REEL@)",
        "_src = flame.find_by_wiretap_node_id(@WT@)",
        "_ret = flame.media_panel.copy(_src, _reel)",
        "_result = {'reel': _v(_reel.name), 'returned': repr(_ret),",
        "           'clips': [_v(_c.name) for _c in list(_reel.clips)]}",
    ]
)

BLUR_CODE: Final[str] = "\n".join(
    [
        LOCATE_CODE,
        "_hits = []",
        "for _r in list(_bg.reels):",
        "    for _c in list(_r.clips):",
        "        if _v(_c.name) == @CLIP@:",
        "            _hits.append(_c)",
        "_result = {'hits': len(_hits)}",
        "if len(_hits) == 1:",
        "    _seg = list(list(list(_hits[0].versions)[0].tracks)[0].segments)[0]",
        "    _ret = _seg.create_effect(@FX@)",
        "    _result['returned'] = repr(_ret)",
        "    _result['fx'] = [_v(_f.type) for _f in list(_seg.effects)]",
    ]
)

READ_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_cur = _v(_d.current_batch_group)",
        "_result = None",
        "if _hit:",
        "    _bg = _hit[0]",
        "    _rows = []",
        "    for _kind, _attr in (('schematic', 'reels'), ('shelf', 'shelf_reels')):",
        "        for _r in list(getattr(_bg, _attr)):",
        "            for _c in list(_r.clips):",
        "                _types = []",
        "                for _ver in list(_c.versions):",
        "                    for _trk in list(_ver.tracks):",
        "                        for _seg in list(_trk.segments):",
        "                            for _f in list(_seg.effects):",
        "                                _types.append(_v(_f.type))",
        "                _rows.append([_kind, _v(_r.name), _v(_c.name), _types])",
        "    _result = {",
        "        'name': _v(_bg.name), 'rows': _rows,",
        "        'isCurrent': _cur is not None and _v(_cur.uid) == _v(_bg.uid),",
        "        'nodes': sorted(_v(_n.name) for _n in _bg.nodes),",
        "    }",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "desktop": DESKTOP_CODE,
    "batch": BATCH_CODE,
    "copy": COPY_CODE,
    "blur": BLUR_CODE,
    "read": READ_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}

FxKey = tuple[str, str, str]
"""(reel kind, reel name, clip name) of a clip row."""


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}_{tail}"


async def tx_group(ctx: Ctx, uid: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(READ_CODE, UID=uid), "p5fx_read")
    return None if is_error(got) or got is None else got


def fx_map(group: Mapping[str, Any] | None) -> dict[FxKey, list[str]]:
    """{(kind, reel, clip): effect types} of a group read by `tx_group`."""
    rows = group["rows"] if group else []
    return {(kind, reel, clip): list(types) for kind, reel, clip, types in rows}


def names_are_unique(group: Mapping[str, Any] | None) -> bool:
    rows = group["rows"] if group else []
    keys = [(kind, reel, clip) for kind, reel, clip, _types in rows]
    return len(keys) == len(set(keys))


def clip_fx(group: Mapping[str, Any] | None, clip: str) -> list[str] | None:
    """The effect types of the clip of that name (None: not there, or the name is not unique)."""
    hits = [types for (_kind, _reel, name), types in fx_map(group).items() if name == clip]
    return hits[0] if len(hits) == 1 else None


def lost_keys(
    before: Mapping[FxKey, Sequence[str]], after: Mapping[FxKey, Sequence[str]]
) -> set[FxKey]:
    """The clips that are in both reads and have fewer effects (by type) after."""
    return {
        key
        for key, types in before.items()
        if key in after and Counter(types) - Counter(after[key])
    }


def reported_keys(rows: Any) -> set[FxKey]:
    return {(r["reelKind"], r["reel"], r["clip"]) for r in rows if isinstance(r, dict)}


def reported_rows_match(
    rows: Any, before: Mapping[FxKey, Sequence[str]], after: Mapping[FxKey, Sequence[str]]
) -> bool:
    """Each reported row carries the transport's before / after lists of its clip."""
    return isinstance(rows, list) and all(
        isinstance(r, dict)
        and r.get("before") == before.get((r.get("reelKind"), r.get("reel"), r.get("clip")))
        and r.get("after") == after.get((r.get("reelKind"), r.get("reel"), r.get("clip")))
        for r in rows
    )


def has_lost_note(result: Mapping[str, Any]) -> bool:
    warnings = result.get("warnings")
    return isinstance(warnings, list) and any(
        isinstance(w, str) and w.startswith(LOST_NOTE_START) for w in warnings
    )


async def run_call(session: Any, ctx: Ctx, call_id: str) -> dict[str, Any]:
    op, build = CALLS[call_id]
    return await do(session, ctx, op, build(ctx.ids))


# -- prepare ------------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    ids = ctx.ids
    project = await tx_val(ctx, PROJECT_CODE, "p5fx_project")
    rec(
        ctx,
        "prepare_the_project_is_the_probe_project",
        project == TARGET_PROJECT,
        project=project,
    )
    if project != TARGET_PROJECT:
        return
    batch_files = [MEDIA_DIR / f"{n}.mov" for n in BATCH_CLIPS]
    missing = [str(p) for p in (*batch_files, DESKTOP_FILE) if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    desktop = await tx_val(
        ctx,
        fill(
            DESKTOP_CODE,
            RG=name_of(ctx, "RG"),
            PATH=str(DESKTOP_FILE),
            FX="Timewarp",
        ),
        "p5fx_desktop",
        MAKE_TIMEOUT_S,
    )
    rec(
        ctx,
        "prepare_the_desktop_clip_is_imported_and_has_a_timewarp",
        isinstance(desktop, dict) and desktop["fx"] == ["Timewarp"],
        desktop=desktop,
    )
    if not isinstance(desktop, dict):
        return
    made = await tx_val(
        ctx,
        fill(BATCH_CODE, NAME=name_of(ctx, "B"), PATHS=[str(p) for p in batch_files]),
        "p5fx_batch",
        MAKE_TIMEOUT_S,
    )
    rec(ctx, "prepare_the_batch_group_is_made", isinstance(made, dict), made=made)
    if not isinstance(made, dict):
        return
    ids["uid"] = made["uid"]
    copied = await tx_val(
        ctx,
        fill(COPY_CODE, UID=ids["uid"], REEL=COPY_REEL, WT=desktop["wt"]),
        "p5fx_copy",
        MAKE_TIMEOUT_S,
    )
    rec(
        ctx,
        "prepare_the_desktop_clip_is_copied_into_a_second_schematic_reel",
        isinstance(copied, dict) and len(copied["clips"]) == 1,
        copied=copied,
    )
    if not (isinstance(copied, dict) and len(copied["clips"]) == 1):
        return
    ids["copiedClip"] = copied["clips"][0]
    blur = await tx_val(
        ctx, fill(BLUR_CODE, UID=ids["uid"], CLIP=BLUR_CLIP, FX="Blur"), "p5fx_blur", MAKE_TIMEOUT_S
    )
    rec(
        ctx,
        "prepare_a_blur_is_made_on_clip1_inside_the_group",
        isinstance(blur, dict) and blur["hits"] == 1 and blur.get("fx") == ["Blur"],
        blur=blur,
    )
    group = await tx_group(ctx, ids["uid"])
    rec(
        ctx,
        "prepare_the_group_is_the_current_one",
        bool(group) and group["isCurrent"] is True,
        group=group,
    )


# -- stage f ------------------------------------------------------------------------------------


def judge_before(ctx: Ctx, before: dict[str, Any] | None) -> bool:
    copied = ctx.ids["copiedClip"]
    ready = (
        before is not None
        and names_are_unique(before)
        and len({PLAIN_CLIP, BLUR_CLIP, copied}) == 3
        and clip_fx(before, BLUR_CLIP) == ["Blur"]
        and clip_fx(before, copied) == ["Timewarp"]
        and clip_fx(before, PLAIN_CLIP) == []
    )
    rec(
        ctx,
        "f1_before_clip1_has_a_blur_the_copied_clip_a_timewarp_and_clip2_nothing",
        ready,
        rows=before and before["rows"],
        copiedClip=copied,
    )
    return ready


def judge_reported(
    ctx: Ctx,
    result: dict[str, Any],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    was, now = fx_map(before), fx_map(after)
    lost = result.get("timelineFxLost")
    unchecked = result.get("timelineFxUnchecked")
    expected = lost_keys(was, now)
    rec(
        ctx,
        "f5_a_the_clips_that_lost_fx_in_the_transport_read_are_the_ones_timeline_fx_lost_names",
        isinstance(lost, list)
        and reported_keys(lost) == expected
        and len(lost) == len(expected)
        and reported_rows_match(lost, was, now)
        and unchecked == [],
        expected=sorted(expected),
        timelineFxLost=lost,
        timelineFxUnchecked=unchecked,
        before=was and {"|".join(k): v for k, v in was.items()},
        after=now and {"|".join(k): v for k, v in now.items()},
    )
    blur_row = next(
        (r for r in lost or [] if isinstance(r, dict) and r.get("clip") == BLUR_CLIP), None
    )
    rec(
        ctx,
        "f6_b_clip1_with_the_blur_made_inside_the_group_is_reported_lost_and_has_no_fx_now",
        blur_row is not None
        and blur_row.get("before") == ["Blur"]
        and blur_row.get("after") == []
        and clip_fx(after, BLUR_CLIP) == [],
        row=blur_row,
        transport=clip_fx(after, BLUR_CLIP),
    )
    rec(
        ctx,
        "f7_c_clip2_without_fx_is_in_neither_list",
        isinstance(lost, list)
        and isinstance(unchecked, list)
        and not any(r.get("clip") == PLAIN_CLIP for r in (*lost, *unchecked)),
        timelineFxLost=lost,
        timelineFxUnchecked=unchecked,
    )
    rec(
        ctx,
        "f8_d_warnings_have_a_timeline_fx_lost_line_when_the_list_is_not_empty_and_not_otherwise",
        isinstance(lost, list) and has_lost_note(result) == bool(lost),
        warnings=result.get("warnings"),
        lost=len(lost) if isinstance(lost, list) else lost,
    )


def judge_kept(
    ctx: Ctx,
    result: dict[str, Any],
    copied: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    lost = result.get("timelineFxLost")
    unchecked = result.get("timelineFxUnchecked")
    rec(
        ctx,
        "f10_e_kept_side_control_the_copied_timewarp_clip_keeps_its_fx_and_is_in_neither_list",
        clip_fx(before, copied) == ["Timewarp"]
        and clip_fx(after, copied) == ["Timewarp"]
        and isinstance(lost, list)
        and isinstance(unchecked, list)
        and not any(r.get("clip") == copied for r in (*lost, *unchecked)),
        copiedClip=copied,
        before=clip_fx(before, copied),
        after=clip_fx(after, copied),
        timelineFxLost=lost,
        timelineFxUnchecked=unchecked,
    )


async def stage_f(ctx: Ctx) -> None:
    ids = ctx.ids
    uid = ids["uid"]
    before = await tx_group(ctx, uid)
    if not judge_before(ctx, before):
        return
    async with server_session(ctx, "p5fx-f") as session:
        written = await run_call(session, ctx, "f1_write_a_node")
        group = (written.get("journal") or {}).get("group")
        rec(
            ctx,
            "f2_the_checkpointed_write_lands_and_the_journal_holds_a_checkpoint",
            written.get("ok") is True
            and isinstance(group, str)
            and bool((written.get("journal") or {}).get("checkpoint")),
            error=written.get("error"),
            journal=written.get("journal"),
        )
        if not isinstance(group, str):
            return
        ids["group"] = group
        after_write = await tx_group(ctx, uid)
        revert = await run_call(session, ctx, "f2_revert_the_write")
        result = _result_of(revert)
        rec(
            ctx,
            "f3_the_revert_is_ok_and_matched",
            revert.get("ok") is True and result.get("matched") is True,
            error=revert.get("error"),
            errorCode=revert.get("errorCode"),
            matched=result.get("matched"),
        )
        after = await tx_group(ctx, uid)
        rec(ctx, "f4_the_group_can_be_read_after_the_revert", after is not None)
        if after is None:
            return
        judge_reported(ctx, result, before, after)
        copied = ids["copiedClip"]
        judge_kept(ctx, result, copied, before, after)
        observe(
            ctx,
            "f9_the_copied_timewarp_clip_after_the_revert",
            copiedClip=copied,
            before=clip_fx(before, copied),
            after=clip_fx(after, copied),
            kept=clip_fx(after, copied) == clip_fx(before, copied),
        )
        observe(
            ctx,
            "f9_the_groups_nodes_around_the_write_and_the_revert",
            beforeWrite=before and before["nodes"],
            afterWrite=after_write and after_write["nodes"],
            afterRevert=after["nodes"],
        )


# -- z / driver ---------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {op for op, _build in CALLS.values()}
    rec(
        ctx,
        "z_every_op_of_this_run_was_called",
        wanted <= set(used),
        missing=sorted(wanted - set(used)),
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
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p5fx_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p5fx_left")
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
        if "copiedClip" in ctx.ids and "uid" in ctx.ids:
            await guarded(ctx, "f", stage_f)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p5fx_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------


def selftest_comparison() -> list[str]:
    """The comparison of two reads on made-up rows (client side only). Returns the problems."""
    rows_before = [
        ["schematic", "R1", "clip1", ["Blur"]],
        ["schematic", "R1", "clip2", []],
        ["schematic", "R2", "p2_len48", ["Timewarp"]],
        ["shelf", "S1", "clip1", ["Timewarp", "Blur"]],
    ]
    rows_after = [
        ["schematic", "R1", "clip1", []],
        ["schematic", "R1", "clip2", []],
        ["schematic", "R2", "p2_len48", ["Timewarp"]],
        ["shelf", "S1", "clip1", ["Timewarp"]],
    ]
    before = fx_map({"rows": rows_before})
    after = fx_map({"rows": rows_after})
    problems = []
    got = lost_keys(before, after)
    want = {("schematic", "R1", "clip1"), ("shelf", "S1", "clip1")}
    if got != want:
        problems.append(f"lost_keys {sorted(got)} != {sorted(want)}")
    reported = [
        {"reelKind": "schematic", "reel": "R1", "clip": "clip1", "before": ["Blur"], "after": []},
        {
            "reelKind": "shelf",
            "reel": "S1",
            "clip": "clip1",
            "before": ["Timewarp", "Blur"],
            "after": ["Timewarp"],
        },
    ]
    if reported_keys(reported) != want or not reported_rows_match(reported, before, after):
        problems.append(
            "reported_keys / reported_rows_match refused a report that matches the reads"
        )
    reported[0]["after"] = ["Blur"]
    if reported_rows_match(reported, before, after):
        problems.append(
            "reported_rows_match accepted a report with a wrong after list; expected a refusal"
        )
    if clip_fx({"rows": rows_before}, "clip1") is not None:
        problems.append("clip_fx of a clip name used twice: expected None")
    if clip_fx({"rows": rows_before}, "clip2") != []:
        problems.append("clip_fx of clip2: expected [] (a clip without Timeline FX)")
    note = f"{LOST_NOTE_START}: the Batch setup ..."
    if not has_lost_note({"warnings": ["x", note]}) or has_lost_note({"warnings": ["x"]}):
        problems.append(
            "has_lost_note: expected True with the note in warnings and False without it"
        )
    return problems


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    ids: dict[str, Any] = {"uid": "uid-b", "group": "g_dry"}
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
                try:
                    compile(execution.build_exec_code(op, checked), f"<{call_id}>", "exec")
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
            print(f"OK  {name} ({len(code)} chars)")
        except SyntaxError as exc:
            print(f"NG  {name}: {exc}")
            failures += 1
    print("===== comparison of two reads on made-up rows =====")
    problems = selftest_comparison()
    print("OK  comparison" if not problems else f"NG  comparison: {problems}")
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
    journal_dir = OUTPUT_DIR / f"journal_p5fx_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p5_batch_fx_loss_e2e_{ts}.json",
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
