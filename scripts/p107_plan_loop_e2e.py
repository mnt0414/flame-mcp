#!/usr/bin/env python3
"""Real-Flame end-to-end: `plan.run` references and `plan.for_each` (run with the `.venv` Python;
`scripts/run_e2e.sh p107_plan_loop` starts Flame; `--dry-run` connects to nothing, lists the
calls, validates their arguments and compiles the transport code).

Checks: A `plan.run` step can use a value of an earlier step (`{{ steps.<id>.result.<path> }}`),
and a reference with no value stops the plan before the step is sent. A read loop of
`plan.for_each` runs one step per item and honours `where` and `expect_count`. A write loop is
confirmed by default (`dry_run` writes nothing, `approve` works once, a wrong digest runs
nothing), one `journal.revert` of its parent group puts every item back, a loop that stops at a
failed item says what ran, and `only` goes on with the rest. With FLAME_MCP_LOOP_UNATTENDED=1 a
loop of renames runs in one call, but a loop of desktop.delete / desktop.reformat (rewrite) still
needs `approve`. A loop that sets `allow_overwrite` is refused in every mode (with and without
FLAME_MCP_LOOP_UNATTENDED=1), and a render loop (render.batch) is listed for review and not run.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it makes the material and READS EVERY NAME BACK
INDEPENDENTLY of the ops under test. The only things written are the three Batch groups this run
makes and renames (`PP5E_<ts>_B1` .. `_B3`, renamed to `PP5E_<ts>_R1` ..); all of them carry the
prefix scripts/p2_4_clean_probe_project.py knows and are deleted at the end by `PP5E_<ts>`; the
stash library name (`PP5E_<ts>_CKPT`) carries it too; then flame.exit(). The journal directory is
this run's own (`p3/journal_p107_e2e_<ts>/`). No FLAME_MCP_TEST_* fault-injection variable
is set for any server; the one variable added is FLAME_MCP_LOOP_UNATTENDED=1, for the second server
of stage x only. The first step refuses to run anywhere but in the project `flame_mcp_probe`.

Not run, on purpose: nothing is rendered (render.batch only goes through `dry_run`, and through
the refusal of allow_overwrite), nothing is deleted or reformatted (the desktop.delete /
desktop.reformat steps point at a wiretap id that does not exist and only go through `dry_run` and
the refusal without `approve`), and no user object is read or written.

Stages (the Batch groups B1 .. B3 are made in `prepare`):
  r  plan.run. Find B1 by name, rename the uid it found to `<name>_X` (the reference alone, and
     inside a longer string); then revert. A reference to items[5] stops the plan before the
     rename (the name is unchanged). A reference to a later step is INVALID_ARGS, nothing runs.
  f  plan.for_each, reads: one desktop.find per item; `source` + `where` + `expect_count` (2 of
     3); an expect_count that does not hold runs nothing.
  w  plan.for_each, writes (renames of B1 .. B3): without `approve` nothing is renamed; `dry_run`
     answers with a digest and leaves no journal record; `approve` runs it (group = the parent);
     the same `approve` again is refused; `journal.revert(parent)` puts the three names back.
  s  A loop whose second item cannot be renamed stops there (first done, second failed, third
     not_run, group present); `only` with the third item (dry_run, then approve) renames it; both
     parents are reverted.
  x  Overwrite and review. A render.batch step with allow_overwrite=true is INVALID_ARGS; the
     render.batch loop without it is listed with class `output` and needs approve. A second
     server with FLAME_MCP_LOOP_UNATTENDED=1: the rename loop runs in one call and is reverted;
     the allow_overwrite call is refused there too; the delete / reformat loop reports mode
     `unattended` and needsApproval, and a call without `approve` runs nothing.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from collections.abc import Callable
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
    judged,
    rec,
    save,
    save_stage,
    server_session,
)
from p2_4_common import (  # noqa: E402
    flame_exit,
    journal_records,
    journal_states,
    ops_used,
    print_observations,
)
from p3_1a_read_e2e import PRELUDE, fill, is_error, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402
from p3_2a_e2e_common import CKPT_ENV  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.loop_limits import UNATTENDED_ENV  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
NAME_PREFIX: Final[str] = "PP5E"
GROUP_COUNT: Final[int] = 3
FAKE_WIRETAP: Final[str] = "pp5e_no_such_wiretap"
FAKE_UID: Final[str] = "pp5e_no_such_uid"
MAKE_TIMEOUT_S: Final[float] = 180.0
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed"}
)

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


def rename_step(target_uid: Any, new_name: Any, step_id: str = "rn") -> dict[str, Any]:
    return {
        "id": step_id,
        "op": "desktop.rename",
        "args": {"target": batch_sel(target_uid), "name": new_name},
    }


def item(ids: dict[str, Any], index: int) -> dict[str, Any]:
    return {"name": ids[f"B{index}"], "uid": ids[f"u{index}"], "new": ids[f"R{index}"]}


def rename_loop(ids: dict[str, Any], **extra: Any) -> dict[str, Any]:
    items = [item(ids, i) for i in range(1, GROUP_COUNT + 1)]
    return {
        "items": items,
        "key": "name",
        "steps": [rename_step("{{ item.uid }}", "{{ item.new }}")],
        **extra,
    }


def stopping_loop(ids: dict[str, Any], **extra: Any) -> dict[str, Any]:
    items = [item(ids, 1), {**item(ids, 2), "uid": FAKE_UID}, item(ids, 3)]
    return {
        "items": items,
        "key": "name",
        "steps": [rename_step("{{ item.uid }}", "{{ item.new }}")],
        **extra,
    }


def find_step(ids: dict[str, Any]) -> dict[str, Any]:
    return {"id": "f", "op": "desktop.find", "args": {"name": ids["B1"]}}


@call_for("plan_with_references", "plan.run")
def _plan_refs(ids: dict[str, Any]) -> dict[str, Any]:
    steps = [
        find_step(ids),
        rename_step(
            "{{ steps.f.result.items[0].uid }}", "{{ steps.f.result.items[0].name }}_X", "r"
        ),
    ]
    return {"steps": steps, "label": "p107 refs"}


@call_for("plan_with_a_reference_without_a_value", "plan.run")
def _plan_missing(ids: dict[str, Any]) -> dict[str, Any]:
    steps = [find_step(ids), rename_step("{{ steps.f.result.items[5].uid }}", ids["R1"], "r")]
    return {"steps": steps, "label": "p107 missing"}


@call_for("plan_with_a_reference_to_a_later_step", "plan.run")
def _plan_forward(ids: dict[str, Any]) -> dict[str, Any]:
    steps = [
        rename_step("{{ steps.f.result.items[0].uid }}", ids["R1"], "r"),
        find_step(ids),
    ]
    return {"steps": steps, "label": "p107 forward"}


@call_for("read_loop_over_given_items", "plan.for_each")
def _read_items(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": [{"name": ids[f"B{i}"]} for i in range(1, GROUP_COUNT + 1)],
        "key": "name",
        "steps": [
            {
                "id": "f",
                "op": "desktop.find",
                "args": {"name": "{{ item.name }}"},
                "return": ["result.items[*].uid"],
            }
        ],
    }


@call_for("read_loop_over_a_source", "plan.for_each")
def _read_source(ids: dict[str, Any], expect: int = 2) -> dict[str, Any]:
    return {
        "source": {
            "op": "desktop.list_children",
            "args": {"target": {"kind": "desktop"}, "types": ["PyBatch"], "limit": 500},
            "path": "result.items[*]",
        },
        "where": [{"path": "name", "in": [ids["B1"], ids["B2"]]}],
        "key": "name",
        "expect_count": expect,
        "steps": [
            {"id": "n", "op": "desktop.find", "args": {"uid": "{{ item.uid }}"}, "return": []}
        ],
    }


@call_for("read_loop_with_a_wrong_expect_count", "plan.for_each")
def _read_wrong(ids: dict[str, Any]) -> dict[str, Any]:
    return _read_source(ids, expect=3)


@call_for("write_loop_of_renames", "plan.for_each")
def _write_loop(ids: dict[str, Any]) -> dict[str, Any]:
    return rename_loop(ids, label="p107 rename")


@call_for("write_loop_that_stops", "plan.for_each")
def _stop_loop(ids: dict[str, Any]) -> dict[str, Any]:
    return stopping_loop(ids, label="p107 stop")


@call_for("write_loop_that_goes_on_with_only", "plan.for_each")
def _only_loop(ids: dict[str, Any]) -> dict[str, Any]:
    return stopping_loop(ids, label="p107 only", only=[ids["B3"]])


@call_for("render_loop_with_allow_overwrite", "plan.for_each")
def _render_overwrite(ids: dict[str, Any]) -> dict[str, Any]:
    return render_loop(ids, {"allow_overwrite": True})


@call_for("render_loop", "plan.for_each")
def _render_loop(ids: dict[str, Any]) -> dict[str, Any]:
    return render_loop(ids, {})


def render_loop(ids: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"target": batch_sel("{{ item.uid }}"), **extra}
    step = {"id": "rb", "op": "render.batch", "args": args}
    return {
        "items": [{"name": ids["B1"], "uid": ids["u1"]}],
        "key": "name",
        "steps": [step],
        "dry_run": True,
    }


@call_for("rewrite_loop", "plan.for_each")
def _rewrite_loop(ids: dict[str, Any]) -> dict[str, Any]:
    fake = {"kind": "wiretap", "id": FAKE_WIRETAP}
    return {
        "items": [{"name": ids["B1"]}],
        "key": "name",
        "steps": [
            {"id": "d", "op": "desktop.delete", "args": {"targets": [fake]}},
            {
                "id": "rf",
                "op": "desktop.reformat",
                "args": {"targets": [fake], "width": 1920, "height": 1080},
            },
        ],
        "label": "p107 rewrite",
    }


@call_for("revert_a_group", "journal.revert")
def _revert(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["group"]}


# -- transport scaffolding (never through the op under test) -----------------------------------

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

MAKE_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = _d.create_batch_group(@NAME@)",
        "_result = {'uid': _v(_bg.uid), 'name': _v(_bg.name)}",
    ]
)

NAMES_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_result = {_v(_b.uid): _v(_b.name) for _b in _d.batch_groups",
        "           if _v(_b.name).startswith(@P@)}",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "make": MAKE_CODE,
    "names": NAMES_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}_{tail}"


async def names_now(ctx: Ctx) -> dict[str, str] | None:
    got = await tx_val(ctx, fill(NAMES_CODE, P=f"{NAME_PREFIX}_{ctx.ts}"), "p107_names")
    return None if is_error(got) or not isinstance(got, dict) else got


async def names_are(ctx: Ctx, expected: dict[int, str]) -> tuple[bool, dict[str, str] | None]:
    """Whether each group (by number) has the expected name, read through the transport."""
    now = await names_now(ctx)
    ok = now is not None and all(now.get(ctx.ids[f"u{i}"]) == n for i, n in expected.items())
    return ok, now


def originals(ctx: Ctx) -> dict[int, str]:
    return {i: ctx.ids[f"B{i}"] for i in range(1, GROUP_COUNT + 1)}


def renamed(ctx: Ctx, which: tuple[int, ...] = (1, 2, 3)) -> dict[int, str]:
    return {i: (ctx.ids[f"R{i}"] if i in which else ctx.ids[f"B{i}"]) for i in originals(ctx)}


async def call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    return await do(session, ctx, op, {**build(ctx.ids), **extra})


async def revert(session: Any, ctx: Ctx, group: str) -> dict[str, Any]:
    ctx.ids["group"] = group
    return await call(session, ctx, "revert_a_group")


def group_of(env: dict[str, Any]) -> str | None:
    value = (env.get("journal") or {}).get("group") or _result_of(env).get("group")
    return value if isinstance(value, str) else None


def journal_count(ctx: Ctx) -> int:
    return len(journal_records(ctx))


def statuses(env: dict[str, Any]) -> list[Any]:
    return [i.get("status") for i in _result_of(env).get("items") or []]


def mentions(env: dict[str, Any], text: str) -> bool:
    return text in json.dumps(env, default=str)


# -- prepare -----------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    project = await tx_val(ctx, PROJECT_CODE, "p107_project")
    rec(
        ctx,
        "prepare_the_project_is_the_probe_project",
        project == TARGET_PROJECT,
        project=project,
    )
    if project != TARGET_PROJECT:
        return
    for i in range(1, GROUP_COUNT + 1):
        ctx.ids[f"B{i}"] = name_of(ctx, f"B{i}")
        ctx.ids[f"R{i}"] = name_of(ctx, f"R{i}")
        made = await tx_val(
            ctx, fill(MAKE_CODE, NAME=ctx.ids[f"B{i}"]), "p107_make", MAKE_TIMEOUT_S
        )
        ok = isinstance(made, dict) and made.get("name") == ctx.ids[f"B{i}"]
        rec(ctx, f"prepare_batch_group_B{i}_is_made", ok, made=made)
        if not ok:
            return
        ctx.ids[f"u{i}"] = made["uid"]
    ok, now = await names_are(ctx, originals(ctx))
    rec(ctx, "prepare_the_three_groups_have_their_names", ok, names=now)


# -- stage r: plan.run references --------------------------------------------------------------


def refused(env: dict[str, Any], code: str) -> bool:
    return env.get("ok") is False and env.get("errorCode") == code


def rec_refused(
    ctx: Ctx, name: str, env: dict[str, Any], code: str, *, also: bool = True, **extra: Any
) -> None:
    """Record that the call was refused with `code` (and `also`), with the error as evidence."""
    rec(
        ctx,
        name,
        refused(env, code) and also,
        errorCode=env.get("errorCode"),
        error=env.get("error"),
        **extra,
    )


async def check_r_references(session: Any, ctx: Ctx) -> None:
    env = await call(session, ctx, "plan_with_references")
    group = group_of(env)
    ok, now = await names_are(ctx, {**originals(ctx), 1: ctx.ids["B1"] + "_X"})
    steps = [s.get("id") for s in _result_of(env).get("steps") or []]
    good, parts = judged(
        ok=env.get("ok") is True,
        ids=steps == ["f", "r"],
        renamed=ok,
        group=group is not None,
    )
    rec(
        ctx,
        "r1_a_reference_gives_the_uid_and_is_embedded_in_a_longer_string",
        good,
        clauses=parts,
        error=env.get("error"),
        steps=steps,
        names=now,
    )
    if group is None:
        return
    back = await revert(session, ctx, group)
    ok, now = await names_are(ctx, originals(ctx))
    rec(
        ctx,
        "r2_the_plan_group_is_reverted_and_the_name_is_back",
        back.get("ok") is True and ok,
        error=back.get("error"),
        names=now,
    )


async def check_r_stops(session: Any, ctx: Ctx) -> None:
    env = await call(session, ctx, "plan_with_a_reference_without_a_value")
    ok, now = await names_are(ctx, originals(ctx))
    good, parts = judged(
        failed=env.get("ok") is False,
        first_done=(env.get("partial") or {}).get("done") == 1,
        unchanged=ok,
        says_which=mentions(env, "items[5]"),
    )
    rec(
        ctx,
        "r3_a_reference_with_no_value_stops_the_plan_before_the_step_is_sent",
        good,
        clauses=parts,
        errorCode=env.get("errorCode"),
        error=env.get("error"),
        partial=env.get("partial"),
        names=now,
    )
    before = journal_count(ctx)
    env = await call(session, ctx, "plan_with_a_reference_to_a_later_step")
    ok, now = await names_are(ctx, originals(ctx))
    rec_refused(
        ctx,
        "r4_a_reference_to_a_later_step_is_refused_and_nothing_runs",
        env,
        "INVALID_ARGS",
        also=ok and journal_count(ctx) == before,
        names=now,
    )


async def stage_r(ctx: Ctx) -> None:
    async with server_session(ctx, "p107-r") as session:
        await check_r_references(session, ctx)
        await check_r_stops(session, ctx)


# -- stage f: reads ----------------------------------------------------------------------------


async def stage_f(ctx: Ctx) -> None:
    async with server_session(ctx, "p107-f") as session:
        before = journal_count(ctx)
        env = await call(session, ctx, "read_loop_over_given_items")
        result = _result_of(env)
        uids = [ctx.ids[f"u{i}"] for i in range(1, GROUP_COUNT + 1)]
        good, parts = judged(
            ok=env.get("ok") is True,
            all_done=statuses(env) == ["done"] * GROUP_COUNT,
            keys=result.get("keys") == [ctx.ids[f"B{i}"] for i in range(1, GROUP_COUNT + 1)],
            uids=all(mentions({"items": result.get("items")}, u) for u in uids),
            no_journal=journal_count(ctx) == before,
        )
        rec(
            ctx,
            "f1_a_read_loop_runs_one_step_per_item_and_returns_the_chosen_path",
            good,
            clauses=parts,
            error=env.get("error"),
            statuses=statuses(env),
        )
        env = await call(session, ctx, "read_loop_over_a_source")
        result = _result_of(env)
        good, parts = judged(
            ok=env.get("ok") is True,
            two=result.get("total") == 2,
            keys=sorted(result.get("keys") or []) == sorted([ctx.ids["B1"], ctx.ids["B2"]]),
        )
        rec(
            ctx,
            "f2_source_where_and_expect_count_keep_two_of_the_three",
            good,
            clauses=parts,
            error=env.get("error"),
            keys=result.get("keys"),
        )
        env = await call(session, ctx, "read_loop_with_a_wrong_expect_count")
        rec_refused(ctx, "f3_an_expect_count_that_does_not_hold_runs_nothing", env, "INVALID_ARGS")


# -- stage w: a confirmed write loop -----------------------------------------------------------


async def check_w_before_approve(session: Any, ctx: Ctx) -> str | None:
    """w1 .. w3; returns the digest of the dry run (None when there is none)."""
    env = await call(session, ctx, "write_loop_of_renames")
    ok, now = await names_are(ctx, originals(ctx))
    rec_refused(
        ctx,
        "w1_without_approve_nothing_is_renamed",
        env,
        "PRECONDITION_UNMET",
        also=ok,
        names=now,
    )
    before = journal_count(ctx)
    env = await call(session, ctx, "write_loop_of_renames", dry_run=True)
    result = _result_of(env)
    digest = result.get("digest")
    ok, now = await names_are(ctx, originals(ctx))
    good, parts = judged(
        ok=env.get("ok") is True,
        digest=isinstance(digest, str),
        count=result.get("count") == GROUP_COUNT,
        needs_approval=result.get("needsApproval") is True,
        mode=result.get("mode") == "attended",
        unchanged=ok,
        no_journal=journal_count(ctx) == before,
    )
    rec(
        ctx,
        "w2_dry_run_lists_the_items_writes_nothing_and_leaves_no_journal_record",
        good,
        clauses=parts,
        error=env.get("error"),
        names=now,
    )
    if not isinstance(digest, str):
        return None
    env = await call(session, ctx, "write_loop_of_renames", approve="0" * 64)
    ok, now = await names_are(ctx, originals(ctx))
    rec_refused(
        ctx, "w3_a_wrong_digest_runs_nothing", env, "PRECONDITION_UNMET", also=ok, names=now
    )
    return digest


async def check_w_approve_and_revert(session: Any, ctx: Ctx, digest: str) -> None:
    """w4 .. w6."""
    env = await call(session, ctx, "write_loop_of_renames", approve=digest)
    group = group_of(env)
    ok, now = await names_are(ctx, renamed(ctx))
    good, parts = judged(
        ok=env.get("ok") is True,
        all_done=statuses(env) == ["done"] * GROUP_COUNT,
        renamed=ok,
        group=group is not None,
    )
    rec(
        ctx,
        "w4_approve_runs_the_loop_and_returns_the_parent_group",
        good,
        clauses=parts,
        error=env.get("error"),
        statuses=statuses(env),
        names=now,
    )
    env = await call(session, ctx, "write_loop_of_renames", approve=digest)
    rec_refused(
        ctx,
        "w5_the_same_approve_works_once_only",
        env,
        "PRECONDITION_UNMET",
        also=mentions(env, "already used"),
    )
    if group is None:
        return
    back = await revert(session, ctx, group)
    ok, now = await names_are(ctx, originals(ctx))
    rec(
        ctx,
        "w6_revert_of_the_parent_puts_the_three_names_back",
        back.get("ok") is True and ok,
        error=back.get("error"),
        names=now,
    )


async def stage_w(ctx: Ctx) -> None:
    async with server_session(ctx, "p107-w") as session:
        digest = await check_w_before_approve(session, ctx)
        if digest is not None:
            await check_w_approve_and_revert(session, ctx, digest)


# -- stage s: a stop and only ------------------------------------------------------------------


async def confirmed(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    """dry_run, then the call with the digest it gave (the answer of the second call)."""
    first = await call(session, ctx, call_id, dry_run=True, **extra)
    digest = _result_of(first).get("digest")
    if not isinstance(digest, str):
        return first
    return await call(session, ctx, call_id, approve=digest, **extra)


async def check_s_stop(session: Any, ctx: Ctx) -> str | None:
    """s1; returns the group of the loop that stopped."""
    env = await confirmed(session, ctx, "write_loop_that_stops")
    result = _result_of(env)
    group = group_of(env)
    ok, now = await names_are(ctx, renamed(ctx, (1,)))
    good, parts = judged(
        failed=env.get("ok") is False,
        statuses=statuses(env) == ["done", "failed", "not_run"],
        stopped=result.get("stoppedAt") == ctx.ids["B2"],
        not_run=result.get("notRun") == [ctx.ids["B3"]],
        names=ok,
        group=group is not None,
    )
    rec(
        ctx,
        "s1_the_loop_stops_at_the_failed_item_and_says_what_ran",
        good,
        clauses=parts,
        errorCode=env.get("errorCode"),
        error=env.get("error"),
        statuses=statuses(env),
        names=now,
    )
    return group


async def check_s_only(session: Any, ctx: Ctx) -> str | None:
    """s2; returns the group of the loop that ran the item left."""
    env = await confirmed(session, ctx, "write_loop_that_goes_on_with_only")
    group = group_of(env)
    ok, now = await names_are(ctx, renamed(ctx, (1, 3)))
    good, parts = judged(
        ok=env.get("ok") is True,
        statuses=statuses(env) == ["done"],
        names=ok,
        group=group is not None,
    )
    rec(
        ctx,
        "s2_only_runs_the_item_that_was_left",
        good,
        clauses=parts,
        error=env.get("error"),
        statuses=statuses(env),
        names=now,
    )
    return group


async def stage_s(ctx: Ctx) -> None:
    async with server_session(ctx, "p107-s") as session:
        first_group = await check_s_stop(session, ctx)
        second_group = await check_s_only(session, ctx)
        for number, group in (("3", second_group), ("4", first_group)):
            if group is None:
                continue
            back = await revert(session, ctx, group)
            rec(
                ctx,
                f"s{number}_revert_of_the_parent_group",
                back.get("ok") is True,
                error=back.get("error"),
            )
        ok, now = await names_are(ctx, originals(ctx))
        rec(ctx, "s5_all_three_names_are_back", ok, names=now)


# -- stage x: overwrite, review, unattended ----------------------------------------------------


async def check_x_attended(session: Any, ctx: Ctx) -> None:
    """x1, x2: the server without FLAME_MCP_LOOP_UNATTENDED."""
    before = journal_count(ctx)
    env = await call(session, ctx, "render_loop_with_allow_overwrite")
    rec_refused(
        ctx,
        "x1_allow_overwrite_in_a_loop_is_refused",
        env,
        "INVALID_ARGS",
        also=mentions(env, "allow_overwrite") and journal_count(ctx) == before,
    )
    env = await call(session, ctx, "render_loop")
    result = _result_of(env)
    review = result.get("review") or []
    rec(
        ctx,
        "x2_a_render_loop_is_listed_for_review_with_class_output_and_not_run",
        env.get("ok") is True
        and result.get("needsApproval") is True
        and [r.get("class") for r in review] == ["output"]
        and result.get("ran") is False,
        error=env.get("error"),
        review=review,
    )


async def check_x_unattended_renames(session: Any, ctx: Ctx) -> None:
    """x3, x4: the server with FLAME_MCP_LOOP_UNATTENDED=1."""
    env = await call(session, ctx, "write_loop_of_renames")
    group = group_of(env)
    ok, now = await names_are(ctx, renamed(ctx))
    good, parts = judged(
        ok=env.get("ok") is True,
        all_done=statuses(env) == ["done"] * GROUP_COUNT,
        renamed=ok,
        group=group is not None,
    )
    rec(
        ctx,
        "x3_unattended_a_loop_of_renames_runs_in_one_call",
        good,
        clauses=parts,
        errorCode=env.get("errorCode"),
        error=env.get("error"),
        names=now,
    )
    if group is not None:
        back = await revert(session, ctx, group)
        ok, now = await names_are(ctx, originals(ctx))
        rec(ctx, "x4_the_unattended_loop_is_reverted", back.get("ok") is True and ok, names=now)


async def check_x_unattended_refusals(session: Any, ctx: Ctx) -> None:
    """x5 .. x7: the same server."""
    before = journal_count(ctx)
    env = await call(session, ctx, "render_loop_with_allow_overwrite")
    rec_refused(
        ctx,
        "x5_unattended_allow_overwrite_in_a_loop_is_refused_too",
        env,
        "INVALID_ARGS",
        also=mentions(env, "allow_overwrite") and journal_count(ctx) == before,
    )
    env = await call(session, ctx, "rewrite_loop", dry_run=True)
    result = _result_of(env)
    classes = sorted(r.get("class") for r in result.get("review") or [])
    rec(
        ctx,
        "x6_unattended_a_delete_and_reformat_loop_is_listed_as_rewrite_and_needs_approve",
        env.get("ok") is True
        and result.get("mode") == "unattended"
        and result.get("needsApproval") is True
        and classes == ["rewrite", "rewrite"],
        error=env.get("error"),
        mode=result.get("mode"),
        classes=classes,
    )
    before = journal_count(ctx)
    env = await call(session, ctx, "rewrite_loop")
    rec_refused(
        ctx,
        "x7_unattended_the_same_loop_without_approve_runs_nothing",
        env,
        "PRECONDITION_UNMET",
        also=journal_count(ctx) == before,
    )


async def stage_x(ctx: Ctx) -> None:
    async with server_session(ctx, "p107-x") as session:
        await check_x_attended(session, ctx)
    async with server_session(ctx, "p107-x-unattended", hooks={UNATTENDED_ENV: "1"}) as session:
        await check_x_unattended_renames(session, ctx)
        await check_x_unattended_refusals(session, ctx)


# -- z / driver --------------------------------------------------------------------------------


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
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "p107_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "p107_left")
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
        if all(f"u{i}" in ctx.ids for i in range(1, GROUP_COUNT + 1)):
            for name, stage in (
                ("r", stage_r),
                ("f", stage_f),
                ("w", stage_w),
                ("s", stage_s),
                ("x", stage_x),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "p107_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run --------------------------------------------------------------------------------


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    ids: dict[str, Any] = {"group": "g_dry"}
    for i in range(1, GROUP_COUNT + 1):
        ids.update({f"B{i}": f"B{i}_dry", f"R{i}": f"R{i}_dry", f"u{i}": f"uid{i}"})
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
        if not validation.ok:
            problems.append(
                "invalid args: " + "; ".join(f"{i.path}: {i.message}" for i in validation.issues)
            )
        if op.thread != "server":
            checked = dict(validation.value or {})
            try:
                compile(execution.build_exec_code(op, checked), f"<{call_id}>", "exec")
            except SyntaxError as exc:
                problems.append(f"compile FAILED: {exc}")
        for index, step in enumerate(args.get("steps") or []):
            step_op = registry.get(step["op"])
            if step_op is None:
                problems.append(f"step {index}: unknown op {step['op']}")
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
    journal_dir = OUTPUT_DIR / f"journal_p107_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p107_plan_loop_e2e_{ts}.json",
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
