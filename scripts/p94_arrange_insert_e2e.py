#!/usr/bin/env python3
"""Real-Flame end-to-end: putting things in the middle of a Desktop reel -- `desktop.move` into
the reel the clip is already in and `desktop.arrange` with `position`, with their journal groups
and `journal.revert` (run with the `.venv` Python; `scripts/run_e2e.sh p94_arrange_insert` starts
Flame).

Checks: `desktop.move` of clips into the reel they are already in (they go to the end in the order
given, with new uids), `desktop.arrange` with `position` (the items are put before that entry and
the entries that were behind it are sent to the end by one more move, under new uids; the reel
lists the order the op reports), the revert of both (the reel lists its old order again; what was
behind the position has new uids), the refusals (a position past the end, a negative one, a move
item that takes an entry out of the destination with position) with nothing written or journaled,
and the seconds that one `desktop.move` of 50 clips takes.

Same shape and helpers as scripts/p2_3d_desktop_e2e.py: a real MCP stdio client drives
`python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport` is scaffolding only
-- it prepares the material (a library with a reel and two imported clips) and READS EVERY STATE
BACK INDEPENDENTLY of the ops under test (a reel's clips with their names, uids and wiretap ids,
`find_by_uid`). At the end it deletes what this run made (names starting with `P94_<ts>` only).
The journal directory is this run's own (`p2/journal_p94_e2e_<ts>/`). No test hook is set for any
server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by what
was measured earlier. OBSERVED steps (`kind: "observation"`, always ok) record what Flame did
where the number is the point: the seconds of one `media_panel.move` of 50 clips (the limit of
`desktop.move`), alone and as the last step of an arrange.

Stages
  a  move inside one reel: a reel with a1..a4; `desktop.move` [a2] into it (order a1 a3 a4 a2, a2
     has a new uid and its old uid finds nothing); `desktop.move` [a4, a1] (order a3 a2 a4 a1);
     the revert of that move leaves the names in the same order with new uids.
  b  arrange with position: a reel with b1..b4 and a source reel with s1, s2; arrange
     [copy s1, move s2] at position 2 gives b1 b2 s1 s2 b3 b4, the op reads the same order back,
     b1 and b2 keep their uids, b3 and b4 have new ones, three ids in one journal group, the
     last one the shift move; position 0 and position = 4 (the end) on a second destination.
  c  the revert of stage b: b1..b4 in the old order (b3 and b4 under new uids), the moved clip back
     in the source reel, the copy deleted, a second revert refused.
  d  refusals with nothing journaled and the reel as it was: position past the end, a negative
     position, a move item whose source is an entry of the destination with position.
  e  the seconds of one move of 50 clips inside a reel (observed), then an arrange with position 0
     in front of 50 clips (the shift of 50 entries; judged: 51 entries, the new one first).
  g  the journal: every group of this run is landed / reverted / conflict, none unknown; no intent
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
MOVE_LIMIT: Final[int] = 50
"""The number of sources one `desktop.move` takes (and the most entries that may be behind a
`position`)."""
GOOD_STATES: Final[frozenset[str]] = frozenset({"landed", "reverted", "conflict"})


def name_of(ctx: Ctx, tail: str) -> str:
    return f"P94_{ctx.ts}_{tail}"


def groups_of(ctx: Ctx) -> list[dict[str, str]]:
    return ctx.ids.setdefault("writeGroups", [])


async def wdo(session: Any, ctx: Ctx, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """`do` that remembers the journal group of a call that wrote (ok or refused inside Flame)."""
    env = await do(session, ctx, op, args)
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        groups_of(ctx).append({"op": op, "group": group})
    return env


async def revert(session: Any, ctx: Ctx, env: dict[str, Any]) -> dict[str, Any]:
    group = (env.get("journal") or {}).get("group")
    out = await wdo(session, ctx, "journal.revert", {"group": group})
    kept = {g["group"] for g in groups_of(ctx)}
    for sub in (out.get("journal") or {}).get("groups") or []:
        if isinstance(sub, str) and sub not in kept:
            groups_of(ctx).append({"op": "journal.revert", "group": sub})
    return out


def fill(code: str, **values: Any) -> str:
    """`code` with each `@NAME@` replaced by repr(value)."""
    for key, value in values.items():
        code = code.replace(f"@{key}@", repr(value))
    return code


async def tx_val(ctx: Ctx, code: str, label: str, timeout_s: float = 60.0) -> Any:
    br = await tx(ctx, code, label=label, timeout_s=timeout_s)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


READ_REEL_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_wiretap_node_id(@WT@)",
        "_result = None if _o is None else [",
        "    [_v(c.name), _v(c.uid), c.get_wiretap_node_id()] for c in _o.clips",
        "]",
    ]
)

FIND_CODE: Final[str] = "\n".join(
    [
        "_o = flame.find_by_uid(@UID@)",
        "_result = None if _o is None else type(_o).__name__",
    ]
)


REEL_GROUP_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_result = [g.get_wiretap_node_id() for g in _d.reel_groups if _v(g.name) == @NAME@]",
    ]
)


async def tx_reel(ctx: Ctx, reel: Any) -> list[list[str]]:
    """[[name, uid, wiretap id], ...] of the clips of a Desktop reel, in Flame's order."""
    rows = await tx_val(ctx, fill(READ_REEL_CODE, WT=reel["id"]), "p94_reel")
    return rows if isinstance(rows, list) else []


async def tx_find(ctx: Ctx, uid: str) -> Any:
    return await tx_val(ctx, fill(FIND_CODE, UID=uid), "p94_find")


def names(rows: list[list[str]]) -> list[str]:
    return [r[0] for r in rows]


def uids(rows: list[list[str]]) -> list[str]:
    return [r[1] for r in rows]


def uid_of(rows: list[list[str]], name: str) -> str | None:
    return next((r[1] for r in rows if r[0] == name), None)


def sel_of(rows: list[list[str]], name: str) -> dict[str, Any]:
    wiretap = next((r[2] for r in rows if r[0] == name), "")
    return {"kind": "wiretap", "id": wiretap}


async def make_reel(session: Any, ctx: Ctx, tail: str) -> dict[str, Any]:
    """The selector of a new Desktop reel in this run's reel group."""
    env = await wdo(
        session,
        ctx,
        "desktop.create",
        {"kind": "reel", "name": name_of(ctx, tail), "parent": ctx.ids["rgSelector"]},
    )
    selector = _result_of(env).get("selector")
    return selector if isinstance(selector, dict) else {}


async def build_reel(
    session: Any, ctx: Ctx, tail: str, clip_names: list[str]
) -> tuple[dict[str, Any], list[list[str]]]:
    """A new reel that holds one copy of a library clip per name, in this order, renamed."""
    reel = await make_reel(session, ctx, tail)
    lib = ctx.ids["clipSelectors"]
    items = [{"action": "copy", "source": lib[i % 2]} for i in range(len(clip_names))]
    built = await wdo(session, ctx, "desktop.arrange", {"destination": reel, "items": items})
    made = [(a.get("made") or {}).get("selector") for a in _result_of(built).get("applied") or []]
    for selector, new_name in zip(made, clip_names, strict=False):
        await wdo(session, ctx, "desktop.rename", {"target": selector, "name": new_name})
    return reel, await tx_reel(ctx, reel)


async def prepare(ctx: Ctx) -> None:
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
                "_result = {'libWt': lib.get_wiretap_node_id(), 'clips': _clips}",
            ]
        ),
        label="p94_prepare_library",
        timeout_s=120.0,
    )
    rec(ctx, "prepare_library_with_2_clips", made.ok, resp=_br_dict(made))
    if made.ok and isinstance(made.result, dict):
        ctx.ids.update(libName=lib_name, **made.result)
        ctx.ids["clipSelectors"] = [{"kind": "wiretap", "id": c[1]} for c in made.result["clips"]]


async def need_reel_group(session: Any, ctx: Ctx) -> bool:
    if "rgSelector" in ctx.ids:
        return True
    name = name_of(ctx, "RG")
    env = await wdo(session, ctx, "desktop.create", {"kind": "reel_group", "name": name})
    found = await tx_val(ctx, fill(REEL_GROUP_CODE, NAME=name), "p94_reel_group")
    wiretap = found[0] if isinstance(found, list) and len(found) == 1 else None
    rec(
        ctx,
        "a0_a_reel_group_for_this_run",
        env.get("ok") is True and bool(wiretap),
        error=env.get("error"),
        transport=found,
    )
    if env.get("ok") is True and wiretap:
        ctx.ids["rgSelector"] = {"kind": "wiretap", "id": wiretap}
    return "rgSelector" in ctx.ids


async def stage_a(ctx: Ctx) -> None:
    async with server_session(ctx, "i-a") as session:
        if not await need_reel_group(session, ctx):
            return
        reel, rows = await build_reel(session, ctx, "A", ["a1", "a2", "a3", "a4"])
        rec(ctx, "a1_a_reel_with_a1_to_a4", names(rows) == ["a1", "a2", "a3", "a4"], rows=rows)
        if names(rows) != ["a1", "a2", "a3", "a4"]:
            return
        old_a2 = uid_of(rows, "a2")
        m1 = await wdo(
            session,
            ctx,
            "desktop.move",
            {"sources": [sel_of(rows, "a2")], "destination": reel},
        )
        after1 = await tx_reel(ctx, reel)
        ok, parts = judged(
            op_ok=m1.get("ok") is True,
            a2_is_last=names(after1) == ["a1", "a3", "a4", "a2"],
            a2_has_a_new_uid=uid_of(after1, "a2") not in (None, old_a2),
            the_old_uid_finds_nothing=await tx_find(ctx, str(old_a2)) is None,
            others_keep_their_uids=[uid_of(after1, n) for n in ("a1", "a3", "a4")]
            == [uid_of(rows, n) for n in ("a1", "a3", "a4")],
        )
        rec(
            ctx,
            "a2_a_move_into_its_own_reel_puts_the_clip_at_the_end_with_a_new_uid",
            ok,
            clauses=parts,
            error=m1.get("error"),
            errorCode=m1.get("errorCode"),
            after=after1,
        )
        m2 = await wdo(
            session,
            ctx,
            "desktop.move",
            {"sources": [sel_of(after1, "a4"), sel_of(after1, "a1")], "destination": reel},
        )
        after2 = await tx_reel(ctx, reel)
        ok, parts = judged(
            op_ok=m2.get("ok") is True,
            order_is_the_one_given=names(after2) == ["a3", "a2", "a4", "a1"],
            op_reports_both=[m["from"]["name"] for m in _result_of(m2).get("moved") or []]
            == ["a4", "a1"],
            no_warning=not m2.get("degraded"),
        )
        rec(
            ctx,
            "a3_a_move_of_two_into_the_reel_puts_them_at_the_end_in_the_order_given",
            ok,
            clauses=parts,
            error=m2.get("error"),
            degraded=m2.get("degraded"),
            after=after2,
        )
        back = await revert(session, ctx, m2)
        after3 = await tx_reel(ctx, reel)
        ok, parts = judged(
            revert_ok=back.get("ok") is True,
            same_names_in_the_same_order=names(after3) == names(after2),
            both_have_new_uids=uid_of(after3, "a4") != uid_of(after2, "a4")
            and uid_of(after3, "a1") != uid_of(after2, "a1"),
        )
        rec(
            ctx,
            "a4_the_revert_of_a_move_inside_a_reel_moves_it_to_the_end_again",
            ok,
            clauses=parts,
            error=back.get("error"),
            after=after3,
        )


async def stage_b(ctx: Ctx) -> None:
    if "rgSelector" not in ctx.ids:
        rec(ctx, "b_skipped_no_reel_group_from_stage_a", False)
        return
    async with server_session(ctx, "i-b") as session:
        dest, dest_rows = await build_reel(session, ctx, "B", ["b1", "b2", "b3", "b4"])
        src, src_rows = await build_reel(session, ctx, "BS", ["s1", "s2"])
        ctx.ids["bDest"], ctx.ids["bSrc"] = dest, src
        ctx.ids["bRowsBefore"], ctx.ids["bSrcBefore"] = dest_rows, src_rows
        rec(
            ctx,
            "b0_a_destination_with_b1_to_b4_and_a_source_with_s1_s2",
            names(dest_rows) == ["b1", "b2", "b3", "b4"] and names(src_rows) == ["s1", "s2"],
            dest=dest_rows,
            src=src_rows,
        )
        if names(dest_rows) != ["b1", "b2", "b3", "b4"] or names(src_rows) != ["s1", "s2"]:
            return
        items = [
            {"action": "copy", "source": sel_of(src_rows, "s1")},
            {"action": "move", "source": sel_of(src_rows, "s2")},
        ]
        a1 = await wdo(
            session,
            ctx,
            "desktop.arrange",
            {"destination": dest, "items": items, "position": 2},
        )
        ctx.ids["bArrange"] = a1
        r1 = _result_of(a1)
        dest_after = await tx_reel(ctx, dest)
        src_after = await tx_reel(ctx, src)
        shift = (r1.get("applied") or [{}])[-1]
        ok, parts = judged(
            op_ok=a1.get("ok") is True,
            transport_lists_the_order=names(dest_after) == ["b1", "b2", "s1", "s2", "b3", "b4"],
            op_read_the_same_order_back=r1.get("orderMatches") is True
            and uids(dest_after) == [row.get("uid") for row in r1.get("order") or []],
            b1_b2_keep_their_uids=uids(dest_after)[:2] == uids(dest_rows)[:2],
            b3_b4_have_new_uids=not set(uids(dest_after)[4:]) & set(uids(dest_rows)),
            three_applied_the_last_is_the_shift=[a.get("index") for a in r1.get("applied") or []]
            == [0, 1, 2]
            and shift.get("shift") is True
            and [s["source"]["name"] for s in shift.get("shifted") or []] == ["b3", "b4"],
            three_ids_in_one_group=len((a1.get("journal") or {}).get("ids") or []) == 3,
            source_keeps_s1_and_loses_s2=names(src_after) == ["s1"],
        )
        rec(
            ctx,
            "b1_arrange_at_position_2_puts_the_items_before_b3",
            ok,
            clauses=parts,
            error=a1.get("error"),
            errorCode=a1.get("errorCode"),
            dest=dest_after,
            src=src_after,
        )
        ctx.ids["bDestAfter"], ctx.ids["bSrcAfter"] = dest_after, src_after

        first, first_rows = await build_reel(session, ctx, "BF", ["f1", "f2"])
        lib = ctx.ids["clipSelectors"]
        p0 = await wdo(
            session,
            ctx,
            "desktop.arrange",
            {"destination": first, "items": [{"action": "copy", "source": lib[0]}], "position": 0},
        )
        end, end_rows = await build_reel(session, ctx, "BE", ["e1", "e2"])
        p_end = await wdo(
            session,
            ctx,
            "desktop.arrange",
            {"destination": end, "items": [{"action": "copy", "source": lib[0]}], "position": 2},
        )
        first_after = await tx_reel(ctx, first)
        end_after = await tx_reel(ctx, end)
        ok, parts = judged(
            position_0_ok=p0.get("ok") is True,
            position_0_puts_it_first=names(first_after)[1:] == ["f1", "f2"]
            and len(first_after) == 3,
            position_0_has_two_steps=len(_result_of(p0).get("applied") or []) == 2,
            position_end_ok=p_end.get("ok") is True,
            position_end_puts_it_last=names(end_after)[:2] == ["e1", "e2"] and len(end_after) == 3,
            position_end_has_one_step=len(_result_of(p_end).get("applied") or []) == 1,
            position_end_keeps_the_uids=uids(end_after)[:2] == uids(end_rows),
        )
        rec(
            ctx,
            "b2_position_0_and_position_at_the_end",
            ok,
            clauses=parts,
            first=first_after,
            end=end_after,
            firstBefore=first_rows,
        )


async def stage_c(ctx: Ctx) -> None:
    arranged = ctx.ids.get("bArrange")
    if not isinstance(arranged, dict) or arranged.get("ok") is not True:
        rec(ctx, "c_skipped_no_arrange_from_stage_b", False)
        return
    dest, src = ctx.ids["bDest"], ctx.ids["bSrc"]
    before: list[list[str]] = ctx.ids["bRowsBefore"]
    async with server_session(ctx, "i-c") as session:
        out = await revert(session, ctx, arranged)
        dest_back = await tx_reel(ctx, dest)
        src_back = await tx_reel(ctx, src)
        ok, parts = judged(
            revert_ok=out.get("ok") is True,
            three_steps_newest_first=[s.get("index") for s in _result_of(out).get("steps") or []]
            == [2, 1, 0],
            old_order=names(dest_back) == ["b1", "b2", "b3", "b4"],
            b1_b2_keep_their_uids=uids(dest_back)[:2] == uids(before)[:2],
            b3_b4_have_other_uids_than_before_the_arrange=not set(uids(dest_back)[2:])
            & set(uids(before)[2:]),
            the_copy_is_deleted_and_the_moved_clip_is_back=sorted(names(src_back)) == ["s1", "s2"],
        )
        rec(
            ctx,
            "c1_revert_of_the_arrange_gives_the_old_order_back",
            ok,
            clauses=parts,
            error=out.get("error"),
            errorCode=out.get("errorCode"),
            dest=dest_back,
            src=src_back,
        )
        again = await revert(session, ctx, arranged)
        rec(
            ctx,
            "c2_a_second_revert_is_refused",
            again.get("ok") is False and again.get("errorCode") == "PRECONDITION_UNMET",
            error=again.get("error"),
        )


async def stage_d(ctx: Ctx) -> None:
    if "bDest" not in ctx.ids:
        rec(ctx, "d_skipped_no_reel_from_stage_b", False)
        return
    dest = ctx.ids["bDest"]
    lib = ctx.ids["clipSelectors"]
    async with server_session(ctx, "i-d") as session:
        rows = await tx_reel(ctx, dest)
        written = len(groups_of(ctx))
        cases = {
            "past_the_end": {"position": len(rows) + 1},
            "negative": {"position": -1},
            "not_an_integer": {"position": 1.5},
        }
        for label, extra in cases.items():
            env = await wdo(
                session,
                ctx,
                "desktop.arrange",
                {"destination": dest, "items": [{"action": "copy", "source": lib[0]}], **extra},
            )
            now = await tx_reel(ctx, dest)
            ok, parts = judged(
                refused=env.get("ok") is False and env.get("errorCode") == "INVALID_ARGS",
                names_the_position="position" in str(env.get("error")),
                nothing_journaled=env.get("journal") is None,
                reel_as_it_was=now == rows,
            )
            rec(
                ctx,
                f"d1_position_{label}_is_refused_and_nothing_is_written",
                ok,
                clauses=parts,
                error=env.get("error"),
            )
        move_env = await wdo(
            session,
            ctx,
            "desktop.arrange",
            {
                "destination": dest,
                "position": 0,
                "items": [{"action": "move", "source": sel_of(rows, "b2")}],
            },
        )
        now = await tx_reel(ctx, dest)
        ok, parts = judged(
            refused=move_env.get("ok") is False and move_env.get("errorCode") == "INVALID_ARGS",
            names_the_item=" items[0]" in f" {move_env.get('error')}",
            nothing_journaled=move_env.get("journal") is None,
            reel_as_it_was=now == rows,
            no_group_added=len(groups_of(ctx)) == written,
        )
        rec(
            ctx,
            "d2_a_move_of_an_entry_of_the_destination_with_position_is_refused",
            ok,
            clauses=parts,
            error=move_env.get("error"),
        )


async def stage_e(ctx: Ctx) -> None:
    if "rgSelector" not in ctx.ids:
        rec(ctx, "e_skipped_no_reel_group_from_stage_a", False)
        return
    lib = ctx.ids["clipSelectors"]
    async with server_session(ctx, "i-e") as session:
        reel = await make_reel(session, ctx, "E")
        fill_items = [{"action": "copy", "source": lib[i % 2]} for i in range(MOVE_LIMIT)]
        built = await wdo(
            session, ctx, "desktop.arrange", {"destination": reel, "items": fill_items}
        )
        rows = await tx_reel(ctx, reel)
        rec(
            ctx,
            "e0_a_reel_with_50_clips",
            built.get("ok") is True and len(rows) == MOVE_LIMIT,
            count=len(rows),
            error=built.get("error"),
        )
        if len(rows) != MOVE_LIMIT:
            return
        started = time.monotonic()
        moved = await wdo(
            session,
            ctx,
            "desktop.move",
            {"sources": [{"kind": "wiretap", "id": r[2]} for r in rows], "destination": reel},
        )
        wall = round(time.monotonic() - started, 3)
        after = await tx_reel(ctx, reel)
        ok, parts = judged(
            op_ok=moved.get("ok") is True,
            same_order_of_names=names(after) == names(rows),
            every_clip_has_a_new_uid=not set(uids(after)) & set(uids(rows)),
        )
        rec(
            ctx,
            "e1_one_move_of_50_clips_inside_the_reel",
            ok,
            clauses=parts,
            error=moved.get("error"),
        )
        observe(
            ctx,
            "e1_seconds_of_one_desktop_move_of_50_clips_inside_a_reel",
            wallSeconds=wall,
            durationMs=moved.get("durationMs"),
            sources=MOVE_LIMIT,
        )
        started = time.monotonic()
        arranged = await wdo(
            session,
            ctx,
            "desktop.arrange",
            {
                "destination": reel,
                "position": 0,
                "items": [{"action": "copy", "source": lib[1]}],
            },
        )
        wall = round(time.monotonic() - started, 3)
        final = await tx_reel(ctx, reel)
        result = _result_of(arranged)
        ok, parts = judged(
            op_ok=arranged.get("ok") is True,
            fifty_one_entries=len(final) == MOVE_LIMIT + 1,
            the_new_one_is_first=names(final) == [CLIP_NAMES[1], *names(after)],
            op_read_the_same_order_back=result.get("orderMatches") is True,
            shifted_50=result.get("shiftedBehind") == MOVE_LIMIT,
        )
        rec(
            ctx,
            "e2_arrange_at_position_0_in_front_of_50_clips",
            ok,
            clauses=parts,
            error=arranged.get("error"),
            errorCode=arranged.get("errorCode"),
        )
        observe(
            ctx,
            "e2_seconds_of_an_arrange_with_a_shift_of_50_entries",
            wallSeconds=wall,
            durationMs=arranged.get("durationMs"),
            shifted=result.get("shiftedBehind"),
        )
        too_many = await wdo(
            session,
            ctx,
            "desktop.arrange",
            {
                "destination": reel,
                "position": 0,
                "items": [{"action": "copy", "source": lib[0]}],
            },
        )
        rec(
            ctx,
            "e3_51_entries_behind_the_position_are_refused_with_nothing_written",
            too_many.get("ok") is False
            and too_many.get("errorCode") == "INVALID_ARGS"
            and too_many.get("journal") is None
            and len(await tx_reel(ctx, reel)) == MOVE_LIMIT + 1,
            error=too_many.get("error"),
        )


async def stage_g(ctx: Ctx) -> None:
    written = groups_of(ctx)
    async with server_session(ctx, "i-g") as session:
        listing = await do(session, ctx, "journal.list", {"limit": 200})
        items = _result_of(listing).get("items", [])
        states = {i.get("group"): i.get("state") for i in items}
        own = journal_states(ctx)
        rec(
            ctx,
            "g_every_group_of_this_run_is_landed_reverted_or_conflict",
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
            "g_no_intent_is_left_undecided_and_writes_are_not_blocked",
            info.get("unresolvedIntents") == 0 and info.get("writesBlocked") is False,
            journal={k: info.get(k) for k in ("unresolvedIntents", "writesBlocked")},
        )


CLEANUP_CODE: Final[str] = "\n".join(
    [
        "_p = @P@",
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_gone = {'reelGroups': [], 'libraries': []}",
        "for _g in list(_d.reel_groups):",
        "    if _v(_g.name).startswith(_p):",
        "        _gone['reelGroups'].append([_v(_g.name), flame.delete(_g, confirm=False)])",
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
        "_ws = flame.projects.current_project.current_workspace",
        "_result = [_v(g.name) for g in _d.reel_groups if _v(g.name).startswith(_p)] + [",
        "    _v(l.name) for l in _ws.libraries if _v(l.name).startswith(_p)",
        "]",
    ]
)


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    observe(ctx, "z_ops_this_run_used", used=used)
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"P94_{ctx.ts}"
    left = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p94_cleanup", 120.0)
    still = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p94_left", 60.0)
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
                ("e", stage_e),
                ("g", stage_g),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p94_cleanup_exit")
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
    journal_dir = OUTPUT_DIR / f"journal_p94_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p94_arrange_insert_e2e_{ts}.json",
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
