#!/usr/bin/env python3
"""Real-Flame end-to-end: the `render` category (`render.plan`, `render.batch`) (run with
the `.venv` Python; `scripts/run_e2e.sh p2_3c_render` starts Flame).

Checks: The `render` ops (`render.plan`, `render.batch`): the planned frames and the resolved path,
the rendered files on disk, the journal record, the refusals made before anything is written
(default output path, too many frames), the clip a Render node aimed at a Desktop reel group
puts into that group's reel (`Batch Renders` when the destination reads back as 2 elements,
the reel its 3rd element names when it reads back as 3), and the `outputs` that `batch.list`
lists for the Write File and Render nodes of these groups, and the file `node.save_setup` of a
Render node reports as its own, and the refusal of render.batch (and the same problem in
render.plan's wouldRefuse) for a group holding a Render node with no input connected, which
renders nothing, writes no journal record, and goes away once the node is wired.

Same shape and helpers as scripts/p2_3b_node_action_e2e.py (imported from p2_2a / p1): a real MCP
stdio client drives `python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport`
is scaffolding only (create the groups and the library, and READ EVERY STATE BACK INDEPENDENTLY of
the ops under test: the resolved path, the directory, the shelf reel's clips). The rendered files
are read by this client process directly (the same machine) AND by Flame's own Python. Everything
happens in the probe project `flame_mcp_probe`, in Batch groups that are NOT current (the decoy
group is created last, so it is the current one; stages g and i, the last two group-building
stages, make their own groups the current one in turn). The journal directory is this run's own
(`p2/journal_p23c_e2e_<ts>/`, never the production one). No test hook is set for any server.

The groups are built with the ops themselves (batch.add_clips of a LIBRARY clip into the group's
schematic reel -> a Clip node; node.create Write File; node.connect; node.set_attributes of
media_path / range_start / range_end, written in the type of the value they hold now).

Output goes to `p2/files_p23c_<ts>/render_<x>/`. NOTHING is ever rendered into `/var/tmp` (the
group of stage c has a Write File left on the default path and a 2-frame range, and the op is
expected to refuse it; the run checks `/var/tmp` for that node's name before and after).

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by what
was measured earlier. OBSERVED steps (`kind: "observation"`, always ok) record
what Flame did where nothing was known yet (elapsed time, what a timeout looks like, which verdict
`journal.check` reaches). They are collected in results["observations"] and printed at the end.

Stages
  a  render.plan of group A (Clip -> Write File `write_a`, media_path files_p23c_<ts>/render_a/,
     range 1-8): 8 planned frames, the resolved path equal to a transport read of
     get_resolved_media_path(), the directory there and empty, nothing written or journaled;
     plan.run refuses render.batch (and runs nothing).
  b  render.batch of group A: 8 numbered frames + a .clip (this client's listing, Flame's listing),
     result.writtenFrames == expectedFrames == 8, the shelf reel `Batch Renders` gained a clip
     (transport), the journal group reads landed with an intent (planned files, directory before)
     flushed and an outcome; journal.revert is refused. Observed: elapsed time, what `returned`
     was, the new files besides the frames, an md5 of the first frame.
  c  group C: a Write File left on the default media_path (/var/tmp/), range 1-2 -> render.batch is
     refused BEFORE it writes (PRECONDITION_UNMET, problems = default_path); nothing in the journal,
     nothing named write_c in /var/tmp before or after.
  d  group D: the default range 1-100 -> render.batch(max_frames=50) is refused (too_many_frames),
     the directory stays empty, nothing journaled.
  e  group E: only a BYPASSED Write File -> render.batch is refused (no_effective_node); the plan
     says `bypassed`; the directory stays empty, nothing journaled.
  f  group F: 24 frames, render.batch(timeout_s=1). Observed: how it ended -- a bridge pending
     handle, BRIDGE_LOST (no pending handle), a bare TIMEOUT, or in time (the
     intent carries the handle this server chose, `handleSource: "client"`, whichever way it
     ends). If it did not finish in time, the server refuses writes (JUDGED); journal.check
     asks the bridge for the result of the intent's handle (`transport.poll`) and is retried
     every 3 s for up to 180 s while its verdict is `unknown` (the bridge may not answer while
     Flame renders): every attempt's time, errorCode, verdict and basis are observed. JUDGED at
     the last attempt, for a pending end AND a BRIDGE_LOST end alike: verdict landed with basis
     `handle` (not `disk`), equal to the files on disk (24 frames), the `polled` record naming
     the intent's handle and holding Flame's answer (returned True), the writes resume. If it
     finished in time, JUDGED: the result file the bridge kept under the handle was removed by
     the server's clean-up poll. The note attribute this stage writes is put back.
  g  group G, created by the stage itself as the last Batch group, which makes it the current one
     (a Render node's destination takes a new reel group only in the current Batch group, so
     stage g builds and uses its own group last; it records that the group is current). Group G
     is a Clip node -> a Render node whose destination is set to ['Reel Groups', <this run's own
     Desktop reel group>] through transport and read back: JUDGED, the read-back is those 2
     elements or those 2 plus a reel name (Flame 2026.2.3 gives 3). The reel is the 3rd element
     when there is one, else `Batch Renders`; the read-back is kept for stage h. render.plan's
     destinationReel agrees with a transport read of that Desktop reel of the group (exists, clip
     count) before the render; render.batch is ok and result.newClips holds the one clip, named
     after the Render node, in `<group>/<reel>`; the Desktop reel
     (transport) gained exactly that clip; reelsBefore / reelsAfter carry the Desktop row; the
     plan after the render says the reel exists with one more clip. node.save_setup of the Render
     node (into `render_g_setup/` of the files directory): JUDGED, ok, result.path ends in
     `.output_node` and the extensions of result.files are exactly `.lut_node`, `.output_node`,
     `.resize` and `.resize_node`. Observed: elapsed time, what `returned` was, the
     reels of the group before and after.
  h  batch.list(outputs=true), paged with limit / offset (after a `stoppedEarly` the next page
     starts at offset + stoppedEarly.read) until the groups of this run are all found. JUDGED:
     group A lists exactly its Write File `write_a` (kind write_file, bypass false, mediaPath
     equal to a transport read of the node's media_path); group E lists exactly one write_file
     with bypass true; group G lists exactly the Render node (kind render, destination equal to a
     transport read of the node's destination, which must also equal the read-back kept in
     stage g, and must be a list: a stage g that ended early leaves none). Observed: the
     outputs of groups C, D and F, the number of pages, every stoppedEarly, the elapsed time, and
     the current group before and after (the call is not expected to change it).
  i  group I, created by the stage itself after stage h (it becomes the current group): a Clip
     node -> Render node `render_i_wired`, plus a Render node `render_i_open` with NO input
     connected and not bypassed, range 1-3 on both. JUDGED: render.plan is ok, its node row for
     `render_i_open` has wiredInputs 0 and effective false, and summary.wouldRefuse holds a
     render_node_unwired problem naming it; render.batch is refused (PRECONDITION_UNMET,
     result.ran false, problem codes exactly ['render_node_unwired'] naming `render_i_open`),
     nothing new in the journal, and the clip counts of the group's reels and shelf reels (read by
     transport) are the same as before. Then node.connect wires `render_i_open` to the Clip node
     and render.plan no longer lists render_node_unwired.
  z  cross-cutting checks over every envelope; nothing left undecided

Cleanup: this run's own Desktop reel group (`P23C_RG_<ts>`) is deleted and checked gone; no Batch
group is deleted (flame.delete on a PyBatch is not used); rendered files, the setup files of
stage g and the library are kept;
then flame.exit().
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
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
    V_PRELUDE,
    Ctx,
    do,
    guarded,
    journal_index,
    rec,
    save,
    save_stage,
    server_session,
    tx,
    version_info,
)

from flame_mcp.journal import Journal

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
GROUPS: Final[tuple[str, ...]] = ("A", "C", "D", "E", "F")
WRITERS: Final[dict[str, str]] = {
    "A": "write_a",
    "C": "write_c",
    "D": "write_d",
    "E": "write_e",
    "F": "write_f",
}
DEFAULT_DIR: Final[str] = "/var/tmp"
RENDER_GROUP: Final[str] = "G"
RENDER_NODE: Final[str] = "render_g"
RENDER_SETUP_SUFFIXES: Final[list[str]] = [".lut_node", ".output_node", ".resize", ".resize_node"]
RENDER_RANGE: Final[tuple[int, int]] = (1, 8)
UNWIRED_GROUP: Final[str] = "I"
UNWIRED_WIRED_NODE: Final[str] = "render_i_wired"
UNWIRED_OPEN_NODE: Final[str] = "render_i_open"
UNWIRED_RANGE: Final[tuple[int, int]] = (1, 3)
DESKTOP_REEL: Final[str] = "Batch Renders"
LIST_PAGE: Final[int] = 20
LIST_MAX_PAGES: Final[int] = 100
RANGES: Final[dict[str, tuple[int | None, int | None]]] = {
    "A": (1, 8),
    "C": (1, 2),
    "D": (None, None),  # the default range, 1-100
    "E": (1, 8),
    "F": (1, 24),
}

CHECK_RETRY_EVERY_S: Final[float] = 3.0
CHECK_RETRY_TOTAL_S: Final[float] = 180.0
"""While a Foreground render runs the bridge may not answer at all: journal.check
and the final flame.exit are retried this often, for this long."""

Results = dict[str, Any]


# Transport scaffolding (never through the op under test)
def _locate(uid: str) -> str:
    return "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            f"_bg = [g for g in _ws.desktop.batch_groups if _v(g.uid) == {uid!r}][0]",
        ]
    )


async def tx_scaffold(ctx: Ctx, uid: str, body: str, label: str, timeout_s: float = 40.0) -> Any:
    br = await tx(ctx, _locate(uid) + "\n" + body, label=label, timeout_s=timeout_s)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_is_current(ctx: Ctx, uid: str) -> Any:
    code = (
        "_cur = _v(flame.projects.current_project.current_workspace.desktop.current_batch_group)\n"
        f"_result = _cur is not None and _v(_cur.uid) == {uid!r}"
    )
    br = await tx(ctx, code, label="p23c_is_current")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_resolved(ctx: Ctx, uid: str, node: str) -> Any:
    body = "\n".join(
        [
            f"_w = [n for n in _bg.nodes if _v(n.name) == {node!r}][0]",
            "_result = {'resolved': _w.get_resolved_media_path(),",
            "           'first': _v(_w.range_start), 'last': _v(_w.range_end),",
            "           'mediaPath': _v(_w.media_path), 'bypass': _v(_w.bypass),",
            "           'pyType': type(_w).__name__}",
        ]
    )
    return await tx_scaffold(ctx, uid, body, "p23c_resolved")


async def tx_destination(ctx: Ctx, uid: str, node: str) -> Any:
    body = "\n".join(
        [
            f"_n = [n for n in _bg.nodes if _v(n.name) == {node!r}][0]",
            "_result = {'destination': list(_v(_n.destination)), 'bypass': _v(_n.bypass)}",
        ]
    )
    return await tx_scaffold(ctx, uid, body, "p23c_destination")


async def tx_list_dir(ctx: Ctx, path: str) -> Any:
    code = (
        "import os\n"
        f"_p = {path!r}\n"
        "_result = sorted([[n, os.path.getsize(os.path.join(_p, n))] for n in os.listdir(_p)]) "
        "if os.path.isdir(_p) else None"
    )
    br = await tx(ctx, code, label="p23c_ls")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_desktop_reel(ctx: Ctx, group: str, reel: str) -> Any:
    """The Desktop reel group `group` and its reel `reel`, read directly from Flame."""
    code = "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            f"_g = [g for g in _ws.desktop.reel_groups if _v(g.name) == {group!r}]",
            f"_r = [r for r in _g[0].reels if _v(r.name) == {reel!r}] if _g else []",
            "_result = {'groupFound': bool(_g),",
            "           'reelFound': bool(_r),",
            "           'reels': [_v(r.name) for r in _g[0].reels] if _g else None,",
            "           'clips': [_v(c.name) for c in _r[0].clips] if _r else []}",
        ]
    )
    br = await tx(ctx, code, label="p23c_desktop_reel")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_set_destination(ctx: Ctx, uid: str, node: str, value: list[str]) -> Any:
    body = "\n".join(
        [
            f"_n = [n for n in _bg.nodes if _v(n.name) == {node!r}][0]",
            f"_ret = _n.destination.set_value({tuple(value)!r})",
            "_result = {'returned': _ret, 'now': list(_v(_n.destination))}",
        ]
    )
    return await tx_scaffold(ctx, uid, body, "p23c_set_destination")


async def tx_shelf(ctx: Ctx, uid: str) -> Any:
    body = "\n".join(
        [
            "_result = {_v(r.name): sorted(_v(c.name) for c in r.clips) for r in _bg.shelf_reels}",
        ]
    )
    return await tx_scaffold(ctx, uid, body, "p23c_shelf")


def local_list(path: Path) -> list[str] | None:
    return sorted(os.listdir(path)) if path.is_dir() else None


def frames_of(names: list[str] | None, prefix: str) -> list[str]:
    """The numbered frames `<prefix><digits>.exr` (the real Flame's names: no dot before the
    number); the `.clip` is not one."""
    return [n for n in names or [] if re.fullmatch(rf"{re.escape(prefix)}\d+\.exr", n)]


def target_of(ctx: Ctx, group: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": ctx.ids["groups"][group]["uid"]}


def uid_of(ctx: Ctx, group: str) -> str:
    return str(ctx.ids["groups"][group]["uid"])


def node_sel(ctx: Ctx, group: str, name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": target_of(ctx, group), "name": name}


def out_dir(ctx: Ctx, group: str) -> Path:
    return Path(str(ctx.ids["filesDir"])) / f"render_{group.lower()}"


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


def journal_records(ctx: Ctx) -> list[dict[str, Any]]:
    return Journal(ctx.journal_dir).read_all().records


def journal_group_count(ctx: Ctx) -> int:
    return len(journal_index(ctx).groups)


def render_groups(ctx: Ctx) -> list[str]:
    """Journal groups whose intents are render intents."""
    index = journal_index(ctx)
    return [
        g
        for g, views in index.groups.items()
        if any(v.intent.get("kind") == "render" for v in views)
    ]


def value_like(current: Any, wanted: Any) -> Any:
    """`wanted` in the type of the value the attribute holds now (an integer attribute refuses a
    float)."""
    if isinstance(current, bool):
        return bool(wanted)
    if isinstance(current, int):
        return int(wanted)
    if isinstance(current, float):
        return float(wanted)
    return str(wanted)


async def prepare(ctx: Ctx) -> None:
    ctx.ids["filesDir"] = str(OUTPUT_DIR / f"files_p23c_{ctx.ts}")
    for group in GROUPS:
        out_dir(ctx, group).mkdir(parents=True, exist_ok=True)
    lib_name = f"P23C_LIB_{ctx.ts}"
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
        label="p23c_prepare_library",
        timeout_s=80.0,
    )
    rec(ctx, "prepare_library_clips", lib.ok, resp=_br_dict(lib))
    if lib.ok and isinstance(lib.result, dict):
        ctx.ids["libClips"] = lib.result["clips"]

    lines = [
        "desktop = flame.projects.current_project.current_workspace.desktop",
        f"reel_group = desktop.create_reel_group('P23C_RG_{ctx.ts}')",
        "groups = {}",
    ]
    for group in GROUPS:
        lines += [
            f"bg = desktop.create_batch_group('P23C_{group}_{ctx.ts}', "
            "nb_reels=1, nb_shelf_reels=1)",
            f"groups[{group!r}] = {{'uid': _v(bg.uid), 'name': _v(bg.name)}}",
        ]
    lines += [
        # created last: it becomes the current group, so none of the test groups is current
        f"decoy = desktop.create_batch_group('P23C_DECOY_{ctx.ts}', nb_reels=1, nb_shelf_reels=1)",
        "_result = {'groups': groups, 'decoyUid': _v(decoy.uid), 'reelGroup': _v(reel_group.name)}",
    ]
    br = await tx(ctx, "\n".join(lines), label="p23c_prepare_groups", timeout_s=90.0)
    rec(ctx, "prepare_groups_and_decoy", br.ok, resp=_br_dict(br))
    if not (br.ok and isinstance(br.result, dict)):
        return
    ctx.ids.update(
        groups=br.result["groups"],
        decoyUid=br.result["decoyUid"],
        reelGroup=br.result["reelGroup"],
    )
    flags = [await tx_is_current(ctx, g["uid"]) for g in br.result["groups"].values()]
    rec(
        ctx,
        "prepare_no_test_group_is_current",
        all(flag is False for flag in flags)
        and await tx_is_current(ctx, br.result["decoyUid"]) is True,
        flags=flags,
    )


async def build_group(ctx: Ctx, session: Any, group: str, *, bypass: bool = False) -> None:
    """Clip node (a library clip through batch.add_clips) -> Write File -> media_path / range."""
    target = target_of(ctx, group)
    writer = WRITERS[group]
    info = _result_of(await do(session, ctx, "batch.info", {"target": target}))
    reel = (info.get("reels") or [{}])[0].get("name")
    lib_clip = ctx.ids["libClips"][0][1]
    added = await do(
        session,
        ctx,
        "batch.add_clips",
        {"target": target, "reel": reel, "clips": [{"kind": "wiretap", "id": lib_clip}]},
    )
    listed = _result_of(await do(session, ctx, "node.list", {"target": target}))
    clip_nodes = [n["name"] for n in listed.get("items", []) if n.get("type") == "Clip"]
    created = await do(
        session, ctx, "node.create", {"target": target, "type": "Write File", "name": writer}
    )
    connected = await do(
        session,
        ctx,
        "node.connect",
        {"source": node_sel(ctx, group, clip_nodes[0]), "target": node_sel(ctx, group, writer)}
        if clip_nodes
        else {"source": target, "target": target},
    )
    keys = ["media_path", "range_start", "range_end", "bypass"]
    current = _result_of(
        await do(
            session,
            ctx,
            "node.get_attributes",
            {"target": node_sel(ctx, group, writer), "keys": keys},
        )
    ).get("attributes", {})
    now = {k: (current.get(k) or {}).get("value") for k in keys}
    values: dict[str, Any] = {}
    if group != "C":  # C keeps the default media_path on purpose
        values["media_path"] = value_like(now["media_path"], str(out_dir(ctx, group)) + "/")
    first, last = RANGES[group]
    if first is not None and last is not None:
        values["range_start"] = value_like(now["range_start"], first)
        values["range_end"] = value_like(now["range_end"], last)
    if bypass:
        values["bypass"] = value_like(now["bypass"], True)
    written = (
        await do(
            session,
            ctx,
            "node.set_attributes",
            {"target": node_sel(ctx, group, writer), "values": values},
        )
        if values
        else {"ok": True}
    )
    rec(
        ctx,
        f"prepare_{group.lower()}_built_with_the_ops",
        all(e.get("ok") is True for e in (added, created, connected, written))
        and len(clip_nodes) == 1,
        clipNodes=clip_nodes,
        currentTypes={k: type(v).__name__ for k, v in now.items()},
        written=values,
        errors=[e.get("error") for e in (added, created, connected, written) if not e.get("ok")],
    )


async def build_render_group(ctx: Ctx, session: Any, group: str = RENDER_GROUP) -> None:
    """Clip node (a library clip through batch.add_clips) -> Render node, range RENDER_RANGE."""
    target = target_of(ctx, group)
    info = _result_of(await do(session, ctx, "batch.info", {"target": target}))
    reel = (info.get("reels") or [{}])[0].get("name")
    lib_clip = ctx.ids["libClips"][0][1]
    added = await do(
        session,
        ctx,
        "batch.add_clips",
        {"target": target, "reel": reel, "clips": [{"kind": "wiretap", "id": lib_clip}]},
    )
    listed = _result_of(await do(session, ctx, "node.list", {"target": target}))
    clip_nodes = [n["name"] for n in listed.get("items", []) if n.get("type") == "Clip"]
    created = await do(
        session, ctx, "node.create", {"target": target, "type": "Render", "name": RENDER_NODE}
    )
    connected = await do(
        session,
        ctx,
        "node.connect",
        {"source": node_sel(ctx, group, clip_nodes[0]), "target": node_sel(ctx, group, RENDER_NODE)}
        if clip_nodes
        else {"source": target, "target": target},
    )
    keys = ["range_start", "range_end"]
    current = _result_of(
        await do(
            session,
            ctx,
            "node.get_attributes",
            {"target": node_sel(ctx, group, RENDER_NODE), "keys": keys},
        )
    ).get("attributes", {})
    now = {k: (current.get(k) or {}).get("value") for k in keys}
    values = {
        "range_start": value_like(now["range_start"], RENDER_RANGE[0]),
        "range_end": value_like(now["range_end"], RENDER_RANGE[1]),
    }
    written = await do(
        session,
        ctx,
        "node.set_attributes",
        {"target": node_sel(ctx, group, RENDER_NODE), "values": values},
    )
    rec(
        ctx,
        "prepare_g_built_with_the_ops",
        all(e.get("ok") is True for e in (added, created, connected, written))
        and len(clip_nodes) == 1,
        clipNodes=clip_nodes,
        written=values,
        errors=[e.get("error") for e in (added, created, connected, written) if not e.get("ok")],
    )


async def build_unwired_group(ctx: Ctx, session: Any, group: str = UNWIRED_GROUP) -> None:
    """Clip node -> Render node UNWIRED_WIRED_NODE, plus Render node UNWIRED_OPEN_NODE with no
    input connected; both with range UNWIRED_RANGE."""
    target = target_of(ctx, group)
    info = _result_of(await do(session, ctx, "batch.info", {"target": target}))
    reel = (info.get("reels") or [{}])[0].get("name")
    lib_clip = ctx.ids["libClips"][0][1]
    added = await do(
        session,
        ctx,
        "batch.add_clips",
        {"target": target, "reel": reel, "clips": [{"kind": "wiretap", "id": lib_clip}]},
    )
    listed = _result_of(await do(session, ctx, "node.list", {"target": target}))
    clip_nodes = [n["name"] for n in listed.get("items", []) if n.get("type") == "Clip"]
    ctx.ids["unwiredClipNode"] = clip_nodes[0] if clip_nodes else None
    envs = [added]
    for name in (UNWIRED_WIRED_NODE, UNWIRED_OPEN_NODE):
        envs.append(
            await do(
                session, ctx, "node.create", {"target": target, "type": "Render", "name": name}
            )
        )
    envs.append(
        await do(
            session,
            ctx,
            "node.connect",
            {
                "source": node_sel(ctx, group, clip_nodes[0]),
                "target": node_sel(ctx, group, UNWIRED_WIRED_NODE),
            }
            if clip_nodes
            else {"source": target, "target": target},
        )
    )
    keys = ["range_start", "range_end"]
    values_written: dict[str, Any] = {}
    for name in (UNWIRED_WIRED_NODE, UNWIRED_OPEN_NODE):
        current = _result_of(
            await do(
                session,
                ctx,
                "node.get_attributes",
                {"target": node_sel(ctx, group, name), "keys": keys},
            )
        ).get("attributes", {})
        now = {k: (current.get(k) or {}).get("value") for k in keys}
        values = {
            "range_start": value_like(now["range_start"], UNWIRED_RANGE[0]),
            "range_end": value_like(now["range_end"], UNWIRED_RANGE[1]),
        }
        values_written[name] = values
        envs.append(
            await do(
                session,
                ctx,
                "node.set_attributes",
                {"target": node_sel(ctx, group, name), "values": values},
            )
        )
    rec(
        ctx,
        "prepare_i_built_with_the_ops",
        all(e.get("ok") is True for e in envs) and len(clip_nodes) == 1,
        clipNodes=clip_nodes,
        written=values_written,
        errors=[e.get("error") for e in envs if not e.get("ok")],
    )


async def prepare_build(ctx: Ctx) -> None:
    async with server_session(ctx, "prepare-build") as session:
        for group in GROUPS:
            await build_group(ctx, session, group, bypass=group == "E")
        for group in GROUPS:
            resolved = await tx_resolved(ctx, uid_of(ctx, group), WRITERS[group])
            ctx.ids.setdefault("resolved", {})[group] = resolved
            observe(ctx, f"prepare_{group.lower()}_resolved_path", resolved=resolved)


async def stage_a(ctx: Ctx) -> None:
    group, writer = "A", WRITERS["A"]
    uid, target = uid_of(ctx, group), target_of(ctx, group)
    directory = out_dir(ctx, group)
    async with server_session(ctx, "a-plain") as session:
        env = await do(session, ctx, "render.plan", {"target": target})
        plan = _result_of(env)
        node = next((n for n in plan.get("nodes", []) if n.get("name") == writer), {})
        via_tx = await tx_resolved(ctx, uid, writer)
        rec(
            ctx,
            "a_plan_has_the_write_file_effective_with_8_frames",
            env.get("ok") is True
            and node.get("kind") == "write_file"
            and node.get("effective") is True
            and node.get("plannedFrames") == 8
            and node.get("plannedFiles") == 8
            and node.get("rangeStart") == 1
            and node.get("rangeEnd") == 8
            and node.get("defaultPath") is False,
            node=node,
        )
        rec(
            ctx,
            "a_plan_resolved_path_equals_the_transport_read",
            isinstance(via_tx, dict)
            and node.get("resolvedMediaPath") == via_tx.get("resolved")
            and node.get("mediaPath") == via_tx.get("mediaPath"),
            plan=node.get("resolvedMediaPath"),
            transport=via_tx,
        )
        rec(
            ctx,
            "a_plan_says_the_directory_is_there_and_nothing_would_be_overwritten",
            node.get("dirExists") is True
            and node.get("existingPlannedFiles") == 0
            and node.get("outputDir") == str(directory)
            and (plan.get("summary") or {}).get("wouldRefuse") == [],
            outputDir=node.get("outputDir"),
            wouldRefuse=(plan.get("summary") or {}).get("wouldRefuse"),
        )
        rec(
            ctx,
            "a_plan_wrote_nothing_and_journaled_nothing",
            local_list(directory) == [] and not render_groups(ctx),
            listing=local_list(directory),
        )
        observe(
            ctx,
            "a_plan_reels_and_output_text",
            reels=plan.get("reels"),
            summary=plan.get("summary"),
            keys=sorted(node),
        )

        refused = await do(
            session,
            ctx,
            "plan.run",
            {
                "steps": [
                    {"op": "render.plan", "args": {"target": target}},
                    {"op": "render.batch", "args": {"target": target}},
                ]
            },
        )
        rec(
            ctx,
            "a_plan_run_refuses_render_batch_and_runs_nothing",
            refused.get("ok") is False
            and refused.get("errorCode") == "INVALID_ARGS"
            and "cannot be undone" in str(refused.get("result"))
            and local_list(directory) == []
            and not render_groups(ctx),
            error=refused.get("error"),
            issues=_result_of(refused).get("issues"),
        )


async def stage_b(ctx: Ctx) -> None:
    group, writer = "A", WRITERS["A"]
    uid, target = uid_of(ctx, group), target_of(ctx, group)
    directory = out_dir(ctx, group)
    shelf_before = await tx_shelf(ctx, uid)
    async with server_session(ctx, "b-plain") as session:
        started = time.monotonic()
        env = await do(session, ctx, "render.batch", {"target": target})
        wall = round(time.monotonic() - started, 2)
        result = _result_of(env)
        names = local_list(directory)
        via_flame = await tx_list_dir(ctx, str(directory))
        frames = frames_of(names, writer)
        expected = [f"{writer}{n:06d}.exr" for n in range(1, 9)]
        rec(
            ctx,
            "b_render_ok_and_8_frames_are_on_disk_by_this_client_and_by_flame",
            env.get("ok") is True
            and frames == expected
            and isinstance(via_flame, list)
            and sorted(n for n, _ in via_flame if n.endswith(".exr")) == expected
            and all(size > 0 for n, size in via_flame if n.endswith(".exr")),
            error=env.get("error"),
            names=names,
            viaFlame=via_flame,
        )
        rec(
            ctx,
            "b_result_counts_the_frames_against_the_plan",
            result.get("ran") is True
            and result.get("expectedFrames") == 8
            and result.get("writtenFrames") == 8
            and result.get("verified") is True
            and env.get("affected") == 8
            and env.get("undoSteps") == 0
            and (env.get("execution") or {}).get("undo") == "unsupported",
            result={k: result.get(k) for k in ("expectedFrames", "writtenFrames", "verified")},
            affected=env.get("affected"),
            execution=env.get("execution"),
        )
        rows = result.get("nodes") or [{}]
        clip_files = [n for n in names or [] if n.endswith(".clip")]
        rec(
            ctx,
            "b_other_new_files_include_the_clip_file",
            bool(clip_files) and all(c in (rows[0].get("otherNewFiles") or []) for c in clip_files),
            clipFiles=clip_files,
            otherNewFiles=rows[0].get("otherNewFiles"),
        )
        shelf_after = await tx_shelf(ctx, uid)
        gained = {
            reel: [c for c in clips if c not in (shelf_before.get(reel) or [])]
            if isinstance(shelf_before, dict)
            else None
            for reel, clips in (shelf_after.items() if isinstance(shelf_after, dict) else [])
        }
        rec(
            ctx,
            "b_a_clip_landed_in_a_shelf_reel_and_the_result_names_it",
            isinstance(shelf_after, dict)
            and any(v for v in gained.values())
            and bool(result.get("newClips")),
            gained=gained,
            newClips=result.get("newClips"),
        )
        journal = env.get("journal") or {}
        index = journal_index(ctx)
        views = index.groups.get(str(journal.get("group")), [])
        intent = views[0].intent if views else {}
        outcome = views[0].outcome if views else {}
        rec(
            ctx,
            "b_the_journal_has_the_intent_with_the_plan_and_the_outcome_landed",
            len(views) == 1
            and intent.get("kind") == "render"
            and intent.get("expectedFrames") == 8
            and (intent.get("nodes") or [{}])[0].get("name") == writer
            and (outcome or {}).get("ok") is True
            and (outcome or {}).get("verdict") == "landed"
            and index.group_state(str(journal.get("group"))) == "landed",
            group=journal.get("group"),
            state=index.group_state(str(journal.get("group"))) if journal.get("group") else None,
            intentKeys=sorted(intent),
        )
        listing = await do(session, ctx, "journal.list", {"limit": 5})
        items = _result_of(listing).get("items") or []
        rec(
            ctx,
            "b_journal_list_shows_the_render_landed",
            any(
                i.get("group") == journal.get("group") and i.get("state") == "landed" for i in items
            ),
            items=items[:3],
        )
        reverted = await do(session, ctx, "journal.revert", {"group": journal.get("group")})
        rec(
            ctx,
            "b_journal_revert_of_a_render_is_refused_and_the_frames_stay",
            reverted.get("ok") is False
            and reverted.get("errorCode") == "PRECONDITION_UNMET"
            and frames_of(local_list(directory), writer) == expected,
            error=reverted.get("error"),
        )
        first = directory / expected[0]
        observe(
            ctx,
            "b_render_observations",
            wallS=wall,
            elapsedS=result.get("elapsedS"),
            returned=result.get("returned"),
            otherNewFiles=rows[0].get("otherNewFiles"),
            firstFrameMd5=hashlib.md5(first.read_bytes()).hexdigest() if first.exists() else None,
            firstFrameSize=first.stat().st_size if first.exists() else None,
            reelsBefore=result.get("reelsBefore"),
            reelsAfter=result.get("reelsAfter"),
            degraded=env.get("degraded"),
        )
        again = await do(session, ctx, "render.plan", {"target": target})
        node = next((n for n in _result_of(again).get("nodes", []) if n["name"] == writer), {})
        rec(
            ctx,
            "b_the_plan_now_sees_the_8_frames_that_a_second_render_would_overwrite",
            node.get("existingPlannedFiles") == 8,
            existing=node.get("existingPlannedFiles"),
        )


# stages c, d, e: refused before anything is written
def var_tmp_named(prefix: str) -> list[str]:
    """Names in /var/tmp that start with `prefix`, read by THIS process (a Flame that is busy
    rendering may not answer the transport, and that must not decide this check)."""
    return sorted(n for n in os.listdir(DEFAULT_DIR) if n.startswith(prefix))


async def var_tmp_via_flame(ctx: Ctx, prefix: str) -> Any:
    code = (
        "import os\n"
        f"_result = sorted(n for n in os.listdir({DEFAULT_DIR!r}) if n.startswith({prefix!r}))"
    )
    br = await tx(ctx, code, label="p23c_var_tmp")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def stage_c(ctx: Ctx) -> None:
    group, writer = "C", WRITERS["C"]
    target = target_of(ctx, group)
    before_tmp = var_tmp_named(writer)
    groups_before = journal_group_count(ctx)
    async with server_session(ctx, "c-plain") as session:
        plan = _result_of(await do(session, ctx, "render.plan", {"target": target}))
        node = next((n for n in plan.get("nodes", []) if n["name"] == writer), {})
        rec(
            ctx,
            "c_plan_shows_the_default_path_and_says_render_would_refuse",
            node.get("defaultPath") is True
            and node.get("mediaPath") == f"{DEFAULT_DIR}/"
            and [p.get("code") for p in (plan.get("summary") or {}).get("wouldRefuse", [])]
            == ["default_path"],
            mediaPath=node.get("mediaPath"),
            resolved=node.get("resolvedMediaPath"),
            wouldRefuse=(plan.get("summary") or {}).get("wouldRefuse"),
        )
        env = await do(session, ctx, "render.batch", {"target": target})
        result = _result_of(env)
        after_tmp = var_tmp_named(writer)
        observe(
            ctx,
            "c_var_tmp_read_by_flame",
            viaFlame=await var_tmp_via_flame(ctx, writer),
            viaClient=after_tmp,
        )
        rec(
            ctx,
            "c_default_path_is_refused_before_anything_is_written",
            env.get("ok") is False
            and env.get("errorCode") == "PRECONDITION_UNMET"
            and result.get("ran") is False
            and [p.get("code") for p in result.get("problems", [])] == ["default_path"]
            and before_tmp == [] == after_tmp
            and journal_group_count(ctx) == groups_before,
            error=env.get("error"),
            varTmpBefore=before_tmp,
            varTmpAfter=after_tmp,
        )
        rec(
            ctx,
            "c_the_write_door_stayed_open",
            _result_of(await version_info(session, ctx)).get("journal", {}).get("writesBlocked")
            is False,
        )


async def stage_d(ctx: Ctx) -> None:
    group, writer = "D", WRITERS["D"]
    target, directory = target_of(ctx, group), out_dir(ctx, group)
    groups_before = journal_group_count(ctx)
    async with server_session(ctx, "d-plain") as session:
        plan = _result_of(await do(session, ctx, "render.plan", {"target": target}))
        node = next((n for n in plan.get("nodes", []) if n["name"] == writer), {})
        rec(
            ctx,
            "d_the_default_range_is_100_frames",
            node.get("rangeStart") == 1
            and node.get("rangeEnd") == 100
            and node.get("plannedFrames") == 100,
            node={k: node.get(k) for k in ("rangeStart", "rangeEnd", "plannedFrames")},
        )
        env = await do(session, ctx, "render.batch", {"target": target, "max_frames": 50})
        result = _result_of(env)
        rec(
            ctx,
            "d_more_than_max_frames_is_refused_before_anything_is_written",
            env.get("ok") is False
            and env.get("errorCode") == "PRECONDITION_UNMET"
            and [p.get("code") for p in result.get("problems", [])] == ["too_many_frames"]
            and local_list(directory) == []
            and journal_group_count(ctx) == groups_before,
            error=env.get("error"),
            listing=local_list(directory),
        )


async def stage_e(ctx: Ctx) -> None:
    group, writer = "E", WRITERS["E"]
    target, directory = target_of(ctx, group), out_dir(ctx, group)
    groups_before = journal_group_count(ctx)
    async with server_session(ctx, "e-plain") as session:
        plan = _result_of(await do(session, ctx, "render.plan", {"target": target}))
        node = next((n for n in plan.get("nodes", []) if n["name"] == writer), {})
        via_tx = await tx_resolved(ctx, uid_of(ctx, group), writer)
        rec(
            ctx,
            "e_the_plan_says_the_write_file_is_bypassed",
            node.get("bypass") is True
            and node.get("effective") is False
            and node.get("skipReason") == "bypassed"
            and isinstance(via_tx, dict)
            and via_tx.get("bypass") is True,
            node={k: node.get(k) for k in ("bypass", "effective", "skipReason")},
        )
        env = await do(session, ctx, "render.batch", {"target": target})
        result = _result_of(env)
        rec(
            ctx,
            "e_a_group_with_only_a_bypassed_write_file_is_refused",
            env.get("ok") is False
            and env.get("errorCode") == "PRECONDITION_UNMET"
            and [p.get("code") for p in result.get("problems", [])] == ["no_effective_node"]
            and local_list(directory) == []
            and journal_group_count(ctx) == groups_before,
            error=env.get("error"),
            listing=local_list(directory),
        )


async def stage_f(ctx: Ctx) -> None:
    group, writer = "F", WRITERS["F"]
    uid, target, directory = uid_of(ctx, group), target_of(ctx, group), out_dir(ctx, group)
    note_target = node_sel(ctx, group, writer)
    async with server_session(ctx, "f-timeout") as session:
        note_read = _result_of(
            await do(session, ctx, "node.get_attributes", {"target": note_target, "keys": ["note"]})
        )
        note_before = ((note_read.get("attributes") or {}).get("note") or {}).get("value")
        rec(
            ctx,
            "f_the_note_attribute_is_readable_as_text",
            isinstance(note_before, str),
            note=note_read.get("attributes"),
        )
        if not isinstance(note_before, str):
            return
        started = time.monotonic()
        env = await do(session, ctx, "render.batch", {"target": target, "timeout_s": 1})
        wall = round(time.monotonic() - started, 2)
        timed_out = env.get("errorCode") in ("TIMEOUT", "BRIDGE_LOST")
        observe(
            ctx,
            "f_render_with_timeout_1_second",
            ok=env.get("ok"),
            errorCode=env.get("errorCode"),
            error=env.get("error"),
            pending=env.get("pending"),
            endedWith=(
                "pending" if env.get("pending") else env.get("errorCode") or "finished in time"
            ),
            timeoutPhase=env.get("timeoutPhase"),
            wallS=wall,
            durationMs=env.get("durationMs"),
            elapsedS=_result_of(env).get("elapsedS"),
            writtenFrames=_result_of(env).get("writtenFrames"),
            timedOut=timed_out,
        )
        version = _result_of(await version_info(session, ctx))
        group_id = (env.get("journal") or {}).get("group")
        intent_record = next(
            (
                r
                for r in journal_records(ctx)
                if r.get("group") == group_id and r.get("type") == "intent"
            ),
            {},
        )
        client_handle = intent_record.get("handle")
        rec(
            ctx,
            "f_the_intent_carries_the_handle_the_server_chose",
            isinstance(client_handle, str)
            and len(client_handle) == 32
            and intent_record.get("handleSource") == "client",
            handle=client_handle,
            handleSource=intent_record.get("handleSource"),
        )
        if not timed_out:
            kept = Path(ctx.run_dir) / "results" / f"{client_handle}.json"
            rec(
                ctx,
                "f_finished_in_time_the_answer_is_complete_and_the_door_stays_open",
                env.get("ok") is True
                and _result_of(env).get("writtenFrames") == 24
                and version.get("journal", {}).get("writesBlocked") is False,
                writtenFrames=_result_of(env).get("writtenFrames"),
            )
            rec(
                ctx,
                "f_finished_in_time_the_result_file_kept_under_the_handle_was_removed",
                isinstance(client_handle, str) and not kept.exists(),
                path=str(kept),
                degraded=env.get("degraded"),
            )
            return
        rec(
            ctx,
            "f_a_timeout_left_the_intent_open_and_the_server_refuses_writes",
            version.get("journal", {}).get("writesBlocked") is True
            and version.get("journal", {}).get("unresolvedIntents", 0) >= 1
            and group_id in journal_index(ctx).groups
            and journal_index(ctx).groups[group_id][0].outcome is None,
            journal=version.get("journal"),
        )
        denied = await do(
            session, ctx, "node.set_attributes", {"target": note_target, "values": {"note": "p23c"}}
        )
        again = await do(session, ctx, "render.batch", {"target": target})
        rec(
            ctx,
            "f_writes_and_a_second_render_are_refused_while_it_is_unresolved",
            denied.get("errorCode") == "POLICY_DENIED"
            and again.get("errorCode") == "POLICY_DENIED",
            denied=denied.get("errorCode"),
            again=again.get("errorCode"),
        )
        attempts: list[dict[str, Any]] = []
        deadline = time.monotonic() + CHECK_RETRY_TOTAL_S
        while True:
            checked = await do(session, ctx, "journal.check", {"group": group_id})
            entries = _result_of(checked).get("checked") or [{}]
            entry = entries[0]
            attempts.append(
                {
                    "at": time.strftime("%H:%M:%S"),
                    "ok": checked.get("ok"),
                    "errorCode": checked.get("errorCode"),
                    "verdict": entry.get("verdict"),
                    "basis": entry.get("basis"),
                    "detail": entry.get("detail"),
                }
            )
            if entry.get("verdict") not in (None, "unknown") or time.monotonic() >= deadline:
                break
            await anyio.sleep(CHECK_RETRY_EVERY_S)
        observe(ctx, "f_check_attempts", count=len(attempts), attempts=attempts)
        names = local_list(directory)
        written = len(frames_of(names, writer))
        expected_verdict = "landed" if written == 24 else "partial" if written else "not_landed"
        observe(
            ctx,
            "f_check_verdict",
            verdict=entry.get("verdict"),
            perKey=entry.get("perKey"),
            current=entry.get("current"),
            detail=entry.get("detail"),
            framesOnDisk=written,
            clipFiles=[n for n in names or [] if n.endswith(".clip")],
        )
        rec(
            ctx,
            "f_check_verdict_agrees_with_the_files_on_disk",
            checked.get("ok") is True and entry.get("verdict") == expected_verdict,
            verdict=entry.get("verdict"),
            expected=expected_verdict,
            framesOnDisk=written,
        )
        pending_handle = (env.get("pending") or {}).get("handle")
        records = [r for r in journal_records(ctx) if r.get("group") == group_id]
        journal_pending = next((r for r in records if r.get("type") == "pending"), {})
        journal_polled = next((r for r in records if r.get("type") == "polled"), {})
        journal_check = next((r for r in reversed(records) if r.get("type") == "check"), {})
        rec(
            ctx,
            "f_the_verdict_came_from_the_bridges_answer_for_the_handle_not_from_the_files",
            isinstance(client_handle, str)
            and (pending_handle is None or pending_handle == client_handle)
            and (pending_handle is None or journal_pending.get("handle") == pending_handle)
            and entry.get("basis") == "handle"
            and journal_check.get("basis") == "handle"
            and journal_polled.get("handle") == client_handle
            and (journal_polled.get("flame") or {}).get("returned") is True
            and entry.get("verdict") == "landed"
            and written == 24,
            clientHandle=client_handle,
            pendingHandle=pending_handle,
            endedWith="pending" if pending_handle else env.get("errorCode"),
            basis=entry.get("basis"),
            journalPending=journal_pending,
            polledFlame=journal_polled.get("flame"),
            renderOutcome=entry.get("renderOutcome"),
        )
        observe(
            ctx,
            "f_what_the_bridge_answered_for_the_handle",
            returned=(journal_polled.get("flame") or {}).get("returned"),
            elapsedS=(journal_polled.get("flame") or {}).get("elapsedS"),
            newClips=(journal_polled.get("flame") or {}).get("newClips"),
            queuedMs=journal_polled.get("queuedMs"),
            runMs=journal_polled.get("runMs"),
            timeoutS=journal_pending.get("timeoutS"),
        )
        rec(
            ctx,
            "f_the_check_lifted_the_refusal_and_the_journal_state_follows_the_verdict",
            _result_of(checked).get("writesBlocked") is False
            and journal_index(ctx).group_state(group_id) == entry.get("verdict"),
            state=journal_index(ctx).group_state(group_id),
        )
        resumed = await do(
            session, ctx, "node.set_attributes", {"target": note_target, "values": {"note": "p23c"}}
        )
        rec(
            ctx,
            "f_writes_resume_after_the_check",
            resumed.get("ok") is True,
            error=resumed.get("error"),
        )
        put_back = await do(
            session,
            ctx,
            "node.set_attributes",
            {"target": note_target, "values": {"note": str(note_before)}},
        )
        rec(ctx, "f_the_note_is_put_back", put_back.get("ok") is True, error=put_back.get("error"))
        plan = _result_of(await do(session, ctx, "render.plan", {"target": target}))
        node = next((n for n in plan.get("nodes", []) if n["name"] == writer), {})
        via_flame = await tx_list_dir(ctx, str(directory))
        observe(
            ctx,
            "f_after_the_check",
            existingPlannedFiles=node.get("existingPlannedFiles"),
            framesViaFlame=len(frames_of([n for n, _ in via_flame], writer))
            if isinstance(via_flame, list)
            else via_flame,
            uid=uid,
        )


async def create_own_batch_group(ctx: Ctx, group: str) -> bool:
    """A new Batch group `P23C_<group>_<ts>` made through transport: it becomes the current
    group. Its uid and name go into `ctx.ids["groups"][group]`."""
    code = "\n".join(
        [
            "desktop = flame.projects.current_project.current_workspace.desktop",
            f"bg = desktop.create_batch_group('P23C_{group}_{ctx.ts}', "
            "nb_reels=1, nb_shelf_reels=1)",
            "_result = {'uid': _v(bg.uid), 'name': _v(bg.name)}",
        ]
    )
    br = await tx(ctx, code, label="p23c_stage_g_group", timeout_s=60.0)
    created = br.ok and isinstance(br.result, dict)
    if created:
        ctx.ids["groups"][group] = br.result
    rec(ctx, "g_own_batch_group_created", created, resp=_br_dict(br))
    return bool(created)


async def stage_g(ctx: Ctx) -> None:
    group = RENDER_GROUP
    if not await create_own_batch_group(ctx, group):
        return
    uid, target = uid_of(ctx, group), target_of(ctx, group)
    rec(
        ctx,
        "g_own_batch_group_is_the_current_one",
        await tx_is_current(ctx, uid) is True,
    )
    async with server_session(ctx, "g-build") as build_session:
        await build_render_group(ctx, build_session, group)
    reel_group = str(ctx.ids["reelGroup"])
    wanted = ["Reel Groups", reel_group]
    set_to = await tx_set_destination(ctx, uid, RENDER_NODE, wanted)
    now = set_to.get("now") if isinstance(set_to, dict) else None
    names_a_reel = (
        isinstance(now, list) and len(now) == 3 and now[:2] == wanted and isinstance(now[2], str)
    )
    rec(
        ctx,
        "g_destination_is_the_desktop_reel_group_by_transport_read_back",
        now == wanted or names_a_reel,
        wanted=wanted,
        set=set_to,
    )
    ctx.ids["renderDestination"] = now
    reel_name = now[2] if names_a_reel else DESKTOP_REEL
    row_name = f"{reel_group}/{reel_name}"
    before = await tx_desktop_reel(ctx, reel_group, reel_name)
    async with server_session(ctx, "g-plain") as session:
        plan = _result_of(await do(session, ctx, "render.plan", {"target": target}))
        node = next((n for n in plan.get("nodes", []) if n.get("name") == RENDER_NODE), {})
        dest = node.get("destinationReel") or {}
        rec(
            ctx,
            "g_plan_destination_reel_agrees_with_the_transport_read_before_the_render",
            isinstance(before, dict)
            and before.get("groupFound") is True
            and node.get("destination") == now
            and node.get("effective") is True
            and dest.get("kind") == "desktop"
            and dest.get("group") == reel_group
            and dest.get("reel") == reel_name
            and dest.get("exists") is before.get("reelFound")
            and dest.get("clipCount") == len(before.get("clips") or [])
            and plan.get("destinationUnsupported") == [],
            destinationReel=dest,
            transport=before,
        )
        env = await do(session, ctx, "render.batch", {"target": target})
        result = _result_of(env)
        after = await tx_desktop_reel(ctx, reel_group, reel_name)
        rows_before = [r for r in result.get("reelsBefore") or [] if r.get("kind") == "desktop"]
        rows_after = [r for r in result.get("reelsAfter") or [] if r.get("kind") == "desktop"]
        gained = (
            len(after.get("clips") or []) - len(before.get("clips") or [])
            if isinstance(after, dict) and isinstance(before, dict)
            else None
        )
        rec(
            ctx,
            "g_render_is_ok",
            env.get("ok") is True and result.get("ran") is True,
            error=env.get("error"),
            errorCode=env.get("errorCode"),
        )
        rec(
            ctx,
            "g_new_clips_names_the_clip_in_the_desktop_reel",
            result.get("newClips")
            == [{"kind": "desktop", "name": row_name, "clips": [RENDER_NODE]}]
            and result.get("destinationUnsupported") == [],
            newClips=result.get("newClips"),
        )
        rec(
            ctx,
            "g_the_desktop_reel_gained_exactly_one_clip_by_transport_read",
            gained == 1
            and isinstance(after, dict)
            and after.get("reelFound") is True
            and (after.get("clips") or []).count(RENDER_NODE)
            == (before.get("clips") or []).count(RENDER_NODE) + 1,
            before=before,
            after=after,
        )
        rec(
            ctx,
            "g_reels_before_and_after_carry_the_desktop_row",
            [(r.get("name"), r.get("clipCount")) for r in rows_before]
            == [(row_name, len(before.get("clips") or []))]
            and [(r.get("name"), r.get("clipCount")) for r in rows_after]
            == [(row_name, len(after.get("clips") or []))],
            rowsBefore=rows_before,
            rowsAfter=rows_after,
        )
        plan_after = _result_of(await do(session, ctx, "render.plan", {"target": target}))
        node_after = next(
            (n for n in plan_after.get("nodes", []) if n.get("name") == RENDER_NODE), {}
        )
        dest_after = node_after.get("destinationReel") or {}
        rec(
            ctx,
            "g_plan_after_the_render_says_the_reel_exists_with_one_more_clip",
            dest_after.get("exists") is True
            and dest_after.get("clipCount") == len(after.get("clips") or []),
            destinationReel=dest_after,
        )
        setup_dir = Path(str(ctx.ids["filesDir"])) / "render_g_setup"
        setup_dir.mkdir(parents=True, exist_ok=True)
        saved = await do(
            session,
            ctx,
            "node.save_setup",
            {"target": node_sel(ctx, group, RENDER_NODE), "path": str(setup_dir / "rnd")},
        )
        saved_result = _result_of(saved)
        rec(
            ctx,
            "g_node_save_setup_of_the_render_node_is_ok_and_its_path_ends_in_output_node",
            saved.get("ok") is True
            and str(saved_result.get("path")).endswith(".output_node")
            and sorted(Path(str(f)).suffix for f in saved_result.get("files") or [])
            == RENDER_SETUP_SUFFIXES,
            error=saved.get("error"),
            errorCode=saved.get("errorCode"),
            path=saved_result.get("path"),
            files=saved_result.get("files"),
        )
        observe(
            ctx,
            "g_what_the_render_did",
            returned=result.get("returned"),
            elapsedS=result.get("elapsedS"),
            reelsOfTheGroupBefore=(before or {}).get("reels")
            if isinstance(before, dict)
            else before,
            reelsOfTheGroupAfter=(after or {}).get("reels") if isinstance(after, dict) else after,
            degraded=env.get("degraded"),
        )


async def tx_reel_counts(ctx: Ctx, uid: str) -> Any:
    """Clip counts of every schematic reel and shelf reel of the group, read from Flame."""
    body = "\n".join(
        [
            "_result = {'reels': {_v(r.name): len(r.clips) for r in _bg.reels},",
            "           'shelf': {_v(r.name): len(r.clips) for r in _bg.shelf_reels}}",
        ]
    )
    return await tx_scaffold(ctx, uid, body, "p23c_reel_counts")


def unwired_problems(plan: dict[str, Any]) -> list[dict[str, Any]]:
    summary = plan.get("summary") or {}
    return [p for p in summary.get("wouldRefuse") or [] if p.get("code") == "render_node_unwired"]


async def stage_i(ctx: Ctx) -> None:
    group = UNWIRED_GROUP
    if not await create_own_batch_group(ctx, group):
        return
    uid, target = uid_of(ctx, group), target_of(ctx, group)
    async with server_session(ctx, "i-build") as build_session:
        await build_unwired_group(ctx, build_session, group)
    groups_before = journal_group_count(ctx)
    counts_before = await tx_reel_counts(ctx, uid)
    async with server_session(ctx, "i-plain") as session:
        plan_env = await do(session, ctx, "render.plan", {"target": target})
        plan = _result_of(plan_env)
        row = next((n for n in plan.get("nodes", []) if n.get("name") == UNWIRED_OPEN_NODE), {})
        wired_row = next(
            (n for n in plan.get("nodes", []) if n.get("name") == UNWIRED_WIRED_NODE), {}
        )
        found = unwired_problems(plan)
        plan_ok = (
            plan_env.get("ok") is True
            and row.get("wiredInputs") == 0
            and row.get("effective") is False
            and wired_row.get("effective") is True
            and [p.get("node") for p in found] == [UNWIRED_OPEN_NODE]
        )
        rec(
            ctx,
            "i_plan_lists_the_unwired_render_node_as_would_refuse",
            plan_ok,
            error=plan_env.get("error"),
            openRow={k: row.get(k) for k in ("kind", "bypass", "wiredInputs", "effective")},
            wiredRow={k: wired_row.get(k) for k in ("kind", "bypass", "wiredInputs", "effective")},
            wouldRefuse=(plan.get("summary") or {}).get("wouldRefuse"),
        )
        if not plan_ok:
            # the plan does not show the refusal: render.batch could really render, so it is not
            # called
            rec(ctx, "i_render_batch_skipped_because_the_plan_check_failed", False)
            return
        env = await do(session, ctx, "render.batch", {"target": target})
        result = _result_of(env)
        problems = result.get("problems") or []
        counts_after = await tx_reel_counts(ctx, uid)
        rec(
            ctx,
            "i_render_batch_is_refused_for_a_render_node_with_no_input",
            env.get("ok") is False
            and env.get("errorCode") == "PRECONDITION_UNMET"
            and result.get("ran") is False
            and [p.get("code") for p in problems] == ["render_node_unwired"]
            and [p.get("node") for p in problems] == [UNWIRED_OPEN_NODE],
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            ran=result.get("ran"),
            problems=problems,
        )
        rec(
            ctx,
            "i_the_refused_render_journaled_nothing_and_rendered_nothing",
            journal_group_count(ctx) == groups_before
            and isinstance(counts_before, dict)
            and counts_after == counts_before,
            journalGroupsBefore=groups_before,
            journalGroupsAfter=journal_group_count(ctx),
            reelsBefore=counts_before,
            reelsAfter=counts_after,
        )
        clip_node = ctx.ids.get("unwiredClipNode")
        connected = await do(
            session,
            ctx,
            "node.connect",
            {
                "source": node_sel(ctx, group, str(clip_node)),
                "target": node_sel(ctx, group, UNWIRED_OPEN_NODE),
            },
        )
        plan_after = _result_of(await do(session, ctx, "render.plan", {"target": target}))
        row_after = next(
            (n for n in plan_after.get("nodes", []) if n.get("name") == UNWIRED_OPEN_NODE), {}
        )
        rec(
            ctx,
            "i_plan_no_longer_lists_the_node_once_it_is_wired",
            connected.get("ok") is True
            and unwired_problems(plan_after) == []
            and row_after.get("wiredInputs") == 1
            and row_after.get("effective") is True,
            error=connected.get("error"),
            row={k: row_after.get(k) for k in ("wiredInputs", "effective")},
            wouldRefuse=(plan_after.get("summary") or {}).get("wouldRefuse"),
        )


async def list_outputs(
    session: Any, ctx: Ctx, wanted: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Page through batch.list(outputs=true) until every uid in `wanted` (group -> uid) is found
    or the pages run out. Returns the items by group and what the paging did."""
    found: dict[str, dict[str, Any]] = {}
    by_uid = {uid: group for group, uid in wanted.items()}
    stopped: list[dict[str, Any]] = []
    errors: list[Any] = []
    pages, offset, total = 0, 0, None
    started = time.monotonic()
    while pages < LIST_MAX_PAGES and len(found) < len(wanted) and (total is None or offset < total):
        env = await do(
            session, ctx, "batch.list", {"outputs": True, "limit": LIST_PAGE, "offset": offset}
        )
        pages += 1
        res = _result_of(env)
        items = res.get("items") or []
        if env.get("ok") is not True or not items:
            errors.append(env.get("error") or "an empty page")
            break
        total = res.get("total")
        for item in items:
            if item.get("uid") in by_uid:
                found[by_uid[item["uid"]]] = item
        early = res.get("stoppedEarly")
        if early:
            stopped.append({"offset": offset, **early})
        offset += early["read"] if early else len(items)
    facts = {
        "pages": pages,
        "total": total,
        "stoppedEarly": stopped,
        "elapsedS": round(time.monotonic() - started, 1),
        "errors": errors,
    }
    return found, facts


async def stage_h(ctx: Ctx) -> None:
    groups = ctx.ids["groups"]
    wanted = {g: uid_of(ctx, g) for g in (*GROUPS, RENDER_GROUP) if g in groups}
    current_before = {g: await tx_is_current(ctx, uid) for g, uid in wanted.items()}
    async with server_session(ctx, "h-plain") as session:
        found, facts = await list_outputs(session, ctx, wanted)
    current_after = {g: await tx_is_current(ctx, uid) for g, uid in wanted.items()}
    observe(
        ctx,
        "h_batch_list_outputs_paging_and_the_current_group",
        **facts,
        found=sorted(found),
        currentBefore=current_before,
        currentAfter=current_after,
    )
    outputs = {g: item.get("outputs") for g, item in found.items()}
    observe(
        ctx,
        "h_outputs_of_the_groups_without_a_judged_expectation",
        **{g: outputs.get(g) for g in ("C", "D", "F")},
    )
    truth_a = await tx_resolved(ctx, uid_of(ctx, "A"), WRITERS["A"]) if "A" in groups else None
    truth_e = await tx_resolved(ctx, uid_of(ctx, "E"), WRITERS["E"]) if "E" in groups else None
    truth_g = (
        await tx_destination(ctx, uid_of(ctx, RENDER_GROUP), RENDER_NODE)
        if RENDER_GROUP in groups
        else None
    )
    got_a = outputs.get("A")
    rec(
        ctx,
        "h_group_a_lists_its_write_file_with_the_media_path_transport_reads",
        isinstance(truth_a, dict)
        and truth_a.get("bypass") is False
        and got_a
        == [
            {
                "name": WRITERS["A"],
                "kind": "write_file",
                "bypass": False,
                "mediaPath": truth_a.get("mediaPath"),
            }
        ],
        api=got_a,
        transport=truth_a,
        paging=facts,
    )
    got_e = outputs.get("E")
    rec(
        ctx,
        "h_group_e_lists_its_one_bypassed_write_file",
        isinstance(truth_e, dict)
        and truth_e.get("bypass") is True
        and isinstance(got_e, list)
        and len(got_e) == 1
        and got_e[0].get("kind") == "write_file"
        and got_e[0].get("name") == WRITERS["E"]
        and got_e[0].get("bypass") is True,
        api=got_e,
        transport=truth_e,
    )
    got_g = outputs.get("G")
    rec(
        ctx,
        "h_group_g_lists_its_render_node_with_the_destination_transport_reads",
        isinstance(truth_g, dict)
        and got_g
        == [
            {
                "name": RENDER_NODE,
                "kind": "render",
                "bypass": truth_g.get("bypass"),
                "destination": truth_g.get("destination"),
            }
        ]
        and isinstance(ctx.ids.get("renderDestination"), list)
        and truth_g.get("destination") == ctx.ids.get("renderDestination"),
        api=got_g,
        transport=truth_g,
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
        listing = await do(session, ctx, "journal.list", {"limit": 80})
        ctx.results["journalListing"] = _result_of(listing).get("items")
        rec(
            ctx,
            "z_nothing_was_ever_rendered_into_var_tmp_by_this_run",
            var_tmp_named("write_c") == [],
        )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def print_observations(results: Results) -> None:
    print("--- observations (nothing here is a pass/fail) ---")
    for obs in results.get("observations", []):
        name = obs.get("name")
        print(f"{name}: { {k: v for k, v in obs.items() if k not in ('name', 'stage')} }")


REEL_GROUP_PREFIX: Final[str] = "P23C_RG_"


def delete_own_reel_group(ctx: Ctx) -> list[str]:
    """Delete the Desktop reel group this run made (name starts with `P23C_RG_`), then read
    Flame again to see that it is gone. Returns the names deleted."""
    name = ctx.ids.get("reelGroup")
    if not isinstance(name, str) or not name.startswith(REEL_GROUP_PREFIX):
        return []
    find = (
        V_PRELUDE + "\n"
        "_ws = flame.projects.current_project.current_workspace\n"
        f"_g = [g for g in _ws.desktop.reel_groups if _v(g.name) == {name!r}]\n"
    )
    br = _tx(
        find + "if _g:\n    flame.delete(_g[0], confirm=False)\n_result = len(_g)",
        ctx.run_dir,
        timeout_s=40.0,
        label="p23c_cleanup_delete_reel_group",
    )
    after = _tx(find + "_result = len(_g)", ctx.run_dir, timeout_s=20.0, label="p23c_cleanup_check")
    rec(
        ctx,
        "cleanup_own_reel_group_deleted_and_gone",
        br.ok and br.result == 1 and after.ok and after.result == 0,
        reelGroup=name,
        deleted=_br_dict(br),
        stillThere=after.result if after.ok else None,
    )
    return [name] if br.ok and br.result == 1 else []


def cleanup(ctx: Ctx) -> None:
    ctx.stage = "cleanup"
    deleted = delete_own_reel_group(ctx)
    ctx.results["cleanup"] = {
        "deleted": [f"reel group {n}" for n in deleted],
        "kept": {
            "batchGroups": ctx.ids.get("groups"),
            "renderedFiles": ctx.ids.get("filesDir"),
            "checkpointFiles": str(ctx.journal_dir / "checkpoints"),
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them); rendered files are kept"
            ),
        },
    }
    deadline = time.monotonic() + CHECK_RETRY_TOTAL_S
    attempts: list[dict[str, Any]] = []
    while True:
        exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label="p23c_cleanup_exit")
        attempts.append({"at": time.strftime("%H:%M:%S"), "errorCode": exit_br.error_code})
        if exit_br.error_code != "BRIDGE_UNAVAILABLE" or time.monotonic() >= deadline:
            break
        time.sleep(CHECK_RETRY_EVERY_S)
    rec(
        ctx,
        "cleanup_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
        attempts=attempts,
    )


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "groups" in ctx.ids and "libClips" in ctx.ids:
            await guarded(ctx, "prepare_build", prepare_build)
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("e", stage_e),
                ("f", stage_f),
                ("g", stage_g),
                ("h", stage_h),
                ("i", stage_i),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_stages_a_to_i_skipped", False, ids=ctx.ids)
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
    journal_dir = OUTPUT_DIR / f"journal_p23c_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_3c_render_e2e_{ts}.json",
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
