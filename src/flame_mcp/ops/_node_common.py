"""Shared pieces of the `node` and `action` write ops: the note every checkpointed
description carries, the selector plumbing that lets a `node` / `action_node` selector name the
Batch group to checkpoint (`CheckpointSpec.group_selector` / `rebind`), and the pre-checkpoint
checks (a typo or a refused precondition should leave no checkpoint and no journal group)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from flame_mcp.checkpointed import RESTORE_SIDE_EFFECTS
from flame_mcp.types import Refusal

CHECKPOINTED_NODE_NOTE: Final[str] = (
    "Runs behind an automatic checkpoint: the node's Batch group is saved first "
    "(batch.checkpoint), an intent is journaled, the call runs, and the outcome is journaled "
    "with the group's fingerprint. The envelope's `journal` = {group, id, checkpoint, "
    "checkpointPath, revert, changed}; journal.revert(group) restores the checkpoint while the "
    "group's fingerprint is still the one the call left (else CONFLICT; force=true overrides, "
    "journaled). The fingerprint -- and so `changed` and that comparison -- covers node names, "
    "types, wiring and the inner nodes (names and types) of Action / Image / GMask Tracer "
    "nodes; it does NOT see attribute values, positions, links between inner nodes, reels, "
    "shelf reels or iterations, so `changed` reads false for a change made only of those. "
    "Native Undo is not used. "
    "Not allowed in plan.run (it owns its journal group). " + RESTORE_SIDE_EFFECTS + " "
)

ROLE_TARGET: Final[str] = "the target"

SelectorMap = Mapping[str, Any]


def batch_of_node(node_selector: SelectorMap) -> SelectorMap:
    """The `batch` selector of a `node` selector."""
    batch: SelectorMap = node_selector["batch"]
    return batch


def batch_of_action_node(inner_selector: SelectorMap) -> SelectorMap:
    """The `batch` selector of an `action_node` selector (its `node`'s `batch`)."""
    return batch_of_node(inner_selector["node"])


def rebind_node(node_selector: SelectorMap, stable: dict[str, Any]) -> dict[str, Any]:
    return {**node_selector, "batch": stable}


def rebind_action_node(inner_selector: SelectorMap, stable: dict[str, Any]) -> dict[str, Any]:
    return {**inner_selector, "node": rebind_node(inner_selector["node"], stable)}


def rebind_params(
    rebind_one: Callable[[SelectorMap, dict[str, Any]], dict[str, Any]], *names: str
) -> Callable[[SelectorMap, dict[str, Any]], dict[str, Any]]:
    """`CheckpointSpec.rebind` for an op whose parameters `names` each hold a selector of the
    same group: every one of them is re-pointed at the checkpointed group."""

    def rebind(args: SelectorMap, stable: dict[str, Any]) -> dict[str, Any]:
        return {**args, **{name: rebind_one(args[name], stable) for name in names}}

    return rebind


def refusal_from(env: Mapping[str, Any], what: str) -> Refusal:
    """A read that failed before the checkpoint, as the refusal of the write."""
    return Refusal(
        env.get("errorCode") or "FLAME_API_ERROR",
        f"nothing was run: {what} ({env.get('error')})",
        hint=env.get("hint"),
        result={"ran": False},
    )


def read_node(rt: Any, selector: SelectorMap, role: str) -> tuple[Refusal | None, dict[str, Any]]:
    """`node.info` of `selector`: the refusal when it cannot be read, else its result (which
    carries `stable` and `sockets`)."""
    env = rt.do("node.info", {"target": dict(selector)})
    if env.get("ok") is not True:
        return refusal_from(env, f"{role} could not be found"), {}
    result = env.get("result")
    return None, dict(result) if isinstance(result, dict) else {}


def read_action(rt: Any, selector: SelectorMap, role: str) -> tuple[Refusal | None, dict[str, Any]]:
    """`action.list_nodes` of `selector` (a `node` selector): the refusal when it cannot be read
    (not there, or not an Action / Image / GMask Tracer node), else its result."""
    env = rt.do("action.list_nodes", {"target": dict(selector)})
    if env.get("ok") is not True:
        return refusal_from(env, f"{role} could not be read"), {}
    result = env.get("result")
    return None, dict(result) if isinstance(result, dict) else {}


def read_inner(rt: Any, selector: SelectorMap, role: str) -> tuple[Refusal | None, dict[str, Any]]:
    """`action.get_attributes` (keys=[name]) of an `action_node` selector: the refusal when it
    cannot be found, else its result (which carries `stable`)."""
    env = rt.do("action.get_attributes", {"target": dict(selector), "keys": ["name"]})
    if env.get("ok") is not True:
        return refusal_from(env, f"{role} could not be found"), {}
    result = env.get("result")
    return None, dict(result) if isinstance(result, dict) else {}


NODE_LIST_PAGE: Final[int] = 500
NODE_LIST_MAX_PAGES: Final[int] = 20


def group_node_names(rt: Any, batch_selector: SelectorMap) -> tuple[Refusal | None, list[str]]:
    """The names of every node of a Batch group, read page by page through `node.list`."""
    names: list[str] = []
    for page in range(NODE_LIST_MAX_PAGES):
        env = rt.do(
            "node.list",
            {
                "target": dict(batch_selector),
                "limit": NODE_LIST_PAGE,
                "offset": page * NODE_LIST_PAGE,
            },
        )
        if env.get("ok") is not True:
            return refusal_from(env, "the group's nodes could not be listed"), names
        result = env.get("result") or {}
        names += [str(row.get("name")) for row in result.get("items") or []]
        if not result.get("truncated"):
            break
    return None, names


SIMILAR_TYPES: Final[int] = 10


def canonical_type(types: list[str], wanted: str) -> tuple[str | None, list[str]]:
    """`(the spelling `types` lists, every case-insensitive match)`. An exact spelling wins;
    otherwise the one type that matches ignoring case (Flame takes `create_node('Mux')` for the
    listed 'MUX'). None when nothing matches or several do."""
    if wanted in types:
        return wanted, [wanted]
    low = wanted.lower()
    hits = [t for t in types if t.lower() == low]
    return (hits[0] if len(hits) == 1 else None), hits


def type_refusal(types: list[str], wanted: str, hits: list[str], what: str, lister: str) -> Refusal:
    """INVALID_ARGS for a type `canonical_type` could not settle, with candidates."""
    if len(hits) > 1:
        return Refusal(
            "INVALID_ARGS",
            f"nothing was run: {wanted!r} matches {len(hits)} {what}s ignoring case {hits}; "
            "give the exact spelling",
            result={"ran": False, "similar": hits},
        )
    low = wanted.lower()
    similar = [t for t in types if low in t.lower() or t.lower() in low][:SIMILAR_TYPES]
    return Refusal(
        "INVALID_ARGS",
        f"nothing was run: {wanted!r} is not a valid {what} ({len(types)} listed by "
        f"{lister}; case does not matter)",
        result={"ran": False, "similar": similar},
    )
