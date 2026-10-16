"""`desktop.create` / `desktop.copy`, server side.

Both make new things on the Desktop (a Batch group, a reel group, a reel; clips in a reel) that
nothing but a delete undoes: a Batch group's creation also moves the current group, and Flame's
native Undo does not put it back (measured on Flame 2025.2.4), so neither op uses it. They are a
write-ahead pair like `render.batch` (`render_run.py`), with one round trip more in front, the
journaled-write way:

  1. pre-read   the op's own Flame code with `_phase: "read"`: what the parent / destination holds
                (names, uids), the current Batch group, a stable address of the parent /
                destination (its wiretap id), and every refusal that needs no write (a selector
                that does not resolve, a Batch reel as destination, a taken name without
                `allow_rename`). A refusal here journals nothing and creates nothing.
  2. intent     {kind: desktop_create | desktop_copy, existing (uids), previousCurrent, target}
                is appended and flushed before the write is sent
  3. write      `_phase: "write"`, addressed by the stable target, with `_expected` = the uids read
                in 1: Flame compares the list again right before creating and creates nothing when
                it changed (CONFLICT)
  4. outcome    {ok, errorCode, created} -- `created` is what `journal.revert` deletes

`journal.revert(group)` (`desktop_revert.revert_desktop_group`) deletes what `created` names with a
Flame-side compare-and-swap: a create only while what it made is still as it was left (a Batch
group: no node and every reel / shelf reel still empty and named as created; a reel group: every
reel empty; a reel: empty), else CONFLICT and nothing is deleted. A copy's revert deletes the
copies by uid, only while every one still has the name, duration and fingerprint it had right
after the copy (else CONFLICT and nothing is deleted); the ones that are gone already are
`skipped`. The revert is journaled as a group of
its own (`kind: desktop_revert`, `reverts` = the original group).

Two more things ride on the same engine: `library.create` is `desktop.create` with
createKind 'library' (`run_library_create`; its revert deletes the library only while it is empty),
and `desktop.copy` of a Batch group (a Desktop group into a library / a library folder, a library
group onto the Desktop; `desktop_batch_prelude`): the copy's record is `{uid, name, type:
'PyBatch', state}`, and its revert deletes the group only while its `state` (name, reels' clip
names, on the Desktop also the node fingerprint) is unchanged.

A write that ends without an answer (TIMEOUT / BRIDGE_LOST / BRIDGE_UNAVAILABLE) leaves the intent
open and shuts the write door (`WriteGuard`) until `journal.check` has decided
(`desktop_revert.check_desktop_intent`: it reads the parent / destination again -- new entries are
what a create / copy made).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.checkpointed import note_unresolved
from flame_mcp.journal import (
    KIND_DESKTOP_COPY,
    KIND_DESKTOP_CREATE,
    new_id,
    now_iso,
)
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.library_open import merge_into_envelope
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import ErrorCode

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

CREATE_OP: Final[str] = "desktop.create"
LIBRARY_CREATE_OP: Final[str] = "library.create"
COPY_OP: Final[str] = "desktop.copy"
CREATE_OPS: Final[frozenset[str]] = frozenset({CREATE_OP, LIBRARY_CREATE_OP})
"""The ops that make one new thing and share `desktop.create`'s journal / revert / check
(`library.create` is `desktop.create` with createKind 'library')."""
PHASE_ARG: Final[str] = "_phase"
EXPECTED_ARG: Final[str] = "_expected"
CREATED_ARG: Final[str] = "_created"
PHASE_READ: Final[str] = "read"
PHASE_WRITE: Final[str] = "write"
PHASE_REVERT: Final[str] = "revert"
PHASE_REVERT_PLAN: Final[str] = "revert_plan"
PHASE_ALIVE: Final[str] = "alive"
PHASE_PROBE: Final[str] = "probe"
RESERVED_ARGS: Final[frozenset[str]] = frozenset({PHASE_ARG, EXPECTED_ARG, CREATED_ARG})
"""Keys only this module puts into an op's args (never declared parameters, so a caller cannot send
them: `validate_op_args` rejects an unknown key). `to_python` of both ops reads them."""
WRITE_TIMEOUT_S: Final[float] = 30.0
MAX_SOURCES: Final[int] = 50

CURRENT_MOVED_NOTE: Final[str] = (
    "current moved to the new Batch group: Flame does this on every create_batch_group (measured "
    "on Flame 2025.2.4). No API is known to put the current group back; "
    "previousCurrent names the group that was current before."
)
CURRENT_NOT_RESTORED_NOTE: Final[str] = (
    "Deleting the current Batch group does NOT make the group that was current before the create "
    "current again: Flame picks another existing group (measured on Flame 2025.2.4). "
    "`current` is the group that is current now; `previousCurrent` the one that was before the "
    "create."
)
RESTORE_NOTE: Final[str] = (
    "A new Batch group was added to the Desktop (a new uid). The current Batch group did not "
    "change: result.current says which group is current."
)
LIBRARY_BATCH_NOTE: Final[str] = (
    "A Batch group inside a library lists its reels, shelf reels and their clips but reads no "
    "nodes (nodes is empty). Copy it onto the Desktop with desktop.copy "
    "(destination {kind: 'desktop'}) to get a group with its nodes and connections."
)
CURRENT_DELETED_NOTE: Final[str] = (
    "The deleted group was the current Batch group, so Flame made another existing group "
    "current: `current` is the group that is current now, `previousCurrent` the deleted one."
)
BATCH_COPY_HINT: Final[str] = (
    "Nothing was copied and nothing journaled. A Batch group is copied into a library or a "
    "library folder (a reel would take its clips instead), and a Batch group inside a library "
    "is copied onto the Desktop (destination {kind: 'desktop'})."
)
LAST_REEL_NOTE: Final[str] = (
    "The reel group held only this reel, so Flame created its default reels again after the "
    "delete (Reel 1 to 4 and Sequences, measured on Flame 2025.2.4); "
    "parentReelsAfter lists them."
)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _refusal(
    op: RegisteredOperation,
    code: ErrorCode,
    message: str,
    started: float,
    *,
    result: Any = None,
    hint: str | None = None,
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=False,
        result=result,
        error=message,
        error_code=code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=_elapsed_ms(started),
        hint_override=hint,
    )


def create_op_name(kind: Any) -> str:
    """The op whose Flame code makes (and reverts, and checks) a thing of this createKind."""
    return LIBRARY_CREATE_OP if kind == "library" else CREATE_OP


def rows_uids(rows: Any) -> list[Any]:
    return (
        [row.get("uid") for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    )


def created_record(op_name: str, result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What a `desktop.create` / `desktop.copy` result says was made, in the form the journal
    keeps and `journal.revert` deletes by. None when nothing is known to have been made."""
    if not isinstance(result, Mapping):
        return None
    if op_name in CREATE_OPS:
        made = result.get("created")
        if not isinstance(made, dict) or not made.get("uid"):
            return None
        return {
            "kind": result.get("kind"),
            "uid": made.get("uid"),
            "name": made.get("name"),
            "type": made.get("type"),
            "wiretapId": made.get("wiretapId"),
            "selector": made.get("selector"),
            "parent": result.get("parent"),
            "state": result.get("state"),
            "previousCurrent": result.get("previousCurrent"),
            "requestedName": result.get("requestedName"),
        }
    copies = [
        {
            "uid": c.get("uid"),
            "name": c.get("name"),
            "type": c.get("type"),
            "wiretapId": c.get("wiretapId"),
            "sourceUid": c.get("sourceUid"),
            "state": c.get("state"),
        }
        for c in result.get("copies") or []
        if isinstance(c, dict) and c.get("uid")
    ]
    if not copies:
        return None
    return {"destination": result.get("destination"), "copies": copies}


BATCH_REEL_HINT: Final[str] = (
    "A reel of a Batch group takes clips through batch.add_clips (it checkpoints the group and "
    "checks the Clip nodes); desktop.copy is for Desktop and library destinations. Nothing was "
    "copied and nothing journaled."
)


def _fail_pre_read(
    op: RegisteredOperation, pre: dict[str, Any], hint: str | None = None
) -> dict[str, Any]:
    failure = dict(pre)
    failure["execution"] = envelope_mod.execution_meta(op)
    failure["error"] = f"nothing was written: {pre.get('error')}"
    if hint is not None:
        failure["hint"] = hint
    return failure


def _stable_write_args(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """(write args, the target's stable selector or None, a reason nothing can be written).
    The parent / destination (and each copy source) is re-addressed by wiretap id so the write
    acts on what the pre-read saw even if a `selection` / `path` moved meanwhile."""
    args = dict(op_args)
    if op.name in CREATE_OPS:
        if op_args["kind"] != "reel":
            return args, None, None
        stable = (pre.get("parent") or {}).get("selector")
        if not isinstance(stable, dict):
            return args, None, "the parent has no wiretap id, so it cannot be found again"
        args["parent"] = stable
        return args, stable, None
    stable = (pre.get("destination") or {}).get("selector")
    if not isinstance(stable, dict):
        return args, None, "the destination has no wiretap id, so it cannot be found again"
    args["destination"] = stable
    rows = pre.get("sources") or []
    args["sources"] = [
        row.get("selector") if isinstance(row.get("selector"), dict) else original
        for original, row in zip(op_args["sources"], rows, strict=True)
    ]
    return args, stable, None


def _intent_record(
    op: RegisteredOperation,
    op_args: dict[str, Any],
    pre: dict[str, Any],
    stable: dict[str, Any] | None,
    *,
    intent_id: str,
    group_id: str,
) -> dict[str, Any]:
    is_create = op.name in CREATE_OPS
    record: dict[str, Any] = {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op.name,
        "kind": KIND_DESKTOP_CREATE if is_create else KIND_DESKTOP_COPY,
        "args": op_args,
        "target": stable,
        "existing": rows_uids(pre.get("existing")),
    }
    if is_create:
        record["createKind"] = op_args["kind"]
        record["previousCurrent"] = pre.get("current")
    else:
        record["sources"] = [
            {"name": s.get("name"), "uid": s.get("uid"), "type": s.get("type")}
            for s in pre.get("sources") or []
        ]
    return record


def _journal_ref(group_id: str, intent_id: str, created: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "group": group_id,
        "id": intent_id,
        "revert": (
            {"op": "journal.revert", "args": {"group": group_id}} if created is not None else None
        ),
    }


def _is_batch_copy(result: Mapping[str, Any]) -> bool:
    return any(isinstance(c, dict) and c.get("type") == "PyBatch" for c in result["copies"])


def _add_notes(op: RegisteredOperation, op_args: Mapping[str, Any], env: dict[str, Any]) -> None:
    result = env.get("result")
    if not isinstance(result, dict):
        return
    notes: list[str] = []
    if op.name in CREATE_OPS and op_args["kind"] == "batch_group" and result.get("created"):
        notes.append(CURRENT_MOVED_NOTE)
    if op.name == COPY_OP and result.get("copies") and _is_batch_copy(result):
        into = (result.get("destination") or {}).get("type")
        notes.append(RESTORE_NOTE if into == "PyDesktop" else LIBRARY_BATCH_NOTE)
    if notes:
        result["notes"] = [*(result.get("notes") or []), *notes]
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warnings:
        env["degraded"] = [*env.get("degraded", []), *[f"read-back differs: {w}" for w in warnings]]


def _refuse_taken_name(
    op: RegisteredOperation, op_args: dict[str, Any], pre: dict[str, Any], started: float
) -> dict[str, Any] | None:
    if op.name not in CREATE_OPS:
        return None
    names = [row.get("name") for row in pre.get("existing") or []]
    if op_args["name"] not in names or op_args.get("allow_rename", False):
        return None
    return _refusal(
        op,
        "PRECONDITION_UNMET",
        f"nothing was created: a {op_args['kind']} named {op_args['name']!r} already exists",
        started,
        result={"ran": False, "existingNames": names},
        hint=(
            "Pick another name, or pass allow_rename=true to let Flame rename the new one (it "
            "appends a number). Nothing was written and nothing journaled."
        ),
    )


def _pre_read_hint(op: RegisteredOperation, pre: Mapping[str, Any]) -> str | None:
    if op.name == COPY_OP and pre.get("errorCode") == "INVALID_ARGS":
        if "Batch group copy:" in str(pre.get("error")):
            return BATCH_COPY_HINT
        return BATCH_REEL_HINT
    return None


def _no_read_back(env: Mapping[str, Any]) -> dict[str, Any] | None:
    return None


@dataclass(frozen=True)
class WriteProfile:
    """What differs between the desktop writes that share `run_write_flow` (the lock, the
    pre-read, the intent, the compare-and-swap write, the outcome, the fail-closed handling of an
    answer that never came). `desktop.create` / `desktop.copy` are `DEFAULT_PROFILE`;
    `desktop_media_run` brings one for import / duplicate / move."""

    refuse: Callable[
        [RegisteredOperation, dict[str, Any], dict[str, Any], float], dict[str, Any] | None
    ]
    """After a good pre-read: an envelope that ends the call (nothing written), or None."""
    stable_args: Callable[
        [RegisteredOperation, dict[str, Any], dict[str, Any]],
        tuple[dict[str, Any], dict[str, Any] | None, str | None],
    ]
    intent: Callable[..., dict[str, Any]]
    created: Callable[[str, Mapping[str, Any] | None], dict[str, Any] | None]
    notes: Callable[[RegisteredOperation, Mapping[str, Any], dict[str, Any]], None]
    pre_hint: Callable[[RegisteredOperation, Mapping[str, Any]], str | None]
    read_back: Callable[[Mapping[str, Any]], dict[str, Any] | None] = _no_read_back
    """What goes into the outcome's `readBack` (a count that explains a SILENT_NOOP)."""
    prepare: Callable[..., dict[str, Any] | None] | None = None
    """`(rt, op, op_args, pre, group_id) -> envelope | None`, after the refusals and before the
    intent: work that has to be done first and may leave notes in `pre` for `stable_args` /
    `intent` (`desktop.delete` copies what it will delete into the stash). An envelope ends the
    call (nothing was written to the project)."""


DEFAULT_PROFILE: Final[WriteProfile] = WriteProfile(
    refuse=_refuse_taken_name,
    stable_args=_stable_write_args,
    intent=_intent_record,
    created=created_record,
    notes=_add_notes,
    pre_hint=_pre_read_hint,
)


def run_write_flow(
    rt: Runtime,
    op: RegisteredOperation,
    op_args: dict[str, Any],
    profile: WriteProfile,
    *,
    group: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    with rt.write_lock:
        denial = rt.guard.denial()
        if denial is not None:  # this call queued behind a write that ended unknown
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=_elapsed_ms(started),
                hint=denial.hint,
            )

        pre_env = execution.run_op(op, {**op_args, PHASE_ARG: PHASE_READ}, run_dir=rt.run_dir)
        if pre_env.get("ok") is not True or not isinstance(pre_env.get("result"), dict):
            return _fail_pre_read(op, pre_env, profile.pre_hint(op, pre_env))
        pre: dict[str, Any] = pre_env["result"]

        refused = profile.refuse(op, op_args, pre, started)
        if refused is not None:
            merge_into_envelope(refused, pre)
            return refused
        group_id = group if group is not None else new_id("g")
        if profile.prepare is not None:
            prepared = profile.prepare(rt, op, op_args, pre, group_id)
            if prepared is not None:
                merge_into_envelope(prepared, pre)
                return prepared
        write_args, stable, problem = profile.stable_args(op, op_args, pre)
        if problem is not None:
            unmatched = _refusal(
                op,
                "PRECONDITION_UNMET",
                f"nothing was written: {problem}",
                started,
                result={"ran": False},
            )
            merge_into_envelope(unmatched, pre)
            return unmatched

        intent_id = new_id("i")
        try:
            rt.journal.append(
                profile.intent(op, op_args, pre, stable, intent_id=intent_id, group_id=group_id)
            )
        except OSError as exc:
            unwritable = _refusal(
                op,
                "POLICY_DENIED",
                f"nothing was written: the journal cannot be written ({exc}), and this server "
                "does not write to the project without an intent on record",
                started,
                result={"ran": False},
                hint=f"Make {rt.journal.directory} writable, then retry.",
            )
            merge_into_envelope(unwritable, pre)
            return unwritable

        write_args[PHASE_ARG] = PHASE_WRITE
        write_args[EXPECTED_ARG] = rows_uids(pre.get("existing"))
        env = execution.run_op(op, write_args, run_dir=rt.run_dir, timeout_s=WRITE_TIMEOUT_S)
        end_code = env.get("errorCode")
        if end_code in UNKNOWN_END_CODES:
            rt.guard.block(intent_id, group_id, op.name, str(end_code))
            note_unresolved(rt, intent_id, group_id, env)
            hint = str(env.get("hint") or "")
            env["hint"] = (
                f"{hint} It is not known whether the call landed: this server now refuses every "
                f"write until journal.check (group={group_id!r}) has read Flame again."
            ).strip()
            env["journal"] = _journal_ref(group_id, intent_id, None)
            merge_into_envelope(env, pre)
            return env

        ok = env.get("ok") is True
        created = profile.created(op.name, env.get("result"))
        outcome: dict[str, Any] = {
            "type": "outcome",
            "id": intent_id,
            "group": group_id,
            "ts": now_iso(),
            "ok": ok,
            "applied": [],
            "failed": {} if ok else {op.name: str(env.get("error"))},
            "errorCode": None if ok else end_code,
            "readBack": profile.read_back(env),
            "created": created,
        }
        try:
            rt.journal.append(outcome)
        except OSError as exc:
            rt.guard.block(intent_id, group_id, op.name, "JOURNAL_OUTCOME_NOT_WRITTEN")
            env["degraded"] = [
                *env.get("degraded", []),
                f"the outcome could not be journaled ({exc}); the answer stands but this server "
                "now refuses writes until journal.check has settled the intent",
            ]
        env["journal"] = _journal_ref(group_id, intent_id, created)
        merge_into_envelope(env, pre)
        profile.notes(op, op_args, env)
        return env


def run_desktop_write(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `desktop.create` and `desktop.copy`."""
    return run_write_flow(rt, op, op_args, DEFAULT_PROFILE, group=group)


def run_library_create(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `library.create`: `desktop.create`'s write flow for a thing of createKind
    'library'."""
    return run_write_flow(rt, op, {**op_args, "kind": "library"}, DEFAULT_PROFILE, group=group)
