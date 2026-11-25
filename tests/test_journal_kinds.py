"""The kind table of journal.check / journal.revert (`journal_revert.KINDS`)."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from flame_mcp import journal_revert
from flame_mcp.journal import (
    DESKTOP_WRITE_KINDS,
    KIND_DESKTOP_COPY,
    KIND_DESKTOP_CREATE,
    KIND_DESKTOP_DELETE,
    KIND_DESKTOP_IMPORT,
    KIND_DESKTOP_REVERT,
    LOOP_GROUP_KIND,
    Index,
    IntentView,
)
from flame_mcp.journal_revert import KINDS, KindSpec
from flame_mcp.ops import journal as ops_journal
from flame_mcp.render_run import RENDER_KIND
from flame_mcp.timeline_checkpoints import KIND as TIMELINE_CHECKPOINT_KIND

SRC = Path(journal_revert.__file__).parent

# Kinds a runner writes that have no row: the generic Desktop writes, a Desktop revert (told
# by `revertOf`), the loop parent's group record and the timeline checkpoint record.
NO_ROW = {
    KIND_DESKTOP_CREATE,
    KIND_DESKTOP_COPY,
    KIND_DESKTOP_REVERT,
    LOOP_GROUP_KIND,
    TIMELINE_CHECKPOINT_KIND,
}
# Kinds whose ops have no revert and no reason to give (see `KindSpec`).
NO_REVERT_NO_REASON = {"segment_connection_remove"}


def _kinds_written_by_runners() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        module = ".".join(("flame_mcp", *path.relative_to(SRC).with_suffix("").parts))
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "kind"
                    and isinstance(value, ast.Name)
                ):
                    resolved = getattr(importlib.import_module(module), value.id, None)
                    if isinstance(resolved, str):
                        found.add(resolved)
    return found


def test_every_kind_a_runner_writes_has_a_row_or_is_known_to_have_none() -> None:
    written = _kinds_written_by_runners() | set(DESKTOP_WRITE_KINDS)
    assert written - set(KINDS) == NO_ROW & written
    assert {RENDER_KIND, KIND_DESKTOP_DELETE, KIND_DESKTOP_IMPORT} <= written


def test_every_row_has_a_check_and_a_revert_or_a_reason_but_not_both() -> None:
    for kind, spec in KINDS.items():
        assert callable(spec.check), kind
        has_revert = spec.revert is not None
        has_reason = spec.irreversible is not None
        if kind in NO_REVERT_NO_REASON:
            assert not has_revert and not has_reason, kind
        else:
            assert has_revert != has_reason, kind


def test_effective_kind_of_a_desktop_revert_is_the_kind_it_reverts() -> None:
    def view(**intent: Any) -> IntentView:
        record: Any = {"id": "i", "group": "g", **intent}
        return IntentView(record)

    assert view(kind=RENDER_KIND).effective_kind == RENDER_KIND
    assert view(kind=KIND_DESKTOP_REVERT, revertOf=KIND_DESKTOP_DELETE).effective_kind == (
        KIND_DESKTOP_DELETE
    )
    assert view(kind=KIND_DESKTOP_REVERT, revertOf=RENDER_KIND).effective_kind == (
        KIND_DESKTOP_REVERT
    )
    assert view(kind=KIND_DESKTOP_REVERT).effective_kind == KIND_DESKTOP_REVERT
    assert view().effective_kind is None
    assert view(kind=["x"]).effective_kind is None


# --- journal.check: which function checks which intent --------------------------------------

_CHECK_NAMES = ("desktop", "timeline", "fingerprint", "values")


@pytest.fixture
def checked_by(monkeypatch: pytest.MonkeyPatch) -> Any:
    def marker(name: str) -> Any:
        return lambda ctx, view: {"by": name}

    for kind in KINDS:
        monkeypatch.setitem(KINDS, kind, KindSpec(marker(kind)))
    monkeypatch.setattr(ops_journal, "check_desktop_intent", marker("desktop"))
    monkeypatch.setattr(ops_journal, "check_timeline_intent", marker("timeline"))
    monkeypatch.setattr(ops_journal, "_check_fingerprint_one", marker("fingerprint"))
    monkeypatch.setattr(ops_journal, "_check_values_one", marker("values"))

    def run(**intent: Any) -> str:
        record: Any = {"id": "i", "group": "g", **intent}
        view = IntentView(record)
        return str(ops_journal._check_one(SimpleNamespace(), view)["by"])

    return run


def test_a_kind_with_a_row_is_checked_by_it_before_anything_else(checked_by: Any) -> None:
    assert checked_by(kind=RENDER_KIND, fingerprintBefore="f", targetKind="timeline") == "render"
    assert checked_by(kind=KIND_DESKTOP_DELETE, fingerprintBefore="f") == KIND_DESKTOP_DELETE


def test_the_revert_of_a_desktop_write_is_checked_as_that_write(checked_by: Any) -> None:
    assert checked_by(kind=KIND_DESKTOP_REVERT, revertOf=KIND_DESKTOP_DELETE) == (
        KIND_DESKTOP_DELETE
    )
    assert checked_by(kind=KIND_DESKTOP_REVERT, revertOf=KIND_DESKTOP_IMPORT) == (
        KIND_DESKTOP_IMPORT
    )


def test_a_desktop_write_without_a_row_is_checked_before_a_fingerprint(checked_by: Any) -> None:
    assert checked_by(kind=KIND_DESKTOP_CREATE, fingerprintBefore="f") == "desktop"
    assert checked_by(kind=KIND_DESKTOP_REVERT, revertOf=KIND_DESKTOP_CREATE) == "desktop"
    assert checked_by(kind=KIND_DESKTOP_REVERT) == "desktop"


def test_a_fingerprint_write_goes_timeline_first_then_the_general_one(checked_by: Any) -> None:
    assert checked_by(kind="eval", fingerprintBefore="f", targetKind="timeline") == "timeline"
    assert checked_by(kind="eval", fingerprintBefore="f") == "fingerprint"
    assert checked_by(kind="eval", targetKind="timeline") == "values"
    assert checked_by(op="node.set_attributes") == "values"


# --- journal.revert --------------------------------------------------------------------------


@pytest.fixture
def reverted_by(monkeypatch: pytest.MonkeyPatch) -> Any:
    def marker(name: str) -> Any:
        return lambda ctx, index, group, views, force: SimpleNamespace(by=name, force=force)

    for kind, spec in list(KINDS.items()):
        if spec.revert is not None:
            monkeypatch.setitem(KINDS, kind, KindSpec(spec.check, marker(kind)))
    monkeypatch.setattr(
        journal_revert,
        "revert_desktop_group",
        lambda ctx, index, group, views: SimpleNamespace(by="generic", force=None),
    )

    def run(*kinds: str, group_kind: Any = None, force: bool = False) -> Any:
        records: list[Any] = [{"id": f"i{n}", "group": "g", "kind": k} for n, k in enumerate(kinds)]
        views = [IntentView(r) for r in records]
        index: Any = SimpleNamespace(group_meta={"g": {"kind": group_kind}})
        result = journal_revert._revert_desktop(
            SimpleNamespace(),
            index,
            "g",
            views,
            force,
        )
        return result

    return run


def test_a_desktop_group_is_reverted_by_its_kind_and_force_is_passed_on(reverted_by: Any) -> None:
    result = reverted_by(KIND_DESKTOP_IMPORT, force=True)
    assert (result.by, result.force) == (KIND_DESKTOP_IMPORT, True)


def test_of_several_kinds_in_a_group_the_first_row_in_the_table_reverts(reverted_by: Any) -> None:
    assert reverted_by(KIND_DESKTOP_IMPORT, KIND_DESKTOP_DELETE).by == KIND_DESKTOP_DELETE


def test_a_desktop_group_of_no_row_goes_to_the_generic_revert(reverted_by: Any) -> None:
    assert reverted_by(KIND_DESKTOP_CREATE).by == "generic"
    assert reverted_by(KIND_DESKTOP_CREATE, group_kind=["unhashable"]).by == "generic"


def test_a_group_record_kind_is_tried_before_the_kinds_of_the_writes(
    monkeypatch: pytest.MonkeyPatch, reverted_by: Any
) -> None:
    (group_kind, _), *_ = journal_revert._GROUP_KIND_REVERTS.items()
    monkeypatch.setitem(
        journal_revert._GROUP_KIND_REVERTS,
        group_kind,
        lambda ctx, index, group, views: SimpleNamespace(by="group_kind", force=None),
    )
    assert reverted_by(KIND_DESKTOP_DELETE, group_kind=group_kind).by == "group_kind"


def test_a_group_of_an_irreversible_kind_says_why() -> None:
    records = [{"type": "intent", "id": "i1", "group": "g1", "kind": RENDER_KIND, "op": "x"}]
    error = journal_revert._nothing_to_revert(Index(records), "g1")
    assert error.code == "PRECONDITION_UNMET"
    assert str(error) == (
        "group g1 is a render: its output (the files on disk, the clips in the shelf reel) "
        "cannot be undone, so there is nothing to revert"
    )
