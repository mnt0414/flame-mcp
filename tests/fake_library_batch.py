"""Fake library and Batch-group copy calls, kept in a module of their own.

Adds to the fake Flame tree (`fake_media_tree`) what 2025.2.4 does for `workspace.create_library`
and for `flame.media_panel.copy` of a Batch group into a library / a library folder / a reel / the
Desktop. Measured on Flame 2025.2.4 (54 steps, 2 runs, same values). Where
nothing was measured the fake RAISES (`NOT_MEASURED`), never does something plain: the fake is not
allowed to be more tolerant than Flame.

What it encodes
  - `create_library(name)` returns a PyLibrary at the END of `libraries`; a name that is taken
    becomes `<name> 1` (a new uid, no exception).
  - `media_panel.copy(PyBatch, library or folder)` answers a list holding ONE new PyBatch (new uid,
    same name) that the destination lists in `children` (and a library also in `batch_groups`). The
    source (its uid, its nodes, the current group) is untouched. A PyBatch IN a library reads
    `nodes` as an empty list (its reels / shelf reels and their clips read fine), has no
    `get_wiretap_node_id` (the attribute is None, calling it raises `TypeError`), and raises
    `RuntimeError` when `start_frame` / `duration` is read. `flame.find_by_uid` is not asked to find
    it (the fake does not list it: it is not known whether Flame does).
  - `media_panel.copy(PyBatch, a LIBRARY reel)` answers the CLIPS of the group's schematic reels
    (PyClip), not a PyBatch.
  - `media_panel.copy(library PyBatch, desktop)` answers a list holding a new PyBatch (new uid, same
    name) on the Desktop whose nodes (names, types, sockets), schematic reels (clip names in order)
    and shelf reels equal the source's; the current Batch group does not move. It is not renamed
    even though the group it was copied from is still on the Desktop.
  - closed libraries (measured in 2 runs): `PyLibrary.opened` is True by default; `close()` answers
    True and the library reads `opened` False and lists NO children (`children`, `reels`, `clips`,
    `sequences`, `batch_groups` all empty; so does a folder in it); reading its name / uid in the
    list of libraries does not open it; `flame.find_by_wiretap_node_id` of the library or of
    anything inside it OPENS it (and finds the thing); `open()` opens it and answers True. NOT
    measured, so the fake refuses (`NOT_MEASURED`): copying into a closed library by an object
    obtained before. `open_fails` / `close_fails` (test hooks) make `open()` / `close()` answer
    False and change nothing.
  - a taken library name is renamed two ways (measured): no trailing digit `X` -> `X 1`; a trailing
    digit `X1` -> `X2` (the number goes up).
  - `media_panel.copy(PyBatch, ..., duplicate_action='replace')` is refused (`NOT_MEASURED`): only
    `add` was measured for a Batch group.
  - `flame.delete(PyBatch in a library, confirm=False)` answers True and the group leaves the
    library; deleting a library that holds things works.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import fake_media_tree as fmt
from fake_media_tree import (
    FakeAttr,
    PyBatch,
    PyClipNode,
    PyDesktop,
    PyFolder,
    PyLibrary,
    PyMediaPanel,
    PyNode,
    PyReel,
    PyWorkspace,
    adopt,
    copy_media,
    next_uid,
)

NOT_MEASURED = "fake: not measured on a real Flame"


class LibraryBatch(PyBatch):
    """A PyBatch inside a library (see the module docstring); `type(x).__name__` is 'PyBatch'."""

    @property
    def nodes(self) -> list[PyNode]:
        return []

    @nodes.setter
    def nodes(self, value: Any) -> None:
        self.hidden_nodes = list(value)

    @property
    def start_frame(self) -> Any:
        raise RuntimeError("Could not get the Batch setup start frame.")

    @start_frame.setter
    def start_frame(self, value: Any) -> None:
        self.hidden_start_frame = value

    @property
    def duration(self) -> Any:
        raise RuntimeError("Could not get the Batch setup duration.")

    @duration.setter
    def duration(self, value: Any) -> None:
        self.hidden_duration = value


LibraryBatch.__name__ = "PyBatch"


def is_library_batch(obj: Any) -> bool:
    return isinstance(obj, LibraryBatch)


def snapshot(batch: PyBatch) -> dict[str, Any]:
    """What a copy keeps of a group: nodes (name, type, sockets, the clip of a Clip node by name),
    reels / shelf reels with their clips, start frame and duration."""
    if is_library_batch(batch):
        nodes = list(batch.hidden_nodes)
        start, duration = batch.hidden_start_frame, batch.hidden_duration
    else:
        nodes = [
            {
                "clip": isinstance(n, PyClipNode),
                "name": str(n.name.get_value()),
                "type": str(n.type.get_value()),
                "sockets": copy.deepcopy(n._sockets),
                "clipName": str(n.clip.name.get_value())
                if isinstance(n, PyClipNode) and n.clip is not None
                else None,
            }
            for n in batch.nodes
        ]
        start, duration = batch.start_frame.get_value(), batch.duration.get_value()
    return {
        "name": str(batch.name.get_value()),
        "nodes": nodes,
        "reels": [(str(r.name.get_value()), list(r.children)) for r in batch.reels],
        "shelfReels": [(str(r.name.get_value()), list(r.children)) for r in batch.shelf_reels],
        "startFrame": start,
        "duration": duration,
    }


def _fill_reels(group: PyBatch, snap: dict[str, Any]) -> None:
    for key, kind, target in (
        ("reels", "Schematic", group.reels),
        ("shelfReels", "Shelf", group.shelf_reels),
    ):
        for name, clips in snap[key]:
            reel = PyReel(
                name,
                next_uid("reel"),
                f"/wt/{group.uid.get_value()}/{name}",
                reel_type=kind,
                owner=group,
            )
            for clip in clips:
                made = copy_media(
                    clip,
                    str(clip.name.get_value()),
                    f"{reel.get_wiretap_node_id()}/{next_uid('c')}",
                    next_uid("clip"),
                )
                adopt(reel, [made])
                reel._children.append(made)
            target.append(reel)


def _rebuild_nodes(group: PyBatch, snap: dict[str, Any]) -> list[PyNode]:
    clips = {str(c.name.get_value()): c for r in group.reels for c in r.children}
    nodes: list[PyNode] = []
    for row in snap["nodes"]:
        node: PyNode
        if row["clip"]:
            node = PyClipNode(row["name"], clips.get(row["clipName"]))
        else:
            node = PyNode(row["name"], row["type"])
        node._sockets = copy.deepcopy(row["sockets"])
        node._owner = group
        nodes.append(node)
    return nodes


def make_library_batch(snap: dict[str, Any]) -> LibraryBatch:
    group = LibraryBatch(snap["name"], next_uid("libbatch"))
    group.hidden_start_frame = snap["startFrame"]
    group.hidden_duration = snap["duration"]
    group.hidden_nodes = list(snap["nodes"])
    _fill_reels(group, snap)
    return group


def make_desktop_batch(desktop: PyDesktop, snap: dict[str, Any]) -> PyBatch:
    group = PyBatch(snap["name"], next_uid("bgback"))
    _fill_reels(group, snap)
    group.nodes = _rebuild_nodes(group, snap)
    group.start_frame.set_value(snap["startFrame"])
    group.duration.set_value(snap["duration"])
    group._desktop = desktop
    group.parent = desktop  # type: ignore[attr-defined]
    return group


def _new_group(source: PyBatch, destination: Any) -> PyBatch:
    snap = snapshot(source)
    if type(destination).__name__ == "PyDesktop":
        if not is_library_batch(source):
            raise RuntimeError(NOT_MEASURED + ": a Desktop group copied onto the Desktop")
        group = make_desktop_batch(destination, snap)
        destination.batch_groups.append(group)
        destination.sync_children()
        return group
    if is_library_batch(source):
        raise RuntimeError(NOT_MEASURED + ": a library group copied into a library or a folder")
    group = make_library_batch(snap)
    adopt(destination, [group])
    destination._children.append(group)
    return group


def copy_batch(sources: list[Any], destination: Any) -> list[Any]:
    if len(sources) != 1:
        raise RuntimeError(NOT_MEASURED + ": copying several Batch groups at once")
    if type(destination).__name__ in ("PyLibrary", "PyFolder") and in_closed_library(destination):
        raise RuntimeError(NOT_MEASURED + ": copying into a closed library")
    source = sources[0]
    kind = type(destination).__name__
    if kind == "PyReel" and type(destination.parent).__name__ == "PyLibrary":
        clips = [c for r in source.reels for c in r.children]
        copies: list[Any] = destination.receive(clips)
        return copies
    if kind in ("PyLibrary", "PyFolder", "PyDesktop"):
        return [_new_group(source, destination)]
    raise RuntimeError(NOT_MEASURED + f": a Batch group copied into a {kind}")


def in_closed_library(obj: Any) -> bool:
    """The object is a library that is closed, or sits (as a folder) in one."""
    current = obj
    for _ in range(12):
        if current is None:
            return False
        if type(current).__name__ == "PyLibrary":
            return not bool(current.opened.get_value())
        current = getattr(current, "parent", None)
    return False


def _visible(self: Any) -> list[Any]:
    return [] if in_closed_library(self) else list(self._children)


def _of_type(name: str) -> Any:
    def getter(self: Any) -> list[Any]:
        return [c for c in _visible(self) if type(c).__name__ == name]

    return property(getter)


def library_batch_groups(self: Any) -> list[Any]:
    return [c for c in _visible(self) if type(c).__name__ == "PyBatch"]


def install() -> None:
    PyLibrary.batch_groups = property(library_batch_groups)  # type: ignore[attr-defined]
    PyFolder.batch_groups = property(library_batch_groups)  # type: ignore[attr-defined]
    for cls in (PyLibrary, PyFolder):
        cls.children = property(_visible)  # type: ignore[assignment,method-assign]
        cls.reels = _of_type("PyReel")  # type: ignore[method-assign]
        cls.clips = _of_type("PyClip")  # type: ignore[method-assign]
        cls.sequences = _of_type("PySequence")  # type: ignore[method-assign]
    original_init = PyLibrary.__init__

    def library_init(self: PyLibrary, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.opened = FakeAttr(True)  # type: ignore[attr-defined]

    PyLibrary.__init__ = library_init  # type: ignore[method-assign]

    def library_close(self: PyLibrary) -> bool:
        if getattr(self, "close_fails", False):
            return False
        self.opened.set_value(False)  # type: ignore[attr-defined]
        return True

    def library_open(self: PyLibrary) -> bool:
        if getattr(self, "open_fails", False):
            return False
        self.opened.set_value(True)  # type: ignore[attr-defined]
        return True

    PyLibrary.close = library_close  # type: ignore[attr-defined]
    PyLibrary.open = library_open  # type: ignore[attr-defined]

    original_find = fmt.FakeFlame.find_by_wiretap_node_id

    def find_by_wiretap_node_id(self: Any, node_id: str) -> Any:
        """Asking for the id of a closed library, or of anything inside one, opens it."""
        for lib in self.projects.current_project.current_workspace.libraries:
            wiretap = lib.get_wiretap_node_id()
            inside = node_id == wiretap or node_id.startswith(wiretap + "/")
            if inside and not lib.opened.get_value():
                lib.open()
        return original_find(self, node_id)

    fmt.FakeFlame.find_by_wiretap_node_id = find_by_wiretap_node_id  # type: ignore[method-assign]
    original_copy = PyMediaPanel.copy

    def panel_copy(
        self: PyMediaPanel, source: Any, destination: Any, duplicate_action: str = "add"
    ) -> list[Any]:
        sources = list(source) if isinstance(source, (list, tuple)) else [source]
        if not any(type(s).__name__ == "PyBatch" for s in sources):
            return original_copy(self, source, destination, duplicate_action)
        if duplicate_action not in ("add", "replace"):
            raise RuntimeError("Invalid duplicate_action, expecting add or replace")
        if duplicate_action != "add":
            raise RuntimeError(NOT_MEASURED + ": duplicate_action " + repr(duplicate_action))
        if not all(type(s).__name__ == "PyBatch" for s in sources):
            raise RuntimeError(NOT_MEASURED + ": a Batch group together with clips")
        self.duplicate_actions.append(duplicate_action)
        self.copy_calls.append((source, destination))
        return copy_batch(sources, destination)

    PyMediaPanel.copy = panel_copy  # type: ignore[method-assign]

    def create_library(self: PyWorkspace, name: str) -> PyLibrary:
        """A taken name is renamed two ways (measured): `X` -> `X 1`, `X1` -> `X2`."""
        taken = {str(lib.name.get_value()) for lib in self._libraries}
        unique = name
        match = re.fullmatch(r"(.*?)(\d+)", name)
        count = int(match.group(2)) if match else 0
        while unique in taken:
            count += 1
            unique = f"{match.group(1)}{count}" if match else f"{name} {count}"
        uid = next_uid("lib")
        library = PyLibrary(unique, uid, f"/wt/lib/{uid}")
        adopt(self, [library])
        self._libraries.append(library)
        return library

    PyWorkspace.create_library = create_library  # type: ignore[method-assign]

    original_all = fmt.FakeFlame.all_objects
    original_delete = fmt.FakeFlame.delete

    def all_objects(self: Any) -> list[Any]:
        """A group inside a library is not listed: whether `find_by_uid` finds one is not known."""
        return [o for o in original_all(self) if not is_library_batch(o)]

    def delete(self: Any, obj: Any, confirm: bool = True) -> bool:
        if is_library_batch(obj):
            holder = obj.parent
            holder._children = [c for c in holder._children if c is not obj]
            return True
        return bool(original_delete(self, obj, confirm))

    fmt.FakeFlame.all_objects = all_objects  # type: ignore[method-assign]
    fmt.FakeFlame.delete = delete  # type: ignore[method-assign]


install()  # importing the module puts the library and Batch-copy behaviours on the fake classes
