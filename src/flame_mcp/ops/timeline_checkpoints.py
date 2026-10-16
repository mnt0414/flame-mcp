"""`timeline.fingerprint` and `timeline.checkpoint`: the Timeline counterpart of
`batch.fingerprint` / `batch.checkpoint`. The Flame side is `timeline_prelude`, the server side of
the checkpoint (the stash library, the journal record) `timeline_checkpoints`, and what puts a
sequence back `timeline_restore`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.registry import Registry
from flame_mcp.timeline_checkpoints import (
    DEFAULT_LIBRARY,
    LIBRARY_ARG,
    LIBRARY_ENV,
    REEL_ARG,
    REEL_PREFIX,
    run_timeline_checkpoint,
)
from flame_mcp.types import OperationParam, OperationSpec, SelectorKind

_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection", "timeline")

_TARGET = OperationParam(
    name="target",
    type="selector",
    description=(
        "A wiretap / path / selection / timeline selector of a sequence or clip on the Desktop "
        "(timeline: the sequence the Timeline is showing)."
    ),
    required=True,
)

_UNSEEN = (
    "Not seen by the fingerprint: markers, transitions, audio, an effect's parameters that are not "
    "listed in its attributes, the playhead, the selection, the names and attributes of segments "
    "and the name of the sequence."
)

_FINGERPRINT_DESCRIPTION = (
    "A sequence's / clip's fingerprint: sha256 over every version, every track and every "
    "segment in order -- type, source_name, record_in / record_out / source_in / source_out "
    "(frames, absolute; record_out / source_out are the last frame, inclusive), head, tail and "
    "the segment's effects (type and the value of every "
    "attribute it lists). Read-only. Also versions (the number of segments of each track of each "
    "version), segmentCount, effectCount and unreadable (how many values could not be read: a "
    "fingerprint with some is still comparable with another taken the same way). target is the "
    "identity of what was read ({type, name, uid, wiretapId, selector, parent}), stable its "
    "wiretap selector. " + _UNSEEN
)

_CHECKPOINT_DESCRIPTION = (
    "Take a checkpoint of a sequence or clip on the Desktop: copy it (flame.media_panel.copy) into "
    "a stash reel of a library, in one Flame call, and record it in the journal (kind 'timeline'). "
    f"The stash is the library '{DEFAULT_LIBRARY}' (the environment variable {LIBRARY_ENV} "
    f"names another) with the reel '{REEL_PREFIX}<YYMMDD>' (today's date); both are made on first "
    "use (result.libraryCreated / reelCreated). The copy's fingerprint must equal the original's "
    "(else FLAME_API_ERROR; the copy is not deleted, its uid is in result.stash, and nothing is "
    "recorded). A target inside a library or a Batch group is refused (PRECONDITION_UNMET). "
    "It reads what the Timeline shows before and after the copy and, if it changed, opens "
    "what was shown before again (result.display: shownBefore, shownAfterCopy, reshown, "
    "note, shownAfter; also in the journal record). "
    "result: checkpoint (the id), source {type, name, uid, wiretapId, parent}, stash {type, name, "
    "uid, wiretapId, library, reel}, fingerprint, segmentCount, effectCount, displayed (whether "
    "the target is the sequence shown in the Timeline). Changes nothing in the target; the stash "
    "is never deleted here (a library entry cannot be edited). Restore with "
    "journal.restore_checkpoint. " + _UNSEEN
)


def _fingerprint_to_python(args: Mapping[str, Any]) -> str:
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        "\n".join(
            [
                "_tl_check_media(target, 'target')",
                "_result = _tl_fingerprint(target)",
                "_result['target'] = _tl_ident(target)",
                "_result['stable'] = _wiretap_selector(target)",
            ]
        ),
    )


def _checkpoint_to_python(args: Mapping[str, Any]) -> str:
    library = args.get(LIBRARY_ARG, DEFAULT_LIBRARY)
    reel = args.get(REEL_ARG, REEL_PREFIX)
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        f"_result = _tl_checkpoint(target, {library!r}, {reel!r})",
        raise_on_error_src(),
    )


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


_FINGERPRINT_REQUIRES = (
    "PySequence.versions",
    "PyVersion.tracks",
    "PyTrack.segments",
    "PySegment.type",
    "PySegment.source_name",
    "PySegment.record_in",
    "PySegment.record_out",
    "PySegment.source_in",
    "PySegment.source_out",
    "PySegment.head",
    "PySegment.tail",
    "PySegment.effects",
    "PyTimelineFX.type",
    "PyTimelineFX.attributes",
    "PySequence.parent",
    "PyClip.parent",
)


def register(registry: Registry) -> None:
    registry.register(
        OperationSpec(
            name="timeline.fingerprint",
            category="timeline",
            description=_FINGERPRINT_DESCRIPTION,
            effect="read",
            params=(_TARGET,),
            accepts=_ACCEPTS,
            to_python=_fingerprint_to_python,
            requires=_FINGERPRINT_REQUIRES,
        )
    )
    registry.register(
        OperationSpec(
            name="timeline.checkpoint",
            category="timeline",
            description=_CHECKPOINT_DESCRIPTION,
            effect="create",
            undo="none",
            params=(
                _TARGET,
                OperationParam(
                    name="label",
                    type="string",
                    description="A note stored on the checkpoint record.",
                    required=False,
                    non_empty=True,
                ),
            ),
            accepts=_ACCEPTS,
            to_python=_checkpoint_to_python,
            undo_steps=_no_undo_steps,
            wrap=run_timeline_checkpoint,
            requires=(
                *_FINGERPRINT_REQUIRES,
                "PyMediaPanel.copy",
                "PyTimeline.clip",
                "PyWorkspace.libraries",
                "PyWorkspace.create_library",
                "PyLibrary.reels",
                "PyLibrary.create_reel",
                "PyReel.children",
                "flame.find_by_uid",
            ),
        )
    )
