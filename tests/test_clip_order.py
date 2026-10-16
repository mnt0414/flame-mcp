"""Tests for restoring the order of the Clip nodes in a checkpoint's reels.

A restore puts the Clip nodes of a checkpoint's `.batch` in the order the reels held their clips
when the checkpoint was taken, because `load_setup` rebuilds each reel from the file's Clip nodes in
the order they are WRITTEN there. The planner is pure (text + records in, new text out); the copy is
written next to the checkpoints and removed afterwards; the original checkpoint is never written.
`restore_checkpoint` wires it in: the load reads the copy, the copy is gone afterwards, and
`clipOrder` is in every result."""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import clip_order
from journal_test_helpers import BG1, Rig, make_rig

R1, R2 = "Schematic Reel 1", "Schematic Reel 2"
NODE_RE = re.compile(r"<Node>.*?</Node>", re.DOTALL)


def node_xml(kind: str, name: str, number: int) -> str:
    return (
        f'<Node><Res W="1920" H="1080" PxR="1" SF="2" PxF="143"/><Type>{kind}</Type>'
        f"<Name>{name}</Name><SchemName>{name}</SchemName><Number>{number}</Number>"
        "<PosX>1</PosX><PosY>2</PosY><Flags>0</Flags></Node>"
    )


def clip_node_xml(clip_name: str, reel: str) -> str:
    return (
        f"<Setup><Base><NAME>x</NAME></Base><State><ClipName>{clip_name}</ClipName>"
        f"<Offset>0</Offset><ReelName>{reel}</ReelName></State></Setup>"
    )


def rows_of(**reels: list[str] | None) -> list[dict[str, Any]]:
    return [
        {"name": name.replace("_", " "), "empty": not clips, "clips": clips, "sequences": []}
        for name, clips in reels.items()
    ]


def record(reel: str, *clips: str) -> dict[str, Any]:
    return {"name": reel, "empty": not clips, "clips": list(clips), "sequences": []}


def write_setup(
    root: Path,
    stem: str,
    nodes: list[tuple[str, str, str | None, str | None]],
    *,
    extra_in_batch: str = "",
) -> Path:
    """nodes: (type, node name, clip name, reel name); clip name / reel name only for 'Clip'
    (a None clip name writes no .clip_node file). Returns the .batch path."""
    root.mkdir(parents=True, exist_ok=True)
    directory = root / stem
    directory.mkdir()
    chunks = []
    for number, (kind, name, clip_name, reel) in enumerate(nodes, start=1):
        chunks.append(node_xml(kind, name, number))
        if kind == "Clip" and clip_name is not None and reel is not None:
            (directory / f"{name}.clip_node").write_text(
                clip_node_xml(clip_name, reel), encoding="utf-8"
            )
    (directory / "action1.action").write_bytes(b"\x00binary\xff payload")
    links = '<Links><Link InSocket="Front" InNodeId="1" OutSocket="Result" OutNodeId="2"/></Links>'
    text = (
        "<Setup><Header><Module>BATCH</Module></Header><Root><BatchName>T</BatchName></Root>"
        + "".join(chunks)
        + extra_in_batch
        + links
        + "<CompassList/></Setup>"
    )
    path = root / f"{stem}.batch"
    path.write_bytes(text.encode("utf-8"))
    return path


def clip(name: str, clip_name: str, reel: str) -> tuple[str, str, str | None, str | None]:
    return ("Clip", name, clip_name, reel)


def tree_hash(path: Path) -> dict[str, str]:
    base = path.parent / path.stem
    files = [path, *sorted(p for p in base.rglob("*") if p.is_file())]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def written_order(batch_text: str) -> list[str]:
    names = []
    for chunk in NODE_RE.findall(batch_text):
        if "<Type>Clip</Type>" in chunk:
            names.append(re.search(r"<Name>(.*?)</Name>", chunk, re.DOTALL).group(1))  # type: ignore[union-attr]
    return names


def prepare(batch: Path, rows: list[dict[str, Any]], work_root: Path) -> clip_order.Preparation:
    return clip_order.prepare(str(batch), rows, work_root)


def test_reversed_clip_nodes_are_put_back_in_the_recorded_order(tmp_path: Path) -> None:
    nodes = [clip(f"clip{n}", f"c{n}", R2) for n in (8, 6, 5, 4, 3, 2)]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    rows = [record(R2, *(f"c{n}" for n in (2, 3, 4, 5, 6, 8)))]
    prep = prepare(batch, rows, tmp_path / "work")
    try:
        assert prep.plan.needed is True and prep.plan.reordered == (R2,)
        assert prep.plan.node_order == tuple(f"clip{n}" for n in (2, 3, 4, 5, 6, 8))
        assert prep.copy is not None
        copied = prep.copy.batch_path.read_bytes().decode("utf-8")
        assert written_order(copied) == [f"clip{n}" for n in (2, 3, 4, 5, 6, 8)]
    finally:
        clip_order.remove_copy(prep.copy)


def test_two_reels_written_alternately_are_each_put_in_order_in_their_own_slots(
    tmp_path: Path,
) -> None:
    nodes = [
        clip("n1", "a", R1),
        ("Action", "action1", None, None),
        clip("n2", "x", R2),
        clip("n3", "b", R1),
        clip("n4", "y", R2),
    ]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    text = batch.read_bytes().decode("utf-8")
    plan = clip_order.plan_order(
        text,
        clip_order.read_clip_node_texts(text, batch.parent / "cp"),
        [record(R1, "b", "a"), record(R2, "y", "x")],
        "cp",
    )
    assert plan.needed is True and set(plan.reordered) == {R1, R2}
    assert plan.new_text is not None
    all_names = [
        re.search(r"<Name>(.*?)</Name>", c).group(1)  # type: ignore[union-attr]
        for c in NODE_RE.findall(plan.new_text)
    ]
    assert all_names == ["n3", "action1", "n4", "n1", "n2"]


def test_clips_with_the_same_name_keep_their_relative_order(tmp_path: Path) -> None:
    nodes = [
        clip("first3", "clip3", R1),
        clip("first1", "clip1", R1),
        clip("only2", "clip2", R1),
        clip("second1", "clip1", R1),
    ]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    rows = [record(R1, "clip1", "clip2", "clip3", "clip1")]
    prep = prepare(batch, rows, tmp_path / "work")
    try:
        assert prep.plan.node_order == ("first1", "only2", "first3", "second1")
    finally:
        clip_order.remove_copy(prep.copy)


def test_a_reel_whose_names_differ_from_the_record_is_skipped_with_the_reason(
    tmp_path: Path,
) -> None:
    nodes = [
        clip("n1", "a", R1),
        clip("n2", "b", R1),
        clip("n3", "q", R2),
        clip("n4", "p", R2),
    ]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    rows = [record(R1, "b", "a"), record(R2, "p", "p")]
    prep = prepare(batch, rows, tmp_path / "work")
    try:
        assert prep.plan.reordered == (R1,)
        [skip] = prep.plan.skipped
        assert skip.reel == R2 and "differ" in skip.reason
        assert prep.plan.node_order == ("n2", "n1", "n3", "n4")  # R2 left as written
    finally:
        clip_order.remove_copy(prep.copy)


def test_a_reel_the_record_does_not_have_or_without_clip_names_is_skipped(
    tmp_path: Path,
) -> None:
    nodes = [clip("n1", "a", R1), clip("n2", "b", R1), clip("n3", "q", R2), clip("n4", "p", R2)]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    rows = [record(R1, "b", "a"), {"name": R2, "empty": None, "clips": None, "sequences": None}]
    prep = prepare(batch, rows, tmp_path / "work")
    try:
        assert prep.plan.reordered == (R1,)
        assert [s.reel for s in prep.plan.skipped] == [R2]
    finally:
        clip_order.remove_copy(prep.copy)
    prep = prepare(batch, [record(R1, "b", "a")], tmp_path / "work")
    try:
        assert [s.reel for s in prep.plan.skipped] == [R2]
    finally:
        clip_order.remove_copy(prep.copy)


def test_a_clip_node_without_a_readable_clip_node_file_makes_its_reel_unmatched(
    tmp_path: Path,
) -> None:
    nodes = [
        clip("n1", "a", R1),
        ("Clip", "n2", None, None),
        clip("n3", "b", R1),
    ]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    prep = prepare(batch, [record(R1, "b", "a", "c")], tmp_path / "work")
    try:
        assert prep.plan.needed is False and prep.copy is None
        assert "n2" in prep.plan.unreadable
    finally:
        clip_order.remove_copy(prep.copy)


def test_an_order_that_already_matches_needs_no_copy(tmp_path: Path) -> None:
    nodes = [clip("n1", "a", R1), clip("n2", "b", R1)]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    work = tmp_path / "work"
    prep = prepare(batch, [record(R1, "a", "b")], work)
    assert prep.plan.needed is False and prep.plan.new_text is None and prep.copy is None
    assert not work.exists() or list(work.iterdir()) == []


def test_everything_but_the_clip_nodes_is_byte_identical_in_the_copy(tmp_path: Path) -> None:
    nodes = [
        ("Action", "action1", None, None),
        clip("n2", "b", R1),
        ("Mux", "mux1", None, None),
        clip("n1", "a", R1),
    ]
    batch = write_setup(tmp_path / "src", "cp", nodes, extra_in_batch="<Odd>  keep  </Odd>")
    original = batch.read_bytes().decode("utf-8")
    prep = prepare(batch, [record(R1, "a", "b")], tmp_path / "work")
    try:
        assert prep.copy is not None
        copied = prep.copy.batch_path.read_bytes().decode("utf-8")
        assert copied != original

        def skeleton(text: str) -> str:
            return NODE_RE.sub(
                lambda m: "[CLIP]" if "<Type>Clip</Type>" in m.group(0) else m.group(0), text
            )

        assert skeleton(copied) == skeleton(original)
        assert sorted(NODE_RE.findall(copied)) == sorted(NODE_RE.findall(original))
        assert copied.split("</Node>")[-1] == original.split("</Node>")[-1]  # Links and the rest
        src_dir, dst_dir = batch.parent / "cp", prep.copy.directory / "cp"
        for source in src_dir.rglob("*"):
            if source.is_file():
                assert (dst_dir / source.relative_to(src_dir)).read_bytes() == source.read_bytes()
    finally:
        clip_order.remove_copy(prep.copy)


def test_the_original_checkpoint_is_never_written(tmp_path: Path) -> None:
    nodes = [clip("n2", "b", R1), clip("n1", "a", R1)]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    before = tree_hash(batch)
    prep = prepare(batch, [record(R1, "a", "b")], tmp_path / "work")
    assert prep.copy is not None
    clip_order.remove_copy(prep.copy)
    assert tree_hash(batch) == before


def test_a_reference_to_the_checkpoint_name_inside_the_batch_file_blocks_the_reorder(
    tmp_path: Path,
) -> None:
    nodes = [clip("n2", "b", R1), clip("n1", "a", R1)]
    batch = write_setup(tmp_path / "src", "cp", nodes, extra_in_batch="<Path>/x/cp/n1</Path>")
    work = tmp_path / "work"
    prep = prepare(batch, [record(R1, "a", "b")], work)
    assert prep.plan.needed is False and prep.copy is None
    [skip] = prep.plan.skipped
    assert skip.reel is None and "cp" in skip.reason and "reference" in skip.reason


def test_the_copy_is_removed_and_only_the_copy(tmp_path: Path) -> None:
    nodes = [clip("n2", "b", R1), clip("n1", "a", R1)]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    work = tmp_path / "work"
    work.mkdir()
    bystander = work / "someone_elses.batch"
    bystander.write_text("keep")
    prep = prepare(batch, [record(R1, "a", "b")], work)
    assert prep.copy is not None and prep.copy.batch_path.is_file()
    assert prep.copy.batch_path.parent.parent == work
    clip_order.remove_copy(prep.copy)
    assert not prep.copy.directory.exists()
    assert bystander.read_text() == "keep" and batch.is_file()
    clip_order.remove_copy(prep.copy)  # twice is fine
    clip_order.remove_copy(None)


def test_a_batch_file_that_is_not_utf8_raises_and_leaves_no_copy(tmp_path: Path) -> None:
    nodes = [clip("n2", "b", R1), clip("n1", "a", R1)]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    batch.write_bytes(batch.read_bytes() + b"\xff\xfe")
    work = tmp_path / "work"
    with pytest.raises(UnicodeDecodeError):
        prepare(batch, [record(R1, "a", "b")], work)
    assert not work.exists() or list(work.iterdir()) == []


def test_clip_names_are_compared_after_xml_unescaping(tmp_path: Path) -> None:
    nodes = [clip("n2", "b &amp; c", R1), clip("n1", "a", R1)]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    prep = prepare(batch, [record(R1, "a", "b & c")], tmp_path / "work")
    try:
        assert prep.plan.node_order == ("n1", "n2")
    finally:
        clip_order.remove_copy(prep.copy)


def test_the_report_names_what_happened(tmp_path: Path) -> None:
    nodes = [clip("n2", "b", R1), clip("n1", "a", R1), clip("n3", "z", R2)]
    batch = write_setup(tmp_path / "src", "cp", nodes)
    prep = prepare(batch, [record(R1, "a", "b")], tmp_path / "work")
    try:
        out = clip_order.report(prep, str(batch), None)
        assert out["needed"] is True and out["reordered"] == [R1]
        assert out["skipped"] == [{"reel": R2, "reason": out["skipped"][0]["reason"]}]
        assert out["loadedFrom"] == str(prep.copy.batch_path)  # type: ignore[union-attr]
        assert out["error"] is None
    finally:
        clip_order.remove_copy(prep.copy)
    failed = clip_order.report(None, str(batch), "OSError: boom")
    assert failed == {
        "needed": None,
        "reordered": [],
        "skipped": [],
        "loadedFrom": str(batch),
        "error": "OSError: boom",
    }


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


def take_checkpoint(rig: Rig) -> dict[str, Any]:
    env = rig.do("batch.checkpoint", {"target": BG1})
    assert env["ok"] is True, env
    result: dict[str, Any] = env["result"]
    return result


def fake_prepare(
    seen: list[Path],
    *,
    body: str | None = None,
    skipped: tuple[clip_order.SkippedReel, ...] = (),
) -> Any:
    """A stand-in for `clip_order.prepare` that 'reorders' by copying the (JSON) checkpoint the
    fake Flame can load; `body` replaces the copy's content."""

    def prepare_stub(
        checkpoint_path: str, rows: list[dict[str, Any]], work_root: Path
    ) -> clip_order.Preparation:
        directory = work_root / f"{clip_order.COPY_PREFIX}stub"
        directory.mkdir(parents=True)
        copy_path = directory / Path(checkpoint_path).name
        copy_path.write_text(
            body if body is not None else Path(checkpoint_path).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        seen.append(copy_path)
        plan = clip_order.OrderPlan(
            needed=True,
            new_text="x",
            node_order=("n1",),
            reordered=(R1,),
            skipped=skipped,
            unreadable=(),
        )
        return clip_order.Preparation(plan, clip_order.OrderedCopy(work_root, directory, copy_path))

    return prepare_stub


def ordered_dir(rig: Rig) -> Path:
    return rig.journal.directory / "checkpoints" / "ordered"


def test_restore_loads_the_ordered_copy_and_removes_it_afterwards(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = take_checkpoint(rig)
    rig.handles["bg1"].create_node("Mux")
    seen: list[Path] = []
    monkeypatch.setattr(clip_order, "prepare", fake_prepare(seen))
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True, env
    [copy_path] = seen
    assert rig.handles["bg1"].load_calls[-1] == str(copy_path)
    assert not copy_path.exists() and list(ordered_dir(rig).iterdir()) == []
    order = env["result"]["clipOrder"]
    assert order["needed"] is True and order["reordered"] == [R1] and order["error"] is None
    assert order["loadedFrom"] == str(copy_path)
    assert env["result"]["path"] == saved["path"]
    assert Path(saved["path"]).is_file()


def test_the_copy_is_removed_when_the_load_fails_too(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = take_checkpoint(rig)
    seen: list[Path] = []
    monkeypatch.setattr(clip_order, "prepare", fake_prepare(seen, body="not json"))
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR"
    [copy_path] = seen
    assert rig.handles["bg1"].load_calls[-1] == str(copy_path)
    assert not copy_path.exists() and list(ordered_dir(rig).iterdir()) == []
    assert env["result"]["clipOrder"]["loadedFrom"] == str(copy_path)
    assert "copyKept" not in env["result"]["clipOrder"]
    assert env["result"]["preCheckpoint"]["id"]


def test_a_failure_while_preparing_loads_the_original_and_reports_the_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = take_checkpoint(rig)

    def broken(*_: Any) -> clip_order.Preparation:
        raise OSError("disk on fire")

    monkeypatch.setattr(clip_order, "prepare", broken)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True, env
    assert rig.handles["bg1"].load_calls[-1] == saved["path"]
    order = env["result"]["clipOrder"]
    assert order["error"] == "OSError: disk on fire" and order["loadedFrom"] == saved["path"]


def test_a_checkpoint_that_needs_no_reorder_is_loaded_as_it_is(rig: Rig) -> None:
    saved = take_checkpoint(rig)  # the fake's .batch is JSON: no Clip nodes to put in order
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True, env
    assert rig.handles["bg1"].load_calls[-1] == saved["path"]
    order = env["result"]["clipOrder"]
    assert order["needed"] is False and order["error"] is None
    assert order["loadedFrom"] == saved["path"]
    assert not ordered_dir(rig).exists() or list(ordered_dir(rig).iterdir()) == []


def test_load_setup_of_a_file_has_no_clip_order(rig: Rig, tmp_path: Path) -> None:
    saved = take_checkpoint(rig)
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    assert env["ok"] is True, env
    assert "clipOrder" not in env["result"]


def test_the_copy_is_kept_when_the_load_ended_without_an_answer(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = take_checkpoint(rig)
    seen: list[Path] = []
    monkeypatch.setattr(clip_order, "prepare", fake_prepare(seen))
    rig.exec.fault("batch.load_setup", "TIMEOUT", when="after")
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT"
    [copy_path] = seen
    assert copy_path.is_file()  # Flame may still be reading it
    order = env["result"]["clipOrder"]
    assert order["copyKept"] is True
    assert order["copyPath"] == str(copy_path.parent) and order["copyKeptReason"]
    assert "cleanupError" not in order


ODD_STRUCTURES = {
    "nested": "<Node><Type>Odd</Type><Node>inner</Node>",
    "stray_close": "</Node>",
    "attributed": '<Node id="1"><Type>Odd</Type><Name>odd</Name></Node>',
    "self_closing": "<Node/>",
}


@pytest.mark.parametrize("form", sorted(ODD_STRUCTURES))
def test_a_setup_with_nested_or_attributed_node_elements_is_not_reordered(
    tmp_path: Path, form: str
) -> None:
    nodes = [clip("n2", "b", R1), clip("n1", "a", R1)]
    batch = write_setup(tmp_path / "src", "cp", nodes, extra_in_batch=ODD_STRUCTURES[form])
    work = tmp_path / "work"
    prep = prepare(batch, [record(R1, "a", "b")], work)
    assert prep.plan.needed is False and prep.copy is None and prep.plan.new_text is None
    [skip] = prep.plan.skipped
    assert skip.reel is None and "structure" in skip.reason
    assert not work.exists() or list(work.iterdir()) == []


def test_remove_copy_refuses_anything_that_is_not_a_directory_write_copy_made(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    (root / "important").mkdir(parents=True)
    (root / "important" / "f").write_text("keep")
    outside = tmp_path / "o_x"
    outside.mkdir()
    (root / "o_ok").mkdir()
    batch = root / "o_ok" / "a.batch"
    batch.write_text("x")
    bad = [
        root,
        root / "important",
        outside,
        root / "o_ok" / "..",
        root / "o_ok" / ".." / "important",
    ]
    for directory in bad:
        with pytest.raises(ValueError):
            clip_order.remove_copy(clip_order.OrderedCopy(root, directory, directory / "a.batch"))
    assert (root / "important" / "f").read_text() == "keep" and outside.is_dir()
    clip_order.remove_copy(clip_order.OrderedCopy(root, root / "o_ok", batch))
    assert not (root / "o_ok").exists() and (root / "important").is_dir() and root.is_dir()


def old_by(path: Path, hours: float) -> None:
    then = time.time() - hours * 3600
    os.utime(path, (then, then))


def test_old_working_copies_are_swept_and_nothing_else(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    old, fresh, other, plain_file = (
        work / "o_old",
        work / "o_fresh",
        work / "keep_me",
        work / "o_file",
    )
    for directory in (old, fresh, other):
        directory.mkdir()
    (old / "f").write_text("x")
    plain_file.write_text("x")
    for path in (old, other, plain_file):
        old_by(path, 25)
    old_by(fresh, 23)
    swept = clip_order.sweep_old(work)
    assert swept.removed == (str(old),) and swept.errors == ()
    assert sorted(p.name for p in work.iterdir()) == ["keep_me", "o_file", "o_fresh"]
    assert clip_order.sweep_old(tmp_path / "missing") == clip_order.Swept()


def test_prepare_sweeps_old_copies_first_and_the_report_says_which(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "o_stale").mkdir(parents=True)
    old_by(work / "o_stale", 48)
    batch = write_setup(tmp_path / "src", "cp", [clip("n1", "a", R1), clip("n2", "b", R1)])
    prep = prepare(batch, [record(R1, "a", "b")], work)
    assert prep.swept.removed == (str(work / "o_stale"),) and not (work / "o_stale").exists()
    assert clip_order.report(prep, str(batch), None)["sweptOld"] == [str(work / "o_stale")]


def test_restore_reports_loaded_true_and_sweeps_old_copies(rig: Rig) -> None:
    saved = take_checkpoint(rig)
    stale = ordered_dir(rig) / "o_stale"
    stale.mkdir(parents=True)
    old_by(stale, 30)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True, env
    order = env["result"]["clipOrder"]
    assert order["loaded"] is True and order["sweptOld"] == [str(stale)]
    assert not stale.exists()


def test_a_restore_refused_before_the_load_removes_the_copy_and_says_it_did_not_load(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = take_checkpoint(rig)
    seen: list[Path] = []
    monkeypatch.setattr(clip_order, "prepare", fake_prepare(seen))
    rig.exec.fault("batch.checkpoint", "BRIDGE_UNAVAILABLE", when="before")
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is False
    assert rig.handles["bg1"].load_calls == []
    [copy_path] = seen
    assert not copy_path.exists() and list(ordered_dir(rig).iterdir()) == []
    order = env["result"]["clipOrder"]
    assert order["loaded"] is False and order["loadedFrom"] == str(copy_path)


@pytest.mark.parametrize("failure", [OSError("busy"), ValueError("not a copy")])
def test_a_copy_that_cannot_be_removed_is_reported_and_does_not_fail_the_restore(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    saved = take_checkpoint(rig)
    monkeypatch.setattr(clip_order, "prepare", fake_prepare([]))

    def refuse(copy: clip_order.OrderedCopy | None) -> None:
        raise failure

    monkeypatch.setattr(clip_order, "remove_copy", refuse)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True, env
    assert env["result"]["clipOrder"]["cleanupError"] == f"{type(failure).__name__}: {failure}"


def test_reels_that_could_not_be_put_back_are_warned_about_in_the_result(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = take_checkpoint(rig)
    skipped = (clip_order.SkippedReel(R2, "names differ"), clip_order.SkippedReel(None, "odd"))
    monkeypatch.setattr(clip_order, "prepare", fake_prepare([], skipped=skipped))
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True, env
    warnings = env["result"]["warnings"]
    assert len(warnings) == 2
    assert R2 in warnings[0] and "names differ" in warnings[0]
    assert "odd" in warnings[1]


def test_no_warnings_when_every_reel_was_handled(rig: Rig) -> None:
    saved = take_checkpoint(rig)
    env = rig.do("journal.restore_checkpoint", {"checkpoint": saved["checkpoint"]})
    assert env["ok"] is True and "warnings" not in env["result"]
