"""`timeline.colour_spaces`: the colour space of every segment of a sequence, and whether they are
mixed. The Flame side is `colourspace_prelude`, the judgement `colour_check`. `render.export` makes
the same check before it exports a sequence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from flame_mcp import execution, selectors
from flame_mcp.colour_check import judge
from flame_mcp.colourspace_prelude import CS_BUDGET_S
from flame_mcp.ops._common import build_source
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.types import OperationParam, OperationSpec, SelectorKind

COLOUR_OP: Final[str] = "timeline.colour_spaces"
_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")

_DESCRIPTION = (
    "List the colour spaces of the segments of a sequence and say whether they are mixed "
    "(PySegment.get_colour_space). Read only. Every version and every video track is read (a "
    "Gap is counted, not read; audio is not). "
    "result: mixed (true only when two or more different colour spaces were read), complete, "
    "spaces [{name, count, first: up to 5 {version, track, index}}], segments, unreadable "
    "(segments whose colour space did not read as a non-empty string: they are never counted as "
    "a colour space and never make the sequence 'mixed'), gaps, versions, warnings. The read "
    f"stops after {CS_BUDGET_S:g} s and then says complete false. A target that is not a "
    "PySequence is SELECTOR_TYPE. render.export of a sequence runs the same check first and "
    "refuses one that is mixed or whose colour spaces could not all be read "
    "(PRECONDITION_UNMET; skip_colour_check=true exports anyway)."
)


def _to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                "if type(target).__name__ != 'PySequence':",
                "    raise _OpError('SELECTOR_TYPE', 'the target is a ' + type(target).__name__"
                " + ', not a PySequence')",
                "_result = _cs_scan(target)",
                "_result['name'] = _try(lambda: _v(target.name))",
                "_affected = 0",
            ]
        ),
    )


def judged(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **judge(raw),
        "name": raw.get("name"),
        "gaps": raw.get("gaps"),
        "versions": raw.get("versions"),
    }


def _run(
    rt: Any, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` of `timeline.colour_spaces`: the Flame read, then `colour_check.judge`."""
    env = execution.run_op(op, op_args, run_dir=rt.run_dir)
    if env.get("ok") is True and isinstance(env.get("result"), dict):
        env["result"] = judged(env["result"])
    return env


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name=COLOUR_OP,
            category="timeline",
            description=_DESCRIPTION,
            effect="read",
            params=(
                OperationParam(
                    name="target",
                    type="selector",
                    description="A wiretap / path / selection selector of one PySequence.",
                    required=True,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_to_python,
            wrap=_run,
            sample_args={"target": {"kind": "wiretap", "id": "s"}},
            requires=(
                "PySequence.versions",
                "PySequence.name",
                "PyVersion.tracks",
                "PyTrack.segments",
                "PySegment.type",
                "PySegment.get_colour_space",
            ),
        )
    )
