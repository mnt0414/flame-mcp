"""`desktop.sync_record_tc`, server side.

  1. read     one Flame call reads the facts of every target (`tc_prelude._rtc_facts`); nothing is
              written. `tc_rules.judge` gives each clip `sync`, `in_sync` or `skipped` + a reason.
  2. write    (not with dry_run) one `desktop.set_record_tc` call per `sync` clip, in one journal
              group, each with its own intent, compare-and-swap and outcome. The first failure
              stops the call; what was written stays journaled.
  3. answer   one row per target: status, reason, before / after, and whether `record_in` reads
              as the Source TC afterwards.

A step that ends without an answer (TIMEOUT / BRIDGE_LOST) is the job of the child call
(`journaled_write`): it shuts the write guard until journal.check has decided.
"""

from __future__ import annotations

import time
from typing import Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.desktop_run import PHASE_ARG, PHASE_READ, pre_read_failure
from flame_mcp.journal import new_id, now_iso
from flame_mcp.registry import RegisteredOperation
from flame_mcp.tc_rules import STATUS_SKIPPED, STATUS_SYNC, judge
from flame_mcp.transport import elapsed_ms

SYNC_OP: Final[str] = "desktop.sync_record_tc"
SET_OP: Final[str] = "desktop.set_record_tc"
GROUP_KIND: Final[str] = "desktop.sync_record_tc"


def build_rows(facts_list: list[dict[str, Any]], include_risky: bool) -> list[dict[str, Any]]:
    """One row per target; a clip listed twice is `skipped` (`duplicate`) the second time."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, facts in enumerate(facts_list):
        uid = facts.get("uid")
        verdict = judge(facts, include_risky=include_risky)
        if isinstance(uid, str) and uid in seen:
            verdict = {
                "status": STATUS_SKIPPED,
                "reason": "duplicate",
                "note": "the same clip is listed twice",
                "risky": False,
            }
        elif isinstance(uid, str):
            seen.add(uid)
        rows.append(
            {
                "index": index,
                "name": facts.get("name"),
                "uid": uid,
                "selector": facts.get("selector"),
                "status": verdict["status"],
                "reason": verdict["reason"],
                "note": verdict["note"],
                "risky": verdict["risky"],
                "sourceTcFrame": facts.get("sourceInFrame"),
                "before": {
                    "recordTcFrame": facts.get("recordInFrame"),
                    "startTimeFrame": facts.get("startTimeFrame"),
                    "currentTimeFrame": facts.get("currentTimeFrame"),
                },
                "after": None,
            }
        )
    return rows


def _set_args(row: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {
        "target": row["selector"],
        "start_time_frame": row["sourceTcFrame"],
    }
    if row["before"]["currentTimeFrame"] is not None:
        args["current_time_frame"] = row["sourceTcFrame"]
    return args


def _write_row(rt: Any, group: str, row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Write one `sync` row; the child's envelope and, when it failed, why."""
    if not isinstance(row["selector"], dict):
        return {}, "the clip has no wiretap id, so it cannot be found again for the write"
    env = rt.call(SET_OP, _set_args(row), group=group)
    result = env.get("result") if isinstance(env.get("result"), dict) else {}
    row["intent"] = (env.get("journal") or {}).get("id")
    if env.get("ok") is not True:
        return env, f"{env.get('errorCode')}: {env.get('error')}"
    record_in = result.get("recordInFrame")
    row["after"] = {
        "recordTcFrame": record_in,
        "startTimeFrame": (result.get("after") or {}).get("start_time_frame"),
        "currentTimeFrame": (result.get("after") or {}).get("current_time_frame"),
    }
    if record_in != row["sourceTcFrame"]:
        return env, f"record_in reads {record_in!r} after the write, not {row['sourceTcFrame']!r}"
    row["status"] = "synced"
    return env, None


def _tally(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row["status"]] = out.get(row["status"], 0) + 1
    return out


def run_sync(
    rt: Any, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    started = time.monotonic()
    meta = envelope_mod.execution_meta(op)
    with rt.write_lock:
        denial = rt.guard.denial()
        if denial is not None:
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=elapsed_ms(started),
                hint=denial.hint,
            )
        pre = execution.run_op(op, {**op_args, PHASE_ARG: PHASE_READ}, run_dir=rt.run_dir)
        raw = pre.get("result")
        facts = raw.get("rows") if isinstance(raw, dict) else None
        if pre.get("ok") is not True or not isinstance(facts, list):
            return pre_read_failure(op, pre, "written")
        wanted = len(op_args["targets"])
        if len(facts) != wanted:
            return pre_read_failure(
                op,
                {
                    **pre,
                    "ok": False,
                    "errorCode": "FLAME_API_ERROR",
                    "error": f"the read answered for {len(facts)} of {wanted} targets",
                },
                "written",
            )
        rows = build_rows(facts, bool(op_args.get("include_risky", False)))
        todo = [r for r in rows if r["status"] == STATUS_SYNC]
        body: dict[str, Any] = {"dryRun": bool(op_args.get("dry_run", False)), "rows": rows}
        if body["dryRun"] or not todo:
            body["counts"] = _tally(rows)
            return _answer(op, body, meta, started, None, written=0)
        group_id = group if group is not None else new_id("g")
        try:
            rt.journal.append(
                {
                    "type": "group",
                    "group": group_id,
                    "ts": now_iso(),
                    "kind": GROUP_KIND,
                    "label": None,
                    "steps": [SET_OP] * len(todo),
                }
            )
        except OSError as exc:
            return _answer(
                op,
                {**body, "counts": _tally(rows)},
                meta,
                started,
                None,
                written=0,
                refusal=(
                    "POLICY_DENIED",
                    f"nothing was written: the journal cannot be written ({exc})",
                ),
            )
        failure: str | None = None
        written = 0
        for row in todo:
            _env, why = _write_row(rt, group_id, row)
            if why is not None:
                failure = f"{row['name']!r}: {why}"
                row["status"] = "failed"
                row["note"] = why
                break
            written += 1
        for row in todo:
            if row["status"] == STATUS_SYNC:
                row["status"] = "not_run"
        body["counts"] = _tally(rows)
        journal = {
            "group": group_id,
            "id": next((r["intent"] for r in todo if r.get("intent")), None),
            "revert": {"op": "journal.revert", "args": {"group": group_id}} if written else None,
        }
        return _answer(op, body, meta, started, journal, written=written, failure=failure)


def _answer(
    op: RegisteredOperation,
    body: dict[str, Any],
    meta: dict[str, Any],
    started: float,
    journal: dict[str, Any] | None,
    *,
    written: int,
    failure: str | None = None,
    refusal: tuple[str, str] | None = None,
) -> dict[str, Any]:
    code = None
    error = None
    partial = None
    if refusal is not None:
        code, error = refusal
    elif failure is not None:
        code, error = "FLAME_API_ERROR", ("partial: " if written else "") + failure
        if written:
            partial = {"synced": written, "notSynced": len(body["rows"]) - written}
    env = envelope_mod.build_envelope(
        ok=code is None,
        result=body,
        affected=written,
        partial=partial,
        error=error,
        error_code=code,  # type: ignore[arg-type]
        journal=journal,
        execution=meta,
        duration_ms=elapsed_ms(started),
    )
    note = _skipped_note(body["rows"])
    if note is not None:
        env["degraded"] = [*env.get("degraded", []), note]
    return env


def _skipped_note(rows: list[dict[str, Any]]) -> str | None:
    reasons: dict[str, int] = {}
    for row in rows:
        if row["status"] == STATUS_SKIPPED:
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    if not reasons:
        return None
    listed = ", ".join(f"{reason} x{count}" for reason, count in sorted(reasons.items()))
    return f"{sum(reasons.values())} clip(s) skipped and not written ({listed})"
