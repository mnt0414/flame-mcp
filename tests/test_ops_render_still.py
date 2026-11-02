"""Tests for `render.still`: one frame of a clip written by Flame and turned into a small PNG.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py); the fake `PyExporter` and `media_panel.copy` are those of
fake_media_tree.py. ffmpeg is a stub script (STUB below) that writes a PNG of the size the scale
filter asks for, prints a warning on stderr like the real one does for Flame's PNG padding, and
logs its arguments. The tests never touch a real Flame, a real ffmpeg or the real stills
directory."""

from __future__ import annotations

import ast
import base64
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import types
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

import fake_media_tree
from fake_media_tree import PyClip, uid_of
from flame_mcp import render_files, still_proxy, still_run
from flame_mcp import server as server_mod
from flame_mcp.envelope import build_envelope
from flame_mcp.journal import Journal
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.server import create_server
from journal_test_helpers import Rig, make_rig
from timeline_p32a_helpers import CLIPX, S1, make_timeline_rig, wt

OP = "render.still"
PRESET_REL = "export/presets/flame/file_sequence/Tiff/Tiff (16-bit LZW).xml"
TEMP_PREFIX = "flame_mcp_still_"
REEL = "flame_mcp_still"

STUB = """#!{python}
import json, os, re, struct, sys, time, zlib

args = sys.argv[1:]
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\\n")
if "-version" in args:
    print("ffmpeg version stub")
    sys.exit(0)
sys.stderr.write("[png @ 0x1] Invalid PNG signature 0x0000000000000000.\\n")
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
if os.environ.get("STUB_EXIT"):
    sys.stderr.write("stub failure\\n")
    sys.exit(int(os.environ["STUB_EXIT"]))
source = args[args.index("-i") + 1]
if not os.path.exists(source):
    sys.stderr.write("No such file\\n")
    sys.exit(1)
sys.stderr.write("Input #0, tiff_pipe\\n  Stream #0:0: Video: tiff, rgb48le, 1920x1080, 25 fps\\n")
wanted = int(re.search(r"min\\((\\d+),iw\\)", args[args.index("-vf") + 1]).group(1))
width = min(wanted, 1920)
height = (width * 9 // 16) // 2 * 2


def chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


pad = b"x" * (width * int(os.environ.get("STUB_BYTES_PER_PX", "100")))
png = (
    b"\\x89PNG\\r\\n\\x1a\\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    + chunk(b"tEXt", b"pad\\x00" + pad)
    + chunk(b"IDAT", zlib.compress(b"\\x00" * (1 + width * 3) * height))
    + chunk(b"IEND", b"")
)
with open(args[-1], "wb") as fh:
    fh.write(png)
"""


@pytest.fixture
def stub_log(tmp_path: Path) -> Path:
    return tmp_path / "ffmpeg_calls.jsonl"


@pytest.fixture(autouse=True)
def still_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_log: Path) -> dict[str, Path]:
    """A stub ffmpeg, a stills directory and a presets directory of the test's own."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffmpeg = bin_dir / "ffmpeg-stub"
    ffmpeg.write_text(STUB.replace("{python}", sys.executable), encoding="utf-8")
    ffmpeg.chmod(0o755)
    stills = tmp_path / "stills"
    presets = tmp_path / "presets"
    preset = presets / "2025.2.4" / PRESET_REL
    preset.parent.mkdir(parents=True)
    preset.write_text("<preset><type>image</type></preset>", encoding="utf-8")
    monkeypatch.setenv(still_proxy.FFMPEG_ENV, str(ffmpeg))
    monkeypatch.setenv(still_proxy.STILLS_DIR_ENV, str(stills))
    monkeypatch.setenv(still_run.PRESETS_DIR_ENV, str(presets))
    monkeypatch.setenv("STUB_LOG", str(stub_log))
    for name in ("STUB_SLEEP", "STUB_EXIT", "STUB_BYTES_PER_PX"):
        monkeypatch.delenv(name, raising=False)
    return {"ffmpeg": ffmpeg, "stills": stills, "presets": presets}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def clipx(rig: Rig) -> PyClip:
    clip: PyClip = rig.handles["clipx"]
    assert clip.duration.frame == 24 and clip.start_frame == 1
    return clip


def still(rig: Rig, source: Any = None, **extra: Any) -> dict[str, Any]:
    args = {"source": source if source is not None else {"target": CLIPX, "frame": 8}, **extra}
    return rig.do(OP, args)


def intents(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "intent"]


def outcomes(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "outcome"]


def record_types(rig: Rig) -> list[str]:
    return [str(r["type"]) for r in rig.records()]


def blocked_groups(rig: Rig) -> list[str]:
    return [b.group for b in rig.runtime.guard.blocked()]


def flame_calls(rig: Rig, marker: str) -> list[Any]:
    return [c for c in rig.exec.calls if c.label == OP and marker in c.code]


def ffmpeg_calls(stub_log: Path) -> list[list[str]]:
    if not stub_log.exists():
        return []
    return [json.loads(line) for line in stub_log.read_text(encoding="utf-8").splitlines()]


def check(rig: Rig, group: str) -> dict[str, Any]:
    env = rig.do("journal.check", {"group": group})
    assert env["ok"] is True, env
    checked: list[dict[str, Any]] = env["result"]["checked"]
    return checked[0] if checked else {}


def desktop_reels(rig: Rig) -> list[Any]:
    return list(rig.handles["reel"].parent.reels)


def still_reel(rig: Rig) -> Any | None:
    found = [r for r in desktop_reels(rig) if str(r.name.get_value()) == REEL]
    return found[0] if found else None


def make_png(path: Path, width: int = 64, height: int = 36) -> Path:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" * (1 + width * 3) * height))
        + chunk(b"IEND", b"")
    )
    return path


@pytest.fixture
def image(tmp_path: Path) -> Path:
    return tmp_path / "frame.exr"


def make_image(path: Path) -> Path:
    path.write_bytes(b"not really an exr")
    return path


# -- arguments that are refused before anything is sent ---------------------------------------


def refused_untouched(rig: Rig, env: dict[str, Any], code: str) -> None:
    assert env["ok"] is False and env["errorCode"] == code, env
    assert rig.records() == [] and rig.exec.calls == [] and rig.flame.export.calls == []
    assert still_reel(rig) is None


def test_without_ffmpeg_both_routes_are_refused_and_the_hint_names_the_variable(
    rig: Rig, image: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(still_proxy.FFMPEG_ENV)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(still_proxy, "bundled_ffmpeg", lambda: None)
    make_image(image)
    for source in ({"target": CLIPX, "frame": 8}, {"path": str(image)}):
        env = still(rig, source)
        refused_untouched(rig, env, "PRECONDITION_UNMET")
        assert still_proxy.FFMPEG_ENV in env["hint"] and "ffmpeg" in env["error"]


def test_ffmpeg_is_taken_from_the_variable_then_the_path_then_the_bundled_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, still_env: dict[str, Path]
) -> None:
    found = still_proxy.resolve_ffmpeg()
    assert found is not None and found.path == str(still_env["ffmpeg"])
    assert found.source == still_proxy.FFMPEG_ENV
    monkeypatch.delenv(still_proxy.FFMPEG_ENV)
    on_path = tmp_path / "pathbin"
    on_path.mkdir()
    (on_path / "ffmpeg").write_text("#!/bin/sh\n", encoding="utf-8")
    (on_path / "ffmpeg").chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path))
    found = still_proxy.resolve_ffmpeg()
    assert found is not None and found.path == str(on_path / "ffmpeg") and found.source == "PATH"
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(still_proxy, "bundled_ffmpeg", lambda: str(still_env["ffmpeg"]))
    found = still_proxy.resolve_ffmpeg()
    assert found is not None and found.source == "imageio_ffmpeg"


def test_a_variable_that_points_at_nothing_is_not_silently_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(still_proxy.FFMPEG_ENV, str(tmp_path / "missing"))
    assert still_proxy.resolve_ffmpeg() is None


@pytest.mark.parametrize(
    "source",
    [
        {},
        {"target": CLIPX},
        {"frame": 8},
        {"target": CLIPX, "frame": 8, "path": "/x.png"},
        {"path": "/x.png", "frame": 8},
        {"target": CLIPX, "frame": 8, "other": 1},
        {"target": CLIPX, "frame": 0},
        {"target": CLIPX, "frame": 2.5},
        {"target": CLIPX, "frame": True},
        {"target": {"kind": "batch", "name": "BG1"}, "frame": 8},
        {"path": 7},
    ],
)
def test_a_source_that_is_not_exactly_one_of_the_two_forms_is_invalid(
    rig: Rig, source: dict[str, Any]
) -> None:
    refused_untouched(rig, still(rig, source), "INVALID_ARGS")


def test_a_source_that_is_not_an_object_is_invalid(rig: Rig) -> None:
    refused_untouched(rig, rig.do(OP, {"source": "x"}), "INVALID_ARGS")
    refused_untouched(rig, rig.do(OP, {}), "INVALID_ARGS")


def test_a_path_is_refused_when_it_is_relative_missing_the_wrong_kind_or_unreadable(
    rig: Rig, tmp_path: Path, image: Path
) -> None:
    refused_untouched(rig, still(rig, {"path": "frame.exr"}), "INVALID_ARGS")
    refused_untouched(rig, still(rig, {"path": str(image)}), "PRECONDITION_UNMET")
    make_image(image)
    wrong = tmp_path / "frame.txt"
    wrong.write_bytes(b"x")
    refused_untouched(rig, still(rig, {"path": str(wrong)}), "INVALID_ARGS")
    folder = tmp_path / "folder.exr"
    folder.mkdir()
    refused_untouched(rig, still(rig, {"path": str(folder)}), "PRECONDITION_UNMET")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads whatever the mode says")
def test_a_path_that_cannot_be_read_is_refused(rig: Rig, image: Path) -> None:
    make_image(image)
    image.chmod(0)
    try:
        env = still(rig, {"path": str(image)})
    finally:
        image.chmod(0o600)
    refused_untouched(rig, env, "PRECONDITION_UNMET")
    assert "readable" in env["error"]


@pytest.mark.parametrize("name", ["a.tif", "a.TIFF", "a.exr", "a.dpx", "a.png", "a.jpg", "a.JPEG"])
def test_every_listed_extension_is_accepted(rig: Rig, tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"x")
    env = still(rig, {"path": str(path)})
    assert env["ok"] is True, env


def test_output_dir_follows_the_rules_of_render_export(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused_untouched(rig, still(rig, output_dir="relative/dir"), "INVALID_ARGS")
    refused_untouched(rig, still(rig, output_dir=str(tmp_path / "missing")), "PRECONDITION_UNMET")
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(render_files, "DEFAULT_OUTPUT_DIRS", (str(shared),))
    env = still(rig, output_dir=str(shared))
    refused_untouched(rig, env, "PRECONDITION_UNMET")
    assert "temp" in env["error"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes wherever it likes")
def test_an_output_dir_that_cannot_be_written_is_refused(rig: Rig, tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        env = still(rig, output_dir=str(locked))
    finally:
        locked.chmod(0o700)
    refused_untouched(rig, env, "PRECONDITION_UNMET")
    assert "not writable" in env["error"]


def test_an_output_dir_means_nothing_for_a_path_source(
    rig: Rig, tmp_path: Path, image: Path
) -> None:
    make_image(image)
    out = tmp_path / "out"
    out.mkdir()
    refused_untouched(rig, still(rig, {"path": str(image)}, output_dir=str(out)), "INVALID_ARGS")


def test_a_path_with_a_two_byte_character_is_refused_before_anything_is_done(
    rig: Rig, tmp_path: Path
) -> None:
    path = tmp_path / "frame_\u3042.exr"
    path.write_bytes(b"x")
    refused_untouched(rig, still(rig, {"path": str(path)}), "INVALID_ARGS")


def test_the_numbers_have_bounds(rig: Rig) -> None:
    for extra in (
        {"max_width": 15},
        {"max_width": 4097},
        {"timeout_s": 0},
        {"timeout_s": 3601},
        {"max_image_bytes": 10},
        {"inline": "yes"},
    ):
        refused_untouched(rig, still(rig, **extra), "INVALID_ARGS")
    assert still(rig, max_width=4096, max_image_bytes=1000, timeout_s=3600)["ok"] is True


# -- targets Flame refuses before anything is made ---------------------------------------------


def untouched_after_read(rig: Rig, env: dict[str, Any], code: str) -> None:
    assert env["ok"] is False and env["errorCode"] == code, env
    assert rig.records() == [] and rig.flame.export.calls == []
    assert flame_calls(rig, "_result = _st_run(") == []
    assert still_reel(rig) is None


def test_a_sequence_is_selector_type(rig: Rig) -> None:
    env = still(rig, {"target": S1, "frame": 3})
    untouched_after_read(rig, env, "SELECTOR_TYPE")
    assert "clip" in env["error"] and "PySequence" in env["error"]


def test_anything_but_a_clip_is_selector_type(rig: Rig) -> None:
    reel = wt(rig.handles["reel"].get_wiretap_node_id())
    untouched_after_read(rig, still(rig, {"target": reel, "frame": 1}), "SELECTOR_TYPE")


def test_a_clip_with_another_start_frame_uses_the_same_marks(rig: Rig) -> None:
    clipx(rig).start_frame = 1001
    env = still(rig)
    assert env["ok"] is True, env
    marks = [(c["sources"].in_mark, c["sources"].out_mark) for c in rig.flame.export.calls]
    assert marks == [(8, 9)]


def test_the_marks_are_decided_in_one_function() -> None:
    assert still_run.still_marks(8) == (8, 9)
    assert still_run.still_marks(1) == (1, 2)


@pytest.mark.parametrize("frame", [25, 100])
def test_a_frame_beyond_the_duration_is_invalid_before_a_copy_is_made(rig: Rig, frame: int) -> None:
    env = still(rig, {"target": CLIPX, "frame": frame})
    untouched_after_read(rig, env, "INVALID_ARGS")
    assert "1 to 24" in env["error"]


def test_the_first_and_the_last_frame_are_accepted(rig: Rig) -> None:
    assert still(rig, {"target": CLIPX, "frame": 1})["ok"] is True
    assert still(rig, {"target": CLIPX, "frame": 24})["ok"] is True
    marks = [(c["sources"].in_mark, c["sources"].out_mark) for c in rig.flame.export.calls]
    assert marks == [(1, 2), (24, 25)]


def test_a_missing_preset_is_refused_before_anything_is_made(
    rig: Rig, still_env: dict[str, Path]
) -> None:
    (still_env["presets"] / "2025.2.4" / PRESET_REL).unlink()
    env = still(rig)
    untouched_after_read(rig, env, "PRECONDITION_UNMET")
    assert still_run.PRESETS_DIR_ENV in env["hint"] and "2025.2.4" in env["error"]


def test_a_target_nothing_matches_is_selector_empty(rig: Rig) -> None:
    env = still(rig, {"target": wt("/wt/RG/Reel 1/NOWHERE"), "frame": 1})
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert rig.records() == []


# -- a frame that is written ------------------------------------------------------------------


def only_tif(directory: Path) -> Path:
    found = sorted(directory.glob("*.tif"))
    assert len(found) == 1, found
    return found[0]


def test_one_frame_is_exported_converted_and_the_temporary_copy_is_gone(
    rig: Rig, still_env: dict[str, Path], stub_log: Path
) -> None:
    clip = clipx(rig)
    before_reels = [str(r.name.get_value()) for r in desktop_reels(rig)]
    env = still(rig)
    assert env["ok"] is True, env
    result = env["result"]
    assert result["ran"] is True and result["frame"] == 8
    assert result["source"]["target"]["name"] == "CLIPX" and result["source"]["frame"] == 8
    assert result["source"]["target"]["uid"] == uid_of(clip)
    stills = list(still_env["stills"].iterdir())
    assert len(stills) == 1
    tif = only_tif(stills[0])
    assert tif.name.startswith(TEMP_PREFIX) and tif.name.endswith(".00000008.tif")
    assert result["original"] == {
        "path": str(tif),
        "width": 1920,
        "height": 1080,
        "format": "tiff",
    }
    proxy = result["proxy"]
    assert proxy["mimeType"] == "image/png" and proxy["width"] == 1280 and proxy["height"] == 720
    assert Path(proxy["path"]).parent == stills[0] and Path(proxy["path"]).suffix == ".png"
    assert proxy["bytes"] == Path(proxy["path"]).stat().st_size
    assert result["ffmpeg"] == {"path": str(still_env["ffmpeg"])}
    assert result["tempRemoved"] is True and result["tempReelCreated"] is True
    assert result["exportElapsedS"] is not None and "attachments" not in result
    assert env["degraded"] == []
    # nothing of the temporary copy is left, and the reel made for it is gone
    assert still_reel(rig) is None
    assert [str(r.name.get_value()) for r in desktop_reels(rig)] == before_reels
    assert clip.in_mark is None and clip.out_mark is None
    (call,) = rig.flame.export.calls
    copy = call["sources"]
    assert copy is not clip and str(copy.name.get_value()).startswith(TEMP_PREFIX)
    assert (copy.in_mark, copy.out_mark) == (8, 9)
    assert call["between_marks"] is True and call["foreground"] is True
    assert call["output_dir"] == str(stills[0])
    assert call["preset"].endswith(PRESET_REL)
    assert env["affected"] == 1 and env["undoSteps"] == 0
    assert env["journal"]["undo"] == "unsupported" and env["journal"]["revert"] is None
    assert len(ffmpeg_calls(stub_log)) == 1


def test_the_default_directory_and_what_is_in_it_are_private(
    rig: Rig, still_env: dict[str, Path]
) -> None:
    env = still(rig)
    assert env["ok"] is True, env
    (directory,) = list(still_env["stills"].iterdir())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(still_env["stills"].stat().st_mode) == 0o700
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_a_directory_of_the_users_is_used_as_it_is_and_the_proxy_goes_to_the_stills_directory(
    rig: Rig, tmp_path: Path, still_env: dict[str, Path]
) -> None:
    out = tmp_path / "originals"
    out.mkdir()
    env = still(rig, output_dir=str(out))
    assert env["ok"] is True, env
    tif = only_tif(out)
    assert env["result"]["original"]["path"] == str(tif)
    proxy_dir = Path(env["result"]["proxy"]["path"]).parent
    assert proxy_dir.parent == still_env["stills"] and proxy_dir != out
    assert list(out.iterdir()) == [tif]


def test_a_directory_that_is_not_empty_is_refused_like_render_export(
    rig: Rig, tmp_path: Path
) -> None:
    out = tmp_path / "originals"
    out.mkdir()
    (out / "old.tif").write_bytes(b"x")
    env = still(rig, output_dir=str(out))
    untouched_after_read(rig, env, "PRECONDITION_UNMET")
    assert "not empty" in env["error"] and env["result"]["existing"]["count"] == 1
    out2 = tmp_path / "hidden_only"
    out2.mkdir()
    (out2 / ".DS_Store").write_bytes(b"x")
    assert still(rig, output_dir=str(out2))["ok"] is True


def test_the_intent_is_journaled_before_the_copy_is_made(rig: Rig, stub_log: Path) -> None:
    env = still(rig)
    (intent,) = intents(rig)
    assert intent["op"] == OP and intent["kind"] == "still"
    assert intent["target"] == CLIPX and intent["uid"] == uid_of(clipx(rig))
    assert intent["frame"] == 8 and intent["marks"] == [8, 9] and intent["duration"] == 24
    assert intent["tempReel"] == REEL and intent["tempReelPreexisting"] is False
    assert intent["tempName"].startswith(TEMP_PREFIX)
    assert intent["outputDir"] and intent["expectedFrames"] == 1
    assert intent["handleSource"] == "client" and len(intent["handle"]) == 32
    (run,) = flame_calls(rig, "_result = _st_run(")
    assert "intent" in run.journal_types and run.handle == intent["handle"]
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is True and outcome["tempRemoved"] is True
    assert outcome["verdict"] == "landed" and outcome["writtenCount"] == 1
    assert record_types(rig) == ["intent", "outcome"]
    assert env["journal"]["id"] == intent["id"]


def test_an_existing_reel_is_used_and_kept(rig: Rig) -> None:
    group = rig.handles["reel"].parent
    reel = group.create_reel(REEL)
    keeper = rig.handles["clipx"]
    rig.flame.media_panel.copy(keeper, reel)
    keeper_uids = [uid_of(c) for c in reel.clips]
    env = still(rig)
    assert env["ok"] is True, env
    assert env["result"]["tempReelCreated"] is False and env["result"]["tempRemoved"] is True
    after = still_reel(rig)
    assert after is not None and after is reel
    assert [uid_of(c) for c in after.clips] == keeper_uids
    assert intents(rig)[0]["tempReelPreexisting"] is True


def test_the_copy_goes_into_the_first_reel_group_of_the_desktop(rig: Rig) -> None:
    first = rig.handles["reel"].parent
    seen: list[Any] = []
    rig.flame.export.on_export = lambda copy: seen.append(copy.parent.parent)
    assert still(rig)["ok"] is True
    assert seen == [first]


def test_the_flame_code_has_the_loops_in_the_prelude_only(rig: Rig) -> None:
    still(rig)
    (read,) = flame_calls(rig, "_result = _st_inspect(")
    (run,) = flame_calls(rig, "_result = _st_run(")
    for call in (read, run):
        ast.parse(call.code)
        assert call.code.isascii()
    own = run.code.split("__op_src = ", 1)[1].split("\n", 1)[0]
    tree = ast.parse(ast.literal_eval(own))
    banned = (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    assert not [n for n in ast.walk(tree) if isinstance(n, banned)]


# -- judging ----------------------------------------------------------------------------------


def test_no_file_is_a_silent_noop_and_the_copy_is_still_removed(rig: Rig) -> None:
    rig.flame.export.writes = False
    env = still(rig)
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["tempRemoved"] is True and still_reel(rig) is None
    assert "proxy" not in env["result"] or env["result"]["proxy"] is None
    (outcome,) = outcomes(rig)
    assert outcome["errorCode"] == "SILENT_NOOP" and outcome["tempRemoved"] is True
    assert blocked_groups(rig) == []


def test_two_files_are_a_flame_api_error(rig: Rig) -> None:
    rig.flame.export.extra = ["second.tif"]
    env = still(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "2" in env["error"] and env["result"]["tempRemoved"] is True
    assert outcomes(rig)[0]["errorCode"] == "FLAME_API_ERROR"


def test_an_export_that_raises_is_a_flame_api_error_and_the_copy_is_removed(rig: Rig) -> None:
    rig.flame.export.raises = "disk full"
    env = still(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "disk full" in env["error"] and env["result"]["tempRemoved"] is True
    assert still_reel(rig) is None


def test_a_file_that_appears_before_the_export_starts_is_a_conflict_and_no_copy_is_made(
    rig: Rig, tmp_path: Path
) -> None:
    out = tmp_path / "originals"
    out.mkdir()

    def appear(label: str | None) -> None:
        if label == OP and "_result = _st_run(" in rig.exec.calls[-1].code:
            (out / "someone_elses.mov").write_bytes(b"theirs")

    rig.exec.before_run = appear
    env = still(rig, output_dir=str(out))
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert rig.flame.export.calls == [] and still_reel(rig) is None
    assert env["result"]["tempRemoved"] is None or env["result"]["tempRemoved"] is True
    assert list(out.iterdir()) == [out / "someone_elses.mov"]


def test_a_clip_that_is_not_the_one_read_is_a_conflict_and_nothing_is_copied(rig: Rig) -> None:
    def swap(label: str | None) -> None:
        if label == OP and "_result = _st_run(" in rig.exec.calls[-1].code:
            clipx(rig).uid.set_value("uid-other")

    rig.exec.before_run = swap
    env = still(rig)
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert rig.flame.export.calls == [] and rig.flame.media_panel.copy_calls == []
    assert still_reel(rig) is None


def test_a_copy_that_was_renamed_or_replaced_meanwhile_is_not_deleted(rig: Rig) -> None:
    def rename(copy: Any) -> None:
        copy.name.set_value("somebody renamed me")

    rig.flame.export.on_export = rename
    env = still(rig)
    assert env["ok"] is True, env
    assert env["result"]["tempRemoved"] is False
    reel = still_reel(rig)
    assert reel is not None and [str(c.name.get_value()) for c in reel.clips] == [
        "somebody renamed me"
    ]
    note = " ".join(env["degraded"])
    assert "somebody renamed me" not in note or "desktop.delete" in note
    assert "flame_mcp_still" in note and "desktop.delete" in note
    assert outcomes(rig)[0]["tempRemoved"] is False


def test_a_copy_with_another_uid_is_not_deleted(rig: Rig) -> None:
    rig.flame.export.on_export = lambda copy: copy.uid.set_value("uid-changed")
    env = still(rig)
    assert env["result"]["tempRemoved"] is False
    reel = still_reel(rig)
    assert reel is not None and len(reel.clips) == 1


def test_a_copy_that_cannot_be_deleted_is_reported_with_where_it_is(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(obj: Any, confirm: bool = True) -> bool:
        raise RuntimeError("busy")

    monkeypatch.setattr(rig.flame, "delete", refuse)
    env = still(rig)
    assert env["ok"] is True and env["result"]["tempRemoved"] is False, env
    assert "busy" in " ".join(env["degraded"])
    assert still_reel(rig) is not None


def stills_left(still_env: dict[str, Path]) -> list[Path]:
    root = still_env["stills"]
    return sorted(root.iterdir()) if root.exists() else []


def test_a_conflict_leaves_no_empty_run_directory(rig: Rig, still_env: dict[str, Path]) -> None:
    def swap(label: str | None) -> None:
        if label == OP and "_result = _st_run(" in rig.exec.calls[-1].code:
            clipx(rig).uid.set_value("uid-other")

    rig.exec.before_run = swap
    assert still(rig)["errorCode"] == "CONFLICT"
    assert stills_left(still_env) == []


def test_two_files_leave_no_empty_run_directory_when_the_user_gave_one(
    rig: Rig, tmp_path: Path, still_env: dict[str, Path]
) -> None:
    out = tmp_path / "originals"
    out.mkdir()
    rig.flame.export.extra = ["second.tif"]
    env = still(rig, output_dir=str(out))
    assert env["errorCode"] == "FLAME_API_ERROR", env
    assert len(list(out.iterdir())) == 2 and stills_left(still_env) == []


def test_a_failed_path_conversion_leaves_no_empty_run_directory(
    rig: Rig, image: Path, still_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_image(image)
    monkeypatch.setenv("STUB_EXIT", "1")
    assert still(rig, {"path": str(image)})["ok"] is False
    assert stills_left(still_env) == []


# -- what the temporary copy and reel leave -----------------------------------------------------


def put_a_sequence_in_the_reel(rig: Rig) -> None:
    def add(copy: Any) -> None:
        rig.flame.media_panel.copy(rig.handles["s1"], copy.parent)

    rig.flame.export.on_export = add


def test_a_reel_that_holds_a_sequence_is_not_deleted_with_the_copy(rig: Rig) -> None:
    put_a_sequence_in_the_reel(rig)
    env = still(rig)
    assert env["ok"] is True and env["result"]["tempRemoved"] is True, env
    reel = still_reel(rig)
    assert reel is not None and len(reel.sequences) == 1 and reel.clips == []
    assert env["result"]["tempReelCreated"] is True
    assert "flame_mcp_still" in " ".join(env["degraded"])


def test_check_does_not_delete_a_reel_that_holds_a_sequence(rig: Rig) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)
    group = still(rig, timeout_s=1)["journal"]["group"]
    (intent,) = intents(rig)
    leave_a_copy(rig, intent)
    reel = still_reel(rig)
    assert reel is not None
    rig.flame.media_panel.copy(rig.handles["s1"], reel)
    entry = check(rig, group)
    assert len(entry["cleanup"]["removed"]) == 1 and entry["cleanup"]["reelRemoved"] is False
    after = still_reel(rig)
    assert after is not None and after is reel and len(after.sequences) == 1


def test_check_does_not_delete_an_empty_reel_when_it_deleted_no_copy(rig: Rig) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)
    group = still(rig, timeout_s=1)["journal"]["group"]
    assert intents(rig)[0]["tempReelPreexisting"] is False
    rig.handles["reel"].parent.create_reel(REEL)  # somebody else's reel of the same name
    entry = check(rig, group)
    assert entry["cleanup"]["found"] == 0 and entry["cleanup"]["reelRemoved"] is False
    assert still_reel(rig) is not None


class RefusesRename(fake_media_tree.FakeAttr):
    def set_value(self, value: Any) -> None:
        raise RuntimeError("name is locked")


RefusesRename.__name__ = "PyAttribute"


def lock_the_name_of_new_copies(rig: Rig, extra_copies: int = 0, copies: bool = True) -> None:
    real = rig.flame.media_panel.copy

    def copy(source: Any, destination: Any, duplicate_action: str = "add") -> Any:
        if not copies:
            return []
        made = real(source, destination, duplicate_action)
        for _ in range(extra_copies):
            real(source, destination, duplicate_action)
        if not extra_copies:
            for one in made:
                one.name = RefusesRename(one.name.get_value())
        return made

    rig.flame.media_panel.copy = copy


def test_a_copy_whose_rename_fails_is_deleted_by_its_uid_and_reported(rig: Rig) -> None:
    lock_the_name_of_new_copies(rig)
    env = still(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "name is locked" in env["error"] and rig.flame.export.calls == []
    result = env["result"]
    assert result["tempRemoved"] is True and still_reel(rig) is None
    assert result["copy"]["name"] == "CLIPX" and result["copy"]["uid"]
    assert result["copy"]["wiretapId"]
    assert env["degraded"] == []


def test_a_copy_whose_rename_fails_and_that_cannot_be_deleted_is_named_in_the_note(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_the_name_of_new_copies(rig)

    def refuse(obj: Any, confirm: bool = True) -> bool:
        raise RuntimeError("busy")

    monkeypatch.setattr(rig.flame, "delete", refuse)
    env = still(rig)
    assert env["ok"] is False and env["result"]["tempRemoved"] is False, env
    reel = still_reel(rig)
    assert reel is not None
    (copy,) = reel.clips
    note = " ".join(env["degraded"])
    assert uid_of(copy) in note and copy.get_wiretap_node_id() in note
    assert "'CLIPX'" in note and "desktop.delete" in note
    assert env["result"]["copy"] == {
        "uid": uid_of(copy),
        "name": "CLIPX",
        "wiretapId": copy.get_wiretap_node_id(),
    }


def test_a_copy_that_cannot_be_deleted_gives_its_uid_and_wiretap_id(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(obj: Any, confirm: bool = True) -> bool:
        raise RuntimeError("busy")

    monkeypatch.setattr(rig.flame, "delete", refuse)
    env = still(rig)
    reel = still_reel(rig)
    assert reel is not None
    (copy,) = reel.clips
    note = " ".join(env["degraded"])
    assert uid_of(copy) in note and copy.get_wiretap_node_id() in note
    assert str(copy.name.get_value()) in note


def test_two_new_clips_after_the_copy_are_all_named_and_none_is_deleted(rig: Rig) -> None:
    lock_the_name_of_new_copies(rig, extra_copies=1)
    env = still(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    reel = still_reel(rig)
    assert reel is not None and len(reel.clips) == 2 and rig.flame.export.calls == []
    note = " ".join(env["degraded"])
    assert "2 new clip" in note and "none was deleted" in note
    for one in reel.clips:
        assert uid_of(one) in note and one.get_wiretap_node_id() in note


def test_no_new_clip_after_the_copy_still_gets_a_note(rig: Rig) -> None:
    lock_the_name_of_new_copies(rig, copies=False)
    env = still(rig)
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    note = " ".join(env["degraded"])
    assert "0 new clip" in note and "flame_mcp_still" in note
    assert rig.flame.export.calls == [] and still_reel(rig) is None


# -- the proxy --------------------------------------------------------------------------------


def test_the_proxy_command_has_no_shell_a_timeout_and_the_filter_that_never_enlarges(
    rig: Rig, stub_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    real_run = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append({"args": args, "kwargs": kwargs})
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    env = still(rig, max_width=640)
    assert env["ok"] is True, env
    (call,) = seen
    assert isinstance(call["args"][0], list) and not call["kwargs"].get("shell")
    assert call["kwargs"]["timeout"] == still_proxy.FFMPEG_TIMEOUT_S
    (argv,) = ffmpeg_calls(stub_log)
    assert argv[argv.index("-i") + 1] == env["result"]["original"]["path"]
    assert argv[-1] == env["result"]["proxy"]["path"]
    vf = argv[argv.index("-vf") + 1]
    assert "min(640,iw)" in vf and ":-2" in vf.replace("h=-2", ":-2") and "lanczos" in vf
    assert argv[argv.index("-pix_fmt") + 1] == "rgb24"
    assert "-y" in argv and "-nostdin" in argv
    assert env["result"]["proxy"]["width"] == 640


def test_a_source_smaller_than_max_width_is_not_enlarged(rig: Rig) -> None:
    env = still(rig, max_width=4096)
    assert env["result"]["proxy"]["width"] == 1920


def test_stderr_noise_is_not_a_failure_when_the_exit_code_is_zero(rig: Rig) -> None:
    env = still(rig)
    assert env["ok"] is True, env
    assert env["degraded"] == []


def assert_proxy_missing_but_call_ok(rig: Rig, env: dict[str, Any], reason: str) -> None:
    assert env["ok"] is True and env["errorCode"] is None, env
    result = env["result"]
    original = result["original"]["path"]
    assert Path(original).is_file() and result["proxy"] is None
    assert "attachments" not in result
    (note,) = env["degraded"]
    assert reason in note and original in note and "{path}" in note
    assert result["tempRemoved"] is True and result["writtenCount"] == 1
    assert outcomes(rig)[0]["ok"] is True and blocked_groups(rig) == []


def test_a_nonzero_exit_leaves_the_call_ok_with_the_original_and_a_note(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_EXIT", "1")
    assert_proxy_missing_but_call_ok(rig, still(rig, inline=True), "stub failure")


def test_an_ffmpeg_that_hangs_is_stopped_by_the_timeout_and_the_call_stays_ok(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_SLEEP", "5")
    monkeypatch.setattr(still_proxy, "FFMPEG_TIMEOUT_S", 0.3)
    assert_proxy_missing_but_call_ok(rig, still(rig), "timed out")


def test_an_ffmpeg_that_exits_zero_without_a_file_leaves_the_call_ok(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lazy = tmp_path / "bin" / "lazy"
    lazy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    lazy.chmod(0o755)
    monkeypatch.setenv(still_proxy.FFMPEG_ENV, str(lazy))
    assert_proxy_missing_but_call_ok(rig, still(rig), "no file")


def test_a_file_that_cannot_be_made_private_leaves_the_call_ok(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Any) -> None:
        raise PermissionError("not yours")

    monkeypatch.setattr(still_proxy, "make_private_file", refuse)
    monkeypatch.setattr(still_run, "make_private_file", refuse)
    assert_proxy_missing_but_call_ok(rig, still(rig), "not yours")


def test_the_proxy_has_a_fixed_name_and_ffmpeg_reads_the_input_as_one_file(
    rig: Rig, stub_log: Path
) -> None:
    env = still(rig)
    assert Path(env["result"]["proxy"]["path"]).name == "proxy.png"
    (argv,) = ffmpeg_calls(stub_log)
    assert argv[argv.index("-update") + 1] == "1"
    assert argv[argv.index("-pattern_type") + 1] == "none"
    assert argv[argv.index("-f") + 1] == "image2" and argv.index("-f") < argv.index("-i")
    assert argv.index("-pattern_type") < argv.index("-i")
    assert argv.index("-update") < len(argv) - 1 and argv[-1].endswith("/proxy.png")


def test_an_original_whose_name_holds_a_percent_sign_is_converted(rig: Rig, tmp_path: Path) -> None:
    path = tmp_path / "frame%03d.exr"
    path.write_bytes(b"x")
    env = still(rig, {"path": str(path)})
    assert env["ok"] is True, env
    assert Path(env["result"]["proxy"]["path"]).name == "proxy.png"
    assert env["result"]["original"]["path"] == str(path)


REAL_FFMPEG = shutil.which("ffmpeg")


REAL_FORMATS = [
    # (file name, pixel format ffmpeg writes it in, format name ffmpeg reports when reading it)
    ("frame%03d.tif", "rgb48le", "tiff"),
    ("plain.tif", "rgb48le", "tiff"),
    ("star*[1].tif", "rgb48le", "tiff"),
    ("plain.exr", "gbrpf32le", "exr"),
    ("frame%03d.exr", "gbrpf32le", "exr"),
    ("plain.dpx", "rgb48le", "dpx"),
    ("frame%03d.dpx", "rgb48le", "dpx"),
    ("plain.png", "rgb48be", "png"),
]


@pytest.mark.skipif(REAL_FFMPEG is None, reason="no ffmpeg on this machine")
@pytest.mark.parametrize(("name", "pix_fmt", "fmt"), REAL_FORMATS)
def test_the_real_ffmpeg_takes_the_arguments(
    tmp_path: Path, name: str, pix_fmt: str, fmt: str
) -> None:
    assert REAL_FFMPEG is not None
    made = tmp_path / ("made." + name.rsplit(".", 1)[1])
    done = subprocess.run(
        [
            REAL_FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360",
            "-frames:v",
            "1",
            "-pix_fmt",
            pix_fmt,
            str(made),
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if done.returncode != 0 or not made.is_file():
        pytest.skip(f"this ffmpeg cannot write {made.suffix} as {pix_fmt}")
    source = tmp_path / name
    made.rename(source)
    out = tmp_path / "out"
    out.mkdir()
    proxy = still_proxy.make_proxy(
        still_proxy.Ffmpeg(REAL_FFMPEG, "PATH"), str(source), out, 320, fit_bytes=None
    )
    assert proxy.path == str(out / "proxy.png")
    assert (proxy.width, proxy.height) == (320, 180)
    assert (proxy.original_width, proxy.original_height) == (640, 360)
    assert proxy.original_format == fmt
    assert still_proxy.png_size(proxy.path) == (320, 180)


def test_the_proxy_stage_as_a_whole_has_a_time_limit(
    rig: Rig, stub_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_SLEEP", "0.6")
    monkeypatch.setattr(still_proxy, "PROXY_TOTAL_TIMEOUT_S", 1.5)
    env = still(rig, inline=True, max_image_bytes=1000)  # never fits: it would run 5 times
    assert env["ok"] is True and env["result"]["proxy"] is None, env
    (note,) = env["degraded"]
    assert "timed out" in note or "longer than" in note
    assert len(ffmpeg_calls(stub_log)) <= 4  # not the 5 an unlimited stage would run


def test_the_time_limits_are_small_enough_to_hold_the_write_lock() -> None:
    assert still_proxy.FFMPEG_TIMEOUT_S == 15 and still_proxy.PROXY_TOTAL_TIMEOUT_S == 30


# -- inline images ----------------------------------------------------------------------------


def test_inline_adds_the_attachment_and_keeps_the_size_in_the_result(rig: Rig) -> None:
    env = still(rig, inline=True, max_image_bytes=1_500_000)
    assert env["ok"] is True, env
    (attachment,) = env["result"]["attachments"]
    proxy = env["result"]["proxy"]
    assert attachment == {"path": proxy["path"], "mimeType": "image/png", "bytes": proxy["bytes"]}
    assert env["degraded"] == []


def test_a_proxy_over_the_limit_is_made_again_narrower_until_it_fits(
    rig: Rig, stub_log: Path
) -> None:
    env = still(rig, inline=True, max_image_bytes=100_000)
    assert env["ok"] is True, env
    widths = [
        int(a[a.index("-vf") + 1].split("min(")[1].split(",")[0]) for a in ffmpeg_calls(stub_log)
    ]
    assert widths == [1280, 960]
    assert env["result"]["proxy"]["width"] == 960 and env["result"]["proxy"]["bytes"] <= 100_000
    assert env["result"]["attachments"][0]["bytes"] == env["result"]["proxy"]["bytes"]


def test_a_proxy_that_never_fits_is_made_four_more_times_and_then_not_attached(
    rig: Rig, stub_log: Path
) -> None:
    env = still(rig, inline=True, max_image_bytes=1000)
    assert env["ok"] is True, env
    assert len(ffmpeg_calls(stub_log)) == 5
    assert "attachments" not in env["result"]
    (note,) = env["degraded"]
    assert "max_image_bytes" in note and env["result"]["proxy"]["bytes"] > 1000


def test_without_inline_the_proxy_is_made_once_whatever_its_size(rig: Rig, stub_log: Path) -> None:
    env = still(rig, max_image_bytes=1000)
    assert env["ok"] is True and "attachments" not in env["result"]
    assert len(ffmpeg_calls(stub_log)) == 1


# -- the path route ---------------------------------------------------------------------------


def test_a_path_source_sends_nothing_to_flame_and_journals_nothing(
    rig: Rig, image: Path, still_env: dict[str, Path], stub_log: Path
) -> None:
    make_image(image)
    env = still(rig, {"path": str(image)})
    assert env["ok"] is True, env
    assert rig.exec.calls == [] and rig.records() == [] and rig.flame.export.calls == []
    result = env["result"]
    assert result["source"] == {"path": str(image)} and result["frame"] is None
    assert result["original"] == {
        "path": str(image),
        "width": 1920,
        "height": 1080,
        "format": "tiff",
    }
    assert result["proxy"]["width"] == 1280 and result["tempRemoved"] is None
    assert Path(result["proxy"]["path"]).parent.parent == still_env["stills"]
    assert result["exportElapsedS"] is None
    assert image.read_bytes() == b"not really an exr"
    assert env["journal"] is None or env["journal"] == {} or env["journal"].get("id") is None


def test_a_path_source_is_refused_like_every_write_while_the_write_door_is_shut(
    rig: Rig, image: Path
) -> None:
    make_image(image)
    rig.runtime.guard.block("i1", "g1", "render.batch", "TIMEOUT")
    for source in ({"path": str(image)}, {"target": CLIPX, "frame": 8}):
        assert still(rig, source)["errorCode"] == "POLICY_DENIED"
    assert rig.exec.calls == [] and rig.records() == []


def test_a_path_source_that_ffmpeg_cannot_read_fails_without_touching_it(
    rig: Rig, image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_image(image)
    monkeypatch.setenv("STUB_EXIT", "1")
    env = still(rig, {"path": str(image)})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert image.read_bytes() == b"not really an exr"


# -- an answer that never came -----------------------------------------------------------------


def test_a_timeout_closes_the_door_and_the_hint_names_the_still(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    env = still(rig, timeout_s=1)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    group = env["journal"]["group"]
    assert group in blocked_groups(rig) and outcomes(rig) == []
    assert "journal.check" in env["hint"] and "render" not in env["hint"].lower()
    assert record_types(rig) == ["intent", "unresolved"]
    assert still(rig)["errorCode"] == "POLICY_DENIED"


def test_check_by_the_handle_finds_the_work_done_and_nothing_left(rig: Rig) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)  # the whole call ran; the answer was lost
    group = still(rig, timeout_s=1)["journal"]["group"]
    cleanups = len(flame_calls(rig, "_result = _st_cleanup("))
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert entry["current"]["writtenCount"] == 1
    assert entry["cleanup"] == {"found": 0, "removed": [], "conflicts": [], "reelRemoved": False}
    assert len(flame_calls(rig, "_result = _st_cleanup(")) == cleanups + 1
    assert blocked_groups(rig) == []


def leave_a_copy(rig: Rig, intent: dict[str, Any], **changes: Any) -> Any:
    """What a call that died inside Flame leaves: a reference copy named as the intent says, in
    the temporary reel (made, as the call would have made it)."""
    group = rig.handles["reel"].parent
    reel = still_reel(rig) or group.create_reel(intent["tempReel"])
    (copy,) = rig.flame.media_panel.copy(clipx(rig), reel)
    copy.name.set_value(changes.get("name", intent["tempName"]))
    copy.in_mark, copy.out_mark = changes.get("marks", intent["marks"])
    if "duration" in changes:
        copy.duration = type(copy.duration)(changes["duration"])
    return copy


def test_check_removes_a_copy_that_a_dead_call_left_and_the_reel_it_made(rig: Rig) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)  # Flame never ran the call
    group = still(rig, timeout_s=1)["journal"]["group"]
    (intent,) = intents(rig)
    copy = leave_a_copy(rig, intent)
    entry = check(rig, group)
    assert entry["cleanup"]["removed"] == [{"uid": uid_of(copy), "name": intent["tempName"]}]
    assert entry["cleanup"]["conflicts"] == [] and entry["cleanup"]["reelRemoved"] is True
    assert still_reel(rig) is None and rig.flame.find_by_uid(uid_of(copy)) is None
    assert entry["verdict"] == "not_landed" and blocked_groups(rig) == []


def test_check_keeps_the_reel_when_it_was_there_before_the_call(rig: Rig) -> None:
    group = rig.handles["reel"].parent
    group.create_reel(REEL)
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)
    gid = still(rig, timeout_s=1)["journal"]["group"]
    (intent,) = intents(rig)
    assert intent["tempReelPreexisting"] is True
    leave_a_copy(rig, intent)
    entry = check(rig, gid)
    assert len(entry["cleanup"]["removed"]) == 1 and entry["cleanup"]["reelRemoved"] is False
    assert still_reel(rig) is not None


@pytest.mark.parametrize(
    "changes",
    [{"duration": 10}, {"marks": (3, 4)}],
    ids=["another duration", "other marks"],
)
def test_check_does_not_delete_a_copy_that_is_not_what_the_intent_describes(
    rig: Rig, changes: dict[str, Any]
) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)
    group = still(rig, timeout_s=1)["journal"]["group"]
    (intent,) = intents(rig)
    copy = leave_a_copy(rig, intent, **changes)
    entry = check(rig, group)
    assert entry["cleanup"]["removed"] == [] and len(entry["cleanup"]["conflicts"]) == 1
    assert entry["errorCode"] == "CONFLICT" and "not deleted" in entry["detail"]
    assert rig.flame.find_by_uid(uid_of(copy)) is copy
    assert blocked_groups(rig) == []


def test_check_never_touches_a_clip_of_another_name(rig: Rig) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)
    group = still(rig, timeout_s=1)["journal"]["group"]
    (intent,) = intents(rig)
    other = leave_a_copy(rig, intent, name="my own clip")
    entry = check(rig, group)
    assert entry["cleanup"]["found"] == 0 and rig.flame.find_by_uid(uid_of(other)) is other


def test_check_without_a_handle_counts_the_file_and_still_cleans(rig: Rig) -> None:
    rig.bridge.features = ()
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = still(rig, timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "disk"
    assert entry["cleanup"]["found"] == 0


def test_a_flame_that_cannot_be_read_leaves_the_check_unknown(rig: Rig) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)
    group = still(rig, timeout_s=1)["journal"]["group"]
    rig.exec.fault(OP, "TIMEOUT", when="before")
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and blocked_groups(rig) == [group]


def test_the_journal_being_unwritable_refuses_the_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_record: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rig.journal, "append", broken)
    env = still(rig)
    assert env["ok"] is False and env["errorCode"] == "POLICY_DENIED", env
    assert rig.flame.export.calls == [] and still_reel(rig) is None


# -- no undo, no plan, policy -----------------------------------------------------------------


def test_a_still_group_cannot_be_reverted(rig: Rig) -> None:
    group = still(rig)["journal"]["group"]
    env = rig.do("journal.revert", {"group": group})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "cannot be undone" in env["error"] and "still" in env["error"]


def test_plan_run_refuses_render_still(rig: Rig) -> None:
    args = {"source": {"target": CLIPX, "frame": 8}}
    env = rig.do("plan.run", {"steps": [{"op": OP, "args": args}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    (issue,) = env["result"]["issues"]
    assert issue["op"] == OP and "occupies Flame" in issue["problems"][0]
    assert rig.exec.labels() == [] and rig.flame.export.calls == []


def test_readonly_hides_render_still(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    readonly = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = readonly.do(OP, {"source": {"path": "/x.png"}})
    assert env["errorCode"] == "POLICY_DENIED"


def test_the_declarations(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None
    assert (op.category, op.effect, op.undo, op.thread) == (
        "render",
        "create",
        "unsupported",
        "main-async",
    )
    assert [p.name for p in op.params] == [
        "source",
        "output_dir",
        "max_width",
        "inline",
        "max_image_bytes",
        "timeout_s",
    ]
    defaults = {p.name: p.default for p in op.params}
    assert defaults["max_width"] == 1280 and defaults["inline"] is False
    assert defaults["max_image_bytes"] == 1_500_000
    for needle in ("derived", "not a deliverable", "whatever its start_frame", "PySequence"):
        assert needle in op.description or needle.lower() in op.description.lower(), needle


def test_flame_version_info_says_where_ffmpeg_is(rig: Rig, still_env: dict[str, Path]) -> None:
    result = server_mod._flame_version_info_sync(rig.runtime)["result"]
    assert result["ffmpeg"] == {
        "found": True,
        "path": str(still_env["ffmpeg"]),
        "source": still_proxy.FFMPEG_ENV,
    }


def test_flame_version_info_says_when_ffmpeg_is_missing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(still_proxy.FFMPEG_ENV)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setattr(still_proxy, "bundled_ffmpeg", lambda: None)
    result = server_mod._flame_version_info_sync(rig.runtime)["result"]
    assert result["ffmpeg"] == {"found": False, "path": None, "source": None}


# -- through the MCP tool: the image rides in a second content block ---------------------------


def serve_and_call(tmp_path: Path, mode: str, arguments: dict[str, Any]) -> types.CallToolResult:
    registry = Registry()
    register_all(registry)
    server = create_server(
        registry,
        Policy.from_env({}),
        run_dir=tmp_path / "run",
        journal=Journal(tmp_path / "journal"),
        response_mode=mode,  # type: ignore[arg-type]
    )
    box: list[types.CallToolResult] = []

    async def main() -> None:
        async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                box.append(await session.call_tool("flame_do", {"op": OP, "args": arguments}))

    anyio.run(main)
    return box[0]


@pytest.mark.parametrize("mode", ["full", "compact"])
def test_inline_puts_an_image_block_after_the_text_and_not_in_the_structured_content(
    tmp_path: Path, image: Path, mode: str
) -> None:
    make_image(image)
    result = serve_and_call(tmp_path, mode, {"source": {"path": str(image)}, "inline": True})
    assert result.is_error is False
    text, picture = result.content
    assert isinstance(text, types.TextContent) and isinstance(picture, types.ImageContent)
    assert picture.mime_type == "image/png"
    data = base64.b64decode(picture.data)
    assert data.startswith(b"\x89PNG") and picture.data not in text.text
    env = result.structured_content
    assert env is not None and env["ok"] is True
    (attachment,) = env["result"]["attachments"]
    assert attachment["bytes"] == len(data) and picture.data not in json.dumps(env)
    assert json.loads(text.text) == env


def test_without_inline_the_tool_result_is_text_only(tmp_path: Path, image: Path) -> None:
    make_image(image)
    result = serve_and_call(tmp_path, "full", {"source": {"path": str(image)}})
    assert len(result.content) == 1 and isinstance(result.content[0], types.TextContent)


def test_a_failed_call_has_no_image_block(tmp_path: Path) -> None:
    result = serve_and_call(tmp_path, "full", {"source": {"path": "relative.png"}, "inline": True})
    assert result.is_error is True and len(result.content) == 1


def test_only_files_in_the_stills_directory_ride_as_images(
    tmp_path: Path, image: Path, still_env: dict[str, Path]
) -> None:
    outside = make_png(tmp_path / "outside.png")
    env = {
        "ok": True,
        "execution": {"op": OP},
        "result": {"attachments": [{"path": str(outside), "mimeType": "image/png", "bytes": 1}]},
    }
    assert still_proxy.attachment_images(env) == []
    other_op = {**env, "execution": {"op": "eval.run"}}
    assert still_proxy.attachment_images(other_op) == []


def tool_content(env_result: dict[str, Any]) -> list[Any]:
    env = build_envelope(ok=True, result=env_result, execution={"op": OP}, duration_ms=1)
    return list(server_mod._tool_result(env, "full").content)


def test_the_tool_result_does_not_read_a_link_that_leaves_the_stills_directory(
    tmp_path: Path, still_env: dict[str, Path]
) -> None:
    outside = make_png(tmp_path / "outside.png")
    directory = still_env["stills"] / "x"
    directory.mkdir(parents=True)
    link = directory / "proxy.png"
    link.symlink_to(outside)
    rows = [{"path": str(link), "mimeType": "image/png", "bytes": 1}]
    assert len(tool_content({"attachments": rows})) == 1


def test_the_tool_result_does_not_read_a_file_that_is_not_a_png(
    still_env: dict[str, Path],
) -> None:
    directory = still_env["stills"] / "x"
    directory.mkdir(parents=True)
    fake = directory / "proxy.png"
    fake.write_text("not a png at all", encoding="utf-8")
    rows = [{"path": str(fake), "mimeType": "image/png", "bytes": 1}]
    assert len(tool_content({"attachments": rows})) == 1


def test_the_tool_result_does_not_read_a_file_over_ten_megabytes(
    still_env: dict[str, Path],
) -> None:
    directory = still_env["stills"] / "x"
    directory.mkdir(parents=True)
    big = directory / "proxy.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 10_000_000)
    assert big.stat().st_size > still_proxy.MAX_IMAGE_BYTES
    rows = [{"path": str(big), "mimeType": "image/png", "bytes": 1}]
    assert len(tool_content({"attachments": rows})) == 1
    small = make_png(directory / "ok.png")
    both = [*rows, {"path": str(small), "mimeType": "image/png", "bytes": 1}]
    assert len(tool_content({"attachments": both})) == 2


def test_attachment_images_reads_what_the_stills_directory_holds(
    still_env: dict[str, Path],
) -> None:
    directory = still_env["stills"] / "x"
    directory.mkdir(parents=True)
    png = make_png(directory / "p.png")
    env = {
        "ok": True,
        "execution": {"op": OP},
        "result": {"attachments": [{"path": str(png), "mimeType": "image/png", "bytes": 1}]},
    }
    ((data, mime),) = still_proxy.attachment_images(env)
    assert mime == "image/png" and data == png.read_bytes()
    gone = {
        **env,
        "result": {"attachments": [{"path": str(directory / "no.png"), "mimeType": "image/png"}]},
    }
    assert still_proxy.attachment_images(gone) == []
    bad_kind = {**env, "result": {"attachments": [{"path": str(png), "mimeType": "text/plain"}]}}
    assert still_proxy.attachment_images(bad_kind) == []
