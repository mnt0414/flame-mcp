"""Stages a (import) and b (duplicate) of the import / duplicate / move real-Flame e2e; see
p3_1b_write_e2e.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import (  # noqa: E402
    _result_of,
)
from p2_2a_journal_e2e import (  # noqa: E402
    Ctx,
    do,
    rec,
    server_session,
)
from p2_4_common import (  # noqa: E402
    observe,
)
from p3_1a_read_e2e import (  # noqa: E402
    COPY_CODE,
    fill,
    is_error,
    tx_struct,
    tx_val,
)
from p3_1b_e2e_common import (  # noqa: E402
    DELETE_UID_CODE,
    FOLDER_CODE,
    REEL_NAMES_CODE,
    RENAME_CODE,
    added,
    batch_refusal,
    check_group,
    err_text,
    expect_error,
    groups_now,
    name_of,
    names_of,
    outcome_of,
    revert,
    run_call,
    segments_of,
    synthetic_open_intent,
    tx_find_uid,
    tx_find_wt,
    tx_list,
    uids_of,
    written_groups,
)

# -- a: import


async def stage_a(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31b-a") as session:
        a_before = await tx_list(ctx, ids["aWt"])
        a1 = await run_call(session, ctx, "a1_import_one")
        a_after = await tx_list(ctx, ids["aWt"])
        res = _result_of(a1)
        made = res.get("imported") or []
        info = (
            await do(session, ctx, "desktop.info", {"target": made[0]["selector"]}) if made else {}
        )
        rec(
            ctx,
            "a1_one_path_makes_one_clip_and_the_op_agrees_with_what_transport_lists",
            a1.get("ok") is True
            and len(made) == 1
            and uids_of(added(a_before, a_after)) == [made[0]["uid"]]
            and names_of(added(a_before, a_after)) == ["clip1"]
            and (res.get("requested"), res.get("returned")) == (1, 1)
            and made[0]["selector"].get("kind") == "wiretap"
            and info.get("ok") is True
            and _result_of(info).get("uid") == made[0]["uid"]
            and (a1.get("journal") or {}).get("revert") is not None,
            error=a1.get("error"),
            imported=made,
            transportAdded=added(a_before, a_after),
        )

        b_before = await tx_list(ctx, ids["bWt"])
        a2 = await run_call(session, ctx, "a2_import_three")
        b_after = await tx_list(ctx, ids["bWt"])
        res = _result_of(a2)
        made = res.get("imported") or []
        rec(
            ctx,
            "a2_a_list_of_three_paths_makes_three_clips_in_the_order_given",
            a2.get("ok") is True
            and names_of(added(b_before, b_after)) == ["clip1", "clip2", "clip3"]
            and [m["uid"] for m in made] == uids_of(added(b_before, b_after))
            and (res.get("requested"), res.get("returned")) == (3, 3),
            error=a2.get("error"),
            transportAdded=added(b_before, b_after),
            imported=[m.get("name") for m in made],
        )

        c_before = await tx_list(ctx, ids["cWt"])
        a3 = await run_call(session, ctx, "a3_import_directory")
        c_after = await tx_list(ctx, ids["cWt"])
        res = _result_of(a3)
        made = res.get("imported") or []
        rec(
            ctx,
            "a3_a_directory_brings_its_two_media_files_in_and_the_notes_say_it_is_not_one_clip",
            a3.get("ok") is True
            and len(added(c_before, c_after)) == 2
            and sorted(m["uid"] for m in made) == sorted(uids_of(added(c_before, c_after)))
            and res.get("requested") == 1
            and bool(res.get("notes")),
            error=a3.get("error"),
            transportAdded=added(c_before, c_after),
            returned=res.get("returned"),
            notes=res.get("notes"),
        )

        lr_before = await tx_list(ctx, ids["lrWt"])
        a4 = await run_call(session, ctx, "a4_import_into_a_library_reel")
        lr_after = await tx_list(ctx, ids["lrWt"])
        made = _result_of(a4).get("imported") or []
        rec(
            ctx,
            "a4_import_into_a_library_reel",
            a4.get("ok") is True
            and [m["uid"] for m in made] == uids_of(added(lr_before, lr_after))
            and len(made) == 1,
            error=a4.get("error"),
            transportAdded=added(lr_before, lr_after),
        )

        await observe_library_and_folder_imports(session, ctx)

        before_groups = groups_now(ctx)
        a_before = await tx_list(ctx, ids["aWt"])
        a7 = await run_call(session, ctx, "a7_import_missing_path")
        a8 = await run_call(session, ctx, "a8_import_zero_byte")
        a9 = await run_call(session, ctx, "a9_import_relative_path")
        a_after = await tx_list(ctx, ids["aWt"])
        rec(
            ctx,
            "a7_a_missing_path_is_refused_before_flame_and_nothing_is_journaled",
            expect_error(a7, "PRECONDITION_UNMET")
            and ids["missing"] in err_text(a7)
            and _result_of(a7).get("missing") == [ids["missing"]]
            and groups_now(ctx) == before_groups
            and uids_of(a_before) == uids_of(a_after),
            error=a7.get("error"),
        )
        rec(
            ctx,
            "a8_a_zero_byte_file_is_refused_before_flame",
            expect_error(a8, "PRECONDITION_UNMET")
            and _result_of(a8).get("empty") == [ids["empty"]]
            and groups_now(ctx) == before_groups,
            error=a8.get("error"),
        )
        rec(
            ctx,
            "a9_a_relative_path_is_invalid_args",
            expect_error(a9, "INVALID_ARGS") and "absolute" in err_text(a9),
            error=a9.get("error"),
        )

        a10 = await run_call(session, ctx, "a10_import_pattern_that_matches_nothing")
        a_after = await tx_list(ctx, ids["aWt"])
        group = (a10.get("journal") or {}).get("group")
        outcome = outcome_of(ctx, group)
        rec(
            ctx,
            "a10_a_pattern_that_matches_nothing_is_a_silent_noop_and_the_outcome_says_so",
            expect_error(a10, "SILENT_NOOP")
            and (a10.get("journal") or {}).get("revert") is None
            and outcome.get("ok") is False
            and outcome.get("errorCode") == "SILENT_NOOP"
            and outcome.get("created") is None
            and uids_of(a_before) == uids_of(a_after),
            error=a10.get("error"),
            outcome={k: outcome.get(k) for k in ("ok", "errorCode", "created", "readBack")},
        )

        await batch_refusal(session, ctx, "a11", "a11_import_into_a_batch_reel", "batch.add_clips")


async def observe_library_and_folder_imports(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    lib_before = await tx_list(ctx, ids["libWt"])
    a5 = await run_call(session, ctx, "a5_import_into_a_library")
    lib_after = await tx_list(ctx, ids["libWt"])
    made = _result_of(a5).get("imported") or []
    found = [await tx_find_uid(ctx, m["uid"]) for m in made]
    observe(
        ctx,
        "a5_import_into_a_library_itself",
        ok=a5.get("ok"),
        errorCode=a5.get("errorCode"),
        error=a5.get("error"),
        degraded=a5.get("degraded"),
        imported=[(m.get("type"), m.get("name")) for m in made],
        transportAdded=added(lib_before, lib_after),
        foundByUid=found,
    )
    if a5.get("ok") is True and (a5.get("journal") or {}).get("revert"):
        out = await revert(session, ctx, a5)
        observe(ctx, "a5_revert_of_that_import", ok=out.get("ok"), error=out.get("error"))
    folder = await tx_val(
        ctx, fill(FOLDER_CODE, LIB=ids["libWt"], NAME=name_of(ctx, "FOLDER")), "p31b_folder"
    )
    if is_error(folder) or not isinstance(folder, str) or folder.startswith("ERR:"):
        observe(ctx, "a6_a_library_folder_could_not_be_made_by_transport", answer=folder)
        return
    ids["folderWt"] = folder
    before = await tx_list(ctx, folder)
    a6 = await run_call(session, ctx, "a6_import_into_a_folder")
    after = await tx_list(ctx, folder)
    observe(
        ctx,
        "a6_import_into_a_library_folder",
        ok=a6.get("ok"),
        errorCode=a6.get("errorCode"),
        error=a6.get("error"),
        degraded=a6.get("degraded"),
        transportAdded=added(before, after),
    )
    if a6.get("ok") is True and (a6.get("journal") or {}).get("revert"):
        out = await revert(session, ctx, a6)
        observe(ctx, "a6_revert_of_that_import", ok=out.get("ok"), error=out.get("error"))


async def stage_a2(ctx: Ctx) -> None:
    """Revert and check of the imports of stage a (each in its own server so a1..a4 stand)."""
    ids = ctx.ids
    async with server_session(ctx, "p31b-a2") as session:
        a_before = await tx_list(ctx, ids["aWt"])
        one = await run_call(session, ctx, "a1_import_one")
        a_mid = await tx_list(ctx, ids["aWt"])
        out = await revert(session, ctx, one)
        a_after = await tx_list(ctx, ids["aWt"])
        made = _result_of(one).get("imported") or []
        gone = [await tx_find_uid(ctx, m["uid"]) for m in made]
        rec(
            ctx,
            "a12_revert_of_an_import_deletes_the_clip_it_made_and_leaves_the_rest",
            one.get("ok") is True
            and out.get("ok") is True
            and len(_result_of(out).get("deleted") or []) == 1
            and gone == [None]
            and uids_of(a_before) == uids_of(a_after)
            and len(a_mid or []) == len(a_before or []) + 1,
            error=out.get("error"),
            deleted=_result_of(out).get("deleted"),
        )
        again = await revert(session, ctx, one)
        rec(
            ctx,
            "a13_a_second_revert_of_the_same_group_is_refused",
            expect_error(again, "PRECONDITION_UNMET"),
            error=again.get("error"),
        )

        c_before = await tx_list(ctx, ids["cWt"])
        real = await run_call(session, ctx, "a3_import_directory")
        c_mid = await tx_list(ctx, ids["cWt"])
        group = (real.get("journal") or {}).get("group")
        synthetic = synthetic_open_intent(ctx, group)
        checked = await check_group(session, ctx, synthetic)
        found = [c.get("uid") for c in (checked.get("created") or {}).get("imports", [])]
        rec(
            ctx,
            "a14_check_of_an_open_import_intent_is_landed_with_the_clips_that_came_in",
            checked.get("verdict") == "landed"
            and sorted(found) == sorted(uids_of(added(c_before, c_mid))),
            verdict=checked.get("verdict"),
            found=found,
        )
        undone = await do(session, ctx, "journal.revert", {"group": synthetic})
        c_after = await tx_list(ctx, ids["cWt"])
        written_groups(ctx).append({"op": "journal.revert", "group": synthetic})
        rec(
            ctx,
            "a15_revert_after_that_check_deletes_them",
            undone.get("ok") is True and uids_of(c_before) == uids_of(c_after),
            error=undone.get("error"),
        )
        nothing = synthetic_open_intent(ctx, group, existing=uids_of(c_after))
        verdict = await check_group(session, ctx, nothing)
        rec(
            ctx,
            "a16_check_of_an_open_import_intent_that_made_nothing_is_not_landed",
            verdict.get("verdict") == "not_landed",
            verdict=verdict.get("verdict"),
        )


# -- b: duplicate


async def stage_b(ctx: Ctx) -> None:
    ids = ctx.ids
    async with server_session(ctx, "p31b-b") as session:
        src_before = await tx_list(ctx, ids["srcWt"])
        b1 = await run_call(session, ctx, "b1_duplicate_clip")
        src_after = await tx_list(ctx, ids["srcWt"])
        res = _result_of(b1)
        dup = res.get("duplicate") or {}
        new = added(src_before, src_after)
        info = await do(session, ctx, "desktop.info", {"target": dup.get("selector")})
        rec(
            ctx,
            "b1_a_clip_is_duplicated_beside_itself_with_the_same_name",
            b1.get("ok") is True
            and [r[2] for r in new] == [dup.get("uid")]
            and names_of(new) == ["clip1"]
            and dup.get("uid") != ids["clipUid"]["clip1"]
            and ids["clipUid"]["clip1"] in uids_of(src_after)
            and (res.get("source") or {}).get("uid") == ids["clipUid"]["clip1"]
            and (res.get("source") or {}).get("parent", {}).get("name") == "SRC"
            and info.get("ok") is True
            and _result_of(info).get("uid") == dup.get("uid"),
            error=b1.get("error"),
            duplicate=dup,
            transportAdded=new,
        )
        undone = await revert(session, ctx, b1)
        rec(
            ctx,
            "b4_revert_of_a_clip_duplicate_deletes_it",
            undone.get("ok") is True
            and uids_of(await tx_list(ctx, ids["srcWt"])) == uids_of(src_before)
            and _result_of(undone).get("deleted") is True,
            error=undone.get("error"),
        )

        seq_before = await tx_list(ctx, ids["seqWt"])
        b2 = await run_call(session, ctx, "b2_duplicate_sequence")
        seq_after = await tx_list(ctx, ids["seqWt"])
        res = _result_of(b2)
        dup = res.get("duplicate") or {}
        new = added(seq_before, seq_after)
        observe(ctx, "b2_what_flame_answered_for_a_sequence", returned=res.get("returned"))
        want = [await segments_of(session, ctx, ids["s1Wt"], t) for t in (0, 1)]
        got = (
            [await segments_of(session, ctx, dup["wiretapId"], t) for t in (0, 1)]
            if dup.get("wiretapId")
            else None
        )
        struct_src = await tx_struct(ctx, ids["s1Wt"])
        struct_dup = await tx_struct(ctx, dup["wiretapId"]) if dup.get("wiretapId") else None
        rec(
            ctx,
            "b2_a_sequence_is_found_by_the_list_and_its_segments_equal_the_sources",
            b2.get("ok") is True
            and [r[2] for r in new] == [dup.get("uid")]
            and dup.get("type") == "PySequence"
            and dup.get("name") == ids["s1Name"]
            and got == want
            and struct_src == struct_dup
            and any(want),
            error=b2.get("error"),
            duplicate=dup,
            segmentsEqual=got == want,
            structEqual=struct_src == struct_dup,
        )
        undone = await revert(session, ctx, b2)
        rec(
            ctx,
            "b4_revert_of_a_sequence_duplicate_deletes_it",
            undone.get("ok") is True
            and uids_of(await tx_list(ctx, ids["seqWt"])) == uids_of(seq_before),
            error=undone.get("error"),
        )

        reels_before = await tx_val(ctx, fill(REEL_NAMES_CODE, WT=ids["rgWt"]), "p31b_reels")
        drel_before = await tx_list(ctx, ids["drelWt"])
        b3 = await run_call(session, ctx, "b3_duplicate_reel")
        reels_after = await tx_val(ctx, fill(REEL_NAMES_CODE, WT=ids["rgWt"]), "p31b_reels")
        res = _result_of(b3)
        dup = res.get("duplicate") or {}
        copy_rows = await tx_list(ctx, dup["wiretapId"]) if dup.get("wiretapId") else None
        info = await do(session, ctx, "desktop.info", {"target": dup.get("selector")})
        rec(
            ctx,
            "b3_a_reel_is_duplicated_with_its_contents_at_the_end_of_the_reel_group",
            b3.get("ok") is True
            and dup.get("type") == "PyReel"
            and dup.get("name") == "DREL copy"
            and isinstance(reels_after, list)
            and reels_after[:-1] == reels_before
            and reels_after[-1] == dup.get("name")
            and [(r[0], r[1]) for r in copy_rows or []] == [(r[0], r[1]) for r in drel_before or []]
            and not set(uids_of(copy_rows)) & set(uids_of(drel_before))
            and uids_of(await tx_list(ctx, ids["drelWt"])) == uids_of(drel_before)
            and info.get("ok") is True,
            error=b3.get("error"),
            duplicate=dup,
            reelsAfter=reels_after,
            copyContents=[(r[0], r[1]) for r in copy_rows or []],
        )
        undone = await revert(session, ctx, b3)
        rec(
            ctx,
            "b4_revert_of_a_reel_duplicate_deletes_it_and_keeps_the_original",
            undone.get("ok") is True
            and await tx_val(ctx, fill(REEL_NAMES_CODE, WT=ids["rgWt"]), "p31b_reels")
            == reels_before
            and uids_of(await tx_list(ctx, ids["drelWt"])) == uids_of(drel_before),
            error=undone.get("error"),
        )

        await stage_b_conflicts(session, ctx)
        await stage_b_refusals(session, ctx)
        await stage_b_library_and_check(session, ctx)


async def stage_b_conflicts(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    b5 = await run_call(session, ctx, "b1_duplicate_clip")
    dup = _result_of(b5).get("duplicate") or {}
    original_name = dup.get("name")
    await tx_val(ctx, fill(RENAME_CODE, UID=dup.get("uid"), NAME="renamed by hand"), "p31b_rename")
    changed = await revert(session, ctx, b5)
    still = await tx_find_uid(ctx, dup.get("uid", ""))
    await tx_val(ctx, fill(RENAME_CODE, UID=dup.get("uid"), NAME=original_name), "p31b_unrename")
    done = await revert(session, ctx, b5)
    rec(
        ctx,
        "b5_a_renamed_duplicate_is_a_conflict_that_deletes_nothing_then_reverts_once_restored",
        expect_error(changed, "CONFLICT")
        and "name" in " ".join(_result_of(changed).get("conflicts") or [])
        and still == "PyClip"
        and done.get("ok") is True
        and await tx_find_uid(ctx, dup.get("uid", "")) is None,
        conflicts=_result_of(changed).get("conflicts"),
        error=done.get("error"),
    )
    b5r = await run_call(session, ctx, "b3_duplicate_reel")
    dup = _result_of(b5r).get("duplicate") or {}
    added_clip = await tx_val(
        ctx, fill(COPY_CODE, SEQ=ids["clipWt"]["clip2"], DEST=dup.get("wiretapId")), "p31b_fill"
    )
    changed = await revert(session, ctx, b5r)
    exists = await tx_find_wt(ctx, dup.get("wiretapId", ""))
    if isinstance(added_clip, list) and added_clip:
        await tx_val(ctx, fill(DELETE_UID_CODE, UID=added_clip[0]["uid"]), "p31b_unfill")
    done = await revert(session, ctx, b5r)
    rec(
        ctx,
        "b5_a_reel_duplicate_with_an_extra_clip_is_a_conflict_then_reverts_once_it_is_removed",
        expect_error(changed, "CONFLICT")
        and "entries" in " ".join(_result_of(changed).get("conflicts") or [])
        and isinstance(exists, list)
        and done.get("ok") is True,
        conflicts=_result_of(changed).get("conflicts"),
        error=done.get("error"),
    )


async def stage_b_refusals(session: Any, ctx: Ctx) -> None:
    before = groups_now(ctx)
    group = await run_call(session, ctx, "b6_duplicate_a_reel_group")
    library = await run_call(session, ctx, "b6_duplicate_a_library")
    rec(
        ctx,
        "b6_a_reel_group_and_a_library_are_refused_as_not_measured_and_nothing_is_journaled",
        expect_error(group, "PRECONDITION_UNMET")
        and "only a clip, a sequence and a reel" in err_text(group)
        and expect_error(library, "PRECONDITION_UNMET")
        and groups_now(ctx) == before,
        groupError=group.get("error"),
        libraryError=library.get("error"),
    )
    await batch_refusal(session, ctx, "b6", "b6_duplicate_a_batch_reel", "batch.add_clips")


async def stage_b_library_and_check(session: Any, ctx: Ctx) -> None:
    ids = ctx.ids
    if "libClipWt" in ids:
        lr_before = await tx_list(ctx, ids["lrWt"])
        b7 = await run_call(session, ctx, "b7_duplicate_a_library_clip")
        lr_after = await tx_list(ctx, ids["lrWt"])
        observe(
            ctx,
            "b7_duplicate_of_a_library_clip",
            ok=b7.get("ok"),
            errorCode=b7.get("errorCode"),
            error=b7.get("error"),
            transportAdded=added(lr_before, lr_after),
        )
        if b7.get("ok") is True:
            out = await revert(session, ctx, b7)
            observe(ctx, "b7_revert_of_that_duplicate", ok=out.get("ok"), error=out.get("error"))
    src_before = await tx_list(ctx, ids["srcWt"])
    real = await run_call(session, ctx, "b1_duplicate_clip")
    dup = _result_of(real).get("duplicate") or {}
    group = (real.get("journal") or {}).get("group")
    landed = await check_group(session, ctx, synthetic_open_intent(ctx, group))
    created = landed.get("created") or {}
    rec(
        ctx,
        "b8_check_of_an_open_duplicate_intent_is_landed_with_the_duplicate",
        landed.get("verdict") == "landed"
        and created.get("uid") == dup.get("uid")
        and (created.get("state") or {}).get("name") == dup.get("name"),
        verdict=landed.get("verdict"),
        created=created,
    )
    now = uids_of(await tx_list(ctx, ids["srcWt"]))
    nothing = await check_group(session, ctx, synthetic_open_intent(ctx, group, existing=now))
    rec(
        ctx,
        "b9_check_of_an_open_duplicate_intent_that_made_nothing_is_not_landed",
        nothing.get("verdict") == "not_landed",
        verdict=nothing.get("verdict"),
    )
    out = await revert(session, ctx, real)
    rec(
        ctx,
        "b10_the_duplicate_made_for_the_check_is_reverted",
        out.get("ok") is True and uids_of(await tx_list(ctx, ids["srcWt"])) == uids_of(src_before),
        error=out.get("error"),
    )
