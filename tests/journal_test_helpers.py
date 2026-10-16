"""Shared scaffolding for the journal tests.

A stand-in for `transport.exec` that runs the generated code against a fake Flame object tree (no
bridge, no socket), can lose an answer on purpose, and records what the journal held at the moment
each call was made.

`Rig` bundles a fake world, a scripted exec, a journal in a temp directory and a `Runtime`
(the real one), so a test reads as: build a rig, call ops, look at the journal / the fake
attributes / the calls the server made."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import transport
from flame_mcp.journal import Journal
from flame_mcp.ops import register_all
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.runtime import Runtime, TestHooks
from flame_mcp.transport import BridgeProtocolError, BridgeResult
from flame_mcp_bridge.impl import FEATURE_CLIENT_HANDLE
from test_ops_attributes import SpyAttr, make_batch_world  # noqa: F401  (re-exported)


@dataclass
class ExecCall:
    label: str | None
    code: str
    timeout_s: float | None
    journal_types: list[str]
    """`type` of every record the journal FILE held (read like another process would, so only
    what was flushed) when this call was made."""
    handle: str | None = None
    """The client handle that reached the bridge: the one the caller passed, and only when the
    (fake) bridge announces `client_handle` -- like `transport.exec`."""


@dataclass
class Fault:
    label: str
    error_code: str
    when: str  # "before": the call is lost without running; "after": it runs, the answer is lost
    times: int = 1
    skip: int = 0
    """How many calls with this label pass untouched before the fault applies."""


def _error_result(code: str) -> BridgeResult:
    return BridgeResult(
        ok=False,
        result=None,
        error_code=code,  # type: ignore[arg-type]
        error=f"scripted {code}",
        traceback=None,
        queued_ms=None,
        run_ms=None,
        timeout_phase="queued" if code == "TIMEOUT" else None,
        pending_handle=None,
        app_log=(),
        app_log_note=None,
        crash=None,
        inflight_path=None,
        duration_ms=1,
    )


def bridge_result(**over: Any) -> BridgeResult:
    fields: dict[str, Any] = {
        "ok": True,
        "result": None,
        "error_code": None,
        "error": None,
        "traceback": None,
        "queued_ms": 0,
        "run_ms": 0,
        "timeout_phase": None,
        "pending_handle": None,
        "app_log": (),
        "app_log_note": None,
        "crash": None,
        "inflight_path": None,
        "duration_ms": 1,
    }
    fields.update(over)
    return BridgeResult(**fields)


class FakeBridge:
    """Fake bridge as the server sees it through `transport.poll` / `probe_features`.

    See `_handle_poll` in the bridge and `_send_request`. A finished result is handed over
    ONCE (the bridge deletes it), a running one answers `pending`, and a handle the bridge does
    not have raises `BridgeProtocolError` ("unknown handle: ...") -- it is not a BridgeResult.
    `features` is what its `hello` announces: with `client_handle`, `ScriptedExec` files the
    result of every call that carried a handle under it, like the real bridge does.
    """

    def __init__(self) -> None:
        self.results: dict[str, BridgeResult] = {}
        self.running: set[str] = set()
        self.calls: list[str] = []
        self.failure: BridgeResult | Exception | None = None
        self.features: tuple[str, ...] = (FEATURE_CLIENT_HANDLE,)

    def probe(self, run_dir: Path | None = None) -> tuple[str, ...]:
        return self.features

    def __call__(self, handle: str, *, run_dir: Path | None = None) -> BridgeResult:
        self.calls.append(handle)
        if isinstance(self.failure, Exception):
            raise self.failure
        if self.failure is not None:
            return self.failure
        if handle in self.results:
            return self.results.pop(handle)
        if handle in self.running:
            return bridge_result(pending_handle=handle)
        raise BridgeProtocolError(f"unknown handle: {handle}")


class ScriptedExec:
    def __init__(self, flame: Any, journal: Journal, bridge: FakeBridge | None = None) -> None:
        self.flame = flame
        self.journal = journal
        self.bridge = bridge if bridge is not None else FakeBridge()
        self.calls: list[ExecCall] = []
        self.faults: list[Fault] = []
        self.before_run: Callable[[str | None], None] | None = None

    def fault(
        self, label: str, error_code: str, *, when: str = "before", times: int = 1, skip: int = 0
    ) -> None:
        self.faults.append(Fault(label, error_code, when, times, skip))

    def labels(self) -> list[str | None]:
        return [call.label for call in self.calls]

    def _next_fault(self, label: str | None) -> Fault | None:
        for fault in self.faults:
            if fault.label == label and fault.times > 0:
                if fault.skip > 0:
                    fault.skip -= 1
                    continue
                fault.times -= 1
                return fault
        return None

    def __call__(
        self,
        code: str,
        thread: str,
        timeout_s: float | None = None,
        label: str | None = None,
        *,
        run_dir: Path | None = None,
        handle: str | None = None,
    ) -> BridgeResult:
        ast.parse(code)  # the bridge parses before it schedules
        records = self.journal.read_all().records
        sent = handle if FEATURE_CLIENT_HANDLE in self.bridge.features else None
        self.calls.append(
            ExecCall(
                label=label,
                code=code,
                timeout_s=timeout_s,
                journal_types=[str(r.get("type")) for r in records],
                handle=sent,
            )
        )
        fault = self._next_fault(label)
        if fault is not None and fault.when == "before":
            return _error_result(fault.error_code)
        if self.before_run is not None:
            self.before_run(label)
        namespace: dict[str, Any] = {"flame": self.flame, "__name__": "__flame_mcp_exec__"}
        exec(compile(code, "<test>", "exec"), namespace)
        real = bridge_result(result=namespace["_result"])
        if sent is not None:
            self.bridge.results[sent] = real  # the bridge files the result whatever the reply
        if fault is not None:
            return _error_result(fault.error_code)
        return real


@dataclass
class Rig:
    flame: Any
    handles: dict[str, Any]
    registry: Registry
    journal: Journal
    exec: ScriptedExec
    runtime: Runtime
    bridge: FakeBridge
    exit_calls: list[int] = field(default_factory=list)

    def do(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.runtime.call(op, args)

    def records(self) -> list[dict[str, Any]]:
        return self.journal.read_all().records

    def write_labels(self) -> list[str | None]:
        return [
            label
            for label in self.exec.labels()
            if label is not None
            and (label.endswith(".set_attributes") or label.endswith(".rename"))
        ]


def make_rig(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hooks: TestHooks | None = None,
    policy: Policy | None = None,
) -> Rig:
    flame, handles = make_batch_world()
    handles["tree_reel"] = flame.find_by_wiretap_node_id(TREE_REEL_ID)
    registry = Registry()
    register_all(registry)
    journal = Journal(tmp_path / "journal")
    fake_bridge = FakeBridge()
    scripted = ScriptedExec(flame, journal, fake_bridge)
    monkeypatch.setattr(transport, "exec", scripted)
    monkeypatch.setattr(transport, "poll", fake_bridge)
    monkeypatch.setattr(transport, "probe_features", fake_bridge.probe)
    runtime = Runtime(
        registry=registry,
        policy=policy if policy is not None else Policy.from_env({}),
        journal=journal,
        run_dir=tmp_path / "run",
        hooks=hooks if hooks is not None else TestHooks(),
    )
    return Rig(flame, handles, registry, journal, scripted, runtime, fake_bridge)


TREE_REEL_ID = "/projects/p/ws/P1O/R1"  # the Library reel of fake_media_tree's default tree
BG1 = {"kind": "batch", "name": "BG1"}
COMP = {"kind": "node", "batch": BG1, "name": "comp1"}
REEL_WIRETAP = {"kind": "wiretap", "id": TREE_REEL_ID}
