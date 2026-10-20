"""`desktop.reformat`: the answer of a call, built from what the clips left in `Run`."""

from __future__ import annotations

from typing import Any

from flame_mcp import envelope as envelope_mod
from flame_mcp.desktop_reformat_state import (
    DUPLICATE,
    DUPLICATE_NOTE,
    IN_PLACE,
    IN_PLACE_NOTE,
    UNCHANGED_NOTE,
    UNKNOWN_HINT,
    Run,
    elapsed_ms,
)
from flame_mcp.errors import as_error_code
from flame_mcp.types import ErrorCode


def _error_code_of(failed: list[dict[str, Any]]) -> ErrorCode:
    codes = {str(f.get("errorCode")) for f in failed}
    return as_error_code(codes.pop()) if len(codes) == 1 else "FLAME_API_ERROR"


def _journal_ref(run: Run, *, revertable: bool) -> dict[str, Any] | None:
    if not run.intent_ids:
        return None
    return {
        "group": run.group,
        "id": run.intent_ids[0],
        "ids": list(run.intent_ids),
        "revert": (
            {"op": "journal.revert", "args": {"group": run.group}}
            if revertable and run.created_any
            else None
        ),
    }


def _result_body(run: Run) -> dict[str, Any]:
    body: dict[str, Any] = {
        "requested": len(run.rows),
        "mode": DUPLICATE if run.duplicate else IN_PLACE,
        "reformatted": run.reformatted,
        "notReformatted": run.failed,
        "partial": bool(run.reformatted and run.failed),
        "warnings": run.warnings,
        "notes": [],
    }
    started = {c["index"] for c in [*run.reformatted, *run.failed]}
    not_run = [
        {"index": i, "name": row.get("name"), "uid": row.get("uid")}
        for i, row in enumerate(run.rows)
        if i not in started
    ]
    if not_run:
        body["notRun"] = not_run
    if run.changed or run.reformatted:
        if run.duplicate:
            body["notes"].append(DUPLICATE_NOTE)
        elif run.created_any:
            body["notes"].append(IN_PLACE_NOTE)
    if run.reformatted and not run.failed and not run.changed and run.stop is None:
        body["notes"].append(UNCHANGED_NOTE)
    return body


def finish(run: Run, started: float) -> dict[str, Any]:
    op = run.op
    body = _result_body(run)
    last = run.last_env or {}
    common: dict[str, Any] = {
        "result": body,
        "affected": run.changed,
        "undo_steps": 0,
        "context": last.get("context"),
        "execution": envelope_mod.execution_meta(op),
        "duration_ms": elapsed_ms(started),
    }
    stop = run.stop
    if stop is not None:
        unknown = stop.get("intent") is not None
        env = envelope_mod.build_envelope(
            ok=False,
            error=str(stop["message"]),
            error_code=as_error_code(str(stop["code"])),
            pending=stop.get("pending"),
            partial=(
                {
                    "reformatted": len(run.reformatted),
                    "notReformatted": len(run.rows) - len(run.reformatted),
                }
                if run.reformatted
                else None
            ),
            journal=_journal_ref(run, revertable=not unknown),
            hint_override=(
                (str(stop.get("hint") or "") + " " + UNKNOWN_HINT.format(group=run.group)).strip()
                if unknown
                else "Clips before it were reformatted; see result.reformatted."
            ),
            **common,
        )
    elif run.reformatted:
        partial = (
            {"reformatted": len(run.reformatted), "notReformatted": len(run.failed)}
            if run.failed
            else None
        )
        env = envelope_mod.build_envelope(
            ok=True,
            partial=partial,
            journal=_journal_ref(run, revertable=True),
            **common,
        )
        if partial is not None:
            env["degraded"] = [
                f"{len(run.failed)} of {len(run.rows)} clip(s) were not reformatted "
                "(result.notReformatted)"
            ]
    else:
        reasons = "; ".join(
            f"targets[{f['index']}] ({(f.get('from') or {}).get('name')!r}): {f.get('reason')}"
            for f in run.failed
        )
        env = envelope_mod.build_envelope(
            ok=False,
            error=f"nothing was reformatted: {reasons}",
            error_code=_error_code_of(run.failed),
            journal=_journal_ref(run, revertable=True),
            hint_override=(
                "A clip that changed all the same can be put back: journal.revert(group)."
                if run.created_any
                else None
            ),
            **common,
        )
    env["degraded"] = [*env.get("degraded", []), *run.degraded]
    if run.warnings:
        env["degraded"] = [*env["degraded"], *[f"read-back differs: {w}" for w in run.warnings]]
    return env
