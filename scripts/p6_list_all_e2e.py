#!/usr/bin/env python3
"""Real-Flame end-to-end: `desktop.list_all` walks the Desktop and the libraries (run with the
`.venv` Python; `scripts/run_e2e.sh p6_list_all` starts Flame; `--dry-run` connects to nothing,
lists the calls, compiles the code sent to the server and to Flame).

Checks: `desktop.list_all` lists a Desktop reel group, a Batch group, an open library with a folder
and a closed library in the order an independent walk reads them, names the closed library in
`closedLibraries` without opening it (`opened` is the same before and after), lists the Batch group
by name without reading inside it, keeps the current Batch group, honours `types`, `scope` and
paging, and refuses a scope outside the enum and a `types` that is not a list of strings.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the op under test (a plain walk of the objects this run made: names, types, the
`opened` flag of each library, the uid of the current Batch group). Everything this run makes is
named `PP6_<ts>_...` (scripts/p2_4_clean_probe_project.py knows the prefix) and is deleted at the
end by that prefix (the Desktop reel group, the Batch group and the two libraries; the closed one
is opened first); then flame.exit(). The journal directory is this run's own
(`p3/journal_p6la_e2e_<ts>/`). No test hook is set for any server. The first step refuses to run
anywhere but in the project `flame_mcp_probe`. The op is a read: nothing in the journal.

Material (transport): a Desktop reel group `PP6_<ts>_RG` with a reel SRC that holds one imported
clip; a Batch group `PP6_<ts>_B`; a library `PP6_<ts>_LIB` with a folder FLD holding a reel LR with
a copy of the clip; a library `PP6_<ts>_CLOSED` with a reel CR holding a copy, closed with
`close()`.

Stage l
  1. Full listing (every page). JUDGED: the entries whose path starts with this run's prefix equal,
     in order, the transport's own walk (reel group > reel > clip, the Batch group, library > folder
     > reel > clip, the closed library with nothing below it); `location` and `library` follow the
     path. OBSERVED: the time of the call, the number of entries by type, `stoppedEarly`.
  2. The closed library. JUDGED: named in `closedLibraries`, the open one is not; no entry below it;
     the transport reads `opened` False for it and True for the open one before and after; the
     result has no `librariesOpened`.
  3. The Batch group. JUDGED: listed with its name and uid and a null selector; `batchGroupsSkipped`
     counts at least this group; the current Batch group has the same uid before and after.
  4. `types=['PyClip']` lists clips only, the same two clips as the walk; `scope='desktop'` has no
     library entry and no closed library; `scope='libraries'` has no Desktop entry.
  5. Paging with `limit=2` (at most MAX_PAGES pages): the pages joined equal the start of the
     full listing and `total` agrees.
  6. `desktop.list_children` with a path selector of the closed library lists its reel and the
     library is closed again afterwards (the transport reads `opened` False).
  7. A scope outside the enum and `types` as a string are INVALID_ARGS.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
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
    _result_of,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do_read,
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
    journal_states,
    observe,
    ops_used,
    print_observations,
)
from p3_1a_read_e2e import fill, is_error  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
NAME_PREFIX: Final[str] = "PP6"
FILE_48: Final[Path] = MEDIA_DIR / "p2" / "p2_len48.mov"
MAKE_TIMEOUT_S: Final[float] = 180.0
PAGE_LIMIT: Final[int] = 100
MAX_PAGES: Final[int] = 40
SMALL_PAGE: Final[int] = 2

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def call_for(call_id: str, op: str) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Any]:
    def register(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


@call_for("l1_list_all", "desktop.list_all")
def _l1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"limit": PAGE_LIMIT, "offset": ids.get("offset", 0)}


@call_for("l4_clips_only", "desktop.list_all")
def _l4c(ids: dict[str, Any]) -> dict[str, Any]:
    return {"types": ["PyClip"], "limit": PAGE_LIMIT, "offset": ids.get("offset", 0)}


@call_for("l4_scope_desktop", "desktop.list_all")
def _l4d(ids: dict[str, Any]) -> dict[str, Any]:
    return {"scope": "desktop", "limit": PAGE_LIMIT, "offset": ids.get("offset", 0)}


@call_for("l4_scope_libraries", "desktop.list_all")
def _l4l(ids: dict[str, Any]) -> dict[str, Any]:
    return {"scope": "libraries", "limit": PAGE_LIMIT, "offset": ids.get("offset", 0)}


@call_for("l5_small_pages", "desktop.list_all")
def _l5(ids: dict[str, Any]) -> dict[str, Any]:
    return {"limit": SMALL_PAGE, "offset": ids.get("offset", 0)}


@call_for("l6_list_children_of_the_closed_library", "desktop.list_children")
def _l6(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": {"kind": "path", "path": [ids["closedName"]]}, "limit": 50}


# -- transport scaffolding (never through the op under test) ------------------------------------

PRELUDE: Final[str] = "\n".join(
    [
        "def _g(fn):",
        "    try:",
        "        return fn()",
        "    except BaseException as _e:",
        "        return 'ERR:' + type(_e).__name__ + ': ' + str(_e)",
    ]
)

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

SETUP_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_d = _ws.desktop",
        "_rg = _d.create_reel_group(@RG@)",
        "_reel = _rg.create_reel('SRC')",
        "_clip = flame.import_clips(@PATH@, _reel)[0]",
        "_bg = _d.create_batch_group(@BATCH@)",
        "_lib = _ws.create_library(@LIB@)",
        "_fld = _lib.create_folder('FLD')",
        "_lreel = _fld.create_reel('LR')",
        "flame.media_panel.copy(_clip, _lreel)",
        "_cl = _ws.create_library(@CLOSED@)",
        "_creel = _cl.create_reel('CR')",
        "flame.media_panel.copy(_clip, _creel)",
        "_closed_returned = _cl.close()",
        "_result = {",
        "    'rg': _v(_rg.name),",
        "    'clip': _v(_clip.name),",
        "    'batchName': _v(_bg.name),",
        "    'batchUid': _v(_bg.uid),",
        "    'lib': _v(_lib.name),",
        "    'closedName': _v(_cl.name),",
        "    'closedReturned': repr(_closed_returned),",
        "    'closedOpened': _g(lambda: _v(_cl.opened)),",
        "}",
    ]
)

STATE_CODE: Final[str] = "\n".join(
    [
        "_ws = flame.projects.current_project.current_workspace",
        "_p = @P@",
        "_cur = _g(lambda: _v(_v(_ws.desktop.current_batch_group).uid))",
        "_libs = {}",
        "for _l in list(_ws.libraries):",
        "    if _v(_l.name).startswith(_p):",
        "        _libs[_v(_l.name)] = _g(lambda _l=_l: _v(_l.opened))",
        "_result = {'currentBatchUid': _cur, 'libraries': _libs}",
    ]
)

WALK_CODE: Final[str] = "\n".join(
    [
        "def _walk(_o, _path, _out):",
        "    _kind = type(_o).__name__",
        "    _here = _path + [_v(_o.name)]",
        "    _out.append([_kind, _here])",
        "    if _kind == 'PyBatch':",
        "        return",
        "    if _kind == 'PyLibrary' and _v(_o.opened) is False:",
        "        return",
        "    if _kind == 'PyReelGroup':",
        "        _kids = list(_o.reels)",
        "    elif _kind in ('PyReel', 'PyFolder', 'PyLibrary'):",
        "        _kids = list(_o.children)",
        "    else:",
        "        return",
        "    for _c in _kids:",
        "        _walk(_c, _here, _out)",
        "_p = @P@",
        "_ws = flame.projects.current_project.current_workspace",
        "_out = []",
        "for _x in list(_ws.desktop.reel_groups):",
        "    if _v(_x.name).startswith(_p):",
        "        _walk(_x, [], _out)",
        "for _x in list(_ws.desktop.batch_groups):",
        "    if _v(_x.name).startswith(_p):",
        "        _walk(_x, [], _out)",
        "for _x in list(_ws.libraries):",
        "    if _v(_x.name).startswith(_p):",
        "        _walk(_x, [], _out)",
        "_result = _out",
    ]
)

OPEN_CODE: Final[str] = "\n".join(
    [
        "_p = @P@",
        "_done = []",
        "for _l in list(flame.projects.current_project.current_workspace.libraries):",
        "    if _v(_l.name).startswith(_p) and _v(_l.opened) is False:",
        "        _done.append([_v(_l.name), _l.open()])",
        "_result = _done",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "setup": SETUP_CODE,
    "state": STATE_CODE,
    "walk": WALK_CODE,
    "open": OPEN_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}_{tail}"


def prefix_of(ctx: Ctx) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}"


async def tx_val(ctx: Ctx, code: str, label: str, timeout_s: float = 60.0) -> Any:
    br = await tx(ctx, PRELUDE + "\n" + code, label=label, timeout_s=timeout_s)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_state(ctx: Ctx) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(STATE_CODE, P=prefix_of(ctx)), "p6la_state")
    return None if is_error(got) or not isinstance(got, dict) else got


async def tx_walk(ctx: Ctx) -> list[list[Any]] | None:
    got = await tx_val(ctx, fill(WALK_CODE, P=prefix_of(ctx)), "p6la_walk")
    return None if is_error(got) or not isinstance(got, list) else got


def pairs(items: list[dict[str, Any]]) -> list[list[Any]]:
    """[[type, path]] of the items that name an object (an error item has no type)."""
    return [[i.get("type"), i.get("path")] for i in items]


def ours(items: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    return [i for i in items if (i.get("path") or [""])[0].startswith(prefix)]


def below_closed(items: list[dict[str, Any]], closed_name: str) -> list[dict[str, Any]]:
    return [i for i in items if i.get("path", [])[:1] == [closed_name] and len(i["path"]) > 1]


def count_by_type(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get("type") or "error")
        counts[key] = counts.get(key, 0) + 1
    return counts


def shape_ok(items: list[dict[str, Any]]) -> bool:
    """Every entry has the find shape plus path / location / library, and they agree."""
    keys = {"type", "name", "uid", "selector", "parent", "path", "location", "library"}
    for item in items:
        path = item.get("path")
        if not keys <= set(item) or not isinstance(path, list) or not path:
            return False
        if item["name"] != path[-1]:
            return False
        if item["location"] == "library":
            if item["library"] != path[0]:
                return False
        elif item["location"] != "desktop" or item["library"] is not None:
            return False
    return True


async def list_pages(
    session: Any, ctx: Ctx, call_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Every page of a listing call (the offset moves by what was shown). Returns (the items, the
    result of the first page, the envelopes)."""
    op, build = CALLS[call_id]
    items: list[dict[str, Any]] = []
    first: dict[str, Any] = {}
    envs: list[dict[str, Any]] = []
    offset = 0
    for _ in range(MAX_PAGES):
        env = await do_read(session, ctx, op, build({**ctx.ids, "offset": offset}))
        envs.append(env)
        value = _result_of(env)
        if not first:
            first = value
        page = value.get("items") or []
        if not isinstance(page, list):
            ctx.ids["list_pages_collapsed"] = {"callId": call_id, "items": page}
            break
        items.extend(page)
        if env.get("ok") is not True or not value.get("truncated") or not page:
            break
        offset += len(page)
    return items, first, envs


# -- prepare ------------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    project = await tx_val(ctx, PROJECT_CODE, "p6la_project")
    rec(
        ctx,
        "prepare_the_project_is_the_probe_project",
        project == TARGET_PROJECT,
        project=project,
    )
    if project != TARGET_PROJECT:
        return
    if not FILE_48.is_file():
        rec(ctx, "prepare_material_file_exists", False, missing=str(FILE_48))
        return
    made = await tx_val(
        ctx,
        fill(
            SETUP_CODE,
            RG=name_of(ctx, "RG"),
            PATH=str(FILE_48),
            BATCH=name_of(ctx, "B"),
            LIB=name_of(ctx, "LIB"),
            CLOSED=name_of(ctx, "CLOSED"),
        ),
        "p6la_setup",
        MAKE_TIMEOUT_S,
    )
    ok = isinstance(made, dict) and made.get("closedOpened") is False
    rec(ctx, "prepare_the_material_is_made_and_the_second_library_is_closed", ok, made=made)
    if not ok:
        return
    ctx.ids.update(made)
    walk = await tx_walk(ctx)
    rec(
        ctx,
        "prepare_the_independent_walk_reads_the_material",
        isinstance(walk, list) and len(walk) >= 9,
        walk=walk,
    )
    if isinstance(walk, list):
        ctx.ids["walk"] = walk


# -- stage l ------------------------------------------------------------------------------------


async def l_full(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    prefix = prefix_of(ctx)
    before = await tx_state(ctx)
    started = time.monotonic()
    items, first, envs = await list_pages(session, ctx, "l1_list_all")
    elapsed_s = round(time.monotonic() - started, 3)
    after = await tx_state(ctx)
    walk = await tx_walk(ctx)
    mine = ours(items, prefix)
    last = _result_of(envs[-1]) if envs else {}
    ok, parts = judged(
        every_page_ok=bool(envs) and all(e.get("ok") is True for e in envs),
        no_page_was_collapsed=not ctx.ids.get("list_pages_collapsed"),
        same_as_the_walk=pairs(mine) == walk and bool(walk),
        shape=shape_ok(mine),
        no_error_items=not [i for i in mine if "error" in i],
        not_stopped_early="stoppedEarly" not in first and "stoppedEarly" not in last,
        state_unchanged=before == after and before is not None,
    )
    rec(
        ctx,
        "l1_the_listing_equals_an_independent_walk_of_what_this_run_made",
        ok,
        clauses=parts,
        listed=pairs(mine),
        walked=walk,
    )
    ids["fullItems"] = items
    ids["stateBefore"] = before
    ids["fullTotal"] = first.get("total")
    ids["firstPage"] = first
    observe(
        ctx,
        "l1_what_the_whole_project_walk_cost",
        elapsedS=elapsed_s,
        pages=len(envs),
        total=first.get("total"),
        byType=count_by_type(items),
        errorItems=len([i for i in items if "error" in i]),
        closedLibraries=first.get("closedLibraries"),
        batchGroupsSkipped=first.get("batchGroupsSkipped"),
        stoppedEarly=first.get("stoppedEarly"),
        durationMs=[e.get("durationMs") for e in envs],
    )


async def l_closed(ctx: Ctx) -> None:
    ids = ctx.ids
    items, first = ids["fullItems"], ids["firstPage"]
    closed, lib = ids["closedName"], ids["lib"]
    state = await tx_state(ctx)
    libraries = (state or {}).get("libraries") or {}
    named = first.get("closedLibraries") or []
    ok, parts = judged(
        named_in_closed_libraries=closed in named,
        open_library_not_named=lib not in named,
        nothing_below_the_closed_one=not below_closed(items, closed),
        closed_one_is_listed=any(i.get("path") == [closed] for i in items),
        transport_closed_still_closed=libraries.get(closed) is False,
        transport_open_still_open=libraries.get(lib) is True,
        no_libraries_opened_in_result="librariesOpened" not in first,
    )
    rec(
        ctx,
        "l2_the_closed_library_is_named_listed_empty_and_not_opened",
        ok,
        clauses=parts,
        closedLibraries=named,
        libraries=libraries,
    )


async def l_batch(ctx: Ctx) -> None:
    ids = ctx.ids
    items, first = ids["fullItems"], ids["firstPage"]
    before = ids.get("stateBefore")
    state = await tx_state(ctx)
    hits = [i for i in items if i.get("path") == [ids["batchName"]]]
    batch = hits[0] if len(hits) == 1 else {}
    skipped = first.get("batchGroupsSkipped")
    ok, parts = judged(
        listed_once=len(hits) == 1,
        type_is_batch=batch.get("type") == "PyBatch",
        uid_matches=batch.get("uid") == ids["batchUid"],
        selector_null=batch.get("selector") is None,
        counted=isinstance(skipped, int) and skipped >= 1,
        nothing_below=not [
            i for i in items if i.get("path", [])[:1] == [ids["batchName"]] and len(i["path"]) > 1
        ],
        current_batch_unchanged=before is not None
        and before.get("currentBatchUid") == (state or {}).get("currentBatchUid"),
    )
    rec(
        ctx,
        "l3_the_batch_group_is_listed_by_name_and_the_current_group_is_unchanged",
        ok,
        clauses=parts,
        item=batch,
        batchGroupsSkipped=skipped,
        state=state,
    )
    observe(
        ctx,
        "l3_which_batch_group_is_current",
        currentBefore=(before or {}).get("currentBatchUid"),
        currentAfter=(state or {}).get("currentBatchUid"),
        madeGroup=ids["batchUid"],
        madeGroupIsCurrent=(state or {}).get("currentBatchUid") == ids["batchUid"],
    )


async def l_types_and_scope(session: Any, ctx: Ctx) -> None:
    prefix = prefix_of(ctx)
    walk = ctx.ids.get("walk") or []
    clip_paths = [p for kind, p in walk if kind == "PyClip"]
    clips, first, _envs = await list_pages(session, ctx, "l4_clips_only")
    ok, parts = judged(
        only_clips=bool(clips) and all(i.get("type") == "PyClip" for i in clips),
        the_clips_of_the_walk=[i["path"] for i in ours(clips, prefix)] == clip_paths,
        total_counts_after_the_filter=first.get("total") == len(clips),
    )
    rec(ctx, "l4_a_types_filter_keeps_the_clips_only", ok, clauses=parts, clipPaths=clip_paths)
    desktop, d_first, _ = await list_pages(session, ctx, "l4_scope_desktop")
    libs, l_first, _ = await list_pages(session, ctx, "l4_scope_libraries")
    ok, parts = judged(
        desktop_scope_has_desktop_only=bool(desktop)
        and all(i.get("location") == "desktop" for i in desktop),
        desktop_scope_names_no_closed_library=d_first.get("closedLibraries") == [],
        libraries_scope_has_libraries_only=bool(libs)
        and all(i.get("location") == "library" for i in libs),
        libraries_scope_names_the_closed_one=ctx.ids["closedName"]
        in (l_first.get("closedLibraries") or []),
        desktop_scope_has_our_group=any(i.get("path") == [ctx.ids["rg"]] for i in desktop),
        libraries_scope_has_our_library=any(i.get("path") == [ctx.ids["lib"]] for i in libs),
    )
    rec(ctx, "l4_scope_splits_the_walk_between_the_desktop_and_the_libraries", ok, clauses=parts)


async def l_paging(session: Any, ctx: Ctx) -> None:
    full = ctx.ids.get("fullItems") or []
    pages, first, envs = await list_pages(session, ctx, "l5_small_pages")
    ok, parts = judged(
        pages_joined_equal_the_start_of_the_full_listing=bool(pages)
        and pairs(pages) == pairs(full)[: len(pages)],
        first_page_is_small=first.get("shown") == SMALL_PAGE and first.get("truncated") is True,
        total_agrees=first.get("total") == ctx.ids.get("fullTotal"),
        every_page_ok=all(e.get("ok") is True for e in envs),
    )
    rec(
        ctx,
        "l5_pages_of_two_joined_equal_the_start_of_the_whole_listing",
        ok,
        clauses=parts,
        pages=len(envs),
    )


async def l_children_of_closed(session: Any, ctx: Ctx) -> None:
    closed = ctx.ids["closedName"]
    env = await do_read(session, ctx, *call_args("l6_list_children_of_the_closed_library", ctx))
    result = _result_of(env)
    after = await tx_state(ctx)
    opened_rows = result.get("librariesOpened") or []
    ok, parts = judged(
        op_ok=env.get("ok") is True,
        lists_the_reel=[i.get("name") for i in result.get("items") or []] == ["CR"],
        opened_for_the_call=bool(opened_rows),
        closed_again=bool(opened_rows) and all(r.get("closedAgain") is True for r in opened_rows),
        transport_reads_it_closed=((after or {}).get("libraries") or {}).get(closed) is False,
    )
    rec(
        ctx,
        "l6_list_children_opens_the_closed_library_for_its_call_and_closes_it_again",
        ok,
        clauses=parts,
        error=env.get("error"),
        result=result,
    )


def call_args(call_id: str, ctx: Ctx) -> tuple[str, dict[str, Any]]:
    op, build = CALLS[call_id]
    return op, build(ctx.ids)


async def l_invalid(session: Any, ctx: Ctx) -> None:
    for step, args in (
        ("l7_a_scope_outside_the_enum_is_invalid_args", {"scope": "everything"}),
        ("l7_types_as_a_string_is_invalid_args", {"types": "PyClip"}),
    ):
        env = await do_read(session, ctx, "desktop.list_all", args)
        rec(ctx, step, env.get("errorCode") == "INVALID_ARGS", errorCode=env.get("errorCode"))


async def stage_l(ctx: Ctx) -> None:
    async with server_session(ctx, "p6la-l") as session:
        await l_full(session, ctx)
        await l_closed(ctx)
        await l_batch(ctx)
        await l_types_and_scope(session, ctx)
        await l_paging(session, ctx)
        await l_children_of_closed(session, ctx)
        await l_invalid(session, ctx)


# -- z / driver ---------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    rec(ctx, "z_list_all_was_called", "desktop.list_all" in used, used=used)
    rec(
        ctx,
        "z_the_journal_of_this_run_holds_no_group",
        journal_states(ctx) == {},
        states=journal_states(ctx),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = prefix_of(ctx)
    opened = await tx_val(ctx, fill(OPEN_CODE, P=prefix), "p6la_open")
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p6la_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p6la_left")
    ctx.results["cleanup"] = {"opened": opened, "deleted": gone, "stillThere": left}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted",
        isinstance(gone, dict)
        and all(ok is True for rows_ in gone.values() for _n, ok in rows_)
        and left == {"reelGroups": [], "batchGroups": [], "libraries": []},
        opened=opened,
        deleted=gone,
        stillThere=left,
    )
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if {"rg", "batchName", "lib", "closedName"} <= set(ctx.ids):
            await guarded(ctx, "l", stage_l)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p6la_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------


def selftest_helpers() -> list[str]:
    """The comparison helpers on made-up listings (client side only). Returns the problems."""
    problems = []
    item = {
        "type": "PyClip",
        "name": "c",
        "uid": "u",
        "selector": None,
        "parent": None,
        "path": ["L", "R", "c"],
        "location": "library",
        "library": "L",
    }
    desk = {**item, "path": ["G", "R", "c"], "location": "desktop", "library": None}
    if not shape_ok([item, desk]):
        problems.append("shape_ok refused a well-formed listing; expected it to accept")
    if shape_ok([{**item, "library": "other"}]):
        problems.append("shape_ok accepted a library that is not path[0]; expected a refusal")
    if shape_ok([{**desk, "library": "G"}]):
        problems.append("shape_ok accepted a library on a Desktop entry; expected a refusal")
    if [i["path"] for i in ours([item, desk], "L")] != [["L", "R", "c"]]:
        problems.append("ours: expected only the entries whose path starts with the library name")
    if below_closed([{"path": ["C"]}, {"path": ["C", "R"]}], "C") != [{"path": ["C", "R"]}]:
        problems.append("below_closed: expected only the entries under the closed library")
    if count_by_type([item, desk, {"error": "x"}]) != {"PyClip": 2, "error": 1}:
        problems.append("count_by_type: expected {PyClip: 2, error: 1}")
    return problems


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    ids: dict[str, Any] = {"closedName": "PP6_x_CLOSED", "offset": 0}
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
            if op.thread != "server":
                code = execution.build_exec_code(op, checked)
                if not code.isascii():
                    problems.append("the code for Flame is not ASCII")
                try:
                    compile(code, f"<{call_id}>", "exec")
                except SyntaxError as exc:
                    problems.append(f"compile FAILED: {exc}")
        else:
            problems.append(
                "invalid args: " + "; ".join(f"{i.path}: {i.message}" for i in validation.issues)
            )
        print(f"{'NG ' + '; '.join(problems) if problems else 'OK':3} {call_id}: {op_name}")
        failures += bool(problems)
    print("===== the two refused calls are refused =====")
    list_all = registry.get("desktop.list_all")
    for args in ({"scope": "everything"}, {"types": "PyClip"}):
        refused = list_all is not None and not validate_op_args(list_all, args).ok
        print(f"{'OK ' if refused else 'NG '} {args}")
        failures += not refused
    print("===== transport code (compiled with every @NAME@ filled) =====")
    for name, template in TEMPLATES.items():
        code = PRELUDE + "\n" + re.sub(r"@[A-Z_0-9]+@", "'x'", template)
        try:
            compile(code, f"<{name}>", "exec")
            note = "" if code.isascii() else " (NOT ASCII)"
            failures += bool(note)
            print(f"{'NG ' if note else 'OK '} {name} ({len(code)} chars){note}")
        except SyntaxError as exc:
            print(f"NG  {name}: {exc}")
            failures += 1
    print("===== comparison helpers on made-up listings =====")
    problems = selftest_helpers()
    print("OK  helpers" if not problems else f"NG  helpers: {problems}")
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
    journal_dir = OUTPUT_DIR / f"journal_p6la_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p6_list_all_e2e_{ts}.json",
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
