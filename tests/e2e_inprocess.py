"""Run a real-Flame e2e script (scripts/p2_*_e2e.py) against the FAKE Flame, in this process.

What replaces what: the script's `server_session` (a `python -m flame_mcp` subprocess and a stdio
MCP client) becomes a session object whose `call_tool` calls a real `Runtime` directly (same
registry, policy, journal directory, envelope shaping as the server), and `transport.exec` -- used
both by the ops (through `execution.run_op`) and by the script's own scaffolding (`tx`) -- runs the
generated code against a `FakeFlame` tree. Everything else (the stages, the read-backs, the checks)
is the script itself.

This is NOT evidence about a real Flame: the fake follows what the probes measured, and a script
that passes here says only that its stages, its selectors and its judgements hang together and
agree with the fake."""

from __future__ import annotations

import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import anyio
import pytest

from fake_media_tree import FakeFlame
from flame_mcp import server, transport
from flame_mcp.journal import Journal
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.runtime import Runtime
from journal_test_helpers import ExecCall, FakeBridge, ScriptedExec, bridge_result


class HarnessExec(ScriptedExec):
    """`ScriptedExec` for a script's own transport code: code that assigns no `_result` answers
    None, and an exception in the generated code is an error answer (what the bridge returns), not
    an exception in the caller."""

    def __call__(
        self,
        code: str,
        thread: str,
        timeout_s: float | None = None,
        label: str | None = None,
        *,
        run_dir: Path | None = None,
        handle: str | None = None,
    ) -> transport.BridgeResult:
        self.calls.append(ExecCall(label=label, code=code, timeout_s=timeout_s, journal_types=[]))
        namespace: dict[str, Any] = {"flame": self.flame, "__name__": "__flame_mcp_exec__"}
        try:
            exec(compile(code, "<harness>", "exec"), namespace)
        except Exception as exc:
            return bridge_result(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
        sent = handle if "client_handle" in self.bridge.features else None
        real = bridge_result(result=namespace.get("_result"))
        if sent is not None:
            self.bridge.results[sent] = real
        return real


class FakeSession:
    """What a script calls on its MCP client session: `call_tool(name, args)`."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = arguments or {}
        if name == "flame_do":
            env = self.runtime.call(str(args["op"]), args.get("args"))
        elif name == "flame_version_info":
            env = server._flame_version_info_sync(self.runtime)
        else:
            raise AssertionError(f"the harness does not fake the tool {name!r}")
        shaped = server._tool_result(env)
        return SimpleNamespace(structured_content=shaped.structured_content)


def install(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, flame: FakeFlame, tmp_path: Path
) -> HarnessExec:
    """Point `module` (an e2e script) at the fake: its `server_session`, `transport.exec` /
    `poll` / `probe_features`, and its output directory."""
    bridge = FakeBridge()
    scripted = HarnessExec(flame, Journal(tmp_path / "exec_journal"), bridge)
    monkeypatch.setattr(transport, "exec", scripted)
    monkeypatch.setattr(transport, "poll", bridge)
    monkeypatch.setattr(transport, "probe_features", bridge.probe)

    @asynccontextmanager
    async def server_session(
        ctx: Any,
        label: str,
        *,
        hooks: Any = None,
        readonly: bool = False,
        eval_enabled: bool = True,
    ) -> AsyncIterator[FakeSession]:
        registry = Registry()
        register_all(registry)
        env: dict[str, str] = {}
        if readonly:
            env["FLAME_MCP_READONLY"] = "1"
        elif eval_enabled:
            env["FLAME_MCP_ENABLE_EVAL"] = "1"
        policy = Policy.from_env(env)
        runtime = Runtime(
            registry=registry,
            policy=policy,
            journal=Journal(ctx.journal_dir),
            run_dir=ctx.run_dir,
        )
        yield FakeSession(runtime)

    monkeypatch.setattr(module, "server_session", server_session)
    for name in ("OUTPUT_DIR",):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, tmp_path / "out")
    return scripted


def run(module: ModuleType, ctx: Any) -> None:
    anyio.run(module.async_main, ctx)
