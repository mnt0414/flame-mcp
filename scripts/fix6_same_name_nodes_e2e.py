#!/usr/bin/env python3
"""Real-Flame end-to-end: two Clip nodes of one clip share one display name;
the `node` selector's `pos` tells them apart, an ambiguous name ends in SELECTOR_AMBIGUOUS, node
rows carry `pos` / `sameNameCount`, `node.info` names `ambiguousSocketNames` (run with the `.venv`
Python; `scripts/run_e2e.sh fix6_same_name_nodes` starts Flame; `--dry-run` connects to nothing,
lists the calls and compiles the code).

Checks: Two Clip nodes that share one display name: the `node` selector's `pos` tells them apart, an
ambiguous name ends in `SELECTOR_AMBIGUOUS`, node rows carry `pos` and `sameNameCount`, and
`node.info` reports `ambiguousSocketNames`.

A real MCP stdio client drives `python -m flame_mcp` against a live Flame + dev bridge;
`flame_mcp.transport` is scaffolding only -- it builds the material and READS EVERY STATE BACK
INDEPENDENTLY of the ops under test (the nodes' names, positions, notes and sockets). Everything
this run makes is named `PR12E_<ts>_...` (scripts/p2_4_clean_probe_project.py knows the prefix) and
is deleted at the end by that prefix; then flame.exit(). The journal directory is this run's own
(`p3/journal_pr12e_e2e_<ts>/`). No test hook is set for any server. The first step refuses to run
anywhere but in the project `flame_mcp_probe`.

Material (transport): two Batch groups, GN (not current) and GC (made last, so the current one),
each with `clip1.mov` imported twice into its first schematic reel by `PyBatch.import_clip`. The
premise is that the two Clip nodes show ONE display name; when they do not, the run records the
raw names and stops (the conditions are not changed to suit the spec). The two
nodes are then given the positions A = (100, 100) and B = (400, 200) through the transport.

Per group (stage n: not current, stage c: current)
  a  `node.info({batch, name})` is SELECTOR_AMBIGUOUS and names both candidates with their pos.
  b  `node.info({batch, name, pos})` addresses each of the two (its pos_x / pos_y are the pos
     given; `stable` carries the pos).
  c  `node.list` rows of the two: pos as read through the transport, sameNameCount 2.
  d  `node.set_attributes` (note) through pos A changes the note of A only (read through the
     transport); `journal.revert` puts it back.
  e  `node.create` (Blur), then `node.connect` with pos A as the source: the transport shows the
     link on A only; `node.info` of the Blur names the clip's name in `ambiguousSocketNames`; a
     wrong pos on the unique Blur is SELECTOR_EMPTY.
  f  a wrong pos on the shared name is SELECTOR_EMPTY and nothing changes (notes, journal).
  h  `node.save_setup` then `node.load_setup` of the Clip node at pos A (the notes of A and B are
     changed through the transport between the two calls): ok, A gets its saved note back, B keeps
     the note it had, names and pos are as before.
  k  a second `node.connect`, the Clip at pos B into the Blur's other input while A is already
     linked: ok; result.readbackAmbiguous is recorded; the transport shows both links.
  g  `journal.revert` of the second connect, the connect and then of the create (checkpoint
     restores): the two Clip nodes still share their display name and keep their pos; the Blur is
     gone.
  i  `node.duplicate` of the Clip at pos A (only the current group allows it): the new node's name
     and pos are recorded; `journal.revert` brings the group back to two Clip nodes.
  j  one of the two same-named Clip nodes is renamed (`node.set_attributes` name, through pos), then
     `journal.revert` puts the name back: the outcome is recorded as it is (in the current group
     Flame is expected to refuse the name in use); the judged step only needs a consistent state.
  z  cross-cutting checks; everything this run made is deleted; flame.exit().
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Final

import anyio

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

import e2e_guard  # noqa: E402
from p1_mcp_e2e import (  # noqa: E402
    MEDIA_DIR,
    REGISTERED_OP_NAMES,
    RUN_DIR_DEFAULT,
    _br_dict,
    _result_of,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do,
    guarded,
    rec,
    save,
    save_stage,
    server_session,
    tx,
)
from p2_4_common import (  # noqa: E402
    flame_exit,
    journal_records,
    journal_states,
    observe,
    ops_used,
    print_observations,
)
from p3_1a_read_e2e import PRELUDE, fill, is_error, tx_val  # noqa: E402
from p3_1b_e2e_common import CLEANUP_CODE, LEFT_CODE  # noqa: E402

from flame_mcp import execution  # noqa: E402
from flame_mcp.callargs import apply_limit_default, apply_param_defaults  # noqa: E402
from flame_mcp.ops import register_all  # noqa: E402
from flame_mcp.opschema import validate_op_args  # noqa: E402
from flame_mcp.registry import Registry  # noqa: E402

Results = dict[str, Any]

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p3"
TARGET_PROJECT: Final[str] = e2e_guard.probe_project_name()
NAME_PREFIX: Final[str] = "PR12E"
SIDE_CLIP_FILES: Final[dict[str, tuple[str, ...]]] = {
    "n": ("clip2.mov", "clip2.mov"),
    "c": ("clip1.mov", "clip1.mov"),
}
"""GN gets a clip whose name GC does not hold: Flame checks a node name against the CURRENT
group, so a rename (or its revert) in GN would be refused because of GC's `clip1` nodes and say
nothing about two same-named nodes of one group."""
POS_A: Final[tuple[int, int]] = (100, 100)
POS_B: Final[tuple[int, int]] = (400, 200)
NOTE_A: Final[str] = "PR12E note A"
NOTE_CHANGED_A: Final[str] = "PR12E A changed after the save"
NOTE_KEPT_B: Final[str] = "PR12E B must not change"
RENAMED: Final[str] = "PR12E_renamed"
BLUR_NAME: Final[str] = "PR12E_blur"
BLUR_POS_A: Final[tuple[int, int]] = (1000, 100)
BLUR_POS_B: Final[tuple[int, int]] = (1000, 300)
SIDES: Final[tuple[str, ...]] = ("n", "c")
SIDE_TITLE: Final[dict[str, str]] = {"n": "GN (not current)", "c": "GC (current)"}
GOOD_STATES: Final[frozenset[str]] = frozenset(
    {"landed", "reverted", "conflict", "failed", "not_landed"}
)
MAKE_TIMEOUT_S: Final[float] = 180.0
POS_IN_TEXT: Final[re.Pattern[str]] = re.compile(r"pos=\[(-?\d+), (-?\d+)\]")

# -- the calls made through the MCP surface (one table: the run and --dry-run read the same one) --

CALLS: dict[str, tuple[str, Any]] = {}


def call_for(call_id: str, op: str) -> Any:
    def register(builder: Any) -> Any:
        CALLS[call_id] = (op, builder)
        return builder

    return register


def batch_sel(uid: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": uid}


def node_sel(uid: str, name: str, pos: tuple[int, int] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": "node", "batch": batch_sel(uid), "name": name}
    if pos is not None:
        out["pos"] = list(pos)
    return out


@call_for("a1_info_by_name_only", "node.info")
def _a1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": node_sel(ids["uid"], ids["clipName"])}


@call_for("b1_info_at_pos", "node.info")
def _b1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": node_sel(ids["uid"], ids["clipName"], tuple(ids["pos"]))}


@call_for("c1_list", "node.list")
def _c1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": batch_sel(ids["uid"]), "limit": 200}


@call_for("d1_set_note_at_pos", "node.set_attributes")
def _d1(ids: dict[str, Any]) -> dict[str, Any]:
    pos = tuple(ids.get("pos", POS_A))
    return {"target": node_sel(ids["uid"], ids["clipName"], pos), "values": {"note": NOTE_A}}


@call_for("d2_revert_the_note", "journal.revert")
def _d2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["group"]}


@call_for("e1_create_a_blur", "node.create")
def _e1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": batch_sel(ids["uid"]), "type": "Blur"}


@call_for("e2_connect_clip_at_pos_to_the_blur", "node.connect")
def _e2(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": node_sel(ids["uid"], ids["clipName"], POS_A),
        "target": node_sel(ids["uid"], ids.get("blurName", "blur1")),
    }


@call_for("e3_info_of_the_blur", "node.info")
def _e3(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": node_sel(ids["uid"], ids.get("blurName", "blur1"))}


@call_for("e4_info_of_the_blur_at_a_wrong_pos", "node.info")
def _e4(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": node_sel(ids["uid"], ids.get("blurName", "blur1"), (7, 7))}


@call_for("f1_set_note_at_a_wrong_pos", "node.set_attributes")
def _f1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": node_sel(ids["uid"], ids["clipName"], (7, 7)), "values": {"note": "x"}}


@call_for("h0_save_the_clip_setup_at_pos", "node.save_setup")
def _h0(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": node_sel(ids["uid"], ids["clipName"], POS_A),
        "path": ids["setupPath"] + "_clip",
    }


@call_for("h1_save_the_setup_at_pos", "node.save_setup")
def _h1(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": node_sel(ids["uid"], ids["setupName"], BLUR_POS_A),
        "path": ids["setupPath"],
    }


@call_for("h2_load_the_setup_at_pos", "node.load_setup")
def _h2(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": node_sel(ids["uid"], ids["setupName"], BLUR_POS_A),
        "path": ids.get("setupFile", ids["setupPath"]),
    }


@call_for("e5_connect_the_sibling_clip_to_the_blur", "node.connect")
def _e5(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": node_sel(ids["uid"], ids["clipName"], POS_B),
        "target": node_sel(ids["uid"], ids.get("blurName", "blur1")),
        "target_socket": "Matte",
    }


@call_for("g0_revert_the_second_connect", "journal.revert")
def _g0(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["connect2Group"]}


@call_for("g1_revert_the_connect", "journal.revert")
def _g1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["connectGroup"]}


@call_for("g2_revert_the_create", "journal.revert")
def _g2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["createGroup"]}


@call_for("i1_duplicate_the_clip_at_pos", "node.duplicate")
def _i1(ids: dict[str, Any]) -> dict[str, Any]:
    return {"target": node_sel(ids["uid"], ids["clipName"], POS_A)}


@call_for("i2_revert_the_duplicate", "journal.revert")
def _i2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["dupGroup"]}


@call_for("j1_rename_the_clip_at_pos_b", "node.set_attributes")
def _j1(ids: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": node_sel(ids["uid"], ids["clipName"], POS_B),
        "values": {"name": RENAMED},
    }


@call_for("j2_revert_the_rename", "journal.revert")
def _j2(ids: dict[str, Any]) -> dict[str, Any]:
    return {"group": ids["renameGroup"]}


# -- transport scaffolding (never through the op under test) ------------------------------------

PROJECT_CODE: Final[str] = "_result = _v(flame.projects.current_project.name)"

MAKE_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_made = [(_key, _d.create_batch_group(_gname), _ps) for _key, _gname, _ps in @GROUPS@]",
        "_uids = {}",
        "for _key, _bg, _paths in _made:",
        "    _rn = _v(_bg.reels[0].name)",
        "    for _p in _paths:",
        "        _bg.import_clip(_p, _rn)",
        "    _uids[_key] = _v(_bg.uid)",
        "_result = _uids",
    ]
)

NODES_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_hit = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@]",
        "_cur = _v(_d.current_batch_group)",
        "_result = None",
        "if _hit:",
        "    _bg = _hit[0]",
        "    _rows = []",
        "    for _i, _n in enumerate(list(_bg.nodes)):",
        "        _px = _v(_n.pos_x)",
        "        _py = _v(_n.pos_y)",
        "        _rows.append({",
        "            'i': _i, 'name': _v(_n.name), 'pyType': type(_n).__name__,",
        "            'type': _g(lambda _n=_n: _v(_n.type)),",
        "            'pos': [_px, _py], 'posTypes': [type(_px).__name__, type(_py).__name__],",
        "            'note': _g(lambda _n=_n: _v(_n.note)),",
        "            'sockets': _g(lambda _n=_n: {",
        "                _s: {_k: list(_c) for _k, _c in _v(_n.sockets)[_s].items()}",
        "                for _s in ('input', 'output')}),",
        "        })",
        "    _result = {",
        "        'name': _v(_bg.name), 'uid': _v(_bg.uid),",
        "        'isCurrent': _cur is not None and _v(_cur.uid) == _v(_bg.uid),",
        "        'nodes': _rows,",
        "    }",
    ]
)

SET_POS_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@][0]",
        "_nodes = list(_bg.nodes)",
        "for _i, _x, _y in @ASSIGN@:",
        "    _nodes[_i].pos_x.set_value(_x)",
        "    _nodes[_i].pos_y.set_value(_y)",
        "_result = True",
    ]
)

SET_NOTE_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@][0]",
        "_nodes = [_n for _n in _bg.nodes if _v(_n.name) == @NAME@]",
        "for _x, _y, _note in @ASSIGN@:",
        "    _hit = [_n for _n in _nodes if [_v(_n.pos_x), _v(_n.pos_y)] == [_x, _y]]",
        "    _hit[0].note.set_value(_note)",
        "_result = True",
    ]
)

MAKE_BLUR_CODE: Final[str] = "\n".join(
    [
        "_d = flame.projects.current_project.current_workspace.desktop",
        "_bg = [_x for _x in _d.batch_groups if _v(_x.uid) == @UID@][0]",
        "_made = [_bg.create_node('Blur'), _bg.create_node('Blur')]",
        "for _n, (_x, _y) in zip(_made, @POSITIONS@):",
        "    _n.name.set_value(@NAME@)",
        "    _n.pos_x.set_value(_x)",
        "    _n.pos_y.set_value(_y)",
        "_result = True",
    ]
)

TEMPLATES: Final[dict[str, str]] = {
    "project": PROJECT_CODE,
    "make": MAKE_CODE,
    "nodes": NODES_CODE,
    "set_pos": SET_POS_CODE,
    "set_note": SET_NOTE_CODE,
    "make_blur": MAKE_BLUR_CODE,
    "cleanup": CLEANUP_CODE,
    "left": LEFT_CODE,
}


async def tx_group(ctx: Ctx, uid: str) -> dict[str, Any] | None:
    got = await tx_val(ctx, fill(NODES_CODE, UID=uid), "pr12e_nodes")
    return None if is_error(got) or got is None else got


def name_of(ctx: Ctx, tail: str) -> str:
    return f"{NAME_PREFIX}_{ctx.ts}_{tail}"


def written_groups(ctx: Ctx) -> list[dict[str, str]]:
    return ctx.ids.setdefault("writeGroups", [])


async def run_call(session: Any, ctx: Ctx, call_id: str, **extra: Any) -> dict[str, Any]:
    op, build = CALLS[call_id]
    env = await do(session, ctx, op, build({**ctx.ids, **extra}))
    group = (env.get("journal") or {}).get("group")
    if isinstance(group, str):
        written_groups(ctx).append({"op": op, "group": group})
    return env


def clip_rows(group: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [r for r in (group or {}).get("nodes", []) if r.get("pyType") == "PyClipNode"]


def clip_positions(group: dict[str, Any] | None) -> list[tuple[Any, Any]]:
    return sorted((tuple(r["pos"]) for r in clip_rows(group)), key=repr)  # type: ignore[misc]


def note_at(group: dict[str, Any] | None, pos: tuple[int, int]) -> Any:
    rows = [r for r in clip_rows(group) if tuple(r["pos"]) == pos]
    return rows[0]["note"] if len(rows) == 1 else "<no single node>"


def raw_nodes(group: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        {k: r[k] for k in ("name", "type", "pos", "posTypes", "note", "sockets")}
        for r in (group or {}).get("nodes", [])
    ]


# -- prepare ------------------------------------------------------------------------------------


async def prepare(ctx: Ctx) -> None:
    ids = ctx.ids
    project = await tx_val(ctx, PROJECT_CODE, "pr12e_project")
    rec(
        ctx,
        "prepare_the_project_is_the_probe_project",
        project == TARGET_PROJECT,
        project=project,
    )
    if project != TARGET_PROJECT:
        return
    files = {n for names in SIDE_CLIP_FILES.values() for n in names}
    missing = [str(MEDIA_DIR / n) for n in sorted(files) if not (MEDIA_DIR / n).is_file()]
    if missing:
        rec(ctx, "prepare_material_files_exist", False, missing=missing)
        return
    groups = [
        [
            side,
            name_of(ctx, f"G{side.upper()}"),
            [str(MEDIA_DIR / n) for n in SIDE_CLIP_FILES[side]],
        ]
        for side in SIDES
    ]
    made = await tx(
        ctx,
        PRELUDE + "\n" + fill(MAKE_CODE, GROUPS=groups),
        label="pr12e_prepare",
        timeout_s=MAKE_TIMEOUT_S,
    )
    rec(ctx, "prepare_material_is_made", made.ok, resp=_br_dict(made))
    if not (made.ok and isinstance(made.result, dict)):
        return
    for side in SIDES:
        ids[f"{side}Uid"] = made.result[side]
    for side in SIDES:
        group = await tx_group(ctx, ids[f"{side}Uid"])
        clips = clip_rows(group)
        names = sorted({r["name"] for r in clips})
        observe(
            ctx,
            f"prepare_{side}_raw_nodes_as_imported",
            title=SIDE_TITLE[side],
            isCurrent=group and group["isCurrent"],
            nodes=raw_nodes(group),
        )
        shared = len(clips) == 2 and len(names) == 1
        rec(
            ctx,
            f"prepare_{side}_premise_two_clip_nodes_of_one_clip_show_one_display_name",
            shared,
            clipNodeNames=[r["name"] for r in clips],
            allNodes=raw_nodes(group),
        )
        if not shared:
            ids["premiseBroken"] = True
            return
        ids[f"{side}ClipName"] = names[0]
        ordered = sorted(clips, key=lambda r: r["i"])
        assign = [[ordered[0]["i"], *POS_A], [ordered[1]["i"], *POS_B]]
        set_ok = await tx_val(
            ctx, fill(SET_POS_CODE, UID=ids[f"{side}Uid"], ASSIGN=assign), "pr12e_set_pos"
        )
        after = await tx_group(ctx, ids[f"{side}Uid"])
        rec(
            ctx,
            f"prepare_{side}_the_two_clip_nodes_are_given_the_positions_A_and_B",
            set_ok is True
            and clip_positions(after) == sorted([POS_A, POS_B], key=repr)
            and all(r["posTypes"] == ["int", "int"] for r in clip_rows(after)),
            set=set_ok,
            nodes=raw_nodes(after),
        )
        ids[f"{side}Notes"] = {
            "A": note_at(after, POS_A),
            "B": note_at(after, POS_B),
        }
    cur = await tx_group(ctx, ids["cUid"])
    other = await tx_group(ctx, ids["nUid"])
    rec(
        ctx,
        "prepare_gc_is_the_current_group_and_gn_is_not",
        bool(cur and cur["isCurrent"]) and bool(other) and not other["isCurrent"],
        gc=cur and cur["isCurrent"],
        gn=other and other["isCurrent"],
    )


# -- the judgements -----------------------------------------------------------------------------


def positions_in(text: object) -> list[tuple[int, int]]:
    return sorted((int(x), int(y)) for x, y in POS_IN_TEXT.findall(str(text)))


async def judge_reads(ctx: Ctx, session: Any, side: str) -> None:
    ids = ctx.ids
    ambiguous = await run_call(session, ctx, "a1_info_by_name_only")
    rec(
        ctx,
        f"{side}a_info_by_name_only_is_selector_ambiguous_with_two_candidates_and_their_pos",
        ambiguous.get("ok") is False
        and ambiguous.get("errorCode") == "SELECTOR_AMBIGUOUS"
        and positions_in(ambiguous.get("error")) == sorted([POS_A, POS_B])
        and str(ambiguous.get("error")).count("type=") == 2,
        errorCode=ambiguous.get("errorCode"),
        error=ambiguous.get("error"),
        hint=ambiguous.get("hint"),
    )
    for label, pos in (("A", POS_A), ("B", POS_B)):
        env = await run_call(session, ctx, "b1_info_at_pos", pos=list(pos))
        result = _result_of(env)
        attrs = result.get("attributes") or {}
        got = [(attrs.get(k) or {}).get("value") for k in ("pos_x", "pos_y")]
        stable = result.get("stable") or {}
        rec(
            ctx,
            f"{side}b_info_with_pos_{label}_addresses_that_node",
            env.get("ok") is True
            and got == list(pos)
            and stable.get("pos") == list(pos)
            and stable.get("name") == ids["clipName"],
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            got=got,
            stable=stable,
        )
    listing = await run_call(session, ctx, "c1_list")
    items = _result_of(listing).get("items") or []
    shared = [i for i in items if i.get("name") == ids["clipName"]]
    group = await tx_group(ctx, ids["uid"])
    rec(
        ctx,
        f"{side}c_node_list_rows_of_the_two_have_pos_as_read_by_transport_and_same_name_count_2",
        listing.get("ok") is True
        and sorted((tuple(i["pos"]) for i in shared), key=repr) == sorted([POS_A, POS_B], key=repr)
        and [i["sameNameCount"] for i in shared] == [2, 2]
        and clip_positions(group) == sorted([POS_A, POS_B], key=repr),
        error=listing.get("error"),
        rows=shared,
    )


async def judge_note(ctx: Ctx, session: Any, side: str) -> None:
    ids = ctx.ids
    initial = ids[f"{side}Notes"]
    env = await run_call(session, ctx, "d1_set_note_at_pos")
    group_id = (env.get("journal") or {}).get("group")
    after = await tx_group(ctx, ids["uid"])
    rec(
        ctx,
        f"{side}d_set_attributes_through_pos_A_changes_the_note_of_A_only",
        env.get("ok") is True
        and note_at(after, POS_A) == NOTE_A
        and note_at(after, POS_B) == initial["B"],
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        noteA=note_at(after, POS_A),
        noteB=note_at(after, POS_B),
        initial=initial,
    )
    if not isinstance(group_id, str):
        rec(ctx, f"{side}d_the_write_has_a_journal_group", False, journal=env.get("journal"))
        return
    ids["group"] = group_id
    back = await run_call(session, ctx, "d2_revert_the_note")
    restored = await tx_group(ctx, ids["uid"])
    rec(
        ctx,
        f"{side}d_journal_revert_puts_the_note_of_A_back_and_B_is_untouched",
        back.get("ok") is True
        and note_at(restored, POS_A) == initial["A"]
        and note_at(restored, POS_B) == initial["B"],
        error=back.get("error"),
        errorCode=back.get("errorCode"),
        noteA=note_at(restored, POS_A),
        noteB=note_at(restored, POS_B),
    )


def blur_name_of(group: dict[str, Any] | None, before_names: list[str]) -> str | None:
    new = [r["name"] for r in (group or {}).get("nodes", []) if r["name"] not in before_names]
    return new[0] if len(new) == 1 else None


async def judge_connect(ctx: Ctx, session: Any, side: str) -> None:
    ids = ctx.ids
    before = await tx_group(ctx, ids["uid"])
    before_names = [r["name"] for r in (before or {}).get("nodes", [])]
    created = await run_call(session, ctx, "e1_create_a_blur")
    group = await tx_group(ctx, ids["uid"])
    blur = blur_name_of(group, before_names)
    node = _result_of(created).get("node") or {}
    rec(
        ctx,
        f"{side}e_node_create_makes_a_blur_whose_row_has_pos_and_same_name_count_1",
        created.get("ok") is True
        and blur is not None
        and node.get("name") == blur
        and node.get("sameNameCount") == 1
        and isinstance(node.get("pos"), list),
        error=created.get("error"),
        row=node,
        blur=blur,
    )
    create_group = (created.get("journal") or {}).get("group")
    if blur is None or not isinstance(create_group, str):
        return
    ids["blurName"] = blur
    ids["createGroup"] = create_group
    connected = await run_call(session, ctx, "e2_connect_clip_at_pos_to_the_blur")
    after = await tx_group(ctx, ids["uid"])
    rows = {tuple(r["pos"]): r for r in clip_rows(after)}
    out_a = ((rows.get(POS_A) or {}).get("sockets") or {}).get("output")
    out_b = ((rows.get(POS_B) or {}).get("sockets") or {}).get("output")
    linked_a = isinstance(out_a, dict) and any(blur in v for v in out_a.values())
    linked_b = isinstance(out_b, dict) and any(blur in v for v in out_b.values())
    rec(
        ctx,
        f"{side}e_node_connect_through_pos_A_links_the_clip_at_A_and_not_the_one_at_B",
        connected.get("ok") is True and linked_a and not linked_b,
        error=connected.get("error"),
        errorCode=connected.get("errorCode"),
        outputA=out_a,
        outputB=out_b,
    )
    ids["connectGroup"] = (connected.get("journal") or {}).get("group")
    info = await run_call(session, ctx, "e3_info_of_the_blur")
    result = _result_of(info)
    rec(
        ctx,
        f"{side}e_node_info_of_the_blur_lists_the_shared_name_in_ambiguous_socket_names",
        info.get("ok") is True
        and result.get("ambiguousSocketNames") == [ids["clipName"]]
        and any(
            ids["clipName"] in names
            for names in (result.get("sockets") or {}).get("input", {}).values()
        ),
        error=info.get("error"),
        sockets=result.get("sockets"),
        ambiguousSocketNames=result.get("ambiguousSocketNames"),
    )
    wrong = await run_call(session, ctx, "e4_info_of_the_blur_at_a_wrong_pos")
    rec(
        ctx,
        f"{side}e_a_wrong_pos_on_a_unique_name_is_selector_empty",
        wrong.get("ok") is False and wrong.get("errorCode") == "SELECTOR_EMPTY",
        errorCode=wrong.get("errorCode"),
        error=wrong.get("error"),
    )


async def judge_sibling_connect(ctx: Ctx, session: Any, side: str) -> None:
    ids = ctx.ids
    if not isinstance(ids.get("blurName"), str):
        rec(ctx, f"{side}k_there_is_a_blur_to_connect_to", False)
        return
    env = await run_call(session, ctx, "e5_connect_the_sibling_clip_to_the_blur")
    group = await tx_group(ctx, ids["uid"])
    rows = {tuple(r["pos"]): r for r in clip_rows(group)}
    outputs = {
        label: ((rows.get(pos) or {}).get("sockets") or {}).get("output")
        for label, pos in (("A", POS_A), ("B", POS_B))
    }
    linked = {
        label: isinstance(out, dict) and any(ids["blurName"] in v for v in out.values())
        for label, out in outputs.items()
    }
    result = _result_of(env)
    rec(
        ctx,
        f"{side}k_connecting_the_sibling_while_the_other_is_linked_links_B_and_keeps_A",
        env.get("ok") is True and linked == {"A": True, "B": True},
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        outputs=outputs,
        readbackAmbiguous=result.get("readbackAmbiguous"),
        alreadyLinked=result.get("alreadyLinked"),
        newLinks=result.get("newLinks"),
        linkedInputSockets=result.get("linkedInputSockets"),
        targetInputsAfter=result.get("targetInputsAfter"),
    )
    observe(
        ctx,
        f"{side}k_readback_ambiguous_of_the_second_connect",
        readbackAmbiguous=result.get("readbackAmbiguous"),
    )
    ids["connect2Group"] = (env.get("journal") or {}).get("group")


def remove_setup_files(ctx: Ctx, side: str) -> None:
    """Delete the files this run's save_setup calls wrote (named pr12e_setup_<ts>_<side>*)."""
    for leftover in OUTPUT_DIR.glob(f"pr12e_setup_{ctx.ts}_{side}*"):
        if leftover.is_file():
            leftover.unlink()


async def judge_clip_setup(ctx: Ctx, session: Any, side: str) -> None:
    """node.save_setup of the Clip node at pos A, recorded as it is (a Clip node writes several
    files; the op wants one)."""
    ids = ctx.ids
    ids["setupPath"] = str(OUTPUT_DIR / f"pr12e_setup_{ctx.ts}_{side}")
    saved = await run_call(session, ctx, "h0_save_the_clip_setup_at_pos")
    observe(
        ctx,
        f"{side}h_clip_node_save_setup_through_pos_raw",
        title=SIDE_TITLE[side],
        opOk=saved.get("ok"),
        errorCode=saved.get("errorCode"),
        error=saved.get("error"),
        path=_result_of(saved).get("path"),
        written=_result_of(saved).get("written"),
    )
    remove_setup_files(ctx, side)


async def judge_blur_setup(ctx: Ctx, session: Any, side: str) -> None:
    """Two Blur nodes of one name (made with set_value, which Flame allows only in the
    group that is not current): save_setup then load_setup through pos change the node at pos A
    only."""
    ids = ctx.ids
    if side != "n":
        observe(
            ctx,
            f"{side}h_blur_setup_skipped",
            reason="a same-name pair of Blur nodes can only be made in a group that is not current",
        )
        return
    ids["setupName"] = BLUR_NAME
    ids["setupPath"] = str(OUTPUT_DIR / f"pr12e_setup_{ctx.ts}_{side}")
    ids.pop("setupFile", None)
    made = await tx_val(
        ctx,
        fill(
            MAKE_BLUR_CODE,
            UID=ids["uid"],
            NAME=BLUR_NAME,
            POSITIONS=[list(BLUR_POS_A), list(BLUR_POS_B)],
        ),
        "pr12e_make_blur",
    )
    group = await tx_group(ctx, ids["uid"])
    blurs = [r for r in (group or {}).get("nodes", []) if r["name"] == BLUR_NAME]
    rec(
        ctx,
        f"{side}h_two_blur_nodes_of_one_name_exist",
        made is True and sorted(tuple(r["pos"]) for r in blurs) == [BLUR_POS_A, BLUR_POS_B],
        made=made,
        nodes=raw_nodes(group),
    )
    if len(blurs) != 2:
        return
    saved = await run_call(session, ctx, "h1_save_the_setup_at_pos")
    file = _result_of(saved).get("path")
    rec(
        ctx,
        f"{side}h_node_save_setup_through_pos_writes_one_file",
        saved.get("ok") is True and isinstance(file, str) and Path(file).is_file(),
        error=saved.get("error"),
        errorCode=saved.get("errorCode"),
        path=file,
    )
    if not isinstance(file, str):
        remove_setup_files(ctx, side)
        return
    ids["setupFile"] = file
    assign = [[*BLUR_POS_A, NOTE_CHANGED_A], [*BLUR_POS_B, NOTE_KEPT_B]]
    changed = await tx_val(
        ctx, fill(SET_NOTE_CODE, UID=ids["uid"], NAME=BLUR_NAME, ASSIGN=assign), "pr12e_notes"
    )
    mid = await tx_group(ctx, ids["uid"])
    loaded = await run_call(session, ctx, "h2_load_the_setup_at_pos")
    after = await tx_group(ctx, ids["uid"])
    result = _result_of(loaded)

    def note(rows_of: dict[str, Any] | None, pos: tuple[int, int]) -> Any:
        hit = [
            r
            for r in (rows_of or {}).get("nodes", [])
            if r["name"] == BLUR_NAME and tuple(r["pos"]) == pos
        ]
        return hit[0]["note"] if len(hit) == 1 else "<no single node>"

    after_blurs = [r for r in (after or {}).get("nodes", []) if r["name"] == BLUR_NAME]
    rec(
        ctx,
        f"{side}h_node_load_setup_through_pos_changes_the_node_at_A_only",
        changed is True
        and note(mid, BLUR_POS_A) == NOTE_CHANGED_A
        and loaded.get("ok") is True
        and note(after, BLUR_POS_A) == ""
        and note(after, BLUR_POS_B) == NOTE_KEPT_B
        and sorted(tuple(r["pos"]) for r in after_blurs) == [BLUR_POS_A, BLUR_POS_B]
        and "note" in (result.get("changedKeys") or []),
        error=loaded.get("error"),
        errorCode=loaded.get("errorCode"),
        changedKeys=result.get("changedKeys"),
        noteA=note(after, BLUR_POS_A),
        noteB=note(after, BLUR_POS_B),
        nodes=raw_nodes(after),
    )
    remove_setup_files(ctx, side)


async def judge_duplicate(ctx: Ctx, session: Any, side: str) -> None:
    ids = ctx.ids
    before = await tx_group(ctx, ids["uid"])
    env = await run_call(session, ctx, "i1_duplicate_the_clip_at_pos")
    after = await tx_group(ctx, ids["uid"])
    result = _result_of(env)
    observe(
        ctx,
        f"{side}i_node_duplicate_raw",
        title=SIDE_TITLE[side],
        opOk=env.get("ok"),
        errorCode=env.get("errorCode"),
        error=env.get("error"),
        node=result.get("node"),
        addedNames=result.get("addedNames"),
        nodesBefore=raw_nodes(before),
        nodesAfter=raw_nodes(after),
    )
    if side == "n":
        rec(
            ctx,
            f"{side}i_duplicate_in_a_group_that_is_not_current_changes_nothing",
            env.get("ok") is False and clip_rows(after) == clip_rows(before),
            errorCode=env.get("errorCode"),
            error=env.get("error"),
        )
        return
    node = result.get("node") or {}
    rec(
        ctx,
        f"{side}i_duplicate_of_a_shared_name_clip_node_is_ok_and_reads_the_new_node",
        env.get("ok") is True
        and len(clip_rows(after)) == len(clip_rows(before)) + 1
        and isinstance(node.get("name"), str)
        and isinstance(node.get("pos"), list)
        and node.get("sameNameCount")
        == sum(1 for r in after["nodes"] if r["name"] == node["name"]),
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        node=node,
    )
    group_id = (env.get("journal") or {}).get("group")
    if not isinstance(group_id, str):
        return
    ids["dupGroup"] = group_id
    back = await run_call(session, ctx, "i2_revert_the_duplicate")
    restored = await tx_group(ctx, ids["uid"])
    rec(
        ctx,
        f"{side}i_journal_revert_of_the_duplicate_leaves_the_two_clip_nodes_at_their_pos",
        back.get("ok") is True
        and len(clip_rows(restored)) == 2
        and clip_positions(restored) == sorted([POS_A, POS_B], key=repr),
        error=back.get("error"),
        errorCode=back.get("errorCode"),
        nodes=raw_nodes(restored),
    )


async def judge_rename(ctx: Ctx, session: Any, side: str) -> None:
    ids = ctx.ids
    env = await run_call(session, ctx, "j1_rename_the_clip_at_pos_b")
    mid = await tx_group(ctx, ids["uid"])
    rec(
        ctx,
        f"{side}j_renaming_one_of_two_same_named_clip_nodes_through_pos_works",
        env.get("ok") is True
        and sorted(r["name"] for r in clip_rows(mid)) == sorted([ids["clipName"], RENAMED]),
        error=env.get("error"),
        errorCode=env.get("errorCode"),
        nodes=raw_nodes(mid),
    )
    group_id = (env.get("journal") or {}).get("group")
    if env.get("ok") is not True or not isinstance(group_id, str):
        return
    ids["renameGroup"] = group_id
    back = await run_call(session, ctx, "j2_revert_the_rename")
    after = await tx_group(ctx, ids["uid"])
    names = sorted(r["name"] for r in clip_rows(after))
    observe(
        ctx,
        f"{side}j_revert_of_the_rename_raw",
        title=SIDE_TITLE[side],
        opOk=back.get("ok"),
        errorCode=back.get("errorCode"),
        error=back.get("error"),
        hint=back.get("hint"),
        namesAfter=names,
        nodesAfter=raw_nodes(after),
    )
    reverted = back.get("ok") is True and names == [ids["clipName"], ids["clipName"]]
    refused = (
        back.get("ok") is False
        and names == sorted([ids["clipName"], RENAMED])
        and note_at(after, POS_A) is not None
    )
    rec(
        ctx,
        f"{side}j_after_the_revert_the_state_is_consistent_(put_back_or_refused_untouched)",
        (reverted or refused) and clip_positions(after) == sorted([POS_A, POS_B], key=repr),
        reverted=reverted,
        refused=refused,
        errorCode=back.get("errorCode"),
        error=back.get("error"),
    )


async def judge_wrong_pos(ctx: Ctx, session: Any, side: str) -> None:
    ids = ctx.ids
    before = await tx_group(ctx, ids["uid"])
    records_before = len(journal_records(ctx))
    env = await run_call(session, ctx, "f1_set_note_at_a_wrong_pos")
    after = await tx_group(ctx, ids["uid"])
    rec(
        ctx,
        f"{side}f_a_wrong_pos_on_the_shared_name_is_selector_empty_and_nothing_changes",
        env.get("ok") is False
        and env.get("errorCode") == "SELECTOR_EMPTY"
        and clip_rows(after) == clip_rows(before)
        and len(journal_records(ctx)) == records_before,
        errorCode=env.get("errorCode"),
        error=env.get("error"),
        journalRecords=[records_before, len(journal_records(ctx))],
    )


async def judge_restores(ctx: Ctx, session: Any, side: str) -> None:
    ids = ctx.ids
    if not isinstance(ids.get("connectGroup"), str) or not isinstance(ids.get("createGroup"), str):
        rec(ctx, f"{side}g_there_are_two_checkpointed_writes_to_revert", False, ids=ids)
        return
    expected = sorted([POS_A, POS_B], key=repr)
    steps = [
        ("connect", "g1_revert_the_connect", False),
        ("create", "g2_revert_the_create", True),
    ]
    if isinstance(ids.get("connect2Group"), str):
        steps.insert(0, ("second_connect", "g0_revert_the_second_connect", False))
    for step, call_id, gone in steps:
        env = await run_call(session, ctx, call_id)
        group = await tx_group(ctx, ids["uid"])
        clips = clip_rows(group)
        names = sorted({r["name"] for r in clips})
        blur_there = any(r["name"] == ids["blurName"] for r in (group or {}).get("nodes", []))
        rec(
            ctx,
            f"{side}g_after_reverting_the_{step}_the_two_clip_nodes_keep_one_name_and_their_pos",
            env.get("ok") is True
            and names == [ids["clipName"]]
            and len(clips) == 2
            and clip_positions(group) == expected
            and blur_there is (not gone),
            error=env.get("error"),
            errorCode=env.get("errorCode"),
            nodes=raw_nodes(group),
        )
        if not gone:
            observe(
                ctx,
                f"{side}g_sockets_after_reverting_the_{step}",
                sockets={str(tuple(r["pos"])): r["sockets"] for r in clips},
            )


async def stage_side(ctx: Ctx, side: str) -> None:
    ids = ctx.ids
    if ids.get("premiseBroken"):
        rec(ctx, f"{side}_stage_skipped_the_premise_of_plan_82_does_not_hold", False)
        return
    ids["uid"] = ids[f"{side}Uid"]
    ids["clipName"] = ids[f"{side}ClipName"]
    ids["pos"] = list(POS_A)
    for key in (
        "blurName",
        "createGroup",
        "connectGroup",
        "connect2Group",
        "dupGroup",
        "renameGroup",
        "setupFile",
        "group",
    ):
        ids.pop(key, None)
    now = await tx_group(ctx, ids["uid"])
    observe(
        ctx,
        f"{side}0_the_target_group_when_the_stage_starts",
        title=SIDE_TITLE[side],
        isCurrent=now and now["isCurrent"],
        nodes=raw_nodes(now),
    )
    async with server_session(ctx, f"pr12e-{side}") as session:
        await judge_reads(ctx, session, side)
        await judge_note(ctx, session, side)
        await judge_clip_setup(ctx, session, side)
        await judge_connect(ctx, session, side)
        await judge_sibling_connect(ctx, session, side)
        await judge_wrong_pos(ctx, session, side)
        await judge_restores(ctx, session, side)
        await judge_duplicate(ctx, session, side)
        await judge_rename(ctx, session, side)
        await judge_blur_setup(ctx, session, side)


async def stage_n(ctx: Ctx) -> None:
    await stage_side(ctx, "n")


async def stage_c(ctx: Ctx) -> None:
    await stage_side(ctx, "c")


# -- z / driver ---------------------------------------------------------------------------------


async def stage_z(ctx: Ctx) -> None:
    used = ops_used(ctx.results)
    ctx.results["opsUsed"] = used
    ctx.results["opsNotUsed"] = sorted(REGISTERED_OP_NAMES - set(used))
    if not ctx.ids.get("premiseBroken"):
        wanted = {op for op, _build in CALLS.values()}
        rec(
            ctx,
            "z_every_op_of_this_fix_was_called",
            wanted <= set(used),
            missing=sorted(wanted - set(used)),
        )
        own = journal_states(ctx)
        rec(
            ctx,
            "z_every_group_of_this_run_is_decided_none_is_unknown",
            bool(own) and set(own.values()) <= GOOD_STATES,
            states=sorted(set(own.values())),
        )
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    prefix = f"{NAME_PREFIX}_{ctx.ts}"
    gone = await tx_val(ctx, fill(CLEANUP_CODE, P=prefix), "pr12e_cleanup", 180.0)
    left = await tx_val(ctx, fill(LEFT_CODE, P=prefix), "pr12e_left")
    ctx.results["cleanup"] = {"deleted": gone, "stillThere": left}
    rec(
        ctx,
        "z_everything_this_run_made_is_deleted",
        isinstance(gone, dict)
        and all(ok is True for rows_ in gone.values() for _n, ok in rows_)
        and left == {"reelGroups": [], "batchGroups": [], "libraries": []},
        deleted=gone,
        stillThere=left,
    )
    save(ctx.results)


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "nUid" in ctx.ids and "cUid" in ctx.ids:
            await guarded(ctx, "n", stage_n)
            await guarded(ctx, "c", stage_c)
        else:
            rec(ctx, "prepare_incomplete_the_stages_were_skipped", False, ids=sorted(ctx.ids))
        await guarded(ctx, "z", stage_z)
    finally:
        flame_exit(ctx, "pr12e_cleanup_exit")
        save_stage(ctx)
        save(ctx.results)


# -- --dry-run ----------------------------------------------------------------------------------


def placeholder_ids() -> dict[str, Any]:
    return {
        "uid": "uid-gn",
        "clipName": "clip1",
        "pos": list(POS_A),
        "group": "g_dry",
        "createGroup": "g_dry_create",
        "connectGroup": "g_dry_connect",
        "connect2Group": "g_dry_connect2",
        "dupGroup": "g_dry_dup",
        "renameGroup": "g_dry_rename",
        "setupPath": "/dry/run/pr12e_setup",
        "setupName": BLUR_NAME,
        "setupFile": str(Path(__file__).resolve()),  # only has to exist for the precheck
        "blurName": "blur1",
    }


def dry_run() -> int:
    registry = Registry()
    register_all(registry)
    failures = 0
    ids = placeholder_ids()
    print("===== calls through flame_do =====")
    for call_id, (op_name, build) in CALLS.items():
        op = registry.get(op_name)
        problems: list[str] = []
        if op is None:
            print(f"NG {call_id}: unknown op {op_name}")
            failures += 1
            continue
        validation = validate_op_args(op, build(dict(ids)))
        if validation.ok:
            checked = dict(validation.value or {})
            apply_param_defaults(op, checked)
            apply_limit_default(op, checked)
            if op.precheck is not None:
                refusal = op.precheck(checked, None)
                if refusal is not None:
                    problems.append(f"precheck refused: {refusal.message}")
            if op.thread != "server":
                try:
                    compile(execution.build_exec_code(op, checked), f"<{call_id}>", "exec")
                except SyntaxError as exc:
                    problems.append(f"compile FAILED: {exc}")
        else:
            problems.append(
                "invalid args: " + "; ".join(f"{i.path}: {i.message}" for i in validation.issues)
            )
        print(f"{'NG ' + '; '.join(problems) if problems else 'OK':3} {call_id}: {op_name}")
        failures += bool(problems)
    print("===== transport code (compiled with every @NAME@ filled) =====")
    for name, template in TEMPLATES.items():
        code = PRELUDE + "\n" + re.sub(r"@[A-Z_0-9]+@", "'x'", template)
        try:
            compile(code, f"<{name}>", "exec")
            print(f"OK  {name} ({len(code)} chars)")
        except SyntaxError as exc:
            print(f"NG  {name}: {exc}")
            failures += 1
    print(f"=== dry run: {'FAILED' if failures else 'OK'} ({failures} problem(s)) ===")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        return dry_run()
    run_dir = Path(args.run_dir)
    refused = e2e_guard.require_probe_project(run_dir)
    if refused is not None:
        return refused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%y%m%d_%H%M%S")
    journal_dir = OUTPUT_DIR / f"journal_pr12e_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"fix6_same_name_nodes_e2e_{ts}.json",
    }
    ctx = Ctx(run_dir, results, ts, journal_dir)
    try:
        anyio.run(async_main, ctx)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()
    save(results)
    failed = [s["name"] for s in results["steps"] if not s["ok"]]
    print(f"Result: {results['_out_path']}")
    print(f"steps={len(results['steps'])} failed={len(failed)}: {failed}")
    print_observations(results)
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
