"""Batch checkpoints, server side.

A checkpoint is a Batch group's setup saved with `PyBatch.save_setup`, plus a `checkpoint`
record in the journal: the id, the group's uid and name, the path `save_setup` really wrote
(it rewrites the path it is given), and the group's fingerprint (a hash of its
nodes and wiring, read-only to compute) at that moment.

The record also carries the group's schematic reel inventory (`reels`: name, empty or not, clip and
sequence names, in order) and the fingerprint over all sections with its data (`fingerprintFull`,
`sectionData`), read right before `save_setup`: what a restore needs to put the reels back
(`reel_fidelity`). A record without `reels` is an older checkpoint. It also carries `clipFx`: the
Timeline FX types of every reel clip, which a restore compares with
(`timeline_fx_loss`); a record without it is an older checkpoint too.

`batch.checkpoint` runs in Flame (`checkpoint_prelude._save_checkpoint`); the server picks
the file name and directory, and records what came back. The directory is
`<journal directory>/checkpoints/` (so a test that gives the journal a temp directory gets
its checkpoints there too). File names carry no dot: `save_setup` turns dots into
underscores, and the glob that finds the real path is simpler without any.

Files are not cleaned up here: cleanup is a human, explicit act. The exception is the work
directory of `batch.compare_setup` (`run_compare_setup`), which only exists for one call (it stays
after BRIDGE_LOST and after a TIMEOUT that Flame may have started).
"""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution, setup_compare, setup_edit, setup_work
from flame_mcp.journal import Journal, new_id, now_iso
from flame_mcp.private_fs import make_private_dirs
from flame_mcp.registry import RegisteredOperation
from flame_mcp.transport import elapsed_ms
from flame_mcp.unrecorded_end import shut_if_unanswered

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime, ServerContext
    from flame_mcp.types import ErrorCode

CHECKPOINT_TIMEOUT_S: Final[float] = 30.0
"""`save_setup` / `load_setup` of a big Batch take longer than an attribute write."""
CHECKPOINT_DIR_NAME: Final[str] = "checkpoints"
FILE_PREFIX: Final[str] = "flame_mcp_"
DEFAULT_FINGERPRINT_SECTIONS: Final[tuple[str, ...]] = ()
"""Which optional parts of a Batch group its fingerprint covers besides the nodes, the wiring
and the inner nodes of Action / Image / GMask Tracer nodes: any of `FINGERPRINT_SECTIONS`.
Empty, because a restore does not bring back every reel, shelf reel and iteration as recorded.
Every checkpoint record carries the sections it was taken with
(`fingerprintSections`); fingerprints taken with different sections do not compare equal."""
FINGERPRINT_SECTIONS: Final[tuple[str, ...]] = (
    "reels",
    "shelf_reels",
    "iterations",
    "attributes",
)
ATTRIBUTES_SECTION: Final[str] = "attributes"
FX_ARG: Final[str] = "_clip_fx"
"""Reserved args key for `batch.load_setup`'s `to_python` (restore path only): when true, the clip
FX of the reels are read right after `load_setup` (`timeline_fx_loss`)."""
REELS_ARG: Final[str] = "_reels"
"""Reserved args key carrying a checkpoint's recorded reel inventory into `batch.load_setup`'s
`to_python` (restore path only): with it, the reels are tidied right after `load_setup`."""
DIR_ARG: Final[str] = "_dir"
BASE_ARG: Final[str] = "_base"
"""Reserved args keys carrying the directory and file stem into `to_python`. They are not
declared parameters, so `validate_op_args` rejects them from a caller."""
COMPARE_DIR_PREFIX: Final[str] = "cmp"
"""The work directory of one `batch.compare_setup` call is `<journal dir>/setups/cmp_<12 hex>`."""
COMPARE_BASE: Final[str] = "setup"
"""The name `batch.compare_setup` gives `save_setup`; the `.batch` it wrote is found by what the
directory holds."""
COMPARE_KEPT_CODES: Final[frozenset[str]] = frozenset({"TIMEOUT", "BRIDGE_LOST"})
"""After these the work directory is left where it is: Flame may still be writing into it. A
TIMEOUT whose `timeoutPhase` is `queued` is the exception: Flame never started the call, so the
directory is removed."""
QUEUED_PHASE: Final[str] = "queued"
_COMPARE_DIR_NAME: Final[re.Pattern[str]] = re.compile(rf"{COMPARE_DIR_PREFIX}_[0-9a-f]{{12}}")
_SECONDS_PER_DAY: Final[int] = 86400


@dataclass(frozen=True)
class CheckpointKind:
    """What a checkpoint is of: the ops the engine calls (through `ctx.do`) to take it and to read
    the fingerprint, and what it calls the thing in messages. A Batch group is checkpointed as a
    setup file (`batch.checkpoint`); a sequence / clip on the Desktop as a copy in a stash reel
    (`timeline.checkpoint`)."""

    name: str
    checkpoint_op: str
    fingerprint_op: str
    subject: str

    @property
    def needed_ops(self) -> tuple[str, ...]:
        return (self.checkpoint_op, self.fingerprint_op)


BATCH_KIND: Final[CheckpointKind] = CheckpointKind(
    "batch", "batch.checkpoint", "batch.fingerprint", "group"
)
TIMELINE_KIND: Final[CheckpointKind] = CheckpointKind(
    "timeline", "timeline.checkpoint", "timeline.fingerprint", "sequence"
)
KINDS: Final[Mapping[str, CheckpointKind]] = {k.name: k for k in (BATCH_KIND, TIMELINE_KIND)}


def kind_named(name: Any) -> CheckpointKind:
    """The kind a journal record names (`targetKind` of an intent, `kind` of a checkpoint record);
    a record without one is a Batch group's."""
    return KINDS.get(str(name), BATCH_KIND) if name is not None else BATCH_KIND


def checkpoint_dir(journal: Journal) -> Path:
    return journal.directory / CHECKPOINT_DIR_NAME


def batch_target(uid: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid}


def wiretap_target(wiretap_id: str) -> dict[str, Any]:
    return {"kind": "wiretap", "id": wiretap_id}


def run_checkpoint(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` handler of `batch.checkpoint`: run the save in Flame, then journal the
    checkpoint record (the file alone would be a setup nothing can find again)."""
    started = time.monotonic()
    checkpoint_id = new_id("c")
    flame_args = {
        **op_args,
        DIR_ARG: str(checkpoint_dir(rt.journal)),
        BASE_ARG: f"{FILE_PREFIX}{checkpoint_id}",
    }
    with rt.write_lock:
        env = execution.run_op(op, flame_args, run_dir=rt.run_dir, timeout_s=CHECKPOINT_TIMEOUT_S)
        if env.get("ok") is not True:
            shut_if_unanswered(rt.guard, env, op.name, group)
            return env
        raw = env.get("result")
        result: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        result.pop("error", None)
        record: dict[str, Any] = {
            "type": "checkpoint",
            "id": checkpoint_id,
            "group": group,
            "ts": now_iso(),
            "batch": result.get("batch"),
            "label": op_args.get("label"),
            "path": result.get("path"),
            "size": result.get("size"),
            "fingerprint": result.get("fingerprint"),
            "fingerprintSections": result.get("fingerprintSections"),
            "nodeCount": result.get("nodeCount"),
            "connectionCount": result.get("connectionCount"),
            "unreadableNodes": result.get("unreadableNodes"),
            "reels": result.get("reels"),
            "reelsError": result.get("reelsError"),
            "clipFx": result.pop("clipFx", None),
            "clipFxError": result.get("clipFxError"),
            "fingerprintFull": result.get("fingerprintFull"),
            "fingerprintFullSections": result.get("fingerprintFullSections"),
            "sectionData": result.pop("sectionData", None),
        }
        result["checkpoint"] = checkpoint_id
        result["label"] = op_args.get("label")
        try:
            rt.journal.append(record)
        except OSError as exc:
            return envelope_mod.build_envelope(
                ok=False,
                result=result,
                error=(
                    f"the setup was saved at {result.get('path')} but the checkpoint could not "
                    f"be recorded in the journal ({exc}), so it cannot be restored by id"
                ),
                error_code="POLICY_DENIED",
                execution=envelope_mod.execution_meta(op),
                duration_ms=elapsed_ms(started),
                hint_override=(
                    f"Make {rt.journal.directory} writable, then take the checkpoint again."
                ),
            )
        env["result"] = result
        env["journal"] = {"group": group, "id": checkpoint_id, "kind": "checkpoint"}
        return env


def take_checkpoint(
    ctx: ServerContext,
    target: Mapping[str, Any],
    *,
    label: str,
    group: str | None,
    kind: CheckpointKind = BATCH_KIND,
) -> dict[str, Any]:
    """`batch.checkpoint` (or the kind's own) through the normal do path; the envelope."""
    return ctx.do(kind.checkpoint_op, {"target": dict(target), "label": label}, group=group)


def read_fingerprint(
    ctx: ServerContext,
    target: Mapping[str, Any],
    kind: CheckpointKind = BATCH_KIND,
    *,
    sections: Sequence[str] | None = None,
) -> dict[str, Any]:
    """`batch.fingerprint` (or the kind's own) through the normal do path; the envelope. With
    `sections` (a Batch group's), the hash covers those parts too (the server's default
    otherwise)."""
    args: dict[str, Any] = {"target": dict(target)}
    if sections is not None:
        args["sections"] = list(sections)
    return ctx.do(kind.fingerprint_op, args)


def stable_subject(
    kind: CheckpointKind, pre: Mapping[str, Any]
) -> tuple[dict[str, Any], Any] | None:
    """(the selector that finds the checkpointed thing again, its uid) out of a checkpoint's
    result, None when it cannot be found again: a Batch group by its uid, a sequence / clip by its
    wiretap id."""
    if kind is TIMELINE_KIND:
        source = pre.get("source")
        if not isinstance(source, dict):
            return None
        wiretap = source.get("wiretapId")
        if not isinstance(wiretap, str) or not wiretap:
            return None
        return wiretap_target(wiretap), source.get("uid")
    batch = pre.get("batch")
    uid = batch.get("uid") if isinstance(batch, dict) else None
    return (batch_target(uid), uid) if isinstance(uid, str) else None


def intent_fields(kind: CheckpointKind, pre: Mapping[str, Any], uid: Any) -> dict[str, Any]:
    """What an intent of a checkpointed write records about the checkpoint beyond the common
    keys. A Batch intent records the checkpoint's path and node count."""
    if kind is TIMELINE_KIND:
        return {
            "targetKind": kind.name,
            "targetUid": uid,
            "targetIdent": pre.get("source"),
            "stash": pre.get("stash"),
            "segmentCountBefore": pre.get("segmentCount"),
        }
    return {
        "checkpointPath": pre.get("path"),
        "nodeCountBefore": pre.get("nodeCount"),
    }


def outcome_fields(kind: CheckpointKind, after_env: Mapping[str, Any]) -> dict[str, Any]:
    """What the outcome of a checkpointed write records beyond the common keys: for a sequence /
    clip, who it is after the run (the uid a later revert looks for)."""
    if kind is not TIMELINE_KIND:
        return {}
    result = after_env.get("result") if after_env.get("ok") is True else None
    ident = result.get("target") if isinstance(result, dict) else None
    if not isinstance(ident, dict):
        return {"targetUid": None, "targetIdent": None}
    return {"targetUid": ident.get("uid"), "targetIdent": ident}


def fingerprint_of(env: Mapping[str, Any]) -> str | None:
    """The fingerprint out of a `batch.fingerprint` (or `batch.checkpoint`) envelope."""
    if env.get("ok") is not True:
        return None
    result = env.get("result")
    value = result.get("fingerprint") if isinstance(result, dict) else None
    return value if isinstance(value, str) and value else None


def sweep_compare_dirs(root: Path) -> None:
    """Remove the work directories of `batch.compare_setup` under `root` that are older than
    `setup_edit.WORKDIR_MAX_AGE_DAYS`: only a real directory (not a link) whose name is
    `cmp_<12 hex>`. A directory that cannot be removed is left."""
    cutoff = time.time() - setup_edit.WORKDIR_MAX_AGE_DAYS * _SECONDS_PER_DAY
    try:
        names = [entry.name for entry in root.iterdir()]
    except OSError:
        return
    for name in names:
        path = root / name
        if _COMPARE_DIR_NAME.fullmatch(name) is None:
            continue
        try:
            if path.is_symlink() or not path.is_dir() or path.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(path)
        except OSError:
            continue


def _remove_work_dir(workdir: Path) -> str | None:
    try:
        shutil.rmtree(workdir)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _note_cleanup_error(env: dict[str, Any], problem: str) -> None:
    result = env.get("result")
    if isinstance(result, dict):
        result["cleanupError"] = problem
    elif result is None:
        env["result"] = {"cleanupError": problem}


def _keeps_work_dir(env: Mapping[str, Any]) -> bool:
    code = env.get("errorCode")
    if code == "TIMEOUT":
        return env.get("timeoutPhase") != QUEUED_PHASE
    return code in COMPARE_KEPT_CODES


def _compare_failure(
    op: RegisteredOperation,
    started: float,
    result: Any,
    message: str,
    error_code: ErrorCode = "FLAME_API_ERROR",
) -> dict[str, Any]:
    return envelope_mod.build_envelope(
        ok=False,
        result=result,
        error=message,
        error_code=error_code,
        execution=envelope_mod.execution_meta(op),
        duration_ms=elapsed_ms(started),
    )


def _compared(
    op: RegisteredOperation,
    env: dict[str, Any],
    baseline: str,
    workdir: Path,
    started: float,
) -> dict[str, Any]:
    raw = env.get("result")
    saved: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    saved.pop("error", None)
    saved.pop("saveSetupReturned", None)
    try:
        written = sorted(p for p in workdir.iterdir() if p.suffix == ".batch" and p.is_file())
    except OSError as exc:
        return _compare_failure(op, started, saved, f"the work directory could not be read ({exc})")
    if len(written) != 1:
        return _compare_failure(
            op,
            started,
            saved,
            f"save_setup wrote no single .batch file in the work directory; found "
            f"{[p.name for p in written]}",
        )
    try:
        compared = setup_compare.compare_setups(Path(baseline), written[0])
    except OSError as exc:
        return _compare_failure(
            op,
            started,
            saved,
            f"a setup file could not be read ({exc}); the baseline may have been moved or removed",
            "PRECONDITION_UNMET",
        )
    env["result"] = {"baseline": baseline, **saved, **compared}
    return env


def run_compare_setup(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` handler of `batch.compare_setup`: Flame saves the group's setup into a work
    directory the server made (one `save_setup`), and the comparison with `baseline` runs here, so
    Flame's main thread is not held for it. A read: no journal record, no write lock. The work
    directory is removed before the call ends, except after BRIDGE_LOST and after a TIMEOUT that was
    not `queued`, when Flame may still be writing into it (a later call removes it once it is old
    enough). A work directory that cannot be made, or a baseline that cannot be read, ends the call
    in an envelope and not in an exception."""
    started = time.monotonic()
    root = Path(setup_work.work_root())
    sweep_compare_dirs(root)
    workdir = root / new_id(COMPARE_DIR_PREFIX)
    try:
        make_private_dirs(workdir)
    except OSError as exc:
        return _compare_failure(
            op,
            started,
            None,
            f"nothing was sent to Flame: the work directory could not be made ({exc})",
            "POLICY_DENIED",
        )
    baseline = str(op_args["baseline"])
    flame_args = {
        **{key: value for key, value in op_args.items() if key != "baseline"},
        DIR_ARG: str(workdir),
        BASE_ARG: COMPARE_BASE,
    }
    keep = False
    env: dict[str, Any] | None = None
    try:
        env = execution.run_op(op, flame_args, run_dir=rt.run_dir, timeout_s=CHECKPOINT_TIMEOUT_S)
        if env.get("ok") is True:
            env = _compared(op, env, baseline, workdir, started)
        else:
            keep = _keeps_work_dir(env)
        return env
    finally:
        if not keep:
            problem = _remove_work_dir(workdir)
            if problem is not None and env is not None:
                _note_cleanup_error(env, problem)
