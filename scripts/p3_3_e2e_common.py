"""Shared parts of the segment-edits real-Flame e2e (scripts/p3_3_segment_e2e.py): the calls made
through the MCP surface (one table the run and `--dry-run` read), the transport scaffolding -- which
READS STATE BACK INDEPENDENTLY of the ops under test -- and the material (`prepare`). The stages are
in p3_3_e2e_edit.py (slip, replace), p3_3_e2e_effects.py (effects, setup files) and
p3_3_e2e_extract.py (copy_to_media_panel / match, plan.run); the driver, the policy stage, the
cleanup and `--dry-run` are in p3_3_segment_e2e.py. The helpers of p3_2a_e2e_common.py are reused
under the prefix `P33`."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import MEDIA_DIR, _br_dict, _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, do, rec, tx  # noqa: E402
from p3_1a_read_e2e import (  # noqa: E402
    BIT_DEPTH_CODE,
    BIT_DEPTH_RESULT,
    COPY_CODE,
    PRELUDE,
    REEL_CLIPS_CODE,
    STRUCT_CODE,
    UID_CODE,
    bit_depth_ok,
    fill,
    is_error,
    tx_val,
)
from p3_1b_e2e_common import BATCH_CODE, CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    DELETE_UID_CODE,
    OUTPUT_DIR,
    PREPARE_CODE,
    STASH_CODE,
    TRANSPORT_TRIM_CODE,
    Results,
    check_group,
    checkpoint_records,
    ckpt_library,
    err_text,
    expect_error,
    fingerprint,
    groups_now,
    intent_of,
    name_of,
    outcome_of,
    seg_at,
    sel,
    stash_uids,
    track_rows,
    tx_seg,
    tx_stash,
    tx_struct,
    written_groups,
)

PREFIX: Final[str] = "P33"
P2_MEDIA_DIR: Final[Path] = MEDIA_DIR / "p2"
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("clip1", MEDIA_DIR / "clip1.mov"),
    ("clip2", MEDIA_DIR / "clip2.mov"),
    ("clip3", MEDIA_DIR / "clip3.mov"),
    ("p2_len12", P2_MEDIA_DIR / "p2_len12.mov"),
    ("p2_len48", P2_MEDIA_DIR / "p2_len48.mov"),
    ("p2_len72", P2_MEDIA_DIR / "p2_len72.mov"),
    ("p2_src_tc1h", P2_MEDIA_DIR / "p2_src_tc1h.mov"),
    ("p2_rep_tc1h", P2_MEDIA_DIR / "p2_rep_tc1h.mov"),
    ("p2_rep_tc1h_plus10", P2_MEDIA_DIR / "p2_rep_tc1h_plus10.mov"),
    ("p2_rep_tc2h", P2_MEDIA_DIR / "p2_rep_tc2h.mov"),
)
"""The material (scripts/make_p2_media.sh, scripts/make_p2_tc_media.sh): p2_lenN has N frames; the
four TC files have 48 frames starting at 01:00:00:00 (src, rep_tc1h), 01:00:00:10 (plus10) and
02:00:00:00 (tc2h)."""
HANDLE: Final[int] = 10
"""head and tail left on p2_len72 (and p2_src_tc1h) by the two trims of `make_seq`."""
LETTERBOX: Final[str] = "Burn-in Letterbox"
MEASURED_EXTENSIONS: Final[dict[str, tuple[str, ...]]] = {
    "Blur": (".blur_node",),
    "Resize": (".resize_node",),
    "Colour Correct": (".correct_node",),
    "2D Transform": (".2dtransform_node",),
    "Flip": (".flip_node",),
    "Timewarp": (".timewarp_node",),
    LETTERBOX: (".letterbox", ".letterbox_node"),
}
"""The files `fx.save_setup(<path without an extension>)` wrote per type (measured)."""
UNMEASURED_TYPES: Final[tuple[str, ...]] = ("Action", "Source Look", "Source Image")
"""Effect types whose setup files were not measured: saved here and RECORDED, not judged."""

MAKE_SEQ_TEMPLATE: Final[str] = "\n".join(
    [
        "_reel = flame.find_by_wiretap_node_id(@REEL@)",
        *BIT_DEPTH_CODE,
        "_kw = {'bit_depth': _bd} if _bd is not None else {}",
        "_seq = _reel.create_sequence(name=@NAME@, video_tracks=2, frame_rate=@FPS@,",
        "                             width=@WIDTH@, height=@HEIGHT@, **_kw)",
        "_notes = []",
        "for _track, _clip_wt, _rel in @PUTS@:",
        "    _clip = flame.find_by_wiretap_node_id(_clip_wt)",
        "    _tr = _seq.versions[0].tracks[_track]",
        "    _notes.append(repr(_seq.overwrite(_clip, flame.PyTime(_rel), _tr)))",
        "for _track, _head, _tail in @TRIMS@:",
        "    _t = _seq.versions[0].tracks[_track]",
        "    _s = [_x for _x in _t.segments if _x.type != 'Gap'][0]",
        "    _notes.append(repr(_s.trim_head(_head)))",
        "    _s = [_x for _x in _t.segments if _x.type != 'Gap'][0]",
        "    _notes.append(repr(_s.trim_tail(_tail)))",
        "for _track, _src, _fx, _attrs in @FXS@:",
        "    _t = _seq.versions[0].tracks[_track]",
        "    _s = [_x for _x in _t.segments if _x.type != 'Gap' and _x.source_name == _src][0]",
        "    _f = _s.create_effect(_fx)",
        "    for _k, _val in _attrs.items():",
        "        _notes.append(repr(getattr(_f, _k).set_value(_val)))",
        "_result = {'wt': _seq.get_wiretap_node_id(), 'uid': _v(_seq.uid),",
        "           'name': _v(_seq.name), 'notes': _notes, " + BIT_DEPTH_RESULT + "}",
    ]
)
"""What makes a sequence of two tracks: clips put with `overwrite(clip, PyTime(rel), track)`, then
the first media segment of a track trimmed (head, then tail; without ripple, so a Gap is left before
it), then effects created on the segment of a source (the in-process test swaps in one the fake can
run)."""

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one)

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

declare("s_slip", "segment.slip", {"target": _SEG, "offset": 3})
declare("s_slip_refused", "segment.slip", {"target": _SEG, "offset": 500})
declare("s_slip_bad_offset", "segment.slip", {"target": _SEG, "offset": 0})
declare("s_slip_in_a_library", "segment.slip", {"target": _SEG, "offset": 1})
declare("r_replace", "segment.replace", {"target": _SEG, "source": sel("/wt/c")})
declare(
    "r_replace_media_and_fx",
    "segment.replace",
    {"target": _SEG, "source": sel("/wt/c"), "mode": "media_and_fx"},
)
declare("r_replace_too_short", "segment.replace", {"target": _SEG, "source": sel("/wt/c")})
declare("r_replace_refused", "segment.replace", {"target": _SEG, "source": sel("/wt/s")})
declare("r_replace_slip", "segment.slip", {"target": _SEG, "offset": 10})
declare("e_create", "segment.create_effect", {"target": _SEG, "type": "Blur"})
declare("e_create_refused", "segment.create_effect", {"target": _SEG, "type": "Blur"})
declare("e_delete", "segment.delete_effect", {"target": _SEG, "type": "Blur"})
declare(
    "e_set", "segment.set_effect", {"target": _SEG, "type": LETTERBOX, "values": {"ratio": 1.5}}
)
declare(
    "e_set_refused",
    "segment.set_effect",
    {"target": _SEG, "type": LETTERBOX, "values": {"ratio": 1000}},
)
declare("e_save", "segment.effect_save_setup", {"target": _SEG, "type": "Blur", "path": "/x/setup"})
declare(
    "e_load",
    "segment.effect_load_setup",
    {"target": _SEG, "type": "Blur", "path": "/x/setup.blur_node"},
)
declare("x_copy_out", "segment.copy_to_media_panel", {"target": _SEG, "destination": _DEST})
declare("x_match", "segment.match", {"target": _SEG, "destination": _DEST})
declare(
    "x_match_preserve_handle",
    "segment.match",
    {"target": _SEG, "destination": _DEST, "preserve_handle": True},
)
declare(
    "x_match_with_effects",
    "segment.match",
    {"target": _SEG, "destination": _DEST, "include_timeline_fx": True},
)
declare(
    "x_into_a_batch_reel", "segment.copy_to_media_panel", {"target": _SEG, "destination": _DEST}
)
declare(
    "p_plan", "plan.run", {"steps": [{"op": "segment.slip", "args": {"target": _SEG, "offset": 1}}]}
)
declare(
    "p_plan_with_a_match",
    "plan.run",
    {"steps": [{"op": "segment.match", "args": {"target": _SEG, "destination": _DEST}}]},
)
declare(
    "b_create_sequence",
    "timeline.create_sequence",
    {
        "parent": sel("/wt/r"),
        "name": "P33_x_BD",
        "frame_rate": "25 fps",
        "width": 1920,
        "height": 1080,
        "bit_depth": 10,
    },
)
declare("b_list_effects", "segment.list_effects", {"target": _SEG})
declare("b_create_resize", "segment.create_effect", {"target": _SEG, "type": "Resize"})
declare("b_delete_resize", "segment.delete_effect", {"target": _SEG, "type": "Resize"})
declare("y_readonly_slip", "segment.slip", {"target": _SEG, "offset": 1})
declare("y_readonly_replace", "segment.replace", {"target": _SEG, "source": sel("/wt/c")})
declare("y_readonly_create_effect", "segment.create_effect", {"target": _SEG, "type": "Blur"})
declare("y_readonly_match", "segment.match", {"target": _SEG, "destination": _DEST})
declare(
    "y_readonly_save", "segment.effect_save_setup", {"target": _SEG, "type": "Blur", "path": "/x/s"}
)

# -- transport scaffolding (never through the op under test)

FX_ROWS_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "def _tj(_x):",
        "    _x = _v(_x)",
        "    if isinstance(_x, (list, tuple)):",
        "        return [_tj(_i) for _i in _x]",
        "    if _x is None or isinstance(_x, (str, int, float, bool)):",
        "        return _x",
        "    return repr(_x)",
        "_rows = []",
        "if _hit:",
        "    for _f in list(_hit[0].effects):",
        "        _vals = {}",
        "        _cands = {}",
        "        for _k in list(_f.attributes):",
        "            _a = getattr(_f, _k)",
        "            _vals[_k] = _g(lambda: _tj(_a))",
        "            _cands[_k] = _g(lambda: _tj(_a.values))",
        "        _rows.append({'type': _v(_f.type), 'values': _vals, 'candidates': _cands})",
        "_result = None if not _hit else {'effects': _rows,",
        "    'effectTypes': _g(lambda: [_v(_t2) for _t2 in list(_hit[0].effect_types)])}",
    ]
)

CLIP_FACTS_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "def _mk(_x):",
        "    _x = _v(_x)",
        "    return None if _x is None else (_x.frame if type(_x).__name__ == 'PyTime' else _x)",
        "if _o is None:",
        "    _result = None",
        "else:",
        "    _segs = []",
        "    for _t in list(list(_o.versions)[0].tracks):",
        "        for _s in list(_t.segments):",
        "            _segs.append({'type': _s.type, 'src': _s.source_name,",
        "                          'rin': _s.record_in.frame, 'rout': _s.record_out.frame,",
        "                          'sin': _s.source_in.frame, 'sout': _s.source_out.frame,",
        "                          'head': _g(lambda: _v(_s.head)),",
        "                          'tail': _g(lambda: _v(_s.tail)), 'fx': _fxrow(_s)})",
        "    _result = {'type': type(_o).__name__, 'name': _v(_o.name), 'uid': _v(_o.uid),",
        "               'duration': _g(lambda: _mk(_o.duration)),",
        "               'startFrame': _g(lambda: _mk(_o.start_frame)),",
        "               'inMark': _g(lambda: _mk(_o.in_mark)),",
        "               'outMark': _g(lambda: _mk(_o.out_mark)), 'segments': _segs}",
    ]
)

RENAME_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_ret = _o.name.set_value(@NAME@)",
        "_result = [repr(_ret), _v(_o.name)]",
    ]
)

CLIP_DEPTH_CODE: Final[str] = "\n".join(
    [
        "_c = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _c is None else _g(lambda: _v(_c.bit_depth))",
    ]
)

PUT_CODE: Final[str] = "\n".join(
    [
        "_seq = flame.find_by_wiretap_node_id(@SEQ@)",
        "_clip = flame.find_by_wiretap_node_id(@CLIP@)",
        "_before = _g(lambda: _v(_seq.bit_depth))",
        "_put = _seq.overwrite(_clip, flame.PyTime(1), _seq.versions[0].tracks[0])",
        "_result = {'put': repr(_put), 'seqBitDepthBefore': _before,",
        "           'seqBitDepth': _g(lambda: _v(_seq.bit_depth)),",
        "           'clipBitDepth': _g(lambda: _v(_clip.bit_depth))}",
    ]
)

SEGMENT_DEPTH_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[0]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "_result = None if not _hit else _g(lambda: _v(_hit[0].source_bit_depth))",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "prepare": PREPARE_CODE,
    "make_seq": MAKE_SEQ_TEMPLATE,
    "fx_rows": FX_ROWS_CODE,
    "clip_facts": CLIP_FACTS_CODE,
    "rename": RENAME_CODE,
    "clip_depth": CLIP_DEPTH_CODE,
    "put_clip": PUT_CODE,
    "segment_depth": SEGMENT_DEPTH_CODE,
    "transport_trim": TRANSPORT_TRIM_CODE,
    "stash": STASH_CODE,
    "copy": COPY_CODE,
    "struct": STRUCT_CODE,
    "uid": UID_CODE,
    "reel_clips": REEL_CLIPS_CODE,
    "batch": BATCH_CODE,
    "delete_uid": DELETE_UID_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def tx_fx(ctx: Ctx, wiretap_id: str, track: int, record_in: int) -> dict[str, Any] | None:
    """The effects of one segment and the candidates of their attributes, read by transport."""
    code = fill(FX_ROWS_CODE, WT=wiretap_id, TRACK=track, RECORD_IN=record_in)
    got = await tx_val(ctx, code, "p33_fx_rows")
    return None if is_error(got) or not isinstance(got, dict) else got


async def tx_clip(ctx: Ctx, wiretap_id: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(CLIP_FACTS_CODE, WT=wiretap_id), "p33_clip_facts")
    return None if is_error(got) or not isinstance(got, dict) else got


def fx_types(rows: dict[str, Any] | None) -> list[str]:
    return sorted(fx_index_order(rows))


def fx_index_order(rows: dict[str, Any] | None) -> list[str]:
    """The effect types in the order `segment.effects` lists them (the order the ops report)."""
    return [str(e["type"]) for e in (rows or {}).get("effects") or []]


def fx_values(rows: dict[str, Any] | None, fx_type: str) -> dict[str, Any] | None:
    for entry in (rows or {}).get("effects") or []:
        if entry["type"] == fx_type:
            values = entry["values"]
            return dict(values) if isinstance(values, dict) else None
    return None


def fx_candidates(rows: dict[str, Any] | None, fx_type: str) -> dict[str, Any]:
    for entry in (rows or {}).get("effects") or []:
        if entry["type"] == fx_type:
            return dict(entry["candidates"])
    return {}


async def run_call(
    session: Any, ctx: Ctx, call_id: str, args: dict[str, Any], *, track: bool = True
) -> dict[str, Any]:
    """The call `call_id` of the table with `args` (what the stage built)."""
    op, _build = CALLS[call_id]
    env = await do(session, ctx, op, args)
    group = (env.get("journal") or {}).get("group")
    if track and isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


async def revert(session: Any, ctx: Ctx, env: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out = await do(
        session,
        ctx,
        "journal.revert",
        {"group": (env.get("journal") or {}).get("group"), **extra},
    )
    group = (out.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": "journal.revert", "group": group})
    return out


def restored_wt(env: dict[str, Any]) -> str | None:
    """The new wiretap id of a sequence a revert put back (it always has another one)."""
    selector = (_result_of(env).get("restored") or {}).get("selector") or {}
    value = selector.get("id")
    return value if isinstance(value, str) else None


def op_time(value: Any) -> int | None:
    """The frame of a PyTime an op answers as {timecode, frame}."""
    return value.get("frame") if isinstance(value, dict) else None


async def state_snapshot(ctx: Ctx) -> dict[str, Any]:
    """What 'nothing happened' is judged against."""
    return {
        "groups": groups_now(ctx),
        "checkpoints": len(checkpoint_records(ctx)),
        "stash": stash_uids(await tx_stash(ctx)),
    }


# -- material


async def make_seq(
    ctx: Ctx,
    tail: str,
    puts: tuple[tuple[int, str, int], ...],
    *,
    trims: tuple[tuple[int, int, int], ...] = (),
    fxs: tuple[tuple[int, str, str, dict[str, Any]], ...] = (),
) -> dict[str, Any] | None:
    """A sequence of this run (`P33_<ts>_<tail>`) in the SEQ reel: (track, clip, relative start)
    puts, (track, head, tail) trims of the first media segment of a track, (track, source, effect
    type, attributes to set) effects."""
    ids = ctx.ids
    code = fill(
        MAKE_SEQ_TEMPLATE,
        REEL=ids["seqReelWt"],
        NAME=name_of(ctx, tail),
        FPS=ids["fps"],
        WIDTH=ids["width"],
        HEIGHT=ids["height"],
        PUTS=[[track, ids["clipWt"][clip], rel] for track, clip, rel in puts],
        TRIMS=[list(t) for t in trims],
        FXS=[[track, src, fx, attrs] for track, src, fx, attrs in fxs],
    )
    br = await tx(ctx, PRELUDE + "\n" + code, label=f"p33_make_{tail}", timeout_s=120.0)
    rec(ctx, f"prepare_{tail}_is_made", br.ok and bit_depth_ok(br.result), resp=_br_dict(br))
    return br.result if br.ok and isinstance(br.result, dict) else None


async def prepare(ctx: Ctx) -> None:
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    files_dir = OUTPUT_DIR / f"files_p33_{ctx.ts}"
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
        label="p33_prepare",
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


__all__ = [
    "BATCH_CODE",
    "CALLS",
    "CLIP_DEPTH_CODE",
    "CLIP_FILES",
    "HANDLE",
    "LETTERBOX",
    "MEASURED_EXTENSIONS",
    "OUTPUT_DIR",
    "PREFIX",
    "PUT_CODE",
    "RENAME_CODE",
    "SEGMENT_DEPTH_CODE",
    "TEMPLATES",
    "UNMEASURED_TYPES",
    "Ctx",
    "Results",
    "check_group",
    "checkpoint_records",
    "ckpt_library",
    "err_text",
    "expect_error",
    "fingerprint",
    "fx_candidates",
    "fx_index_order",
    "fx_types",
    "fx_values",
    "groups_now",
    "intent_of",
    "make_seq",
    "op_time",
    "outcome_of",
    "prepare",
    "restored_wt",
    "revert",
    "run_call",
    "seg_at",
    "sel",
    "stash_uids",
    "state_snapshot",
    "track_rows",
    "tx_clip",
    "tx_fx",
    "tx_seg",
    "tx_stash",
    "tx_struct",
]
