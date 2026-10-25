"""Tests for `render.clip`: the Timeline FX of a clip or sequence rendered in the Foreground.

Run through the real Runtime with a scripted `transport.exec` over the fake Flame tree
(journal_test_helpers.py). The fake's `render` / `is_rendered` / `flush_renders` follow what was
measured on Flame 2025.2.4 (fake_media_tree.py): a render of a clip with Timeline FX makes
`is_rendered()` True and changes neither its uid nor its wiretap id, `flush_renders` does not undo
it, and a clip without Timeline FX reads as rendered. What a real Flame does is measured by
scripts/p5_render_clip_e2e.py."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from fake_media_tree import PyClip, PyTimelineFX, PyTrack, PyVersion, adopt, uid_of
from flame_mcp import server as server_mod
from flame_mcp import transport
from flame_mcp.journal import Index
from flame_mcp.policy import Policy
from flame_mcp.transport import BridgeResult
from journal_test_helpers import FakeBridge, Rig, bridge_result, make_rig
from timeline_p32a_helpers import S1, make_timeline_rig, media, wt

OP = "render.clip"
HEX32 = "0123456789abcdef"


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return make_timeline_rig(monkeypatch, tmp_path)


def fx_clip(container: Any, name: str, effects: tuple[str, ...] = ("Blur",)) -> PyClip:
    segment = media("clip1", 0, 23, effects=tuple(PyTimelineFX(t) for t in effects))
    clip = PyClip(
        name,
        wiretap_id=f"{container.get_wiretap_node_id()}/{name}",
        duration=24,
        versions=[PyVersion([PyTrack([segment])])],
    )
    adopt(container, [clip])
    container._children.append(clip)
    return clip


def sel(clip: Any) -> dict[str, Any]:
    return wt(clip.get_wiretap_node_id())


@pytest.fixture
def clip(rig: Rig) -> PyClip:
    made = fx_clip(rig.handles["reel"], "BLURRED")
    rig.handles["blurred"] = made
    return made


def render_clip(rig: Rig, target: Any, **extra: Any) -> dict[str, Any]:
    return rig.do(OP, {"target": target, **extra})


def intents(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "intent"]


def outcomes(rig: Rig) -> list[dict[str, Any]]:
    return [r for r in rig.records() if r.get("type") == "outcome"]


def record_types(rig: Rig) -> list[str]:
    return [str(r["type"]) for r in rig.records()]


def states(rig: Rig) -> dict[str, str]:
    env = rig.do("journal.list", {})
    return {item["group"]: item["state"] for item in env["result"]["items"]}


def blocked_groups(rig: Rig) -> list[str]:
    return [b.group for b in rig.runtime.guard.blocked()]


def op_calls(rig: Rig, phase: str) -> list[Any]:
    marker = {"read": "_result = _rc_inspect(", "run": "_result = _rc_render("}[phase]
    return [c for c in rig.exec.calls if c.label == OP and marker in c.code]


def check(rig: Rig, group: str) -> dict[str, Any]:
    env = rig.do("journal.check", {"group": group})
    assert env["ok"] is True, env
    checked: list[dict[str, Any]] = env["result"]["checked"]
    return checked[0] if checked else {}


def intent_handle(rig: Rig) -> str:
    (intent,) = intents(rig)
    handle = intent["handle"]
    assert isinstance(handle, str) and len(handle) == 32 and set(handle) <= set(HEX32)
    return handle


def old_bridge(rig: Rig) -> None:
    """A bridge that does not announce `client_handle`: no handle is sent, nothing is kept."""
    rig.bridge.features = ()


def pending_render(
    rig: Rig,
    bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    target: Any,
    *,
    handle: str = "h123",
    finished: bool = True,
) -> dict[str, Any]:
    """`render.clip(timeout_s=1)` whose wait ended with the bridge's own pending handle. The
    render itself ran; with `finished` the bridge holds its answer for `poll`."""
    scripted = rig.exec
    bridge.features = ()

    def pending(
        code: str,
        thread: str,
        timeout_s: float | None = None,
        label: str | None = None,
        *,
        run_dir: Path | None = None,
        handle: str | None = None,
    ) -> BridgeResult:
        real = scripted(code, thread, timeout_s, label, run_dir=run_dir, handle=handle)
        if label != OP or "_result = _rc_render(" not in code:
            return real
        if finished:
            bridge.results[bridge_own] = real
        else:
            bridge.running.add(bridge_own)
        return bridge_result(pending_handle=bridge_own)

    bridge_own = handle
    monkeypatch.setattr(transport, "exec", pending)
    return render_clip(rig, target, timeout_s=1)


# -- a render that works ----------------------------------------------------------------------


def test_a_render_makes_the_fx_rendered_and_reads_it_back(rig: Rig, clip: PyClip) -> None:
    uid_before = uid_of(clip)
    assert clip.is_rendered() is False
    clip.is_rendered_calls.clear()
    env = render_clip(rig, sel(clip))
    assert env["ok"] is True, env
    result = env["result"]
    assert result["ran"] is True and result["returned"] is True
    assert (result["isRenderedBefore"], result["isRenderedAfter"]) == (False, True)
    assert result["renderMode"] == "All" and result["renderOption"] == "Foreground"
    assert result["renderQuality"] == "Full Resolution" and result["includeHandles"] is False
    assert result["effects"] == ["Blur"] and result["effectCount"] == 1
    assert result["target"]["name"] == "BLURRED" and result["target"]["uid"] == uid_before
    assert result["target"]["type"] == "PyClip" and result["target"]["selector"] == sel(clip)
    assert result["elapsedS"] is not None and "unsupported" in result["undo"]
    assert clip.is_rendered() is True and uid_of(clip) == uid_before
    assert clip.render_calls == [
        {
            "render_mode": "All",
            "render_option": "Foreground",
            "render_quality": "Full Resolution",
            "effect_type": "",
            "effect_caching_mode": "Current",
            "include_handles": False,
        }
    ]
    assert env["affected"] == 1 and env["undoSteps"] == 0 and env["degraded"] == []
    assert env["journal"]["undo"] == "unsupported" and env["journal"]["revert"] is None


def test_the_run_reads_is_rendered_three_times_at_most(rig: Rig, clip: PyClip) -> None:
    clip.is_rendered_calls.clear()
    assert render_clip(rig, sel(clip))["ok"] is True
    assert len(clip.is_rendered_calls) == 3


def test_the_arguments_reach_both_the_render_and_the_read_back(rig: Rig, clip: PyClip) -> None:
    env = render_clip(
        rig,
        sel(clip),
        render_mode="Top",
        include_handles=True,
    )
    assert env["ok"] is True, env
    (call,) = clip.render_calls
    assert call["render_mode"] == "Top" and call["render_quality"] == "Full Resolution"
    assert call["include_handles"] is True and call["effect_caching_mode"] == "Current"
    assert call["render_option"] == "Foreground"
    assert clip.is_rendered_calls and all(
        c == {"top_only": True, "render_quality": "Full Resolution"} for c in clip.is_rendered_calls
    )
    assert env["result"]["renderMode"] == "Top" and env["result"]["includeHandles"] is True


def test_the_flame_code_is_read_once_then_run_once_with_the_loops_in_the_prelude(
    rig: Rig, clip: PyClip
) -> None:
    render_clip(rig, sel(clip))
    (read,) = op_calls(rig, "read")
    (run,) = op_calls(rig, "run")
    assert read.journal_types == [] and "intent" in run.journal_types
    for call in (read, run):
        ast.parse(call.code)
        assert call.code.isascii()
    own = run.code.split("__op_src = ", 1)[1].split("\n", 1)[0]
    source = ast.literal_eval(own)
    tree = ast.parse(source)
    banned = (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    assert not [n for n in ast.walk(tree) if isinstance(n, banned)]


def test_a_sequence_is_rendered_like_a_clip(rig: Rig) -> None:
    sequence = rig.handles["s1"]
    assert sequence.is_rendered() is False
    env = render_clip(rig, S1)
    assert env["ok"] is True, env
    assert env["result"]["target"]["type"] == "PySequence"
    assert env["result"]["effects"] == ["Burn-in Letterbox"]
    assert sequence.is_rendered() is True and len(sequence.render_calls) == 1


def test_a_clip_inside_a_reel_of_a_batch_group_is_rendered(rig: Rig) -> None:
    reel = rig.handles["bg1"].create_reel("Schematic Reel 1")
    inside = fx_clip(reel, "BATCHFX")
    env = render_clip(rig, sel(inside))
    assert env["ok"] is True, env
    assert inside.is_rendered() is True and len(inside.render_calls) == 1


def test_a_path_selector_is_turned_into_the_wiretap_selector_for_the_run(
    rig: Rig, clip: PyClip
) -> None:
    path = {"kind": "path", "root": "desktop", "path": ["RG", "Reel 1", "BLURRED"]}
    env = render_clip(rig, path)
    assert env["ok"] is True, env
    (intent,) = intents(rig)
    assert intent["target"] == sel(clip)
    assert env["result"]["target"]["selector"] == sel(clip)


# -- the journal ------------------------------------------------------------------------------


def test_the_intent_is_flushed_before_the_render_and_the_outcome_lands(
    rig: Rig, clip: PyClip
) -> None:
    env = render_clip(rig, sel(clip), timeout_s=60)
    (intent,) = intents(rig)
    assert intent["op"] == OP and intent["kind"] == "render_clip"
    assert intent["target"] == sel(clip) and intent["isRenderedBefore"] is False
    assert intent["renderMode"] == "All" and intent["renderQuality"] == "Full Resolution"
    assert intent["includeHandles"] is False and intent["timeoutS"] == 60
    assert intent["uid"] == uid_of(clip) and intent["name"] == "BLURRED"
    assert intent["handleSource"] == "client" and intent["handle"] == intent_handle(rig)
    (run,) = op_calls(rig, "run")
    assert run.handle == intent["handle"] and run.timeout_s == 60
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is True and outcome["isRenderedAfter"] is True
    assert outcome["verdict"] == "landed" and outcome["applied"] == []
    assert states(rig)[env["journal"]["group"]] == "landed"
    assert record_types(rig) == ["intent", "outcome"]


def test_a_run_answered_in_band_has_its_result_file_removed_by_one_poll(
    rig: Rig, clip: PyClip
) -> None:
    env = render_clip(rig, sel(clip))
    assert rig.bridge.calls == [intent_handle(rig)] and rig.bridge.results == {}
    assert env["ok"] is True and env["degraded"] == []


def test_an_old_bridge_gets_no_handle_and_the_intent_says_so(rig: Rig, clip: PyClip) -> None:
    old_bridge(rig)
    assert render_clip(rig, sel(clip))["ok"] is True
    (intent,) = intents(rig)
    assert intent["handle"] is None and intent["handleSource"] is None
    assert rig.bridge.calls == []


# -- judging ----------------------------------------------------------------------------------


def test_a_render_that_returns_false_but_leaves_the_fx_rendered_is_ok_with_a_note(
    rig: Rig, clip: PyClip
) -> None:
    clip.render_returns = False
    env = render_clip(rig, sel(clip))
    assert env["ok"] is True, env
    (note,) = env["degraded"]
    assert "False" in note and "not True" in note
    assert env["result"]["returned"] is False and env["result"]["isRenderedAfter"] is True


def test_a_render_that_answers_but_leaves_the_fx_unrendered_is_silent_noop(
    rig: Rig, clip: PyClip
) -> None:
    clip.render_effective = False
    env = render_clip(rig, sel(clip))
    assert env["ok"] is False and env["errorCode"] == "SILENT_NOOP", env
    assert env["result"]["isRenderedAfter"] is False and env["result"]["returned"] is True
    assert env["affected"] == 0 and env["journal"]["undo"] == "unsupported"
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is False and outcome["errorCode"] == "SILENT_NOOP"
    assert states(rig)[env["journal"]["group"]] == "failed"
    assert blocked_groups(rig) == []


def test_a_render_that_raises_is_a_flame_api_error_and_keeps_what_was_read(
    rig: Rig, clip: PyClip
) -> None:
    clip.render_raises = "out of disk"
    env = render_clip(rig, sel(clip))
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "out of disk" in env["error"]
    assert env["result"]["isRenderedBefore"] is False and env["result"]["isRenderedAfter"] is False
    (outcome,) = outcomes(rig)
    assert outcome["ok"] is False and outcome["errorCode"] == "FLAME_API_ERROR"
    assert blocked_groups(rig) == []


def test_a_uid_that_changed_during_the_render_is_a_note(rig: Rig, clip: PyClip) -> None:
    clip.render_changes_uid = True
    env = render_clip(rig, sel(clip))
    assert env["ok"] is True, env
    (note,) = env["degraded"]
    assert "uid" in note and "changed" in note


def test_a_clip_that_is_not_the_one_read_is_a_conflict_and_nothing_is_rendered(
    rig: Rig, clip: PyClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    def swap(label: str | None) -> None:
        if label == OP and "_result = _rc_render(" in rig.exec.calls[-1].code:
            clip.uid.set_value("uid-other")

    rig.exec.before_run = swap
    env = render_clip(rig, sel(clip))
    assert env["ok"] is False and env["errorCode"] == "CONFLICT", env
    assert clip.render_calls == []
    assert outcomes(rig)[0]["errorCode"] == "CONFLICT" and blocked_groups(rig) == []


# -- refusals ---------------------------------------------------------------------------------


def test_a_clip_without_timeline_fx_is_refused_and_not_called_rendered(rig: Rig) -> None:
    plain = fx_clip(rig.handles["reel"], "PLAIN", effects=())
    assert plain.is_rendered() is True
    plain.is_rendered_calls.clear()
    env = render_clip(rig, sel(plain))
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "no Timeline FX" in env["error"] and "already rendered" not in env["error"]
    assert env["result"]["ran"] is False and env["result"]["effects"] == []
    assert plain.render_calls == [] and rig.records() == []
    assert plain.is_rendered_calls == []


def test_a_clip_that_is_already_rendered_is_refused_and_force_renders_it_again(
    rig: Rig, clip: PyClip
) -> None:
    assert render_clip(rig, sel(clip))["ok"] is True
    clip.render_calls.clear()
    before = len(rig.records())
    env = render_clip(rig, sel(clip))
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "already rendered" in env["error"] and "force=true" in env["hint"]
    assert env["result"]["ran"] is False and env["result"]["isRendered"] is True
    assert clip.render_calls == [] and len(rig.records()) == before
    forced = render_clip(rig, sel(clip), force=True)
    assert forced["ok"] is True, forced
    assert forced["result"]["isRenderedBefore"] is True and len(clip.render_calls) == 1
    assert intents(rig)[-1]["isRenderedBefore"] is True


def test_a_clip_in_a_library_is_refused_and_nothing_is_journaled(rig: Rig) -> None:
    inside = fx_clip(rig.handles["tree_reel"], "LIBFX")
    env = render_clip(rig, sel(inside))
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET", env
    assert "library" in env["error"] and env["result"]["ran"] is False
    assert inside.render_calls == [] and rig.records() == []


def test_a_target_that_is_not_a_clip_or_sequence_is_selector_type(rig: Rig) -> None:
    env = render_clip(rig, wt(rig.handles["reel"].get_wiretap_node_id()))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_TYPE", env
    assert "PyReel" in env["error"] and rig.records() == []


def test_a_target_nothing_matches_is_selector_empty_and_nothing_is_journaled(rig: Rig) -> None:
    env = render_clip(rig, wt("/wt/RG/Reel 1/NOWHERE"))
    assert env["ok"] is False and env["errorCode"] == "SELECTOR_EMPTY", env
    assert rig.records() == []


def test_an_unknown_enum_value_is_invalid_args(rig: Rig, clip: PyClip) -> None:
    for extra in (
        {"render_mode": "Bottom"},
        {"render_mode": "Selection"},
        {"include_handles": "yes"},
    ):
        env = render_clip(rig, sel(clip), **extra)
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", (extra, env)
    assert clip.render_calls == [] and rig.records() == []


def test_a_selector_kind_the_op_does_not_take_is_invalid_args(rig: Rig) -> None:
    env = render_clip(rig, {"kind": "batch", "name": "BG1"})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env


def test_timeout_s_is_bounded(rig: Rig, clip: PyClip) -> None:
    for value in (0, 3601):
        env = render_clip(rig, sel(clip), timeout_s=value)
        assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", (value, env)
    assert render_clip(rig, sel(clip), timeout_s=3600)["ok"] is True


def test_a_write_that_ended_unknown_blocks_the_next_render(rig: Rig, clip: PyClip) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    assert render_clip(rig, sel(clip), timeout_s=1)["errorCode"] == "TIMEOUT"
    other = fx_clip(rig.handles["reel"], "OTHER")
    env = render_clip(rig, sel(other))
    assert env["errorCode"] == "POLICY_DENIED" and other.render_calls == []


# -- an answer that never came ----------------------------------------------------------------


def test_a_timeout_closes_the_write_door_and_leaves_the_intent_open(rig: Rig, clip: PyClip) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)  # the render ran, the answer is lost
    env = render_clip(rig, sel(clip), timeout_s=1)
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    group = env["journal"]["group"]
    handle = intent_handle(rig)
    assert "journal.check" in env["hint"] and "keeps" in env["hint"] and handle in env["hint"]
    assert "may not answer at all" in env["hint"] and "again" in env["hint"]
    assert "output files" not in env["hint"]
    assert group in blocked_groups(rig) and env["journal"]["undo"] == "unsupported"
    assert outcomes(rig) == [] and record_types(rig) == ["intent", "unresolved"]
    assert rig.do("render.plan", {"target": {"kind": "batch", "name": "BG1"}})["ok"] is True
    denied = render_clip(rig, sel(clip), force=True)
    assert denied["errorCode"] == "POLICY_DENIED" and len(clip.render_calls) == 1


def test_check_asks_the_bridge_for_the_intents_handle_and_judges_by_its_answer(
    rig: Rig, clip: PyClip
) -> None:
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    handle = intent_handle(rig)
    reads_before = len(op_calls(rig, "read"))
    entry = check(rig, group)
    assert rig.bridge.calls == [handle]
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"
    assert entry["renderOutcome"]["ok"] is True and entry["renderOutcome"]["returned"] is True
    assert entry["renderOutcome"]["isRenderedAfter"] is True
    assert len(op_calls(rig, "read")) == reads_before  # Flame is not read
    assert blocked_groups(rig) == [] and states(rig)[group] == "landed"
    types = record_types(rig)
    assert types.index("polled") < types.index("check")
    polled = next(r for r in rig.records() if r["type"] == "polled")
    assert polled["handle"] == handle and polled["ok"] is True
    assert polled["flame"]["isRenderedAfter"] is True
    checked = next(r for r in rig.records() if r["type"] == "check")
    assert checked["basis"] == "handle" and checked["verdict"] == "landed"
    assert check(rig, group) == {}  # decided: nothing left to check


def test_check_by_the_handle_reads_a_silent_noop_as_not_landed(rig: Rig, clip: PyClip) -> None:
    clip.render_effective = False
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "not_landed" and entry["basis"] == "handle"
    assert entry["renderOutcome"]["errorCode"] == "SILENT_NOOP"
    assert blocked_groups(rig) == []


def test_check_by_the_handle_reads_a_render_that_raised_as_failed(rig: Rig, clip: PyClip) -> None:
    clip.render_raises = "out of disk"
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "failed" and entry["basis"] == "handle"
    assert "out of disk" in entry["renderOutcome"]["error"]


def test_a_render_still_running_is_unknown_and_the_door_stays_shut(rig: Rig, clip: PyClip) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    handle = intent_handle(rig)
    held = rig.bridge.results.pop(handle)
    rig.bridge.running.add(handle)
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "still running" in entry["detail"]
    assert blocked_groups(rig) == [group]
    rig.bridge.running.discard(handle)
    rig.bridge.results[handle] = held
    assert check(rig, group)["verdict"] == "landed" and blocked_groups(rig) == []


def test_a_bridge_that_cannot_be_asked_leaves_the_check_unknown(rig: Rig, clip: PyClip) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    rig.bridge.failure = bridge_result(ok=False, error_code="TIMEOUT", error="no reply")
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "TIMEOUT" in entry["detail"]
    assert "may not answer at all" in entry["detail"] and blocked_groups(rig) == [group]


def test_a_pending_handle_of_the_bridge_is_journaled_and_used(
    rig: Rig, clip: PyClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = pending_render(rig, rig.bridge, monkeypatch, sel(clip))
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert "pending handle" in env["error"] and env["pending"] == {"handle": "h123"}
    assert record_types(rig) == ["intent", "unresolved", "pending"]
    assert rig.records()[-1]["handle"] == "h123" and rig.records()[-1]["timeoutS"] == 1.0
    (view,) = Index(rig.journal.read_all().records).groups[env["journal"]["group"]]
    assert view.pending_handle == "h123"
    entry = check(rig, env["journal"]["group"])
    assert rig.bridge.calls == ["h123"]
    assert entry["verdict"] == "landed" and entry["basis"] == "handle"


def test_check_reads_flame_when_there_is_no_handle_and_a_rendered_clip_is_landed(
    rig: Rig, clip: PyClip
) -> None:
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    assert record_types(rig) == ["intent", "unresolved"] and rig.bridge.calls == []
    reads_before = len(op_calls(rig, "read"))
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "flame"
    assert "estimate" in entry["detail"]
    assert len(op_calls(rig, "read")) == reads_before + 1
    assert entry["current"]["isRendered"] is True
    assert blocked_groups(rig) == [] and states(rig)[group] == "landed"
    checked = next(r for r in rig.records() if r["type"] == "check")
    assert checked["basis"] == "flame" and checked["verdict"] == "landed"


def test_check_reads_flame_and_an_unrendered_clip_is_not_landed(rig: Rig, clip: PyClip) -> None:
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="before", skip=1)  # the render never started
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    assert clip.render_calls == []
    entry = check(rig, group)
    assert entry["verdict"] == "not_landed" and entry["basis"] == "flame"
    assert entry["current"]["isRendered"] is False
    assert blocked_groups(rig) == [] and states(rig)[group] == "not_landed"


def test_an_intents_handle_the_bridge_never_saw_falls_back_to_flame_and_says_why(
    rig: Rig, clip: PyClip
) -> None:
    rig.exec.fault(OP, "BRIDGE_LOST", when="before", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    handle = intent_handle(rig)
    entry = check(rig, group)
    assert rig.bridge.calls == [handle]
    assert entry["verdict"] == "not_landed" and entry["basis"] == "flame"
    assert "Flame was restarted before the render finished" in entry["detail"]
    assert handle in entry["detail"]


def test_check_by_flame_says_a_clip_that_was_rendered_before_proves_nothing_more(
    rig: Rig, clip: PyClip
) -> None:
    assert render_clip(rig, sel(clip))["ok"] is True
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="before", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1, force=True)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "landed" and entry["basis"] == "flame"
    assert "already rendered" in entry["detail"]


def test_check_by_flame_finds_a_clip_that_is_gone_diverged(rig: Rig, clip: PyClip) -> None:
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    rig.handles["reel"]._children.remove(clip)
    rig.flame.forget_wiretap(clip)
    entry = check(rig, group)
    assert entry["verdict"] == "diverged" and entry["basis"] == "flame"
    assert blocked_groups(rig) == []


def test_check_by_flame_is_unknown_while_flame_cannot_be_read(rig: Rig, clip: PyClip) -> None:
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    rig.exec.fault(OP, "TIMEOUT", when="before")
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "may not answer at all" in entry["detail"]
    assert blocked_groups(rig) == [group]


# -- no undo, no plan, policy -----------------------------------------------------------------


def test_a_render_group_cannot_be_reverted(rig: Rig, clip: PyClip) -> None:
    group = render_clip(rig, sel(clip))["journal"]["group"]
    env = rig.do("journal.revert", {"group": group})
    assert env["ok"] is False and env["errorCode"] == "PRECONDITION_UNMET"
    assert "cannot be undone" in env["error"] and "render" in env["error"]
    assert clip.flush_calls == 0 and clip.is_rendered() is True


def test_plan_run_refuses_render_clip_with_the_reason_and_runs_nothing(
    rig: Rig, clip: PyClip
) -> None:
    env = rig.do("plan.run", {"steps": [{"op": OP, "args": {"target": sel(clip)}}]})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS"
    (issue,) = env["result"]["issues"]
    assert issue["op"] == OP and "occupies Flame" in issue["problems"][0]
    assert "cannot be undone" in issue["problems"][0]
    assert clip.render_calls == [] and rig.exec.labels() == []


def test_readonly_hides_render_clip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    readonly = make_rig(monkeypatch, tmp_path, policy=Policy.from_env({"FLAME_MCP_READONLY": "1"}))
    env = readonly.do(OP, {"target": wt("/wt/c1")})
    assert env["errorCode"] == "POLICY_DENIED"


def test_the_instructions_ask_for_confirmation_before_render_clip() -> None:
    text = server_mod.INSTRUCTIONS
    assert "render.clip" in text and "ask the user to confirm" in text
    assert len(text.encode("utf-8")) <= server_mod.MAX_INSTRUCTIONS_BYTES


def test_the_declarations(rig: Rig) -> None:
    op = rig.registry.get(OP)
    assert op is not None
    assert (op.effect, op.undo, op.category) == ("create", "unsupported", "render")
    assert op.thread == "main-async" and op.wrap is not None
    assert op.checkpoint is None and op.journal is None
    assert op.accepts == ("wiretap", "path", "selection")
    assert op.undo_steps({}, 1) == 0
    params = {p.name: p for p in op.params}
    assert list(params) == [
        "target",
        "render_mode",
        "include_handles",
        "timeout_s",
        "force",
    ]
    assert params["render_mode"].enum == ("Top", "All")
    assert params["render_mode"].default == "All"
    assert params["include_handles"].default is False and params["force"].default is False
    assert (params["timeout_s"].default, params["timeout_s"].minimum) == (120, 1)
    assert params["timeout_s"].integer
    assert "reads included" in params["timeout_s"].description
    for needle in (
        "Flame is occupied",
        "reads included",
        "this server has no op to stop a render",
        "render.clip provides no undo",
        "plan.run",
        "journal.check",
        "force=true",
        "timelineFxLost",
        "Foreground",
    ):
        assert needle in op.description, needle
    assert "ML Timewarp" not in op.description and "plan/" not in op.description
    category = rig.registry.categories()["render"]
    assert "Timeline FX" in category.description and "Batch group" in category.description


def test_a_render_group_shows_the_same_way_as_render_batch_in_the_journal(
    rig: Rig, clip: PyClip
) -> None:
    group = render_clip(rig, sel(clip))["journal"]["group"]
    shown = rig.do("journal.show", {"group": group})
    assert shown["ok"] is True, shown


# -- non-ASCII, returned False, a read that did not answer, descriptions ---------


def pending_read(monkeypatch: pytest.MonkeyPatch, rig: Rig, marker: str, handle: str) -> None:
    """The Flame call whose code holds `marker` ends with the bridge's pending handle."""
    scripted = rig.exec

    def pending(
        code: str,
        thread: str,
        timeout_s: float | None = None,
        label: str | None = None,
        *,
        run_dir: Path | None = None,
        handle: str | None = None,
    ) -> BridgeResult:
        if label == OP and marker in code:
            return bridge_result(pending_handle=pending_handle)
        return scripted(code, thread, timeout_s, label, run_dir=run_dir, handle=handle)

    pending_handle = handle
    monkeypatch.setattr(transport, "exec", pending)


def test_a_non_ascii_wiretap_id_read_from_a_selection_is_refused_before_the_intent(
    rig: Rig,
) -> None:
    odd = fx_clip(rig.handles["reel"], "CAFÉ")
    rig.flame.media_panel.selected_entries = [odd]
    env = render_clip(rig, {"kind": "selection"})
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert "nothing was rendered" in env["error"] and "U+00C9" in env["error"]
    assert env["error"].isascii() and "no intent was written" in env["hint"]
    assert env["result"]["ran"] is False
    assert rig.records() == [] and odd.render_calls == [] and op_calls(rig, "run") == []


def test_a_render_that_returned_false_and_left_the_fx_unrendered_is_a_flame_api_error(
    rig: Rig, clip: PyClip
) -> None:
    clip.render_returns = False
    clip.render_effective = False
    env = render_clip(rig, sel(clip))
    assert env["ok"] is False and env["errorCode"] == "FLAME_API_ERROR", env
    assert "False" in env["error"] and "is_rendered()" in env["error"]
    assert outcomes(rig)[0]["errorCode"] == "FLAME_API_ERROR" and blocked_groups(rig) == []


def test_check_by_the_handle_reads_a_false_return_with_nothing_rendered_as_failed(
    rig: Rig, clip: PyClip
) -> None:
    clip.render_returns = False
    clip.render_effective = False
    rig.exec.fault(OP, "BRIDGE_LOST", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    entry = check(rig, group)
    assert entry["verdict"] == "failed" and entry["basis"] == "handle"


def test_a_read_that_ends_pending_says_so_and_nothing_is_rendered_or_journaled(
    rig: Rig, clip: PyClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending_read(monkeypatch, rig, "_result = _rc_inspect(", "h77")
    env = render_clip(rig, sel(clip))
    assert env["ok"] is False and env["errorCode"] == "TIMEOUT", env
    assert env["pending"] == {"handle": "h77"}
    assert "None" not in env["error"] and "nothing was rendered" in env["error"]
    assert "pending handle" in env["error"] and "journal.poll" in env["hint"]
    assert rig.records() == [] and clip.render_calls == []


def test_check_by_flame_says_a_read_that_ended_pending_in_words(
    rig: Rig, clip: PyClip, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_bridge(rig)
    rig.exec.fault(OP, "TIMEOUT", when="after", skip=1)
    group = render_clip(rig, sel(clip), timeout_s=1)["journal"]["group"]
    pending_read(monkeypatch, rig, "_result = _rc_inspect(", "h78")
    entry = check(rig, group)
    assert entry["verdict"] == "unknown" and "None" not in entry["detail"]
    assert "pending handle h78" in entry["detail"] and "journal.check again" in entry["detail"]
    assert blocked_groups(rig) == [group]


def test_the_requires_name_the_reel_type_and_the_descriptions_name_the_force_caveat(
    rig: Rig,
) -> None:
    op = rig.registry.get(OP)
    check_op = rig.registry.get("journal.check")
    assert op is not None and check_op is not None
    assert "PyReel.type" in op.requires
    for text in (op.description, check_op.description):
        assert "isRenderedBefore" in text and "not evidence" in text


def test_render_quality_is_not_a_parameter_and_every_call_uses_full_resolution(
    rig: Rig, clip: PyClip
) -> None:
    env = render_clip(rig, sel(clip), render_quality="Proxy")
    assert env["ok"] is False and env["errorCode"] == "INVALID_ARGS", env
    assert "render_quality" in env["error"]
    assert clip.render_calls == [] and rig.records() == []
    text = rig.registry.get(OP).description  # type: ignore[union-attr]
    for word in ("render_quality", "Proxy", "Selection"):
        assert word not in text, word
    assert render_clip(rig, sel(clip))["ok"] is True
    assert {c["render_quality"] for c in clip.render_calls} == {"Full Resolution"}
    assert {c["render_quality"] for c in clip.is_rendered_calls} == {"Full Resolution"}
