#!/usr/bin/env python3
"""MCP end-to-end check of the generic attribute ops and the selectors.

Checks: The generic attribute ops on libraries, reels, clips, segments, Batch groups and nodes, the
revert of each write, the refusal of bad values, and the `batch`, `node` and `action_node`
selectors.

Also covers the `batch` / `node` / `action_node` selectors (run with the `.venv` Python;
`scripts/run_e2e.sh p2_1_attr` starts Flame).

Same shape as scripts/p1_mcp_e2e.py: a real MCP stdio client drives
`python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport.exec`
is used only as scaffolding (create the test objects, read a value back
INDEPENDENTLY of the op under test, clean up). Everything happens in the probe
project `flame_mcp_probe`.

Every stage is wrapped so one failure does not hide the others, and the results
JSON is rewritten after every recorded step
(`~/Library/Application Support/flame-mcp/p2/p2_1_attr_e2e_<ts>.json`), so a
crash mid-run still leaves everything up to that point. `flame.exit()` runs in a
`finally`.

Stages
  a   flame_version_info / flame_catalog: the catalog equals the registry, the 10 ops under test
      with the right effect/undo, selector vocabulary has batch / node / action_node
  b   prepare (transport): Library + Reel + clip; Desktop Reel + Sequence with
      segments; two Batch groups A (non-current) and B (current). A: Comp, Exposure,
      Blur (blur1), Blur (blur2, the rename subject); B: Blur (blur1), Mux. The only name
      both groups share is blur1 (stage f); everything stage c renames exists in A only
  c   for each target: get_attributes(include_values) -> set_attributes ->
      get_attributes == new value AND an independent transport read == new value ->
      the returned `revert` called as-is through flame_do -> both reads == before
        Library (name) / Desktop Reel (name) / Clip (name, colour_label if the
        attribute lists candidates) / Segment (shot_name + comment in one call) /
        Batch (name) / Node (Comp.blend_type from its `values`, Exposure.exposure_red,
        and three node renames of blur2: (a) to a unique name -> the revert
        restores it by the post-write name; (b) to a name the CURRENT group B holds ->
        Flame refuses, ok=false, explained, unchanged; (c) to a sibling's name in the same
        group -> refused before any write. Stages e and f do not depend on these)
  d   Segment dynamic_* : set -> ok=false (SILENT_NOOP expected), value unchanged, partial
  e   Comp.blend_type 'NoSuchMode' -> INVALID_ARGS, value unchanged (both reads);
      Exposure.exposure_red just above its range -> rejected; unknown key -> rejected;
      a rejected key means no other key of the same call is written
  f   non-current Batch: batch.list says A is not current (before and after every
      write), node ops on A leave B's same-named node untouched, current unchanged
  g   path(root='desktop'): Reel Group -> Reel reaches (get_attributes + list_children),
      a Batch group is reachable and revertable through desktop.set_attributes;
      observation of Desktop.children / ReelGroup.children shapes (no shape is asserted)
  h   abnormal: unknown batch / node -> SELECTOR_EMPTY, wrong selector kind -> INVALID_ARGS,
      empty values -> INVALID_ARGS
  i   READONLY subprocess: the 5 write ops are not offered, set_attributes -> POLICY_DENIED
  j   cross-cutting checks over every envelope (shape / rider / logs+crash)
  k   cleanup: delete ONLY the Desktop reel (flame.delete on a Desktop reel is measured safe).
      The Library and the Batch groups are left under their P21_ prefixes: flame.delete is
      not called on a PyLibrary or a PyBatch here, to stay on the safe side;
      p2_4_clean_probe_project.py removes them. What was deleted / kept is recorded in
      results["cleanup"]. Then flame.exit()
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import e2e_guard  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    MEDIA_DIR,
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
    WRITE_OP_NAMES,
    _br_dict,
    _result_of,
    _structured,
    _tx,
    check_envelope_shapes,
    check_logs_and_crash,
    check_rider,
    mcp_session,
)

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
NEW_READ_OPS: Final[frozenset[str]] = frozenset(
    {
        "desktop.get_attributes",
        "segment.get_attributes",
        "batch.list",
        "batch.get_attributes",
        "node.list",
        "node.get_attributes",
    }
)
NEW_WRITE_OPS: Final[frozenset[str]] = frozenset(
    {
        "desktop.set_attributes",
        "segment.set_attributes",
        "batch.set_attributes",
        "node.set_attributes",
    }
)
ALL_WRITE_OPS: Final[frozenset[str]] = WRITE_OP_NAMES
NEW_KINDS: Final[frozenset[str]] = frozenset({"batch", "node", "action_node"})

Results = dict[str, Any]

# Type-name unwrap for the scaffolding code sent through transport. Never call
# `.get_value()` directly there: what is a PyAttribute is not what one expects
# (PySegment.type is a plain str, PyDesktop.current_batch_group IS a PyAttribute),
# and p1's hasattr-based prelude would be fooled by a PyFlameObject's catch-all
# __getattr__.
V_PRELUDE: Final[str] = (
    "def _v(x):\n    return x.get_value() if type(x).__name__ == 'PyAttribute' else x"
)


def _txv(code: str, run_dir: Path, **kwargs: Any) -> Any:
    """`transport.exec` on the main thread with the `_v` prelude in front."""
    return _tx(V_PRELUDE + "\n" + code, run_dir, **kwargs)


class Ctx:
    """Everything a stage needs (the MCP session, the transport run dir, ids)."""

    def __init__(self, session: ClientSession, run_dir: Path, results: Results, ts: str) -> None:
        self.session = session
        self.run_dir = run_dir
        self.results = results
        self.ts = ts
        self.ids: dict[str, Any] = {}


def save(results: Results) -> None:
    path = results.get("_out_path")
    if isinstance(path, Path):
        path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


def rec(results: Results, name: str, ok: bool, **extra: Any) -> None:
    results["steps"].append({"name": name, "ok": ok, **extra})
    if not ok:
        results["ok"] = False
    save(results)


async def do(ctx: Ctx, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"op": op}
    if args is not None:
        payload["args"] = args
    result = await ctx.session.call_tool("flame_do", payload)
    return _structured(result, tool="flame_do", results=ctx.results)


def value_of(env: dict[str, Any], *keys: str) -> Any:
    cur: Any = env.get("result")
    for key in keys:
        cur = cur.get(key) if isinstance(cur, dict) else None
    return cur


def attr_value(env: dict[str, Any], key: str) -> Any:
    entry = value_of(env, "attributes", key)
    return entry.get("value") if isinstance(entry, dict) else None


def tx_read(ctx: Ctx, locate: str, key: str) -> Any:
    """Independent read: `locate` is transport code that binds `obj`."""
    code = "\n".join(
        [
            locate,
            f"_x = _v(getattr(obj, {key!r}))",
            "if isinstance(_x, (tuple, list)):",
            "    _x = list(_x)",
            "_ok = (str, int, float, bool, list, type(None))",
            "_result = _x if isinstance(_x, _ok) else repr(_x)",
        ]
    )
    br = _txv(code, ctx.run_dir, label=f"read_{key}")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


def locate_wiretap(wid: str) -> str:
    return f"obj = flame.find_by_wiretap_node_id({wid!r})"


def locate_batch(uid: str) -> str:
    return "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            f"obj = [g for g in _ws.desktop.batch_groups if _v(g.uid) == {uid!r}][0]",
        ]
    )


def locate_node(uid: str, name: str) -> str:
    return "\n".join(
        [
            locate_batch(uid).replace("obj = ", "_bg = "),
            f"obj = [n for n in _bg.nodes if _v(n.name) == {name!r}][0]",
        ]
    )


def locate_segment(seq_wid: str, index: int) -> str:
    return "\n".join(
        [
            f"_seq = flame.find_by_wiretap_node_id({seq_wid!r})",
            f"obj = list(_seq.versions[0].tracks[0].segments)[{index}]",
        ]
    )


def number_like(current: Any, n: int) -> int | float:
    """`n` as the same kind of number the attribute holds: an integer attribute takes an
    integer only (op refuses 123.0), a float attribute is written with a float."""
    return n if isinstance(current, int) and not isinstance(current, bool) else float(n)


def same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= 1e-6
    return bool(a == b)


async def stage_a(ctx: Ctx) -> None:
    version = _structured(
        await ctx.session.call_tool("flame_version_info", {}),
        tool="flame_version_info",
        results=ctx.results,
    )
    rec(ctx.results, "a_flame_version_info", version.get("ok") is True, env=version)

    catalog = _structured(
        await ctx.session.call_tool("flame_catalog", {}), tool="flame_catalog", results=ctx.results
    )
    ops = {op["name"]: op for op in _result_of(catalog).get("operations", [])}
    rec(
        ctx.results,
        "a_catalog_equals_registry_and_has_the_new_ops",
        catalog.get("ok") is True
        and set(ops) == REGISTERED_OP_NAMES
        and (NEW_READ_OPS | NEW_WRITE_OPS) <= set(ops),
        opNames=sorted(ops),
    )

    described: dict[str, Any] = {}
    for name in sorted(NEW_WRITE_OPS):
        env = _structured(
            await ctx.session.call_tool("flame_catalog", {"op": name}),
            tool="flame_catalog",
            results=ctx.results,
        )
        described[name] = _result_of(env).get("operation", {})
    rec(
        ctx.results,
        "a_set_attributes_are_reversible_value",
        all(
            d.get("effect") == "reversible" and d.get("undo") == "value" for d in described.values()
        ),
        effects={n: (d.get("effect"), d.get("undo")) for n, d in described.items()},
    )

    context = _structured(
        await ctx.session.call_tool("flame_context", {}),
        tool="flame_context",
        results=ctx.results,
    )
    vocab = {v["kind"]: v for v in _result_of(context).get("selectorVocabulary", [])}
    node_accepts = [a["kind"] for a in described["node.set_attributes"].get("accepts", [])]
    rec(
        ctx.results,
        "a_selector_vocabulary_has_new_kinds",
        NEW_KINDS <= set(vocab)
        and set(vocab["node"]["fields"]) == {"batch", "name", "pos"}
        and node_accepts == ["node"]
        and "root" in vocab["path"]["fields"],
        kinds=sorted(vocab),
        nodeFields=vocab.get("node", {}).get("fields"),
    )


# stage b: prepare (transport)
def prepare(ctx: Ctx) -> None:
    ts, ids = ctx.ts, ctx.ids
    lib_name = f"P21_LIB_{ts}"
    br = _txv(
        "\n".join(
            [
                "ws = flame.projects.current_project.current_workspace",
                f"lib = ws.create_library({lib_name!r})",
                "reel = lib.create_reel('R1')",
                f"clips = flame.import_clips({str(MEDIA_DIR / 'clip1.mov')!r}, reel)",
                "_result = {",
                "    'lib': lib.get_wiretap_node_id(),",
                "    'reel': reel.get_wiretap_node_id(),",
                "    'clip': clips[0].get_wiretap_node_id(),",
                "    'clipName': _v(clips[0].name),",
                "}",
            ]
        ),
        ctx.run_dir,
        timeout_s=40.0,
        label="p21_prepare_library",
    )
    rec(ctx.results, "b_library_reel_clip", br.ok, resp=_br_dict(br))
    if br.ok and isinstance(br.result, dict):
        ids.update(
            libName=lib_name,
            libWtid=br.result["lib"],
            libReelWtid=br.result["reel"],
            clipWtid=br.result["clip"],
            clipName=br.result["clipName"],
        )

    desk_reel_name = f"P21_DESK_{ts}"
    seq_name = f"P21_SEQ_{ts}"
    clip_paths = [str(MEDIA_DIR / n) for n in ("clip1.mov", "clip2.mov")]
    br = _txv(
        "\n".join(
            [
                "desktop = flame.projects.current_project.current_workspace.desktop",
                "groups = list(desktop.reel_groups)",
                f"rg = groups[0] if groups else desktop.create_reel_group('P21_RG_{ts}')",
                f"desk_reel = rg.create_reel({desk_reel_name!r})",
                f"_clips = [flame.import_clips(_p, desk_reel)[0] for _p in {clip_paths!r}]",
                "_depth = _clips[0].bit_depth",
                f"seq = desk_reel.create_sequence(name={seq_name!r}, bit_depth=_depth)",
                "_cursor = 1",
                "for _c in _clips:",
                "    seq.overwrite(_c, flame.PyTime(_cursor), seq.versions[0].tracks[0])",
                "    _cursor += 10",
                "_result = {",
                "    'rgName': _v(rg.name),",
                "    'rgWtid': rg.get_wiretap_node_id(),",
                "    'deskReelWtid': desk_reel.get_wiretap_node_id(),",
                "    'seqWtid': seq.get_wiretap_node_id(),",
                "    'segments': [",
                "        [_v(s.type), _v(s.name)]",
                "        for s in seq.versions[0].tracks[0].segments",
                "    ],",
                "}",
            ]
        ),
        ctx.run_dir,
        timeout_s=60.0,
        label="p21_prepare_desktop",
    )
    rec(ctx.results, "b_desktop_reel_and_sequence", br.ok, resp=_br_dict(br))
    if br.ok and isinstance(br.result, dict):
        ids.update(
            rgName=br.result["rgName"],
            rgWtid=br.result["rgWtid"],
            deskReelName=desk_reel_name,
            deskReelWtid=br.result["deskReelWtid"],
            seqWtid=br.result["seqWtid"],
            segmentsBefore=br.result["segments"],
        )

    name_a, name_b = f"P21_BG_A_{ts}", f"P21_BG_B_{ts}"
    br = _txv(
        "\n".join(
            [
                "desktop = flame.projects.current_project.current_workspace.desktop",
                f"a = desktop.create_batch_group({name_a!r}, nb_reels=1, nb_shelf_reels=1)",
                "a_comp = a.create_node('Comp')",
                "a_expo = a.create_node('Exposure')",
                "a_blur = a.create_node('Blur')",
                "a_ren = a.create_node('Blur')",
                f"b = desktop.create_batch_group({name_b!r}, nb_reels=1, nb_shelf_reels=1)",
                "b_blur = b.create_node('Blur')",
                "b_mux = b.create_node('Mux')",
                "_result = {",
                "    'aUid': _v(a.uid), 'bUid': _v(b.uid),",
                "    'aComp': _v(a_comp.name), 'aExpo': _v(a_expo.name),",
                "    'aBlur': _v(a_blur.name), 'aRen': _v(a_ren.name),",
                "    'bBlur': _v(b_blur.name), 'bMux': _v(b_mux.name),",
                "    'currentUid': _v(_v(desktop.current_batch_group).uid),",
                "}",
            ]
        ),
        ctx.run_dir,
        timeout_s=40.0,
        label="p21_prepare_batch",
    )
    rec(ctx.results, "b_batch_groups_and_nodes", br.ok, resp=_br_dict(br))
    if br.ok and isinstance(br.result, dict):
        r = br.result
        ids.update(
            batchAName=name_a,
            batchBName=name_b,
            aUid=r["aUid"],
            bUid=r["bUid"],
            aComp=r["aComp"],
            aExpo=r["aExpo"],
            aBlur=r["aBlur"],
            aRen=r["aRen"],
            bBlur=r["bBlur"],
            bMux=r["bMux"],
        )
        # node names must not collide across the two groups except aBlur == bBlur (stage f,
        # same name in both groups); stage c renames aRen, which exists in A only, and
        # Flame checks a name against the CURRENT group B
        a_names = {r["aComp"], r["aExpo"], r["aBlur"], r["aRen"]}
        b_names = {r["bBlur"], r["bMux"]}
        rec(
            ctx.results,
            "b_node_names_do_not_overlap_except_the_blur",
            len(a_names) == 4 and a_names & b_names == {r["aBlur"]} and r["aBlur"] == r["bBlur"],
            aNames=sorted(a_names),
            bNames=sorted(b_names),
        )
        rec(
            ctx.results,
            "b_group_A_is_not_current",
            r["currentUid"] == r["bUid"] and r["aUid"] != r["bUid"],
            currentUid=r["currentUid"],
        )


# stage c: the round trip
async def roundtrip(
    ctx: Ctx,
    label: str,
    prefix: str,
    target: dict[str, Any],
    values: dict[str, Any],
    locate: str,
    *,
    relocate: Callable[[dict[str, Any]], str] | None = None,
    expect_before: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """get -> set -> get == new == independent read -> revert via flame_do -> before.
    `relocate(revert_target)` builds the locate code for the target after a rename
    (its address changes); `locate` is used when it is None."""
    keys = list(values)
    got = await do(
        ctx, f"{prefix}.get_attributes", {"target": target, "keys": keys, "include_values": True}
    )
    before = {k: attr_value(got, k) for k in keys}
    rec(
        ctx.results,
        f"{label}_1_get",
        got.get("ok") is True
        and all("error" not in (value_of(got, "attributes", k) or {}) for k in keys),
        before=before,
        values={k: value_of(got, "attributes", k, "values") for k in keys},
        env=got,
    )

    setenv = await do(ctx, f"{prefix}.set_attributes", {"target": target, "values": values})
    result = setenv.get("result") if isinstance(setenv.get("result"), dict) else {}
    revert = result.get("revert")
    rec(
        ctx.results,
        f"{label}_2_set",
        setenv.get("ok") is True
        and setenv.get("undoSteps") == 0
        and setenv.get("partial") is None
        and result.get("applied") == keys
        and result.get("failed") == {}
        and all(same(result.get("after", {}).get(k), v) for k, v in values.items())
        and (expect_before is None or result.get("before") == expect_before)
        and isinstance(revert, dict)
        and revert.get("op") == f"{prefix}.set_attributes",
        env=setenv,
    )
    if not isinstance(revert, dict):
        return None

    new_target = revert["args"]["target"]  # the post-write address
    if relocate is not None:
        where = relocate(new_target)
    elif new_target.get("kind") == "wiretap":
        where = locate_wiretap(new_target["id"])  # a rename can change the wiretap id
    else:
        where = locate
    after_get = await do(ctx, f"{prefix}.get_attributes", {"target": new_target, "keys": keys})
    after_indep = {k: tx_read(ctx, where, k) for k in keys}
    rec(
        ctx.results,
        f"{label}_3_read_back_matches",
        after_get.get("ok") is True
        and all(same(attr_value(after_get, k), v) for k, v in values.items())
        and all(same(after_indep[k], v) for k, v in values.items()),
        viaOp={k: attr_value(after_get, k) for k in keys},
        viaTransport=after_indep,
        revertTarget=new_target,
        getEnv=after_get,
    )

    back = await do(ctx, revert["op"], revert["args"])
    back_get = await do(ctx, f"{prefix}.get_attributes", {"target": target, "keys": keys})
    back_where = locate  # after the revert the original address is valid again
    back_indep = {k: tx_read(ctx, back_where, k) for k in keys}
    rec(
        ctx.results,
        f"{label}_4_revert_restores_before",
        back.get("ok") is True
        and all(same(attr_value(back_get, k), before[k]) for k in keys)
        and all(same(back_indep[k], before[k]) for k in keys),
        revertEnv=back,
        viaOp={k: attr_value(back_get, k) for k in keys},
        viaTransport=back_indep,
        before=before,
        getEnv=back_get,
    )
    return result


async def guarded(ctx: Ctx, name: str, stage: Callable[[Ctx], Awaitable[None]]) -> None:
    try:
        await stage(ctx)
    except BaseException:
        rec(ctx.results, f"{name}_stage_crashed", False, traceback=traceback.format_exc())


async def stage_c_library(ctx: Ctx) -> None:
    ids = ctx.ids
    await roundtrip(
        ctx,
        "c_library_name",
        "desktop",
        {"kind": "wiretap", "id": ids["libWtid"]},
        {"name": f"{ids['libName']}_attr"},
        locate_wiretap(ids["libWtid"]),
        expect_before={"name": ids["libName"]},
    )


async def stage_c_reel(ctx: Ctx) -> None:
    ids = ctx.ids
    await roundtrip(
        ctx,
        "c_desktop_reel_name",
        "desktop",
        {"kind": "wiretap", "id": ids["deskReelWtid"]},
        {"name": f"{ids['deskReelName']}_attr"},
        locate_wiretap(ids["deskReelWtid"]),
        expect_before={"name": ids["deskReelName"]},
    )


async def stage_c_clip(ctx: Ctx) -> None:
    ids = ctx.ids
    target = {"kind": "wiretap", "id": ids["clipWtid"]}
    await roundtrip(
        ctx,
        "c_clip_name",
        "desktop",
        target,
        {"name": f"{ids['clipName']}_attr"},
        locate_wiretap(ids["clipWtid"]),
        expect_before={"name": ids["clipName"]},
    )
    probe = await do(
        ctx,
        "desktop.get_attributes",
        {"target": target, "keys": ["colour_label"], "include_values": True},
    )
    entry = value_of(probe, "attributes", "colour_label")
    candidates = entry.get("values") if isinstance(entry, dict) else None
    current = entry.get("value") if isinstance(entry, dict) else None
    pick = next(
        (c for c in candidates or [] if isinstance(c, str) and c != current),
        None,
    )
    if pick is not None:
        await roundtrip(
            ctx,
            "c_clip_colour_label",
            "desktop",
            target,
            {"colour_label": pick},
            locate_wiretap(ids["clipWtid"]),
        )
    else:
        rec(
            ctx.results,
            "c_clip_colour_label_skipped",
            True,
            note="colour_label is not an attribute with candidate values on this clip",
            entry=entry,
            env=probe,
        )


def first_media_index(ctx: Ctx, items: list[dict[str, Any]]) -> int | None:
    for item in items:
        if item.get("isGap") is False:
            return int(item["index"])
    return None


async def segment_target(ctx: Ctx) -> tuple[dict[str, Any], int] | None:
    ids = ctx.ids
    listing = await do(
        ctx,
        "timeline.list_segments",
        {"target": {"kind": "wiretap", "id": ids["seqWtid"]}, "version": 0, "track": 0},
    )
    index = first_media_index(ctx, _result_of(listing).get("items", []))
    rec(ctx.results, "c_segment_found_a_media_segment", index is not None, env=listing)
    if index is None:
        return None
    selector = {
        "kind": "segment",
        "parent": {"kind": "wiretap", "id": ids["seqWtid"]},
        "version": 0,
        "track": 0,
        "index": index,
    }
    return selector, index


async def stage_c_segment(ctx: Ctx) -> None:
    ids = ctx.ids
    found = await segment_target(ctx)
    if found is None:
        return
    selector, index = found
    ids["segmentSelector"], ids["segmentIndex"] = selector, index
    await roundtrip(
        ctx,
        "c_segment_shot_name_and_comment",
        "segment",
        selector,
        {"shot_name": f"SH_P21_{ctx.ts}", "comment": f"comment {ctx.ts}"},
        locate_segment(ids["seqWtid"], index),
    )


async def stage_c_batch(ctx: Ctx) -> None:
    ids = ctx.ids
    await roundtrip(
        ctx,
        "c_batch_name",
        "batch",
        {"kind": "batch", "uid": ids["aUid"]},
        {"name": f"{ids['batchAName']}_attr"},
        locate_batch(ids["aUid"]),
        expect_before={"name": ids["batchAName"]},
    )


def node_selector(ids: dict[str, Any], name: str, group: str = "a") -> dict[str, Any]:
    uid = ids["aUid"] if group == "a" else ids["bUid"]
    return {"kind": "node", "batch": {"kind": "batch", "uid": uid}, "name": name}


async def stage_c_nodes(ctx: Ctx) -> None:
    ids = ctx.ids
    comp = node_selector(ids, ids["aComp"])
    probe = await do(
        ctx,
        "node.get_attributes",
        {"target": comp, "keys": ["blend_type"], "include_values": True},
    )
    entry = value_of(probe, "attributes", "blend_type") or {}
    candidates = entry.get("values")
    current = entry.get("value")
    ids["compBlendBefore"], ids["compBlendCandidates"] = current, candidates
    pick = next((c for c in candidates or [] if isinstance(c, str) and c != current), None)
    rec(
        ctx.results,
        "c_comp_blend_type_has_candidates",
        pick is not None,
        entry=entry,
        env=probe,
    )
    if pick is not None:
        await roundtrip(
            ctx,
            "c_node_comp_blend_type",
            "node",
            comp,
            {"blend_type": pick},
            locate_node(ids["aUid"], ids["aComp"]),
            expect_before={"blend_type": current},
        )

    expo = node_selector(ids, ids["aExpo"])
    expo_probe = await do(
        ctx,
        "node.get_attributes",
        {"target": expo, "keys": ["exposure_red"], "include_values": True},
    )
    expo_entry = value_of(expo_probe, "attributes", "exposure_red") or {}
    rng = expo_entry.get("values")
    ids["expoRange"] = rng
    rec(
        ctx.results,
        "c_exposure_red_range_is_known",
        isinstance(rng, list) and len(rng) == 2,
        entry=expo_entry,
        env=expo_probe,
    )
    await roundtrip(
        ctx,
        "c_node_exposure_exposure_red",
        "node",
        expo,
        {"exposure_red": 0.5},
        locate_node(ids["aUid"], ids["aExpo"]),
    )

    await stage_c_node_renames(ctx)


async def stage_c_node_renames(ctx: Ctx) -> None:
    """Node names. All three use aRen, a node that exists in A only, so nothing
    here changes the names the later stages (e, f) rely on."""
    ids = ctx.ids
    ren = node_selector(ids, ids["aRen"])
    where = locate_node(ids["aUid"], ids["aRen"])

    # (a) a name that exists nowhere -> ok, revert restores it (by the post-write name)
    unique = f"{ids['aRen']}_p21"
    await roundtrip(
        ctx,
        "c_node_rename_unique_and_revert",
        "node",
        ren,
        {"name": unique},
        where,
        relocate=lambda revert_target: locate_node(ids["aUid"], revert_target["name"]),
        expect_before={"name": ids["aRen"]},
    )
    name_now = tx_read(ctx, where, "name")
    rec(
        ctx.results,
        "c_node_rename_a_left_the_node_under_its_old_name",
        name_now == ids["aRen"],
        nameViaTransport=name_now,
    )

    # (b) a name the CURRENT group (B) holds -> Flame refuses: ok=false, explained, unchanged
    env = await do(ctx, "node.set_attributes", {"target": ren, "values": {"name": ids["bMux"]}})
    reason = (value_of(env, "failed") or {}).get("name", "")
    unchanged = tx_read(ctx, where, "name")
    if env.get("ok") is True:  # Flame did not refuse after all: put it back
        revert = value_of(env, "revert")
        if isinstance(revert, dict):
            await do(ctx, revert["op"], revert["args"])
    rec(
        ctx.results,
        "c_node_rename_to_a_name_in_the_current_group_is_refused_and_explained",
        env.get("ok") is False
        and env.get("errorCode") == "FLAME_API_ERROR"
        and "name is already being used" in reason
        and "CURRENT Batch group" in reason
        and unchanged == ids["aRen"],
        env=env,
        reason=reason,
        nameViaTransport=unchanged,
    )

    # (c) the name of another node of the SAME group -> refused before anything is written
    pos_before = tx_read(ctx, where, "pos_x")
    env = await do(
        ctx,
        "node.set_attributes",
        {"target": ren, "values": {"pos_x": number_like(pos_before, 77), "name": ids["aExpo"]}},
    )
    reason = (value_of(env, "failed") or {}).get("name", "")
    rec(
        ctx.results,
        "c_node_rename_to_a_sibling_name_is_refused_before_any_write",
        env.get("ok") is False
        and env.get("errorCode") == "INVALID_ARGS"
        and "already named" in reason
        and env.get("partial") == {"applied": 0, "failed": 1}
        and tx_read(ctx, where, "name") == ids["aRen"]
        and same(tx_read(ctx, where, "pos_x"), pos_before),
        env=env,
        reason=reason,
        posBefore=pos_before,
    )


async def stage_d_segment_dynamic(ctx: Ctx) -> None:
    ids = ctx.ids
    selector = ids.get("segmentSelector")
    if selector is None:
        rec(ctx.results, "d_segment_dynamic_skipped", False, note="no segment selector (stage c)")
        return
    listing = await do(ctx, "segment.get_attributes", {"target": selector, "include_values": True})
    attrs = value_of(listing, "attributes") or {}
    dynamic = {k: v for k, v in attrs.items() if k.startswith("dynamic_") and "value" in v}
    rec(
        ctx.results,
        "d_segment_lists_dynamic_attributes",
        listing.get("ok") is True,
        dynamicKeys=sorted(dynamic),
        allKeys=sorted(attrs),
        env=listing,
    )
    outcomes: dict[str, Any] = {}
    for key, entry in sorted(dynamic.items()):
        current = entry["value"]
        if isinstance(current, bool):
            new: Any = not current
        elif isinstance(current, (int, float)):
            new = current + 1
        elif isinstance(current, str):
            new = current + "_x"
        else:
            outcomes[key] = {"skipped": f"unsupported type {type(current).__name__}"}
            continue
        env = await do(ctx, "segment.set_attributes", {"target": selector, "values": {key: new}})
        after = tx_read(ctx, locate_segment(ids["seqWtid"], ids["segmentIndex"]), key)
        outcomes[key] = {
            "ok": env.get("ok"),
            "errorCode": env.get("errorCode"),
            "partial": env.get("partial"),
            "failed": value_of(env, "failed"),
            "before": current,
            "requested": new,
            "afterViaTransport": after,
            "env": env,
        }
    tried = [o for o in outcomes.values() if "ok" in o]
    rec(
        ctx.results,
        "d_segment_dynamic_write_does_not_pass_silently",
        bool(tried)
        and all(
            o["ok"] is False
            and o["errorCode"] in ("SILENT_NOOP", "INVALID_ARGS")
            and same(o["afterViaTransport"], o["before"])
            for o in tried
        ),
        outcomes=outcomes,
        silentNoopKeys=sorted(
            k for k, o in outcomes.items() if o.get("errorCode") == "SILENT_NOOP"
        ),
        note="SILENT_NOOP is the expected code (measured on a real Flame: the write leaves "
        "the value unchanged and raises no error); INVALID_ARGS means the key is not "
        "writable at all -- both keep the value; the JSON says which",
    )


async def stage_e_rejections(ctx: Ctx) -> None:
    ids = ctx.ids
    comp = node_selector(ids, ids["aComp"])
    where = locate_node(ids["aUid"], ids["aComp"])
    before = tx_read(ctx, where, "blend_type")
    env = await do(
        ctx, "node.set_attributes", {"target": comp, "values": {"blend_type": "NoSuchMode"}}
    )
    after_env = await do(ctx, "node.get_attributes", {"target": comp, "keys": ["blend_type"]})
    after_op = attr_value(after_env, "blend_type")
    after_tx = tx_read(ctx, where, "blend_type")
    rec(
        ctx.results,
        "e_comp_blend_type_NoSuchMode_is_rejected_and_unchanged",
        env.get("ok") is False
        and env.get("errorCode") == "INVALID_ARGS"
        and "blend_type" in (value_of(env, "failed") or {})
        and env.get("partial") == {"applied": 0, "failed": 1}
        and after_op == before
        and after_tx == before,
        env=env,
        before=before,
        afterViaOp=after_op,
        afterViaTransport=after_tx,
        getEnv=after_env,
    )

    # an integer attribute takes integers only: 123.0 / 1.5 are refused up front (the op does
    # not leave the type to Flame). Skipped -- and the type recorded -- when pos_x is a float here.
    pos_where = locate_node(ids["aUid"], ids["aComp"])
    pos_now = tx_read(ctx, pos_where, "pos_x")
    if isinstance(pos_now, int) and not isinstance(pos_now, bool):
        outcomes: dict[str, Any] = {}
        for bad in (123.0, 1.5):
            bad_env = await do(
                ctx, "node.set_attributes", {"target": comp, "values": {"pos_x": bad}}
            )
            outcomes[repr(bad)] = {
                "ok": bad_env.get("ok"),
                "errorCode": bad_env.get("errorCode"),
                "reason": (value_of(bad_env, "failed") or {}).get("pos_x"),
                "env": bad_env,
            }
        pos_after = tx_read(ctx, pos_where, "pos_x")
        rec(
            ctx.results,
            "e_float_for_an_integer_attribute_is_refused_before_writing",
            all(
                o["ok"] is False
                and o["errorCode"] == "INVALID_ARGS"
                and "integer" in (o["reason"] or "")
                for o in outcomes.values()
            )
            and same(pos_after, pos_now),
            posBefore=pos_now,
            posAfterViaTransport=pos_after,
            outcomes=outcomes,
        )
    else:
        rec(
            ctx.results,
            "e_float_for_an_integer_attribute_skipped",
            True,
            note="pos_x is not an integer attribute on this node",
            posX=pos_now,
        )

    rng = ids.get("expoRange")
    expo = node_selector(ids, ids["aExpo"])
    if isinstance(rng, list) and len(rng) == 2:
        env = await do(
            ctx, "node.set_attributes", {"target": expo, "values": {"exposure_red": rng[1] + 1}}
        )
        unchanged = tx_read(ctx, locate_node(ids["aUid"], ids["aExpo"]), "exposure_red")
        rec(
            ctx.results,
            "e_exposure_above_range_is_rejected",
            env.get("ok") is False
            and env.get("errorCode") == "INVALID_ARGS"
            and same(unchanged, 0.0),
            env=env,
            range=rng,
            valueViaTransport=unchanged,
        )
    else:
        rec(ctx.results, "e_exposure_above_range_skipped", False, range=rng)

    env = await do(
        ctx,
        "node.set_attributes",
        {"target": comp, "values": {"bypass": True, "no_such_attribute": 1}},
    )
    bypass_after = tx_read(ctx, where, "bypass")
    rec(
        ctx.results,
        "e_a_rejected_key_writes_nothing_else",
        env.get("ok") is False
        and env.get("errorCode") == "INVALID_ARGS"
        and bypass_after is False
        and list(value_of(env, "failed") or {}) == ["no_such_attribute"],
        env=env,
        bypassViaTransport=bypass_after,
    )


# stage f: non-current groups
BATCH_LIST_PAGE: Final[int] = 100
"""batch.list reads no node counts by default, so listing a page does not load the non-current
groups on it (Flame loads a group the first time its nodes are read; that load is what made a
500-item page time out). The page size is kept modest all the same."""


async def do_read(
    ctx: Ctx, op: str, args: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """`do` for a read-only op: a TIMEOUT is read once more (nothing is written). Returns the
    envelope and what the first read did: {firstMs, firstTimedOut, retryMs}."""
    started = time.monotonic()
    env = await do(ctx, op, args)
    info: dict[str, Any] = {
        "firstMs": round((time.monotonic() - started) * 1000),
        "firstTimedOut": env.get("errorCode") == "TIMEOUT",
        "retryMs": None,
    }
    if info["firstTimedOut"]:
        info["firstTimeoutPhase"] = env.get("timeoutPhase")
        started = time.monotonic()
        env = await do(ctx, op, args)
        info["retryMs"] = round((time.monotonic() - started) * 1000)
    return env, info


async def batch_states(ctx: Ctx, label: str) -> tuple[dict[str, bool], dict[str, Any]]:
    """{uid: isCurrent} for EVERY Batch group: batch.list pages (BATCH_LIST_PAGE each) until
    `truncated` is False -- the probe project holds far more than the default 50 groups. Each
    page is read once more after a TIMEOUT; how long each read took, and which timed out, is
    recorded as an observation."""
    states: dict[str, bool] = {}
    pages: list[dict[str, Any]] = []
    offset = 0
    while True:
        listing, info = await do_read(
            ctx, "batch.list", {"limit": BATCH_LIST_PAGE, "offset": offset}
        )
        result = _result_of(listing)
        items = result.get("items", [])
        pages.append(
            {
                "offset": offset,
                "ok": listing.get("ok"),
                "shown": len(items),
                "total": result.get("total"),
                "truncated": result.get("truncated"),
                "currentError": result.get("currentError"),
                **info,
            }
        )
        if listing.get("ok") is not True or not items:
            break
        states.update({i["uid"]: i["isCurrent"] for i in items})
        if not result.get("truncated"):
            break
        offset += len(items)
    total = pages[-1].get("total")
    rec(
        ctx.results,
        f"f_observation_batch_list_reads_{label}",
        True,
        kind="observation",
        note="observational: how long each batch.list page took, and whether it timed out",
        pageSize=BATCH_LIST_PAGE,
        pages=pages,
    )
    return states, {"pages": pages, "collected": len(states), "total": total}


async def stage_f_non_current(ctx: Ctx) -> None:
    ids = ctx.ids
    states, states_meta = await batch_states(ctx, "before")
    rec(
        ctx.results,
        "f_batch_list_shows_A_not_current_and_B_current",
        states.get(ids["aUid"]) is False
        and states.get(ids["bUid"]) is True
        and states_meta["collected"] == states_meta["total"]
        and sum(1 for is_current in states.values() if is_current) == 1,
        aIsCurrent=states.get(ids["aUid"]),
        bIsCurrent=states.get(ids["bUid"]),
        currentCount=sum(1 for is_current in states.values() if is_current),
        **states_meta,
    )

    b_blur_before = tx_read(ctx, locate_node(ids["bUid"], ids["bBlur"]), "pos_x")
    node_list = await do(ctx, "node.list", {"target": {"kind": "batch", "uid": ids["aUid"]}})
    names = [i["name"] for i in _result_of(node_list).get("items", [])]
    rec(
        ctx.results,
        "f_node_list_of_the_non_current_group",
        node_list.get("ok") is True and ids["aBlur"] in names and ids["aExpo"] in names,
        names=names,
        env=node_list,
    )

    a_blur = node_selector(ids, ids["aBlur"], "a")
    b_blur = node_selector(ids, ids["bBlur"], "b")
    same_name = ids["aBlur"] == ids["bBlur"]
    pos_cur = tx_read(ctx, locate_node(ids["aUid"], ids["aBlur"]), "pos_x")
    new_pos = number_like(pos_cur, 123)
    env = await do(ctx, "node.set_attributes", {"target": a_blur, "values": {"pos_x": new_pos}})
    a_after = tx_read(ctx, locate_node(ids["aUid"], ids["aBlur"]), "pos_x")
    b_after = tx_read(ctx, locate_node(ids["bUid"], ids["bBlur"]), "pos_x")
    b_get = await do(ctx, "node.get_attributes", {"target": b_blur, "keys": ["pos_x"]})
    b_via_op = attr_value(b_get, "pos_x")
    rec(
        ctx.results,
        "f_write_to_A_leaves_B_same_named_node_alone",
        env.get("ok") is True
        and same(a_after, new_pos)
        and same(b_after, b_blur_before)
        and same(b_via_op, b_blur_before),
        sameNodeNameInBothGroups=same_name,
        requested=new_pos,
        aBefore=pos_cur,
        aAfter=a_after,
        bBefore=b_blur_before,
        bAfter=b_after,
        setEnv=env,
        bGetEnv=b_get,
    )
    revert = value_of(env, "revert")
    if isinstance(revert, dict):
        await do(ctx, revert["op"], revert["args"])

    states_after, after_meta = await batch_states(ctx, "after")
    rec(
        ctx.results,
        "f_current_group_unchanged_by_all_the_writes",
        states_after == states,
        currentBefore=[u for u, c in states.items() if c],
        currentAfter=[u for u, c in states_after.items() if c],
        before=states_meta,
        after=after_meta,
    )


async def stage_g_desktop_path(ctx: Ctx) -> None:
    ids = ctx.ids
    reel_path = {"kind": "path", "path": [ids["rgName"], ids["deskReelName"]], "root": "desktop"}
    got = await do(ctx, "desktop.get_attributes", {"target": reel_path, "keys": ["name"]})
    rec(
        ctx.results,
        "g_path_root_desktop_reaches_reel_group_then_reel",
        got.get("ok") is True
        and value_of(got, "type") == "PyReel"
        and attr_value(got, "name") == ids["deskReelName"],
        env=got,
    )
    listing = await do(
        ctx,
        "desktop.list_children",
        {"target": {"kind": "path", "path": [ids["rgName"]], "root": "desktop"}, "limit": 50},
    )
    names = [i["name"] for i in _result_of(listing).get("items", [])]
    rec(
        ctx.results,
        "g_path_root_desktop_list_children_of_the_reel_group",
        listing.get("ok") is True and ids["deskReelName"] in names,
        names=names,
        env=listing,
    )

    batch_path = {"kind": "path", "path": [ids["batchBName"]], "root": "desktop"}
    got_batch = await do(ctx, "desktop.get_attributes", {"target": batch_path, "keys": ["name"]})
    rec(
        ctx.results,
        "g_path_root_desktop_reaches_a_batch_group",
        got_batch.get("ok") is True and value_of(got_batch, "type") == "PyBatch",
        env=got_batch,
    )
    await roundtrip(
        ctx,
        "g_desktop_set_attributes_on_a_batch_group",
        "desktop",
        batch_path,
        {"name": f"{ids['batchBName']}_via_desktop"},
        locate_batch(ids["bUid"]),
        expect_before={"name": ids["batchBName"]},
    )
    default_root = await do(
        ctx, "desktop.get_attributes", {"target": {"kind": "path", "path": [ids["rgName"]]}}
    )
    rec(
        ctx.results,
        "g_default_root_is_libraries_not_the_desktop",
        default_root.get("errorCode") == "SELECTOR_EMPTY",
        env=default_root,
    )

    observe = _txv(
        "\n".join(
            [
                "desktop = flame.projects.current_project.current_workspace.desktop",
                f"rg = flame.find_by_wiretap_node_id({ids['rgWtid']!r})",
                "def _names(x):",
                "    try:",
                "        return [_v(i.name) for i in x]",
                "    except BaseException as e:",
                "        return 'error: ' + repr(e)",
                "_result = {",
                "    'desktop.children': _names(desktop.children),",
                "    'desktop.reel_groups': _names(desktop.reel_groups),",
                "    'desktop.batch_groups': _names(desktop.batch_groups),",
                "    'reelGroup.children': _names(rg.children),",
                "    'reelGroup.reels': _names(rg.reels),",
                "}",
            ]
        ),
        ctx.run_dir,
        label="p21_observe_children",
    )
    rec(
        ctx.results,
        "g_observation_desktop_and_reel_group_children",
        True,
        note="observational (unconfirmed whether the children of the Desktop and of a reel "
        "group hold the groups / reels)",
        resp=_br_dict(observe),
    )


async def stage_h_abnormal(ctx: Ctx) -> None:
    ids = ctx.ids
    unknown_batch = await do(
        ctx, "batch.get_attributes", {"target": {"kind": "batch", "name": "NoSuchBatch_p21"}}
    )
    rec(
        ctx.results,
        "h_unknown_batch_is_selector_empty",
        unknown_batch.get("errorCode") == "SELECTOR_EMPTY",
        env=unknown_batch,
    )
    unknown_node = await do(
        ctx, "node.get_attributes", {"target": node_selector(ids, "no_such_node_p21")}
    )
    rec(
        ctx.results,
        "h_unknown_node_is_selector_empty",
        unknown_node.get("errorCode") == "SELECTOR_EMPTY",
        env=unknown_node,
    )
    wrong_kind = await do(
        ctx,
        "node.get_attributes",
        {"target": {"kind": "batch", "uid": ids["aUid"]}},
    )
    rec(
        ctx.results,
        "h_batch_selector_to_a_node_op_is_invalid_args",
        wrong_kind.get("errorCode") == "INVALID_ARGS",
        env=wrong_kind,
    )
    both = await do(
        ctx,
        "batch.get_attributes",
        {"target": {"kind": "batch", "uid": ids["aUid"], "name": ids["batchAName"]}},
    )
    rec(
        ctx.results,
        "h_batch_selector_with_uid_and_name_is_invalid_args",
        both.get("errorCode") == "INVALID_ARGS",
        env=both,
    )
    empty = await do(
        ctx, "batch.set_attributes", {"target": {"kind": "batch", "uid": ids["aUid"]}, "values": {}}
    )
    rec(
        ctx.results,
        "h_empty_values_is_rejected",
        empty.get("ok") is False and empty.get("errorCode") == "INVALID_ARGS",
        env=empty,
    )


async def stage_i_readonly(run_dir: Path, results: Results, ids: dict[str, Any]) -> None:
    async with mcp_session(run_dir, readonly=True) as session:
        listed = await session.list_tools()
        do_tool = next(t for t in listed.tools if t.name == "flame_do")
        enum_names = set(do_tool.input_schema["properties"]["op"]["enum"])
        rec(
            results,
            "i_tool_enum_omits_all_write_ops",
            not (ALL_WRITE_OPS & enum_names) and enum_names == REGISTERED_OP_NAMES - ALL_WRITE_OPS,
            enumNames=sorted(enum_names),
        )
        denied = _structured(
            await session.call_tool(
                "flame_do",
                {
                    "op": "node.set_attributes",
                    "args": {
                        "target": node_selector(ids, "x"),
                        "values": {"bypass": True},
                    },
                },
            ),
            tool="flame_do",
            results=results,
        )
        rec(
            results,
            "i_set_attributes_is_policy_denied",
            denied.get("errorCode") == "POLICY_DENIED",
            env=denied,
        )
        listing_started = time.monotonic()
        listing = _structured(
            await session.call_tool("flame_do", {"op": "batch.list"}),
            tool="flame_do",
            results=results,
        )
        listing_first_ms = round((time.monotonic() - listing_started) * 1000)
        if listing.get("errorCode") == "TIMEOUT":
            first_error = listing.get("errorCode")
            listing = _structured(
                await session.call_tool("flame_do", {"op": "batch.list"}),
                tool="flame_do",
                results=results,
            )
            rec(
                results,
                "i_observation_batch_list_retried_after_timeout",
                True,
                kind="observation",
                firstMs=listing_first_ms,
                firstErrorCode=first_error,
                retryOk=listing.get("ok"),
            )
        rec(results, "i_read_op_still_works", listing.get("ok") is True, env=listing)


# stage k: cleanup
def cleanup(run_dir: Path, ids: dict[str, Any], results: Results) -> None:
    deleted: list[str] = []
    kept = {
        "library": ids.get("libName"),
        "batchGroups": [ids.get("batchAName"), ids.get("batchBName")],
        "reason": (
            "flame.delete is not called on a PyLibrary or a PyBatch here, to stay on the safe "
            "side; p2_4_clean_probe_project.py removes them"
        ),
    }
    desk_reel = ids.get("deskReelWtid")
    if desk_reel is not None:
        br = _txv(
            "\n".join(
                [
                    f"reel = flame.find_by_wiretap_node_id({desk_reel!r})",
                    "flame.delete(reel, confirm=False)",
                    "_result = True",
                ]
            ),
            run_dir,
            label="p21_cleanup_delete_desktop_reel",
        )
        rec(results, "k_delete_desktop_reel", br.ok, resp=_br_dict(br))
        if br.ok:
            deleted.append(f"desktop reel {ids.get('deskReelName')}")
    results["cleanup"] = {"deleted": deleted, "kept": kept}
    exit_br = _txv("flame.exit()", run_dir, timeout_s=5.0, label="p21_cleanup_exit")
    # no response is expected: the socket closes as Flame exits
    rec(
        results,
        "k_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
    )


async def async_main(run_dir: Path, results: Results) -> None:
    ts = time.strftime("%y%m%d_%H%M%S")
    ids: dict[str, Any] = {}
    try:
        async with mcp_session(run_dir) as session:
            ctx = Ctx(session, run_dir, results, ts)
            ctx.ids = ids
            await guarded(ctx, "a", stage_a)
            try:
                prepare(ctx)
            except BaseException:
                rec(results, "b_prepare_crashed", False, traceback=traceback.format_exc())
            if "aUid" in ids and "libWtid" in ids:
                for name, stage in (
                    ("c_library", stage_c_library),
                    ("c_reel", stage_c_reel),
                    ("c_clip", stage_c_clip),
                    ("c_segment", stage_c_segment),
                    ("c_batch", stage_c_batch),
                    ("c_nodes", stage_c_nodes),
                    ("d", stage_d_segment_dynamic),
                    ("e", stage_e_rejections),
                    ("f", stage_f_non_current),
                    ("g", stage_g_desktop_path),
                    ("h", stage_h_abnormal),
                ):
                    await guarded(ctx, name, stage)
            else:
                rec(results, "b_prepare_incomplete_stages_c_to_h_skipped", False, ids=ids)
        if "aUid" in ids:
            try:
                await stage_i_readonly(run_dir, results, ids)
            except BaseException:
                rec(results, "i_stage_crashed", False, traceback=traceback.format_exc())
        check_envelope_shapes(results)
        check_rider(results)
        check_logs_and_crash(results)
        save(results)
    finally:
        cleanup(run_dir, ids, results)
        save(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    results: Results = {
        "ok": True,
        "steps": [],
        "_out_path": OUTPUT_DIR / f"p2_1_attr_e2e_{ts}.json",
    }
    try:
        anyio.run(async_main, run_dir, results)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()
    save(results)
    failed = [s["name"] for s in results["steps"] if not s["ok"]]
    print(f"Result: {results['_out_path']}")
    print(f"steps={len(results['steps'])} failed={len(failed)}: {failed}")
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
