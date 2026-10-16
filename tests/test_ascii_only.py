"""Flame is sent ASCII only: refused at the entrance, at the send point and inside the bridge.

A 2-byte character that reaches Flame can crash it, so nothing non-ASCII is allowed to travel:
`opschema.validate_op_args` refuses the arguments (INVALID_ARGS), `transport.exec` refuses code
that is not ASCII without connecting, and the bridge's `_handle_exec` refuses it before `compile`.
The files that run inside Flame (`src/`, `stub/`) are ASCII themselves.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from flame_mcp import checkpointed, execution, server, transport
from flame_mcp.__main__ import build_server
from flame_mcp.checkpointed import Judgement, PreRunRefusal, run_checkpointed
from flame_mcp.ops import register_all
from flame_mcp.opschema import ArgIssue, validate_op_args
from flame_mcp.policy import Policy
from flame_mcp.registry import Registry
from flame_mcp.types import OperationParam
from flame_mcp_bridge import impl
from journal_test_helpers import BG1, COMP, Rig, make_rig
from test_bridge_impl import (
    FakeFlame,
    FakeMainLoop,
    FakeTimer,
    _sock,
    scratch_dir,  # noqa: F401  (fixture shared with test_bridge_impl)
    send_request,
)
from test_ops_render import add_writer
from test_opschema import make_op

REAL_EXEC = transport.exec  # taken at import: the tests patch `transport.exec` per test
TE = "テ"  # a katakana character: 3 bytes in UTF-8
REPO = Path(__file__).resolve().parent.parent


def string_param(name: str) -> OperationParam:
    return OperationParam(name=name, type="string", description="d", required=False)


def object_param(name: str) -> OperationParam:
    return OperationParam(name=name, type="object", description="d", required=False)


def array_param(name: str) -> OperationParam:
    return OperationParam(name=name, type="array", description="d", required=False)


def selector_param(name: str) -> OperationParam:
    return OperationParam(name=name, type="selector", description="d", required=True)


def issues_of(op: Any, raw: object) -> tuple[ArgIssue, ...]:
    result = validate_op_args(op, raw)
    assert result.ok is False
    return result.issues


def assert_refusal_is_ascii(issues: tuple[ArgIssue, ...]) -> None:
    for issue in issues:
        assert issue.path.isascii() and issue.message.isascii(), issue
        assert TE not in issue.path and TE not in issue.message


@pytest.fixture
def loop() -> FakeMainLoop:
    return FakeMainLoop()


@pytest.fixture
def bridge(scratch_dir: Path, loop: FakeMainLoop) -> Iterator[impl.Bridge]:  # noqa: F811
    run_dir = scratch_dir / "run"
    run_dir.mkdir()
    started = impl.Bridge(
        schedule=loop.schedule,
        flame_module=FakeFlame(),
        version="2025.2.4",
        project_name="proj",
        run_dir=run_dir,
        log_path=scratch_dir / "bridge.log",
        make_timer=lambda: FakeTimer(loop),
    )
    started.start()
    yield started
    started.stop()


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_rig(monkeypatch, tmp_path)


# --- 1. the entrance: opschema.validate_op_args -------------------------------------------


def test_a_non_ascii_string_argument_is_refused_with_its_path_and_code_point() -> None:
    op = make_op(string_param("name"))
    issues = issues_of(op, {"name": f"a{TE}b"})
    assert len(issues) == 1
    assert issues[0].path == "args.name"
    assert "U+30C6" in issues[0].message
    assert "ASCII" in issues[0].message
    assert_refusal_is_ascii(issues)


def test_the_advice_is_only_to_use_ascii_characters() -> None:
    op = make_op(string_param("name"))
    message = issues_of(op, {"name": TE})[0].message
    assert message.endswith("Flame takes ASCII only. Use ASCII characters.")
    assert "uid" not in message and "wiretap" not in message


def test_a_non_ascii_character_nested_in_an_object_and_a_list_is_found_with_its_path() -> None:
    op = make_op(object_param("values"), array_param("items"))
    nested = issues_of(op, {"values": {"a": {"b": [1, f"x{TE}"]}}})
    assert nested[0].path == "args.values.a.b[1]"
    assert "U+30C6" in nested[0].message
    in_list = issues_of(op, {"items": ["ok", {"k": TE}]})
    assert in_list[0].path == "args.items[1].k"
    assert_refusal_is_ascii(nested + in_list)


def test_a_non_ascii_selector_path_is_found_with_its_path() -> None:
    op = make_op(selector_param("target"), accepts=("path",))
    raw = {"target": {"kind": "path", "path": ["Library", f"{TE}reel"]}}
    issues = issues_of(op, raw)
    assert issues[0].path == "args.target.path[1]"
    assert "U+30C6" in issues[0].message
    assert_refusal_is_ascii(issues)


def test_a_non_ascii_dict_key_is_refused_without_echoing_it() -> None:
    op = make_op(object_param("values"))
    issues = issues_of(op, {"values": {f"k{TE}": 1}})
    assert issues[0].path == "args.values"
    assert "key" in issues[0].message
    assert "U+30C6" in issues[0].message
    assert_refusal_is_ascii(issues)


def test_a_non_ascii_top_level_key_is_refused_before_the_unknown_parameter_check() -> None:
    op = make_op(string_param("name"))
    issues = issues_of(op, {f"nam{TE}": "x"})
    assert len(issues) == 1 and "U+30C6" in issues[0].message
    assert "unknown parameter" not in issues[0].message
    assert_refusal_is_ascii(issues)


def test_the_first_non_ascii_character_is_the_one_reported() -> None:
    op = make_op(string_param("name"))
    issues = issues_of(op, {"name": "aébテ"})
    assert "U+00E9" in issues[0].message


def test_ascii_arguments_still_pass() -> None:
    op = make_op(string_param("name"), object_param("values"))
    result = validate_op_args(op, {"name": "plain name 1", "values": {"k": ["v", 1, None]}})
    assert result.ok is True


def test_the_ascii_check_covers_args_that_are_not_declared_as_strings() -> None:
    op = make_op(OperationParam(name="any_value", type="any", description="d", required=False))
    assert issues_of(op, {"any_value": {"x": [TE]}})[0].path == "args.any_value.x[0]"


def test_runtime_refuses_a_non_ascii_name_and_sends_nothing(rig: Rig) -> None:
    env = rig.do("node.set_attributes", {"target": COMP, "values": {"blend_type": f"Gimp{TE}"}})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "args.values.blend_type" in env["error"] and "U+30C6" in env["error"]
    assert env["error"].isascii()
    assert rig.exec.calls == []
    assert rig.records() == []


def test_a_read_op_that_names_by_a_non_ascii_selector_is_refused_too(rig: Rig) -> None:
    target = {"kind": "node", "batch": BG1, "name": f"{TE}comp"}
    env = rig.do("node.get_attributes", {"target": target})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "args.target.name" in env["error"]
    assert rig.exec.calls == []


def test_eval_code_with_a_non_ascii_character_is_refused_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    enabled = make_rig(
        monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_ENABLE_EVAL": "1"})
    )
    env = enabled.do("eval.run", {"code": f"_result = '{TE}'", "effect": "read"})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "args.code" in env["error"] and env["error"].isascii()
    assert enabled.exec.calls == []


def test_a_plan_with_a_non_ascii_step_argument_is_refused_whole(rig: Rig) -> None:
    steps = [
        {"op": "node.get_attributes", "args": {"target": COMP}},
        {"op": "node.get_attributes", "args": {"target": COMP}},
        {
            "op": "node.set_attributes",
            "args": {"target": COMP, "values": {"blend_type": TE}},
        },
    ]
    env = rig.do("plan.run", {"steps": steps})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert "steps[2].args.values.blend_type" in env["error"]
    assert "U+30C6" in env["error"] and env["error"].isascii()
    assert rig.exec.calls == []
    assert rig.records() == []


# --- 2. the send point: transport.exec ----------------------------------------------------


def test_exec_refuses_non_ascii_code_without_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the bridge must not be contacted for non-ASCII code")

    monkeypatch.setattr(transport, "_discover", forbidden)
    monkeypatch.setattr(transport, "_send_request", forbidden)
    monkeypatch.setattr(transport, "_write_inflight", forbidden)
    result = transport.exec(f"_result = '{TE}'", thread="main", run_dir=tmp_path)
    assert result.ok is False
    assert result.error_code == "INVALID_ARGS"
    assert result.error is not None and result.error.isascii()
    assert "U+30C6" in result.error
    assert result.crash is None and result.pending_handle is None
    assert result.inflight_path is None
    assert not (tmp_path / "inflight").exists()


def test_exec_still_contacts_the_bridge_for_ascii_code(tmp_path: Path) -> None:
    result = transport.exec("_result = 1", thread="main", run_dir=tmp_path)
    assert result.error_code in ("BRIDGE_UNAVAILABLE", "BRIDGE_LOST")


def test_a_send_point_refusal_is_a_plain_answer_in_the_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = Registry()
    register_all(registry)
    op = registry.get("node.get_attributes")
    assert op is not None
    monkeypatch.setattr(execution, "build_exec_code", lambda _op, _args: f"_result = '{TE}'")
    env = execution.run_op(op, {"target": COMP}, run_dir=tmp_path)
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert env["error"].isascii()
    assert env.get("pending") is None


def test_a_journaled_write_refused_at_the_send_point_is_an_outcome_not_a_doubt(rig: Rig) -> None:
    rig.exec.fault("node.set_attributes", "INVALID_ARGS", when="before")
    env = rig.do("node.set_attributes", {"target": COMP, "values": {"blend_type": "Gimp"}})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    types = [r["type"] for r in rig.records()]
    assert types == ["intent", "outcome"]
    assert rig.records()[1]["ok"] is False
    assert rig.runtime.guard.denial() is None
    again = rig.do("node.set_attributes", {"target": COMP, "values": {"blend_type": "Gimp"}})
    assert again["ok"] is True  # the door is open: the next write is sent


def test_the_real_send_point_refusal_of_a_journaled_write_leaves_the_door_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_exec = transport.exec
    rig = make_rig(monkeypatch, tmp_path)
    seen: list[str] = []

    def exec_with_non_ascii_write(code: str, thread: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("label") == "node.set_attributes":
            seen.append("write")
            return real_exec(code + f"\n# {TE}", thread, *args, **kwargs)
        return rig.exec(code, thread, *args, **kwargs)

    monkeypatch.setattr(transport, "exec", exec_with_non_ascii_write)
    env = rig.do("node.set_attributes", {"target": COMP, "values": {"blend_type": "Gimp"}})
    assert seen == ["write"]
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    assert [r["type"] for r in rig.records()] == ["intent", "outcome"]
    assert rig.runtime.guard.denial() is None


# --- 3. inside Flame: the bridge ----------------------------------------------------------


def test_the_bridge_refuses_non_ascii_code_before_compile_and_runs_nothing(
    bridge: impl.Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled: list[str] = []
    real_compile = compile

    def spy_compile(source: Any, *args: Any, **kwargs: Any) -> Any:
        compiled.append(str(source))
        return real_compile(source, *args, **kwargs)

    monkeypatch.setitem(impl.__dict__, "compile", spy_compile)
    request = {
        "v": 1,
        "id": "na1",
        "kind": "exec",
        "code": f"_result = '{TE}'",
        "thread": "main",
        "timeoutS": 5,
    }
    resp = send_request(_sock(bridge), request)
    assert resp["ok"] is False
    assert isinstance(resp["protocolError"], str) and resp["protocolError"].isascii()
    assert "ASCII" in resp["protocolError"]
    assert compiled == []


def test_the_bridge_still_runs_ascii_code(bridge: impl.Bridge, loop: FakeMainLoop) -> None:
    result: dict[str, Any] = {}

    def client() -> None:
        result["resp"] = send_request(
            _sock(bridge),
            {"v": 1, "id": "a1", "kind": "exec", "code": "_result = 7", "thread": "main"},
            timeout=5,
        )

    t = threading.Thread(target=client)
    t.start()
    loop.run_until(lambda: "resp" in result, timeout=5)
    t.join(5)
    assert result["resp"]["ok"] is True and result["resp"]["result"] == 7


def test_the_hello_features_are_unchanged(bridge: impl.Bridge) -> None:
    hello = send_request(_sock(bridge), {"v": 1, "id": "h", "kind": "hello"})
    assert hello["features"] == list(impl.BRIDGE_FEATURES)


# --- 4. the files that run inside Flame, and what is sent ---------------------------------


def non_ascii_lines(path: Path) -> list[int]:
    return [
        number
        for number, line in enumerate(path.read_bytes().splitlines(), start=1)
        if not line.isascii()
    ]


@pytest.mark.parametrize("folder", ["src", "stub"])
def test_every_python_file_that_ships_into_or_to_flame_is_ascii(folder: str) -> None:
    offenders = {
        str(path.relative_to(REPO)): non_ascii_lines(path)
        for path in sorted((REPO / folder).rglob("*.py"))
        if non_ascii_lines(path)
    }
    assert offenders == {}


def test_the_code_every_registered_op_sends_is_ascii() -> None:
    registry = Registry()
    register_all(registry)
    checked = 0
    offenders: list[str] = []
    for op in registry.list_ops():
        if op.to_python is None:
            continue
        code = execution.build_exec_code(op, dict(op.sample_args))
        checked += 1
        if not code.isascii():
            offenders.append(op.name)
    assert checked > 50
    assert offenders == []


def test_the_flame_context_code_is_ascii() -> None:
    assert server._CONTEXT_CODE.isascii()


def test_the_pitfall_and_the_docs_say_crash_and_give_no_uid_advice() -> None:
    pitfalls = [p["text"] for p in server._static_context(Registry())["pitfalls"]]
    (text,) = [t for t in pitfalls if "2-byte" in t]
    assert "crash" in text and "ASCII" in text
    assert "uid" not in text and "wiretap" not in text
    safety = (REPO / "docs" / "safety.md").read_text(encoding="utf-8")
    section = safety.split("## ASCII only", 1)[1].split("\n## ", 1)[0]
    assert "crash" in section and "uid" not in section and "wiretap" not in section


def test_the_server_instructions_tell_the_ai_about_ascii() -> None:
    assert "ASCII" in server.INSTRUCTIONS
    assert json.dumps(server.INSTRUCTIONS).count("INVALID_ARGS") >= 1


# --- review fixes -------------------------------------------------------------------------


def build_with_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str
) -> pytest.ExceptionInfo[SystemExit]:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as exc_info:
        build_server(run_dir=tmp_path / "run")
    return exc_info


def test_startup_refuses_a_non_ascii_journal_directory_and_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    info = build_with_env(monkeypatch, tmp_path, FLAME_MCP_JOURNAL_DIR=str(tmp_path / f"j{TE}"))
    assert info.value.code == 1
    err = capsys.readouterr().err
    assert "FLAME_MCP_JOURNAL_DIR" in err and "U+30C6" in err and "ASCII" in err
    assert err.isascii()


def test_startup_refuses_a_non_ascii_default_journal_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FLAME_MCP_JOURNAL_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / f"h{TE}"))
    with pytest.raises(SystemExit) as info:
        build_server(run_dir=tmp_path / "run")
    assert info.value.code == 1
    err = capsys.readouterr().err
    assert "journal directory" in err and "FLAME_MCP_JOURNAL_DIR" in err and "U+30C6" in err
    assert err.isascii()


def test_startup_refuses_a_non_ascii_checkpoint_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FLAME_MCP_JOURNAL_DIR", str(tmp_path / "journal"))
    info = build_with_env(monkeypatch, tmp_path, FLAME_MCP_CHECKPOINT_LIBRARY=f"lib{TE}")
    assert info.value.code == 1
    err = capsys.readouterr().err
    assert "FLAME_MCP_CHECKPOINT_LIBRARY" in err and "U+30C6" in err
    assert err.isascii()


def test_startup_accepts_an_ascii_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLAME_MCP_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("FLAME_MCP_CHECKPOINT_LIBRARY", "my_checkpoints")
    assert build_server(run_dir=tmp_path / "run") is not None


def test_the_send_point_message_names_the_op_not_a_line_and_the_hint_fits(
    tmp_path: Path,
) -> None:
    result = REAL_EXEC(
        f"_result = '{TE}'", thread="main", label="batch.load_setup", run_dir=tmp_path
    )
    assert result.error is not None and result.error.isascii()
    assert "'batch.load_setup'" in result.error and "line" not in result.error
    assert "U+30C6" in result.error
    assert result.hint is not None and result.hint.isascii()
    assert "did not pass validation" not in result.hint
    assert "Flame" in result.hint and "setting" in result.hint


def test_the_envelope_of_a_send_point_refusal_carries_that_hint(tmp_path: Path) -> None:
    registry = Registry()
    register_all(registry)
    op = registry.get("node.get_attributes")
    assert op is not None
    from flame_mcp import envelope as envelope_mod

    br = REAL_EXEC(f"_result = '{TE}'", thread="main", label=op.name, run_dir=tmp_path)
    env = envelope_mod.from_bridge_result(op, {}, br)
    assert env["errorCode"] == "INVALID_ARGS"
    assert env["hint"] == br.hint
    assert "did not pass validation" not in env["hint"]


def refuse_at_real_send_point(
    monkeypatch: pytest.MonkeyPatch, rig: Rig, matches: Callable[[str | None, str], bool]
) -> None:
    """Route the calls `matches(label, code)` picks through the real `transport.exec` with a
    non-ASCII character in the code; every other call still goes to the scripted fake."""
    scripted = transport.exec

    def routed(code: str, thread: Any, *args: Any, **kwargs: Any) -> Any:
        if matches(kwargs.get("label"), code):
            return REAL_EXEC(code + f"\n# {TE}\n", thread, *args, **kwargs)
        return scripted(code, thread, *args, **kwargs)

    monkeypatch.setattr(transport, "exec", routed)


def assert_refused_as_an_outcome(rig: Rig, env: dict[str, Any]) -> None:
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    types = [r["type"] for r in rig.records() if r["type"] in ("intent", "outcome", "unresolved")]
    assert types == ["intent", "outcome"], types
    outcome = next(r for r in rig.records() if r["type"] == "outcome")
    assert outcome["ok"] is False
    assert rig.runtime.guard.denial() is None


def test_a_checkpointed_write_refused_at_the_send_point_is_an_outcome_and_the_door_stays_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    saved = rig.do("batch.checkpoint", {"target": BG1})["result"]
    refuse_at_real_send_point(monkeypatch, rig, lambda label, code: label == "batch.load_setup")
    env = rig.do("batch.load_setup", {"target": BG1, "path": saved["path"]})
    types = [r["type"] for r in rig.records() if r["type"] in ("intent", "outcome", "unresolved")]
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert types == ["intent", "outcome"]
    assert rig.runtime.guard.denial() is None
    assert "emptied or replaced" not in env["error"]
    assert "nothing was sent" in env["error"]


def test_a_desktop_write_refused_at_the_send_point_is_an_outcome_and_the_door_stays_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    refuse_at_real_send_point(monkeypatch, rig, lambda label, code: "_result = _dc_create(" in code)
    env = rig.do("desktop.create", {"kind": "batch_group", "name": "X1"})
    assert_refused_as_an_outcome(rig, env)


def test_a_render_refused_at_the_send_point_is_an_outcome_and_the_door_stays_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    add_writer(rig, tmp_path, last=3)
    refuse_at_real_send_point(monkeypatch, rig, lambda label, code: label == "render.batch")
    env = rig.do("render.batch", {"target": BG1})
    assert_refused_as_an_outcome(rig, env)


def test_run_checkpointed_refuses_a_non_ascii_stable_address_before_the_intent(
    monkeypatch: pytest.MonkeyPatch, rig: Rig
) -> None:
    pre = {"checkpoint": "c_1", "fingerprint": "f", "batch": {"uid": f"uid-{TE}", "name": "BG1"}}
    monkeypatch.setattr(
        checkpointed, "take_checkpoint", lambda *a, **k: {"ok": True, "result": pre}
    )
    ran: list[Any] = []

    def run(stable: dict[str, Any]) -> dict[str, Any]:
        ran.append(stable)
        return {}

    with pytest.raises(PreRunRefusal) as info:
        run_checkpointed(
            rig.runtime,
            selector=BG1,
            op_name="batch.load_setup",
            kind="load",
            args={},
            intent_extra={},
            expected_fp=None,
            label="t",
            run=run,
            judge=lambda env, fp: Judgement(ok=True),
            failed_key="t",
        )
    assert info.value.stage == "ascii"
    assert info.value.issue is not None and "U+30C6" in info.value.issue.message
    assert ran == [] and rig.records() == []


def test_run_checkpointed_refuses_non_ascii_args_before_taking_a_checkpoint(rig: Rig) -> None:
    ran: list[Any] = []

    def run(stable: dict[str, Any]) -> dict[str, Any]:
        ran.append(stable)
        return {}

    with pytest.raises(PreRunRefusal) as info:
        run_checkpointed(
            rig.runtime,
            selector=BG1,
            op_name="batch.load_setup",
            kind="load",
            args={"path": f"/x/{TE}"},
            intent_extra={},
            expected_fp=None,
            label="t",
            run=run,
            judge=lambda env, fp: Judgement(ok=True),
            failed_key="t",
        )
    assert info.value.stage == "ascii"
    assert ran == [] and rig.records() == [] and rig.exec.calls == []
