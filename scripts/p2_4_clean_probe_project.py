#!/usr/bin/env python3
"""Deletes only what the tests made in the Flame test project (flame_mcp_probe by default).

Run with the `.venv` Python.

Usage: scripts/run_flame_with_bridge.sh scripts/p2_4_clean_probe_project.py 900 -- --mode dry-run
       scripts/run_flame_with_bridge.sh scripts/p2_4_clean_probe_project.py 900 -- --mode apply

It acts only on the libraries and the Desktop (Batch groups and reel groups) of
current_project.current_workspace. It does not list, switch, create, delete or open projects,
and does not touch shared libraries or files on disk (flame.projects is read only for
current_project).

What is deleted and what is kept (decided by name):
  Only an object whose name has the form "prefix the tests use (TEST_PREFIXES) + _ + time
  (YYMMDD_HHMMSS)" is deleted. Everything else is kept and listed under "untouched" in the plan
  (the default libraries Default Library, Grabbed References and Timeline FX, and any other
  library, are kept when the name does not match).
  1 Libraries        Delete only those whose name matches TEST_NAME.
  2 Batch groups     If any name matches TEST_NAME, first create one empty
                     "P2C_CLEAN_<time>" (it has a test name, so the next clean removes it and
                     only one empty Batch group stays). create_batch_group moves the current
                     group to the new one. Then delete only the matching ones.
  3 Desktop reel groups
                     Delete only those whose name matches TEST_NAME and that do not contain a
                     name starting with "U5S" (a group with "U5S" is kept because
                     find_gap_sequence in scripts/p1_mcp_e2e.py uses it). If no reel group
                     would remain, first create one empty reel group. The inside of a kept
                     reel group is not touched.

Safety checks (if one fails, nothing is deleted or created and the run stops; the same in
dry-run):
  - The bridge's hello reports projectName == the test project (client side).
  - Inside Flame, current_project.name == the test project (checked at the start of every exec;
    if it fails, nothing is done).
  - The delete loop checks current_project.name again for every item.
  - An object found by uid is deleted only if its name is the same as when the plan was made and
    it matches TEST_NAME. What must be kept (protected uids, reel groups with "U5S", the Batch
    group just created, names that do not match a prefix) is rejected again on the Flame side.
  - When it stops after the project check passed, it still calls flame.exit() (otherwise the
    runner would wait for Flame to exit forever). When the project is not the test project (the
    hello, or current_project.name inside Flame at the start of an exec or during the delete loop),
    or the bridge was lost, it does not call flame.exit().

dry-run: print what would be deleted, kept and created, as a count and names (the first 30)
for each kind. Nothing is deleted or created.
apply: delete one by one and record the result (True / exception). One exec handles at most 20
items and its timeout is 120 s. Record the name of the current Batch group and the counts before
and after each stage, and read the lists again at the end to record what remains. Stop at the
first TIMEOUT, BRIDGE_* error or abort inside Flame (the re-read is still done when possible).

The result is written to ~/Library/Application Support/flame-mcp/p2/p2_4_clean_<mode>_<ts>.json
(saved after every stage).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from e2e_guard import (  # noqa: E402
    DEFAULT_WAIT_SOCKET_S,
    EXIT_TIMEOUT_S,
    FATAL_ERROR_CODES,
    OUTPUT_DIR,
    RUN_DIR_DEFAULT,
    STAGE_WAIT_S,
    _br_dict,
    _save,
    _wait_for_bridge,
    probe_project_name,
)

from flame_mcp import transport  # noqa: E402

TARGET_PROJECT = probe_project_name()
TEST_PREFIXES = (
    "P1E",
    "P1E3",
    "P1O",
    "P21",
    "P22A",
    "P22B",
    "P22C",
    "P23",
    "P23A",
    "P23B",
    "P23C",
    "P23D",
    "P23O",
    "P23R",
    "P24R",
    "P24S4",
    "P24ST",
    "P26R",
    "P2B",
    "P2C",
    "P2D",
    "P2N",
    "P2Q",
    "P2R",
    "P2S",
    "P2T",
    "P30",
    "P30E",
    "P30X",
    "P31A",
    "P31B",
    "P32A",
    "P32B",
    "P33",
    "P34",
    "P34E",
    "P35",
    "PFX2",
    "PFX3",
    "PFX3E",
    "PF7E",
    "PR12",
    "PR12E",
    "PR5E",
    "PTW3",
    "PTW3E",
    "PW7",
    "UNDO",
)
"""Prefixes of the names the tests create (collected by grepping the names used by the e2e
scripts in scripts/). A name is `<prefix>_<...>_<YYMMDD_HHMMSS>` or
`<prefix>_<YYMMDD_HHMMSS>_<...>` (for example P23A_B1_261001_123456, P32A_261001_123456_CKPT,
P1O_RG_261001_123456). The number Flame adds to a duplicate name (" 1" and the like) may follow."""
TEST_NAME_PATTERN = (
    r"^(?:"
    + "|".join(sorted(TEST_PREFIXES, key=len, reverse=True))
    + r")_(?:[A-Za-z0-9]+_)*\d{6}_\d{6}(?:[ _][A-Za-z0-9_ ]*)?$"
)
TEST_NAME = re.compile(TEST_NAME_PATTERN)
KEEP_REEL_GROUP_PREFIX = "U5S"
# named like a test so that the next clean removes it (one empty group is kept, not one per clean)
NEW_BATCH_GROUP_NAME = "P2C_CLEAN_" + time.strftime("%y%m%d_%H%M%S")
NEW_REEL_GROUP_NAME = "Reel Group"
CHUNK_SIZE = 20
INVENTORY_TIMEOUT_S = 120.0
CREATE_TIMEOUT_S = 60.0
DELETE_TIMEOUT_S = 120.0
NAMES_SHOWN = 30
EXIT_RETRY_INTERVAL_S = 3.0
EXIT_RETRY_TOTAL_S = 180.0
MODES = ("dry-run", "apply")

CLEAN_PRELUDE = """\
def _v(x):
    return x.get_value() if type(x).__name__ == 'PyAttribute' else x


def _try(fn):
    try:
        return fn()
    except Exception as e:
        return 'ERR:' + repr(e)


def _nm(o):
    return _try(lambda: _v(o.name))


def _uid(o):
    return _try(lambda: _v(o.uid))


def _project_name():
    return _try(lambda: _v(flame.projects.current_project.name))


def _ws():
    return flame.projects.current_project.current_workspace


def _snap():
    ws = _ws()
    desk = ws.desktop
    cur = _try(lambda: _v(desk.current_batch_group))
    return {
        'project': _project_name(),
        'current_batch': None if cur is None else _nm(cur),
        'current_batch_uid': None if cur is None else _uid(cur),
        'n_libraries': _try(lambda: len(list(ws.libraries))),
        'n_batch_groups': _try(lambda: len(list(desk.batch_groups))),
        'n_reel_groups': _try(lambda: len(list(desk.reel_groups))),
    }


def _plain(x):
    return x if x is None or isinstance(x, (bool, int, float, str)) else repr(x)
"""

INVENTORY_BODY = """\
_PREFIX = {prefix!r}


def _stage():
    ws = _ws()
    desk = ws.desktop
    libs = [{{'name': _nm(o), 'uid': _uid(o)}} for o in ws.libraries]
    bgs = [{{'name': _nm(o), 'uid': _uid(o)}} for o in desk.batch_groups]
    rgs = []
    for rg in desk.reel_groups:
        n_reels = 0
        n_children = 0
        keep_hits = []
        for reel in rg.reels:
            n_reels += 1
            for c in reel.children:
                n_children += 1
                nm = _nm(c)
                if nm.startswith(_PREFIX):
                    keep_hits.append({{'reel': _nm(reel), 'type': type(c).__name__, 'name': nm}})
        rgs.append({{
            'name': _nm(rg), 'uid': _uid(rg), 'n_reels': n_reels,
            'n_children': n_children, 'keep_hits': keep_hits,
        }})
    crg = _try(lambda: _v(ws.current_reel_group))
    return {{
        'libraries': libs, 'batch_groups': bgs, 'reel_groups': rgs,
        'current_reel_group': None if crg is None else _nm(crg),
    }}
"""

CREATE_BATCH_BODY = """\
def _stage():
    desk = _ws().desktop
    o = desk.create_batch_group({name!r})
    cur = _v(desk.current_batch_group)
    return {{
        'name': _nm(o), 'uid': _uid(o),
        'is_current': cur is not None and _uid(cur) == _uid(o),
    }}
"""

CREATE_REEL_GROUP_BODY = """\
def _stage():
    desk = _ws().desktop
    o = desk.create_reel_group({name!r})
    return {{'name': _nm(o), 'uid': _uid(o)}}
"""

DELETE_BODY = """\
_KIND = {kind!r}
_ITEMS = {items!r}
_NAME_PATTERN = {name_pattern!r}
_KEEP_UIDS = {keep_uids!r}
_PREFIX = {prefix!r}


def _is_test_name(name):
    import re
    return isinstance(name, str) and re.match(_NAME_PATTERN, name) is not None


def _collection():
    ws = _ws()
    if _KIND == 'library':
        return ws.libraries
    if _KIND == 'batch':
        return ws.desktop.batch_groups
    return ws.desktop.reel_groups


def _has_keep_hit(rg):
    for reel in rg.reels:
        for c in reel.children:
            if _nm(c).startswith(_PREFIX):
                return True
    return False


def _stage():
    wanted = set(i['uid'] for i in _ITEMS)
    by_uid = {{}}
    for o in _collection():
        u = _uid(o)
        if u in wanted:
            by_uid[u] = o
    cur_uid = None
    if _KIND == 'batch':
        cur = _v(_ws().desktop.current_batch_group)
        cur_uid = None if cur is None else _uid(cur)
        if cur_uid not in _KEEP_UIDS:
            return {{'aborted': 'current_batch_is_not_the_new_group', 'current_uid': cur_uid,
                     'rows': []}}
    rows = []
    for item in _ITEMS:
        row = {{'uid': item['uid'], 'name': item['name']}}
        rows.append(row)
        if _project_name() != _TARGET:
            row['skipped'] = 'project_changed'
            return {{'aborted': 'project_changed', 'rows': rows}}
        o = by_uid.get(item['uid'])
        if o is None:
            row['skipped'] = 'not_found'
        elif _nm(o) != item['name']:
            row['skipped'] = 'name_changed'
            row['nameNow'] = _nm(o)
        elif item['uid'] in _KEEP_UIDS:
            row['skipped'] = 'protected_uid'
        elif not _is_test_name(item['name']):
            row['skipped'] = 'not_a_test_name'
        elif _KIND == 'batch' and item['uid'] == cur_uid:
            row['skipped'] = 'is_current'
        elif _KIND == 'reel_group' and _has_keep_hit(o):
            row['skipped'] = 'has_keep_hit'
        else:
            try:
                row['result'] = _plain(flame.delete(o, confirm=False))
            except Exception as e:
                row['error'] = repr(e)
    return {{'rows': rows}}
"""


def stage_code(body: str) -> str:
    """Every exec starts with the project check; the stage body runs only when it holds."""
    return "\n".join(
        [
            CLEAN_PRELUDE,
            f"_TARGET = {TARGET_PROJECT!r}",
            body,
            "_pn = _project_name()",
            "if _pn != _TARGET:",
            "    _result = {'aborted': 'project_mismatch', 'project': _pn}",
            "else:",
            "    _R = {'before': _snap()}",
            "    _R['data'] = _stage()",
            "    _R['after'] = _snap()",
            "    _result = _R",
        ]
    )


def inventory_code() -> str:
    return stage_code(INVENTORY_BODY.format(prefix=KEEP_REEL_GROUP_PREFIX))


def create_batch_code() -> str:
    return stage_code(CREATE_BATCH_BODY.format(name=NEW_BATCH_GROUP_NAME))


def create_reel_group_code() -> str:
    return stage_code(CREATE_REEL_GROUP_BODY.format(name=NEW_REEL_GROUP_NAME))


def delete_code(
    kind: str,
    items: Sequence[dict[str, str]],
    keep_uids: Sequence[str],
) -> str:
    body = DELETE_BODY.format(
        kind=kind,
        items=[{"uid": i["uid"], "name": i["name"]} for i in items],
        name_pattern=TEST_NAME_PATTERN,
        keep_uids=list(keep_uids),
        prefix=KEEP_REEL_GROUP_PREFIX,
    )
    return stage_code(body)


# plan (pure)
def _readable(entry: dict[str, Any]) -> bool:
    return not any(
        isinstance(entry.get(k), str) and str(entry[k]).startswith("ERR:") for k in ("name", "uid")
    )


def _listing(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(entries), "names": [e["name"] for e in entries[:NAMES_SHOWN]]}


def _names(entries: Sequence[dict[str, Any]]) -> list[Any]:
    return [e["name"] for e in entries]


def build_plan(inv: dict[str, Any]) -> dict[str, Any]:
    libs = inv["libraries"]
    bgs = inv["batch_groups"]
    rgs = inv["reel_groups"]

    def is_test(entry: dict[str, Any]) -> bool:
        return _readable(entry) and TEST_NAME.match(entry["name"]) is not None

    lib_del = [e for e in libs if is_test(e)]
    lib_untouched = [e for e in libs if e not in lib_del]
    bg_del = [e for e in bgs if is_test(e)]
    bg_untouched = [e for e in bgs if e not in bg_del]
    rg_del = [e for e in rgs if is_test(e) and not e["keep_hits"]]
    rg_untouched = [e for e in rgs if e not in rg_del]
    rg_keep = [e for e in rgs if _readable(e) and e["keep_hits"]]
    unreadable = {
        "libraries": [e for e in libs if not _readable(e)],
        "batch_groups": [e for e in bgs if not _readable(e)],
        "reel_groups": [e for e in rgs if not _readable(e)],
    }
    return {
        "libraries": {"delete": _listing(lib_del)},
        "batchGroups": {
            "create": [NEW_BATCH_GROUP_NAME] if bg_del else [],
            "delete": _listing(bg_del),
        },
        "reelGroups": {
            "keep": {
                **_listing(rg_keep),
                "hits": {e["name"]: e["keep_hits"][:NAMES_SHOWN] for e in rg_keep[:NAMES_SHOWN]},
            },
            "create": [NEW_REEL_GROUP_NAME] if rg_del and not rg_untouched else [],
            "delete": _listing(rg_del),
        },
        "untouched": {
            "libraries": _names(lib_untouched),
            "batchGroups": _names(bg_untouched),
            "reelGroups": _names(rg_untouched),
        },
        "unreadable": {k: len(v) for k, v in unreadable.items()},
        "unreadableEntries": unreadable,
        "full": {
            "libraryDelete": lib_del,
            "batchDelete": bg_del,
            "reelGroupUntouched": rg_untouched,
            "reelGroupDelete": rg_del,
        },
    }


def chunks(items: Sequence[dict[str, Any]], size: int = CHUNK_SIZE) -> list[list[dict[str, Any]]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


class Runner:
    def __init__(self, run_dir: Path, results: dict[str, Any], out_path: Path) -> None:
        self.run_dir = run_dir
        self.results = results
        self.out_path = out_path
        self.stop: str | None = None
        self.lost = False
        self.wrong_project = False

    def halt(self, reason: str) -> None:
        """Keeps the FIRST reason in `stop`; every reason goes to `stopLog`."""
        self.results.setdefault("stopLog", []).append(reason)
        self.stop = self.stop or reason

    def save(self) -> None:
        _save(self.results, self.out_path)

    def call(self, sid: str, title: str, code: str, timeout_s: float) -> dict[str, Any] | None:
        """Runs one exec and records it. Sets `self.stop` and returns None when the run must not
        go on (transport failure, Flame-side error, project mismatch or an abort inside)."""
        br = transport.exec(
            code, thread="main", timeout_s=timeout_s, label=sid, run_dir=self.run_dir
        )
        self.results["stages"][sid] = {"title": title, "resp": _br_dict(br)}
        self.save()
        time.sleep(STAGE_WAIT_S)
        if not br.ok:
            self.halt(f"{sid}: {br.error_code}: {br.error}")
            self.lost = br.error_code in FATAL_ERROR_CODES
            return None
        res = br.result
        if not isinstance(res, dict):
            self.halt(f"{sid}: unexpected result {res!r}")
            return None
        if res.get("aborted"):
            self.halt(f"{sid}: aborted: {res['aborted']} (project={res.get('project')!r})")
            self.wrong_project = self.wrong_project or res["aborted"] == "project_mismatch"
            return None
        data = res.get("data")
        if isinstance(data, dict) and data.get("aborted"):
            self.halt(f"{sid}: aborted in Flame: {data['aborted']}")
            self.wrong_project = self.wrong_project or data["aborted"] == "project_changed"
            return res
        return res


def _rows(res: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = (res or {}).get("data")
    return list(data.get("rows", [])) if isinstance(data, dict) else []


def _delete_chunks(
    runner: Runner,
    tag: str,
    title: str,
    kind: str,
    items: Sequence[dict[str, Any]],
    keep_uids: Sequence[str],
) -> None:
    done: list[dict[str, Any]] = []
    for n, chunk in enumerate(chunks(items), start=1):
        res = runner.call(
            f"{tag}_{n:03d}",
            f"{title} [{n}]",
            delete_code(kind, chunk, keep_uids),
            DELETE_TIMEOUT_S,
        )
        done.extend(_rows(res))
        if runner.stop:
            break
    runner.results["deleted"][tag] = summarize_rows(done, len(items))


def summarize_rows(rows: Sequence[dict[str, Any]], planned: int) -> dict[str, Any]:
    deleted = [r for r in rows if r.get("result") is True]
    return {
        "planned": planned,
        "attempted": len(rows),
        "deleted": len(deleted),
        "skipped": {r["name"]: r["skipped"] for r in rows if "skipped" in r},
        "errors": {r["name"]: r["error"] for r in rows if "error" in r},
        "otherResults": {
            r["name"]: r["result"] for r in rows if "result" in r and r["result"] is not True
        },
        "rows": list(rows),
    }


def _summarize_inventory(inv: dict[str, Any]) -> dict[str, Any]:
    return {
        "libraries": [e["name"] for e in inv["libraries"]],
        "batchGroups": _listing(inv["batch_groups"]),
        "reelGroups": [
            {"name": e["name"], "keepHits": len(e["keep_hits"])} for e in inv["reel_groups"]
        ],
        "currentReelGroup": inv.get("current_reel_group"),
    }


def _inventory(runner: Runner, sid: str, title: str) -> dict[str, Any] | None:
    res = runner.call(sid, title, inventory_code(), INVENTORY_TIMEOUT_S)
    data = (res or {}).get("data")
    return data if isinstance(data, dict) and "libraries" in data else None


def _check_final(
    inv: dict[str, Any], plan: dict[str, Any], created: dict[str, Any]
) -> dict[str, Any]:
    untouched = plan["untouched"]
    keep_rg = {e["uid"] for e in plan["full"]["reelGroupUntouched"]}
    if created.get("reelGroupUid"):
        keep_rg.add(created["reelGroupUid"])
    batch_names_now = [e["name"] for e in inv["batch_groups"]]
    return {
        "note": "Facts only (does the result match the plan?); the verdict is left to the reader",
        "libraryNames": [e["name"] for e in inv["libraries"]],
        "librariesUntouchedAllThere": all(
            n in [e["name"] for e in inv["libraries"]] for n in untouched["libraries"]
        ),
        "librariesNoTestNameLeft": not any(TEST_NAME.match(e["name"]) for e in inv["libraries"]),
        "batchGroupNames": batch_names_now,
        "batchGroupsUntouchedAllThere": all(n in batch_names_now for n in untouched["batchGroups"]),
        "batchGroupsNoTestNameLeft": not any(TEST_NAME.match(n) for n in batch_names_now),
        "reelGroupsOnlyKept": all(e["uid"] in keep_rg for e in inv["reel_groups"]),
        "keptReelGroupsStillThere": sorted(keep_rg - {e["uid"] for e in inv["reel_groups"]}) == [],
    }


def run_apply(runner: Runner, plan: dict[str, Any]) -> dict[str, Any]:
    results = runner.results
    results["deleted"] = {}
    created: dict[str, Any] = {}
    results["created"] = created
    full = plan["full"]

    _delete_chunks(runner, "lib", "libraries", "library", full["libraryDelete"], [])
    if runner.stop:
        return created

    if plan["batchGroups"]["create"]:
        res = runner.call(
            "batch_create", "create Batch group", create_batch_code(), CREATE_TIMEOUT_S
        )
        data = (res or {}).get("data")
        if runner.stop or not isinstance(data, dict):
            return created
        created["batchName"] = data.get("name")
        created["batchUid"] = data.get("uid")
        created["batchIsCurrent"] = data.get("is_current")
        if not data.get("is_current") or not data.get("uid"):
            runner.halt(
                "batch_create: the new Batch group is not current; not deleting Batch groups"
            )
            return created
        _delete_chunks(
            runner, "batch", "batch groups", "batch", full["batchDelete"], [created["batchUid"]]
        )
        if runner.stop:
            return created

    keep_uids = [e["uid"] for e in full["reelGroupUntouched"]]
    if plan["reelGroups"]["create"]:
        res = runner.call(
            "rg_create", "create reel group", create_reel_group_code(), CREATE_TIMEOUT_S
        )
        data = (res or {}).get("data")
        if runner.stop or not isinstance(data, dict) or not data.get("uid"):
            runner.halt("rg_create: no reel group uid; not deleting reel groups")
            return created
        created["reelGroupName"] = data.get("name")
        created["reelGroupUid"] = data["uid"]
        keep_uids.append(data["uid"])
    _delete_chunks(runner, "rg", "reel groups", "reel_group", full["reelGroupDelete"], keep_uids)
    return created


def exit_flame(run_dir: Path, results: dict[str, Any], save: Callable[[], None]) -> None:
    deadline = time.monotonic() + EXIT_RETRY_TOTAL_S
    attempts = 0
    while True:
        attempts += 1
        br = transport.exec(
            "flame.exit()", thread="main", timeout_s=EXIT_TIMEOUT_S, label="exit", run_dir=run_dir
        )
        if br.error_code != "BRIDGE_UNAVAILABLE" or time.monotonic() >= deadline:
            break
        time.sleep(EXIT_RETRY_INTERVAL_S)
    results["exit"] = {
        "attempts": attempts,
        "expectedShape": br.error_code in ("BRIDGE_LOST", "TIMEOUT", None),
        "resp": _br_dict(br),
    }
    save()


def run(run_dir: Path, wait_socket_s: float, mode: str, results: dict[str, Any]) -> None:
    candidate = _wait_for_bridge(run_dir, wait_socket_s)
    results["bridge"] = {
        "pid": candidate.pid,
        "flameVersion": candidate.flame_version,
        "projectName": candidate.project_name,
    }
    ts = time.strftime("%y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"p2_4_clean_{mode}_{ts}.json"
    results["ts"] = ts
    results["mode"] = mode
    results["outPath"] = str(out_path)
    results["targetProject"] = TARGET_PROJECT
    runner = Runner(run_dir, results, out_path)
    is_probe = candidate.project_name == TARGET_PROJECT
    try:
        if is_probe:
            execute(runner, mode)
        else:
            runner.halt(
                f"hello projectName {candidate.project_name!r} != {TARGET_PROJECT!r}: "
                "nothing was run"
            )
    finally:
        results["stopped"] = runner.stop
        runner.save()
    if runner.lost or runner.wrong_project or not is_probe:
        return
    exit_flame(run_dir, results, runner.save)


def execute(runner: Runner, mode: str) -> None:
    results = runner.results
    inv = _inventory(runner, "inventory_before", "inventory (read only)")
    if inv is None or runner.stop:
        return
    plan = build_plan(inv)
    results["before"] = _summarize_inventory(inv)
    results["plan"] = plan
    if mode == "dry-run":
        return
    created = run_apply(runner, plan)
    if runner.lost:
        return
    after = _inventory(runner, "inventory_after", "inventory after (read only)")
    if after is not None:
        results["after"] = _summarize_inventory(after)
        results["verification"] = _check_final(after, plan, created)


def build_summary(results: dict[str, Any]) -> dict[str, Any]:
    plan = results.get("plan") or {}
    shown = {k: v for k, v in plan.items() if k not in ("full", "unreadableEntries")}
    return {
        "mode": results.get("mode"),
        "bridge": results.get("bridge"),
        "stopped": results.get("stopped"),
        "plan": shown,
        "created": results.get("created"),
        "deleted": {
            k: {kk: vv for kk, vv in v.items() if kk != "rows"}
            for k, v in (results.get("deleted") or {}).items()
        },
        "after": results.get("after"),
        "verification": results.get("verification"),
        "exit": (results.get("exit") or {}).get("expectedShape"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, default="dry-run")
    parser.add_argument("--run-dir", default=str(RUN_DIR_DEFAULT))
    parser.add_argument("--wait-socket-s", type=float, default=DEFAULT_WAIT_SOCKET_S)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"ok": True, "stages": {}}
    try:
        run(Path(args.run_dir), args.wait_socket_s, args.mode, results)
    except BaseException:
        results["ok"] = False
        results["fatal_error"] = traceback.format_exc()
        out = results.get("outPath")
        if not isinstance(out, str):
            out = str(OUTPUT_DIR / f"p2_4_clean_{args.mode}_{time.strftime('%y%m%d_%H%M%S')}.json")
            results["outPath"] = out
        _save(results, Path(out))

    print(f"Result: {results.get('outPath')}")
    print(json.dumps(build_summary(results), indent=2, default=str, ensure_ascii=False))
    if "fatal_error" in results:
        print(results["fatal_error"], file=sys.stderr)
    return 0 if results.get("ok") and not results.get("stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
