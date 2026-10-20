#!/usr/bin/env python3
"""Real-Flame end-to-end: the write journal, the compare-and-swap, and how a write whose
outcome is unknown is told apart afterwards (run with the `.venv` Python;
`scripts/run_e2e.sh p2_2a_journal` starts Flame).

Checks: The write journal and its compare-and-swap check: a write, its journal entries and its
revert; a conflict when a value changes behind the server's back; how a write with an unknown
outcome is told apart afterwards; and that the server refuses writes after a timeout.

Same shape as scripts/p2_1_attr_e2e.py: a real MCP stdio client drives `python -m flame_mcp`
against a live Flame + dev bridge, and `flame_mcp.transport.exec` is used only as scaffolding
(create the test objects, read a value back INDEPENDENTLY of the op under test, write a value
"behind the server's back", clean up). Everything happens in the probe project
`flame_mcp_probe`. Each stage starts its OWN server
subprocess(es) with the environment that stage needs -- the fault-injection variables
(FLAME_MCP_TEST_PAUSE_AFTER_PREREAD / FLAME_MCP_TEST_EXIT_AFTER_WRITE /
FLAME_MCP_TEST_WRITE_TIMEOUT_S) are set for exactly the subprocess that needs them and never
inherited; every subprocess's environment is recorded in results["servers"]. All servers of
one run share one journal directory (FLAME_MCP_JOURNAL_DIR, under the p2/ output directory,
NOT the production journal), so a server started after another died can read what it left.

Stages (each one ends by saving its own JSON as well as the running one)
  a  a fresh server: version_info reports the journal and NO test hook; the catalog equals the
     registry. Node N_a pos_x: set_attributes -> ok, journal {group, id} -> independent read is
     the new value -> journal.list says `landed` -> journal.show has intent + outcome ->
     journal.revert -> independent read is the previous value -> the group is `reverted` ->
     a SECOND revert of the same group is refused and the value is untouched. Then the same
     round trip for desktop.rename (a Desktop reel).
  b  CAS: a server with FLAME_MCP_TEST_PAUSE_AFTER_PREREAD=3 is asked to set N_b pos_x; while it
     waits (its intent is already in the journal, the value in Flame is still the old one) the
     script writes a different value through transport. The call ends in CONFLICT, the value is
     the one transport wrote (the op wrote nothing), the group is `conflict`.
  c  revert refusal: set N_c pos_x; change it through transport; journal.revert is refused
     (CONFLICT, per-key expected/current) and the value is left as transport wrote it.
  d  telling landed from not landed after the server died
     d1  FLAME_MCP_TEST_EXIT_AFTER_WRITE=1: the server exits after the write's answer, before the
         outcome is journaled. Independent read: the write landed. A NEW server: version_info
         counts 1 unresolved intent; journal.list says `unknown`; journal.check -> `landed`.
     d2  FLAME_MCP_TEST_PAUSE_AFTER_PREREAD=30: kill -9 the server while it waits (its intent is
         in the journal, nothing was written). Independent read: unchanged. A NEW server:
         journal.check -> `not_landed`.
  e  fail closed, inside ONE server process: FLAME_MCP_TEST_WRITE_TIMEOUT_S=0.001 makes the first
     write end in TIMEOUT. Then: a write -> POLICY_DENIED, journal.revert -> POLICY_DENIED,
     reads (batch.list / node.get_attributes / journal.list) still work, version_info says
     writes are blocked; journal.check reads Flame again and its verdict equals what an
     independent transport read says; then writes go through again.
  f  a READONLY server: journal.revert is neither offered nor callable; list / show / check are.
  z  cross-cutting checks over every envelope, and a last look at the journal (nothing left
     undecided, no test hook active on a plain server)

Cleanup deletes ONLY the Desktop reel (flame.delete on a Desktop reel is measured safe); Batch
groups and the Library are left under their P22A_ prefixes (flame.delete on PyBatch / PyLibrary
is not used). Then flame.exit().
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, Final

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import e2e_guard  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
    _br_dict,
    _result_of,
    _structured,
    _tx,
    check_envelope_shapes,
    check_logs_and_crash,
    check_rider,
)

from flame_mcp.journal import Index, Journal  # noqa: E402

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
TEST_ENV_PREFIX: Final[str] = "FLAME_MCP_TEST_"
ENV_PAUSE: Final[str] = "FLAME_MCP_TEST_PAUSE_AFTER_PREREAD"
ENV_EXIT: Final[str] = "FLAME_MCP_TEST_EXIT_AFTER_WRITE"
ENV_WRITE_TIMEOUT: Final[str] = "FLAME_MCP_TEST_WRITE_TIMEOUT_S"
JOURNAL_OPS: Final[frozenset[str]] = frozenset(
    {"journal.list", "journal.show", "journal.check", "journal.revert"}
)
NODE_NAMES_NEEDED: Final[int] = 7  # a, b, c, d1, d2, e, spare
CALL_TIMEOUT_S: Final[float] = 60.0

Results = dict[str, Any]

V_PRELUDE: Final[str] = (
    "def _v(x):\n    return x.get_value() if type(x).__name__ == 'PyAttribute' else x"
)


class Ctx:
    def __init__(self, run_dir: Path, results: Results, ts: str, journal_dir: Path) -> None:
        self.run_dir = run_dir
        self.results = results
        self.ts = ts
        self.journal_dir = journal_dir
        self.ids: dict[str, Any] = {}
        self.stage = "-"
        self.stage_steps_from = 0


def save(results: Results) -> None:
    path = results.get("_out_path")
    if isinstance(path, Path):
        path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


def rec(ctx: Ctx, name: str, ok: bool, **extra: Any) -> None:
    ok = bool(ok)
    ctx.results["steps"].append({"name": name, "ok": ok, "stage": ctx.stage, **extra})
    if not ok:
        ctx.results["ok"] = False
    save(ctx.results)


def judged(**clauses: Any) -> tuple[bool, dict[str, bool]]:
    """(all clauses hold, {clause: bool}) -- pass the second to `rec` as `clauses=` so a step that
    judges several conditions at once shows which of them failed."""
    parts = {name: bool(value) for name, value in clauses.items()}
    return all(parts.values()), parts


def save_stage(ctx: Ctx) -> None:
    """Every stage also leaves its own JSON (its steps and the servers it started)."""
    steps = ctx.results["steps"][ctx.stage_steps_from :]
    ctx.stage_steps_from = len(ctx.results["steps"])
    out_path = ctx.results["_out_path"]
    if not isinstance(out_path, Path):
        return
    stage_path = out_path.with_name(f"{out_path.stem}_stage_{ctx.stage}.json")
    servers = [s for s in ctx.results.get("servers", []) if s.get("stage") == ctx.stage]
    stage_path.write_text(
        json.dumps(
            {"stage": ctx.stage, "steps": steps, "servers": servers, "ids": ctx.ids},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= 1e-6
    return bool(a == b)


@asynccontextmanager
async def server_session(
    ctx: Ctx,
    label: str,
    *,
    hooks: dict[str, str] | None = None,
    readonly: bool = False,
    eval_enabled: bool = True,
) -> AsyncIterator[ClientSession]:
    """`python -m flame_mcp` as a subprocess with exactly this stage's environment. Every
    FLAME_MCP_TEST_* variable of the parent is dropped first; the ones in `hooks` are added
    back for this subprocess only. The environment is recorded in results["servers"].

    eval.run exists only with FLAME_MCP_ENABLE_EVAL=1, so the server gets it unless `readonly` or
    `eval_enabled` is False (the variable is then removed even if the parent has it)."""
    env = dict(os.environ)
    stripped = sorted(name for name in env if name.startswith(TEST_ENV_PREFIX))
    for name in stripped:
        del env[name]
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["FLAME_MCP_RUN_DIR"] = str(ctx.run_dir)
    env["FLAME_MCP_JOURNAL_DIR"] = str(ctx.journal_dir)
    if readonly:
        env["FLAME_MCP_READONLY"] = "1"
    else:
        env.pop("FLAME_MCP_READONLY", None)
    with_eval = eval_enabled and not readonly
    if with_eval:
        env["FLAME_MCP_ENABLE_EVAL"] = "1"
    else:
        env.pop("FLAME_MCP_ENABLE_EVAL", None)
    env.update(hooks or {})
    ctx.results.setdefault("servers", []).append(
        {
            "stage": ctx.stage,
            "label": label,
            "hooks": dict(hooks or {}),
            "readonly": readonly,
            "evalEnabled": env.get("FLAME_MCP_ENABLE_EVAL") == "1",
            "strippedInheritedTestEnv": stripped,
            "journalDir": str(ctx.journal_dir),
            "runDir": str(ctx.run_dir),
            "testEnvActuallySet": sorted(n for n in env if n.startswith(TEST_ENV_PREFIX)),
        }
    )
    save(ctx.results)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "flame_mcp"], env=env, cwd=str(REPO_ROOT)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def do(
    session: ClientSession, ctx: Ctx, op: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"op": op}
    if args is not None:
        payload["args"] = args
    with anyio.fail_after(CALL_TIMEOUT_S):
        result = await session.call_tool("flame_do", payload)
    return _structured(result, tool="flame_do", results=ctx.results)


def timed_out(env: dict[str, Any]) -> bool:
    """The call answered TIMEOUT, or a `plan.run` child step did."""
    if env.get("errorCode") == "TIMEOUT":
        return True
    steps = _result_of(env).get("steps")
    return isinstance(steps, list) and any(
        isinstance(s, dict) and s.get("errorCode") == "TIMEOUT" for s in steps
    )


async def do_read(
    session: ClientSession, ctx: Ctx, op: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`do` for a READ-ONLY call (a read op, or a plan of reads only): the first read on a
    project that has piled up Batch groups can answer TIMEOUT while Flame walks them all. A
    TIMEOUT is read once more (safe: nothing is written) and what the first read did is recorded
    as an observation; a second failure is returned as it is and fails the step that asked."""
    started = time.monotonic()
    first = await do(session, ctx, op, args)
    first_ms = round((time.monotonic() - started) * 1000)
    if not timed_out(first):
        return first
    started = time.monotonic()
    second = await do(session, ctx, op, args)
    rec(
        ctx,
        f"observation_read_retried_after_timeout_{op.replace('.', '_')}",
        True,
        kind="observation",
        op=op,
        firstMs=first_ms,
        firstErrorCode=first.get("errorCode"),
        firstTimeoutPhase=first.get("timeoutPhase"),
        retryMs=round((time.monotonic() - started) * 1000),
        retryOk=second.get("ok"),
        retryErrorCode=second.get("errorCode"),
    )
    return second


async def version_info(session: ClientSession, ctx: Ctx) -> dict[str, Any]:
    with anyio.fail_after(CALL_TIMEOUT_S):
        result = await session.call_tool("flame_version_info", {})
    return _structured(result, tool="flame_version_info", results=ctx.results)


def journal_of(env: dict[str, Any]) -> dict[str, Any]:
    j = env.get("journal")
    return j if isinstance(j, dict) else {}


# Transport scaffolding (never through the op under test)
async def tx(ctx: Ctx, code: str, *, label: str, timeout_s: float = 20.0) -> Any:
    return await anyio.to_thread.run_sync(
        partial(_tx, V_PRELUDE + "\n" + code, ctx.run_dir, timeout_s=timeout_s, label=label)
    )


def node_locate(uid: str, name: str) -> str:
    return "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            f"_bg = [g for g in _ws.desktop.batch_groups if _v(g.uid) == {uid!r}][0]",
            f"_n = [n for n in _bg.nodes if _v(n.name) == {name!r}][0]",
        ]
    )


async def tx_read_pos(ctx: Ctx, name: str) -> Any:
    code = node_locate(ctx.ids["bgUid"], name) + "\n_result = _v(_n.pos_x)"
    br = await tx(ctx, code, label=f"p22a_read_{name}")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_write_pos(ctx: Ctx, name: str, value: int) -> Any:
    # pos_x of a Blur node is an INTEGER attribute on 2025.2.4: Flame itself refuses a float
    # (TypeError: ... C++ rvalue of type int ...), and so does the op.
    # Write the type it holds.
    code = (
        node_locate(ctx.ids["bgUid"], name)
        + "\n_cur = _v(_n.pos_x)"
        + f"\n_n.pos_x.set_value(int({value!r}) if isinstance(_cur, int) else float({value!r}))"
        + "\n_result = _v(_n.pos_x)"
    )
    br = await tx(ctx, code, label=f"p22a_write_{name}")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_read_reel_name(ctx: Ctx) -> Any:
    code = f"_r = flame.find_by_wiretap_node_id({ctx.ids['reelWtid']!r})\n_result = _v(_r.name)"
    br = await tx(ctx, code, label="p22a_read_reel_name")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


def node_selector(ctx: Ctx, name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": {"kind": "batch", "uid": ctx.ids["bgUid"]}, "name": name}


# The journal, read from outside the server
def journal_index(ctx: Ctx) -> Index:
    return Index(Journal(ctx.journal_dir).read_all().records)


def intents_for(ctx: Ctx, node: str, op: str = "node.set_attributes") -> list[dict[str, Any]]:
    index = journal_index(ctx)
    return [
        v.intent
        for v in index.intents.values()
        if v.intent.get("op") == op
        and isinstance(v.intent.get("target"), dict)
        and v.intent["target"].get("name") == node
    ]


async def wait_for_intent(
    ctx: Ctx, node: str, *, already: int = 0, timeout_s: float = 30.0
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = intents_for(ctx, node)
        if len(found) > already:
            return found[already]
        await anyio.sleep(0.1)
    return None


def state_of(ctx: Ctx, group: str | None) -> str | None:
    if group is None:
        return None
    index = journal_index(ctx)
    return index.group_state(group) if group in index.groups else None


async def prepare(ctx: Ctx) -> None:
    ts = ctx.ts
    reel_name = f"P22A_REEL_{ts}"
    bg_name = f"P22A_BG_{ts}"
    code = "\n".join(
        [
            "desktop = flame.projects.current_project.current_workspace.desktop",
            "groups = list(desktop.reel_groups)",
            f"rg = groups[0] if groups else desktop.create_reel_group('P22A_RG_{ts}')",
            f"reel = rg.create_reel({reel_name!r})",
            f"bg = desktop.create_batch_group({bg_name!r}, nb_reels=1, nb_shelf_reels=1)",
            "names = []",
            f"for _i in range({NODE_NAMES_NEEDED}):",
            "    names.append(_v(bg.create_node('Blur').name))",
            "_result = {",
            "    'reelWtid': reel.get_wiretap_node_id(),",
            "    'bgUid': _v(bg.uid),",
            "    'nodes': names,",
            "}",
        ]
    )
    br = await tx(ctx, code, label="p22a_prepare", timeout_s=60.0)
    rec(ctx, "prepare_reel_batch_and_nodes", br.ok, resp=_br_dict(br))
    if br.ok and isinstance(br.result, dict):
        ctx.ids.update(
            reelName=reel_name,
            batchName=bg_name,
            reelWtid=br.result["reelWtid"],
            bgUid=br.result["bgUid"],
            nodes=br.result["nodes"],
        )
        rec(
            ctx,
            "prepare_node_names_are_distinct",
            len(set(br.result["nodes"])) == NODE_NAMES_NEEDED,
            nodes=br.result["nodes"],
        )


async def stage_a(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][0]
    async with server_session(ctx, "a-plain") as session:
        info = await version_info(session, ctx)
        journal = _result_of(info).get("journal", {})
        rec(
            ctx,
            "a_version_info_reports_the_journal_and_no_test_hook",
            info.get("ok") is True
            and journal.get("dir") == str(ctx.journal_dir)
            and journal.get("testHooks") == []
            and journal.get("writesBlocked") is False
            and isinstance(_result_of(info).get("serverPid"), int),
            journal=journal,
            serverPid=_result_of(info).get("serverPid"),
        )
        catalog = _structured(
            await session.call_tool("flame_catalog", {}), tool="flame_catalog", results=ctx.results
        )
        ops = {op["name"]: op for op in _result_of(catalog).get("operations", [])}
        threads: dict[str, Any] = {}
        for name in sorted(JOURNAL_OPS):  # thread is in the one-op description, not the listing
            described = _structured(
                await session.call_tool("flame_catalog", {"op": name}),
                tool="flame_catalog",
                results=ctx.results,
            )
            threads[name] = _result_of(described).get("operation", {}).get("thread")
        rec(
            ctx,
            "a_catalog_equals_registry_and_has_the_journal_ops",
            set(ops) == REGISTERED_OP_NAMES
            and JOURNAL_OPS <= set(ops)
            and all(threads[n] == "server" for n in JOURNAL_OPS),
            journalOps={n: (threads[n], ops[n]["effect"]) for n in sorted(JOURNAL_OPS)},
        )

        p0 = await tx_read_pos(ctx, node)
        sel = node_selector(ctx, node)
        set_env = await do(
            session, ctx, "node.set_attributes", {"target": sel, "values": {"pos_x": 111}}
        )
        group = journal_of(set_env).get("group")
        rec(
            ctx,
            "a_set_is_ok_and_names_its_journal_records",
            set_env.get("ok") is True and isinstance(group, str) and journal_of(set_env).get("id"),
            before=p0,
            env=set_env,
        )
        after = await tx_read_pos(ctx, node)
        rec(ctx, "a_independent_read_is_the_new_value", same(after, 111), viaTransport=after)

        listing = await do(session, ctx, "journal.list", {"limit": 5})
        first = (_result_of(listing).get("items") or [{}])[0]
        rec(
            ctx,
            "a_list_says_landed",
            first.get("group") == group and first.get("state") == "landed",
            first=first,
        )
        shown = await do(session, ctx, "journal.show", {"group": group})
        records = _result_of(shown).get("records", [])
        rec(
            ctx,
            "a_show_has_intent_then_outcome_with_the_values",
            [r.get("type") for r in records] == ["intent", "outcome"]
            and same(records[0].get("before", {}).get("pos_x"), p0)
            and same(records[0].get("after", {}).get("pos_x"), 111)
            and records[1].get("ok") is True
            and records[1].get("applied") == ["pos_x"],
            records=records,
        )

        back = await do(session, ctx, "journal.revert", {"group": group})
        restored = await tx_read_pos(ctx, node)
        listing2 = await do(session, ctx, "journal.list", {"limit": 5})
        states = {i.get("group"): i.get("state") for i in _result_of(listing2).get("items", [])}
        new_group = _result_of(back).get("group")
        rec(
            ctx,
            "a_revert_restores_the_previous_value",
            back.get("ok") is True
            and same(restored, p0)
            and states.get(group) == "reverted"
            and states.get(new_group) == "landed",
            viaTransport=restored,
            states=states,
            env=back,
        )

        again = await do(session, ctx, "journal.revert", {"group": group})
        still = await tx_read_pos(ctx, node)
        rec(
            ctx,
            "a_second_revert_of_the_same_group_is_refused",
            again.get("ok") is False and same(still, p0),
            viaTransport=still,
            env=again,
        )

        reel_sel = {"kind": "wiretap", "id": ctx.ids["reelWtid"]}
        renamed = await do(
            session,
            ctx,
            "desktop.rename",
            {"target": reel_sel, "name": f"{ctx.ids['reelName']}_j"},
        )
        name_now = await tx_read_reel_name(ctx)
        rename_group = journal_of(renamed).get("group")
        rec(
            ctx,
            "a_rename_is_journaled",
            renamed.get("ok") is True
            and name_now == f"{ctx.ids['reelName']}_j"
            and state_of(ctx, rename_group) == "landed",
            viaTransport=name_now,
            env=renamed,
        )
        rename_back = await do(session, ctx, "journal.revert", {"group": rename_group})
        name_after = await tx_read_reel_name(ctx)
        rec(
            ctx,
            "a_rename_revert_restores_the_name",
            rename_back.get("ok") is True and name_after == ctx.ids["reelName"],
            viaTransport=name_after,
            env=rename_back,
        )


async def stage_b(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][1]
    sel = node_selector(ctx, node)
    p0 = await tx_read_pos(ctx, node)
    box: dict[str, Any] = {}
    async with server_session(ctx, "b-pause-3s", hooks={ENV_PAUSE: "3"}) as session:

        async def call() -> None:
            try:
                box["env"] = await do(
                    session, ctx, "node.set_attributes", {"target": sel, "values": {"pos_x": 222}}
                )
            except BaseException:
                box["error"] = traceback.format_exc()

        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            intent = await wait_for_intent(ctx, node)
            during = await tx_read_pos(ctx, node)
            rec(
                ctx,
                "b_intent_is_journaled_while_the_write_has_not_been_sent",
                intent is not None and same(during, p0),
                intent=intent,
                valueInFlameDuringThePause=during,
            )
            wrote = await tx_write_pos(ctx, node, 333)
            rec(ctx, "b_transport_wrote_behind_the_servers_back", same(wrote, 333), value=wrote)
    env = box.get("env")
    if env is None:
        rec(ctx, "b_call_returned", False, error=box.get("error"))
        return
    final = await tx_read_pos(ctx, node)
    conflicts = _result_of(env).get("conflicts", {})
    pos = conflicts.get("pos_x", {}) if isinstance(conflicts, dict) else {}
    group = journal_of(env).get("group")
    rec(
        ctx,
        "b_conflict_and_the_op_wrote_nothing",
        env.get("ok") is False
        and env.get("errorCode") == "CONFLICT"
        and env.get("retryable") is True
        and same(pos.get("expected"), p0)
        and same(pos.get("current"), 333)
        and same(final, 333),
        viaTransport=final,
        env=env,
    )
    rec(
        ctx,
        "b_the_group_is_a_conflict_on_record",
        state_of(ctx, group) == "conflict",
        state=state_of(ctx, group),
    )


async def stage_c(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][2]
    sel = node_selector(ctx, node)
    async with server_session(ctx, "c-plain") as session:
        set_env = await do(
            session, ctx, "node.set_attributes", {"target": sel, "values": {"pos_x": 444}}
        )
        group = journal_of(set_env).get("group")
        rec(ctx, "c_set_is_ok", set_env.get("ok") is True and isinstance(group, str), env=set_env)
        wrote = await tx_write_pos(ctx, node, 555)
        rec(ctx, "c_transport_changed_the_value_afterwards", same(wrote, 555), value=wrote)
        refused = await do(session, ctx, "journal.revert", {"group": group})
        final = await tx_read_pos(ctx, node)
        keys = (_result_of(refused).get("intents") or [{}])[0].get("keys", {})
        pos = keys.get("pos_x", {}) if isinstance(keys, dict) else {}
        rec(
            ctx,
            "c_revert_is_refused_and_the_value_is_left_alone",
            refused.get("ok") is False
            and refused.get("errorCode") == "CONFLICT"
            and same(pos.get("expected"), 444)
            and same(pos.get("current"), 555)
            and pos.get("match") is False
            and same(final, 555),
            viaTransport=final,
            env=refused,
        )
        rec(
            ctx,
            "c_the_group_is_still_landed_not_reverted",
            state_of(ctx, group) == "landed",
            state=state_of(ctx, group),
        )


# landed or not, after the server died
def pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


async def wait_pid_gone(pid: int, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pid_gone(pid):
            return True
        await anyio.sleep(0.2)
    return pid_gone(pid)


async def stage_d1(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][3]
    sel = node_selector(ctx, node)
    p0 = await tx_read_pos(ctx, node)
    pid: int | None = None
    call_error: str | None = None
    try:
        async with server_session(ctx, "d1-exit-after-write", hooks={ENV_EXIT: "1"}) as session:
            info = await version_info(session, ctx)
            pid = _result_of(info).get("serverPid")
            try:
                env = await do(
                    session, ctx, "node.set_attributes", {"target": sel, "values": {"pos_x": 666}}
                )
                rec(ctx, "d1_the_call_did_not_answer", False, env=env)
            except BaseException:
                call_error = traceback.format_exc()
    except BaseException:
        call_error = (call_error or "") + "\n[session exit] " + traceback.format_exc()
    gone = await wait_pid_gone(pid) if isinstance(pid, int) else False
    rec(
        ctx,
        "d1_the_server_process_ended_without_answering",
        gone and call_error is not None,
        serverPid=pid,
        processGone=gone,
        callError=(call_error or "")[-600:],
    )
    landed = await tx_read_pos(ctx, node)
    rec(
        ctx,
        "d1_independent_read_the_write_landed",
        same(landed, 666) and not same(landed, p0),
        before=p0,
        viaTransport=landed,
    )
    intents = intents_for(ctx, node)
    rec(
        ctx,
        "d1_the_journal_holds_an_intent_and_no_outcome",
        len(intents) == 1 and state_of(ctx, intents[0]["group"]) == "unknown",
        intents=intents,
        state=state_of(ctx, intents[0]["group"]) if intents else None,
    )
    async with server_session(ctx, "d1-new-plain") as session:
        info = await version_info(session, ctx)
        journal = _result_of(info).get("journal", {})
        rec(
            ctx,
            "d1_a_new_server_counts_the_unresolved_intent_and_allows_writes",
            journal.get("unresolvedIntents") == 1 and journal.get("writesBlocked") is False,
            journal=journal,
        )
        listing = await do(session, ctx, "journal.list", {"limit": 10})
        unknown = [i for i in _result_of(listing).get("items", []) if i.get("state") == "unknown"]
        group = intents[0]["group"] if intents else None
        rec(
            ctx,
            "d1_list_shows_it_as_unknown",
            len(unknown) == 1 and unknown[0].get("group") == group,
            unknown=unknown,
        )
        found = await do(session, ctx, "journal.check", {"group": group})
        checked = (_result_of(found).get("checked") or [{}])[0]
        rec(
            ctx,
            "d1_check_says_landed",
            found.get("ok") is True
            and checked.get("verdict") == "landed"
            and same(checked.get("current", {}).get("pos_x"), 666),
            env=found,
        )
        again = await do(session, ctx, "journal.check", {})
        rec(
            ctx,
            "d1_nothing_is_left_undecided",
            _result_of(again).get("inDoubt") == 0 and state_of(ctx, group) == "landed",
            env=again,
            state=state_of(ctx, group),
        )
        still = await tx_read_pos(ctx, node)
        rec(ctx, "d1_check_wrote_nothing", same(still, 666), viaTransport=still)


async def stage_d2(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][4]
    sel = node_selector(ctx, node)
    p0 = await tx_read_pos(ctx, node)
    pid: int | None = None
    call_error: str | None = None
    intent: dict[str, Any] | None = None
    try:
        async with server_session(
            ctx, "d2-pause-30s-then-kill", hooks={ENV_PAUSE: "30"}
        ) as session:
            info = await version_info(session, ctx)
            pid = _result_of(info).get("serverPid")

            async def call() -> None:
                nonlocal call_error
                try:
                    await do(
                        session,
                        ctx,
                        "node.set_attributes",
                        {"target": sel, "values": {"pos_x": 777}},
                    )
                except BaseException:
                    call_error = traceback.format_exc()

            async with anyio.create_task_group() as tg:
                tg.start_soon(call)
                intent = await wait_for_intent(ctx, node)
                during = await tx_read_pos(ctx, node)
                rec(
                    ctx,
                    "d2_intent_is_journaled_and_nothing_written_yet",
                    intent is not None and same(during, p0),
                    intent=intent,
                    valueInFlame=during,
                )
                if isinstance(pid, int):
                    os.kill(pid, signal.SIGKILL)
                await anyio.sleep(1.0)
                tg.cancel_scope.cancel()  # the call will never be answered
    except BaseException:
        call_error = (call_error or "") + "\n[session exit] " + traceback.format_exc()
    gone = await wait_pid_gone(pid) if isinstance(pid, int) else False
    rec(
        ctx,
        "d2_the_server_was_killed_mid_wait",
        gone,
        serverPid=pid,
        processGone=gone,
        callError=(call_error or "")[-600:],
    )
    unchanged = await tx_read_pos(ctx, node)
    rec(ctx, "d2_independent_read_nothing_was_written", same(unchanged, p0), viaTransport=unchanged)
    async with server_session(ctx, "d2-new-plain") as session:
        info = await version_info(session, ctx)
        journal = _result_of(info).get("journal", {})
        rec(
            ctx,
            "d2_a_new_server_counts_the_unresolved_intent",
            journal.get("unresolvedIntents") == 1,
            journal=journal,
        )
        group = intent["group"] if intent else None
        found = await do(session, ctx, "journal.check", {"group": group})
        checked = (_result_of(found).get("checked") or [{}])[0]
        rec(
            ctx,
            "d2_check_says_not_landed",
            found.get("ok") is True
            and checked.get("verdict") == "not_landed"
            and same(checked.get("current", {}).get("pos_x"), p0),
            env=found,
        )
        still = await tx_read_pos(ctx, node)
        rec(ctx, "d2_check_wrote_nothing", same(still, p0), viaTransport=still)


async def stage_d(ctx: Ctx) -> None:
    await stage_d1(ctx)
    await stage_d2(ctx)


async def stage_e(ctx: Ctx) -> None:
    node = ctx.ids["nodes"][5]
    other = ctx.ids["nodes"][6]
    sel = node_selector(ctx, node)
    p0 = await tx_read_pos(ctx, node)
    other0 = await tx_read_pos(ctx, other)
    async with server_session(
        ctx, "e-first-write-times-out", hooks={ENV_WRITE_TIMEOUT: "0.001"}
    ) as session:
        first = await do(
            session, ctx, "node.set_attributes", {"target": sel, "values": {"pos_x": 501}}
        )
        group = journal_of(first).get("group")
        rec(
            ctx,
            "e_the_first_write_ends_in_timeout_with_an_open_intent",
            first.get("ok") is False
            and first.get("errorCode") == "TIMEOUT"
            and isinstance(group, str)
            and state_of(ctx, group) == "unknown",
            timeoutPhase=first.get("timeoutPhase"),
            env=first,
        )
        info = await version_info(session, ctx)
        journal = _result_of(info).get("journal", {})
        rec(
            ctx,
            "e_version_info_says_writes_are_blocked",
            journal.get("writesBlocked") is True
            and [b.get("group") for b in journal.get("blocked", [])] == [group],
            journal=journal,
        )
        denied = await do(
            session,
            ctx,
            "node.set_attributes",
            {"target": node_selector(ctx, other), "values": {"pos_x": 503}},
        )
        other_value = await tx_read_pos(ctx, other)
        rec(
            ctx,
            "e_a_write_is_refused_and_nothing_was_written",
            denied.get("errorCode") == "POLICY_DENIED"
            and group in str(denied.get("error"))
            and "journal.check" in str(denied.get("hint"))
            and same(other_value, other0),
            before=other0,
            viaTransport=other_value,
            env=denied,
        )
        revert = await do(session, ctx, "journal.revert", {"group": group})
        rec(
            ctx,
            "e_journal_revert_is_refused_too",
            revert.get("errorCode") == "POLICY_DENIED",
            env=revert,
        )
        reads = {
            "batch.list": await do_read(session, ctx, "batch.list", {"limit": 5}),
            "node.get_attributes": await do(
                session, ctx, "node.get_attributes", {"target": sel, "keys": ["pos_x"]}
            ),
            "journal.list": await do(session, ctx, "journal.list", {"limit": 3}),
        }
        rec(
            ctx,
            "e_reads_still_work",
            all(env.get("ok") is True for env in reads.values()),
            reads={name: env.get("ok") for name, env in reads.items()},
        )

        observed = await tx_read_pos(ctx, node)
        expected_verdict = "landed" if same(observed, 501) else "not_landed"
        found = await do(session, ctx, "journal.check", {"group": group})
        checked = (_result_of(found).get("checked") or [{}])[0]
        rec(
            ctx,
            "e_check_reads_flame_and_agrees_with_an_independent_read",
            found.get("ok") is True
            and checked.get("verdict") == expected_verdict
            and (same(observed, 501) or same(observed, p0)),
            independentRead=observed,
            expectedVerdict=expected_verdict,
            env=found,
        )
        info_after = await version_info(session, ctx)
        rec(
            ctx,
            "e_after_check_writes_are_allowed_again",
            _result_of(info_after).get("journal", {}).get("writesBlocked") is False,
            journal=_result_of(info_after).get("journal"),
        )
        allowed = await do(
            session,
            ctx,
            "node.set_attributes",
            {"target": node_selector(ctx, other), "values": {"pos_x": 503}},
        )
        other_after = await tx_read_pos(ctx, other)
        rec(
            ctx,
            "e_a_write_goes_through_after_the_check",
            allowed.get("ok") is True and same(other_after, 503),
            viaTransport=other_after,
            env=allowed,
        )


async def stage_f(ctx: Ctx) -> None:
    async with server_session(ctx, "f-readonly", readonly=True) as session:
        listed = await session.list_tools()
        do_tool = next(t for t in listed.tools if t.name == "flame_do")
        enum_names = set(do_tool.input_schema["properties"]["op"]["enum"])
        rec(
            ctx,
            "f_revert_is_not_offered_the_other_journal_ops_are",
            "journal.revert" not in enum_names
            and {"journal.list", "journal.show", "journal.check"} <= enum_names,
            journalOps=sorted(n for n in enum_names if n.startswith("journal.")),
        )
        denied = await do(session, ctx, "journal.revert", {"group": "g_x"})
        rec(
            ctx, "f_revert_is_policy_denied", denied.get("errorCode") == "POLICY_DENIED", env=denied
        )
        listing = await do(session, ctx, "journal.list", {"limit": 3})
        rec(ctx, "f_list_works", listing.get("ok") is True, env=listing)


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
        rec(ctx, "z_journal_listing_recorded", listing.get("ok") is True)
    check_envelope_shapes(ctx.results)
    check_rider(ctx.results)
    check_logs_and_crash(ctx.results)
    save(ctx.results)


async def guarded(ctx: Ctx, name: str, stage: Callable[[Ctx], Awaitable[None]]) -> None:
    ctx.stage = name
    try:
        await stage(ctx)
    except BaseException:
        rec(ctx, f"{name}_stage_crashed", False, traceback=traceback.format_exc())
    save_stage(ctx)


def cleanup(ctx: Ctx) -> None:
    deleted: list[str] = []
    wtid = ctx.ids.get("reelWtid")
    if wtid is not None:
        code = (
            f"reel = flame.find_by_wiretap_node_id({wtid!r})\n"
            "flame.delete(reel, confirm=False)\n_result = True"
        )
        br = _tx(V_PRELUDE + "\n" + code, ctx.run_dir, label="p22a_cleanup_delete_reel")
        ctx.stage = "cleanup"
        rec(ctx, "cleanup_delete_desktop_reel", br.ok, resp=_br_dict(br))
        if br.ok:
            deleted.append(f"desktop reel {ctx.ids.get('reelName')}")
    ctx.results["cleanup"] = {
        "deleted": deleted,
        "kept": {
            "batchGroup": ctx.ids.get("batchName"),
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them); the journal directory is kept too"
            ),
        },
    }
    exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label="p22a_cleanup_exit")
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
    journal_dir = OUTPUT_DIR / f"journal_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_2a_journal_e2e_{ts}.json",
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
