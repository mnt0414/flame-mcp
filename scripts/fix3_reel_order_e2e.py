#!/usr/bin/env python3
"""Real-Flame end-to-end: a restore of a Batch checkpoint puts the clips of a reel
back in the order the checkpoint recorded, although the `.batch` writes its Clip nodes in another
order (run with the `.venv` Python; `scripts/run_e2e.sh fix3_reel_order` starts Flame; `--dry-run`
connects to nothing, lists the calls, compiles the code and checks the file rewrite on a made-up
setup).

Checks: Restoring a Batch checkpoint puts the clips of a reel back in the order the checkpoint
recorded, although Flame writes the Clip nodes of a `.batch` file in another order.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (the groups' reels and nodes, the current group).
Everything this run makes is named `PFX3E_<ts>_...` (scripts/p2_4_clean_probe_project.py knows the
prefix) and is deleted at the end by that prefix; then flame.exit(). The journal directory is this
run's own (`p3/journal_fix3_e2e_<ts>/`). No test hook is set for any server. The first step refuses
to run anywhere but in the project `flame_mcp_probe`.

Material (transport): two Batch groups, GN and GC (made last, so the current one), each with
`clip1.mov clip2.mov clip3.mov clip1.mov` imported into its first schematic reel by
`PyBatch.import_clip`, in that order.

Per group (stage n: not current, stage c: current)
  1. `node.create` (a checkpointed write): the checkpoint it took is the state to come back to.
     The reel's recorded order is the one read through the transport before the write.
  2. This run rewrites THAT checkpoint's `.batch` (its own file) so that the Clip nodes are
     written in the reverse order (`reorder_clip_elements`; nothing else in the file changes):
     the order a real `.batch` can have. Checked: the written order is now clip1 clip3 clip2
     clip1.
  3. `journal.revert` of the write. JUDGED: the reel is clip1 clip2 clip3 clip1 again (read through
     the transport), result.reelsRestored and result.matched are true, the fingerprint after equals
     the checkpoint's, result.clipOrder says needed / reordered [Schematic Reel 1] / no error and
     names a copy that is gone, the group's node list is the one before the write, the directory
     of working copies holds nothing, and the rewritten checkpoint (the `.batch` and every file of
     its directory) has the same bytes as before the restore.
  4. Negative control: `batch.load_setup` of the SAME rewritten file reads the reel in the written
     order (clip1 clip3 clip2 clip1), and its result has no clipOrder: the file really had the
     order that makes the reel wrong, and the restore's reorder is what corrects it.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
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
    _br_dict,
    _result_of,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do,
    guarded,
    rec,
    save,
    save_stage,
    server_session,
    tx,
)
from p2_4_common import (  # noqa: E402
    flame_exit,
    journal_records,
    journal_states,
    observe,
    ops_used,
    print_observations,
)
from p3_1a_read_e2e import PRELUDE, fill, is_error, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
NAME_PREFIX: Final[str] = "PFX3E"
CLIP_FILES: Final[tuple[str, ...]] = ("clip1.mov", "clip2.mov", "clip3.mov", "clip1.mov")
RECORDED_ORDER: Final[list[str]] = ["clip1", "clip2", "clip3", "clip1"]
"""The reel as imported, and so as the checkpoint records it."""
WRITTEN_ORDER: Final[list[str]] = ["clip1", "clip3", "clip2", "clip1"]
"""The same Clip nodes written in reverse: what `load_setup` makes of the reel (the restore's
reorder is not applied)."""
REEL_NAME: Final[str] = "Schematic Reel 1"
SIDES: Final[tuple[str, ...]] = ("n", "c")
SIDE_TITLE: Final[dict[str, str]] = {"n": "GN (not current)", "c": "GC (current)"}
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed"}
)
MAKE_TIMEOUT_S: Final[float] = 180.0
ORDERED_DIR: Final[tuple[str, ...]] = ("checkpoints", "ordered")

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Any]] = {}


def call_for(call_id: str, op: str) -> Any:
    def register(builder: Any) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


def batch_sel(uid: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid}


@call_for("w1_write_a_node", "node.create")
def _w1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": batch_sel(ids["uid"]), "type": "Blur"}


@call_for("r1_revert_the_write", "journal.revert")
def _r1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["group"]}


@call_for("n1_load_the_rewritten_file", "batch.load_setup")
def _n1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": batch_sel(ids["uid"]), "path": ids["path"]}


# -- transport scaffolding (never through the op under test) ------------------------------------

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

MAKE_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_made = [(_key, _d.create_batch_group(_gname)) for _key, _gname in @GROUPS@]",
        "_uids = {}",
        "for _key, _bg in _made:",
        "    _rn = _v(_bg.reels[0].name)",
        "    for _p in @PATHS@:",
        "        _bg.import_clip(_p, _rn)",
        "    _uids[_key] = _v(_bg.uid)",
        "_result = _uids",
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
        "        'nodes': sorted(_v(_n.name) for _n in _bg.nodes),",
        "        'reels': [[_v(_r.name), [_v(_c.name) for _c in _r.clips]] for _r in _bg.reels],",
        "    }",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "make": MAKE_CODE,
    "group": GROUP_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def tx_group(ctx: Ctx, uid: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(GROUP_CODE, UID=uid), "fix3_group")
    return None if is_error(got) or got is None else got


def reel_clips(group: dict[str, Any] | None) -> list[str] | None:
    """The clip names of `Schematic Reel 1` of a group read by `tx_group`."""
    if not isinstance(group, dict):
        return None
    return next((row[1] for row in group["reels"] if row[0] == REEL_NAME), None)


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}_{tail}"


def written_groups(ctx: Ctx) -> list[dict[str, str]]:
    return ctx.ids.setdefault("writeGroups", [])


async def run_call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    env = await do(session, ctx, op, build({**ctx.ids, **extra}))
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


# -- the checkpoint file (client side; nothing here touches Flame) -----------------------------


def checkpoint_of(ctx: Ctx, write_group: str) -> dict[str, Any]:
    """The checkpoint the write in `write_group` took first: its record (id, path, reels,
    fingerprint), found through the intent's `revertCheckpoint` in this run's own journal."""
    records = journal_records(ctx)
    intent = next(
        (r for r in records if r.get("type") == "intent" and r.get("group") == write_group), {}
    )
    wanted = intent.get("revertCheckpoint")
    return next((r for r in records if r.get("type") == "checkpoint" and r.get("id") == wanted), {})


NODE_RE = re.compile(r"<Node>.*?</Node>", re.DOTALL)


def _tag(chunk: str, name: str) -> str | None:
    m = re.search(rf"<{name}>(.*?)</{name}>", chunk, re.DOTALL)
    return html.unescape(m.group(1)) if m else None


def node_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in NODE_RE.finditer(text)]


def _rebuild(text: str, spans: list[tuple[int, int]], chunks: list[str]) -> str:
    out: list[str] = []
    cursor = 0
    for (a, b), chunk in zip(spans, chunks, strict=True):
        out.append(text[cursor:a])
        out.append(chunk)
        cursor = b
    out.append(text[cursor:])
    return "".join(out)


def node_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, (a, b) in enumerate(node_spans(text)):
        chunk = text[a:b]
        number = _tag(chunk, "Number")
        rows.append(
            {
                "index": i,
                "type": _tag(chunk, "Type"),
                "name": _tag(chunk, "Name"),
                "schem_name": _tag(chunk, "SchemName"),
                "number": int(number) if number is not None and number.isdigit() else number,
            }
        )
    return rows


def clip_rows(batch_path: Path) -> list[dict[str, Any]]:
    text = batch_path.read_bytes().decode("utf-8")
    base_dir = batch_path.parent / batch_path.stem
    out: list[dict[str, Any]] = []
    for row in node_rows(text):
        if row["type"] != "Clip":
            continue
        node_file = base_dir / f"{row['name']}.clip_node"
        exists = node_file.is_file()
        body = node_file.read_bytes().decode("utf-8", errors="replace") if exists else ""
        out.append(
            {
                "pos_in_clips": len(out),
                "node_index": row["index"],
                "node_name": row["name"],
                "number": row["number"],
                "schem_name": row["schem_name"],
                "clip_node_file": exists,
                "clip_name": _tag(body, "ClipName"),
                "reel_name": _tag(body, "ReelName"),
            }
        )
    return out


def reorder_clip_elements(text: str) -> str:
    spans = node_spans(text)
    chunks = [text[a:b] for a, b in spans]
    idx = [i for i, c in enumerate(chunks) if _tag(c, "Type") == "Clip"]
    out = list(chunks)
    for dst, src in zip(idx, reversed(idx), strict=True):
        out[dst] = chunks[src]
    return _rebuild(text, spans, out)


def tree_digest(batch_path: Path) -> dict[str, str]:
    """sha256 of the `.batch` and of every file of its directory, by relative path."""
    base = batch_path.parent / batch_path.stem
    files = [batch_path, *sorted(p for p in base.rglob("*") if p.is_file())]
    return {
        p.relative_to(batch_path.parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in files
    }


def written_clip_names(batch_path: Path) -> list[str | None]:
    """The ClipName of every Clip node of a `.batch`, in the order they are written."""
    return [row["clip_name"] for row in clip_rows(batch_path)]


def rewrite_reversed(batch_path: Path) -> None:
    """Write the Clip nodes of the `.batch` in reverse order, in place (nothing else changes)."""
    text = batch_path.read_bytes().decode("utf-8")
    batch_path.write_bytes(reorder_clip_elements(text).encode("utf-8"))


def ordered_leftovers(ctx: Ctx) -> list[str] | None:
    """What is in the directory of working copies now; None when the directory is not there."""
    directory = ctx.journal_dir.joinpath(*ORDERED_DIR)
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else None


# -- prepare ------------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    ids = ctx.ids
    project = await tx_val(ctx, PROJECT_CODE, "fix3_project")
    rec(
        ctx,
        "prepare_the_project_is_the_probe_project",
        project == TARGET_PROJECT,
        project=project,
    )
    if project != TARGET_PROJECT:
        return
    missing = [str(MEDIA_DIR / n) for n in CLIP_FILES if not (MEDIA_DIR / n).is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    groups = [["n", name_of(ctx, "GN")], ["c", name_of(ctx, "GC")]]
    made = await tx(
        ctx,
        PRELUDE
        + "\n"
        + fill(MAKE_CODE, GROUPS=groups, PATHS=[str(MEDIA_DIR / n) for n in CLIP_FILES]),
        label="fix3_prepare",
        timeout_s=MAKE_TIMEOUT_S,
    )
    rec(ctx, "prepare_material_is_made", made.ok, resp=_br_dict(made))
    if not (made.ok and isinstance(made.result, dict)):
        return
    for side in SIDES:
        ids[f"{side}Uid"] = made.result[side]
    for side in SIDES:
        group = await tx_group(ctx, ids[f"{side}Uid"])
        clips = reel_clips(group)
        ids[f"{side}Before"] = group
        rec(
            ctx,
            f"prepare_{side}_the_reel_holds_the_four_clips_in_the_order_they_were_imported",
            clips == RECORDED_ORDER,
            clips=clips,
            group=group,
        )
    cur = await tx_group(ctx, ids["cUid"])
    other = await tx_group(ctx, ids["nUid"])
    rec(
        ctx,
        "prepare_gc_is_the_current_group_and_gn_is_not",
        bool(cur and cur["isCurrent"]) and bool(other) and not other["isCurrent"],
        gc=cur and cur["isCurrent"],
        gn=other and other["isCurrent"],
    )


# -- stages n / c -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rewritten:
    """The checkpoint of this side's write after its `.batch` was rewritten."""

    path: Path
    checkpoint: dict[str, Any]
    group: str
    digest: dict[str, str]


async def write_and_rewrite(ctx: Ctx, session: Any, side: str) -> Rewritten | None:
    """Steps 1 and 2: the checkpointed write, then the rewrite of the checkpoint's `.batch`."""
    written = await run_call(session, ctx, "w1_write_a_node")
    write_group = (written.get("journal") or {}).get("group")
    rec(
        ctx,
        f"{side}1_the_checkpointed_write_lands",
        written.get("ok") is True and isinstance(write_group, str),
        error=written.get("error"),
        title=SIDE_TITLE[side],
    )
    checkpoint = checkpoint_of(ctx, str(write_group))
    path = Path(str(checkpoint.get("path")))
    recorded = next(
        (r.get("clips") for r in checkpoint.get("reels") or [] if r.get("name") == REEL_NAME),
        None,
    )
    usable = path.is_file() and recorded == RECORDED_ORDER
    rec(
        ctx,
        f"{side}1_its_checkpoint_has_a_file_and_records_the_reel_in_import_order",
        usable,
        path=str(path),
        recorded=recorded,
    )
    if not usable:
        return None
    file_before = written_clip_names(path)
    rewrite_reversed(path)
    file_after = written_clip_names(path)
    rec(
        ctx,
        f"{side}2_the_checkpoints_clip_nodes_are_now_written_in_reverse",
        file_before == RECORDED_ORDER and file_after == WRITTEN_ORDER,
        writtenBefore=file_before,
        writtenAfter=file_after,
    )
    return Rewritten(path, checkpoint, str(write_group), tree_digest(path))


def judge_clip_order(ctx: Ctx, side: str, done: Rewritten, order: dict[str, Any]) -> None:
    copy_path = order.get("loadedFrom")
    rec(
        ctx,
        f"{side}3_clip_order_says_it_reordered_that_reel_and_names_a_copy_that_is_gone",
        order.get("needed") is True
        and order.get("reordered") == [REEL_NAME]
        and order.get("error") is None
        and order.get("loaded") is True
        and isinstance(copy_path, str)
        and copy_path != str(done.path)
        and not Path(copy_path).exists(),
        clipOrder=order,
    )
    leftovers = ordered_leftovers(ctx)
    rec(ctx, f"{side}3_no_working_copy_is_left", leftovers in (None, []), leftovers=leftovers)
    rec(
        ctx,
        f"{side}3_the_rewritten_checkpoint_has_the_same_bytes_as_before_the_restore",
        tree_digest(done.path) == done.digest,
        files=len(done.digest),
    )


def judge_group(ctx: Ctx, side: str, nodes: dict[str, Any], result: dict[str, Any]) -> None:
    group = nodes["after"]
    rec(
        ctx,
        f"{side}3_the_group_lost_the_node_the_write_added",
        bool(group)
        and bool(nodes["afterWrite"])
        and group["nodes"] == nodes["before"]
        and group["nodes"] != nodes["afterWrite"],
        nodesAfterWrite=nodes["afterWrite"],
        nodesAfterRevert=group and group["nodes"],
        nodesBeforeWrite=nodes["before"],
    )
    observe(
        ctx,
        f"{side}3_reel_tidy_and_group_after_the_restore",
        reelTidy=result.get("reelTidy"),
        group=group,
    )


def judge_revert(
    ctx: Ctx,
    side: str,
    done: Rewritten,
    revert: dict[str, Any],
    nodes: dict[str, Any],
) -> None:
    """Step 3. `nodes` has what the transport read: the group after the revert (`after`), after
    the write (`afterWrite`) and before it (`before`)."""
    result = _result_of(revert)
    group = nodes["after"]
    clips = reel_clips(group)
    rec(
        ctx,
        f"{side}3_the_reel_is_back_in_the_recorded_order",
        revert.get("ok") is True and clips == RECORDED_ORDER,
        clips=clips,
        error=revert.get("error"),
    )
    rec(
        ctx,
        f"{side}3_reels_restored_matched_and_the_fingerprint_is_the_checkpoints",
        result.get("reelsRestored") is True
        and result.get("matched") is True
        and result.get("fingerprintAfter")
        == result.get("expectedFingerprint")
        == done.checkpoint.get("fingerprint"),
        reelsRestored=result.get("reelsRestored"),
        matched=result.get("matched"),
        fingerprintAfter=result.get("fingerprintAfter"),
        expected=result.get("expectedFingerprint"),
    )
    order = result.get("clipOrder") if isinstance(result.get("clipOrder"), dict) else {}
    judge_clip_order(ctx, side, done, order)
    judge_group(ctx, side, nodes, result)


async def control_load(ctx: Ctx, session: Any, side: str, uid: str) -> None:
    """Step 4: the same rewritten file through `batch.load_setup`."""
    loaded = await run_call(session, ctx, "n1_load_the_rewritten_file")
    control = reel_clips(await tx_group(ctx, uid))
    rec(
        ctx,
        f"{side}4_control_load_setup_of_the_same_file_reads_the_reel_in_written_order",
        loaded.get("ok") is True
        and control == WRITTEN_ORDER
        and "clipOrder" not in _result_of(loaded),
        clips=control,
        error=loaded.get("error"),
    )


async def stage_side(ctx: Ctx, side: str) -> None:
    ids = ctx.ids
    uid = ids[f"{side}Uid"]
    ids["uid"] = uid
    now = await tx_group(ctx, uid)
    observe(
        ctx,
        f"{side}0_the_target_group_when_the_stage_starts",
        title=SIDE_TITLE[side],
        isCurrent=now and now["isCurrent"],
    )
    async with server_session(ctx, f"fix3-{side}") as session:
        done = await write_and_rewrite(ctx, session, side)
        if done is None:
            return
        ids["path"] = str(done.path)
        ids["group"] = done.group
        after_write = (await tx_group(ctx, uid) or {}).get("nodes")
        revert = await run_call(session, ctx, "r1_revert_the_write")
        nodes = {
            "before": ids[f"{side}Before"]["nodes"],
            "afterWrite": after_write,
            "after": await tx_group(ctx, uid),
        }
        judge_revert(ctx, side, done, revert, nodes)
        await control_load(ctx, session, side, uid)


async def stage_n(ctx: Ctx) -> None:
    await stage_side(ctx, "n")


async def stage_c(ctx: Ctx) -> None:
    await stage_side(ctx, "c")


# -- z / driver ---------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {op for op, _build in CALLS.values()}
    rec(
        ctx,
        "z_every_op_of_this_fix_was_called",
        wanted <= set(used),
        missing=sorted(wanted - set(used)),
    )
    own = journal_states(ctx)
    rec(
        ctx,
        "z_every_group_of_this_run_is_decided_none_is_unknown",
        bool(own) and set(own.values()) <= GOOD_STATES,
        states=sorted(set(own.values())),
    )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{NAME_PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "fix3_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "fix3_left")
    ctx.results["cleanup"] = {"deleted": gone, "stillThere": left}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted",
        isinstance(gone, dict)
        and all(ok is True for rows_ in gone.values() for _n, ok in rows_)
        and left == {"reelGroups": [], "batchGroups": [], "libraries": []},
        deleted=gone,
        stillThere=left,
    )
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "nUid" in ctx.ids and "cUid" in ctx.ids:
            await guarded(ctx, "n", stage_n)
            await guarded(ctx, "c", stage_c)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "fix3_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------

SELFTEST_STEM: Final[str] = "selftest"


def selftest_rewrite() -> list[str]:
    """The file rewrite on a made-up setup (client side only): Clip nodes reversed, nothing else
    changed. Returns the problems found."""
    with tempfile.TemporaryDirectory(prefix="fix3_dry_", dir=OUTPUT_DIR) as tmp:
        root = Path(tmp)
        names = ("clip1", "clip2", "clip3", "clip4")
        clips = ["c1", "c2", "c3", "c1"]
        (root / SELFTEST_STEM).mkdir()
        chunks = []
        for number, (name, clip_name) in enumerate(zip(names, clips, strict=True), start=1):
            chunks.append(
                f"<Node><Type>Clip</Type><Name>{name}</Name><SchemName>x</SchemName>"
                f"<Number>{number}</Number></Node>"
            )
            (root / SELFTEST_STEM / f"{name}.clip_node").write_text(
                f"<Setup><State><ClipName>{clip_name}</ClipName>"
                f"<ReelName>{REEL_NAME}</ReelName></State></Setup>",
                encoding="utf-8",
            )
        batch = root / f"{SELFTEST_STEM}.batch"
        batch.write_bytes(("<Setup>" + "".join(chunks) + "<Links></Links></Setup>").encode("utf-8"))
        problems = []
        if written_clip_names(batch) != clips:
            problems.append(f"written order before: {written_clip_names(batch)}")
        before = tree_digest(batch)
        rewrite_reversed(batch)
        if written_clip_names(batch) != list(reversed(clips)):
            problems.append(f"written order after: {written_clip_names(batch)}")
        after = tree_digest(batch)
        changed = [k for k in before if before[k] != after[k]]
        if changed != [f"{SELFTEST_STEM}.batch"]:
            problems.append(f"files changed by the rewrite: {changed}")
        return problems


def placeholder_ids() -> dict[str, Any]:
    return {"uid": "uid-gn", "group": "g_dry", "path": "/dry/run/checkpoint.batch"}


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    ids = placeholder_ids()
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
            if op.precheck is not None:
                refusal = op.precheck(checked, None)
                if refusal is not None:
                    problems.append(f"precheck refused: {refusal.message}")
            if op.thread != "server":
                try:
                    compile(execution.build_exec_code(op, checked), f"<{call_id}>", "exec")
                except SyntaxError as exc:
                    problems.append(f"compile FAILED: {exc}")
        else:
            problems.append(
                "invalid args: " + "; ".join(f"{i.path}: {i.message}" for i in validation.issues)
            )
        print(f"{'NG ' + '; '.join(problems) if problems else 'OK':3} {call_id}: {op_name}")
        failures += bool(problems)
    print("===== transport code (compiled with every @NAME@ filled) =====")
    for name, template in TEMPLATES.items():
        code = PRELUDE + "\n" + re.sub(r"@[A-Z_0-9]+@", "'x'", template)
        try:
            compile(code, f"<{name}>", "exec")
            print(f"OK  {name} ({len(code)} chars)")
        except SyntaxError as exc:
            print(f"NG  {name}: {exc}")
            failures += 1
    print("===== file rewrite on a made-up setup =====")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    problems = selftest_rewrite()
    print("OK  rewrite_reversed" if not problems else f"NG  rewrite_reversed: {problems}")
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
    journal_dir = OUTPUT_DIR / f"journal_fix3_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"fix3_reel_order_e2e_{ts}.json",
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
