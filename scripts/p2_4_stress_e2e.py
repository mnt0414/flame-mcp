#!/usr/bin/env python3
"""Load (burst) e2e THROUGH THE MCP SERVER (real Flame; run with the `.venv` Python;
`scripts/run_e2e.sh p2_4_stress` starts Flame). The transport on its own is put under load by
scripts/p1_stress_e2e.py; this drives `python -m flame_mcp` with a real MCP stdio client
so the server's own machinery (envelope, journal, write lock, checkpoints of the setup ops) is
under load too.

Checks: The server under load: back-to-back reads, a long run of journaled writes followed by their
reverts, and writes from two servers at once, then that no journal entry is left undecided.

Groups (created through transport, scaffolding only; none is current, the decoy group is created
last): S holds 3 Blur nodes, P1 and P2 hold one each. The Blur nodes are created BY THE OPS
(node.create). `flame_mcp.transport` also READS BACK every value the judgements rest on,
independently of the ops under test.

Stages
  a  reads: READS calls back to back on ONE server session, cycling through batch.list, node.list,
     node.get_attributes, batch.info, flame_catalog and flame_context. JUDGED: every envelope ok.
     OBSERVED: time per call (this client's wall clock and the server's `durationMs`): count,
     min, median, p95, max, per kind and overall.
  b  journaled writes: WRITES node.set_attributes of S's first Blur `pos_x` (an integer attribute:
     start+1, start+2, ...), one after another; then journal.revert of ALL of them in reverse
     order. JUDGED: every write / revert ok; each write's before is the value the previous one
     left; transport reads back start+WRITES after the writes and `start` after the reverts;
     the op's own read-back agrees; every journal group of the run is `landed` or `reverted`
     (nothing unknown / failed / conflict / partial), exactly WRITES groups are `reverted`.
  c  two servers at once: two MCP server subprocesses are opened first, then PARALLEL_WRITES
     node.set_attributes each are sent to a group of its own (P1 / P2), concurrently (anyio
     tasks, started together). JUDGED: every write ok; each session's before-chain unbroken;
     transport reads back start+PARALLEL_WRITES for both; the journal holds exactly
     PARALLEL_WRITES intents per node, all landed; both servers agree nothing is left undecided.
     OBSERVED: time per call per session, the wall time of the whole stage.
  z  cross-cutting checks over every envelope; nothing left undecided

A BRIDGE_LOST / BRIDGE_UNAVAILABLE / TIMEOUT answer is recorded (which call, how many calls had
gone through) and stops the run: the stage ends, later stages are skipped, and the run is not ok.
The runner script separately checks shell.log for `abnormal termination`.

Counts are constants (READS, WRITES, PARALLEL_WRITES) and can be changed:
  .venv/bin/python scripts/p2_4_stress_e2e.py --reads 300 --writes 100 --parallel-writes 50

Cleanup: no Batch group is deleted (flame.delete on a PyBatch is not used); then flame.exit()
(BRIDGE_UNAVAILABLE retried every 3 s for up to 180 s).
"""

from __future__ import annotations

import argparse
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import anyio
import e2e_guard
from p1_mcp_e2e import (
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
    guarded,
    rec,
    same,
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
    journal_records,
    journal_states,
    observe,
    ops_used,
    print_observations,
    summarize_ms,
    tx_is_current,
    tx_scaffold,
    value_like,
)

Results = dict[str, Any]

READS: Final[int] = 300
WRITES: Final[int] = 100
PARALLEL_WRITES: Final[int] = 50
READBACK_EVERY: Final[int] = 10
"""During stage b every this-many writes, transport reads the value back independently (and after
the last one)."""
FATAL_CODES: Final[frozenset[str]] = frozenset({"BRIDGE_LOST", "BRIDGE_UNAVAILABLE", "TIMEOUT"})
READ_KINDS: Final[tuple[str, ...]] = (
    "batch.list",
    "node.list",
    "node.get_attributes",
    "batch.info",
    "flame_catalog",
    "flame_context",
)
GROUPS: Final[tuple[str, ...]] = ("S", "P1", "P2")
BLURS: Final[dict[str, tuple[str, ...]]] = {
    "S": ("stress_blur_1", "stress_blur_2", "stress_blur_3"),
    "P1": ("par1_blur",),
    "P2": ("par2_blur",),
}
WRITE_KEY: Final[str] = "pos_x"
NOISE_TOOLS: Final[frozenset[str]] = frozenset({"flame_catalog", "flame_context"})


class StressAbort(Exception):
    """A fatal answer (BRIDGE_LOST / BRIDGE_UNAVAILABLE / TIMEOUT): the run stops."""


@dataclass(frozen=True)
class Sample:
    kind: str
    wall_ms: float
    ok: bool
    error_code: str | None
    server_ms: int | None
    env: dict[str, Any]


def uid_of(ctx: Ctx, group: str) -> str:
    return str(ctx.ids["groups"][group]["uid"])


def target_of(ctx: Ctx, group: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid_of(ctx, group)}


def node_sel(ctx: Ctx, group: str, name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": target_of(ctx, group), "name": name}


def writer_node(group: str) -> str:
    return BLURS[group][0]


async def timed(session: Any, ctx: Ctx, kind: str, args: dict[str, Any] | None = None) -> Sample:
    """One call, timed on this client's clock. A fatal error code raises StressAbort after it is
    recorded (the caller decides what to record about the run)."""
    started = time.perf_counter()
    if kind in NOISE_TOOLS:
        result = await session.call_tool(kind, args or {})
        env = _structured(result, tool=kind, results=ctx.results)
    else:
        env = await do(session, ctx, kind, args)
    wall = round((time.perf_counter() - started) * 1000, 2)
    sample = Sample(
        kind, wall, env.get("ok") is True, env.get("errorCode"), env.get("durationMs"), env
    )
    if sample.error_code in FATAL_CODES:
        ctx.results.setdefault("aborts", []).append(
            {
                "stage": ctx.stage,
                "kind": kind,
                "errorCode": sample.error_code,
                "error": env.get("error"),
                "wallMs": wall,
            }
        )
        raise StressAbort(f"{kind}: {sample.error_code}")
    return sample


def timing_table(samples: list[Sample]) -> dict[str, Any]:
    kinds = sorted({s.kind for s in samples})
    return {
        "overall": {
            "clientMs": summarize_ms([s.wall_ms for s in samples]),
            "serverMs": summarize_ms([float(s.server_ms) for s in samples if s.server_ms]),
        },
        **{
            kind: {
                "clientMs": summarize_ms([s.wall_ms for s in samples if s.kind == kind]),
                "serverMs": summarize_ms(
                    [float(s.server_ms) for s in samples if s.kind == kind and s.server_ms]
                ),
            }
            for kind in kinds
        },
    }


def compact(table: dict[str, Any]) -> dict[str, Any]:
    """The `overall` part of a timing table (client and server milliseconds)."""
    return dict(table["overall"])


def failures(samples: list[Sample]) -> list[dict[str, Any]]:
    return [
        {"kind": s.kind, "errorCode": s.error_code, "error": s.env.get("error")}
        for s in samples
        if not s.ok
    ][:10]


def slim_noise_envelopes(ctx: Ctx) -> None:
    """flame_catalog / flame_context answer tens of KB each; keep the envelope's keys (the shape
    check needs them) and drop their `result` from the recorded copies."""
    for entry in ctx.results.get("_envelopes", []):
        env = entry.get("env")
        if entry.get("tool") in NOISE_TOOLS and isinstance(env, dict) and env.get("result"):
            env["result"] = {"omitted": True}


# Transport read-backs (independent of the ops under test)
async def tx_pos(ctx: Ctx, group: str, node: str, key: str = WRITE_KEY) -> Any:
    body = f"_n = [n for n in _bg.nodes if _v(n.name) == {node!r}][0]\n_result = _v(_n.{key})"
    return await tx_scaffold(ctx, uid_of(ctx, group), body, f"p24st_pos_{group}")


async def tx_node_names(ctx: Ctx, group: str) -> Any:
    body = "_result = sorted(_v(n.name) for n in _bg.nodes)"
    return await tx_scaffold(ctx, uid_of(ctx, group), body, f"p24st_names_{group}")


def sample_dict(sample: Sample) -> dict[str, Any]:
    return {"kind": sample.kind, "wallMs": sample.wall_ms, "ok": sample.ok}


async def prepare(ctx: Ctx) -> None:
    lines = [
        "desktop = flame.projects.current_project.current_workspace.desktop",
        "groups = {}",
    ]
    for group in GROUPS:
        lines += [
            f"bg = desktop.create_batch_group('P24ST_{group}_{ctx.ts}', nb_reels=1, "
            "nb_shelf_reels=1)",
            f"groups[{group!r}] = {{'uid': _v(bg.uid), 'name': _v(bg.name)}}",
        ]
    lines += [
        # created last: it becomes the current group, so none of the test groups is current
        f"decoy = desktop.create_batch_group('P24ST_DECOY_{ctx.ts}', nb_reels=1, nb_shelf_reels=1)",
        "_result = {'groups': groups, 'decoyUid': _v(decoy.uid)}",
    ]
    br = await tx(ctx, "\n".join(lines), label="p24st_prepare_groups", timeout_s=90.0)
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


async def prepare_build(ctx: Ctx) -> None:
    """The Blur nodes, created with node.create."""
    async with server_session(ctx, "prepare-build") as session:
        for group in GROUPS:
            envs = [
                await do(
                    session,
                    ctx,
                    "node.create",
                    {"target": target_of(ctx, group), "type": "Blur", "name": name},
                )
                for name in BLURS[group]
            ]
            names = await tx_node_names(ctx, group)
            rec(
                ctx,
                f"prepare_{group.lower()}_blur_nodes_created_by_node_create",
                all(e.get("ok") is True for e in envs) and names == sorted(BLURS[group]),
                names=names,
                errors=[e.get("error") for e in envs if not e.get("ok")],
            )
    start = {g: await tx_pos(ctx, g, writer_node(g)) for g in GROUPS if g in ctx.ids["groups"]}
    numeric = len(start) == len(GROUPS) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in start.values()
    )
    rec(ctx, "prepare_start_values_are_numbers", numeric, pos_x=start)
    if numeric:
        ctx.ids["start"] = start


# stage a: reads
def read_args(ctx: Ctx, kind: str) -> dict[str, Any] | None:
    match kind:
        case "batch.list":
            return {"limit": 5}
        case "node.list" | "batch.info":
            return {"target": target_of(ctx, "S")}
        case "node.get_attributes":
            return {
                "target": node_sel(ctx, "S", writer_node("S")),
                "keys": ["pos_x", "pos_y"],
            }
        case _:
            return None


async def stage_a(ctx: Ctx, reads: int) -> None:
    samples: list[Sample] = []
    started = time.perf_counter()
    async with server_session(ctx, "a-reads") as session:
        try:
            for i in range(reads):
                kind = READ_KINDS[i % len(READ_KINDS)]
                samples.append(await timed(session, ctx, kind, read_args(ctx, kind)))
        except StressAbort as exc:
            ctx.ids["aborted"] = str(exc)
    wall = round(time.perf_counter() - started, 2)
    slim_noise_envelopes(ctx)
    ctx.results.setdefault("timings", {})["reads"] = timing_table(samples)
    rec(
        ctx,
        "a_every_read_answered_ok",
        len(samples) == reads and all(s.ok for s in samples) and "aborted" not in ctx.ids,
        calls=len(samples),
        expected=reads,
        failures=failures(samples),
        aborted=ctx.ids.get("aborted"),
    )
    observe(
        ctx,
        "a_read_timing",
        wallS=wall,
        callsPerS=round(len(samples) / wall, 2) if wall else None,
        overall=compact(ctx.results["timings"]["reads"]),
    )


# stage b: journaled writes, then all of them reverted
def _chain_ok(env: dict[str, Any], previous: Any, wanted: Any) -> bool:
    result = _result_of(env)
    return (
        env.get("ok") is True
        and same((result.get("before") or {}).get(WRITE_KEY), previous)
        and same((result.get("after") or {}).get(WRITE_KEY), wanted)
        and isinstance((env.get("journal") or {}).get("group"), str)
    )


async def stage_b(ctx: Ctx, writes: int) -> None:
    group, node = "S", writer_node("S")
    start = ctx.ids["start"][group]
    write_samples: list[Sample] = []
    revert_samples: list[Sample] = []
    groups: list[str] = []
    chain_breaks: list[dict[str, Any]] = []
    readback_misses: list[dict[str, Any]] = []
    previous: Any = start
    async with server_session(ctx, "b-writes") as session:
        try:
            for i in range(1, writes + 1):
                wanted = value_like(start, start + i)
                sample = await timed(
                    session,
                    ctx,
                    "node.set_attributes",
                    {"target": node_sel(ctx, group, node), "values": {WRITE_KEY: wanted}},
                )
                write_samples.append(sample)
                if not _chain_ok(sample.env, previous, wanted):
                    chain_breaks.append({"i": i, "error": sample.env.get("error")})
                    break
                previous = wanted
                groups.append(str((sample.env.get("journal") or {})["group"]))
                if i % READBACK_EVERY == 0 or i == writes:
                    seen = await tx_pos(ctx, group, node)
                    if not same(seen, wanted):
                        readback_misses.append({"i": i, "wanted": wanted, "transport": seen})
        except StressAbort as exc:
            ctx.ids["aborted"] = str(exc)
        after_writes = await tx_pos(ctx, group, node)
        rec(
            ctx,
            "b_every_write_ok_and_the_before_chain_is_unbroken",
            len(write_samples) == writes
            and not chain_breaks
            and "aborted" not in ctx.ids
            and len(set(groups)) == writes,
            calls=len(write_samples),
            expected=writes,
            journalGroups=len(set(groups)),
            chainBreaks=chain_breaks[:5],
            failures=failures(write_samples),
            aborted=ctx.ids.get("aborted"),
        )
        rec(
            ctx,
            "b_transport_reads_the_last_value_back_and_agrees_along_the_way",
            not readback_misses and same(after_writes, start + writes),
            afterWrites=after_writes,
            expected=start + writes,
            misses=readback_misses[:5],
        )
        if "aborted" in ctx.ids or chain_breaks:
            _finish_b(ctx, write_samples, revert_samples)
            return
        try:
            for group_id in reversed(groups):
                revert_samples.append(
                    await timed(session, ctx, "journal.revert", {"group": group_id})
                )
        except StressAbort as exc:
            ctx.ids["aborted"] = str(exc)
        after_reverts = await tx_pos(ctx, group, node)
        via_op = _result_of(
            await do(
                session,
                ctx,
                "node.get_attributes",
                {"target": node_sel(ctx, group, node), "keys": [WRITE_KEY]},
            )
        )
        op_value = ((via_op.get("attributes") or {}).get(WRITE_KEY) or {}).get("value")
        rec(
            ctx,
            "b_every_revert_ok_in_reverse_order",
            len(revert_samples) == len(groups)
            and all(s.ok for s in revert_samples)
            and "aborted" not in ctx.ids,
            calls=len(revert_samples),
            expected=len(groups),
            failures=failures(revert_samples),
            aborted=ctx.ids.get("aborted"),
        )
        rec(
            ctx,
            "b_transport_and_the_op_read_the_start_value_after_the_reverts",
            same(after_reverts, start) and same(op_value, start),
            start=start,
            viaTransport=after_reverts,
            viaOp=op_value,
        )
        states = journal_states(ctx)
        counts = {s: list(states.values()).count(s) for s in sorted(set(states.values()))}
        rec(
            ctx,
            "b_the_journal_has_only_landed_and_reverted_groups",
            set(states) >= set(groups)
            and set(states.values()) <= GOOD_JOURNAL_STATES
            and sum(1 for g in groups if states.get(g) == "reverted") == len(groups),
            counts=counts,
            groups=len(states),
        )
        listing = await do(session, ctx, "journal.list", {"limit": 5})
        rec(
            ctx,
            "b_journal_list_answers_after_the_burst",
            listing.get("ok") is True,
            total=_result_of(listing).get("total"),
        )
    _finish_b(ctx, write_samples, revert_samples)


def _finish_b(ctx: Ctx, writes: list[Sample], reverts: list[Sample]) -> None:
    table = {"writes": timing_table(writes), "reverts": timing_table(reverts)}
    ctx.results.setdefault("timings", {}).update(table)
    observe(ctx, "b_write_and_revert_timing", **{k: compact(v) for k, v in table.items()})


# stage c: two servers writing at once
@dataclass
class Worker:
    label: str
    group: str
    samples: list[Sample]
    chain_breaks: list[dict[str, Any]]
    groups: list[str]
    aborted: str | None = None


async def run_worker(
    session: Any,
    ctx: Ctx,
    worker: Worker,
    writes: int,
    start: Any,
    go: anyio.Event,
    halt: list[str],
) -> None:
    node = writer_node(worker.group)
    previous: Any = start
    await go.wait()
    try:
        for i in range(1, writes + 1):
            if halt:
                return
            wanted = value_like(start, start + i)
            sample = await timed(
                session,
                ctx,
                "node.set_attributes",
                {"target": node_sel(ctx, worker.group, node), "values": {WRITE_KEY: wanted}},
            )
            worker.samples.append(sample)
            if not _chain_ok(sample.env, previous, wanted):
                worker.chain_breaks.append({"i": i, "error": sample.env.get("error")})
                halt.append(f"{worker.label}: write {i} not ok")
                return
            previous = wanted
            worker.groups.append(str((sample.env.get("journal") or {})["group"]))
    except StressAbort as exc:
        worker.aborted = str(exc)
        halt.append(str(exc))


async def stage_c(ctx: Ctx, writes: int) -> None:
    starts = {g: ctx.ids["start"][g] for g in ("P1", "P2")}
    workers = [Worker(f"server-{g.lower()}", g, [], [], []) for g in ("P1", "P2")]
    halt: list[str] = []
    started = 0.0
    wall = 0.0
    async with (
        server_session(ctx, "c-par-1") as first,
        server_session(ctx, "c-par-2") as second,
    ):
        go = anyio.Event()
        async with anyio.create_task_group() as tg:
            for worker, session in zip(workers, (first, second), strict=True):
                tg.start_soon(
                    run_worker, session, ctx, worker, writes, starts[worker.group], go, halt
                )
            await anyio.sleep(0)
            started = time.perf_counter()
            go.set()
        wall = round(time.perf_counter() - started, 2)
        infos = [_result_of(await version_info(s, ctx)).get("journal", {}) for s in (first, second)]
    if halt:
        ctx.ids["aborted"] = "; ".join(halt)
    slim_noise_envelopes(ctx)
    all_samples = [s for w in workers for s in w.samples]
    ctx.results.setdefault("timings", {})["parallel"] = {
        "all": timing_table(all_samples),
        **{w.label: timing_table(w.samples) for w in workers},
    }
    finals = {w.group: await tx_pos(ctx, w.group, writer_node(w.group)) for w in workers}
    rec(
        ctx,
        "c_every_parallel_write_ok_and_each_before_chain_is_unbroken",
        all(len(w.samples) == writes and not w.chain_breaks and not w.aborted for w in workers)
        and not halt
        and all(len(set(w.groups)) == writes for w in workers),
        calls={w.label: len(w.samples) for w in workers},
        expected=writes,
        halt=halt,
        chainBreaks={w.label: w.chain_breaks[:3] for w in workers},
        failures=failures(all_samples),
    )
    rec(
        ctx,
        "c_transport_reads_both_final_values_back",
        all(same(finals[w.group], starts[w.group] + writes) for w in workers),
        finals=finals,
        expected={g: starts[g] + writes for g in starts},
    )
    records = journal_records(ctx)
    intents = {
        w.group: [
            r
            for r in records
            if r.get("type") == "intent"
            and r.get("op") == "node.set_attributes"
            and (r.get("target") or {}).get("name") == writer_node(w.group)
        ]
        for w in workers
    }
    states = journal_states(ctx)
    rec(
        ctx,
        "c_the_shared_journal_holds_every_intent_of_both_servers_and_all_landed",
        all(len(intents[w.group]) == writes for w in workers)
        and all(states.get(g) == "landed" for w in workers for g in w.groups),
        intents={g: len(v) for g, v in intents.items()},
        states=sorted({states.get(g, "?") for w in workers for g in w.groups}),
    )
    rec(
        ctx,
        "c_neither_server_sees_an_undecided_intent_or_blocked_writes",
        all(i.get("unresolvedIntents") == 0 and i.get("writesBlocked") is False for i in infos),
        journals=[{k: i.get(k) for k in ("unresolvedIntents", "writesBlocked")} for i in infos],
    )
    observe(
        ctx,
        "c_parallel_timing",
        wallS=wall,
        callsPerS=round(len(all_samples) / wall, 2) if wall else None,
        all=compact(ctx.results["timings"]["parallel"]["all"]),
        perServer={w.label: compact(ctx.results["timings"]["parallel"][w.label]) for w in workers},
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
    states = journal_states(ctx)
    bad = {g: s for g, s in states.items() if s not in GOOD_JOURNAL_STATES}
    rec(
        ctx,
        "z_no_journal_group_of_the_run_is_unknown_failed_conflict_or_partial",
        not bad,
        groups=len(states),
        bad=dict(list(bad.items())[:10]),
    )
    rec(
        ctx,
        "z_no_fatal_answer_was_recorded",
        not ctx.results.get("aborts"),
        aborts=ctx.results.get("aborts", []),
    )
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def cleanup(ctx: Ctx) -> None:
    ctx.results["cleanup"] = {
        "deleted": [],
        "kept": {
            "batchGroups": ctx.ids.get("groups"),
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them)"
            ),
        },
    }
    flame_exit(ctx, "p24st_cleanup_exit")


def print_timings(results: Results) -> None:
    print("--- timings (ms; this client's wall clock, then the server's durationMs) ---")
    for stage, table in results.get("timings", {}).items():
        overall = compact(table["all"] if stage == "parallel" else table)
        print(f"{stage}: client={overall['clientMs']}")
        print(f"{' ' * len(stage)}  server={overall['serverMs']}")


async def async_main(ctx: Ctx, reads: int, writes: int, parallel_writes: int) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "groups" in ctx.ids:
            await guarded(ctx, "prepare_build", prepare_build)
        if "start" in ctx.ids:
            for name, stage in (
                ("a", lambda c: stage_a(c, reads)),
                ("b", lambda c: stage_b(c, writes)),
                ("c", lambda c: stage_c(c, parallel_writes)),
            ):
                if "aborted" in ctx.ids:
                    rec(
                        ctx,
                        f"stage_{name}_skipped_after_a_fatal_answer",
                        False,
                        aborted=ctx.ids["aborted"],
                    )
                    continue
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_stages_a_to_c_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        cleanup(ctx)
        save_stage(ctx)
        save(ctx.results)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--reads", type=int, default=READS)
    parser.add_argument("--writes", type=int, default=WRITES)
    parser.add_argument("--parallel-writes", type=int, default=PARALLEL_WRITES)
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_p24st_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "counts": {
            "reads": args.reads,
            "writes": args.writes,
            "parallelWritesPerSession": args.parallel_writes,
        },
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_4_stress_e2e_{ts}.json",
    }
    ctx = Ctx(run_dir, results, ts, journal_dir)
    try:
        anyio.run(async_main, ctx, args.reads, args.writes, args.parallel_writes)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()
    save(results)
    failed = [s["name"] for s in results["steps"] if not s["ok"]]
    print(f"Result: {results['_out_path']}")
    print(f"steps={len(results['steps'])} failed={len(failed)}: {failed}")
    print_timings(results)
    print_observations(results)
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
