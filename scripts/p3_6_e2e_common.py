"""Shared parts of the editing e2e (scripts/p3_6_edit_e2e.py): the calls made through the MCP
surface (one table the run and `--dry-run` read), the transport scaffolding -- which READS EVERY
STATE BACK INDEPENDENTLY of the ops under test (video rows with their source range and effect
types, the audio channels and their segments, a segment's connections, the stash library) -- the
row arithmetic the stages judge with, and the material (`prepare`). The stages are in
p3_6_e2e_delete.py (timeline.delete_segments), p3_6_e2e_move.py (timeline.move_track),
p3_6_e2e_audio.py (reading the audio, timeline.shorten) and p3_6_e2e_compare.py
(timeline.compare, segment.connections, the read-only policy); the driver, the cleanup and
`--dry-run` are in p3_6_edit_e2e.py. The helpers of the earlier e2es are reused under the prefix
`P36E`."""

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
from p3_1a_read_e2e import PRELUDE, UID_CODE, fill, is_error, tx_val  # noqa: E402
from p3_2a_e2e_common import (  # noqa: E402
    PREPARE_CODE,
    S1_PUTS,
    checkpoint_records,
    name_of,
    seg_at,
    written_groups,
)
from p3_3_e2e_common import (  # noqa: E402
    LETTERBOX,
    OUTPUT_DIR,
    Results,
    ckpt_library,
    err_text,
    expect_error,
    fingerprint,
    make_seq,
    restored_wt,
    revert,
    sel,
    state_snapshot,
    tx_stash,
    tx_struct,
)
from p3_4_e2e_common import SEQ_FACTS_CODE, tx_seq  # noqa: E402

PREFIX: Final[str] = "P36E"
P2_MEDIA_DIR: Final[Path] = MEDIA_DIR / "p2"
AV: Final[str] = "p2_av72"
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("clip1", MEDIA_DIR / "clip1.mov"),
    ("clip2", MEDIA_DIR / "clip2.mov"),
    ("clip3", MEDIA_DIR / "clip3.mov"),
    ("p2_len12", P2_MEDIA_DIR / "p2_len12.mov"),
    ("p2_len48", P2_MEDIA_DIR / "p2_len48.mov"),
    ("p2_len72", P2_MEDIA_DIR / "p2_len72.mov"),
    (AV, P2_MEDIA_DIR / f"{AV}.mov"),
)
"""The material: scripts/make_clip_media.sh (the 24-frame clips), scripts/make_p2_media.sh
(p2_lenN has N frames) and scripts/make_p2_av_media.sh (p2_av72: 72 frames, PCM audio 48 kHz,
2 channels, as long as the picture)."""
S1_SHAPE: Final[tuple[tuple[tuple[str, int], ...], ...]] = (
    (("clip1", 0), ("clip2", 48), ("p2_len48", 72)),
    (("p2_len72", 0),),
)
"""`video_shape` of `make_s` (S1_PUTS)."""
CONTIGUOUS_SHAPE: Final[tuple[tuple[tuple[str, int], ...], ...]] = (
    (("clip1", 0), ("clip2", 24), ("p2_len48", 48)),
    (("p2_len72", 0),),
)
"""`video_shape` of `make_contiguous`."""
LEN12: Final[str] = "p2_len12"
AUDIO_SEGMENT: Final[str] = "Audio Segment"
GAP: Final[str] = "Gap"
LETTERBOX_FX: Final[tuple[tuple[int, str, str, dict[str, Any]], ...]] = (
    (0, "clip2", LETTERBOX, {"mode": "Custom", "ratio": 1.5}),
)
"""A Burn-in Letterbox on clip2 of track 0 of S1, so that a track move has an effect type to
carry."""
AV_PUTS: Final[tuple[tuple[int, str, int], ...]] = (
    (0, AV, 1),
    (1, "clip1", 1),
    (1, "clip2", 49),
)
"""(track, clip, relative start) of the sequences with sound: track 0 = p2_av72 0-71 (picture and
audio), track 1 = clip1 0-23, a Gap, clip2 48-71. The audio clip goes on track 0, where an
overwrite was measured to bring its audio in."""
CONTIGUOUS_PUTS: Final[tuple[tuple[int, str, int], ...]] = (
    (0, "clip1", 1),
    (0, "clip2", 25),
    (0, "p2_len48", 49),
    (1, "p2_len72", 1),
)
"""Track 0 = clip1 0-23, clip2 24-47, p2_len48 48-95 (nothing is a Gap, so a Gap that a lift
leaves is not next to another one); track 1 = p2_len72 0-71."""

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def declare(call_id: str, op: str, sample: dict[str, Any]) -> None:
    """`CALLS[call_id] = (op, build)`: `build(ids)` answers `ids["args"]` when a stage supplies the
    arguments and `sample` (a well-formed set, for `--dry-run`) otherwise."""

    def build(ids: dict[str, Any]) -> dict[str, Any]:
        args = ids.get("args")
        return dict(args) if isinstance(args, dict) else dict(sample)

    CALLS[call_id] = (op, build)


_SEQ = sel("/wt/s")
_SEG = seg_at(_SEQ, 0, 0)

declare(
    "d_delete",
    "timeline.delete_segments",
    {"sequence": _SEQ, "segments": [48], "track": 0, "expect_source_names": ["clip2"]},
)
declare(
    "d_delete_lift",
    "timeline.delete_segments",
    {"sequence": _SEQ, "segments": [24], "track": 0, "ripple": False},
)
declare(
    "d_delete_gap", "timeline.delete_segments", {"sequence": _SEQ, "segments": [24], "track": 0}
)
declare(
    "d_delete_refused", "timeline.delete_segments", {"sequence": _SEQ, "segments": [5], "track": 0}
)
declare("m_move", "timeline.move_track", {"sequence": _SEQ, "track": 1, "to": 0})
declare("m_move_refused", "timeline.move_track", {"sequence": _SEQ, "track": 1, "to": 0})
declare("a_info", "timeline.info", {"target": _SEQ})
declare(
    "a_list_audio",
    "timeline.list_segments",
    {"target": _SEQ, "media": "audio", "track": 0, "channel": 0},
)
declare(
    "a_list_refused",
    "timeline.list_segments",
    {"target": _SEQ, "media": "audio", "track": 0, "channel": 0},
)
declare("s_shorten", "timeline.shorten", {"sequence": _SEQ, "duration": 36})
declare("s_shorten_refused", "timeline.shorten", {"sequence": _SEQ, "duration": 36})
declare("q_compare", "timeline.compare", {"a": _SEQ, "b": sel("/wt/t")})
declare("c_connections", "segment.connections", {"target": _SEG})
declare("v_create_version", "timeline.create_version", {"sequence": _SEQ})
declare("v_create_track", "timeline.create_track", {"sequence": _SEQ})
declare(
    "y_readonly_delete", "timeline.delete_segments", {"sequence": _SEQ, "segments": [0], "track": 0}
)
declare("y_readonly_move", "timeline.move_track", {"sequence": _SEQ, "track": 1, "to": 0})
declare("y_readonly_shorten", "timeline.shorten", {"sequence": _SEQ, "duration": 10})
declare("y_readonly_compare", "timeline.compare", {"a": _SEQ, "b": sel("/wt/t")})
declare("y_readonly_connections", "segment.connections", {"target": _SEG})


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


# -- transport scaffolding (never through the op under test) ------------------------------------

FULL_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "def _fr(_x):",
        "    _x = _v(_x)",
        "    return _x.frame if type(_x).__name__ == 'PyTime' else _x",
        "def _vrows(_trk):",
        "    _out = []",
        "    for _s in list(_trk.segments):",
        "        _out.append({'type': _s.type, 'src': _s.source_name, 'rin': _s.record_in.frame,",
        "                     'rout': _s.record_out.frame, 'sin': _s.source_in.frame,",
        "                     'sout': _s.source_out.frame, 'fx': _fxrow(_s)})",
        "    return _out",
        "def _arows(_seq):",
        "    _counts = []",
        "    _rows = []",
        "    for _ai, _a in enumerate(list(_seq.audio_tracks)):",
        "        _chs = []",
        "        for _ci, _c in enumerate(list(_a.channels)):",
        "            _segs = list(_c.segments)",
        "            _chs.append(len(_segs))",
        "            for _si, _s in enumerate(_segs):",
        "                _rows.append({'track': _ai, 'channel': _ci, 'index': _si,",
        "                              'type': _s.type, 'src': _s.source_name,",
        "                              'rin': _s.record_in.frame, 'rout': _s.record_out.frame,",
        "                              'sin': _s.source_in.frame, 'sout': _s.source_out.frame})",
        "        _counts.append(_chs)",
        "    return {'channels': _counts, 'rows': _rows}",
        "if _o is None:",
        "    _result = None",
        "else:",
        "    _result = {",
        "        'type': type(_o).__name__,",
        "        'video': _g(lambda: [[_vrows(_t) for _t in list(_ve.tracks)]",
        "                             for _ve in list(_o.versions)]),",
        "        'audio': _g(lambda: _arows(_o)),",
        "        'duration': _g(lambda: _fr(_o.duration)),",
        "        'startTime': _g(lambda: _fr(_o.start_time)),",
        "    }",
    ]
)
"""Every video track of every version (type, source_name, record range, source range, effect
types) and every audio channel of every audio track (the same), by the API directly."""

CLIP_AUDIO_CODE: Final[str] = "\n".join(
    [
        "_c = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _c is None else _g(lambda: [",
        "    [len(list(_a.channels)), sum(len(list(_ch.segments)) for _ch in _a.channels)]",
        "    for _a in list(_c.audio_tracks)])",
    ]
)

CONNECTED_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_t = list(list(_o.versions)[0].tracks)[@TRACK@]",
        "_hit = [_s for _s in _t.segments if _s.record_in.frame == @RECORD_IN@]",
        "_result = None if not _hit else len(list(_hit[0].connected_segments()))",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "prepare": PREPARE_CODE,
    "seq_facts": SEQ_FACTS_CODE,
    "full": FULL_CODE,
    "clip_audio": CLIP_AUDIO_CODE,
    "connected": CONNECTED_CODE,
    "uid": UID_CODE,
}


async def tx_full(ctx: Ctx, wiretap_id: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(FULL_CODE, WT=wiretap_id), "p36e_full")
    return got if not is_error(got) and _has_video_rows(got) else None


def _has_video_rows(got: Any) -> bool:
    """True when `got` is a `FULL_CODE` answer whose video part was read (a list of versions of
    lists of tracks of lists of rows); a read that raised leaves a string there instead."""
    video = got.get("video") if isinstance(got, dict) else None
    return (
        isinstance(video, list)
        and bool(video)
        and all(
            isinstance(version, list) and all(isinstance(track, list) for track in version)
            for version in video
        )
    )


async def tx_connected(ctx: Ctx, wiretap_id: str, track: int, record_in: int) -> int | None:
    code = fill(CONNECTED_CODE, WT=wiretap_id, TRACK=track, RECORD_IN=record_in)
    got = await tx_val(ctx, code, "p36e_connected")
    return None if is_error(got) or not isinstance(got, int) else got


# -- rows: [type, record_in, record_out, source_in, source_out, source_name, (effect types)] ----


def vnorm(row: dict[str, Any]) -> list[Any]:
    """A video row as it is compared: a Gap by type and record range only (its source range and
    name change without the edit changing, measured), media by everything but its name."""
    if row["type"] == GAP:
        return [GAP, row["rin"], row["rout"]]
    return [row["type"], row["rin"], row["rout"], row["sin"], row["sout"], row["src"], row["fx"]]


def anorm(row: dict[str, Any]) -> list[Any]:
    if row["type"] == GAP:
        return [GAP, row["rin"], row["rout"]]
    return [row["type"], row["rin"], row["rout"], row["sin"], row["sout"], row["src"]]


def raw_track(full: dict[str, Any] | None, track: int, version: int = 0) -> list[dict[str, Any]]:
    """The rows of one video track as transport read them (before they are normalised)."""
    return list(full["video"][version][track]) if readable(full) else []


def video_track(full: dict[str, Any] | None, track: int, version: int = 0) -> list[list[Any]]:
    if not readable(full):
        return []
    return [vnorm(r) for r in full["video"][version][track]]


def video_tracks(full: dict[str, Any] | None, version: int = 0) -> list[list[list[Any]]]:
    count = len(full["video"][version]) if readable(full) else 0
    return [video_track(full, t, version) for t in range(count)]


def readable(full: dict[str, Any] | None) -> bool:
    """True when `tx_full` read the video rows. `video_track` and `video_tracks` answer [] for a
    read that failed, so a comparison of two failed reads is [] == [] unless this is asked first."""
    return full is not None and _has_video_rows(full)


def same_video(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """The video rows of two reads are equal; False when either could not be read."""
    return readable(a) and readable(b) and video_tracks(a) == video_tracks(b)


def video_shape(full: dict[str, Any] | None) -> tuple[tuple[tuple[str, int], ...], ...] | None:
    """(source name, record_in) of the media of every video track of version 0, the Gaps left out
    (whether the fake and a real Flame show a Gap between two puts differs, and a Gap at the end
    of a track is not part of what was made). None when the read failed."""
    if not readable(full):
        return None
    return tuple(tuple((str(r[5]), r[1]) for r in media_only(rows)) for rows in video_tracks(full))


def audio_rows(full: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """The audio rows transport read; None when they could not be read."""
    audio = (full or {}).get("audio")
    return audio["rows"] if isinstance(audio, dict) else None


def audio_channels(full: dict[str, Any] | None) -> dict[tuple[int, int], list[list[Any]]]:
    """{(audio track, channel): rows} (a channel with no segment is there with []), or {} when the
    audio could not be read."""
    audio = (full or {}).get("audio")
    if not isinstance(audio, dict):
        return {}
    found: dict[tuple[int, int], list[list[Any]]] = {
        (ai, ci): [] for ai, chans in enumerate(audio["channels"]) for ci in range(len(chans))
    }
    for row in audio["rows"]:
        found[(row["track"], row["channel"])].append(anorm(row))
    return found


def media_only(rows: list[list[Any]]) -> list[list[Any]]:
    return [r for r in rows if r[0] != GAP]


def strip_trailing_gaps(rows: list[list[Any]]) -> list[list[Any]]:
    """A track moved by `timeline.move_track` is rebuilt on a new track that is one Gap as long as
    the sequence, so the Gap(s) at its end are left out when two tracks are compared."""
    kept = list(rows)
    while kept and kept[-1][0] == GAP:
        kept.pop()
    return kept


def media_after_delete(
    rows: list[dict[str, Any]], record_ins: list[int], *, ripple: bool
) -> list[list[Any]]:
    """The media rows a track should have left after the segments at `record_ins` are taken out:
    the others, each moved left by what was removed in front of it when the hole is closed."""
    expected: list[list[Any]] = []
    shift = 0
    for row in rows:
        if row["rin"] in record_ins:
            shift += (row["rout"] - row["rin"] + 1) if ripple else 0
        elif row["type"] != GAP:
            moved = vnorm(row)
            moved[1] -= shift
            moved[2] -= shift
            expected.append(moved)
    return expected


def end_after_delete(
    full: dict[str, Any] | None, record_ins: list[int], *, track: int = 0, version: int = 0
) -> int | None:
    """The frame after the last content the sequence should have once the segments at `record_ins`
    are ripple-deleted from `track` (measured): the longest content (everything but a
    Gap) of the target track as it is left (the later media moved left), of every other video
    track of the version and of every audio channel, as `full` read them before the call. None
    when the read failed or there is no content at all."""
    if not readable(full):
        return None
    ends: list[int] = []
    for ti in range(len(full["video"][version])):
        rows = raw_track(full, ti, version)
        if ti == track:
            ends += [r[2] + 1 for r in media_after_delete(rows, record_ins, ripple=True)]
        else:
            ends += [r["rout"] + 1 for r in rows if r["type"] != GAP]
    for rows in audio_channels(full).values():
        ends += [r[2] + 1 for r in rows if r[0] != GAP]
    return max(ends, default=None)


def gaps_follow(full: dict[str, Any] | None, end: int | None) -> bool:
    """After a ripple delete the sequence is `end - start` frames long and the Gap at the end of
    every video track of version 0 and of every audio channel, where a track has one, ends at
    `end - 1` (a track that ends in content at the new end has none)."""
    if not readable(full) or end is None:
        return False
    start = full.get("startTime")
    duration = full.get("duration")
    if not isinstance(duration, int) or duration != end - (start if isinstance(start, int) else 0):
        return False
    tracks = [video_track(full, t) for t in range(len(full["video"][0]))]
    tracks += list(audio_channels(full).values())
    return all(not rows or rows[-1][0] != GAP or rows[-1][2] == end - 1 for rows in tracks)


def shorten_expect(rows: list[list[Any]], end: int) -> list[list[Any]]:
    """What a track should hold after `timeline.shorten` to the absolute frame `end`: the rows that
    end before it as they were, a row that crosses it cut to end - 1 (its source_out is not
    compared: it is set to None here and in `masked`), the rows from `end` on gone."""
    expected: list[list[Any]] = []
    for row in rows:
        if row[2] < end:
            expected.append(list(row))
        elif row[1] < end:
            piece = list(row)
            piece[2] = end - 1
            expected.append(piece)
    return masked(expected, end)


def masked(rows: list[list[Any]], end: int) -> list[list[Any]]:
    out = []
    for row in rows:
        row = list(row)
        if row[0] != GAP and row[2] == end - 1:
            row[4] = None
        out.append(row)
    return out


def frame_of(value: Any) -> Any:
    """The frame of a PyTime an op answers as {timecode, frame}; any other value as it is."""
    return value.get("frame") if isinstance(value, dict) else value


# -- the stash and the journal ---------------------------------------------------------------


async def stash_entries(ctx: Ctx) -> list[list[Any]]:
    return list((await tx_stash(ctx) or {}).get("entries") or [])


def added_since(before: list[list[Any]], now: list[list[Any]]) -> list[list[Any]]:
    seen = {e[3] for e in before}
    return [e for e in now if e[3] not in seen]


def checkpoint_stash_uid(ctx: Ctx, env: dict[str, Any]) -> str | None:
    """The uid of the stashed copy that the journal says was this call's checkpoint."""
    wanted = (env.get("journal") or {}).get("checkpoint")
    for record in checkpoint_records(ctx):
        if record.get("id") == wanted:
            uid = (record.get("stash") or {}).get("uid")
            return uid if isinstance(uid, str) else None
    return None


async def revert_if_journaled(
    session: Any, ctx: Ctx, env: dict[str, Any], case: dict[str, Any]
) -> dict[str, Any] | None:
    """Reverts a call that may have changed the sequence although it did not succeed (a
    FLAME_API_ERROR is 'still revertible'); None when it left no checkpoint to revert to."""
    journal = env.get("journal") or {}
    if not (journal.get("group") and journal.get("checkpoint")):
        return None
    out = await revert(session, ctx, env)
    case["wt"] = restored_wt(out) or case["wt"]
    return out


# -- material ---------------------------------------------------------------------------------


async def make_s(ctx: Ctx, tag: str) -> Any:
    """A fresh sequence of this run (`P36E_<ts>_<tag>`): track 0 = clip1 0-23, a Gap, clip2 48-71,
    p2_len48 72-119; track 1 = p2_len72 0-71."""
    return await make_seq(ctx, tag, S1_PUTS)


async def make_av(ctx: Ctx, tag: str, puts: tuple[tuple[int, str, int], ...] = AV_PUTS) -> Any:
    return await make_seq(ctx, tag, puts)


async def make_contiguous(ctx: Ctx, tag: str) -> Any:
    return await make_seq(ctx, tag, CONTIGUOUS_PUTS)


async def make_effect(ctx: Ctx, tag: str) -> Any:
    """S1 with a Burn-in Letterbox on clip2 of track 0."""
    return await make_seq(ctx, tag, S1_PUTS, fxs=LETTERBOX_FX)


async def prepare(ctx: Ctx) -> None:
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
        rec(
            ctx,
            "prepare_material_files_exist",
            False,
            missing=missing,
            hint="scripts/make_p2_av_media.sh makes p2_av72.mov",
        )
        return
    ids = ctx.ids
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
        label="p36e_prepare",
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
    got = await tx_val(ctx, fill(CLIP_AUDIO_CODE, WT=ids["clipWt"][AV]), "p36e_clip_audio")
    has_audio = (
        not is_error(got)
        and isinstance(got, list)
        and any(isinstance(t, list) and t[1] > 0 for t in got)
    )
    ids["avAudio"] = has_audio
    rec(ctx, "prepare_the_audio_clip_was_imported_with_audio", has_audio, audio=got)


__all__ = [
    "AUDIO_SEGMENT",
    "AV",
    "AV_PUTS",
    "CALLS",
    "CONTIGUOUS_PUTS",
    "CONTIGUOUS_SHAPE",
    "GAP",
    "LEN12",
    "LETTERBOX",
    "OUTPUT_DIR",
    "PREFIX",
    "S1_PUTS",
    "S1_SHAPE",
    "TEMPLATES",
    "Ctx",
    "Results",
    "added_since",
    "anorm",
    "audio_channels",
    "audio_rows",
    "checkpoint_stash_uid",
    "ckpt_library",
    "end_after_delete",
    "err_text",
    "expect_error",
    "fingerprint",
    "frame_of",
    "gaps_follow",
    "make_av",
    "make_contiguous",
    "make_effect",
    "make_s",
    "masked",
    "media_after_delete",
    "media_only",
    "prepare",
    "raw_track",
    "readable",
    "rec",
    "restored_wt",
    "revert",
    "revert_if_journaled",
    "run_call",
    "same_video",
    "seg_at",
    "sel",
    "shorten_expect",
    "stash_entries",
    "state_snapshot",
    "strip_trailing_gaps",
    "tx_connected",
    "tx_full",
    "tx_seq",
    "tx_struct",
    "video_shape",
    "video_track",
    "video_tracks",
    "vnorm",
]
