"""Writes that run behind an automatic checkpoint. The one mechanism behind `eval.run`
(destructive), `batch.load_setup`, `journal.restore_checkpoint` and every Batch write op that
declares a `CheckpointSpec`:

    checkpoint the target group  ->  journal an intent (fingerprint before, the checkpoint id,
    what is about to run)  ->  run  ->  read the group's fingerprint back  ->  journal the
    outcome (fingerprint after)

`journal.revert(group)` then restores the checkpoint, but only while the group's fingerprint is
still the one the run left (`force` overrides, and is journaled). The checkpoint covers the
target group's nodes and wiring (and Action / Image / GMask Tracer inner nodes) -- see
`checkpoint_prelude` for what the fingerprint sees. What a restore does not bring back is what
the ops that use this mechanism say in their own descriptions.

The same flow serves a sequence / clip on the Desktop (`CheckpointKind` TIMELINE_KIND):
the checkpoint is a copy in a stash reel (`timeline.checkpoint`), the fingerprint is
`timeline.fingerprint`'s, and the target is addressed by its wiretap id. Nothing below changes for a
Batch group.

Layers:
  `run_checkpointed`     the engine. Holds no wording: it raises `PreRunRefusal` when nothing was
                         run yet and returns a `CheckpointedRun` (the facts) otherwise. The caller
                         holds `ctx.write_lock`.
  `run_declared`         the `wrap` of an op that declares `checkpoint=CheckpointSpec(...)`:
                         the op's own result, plus `journal` = {group, id, checkpoint, revert, ...}.
  `refusal_envelope` / `run_envelope`   the default translation of a refusal / of the facts into
                         an envelope; `eval.run` uses them with its own wording (`Words`).
  `restore.replace_group` keeps its own result shape (`matched`, `preCheckpoint`, `undo`): the
                         results of `batch.load_setup` and `journal.restore_checkpoint` are
                         documented that way.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.checkpoints import (
    BATCH_KIND,
    TIMELINE_KIND,
    CheckpointKind,
    fingerprint_of,
    intent_fields,
    kind_named,
    outcome_fields,
    read_fingerprint,
    stable_subject,
    take_checkpoint,
)
from flame_mcp.errors import as_error_code
from flame_mcp.journal import new_id, now_iso
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.opschema import ArgIssue, find_non_ascii
from flame_mcp.policy import deny_operation
from flame_mcp.registry import RegisteredOperation
from flame_mcp.types import CheckpointSpec, ErrorCode, ServerOpError

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime, ServerContext

RESTORE_SIDE_EFFECTS = (
    "Restore side effects: a restore (load_setup of a checkpoint) brings the group's contents "
    "back -- nodes, wiring, settings, Clip nodes and their media. load_setup itself drops "
    "empty schematic reels and adds Flame's default empty reels; a restore "
    "of a checkpoint that recorded the reels corrects that right after the load: the recorded "
    "empty reels are created again, then the empty reels the record lacks are deleted (a reel "
    "that holds a clip or a sequence is never deleted), and result.reelsRestored says whether "
    "the schematic reels' layout (names, clip and sequence names) now equals the recorded one: "
    "reelsRestored looks at the schematic reels only (result.reelTidy: what was created or "
    "deleted, errors as raised, the differences, and orderMatches for the order of the reels; "
    "differences in shelf reels and iterations are reported apart, in "
    "reelTidy.outsideTheCheckpoint). Not put back: reels that hold "
    "clips are rebuilt with new uids (a selector that holds an old clip / reel uid no longer "
    "works: address by name, or list again after a restore), and shelf reels and iterations "
    "are not restored at all. "
    "A checkpoint that holds no reel record is not tidied "
    "(reelTidy.skipped). Force: a restore with force=true (journal.revert of a group whose "
    "nodes changed since) also loses everything added to the group after the checkpoint -- "
    "nodes, reels and the clips in those reels (load_setup does not create a reel the setup "
    "lacks)."
)
"""The side effects of every restore, worded once for the descriptions of everything that
restores a checkpoint (journal.revert, journal.restore_checkpoint, batch.load_setup, a
destructive eval.run, and each op that declares a `CheckpointSpec`)."""

NEEDED_OPS: tuple[str, ...] = BATCH_KIND.needed_ops
"""The ops the engine itself calls for a Batch group (through `ctx.do`, so under the same policy);
`CheckpointKind.needed_ops` for the other kind."""

Stage = Literal["checkpoint", "uid", "intent", "ascii"]


@dataclass(frozen=True)
class Judgement:
    """What the caller makes of the run's answer and the group's fingerprint afterwards: the
    outcome record's `ok`, the text stored under `failed[failed_key]`, and its `errorCode`."""

    ok: bool
    failed: str | None = None
    error_code: str | None = None


class PreRunRefusal(Exception):
    """Nothing was run: the checkpoint could not be taken (`cp_env` is the failed envelope), the
    group has no uid (`cp` is the checkpoint's result), a value to be sent to Flame is not ASCII
    (`issue`), or the intent could not be journaled (`cause`, and `cp`). The caller turns it into
    the error of its own op."""

    def __init__(
        self,
        stage: Stage,
        *,
        cp_env: Mapping[str, Any] | None = None,
        cp: Mapping[str, Any] | None = None,
        cause: OSError | None = None,
        issue: ArgIssue | None = None,
        kind: CheckpointKind = BATCH_KIND,
    ) -> None:
        super().__init__(stage)
        self.stage: Stage = stage
        self.cp_env = cp_env
        self.cp = cp
        self.cause = cause
        self.issue = issue
        self.kind = kind


@dataclass
class CheckpointedRun:
    """What happened. `env` is Flame's envelope of the run itself. With `unknown_end` the run
    ended without an answer: the write guard is on, an `unresolved` note is journaled, there is
    no outcome and no fingerprint after."""

    group: str
    intent_id: str
    target: dict[str, Any]
    pre: dict[str, Any]
    env: dict[str, Any]
    fingerprint_before: str | None
    fingerprint_after: str | None = None
    unknown_end: bool = False
    ok: bool = False
    changed: bool | None = None
    journal_error: str | None = None
    after_error: Any = None
    extra: dict[str, Any] = field(default_factory=dict)
    kind: CheckpointKind = BATCH_KIND

    @property
    def pre_id(self) -> str:
        return str(self.pre.get("checkpoint"))

    @property
    def pre_path(self) -> Any:
        """Where the checkpoint is kept: the setup file of a Batch group, the stash reel
        (`<library>/<reel>`) of a sequence / clip."""
        if self.kind is TIMELINE_KIND:
            stash = self.pre.get("stash")
            if isinstance(stash, dict):
                return f"{stash.get('library')}/{stash.get('reel')}"
            return None
        return self.pre.get("path")

    def pre_summary(self) -> dict[str, Any]:
        if self.kind is TIMELINE_KIND:
            return {
                "id": self.pre.get("checkpoint"),
                "stash": self.pre.get("stash"),
                "fingerprint": self.pre.get("fingerprint"),
                "segmentCount": self.pre.get("segmentCount"),
            }
        return {
            "id": self.pre.get("checkpoint"),
            "path": self.pre.get("path"),
            "fingerprint": self.pre.get("fingerprint"),
            "nodeCount": self.pre.get("nodeCount"),
        }


def refuse_if_denied(ctx: ServerContext, names: tuple[str, ...]) -> None:
    """Raise `ServerOpError` (POLICY_DENIED / UNKNOWN_OPERATION) when an op the engine (or its
    caller) is about to call through `ctx.do` is denied: better to know before anything is
    written than half way."""
    for name in names:
        op = ctx.registry.get(name)
        if op is None:
            raise ServerOpError("UNKNOWN_OPERATION", f"'{name}' is not registered")
        denial = deny_operation(op, ctx.policy)
        if denial is not None:
            raise ServerOpError(
                "POLICY_DENIED",
                f"nothing was run: this needs '{name}' and it is denied ({denial.message})",
                hint=denial.hint,
            )


ASCII_REFUSAL_HINT: Final[str] = (
    "A value this server would send to Flame holds a non-ASCII character, so nothing was run "
    "and no intent was written. Flame takes ASCII only."
)


def ascii_refusal_text(exc: PreRunRefusal) -> str:
    issue = exc.issue
    assert issue is not None
    return f"{issue.path}: {issue.message}"


def _first_non_ascii(**values: Any) -> ArgIssue | None:
    for name, value in values.items():
        issue = find_non_ascii(value, name)
        if issue is not None:
            return issue
    return None


def run_checkpointed(
    ctx: ServerContext,
    *,
    selector: Mapping[str, Any],
    op_name: str,
    kind: str,
    args: Mapping[str, Any],
    intent_extra: Mapping[str, Any],
    expected_fp: str | None,
    label: str,
    run: Callable[[dict[str, Any]], dict[str, Any]],
    judge: Callable[[dict[str, Any], str | None], Judgement],
    failed_key: str,
    outcome_extra: Callable[[dict[str, Any], str | None], dict[str, Any]] | None = None,
    group: str | None = None,
    reverts: str | None = None,
    reverts_intent: str | None = None,
    forced: Mapping[str, Any] | None = None,
    checkpoint_kind: CheckpointKind = BATCH_KIND,
) -> CheckpointedRun:
    """The engine. `selector` addresses the Batch group (or the sequence / clip, for the timeline
    kind); `run(stable)` runs the op against the target addressed by uid (wiretap id) and returns
    Flame's envelope; `judge(env, fingerprint_after)` decides whether the run counts as done. The
    caller must hold `ctx.write_lock`."""
    group_id = group if group is not None else new_id("g")
    issue = _first_non_ascii(args=args, intent=intent_extra)
    if issue is not None:
        raise PreRunRefusal("ascii", issue=issue, kind=checkpoint_kind)
    cp_env = take_checkpoint(ctx, selector, label=label, group=group_id, kind=checkpoint_kind)
    if cp_env.get("ok") is not True:
        raise PreRunRefusal("checkpoint", cp_env=cp_env, kind=checkpoint_kind)
    cp_raw = cp_env.get("result")
    pre: dict[str, Any] = dict(cp_raw) if isinstance(cp_raw, dict) else {}
    subject = stable_subject(checkpoint_kind, pre)
    if subject is None:
        raise PreRunRefusal("uid", cp=pre, kind=checkpoint_kind)
    stable, uid = subject
    issue = _first_non_ascii(target=stable)
    if issue is not None:
        raise PreRunRefusal("ascii", cp=pre, issue=issue, kind=checkpoint_kind)
    pre_id = str(pre.get("checkpoint"))

    intent_id = new_id("i")
    intent: dict[str, Any] = {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op_name,
        "kind": kind,
        "args": dict(args),
        "target": dict(stable),
        "checkpoint": pre_id,
        "preCheckpoint": pre_id,
        "revertCheckpoint": pre_id,
        "fingerprintBefore": pre.get("fingerprint"),
        "fingerprintExpected": expected_fp,
        **intent_fields(checkpoint_kind, pre, uid),
        **intent_extra,
    }
    if reverts is not None:
        intent["reverts"] = reverts
    if reverts_intent is not None:
        intent["revertsIntent"] = reverts_intent
    if forced is not None:
        intent["forced"] = dict(forced)
    try:
        ctx.journal.append(intent)
    except OSError as exc:
        raise PreRunRefusal("intent", cp=pre, cause=exc, kind=checkpoint_kind) from exc

    env = run(dict(stable))
    facts = CheckpointedRun(
        group=group_id,
        intent_id=intent_id,
        target=dict(stable),
        pre=pre,
        env=env,
        fingerprint_before=pre.get("fingerprint"),
        kind=checkpoint_kind,
    )
    end_code = env.get("errorCode")
    if end_code in UNKNOWN_END_CODES:
        ctx.guard.block(intent_id, group_id, op_name, str(end_code))
        note_unresolved(ctx, intent_id, group_id, env)
        facts.unknown_end = True
        return facts

    after_env = read_fingerprint(ctx, stable, checkpoint_kind)
    facts.fingerprint_after = fingerprint_of(after_env)
    facts.after_error = None if facts.fingerprint_after is not None else after_env.get("error")
    facts.changed = (
        facts.fingerprint_after != facts.fingerprint_before
        if facts.fingerprint_after is not None
        else None
    )
    judgement = judge(env, facts.fingerprint_after)
    facts.ok = judgement.ok
    outcome: dict[str, Any] = {
        "type": "outcome",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "ok": judgement.ok,
        "applied": [],
        "failed": {} if judgement.ok else {failed_key: judgement.failed or ""},
        "errorCode": None if judgement.ok else judgement.error_code,
        "readBack": None,
        "fingerprintAfter": facts.fingerprint_after,
        "fingerprintAfterError": facts.after_error,
        "changed": facts.changed,
        **outcome_fields(checkpoint_kind, after_env),
    }
    if outcome_extra is not None:
        outcome.update(outcome_extra(env, facts.fingerprint_after))
    try:
        ctx.journal.append(outcome)
    except OSError as exc:
        facts.journal_error = str(exc)
        ctx.guard.block(intent_id, group_id, op_name, "JOURNAL_OUTCOME_NOT_WRITTEN")
    return facts


def note_unresolved(ctx: ServerContext, intent_id: str, group: str, env: Mapping[str, Any]) -> None:
    try:
        ctx.journal.append(
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


@dataclass(frozen=True)
class Words:
    """The few phrases that name what was being run in the default messages."""

    what: str = "this call"
    ran: str = "the operation"
    refuses: str = "a checkpointed write"


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _code_of(env: Mapping[str, Any]) -> ErrorCode:
    return as_error_code(str(env.get("errorCode")))


def refusal_envelope(
    op: RegisteredOperation,
    exc: PreRunRefusal,
    words: Words,
    meta: Mapping[str, Any],
    started: float,
    *,
    journal_dir: Any,
) -> dict[str, Any]:
    """The envelope of a call that stopped before anything was run."""
    code: ErrorCode
    result: dict[str, Any]
    hint: str | None = None
    if exc.stage == "checkpoint":
        cp_env = exc.cp_env or {}
        code = _code_of(cp_env)
        message = (
            f"nothing was run: the checkpoint that makes {words.what} revertible could not be "
            f"taken ({cp_env.get('error')})"
        )
        result = {"ran": False, "checkpointError": cp_env.get("error")}
        hint = cp_env.get("hint")
    elif exc.stage == "ascii":
        code = "INVALID_ARGS"
        message = f"nothing was run: {ascii_refusal_text(exc)}"
        result = {"ran": False, "checkpoint": (exc.cp or {}).get("checkpoint")}
        hint = ASCII_REFUSAL_HINT
    elif exc.stage == "uid":
        code = "PRECONDITION_UNMET"
        has = "wiretap id" if exc.kind is TIMELINE_KIND else "uid"
        message = (
            f"nothing was run: the target {exc.kind.subject} has no {has}, so its state cannot "
            "be found again for the outcome or the revert"
        )
        result = {"ran": False, "checkpoint": (exc.cp or {}).get("checkpoint")}
    else:
        code = "POLICY_DENIED"
        message = (
            f"nothing was run: the journal cannot be written ({exc.cause}), and this server "
            f"does not run {words.refuses} without an intent on record"
        )
        result = {"ran": False, "checkpoint": (exc.cp or {}).get("checkpoint")}
        hint = f"Make {journal_dir} writable, then retry."
    return envelope_mod.build_envelope(
        ok=False,
        result=result,
        error=message,
        error_code=code,
        execution=meta,
        duration_ms=_elapsed_ms(started),
        hint_override=hint,
    )


def run_envelope(
    facts: CheckpointedRun,
    words: Words,
    meta: Mapping[str, Any],
    *,
    failure_hint: bool,
) -> dict[str, Any]:
    """Flame's envelope of the run, with `journal` = {group, id, checkpoint, checkpointPath,
    revert, changed}. `changed` is what the fingerprint saw (nodes, wiring, inner nodes of Action
    / Image / GMask Tracer nodes): a change it cannot see reads as false."""
    env = facts.env
    env["execution"] = dict(meta)
    ref: dict[str, Any] = {
        "group": facts.group,
        "id": facts.intent_id,
        "checkpoint": facts.pre_id,
        "checkpointPath": facts.pre_path,
        "revert": {"op": "journal.revert", "args": {"group": facts.group}},
    }
    if facts.kind is TIMELINE_KIND:
        ref["stash"] = facts.pre.get("stash")
        ref["display"] = facts.pre.get("display")
    if facts.unknown_end:
        hint = str(env.get("hint") or "")
        env["hint"] = (
            f"{hint} It is not known whether {words.ran} ran: this server now refuses every "
            f"write until journal.check (group={facts.group!r}) has read Flame again. The state "
            f"before it is saved in checkpoint {facts.pre_id} ({facts.pre_path})."
        ).strip()
        env["journal"] = ref
        return env
    ref["changed"] = facts.changed
    if facts.journal_error is not None:
        env["degraded"] = [
            *env.get("degraded", []),
            f"the outcome could not be journaled ({facts.journal_error}); the run's answer "
            "stands but this server now refuses writes until journal.check has settled the intent",
        ]
    if failure_hint and env.get("ok") is not True:
        hint = str(env.get("hint") or "")
        env["hint"] = (
            f"{hint} The {facts.kind.subject}'s state before this call is saved in checkpoint "
            f"{facts.pre_id} ({facts.pre_path}): journal.revert(group={facts.group!r}) puts it "
            f"back while the {facts.kind.subject} still has the fingerprint the failed run left "
            "(force=true otherwise)."
        ).strip()
    env["journal"] = ref
    return env


def _judge_declared(env: dict[str, Any], fingerprint_after: str | None) -> Judgement:
    ok = env.get("ok") is True
    return Judgement(
        ok=ok,
        failed=None if ok else str(env.get("error")),
        error_code=None if ok else str(env.get("errorCode")),
    )


def declared_run_args(
    spec: CheckpointSpec, op_args: Mapping[str, Any], stable: dict[str, Any]
) -> dict[str, Any]:
    """The op's arguments re-pointed at the checkpointed thing addressed by `stable`."""
    if spec.rebind is not None:
        return spec.rebind(op_args, stable)
    return {**op_args, spec.batch_arg: stable}


def run_declared(
    rt: Runtime,
    op: RegisteredOperation,
    op_args: dict[str, Any],
    *,
    group: str | None = None,
) -> dict[str, Any]:
    """`Runtime.execute` runs an op that declares a `CheckpointSpec` through here."""
    spec = op.checkpoint
    assert spec is not None  # Runtime only calls this for such an op
    started = time.monotonic()
    meta = envelope_mod.execution_meta(op)
    words = Words(what=f"this {op.name} call")
    timeout_s = spec.timeout_s
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
        target_kind = kind_named(spec.target_kind)
        try:
            refuse_if_denied(rt, target_kind.needed_ops)
        except ServerOpError as exc:
            return envelope_mod.build_envelope(
                ok=False,
                error=exc.message,
                error_code=exc.code,
                execution=meta,
                duration_ms=_elapsed_ms(started),
                hint_override=exc.hint,
            )
        if spec.check is not None:
            refusal = spec.check(rt, op_args)
            if refusal is not None:
                return envelope_mod.build_envelope(
                    ok=False,
                    result=refusal.result,
                    error=refusal.message,
                    error_code=refusal.code,
                    execution=meta,
                    duration_ms=_elapsed_ms(started),
                    hint_override=refusal.hint,
                )

        def run(stable: dict[str, Any]) -> dict[str, Any]:
            run_args = declared_run_args(spec, op_args, stable)
            return execution.run_op(op, run_args, run_dir=rt.run_dir, timeout_s=timeout_s)

        group_selector = spec.group_selector(op_args) if spec.group_selector is not None else None
        try:
            facts = run_checkpointed(
                rt,
                selector=group_selector if group_selector is not None else op_args[spec.batch_arg],
                op_name=op.name,
                kind=op.name.split(".", 1)[-1],
                args=op_args,
                intent_extra={},
                expected_fp=None,
                label=spec.label(op_args) if spec.label is not None else f"before {op.name}",
                run=run,
                judge=_judge_declared,
                failed_key=op.name,
                group=group,
                checkpoint_kind=target_kind,
            )
        except PreRunRefusal as exc:
            return refusal_envelope(op, exc, words, meta, started, journal_dir=rt.journal.directory)
    return run_envelope(facts, words, meta, failure_hint=True)
