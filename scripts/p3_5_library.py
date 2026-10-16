"""Stage n of the scenario e2e (see p3_5_scenarios_e2e.py): what the library protection
refuses, tried on the clip scenario 1 brought into a library reel, and the reverts it
still allows.

  desktop.move of a library clip out of the library -> PRECONDITION_UNMET (take it out with
  desktop.copy), nothing moved, nothing journaled;
  desktop.delete of a library clip -> PRECONDITION_UNMET, nothing stashed or deleted;
  journal.revert of the desktop.copy (the copy on the Desktop goes, the library keeps its clip) and
  of the mediahub.import (the clip this run brought in goes: a revert of what the run made is
  allowed inside a library)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, rec, server_session  # noqa: E402
from p3_5_e2e_common import (  # noqa: E402
    err_text,
    expect_error,
    revert,
    run_call,
    sel,
    state_snapshot,
    tx_children,
    tx_find_uid,
    uids_of,
)


async def stage_n(ctx: Ctx) -> None:
    ids = ctx.ids
    entry = ids.get("p35LibraryClip")
    copy_env = ids.get("p35Copy")
    import_env = ids.get("p35Import")
    if not (entry and copy_env and import_env):
        rec(ctx, "n0_the_clip_scenario_1_brought_into_the_library_is_there", False)
        return
    copy_uid = ((_result_of(copy_env).get("copies") or [{}])[0]).get("uid")
    async with server_session(ctx, "p35-n") as session:
        lib_before = uids_of(await tx_children(ctx, ids["libReelWt"]))
        ccp_before = uids_of(await tx_children(ctx, ids["ccpWt"]))
        snap = await state_snapshot(ctx)
        moved = await run_call(
            session,
            ctx,
            "n_move_out",
            {"sources": [entry["selector"]], "destination": sel(ids["ccpWt"])},
        )
        rec(
            ctx,
            "n1_a_library_clip_is_not_moved_out_it_is_refused_and_nothing_changes",
            expect_error(moved, "PRECONDITION_UNMET")
            and "inside a library" in err_text(moved)
            and "desktop.copy" in err_text(moved)
            and uids_of(await tx_children(ctx, ids["libReelWt"])) == lib_before
            and uids_of(await tx_children(ctx, ids["ccpWt"])) == ccp_before
            and await tx_find_uid(ctx, entry["uid"]) == "PyClip"
            and not (moved.get("journal") or {}).get("group")
            and await state_snapshot(ctx) == snap,
            error=moved.get("error"),
        )
        deleted = await run_call(session, ctx, "n_delete", {"targets": [entry["selector"]]})
        rec(
            ctx,
            "n2_a_library_clip_is_not_deleted_it_is_refused_and_nothing_is_stashed",
            expect_error(deleted, "PRECONDITION_UNMET")
            and await tx_find_uid(ctx, entry["uid"]) == "PyClip"
            and uids_of(await tx_children(ctx, ids["libReelWt"])) == lib_before
            and not (deleted.get("journal") or {}).get("group")
            and await state_snapshot(ctx) == snap,
            error=deleted.get("error"),
        )
        out = await revert(session, ctx, copy_env)
        rec(
            ctx,
            "n3_reverting_the_copy_removes_the_copy_and_the_library_keeps_its_clip",
            out.get("ok") is True
            and await tx_find_uid(ctx, copy_uid) is None
            and uids_of(await tx_children(ctx, ids["libReelWt"])) == lib_before
            and await tx_find_uid(ctx, entry["uid"]) == "PyClip",
            error=out.get("error"),
        )
        out = await revert(session, ctx, import_env)
        rec(
            ctx,
            "n4_reverting_the_import_removes_the_clip_this_run_brought_in",
            out.get("ok") is True
            and await tx_find_uid(ctx, entry["uid"]) is None
            and uids_of(await tx_children(ctx, ids["libReelWt"])) == [],
            error=out.get("error"),
        )
