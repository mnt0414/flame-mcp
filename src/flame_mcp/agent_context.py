"""Static context for flame_context: known pitfalls and the conventions block."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, get_args

from flame_mcp import registry as registry_mod
from flame_mcp import selectors
from flame_mcp.registry import Registry
from flame_mcp.types import SelectorKind, Undo

PITFALLS: Final[tuple[Mapping[str, str], ...]] = (
    {
        "text": "A Batch setup does not contain the Timeline FX of the clips in the group's reels: "
        "loading the setup again (journal.revert, journal.restore_checkpoint) can bring a clip "
        "back without them. Every clip whose Timeline FX were changed or rendered while it was "
        "in the group came back without them; a clip that was only copied in can lose them too. "
        "result.timelineFxLost lists the clips that lost theirs; "
        "add the original clip from the Desktop again (batch.add_clips).",
    },
    {
        "text": "Do not save or load the setup of a Timeline FX (save_setup / load_setup) in the "
        "same Flame call as a smart_replace_media of its segment. Measured with a Timewarp: "
        "after that, every clip put on a sequence made later in the session got an extra Image "
        "effect (seen on Flame 2025.2.4 and 2026.2.3), and Flame 2025.2.4 later "
        "hung or crashed in save_setup. The same steps as separate calls (plan.run steps are "
        "separate calls) changed nothing on either version. A replace also shifts the "
        "Timewarp's timing by the length of the head, so save its setup before and load it "
        "after, each as its own call (segment.effect_save_setup, segment.effect_load_setup).",
    },
    {
        "text": "smart_replace (segment.replace mode=media_and_fx) drops the segment's effects "
        "when the replacing media has none of its own (a Resize that Flame added by itself "
        "because of a bit depth difference stays: measured on Flame 2026.2.3). To keep them "
        "use mode=media (smart_replace_media).",
    },
    {
        "text": "A sequence whose bit depth differs from its media gets a Resize on those segments "
        "from Flame, automatically. On Flame 2026.2.3, Flame refused to delete that Resize; on "
        "every version segment.delete_effect refuses it up front, for safety. "
        "timeline.create_sequence takes bit_depth (8, 10, 12, 16 or 32) so a "
        "sequence can be made with the bit depth of its media; left out, the project's default "
        "applies and it differs per project.",
    },
    {
        "text": "replace on an Unlinked Video segment (from an EDL) matches by offset from the "
        "clip start, not by source timecode: replace, then slip by the difference to line up "
        "TC. On a linked 8K DPX segment (Flame 2025.2.4) it matched by source timecode, and "
        "refused with 'Invalid source_clip' a clip whose range does not contain the segment's "
        "source range (a 1-frame clip is always accepted); see segment.replace.",
    },
    {
        "text": "PyTime(int) is relative (the sequence start is frame 1). "
        "An absolute position is PyTime(frame, fps) or PyTime(timecode, fps).",
    },
    {
        "text": "A PyAttribute.set_value write may not be put on Ctrl+Z history (seen on the name "
        "of a reel on the Desktop and on a Batch node attribute: Ctrl+Z removed an earlier, "
        "unrelated operation). Do not undo it with Ctrl+Z. Read it back; revert with "
        "journal.revert.",
    },
    {
        "text": "create_batch_group moves the current Batch group. Deleting the current Batch "
        "group moves current to an unpredictable other group.",
    },
    {
        "text": "Deleting a Batch schematic reel that holds clips also deletes the Clip nodes that "
        "reference it. desktop.delete refuses a reel (Desktop or Batch) that holds a clip or a "
        "sequence for the same reason; it deletes only an empty schematic reel of a Batch group, "
        "and never the last one of a group.",
    },
    {
        "text": "desktop.delete refuses a Batch group. batch.delete deletes one after saving its "
        "setup (result.setupPath); load it into a new Batch group with batch.load_setup to get "
        "the nodes back (not shelf reels, iterations or empty reels). It refuses the current or "
        "an open group (allow_current), a group whose reels or iterations hold anything "
        "(allow_content_loss) and a name that differs from expect_name. journal.revert cannot "
        "put a deleted group back.",
    },
    {
        "text": "desktop.sync_record_tc writes only the clips it can judge: a row is synced, "
        "in_sync or skipped with a reason (several segments, a Gap before the media, a "
        "frame-rate difference, a DPX source, a source with no Source TC). journal.revert writes "
        "the earlier Record TC back.",
    },
    {
        "text": "render.export of a sequence reads the colour space of every segment first "
        "(timeline.colour_spaces). A sequence that is mixed, or whose colour spaces could not all "
        "be read, is refused with PRECONDITION_UNMET; skip_colour_check=true exports anyway.",
    },
    {
        "text": "shared_source_segments() does not return copies inside a library "
        "(from the desktop it returns desktop copies only).",
    },
    {
        "text": "Deleting the displayed sequence can leave the Timeline empty "
        "(flame.timeline.clip is None); in other runs it went back to a sequence shown "
        "before it within 3 s.",
    },
    {
        "text": "In at least one project, a sequence copy moved the Timeline to the new copy. "
        "This server reads what the Timeline shows before and after every sequence copy it "
        "makes (checkpoints, desktop.copy, desktop.duplicate) and opens what was shown before "
        "again when it changed (result.display).",
    },
    {
        "text": "trim_head / trim_tail / slip return False even when they worked. "
        "Judge by reading the segment back.",
    },
    {
        "text": "cut outside the sequence and replace with a shorter clip raise nothing "
        "and change nothing.",
    },
    {
        "text": "An audio segment is listed only through seq.audio_tracks[].channels[].segments "
        "(PyAudioTrack has no .segments). timeline.list_segments with media='audio' lists them; "
        "no op of this server selects one: the segment selector reads the video tracks only.",
    },
    {
        "text": "PySegment.smart_replace_media on an audio segment crashed Flame (signal 11; "
        "Flame 2025.2.4). To replace audio, use timeline.overwrite with a clip that has audio.",
    },
    {
        "text": "PySegment.trim_tail on an audio segment turned the audio into a Gap, with ripple "
        "True and with ripple False (Flame 2025.2.4). To shorten a sequence, cut the audio and "
        "Gap tracks at the end frame and extract the tail pieces: timeline.shorten does that.",
    },
    {
        "text": "timeline.overwrite of a clip that has audio replaced the audio of the sequence "
        "even where the video segment already held the same source and did not change (Flame "
        "2025.2.4). The fingerprint behind SILENT_NOOP and journal.changed does not see audio; "
        "timeline.overwrite, insert, cut, delete_segments, move_track and shorten read the audio "
        "tracks on their own and report them in result.audio, and a call that changed only the "
        "audio is not a SILENT_NOOP.",
    },
    {
        "text": "PySequence.extract_selection_to_media_panel (what timeline.delete_segments with "
        "ripple and timeline.shorten call) sets the length of the sequence to the end of the "
        "longest content of ANY video track or audio channel (a Gap is not content) and makes the "
        "Gap at the end of every track follow it, also on tracks that were not selected (Flame "
        "2025.2.4). Deleting the last content of the longest track can therefore make "
        "the whole sequence shorter and shorten the Gap at the end of the other tracks and of the "
        "audio; a track that ends in content at the new end has no Gap. The content of the other "
        "tracks does not move.",
    },
    {
        "text": "No method that moves or reorders a track was found on PySequence, PyVersion, "
        "PyTrack or PyAudioTrack (Flame 2025.2.4, measured by listing them). To move a video "
        "track use timeline.move_track, which rebuilds it. No method to move or reorder audio "
        "tracks was found either. Moving a video track "
        "of a sequence with sound (measured with 2 to 4 video tracks and one audio track of 2 "
        "channels), also the track the audio clip was laid on, left the audio rows as they were "
        "(Flame 2025.2.4).",
    },
    {
        "text": "Flame can crash on a 2-byte character. Every string sent to it (names, paths, "
        "eval.run code) must be ASCII; anything else is refused with INVALID_ARGS before "
        "it is sent.",
    },
)


UNDO_COMMON: Final[str] = (
    "Revert with journal.revert(group): the group is in the response's `journal`. Do not "
    "press Ctrl+Z. This server does not use Flame's Undo, and Flame's Undo has two faults here. "
    "A set_value write may not be put on its history (seen on the name of a reel on the Desktop "
    "and on a Batch node attribute: Ctrl+Z removed an earlier, unrelated operation instead), so "
    "never take a write back with Ctrl+Z; use journal.revert. And the journal does not know "
    "about a Ctrl+Z: after one, a later journal.revert can come back CONFLICT or alreadyGone. "
    "journal.revert puts things back only while each is still as the call left it (else "
    "CONFLICT and nothing is touched). A write whose value changed between the server's read "
    "and its write is refused with CONFLICT (nothing written); the response's `revert` field is "
    "a ready-to-call op + args. What an op's revert restores, what it does not bring back and "
    "what stays is in that op's description (flame_catalog). A closed library reads as empty: a "
    "call whose selectors name one opens it first and closes it again afterwards "
    "(result.librariesOpened); if it cannot be opened the call stops with PRECONDITION_UNMET "
    "before anything is done. Library contents are never deleted or moved out by this server."
)

UNDO_CLASSES: Final[tuple[tuple[Undo, str], ...]] = (
    (
        "value",
        "journal.revert puts the previous value back, or deletes or moves back what the call made",
    ),
    ("checkpoint", "journal.revert restores the checkpoint taken before the call"),
    ("unsupported", "journal.revert cannot reverse it"),
    (
        "none",
        "journal.revert has nothing to restore; the op's description says what to do instead",
    ),
)


def undoable_op_names(registry: Registry, undo: Undo) -> list[str]:
    return sorted(
        op.name
        for op in registry.list_ops()
        if op.undo == undo and not (op.undo == "none" and op.effect == "read")
    )


def undo_contract(registry: Registry) -> str:
    parts = [UNDO_COMMON]
    for undo, meaning in UNDO_CLASSES:
        if names := undoable_op_names(registry, undo):
            parts.append(f"undo={undo} ({meaning}): {', '.join(names)}.")
    parts.append(
        "Ops with effect=read are not listed (they change nothing). plan.run, plan.for_each and "
        "eval.run take their undo from their steps or declared effect: see their descriptions."
    )
    return " ".join(parts)


def static_context(registry: Registry) -> dict[str, Any]:
    return {
        "undoContract": undo_contract(registry),
        "threadContract": (
            "Every Flame API call happens on Flame's own main thread via "
            "schedule_idle_event, reached only through the bridge; this server never "
            "imports or calls the Flame API itself."
        ),
        "selectorVocabulary": [selectors.describe_kind(kind) for kind in get_args(SelectorKind)],
        "defaultLimit": registry_mod.DEFAULT_LIMIT,
        "maxLimit": registry_mod.MAX_LIMIT,
        "categories": list(registry.list_categories()),
        "pitfalls": [dict(pitfall) for pitfall in PITFALLS],
    }
