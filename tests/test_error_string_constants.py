"""The server selects a hint by a phrase the Flame-side prelude puts in its refusal; the
phrase lives in one server constant and these tests pin it to the prelude text."""

from __future__ import annotations

import shutil
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import transport
from flame_mcp.desktop_batch_prelude import BATCH_COPY_HELPER
from flame_mcp.desktop_delete_prelude import DELETE_HELPER
from flame_mcp.desktop_delete_run import BATCH_REEL_PHRASE
from flame_mcp.desktop_run import BATCH_COPY_PREFIX
from test_transport import SCRATCH_ROOT, FakeRawBridge, _send_json


def test_batch_copy_prefix_is_in_the_copy_prelude() -> None:
    assert f"'{BATCH_COPY_PREFIX} " in BATCH_COPY_HELPER


def test_batch_reel_phrase_is_in_the_delete_prelude() -> None:
    assert BATCH_REEL_PHRASE in DELETE_HELPER


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    d = SCRATCH_ROOT / uuid.uuid4().hex[:8]
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("unknown handle: abc", transport.UnknownHandleError),
        ("bad request", transport.BridgeProtocolError),
        ("an unknown handle: abc", transport.BridgeProtocolError),
    ],
)
def test_only_an_unknown_handle_reply_raises_the_subtype(
    sock_dir: Path, reply: str, expected: type[Exception]
) -> None:
    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        _send_json(conn, {"id": req.get("id"), "ok": False, "protocolError": reply})

    bridge = FakeRawBridge(sock_dir / "flame-mcp-fake.sock", handler)
    try:
        with pytest.raises(transport.BridgeProtocolError) as info:
            transport._send_request(bridge.sock_path, {"v": 1}, 1.0)
    finally:
        bridge.close()
    assert type(info.value) is expected
