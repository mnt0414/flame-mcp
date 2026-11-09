"""Tests `setup_compare.compare_setups`: two saved Batch setups compared as text, with the lines
that change without a change of value left out. Pure functions over files in a temp directory; no
Flame. The op that saves the current setup and calls this is tested in test_batch_compare_setup.py.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import setup_compare
from flame_mcp.setup_compare import (
    IGNORED_FILES,
    MAX_DIFF_LINES,
    MAX_HUNK_LINES,
    MAX_HUNKS_PER_FILE,
    MAX_LINE_CHARS,
    MAX_LISTED_FILES,
    NOISE_FLAG_BITS,
    compare_setups,
)


def make_setup(
    root: Path,
    stem: str,
    batch: str = "<Setup></Setup>",
    files: dict[str, str | bytes] | None = None,
) -> Path:
    path = root / f"{stem}.batch"
    path.write_text(batch, encoding="utf-8")
    directory = root / stem
    directory.mkdir()
    for name, content in (files or {}).items():
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        (directory / name).write_bytes(data)
    return path


def pair(
    tmp_path: Path,
    *,
    before_batch: str = "<Setup></Setup>",
    after_batch: str | None = None,
    before: dict[str, str | bytes] | None = None,
    after: dict[str, str | bytes] | None = None,
) -> tuple[Path, Path]:
    (tmp_path / "a").mkdir(parents=True, exist_ok=True)
    (tmp_path / "b").mkdir(parents=True, exist_ok=True)
    first = make_setup(tmp_path / "a", "stem_one", before_batch, before)
    second = make_setup(
        tmp_path / "b",
        "stem_two",
        before_batch if after_batch is None else after_batch,
        before if after is None else after,
    )
    return first, second


def action(
    *,
    current_frame: int = 9,
    stamp: str = "Wed Oct  7 19:59:27 2026",
    pos_x_value: str = "0",
    lead: str = "34.9970016",
    key_value: str = "0.00439999998",
    uncollapsed: bool = True,
    front_uid: str = "uid-1",
) -> str:
    collapsed = "\t\t\tUncollapsed\n" if uncollapsed else ""
    return (
        "Module Action\n"
        f"CreationDate {stamp}\n"
        "\tMinFrame 1\n"
        f"\tCurrentFrame {current_frame}\n"
        "Node Axis\n"
        "\tName axis2\n"
        "\tNumber 5\n"
        f"\tFrontClipUID {front_uid}\n"
        "\tSpecifics\n"
        "\t{\n"
        "\t\tChannel position/x\n"
        "\t\t\tExtrapolation constant\n"
        f"\t\t\tValue {pos_x_value}\n"
        f"{collapsed}"
        "\t\t\tEnd\n"
        "\t\tChannel rotation/z\n"
        "\t\t\tExtrapolation constant\n"
        f"\t\t\tValue {lead}\n"
        "\t\t\tSize 2\n"
        "\t\t\tKeyVersion 2\n"
        "\t\t\tKey 0\n"
        "\t\t\t\tFrame 6\n"
        f"\t\t\t\tValue {key_value}\n"
        "\t\t\t\tRHandle_dX 9.66666698\n"
        "\t\t\t\tEnd\n"
    )


def timewarp(*, frame_mode: str = "3", key_value: str = "4", current: str = "1") -> str:
    return (
        "<Setup><Base><Version>21.020000</Version><NAME>timewarp1</NAME><Note></Note>"
        "<Expanded>False</Expanded><ScrollBar>0</ScrollBar><Frames>0</Frames>"
        f"<Current_Time>{current}</Current_Time></Base><State>"
        f"<TW_FrameMode>{frame_mode}</TW_FrameMode><TW_SpeedTiming>"
        '<Channel Name="SpeedTiming"><Extrap>linear</Extrap><Value>-8</Value><Size>1</Size>'
        '<KeyVersion>2</KeyVersion><KFrames><Key Index="0"><Frame>4</Frame>'
        f"<Value>{key_value}</Value></Key></KFrames></Channel></TW_SpeedTiming></State></Setup>"
    )


def batch_xml(
    *,
    flags: int = 0,
    date: int = 1792177580,
    pos_x: int = 0,
    current: int = 1,
    stamp: str = "Sat Oct 17 20:51:08 2026",
    length: int = 100,
) -> str:
    return (
        "<Setup><Header><Module>BATCH</Module>"
        f"<CreationDate>{stamp}</CreationDate></Header>"
        f"<Root><BatchLength>{length}</BatchLength><BatchCurrent>{current}</BatchCurrent>"
        f"<BatchDate>{date}</BatchDate></Root>"
        '<Node><Res W="1920" H="1080" PxR="1" SF="2" PxF="143"/><Type>Group</Type>'
        f"<Name>root</Name><PosX>{pos_x}</PosX><PosY>0</PosY>"
        '<NotePosition Size="4" Vector=" 41 251 41 216"/>'
        f"<Flags>{flags}</Flags><Collapsed>True</Collapsed><IconWidth>140</IconWidth></Node>"
        "<Links/></Setup>"
    )


# -- unchanged ----------------------------------------------------------------------------------


def test_the_same_content_is_unchanged_even_when_the_stems_differ(tmp_path: Path) -> None:
    a, b = pair(tmp_path, before_batch=batch_xml(), before={"axis2.action": action()})
    result = compare_setups(a, b)
    assert result == {
        "changed": False,
        "filesCompared": 2,
        "changedFiles": 0,
        "files": [],
        "truncated": False,
    }


def test_identical_bytes_are_not_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(name: str, text: str) -> list[str]:
        raise AssertionError("normalized identical bytes")

    monkeypatch.setattr(setup_compare, "normalize", boom)
    a, b = pair(tmp_path, before_batch=batch_xml(), before={"axis2.action": action()})
    assert compare_setups(a, b)["changed"] is False


@pytest.mark.parametrize(
    "variant",
    [
        {"stamp": "Thu Oct  8 01:02:03 2026"},
        {"current_frame": 40},
        {"front_uid": "uid-other"},
        {"uncollapsed": False},
        {"lead": "12.5"},
    ],
    ids=["creation-date", "current-frame", "clip-uid", "uncollapsed", "leading-value-of-keyed"],
)
def test_noise_in_the_line_format_is_not_a_change(tmp_path: Path, variant: dict[str, Any]) -> None:
    a, b = pair(
        tmp_path,
        before={"axis2.action": action()},
        after={"axis2.action": action(**variant)},
    )
    result = compare_setups(a, b)
    assert result["changed"] is False, result


def test_the_leading_value_of_a_keyed_channel_is_ignored_with_any_indent(tmp_path: Path) -> None:
    before = "Channel a\nValue 1\nSize 3\nKey 0\nValue 5\nEnd\n"
    after = "Channel a\n\t\t\tValue 2\n\t\t\tSize 3\nKey 0\nValue 5\nEnd\n"
    a, b = pair(tmp_path, before={"x.action": before}, after={"x.action": after})
    assert compare_setups(a, b)["changed"] is False


def test_mv_json_is_not_compared(tmp_path: Path) -> None:
    assert "MV.json" in IGNORED_FILES
    a, b = pair(tmp_path, before={"MV.json": '{"zoom": 1}'}, after={"MV.json": '{"zoom": 2}'})
    result = compare_setups(a, b)
    assert result["changed"] is False
    assert result["files"] == []


def test_the_lut_creation_date_comment_is_noise(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"x.lut": "# CreationDate Mon Oct 1 2026\n0 0 0\n"},
        after={"x.lut": "# CreationDate Tue Oct 2 2026\n0 0 0\n"},
    )
    assert compare_setups(a, b)["changed"] is False


def test_xml_noise_is_not_a_change(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"tw.timewarp_node": timewarp(current="1")},
        after={
            "tw.timewarp_node": timewarp(current="50")
            .replace("<ScrollBar>0</ScrollBar>", "<ScrollBar>7</ScrollBar><CUIDsec>1</CUIDsec>")
            .replace("<Expanded>False", "<Expanded>True")
            .replace("<Note></Note>", "<Note></Note><Uncollapsed/>")
        },
    )
    assert compare_setups(a, b)["changed"] is False


def test_batch_noise_is_not_a_change(tmp_path: Path) -> None:
    noise = sum(1 << bit for bit in NOISE_FLAG_BITS)
    a, b = pair(
        tmp_path,
        before_batch=batch_xml(),
        after_batch=batch_xml(
            flags=noise,
            date=1,
            pos_x=999,
            current=77,
            stamp="Sun Oct 18 00:00:00 2026",
        ).replace('Vector=" 41 251 41 216"', 'Vector=" 1 2 3 4"'),
    )
    assert compare_setups(a, b)["changed"] is False


def test_batch_only_tags_are_not_ignored_in_other_files(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"x.xml": "<A><PosX>1</PosX></A>"},
        after={"x.xml": "<A><PosX>2</PosX></A>"},
    )
    result = compare_setups(a, b)
    assert result["changed"] is True
    assert result["files"][0]["file"] == "x.xml"


def test_the_noise_bits_are_exactly_6_7_18_19_24_26() -> None:
    assert sorted(NOISE_FLAG_BITS) == [6, 7, 18, 19, 24, 26]


# -- changed ------------------------------------------------------------------------------------


def test_a_value_inside_a_key_is_a_change_and_names_the_node_and_channel(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"axis2.action": action()},
        after={"axis2.action": action(key_value="0.5")},
    )
    result = compare_setups(a, b)
    assert result["changed"] is True
    assert result["changedFiles"] == 1 and result["filesCompared"] == 2
    [entry] = result["files"]
    assert entry["file"] == "axis2.action" and entry["status"] == "changed"
    assert entry["lineCount"] == 2
    assert entry["hunks"] == [
        {
            "where": {"node": "axis2", "channel": "rotation/z"},
            "before": ["Value 0.00439999998"],
            "after": ["Value 0.5"],
        }
    ]
    assert result["truncated"] is False


def test_a_value_of_a_channel_without_keys_is_a_change(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"axis2.action": action()},
        after={"axis2.action": action(pos_x_value="12.5")},
    )
    [entry] = compare_setups(a, b)["files"]
    [hunk] = entry["hunks"]
    assert hunk["where"] == {"node": "axis2", "channel": "position/x"}
    assert hunk["before"] == ["Value 0"] and hunk["after"] == ["Value 12.5"]


def test_where_is_null_when_nothing_precedes_the_change(tmp_path: Path) -> None:
    a, b = pair(tmp_path, before={"x.txt": "alpha\nbeta\n"}, after={"x.txt": "alpha\ngamma\n"})
    [entry] = compare_setups(a, b)["files"]
    assert entry["hunks"][0]["where"] == {"node": None, "channel": None}


def test_the_nearest_name_and_channel_before_the_change_are_used(tmp_path: Path) -> None:
    text = "Name first\nChannel c1\nValue 1\nName second\nChannel c2\nValue 2\n"
    a, b = pair(
        tmp_path, before={"x.action": text}, after={"x.action": text.replace("Value 2", "Value 3")}
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["where"] == {"node": "second", "channel": "c2"}


def test_a_change_in_an_added_or_removed_line_is_a_hunk(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"x.txt": "one\ntwo\nthree\n"},
        after={"x.txt": "one\ntwo\ntwo and a half\nthree\n"},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["before"] == [] and hunk["after"] == ["two and a half"]


def test_a_flags_bit_outside_the_noise_bits_is_a_change(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path, before_batch=batch_xml(flags=786432), after_batch=batch_xml(flags=786432 | 1)
    )
    [entry] = compare_setups(a, b)["files"]
    assert entry["file"] == ".batch" and entry["status"] == "changed"
    [hunk] = entry["hunks"]
    assert hunk["before"] == ["<Flags>0</Flags>"] and hunk["after"] == ["<Flags>1</Flags>"]


def test_a_bypass_style_flag_in_a_noise_free_bit_is_a_change(tmp_path: Path) -> None:
    a, b = pair(tmp_path, before_batch=batch_xml(flags=0), after_batch=batch_xml(flags=1 << 20))
    assert compare_setups(a, b)["changed"] is True


def test_a_batch_length_change_is_a_change_and_the_node_is_not_guessed(tmp_path: Path) -> None:
    a, b = pair(tmp_path, before_batch=batch_xml(), after_batch=batch_xml(length=120))
    [entry] = compare_setups(a, b)["files"]
    [hunk] = entry["hunks"]
    assert hunk["before"] == ["<BatchLength>100</BatchLength>"]
    assert hunk["after"] == ["<BatchLength>120</BatchLength>"]
    assert hunk["where"] == {"node": None, "channel": None}


def test_a_speed_timing_key_of_a_timewarp_is_a_change(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"tw.timewarp_node": timewarp()},
        after={"tw.timewarp_node": timewarp(key_value="5")},
    )
    [entry] = compare_setups(a, b)["files"]
    [hunk] = entry["hunks"]
    assert hunk["before"] == ["<Value>4</Value>"] and hunk["after"] == ["<Value>5</Value>"]
    assert hunk["where"] == {"node": "timewarp1", "channel": "SpeedTiming"}


def test_the_leading_value_of_a_keyed_xml_channel_is_ignored_but_the_frame_mode_is_not(
    tmp_path: Path,
) -> None:
    changed_lead = timewarp().replace("<Value>-8</Value>", "<Value>99</Value>")
    a, b = pair(
        tmp_path / "lead",
        before={"tw.timewarp_node": timewarp()},
        after={"tw.timewarp_node": changed_lead},
    )
    assert compare_setups(a, b)["changed"] is False
    a2, b2 = pair(
        tmp_path / "mode",
        before={"tw.timewarp_node": timewarp()},
        after={"tw.timewarp_node": timewarp(frame_mode="2")},
    )
    [entry] = compare_setups(a2, b2)["files"]
    [hunk] = entry["hunks"]
    assert hunk["before"] == ["<TW_FrameMode>3</TW_FrameMode>"]
    assert hunk["after"] == ["<TW_FrameMode>2</TW_FrameMode>"]


def test_a_value_inside_a_key_of_a_keyed_xml_channel_stays(tmp_path: Path) -> None:
    text = (
        '<Channel Name="c"><Value>1</Value><Size>2</Size><Key Index="0"><Value>7</Value></Key>'
        "</Channel>"
    )
    a, b = pair(
        tmp_path,
        before={"x.xml": text},
        after={"x.xml": text.replace("<Value>7", "<Value>8")},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["before"] == ["<Value>7</Value>"]


def test_a_value_of_an_xml_channel_without_size_stays(tmp_path: Path) -> None:
    text = '<Channel Name="c"><Value>1</Value><Extrap>x</Extrap></Channel>'
    a, b = pair(
        tmp_path, before={"x.xml": text}, after={"x.xml": text.replace("<Value>1", "<Value>2")}
    )
    assert compare_setups(a, b)["changed"] is True


def test_a_file_added_or_removed_has_a_status_and_no_hunks(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"gone.clip_node": "x", "kept.txt": "k"},
        after={"new.output_node": "y", "kept.txt": "k"},
    )
    result = compare_setups(a, b)
    assert result["changed"] is True and result["changedFiles"] == 2
    assert result["filesCompared"] == 4
    assert result["files"] == [
        {"file": "gone.clip_node", "status": "removed"},
        {"file": "new.output_node", "status": "added"},
    ]


def test_the_batch_file_comes_first_and_the_rest_by_name(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before_batch=batch_xml(),
        after_batch=batch_xml(length=1),
        before={"b.txt": "1", "a.txt": "1", "c.txt": "1"},
        after={"b.txt": "2", "a.txt": "2", "c.txt": "2"},
    )
    assert [f["file"] for f in compare_setups(a, b)["files"]] == [
        ".batch",
        "a.txt",
        "b.txt",
        "c.txt",
    ]


# -- limits -------------------------------------------------------------------------------------


def test_the_limits_have_the_agreed_values() -> None:
    assert MAX_DIFF_LINES == 20_000
    assert MAX_HUNK_LINES == 20
    assert not hasattr(setup_compare, "MAX_DIFF_BYTES")
    assert MAX_HUNKS_PER_FILE == 20
    assert MAX_LISTED_FILES == 50
    assert MAX_LINE_CHARS == 200


def test_hunks_beyond_the_limit_are_left_out_and_counted_in_line_count(tmp_path: Path) -> None:
    total = MAX_HUNKS_PER_FILE + 10
    before = "".join(f"same{i}\nvalue{i} old\n" for i in range(total))
    after = "".join(f"same{i}\nvalue{i} new\n" for i in range(total))
    a, b = pair(tmp_path, before={"x.txt": before}, after={"x.txt": after})
    result = compare_setups(a, b)
    [entry] = result["files"]
    assert len(entry["hunks"]) == MAX_HUNKS_PER_FILE
    assert entry["lineCount"] == total * 2
    assert result["truncated"] is True


def test_files_beyond_the_limit_are_left_out_but_counted(tmp_path: Path) -> None:
    count = MAX_LISTED_FILES + 10
    before = {f"f{i:03d}.txt": "old" for i in range(count)}
    after = {f"f{i:03d}.txt": "new" for i in range(count)}
    a, b = pair(tmp_path, before={**before}, after={**after})
    result = compare_setups(a, b)
    assert len(result["files"]) == MAX_LISTED_FILES
    assert result["changedFiles"] == count
    assert result["filesCompared"] == count + 1
    assert result["truncated"] is True
    assert result["files"][0]["file"] == "f000.txt"


def test_a_long_line_is_cut_and_trimmed(tmp_path: Path) -> None:
    long = "x" * (MAX_LINE_CHARS + 50)
    a, b = pair(
        tmp_path,
        before={"x.txt": "   padded old   \n" + long + "a\n"},
        after={"x.txt": "   padded new   \n" + long + "b\n"},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["before"][0] == "padded old"
    assert hunk["after"][0] == "padded new"
    assert hunk["before"][1] == "x" * MAX_LINE_CHARS
    assert hunk["after"][1] == "x" * MAX_LINE_CHARS


def forbid_sequence_matcher(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("SequenceMatcher was called")

    monkeypatch.setattr(difflib, "SequenceMatcher", boom)


def numbered(count: int, tag: str) -> str:
    return "".join(f"{tag} {i}\n" for i in range(count))


def test_a_middle_over_the_line_limit_is_changed_without_lines_and_without_a_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_compare, "MAX_DIFF_LINES", 10)
    a, b = pair(
        tmp_path,
        before={"big.txt": numbered(11, "old"), "small.txt": "1\n"},
        after={"big.txt": numbered(11, "new"), "small.txt": "2\n"},
    )
    result = compare_setups(a, b)
    big = next(f for f in result["files"] if f["file"] == "big.txt")
    assert big == {"file": "big.txt", "status": "changed", "tooLargeToList": True}
    small = next(f for f in result["files"] if f["file"] == "small.txt")
    assert small["hunks"]


def test_the_larger_side_of_the_middle_decides_and_SequenceMatcher_is_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_compare, "MAX_DIFF_LINES", 10)
    forbid_sequence_matcher(monkeypatch)
    a, b = pair(
        tmp_path,
        before={"grow.txt": "head\nx\ntail\n"},
        after={"grow.txt": "head\n" + numbered(11, "y") + "tail\n"},
    )
    assert compare_setups(a, b)["files"] == [
        {"file": "grow.txt", "status": "changed", "tooLargeToList": True}
    ]


def test_a_middle_at_the_line_limit_is_still_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_compare, "MAX_DIFF_LINES", 10)
    a, b = pair(
        tmp_path,
        before={"edge.txt": numbered(10, "old")},
        after={"edge.txt": numbered(10, "new")},
    )
    [entry] = compare_setups(a, b)["files"]
    assert entry["lineCount"] == 20 and "tooLargeToList" not in entry


def test_the_common_head_and_tail_do_not_count_toward_the_line_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_compare, "MAX_DIFF_LINES", 10)
    head, tail = numbered(500, "head"), numbered(500, "tail")
    a, b = pair(
        tmp_path,
        before={"long.txt": head + "old\n" + tail},
        after={"long.txt": head + "new\n" + tail},
    )
    [entry] = compare_setups(a, b)["files"]
    assert entry["hunks"][0]["before"] == ["old"] and entry["hunks"][0]["after"] == ["new"]


def test_a_large_file_that_only_differs_in_noise_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_compare, "MAX_DIFF_LINES", 10)
    forbid_sequence_matcher(monkeypatch)
    body = numbered(100, "v")
    a, b = pair(
        tmp_path,
        before={"big.txt": "CreationDate one\n" + body},
        after={"big.txt": "CreationDate two\n" + body},
    )
    assert compare_setups(a, b)["changed"] is False


def test_a_file_that_is_not_utf8_is_compared_as_bytes(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"bin.dat": b"\xff\xfe\x00\x01", "same.dat": b"\xff\xff"},
        after={"bin.dat": b"\xff\xfe\x00\x02", "same.dat": b"\xff\xff"},
    )
    result = compare_setups(a, b)
    assert result["files"] == [{"file": "bin.dat", "status": "changed", "notText": True}]


def test_a_symlink_in_the_directory_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "one").write_text("first")
    (outside / "two").write_text("second")
    a, b = pair(tmp_path, before={"real.txt": "same"}, after={"real.txt": "same"})
    os.symlink(outside / "one", a.parent / "stem_one" / "link.txt")
    os.symlink(outside / "two", b.parent / "stem_two" / "link.txt")
    os.symlink(outside / "two", b.parent / "stem_two" / "only_current.txt")
    result = compare_setups(a, b)
    assert result["changed"] is False
    assert result["filesCompared"] == 2


def test_something_that_is_not_a_regular_file_in_the_directory_is_not_compared(
    tmp_path: Path,
) -> None:
    a, b = pair(tmp_path)
    (a.parent / "stem_one" / "sub").mkdir()
    (b.parent / "stem_two" / "sub").mkdir()
    (b.parent / "stem_two" / "sub" / "inner.txt").write_text("x")
    assert compare_setups(a, b)["changed"] is False


def test_a_missing_directory_is_an_error_and_not_an_empty_side(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = tmp_path / "a" / "s.batch"
    first.write_text("<Setup></Setup>")
    second = make_setup(tmp_path / "b", "s", files={"x.txt": "1"})
    with pytest.raises(FileNotFoundError):
        compare_setups(first, second)
    with pytest.raises(FileNotFoundError):
        compare_setups(second, first)


def test_a_missing_batch_file_is_an_error_and_not_an_added_side(tmp_path: Path) -> None:
    a, b = pair(tmp_path, before={"x.txt": "1"})
    a.unlink()
    with pytest.raises(FileNotFoundError):
        compare_setups(a, b)


# -- where --------------------------------------------------------------------------------------


def test_a_name_tag_of_the_batch_file_names_the_node(tmp_path: Path) -> None:
    before = batch_xml().replace(
        "<Links/>", "<Node><Name>blur1</Name><Note>a</Note></Node><Links/>"
    )
    after = before.replace("<Note>a</Note>", "<Note>b</Note>")
    a, b = pair(tmp_path, before_batch=before, after_batch=after)
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["before"] == ["<Note>a</Note>"]
    assert hunk["where"] == {"node": "blur1", "channel": None}


def test_the_channel_is_not_taken_from_an_earlier_node(tmp_path: Path) -> None:
    text = (
        "Node Axis\nName axis1\nChannel position/x\nValue 1\nEnd\n"
        "Node Axis\nName axis2\nVisible 1\n"
    )
    a, b = pair(
        tmp_path,
        before={"x.action": text},
        after={"x.action": text.replace("Visible 1", "Visible 0")},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["where"] == {"node": "axis2", "channel": None}


def test_the_channel_of_the_same_node_is_still_found(tmp_path: Path) -> None:
    text = "Node Axis\nName axis2\nChannel position/x\nValue 1\nVisible 1\n"
    a, b = pair(
        tmp_path,
        before={"x.action": text},
        after={"x.action": text.replace("Visible 1", "Visible 0")},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["where"] == {"node": "axis2", "channel": "position/x"}


def test_a_channel_with_no_node_before_it_is_still_found(tmp_path: Path) -> None:
    text = "Channel lone\nValue 1\n"
    a, b = pair(
        tmp_path,
        before={"x.lut": text},
        after={"x.lut": text.replace("Value 1", "Value 2")},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["where"] == {"node": None, "channel": "lone"}


# -- binary and the size of a hunk --------------------------------------------------------------


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x1b", "\x1f"])
def test_utf8_with_a_control_character_is_not_text(tmp_path: Path, control: str) -> None:
    a, b = pair(
        tmp_path,
        before={"bin.mx": f"head{control}one\n"},
        after={"bin.mx": f"head{control}two\n"},
    )
    assert compare_setups(a, b)["files"] == [
        {"file": "bin.mx", "status": "changed", "notText": True}
    ]


def test_a_control_character_on_one_side_only_is_not_text(tmp_path: Path) -> None:
    a, b = pair(tmp_path, before={"x.dat": "plain\n"}, after={"x.dat": "plain\x00\n"})
    assert compare_setups(a, b)["files"] == [
        {"file": "x.dat", "status": "changed", "notText": True}
    ]


def test_tab_newline_and_carriage_return_are_text(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"x.txt": "a\tb\r\nc\n"},
        after={"x.txt": "a\tb\r\nd\n"},
    )
    [entry] = compare_setups(a, b)["files"]
    assert entry["hunks"][0]["before"] == ["c"] and "notText" not in entry


def test_a_hunk_is_cut_to_the_line_limit_and_says_so(tmp_path: Path) -> None:
    count = MAX_HUNK_LINES + 15
    a, b = pair(
        tmp_path,
        before={"x.txt": "top\n" + numbered(count, "old") + "bottom\n"},
        after={"x.txt": "top\n" + numbered(count, "new") + "bottom\n"},
    )
    result = compare_setups(a, b)
    [entry] = result["files"]
    [hunk] = entry["hunks"]
    assert hunk["before"] == [f"old {i}" for i in range(MAX_HUNK_LINES)]
    assert hunk["after"] == [f"new {i}" for i in range(MAX_HUNK_LINES)]
    assert hunk["truncated"] is True
    assert entry["lineCount"] == count * 2
    assert result["truncated"] is True


def test_a_hunk_at_the_line_limit_is_not_marked(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"x.txt": numbered(MAX_HUNK_LINES, "old")},
        after={"x.txt": numbered(MAX_HUNK_LINES, "new")},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert len(hunk["before"]) == MAX_HUNK_LINES and "truncated" not in hunk


def test_only_the_side_over_the_limit_is_cut(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"x.txt": "top\nold\nbottom\n"},
        after={"x.txt": "top\n" + numbered(MAX_HUNK_LINES + 5, "new") + "bottom\n"},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["before"] == ["old"] and len(hunk["after"]) == MAX_HUNK_LINES
    assert hunk["truncated"] is True


# -- XML on several lines -----------------------------------------------------------------------


def test_every_line_that_starts_with_a_tag_is_split_between_its_tags(tmp_path: Path) -> None:
    before = "<Setup><NAME>n1</NAME><A><B>1</B></A>\n<C><D>2</D></C></Setup>\n"
    after = before.replace("<D>2</D>", "<D>3</D>")
    a, b = pair(tmp_path, before={"x.xml": before}, after={"x.xml": after})
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["before"] == ["<D>2</D>"] and hunk["after"] == ["<D>3</D>"]
    assert hunk["where"]["node"] == "n1"


def test_a_line_that_does_not_start_with_a_tag_is_not_split(tmp_path: Path) -> None:
    a, b = pair(tmp_path, before={"x.txt": "a><b old\n"}, after={"x.txt": "a><b new\n"})
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["before"] == ["a><b old"]


def test_an_empty_xml_element_is_the_same_whichever_way_it_is_written(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={
            "r.resize_node": '<A><Channel Name="X">\n  </Channel><Channel Name="Y"></Channel></A>'
        },
        after={"r.resize_node": '<A><Channel Name="X"/><Channel Name="Y"/></A>'},
    )
    assert compare_setups(a, b)["changed"] is False


def test_an_element_with_a_value_is_not_the_same_as_an_empty_one(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"r.resize_node": '<A><Channel Name="X"></Channel></A>'},
        after={"r.resize_node": '<A><Channel Name="X"><Value>1</Value></Channel></A>'},
    )
    assert compare_setups(a, b)["changed"] is True


def test_an_empty_ignored_tag_is_still_dropped(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"x.xml": "<A><ScrollBar></ScrollBar><B>1</B></A>"},
        after={"x.xml": "<A><B>1</B></A>"},
    )
    assert compare_setups(a, b)["changed"] is False


def test_a_clip_uid_path_is_ignored_whether_empty_or_filled_or_different(tmp_path: Path) -> None:
    def clip(path: str) -> str:
        return f"<Clip><ClipUIDPath>{path}</ClipUIDPath><ClipName>c</ClipName></Clip>"

    cases = [
        ("\n", "/a/b"),
        ("/a/b", "\n"),
        ("/H_1_S_2_U_3", "/H_1_S_2_U_4"),
    ]
    for number, (first, second) in enumerate(cases):
        a, b = pair(
            tmp_path / f"case{number}",
            before={"c.clip_node": clip(first)},
            after={"c.clip_node": clip(second)},
        )
        assert compare_setups(a, b)["changed"] is False


def test_a_clip_name_that_differs_is_a_change_though_the_clip_uid_path_is_ignored(
    tmp_path: Path,
) -> None:
    a, b = pair(
        tmp_path,
        before={"c.clip_node": "<Clip><ClipUIDPath>/a</ClipUIDPath><ClipName>c</ClipName></Clip>"},
        after={"c.clip_node": "<Clip><ClipUIDPath>/b</ClipUIDPath><ClipName>d</ClipName></Clip>"},
    )
    [hunk] = compare_setups(a, b)["files"][0]["hunks"]
    assert hunk["before"] == ["<ClipName>c</ClipName>"]
    assert hunk["after"] == ["<ClipName>d</ClipName>"]


def test_an_empty_batch_date_is_dropped_and_not_turned_into_a_difference(tmp_path: Path) -> None:
    filled = batch_xml()
    empty = filled.replace("<BatchDate>1792177580</BatchDate>", "<BatchDate></BatchDate>")
    assert empty != filled
    a, b = pair(tmp_path, before_batch=filled, after_batch=empty)
    assert compare_setups(a, b)["changed"] is False


def test_an_ignored_empty_tag_is_dropped_in_both_empty_forms(tmp_path: Path) -> None:
    a, b = pair(
        tmp_path,
        before={"x.xml": "<A><Uncollapsed></Uncollapsed><B>1</B></A>"},
        after={"x.xml": "<A><Uncollapsed/><B>1</B></A>"},
    )
    assert compare_setups(a, b)["changed"] is False
    c, d = pair(
        tmp_path / "plain",
        before={"x.xml": "<A><Uncollapsed> </Uncollapsed><B>1</B></A>"},
        after={"x.xml": "<A><B>1</B></A>"},
    )
    assert compare_setups(c, d)["changed"] is False


def test_an_empty_element_with_a_greater_than_in_an_attribute_is_not_normalized(
    tmp_path: Path,
) -> None:
    a, b = pair(
        tmp_path,
        before={"x.xml": '<A><X a=">"></X></A>'},
        after={"x.xml": '<A><X a=">"/></A>'},
    )
    assert compare_setups(a, b)["changed"] is True


def test_a_node_added_to_the_batch_file_is_named_in_where(tmp_path: Path) -> None:
    before = batch_xml().replace(
        "<Links/>", "<Node><Name>render1</Name><Note>a</Note></Node><Links/>"
    )
    after = before.replace(
        "<Links/>", "<Node><Type>Blur</Type><Name>p97_blur</Name></Node><Links/>"
    )
    a, b = pair(tmp_path, before_batch=before, after_batch=after)
    hunks = compare_setups(a, b)["files"][0]["hunks"]
    assert [h["where"]["node"] for h in hunks] == ["p97_blur"]
    removed = compare_setups(b, a)["files"][0]["hunks"]
    assert [h["where"]["node"] for h in removed] == ["p97_blur"]
