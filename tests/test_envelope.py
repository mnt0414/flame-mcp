"""Envelope tests: build_envelope, from_pre_bridge_error, from_bridge_result.

Also the layer-3 byte-budget degrade."""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from flame_mcp import envelope
from flame_mcp import errors as errors_mod
from flame_mcp.registry import RegisteredOperation, Registry
from flame_mcp.transport import BridgeResult, CrashInfo
from flame_mcp.types import Category, OperationSpec

SCRATCH_ROOT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "scratch" / "p14"


@pytest.fixture
def scratch_dir() -> Iterator[Path]:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    d = SCRATCH_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _make_op(*, effect: str = "read", undo_steps: object = None) -> RegisteredOperation:
    registry = Registry(symbols=frozenset())
    registry.register_category(
        Category(
            name="test",
            description="t",
            default_thread="main",
            default_pacing="none",
            allowed_thread=frozenset({"main"}),
            allowed_pacing=frozenset({"none"}),
        )
    )
    registry.register(
        OperationSpec(
            name="test.op",
            category="test",
            description="d",
            effect=effect,  # type: ignore[arg-type]
            to_python=lambda args: "_result = None",
            undo_steps=undo_steps,  # type: ignore[arg-type]
        )
    )
    op = registry.get("test.op")
    assert op is not None
    return op


def _br(**overrides: object) -> BridgeResult:
    base: dict[str, object] = {
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
        "duration_ms": 5,
    }
    base.update(overrides)
    return BridgeResult(**base)  # type: ignore[arg-type]


def test_build_envelope_success_shape() -> None:
    env = envelope.build_envelope(ok=True, result=42, affected=1, undo_steps=1, duration_ms=7)
    assert env["ok"] is True
    assert env["result"] == 42
    assert env["affected"] == 1
    assert env["undoSteps"] == 1
    assert env["partial"] is None
    assert env["pending"] is None
    assert env["error"] is None
    assert env["errorCode"] is None
    assert env["hint"] is None
    assert env["retryable"] is None
    assert env["context"] is None
    assert env["execution"] is None
    assert env["durationMs"] == 7
    assert env["logs"] == []
    assert env["logsNote"] is None
    assert env["crash"] is None
    assert env["degraded"] == []
    assert env["notices"] == []
    assert env["timeoutPhase"] is None


def test_build_envelope_derives_hint_and_retryable_from_error_code() -> None:
    env = envelope.build_envelope(ok=False, error="x", error_code="BRIDGE_UNAVAILABLE")
    assert env["hint"] == errors_mod.hint_for("BRIDGE_UNAVAILABLE")
    assert env["retryable"] is True


def test_build_envelope_hint_override_wins_over_generic_hint() -> None:
    env = envelope.build_envelope(
        ok=False, error="x", error_code="POLICY_DENIED", hint_override="set FLAME_MCP_READONLY=0"
    )
    assert env["hint"] == "set FLAME_MCP_READONLY=0"
    assert env["hint"] != errors_mod.hint_for("POLICY_DENIED")
    assert env["retryable"] is False  # still derived from the ErrorCode


def test_from_pre_bridge_error_unknown_operation_has_no_execution() -> None:
    env = envelope.from_pre_bridge_error(
        error="no such op", error_code="UNKNOWN_OPERATION", op=None, duration_ms=3
    )
    assert env["ok"] is False
    assert env["errorCode"] == "UNKNOWN_OPERATION"
    assert env["context"] is None
    assert env["execution"] is None


def test_from_pre_bridge_error_policy_denied_has_execution_and_hint() -> None:
    op = _make_op(effect="reversible", undo_steps=lambda args, affected: affected)
    env = envelope.from_pre_bridge_error(
        error="denied", error_code="POLICY_DENIED", op=op, duration_ms=3, hint="unset READONLY"
    )
    assert env["execution"]["op"] == "test.op"
    assert env["execution"]["effect"] == "reversible"
    assert env["hint"] == "unset READONLY"
    assert env["context"] is None


def test_from_bridge_result_bridge_lost_has_no_context_and_has_crash() -> None:
    op = _make_op()
    crash = CrashInfo(
        app_log_tail=("[warn] x",),
        shell_crash_lines=("abnormal termination",),
        inflight_path=Path("/x/y.json"),
    )
    br = _br(ok=False, error_code="BRIDGE_LOST", error="lost", crash=crash, duration_ms=99)
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is False
    assert env["errorCode"] == "BRIDGE_LOST"
    assert env["context"] is None  # never conflated with a rider that ran and got Nones
    assert env["crash"] == {
        "inflightPath": "/x/y.json",
        "shellCrashLines": ["abnormal termination"],
        "appLogTail": ["[warn] x"],
    }
    assert env["durationMs"] == 99


def test_from_bridge_result_pending_is_ok_true_with_handle() -> None:
    op = _make_op()
    br = _br(ok=True, result=None, pending_handle="h123")
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is True
    assert env["pending"] == {"handle": "h123"}
    assert env["context"] is None


def test_from_bridge_result_malformed_wrapper_result_is_flame_api_error() -> None:
    op = _make_op()
    br = _br(ok=True, result="not-a-dict")
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is False
    assert env["errorCode"] == "FLAME_API_ERROR"


def test_from_bridge_result_op_error_maps_to_flame_api_error() -> None:
    op = _make_op(effect="reversible", undo_steps=lambda args, affected: affected)
    raw = {
        "value": None,
        "affected": 0,
        "opError": "ValueError: boom",
        "opTraceback": "Traceback...\nValueError: boom",
        "verified": None,
        "verifyDetail": None,
        "context": {"project": "P", "workspace": "W", "selected": 0},
    }
    br = _br(ok=True, result=raw)
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is False
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert env["error"] == "ValueError: boom"
    assert env["traceback"] == raw["opTraceback"]
    assert env["context"] == raw["context"]
    assert env["affected"] == 0
    assert env["undoSteps"] == 0


def test_from_bridge_result_op_error_code_selector_empty_is_passed_through() -> None:
    op = _make_op(effect="reversible", undo_steps=lambda args, affected: affected)
    raw = {
        "value": None,
        "affected": 0,
        "opError": "no match for 'R1' at path step 1",
        "opErrorCode": "SELECTOR_EMPTY",
        "opTraceback": None,
        "verified": None,
        "verifyDetail": None,
        "context": {"project": "P", "workspace": "W", "selected": 0},
    }
    br = _br(ok=True, result=raw)
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is False
    assert env["errorCode"] == "SELECTOR_EMPTY"
    assert env["error"] == "no match for 'R1' at path step 1"


def test_from_bridge_result_unknown_op_error_code_falls_back_to_flame_api_error() -> None:
    op = _make_op(effect="reversible", undo_steps=lambda args, affected: affected)
    raw = {
        "value": None,
        "affected": 0,
        "opError": "boom",
        "opErrorCode": "NOT_A_REAL_CODE",
        "opTraceback": None,
        "verified": None,
        "verifyDetail": None,
        "context": {"project": "P", "workspace": "W", "selected": 0},
    }
    br = _br(ok=True, result=raw)
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is False
    assert env["errorCode"] == "FLAME_API_ERROR"
    assert "NOT_A_REAL_CODE" in env["error"]


def test_from_bridge_result_verified_false_maps_to_silent_noop() -> None:
    op = _make_op(effect="reversible", undo_steps=lambda args, affected: affected)
    raw = {
        "value": "ok",
        "affected": 1,
        "opError": None,
        "opTraceback": None,
        "verified": False,
        "verifyDetail": "value did not stick",
        "context": {"project": "P", "workspace": "W", "selected": 0},
    }
    br = _br(ok=True, result=raw)
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is False
    assert env["errorCode"] == "SILENT_NOOP"
    assert env["error"] == "value did not stick"
    assert env["affected"] == 1
    assert env["undoSteps"] == 1


def test_from_bridge_result_success_maps_value_and_calls_undo_steps() -> None:
    seen: dict[str, object] = {}

    def undo_steps(args: dict[str, object], affected: int) -> int:
        seen["args"] = args
        seen["affected"] = affected
        return affected * 2

    op = _make_op(effect="reversible", undo_steps=undo_steps)
    raw = {
        "value": {"renamed": True},
        "affected": 1,
        "opError": None,
        "opTraceback": None,
        "verified": True,
        "verifyDetail": None,
        "context": {"project": "P", "workspace": "W", "selected": 3},
    }
    br = _br(ok=True, result=raw, duration_ms=12, app_log=("[warn] x",), app_log_note="note")
    env = envelope.from_bridge_result(op, {"name": "new"}, br)
    assert env["ok"] is True
    assert env["result"] == {"renamed": True}
    assert env["affected"] == 1
    assert env["undoSteps"] == 2
    assert seen == {"args": {"name": "new"}, "affected": 1}
    assert env["context"] == raw["context"]
    assert env["logs"] == ["[warn] x"]
    assert env["logsNote"] == "note"
    assert env["durationMs"] == 12


def test_timeout_error_and_pending_passthrough_for_non_timeout_codes() -> None:
    br = _br(ok=False, error_code="BRIDGE_UNAVAILABLE", error="no bridge")
    error_text, pending = envelope.timeout_error_and_pending(br)
    assert error_text == "no bridge"
    assert pending is None


def test_timeout_error_and_pending_queued_phase_has_no_pending_and_fills_in_error() -> None:
    # The real bridge's queued-phase JSON has no "error" key at all.
    br = _br(ok=False, error_code="TIMEOUT", error=None, timeout_phase="queued")
    error_text, pending = envelope.timeout_error_and_pending(br)
    assert error_text is not None and "guaranteed not to have run" in error_text
    assert pending is None


def test_timeout_error_and_pending_running_phase_has_pending_when_handle_present() -> None:
    br = _br(
        ok=False, error_code="TIMEOUT", error=None, timeout_phase="running", pending_handle="h1"
    )
    error_text, pending = envelope.timeout_error_and_pending(br)
    assert error_text is not None and "may complete later" in error_text
    assert pending == {"handle": "h1"}


def test_timeout_error_and_pending_running_phase_without_handle_has_no_pending() -> None:
    br = _br(
        ok=False,
        error_code="TIMEOUT",
        error=None,
        timeout_phase="running",
        pending_handle=None,
    )
    error_text, pending = envelope.timeout_error_and_pending(br)
    assert error_text is not None
    assert pending is None


def test_timeout_error_and_pending_client_side_timeout_keeps_its_own_error() -> None:
    # transport's "bridge alive but no reply" TIMEOUT: no phase, but it
    # already has its own message -- must not be overwritten.
    br = _br(ok=False, error_code="TIMEOUT", error="bridge alive but no reply", timeout_phase=None)
    error_text, pending = envelope.timeout_error_and_pending(br)
    assert error_text == "bridge alive but no reply"
    assert pending is None


def test_from_bridge_result_timeout_queued_phase_sets_timeout_phase_no_pending() -> None:
    op = _make_op()
    br = _br(ok=False, error_code="TIMEOUT", error=None, timeout_phase="queued")
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is False
    assert env["errorCode"] == "TIMEOUT"
    assert env["timeoutPhase"] == "queued"
    assert env["pending"] is None
    assert env["error"] is not None and "guaranteed not to have run" in env["error"]
    assert env["context"] is None


def test_from_bridge_result_timeout_running_phase_sets_pending_handle() -> None:
    op = _make_op()
    br = _br(
        ok=False, error_code="TIMEOUT", error=None, timeout_phase="running", pending_handle="h9"
    )
    env = envelope.from_bridge_result(op, {}, br)
    assert env["ok"] is False
    assert env["errorCode"] == "TIMEOUT"
    assert env["timeoutPhase"] == "running"
    assert env["pending"] == {"handle": "h9"}
    assert env["error"] is not None and "may complete later" in env["error"]


# Layer 3 of degrade_for_size is the byte budget.


def test_degrade_below_budget_is_untouched() -> None:
    env = envelope.build_envelope(ok=True, result="small", duration_ms=1)
    out = envelope.degrade_for_size(env, max_bytes=envelope.MAX_RESPONSE_BYTES)
    assert out == env


def test_degrade_stage1_truncates_traceback_and_logs() -> None:
    env = envelope.build_envelope(
        ok=False,
        error="boom",
        error_code="FLAME_API_ERROR",
        traceback="\n".join(f"tb line {i:03d} filler filler filler" for i in range(100)),
        logs=[f"log line {i:03d} filler filler filler" for i in range(100)],
        duration_ms=1,
    )
    full_size = len(json.dumps(env, default=str).encode("utf-8"))
    out = envelope.degrade_for_size(env, max_bytes=full_size - 200)
    assert any("traceback truncated" in note for note in out["degraded"])
    assert any("logs truncated" in note for note in out["degraded"])
    assert len(out["traceback"].splitlines()) == envelope.TRACEBACK_TAIL_LINES
    assert len(out["logs"]) == envelope.LOGS_TAIL_LINES


def test_degrade_stage2_collapses_top_level_result_list() -> None:
    env = envelope.build_envelope(
        ok=True, result=[{"i": i, "pad": "x" * 50} for i in range(500)], duration_ms=1
    )
    out = envelope.degrade_for_size(env, max_bytes=450)
    assert any("collapsed" in note for note in out["degraded"])
    assert out["result"] == {"count": 500, "omitted": True}


def test_degrade_stage2_collapses_one_level_down_lists_in_a_dict_result() -> None:
    env = envelope.build_envelope(
        ok=True,
        result={
            "categories": ["a", "b"],
            "operations": [{"name": f"op{i}"} for i in range(500)],
        },
        duration_ms=1,
    )
    # 450 + the `journal` key and the `notices` key
    out = envelope.degrade_for_size(env, max_bytes=500)
    assert out["result"]["operations"] == {"count": 500, "omitted": True}
    assert out["result"]["categories"] == {"count": 2, "omitted": True}


def test_degrade_stage3_spills_full_envelope_to_disk(scratch_dir: Path) -> None:
    env = envelope.build_envelope(ok=True, result="x" * 5000, duration_ms=1)
    out = envelope.degrade_for_size(env, max_bytes=200, spill_dir=scratch_dir)
    assert any("spilled" in note for note in out["degraded"])
    assert isinstance(out["result"], dict)
    spill_path = Path(out["result"]["spilledTo"])
    assert spill_path.exists()
    assert spill_path.parent == scratch_dir
    written = json.loads(spill_path.read_text())
    assert written["result"] == "x" * 5000
    assert written["ok"] is True


def test_degrade_preserves_preexisting_degraded_notes_before_its_own(scratch_dir: Path) -> None:
    # A note already in `degraded` when degrade_for_size is
    # called (e.g. flame_do's limit-clamp note, added before the byte-budget
    # pass ever runs) must stay first, including in a spilled file.
    env = envelope.build_envelope(ok=True, result="x" * 5000, duration_ms=1)
    env["degraded"] = ["requested limit 999999 clamped to max_limit=2000"]
    out = envelope.degrade_for_size(env, max_bytes=200, spill_dir=scratch_dir)
    assert out["degraded"][0] == "requested limit 999999 clamped to max_limit=2000"
    assert any("spilled" in note for note in out["degraded"][1:])
    spill_path = Path(out["result"]["spilledTo"])
    written = json.loads(spill_path.read_text())
    assert written["degraded"][0] == "requested limit 999999 clamped to max_limit=2000"


def test_spill_file_keeps_every_list_item_of_the_result(scratch_dir: Path) -> None:
    clips = [{"name": f"c{i}"} for i in range(50)]
    env = envelope.build_envelope(
        ok=True, result={"clips": clips, "note": "y" * 5000}, duration_ms=1
    )
    out = envelope.degrade_for_size(env, max_bytes=2000, spill_dir=scratch_dir)
    assert out["result"].keys() == {"spilledTo"}
    written = json.loads(Path(out["result"]["spilledTo"]).read_text())
    assert written["result"]["clips"] == clips


def test_spill_file_keeps_the_untruncated_traceback_and_logs(scratch_dir: Path) -> None:
    tb = "\n".join(f"tb line {i:03d}" for i in range(100))
    logs = [f"log line {i:03d}" for i in range(100)]
    env = envelope.build_envelope(
        ok=False,
        result="z" * 5000,
        error="boom",
        error_code="FLAME_API_ERROR",
        traceback=tb,
        logs=logs,
        duration_ms=1,
    )
    out = envelope.degrade_for_size(env, max_bytes=2000, spill_dir=scratch_dir)
    assert len(out["traceback"].splitlines()) == envelope.TRACEBACK_TAIL_LINES
    written = json.loads(Path(out["result"]["spilledTo"]).read_text())
    assert written["traceback"] == tb
    assert written["logs"] == logs
    assert written["result"] == "z" * 5000


def _raw_with(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "value": None,
        "affected": 0,
        "partial": None,
        "opError": None,
        "opErrorCode": None,
        "opTraceback": None,
        "verified": None,
        "verifyDetail": None,
        "context": {"project": "P", "workspace": "W", "selected": 0},
    }
    raw.update(overrides)
    return raw


def test_from_bridge_result_op_error_keeps_the_result_and_partial() -> None:
    op = _make_op(effect="reversible", undo_steps=lambda args, affected: 0)
    raw = _raw_with(
        value={"applied": ["a"], "failed": {"b": "silent_noop: ..."}},
        affected=1,
        partial={"applied": 1, "failed": 1},
        opError="set_attributes: applied 1, failed 1",
        opErrorCode="SILENT_NOOP",
    )
    env = envelope.from_bridge_result(op, {}, _br(ok=True, result=raw))
    assert env["ok"] is False
    assert env["errorCode"] == "SILENT_NOOP"
    assert env["result"] == {"applied": ["a"], "failed": {"b": "silent_noop: ..."}}
    assert env["partial"] == {"applied": 1, "failed": 1}
    assert env["affected"] == 1
    assert env["undoSteps"] == 0


def test_from_bridge_result_success_passes_partial_through() -> None:
    op = _make_op(effect="reversible", undo_steps=lambda args, affected: 0)
    raw = _raw_with(value="ok", partial={"applied": 3, "skipped": 1})
    env = envelope.from_bridge_result(op, {}, _br(ok=True, result=raw))
    assert env["ok"] is True
    assert env["partial"] == {"applied": 3, "skipped": 1}


@pytest.mark.parametrize(
    "garbage", [None, "x", [1], {}, {"a": "b"}, {"a": 1.5}, {"a": True}, {1: 2}, {"a": 1, "b": "c"}]
)
def test_from_bridge_result_drops_a_malformed_partial(garbage: object) -> None:
    op = _make_op()
    env = envelope.from_bridge_result(op, {}, _br(ok=True, result=_raw_with(partial=garbage)))
    assert env["ok"] is True
    assert env["partial"] is None


def test_from_bridge_result_op_error_without_a_value_keeps_result_none() -> None:
    op = _make_op(effect="reversible", undo_steps=lambda args, affected: 0)
    raw = _raw_with(opError="boom", opErrorCode="SELECTOR_EMPTY")
    env = envelope.from_bridge_result(op, {}, _br(ok=True, result=raw))
    assert env["result"] is None
    assert env["partial"] is None


MEDIA_PANEL_LINE = (
    "2026-10-12 17:00:01 ERROR The Media Panel needs to be available to modify it's objects "
    "selection."
)


def test_build_envelope_adds_a_notice_when_the_logs_say_the_media_panel_is_unavailable() -> None:
    env = envelope.build_envelope(ok=True, logs=["something else", MEDIA_PANEL_LINE])
    assert env["notices"] == [envelope.MEDIA_PANEL_NOTICE]
    assert "modal" in envelope.MEDIA_PANEL_NOTICE and "silently" in envelope.MEDIA_PANEL_NOTICE


def test_the_notice_is_given_once_however_many_log_lines_say_it() -> None:
    env = envelope.build_envelope(ok=True, logs=[MEDIA_PANEL_LINE, MEDIA_PANEL_LINE])
    assert env["notices"] == [envelope.MEDIA_PANEL_NOTICE]


def test_logs_without_the_line_leave_notices_empty() -> None:
    env = envelope.build_envelope(ok=True, logs=["Direct I/O disabled", "Loading setup"])
    assert env["notices"] == []


def test_the_notice_is_on_failures_and_on_bridge_results_too() -> None:
    failed = envelope.build_envelope(
        ok=False, error="x", error_code="SILENT_NOOP", logs=[MEDIA_PANEL_LINE]
    )
    assert failed["notices"] == [envelope.MEDIA_PANEL_NOTICE]
    op = _make_op()
    raw = {"value": 1, "affected": 0, "opError": None, "opTraceback": None, "context": None}
    env = envelope.from_bridge_result(op, {}, _br(ok=True, result=raw, app_log=(MEDIA_PANEL_LINE,)))
    assert env["ok"] is True and env["notices"] == [envelope.MEDIA_PANEL_NOTICE]
    lost = envelope.from_bridge_result(
        op, {}, _br(ok=False, error_code="TIMEOUT", app_log=(MEDIA_PANEL_LINE,))
    )
    assert lost["notices"] == [envelope.MEDIA_PANEL_NOTICE]


def test_the_notice_survives_the_size_degrade_of_the_logs() -> None:
    logs = [f"line {n}" for n in range(300)] + [MEDIA_PANEL_LINE] + ["x" * 400] * 300
    env = envelope.build_envelope(ok=True, logs=logs)
    degraded = envelope.degrade_for_size(env, max_bytes=2000)
    assert degraded["notices"] == [envelope.MEDIA_PANEL_NOTICE]


def test_silent_noop_hint_says_to_look_for_an_open_modal() -> None:
    hint = errors_mod.hint_for("SILENT_NOOP")
    assert "modal" in hint and "Flame" in hint
