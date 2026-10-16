"""The stages of the library / Batch-copy real-Flame e2e (scripts/fix2_library_batch_e2e.py):
l (library.create), b (a Batch group into a library), r (a library Batch group back onto the
Desktop), d (the desktop selector), c (journal.check). Everything the ops report is READ BACK
through the transport, never taken from the op's own answer (fix2_e2e_common.py)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from fix2_e2e_common import (  # noqa: E402
    ADD_NODE_CODE,
    DELETE_GROUP_CODE,
    LIB_OPEN_STATE_CODE,
    LIB_SET_OPEN_CODE,
    LIBS_OPENED_CODE,
    PUT_CLIP_CODE,
    RENAME_LIB_BATCH_CODE,
    SET_CURRENT_CODE,
    TOGGLE_BYPASS_CODE,
    check_group,
    err_text,
    expect_error,
    groups_now,
    name_of,
    revert,
    run_call,
    same_shape,
    synthetic_open_intent,
    tx_clip_names,
    tx_current,
    tx_group,
    tx_lib_batches,
    tx_libraries,
)
from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_1a_read_e2e import fill, is_error, items_of, tx_val  # noqa: E402
from p3_1b_e2e_common import FOLDER_CODE, MAKE_REEL_CODE  # noqa: E402


def base_name(name: str) -> str:
    """Flame renames a taken name `X` -> `X 1` and `X1` -> `X2`: the base is what both share."""
    return re.sub(r"\s?\d+$", "", name)


def names_in(libs: list[list[Any]]) -> list[str]:
    return [row[0] for row in libs]


def row_of(libs: list[list[Any]], name: str) -> list[Any] | None:
    return next((row for row in libs if row[0] == name), None)


def result(env: dict[str, Any]) -> dict[str, Any]:
    return _result_of(env)


def first_copy(env: dict[str, Any]) -> dict[str, Any]:
    copies = result(env).get("copies")
    return copies[0] if isinstance(copies, list) and copies and isinstance(copies[0], dict) else {}


async def unchanged_sources(ctx: Ctx) -> dict[str, bool]:
    ids = ctx.ids
    out: dict[str, bool] = {}
    for key in ("bgn", "bgc"):
        now = await tx_group(ctx, ids[f"{key}Uid"])
        was = ids["original"].get(key)
        out[key] = (
            isinstance(now, dict) and isinstance(was, dict) and all(same_shape(was, now).values())
        )
    return out


# -- l: library.create ---------------------------------------------------------------------------


async def stage_l(ctx: Ctx) -> None:
    ids = ctx.ids
    state_of_all = await tx_val(ctx, LIBS_OPENED_CODE, "fix2_libs_opened")
    observe(
        ctx,
        "l0_opened_of_every_library_including_the_default_ones",
        libraries=state_of_all,
        allReadable=isinstance(state_of_all, list)
        and all(isinstance(row[1], bool) for row in state_of_all),
    )
    async with server_session(ctx, "fix2-l") as session:
        before = await tx_libraries(ctx)
        l1 = await run_call(session, ctx, "l1_create_library")
        libs = await tx_libraries(ctx)
        row = row_of(libs, ids["lib1Name"])
        rec(
            ctx,
            "l1_the_library_is_created_empty_and_listed_once",
            l1.get("ok") is True
            and row is not None
            and row[3] == 0
            and names_in(libs).count(ids["lib1Name"]) == 1
            and len(libs) == len(before) + 1
            and result(l1).get("name") == ids["lib1Name"]
            and result(l1).get("renamed") is False,
            error=l1.get("error"),
            row=row,
        )
        rec(
            ctx,
            "l1_the_selector_it_returns_is_the_wiretap_id_of_that_library",
            row is not None and (result(l1).get("selector") or {}).get("id") == row[2],
            selector=result(l1).get("selector"),
        )
        groups = groups_now(ctx)
        l2 = await run_call(session, ctx, "l2_create_taken_name")
        libs2 = await tx_libraries(ctx)
        rec(
            ctx,
            "l2_a_taken_name_is_refused_and_nothing_is_made_or_journaled",
            expect_error(l2, "PRECONDITION_UNMET")
            and ids["lib1Name"] in (result(l2).get("existingNames") or [])
            and libs2 == libs
            and groups_now(ctx) == groups,
            error=err_text(l2),
            existingNames=result(l2).get("existingNames"),
        )
        l3 = await run_call(session, ctx, "l3_create_allow_rename")
        libs3 = await tx_libraries(ctx)
        renamed = result(l3).get("name")
        rec(
            ctx,
            "l3_allow_rename_keeps_the_name_flame_gave",
            l3.get("ok") is True
            and isinstance(renamed, str)
            and renamed != ids["lib1Name"]
            and base_name(renamed) == base_name(ids["lib1Name"])
            and result(l3).get("renamed") is True
            and result(l3).get("requestedName") == ids["lib1Name"]
            and row_of(libs3, renamed) is not None
            and names_in(libs3).count(ids["lib1Name"]) == 1,
            error=l3.get("error"),
            gotName=renamed,
            libraries=names_in(libs3),
        )
        l4 = await revert(session, ctx, l3)
        libs4 = await tx_libraries(ctx)
        rec(
            ctx,
            "l4_revert_deletes_the_renamed_library_only",
            l4.get("ok") is True
            and result(l4).get("deleted") is True
            and row_of(libs4, renamed) is None
            and row_of(libs4, ids["lib1Name"]) is not None,
            error=l4.get("error"),
        )
        l5 = await revert(session, ctx, l1)
        again = await revert(session, ctx, l1)
        libs5 = await tx_libraries(ctx)
        rec(
            ctx,
            "l5_revert_deletes_the_empty_library_and_cannot_be_repeated",
            l5.get("ok") is True
            and row_of(libs5, ids["lib1Name"]) is None
            and expect_error(again, "PRECONDITION_UNMET"),
            error=l5.get("error"),
            second=err_text(again),
        )
        l6 = await run_call(session, ctx, "l6_create_library_that_gets_a_clip")
        row6 = row_of(await tx_libraries(ctx), ids["lib2Name"])
        if l6.get("ok") is not True or row6 is None:
            rec(ctx, "l6_the_library_is_made", False, error=l6.get("error"))
        else:
            put = await tx_val(
                ctx,
                fill(PUT_CLIP_CODE, SEQ=ids["clipWt"]["c1"], DEST=row6[2]),
                "fix2_put_clip",
            )
            l6r = await revert(session, ctx, l6)
            row6b = row_of(await tx_libraries(ctx), ids["lib2Name"])
            rec(
                ctx,
                "l6_revert_is_a_conflict_and_deletes_nothing_when_the_library_got_a_clip",
                not is_error(put)
                and expect_error(l6r, "CONFLICT")
                and row6b is not None
                and row6b[3] == 1,
                put=put,
                error=err_text(l6r),
            )
        l7 = await run_call(session, ctx, "l7_create_backup_library")
        ids["libCreateGroup"] = (l7.get("journal") or {}).get("group")
        l7b = await run_call(session, ctx, "l7_create_second_backup_library")
        libs7 = await tx_libraries(ctx)
        r3, r4 = row_of(libs7, ids["lib3Name"]), row_of(libs7, ids["lib4Name"])
        rec(
            ctx,
            "l7_the_two_backup_libraries_are_made",
            l7.get("ok") is True and l7b.get("ok") is True and r3 is not None and r4 is not None,
        )
        if r3 is None or r4 is None:
            return
        ids.update(lib3Wt=r3[2], lib4Wt=r4[2])
        reel = await tx_val(
            ctx, fill(MAKE_REEL_CODE, RG=r3[2], NAME=name_of(ctx, "LR")), "fix2_lib_reel"
        )
        folder = await tx_val(
            ctx, fill(FOLDER_CODE, LIB=r3[2], NAME=name_of(ctx, "FO")), "fix2_lib_folder"
        )
        rec(
            ctx,
            "l7_a_reel_and_a_folder_are_made_in_the_backup_library",
            not is_error(reel) and not is_error(folder),
            reel=reel,
            folder=folder,
        )
        ids.update(libReelWt=reel, folderWt=folder)


# -- b: a Batch group into a library -------------------------------------------------------------


async def stage_b(ctx: Ctx) -> None:
    ids = ctx.ids
    original = ids["original"]
    async with server_session(ctx, "fix2-b") as session:
        current = await tx_current(ctx)
        b1 = await run_call(session, ctx, "b1_copy_group_into_library")
        held = await tx_lib_batches(ctx, ids["lib3Wt"]) or []
        copy1 = first_copy(b1)
        bgn_name = original["bgn"]["name"]
        rec(
            ctx,
            "b1_the_group_is_in_the_library_with_its_reels_and_clips_but_no_nodes",
            b1.get("ok") is True
            and copy1.get("type") == "PyBatch"
            and [h["name"] for h in held] == [bgn_name]
            and held[0]["nodeCount"] == 0
            and held[0]["reels"] == original["bgn"]["reels"]
            and held[0]["shelfReels"] == original["bgn"]["shelfReels"]
            and held[0]["uid"] == copy1.get("uid")
            and held[0]["uid"] != ids["bgnUid"],
            error=b1.get("error"),
            held=held,
        )
        rec(
            ctx,
            "b1_the_source_group_and_the_current_group_are_not_touched",
            all((await unchanged_sources(ctx)).values()) and await tx_current(ctx) == current,
            current=current,
        )
        rec(
            ctx,
            "b1_the_selector_of_the_copy_is_a_name_path_that_finds_it",
            copy1.get("selector") == {"kind": "path", "path": [ids["lib3Name"], bgn_name]},
            selector=copy1.get("selector"),
        )
        b2 = await revert(session, ctx, b1)
        rec(
            ctx,
            "b2_revert_deletes_the_copy_and_leaves_the_source",
            b2.get("ok") is True
            and (await tx_lib_batches(ctx, ids["lib3Wt"])) == []
            and all((await unchanged_sources(ctx)).values()),
            error=b2.get("error"),
        )
        b3a = await run_call(session, ctx, "b1_copy_group_into_library")
        b3 = await run_call(session, ctx, "b3_copy_current_group_into_library")
        held3 = await tx_lib_batches(ctx, ids["lib3Wt"]) or []
        rec(
            ctx,
            "b3_a_current_group_is_copied_too_and_stays_current",
            b3a.get("ok") is True
            and b3.get("ok") is True
            and sorted(h["name"] for h in held3) == sorted([bgn_name, original["bgc"]["name"]])
            and held3[0]["uid"] != held3[1]["uid"]
            and await tx_current(ctx) == current
            and all((await unchanged_sources(ctx)).values()),
            error=b3.get("error") or b3a.get("error"),
            held=held3,
        )
        ids["restoreSel"] = first_copy(b3a).get("selector")
        ids["restoreCurrentSel"] = first_copy(b3).get("selector")
        ids["backupCopyGroup"] = (b3a.get("journal") or {}).get("group")
        ids["backupSource"] = {
            "nodeCount": result(b3a).get("sourceNodeCount"),
            "fingerprint": result(b3a).get("sourceFingerprint"),
        }
        rec(
            ctx,
            "b3_the_result_carries_the_node_count_and_fingerprint_of_the_source",
            ids["backupSource"]["nodeCount"] == len(original["bgn"]["nodes"])
            and isinstance(ids["backupSource"]["fingerprint"], str)
            and result(b3a).get("copyFingerprint") is None,
            backupSource=ids["backupSource"],
        )
        await folder_copy(session, ctx)
        await refusals(session, ctx)
        await conflict_when_renamed(session, ctx)
        await closed_library(session, ctx)


async def folder_copy(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    b4 = await run_call(session, ctx, "b4_copy_group_into_a_folder")
    in_folder = await tx_lib_batches(ctx, ids["folderWt"]) or []
    copy4 = first_copy(b4)
    rec(
        ctx,
        "b4_a_group_is_copied_into_a_library_folder",
        b4.get("ok") is True
        and len(in_folder) == 1
        and (copy4.get("selector") or {}).get("path", [None])[:2]
        == [ids["lib3Name"], name_of(ctx, "FO")],
        error=b4.get("error"),
        selector=copy4.get("selector"),
    )
    b4r = await revert(session, ctx, b4)
    rec(
        ctx,
        "b4_revert_empties_the_folder_again",
        b4r.get("ok") is True and (await tx_lib_batches(ctx, ids["folderWt"])) == [],
        error=b4r.get("error"),
    )


async def refusals(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    groups = groups_now(ctx)
    held = await tx_lib_batches(ctx, ids["lib3Wt"]) or []
    for call_id, needle in (
        ("b5_copy_group_into_a_library_reel", "reel"),
        ("b5_copy_group_into_a_desktop_reel", "reel"),
        ("b5_copy_group_with_a_clip", "one Batch group"),
        ("b5_copy_desktop_group_onto_the_desktop", "library"),
    ):
        env = await run_call(session, ctx, call_id)
        rec(
            ctx,
            f"{call_id}_is_refused_before_anything_is_written",
            expect_error(env, "INVALID_ARGS") and needle in err_text(env),
            error=err_text(env),
        )
    lib_reel = await tx_clip_names(ctx, ids["libReelWt"])
    desk_reel = await tx_clip_names(ctx, ids["deskReelWt"])
    rec(
        ctx,
        "b5_no_reel_got_a_clip_and_no_journal_group_was_written",
        lib_reel == []
        and desk_reel == []
        and groups_now(ctx) == groups
        and len(await tx_lib_batches(ctx, ids["lib3Wt"]) or []) == len(held),
        libReel=lib_reel,
        deskReel=desk_reel,
    )


async def conflict_when_renamed(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    b6 = await run_call(session, ctx, "b6_copy_group_into_second_library")
    held = await tx_lib_batches(ctx, ids["lib4Wt"]) or []
    if b6.get("ok") is not True or len(held) != 1:
        rec(ctx, "b6_the_group_is_copied_into_the_second_library", False, error=b6.get("error"))
        return
    uid, old = held[0]["uid"], held[0]["name"]
    new = f"{old}_X"
    renamed = await tx_val(
        ctx,
        fill(RENAME_LIB_BATCH_CODE, WT=ids["lib4Wt"], UID=uid, NAME=new),
        "fix2_rename_lib_batch",
    )
    took = isinstance(renamed, dict) and renamed.get("name") == new
    if not took:
        observe(
            ctx, "b6_a_group_in_a_library_cannot_be_renamed_so_no_conflict_is_made", got=renamed
        )
        return
    conflict = await revert(session, ctx, b6)
    still = await tx_lib_batches(ctx, ids["lib4Wt"]) or []
    rec(
        ctx,
        "b6_revert_is_a_conflict_and_deletes_nothing_when_the_copy_was_renamed",
        expect_error(conflict, "CONFLICT") and len(still) == 1,
        error=err_text(conflict),
    )
    await tx_val(
        ctx,
        fill(RENAME_LIB_BATCH_CODE, WT=ids["lib4Wt"], UID=uid, NAME=old),
        "fix2_rename_lib_batch_back",
    )
    back = await revert(session, ctx, b6)
    rec(
        ctx,
        "b6_revert_works_again_once_the_name_is_back",
        back.get("ok") is True and (await tx_lib_batches(ctx, ids["lib4Wt"])) == [],
        error=back.get("error"),
    )


async def lib_opened(ctx: Ctx, uid: str) -> Any:
    """`opened` read through the list of libraries (which does not open a closed one)."""
    got = await tx_val(ctx, fill(LIB_OPEN_STATE_CODE, UID=uid), "fix2_lib_state")
    return got.get("opened") if isinstance(got, dict) else None


async def peek_batches(ctx: Ctx, uid: str) -> list[dict[str, Any]] | None:
    """The Batch groups a library holds, read the way the transport can (a wiretap lookup, which
    opens a closed library), then the library is closed again."""
    wiretap = ctx.ids["lib5Wt"]
    held = await tx_lib_batches(ctx, wiretap)
    await tx_val(ctx, fill(LIB_SET_OPEN_CODE, UID=uid, OPEN=False), "fix2_lib_close_again")
    return held


async def closed_library(session: Any, ctx: Ctx) -> None:
    """A closed library is opened for the call and closed again. Whether a
    library can be closed through the API, and what a closed library lists, is observed; what the
    ops do with a closed library is judged only when it did close."""
    ids = ctx.ids
    made = await run_call(session, ctx, "o1_create_library_to_close")
    row = next((r for r in await tx_libraries(ctx) if r[0] == ids["lib5Name"]), None)
    if made.get("ok") is not True or row is None:
        rec(ctx, "o1_the_library_to_close_is_made", False, error=made.get("error"))
        return
    ids["lib5Wt"], uid = row[2], row[1]
    copied = await run_call(session, ctx, "o2_copy_group_into_the_library_to_close")
    rec(ctx, "o2_a_group_is_copied_into_it", copied.get("ok") is True, error=copied.get("error"))
    state = await tx_val(ctx, fill(LIB_OPEN_STATE_CODE, UID=uid), "fix2_lib_state")
    closed = await tx_val(ctx, fill(LIB_SET_OPEN_CODE, UID=uid, OPEN=False), "fix2_lib_close")
    took = isinstance(closed, dict) and closed.get("opened") is False
    observe(ctx, "o3_what_a_closed_library_reads", before=state, afterClose=closed)
    if not took:
        observe(ctx, "o3_the_library_could_not_be_closed_so_no_closed_case_is_judged", got=closed)
        await tx_val(ctx, fill(LIB_SET_OPEN_CODE, UID=uid, OPEN=True), "fix2_lib_open")
        return
    reopened = await tx_val(ctx, fill(LIB_SET_OPEN_CODE, UID=uid, OPEN=True), "fix2_lib_open")
    observe(
        ctx,
        "o3_what_open_answers_and_whether_opened_reads_true_right_after",
        got=reopened,
        openedRightAfter=isinstance(reopened, dict) and reopened.get("opened") is True,
    )
    again = await tx_val(ctx, fill(LIB_SET_OPEN_CODE, UID=uid, OPEN=False), "fix2_lib_close")
    if not (isinstance(again, dict) and again.get("opened") is False):
        observe(ctx, "o3_the_library_could_not_be_closed_again", got=again)
        return
    env4 = await run_call(session, ctx, "o4_copy_group_into_the_closed_library")
    opened_after = await lib_opened(ctx, uid)
    rec(
        ctx,
        "o4_a_copy_into_a_closed_library_works_and_the_library_is_closed_again",
        env4.get("ok") is True
        and opened_after is False
        and any(u.get("closedAgain") is True for u in result(env4).get("librariesOpened") or []),
        error=env4.get("error"),
        opened=opened_after,
        librariesOpened=result(env4).get("librariesOpened"),
    )
    held = await peek_batches(ctx, uid) or []
    rec(ctx, "o4_both_copies_are_in_the_library", len(held) == 2, held=held)
    rev_lib = await revert(session, ctx, made)
    held_after = await peek_batches(ctx, uid) or []
    rec(
        ctx,
        "o5_library_create_revert_is_a_conflict_while_closed_and_deletes_nothing",
        expect_error(rev_lib, "CONFLICT")
        and await lib_opened(ctx, uid) is False
        and len(held_after) == 2,
        error=err_text(rev_lib),
        librariesOpened=result(rev_lib).get("librariesOpened"),
    )
    rev_copy = await revert(session, ctx, env4)
    rec(
        ctx,
        "o6_a_copy_in_the_closed_library_is_reverted_and_the_library_stays_closed",
        rev_copy.get("ok") is True and await lib_opened(ctx, uid) is False,
        error=rev_copy.get("error"),
    )
    rev_first = await revert(session, ctx, copied)
    rev_made = await revert(session, ctx, made)
    rec(
        ctx,
        "o6_the_other_copy_and_the_emptied_library_are_reverted_while_closed",
        rev_first.get("ok") is True
        and rev_made.get("ok") is True
        and row_of(await tx_libraries(ctx), ids["lib5Name"]) is None,
        copy=rev_first.get("error"),
        library=rev_made.get("error"),
    )


async def stage_s(ctx: Ctx) -> None:
    """The checkpoint library is named by a string argument, not by a selector: a purge of a CLOSED
    one must still see what it holds (no copy is called gone) and leave it closed."""
    ids = ctx.ids
    async with server_session(ctx, "fix2-s") as session:
        made = await run_call(session, ctx, "s1_create_checkpoint_library")
        row = next((r for r in await tx_libraries(ctx) if r[0] == ids["ckptName"]), None)
        if made.get("ok") is not True or row is None:
            rec(ctx, "s1_the_checkpoint_library_is_made", False, error=made.get("error"))
            return
        uid, wiretap = row[1], row[2]
        reel = await tx_val(
            ctx, fill(MAKE_REEL_CODE, RG=wiretap, NAME=name_of(ctx, "STASHREEL")), "fix2_stash_reel"
        )
        put = await tx_val(
            ctx,
            fill(PUT_CLIP_CODE, SEQ=ids["clipWt"]["c1"], DEST=reel),
            "fix2_stash_clip",
        )
        rec(ctx, "s1_the_library_holds_one_copy", not is_error(reel) and not is_error(put), put=put)
        closed = await tx_val(ctx, fill(LIB_SET_OPEN_CODE, UID=uid, OPEN=False), "fix2_lib_close")
        if not (isinstance(closed, dict) and closed.get("opened") is False):
            observe(ctx, "s2_the_checkpoint_library_could_not_be_closed", got=closed)
            return
        env = await run_call(session, ctx, "s2_purge_dry_run_on_the_closed_checkpoint_library")
        after = await lib_opened(ctx, uid)
        rec(
            ctx,
            "s2_a_dry_purge_sees_the_copy_in_the_closed_library_and_leaves_it_closed",
            env.get("ok") is True
            and result(env).get("libraryFound") is True
            and len(result(env).get("unknown") or []) == 1
            and result(env).get("missing") == []
            and after is False,
            error=env.get("error"),
            unknown=len(result(env).get("unknown") or []),
            opened=after,
            librariesOpened=result(env).get("librariesOpened"),
        )


# -- r: a library Batch group back onto the Desktop ----------------------------------------------


async def stage_r(ctx: Ctx) -> None:
    ids = ctx.ids
    if not ids.get("restoreSel") or not ids.get("restoreCurrentSel"):
        rec(ctx, "r_stage_skipped_because_the_copies_in_the_library_have_no_selector", False)
        return
    original = ids["original"]
    async with server_session(ctx, "fix2-r") as session:
        current = await tx_current(ctx)
        r1 = await run_call(session, ctx, "r1_restore_group_onto_the_desktop")
        new_uid = first_copy(r1).get("uid")
        back = await tx_group(ctx, new_uid) if new_uid else None
        shape = same_shape(original["bgn"], back) if isinstance(back, dict) else {}
        rec(
            ctx,
            "r1_the_restored_group_has_the_nodes_connections_and_clips_of_the_original",
            r1.get("ok") is True
            and isinstance(back, dict)
            and bool(shape)
            and all(shape.values())
            and back["uid"] not in (ids["bgnUid"], ids["bgcUid"]),
            error=r1.get("error"),
            shape=shape,
            nodes=back["nodes"] if isinstance(back, dict) else None,
        )
        rec(
            ctx,
            "r1_current_does_not_move_and_the_original_is_untouched",
            await tx_current(ctx) == current
            and isinstance(back, dict)
            and back["isCurrent"] is False
            and all((await unchanged_sources(ctx)).values()),
            current=current,
        )
        rec(
            ctx,
            "r1_node_count_and_fingerprint_of_the_restored_group_equal_the_backups_source",
            result(r1).get("copyNodeCount") == ids["backupSource"]["nodeCount"]
            and result(r1).get("copyFingerprint") == ids["backupSource"]["fingerprint"]
            and result(r1).get("sourceFingerprint") is None,
            copy={k: result(r1).get(k) for k in ("copyNodeCount", "copyFingerprint")},
            backup=ids["backupSource"],
        )
        observe(
            ctx,
            "r1_the_restored_group_keeps_the_name_of_the_original",
            restoredName=back["name"] if isinstance(back, dict) else None,
            originalName=original["bgn"]["name"],
        )
        r2 = await revert(session, ctx, r1)
        rec(
            ctx,
            "r2_revert_deletes_the_restored_group_and_nothing_else",
            r2.get("ok") is True
            and new_uid is not None
            and await tx_group(ctx, new_uid) is None
            and await tx_current(ctx) == current
            and all((await unchanged_sources(ctx)).values()),
            error=r2.get("error"),
        )
        saved = result(r2).get("savedSetups") or []
        rec(
            ctx,
            "r2_the_setup_was_saved_as_a_checkpoint_before_the_group_was_deleted",
            len(saved) == 1
            and Path(str(saved[0].get("path"))).is_file()
            and "batch.load_setup" in str(saved[0].get("restoreWith")),
            savedSetups=saved,
        )
        await edited_restore(session, ctx)
        await edited_value_restore(session, ctx)
        await current_restore(session, ctx)
        current = await tx_current(ctx)  # r4 may have moved it (deleting the current group)
        r5 = await run_call(session, ctx, "r5_restore_the_group_that_was_current")
        uid5 = first_copy(r5).get("uid")
        back5 = await tx_group(ctx, uid5) if uid5 else None
        shape5 = same_shape(original["bgc"], back5) if isinstance(back5, dict) else {}
        rec(
            ctx,
            "r5_the_group_that_was_current_comes_back_whole_and_current_stays",
            r5.get("ok") is True
            and bool(shape5)
            and all(shape5.values())
            and await tx_current(ctx) == current,
            error=r5.get("error"),
            shape=shape5,
        )
        r5r = await revert(session, ctx, r5)
        rec(
            ctx,
            "r5_revert_deletes_it_again",
            r5r.get("ok") is True and uid5 is not None and await tx_group(ctx, uid5) is None,
            error=r5r.get("error"),
        )
        r6 = await run_call(session, ctx, "r6_restore_a_library_group_into_a_library")
        rec(
            ctx,
            "r6_a_library_group_cannot_be_copied_into_a_library",
            expect_error(r6, "INVALID_ARGS") and "Desktop" in err_text(r6),
            error=err_text(r6),
        )


async def edited_restore(session: Any, ctx: Ctx) -> None:
    r3 = await run_call(session, ctx, "r1_restore_group_onto_the_desktop")
    uid = first_copy(r3).get("uid")
    if r3.get("ok") is not True or not uid:
        rec(ctx, "r3_the_group_is_restored_again", False, error=r3.get("error"))
        return
    added = await tx_val(ctx, fill(ADD_NODE_CODE, UID=uid), "fix2_add_node")
    out = await revert(session, ctx, r3)
    left = await tx_group(ctx, uid)
    rec(
        ctx,
        "r3_revert_is_a_conflict_and_deletes_nothing_when_a_node_was_added",
        not is_error(added) and expect_error(out, "CONFLICT") and left is not None,
        added=added,
        error=err_text(out),
    )
    await tx_val(ctx, fill(DELETE_GROUP_CODE, UID=uid), "fix2_delete_edited_restore")


async def edited_value_restore(session: Any, ctx: Ctx) -> None:
    """Only a node's attribute value is changed by hand (no node added, no wire moved): the revert
    must still see it. Whether the value can be changed through the API is observed."""
    r = await run_call(session, ctx, "r1_restore_group_onto_the_desktop")
    uid = first_copy(r).get("uid")
    if r.get("ok") is not True or not uid:
        rec(ctx, "r3b_the_group_is_restored_again", False, error=r.get("error"))
        return
    changed = await tx_val(ctx, fill(TOGGLE_BYPASS_CODE, UID=uid), "fix2_toggle_bypass")
    took = isinstance(changed, dict) and changed.get("after") != changed.get("before")
    out = await revert(session, ctx, r)
    left = await tx_group(ctx, uid)
    if not took:
        observe(ctx, "r3b_a_node_value_could_not_be_changed_so_no_conflict_is_made", got=changed)
        if out.get("ok") is not True:
            await tx_val(ctx, fill(DELETE_GROUP_CODE, UID=uid), "fix2_delete_restore")
        return
    rec(
        ctx,
        "r3b_revert_is_a_conflict_and_deletes_nothing_when_only_a_node_value_changed",
        expect_error(out, "CONFLICT") and left is not None,
        changed=changed,
        error=err_text(out),
    )
    await tx_val(ctx, fill(DELETE_GROUP_CODE, UID=uid), "fix2_delete_edited_restore")


async def current_restore(session: Any, ctx: Ctx) -> None:
    r4 = await run_call(session, ctx, "r1_restore_group_onto_the_desktop")
    uid = first_copy(r4).get("uid")
    if r4.get("ok") is not True or not uid:
        rec(ctx, "r4_the_group_is_restored_for_the_current_case", False, error=r4.get("error"))
        return
    made = await tx_val(ctx, fill(SET_CURRENT_CODE, UID=uid), "fix2_set_current")
    took = isinstance(made, dict) and made.get("current") == uid
    out = await revert(session, ctx, r4)
    if not took:
        observe(ctx, "r4_the_restored_group_could_not_be_made_current", made=made)
        if out.get("ok") is not True:
            await tx_val(ctx, fill(DELETE_GROUP_CODE, UID=uid), "fix2_delete_restore")
        return
    rec(
        ctx,
        "r4_deleting_a_restored_group_that_is_current_says_current_moved",
        out.get("ok") is True
        and result(out).get("currentChanged") is True
        and any("current" in str(n) for n in result(out).get("notes") or [])
        and (result(out).get("current") or {}).get("uid") != uid,
        error=out.get("error"),
        current=result(out).get("current"),
        notes=result(out).get("notes"),
    )


# -- d: the desktop selector ---------------------------------------------------------------------


async def stage_d(ctx: Ctx) -> None:
    names = [name_of(ctx, tail) for tail in ("BGK", "BGN", "BGC")]
    rg = name_of(ctx, "RG")
    async with server_session(ctx, "fix2-d") as session:
        env = await run_call(session, ctx, "d1_list_the_desktop")
        pairs = [(i.get("type"), i.get("name")) for i in items_of(env)]
        rec(
            ctx,
            "d1_the_desktop_lists_its_reel_groups_and_batch_groups_together",
            env.get("ok") is True
            and ("PyReelGroup", rg) in pairs
            and all(("PyBatch", n) in pairs for n in names),
            error=env.get("error"),
            total=result(env).get("total"),
        )
        only = await run_call(session, ctx, "d1_list_the_desktop_batch_groups_only")
        kinds = {i.get("type") for i in items_of(only)}
        rec(
            ctx,
            "d1_the_types_filter_keeps_the_batch_groups_only",
            only.get("ok") is True
            and kinds == {"PyBatch"}
            and all(n in [i.get("name") for i in items_of(only)] for n in names),
            error=only.get("error"),
        )


# -- c: journal.check ----------------------------------------------------------------------------


async def stage_c(ctx: Ctx) -> None:
    ids = ctx.ids
    libs = await tx_libraries(ctx)
    lib3 = row_of(libs, ids["lib3Name"])
    held = await tx_lib_batches(ctx, ids["lib3Wt"]) or []
    if lib3 is None or "libCreateGroup" not in ids or "backupCopyGroup" not in ids:
        rec(ctx, "c_the_material_for_the_check_steps_exists", False, held=len(held))
        return
    async with server_session(ctx, "fix2-c") as session:
        others = [row[1] for row in libs if row[0] != ids["lib3Name"]]
        group = synthetic_open_intent(ctx, ids["libCreateGroup"], existing=others)
        checked = await check_group(session, ctx, group)
        rec(
            ctx,
            "c1_a_library_create_without_an_outcome_is_found_landed",
            checked.get("verdict") == "landed"
            and ((checked.get("created") or {}).get("uid") == lib3[1]),
            checked=checked,
        )
        bgn_name = ids["original"]["bgn"]["name"]
        wanted = [h["uid"] for h in held if h["name"] != bgn_name]
        group2 = synthetic_open_intent(ctx, ids["backupCopyGroup"], existing=wanted)
        checked2 = await check_group(session, ctx, group2)
        made = ((checked2.get("created") or {}).get("copies")) or [{}]
        rec(
            ctx,
            "c2_a_batch_copy_without_an_outcome_is_found_landed_as_a_batch_group",
            checked2.get("verdict") == "landed" and made[0].get("type") == "PyBatch",
            checked=checked2,
        )
