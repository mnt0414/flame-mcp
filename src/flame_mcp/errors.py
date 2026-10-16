"""ErrorCode hints and retry classification."""

from __future__ import annotations

from typing import assert_never, get_args

from flame_mcp.types import ErrorCode


def as_error_code(code: str | None) -> ErrorCode:
    """Converts an envelope's errorCode (a str on the wire) to the closed ErrorCode.

    Anything else is reported as FLAME_API_ERROR.
    """
    for member in get_args(ErrorCode):
        if member == code:
            return member  # type: ignore[no-any-return]
    return "FLAME_API_ERROR"


def hint_for(code: ErrorCode) -> str:
    match code:
        case "SELECTOR_EMPTY":
            return "The selector matched zero targets; check the selection or path before retrying."
        case "SELECTOR_TYPE":
            return "The resolved target's type does not match what this operation expects."
        case "SELECTOR_AMBIGUOUS":
            return (
                "The selector matched more than one target. Narrow it: a node needs pos, a "
                "Batch group needs uid, a reel needs reel_kind. Read the candidates to check."
            )
        case "FLAME_API_ERROR":
            return (
                "The Flame API raised an exception, or what Flame did differed from the "
                "measured behaviour; read the error (a traceback, if there is one, is the cause)."
            )
        case "SILENT_NOOP":
            return (
                "The call raised no exception but had no observable effect; "
                "verify preconditions and re-check the target. For a write to the Media Panel, "
                "check that no modal (the message history, a dialog) is open in Flame."
            )
        case "TIMEOUT":
            return (
                "The call did not finish in time; Flame's main thread may still be "
                "busy -- read the target's current state before retrying."
            )
        case "MAIN_THREAD_UNAVAILABLE":
            return "Flame is running but schedule_idle_event is not yet bound; wait and retry."
        case "BRIDGE_LOST":
            return (
                "The socket closed mid-call, which suggests Flame crashed; "
                "restart Flame and check app.log before retrying."
            )
        case "BRIDGE_UNAVAILABLE":
            return "No bridge is reachable yet; wait for Flame to start and a project to load."
        case "BRIDGE_AMBIGUOUS":
            return "Multiple live bridges were found; set FLAME_BRIDGE_SOCKET to select one."
        case "PRECONDITION_UNMET":
            return "A category precondition is not met; resolve it in the Flame UI, then retry."
        case "UNKNOWN_OPERATION":
            return (
                "No operation is registered under that name; "
                "check flame_catalog for the closest match."
            )
        case "INVALID_ARGS":
            return "The arguments did not pass validation; see the issues in the error message."
        case "POLICY_DENIED":
            return "This server's policy denies that operation; see the hint for what to change."
        case "CONFLICT":
            return (
                "The value changed between reading it and writing it, so nothing was "
                "written; read the current value again before deciding what to do."
            )
        case _ as unreachable:
            assert_never(unreachable)


def is_retryable(code: ErrorCode) -> bool:
    """Returns whether the same call may be repeated later.

    True only when no human action or state check is needed first.
    """
    match code:
        case "BRIDGE_UNAVAILABLE" | "MAIN_THREAD_UNAVAILABLE" | "CONFLICT":
            return True
        case (
            "SELECTOR_EMPTY"
            | "SELECTOR_TYPE"
            | "SELECTOR_AMBIGUOUS"
            | "FLAME_API_ERROR"
            | "SILENT_NOOP"
            | "TIMEOUT"
            | "BRIDGE_LOST"
            | "BRIDGE_AMBIGUOUS"
            | "PRECONDITION_UNMET"
            | "UNKNOWN_OPERATION"
            | "INVALID_ARGS"
            | "POLICY_DENIED"
        ):
            return False
        case _ as unreachable:
            assert_never(unreachable)
