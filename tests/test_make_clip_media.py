"""scripts/make_clip_media.sh, run against a fake HOME and, where the script has to reach ffmpeg, a
fake ffmpeg and ffprobe. The one test that makes real clips needs the real ffmpeg and is skipped
without it. Nothing here touches Flame or the real Application Support directory."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_clip_media.sh"
CLIPS = ("clip1.mov", "clip2.mov", "clip3.mov")
OLD = b"existing clip"
FAKE_FFMPEG = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
for last; do :; done
printf 'fake clip' > "$last"
"""
FAKE_FFPROBE_WRONG = """#!/bin/sh
echo codec_name=h264
"""


class Env:
    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.home.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "ffmpeg.log"
        self.out = root / "media"

    def fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run(self, *args: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(self.home),
            "PATH": path if path is not None else f"{self.bin}:/usr/bin:/bin",
            "FAKE_LOG": str(self.log),
        }
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def fill_old(self, *names: str) -> None:
        self.out.mkdir(exist_ok=True)
        for name in names:
            (self.out / name).write_bytes(OLD)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_the_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_it_stops_when_ffmpeg_and_ffprobe_are_not_on_path(env: Env, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = env.run(str(env.out), path=str(empty))
    assert result.returncode == 1
    assert "ffmpeg or ffprobe is not on PATH" in result.stderr
    assert not env.out.exists()


def test_it_stops_when_only_ffprobe_is_missing(env: Env) -> None:
    only_ffmpeg = env.bin.parent / "only_ffmpeg"
    only_ffmpeg.mkdir()
    (only_ffmpeg / "ffmpeg").write_text(FAKE_FFMPEG, encoding="utf-8")
    (only_ffmpeg / "ffmpeg").chmod(0o755)
    result = env.run(str(env.out), path=str(only_ffmpeg))
    assert result.returncode == 1
    assert "ffmpeg or ffprobe is not on PATH" in result.stderr
    assert not env.out.exists()


def test_it_does_not_overwrite_an_existing_clip_and_writes_nothing_else(env: Env) -> None:
    env.fake("ffmpeg", FAKE_FFMPEG)
    env.fake("ffprobe", FAKE_FFPROBE_WRONG)
    env.fill_old("clip2.mov")
    result = env.run(str(env.out))
    assert result.returncode == 1
    assert "Already exists" in result.stderr and "clip2.mov" in result.stderr
    assert "--force" in result.stderr
    assert listing(env.out) == ["clip2.mov"]
    assert (env.out / "clip2.mov").read_bytes() == OLD
    assert not env.log.exists(), "ffmpeg must not run when a clip is already there"


def test_it_reports_every_existing_clip(env: Env) -> None:
    env.fake("ffmpeg", FAKE_FFMPEG)
    env.fake("ffprobe", FAKE_FFPROBE_WRONG)
    env.fill_old(*CLIPS)
    result = env.run(str(env.out))
    assert result.returncode == 1
    assert all(name in result.stderr for name in CLIPS)


def test_a_failed_check_leaves_existing_clips_and_the_folder_untouched(env: Env) -> None:
    env.fake("ffmpeg", FAKE_FFMPEG)
    env.fake("ffprobe", FAKE_FFPROBE_WRONG)
    env.fill_old(*CLIPS)
    result = env.run("--force", str(env.out))
    assert result.returncode != 0
    assert "clip1: the video stream is not as expected" in result.stderr
    assert listing(env.out) == list(CLIPS)
    assert all((env.out / name).read_bytes() == OLD for name in CLIPS)


def test_a_failed_check_leaves_no_clip_in_a_new_folder(env: Env) -> None:
    env.fake("ffmpeg", FAKE_FFMPEG)
    env.fake("ffprobe", FAKE_FFPROBE_WRONG)
    result = env.run(str(env.out))
    assert result.returncode != 0
    assert listing(env.out) == []


@pytest.mark.parametrize("args", [["--nope"], ["a", "b"]], ids=["unknown-option", "two-folders"])
def test_bad_arguments_exit_2_before_anything_is_written(env: Env, args: list[str]) -> None:
    result = env.run(*args)
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not (env.home / "Library").exists()


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are not installed",
)


def real_path(env: Env) -> str:
    dirs = {str(Path(shutil.which(tool) or "").parent) for tool in ("ffmpeg", "ffprobe")}
    return ":".join([*sorted(dirs), "/usr/bin", "/bin"])


@needs_ffmpeg
def test_it_makes_the_three_clips_in_the_default_folder_and_force_replaces_them(env: Env) -> None:
    path = real_path(env)
    default = env.home / "Library" / "Application Support" / "flame-mcp" / "probe_project" / "media"
    first = env.run(path=path)
    assert first.returncode == 0, first.stderr
    assert listing(default) == list(CLIPS)

    probe = shutil.which("ffprobe") or "ffprobe"
    for name in CLIPS:
        shown = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=profile,pix_fmt,width,height,r_frame_rate,nb_read_frames",
                "-of",
                "csv=p=0",
                str(default / name),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert shown == "Standard,1920,1080,yuv422p10le,24000/1001,24"

    refused = env.run(path=path)
    assert refused.returncode == 1

    (default / "clip1.mov").write_bytes(OLD)
    forced = env.run("--force", path=path)
    assert forced.returncode == 0, forced.stderr
    assert (default / "clip1.mov").read_bytes() != OLD
    assert listing(default) == list(CLIPS)


@needs_ffmpeg
def test_the_three_clips_differ_only_in_colour(env: Env) -> None:
    path = real_path(env)
    result = env.run(str(env.out), path=path)
    assert result.returncode == 0, result.stderr
    sizes = {(env.out / name).stat().st_size for name in CLIPS}
    assert len(sizes) == 3
    contents = {(env.out / name).read_bytes() for name in CLIPS}
    assert len(contents) == 3
