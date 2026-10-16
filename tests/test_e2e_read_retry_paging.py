"""The read-side robustness of the real-Flame e2e scripts, against a scripted MCP session (no
Flame): library.list is paged until the run's own library turns up, and a read that answers
TIMEOUT is read once more (and the first read is recorded as an observation)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str) -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)


p1 = load("p1_mcp_e2e")
p21 = load("p2_1_attr_e2e")
p22a = load("p2_2a_journal_e2e")


class ScriptedSession:
    """`call_tool` answers from `handler(op, args)` -> envelope; every call is kept."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool: str, payload: dict[str, Any]) -> Any:
        op = payload["op"]
        args = payload.get("args") or {}
        self.calls.append((op, args))
        return SimpleNamespace(structured_content=self.handler(op, args))


def page_of(items: list[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
    offset, limit = args.get("offset", 0), args["limit"]
    shown = items[offset : offset + limit]
    return {
        "ok": True,
        "result": {
            "items": shown,
            "total": len(items),
            "shown": len(shown),
            "truncated": offset + len(shown) < len(items),
        },
    }


TIMEOUT_ENV = {"ok": False, "errorCode": "TIMEOUT", "timeoutPhase": "running", "result": None}


def run_libraries(libs: list[str], wanted: str) -> tuple[dict[str, Any], ScriptedSession]:
    items = [{"name": n} for n in libs]
    session = ScriptedSession(lambda op, args: page_of(items, args))
    results: dict[str, Any] = {"steps": [], "ok": True}
    anyio.run(p1.step_c_list_libraries, session, wanted, results)
    return results, session


def step(results: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in results["steps"] if s["name"] == name)


def test_the_library_made_by_this_run_is_found_beyond_page_one() -> None:
    libs = [f"old{i}" for i in range(51)] + ["mine"]
    results, session = run_libraries(libs, "mine")
    found = step(results, "c_list_libraries")
    assert found["ok"] is True and found["collected"] == 52 and found["total"] == 52
    assert [c[1]["offset"] for c in session.calls] == [0, 50]


def test_a_missing_library_fails_after_every_page_was_read() -> None:
    results, session = run_libraries([f"old{i}" for i in range(120)], "mine")
    assert step(results, "c_list_libraries")["ok"] is False
    assert [c[1]["offset"] for c in session.calls] == [0, 50, 100]


def test_the_page_count_is_capped() -> None:
    libs = [f"old{i}" for i in range(50 * (p1.LIBRARY_LIST_MAX_PAGES + 5))]
    results, session = run_libraries(libs, "mine")
    assert step(results, "c_list_libraries")["ok"] is False
    assert len(session.calls) == p1.LIBRARY_LIST_MAX_PAGES


def test_a_total_that_disagrees_with_the_pages_fails() -> None:
    items = [{"name": f"l{i}"} for i in range(10)] + [{"name": "mine"}]

    def handler(op: str, args: dict[str, Any]) -> dict[str, Any]:
        env = page_of(items, args)
        env["result"]["total"] = 99
        env["result"]["truncated"] = False
        return env

    results: dict[str, Any] = {"steps": [], "ok": True}
    anyio.run(p1.step_c_list_libraries, ScriptedSession(handler), "mine", results)
    assert step(results, "c_list_libraries")["ok"] is False


def p21_ctx(handler: Any) -> Any:
    results: dict[str, Any] = {"steps": [], "ok": True}
    return p21.Ctx(ScriptedSession(handler), Path("."), results, "261001_000000")


def batch_items(n: int) -> list[dict[str, Any]]:
    return [{"name": f"g{i}", "uid": f"u{i}", "isCurrent": i == 3} for i in range(n)]


def test_p21_batch_states_pages_and_retries_a_first_timeout() -> None:
    items = batch_items(250)
    seen: list[tuple[str, int]] = []

    def handler(op: str, args: dict[str, Any]) -> dict[str, Any]:
        seen.append((op, args["offset"]))
        if len(seen) == 1:
            return TIMEOUT_ENV
        return page_of(items, args)

    ctx = p21_ctx(handler)
    states, meta = anyio.run(p21.batch_states, ctx, "before")
    assert len(states) == meta["collected"] == meta["total"] == 250
    assert seen == [("batch.list", 0), ("batch.list", 0), ("batch.list", 100), ("batch.list", 200)]
    first = meta["pages"][0]
    assert first["firstTimedOut"] is True and first["retryMs"] is not None
    assert all(p["firstTimedOut"] is False for p in meta["pages"][1:])
    observation = step(ctx.results, "f_observation_batch_list_reads_before")
    assert observation["kind"] == "observation" and observation["ok"] is True
    assert ctx.results["ok"] is True


def test_p21_batch_states_gives_up_when_the_retry_times_out_too() -> None:
    ctx = p21_ctx(lambda op, args: TIMEOUT_ENV)
    states, meta = anyio.run(p21.batch_states, ctx, "before")
    assert states == {} and meta["collected"] == 0 and meta["total"] is None
    assert len(ctx.session.calls) == 2


def test_p22a_do_read_retries_one_timeout_and_records_the_first_read() -> None:
    answers = [TIMEOUT_ENV, {"ok": True, "result": {"total": 1}}]
    session = ScriptedSession(lambda op, args: answers.pop(0))
    results: dict[str, Any] = {"steps": [], "ok": True}
    ctx = p22a.Ctx(Path("."), results, "261001_000000", Path("."))
    env = anyio.run(p22a.do_read, session, ctx, "batch.list", {"limit": 5})
    assert env["ok"] is True and len(session.calls) == 2
    obs = step(results, "observation_read_retried_after_timeout_batch_list")
    assert obs["kind"] == "observation" and obs["firstErrorCode"] == "TIMEOUT"
    assert obs["retryOk"] is True and results["ok"] is True


def test_p22a_do_read_does_not_retry_other_failures_or_successes() -> None:
    for env in ({"ok": True, "result": {}}, {"ok": False, "errorCode": "SELECTOR_EMPTY"}):
        session = ScriptedSession(lambda op, args, env=env: env)
        results: dict[str, Any] = {"steps": [], "ok": True}
        ctx = p22a.Ctx(Path("."), results, "261001_000000", Path("."))
        assert anyio.run(p22a.do_read, session, ctx, "batch.list") == env
        assert len(session.calls) == 1 and results["steps"] == []


def test_p22a_do_read_retries_a_plan_whose_child_step_timed_out() -> None:
    bad = {
        "ok": True,
        "result": {"steps": [{"op": "batch.list", "ok": False, "errorCode": "TIMEOUT"}]},
    }
    good = {"ok": True, "result": {"steps": [{"op": "batch.list", "ok": True}]}}
    answers = [bad, good]
    session = ScriptedSession(lambda op, args: answers.pop(0))
    results: dict[str, Any] = {"steps": [], "ok": True}
    ctx = p22a.Ctx(Path("."), results, "261001_000000", Path("."))
    assert anyio.run(p22a.do_read, session, ctx, "plan.run", {"steps": []}) == good
    assert len(session.calls) == 2
