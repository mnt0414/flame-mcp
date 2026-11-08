"""A fake Flame that writes and reads setup files made of tags, for `segment.edit_effect_setup` and
`node.edit_setup`.

The effect / node setups of the other fakes are JSON. These follow what was measured of the real
files on Flame 2025.2.4: one line, no newline, `<TAG>value</TAG>` scalars next to
attributed tags and repeated ones, booleans spelled `True` / `False`, and a read-back that equals
the file that was loaded. `XmlBehaviour` makes the fake load do something else on purpose."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import FakeAttr, PyNode, PyTimelineFX

DEFAULT_XML = (
    "<TimewarpNode><TW_FrameMode>2</TW_FrameMode><TW_Bypass>False</TW_Bypass>"
    '<TW_Speed><Channel Name="Speed"><Value>100</Value></Channel></TW_Speed>'
    '<Range Enable="False" Start="1"/><Frame>1</Frame><Frame>2</Frame></TimewarpNode>'
)
EXTENSION = ".timewarp_node"
FLAME_FILE_MODE = 0o666
"""What Flame gave the setup files it wrote."""


class LoadRefused(RuntimeError):
    pass


@dataclass
class XmlBehaviour:
    load: str = "echo"
    """`echo`: the text becomes the effect's; `ignore`: True and nothing changes; `raise`; `false`:
    False and nothing changes; `normalize`: another tag changes; `reformat`: numbers get six
    decimals."""
    files: int = 1
    """How many files one save writes (the first holds the text)."""
    companions: tuple[str, ...] = ()
    """Extensions of the other files a node save writes next to the main file (a Render node's
    `lut_node`, `resize`); the main file names each one as `<Ref_ext>{stem}.ext</Ref_ext>`."""
    drifting: tuple[str, ...] = ()
    """Companions whose bytes differ in a save made after a load."""
    companion_text: str = ""
    """Added to every companion file (a tag that exists only there)."""
    save_nothing_after_load: bool = False
    save_raises_after_load: bool = False
    replace_node_on_load: bool = False
    """The node load puts a new object of the same name in the node's place in the group."""
    saved: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    loads: int = 0


def transform(text: str, how: str) -> str:
    if how == "normalize":
        return text.replace("<Frame>1</Frame>", "<Frame>9</Frame>", 1)
    if how == "reformat":
        return re.sub(
            r"<TW_FrameMode>(\d+)</TW_FrameMode>", r"<TW_FrameMode>\1.000000</TW_FrameMode>", text
        )
    return text


def write_files(path: str, text: str, files: int) -> list[str]:
    written = []
    for position in range(files):
        target = path + (EXTENSION if position == 0 else f".part{position}")
        Path(target).write_text(text if position == 0 else "{}", encoding="utf-8", newline="")
        os.chmod(target, FLAME_FILE_MODE)
        written.append(target)
    return written


def install_effect_xml(
    monkeypatch: pytest.MonkeyPatch, behaviour: XmlBehaviour | None = None
) -> XmlBehaviour:
    state = behaviour if behaviour is not None else XmlBehaviour()

    def save_setup(self: PyTimelineFX, path: str) -> bool:
        if state.save_raises_after_load and state.loads:
            raise RuntimeError("the setup could not be saved")
        if state.save_nothing_after_load and state.loads:
            return True
        state.saved += write_files(path, self.hidden.get("xml", DEFAULT_XML), state.files)
        return True

    def load_setup(self: PyTimelineFX, path: str) -> bool:
        state.loads += 1
        state.loaded.append(path)
        text = Path(path).read_bytes().decode("utf-8")
        if state.load == "raise":
            raise LoadRefused("Could not load the setup. Check the filepath.")
        if state.load == "false":
            return False
        if state.load != "ignore":
            self.hidden["xml"] = transform(text, state.load)
        return True

    monkeypatch.setattr(PyTimelineFX, "save_setup", save_setup)
    monkeypatch.setattr(PyTimelineFX, "load_setup", load_setup)
    return state


NODE_XML = "<ExposureNode><Red>{red}</Red><Bypass>{bypass}</Bypass><Mode>Auto</Mode></ExposureNode>"


def node_text(node: PyNode) -> str:
    red = node.exposure_red.get_value()
    return NODE_XML.format(red=red, bypass=bool(node.bypass.get_value()))


def install_node_xml(
    monkeypatch: pytest.MonkeyPatch, behaviour: XmlBehaviour | None = None
) -> XmlBehaviour:
    """The node's setup is its `exposure_red` and `bypass`, written as tags, so a restore of the
    Batch checkpoint (which holds attributes) puts the setup back too."""
    state = behaviour if behaviour is not None else XmlBehaviour()

    def save_node_setup(self: PyNode, path: str) -> bool:
        if state.save_raises_after_load and state.loads:
            raise RuntimeError("the node setup could not be saved")
        if state.save_nothing_after_load and state.loads:
            return True
        stem = os.path.basename(path)
        directory = os.path.dirname(path)
        base = os.path.join(directory, stem)
        if state.companions:
            refs = "".join(f"<Ref_{e}>{stem}.{e}</Ref_{e}>" for e in state.companions)
            main = base + ".exposure_node"
            Path(main).write_text(node_text(self) + refs, encoding="utf-8")
            os.chmod(main, FLAME_FILE_MODE)
            state.saved.append(main)
            for ext in state.companions:
                drift = "<Drift/>" if ext in state.drifting and state.loads else ""
                target = f"{base}.{ext}"
                Path(target).write_text(
                    f"<C>{ext}</C>{state.companion_text}{drift}", encoding="utf-8"
                )
                os.chmod(target, FLAME_FILE_MODE)
                state.saved.append(target)
            return True
        for position in range(state.files):
            target = base + (".exposure_node" if position == 0 else f".part{position}")
            Path(target).write_text(node_text(self) if position == 0 else "{}", encoding="utf-8")
            os.chmod(target, FLAME_FILE_MODE)
            state.saved.append(target)
        return True

    def load_node_setup(self: PyNode, path: str) -> Any:
        state.loads += 1
        state.loaded.append(path)
        if not os.path.isfile(path):
            return True
        if state.load == "raise":
            raise LoadRefused("Could not load the node setup.")
        if state.load == "false":
            return False
        if state.load == "ignore":
            return True
        text = Path(path).read_text(encoding="utf-8")
        red = re.search(r"<Red>([^<]*)</Red>", text)
        bypass = re.search(r"<Bypass>([^<]*)</Bypass>", text)
        if red is not None:
            self.exposure_red.set_value(float(red.group(1)))
        if bypass is not None:
            self.bypass.set_value(bypass.group(1) == "True")
        if state.replace_node_on_load and self._owner is not None:
            owner = self._owner
            fresh = PyNode(
                str(self.name.get_value()),
                str(self.type.get_value()),
                extra={"exposure_red": FakeAttr(self.exposure_red.get_value())},
            )
            fresh.bypass.set_value(self.bypass.get_value())
            fresh._owner = owner
            owner.nodes[owner.nodes.index(self)] = fresh
        return True

    monkeypatch.setattr(PyNode, "save_node_setup", save_node_setup)
    monkeypatch.setattr(PyNode, "load_node_setup", load_node_setup)
    return state
