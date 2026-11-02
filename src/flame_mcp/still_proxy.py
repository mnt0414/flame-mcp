"""`render.still`, the image side: ffmpeg turns the frame Flame wrote (or a file the caller names)
into a small PNG, and `attachment_images` hands such a PNG to the MCP layer as an image block.

The PNG is a derived check image, not a deliverable. Everything here runs in the server process;
none of it sends anything to Flame. ffmpeg is found through `FLAME_MCP_FFMPEG`, then the PATH,
then the `imageio_ffmpeg` package; it is started without a shell and with a timeout, and its
result is judged by its exit code and by the output file -- never by what it prints on stderr
(it warns about the padding after Flame's PNG and TIFF files and still exits 0).
"""

from __future__ import annotations

import importlib
import os
import re
import secrets
import shutil
import struct
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from flame_mcp import private_fs

FFMPEG_ENV: Final[str] = "FLAME_MCP_FFMPEG"
STILLS_DIR_ENV: Final[str] = "FLAME_MCP_STILLS_DIR"
STILLS_DIRNAME: Final[str] = "stills"
SOURCE_PATH: Final[str] = "PATH"
SOURCE_BUNDLED: Final[str] = "imageio_ffmpeg"
FFMPEG_TIMEOUT_S: float = 15.0
"""The longest one ffmpeg run may take (a run takes 0.04 to 0.10 s on a 1920x1080 frame)."""
PROXY_TOTAL_TIMEOUT_S: float = 30.0
"""The longest the whole proxy stage may take, with the runs that make the PNG narrower again.
The server holds its write lock meanwhile, so this is the most it can keep other writes
waiting."""
DEFAULT_MAX_WIDTH: Final[int] = 1280
MIN_WIDTH: Final[int] = 16
MAX_WIDTH: Final[int] = 4096
DEFAULT_MAX_IMAGE_BYTES: Final[int] = 1_500_000
MIN_IMAGE_BYTES: Final[int] = 1000
MAX_IMAGE_BYTES: Final[int] = 10_000_000
SHRINK_FACTOR: Final[float] = 0.75
MAX_SHRINKS: Final[int] = 4
PROXY_MIME: Final[str] = "image/png"
STDERR_TAIL_CHARS: Final[int] = 300
PROXY_NAME: Final[str] = "proxy.png"
"""The one PNG of a run directory. A fixed name, so that a `%` or a glob character in the name of
the original cannot reach ffmpeg's output pattern."""
STILL_OP: Final[str] = "render.still"
SOURCE_EXTENSIONS: Final[tuple[str, ...]] = ("tif", "tiff", "exr", "dpx", "png", "jpg", "jpeg")

_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_STREAM = re.compile(r"Video: ([A-Za-z0-9_]+).*?\b(\d{2,6})x(\d{2,6})\b")


@dataclass(frozen=True)
class Ffmpeg:
    path: str
    source: str


class ProxyError(Exception):
    """ffmpeg could not make the proxy; the message says why."""


@dataclass(frozen=True)
class Proxy:
    """`path`, `width`, `height`, `bytes` of the PNG; what ffmpeg reported of the source it read
    (`original_*`, None when it did not say); `attempts` the number of times ffmpeg ran."""

    path: str
    width: int
    height: int
    bytes: int
    original_width: int | None
    original_height: int | None
    original_format: str | None
    attempts: int


def bundled_ffmpeg() -> str | None:
    try:
        module = importlib.import_module("imageio_ffmpeg")
        exe = module.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return None
    return str(exe) if isinstance(exe, str) and os.path.isfile(exe) else None


def _runnable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_ffmpeg() -> Ffmpeg | None:
    """`FLAME_MCP_FFMPEG` (taken as it is: one that points at nothing is not replaced by another
    ffmpeg, so a typo is not hidden), else `ffmpeg` on the PATH, else the `imageio_ffmpeg` one."""
    configured = os.environ.get(FFMPEG_ENV)
    if configured:
        return Ffmpeg(configured, FFMPEG_ENV) if _runnable(configured) else None
    found = shutil.which("ffmpeg")
    if found:
        return Ffmpeg(found, SOURCE_PATH)
    bundled = bundled_ffmpeg()
    return Ffmpeg(bundled, SOURCE_BUNDLED) if bundled else None


def ffmpeg_info() -> dict[str, Any]:
    """What `flame_version_info` says of ffmpeg."""
    found = resolve_ffmpeg()
    if found is None:
        return {"found": False, "path": None, "source": None}
    return {"found": True, "path": found.path, "source": found.source}


def stills_root() -> Path:
    configured = os.environ.get(STILLS_DIR_ENV)
    return Path(configured) if configured else private_fs.default_root() / STILLS_DIRNAME


def make_run_dir() -> Path:
    """A new private directory below the stills root for one call."""
    root = stills_root()
    private_fs.make_private_dirs(root)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    directory = root / name
    os.mkdir(directory, private_fs.PRIVATE_DIR_MODE)
    os.chmod(directory, private_fs.PRIVATE_DIR_MODE)
    return directory


def make_private_file(path: str | Path) -> None:
    os.chmod(path, private_fs.PRIVATE_FILE_MODE)


def png_size(path: str) -> tuple[int, int] | None:
    """Width and height from the IHDR chunk of a PNG, None for anything else."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return int(width), int(height)


def _tail(text: str) -> str:
    flat = " ".join(text.split())
    return flat[-STDERR_TAIL_CHARS:]


def _run_once(ffmpeg: Ffmpeg, source: str, out_path: Path, width: int, timeout_s: float) -> str:
    """One ffmpeg run; returns its stderr. The scale never enlarges: `min(width, iw)`. The input
    is one file whatever its name holds (`-f image2 -pattern_type none`: no `%d` sequence, no glob;
    `-pattern_type` is an option of that demuxer only, so it is named) and the output is one image
    (`-update 1`)."""
    argv = [
        ffmpeg.path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-f",
        "image2",
        "-pattern_type",
        "none",
        "-i",
        source,
        "-frames:v",
        "1",
        "-update",
        "1",
        "-vf",
        f"scale='min({width},iw)':-2:flags=lanczos",
        "-pix_fmt",
        "rgb24",
        str(out_path),
    ]
    try:
        done = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProxyError(f"ffmpeg timed out after {timeout_s:g} s") from exc
    except OSError as exc:
        raise ProxyError(f"ffmpeg could not be started: {exc}") from exc
    if done.returncode != 0:
        raise ProxyError(f"ffmpeg exited with {done.returncode}: {_tail(done.stderr)}")
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise ProxyError("ffmpeg exited with 0 but wrote no file")
    return done.stderr


def _source_facts(stderr: str) -> tuple[str | None, int | None, int | None]:
    found = _STREAM.search(stderr)
    if found is None:
        return None, None, None
    return found.group(1), int(found.group(2)), int(found.group(3))


def make_proxy(
    ffmpeg: Ffmpeg,
    source: str,
    directory: Path,
    max_width: int,
    *,
    fit_bytes: int | None,
) -> Proxy:
    """The PNG of `source` in `directory`, at most `max_width` wide. With `fit_bytes` a PNG over
    that size is made again 0.75 times as wide, at most `MAX_SHRINKS` more times; the last one
    stands whether it fits or not (the caller compares its `bytes`). One run is limited to
    `FFMPEG_TIMEOUT_S`, the whole stage to `PROXY_TOTAL_TIMEOUT_S` (a `ProxyError` past it)."""
    out_path = directory / PROXY_NAME
    width = max_width
    attempts = 0
    deadline = time.monotonic() + PROXY_TOTAL_TIMEOUT_S
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ProxyError(
                f"the proxy stage took longer than {PROXY_TOTAL_TIMEOUT_S:g} s in all "
                f"({attempts} ffmpeg run(s))"
            )
        stderr = _run_once(ffmpeg, source, out_path, width, min(FFMPEG_TIMEOUT_S, left))
        attempts += 1
        make_private_file(out_path)
        size = png_size(str(out_path))
        if size is None:
            raise ProxyError("ffmpeg wrote a file that is not a PNG")
        total = out_path.stat().st_size
        shrunk = int(min(width, size[0]) * SHRINK_FACTOR)
        done = (
            fit_bytes is None or total <= fit_bytes or attempts > MAX_SHRINKS or shrunk < MIN_WIDTH
        )
        if done:
            fmt, original_width, original_height = _source_facts(stderr)
            return Proxy(
                str(out_path),
                size[0],
                size[1],
                total,
                original_width,
                original_height,
                fmt,
                attempts,
            )
        width = shrunk


def attachment_images(env: Mapping[str, Any]) -> list[tuple[bytes, str]]:
    """The PNG files `render.still` attached (`result.attachments`), read from the stills
    directory. Only an envelope of this op counts, and only a file inside the stills directory:
    the envelope names the path, and the server never reads one from outside it."""
    execution = env.get("execution")
    if env.get("ok") is not True or not isinstance(execution, Mapping):
        return []
    if execution.get("op") != STILL_OP:
        return []
    result = env.get("result")
    rows = result.get("attachments") if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        return []
    root = stills_root().resolve()
    images: list[tuple[bytes, str]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("mimeType") != PROXY_MIME:
            continue
        try:
            path = Path(str(row.get("path"))).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            if path.stat().st_size > MAX_IMAGE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if data.startswith(_PNG_SIGNATURE):
            images.append((data, PROXY_MIME))
    return images
