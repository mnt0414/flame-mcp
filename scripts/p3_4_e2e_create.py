"""Stages c (timeline.create_sequence), p (plan.run holding the construction ops) and y (the
read-only policy) of the construction e2e; see p3_4_construct_e2e.py.

JUDGED are the things measured on a real Flame beforehand (a sequence is made with the frame rate,
size, start timecode and track count asked for; a frame rate that is not one of the ten is refused;
the same name can be used twice) and the design (a revert deletes only a sequence still as made and
puts the Timeline's previous sequence back; a refused call writes nothing). OBSERVED (always ok)
is whether the Timeline shows the new sequence."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_1a_read_e2e import fill, tx_val  # noqa: E402
from p3_2a_e2e_common import name_of  # noqa: E402
from p3_3_e2e_common import BATCH_CODE  # noqa: E402
from p3_4_e2e_common import (  # noqa: E402
    FRAME_RATES,
    START_FRAMES,
    START_TC,
    err_text,
    expect_error,
    fingerprint,
    intent_of,
    lay,
    make_s,
    media_rows,
    names_of,
    new_tracks,
    outcome_of,
    restored_wt,
    revert,
    run_call,
    sel,
    stash_uids,
    state_snapshot,
    tx_children,
    tx_displayed,
    tx_exists,
    tx_open,
    tx_seq,
    tx_stash,
    tx_struct,
)

LEN12 = "p2_len12"


def made_args(ctx: Ctx, name: str, **extra: Any) -> dict[str, Any]:
    ids = ctx.ids
    rate = ids["fps"] if ids["fps"] in FRAME_RATES else "25 fps"
    return {
        "parent": sel(ids["seqReelWt"]),
        "name": name,
        "frame_rate": rate,
        "width": ids["width"],
        "height": ids["height"],
        **extra,
    }


async def reel_entries(ctx: Ctx) -> list[Any]:
    return [r[2] for r in await tx_children(ctx, ctx.ids["seqReelWt"]) or []]


# -- c: timeline.create_sequence ----------------------------------------------------------------


async def stage_c(ctx: Ctx) -> None:
    shown = await make_s(ctx, "CS")
    if shown is None:
        return
    async with server_session(ctx, "p34e-c") as session:
        opened = await tx_open(ctx, shown["wt"])
        before = await tx_displayed(ctx)
        rec(
            ctx,
            "c0_a_sequence_is_open_in_the_timeline_to_start_from",
            before is not None and before.get("uid") == shown["uid"],
            opened=opened,
            displayed=before,
        )
        args = made_args(ctx, name_of(ctx, "NEW1"), start_tc=START_TC, video_tracks=2)
        entries0 = await reel_entries(ctx)
        env = await run_call(session, ctx, "c_create", args)
        res = _result_of(env)
        made = res.get("created") or {}
        selector = made.get("selector") or {}
        facts = await tx_seq(ctx, str(selector.get("id")))
        after = await tx_displayed(ctx)
        start = START_FRAMES.get(args["frame_rate"])
        rec(
            ctx,
            "c1_the_sequence_is_made_with_the_rate_size_start_and_tracks_asked_for",
            env.get("ok") is True
            and facts is not None
            and facts["type"] == "PySequence"
            and facts["name"] == args["name"]
            and facts["frameRate"] == args["frame_rate"]
            and (facts["width"], facts["height"]) == (args["width"], args["height"])
            and facts["tracks"] == [2]
            and (start is None or facts["startTime"] == start)
            and facts["parent"] == "SEQ"
            and made.get("uid") == facts["uid"]
            and res.get("warnings") == [],
            error=env.get("error"),
            facts=facts,
            requested={k: args[k] for k in ("name", "frame_rate", "width", "height")},
        )
        sequence = res.get("sequence") or {}
        rec(
            ctx,
            "c2_the_result_says_what_transport_read_and_the_display_before_and_after",
            facts is not None
            and sequence.get("frameRate") == facts["frameRate"]
            and (sequence.get("width"), sequence.get("height")) == (facts["width"], facts["height"])
            and sequence.get("videoTracks") == 2
            and (res.get("shownBefore") or {}).get("uid") == (before or {}).get("uid")
            and (res.get("shownAfter") or {}).get("uid") == (after or {}).get("uid"),
            shownBefore=res.get("shownBefore"),
            shownAfter=res.get("shownAfter"),
            displayed=after,
        )
        observe(
            ctx,
            "c2_the_timeline_after_the_create",
            showsTheNewSequence=(after or {}).get("uid") == made.get("uid"),
            displayed=after,
        )
        journal = env.get("journal") or {}
        intent, outcome = (
            intent_of(ctx, journal.get("group")),
            outcome_of(ctx, journal.get("group")),
        )
        rec(
            ctx,
            "c3_the_journal_holds_the_intent_with_the_reel_and_the_outcome_with_what_was_made",
            intent.get("kind") == "desktop_sequence"
            and intent.get("existing") == entries0
            and (outcome.get("created") or {}).get("uid") == made.get("uid")
            and journal.get("revert", {}).get("op") == "journal.revert",
            intent={k: intent.get(k) for k in ("kind", "op", "target")},
        )
        out = await revert(session, ctx, env)
        shown_after_revert = await tx_displayed(ctx)
        rec(
            ctx,
            "c4_a_revert_deletes_the_sequence_and_the_timeline_shows_what_it_showed_before",
            out.get("ok") is True
            and not await tx_exists(ctx, str(made.get("uid")))
            and await reel_entries(ctx) == entries0
            and (shown_after_revert or {}).get("uid") == shown["uid"],
            error=out.get("error"),
            displayed=shown_after_revert,
            result={k: _result_of(out).get(k) for k in ("deleted", "reshown", "shownAfter")},
        )
        await twice_and_edited(session, ctx)
        await refusals(session, ctx, shown)


async def twice_and_edited(session: Any, ctx: Ctx) -> None:
    name = name_of(ctx, "NEW2")
    one = await run_call(session, ctx, "c_create", made_args(ctx, name))
    two = await run_call(session, ctx, "c_create", made_args(ctx, name))
    entries = names_of(await tx_children(ctx, ctx.ids["seqReelWt"]))
    facts = await tx_seq(
        ctx, str(((_result_of(one).get("created") or {}).get("selector") or {}).get("id"))
    )
    rec(
        ctx,
        "c5_the_same_name_can_be_used_twice_and_the_defaults_start_at_zero_with_one_track",
        one.get("ok") is True
        and two.get("ok") is True
        and entries.count(name) == 2
        and facts is not None
        and facts["tracks"] == [1]
        and facts["startTime"] == 0,
        error=two.get("error"),
        entries=entries,
        facts=facts,
    )
    created = _result_of(one).get("created") or {}
    start = (facts or {}).get("startTime", 0)
    filled = await run_call(
        session,
        ctx,
        "o_overwrite",
        {
            "sequence": created.get("selector"),
            "source": sel(ctx.ids["clipWt"][LEN12]),
            "at": start,
        },
    )
    struct = await tx_struct(ctx, str((created.get("selector") or {}).get("id")))
    rec(
        ctx,
        "c6_the_new_sequence_can_be_filled_from_its_start",
        filled.get("ok") is True
        and [r[1:3] for r in media_rows(struct, 0)] == [[start, start + 11]],
        error=filled.get("error"),
        track0=lay(struct, 0),
    )
    snap = await state_snapshot(ctx)
    blocked = await revert(session, ctx, one)
    rec(
        ctx,
        "c7_a_revert_of_a_sequence_that_was_edited_is_a_conflict_and_deletes_nothing",
        expect_error(blocked, "CONFLICT")
        and await tx_exists(ctx, str(created.get("uid")))
        and (await state_snapshot(ctx))["stash"] == snap["stash"],
        error=err_text(blocked),
    )
    undone = await revert(session, ctx, filled)
    restored_uid = str((_result_of(undone).get("restored") or {}).get("uid"))
    again = await revert(session, ctx, one)
    gone = _result_of(again)
    rec(
        ctx,
        "c8_a_revert_of_the_create_follows_the_sequence_through_the_revert_of_the_edit",
        undone.get("ok") is True
        and restored_uid != created.get("uid")
        and again.get("ok") is True
        and gone.get("deleted") is True
        and gone.get("alreadyGone") is False
        and gone.get("foundUid") == restored_uid
        and not await tx_exists(ctx, restored_uid)
        and not await tx_exists(ctx, str(created.get("uid")))
        and names_of(await tx_children(ctx, ctx.ids["seqReelWt"])).count(name) == 1,
        error=err_text(again),
        result={k: gone.get(k) for k in ("deleted", "alreadyGone", "foundUid", "uidChain")},
    )
    last = await revert(session, ctx, two)
    rec(ctx, "c9_the_second_one_is_reverted_too", last.get("ok") is True, error=err_text(last))


async def refusals(session: Any, ctx: Ctx, shown: dict[str, Any]) -> None:
    entries = await reel_entries(ctx)
    snap = await state_snapshot(ctx)
    library = await run_call(
        session,
        ctx,
        "c_create_refused",
        {**made_args(ctx, name_of(ctx, "NOPE")), "parent": sel(ctx.ids["libReelWt"])},
    )
    rec(
        ctx,
        "c10_a_reel_in_a_library_is_refused_and_nothing_is_written_or_journaled",
        expect_error(library, "PRECONDITION_UNMET")
        and "library" in err_text(library)
        and await state_snapshot(ctx) == snap,
        error=err_text(library),
    )
    batch = await tx_val(
        ctx,
        fill(BATCH_CODE, NAME=name_of(ctx, "BG"), SRC=ctx.ids["clipWt"]["clip1"]),
        "p34e_batch",
        timeout_s=60.0,
    )
    reel_wt = batch.get("reelWt") if isinstance(batch, dict) else None
    if isinstance(reel_wt, str) and not reel_wt.startswith("ERR:"):
        snap = await state_snapshot(ctx)
        before = names_of(await tx_children(ctx, reel_wt))
        env = await run_call(
            session,
            ctx,
            "c_create_refused",
            {**made_args(ctx, name_of(ctx, "NOPE")), "parent": sel(reel_wt)},
        )
        rec(
            ctx,
            "c11_a_reel_of_a_batch_group_is_refused_and_nothing_is_written_or_journaled",
            expect_error(env, "INVALID_ARGS")
            and "Batch" in err_text(env)
            and names_of(await tx_children(ctx, reel_wt)) == before
            and await state_snapshot(ctx) == snap,
            error=err_text(env),
        )
    else:
        rec(ctx, "c11_a_batch_group_with_a_reel_is_there", False, batch=batch)
    wrong = await run_call(
        session,
        ctx,
        "c_create_refused",
        {**made_args(ctx, name_of(ctx, "NOPE")), "parent": sel(shown["wt"])},
    )
    rec(
        ctx,
        "c12_something_that_is_not_a_reel_is_refused",
        expect_error(wrong, "SELECTOR_TYPE") and await state_snapshot(ctx) == snap,
        error=err_text(wrong),
    )
    bad = [
        ("c13_a_frame_rate_that_is_not_one_of_the_ten", {"frame_rate": "99 fps"}),
        ("c14_a_timecode_that_is_not_hh_mm_ss_ff", {"start_tc": "1:00:00"}),
        ("c15_a_frame_number_past_the_rate", {"start_tc": "00:00:00:25", "frame_rate": "25 fps"}),
        ("c16_nine_video_tracks", {"video_tracks": 9}),
    ]
    for tag, extra in bad:
        env = await run_call(
            session, ctx, "c_create_refused", {**made_args(ctx, name_of(ctx, "NOPE")), **extra}
        )
        rec(
            ctx,
            f"{tag}_is_invalid_args_and_nothing_is_written",
            expect_error(env, "INVALID_ARGS") and await state_snapshot(ctx) == snap,
            error=err_text(env),
        )
    rec(
        ctx,
        "c17_the_reel_holds_what_it_held_before_the_refusals",
        await reel_entries(ctx) == entries,
        entries=entries,
    )


# -- p: plan.run --------------------------------------------------------------------------------


async def stage_p(ctx: Ctx) -> None:
    case = await make_s(ctx, "PL")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_struct(ctx, wiretap)
    steps = [
        {"op": "timeline.create_track", "args": {"sequence": sel(wiretap)}},
        {
            "op": "timeline.overwrite",
            "args": {
                "sequence": sel(wiretap),
                "source": sel(ctx.ids["clipWt"][LEN12]),
                "at": 8,
                "track": 2,
            },
        },
        {"op": "timeline.cut", "args": {"sequence": sel(wiretap), "at": 40, "scope": "all"}},
        {"op": "timeline.create_version", "args": {"sequence": sel(wiretap)}},
    ]
    async with server_session(ctx, "p34e-p") as session:
        fp0 = await fingerprint(session, ctx, wiretap)
        snap0 = await state_snapshot(ctx)
        env = await run_call(session, ctx, "p_plan", {"steps": steps, "label": "p34e build"})
        res = _result_of(env)
        after = await tx_struct(ctx, wiretap)
        snap1 = await state_snapshot(ctx)
        rec(
            ctx,
            "p1_four_construction_steps_ran_as_one_plan_and_transport_sees_every_edit",
            env.get("ok") is True
            and [s.get("ok") for s in res.get("steps") or []] == [True] * 4
            and new_tracks(after) == [3, 1]
            and [r[1:3] for r in media_rows(after, 2) if r[3] == LEN12] == [[8, 19]]
            and [r[1:3] for r in media_rows(after, 1) if r[3] == "p2_len72"] == [[0, 39], [40, 71]],
            error=env.get("error"),
            tracks=new_tracks(after),
        )
        rec(
            ctx,
            "p2_the_plan_is_one_checkpoint_and_one_stashed_copy",
            snap1["checkpoints"] == snap0["checkpoints"] + 1
            and len(snap1["stash"]) == len(snap0["stash"]) + 1
            and (res.get("timeline") or {}).get("changed") is True,
            checkpoints=(snap0["checkpoints"], snap1["checkpoints"]),
        )
        out = await revert(session, ctx, {"journal": {"group": res.get("group")}})
        back = restored_wt(out) or wiretap
        rec(
            ctx,
            "p3_one_revert_puts_the_whole_plan_back_with_the_fingerprint_it_had",
            out.get("ok") is True
            and await fingerprint(session, ctx, back) == fp0
            and new_tracks(await tx_struct(ctx, back)) == new_tracks(before),
            error=out.get("error"),
        )
        stopping = [
            {"op": "timeline.create_track", "args": {"sequence": sel(back)}},
            {"op": "timeline.cut", "args": {"sequence": sel(back), "at": 0, "track": 0}},
        ]
        stopped = await run_call(session, ctx, "p_plan", {"steps": stopping})
        sres = _result_of(stopped)
        undone = await revert(session, ctx, {"journal": {"group": sres.get("group")}})
        rec(
            ctx,
            "p4_a_step_that_does_not_apply_stops_the_plan_and_the_revert_puts_it_all_back",
            stopped.get("ok") is False
            and stopped.get("errorCode") == "PRECONDITION_UNMET"
            and sres.get("stoppedAt") == 1
            and undone.get("ok") is True
            and await fingerprint(session, ctx, restored_wt(undone) or back) == fp0,
            error=stopped.get("error"),
        )
        entries = await reel_entries(ctx)
        snap = await state_snapshot(ctx)
        with_create = await run_call(
            session,
            ctx,
            "p_plan_with_create",
            {
                "steps": [
                    {
                        "op": "timeline.create_sequence",
                        "args": made_args(ctx, name_of(ctx, "NOPE")),
                    }
                ]
            },
        )
        rec(
            ctx,
            "p5_create_sequence_cannot_be_a_plan_step",
            with_create.get("ok") is False
            and "cannot be a plan step" in str(with_create)
            and await reel_entries(ctx) == entries
            and await state_snapshot(ctx) == snap,
            error=err_text(with_create),
        )
        stash = stash_uids(await tx_stash(ctx))
        observe(ctx, "p6_the_stash_after_the_plan", entries=len(stash))


# -- y: the read-only policy --------------------------------------------------------------------


async def stage_y(ctx: Ctx) -> None:
    case = ctx.ids.get("case", {}).get("overwrite")
    if not case:
        rec(ctx, "y0_a_sequence_to_aim_the_denied_calls_at_is_there", False)
        return
    seq = sel(case["wt"])
    clip = sel(ctx.ids["clipWt"][LEN12])
    calls = {
        "y_readonly_overwrite": {"sequence": seq, "source": clip, "at": 1},
        "y_readonly_insert": {"sequence": seq, "source": clip, "at": 1},
        "y_readonly_cut": {"sequence": seq, "at": 10, "track": 0},
        "y_readonly_create_version": {"sequence": seq},
        "y_readonly_create_track": {"sequence": seq},
        "y_readonly_create_sequence": made_args(ctx, name_of(ctx, "NOPE")),
    }
    async with server_session(ctx, "p34e-y", readonly=True) as session:
        before = await state_snapshot(ctx)
        entries = await reel_entries(ctx)
        denied = {
            call_id: (await run_call(session, ctx, call_id, args)).get("errorCode")
            for call_id, args in calls.items()
        }
        rec(
            ctx,
            "y_a_readonly_server_denies_all_six_construction_ops",
            set(denied.values()) == {"POLICY_DENIED"}
            and await state_snapshot(ctx) == before
            and await reel_entries(ctx) == entries,
            denied=denied,
        )
