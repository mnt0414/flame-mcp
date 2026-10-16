"""Helpers shared by the two real-Flame e2e scripts (`p2_4_scenario4_e2e.py`,
`p2_4_stress_e2e.py`). Same scaffolding as scripts/p2_3c_render_e2e.py (imported from p2_2a / p1):
`flame_mcp.transport` is used only to create the Batch groups and to READ STATE BACK
independently of the ops under test.

Nothing here talks to the MCP server: the scripts own their `server_session` calls, so a fake
harness can substitute it in one place.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import Any, Final

from p1_mcp_e2e import RUN_DIR_DEFAULT, _br_dict, _tx
from p2_2a_journal_e2e import Ctx, rec, tx

from flame_mcp.journal import Index, Journal

OUTPUT_DIR: Final[Path] = Path.home() / "Library" / "Application Support" / "flame-mcp" / "p2"
EXIT_RETRY_EVERY_S: Final[float] = 3.0
EXIT_RETRY_TOTAL_S: Final[float] = 180.0
"""flame.exit() is retried this often, for this long, while the bridge answers
BRIDGE_UNAVAILABLE (a Foreground render may still be running)."""
GOOD_JOURNAL_STATES: Final[frozenset[str]] = frozenset({"landed", "reverted"})

Results = dict[str, Any]

__all__ = [
    "GOOD_JOURNAL_STATES",
    "OUTPUT_DIR",
    "RUN_DIR_DEFAULT",
    "Results",
    "flame_exit",
    "journal_records",
    "journal_states",
    "local_list",
    "locate",
    "observe",
    "ops_used",
    "print_observations",
    "summarize_ms",
    "tx_is_current",
    "tx_list_dir",
    "tx_scaffold",
    "value_like",
]


def locate(uid: str) -> str:
    return "\n".join(
        [
            "_ws = flame.projects.current_project.current_workspace",
            f"_bg = [g for g in _ws.desktop.batch_groups if _v(g.uid) == {uid!r}][0]",
        ]
    )


async def tx_scaffold(ctx: Ctx, uid: str, body: str, label: str, timeout_s: float = 40.0) -> Any:
    br = await tx(ctx, locate(uid) + "\n" + body, label=label, timeout_s=timeout_s)
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_is_current(ctx: Ctx, uid: str) -> Any:
    code = (
        "_cur = _v(flame.projects.current_project.current_workspace.desktop.current_batch_group)\n"
        f"_result = _cur is not None and _v(_cur.uid) == {uid!r}"
    )
    br = await tx(ctx, code, label="p24_is_current")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


async def tx_list_dir(ctx: Ctx, path: str) -> Any:
    code = (
        "import os\n"
        f"_p = {path!r}\n"
        "_result = sorted([[n, os.path.getsize(os.path.join(_p, n))] for n in os.listdir(_p)]) "
        "if os.path.isdir(_p) else None"
    )
    br = await tx(ctx, code, label="p24_ls")
    return br.result if br.ok else f"<transport error {br.error_code}: {br.error}>"


def local_list(path: Path) -> list[str] | None:
    return sorted(os.listdir(path)) if path.is_dir() else None


def value_like(current: Any, wanted: Any) -> Any:
    """`wanted` in the type of the value the attribute holds now (an integer attribute refuses a
    float)."""
    if isinstance(current, bool):
        return bool(wanted)
    if isinstance(current, int):
        return int(wanted)
    if isinstance(current, float):
        return float(wanted)
    return str(wanted)


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


def journal_records(ctx: Ctx) -> list[dict[str, Any]]:
    return Journal(ctx.journal_dir).read_all().records


def journal_states(ctx: Ctx) -> dict[str, str]:
    """{journal group: state} for every group in this run's own journal directory."""
    index = Index(Journal(ctx.journal_dir).read_all().records)
    return {group: index.group_state(group) for group in index.groups}


def ops_used(results: Results) -> list[str]:
    """Registered op names this run called directly (from the envelopes it recorded)."""
    names = {
        (entry["env"].get("execution") or {}).get("op")
        for entry in results.get("_envelopes", [])
        if entry.get("tool") == "flame_do" and isinstance(entry.get("env"), dict)
    }
    return sorted(n for n in names if isinstance(n, str))


def summarize_ms(values: list[float]) -> dict[str, Any]:
    """count / min / median / p95 (nearest rank) / max / mean, in the unit of `values`."""
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None, "mean": None}
    ordered = sorted(values)
    count = len(ordered)

    p95_index = min(count - 1, max(0, math.ceil(0.95 * count) - 1))
    middle = count // 2
    median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {
        "count": count,
        "min": ordered[0],
        "median": median,
        "p95": ordered[p95_index],
        "max": ordered[-1],
        "mean": round(sum(ordered) / count, 3),
    }


def print_observations(results: Results) -> None:
    print("--- observations (nothing here is a pass/fail) ---")
    for obs in results.get("observations", []):
        name = obs.get("name")
        print(f"{name}: { {k: v for k, v in obs.items() if k not in ('name', 'stage')} }")


def flame_exit(ctx: Ctx, label: str) -> None:
    """flame.exit(), retried every EXIT_RETRY_EVERY_S for up to EXIT_RETRY_TOTAL_S while the
    bridge answers BRIDGE_UNAVAILABLE. BRIDGE_LOST / TIMEOUT / no error all mean it went down."""
    ctx.stage = "cleanup"
    deadline = time.monotonic() + EXIT_RETRY_TOTAL_S
    attempts: list[dict[str, Any]] = []
    while True:
        exit_br = _tx("flame.exit()", ctx.run_dir, timeout_s=5.0, label=label)
        attempts.append({"at": time.strftime("%H:%M:%S"), "errorCode": exit_br.error_code})
        if exit_br.error_code != "BRIDGE_UNAVAILABLE" or time.monotonic() >= deadline:
            break
        time.sleep(EXIT_RETRY_EVERY_S)
    rec(
        ctx,
        "cleanup_flame_exit",
        exit_br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        resp=_br_dict(exit_br),
        attempts=attempts,
    )
