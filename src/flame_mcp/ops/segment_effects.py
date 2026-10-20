"""The effect ops of a segment: `segment.create_effect`, `delete_effect`, `set_effect`,
`effect_load_setup` (each behind a Timeline checkpoint, `_segment_edit`) and `effect_save_setup` (a
file is written, the sequence is not touched: no checkpoint). The Flame side is
`timeline_edit_prelude`; measured on Flame 2025.2.4: TimelineFX attributes hold almost nothing
besides `bypass` (so values change by loading a setup file), a saved setup file gets a per-type
extension, and an effect survives a copy of the sequence. A Resize that Flame adds by itself (the
sequence's bit depth differs from the segment's source bit depth; measured on 2025.2.4 and
2026.2.3) is not deleted: `segment.delete_effect` refuses it first."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any, Final

from flame_mcp import selectors
from flame_mcp.effective_effect import deny_destructive_call
from flame_mcp.ops._common import build_source, raise_on_error_src
from flame_mcp.ops._segment_edit import (
    CHECKPOINTED_SEGMENT_NOTE,
    SEGMENT_REQUIRES,
    checkpoint_spec,
    edit_source,
    no_undo_steps,
    seg_args,
    segment_target_param,
)
from flame_mcp.registry import Registry
from flame_mcp.render_files import is_default_output_dir
from flame_mcp.types import Effect, OperationParam, OperationSpec, Refusal

CREATE_OP: Final[str] = "segment.create_effect"
DELETE_OP: Final[str] = "segment.delete_effect"
SET_OP: Final[str] = "segment.set_effect"
LOAD_OP: Final[str] = "segment.effect_load_setup"
SAVE_OP: Final[str] = "segment.effect_save_setup"

EFFECT_REQUIRES: Final[tuple[str, ...]] = (
    "PySegment.effects",
    "PySegment.effect_types",
    "PyTimelineFX.type",
    "PyTimelineFX.attributes",
    *SEGMENT_REQUIRES,
)

_TYPE_PARAM_NOTE = (
    "`type` is the effect type name as segment.list_effects (include_available=true) lists it, "
    "e.g. 'Burn-in Letterbox', 'Blur' (case does not matter)."
)
_ONE_OF_EACH = (
    "A segment holds at most one effect of each type (Flame answers 'An effect of this type "
    "already exists.'), and the effects are not listed in the order they were made."
)

_CREATE_DESCRIPTION = (
    "Put a new TimelineFX on one segment (PySegment.create_effect). "
    + _TYPE_PARAM_NOTE
    + " "
    + _ONE_OF_EACH
    + " Checked in Flame before anything is copied or journaled: a type that is not "
    "in the segment's effect_types is INVALID_ARGS (result.availableTypes lists all of them, "
    "result.similar the close ones; a type that matches only ignoring case is taken as the "
    "listed spelling, result.requestedType / result.type), a type the segment already has is "
    "PRECONDITION_UNMET (result.effectTypes), and so is a Gap or a sequence inside a library / "
    "Batch group. The effects are read back: one more effect, of that type, or SILENT_NOOP. "
    "result: type, requestedType, effects {before, after} (each [{index, type, attributes: "
    "{name: value}}]), created (the new effect's row), position {version, track, index, "
    "recordIn, selector, selectorByIndex}, returned. The new effect has Flame's default "
    "settings: most types list only `bypass` as an attribute, so segment.set_effect cannot set "
    "their parameters -- segment.effect_load_setup loads them from a setup file. "
    + CHECKPOINTED_SEGMENT_NOTE
)

_DELETE_DESCRIPTION = (
    "Take one TimelineFX off a segment (flame.delete(fx, confirm=False)). "
    + _TYPE_PARAM_NOTE
    + " A segment that has no effect of that type is refused before anything is copied or "
    "journaled (PRECONDITION_UNMET, result.effectTypes is what it has), and so is a Gap or a "
    "sequence inside a library / Batch group. A Resize that is treated as the one Flame adds by "
    "itself is refused (PRECONDITION_UNMET): when the sequence's bit depth differs from the "
    "segment's source bit depth (result.sequenceBitDepth / result.sourceBitDepth), Flame puts a "
    "Resize on the segment, and whether a user made this one on purpose cannot be told. On "
    "Flame 2026.2.3, Flame refused to delete that Resize ('The Timeline FX cannot be deleted.', "
    "after the checkpoint and the journal intent); this op refuses it up front on every "
    "version, for safety. Called alone, it is refused before anything is copied or journaled. "
    "Inside plan.run it stops that step instead (the step checks do not run up front there): "
    "the plan's checkpoint is already taken and the plan can be reverted with journal.revert. "
    "A sequence made with the media's bit depth (timeline.create_sequence bit_depth) gets none. "
    "A depth that cannot be read is not judged. Judged by reading the effects back: the type "
    "must be gone (still there = SILENT_NOOP). result: type, requestedType, effects {before, "
    "after} (each [{index, type, attributes}]), position, returned, warnings, and for a Resize "
    "sequenceBitDepth / sourceBitDepth. " + CHECKPOINTED_SEGMENT_NOTE
)

_SET_DESCRIPTION = (
    "Set attributes of one TimelineFX of a segment (PyAttribute.set_value on the effect's own "
    "attributes). " + _TYPE_PARAM_NOTE + " Most effect types list only `bypass` (a boolean); "
    "Burn-in Letterbox also has `mode` (one of 18 listed values), `ratio` (0.01 to 100) and "
    "`colour` (three numbers, each 0 to 1). A name the effect does not list is INVALID_ARGS "
    "(result.attributes lists the names it has). Every value is checked before a checkpoint is "
    "taken or anything is written, against the attribute's `values` as segment.get_attributes "
    "does (node.set_attributes' check): an enumeration needs one of its candidates, a number "
    "must lie in its range, a tuple takes a list and every element must lie in its own range, "
    "a boolean a boolean, and an integer attribute an integer (123.0 is refused); anything "
    "refused is INVALID_ARGS with result.problems = {name: reason} and nothing written. The "
    "values are then written in order, each read back through a fresh read of the attribute: "
    "one that does not stick is SILENT_NOOP (later ones are not written). result.unchanged "
    "lists the attributes that already held the value asked for. result: type, before / after "
    "({name: value}), applied, failed, unchanged, effect {type, attributes} (all of them now), "
    "position. An effect parameter that is not an attribute cannot be set here: use "
    "segment.effect_load_setup. " + CHECKPOINTED_SEGMENT_NOTE
)

_LOAD_DESCRIPTION = (
    "Load a setup file into one TimelineFX of a segment (PyTimelineFX.load_setup): the effect's "
    "parameters become those of the file. " + _TYPE_PARAM_NOTE + " The effect must already be on "
    "the segment (segment.create_effect first: PRECONDITION_UNMET otherwise). `path` is the real "
    "file (segment.effect_save_setup's result.files[].path, with its extension), absolute. A "
    "path that is not there, or a file of 0 bytes, is refused on this machine before anything is "
    "copied or journaled (PRECONDITION_UNMET; Flame raises 'Could not load the setup. Check the "
    "filepath.' for it and changes nothing); the file is checked again inside Flame. An "
    "exception from load_setup, or an answer other than True, is FLAME_API_ERROR (the effect "
    "list and the segment are read back and reported as they are: result.exception, "
    "result.trackChanged); a setup of another effect type is such an error and changes "
    "nothing. What can be confirmed: the parameters of most "
    "effect types are not attributes, so a load cannot be read back -- confirming that it took "
    "effect goes only as far as the return value (result.returned) and the absence of an "
    "exception; result.attributes {before, after} and result.changedKeys cover the effect's "
    "attributes only (Burn-in Letterbox: mode / ratio / colour), the segment's own track is "
    "compared as a whole (result.trackChanged). The sequence's fingerprint sees attributes only, "
    "so journal.changed reads false for a load that changed only parameters that are not "
    "attributes; journal.revert still restores the whole effect (a copy of a sequence keeps its "
    "effects' settings). result: type, path, size, returned, exception, "
    "attributes, changedKeys, trackChanged, effects (the types now), position. "
    + CHECKPOINTED_SEGMENT_NOTE
)

_SAVE_DESCRIPTION = (
    "Save one TimelineFX's setup to a file (PyTimelineFX.save_setup); nothing in the project "
    "changes (no checkpoint, no journal). " + _TYPE_PARAM_NOTE + " Flame appends an extension "
    "that depends on the effect type ('.blur_node', '.resize_node', '.correct_node', "
    "'.2dtransform_node', '.flip_node', '.timewarp_node'; Burn-in Letterbox writes two files, "
    "'.letterbox' and '.letterbox_node'), so give `path` without an extension: the files that "
    "really appeared are found by comparing the directory before and after (new or changed "
    "files, by size and mtime) and returned as result.files = [{path, size}] (pass one of them to "
    "segment.effect_load_setup); none = SILENT_NOOP. Refused before anything is written: a path "
    "that is not absolute (INVALID_ARGS), a directory that does not exist, a path directly in "
    "/tmp or /var/tmp (a shared temp directory; allow_tmp=true allows it), files named "
    "'<path>.*' already there unless overwrite=true (PRECONDITION_UNMET), a segment without an "
    "effect of that type, a Gap. A call with overwrite=true rewrites those files for good and "
    "counts as destructive for FLAME_MCP_ALLOW_EFFECTS (the op stays effect=create). "
    "Works on a segment inside a library too (it only reads), and on the segment of a reel clip "
    "on the Desktop (parent = the clip's wiretap, version 0, track 0, index 0; measured on Flame "
    "2025.2.4). "
    "result: type, requestedPath, files, overwritten, returned, warnings. Undo: none "
    "(a file was written, delete it by hand; the project is not changed)."
)


def _type_param() -> OperationParam:
    return OperationParam(
        name="type",
        type="string",
        description="The effect type, e.g. 'Burn-in Letterbox' (case does not matter).",
        required=True,
        non_empty=True,
    )


def _path_param(description: str) -> OperationParam:
    return OperationParam(
        name="path", type="string", description=description, required=True, non_empty=True
    )


def _create_to_python(args: Mapping[str, Any]) -> str:
    fx_type = args["type"]
    return edit_source(
        args,
        f"_te_create_check(target, target__parent, {fx_type!r})",
        f"_te_create_effect({seg_args(args)}, {fx_type!r})",
    )


def _delete_to_python(args: Mapping[str, Any]) -> str:
    fx_type = args["type"]
    return edit_source(
        args,
        f"_te_delete_check(target, target__parent, {fx_type!r})",
        f"_te_delete_effect({seg_args(args)}, {fx_type!r})",
    )


def _set_to_python(args: Mapping[str, Any]) -> str:
    fx_type, values = args["type"], dict(args["values"])
    return edit_source(
        args,
        f"_te_set_check(target, target__parent, {fx_type!r}, {values!r})",
        f"_te_set_effect({seg_args(args)}, {fx_type!r}, {values!r})",
    )


def _load_to_python(args: Mapping[str, Any]) -> str:
    fx_type, path = args["type"], args["path"]
    return edit_source(
        args,
        f"_te_load_check(target, target__parent, {fx_type!r}, {path!r})",
        f"_te_load_setup({seg_args(args)}, {fx_type!r}, {path!r})",
    )


def _save_to_python(args: Mapping[str, Any]) -> str:
    fx_type, path = args["type"], args["path"]
    overwrite = bool(args.get("overwrite", False))
    return build_source(
        selectors.resolve_code(args["target"], "target"),
        f"_result = _te_save_setup(target, target__parent, {fx_type!r}, {path!r}, {overwrite!r})",
        raise_on_error_src(),
        "_affected = len(_result['files'])",
    )


def _set_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    values = args.get("values")
    if not isinstance(values, dict) or not values:
        return Refusal("INVALID_ARGS", "values must be a non-empty object {attribute: value}")
    return None


def _load_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    path = args.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return Refusal("INVALID_ARGS", f"path must be an absolute path, got {path!r}")
    if not os.path.isfile(path):
        return Refusal(
            "PRECONDITION_UNMET",
            f"nothing was run: there is no setup file at {path!r} on this machine. "
            "segment.effect_save_setup returns the files it really wrote (result.files[].path, "
            "with the extension Flame appended). Flame itself would raise 'Could not load the "
            "setup. Check the filepath.' for it, so it is not called.",
            result={"ran": False},
        )
    if os.path.getsize(path) == 0:
        return Refusal(
            "PRECONDITION_UNMET",
            f"nothing was run: the setup file {path!r} is empty (0 bytes)",
            result={"ran": False},
        )
    return None


def _save_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    if args.get("overwrite") is True:
        denial = deny_destructive_call(SAVE_OP, "overwrite=true", policy)
        if denial is not None:
            return denial
    path = args.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return Refusal("INVALID_ARGS", f"path must be an absolute path, got {path!r}")
    directory, name = os.path.split(path)
    if not name:
        return Refusal("INVALID_ARGS", f"path names a directory, not a file: {path!r}")
    if not os.path.isdir(directory):
        return Refusal(
            "PRECONDITION_UNMET",
            f"nothing was run: the directory {directory!r} does not exist",
            result={"ran": False},
        )
    if is_default_output_dir(directory) and not args.get("allow_tmp", False):
        return Refusal(
            "PRECONDITION_UNMET",
            f"nothing was run: {directory!r} is a shared temp directory; pick a directory of "
            "your own, or pass allow_tmp=true",
            result={"ran": False},
        )
    existing = sorted(f for f in os.listdir(directory) if f.startswith(name + "."))
    if existing and not args.get("overwrite", False):
        return Refusal(
            "PRECONDITION_UNMET",
            f"nothing was run: save_setup would write next to {existing} in {directory!r}; pass "
            "overwrite=true to allow that",
            result={"ran": False, "existing": existing},
        )
    return None


def _label(op_name: str) -> Callable[[Mapping[str, Any]], str]:
    def label(args: Mapping[str, Any]) -> str:
        return f"before {op_name} ({args['type']})"

    return label


def register(registry: Registry) -> None:
    target = segment_target_param()
    type_param = _type_param()

    def checkpointed(
        name: str,
        effect: Effect,
        description: str,
        params: tuple[OperationParam, ...],
        **kwargs: Any,
    ) -> None:
        registry.register(
            OperationSpec(
                name=name,
                category="segment",
                description=description,
                effect=effect,
                undo="checkpoint",
                params=(target, type_param, *params),
                accepts=("segment",),
                undo_steps=no_undo_steps,
                checkpoint=checkpoint_spec(name, _label(name)),
                **kwargs,
            )
        )

    checkpointed(
        CREATE_OP,
        "create",
        _CREATE_DESCRIPTION,
        (),
        to_python=_create_to_python,
        requires=("PySegment.create_effect", *EFFECT_REQUIRES),
    )
    checkpointed(
        DELETE_OP,
        "destructive",
        _DELETE_DESCRIPTION,
        (),
        to_python=_delete_to_python,
        requires=("flame.delete", *EFFECT_REQUIRES),
    )
    checkpointed(
        SET_OP,
        "reversible",
        _SET_DESCRIPTION,
        (
            OperationParam(
                name="values",
                type="object",
                description="{attribute name: new value}. A tuple attribute takes a list.",
                required=True,
            ),
        ),
        to_python=_set_to_python,
        precheck=_set_precheck,
        requires=("PyAttribute.values", "PyAttribute.set_value", *EFFECT_REQUIRES),
    )
    checkpointed(
        LOAD_OP,
        "reversible",
        _LOAD_DESCRIPTION,
        (_path_param("Absolute path of an existing effect setup file (with its extension)."),),
        to_python=_load_to_python,
        precheck=_load_precheck,
        requires=("PyTimelineFX.load_setup", *EFFECT_REQUIRES),
    )
    registry.register(
        OperationSpec(
            name=SAVE_OP,
            category="segment",
            description=_SAVE_DESCRIPTION,
            effect="create",
            undo="none",
            params=(
                target,
                type_param,
                _path_param(
                    "Absolute path WITHOUT an extension (Flame appends one per effect type)."
                ),
                OperationParam(
                    name="overwrite",
                    type="boolean",
                    description="Allow files named '<path>.*' that already exist to be rewritten.",
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="allow_tmp",
                    type="boolean",
                    description="Allow a path directly in /tmp or /var/tmp.",
                    required=False,
                    default=False,
                ),
            ),
            accepts=("segment",),
            to_python=_save_to_python,
            undo_steps=no_undo_steps,
            precheck=_save_precheck,
            requires=("PyTimelineFX.save_setup", *EFFECT_REQUIRES),
        )
    )
