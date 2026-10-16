"""`desktop` category: Desktop Reel Group / Reel / Clip and the operations
that work on both the Desktop and the Library (listing children, the current
selection, rename)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flame_mcp import selectors
from flame_mcp.desktop_run import (
    CREATED_ARG,
    EXPECTED_ARG,
    MAX_SOURCES,
    PHASE_ALIVE,
    PHASE_ARG,
    PHASE_READ,
    PHASE_REVERT,
    PHASE_REVERT_PLAN,
    PHASE_WRITE,
    run_desktop_write,
)
from flame_mcp.ops._attributes import attribute_requires, register_attribute_ops
from flame_mcp.ops._common import (
    build_source,
    list_result_src,
    offset_param,
    raise_on_error_src,
)
from flame_mcp.registry import Registry
from flame_mcp.types import (
    Category,
    JournalSpec,
    OperationParam,
    OperationSpec,
    Refusal,
    SelectorKind,
)

CATEGORY = Category(
    name="desktop",
    description=(
        "Reel groups, reels and clips of the Desktop; creating a Batch group; selection; copy "
        "and move; generic operations that work on the Desktop and in a library."
    ),
    default_thread="main",
    default_pacing="none",
    allowed_thread=frozenset({"main", "main-async"}),
    allowed_pacing=frozenset({"none"}),
)

_TARGET_ACCEPTS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection")
_SOURCE_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection", "batch")
_DEST_KINDS: tuple[SelectorKind, ...] = ("wiretap", "path", "selection", "desktop")
CREATE_KINDS: tuple[str, ...] = ("batch_group", "reel_group", "reel")
_BATCH_ONLY_ARGS: tuple[str, ...] = ("reels", "shelf_reels", "start_frame", "duration")
MAX_REEL_NAMES = 50


def _list_children_to_python(args: Mapping[str, Any]) -> str:
    limit = args["limit"]
    offset = args["offset"]
    types = list(args.get("types") or [])
    target_sel = args["target"]
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        "\n".join(
            [
                "try:",
                "    _children = list(target.children)",
                "except BaseException as __lc_exc:",
                "    raise _OpError('SELECTOR_TYPE', 'target has no children: ' + str(__lc_exc))",
                f"_types = {types!r}",
                "if _types:",
                "    _children = [_x for _x in _children if type(_x).__name__ in _types]",
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_children)",
                "_items = [_describe_entry(_x) for _x in _children[_offset : _offset + _limit]]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
    )


def _selection_to_python(args: Mapping[str, Any]) -> str:
    limit = args["limit"]
    offset = args["offset"]
    return build_source(
        "\n".join(
            [
                "_entries = list(flame.media_panel.selected_entries)",
                f"_limit = {limit!r}",
                f"_offset = {offset!r}",
                "_total = len(_entries)",
                "_items = [_describe_entry(_x) for _x in _entries[_offset : _offset + _limit]]",
            ]
        ),
        list_result_src("_items", "_total", "_offset"),
    )


def _rename_to_python(args: Mapping[str, Any]) -> str:
    # Measured on Flame 2025.2.4: a PyAttribute.set_value() write may not be put on Flame's
    # Undo history (on the name of a reel on the Desktop, Ctrl+Z removed the preceding,
    # unrelated operation instead). So undo_steps is always
    # reported as 0, and the result carries a `revert` (the op/args that call rename again)
    # that stays valid across wiretap IDs. get_wiretap_node_id() should work for an object on
    # the Media Panel; if it cannot be read, `revert` is None (the result does not pretend
    # that Ctrl+Z or a re-call can undo it).
    name = args["name"]
    target_sel = args["target"]
    # `_expected` is set only by the server's journaled write path (journaled_write.py):
    # compare-and-swap on the name it read a moment ago.
    expected = args.get("_expected")
    cas_lines: list[str] = []
    if expected is not None:
        cas_lines = [
            f"_expected_name = {expected.get('name')!r}",
            "if _before != _expected_name:",
            "    _result = {",
            "        'type': type(target).__name__,",
            "        'before': _before,",
            "        'after': _before,",
            "        'applied': [],",
            "        'failed': {'name': 'conflict: expected ' + repr(_expected_name)",
            "                   + ' but the current value is ' + repr(_before)},",
            "        'conflicts': {'name': {'expected': _expected_name, 'current': _before}},",
            "        'revert': None,",
            "    }",
            "    raise _OpError(",
            "        'CONFLICT',",
            "        'rename: nothing written, the name changed between the read and the write '",
            "        '(expected ' + repr(_expected_name) + ', current ' + repr(_before) + ')',",
            "    )",
        ]
    return build_source(
        selectors.resolve_code(target_sel, "target"),
        "\n".join(
            [
                "_before = _v(target.name)",
                *cas_lines,
                f"target.name.set_value({name!r})",
                "_after = _v(target.name)",
                "try:",
                "    _wiretap_id = target.get_wiretap_node_id()",
                "except BaseException:",
                "    _wiretap_id = None",
                "_revert = None",
                "if _wiretap_id is not None:",
                "    _revert = {",
                "        'op': 'desktop.rename',",
                "        'args': {",
                "            'target': {'kind': 'wiretap', 'id': _wiretap_id},",
                "            'name': _before,",
                "        },",
                "    }",
                "_result = {",
                "    'type': type(target).__name__,",
                "    'before': _before,",
                "    'after': _after,",
                "    'applied': ['name'],",
                "    'failed': {},",
                "    'readBack': {'name': _after},",
                "    'wiretapId': _wiretap_id,",
                "    'revert': _revert,",
                "}",
                "_affected = 1",
            ]
        ),
    )


def _rename_verify(args: Mapping[str, Any]) -> str:
    name = args["name"]
    return "\n".join(
        [
            "_actual = _v(target.name)",
            f"_verified = (_actual == {name!r})",
            "if not _verified:",
            f"    _verify_detail = 'expected name ' + repr({name!r}) + ' but got ' + repr(_actual)",
        ]
    )


def _create_opts(args: Mapping[str, Any]) -> dict[str, Any]:
    opts: dict[str, Any] = {key: args[key] for key in _BATCH_ONLY_ARGS if args.get(key) is not None}
    if args.get("sequence"):
        opts["sequence"] = True
    return opts


def _create_to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_WRITE)
    kind = args["kind"]
    if phase == PHASE_ALIVE:
        items = list(args[CREATED_ARG]["items"])
        return build_source(f"_result = _dc_alive({kind!r}, {items!r})", "_affected = 0")
    if phase == PHASE_REVERT:
        return build_source(
            f"_result = _dc_revert_created({kind!r}, {dict(args[CREATED_ARG])!r})",
            raise_on_error_src(),
            "_affected = 1 if _result['deleted'] else 0",
        )
    parent = selectors.resolve_code(args["parent"], "parent") if kind == "reel" else "parent = None"
    opts = _create_opts(args)
    if phase == PHASE_READ:
        return build_source(
            parent,
            f"_result = _dc_snapshot({kind!r}, parent, {bool(opts.get('sequence'))!r})",
            "_affected = 0",
        )
    expected = args.get(EXPECTED_ARG)
    return build_source(
        parent,
        f"_result = _dc_create({kind!r}, parent, {args['name']!r}, "
        f"{bool(args.get('allow_rename', False))!r}, {opts!r}, {expected!r})",
        raise_on_error_src(),
        "_affected = 1",
    )


def _create_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    kind = args.get("kind")
    if kind not in CREATE_KINDS:
        return Refusal("INVALID_ARGS", f"kind must be one of {list(CREATE_KINDS)}, got {kind!r}")
    if kind != "batch_group":
        given = [key for key in _BATCH_ONLY_ARGS if key in args]
        if given:
            return Refusal("INVALID_ARGS", f"{given} only apply to kind='batch_group'")
    if kind != "reel":
        if "parent" in args:
            return Refusal(
                "INVALID_ARGS",
                "parent only applies to kind='reel' (a Batch group and a reel group are made on "
                "the current Desktop)",
            )
        if args.get("sequence"):
            return Refusal("INVALID_ARGS", "sequence only applies to kind='reel'")
    elif "parent" not in args:
        return Refusal(
            "INVALID_ARGS",
            "kind='reel' needs parent: a selector of a Desktop reel group or a library",
        )
    for key in ("reels", "shelf_reels"):
        names = args.get(key)
        if names is None:
            continue
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_REEL_NAMES:
            return Refusal(
                "INVALID_ARGS",
                f"{key} must hold 1 to {MAX_REEL_NAMES} names (omit it for Flame's default; an "
                "empty list is refused)",
            )
        if not all(isinstance(n, str) and n != "" for n in names):
            return Refusal("INVALID_ARGS", f"{key} must be non-empty strings")
    return None


def _copy_to_python(args: Mapping[str, Any]) -> str:
    phase = args.get(PHASE_ARG, PHASE_WRITE)
    if phase == PHASE_ALIVE:
        items = list(args[CREATED_ARG]["items"])
        return build_source(f"_result = _dc_alive('copy', {items!r})", "_affected = 0")
    if phase == PHASE_REVERT_PLAN:
        return build_source(
            f"_result = _dc_revert_copies({dict(args[CREATED_ARG])!r}, True)",
            raise_on_error_src(),
            "_affected = 0",
        )
    if phase == PHASE_REVERT:
        return build_source(
            f"_result = _dc_revert_copies({dict(args[CREATED_ARG])!r})",
            raise_on_error_src(),
            "_affected = len(_result['deleted'])",
        )
    sources = list(args["sources"])
    blocks = [selectors.resolve_code(args["destination"], "dest")]
    blocks += [selectors.resolve_code(sel, f"_src{i}") for i, sel in enumerate(sources)]
    listed = "[" + ", ".join(f"_src{i}" for i in range(len(sources))) + "]"
    if phase == PHASE_READ:
        created = args.get(CREATED_ARG) or {}
        known, batch = created.get("known"), bool(created.get("batch"))
        blocks += [
            f"_result = _dc_copy_snapshot(dest, {listed}, {known!r}, {batch!r})",
            "_affected = 0",
        ]
    else:
        blocks += [
            f"_result = _dc_copy(dest, {listed}, {args.get(EXPECTED_ARG)!r})",
            raise_on_error_src(),
            "_affected = len(_result['copies'])",
        ]
    return build_source(*blocks)


def _copy_precheck(args: Mapping[str, Any], policy: Any) -> Refusal | None:
    sources = args.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        count = len(sources) if isinstance(sources, list) else sources
        return Refusal(
            "INVALID_ARGS", f"sources must hold 1 to {MAX_SOURCES} selectors, got {count!r}"
        )
    problems = [
        f"sources[{i}]: {issue}"
        for i, sel in enumerate(sources)
        for issue in selectors.validate_selector(sel, _SOURCE_KINDS)
    ]
    problems += [
        f"destination: {issue}"
        for issue in selectors.validate_selector(args.get("destination"), _DEST_KINDS)
    ]
    if problems:
        return Refusal("INVALID_ARGS", "invalid sources / destination: " + "; ".join(problems))
    kinds = [sel.get("kind") for sel in sources]
    if "batch" in kinds and len(sources) > 1:
        return Refusal(
            "INVALID_ARGS",
            "one Batch group is copied alone: a Batch group is not copied together with other "
            "sources",
        )
    if "batch" in kinds and args["destination"].get("kind") == "desktop":
        return Refusal(
            "INVALID_ARGS",
            "a Batch group of the Desktop cannot be copied onto the Desktop: copy it into a "
            "library (a library's copy goes onto the Desktop)",
        )
    return None


def _no_undo_steps(args: Mapping[str, Any], affected: int) -> int:
    return 0


_CREATE_DESCRIPTION = (
    "Create a Batch group (kind='batch_group'), a Desktop reel group (kind='reel_group') or a "
    "reel (kind='reel', in the Desktop reel group or the library that `parent` selects). "
    "A same-named one that already exists refuses the call before anything is made "
    "(PRECONDITION_UNMET, result.existingNames; nothing written, nothing journaled) unless "
    "allow_rename=true: then Flame may change the name of the new one; result.name / "
    "result.renamed / result.requestedName say what the name is. "
    "Side effect of a Batch group: Flame makes the new group the current Batch group; this op "
    "does not put the previous one back. result.previousCurrent and result.current name both, "
    "result.notes says so. "
    "batch_group: `reels` / `shelf_reels` name the schematic / shelf reels (given = exactly "
    "those, and shelf_reels given = no 'Batch Renders'; omitted = Flame's default (result.reels "
    "is the actual list of names) and one shelf reel 'Batch Renders'), `start_frame` (default 1) "
    "and `duration` (default 100) set the group's range; nb_reels is not offered. result.selector "
    "is {kind: 'batch', uid}; reels / shelfReels / startFrame / duration / nodeCount are read "
    "back (a difference from what was asked is listed in `degraded`); a new group holds no node. "
    "reel_group: starts with Reel 1 to 4 and Sequences (result.reelRows). reel: result.type is "
    "'Reel', or 'Sequences' with sequence=true (only for a reel group parent); the reel is "
    "added at the end of the parent. Selectors "
    "(result.selector): a Batch group by uid, the others by wiretap id. "
    "JOURNAL: the server reads the parent's list first, records an intent (the uids it held, "
    "the previous current group), creates with a compare-and-swap on that list (changed = "
    "CONFLICT, nothing created) and records the outcome. Envelope journal = {group, id, "
    "revert}. "
    "Undo: journal.revert(group) deletes what was created (flame.delete) -- only while it is "
    "still as created: a Batch group = no node and every reel / shelf reel empty and named as "
    "created; a reel group = every reel empty; a reel = empty; anything else is CONFLICT and "
    "nothing is deleted. Reverting a Batch group does not make the group that was current "
    "before current again (result.current says which is). "
    "Reverting the last reel of a reel group makes Flame create its default reels again. "
    "Native Ctrl+Z is not used (undoSteps 0). Not allowed in plan.run (it owns its "
    "journal group)."
)

_COPY_DESCRIPTION = (
    "Copy clips / sequences into a reel, a library or a library folder, or one Batch group into a "
    "library or back onto the Desktop, with flame.media_panel.copy. "
    "Clips / sequences: the copies are new clips (new "
    "uids) that reference the same media; the sources stay where they are. This is how to take "
    "something out of a library: desktop.move refuses a source inside one. "
    "It reads what the Timeline shows before and after the copy and, if it changed, opens "
    "what was shown before again "
    "(result.display: shownBefore, shownAfterCopy, reshown, note, shownAfter). Several sources "
    "are copied at once (the copies come back in the order given). Copying a name that is "
    "already in the reel adds another clip of that name "
    "(duplicate_action is not offered: 'replace' replaced nothing). Into a library or a "
    "folder the copies are checked by the returned clips' uids (a warning says when "
    "the destination does not list them). "
    "A destination that is a reel of a Batch group is refused (INVALID_ARGS, nothing written): "
    "use batch.add_clips, which checkpoints the group. "
    "Batch group: one per call, never together with clips. A Batch group of the Desktop "
    "(source {kind: 'batch', uid | name}) goes into a library or a library folder as a backup "
    "copy; a Batch group inside a library (source: a path or selection selector) goes onto the "
    "Desktop (destination {kind: 'desktop'}) as a new Batch group (new uid, same name) with its "
    "nodes, connections, reels and clips; the current Batch group does not change. A reel "
    "destination, in a library or on the Desktop, is refused (INVALID_ARGS, nothing written): "
    "Flame puts the group's clips into a reel instead of the group. A Desktop group onto the "
    "Desktop and a library group into a library are refused too. A Batch group inside a library "
    "lists its reels and clips but reads no nodes (nodes is an empty list): its nodes are back "
    "only after it is copied onto the Desktop. result.copies[].selector of a group in a library "
    "is a name path from its library, null when a name on the way is not unique (then select it "
    "in the Media Panel and use a selection selector); on the Desktop it is {kind: 'batch', uid}. "
    f"At most {MAX_SOURCES} sources. result: destination, requested / returned, copies "
    "[{name, uid, wiretapId, selector, type, sourceUid, inDestination}], sources (with the "
    "uid each had), sourcesRemain (every source is still found by uid) and the destination's "
    "entry names before / after; a Batch group copy also has current / currentBefore / "
    "currentMoved and node counts / fingerprints (sourceNodeCount, sourceFingerprint of the "
    "group of the Desktop copied into a library; copyNodeCount, copyFingerprint of the group "
    "put back onto the Desktop: equal when nothing changed). A call that returns without "
    "an error but leaves the destination "
    "without the copies is an error (SILENT_NOOP). "
    "JOURNAL as desktop.create (intent with the destination's uids -> compare-and-swap -> "
    "outcome with the copies' uids). Envelope journal = {group, id, revert}. "
    "Undo: journal.revert(group) deletes exactly those copies by uid (flame.delete), only while "
    "every one still has the name, duration and content (segments and effects) it had right "
    "after the call; if any changed, or the record has no content fingerprint, it is CONFLICT "
    "and nothing is deleted. A Batch group copy is deleted only while it is still as created: "
    "in a library its name and the clip names of its reels and shelf reels, on the Desktop also "
    "its nodes' names, types, connections and attribute values (a value that cannot be read "
    "counts as changed); before a group on the Desktop is deleted its setup is saved as a "
    "checkpoint (result.savedSetups: name, checkpoint, path, restoreWith = how to bring it "
    "back), and a setup that cannot be saved means nothing is deleted; if the deleted group was "
    "the current Batch group, Flame makes another group current (result.current and notes say "
    "so). A library that is closed (a closed library reads as empty) is opened for the call "
    "and closed again afterwards, a copy into it, out of it and its revert included "
    "(result.librariesOpened [{name, uid, closedAgain}] and a note; one that cannot be closed "
    "again is left open and told there); one that cannot be opened, or whose state cannot be "
    "read, stops the call before anything is done (PRECONDITION_UNMET). "
    "One that is gone already is listed in result.skipped. Native "
    "Ctrl+Z is not used (undoSteps 0). Not allowed in plan.run."
)


def _register_writes(registry: Registry) -> None:
    create_requires = (
        "PyDesktop.create_batch_group",
        "PyDesktop.create_reel_group",
        "PyDesktop.batch_groups",
        "PyDesktop.reel_groups",
        "PyDesktop.current_batch_group",
        "PyReelGroup.create_reel",
        "PyReelGroup.reels",
        "PyLibrary.create_reel",
        "PyLibrary.reels",
        "PyBatch.uid",
        "PyBatch.reels",
        "PyBatch.shelf_reels",
        "PyBatch.nodes",
        "PyBatch.start_frame",
        "PyBatch.duration",
        "PyReel.clips",
        "PyReel.sequences",
        "PyReel.type",
        "PyReel.uid",
        "flame.delete",
        "flame.find_by_uid",
    )
    registry.register(
        OperationSpec(
            name="desktop.create",
            category="desktop",
            description=_CREATE_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="kind",
                    type="string",
                    description="'batch_group', 'reel_group' or 'reel'.",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="name",
                    type="string",
                    description="Name of the new Batch group / reel group / reel.",
                    required=True,
                    non_empty=True,
                ),
                OperationParam(
                    name="allow_rename",
                    type="boolean",
                    description=(
                        "Let Flame rename the new one when the name is taken (default false: "
                        "a taken name refuses the call)."
                    ),
                    required=False,
                    default=False,
                ),
                OperationParam(
                    name="parent",
                    type="selector",
                    description=(
                        "kind='reel' only, required: a wiretap / path / selection selector of a "
                        "Desktop reel group (path root='desktop') or of a library."
                    ),
                    required=False,
                ),
                OperationParam(
                    name="sequence",
                    type="boolean",
                    description=(
                        "kind='reel' only: make a Sequences reel (type 'Sequences'); only for a "
                        "reel group parent."
                    ),
                    required=False,
                ),
                OperationParam(
                    name="reels",
                    type="array",
                    description=(
                        "kind='batch_group' only: names of the schematic reels (exactly these). "
                        "Omit for Flame's default (result.reels is the actual list of names)."
                    ),
                    required=False,
                    item_type="string",
                ),
                OperationParam(
                    name="shelf_reels",
                    type="array",
                    description=(
                        "kind='batch_group' only: names of the shelf reels (exactly these; no "
                        "'Batch Renders' then). Omit for one shelf reel 'Batch Renders'."
                    ),
                    required=False,
                    item_type="string",
                ),
                OperationParam(
                    name="start_frame",
                    type="number",
                    description="kind='batch_group' only: the group's start frame (default 1).",
                    required=False,
                    integer=True,
                ),
                OperationParam(
                    name="duration",
                    type="number",
                    description="kind='batch_group' only: the group's duration in frames "
                    "(default 100).",
                    required=False,
                    integer=True,
                    minimum=1,
                ),
            ),
            accepts=_TARGET_ACCEPTS,
            to_python=_create_to_python,
            undo_steps=_no_undo_steps,
            precheck=_create_precheck,
            wrap=run_desktop_write,
            sample_args={
                "kind": "reel",
                "name": "sample",
                "parent": {"kind": "wiretap", "id": "s"},
            },
            requires=create_requires,
        )
    )
    registry.register(
        OperationSpec(
            name="desktop.copy",
            category="desktop",
            description=_COPY_DESCRIPTION,
            effect="create",
            undo="value",
            params=(
                OperationParam(
                    name="sources",
                    type="array",
                    description=(
                        f"1 to {MAX_SOURCES} selectors of clips / sequences: wiretap, path "
                        "(root='desktop' for the Desktop) or selection; each must resolve to "
                        "one PyClip / PySequence. Or exactly one Batch group: a 'batch' "
                        "selector (a Batch group of the Desktop), or a path / selection "
                        "selector of a Batch group inside a library."
                    ),
                    required=True,
                    item_type="selector",
                ),
                OperationParam(
                    name="destination",
                    type="selector",
                    description=(
                        "A wiretap / path / selection selector of a Desktop reel, a library, a "
                        "library reel or a library folder (for a Batch group source: a library "
                        "or a library folder only). A reel of a Batch group is refused (use "
                        "batch.add_clips). {kind: 'desktop'}: only for a Batch group that is "
                        "inside a library."
                    ),
                    required=True,
                ),
            ),
            accepts=(*_TARGET_ACCEPTS, "batch", "desktop"),
            to_python=_copy_to_python,
            undo_steps=_no_undo_steps,
            precheck=_copy_precheck,
            wrap=run_desktop_write,
            sample_args={
                "sources": [{"kind": "wiretap", "id": "s"}],
                "destination": {"kind": "wiretap", "id": "d"},
            },
            requires=(
                "PyMediaPanel.copy",
                "PyReel.clips",
                "PyReel.sequences",
                "PyReel.type",
                "PyLibrary.clips",
                "PyLibrary.sequences",
                "PyLibrary.children",
                "PyFolder.clips",
                "PyFolder.sequences",
                "PyFolder.children",
                "PyDesktop.batch_groups",
                "PyBatch.reels",
                "PyBatch.shelf_reels",
                "PyBatch.nodes",
                "PyBatch.uid",
                "PyBatch.parent",
                "flame.delete",
                "flame.find_by_uid",
            ),
        )
    )


def register(registry: Registry) -> None:
    registry.register_category(CATEGORY)
    _register_writes(registry)

    limit_param = OperationParam(
        name="limit", type="number", description="Max items to return.", required=False
    )
    target_param = OperationParam(
        name="target",
        type="selector",
        description="wiretap / path / selection selector for the container or entry.",
        required=True,
    )
    list_target_param = OperationParam(
        name="target",
        type="selector",
        description=(
            "wiretap / path / selection selector for the container or entry; {kind: 'desktop'} "
            "for the current Desktop (its reel groups and Batch groups)."
        ),
        required=True,
    )

    registry.register(
        OperationSpec(
            name="desktop.list_children",
            category="desktop",
            description=(
                "List the children of a library/folder/reel group/reel/desktop "
                "(anything with a `.children` attribute), one page at a time: "
                "`offset` skips items, `types` keeps only the named Flame types "
                "(`total` counts the items after that filter)."
            ),
            effect="read",
            params=(
                list_target_param,
                limit_param,
                offset_param(),
                OperationParam(
                    name="types",
                    type="array",
                    description=(
                        "Flame type names to keep, e.g. ['PyClip', 'PySequence']. "
                        "Omit (or pass []) to list every child."
                    ),
                    required=False,
                    item_type="string",
                ),
            ),
            accepts=(*_TARGET_ACCEPTS, "desktop"),
            to_python=_list_children_to_python,
            requires=("PyLibrary.children", "PyReel.children", "PyDesktop.children"),
        )
    )

    registry.register(
        OperationSpec(
            name="desktop.selection",
            category="desktop",
            description="List the current Media Panel selection.",
            effect="read",
            params=(limit_param, offset_param()),
            to_python=_selection_to_python,
            requires=("PyMediaPanel.selected_entries",),
        )
    )

    registry.register(
        OperationSpec(
            name="desktop.rename",
            category="desktop",
            description=(
                "Rename one Media Panel entry (library/folder/reel/clip/sequence/...). "
                "Do not revert it with Ctrl+Z: a set_value write may not be put on Flame's "
                "Undo history (measured on the name of a reel on the Desktop, where Ctrl+Z "
                "removed an earlier, unrelated operation instead). To revert, use "
                "journal.revert, or call flame_do again with this call's result.revert "
                "(op + args, already "
                "filled in) -- it is None only when no wiretap ID could be obtained. "
                "JOURNAL: the server first reads the current name, records an intent "
                "(envelope `journal` = {group, id}), then renames with a compare-and-swap; "
                "if the name changed in between, nothing is written (errorCode CONFLICT). "
                "journal.revert(group) puts the previous name back. A 'batch' selector "
                "(a Batch group, by uid or name) is accepted as well."
            ),
            effect="reversible",
            params=(
                target_param,
                OperationParam(
                    name="name",
                    type="string",
                    description="New name (must not be empty).",
                    required=True,
                    non_empty=True,
                ),
            ),
            accepts=(*_TARGET_ACCEPTS, "batch"),
            to_python=_rename_to_python,
            verify=_rename_verify,
            journal=JournalSpec(
                read_op="desktop.get_attributes",
                keys=lambda args: ["name"],
                values=lambda args: {"name": args["name"]},
                write_args=lambda target, values: {"target": target, "name": values["name"]},
            ),
            # Ctrl+Z does not undo it, so always report 0 (whatever `affected` is). The way
            # back is the result's `revert` field.
            undo_steps=lambda args, affected: 0,
            requires=(
                "PyAttribute.set_value",
                "PyAttribute.get_value",
                "PyReel.get_wiretap_node_id",
                "PyClip.get_wiretap_node_id",
            ),
        )
    )

    register_attribute_ops(
        registry,
        category="desktop",
        noun=(
            "Desktop or Library entry (library / folder / reel group / reel / clip / "
            "sequence / Batch group ...)"
        ),
        accepts=(*_TARGET_ACCEPTS, "batch"),
        target_description=(
            "wiretap / path / selection selector for the entry, or a 'batch' selector for "
            "a Batch group (its revert addresses a Batch group that way, since a Batch "
            "group has no wiretap id). A path may start on the Desktop with root='desktop' "
            "(Reel Group, Reel, Batch group)."
        ),
        stable_src=lambda _args: "_stable = _stable_desktop(target)",
        revert_kinds=("wiretap", "batch"),
        requires=attribute_requires(
            "PyLibrary.attributes",
            "PyReel.attributes",
            "PyClip.attributes",
            "PyDesktop.children",
            "PyDesktop.reel_groups",
            "PyDesktop.batch_groups",
            "PyReel.get_wiretap_node_id",
            "PyBatch.uid",
        ),
    )
