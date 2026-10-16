"""The helpers that scripts/fix3_reel_order_e2e.py and scripts/p3_2a_checkpoint_e2e.py carry
themselves. The in-process runs of the e2e clients replace some of them (the pixel md5 is swapped
for a file md5), so they are tested here directly."""

from __future__ import annotations

import hashlib
import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str) -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


def batch_text(names: tuple[str, ...], types: tuple[str, ...]) -> str:
    nodes = "".join(
        f"<Node><Type>{t}</Type><Name>{n}</Name><SchemName>s_{n}</SchemName>"
        f"<Number>{i}</Number></Node>"
        for i, (n, t) in enumerate(zip(names, types, strict=True), start=1)
    )
    return f"<Setup>head{nodes}tail</Setup>"


def test_reorder_clip_elements_reverses_only_the_clip_nodes() -> None:
    fix3 = load("fix3_reel_order_e2e")
    text = batch_text(("a", "b", "c", "d", "e"), ("Clip", "Blur", "Clip", "Comp", "Clip"))

    out = fix3.reorder_clip_elements(text)

    assert [r["name"] for r in fix3.node_rows(out)] == ["e", "b", "c", "d", "a"]
    assert [r["type"] for r in fix3.node_rows(out)] == ["Clip", "Blur", "Clip", "Comp", "Clip"]
    assert out.startswith("<Setup>head")
    assert out.endswith("tail</Setup>")
    assert fix3.reorder_clip_elements(out) == text


def test_node_rows_reads_number_as_int_and_tags_are_unescaped() -> None:
    fix3 = load("fix3_reel_order_e2e")
    text = "<Node><Type>Clip</Type><Name>a&amp;b</Name><Number>7</Number></Node>"

    assert fix3.node_rows(text) == [
        {"index": 0, "type": "Clip", "name": "a&b", "schem_name": None, "number": 7}
    ]


def test_clip_rows_joins_each_clip_node_with_its_clip_node_file(tmp_path: Path) -> None:
    fix3 = load("fix3_reel_order_e2e")
    batch = tmp_path / "setup.batch"
    batch.write_text(batch_text(("c1", "x", "c2"), ("Clip", "Blur", "Clip")), encoding="utf-8")
    (tmp_path / "setup").mkdir()
    (tmp_path / "setup" / "c1.clip_node").write_text(
        "<State><ClipName>one</ClipName><ReelName>Reel A</ReelName></State>", encoding="utf-8"
    )

    rows = fix3.clip_rows(batch)

    assert [
        (r["node_name"], r["clip_node_file"], r["clip_name"], r["reel_name"]) for r in rows
    ] == [
        ("c1", True, "one", "Reel A"),
        ("c2", False, None, None),
    ]
    assert [r["pos_in_clips"] for r in rows] == [0, 1]
    assert [r["node_index"] for r in rows] == [0, 2]


def test_fix3_selftest_rewrite_finds_no_problem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fix3 = load("fix3_reel_order_e2e")
    monkeypatch.setattr(fix3, "OUTPUT_DIR", tmp_path)

    assert fix3.selftest_rewrite() == []


def test_hash_dir_lists_files_sorted_with_md5_and_size(tmp_path: Path) -> None:
    p3 = load("p3_2a_checkpoint_e2e")
    (tmp_path / "b.bin").write_bytes(b"bb")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.bin").write_bytes(b"")

    listing = p3._hash_dir(tmp_path)

    assert listing["exists"] is True
    assert listing["count"] == 2
    assert listing["total_bytes"] == 2
    assert [(f["rel"], f["bytes"]) for f in listing["files"]] == [("b.bin", 2), ("sub/a.bin", 0)]
    assert listing["files"][0]["md5"] == hashlib.md5(b"bb", usedforsecurity=False).hexdigest()


def test_hash_dir_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    p3 = load("p3_2a_checkpoint_e2e")

    listing = p3._hash_dir(tmp_path / "nope")

    assert listing == {
        "dir": str(tmp_path / "nope"),
        "exists": False,
        "count": 0,
        "total_bytes": 0,
        "files": [],
    }


def ffmpeg_for(p3: ModuleType) -> str | None:
    exe: str = p3._ffmpeg_exe()[0]
    return exe if shutil.which(exe) or Path(exe).is_file() else None


def make_tiff(ffmpeg: str, path: Path, colour: str) -> None:
    source = f"color=c={colour}:s=16x16"
    command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", source, "-frames:v", "1", "-y"]
    subprocess.run([*command, str(path)], check=True, timeout=60)


def test_pix_hash_dir_hashes_the_pixels_not_the_file(tmp_path: Path) -> None:
    p3 = load("p3_2a_checkpoint_e2e")
    ffmpeg = ffmpeg_for(p3)
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    make_tiff(ffmpeg, tmp_path / "red.tiff", "red")
    make_tiff(ffmpeg, tmp_path / "blue.tiff", "blue")
    padded = tmp_path / "red_padded.tiff"
    padded.write_bytes((tmp_path / "red.tiff").read_bytes() + b"\0" * 4)

    by_name = {f["rel"]: f for f in p3._pix_hash_dir(tmp_path)["files"]}

    assert by_name["red.tiff"]["md5"] != by_name["blue.tiff"]["md5"]
    assert by_name["red.tiff"]["file_md5"] != by_name["red_padded.tiff"]["file_md5"]
    assert not by_name["red.tiff"]["md5"].startswith("ERR:")


def test_pix_hash_dir_marks_a_file_ffmpeg_cannot_decode(tmp_path: Path) -> None:
    p3 = load("p3_2a_checkpoint_e2e")
    if ffmpeg_for(p3) is None:
        pytest.skip("no ffmpeg")
    (tmp_path / "junk.tiff").write_bytes(b"not an image")

    listing = p3._pix_hash_dir(tmp_path)

    assert listing["md5_kind"] == "pixel (ffmpeg -f md5)"
    assert listing["files"][0]["md5"].startswith("ERR:")
    assert listing["files"][0]["file_md5"] == p3._hash_dir(tmp_path)["files"][0]["md5"]


def test_pixel_hash_dir_is_the_pixel_listing(tmp_path: Path) -> None:
    p3 = load("p3_2a_checkpoint_e2e")
    (tmp_path / "x.bin").write_bytes(b"x")
    if ffmpeg_for(p3) is None:
        pytest.skip("no ffmpeg")

    assert p3.pixel_hash_dir(tmp_path)["files"][0].keys() == {"rel", "bytes", "md5", "file_md5"}
