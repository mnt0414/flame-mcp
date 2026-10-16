#!/usr/bin/env python3
"""Scenario check (real Flame; run with the `.venv` Python;
`scripts/run_e2e.sh p2_4_scenario4` starts Flame): "make a Batch from the clips lined up in a
reel", done with OPS ONLY.

Checks: One scenario made only of ops: build a Batch from the clips lined up in a reel, connect each
Clip node to a Write File, organize, plan and render the Batch, then confirm the frames on disk and
the journal.

The op sequence (an MCP server subprocess per stage, `python -m flame_mcp`, a real stdio client):

  desktop.create (a reel group) -> desktop.create (a reel in it) -> desktop.copy (the 3 library
  clips into that reel) -> desktop.create (the Batch group) -> desktop.create (the decoy group)
  -> desktop.list_children (path root=desktop: the reel group, then the reel, then its clips)
  -> batch.list (find the group) -> batch.info (the schematic reel's name)
  -> batch.add_clips (the 3 clips into the schematic reel: 3 Clip nodes) -> node.list
  -> per clip: node.create `Write File`, node.connect Clip -> Write File,
     node.set_attributes (media_path in this run's own directory, range 1-4)
  -> batch.organize -> render.plan (wouldRefuse empty) -> render.batch -> journal.list

Every step of the scenario is an op (including the ones that make the groups and put the clips in
the reel). `flame_mcp.transport` is scaffolding only: it (1) prepares the MATERIAL -- a library with
a reel holding 3 imported clips (no op imports media) -- and (2) makes INDEPENDENT read-backs (the
node graph with its wiring, the Write File's resolved path and range, the files on disk by this
client and by Flame, the shelf reel, which group is current) and is never a step of the scenario.
The decoy group is created last, so the test group is NOT current.

JUDGED steps: every op answered ok, Clip nodes == clips, the connections read back by transport,
the attributes read back, the plan's 12 planned frames (3 Write Files x range 1-4) and empty
`wouldRefuse`, the frames on disk (`<name>000001.exr` .. 4, no dot before the number: the form
Flame writes), `render.batch`'s own count, every journaled write `landed`. OBSERVED steps
(`kind: "observation"`, always ok): elapsed time, positions after `batch.organize`, the shelf reel.

Output: `p2/files_p24s4_<ts>/render_<a|b|c>/`. Nothing is rendered into /var/tmp (each Write File is
given its own media_path before the plan is read). No Batch group is deleted (flame.delete on a
PyBatch is not used), rendered files are kept, then flame.exit() (BRIDGE_UNAVAILABLE retried
every 3 s for up to 180 s).
"""

from __future__ import annotations

import argparse
import hashlib
import re
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
    _structured,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (
    Ctx,
    do,
    do_read,
    guarded,
    rec,
    save,
    save_stage,
    server_session,
    tx,
    version_info,
)
from p2_4_common import (
    GOOD_JOURNAL_STATES,
    OUTPUT_DIR,
    flame_exit,
    journal_states,
    local_list,
    observe,
    ops_used,
    print_observations,
    tx_is_current,
    tx_list_dir,
    tx_scaffold,
    value_like,
)

Results = dict[str, Any]

CLIP_COUNT: Final[int] = 3
FRAMES_PER_WRITER: Final[int] = 4
WRITER_TAGS: Final[tuple[str, ...]] = ("a", "b", "c")
RENDER_TIMEOUT_S: Final[int] = 300
RENDER_CALL_TIMEOUT_S: Final[float] = 400.0
"""The client waits this long for render.batch (do() allows 60 s, too short for a slow render)."""
LIST_LIMIT: Final[int] = 100
GAPS: Final[tuple[str, ...]] = ()
"""What the scenario still needs transport for besides reading back (none: the material -- a library
with imported clips -- is the only scaffolding, and no op imports media)."""
PREPARE_OPS: Final[tuple[str, ...]] = (
    "desktop.create",
    "desktop.create",
    "desktop.copy",
    "desktop.create",
    "desktop.create",
)
"""The write ops of `prepare`, in order: reel group, reel, copy of the 3 clips, Batch group,
decoy Batch group."""


BUILD_STAGES: Final[frozenset[str]] = frozenset({"a", "b", "c"})
"""A failure in these stages leaves nothing sensible for the next ones to run on (organize, the
render and the journal check still run after a failure of d, e)."""


def failed_steps(ctx: Ctx) -> int:
    return sum(1 for s in ctx.results["steps"] if not s["ok"])


def writer_name(tag: str) -> str:
    return f"s4_write_{tag}"


def out_dir(ctx: Ctx, tag: str) -> Path:
    return Path(str(ctx.ids["filesDir"])) / f"render_{tag}"


def target_of(ctx: Ctx) -> dict[str, Any]:
    return {"kind": "batch", "uid": ctx.ids["uid"]}


def node_sel(ctx: Ctx, name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": target_of(ctx), "name": name}


def frames_of(names: list[str] | None, prefix: str) -> list[str]:
    """The numbered frames `<prefix><digits>.exr` (no dot before the number); the `.clip`
    Flame writes next to them is not one."""
    return [n for n in names or [] if re.fullmatch(rf"{re.escape(prefix)}\d+\.exr", n)]


def expected_frames(tag: str) -> list[str]:
    return [f"{writer_name(tag)}{n:06d}.exr" for n in range(1, FRAMES_PER_WRITER + 1)]


# Transport read-backs (independent of the ops under test)

WORLD_CODE: Final[str] = "\n".join(
    [
        "def _try(fn):",
        "    try:",
        "        return fn()",
        "    except BaseException as _e:",
        "        return 'ERR:' + type(_e).__name__",
        "_nodes = []",
        "for _n in _bg.nodes:",
        "    _s = _n.sockets",
        "    _nodes.append({",
        "        'name': _v(_n.name), 'pyType': type(_n).__name__,",
        "        'x': _try(lambda: _v(_n.pos_x)), 'y': _try(lambda: _v(_n.pos_y)),",
        "        'in': {k: sorted(v) for k, v in _s['input'].items() if v},",
        "        'out': {k: sorted(v) for k, v in _s['output'].items() if v},",
        "    })",
        "_result = {'nodes': sorted(_nodes, key=lambda e: e['name'])}",
    ]
)


async def tx_world(ctx: Ctx) -> dict[str, Any]:
    out = await tx_scaffold(ctx, str(ctx.ids["uid"]), WORLD_CODE, "p24s4_world")
    return out if isinstance(out, dict) else {"error": out, "nodes": []}


async def tx_writer(ctx: Ctx, name: str) -> Any:
    body = "\n".join(
        [
            f"_w = [n for n in _bg.nodes if _v(n.name) == {name!r}][0]",
            "_result = {'resolved': _w.get_resolved_media_path(),",
            "           'first': _v(_w.range_start), 'last': _v(_w.range_end),",
            "           'mediaPath': _v(_w.media_path), 'bypass': _v(_w.bypass),",
            "           'pyType': type(_w).__name__}",
        ]
    )
    return await tx_scaffold(ctx, str(ctx.ids["uid"]), body, "p24s4_writer")


async def tx_shelf(ctx: Ctx) -> Any:
    body = "_result = {_v(r.name): sorted(_v(c.name) for c in r.clips) for r in _bg.shelf_reels}"
    return await tx_scaffold(ctx, str(ctx.ids["uid"]), body, "p24s4_shelf")


def node_row(world: dict[str, Any], name: str) -> dict[str, Any]:
    return next((n for n in world.get("nodes", []) if n["name"] == name), {})


async def render_call(session: Any, ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    with anyio.fail_after(RENDER_CALL_TIMEOUT_S):
        result = await session.call_tool("flame_do", {"op": "render.batch", "args": args})
    return _structured(result, tool="flame_do", results=ctx.results)


async def wdo(session: Any, ctx: Ctx, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """`do` for a journaled write: remembers the journal group the envelope names."""
    env = await do(session, ctx, op, args)
    group = (env.get("journal") or {}).get("group")
    if env.get("ok") is True and isinstance(group, str):
        ctx.ids.setdefault("writeGroups", []).append({"op": op, "group": group})
    return env


async def prepare(ctx: Ctx) -> None:
    ctx.ids["filesDir"] = str(OUTPUT_DIR / f"files_p24s4_{ctx.ts}")
    for tag in WRITER_TAGS:
        out_dir(ctx, tag).mkdir(parents=True, exist_ok=True)
    lib_name = f"P24S4_LIB_{ctx.ts}"
    paths = [str(MEDIA_DIR / f"clip{i}.mov") for i in range(1, CLIP_COUNT + 1)]
    material = await tx(
        ctx,
        "\n".join(
            [
                "ws = flame.projects.current_project.current_workspace",
                f"lib = ws.create_library({lib_name!r})",
                "reel = lib.create_reel('R1')",
                "_ids = []",
                f"for _p in {paths!r}:",
                "    _c = flame.import_clips(_p, reel)[0]",
                "    _ids.append([_v(_c.name), _c.get_wiretap_node_id()])",
                "_result = {'libWtid': lib.get_wiretap_node_id(), 'libClips': _ids}",
            ]
        ),
        label="p24s4_prepare_material",
        timeout_s=120.0,
    )
    rec(ctx, "prepare_library_with_3_imported_clips", material.ok, resp=_br_dict(material))
    if not (material.ok and isinstance(material.result, dict)):
        return
    ctx.ids.update(libName=lib_name, **material.result)
    lib_clips = [{"kind": "wiretap", "id": wtid} for _name, wtid in material.result["libClips"]]

    rg_name, reel_name = f"P24S4_RG_{ctx.ts}", f"P24S4_REEL_{ctx.ts}"
    group_name = f"P24S4_{ctx.ts}"
    async with server_session(ctx, "s4-prepare") as session:
        made_rg = await wdo(session, ctx, "desktop.create", {"kind": "reel_group", "name": rg_name})
        rg_selector = _result_of(made_rg).get("selector")
        made_reel = await wdo(
            session,
            ctx,
            "desktop.create",
            {"kind": "reel", "name": reel_name, "parent": rg_selector},
        )
        reel_selector = _result_of(made_reel).get("selector")
        copied = await wdo(
            session,
            ctx,
            "desktop.copy",
            {"sources": lib_clips, "destination": reel_selector},
        )
        copies = _result_of(copied).get("copies") or []
        rec(
            ctx,
            "prepare_desktop_reel_with_3_clips_by_desktop_create_and_desktop_copy",
            made_rg.get("ok") is True
            and made_reel.get("ok") is True
            and copied.get("ok") is True
            and isinstance(rg_selector, dict)
            and isinstance(reel_selector, dict)
            and len(copies) == CLIP_COUNT,
            errors=[e.get("error") for e in (made_rg, made_reel, copied)],
            copies=[c.get("name") for c in copies],
        )
        if not (isinstance(reel_selector, dict) and len(copies) == CLIP_COUNT):
            return
        ctx.ids.update(
            rgName=rg_name,
            reelName=reel_name,
            reelWtid=reel_selector.get("id"),
            clips=[[c.get("name"), c.get("wiretapId")] for c in copies],
        )

        made_group = await wdo(
            session, ctx, "desktop.create", {"kind": "batch_group", "name": group_name}
        )
        # created last: it becomes the current group, so the test group is not current
        decoy = await wdo(
            session,
            ctx,
            "desktop.create",
            {"kind": "batch_group", "name": f"P24S4_DECOY_{ctx.ts}"},
        )
    group_uid = _result_of(made_group).get("uid")
    decoy_uid = _result_of(decoy).get("uid")
    rec(
        ctx,
        "prepare_batch_group_and_decoy_by_desktop_create",
        made_group.get("ok") is True
        and decoy.get("ok") is True
        and isinstance(group_uid, str)
        and isinstance(decoy_uid, str),
        errors=[made_group.get("error"), decoy.get("error")],
    )
    if isinstance(group_uid, str) and isinstance(decoy_uid, str):
        ctx.ids.update(groupName=group_name, uid=group_uid, decoyUid=decoy_uid)
        rec(
            ctx,
            "prepare_the_test_group_is_not_current",
            await tx_is_current(ctx, group_uid) is False
            and await tx_is_current(ctx, decoy_uid) is True,
        )


async def stage_a(ctx: Ctx) -> None:
    async with server_session(ctx, "s4-a") as session:
        by_path = await do(
            session,
            ctx,
            "desktop.list_children",
            {
                "target": {"kind": "path", "root": "desktop", "path": [ctx.ids["rgName"]]},
                "limit": LIST_LIMIT,
            },
        )
        reels = [i for i in _result_of(by_path).get("items", []) if i.get("type") == "PyReel"]
        found = [r for r in reels if r.get("name") == ctx.ids["reelName"]]
        rec(
            ctx,
            "a_the_reel_is_found_below_its_reel_group_by_name",
            by_path.get("ok") is True
            and len(found) == 1
            and found[0].get("wiretapId") == ctx.ids["reelWtid"],
            error=by_path.get("error"),
            found=found,
        )
        listed = await do(
            session,
            ctx,
            "desktop.list_children",
            {
                "target": {"kind": "wiretap", "id": ctx.ids["reelWtid"]},
                "types": ["PyClip"],
                "limit": LIST_LIMIT,
            },
        )
        items = _result_of(listed).get("items", [])
        wanted = {name: wtid for name, wtid in ctx.ids["clips"]}
        rec(
            ctx,
            "a_the_reel_lists_its_3_clips_as_prepared",
            listed.get("ok") is True
            and _result_of(listed).get("total") == CLIP_COUNT
            and {i.get("name"): i.get("wiretapId") for i in items} == wanted,
            items=items,
            wanted=wanted,
        )
        ctx.ids["clipSelectors"] = [
            {"kind": "wiretap", "id": i["wiretapId"]} for i in items if i.get("wiretapId")
        ]

        located: dict[str, Any] | None = None
        seen = 0
        offset = 0
        while located is None:
            page = await do_read(
                session, ctx, "batch.list", {"limit": LIST_LIMIT, "offset": offset}
            )
            found_items = _result_of(page).get("items", [])
            located = next((g for g in found_items if g.get("uid") == ctx.ids["uid"]), None)
            seen += len(found_items)
            offset += len(found_items)
            if not found_items or not _result_of(page).get("truncated"):
                break
        rec(
            ctx,
            "a_batch_list_finds_the_group_and_says_it_is_not_current",
            located is not None
            and located.get("name") == ctx.ids["groupName"]
            and located.get("isCurrent") is False,
            group=located,
            groupsSeen=seen,
        )
        info = _result_of(await do(session, ctx, "batch.info", {"target": target_of(ctx)}))
        schematic = (info.get("reels") or [{}])[0]
        ctx.ids["schematicReel"] = schematic.get("name")
        rec(
            ctx,
            "a_batch_info_names_an_empty_schematic_reel_and_a_shelf_reel",
            bool(schematic.get("name"))
            and schematic.get("clipCount") == 0
            and info.get("nodeCount") == 0
            and len(info.get("shelfReels") or []) == 1,
            reels=[r.get("name") for r in info.get("reels", [])],
            shelfReels=[r.get("name") for r in info.get("shelfReels", [])],
        )


async def stage_b(ctx: Ctx) -> None:
    async with server_session(ctx, "s4-b") as session:
        added = await wdo(
            session,
            ctx,
            "batch.add_clips",
            {
                "target": target_of(ctx),
                "reel": ctx.ids["schematicReel"],
                "clips": ctx.ids["clipSelectors"],
            },
        )
        result = _result_of(added)
        rec(
            ctx,
            "b_add_clips_put_3_clips_into_the_schematic_reel_as_3_clip_nodes",
            added.get("ok") is True
            and result.get("requested") == CLIP_COUNT
            and result.get("returned") == CLIP_COUNT
            and result.get("clipNodesAsExpected") is True
            and len(result.get("clipNodesAdded") or []) == CLIP_COUNT,
            error=added.get("error"),
            result={
                k: result.get(k)
                for k in ("copied", "clipNodesAdded", "expectedClipNodes", "nodesBefore")
            },
        )
        listed = _result_of(await do(session, ctx, "node.list", {"target": target_of(ctx)}))
        clip_nodes = sorted(n["name"] for n in listed.get("items", []) if n.get("type") == "Clip")
        world = await tx_world(ctx)
        via_tx = sorted(n["name"] for n in world["nodes"] if n["pyType"] == "PyClipNode")
        rec(
            ctx,
            "b_node_list_and_transport_agree_there_are_3_clip_nodes_for_3_clips",
            len(clip_nodes) == CLIP_COUNT == len(via_tx)
            and clip_nodes == via_tx
            and clip_nodes == sorted(result.get("clipNodesAdded") or []),
            nodeList=clip_nodes,
            viaTransport=via_tx,
        )
        ctx.ids["clipNodes"] = sorted(result.get("clipNodesAdded") or [])


async def stage_c(ctx: Ctx) -> None:
    clip_nodes: list[str] = ctx.ids["clipNodes"]
    async with server_session(ctx, "s4-c") as session:
        for tag, clip in zip(WRITER_TAGS, clip_nodes, strict=False):
            writer = writer_name(tag)
            created = await wdo(
                session,
                ctx,
                "node.create",
                {"target": target_of(ctx), "type": "Write File", "name": writer},
            )
            connected = await wdo(
                session,
                ctx,
                "node.connect",
                {"source": node_sel(ctx, clip), "target": node_sel(ctx, writer)},
            )
            world = await tx_world(ctx)
            wired = node_row(world, writer).get("in", {})
            rec(
                ctx,
                f"c_{tag}_write_file_is_created_and_wired_to_{clip}_as_transport_reads_it",
                created.get("ok") is True
                and connected.get("ok") is True
                and node_row(world, writer).get("pyType") == "PyWriteFileNode"
                and any(clip in upstream for upstream in wired.values())
                and writer
                in [w for outs in node_row(world, clip).get("out", {}).values() for w in outs],
                created=created.get("error"),
                connected=connected.get("error"),
                inputs=wired,
                newLinks=_result_of(connected).get("newLinks"),
            )
            keys = ["media_path", "range_start", "range_end"]
            current = _result_of(
                await do(
                    session,
                    ctx,
                    "node.get_attributes",
                    {"target": node_sel(ctx, writer), "keys": keys},
                )
            ).get("attributes", {})
            now = {k: (current.get(k) or {}).get("value") for k in keys}
            values = {
                "media_path": value_like(now["media_path"], str(out_dir(ctx, tag)) + "/"),
                "range_start": value_like(now["range_start"], 1),
                "range_end": value_like(now["range_end"], FRAMES_PER_WRITER),
            }
            written = await wdo(
                session,
                ctx,
                "node.set_attributes",
                {"target": node_sel(ctx, writer), "values": values},
            )
            via_tx = await tx_writer(ctx, writer)
            rec(
                ctx,
                f"c_{tag}_media_path_and_range_are_written_and_read_back_by_transport",
                written.get("ok") is True
                and isinstance(via_tx, dict)
                and via_tx.get("mediaPath") == values["media_path"]
                and via_tx.get("first") == 1
                and via_tx.get("last") == FRAMES_PER_WRITER
                and str(out_dir(ctx, tag)) in str(via_tx.get("resolved")),
                error=written.get("error"),
                types={k: type(v).__name__ for k, v in now.items()},
                viaTransport=via_tx,
            )


async def stage_d(ctx: Ctx) -> None:
    before = await tx_world(ctx)
    wiring_before = {n["name"]: (n["in"], n["out"]) for n in before["nodes"]}
    async with server_session(ctx, "s4-d") as session:
        organized = await wdo(session, ctx, "batch.organize", {"target": target_of(ctx)})
        after = await tx_world(ctx)
        wiring_after = {n["name"]: (n["in"], n["out"]) for n in after["nodes"]}
        listed = _result_of(await do(session, ctx, "node.list", {"target": target_of(ctx)}))
        rec(
            ctx,
            "d_organize_ok_and_the_wiring_and_the_6_nodes_are_unchanged",
            organized.get("ok") is True
            and wiring_before == wiring_after
            and len(after["nodes"]) == CLIP_COUNT * 2
            and listed.get("total") == CLIP_COUNT * 2,
            error=organized.get("error"),
            total=listed.get("total"),
        )
        observe(
            ctx,
            "d_positions_before_and_after_organize",
            before={n["name"]: [n["x"], n["y"]] for n in before["nodes"]},
            after={n["name"]: [n["x"], n["y"]] for n in after["nodes"]},
            listedPositions={n["name"]: [n.get("posX"), n.get("posY")] for n in listed["items"]},
        )


async def stage_e(ctx: Ctx) -> None:
    async with server_session(ctx, "s4-e") as session:
        planned = await do(session, ctx, "render.plan", {"target": target_of(ctx)})
        plan = _result_of(planned)
        nodes = {n.get("name"): n for n in plan.get("nodes", [])}
        summary = plan.get("summary") or {}
        rec(
            ctx,
            "e_plan_has_3_effective_write_files_of_4_frames_and_would_refuse_nothing",
            planned.get("ok") is True
            and sorted(nodes) == sorted(writer_name(t) for t in WRITER_TAGS)
            and all(
                nodes[writer_name(t)].get("effective") is True
                and nodes[writer_name(t)].get("plannedFrames") == FRAMES_PER_WRITER
                and nodes[writer_name(t)].get("existingPlannedFiles") == 0
                and nodes[writer_name(t)].get("defaultPath") is False
                and nodes[writer_name(t)].get("outputDir") == str(out_dir(ctx, t))
                for t in WRITER_TAGS
                if writer_name(t) in nodes
            )
            and summary.get("plannedFrames") == CLIP_COUNT * FRAMES_PER_WRITER
            and summary.get("wouldRefuse") == [],
            error=planned.get("error"),
            summary=summary,
            nodes={
                k: {f: v.get(f) for f in ("effective", "plannedFrames", "skipReason")}
                for k, v in nodes.items()
            },
        )
        rec(
            ctx,
            "e_plan_wrote_nothing",
            all(local_list(out_dir(ctx, t)) == [] for t in WRITER_TAGS),
            listings={t: local_list(out_dir(ctx, t)) for t in WRITER_TAGS},
        )
        shelf_before = await tx_shelf(ctx)
        started = time.monotonic()
        env = await render_call(
            session, ctx, {"target": target_of(ctx), "timeout_s": RENDER_TIMEOUT_S}
        )
        wall = round(time.monotonic() - started, 2)
        result = _result_of(env)
        rows = {r.get("node"): r for r in result.get("nodes", [])}
        listings = {t: local_list(out_dir(ctx, t)) for t in WRITER_TAGS}
        via_flame = {t: await tx_list_dir(ctx, str(out_dir(ctx, t))) for t in WRITER_TAGS}
        rec(
            ctx,
            "e_render_ok_and_every_write_file_has_its_4_numbered_frames_on_disk",
            env.get("ok") is True
            and all(
                frames_of(listings[t], writer_name(t)) == expected_frames(t) for t in WRITER_TAGS
            )
            and all(
                isinstance(via_flame[t], list)
                and sorted(n for n, _ in via_flame[t] if n.endswith(".exr")) == expected_frames(t)
                and all(size > 0 for n, size in via_flame[t] if n.endswith(".exr"))
                for t in WRITER_TAGS
            ),
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            listings=listings,
        )
        rec(
            ctx,
            "e_render_result_counts_12_frames_and_each_node_is_complete",
            result.get("ran") is True
            and result.get("expectedFrames") == CLIP_COUNT * FRAMES_PER_WRITER
            and result.get("writtenFrames") == CLIP_COUNT * FRAMES_PER_WRITER
            and result.get("verified") is True
            and all(
                (rows.get(writer_name(t)) or {}).get("writtenFrames") == FRAMES_PER_WRITER
                and (rows.get(writer_name(t)) or {}).get("complete") is True
                for t in WRITER_TAGS
            ),
            counts={k: result.get(k) for k in ("expectedFrames", "writtenFrames", "verified")},
            perNode={k: (r.get("writtenFrames"), r.get("complete")) for k, r in rows.items()},
        )
        shelf_after = await tx_shelf(ctx)
        gained = {
            reel: [c for c in clips if c not in (shelf_before.get(reel) or [])]
            for reel, clips in (shelf_after.items() if isinstance(shelf_after, dict) else [])
        }
        first = out_dir(ctx, "a") / expected_frames("a")[0]
        observe(
            ctx,
            "e_render_observations",
            wallS=wall,
            elapsedS=result.get("elapsedS"),
            returned=result.get("returned"),
            shelfGained=gained,
            newClips=result.get("newClips"),
            otherNewFiles={k: r.get("otherNewFiles") for k, r in rows.items()},
            firstFrameMd5=hashlib.md5(first.read_bytes()).hexdigest() if first.exists() else None,
            degraded=env.get("degraded"),
        )
        ctx.ids["renderGroup"] = (env.get("journal") or {}).get("group")


async def stage_f(ctx: Ctx) -> None:
    written: list[dict[str, str]] = ctx.ids.get("writeGroups", [])
    expected_ops = (
        list(PREPARE_OPS)
        + ["batch.add_clips"]
        + ["node.create", "node.connect", "node.set_attributes"] * CLIP_COUNT
        + ["batch.organize"]
    )
    async with server_session(ctx, "s4-f") as session:
        listing = await do(session, ctx, "journal.list", {"limit": LIST_LIMIT})
        items = _result_of(listing).get("items", [])
        states = {i.get("group"): i.get("state") for i in items}
        rec(
            ctx,
            "f_the_write_ops_were_journaled_in_the_order_they_ran",
            [w["op"] for w in written] == expected_ops,
            ran=[w["op"] for w in written],
        )
        rec(
            ctx,
            "f_journal_list_says_landed_for_every_write_of_the_scenario",
            listing.get("ok") is True and all(states.get(w["group"]) == "landed" for w in written),
            states={w["op"]: states.get(w["group"]) for w in written},
        )
        render_group = ctx.ids.get("renderGroup")
        rec(
            ctx,
            "f_the_render_is_journaled_and_landed",
            isinstance(render_group, str) and states.get(render_group) == "landed",
            group=render_group,
            state=states.get(str(render_group)),
        )
        own = journal_states(ctx)
        rec(
            ctx,
            "f_nothing_in_this_runs_journal_is_unknown_failed_or_partial",
            len(own) == len(written) + 1 and set(own.values()) <= GOOD_JOURNAL_STATES,
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


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    observe(ctx, "z_ops_this_scenario_used", used=used, notUsed=ctx.results["opsNotUsed"])
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def cleanup(ctx: Ctx) -> None:
    ctx.results["cleanup"] = {
        "deleted": [],
        "kept": {
            "batchGroup": ctx.ids.get("groupName"),
            "library": ctx.ids.get("libName"),
            "reelGroup": ctx.ids.get("rgName"),
            "desktopReel": ctx.ids.get("reelName"),
            "renderedFiles": ctx.ids.get("filesDir"),
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them); rendered files are kept"
            ),
        },
    }
    flame_exit(ctx, "p24s4_cleanup_exit")


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if all(k in ctx.ids for k in ("uid", "clips", "reelWtid")):
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("e", stage_e),
                ("f", stage_f),
            ):
                failed_before = failed_steps(ctx)
                await guarded(ctx, name, stage)
                if name in BUILD_STAGES and failed_steps(ctx) > failed_before:
                    rec(ctx, f"stopped_after_build_stage_{name}_failed", True, kind="observation")
                    break
        else:
            rec(ctx, "prepare_incomplete_the_scenario_was_skipped", False, ids=sorted(ctx.ids))
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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_p24s4_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "gaps": list(GAPS),
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_4_scenario4_e2e_{ts}.json",
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
    print(f"gaps: {results['gaps']}")
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
