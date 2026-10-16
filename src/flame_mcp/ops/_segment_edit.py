"""Shared pieces of the segment edits that run behind a Timeline checkpoint (`segment.slip`,
`segment.replace`, the effect ops): the `CheckpointSpec` they all declare (the sequence the segment
belongs to is copied into the stash first; `journal.revert` puts it back), the pre-checkpoint check
that asks Flame whether the call can be made at all (a refused call leaves no stash and no journal
group behind), and the note every description carries. `segment.trim` declares the same
things in its own module."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from flame_mcp import execution, selectors
from flame_mcp.errors import as_error_code
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.timeline_restore import NOT_RESTORED
from flame_mcp.types import (
    CheckpointCheck,
    CheckpointSpec,
    OperationParam,
    Refusal,
)

PHASE_ARG: Final[str] = "_phase"
PHASE_CHECK: Final[str] = "check"
REFUSAL_HINT_KEY: Final[str] = "refusalHint"
"""A Flame-side check may put a hint for its own refusal under this key of its result; the refusal
carries it instead of the generic hint of the error code, and it is not kept in the result."""

SEGMENT_TARGET_DESCRIPTION: Final[str] = (
    "A 'segment' selector identifying the segment, by index or by record_in (the record_in frame; "
    "it stays put while other segments are edited without ripple)."
)

SEGMENT_REQUIRES: Final[tuple[str, ...]] = (
    "PySegment.record_in",
    "PySegment.record_out",
    "PySegment.record_duration",
    "PySegment.source_in",
    "PySegment.source_out",
    "PySegment.head",
    "PySegment.tail",
    "PySegment.type",
    "PySegment.source_name",
    "PySegment.parent",
    "PyTrack.segments",
    "PyVersion.tracks",
)

CHECKPOINTED_SEGMENT_NOTE: Final[str] = (
    "Runs behind an automatic checkpoint of its sequence: the sequence is copied into the stash "
    "reel first (timeline.checkpoint), the call is journaled (intent with the fingerprint before, "
    "outcome with the fingerprint after) and journal.revert(group) puts the sequence back. "
    "Everything that can be refused without writing (a Gap, a sequence inside a library or a Batch "
    "group, a value Flame would not take) is refused before anything is copied or journaled. "
    "Envelope journal = {group, id, checkpoint, checkpointPath (the stash: <library>/<reel>), "
    "stash, revert, changed}. Undo: journal.revert(group) restores the checkpoint, only while the "
    "sequence still has the fingerprint (segments and their effects) the call left (else CONFLICT "
    "and nothing is touched; force=true overrides, journaled). Timeline restore: the edited "
    "sequence is moved into the stash reel (result.preCheckpoint: the revert can itself be "
    "undone) and the stashed copy is copied back, with a new uid and wiretap id at the end of "
    "its reel. Not put back: " + "; ".join(NOT_RESTORED) + ". Native Ctrl+Z is not used "
    "(undoSteps 0). Segment edits run on Flame's main thread only. In plan.run: a plan that holds "
    "this step is one checkpointed write on this sequence (one stashed copy, journal.revert(group) "
    "is one restore; the step's own checkpoint is not taken); see plan.run."
)


def segment_target_param() -> OperationParam:
    return OperationParam(
        name="target", type="selector", description=SEGMENT_TARGET_DESCRIPTION, required=True
    )


def no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def group_selector(args: Mapping[str, Any]) -> Mapping[str, Any]:
    parent: Mapping[str, Any] = args["target"]["parent"]
    return parent


def rebind(args: Mapping[str, Any], stable: dict[str, Any]) -> dict[str, Any]:
    return {**args, "target": {**args["target"], "parent": stable}}


def seg_args(args: Mapping[str, Any]) -> str:
    """The first four arguments of every Flame-side edit function: the segment, its sequence, the
    version and the track it is in."""
    sel = args["target"]  # .get: an op that also takes other selectors is smoke-tested per kind
    return f"target, target__parent, {sel.get('version', 0)!r}, {sel.get('track', 0)!r}"


def edit_source(
    args: Mapping[str, Any], check_call: str, write_call: str, *extra_resolve: str
) -> str:
    """The op's code: resolve the segment (and what else the op takes), then the Flame-side check
    (`_phase: check`, run before a checkpoint is taken) or the write, each answering a dict whose
    `error` / `errorCode` become the op's error with the dict kept as the result."""
    resolve = selectors.resolve_code(args["target"], "target")
    if args.get(PHASE_ARG) == PHASE_CHECK:
        return build_source(
            resolve,
            *extra_resolve,
            f"_result = {check_call}",
            raise_on_error_src(),
            "_affected = 0",
        )
    return build_source(
        resolve, *extra_resolve, f"_result = {write_call}", raise_on_error_src(), "_affected = 1"
    )


def make_check(op_name: str) -> CheckpointCheck:
    """`CheckpointSpec.check` of `op_name`: its own Flame code in the `check` phase, under the
    write lock and before the checkpoint. A refusal keeps what the check found."""

    def check(rt: Any, args: Mapping[str, Any]) -> Refusal | None:
        op = rt.registry.get(op_name)
        env = execution.run_op(op, {**args, PHASE_ARG: PHASE_CHECK}, run_dir=rt.run_dir)
        if env.get("ok") is True:
            return None
        raw = env.get("result")
        value: dict[str, Any] = raw if isinstance(raw, dict) else {}
        kept = {
            k: v
            for k, v in value.items()
            if k not in ("error", "errorCode", "ran", REFUSAL_HINT_KEY) and v is not None
        }
        return Refusal(
            as_error_code(str(env.get("errorCode"))),
            f"nothing was run: {env.get('error')}",
            hint=value.get(REFUSAL_HINT_KEY) or env.get("hint"),
            result={"ran": False, **kept},
        )

    return check


def checkpoint_spec(op_name: str, label: Callable[[Mapping[str, Any]], str]) -> CheckpointSpec:
    return CheckpointSpec(
        target_kind="timeline",
        label=label,
        check=make_check(op_name),
        group_selector=group_selector,
        rebind=rebind,
    )
