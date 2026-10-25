"""The setup files `node.save_setup`, `batch.save_setup` and `segment.effect_save_setup` write to a
path the caller chose are made 0600 right after the save (Flame runs with umask 0 and writes them
0666). The fakes write 0666 like Flame. Only the files this call wrote are touched; a symlink, a
file that is not a regular file or not ours is left alone and listed in `notMadePrivate`."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyBatch, PyNode, PyTimelineFX
from flame_mcp.checkpoint_prelude import CHECKPOINT_HELPER
from journal_test_helpers import BG1, Rig, make_rig
from p33_helpers import CREATE_FX, SAVE_FX, a_seg
from timeline_p32a_helpers import make_timeline_rig

EXPO = {"kind": "node", "batch": BG1, "name": "exposure1"}


def mode_of(path: str | Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@dataclass
class Case:
    name: str
    rig: Rig
    out: Path
    extension: str
    has_warnings: bool

    def save(self, stem: str, **extra: Any) -> dict[str, Any]:
        path = str(self.out / stem)
        if self.name == "node.save_setup":
            return self.rig.do(self.name, {"target": EXPO, "path": path, **extra})
        if self.name == "batch.save_setup":
            return self.rig.do(self.name, {"target": BG1, "path": path, **extra})
        return self.rig.do(SAVE_FX, {"target": a_seg(), "type": "Blur", "path": path, **extra})

    def files(self, env: dict[str, Any]) -> list[str]:
        result = env["result"]
        if self.name == "node.save_setup":
            return list(result["files"])
        if self.name == "batch.save_setup":
            return list(result["written"])
        return [f["path"] for f in result["files"]]

    def after_save(
        self, monkeypatch: pytest.MonkeyPatch, hook: Callable[[str], bool | None]
    ) -> None:
        """Runs `hook(path)` right after Flame's save; a bool it answers replaces Flame's answer."""
        owner, attr = {
            "node.save_setup": (PyNode, "save_node_setup"),
            "batch.save_setup": (PyBatch, "save_setup"),
        }.get(self.name, (PyTimelineFX, "save_setup"))
        original: Callable[..., bool] = getattr(owner, attr)

        def wrapped(obj: Any, path: str) -> bool:
            returned = original(obj, path)
            override = hook(path)
            return returned if override is None else override

        monkeypatch.setattr(owner, attr, wrapped)


@pytest.fixture(params=["node.save_setup", "batch.save_setup", "segment.effect_save_setup"])
def case(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Case:
    out = tmp_path / "out"
    out.mkdir()
    name: str = request.param
    if name == "segment.effect_save_setup":
        rig = make_timeline_rig(monkeypatch, tmp_path)
        assert rig.do(CREATE_FX, {"target": a_seg(), "type": "Blur"})["ok"] is True
        return Case(name, rig, out, ".blur_node", True)
    rig = make_rig(monkeypatch, tmp_path)
    return Case(name, rig, out, ".exposure_node" if name == "node.save_setup" else ".batch", False)


def test_the_file_the_save_wrote_is_0600_and_listed_in_made_private(case: Case) -> None:
    env = case.save("fresh")
    assert env["ok"] is True, env
    files = case.files(env)
    assert len(files) == 1 and mode_of(files[0]) == 0o600
    assert env["result"]["madePrivate"] == files
    assert env["result"]["notMadePrivate"] == {}
    if case.has_warnings:
        assert env["result"]["warnings"] == []


def test_an_overwritten_file_is_0600_too(case: Case) -> None:
    first = case.files(case.save("again"))
    os.chmod(first[0], 0o666)
    os.utime(first[0], ns=(0, 0))
    env = case.save("again", overwrite=True)
    assert env["ok"] is True, env
    assert case.files(env) == first and mode_of(first[0]) == 0o600
    assert env["result"]["madePrivate"] == first


def test_a_symlink_is_not_touched_and_is_listed_with_the_reason(case: Case, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("{}")
    link = case.out / ("link" + case.extension)
    link.symlink_to(elsewhere)
    env = case.save("link", overwrite=True)
    assert env["ok"] is True, env
    assert env["result"]["madePrivate"] == []
    assert env["result"]["notMadePrivate"] == {str(link): "symlink"}
    assert mode_of(elsewhere) == 0o666  # the fake writes through the link and leaves 0666
    assert link.is_symlink()
    if case.has_warnings:
        assert any("0600" in w for w in env["result"]["warnings"])


def test_a_chmod_that_fails_does_not_fail_the_op(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_chmod = os.chmod

    def refuse(path: Any, mode: int, **kw: Any) -> None:
        if mode == 0o600 and str(path).startswith(str(case.out)):
            raise PermissionError(1, "Operation not permitted")
        real_chmod(path, mode, **kw)

    monkeypatch.setattr(os, "chmod", refuse)
    env = case.save("stubborn")
    assert env["ok"] is True, env
    files = case.files(env)
    assert env["result"]["madePrivate"] == []
    assert env["result"]["notMadePrivate"] == {files[0]: "PermissionError"}
    assert mode_of(files[0]) == 0o666
    if case.has_warnings:
        assert any("0600" in w for w in env["result"]["warnings"])


def test_a_file_next_to_it_that_this_call_did_not_write_keeps_its_mode(case: Case) -> None:
    stranger = case.out / "stranger.txt"
    stranger.write_text("x")
    os.chmod(stranger, 0o644)
    cousin = case.out / ("fresh2_old" + case.extension)
    cousin.write_text("x")
    os.chmod(cousin, 0o666)
    env = case.save("fresh2")
    assert env["ok"] is True, env
    assert mode_of(stranger) == 0o644 and mode_of(cousin) == 0o666
    assert str(stranger) not in env["result"]["madePrivate"]


def test_the_file_is_made_private_even_when_flame_answers_false(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case.after_save(monkeypatch, lambda path: False)
    env = case.save("odd")
    assert env["ok"] is False, env
    files = case.files(env)
    assert len(files) == 1 and mode_of(files[0]) == 0o600
    assert env["result"]["madePrivate"] == files


NOT_NAMED = "not named after the file asked for"


def test_a_file_that_appears_during_the_save_but_is_not_named_after_it_is_left_alone(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case.name == "batch.save_setup":
        pytest.skip("batch.save_setup only looks at .batch files")
    stranger = case.out / "zz_stranger.txt"

    def drop(path: str) -> None:
        stranger.write_text("x")
        os.chmod(stranger, 0o666)

    case.after_save(monkeypatch, drop)
    env = case.save("named")
    assert env["ok"] is True, env
    own = [p for p in case.files(env) if p != str(stranger)]
    assert len(own) == 1 and mode_of(own[0]) == 0o600
    assert mode_of(stranger) == 0o666
    assert env["result"]["madePrivate"] == own
    assert env["result"]["notMadePrivate"] == {str(stranger): NOT_NAMED}
    if case.has_warnings:
        assert any("do not start with the name" in w for w in env["result"]["warnings"])


def prelude_namespace() -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(CHECKPOINT_HELPER, namespace)
    return namespace


def test_a_directory_and_a_pipe_are_not_regular_files(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    made, refused = prelude_namespace()["_make_files_private"]([str(folder), str(pipe)])
    assert made == []
    assert refused == {str(folder): "not a regular file", str(pipe): "not a regular file"}


def test_a_file_of_another_owner_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.write_text("x")
    os.chmod(other, 0o666)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(other).st_uid + 1)
    made, refused = prelude_namespace()["_make_files_private"]([str(other)])
    assert made == [] and refused == {str(other): "not owner"}
    assert mode_of(other) == 0o666


def test_a_path_that_became_a_symlink_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.chmod not in os.supports_follow_symlinks:
        pytest.skip("os.chmod cannot leave a symlink alone on this platform")
    target = tmp_path / "target"
    target.write_text("x")
    os.chmod(target, 0o666)
    plain = tmp_path / "plain"
    plain.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target)
    real_lstat = os.lstat

    def lstat_says_plain(path: Any, **kw: Any) -> os.stat_result:
        return real_lstat(plain if str(path) == str(link) else path, **kw)

    monkeypatch.setattr(os, "lstat", lstat_says_plain)
    made, _ = prelude_namespace()["_make_files_private"]([str(link)])
    assert made == [str(link)]
    assert mode_of(target) == 0o666
