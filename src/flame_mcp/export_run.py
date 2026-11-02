"""`render.export`, server side: a clip or sequence written to files by `PyExporter.export`.

A write-ahead pair like `render.clip` (`render_clip_run.py`), whose shared parts it uses (the
client handle, `_unknown_end`, the result file clean-up). What it wrote is judged from the disk,
not from Flame: the output directory is listed (`render_files.scan_tree`, a few levels down,
because a preset may put its files into a sub-folder) before and after.

  1. read       one Flame call (`_ex_inspect`): type, name, uid, wiretap id, duration, place. A
                target that is not a PyClip / PySequence ends the call here (SELECTOR_TYPE).
  2. check      the output directory is listed. A directory that cannot be read is refused, not
                taken for empty. Anything visible in it (hidden names -- `.DS_Store` -- and what
                is inside a hidden folder do not count) is PRECONDITION_UNMET: the preset decides
                the names of the files, so this op writes only into an empty directory and can
                never overwrite.
  3. intent     `kind: "export"`: the stable selector, the preset, the directory as it was
                (`directoryBefore`), the frames an image sequence is to have (`expectedFrames`)
                and the client handle, journaled before the bridge gets the call.
  4. export     one `main-async` Flame call (`_ex_export`): the uid is compared with the one read,
                the directory is listed again (something that appeared since is CONFLICT), then
                `PyExporter.export` runs in the Foreground. It waits up to `timeout_s`.
  5. outcome    the directory is listed again, deeper than before (`count_written`); the files
                that are new and not hidden are what the export wrote (`judge_export`): none =
                SILENT_NOOP, an exception = FLAME_API_ERROR, fewer than `expectedFrames` =
                FLAME_API_ERROR starting `partial:`. A listing cut short (the entry limit, or a
                folder that is not hidden and cannot be read) cannot settle a count of 0 or a
                short one (`unsure_reason`): ok with a note.

A call that ends without an answer (TIMEOUT / BRIDGE_LOST / a `pending` handle) leaves the intent
open and closes the write guard (`WriteGuard`; writes are refused until `journal.check` has
decided, `export_check.check_export_intent`).

There is no undo: the files stay on disk.
"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import errors, execution
from flame_mcp.checkpointed import ASCII_REFUSAL_HINT, _first_non_ascii
from flame_mcp.colour_check import judge as judge_colour
from flame_mcp.colour_check import problem_of
from flame_mcp.desktop_run import (
    EXPECTED_ARG,
    PHASE_ARG,
    PHASE_READ,
    PHASE_WRITE,
    pre_read_failure,
)
from flame_mcp.journal import STATE_LANDED, STATE_NOT_LANDED, STATE_PARTIAL, new_id, now_iso
from flame_mcp.journaled_write import UNKNOWN_END_CODES
from flame_mcp.registry import RegisteredOperation
from flame_mcp.render_clip_run import MEDIA_TYPES, _stable_selector, target_info
from flame_mcp.render_files import (
    EXPORT_COUNT_DEPTH,
    EXPORT_COUNT_LIMIT,
    EXPORT_SCAN_LIMIT,
    NAME_LIST_CAP,
    Stat,
    Tree,
    is_hidden,
    scan_tree,
    visible_entries,
    visible_folders,
    written_files,
)
from flame_mcp.render_run import (
    HANDLE_SOURCE_CLIENT,
    RenderJudgement,
    _fail_env,
    _new_client_handle,
    _refusal,
    _remove_result_file,
    _unknown_end,
    frames_verdict,
)
from flame_mcp.transport import elapsed_ms
from flame_mcp.types import ErrorCode

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

OP_NAME: Final[str] = "render.export"
EXPORT_KIND: Final[str] = "export"
"""`kind` of a render.export intent in the journal (`journal.check` dispatches on it)."""
EXISTING_CAP: Final[int] = 10
"""How many of the entries that make the output directory not empty `result.existing` names."""
PRESET_MAX_BYTES: Final[int] = 1_000_000
IMAGE_PRESET_TYPE: Final[str] = "image"
"""The `<type>` of an image-sequence preset: the only kind whose file count is known (one per
frame)."""
UNDO_NOTE: Final[str] = "unsupported: the exported files stay on disk"


@dataclass(frozen=True)
class PresetFacts:
    """What an export preset says that decides how many files an export writes: its `<type>` (None
    when the file is too big, is not XML or has none) and whether it writes one poster frame."""

    type: str | None
    poster_frame: bool


_TRUE_TEXT: Final[frozenset[str]] = frozenset({"true", "1", "yes"})
_POSTER_TAGS: Final[tuple[str, ...]] = ("posterFrame", "useFrameAsPoster")


def preset_facts(preset: str) -> PresetFacts:
    try:
        if os.path.getsize(preset) > PRESET_MAX_BYTES:
            return PresetFacts(None, False)
        root = ET.parse(preset).getroot()
    except (OSError, ET.ParseError):
        return PresetFacts(None, False)
    text = (root.findtext("type") or "").strip()
    poster = any(
        (element.text or "").strip().lower() in _TRUE_TEXT
        for tag in _POSTER_TAGS
        for element in root.iter(tag)
    )
    return PresetFacts(text or None, poster)


def preset_type(preset: str) -> str | None:
    """The `<type>` element of an export preset (`image`, `movie`, `audio` ...)."""
    return preset_facts(preset).type


def expected_frames(preset: str, duration: Any, between_marks: bool) -> int | None:
    """The number of files an export is to write: the duration for an image-sequence preset, one
    file per frame. None when it cannot be told: another kind of preset, a preset that writes one
    poster frame, a range between marks, an unreadable preset or duration."""
    if between_marks or not isinstance(duration, int) or duration <= 0:
        return None
    facts = preset_facts(preset)
    return duration if facts.type == IMAGE_PRESET_TYPE and not facts.poster_frame else None


@dataclass(frozen=True)
class Written:
    """What an export wrote, by `count_written`: `files` the relative paths of the files that are
    new and not hidden, `folders` the folders that are not hidden, `tree` the listing they come
    from (`complete` False when it was cut short)."""

    files: list[str]
    folders: list[str]
    tree: Tree

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def complete(self) -> bool:
        return not self.tree.incomplete


NOTHING_WRITTEN: Final[Written] = Written([], [], Tree({}, (), (), (), False))


def count_written(
    output_dir: str,
    before: Mapping[str, Stat],
    depth: int,
    limit: int | None,
) -> Written:
    """The files below `output_dir` (down to `depth` levels, at most `limit` entries) that are new
    or changed since `before`, hidden ones left out."""
    tree = scan_tree(output_dir, depth, limit)
    return Written(written_files(before, tree), visible_folders(tree), tree)


def unsure_reason(written: Written, expected: int | None) -> str | None:
    """Why the files found cannot settle whether the export wrote what it should, or None when
    they can. Only a count of 0, or one short of `expected`, needs settling, and it cannot be when
    the listing was cut short (the limit was reached, or a folder that is not hidden cannot be
    read): the count is then a lower bound. Folders with no file in them are not a reason:
    the count goes deeper than any preset writes, so it is an export that only made folders."""
    if written.count > 0 and (expected is None or written.count >= expected):
        return None
    if written.complete:
        return None
    return (
        f"the output directory could not be counted completely ({written.count} files found; "
        "it holds more entries than the count reads, or a folder in it cannot be read), so the "
        "count is a lower bound"
    )


def judge_export(
    *,
    env_ok: bool,
    env_code: Any,
    env_error: Any,
    flame: Mapping[str, Any],
    written: int,
    expected: int | None,
    unsure: str | None = None,
) -> RenderJudgement:
    """What a finished export counts as, from Flame's answer (`env_*`, the op's own value `flame`)
    and the files found (`unsure`: `unsure_reason`).

    The rule is the same for `render.export` and for a `journal.check` that got the answer through
    the bridge's poll. An error of the export stays an error, whatever is on disk. An export that
    returned without an error but wrote nothing is SILENT_NOOP (FLAME_API_ERROR when it returned
    False). Fewer files than `expected` is FLAME_API_ERROR starting `partial:` (the files stay;
    `journal.check` reads it as partial too). A False return with the files there is a success
    with a note. When `unsure` says the files cannot settle a count of 0 or one short of
    `expected`, the export is neither SILENT_NOOP nor partial: it is a success with `unsure` as
    the note -- unless it returned False, which stays FLAME_API_ERROR."""
    if not env_ok:
        return RenderJudgement(False, errors.as_error_code(str(env_code)), str(env_error))
    returned = flame.get("returned")
    if unsure is not None and (written == 0 or (expected is not None and written < expected)):
        if returned is False:
            return RenderJudgement(
                False,
                "FLAME_API_ERROR",
                f"export returned False and only {written} file(s) were counted ({unsure})",
            )
        return RenderJudgement(True, None, None, [unsure])
    if written == 0:
        code: ErrorCode = "FLAME_API_ERROR" if returned is False else "SILENT_NOOP"
        return RenderJudgement(
            False,
            code,
            f"export returned {returned!r} without an exception but no file appeared in the "
            "output directory",
        )
    if expected is not None and written < expected:
        return RenderJudgement(
            False,
            "FLAME_API_ERROR",
            f"partial: export returned {returned!r} without an exception but only {written} of "
            f"{expected} files (one per frame) were written",
        )
    degraded: list[str] = []
    if returned is False:
        degraded.append(f"export returned {returned!r}, not True; the files are on disk")
    return RenderJudgement(True, None, None, degraded)


def outcome_verdict(judged: RenderJudgement, written: int, unsure: str | None) -> str | None:
    """None when the export was called ok although the files could not settle it (`unsure`)."""
    if judged.ok:
        return None if unsure is not None else STATE_LANDED
    return STATE_PARTIAL if written > 0 else STATE_NOT_LANDED


def disk_verdict(written: int, expected: int | None) -> str:
    """What the files alone say: every expected file (any file when the count is not known) =
    landed, some = partial, none = not_landed."""
    if expected is None:
        return STATE_LANDED if written > 0 else STATE_NOT_LANDED
    return frames_verdict(written, expected) or STATE_NOT_LANDED


def before_stats_of(tree: Tree) -> dict[str, Stat]:
    return dict(tree.files)


def before_stats(intent: Mapping[str, Any]) -> dict[str, Stat]:
    return {
        str(name): (int(stat[0]), int(stat[1]))
        for name, stat in (intent.get("directoryBefore") or {}).items()
    }


def file_rows(written: Written) -> tuple[list[dict[str, Any]], bool]:
    shown = [
        {"name": name, "size": written.tree.files[name][0]}
        for name in written.files[:NAME_LIST_CAP]
    ]
    return shown, len(written.files) > NAME_LIST_CAP


def directory_before(tree: Tree) -> tuple[dict[str, list[int]], int]:
    """What the intent records of the files in the directory: every file that is not hidden (what
    the judgement compares with; an export only starts into a directory with none) and the names
    of the first `NAME_LIST_CAP` hidden ones, which no judgement counts; and how many hidden files
    there were, so a directory full of them cannot swell the journal."""
    hidden = sorted(name for name in tree.files if is_hidden(name))
    kept = [name for name in tree.files if not is_hidden(name)] + hidden[:NAME_LIST_CAP]
    return {name: list(tree.files[name]) for name in kept}, len(hidden)


def _intent_record(
    op: RegisteredOperation,
    op_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    stable: Any,
    before: Tree,
    expected: int | None,
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
        "kind": EXPORT_KIND,
        "args": dict(op_args),
        "target": stable,
        "targetType": facts.get("type"),
        "name": facts.get("name"),
        "uid": facts.get("uid"),
        "preset": op_args["preset"],
        "outputDir": op_args["output_dir"],
        "betweenMarks": op_args["between_marks"],
        "directoryBefore": before_files,
        "hiddenBefore": hidden_count,
        "expectedFrames": expected,
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
    expected: int | None,
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
        "expectedFrames": expected,
        "writtenCount": written,
        "countUnsure": unsure,
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
    }


def _existing_refusal(
    op: RegisteredOperation,
    existing: list[str],
    facts: Mapping[str, Any],
    stable: Any,
    started: float,
) -> dict[str, Any]:
    return _refusal(
        op,
        "PRECONDITION_UNMET",
        f"nothing was exported: the output directory is not empty (it holds {len(existing)} "
        "file(s) or folder(s) that are not hidden); this op writes only into an empty directory",
        started,
        result={
            "ran": False,
            "target": target_info(facts, stable),
            "existing": {"count": len(existing), "first": existing[:EXISTING_CAP]},
        },
        hint=(
            "The preset decides the names of the files and their folders, so this op cannot tell "
            "what it would overwrite. Make a new, empty directory for each export."
        ),
    )


def run_export(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `render.export`."""
    started = time.monotonic()
    timeout_s = float(op_args["timeout_s"])
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
            return pre_read_failure(op, pre_env, "exported")
        facts: dict[str, Any] = raw
        stable = _stable_selector(facts, op_args["target"])
        kind = facts.get("type")
        if kind not in MEDIA_TYPES:
            return _refusal(
                op,
                "SELECTOR_TYPE",
                f"nothing was exported: the target is a {kind}, not a PyClip / PySequence",
                started,
                result={"ran": False, "target": target_info(facts, stable)},
            )
        if stable is None:
            return _refusal(
                op,
                "PRECONDITION_UNMET",
                "nothing was exported: the target has no wiretap id, so it could not be found "
                "again for the export and for journal.check",
                started,
                result={"ran": False, "target": target_info(facts, stable)},
                hint="Give a wiretap or a path selector.",
            )
        colour = judge_colour(facts.get("colourScan")) if kind == "PySequence" else None
        colour_problem = problem_of(colour) if colour is not None else None
        skipped = bool(op_args.get("skip_colour_check", False)) and colour_problem is not None
        if colour_problem is not None and not skipped:
            return _refusal(
                op,
                "PRECONDITION_UNMET",
                colour_problem,
                started,
                result={"ran": False, "target": target_info(facts, stable), "colourSpaces": colour},
                hint=(
                    "Nothing was exported or journaled. timeline.colour_spaces lists where each "
                    "colour space is; skip_colour_check=true exports anyway."
                ),
            )
        issue = _first_non_ascii(
            target=stable, uid=facts.get("uid"), wiretapId=facts.get("wiretapId")
        )
        if issue is not None:
            return _refusal(
                op,
                "INVALID_ARGS",
                f"nothing was exported: {issue.path}: {issue.message}",
                started,
                result={"ran": False},
                hint=ASCII_REFUSAL_HINT,
            )

        before = scan_tree(op_args["output_dir"], limit=EXPORT_SCAN_LIMIT)
        if "" in before.unreadable:
            return _refusal(
                op,
                "PRECONDITION_UNMET",
                "nothing was exported: the output directory could not be read, so it cannot be "
                "told whether it is empty",
                started,
                result={"ran": False, "target": target_info(facts, stable)},
                hint="Check the permissions of the directory, or use another one.",
            )
        if before.truncated:
            return _refusal(
                op,
                "PRECONDITION_UNMET",
                "nothing was exported: the output directory holds too many entries to tell "
                "afterwards what the export wrote",
                started,
                result={"ran": False, "target": target_info(facts, stable)},
                hint="Use an empty or smaller directory.",
            )
        existing = visible_entries(before)
        if existing:
            return _existing_refusal(op, existing, facts, stable, started)

        expected = expected_frames(
            op_args["preset"], facts.get("duration"), bool(op_args["between_marks"])
        )
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
                    expected,
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
                f"nothing was exported: the journal cannot be written ({exc}), and this server "
                "does not export without an intent on record",
                started,
                result={"ran": False},
                hint=f"Make {rt.journal.directory} writable, then retry.",
            )

        run_args = {
            **op_args,
            "target": stable,
            PHASE_ARG: PHASE_WRITE,
            EXPECTED_ARG: facts.get("uid"),
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
                reads="the output directory",
                noun="export",
            )
        finished = _finish(
            rt, op, env, op_args, facts, stable, before, expected, intent_id, group_id, started
        )
        if client_handle is not None and end_code != "BRIDGE_AMBIGUOUS":
            _remove_result_file(rt, finished, client_handle)
        return _with_colour_notes(finished, colour, colour_problem if skipped else None)


def _with_colour_notes(
    env: dict[str, Any], colour: dict[str, Any] | None, skipped_problem: str | None
) -> dict[str, Any]:
    """Keep the colour check in the result, and say in `degraded` what was skipped
    (`skip_colour_check`) and what could not be read."""
    if colour is None:
        return env
    if isinstance(env.get("result"), dict):
        env["result"]["colourSpaces"] = colour
    if skipped_problem is not None:
        env["degraded"] = [
            *env.get("degraded", []),
            f"colour check skipped (skip_colour_check): {skipped_problem}",
        ]
    if colour["warnings"]:
        env["degraded"] = [
            *env.get("degraded", []),
            *(f"colour spaces: {w}" for w in colour["warnings"]),
        ]
    return env


def _finish(
    rt: Runtime,
    op: RegisteredOperation,
    env: dict[str, Any],
    op_args: Mapping[str, Any],
    facts: Mapping[str, Any],
    stable: Any,
    before: Tree,
    expected: int | None,
    intent_id: str,
    group_id: str,
    started: float,
) -> dict[str, Any]:
    flame_raw = env.get("result")
    flame: dict[str, Any] = flame_raw if isinstance(flame_raw, dict) else {}
    conflict = env.get("errorCode") == "CONFLICT"  # raised before the export was called
    found = (
        NOTHING_WRITTEN
        if conflict
        else count_written(
            op_args["output_dir"], before_stats_of(before), EXPORT_COUNT_DEPTH, EXPORT_COUNT_LIMIT
        )
    )
    written = found.count
    unsure = unsure_reason(found, expected)
    judged = judge_export(
        env_ok=env.get("ok") is True,
        env_code=env.get("errorCode"),
        env_error=env.get("error"),
        flame=flame,
        written=written,
        expected=expected,
        unsure=unsure,
    )
    shown, truncated = file_rows(found)
    result: dict[str, Any] = {
        "ran": True,
        "target": target_info(facts, stable),
        "preset": op_args["preset"],
        "outputDir": op_args["output_dir"],
        "betweenMarks": op_args["between_marks"],
        "files": shown,
        "filesTruncated": truncated,
        "writtenCount": written,
        "expectedFrames": expected,
        "countComplete": found.complete,
        "newFolders": found.folders[:EXISTING_CAP],
        "returned": flame.get("returned"),
        "elapsedS": flame.get("elapsedS"),
        "undo": UNDO_NOTE,
    }
    if not judged.ok and judged.code is not None:
        _fail_env(env, judged.code, judged.message or "")
    env["result"] = result
    env["affected"] = written
    env["undoSteps"] = 0
    env["partial"] = (
        {"written": written, "expected": expected}
        if expected is not None and written < expected and not judged.ok and not conflict
        else None
    )
    env["degraded"] = [*env.get("degraded", []), *judged.degraded]
    env["journal"] = {"group": group_id, "id": intent_id, "undo": "unsupported", "revert": None}
    env["durationMs"] = elapsed_ms(started)
    try:
        rt.journal.append(
            _outcome_record(intent_id, group_id, judged, flame, written, expected, unsure)
        )
    except OSError as exc:
        rt.guard.block(intent_id, group_id, op.name, "JOURNAL_OUTCOME_NOT_WRITTEN")
        env["degraded"] = [
            *env["degraded"],
            f"the outcome could not be journaled ({exc}); the run's answer stands but this "
            "server now refuses writes until journal.check has settled the intent",
        ]
    return env
