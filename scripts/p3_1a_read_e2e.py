#!/usr/bin/env python3
"""Real-Flame end-to-end: the segment selector by `record_in` (with `expect_source_name`),
`timeline.list_segments` with `effects`, `segment.list_effects`, `segment.shared_sources`,
`desktop.find` and `desktop.info` (run with the `.venv` Python; `scripts/run_e2e.sh p3_1a_read`
starts Flame; `--dry-run` connects to nothing and only lists the stages and compiles the code).

Checks: The segment selector by `record_in`, `timeline.list_segments` with effects,
`segment.list_effects`, `segment.shared_sources`, `desktop.find` and `desktop.info`.

The test sequences are made with the bit depth of the media, read from the clip: Flame puts a
Resize on each segment of a sequence of another bit depth, and a new sequence otherwise gets the
bit depth of the project, which differs between projects.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (the segments of each sequence, their effects, a comment, a
reel's children, `find_by_uid`). Everything this run makes is named `P31A_<ts>_...` and is
deleted at the end; then flame.exit(). The journal directory is this run's own
(`p3/journal_p31a_e2e_<ts>/`). No test hook is set for any server.

JUDGED steps (`ok` decides the run) compare an op's answer with what transport read back.
OBSERVED steps (`kind: "observation"`, always ok) record what Flame did where nothing was
measured yet (how long a shared_sources page took, how many segments of other projects' work
it also returned, the exact attribute values of an effect).

Material (transport)
  desktop reel group RG with reels SRC (clip1, clip2, p2_len48 imported) and SEQ; library LIB
  with reel LR.
  S1   2 video tracks. track 0: clip1 | gap | clip2 [Blur] | gap | p2_len48 [Burn-in Letterbox];
       track 1: clip1 one clip further on (so it is NOT the same content as track 0's clip1).
  S2   one clip1.                         S1COPY  S1 copied into LR (same name as S1).
  S1H  start_at 01:00:00:00, one clip1 (its first record_in.frame is 86400).
  S3   clip1 at the same place on BOTH tracks (identical content: the ambiguous case).

Stages
  a  list_segments carries `effects` (a gap has []); segment.info by index and by record_in
     answer the same; S1H resolves record_in 86400; the catalog publishes the new selector fields.
  b  record_in that does not exist -> SELECTOR_EMPTY with the nearest values; index and record_in
     together -> INVALID_ARGS; expect_source_name mismatch -> CONFLICT and nothing written
     (nothing journaled, the comment untouched); a match goes through; a segment selector whose
     parent is a CLIP (SRC's clip1) resolves.
  c  segment.set_attributes (comment) by record_in -> read back -> its journal intent and revert
     address keep the record_in form -> journal.revert puts the comment back.
  d  segment.list_effects: Blur, Burn-in Letterbox (attributes read back one by one), a segment
     without effects, include_available (the effect_types list).
  e  segment.shared_sources from S1's first clip1: S1 (two tracks), S2, SRC's clip (the segment
     inside it), S1H and S3 come back, S1COPY (made in the library) does NOT; every
     selector returned resolves through segment.info; S3's identical pair is ambiguous with
     candidates; exactly one item is the target. From S1COPY's own segment no Desktop container
     comes back.
  f  desktop.find (a name in the Desktop and the library together, with parent, with types, by
     uid, an unknown name / uid); desktop.info of a clip, a sequence, S1H, a reel, a library, a
     reel group, compared with transport's own reads.
  h  timeline.info (start time, size, audio), segment.info (type, head, tail, effects;
     attributes as structure), a Flame object value as {type}.
  g  the journal: every group of this run is landed / reverted, none unknown.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import json
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
    _structured,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do,
    guarded,
    judged,
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

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("clip1", MEDIA_DIR / "clip1.mov"),
    ("clip2", MEDIA_DIR / "clip2.mov"),
    ("p2_len48", MEDIA_DIR / "p2" / "p2_len48.mov"),
)
GOOD_STATES: Final[frozenset[str]] = frozenset({"landed", "reverted"})
S1_FIRST_RELATIVE: Final[int] = 1
"""`overwrite(clip, flame.PyTime(n), track)`: a PyTime of an integer only is relative, 1 = the first
frame of the sequence."""
S1_LAYOUT: Final[tuple[tuple[str, int], ...]] = (("clip1", 1), ("clip2", 49), ("p2_len48", 97))
"""track 0 of S1: (clip, relative start). clip1 / clip2 are 24 frames long, so frames 24-47 and
72-95 are gaps (checked by reading S1 back, not assumed)."""
S1_TRACK1: Final[tuple[str, int]] = ("clip1", 25)
START_TC: Final[str] = "01:00:00:00"
START_FRAME: Final[int] = 86400
SHARED_PAGE: Final[int] = 20
SHARED_MAX_PAGES: Final[int] = 25

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one)

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def call_for(call_id: str, op: str) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Any]:
    def register(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


def seg_at(
    parent: str, record_in: int, *, track: int = 0, version: int = 0, expect: str | None = None
) -> dict[str, Any]:
    sel: dict[str, Any] = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": parent},
        "version": version,
        "track": track,
        "record_in": record_in,
    }
    if expect is not None:
        sel["expect_source_name"] = expect
    return sel


def seg_idx(parent: str, index: int, *, track: int = 0, version: int = 0) -> dict[str, Any]:
    return {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": parent},
        "version": version,
        "track": track,
        "index": index,
    }


def wt(wiretap_id: str) -> dict[str, Any]:
    return {"kind": "wiretap", "id": wiretap_id}


@call_for("a1_list_segments", "timeline.list_segments")
def _a1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["s1Wt"]), "version": 0, "track": 0, "limit": 100}


@call_for("a2_info_by_index", "segment.info")
def _a2i(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_idx(ids["s1Wt"], ids["c2Idx"])}


@call_for("a2_info_by_record_in", "segment.info")
def _a2r(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_at(ids["s1Wt"], ids["c2In"])}


@call_for("a3_info_s1h_record_in", "segment.info")
def _a3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_at(ids["s1hWt"], START_FRAME, expect="clip1")}


@call_for("a3_list_s1h", "timeline.list_segments")
def _a3l(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["s1hWt"]), "limit": 50}


@call_for("b1_missing_record_in", "segment.info")
def _b1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_at(ids["s1Wt"], ids["missingIn"])}


@call_for("b2_index_and_record_in", "segment.info")
def _b2(ids: dict[str, Any]) -> dict[str, Any]:
    both = seg_idx(ids["s1Wt"], 0)
    both["record_in"] = 0
    return {"target": both}


@call_for("b3_expect_mismatch_write", "segment.set_attributes")
def _b3(ids: dict[str, Any]) -> dict[str, Any]:
    target = seg_at(ids["s1Wt"], ids["c1In"], expect="clip2")
    return {"target": target, "values": {"comment": ids["comment"]}}


@call_for("b3_expect_mismatch_info_by_index", "segment.info")
def _b3i(ids: dict[str, Any]) -> dict[str, Any]:
    target = seg_idx(ids["s1Wt"], ids["c1Idx"])
    target["expect_source_name"] = "clip2"
    return {"target": target}


@call_for("b4_expect_match_info", "segment.info")
def _b4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_at(ids["s1Wt"], ids["c2In"], expect="clip2")}


@call_for("b5_info_inside_a_clip", "segment.info")
def _b5(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_idx(ids["srcClipWt"], 0)}


@call_for("b5_info_inside_a_clip_by_record_in", "segment.info")
def _b5r(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_at(ids["srcClipWt"], ids["srcInnerIn"], expect="clip1")}


@call_for("c1_write_comment_by_record_in", "segment.set_attributes")
def _c1(ids: dict[str, Any]) -> dict[str, Any]:
    target = seg_at(ids["s1Wt"], ids["c1In"], expect="clip1")
    return {"target": target, "values": {"comment": ids["comment"]}}


@call_for("d1_effects_blur", "segment.list_effects")
def _d1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_at(ids["s1Wt"], ids["c2In"], expect="clip2")}


@call_for("d2_effects_letterbox", "segment.list_effects")
def _d2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_at(ids["s1Wt"], ids["l48In"], expect="p2_len48")}


@call_for("d3_effects_none_and_available", "segment.list_effects")
def _d3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_at(ids["s1Wt"], ids["c1In"]), "include_available": True}


@call_for("e1_shared_sources_page", "segment.shared_sources")
def _e1(ids: dict[str, Any]) -> dict[str, Any]:
    target = seg_at(ids["s1Wt"], ids["c1In"], expect="clip1")
    return {"target": target, "limit": SHARED_PAGE, "offset": ids.get("offset", 0)}


@call_for("e1_shared_sources_by_index", "segment.shared_sources")
def _e1i(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_idx(ids["s1Wt"], ids["c1Idx"]), "limit": 1, "offset": 0}


@call_for("e4_shared_sources_from_the_library_copy", "segment.shared_sources")
def _e4(ids: dict[str, Any]) -> dict[str, Any]:
    target = seg_at(ids["s1cWt"], ids["c1In"], expect="clip1")
    return {"target": target, "limit": SHARED_PAGE, "offset": 0}


@call_for("e2_info_of_a_returned_selector", "segment.info")
def _e2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": ids["selector"]}


@call_for("f1_find_name_everywhere", "desktop.find")
def _f1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["s1Name"], "limit": 50}


@call_for("f2_find_name_in_the_desktop_reel", "desktop.find")
def _f2d(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["s1Name"], "parent": wt(ids["seqReelWt"]), "limit": 50}


@call_for("f2_find_name_in_the_library_reel", "desktop.find")
def _f2l(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["s1Name"], "parent": wt(ids["libReelWt"]), "limit": 50}


@call_for("f3_find_name_with_types", "desktop.find")
def _f3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["s1Name"], "types": ["PyClip"], "limit": 50}


@call_for("f4_find_uid", "desktop.find")
def _f4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"uid": ids["s1Uid"], "limit": 50}


@call_for("f4_find_unknown_uid", "desktop.find")
def _f4u(ids: dict[str, Any]) -> dict[str, Any]:
    return {"uid": "P31A-no-such-uid", "limit": 50}


@call_for("f4_find_unknown_name", "desktop.find")
def _f4n(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": "P31A no such name", "limit": 50}


@call_for("f4_find_name_and_uid_refused", "desktop.find")
def _f4b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["s1Name"], "uid": ids["s1Uid"]}


@call_for("f5_info", "desktop.info")
def _f5(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["infoWt"])}


@call_for("h1_timeline_info_s1", "timeline.info")
def _h1a(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["s1Wt"])}


@call_for("h1_timeline_info_s1h", "timeline.info")
def _h1b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["s1hWt"])}


@call_for("h2_segment_info_clip2", "segment.info")
def _h2a(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_idx(ids["s1Wt"], ids["c2Idx"])}


@call_for("h2_segment_info_letterbox", "segment.info")
def _h2b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_idx(ids["s1Wt"], ids["l48Idx"])}


@call_for("h2_segment_info_gap", "segment.info")
def _h2c(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_idx(ids["s1Wt"], ids["gapIdx"])}


@call_for("h3_segment_get_attributes", "segment.get_attributes")
def _h3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": seg_idx(ids["s1Wt"], ids["c2Idx"])}


@call_for("h4_sequence_get_attributes", "desktop.get_attributes")
def _h4(ids: dict[str, Any]) -> dict[str, Any]:
    keys = ["primary_track", "secondary_track", "subtitles_track", "selected_segments"]
    return {"target": wt(ids["s1Wt"]), "keys": keys}


# -- transport scaffolding (never through the op under test)

PRELUDE: Final[str] = "\n".join(
    [
        "def _g(fn):",
        "    try:",
        "        return fn()",
        "    except BaseException as _e:",
        "        return 'ERR:' + type(_e).__name__ + ': ' + str(_e)",
        "def _fxrow(_s):",
        "    try:",
        "        return [_v(_f.type) for _f in list(_s.effects)]",
        "    except BaseException:",
        "        return []",
        "def _struct(_seq):",
        "    _out = []",
        "    for _ver in list(_seq.versions):",
        "        _trs = []",
        "        for _trk in list(_ver.tracks):",
        "            _rows = []",
        "            for _i, _s in enumerate(list(_trk.segments)):",
        "                _rows.append({'i': _i, 'type': _s.type, 'src': _s.source_name,",
        "                              'rin': _s.record_in.frame, 'rout': _s.record_out.frame,",
        "                              'sin': _s.source_in.frame, 'fx': _fxrow(_s)})",
        "            _trs.append(_rows)",
        "        _out.append(_trs)",
        "    return _out",
    ]
)

PREPARE_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_rg = _ws.desktop.create_reel_group(@RG@)",
        "_src = _rg.create_reel('SRC')",
        "_seqr = _rg.create_reel('SEQ')",
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
        "    'libWt': _lib.get_wiretap_node_id(), 'libReelWt': _lr.get_wiretap_node_id(),",
        "    'clips': _clips, 'fps': _c1.frame_rate, 'width': _c1.width, 'height': _c1.height,",
        "}",
    ]
)

BIT_DEPTH_CODE: Final[tuple[str, ...]] = (
    "_depths = [flame.find_by_wiretap_node_id(_p[1]).bit_depth for _p in @PUTS@]",
    "_bd = _depths[0] if _depths else None",
)
"""Reads the bit depth of the clips a sequence is about to hold (`_bd`: the first one's, `_depths`:
every one's) so the sequence is made with it. A sequence of another bit depth than its media gets a
Resize on each segment from Flame by itself, and the bit depth a new sequence gets when none is
asked for is the project's own (10 in one project, 8 in another), so it is never left to default."""

BIT_DEPTH_RESULT: Final[str] = (
    "'bitDepth': _bd, 'clipBitDepths': _depths, 'seqBitDepth': _g(lambda: _v(_seq.bit_depth))"
)


def bit_depth_ok(result: Any) -> bool:
    """True when the sequence a make-code built has the bit depth of every clip put on it (a result
    without a `bitDepth`, a make-code that put no clip, is not judged)."""
    if not isinstance(result, dict) or result.get("bitDepth") is None:
        return True
    return {result["seqBitDepth"], *result["clipBitDepths"]} == {result["bitDepth"]}


MAKE_SEQUENCE_CODE: Final[str] = "\n".join(
    [
        "_reel = flame.find_by_wiretap_node_id(@REEL@)",
        *BIT_DEPTH_CODE,
        "_kw = {'frame_rate': @FPS@, 'width': @WIDTH@, 'height': @HEIGHT@}",
        "if _bd is not None:",
        "    _kw['bit_depth'] = _bd",
        "_start = @START@",
        "if _start:",
        "    _kw['start_at'] = flame.PyTime(_start, @FPS@)",
        "_note = None",
        "try:",
        "    _seq = _reel.create_sequence(name=@NAME@, video_tracks=@TRACKS@, **_kw)",
        "except BaseException as _e:",
        "    _kw.pop('start_at', None)",
        "    _seq = _reel.create_sequence(name=@NAME@, video_tracks=@TRACKS@)",
        "    _note = 'create_sequence kwargs refused: ' + str(_e)",
        "_puts = []",
        "for _track, _clip_wt, _rel in @PUTS@:",
        "    _clip = flame.find_by_wiretap_node_id(_clip_wt)",
        "    _tr = _seq.versions[0].tracks[_track]",
        "    _put = _seq.overwrite(_clip, flame.PyTime(_rel), _tr)",
        "    _puts.append([_track, _v(_clip.name), _rel, repr(_put)])",
        "_result = {'wt': _seq.get_wiretap_node_id(), 'uid': _v(_seq.uid), 'puts': _puts,",
        "           'name': _v(_seq.name), 'note': _note, " + BIT_DEPTH_RESULT + "}",
    ]
)

EFFECTS_CODE: Final[str] = "\n".join(
    [
        "_seq = flame.find_by_wiretap_node_id(@SEQ@)",
        "_made = []",
        "for _src, _fx in @FX@:",
        "    _hit = [_s for _s in _seq.versions[0].tracks[0].segments if _s.source_name == _src]",
        "    _made.append([_src, _fx, repr(_hit[0].create_effect(_fx))])",
        "_result = _made",
    ]
)

COPY_CODE: Final[str] = "\n".join(
    [
        "_seq = flame.find_by_wiretap_node_id(@SEQ@)",
        "_dest = flame.find_by_wiretap_node_id(@DEST@)",
        "_copies = flame.media_panel.copy(_seq, _dest)",
        "_result = [{'wt': _c.get_wiretap_node_id(), 'uid': _v(_c.uid), 'name': _v(_c.name),",
        "            'type': type(_c).__name__} for _c in _copies]",
    ]
)

STRUCT_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else {'type': type(_o).__name__, 'struct': _struct(_o)}",
    ]
)

SEG_READ_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[@VERSION@].tracks)[@TRACK@]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "_s = _hit[0]",
        "_fx = []",
        "for _f in list(_s.effects):",
        "    _fx.append({'type': _v(_f.type), 'names': list(_f.attributes),",
        "                'bypass': _g(lambda: _v(_f.bypass)),",
        "                'values': {_k: _g(lambda: repr(_v(getattr(_f, _k))))",
        "                           for _k in list(_f.attributes)}})",
        "_result = {'hits': len(_hit), 'comment': _g(lambda: _v(_s.comment)),",
        "           'source': _s.source_name, 'fx': _fx,",
        "           'effectTypes': _g(lambda: [_v(_t2) for _t2 in list(_s.effect_types)])}",
    ]
)

ENTRY_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_row = {'type': type(_o).__name__, 'name': _v(_o.name), 'uid': _g(lambda: _v(_o.uid))}",
        "if _row['type'] in ('PyClip', 'PySequence'):",
        "    _row.update({",
        "        'duration': _g(lambda: _o.duration.frame),",
        "        'startFrame': _g(lambda: _o.start_frame),",
        "        'startTime': _g(lambda: _v(_o.start_time).frame),",
        "        'frameRate': _g(lambda: _o.frame_rate),",
        "        'width': _g(lambda: _o.width), 'height': _g(lambda: _o.height),",
        "        'bitDepth': _g(lambda: _o.bit_depth), 'scanMode': _g(lambda: _o.scan_mode),",
        "        'cached': _g(lambda: _o.cached), 'unlinked': _g(lambda: _o.unlinked),",
        "        'versions': _g(lambda: [len(list(_x.tracks)) for _x in list(_o.versions)]),",
        "        'audioTracks': _g(lambda: len(list(_o.audio_tracks))),",
        "    })",
        "else:",
        "    _counts = {}",
        "    for _c in list(_o.children):",
        "        _counts[type(_c).__name__] = _counts.get(type(_c).__name__, 0) + 1",
        "    _row['childCounts'] = _counts",
        "_par = _g(lambda: _o.parent)",
        "_row['parent'] = None if isinstance(_par, str) else {'type': type(_par).__name__,",
        "                                                      'name': _g(lambda: _v(_par.name))}",
        "_result = _row",
    ]
)

UID_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_uid(@UID@)",
        "_result = None if _o is None else type(_o).__name__",
    ]
)

REEL_CLIPS_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = [[type(_c).__name__, _v(_c.name), _v(_c.uid)] for _c in _o.children]",
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
    "make_sequence": MAKE_SEQUENCE_CODE,
    "effects": EFFECTS_CODE,
    "copy": COPY_CODE,
    "struct": STRUCT_CODE,
    "seg_read": SEG_READ_CODE,
    "entry": ENTRY_CODE,
    "uid": UID_CODE,
    "reel_clips": REEL_CLIPS_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


def fill(code: str, **values: Any) -> str:
    """`code` with each `@NAME@` replaced by repr(value)."""
    for key, value in values.items():
        code = code.replace(f"@{key}@", repr(value))
    return code


async def tx_val(ctx: Ctx, code: str, label: str, timeout_s: float = 60.0) -> Any:
    br = await tx(ctx, PRELUDE + "\n" + code, label=label, timeout_s=timeout_s)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


def is_error(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("<transport error")


async def tx_struct(ctx: Ctx, wiretap_id: str) -> Any:
    got = await tx_val(ctx, fill(STRUCT_CODE, WT=wiretap_id), "p31a_struct")
    return None if is_error(got) or got is None else got["struct"]


def rows(struct: Any, track: int = 0, version: int = 0) -> list[dict[str, Any]]:
    return list(struct[version][track]) if struct else []


def media_rows(struct: Any, source: str, track: int = 0) -> list[dict[str, Any]]:
    return [r for r in rows(struct, track) if r["type"] != "Gap" and r["src"] == source]


def name_of(ctx: Ctx, tail: str) -> str:
    return f"P31A_{ctx.ts}_{tail}"


# -- prepare


async def make_sequence(
    ctx: Ctx, tail: str, reel_wt: str, tracks: int, puts: list[list[Any]], start: str = ""
) -> dict[str, Any] | None:
    ids = ctx.ids
    code = fill(
        MAKE_SEQUENCE_CODE,
        REEL=reel_wt,
        FPS=ids["fps"],
        WIDTH=ids["width"],
        HEIGHT=ids["height"],
        START=start,
        NAME=name_of(ctx, tail),
        TRACKS=tracks,
        PUTS=puts,
    )
    br = await tx(ctx, PRELUDE + "\n" + code, label=f"p31a_make_{tail}", timeout_s=120.0)
    rec(ctx, f"prepare_{tail}_is_made", br.ok and bit_depth_ok(br.result), resp=_br_dict(br))
    return br.result if br.ok and isinstance(br.result, dict) else None


async def prepare(ctx: Ctx) -> None:
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
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
        label="p31a_prepare",
        timeout_s=180.0,
    )
    rec(ctx, "prepare_reel_group_library_and_3_clips", made.ok, resp=_br_dict(made))
    if not (made.ok and isinstance(made.result, dict)):
        return
    ids = ctx.ids
    ids.update(made.result)
    ids["srcClipWt"] = made.result["clips"]["clip1"]["wt"]
    clip = {n: v["wt"] for n, v in made.result["clips"].items()}
    ids["s1Name"] = name_of(ctx, "S1")
    ids["comment"] = f"P31A {ctx.ts}"

    s1 = await make_sequence(
        ctx,
        "S1",
        ids["seqReelWt"],
        2,
        [
            *[[0, clip[c], rel] for c, rel in S1_LAYOUT],
            [1, clip[S1_TRACK1[0]], S1_TRACK1[1]],
        ],
    )
    if s1 is None:
        return
    ids.update(s1Wt=s1["wt"], s1Uid=s1["uid"])
    fx = await tx(
        ctx,
        PRELUDE
        + "\n"
        + fill(
            EFFECTS_CODE,
            SEQ=ids["s1Wt"],
            FX=[["clip2", "Blur"], ["p2_len48", "Burn-in Letterbox"]],
        ),
        label="p31a_effects",
    )
    rec(ctx, "prepare_s1_effects_blur_and_burn_in_letterbox", fx.ok, resp=_br_dict(fx))
    s2 = await make_sequence(ctx, "S2", ids["seqReelWt"], 1, [[0, clip["clip1"], 1]])
    s3 = await make_sequence(
        ctx, "S3", ids["seqReelWt"], 2, [[0, clip["clip1"], 1], [1, clip["clip1"], 1]]
    )
    s1h = await make_sequence(
        ctx, "S1H", ids["seqReelWt"], 1, [[0, clip["clip1"], 1]], start=START_TC
    )
    if s2 is None or s3 is None or s1h is None:
        return
    ids.update(s2Wt=s2["wt"], s3Wt=s3["wt"], s1hWt=s1h["wt"])
    copied = await tx(
        ctx,
        PRELUDE + "\n" + fill(COPY_CODE, SEQ=ids["s1Wt"], DEST=ids["libReelWt"]),
        label="p31a_copy_s1_to_library",
    )
    rec(ctx, "prepare_s1_copied_into_the_library_reel", copied.ok, resp=_br_dict(copied))
    if copied.ok and isinstance(copied.result, list) and copied.result:
        ids["s1cWt"] = copied.result[0]["wt"]
    await read_facts(ctx)


async def read_facts(ctx: Ctx) -> None:
    """The record positions the stages address, read from Flame (not assumed)."""
    ids = ctx.ids
    s1 = await tx_struct(ctx, ids["s1Wt"])
    s1h = await tx_struct(ctx, ids["s1hWt"])
    src_inner = await tx_struct(ctx, ids["srcClipWt"])
    try:
        c1, c2, l48 = (media_rows(s1, n)[0] for n in ("clip1", "clip2", "p2_len48"))
        ids.update(
            c1In=c1["rin"],
            c1Idx=c1["i"],
            c2In=c2["rin"],
            c2Idx=c2["i"],
            l48In=l48["rin"],
            l48Idx=l48["i"],
            gapIdx=next(r["i"] for r in rows(s1) if r["type"] == "Gap"),
            srcInnerIn=rows(src_inner)[0]["rin"],
            s1hIn=media_rows(s1h, "clip1")[0]["rin"],
        )
        track0_in = [r["rin"] for r in rows(s1)]
        ids["missingIn"] = next(n for n in range(5, 1000) if n not in track0_in)
        ids["facts"] = {"s1": s1, "s1h": s1h, "srcInner": src_inner}
    except (IndexError, TypeError, StopIteration, KeyError):
        pass
    rec(
        ctx,
        "prepare_facts_read_back_from_flame",
        "c1In" in ids,
        c1In=ids.get("c1In"),
        c2In=ids.get("c2In"),
        l48In=ids.get("l48In"),
        s1hIn=ids.get("s1hIn"),
        s1Track0=[(r["type"], r["src"], r["rin"], r["rout"], r["fx"]) for r in rows(s1)],
        s1Track1=[(r["type"], r["src"], r["rin"], r["rout"]) for r in rows(s1, 1)],
    )


async def run_call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    return await do(session, ctx, op, build({**ctx.ids, **extra}))


def expect_error(env: dict[str, Any], code: str) -> bool:
    return env.get("ok") is False and env.get("errorCode") == code


def err_text(env: dict[str, Any]) -> str:
    return str(env.get("error") or "")


# -- a: reads


async def stage_a(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31a-a") as session:
        catalog = _structured(
            await session.call_tool("flame_catalog", {"op": "segment.info"}),
            tool="flame_catalog",
            results=ctx.results,
        )
        fields = ((_result_of(catalog).get("operation") or {}).get("accepts") or [{}])[0].get(
            "fields", {}
        )
        rec(
            ctx,
            "a0_the_catalog_publishes_record_in_and_expect_source_name",
            {"record_in", "expect_source_name", "index"} <= set(fields)
            and fields.get("index", {}).get("required") is False,
            fields=sorted(fields),
        )
        names = _structured(
            await session.call_tool("flame_catalog", {}), tool="flame_catalog", results=ctx.results
        )
        listed = {o["name"] for o in _result_of(names).get("operations", [])}
        new_ops = {"segment.list_effects", "segment.shared_sources", "desktop.find", "desktop.info"}
        rec(
            ctx,
            "a0_the_catalog_lists_the_four_new_ops",
            new_ops <= listed and listed == REGISTERED_OP_NAMES,
            missing=sorted(new_ops - listed),
        )

        a1 = await run_call(session, ctx, "a1_list_segments")
        items = _result_of(a1).get("items") or []
        truth = rows(ids["facts"]["s1"])
        rec(
            ctx,
            "a1_list_segments_effects_equal_what_transport_read_and_a_gap_has_none",
            a1.get("ok") is True
            and [i.get("effects") for i in items] == [r["fx"] for r in truth]
            and [i.get("type") for i in items] == [r["type"] for r in truth]
            and any("Blur" in i["effects"] for i in items)
            and any("Burn-in Letterbox" in i["effects"] for i in items)
            and all(i["effects"] == [] for i in items if i.get("isGap")),
            api=[(i.get("type"), i.get("effects")) for i in items],
            transport=[(r["type"], r["fx"]) for r in truth],
        )

        a2i = await run_call(session, ctx, "a2_info_by_index")
        a2r = await run_call(session, ctx, "a2_info_by_record_in")
        rec(
            ctx,
            "a2_segment_info_by_index_and_by_record_in_answer_the_same",
            a2i.get("ok") is True
            and a2r.get("ok") is True
            and _result_of(a2i) == _result_of(a2r)
            and _result_of(a2r).get("sourceName") == "clip2"
            and (_result_of(a2r).get("recordIn") or {}).get("frame") == ids["c2In"],
            byIndex=_result_of(a2i).get("recordIn"),
            byRecordIn=_result_of(a2r).get("recordIn"),
            error=a2r.get("error"),
        )

        a3 = await run_call(session, ctx, "a3_info_s1h_record_in")
        a3l = await run_call(session, ctx, "a3_list_s1h")
        first = (_result_of(a3l).get("items") or [{}])[0]
        rec(
            ctx,
            "a3_a_sequence_that_starts_at_01_00_00_00_resolves_record_in_86400",
            a3.get("ok") is True
            and (_result_of(a3).get("recordIn") or {}).get("frame") == START_FRAME
            and ids.get("s1hIn") == START_FRAME,
            recordIn=_result_of(a3).get("recordIn"),
            firstListed=first.get("recordIn"),
            error=a3.get("error"),
        )


# -- b: errors and the safeguard


async def stage_b(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31a-b") as session:
        b1 = await run_call(session, ctx, "b1_missing_record_in")
        track0_in = [r["rin"] for r in rows(ids["facts"]["s1"])]
        near = sorted(track0_in, key=lambda f: (abs(f - ids["missingIn"]), f))[:5]
        rec(
            ctx,
            "b1_a_record_in_nothing_has_is_selector_empty_with_the_nearest_five",
            expect_error(b1, "SELECTOR_EMPTY") and str(near) in err_text(b1),
            expectedNearest=near,
            error=b1.get("error"),
        )
        b2 = await run_call(session, ctx, "b2_index_and_record_in")
        rec(
            ctx,
            "b2_index_and_record_in_together_are_invalid_args",
            expect_error(b2, "INVALID_ARGS") and "exactly one of index / record_in" in err_text(b2),
            error=b2.get("error"),
        )

        groups_before = len(journal_states(ctx))
        before = await read_segment(ctx, ids["s1Wt"], ids["c1In"])
        b3 = await run_call(session, ctx, "b3_expect_mismatch_write")
        after = await read_segment(ctx, ids["s1Wt"], ids["c1In"])
        rec(
            ctx,
            "b3_a_source_name_that_differs_is_a_conflict_and_nothing_is_written",
            expect_error(b3, "CONFLICT")
            and "'clip1'" in err_text(b3)
            and groups_before == len(journal_states(ctx))
            and isinstance(before, dict)
            and isinstance(after, dict)
            and before.get("comment") == after.get("comment"),
            error=b3.get("error"),
            commentBefore=(before or {}).get("comment") if isinstance(before, dict) else before,
            commentAfter=(after or {}).get("comment") if isinstance(after, dict) else after,
            journalGroups=len(journal_states(ctx)),
        )
        b3i = await run_call(session, ctx, "b3_expect_mismatch_info_by_index")
        rec(
            ctx,
            "b3_the_index_form_with_expect_source_name_is_a_conflict_too",
            expect_error(b3i, "CONFLICT") and "'clip1'" in err_text(b3i),
            error=b3i.get("error"),
        )
        b4 = await run_call(session, ctx, "b4_expect_match_info")
        rec(
            ctx,
            "b4_a_matching_expect_source_name_goes_through",
            b4.get("ok") is True and _result_of(b4).get("sourceName") == "clip2",
            error=b4.get("error"),
        )
        b5 = await run_call(session, ctx, "b5_info_inside_a_clip")
        b5r = await run_call(session, ctx, "b5_info_inside_a_clip_by_record_in")
        rec(
            ctx,
            "b5_a_segment_selector_whose_parent_is_a_clip_resolves",
            b5.get("ok") is True
            and b5r.get("ok") is True
            and _result_of(b5).get("sourceName") == "clip1"
            and _result_of(b5r) == _result_of(b5),
            sourceName=_result_of(b5).get("sourceName"),
            recordIn=_result_of(b5).get("recordIn"),
            errorByIndex=b5.get("error"),
            errorByRecordIn=b5r.get("error"),
        )


async def read_segment(ctx: Ctx, wiretap_id: str, record_in: int) -> Any:
    return await tx_val(
        ctx,
        fill(SEG_READ_CODE, WT=wiretap_id, VERSION=0, TRACK=0, RECORD_IN=record_in),
        "p31a_read_segment",
    )


# -- c: write and revert


async def stage_c(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31a-c") as session:
        before = await read_segment(ctx, ids["s1Wt"], ids["c1In"])
        c1 = await do_write(session, ctx, "c1_write_comment_by_record_in")
        after = await read_segment(ctx, ids["s1Wt"], ids["c1In"])
        target = _result_of(c1).get("revert", {}) or {}
        revert_target = (target.get("args") or {}).get("target") or {}
        want = seg_at(ids["s1Wt"], ids["c1In"], expect="clip1")
        rec(
            ctx,
            "c1_a_comment_written_by_record_in_reads_back_and_the_revert_keeps_the_form",
            c1.get("ok") is True
            and isinstance(after, dict)
            and after.get("comment") == ids["comment"]
            and revert_target == want
            and "index" not in revert_target,
            error=c1.get("error"),
            commentBefore=(before or {}).get("comment") if isinstance(before, dict) else before,
            commentAfter=(after or {}).get("comment") if isinstance(after, dict) else after,
            revertTarget=revert_target,
        )
        group = (c1.get("journal") or {}).get("group")
        intent = next(
            (
                r
                for r in journal_records(ctx)
                if r.get("type") == "intent" and r.get("group") == group
            ),
            {},
        )
        rec(
            ctx,
            "c1_the_journal_intent_addresses_the_segment_by_record_in",
            intent.get("target") == want
            and (intent.get("before") or {}).get("comment") is not None,
            target=intent.get("target"),
            before=intent.get("before"),
        )
        c2 = await do_write(session, ctx, "journal.revert", {"group": group})
        again = await read_segment(ctx, ids["s1Wt"], ids["c1In"])
        rec(
            ctx,
            "c2_journal_revert_puts_the_previous_comment_back",
            c2.get("ok") is True
            and isinstance(again, dict)
            and isinstance(before, dict)
            and again.get("comment") == before.get("comment"),
            error=c2.get("error"),
            commentBack=(again or {}).get("comment") if isinstance(again, dict) else again,
        )


def written_groups(ctx: Ctx) -> list[dict[str, str]]:
    return ctx.ids.setdefault("writeGroups", [])


async def do_write(
    session: Any, ctx: Ctx, call_id: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A call that writes (a CALLS entry or an op name) and remembers its journal group."""
    if call_id in CALLS:
        env = await run_call(session, ctx, call_id)
    else:
        env = await do(session, ctx, call_id, args)
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": call_id, "group": group})
    return env


# -- d: list_effects


def effects_of(env: dict[str, Any]) -> list[dict[str, Any]]:
    value = _result_of(env).get("effects")
    return value if isinstance(value, list) else []


async def stage_d(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31a-d") as session:
        d1 = await run_call(session, ctx, "d1_effects_blur")
        d2 = await run_call(session, ctx, "d2_effects_letterbox")
        d3 = await run_call(session, ctx, "d3_effects_none_and_available")
        blur = await read_segment(ctx, ids["s1Wt"], ids["c2In"])
        box = await read_segment(ctx, ids["s1Wt"], ids["l48In"])
        none = await read_segment(ctx, ids["s1Wt"], ids["c1In"])
        for name, env, truth in (
            ("d1_blur", d1, blur),
            ("d2_burn_in_letterbox", d2, box),
        ):
            facts = truth.get("fx") if isinstance(truth, dict) else None
            got = effects_of(env)
            rec(
                ctx,
                f"{name}_effects_equal_what_transport_read_one_attribute_at_a_time",
                env.get("ok") is True
                and isinstance(facts, list)
                and [e.get("type") for e in got] == [f["type"] for f in facts]
                and [sorted(e.get("attributes") or {}) for e in got]
                == [sorted(f["names"]) for f in facts]
                and [e.get("index") for e in got] == list(range(len(facts))),
                api=got,
                transport=facts,
                error=env.get("error"),
            )
        observe(
            ctx,
            "d2_the_values_of_burn_in_letterbox_attributes_as_the_op_and_transport_show_them",
            api=[e.get("attributes") for e in effects_of(d2)],
            transport=[f.get("values") for f in (box.get("fx") if isinstance(box, dict) else [])],
        )
        types = _result_of(d3).get("availableTypes")
        truth_types = none.get("effectTypes") if isinstance(none, dict) else None
        ok, parts = judged(
            op_ok=d3.get("ok") is True,
            op_lists_no_effect=effects_of(d3) == [],
            transport_reads_the_segment=isinstance(none, dict),
            transport_lists_no_effect=isinstance(none, dict) and none.get("fx") == [],
            op_lists_available_types=isinstance(types, list),
            transport_lists_available_types=isinstance(truth_types, list),
            the_two_lists_are_equal=types == truth_types,
        )
        rec(
            ctx,
            "d3_a_segment_without_effects_is_empty_and_include_available_lists_effect_types",
            ok,
            clauses=parts,
            count=len(types) if isinstance(types, list) else None,
            opEffects=effects_of(d3),
            transportEffects=none.get("fx") if isinstance(none, dict) else None,
            error=d3.get("error"),
        )
        observe(
            ctx,
            "d3_effect_types_the_segment_offers",
            count=len(types) if isinstance(types, list) else None,
            types=types,
        )


# -- e: shared_sources


async def shared_pages(session: Any, ctx: Ctx) -> tuple[list[dict[str, Any]], list[int], int]:
    items: list[dict[str, Any]] = []
    millis: list[int] = []
    total = 0
    offset = 0
    for _page in range(SHARED_MAX_PAGES):
        started = time.monotonic()
        env = await run_call(session, ctx, "e1_shared_sources_page", offset=offset)
        millis.append(round((time.monotonic() - started) * 1000))
        value = _result_of(env)
        if env.get("ok") is not True:
            ctx.ids["sharedError"] = env.get("error")
            break
        items.extend(value.get("items") or [])
        total = int(value.get("total") or 0)
        if not value.get("truncated"):
            break
        offset += int(value.get("shown") or 1)
    return items, millis, total


def by_container(items: list[dict[str, Any]], wiretap_id: str | None) -> list[dict[str, Any]]:
    return [
        i
        for i in items
        if wiretap_id is not None
        and ((i.get("container") or {}).get("selector") or {}).get("id") == wiretap_id
    ]


async def stage_e(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31a-e") as session:
        items, millis, total = await shared_pages(session, ctx)
        owned = {
            "s1": ids["s1Wt"],
            "s2": ids["s2Wt"],
            "s3": ids["s3Wt"],
            "s1h": ids["s1hWt"],
            "s1c": ids.get("s1cWt"),
            "src": ids["srcClipWt"],
        }
        # a copy made in the LIBRARY is not returned from the Desktop
        expect_n = {"s1": 2, "s2": 1, "s3": 2, "s1h": 1, "s1c": 0, "src": 1}
        got_n = {k: len(by_container(items, w)) for k, w in owned.items()}
        observe(
            ctx,
            "e1_shared_sources_pages_and_how_long_each_took",
            pageMs=millis,
            pages=len(millis),
            total=total,
            returned=len(items),
            fromOtherWork=len(items) - sum(got_n.values()),
        )
        rec(
            ctx,
            "e1_every_container_with_clip1_comes_back_with_its_segments",
            len(millis) >= 1 and got_n == expect_n,
            expected=expect_n,
            got=got_n,
            error=ids.get("sharedError"),
        )
        s1_items = by_container(items, ids["s1Wt"])
        targets = [i for i in items if i.get("isTarget") is True]
        rec(
            ctx,
            "e1_exactly_one_item_is_the_target_and_it_is_s1_track_0_record_in_0",
            len(targets) == 1
            and (targets[0].get("selector") or {}).get("parent", {}).get("id") == ids["s1Wt"]
            and (targets[0].get("selector") or {}).get("track") == 0
            and (targets[0].get("recordIn") or {}).get("frame") == ids["c1In"],
            targets=[(t.get("container"), t.get("selector")) for t in targets],
        )
        rec(
            ctx,
            "e1_s1s_two_clip1_segments_have_selectors_one_per_track",
            len(s1_items) == 2
            and all(i.get("selector") and i.get("ambiguous") is False for i in s1_items)
            and sorted(i["selector"]["track"] for i in s1_items) == [0, 1],
            items=[(i.get("selector"), i.get("isTarget")) for i in s1_items],
        )
        s3_items = by_container(items, ids["s3Wt"])
        observe(
            ctx,
            "e1_identical_content_on_two_tracks_is_ambiguous",
            items=[
                {
                    "selector": i.get("selector"),
                    "ambiguous": i.get("ambiguous"),
                    "candidates": i.get("candidates"),
                    "isTarget": i.get("isTarget"),
                }
                for i in s3_items
            ],
        )
        rec(
            ctx,
            "e1_s3s_identical_pair_is_ambiguous_with_both_candidates_and_no_selector",
            len(s3_items) == 2
            and all(
                i.get("ambiguous") is True
                and i.get("selector") is None
                and i.get("isTarget") is False
                and sorted(c.get("track") for c in i.get("candidates") or []) == [0, 1]
                for i in s3_items
            ),
        )
        src_items = by_container(items, ids["srcClipWt"])
        rec(
            ctx,
            "e1_the_segment_inside_the_reel_clip_is_listed_with_a_clip_container",
            len(src_items) == 1
            and (src_items[0].get("container") or {}).get("type") == "PyClip"
            and ((src_items[0].get("container") or {}).get("parent") or {}).get("name") == "SRC",
            container=[i.get("container") for i in src_items],
        )
        e4 = await run_call(session, ctx, "e4_shared_sources_from_the_library_copy")
        from_library = items_of(e4)
        desktop_wts = {ids[k] for k in ("s1Wt", "s2Wt", "s3Wt", "s1hWt", "srcClipWt")}
        leaked = [
            i
            for i in from_library
            if ((i.get("container") or {}).get("selector") or {}).get("id") in desktop_wts
        ]
        observe(
            ctx,
            "e4_what_shared_sources_returns_from_a_segment_of_the_library_copy",
            total=_result_of(e4).get("total"),
            items=[
                {
                    "container": i.get("container"),
                    "selector": i.get("selector"),
                    "isTarget": i.get("isTarget"),
                    "ambiguous": i.get("ambiguous"),
                }
                for i in from_library
            ],
        )
        rec(
            ctx,
            "e4_from_the_library_copy_no_desktop_container_comes_back",
            e4.get("ok") is True and leaked == [],
            leaked=[i.get("container") for i in leaked],
            error=e4.get("error"),
        )

        resolved: list[dict[str, Any]] = []
        for item in items:
            selector = item.get("selector")
            if not selector or not _is_ours(ctx, item):
                continue
            ids["selector"] = selector
            info = await run_call(session, ctx, "e2_info_of_a_returned_selector")
            resolved.append(
                {
                    "ok": info.get("ok") is True
                    and _result_of(info).get("sourceName") == "clip1"
                    and (_result_of(info).get("recordIn") or {}).get("frame")
                    == (item.get("recordIn") or {}).get("frame"),
                    "selector": selector,
                    "error": info.get("error"),
                }
            )
        rec(
            ctx,
            "e2_every_selector_this_run_got_back_resolves_through_segment_info",
            len(resolved) == 5 and all(r["ok"] for r in resolved),
            resolved=resolved,
        )
        e3 = await run_call(session, ctx, "e1_shared_sources_by_index")
        value = _result_of(e3)
        rec(
            ctx,
            "e3_the_index_form_with_limit_1_pages",
            e3.get("ok") is True
            and value.get("shown") == 1
            and value.get("truncated") is True
            and value.get("total") == total,
            total=value.get("total"),
            error=e3.get("error"),
        )


def _is_ours(ctx: Ctx, item: dict[str, Any]) -> bool:
    """An item whose container is one this run made (others are other work in the project)."""
    ids = ctx.ids
    wiretaps = {ids["s1Wt"], ids["s2Wt"], ids["s3Wt"], ids["s1hWt"], ids["srcClipWt"]}
    return ((item.get("container") or {}).get("selector") or {}).get("id") in wiretaps


# -- f: find / info


def items_of(env: dict[str, Any]) -> list[dict[str, Any]]:
    value = _result_of(env).get("items")
    return value if isinstance(value, list) else []


async def stage_f(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31a-f") as session:
        f1 = await run_call(session, ctx, "f1_find_name_everywhere")
        parents = sorted(str((i.get("parent") or {}).get("name")) for i in items_of(f1))
        rec(
            ctx,
            "f1_a_name_is_found_in_the_desktop_and_the_library_together",
            f1.get("ok") is True
            and _result_of(f1).get("total") == 2
            and parents == ["LR", "SEQ"]
            and all(i.get("type") == "PySequence" and i.get("selector") for i in items_of(f1)),
            parents=parents,
            error=f1.get("error"),
        )
        f2d = await run_call(session, ctx, "f2_find_name_in_the_desktop_reel")
        f2l = await run_call(session, ctx, "f2_find_name_in_the_library_reel")
        rec(
            ctx,
            "f2_parent_keeps_only_that_reels_entry",
            [(i.get("uid"), (i.get("parent") or {}).get("name")) for i in items_of(f2d)]
            == [(ids["s1Uid"], "SEQ")]
            and [(i.get("parent") or {}).get("name") for i in items_of(f2l)] == ["LR"]
            and [i.get("selector") for i in items_of(f2l)] == [wt(ids["s1cWt"])],
            desktop=items_of(f2d),
            library=items_of(f2l),
            errorDesktop=f2d.get("error"),
            errorLibrary=f2l.get("error"),
        )
        f3 = await run_call(session, ctx, "f3_find_name_with_types")
        rec(
            ctx,
            "f3_types_filters_the_matches_out",
            f3.get("ok") is True and _result_of(f3).get("total") == 0 and items_of(f3) == [],
            error=f3.get("error"),
        )
        f4 = await run_call(session, ctx, "f4_find_uid")
        rec(
            ctx,
            "f4_a_uid_finds_one_entry",
            f4.get("ok") is True
            and [(i.get("name"), i.get("uid")) for i in items_of(f4)]
            == [(ids["s1Name"], ids["s1Uid"])],
            items=items_of(f4),
            error=f4.get("error"),
        )
        unknown_uid = await run_call(session, ctx, "f4_find_unknown_uid")
        unknown_name = await run_call(session, ctx, "f4_find_unknown_name")
        rec(
            ctx,
            "f4_an_unknown_uid_and_an_unknown_name_are_empty_pages_not_errors",
            unknown_uid.get("ok") is True
            and unknown_name.get("ok") is True
            and items_of(unknown_uid) == []
            and items_of(unknown_name) == []
            and _result_of(unknown_uid).get("total") == 0,
            errorUid=unknown_uid.get("error"),
            errorName=unknown_name.get("error"),
        )
        refused = await run_call(session, ctx, "f4_find_name_and_uid_refused")
        rec(
            ctx,
            "f4_name_and_uid_together_are_invalid_args",
            expect_error(refused, "INVALID_ARGS"),
            error=refused.get("error"),
        )
        await stage_f_info(session, ctx, items_of(f1))


async def stage_f_info(session: Any, ctx: Ctx, found: list[dict[str, Any]]) -> None:
    ids = ctx.ids
    roundtrip = []
    for item in found:
        ids["infoWt"] = (item.get("selector") or {}).get("id")
        env = await run_call(session, ctx, "f5_info")
        roundtrip.append(
            env.get("ok") is True
            and _result_of(env).get("uid") == item.get("uid")
            and _result_of(env).get("name") == item.get("name")
        )
    rec(
        ctx,
        "f5_every_selector_desktop_find_returned_resolves_through_desktop_info",
        len(roundtrip) == 2 and all(roundtrip),
        roundtrip=roundtrip,
    )
    for tail, key, kind in (
        ("clip", "srcClipWt", "media"),
        ("s1", "s1Wt", "media"),
        ("s1h", "s1hWt", "media"),
        ("s3", "s3Wt", "media"),
        ("reel_seq", "seqReelWt", "container"),
        ("reel_src", "srcWt", "container"),
        ("library", "libWt", "container"),
        ("reel_group", "rgWt", "container"),
    ):
        ids["infoWt"] = ids[key]
        env = await run_call(session, ctx, "f5_info")
        truth = await tx_val(ctx, fill(ENTRY_CODE, WT=ids[key]), "p31a_entry")
        got = _result_of(env)
        rec(
            ctx,
            f"f5_desktop_info_of_{tail}_equals_what_transport_read",
            env.get("ok") is True and isinstance(truth, dict) and info_matches(got, truth, kind),
            api={k: got.get(k) for k in sorted(got)},
            transport=truth,
            error=env.get("error"),
        )
    info_s1h = await tx_val(ctx, fill(ENTRY_CODE, WT=ids["s1hWt"]), "p31a_entry_s1h")
    observe(ctx, "f5_s1h_start_values_as_transport_reads_them", transport=info_s1h)


def info_matches(got: dict[str, Any], truth: dict[str, Any], kind: str) -> bool:
    same = all(got.get(k) == truth.get(k) for k in ("type", "name", "uid"))
    same = same and (got.get("parent") or {}) == (truth.get("parent") or {})
    if kind == "container":
        return same and got.get("childCounts") == truth.get("childCounts")

    def frames(value: Any) -> Any:
        return value.get("frame") if isinstance(value, dict) else value

    return (
        same
        and frames(got.get("duration")) == truth.get("duration")
        and got.get("startFrame") == truth.get("startFrame")
        and frames(got.get("startTime")) == truth.get("startTime")
        and all(
            got.get(k) == truth.get(k)
            for k in (
                "frameRate",
                "width",
                "height",
                "bitDepth",
                "scanMode",
                "cached",
                "unlinked",
                "versions",
                "audioTracks",
            )
        )
    )


# -- h: timeline.info, segment.info, Flame object values


def looks_like_a_repr(value: Any) -> bool:
    """A string that is a tuple / list / object printed (`(0.0, 0.0, 0.0)`, `[]`, `<flame...>`)."""
    return isinstance(value, str) and value[:1] in ("(", "[", "<")


async def stage_h(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31a-h") as session:
        for tail, key, frame in (("s1", "s1Wt", 0), ("s1h", "s1hWt", START_FRAME)):
            env = await run_call(session, ctx, f"h1_timeline_info_{tail}")
            got = _result_of(env)
            truth = await tx_val(ctx, fill(ENTRY_CODE, WT=ids[key]), f"p31a_entry_{tail}")
            start = got.get("startTime")
            rec(
                ctx,
                f"h1_timeline_info_of_{tail}_reads_start_time_size_and_audio",
                env.get("ok") is True
                and isinstance(truth, dict)
                and isinstance(start, dict)
                and start.get("frame") == frame
                and got.get("startFrame") == truth.get("startFrame")
                and got.get("width") == truth.get("width")
                and got.get("height") == truth.get("height")
                and got.get("audioTracks") == truth.get("audioTracks")
                and {"name", "duration", "frameRate", "versionCount", "versions"} <= set(got),
                api={k: got.get(k) for k in ("startTime", "startFrame", "width", "height")}
                | {"audioTracks": got.get("audioTracks")},
                transport=truth,
                error=env.get("error"),
            )
        listing = await run_call(session, ctx, "a1_list_segments")
        listed = {i.get("index"): i for i in items_of(listing)}
        for tail, index_key in (("clip2", "c2Idx"), ("letterbox", "l48Idx"), ("gap", "gapIdx")):
            env = await run_call(session, ctx, f"h2_segment_info_{tail}")
            got = _result_of(env)
            same = listed.get(ids[index_key], {})
            keys = ("type", "isGap", "head", "tail", "effects")
            rec(
                ctx,
                f"h2_segment_info_of_{tail}_has_the_same_type_head_tail_effects_as_list_segments",
                env.get("ok") is True
                and bool(same)
                and all(got.get(k) == same.get(k) for k in keys),
                info={k: got.get(k) for k in keys},
                listed={k: same.get(k) for k in keys},
                error=env.get("error"),
            )
        info = await run_call(session, ctx, "h2_segment_info_clip2")
        attrs = _result_of(info).get("attributes") or {}
        got_attrs = await run_call(session, ctx, "h3_segment_get_attributes")
        values = {
            k: v.get("value")
            for k, v in (_result_of(got_attrs).get("attributes") or {}).items()
            if isinstance(v, dict) and "value" in v
        }
        rec(
            ctx,
            "h3_segment_info_attributes_are_structure_and_equal_get_attributes_values",
            info.get("ok") is True
            and got_attrs.get("ok") is True
            and isinstance(attrs, dict)
            and not any(looks_like_a_repr(v) for v in attrs.values())
            and all(attrs.get(k) == v for k, v in values.items() if k in attrs),
            colour=attrs.get("colour"),
            tags=attrs.get("tags"),
            selectedMarkers=attrs.get("selected_markers"),
            asStrings=sorted(k for k, v in attrs.items() if looks_like_a_repr(v)),
            differing=sorted(k for k, v in values.items() if k in attrs and attrs[k] != v),
        )
        wrapped = await run_call(session, ctx, "h4_sequence_get_attributes")
        seq_attrs = _result_of(wrapped).get("attributes") or {}
        primary = (seq_attrs.get("primary_track") or {}).get("value")
        selected = (seq_attrs.get("selected_segments") or {}).get("value")
        rec(
            ctx,
            "h4_a_flame_object_value_is_its_type_not_a_repr_with_an_address",
            wrapped.get("ok") is True
            and isinstance(primary, dict)
            and primary.get("type") == "PyTrack"
            and "0x" not in json.dumps(seq_attrs)
            and (
                not isinstance(selected, list)
                or all(isinstance(i, dict) and "type" in i for i in selected)
            ),
            primary=primary,
            secondary=(seq_attrs.get("secondary_track") or {}).get("value"),
            subtitles=(seq_attrs.get("subtitles_track") or {}).get("value"),
            selected=selected,
            error=wrapped.get("error"),
        )


# -- g: the journal


async def stage_g(ctx: Ctx) -> None:
    written = written_groups(ctx)
    async with server_session(ctx, "p31a-g") as session:
        listing = await do(session, ctx, "journal.list", {"limit": 100})
        states = {i.get("group"): i.get("state") for i in _result_of(listing).get("items", [])}
        own = journal_states(ctx)
        rec(
            ctx,
            "g_every_group_of_this_run_is_landed_or_reverted",
            listing.get("ok") is True
            and len(own) == len(written)
            and set(own.values()) <= GOOD_STATES
            and all(states.get(w["group"]) in GOOD_STATES for w in written),
            groups=len(own),
            states=sorted(set(own.values())),
        )


# -- z / driver


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {
        "timeline.list_segments",
        "segment.info",
        "segment.set_attributes",
        "segment.list_effects",
        "segment.shared_sources",
        "desktop.find",
        "desktop.info",
        "journal.revert",
        "timeline.info",
        "segment.get_attributes",
        "desktop.get_attributes",
    }
    rec(
        ctx,
        "z_every_op_of_this_milestone_was_called",
        wanted <= set(used),
        missing=sorted(wanted - set(used)),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"P31A_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p31a_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p31a_left")
    ctx.results["cleanup"] = {"deleted": gone, "stillThere": left}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted",
        isinstance(gone, dict)
        and all(ok is True for rows_ in gone.values() for _n, ok in rows_)
        and left == {"reelGroups": [], "libraries": []},
        deleted=gone,
        stillThere=left,
    )
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "c1In" in ctx.ids and "s1cWt" in ctx.ids:
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("e", stage_e),
                ("f", stage_f),
                ("h", stage_h),
                ("g", stage_g),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p31a_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run

PLACEHOLDER_IDS: Final[dict[str, Any]] = {
    **{
        key: f"/wt/{key}"
        for key in (
            "s1Wt",
            "s2Wt",
            "s3Wt",
            "s1hWt",
            "s1cWt",
            "srcClipWt",
            "seqReelWt",
            "libReelWt",
            "libWt",
            "rgWt",
            "srcWt",
            "infoWt",
        )
    },
    **{
        key: 0
        for key in (
            "c1In",
            "c1Idx",
            "c2In",
            "c2Idx",
            "l48In",
            "l48Idx",
            "srcInnerIn",
            "s1hIn",
            "missingIn",
            "gapIdx",
        )
    },
    "s1Name": "P31A_dry_S1",
    "s1Uid": "uid-dry",
    "comment": "P31A dry",
    "selector": seg_idx("/wt/s1Wt", 0),
}


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    print("===== calls through flame_do (stage letter = the stage that makes it) =====")
    for call_id, (op_name, build) in CALLS.items():
        op = registry.get(op_name)
        problems: list[str] = []
        if op is None:
            print(f"NG {call_id}: unknown op {op_name}")
            failures += 1
            continue
        args = build(dict(PLACEHOLDER_IDS))
        validation = validate_op_args(op, args)
        refusal = None
        if validation.ok:
            checked = dict(validation.value or {})
            apply_param_defaults(op, checked)
            apply_limit_default(op, checked)
            if op.precheck is not None:
                refusal = op.precheck(checked, None)
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
        verdict = "NG " + "; ".join(problems) if problems else "OK"
        print(f"{verdict:3} {call_id}: {op_name}  args={args}")
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
    stages = sorted({cid[0] for cid in CALLS})
    print(f"===== stages with calls: {stages}; the run also has prepare, g and z =====")
    print(f"=== dry run: {'FAILED' if failures else 'OK'} ({failures} problem(s)) ===")
    return 1 if failures else 0


EXPECTED_VALIDATION_FAILURES: Final[frozenset[str]] = frozenset({"b2_index_and_record_in"})
EXPECTED_REFUSALS: Final[frozenset[str]] = frozenset({"f4_find_name_and_uid_refused"})


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
    journal_dir = OUTPUT_DIR / f"journal_p31a_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p3_1a_read_e2e_{ts}.json",
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
