#!/usr/bin/env python3
"""Real-Flame end-to-end: the checkpointed-write mechanism and the `batch` ops built on it
(run with the `.venv` Python; `scripts/run_e2e.sh p2_3a_batch` starts Flame).

Checks: The checkpointed-write mechanism and the `batch` ops built on it (`batch.info`,
`batch.create_reel`, `batch.create_shelf_reel`, `batch.add_clips`, `batch.organize`,
`batch.iterate`, `batch.save_setup`, `batch.load_setup`), and what `journal.revert` brings back
after each.

Same shape and helpers as scripts/p2_2c_checkpoint_eval_e2e.py (imported from p2_2a / p1): a real
MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only (create the groups and the media, and READ EVERY STATE
BACK INDEPENDENTLY of the ops under test -- nodes, wiring, positions, reels, reel clips, shelf
reels, iterations). Everything happens in the probe project `flame_mcp_probe`, in Batch groups that
are NOT current (a decoy group is created last). The journal directory is this run's own
(`p2/journal_p23a_e2e_<ts>/`, never the production one). No test hook is set for any server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by what
was measured earlier. OBSERVED steps (`kind: "observation"`, always ok) record what Flame did where
nothing was known yet -- above all what `journal.revert` brings back and what it does not. They
are collected in results["observations"] and printed as a table at the end; the undo declarations
of the ops under test (batch.py: UNDO_UNCONFIRMED) are decided from them, not from this script.

Stages
  a  batch.info on a group that is not current: reels, shelf reels, iterations and the current
     iteration number equal what a transport read says (after transport made an extra reel, an
     extra shelf reel, a clip in the shelf reel and two iterations); isCurrent is false for the
     group and true for the decoy.
  b  create_reel / create_shelf_reel / add_clips (schematic reel, shelf reel) / organize, one
     group each: op -> the change, read independently -> journal.revert -> what is back and
     what is not (nodes, wiring, positions, reels, reel clips, shelf reels, shelf clips,
     iterations). JUDGED: the change itself; that add_clips' Clip nodes are gone after the revert
     and the fingerprint equals the one before; that the revert answers ok. OBSERVED: everything
     else, plus each fingerprint with all sections (reels, shelf_reels, iterations) before / after
     the op / after the revert, and add_clips with a LIBRARY clip and a sequence.
  c  iterate: a new iteration; another; index of an existing one without overwrite -> INVALID_ARGS,
     nothing checkpointed or journaled, iterations unchanged; with overwrite -> succeeds and the
     iteration's uid changes; journal.revert of the overwrite and of the second iterate ->
     OBSERVED what the iterations look like afterwards.
  d  save_setup: a dot-less name gets `.batch`, the reported path is the file that
     exists; an existing target is refused, overwrite=true replaces it; a dotted name, a relative
     path and a missing directory; nothing is journaled.
  e  eval.run (destructive, with FLAME_MCP_ENABLE_EVAL=1) and batch.load_setup still work on the
     shared mechanism and revert (minimal checks).
  z  cross-cutting checks over every envelope; nothing left undecided

Cleanup: no Batch group is deleted (flame.delete on a PyBatch is not used); the library and the
desktop reel this run made are kept (their clips are referenced by the groups' copies); then
flame.exit().
"""

from __future__ import annotations

import argparse
import os
import time
import traceback
from pathlib import Path
from typing import Any, Final

import anyio
import e2e_guard
from p1_mcp_e2e import (
    MEDIA_DIR,
    RUN_DIR_DEFAULT,
    _br_dict,
    _result_of,
    _tx,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (
    Ctx,
    do,
    guarded,
    journal_index,
    rec,
    save,
    save_stage,
    server_session,
    state_of,
    tx,
    version_info,
)

from flame_mcp.journal import Journal

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
ENABLE_EVAL: Final[str] = "FLAME_MCP_ENABLE_EVAL"
GROUPS: Final[tuple[str, ...]] = (
    "A",
    "B1",
    "B2",
    "B3",
    "B4",
    "B5",
    "B6",
    "C",
    "D",
    "E",
)
ALL_SECTIONS: Final[list[str]] = ["reels", "shelf_reels", "iterations"]
EVAL_MAKE_PAIR: Final[str] = "\n".join(
    [
        "a = target.create_node('Mux')",
        "b = target.create_node('Mux')",
        "target.connect_nodes(a, 'Default', b, 'Default')",
        "_result = [_v(a.name), _v(b.name)]",
    ]
)

Results = dict[str, Any]
State = dict[str, Any]


# Transport scaffolding (never through the op under test)
def _locate(uid: str) -> str:
    return "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            f"_bg = [g for g in _ws.desktop.batch_groups if _v(g.uid) == {uid!r}][0]",
        ]
    )


WORLD_CODE: Final[str] = "\n".join(
    [
        "def _names(objs):",
        "    return [_v(o.name) for o in objs]",
        "def _reel(r):",
        "    try:",
        "        _t = _v(r.type)",
        "    except BaseException:",
        "        _t = None",
        "    return {'name': _v(r.name), 'type': _t, 'clips': _names(r.clips),",
        "            'sequences': _names(r.sequences)}",
        "_nodes = []",
        "for _n in _bg.nodes:",
        "    _s = _n.sockets",
        "    _nodes.append({",
        "        'name': _v(_n.name), 'pyType': type(_n).__name__,",
        "        'x': _v(_n.pos_x), 'y': _v(_n.pos_y),",
        "        'in': {k: sorted(v) for k, v in _s['input'].items() if v},",
        "        'inner': sorted(_v(_c.name) for _c in _n.nodes)"
        " if type(_n).__name__ == 'PyActionNode' else None,",
        "    })",
        "_its = []",
        "for _it in _bg.batch_iterations:",
        "    _its.append({'name': _v(_it.name), 'uid': _v(_it.uid),",
        "                 'number': _v(_it.iteration_number)})",
        "_its.sort(key=lambda e: e['number'])",
        "try:",
        "    _cur_it = _v(_bg.current_iteration_number)",
        "except BaseException:",
        "    _cur_it = None",
        "_result = {",
        "    'nodes': sorted(_nodes, key=lambda e: e['name']),",
        "    'reels': [_reel(r) for r in _bg.reels],",
        "    'shelfReels': [_reel(r) for r in _bg.shelf_reels],",
        "    'iterations': _its,",
        "    'currentIteration': _cur_it,",
        "}",
    ]
)


async def tx_world(ctx: Ctx, uid: str) -> State:
    """Everything about the group the ops under test could have touched, read without any op or
    prelude of ours."""
    br = await tx(ctx, _locate(uid) + "\n" + WORLD_CODE, label="p23a_world")
    if br.ok and isinstance(br.result, dict):
        return dict(br.result)
    return {"error": f"transport {br.error_code}: {br.error}"}


def node_names(world: State) -> list[str]:
    return [n["name"] for n in world.get("nodes", [])]


def wiring(world: State) -> dict[str, Any]:
    return {n["name"]: n["in"] for n in world.get("nodes", []) if n["in"]}


def positions(world: State) -> dict[str, Any]:
    return {n["name"]: [n["x"], n["y"]] for n in world.get("nodes", [])}


def reel_map(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        r["name"]: {"type": r["type"], "clips": r["clips"], "sequences": r["sequences"]}
        for r in rows
    }


def diff_world(a: State, b: State) -> dict[str, Any]:
    """What differs between two reads, section by section (empty section = same)."""
    out: dict[str, Any] = {}
    added = sorted(set(node_names(b)) - set(node_names(a)))
    removed = sorted(set(node_names(a)) - set(node_names(b)))
    if added or removed:
        out["nodes"] = {"added": added, "removed": removed}
    if wiring(a) != wiring(b):
        out["wiring"] = {"before": wiring(a), "after": wiring(b)}
    moved = {
        k: {"before": positions(a)[k], "after": positions(b)[k]}
        for k in positions(a)
        if k in positions(b) and positions(a)[k] != positions(b)[k]
    }
    if moved:
        out["positions"] = moved
    for key, label in (("reels", "reels"), ("shelfReels", "shelfReels")):
        ra, rb = reel_map(a.get(key, [])), reel_map(b.get(key, []))
        section: dict[str, Any] = {}
        if set(ra) != set(rb):
            section["namesAdded"] = sorted(set(rb) - set(ra))
            section["namesRemoved"] = sorted(set(ra) - set(rb))
        changed = {k: {"before": ra[k], "after": rb[k]} for k in ra if k in rb and ra[k] != rb[k]}
        if changed:
            section["reelsChanged"] = changed
        if section:
            out[label] = section
    ia = {i["number"]: i["uid"] for i in a.get("iterations", [])}
    ib = {i["number"]: i["uid"] for i in b.get("iterations", [])}
    if ia != ib:
        out["iterations"] = {
            "numbersAdded": sorted(set(ib) - set(ia)),
            "numbersRemoved": sorted(set(ia) - set(ib)),
            "uidChanged": sorted(n for n in ia if n in ib and ia[n] != ib[n]),
        }
    if a.get("currentIteration") != b.get("currentIteration"):
        out["currentIteration"] = {
            "before": a.get("currentIteration"),
            "after": b.get("currentIteration"),
        }
    return out


async def tx_add_node(ctx: Ctx, uid: str, node_type: str) -> Any:
    code = _locate(uid) + f"\n_result = _v(_bg.create_node({node_type!r}).name)"
    br = await tx(ctx, code, label="p23a_add_node")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_scaffold(ctx: Ctx, uid: str, body: str, label: str) -> Any:
    br = await tx(ctx, _locate(uid) + "\n" + body, label=label, timeout_s=40.0)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_file_exists(ctx: Ctx, path: str) -> Any:
    br = await tx(ctx, f"import os\n_result = os.path.isfile({path!r})", label="p23a_exists")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_dir_exists(ctx: Ctx, path: str) -> Any:
    br = await tx(ctx, f"import os\n_result = os.path.isdir({path!r})", label="p23a_isdir")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_mtime(ctx: Ctx, path: str) -> Any:
    code = f"import os\n_result = os.stat({path!r}).st_mtime_ns"
    br = await tx(ctx, code, label="p23a_mtime")
    return br.result if br.ok else None


async def tx_glob_batch(ctx: Ctx, directory: str) -> list[str]:
    code = f"import glob, os\n_result = sorted(glob.glob(os.path.join({directory!r}, '*.batch')))"
    br = await tx(ctx, code, label="p23a_glob")
    return list(br.result) if br.ok and isinstance(br.result, list) else []


async def tx_makedirs(ctx: Ctx, path: str) -> Any:
    br = await tx(
        ctx, f"import os\nos.makedirs({path!r}, exist_ok=True)\n_result = True", label="p23a_mk"
    )
    return br.ok


def target_of(ctx: Ctx, group: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": ctx.ids["groups"][group]["uid"]}


def uid_of(ctx: Ctx, group: str) -> str:
    return str(ctx.ids["groups"][group]["uid"])


def wt_selector(wtid: str) -> dict[str, Any]:
    return {"kind": "wiretap", "id": wtid}


def observe(ctx: Ctx, name: str, **data: Any) -> None:
    """A step that records what Flame did and decides nothing. A `ok` in `data` is the answer of
    the op under observation, not this step's verdict (`rec` takes `ok` itself): it is recorded
    as `opOk`. `kind` is reserved for the step and refused."""
    if "kind" in data:
        raise ValueError("observe(): `kind` is reserved (the step's kind is 'observation')")
    if "ok" in data:
        data["opOk"] = data.pop("ok")
    ctx.results.setdefault("observations", []).append({"name": name, "stage": ctx.stage, **data})
    rec(ctx, name, True, kind="observation", **data)


def group_records(ctx: Ctx, group: str) -> list[dict[str, Any]]:
    return [r for r in Journal(ctx.journal_dir).read_all().records if r.get("group") == group]


def journal_group_count(ctx: Ctx) -> int:
    return len(journal_index(ctx).groups)


def has_no_bridge_loss(env: dict[str, Any]) -> bool:
    return env.get("errorCode") not in ("BRIDGE_LOST", "BRIDGE_UNAVAILABLE", "TIMEOUT")


async def prepare(ctx: Ctx) -> None:
    lines = [
        "desktop = flame.projects.current_project.current_workspace.desktop",
        "groups = {}",
    ]
    for group in GROUPS:
        lines += [
            f"bg = desktop.create_batch_group('P23A_{group}_{ctx.ts}', "
            "nb_reels=1, nb_shelf_reels=1)",
            "a = bg.create_node('Blur')",
            "b = bg.create_node('Blur')",
            "c = bg.create_node('Comp')",
            "bg.connect_nodes(a, 'Default', c, 'Default')",
            f"groups[{group!r}] = {{'uid': _v(bg.uid), 'name': _v(bg.name)}}",
        ]
    lines += [
        # created last: it becomes the current group, so none of the test groups is current
        f"decoy = desktop.create_batch_group('P23A_DECOY_{ctx.ts}', nb_reels=1, nb_shelf_reels=1)",
        "_result = {'groups': groups, 'decoyUid': _v(decoy.uid)}",
    ]
    br = await tx(ctx, "\n".join(lines), label="p23a_prepare_groups", timeout_s=90.0)
    rec(ctx, "prepare_groups_and_decoy", br.ok, resp=_br_dict(br))
    if br.ok and isinstance(br.result, dict):
        ctx.ids.update(groups=br.result["groups"], decoyUid=br.result["decoyUid"])
        code = (
            "_ws = flame.projects.current_project.current_workspace\n"
            "_cur = _v(_ws.desktop.current_batch_group)\n"
            "_result = _v(_cur.uid) if _cur is not None else None"
        )
        cur = await tx(ctx, code, label="p23a_current")
        others = {v["uid"] for v in br.result["groups"].values()}
        rec(
            ctx,
            "prepare_no_test_group_is_current",
            cur.ok and cur.result == br.result["decoyUid"] and cur.result not in others,
            current=cur.result,
            decoy=br.result["decoyUid"],
        )
    if "groups" in ctx.ids:
        action = await tx_scaffold(
            ctx,
            uid_of(ctx, "E"),
            "\n".join(
                [
                    "_act = _bg.create_node('Action')",
                    "_inner = _act.create_node('3D Path')",
                    "_result = [_v(_act.name), _v(_inner.name), type(_act).__name__]",
                ]
            ),
            "p23a_prepare_action",
        )
        rec(
            ctx,
            "prepare_group_e_holds_an_action_node_with_an_inner_node",
            isinstance(action, list) and action[2] == "PyActionNode",
            action=action,
        )
    await prepare_media(ctx)


async def prepare_media(ctx: Ctx) -> None:
    lib_name = f"P23A_LIB_{ctx.ts}"
    lib_paths = [str(MEDIA_DIR / f"clip{i}.mov") for i in (1, 2, 3)]
    lib = await tx(
        ctx,
        "\n".join(
            [
                "ws = flame.projects.current_project.current_workspace",
                f"lib = ws.create_library({lib_name!r})",
                "reel = lib.create_reel('R1')",
                "_ids = []",
                f"for _p in {lib_paths!r}:",
                "    _c = flame.import_clips(_p, reel)[0]",
                "    _ids.append([_v(_c.name), _c.get_wiretap_node_id()])",
                "_result = {'lib': lib.get_wiretap_node_id(), 'clips': _ids}",
            ]
        ),
        label="p23a_prepare_library",
        timeout_s=80.0,
    )
    rec(ctx, "prepare_library_clips", lib.ok, resp=_br_dict(lib))
    if lib.ok and isinstance(lib.result, dict):
        ctx.ids["libClips"] = lib.result["clips"]

    desk_reel = f"P23A_DESK_{ctx.ts}"
    seq_name = f"P23A_SEQ_{ctx.ts}"
    desk_paths = [str(MEDIA_DIR / f"clip{i}.mov") for i in (1, 2, 3)]
    desk = await tx(
        ctx,
        "\n".join(
            [
                "desktop = flame.projects.current_project.current_workspace.desktop",
                "groups = list(desktop.reel_groups)",
                f"rg = groups[0] if groups else desktop.create_reel_group('P23A_RG_{ctx.ts}')",
                f"desk_reel = rg.create_reel({desk_reel!r})",
                "_ids = []",
                f"for _p in {desk_paths!r}:",
                "    _c = flame.import_clips(_p, desk_reel)[0]",
                "    _ids.append([_v(_c.name), _c.get_wiretap_node_id()])",
                "_depth = flame.find_by_wiretap_node_id(_ids[0][1]).bit_depth",
                f"seq = desk_reel.create_sequence(name={seq_name!r}, bit_depth=_depth)",
                "_cursor = 1",
                "for _n, _w in _ids[:2]:",
                "    _c = flame.find_by_wiretap_node_id(_w)",
                "    seq.overwrite(_c, flame.PyTime(_cursor), seq.versions[0].tracks[0])",
                "    _cursor += 10",
                "_result = {'clips': _ids, 'seq': seq.get_wiretap_node_id(),",
                "           'deskReel': desk_reel.get_wiretap_node_id()}",
            ]
        ),
        label="p23a_prepare_desktop",
        timeout_s=120.0,
    )
    rec(ctx, "prepare_desktop_clips_and_sequence", desk.ok, resp=_br_dict(desk))
    if desk.ok and isinstance(desk.result, dict):
        ctx.ids["deskClips"] = desk.result["clips"]
        ctx.ids["deskSeq"] = desk.result["seq"]


def desk_clip(ctx: Ctx, index: int) -> dict[str, Any]:
    return wt_selector(str(ctx.ids["deskClips"][index][1]))


async def stage_a(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "A"), target_of(ctx, "A")
    base = await tx_world(ctx, uid)
    async with server_session(ctx, "a-plain") as session:
        info = await do(session, ctx, "batch.info", {"target": target})
        result = _result_of(info)
        rec(
            ctx,
            "a_info_of_a_fresh_group_matches_the_independent_read",
            info.get("ok") is True
            and result.get("uid") == uid
            and result.get("isCurrent") is False
            and result.get("nodeCount") == len(node_names(base))
            and [r["name"] for r in result.get("reels", [])] == [r["name"] for r in base["reels"]]
            and [r["name"] for r in result.get("shelfReels", [])]
            == [r["name"] for r in base["shelfReels"]]
            and result.get("iterations") == [],
            base={k: base[k] for k in ("reels", "shelfReels", "iterations")},
            env=info,
        )
        observe(
            ctx,
            "a_info_scalars_of_a_fresh_group",
            duration=result.get("duration"),
            startFrame=result.get("startFrame"),
            currentIterationNumber=result.get("currentIterationNumber"),
            reelTypes=[r.get("type") for r in result.get("reels", [])],
            shelfReelTypes=[r.get("type") for r in result.get("shelfReels", [])],
            unreadable=result.get("unreadable"),
        )
        decoy = await do(
            session, ctx, "batch.info", {"target": {"kind": "batch", "uid": ctx.ids["decoyUid"]}}
        )
        rec(
            ctx,
            "a_info_says_the_decoy_is_current",
            _result_of(decoy).get("isCurrent") is True,
            env=decoy,
        )

        # transport makes things the op has to report
        made = await tx_scaffold(
            ctx,
            uid,
            "\n".join(
                [
                    "_r = _bg.create_reel('P23A_extra_reel')",
                    "_s = _bg.create_shelf_reel('P23A_extra_shelf')",
                    f"_c = flame.find_by_wiretap_node_id({ctx.ids['deskClips'][0][1]!r})",
                    "flame.media_panel.copy(_c, _s)",
                    "_bg.iterate()",
                    "_bg.iterate()",
                    "_result = True",
                ]
            ),
            "p23a_a_scaffold",
        )
        rec(ctx, "a_transport_made_reel_shelf_clip_and_two_iterations", made is True, resp=made)
        world = await tx_world(ctx, uid)
        info2 = await do(session, ctx, "batch.info", {"target": target})
        r2 = _result_of(info2)

        def rows(rs: list[dict[str, Any]]) -> list[Any]:
            return [(r.get("name"), r.get("clips"), r.get("sequences")) for r in rs]

        rec(
            ctx,
            "a_info_reels_and_shelf_reels_equal_the_independent_read",
            info2.get("ok") is True
            and rows(r2.get("reels", []))
            == [(r["name"], r["clips"], r["sequences"]) for r in world["reels"]]
            and rows(r2.get("shelfReels", []))
            == [(r["name"], r["clips"], r["sequences"]) for r in world["shelfReels"]]
            and any(r["clips"] for r in world["shelfReels"]),
            viaTransport={k: world[k] for k in ("reels", "shelfReels")},
            info={k: r2.get(k) for k in ("reels", "shelfReels")},
        )
        rec(
            ctx,
            "a_info_iterations_equal_the_independent_read",
            [(i.get("name"), i.get("number"), i.get("uid")) for i in r2.get("iterations", [])]
            == [(i["name"], i["number"], i["uid"]) for i in world["iterations"]]
            and len(world["iterations"]) == 2,
            viaTransport=world["iterations"],
            info=r2.get("iterations"),
        )
        rec(
            ctx,
            "a_info_current_iteration_number_equals_the_independent_read",
            r2.get("currentIterationNumber") == world.get("currentIteration"),
            viaTransport=world.get("currentIteration"),
            info=r2.get("currentIterationNumber"),
        )
        rec(ctx, "a_info_wrote_nothing", journal_group_count(ctx) == 0)


# stage b
async def all_sections_fp(session: Any, ctx: Ctx, target: dict[str, Any]) -> Any:
    env = await do(session, ctx, "batch.fingerprint", {"target": target, "sections": ALL_SECTIONS})
    return _result_of(env).get("fingerprint")


async def plain_fp(session: Any, ctx: Ctx, target: dict[str, Any]) -> Any:
    env = await do(session, ctx, "batch.fingerprint", {"target": target})
    return _result_of(env).get("fingerprint")


async def op_then_revert(
    session: Any,
    ctx: Ctx,
    group: str,
    op: str,
    args: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Run `op`, read the group back, `journal.revert` it, read again, and record the fingerprints
    (plain and with every section) at the three moments. Returns everything for the judging."""
    uid, target = uid_of(ctx, group), target_of(ctx, group)
    base = await tx_world(ctx, uid)
    fp = {
        "base": await plain_fp(session, ctx, target),
        "baseAll": await all_sections_fp(session, ctx, target),
    }
    env = await do(session, ctx, op, {"target": target, **args})
    after_op = await tx_world(ctx, uid)
    fp["afterOp"] = await plain_fp(session, ctx, target)
    fp["afterOpAll"] = await all_sections_fp(session, ctx, target)
    journal = env.get("journal") or {}
    reverted = await do(session, ctx, "journal.revert", {"group": journal.get("group")})
    after_revert = await tx_world(ctx, uid)
    fp["afterRevert"] = await plain_fp(session, ctx, target)
    fp["afterRevertAll"] = await all_sections_fp(session, ctx, target)
    change = diff_world(base, after_op)
    back = diff_world(after_op, after_revert)
    leftover = diff_world(base, after_revert)
    observe(
        ctx,
        f"{label}_observation_what_the_op_changed_and_what_revert_brought_back",
        op=op,
        opChanged=change,
        revertChanged=back,
        stillDifferentFromBeforeTheOp=leftover,
        fingerprints=fp,
        fingerprintPlainBackToBase=fp["afterRevert"] == fp["base"],
        fingerprintAllSectionsBackToBase=fp["afterRevertAll"] == fp["baseAll"],
        opEnvelopeJournalChanged=journal.get("changed"),
        revertEnvelope={
            k: reverted.get(k)
            for k in ("ok", "errorCode", "error", "hint", "affected", "undoSteps")
        },
        revertResult={
            k: _result_of(reverted).get(k)
            for k in ("matched", "expectedFingerprint", "fingerprintAfter", "reverted")
        },
    )
    return {
        "env": env,
        "reverted": reverted,
        "base": base,
        "afterOp": after_op,
        "afterRevert": after_revert,
        "change": change,
        "back": back,
        "leftover": leftover,
        "fp": fp,
        "journal": journal,
    }


def reel_named(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((r for r in rows if r["name"] == name), None)


async def stage_b(ctx: Ctx) -> None:
    async with server_session(ctx, "b-plain") as session:
        # b1 create_reel
        r = await op_then_revert(
            session, ctx, "B1", "batch.create_reel", {"name": "P23A_new_reel"}, "b1"
        )
        rec(
            ctx,
            "b1_create_reel_made_the_reel_and_the_envelope_names_the_group",
            r["env"].get("ok") is True
            and reel_named(r["afterOp"]["reels"], "P23A_new_reel") is not None
            and reel_named(r["base"]["reels"], "P23A_new_reel") is None
            and r["journal"].get("group")
            and r["journal"].get("checkpoint"),
            change=r["change"],
            env=r["env"],
        )
        rec(
            ctx,
            "b1_the_reel_is_not_in_the_default_fingerprint_so_changed_reads_false",
            r["journal"].get("changed") is False and r["fp"]["afterOp"] == r["fp"]["base"],
            journal=r["journal"],
        )
        rec(
            ctx,
            "b1_revert_answers_ok_and_the_nodes_and_wiring_are_as_before",
            r["reverted"].get("ok") is True
            and "nodes" not in r["leftover"]
            and "wiring" not in r["leftover"],
            revert=r["reverted"],
            leftover=r["leftover"],
        )

        # b2 create_shelf_reel
        r = await op_then_revert(
            session, ctx, "B2", "batch.create_shelf_reel", {"name": "P23A_new_shelf"}, "b2"
        )
        rec(
            ctx,
            "b2_create_shelf_reel_made_the_shelf_reel",
            r["env"].get("ok") is True
            and reel_named(r["afterOp"]["shelfReels"], "P23A_new_shelf") is not None
            and reel_named(r["base"]["shelfReels"], "P23A_new_shelf") is None
            and (reel_named(r["afterOp"]["shelfReels"], "P23A_new_shelf") or {}).get("type")
            == "Shelf",
            change=r["change"],
            env=r["env"],
        )
        rec(
            ctx,
            "b2_revert_answers_ok_and_the_nodes_and_wiring_are_as_before",
            r["reverted"].get("ok") is True
            and "nodes" not in r["leftover"]
            and "wiring" not in r["leftover"],
            revert=r["reverted"],
            leftover=r["leftover"],
        )

        # b3 add_clips into the schematic reel
        world = await tx_world(ctx, uid_of(ctx, "B3"))
        schematic = world["reels"][0]["name"]
        clips = [desk_clip(ctx, 0), desk_clip(ctx, 1)]
        r = await op_then_revert(
            session, ctx, "B3", "batch.add_clips", {"reel": schematic, "clips": clips}, "b3"
        )
        added = (r["change"].get("nodes") or {}).get("added", [])
        new_nodes = [n for n in r["afterOp"]["nodes"] if n["name"] in added]
        rec(
            ctx,
            "b3_add_clips_to_a_schematic_reel_made_two_clip_nodes_and_two_reel_clips",
            r["env"].get("ok") is True
            and len(added) == 2
            and all(n["pyType"] == "PyClipNode" for n in new_nodes)
            and len(reel_named(r["afterOp"]["reels"], schematic)["clips"]) == 2
            and _result_of(r["env"]).get("clipNodesAsExpected") is True,
            change=r["change"],
            env=r["env"],
        )
        rec(
            ctx,
            "b3_the_default_fingerprint_saw_the_clip_nodes",
            r["journal"].get("changed") is True and r["fp"]["afterOp"] != r["fp"]["base"],
            journal=r["journal"],
        )
        rec(
            ctx,
            "b3_revert_removed_the_clip_nodes_and_the_fingerprint_equals_the_one_before",
            r["reverted"].get("ok") is True
            and not set(added) & set(node_names(r["afterRevert"]))
            and r["fp"]["afterRevert"] == r["fp"]["base"]
            and "nodes" not in r["leftover"]
            and "wiring" not in r["leftover"],
            revert=r["reverted"],
            leftover=r["leftover"],
        )

        # b4 add_clips into the shelf reel
        world = await tx_world(ctx, uid_of(ctx, "B4"))
        shelf = world["shelfReels"][0]["name"]
        r = await op_then_revert(
            session,
            ctx,
            "B4",
            "batch.add_clips",
            {"reel": shelf, "clips": [desk_clip(ctx, 2)]},
            "b4",
        )
        rec(
            ctx,
            "b4_add_clips_to_a_shelf_reel_made_no_node_and_one_shelf_clip",
            r["env"].get("ok") is True
            and "nodes" not in r["change"]
            and len(reel_named(r["afterOp"]["shelfReels"], shelf)["clips"]) == 1
            and _result_of(r["env"]).get("clipNodesAdded") == [],
            change=r["change"],
            env=r["env"],
        )
        rec(
            ctx,
            "b4_revert_answers_ok_and_the_nodes_and_wiring_are_as_before",
            r["reverted"].get("ok") is True
            and "nodes" not in r["leftover"]
            and "wiring" not in r["leftover"],
            revert=r["reverted"],
            leftover=r["leftover"],
        )

        # b5 organize
        r = await op_then_revert(session, ctx, "B5", "batch.organize", {}, "b5")
        rec(
            ctx,
            "b5_organize_ok_and_at_least_one_node_moved",
            r["env"].get("ok") is True
            and _result_of(r["env"]).get("returned") is True
            and bool(r["change"].get("positions")),
            change=r["change"],
            env=r["env"],
        )
        rec(
            ctx,
            "b5_revert_answers_ok_and_the_nodes_and_wiring_are_as_before",
            r["reverted"].get("ok") is True
            and "nodes" not in r["leftover"]
            and "wiring" not in r["leftover"],
            revert=r["reverted"],
            leftover=r["leftover"],
        )

        # b6: a LIBRARY clip and a SEQUENCE
        uid6, target6 = uid_of(ctx, "B6"), target_of(ctx, "B6")
        world6 = await tx_world(ctx, uid6)
        schematic6 = world6["reels"][0]["name"]
        lib_clip = wt_selector(str(ctx.ids["libClips"][0][1]))
        lib = await do(
            session,
            ctx,
            "batch.add_clips",
            {"target": target6, "reel": schematic6, "clips": [lib_clip]},
        )
        after_lib = await tx_world(ctx, uid6)
        observe(
            ctx,
            "b6_add_clips_with_a_library_clip",
            opOk=lib.get("ok"),
            errorCode=lib.get("errorCode"),
            error=lib.get("error"),
            change=diff_world(world6, after_lib),
            result=_result_of(lib),
        )
        rec(
            ctx,
            "b6_the_library_clip_call_did_not_lose_the_bridge",
            has_no_bridge_loss(lib),
            env=lib,
        )
        seq = await do(
            session,
            ctx,
            "batch.add_clips",
            {
                "target": target6,
                "reel": schematic6,
                "clips": [wt_selector(str(ctx.ids["deskSeq"]))],
            },
        )
        after_seq = await tx_world(ctx, uid6)
        observe(
            ctx,
            "b6_add_clips_with_a_sequence",
            opOk=seq.get("ok"),
            errorCode=seq.get("errorCode"),
            error=seq.get("error"),
            change=diff_world(after_lib, after_seq),
            result=_result_of(seq),
        )
        rec(ctx, "b6_the_sequence_call_did_not_lose_the_bridge", has_no_bridge_loss(seq), env=seq)
        bad = await do(
            session,
            ctx,
            "batch.add_clips",
            {"target": target6, "reel": "P23A_no_such_reel", "clips": [desk_clip(ctx, 0)]},
        )
        groups_before = journal_group_count(ctx)
        rec(
            ctx,
            "b6_a_reel_that_is_not_there_is_refused_before_a_checkpoint",
            bad.get("errorCode") == "SELECTOR_EMPTY"
            and bad.get("journal") is None
            and journal_group_count(ctx) == groups_before
            and diff_world(after_seq, await tx_world(ctx, uid6)) == {},
            env=bad,
        )


async def stage_c(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "C"), target_of(ctx, "C")
    async with server_session(ctx, "c-plain") as session:
        base = await tx_world(ctx, uid)
        first = await do(session, ctx, "batch.iterate", {"target": target})
        w1 = await tx_world(ctx, uid)
        rec(
            ctx,
            "c_iterate_made_iteration_1",
            first.get("ok") is True
            and [i["number"] for i in w1["iterations"]] == [1]
            and _result_of(first).get("iteration", {}).get("number") == 1
            and _result_of(first).get("iteration", {}).get("uid") == w1["iterations"][0]["uid"],
            change=diff_world(base, w1),
            env=first,
        )
        await tx_add_node(ctx, uid, "Mux")
        second = await do(session, ctx, "batch.iterate", {"target": target})
        w2 = await tx_world(ctx, uid)
        rec(
            ctx,
            "c_iterate_again_made_iteration_2",
            second.get("ok") is True and [i["number"] for i in w2["iterations"]] == [1, 2],
            iterations=w2["iterations"],
            env=second,
        )
        uid_of_1 = w2["iterations"][0]["uid"]

        groups_before = journal_group_count(ctx)
        checkpoints_before = len(Journal(ctx.journal_dir).read_all().records)
        refused = await do(session, ctx, "batch.iterate", {"target": target, "index": 1})
        w3 = await tx_world(ctx, uid)
        rec(
            ctx,
            "c_index_of_an_existing_iteration_without_overwrite_is_refused",
            refused.get("ok") is False
            and refused.get("errorCode") == "INVALID_ARGS"
            and "overwrite=true" in str(refused.get("error"))
            and w3["iterations"] == w2["iterations"],
            env=refused,
        )
        rec(
            ctx,
            "c_the_refusal_took_no_checkpoint_and_left_no_journal_group",
            refused.get("journal") is None
            and journal_group_count(ctx) == groups_before
            and len(Journal(ctx.journal_dir).read_all().records) == checkpoints_before,
        )

        over = await do(
            session, ctx, "batch.iterate", {"target": target, "index": 1, "overwrite": True}
        )
        w4 = await tx_world(ctx, uid)
        rec(
            ctx,
            "c_overwrite_true_replaced_iteration_1",
            over.get("ok") is True
            and _result_of(over).get("overwrote") is True
            and [i["number"] for i in w4["iterations"]] == [1, 2]
            and w4["iterations"][0]["uid"] != uid_of_1
            and w4["iterations"][1]["uid"] == w2["iterations"][1]["uid"],
            change=diff_world(w2, w4),
            env=over,
        )
        observe(
            ctx,
            "c_envelope_of_the_overwrite",
            journal=over.get("journal"),
            execution=over.get("execution"),
            undoSteps=over.get("undoSteps"),
        )

        # the reverts: what happens to the iterations
        rv = await do(
            session, ctx, "journal.revert", {"group": (over.get("journal") or {}).get("group")}
        )
        w5 = await tx_world(ctx, uid)
        observe(
            ctx,
            "c_observation_revert_of_the_overwrite",
            revertOk=rv.get("ok"),
            errorCode=rv.get("errorCode"),
            error=rv.get("error"),
            iterationsBefore=[(i["number"], i["uid"]) for i in w4["iterations"]],
            iterationsAfter=[(i["number"], i["uid"]) for i in w5["iterations"]],
            change=diff_world(w4, w5),
            overwrittenIterationBack=w5["iterations"][0]["uid"] == uid_of_1
            if w5["iterations"]
            else None,
        )
        rv2 = await do(
            session, ctx, "journal.revert", {"group": (second.get("journal") or {}).get("group")}
        )
        w6 = await tx_world(ctx, uid)
        observe(
            ctx,
            "c_observation_revert_of_the_second_iterate",
            revertOk=rv2.get("ok"),
            errorCode=rv2.get("errorCode"),
            error=rv2.get("error"),
            iterationsBefore=[(i["number"], i["uid"]) for i in w5["iterations"]],
            iterationsAfter=[(i["number"], i["uid"]) for i in w6["iterations"]],
            iteration2StillThere=any(i["number"] == 2 for i in w6["iterations"]),
            nodesAfter=node_names(w6),
            change=diff_world(w5, w6),
        )
        rec(
            ctx,
            "c_reverts_did_not_lose_the_bridge",
            has_no_bridge_loss(rv) and has_no_bridge_loss(rv2),
        )


async def stage_d(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "D"), target_of(ctx, "D")
    directory = str(OUTPUT_DIR / f"save_setup_e2e_{ctx.ts}")
    await tx_makedirs(ctx, directory)
    plain = os.path.join(directory, "dsetup")
    async with server_session(ctx, "d-plain") as session:
        groups_before = journal_group_count(ctx)
        env = await do(session, ctx, "batch.save_setup", {"target": target, "path": plain})
        result = _result_of(env)
        files = await tx_glob_batch(ctx, directory)
        rec(
            ctx,
            "d_a_dotless_name_gets_batch_appended_and_the_reported_path_exists",
            env.get("ok") is True
            and result.get("path") == plain + ".batch"
            and await tx_file_exists(ctx, plain + ".batch") is True
            and files == [plain + ".batch"]
            and result.get("written") == [plain + ".batch"],
            env=env,
            glob=files,
        )
        observe(
            ctx,
            "d_the_sibling_directory_save_setup_wrote",
            directoryBesideTheFile=await tx_dir_exists(ctx, plain),
            size=result.get("size"),
            saveSetupReturned=result.get("saveSetupReturned"),
        )
        rec(
            ctx,
            "d_save_setup_is_not_journaled_and_declares_no_undo",
            env.get("journal") is None
            and env.get("execution", {}).get("undo") == "none"
            and journal_group_count(ctx) == groups_before,
            execution=env.get("execution"),
        )
        world = await tx_world(ctx, uid)
        rec(ctx, "d_the_group_is_unchanged", "error" not in world)

        mtime = await tx_mtime(ctx, plain + ".batch")
        again = await do(session, ctx, "batch.save_setup", {"target": target, "path": plain})
        rec(
            ctx,
            "d_an_existing_target_is_refused_and_the_file_is_untouched",
            again.get("errorCode") == "PRECONDITION_UNMET"
            and "overwrite=true" in str(again.get("error"))
            and await tx_mtime(ctx, plain + ".batch") == mtime,
            env=again,
        )
        await tx_add_node(ctx, uid, "Mux")
        forced = await do(
            session, ctx, "batch.save_setup", {"target": target, "path": plain, "overwrite": True}
        )
        rec(
            ctx,
            "d_overwrite_true_replaces_it_and_reports_it",
            forced.get("ok") is True
            and _result_of(forced).get("overwritten") == [plain + ".batch"]
            and (await tx_mtime(ctx, plain + ".batch")) != mtime,
            env=forced,
        )

        dotted = os.path.join(directory, "d.dotted")
        dot = await do(session, ctx, "batch.save_setup", {"target": target, "path": dotted})
        dot_files = await tx_glob_batch(ctx, directory)
        rec(
            ctx,
            "d_a_dotted_name_reports_a_path_that_exists",
            dot.get("ok") is True
            and isinstance(_result_of(dot).get("path"), str)
            and await tx_file_exists(ctx, str(_result_of(dot).get("path"))) is True,
            env=dot,
        )
        observe(
            ctx,
            "d_where_the_dotted_name_went",
            requested=dotted,
            reported=_result_of(dot).get("path"),
            batchFilesInTheDirectory=dot_files,
        )

        relative = await do(
            session, ctx, "batch.save_setup", {"target": target, "path": "rel/setup"}
        )
        missing_dir = os.path.join(directory, "no_such_dir", "x")
        missing = await do(
            session, ctx, "batch.save_setup", {"target": target, "path": missing_dir}
        )
        rec(
            ctx,
            "d_a_relative_path_and_a_missing_directory_are_refused",
            relative.get("errorCode") == "INVALID_ARGS"
            and missing.get("errorCode") == "PRECONDITION_UNMET"
            and await tx_dir_exists(ctx, os.path.join(directory, "no_such_dir")) is False,
            relative=relative,
            missing=missing,
        )


async def stage_e(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "E"), target_of(ctx, "E")
    base = await tx_world(ctx, uid)
    async with server_session(ctx, "e-switch", hooks={ENABLE_EVAL: "1"}) as session:
        listing = await do(
            session, ctx, "batch.fingerprint", {"target": target, "include_nodes": True}
        )
        listed = {n["name"]: n for n in _result_of(listing).get("nodes", [])}
        actions = [n for n in base["nodes"] if n["pyType"] == "PyActionNode"]
        rec(
            ctx,
            "e_the_fingerprint_lists_the_inner_nodes_of_the_action_node",
            listing.get("ok") is True
            and len(actions) == 1
            and len(actions[0]["inner"] or []) >= 1
            and [i[0] for i in (listed.get(actions[0]["name"], {}).get("inner") or [])]
            == sorted(actions[0]["inner"]),
            viaTransport=actions,
            fingerprintListing=listed.get(actions[0]["name"]) if actions else None,
        )
        observe(
            ctx,
            "e_observation_inner_node_types_the_fingerprint_reads",
            inner=(listed.get(actions[0]["name"], {}) if actions else {}).get("inner"),
            unreadableNodes=_result_of(listing).get("unreadableNodes"),
        )
        env = await do(
            session,
            ctx,
            "eval.run",
            {"code": EVAL_MAKE_PAIR, "effect": "destructive", "target": target},
        )
        made = env.get("result")
        journal = env.get("journal") or {}
        now = await tx_world(ctx, uid)
        rec(
            ctx,
            "e_destructive_eval_created_two_nodes_and_names_group_checkpoint_revert",
            env.get("ok") is True
            and isinstance(made, list)
            and sorted(node_names(now)) == sorted([*node_names(base), *made])
            and isinstance(journal.get("group"), str)
            and isinstance(journal.get("checkpoint"), str)
            and journal.get("changed") is True
            and (journal.get("revert") or {}).get("op") == "journal.revert",
            made=made,
            journal=journal,
        )
        reverted = await do(session, ctx, "journal.revert", {"group": journal.get("group")})
        after = await tx_world(ctx, uid)
        rec(
            ctx,
            "e_eval_revert_removes_the_nodes",
            reverted.get("ok") is True
            and diff_world(base, after).get("nodes") is None
            and wiring(base) == wiring(after)
            and state_of(ctx, journal.get("group")) == "reverted",
            change=diff_world(base, after),
            env=reverted,
        )

        cp = await do(session, ctx, "batch.checkpoint", {"target": target, "label": "p23a e"})
        cp_path = _result_of(cp).get("path")
        rec(
            ctx,
            "e_checkpoint_taken_and_its_file_exists",
            cp.get("ok") is True and await tx_file_exists(ctx, str(cp_path)) is True,
            env=cp,
        )
        await tx_add_node(ctx, uid, "Mux")
        changed = await tx_world(ctx, uid)
        loaded = await do(session, ctx, "batch.load_setup", {"target": target, "path": cp_path})
        after_load = await tx_world(ctx, uid)
        pre = _result_of(loaded).get("preCheckpoint") or {}
        rec(
            ctx,
            "e_load_setup_replaced_the_group_after_checkpointing_the_current_state",
            loaded.get("ok") is True
            and node_names(after_load) == node_names(base)
            and wiring(after_load) == wiring(base)
            and isinstance(pre.get("path"), str)
            and await tx_file_exists(ctx, str(pre["path"])) is True,
            viaTransport=node_names(after_load),
            env=loaded,
        )
        back = await do(
            session, ctx, "journal.revert", {"group": (loaded.get("journal") or {}).get("group")}
        )
        after_back = await tx_world(ctx, uid)
        rec(
            ctx,
            "e_revert_of_load_setup_brings_the_previous_contents_back",
            back.get("ok") is True
            and sorted(node_names(after_back)) == sorted(node_names(changed))
            and wiring(after_back) == wiring(changed),
            viaTransport=node_names(after_back),
            env=back,
        )
        observe(
            ctx,
            "e_observation_action_inner_nodes_before_and_after_the_restores",
            before=[n["inner"] for n in base["nodes"] if n["pyType"] == "PyActionNode"],
            afterEvalRevert=[n["inner"] for n in after["nodes"] if n["pyType"] == "PyActionNode"],
            afterLoadSetupRevert=[
                n["inner"] for n in after_back["nodes"] if n["pyType"] == "PyActionNode"
            ],
        )
        missing = await do(
            session,
            ctx,
            "batch.load_setup",
            {"target": target, "path": str(ctx.journal_dir / "checkpoints" / "no_such.batch")},
        )
        after_missing = await tx_world(ctx, uid)
        rec(
            ctx,
            "e_load_setup_of_a_missing_file_is_refused_and_nothing_changed",
            missing.get("errorCode") == "PRECONDITION_UNMET"
            and _result_of(missing).get("loadSetupCalled") is False
            and diff_world(after_back, after_missing) == {},
            env=missing,
        )


async def stage_z(ctx: Ctx) -> None:
    async with server_session(ctx, "z-plain") as session:
        info = await version_info(session, ctx)
        journal = _result_of(info).get("journal", {})
        rec(
            ctx,
            "z_nothing_is_left_undecided_and_no_test_hook_is_active",
            journal.get("unresolvedIntents") == 0
            and journal.get("writesBlocked") is False
            and journal.get("testHooks") == [],
            journal=journal,
        )
        listing = await do(session, ctx, "journal.list", {"limit": 60})
        ctx.results["journalListing"] = _result_of(listing).get("items")
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def print_observations(results: Results) -> None:
    print("--- observations (nothing here is a pass/fail) ---")
    for obs in results.get("observations", []):
        name = obs.get("name")
        if str(name).endswith("what_the_op_changed_and_what_revert_brought_back"):
            print(f"{name}")
            print(f"  op changed:              {obs.get('opChanged')}")
            print(f"  revert changed:          {obs.get('revertChanged')}")
            print(f"  still differs from base: {obs.get('stillDifferentFromBeforeTheOp')}")
            print(
                "  fingerprint back to base: plain="
                f"{obs.get('fingerprintPlainBackToBase')} allSections="
                f"{obs.get('fingerprintAllSectionsBackToBase')}  journal.changed="
                f"{obs.get('opEnvelopeJournalChanged')}"
            )
        else:
            print(f"{name}: { {k: v for k, v in obs.items() if k not in ('name', 'stage')} }")


def cleanup(ctx: Ctx) -> None:
    ctx.stage = "cleanup"
    ctx.results["cleanup"] = {
        "deleted": [],
        "kept": {
            "batchGroups": ctx.ids.get("groups"),
            "checkpointFiles": str(ctx.journal_dir / "checkpoints"),
            "library": f"P23A_LIB_{ctx.ts}",
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them); checkpoint files are never "
                "deleted; the library and the desktop reel are kept because the groups' clips "
                "are copies that reference their media"
            ),
        },
    }
    exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label="p23a_cleanup_exit")
    rec(
        ctx,
        "cleanup_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
    )


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        ready = "groups" in ctx.ids and "deskClips" in ctx.ids and "libClips" in ctx.ids
        if ready:
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("e", stage_e),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_stages_a_to_e_skipped", False, ids=ctx.ids)
        await guarded(ctx, "z", stage_z)
    finally:
        cleanup(ctx)
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

    # every server starts from a known environment: the eval switch only where a stage sets it
    os.environ.pop(ENABLE_EVAL, None)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_p23a_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_3a_batch_e2e_{ts}.json",
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
