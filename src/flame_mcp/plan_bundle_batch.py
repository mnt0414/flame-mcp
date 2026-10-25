"""The Batch half of `plan.run`'s bundle (`plan_bundle`): the up-front resolution of the node
selectors of a plan that holds a Batch-checkpointed write (`node.create`, `delete`, `connect`,
`action.create_node`, `batch.create_reel` ...), and what runs one of its steps.

The steps run without a checkpoint of their own, but each op's `check` (the reads that refuse a
typo before anything is written) runs in the step's own turn, so a step that points at a node an
earlier step made is checked once that node is there.

`late`: a step that points at a node an earlier step makes (or renames) cannot be found by the
up-front check. A step whose node selector names a node an earlier `node.create` of the plan gave
`name` in the same group is left out of it by itself (`resolved[].late == 'implied'`); any other
step that needs it (a node left to Flame's default name, a `node.duplicate`) says `late: true`.
Either way the node is found when the step's turn comes, and if it is not there the plan stops
there with what the earlier steps did still applied (and revertible).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution, selectors
from flame_mcp.callargs import apply_param_defaults
from flame_mcp.checkpointed import declared_run_args
from flame_mcp.checkpoints import BATCH_KIND
from flame_mcp.plan_bundle import (
    SKIPPED_KEY,
    BundleTarget,
    StepLike,
    is_bundled_op,
    single_target,
    unreadable,
    unresolved,
)
from flame_mcp.reel_kinds import reel_kinds_named
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.types import Category, OperationSpec, Refusal

if TYPE_CHECKING:
    from flame_mcp.runtime import ServerContext

SPECS_ARG: Final[str] = "_specs"
CREATE_OP: Final[str] = "node.create"
FREEING_OPS: Final[frozenset[str]] = frozenset({"node.delete", "node.set_attributes"})
"""Ops that can free a node name (a delete, a rename): a `node.create` after one of them may take
a name that is taken when the plan starts. A `node.set_attributes` frees one only when it writes
`name` (`frees_name`)."""
IMPLIED: Final[str] = "implied"
LATE_HINT: Final[str] = (
    "A step that points at a node an earlier step of the plan makes (a node left to Flame's "
    "default name, a node.duplicate, a rename) is not found by the up-front check: give it "
    "`late: true` and it is resolved when its turn comes. A node an earlier node.create "
    "gave `name` is found without it."
)
CREATED_AS_HINT: Final[str] = (
    "The node was made as {actual!r}, not as {wanted!r}. A later step must point at "
    "{actual!r} or carry `late: true`."
)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def group_selector(step: StepLike) -> dict[str, Any]:
    """The `batch` selector of the group the step writes to."""
    op = step.op
    assert op is not None
    spec = op.checkpoint
    if spec is not None:
        if spec.group_selector is not None:
            return dict(spec.group_selector(step.args))
        return dict(step.args[spec.batch_arg])
    sel: dict[str, Any] = step.args["target"]
    if sel.get("kind") == "action_node":
        sel = sel["node"]
    return dict(sel["batch"])


def node_selectors(step: StepLike) -> list[dict[str, Any]]:
    """The `node` selectors the step's arguments hold (the node of an `action_node` selector
    included), in argument order."""
    found: list[dict[str, Any]] = []
    assert step.op is not None
    selector_params = {p.name for p in step.op.params if p.type == "selector"}
    for key, value in step.args.items():
        if key not in selector_params or not isinstance(value, dict):
            continue
        if value.get("kind") == "node":
            found.append(value)
        elif value.get("kind") == "action_node":
            found.append(value["node"])
    return found


def frees_name(step: StepLike) -> bool:
    """The step can free a node name: a delete, or a `node.set_attributes` that writes `name`."""
    if step.op_name not in FREEING_OPS:
        return False
    if step.op_name != "node.set_attributes":
        return True
    values = step.args.get("values")
    return isinstance(values, dict) and "name" in values


def _same(selector: Mapping[str, Any]) -> str:
    return json.dumps(selector, sort_keys=True)


@dataclass(frozen=True)
class _Row:
    """One selector of one write step, as sent to Flame."""

    step: StepLike
    node: dict[str, Any] | None
    late: bool
    implied: bool
    create: str | None


def _rows(writes: Sequence[StepLike]) -> tuple[list[_Row], list[dict[str, Any]]]:
    """The rows and, beside each, the spec sent to Flame. A node selector that names a node an
    earlier `node.create` gave `name` in the same group (the group addressed the same way) is
    `implied` late."""
    made: dict[str, set[str]] = {}
    rows: list[_Row] = []
    specs: list[dict[str, Any]] = []
    for step in writes:
        batch = group_selector(step)
        key = _same(batch)
        nodes = node_selectors(step)
        wanted = step.args.get("name") if step.op_name == CREATE_OP else None
        if not nodes:
            rows.append(_Row(step, None, step.late, False, wanted))
            specs.append({"batch": batch, "node": None, "late": step.late, "create": wanted})
        for sel in nodes:
            implied = not step.late and sel["name"] in made.get(key, set())
            late = step.late or implied
            rows.append(_Row(step, sel, late, implied, None))
            specs.append({"batch": batch, "node": sel, "late": late, "create": None})
        if isinstance(wanted, str):
            made.setdefault(key, set()).add(wanted)
    return rows, specs


def _preresolve_to_python(args: Mapping[str, Any]) -> str:
    blocks = ["_rows = []"]
    for i, spec in enumerate(args.get(SPECS_ARG) or []):
        resolve = "\n".join(
            "    " + line for line in selectors.resolve_code(spec["batch"], "_b").splitlines()
        )
        blocks.append(f"def _pr{i}():\n{resolve}\n    return _b")
        node = spec.get("node") or {}
        pos = list(node["pos"]) if "pos" in node else None
        blocks.append(
            f"_rows.append(_nd_pre_row(_pr{i}, {spec['late']!r}, {node.get('name')!r}, {pos!r}, "
            f"{spec.get('create')!r}))"
        )
    blocks.append("_result = {'rows': _rows}")
    return "\n\n".join(blocks)


@cache
def preresolve_op() -> RegisteredOperation:
    """The Flame-side read as an op of a registry of its own (never in the public catalog)."""
    registry = Registry()
    registry.register_category(
        Category(
            name="batch",
            description="internal",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    return registry.register(
        OperationSpec(
            name="batch.preresolve",
            category="batch",
            description="internal: resolve a bundled plan's node selectors up front",
            effect="read",
            to_python=_preresolve_to_python,
            undo_steps=_no_undo_steps,
            requires=("PyBatch.nodes", "PyBatch.uid", "PyBatch.name", "PyNode.attributes"),
        )
    )


def _issues(
    writes: Sequence[StepLike], rows: Sequence[_Row], answers: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The first selector of each step that does not resolve (or a `node.create` name that is
    taken and that no earlier step of the plan frees)."""
    issues: dict[int, dict[str, Any]] = {}
    for row, answer in zip(rows, answers, strict=True):
        step = row.step
        if step.index in issues:
            continue
        if not answer["ok"]:
            code, text = answer.get("errorCode"), answer.get("error")
        elif answer.get("nameTaken") is True and not any(
            earlier.index < step.index and frees_name(earlier) for earlier in writes
        ):
            code = "INVALID_ARGS"
            text = (
                f"the group already has a node named {row.create!r} (in a non-current "
                "group Flame would silently make a second one)"
            )
        else:
            continue
        issues[step.index] = {
            "index": step.index,
            "op": step.op_name,
            "errorCode": code,
            "error": text,
        }
    return list(issues.values())


def _hint(writes: Sequence[StepLike], issues: Sequence[Mapping[str, Any]]) -> str:
    """LATE_HINT, and when a step that does not resolve names a node an earlier node.create of
    the plan made, why that was not taken for granted."""
    first_made: dict[str, int] = {}
    for step in writes:
        wanted = step.args.get("name") if step.op_name == CREATE_OP else None
        if isinstance(wanted, str):
            first_made.setdefault(wanted, step.index)
    notes = []
    for issue in issues:
        step = next(s for s in writes if s.index == issue["index"])
        for sel in node_selectors(step):
            made_at = first_made.get(sel["name"])
            if made_at is not None and made_at < step.index:
                notes.append(
                    f"Step {made_at} (node.create) gives {sel['name']!r} to a node; step "
                    f"{step.index} finds it without `late: true` only when it addresses the "
                    "Batch group with the same selector as that step (the same name, or the same "
                    "uid). Use the same selector, or give the step `late: true`."
                )
    return " ".join([LATE_HINT, *dict.fromkeys(notes)])


def preresolve(ctx: ServerContext, steps: Sequence[StepLike]) -> BundleTarget:
    """Resolve every write step's selectors in one Flame read. Raises `ServerOpError` (and nothing
    has been journaled or copied) when one does not resolve, when the writes are not all to one
    Batch group, or when that one cannot be found again by its uid."""
    writes = [s for s in steps if s.op is not None and is_bundled_op(s.op, BATCH_KIND)]
    rows, specs = _rows(writes)
    env = execution.run_op(preresolve_op(), {SPECS_ARG: specs}, run_dir=ctx.run_dir)
    raw = env.get("result")
    value: dict[str, Any] = raw if isinstance(raw, dict) else {}
    answers = value.get("rows")
    if env.get("ok") is not True or not isinstance(answers, list) or len(answers) != len(rows):
        raise unreadable(env)
    issues = _issues(writes, rows, answers)
    if issues:
        raise unresolved(issues, _hint(writes, issues))
    ident, stable, uid = single_target(
        BATCH_KIND, [(r.step, a["target"]) for r, a in zip(rows, answers, strict=True)]
    )
    return BundleTarget(BATCH_KIND, ident, stable, uid, _resolved(writes, rows, answers))


def _resolved(
    writes: Sequence[StepLike], rows: Sequence[_Row], answers: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    out = []
    for step in writes:
        mine = [(r, a) for r, a in zip(rows, answers, strict=True) if r.step is step]
        nodes = []
        for row, answer in mine:
            if row.node is None:
                continue
            seen = answer.get("node") or {}
            nodes.append(
                {
                    "name": row.node["name"],
                    "pos": seen.get("pos"),
                    "type": seen.get("type"),
                    "late": row.late,
                }
            )
        late: bool | str = step.late or (IMPLIED if any(r.implied for r, _a in mine) else False)
        out.append({"index": step.index, "op": step.op_name, "late": late, "nodes": nodes})
    return out


def renamed_by_flame(op_name: str, args: Mapping[str, Any], env: Mapping[str, Any]) -> str | None:
    """The name a `node.create` really gave the node when it is not the one asked for (Flame
    names a node against the CURRENT group, so naming can end in another name or leave the
    default one), None when it is as asked or no name was asked for."""
    wanted = args.get("name")
    result = env.get("result")
    if op_name != CREATE_OP or wanted is None or not isinstance(result, Mapping):
        return None
    row = result.get("node")
    actual = row.get("name") if isinstance(row, Mapping) else None
    if actual is None and env.get("ok") is not True:
        actual = result.get("createdAs")
    return actual if isinstance(actual, str) and actual != wanted else None


SHELF_REFUSAL: Final[str] = (
    "nothing was run: reel {reel!r} is a shelf reel. Shelf reels are outside a Batch setup, so "
    "the plan's revert would leave the clips in it; call batch.add_clips on its own"
)


def shelf_refusal(ctx: ServerContext, op_name: str, args: Mapping[str, Any]) -> Refusal | None:
    """A `batch.add_clips` step whose reel is a shelf reel: the one restore of the plan would not
    take the clips out. Read in the step's turn, after the op's own check has found the reel."""
    if op_name != "batch.add_clips":
        return None
    env = ctx.do("batch.info", {"target": args["target"]})
    info = env.get("result") if env.get("ok") is True else None
    if not isinstance(info, Mapping):
        return None
    if "shelf" not in reel_kinds_named(info, args["reel"], args.get("reel_kind")):
        return None
    return Refusal(
        "PRECONDITION_UNMET",
        SHELF_REFUSAL.format(reel=args["reel"]),
        result={"ran": False},
    )


def _refused(op: RegisteredOperation, refusal: Any) -> dict[str, Any]:
    env = envelope_mod.build_envelope(
        ok=False,
        result=refusal.result,
        error=refusal.message,
        error_code=refusal.code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=0,
        hint_override=refusal.hint,
    )
    env[SKIPPED_KEY] = True
    return env


def _check_created_name(step: StepLike, env: dict[str, Any]) -> dict[str, Any]:
    assert step.op_name is not None
    actual = renamed_by_flame(step.op_name, step.args, env)
    if actual is None:
        return env
    wanted = step.args["name"]
    if env.get("ok") is True:
        env["ok"] = False
        env["errorCode"] = "FLAME_API_ERROR"
        env["error"] = f"the node was made as {actual!r}, not as the requested {wanted!r}"
    hint = CREATED_AS_HINT.format(actual=actual, wanted=wanted)
    env["hint"] = f"{env.get('hint') or ''} {hint}".strip()
    return env


def _rebound_value_write(
    op: RegisteredOperation, args: dict[str, Any], stable: Any
) -> dict[str, Any]:
    sel = args["target"]
    if op.name == "node.set_attributes":
        return {**args, "target": {**sel, "batch": stable}}
    if op.name == "action.set_attributes":
        return {**args, "target": {**sel, "node": {**sel["node"], "batch": stable}}}
    return args


def step_runner(
    ctx: ServerContext, target: BundleTarget, group: str
) -> Callable[[StepLike], dict[str, Any]]:
    """What runs one step of a bundled plan: a Batch-checkpointed step without its own checkpoint
    (its `check` first, in its own turn), a `node.set_attributes` / `action.set_attributes` and
    every other step through the normal do path -- the group's selector replaced by the one
    pinned by uid."""

    def run(step: StepLike) -> dict[str, Any]:
        op = step.op
        assert op is not None and step.op_name is not None
        spec = op.checkpoint
        if spec is None or spec.target_kind != BATCH_KIND.name:
            args = _rebound_value_write(op, step.args, target.stable)
            return ctx.do(step.op_name, args, group=group)
        filled = dict(step.args)
        apply_param_defaults(op, filled)
        run_args = declared_run_args(spec, filled, target.stable)
        if spec.check is not None:
            refusal = spec.check(ctx, run_args)
            if refusal is not None:
                return _refused(op, refusal)
        refusal = shelf_refusal(ctx, step.op_name, run_args)
        if refusal is not None:
            return _refused(op, refusal)
        env = execution.run_op(op, run_args, run_dir=ctx.run_dir, timeout_s=spec.timeout_s)
        env["journal"] = {"group": group, "bundled": True}
        return _check_created_name(step, env)

    return run
