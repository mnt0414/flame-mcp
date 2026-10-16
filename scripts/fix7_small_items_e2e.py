#!/usr/bin/env python3
"""Real-Flame end-to-end of several small items: newerGroups on a CONFLICT of `journal.revert`,
`journal.poll`, `journal.inflight`, `segment.info` source facts, and `node.save_setup` of a Clip
node, then `node.load_setup` of its main file. Run with the `.venv` Python;
`scripts/run_e2e.sh fix7_small_items` starts Flame; `--dry-run` connects to nothing, lists the
calls and compiles the code. The Media Panel notice needs a modal Flame cannot be made to open
here: it is covered by the in-process tests only.

Checks: Several small items: `newerGroups` on a conflict of `journal.revert`, `journal.poll`,
`journal.inflight`, the source facts of `segment.info`, and `node.save_setup` followed by
`node.load_setup` on a Clip node.

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (a segment's source facts, the files a setup save wrote, the
fingerprints). Everything this run makes is named `PF7E_<ts>_...`
(scripts/p2_4_clean_probe_project.py knows the prefix) and is deleted at the end by that
prefix, the stash library (`PF7E_<ts>_CKPT`, passed to every server as
FLAME_MCP_CHECKPOINT_LIBRARY) included; then flame.exit(). On disk this run leaves: its own
journal directory (`p3/journal_pf7e_e2e_<ts>/`), the result files, and
`p3/files_pf7e_<ts>/` (the setup files and the rendered frames). No test hook is set. The
first step refuses to run anywhere but in the project `flame_mcp_probe`.

Stages
  a  A sequence; `segment.slip` (group G1), `segment.create_effect` Blur on the same
     segment (G2). `journal.revert(G1)` is a CONFLICT whose result.newerGroups is exactly [G2
     with its op and ts] and whose message says to revert it first (nothing changed: the
     fingerprint is the one G2 left). `journal.revert(G2)` is ok, then `journal.revert(G1)` is
     ok and the fingerprint is the one before G1.
  b  `segment.info` of the media segment of a sequence made from `p2_len72.mov`: filePath equals
     the path that file was imported from AND the segment's `file_path` as transport reads it;
     tapeName / sourceUid / sourceUnlinked equal the transport's reads; the Gap before the segment
     has all four null.
  c  `eval.run` (read) sleeps 2.5 s with timeout_s=1: TIMEOUT, timeoutPhase running, a
     pending.handle, and the error text names journal.poll and journal.check. While it runs a
     second server's `journal.inflight` lists its record (label, code, elapsedS). `journal.poll` of
     the handle at once is `pending`; after the sleep it is `done` with Flame's raw result; asking
     again, and asking for a handle nobody has, is PRECONDITION_UNMET.
  d  A Batch group with a Clip node (an imported `clip1.mov`) and a Write File (4 frames):
     `render.batch` then `journal.poll` of the intent's handle is INVALID_ARGS saying the group is
     already decided; a malformed handle is INVALID_ARGS; journal.check still works.
     `node.save_setup` of the Clip node: result.files is what the directory really holds under
     that name (listed by this client), result.path is the `.clip_node` one. `node.load_setup` of
     result.path into the same node: recorded as it came out (the load is not judged), then
     reverted.
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
    _br_dict,
    _result_of,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    guarded,
    rec,
    save,
    save_stage,
    server_session,
    tx,
)
from p2_4_common import (  # noqa: E402
    flame_exit,
    journal_states,
    observe,
    ops_used,
    print_observations,
    value_like,
)
from p3_1a_read_e2e import PRELUDE, fill, is_error, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    CKPT_ENV,
    OUTPUT_DIR,
    PREPARE_CODE,
    Results,
    call,
    ckpt_library,
    fingerprint,
    intent_of,
    media_row,
    name_of,
    seg_at,
    track_rows,
    tx_struct,
)
from p3_3_e2e_common import CLIP_FILES as P33_CLIP_FILES  # noqa: E402
from p3_3_e2e_common import make_seq, restored_wt, revert  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.policy import Policy  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

PREFIX: Final[str] = "PF7E"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = tuple(
    (n, p) for n, p in P33_CLIP_FILES if n in ("clip1", "p2_len72")
)
"""clip1 is only there for the frame rate and size the sequences are made with."""
SOURCE_CLIP: Final[str] = "p2_len72"
TRIM_HANDLE: Final[int] = 14
SLIP_FRAMES: Final[int] = 3
SLEEP_S: Final[float] = 2.5
EVAL_TIMEOUT_S: Final[int] = 1
INFLIGHT_PROBE_DELAY_S: Final[float] = 0.4
WRITER: Final[str] = "pf7e_write"
NOTE_CHANGED: Final[str] = "PF7E note changed after the save"
RENDER_FRAMES: Final[int] = 4
SLEEP_MARKER: Final[str] = "PF7E_slept"
CLIP_NODE_EXTENSIONS: Final[tuple[str, ...]] = (
    ".clip_node",
    ".clip_node_clip",
    ".clip_node_format",
    ".lut_node",
    ".resize",
    ".resize_node",
)
"""The six files one Clip node save was seen to write (an observation, not a premise:
the run judges result.files against the directory it lists itself)."""
SOURCE_KEYS: Final[tuple[str, ...]] = ("filePath", "tapeName", "sourceUid", "sourceUnlinked")
MAKE_TIMEOUT_S: Final[float] = 180.0
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed", "partially_reverted"}
)

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def declare(call_id: str, op: str, build: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    CALLS[call_id] = (op, build)


def seg_target(ids: dict[str, Any]) -> dict[str, Any]:
    return seg_at({"kind": "wiretap", "id": ids["wt"]}, 0, ids["rin"])


def batch_sel(uid: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid}


def node_sel(uid: str, name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": batch_sel(uid), "name": name}


declare(
    "a_slip",
    "segment.slip",
    lambda ids: {"target": seg_target(ids), "offset": SLIP_FRAMES},
)
declare(
    "a_create_effect",
    "segment.create_effect",
    lambda ids: {"target": seg_target(ids), "type": "Blur"},
)
declare("a_revert", "journal.revert", lambda ids: {"group": ids["group"]})
declare("b_info", "segment.info", lambda ids: {"target": seg_target(ids)})
declare(
    "c_eval_sleep",
    "eval.run",
    lambda ids: {
        "code": f"import time\ntime.sleep({SLEEP_S})\n_result = {SLEEP_MARKER!r}",
        "effect": "read",
        "timeout_s": EVAL_TIMEOUT_S,
    },
)
declare("c_poll", "journal.poll", lambda ids: {"handle": ids["handle"]})
declare(
    "c_inflight",
    "journal.inflight",
    lambda ids: {"limit": ids.get("limit", 50)},
)
declare("d_node_list", "node.list", lambda ids: {"target": batch_sel(ids["uid"]), "limit": 200})
declare(
    "d_create_write",
    "node.create",
    lambda ids: {"target": batch_sel(ids["uid"]), "type": "Write File", "name": WRITER},
)
declare(
    "d_connect",
    "node.connect",
    lambda ids: {
        "source": node_sel(ids["uid"], ids["clipName"]),
        "target": node_sel(ids["uid"], WRITER),
    },
)
declare(
    "d_get_writer",
    "node.get_attributes",
    lambda ids: {
        "target": node_sel(ids["uid"], WRITER),
        "keys": ["media_path", "range_start", "range_end"],
    },
)
declare(
    "d_set_writer",
    "node.set_attributes",
    lambda ids: {"target": node_sel(ids["uid"], WRITER), "values": ids["writerValues"]},
)
declare("d_render", "render.batch", lambda ids: {"target": batch_sel(ids["uid"])})
declare("d_poll_render", "journal.poll", lambda ids: {"handle": ids["handle"]})
declare("d_check_render", "journal.check", lambda ids: {"group": ids["renderGroup"]})
declare(
    "d_save_setup",
    "node.save_setup",
    lambda ids: {"target": node_sel(ids["uid"], ids["clipName"]), "path": ids["setupPath"]},
)
declare(
    "d_get_note",
    "node.get_attributes",
    lambda ids: {"target": node_sel(ids["uid"], ids["clipName"]), "keys": ["note"]},
)
declare(
    "d_set_note",
    "node.set_attributes",
    lambda ids: {"target": node_sel(ids["uid"], ids["clipName"]), "values": {"note": NOTE_CHANGED}},
)
declare(
    "d_load_setup",
    "node.load_setup",
    lambda ids: {"target": node_sel(ids["uid"], ids["clipName"]), "path": ids["setupFile"]},
)

# -- transport scaffolding (never through the op under test) ------------------------------------

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

MAKE_BATCH_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = _d.create_batch_group(@GNAME@)",
        "_rn = _v(_bg.reels[0].name)",
        "_bg.import_clip(@PATH@, _rn)",
        "_decoy = _d.create_batch_group(@DNAME@)",
        "_result = {'uid': _v(_bg.uid), 'decoyUid': _v(_decoy.uid)}",
    ]
)

SEG_SOURCE_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "def _plain(_x):",
        "    _x = _v(_x)",
        "    return _x if _x is None or isinstance(_x, (str, int, float, bool)) else repr(_x)",
        "_result = None",
        "if _hit:",
        "    _s = _hit[0]",
        "    _result = {",
        "        'type': _s.type,",
        "        'filePath': _g(lambda: _plain(_s.file_path)),",
        "        'tapeName': _g(lambda: _plain(_s.tape_name)),",
        "        'sourceUid': _g(lambda: _plain(_s.source_uid)),",
        "        'sourceUnlinked': _g(lambda: _plain(_s.source_unlinked)),",
        "    }",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "prepare": PREPARE_CODE,
    "make_batch": MAKE_BATCH_CODE,
    "seg_source": SEG_SOURCE_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def run_call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    return await call(session, ctx, op, build({**ctx.ids, **extra}))


def group_of(env: dict[str, Any]) -> str | None:
    value = (env.get("journal") or {}).get("group")
    return value if isinstance(value, str) else None


# -- prepare ------------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    ids = ctx.ids
    project = await tx_val(ctx, PROJECT_CODE, "pf7e_project")
    rec(ctx, "prepare_the_project_is_the_probe_project", project == TARGET_PROJECT, project=project)
    if project != TARGET_PROJECT:
        return
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    clip1 = MEDIA_DIR / "clip1.mov"
    if missing or not clip1.is_file():
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
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
        label="pf7e_prepare",
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
    batch = await tx(
        ctx,
        PRELUDE
        + "\n"
        + fill(
            MAKE_BATCH_CODE,
            GNAME=name_of(ctx, "GB"),
            DNAME=name_of(ctx, "DECOY"),
            PATH=str(clip1),
        ),
        label="pf7e_make_batch",
        timeout_s=MAKE_TIMEOUT_S,
    )
    rec(ctx, "prepare_batch_group_with_a_clip_node_and_a_decoy", batch.ok, resp=_br_dict(batch))
    if batch.ok and isinstance(batch.result, dict):
        ids.update(uid=batch.result["uid"], decoyUid=batch.result["decoyUid"])
    files_dir = OUTPUT_DIR / f"files_pf7e_{ctx.ts}"
    (files_dir / "setup").mkdir(parents=True, exist_ok=True)
    (files_dir / "render").mkdir(parents=True, exist_ok=True)
    ids["filesDir"] = str(files_dir)


async def fresh(ctx: Ctx, tag: str) -> dict[str, Any] | None:
    """A new sequence (p2_len72 trimmed head and tail by 14 on track 0: a Gap, then the media
    segment) and the media segment's record_in."""
    made = await make_seq(ctx, tag, ((0, SOURCE_CLIP, 1),), trims=((0, TRIM_HANDLE, TRIM_HANDLE),))
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    row = media_row(struct, 0, SOURCE_CLIP)
    if row is None:
        rec(ctx, f"{tag}_the_segment_is_there", False, track0=track_rows(struct, 0))
        return None
    gaps = [r for r in track_rows(struct, 0) if r["type"] == "Gap"]
    return {
        "wt": made["wt"],
        "uid": made["uid"],
        "name": made["name"],
        "rin": row["rin"],
        "gapRin": gaps[0]["rin"] if gaps else None,
    }


# -- a: newerGroups ---------------------------------------------------------------------------


async def stage_a(ctx: Ctx) -> None:
    async with server_session(ctx, "pf7e-a") as session:
        seq = await fresh(ctx, "SA")
        if seq is None:
            return
        ids = ctx.ids
        ids.update(wt=seq["wt"], rin=seq["rin"])
        fp0 = await fingerprint(session, ctx, seq["wt"])
        slip = await run_call(session, ctx, "a_slip")
        g1 = group_of(slip)
        fx = await run_call(session, ctx, "a_create_effect")
        g2 = group_of(fx)
        fp2 = await fingerprint(session, ctx, seq["wt"])
        rec(
            ctx,
            "a1_the_slip_and_the_effect_are_two_groups_that_changed_the_sequence",
            slip.get("ok") is True
            and fx.get("ok") is True
            and g1 is not None
            and g2 is not None
            and g1 != g2
            and fp0 is not None
            and fp2 != fp0,
            slip=[slip.get("ok"), slip.get("errorCode"), slip.get("error")],
            effect=[fx.get("ok"), fx.get("errorCode"), fx.get("error")],
            fingerprints=[fp0, fp2],
        )
        if g1 is None or g2 is None:
            return
        ids["group"] = g1
        refused = await run_call(session, ctx, "a_revert")
        result = _result_of(refused)
        rows = result.get("newerGroups")
        fp_after_refusal = await fingerprint(session, ctx, seq["wt"])
        rec(
            ctx,
            "a2_reverting_the_older_group_is_a_conflict_naming_the_later_group",
            refused.get("ok") is False
            and refused.get("errorCode") == "CONFLICT"
            and isinstance(rows, list)
            and len(rows) == 1
            and rows[0].get("group") == g2
            and rows[0].get("op") == "segment.create_effect"
            and isinstance(rows[0].get("ts"), str)
            and g2 in str(refused.get("error"))
            and "newest first" in str(refused.get("error"))
            and result.get("refused") is True,
            errorCode=refused.get("errorCode"),
            error=refused.get("error"),
            newerGroups=rows,
            want={"group": g2, "op": "segment.create_effect"},
        )
        rec(
            ctx,
            "a3_the_refused_revert_changed_nothing",
            fp_after_refusal == fp2,
            fingerprints=[fp2, fp_after_refusal],
        )
        undone_fx = await revert(session, ctx, fx)
        wt_mid = restored_wt(undone_fx)
        rec(
            ctx,
            "a4_reverting_the_later_group_first_works",
            undone_fx.get("ok") is True and wt_mid is not None,
            errorCode=undone_fx.get("errorCode"),
            error=undone_fx.get("error"),
        )
        if wt_mid is None:
            return
        undone_slip = await revert(session, ctx, slip)
        wt_end = restored_wt(undone_slip)
        fp_end = await fingerprint(session, ctx, wt_end) if wt_end else None
        rec(
            ctx,
            "a5_then_the_older_group_reverts_and_the_fingerprint_is_the_original",
            undone_slip.get("ok") is True and fp_end == fp0,
            errorCode=undone_slip.get("errorCode"),
            error=undone_slip.get("error"),
            fingerprints=[fp0, fp_end],
        )


# -- b: segment.info source facts -------------------------------------------------------------


async def read_source(ctx: Ctx, wt: str, record_in: int) -> dict[str, Any] | None:
    got = await tx_val(
        ctx, fill(SEG_SOURCE_CODE, WT=wt, TRACK=0, RECORD_IN=record_in), "pf7e_seg_source"
    )
    return None if is_error(got) or not isinstance(got, dict) else got


async def stage_b(ctx: Ctx) -> None:
    async with server_session(ctx, "pf7e-b") as session:
        seq = await fresh(ctx, "SB")
        if seq is None:
            return
        ids = ctx.ids
        ids.update(wt=seq["wt"], rin=seq["rin"])
        expected_path = str(dict(CLIP_FILES)[SOURCE_CLIP])
        direct = await read_source(ctx, seq["wt"], seq["rin"])
        env = await run_call(session, ctx, "b_info")
        result = _result_of(env)
        got = {k: result.get(k) for k in SOURCE_KEYS}
        observe(ctx, "b_source_facts_as_op_and_transport_read_them", op=got, transport=direct)
        rec(
            ctx,
            "b1_segment_info_file_path_is_the_path_the_file_was_imported_from",
            env.get("ok") is True and got["filePath"] == expected_path,
            filePath=got["filePath"],
            expected=expected_path,
            error=env.get("error"),
        )
        rec(
            ctx,
            "b2_the_four_facts_equal_what_transport_reads_from_the_segment",
            env.get("ok") is True
            and direct is not None
            and got["filePath"] == direct.get("filePath")
            and got["tapeName"] == direct.get("tapeName")
            and got["sourceUid"] == direct.get("sourceUid")
            and got["sourceUnlinked"] == direct.get("sourceUnlinked"),
            op=got,
            transport=direct,
        )
        observe(
            ctx,
            "b3_what_flame_gives_for_source_uid_and_source_unlinked_of_an_imported_clip",
            sourceUid=got["sourceUid"],
            sourceUidType=type(got["sourceUid"]).__name__,
            sourceUnlinked=got["sourceUnlinked"],
            sourceUnlinkedType=type(got["sourceUnlinked"]).__name__,
        )
        if seq["gapRin"] is None:
            rec(ctx, "b4_there_is_a_gap_to_read", False)
            return
        ids["rin"] = seq["gapRin"]
        gap = await run_call(session, ctx, "b_info")
        gap_result = _result_of(gap)
        rec(
            ctx,
            "b4_a_gap_has_all_four_null",
            gap.get("ok") is True
            and gap_result.get("isGap") is True
            and all(k in gap_result and gap_result[k] is None for k in SOURCE_KEYS),
            got={k: gap_result.get(k, "<missing>") for k in SOURCE_KEYS},
            isGap=gap_result.get("isGap"),
            error=gap.get("error"),
        )


# -- c: journal.poll (read TIMEOUT) and journal.inflight ---------------------------------------


async def stage_c(ctx: Ctx) -> None:
    ids = ctx.ids
    holder: dict[str, Any] = {}
    async with (
        server_session(ctx, "pf7e-c-eval") as session,
        server_session(ctx, "pf7e-c-other") as other,
    ):

        async def run_eval() -> None:
            holder["started"] = time.monotonic()
            holder["env"] = await run_call(session, ctx, "c_eval_sleep")
            holder["timeoutAfterS"] = round(time.monotonic() - holder["started"], 2)

        async with anyio.create_task_group() as group:
            group.start_soon(run_eval)
            await anyio.sleep(INFLIGHT_PROBE_DELAY_S)
            holder["inflight"] = await run_call(other, ctx, "c_inflight")
        env = holder["env"]
        pending = (env.get("pending") or {}).get("handle")
        error = str(env.get("error"))
        rec(
            ctx,
            "c1_a_read_that_runs_past_its_timeout_is_timeout_with_a_pending_handle",
            env.get("ok") is False
            and env.get("errorCode") == "TIMEOUT"
            and env.get("timeoutPhase") == "running"
            and isinstance(pending, str)
            and bool(pending),
            errorCode=env.get("errorCode"),
            timeoutPhase=env.get("timeoutPhase"),
            pending=env.get("pending"),
            afterS=holder.get("timeoutAfterS"),
        )
        rec(
            ctx,
            "c2_the_timeout_text_points_a_read_at_journal_poll_and_a_write_at_journal_check",
            "journal.poll" in error and "journal.check" in error,
            error=error,
        )
        listing = _result_of(holder["inflight"])
        items = listing.get("items") or []
        mine = [
            i
            for i in items
            if i.get("label") == "eval.run"
            and isinstance(i.get("elapsedS"), (int, float))
            and i["elapsedS"] < 10
        ]
        rec(
            ctx,
            "c3_journal_inflight_lists_the_call_while_it_runs",
            holder["inflight"].get("ok") is True
            and len(mine) == 1
            and isinstance(mine[0].get("sentAt"), str)
            and isinstance(mine[0].get("id"), str)
            and isinstance(mine[0].get("codeChars"), int)
            and "code" not in mine[0]
            and mine[0].get("thread") == "main"
            and listing.get("total", 0) >= 1,
            total=listing.get("total"),
            unreadable=listing.get("unreadable"),
            mine=mine,
            labels=[i.get("label") for i in items],
            degraded=holder["inflight"].get("degraded"),
        )
        if not isinstance(pending, str):
            return
        ids["handle"] = pending
        early = await run_call(session, ctx, "c_poll")
        early_result = _result_of(early)
        rec(
            ctx,
            "c4_journal_poll_right_after_the_timeout_says_pending",
            early.get("ok") is True and early_result.get("state") == "pending",
            result=early_result,
            error=early.get("error"),
        )
        await anyio.sleep(SLEEP_S + 1.0)
        done = await run_call(session, ctx, "c_poll")
        done_result = _result_of(done)
        raw = done_result.get("result")
        observe(ctx, "c_the_raw_result_journal_poll_handed_over", result=done_result)
        rec(
            ctx,
            "c5_journal_poll_after_the_sleep_hands_over_flames_raw_result",
            done.get("ok") is True
            and done_result.get("state") == "done"
            and done_result.get("ok") is True
            and SLEEP_MARKER in str(raw),
            state=done_result.get("state"),
            error=done.get("error"),
        )
        again = await run_call(session, ctx, "c_poll")
        rec(
            ctx,
            "c6_the_result_is_handed_over_once_asking_again_is_precondition_unmet",
            again.get("ok") is False and again.get("errorCode") == "PRECONDITION_UNMET",
            errorCode=again.get("errorCode"),
            error=again.get("error"),
        )
        unknown = await run_call(session, ctx, "c_poll", handle="0" * 32)
        rec(
            ctx,
            "c7_a_handle_nobody_has_is_precondition_unmet",
            unknown.get("ok") is False and unknown.get("errorCode") == "PRECONDITION_UNMET",
            errorCode=unknown.get("errorCode"),
            error=unknown.get("error"),
        )
        later = _result_of(await run_call(other, ctx, "c_inflight"))
        left = [
            i
            for i in later.get("items") or []
            if i.get("label") == "eval.run" and (i.get("elapsedS") or 99) < 30
        ]
        observe(
            ctx,
            "c_inflight_after_the_timeout",
            stillListed=len(left),
            total=later.get("total"),
        )


# -- d: journal.poll (render handle) and node.save_setup ---------------------------------------


async def build_render_group(session: Any, ctx: Ctx) -> bool:
    ids = ctx.ids
    listed = _result_of(await run_call(session, ctx, "d_node_list"))
    clip_nodes = [n["name"] for n in listed.get("items", []) if n.get("type") == "Clip"]
    rec(ctx, "d0_the_group_has_one_clip_node", len(clip_nodes) == 1, clipNodes=clip_nodes)
    if len(clip_nodes) != 1:
        return False
    ids["clipName"] = clip_nodes[0]
    created = await run_call(session, ctx, "d_create_write")
    connected = await run_call(session, ctx, "d_connect")
    current = _result_of(await run_call(session, ctx, "d_get_writer")).get("attributes", {})
    now = {
        k: (current.get(k) or {}).get("value") for k in ("media_path", "range_start", "range_end")
    }
    ids["writerValues"] = {
        "media_path": value_like(now["media_path"], str(Path(ids["filesDir"]) / "render") + "/"),
        "range_start": value_like(now["range_start"], 1),
        "range_end": value_like(now["range_end"], RENDER_FRAMES),
    }
    written = await run_call(session, ctx, "d_set_writer")
    rec(
        ctx,
        "d0_the_group_is_built_clip_to_write_file_with_the_media_path_and_range",
        all(e.get("ok") is True for e in (created, connected, written)),
        errors=[e.get("error") for e in (created, connected, written) if not e.get("ok")],
        values=ids["writerValues"],
    )
    return all(e.get("ok") is True for e in (created, connected, written))


async def judge_render_handle(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    env = await run_call(session, ctx, "d_render")
    group = group_of(env)
    intent = intent_of(ctx, group)
    handle = intent.get("handle")
    observe(
        ctx,
        "d_render_batch_and_the_handle_its_intent_carries",
        opOk=env.get("ok"),
        errorCode=env.get("errorCode"),
        handleSource=intent.get("handleSource"),
        handle=handle,
        frames=_result_of(env).get("writtenFrames"),
    )
    rec(
        ctx,
        "d1_the_render_ran_and_its_intent_carries_a_client_handle",
        env.get("ok") is True
        and group is not None
        and intent.get("handleSource") == "client"
        and isinstance(handle, str),
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        handleSource=intent.get("handleSource"),
    )
    if not (isinstance(handle, str) and group is not None):
        return
    ids.update(handle=handle, renderGroup=group)
    polled = await run_call(session, ctx, "d_poll_render")
    rec(
        ctx,
        "d2_journal_poll_of_a_decided_renders_handle_is_invalid_args_naming_the_group",
        polled.get("ok") is False
        and polled.get("errorCode") == "INVALID_ARGS"
        and "already decided" in str(polled.get("error"))
        and group in str(polled.get("error")),
        errorCode=polled.get("errorCode"),
        error=polled.get("error"),
    )
    malformed = await run_call(session, ctx, "d_poll_render", handle="../x")
    rec(
        ctx,
        "d2b_journal_poll_of_a_malformed_handle_is_invalid_args",
        malformed.get("ok") is False
        and malformed.get("errorCode") == "INVALID_ARGS"
        and "32 lowercase" in str(malformed.get("error")),
        errorCode=malformed.get("errorCode"),
        error=malformed.get("error"),
    )
    checked = await run_call(session, ctx, "d_check_render")
    rec(
        ctx,
        "d3_journal_check_of_the_group_is_still_fine",
        checked.get("ok") is True,
        errorCode=checked.get("errorCode"),
        error=checked.get("error"),
    )


def files_next_to(setup: Path) -> list[str]:
    stem = setup.name
    return sorted(n for n in os.listdir(setup.parent) if n.startswith(stem + "."))


async def judge_clip_setup(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    setup = Path(ids["filesDir"]) / "setup" / "clipnode"
    ids["setupPath"] = str(setup)
    saved = await run_call(session, ctx, "d_save_setup")
    result = _result_of(saved)
    files = result.get("files") or []
    on_disk = [str(setup.parent / n) for n in files_next_to(setup)]
    observe(
        ctx,
        "d_save_setup_of_the_clip_node_as_it_came_out",
        opOk=saved.get("ok"),
        errorCode=saved.get("errorCode"),
        error=saved.get("error"),
        path=result.get("path"),
        extension=result.get("extension"),
        files=[os.path.basename(f) for f in files],
        listedByThisClient=[os.path.basename(f) for f in on_disk],
        size=result.get("size"),
    )
    rec(
        ctx,
        "d4_node_save_setup_of_a_clip_node_succeeds_with_every_file_and_the_clip_node_as_path",
        saved.get("ok") is True
        and len(files) >= 1
        and sorted(files) == sorted(on_disk)
        and result.get("path") == str(setup) + ".clip_node"
        and result.get("extension") == ".clip_node"
        and result.get("path") in files,
        errorCode=saved.get("errorCode"),
        error=saved.get("error"),
        path=result.get("path"),
        files=files,
        onDisk=on_disk,
    )
    rec(
        ctx,
        "d5_the_files_are_the_ones_plan_82_saw_if_they_differ_it_is_recorded",
        True,
        kind="observation",
        sameAsPlan82=sorted(os.path.basename(f) for f in files)
        == sorted("clipnode" + ext for ext in CLIP_NODE_EXTENSIONS),
        saw=sorted(os.path.basename(f) for f in files),
    )
    if not isinstance(result.get("path"), str):
        return
    ids["setupFile"] = result["path"]
    note_before = (
        _result_of(await run_call(session, ctx, "d_get_note")).get("attributes") or {}
    ).get("note")
    noted = await run_call(session, ctx, "d_set_note")
    note_mid = (_result_of(await run_call(session, ctx, "d_get_note")).get("attributes") or {}).get(
        "note"
    )
    loaded = await run_call(session, ctx, "d_load_setup")
    load_result = _result_of(loaded)
    note_after = (
        _result_of(await run_call(session, ctx, "d_get_note")).get("attributes") or {}
    ).get("note")
    observe(
        ctx,
        "d_load_setup_of_the_clip_nodes_main_file_into_the_same_node",
        opOk=loaded.get("ok"),
        errorCode=loaded.get("errorCode"),
        error=loaded.get("error"),
        noChange=load_result.get("noChange"),
        changedKeys=load_result.get("changedKeys"),
        changed=load_result.get("changed"),
        loadReturned=load_result.get("loadReturned"),
        noteSet=noted.get("ok"),
        noteBeforeSave=note_before,
        noteBeforeLoad=note_mid,
        noteAfterLoad=note_after,
    )
    rec(
        ctx,
        "d6_the_load_ran_and_its_outcome_is_recorded_whatever_it_was",
        loaded.get("ok") is True or loaded.get("ok") is False,
        loadOk=loaded.get("ok"),
    )
    for step, env in (("load", loaded), ("note", noted)):
        group = group_of(env)
        if group is None:
            continue
        undone = await call(session, ctx, "journal.revert", {"group": group})
        rec(
            ctx,
            f"d7_the_{step}_is_reverted_or_refused_cleanly",
            undone.get("ok") is True or undone.get("errorCode") in ("CONFLICT",),
            revertOk=undone.get("ok"),
            errorCode=undone.get("errorCode"),
            error=undone.get("error"),
        )


async def stage_d(ctx: Ctx) -> None:
    if "uid" not in ctx.ids:
        rec(ctx, "d_stage_skipped_there_is_no_batch_group", False)
        return
    async with server_session(ctx, "pf7e-d") as session:
        if not await build_render_group(session, ctx):
            return
        await judge_render_handle(session, ctx)
        await judge_clip_setup(session, ctx)


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
    envelopes = [
        e["env"] for e in ctx.results.get("_envelopes", []) if isinstance(e.get("env"), dict)
    ]
    rec(
        ctx,
        "z_every_envelope_has_notices_a_list_and_none_has_one_here",
        bool(envelopes) and all(e.get("notices") == [] for e in envelopes),
        counted=len(envelopes),
        withNotices=[e.get("notices") for e in envelopes if e.get("notices") != []][:3],
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "pf7e_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "pf7e_left")
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
    ctx.results["filesLeftOnDisk"] = str(Path(ctx.ids.get("filesDir", "")))
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    ctx.ids["prefix"] = PREFIX
    os.environ[CKPT_ENV] = ckpt_library(ctx)
    ctx.results["env"] = {CKPT_ENV: os.environ[CKPT_ENV]}
    try:
        await guarded(ctx, "prepare", prepare)
        if "seqReelWt" in ctx.ids and "clipWt" in ctx.ids:
            for name, stage in (("a", stage_a), ("b", stage_b), ("c", stage_c), ("d", stage_d)):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "pf7e_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------


def placeholder_ids() -> dict[str, Any]:
    return {
        "wt": "/wt/x",
        "rin": 14,
        "group": "g_dry",
        "handle": "ab" * 16,
        "uid": "uid-gb",
        "clipName": "clip1",
        "writerValues": {"media_path": "/dry/run/", "range_start": 1, "range_end": RENDER_FRAMES},
        "renderGroup": "g_dry_render",
        "setupPath": "/dry/run/clipnode",
        "setupFile": str(Path(__file__).resolve()),  # only has to exist for the precheck
    }


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    policy = Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
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
        validation = validate_op_args(op, build(dict(ids)))
        if validation.ok:
            checked = dict(validation.value or {})
            apply_param_defaults(op, checked)
            apply_limit_default(op, checked)
            if op.precheck is not None:
                refusal = op.precheck(checked, policy)
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
    journal_dir = OUTPUT_DIR / f"journal_pf7e_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"fix7_small_items_e2e_{ts}.json",
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
