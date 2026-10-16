"""Shared parts of the checkpoint / trim real-Flame e2e (scripts/p3_2a_checkpoint_e2e.py): the calls
made through the MCP surface, the transport scaffolding (which READS STATE BACK INDEPENDENTLY of the
ops under test), the helpers the stages use and the material (`prepare`). The stages are in
p3_2a_e2e_stages.py and p3_2a_e2e_trim.py; the driver, the pixel / plan / policy stages, the journal
stage, the cleanup and `--dry-run` are in p3_2a_checkpoint_e2e.py."""

from __future__ import annotations

import copy
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import e2e_guard  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    MEDIA_DIR,
    _br_dict,
    _result_of,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do,
    rec,
    tx,
)
from p2_4_common import (  # noqa: E402
    journal_records,
    journal_states,
    observe,
)
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

from flame_mcp.journal import Journal, new_id, now_iso  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
P2_MEDIA_DIR: Final[Path] = MEDIA_DIR / "p2"
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("clip1", MEDIA_DIR / "clip1.mov"),
    ("clip2", MEDIA_DIR / "clip2.mov"),
    ("p2_len48", P2_MEDIA_DIR / "p2_len48.mov"),
    ("p2_len72", P2_MEDIA_DIR / "p2_len72.mov"),
)
CKPT_ENV: Final[str] = "FLAME_MCP_CHECKPOINT_LIBRARY"
FLAME_VERSION: Final[str] = e2e_guard.e2e_flame_version()
PRESET_REL: Final[str] = "export/presets/flame/file_sequence/Tiff/Tiff (16-bit LZW).xml"
PRESET_SOURCE: Final[Path] = Path("/opt/Autodesk/presets") / FLAME_VERSION / PRESET_REL
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed", "diverged"}
)
S1_PUTS: Final[tuple[tuple[int, str, int], ...]] = (
    (0, "clip1", 1),
    (0, "clip2", 49),
    (0, "p2_len48", 73),
    (1, "p2_len72", 1),
)
"""(track, clip, relative start) of S1 (`overwrite(clip, PyTime(n), track)`: a PyTime of an integer
only is relative, 1 = the first frame). p2_len72 on track 1 is then trimmed to head 10 /
tail 10; clip2 on track 0 gets a Burn-in Letterbox with ratio 1.5."""
TRIM_HANDLE: Final[int] = 10
LETTERBOX_RATIO: Final[float] = 1.5
LETTERBOX_TYPE: Final[str] = "Burn-in Letterbox"
FP_ATTR: Final[tuple[str, Any]] = ("comment", "p32a comment")
"""The segment attribute the fingerprint is shown NOT to see (tests/test_p3_2a_e2e_inprocess.py
swaps in one the fake has)."""
MAKE_S1_TEMPLATE: Final[str] = "\n".join(
    [
        "_reel = flame.find_by_wiretap_node_id(@REEL@)",
        *BIT_DEPTH_CODE,
        "_kw = {'bit_depth': _bd} if _bd is not None else {}",
        "_seq = _reel.create_sequence(name=@NAME@, video_tracks=2, frame_rate=@FPS@,",
        "                             width=@WIDTH@, height=@HEIGHT@, **_kw)",
        "_puts = []",
        "for _track, _clip_wt, _rel in @PUTS@:",
        "    _clip = flame.find_by_wiretap_node_id(_clip_wt)",
        "    _tr = _seq.versions[0].tracks[_track]",
        "    _put = _seq.overwrite(_clip, flame.PyTime(_rel), _tr)",
        "    _puts.append([_track, _v(_clip.name), _rel, repr(_put)])",
        "_t1 = _seq.versions[0].tracks[1]",
        "_notes = []",
        "if @TRIM@:",
        "    _s = [_x for _x in _t1.segments if _x.type != 'Gap'][0]",
        "    _notes.append(repr(_s.trim_head(@HANDLE@)))",
        "    _s = [_x for _x in _t1.segments if _x.type != 'Gap'][0]",
        "    _notes.append(repr(_s.trim_tail(@HANDLE@)))",
        "if @FX@:",
        "    _t0 = _seq.versions[0].tracks[0]",
        "    _s = [_x for _x in _t0.segments if _x.type != 'Gap' and _x.source_name == 'clip2'][0]",
        f"    _f = _s.create_effect({LETTERBOX_TYPE!r})",
        "    _notes.append(repr(_f.mode.set_value('Custom')))",
        "    _notes.append(repr(_f.ratio.set_value(@RATIO@)))",
        "_result = {'wt': _seq.get_wiretap_node_id(), 'uid': _v(_seq.uid), 'puts': _puts,",
        "           'name': _v(_seq.name), 'notes': _notes, " + BIT_DEPTH_RESULT + "}",
    ]
)
"""What makes S1 and the sequences like it (the in-process test swaps in one the fake can run)."""

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one)

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def call_for(call_id: str, op: str) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Any]:
    def register(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


def sel(wiretap_id: str) -> dict[str, Any]:
    return {"kind": "wiretap", "id": wiretap_id}


def seg_at(
    parent: dict[str, Any], track: int, record_in: int, *, expect: str | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "segment",
        "parent": parent,
        "version": 0,
        "track": track,
        "record_in": record_in,
    }
    if expect is not None:
        out["expect_source_name"] = expect
    return out


def seg_idx(parent: dict[str, Any], track: int, index: int) -> dict[str, Any]:
    return {"kind": "segment", "parent": parent, "version": 0, "track": track, "index": index}


@call_for("a1_fingerprint", "timeline.fingerprint")
def _a1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": sel(ids["fWt"])}


@call_for("b1_checkpoint_first", "timeline.checkpoint")
def _b1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": sel(ids["s1Wt"]), "label": "p32a first"}


@call_for("b2_checkpoint_second", "timeline.checkpoint")
def _b2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": sel(ids["s2Wt"]), "label": "p32a second"}


@call_for("b3_checkpoint_in_a_library", "timeline.checkpoint")
def _b3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": sel(ids["s1cWt"])}


@call_for("b4_checkpoint_of_a_reel", "timeline.checkpoint")
def _b4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": sel(ids["seqReelWt"])}


@call_for("b5_list_checkpoints", "journal.list_checkpoints")
def _b5(ids: dict[str, Any]) -> dict[str, Any]:
    return {"limit": 50}


TRIM_CASES: Final[tuple[tuple[str, str, int, bool], ...]] = (
    ("c1_head_shorten", "head", 3, False),
    ("c2_head_extend", "head", -4, False),
    ("c3_tail_shorten", "tail", 3, False),
    ("c4_tail_extend", "tail", -4, False),
    ("c5_head_shorten_ripple", "head", 3, True),
    ("c6_head_extend_ripple", "head", -4, True),
    ("c7_tail_shorten_ripple", "tail", 3, True),
    ("c8_tail_extend_ripple", "tail", -4, True),
)
"""(call id, edge, offset, ripple): every edge x shorten / extend x ripple once, on the segment
p2_len72 (track 1, head 10 / tail 10 handles)."""
TRIM_REFUSALS: Final[tuple[tuple[str, str, int], ...]] = (
    ("c9_extension_beyond_the_handle", "head", -(TRIM_HANDLE + 1)),
    ("c10_trim_the_whole_length", "tail", 52),
)


def case_ids(ids: dict[str, Any], tag: str) -> dict[str, Any]:
    return ids.get("case", {}).get(tag) or {"wt": "/wt/" + tag, "rin": 10}


def trim_call(
    wiretap_id: str, track: int, record_in: int, edge: str, offset: int, ripple: bool
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "target": seg_at(sel(wiretap_id), track, record_in),
        "edge": edge,
        "offset": offset,
    }
    if ripple:
        args["ripple"] = True
    return args


def _register_trim_cases() -> None:
    for tag, edge, offset, ripple in TRIM_CASES:

        def build(
            ids: dict[str, Any],
            tag: str = tag,
            edge: str = edge,
            offset: int = offset,
            ripple: bool = ripple,
        ) -> dict[str, Any]:
            case = case_ids(ids, tag)
            return trim_call(case["wt"], 1, case["rin"], edge, offset, ripple)

        CALLS[tag] = ("segment.trim", build)
    for tag, edge, offset in TRIM_REFUSALS:

        def build_refusal(
            ids: dict[str, Any], tag: str = tag, edge: str = edge, offset: int = offset
        ) -> dict[str, Any]:
            case = case_ids(ids, tag)
            return trim_call(case["wt"], 1, case["rin"], edge, offset, False)

        CALLS[tag] = ("segment.trim", build_refusal)


_register_trim_cases()


@call_for("c11_trim_a_gap", "segment.trim")
def _c11(ids: dict[str, Any]) -> dict[str, Any]:
    case = case_ids(ids, "c11_trim_a_gap")
    return trim_call(case["wt"], 1, case.get("gapRin", 0), "head", 1, False)


@call_for("c12_trim_in_a_library", "segment.trim")
def _c12(ids: dict[str, Any]) -> dict[str, Any]:
    return trim_call(ids["s1cWt"], 1, 10, "head", 1, False)


@call_for("c13_bad_edge", "segment.trim")
def _c13(ids: dict[str, Any]) -> dict[str, Any]:
    return trim_call(ids["s1Wt"], 1, 10, "middle", 1, False)


@call_for("c14_zero_offset", "segment.trim")
def _c14(ids: dict[str, Any]) -> dict[str, Any]:
    return trim_call(ids["s1Wt"], 1, 10, "head", 0, False)


@call_for("d1_trim_for_revert", "segment.trim")
def _d1(ids: dict[str, Any]) -> dict[str, Any]:
    case = case_ids(ids, "d1_trim_for_revert")
    return trim_call(case["wt"], 0, case.get("rin", 48), "tail", 3, False)


@call_for("e1_trim_the_displayed_sequence", "segment.trim")
def _e1(ids: dict[str, Any]) -> dict[str, Any]:
    case = case_ids(ids, "e1_trim_the_displayed_sequence")
    return trim_call(case["wt"], 0, case.get("rin", 48), "tail", 3, False)


@call_for("e5_trim_in_front_of_the_shown_one", "segment.trim")
def _e5(ids: dict[str, Any]) -> dict[str, Any]:
    case = case_ids(ids, "e5_trim_in_front_of_the_shown_one")
    return trim_call(case["wt"], 0, case.get("rin", 48), "tail", 3, False)


@call_for("f1_trim_then_a_transport_edit", "segment.trim")
def _f1(ids: dict[str, Any]) -> dict[str, Any]:
    case = case_ids(ids, "f1_trim_then_a_transport_edit")
    return trim_call(case["wt"], 0, case.get("rin", 48), "tail", 3, False)


@call_for("g1_trim_for_pixels", "segment.trim")
def _g1(ids: dict[str, Any]) -> dict[str, Any]:
    case = case_ids(ids, "g1_trim_for_pixels")
    return trim_call(case["wt"], 0, case.get("rin", 48), "tail", 3, False)


@call_for("h1_trim_inside_a_clip", "segment.trim")
def _h1(ids: dict[str, Any]) -> dict[str, Any]:
    case = case_ids(ids, "h1_trim_inside_a_clip")
    return {
        "target": seg_idx(sel(case["wt"]), 0, 0),
        "edge": "tail",
        "offset": 3,
    }


@call_for("j1_plan_trim", "plan.run")
def _j1(ids: dict[str, Any]) -> dict[str, Any]:
    case = case_ids(ids, "j1_plan_trim")
    step = {"op": "segment.trim", "args": trim_call(case["wt"], 1, case["rin"], "head", 1, False)}
    return {"steps": [step]}


@call_for("j2_readonly_trim", "segment.trim")
def _j2(ids: dict[str, Any]) -> dict[str, Any]:
    return trim_call(ids["s1Wt"], 1, 10, "head", 1, False)


@call_for("j2_readonly_checkpoint", "timeline.checkpoint")
def _j2b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": sel(ids["s1Wt"])}


@call_for("j2_readonly_fingerprint", "timeline.fingerprint")
def _j2c(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": sel(ids["s1Wt"])}


# -- transport scaffolding (never through the op under test)

PREPARE_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_rg = _ws.desktop.create_reel_group(@RG@)",
        "_src = _rg.create_reel('SRC')",
        "_seqr = _rg.create_reel('SEQ')",
        "_ccp = _rg.create_reel('CCP')",
        "_lib = _ws.create_library(@LIB@)",
        "_lr = _lib.create_reel('LR')",
        "_clips = {}",
        "for _name, _p in @PATHS@:",
        "    _c = flame.import_clips(_p, _src)[0]",
        "    _clips[_name] = {'name': _v(_c.name), 'wt': _c.get_wiretap_node_id(),",
        "                     'uid': _v(_c.uid)}",
        "_c1 = flame.find_by_wiretap_node_id(_clips['clip1']['wt'])",
        "_result = {",
        "    'rgWt': _rg.get_wiretap_node_id(), 'rgName': _v(_rg.name),",
        "    'srcWt': _src.get_wiretap_node_id(), 'seqReelWt': _seqr.get_wiretap_node_id(),",
        "    'ccpWt': _ccp.get_wiretap_node_id(),",
        "    'libWt': _lib.get_wiretap_node_id(), 'libReelWt': _lr.get_wiretap_node_id(),",
        "    'clips': _clips, 'fps': _c1.frame_rate, 'width': _c1.width, 'height': _c1.height,",
        "}",
    ]
)

DISPLAYED_CODE: Final[str] = "\n".join(
    [
        "_c = _g(lambda: flame.timeline.clip)",
        "_result = None if (_c is None or isinstance(_c, str)) else {",
        "    'type': type(_c).__name__, 'uid': _g(lambda: _v(_c.uid)),",
        "    'name': _g(lambda: _v(_c.name))}",
    ]
)

OPEN_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_ret = _o.open()",
        "_c = _g(lambda: flame.timeline.clip)",
        "_result = {'returned': repr(_ret),",
        "           'displayedUid': None if (_c is None or isinstance(_c, str))",
        "           else _g(lambda: _v(_c.uid))}",
    ]
)

TRANSPORT_TRIM_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "_ret = _hit[0].trim_tail(@N@)",
        "_result = repr(_ret)",
    ]
)

SEG_FACTS_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_segs = list(_t.segments)",
        "_hit = [(_i, _s) for _i, _s in enumerate(_segs) if _s.record_in.frame == @RECORD_IN@]",
        "if not _hit:",
        "    _result = None",
        "else:",
        "    _i, _s = _hit[0]",
        "    _result = {'index': _i, 'type': _s.type, 'src': _s.source_name,",
        "               'rin': _s.record_in.frame, 'rout': _s.record_out.frame,",
        "               'sin': _s.source_in.frame, 'sout': _s.source_out.frame,",
        "               'head': _g(lambda: _v(_s.head)), 'tail': _g(lambda: _v(_s.tail)),",
        "               'count': len(_segs)}",
    ]
)

EFFECT_SET_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "_types = [_v(_f.type) for _f in list(_hit[0].effects)]",
        "_fx = [_f for _f in list(_hit[0].effects) if _v(_f.type) == @TYPE@][0]",
        "_ret = getattr(_fx, @KEY@).set_value(@VALUE@)",
        "_result = [repr(_ret), _g(lambda: _v(getattr(_fx, @KEY@))), _types]",
    ]
)

STASH_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_libs = [_l for _l in _ws.libraries if _v(_l.name) == @LIB@]",
        "_result = {'libraries': len(_libs), 'reels': [], 'entries': []}",
        "for _l in _libs:",
        "    for _r in _l.reels:",
        "        _result['reels'].append(_v(_r.name))",
        "        for _c in _r.children:",
        "            _result['entries'].append([_v(_r.name), type(_c).__name__, _v(_c.name),",
        "                                       _v(_c.uid), _g(lambda: _c.get_wiretap_node_id())])",
    ]
)

EXPORT_CODE: Final[str] = "\n".join(
    [
        "import os",
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_out = @OUT@",
        "os.makedirs(_out, exist_ok=True)",
        "_exp = flame.PyExporter()",
        "_exp.foreground = True",
        "_ret = _exp.export(_o, @PRESET@, _out)",
        "_result = {'ret': repr(_ret), 'files': len(os.listdir(_out))}",
    ]
)

CREATE_LIBRARY_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_result = [_v(_l.name) for _l in _ws.libraries if _v(_l.name) == @LIB@]",
    ]
)

DELETE_UID_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_uid(@UID@)",
        "_result = None if _o is None else flame.delete(_o, confirm=False)",
    ]
)

CLEANUP_CODE: Final[str] = "\n".join(
    [
        "_p = @P@",
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_gone = {'reelGroups': [], 'libraries': []}",
        "for _g2 in list(_d.reel_groups):",
        "    if _v(_g2.name).startswith(_p):",
        "        _gone['reelGroups'].append([_v(_g2.name), flame.delete(_g2, confirm=False)])",
        "for _l in list(flame.projects.current_project.current_workspace.libraries):",
        "    if _v(_l.name).startswith(_p):",
        "        _gone['libraries'].append([_v(_l.name), flame.delete(_l, confirm=False)])",
        "_result = _gone",
    ]
)

LEFT_CODE: Final[str] = "\n".join(
    [
        "_p = @P@",
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_result = {",
        "    'reelGroups': [_v(_g2.name) for _g2 in _d.reel_groups",
        "                   if _v(_g2.name).startswith(_p)],",
        "    'libraries': [_v(_l.name) for _l in",
        "                  flame.projects.current_project.current_workspace.libraries",
        "                  if _v(_l.name).startswith(_p)],",
        "}",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "prepare": PREPARE_CODE,
    "make_s1": MAKE_S1_TEMPLATE,
    "displayed": DISPLAYED_CODE,
    "open": OPEN_CODE,
    "transport_trim": TRANSPORT_TRIM_CODE,
    "seg_facts": SEG_FACTS_CODE,
    "effect_set": EFFECT_SET_CODE,
    "stash": STASH_CODE,
    "export": EXPORT_CODE,
    "library_by_name": CREATE_LIBRARY_CODE,
    "copy": COPY_CODE,
    "struct": STRUCT_CODE,
    "uid": UID_CODE,
    "reel_clips": REEL_CLIPS_CODE,
    "delete_uid": DELETE_UID_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


def name_prefix(ctx: Ctx) -> str:
    """`P32A` (this run's names start with it); the plan / delete e2e (p3_2b_plan_delete_e2e.py)
    reuses the material and sets `ctx.ids["prefix"] = "P32B"` first."""
    return str(ctx.ids.get("prefix", "P32A"))


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{name_prefix(ctx)}_{ctx.ts}_{tail}"


def ckpt_library(ctx: Ctx) -> str:
    return name_of(ctx, "CKPT")


def reel_name_today() -> str:
    return f"timeline_{time.strftime('%y%m%d')}"


async def tx_seg(ctx: Ctx, wiretap_id: str, track: int, record_in: int) -> dict[str, Any] | None:
    code = fill(SEG_FACTS_CODE, WT=wiretap_id, TRACK=track, RECORD_IN=record_in)
    got = await tx_val(ctx, code, "p32a_seg_facts")
    return None if is_error(got) or not isinstance(got, dict) else got


def err_text(env: dict[str, Any]) -> str:
    return str(env.get("error") or "")


def expect_error(env: dict[str, Any], code: str) -> bool:
    return env.get("ok") is False and env.get("errorCode") == code


# -- reading state back by transport


async def tx_struct(ctx: Ctx, wiretap_id: str) -> list[list[list[dict[str, Any]]]] | None:
    got = await tx_val(ctx, fill(STRUCT_CODE, WT=wiretap_id), "p32a_struct")
    return None if is_error(got) or got is None else got["struct"]


def track_rows(struct: Any, track: int, version: int = 0) -> list[dict[str, Any]]:
    return list(struct[version][track]) if struct else []


def media_row(struct: Any, track: int, source: str) -> dict[str, Any] | None:
    found = [r for r in track_rows(struct, track) if r["type"] != "Gap" and r["src"] == source]
    return found[0] if found else None


async def tx_children(ctx: Ctx, wiretap_id: str) -> list[list[Any]] | None:
    got = await tx_val(ctx, fill(REEL_CLIPS_CODE, WT=wiretap_id), "p32a_children")
    return None if is_error(got) or got is None else got


async def tx_find_uid(ctx: Ctx, uid: str) -> Any:
    return await tx_val(ctx, fill(UID_CODE, UID=uid), "p32a_find_uid")


async def tx_displayed(ctx: Ctx) -> dict[str, Any] | None:
    got = await tx_val(ctx, DISPLAYED_CODE, "p32a_displayed")
    return None if is_error(got) or got is None else got


async def tx_stash(ctx: Ctx) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(STASH_CODE, LIB=ckpt_library(ctx)), "p32a_stash")
    return None if is_error(got) or got is None else got


def uids_of(rows: list[list[Any]] | None) -> list[str]:
    return [r[2] for r in rows or []]


def names_of(rows: list[list[Any]] | None) -> list[str]:
    return [r[1] for r in rows or []]


def stash_uids(stash: dict[str, Any] | None) -> list[str]:
    return [e[3] for e in (stash or {}).get("entries") or []]


# -- journal and calls


def written_groups(ctx: Ctx) -> list[dict[str, str]]:
    return ctx.ids.setdefault("writeGroups", [])


async def run_call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    env = await do(session, ctx, op, build({**ctx.ids, **extra}))
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


async def call(
    session: Any, ctx: Ctx, op: str, args: dict[str, Any], *, track: bool = True
) -> dict[str, Any]:
    env = await do(session, ctx, op, args)
    group = (env.get("journal") or {}).get("group")
    if track and isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


async def revert(session: Any, ctx: Ctx, env: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return await call(
        session,
        ctx,
        "journal.revert",
        {"group": (env.get("journal") or {}).get("group"), **extra},
    )


def groups_now(ctx: Ctx) -> int:
    return len(journal_states(ctx))


def checkpoint_records(ctx: Ctx) -> list[dict[str, Any]]:
    return [r for r in journal_records(ctx) if r.get("type") == "checkpoint"]


def intent_of(ctx: Ctx, group: str | None) -> dict[str, Any]:
    return next(
        (r for r in journal_records(ctx) if r.get("type") == "intent" and r.get("group") == group),
        {},
    )


def outcome_of(ctx: Ctx, group: str | None) -> dict[str, Any]:
    return next(
        (r for r in journal_records(ctx) if r.get("type") == "outcome" and r.get("group") == group),
        {},
    )


def synthetic_open_intent(ctx: Ctx, real_group: str | None, **override: Any) -> str:
    """A copy of a real group's intent, with a new id and group and NO outcome, appended to this
    run's own journal directory: what a call whose answer never came leaves. `journal.check` then
    reads the real Flame and decides. `override` replaces fields of the copy."""
    record = copy.deepcopy(intent_of(ctx, real_group))
    group = new_id("g")
    record.update(id=new_id("i"), group=group, ts=now_iso())
    record.update(override)
    Journal(ctx.journal_dir).append(record)
    return group


async def check_group(session: Any, ctx: Ctx, group: str) -> dict[str, Any]:
    env = await do(session, ctx, "journal.check", {"group": group})
    return next(iter(_result_of(env).get("checked") or []), {}) if env.get("ok") else {}


async def fingerprint(session: Any, ctx: Ctx, wiretap_id: str) -> str | None:
    env = await do(session, ctx, "timeline.fingerprint", {"target": sel(wiretap_id)})
    value = _result_of(env).get("fingerprint")
    return value if env.get("ok") is True and isinstance(value, str) else None


# -- material


async def make_s1(
    ctx: Ctx,
    tail: str,
    *,
    reel_wt: str | None = None,
    trimmed: bool = True,
    fx: bool = True,
    puts: tuple[tuple[int, str, int], ...] = S1_PUTS,
) -> dict[str, Any] | None:
    ids = ctx.ids
    code = fill(
        MAKE_S1_TEMPLATE,
        REEL=reel_wt or ids["seqReelWt"],
        NAME=name_of(ctx, tail),
        FPS=ids["fps"],
        WIDTH=ids["width"],
        HEIGHT=ids["height"],
        PUTS=[[track, ids["clipWt"][clip], rel] for track, clip, rel in puts],
        TRIM=trimmed,
        HANDLE=TRIM_HANDLE,
        FX=fx,
        RATIO=LETTERBOX_RATIO,
    )
    br = await tx(ctx, PRELUDE + "\n" + code, label=f"p32a_make_{tail}", timeout_s=120.0)
    rec(ctx, f"prepare_{tail}_is_made", br.ok and bit_depth_ok(br.result), resp=_br_dict(br))
    return br.result if br.ok and isinstance(br.result, dict) else None


async def copy_into(ctx: Ctx, source_wt: str, dest_wt: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(COPY_CODE, SEQ=source_wt, DEST=dest_wt), "p32a_copy")
    return None if is_error(got) or not got else got[0]


async def prepare(ctx: Ctx) -> None:
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    files_dir = OUTPUT_DIR / f"files_{name_prefix(ctx).lower()}_{ctx.ts}"
    files_dir.mkdir(parents=True, exist_ok=True)
    ids = ctx.ids
    ids.update(filesDir=str(files_dir), exportRoot=str(files_dir / "exports"))
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
        label="p32a_prepare",
        timeout_s=180.0,
    )
    rec(ctx, "prepare_reel_group_library_and_4_clips", made.ok, resp=_br_dict(made))
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
        clipUid={n: v["uid"] for n, v in result["clips"].items()},
    )
    s1 = await make_s1(ctx, "S1")
    s2 = await make_s1(ctx, "S2", trimmed=False, fx=False, puts=((0, "clip1", 1),))
    s3 = await make_s1(ctx, "S3", trimmed=False, fx=False, puts=((0, "clip2", 1),))
    sandbox = await make_s1(ctx, "F")
    if None in (s1, s2, s3, sandbox):
        return
    assert s1 is not None and s2 is not None and s3 is not None and sandbox is not None
    ids.update(
        s1Wt=s1["wt"],
        s1Uid=s1["uid"],
        s2Wt=s2["wt"],
        s2Uid=s2["uid"],
        s3Wt=s3["wt"],
        s3Uid=s3["uid"],
        fWt=sandbox["wt"],
        fUid=sandbox["uid"],
    )
    library_copy = await copy_into(ctx, s1["wt"], result["libReelWt"])
    rec(ctx, "prepare_s1_copied_into_the_library", library_copy is not None, copy=library_copy)
    if library_copy is not None:
        ids.update(s1cWt=library_copy["wt"], s1cUid=library_copy["uid"])
    clip_copy = await copy_into(ctx, result["clips"]["clip1"]["wt"], result["ccpWt"])
    rec(ctx, "prepare_clip1_copied_into_ccp", clip_copy is not None, copy=clip_copy)
    if clip_copy is not None:
        ids.update(ccWt=clip_copy["wt"], ccUid=clip_copy["uid"])
    struct = await tx_struct(ctx, s1["wt"])
    media = media_row(struct, 1, "p2_len72")
    rec(
        ctx,
        "prepare_s1_has_the_trimmed_p2_len72_with_handles_10_and_10",
        media is not None and len(track_rows(struct, 0)) >= 3,
        track0=track_rows(struct, 0),
        track1=track_rows(struct, 1),
    )
    ids["s1Struct"] = struct
    preset = await prepare_preset(ctx, files_dir)
    ids["presetPath"] = preset


async def prepare_preset(ctx: Ctx, files_dir: Path) -> str | None:
    """The Tiff 16-bit LZW export preset, copied into this run's own files directory."""
    if not PRESET_SOURCE.is_file():
        observe(ctx, "prepare_export_preset_is_missing", source=str(PRESET_SOURCE))
        return None
    dst = files_dir / "presets" / "P32A_Tiff_16bit.xml"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PRESET_SOURCE, dst)
    return str(dst)


def ckpt_env_value(ctx: Ctx) -> str:
    return os.environ.get(CKPT_ENV, ckpt_library(ctx))
