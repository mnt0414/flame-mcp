"""Stages o (timeline.overwrite), i (timeline.insert), k (timeline.cut) and v (timeline.
create_version / create_track) of the construction e2e; see p3_4_construct_e2e.py.

Every case is a fresh sequence made by `make_s` (track 0: clip1 0-23, Gap 24-47, clip2 48-71,
p2_len48 72-119; track 1: p2_len72 0-71), so one edit never starts from another's result. What a
call did is read back by transport (`tx_struct`) and compared with the op's own `before` / `after`
layouts. JUDGED are the things measured on a real Flame beforehand (a time is the absolute record
frame, overwrite / insert / cut land at it, insert moves only its own track, a cut needs the frame
inside a segment, media or Gap, a new version / track is one Gap); OBSERVED (always ok) are the
cases that were not measured (an overwrite that changes nothing, which tracks `PySequence.cut`
cuts)."""

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
from p3_2a_e2e_common import copy_into  # noqa: E402
from p3_4_e2e_common import (  # noqa: E402
    err_text,
    expect_error,
    fingerprint,
    lay,
    layout_matches,
    make_s,
    media_rows,
    new_tracks,
    outcome_of,
    restored_wt,
    revert,
    run_call,
    sel,
    state_snapshot,
    tx_struct,
)

LEN12 = "p2_len12"


def found(struct: Any, track: int, source: str) -> list[Any] | None:
    """The first media segment of a track that comes from `source`, as [type, in, out, source]."""
    return next((r for r in media_rows(struct, track) if r[3] == source), None)


def clip12(ctx: Ctx) -> dict[str, Any]:
    return sel(ctx.ids["clipWt"][LEN12])


async def put(
    session: Any, ctx: Ctx, call_id: str, wiretap: str, at: int, track: int = 0, **extra: Any
) -> dict[str, Any]:
    args = {"sequence": sel(wiretap), "source": clip12(ctx), "at": at, **extra}
    if track != 0 or "version" in extra:
        args["track"] = track
    return await run_call(session, ctx, call_id, args)


async def check_journal(ctx: Ctx, tag: str, env: dict[str, Any]) -> None:
    journal = env.get("journal") or {}
    outcome = outcome_of(ctx, journal.get("group"))
    rec(
        ctx,
        f"{tag}_the_journal_holds_a_checkpoint_and_the_fingerprints",
        bool(journal.get("checkpoint"))
        and journal.get("changed") is True
        and outcome.get("ok") is True
        and outcome.get("fingerprintAfter") is not None,
        journal=journal,
    )


async def revert_restores(
    session: Any,
    ctx: Ctx,
    tag: str,
    env: dict[str, Any],
    case: dict[str, Any],
    fp_before: str | None,
) -> None:
    """Revert `env`'s group; the restored sequence has the fingerprint it had. `case["wt"]` becomes
    the restored sequence's wiretap id (a restore gives it another one)."""
    out = await revert(session, ctx, env)
    case["wt"] = restored_wt(out) or case["wt"]
    rec(
        ctx,
        f"{tag}_a_revert_puts_the_sequence_back_with_the_fingerprint_it_had",
        out.get("ok") is True
        and fp_before is not None
        and await fingerprint(session, ctx, case["wt"]) == fp_before,
        error=out.get("error"),
    )


# -- o: overwrite -------------------------------------------------------------------------------


async def stage_o(ctx: Ctx) -> None:
    case = await make_s(ctx, "OW")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_struct(ctx, wiretap)
    ctx.ids.setdefault("case", {})["overwrite"] = case
    async with server_session(ctx, "p34e-o") as session:
        fp0 = await fingerprint(session, ctx, wiretap)
        env = await put(session, ctx, "o_overwrite", wiretap, 30)
        res = _result_of(env)
        after = await tx_struct(ctx, wiretap)
        placed = [r for r in media_rows(after, 0) if r[3] == LEN12]
        kept = [r for r in media_rows(before, 0) if r[3] != LEN12]
        rec(
            ctx,
            "o1_the_clip_lands_at_the_absolute_frame_and_the_other_segments_stay",
            env.get("ok") is True
            and placed
            and placed[0][1:3] == [30, 41]
            and all(r in media_rows(after, 0) for r in kept)
            and lay(after, 1) == lay(before, 1),
            error=env.get("error"),
            before=lay(before, 0),
            after=lay(after, 0),
        )
        op_after = (res.get("after") or {}).get("tracks") or []
        rec(
            ctx,
            "o2_the_result_says_what_transport_read",
            env.get("ok") is True
            and res.get("changed") is True
            and (res.get("placed") or {}).get("recordIn") == 30
            and bool(op_after)
            and layout_matches(op_after[0].get("layout"), after, 0)
            and op_after[0].get("endFrame") == max(r[2] for r in lay(after, 0))
            and (res.get("startFrame") or {}).get("changed") is False,
            result={k: res.get(k) for k in ("mode", "at", "changed", "placed", "startFrame")},
        )
        await check_journal(ctx, "o3", env)
        await revert_restores(session, ctx, "o4", env, case, fp0)
        await overwrite_past_the_end(session, ctx, case)
        await overwrite_twice(session, ctx, case)
        await overwrite_refusals(session, ctx, case)


async def overwrite_past_the_end(session: Any, ctx: Ctx, case: dict[str, Any]) -> None:
    wiretap = case["wt"]
    before = await tx_struct(ctx, wiretap)
    end = max(r[2] for r in lay(before, 0))
    env = await put(session, ctx, "o_overwrite", wiretap, end + 11)
    after = await tx_struct(ctx, wiretap)
    rows_after = lay(after, 0)
    rec(
        ctx,
        "o5_an_overwrite_after_the_end_leaves_a_gap_before_the_clip",
        env.get("ok") is True
        and rows_after[-1][:3] == ["Video Segment", end + 11, end + 22]
        and rows_after[-2][:3] == ["Gap", end + 1, end + 10],
        error=env.get("error"),
        tail=rows_after[-3:],
    )


async def overwrite_twice(session: Any, ctx: Ctx, case: dict[str, Any]) -> None:
    wiretap = case["wt"]
    first = await put(session, ctx, "o_overwrite", wiretap, 2)
    mid = await tx_struct(ctx, wiretap)
    again = await put(session, ctx, "o_overwrite", wiretap, 2)
    last = await tx_struct(ctx, wiretap)
    code_ok = again.get("ok") is True or again.get("errorCode") == "SILENT_NOOP"
    unchanged = lay(mid, 0) == lay(last, 0)
    rec(
        ctx,
        "o6_the_same_overwrite_twice_is_ok_or_a_silent_noop_and_never_another_error",
        first.get("ok") is True and code_ok and (again.get("ok") is True or unchanged),
        errorCode=again.get("errorCode"),
    )
    observe(
        ctx,
        "o6_the_same_overwrite_twice",
        ok=again.get("ok"),
        errorCode=again.get("errorCode"),
        changed=_result_of(again).get("changed"),
        layoutUnchanged=unchanged,
    )


async def overwrite_refusals(session: Any, ctx: Ctx, case: dict[str, Any]) -> None:
    wiretap = case["wt"]
    snap = await state_snapshot(ctx)
    base = {"sequence": sel(wiretap), "source": clip12(ctx), "at": 5}
    seq_as_source = await run_call(
        session, ctx, "o_overwrite_refused", {**base, "source": sel(wiretap)}
    )
    rec(
        ctx,
        "o7_a_source_that_is_not_a_clip_is_refused_and_nothing_is_copied_or_journaled",
        expect_error(seq_as_source, "SELECTOR_TYPE") and await state_snapshot(ctx) == snap,
        error=err_text(seq_as_source),
    )
    no_track = await run_call(session, ctx, "o_overwrite_refused", {**base, "track": 9})
    rec(
        ctx,
        "o8_a_track_that_is_not_there_is_refused",
        expect_error(no_track, "SELECTOR_EMPTY") and await state_snapshot(ctx) == snap,
        error=err_text(no_track),
    )
    negative = await run_call(session, ctx, "o_overwrite_refused", {**base, "at": -1})
    rec(
        ctx,
        "o9_a_negative_frame_is_invalid_args",
        expect_error(negative, "INVALID_ARGS") and await state_snapshot(ctx) == snap,
        error=err_text(negative),
    )
    copy = await copy_into(ctx, wiretap, ctx.ids["libReelWt"])
    if copy is None:
        rec(ctx, "o10_a_library_copy_is_there", False)
        return
    snap = await state_snapshot(ctx)
    before = await tx_struct(ctx, copy["wt"])
    lib = await run_call(session, ctx, "o_overwrite_refused", {**base, "sequence": sel(copy["wt"])})
    rec(
        ctx,
        "o10_a_sequence_in_a_library_is_refused_and_not_touched",
        expect_error(lib, "PRECONDITION_UNMET")
        and "library" in err_text(lib)
        and await tx_struct(ctx, copy["wt"]) == before
        and await state_snapshot(ctx) == snap,
        error=err_text(lib),
    )


# -- i: insert ----------------------------------------------------------------------------------


async def stage_i(ctx: Ctx) -> None:
    case = await make_s(ctx, "IN")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_struct(ctx, wiretap)
    async with server_session(ctx, "p34e-i") as session:
        fp0 = await fingerprint(session, ctx, wiretap)
        env = await put(session, ctx, "i_insert", wiretap, 30)
        res = _result_of(env)
        after = await tx_struct(ctx, wiretap)
        shifted = [[t, a + 12, b + 12, s] for t, a, b, s in media_rows(before, 0)[1:]]
        rec(
            ctx,
            "i1_the_clip_is_inserted_at_the_frame_and_what_follows_moves_by_its_length",
            env.get("ok") is True
            and (found(after, 0, LEN12) or [])[1:3] == [30, 41]
            and media_rows(after, 0)[0] == media_rows(before, 0)[0]
            and all(r in media_rows(after, 0) for r in shifted),
            error=env.get("error"),
            before=lay(before, 0),
            after=lay(after, 0),
        )
        rec(
            ctx,
            "i2_the_other_track_does_not_move_and_the_result_shows_the_tracks_out_of_step",
            media_rows(after, 1) == media_rows(before, 1)
            and {t["track"]: t["endFrame"] for t in (res.get("after") or {}).get("tracks") or []}
            == {0: max(r[2] for r in lay(after, 0)), 1: max(r[2] for r in lay(after, 1))},
            tracks=(res.get("after") or {}).get("tracks"),
        )
        await check_journal(ctx, "i3", env)
        await revert_restores(session, ctx, "i4", env, case, fp0)
        new_wt = case["wt"]
        again = await put(session, ctx, "i_insert", new_wt, 60)
        split = await tx_struct(ctx, new_wt)
        rec(
            ctx,
            "i5_an_insert_inside_a_segment_splits_it",
            again.get("ok") is True
            and (found(split, 0, LEN12) or [])[1:3] == [60, 71]
            and len([r for r in media_rows(split, 0) if r[3] == "clip2"]) == 2,
            error=again.get("error"),
            after=lay(split, 0),
        )
        snap = await state_snapshot(ctx)
        no_version = await put(session, ctx, "i_insert_refused", new_wt, 0, version=4)
        rec(
            ctx,
            "i6_a_version_that_is_not_there_is_refused_before_anything_is_copied",
            expect_error(no_version, "SELECTOR_EMPTY") and await state_snapshot(ctx) == snap,
            error=err_text(no_version),
        )


# -- k: cut -------------------------------------------------------------------------------------


async def stage_k(ctx: Ctx) -> None:
    case = await make_s(ctx, "CU")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_struct(ctx, wiretap)
    async with server_session(ctx, "p34e-k") as session:
        fp0 = await fingerprint(session, ctx, wiretap)
        env = await run_call(
            session, ctx, "k_cut", {"sequence": sel(wiretap), "at": 10, "track": 0}
        )
        res = _result_of(env)
        after = await tx_struct(ctx, wiretap)
        pieces = [r for r in media_rows(after, 0) if r[3] == "clip1"]
        rec(
            ctx,
            "k1_a_track_cut_makes_the_frame_the_first_one_of_the_right_part",
            env.get("ok") is True
            and [p[1:3] for p in pieces] == [[0, 9], [10, 23]]
            and lay(after, 1) == lay(before, 1)
            and res.get("split") is True,
            error=env.get("error"),
            after=lay(after, 0),
        )
        await check_journal(ctx, "k2", env)
        await revert_restores(session, ctx, "k3", env, case, fp0)
        await cut_all(session, ctx, case)
        await cut_refusals(session, ctx, case["wt"])
        await cut_in_gap(session, ctx, case)


async def cut_in_gap(session: Any, ctx: Ctx, case: dict[str, Any]) -> None:
    wiretap = case["wt"]
    before = await tx_struct(ctx, wiretap)
    gaps = [r for r in lay(before, 0) if r[0] == "Gap" and r[2] - r[1] >= 1]
    if not gaps:
        observe(ctx, "k7_no_gap_of_two_frames_in_track_0_to_cut_in", layout=lay(before, 0))
        return
    first, last = gaps[0][1], gaps[0][2]
    at = first + 1
    fp0 = await fingerprint(session, ctx, wiretap)
    env = await run_call(
        session, ctx, "k_cut_gap", {"sequence": sel(wiretap), "at": at, "track": 0}
    )
    after = await tx_struct(ctx, wiretap)
    pieces = [r[1:3] for r in lay(after, 0) if r[0] == "Gap"]
    rec(
        ctx,
        "k7_a_cut_in_a_gap_splits_the_gap",
        env.get("ok") is True
        and [first, at - 1] in pieces
        and [at, last] in pieces
        and _result_of(env).get("split") is True,
        error=env.get("error"),
        after=lay(after, 0),
    )
    await revert_restores(session, ctx, "k7b", env, case, fp0)


async def cut_all(session: Any, ctx: Ctx, case: dict[str, Any]) -> None:
    wiretap = case["wt"]
    before = await tx_struct(ctx, wiretap)
    fp0 = await fingerprint(session, ctx, wiretap)
    env = await run_call(
        session, ctx, "k_cut_all", {"sequence": sel(wiretap), "at": 40, "scope": "all"}
    )
    after = await tx_struct(ctx, wiretap)
    long_pieces = [r for r in media_rows(after, 1) if r[3] == "p2_len72"]
    rec(
        ctx,
        "k4_a_cut_of_the_whole_sequence_cuts_the_track_that_has_media_there",
        env.get("ok") is True and [p[1:3] for p in long_pieces] == [[0, 39], [40, 71]],
        error=env.get("error"),
        after=lay(after, 1),
    )
    observe(
        ctx,
        "k4_the_other_track_of_a_whole_sequence_cut",
        track0_before=lay(before, 0),
        track0_after=lay(after, 0),
        split=_result_of(env).get("split"),
    )
    await revert_restores(session, ctx, "k5", env, case, fp0)


async def cut_refusals(session: Any, ctx: Ctx, wiretap: str) -> None:
    current = await tx_struct(ctx, wiretap)
    snap = await state_snapshot(ctx)
    cases = [
        ("k6_a_cut_at_the_first_frame_of_a_segment", 0),
        ("k8_a_cut_after_the_last_segment", 500),
    ]
    for tag, at in cases:
        env = await run_call(
            session, ctx, "k_cut_refused", {"sequence": sel(wiretap), "at": at, "track": 0}
        )
        rec(
            ctx,
            f"{tag}_is_refused_before_anything_is_copied_or_journaled",
            expect_error(env, "PRECONDITION_UNMET")
            and (_result_of(env) or {}).get("ran") is False
            and await state_snapshot(ctx) == snap
            and await tx_struct(ctx, wiretap) == current,
            error=err_text(env),
        )
    invalid = [
        ("k9_scope_track_without_a_track", {"at": 10}),
        ("k10_scope_all_with_a_track", {"at": 10, "scope": "all", "track": 0}),
        ("k11_a_scope_that_is_neither", {"at": 10, "scope": "row", "track": 0}),
    ]
    for tag, extra in invalid:
        env = await run_call(session, ctx, "k_cut_refused", {"sequence": sel(wiretap), **extra})
        rec(
            ctx,
            f"{tag}_is_invalid_args",
            expect_error(env, "INVALID_ARGS") and await state_snapshot(ctx) == snap,
            error=err_text(env),
        )


# -- v: create_version / create_track -----------------------------------------------------------


async def stage_v(ctx: Ctx) -> None:
    case = await make_s(ctx, "VE")
    if case is None:
        return
    wiretap = case["wt"]
    before = await tx_struct(ctx, wiretap)
    async with server_session(ctx, "p34e-v") as session:
        fp0 = await fingerprint(session, ctx, wiretap)
        env = await run_call(session, ctx, "v_create_version", {"sequence": sel(wiretap)})
        res = _result_of(env)
        after = await tx_struct(ctx, wiretap)
        new_rows = lay(after, 0, 1) if len(after or []) > 1 else []
        rec(
            ctx,
            "v1_a_version_is_added_at_the_end_with_one_track_that_is_one_gap",
            env.get("ok") is True
            and new_tracks(after) == [2, 1]
            and len(new_rows) == 1
            and new_rows[0][0] == "Gap"
            and (res.get("version") or {}).get("index") == 1
            and res.get("versionsAfter") == [2, 1],
            error=env.get("error"),
            newVersion=new_rows,
        )
        fp_with_version = await fingerprint(session, ctx, wiretap)
        fill = await run_call(
            session,
            ctx,
            "o_overwrite",
            {"sequence": sel(wiretap), "source": clip12(ctx), "at": 0, "version": 1, "track": 0},
        )
        filled = await tx_struct(ctx, wiretap)
        rec(
            ctx,
            "v2_the_new_version_can_be_filled",
            fill.get("ok") is True
            and [r[1:3] for r in media_rows(filled, 0, 1) if r[3] == LEN12] == [[0, 11]]
            and lay(filled, 0, 0) == lay(before, 0, 0),
            error=fill.get("error"),
            version1=lay(filled, 0, 1),
        )
        await check_journal(ctx, "v3", env)
        # the fill is undone first (it changed the sequence after the version was added)
        await revert_restores(session, ctx, "v4a", fill, case, fp_with_version)
        await revert_restores(session, ctx, "v4", env, case, fp0)
        await create_track(session, ctx)


async def create_track(session: Any, ctx: Ctx) -> None:
    made = await make_s(ctx, "TR")
    if made is None:
        return
    wiretap = made["wt"]
    before = await tx_struct(ctx, wiretap)
    fp0 = await fingerprint(session, ctx, wiretap)
    env = await run_call(session, ctx, "v_create_track", {"sequence": sel(wiretap)})
    res = _result_of(env)
    after = await tx_struct(ctx, wiretap)
    rows_new = lay(after, 2)
    rec(
        ctx,
        "v5_a_track_is_added_at_the_end_of_the_version_as_one_gap",
        env.get("ok") is True
        and new_tracks(after) == [3]
        and len(rows_new) == 1
        and rows_new[0][0] == "Gap"
        and (res.get("track") or {}).get("index") == 2
        and lay(after, 0) == lay(before, 0),
        error=env.get("error"),
        newTrack=rows_new,
    )
    fp_with_track = await fingerprint(session, ctx, wiretap)
    fill = await put(session, ctx, "o_overwrite", wiretap, 8, track=2)
    filled = await tx_struct(ctx, wiretap)
    rec(
        ctx,
        "v6_the_new_track_can_be_filled",
        fill.get("ok") is True
        and [r[1:3] for r in media_rows(filled, 2) if r[3] == LEN12] == [[8, 19]],
        error=fill.get("error"),
        track2=lay(filled, 2),
    )
    snap = await state_snapshot(ctx)
    missing = await run_call(
        session, ctx, "v_create_track", {"sequence": sel(wiretap), "version": 5}
    )
    rec(
        ctx,
        "v7_a_version_that_is_not_there_is_refused",
        expect_error(missing, "SELECTOR_EMPTY") and await state_snapshot(ctx) == snap,
        error=err_text(missing),
    )
    await revert_restores(session, ctx, "v8a", fill, made, fp_with_track)
    await revert_restores(session, ctx, "v8", env, made, fp0)
