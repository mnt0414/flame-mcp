#!/usr/bin/env python3
"""Real-Flame end-to-end: `segment.edit_effect_setup` and `node.edit_setup` replace one value of a
Timewarp's setup file, and `segment.read_effect_setup` and `node.read_setup` read tags without
writing (run with the `.venv` Python; `scripts/run_e2e.sh p5_setup_edit` starts Flame;
`--dry-run` connects to nothing, lists the calls, compiles the code and checks the tag reader on
made-up text).

Checks: `segment.edit_effect_setup` sets `TW_FrameMode` of a Timewarp on a Desktop reel clip from 2
to 0, and an independent read of the setup (by transport) says 0. A second call with the same value
is `PRECONDITION_UNMET`, and a value with `<` is `INVALID_ARGS`; neither leaves an intent in the
journal. `journal.revert` puts the clip back to 2. A `plan.run` of three edits on the three
segments of one sequence is one checkpoint, and one revert puts all three back. `node.edit_setup`
does the same on a Timewarp node of a Batch group, and its revert puts the node back. Given the
`file` argument, it edits the own file of a node that saves more than one file.
`segment.read_effect_setup` and `node.read_setup` read tags without writing, and leave no work
folder and no journal record.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (it saves the setup of the effect / node itself and reads the
tag from the file). Everything this run makes is named `PP5E_<ts>_...` (scripts/
p2_4_clean_probe_project.py knows the prefix) and is deleted at the end by that prefix; the stash
library (`PP5E_<ts>_CKPT`, passed to every server as FLAME_MCP_CHECKPOINT_LIBRARY) carries the
prefix too; then flame.exit(). The journal directory is this run's own
(`p3/journal_p5se_e2e_<ts>/`; the work files of the ops are under its `setups/`, and the ones
`keep_files` keeps stay there). The setup files the transport saves are in `p3/files_pp5e_<ts>/`.
No test hook is set for any server. The first step refuses to run anywhere but in the project
`flame_mcp_probe`.

Material (transport): a Desktop reel group `PP5E_<ts>_RG` with clip1 and p2_len48 imported; a
Timewarp (Flame's default) made on the segment of p2_len48; a sequence of three p2_len48 segments
on track 0 with a Timewarp on each; the Batch group `PP5E_<ts>_B` (made last, so the current one)
with a Timewarp node and a Render node.

Stage c (the clip)
  1. The transport reads the clip's setup. JUDGED: one file, and `TW_FrameMode` occurs once with
     the value 2.
  2. `segment.edit_effect_setup` TW_FrameMode 0. JUDGED: ok; result.tags says before 2, requested
     0, after 0; the transport's own read says 0; the work directory is empty again.
  3. The same call again. JUDGED: PRECONDITION_UNMET with "nothing to change"; no new intent in
     the journal and no new checkpoint.
  4. The same call with the value `0<1`, and with an unknown tag. JUDGED: INVALID_ARGS and nothing
     written (no new intent); PRECONDITION_UNMET with result.tagCounts 0 for the unknown tag.
  5. `journal.revert` of step 2. JUDGED: ok; the restored clip reads 2 and has the fingerprint it
     had before.
  6. The call with `keep_files`. JUDGED: ok; the kept original / edited / read-back files are
     mode 0600 in a 0700 directory under the journal directory, the original holds 2, the edited
     and read-back hold 0; then it is reverted. JUDGED: the revert is ok and the restored clip has
     a wiretap id, which the later steps use. OBSERVED: how often `Value` occurs in the file.
  1a. (after 1) `segment.read_effect_setup` with tags TW_FrameMode and PP5E_NoSuchTag. JUDGED: ok;
     values.TW_FrameMode is "2"; the unknown tag is null and is in result.problems; no new s_
     folder is left under the journal directory's setups/; no new intent in the journal.
  2a. (after 2) The same read. JUDGED: ok; values.TW_FrameMode is "0".
  4a. The read with the tag name `a<b`. JUDGED: INVALID_ARGS; no new intent.
Stage p (a plan)
  1. `plan.run` of three edits (TW_FrameMode 0) on the three segments. JUDGED: ok; one more
     checkpoint record; the transport reads 0 on all three.
  2. `journal.revert` of the plan. JUDGED: ok; the restored sequence reads 2 on all three.
Stage n (the node)
  1. The transport saves the node's setup. JUDGED: one file, `TW_FrameMode` once, value 2.
  2. `node.edit_setup` TW_FrameMode 0. JUDGED: ok; result.tags before 2, requested 0, after 0; the
     transport's own save of the node's setup reads 0; the node keeps its name.
  3. `journal.revert`. JUDGED: ok; the transport reads 2.
  1a. (after 1, before the edit) `node.read_setup` of the Timewarp node with tag TW_FrameMode.
     JUDGED: ok; the value is "2"; no s_ folder is left.
  1b. `node.read_setup` of the Render node with tags LogLinTargetPixelFormat and UseLuts. JUDGED:
     ok; the value of LogLinTargetPixelFormat is not null and result.fileOf names `.lut_node` for
     it. OBSERVED: the value of UseLuts and whether it is null, result.fileOf, result.tagCounts,
     the files the save wrote, every problem, and whether a work folder or an intent was left.
  4. The transport saves the Render node's setup (before). `node.edit_setup` on the Render node
     with file `output_node`, values {SourceTime: 100} (101 when it already reads 100). JUDGED: ok;
     result.file is `output_node`; the transport's own save of the Render node reads the new
     `<SourceTime>` in the `.output_node` file; result.otherFilesChanged is a list. OBSERVED: the
     files the save wrote, result.otherFilesChanged, result.otherDifferences, `degraded`.
  5. `journal.revert` of step 4. JUDGED: ok; the transport reads the value of before.
  6. The transport adds a Clip node to the Batch group when it has none: it first copies
     p2_len48 (which carries a Timewarp) into the batch reel `sch` with `flame.media_panel.copy`
     (OBSERVED: what it returned or raised, and the Clip nodes after it), and when that made no
     Clip node it copies clip1 (no Timeline FX). `node.edit_setup` on the first Clip node with
     file `clip_node`, values {HeadMode: 1} (2
     when it already reads 1). JUDGED: ok; the transport's own save reads the new `<HeadMode>` in
     the `.clip_node` file; result.otherFilesChanged is a list. OBSERVED: the value before (2 was
     measured in a fresh Clip node of p2_len48), the files the save wrote,
     result.otherFilesChanged.
  7. `journal.revert` of step 6. JUDGED: ok; the transport reads the value of before.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
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
    _br_dict,
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
    tx,
)
from p2_4_common import (  # noqa: E402
    flame_exit,
    journal_records,
    journal_states,
    observe,
    ops_used,
    print_observations,
)
from p3_1a_read_e2e import PRELUDE, fill, is_error, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    CKPT_ENV,
    OUTPUT_DIR,
    PREPARE_CODE,
    Results,
    checkpoint_records,
    ckpt_library,
    fingerprint,
    name_of,
    seg_at,
    sel,
    track_rows,
    tx_struct,
)
from p3_3_e2e_common import make_seq, restored_wt, revert  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

PREFIX: Final[str] = "PP5E"
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("clip1", MEDIA_DIR / "clip1.mov"),
    ("p2_len48", MEDIA_DIR / "p2" / "p2_len48.mov"),
)
"""clip1 is only there for the frame rate and size the sequence is made with."""
SOURCE_CLIP: Final[str] = "p2_len48"
PLAIN_CLIP: Final[str] = "clip1"
SEQUENCE_PUTS: Final[tuple[tuple[int, str, int], ...]] = (
    (0, SOURCE_CLIP, 1),
    (0, SOURCE_CLIP, 49),
    (0, SOURCE_CLIP, 97),
)
FX_TYPE: Final[str] = "Timewarp"
TAG: Final[str] = "TW_FrameMode"
START_VALUE: Final[str] = "2"
NEW_VALUE: Final[int] = 0
UNKNOWN_TAG: Final[str] = "TW_NoSuchTag"
BAD_VALUE: Final[str] = "0<1"
UNKNOWN_READ_TAG: Final[str] = "PP5E_NoSuchTag"
BAD_TAG_NAME: Final[str] = "a<b"
RENDER_TYPE: Final[str] = "Render"
RENDER_TAGS: Final[tuple[str, str]] = ("LogLinTargetPixelFormat", "UseLuts")
RENDER_JUDGED_TAG: Final[str] = "LogLinTargetPixelFormat"
RENDER_JUDGED_FILE: Final[str] = ".lut_node"
RENDER_FILE: Final[str] = "output_node"
RENDER_EDIT_TAG: Final[str] = "SourceTime"
RENDER_EDIT_VALUE: Final[int] = 100
RENDER_EDIT_OTHER: Final[int] = 101
CLIP_FILE: Final[str] = "clip_node"
CLIP_EDIT_TAG: Final[str] = "HeadMode"
CLIP_EDIT_VALUE: Final[int] = 1
CLIP_EDIT_OTHER: Final[int] = 2
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed"}
)
MAKE_TIMEOUT_S: Final[float] = 180.0

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def declare(call_id: str, op: str, sample: dict[str, Any]) -> None:
    """`CALLS[call_id] = (op, build)`: `build(ids)` answers `ids["args"]` when a stage supplies the
    arguments and `sample` (a well-formed set, for `--dry-run`) otherwise."""

    def build(ids: dict[str, Any]) -> dict[str, Any]:
        args = ids.get("args")
        return dict(args) if isinstance(args, dict) else dict(sample)

    CALLS[call_id] = (op, build)


_SEG = seg_at(sel("/wt/x"), 0, 0)
_NODE = {"kind": "node", "batch": {"kind": "batch", "uid": "uid-b"}, "name": "timewarp1"}
_VALUES = {TAG: NEW_VALUE}

declare("c_edit", "segment.edit_effect_setup", {"target": _SEG, "type": FX_TYPE, "values": _VALUES})
declare(
    "c_edit_keep",
    "segment.edit_effect_setup",
    {"target": _SEG, "type": FX_TYPE, "values": _VALUES, "keep_files": True},
)
declare(
    "p_plan",
    "plan.run",
    {
        "steps": [
            {
                "op": "segment.edit_effect_setup",
                "args": {"target": _SEG, "type": FX_TYPE, "values": _VALUES},
            }
        ],
        "label": "p5 setup edit",
    },
)
declare("n_edit", "node.edit_setup", {"target": _NODE, "values": _VALUES})
declare(
    "n_edit_render",
    "node.edit_setup",
    {"target": _NODE, "values": {RENDER_EDIT_TAG: RENDER_EDIT_VALUE}, "file": RENDER_FILE},
)
declare(
    "n_edit_clip",
    "node.edit_setup",
    {"target": _NODE, "values": {CLIP_EDIT_TAG: CLIP_EDIT_VALUE}, "file": CLIP_FILE},
)
declare(
    "c_read",
    "segment.read_effect_setup",
    {"target": _SEG, "type": FX_TYPE, "tags": [TAG, UNKNOWN_READ_TAG]},
)
declare("n_read", "node.read_setup", {"target": _NODE, "tags": [TAG]})
declare("r_revert", "journal.revert", {"group": "g_dry"})

# -- transport scaffolding (never through the ops under test) -----------------------------------

CREATE_FX_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_rows = []",
        "for _s in list(_t.segments):",
        "    if _s.type != 'Gap':",
        "        _had = [_v(_f.type) for _f in list(_s.effects)]",
        "        _ret = None",
        "        if @FX@ not in _had:",
        "            _ret = _s.create_effect(@FX@)",
        "        _rows.append({'rin': _s.record_in.frame, 'had': _had, 'returned': repr(_ret),",
        "                      'fx': [_v(_f.type) for _f in list(_s.effects)]})",
        "_result = _rows",
    ]
)

READ_FX_CODE: Final[str] = "\n".join(
    [
        "import os",
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "_fx = [_f for _f in list(_hit[0].effects) if _v(_f.type) == @FX@][0]",
        "_before = set(os.listdir(@DIR@))",
        "_ret = _fx.save_setup(os.path.join(@DIR@, @BASE@))",
        "_new = sorted(set(os.listdir(@DIR@)) - _before)",
        "_texts = []",
        "for _n in _new:",
        "    with open(os.path.join(@DIR@, _n), 'rb') as _fh:",
        "        _texts.append(_fh.read().decode('utf-8'))",
        "_result = {'returned': repr(_ret), 'files': _new, 'texts': _texts}",
    ]
)

BATCH_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = _d.create_batch_group(@NAME@, reels=['sch'])",
        "_n = _bg.create_node(@TYPE@)",
        "_r = _bg.create_node(@RENDER@)",
        "_cur = _v(_d.current_batch_group)",
        "_result = {'uid': _v(_bg.uid), 'node': _v(_n.name), 'render': _v(_r.name),",
        "           'isCurrent': _cur is not None and _v(_cur.uid) == _v(_bg.uid)}",
    ]
)

READ_NODE_CODE: Final[str] = "\n".join(
    [
        "import os",
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@][0]",
        "_hits = [_x for _x in _bg.nodes if _v(_x.name) == @NAME@]",
        "_result = {'nodes': sorted(_v(_x.name) for _x in _bg.nodes), 'hits': len(_hits)}",
        "if len(_hits) == 1:",
        "    _before = set(os.listdir(@DIR@))",
        "    _ret = _hits[0].save_node_setup(os.path.join(@DIR@, @BASE@))",
        "    _new = sorted(set(os.listdir(@DIR@)) - _before)",
        "    _texts = []",
        "    for _n in _new:",
        "        with open(os.path.join(@DIR@, _n), 'rb') as _fh:",
        "            _texts.append(_fh.read().decode('utf-8'))",
        "    _result.update(returned=repr(_ret), files=_new, texts=_texts)",
    ]
)

ADD_CLIP_NODE_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@][0]",
        "def _clip_nodes():",
        "    return [_x for _x in _bg.nodes if str(_v(_x.type)) == 'Clip']",
        "_had = [_v(_x.name) for _x in _clip_nodes()]",
        "_result = {'had': _had}",
        "if len(_had) == 0:",
        "    _reel = [_r for _r in list(_bg.reels) if str(_v(_r.name)) == 'sch'][0]",
        "    _fx = flame.find_by_wiretap_node_id(@FX_WT@)",
        "    try:",
        "        _result['fxCopyReturned'] = repr(flame.media_panel.copy(_fx, _reel))",
        "    except BaseException as _e:",
        "        _result['fxCopyRaised'] = type(_e).__name__ + ': ' + str(_e)",
        "    _result['clipNodesAfterFxCopy'] = [_v(_x.name) for _x in _clip_nodes()]",
        "    if len(_result['clipNodesAfterFxCopy']) == 0:",
        "        _src = flame.find_by_wiretap_node_id(@WT@)",
        "        _result['copyReturned'] = repr(flame.media_panel.copy(_src, _reel))",
        "_result['clipNodes'] = [_v(_x.name) for _x in _clip_nodes()]",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "prepare": PREPARE_CODE,
    "create_fx": CREATE_FX_CODE,
    "read_fx": READ_FX_CODE,
    "batch": BATCH_CODE,
    "read_node": READ_NODE_CODE,
    "add_clip_node": ADD_CLIP_NODE_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


def tag_values(text: str, tag: str) -> list[str]:
    """The values of every `<tag>value</tag>` in `text` (the reader the run judges with)."""
    name = re.escape(tag)
    return re.findall(f"<{name}>([^<]*)</{name}>", text)


def frame_mode(read: Any) -> str | None:
    """`TW_FrameMode` of a read made by transport (one file, the tag once), else None."""
    if not isinstance(read, dict) or len(read.get("texts") or []) != 1:
        return None
    found = tag_values(read["texts"][0], TAG)
    return found[0] if len(found) == 1 else None


def mode_bits(path: str | Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def intents(ctx: Ctx) -> int:
    return len([r for r in journal_records(ctx) if r.get("type") == "intent"])


def files_dir(ctx: Ctx) -> Path:
    return Path(ctx.ids["filesDir"])


async def read_fx(ctx: Ctx, wiretap: str, rin: int, tag: str) -> dict[str, Any] | None:
    code = fill(
        READ_FX_CODE,
        WT=wiretap,
        TRACK=0,
        RECORD_IN=rin,
        FX=FX_TYPE,
        DIR=str(files_dir(ctx)),
        BASE=f"{tag}_{time.monotonic_ns()}",
    )
    got = await tx_val(ctx, code, "pp5e_read_fx")
    return None if is_error(got) or not isinstance(got, dict) else got


async def read_mode(ctx: Ctx, wiretap: str, rin: int, tag: str) -> str | None:
    return frame_mode(await read_fx(ctx, wiretap, rin, tag))


def file_tag(read: Any, ext: str, tag: str) -> str | None:
    """The value of `tag` (once) in the file with extension `ext` of a read made by transport."""
    if not isinstance(read, dict):
        return None
    files, texts = read.get("files") or [], read.get("texts") or []
    hits = [t for f, t in zip(files, texts, strict=False) if str(f).endswith("." + ext)]
    if len(hits) != 1:
        return None
    found = tag_values(hits[0], tag)
    return found[0] if len(found) == 1 else None


async def read_node(ctx: Ctx, tag: str, name: str | None = None) -> dict[str, Any] | None:
    code = fill(
        READ_NODE_CODE,
        UID=ctx.ids["batchUid"],
        NAME=name if name is not None else ctx.ids["nodeName"],
        DIR=str(files_dir(ctx)),
        BASE=f"{tag}_{time.monotonic_ns()}",
    )
    got = await tx_val(ctx, code, "pp5e_read_node")
    return None if is_error(got) or not isinstance(got, dict) else got


async def run_call(session: Any, ctx: Ctx, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
    op, _build = CALLS[call_id]
    return await do(session, ctx, op, args)


def read_args(seg: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    return {"target": seg, "type": FX_TYPE, "tags": tags}


def edit_args(seg: dict[str, Any], values: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"target": seg, "type": FX_TYPE, "values": values, **extra}


def work_dir_is_empty(ctx: Ctx) -> bool:
    root = ctx.journal_dir / "setups"
    return not root.exists() or not any(root.iterdir())


def work_dirs(ctx: Ctx) -> list[str]:
    root = ctx.journal_dir / "setups"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def remove_kept_dir(ctx: Ctx, paths: list[Any]) -> None:
    """Deletes the directory keep_files left (only one under this run's own setups/), so that
    the later stages can check that a successful edit leaves nothing behind."""
    root = (ctx.journal_dir / "setups").resolve()
    kept = {Path(p).resolve().parent for p in paths if isinstance(p, str)}
    for d in kept:
        if d.parent == root and d.name.startswith("s_"):
            shutil.rmtree(d)


# -- prepare ------------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    out = OUTPUT_DIR / f"files_pp5e_{ctx.ts}"
    out.mkdir(parents=True, exist_ok=True)
    ids = ctx.ids
    ids["filesDir"] = str(out)
    made = await tx(
        ctx,
        PRELUDE
        + "\n"
        + fill(
            PREPARE_CODE,
            RG=name_of(ctx, "RG"),
            LIB=name_of(ctx, "LIB"),
            PATHS=[[n, str(p)] for n, p in CLIP_FILES],
        ),
        label="pp5e_prepare",
        timeout_s=240.0,
    )
    rec(ctx, "prepare_reel_group_library_and_the_clips", made.ok, resp=_br_dict(made))
    if not (made.ok and isinstance(made.result, dict)):
        return
    result = made.result
    ids.update(
        seqReelWt=result["seqReelWt"],
        fps=result["fps"],
        width=result["width"],
        height=result["height"],
        clipWt={n: v["wt"] for n, v in result["clips"].items()},
    )
    clip_wt = ids["clipWt"][SOURCE_CLIP]
    struct = await tx_struct(ctx, clip_wt)
    media = [r for r in track_rows(struct, 0) if r["type"] != "Gap"]
    made_fx = await tx_val(
        ctx, fill(CREATE_FX_CODE, WT=clip_wt, TRACK=0, FX=FX_TYPE), "pp5e_create_fx_clip"
    )
    rec(
        ctx,
        "prepare_the_clip_has_a_timewarp",
        len(media) == 1
        and isinstance(made_fx, list)
        and len(made_fx) == 1
        and made_fx[0]["fx"] == [FX_TYPE],
        media=media,
        madeFx=made_fx,
    )
    if len(media) == 1:
        ids["clipRin"] = media[0]["rin"]
    seq = await make_seq(ctx, "SEQ", SEQUENCE_PUTS)
    if seq is not None:
        ids["seqWt"] = seq["wt"]
        created = await tx_val(
            ctx, fill(CREATE_FX_CODE, WT=seq["wt"], TRACK=0, FX=FX_TYPE), "pp5e_create_fx_seq"
        )
        ok = (
            isinstance(created, list)
            and len(created) == len(SEQUENCE_PUTS)
            and all(row["fx"] == [FX_TYPE] for row in created)
        )
        rec(ctx, "prepare_the_three_segments_of_the_sequence_have_a_timewarp", ok, created=created)
        observe(
            ctx,
            "prepare_which_segments_of_the_sequence_already_had_a_timewarp_so_none_was_created",
            had=[row["had"] for row in created] if isinstance(created, list) else created,
            note="a sequence made from a clip that has a Timewarp carries it; create is skipped"
            " for a segment that has one",
        )
        if ok:
            ids["seqRins"] = [row["rin"] for row in created]
    batch = await tx_val(
        ctx,
        fill(BATCH_CODE, NAME=name_of(ctx, "B"), TYPE=FX_TYPE, RENDER=RENDER_TYPE),
        "pp5e_batch",
        MAKE_TIMEOUT_S,
    )
    rec(
        ctx,
        "prepare_the_batch_group_with_a_timewarp_node_and_a_render_node_is_made_and_is_current",
        isinstance(batch, dict) and batch.get("isCurrent") is True,
        batch=batch,
    )
    if isinstance(batch, dict):
        ids.update(batchUid=batch["uid"], nodeName=batch["node"], renderName=batch.get("render"))


# -- stage c: a clip on the Desktop -------------------------------------------------------------


async def stage_c(ctx: Ctx) -> None:
    ids = ctx.ids
    wiretap, rin = ids["clipWt"][SOURCE_CLIP], ids["clipRin"]
    seg = seg_at(sel(wiretap), 0, rin)
    first = await read_fx(ctx, wiretap, rin, "c1")
    rec(
        ctx,
        "c1_the_setup_is_one_file_and_the_tag_occurs_once_with_the_value_2",
        frame_mode(first) == START_VALUE and len(first["files"]) == 1,
        files=first and first["files"],
        mode=frame_mode(first),
    )
    if frame_mode(first) != START_VALUE:
        return
    async with server_session(ctx, "pp5e-c") as session:
        await judge_segment_read(
            session, ctx, seg, "c1a_the_read_gives_2_and_null_with_a_reason_and_leaves_nothing", "2"
        )
        before = await fingerprint(session, ctx, wiretap)
        env = await run_call(session, ctx, "c_edit", edit_args(seg, {TAG: NEW_VALUE}))
        result = _result_of(env)
        mode = await read_mode(ctx, wiretap, rin, "c2")
        rec(
            ctx,
            "c2_the_edit_is_ok_the_tags_say_2_to_0_and_the_independent_read_says_0",
            env.get("ok") is True
            and result.get("tags") == {TAG: {"before": "2", "requested": "0", "after": "0"}}
            and mode == "0"
            and work_dir_is_empty(ctx),
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            tags=result.get("tags"),
            degraded=env.get("degraded"),
            transport=mode,
        )
        await judge_segment_read(
            session, ctx, seg, "c2a_the_read_after_the_edit_gives_0_and_leaves_nothing", "0"
        )
        group = (env.get("journal") or {}).get("group")
        counts = (len(checkpoint_records(ctx)), intents(ctx), len(journal_states(ctx)))
        same = await run_call(session, ctx, "c_edit", edit_args(seg, {TAG: NEW_VALUE}))
        rec(
            ctx,
            "c3_the_same_value_again_is_precondition_unmet_nothing_to_change_and_journals_nothing",
            same.get("errorCode") == "PRECONDITION_UNMET"
            and "nothing to change" in str(same.get("error"))
            and (len(checkpoint_records(ctx)), intents(ctx), len(journal_states(ctx))) == counts,
            errorCode=same.get("errorCode"),
            error=same.get("error"),
        )
        bad = await run_call(session, ctx, "c_edit", edit_args(seg, {TAG: BAD_VALUE}))
        unknown = await run_call(session, ctx, "c_edit", edit_args(seg, {UNKNOWN_TAG: 1}))
        rec(
            ctx,
            "c4_a_value_with_a_bracket_is_invalid_args_and_an_unknown_tag_is_counted_zero",
            bad.get("errorCode") == "INVALID_ARGS"
            and unknown.get("errorCode") == "PRECONDITION_UNMET"
            and _result_of(unknown).get("tagCounts") == {UNKNOWN_TAG: 0}
            and (len(checkpoint_records(ctx)), intents(ctx), len(journal_states(ctx))) == counts
            and await read_mode(ctx, wiretap, rin, "c4") == "0",
            bad=bad.get("error"),
            unknown=unknown.get("error"),
            tagCounts=_result_of(unknown).get("tagCounts"),
        )
        bad_name = await run_call(session, ctx, "c_read", read_args(seg, [BAD_TAG_NAME]))
        rec(
            ctx,
            "c4a_a_tag_name_with_a_bracket_is_invalid_args_and_journals_nothing",
            bad_name.get("errorCode") == "INVALID_ARGS"
            and (len(checkpoint_records(ctx)), intents(ctx), len(journal_states(ctx))) == counts,
            errorCode=bad_name.get("errorCode"),
            error=bad_name.get("error"),
        )
        out = await revert(session, ctx, {"journal": {"group": group}})
        new_wt = restored_wt(out)
        back = await read_mode(ctx, new_wt, rin, "c5") if new_wt else None
        after = await fingerprint(session, ctx, new_wt) if new_wt else None
        rec(
            ctx,
            "c5_the_revert_puts_the_clip_back_to_2_with_the_fingerprint_it_had",
            out.get("ok") is True
            and back == START_VALUE
            and before is not None
            and after == before,
            error=out.get("error"),
            transport=back,
            fingerprints=[before, after],
        )
        if not new_wt:
            return
        ids["clipWt"][SOURCE_CLIP] = new_wt
        keep = await run_call(
            session,
            ctx,
            "c_edit_keep",
            edit_args(seg_at(sel(new_wt), 0, rin), {TAG: NEW_VALUE}, keep_files=True),
        )
        await judge_kept_files(ctx, keep)
        kept_back = await revert(session, ctx, keep)
        kept_wt = restored_wt(kept_back)
        rec(
            ctx,
            "c6a_the_revert_of_the_kept_edit_gives_the_restored_clip_a_wiretap_id",
            kept_back.get("ok") is True and kept_wt is not None,
            error=kept_back.get("error"),
            wiretap=kept_wt,
        )
        if kept_wt:
            ids["clipWt"][SOURCE_CLIP] = kept_wt
        observe(
            ctx,
            "c6_how_often_the_value_tag_occurs_in_the_file",
            count=len(tag_values((first or {}).get("texts", [""])[0], "Value")),
        )


async def judge_segment_read(
    session: Any, ctx: Ctx, seg: dict[str, Any], name: str, expected: str
) -> None:
    dirs, written = work_dirs(ctx), intents(ctx)
    env = await run_call(session, ctx, "c_read", read_args(seg, [TAG, UNKNOWN_READ_TAG]))
    result = _result_of(env)
    values, problems = result.get("values") or {}, result.get("problems") or {}
    rec(
        ctx,
        name,
        env.get("ok") is True
        and values.get(TAG) == expected
        and values.get(UNKNOWN_READ_TAG, "missing") is None
        and UNKNOWN_READ_TAG in problems
        and TAG not in problems
        and work_dirs(ctx) == dirs
        and intents(ctx) == written,
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        values=values,
        problems=problems,
        workDirsBefore=dirs,
        workDirsAfter=work_dirs(ctx),
    )


async def judge_kept_files(ctx: Ctx, env: dict[str, Any]) -> None:
    result = _result_of(env)
    files = result.get("files") or {}
    paths = [files.get(k) for k in ("original", "edited", "readback")]
    present = all(isinstance(p, str) and os.path.isfile(p) for p in paths)
    texts = [Path(p).read_text(encoding="utf-8") if present else "" for p in paths]
    root = ctx.journal_dir / "setups"
    rec(
        ctx,
        "c6_keep_files_leaves_private_files_with_the_old_and_the_new_value",
        env.get("ok") is True
        and result.get("filesKept") is True
        and present
        and all(str(p).startswith(str(root)) for p in paths)
        and all(mode_bits(p) == 0o600 for p in paths)
        and mode_bits(Path(paths[0]).parent) == 0o700
        and mode_bits(root) == 0o700
        and tag_values(texts[0], TAG) == [START_VALUE]
        and tag_values(texts[1], TAG) == ["0"]
        and tag_values(texts[2], TAG) == ["0"],
        error=env.get("error"),
        files=files,
        modes=[mode_bits(p) for p in paths if isinstance(p, str) and os.path.exists(p)],
    )
    remove_kept_dir(ctx, paths)


# -- stage p: a plan ----------------------------------------------------------------------------


async def stage_p(ctx: Ctx) -> None:
    ids = ctx.ids
    if "seqRins" not in ids:
        rec(ctx, "p0_the_sequence_is_ready", False)
        return
    wiretap, rins = ids["seqWt"], ids["seqRins"]
    before = [await read_mode(ctx, wiretap, rin, f"p0_{i}") for i, rin in enumerate(rins)]
    rec(ctx, "p0_the_three_segments_read_2_before", before == [START_VALUE] * 3, before=before)
    if before != [START_VALUE] * 3:
        return
    steps = [
        {
            "op": "segment.edit_effect_setup",
            "args": edit_args(seg_at(sel(wiretap), 0, rin), {TAG: NEW_VALUE}),
        }
        for rin in rins
    ]
    async with server_session(ctx, "pp5e-p") as session:
        checkpoints = len(checkpoint_records(ctx))
        env = await run_call(session, ctx, "p_plan", {"steps": steps, "label": "p5 setup edit"})
        after = [await read_mode(ctx, wiretap, rin, f"p1_{i}") for i, rin in enumerate(rins)]
        rows = _result_of(env).get("steps") or []
        rec(
            ctx,
            "p1_the_plan_is_ok_one_checkpoint_was_taken_and_all_three_read_0",
            env.get("ok") is True
            and len(rows) == 3
            and all(r.get("ok") is True for r in rows)
            and len(checkpoint_records(ctx)) == checkpoints + 1
            and after == ["0"] * 3,
            error=env.get("error"),
            steps=[(r.get("ok"), r.get("errorCode")) for r in rows],
            after=after,
            checkpoints=len(checkpoint_records(ctx)) - checkpoints,
        )
        out = await revert(session, ctx, env)
        new_wt = restored_wt(out)
        back = (
            [await read_mode(ctx, new_wt, rin, f"p2_{i}") for i, rin in enumerate(rins)]
            if new_wt
            else None
        )
        rec(
            ctx,
            "p2_one_revert_of_the_plan_puts_all_three_back_to_2",
            out.get("ok") is True and back == [START_VALUE] * 3,
            error=out.get("error"),
            back=back,
        )


# -- stage n: a Batch node ----------------------------------------------------------------------


async def stage_n(ctx: Ctx) -> None:
    ids = ctx.ids
    if "batchUid" not in ids:
        rec(ctx, "n0_the_batch_group_is_ready", False)
        return
    node = {
        "kind": "node",
        "batch": {"kind": "batch", "uid": ids["batchUid"]},
        "name": ids["nodeName"],
    }
    first = await read_node(ctx, "n1")
    rec(
        ctx,
        "n1_the_node_setup_is_one_file_and_the_tag_occurs_once_with_the_value_2",
        frame_mode(first) == START_VALUE and len((first or {}).get("files") or []) == 1,
        files=first and first.get("files"),
        mode=frame_mode(first),
    )
    if frame_mode(first) != START_VALUE:
        return
    async with server_session(ctx, "pp5e-n") as session:
        await judge_node_reads(session, ctx, node)
        env = await run_call(session, ctx, "n_edit", {"target": node, "values": {TAG: NEW_VALUE}})
        result = _result_of(env)
        after = await read_node(ctx, "n2")
        rec(
            ctx,
            "n2_the_edit_is_ok_the_tags_say_2_to_0_the_independent_read_says_0_the_name_is_kept",
            env.get("ok") is True
            and result.get("tags") == {TAG: {"before": "2", "requested": "0", "after": "0"}}
            and frame_mode(after) == "0"
            and (after or {}).get("hits") == 1
            and work_dir_is_empty(ctx),
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            tags=result.get("tags"),
            degraded=env.get("degraded"),
            transport=frame_mode(after),
            hits=(after or {}).get("hits"),
            workDirs=work_dirs(ctx),
            nodes=after and after.get("nodes"),
        )
        out = await revert(session, ctx, env)
        back = await read_node(ctx, "n3")
        rec(
            ctx,
            "n3_the_revert_puts_the_node_back_to_2",
            out.get("ok") is True and frame_mode(back) == START_VALUE,
            error=out.get("error"),
            errorCode=out.get("errorCode"),
            transport=frame_mode(back),
        )
        await judge_render_and_clip_edits(session, ctx, node)


async def judge_multi_file_edit(
    session: Any,
    ctx: Ctx,
    call_id: str,
    stage: str,
    node: dict[str, Any],
    file: str,
    tag: str,
    values: tuple[int, int],
) -> None:
    """Edit `tag` of the node's own file `file`, judge it by the transport's own save, revert."""
    name = node["name"]
    before = file_tag(await read_node(ctx, f"{stage}a", name), file, tag)
    rec(
        ctx,
        f"{stage}a_the_transport_reads_{tag}_of_the_{file}_file",
        before is not None,
        tag=before,
    )
    if before is None:
        return
    wanted = values[1] if before == str(values[0]) else values[0]
    env = await run_call(
        session, ctx, call_id, {"target": node, "values": {tag: wanted}, "file": file}
    )
    result = _result_of(env)
    saved = await read_node(ctx, f"{stage}b", name)
    rec(
        ctx,
        f"{stage}b_the_edit_of_{file}_is_ok_the_independent_read_says_{wanted}_other_files_listed",
        env.get("ok") is True
        and result.get("file") == file
        and file_tag(saved, file, tag) == str(wanted)
        and isinstance(result.get("otherFilesChanged"), list),
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        before=before,
        transport=file_tag(saved, file, tag),
        resultFile=result.get("file"),
        otherFilesChanged=result.get("otherFilesChanged"),
    )
    observe(
        ctx,
        f"{stage}b_what_the_edit_of_{file}_said",
        savedFiles=result.get("savedFiles"),
        tags=result.get("tags"),
        otherFilesChanged=result.get("otherFilesChanged"),
        otherDifferences=result.get("otherDifferences"),
        degraded=env.get("degraded"),
        transportFiles=saved and saved.get("files"),
    )
    out = await revert(session, ctx, env)
    back = file_tag(await read_node(ctx, f"{stage}c", name), file, tag)
    rec(
        ctx,
        f"{stage}c_the_revert_puts_{tag}_back_to_{before}",
        out.get("ok") is True and back == before,
        error=out.get("error"),
        errorCode=out.get("errorCode"),
        transport=back,
    )


async def judge_render_and_clip_edits(session: Any, ctx: Ctx, node: dict[str, Any]) -> None:
    render_name = ctx.ids.get("renderName")
    if not render_name:
        rec(ctx, "n4_the_render_node_is_ready", False, ids=sorted(ctx.ids))
    else:
        await judge_multi_file_edit(
            session,
            ctx,
            "n_edit_render",
            "n4",
            {**node, "name": render_name},
            RENDER_FILE,
            RENDER_EDIT_TAG,
            (RENDER_EDIT_VALUE, RENDER_EDIT_OTHER),
        )
    added = await tx_val(
        ctx,
        fill(
            ADD_CLIP_NODE_CODE,
            UID=ctx.ids["batchUid"],
            FX_WT=ctx.ids["clipWt"][SOURCE_CLIP],
            WT=ctx.ids["clipWt"][PLAIN_CLIP],
        ),
        "pp5e_add_clip_node",
        MAKE_TIMEOUT_S,
    )
    names = added.get("clipNodes") if isinstance(added, dict) else None
    rec(
        ctx,
        "n6_the_batch_group_has_a_clip_node",
        isinstance(names, list) and len(names) >= 1,
        added=added,
    )
    if isinstance(added, dict):
        observe(
            ctx,
            "n6_what_media_panel_copy_of_the_clip_with_a_timewarp_into_the_schematic_reel_did",
            fxCopyReturned=added.get("fxCopyReturned"),
            fxCopyRaised=added.get("fxCopyRaised"),
            clipNodesAfterFxCopy=added.get("clipNodesAfterFxCopy"),
        )
    if isinstance(names, list) and names:
        await judge_multi_file_edit(
            session,
            ctx,
            "n_edit_clip",
            "n6",
            {**node, "name": names[0]},
            CLIP_FILE,
            CLIP_EDIT_TAG,
            (CLIP_EDIT_VALUE, CLIP_EDIT_OTHER),
        )


async def judge_node_reads(session: Any, ctx: Ctx, node: dict[str, Any]) -> None:
    dirs, written = work_dirs(ctx), intents(ctx)
    env = await run_call(session, ctx, "n_read", {"target": node, "tags": [TAG]})
    result = _result_of(env)
    rec(
        ctx,
        "n1a_node_read_setup_of_the_timewarp_node_gives_2_and_leaves_nothing",
        env.get("ok") is True
        and (result.get("values") or {}).get(TAG) == START_VALUE
        and work_dirs(ctx) == dirs
        and intents(ctx) == written,
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        values=result.get("values"),
        problems=result.get("problems"),
        savedFiles=result.get("savedFiles"),
    )
    render_name = ctx.ids.get("renderName")
    if not render_name:
        rec(ctx, "n1b_the_render_node_is_ready", False, ids=sorted(ctx.ids))
        return
    render = {**node, "name": render_name}
    env = await run_call(session, ctx, "n_read", {"target": render, "tags": list(RENDER_TAGS)})
    result = _result_of(env)
    values, file_of = result.get("values") or {}, result.get("fileOf") or {}
    rec(
        ctx,
        "n1b_node_read_setup_of_the_render_node_gives_the_pixel_format_from_the_lut_file",
        env.get("ok") is True
        and values.get(RENDER_JUDGED_TAG) is not None
        and file_of.get(RENDER_JUDGED_TAG) == RENDER_JUDGED_FILE,
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        values=values,
        fileOf=file_of,
    )
    observe(
        ctx,
        "n1b_the_render_node_use_luts_and_the_files_its_save_wrote",
        values=values,
        useLutsIsNull=values.get("UseLuts") is None,
        fileOf=file_of,
        tagCounts=result.get("tagCounts"),
        savedFiles=result.get("savedFiles"),
        notText=result.get("notText"),
        problems=result.get("problems"),
        workDirsLeft=work_dirs(ctx) != dirs,
        intentsAdded=intents(ctx) - written,
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
    prefix = f"{PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "pp5e_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "pp5e_left")
    ctx.results["cleanup"] = {"deleted": gone, "stillThere": left}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted_the_stash_library_too",
        isinstance(gone, dict)
        and all(ok is True for rows_ in gone.values() for _n, ok in rows_)
        and left == {"reelGroups": [], "batchGroups": [], "libraries": []},
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
        if "clipRin" in ctx.ids and "clipWt" in ctx.ids:
            for name, stage in (("c", stage_c), ("p", stage_p), ("n", stage_n)):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "pp5e_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------


def selftest_reader() -> list[str]:
    """The tag reader on made-up setup text (client side only). Returns the problems."""
    text = (
        "<TimewarpNode><TW_FrameMode>2</TW_FrameMode>"
        '<Channel Name="X"><Value>1</Value><Value>2</Value></Channel></TimewarpNode>'
    )
    problems = []
    if tag_values(text, TAG) != ["2"]:
        problems.append(f"tag_values {tag_values(text, TAG)}")
    if tag_values(text, "Value") != ["1", "2"]:
        problems.append("a tag that occurs twice must give both values")
    if frame_mode({"texts": [text]}) != "2":
        problems.append("frame_mode of one file")
    if frame_mode({"texts": [text, text]}) is not None or frame_mode(None) is not None:
        problems.append("frame_mode of two files or of nothing must be None")
    if frame_mode({"texts": [text + text]}) is not None:
        problems.append("frame_mode of a tag that occurs twice must be None")
    return problems


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
        validation = validate_op_args(op, build({}))
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
    print("===== the tag reader on made-up setup text =====")
    problems = selftest_reader()
    print("OK  reader" if not problems else f"NG  reader: {problems}")
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
    journal_dir = OUTPUT_DIR / f"journal_p5se_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p5_setup_edit_e2e_{ts}.json",
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
