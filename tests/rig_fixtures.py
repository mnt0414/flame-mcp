"""Fixture factories shared by the test files: a file names the rig builder or the scratch root it
needs and gets the fixture, instead of repeating the same fixture body."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from journal_test_helpers import Rig


def rig_fixture(
    factory: Callable[..., Rig], **kwargs: Any
) -> Callable[[pytest.MonkeyPatch, Path], Rig]:
    """A `rig` fixture that builds the fake Flame with `factory(monkeypatch, tmp_path, **kwargs)`.
    Assign it to the fixture's name: `rig = rig_fixture(make_rig, policy=ENABLED)`."""

    @pytest.fixture
    def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
        return factory(monkeypatch, tmp_path, **kwargs)

    return rig


def scratch_dir_fixture(root: Path) -> Callable[[], Iterator[Path]]:
    """A `scratch_dir` fixture: a fresh directory under `root`, which lies under the home directory
    because a socket path in `tmp_path` is too long for AF_UNIX. Each test file has its own root
    so that parallel workers running different files never share a directory."""

    @pytest.fixture
    def scratch_dir() -> Iterator[Path]:
        root.mkdir(parents=True, exist_ok=True)
        d = root / uuid.uuid4().hex[:8]
        d.mkdir(parents=True, exist_ok=True)
        yield d
        shutil.rmtree(d, ignore_errors=True)

    return scratch_dir
