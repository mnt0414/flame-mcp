"""Timeline checkpoints, server side.

A checkpoint of a sequence / clip on the Desktop is a copy of it in a stash reel
(`flame.media_panel.copy`; measured on Flame 2025.2.4: the copy has a new uid, the same
segments and effect settings, and exported pixels identical to the source), plus a `checkpoint`
record in the journal with `kind: "timeline"`: the id, the source (what was copied: type, name,
uid, wiretap id, parent), the stash (where the copy is: uid, wiretap id, library, reel), the
fingerprint (`timeline_prelude._tl_fingerprint`), the segment count and
whether the source was the sequence shown in the Timeline.

The stash is the library `flame_mcp_checkpoints` (`FLAME_MCP_CHECKPOINT_LIBRARY` overrides the
name: an end-to-end test uses its own so that it sees the first-time creation every run and
can remove it), made on first use, with a reel `timeline_<YYMMDD>` (the date is the server's)
made on first use of the day. A `trim_tail` on a copy in a library raised "Clip is locked." on
Flame 2025.2.4, so a stashed copy is hard to change by mistake. Nothing here deletes anything: a
stash is kept until a human removes it.

`timeline.checkpoint` runs in Flame in one call (`_tl_checkpoint`): fingerprint of the source, the
stash reel, the copy, the fingerprint of the copy. A copy whose fingerprint differs is an error
that names its uid and is not recorded as a checkpoint.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from flame_mcp import envelope as envelope_mod
from flame_mcp import execution
from flame_mcp.checkpoints import CHECKPOINT_TIMEOUT_S
from flame_mcp.journal import new_id, now_iso
from flame_mcp.registry import RegisteredOperation

if TYPE_CHECKING:
    from flame_mcp.runtime import Runtime

LIBRARY_ENV: Final[str] = "FLAME_MCP_CHECKPOINT_LIBRARY"
DEFAULT_LIBRARY: Final[str] = "flame_mcp_checkpoints"
REEL_PREFIX: Final[str] = "timeline_"
LIBRARY_ARG: Final[str] = "_library"
REEL_ARG: Final[str] = "_reel"
"""Reserved args keys carrying the stash's library and reel name into `to_python`. They are not
declared parameters, so `validate_op_args` rejects them from a caller."""

KIND: Final[str] = "timeline"


def checkpoint_library_name() -> str:
    return os.environ.get(LIBRARY_ENV) or DEFAULT_LIBRARY


def stash_reel_name() -> str:
    return f"{REEL_PREFIX}{datetime.now().astimezone():%y%m%d}"


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def stash_record(stash: Any) -> dict[str, Any] | None:
    """The part of Flame's stash row a journal record keeps."""
    if not isinstance(stash, dict):
        return None
    return {
        "type": stash.get("type"),
        "name": stash.get("name"),
        "uid": stash.get("uid"),
        "wiretapId": stash.get("wiretapId"),
        "library": stash.get("library"),
        "reel": stash.get("reel"),
    }


def source_record(source: Any) -> dict[str, Any] | None:
    if not isinstance(source, dict):
        return None
    parent = source.get("parent")
    return {
        "type": source.get("type"),
        "name": source.get("name"),
        "uid": source.get("uid"),
        "wiretapId": source.get("wiretapId"),
        "parent": (
            {
                "type": parent.get("type"),
                "name": parent.get("name"),
                "uid": parent.get("uid"),
                "wiretapId": parent.get("wiretapId"),
            }
            if isinstance(parent, dict)
            else None
        ),
    }


def checkpoint_record(
    checkpoint_id: str,
    *,
    group: str | None,
    label: Any,
    source: Any,
    stash: Any,
    fingerprint: Any,
    segment_count: Any,
    displayed: Any,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "checkpoint",
        "id": checkpoint_id,
        "group": group,
        "ts": now_iso(),
        "kind": KIND,
        "label": label,
        "source": source_record(source),
        "stash": stash_record(stash),
        "fingerprint": fingerprint,
        "segmentCount": segment_count,
        "displayed": displayed,
        **extra,
    }


def run_timeline_checkpoint(
    rt: Runtime, op: RegisteredOperation, op_args: dict[str, Any], *, group: str | None = None
) -> dict[str, Any]:
    """The `wrap` handler of `timeline.checkpoint`: run the copy in Flame, then journal the
    checkpoint record (the copy alone would be a sequence nothing can find again)."""
    started = time.monotonic()
    checkpoint_id = new_id("c")
    flame_args = {
        **op_args,
        LIBRARY_ARG: checkpoint_library_name(),
        REEL_ARG: stash_reel_name(),
    }
    with rt.write_lock:
        env = execution.run_op(op, flame_args, run_dir=rt.run_dir, timeout_s=CHECKPOINT_TIMEOUT_S)
        if env.get("ok") is not True:
            return env
        raw = env.get("result")
        result: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        result.pop("error", None)
        result.pop("errorCode", None)
        record = checkpoint_record(
            checkpoint_id,
            group=group,
            label=op_args.get("label"),
            source=result.get("source"),
            stash=result.get("stash"),
            fingerprint=result.get("fingerprint"),
            segment_count=result.get("segmentCount"),
            displayed=result.get("displayed"),
            effectCount=result.get("effectCount"),
            display=result.get("display"),
        )
        result["checkpoint"] = checkpoint_id
        result["label"] = op_args.get("label")
        try:
            rt.journal.append(record)
        except OSError as exc:
            stash = result.get("stash") or {}
            return envelope_mod.build_envelope(
                ok=False,
                result=result,
                error=(
                    f"the sequence was copied (uid {stash.get('uid')}, in "
                    f"{stash.get('library')}/{stash.get('reel')}) but the checkpoint could not be "
                    f"recorded in the journal ({exc}), so it cannot be restored by id"
                ),
                error_code="POLICY_DENIED",
                execution=envelope_mod.execution_meta(op),
                duration_ms=_elapsed_ms(started),
                hint_override=(
                    f"Make {rt.journal.directory} writable, then take the checkpoint again."
                ),
            )
        env["result"] = result
        env["journal"] = {"group": group, "id": checkpoint_id, "kind": "checkpoint"}
        return env
