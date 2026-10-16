#!/usr/bin/env python3
"""Real-Flame end-to-end: the `node` and `action` categories' ops (run with the `.venv`
Python; `scripts/run_e2e.sh p2_3b_node_action` starts Flame).

Checks: The `node` ops (`list_types`, `create`, `info`, `connect`, `disconnect`, `delete`,
`duplicate`, `save_setup`, `load_setup`) and the `action` ops (`list_nodes`, `create_node`,
`connect`, `set_attributes`), each read back independently and reverted with `journal.revert`.
`list_types` is checked against a transport read and the types the test uses, not against a count
(the count differs by Flame version).

Same shape and helpers as scripts/p2_3a_batch_e2e.py (imported from p2_2a / p1): a real MCP stdio
client drives `python -m flame_mcp` against a live Flame + dev bridge; `flame_mcp.transport` is
scaffolding only (create the groups, and READ EVERY STATE BACK INDEPENDENTLY of the ops under test
-- nodes, sockets, an Action's inner nodes and their links, attribute values). Everything happens
in the probe project `flame_mcp_probe`, in Batch groups that are NOT current, except the one
group `CUR` (created last, so it is the current one) that `node.duplicate` needs. The journal
directory is this run's own (`p2/journal_p23b_e2e_<ts>/`, never the production one). No test hook
is set for any server.

JUDGED steps (`ok` decides the run) are the ones whose outcome is fixed by the design or by what
was measured earlier. OBSERVED steps (`kind: "observation"`, always ok) record what
Flame did where nothing was known yet. They are collected in results["observations"] and printed
as a table at the end.

Stages
  a  node.list_types is the list of this Flame's types (equal to a transport read, its count the
     length of the list, every type this script creates in it; how many types there are differs by
     version and is recorded, not judged);
     node.create of Blur (named), Comp, Exposure, Write File and Action -> each node is there
     (transport), node.info gives its type and its sockets (equal to a transport read); an
     unknown type and a name the group already has are refused before anything is written.
  b  node.connect Blur -> Comp (the first Comp input named Front when there is one) -> both ends'
     sockets by transport; an unknown socket and nodes of different groups are refused, nothing
     written; node.disconnect with a socket, then without one -> transport; node.delete -> the
     node is gone from `nodes` and from the upstream node's output.
  c  journal.revert of each write (create, connect, disconnect, delete): the fingerprint is the
     one before the write, and transport reads nodes and sockets back independently.
  d  node.duplicate: in a group that is NOT current -> PRECONDITION_UNMET, nothing changes and
     nothing is journaled; in the current group -> the copy exists (transport) -> journal.revert
     removes it.
  e  node.save_setup (Exposure) -> the real file ends in `.exposure_node`; set_attributes changes
     exposure_red; node.load_setup(real path) -> the value is back and noChange is false; a path
     that is not there is refused before anything is written and the value stays.
  f  action.list_nodes (Default, Perspective) -> action.create_node (3D Path, 3D Shape: the inner
     nodes, the axes Flame adds) -> action.connect -> action.set_attributes (an inner node's
     position, in the type of its current value) -> journal.revert of each: the inner nodes are
     as before (fingerprint and transport).
  z  cross-cutting checks over every envelope; nothing left undecided

Cleanup: no Batch group is deleted (flame.delete on a PyBatch is not used); checkpoint files and
setup files are never deleted; then flame.exit().
"""

from __future__ import annotations

import argparse
import os
import time
import traceback
from pathlib import Path
from typing import Any, Final

import anyio
import e2e_guard
from p1_mcp_e2e import (
    RUN_DIR_DEFAULT,
    _br_dict,
    _result_of,
    _tx,
    check_envelope_shapes,
    check_logs_and_crash,
)
from p2_2a_journal_e2e import (
    Ctx,
    do,
    guarded,
    journal_index,
    judged,
    rec,
    save,
    save_stage,
    server_session,
    tx,
    version_info,
)

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
USED_NODE_TYPES: Final[tuple[str, ...]] = (
    "Blur",
    "Comp",
    "Exposure",
    "Write File",
    "Action",
    "Mux",
)
"""The node types this script creates: each must be in the list the op gives. The number of types
the list holds is not fixed here: it differs by Flame version, so it is only recorded."""
GROUPS: Final[tuple[str, ...]] = ("A", "B", "C", "D", "E", "F")
CUR: Final[str] = "CUR"
NEW_NAME: Final[str] = "P23B_named_blur"
MISSING_TYPE: Final[str] = "P23B_NoSuchType"
PYTYPE_OF: Final[dict[str, str]] = {"Write File": "PyWriteFileNode", "Action": "PyActionNode"}

Results = dict[str, Any]
State = dict[str, Any]


# Transport scaffolding (never through the op under test)
def _locate(uid: str) -> str:
    return "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            f"_bg = [g for g in _ws.desktop.batch_groups if _v(g.uid) == {uid!r}][0]",
        ]
    )


WORLD_CODE: Final[str] = "\n".join(
    [
        "def _try(fn):",
        "    try:",
        "        return fn()",
        "    except BaseException as _e:",
        "        return 'ERR:' + type(_e).__name__",
        "_nodes = []",
        "for _n in _bg.nodes:",
        "    _s = _n.sockets",
        "    _row = {",
        "        'name': _v(_n.name), 'pyType': type(_n).__name__,",
        "        'type': _try(lambda: _v(_n.type)),",
        "        'x': _try(lambda: _v(_n.pos_x)), 'y': _try(lambda: _v(_n.pos_y)),",
        "        'in': {k: sorted(v) for k, v in _s['input'].items() if v},",
        "        'out': {k: sorted(v) for k, v in _s['output'].items() if v},",
        "        'inputSockets': sorted(_s['input']), 'outputSockets': sorted(_s['output']),",
        "        'inner': None,",
        "    }",
        "    if type(_n).__name__ == 'PyActionNode':",
        "        _row['inner'] = [{",
        "            'name': _v(_c.name),",
        "            'type': _try(lambda: str(_v(_c.type))),",
        "            'parents': _try(lambda: sorted(_v(_p.name) for _p in _c.parents('Default'))),",
        "            'children': _try(",
        "                lambda: sorted(_v(_p.name) for _p in _c.children('Default'))),",
        "            'parentsNoArg': _try(lambda: sorted(_v(_p.name) for _p in _c.parents())),",
        "            'childrenNoArg': _try(lambda: sorted(_v(_p.name) for _p in _c.children())),",
        "        } for _c in _n.nodes]",
        "    _nodes.append(_row)",
        "_result = {'nodes': sorted(_nodes, key=lambda e: e['name'])}",
    ]
)


async def tx_world(ctx: Ctx, uid: str) -> State:
    br = await tx(ctx, _locate(uid) + "\n" + WORLD_CODE, label="p23b_world")
    if br.ok and isinstance(br.result, dict):
        return dict(br.result)
    return {"error": f"transport {br.error_code}: {br.error}", "nodes": []}


def node_names(world: State) -> list[str]:
    return sorted(n["name"] for n in world.get("nodes", []))


def find(world: State, name: str) -> dict[str, Any] | None:
    return next((n for n in world.get("nodes", []) if n["name"] == name), None)


def wiring(world: State) -> dict[str, Any]:
    return {n["name"]: {"in": n["in"], "out": n["out"]} for n in world.get("nodes", [])}


def inner_map(world: State) -> dict[str, Any]:
    return {n["name"]: n["inner"] for n in world.get("nodes", []) if n["inner"] is not None}


def inner_names(world: State, action: str) -> list[str]:
    node = find(world, action) or {}
    return [c["name"] for c in node.get("inner") or []]


def diff_world(a: State, b: State) -> dict[str, Any]:
    out: dict[str, Any] = {}
    added = sorted(set(node_names(b)) - set(node_names(a)))
    removed = sorted(set(node_names(a)) - set(node_names(b)))
    if added or removed:
        out["nodes"] = {"added": added, "removed": removed}
    if wiring(a) != wiring(b):
        out["wiring"] = {"before": wiring(a), "after": wiring(b)}
    if inner_map(a) != inner_map(b):
        out["inner"] = {"before": inner_map(a), "after": inner_map(b)}
    return out


async def tx_scaffold(ctx: Ctx, uid: str, body: str, label: str) -> Any:
    br = await tx(ctx, _locate(uid) + "\n" + body, label=label, timeout_s=40.0)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_add_node(ctx: Ctx, uid: str, node_type: str) -> Any:
    return await tx_scaffold(
        ctx, uid, f"_result = _v(_bg.create_node({node_type!r}).name)", "p23b_add_node"
    )


async def tx_node_types(ctx: Ctx, uid: str) -> Any:
    return await tx_scaffold(ctx, uid, "_result = [str(t) for t in _bg.node_types]", "p23b_types")


async def tx_attr(ctx: Ctx, uid: str, node: str, key: str, inner: str | None = None) -> Any:
    """`{'value', 'pyType'}` of one attribute, or an error string."""
    path = "_o = [n for n in _bg.nodes if _v(n.name) == " + repr(node) + "][0]"
    if inner is not None:
        path += "\n_o = [c for c in _o.nodes if _v(c.name) == " + repr(inner) + "][0]"
    body = "\n".join(
        [
            path,
            f"_x = getattr(_o, {key!r})",
            "_val = _v(_x)",
            "_result = {'value': _val, 'pyType': type(_val).__name__}",
        ]
    )
    return await tx_scaffold(ctx, uid, body, "p23b_attr")


async def tx_file_exists(ctx: Ctx, path: str) -> Any:
    br = await tx(ctx, f"import os\n_result = os.path.isfile({path!r})", label="p23b_exists")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_list_dir(ctx: Ctx, path: str) -> Any:
    br = await tx(ctx, f"import os\n_result = sorted(os.listdir({path!r}))", label="p23b_ls")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_makedirs(ctx: Ctx, path: str) -> Any:
    br = await tx(
        ctx, f"import os\nos.makedirs({path!r}, exist_ok=True)\n_result = True", label="p23b_mk"
    )
    return br.ok


async def tx_is_current(ctx: Ctx, uid: str) -> Any:
    code = (
        "_cur = _v(flame.projects.current_project.current_workspace.desktop.current_batch_group)\n"
        f"_result = _cur is not None and _v(_cur.uid) == {uid!r}"
    )
    br = await tx(ctx, code, label="p23b_is_current")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


def target_of(ctx: Ctx, group: str) -> dict[str, Any]:
    return {"kind": "batch", "uid": ctx.ids["groups"][group]["uid"]}


def uid_of(ctx: Ctx, group: str) -> str:
    return str(ctx.ids["groups"][group]["uid"])


def node_sel(ctx: Ctx, group: str, name: str) -> dict[str, Any]:
    return {"kind": "node", "batch": target_of(ctx, group), "name": name}


def inner_sel(ctx: Ctx, group: str, action: str, name: str) -> dict[str, Any]:
    return {"kind": "action_node", "node": node_sel(ctx, group, action), "name": name}


def observe(ctx: Ctx, name: str, **data: Any) -> None:
    """A step that records what Flame did and decides nothing. A `ok` in `data` is the answer of
    the op under observation, not this step's verdict (`rec` takes `ok` itself): it is recorded
    as `opOk`. `kind` is reserved for the step and refused."""
    if "kind" in data:
        raise ValueError("observe(): `kind` is reserved (the step's kind is 'observation')")
    if "ok" in data:
        data["opOk"] = data.pop("ok")
    ctx.results.setdefault("observations", []).append({"name": name, "stage": ctx.stage, **data})
    rec(ctx, name, True, kind="observation", **data)


def journal_group_count(ctx: Ctx) -> int:
    return len(journal_index(ctx).groups)


async def plain_fp(session: Any, ctx: Ctx, group: str) -> Any:
    env = await do(session, ctx, "batch.fingerprint", {"target": target_of(ctx, group)})
    return _result_of(env).get("fingerprint")


def same_sockets(info: dict[str, Any], row: dict[str, Any] | None) -> bool:
    """`node.info`'s sockets carry the same socket names as the transport read."""
    sockets = info.get("sockets") or {}
    if row is None:
        return False
    return (
        sorted(sockets.get("input", {})) == row["inputSockets"]
        and sorted(sockets.get("output", {})) == row["outputSockets"]
    )


async def prepare(ctx: Ctx) -> None:
    lines = [
        "desktop = flame.projects.current_project.current_workspace.desktop",
        "groups = {}",
    ]
    seeds = {
        "A": [],
        "B": ["Blur", "Comp", "Exposure"],
        "C": ["Blur", "Comp", "Exposure"],
        "D": ["Blur"],
        "E": ["Exposure"],
        "F": ["Action"],
    }
    for group in GROUPS:
        lines.append(
            f"bg = desktop.create_batch_group('P23B_{group}_{ctx.ts}', "
            "nb_reels=1, nb_shelf_reels=1)"
        )
        lines += [f"bg.create_node({t!r})" for t in seeds[group]]
        lines.append(f"groups[{group!r}] = {{'uid': _v(bg.uid), 'name': _v(bg.name)}}")
    lines += [
        # created last: it becomes the current group, so none of the six is current
        f"cur = desktop.create_batch_group('P23B_{CUR}_{ctx.ts}', nb_reels=1, nb_shelf_reels=1)",
        "cur.create_node('Blur')",
        "cur.create_node('Exposure')",
        "_result = {'groups': groups, 'cur': {'uid': _v(cur.uid), 'name': _v(cur.name)}}",
    ]
    br = await tx(ctx, "\n".join(lines), label="p23b_prepare_groups", timeout_s=90.0)
    rec(ctx, "prepare_groups_and_the_current_group", br.ok, resp=_br_dict(br))
    if not (br.ok and isinstance(br.result, dict)):
        return
    groups = dict(br.result["groups"])
    groups[CUR] = br.result["cur"]
    ctx.ids.update(groups=groups)
    others = [groups[g]["uid"] for g in GROUPS]
    cur_ok = await tx_is_current(ctx, groups[CUR]["uid"])
    rec(
        ctx,
        "prepare_only_cur_is_current",
        cur_ok is True and all([await tx_is_current(ctx, u) is False for u in others]),
        cur=cur_ok,
    )


async def stage_a(ctx: Ctx) -> None:
    uid, target = uid_of(ctx, "A"), target_of(ctx, "A")
    async with server_session(ctx, "a-plain") as session:
        listed = await do(session, ctx, "node.list_types", {"target": target})
        types = _result_of(listed).get("types")
        via_tx = await tx_node_types(ctx, uid)
        count = _result_of(listed).get("count")
        known = types if isinstance(types, list) else []
        # node.create matches a type ignoring case (it takes 'Mux' for the listed 'MUX').
        known_folded = {str(t).casefold() for t in known}
        ok, parts = judged(
            op_ok=listed.get("ok") is True,
            types_is_a_list=isinstance(types, list),
            count_is_the_length_of_the_list=count == len(known),
            count_is_the_length_of_the_transport_read=isinstance(via_tx, list)
            and count == len(via_tx),
            list_equals_the_transport_read=types == via_tx,
            every_type_this_script_creates_is_listed=all(
                t.casefold() in known_folded for t in USED_NODE_TYPES
            ),
        )
        rec(
            ctx,
            "a_list_types_is_the_transport_read_and_holds_every_type_this_script_creates",
            ok,
            clauses=parts,
            count=count,
            transportCount=len(via_tx) if isinstance(via_tx, list) else None,
            notListed=[t for t in USED_NODE_TYPES if t.casefold() not in known_folded],
        )
        observe(
            ctx,
            "a_list_types_how_many_types_this_flame_has",
            count=count,
            flameVersion=e2e_guard.e2e_flame_version(),
        )
        rec(ctx, "a_list_types_wrote_nothing", journal_group_count(ctx) == 0)

        # create: Blur (named), Comp, Exposure, Write File, Action
        created: dict[str, str] = {}
        for node_type, name in (
            ("Blur", NEW_NAME),
            ("Comp", None),
            ("Exposure", None),
            ("Write File", None),
            ("Action", None),
        ):
            before = await tx_world(ctx, uid)
            args: dict[str, Any] = {"target": target, "type": node_type}
            if name is not None:
                args["name"] = name
            env = await do(session, ctx, "node.create", args)
            after = await tx_world(ctx, uid)
            added = sorted(set(node_names(after)) - set(node_names(before)))
            result = _result_of(env)
            made = (result.get("node") or {}).get("name")
            row = find(after, made) if made else None
            rec(
                ctx,
                f"a_create_{node_type.replace(' ', '_').lower()}_made_exactly_that_node",
                env.get("ok") is True
                and added == [made]
                and row is not None
                and (name is None or made == name)
                and (env.get("journal") or {}).get("group") is not None
                and (env.get("journal") or {}).get("checkpoint") is not None,
                requested=node_type,
                added=added,
                result=result,
            )
            observe(
                ctx,
                f"a_create_{node_type.replace(' ', '_').lower()}_type_and_pytype",
                requested=node_type,
                typeAttribute=(row or {}).get("type"),
                pyType=(row or {}).get("pyType"),
                typeAsRequested=result.get("typeAsRequested"),
                canonicalType=result.get("canonicalType"),
                typeAsCanonical=result.get("typeAsCanonical"),
                createdAs=result.get("createdAs"),
                expectedPyType=PYTYPE_OF.get(node_type, "PyNode"),
            )
            if made:
                created[node_type] = made

        # info of each
        world = await tx_world(ctx, uid)
        for node_type, made in created.items():
            info = await do(session, ctx, "node.info", {"target": node_sel(ctx, "A", made)})
            result = _result_of(info)
            row = find(world, made)
            rec(
                ctx,
                f"a_info_of_{node_type.replace(' ', '_').lower()}_names_its_type_and_sockets",
                info.get("ok") is True
                and result.get("name") == made
                and result.get("pyType") == (row or {}).get("pyType")
                and result.get("type") == (row or {}).get("type")
                and same_sockets(result, row)
                and (result.get("stable") or {}).get("name") == made,
                sockets=result.get("sockets"),
                viaTransport={k: (row or {}).get(k) for k in ("inputSockets", "outputSockets")},
                unreadable=result.get("unreadable"),
            )
            observe(
                ctx,
                f"a_info_of_{node_type.replace(' ', '_').lower()}_extras",
                clip=result.get("clip"),
                render=result.get("render"),
                innerNodeCount=result.get("innerNodeCount"),
                attributeCount=len(result.get("attributes") or {}),
            )
        blur_info = _result_of(
            await do(
                session, ctx, "node.info", {"target": node_sel(ctx, "A", created.get("Blur", ""))}
            )
        )
        observe(
            ctx,
            "a_blur_sockets_against_plan_48",
            sockets=blur_info.get("sockets"),
            plan48="input [Front, Matte] / output [Result, OutMatte]",
        )
        with_values = _result_of(
            await do(
                session,
                ctx,
                "node.info",
                {"target": node_sel(ctx, "A", created.get("Comp", "")), "include_values": True},
            )
        )
        rec(
            ctx,
            "a_info_include_values_adds_the_allowed_values_of_an_enumerated_attribute",
            any(
                isinstance(a, dict) and isinstance(a.get("values"), list) and a["values"]
                for a in (with_values.get("attributes") or {}).values()
            ),
            attributes=len(with_values.get("attributes") or {}),
        )

        # refusals: nothing is written, nothing is journaled
        groups_before = journal_group_count(ctx)
        base = await tx_world(ctx, uid)
        unknown = await do(session, ctx, "node.create", {"target": target, "type": MISSING_TYPE})
        taken = await do(
            session, ctx, "node.create", {"target": target, "type": "Blur", "name": NEW_NAME}
        )
        after = await tx_world(ctx, uid)
        rec(
            ctx,
            "a_an_unknown_type_and_a_taken_name_are_refused_before_anything_is_written",
            unknown.get("errorCode") == "INVALID_ARGS"
            and taken.get("errorCode") == "INVALID_ARGS"
            and unknown.get("journal") is None
            and taken.get("journal") is None
            and journal_group_count(ctx) == groups_before
            and diff_world(base, after) == {},
            unknown=unknown,
            taken=taken,
        )


# stage b
def pick(names: list[str], preferred: str) -> str:
    return preferred if preferred in names else (names[0] if names else preferred)


async def stage_b(ctx: Ctx) -> None:
    uid = uid_of(ctx, "B")
    world = await tx_world(ctx, uid)
    blur = next(n for n in world["nodes"] if n["type"] == "Blur" or n["name"].startswith("blur"))
    comp = next(n for n in world["nodes"] if n["name"].startswith("comp"))
    expo = next(n for n in world["nodes"] if n["name"].startswith("exposure"))
    front = pick(comp["inputSockets"], "Front")
    out_socket = pick(blur["outputSockets"], "Result")
    observe(
        ctx,
        "b_sockets_used",
        blurOut=blur["outputSockets"],
        compIn=comp["inputSockets"],
        chosenTarget=front,
        chosenSource=out_socket,
    )
    async with server_session(ctx, "b-plain") as session:
        # connect with named sockets
        env = await do(
            session,
            ctx,
            "node.connect",
            {
                "source": node_sel(ctx, "B", blur["name"]),
                "target": node_sel(ctx, "B", comp["name"]),
                "source_socket": out_socket,
                "target_socket": front,
            },
        )
        after = await tx_world(ctx, uid)
        rec(
            ctx,
            "b_connect_linked_blur_to_comp_on_both_ends_by_transport",
            env.get("ok") is True
            and find(after, comp["name"])["in"].get(front) == [blur["name"]]
            and find(after, blur["name"])["out"].get(out_socket) == [comp["name"]]
            and _result_of(env).get("linkedInputSockets") == [front],
            result=_result_of(env),
            compIn=find(after, comp["name"])["in"],
            blurOut=find(after, blur["name"])["out"],
        )

        # refusals: nothing is written, nothing is journaled
        groups_before = journal_group_count(ctx)
        snapshot = await tx_world(ctx, uid)
        wrong_socket = await do(
            session,
            ctx,
            "node.connect",
            {
                "source": node_sel(ctx, "B", blur["name"]),
                "target": node_sel(ctx, "B", comp["name"]),
                "target_socket": "P23B_NoSuchSocket",
            },
        )
        other = await do(
            session,
            ctx,
            "node.connect",
            {
                "source": node_sel(ctx, "A", NEW_NAME),
                "target": node_sel(ctx, "B", comp["name"]),
            },
        )
        rec(
            ctx,
            "b_an_unknown_socket_and_nodes_of_two_groups_are_refused_and_nothing_changed",
            wrong_socket.get("errorCode") == "INVALID_ARGS"
            and other.get("errorCode") == "PRECONDITION_UNMET"
            and wrong_socket.get("journal") is None
            and other.get("journal") is None
            and journal_group_count(ctx) == groups_before
            and diff_world(snapshot, await tx_world(ctx, uid)) == {},
            wrongSocket=wrong_socket,
            otherGroup=other,
        )

        # Default sockets (what Flame chooses)
        default = await do(
            session,
            ctx,
            "node.connect",
            {
                "source": node_sel(ctx, "B", expo["name"]),
                "target": node_sel(ctx, "B", blur["name"]),
            },
        )
        after_default = await tx_world(ctx, uid)
        observe(
            ctx,
            "b_connect_with_default_sockets_exposure_to_blur",
            opOk=default.get("ok"),
            error=default.get("error"),
            errorCode=default.get("errorCode"),
            linkedInputSockets=_result_of(default).get("linkedInputSockets"),
            blurIn=(find(after_default, blur["name"]) or {}).get("in"),
        )

        # disconnect the named socket
        cut = await do(
            session,
            ctx,
            "node.disconnect",
            {"target": node_sel(ctx, "B", comp["name"]), "socket": front},
        )
        after_cut = await tx_world(ctx, uid)
        rec(
            ctx,
            "b_disconnect_with_a_socket_cut_that_socket_on_both_ends",
            cut.get("ok") is True
            and front not in find(after_cut, comp["name"])["in"]
            and out_socket not in find(after_cut, blur["name"])["out"]
            and _result_of(cut).get("hadLinks") is True,
            result=_result_of(cut),
            compIn=find(after_cut, comp["name"])["in"],
        )

        # connect again, then disconnect without a socket: every input is cut
        again = await do(
            session,
            ctx,
            "node.connect",
            {
                "source": node_sel(ctx, "B", blur["name"]),
                "target": node_sel(ctx, "B", comp["name"]),
                "source_socket": out_socket,
                "target_socket": front,
            },
        )
        cut_all = await do(
            session, ctx, "node.disconnect", {"target": node_sel(ctx, "B", comp["name"])}
        )
        after_all = await tx_world(ctx, uid)
        rec(
            ctx,
            "b_disconnect_without_a_socket_cut_every_input",
            again.get("ok") is True
            and cut_all.get("ok") is True
            and find(after_all, comp["name"])["in"] == {}
            and out_socket not in find(after_all, blur["name"])["out"],
            result=_result_of(cut_all),
            compIn=find(after_all, comp["name"])["in"],
        )

        # delete: the node goes, and its link goes from the upstream node's output
        before_delete = await tx_world(ctx, uid)
        rewired = await do(
            session,
            ctx,
            "node.connect",
            {
                "source": node_sel(ctx, "B", blur["name"]),
                "target": node_sel(ctx, "B", comp["name"]),
                "source_socket": out_socket,
                "target_socket": front,
            },
        )
        gone = await do(session, ctx, "node.delete", {"target": node_sel(ctx, "B", comp["name"])})
        after_delete = await tx_world(ctx, uid)
        rec(
            ctx,
            "b_delete_removed_the_node_and_the_upstream_output_no_longer_names_it",
            rewired.get("ok") is True
            and gone.get("ok") is True
            and comp["name"] not in node_names(after_delete)
            and comp["name"]
            not in [c for v in find(after_delete, blur["name"])["out"].values() for c in v]
            and sorted(node_names(before_delete))
            == sorted([*node_names(after_delete), comp["name"]]),
            result=_result_of(gone),
            blurOut=find(after_delete, blur["name"])["out"],
        )
        missing = await do(
            session, ctx, "node.delete", {"target": node_sel(ctx, "B", comp["name"])}
        )
        rec(
            ctx,
            "b_deleting_it_again_is_refused_before_a_checkpoint",
            missing.get("errorCode") == "SELECTOR_EMPTY" and missing.get("journal") is None,
            env=missing,
        )


# stage c
async def revert_and_read(session: Any, ctx: Ctx, env: dict[str, Any]) -> dict[str, Any]:
    return await do(
        session, ctx, "journal.revert", {"group": (env.get("journal") or {}).get("group")}
    )


async def stage_c(ctx: Ctx) -> None:
    uid = uid_of(ctx, "C")
    world = await tx_world(ctx, uid)
    blur = next(n for n in world["nodes"] if n["name"].startswith("blur"))
    comp = next(n for n in world["nodes"] if n["name"].startswith("comp"))
    front = pick(comp["inputSockets"], "Front")
    out_socket = pick(blur["outputSockets"], "Result")
    connect_args = {
        "source": node_sel(ctx, "C", blur["name"]),
        "target": node_sel(ctx, "C", comp["name"]),
        "source_socket": out_socket,
        "target_socket": front,
    }
    async with server_session(ctx, "c-plain") as session:
        # create -> revert
        base = await tx_world(ctx, uid)
        fp0 = await plain_fp(session, ctx, "C")
        made = await do(
            session,
            ctx,
            "node.create",
            {"target": target_of(ctx, "C"), "type": "Mux", "name": "P23B_c_new"},
        )
        fp1 = await plain_fp(session, ctx, "C")
        back = await revert_and_read(session, ctx, made)
        after = await tx_world(ctx, uid)
        rec(
            ctx,
            "c_create_then_revert_the_fingerprint_is_the_one_before_and_the_node_is_gone",
            made.get("ok") is True
            and back.get("ok") is True
            and fp1 != fp0
            and await plain_fp(session, ctx, "C") == fp0
            and "P23B_c_new" not in node_names(after)
            and diff_world(base, after) == {},
            revert=back,
            change=diff_world(base, after),
        )

        # connect -> revert
        base = await tx_world(ctx, uid)
        fp0 = await plain_fp(session, ctx, "C")
        linked = await do(session, ctx, "node.connect", connect_args)
        fp1 = await plain_fp(session, ctx, "C")
        back = await revert_and_read(session, ctx, linked)
        after = await tx_world(ctx, uid)
        rec(
            ctx,
            "c_connect_then_revert_the_fingerprint_is_the_one_before_and_the_link_is_gone",
            linked.get("ok") is True
            and back.get("ok") is True
            and fp1 != fp0
            and await plain_fp(session, ctx, "C") == fp0
            and diff_world(base, after) == {},
            revert=back,
            change=diff_world(base, after),
        )

        # disconnect -> revert (start from a linked group)
        keep = await do(session, ctx, "node.connect", connect_args)
        base = await tx_world(ctx, uid)
        fp0 = await plain_fp(session, ctx, "C")
        cut = await do(
            session, ctx, "node.disconnect", {"target": node_sel(ctx, "C", comp["name"])}
        )
        fp1 = await plain_fp(session, ctx, "C")
        back = await revert_and_read(session, ctx, cut)
        after = await tx_world(ctx, uid)
        rec(
            ctx,
            "c_disconnect_then_revert_puts_the_link_back",
            keep.get("ok") is True
            and cut.get("ok") is True
            and back.get("ok") is True
            and fp1 != fp0
            and await plain_fp(session, ctx, "C") == fp0
            and diff_world(base, after) == {}
            and find(after, comp["name"])["in"].get(front) == [blur["name"]],
            revert=back,
            change=diff_world(base, after),
        )

        # delete -> revert
        base = await tx_world(ctx, uid)
        fp0 = await plain_fp(session, ctx, "C")
        gone = await do(session, ctx, "node.delete", {"target": node_sel(ctx, "C", comp["name"])})
        fp1 = await plain_fp(session, ctx, "C")
        back = await revert_and_read(session, ctx, gone)
        after = await tx_world(ctx, uid)
        rec(
            ctx,
            "c_delete_then_revert_brings_the_node_and_its_wiring_back",
            gone.get("ok") is True
            and back.get("ok") is True
            and fp1 != fp0
            and await plain_fp(session, ctx, "C") == fp0
            and diff_world(base, after) == {}
            and comp["name"] in node_names(after),
            revert=back,
            change=diff_world(base, after),
        )
        observe(
            ctx,
            "c_the_restored_comp_after_the_delete_revert",
            comp=find(after, comp["name"]),
            revertResult={
                k: _result_of(back).get(k) for k in ("matched", "expectedFingerprint", "reverted")
            },
        )


async def stage_d(ctx: Ctx) -> None:
    uid_n, uid_c = uid_of(ctx, "D"), uid_of(ctx, CUR)
    world_n = await tx_world(ctx, uid_n)
    blur_n = world_n["nodes"][0]["name"]
    async with server_session(ctx, "d-plain") as session:
        rec(
            ctx,
            "d_the_groups_are_as_planned_d_is_not_current_cur_is",
            await tx_is_current(ctx, uid_n) is False and await tx_is_current(ctx, uid_c) is True,
        )
        # not current: refused, nothing changes, nothing is journaled
        groups_before = journal_group_count(ctx)
        refused = await do(session, ctx, "node.duplicate", {"target": node_sel(ctx, "D", blur_n)})
        rec(
            ctx,
            "d_duplicate_in_a_group_that_is_not_current_is_precondition_unmet_and_writes_nothing",
            refused.get("errorCode") == "PRECONDITION_UNMET"
            and refused.get("journal") is None
            and journal_group_count(ctx) == groups_before
            and diff_world(world_n, await tx_world(ctx, uid_n)) == {},
            env=refused,
        )
        # the plain Flame call, for the record (the message the op relies on)
        raw = await tx_scaffold(
            ctx,
            uid_n,
            "\n".join(
                [
                    f"_n = [n for n in _bg.nodes if _v(n.name) == {blur_n!r}][0]",
                    "try:",
                    "    _n.duplicate()",
                    "    _result = 'no error'",
                    "except BaseException as _e:",
                    "    _result = type(_e).__name__ + ': ' + str(_e)",
                ]
            ),
            "p23b_d_raw_duplicate",
        )
        observe(
            ctx,
            "d_the_raw_flame_answer_for_a_non_current_group",
            answer=raw,
            worldAfter=diff_world(world_n, await tx_world(ctx, uid_n)),
        )

        # current: the copy exists, then revert removes it
        base = await tx_world(ctx, uid_c)
        source = next(n for n in base["nodes"] if n["name"].startswith("blur"))
        fp0 = await plain_fp(session, ctx, CUR)
        dup = await do(
            session, ctx, "node.duplicate", {"target": node_sel(ctx, CUR, source["name"])}
        )
        after = await tx_world(ctx, uid_c)
        added = sorted(set(node_names(after)) - set(node_names(base)))
        result = _result_of(dup)
        rec(
            ctx,
            "d_duplicate_in_the_current_group_made_one_new_node_named_in_the_result",
            dup.get("ok") is True
            and len(added) == 1
            and (result.get("node") or {}).get("name") == added[0]
            and (find(after, added[0]) or {}).get("type") == source["type"]
            and (dup.get("journal") or {}).get("changed") is True,
            added=added,
            result=result,
        )
        back = await revert_and_read(session, ctx, dup)
        after_back = await tx_world(ctx, uid_c)
        rec(
            ctx,
            "d_revert_of_the_duplicate_removed_it_and_the_fingerprint_is_the_one_before",
            back.get("ok") is True
            and await plain_fp(session, ctx, CUR) == fp0
            and diff_world(base, after_back) == {},
            revert=back,
            change=diff_world(base, after_back),
        )
        observe(
            ctx,
            "d_cur_is_still_current_after_the_revert",
            isCurrent=await tx_is_current(ctx, uid_c),
        )


async def stage_e(ctx: Ctx) -> None:
    uid = uid_of(ctx, "E")
    world = await tx_world(ctx, uid)
    expo = world["nodes"][0]["name"]
    target = node_sel(ctx, "E", expo)
    directory = str(OUTPUT_DIR / f"node_setup_e2e_{ctx.ts}")
    await tx_makedirs(ctx, directory)
    base_path = os.path.join(directory, "expo")
    async with server_session(ctx, "e-plain") as session:
        groups_before = journal_group_count(ctx)
        saved = await do(session, ctx, "node.save_setup", {"target": target, "path": base_path})
        real = _result_of(saved).get("path")
        listing = await tx_list_dir(ctx, directory)
        rec(
            ctx,
            "e_save_setup_wrote_one_file_ending_in_exposure_node_and_reports_it",
            saved.get("ok") is True
            and real == base_path + ".exposure_node"
            and await tx_file_exists(ctx, str(real)) is True
            and listing == ["expo.exposure_node"]
            and _result_of(saved).get("written") == [real]
            and (_result_of(saved).get("size") or 0) > 0,
            result=_result_of(saved),
            directory=listing,
        )
        rec(
            ctx,
            "e_save_setup_is_not_journaled_and_declares_no_undo",
            saved.get("journal") is None
            and saved.get("execution", {}).get("undo") == "none"
            and journal_group_count(ctx) == groups_before,
            execution=saved.get("execution"),
        )
        again = await do(session, ctx, "node.save_setup", {"target": target, "path": base_path})
        rec(
            ctx,
            "e_an_existing_setup_is_refused_without_overwrite",
            again.get("errorCode") == "PRECONDITION_UNMET",
            env=again,
        )

        # change a value in the type of its current value, then load the setup back
        current = await tx_attr(ctx, uid, expo, "exposure_red")
        v0 = current["value"] if isinstance(current, dict) else None
        py = current["pyType"] if isinstance(current, dict) else None
        v1 = (v0 + 0.5) if py == "float" else (v0 + 1 if py == "int" else None)
        observe(ctx, "e_exposure_red_before", value=v0, pyType=py, willWrite=v1)
        changed = await do(
            session, ctx, "node.set_attributes", {"target": target, "values": {"exposure_red": v1}}
        )
        after_set = await tx_attr(ctx, uid, expo, "exposure_red")
        rec(
            ctx,
            "e_set_attributes_changed_exposure_red_by_transport",
            changed.get("ok") is True
            and isinstance(after_set, dict)
            and abs(after_set["value"] - v1) < 1e-6,
            after=after_set,
            env=changed,
        )
        loaded = await do(session, ctx, "node.load_setup", {"target": target, "path": real})
        after_load = await tx_attr(ctx, uid, expo, "exposure_red")
        result = _result_of(loaded)
        rec(
            ctx,
            "e_load_setup_put_the_value_back_and_says_it_changed_something",
            loaded.get("ok") is True
            and isinstance(after_load, dict)
            and abs(after_load["value"] - v0) < 1e-6
            and result.get("noChange") is False
            and "exposure_red" in (result.get("changedKeys") or [])
            and (loaded.get("journal") or {}).get("checkpoint") is not None,
            after=after_load,
            result=result,
        )
        observe(
            ctx,
            "e_every_key_the_load_changed",
            changedKeys=result.get("changedKeys"),
            changed=result.get("changed"),
        )
        second = await do(session, ctx, "node.load_setup", {"target": target, "path": real})
        observe(
            ctx,
            "e_loading_the_same_setup_again",
            opOk=second.get("ok"),
            noChange=_result_of(second).get("noChange"),
            changedKeys=_result_of(second).get("changedKeys"),
        )

        # a path that is not there: refused before anything is written, value untouched
        groups_before = journal_group_count(ctx)
        missing_path = os.path.join(directory, "nothing.exposure_node")
        missing = await do(
            session, ctx, "node.load_setup", {"target": target, "path": missing_path}
        )
        after_missing = await tx_attr(ctx, uid, expo, "exposure_red")
        rec(
            ctx,
            "e_load_setup_of_a_path_that_is_not_there_is_refused_and_the_value_stays",
            missing.get("errorCode") == "PRECONDITION_UNMET"
            and missing.get("journal") is None
            and journal_group_count(ctx) == groups_before
            and isinstance(after_missing, dict)
            and abs(after_missing["value"] - v0) < 1e-6,
            env=missing,
            value=after_missing,
        )
        relative = await do(
            session, ctx, "node.load_setup", {"target": target, "path": "expo.exposure_node"}
        )
        rec(
            ctx,
            "e_load_setup_needs_an_absolute_path",
            relative.get("errorCode") == "INVALID_ARGS",
            env=relative,
        )

        # what a revert of the load brings back (the checkpoint held the changed value)
        await do(
            session, ctx, "node.set_attributes", {"target": target, "values": {"exposure_red": v1}}
        )
        loaded2 = await do(session, ctx, "node.load_setup", {"target": target, "path": real})
        back = await revert_and_read(session, ctx, loaded2)
        after_revert = await tx_attr(ctx, uid, expo, "exposure_red")
        observe(
            ctx,
            "e_revert_of_the_load_setup",
            revertOk=back.get("ok"),
            errorCode=back.get("errorCode"),
            valueAfterRevert=after_revert,
            expectedIfTheCheckpointHoldsSettings=v1,
        )
        rec(
            ctx,
            "e_the_revert_did_not_lose_the_bridge",
            back.get("errorCode") not in ("BRIDGE_LOST", "BRIDGE_UNAVAILABLE", "TIMEOUT"),
        )


async def stage_f(ctx: Ctx) -> None:
    uid = uid_of(ctx, "F")
    world = await tx_world(ctx, uid)
    action = next(n["name"] for n in world["nodes"] if n["pyType"] == "PyActionNode")
    target = node_sel(ctx, "F", action)
    inner0 = inner_names(world, action)
    async with server_session(ctx, "f-plain") as session:
        listing = await do(session, ctx, "action.list_nodes", {"target": target})
        result = _result_of(listing)
        rec(
            ctx,
            "f_list_nodes_is_default_and_perspective_and_matches_the_transport_read",
            listing.get("ok") is True
            and sorted(r["name"] for r in result.get("nodes") or []) == sorted(inner0)
            and {"Default", "Perspective"} <= set(inner0)
            and "3D Path" in (result.get("nodeTypes") or []),
            result=result,
            viaTransport=inner0,
        )
        wrong = await do(
            session,
            ctx,
            "action.list_nodes",
            {"target": node_sel(ctx, "F", "P23B_no_such_node")},
        )
        rec(
            ctx,
            "f_list_nodes_of_a_node_that_is_not_there_is_selector_empty",
            wrong.get("errorCode") == "SELECTOR_EMPTY",
            env=wrong,
        )

        fp0 = await plain_fp(session, ctx, "F")
        groups_before = journal_group_count(ctx)
        refused = await do(
            session, ctx, "action.create_node", {"target": target, "type": MISSING_TYPE}
        )
        rec(
            ctx,
            "f_create_node_of_an_unknown_type_is_refused_before_anything_is_written",
            refused.get("errorCode") == "INVALID_ARGS"
            and refused.get("journal") is None
            and journal_group_count(ctx) == groups_before
            and inner_names(await tx_world(ctx, uid), action) == inner0,
            env=refused,
        )

        # create 3D Path, then 3D Shape
        made: list[dict[str, Any]] = []
        prior = inner0
        for inner_type in ("3D Path", "3D Shape"):
            env = await do(
                session, ctx, "action.create_node", {"target": target, "type": inner_type}
            )
            now = inner_names(await tx_world(ctx, uid), action)
            result = _result_of(env)
            added = [n for n in now if n not in prior]
            rec(
                ctx,
                f"f_create_node_{inner_type.replace(' ', '_').lower()}_added_what_transport_sees",
                env.get("ok") is True
                and added == [r["name"] for r in result.get("added") or []]
                and (result.get("created") or {}).get("name") in added
                and len(now) == len(prior) + len(added),
                added=added,
                result=result,
            )
            observe(
                ctx,
                f"f_create_node_{inner_type.replace(' ', '_').lower()}_what_flame_added_itself",
                created=(result.get("created") or {}).get("name"),
                alsoAdded=result.get("alsoAdded"),
                addedWithTypes=result.get("added"),
            )
            made.append({"env": env, "created": (result.get("created") or {}).get("name")})
            prior = now
        path_node, shape_node = made[0]["created"], made[1]["created"]

        # connect: parent = the 3D Path, child = the 3D Shape
        before_link = await tx_world(ctx, uid)
        linked = await do(
            session,
            ctx,
            "action.connect",
            {
                "parent": inner_sel(ctx, "F", action, path_node),
                "child": inner_sel(ctx, "F", action, shape_node),
            },
        )
        after_link = await tx_world(ctx, uid)
        child_row = next(
            (
                c
                for c in (find(after_link, action) or {}).get("inner", [])
                if c["name"] == shape_node
            ),
            {},
        )
        parent_row = next(
            (
                c
                for c in (find(after_link, action) or {}).get("inner", [])
                if c["name"] == path_node
            ),
            {},
        )
        observe(
            ctx,
            "f_connect_what_the_transport_reads_of_parents_and_children",
            opOk=linked.get("ok"),
            errorCode=linked.get("errorCode"),
            error=linked.get("error"),
            result=_result_of(linked),
            childParents=child_row.get("parents"),
            parentChildren=parent_row.get("children"),
            childParentsNoArg=child_row.get("parentsNoArg"),
            parentChildrenNoArg=parent_row.get("childrenNoArg"),
            before=diff_world(before_link, after_link),
        )
        rec(
            ctx,
            "f_connect_linked_them_and_the_transport_agrees",
            linked.get("ok") is True
            and _result_of(linked).get("linked") is True
            and path_node in (child_row.get("parents") or [])
            and shape_node in (parent_row.get("children") or []),
            result=_result_of(linked),
            childParents=child_row.get("parents"),
            parentChildren=parent_row.get("children"),
        )
        bad_link = await do(
            session,
            ctx,
            "action.connect",
            {
                "parent": inner_sel(ctx, "F", action, path_node),
                "child": inner_sel(ctx, "F", action, shape_node),
                "link_type": "P23B_nonsense",
            },
        )
        rec(
            ctx,
            "f_an_unknown_link_type_is_refused_before_a_checkpoint",
            bad_link.get("errorCode") == "INVALID_ARGS" and bad_link.get("journal") is None,
            env=bad_link,
        )

        # set_attributes on an inner node's position, in the type of its current value
        pos = await tx_attr(ctx, uid, action, "pos_x", inner=path_node)
        info = await do(
            session,
            ctx,
            "action.get_attributes",
            {
                "target": inner_sel(ctx, "F", action, path_node),
                "keys": ["pos_x"],
                "include_values": True,
            },
        )
        observe(
            ctx,
            "f_the_inner_node_position_type_and_range",
            transport=pos,
            viaOp=_result_of(info).get("attributes"),
        )
        value_group = None
        if isinstance(pos, dict) and pos["pyType"] in ("int", "float"):
            new = pos["value"] + (10 if pos["pyType"] == "int" else 10.0)
            wrote = await do(
                session,
                ctx,
                "action.set_attributes",
                {"target": inner_sel(ctx, "F", action, path_node), "values": {"pos_x": new}},
            )
            after_pos = await tx_attr(ctx, uid, action, "pos_x", inner=path_node)
            rec(
                ctx,
                "f_set_attributes_moved_the_inner_node_and_the_transport_agrees",
                wrote.get("ok") is True
                and isinstance(after_pos, dict)
                and abs(after_pos["value"] - new) < 1e-6
                and (_result_of(wrote).get("revert") or {}).get("op") == "action.set_attributes",
                before=pos,
                after=after_pos,
                env=wrote,
            )
            value_group = (wrote.get("journal") or {}).get("group")
            back_value = await do(session, ctx, "journal.revert", {"group": value_group})
            restored = await tx_attr(ctx, uid, action, "pos_x", inner=path_node)
            rec(
                ctx,
                "f_revert_of_the_position_write_put_the_old_value_back",
                back_value.get("ok") is True
                and isinstance(restored, dict)
                and abs(restored["value"] - pos["value"]) < 1e-6,
                restored=restored,
                revert=back_value,
            )
        else:
            rec(ctx, "f_the_inner_node_position_is_a_number_to_write", False, pos=pos)

        # revert the three checkpointed writes, newest first: the inner nodes are as before
        back_connect = await revert_and_read(session, ctx, linked)
        after_connect_revert = await tx_world(ctx, uid)
        observe(
            ctx,
            "f_revert_of_the_connect_what_the_transport_reads",
            revertOk=back_connect.get("ok"),
            errorCode=back_connect.get("errorCode"),
            innerAfter=(find(after_connect_revert, action) or {}).get("inner"),
        )
        back_shape = await revert_and_read(session, ctx, made[1]["env"])
        back_path = await revert_and_read(session, ctx, made[0]["env"])
        after = await tx_world(ctx, uid)
        rec(
            ctx,
            "f_reverts_brought_the_inner_nodes_and_the_fingerprint_back",
            back_connect.get("ok") is True
            and back_shape.get("ok") is True
            and back_path.get("ok") is True
            and inner_names(after, action) == inner0
            and await plain_fp(session, ctx, "F") == fp0
            and node_names(after) == node_names(world),
            reverts=[back_connect, back_shape, back_path],
            innerNow=inner_names(after, action),
            innerBefore=inner0,
        )


async def stage_z(ctx: Ctx) -> None:
    async with server_session(ctx, "z-plain") as session:
        info = await version_info(session, ctx)
        journal = _result_of(info).get("journal", {})
        rec(
            ctx,
            "z_nothing_is_left_undecided_and_no_test_hook_is_active",
            journal.get("unresolvedIntents") == 0
            and journal.get("writesBlocked") is False
            and journal.get("testHooks") == [],
            journal=journal,
        )
        listing = await do(session, ctx, "journal.list", {"limit": 80})
        ctx.results["journalListing"] = _result_of(listing).get("items")
    check_envelope_shapes(ctx.results)
    check_logs_and_crash(ctx.results)
    save(ctx.results)


def print_observations(results: Results) -> None:
    print("--- observations (nothing here is a pass/fail) ---")
    for obs in results.get("observations", []):
        name = obs.get("name")
        print(f"{name}: { {k: v for k, v in obs.items() if k not in ('name', 'stage')} }")


def cleanup(ctx: Ctx) -> None:
    ctx.stage = "cleanup"
    ctx.results["cleanup"] = {
        "deleted": [],
        "kept": {
            "batchGroups": ctx.ids.get("groups"),
            "checkpointFiles": str(ctx.journal_dir / "checkpoints"),
            "setupFiles": str(OUTPUT_DIR / f"node_setup_e2e_{ctx.ts}"),
            "reason": (
                "Batch groups are not deleted (this run never calls flame.delete on a PyBatch; "
                "p2_4_clean_probe_project.py removes them); checkpoint and setup files are kept"
            ),
        },
    }
    exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label="p23b_cleanup_exit")
    rec(
        ctx,
        "cleanup_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
    )


async def async_main(ctx: Ctx) -> None:
    try:
        await guarded(ctx, "prepare", prepare)
        if "groups" in ctx.ids:
            for name, stage in (
                ("a", stage_a),
                ("b", stage_b),
                ("c", stage_c),
                ("d", stage_d),
                ("e", stage_e),
                ("f", stage_f),
            ):
                await guarded(ctx, name, stage)
        else:
            rec(ctx, "prepare_incomplete_stages_a_to_f_skipped", False, ids=ctx.ids)
        await guarded(ctx, "z", stage_z)
    finally:
        cleanup(ctx)
        save_stage(ctx)
        save(ctx.results)


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
    journal_dir = OUTPUT_DIR / f"journal_p23b_e2e_{ts}"
    journal_dir.mkdir(parents=True, exist_ok=True)
    results: Results = {
        "ok": True,
        "steps": [],
        "servers": [],
        "observations": [],
        "journalDir": str(journal_dir),
        "_out_path": OUTPUT_DIR / f"p2_3b_node_action_e2e_{ts}.json",
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
