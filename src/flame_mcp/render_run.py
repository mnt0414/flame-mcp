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
    the intent open and closes the write guard (`WriteGuard`; writes are refused until
    `journal.check` has decided, `render_check.check_render_intent`). `journal.check` asks the
    bridge for the run's result with `transport.poll` -- that, not the files, says the render
    has ended (in one run the frames were complete about 8 s before render() returned) -- and
    then counts the files. The bridge gives a
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
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import errors
from flame_mcp.checkpointed import refuse_if_denied
from flame_mcp.journal import (
    STATE_LANDED,
    STATE_NOT_LANDED,
    STATE_PARTIAL,
    new_id,
    now_iso,
)
from flame_mcp.journal_records import IntentRecord, OutcomeRecord
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
from flame_mcp.types import ErrorCode, ServerOpError
from flame_mcp.write_run import (
    HANDLE_SOURCE_CLIENT,
    Refused,
    RenderJudgement,
    append_intent,
    new_client_handle,
    run_and_close,
    settle_envelope,
)

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

DEFAULT_TIMEOUT_S: Final[int] = 120
MAX_TIMEOUT_S: Final[int] = 3600
MAX_FRAMES_LIMIT: Final[int] = 100_000
RENDER_KIND: Final[str] = "render"
"""`kind` of a render intent in the journal (`journal.check` dispatches on it)."""
_NOT_FOUND_CODE: Final[str] = "SELECTOR_EMPTY"


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
) -> IntentRecord:
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
) -> OutcomeRecord:
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


def _read_plan(
    rt: Runtime, op: RegisteredOperation, op_args: Mapping[str, Any], started: float
) -> dict[str, Any]:
    """The enriched plan of the group; `Refused` when it cannot be read."""
    try:
        refuse_if_denied(rt, ("render.plan",))
    except ServerOpError as exc:
        raise Refused(
            envelope_mod.refusal(op, exc.code, exc.message, started, hint=exc.hint)
        ) from exc
    plan_env = rt.do("render.plan", {"target": op_args["target"]})
    if plan_env.get("ok") is not True:
        raise Refused(
            envelope_mod.refusal(
                op,
                errors.as_error_code(str(plan_env.get("errorCode"))),
                "nothing was run: the group's Write File / Render nodes could not be read "
                f"({plan_env.get('error')})",
                started,
                result={"ran": False},
                hint=plan_env.get("hint"),
            )
        )
    plan: dict[str, Any] = plan_env["result"]
    return plan


def _check_preflight(
    op: RegisteredOperation,
    op_args: Mapping[str, Any],
    plan: Mapping[str, Any],
    started: float,
) -> None:
    problems = preflight_problems(
        plan,
        max_frames=int(op_args["max_frames"]),
        allow_default_path=bool(op_args.get("allow_default_path", False)),
        allow_overwrite=bool(op_args.get("allow_overwrite", False)),
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
        raise Refused(
            envelope_mod.refusal(
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
        )


def run_render(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `render.batch`."""
    started = time.monotonic()
    timeout_s = float(op_args["timeout_s"])
    try:
        plan = _read_plan(rt, op, op_args, started)
        _check_preflight(op, op_args, plan, started)
        targets = _node_targets(plan)
        group_id = group if group is not None else new_id("g")
        intent_id = new_id("i")
        client_handle = new_client_handle(rt)
        append_intent(
            rt,
            op,
            _intent_record(
                op,
                op_args,
                plan,
                targets,
                intent_id=intent_id,
                group_id=group_id,
                timeout_s=timeout_s,
                handle=client_handle,
            ),
            started,
            done="rendered",
            action="render",
        )
    except Refused as stop:
        return stop.env
    return run_and_close(
        rt,
        op,
        {**op_args, "target": plan["stable"]},
        timeout_s=timeout_s,
        handle=client_handle,
        intent_id=intent_id,
        group_id=group_id,
        finish=lambda env: _finish(rt, op, env, plan, targets, intent_id, group_id, started),
    )


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
    `flame`) and the frames found.

    The rule is the same for `render.batch` and for a `journal.check` that got the answer through
    the bridge's poll. An error of the render stays an error, whatever is on disk. A render that
    returned without an error but wrote some of the planned frames is FLAME_API_ERROR starting
    `partial:` (the frames stay; `journal.check` reads it as partial too). A render that wrote
    none is SILENT_NOOP (FLAME_API_ERROR when it returned False). A False return with every frame
    there is a success with a note. With no Write File to count (`checked` false: only Render
    nodes), the clips the reels gained (`flame['newClips']`) are the evidence. None gained is
    SILENT_NOOP, unless a reel could not be read, this server does not check a destination, or
    the Desktop reel group it names is not there; then it is a success with a note."""
    if not env_ok:
        return RenderJudgement(False, errors.as_error_code(str(env_code)), str(env_error))
    returned = flame.get("returned")
    if checked and written < expected:
        short = (
            f"render returned {returned!r} without an exception but only {written} "
            f"of {expected} planned frames were written (see result.nodes for what is missing)"
        )
        if written > 0:
            return RenderJudgement(False, "FLAME_API_ERROR", f"{errors.PARTIAL_PREFIX} {short}")
        code: ErrorCode = "FLAME_API_ERROR" if returned is False else "SILENT_NOOP"
        return RenderJudgement(False, code, short)
    degraded: list[str] = []
    new_clips = flame.get("newClips")
    if not checked and isinstance(new_clips, list) and not new_clips:
        unread = flame.get("newClipsUnknown")
        unfollowed = flame.get("destinationUnsupported")
        no_group = [
            r.get("name")
            for r in flame.get("reelsAfter") or []
            if isinstance(r, Mapping) and r.get("groupMissing")
        ]
        if not unread and not unfollowed and not no_group:
            code = "FLAME_API_ERROR" if returned is False else "SILENT_NOOP"
            message = (
                f"render returned {returned!r} without an exception but no reel gained a clip "
                "(only Render nodes were rendered; result.newClips is empty)"
            )
            return RenderJudgement(False, code, message)
        degraded.append(
            "no new clip was seen, but the Render nodes' output could not be fully read back "
            f"(reels that could not be read: {unread!r}; destinations this server does not check: "
            f"{unfollowed!r}; reel groups that are not there: {no_group!r})"
        )
    if returned is not True:
        seen = (
            "the output files are complete"
            if checked
            else "the reels gained the clips (result.newClips)"
            if new_clips
            else "its output could not be checked"
        )
        degraded.append(f"render returned {returned!r}, not True; {seen}")
    return RenderJudgement(True, None, None, degraded)


def _reel_key(kind: str, group: Any, reel: Any) -> tuple[str, str]:
    """How Flame names a reel in newClips / reelsAfter: a Desktop reel as `group/reel`."""
    return (kind, f"{group}/{reel}" if kind == "desktop" else str(reel))


def missing_outputs(
    plan: Mapping[str, Any], new_clips: Any, unknown: Any = None, reels_after: Any = None
) -> list[dict[str, Any]]:
    """Per destination reel, the effective Render nodes that write into it against the clips the
    reel gained. A reel that gained fewer clips than it has nodes is listed. A node whose
    destination could not be read or followed, or whose reel could not be read before the render
    (`unknown`, Flame's `newClipsUnknown`) or after it (`reels_after`, Flame's `reelsAfter`, a
    `clipCount` of None), is not counted."""
    unread = {(e.get("kind"), e.get("name")) for e in unknown or [] if isinstance(e, Mapping)}
    unread |= {
        (r.get("kind"), r.get("name"))
        for r in reels_after or []
        if isinstance(r, Mapping) and r.get("clipCount") is None
    }
    by_reel: dict[tuple[str, str], list[str]] = {}
    for row in plan.get("nodes") or []:
        info = row.get("destinationReel")
        if not info or row.get("kind") != "render" or not row.get("effective"):
            continue
        if info.get("groupMissing") or info.get("exists") is None:
            continue
        kind = info.get("kind")
        if kind not in ("shelf", "desktop") or not isinstance(info.get("reel"), str):
            continue
        key = _reel_key(kind, info.get("group"), info.get("reel"))
        if key not in unread:
            by_reel.setdefault(key, []).append(str(row.get("name")))
    gained: dict[tuple[str, str], int] = {}
    for entry in new_clips if isinstance(new_clips, list) else []:
        if isinstance(entry, Mapping) and entry.get("kind") in ("shelf", "desktop"):
            clips = entry.get("clips")
            gained[(entry["kind"], str(entry.get("name")))] = (
                len(clips) if isinstance(clips, list) else 0
            )
    return [
        {"reel": key[1], "expected": len(nodes), "gained": gained.get(key, 0), "nodes": nodes}
        for key, nodes in by_reel.items()
        if gained.get(key, 0) < len(nodes)
    ]


def _render_result(
    plan: Mapping[str, Any],
    flame: Mapping[str, Any],
    rows: list[dict[str, Any]],
    written: int,
    expected: int,
    checked: bool,
    missing: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ran": True,
        "group": plan.get("batch"),
        "renderOption": "Foreground",
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "expectedFrames": expected,
        "writtenFrames": written,
        "verified": (written >= expected) if checked else None,
        "verifiedNote": (
            None
            if checked
            else "only Render nodes were rendered: they write clips into reels (result.newClips), "
            "not files, so nothing could be checked on disk"
        ),
        "nodes": rows,
        "newClips": flame.get("newClips"),
        "newClipsUnknown": flame.get("newClipsUnknown"),
        "missingOutputs": missing or None,
        "reelsBefore": flame.get("reelsBefore"),
        "reelsAfter": flame.get("reelsAfter"),
        "destinationUnsupported": flame.get("destinationUnsupported"),
        "undo": "unsupported: the frames on disk and the clips in the reels stay",
    }


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
    degraded = judged.degraded
    missing = (
        missing_outputs(
            plan, flame.get("newClips"), flame.get("newClipsUnknown"), flame.get("reelsAfter")
        )
        if judged.ok
        else []
    )
    if missing:
        degraded = [
            *degraded,
            "some Render nodes wrote no clip into their destination reel (result.missingOutputs "
            "lists, per reel, the clips expected and the clips gained)",
        ]
    record = _outcome_record(
        intent_id, group_id, judged.ok, judged.code, judged.message, rows, written, expected, flame
    )
    return settle_envelope(
        rt,
        op,
        env,
        judged,
        record,
        result=_render_result(plan, flame, rows, written, expected, checked, missing),
        affected=written,
        partial={"written": written, "expected": expected} if checked else None,
        degraded=degraded,
        intent_id=intent_id,
        group_id=group_id,
        started=started,
    )
