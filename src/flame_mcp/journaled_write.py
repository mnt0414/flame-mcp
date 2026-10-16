"""The journaled write: two round trips and a compare-and-swap.

For a `value`-undo op that carries a `JournalSpec` (desktop.rename and every
`*.set_attributes`) the server does, under `Runtime.write_lock`:

  1. pre-read   the spec's read op (get_attributes for the same target and keys) returns the
                previous values and a stable address of the target (wiretap id / batch uid /
                node name -- not the caller's `selection` or `path`, which can move)
  2. intent     {op, args, target, before, after} is appended and flushed before anything is
                written: if this process dies from here on, the journal still says what was
                about to happen and what the values were
  3. write      the op itself, addressed by the stable target, with `_expected` = the values
                read in 1. The generated code compares every key's current value with
                `_expected` right before writing and writes nothing if one differs (CONFLICT)
  4. outcome    {ok, applied, failed, errorCode, readBack} is appended and flushed

If the write's answer never comes (TIMEOUT / BRIDGE_LOST / BRIDGE_UNAVAILABLE) there is no
outcome: the intent stays open, an `unresolved` note is added, and `WriteGuard` refuses every
later non-read op until `journal.check` has read Flame again and decided what happened.
Nothing here retries.

A pre-read that fails means nothing was written and nothing was journaled: its error is
returned as the write's own (prefixed), and the write is not attempted.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.callargs import apply_param_defaults
from flame_mcp.journal import new_id, now_iso, values_equal
from flame_mcp.opschema import validate_op_args
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import ErrorCode

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

UNKNOWN_END_CODES: Final[frozenset[str]] = frozenset(
    {"TIMEOUT", "BRIDGE_LOST", "BRIDGE_UNAVAILABLE"}
)
"""Errors after which whether the write landed is not known (the connection was lost, or it
never answered). Everything else -- including CONFLICT and Flame's own exceptions -- is an
answer, and gets an outcome record."""

RESTORING_ARG: Final[str] = "_restoring"
"""Reserved args key set only when the write is a journal.revert: it puts back a recorded value,
so the server-side same-name check of a node rename is skipped (Flame's own check stays)."""

EXPECTED_ARG: Final[str] = "_expected"
"""The reserved args key carrying the compare-and-swap values into `to_python`. It is not a
declared parameter, so `validate_op_args` rejects it from a caller."""


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _fail(
    op: RegisteredOperation,
    code: ErrorCode,
    message: str,
    started: float,
    *,
    result: Any = None,
    hint: str | None = None,
    partial: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=False,
        result=result,
        partial=partial,
        error=message,
        error_code=code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=_elapsed_ms(started),
        hint_override=hint,
    )


def _whole_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _address_after(
    stable: Mapping[str, Any], values: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """`(address, problem)`: where a node (or an inner node) is addressed once `values` are
    written -- under its new name and, for a node whose address carries `pos`, at its new
    position. The address is None when the write changes neither; `problem` says why the new
    address cannot be decided (a node that shares its name is moved to a position that is not two
    integers, so a revert could not find it)."""
    if stable.get("kind") not in ("node", "action_node"):
        return None, None
    after = dict(stable)
    changed = False
    if "name" in values:
        after["name"] = values["name"]
        changed = True
    pos = stable.get("pos")
    if stable.get("kind") == "node" and isinstance(pos, list) and len(pos) == 2:
        moved = [
            _whole_number(values.get(key, old))
            for key, old in zip(("pos_x", "pos_y"), pos, strict=True)
        ]
        if None in moved:
            return None, (
                "the node shares its name with another node of the group and its new position "
                "is not two integers, so a revert could not decide where to find it again"
            )
        if moved != pos:
            after["pos"] = moved
            changed = True
    return (after if changed else None), None


def _read_values(pre: Mapping[str, Any], keys: list[str]) -> tuple[dict[str, Any], dict[str, str]]:
    """The previous values out of the pre-read's result, and a reason for every key that could
    not be read (the write op itself would have rejected such a key)."""
    result = pre.get("result")
    attributes = result.get("attributes") if isinstance(result, dict) else None
    if not isinstance(attributes, dict):
        return {}, {key: "rejected: the pre-read returned no attributes" for key in keys}
    before: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for key in keys:
        entry = attributes.get(key)
        if not isinstance(entry, dict) or "error" in entry or "value" not in entry:
            detail = entry.get("error") if isinstance(entry, dict) else "missing from the pre-read"
            rejected[key] = f"rejected: cannot read the attribute before writing ({detail})"
        else:
            before[key] = entry["value"]
    return before, rejected


def _outcome_record(
    env: Mapping[str, Any], intent_id: str, group: str, keys: list[str]
) -> dict[str, Any]:
    ok = bool(env.get("ok"))
    raw_result = env.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    applied = result.get("applied")
    if not isinstance(applied, list):
        applied = list(keys) if ok else []
    failed = result.get("failed")
    if not isinstance(failed, dict):
        failed = {}
    if not ok and not failed:
        failed = {key: str(env.get("error")) for key in keys if key not in applied}
    read_back = result.get("readBack")
    if not isinstance(read_back, dict):
        read_back = result.get("after") if isinstance(result.get("after"), dict) else None
    return {
        "type": "outcome",
        "id": intent_id,
        "group": group,
        "ts": now_iso(),
        "ok": ok,
        "applied": applied,
        "failed": failed,
        "errorCode": env.get("errorCode"),
        "readBack": read_back,
    }


def run_journaled_write(
    rt: Runtime,
    op: RegisteredOperation,
    op_args: dict[str, Any],
    *,
    group: str | None = None,
    reverts: str | None = None,
    reverts_intent: str | None = None,
    precondition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run `op` (which carries a JournalSpec) as described in the module docstring.

    `group` groups several calls (`plan.run`: one group for the whole plan); None starts a new
    group. `reverts` marks the group as the revert of another (`reverts_intent`: of that
    group's one intent). `precondition` ({key: value}) makes the call refuse with
    CONFLICT, before anything is journaled or written, unless the pre-read values equal it --
    `journal.revert` uses it to put a value back only while it still is what the write left."""
    spec = op.journal
    if spec is None:
        raise RuntimeError(f"'{op.name}' has no journal spec")
    started = time.monotonic()
    read_op = rt.registry.get(spec.read_op)
    if read_op is None:  # Registry.validate_references() rules this out at startup
        raise RuntimeError(f"'{op.name}': journal read op '{spec.read_op}' is not registered")
    keys = spec.keys(op_args)
    values = spec.values(op_args)
    if not keys:  # an empty pre-read key list would mean "every attribute"
        return _fail(op, "INVALID_ARGS", "values must be a non-empty object", started)

    with rt.write_lock:
        denial = rt.guard.denial()  # a write that queued behind the one that just went unknown
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=_elapsed_ms(started),
                hint=denial.hint,
            )

        read_args: dict[str, Any] = {"target": op_args["target"], "keys": keys}
        apply_param_defaults(read_op, read_args)
        pre = execution.run_op(read_op, read_args, run_dir=rt.run_dir)
        if not pre["ok"]:
            failure = dict(pre)
            failure["execution"] = envelope_mod.execution_meta(op)
            failure["error"] = f"pre-read failed, nothing was written: {pre.get('error')}"
            return failure
        before, rejected = _read_values(pre, keys)
        if rejected:  # what the op's own preflight would have said: nothing written, INVALID_ARGS
            detail = "; ".join(f"{key}: {reason}" for key, reason in rejected.items())
            return _fail(
                op,
                "INVALID_ARGS",
                f"nothing was written: {len(rejected)} attribute(s) rejected ({detail})",
                started,
                result={"applied": [], "failed": rejected, "before": {}, "after": {}},
                partial={"applied": 0, "failed": len(rejected)},
            )
        stable = pre["result"].get("stable")
        if not isinstance(stable, dict):
            return _fail(
                op,
                "PRECONDITION_UNMET",
                "nothing was written: the target has no stable address (no wiretap id / uid; "
                "for a node that shares its name with another node of the group, a pos that "
                "reads as two integers), so a journal record could not find it again",
                started,
            )
        if precondition is not None:
            conflicts = {
                key: {"expected": expected, "current": before.get(key)}
                for key, expected in precondition.items()
                if not values_equal(before.get(key), expected)
            }
            if conflicts:
                detail = "; ".join(
                    f"{k}: expected {c['expected']!r}, current {c['current']!r}"
                    for k, c in conflicts.items()
                )
                return _fail(
                    op,
                    "CONFLICT",
                    f"nothing was written: the current value is not the one expected ({detail})",
                    started,
                    result={"applied": [], "failed": {}, "conflicts": conflicts},
                )

        write_args = spec.write_args(stable, values)
        validation = validate_op_args(op, write_args)
        if not validation.ok:  # the stable address must be one the op accepts
            issues = "; ".join(f"{i.path}: {i.message}" for i in validation.issues)
            return _fail(
                op,
                "INVALID_ARGS",
                f"nothing was written: the stable target address is not accepted by "
                f"'{op.name}' ({issues})",
                started,
            )

        # The intent is flushed before the write is sent.
        group_id = group if group is not None else new_id("g")
        intent_id = new_id("i")
        target_after, address_problem = _address_after(stable, values)
        if address_problem is not None:
            return _fail(
                op, "PRECONDITION_UNMET", f"nothing was written: {address_problem}", started
            )
        intent: dict[str, Any] = {
            "type": "intent",
            "id": intent_id,
            "group": group_id,
            "ts": now_iso(),
            "op": op.name,
            "args": op_args,
            "target": stable,
            "targetAfter": target_after,
            "readOp": spec.read_op,
            "before": before,
            "after": values,
        }
        if reverts is not None:
            intent["reverts"] = reverts
        if reverts_intent is not None:
            intent["revertsIntent"] = reverts_intent
        try:
            rt.journal.append(intent)
        except OSError as exc:
            return _fail(
                op,
                "POLICY_DENIED",
                f"nothing was written: the journal cannot be written ({exc}), and this "
                "server does not write to the project without an intent on record",
                started,
                hint=f"Make {rt.journal.directory} writable, then retry.",
            )

        rt.hooks.pause_after_preread()

        exec_args = dict(write_args)
        exec_args[EXPECTED_ARG] = before
        if reverts is not None:
            exec_args[RESTORING_ARG] = True
        apply_param_defaults(op, exec_args)
        env = execution.run_op(
            op, exec_args, run_dir=rt.run_dir, timeout_s=rt.hooks.take_write_timeout()
        )
        rt.hooks.exit_after_write_answer()

        # The outcome is recorded unless no answer came: then the intent stays open and the
        # write guard is shut.
        code_now = env.get("errorCode")
        if code_now in UNKNOWN_END_CODES:
            rt.guard.block(intent_id, group_id, op.name, str(code_now))
            _note_unresolved(rt, intent_id, group_id, env)
            hint = str(env.get("hint") or "")
            env["hint"] = (
                f"{hint} It is not known whether the write landed: this server now refuses every "
                f"write until journal.check (group={group_id!r}) has read Flame again."
            ).strip()
        else:
            try:
                rt.journal.append(_outcome_record(env, intent_id, group_id, keys))
            except OSError as exc:
                rt.guard.block(intent_id, group_id, op.name, "JOURNAL_OUTCOME_NOT_WRITTEN")
                env["degraded"] = [
                    *env.get("degraded", []),
                    f"the outcome could not be journaled ({exc}); the write's answer stands but "
                    "this server now refuses writes until journal.check has settled the intent",
                ]
        env["journal"] = {"group": group_id, "id": intent_id}
        return env


def _note_unresolved(rt: Runtime, intent_id: str, group: str, env: Mapping[str, Any]) -> None:
    try:
        rt.journal.append(
            {
                "type": "unresolved",
                "id": intent_id,
                "group": group,
                "ts": now_iso(),
                "errorCode": env.get("errorCode"),
                "error": env.get("error"),
            }
        )
    except OSError:
        pass  # the intent alone already says "in doubt"; the guard is what matters
