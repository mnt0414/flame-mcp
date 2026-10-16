#!/usr/bin/env python3
"""MCP end-to-end verification client (run with the `.venv` Python).

Checks: Eight basic operations (reads of the project, libraries, desktop, timeline and segments, and
`desktop.rename`) through the MCP server: the catalog, the answers, a rename and its revert, and the
error cases.

Drives the real MCP surface (`python -m flame_mcp` over stdio, via the
`mcp` package's own stdio client -- `mcp.client.stdio.stdio_client` +
`mcp.client.session.ClientSession`) against a live Flame + bridge that
`scripts/run_e2e.sh p1_mcp` has already started. This script never imports
or drives Flame itself, and never starts one -- only `flame_mcp.transport`
(the same client the server's own tools use) for setup/teardown/undo, which
is allowed as test scaffolding, not an op.

Steps:
  a. flame_version_info (bridge reachable) / flame_catalog (the 8 ops under test, all still
     there, and the total equals the registry's op count)
  b. prepare: library P1O_<ts> / reel R1 / import clip1-3 (1 request each);
     desktop reel group + reel + sequence, overwrite 2-3 clips (1 request
     each, u5_seg's own call shapes -- desktop_reel.create_sequence(name=),
     seq.overwrite(clip, PyTime, track))
  c. reads: project.info / library.list / desktop.list_children (path, then
     path+limit=2 truncated, then wiretap on a non-container) /
     desktop.selection / timeline.info / list_segments / segment.info
     (c_timecode_shape: the
     PyTime-shaped fields -- recordIn/recordOut/sourceIn/sourceOut/
     recordDuration -- must come back as {"timecode": str, "frame": int},
     never a quoted string) / flame_context
     (c_flame_context: undoContract mentions revert+undoSteps, libraries
     includes the library step b just created, currentTab is a string,
     context is a rider dict)
     Further checks: c_list_children_offset (2nd page via offset, no overlap
     with the 1st), c_list_children_types (types=[PyReel] keeps only reels,
     types=[PyClip] on a reel keeps its clips, a type that is absent gives
     0), c_list_segments_gap_and_media (a Gap item has type=="Gap" and
     isGap True, a media item has type=="Video Segment" and isGap False;
     read from a Desktop U5S* sequence that holds a Gap, recorded as
     skipped when none is found),
     c_segment_info_attributes (a dict whose `name` is a string),
     a_catalog_selector_fields / c_selector_vocabulary_fields (fields +
     example are published), d_revert_op_is_desktop_rename
  d. write: rename a clip (path selector); confirm undoSteps==0 and a
     `revert` op+args came back (a set_value() rename is
     never pushed onto Flame's native Undo history, so `execute_shortcut
     ('Undo')` must not be used -- it would silently revert an unrelated
     prior operation instead); call flame_do again with that `revert`, then
     verify via a transport read that the name is back to `before`; repeat
     via a wiretap selector (the reel)
  e. abnormal paths: unknown wiretap/path -> SELECTOR_EMPTY, selection of 0
     -> SELECTOR_EMPTY, wrong shape / unaccepted kind -> INVALID_ARGS
  f. a second server subprocess with FLAME_MCP_READONLY=1: tools/list's
     flame_do op enum and flame_catalog both omit every op READONLY hides (the registry's
     non-read ops: desktop.rename + the *.set_attributes, plus eval.run);
     flame_do(desktop.rename) -> POLICY_DENIED; a read op still works
  g. cleanup via transport (delete the created library + desktop reel),
     then flame.exit() (no response is expected -- the connection just
     closes as the bridge and Flame shut down)

  h. cross-cutting checks over every envelope any of the 4 tools returned
     across a-f (tracked in results["_envelopes"] by `_structured`):
     h_envelope_shape (every envelope key `envelope.build_envelope`
     produces is present -- read from the real function, not hardcoded),
     h_rider (every successful flame_do that reached Flame has
     context.project == the test project's name, context.workspace a string,
     context.selected an int), h_logs_and_crash (every envelope has
     logs as a list and crash as None, including step f's READONLY
     subprocess)

Every step's envelope is written verbatim to
~/Library/Application Support/flame-mcp/p1/p1_mcp_e2e_<ts>.json. Value
comparisons in this script's own code only ever compare already-JSON-decoded
plain values (str/int/...) -- the `_v(...)`/`.get_value()` unwrapping
happens inside the code strings sent to the bridge, before `_result` is set,
exactly like `execution.build_exec_code`'s convention.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

import anyio

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import e2e_guard  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from flame_mcp import envelope as envelope_mod  # noqa: E402
from flame_mcp import transport  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

OUTPUT_DIR = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p1"
RUN_DIR_DEFAULT = Path.home() / "Library" / "Application Support" / "flame-mcp" / "run"
MEDIA_DIR = (
    Path.home() / "Library" / "Application Support" / "flame-mcp" / "probe_project" / "media"
)
CLIP_NAMES = ("clip1.mov", "clip2.mov", "clip3.mov")
GAP_SEQUENCE_PREFIX: Final[str] = "U5S"

P1_OP_NAMES: Final[frozenset[str]] = frozenset(
    {
        "project.info",
        "library.list",
        "desktop.list_children",
        "desktop.selection",
        "desktop.rename",
        "timeline.info",
        "timeline.list_segments",
        "segment.info",
    }
)
"""The 8 ops this script exercises; ops added later must not break it."""


def _registered_ops() -> tuple[frozenset[str], frozenset[str]]:
    """(every registered op name, the ops READONLY hides) from the real registry, so the
    expected counts follow the code instead of a hard-coded number."""
    registry = Registry()
    register_all(registry)
    ops = registry.list_ops()
    return (
        frozenset(op.name for op in ops),
        frozenset(op.name for op in ops if op.effect != "read" or op.name == "eval.run"),
    )


REGISTERED_OP_NAMES, WRITE_OP_NAMES = _registered_ops()
"""The ops hidden under FLAME_MCP_READONLY: the write ops and eval.run."""
SEGMENT_SELECTOR_FIELDS: Final[frozenset[str]] = frozenset(
    {"parent", "version", "track", "index", "record_in", "expect_source_name"}
)
V_PRELUDE = "_v = lambda x: x.get_value() if hasattr(x, 'get_value') else x"
PROBE_PROJECT_NAME: Final[str] = e2e_guard.probe_project_name()
_SEGMENT_TIME_FIELDS: Final[tuple[str, ...]] = (
    "recordIn",
    "recordOut",
    "sourceIn",
    "sourceOut",
    "recordDuration",
)


def record(results: dict[str, Any], name: str, ok: bool, **extra: Any) -> None:
    results["steps"].append({"name": name, "ok": ok, **extra})
    if not ok:
        results["ok"] = False


def _tx(
    code: str, run_dir: Path, *, timeout_s: float = 10.0, label: str | None = None
) -> transport.BridgeResult:
    """One `transport.exec` call on the main thread -- setup/teardown/undo
    scaffolding (explicitly not an op)."""
    return transport.exec(code, thread="main", timeout_s=timeout_s, label=label, run_dir=run_dir)


def _br_dict(br: transport.BridgeResult) -> dict[str, Any]:
    return {
        "ok": br.ok,
        "result": br.result,
        "errorCode": br.error_code,
        "error": br.error,
        "durationMs": br.duration_ms,
    }


def _structured(
    result: Any, *, tool: str | None = None, results: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`result.structured_content`, and -- when `tool`/`results` are given --
    also appended to `results["_envelopes"]` for the cross-cutting
    checks (`h_envelope_shape` / `h_rider` / `h_logs_and_crash`), which need
    to see every envelope any of the 4 MCP tools ever returned, not just the
    ones each individual step already inspects."""
    sc = result.structured_content
    assert isinstance(sc, dict)
    if tool is not None and results is not None:
        results.setdefault("_envelopes", []).append({"tool": tool, "env": sc})
    return sc


def _is_time_field(value: Any) -> bool:
    """`_j`'s PyTime shape is `{"timecode": str, "frame": int}`; `timecode`
    must never start with a quote character (a str()-style repr such as
    `"'00:00:00+00'"`)."""
    return (
        isinstance(value, dict)
        and set(value.keys()) == {"timecode", "frame"}
        and isinstance(value.get("timecode"), str)
        and not value["timecode"].startswith("'")
        and isinstance(value.get("frame"), int)
    )


def check_envelope_shapes(results: dict[str, Any]) -> None:
    """h_envelope_shape: every envelope any of the 4 MCP tools returned
    (flame_do/flame_catalog/flame_context/flame_version_info) must carry
    every key `envelope.build_envelope` produces -- read from the real
    function rather than hardcoded, so this never drifts from the real envelope."""
    expected_keys = set(envelope_mod.build_envelope(ok=True).keys())
    envelopes: list[dict[str, Any]] = results.get("_envelopes", [])
    missing_by_tool: dict[str, list[str]] = {}
    for entry in envelopes:
        env: dict[str, Any] = entry["env"]
        missing = sorted(expected_keys - set(env.keys()))
        if missing:
            missing_by_tool.setdefault(entry["tool"], []).extend(missing)
    record(
        results,
        "h_envelope_shape",
        len(envelopes) > 0 and not missing_by_tool,
        expectedKeys=sorted(expected_keys),
        checkedCount=len(envelopes),
        missingByTool=missing_by_tool,
    )


def check_rider(results: dict[str, Any]) -> None:
    """h_rider: every flame_do envelope that actually reached Flame and
    succeeded (context is not None) must carry a rider whose `project` is
    the probe project, `workspace` a string, `selected` an int."""
    envelopes: list[dict[str, Any]] = results.get("_envelopes", [])
    checked = 0
    bad_entries: list[dict[str, Any]] = []
    for entry in envelopes:
        if entry["tool"] != "flame_do":
            continue
        env: dict[str, Any] = entry["env"]
        if env.get("ok") is not True:
            continue
        context = env.get("context")
        if context is None:
            continue
        checked += 1
        if not (
            isinstance(context, dict)
            and context.get("project") == PROBE_PROJECT_NAME
            and isinstance(context.get("workspace"), str)
            and isinstance(context.get("selected"), int)
        ):
            bad_entries.append({"context": context})
    record(
        results,
        "h_rider",
        checked > 0 and not bad_entries,
        checkedCount=checked,
        badEntries=bad_entries,
        expectedProject=PROBE_PROJECT_NAME,
    )


def check_logs_and_crash(results: dict[str, Any]) -> None:
    """h_logs_and_crash: every tracked envelope (including the READONLY
    subprocess's f_* calls) must have `logs` as a list and `crash` as None
    -- app.log window present, no crash recorded for a normal call."""
    envelopes: list[dict[str, Any]] = results.get("_envelopes", [])
    bad_entries: list[dict[str, Any]] = []
    for entry in envelopes:
        env: dict[str, Any] = entry["env"]
        if not isinstance(env.get("logs"), list) or env.get("crash") is not None:
            bad_entries.append(
                {"tool": entry["tool"], "logs": env.get("logs"), "crash": env.get("crash")}
            )
    record(
        results,
        "h_logs_and_crash",
        len(envelopes) > 0 and not bad_entries,
        checkedCount=len(envelopes),
        badEntries=bad_entries,
    )


def _result_of(env: dict[str, Any]) -> dict[str, Any]:
    """`env["result"]` as a dict, defaulting to `{}` whether the key is
    missing OR present with value `None` (every `ok=False` envelope has
    `result: None`, not a missing key) -- `env.get("result", {})` only
    covers the first case and raises `AttributeError` on the second, which
    is exactly the shape every error envelope this script observes has."""
    result = env.get("result")
    return result if isinstance(result, dict) else {}


# step b: prepare (transport only)
def prepare_library_and_reel(run_dir: Path, ts: str, results: dict[str, Any]) -> dict[str, Any]:
    lib_name = f"P1O_{ts}"
    lib_code = "\n".join(
        [
            V_PRELUDE,
            "ws = flame.projects.current_project.current_workspace",
            f"lib = ws.create_library({lib_name!r})",
            "_result = lib.get_wiretap_node_id()",
        ]
    )
    lib_br = _tx(lib_code, run_dir, label="prepare_create_library")
    record(results, "b_create_library", lib_br.ok, resp=_br_dict(lib_br), libName=lib_name)
    lib_wtid = lib_br.result if lib_br.ok else None

    reel_code = "\n".join(
        [
            f"lib = flame.find_by_wiretap_node_id({lib_wtid!r})",
            "reel = lib.create_reel('R1')",
            "_result = reel.get_wiretap_node_id()",
        ]
    )
    reel_br = _tx(reel_code, run_dir, label="prepare_create_reel")
    record(results, "b_create_reel", reel_br.ok, resp=_br_dict(reel_br))
    reel_wtid = reel_br.result if reel_br.ok else None

    clip_wtids: list[str | None] = []
    for clip_name in CLIP_NAMES:
        clip_path = str(MEDIA_DIR / clip_name)
        import_code = "\n".join(
            [
                f"reel = flame.find_by_wiretap_node_id({reel_wtid!r})",
                f"clips = flame.import_clips({clip_path!r}, reel)",
                "_result = clips[0].get_wiretap_node_id() if clips else None",
            ]
        )
        import_br = _tx(import_code, run_dir, timeout_s=30.0, label=f"prepare_import_{clip_name}")
        record(results, f"b_import_{clip_name}", import_br.ok, resp=_br_dict(import_br))
        clip_wtids.append(import_br.result if import_br.ok else None)

    return {
        "libName": lib_name,
        "libWtid": lib_wtid,
        "reelWtid": reel_wtid,
        "clipWtids": clip_wtids,
    }


def prepare_desktop_sequence(
    run_dir: Path, ts: str, results: dict[str, Any], clip_wtid: str | None
) -> dict[str, Any]:
    """Makes the Desktop sequence with the bit depth of `clip_wtid` (the clips put on it are of
    that depth): Flame puts a Resize on a segment whose sequence has another bit depth, and the
    bit depth of a sequence made without one is the project's own."""
    rg_code = "\n".join(
        [
            "desktop = flame.projects.current_project.current_workspace.desktop",
            "groups = list(desktop.reel_groups)",
            f"rg = groups[0] if groups else desktop.create_reel_group('P1O_RG_{ts}')",
            "_result = rg.get_wiretap_node_id()",
        ]
    )
    rg_br = _tx(rg_code, run_dir, label="prepare_desktop_reel_group")
    record(results, "b_desktop_reel_group", rg_br.ok, resp=_br_dict(rg_br))
    rg_wtid = rg_br.result if rg_br.ok else None

    desk_reel_name = f"P1O_DESK_{ts}"
    seq_name = f"P1O_SEQ_{ts}"
    seq_code = "\n".join(
        [
            f"rg = flame.find_by_wiretap_node_id({rg_wtid!r})",
            f"desk_reel = rg.create_reel({desk_reel_name!r})",
            (
                f"_kw = {{'bit_depth': flame.find_by_wiretap_node_id({clip_wtid!r}).bit_depth}}"
                if clip_wtid is not None
                else "_kw = {}"
            ),
            f"seq = desk_reel.create_sequence(name={seq_name!r}, **_kw)",
            "_result = {",
            "    'deskReelWtid': desk_reel.get_wiretap_node_id(),",
            "    'seqWtid': seq.get_wiretap_node_id(),",
            "}",
        ]
    )
    seq_br = _tx(seq_code, run_dir, label="prepare_desktop_sequence")
    record(results, "b_desktop_sequence", seq_br.ok, resp=_br_dict(seq_br))
    seq_result = seq_br.result if seq_br.ok and isinstance(seq_br.result, dict) else {}
    return {
        "reelGroupWtid": rg_wtid,
        "deskReelWtid": seq_result.get("deskReelWtid"),
        "seqWtid": seq_result.get("seqWtid"),
    }


def overwrite_clips_onto_sequence(
    run_dir: Path, seq_wtid: str | None, clip_wtids: list[str | None], results: dict[str, Any]
) -> None:
    cursor = 1
    for i, clip_wtid in enumerate(clip_wtids[:2]):  # 2 clips are enough for the e2e
        if clip_wtid is None:
            record(results, f"b_overwrite_{i}", False, reason="no clip wiretap id from import step")
            continue
        code = "\n".join(
            [
                f"seq = flame.find_by_wiretap_node_id({seq_wtid!r})",
                f"clip = flame.find_by_wiretap_node_id({clip_wtid!r})",
                "track = seq.versions[0].tracks[0]",
                f"t = flame.PyTime({cursor!r})",
                "did_overwrite = seq.overwrite(clip, t, track)",
                "segs = list(track.segments)",
                "_result = {'ok': bool(did_overwrite), 'segmentCount': len(segs)}",
            ]
        )
        br = _tx(code, run_dir, timeout_s=15.0, label=f"prepare_overwrite_{i}")
        record(results, f"b_overwrite_{i}", br.ok, resp=_br_dict(br))
        if br.ok and isinstance(br.result, dict):
            cursor += 10  # advance the cursor past this clip's placeholder duration


def find_gap_sequence(run_dir: Path, results: dict[str, Any]) -> str | None:
    """Wiretap id of a Desktop sequence named U5S* whose version 0 / track 0
    holds a Gap, or None. Transport-only scaffolding (read-only)."""
    code = "\n".join(
        [
            V_PRELUDE,
            "_found = []",
            "for _rg in flame.projects.current_project.current_workspace.desktop.reel_groups:",
            "    for _reel in _rg.reels:",
            "        for _seq in _reel.children:",
            "            if type(_seq).__name__ != 'PySequence':",
            "                continue",
            f"            if not _v(_seq.name).startswith({GAP_SEQUENCE_PREFIX!r}):",
            "                continue",
            "            _segs = list(_seq.versions[0].tracks[0].segments)",
            "            if any(_v(_x.type) == 'Gap' for _x in _segs):",
            "                _found.append(_seq.get_wiretap_node_id())",
            "_result = _found",
        ]
    )
    br = _tx(code, run_dir, timeout_s=30.0, label="find_gap_sequence")
    found = br.result if br.ok and isinstance(br.result, list) else []
    results["_gap_sequence_scan"] = {"resp": _br_dict(br), "found": found}
    return found[0] if found else None


# step g: cleanup (transport only)
def cleanup(
    run_dir: Path, lib_wtid: str | None, desk_reel_wtid: str | None, results: dict[str, Any]
) -> None:
    if lib_wtid is not None:
        code = "\n".join(
            [
                f"lib = flame.find_by_wiretap_node_id({lib_wtid!r})",
                "flame.delete(lib, confirm=False)",
                "_result = True",
            ]
        )
        br = _tx(code, run_dir, label="cleanup_delete_library")
        record(results, "g_delete_library", br.ok, resp=_br_dict(br))
    if desk_reel_wtid is not None:
        code = "\n".join(
            [
                f"reel = flame.find_by_wiretap_node_id({desk_reel_wtid!r})",
                "flame.delete(reel, confirm=False)",
                "_result = True",
            ]
        )
        br = _tx(code, run_dir, label="cleanup_delete_desktop_reel")
        record(results, "g_delete_desktop_reel", br.ok, resp=_br_dict(br))

    exit_br = _tx("flame.exit()", run_dir, timeout_s=5.0, label="cleanup_exit")
    # No response is expected: the socket closes as Flame exits. BRIDGE_LOST
    # (or a client-side TIMEOUT if the exit races the socket teardown) is the
    # success shape here, not a real failure.
    record(
        results,
        "g_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
    )


@asynccontextmanager
async def mcp_session(
    run_dir: Path, *, readonly: bool = False, eval_enabled: bool = True
) -> AsyncIterator[ClientSession]:
    """Spawn `python -m flame_mcp` and yield a connected session.

    `FLAME_MCP_RUN_DIR` is set explicitly to `run_dir` so this subprocess's
    own bridge discovery agrees with every `transport.exec` call this script
    makes directly (steps b/d/g) -- without it, the spawned server would
    silently fall back to its own default (`~/Library/Application
    Support/flame-mcp/run`), which is wrong whenever `--run-dir` overrides
    the default (as it must for a dry run against no bridge at all, to avoid
    ever touching a real bridge socket that happens to exist there).

    eval.run exists only with FLAME_MCP_ENABLE_EVAL=1, so the server gets it unless `readonly` or
    `eval_enabled` is False.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["FLAME_MCP_RUN_DIR"] = str(run_dir)
    if readonly:
        env["FLAME_MCP_READONLY"] = "1"
    else:
        env.pop("FLAME_MCP_READONLY", None)
    if eval_enabled and not readonly:
        env["FLAME_MCP_ENABLE_EVAL"] = "1"
    else:
        env.pop("FLAME_MCP_ENABLE_EVAL", None)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "flame_mcp"], env=env, cwd=str(REPO_ROOT)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def step_a_identity(session: ClientSession, results: dict[str, Any]) -> None:
    version_result = await session.call_tool("flame_version_info", {})
    version_env = _structured(version_result, tool="flame_version_info", results=results)
    version_result_value = _result_of(version_env)
    bridge_info = version_result_value.get("bridge")
    bridge_reachable = (
        bool(bridge_info.get("reachable")) if isinstance(bridge_info, dict) else False
    )
    record(
        results,
        "a_flame_version_info",
        version_env.get("ok") is True,
        env=version_env,
        bridge_reachable=bridge_reachable,
    )

    catalog_result = await session.call_tool("flame_catalog", {})
    catalog_env = _structured(catalog_result, tool="flame_catalog", results=results)
    op_names = {op["name"] for op in _result_of(catalog_env).get("operations", [])}
    record(
        results,
        "a_flame_catalog",
        catalog_env.get("ok") is True
        and P1_OP_NAMES <= op_names
        and op_names == REGISTERED_OP_NAMES,
        env=catalog_env,
        opNames=sorted(op_names),
        missingP1Ops=sorted(P1_OP_NAMES - op_names),
        unexpected=sorted(op_names - REGISTERED_OP_NAMES),
        missingFromCatalog=sorted(REGISTERED_OP_NAMES - op_names),
    )

    # the listing names each selector kind (a plain string) only; the fields and example come
    # with a one-op description (flame_catalog(op=...)) and, once, in flame_context's
    # selectorVocabulary
    listed = next(
        (
            op
            for op in _result_of(catalog_env).get("operations", [])
            if op.get("name") == "segment.info"
        ),
        {},
    )
    segment_result = await session.call_tool("flame_catalog", {"op": "segment.info"})
    segment_env = _structured(segment_result, tool="flame_catalog", results=results)
    segment_info = _result_of(segment_env).get("operation", {})
    accepts = segment_info.get("accepts", [])
    segment_accept = accepts[0] if accepts else {}
    record(
        results,
        "a_catalog_selector_fields",
        segment_accept.get("kind") == "segment"
        and listed.get("accepts") == ["segment"]
        and "description" not in listed
        and "thread" not in listed
        and isinstance(segment_accept.get("fields"), dict)
        and set(segment_accept["fields"]) == SEGMENT_SELECTOR_FIELDS
        and isinstance(segment_accept.get("example"), dict),
        segmentInfoAccepts=accepts,
    )


LIBRARY_LIST_PAGE: Final[int] = 50
LIBRARY_LIST_MAX_PAGES: Final[int] = 20


async def step_c_list_libraries(
    session: ClientSession, lib_name: str, results: dict[str, Any]
) -> None:
    """library.list pages (offset) until the library made for this run turns up or `truncated` is
    False: the probe project keeps every library earlier runs left, so the newest one is not
    necessarily on page 1. `total` must agree with what the pages showed."""
    pages: list[dict[str, Any]] = []
    names: list[str] = []
    all_ok = True
    last_env: dict[str, Any] = {}
    offset = 0
    for _ in range(LIBRARY_LIST_MAX_PAGES):
        result = await session.call_tool(
            "flame_do",
            {"op": "library.list", "args": {"limit": LIBRARY_LIST_PAGE, "offset": offset}},
        )
        last_env = _structured(result, tool="flame_do", results=results)
        page = _result_of(last_env)
        items = page.get("items", [])
        all_ok = all_ok and last_env.get("ok") is True
        pages.append(
            {
                "offset": offset,
                "shown": len(items),
                "total": page.get("total"),
                "truncated": page.get("truncated"),
            }
        )
        names.extend(item["name"] for item in items)
        if last_env.get("ok") is not True or not items or not page.get("truncated"):
            break
        if lib_name in names:
            break
        offset += len(items)
    total = pages[-1]["total"]
    exhausted = pages[-1]["truncated"] is not True
    consistent = (
        isinstance(total, int)
        and len(names) <= total
        and all(p["total"] == total for p in pages)
        and (not exhausted or len(names) == total)
    )
    record(
        results,
        "c_list_libraries",
        all_ok and lib_name in names and consistent,
        pages=pages,
        collected=len(names),
        total=total,
        env=last_env,
    )


async def step_c_reads(
    session: ClientSession, run_dir: Path, lib: dict[str, Any], results: dict[str, Any]
) -> None:
    lib_name = lib["libName"]

    info_result = await session.call_tool("flame_do", {"op": "project.info"})
    info_env = _structured(info_result, tool="flame_do", results=results)
    record(results, "c_project_info", info_env.get("ok") is True, env=info_env)

    await step_c_list_libraries(session, lib_name, results)

    children_result = await session.call_tool(
        "flame_do",
        {
            "op": "desktop.list_children",
            "args": {"target": {"kind": "path", "path": [lib_name]}, "limit": 50},
        },
    )
    children_env = _structured(children_result, tool="flame_do", results=results)
    r1_present = any(item["name"] == "R1" for item in _result_of(children_env).get("items", []))
    record(
        results,
        "c_list_children_library",
        children_env.get("ok") is True and r1_present,
        env=children_env,
    )

    truncated_result = await session.call_tool(
        "flame_do",
        {
            "op": "desktop.list_children",
            "args": {"target": {"kind": "path", "path": [lib_name, "R1"]}, "limit": 2},
        },
    )
    truncated_env = _structured(truncated_result, tool="flame_do", results=results)
    truncated_result_value = _result_of(truncated_env)
    truncated_ok = (
        truncated_env.get("ok") is True
        and truncated_result_value.get("total") == 3
        and truncated_result_value.get("shown") == 2
        and truncated_result_value.get("truncated") is True
    )
    record(results, "c_list_children_truncated", truncated_ok, env=truncated_env)

    await step_c_list_children_paging(session, lib_name, results)

    clip_wtid = lib["clipWtids"][0]
    wiretap_children_result = await session.call_tool(
        "flame_do",
        {
            "op": "desktop.list_children",
            "args": {"target": {"kind": "wiretap", "id": clip_wtid}, "limit": 50},
        },
    )
    wiretap_children_env = _structured(wiretap_children_result, tool="flame_do", results=results)
    # A clip is not a container: recorded either way -- this only records
    # what comes back, it asserts no specific shape.
    record(
        results,
        "c_list_children_of_a_clip",
        True,
        env=wiretap_children_env,
        note="observational: a clip has no .children",
    )

    selection_result = await session.call_tool(
        "flame_do", {"op": "desktop.selection", "args": {"limit": 50}}
    )
    selection_env = _structured(selection_result, tool="flame_do", results=results)
    record(results, "c_selection", selection_env.get("ok") is True, env=selection_env)


async def _list_children(
    session: ClientSession, path: list[str], results: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    result = await session.call_tool(
        "flame_do",
        {
            "op": "desktop.list_children",
            "args": {"target": {"kind": "path", "path": path}, "limit": 2, **extra},
        },
    )
    return _structured(result, tool="flame_do", results=results)


async def step_c_list_children_paging(
    session: ClientSession, lib_name: str, results: dict[str, Any]
) -> None:
    """Offset gives the 2nd page (R1 holds 3 clips, limit 2), and
    `types` filters (total counts the filtered items)."""
    reel_path = [lib_name, "R1"]
    page1_env = await _list_children(session, reel_path, results, offset=0)
    page2_env = await _list_children(session, reel_path, results, offset=2)
    page1 = _result_of(page1_env)
    page2 = _result_of(page2_env)
    names1 = [item["name"] for item in page1.get("items", [])]
    names2 = [item["name"] for item in page2.get("items", [])]
    offset_ok = (
        page1_env.get("ok") is True
        and page2_env.get("ok") is True
        and (page1.get("total"), page1.get("offset"), page1.get("shown"), page1.get("truncated"))
        == (3, 0, 2, True)
        and (page2.get("total"), page2.get("offset"), page2.get("shown"), page2.get("truncated"))
        == (3, 2, 1, False)
        and len(set(names1) | set(names2)) == 3
    )
    record(
        results,
        "c_list_children_offset",
        offset_ok,
        page1Names=names1,
        page2Names=names2,
        page1=page1_env,
        page2=page2_env,
    )

    reels_in_lib_env = await _list_children(session, [lib_name], results, types=["PyReel"])
    reels_in_lib = _result_of(reels_in_lib_env)
    clips_in_reel_env = await _list_children(session, reel_path, results, types=["PyClip"])
    clips_in_reel = _result_of(clips_in_reel_env)
    reels_in_reel_env = await _list_children(session, reel_path, results, types=["PyReel"])
    reels_in_reel = _result_of(reels_in_reel_env)
    types_ok = (
        reels_in_lib_env.get("ok") is True
        and reels_in_lib.get("total", 0) >= 1
        and all(item.get("type") == "PyReel" for item in reels_in_lib.get("items", []))
        and any(item.get("name") == "R1" for item in reels_in_lib.get("items", []))
        and clips_in_reel_env.get("ok") is True
        and clips_in_reel.get("total") == 3
        and all(item.get("type") == "PyClip" for item in clips_in_reel.get("items", []))
        and reels_in_reel_env.get("ok") is True
        and reels_in_reel.get("total") == 0
        and reels_in_reel.get("items") == []
    )
    record(
        results,
        "c_list_children_types",
        types_ok,
        reelsInLibrary=reels_in_lib_env,
        clipsInReel=clips_in_reel_env,
        reelsInReel=reels_in_reel_env,
    )


def _segment_gap_check(items: Any) -> tuple[bool, bool, bool]:
    """(has_gap, has_media, consistent). A Gap item: type "Gap" and isGap
    True; a media item: type "Video Segment" and isGap False. `consistent`
    also requires `isGap == (type == "Gap")` for every item, and head / tail
    to be an int or the string "infinite"."""
    if not isinstance(items, list):
        return False, False, False
    has_gap = any(i.get("type") == "Gap" and i.get("isGap") is True for i in items)
    has_media = any(i.get("type") == "Video Segment" and i.get("isGap") is False for i in items)

    def head_tail_ok(value: Any) -> bool:
        return (isinstance(value, int) and not isinstance(value, bool)) or value == "infinite"

    consistent = all(
        isinstance(i.get("type"), str)
        and i.get("isGap") == (i.get("type") == "Gap")
        and head_tail_ok(i.get("head"))
        and head_tail_ok(i.get("tail"))
        for i in items
    )
    return has_gap, has_media, consistent


async def _list_segments_env(
    session: ClientSession, seq_wtid: str | None, results: dict[str, Any]
) -> dict[str, Any]:
    result = await session.call_tool(
        "flame_do",
        {
            "op": "timeline.list_segments",
            "args": {
                "target": {"kind": "wiretap", "id": seq_wtid},
                "version": 0,
                "track": 0,
                "limit": 50,
            },
        },
    )
    return _structured(result, tool="flame_do", results=results)


async def step_c_gap_and_media(
    session: ClientSession, run_dir: Path, results: dict[str, Any]
) -> None:
    """list_segments reports `type` / `isGap` so Gap and media can be
    told apart. Runs against a Desktop sequence named U5S* that holds a Gap
    (the probe project's U5S sequences do); recorded as skipped when none
    can be found."""
    seq_wtid = find_gap_sequence(run_dir, results)
    if seq_wtid is None:
        record(
            results,
            "c_list_segments_gap_and_media",
            True,
            skipped=True,
            reason=f"no Desktop sequence named {GAP_SEQUENCE_PREFIX}* with a Gap found",
            scan=results.get("_gap_sequence_scan"),
        )
        return
    env = await _list_segments_env(session, seq_wtid, results)
    items = _result_of(env).get("items")
    has_gap, has_media, consistent = _segment_gap_check(items)
    record(
        results,
        "c_list_segments_gap_and_media",
        env.get("ok") is True and has_gap and has_media and consistent,
        sequenceWiretapId=seq_wtid,
        hasGap=has_gap,
        hasMedia=has_media,
        consistent=consistent,
        items=items,
    )


async def step_c_timeline(
    session: ClientSession, run_dir: Path, seq_wtid: str | None, results: dict[str, Any]
) -> None:
    seq_result = await session.call_tool(
        "flame_do",
        {"op": "timeline.info", "args": {"target": {"kind": "wiretap", "id": seq_wtid}}},
    )
    seq_env = _structured(seq_result, tool="flame_do", results=results)
    record(results, "c_sequence_info", seq_env.get("ok") is True, env=seq_env)

    segments_result = await session.call_tool(
        "flame_do",
        {
            "op": "timeline.list_segments",
            "args": {
                "target": {"kind": "wiretap", "id": seq_wtid},
                "version": 0,
                "track": 0,
                "limit": 50,
            },
        },
    )
    segments_env = _structured(segments_result, tool="flame_do", results=results)
    record(results, "c_list_segments", segments_env.get("ok") is True, env=segments_env)
    await step_c_gap_and_media(session, run_dir, results)

    segment_info_result = await session.call_tool(
        "flame_do",
        {
            "op": "segment.info",
            "args": {
                "target": {
                    "kind": "segment",
                    "parent": {"kind": "wiretap", "id": seq_wtid},
                    "version": 0,
                    "track": 0,
                    "index": 0,
                }
            },
        },
    )
    segment_info_env = _structured(segment_info_result, tool="flame_do", results=results)
    record(results, "c_segment_info", segment_info_env.get("ok") is True, env=segment_info_env)
    attributes = _result_of(segment_info_env).get("attributes")
    record(
        results,
        "c_segment_info_attributes",
        segment_info_env.get("ok") is True
        and isinstance(attributes, dict)
        and isinstance(attributes.get("name"), str),
        attributes=attributes,
    )

    segments_items = _result_of(segments_env).get("items")
    segments_time_shape_ok = (
        isinstance(segments_items, list)
        and len(segments_items) > 0
        and all(
            isinstance(item, dict)
            and all(_is_time_field(item.get(field)) for field in _SEGMENT_TIME_FIELDS)
            for item in segments_items
        )
    )
    segment_info_value = _result_of(segment_info_env)
    segment_info_time_shape_ok = all(
        _is_time_field(segment_info_value.get(field)) for field in _SEGMENT_TIME_FIELDS
    )
    record(
        results,
        "c_timecode_shape",
        (
            segments_env.get("ok") is True
            and segment_info_env.get("ok") is True
            and segments_time_shape_ok
            and segment_info_time_shape_ok
        ),
        segmentsItems=segments_items,
        segmentInfoValue=segment_info_value,
    )


async def step_c_flame_context(
    session: ClientSession, lib: dict[str, Any], results: dict[str, Any]
) -> None:
    """The 4th tool. Static knowledge (undoContract must mention both
    `revert` and `undoSteps`) plus the live
    snapshot (`libraries` includes the library step b just created,
    `currentTab` a string) and the same rider shape flame_do carries."""
    context_result = await session.call_tool("flame_context", {})
    context_env = _structured(context_result, tool="flame_context", results=results)
    value = _result_of(context_env)
    undo_contract = value.get("undoContract")
    libraries = value.get("libraries")
    record(
        results,
        "c_flame_context",
        (
            context_env.get("ok") is True
            and isinstance(undo_contract, str)
            and "revert" in undo_contract
            and "undoSteps" in undo_contract
            and isinstance(libraries, list)
            and lib["libName"] in libraries
            and isinstance(value.get("currentTab"), str)
            and isinstance(context_env.get("context"), dict)
        ),
        env=context_env,
    )

    vocabulary = value.get("selectorVocabulary")
    entries = vocabulary if isinstance(vocabulary, list) else []
    segment_entry = next((e for e in entries if e.get("kind") == "segment"), {})
    record(
        results,
        "c_selector_vocabulary_fields",
        len(entries) > 0
        and all(
            isinstance(e.get("fields"), dict) and isinstance(e.get("example"), dict)
            for e in entries
        )
        and set(segment_entry.get("fields", {})) == SEGMENT_SELECTOR_FIELDS,
        selectorVocabulary=vocabulary,
    )


def _read_name_by_wiretap(run_dir: Path, wiretap_id: str) -> transport.BridgeResult:
    code = "\n".join(
        [
            V_PRELUDE,
            f"obj = flame.find_by_wiretap_node_id({wiretap_id!r})",
            "_result = _v(obj.name)",
        ]
    )
    return _tx(code, run_dir, label="readback_name")


async def _rename_via_flame_do(
    session: ClientSession, target_sel: dict[str, Any], new_name: str, results: dict[str, Any]
) -> dict[str, Any]:
    result = await session.call_tool(
        "flame_do", {"op": "desktop.rename", "args": {"target": target_sel, "name": new_name}}
    )
    return _structured(result, tool="flame_do", results=results)


async def _revert_via_flame_do(
    session: ClientSession, revert: Any, results: dict[str, Any]
) -> dict[str, Any] | None:
    """Call the rename's own `result.revert` (an op+args) back through
    flame_do -- a set_value() rename is never pushed onto
    Flame's native Undo history, so `execute_shortcut('Undo')` must not be
    used here (it would silently revert an unrelated prior operation
    instead). Returns None when there was no `revert` to call (e.g.
    wiretapId could not be obtained)."""
    if not isinstance(revert, dict):
        return None
    result = await session.call_tool("flame_do", revert)
    return _structured(result, tool="flame_do", results=results)


async def step_d_rename_and_revert(
    session: ClientSession,
    run_dir: Path,
    lib: dict[str, Any],
    results: dict[str, Any],
) -> None:
    lib_name = lib["libName"]
    clip_wtid = lib["clipWtids"][0]

    before_br = _read_name_by_wiretap(run_dir, clip_wtid) if clip_wtid else None
    before_name = before_br.result if before_br and before_br.ok else None

    rename_env = await _rename_via_flame_do(
        session,
        {"kind": "path", "path": [lib_name, "R1", str(before_name)]},
        "P1O_renamed",
        results,
    )
    rename_value = _result_of(rename_env)
    revert = rename_value.get("revert")
    record(
        results,
        "d_rename_via_path",
        (
            rename_env.get("ok") is True
            and rename_value.get("after") == "P1O_renamed"
            # Ctrl+Z does not undo it, so undoSteps must always be 0 --
            # without the revert (a re-call through wiretap) there is no way back.
            and rename_env.get("undoSteps") == 0
            and revert is not None
        ),
        env=rename_env,
        beforeName=before_name,
    )

    record(
        results,
        "d_revert_op_is_desktop_rename",
        isinstance(revert, dict) and revert.get("op") == "desktop.rename",
        revert=revert,
    )

    revert_env = await _revert_via_flame_do(session, revert, results)
    # Check through a transport read that the name is back to `before` (confirm it with a
    # read-back on another path, not only from flame_do's response).
    after_revert_br = _read_name_by_wiretap(run_dir, clip_wtid) if clip_wtid else None
    reverted = (
        revert_env is not None
        and revert_env.get("ok") is True
        and after_revert_br is not None
        and after_revert_br.ok
        and after_revert_br.result == before_name
    )
    record(
        results,
        "d_revert_via_path",
        bool(reverted),
        revertEnv=revert_env,
        nameAfterRevert=after_revert_br.result if after_revert_br else None,
        expectedName=before_name,
    )

    reel_wtid = lib["reelWtid"]
    before_reel_br = _read_name_by_wiretap(run_dir, reel_wtid) if reel_wtid else None
    before_reel_name = before_reel_br.result if before_reel_br and before_reel_br.ok else None

    rename_wt_env = await _rename_via_flame_do(
        session, {"kind": "wiretap", "id": reel_wtid}, "R1_renamed", results
    )
    rename_wt_value = _result_of(rename_wt_env)
    revert_wt = rename_wt_value.get("revert")
    record(
        results,
        "d_rename_via_wiretap",
        (
            rename_wt_env.get("ok") is True
            and rename_wt_value.get("after") == "R1_renamed"
            and rename_wt_env.get("undoSteps") == 0
            and revert_wt is not None
        ),
        env=rename_wt_env,
    )

    revert_wt_env = await _revert_via_flame_do(session, revert_wt, results)
    after_revert_wt_br = _read_name_by_wiretap(run_dir, reel_wtid) if reel_wtid else None
    reel_reverted = (
        revert_wt_env is not None
        and revert_wt_env.get("ok") is True
        and after_revert_wt_br is not None
        and after_revert_wt_br.ok
        and after_revert_wt_br.result == before_reel_name
    )
    record(
        results,
        "d_revert_via_wiretap",
        bool(reel_reverted),
        revertEnv=revert_wt_env,
        nameAfterRevert=after_revert_wt_br.result if after_revert_wt_br else None,
        expectedName=before_reel_name,
    )


async def step_e_abnormal(session: ClientSession, results: dict[str, Any]) -> None:
    unknown_wiretap_result = await session.call_tool(
        "flame_do",
        {
            "op": "desktop.list_children",
            "args": {"target": {"kind": "wiretap", "id": "nope-not-a-real-id"}, "limit": 50},
        },
    )
    env = _structured(unknown_wiretap_result, tool="flame_do", results=results)
    record(
        results,
        "e_unknown_wiretap_is_selector_empty",
        env.get("errorCode") == "SELECTOR_EMPTY",
        env=env,
        note="records whether Flame returns None or raises for an unknown id",
    )

    unknown_path_result = await session.call_tool(
        "flame_do",
        {
            "op": "desktop.list_children",
            "args": {"target": {"kind": "path", "path": ["NoSuchLibrary"]}, "limit": 50},
        },
    )
    env = _structured(unknown_path_result, tool="flame_do", results=results)
    record(
        results,
        "e_unknown_path_is_selector_empty",
        env.get("errorCode") == "SELECTOR_EMPTY",
        env=env,
    )

    # Only trust "selection matched 0" when the rider independently confirms
    # 0 selected.
    #
    # An abnormal-path check must never use a write op to probe: calling
    # desktop.rename with target=selection would rename whatever is actually
    # selected. Read the selection count first with a read op, and only probe
    # (with another read op -- desktop.list_children on the same selector
    # kind resolves through the identical _resolve_selection() SELECTOR_EMPTY
    # path rename would) once the count is confirmed to be 0.
    selection_check_result = await session.call_tool(
        "flame_do", {"op": "desktop.selection", "args": {"limit": 1}}
    )
    selection_check_env = _structured(selection_check_result, tool="flame_do", results=results)
    selected_count = (selection_check_env.get("context") or {}).get("selected")
    if selection_check_env.get("ok") is not True or selected_count != 0:
        record(
            results,
            "e_empty_selection_is_selector_empty",
            True,
            skipped=True,
            reason="selection is not confirmed empty; skipped to avoid mutating a real object",
            selectionCheckEnv=selection_check_env,
            selectedCount=selected_count,
        )
    else:
        empty_selection_result = await session.call_tool(
            "flame_do",
            {
                "op": "desktop.list_children",
                "args": {"target": {"kind": "selection"}, "limit": 50},
            },
        )
        env = _structured(empty_selection_result, tool="flame_do", results=results)
        rider_selected = (env.get("context") or {}).get("selected")
        record(
            results,
            "e_empty_selection_is_selector_empty",
            env.get("errorCode") == "SELECTOR_EMPTY" and rider_selected == 0,
            env=env,
            riderSelected=rider_selected,
        )

    bad_shape_result = await session.call_tool(
        "flame_do", {"op": "desktop.rename", "args": {"target": "not-an-object", "name": "x"}}
    )
    env = _structured(bad_shape_result, tool="flame_do", results=results)
    record(results, "e_bad_shape_is_invalid_args", env.get("errorCode") == "INVALID_ARGS", env=env)

    unaccepted_kind_result = await session.call_tool(
        "flame_do",
        {
            "op": "desktop.list_children",
            "args": {
                "target": {
                    "kind": "segment",
                    "parent": {"kind": "selection"},
                    "version": 0,
                    "track": 0,
                    "index": 0,
                },
                "limit": 50,
            },
        },
    )
    env = _structured(unaccepted_kind_result, tool="flame_do", results=results)
    record(
        results,
        "e_unaccepted_kind_is_invalid_args",
        env.get("errorCode") == "INVALID_ARGS",
        env=env,
    )


async def step_f_readonly(run_dir: Path, results: dict[str, Any]) -> None:
    async with mcp_session(run_dir, readonly=True) as session:
        listed = await session.list_tools()
        do_tool = next(t for t in listed.tools if t.name == "flame_do")
        enum_names = set(do_tool.input_schema["properties"]["op"]["enum"])
        record(
            results,
            "f_tool_enum_omits_write_ops",
            not (WRITE_OP_NAMES & enum_names)
            and enum_names == REGISTERED_OP_NAMES - WRITE_OP_NAMES,
            enumNames=sorted(enum_names),
            writeOps=sorted(WRITE_OP_NAMES),
        )

        catalog_result = await session.call_tool("flame_catalog", {})
        catalog_env = _structured(catalog_result, tool="flame_catalog", results=results)
        catalog_names = {op["name"] for op in _result_of(catalog_env).get("operations", [])}
        record(
            results,
            "f_catalog_omits_write_ops",
            not (WRITE_OP_NAMES & catalog_names)
            and catalog_names == REGISTERED_OP_NAMES - WRITE_OP_NAMES,
            catalogNames=sorted(catalog_names),
            writeOps=sorted(WRITE_OP_NAMES),
        )

        rename_result = await session.call_tool(
            "flame_do",
            {"op": "desktop.rename", "args": {"target": {"kind": "selection"}, "name": "x"}},
        )
        rename_env = _structured(rename_result, tool="flame_do", results=results)
        record(
            results,
            "f_rename_is_policy_denied",
            rename_env.get("errorCode") == "POLICY_DENIED",
            env=rename_env,
        )

        read_result = await session.call_tool("flame_do", {"op": "project.info"})
        read_env = _structured(read_result, tool="flame_do", results=results)
        record(results, "f_read_op_still_works", read_env.get("ok") is True, env=read_env)


async def async_main(run_dir: Path, results: dict[str, Any]) -> None:
    ts = time.strftime("%y%m%d_%H%M%S")

    async with mcp_session(run_dir) as session:
        await step_a_identity(session, results)

        lib = prepare_library_and_reel(run_dir, ts, results)
        desk = prepare_desktop_sequence(run_dir, ts, results, lib["clipWtids"][0])
        overwrite_clips_onto_sequence(run_dir, desk["seqWtid"], lib["clipWtids"], results)

        await step_c_reads(session, run_dir, lib, results)
        await step_c_timeline(session, run_dir, desk["seqWtid"], results)
        await step_c_flame_context(session, lib, results)
        await step_d_rename_and_revert(session, run_dir, lib, results)
        await step_e_abnormal(session, results)

        results["_lib"] = lib
        results["_desk"] = desk

    await step_f_readonly(run_dir, results)

    # h_* checks are cross-cutting over every envelope any of the 4 tools
    # returned across all of a-f (tracked in results["_envelopes"] by
    # _structured), so they only make sense once every step has run.
    check_envelope_shapes(results)
    check_rider(results)
    check_logs_and_crash(results)

    cleanup(
        run_dir,
        results.get("_lib", {}).get("libWtid"),
        results.get("_desk", {}).get("deskReelWtid"),
        results,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"ok": True, "steps": []}
    try:
        anyio.run(async_main, run_dir, results)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()

    ts = time.strftime("%y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"p1_mcp_e2e_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Result: {out_path}")
    print(json.dumps(results, indent=2, default=str))
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
