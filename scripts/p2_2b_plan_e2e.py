#!/usr/bin/env python3
"""Real-Flame end-to-end: `plan.run` (run with the `.venv` Python;
`scripts/run_e2e.sh p2_2b_plan` starts Flame).

Checks: `plan.run`: several writes in one plan share one journal group and one revert; a plan with
an invalid step runs nothing; a step that fails at run time stops the plan; a plan of reads can
continue on error; and a read-only server refuses a plan that writes.

Same shape and helpers as scripts/p2_2a_journal_e2e.py (imported from it): a real MCP stdio
client drives `python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport`
is scaffolding only (create the test objects, read every value back INDEPENDENTLY of the op
under test). Everything happens in the probe project `flame_mcp_probe`; the journal directory
is this run's own (`p2/journal_plan_e2e_<ts>/`, never the production one). No test hook is set
for any server. Values written are always of the type the attribute holds now (Blur's `pos_x`
is an INTEGER attribute on 2025.2.4: Flame and the op both refuse a float).

Stages
  a  three set_attributes steps (three Blur nodes' pos_x) in one plan -> all `landed`, ONE
     journal group holding three intents (journal.list: one group, label recorded), every value
     read back through transport -> ONE journal.revert puts all three back (transport reads),
     the group is `reverted`, a second revert is refused
  b  the third step names an op that does not exist -> INVALID_ARGS with issues=[{index: 2, ..}],
     NOTHING ran: the first two nodes' values are unchanged and the journal has no intent for them
  c  the second step fails at run time (Comp.blend_type = 'NoSuchMode': Flame's own value check)
     -> abort: stoppedAt=1, step 1 applied, step 3 not run (value unchanged), partial
     {done:1, failed:1, notRun:1}; journal.revert puts step 1 back
  d  read-only plan with on_error='continue': step 2 reads a node that does not exist
     (SELECTOR_EMPTY) and step 3 still runs
  e  a plan with a write and on_error='continue' -> INVALID_ARGS, nothing ran
  f  a READONLY server: a plan with a write is POLICY_DENIED (nothing ran), a plan of reads runs
  z  cross-cutting checks over every envelope (shape, logs/crash; no rider check: every op this
     script calls is a server-side op, whose envelope has no rider); nothing left undecided

Cleanup: no Batch group is deleted (flame.delete on a PyBatch is not used); then flame.exit().
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
    do_read,
    guarded,
    journal_index,
    node_locate,
    node_selector,
    rec,
    same,
    save,
    save_stage,
    server_session,
    state_of,
    tx,
    tx_read_pos,
    version_info,
)

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
BLUR_NODES: Final[int] = 7  # a: 0-2, b: 3-4, c: 3-4 (again, after b), e: 5, spare: 6
PLAN_LABEL_A: Final[str] = "p2_2b stage a"

Results = dict[str, Any]


def number_like(current: Any, n: int) -> int | float:
    """`n` as the kind of number the attribute holds now (an integer attribute takes an int)."""
    return n if isinstance(current, int) and not isinstance(current, bool) else float(n)


async def tx_read_attr(ctx: Ctx, name: str, key: str) -> Any:
    code = node_locate(ctx.ids["bgUid"], name) + f"\n_result = _v(getattr(_n, {key!r}))"
    br = await tx(ctx, code, label=f"p22b_read_{name}_{key}")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


def set_step(ctx: Ctx, name: str, key: str, value: Any) -> dict[str, Any]:
    return {
        "op": "node.set_attributes",
        "args": {"target": node_selector(ctx, name), "values": {key: value}},
    }


def intents_in(ctx: Ctx, group: str | None) -> list[dict[str, Any]]:
    if group is None:
        return []
    return [v.intent for v in journal_index(ctx).groups.get(group, [])]


def intents_touching(ctx: Ctx, names: list[str]) -> list[str]:
    """Names among `names` that any intent in the journal targets."""
    index = journal_index(ctx)
    hit: set[str] = set()
    for view in index.intents.values():
        target = view.intent.get("target")
        if isinstance(target, dict) and target.get("name") in names:
            hit.add(str(target["name"]))
    return sorted(hit)


async def prepare(ctx: Ctx) -> None:
    bg_name = f"P22B_BG_{ctx.ts}"
    code = "\n".join(
        [
            "desktop = flame.projects.current_project.current_workspace.desktop",
            f"bg = desktop.create_batch_group({bg_name!r}, nb_reels=1, nb_shelf_reels=1)",
            "names = []",
            f"for _i in range({BLUR_NODES}):",
            "    names.append(_v(bg.create_node('Blur').name))",
            "comp = _v(bg.create_node('Comp').name)",
            "_result = {'bgUid': _v(bg.uid), 'nodes': names, 'comp': comp}",
        ]
    )
    br = await tx(ctx, code, label="p22b_prepare", timeout_s=60.0)
    rec(ctx, "prepare_batch_and_nodes", br.ok, resp=_br_dict(br))
    if br.ok and isinstance(br.result, dict):
        ctx.ids.update(
            batchName=bg_name,
            bgUid=br.result["bgUid"],
            nodes=br.result["nodes"],
            comp=br.result["comp"],
        )
        names = [*br.result["nodes"], br.result["comp"]]
        rec(ctx, "prepare_node_names_are_distinct", len(set(names)) == len(names), nodes=names)


async def stage_a(ctx: Ctx) -> None:
    nodes = ctx.ids["nodes"][:3]
    before = [await tx_read_pos(ctx, n) for n in nodes]
    wanted = [number_like(b, 11 * (i + 1)) for i, b in enumerate(before)]
    async with server_session(ctx, "a-plain") as session:
        catalog = await session.call_tool("flame_catalog", {"op": "plan.run"})
        entry = _result_of(catalog.structured_content or {}).get("operation", {})
        rec(
            ctx,
            "a_catalog_has_plan_run_as_a_server_read_op",
            entry.get("thread") == "server"
            and entry.get("effect") == "read"
            and entry.get("category") == "plan"
            and "one group to undo" in str(entry.get("description")),
            thread=entry.get("thread"),
            effect=entry.get("effect"),
        )
        steps = [set_step(ctx, n, "pos_x", w) for n, w in zip(nodes, wanted, strict=True)]
        env = await do(session, ctx, "plan.run", {"steps": steps, "label": PLAN_LABEL_A})
        group = (env.get("journal") or {}).get("group")
        result = _result_of(env)
        rec(
            ctx,
            "a_plan_runs_all_three_steps",
            env.get("ok") is True
            and isinstance(group, str)
            and env.get("partial") == {"done": 3, "failed": 0, "notRun": 0}
            and env.get("affected") == 3
            and [s.get("ok") for s in result.get("steps", [])] == [True, True, True]
            and result.get("stoppedAt") is None,
            env=env,
        )
        reads = [await tx_read_pos(ctx, n) for n in nodes]
        rec(
            ctx,
            "a_independent_reads_are_the_new_values",
            all(same(r, w) for r, w in zip(reads, wanted, strict=True)),
            viaTransport=reads,
            wanted=wanted,
        )
        intents = intents_in(ctx, group)
        rec(
            ctx,
            "a_one_group_holds_three_intents",
            len(intents) == 3
            and [i.get("target", {}).get("name") for i in intents] == nodes
            and state_of(ctx, group) == "landed",
            intents=[i.get("id") for i in intents],
            state=state_of(ctx, group),
        )
        listing = await do(session, ctx, "journal.list", {"limit": 5})
        first = (_result_of(listing).get("items") or [{}])[0]
        rec(
            ctx,
            "a_list_shows_one_group_with_the_label",
            first.get("group") == group
            and first.get("intents") == 3
            and first.get("label") == PLAN_LABEL_A
            and first.get("state") == "landed",
            first=first,
        )
        back = await do(session, ctx, "journal.revert", {"group": group})
        restored = [await tx_read_pos(ctx, n) for n in nodes]
        rec(
            ctx,
            "a_one_revert_puts_all_three_back",
            back.get("ok") is True
            and all(same(r, b) for r, b in zip(restored, before, strict=True))
            and state_of(ctx, group) == "reverted",
            viaTransport=restored,
            before=before,
            env=back,
        )
        again = await do(session, ctx, "journal.revert", {"group": group})
        still = [await tx_read_pos(ctx, n) for n in nodes]
        rec(
            ctx,
            "a_a_second_revert_is_refused",
            again.get("ok") is False
            and all(same(r, b) for r, b in zip(still, before, strict=True)),
            env=again,
        )


async def stage_b(ctx: Ctx) -> None:
    n3, n4 = ctx.ids["nodes"][3], ctx.ids["nodes"][4]
    before = [await tx_read_pos(ctx, n) for n in (n3, n4)]
    async with server_session(ctx, "b-plain") as session:
        steps = [
            set_step(ctx, n3, "pos_x", number_like(before[0], 44)),
            set_step(ctx, n4, "pos_x", number_like(before[1], 55)),
            {"op": "node.set_attributez", "args": {}},
        ]
        env = await do(session, ctx, "plan.run", {"steps": steps})
        issues = _result_of(env).get("issues") or []
        rec(
            ctx,
            "b_invalid_third_step_refuses_the_whole_plan",
            env.get("ok") is False
            and env.get("errorCode") == "INVALID_ARGS"
            and [i.get("index") for i in issues] == [2]
            and issues[0].get("op") == "node.set_attributez"
            and _result_of(env).get("ran") is False,
            env=env,
        )
        after = [await tx_read_pos(ctx, n) for n in (n3, n4)]
        rec(
            ctx,
            "b_nothing_ran_values_unchanged",
            all(same(a, b) for a, b in zip(after, before, strict=True)),
            before=before,
            viaTransport=after,
        )
        rec(
            ctx,
            "b_the_journal_has_no_intent_for_those_nodes",
            intents_touching(ctx, [n3, n4]) == [],
            touched=intents_touching(ctx, [n3, n4]),
        )


async def stage_c(ctx: Ctx) -> None:
    n3, n4, comp = ctx.ids["nodes"][3], ctx.ids["nodes"][4], ctx.ids["comp"]
    p3 = await tx_read_pos(ctx, n3)
    p4 = await tx_read_pos(ctx, n4)
    blend0 = await tx_read_attr(ctx, comp, "blend_type")
    async with server_session(ctx, "c-plain") as session:
        steps = [
            set_step(ctx, n3, "pos_x", number_like(p3, 44)),
            set_step(ctx, comp, "blend_type", "NoSuchMode"),
            set_step(ctx, n4, "pos_x", number_like(p4, 55)),
        ]
        env = await do(session, ctx, "plan.run", {"steps": steps, "label": "p2_2b stage c"})
        result = _result_of(env)
        group = (env.get("journal") or {}).get("group")
        reported = result.get("steps", [])
        rec(
            ctx,
            "c_the_plan_aborts_at_the_failing_step",
            env.get("ok") is False
            and env.get("errorCode") == "INVALID_ARGS"
            and result.get("stoppedAt") == 1
            and [s.get("ok") for s in reported] == [True, False]
            and env.get("partial") == {"done": 1, "failed": 1, "notRun": 1}
            and "NoSuchMode" in str(reported[1].get("error") if len(reported) > 1 else ""),
            env=env,
        )
        v3 = await tx_read_pos(ctx, n3)
        v4 = await tx_read_pos(ctx, n4)
        blend = await tx_read_attr(ctx, comp, "blend_type")
        rec(
            ctx,
            "c_step_one_is_applied_two_and_three_changed_nothing",
            same(v3, number_like(p3, 44)) and same(v4, p4) and blend == blend0,
            n3=v3,
            n4=v4,
            blend=blend,
            blendBefore=blend0,
        )
        rec(
            ctx,
            "c_the_group_is_partial_and_step_three_left_no_intent",
            state_of(ctx, group) == "partial" and intents_touching(ctx, [n4]) == [],
            state=state_of(ctx, group),
        )
        back = await do(session, ctx, "journal.revert", {"group": group})
        restored = await tx_read_pos(ctx, n3)
        rec(
            ctx,
            "c_revert_puts_step_one_back",
            back.get("ok") is True and same(restored, p3) and state_of(ctx, group) == "reverted",
            viaTransport=restored,
            env=back,
        )


async def stage_d(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][0]
    missing = {
        "kind": "node",
        "batch": {"kind": "batch", "uid": ctx.ids["bgUid"]},
        "name": "no_such_node_p22b",
    }
    async with server_session(ctx, "d-plain") as session:
        steps = [
            {
                "op": "node.get_attributes",
                "args": {"target": node_selector(ctx, node), "keys": ["pos_x"]},
            },
            {"op": "node.get_attributes", "args": {"target": missing}},
            {"op": "batch.list", "args": {"limit": 5}},
        ]
        env = await do_read(session, ctx, "plan.run", {"steps": steps, "on_error": "continue"})
        reported = _result_of(env).get("steps", [])
        rec(
            ctx,
            "d_read_only_continue_runs_the_step_after_a_failure",
            env.get("ok") is True
            and len(reported) == 3
            and [s.get("ok") for s in reported] == [True, False, True]
            and reported[1].get("errorCode") == "SELECTOR_EMPTY"
            and isinstance(_result_of({"result": reported[2].get("result")}).get("total"), int)
            and env.get("partial") == {"done": 2, "failed": 1, "notRun": 0}
            and _result_of(env).get("stoppedAt") is None
            and env.get("journal") is None,
            env=env,
        )


async def stage_e(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][5]
    before = await tx_read_pos(ctx, node)
    async with server_session(ctx, "e-plain") as session:
        steps = [
            {"op": "batch.list", "args": {"limit": 3}},
            set_step(ctx, node, "pos_x", number_like(before, 66)),
        ]
        env = await do(session, ctx, "plan.run", {"steps": steps, "on_error": "continue"})
        after = await tx_read_pos(ctx, node)
        rec(
            ctx,
            "e_continue_with_a_write_is_refused_and_nothing_ran",
            env.get("ok") is False
            and env.get("errorCode") == "INVALID_ARGS"
            and _result_of(env).get("ran") is False
            and same(after, before)
            and intents_touching(ctx, [node]) == [],
            viaTransport=after,
            env=env,
        )


async def stage_f(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][6]
    before = await tx_read_pos(ctx, node)
    async with server_session(ctx, "f-readonly", readonly=True) as session:
        denied = await do(
            session,
            ctx,
            "plan.run",
            {
                "steps": [
                    {"op": "batch.list", "args": {"limit": 3}},
                    set_step(ctx, node, "pos_x", number_like(before, 77)),
                ]
            },
        )
        after = await tx_read_pos(ctx, node)
        rec(
            ctx,
            "f_readonly_refuses_a_plan_with_a_write",
            denied.get("errorCode") == "POLICY_DENIED"
            and [i.get("index") for i in _result_of(denied).get("issues", [])] == [1]
            and same(after, before),
            env=denied,
        )
        reads = await do_read(
            session,
            ctx,
            "plan.run",
            {"steps": [{"op": "batch.list", "args": {"limit": 3}}, {"op": "journal.list"}]},
        )
        rec(ctx, "f_readonly_runs_a_plan_of_reads", reads.get("ok") is True, env=reads)


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
        listing = await do(session, ctx, "journal.list", {"limit": 50})
        ctx.results["journalListing"] = _result_of(listing).get("items")
    check_envelope_shapes(ctx.results)
    # no check_rider: plan.run and journal.* are server-side ops, their envelopes carry no rider
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def cleanup(ctx: Ctx) -> None:
    ctx.stage = "cleanup"
    ctx.results["cleanup"] = {
        "deleted": [],
        "kept": {
            "batchGroup": ctx.ids.get("batchName"),
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them)"
            ),
        },
    }
    exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label="p22b_cleanup_exit")
    rec(
        ctx,
        "cleanup_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
    )


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "bgUid" in ctx.ids:
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("e", stage_e),
                ("f", stage_f),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_stages_a_to_f_skipped", False, ids=ctx.ids)
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
    journal_dir = OUTPUT_DIR / f"journal_plan_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_2b_plan_e2e_{ts}.json",
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
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
