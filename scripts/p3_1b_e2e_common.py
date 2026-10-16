"""Shared parts of the import / duplicate / move real-Flame e2e (scripts/p3_1b_write_e2e.py): the
calls, the transport scaffolding (which READS STATE BACK INDEPENDENTLY of the ops under test), the
helpers the stages use, and the material (`prepare`). The stages are in p3_1b_e2e_import_dup.py and
p3_1b_e2e_move.py; the driver, the plan / policy stage, the journal stage, the cleanup and
`--dry-run` are in p3_1b_write_e2e.py."""

from __future__ import annotations

import copy
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

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
    COPY_CODE,
    MAKE_SEQUENCE_CODE,
    PRELUDE,
    UID_CODE,
    bit_depth_ok,
    fill,
    is_error,
    items_of,
    tx_val,
    wt,
)

from flame_mcp.journal import Journal, new_id, now_iso  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("clip1", MEDIA_DIR / "clip1.mov"),
    ("clip2", MEDIA_DIR / "clip2.mov"),
    ("clip3", MEDIA_DIR / "clip3.mov"),
)
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed"}
)
S1_PUTS: Final[tuple[tuple[int, str, int], ...]] = (
    (0, "clip1", 1),
    (0, "clip2", 49),
    (1, "clip1", 25),
)
"""(track, clip, relative start) of the sequence S1 (`overwrite(clip, PyTime(n), track)`: a PyTime
of an integer only is relative, 1 = the first frame)."""
REEL_NAMES: Final[tuple[str, ...]] = ("SRC", "SEQ", "A", "B", "C", "D", "E", "DREL")
SEQUENCE_TEMPLATE: Final[str] = MAKE_SEQUENCE_CODE
"""What makes a sequence (tests/test_p3_1b_e2e_inprocess.py swaps in one the fake can run)."""

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one)

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def call_for(call_id: str, op: str) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Any]:
    def register(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


@call_for("a1_import_one", "mediahub.import")
def _a1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["path1"]], "destination": wt(ids["aWt"])}


@call_for("a2_import_three", "mediahub.import")
def _a2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["path1"], ids["path2"], ids["path3"]], "destination": wt(ids["bWt"])}


@call_for("a3_import_directory", "mediahub.import")
def _a3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["dir2"]], "destination": wt(ids["cWt"])}


@call_for("a4_import_into_a_library_reel", "mediahub.import")
def _a4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["path2"]], "destination": wt(ids["lrWt"])}


@call_for("a5_import_into_a_library", "mediahub.import")
def _a5(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["path3"]], "destination": wt(ids["libWt"])}


@call_for("a6_import_into_a_folder", "mediahub.import")
def _a6(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["path3"]], "destination": wt(ids["folderWt"])}


@call_for("a7_import_missing_path", "mediahub.import")
def _a7(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["path1"], ids["missing"]], "destination": wt(ids["aWt"])}


@call_for("a8_import_zero_byte", "mediahub.import")
def _a8(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["empty"]], "destination": wt(ids["aWt"])}


@call_for("a9_import_relative_path", "mediahub.import")
def _a9(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": ["clip1.mov"], "destination": wt(ids["aWt"])}


@call_for("a10_import_pattern_that_matches_nothing", "mediahub.import")
def _a10(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["ghost"]], "destination": wt(ids["aWt"])}


@call_for("a11_import_into_a_batch_reel", "mediahub.import")
def _a11(ids: dict[str, Any]) -> dict[str, Any]:
    return {"paths": [ids["path1"]], "destination": wt(ids["batchReelWt"])}


@call_for("b1_duplicate_clip", "desktop.duplicate")
def _b1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["clip1Wt"])}


@call_for("b2_duplicate_sequence", "desktop.duplicate")
def _b2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["s1Wt"])}


@call_for("b3_duplicate_reel", "desktop.duplicate")
def _b3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["drelWt"])}


@call_for("b6_duplicate_a_reel_group", "desktop.duplicate")
def _b6a(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["rgWt"])}


@call_for("b6_duplicate_a_library", "desktop.duplicate")
def _b6b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["libWt"])}


@call_for("b6_duplicate_a_batch_reel", "desktop.duplicate")
def _b6c(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["batchReelWt"])}


@call_for("b7_duplicate_a_library_clip", "desktop.duplicate")
def _b7(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": wt(ids["libClipWt"])}


@call_for("c1_move_one", "desktop.move")
def _c1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [wt(ids["mvSrcWt"])], "destination": wt(ids["mvDestWt"])}


@call_for("c9_move_into_a_batch_reel", "desktop.move")
def _c9a(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [wt(ids["mvSrcWt"])], "destination": wt(ids["batchReelWt"])}


@call_for("c9_move_out_of_a_batch_reel", "desktop.move")
def _c9b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [wt(ids["batchClipWt"])], "destination": wt(ids["mvDestWt"])}


@call_for("c10_move_into_its_own_reel", "desktop.move")
def _c10(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [wt(ids["mvSrcWt"])], "destination": wt(ids["mvSrcReelWt"])}


@call_for("c11_move_to_a_selection", "desktop.move")
def _c11(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [wt(ids["mvSrcWt"])], "destination": {"kind": "selection"}}


@call_for("c12_move_a_reel", "desktop.move")
def _c12(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [wt(ids["mvSrcReelWt"])], "destination": wt(ids["mvDestWt"])}


@call_for("e1_plan_import", "plan.run")
def _e1a(ids: dict[str, Any]) -> dict[str, Any]:
    step = {"op": "mediahub.import", "args": _a1(ids)}
    return {"steps": [step]}


@call_for("e1_plan_duplicate", "plan.run")
def _e1b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"steps": [{"op": "desktop.duplicate", "args": _b1(ids)}]}


@call_for("e1_plan_move", "plan.run")
def _e1c(ids: dict[str, Any]) -> dict[str, Any]:
    return {"steps": [{"op": "desktop.move", "args": _c1(ids)}]}


# -- transport scaffolding (never through the op under test)

PREPARE_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_rg = _ws.desktop.create_reel_group(@RG@)",
        "_reels = {}",
        "for _n in @REELS@:",
        "    _reels[_n] = _rg.create_reel(_n).get_wiretap_node_id()",
        "_lib = _ws.create_library(@LIB@)",
        "_lr = _lib.create_reel('LR')",
        "_lr2 = _lib.create_reel('LR2')",
        "_src = flame.find_by_wiretap_node_id(_reels['SRC'])",
        "_clips = {}",
        "for _name, _p in @PATHS@:",
        "    _c = flame.import_clips(_p, _src)[0]",
        "    _clips[_name] = {'name': _v(_c.name), 'wt': _c.get_wiretap_node_id(),",
        "                     'uid': _v(_c.uid)}",
        "_c1 = flame.find_by_wiretap_node_id(_clips['clip1']['wt'])",
        "_result = {",
        "    'rgWt': _rg.get_wiretap_node_id(), 'reels': _reels,",
        "    'libWt': _lib.get_wiretap_node_id(), 'lrWt': _lr.get_wiretap_node_id(),",
        "    'lr2Wt': _lr2.get_wiretap_node_id(), 'clips': _clips,",
        "    'fps': _c1.frame_rate, 'width': _c1.width, 'height': _c1.height,",
        "}",
    ]
)

IMPORT_CODE: Final[str] = "\n".join(
    [
        "_reel = flame.find_by_wiretap_node_id(@REEL@)",
        "_made = flame.import_clips(@PATH@, _reel)",
        "_result = [",
        "    [type(_c).__name__, _v(_c.name), _v(_c.uid), _g(lambda: _c.get_wiretap_node_id())]",
        "    for _c in _made]",
    ]
)

LISTING_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else [",
        "    [type(_c).__name__, _v(_c.name), _v(_c.uid), _g(lambda: _c.get_wiretap_node_id())]",
        "    for _c in _o.children]",
    ]
)

FIND_WT_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else [type(_o).__name__, _v(_o.name), _v(_o.uid)]",
    ]
)

REEL_NAMES_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else [_v(_r.name) for _r in _o.reels]",
    ]
)

BATCH_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = _d.create_batch_group(@NAME@, nb_reels=1, nb_shelf_reels=1)",
        "_r = _bg.reels[0]",
        "_wt = _g(lambda: _r.get_wiretap_node_id())",
        "_clip = None",
        "if not (isinstance(_wt, str) and _wt.startswith('ERR:')):",
        "    _src = flame.find_by_wiretap_node_id(@SRC@)",
        "    _made = flame.media_panel.copy(_src, _r)",
        "    _clip = _g(lambda: _made[0].get_wiretap_node_id())",
        "_result = {'uid': _v(_bg.uid), 'name': _v(_bg.name), 'reelWt': _wt, 'clipWt': _clip}",
    ]
)

FOLDER_CODE: Final[str] = "\n".join(
    [
        "_lib = flame.find_by_wiretap_node_id(@LIB@)",
        "_f = _lib.create_folder(@NAME@)",
        "_result = _f.get_wiretap_node_id()",
    ]
)

DELETE_UID_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_uid(@UID@)",
        "_result = None if _o is None else flame.delete(_o, confirm=False)",
    ]
)

DELETE_WT_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else flame.delete(_o, confirm=False)",
    ]
)

RENAME_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_uid(@UID@)",
        "_o.name.set_value(@NAME@)",
        "_result = _v(_o.name)",
    ]
)

MAKE_REEL_CODE: Final[str] = "\n".join(
    [
        "_rg = flame.find_by_wiretap_node_id(@RG@)",
        "_result = _rg.create_reel(@NAME@).get_wiretap_node_id()",
    ]
)

CLEANUP_CODE: Final[str] = "\n".join(
    [
        "_p = @P@",
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_gone = {'reelGroups': [], 'libraries': [], 'batchGroups': []}",
        "for _g2 in list(_d.reel_groups):",
        "    if _v(_g2.name).startswith(_p):",
        "        _gone['reelGroups'].append([_v(_g2.name), flame.delete(_g2, confirm=False)])",
        "for _b in list(_d.batch_groups):",
        "    if _v(_b.name).startswith(_p):",
        "        _gone['batchGroups'].append([_v(_b.name), flame.delete(_b, confirm=False)])",
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
        "    'batchGroups': [_v(_b.name) for _b in _d.batch_groups if _v(_b.name).startswith(_p)],",
        "    'libraries': [_v(_l.name) for _l in",
        "                  flame.projects.current_project.current_workspace.libraries",
        "                  if _v(_l.name).startswith(_p)],",
        "}",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "prepare": PREPARE_CODE,
    "import": IMPORT_CODE,
    "listing": LISTING_CODE,
    "find_wt": FIND_WT_CODE,
    "reel_names": REEL_NAMES_CODE,
    "batch": BATCH_CODE,
    "folder": FOLDER_CODE,
    "delete_uid": DELETE_UID_CODE,
    "delete_wt": DELETE_WT_CODE,
    "rename": RENAME_CODE,
    "make_reel": MAKE_REEL_CODE,
    "copy": COPY_CODE,
    "sequence": SEQUENCE_TEMPLATE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def tx_list(ctx: Ctx, wiretap_id: str) -> list[list[Any]] | None:
    got = await tx_val(ctx, fill(LISTING_CODE, WT=wiretap_id), "p31b_listing")
    return None if is_error(got) or got is None else got


async def tx_find_uid(ctx: Ctx, uid: str) -> Any:
    return await tx_val(ctx, fill(UID_CODE, UID=uid), "p31b_find_uid")


async def tx_find_wt(ctx: Ctx, wiretap_id: str) -> Any:
    return await tx_val(ctx, fill(FIND_WT_CODE, WT=wiretap_id), "p31b_find_wt")


def uids_of(rows: list[list[Any]] | None) -> list[str]:
    return [r[2] for r in rows or []]


def names_of(rows: list[list[Any]] | None) -> list[str]:
    return [r[1] for r in rows or []]


def added(before: list[list[Any]] | None, after: list[list[Any]] | None) -> list[list[Any]]:
    known = set(uids_of(before))
    return [r for r in after or [] if r[2] not in known]


def name_of(ctx: Ctx, tail: str) -> str:
    return f"P31B_{ctx.ts}_{tail}"


def sel(wiretap_id: str) -> dict[str, Any]:
    return {"kind": "wiretap", "id": wiretap_id}


def err_text(env: dict[str, Any]) -> str:
    return str(env.get("error") or "")


def expect_error(env: dict[str, Any], code: str) -> bool:
    return env.get("ok") is False and env.get("errorCode") == code


def written_groups(ctx: Ctx) -> list[dict[str, str]]:
    return ctx.ids.setdefault("writeGroups", [])


async def run_call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    env = await do(session, ctx, op, build({**ctx.ids, **extra}))
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


async def revert(session: Any, ctx: Ctx, env: dict[str, Any]) -> dict[str, Any]:
    out = await do(
        session, ctx, "journal.revert", {"group": (env.get("journal") or {}).get("group")}
    )
    group = (out.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": "journal.revert", "group": group})
    return out


def groups_now(ctx: Ctx) -> int:
    return len(journal_states(ctx))


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


# -- prepare


async def make_sequence(
    ctx: Ctx, tail: str, reel_wt: str, tracks: int, puts: list[list[Any]]
) -> dict[str, Any] | None:
    ids = ctx.ids
    code = fill(
        SEQUENCE_TEMPLATE,
        REEL=reel_wt,
        FPS=ids["fps"],
        WIDTH=ids["width"],
        HEIGHT=ids["height"],
        START="",
        NAME=name_of(ctx, tail),
        TRACKS=tracks,
        PUTS=puts,
    )
    br = await tx(ctx, PRELUDE + "\n" + code, label=f"p31b_make_{tail}", timeout_s=120.0)
    rec(ctx, f"prepare_{tail}_is_made", br.ok and bit_depth_ok(br.result), resp=_br_dict(br))
    return br.result if br.ok and isinstance(br.result, dict) else None


def s1_puts(ids: dict[str, Any]) -> list[list[Any]]:
    return [[track, ids["clipWt"][clip], rel] for track, clip, rel in S1_PUTS]


async def fresh_clip(ctx: Ctx, reel_wt: str, key: str = "clip1") -> dict[str, Any] | None:
    """A clip imported (by transport) into `reel_wt`: {wt, uid, name}."""
    got = await tx_val(
        ctx, fill(IMPORT_CODE, REEL=reel_wt, PATH=ctx.ids["paths"][key]), "p31b_fresh_clip"
    )
    if is_error(got) or not got:
        return None
    _type, name, uid, wiretap_id = got[0]
    return {"wt": wiretap_id, "uid": uid, "name": name}


async def prepare(ctx: Ctx) -> None:
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    files_dir = OUTPUT_DIR / f"files_p31b_{ctx.ts}"
    dir2 = files_dir / "dir2"
    dir2.mkdir(parents=True, exist_ok=True)
    shutil.copy(CLIP_FILES[0][1], dir2 / "dir_a.mov")
    shutil.copy(CLIP_FILES[1][1], dir2 / "dir_b.mov")
    (files_dir / "empty.mov").write_bytes(b"")
    ids = ctx.ids
    ids.update(
        filesDir=str(files_dir),
        dir2=str(dir2),
        empty=str(files_dir / "empty.mov"),
        missing=str(files_dir / "gone.mov"),
        ghost=str(files_dir / "ghost.[00000001-00000024].tif"),
        paths={n: str(p) for n, p in CLIP_FILES},
        path1=str(CLIP_FILES[0][1]),
        path2=str(CLIP_FILES[1][1]),
        path3=str(CLIP_FILES[2][1]),
    )
    made = await tx(
        ctx,
        PRELUDE
        + "\n"
        + fill(
            PREPARE_CODE,
            RG=name_of(ctx, "RG"),
            LIB=name_of(ctx, "LIB"),
            REELS=list(REEL_NAMES),
            PATHS=[[n, str(p)] for n, p in CLIP_FILES],
        ),
        label="p31b_prepare",
        timeout_s=180.0,
    )
    rec(ctx, "prepare_reel_group_library_and_3_clips", made.ok, resp=_br_dict(made))
    if not (made.ok and isinstance(made.result, dict)):
        return
    result = made.result
    ids.update(
        rgWt=result["rgWt"],
        libWt=result["libWt"],
        lrWt=result["lrWt"],
        lr2Wt=result["lr2Wt"],
        fps=result["fps"],
        width=result["width"],
        height=result["height"],
        clipWt={n: v["wt"] for n, v in result["clips"].items()},
        clipUid={n: v["uid"] for n, v in result["clips"].items()},
    )
    for reel, wiretap_id in result["reels"].items():
        ids[f"{reel.lower()}Wt"] = wiretap_id
    ids["clip1Wt"] = ids["clipWt"]["clip1"]
    s1 = await make_sequence(ctx, "S1", ids["seqWt"], 2, s1_puts(ids))
    if s1 is None:
        return
    ids.update(s1Wt=s1["wt"], s1Uid=s1["uid"], s1Name=s1["name"])
    for key in ("clip3", "S1"):
        source = ids["clipWt"]["clip3"] if key == "clip3" else ids["s1Wt"]
        copied = await tx_val(
            ctx, fill(COPY_CODE, SEQ=source, DEST=ids["drelWt"]), "p31b_fill_drel"
        )
        rec(ctx, f"prepare_{key}_copied_into_drel", not is_error(copied) and bool(copied))
    batch = await tx_val(
        ctx,
        fill(BATCH_CODE, NAME=name_of(ctx, "BG"), SRC=ids["clipWt"]["clip1"]),
        "p31b_batch",
        timeout_s=60.0,
    )
    rec(ctx, "prepare_batch_group", not is_error(batch) and isinstance(batch, dict), resp=batch)
    if isinstance(batch, dict):
        usable = isinstance(batch.get("reelWt"), str) and not batch["reelWt"].startswith("ERR:")
        ids["batchReelWt"] = batch["reelWt"] if usable else "/wt/no-batch-reel-id"
        ids["batchHasWt"] = usable
        clip_ok = isinstance(batch.get("clipWt"), str) and not batch["clipWt"].startswith("ERR:")
        ids["batchClipWt"] = batch["clipWt"] if clip_ok else "/wt/no-batch-clip-id"
        ids["batchClipHasWt"] = clip_ok
    lib_clip = await fresh_clip(ctx, ids["lrWt"])
    rec(ctx, "prepare_a_clip_in_the_library", lib_clip is not None)
    if lib_clip is not None:
        ids["libClipWt"] = lib_clip["wt"]
    ids["fingerprints"] = {}


async def batch_refusal(
    session: Any, ctx: Ctx, step: str, call_id: str, needle: str, **extra: Any
) -> None:
    ids = ctx.ids
    if not ids.get("batchHasWt"):
        observe(ctx, f"{step}_a_batch_reel_has_no_wiretap_id", batchReelWt=ids.get("batchReelWt"))
        return
    before = groups_now(ctx)
    env = await run_call(session, ctx, call_id, **extra)
    rec(
        ctx,
        f"{step}_a_batch_reel_is_refused_and_points_to_add_clips",
        expect_error(env, "INVALID_ARGS")
        and needle in str(env.get("hint"))
        and groups_now(ctx) == before,
        error=env.get("error"),
        hint=env.get("hint"),
    )


async def segments_of(session: Any, ctx: Ctx, wiretap_id: str, track: int) -> list[Any]:
    env = await do(
        session,
        ctx,
        "timeline.list_segments",
        {"target": sel(wiretap_id), "version": 0, "track": track, "limit": 100},
    )
    return [{k: v for k, v in i.items() if k != "index"} for i in items_of(env)]
