"""Which libraries an op's arguments name, so `execution.build_exec_code` can have
a closed one opened before the op resolves its selectors (`library_open_prelude`).

A closed library reads as empty, and Flame opens it by itself when a wiretap id inside it is
resolved, so the decision is made before that, from the arguments alone:
  - a `wiretap` selector (or a `wiretapId` of a recorded entry): the ids hold the library's uid as
    one of their `/`-separated parts; the Flame side matches it against the uids in the list of
    libraries (reading a uid there does not open anything);
  - a `path` selector with the default root: its first name is a library's name;
  - a library name in the reserved argument `_library` (the checkpoint library of the timeline
    checkpoints, `desktop.delete`'s stash and `journal.purge_checkpoints`);
  - a recorded library (`kind: 'library'`, `type: 'PyLibrary'`) by uid: the revert of a
    `library.create` and of a copy that went into a library.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

LIBRARY_NAME_ARG: Final[str] = "_library"
"""The reserved argument that carries a library's name to an op (the checkpoint library:
`timeline_checkpoints.LIBRARY_ARG`); the Flame side finds the library by it."""


def library_refs(args: Mapping[str, Any]) -> dict[str, list[str]]:
    uids: set[str] = set()
    names: set[str] = set()
    wiretap_ids: set[str] = set()

    def text(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            kind = node.get("kind")
            if kind == "wiretap" and text(node.get("id")):
                wiretap_ids.add(str(node["id"]))
            elif kind == "path" and node.get("root", "libraries") == "libraries":
                path = node.get("path")
                if isinstance(path, list) and path and text(path[0]):
                    names.add(str(path[0]))
            if text(node.get(LIBRARY_NAME_ARG)):
                names.add(str(node[LIBRARY_NAME_ARG]))
            if text(node.get("wiretapId")):
                wiretap_ids.add(str(node["wiretapId"]))
            if (kind == "library" or node.get("type") == "PyLibrary") and text(node.get("uid")):
                uids.add(str(node["uid"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(args)
    if args.get("kind") == "library":
        created = args.get("_created")
        if isinstance(created, Mapping):
            for item in created.get("items") or []:
                if isinstance(item, Mapping) and text(item.get("uid")):
                    uids.add(str(item["uid"]))
    return {"uids": sorted(uids), "names": sorted(names), "wiretapIds": sorted(wiretap_ids)}


def has_refs(refs: Mapping[str, list[str]]) -> bool:
    return any(refs.values())


def merge_library_reports(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Add the libraries `source` (an earlier Flame call of the same op: the pre-read of a write,
    the plan of a revert) opened and the notes about them to `target`, unless `target` has them:
    each call opens and closes by itself, so the call that carries the answer may have found the
    library already open (it could not be closed again earlier) and says nothing."""
    rows = list(target.get("librariesOpened") or [])
    seen = {r.get("uid") for r in rows if isinstance(r, dict)}
    added = [
        r
        for r in source.get("librariesOpened") or []
        if isinstance(r, dict) and r.get("uid") not in seen
    ]
    if not added:
        return
    target["librariesOpened"] = [*rows, *added]
    notes = list(target.get("notes") or [])
    notes += [n for n in source.get("notes") or [] if n not in notes]
    target["notes"] = notes


def merge_into_envelope(env: dict[str, Any], source: Mapping[str, Any]) -> None:
    """`merge_library_reports` into the result of an envelope that ends a call early (a refusal, an
    answer that never came): a result that is None becomes one when there is something to tell."""
    if not source.get("librariesOpened"):
        return
    if not isinstance(env.get("result"), dict):
        env["result"] = {}
    merge_library_reports(env["result"], source)
