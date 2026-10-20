"""Structured selectors: shape validation and code generation.

`validate_selector` checks the shape; `resolve_code` turns one JSON selector into the
Python source an op's `to_python` embeds to resolve its target(s).

`resolve_code`'s output assumes the exec namespace it runs in already has
`flame` (the Flame API module) and the `_OpError` exception (defined by
execution.py's wrapper prelude, alongside `_v`/`_j`) in scope -- it never
defines those itself, matching the "V_HELPER exists in exactly one place"
convention documented in execution.py.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, assert_never, cast, get_args

from flame_mcp.types import SelectorKind


def selector_label(kind: SelectorKind) -> str:
    match kind:
        case "selection":
            return "The current Media Panel selection."
        case "wiretap":
            return "A stable id from PyClip.get_wiretap_node_id()."
        case "path":
            return "A name path starting from a library (or, with root='desktop', the Desktop)."
        case "segment":
            return "A position within a sequence's tracks."
        case "batch":
            return "A Batch group on the Desktop, by uid or name (it need not be current)."
        case "node":
            return (
                "A node of a Batch group, by name; pos picks one of several nodes that share "
                "the name (the group need not be current)."
            )
        case "action_node":
            return "A node inside an Action / Image / GMask Tracer node, by name."
        case "desktop":
            return "The current workspace's Desktop (it has no fields)."
        case "timeline":
            return (
                "The sequence the Timeline is showing now, read when the call runs (it has no "
                "fields; SELECTOR_EMPTY when the Timeline shows nothing)."
            )
        case _ as unreachable:
            assert_never(unreachable)


# Every selector kind's fields live here and only here: `_KIND_KEYS` (used by
# `validate_selector`'s unexpected-key check) is derived from it, and
# `describe_kind` (what flame_context / flame_catalog publish) reads it. A
# field added to a kind therefore shows up in the published vocabulary the
# moment it is accepted by validation -- tests/test_selectors.py checks that
# `validate_selector`'s per-field rules and this table stay in step.

_NON_NEGATIVE_INT = "non-negative integer"

_KIND_FIELDS: dict[SelectorKind, dict[str, tuple[str, str]]] = {
    "selection": {},
    "wiretap": {
        "id": ("string", "Stable id from PyClip.get_wiretap_node_id(); must not be empty."),
    },
    "path": {
        "path": (
            "string[]",
            "Names from the root down to the target, e.g. [library, reel, clip]; "
            "must not be empty, names must not be empty and must match exactly one entry.",
        ),
        "root": (
            "string",
            "Where the first name is looked up: 'libraries' (default: the workspace's "
            "libraries) or 'desktop' (the Desktop's Reel Groups and Batch groups).",
        ),
    },
    "segment": {
        "parent": (
            "selector",
            "Selector for the sequence, or the reel clip, that holds the segment (selection / "
            "wiretap / path / timeline; not itself a 'segment'). A reel clip holds its own "
            "segments (version 0, track 0, index 0 for a clip with one segment), so a clip's "
            "wiretap id works here like a sequence's. Measured on a clip (Flame 2025.2.4): "
            "info, get_attributes, set_attributes, list_effects, create_effect, set_effect, "
            "delete_effect, effect_save_setup, effect_load_setup, trim, slip, shared_sources, "
            "copy_to_media_panel and match.",
        ),
        "version": (
            _NON_NEGATIVE_INT,
            "Version index within the sequence or clip (0-based).",
        ),
        "track": (_NON_NEGATIVE_INT, "Track index within the version (0-based)."),
        "index": (
            _NON_NEGATIVE_INT,
            "Segment index within the track (0-based). Give exactly one of index / record_in. "
            "A ripple edit shifts the indexes after it: count again after one.",
        ),
        "record_in": (
            _NON_NEGATIVE_INT,
            "The segment's record_in.frame (the value timeline.list_segments returns as "
            "recordIn.frame; absolute, so 86400 for the first segment of a sequence that "
            "starts at 01:00:00:00). Unique within a track. Give exactly one of index / "
            "record_in. An edit that does not ripple leaves the other segments' record_in "
            "where they were, so address by record_in when a step is followed by more "
            "edits; after a ripple edit, read the track again.",
        ),
        "expect_source_name": (
            "string",
            "Optional safeguard: when the segment found has a different source_name, nothing "
            "is done and the call ends in CONFLICT (the message names the actual source_name).",
        ),
    },
    "batch": {
        "uid": ("string", "The Batch group's uid. Give exactly one of uid / name."),
        "name": (
            "string",
            "The Batch group's name; it must match exactly one group. "
            "Give exactly one of uid / name.",
        ),
    },
    "node": {
        "batch": ("selector", "A 'batch' selector for the group that holds the node."),
        "name": (
            "string",
            "The node's name (PyNode.name, the name Schematic shows); must not be empty. "
            "Several nodes of one group can share it.",
        ),
        "pos": (
            "integer[2]",
            "The node's [pos_x, pos_y], two integers compared with pos_x and pos_y exactly. "
            "Picks one of several nodes that share a name (node.list gives each node's pos). "
            "When no node of that name is at that position the call ends in SELECTOR_EMPTY, "
            "also when the name is unique.",
        ),
    },
    "action_node": {
        "node": (
            "selector",
            "A 'node' selector for the Action / Image / GMask Tracer node that holds it.",
        ),
        "name": ("string", "The inner node's name; must not be empty."),
    },
    "desktop": {},
    "timeline": {},
}

_OPTIONAL_FIELDS: dict[SelectorKind, frozenset[str]] = {
    "path": frozenset({"root"}),
    "segment": frozenset({"index", "record_in", "expect_source_name"}),
    "batch": frozenset({"uid", "name"}),
    "node": frozenset({"pos"}),
}
"""Fields validation does not require on their own (`batch`'s uid and name are
each optional, but exactly one of them must be given; so are a `segment`'s index and
record_in; a `node`'s pos narrows several nodes of one name)."""

_PATH_ROOTS = ("libraries", "desktop")
_SEGMENT_PARENT_KINDS: tuple[SelectorKind, ...] = ("selection", "wiretap", "path", "timeline")

_BATCH_EXAMPLE: dict[str, Any] = {"kind": "batch", "name": "Batch Group 1"}
_NODE_EXAMPLE: dict[str, Any] = {"kind": "node", "batch": _BATCH_EXAMPLE, "name": "blur1"}

_KIND_EXAMPLES: dict[SelectorKind, dict[str, Any]] = {
    "selection": {"kind": "selection"},
    "wiretap": {"kind": "wiretap", "id": "/projects/P/ws/Lib/Reel/clip"},
    "path": {"kind": "path", "path": ["Library", "Reel", "clip"]},
    "segment": {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": "/projects/P/ws/Lib/Seq"},
        "version": 0,
        "track": 0,
        "record_in": 0,
        "expect_source_name": "clip1",
    },
    "batch": _BATCH_EXAMPLE,
    "node": _NODE_EXAMPLE,
    "action_node": {"kind": "action_node", "node": _NODE_EXAMPLE, "name": "axis1"},
    "desktop": {"kind": "desktop"},
    "timeline": {"kind": "timeline"},
}

_KIND_KEYS: dict[SelectorKind, frozenset[str]] = {
    kind: frozenset({"kind", *fields}) for kind, fields in _KIND_FIELDS.items()
}


def describe_kind(kind: SelectorKind) -> dict[str, Any]:
    """Returns `{"kind", "label", "fields", "example"}` as published by flame_context's
    selectorVocabulary and flame_catalog's per-op `accepts`. `fields` maps each
    key (besides `kind`) to `{"type", "description", "required"}`; `example` is
    a minimal valid selector (optional fields left out)."""
    optional = _OPTIONAL_FIELDS.get(kind, frozenset())
    return {
        "kind": kind,
        "label": selector_label(kind),
        "fields": {
            name: {
                "type": type_name,
                "description": description,
                "required": name not in optional,
            }
            for name, (type_name, description) in _KIND_FIELDS[kind].items()
        },
        "example": copy.deepcopy(_KIND_EXAMPLES[kind]),
    }


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_non_empty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _nested_issues(
    sel: Mapping[str, Any], key: str, accepted: tuple[SelectorKind, ...], kind: SelectorKind
) -> list[str]:
    nested = sel.get(key)
    if not isinstance(nested, Mapping):
        return [f"selector.{key} must be an object for kind {kind!r}"]
    return [f"{key}.{issue}" for issue in validate_selector(nested, accepted)]


def _validate_path(sel: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    path = sel.get("path")
    if not isinstance(path, list) or len(path) == 0 or not all(_is_non_empty_str(p) for p in path):
        issues.append("selector.path must be a non-empty list of non-empty strings for kind 'path'")
    if "root" in sel and sel["root"] not in _PATH_ROOTS:
        issues.append(f"selector.root must be one of {list(_PATH_ROOTS)}, got {sel['root']!r}")
    return issues


def _validate_segment(sel: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    parent = sel.get("parent")
    if not isinstance(parent, Mapping):
        issues.append("selector.parent must be an object for kind 'segment'")
    elif parent.get("kind") == "segment":
        issues.append("selector.parent must not itself be a 'segment' selector")
    else:
        issues.extend(
            f"parent.{issue}" for issue in validate_selector(parent, _SEGMENT_PARENT_KINDS)
        )
    for field in ("version", "track"):
        if not _is_non_negative_int(sel.get(field)):
            issues.append(f"selector.{field} must be a non-negative integer")
    given = [field for field in ("index", "record_in") if field in sel]
    if len(given) != 1:
        issues.append("selector for kind 'segment' needs exactly one of index / record_in")
    for field in given:
        if not _is_non_negative_int(sel[field]):
            issues.append(f"selector.{field} must be a non-negative integer")
    if "expect_source_name" in sel and not _is_non_empty_str(sel["expect_source_name"]):
        issues.append("selector.expect_source_name must be a non-empty string")
    return issues


def _validate_batch(sel: Mapping[str, Any]) -> list[str]:
    given = [key for key in ("uid", "name") if key in sel]
    if len(given) != 1:
        return ["selector for kind 'batch' needs exactly one of uid / name"]
    if not _is_non_empty_str(sel[given[0]]):
        return [f"selector.{given[0]} must be a non-empty string for kind 'batch'"]
    return []


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_node(sel: Mapping[str, Any]) -> list[str]:
    issues = _validate_named_child(sel, "batch", "batch", "node")
    if "pos" in sel:
        pos = sel["pos"]
        if not (isinstance(pos, list) and len(pos) == 2 and all(_is_int(p) for p in pos)):
            issues.append("selector.pos must be a list of two integers [pos_x, pos_y]")
    return issues


def _validate_named_child(
    sel: Mapping[str, Any], parent_key: str, parent_kind: SelectorKind, kind: SelectorKind
) -> list[str]:
    issues = _nested_issues(sel, parent_key, (parent_kind,), kind)
    if not _is_non_empty_str(sel.get("name")):
        issues.append(f"selector.name must be a non-empty string for kind {kind!r}")
    return issues


def validate_selector(sel: object, accepted: tuple[SelectorKind, ...]) -> list[str]:
    """Returns a list of human-readable problems with `sel` (empty = valid).

    Structural only: this never touches Flame. It only checks the JSON shape against
    the selector kinds declared above and the op's own `accepts`.
    """
    if not isinstance(sel, Mapping):
        return [f"selector must be an object, got {type(sel).__name__}"]

    kind_raw = sel.get("kind")
    if kind_raw not in get_args(SelectorKind):
        return [f"selector.kind must be one of {sorted(get_args(SelectorKind))}, got {kind_raw!r}"]
    kind = cast(SelectorKind, kind_raw)

    issues: list[str] = []
    if kind not in accepted:
        accepted_desc = ", ".join(accepted) if accepted else "(none)"
        issues.append(f"selector.kind {kind!r} is not accepted here (accepts: {accepted_desc})")

    extra = set(sel.keys()) - _KIND_KEYS[kind]
    if extra:
        issues.append(f"selector has unexpected key(s) for kind {kind!r}: {sorted(extra)}")

    match kind:
        case "selection":
            pass
        case "wiretap":
            if not _is_non_empty_str(sel.get("id")):
                issues.append("selector.id must be a non-empty string for kind 'wiretap'")
        case "path":
            issues.extend(_validate_path(sel))
        case "segment":
            issues.extend(_validate_segment(sel))
        case "batch":
            issues.extend(_validate_batch(sel))
        case "node":
            issues.extend(_validate_node(sel))
        case "action_node":
            issues.extend(_validate_named_child(sel, "node", "node", kind))
        case "desktop" | "timeline":
            pass
        case _ as unreachable:
            assert_never(unreachable)
    return issues


# The actual resolution logic (the loop over path segments, the
# find_by_wiretap_node_id try/except, ...) lives in execution.py's prelude as
# real Python functions (_resolve_selection/_resolve_wiretap/_resolve_path/
# _resolve_segment/_resolve_batch/_resolve_node/_resolve_action_node), next to
# `_v`/`_j`/`_OpError`. Every function here emits only single-line function-call
# assignments (a nested selector adds one line per level). No selector kind's
# generated code contains a loop/comprehension, so registry.py's write-op
# pacing guard cannot be tripped by selector resolution, and a loop cannot hide
# in a non-first `accepts` kind.


def _resolve_selection_code(var: str) -> str:
    return f"{var} = _resolve_selection()"


def _resolve_wiretap_code(sel: Mapping[str, Any], var: str) -> str:
    return f"{var} = _resolve_wiretap({sel['id']!r})"


def _resolve_path_code(sel: Mapping[str, Any], var: str) -> str:
    root = sel.get("root", "libraries")
    if root == "libraries":
        return f"{var} = _resolve_path({sel['path']!r})"
    return f"{var} = _resolve_path({sel['path']!r}, {root!r})"


def _resolve_segment_code(sel: Mapping[str, Any], var: str) -> str:
    parent_var = f"{var}__parent"
    parent_line = resolve_code(sel["parent"], parent_var)
    resolver, position = (
        ("_resolve_segment_at", sel["record_in"])
        if "record_in" in sel
        else ("_resolve_segment", sel["index"])
    )
    expect = f", {sel['expect_source_name']!r}" if "expect_source_name" in sel else ""
    call_line = (
        f"{var} = {resolver}("
        f"{parent_var}, {sel['version']!r}, {sel['track']!r}, {position!r}{expect})"
    )
    return parent_line + "\n" + call_line


def _resolve_batch_code(sel: Mapping[str, Any], var: str) -> str:
    return f"{var} = _resolve_batch({sel.get('uid')!r}, {sel.get('name')!r})"


def _resolve_node_code(sel: Mapping[str, Any], var: str) -> str:
    batch_var = f"{var}__batch"
    batch_line = resolve_code(sel["batch"], batch_var)
    pos = f", {list(sel['pos'])!r}" if "pos" in sel else ""
    return batch_line + "\n" + f"{var} = _resolve_node({batch_var}, {sel['name']!r}{pos})"


def _resolve_action_node_code(sel: Mapping[str, Any], var: str) -> str:
    node_var = f"{var}__node"
    node_lines = resolve_code(sel["node"], node_var)
    return node_lines + "\n" + f"{var} = _resolve_action_node({node_var}, {sel['name']!r})"


def _resolve_desktop_code(var: str) -> str:
    return f"{var} = _resolve_desktop()"


def _resolve_timeline_code(var: str) -> str:
    return f"{var} = _resolve_timeline()"


def resolve_code(sel: Mapping[str, Any], var: str) -> str:
    """Returns Python source that resolves `sel` and binds it to the local name `var`.

    It calls the matching `_resolve_*` prelude function
    (execution.py's `SELECTOR_HELPER`). Every value taken from `sel` is
    embedded via `!r` (repr) -- never string-concatenated -- so a selector
    value can never break out of its literal (tests/test_selectors.py
    exercises this directly). A nested selector (segment.parent, node.batch,
    action_node.node) is resolved first into `<var>__parent` / `<var>__batch` /
    `<var>__node`, which the op's own code may read.

    Callers must have already run `validate_selector` (or otherwise be sure
    `sel` is well-formed) -- this function trusts the shape of `sel` and
    raises a plain `ValueError` (a server bug, not a user-facing ErrorCode)
    if `sel["kind"]` isn't one of the known kinds.
    """
    kind_raw = sel.get("kind")
    if kind_raw not in get_args(SelectorKind):
        raise ValueError(f"resolve_code: invalid selector kind {kind_raw!r}")
    kind = cast(SelectorKind, kind_raw)
    match kind:
        case "selection":
            return _resolve_selection_code(var)
        case "wiretap":
            return _resolve_wiretap_code(sel, var)
        case "path":
            return _resolve_path_code(sel, var)
        case "segment":
            return _resolve_segment_code(sel, var)
        case "batch":
            return _resolve_batch_code(sel, var)
        case "node":
            return _resolve_node_code(sel, var)
        case "action_node":
            return _resolve_action_node_code(sel, var)
        case "desktop":
            return _resolve_desktop_code(var)
        case "timeline":
            return _resolve_timeline_code(var)
        case _ as unreachable:
            assert_never(unreachable)
