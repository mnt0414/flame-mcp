"""`render.still`, server side: one frame of a clip, written by Flame and turned into a small PNG.

Two routes, chosen by `source`:

  {path}            the file is converted by ffmpeg (`still_proxy`); nothing is sent to Flame and
                    nothing is journaled.
  {target, frame}   a write-ahead pair like `render.export` (`export_run.py`), whose parts it uses:

  1. read       one Flame call (`_st_inspect`): type, name, uid, wiretap id, duration,
                `start_frame`, the Flame version, whether the temporary reel is there. Anything
                but a PyClip is SELECTOR_TYPE; a `frame` outside 1..duration is INVALID_ARGS
                (`still_marks` decides the marks in one place); the export preset must exist.
  2. check      the output directory (the caller's, which must be empty -- the preset decides the
                names of the files -- or a new one below the stills directory).
  3. intent     `kind: "still"`: the stable selector, the frame, the marks, the temporary reel and
                copy name, the directory and the client handle, journaled before the bridge gets
                the call.
  4. still      one `main-async` Flame call (`_st_run`): a reference copy of the clip is made in a
                reel of the first reel group of the Desktop, renamed, given the marks, exported
                with `export_between_marks` in the Foreground and removed again (only while its
                uid and name are the ones it was given).
  5. outcome    the output directory is listed: one new file = landed, none = SILENT_NOOP, two or
                more = FLAME_API_ERROR. The outcome is journaled as what Flame did.
  6. proxy      ffmpeg makes the PNG; a failure of it is an error of the call, not of the outcome.

A call that ends without an answer (TIMEOUT / BRIDGE_LOST / a `pending` handle) leaves the intent
open and closes the write guard; `still_check.check_still_intent` decides it and removes what the
dead call left. There is no undo: the files stay on disk.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.checkpointed import ASCII_REFUSAL_HINT, _first_non_ascii
from flame_mcp.desktop_run import (
    EXPECTED_ARG,
    PHASE_ARG,
    PHASE_READ,
    PHASE_WRITE,
    pre_read_failure,
)
from flame_mcp.export_run import (
    EXISTING_CAP,
    NOTHING_WRITTEN,
    before_stats_of,
    count_written,
    directory_before,
    judge_export,
    outcome_verdict,
    unsure_reason,
)
from flame_mcp.journal import new_id, now_iso
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.registry import RegisteredOperation
from flame_mcp.render_clip_run import _stable_selector, target_info
from flame_mcp.render_files import (
    EXPORT_COUNT_DEPTH,
    EXPORT_COUNT_LIMIT,
    EXPORT_SCAN_LIMIT,
    Tree,
    scan_tree,
    visible_entries,
)
from flame_mcp.render_run import (
    HANDLE_SOURCE_CLIENT,
    RenderJudgement,
    _fail_env,
    _new_client_handle,
    _refusal,
    _remove_result_file,
    _unknown_end,
)
from flame_mcp.still_proxy import (
    PROXY_MIME,
    STILLS_DIR_ENV,
    Ffmpeg,
    Proxy,
    ProxyError,
    make_private_file,
    make_proxy,
    make_run_dir,
    resolve_ffmpeg,
    stills_root,
)
from flame_mcp.transport import elapsed_ms

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

OP_NAME: Final[str] = "render.still"
STILL_KIND: Final[str] = "still"
"""`kind` of a render.still intent in the journal (`journal.check` dispatches on it)."""
STILL_ARG: Final[str] = "_still"
PHASE_CLEANUP: Final[str] = "cleanup"
TEMP_REEL: Final[str] = "flame_mcp_still"
TEMP_PREFIX: Final[str] = "flame_mcp_still_"
PRESETS_DIR_ENV: Final[str] = "FLAME_MCP_PRESETS_DIR"
DEFAULT_PRESETS_ROOT: Final[str] = "/opt/Autodesk/presets"
"""Where Flame keeps its export presets; `<root>/<Flame version>/<PRESET_REL>` is the file."""
PRESET_REL: Final[str] = "export/presets/flame/file_sequence/Tiff/Tiff (16-bit LZW).xml"
UNDO_NOTE: Final[str] = "unsupported: the exported frame and its proxy stay on disk"
_VERSION_NUMBERS = re.compile(r"[0-9]+(\.[0-9]+)+")


def still_marks(frame: int) -> tuple[int, int]:
    """The in and out mark of the reference copy for `frame` (1-based): `(frame, frame + 1)`, the
    out mark not being included, so exactly one frame is exported. Integer marks count from the
    head of the clip whatever its `start_frame` (2025.2.4, start_frame 1001: marks (8, 9) export
    the 8th frame). This is the only place that decides the marks."""
    return frame, frame + 1


def preset_path(version: str) -> str | None:
    """The preset file for Flame `version` (its full string, else the number in it)."""
    root = os.environ.get(PRESETS_DIR_ENV) or DEFAULT_PRESETS_ROOT
    candidates = [version]
    found = _VERSION_NUMBERS.search(version)
    if found is not None and found.group(0) != version:
        candidates.append(found.group(0))
    for candidate in candidates:
        path = os.path.join(root, candidate, PRESET_REL)
        if os.path.isfile(path):
            return path
    return None


def new_temp_name() -> str:
    return f"{TEMP_PREFIX}{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


def cleanup_params(intent: Mapping[str, Any]) -> dict[str, Any]:
    """What `_st_cleanup` needs to find the copy a call made, from its intent."""
    return {
        "reel": intent.get("tempReel") or TEMP_REEL,
        "tempName": intent.get("tempName") or "",
        "marks": list(intent.get("marks") or [None, None]),
        "duration": intent.get("duration"),
        "reelPreexisting": bool(intent.get("tempReelPreexisting")),
    }


def judge_still(
    *,
    env_ok: bool,
    env_code: Any,
    env_error: Any,
    flame: Mapping[str, Any],
    written: int,
    unsure: str | None,
) -> RenderJudgement:
    """What a finished still counts as, from Flame's answer and the files found. One file is the
    frame; none is SILENT_NOOP (`judge_export`); two or more are FLAME_API_ERROR -- the files
    stay, and a person has to look at them."""
    if env_ok and written >= 2:
        return RenderJudgement(
            False,
            "FLAME_API_ERROR",
            f"the export wrote {written} files for one frame, not 1: the marks did not select a "
            "single frame (the files stay in the output directory)",
        )
    return judge_export(
        env_ok=env_ok,
        env_code=env_code,
        env_error=env_error,
        flame=flame,
        written=written,
        expected=None,
        unsure=unsure,
    )


def _intent_record(
    op: RegisteredOperation,
    op_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    stable: Any,
    before: Tree,
    plan: Mapping[str, Any],
    *,
    intent_id: str,
    group_id: str,
    timeout_s: float,
    handle: str | None,
) -> dict[str, Any]:
    before_files, hidden_count = directory_before(before)
    return {
        "type": "intent",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "op": op.name,
        "kind": STILL_KIND,
        "args": dict(op_args),
        "target": stable,
        "targetType": facts.get("type"),
        "name": facts.get("name"),
        "uid": facts.get("uid"),
        "frame": plan["frame"],
        "marks": list(plan["marks"]),
        "duration": facts.get("duration"),
        "tempReel": TEMP_REEL,
        "tempReelPreexisting": bool(facts.get("reelExists")),
        "tempName": plan["tempName"],
        "preset": plan["preset"],
        "outputDir": plan["outputDir"],
        "runDir": plan["runDir"],
        "directoryBefore": before_files,
        "hiddenBefore": hidden_count,
        "expectedFrames": 1,
        "timeoutS": timeout_s,
        "handle": handle,
        "handleSource": HANDLE_SOURCE_CLIENT if handle is not None else None,
    }


def _outcome_record(
    intent_id: str,
    group_id: str,
    judged: RenderJudgement,
    flame: Mapping[str, Any],
    written: int,
    unsure: str | None,
) -> dict[str, Any]:
    return {
        "type": "outcome",
        "id": intent_id,
        "group": group_id,
        "ts": now_iso(),
        "ok": judged.ok,
        "applied": [],
        "failed": {} if judged.ok else {OP_NAME: judged.message or ""},
        "errorCode": judged.code,
        "readBack": None,
        "verdict": outcome_verdict(judged, written, unsure),
        "expectedFrames": 1,
        "writtenCount": written,
        "countUnsure": unsure,
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "tempRemoved": flame.get("tempRemoved"),
        "tempReelCreated": flame.get("tempReelCreated"),
    }


def _ffmpeg_refusal(op: RegisteredOperation, started: float, result: Any = None) -> dict[str, Any]:
    return _refusal(
        op,
        "PRECONDITION_UNMET",
        "nothing was made: ffmpeg was not found",
        started,
        result=result,
        hint="Install ffmpeg, or set FLAME_MCP_FFMPEG to the path of an ffmpeg executable.",
    )


def _proxy_parts(
    op_args: Mapping[str, Any], ffmpeg: Ffmpeg, source: str, directory: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """The `original` / `proxy` parts of the result, the attachments and the notes."""
    inline = bool(op_args["inline"])
    limit = int(op_args["max_image_bytes"])
    proxy: Proxy = make_proxy(
        ffmpeg, source, directory, int(op_args["max_width"]), fit_bytes=limit if inline else None
    )
    parts: dict[str, Any] = {
        "original": {
            "path": source,
            "width": proxy.original_width,
            "height": proxy.original_height,
            "format": proxy.original_format,
        },
        "proxy": {
            "path": proxy.path,
            "width": proxy.width,
            "height": proxy.height,
            "bytes": proxy.bytes,
            "mimeType": PROXY_MIME,
        },
        "ffmpeg": {"path": ffmpeg.path},
    }
    attachments: list[dict[str, Any]] = []
    notes: list[str] = []
    if inline:
        if proxy.bytes <= limit:
            attachments.append({"path": proxy.path, "mimeType": PROXY_MIME, "bytes": proxy.bytes})
        else:
            notes.append(
                f"inline: the proxy is still {proxy.bytes} bytes at {proxy.width} px wide, over "
                f"max_image_bytes ({limit}), so it is not attached; the file is at {proxy.path}"
            )
    return parts, attachments, notes


def _run_path(
    op: RegisteredOperation, op_args: Mapping[str, Any], started: float
) -> dict[str, Any]:
    source = str(op_args["source"]["path"])
    ffmpeg = resolve_ffmpeg()
    if ffmpeg is None:
        return _ffmpeg_refusal(op, started)
    try:
        directory = make_run_dir()
    except OSError as exc:
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            f"nothing was made: the stills directory {stills_root()} cannot be written ({exc})",
            started,
            hint=f"Make it writable, or set {STILLS_DIR_ENV} to another directory.",
        )
    try:
        parts, attachments, notes = _proxy_parts(op_args, ffmpeg, source, directory)
    except (ProxyError, OSError) as exc:
        _drop_empty_dir(directory)
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            f"the proxy could not be made: {exc}",
            started,
            result={"ran": True, "source": {"path": source}, "original": {"path": source}},
            hint="The file is untouched. Check that ffmpeg can read it.",
        )
    result: dict[str, Any] = {
        "ran": True,
        "source": {"path": source},
        "frame": None,
        **parts,
        "exportElapsedS": None,
        "tempRemoved": None,
    }
    if attachments:
        result["attachments"] = attachments
    env = envelope_mod.build_envelope(
        ok=True,
        result=result,
        affected=1,
        execution=envelope_mod.execution_meta(op),
        duration_ms=elapsed_ms(started),
    )
    env["degraded"] = notes
    return env


def run_still(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `render.still`."""
    started = time.monotonic()
    source = op_args["source"]
    if "path" in source:
        return _run_path(op, op_args, started)
    frame = int(source["frame"])
    with rt.write_lock:
        denial = rt.guard.denial()
        if denial is not None:  # this call queued behind a write that ended unknown
            return envelope_mod.from_pre_bridge_error(
                error=denial.message,
                error_code="POLICY_DENIED",
                op=op,
                duration_ms=elapsed_ms(started),
                hint=denial.hint,
            )
        pre_env = execution.run_op(op, {**op_args, PHASE_ARG: PHASE_READ}, run_dir=rt.run_dir)
        raw = pre_env.get("result")
        if pre_env.get("ok") is not True or not isinstance(raw, dict):
            return pre_read_failure(op, pre_env, "made")
        facts: dict[str, Any] = raw
        stable = _stable_selector(facts, source["target"])
        refusal = _refuse_facts(op, facts, stable, frame, started)
        if refusal is not None:
            return refusal
        marks = still_marks(frame)
        preset = preset_path(str(facts.get("version") or ""))
        if preset is None:
            return _refusal(
                op,
                "PRECONDITION_UNMET",
                f"nothing was made: no export preset file for Flame {facts.get('version')!r} "
                f"under {os.environ.get(PRESETS_DIR_ENV) or DEFAULT_PRESETS_ROOT!r}",
                started,
                result={"ran": False, "target": target_info(facts, stable)},
                hint=f"Set {PRESETS_DIR_ENV} to the directory that holds Flame's presets "
                "(it has a folder per Flame version).",
            )
        return _still(rt, op, op_args, facts, stable, frame, marks, preset, group, started)


def _refuse_facts(
    op: RegisteredOperation,
    facts: Mapping[str, Any],
    stable: Any,
    frame: int,
    started: float,
) -> dict[str, Any] | None:
    """The refusals that need the read: the kind of the target and its frames."""
    kind = facts.get("type")
    info = {"ran": False, "target": target_info(facts, stable)}
    if kind != "PyClip":
        return _refusal(
            op,
            "SELECTOR_TYPE",
            f"nothing was made: the target is a {kind}, not a PyClip; this op takes clips only",
            started,
            result=info,
        )
    if stable is None:
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            "nothing was made: the target has no wiretap id, so it could not be found again "
            "for the export and for journal.check",
            started,
            result=info,
            hint="Give a wiretap or a path selector.",
        )
    duration = facts.get("duration")
    if not isinstance(duration, int) or not 1 <= frame <= duration:
        return _refusal(
            op,
            "INVALID_ARGS",
            f"nothing was made: frame must be 1 to {duration} for this clip, got {frame}",
            started,
            result=info,
        )
    if not facts.get("groupFound"):
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            "nothing was made: the Desktop has no reel group to hold the temporary copy",
            started,
            result=info,
        )
    return None


def _directory_refusal(
    op: RegisteredOperation,
    output_dir: str,
    before: Tree,
    info: dict[str, Any],
    started: float,
) -> dict[str, Any] | None:
    if "" in before.unreadable:
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            "nothing was made: the output directory could not be read, so it cannot be told "
            "whether it is empty",
            started,
            result=info,
            hint="Check the permissions of the directory, or use another one.",
        )
    if before.truncated:
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            "nothing was made: the output directory holds too many entries to tell afterwards "
            "what the export wrote",
            started,
            result=info,
            hint="Use an empty directory.",
        )
    existing = visible_entries(before)
    if existing:
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            f"nothing was made: the output directory is not empty (it holds {len(existing)} "
            "file(s) or folder(s) that are not hidden); this op writes only into an empty "
            "directory",
            started,
            result={**info, "existing": {"count": len(existing), "first": existing[:EXISTING_CAP]}},
            hint=(
                "The preset decides the names of the files and their folders, so this op cannot "
                "tell what it would overwrite. Use an empty directory, or leave output_dir out."
            ),
        )
    return None


def _still(
    rt: Runtime,
    op: RegisteredOperation,
    op_args: dict[str, Any],
    facts: dict[str, Any],
    stable: Any,
    frame: int,
    marks: tuple[int, int],
    preset: str,
    group: str | None,
    started: float,
) -> dict[str, Any]:
    timeout_s = float(op_args["timeout_s"])
    info = {"ran": False, "target": target_info(facts, stable)}
    user_dir = op_args.get("output_dir")
    issue = _first_non_ascii(
        target=stable,
        uid=facts.get("uid"),
        wiretapId=facts.get("wiretapId"),
        outputDir=user_dir or str(stills_root()),
        preset=preset,
    )
    if issue is not None:
        return _refusal(
            op,
            "INVALID_ARGS",
            f"nothing was made: {issue.path}: {issue.message}",
            started,
            result={"ran": False},
            hint=ASCII_REFUSAL_HINT,
        )
    if user_dir:
        before = scan_tree(user_dir, limit=EXPORT_SCAN_LIMIT)
        refused = _directory_refusal(op, user_dir, before, info, started)
        if refused is not None:
            return refused
    ffmpeg = resolve_ffmpeg()
    if ffmpeg is None:
        return _ffmpeg_refusal(op, started, info)
    try:
        run_dir = make_run_dir()
    except OSError as exc:
        return _refusal(
            op,
            "PRECONDITION_UNMET",
            f"nothing was made: the stills directory {stills_root()} cannot be written ({exc})",
            started,
            result=info,
            hint=f"Make it writable, or set {STILLS_DIR_ENV} to another directory.",
        )
    output_dir = str(user_dir or run_dir)
    if not user_dir:
        before = scan_tree(output_dir, limit=EXPORT_SCAN_LIMIT)
    plan = {
        "frame": frame,
        "marks": marks,
        "tempName": new_temp_name(),
        "preset": preset,
        "outputDir": output_dir,
        "runDir": str(run_dir),
    }
    group_id = group if group is not None else new_id("g")
    intent_id = new_id("i")
    client_handle = _new_client_handle(rt)
    try:
        rt.journal.append(
            _intent_record(
                op,
                op_args,
                facts,
                stable,
                before,
                plan,
                intent_id=intent_id,
                group_id=group_id,
                timeout_s=timeout_s,
                handle=client_handle,
            )
        )
    except OSError as exc:
        _drop_empty_dir(run_dir)
        return _refusal(
            op,
            "POLICY_DENIED",
            f"nothing was made: the journal cannot be written ({exc}), and this server does not "
            "make a still without an intent on record",
            started,
            result={"ran": False},
            hint=f"Make {rt.journal.directory} writable, then retry.",
        )
    run_args = {
        **op_args,
        "source": {"target": stable, "frame": frame},
        PHASE_ARG: PHASE_WRITE,
        EXPECTED_ARG: facts.get("uid"),
        STILL_ARG: {
            "reel": TEMP_REEL,
            "tempName": plan["tempName"],
            "marks": list(marks),
            "preset": preset,
            "outputDir": output_dir,
        },
    }
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
            reads="the output directory and the reel flame_mcp_still on the Desktop",
            noun="still",
        )
    finished = _finish(
        rt, op, env, op_args, facts, stable, ffmpeg, before, plan, intent_id, group_id, started
    )
    if client_handle is not None and end_code != "BRIDGE_AMBIGUOUS":
        _remove_result_file(rt, finished, client_handle)
    return finished


def _drop_empty_dir(directory: Path | str) -> None:
    try:
        os.rmdir(directory)
    except OSError:
        pass  # not empty, or already gone: it stays


def _copy_info(flame: Mapping[str, Any]) -> dict[str, Any] | None:
    """The reference copy as Flame read it right after making it (its name is the temporary
    one after a rename that worked, the clip's own after one that did not)."""
    if flame.get("copyUid") is None:
        return None
    return {
        "uid": flame.get("copyUid"),
        "name": flame.get("copyName"),
        "wiretapId": flame.get("copyWiretapId"),
    }


def _rows_text(rows: list[Any]) -> str:
    return "; ".join(
        f"name {row.get('name')!r}, uid {row.get('uid')}, wiretap id {row.get('wiretapId')}"
        for row in rows
        if isinstance(row, Mapping)
    )


def _leftover_notes(flame: Mapping[str, Any]) -> list[str]:
    """What this call may have left in Flame, with what a person needs to find it: a wiretap
    selector ({kind: 'wiretap', id}) or the uid addresses it for desktop.delete."""
    notes = [str(n) for n in flame.get("notes") or []]
    where = f"the reel {TEMP_REEL!r} of the first reel group of the Desktop"
    copy = _copy_info(flame)
    fresh = flame.get("freshCount")
    if copy is not None and flame.get("tempRemoved") is not True:
        notes.append(
            f"the temporary copy of the clip (name {copy['name']!r}, uid {copy['uid']}, wiretap "
            f"id {copy['wiretapId']}) may still be in {where}; delete it with desktop.delete "
            "(selector {kind: 'wiretap', id: <that wiretap id>}) if you do not need it"
        )
    elif isinstance(fresh, int) and fresh != 1:
        rows = flame.get("fresh") or []
        listed = f" ({_rows_text(rows)})" if rows else ""
        notes.append(
            f"the copy left {fresh} new clip(s) in {where}{listed}, not 1, so none was deleted; "
            "look at the reel and delete what is a leftover with desktop.delete"
        )
    if flame.get("tempReelCreated") and not flame.get("tempReelRemoved"):
        notes.append(
            f"the reel {TEMP_REEL!r} was made by this call and is still there (it is not empty, "
            "or it could not be deleted); delete it with desktop.delete if you do not need it"
        )
    return notes


def _finish(
    rt: Runtime,
    op: RegisteredOperation,
    env: dict[str, Any],
    op_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    stable: Any,
    ffmpeg: Ffmpeg,
    before: Tree,
    plan: Mapping[str, Any],
    intent_id: str,
    group_id: str,
    started: float,
) -> dict[str, Any]:
    flame_raw = env.get("result")
    flame: dict[str, Any] = flame_raw if isinstance(flame_raw, dict) else {}
    output_dir = str(plan["outputDir"])
    conflict = env.get("errorCode") == "CONFLICT"  # raised before anything was copied
    found = (
        NOTHING_WRITTEN
        if conflict
        else count_written(
            output_dir, before_stats_of(before), EXPORT_COUNT_DEPTH, EXPORT_COUNT_LIMIT
        )
    )
    written = found.count
    unsure = unsure_reason(found, 1)
    judged = judge_still(
        env_ok=env.get("ok") is True,
        env_code=env.get("errorCode"),
        env_error=env.get("error"),
        flame=flame,
        written=written,
        unsure=unsure,
    )
    result: dict[str, Any] = {
        "ran": True,
        "source": {"target": target_info(facts, stable), "frame": plan["frame"]},
        "frame": plan["frame"],
        "original": None,
        "proxy": None,
        "ffmpeg": {"path": ffmpeg.path},
        "outputDir": output_dir,
        "writtenCount": written,
        "countComplete": found.complete,
        "returned": flame.get("returned"),
        "exportElapsedS": flame.get("elapsedS"),
        "tempRemoved": flame.get("tempRemoved"),
        "tempReelCreated": flame.get("tempReelCreated"),
        "copy": _copy_info(flame),
        "undo": UNDO_NOTE,
    }
    degraded: list[str] = [*env.get("degraded", []), *_leftover_notes(flame), *judged.degraded]
    if not judged.ok and judged.code is not None:
        _fail_env(env, judged.code, judged.message or "")
    env["result"] = result
    env["affected"] = written
    env["undoSteps"] = 0
    env["partial"] = None
    env["journal"] = {"group": group_id, "id": intent_id, "undo": "unsupported", "revert": None}
    try:
        rt.journal.append(_outcome_record(intent_id, group_id, judged, flame, written, unsure))
    except OSError as exc:
        rt.guard.block(intent_id, group_id, op.name, "JOURNAL_OUTCOME_NOT_WRITTEN")
        degraded.append(
            f"the outcome could not be journaled ({exc}); the run's answer stands but this "
            "server now refuses writes until journal.check has settled the intent"
        )
    env["degraded"] = degraded
    if judged.ok and written == 1:
        _add_proxy(env, op_args, ffmpeg, plan, found.files[0])
    else:
        _drop_empty_dir(str(plan["runDir"]))  # the proxy would have gone there
    env["durationMs"] = elapsed_ms(started)
    return env


def _add_proxy(
    env: dict[str, Any],
    op_args: Mapping[str, Any],
    ffmpeg: Ffmpeg,
    plan: Mapping[str, Any],
    relative: str,
) -> None:
    """Make the proxy of the frame Flame wrote. Flame's work is done and journaled by now, so a
    proxy that cannot be made leaves the call ok, with the original named in a note: a new
    call with `{path}` makes the proxy alone, where calling again would export another TIFF and
    make another reference copy."""
    source = os.path.join(str(plan["outputDir"]), relative)
    run_dir = Path(str(plan["runDir"]))
    try:
        if str(plan["outputDir"]) == str(plan["runDir"]):
            make_private_file(source)
        parts, attachments, notes = _proxy_parts(op_args, ffmpeg, source, run_dir)
    except (ProxyError, OSError) as exc:
        env["result"]["original"] = {"path": source, "width": None, "height": None, "format": None}
        env["degraded"].append(
            f"the proxy could not be made ({exc}). The original is at {source}; call again with "
            "a {path} source naming that file to make only the proxy"
        )
        _drop_empty_dir(run_dir)
        return
    env["result"].update(parts)
    if attachments:
        env["result"]["attachments"] = attachments
    env["degraded"].extend(notes)
