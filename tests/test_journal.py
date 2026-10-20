"""journal.py: the JSONL store (append + flush, torn tail, 30-day sweep, directory override)
and the record -> state rules. No Flame, no bridge."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import journal as journal_mod
from flame_mcp.journal import (
    JOURNAL_DIR_ENV,
    JOURNAL_MAX_AGE_S,
    Index,
    Journal,
    attribute_values_equal,
    default_journal_dir,
    values_equal,
)


def intent(iid: str, group: str, *, reverts: str | None = None, **over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "intent",
        "id": iid,
        "group": group,
        "ts": "2026-09-29T00:00:00.000+00:00",
        "op": "node.set_attributes",
        "args": {},
        "target": {"kind": "node"},
        "before": {"a": 1},
        "after": {"a": 2},
    }
    if reverts is not None:
        record["reverts"] = reverts
    record.update(over)
    return record


def render_intent(iid: str, group: str) -> dict[str, Any]:
    """A `render.batch` intent: no `before` / `after` keys, `kind: render`."""
    return intent(iid, group, kind="render", op="render.batch", before={}, after={})


def outcome(iid: str, group: str, *, ok: bool = True, **over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "outcome",
        "id": iid,
        "group": group,
        "ok": ok,
        "applied": ["a"] if ok else [],
        "failed": {},
        "errorCode": None if ok else "FLAME_API_ERROR",
        "readBack": {"a": 2} if ok else None,
    }
    record.update(over)
    return record


def check(iid: str, group: str, verdict: str) -> dict[str, Any]:
    return {"type": "check", "id": iid, "group": group, "verdict": verdict}


def test_default_dir_follows_the_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(JOURNAL_DIR_ENV, str(tmp_path / "elsewhere"))
    assert default_journal_dir() == tmp_path / "elsewhere"
    assert Journal().directory == tmp_path / "elsewhere"
    monkeypatch.delenv(JOURNAL_DIR_ENV)
    assert default_journal_dir() == (
        Path.home() / "Library" / "Application Support" / "flame-mcp" / "journal"
    )


def test_append_is_flushed_to_the_file_before_the_journal_is_closed(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j")
    journal.append(intent("i1", "g1"))
    # a second reader (as another process would be) sees the line although nothing was closed
    other = Journal(tmp_path / "j")
    assert [r["id"] for r in other.read_all().records] == ["i1"]
    journal.close()


def test_appends_go_to_one_file_per_day_and_never_rewrite(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j")
    journal.append(intent("i1", "g1"))
    journal.append(outcome("i1", "g1"))
    files = journal.files()
    assert len(files) == 1 and files[0].name.startswith("journal_") and files[0].suffix == ".jsonl"
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    journal.close()


def test_a_torn_last_line_is_dropped_and_does_not_swallow_the_next_record(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j")
    journal.append(intent("i1", "g1"))
    journal.close()
    path = journal.files()[0]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type":"outcome","id":"i1","gro')  # the process died mid-line
    reader = Journal(tmp_path / "j").read_all()
    assert [r["type"] for r in reader.records] == ["intent"]
    assert reader.skipped_lines == 1

    again = Journal(tmp_path / "j")
    again.append(outcome("i1", "g1"))  # must start on a fresh line
    result = Journal(tmp_path / "j").read_all()
    assert [r["type"] for r in result.records] == ["intent", "outcome"]
    assert result.skipped_lines == 1
    again.close()


def test_lines_that_are_not_records_are_skipped(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j")
    journal.append(intent("i1", "g1"))
    journal.close()
    with open(journal.files()[0], "a", encoding="utf-8") as fh:
        fh.write("not json\n[1, 2]\n{}\n\n")
    result = journal.read_all()
    assert [r["id"] for r in result.records] == ["i1"]
    assert result.skipped_lines == 3


def test_reading_a_missing_directory_is_empty(tmp_path: Path) -> None:
    result = Journal(tmp_path / "never_made").read_all()
    assert result.records == [] and result.skipped_lines == 0


def test_cleanup_removes_only_files_older_than_30_days(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j")
    journal.append(intent("i1", "g1"))
    journal.close()
    directory = tmp_path / "j"
    old = directory / "journal_20250101.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    recent = directory / "journal_20250102.jsonl"
    recent.write_text("{}\n", encoding="utf-8")
    now = 2_000_000_000.0
    os.utime(old, (now - JOURNAL_MAX_AGE_S - 60, now - JOURNAL_MAX_AGE_S - 60))
    os.utime(recent, (now - JOURNAL_MAX_AGE_S + 60, now - JOURNAL_MAX_AGE_S + 60))
    fresh = journal.files()[-1]
    os.utime(fresh, (now, now))
    stray = directory / "notes.txt"
    stray.write_text("keep", encoding="utf-8")
    os.utime(stray, (0, 0))

    assert journal.cleanup_old(now=now) == 1
    assert not old.exists() and recent.exists() and fresh.exists() and stray.exists()


def test_the_retention_is_30_days() -> None:
    assert JOURNAL_MAX_AGE_S == 30 * 24 * 60 * 60


def test_unwritable_directory_raises_oserror(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        Journal(blocker / "sub").append(intent("i1", "g1"))


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (1, 1.0, True),
        (0.1, 0.1000000001, True),
        (0.1, 0.2, False),
        (True, 1, False),
        (False, False, True),
        ("a", "a", True),
        ("a", "b", False),
        ([1, 2.0], (1, 2), True),
        ([1, 2], [1, 3], False),
        ([1], [1, 2], False),
        (None, None, True),
        (None, 0, False),
    ],
)
def test_values_equal(a: Any, b: Any, expected: bool) -> None:
    assert values_equal(a, b) is expected


@pytest.mark.parametrize(
    ("key", "a", "b", "expected"),
    [
        ("colour_label", "Green", "green", True),
        ("schematic_colour_label", "Light Grey", "light grey", True),
        ("colour_label", "Light Grey", "lightgrey", False),
        ("colour_label", "", "Green", False),
        ("name", "Comp1", "comp1", False),
        ("pos_x", 1.0, 1.0000000001, True),
    ],
)
def test_attribute_values_equal_ignores_letter_case_only_for_colour_labels(
    key: str, a: Any, b: Any, expected: bool
) -> None:
    assert attribute_values_equal(key, a, b) is expected


def state_of(*records: dict[str, Any], group: str = "g1") -> str:
    return Index(list(records)).group_state(group)


def test_an_intent_with_no_outcome_is_unknown_and_in_doubt() -> None:
    index = Index([intent("i1", "g1")])
    assert index.group_state("g1") == "unknown"
    assert [v.id for v in index.in_doubt()] == ["i1"]


def test_outcome_states() -> None:
    assert state_of(intent("i1", "g1"), outcome("i1", "g1")) == "landed"
    assert state_of(intent("i1", "g1"), outcome("i1", "g1", ok=False)) == "failed"
    assert (
        state_of(intent("i1", "g1"), outcome("i1", "g1", ok=False, errorCode="CONFLICT"))
        == "conflict"
    )
    assert (
        state_of(
            intent("i1", "g1"), outcome("i1", "g1", ok=False, applied=["a"], failed={"b": "x"})
        )
        == "partial"
    )


def test_a_render_outcome_that_came_out_short_reads_partial_only_when_its_verdict_says_so() -> None:
    """A `render.batch` outcome has no applied keys.

    Its `verdict` is what says some frames were written (an outcome of a render that wrote none
    stays `failed`)."""
    short = outcome("i1", "g1", ok=False, errorCode="SILENT_NOOP", verdict="partial")
    assert state_of(render_intent("i1", "g1"), short) == "partial"
    none = outcome("i1", "g1", ok=False, errorCode="SILENT_NOOP", verdict="not_landed")
    assert state_of(render_intent("i1", "g1"), none) == "failed"
    assert state_of(render_intent("i1", "g1"), outcome("i1", "g1", verdict="landed")) == ("landed")


def test_a_render_intent_is_never_revertible() -> None:
    index = Index([render_intent("i1", "g1"), outcome("i1", "g1", verdict="landed", applied=[])])
    (view,) = index.groups["g1"]
    assert view.is_revertible() is False and view.revertible_keys() == []
    assert index.is_reverted("g1") is False


def test_check_verdicts_decide_an_intent_with_no_outcome() -> None:
    for verdict in ("landed", "not_landed", "partial", "diverged"):
        index = Index([intent("i1", "g1"), check("i1", "g1", verdict)])
        assert index.group_state("g1") == verdict
        assert index.in_doubt() == []


def test_an_unknown_check_leaves_the_intent_in_doubt() -> None:
    index = Index([intent("i1", "g1"), check("i1", "g1", "unknown")])
    assert index.group_state("g1") == "unknown"
    assert [v.id for v in index.in_doubt()] == ["i1"]


def test_records_for_an_unknown_intent_are_ignored() -> None:
    index = Index([outcome("ghost", "g9"), check("ghost", "g9", "landed")])
    assert index.groups == {}


def test_a_group_of_several_intents_shows_the_worst_or_partial() -> None:
    landed = [intent("i1", "g1"), outcome("i1", "g1")]
    failed = [intent("i2", "g1"), outcome("i2", "g1", ok=False)]
    unknown = [intent("i3", "g1")]
    assert state_of(*landed, *failed) == "partial"
    assert state_of(*landed, *unknown) == "unknown"
    assert state_of(*landed, intent("i4", "g1"), outcome("i4", "g1")) == "landed"


def test_a_landed_revert_group_makes_the_original_reverted() -> None:
    records = [
        intent("i1", "g1"),
        outcome("i1", "g1"),
        intent("i2", "g2", reverts="g1"),
        outcome("i2", "g2"),
    ]
    index = Index(records)
    assert index.group_state("g1") == "reverted"
    assert index.reverted_by("g1") == ["g2"]
    assert index.group_state("g2") == "landed"
    summary = index.group_summary("g2")
    assert summary["reverts"] == "g1" and summary["revertedBy"] == []


def test_a_failed_or_conflicting_revert_does_not_make_the_original_reverted() -> None:
    for bad in (outcome("i2", "g2", ok=False), outcome("i2", "g2", ok=False, errorCode="CONFLICT")):
        index = Index(
            [intent("i1", "g1"), outcome("i1", "g1"), intent("i2", "g2", reverts="g1"), bad]
        )
        assert index.group_state("g1") == "landed"


def test_reverting_a_revert_makes_the_original_stand_again() -> None:
    records = [
        intent("i1", "g1"),
        outcome("i1", "g1"),
        intent("i2", "g2", reverts="g1"),
        outcome("i2", "g2"),
        intent("i3", "g3", reverts="g2"),
        outcome("i3", "g3"),
    ]
    index = Index(records)
    assert index.group_state("g3") == "landed"
    assert index.group_state("g2") == "reverted"
    assert index.group_state("g1") == "landed"


def test_a_cycle_in_a_malformed_journal_does_not_recurse_forever() -> None:
    records = [
        intent("i1", "g1", reverts="g1"),
        outcome("i1", "g1"),
    ]
    assert Index(records).group_state("g1") in {"landed", "reverted"}


def test_revertible_keys_follow_the_outcome_or_a_landed_check() -> None:
    both = intent("i1", "g1", before={"a": 1, "b": 1}, after={"a": 2, "b": 2})
    partial = Index([both, outcome("i1", "g1", ok=False, applied=["a"], failed={"b": "x"})])
    assert partial.intents["i1"].revertible_keys() == ["a"]
    unchecked = Index([both])
    assert unchecked.intents["i1"].revertible_keys() == []
    landed = Index([both, check("i1", "g1", "landed")])
    assert landed.intents["i1"].revertible_keys() == ["a", "b"]
    not_landed = Index([both, check("i1", "g1", "not_landed")])
    assert not_landed.intents["i1"].revertible_keys() == []


def test_expected_after_prefers_what_flame_read_back() -> None:
    view = Index([intent("i1", "g1"), outcome("i1", "g1", readBack={"a": 2.0000001})]).intents["i1"]
    assert view.expected_after("a") == 2.0000001
    bare = Index([intent("i1", "g1"), check("i1", "g1", "landed")]).intents["i1"]
    assert bare.expected_after("a") == 2


def test_ids_are_prefixed_and_unique() -> None:
    ids = {journal_mod.new_id("g") for _ in range(50)}
    assert len(ids) == 50 and all(i.startswith("g_") for i in ids)


def group_record(group: str, label: str | None = None) -> dict[str, Any]:
    return {"type": "group", "group": group, "kind": "plan.run", "label": label, "steps": []}


def plan_records() -> list[dict[str, Any]]:
    return [
        group_record("g1", "the plan"),
        intent("i1", "g1"),
        outcome("i1", "g1"),
        intent("i2", "g1"),
        outcome("i2", "g1"),
    ]


def test_a_group_record_labels_the_group_and_is_not_an_intent() -> None:
    index = Index(plan_records())
    assert list(index.intents) == ["i1", "i2"]
    summary = index.group_summary("g1")
    assert summary["label"] == "the plan" and summary["intents"] == 2
    assert Index([intent("i1", "g9"), outcome("i1", "g9")]).group_summary("g9")["label"] is None


def test_a_group_of_two_landed_intents_is_reverted_only_when_both_are_put_back() -> None:
    both = [
        *plan_records(),
        intent("r2", "g2", reverts="g1", revertsIntent="i2"),
        outcome("r2", "g2"),
    ]
    index = Index(both)
    assert index.group_state("g1") == "partially_reverted"
    assert index.intent_reverted(index.intents["i2"]) and not index.intent_reverted(
        index.intents["i1"]
    )
    finished = [
        *both,
        intent("r1", "g3", reverts="g1", revertsIntent="i1"),
        outcome("r1", "g3"),
    ]
    done = Index(finished)
    assert done.group_state("g1") == "reverted"
    assert done.reverted_by("g1") == ["g2", "g3"]


def test_a_revert_intent_that_failed_puts_nothing_back() -> None:
    records = [
        *plan_records(),
        intent("r2", "g2", reverts="g1", revertsIntent="i2"),
        outcome("r2", "g2", ok=False, errorCode="CONFLICT"),
    ]
    assert Index(records).group_state("g1") == "landed"


def test_a_revert_intent_without_revertsintent_covers_the_whole_group() -> None:
    """Records without `revertsIntent` name only the group."""
    records = [*plan_records(), intent("r2", "g2", reverts="g1"), outcome("r2", "g2")]
    assert Index(records).group_state("g1") == "reverted"


def test_reverting_a_partial_revert_makes_the_intent_stand_again() -> None:
    records = [
        *plan_records(),
        intent("r2", "g2", reverts="g1", revertsIntent="i2"),
        outcome("r2", "g2"),
        intent("x", "g3", reverts="g2"),
        outcome("x", "g3"),
    ]
    assert Index(records).group_state("g1") == "landed"
