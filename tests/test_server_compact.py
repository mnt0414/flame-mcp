"""FLAME_MCP_RESPONSE in the server: full stays what it was, compact shrinks the envelope, and
the variable is checked at startup."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from flame_mcp import applog, transport
from flame_mcp import compact as compact_mod
from flame_mcp import envelope as envelope_mod
from flame_mcp import server as server_mod
from flame_mcp.__main__ import build_server
from flame_mcp.envelope import build_envelope
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.server import create_server
from flame_mcp.types import Category, OperationSpec


def _sample_env() -> dict[str, Any]:
    items = [
        {"selector": {"kind": "wiretap", "id": f"/p/{i}"}, "wiretapId": f"/p/{i}", "name": f"c{i}"}
        for i in range(4)
    ]
    return build_envelope(
        ok=True,
        result={"total": 4, "items": items, "note": None},
        context={"project": "p", "workspace": "w", "selected": 0},
        execution={"op": "desktop.find", "thread": "main"},
        duration_ms=5,
    )


def _old_tool_result(env: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    degraded = envelope_mod.degrade_for_size(env)
    return json.dumps(degraded, default=str), degraded, not bool(degraded["ok"])


def _registry() -> Registry:
    registry = Registry(symbols=frozenset())
    registry.register_category(
        Category(
            name="testcat",
            description="test category",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    registry.register(
        OperationSpec(
            name="testcat.read_marker",
            category="testcat",
            description="read the marker",
            effect="read",
            to_python=lambda args: "_result = 1",
        )
    )
    return registry


@pytest.fixture(autouse=True)
def _fake_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(applog.APP_LOG_ENV, str(tmp_path / "app.log"))
    monkeypatch.setenv(applog.SHELL_LOG_ENV, str(tmp_path / "shell.log"))
    monkeypatch.delenv(transport.BRIDGE_SOCKET_ENV, raising=False)
    monkeypatch.delenv(compact_mod.RESPONSE_ENV, raising=False)


@asynccontextmanager
async def _session(mode: str | None, run_dir: Path) -> AsyncIterator[ClientSession]:
    if mode is None:
        server = create_server(_registry(), Policy.from_env({}), run_dir=run_dir)
    else:
        server = create_server(
            _registry(),
            Policy.from_env({}),
            run_dir=run_dir,
            response_mode=mode,  # type: ignore[arg-type]
        )
    async with InMemoryTransport(server, raise_exceptions=True) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# ---- _tool_result ------------------------------------------------------------------------------


def test_full_tool_result_is_what_it_was_before_and_never_calls_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(env: Any) -> Any:
        raise AssertionError("compact_envelope must not run in full mode")

    monkeypatch.setattr(compact_mod, "compact_envelope", boom)
    env = _sample_env()
    text, structured, is_error = _old_tool_result(env)
    for result in (server_mod._tool_result(env), server_mod._tool_result(env, "full")):
        assert result.content[0].text == text  # type: ignore[union-attr]
        assert result.structured_content == structured
        assert result.is_error is is_error


def test_compact_tool_result_text_and_structured_content_are_the_same_compact_dict() -> None:
    env = _sample_env()
    result = server_mod._tool_result(env, "compact")
    structured = result.structured_content
    assert structured is not None
    assert json.loads(result.content[0].text) == structured  # type: ignore[union-attr]
    assert result.content[0].text == json.dumps(structured)  # type: ignore[union-attr]
    assert structured == envelope_mod.degrade_for_size(compact_mod.compact_envelope(env))
    assert "execution" not in structured and "wiretapId" not in json.dumps(structured)
    assert structured["result"]["items"]["columns"] == ["selector.kind", "selector.id", "name"]
    assert len(result.content[0].text) < len(_old_tool_result(env)[0])  # type: ignore[union-attr]


def test_compact_is_error_follows_ok() -> None:
    ok_result = server_mod._tool_result(_sample_env(), "compact")
    assert ok_result.is_error is False
    failed = build_envelope(ok=False, error="x", error_code="TIMEOUT")
    result = server_mod._tool_result(failed, "compact")
    assert result.is_error is True
    assert result.structured_content is not None and result.structured_content["ok"] is False


def test_within_the_limit_the_result_is_exactly_the_compact_envelope() -> None:
    env = _sample_env()
    structured = server_mod._tool_result(env, "compact").structured_content
    assert structured == compact_mod.compact_envelope(env)


def test_over_the_limit_the_size_limit_is_applied_to_the_full_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []
    real = envelope_mod.degrade_for_size

    def spy(env: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(dict(env))
        return real(env, **kwargs)

    monkeypatch.setattr(envelope_mod, "degrade_for_size", spy)
    env = build_envelope(ok=True, result={"items": _big_items(3000)}, execution={"op": "x"})
    server_mod._tool_result(env, "compact")
    assert len(seen) == 1 and seen[0]["execution"] == {"op": "x"} and "logs" in seen[0]


def _big_items(n: int) -> list[dict[str, Any]]:
    return [
        {
            "selector": {"kind": "wiretap", "id": f"/projects/p/ws/lib/reel/clip{i:05d}"},
            "wiretapId": f"/projects/p/ws/lib/reel/clip{i:05d}",
            "name": f"clip number {i:05d}",
            "uid": f"00000000-0000-0000-0000-{i:012d}",
        }
        for i in range(n)
    ]


def test_compact_over_the_limit_still_collapses_the_list_and_says_so() -> None:
    env = build_envelope(
        ok=True, result={"total": 3000, "items": _big_items(3000)}, execution={"op": "x"}
    )
    assert (
        envelope_mod.byte_len(compact_mod.compact_envelope(env)) > envelope_mod.MAX_RESPONSE_BYTES
    )
    result = server_mod._tool_result(env, "compact")
    structured = result.structured_content
    assert structured is not None
    assert structured["result"]["items"] == {"count": 3000, "omitted": True}
    assert structured["result"]["total"] == 3000
    assert any("collapsed" in note for note in structured["degraded"])
    assert "execution" not in structured and "spilledTo" not in json.dumps(structured)
    assert json.loads(result.content[0].text) == structured  # type: ignore[union-attr]
    assert envelope_mod.byte_len(structured) <= envelope_mod.MAX_RESPONSE_BYTES


def test_compact_over_the_limit_with_a_list_result_collapses_it_whole() -> None:
    env = build_envelope(ok=True, result=_big_items(3000))
    structured = server_mod._tool_result(env, "compact").structured_content
    assert structured is not None
    assert structured["result"] == {"count": 3000, "omitted": True}
    assert any("collapsed" in note for note in structured["degraded"])


def test_compact_within_the_limit_still_makes_a_table_from_a_list_result() -> None:
    env = build_envelope(ok=True, result=_big_items(20))
    assert envelope_mod.byte_len(env) <= envelope_mod.MAX_RESPONSE_BYTES
    structured = server_mod._tool_result(env, "compact").structured_content
    assert structured is not None
    assert structured["result"]["columns"] == ["selector.kind", "selector.id", "name", "uid"]
    assert len(structured["result"]["rows"]) == 20 and "degraded" not in structured


def test_compact_that_fits_only_because_of_the_table_is_not_collapsed() -> None:
    items = _big_items(400)
    env = build_envelope(ok=True, result={"items": items})
    assert envelope_mod.byte_len(env) > envelope_mod.MAX_RESPONSE_BYTES
    compact_size = envelope_mod.byte_len(compact_mod.compact_envelope(env))
    assert compact_size <= envelope_mod.MAX_RESPONSE_BYTES
    structured = server_mod._tool_result(env, "compact").structured_content
    assert structured is not None
    assert len(structured["result"]["items"]["rows"]) == 400 and "degraded" not in structured


def test_compact_spill_writes_the_full_envelope(tmp_path: Path) -> None:
    items = _big_items(3)
    env = build_envelope(
        ok=True,
        result={"items": items, "blob": "x" * 100_000},
        execution={"op": "desktop.find"},
        logs=["a log line"],
    )
    result = server_mod._tool_result(env, "compact")
    structured = result.structured_content
    assert structured is not None
    spilled = Path(structured["result"]["spilledTo"])
    assert any("spilled" in note for note in structured["degraded"])
    body = json.loads(spilled.read_text())
    assert body["result"]["items"] == items
    assert body["execution"] == {"op": "desktop.find"} and body["logs"] == ["a log line"]
    assert "columns" not in json.dumps(body)
    assert "execution" not in structured


# ---- through an MCP session --------------------------------------------------------------------


def test_default_server_answers_in_full_through_a_session(tmp_path: Path) -> None:
    async def main() -> None:
        async with _session(None, tmp_path / "run") as session:
            result = await session.call_tool("flame_do", {"op": "no.such.op"})
            env = result.structured_content
            assert env is not None
            for key in ("result", "affected", "logs", "execution", "journal", "degraded"):
                assert key in env, key
            assert result.content[0].text == json.dumps(env)  # type: ignore[union-attr]

    anyio.run(main)


def test_compact_server_answers_compactly_with_matching_text(tmp_path: Path) -> None:
    async def main() -> None:
        async with _session("compact", tmp_path / "run") as session:
            result = await session.call_tool("flame_do", {"op": "no.such.op"})
            env = result.structured_content
            assert env is not None
            assert env["ok"] is False and env["errorCode"] == "UNKNOWN_OPERATION"
            for key in ("result", "affected", "logs", "journal", "degraded", "crash"):
                assert key not in env, key
            assert json.loads(result.content[0].text) == env  # type: ignore[union-attr]
            assert result.is_error is True

    anyio.run(main)


def test_instructions_in_full_are_unchanged_and_compact_adds_one_sentence(tmp_path: Path) -> None:
    async def main() -> None:
        async with InMemoryTransport(
            create_server(_registry(), Policy.from_env({}), run_dir=tmp_path / "r"),
            raise_exceptions=True,
        ) as (read, write):
            async with ClientSession(read, write) as session:
                assert (await session.initialize()).instructions == server_mod.INSTRUCTIONS
        compact_server = create_server(
            _registry(), Policy.from_env({}), run_dir=tmp_path / "r", response_mode="compact"
        )
        async with InMemoryTransport(compact_server, raise_exceptions=True) as (read, write):
            async with ClientSession(read, write) as session:
                text = (await session.initialize()).instructions
                assert text is not None
                assert text.startswith(server_mod.INSTRUCTIONS)
                assert compact_mod.INSTRUCTIONS_NOTE in text
                assert len(text.encode("utf-8")) <= server_mod.MAX_INSTRUCTIONS_BYTES
                assert text.isascii()

    anyio.run(main)


def test_the_full_instructions_constant_does_not_mention_compact() -> None:
    assert "compact" not in server_mod.INSTRUCTIONS.lower()
    assert len(server_mod.INSTRUCTIONS.encode("utf-8")) <= server_mod.MAX_INSTRUCTIONS_BYTES


def test_the_compact_instructions_fit_with_room_to_spare() -> None:
    size = len(server_mod.instructions_for("compact").encode("utf-8"))
    assert size <= server_mod.MAX_INSTRUCTIONS_BYTES == 2048
    assert server_mod.instructions_for("full") == server_mod.INSTRUCTIONS


def test_flame_context_has_response_format_only_in_compact(tmp_path: Path) -> None:
    async def main() -> None:
        async with _session("full", tmp_path / "run") as session:
            env = (await session.call_tool("flame_context", {})).structured_content
            assert env is not None and "responseFormat" not in env["result"]
            assert "responseFormat" not in json.dumps(env)
        async with _session("compact", tmp_path / "run") as session:
            result = await session.call_tool("flame_context", {})
            env = result.structured_content
            assert env is not None
            assert env["result"]["responseFormat"] == compact_mod.RESPONSE_FORMAT
            assert json.loads(result.content[0].text) == env  # type: ignore[union-attr]

    anyio.run(main)


def test_response_format_text_is_ascii_and_names_the_rules() -> None:
    text = compact_mod.RESPONSE_FORMAT
    assert text.isascii()
    for needle in (
        "null",
        "execution",
        "wiretapId",
        "columns",
        "rows",
        "selector.id",
        "cell can itself be a list of plain values",
    ):
        assert needle in text


# ---- startup -----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["bogus", "COMPACT", ""])
def test_startup_stops_on_an_invalid_response_mode_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    monkeypatch.setenv(compact_mod.RESPONSE_ENV, value)
    with pytest.raises(SystemExit) as info:
        build_server(run_dir=tmp_path / "run")
    assert info.value.code == 1
    err = capsys.readouterr().err
    assert "FLAME_MCP_RESPONSE" in err and repr(value) in err


@pytest.mark.parametrize(("value", "compact"), [(None, False), ("full", False), ("compact", True)])
def test_startup_passes_the_mode_to_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str | None, compact: bool
) -> None:
    if value is not None:
        monkeypatch.setenv(compact_mod.RESPONSE_ENV, value)
    server = build_server(run_dir=tmp_path / "run")
    has_note = compact_mod.INSTRUCTIONS_NOTE in (server.instructions or "")
    assert has_note is compact
