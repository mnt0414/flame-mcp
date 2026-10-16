"""Shared parts of the scenario e2e (scripts/p3_5_scenarios_e2e.py): the calls made through
the MCP surface (one table the run and `--dry-run` read), the material (`prepare`) and the
transport scaffolding -- which only READS STATE BACK INDEPENDENTLY of the ops under test. This run
uses transport for nothing else than the material (the reel group, the library, the clips, the
sequences) and the cleanup: no `eval.run`, no write by transport inside a scenario. The stages are
in p3_5_scenario1.py (scenario 1: replace with timecode match), p3_5_scenario2.py (scenario 2: a
shot with handles) and p3_5_library.py (what the library protection refuses); the driver, the
cleanup and `--dry-run` are in p3_5_scenarios_e2e.py."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import MEDIA_DIR, _br_dict  # noqa: E402
from p2_2a_journal_e2e import Ctx, do, rec, tx  # noqa: E402
from p3_1a_read_e2e import PRELUDE, REEL_CLIPS_CODE, STRUCT_CODE, UID_CODE, fill  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    OUTPUT_DIR,
    PREPARE_CODE,
    SEG_FACTS_CODE,
    STASH_CODE,
    Results,
    ckpt_library,
    err_text,
    expect_error,
    fingerprint,
    media_row,
    name_of,
    names_of,
    seg_at,
    sel,
    stash_uids,
    track_rows,
    tx_children,
    tx_find_uid,
    tx_seg,
    tx_stash,
    tx_struct,
    uids_of,
    written_groups,
)
from p3_3_e2e_common import (  # noqa: E402
    CLIP_FACTS_CODE,
    FX_ROWS_CODE,
    HANDLE,
    LETTERBOX,
    MAKE_SEQ_TEMPLATE,
    MEASURED_EXTENSIONS,
    P2_MEDIA_DIR,
    fx_index_order,
    fx_types,
    fx_values,
    make_seq,
    restored_wt,
    revert,
    state_snapshot,
    tx_clip,
    tx_fx,
)

PREFIX: Final[str] = "P35"
SOURCE_CLIP: Final[str] = "p2_src_tc1h"
REPLACEMENT_CLIP: Final[str] = "p2_rep_tc1h_plus10"
REPLACEMENT_FILE: Final[Path] = P2_MEDIA_DIR / f"{REPLACEMENT_CLIP}.mov"
"""What scenario 1 brings in with `mediahub.import`: 48 frames from 01:00:00:10 (the frame the
TC files of scripts/make_p2_tc_media.sh start at: p2_src_tc1h starts at 01:00:00:00)."""
LATE_BY: Final[int] = 10
"""How many frames later the replacement's timecode starts than the source's."""
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("clip1", MEDIA_DIR / "clip1.mov"),
    ("clip2", MEDIA_DIR / "clip2.mov"),
    ("p2_len72", P2_MEDIA_DIR / "p2_len72.mov"),
    (SOURCE_CLIP, P2_MEDIA_DIR / f"{SOURCE_CLIP}.mov"),
)
"""The material put in by transport (the Desktop reel `SRC`). The replacement clip is NOT among
them: the run imports it through the op."""
WANT_HANDLE: Final[int] = 6
"""The handle length scenario 2 hands the shot on with (frames per side; the soft handle is
`HANDLE` = 10, so the request fits)."""

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def declare(call_id: str, op: str, sample: dict[str, Any]) -> None:
    """`CALLS[call_id] = (op, build)`: `build(ids)` answers `ids["args"]` when a stage supplies the
    arguments and `sample` (a well-formed set, for `--dry-run`) otherwise."""

    def build(ids: dict[str, Any]) -> dict[str, Any]:
        args = ids.get("args")
        return dict(args) if isinstance(args, dict) else dict(sample)

    CALLS[call_id] = (op, build)


_WT = sel("/wt/x")
_SEG = seg_at(_WT, 1, 10)
_DEST = sel("/wt/d")
_SRC = sel("/wt/c")

declare("a_library_list", "library.list", {"limit": 500})
declare("a_list_children", "desktop.list_children", {"target": _DEST})
declare("a_import", "mediahub.import", {"paths": ["/x/clip.mov"], "destination": _DEST})
declare("a_find", "desktop.find", {"name": REPLACEMENT_CLIP, "parent": _DEST})
declare("a_list_segments", "timeline.list_segments", {"target": _WT, "version": 0, "track": 1})
declare("a_info", "segment.info", {"target": _SEG})
declare("a_copy_out", "desktop.copy", {"sources": [_SRC], "destination": _DEST})
declare(
    "a_plan_replace",
    "plan.run",
    {"steps": [{"op": "segment.replace", "args": {"target": _SEG, "source": _SRC}}]},
)
declare("a_slip", "segment.slip", {"target": _SEG, "offset": LATE_BY})
declare("a_create_effect", "segment.create_effect", {"target": _SEG, "type": LETTERBOX})
declare(
    "a_save_setup",
    "segment.effect_save_setup",
    {"target": _SEG, "type": LETTERBOX, "path": "/x/setup"},
)
declare(
    "a_set_effect",
    "segment.set_effect",
    {"target": _SEG, "type": LETTERBOX, "values": {"ratio": 2}},
)
declare(
    "a_load_setup",
    "segment.effect_load_setup",
    {"target": _SEG, "type": LETTERBOX, "path": "/x/setup.letterbox_node"},
)
declare("n_move_out", "desktop.move", {"sources": [_SRC], "destination": _DEST})
declare("n_delete", "desktop.delete", {"targets": [_SRC]})
declare("b_list_segments", "timeline.list_segments", {"target": _WT, "version": 0, "track": 1})
declare("b_info", "segment.info", {"target": _SEG})
declare("b_copy_out", "segment.copy_to_media_panel", {"target": _SEG, "destination": _DEST})
declare(
    "b_trim_head",
    "segment.trim",
    {"target": _SEG, "edge": "head", "offset": -WANT_HANDLE, "ripple": True},
)
declare(
    "b_trim_tail",
    "segment.trim",
    {"target": _SEG, "edge": "tail", "offset": -WANT_HANDLE, "ripple": True},
)
declare("b_rename", "desktop.rename", {"target": _SRC, "name": "P35_x"})


async def run_call(
    session: Any, ctx: Ctx, call_id: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The call `call_id` of the table with `args` (what the stage built; the table's sample when
    none): the answer, its journal group recorded for the run's own check."""
    op, build = CALLS[call_id]
    env = await do(session, ctx, op, args if args is not None else build({}))
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


# -- transport scaffolding (never through the op under test) ------------------------------------

TEMPLATES: Final[dict[str, str]] = {
    "prepare": PREPARE_CODE,
    "make_seq": MAKE_SEQ_TEMPLATE,
    "fx_rows": FX_ROWS_CODE,
    "clip_facts": CLIP_FACTS_CODE,
    "seg_facts": SEG_FACTS_CODE,
    "stash": STASH_CODE,
    "struct": STRUCT_CODE,
    "uid": UID_CODE,
    "reel_clips": REEL_CLIPS_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def prepare(ctx: Ctx) -> None:
    """The reel group (SRC / SEQ / CCP), the library with a reel, and the clips of `CLIP_FILES`
    on SRC. The replacement material is brought in by the run itself, through `mediahub.import`."""
    missing = [
        str(p) for _n, p in (*CLIP_FILES, (REPLACEMENT_CLIP, REPLACEMENT_FILE)) if not p.is_file()
    ]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    files_dir = OUTPUT_DIR / f"files_p35_{ctx.ts}"
    files_dir.mkdir(parents=True, exist_ok=True)
    ids = ctx.ids
    ids.update(filesDir=str(files_dir))
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
        label="p35_prepare",
        timeout_s=240.0,
    )
    rec(ctx, "prepare_reel_group_library_and_the_clips", made.ok, resp=_br_dict(made))
    if not (made.ok and isinstance(made.result, dict)):
        return
    result = made.result
    ids.update(
        rgWt=result["rgWt"],
        srcWt=result["srcWt"],
        seqReelWt=result["seqReelWt"],
        ccpWt=result["ccpWt"],
        libWt=result["libWt"],
        libReelWt=result["libReelWt"],
        fps=result["fps"],
        width=result["width"],
        height=result["height"],
        clipWt={n: v["wt"] for n, v in result["clips"].items()},
    )
    rec(
        ctx,
        "prepare_every_clip_was_imported",
        set(result["clips"]) == {n for n, _p in CLIP_FILES},
        clips=sorted(result["clips"]),
    )


def ops_via_plan(results: Results) -> list[str]:
    """The ops that ran as ok children of a `plan.run` (`ops_used` lists direct calls only)."""
    names: set[str] = set()
    for entry in results.get("_envelopes", []):
        env = entry.get("env") if entry.get("tool") == "flame_do" else None
        if not isinstance(env, dict) or (env.get("execution") or {}).get("op") != "plan.run":
            continue
        for step in (env.get("result") or {}).get("steps") or []:
            if (
                isinstance(step, dict)
                and step.get("ok") is True
                and isinstance(step.get("op"), str)
            ):
                names.add(step["op"])
    return sorted(names)


def segment_row(items: list[dict[str, Any]], record_in: int) -> dict[str, Any] | None:
    """The item of a `timeline.list_segments` page whose record_in is `record_in`."""
    for item in items:
        record = item.get("recordIn")
        frame = record.get("frame") if isinstance(record, dict) else record
        if frame == record_in:
            return item
    return None


__all__ = [
    "CALLS",
    "CLIP_FILES",
    "HANDLE",
    "LATE_BY",
    "LETTERBOX",
    "MEASURED_EXTENSIONS",
    "OUTPUT_DIR",
    "PREFIX",
    "REPLACEMENT_CLIP",
    "REPLACEMENT_FILE",
    "SOURCE_CLIP",
    "TEMPLATES",
    "WANT_HANDLE",
    "Ctx",
    "Results",
    "ckpt_library",
    "err_text",
    "expect_error",
    "fingerprint",
    "fx_index_order",
    "fx_types",
    "fx_values",
    "make_seq",
    "media_row",
    "name_of",
    "names_of",
    "ops_via_plan",
    "prepare",
    "restored_wt",
    "revert",
    "run_call",
    "seg_at",
    "segment_row",
    "sel",
    "stash_uids",
    "state_snapshot",
    "track_rows",
    "tx_children",
    "tx_clip",
    "tx_find_uid",
    "tx_fx",
    "tx_seg",
    "tx_stash",
    "tx_struct",
    "uids_of",
]
