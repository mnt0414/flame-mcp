#!/usr/bin/env python3
"""Real-Flame end-to-end: `plan.run` with Batch node edits is ONE checkpointed write on one Batch
group, and one `journal.revert` puts the group back (run with the `.venv` Python;
`scripts/run_e2e.sh p5_plan_batch` starts Flame; `--dry-run` connects to nothing, lists the calls,
compiles the code and checks the comparison on made-up reads).

Checks: A `plan.run` of Batch node edits is one checkpointed write, in a group that is not the
current one and in the current one. The plan creates Blur b1 and Mux m1, connects b1 to m1 by the
names the plan gave them, sets a note on a node that was there and deletes a node that was there.
When it passes, the transport reads the edits and the journal holds one checkpoint. When its third
step is made to fail, the first two stay applied. One `journal.revert` brings back the node names,
the connections, the notes (the one the plan set included) and the `batch.fingerprint` the group
had before. A plan whose node does not resolve runs nothing.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (for the group: its nodes with their sockets and notes, and
whether it is current). Everything this run makes is named `PP5E_<ts>_...`
(scripts/p2_4_clean_probe_project.py knows the prefix) and is deleted at the end by that prefix;
the stash library name (`PP5E_<ts>_CKPT`, passed to every server as FLAME_MCP_CHECKPOINT_LIBRARY)
carries the prefix too; then flame.exit(). The journal directory is this run's own
(`p3/journal_p5pb_e2e_<ts>/`). No test hook is set for any server. The first step refuses to run
anywhere but in the project `flame_mcp_probe`.

Material (transport): two Batch groups, `PP5E_<ts>_A` and then `PP5E_<ts>_B`. A Batch group made
through Python becomes the current one and the current group cannot be changed from Python
(measured), so B, made last, is the current group and A is not. Each group gets, while it
is the current one, a Blur named `pp_base` and an Exposure named `pp_gone` with `pp_base` wired
into it (Flame checks a new node name against the current group only).

The plan of stages a and b, on the group of the stage:
  0 `node.create` Blur `b1`, 1 `node.create` Mux `m1`, 2 `node.connect` b1 -> m1,
  3 `node.set_attributes` the note of pp_base, 4 `node.delete` pp_gone.
  Step 2 names b1 and m1, which steps 0 and 1 create under exactly those names. The up-front
  check cannot find them, so the server leaves step 2 to its own turn by itself, without
  `late: true` in the step (`late` = 'implied' in result.batch.resolved). Steps 3 and 4 name nodes
  that are there before the plan, so the up-front check finds them and they are not late.
  The failing version asks step 2 for a socket the node does not have. The op's own check refuses
  it in the step's turn, so it fails every time.

Stage a (the group that is not current)
  1. The group is read through the transport (before). JUDGED: nodes pp_base and pp_gone, pp_base
     wired into pp_gone, and the group is not the current one. `batch.fingerprint` is recorded.
  2. The failing plan. JUDGED: not ok, INVALID_ARGS, stoppedAt 2, partial {done 2, failed 1,
     notRun 2}; the transport reads b1 and m1 (steps 0 and 1 stay applied) and pp_gone (step 4
     never ran); result.batch.fingerprintBefore is the fingerprint read before and
     fingerprintAfter is not.
  3. `journal.revert` of that group. JUDGED: ok; the transport state (node names, sockets, notes)
     equals the one before; `batch.fingerprint` equals the one before.
  4. The passing plan. JUDGED: ok, done 5; the transport reads b1, m1 and pp_base but not
     pp_gone, the link b1 -> m1, and the note on pp_base; result.batch.resolved has late
     [False, False, 'implied', False, False]; the journal holds one checkpoint record of the
     plan's group and result.batch.checkpoint is its id.
  5. `journal.revert` of that group. JUDGED: ok; the same three equalities as in 3, and the note
     the plan set on pp_base is back to the value it had before (a restore puts back what
     `node.set_attributes` wrote, read through the transport). OBSERVED:
     result.timelineFxLost of the revert, and whether the group is still not current.
Stage b: the same steps 1-5 on the current group. (The stages run b, a, n, z: it is not known
  whether a restore of a group that is not current makes it the current one, and B must still be
  the current group when stage b starts; step 5 records what happened to the current group.)
Stage n: JUDGED: a plan whose connect step names a node that is not there ends SELECTOR_EMPTY with
  result.ran false, `plan not run` in the error, the transport reads the group as before and the
  journal holds no record of it (nothing was saved, nothing was journaled).
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import e2e_guard  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
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
from p3_2a_e2e_common import CKPT_ENV  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
NAME_PREFIX: Final[str] = "PP5E"
BASE_NODE: Final[str] = "pp_base"
GONE_NODE: Final[str] = "pp_gone"
NEW_BLUR: Final[str] = "b1"
NEW_MUX: Final[str] = "m1"
NOTE: Final[str] = "PP5E plan note"
FAILING_SOCKET: Final[str] = "PP5E_NoSuchSocket"
UNRESOLVED_NODE: Final[str] = "pp_nope"
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed"}
)
MAKE_TIMEOUT_S: Final[float] = 180.0
PASSING_LATE: Final[tuple[Any, ...]] = (False, False, "implied", False, False)

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {}


def call_for(
    call_id: str, op: str
) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Callable[[dict[str, Any]], Any]]:
    def register(builder: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


def batch_sel(uid: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid}


def node_sel(uid: str, name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": batch_sel(uid), "name": name}


def plan_steps(uid: str, *, failing: bool) -> list[dict[str, Any]]:
    connect_args: dict[str, Any] = {
        "source": node_sel(uid, NEW_BLUR),
        "target": node_sel(uid, NEW_MUX),
    }
    if failing:
        connect_args["source_socket"] = FAILING_SOCKET
    return [
        {"op": "node.create", "args": {"target": batch_sel(uid), "type": "Blur", "name": NEW_BLUR}},
        {"op": "node.create", "args": {"target": batch_sel(uid), "type": "Mux", "name": NEW_MUX}},
        {"op": "node.connect", "args": connect_args},
        {
            "op": "node.set_attributes",
            "args": {"target": node_sel(uid, BASE_NODE), "values": {"note": NOTE}},
        },
        {"op": "node.delete", "args": {"target": node_sel(uid, GONE_NODE)}},
    ]


@call_for("plan_that_passes", "plan.run")
def _pass(ids: dict[str, Any]) -> dict[str, Any]:
    return {"steps": plan_steps(ids["uid"], failing=False), "label": "p5pb pass"}


@call_for("plan_that_fails_at_step_2", "plan.run")
def _fail(ids: dict[str, Any]) -> dict[str, Any]:
    return {"steps": plan_steps(ids["uid"], failing=True), "label": "p5pb fail"}


@call_for("plan_whose_node_is_not_there", "plan.run")
def _unresolved(ids: dict[str, Any]) -> dict[str, Any]:
    uid = ids["uid"]
    steps = [
        {"op": "node.create", "args": {"target": batch_sel(uid), "type": "Blur", "name": NEW_BLUR}},
        {
            "op": "node.connect",
            "args": {"source": node_sel(uid, BASE_NODE), "target": node_sel(uid, UNRESOLVED_NODE)},
        },
    ]
    return {"steps": steps, "label": "p5pb unresolved"}


@call_for("revert_the_plan", "journal.revert")
def _revert(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["group"]}


@call_for("fingerprint_of_the_group", "batch.fingerprint")
def _fingerprint(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": batch_sel(ids["uid"])}


# -- transport scaffolding (never through the op under test) ------------------------------------

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

MAKE_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = _d.create_batch_group(@NAME@)",
        "_a = _bg.create_node('Blur')",
        "_a.name.set_value(@BASE@)",
        "_b = _bg.create_node('Exposure')",
        "_b.name.set_value(@GONE@)",
        "_ret = _bg.connect_nodes(_a, 'Default', _b, 'Default')",
        "_cur = _v(_d.current_batch_group)",
        "_result = {",
        "    'uid': _v(_bg.uid), 'returned': repr(_ret),",
        "    'isCurrent': _cur is not None and _v(_cur.uid) == _v(_bg.uid),",
        "}",
    ]
)

READ_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_cur = _v(_d.current_batch_group)",
        "_result = None",
        "if _hit:",
        "    _bg = _hit[0]",
        "    _rows = []",
        "    for _n in list(_bg.nodes):",
        "        _rows.append({",
        "            'name': _v(_n.name), 'type': _g(lambda _n=_n: _v(_n.type)),",
        "            'note': _g(lambda _n=_n: _v(_n.note)),",
        "            'sockets': _g(lambda _n=_n: {",
        "                _s: {_k: list(_c) for _k, _c in _v(_n.sockets)[_s].items()}",
        "                for _s in ('input', 'output')}),",
        "        })",
        "    _result = {",
        "        'name': _v(_bg.name), 'uid': _v(_bg.uid),",
        "        'isCurrent': _cur is not None and _v(_cur.uid) == _v(_bg.uid),",
        "        'nodes': _rows,",
        "    }",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "make": MAKE_CODE,
    "read": READ_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}_{tail}"


async def tx_group(ctx: Ctx, uid: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(READ_CODE, UID=uid), "p5pb_read")
    return None if is_error(got) or got is None else got


# -- reading a read ----------------------------------------------------------------------------


def state_of(group: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a revert must give back, from a group read by `tx_group`: {node name: (sockets,
    note)}. Positions are left out (the op does not promise them)."""
    if group is None:
        return None
    return {n["name"]: {"sockets": n["sockets"], "note": n["note"]} for n in group["nodes"]}


def names_of(group: Mapping[str, Any] | None) -> list[str]:
    return sorted(n["name"] for n in group["nodes"]) if group else []


def links_of(group: Mapping[str, Any] | None) -> set[tuple[str, str]]:
    """{(source, target)} from the output sockets of the nodes."""
    found: set[tuple[str, str]] = set()
    for node in group["nodes"] if group else []:
        sockets = node["sockets"]
        if not isinstance(sockets, dict):
            continue
        for linked in sockets["output"].values():
            found.update((node["name"], target) for target in linked)
    return found


def note_of(group: Mapping[str, Any] | None, name: str) -> Any:
    hits = [n["note"] for n in group["nodes"] if n["name"] == name] if group else []
    return hits[0] if len(hits) == 1 else None


async def run_call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    return await do(session, ctx, op, build({**ctx.ids, **extra}))


async def fingerprint(session: Any, ctx: Ctx, uid: str) -> str | None:
    env = await run_call(session, ctx, "fingerprint_of_the_group", uid=uid)
    value = _result_of(env).get("fingerprint")
    return value if env.get("ok") is True and isinstance(value, str) else None


def plan_group_records(ctx: Ctx, group: str) -> list[dict[str, Any]]:
    return [r for r in journal_records(ctx) if r.get("group") == group]


# -- prepare ------------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    ids = ctx.ids
    project = await tx_val(ctx, PROJECT_CODE, "p5pb_project")
    rec(
        ctx,
        "prepare_the_project_is_the_probe_project",
        project == TARGET_PROJECT,
        project=project,
    )
    if project != TARGET_PROJECT:
        return
    for key, tail in (("A", "A"), ("B", "B")):
        made = await tx_val(
            ctx,
            fill(MAKE_CODE, NAME=name_of(ctx, tail), BASE=BASE_NODE, GONE=GONE_NODE),
            f"p5pb_make_{key}",
            MAKE_TIMEOUT_S,
        )
        rec(
            ctx,
            f"prepare_group_{key}_is_made_with_a_wired_pair_of_nodes",
            isinstance(made, dict) and made.get("returned") == "True",
            made=made,
        )
        if not isinstance(made, dict):
            return
        ids[f"uid{key}"] = made["uid"]
        ids[f"made{key}"] = made
    a_now = await tx_group(ctx, ids["uidA"])
    b_now = await tx_group(ctx, ids["uidB"])
    rec(
        ctx,
        "prepare_b_made_last_is_the_current_group_and_a_is_not",
        bool(a_now) and bool(b_now) and b_now["isCurrent"] is True and a_now["isCurrent"] is False,
        a=a_now and a_now["isCurrent"],
        b=b_now and b_now["isCurrent"],
    )


# -- stages a and b -----------------------------------------------------------------------------


def judge_before(ctx: Ctx, tag: str, before: dict[str, Any] | None, *, current: bool) -> bool:
    ready = (
        before is not None
        and names_of(before) == sorted([BASE_NODE, GONE_NODE])
        and links_of(before) == {(BASE_NODE, GONE_NODE)}
        and note_of(before, BASE_NODE) != NOTE
        and before["isCurrent"] is current
    )
    rec(
        ctx,
        f"{tag}1_before_the_pair_is_wired_and_the_group_is_{'' if current else 'not_'}current",
        ready,
        nodes=before and before["nodes"],
        isCurrent=before and before["isCurrent"],
    )
    return ready


async def failing_plan(
    session: Any,
    ctx: Ctx,
    tag: str,
    uid: str,
    before: dict[str, Any],
    fp_before: str | None,
) -> bool:
    env = await run_call(session, ctx, "plan_that_fails_at_step_2", uid=uid)
    result = _result_of(env)
    group = (env.get("journal") or {}).get("group")
    info = result.get("batch") or {}
    after = await tx_group(ctx, uid)
    rec(
        ctx,
        f"{tag}2_the_failing_plan_stops_at_step_2_and_the_first_two_steps_stay",
        env.get("ok") is False
        and env.get("errorCode") == "INVALID_ARGS"
        and result.get("stoppedAt") == 2
        and env.get("partial") == {"done": 2, "failed": 1, "notRun": 2}
        and isinstance(group, str)
        and names_of(after) == sorted([BASE_NODE, GONE_NODE, NEW_BLUR, NEW_MUX])
        and info.get("fingerprintBefore") == fp_before
        and fp_before is not None
        and info.get("fingerprintAfter") != fp_before,
        errorCode=env.get("errorCode"),
        error=env.get("error"),
        stoppedAt=result.get("stoppedAt"),
        partial=env.get("partial"),
        names=names_of(after),
        fingerprintBefore=info.get("fingerprintBefore"),
        fingerprintAfter=info.get("fingerprintAfter"),
    )
    if not isinstance(group, str):
        return False
    ctx.ids["group"] = group
    return await reverted_to(session, ctx, tag, "3", uid, before, fp_before)


async def reverted_to(
    session: Any,
    ctx: Ctx,
    tag: str,
    number: str,
    uid: str,
    before: dict[str, Any],
    fp_before: str | None,
) -> bool:
    env = await run_call(session, ctx, "revert_the_plan")
    result = _result_of(env)
    after = await tx_group(ctx, uid)
    fp_after = await fingerprint(session, ctx, uid)
    state_ok = state_of(after) == state_of(before)
    rec(
        ctx,
        f"{tag}{number}_one_revert_gives_back_the_names_the_connections_the_notes_and_the_fingerprint",
        env.get("ok") is True
        and state_ok
        and fp_after is not None
        and fp_after == fp_before
        and after is not None
        and names_of(after) == names_of(before),
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        stateEqual=state_ok,
        before=state_of(before),
        after=state_of(after),
        fingerprintBefore=fp_before,
        fingerprintAfter=fp_after,
    )
    if number == "5":
        rec(
            ctx,
            f"{tag}5_the_note_the_plan_set_on_the_old_node_is_back_to_its_value",
            note_of(after, BASE_NODE) == note_of(before, BASE_NODE)
            and note_of(before, BASE_NODE) != NOTE,
            before=note_of(before, BASE_NODE),
            after=note_of(after, BASE_NODE),
        )
    observe(
        ctx,
        f"{tag}{number}_the_revert_result_timeline_fx_and_the_current_group_after",
        timelineFxLost=result.get("timelineFxLost"),
        timelineFxUnchecked=result.get("timelineFxUnchecked"),
        isCurrentAfter=after and after["isCurrent"],
        isCurrentBefore=before["isCurrent"],
    )
    return bool(env.get("ok"))


async def passing_plan(
    session: Any,
    ctx: Ctx,
    tag: str,
    uid: str,
    before: dict[str, Any],
    fp_before: str | None,
) -> None:
    env = await run_call(session, ctx, "plan_that_passes", uid=uid)
    result = _result_of(env)
    group = (env.get("journal") or {}).get("group")
    info = result.get("batch") or {}
    after = await tx_group(ctx, uid)
    late = [r.get("late") for r in info.get("resolved") or []]
    checkpoints = [
        r
        for r in plan_group_records(ctx, group if isinstance(group, str) else "")
        if r.get("type") == "checkpoint"
    ]
    rec(
        ctx,
        f"{tag}4_the_passing_plan_lands_and_takes_one_checkpoint",
        env.get("ok") is True
        and isinstance(group, str)
        and env.get("partial") == {"done": 5, "failed": 0, "notRun": 0}
        and names_of(after) == sorted([BASE_NODE, NEW_BLUR, NEW_MUX])
        and (NEW_BLUR, NEW_MUX) in links_of(after)
        and note_of(after, BASE_NODE) == NOTE
        and tuple(late) == PASSING_LATE
        and len(checkpoints) == 1
        and checkpoints[0].get("id") == info.get("checkpoint"),
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        partial=env.get("partial"),
        names=names_of(after),
        links=sorted(links_of(after)),
        note=note_of(after, BASE_NODE),
        late=late,
        checkpointRecords=[c.get("id") for c in checkpoints],
        checkpoint=info.get("checkpoint"),
        changed=info.get("changed"),
    )
    if not isinstance(group, str):
        return
    ctx.ids["group"] = group
    await reverted_to(session, ctx, tag, "5", uid, before, fp_before)


async def stage_group(ctx: Ctx, key: str, tag: str, *, current: bool) -> None:
    uid = ctx.ids[f"uid{key}"]
    ctx.ids["uid"] = uid
    before = await tx_group(ctx, uid)
    if before is None or not judge_before(ctx, tag, before, current=current):
        return
    async with server_session(ctx, f"p5pb-{tag}") as session:
        fp_before = await fingerprint(session, ctx, uid)
        observe(ctx, f"{tag}1_the_fingerprint_before", fingerprint=fp_before)
        if not await failing_plan(session, ctx, tag, uid, before, fp_before):
            return
        await passing_plan(session, ctx, tag, uid, before, fp_before)


async def stage_a(ctx: Ctx) -> None:
    await stage_group(ctx, "A", "a", current=False)


async def stage_b(ctx: Ctx) -> None:
    await stage_group(ctx, "B", "b", current=True)


# -- stage n ------------------------------------------------------------------------------------


async def stage_n(ctx: Ctx) -> None:
    uid = ctx.ids["uidA"]
    ctx.ids["uid"] = uid
    before = await tx_group(ctx, uid)
    records_before = len(journal_records(ctx))
    async with server_session(ctx, "p5pb-n") as session:
        env = await run_call(session, ctx, "plan_whose_node_is_not_there")
        result = _result_of(env)
        after = await tx_group(ctx, uid)
        rec(
            ctx,
            "n1_a_plan_whose_node_is_not_there_runs_nothing_and_leaves_no_trace",
            env.get("ok") is False
            and env.get("errorCode") == "SELECTOR_EMPTY"
            and result.get("ran") is False
            and "plan not run" in str(env.get("error"))
            and state_of(after) == state_of(before)
            and len(journal_records(ctx)) == records_before,
            errorCode=env.get("errorCode"),
            error=env.get("error"),
            ran=result.get("ran"),
            preResolution=result.get("preResolution"),
            stateEqual=state_of(after) == state_of(before),
            journalRecordsBefore=records_before,
            journalRecordsAfter=len(journal_records(ctx)),
        )


# -- z / driver ---------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    wanted = {op for op, _build in CALLS.values()}
    rec(
        ctx,
        "z_every_op_of_this_run_was_called",
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
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p5pb_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p5pb_left")
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
    os.environ[CKPT_ENV] = name_of(ctx, "CKPT")
    ctx.results["env"] = {CKPT_ENV: os.environ[CKPT_ENV]}
    try:
        await guarded(ctx, "prepare", prepare)
        if "uidA" in ctx.ids and "uidB" in ctx.ids:
            await guarded(ctx, "b", stage_b)  # first: a restore of A might make A the current one
            await guarded(ctx, "a", stage_a)
            await guarded(ctx, "n", stage_n)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p5pb_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------


def selftest_comparison() -> list[str]:
    """The comparison of two reads on made-up groups (client side only). Returns the problems."""

    def node(name: str, out: list[str], inp: list[str], note: str = "") -> dict[str, Any]:
        return {
            "name": name,
            "type": "Blur",
            "note": note,
            "sockets": {"input": {"Input_0": inp}, "output": {"Result": out}},
        }

    before = {
        "isCurrent": False,
        "nodes": [node(BASE_NODE, [GONE_NODE], []), node(GONE_NODE, [], [BASE_NODE])],
    }
    same = {"isCurrent": False, "nodes": list(reversed(before["nodes"]))}
    other = {
        "isCurrent": False,
        "nodes": [node(BASE_NODE, [], []), node(GONE_NODE, [], [])],
    }
    noted = {
        "isCurrent": False,
        "nodes": [node(BASE_NODE, [GONE_NODE], [], "x"), before["nodes"][1]],
    }
    problems = []
    if state_of(before) != state_of(same):
        problems.append(
            "state_of: reads that differ only in node order gave different states; expected equal"
        )
    if state_of(before) == state_of(other):
        problems.append(
            "state_of: a group without the link gave the same state; expected different"
        )
    if state_of(before) == state_of(noted):
        problems.append(
            "state_of: a group with a changed note gave the same state; expected different"
        )
    if links_of(before) != {(BASE_NODE, GONE_NODE)}:
        problems.append(f"links_of: got {sorted(links_of(before))}, expected one link")
    if names_of(before) != sorted([BASE_NODE, GONE_NODE]) or note_of(noted, BASE_NODE) != "x":
        problems.append("names_of / note_of: expected the two node names and the note x")
    if state_of(None) is not None or links_of(None) or names_of(None):
        problems.append("state_of / links_of / names_of of None: expected None or empty")
    return problems


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    ids: dict[str, Any] = {"uid": "uid-b", "group": "g_dry"}
    print("===== calls through flame_do =====")
    for call_id, (op_name, build) in CALLS.items():
        op = registry.get(op_name)
        problems: list[str] = []
        if op is None:
            print(f"NG {call_id}: unknown op {op_name}")
            failures += 1
            continue
        args = build(dict(ids))
        validation = validate_op_args(op, args)
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
        if op_name == "plan.run":
            for index, step in enumerate(args["steps"]):
                step_op = registry.get(step["op"])
                step_check = (
                    validate_op_args(step_op, step["args"]) if step_op is not None else None
                )
                if step_check is None or not step_check.ok:
                    problems.append(f"step {index} ({step['op']}) does not validate")
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
    print("===== comparison of two reads on made-up groups =====")
    problems = selftest_comparison()
    print("OK  comparison" if not problems else f"NG  comparison: {problems}")
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
    journal_dir = OUTPUT_DIR / f"journal_p5pb_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p5_plan_batch_e2e_{ts}.json",
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
