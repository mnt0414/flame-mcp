#!/usr/bin/env python3
"""Real-Flame end-to-end: telling what became of the journal's undecided intents ACROSS A
FLAME RESTART (run with the `.venv` Python; `scripts/run_p2_4_restart_e2e.sh` starts Flame TWICE
and calls this script with `--phase 1`, then `--phase 2`).

Checks: How `journal.check` tells what became of intents left undecided across a Flame restart: a
write whose server died, and two renders whose answers were lost. Flame is started twice.

Same shape and helpers as scripts/p2_3c_render_e2e.py / p2_2a_journal_e2e.py: a real MCP stdio
client drives `python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport` is
scaffolding only (build the groups, read a value back INDEPENDENTLY of the op under test).
Everything happens in the probe project `flame_mcp_probe`, in Batch groups that are NOT current.
ONE journal directory serves both phases (`p2/journal_p24_e2e_<tag>/`, never the production one),
and the state between the phases (group uids, journal groups, handles, expected values) is a
JSON file (`p2/p2_4_restart_state_<tag>.json`) the runner passes as `--state-file`. No test hook
is set for any server except the one that must die (`FLAME_MCP_TEST_EXIT_AFTER_WRITE`, W1).

PHASE 1 (the first Flame): three intents are left without an outcome, in three different ways
  W1  node.set_attributes of pos_x (a Blur node of group G) by a server that ends right after
      the write's answer, before the outcome is journaled: the intent is alone in the journal.
  R1  render.batch of group H (Clip of p2_len48 -> Write File, range 1-48, `render_h/`) with
      timeout_s=1.
      It ends without an answer -- TIMEOUT (with a bridge pending handle, journaled as
      `pending`) or BRIDGE_LOST (no pending handle) -- and either way the intent carries the
      handle this server chose (`handleSource: "client"`), which is the handle awaited and
      checked below. Which way it
      ended is OBSERVED. journal.check is NOT called. The end of the render is awaited by
      watching `run/results/<handle>.json` appear with `stat` (it is neither read nor deleted,
      and the bridge is not asked anything: a Foreground render keeps Flame from answering).
      The 48 frames are counted by this client.
  R2  the same for group K (p2_len72, `render_k/`, range 1-72). After its result file appears the
      file is MOVED (not deleted) to `p2/moved_results_<tag>/`: the situation of "Flame was
      restarted and its result is gone". Then flame.exit().

PHASE 2 (the second Flame, same project): a NEW server reads what is left
  1  flame_version_info: unresolvedIntents == 3, writes not blocked (the design: a new server
     counts undecided intents but allows writes).
  2  journal.check(W1): Flame is read again; the verdict must equal what an independent
     transport read says (landed if pos_x holds the new value, not_landed if the old one,
     diverged if the node / group is gone). Whether Flame KEEPS a Batch change made before
     flame.exit() is NOT known beforehand: it is observed, and either answer passes as long as
     the verdict agrees with the value that is really there.
  3  journal.check(R1): if `results/<handle>.json` survived the restart the bridge hands it over
     (`basis: handle`), else the files decide (`basis: disk`). The verdict must equal the 48
     frames on disk (landed); the basis must be `handle` exactly when the file survived.
  4  journal.check(R2): the result file was moved away, so the bridge knows no such handle ->
     `basis: disk`, verdict equal to the 72 frames on disk (landed), and the detail says it is
     an ESTIMATE (the end of the render was not confirmed).
  5  a second journal.check of R1 does not poll again: it has nothing left to decide (inDoubt 0,
     checked []), the journal holds exactly one `check` (and one `polled` iff basis handle) and
     its verdict / basis are the first ones.
  6  journal.list: none of the 3 groups is `unknown`; flame_version_info: unresolvedIntents == 0.
  The moved result file is NOT put back. Then flame.exit().

JUDGED steps (`ok` decides the run) are the ones fixed by the design. OBSERVED steps
(`kind: "observation"`, always ok) record what Flame did where nothing was known yet (whether
the Batch change and the result files survived the restart, what the render's answer held).
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from collections.abc import Awaitable, Callable
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
    ENV_EXIT,
    Ctx,
    do,
    guarded,
    intents_for,
    rec,
    same,
    save,
    save_stage,
    server_session,
    state_of,
    tx,
    version_info,
    wait_pid_gone,
)
from p2_3c_render_e2e import (
    CHECK_RETRY_EVERY_S,
    CHECK_RETRY_TOTAL_S,
    frames_of,
    local_list,
    observe,
    print_observations,
    value_like,
)

from flame_mcp.journal import Journal

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
GROUPS: Final[tuple[str, ...]] = ("G", "H", "K")
WRITERS: Final[dict[str, str]] = {"H": "write_h", "K": "write_k"}
RANGES: Final[dict[str, tuple[int, int]]] = {"H": (1, 48), "K": (1, 72)}
# each render must outlast timeout_s=1 by far: on a lightly loaded probe project an 8-frame
# render answered in 0.9 s, so H / K render the 48 / 72-frame clips in full
RENDER_CLIP: Final[dict[str, int]] = {"H": 3, "K": 4}
W1_NODE: Final[str] = "p24_blur"
W1_KEY: Final[str] = "pos_x"
W1_WANTED: Final[int] = 1234
OPEN_INTENTS: Final[int] = 3
RENDER_WAIT_S: Final[float] = 120.0
RESULT_WATCH_EVERY_S: Final[float] = 0.5
RENDER_START_RETRY_S: Final[float] = 60.0
DESKTOP_READY_S: Final[float] = 60.0
BUSY_CODES: Final[frozenset[str]] = frozenset(
    {"BRIDGE_UNAVAILABLE", "TIMEOUT", "MAIN_THREAD_UNAVAILABLE"}
)
STATE_KEYS_NEEDED: Final[tuple[str, ...]] = ("w1", "r1", "r2")

Results = dict[str, Any]


# State handed from phase 1 to phase 2
def write_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_state(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def state_is_ready(ids: dict[str, Any]) -> bool:
    w1, r1, r2 = (ids.get(k) for k in STATE_KEYS_NEEDED)
    return bool(
        isinstance(w1, dict)
        and w1.get("group")
        and isinstance(r1, dict)
        and r1.get("group")
        and r1.get("handle")
        and isinstance(r2, dict)
        and r2.get("group")
        and r2.get("handle")
        and r2.get("movedTo")
    )


def flush(ctx: Ctx, state_path: Path) -> None:
    write_state(state_path, {**ctx.ids, "ready": state_is_ready(ctx.ids)})


def target_of(ctx: Ctx, key: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": ctx.ids["groups"][key]["uid"]}


def uid_of(ctx: Ctx, key: str) -> str:
    return str(ctx.ids["groups"][key]["uid"])


def node_sel(ctx: Ctx, key: str, name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": target_of(ctx, key), "name": name}


def out_dir(ctx: Ctx, key: str) -> Path:
    return Path(str(ctx.ids["filesDir"])) / f"render_{key.lower()}"


def results_dir(ctx: Ctx) -> Path:
    return Path(ctx.run_dir) / "results"


def result_file(ctx: Ctx, handle: str) -> Path:
    return results_dir(ctx) / f"{handle}.json"


def frame_names(writer: str, count: int) -> list[str]:
    return [f"{writer}{n:06d}.exr" for n in range(1, count + 1)]


def verdict_from_count(written: int, expected: int) -> str:
    if written >= expected:
        return "landed"
    return "partial" if written > 0 else "not_landed"


def expected_write_verdict(probe: Any, before: Any, after: Any) -> str | None:
    """What `journal.check` must say about W1, from an independent read of the node."""
    if not isinstance(probe, dict):
        return None
    if not probe.get("nodeFound"):
        return "diverged"
    if same(probe.get("value"), after):
        return "landed"
    if same(probe.get("value"), before):
        return "not_landed"
    return "diverged"


def journal_records_of(ctx: Ctx, group: str) -> list[dict[str, Any]]:
    return [r for r in Journal(ctx.journal_dir).read_all().records if r.get("group") == group]


def count_type(records: list[dict[str, Any]], kind: str) -> int:
    return sum(1 for r in records if r.get("type") == kind)


def stat_of(path: Path) -> dict[str, Any] | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return {"size": st.st_size, "mtime": time.strftime("%H:%M:%S", time.localtime(st.st_mtime))}


# Transport scaffolding (never through the op under test)
async def probe_pos(ctx: Ctx, uid: str, node: str) -> Any:
    """The node's pos_x, found by uid and name, read on purpose without any op. A group or a node
    that is not there is an answer (`groupFound` / `nodeFound` False), not an error."""
    code = "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            f"_gs = [g for g in _ws.desktop.batch_groups if _v(g.uid) == {uid!r}]",
            f"_ns = [n for n in _gs[0].nodes if _v(n.name) == {node!r}] if _gs else []",
            f"_val = _v(_ns[0].{W1_KEY}) if _ns else None",
            "_result = {'groupFound': bool(_gs), 'nodeFound': bool(_ns), 'value': _val,",
            "           'valueType': type(_val).__name__}",
        ]
    )
    br = await tx(ctx, code, label="p24_probe_pos", timeout_s=40.0)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_is_current(ctx: Ctx, uid: str) -> Any:
    code = (
        "_cur = _v(flame.projects.current_project.current_workspace.desktop.current_batch_group)\n"
        f"_result = _cur is not None and _v(_cur.uid) == {uid!r}"
    )
    br = await tx(ctx, code, label="p24_is_current")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def wait_for_desktop(ctx: Ctx) -> Any:
    """A freshly started Flame may open its socket before the project's desktop can be read."""
    code = "\n".join(
        [
            "_ds = flame.projects.current_project.current_workspace.desktop",
            "_uids = [_v(g.uid) for g in _ds.batch_groups]",
            "_result = {'batchGroups': len(_uids), 'uids': _uids}",
        ]
    )
    deadline = time.monotonic() + DESKTOP_READY_S
    last: Any = None
    while True:
        br = await tx(ctx, code, label="p24_desktop_ready", timeout_s=40.0)
        last = br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"
        if br.ok or time.monotonic() >= deadline:
            return last
        await anyio.sleep(CHECK_RETRY_EVERY_S)


# phase 1: prepare
async def prepare(ctx: Ctx) -> None:
    ctx.ids["filesDir"] = str(OUTPUT_DIR / f"files_p24r_{ctx.ts}")
    for key in ("H", "K"):
        out_dir(ctx, key).mkdir(parents=True, exist_ok=True)
    lib_paths = [str(MEDIA_DIR / f"clip{i}.mov") for i in (1, 2, 3)] + [
        str(MEDIA_DIR / "p2" / f"p2_len{n}.mov") for n in (48, 72)
    ]
    lib = await tx(
        ctx,
        "\n".join(
            [
                "ws = flame.projects.current_project.current_workspace",
                f"lib = ws.create_library('P24R_LIB_{ctx.ts}')",
                "reel = lib.create_reel('R1')",
                "_ids = []",
                f"for _p in {lib_paths!r}:",
                "    _c = flame.import_clips(_p, reel)[0]",
                "    _ids.append([_v(_c.name), _c.get_wiretap_node_id()])",
                "_result = {'lib': lib.get_wiretap_node_id(), 'clips': _ids}",
            ]
        ),
        label="p24_prepare_library",
        timeout_s=80.0,
    )
    rec(ctx, "prepare_library_clips", lib.ok, resp=_br_dict(lib))
    if lib.ok and isinstance(lib.result, dict):
        ctx.ids["libClips"] = lib.result["clips"]

    lines = [
        "desktop = flame.projects.current_project.current_workspace.desktop",
        "groups = {}",
    ]
    for key in GROUPS:
        lines += [
            f"bg = desktop.create_batch_group('P24R_{key}_{ctx.ts}', nb_reels=1, nb_shelf_reels=1)",
            f"groups[{key!r}] = {{'uid': _v(bg.uid), 'name': _v(bg.name)}}",
        ]
    lines += [
        # created last: it becomes the current group, so none of the test groups is current
        f"decoy = desktop.create_batch_group('P24R_DECOY_{ctx.ts}', nb_reels=1, nb_shelf_reels=1)",
        "_result = {'groups': groups, 'decoyUid': _v(decoy.uid)}",
    ]
    br = await tx(ctx, "\n".join(lines), label="p24_prepare_groups", timeout_s=90.0)
    rec(ctx, "prepare_groups_and_decoy", br.ok, resp=_br_dict(br))
    if not (br.ok and isinstance(br.result, dict)):
        return
    ctx.ids.update(groups=br.result["groups"], decoyUid=br.result["decoyUid"])
    flags = [await tx_is_current(ctx, g["uid"]) for g in br.result["groups"].values()]
    rec(
        ctx,
        "prepare_no_test_group_is_current",
        all(flag is False for flag in flags)
        and await tx_is_current(ctx, br.result["decoyUid"]) is True,
        flags=flags,
    )


async def build_writer_group(ctx: Ctx, session: Any, key: str) -> None:
    """Clip node (a library clip through batch.add_clips) -> Write File -> media_path / range."""
    target = target_of(ctx, key)
    writer = WRITERS[key]
    info = _result_of(await do(session, ctx, "batch.info", {"target": target}))
    reel = (info.get("reels") or [{}])[0].get("name")
    added = await do(
        session,
        ctx,
        "batch.add_clips",
        {
            "target": target,
            "reel": reel,
            "clips": [{"kind": "wiretap", "id": ctx.ids["libClips"][RENDER_CLIP[key]][1]}],
        },
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
        {"source": node_sel(ctx, key, clip_nodes[0]), "target": node_sel(ctx, key, writer)}
        if clip_nodes
        else {"source": target, "target": target},
    )
    keys = ["media_path", "range_start", "range_end"]
    current = _result_of(
        await do(
            session,
            ctx,
            "node.get_attributes",
            {"target": node_sel(ctx, key, writer), "keys": keys},
        )
    ).get("attributes", {})
    now = {k: (current.get(k) or {}).get("value") for k in keys}
    first, last = RANGES[key]
    values = {
        "media_path": value_like(now["media_path"], str(out_dir(ctx, key)) + "/"),
        "range_start": value_like(now["range_start"], first),
        "range_end": value_like(now["range_end"], last),
    }
    written = await do(
        session,
        ctx,
        "node.set_attributes",
        {"target": node_sel(ctx, key, writer), "values": values},
    )
    rec(
        ctx,
        f"prepare_{key.lower()}_built_with_the_ops",
        all(e.get("ok") is True for e in (added, created, connected, written))
        and len(clip_nodes) == 1,
        clipNodes=clip_nodes,
        written=values,
        errors=[e.get("error") for e in (added, created, connected, written) if not e.get("ok")],
    )


async def build_w1_node(ctx: Ctx, session: Any) -> None:
    target = target_of(ctx, "G")
    created = await do(
        session, ctx, "node.create", {"target": target, "type": "Blur", "name": W1_NODE}
    )
    sel = node_sel(ctx, "G", W1_NODE)
    read = _result_of(
        await do(session, ctx, "node.get_attributes", {"target": sel, "keys": [W1_KEY]})
    )
    before = ((read.get("attributes") or {}).get(W1_KEY) or {}).get("value")
    wanted = value_like(before, W1_WANTED if not same(before, W1_WANTED) else W1_WANTED + 1)
    ctx.ids["w1"] = {
        "node": W1_NODE,
        "target": sel,
        "key": W1_KEY,
        "before": before,
        "after": wanted,
    }
    probe = await probe_pos(ctx, uid_of(ctx, "G"), W1_NODE)
    rec(
        ctx,
        "prepare_g_blur_node_built_with_the_op",
        created.get("ok") is True
        and isinstance(probe, dict)
        and probe.get("nodeFound") is True
        and same(probe.get("value"), before)
        and not same(before, wanted),
        before=before,
        wanted=wanted,
        probe=probe,
        error=created.get("error"),
    )


async def prepare_build(ctx: Ctx) -> None:
    async with server_session(ctx, "prepare-build") as session:
        await build_w1_node(ctx, session)
        for key in ("H", "K"):
            await build_writer_group(ctx, session, key)


# phase 1: W1
async def stage_w1(ctx: Ctx) -> None:
    w1 = ctx.ids["w1"]
    uid = uid_of(ctx, "G")
    pid: int | None = None
    call_error: str | None = None
    answered: dict[str, Any] | None = None
    try:
        async with server_session(ctx, "w1-exit-after-write", hooks={ENV_EXIT: "1"}) as session:
            info = await version_info(session, ctx)
            pid = _result_of(info).get("serverPid")
            try:
                answered = await do(
                    session,
                    ctx,
                    "node.set_attributes",
                    {"target": w1["target"], "values": {W1_KEY: w1["after"]}},
                )
            except BaseException:
                call_error = traceback.format_exc()
    except BaseException:
        call_error = (call_error or "") + "\n[session exit] " + traceback.format_exc()
    gone = await wait_pid_gone(pid) if isinstance(pid, int) else False
    rec(
        ctx,
        "w1_the_server_ended_without_answering",
        gone and call_error is not None and answered is None,
        serverPid=pid,
        processGone=gone,
        answered=answered,
        callError=(call_error or "")[-600:],
    )
    probe = await probe_pos(ctx, uid, W1_NODE)
    rec(
        ctx,
        "w1_independent_read_the_write_landed_in_flame",
        isinstance(probe, dict)
        and same(probe.get("value"), w1["after"])
        and not same(probe.get("value"), w1["before"]),
        before=w1["before"],
        viaTransport=probe,
    )
    intents = intents_for(ctx, W1_NODE)
    group = intents[0]["group"] if len(intents) == 1 else None
    rec(
        ctx,
        "w1_the_journal_holds_an_intent_and_no_outcome",
        group is not None and state_of(ctx, group) == "unknown",
        intents=len(intents),
        state=state_of(ctx, group),
    )
    if group is not None:
        w1.update(group=group, intentId=intents[0]["id"], valueAfterPhase1=probe)


# phase 1: R1 / R2
async def render_start(session: Any, ctx: Ctx, key: str) -> dict[str, Any]:
    """render.batch with timeout_s=1. A refusal that happened before anything was journaled
    because Flame was still busy with the previous render is retried; a call that reached the
    journal (its envelope names a group) is never repeated."""
    deadline = time.monotonic() + RENDER_START_RETRY_S
    attempts: list[dict[str, Any]] = []
    while True:
        env = await do(
            session, ctx, "render.batch", {"target": target_of(ctx, key), "timeout_s": 1}
        )
        attempts.append(
            {
                "at": time.strftime("%H:%M:%S"),
                "errorCode": env.get("errorCode"),
                "journaled": env.get("journal") is not None,
            }
        )
        started = env.get("journal") is not None
        if started or env.get("errorCode") not in BUSY_CODES or time.monotonic() >= deadline:
            ctx.results.setdefault("renderStartAttempts", {})[key] = attempts
            return env
        await anyio.sleep(CHECK_RETRY_EVERY_S)


async def wait_for_result_file(ctx: Ctx, handle: str) -> dict[str, Any]:
    """Wait for `results/<handle>.json` to appear, by `stat` only: the file is not read (the
    bridge hands it over once, on a poll) and the bridge is not asked (a Foreground render keeps
    Flame from answering)."""
    path = result_file(ctx, handle)
    started = time.monotonic()
    while True:
        found = stat_of(path)
        waited = round(time.monotonic() - started, 1)
        if found is not None:
            return {"appeared": True, "waitedS": waited, "path": str(path), **found}
        if waited >= RENDER_WAIT_S:
            return {"appeared": False, "waitedS": waited, "path": str(path)}
        await anyio.sleep(RESULT_WATCH_EVERY_S)


async def stage_render(ctx: Ctx, key: str, *, move_result: bool) -> None:
    label = "r1" if key == "H" else "r2"
    writer = WRITERS[key]
    first, last = RANGES[key]
    expected = last - first + 1
    directory = out_dir(ctx, key)
    async with server_session(ctx, f"{label}-render-timeout-1s") as session:
        started = time.monotonic()
        env = await render_start(session, ctx, key)
        wall = round(time.monotonic() - started, 2)
        pending_handle = (env.get("pending") or {}).get("handle")
        group = (env.get("journal") or {}).get("group")
        observe(
            ctx,
            f"{label}_render_with_timeout_1_second",
            ok=env.get("ok"),
            errorCode=env.get("errorCode"),
            pending=env.get("pending"),
            endedWith="pending" if pending_handle else env.get("errorCode"),
            wallS=wall,
            durationMs=env.get("durationMs"),
        )
        rec(
            ctx,
            f"{label}_ended_without_an_answer_and_left_a_journal_group",
            env.get("errorCode") in ("TIMEOUT", "BRIDGE_LOST") and isinstance(group, str),
            errorCode=env.get("errorCode"),
            error=env.get("error"),
            pending=env.get("pending"),
        )
        if not isinstance(group, str):
            return
        records = journal_records_of(ctx, group)
        intent = next((r for r in records if r.get("type") == "intent"), {})
        handle = intent.get("handle")
        pending = next((r for r in records if r.get("type") == "pending"), {})
        rec(
            ctx,
            f"{label}_the_intent_carries_the_handle_the_server_chose",
            isinstance(handle, str)
            and len(handle) == 32
            and intent.get("handleSource") == "client"
            and (pending_handle is None or pending_handle == handle),
            handle=handle,
            handleSource=intent.get("handleSource"),
            pendingHandle=pending_handle,
        )
        rec(
            ctx,
            f"{label}_the_intent_is_journaled_without_an_outcome",
            count_type(records, "intent") == 1
            and count_type(records, "outcome") == 0
            and (pending_handle is None or pending.get("handle") == pending_handle)
            and (pending_handle is not None or count_type(records, "pending") == 0)
            and state_of(ctx, group) == "unknown",
            handle=handle,
            recordTypes=[r.get("type") for r in records],
        )
        if not isinstance(handle, str):
            return
    ctx.ids[label] = {"group": group, "handle": handle, "key": key, "writer": writer}
    ctx.ids[label]["expectedFrames"] = expected

    waited = await wait_for_result_file(ctx, handle)
    rec(
        ctx,
        f"{label}_the_result_file_appeared_in_the_bridges_results_directory",
        waited["appeared"] is True and waited.get("size", 0) > 0,
        **waited,
    )
    names = local_list(directory)
    frames = frames_of(names, writer)
    rec(
        ctx,
        f"{label}_all_frames_are_on_disk_by_this_client",
        frames == frame_names(writer, expected),
        found=len(frames),
        expected=expected,
        names=names,
    )
    ctx.ids[label].update(framesOnDisk=len(frames), resultFileAtEnd=waited)
    if move_result and waited["appeared"]:
        move_the_result_file(ctx, label, handle)


def move_the_result_file(ctx: Ctx, label: str, handle: str) -> None:
    """Take the finished render's result file out of the bridge's results directory (the
    situation of a Flame restart that lost it). Moved, never deleted, and never put back."""
    dest_dir = OUTPUT_DIR / f"moved_results_{ctx.ts}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    src, dest = result_file(ctx, handle), dest_dir / f"{handle}.json"
    before = stat_of(src)
    os.replace(src, dest)
    after_src, after_dest = stat_of(src), stat_of(dest)
    moved: dict[str, Any] = {"src": str(src), "dest": str(dest)}
    try:
        answer = json.loads(dest.read_text(encoding="utf-8"))
        flame = answer.get("result") if isinstance(answer, dict) else None
        moved["answerOk"] = answer.get("ok") if isinstance(answer, dict) else None
        moved["runMs"] = answer.get("runMs") if isinstance(answer, dict) else None
        moved["returned"] = flame.get("returned") if isinstance(flame, dict) else None
    except (OSError, ValueError) as exc:
        moved["readError"] = str(exc)
    ctx.ids[label]["movedTo"] = str(dest)
    rec(
        ctx,
        f"{label}_the_result_file_was_moved_out_of_the_results_directory",
        before is not None
        and after_src is None
        and after_dest is not None
        and after_dest["size"] == before["size"],
        before=before,
        afterSrc=after_src,
        afterDest=after_dest,
    )
    observe(ctx, f"{label}_moved_result_file_content", **moved)


async def stage_r1(ctx: Ctx) -> None:
    await stage_render(ctx, "H", move_result=False)


async def stage_r2(ctx: Ctx) -> None:
    await stage_render(ctx, "K", move_result=True)


async def stage_z1(ctx: Ctx) -> None:
    async with server_session(ctx, "z1-plain") as session:
        journal = _result_of(await version_info(session, ctx)).get("journal", {})
        rec(
            ctx,
            "z1_three_intents_are_left_undecided_and_no_test_hook_is_active",
            journal.get("unresolvedIntents") == OPEN_INTENTS
            and journal.get("writesBlocked") is False
            and journal.get("testHooks") == [],
            journal=journal,
        )
    observe(
        ctx,
        "z1_results_directory_before_exit",
        files=sorted(p.name for p in results_dir(ctx).glob("*.json")),
        r1=(ctx.ids.get("r1") or {}).get("handle"),
        r2=(ctx.ids.get("r2") or {}).get("handle"),
    )


# phase 2
async def stage_ready(ctx: Ctx) -> None:
    desktop = await wait_for_desktop(ctx)
    uids = set(desktop.get("uids", [])) if isinstance(desktop, dict) else set()
    rec(
        ctx,
        "ready_the_desktop_can_be_read",
        isinstance(desktop, dict),
        batchGroups=(desktop.get("batchGroups") if isinstance(desktop, dict) else desktop),
    )
    observe(
        ctx,
        "ready_which_groups_survived_the_restart",
        groups={k: uid_of(ctx, k) in uids for k in GROUPS},
        r1ResultFile=stat_of(result_file(ctx, ctx.ids["r1"]["handle"])),
        r2ResultFile=stat_of(result_file(ctx, ctx.ids["r2"]["handle"])),
        resultsDirectory=sorted(p.name for p in results_dir(ctx).glob("*.json")),
    )


async def stage_p2_1(ctx: Ctx) -> None:
    async with server_session(ctx, "p2-1-plain") as session:
        info = await version_info(session, ctx)
        journal = _result_of(info).get("journal", {})
        rec(
            ctx,
            "p2_1_a_new_server_counts_3_undecided_intents_and_allows_writes",
            journal.get("unresolvedIntents") == OPEN_INTENTS
            and journal.get("writesBlocked") is False
            and journal.get("testHooks") == [],
            journal=journal,
        )


def check_entry(env: dict[str, Any]) -> dict[str, Any]:
    entries = _result_of(env).get("checked") or [{}]
    return entries[0] if isinstance(entries[0], dict) else {}


async def stage_p2_2_w1(ctx: Ctx) -> None:
    w1 = ctx.ids["w1"]
    uid = uid_of(ctx, "G")
    async with server_session(ctx, "p2-2-check-w1") as session:
        probe_before = await probe_pos(ctx, uid, W1_NODE)
        found = await do(session, ctx, "journal.check", {"group": w1["group"]})
        probe_after = await probe_pos(ctx, uid, W1_NODE)
        entry = check_entry(found)
        expected = expected_write_verdict(probe_before, w1["before"], w1["after"])
        observe(
            ctx,
            "p2_2_did_the_batch_change_survive_flame_exit",
            valueAfterPhase1=(w1.get("valueAfterPhase1") or {}).get("value"),
            valueNow=probe_before,
            before=w1["before"],
            after=w1["after"],
            verdict=entry.get("verdict"),
            detail=entry.get("detail"),
            perKey=entry.get("perKey"),
        )
        current = entry.get("current") if isinstance(entry.get("current"), dict) else {}
        node_found = isinstance(probe_before, dict) and probe_before.get("nodeFound") is True
        rec(
            ctx,
            "p2_2_check_verdict_agrees_with_the_value_really_there",
            found.get("ok") is True
            and expected is not None
            and entry.get("verdict") == expected
            and probe_before == probe_after
            and (not node_found or same(current.get(W1_KEY), probe_before.get("value"))),
            verdict=entry.get("verdict"),
            expected=expected,
            probeBefore=probe_before,
            probeAfter=probe_after,
            current=current,
            error=found.get("error"),
        )
        rec(
            ctx,
            "p2_2_check_wrote_nothing_and_the_group_state_follows_the_verdict",
            state_of(ctx, w1["group"]) == entry.get("verdict"),
            state=state_of(ctx, w1["group"]),
        )


async def stage_p2_3_r1(ctx: Ctx) -> None:
    r1 = ctx.ids["r1"]
    handle, writer, expected = r1["handle"], r1["writer"], int(r1["expectedFrames"])
    survived = stat_of(result_file(ctx, handle))
    async with server_session(ctx, "p2-3-check-r1") as session:
        found = await do(session, ctx, "journal.check", {"group": r1["group"]})
    entry = check_entry(found)
    frames = frames_of(local_list(out_dir(ctx, "H")), writer)
    records = journal_records_of(ctx, r1["group"])
    polled = next((r for r in records if r.get("type") == "polled"), {})
    ctx.ids["phase2R1"] = {"verdict": entry.get("verdict"), "basis": entry.get("basis")}
    observe(
        ctx,
        "p2_3_r1_check",
        resultFileSurvivedTheRestart=survived,
        basis=entry.get("basis"),
        verdict=entry.get("verdict"),
        detail=entry.get("detail"),
        renderOutcome=entry.get("renderOutcome"),
        polledFlame=polled.get("flame"),
        polledRunMs=polled.get("runMs"),
        resultFileAfter=stat_of(result_file(ctx, handle)),
    )
    rec(
        ctx,
        "p2_3_r1_verdict_agrees_with_the_files_on_disk",
        found.get("ok") is True
        and len(frames) == expected
        and entry.get("verdict") == verdict_from_count(len(frames), expected) == "landed",
        verdict=entry.get("verdict"),
        framesOnDisk=len(frames),
        expected=expected,
        error=found.get("error"),
    )
    rec(
        ctx,
        "p2_3_r1_basis_is_handle_exactly_when_the_result_file_survived",
        entry.get("basis") == ("handle" if survived is not None else "disk"),
        basis=entry.get("basis"),
        survived=survived,
    )
    rec(
        ctx,
        "p2_3_r1_the_poll_handed_the_file_over_once_and_journaled_it",
        (entry.get("basis") == "handle")
        == (count_type(records, "polled") == 1 and stat_of(result_file(ctx, handle)) is None),
        polledRecords=count_type(records, "polled"),
        fileStillThere=stat_of(result_file(ctx, handle)),
    )


async def stage_p2_4_r2(ctx: Ctx) -> None:
    r2 = ctx.ids["r2"]
    handle, writer, expected = r2["handle"], r2["writer"], int(r2["expectedFrames"])
    absent = stat_of(result_file(ctx, handle)) is None
    async with server_session(ctx, "p2-4-check-r2") as session:
        found = await do(session, ctx, "journal.check", {"group": r2["group"]})
    entry = check_entry(found)
    frames = frames_of(local_list(out_dir(ctx, "K")), writer)
    ctx.ids["phase2R2"] = {"verdict": entry.get("verdict"), "basis": entry.get("basis")}
    detail = str(entry.get("detail") or "")
    observe(
        ctx,
        "p2_4_r2_check",
        basis=entry.get("basis"),
        verdict=entry.get("verdict"),
        detail=entry.get("detail"),
        perKey=entry.get("perKey"),
        current=entry.get("current"),
        groupFound=entry.get("groupFound", True),
    )
    rec(
        ctx,
        "p2_4_r2_the_result_file_is_still_absent_before_the_check",
        absent,
        moved=r2.get("movedTo"),
    )
    rec(
        ctx,
        "p2_4_r2_basis_disk_and_verdict_agrees_with_the_files_on_disk",
        found.get("ok") is True
        and entry.get("basis") == "disk"
        and len(frames) == expected
        and entry.get("verdict") == verdict_from_count(len(frames), expected) == "landed",
        basis=entry.get("basis"),
        verdict=entry.get("verdict"),
        framesOnDisk=len(frames),
        expected=expected,
        error=found.get("error"),
    )
    rec(
        ctx,
        "p2_4_r2_detail_says_the_end_of_the_render_was_not_confirmed",
        "estimate" in detail and "not confirmed" in detail,
        detail=detail,
    )


async def stage_p2_5_again(ctx: Ctx) -> None:
    r1, first = ctx.ids["r1"], ctx.ids["phase2R1"]
    async with server_session(ctx, "p2-5-check-r1-again") as session:
        again = await do(session, ctx, "journal.check", {"group": r1["group"]})
        shown = await do(session, ctx, "journal.show", {"group": r1["group"]})
        listing = await do(session, ctx, "journal.list", {"limit": 200})
    result = _result_of(again)
    rec(
        ctx,
        "p2_5_a_second_check_has_nothing_to_decide_and_asks_nobody",
        again.get("ok") is True and result.get("inDoubt") == 0 and result.get("checked") == [],
        result=result,
    )
    records = _result_of(shown).get("records") or []
    checks = [r for r in records if r.get("type") == "check"]
    rec(
        ctx,
        "p2_5_the_journal_holds_one_pending_one_check_and_polled_only_after_a_handle_answer",
        count_type(records, "pending") == 1
        and len(checks) == 1
        and count_type(records, "polled") == (1 if first["basis"] == "handle" else 0)
        and checks[0].get("verdict") == first["verdict"]
        and checks[0].get("basis") == first["basis"],
        recordTypes=[r.get("type") for r in records],
        first=first,
        check={k: checks[0].get(k) for k in ("verdict", "basis")} if checks else None,
    )
    states = {i.get("group"): i.get("state") for i in _result_of(listing).get("items", [])}
    rec(
        ctx,
        "p2_5_list_still_says_the_first_verdict",
        states.get(r1["group"]) == first["verdict"],
        state=states.get(r1["group"]),
    )


async def stage_p2_6_list(ctx: Ctx) -> None:
    groups = {k: ctx.ids[k]["group"] for k in STATE_KEYS_NEEDED}
    async with server_session(ctx, "p2-6-list") as session:
        listing = await do(session, ctx, "journal.list", {"limit": 200})
        nothing = await do(session, ctx, "journal.check", {})
        info = await version_info(session, ctx)
    states = {i.get("group"): i.get("state") for i in _result_of(listing).get("items", [])}
    rec(
        ctx,
        "p2_6_none_of_the_three_groups_is_unknown",
        all(states.get(g) not in (None, "unknown") for g in groups.values()),
        states={k: states.get(g) for k, g in groups.items()},
    )
    rec(
        ctx,
        "p2_6_a_check_of_everything_finds_nothing_in_doubt",
        nothing.get("ok") is True and _result_of(nothing).get("inDoubt") == 0,
        result=_result_of(nothing),
    )
    journal = _result_of(info).get("journal", {})
    rec(
        ctx,
        "p2_6_no_intent_is_left_undecided",
        journal.get("unresolvedIntents") == 0
        and journal.get("writesBlocked") is False
        and journal.get("testHooks") == [],
        journal=journal,
    )
    observe(
        ctx,
        "p2_6_results_directory_at_the_end",
        files=sorted(p.name for p in results_dir(ctx).glob("*.json")),
        movedResultFileNotPutBack=stat_of(result_file(ctx, ctx.ids["r2"]["handle"])) is None,
    )


async def stage_z2(ctx: Ctx) -> None:
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def exit_flame(ctx: Ctx, label: str) -> None:
    ctx.stage = "cleanup"
    ctx.results["cleanup"] = {
        "deleted": [],
        "kept": {
            "batchGroups": ctx.ids.get("groups"),
            "renderedFiles": ctx.ids.get("filesDir"),
            "movedResultFiles": [
                (ctx.ids.get(k) or {}).get("movedTo") for k in ("r1", "r2") if ctx.ids.get(k)
            ],
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them); rendered files are kept"
            ),
        },
    }
    deadline = time.monotonic() + CHECK_RETRY_TOTAL_S
    attempts: list[dict[str, Any]] = []
    while True:
        exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label=label)
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


async def phase1(ctx: Ctx, state_path: Path) -> None:
    async def flushed(name: str, stage: Callable[[Ctx], Awaitable[None]]) -> None:
        await guarded(ctx, name, stage)
        flush(ctx, state_path)

    try:
        await flushed("prepare", prepare)
        if "groups" in ctx.ids and "libClips" in ctx.ids:
            await flushed("prepare_build", prepare_build)
            for name, stage in (("w1", stage_w1), ("r1", stage_r1), ("r2", stage_r2)):
                await flushed(name, stage)
            await flushed("z1", stage_z1)
        else:
            rec(ctx, "prepare_incomplete_stages_skipped", False, ids=ctx.ids)
        rec(
            ctx, "phase1_state_is_ready_for_phase_2", state_is_ready(ctx.ids), state=str(state_path)
        )
    finally:
        exit_flame(ctx, "p24_phase1_exit")
        flush(ctx, state_path)
        save_stage(ctx)
        save(ctx.results)


async def phase2(ctx: Ctx, _state_path: Path) -> None:
    try:
        for name, stage in (
            ("ready", stage_ready),
            ("p2_1", stage_p2_1),
            ("p2_2", stage_p2_2_w1),
            ("p2_3", stage_p2_3_r1),
            ("p2_4", stage_p2_4_r2),
            ("p2_5", stage_p2_5_again),
            ("p2_6", stage_p2_6_list),
            ("z2", stage_z2),
        ):
            await guarded(ctx, name, stage)
    finally:
        exit_flame(ctx, "p24_phase2_exit")
        save_stage(ctx)
        save(ctx.results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", type=int, choices=(1, 2), required=True)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--tag", default=None, help="the run's timestamp, shared by both phases")
    parser.add_argument("--state-file", default=None)
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.tag or time.strftime("%y%m%d_%H%M%S")
    state_path = (
        Path(args.state_file) if args.state_file else OUTPUT_DIR / f"p2_4_restart_state_{tag}.json"
    )
    results: Results = {
        "ok": True,
        "phase": args.phase,
        "steps": [],
        "servers": [],
        "observations": [],
        "statePath": str(state_path),
        "_out_path": OUTPUT_DIR / f"p2_4_restart_e2e_{tag}_phase{args.phase}.json",
    }
    fatal: str | None = None
    ctx: Ctx | None = None
    try:
        if args.phase == 1:
            journal_dir = OUTPUT_DIR / f"journal_p24_e2e_{tag}"
            journal_dir.mkdir(parents=True, exist_ok=True)
            ctx = Ctx(run_dir, results, tag, journal_dir)
            ctx.ids.update(tag=tag, journalDir=str(journal_dir), runDir=str(run_dir))
            results["journalDir"] = str(journal_dir)
            anyio.run(phase1, ctx, state_path)
        else:
            state = read_state(state_path)
            journal_dir = Path(str(state["journalDir"]))
            ctx = Ctx(run_dir, results, tag, journal_dir)
            ctx.ids.update(state)
            results["journalDir"] = str(journal_dir)
            anyio.run(phase2, ctx, state_path)
    except BaseException:
        fatal = traceback.format_exc()
        results["ok"] = False
        results["fatal_error"] = fatal
    save(results)
    failed = [s["name"] for s in results["steps"] if not s["ok"]]
    print(f"Result: {results['_out_path']}")
    print(f"phase={args.phase} steps={len(results['steps'])} failed={len(failed)}: {failed}")
    print_observations(results)
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
