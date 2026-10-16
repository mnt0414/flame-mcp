"""Tests `render_files`: what a render is to write and what the directory shows."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from flame_mcp import render_files
from flame_mcp.render_files import (
    OutputSpec,
    directory_summary,
    existing_planned,
    is_default_output_dir,
    list_directory,
    parse_resolved_path,
    spec_frames_present,
    written_since,
)


def test_the_dotted_resolved_path_of_plan_48_is_parsed_into_head_and_tail() -> None:
    spec = parse_resolved_path("/var/tmp/writefile1.[000001-000100].exr")
    assert spec == OutputSpec(
        directory="/var/tmp",
        sequence=True,
        head="writefile1.",
        tail=".exr",
        first=1,
        last=100,
        padding=6,
    )
    assert spec is not None and spec.count == 100
    assert spec.names()[0] == "writefile1.000001.exr"
    assert spec.names()[-1] == "writefile1.000100.exr"
    assert spec.paths()[2] == "/var/tmp/writefile1.000003.exr"


def test_the_undotted_resolved_path_measured_on_the_real_flame_is_parsed() -> None:
    """Parses the resolved path of a Write File whose numbers have no dot before them.

    Measured on Flame 2025.2.4: the resolved path is `.../render_a/write_a[000001-000008].exr`
    while the files are `write_a000001.exr`."""
    spec = parse_resolved_path("/out/render_a/write_a[000001-000008].exr")
    assert spec == OutputSpec(
        directory="/out/render_a",
        sequence=True,
        head="write_a",
        tail=".exr",
        first=1,
        last=8,
        padding=6,
    )
    assert spec is not None and spec.count == 8
    assert spec.names() == [f"write_a{n:06d}.exr" for n in range(1, 9)]
    assert spec.name_of(3) == "write_a000003.exr"


def test_the_padding_and_a_dotted_head_follow_the_path() -> None:
    spec = parse_resolved_path("/a/b/shot.v01.[0008-0010].dpx")
    assert spec is not None
    assert (spec.head, spec.tail, spec.padding, spec.first, spec.last) == (
        "shot.v01.",
        ".dpx",
        4,
        8,
        10,
    )
    assert spec.names() == ["shot.v01.0008.dpx", "shot.v01.0009.dpx", "shot.v01.0010.dpx"]


def test_any_head_and_tail_around_the_bracket_are_kept() -> None:
    spec = parse_resolved_path("/d/x_[01-03]_final.tga")
    assert spec is not None and (spec.head, spec.tail) == ("x_", "_final.tga")
    assert spec.names() == ["x_01_final.tga", "x_02_final.tga", "x_03_final.tga"]
    bare = parse_resolved_path("/d/[1-2]")
    assert bare is not None and bare.names() == ["1", "2"]


def test_a_path_that_is_not_a_sequence_is_one_file() -> None:
    spec = parse_resolved_path("/out/movie.mov")
    assert spec == OutputSpec(directory="/out", sequence=False, path="/out/movie.mov")
    assert spec is not None and spec.count == 1 and spec.names() == ["movie.mov"]


def test_a_bracket_that_is_not_a_range_is_not_a_sequence() -> None:
    for path in ("/out/movie[abc].mov", "/out/movie[1].mov", "/out/movie[1-].mov"):
        spec = parse_resolved_path(path)
        assert spec is not None and spec.sequence is False and spec.path == path


def test_a_bracket_in_a_directory_name_is_never_taken_for_a_range() -> None:
    plain = parse_resolved_path("/shots/[001-005]/movie.mov")
    assert plain is not None and plain.sequence is False
    assert plain.directory == "/shots/[001-005]" and plain.names() == ["movie.mov"]
    spec = parse_resolved_path("/shots/[001-005]/w[0001-0002].exr")
    assert spec is not None and spec.sequence is True
    assert spec.directory == "/shots/[001-005]"
    assert spec.names() == ["w0001.exr", "w0002.exr"]


@pytest.mark.parametrize("bad", [None, "", "   ", 5, "no-directory.exr"])
def test_something_that_is_not_a_path_is_not_parsed(bad: object) -> None:
    assert parse_resolved_path(bad) is None  # type: ignore[arg-type]


def test_an_empty_or_reversed_range_has_no_frames() -> None:
    spec = OutputSpec(
        directory="/x", sequence=True, head="p.", first=5, last=4, padding=3, tail=".e"
    )
    assert spec.count == 0 and spec.names() == []


def test_a_range_beyond_the_expansion_cap_is_not_expanded() -> None:
    spec = OutputSpec(
        directory="/x",
        sequence=True,
        head="p.",
        first=1,
        last=render_files.MAX_EXPANDED_FRAMES + 1,
        padding=6,
        tail=".e",
    )
    assert spec.count == render_files.MAX_EXPANDED_FRAMES + 1 and spec.names() == []


def test_a_spec_survives_json() -> None:
    for path in ("/d/w.[0001-0003].exr", "/d/w[0001-0003].exr", "/d/single.mov"):
        spec = parse_resolved_path(path)
        assert spec is not None and OutputSpec.from_json(spec.to_json()) == spec


def test_a_record_written_in_the_first_form_is_still_read() -> None:
    """First form: `prefix` / `ext`, files named `<prefix>.<number>.<ext>`."""
    old = {
        "directory": "/d",
        "sequence": True,
        "prefix": "w",
        "first": 1,
        "last": 3,
        "padding": 4,
        "ext": "exr",
        "path": None,
    }
    spec = OutputSpec.from_json(old)
    assert (spec.head, spec.tail) == ("w.", ".exr")
    assert spec.names() == ["w.0001.exr", "w.0002.exr", "w.0003.exr"]
    assert spec == parse_resolved_path("/d/w.[0001-0003].exr")


def test_the_default_output_directories_are_recognised_through_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert is_default_output_dir("/var/tmp/") and is_default_output_dir("/var/tmp")
    assert is_default_output_dir("/tmp") and is_default_output_dir("/private/tmp")
    assert not is_default_output_dir("/var/tmp/sub")
    assert not is_default_output_dir(str(tmp_path)) and not is_default_output_dir(None)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(render_files, "DEFAULT_OUTPUT_DIRS", (str(real),))
    assert is_default_output_dir(str(link)) and is_default_output_dir(str(real) + "/")


def test_listing_sees_regular_files_only_and_a_missing_directory_is_empty(tmp_path: Path) -> None:
    (tmp_path / "a.exr").write_bytes(b"12345")
    (tmp_path / "sub").mkdir()
    assert list_directory(str(tmp_path)).keys() == {"a.exr"}
    assert list_directory(str(tmp_path))["a.exr"][0] == 5
    assert list_directory(str(tmp_path / "nope")) == {}


def test_written_since_sees_new_and_rewritten_files_but_not_untouched_ones(tmp_path: Path) -> None:
    (tmp_path / "kept").write_bytes(b"1")
    (tmp_path / "rewritten").write_bytes(b"1")
    before = list_directory(str(tmp_path))
    (tmp_path / "rewritten").write_bytes(b"12")
    (tmp_path / "new").write_bytes(b"1")
    assert written_since(before, list_directory(str(tmp_path))) == ["new", "rewritten"]


def test_frames_present_counts_only_files_written_since_and_lists_the_missing(
    tmp_path: Path,
) -> None:
    spec = OutputSpec(
        directory=str(tmp_path), sequence=True, head="w.", first=1, last=5, padding=2, tail=".e"
    )
    (tmp_path / "w.01.e").write_bytes(b"old")  # there before: not written by the run
    before = list_directory(str(tmp_path))
    (tmp_path / "w.02.e").write_bytes(b"x")
    (tmp_path / "w.03.e").write_bytes(b"x")
    written, missing = spec_frames_present(spec, before, list_directory(str(tmp_path)))
    assert written == 2 and missing == ["w.01.e", "w.04.e", "w.05.e"]


def test_frames_present_caps_the_missing_names(tmp_path: Path) -> None:
    spec = OutputSpec(
        directory=str(tmp_path), sequence=True, head="w.", first=1, last=100, padding=3, tail=".e"
    )
    written, missing = spec_frames_present(spec, {}, {})
    assert written == 0 and len(missing) == render_files.MISSING_CAP


def test_existing_planned_files_are_the_ones_a_render_would_overwrite(tmp_path: Path) -> None:
    spec = OutputSpec(
        directory=str(tmp_path), sequence=True, head="w.", first=1, last=3, padding=2, tail=".e"
    )
    (tmp_path / "w.02.e").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_bytes(b"x")
    assert list(existing_planned(spec, list_directory(str(tmp_path)))) == ["w.02.e"]


def test_the_directory_summary_lists_a_bounded_number_of_names(tmp_path: Path) -> None:
    for n in range(render_files.NAME_LIST_CAP + 5):
        (tmp_path / f"f{n:04d}").write_bytes(b"")
    summary = directory_summary(list_directory(str(tmp_path)))
    assert summary["count"] == render_files.NAME_LIST_CAP + 5
    assert len(summary["names"]) == render_files.NAME_LIST_CAP and summary["truncated"] is True
    assert os.path.isdir(tmp_path)
