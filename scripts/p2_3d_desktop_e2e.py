#!/usr/bin/env python3
"""Real-Flame end-to-end: `desktop.create` (Batch group / reel group / reel) and
`desktop.copy`, with their journal groups and `journal.revert` (run with the `.venv` Python;
`scripts/run_e2e.sh p2_3d_desktop` starts Flame).

Checks: `desktop.create` (Batch group, reel group, reel) and `desktop.copy`: names, `allow_rename`,
refusals, and `journal.revert`, including the cases where a revert is refused or skips an item that
changed. A default Batch group is compared with one that Flame itself makes with only a name (the
number of reels differs by Flame version).

Same shape and helpers as scripts/p2_3c_render_e2e.py (imported from p2_2a / p1 / p2_4_common): a
real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it prepares the material (a library with a reel and
two imported clips), and READS EVERY STATE BACK INDEPENDENTLY of the ops under test (the Batch
group's reels / shelf reels / nodes / range / current, the reel group's reels, a reel's clips with
their uids, `find_by_uid`). It also does the few things no op does: adds a node to a Batch group
(and takes it out), deletes a copy by hand (to make a revert meet a changed state), leaves a reel
group with one reel (to see what the revert of the last reel does), and at the end deletes what
this run made
(names starting with `P2D_<ts>` only). The journal directory is this run's own
(`p2/journal_p23d_e2e_<ts>/`). No test hook is set for any server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by what was
measured earlier. OBSERVED steps (`kind: "observation"`,
always ok) record what Flame did where nothing was measured yet: the name Flame gives a renamed new
one, where `current` goes when a created group is reverted, whether a Batch reel has a wiretap id,
what the library / folder paths of `desktop.copy` and a reel created in a library did.

Stages
  a  batch_group: default create (reels / shelf / range / current read back by transport; the
     reels, shelf reels and range are compared with a Batch group transport makes with a name
     only, so no count is fixed here: it differs by Flame version; the shelf reel 'Batch Renders',
     start frame 1 and duration 100 that the op description states are checked on their own), create
     with reels / shelf_reels / start_frame / duration, a taken name refused (nothing made),
     allow_rename, revert (gone; `current` as transport reads it), revert refused (CONFLICT) while
     the group holds a node, then done once it does not.
  b  reel_group: default reels, taken name refused, allow_rename, revert, revert refused while a
     reel holds a clip (a `desktop.copy` puts it there).
  c  reel: created at the end of a reel group, sequence=true, taken name refused, allow_rename,
     revert, revert refused while it holds a copy (then done after the copy's own revert), a reel in
     a library and its revert, the last reel of a reel group (Flame makes the default reels again).
  d  copy: library -> Desktop reel, Desktop reel -> Desktop reel, several at once (order and count),
     the same name twice (a second clip), a Batch reel refused (nothing copied, nothing journaled),
     revert of the several (the sources stay), a second revert refused, revert with one copy deleted
     by hand (skipped), copy into a library (observed).
  f  the journal: every group of this run is landed / reverted / conflict, none unknown; no intent
     left undecided, writes not blocked.
  z  cross-cutting checks; everything this run made is deleted by transport; then flame.exit().
"""

from __future__ import annotations

import argparse
import time
import traceback
from pathlib import Path
from typing import Any, Final

import anyio
import e2e_guard
from p1_mcp_e2e import (
    MEDIA_DIR,
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
    _br_dict,
    _result_of,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (
    Ctx,
    do,
    guarded,
    judged,
    rec,
    save,
    save_stage,
    server_session,
    tx,
    version_info,
)
from p2_4_common import (
    OUTPUT_DIR,
    flame_exit,
    journal_states,
    observe,
    ops_used,
    print_observations,
)

Results = dict[str, Any]

CLIP_NAMES: Final[tuple[str, str]] = ("clip1", "clip2")
DEFAULT_REELS: Final[list[str]] = ["Reel 1", "Reel 2", "Reel 3", "Reel 4", "Sequences"]
DEFAULT_START_FRAME: Final[int] = 1
DEFAULT_DURATION: Final[int] = 100
"""The defaults `desktop.create` states for a Batch group's range (its description; the reels and
the shelf reels Flame adds are compared with a group Flame makes itself, `batchRef`)."""
GOOD_STATES: Final[frozenset[str]] = frozenset({"landed", "reverted", "conflict"})
"""Journal states this run may leave: landed, reverted, and `conflict` (a revert or a create that
refused inside Flame and wrote nothing)."""


def name_of(ctx: Ctx, tail: str) -> str:
    return f"P2D_{ctx.ts}_{tail}"


def wt(selector: Any) -> dict[str, Any]:
    return dict(selector) if isinstance(selector, dict) else {}


def groups_of(ctx: Ctx) -> list[dict[str, str]]:
    return ctx.ids.setdefault("writeGroups", [])


async def wdo(session: Any, ctx: Ctx, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """`do` that remembers the journal group of a call that wrote (ok or refused inside Flame)."""
    env = await do(session, ctx, op, args)
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        groups_of(ctx).append({"op": op, "group": group})
    return env


async def revert(session: Any, ctx: Ctx, env: dict[str, Any], **extra: Any) -> dict[str, Any]:
    group = (env.get("journal") or {}).get("group")
    return await wdo(session, ctx, "journal.revert", {"group": group, **extra})


# Transport read-backs (independent of the ops under test)
async def tx_val(ctx: Ctx, code: str, label: str, timeout_s: float = 40.0) -> Any:
    br = await tx(ctx, code, label=label, timeout_s=timeout_s)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


def fill(code: str, **values: Any) -> str:
    """`code` with each `@NAME@` replaced by repr(value)."""
    for key, value in values.items():
        code = code.replace(f"@{key}@", repr(value))
    return code


CURRENT_CODE = "\n".join(
    [
        "_cur = _v(flame.projects.current_project.current_workspace.desktop.current_batch_group)",
        "_result = None if _cur is None else {'name': _v(_cur.name), 'uid': _v(_cur.uid)}",
    ]
)

BATCH_CODE = "\n".join(
    [
        "def _try(fn):",
        "    try:",
        "        return fn()",
        "    except BaseException as _e:",
        "        return 'ERR:' + type(_e).__name__",
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_m = [g for g in _d.batch_groups if _v(g.uid) == @UID@]",
        "_cur = _v(_d.current_batch_group)",
        "if not _m:",
        "    _result = {'found': False, 'currentUid': None if _cur is None else _v(_cur.uid)}",
        "else:",
        "    _g = _m[0]",
        "    _result = {",
        "        'found': True, 'name': _v(_g.name),",
        "        'isCurrent': _cur is not None and _v(_cur.uid) == @UID@,",
        "        'nodes': len(list(_g.nodes)),",
        "        'reels': [_v(r.name) for r in _g.reels],",
        "        'shelf': [_v(r.name) for r in _g.shelf_reels],",
        "        'reelClips': [len(list(r.clips)) for r in _g.reels],",
        "        'start': _try(lambda: _v(_g.start_frame)),",
        "        'duration': _try(lambda: _v(_g.duration)),",
        "        'currentUid': None if _cur is None else _v(_cur.uid),",
        "        'batchReelWiretap': _try(lambda: _g.reels[0].get_wiretap_node_id()),",
        "    }",
    ]
)

REFERENCE_BATCH_CODE = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_g = _d.create_batch_group(@NAME@)",
        "_result = {",
        "    'uid': _v(_g.uid), 'name': _v(_g.name), 'reels': [_v(r.name) for r in _g.reels],",
        "    'shelf': [_v(r.name) for r in _g.shelf_reels], 'start': _v(_g.start_frame),",
        "    'duration': _v(_g.duration), 'nodes': len(list(_g.nodes)),",
        "}",
    ]
)
"""A Batch group made by transport with a name and nothing else: what Flame of this version makes
by default (the reels, the shelf reels, the range). The group the op makes with no options is
compared with it, so no count or name is fixed in this script."""

BATCH_NAMES_CODE = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_result = [[_v(g.name), _v(g.uid)] for g in _d.batch_groups",
        "           if _v(g.name).startswith(@P@)]",
    ]
)

REEL_GROUP_CODE = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_m = [g for g in _d.reel_groups if _v(g.name) == @NAME@ or _v(g.uid) == @UID@]",
        "_result = [[_v(g.name), _v(g.uid), g.get_wiretap_node_id(),",
        "            [[_v(r.name), str(_v(r.type)), _v(r.uid), len(list(r.clips))]",
        "             for r in g.reels]] for g in _m]",
    ]
)

REEL_CODE = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else {",
        "    'type': type(_o).__name__, 'name': _v(_o.name),",
        "    'reelType': str(_v(_o.type)) if hasattr(_o, 'type') else None,",
        "    'clips': [[_v(c.name), _v(c.uid)] for c in _o.clips],",
        "}",
    ]
)

FIND_CODE = "\n".join(
    [
        "_o = flame.find_by_uid(@UID@)",
        "_result = None if _o is None else type(_o).__name__",
    ]
)

LIBRARY_REELS_CODE = "\n".join(
    [
        "_l = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _l is None else {",
        "    'reels': [_v(r.name) for r in _l.reels],",
        "    'clips': [[_v(c.name), _v(c.uid)] for c in _l.clips],",
        "}",
    ]
)


async def tx_current(ctx: Ctx) -> Any:
    return await tx_val(ctx, CURRENT_CODE, "p23d_current")


async def tx_batch(ctx: Ctx, uid: str) -> Any:
    return await tx_val(ctx, fill(BATCH_CODE, UID=uid), "p23d_batch")


async def tx_batch_names(ctx: Ctx) -> Any:
    return await tx_val(ctx, fill(BATCH_NAMES_CODE, P=f"P2D_{ctx.ts}"), "p23d_batch_names")


async def tx_reel_group(ctx: Ctx, name: str, uid: str = "") -> Any:
    return await tx_val(ctx, fill(REEL_GROUP_CODE, NAME=name, UID=uid), "p23d_reel_group")


async def tx_reel(ctx: Ctx, wiretap: str) -> Any:
    return await tx_val(ctx, fill(REEL_CODE, WT=wiretap), "p23d_reel")


async def tx_find(ctx: Ctx, uid: str) -> Any:
    return await tx_val(ctx, fill(FIND_CODE, UID=uid), "p23d_find")


async def tx_library(ctx: Ctx) -> Any:
    return await tx_val(ctx, fill(LIBRARY_REELS_CODE, WT=ctx.ids["libWt"]), "p23d_library")


def reel_names(rows: Any) -> list[str]:
    return [r[0] for r in rows] if isinstance(rows, list) else []


async def prepare(ctx: Ctx) -> None:
    reference = await tx(
        ctx,
        fill(REFERENCE_BATCH_CODE, NAME=name_of(ctx, "BGREF")),
        label="p23d_prepare_reference_batch_group",
        timeout_s=60.0,
    )
    rec(
        ctx,
        "prepare_reference_batch_group_made_by_transport",
        reference.ok,
        resp=_br_dict(reference),
    )
    if not (reference.ok and isinstance(reference.result, dict)):
        return
    ctx.ids["batchRef"] = reference.result
    lib_name = name_of(ctx, "LIB")
    paths = [str(MEDIA_DIR / f"{n}.mov") for n in CLIP_NAMES]
    made = await tx(
        ctx,
        "\n".join(
            [
                "ws = flame.projects.current_project.current_workspace",
                f"lib = ws.create_library({lib_name!r})",
                "reel = lib.create_reel('R1')",
                "_clips = []",
                f"for _p in {paths!r}:",
                "    _c = flame.import_clips(_p, reel)[0]",
                "    _clips.append([_v(_c.name), _c.get_wiretap_node_id(), _v(_c.uid)])",
                "_result = {'libWt': lib.get_wiretap_node_id(),",
                "           'reelWt': reel.get_wiretap_node_id(), 'clips': _clips}",
            ]
        ),
        label="p23d_prepare_library",
        timeout_s=120.0,
    )
    rec(ctx, "prepare_library_with_2_clips", made.ok, resp=_br_dict(made))
    if made.ok and isinstance(made.result, dict):
        ctx.ids.update(libName=lib_name, **made.result)
        ctx.ids["clipSelectors"] = [{"kind": "wiretap", "id": c[1]} for c in made.result["clips"]]
        ctx.ids["clipUids"] = [c[2] for c in made.result["clips"]]


# stage a: Batch groups
async def conflict_with_a_node(session: Any, ctx: Ctx) -> None:
    """A fresh group gets a node from transport: its revert is refused (CONFLICT), then done once
    the node is gone. The group's name is explicit and shares no stem with a name Flame may give a
    renamed one (e.g. `BG1` became `BG3`), and a failed create ends the step with its reason."""
    n3 = name_of(ctx, "BGNODE")
    a6 = await wdo(session, ctx, "desktop.create", {"kind": "batch_group", "name": n3})
    uid6 = _result_of(a6).get("uid")
    if a6.get("ok") is not True or not isinstance(uid6, str):
        rec(
            ctx,
            "a6_the_group_for_the_node_test_is_made",
            False,
            error=a6.get("error"),
            errorCode=a6.get("errorCode"),
            skipped="a6 revert-refusal steps, which need this group",
        )
        return
    added = await tx_val(
        ctx,
        fill(
            "\n".join(
                [
                    "_d = flame.projects.current_project.current_workspace.desktop",
                    "_g = [g for g in _d.batch_groups if _v(g.uid) == @UID@][0]",
                    "_n = _g.create_node('Blur')",
                    "_result = _v(_n.name)",
                ]
            ),
            UID=uid6,
        ),
        "p23d_add_node",
    )
    a6r = await revert(session, ctx, a6)
    row6 = await tx_batch(ctx, uid6)
    rec(
        ctx,
        "a6_revert_is_refused_while_the_group_holds_a_node_and_nothing_is_deleted",
        a6.get("ok") is True
        and a6r.get("ok") is False
        and a6r.get("errorCode") == "CONFLICT"
        and isinstance(row6, dict)
        and row6.get("found") is True
        and row6.get("nodes") == 1,
        addedNode=added,
        error=a6r.get("error"),
        conflicts=_result_of(a6r).get("conflicts"),
    )
    await tx_val(
        ctx,
        fill(
            "\n".join(
                [
                    "_d = flame.projects.current_project.current_workspace.desktop",
                    "_g = [g for g in _d.batch_groups if _v(g.uid) == @UID@][0]",
                    "_n = list(_g.nodes)[0]",
                    "_n.delete(confirm=False)",
                    "_result = len(list(_g.nodes))",
                ]
            ),
            UID=uid6,
        ),
        "p23d_remove_node",
    )
    a6r2 = await revert(session, ctx, a6)
    row6b = await tx_batch(ctx, uid6)
    rec(
        ctx,
        "a6_after_the_node_is_gone_the_same_revert_deletes_the_group",
        a6r2.get("ok") is True and isinstance(row6b, dict) and row6b.get("found") is False,
        error=a6r2.get("error"),
    )


async def stage_a(ctx: Ctx) -> None:
    async with server_session(ctx, "d-a") as session:
        n1, n2 = name_of(ctx, "BG1"), name_of(ctx, "BGOPT")
        current_before = await tx_current(ctx)
        a1 = await wdo(session, ctx, "desktop.create", {"kind": "batch_group", "name": n1})
        r1 = _result_of(a1)
        uid1 = str(r1.get("uid"))
        row = await tx_batch(ctx, uid1)
        cur_after = await tx_current(ctx)
        ctx.ids["a1"] = a1
        reference = ctx.ids["batchRef"]
        ok, parts = judged(
            op_ok=a1.get("ok") is True,
            result_names_the_group=r1.get("name") == n1,
            result_selector_is_the_batch_uid=r1.get("selector") == {"kind": "batch", "uid": uid1},
            result_reels_are_the_reference_ones=r1.get("reels") == reference["reels"],
            result_shelf_reels_are_the_reference_ones=r1.get("shelfReels") == reference["shelf"],
            result_range_is_the_reference_one=(
                r1.get("startFrame"),
                r1.get("duration"),
                r1.get("nodeCount"),
            )
            == (reference["start"], reference["duration"], reference["nodes"]),
            transport_found_the_group=isinstance(row, dict) and row.get("found") is True,
            transport_name=isinstance(row, dict) and row.get("name") == n1,
            transport_reels_are_the_reference_ones=isinstance(row, dict)
            and row.get("reels") == reference["reels"],
            transport_shelf_reels_are_the_reference_ones=isinstance(row, dict)
            and row.get("shelf") == reference["shelf"],
            transport_range_is_the_reference_one=isinstance(row, dict)
            and (row.get("start"), row.get("duration"), row.get("nodes"))
            == (reference["start"], reference["duration"], reference["nodes"]),
        )
        rec(
            ctx,
            "a1_default_batch_group_is_made_and_transport_reads_the_same_group_back",
            ok,
            clauses=parts,
            error=a1.get("error"),
            result={
                k: r1.get(k) for k in ("name", "reels", "shelfReels", "startFrame", "duration")
            },
            transport=row,
            reference=reference,
        )
        ok, parts = judged(
            op_ok=a1.get("ok") is True,
            result_shelf_reels_hold_batch_renders="Batch Renders" in (r1.get("shelfReels") or []),
            result_start_frame_is_1=r1.get("startFrame") == DEFAULT_START_FRAME,
            result_duration_is_100=r1.get("duration") == DEFAULT_DURATION,
            transport_shelf_reels_hold_batch_renders=isinstance(row, dict)
            and "Batch Renders" in (row.get("shelf") or []),
            transport_start_frame_is_1=isinstance(row, dict)
            and row.get("start") == DEFAULT_START_FRAME,
            transport_duration_is_100=isinstance(row, dict)
            and row.get("duration") == DEFAULT_DURATION,
        )
        rec(
            ctx,
            "a1_the_defaults_the_op_description_states_shelf_reel_start_frame_and_duration",
            ok,
            clauses=parts,
            result={k: r1.get(k) for k in ("shelfReels", "startFrame", "duration")},
            transport=row,
        )
        rec(
            ctx,
            "a1_current_moved_to_the_new_group_as_transport_reads_it_and_the_result_says_so",
            isinstance(cur_after, dict)
            and cur_after.get("uid") == uid1
            and r1.get("currentMoved") is True
            and (r1.get("current") or {}).get("uid") == uid1
            and (r1.get("previousCurrent") or {}).get("uid") == (current_before or {}).get("uid")
            and any("current moved" in n for n in r1.get("notes", [])),
            before=current_before,
            after=cur_after,
            previousCurrent=r1.get("previousCurrent"),
        )

        a2 = await wdo(
            session,
            ctx,
            "desktop.create",
            {
                "kind": "batch_group",
                "name": n2,
                "reels": ["FR_A", "FR_B"],
                "shelf_reels": ["FS_A"],
                "start_frame": 1001,
                "duration": 48,
            },
        )
        r2 = _result_of(a2)
        row2 = await tx_batch(ctx, str(r2.get("uid")))
        ctx.ids["a2"] = a2
        rec(
            ctx,
            "a2_reels_shelf_reels_start_frame_and_duration_are_made_as_asked_and_read_back",
            a2.get("ok") is True
            and r2.get("reels") == ["FR_A", "FR_B"]
            and r2.get("shelfReels") == ["FS_A"]
            and (r2.get("startFrame"), r2.get("duration")) == (1001, 48)
            and isinstance(row2, dict)
            and (row2.get("reels"), row2.get("shelf")) == (["FR_A", "FR_B"], ["FS_A"])
            and (row2.get("start"), row2.get("duration")) == (1001, 48)
            and a2.get("degraded") == [],
            error=a2.get("error"),
            transport=row2,
            degraded=a2.get("degraded"),
        )

        names_before = await tx_batch_names(ctx)
        a3 = await wdo(session, ctx, "desktop.create", {"kind": "batch_group", "name": n1})
        names_after = await tx_batch_names(ctx)
        cur3 = await tx_current(ctx)
        rec(
            ctx,
            "a3_a_taken_name_is_refused_and_transport_sees_nothing_new",
            a3.get("ok") is False
            and a3.get("errorCode") == "PRECONDITION_UNMET"
            and n1 in (_result_of(a3).get("existingNames") or [])
            and names_before == names_after
            and (cur3 or {}).get("uid") == str(r2.get("uid"))
            and a3.get("journal") is None,
            error=a3.get("error"),
            namesBefore=names_before,
            namesAfter=names_after,
        )

        a4 = await wdo(
            session,
            ctx,
            "desktop.create",
            {"kind": "batch_group", "name": n1, "allow_rename": True},
        )
        r4 = _result_of(a4)
        row4 = await tx_batch(ctx, str(r4.get("uid")))
        ctx.ids["a4"] = a4
        rec(
            ctx,
            "a4_allow_rename_makes_a_group_under_another_name_and_says_so",
            a4.get("ok") is True
            and r4.get("requestedName") == n1
            and r4.get("renamed") is True
            and r4.get("name") != n1
            and isinstance(row4, dict)
            and row4.get("name") == r4.get("name"),
            error=a4.get("error"),
            gotName=r4.get("name"),
        )
        observe(ctx, "a4_the_name_flame_gave", requested=n1, actual=r4.get("name"))

        a5 = await revert(session, ctx, ctx.ids["a1"])
        row5 = await tx_batch(ctx, uid1)
        cur5 = await tx_current(ctx)
        r5 = _result_of(a5)
        rec(
            ctx,
            "a5_revert_deletes_the_group_and_result_current_is_what_transport_reads",
            a5.get("ok") is True
            and r5.get("deleted") is True
            and isinstance(row5, dict)
            and row5.get("found") is False
            and (cur5 or {}).get("uid") == (r5.get("current") or {}).get("uid")
            and (r5.get("previousCurrent") or {}).get("uid") == (current_before or {}).get("uid"),
            error=a5.get("error"),
            current=cur5,
            reported=r5.get("current"),
        )
        observe(
            ctx,
            "a5_where_current_went_after_the_revert",
            currentNow=cur5,
            currentBeforeTheCreate=current_before,
            backToTheOldOne=(cur5 or {}).get("uid") == (current_before or {}).get("uid"),
            notes=r5.get("notes"),
        )

        await conflict_with_a_node(session, ctx)

        for label, env in (("a2", ctx.ids["a2"]), ("a4", ctx.ids["a4"])):
            done = await revert(session, ctx, env)
            gone = await tx_batch(ctx, str(_result_of(env).get("uid")))
            rec(
                ctx,
                f"a7_{label}_revert_deletes_the_group_made_with_options_or_a_new_name",
                done.get("ok") is True and isinstance(gone, dict) and gone.get("found") is False,
                error=done.get("error"),
            )


async def stage_b(ctx: Ctx) -> None:
    async with server_session(ctx, "d-b") as session:
        g1 = name_of(ctx, "RG1")
        current_before = await tx_current(ctx)
        b1 = await wdo(session, ctx, "desktop.create", {"kind": "reel_group", "name": g1})
        r1 = _result_of(b1)
        rows = await tx_reel_group(ctx, g1, str(r1.get("uid")))
        current_after = await tx_current(ctx)
        ctx.ids["rg1"] = b1
        rec(
            ctx,
            "b1_reel_group_is_made_with_the_default_reels_as_transport_reads_them",
            b1.get("ok") is True
            and [r.get("name") for r in r1.get("reelRows", [])] == DEFAULT_REELS
            and r1["reelRows"][-1].get("type") == "Sequences"
            and isinstance(rows, list)
            and len(rows) == 1
            and reel_names(rows[0][3]) == DEFAULT_REELS
            and rows[0][3][-1][1] == "Sequences",
            error=b1.get("error"),
            transport=rows,
        )
        rec(
            ctx,
            "b1_a_reel_group_does_not_move_current",
            current_before == current_after and r1.get("previousCurrent") is None,
            before=current_before,
            after=current_after,
        )
        if isinstance(rows, list) and rows:
            ctx.ids["rg1Wt"] = rows[0][2]
            ctx.ids["rg1Selector"] = {"kind": "wiretap", "id": rows[0][2]}

        b2 = await wdo(session, ctx, "desktop.create", {"kind": "reel_group", "name": g1})
        rows2 = await tx_reel_group(ctx, g1)
        rec(
            ctx,
            "b2_a_taken_reel_group_name_is_refused_and_only_one_group_has_it",
            b2.get("ok") is False
            and b2.get("errorCode") == "PRECONDITION_UNMET"
            and g1 in (_result_of(b2).get("existingNames") or [])
            and isinstance(rows2, list)
            and len(rows2) == 1
            and b2.get("journal") is None,
            error=b2.get("error"),
        )

        b3 = await wdo(
            session, ctx, "desktop.create", {"kind": "reel_group", "name": g1, "allow_rename": True}
        )
        r3 = _result_of(b3)
        rows3 = await tx_reel_group(ctx, str(r3.get("name")), str(r3.get("uid")))
        rec(
            ctx,
            "b3_allow_rename_makes_another_reel_group_and_says_so",
            b3.get("ok") is True
            and r3.get("renamed") is True
            and r3.get("requestedName") == g1
            and r3.get("name") != g1
            and isinstance(rows3, list)
            and len(rows3) == 1,
            error=b3.get("error"),
            gotName=r3.get("name"),
        )
        observe(ctx, "b3_the_name_flame_gave", requested=g1, actual=r3.get("name"))
        b4 = await revert(session, ctx, b3)
        rows4 = await tx_reel_group(ctx, str(r3.get("name")), str(r3.get("uid")))
        rec(
            ctx,
            "b4_revert_deletes_the_renamed_reel_group",
            b4.get("ok") is True and rows4 == [],
            error=b4.get("error"),
        )

        g5 = name_of(ctx, "RGCOPY")
        b5 = await wdo(session, ctx, "desktop.create", {"kind": "reel_group", "name": g5})
        r5 = _result_of(b5)
        reel1 = (r5.get("reelRows") or [{}])[0].get("selector")
        copied = None
        if reel1 is not None:
            copied = await wdo(
                session,
                ctx,
                "desktop.copy",
                {"sources": [ctx.ids["clipSelectors"][0]], "destination": reel1},
            )
        b5r = await revert(session, ctx, b5)
        rows5b = await tx_reel_group(ctx, g5, str(r5.get("uid")))
        rec(
            ctx,
            "b5_revert_is_refused_while_a_reel_of_the_group_holds_a_clip",
            b5.get("ok") is True
            and copied is not None
            and copied.get("ok") is True
            and b5r.get("ok") is False
            and b5r.get("errorCode") == "CONFLICT"
            and isinstance(rows5b, list)
            and len(rows5b) == 1
            and rows5b[0][3][0][3] == 1,
            copyError=None if copied is None else copied.get("error"),
            error=b5r.get("error"),
            conflicts=_result_of(b5r).get("conflicts"),
        )
        ctx.ids["rg5"] = b5
        ctx.ids["rg5Copy"] = copied


async def stage_c(ctx: Ctx) -> None:
    parent = ctx.ids.get("rg1Selector")
    if parent is None:
        rec(ctx, "c_skipped_no_reel_group_from_stage_b", False)
        return
    g1 = name_of(ctx, "RG1")
    async with server_session(ctx, "d-c") as session:
        l1 = name_of(ctx, "RL1")
        c1 = await wdo(
            session, ctx, "desktop.create", {"kind": "reel", "name": l1, "parent": parent}
        )
        r1 = _result_of(c1)
        rows = await tx_reel_group(ctx, g1)
        reels = rows[0][3] if isinstance(rows, list) and rows else []
        ctx.ids["rl1"] = c1
        ctx.ids["rl1Selector"] = r1.get("selector")
        rec(
            ctx,
            "c1_reel_is_added_at_the_end_of_the_reel_group_as_type_reel",
            c1.get("ok") is True
            and r1.get("name") == l1
            and r1.get("type") == "Reel"
            and reel_names(reels) == [*DEFAULT_REELS, l1]
            and reels[-1][1] == "Reel",
            error=c1.get("error"),
            transport=reels,
        )
        c2 = await wdo(
            session,
            ctx,
            "desktop.create",
            {"kind": "reel", "name": name_of(ctx, "RLS"), "parent": parent, "sequence": True},
        )
        rows2 = await tx_reel_group(ctx, g1)
        reels2 = rows2[0][3] if isinstance(rows2, list) and rows2 else []
        rec(
            ctx,
            "c2_sequence_true_makes_a_reel_of_type_sequences",
            c2.get("ok") is True
            and _result_of(c2).get("type") == "Sequences"
            and bool(reels2)
            and reels2[-1][1] == "Sequences"
            and reels2[-1][0] == name_of(ctx, "RLS"),
            error=c2.get("error"),
            transport=reels2,
        )
        c3 = await wdo(
            session, ctx, "desktop.create", {"kind": "reel", "name": l1, "parent": parent}
        )
        rec(
            ctx,
            "c3_a_taken_reel_name_is_refused_and_lists_the_reels",
            c3.get("ok") is False
            and c3.get("errorCode") == "PRECONDITION_UNMET"
            and l1 in (_result_of(c3).get("existingNames") or [])
            and c3.get("journal") is None,
            error=c3.get("error"),
        )
        c4 = await wdo(
            session,
            ctx,
            "desktop.create",
            {"kind": "reel", "name": l1, "parent": parent, "allow_rename": True},
        )
        r4 = _result_of(c4)
        rows4 = await tx_reel_group(ctx, g1)
        reels4 = rows4[0][3] if isinstance(rows4, list) and rows4 else []
        rec(
            ctx,
            "c4_allow_rename_makes_another_reel_and_says_so",
            c4.get("ok") is True
            and r4.get("renamed") is True
            and r4.get("name") != l1
            and r4.get("name") in reel_names(reels4),
            error=c4.get("error"),
            gotName=r4.get("name"),
        )
        observe(ctx, "c4_the_name_flame_gave", requested=l1, actual=r4.get("name"))
        c5 = await revert(session, ctx, c4)
        rows5 = await tx_reel_group(ctx, g1)
        reels5 = rows5[0][3] if isinstance(rows5, list) and rows5 else []
        rec(
            ctx,
            "c5_revert_deletes_the_renamed_reel",
            c5.get("ok") is True and r4.get("name") not in reel_names(reels5),
            error=c5.get("error"),
        )

        l3 = name_of(ctx, "RLCOPY")
        c6 = await wdo(
            session, ctx, "desktop.create", {"kind": "reel", "name": l3, "parent": parent}
        )
        sel6 = _result_of(c6).get("selector")
        copy6 = await wdo(
            session,
            ctx,
            "desktop.copy",
            {"sources": [ctx.ids["clipSelectors"][0]], "destination": sel6},
        )
        c6r = await revert(session, ctx, c6)
        reel6 = await tx_reel(ctx, str(wt(sel6).get("id")))
        rec(
            ctx,
            "c6_revert_is_refused_while_the_reel_holds_a_copy_and_the_reel_stays",
            c6.get("ok") is True
            and copy6.get("ok") is True
            and c6r.get("ok") is False
            and c6r.get("errorCode") == "CONFLICT"
            and isinstance(reel6, dict)
            and len(reel6.get("clips", [])) == 1,
            error=c6r.get("error"),
            conflicts=_result_of(c6r).get("conflicts"),
        )
        undone = await revert(session, ctx, copy6)
        c6r2 = await revert(session, ctx, c6)
        reel6b = await tx_reel(ctx, str(wt(sel6).get("id")))
        rec(
            ctx,
            "c6_after_the_copy_is_reverted_the_reel_revert_deletes_the_reel",
            undone.get("ok") is True and c6r2.get("ok") is True and reel6b is None,
            copyRevert=undone.get("error"),
            error=c6r2.get("error"),
            transport=reel6b,
        )

        lib = {"kind": "wiretap", "id": ctx.ids["libWt"]}
        lr = name_of(ctx, "LR")
        c7 = await wdo(session, ctx, "desktop.create", {"kind": "reel", "name": lr, "parent": lib})
        libs7 = await tx_library(ctx)
        rec(
            ctx,
            "c7_a_reel_in_a_library_is_made_and_transport_sees_it",
            c7.get("ok") is True
            and isinstance(libs7, dict)
            and lr in libs7.get("reels", [])
            and (_result_of(c7).get("parent") or {}).get("type") == "PyLibrary",
            error=c7.get("error"),
            transport=libs7,
        )
        c7r = await revert(session, ctx, c7)
        libs7b = await tx_library(ctx)
        rec(
            ctx,
            "c7_and_its_revert_deletes_it",
            c7r.get("ok") is True
            and isinstance(libs7b, dict)
            and lr not in libs7b.get("reels", []),
            error=c7r.get("error"),
            transport=libs7b,
        )
        c7s = await wdo(
            session,
            ctx,
            "desktop.create",
            {"kind": "reel", "name": name_of(ctx, "LS"), "parent": lib, "sequence": True},
        )
        rec(
            ctx,
            "c7_sequence_true_in_a_library_is_refused_and_nothing_is_made",
            c7s.get("ok") is False
            and c7s.get("errorCode") == "INVALID_ARGS"
            and c7s.get("journal") is None,
            error=c7s.get("error"),
        )

        g8 = name_of(ctx, "RGLAST")
        c8g = await wdo(session, ctx, "desktop.create", {"kind": "reel_group", "name": g8})
        rows8 = await tx_reel_group(ctx, g8, str(_result_of(c8g).get("uid")))
        sel8 = {"kind": "wiretap", "id": rows8[0][2]} if isinstance(rows8, list) and rows8 else None
        if sel8 is not None:
            c8 = await wdo(
                session,
                ctx,
                "desktop.create",
                {"kind": "reel", "name": name_of(ctx, "RLLAST"), "parent": sel8},
            )
            only = await tx_val(
                ctx,
                fill(
                    "\n".join(
                        [
                            "_d = flame.projects.current_project.current_workspace.desktop",
                            "_g = [g for g in _d.reel_groups if _v(g.uid) == @UID@][0]",
                            "for _r in list(_g.reels):",
                            "    if _v(_r.name) != @KEEP@:",
                            "        flame.delete(_r, confirm=False)",
                            "_result = [_v(_r.name) for _r in _g.reels]",
                        ]
                    ),
                    UID=str(_result_of(c8g).get("uid")),
                    KEEP=name_of(ctx, "RLLAST"),
                ),
                "p23d_leave_one_reel",
            )
            c8r = await revert(session, ctx, c8)
            after8 = await tx_reel_group(ctx, g8, str(_result_of(c8g).get("uid")))
            rec(
                ctx,
                "c8_reverting_the_last_reel_makes_flame_create_the_default_reels_again",
                c8.get("ok") is True
                and only == [name_of(ctx, "RLLAST")]
                and c8r.get("ok") is True
                and isinstance(after8, list)
                and len(after8) == 1
                and reel_names(after8[0][3]) == DEFAULT_REELS
                and any("default reels" in n for n in _result_of(c8r).get("notes", [])),
                error=c8r.get("error"),
                onlyReel=only,
                after=after8,
            )
        else:
            rec(ctx, "c8_skipped_no_wiretap_for_the_reel_group", False)


# copy
async def make_reel(session: Any, ctx: Ctx, tail: str) -> Any:
    """The selector of a new Desktop reel (in the reel group of stage b)."""
    env = await wdo(
        session,
        ctx,
        "desktop.create",
        {"kind": "reel", "name": name_of(ctx, tail), "parent": ctx.ids["rg1Selector"]},
    )
    return _result_of(env).get("selector")


async def stage_d(ctx: Ctx) -> None:
    if "rg1Selector" not in ctx.ids:
        rec(ctx, "d_skipped_no_reel_group_from_stage_b", False)
        return
    clip1, clip2 = ctx.ids["clipSelectors"]
    uid1, uid2 = ctx.ids["clipUids"]
    async with server_session(ctx, "d-d") as session:
        dest1 = await make_reel(session, ctx, "D1")
        d1 = await wdo(session, ctx, "desktop.copy", {"sources": [clip1], "destination": dest1})
        r1 = _result_of(d1)
        reel1 = await tx_reel(ctx, str(wt(dest1).get("id")))
        found_src = await tx_find(ctx, uid1)
        copy1 = (r1.get("copies") or [{}])[0]
        rec(
            ctx,
            "d1_library_clip_into_a_desktop_reel_makes_a_new_clip_and_the_source_stays",
            d1.get("ok") is True
            and r1.get("requested") == r1.get("returned") == 1
            and copy1.get("name") == "clip1"
            and copy1.get("uid") not in (uid1, uid2, None)
            and copy1.get("sourceUid") == uid1
            and r1.get("sourcesRemain") is True
            and isinstance(reel1, dict)
            and reel1.get("clips") == [["clip1", copy1.get("uid")]]
            and found_src == "PyClip",
            error=d1.get("error"),
            transport=reel1,
            copy=copy1,
        )

        dest2 = await make_reel(session, ctx, "D2")
        d2 = await wdo(
            session,
            ctx,
            "desktop.copy",
            {"sources": [copy1.get("selector")], "destination": dest2},
        )
        r2 = _result_of(d2)
        reel2 = await tx_reel(ctx, str(wt(dest2).get("id")))
        reel1b = await tx_reel(ctx, str(wt(dest1).get("id")))
        c2 = (r2.get("copies") or [{}])[0]
        rec(
            ctx,
            "d2_desktop_clip_into_another_reel_copies_it_and_the_first_reel_keeps_its_clip",
            d2.get("ok") is True
            and c2.get("uid") not in (None, copy1.get("uid"))
            and c2.get("sourceUid") == copy1.get("uid")
            and isinstance(reel2, dict)
            and reel2.get("clips") == [["clip1", c2.get("uid")]]
            and isinstance(reel1b, dict)
            and reel1b.get("clips") == [["clip1", copy1.get("uid")]],
            error=d2.get("error"),
        )

        dest3 = await make_reel(session, ctx, "D3")
        d3 = await wdo(
            session, ctx, "desktop.copy", {"sources": [clip1, clip2], "destination": dest3}
        )
        r3 = _result_of(d3)
        reel3 = await tx_reel(ctx, str(wt(dest3).get("id")))
        copies3 = r3.get("copies") or []
        rec(
            ctx,
            "d3_several_at_once_come_back_in_the_order_given_and_land_in_the_reel",
            d3.get("ok") is True
            and [c.get("name") for c in copies3] == ["clip1", "clip2"]
            and [c.get("sourceUid") for c in copies3] == [uid1, uid2]
            and isinstance(reel3, dict)
            and reel3.get("clips") == [[c.get("name"), c.get("uid")] for c in copies3]
            and len({c.get("uid") for c in copies3}) == 2,
            error=d3.get("error"),
            transport=reel3,
        )

        d4 = await wdo(session, ctx, "desktop.copy", {"sources": [clip1], "destination": dest3})
        reel3b = await tx_reel(ctx, str(wt(dest3).get("id")))
        rec(
            ctx,
            "d4_the_same_name_again_adds_a_second_clip",
            d4.get("ok") is True
            and isinstance(reel3b, dict)
            and [c[0] for c in reel3b.get("clips", [])].count("clip1") == 2
            and len(reel3b.get("clips", [])) == 3,
            error=d4.get("error"),
            transport=reel3b,
        )

        bname = name_of(ctx, "BGD")
        bg = await wdo(session, ctx, "desktop.create", {"kind": "batch_group", "name": bname})
        brow = await tx_batch(ctx, str(_result_of(bg).get("uid")))
        batch_wt = brow.get("batchReelWiretap") if isinstance(brow, dict) else None
        journal_before = len(groups_of(ctx))
        if isinstance(batch_wt, str) and not batch_wt.startswith("ERR:"):
            d5 = await wdo(
                session,
                ctx,
                "desktop.copy",
                {"sources": [clip1], "destination": {"kind": "wiretap", "id": batch_wt}},
            )
            brow2 = await tx_batch(ctx, str(_result_of(bg).get("uid")))
            reference = ctx.ids["batchRef"]
            ok, parts = judged(
                op_refused=d5.get("ok") is False,
                error_code_is_invalid_args=d5.get("errorCode") == "INVALID_ARGS",
                hint_points_to_add_clips="batch.add_clips" in str(d5.get("hint")),
                nothing_was_journaled=len(groups_of(ctx)) == journal_before,
                transport_reads_the_group=isinstance(brow2, dict),
                the_group_holds_no_node=isinstance(brow2, dict) and brow2.get("nodes") == 0,
                the_group_has_the_reference_reels=isinstance(brow2, dict)
                and brow2.get("reels") == reference["reels"],
                no_reel_holds_a_clip=isinstance(brow2, dict)
                and brow2.get("reelClips") == [0] * len(reference["reels"]),
            )
            rec(
                ctx,
                "d5_a_reel_of_a_batch_group_is_refused_and_points_to_add_clips",
                ok,
                clauses=parts,
                error=d5.get("error"),
                hint=d5.get("hint"),
                transport=brow2,
            )
        else:
            observe(
                ctx,
                "d5_a_batch_reel_has_no_wiretap_id_so_the_refusal_cannot_be_asked_for_here",
                batchReelWiretap=batch_wt,
            )
        await revert(session, ctx, bg)

        d6 = await revert(session, ctx, d3)
        reel3c = await tx_reel(ctx, str(wt(dest3).get("id")))
        uid_d4 = (_result_of(d4).get("copies") or [{}])[0].get("uid")
        rec(
            ctx,
            "d6_revert_of_the_several_deletes_exactly_those_and_keeps_the_others_and_the_sources",
            d6.get("ok") is True
            and len(_result_of(d6).get("deleted") or []) == 2
            and _result_of(d6).get("skipped") == []
            and isinstance(reel3c, dict)
            and reel3c.get("clips") == [["clip1", uid_d4]]
            and await tx_find(ctx, uid1) == "PyClip"
            and await tx_find(ctx, uid2) == "PyClip",
            error=d6.get("error"),
            transport=reel3c,
        )
        d7 = await revert(session, ctx, d3)
        rec(
            ctx,
            "d7_a_second_revert_of_the_same_group_is_refused",
            d7.get("ok") is False and d7.get("errorCode") == "PRECONDITION_UNMET",
            error=d7.get("error"),
        )

        d8c = await wdo(
            session, ctx, "desktop.copy", {"sources": [clip1, clip2], "destination": dest3}
        )
        copies8 = _result_of(d8c).get("copies") or []
        by_hand = await tx_val(
            ctx,
            fill(
                "\n".join(
                    [
                        "_o = flame.find_by_uid(@UID@)",
                        "_result = flame.delete(_o, confirm=False) if _o is not None else None",
                    ]
                ),
                UID=copies8[0].get("uid") if copies8 else "",
            ),
            "p23d_delete_a_copy_by_hand",
        )
        d8 = await revert(session, ctx, d8c)
        reel3d = await tx_reel(ctx, str(wt(dest3).get("id")))
        rec(
            ctx,
            "d8_a_copy_deleted_by_hand_is_skipped_and_the_other_is_deleted",
            d8c.get("ok") is True
            and by_hand is True
            and d8.get("ok") is True
            and [s.get("uid") for s in _result_of(d8).get("skipped") or []]
            == [copies8[0].get("uid")]
            and [x.get("uid") for x in _result_of(d8).get("deleted") or []]
            == [copies8[1].get("uid")]
            and isinstance(reel3d, dict)
            and reel3d.get("clips") == [["clip1", uid_d4]],
            error=d8.get("error"),
            transport=reel3d,
        )

        lib = {"kind": "wiretap", "id": ctx.ids["libWt"]}
        d9 = await wdo(
            session, ctx, "desktop.copy", {"sources": [copy1.get("selector")], "destination": lib}
        )
        libs9 = await tx_library(ctx)
        observe(
            ctx,
            "d9_copy_into_a_library_was_not_measured",
            opOk=d9.get("ok"),
            error=d9.get("error"),
            warnings=_result_of(d9).get("warnings"),
            inDestination=[c.get("inDestination") for c in _result_of(d9).get("copies") or []],
            libraryNow=libs9,
        )
        if d9.get("ok") is True:
            d9r = await revert(session, ctx, d9)
            libs9b = await tx_library(ctx)
            observe(
                ctx,
                "d9_and_its_revert",
                opOk=d9r.get("ok"),
                error=d9r.get("error"),
                libraryNow=libs9b,
            )

        d10c = await wdo(session, ctx, "desktop.copy", {"sources": [clip1], "destination": dest3})
        copy10 = (_result_of(d10c).get("copies") or [{}])[0]
        rename_code = "\n".join(
            [
                "_o = flame.find_by_uid(@UID@)",
                "_o.name.set_value(@NAME@)",
                "_result = _v(_o.name)",
            ]
        )
        await tx_val(
            ctx,
            fill(rename_code, UID=copy10.get("uid"), NAME="renamed by hand"),
            "p23d_rename_a_copy_by_hand",
        )
        d10 = await revert(session, ctx, d10c)
        still = await tx_find(ctx, str(copy10.get("uid")))
        await tx_val(
            ctx,
            fill(rename_code, UID=copy10.get("uid"), NAME=copy10.get("name")),
            "p23d_rename_it_back",
        )
        d10b = await revert(session, ctx, d10c)
        rec(
            ctx,
            "d10_a_copy_that_was_renamed_is_a_conflict_that_deletes_nothing_then_reverts_once_restored",
            d10c.get("ok") is True
            and d10.get("ok") is False
            and d10.get("errorCode") == "CONFLICT"
            and still == "PyClip"
            and d10b.get("ok") is True
            and await tx_find(ctx, str(copy10.get("uid"))) is None,
            conflictError=d10.get("error"),
            error=d10b.get("error"),
        )


async def stage_f(ctx: Ctx) -> None:
    written = groups_of(ctx)
    async with server_session(ctx, "d-f") as session:
        listing = await do(session, ctx, "journal.list", {"limit": 100})
        items = _result_of(listing).get("items", [])
        states = {i.get("group"): i.get("state") for i in items}
        own = journal_states(ctx)
        rec(
            ctx,
            "f_every_group_of_this_run_is_landed_reverted_or_conflict",
            listing.get("ok") is True
            and len(own) == len(written)
            and set(own.values()) <= GOOD_STATES
            and all(states.get(w["group"]) in GOOD_STATES for w in written),
            groups=len(own),
            states=sorted(set(own.values())),
        )
        info = _result_of(await version_info(session, ctx)).get("journal", {})
        rec(
            ctx,
            "f_no_intent_is_left_undecided_and_writes_are_not_blocked",
            info.get("unresolvedIntents") == 0 and info.get("writesBlocked") is False,
            journal={k: info.get(k) for k in ("unresolvedIntents", "writesBlocked")},
        )


CLEANUP_CODE: Final[str] = "\n".join(
    [
        "_p = @P@",
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_gone = {'batchGroups': [], 'reelGroups': [], 'libraries': []}",
        "for _g in list(_d.batch_groups):",
        "    if _v(_g.name).startswith(_p):",
        "        _gone['batchGroups'].append([_v(_g.name), flame.delete(_g, confirm=False)])",
        "for _g in list(_d.reel_groups):",
        "    if _v(_g.name).startswith(_p):",
        "        _gone['reelGroups'].append([_v(_g.name), flame.delete(_g, confirm=False)])",
        "for _l in list(flame.projects.current_project.current_workspace.libraries):",
        "    if _v(_l.name).startswith(_p):",
        "        _gone['libraries'].append([_v(_l.name), flame.delete(_l, confirm=False)])",
        "_result = _gone",
    ]
)


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    observe(ctx, "z_ops_this_run_used", used=used)
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    left = await tx_val(ctx, fill(CLEANUP_CODE, P=f"P2D_{ctx.ts}"), "p23d_cleanup", 120.0)
    still = await tx_batch_names(ctx)
    ctx.results["cleanup"] = {"deleted": left, "stillThere": still}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted",
        isinstance(left, dict)
        and all(ok is True for rows in left.values() for _n, ok in rows)
        and still == [],
        deleted=left,
        stillThere=still,
    )
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "libWt" in ctx.ids:
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("f", stage_f),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p23d_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_p23d_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_3d_desktop_e2e_{ts}.json",
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
