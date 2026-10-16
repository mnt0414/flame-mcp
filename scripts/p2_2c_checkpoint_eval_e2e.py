#!/usr/bin/env python3
"""Real-Flame end-to-end: Batch checkpoints, `journal.restore_checkpoint` and `eval.run`
(run with the `.venv` Python; `scripts/run_e2e.sh p2_2c_checkpoint_eval` starts Flame).

Checks: Batch checkpoints and `journal.restore_checkpoint`, including its refusals;
`batch.load_setup`; and `eval.run` with and without the eval switch, with its checkpoint, journal
entries and revert.

Same shape and helpers as scripts/p2_2b_plan_e2e.py (imported from p2_2a): a real MCP stdio
client drives `python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport` is
scaffolding only (create the test groups, read every node / wiring back INDEPENDENTLY of the
ops under test, remove a checkpoint file, glob the checkpoint directory). Everything happens in
the probe project `flame_mcp_probe`, in Batch groups that are NOT current (a decoy group is
created last). The journal directory is this run's own (`p2/journal_ckpt_e2e_<ts>/`, never the
production one; checkpoints therefore land in its `checkpoints/`). No test hook is set for any
server. The environment of every server is recorded in results["servers"] (`evalEnabled` says
whether it was started with FLAME_MCP_ENABLE_EVAL=1; whether eval.run is listed in flame_catalog
is what the server itself reports).
Nothing here writes an attribute value: the eval code creates nodes and wires them.

Stages
  a  batch.checkpoint (server WITHOUT the eval switch): the path it reports exists (Flame side,
     os.path.exists), has the `.batch` suffix and sits in the run's checkpoint directory; the
     fingerprint is reported. A node is added through transport, then journal.restore_checkpoint:
     the node is gone, the group's nodes and wiring equal what they were, the fingerprint equals
     the checkpoint's, and the pre-restore checkpoint's file exists. Restoring THAT checkpoint
     brings the extra node back (a restore can be undone). journal.list_checkpoints lists all.
  b  batch.load_setup with an existing setup: the group's previous state is checkpointed first
     (its file exists), the group equals the setup, journal.revert brings the previous contents
     back. Then restore refusals: a checkpoint whose file was removed ->
     PRECONDITION_UNMET, the group's
     nodes are untouched (NOT emptied) and no pre-restore checkpoint file appeared; an id that
     never existed -> INVALID_ARGS, nodes untouched; batch.load_setup on a missing path ->
     PRECONDITION_UNMET, nodes untouched.
  c  eval.run effect='read' (FLAME_MCP_ENABLE_EVAL=1): a node listing comes back and equals the
     transport read; with effect='read' a `set_value` call, `flame.projects` iteration and
     `execute_shortcut` are refused (INVALID_ARGS, nothing changed). Under READONLY every
     eval.run, a read included, is POLICY_DENIED.
  d  a server WITHOUT FLAME_MCP_ENABLE_EVAL: eval.run is not in flame_catalog and a call, read
     or destructive, is POLICY_DENIED naming the switch (not UNKNOWN_OPERATION), nodes untouched.
     A server WITH it: destructive without a target ->
     INVALID_ARGS; destructive with a static violation -> INVALID_ARGS.
  e  FLAME_MCP_ENABLE_EVAL=1: destructive eval on a group creates two nodes and wires them ->
     independent read shows them; the journal has a checkpoint record, an intent (code and its
     sha256, target, checkpoint id, fingerprint before) and an outcome (fingerprint after);
     journal.revert -> the nodes are gone, wiring and fingerprint as before, group `reverted`.
     Then eval again, add a node through transport, journal.revert -> CONFLICT and nothing
     changed; journal.revert force=true -> restored, the restore's intent records the force.
  z  cross-cutting checks over every envelope; nothing left undecided

Cleanup: no Batch group is deleted (flame.delete on a PyBatch is not used); then flame.exit().
"""

from __future__ import annotations

import argparse
import hashlib
import os
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
    _structured,
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
GROUPS: Final[tuple[str, ...]] = ("A", "B", "C", "D", "E")
EVAL_MAKE_PAIR: Final[str] = "\n".join(
    [
        "a = target.create_node('Mux')",
        "b = target.create_node('Mux')",
        "target.connect_nodes(a, 'Default', b, 'Default')",
        "_result = [_v(a.name), _v(b.name)]",
    ]
)
REELS: Final[str] = "nb_reels=1, nb_shelf_reels=1"
EVAL_LIST_NODES: Final[str] = "_result = sorted(_v(n.name) for n in target.nodes)"

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


async def tx_state(ctx: Ctx, uid: str) -> State:
    """The group's node names and input wiring, read without any op or prelude of ours."""
    code = (
        _locate(uid)
        + "\n"
        + "\n".join(
            [
                "_inputs = {}",
                "for _n in _bg.nodes:",
                "    _s = _n.sockets",
                "    _w = {k: sorted(v) for k, v in _s['input'].items() if v}",
                "    if _w:",
                "        _inputs[_v(_n.name)] = _w",
                "_result = {'names': sorted(_v(_n.name) for _n in _bg.nodes), 'inputs': _inputs}",
            ]
        )
    )
    br = await tx(ctx, code, label="p22c_state")
    if br.ok and isinstance(br.result, dict):
        return dict(br.result)
    return {"error": f"transport {br.error_code}: {br.error}"}


async def tx_add_node(ctx: Ctx, uid: str, node_type: str) -> Any:
    code = _locate(uid) + f"\n_result = _v(_bg.create_node({node_type!r}).name)"
    br = await tx(ctx, code, label="p22c_add_node")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_file_exists(ctx: Ctx, path: str) -> Any:
    br = await tx(ctx, f"import os\n_result = os.path.isfile({path!r})", label="p22c_exists")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_remove(ctx: Ctx, path: str) -> Any:
    br = await tx(ctx, f"import os\nos.remove({path!r})\n_result = True", label="p22c_remove")
    return br.ok


async def tx_batch_files(ctx: Ctx) -> list[str]:
    directory = str(ctx.journal_dir / "checkpoints")
    code = f"import glob, os\n_result = sorted(glob.glob(os.path.join({directory!r}, '*.batch')))"
    br = await tx(ctx, code, label="p22c_glob")
    return list(br.result) if br.ok and isinstance(br.result, list) else []


def target_of(ctx: Ctx, group: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": ctx.ids["groups"][group]["uid"]}


def uid_of(ctx: Ctx, group: str) -> str:
    return str(ctx.ids["groups"][group]["uid"])


def intact(before: State, after: State) -> bool:
    return "error" not in before and before == after


def eval_args(code: str, effect: str, target: dict[str, Any] | None) -> dict[str, Any]:
    args: dict[str, Any] = {"code": code, "effect": effect}
    if target is not None:
        args["target"] = target
    return args


async def report_eval_state(session: Any, ctx: Ctx, label: str) -> str | None:
    """What this server itself says about the eval switch (recorded, and returned)."""
    info = await version_info(session, ctx)
    policy = _result_of(info).get("policy")
    rec(ctx, f"{label}_server_reports_its_eval_setting", isinstance(policy, str), policy=policy)
    return policy if isinstance(policy, str) else None


async def eval_is_listed(session: Any, ctx: Ctx) -> bool:
    """Whether flame_catalog lists eval.run (it does so only with FLAME_MCP_ENABLE_EVAL=1)."""
    catalog = _structured(
        await session.call_tool("flame_catalog", {}), tool="flame_catalog", results=ctx.results
    )
    return "eval.run" in {op["name"] for op in _result_of(catalog).get("operations", [])}


async def prepare(ctx: Ctx) -> None:
    lines = ["desktop = flame.projects.current_project.current_workspace.desktop", "groups = {}"]
    for group in GROUPS:
        lines += [
            f"bg = desktop.create_batch_group('P22C_{group}_{ctx.ts}', {REELS})",
            "bg.create_node('Blur')",
            "bg.create_node('Blur')",
            "bg.create_node('Comp')",
            f"groups[{group!r}] = {{'uid': _v(bg.uid), 'name': _v(bg.name)}}",
        ]
    lines += [
        # created last: it becomes the current group, so none of A-E is current
        f"decoy = desktop.create_batch_group('P22C_DECOY_{ctx.ts}', nb_reels=1, nb_shelf_reels=1)",
        "_result = {'groups': groups, 'decoyUid': _v(decoy.uid)}",
    ]
    br = await tx(ctx, "\n".join(lines), label="p22c_prepare", timeout_s=60.0)
    rec(ctx, "prepare_groups_and_decoy", br.ok, resp=_br_dict(br))
    if br.ok and isinstance(br.result, dict):
        ctx.ids.update(groups=br.result["groups"], decoyUid=br.result["decoyUid"])
        code = (
            "_ws = flame.projects.current_project.current_workspace\n"
            "_cur = _v(_ws.desktop.current_batch_group)\n"
            "_result = _v(_cur.uid) if _cur is not None else None"
        )
        cur = await tx(ctx, code, label="p22c_current")
        others = {v["uid"] for v in br.result["groups"].values()}
        rec(
            ctx,
            "prepare_no_test_group_is_current",
            cur.ok and cur.result == br.result["decoyUid"] and cur.result not in others,
            current=cur.result,
            decoy=br.result["decoyUid"],
        )


async def stage_a(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "A"), target_of(ctx, "A")
    base = await tx_state(ctx, uid)
    async with server_session(ctx, "a-plain", eval_enabled=False) as session:
        await report_eval_state(session, ctx, "a")
        rec(ctx, "a_no_eval_switch_on_this_server", not await eval_is_listed(session, ctx))
        cp = await do(session, ctx, "batch.checkpoint", {"target": target, "label": "p22c a"})
        result = _result_of(cp)
        path = result.get("path")
        rec(
            ctx,
            "a_checkpoint_reports_a_real_file_in_the_run_directory",
            cp.get("ok") is True
            and isinstance(path, str)
            and path.endswith(".batch")
            and path.startswith(str(ctx.journal_dir / "checkpoints"))
            and await tx_file_exists(ctx, str(path)) is True
            and isinstance(result.get("fingerprint"), str)
            and result.get("nodeCount") == len(base.get("names", [])),
            env=cp,
        )
        cp_id = result.get("checkpoint")
        records = [
            r for r in Journal(ctx.journal_dir).read_all().records if r["type"] == "checkpoint"
        ]
        rec(
            ctx,
            "a_the_journal_holds_the_checkpoint_record",
            any(
                r.get("id") == cp_id
                and r.get("path") == path
                and r.get("fingerprint") == result.get("fingerprint")
                for r in records
            ),
            records=records,
        )
        after_cp = await tx_state(ctx, uid)
        rec(ctx, "a_checkpointing_changed_nothing", intact(base, after_cp), base=base, now=after_cp)

        extra = await tx_add_node(ctx, uid, "Mux")
        with_extra = await tx_state(ctx, uid)
        rec(
            ctx,
            "a_an_extra_node_was_added_through_transport",
            extra in with_extra.get("names", []) and extra not in base.get("names", []),
            extra=extra,
        )
        restored = await do(session, ctx, "journal.restore_checkpoint", {"checkpoint": cp_id})
        rr = _result_of(restored)
        pre = rr.get("preCheckpoint") or {}
        now = await tx_state(ctx, uid)
        rec(
            ctx,
            "a_restore_removes_the_node_and_the_group_equals_what_it_was",
            restored.get("ok") is True and intact(base, now) and rr.get("matched") is True,
            viaTransport=now,
            base=base,
            env=restored,
        )
        rec(
            ctx,
            "a_the_restore_read_back_equals_the_checkpoint_fingerprint",
            rr.get("fingerprintAfter") == result.get("fingerprint")
            and rr.get("expectedFingerprint") == result.get("fingerprint"),
            after=rr.get("fingerprintAfter"),
            checkpoint=result.get("fingerprint"),
        )
        rec(
            ctx,
            "a_the_pre_restore_checkpoint_file_exists",
            isinstance(pre.get("path"), str)
            and await tx_file_exists(ctx, str(pre["path"])) is True,
            preCheckpoint=pre,
        )
        undone = await do(session, ctx, "journal.restore_checkpoint", {"checkpoint": pre.get("id")})
        back = await tx_state(ctx, uid)
        rec(
            ctx,
            "a_restoring_the_pre_restore_checkpoint_undoes_the_restore",
            undone.get("ok") is True and extra in back.get("names", []) and back == with_extra,
            viaTransport=back,
            expected=with_extra,
            env=undone,
        )
        listing = await do(session, ctx, "journal.list_checkpoints", {"limit": 20})
        items = _result_of(listing).get("items") or []
        ids = {i.get("id") for i in items}
        rec(
            ctx,
            "a_list_checkpoints_lists_them_with_their_files",
            {cp_id, pre.get("id")} <= ids and all(i.get("fileExists") is True for i in items[:3]),
            items=items[:4],
        )


async def stage_b(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "B"), target_of(ctx, "B")
    base = await tx_state(ctx, uid)
    async with server_session(ctx, "b-plain", eval_enabled=False) as session:
        cp = await do(session, ctx, "batch.checkpoint", {"target": target})
        result = _result_of(cp)
        rec(ctx, "b_checkpoint_taken", cp.get("ok") is True, env=cp)
        await tx_add_node(ctx, uid, "Mux")
        changed = await tx_state(ctx, uid)

        loaded = await do(
            session, ctx, "batch.load_setup", {"target": target, "path": result.get("path")}
        )
        lr = _result_of(loaded)
        pre_l = lr.get("preCheckpoint") or {}
        after_load = await tx_state(ctx, uid)
        rec(
            ctx,
            "b_load_setup_checkpoints_first_then_replaces_the_group",
            loaded.get("ok") is True
            and intact(base, after_load)
            and isinstance(pre_l.get("path"), str)
            and await tx_file_exists(ctx, str(pre_l["path"])) is True
            and pre_l.get("fingerprint") != result.get("fingerprint"),
            viaTransport=after_load,
            base=base,
            env=loaded,
        )
        reverted_load = await do(
            session, ctx, "journal.revert", {"group": (loaded.get("journal") or {}).get("group")}
        )
        after_revert = await tx_state(ctx, uid)
        rec(
            ctx,
            "b_revert_of_a_load_setup_brings_the_previous_contents_back",
            reverted_load.get("ok") is True and intact(changed, after_revert),
            viaTransport=after_revert,
            expected=changed,
            env=reverted_load,
        )
        files_before = await tx_batch_files(ctx)
        removed = await tx_remove(ctx, str(result.get("path")))
        gone = await tx_file_exists(ctx, str(result.get("path")))
        rec(
            ctx,
            "b_the_checkpoint_file_was_removed",
            removed and gone is False,
            path=result.get("path"),
        )

        refused = await do(
            session, ctx, "journal.restore_checkpoint", {"checkpoint": result.get("checkpoint")}
        )
        after = await tx_state(ctx, uid)
        files_after = await tx_batch_files(ctx)
        rec(
            ctx,
            "b_restore_of_a_missing_file_is_refused",
            refused.get("ok") is False
            and refused.get("errorCode") == "PRECONDITION_UNMET"
            and _result_of(refused).get("loadSetupCalled") is False,
            env=refused,
        )
        rec(
            ctx,
            "b_the_group_was_not_emptied_and_nothing_changed",
            intact(changed, after) and len(after.get("names", [])) == len(base["names"]) + 1,
            before=changed,
            after=after,
        )
        rec(
            ctx,
            "b_no_pre_restore_checkpoint_file_appeared",
            files_after == [p for p in files_before if p != result.get("path")],
            before=files_before,
            after=files_after,
        )

        unknown = await do(
            session, ctx, "journal.restore_checkpoint", {"checkpoint": "c_no_such_id"}
        )
        after_unknown = await tx_state(ctx, uid)
        rec(
            ctx,
            "b_an_id_that_never_existed_is_refused_and_nothing_changed",
            unknown.get("errorCode") == "INVALID_ARGS" and intact(changed, after_unknown),
            env=unknown,
        )

        missing_path = str(ctx.journal_dir / "checkpoints" / "no_such_setup.batch")
        direct = await do(
            session, ctx, "batch.load_setup", {"target": target, "path": missing_path}
        )
        after_direct = await tx_state(ctx, uid)
        rec(
            ctx,
            "b_load_setup_on_a_missing_path_is_refused_and_nothing_changed",
            direct.get("errorCode") == "PRECONDITION_UNMET"
            and _result_of(direct).get("loadSetupCalled") is False
            and intact(changed, after_direct),
            env=direct,
        )


async def stage_c(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "C"), target_of(ctx, "C")
    base = await tx_state(ctx, uid)
    async with server_session(ctx, "c-plain") as session:
        await report_eval_state(session, ctx, "c")
        rec(ctx, "c_the_eval_switch_is_on_on_this_server", await eval_is_listed(session, ctx))
        listing = await do(session, ctx, "eval.run", eval_args(EVAL_LIST_NODES, "read", target))
        rec(
            ctx,
            "c_read_eval_returns_the_node_names",
            listing.get("ok") is True and listing.get("result") == base.get("names"),
            viaTransport=base.get("names"),
            env=listing,
        )
        cases = {
            "set_value": "target.nodes[0].pos_x.set_value(1)",
            "flame_projects_iteration": "_result = [p for p in flame.projects]",
            "flame_projects_len": "_result = len(flame.projects)",
            "execute_shortcut": "flame.execute_shortcut('Undo')",
            "create_node": "target.create_node('Mux')",
        }
        for name, code in cases.items():
            env = await do(session, ctx, "eval.run", eval_args(code, "read", target))
            rec(
                ctx,
                f"c_read_eval_refuses_{name}",
                env.get("ok") is False
                and env.get("errorCode") == "INVALID_ARGS"
                and _result_of(env).get("ran") is False,
                env=env,
            )
        after = await tx_state(ctx, uid)
        rec(ctx, "c_nothing_changed", intact(base, after), before=base, after=after)
    async with server_session(ctx, "c-readonly", readonly=True) as session:
        env = await do(session, ctx, "eval.run", eval_args(EVAL_LIST_NODES, "read", target))
        rec(
            ctx,
            "c_read_eval_under_readonly_is_policy_denied",
            env.get("errorCode") == "POLICY_DENIED",
            env=env,
        )
        denied = await do(
            session, ctx, "eval.run", eval_args(EVAL_MAKE_PAIR, "destructive", target)
        )
        rec(
            ctx,
            "c_destructive_eval_under_readonly_is_policy_denied",
            denied.get("errorCode") == "POLICY_DENIED",
            env=denied,
        )


async def stage_d(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "D"), target_of(ctx, "D")
    base = await tx_state(ctx, uid)
    async with server_session(ctx, "d-no-switch", eval_enabled=False) as session:
        await report_eval_state(session, ctx, "d1")
        rec(ctx, "d_no_eval_switch_on_this_server", not await eval_is_listed(session, ctx))
        for effect, code in (("read", EVAL_LIST_NODES), ("destructive", EVAL_MAKE_PAIR)):
            env = await do(session, ctx, "eval.run", eval_args(code, effect, target))
            rec(
                ctx,
                f"d_{effect}_eval_without_the_switch_is_policy_denied_naming_the_switch",
                env.get("ok") is False
                and env.get("errorCode") == "POLICY_DENIED"
                and ENABLE_EVAL in f"{env.get('error')} {env.get('hint')}",
                env=env,
            )
        rec(ctx, "d_nothing_changed_without_the_switch", intact(base, await tx_state(ctx, uid)))
    async with server_session(ctx, "d-switch") as session:
        await report_eval_state(session, ctx, "d2")
        rec(ctx, "d_the_switch_is_on_on_this_server", await eval_is_listed(session, ctx))
        no_target = await do(
            session, ctx, "eval.run", eval_args(EVAL_MAKE_PAIR, "destructive", None)
        )
        rec(
            ctx,
            "d_destructive_without_a_target_is_refused",
            no_target.get("errorCode") == "INVALID_ARGS",
            env=no_target,
        )
        scan = await do(session, ctx, "eval.run", eval_args("exec('1')", "destructive", target))
        rec(
            ctx,
            "d_destructive_with_a_static_violation_is_refused_before_a_checkpoint",
            scan.get("errorCode") == "INVALID_ARGS" and _result_of(scan).get("ran") is False,
            env=scan,
        )
        rec(ctx, "d_nothing_changed_with_the_switch", intact(base, await tx_state(ctx, uid)))


def group_records(ctx: Ctx, group: str) -> list[dict[str, Any]]:
    return [r for r in Journal(ctx.journal_dir).read_all().records if r.get("group") == group]


async def stage_e(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "E"), target_of(ctx, "E")
    base = await tx_state(ctx, uid)
    async with server_session(ctx, "e-switch") as session:
        await report_eval_state(session, ctx, "e")
        rec(ctx, "e_the_switch_is_on_on_this_server", await eval_is_listed(session, ctx))
        env = await do(session, ctx, "eval.run", eval_args(EVAL_MAKE_PAIR, "destructive", target))
        made = env.get("result")
        journal = env.get("journal") or {}
        group = journal.get("group")
        now = await tx_state(ctx, uid)
        rec(
            ctx,
            "e_the_eval_created_and_wired_two_nodes",
            env.get("ok") is True
            and isinstance(made, list)
            and len(made) == 2
            and now.get("names") == sorted([*base["names"], *made])
            and any(made[0] in wired for wired in now.get("inputs", {}).get(made[1], {}).values()),
            made=made,
            viaTransport=now,
            base=base,
            env=env,
        )
        rec(
            ctx,
            "e_the_envelope_names_the_group_the_checkpoint_and_the_revert",
            isinstance(group, str)
            and isinstance(journal.get("checkpoint"), str)
            and journal.get("changed") is True
            and (journal.get("revert") or {}).get("op") == "journal.revert",
            journal=journal,
        )
        records = group_records(ctx, str(group))
        types = [r.get("type") for r in records]
        intent = next((r for r in records if r.get("type") == "intent"), {})
        outcome = next((r for r in records if r.get("type") == "outcome"), {})
        checkpoint = next((r for r in records if r.get("type") == "checkpoint"), {})
        rec(
            ctx,
            "e_the_journal_has_checkpoint_intent_and_outcome_in_order",
            types == ["checkpoint", "intent", "outcome"],
            types=types,
        )
        rec(
            ctx,
            "e_the_intent_holds_the_code_its_sha256_the_target_and_the_checkpoint",
            intent.get("code") == EVAL_MAKE_PAIR
            and intent.get("codeSha256") == hashlib.sha256(EVAL_MAKE_PAIR.encode()).hexdigest()
            and intent.get("target") == target
            and intent.get("checkpoint") == checkpoint.get("id") == journal.get("checkpoint")
            and intent.get("fingerprintBefore") == checkpoint.get("fingerprint")
            and outcome.get("ok") is True
            and isinstance(outcome.get("fingerprintAfter"), str)
            and outcome.get("fingerprintAfter") != intent.get("fingerprintBefore"),
            intent={k: v for k, v in intent.items() if k != "code"},
            outcome=outcome,
        )
        rec(
            ctx,
            "e_the_checkpoint_file_exists",
            isinstance(checkpoint.get("path"), str)
            and await tx_file_exists(ctx, str(checkpoint["path"])) is True,
            path=checkpoint.get("path"),
        )

        reverted = await do(session, ctx, "journal.revert", {"group": group})
        after = await tx_state(ctx, uid)
        rec(
            ctx,
            "e_revert_removes_the_nodes_and_restores_wiring",
            reverted.get("ok") is True
            and intact(base, after)
            and state_of(ctx, group) == "reverted",
            viaTransport=after,
            base=base,
            env=reverted,
        )
        rr = _result_of(reverted)
        rec(
            ctx,
            "e_the_revert_fingerprint_equals_the_one_before_the_eval",
            rr.get("fingerprintAfter") == intent.get("fingerprintBefore"),
            after=rr.get("fingerprintAfter"),
            before=intent.get("fingerprintBefore"),
        )
        again = await do(session, ctx, "journal.revert", {"group": group})
        rec(ctx, "e_a_second_revert_is_refused", again.get("ok") is False, env=again)

        # a second eval, then somebody else touches the group
        second = await do(
            session, ctx, "eval.run", eval_args(EVAL_MAKE_PAIR, "destructive", target)
        )
        group2 = (second.get("journal") or {}).get("group")
        stranger = await tx_add_node(ctx, uid, "Blur")
        dirty = await tx_state(ctx, uid)
        rec(
            ctx,
            "e_second_eval_ran_and_a_stranger_added_a_node",
            second.get("ok") is True
            and stranger in dirty.get("names", [])
            and len(dirty["names"]) == len(base["names"]) + 3,
            stranger=stranger,
            viaTransport=dirty,
        )
        refused = await do(session, ctx, "journal.revert", {"group": group2})
        still = await tx_state(ctx, uid)
        rec(
            ctx,
            "e_revert_after_a_change_is_a_conflict_and_touches_nothing",
            refused.get("ok") is False
            and refused.get("errorCode") == "CONFLICT"
            and _result_of(refused).get("refused") is True
            and intact(dirty, still),
            env=refused,
            viaTransport=still,
        )
        forced = await do(session, ctx, "journal.revert", {"group": group2, "force": True})
        final = await tx_state(ctx, uid)
        intents = [
            v.intent
            for v in journal_index(ctx).groups.get((forced.get("journal") or {}).get("group"), [])
        ]
        rec(
            ctx,
            "e_force_restores_and_the_journal_records_it",
            forced.get("ok") is True
            and intact(base, final)
            and len(intents) == 1
            and (intents[0].get("forced") or {}).get("used") is True
            and intents[0].get("reverts") == group2,
            viaTransport=final,
            env=forced,
            intents=intents,
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
        listing = await do(session, ctx, "journal.list", {"limit": 50})
        ctx.results["journalListing"] = _result_of(listing).get("items")
    check_envelope_shapes(ctx.results)
    # no check_rider: batch.checkpoint / eval.run carry a rider, but the journal ops do not and
    # the shape check above already covers both
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def cleanup(ctx: Ctx) -> None:
    ctx.stage = "cleanup"
    ctx.results["cleanup"] = {
        "deleted": [],
        "kept": {
            "batchGroups": ctx.ids.get("groups"),
            "checkpointFiles": str(ctx.journal_dir / "checkpoints"),
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them); checkpoint files are never deleted"
            ),
        },
    }
    exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label="p22c_cleanup_exit")
    rec(
        ctx,
        "cleanup_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
    )


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "groups" in ctx.ids:
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

    # every server starts from a known environment: server_session sets the eval switch itself
    os.environ.pop(ENABLE_EVAL, None)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_ckpt_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_2c_checkpoint_eval_e2e_{ts}.json",
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
