"""`render.batch`, server side. `render.plan`, which says what a
`render.batch` call would do and is where its refusals come from, is in `render_plan.py`.

`render.batch` is an op whose effect cannot be brought back by a checkpoint: it writes
files to disk and puts clips into a shelf reel, both outside a Batch setup. So it does not use
`checkpointed.py`; it is a plain write-ahead pair (intent -> the render -> outcome) with these
extra rules:

  - everything that can be refused is refused before the render, from what Flame and the disk
    say (`preflight_problems`), and nothing is journaled for a refusal;
  - the intent lists what was on disk before (`journal.check` needs it to tell a file the render
    wrote from one that was already there);
  - after the run the frames are counted against the plan (`SILENT_NOOP` / `FLAME_API_ERROR` when
    short);
  - a run that ends without an answer (TIMEOUT / BRIDGE_LOST / a bridge `pending` handle) leaves
    the intent open and closes the write door (`WriteGuard`) until `journal.check` has decided
    (`render_check.check_render_intent`). `journal.check` asks the bridge for the run's result with
    `transport.poll` -- that, not the files, says the render has ended (in one run the frames were
    complete about 8 s before render() returned) -- and then counts the files. The bridge gives a
    result over once, so it is journaled at once (`polled`).
  - the handle to ask with is chosen by this server: when the bridge announces
    `client_handle` the intent carries a fresh `handle` (`handleSource: "client"`) before anything
    runs, and the bridge files the run's result under it even if its answer never arrives (observed:
    BRIDGE_LOST after 8 s with no pending handle). A bridge that does not announce it gets no
    handle (`handleSource: null`); its `pending` handle, when it answers with one, is journaled
    (`pending`) and takes precedence. A run that was answered in-band leaves its result file too:
    one poll removes it.

Everything about the filesystem is in `render_files`; the Flame side is `render_prelude`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import errors, execution, transport
from flame_mcp.checkpointed import note_unresolved, refuse_if_denied
from flame_mcp.journal import (
    STATE_LANDED,
    STATE_NOT_LANDED,
    STATE_PARTIAL,
    new_id,
    now_iso,
)
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.registry import RegisteredOperation
from flame_mcp.render_files import (
    MISSING_CAP,
    OTHER_FILES_CAP,
    OutputSpec,
    directory_summary,
    existing_planned,
    list_directory,
    spec_frames_present,
    written_since,
)
from flame_mcp.render_plan import frames_of, preflight_problems
from flame_mcp.transport import BridgeProtocolError
from flame_mcp.types import ErrorCode, ServerOpError
from flame_mcp_bridge.impl import FEATURE_CLIENT_HANDLE

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

DEFAULT_TIMEOUT_S: Final[int] = 120
MAX_TIMEOUT_S: Final[int] = 3600
MAX_FRAMES_LIMIT: Final[int] = 100_000
RENDER_KIND: Final[str] = "render"
"""`kind` of a render intent in the journal (`journal.check` dispatches on it)."""
_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"
BRIDGE_BUSY_NOTE: Final[str] = (
    "While a Foreground render is still running the bridge may not answer at all, not even to its "
    "hello: wait a little, then run journal.check again."
)
BRIDGE_BUSY_CODES: Final[frozenset[str]] = frozenset({"BRIDGE_UNAVAILABLE", "TIMEOUT"})
HANDLE_SOURCE_CLIENT: Final[str] = "client"
"""`handleSource` of an intent whose `handle` this server chose and sent to the bridge."""
_PENDING_NOTE: Final[str] = (
    "the bridge answered with a pending handle: the render was still running when timeout_s elapsed"
)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _new_client_handle(rt: Runtime) -> str | None:
    """A fresh handle for the run when the live bridge announces `client_handle`, else None (an
    older bridge would ignore it and use its own)."""
    if FEATURE_CLIENT_HANDLE in transport.probe_features(rt.run_dir):
        return uuid.uuid4().hex
    return None


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


def _node_targets(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The effective nodes with their spec and the directory as it is right now."""
    targets: list[dict[str, Any]] = []
    for node in plan.get("nodes") or []:
        if not node.get("effective"):
            continue
        spec = (
            OutputSpec.from_json(node["outputSpec"])
            if node.get("kind") == "write_file" and isinstance(node.get("outputSpec"), dict)
            else None
        )
        listing = list_directory(spec.directory) if spec is not None else {}
        targets.append(
            {
                "name": node.get("name"),
                "kind": node.get("kind"),
                "plannedFrames": frames_of(node),
                "spec": spec,
                "listing": listing,
                "preexisting": existing_planned(spec, listing) if spec is not None else {},
            }
        )
    return targets


def _intent_record(
    op: RegisteredOperation,
    op_args: Mapping[str, Any],
    plan: Mapping[str, Any],
    targets: list[dict[str, Any]],
    *,
    intent_id: str,
    group_id: str,
    timeout_s: float,
    handle: str | None,
) -> dict[str, Any]:
    nodes = [
        {
            "name": t["name"],
            "kind": t["kind"],
            "plannedFrames": t["plannedFrames"],
            "spec": t["spec"].to_json() if t["spec"] is not None else None,
            "preexisting": {name: list(stat) for name, stat in t["preexisting"].items()},
            "directory": directory_summary(t["listing"]) if t["spec"] is not None else None,
        }
        for t in targets
    ]
    return {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op.name,
        "kind": RENDER_KIND,
        "args": dict(op_args),
        "target": plan.get("stable"),
        "batch": plan.get("batch"),
        "renderOption": "Foreground",
        "timeoutS": timeout_s,
        "handle": handle,
        "handleSource": HANDLE_SOURCE_CLIENT if handle is not None else None,
        "expectedFrames": sum(int(t["plannedFrames"] or 0) for t in targets),
        "nodes": nodes,
        "reelsBefore": plan.get("reels"),
    }


def _judge_nodes(
    targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Per Write File node: the frames present now that were written since the intent, the
    files this node's directory gained that are not its own, and the totals (written,
    expected)."""
    rows: list[dict[str, Any]] = []
    written_total = 0
    expected_total = 0
    owned: dict[str, set[str]] = {}
    reported: dict[str, set[str]] = {}
    for target in targets:
        if target["spec"] is not None:
            owned.setdefault(target["spec"].directory, set()).update(target["spec"].names())
    for target in targets:
        spec: OutputSpec | None = target["spec"]
        if spec is None:
            rows.append({"node": target["name"], "kind": target["kind"], "checked": False})
            continue
        now = list_directory(spec.directory)
        written, missing = spec_frames_present(spec, target["listing"], now)
        expected = spec.count
        written_total += written
        expected_total += expected
        seen = reported.setdefault(spec.directory, set())
        other = [
            n
            for n in written_since(target["listing"], now)
            if n not in owned.get(spec.directory, set()) and n not in seen
        ]
        seen.update(other)
        rows.append(
            {
                "node": target["name"],
                "kind": target["kind"],
                "checked": True,
                "directory": spec.directory,
                "expectedFrames": expected,
                "writtenFrames": written,
                "complete": written == expected,
                "missing": missing,
                "missingTruncated": expected - written > MISSING_CAP,
                "otherNewFiles": other[:OTHER_FILES_CAP],
                "otherNewFilesTruncated": len(other) > OTHER_FILES_CAP,
                "overwrote": len(target["preexisting"]),
            }
        )
    return rows, written_total, expected_total


def frames_verdict(written: int, expected: int) -> str | None:
    if expected == 0:
        return None
    if written >= expected:
        return STATE_LANDED
    return STATE_PARTIAL if written > 0 else STATE_NOT_LANDED


def _fail_env(env: dict[str, Any], code: ErrorCode, message: str) -> None:
    env.update(
        ok=False,
        error=message,
        errorCode=code,
        hint=errors.hint_for(code),
        retryable=errors.is_retryable(code),
    )


def _unknown_end(
    ctx: Runtime,
    env: dict[str, Any],
    op: RegisteredOperation,
    intent_id: str,
    group_id: str,
    code: str,
    timeout_s: float,
    client_handle: str | None,
) -> dict[str, Any]:
    ctx.guard.block(intent_id, group_id, op.name, code)
    note_unresolved(ctx, intent_id, group_id, env)
    handle = (env.get("pending") or {}).get("handle")
    if isinstance(handle, str) and handle:
        if client_handle is not None and handle != client_handle:
            env["degraded"] = [
                *env.get("degraded", []),
                f"the bridge answered with its own pending handle {handle}, not the handle "
                f"{client_handle} this server gave it: journal.check asks for the bridge's own",
            ]
        try:
            ctx.journal.append(
                {
                    "type": "pending",
                    "id": intent_id,
                    "group": group_id,
                    "ts": now_iso(),
                    "handle": handle,
                    "timeoutS": timeout_s,
                }
            )
        except OSError:
            pass  # the intent alone says "in doubt"; without the handle check falls back to files
    if env.get("pending") is not None or env.get("ok") is True:
        _fail_env(env, "TIMEOUT", _PENDING_NOTE)
    hint = str(env.get("hint") or "")
    handle_note = (
        f" If the bridge received the call it keeps the render's result under handle "
        f"{client_handle} (chosen by this server), and journal.check fetches it once the "
        "bridge answers again."
        if client_handle is not None
        else ""
    )
    env["hint"] = (
        f"{hint} It is not known whether the render finished: a Foreground render keeps "
        "Flame occupied and a TIMEOUT does not stop it. This server now refuses every "
        f"write until journal.check (group={group_id!r}) has read Flame and the output files "
        f"again.{handle_note} {BRIDGE_BUSY_NOTE}"
    ).strip()
    env["journal"] = {"group": group_id, "id": intent_id, "undo": "unsupported", "revert": None}
    return env


def _outcome_record(
    intent_id: str,
    group_id: str,
    ok: bool,
    code: str | None,
    message: str | None,
    rows: list[dict[str, Any]],
    written: int,
    expected: int,
    flame: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "type": "outcome",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "ok": ok,
        "applied": [],
        "failed": {} if ok else {"render.batch": message or ""},
        "errorCode": None if ok else code,
        "readBack": None,
        "verdict": frames_verdict(written, expected),
        "expectedFrames": expected,
        "writtenFrames": written,
        "nodes": [
            {
                k: r.get(k)
                for k in ("node", "expectedFrames", "writtenFrames", "complete", "overwrote")
            }
            for r in rows
        ],
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "newClips": flame.get("newClips"),
    }


def run_render(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `render.batch`."""
    started = time.monotonic()
    timeout_s = float(op_args["timeout_s"])
    max_frames = int(op_args["max_frames"])
    allow_default = bool(op_args.get("allow_default_path", False))
    allow_overwrite = bool(op_args.get("allow_overwrite", False))
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
        try:
            refuse_if_denied(rt, ("render.plan",))
        except ServerOpError as exc:
            return _refusal(op, exc.code, exc.message, started, hint=exc.hint)

        plan_env = rt.do("render.plan", {"target": op_args["target"]})
        if plan_env.get("ok") is not True:
            return _refusal(
                op,
                errors.as_error_code(str(plan_env.get("errorCode"))),
                "nothing was run: the group's Write File / Render nodes could not be read "
                f"({plan_env.get('error')})",
                started,
                result={"ran": False},
                hint=plan_env.get("hint"),
            )
        plan: dict[str, Any] = plan_env["result"]
        problems = preflight_problems(
            plan,
            max_frames=max_frames,
            allow_default_path=allow_default,
            allow_overwrite=allow_overwrite,
        )
        if not isinstance(plan.get("stable"), dict):
            problems.append(
                {
                    "code": "no_uid",
                    "node": None,
                    "message": "the group has no uid, so it cannot be found again for the outcome",
                }
            )
        if problems:
            return _refusal(
                op,
                "PRECONDITION_UNMET",
                "nothing was rendered: " + "; ".join(p["message"] for p in problems),
                started,
                result={"ran": False, "problems": problems, "plan": plan},
                hint=(
                    "render.plan shows what the group would write. Nothing was written and "
                    "nothing was journaled."
                ),
            )

        targets = _node_targets(plan)
        group_id = group if group is not None else new_id("g")
        intent_id = new_id("i")
        client_handle = _new_client_handle(rt)
        try:
            rt.journal.append(
                _intent_record(
                    op,
                    op_args,
                    plan,
                    targets,
                    intent_id=intent_id,
                    group_id=group_id,
                    timeout_s=timeout_s,
                    handle=client_handle,
                )
            )
        except OSError as exc:
            return _refusal(
                op,
                "POLICY_DENIED",
                f"nothing was rendered: the journal cannot be written ({exc}), and this server "
                "does not render without an intent on record",
                started,
                result={"ran": False},
                hint=f"Make {rt.journal.directory} writable, then retry.",
            )

        run_args = {**op_args, "target": plan["stable"]}
        env = execution.run_op(
            op, run_args, run_dir=rt.run_dir, timeout_s=timeout_s, handle=client_handle
        )
        end_code = env.get("errorCode")
        if end_code in UNKNOWN_END_CODES or env.get("pending") is not None:
            return _unknown_end(
                rt,
                env,
                op,
                intent_id,
                group_id,
                str(end_code or "TIMEOUT"),
                timeout_s,
                client_handle,
            )
        finished = _finish(rt, op, env, plan, targets, intent_id, group_id, started)
        if client_handle is not None and end_code != "BRIDGE_AMBIGUOUS":
            _remove_result_file(rt, finished, client_handle)
        return finished


def _remove_result_file(rt: Runtime, env: dict[str, Any], handle: str) -> None:
    """The bridge keeps the result of a run that has a client handle in a file even when the
    answer came in-band; one poll hands it over and deletes it. Best effort: a failure only
    leaves the file to the bridge's own 24 h sweep, and is noted in `degraded`."""
    try:
        br = transport.poll(handle, run_dir=rt.run_dir)
    except BridgeProtocolError as exc:
        if "unknown handle" in str(exc):
            return  # nothing kept under it (or already gone): nothing to remove
        problem = f"PROTOCOL_ERROR: {exc}"
    else:
        if br.error_code is None:
            return
        problem = f"{br.error_code}: {br.error}"
    env["degraded"] = [
        *env.get("degraded", []),
        f"the result file the bridge keeps under handle {handle} could not be removed ({problem}); "
        "the bridge deletes it after 24 hours",
    ]


@dataclass(frozen=True)
class RenderJudgement:
    ok: bool
    code: ErrorCode | None
    message: str | None
    degraded: list[str] = field(default_factory=list)


def judge_render(
    *,
    env_ok: bool,
    env_code: Any,
    env_error: Any,
    flame: Mapping[str, Any],
    written: int,
    expected: int,
    checked: bool,
) -> RenderJudgement:
    """What a finished render counts as, from Flame's answer (`env_*`, the op's own value
    `flame`) and the frames found: the same rule for `render.batch` and for a `journal.check`
    that got the answer through the bridge's poll. An error of the render stays an error
    (whatever is on disk); a render that returned without an error but wrote fewer frames than
    planned is SILENT_NOOP (FLAME_API_ERROR when it returned False); a False return with every
    frame there is a success with a note."""
    if not env_ok:
        return RenderJudgement(False, errors.as_error_code(str(env_code)), str(env_error))
    if checked and written < expected:
        code: ErrorCode = "FLAME_API_ERROR" if flame.get("returned") is False else "SILENT_NOOP"
        message = (
            f"render returned {flame.get('returned')!r} without an exception but only {written} "
            f"of {expected} planned frames were written (see result.nodes for what is missing)"
        )
        return RenderJudgement(False, code, message)
    degraded: list[str] = []
    if flame.get("returned") is not True:
        degraded.append(
            f"render returned {flame.get('returned')!r}, not True; the output files are complete"
        )
    return RenderJudgement(True, None, None, degraded)


def _finish(
    rt: Runtime,
    op: RegisteredOperation,
    env: dict[str, Any],
    plan: Mapping[str, Any],
    targets: list[dict[str, Any]],
    intent_id: str,
    group_id: str,
    started: float,
) -> dict[str, Any]:
    flame_raw = env.get("result")
    flame: dict[str, Any] = flame_raw if isinstance(flame_raw, dict) else {}
    rows, written, expected = _judge_nodes(targets)
    checked = any(r.get("checked") for r in rows)
    judged = judge_render(
        env_ok=env.get("ok") is True,
        env_code=env.get("errorCode"),
        env_error=env.get("error"),
        flame=flame,
        written=written,
        expected=expected,
        checked=checked,
    )
    ok, code, message, degraded = judged.ok, judged.code, judged.message, judged.degraded
    verified: bool | None = (written >= expected) if checked else None
    result: dict[str, Any] = {
        "ran": True,
        "group": plan.get("batch"),
        "renderOption": "Foreground",
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "expectedFrames": expected,
        "writtenFrames": written,
        "verified": verified,
        "verifiedNote": (
            None
            if checked
            else "only Render nodes were rendered: they write clips into reels (result.newClips), "
            "not files, so nothing could be checked on disk"
        ),
        "nodes": rows,
        "newClips": flame.get("newClips"),
        "reelsBefore": flame.get("reelsBefore"),
        "reelsAfter": flame.get("reelsAfter"),
        "undo": "unsupported: the frames on disk and the clips in the reels stay",
    }
    if not ok and code is not None:
        _fail_env(env, code, message or "")
    env["result"] = result
    env["affected"] = written
    env["undoSteps"] = 0
    env["partial"] = {"written": written, "expected": expected} if checked else None
    env["degraded"] = [*env.get("degraded", []), *degraded]
    env["journal"] = {"group": group_id, "id": intent_id, "undo": "unsupported", "revert": None}
    env["durationMs"] = _elapsed_ms(started)
    record = _outcome_record(intent_id, group_id, ok, code, message, rows, written, expected, flame)
    try:
        rt.journal.append(record)
    except OSError as exc:
        rt.guard.block(intent_id, group_id, op.name, "JOURNAL_OUTCOME_NOT_WRITTEN")
        env["degraded"] = [
            *env["degraded"],
            f"the outcome could not be journaled ({exc}); the run's answer stands but this "
            "server now refuses writes until journal.check has settled the intent",
        ]
    return env
