"""`render.clip`: render the Timeline FX of a clip or sequence. The server side is
`render_clip_run` (and `render_clip_check` for journal.check), the Flame side
`render_clip_prelude`. By design it provides no undo."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.desktop_run import EXPECTED_ARG, PHASE_ARG, PHASE_READ
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.registry import Registry
from flame_mcp.render_clip_run import (
    OP_NAME,
    RENDER_MODES,
    RENDER_QUALITY,
    run_render_clip,
)
from flame_mcp.render_run import DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S
from flame_mcp.types import OperationParam, OperationSpec, Refusal, SelectorKind

_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")

_DESCRIPTION = (
    "Render the Timeline FX of one clip or sequence in the Foreground, and judge it by reading "
    "is_rendered() back. "
    "The target is a wiretap / path / selection selector of a PyClip or PySequence on the "
    "Desktop or in a reel of a Batch group; one in a library is refused. "
    "While it runs Flame is occupied: MCP calls -- reads included -- may not be answered "
    "(BRIDGE_UNAVAILABLE or TIMEOUT). Make timeout_s long enough for the whole render: this "
    "server has no op to stop a render. "
    "Refused before anything is rendered or journaled (PRECONDITION_UNMET, result.ran false): a "
    "clip in a library; a clip without Timeline FX (result.effects is empty); a clip that "
    "is_rendered() already reads true for, unless force=true. A target that is not a PyClip or "
    "PySequence is SELECTOR_TYPE. "
    "is_rendered() is read before and after the render (result.isRenderedBefore / "
    "isRenderedAfter, with top_only for render_mode 'Top'): true afterwards = ok (a render that "
    "returned something other than True is ok with a note in degraded; so is a uid that "
    "changed during it); false afterwards without an "
    "exception = SILENT_NOOP (FLAME_API_ERROR when render returned False); an exception of "
    "Flame = FLAME_API_ERROR. When is_rendered() already read true before the render "
    "(force=true) the read cannot show that the render did anything "
    "(result.readBackDecisive false, result.readBackNote): then the return value is the only "
    "evidence, and a return value other than True is FLAME_API_ERROR. render_option is always "
    "'Foreground', the quality is always 'Full Resolution' and effect_caching_mode stays at "
    "Flame's 'Current'. "
    "render.clip provides no undo: the rendered FX stay rendered, and PyClip.flush_renders does "
    "not make is_rendered() read false again (undo=unsupported; journal.revert has nothing to "
    "restore). "
    "A clip inside a reel of a Batch group can be rendered, but a later restore of that Batch "
    "group's setup (journal.revert or journal.restore_checkpoint of a Batch write) drops the "
    "Timeline FX of a reel clip that was changed inside the group, a render included: "
    "result.timelineFxLost of that restore names them. "
    "A TIMEOUT / BRIDGE_LOST is not an answer: the render may still be running (a TIMEOUT does "
    "not stop it), and this server refuses every write until journal.check has decided. When the "
    "wait ended with the bridge's pending handle, or the bridge still holds the result under the "
    "handle this server gave the call, journal.check asks the bridge for the result first (that "
    "says the render has ended; basis 'handle'). Otherwise it reads is_rendered() again: true = "
    "landed, false = not_landed (basis 'flame', an estimate; for a force=true call that began "
    "with isRenderedBefore true, a true read is not evidence that the render ran). "
    "Not allowed in plan.run."
)


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


def _to_python(args: Mapping[str, Any]) -> str:
    resolve = selectors.resolve_code(args["target"], "target")
    mode = args.get("render_mode", "All")
    quality = RENDER_QUALITY
    if args.get(PHASE_ARG) == PHASE_READ:
        return build_source(
            resolve,
            f"_result = _rc_inspect(target, {mode!r}, {quality!r})",
            raise_on_error_src(),
            "_affected = 0",
        )
    handles = bool(args.get("include_handles", False))
    return build_source(
        resolve,
        f"_result = _rc_render(target, {args.get(EXPECTED_ARG)!r}, {mode!r}, {quality!r}, "
        f"{handles!r})",
        raise_on_error_src(),
        "_affected = 1 if _result['isRenderedAfter'] is True else 0",
    )


def _precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    timeout_s = args.get("timeout_s")
    if isinstance(timeout_s, (int, float)) and timeout_s > MAX_TIMEOUT_S:
        return Refusal(
            "INVALID_ARGS", f"timeout_s must be at most {MAX_TIMEOUT_S}, got {timeout_s}"
        )
    return None


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=OP_NAME,
            category="render",
            description=_DESCRIPTION,
            effect="create",
            undo="unsupported",
            thread="main-async",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description=(
                        "A wiretap / path / selection selector of one PyClip or PySequence, on "
                        "the Desktop or in a reel of a Batch group (not in a library)."
                    ),
                    required=True,
                ),
                OperationParam(
                    name="render_mode",
                    type="string",
                    description=(
                        "The render_mode of PyClip.render: 'All' (default) or 'Top'. 'Top' is "
                        "read back with is_rendered(top_only=True)."
                    ),
                    required=False,
                    default="All",
                    enum=RENDER_MODES,
                ),
                OperationParam(
                    name="include_handles",
                    type="boolean",
                    description="The include_handles argument of PyClip.render (default false).",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="timeout_s",
                    type="number",
                    description=(
                        f"How long to wait for the render, 1 to {MAX_TIMEOUT_S} seconds "
                        f"(default {DEFAULT_TIMEOUT_S}). Make it long enough for the whole "
                        "render: while it runs the bridge may not answer MCP calls (reads "
                        "included) and this server has no op to stop it. The bridge caps a wait "
                        "at 600 s."
                    ),
                    required=False,
                    default=DEFAULT_TIMEOUT_S,
                    integer=True,
                    minimum=1,
                ),
                OperationParam(
                    name="force",
                    type="boolean",
                    description=(
                        "Render although is_rendered() already reads true (the default is "
                        "PRECONDITION_UNMET). A clip without Timeline FX is refused either way."
                    ),
                    required=False,
                    default=False,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_to_python,
            undo_steps=_no_undo_steps,
            precheck=_precheck,
            wrap=run_render_clip,
            requires=(
                "PyClip.render",
                "PyClip.is_rendered",
                "PySequence.render",
                "PySequence.is_rendered",
                "PyClip.name",
                "PyClip.uid",
                "PyClip.parent",
                "PyClip.get_wiretap_node_id",
                "PySequence.name",
                "PySequence.uid",
                "PySequence.parent",
                "PySequence.get_wiretap_node_id",
                "PyReel.type",
                "PyClip.versions",
                "PySequence.versions",
                "PyVersion.tracks",
                "PyTrack.segments",
                "PySegment.effects",
                "PyTimelineFX.type",
            ),
        )
    )
