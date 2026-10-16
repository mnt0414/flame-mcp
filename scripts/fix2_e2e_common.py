"""Shared parts of the library / Batch-copy real-Flame e2e (scripts/fix2_library_batch_e2e.py):
the calls made through the MCP surface, the transport scaffolding (which READS EVERY STATE BACK
INDEPENDENTLY of the ops under test), the helpers the stages use and the material (`prepare`). The
stages are in fix2_e2e_stages.py; the driver, the plan / policy stage, the journal stage, the
cleanup and `--dry-run` are in fix2_library_batch_e2e.py."""

from __future__ import annotations

import copy
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import e2e_guard  # noqa: E402
from p1_mcp_e2e import MEDIA_DIR, _br_dict, _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, do, rec, tx  # noqa: E402
from p2_4_common import journal_records, journal_states  # noqa: E402
from p3_1a_read_e2e import COPY_CODE, PRELUDE, fill, is_error, tx_val, wt  # noqa: E402
from p3_1b_e2e_common import (  # noqa: E402
    CLEANUP_CODE,
    DELETE_WT_CODE,
    FIND_WT_CODE,
    FOLDER_CODE,
    LEFT_CODE,
    LISTING_CODE,
    MAKE_REEL_CODE,
    RENAME_CODE,
)

from flame_mcp.journal import Journal, new_id, now_iso  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
CKPT_ENV: Final[str] = "FLAME_MCP_CHECKPOINT_LIBRARY"
NAME_PREFIX: Final[str] = "PFX2"
CLIP_FILES: Final[tuple[tuple[str, Path], ...]] = (
    ("c1", MEDIA_DIR / "clip1.mov"),
    ("c2", MEDIA_DIR / "clip2.mov"),
    ("c3", MEDIA_DIR / "clip3.mov"),
)
"""(the name a clip gets, its file): the order here is the order the clips go into a group's first
schematic reel, and so the order every comparison expects."""
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed"}
)

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def call_for(call_id: str, op: str) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Any]:
    def register(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


def sel(wiretap_id: str) -> dict[str, Any]:
    return wt(wiretap_id)


def batch_sel(uid: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid}


DESKTOP_SEL: Final[dict[str, Any]] = {"kind": "desktop"}


@call_for("l1_create_library", "library.create")
def _l1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["lib1Name"]}


@call_for("l2_create_taken_name", "library.create")
def _l2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["lib1Name"]}


@call_for("l3_create_allow_rename", "library.create")
def _l3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["lib1Name"], "allow_rename": True}


@call_for("l6_create_library_that_gets_a_clip", "library.create")
def _l6(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["lib2Name"]}


@call_for("l7_create_backup_library", "library.create")
def _l7(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["lib3Name"]}


@call_for("l7_create_second_backup_library", "library.create")
def _l7b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["lib4Name"]}


@call_for("b1_copy_group_into_library", "desktop.copy")
def _b1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgnUid"])], "destination": sel(ids["lib3Wt"])}


@call_for("o1_create_library_to_close", "library.create")
def _o1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["lib5Name"]}


@call_for("o2_copy_group_into_the_library_to_close", "desktop.copy")
def _o2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgnUid"])], "destination": sel(ids["lib5Wt"])}


@call_for("o4_copy_group_into_the_closed_library", "desktop.copy")
def _o4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgcUid"])], "destination": sel(ids["lib5Wt"])}


@call_for("s1_create_checkpoint_library", "library.create")
def _s1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"name": ids["ckptName"]}


@call_for("s2_purge_dry_run_on_the_closed_checkpoint_library", "journal.purge_checkpoints")
def _s2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"dry_run": True}


@call_for("b3_copy_current_group_into_library", "desktop.copy")
def _b3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgcUid"])], "destination": sel(ids["lib3Wt"])}


@call_for("b4_copy_group_into_a_folder", "desktop.copy")
def _b4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgnUid"])], "destination": sel(ids["folderWt"])}


@call_for("b5_copy_group_into_a_library_reel", "desktop.copy")
def _b5a(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgnUid"])], "destination": sel(ids["libReelWt"])}


@call_for("b5_copy_group_into_a_desktop_reel", "desktop.copy")
def _b5b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgnUid"])], "destination": sel(ids["deskReelWt"])}


@call_for("b5_copy_group_with_a_clip", "desktop.copy")
def _b5c(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "sources": [batch_sel(ids["bgnUid"]), sel(ids["clipWt"]["c1"])],
        "destination": sel(ids["lib3Wt"]),
    }


@call_for("b5_copy_desktop_group_onto_the_desktop", "desktop.copy")
def _b5d(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgnUid"])], "destination": DESKTOP_SEL}


@call_for("b6_copy_group_into_second_library", "desktop.copy")
def _b6(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [batch_sel(ids["bgnUid"])], "destination": sel(ids["lib4Wt"])}


@call_for("r1_restore_group_onto_the_desktop", "desktop.copy")
def _r1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [ids["restoreSel"]], "destination": DESKTOP_SEL}


@call_for("r5_restore_the_group_that_was_current", "desktop.copy")
def _r5(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [ids["restoreCurrentSel"]], "destination": DESKTOP_SEL}


@call_for("r6_restore_a_library_group_into_a_library", "desktop.copy")
def _r6(ids: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [ids["restoreSel"]], "destination": sel(ids["lib4Wt"])}


@call_for("d1_list_the_desktop", "desktop.list_children")
def _d1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": DESKTOP_SEL, "limit": 500}


@call_for("d1_list_the_desktop_batch_groups_only", "desktop.list_children")
def _d1b(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": DESKTOP_SEL, "types": ["PyBatch"], "limit": 500}


@call_for("e1_plan_library_create", "plan.run")
def _e1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"steps": [{"op": "library.create", "args": {"name": ids["lib1Name"]}}]}


# -- transport scaffolding (never through the op under test) ------------------------------------

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

PREPARE_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_d = _ws.desktop",
        "_rg = _d.create_reel_group(@RG@)",
        "_dreel = _rg.create_reel('DREL')",
        "_src = _ws.create_library(@SRCLIB@)",
        "_sr = _src.create_reel('SR')",
        "_clips = {}",
        "for _name, _p in @CLIPS@:",
        "    _c = flame.import_clips(_p, _sr)[0]",
        "    _c.name.set_value(_name)",
        "    _clips[_name] = _c",
        "_groups = {}",
        "for _key, _gname, _wired in @GROUPS@:",
        "    _bg = _d.create_batch_group(_gname)",
        "    if _wired:",
        "        _blur = _bg.create_node('Blur')",
        "        _comp = _bg.create_node('Comp')",
        "        _bg.connect_nodes(_blur, 'Default', _comp, 'Default')",
        "        for _name in @ORDER@:",
        "            flame.media_panel.copy(_clips[_name], _bg.reels[0])",
        "    _groups[_key] = _v(_bg.uid)",
        "_result = {",
        "    'rgWt': _rg.get_wiretap_node_id(), 'deskReelWt': _dreel.get_wiretap_node_id(),",
        "    'srcLibWt': _src.get_wiretap_node_id(),",
        "    'clipWt': {_n: _clips[_n].get_wiretap_node_id() for _n in _clips},",
        "    'groups': _groups,",
        "}",
    ]
)

GROUP_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_cur = _v(_d.current_batch_group)",
        "_result = None",
        "if _hit:",
        "    _bg = _hit[0]",
        "    _result = {",
        "        'name': _v(_bg.name), 'uid': _v(_bg.uid),",
        "        'isCurrent': _cur is not None and _v(_cur.uid) == _v(_bg.uid),",
        "        'nodes': [[_v(_n.name), _v(_n.type), _v(_n.sockets)] for _n in _bg.nodes],",
        "        'reels': [[_v(_r.name), [_v(_c.name) for _c in _r.clips]] for _r in _bg.reels],",
        "        'shelfReels': [",
        "            [_v(_r.name), [_v(_c.name) for _c in _r.clips]] for _r in _bg.shelf_reels],",
        "    }",
    ]
)

CURRENT_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_cur = _v(_d.current_batch_group)",
        "_result = None if _cur is None else [_v(_cur.name), _v(_cur.uid)]",
    ]
)

SET_CURRENT_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_note = None",
        "try:",
        "    _d.current_batch_group.set_value(_hit[0])",
        "except BaseException as _e:",
        "    _note = type(_e).__name__ + ': ' + str(_e)",
        "_cur = _v(_d.current_batch_group)",
        "_result = {'note': _note, 'current': None if _cur is None else _v(_cur.uid)}",
    ]
)

LIB_BATCHES_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else [{",
        "    'name': _v(_b.name), 'uid': _v(_b.uid), 'nodeCount': len(list(_b.nodes)),",
        "    'reels': [[_v(_r.name), [_v(_c.name) for _c in _r.clips]] for _r in _b.reels],",
        "    'shelfReels': [",
        "        [_v(_r.name), [_v(_c.name) for _c in _r.clips]] for _r in _b.shelf_reels],",
        "} for _b in _o.children if type(_b).__name__ == 'PyBatch']",
    ]
)

LIBRARIES_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_result = [[_v(_l.name), _v(_l.uid), _l.get_wiretap_node_id(), len(list(_l.children))]",
        "           for _l in _ws.libraries]",
    ]
)

DESKTOP_CHILDREN_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_result = [[type(_c).__name__, _v(_c.name)] for _c in _d.children]",
    ]
)

LIB_OPEN_STATE_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_hit = [_l for _l in _ws.libraries if _v(_l.uid) == @UID@]",
        "_result = None if not _hit else {",
        "    'opened': _g(lambda: _v(_hit[0].opened)),",
        "    'hasClose': callable(getattr(_hit[0], 'close', None)),",
        "    'children': _g(lambda: len(list(_hit[0].children))),",
        "}",
    ]
)

LIB_SET_OPEN_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_hit = [_l for _l in _ws.libraries if _v(_l.uid) == @UID@]",
        "_note = None",
        "_ret = None",
        "try:",
        "    _ret = _hit[0].open() if @OPEN@ else _hit[0].close()",
        "except BaseException as _e:",
        "    _note = type(_e).__name__ + ': ' + str(_e)",
        "_result = {'note': _note, 'returned': repr(_ret),",
        "           'opened': _g(lambda: _v(_hit[0].opened)),",
        "           'children': _g(lambda: len(list(_hit[0].children)))}",
    ]
)

TOGGLE_BYPASS_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_n = _hit[0].nodes[0]",
        "_before = _g(lambda: _v(_n.bypass))",
        "_note = None",
        "try:",
        "    _n.bypass.set_value(not _before)",
        "except BaseException as _e:",
        "    _note = type(_e).__name__ + ': ' + str(_e)",
        "_result = {'node': _v(_n.name), 'before': _before, 'note': _note,",
        "           'after': _g(lambda: _v(_n.bypass))}",
    ]
)

LIBS_OPENED_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_result = [[_v(_l.name), _g(lambda _l=_l: _v(_l.opened))] for _l in _ws.libraries]",
    ]
)

RENAME_LIB_BATCH_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_hit = [_b for _b in _o.children",
        "        if type(_b).__name__ == 'PyBatch' and _v(_b.uid) == @UID@]",
        "_note = None",
        "if _hit:",
        "    try:",
        "        _hit[0].name.set_value(@NAME@)",
        "    except BaseException as _e:",
        "        _note = type(_e).__name__ + ': ' + str(_e)",
        "_result = {'found': bool(_hit), 'note': _note,",
        "           'name': _v(_hit[0].name) if _hit else None}",
    ]
)

ADD_NODE_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_n = _hit[0].create_node('Exposure')",
        "_result = _v(_n.name)",
    ]
)

DELETE_GROUP_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_result = None if not _hit else flame.delete(_hit[0], confirm=False)",
    ]
)

PUT_CLIP_CODE: Final[str] = COPY_CODE

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "prepare": PREPARE_CODE,
    "group": GROUP_CODE,
    "current": CURRENT_CODE,
    "set_current": SET_CURRENT_CODE,
    "lib_batches": LIB_BATCHES_CODE,
    "libraries": LIBRARIES_CODE,
    "desktop_children": DESKTOP_CHILDREN_CODE,
    "rename_lib_batch": RENAME_LIB_BATCH_CODE,
    "libs_opened": LIBS_OPENED_CODE,
    "lib_open_state": LIB_OPEN_STATE_CODE,
    "lib_set_open": LIB_SET_OPEN_CODE,
    "toggle_bypass": TOGGLE_BYPASS_CODE,
    "add_node": ADD_NODE_CODE,
    "delete_group": DELETE_GROUP_CODE,
    "put_clip": PUT_CLIP_CODE,
    "listing": LISTING_CODE,
    "find_wt": FIND_WT_CODE,
    "folder": FOLDER_CODE,
    "make_reel": MAKE_REEL_CODE,
    "delete_wt": DELETE_WT_CODE,
    "rename": RENAME_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def tx_group(ctx: Ctx, uid: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(GROUP_CODE, UID=uid), "fix2_group")
    return None if is_error(got) or got is None else got


async def tx_current(ctx: Ctx) -> Any:
    return await tx_val(ctx, CURRENT_CODE, "fix2_current")


async def tx_lib_batches(ctx: Ctx, wiretap_id: str) -> list[dict[str, Any]] | None:
    got = await tx_val(ctx, fill(LIB_BATCHES_CODE, WT=wiretap_id), "fix2_lib_batches")
    return None if is_error(got) or got is None else got


async def tx_libraries(ctx: Ctx) -> list[list[Any]]:
    got = await tx_val(ctx, LIBRARIES_CODE, "fix2_libraries")
    return [] if is_error(got) or not isinstance(got, list) else got


async def tx_children(ctx: Ctx, wiretap_id: str) -> list[list[Any]] | None:
    got = await tx_val(ctx, fill(LISTING_CODE, WT=wiretap_id), "fix2_children")
    return None if is_error(got) or got is None else got


async def tx_clip_names(ctx: Ctx, wiretap_id: str) -> list[str] | None:
    rows = await tx_children(ctx, wiretap_id)
    return None if rows is None else [r[1] for r in rows]


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}_{tail}"


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


def synthetic_open_intent(ctx: Ctx, real_group: str | None, **override: Any) -> str:
    """A copy of a real group's intent, with a new id and group and NO outcome, appended to this
    run's own journal directory: what a call whose answer never came leaves."""
    record = copy.deepcopy(intent_of(ctx, real_group))
    group = new_id("g")
    record.update(id=new_id("i"), group=group, ts=now_iso())
    record.update(override)
    Journal(ctx.journal_dir).append(record)
    return group


async def check_group(session: Any, ctx: Ctx, group: str) -> dict[str, Any]:
    env = await do(session, ctx, "journal.check", {"group": group})
    return next(iter(_result_of(env).get("checked") or []), {}) if env.get("ok") else {}


def same_shape(original: dict[str, Any], other: dict[str, Any]) -> dict[str, bool]:
    """What a restored group must share with the original: node names / types / connections (the
    sockets), the schematic reels' clip names in order and the shelf reels."""
    return {
        "nodeNames": [n[0] for n in original["nodes"]] == [n[0] for n in other["nodes"]],
        "nodeTypes": [n[1] for n in original["nodes"]] == [n[1] for n in other["nodes"]],
        "connections": [n[2] for n in original["nodes"]] == [n[2] for n in other["nodes"]],
        "reels": original["reels"] == other["reels"],
        "shelfReels": original["shelfReels"] == other["shelfReels"],
    }


# -- prepare ----------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    ids = ctx.ids
    project = await tx_val(ctx, PROJECT_CODE, "fix2_project")
    rec(
        ctx,
        "prepare_the_project_is_the_probe_project",
        project == TARGET_PROJECT,
        project=project,
    )
    if project != TARGET_PROJECT:
        return
    missing = [str(p) for _n, p in CLIP_FILES if not p.is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    os.environ[CKPT_ENV] = name_of(ctx, "CKPT")  # the servers this run starts inherit it
    ids.update(
        lib1Name=name_of(ctx, "LIB1"),
        lib2Name=name_of(ctx, "LIB2"),
        lib3Name=name_of(ctx, "LIB3"),
        lib4Name=name_of(ctx, "LIB4"),
        lib5Name=name_of(ctx, "LIB5"),
        ckptName=name_of(ctx, "CKPT"),
        order=[name_of(ctx, n) for n, _p in CLIP_FILES],
    )
    clips = [[name_of(ctx, n), str(p)] for n, p in CLIP_FILES]
    groups = [
        ["bgk", name_of(ctx, "BGK"), False],
        ["bgn", name_of(ctx, "BGN"), True],
        ["bgc", name_of(ctx, "BGC"), True],
    ]
    made = await tx(
        ctx,
        PRELUDE
        + "\n"
        + fill(
            PREPARE_CODE,
            RG=name_of(ctx, "RG"),
            SRCLIB=name_of(ctx, "SRC"),
            CLIPS=clips,
            GROUPS=groups,
            ORDER=ids["order"],
        ),
        label="fix2_prepare",
        timeout_s=180.0,
    )
    rec(ctx, "prepare_material_is_made", made.ok, resp=_br_dict(made))
    if not (made.ok and isinstance(made.result, dict)):
        return
    result = made.result
    ids.update(
        rgWt=result["rgWt"],
        deskReelWt=result["deskReelWt"],
        srcLibWt=result["srcLibWt"],
        clipWt={n[len(f"{NAME_PREFIX}_{ctx.ts}_") :]: v for n, v in result["clipWt"].items()},
        bgkUid=result["groups"]["bgk"],
        bgnUid=result["groups"]["bgn"],
        bgcUid=result["groups"]["bgc"],
    )
    original = {}
    for key in ("bgn", "bgc"):
        original[key] = await tx_group(ctx, ids[f"{key}Uid"])
    ids["original"] = original
    shaped = all(
        isinstance(g, dict)
        and [n[1] for n in g["nodes"]][:2] == ["Blur", "Comp"]
        and [c for r in g["reels"][:1] for c in r[1]] == ids["order"]
        for g in original.values()
    )
    rec(
        ctx, "prepare_groups_have_two_wired_nodes_and_three_clips_in_order", shaped, groups=original
    )
    current = await tx_current(ctx)
    rec(
        ctx,
        "prepare_bgc_is_the_current_group",
        isinstance(current, list) and current[1] == ids["bgcUid"],
        current=current,
    )
