"""Stage e (the effect ops of a segment) of the segment-edits real-Flame e2e;
see p3_3_segment_e2e.py.

JUDGED: behaviour measured on a real Flame (create_effect, the case-insensitive type, a type that is
not there / already there, a Burn-in Letterbox's mode / ratio / colour written and read back, values
outside the candidates or the range refused before anything is written, the files `save_setup`
writes per type, a setup that is saved, changed away from and loaded back, a path that is not there,
delete_effect, the reverts). OBSERVED (recorded, never failing the run): the setup files of the
types whose files were not measured, and what `load_setup` does with the setup of ANOTHER type.
Every state is read back by transport, never through the ops under test."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, judged, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_2a_e2e_common import media_row  # noqa: E402
from p3_3_e2e_common import (  # noqa: E402
    HANDLE,
    LETTERBOX,
    MEASURED_EXTENSIONS,
    UNMEASURED_TYPES,
    err_text,
    expect_error,
    fingerprint,
    fx_candidates,
    fx_index_order,
    fx_types,
    fx_values,
    make_seq,
    restored_wt,
    revert,
    run_call,
    seg_at,
    sel,
    state_snapshot,
    track_rows,
    tx_fx,
    tx_struct,
)

TYPES_ON_ONE_SEGMENT = ("Blur", "Resize", "Colour Correct", "2D Transform", "Flip", "Timewarp")


def sel_of(case: dict[str, Any], track: int, rin: int) -> dict[str, Any]:
    return seg_at(sel(case["wt"]), track, rin)


def _near(got: Any, want: list[float]) -> bool:
    return (
        isinstance(got, list)
        and len(got) == len(want)
        and all(
            isinstance(g, (int, float)) and abs(g - w) < 1e-6
            for g, w in zip(got, want, strict=True)
        )
    )


def listing(directory: Path) -> dict[str, int]:
    return {p.name: p.stat().st_size for p in sorted(directory.iterdir()) if p.is_file()}


async def fx_case(ctx: Ctx, tag: str) -> dict[str, Any] | None:
    """clip1 (track 0, no effect), clip2 (track 0, a Burn-in Letterbox ratio 1.5), p2_len72
    (track 1, head / tail 10, no effect)."""
    made = await make_seq(
        ctx,
        tag,
        ((0, "clip1", 1), (0, "clip2", 25), (1, "p2_len72", 1)),
        trims=((1, HANDLE, HANDLE),),
        fxs=((0, "clip2", LETTERBOX, {"mode": "Custom", "ratio": 1.5}),),
    )
    if made is None:
        return None
    struct = await tx_struct(ctx, made["wt"])
    one = media_row(struct, 0, "clip1")
    two = media_row(struct, 0, "clip2")
    long = media_row(struct, 1, "p2_len72")
    if not (one and two and long):
        rec(ctx, f"{tag}_the_segments_are_there", False, track0=track_rows(struct, 0))
        return None
    return {"wt": made["wt"], "oneRin": one["rin"], "twoRin": two["rin"], "longRin": long["rin"]}


async def create_fx(
    session: Any, ctx: Ctx, case: dict[str, Any], track: int, rin: int, fx_type: str
) -> dict[str, Any]:
    args = {"target": sel_of(case, track, rin), "type": fx_type}
    return await run_call(session, ctx, "e_create", args)


async def stage_e(ctx: Ctx) -> None:
    case = await fx_case(ctx, "FX")
    if case is None:
        return
    ctx.ids.setdefault("case", {})["effects"] = case
    wiretap, one, two = case["wt"], case["oneRin"], case["twoRin"]
    files = Path(ctx.ids["filesDir"]) / "setups"
    files.mkdir(parents=True, exist_ok=True)
    async with server_session(ctx, "p33-e") as session:
        # e1 / e2: create
        env = await create_fx(session, ctx, case, 0, one, "Blur")
        rows = await tx_fx(ctx, wiretap, 0, one)
        res = _result_of(env)
        ok, parts = judged(
            op_ok=env.get("ok") is True,
            transport_holds_only_a_blur=fx_types(rows) == ["Blur"],
            result_names_the_created_blur=(res.get("created") or {}).get("type") == "Blur",
            result_lists_only_a_blur_after=[
                r["type"] for r in (res.get("effects") or {}).get("after") or []
            ]
            == ["Blur"],
        )
        rec(
            ctx,
            "e1_create_effect_puts_a_Blur_on_the_segment_(read_back_by_transport)",
            ok,
            clauses=parts,
            error=env.get("error"),
            transport=fx_index_order(rows),
            resultAfter=(res.get("effects") or {}).get("after"),
        )
        env = await create_fx(session, ctx, case, 0, one, "burn-in letterbox")
        rows = await tx_fx(ctx, wiretap, 0, one)
        names = sorted((fx_values(rows, LETTERBOX) or {}).keys())
        ok, parts = judged(
            op_ok=env.get("ok") is True,
            transport_holds_blur_and_letterbox=fx_types(rows) == sorted(["Blur", LETTERBOX]),
            result_type_is_the_listed_spelling=_result_of(env).get("type") == LETTERBOX,
            result_keeps_the_requested_spelling=_result_of(env).get("requestedType")
            == "burn-in letterbox",
            letterbox_attributes_are_there={"bypass", "mode", "ratio", "colour"} <= set(names),
        )
        rec(
            ctx,
            "e2_a_type_that_differs_only_in_case_is_taken_as_the_listed_spelling",
            ok,
            clauses=parts,
            error=env.get("error"),
            attributes=names,
            transport=fx_index_order(rows),
        )
        snap = await state_snapshot(ctx)
        again = await create_fx(session, ctx, case, 0, one, "Blur")
        unknown = await create_fx(session, ctx, case, 0, one, "NoSuchEffect")
        rec(
            ctx,
            "e3_a_type_the_segment_has_is_refused_before_anything_is_copied_or_journaled",
            expect_error(again, "PRECONDITION_UNMET")
            and "already has" in err_text(again)
            and await state_snapshot(ctx) == snap,
            error=again.get("error"),
        )
        available = _result_of(unknown).get("availableTypes") or []
        rec(
            ctx,
            "e4_an_unknown_type_is_invalid_args_with_the_types_to_choose_from",
            expect_error(unknown, "INVALID_ARGS")
            and "Blur" in available
            and LETTERBOX in available
            and await state_snapshot(ctx) == snap,
            error=unknown.get("error"),
            availableCount=len(available),
        )

        # e5: set_effect (Burn-in Letterbox: mode / ratio / colour)
        rows = await tx_fx(ctx, wiretap, 0, one)
        candidates = fx_candidates(rows, LETTERBOX)
        modes = candidates.get("mode") if isinstance(candidates.get("mode"), list) else []
        current = fx_values(rows, LETTERBOX) or {}
        new_mode = next((m for m in modes if m != current.get("mode")), None)
        rec(
            ctx,
            "e5_the_attribute_candidates_are_as_plan_60_read_them",
            len(modes) == 18
            and _near(candidates.get("ratio"), [0.01, 100.0])
            and candidates.get("colour") == [[0, 0, 0], [1, 1, 1]],
            modes=len(modes),
            ratio=candidates.get("ratio"),
            colour=candidates.get("colour"),
        )
        fp_before_set = await fingerprint(session, ctx, wiretap)
        values = {"mode": new_mode, "ratio": 1.85, "colour": [1.0, 0.0, 0.0]}
        env = await run_call(
            session,
            ctx,
            "e_set",
            {"target": sel_of(case, 0, one), "type": LETTERBOX, "values": values},
        )
        rows = await tx_fx(ctx, wiretap, 0, one)
        got = fx_values(rows, LETTERBOX) or {}
        rec(
            ctx,
            "e6_set_effect_wrote_mode_ratio_and_colour_and_transport_reads_them_back",
            env.get("ok") is True
            and got.get("mode") == new_mode
            and abs(float(got.get("ratio", 0)) - 1.85) < 1e-6
            and got.get("colour") == [1.0, 0.0, 0.0]
            and _result_of(env).get("applied") == ["mode", "ratio", "colour"],
            error=env.get("error"),
            values=values,
            got=got,
        )
        fp_after_set = await fingerprint(session, ctx, wiretap)
        out = await revert(session, ctx, env)
        new_wt = restored_wt(out)
        rec(
            ctx,
            "e7_a_revert_puts_the_old_settings_back_with_the_old_fingerprint",
            out.get("ok") is True
            and fp_after_set != fp_before_set
            and new_wt is not None
            and await fingerprint(session, ctx, new_wt) == fp_before_set
            and (fx_values(await tx_fx(ctx, new_wt, 0, one), LETTERBOX) or {}).get("ratio")
            == current.get("ratio"),
            error=out.get("error"),
        )
        wiretap = new_wt or wiretap
        case["wt"] = wiretap
        await refused_sets(session, ctx, case, one, modes)
        await save_load(session, ctx, case, one, files)
        await save_per_type(session, ctx, case, files)
        await delete_and_revert(session, ctx, case, one, two)


async def refused_sets(
    session: Any, ctx: Ctx, case: dict[str, Any], one: int, modes: list[Any]
) -> None:
    wiretap = case["wt"]
    snap = await state_snapshot(ctx)
    before = await tx_fx(ctx, wiretap, 0, one)
    bad = [
        ("e8_a_value_outside_the_range", {"ratio": 1000}, "outside the range"),
        ("e9_a_mode_that_is_not_a_candidate", {"mode": "no such mode"}, "is not one of"),
        (
            "e10_a_colour_element_outside_its_range",
            {"colour": [2.0, 0.0, 0.0]},
            "outside its range",
        ),
        ("e11_a_name_the_effect_does_not_list", {"amount": 3}, "no such attribute"),
    ]
    for tag, values, needle in bad:
        env = await run_call(
            session,
            ctx,
            "e_set_refused",
            {"target": sel_of(case, 0, one), "type": LETTERBOX, "values": values},
        )
        rec(
            ctx,
            f"{tag}_is_invalid_args_and_nothing_is_written_or_journaled",
            expect_error(env, "INVALID_ARGS")
            and needle in err_text(env)
            and await state_snapshot(ctx) == snap
            and await tx_fx(ctx, wiretap, 0, one) == before,
            error=env.get("error"),
        )
    one_bad = await run_call(
        session,
        ctx,
        "e_set_refused",
        {
            "target": sel_of(case, 0, one),
            "type": LETTERBOX,
            "values": {"ratio": 1.7, "mode": "no such mode"},
        },
    )
    rec(
        ctx,
        "e12_one_bad_value_stops_the_good_one_next_to_it",
        expect_error(one_bad, "INVALID_ARGS") and await tx_fx(ctx, wiretap, 0, one) == before,
        error=one_bad.get("error"),
    )


async def save_load(session: Any, ctx: Ctx, case: dict[str, Any], one: int, files: Path) -> None:
    wiretap = case["wt"]
    target = sel_of(case, 0, one)
    before_dir = listing(files)
    saved = await run_call(
        session,
        ctx,
        "e_save",
        {"target": target, "type": LETTERBOX, "path": str(files / "lb")},
    )
    wrote = {k: v for k, v in listing(files).items() if k not in before_dir}
    reported = {
        os.path.basename(f["path"]): f["size"] for f in _result_of(saved).get("files") or []
    }
    rec(
        ctx,
        "e13_save_setup_wrote_the_two_letterbox_files_and_names_exactly_those",
        saved.get("ok") is True
        and sorted(wrote) == ["lb" + ext for ext in MEASURED_EXTENSIONS[LETTERBOX]]
        and reported == wrote
        and all(size > 0 for size in wrote.values()),
        error=saved.get("error"),
        wrote=wrote,
        reported=reported,
    )
    node_file = next(
        (f["path"] for f in _result_of(saved).get("files") or [] if f["path"].endswith("_node")),
        None,
    )
    again = await run_call(
        session, ctx, "e_save", {"target": target, "type": LETTERBOX, "path": str(files / "lb")}
    )
    rec(
        ctx,
        "e14_files_that_are_already_there_are_refused_without_overwrite",
        expect_error(again, "PRECONDITION_UNMET") and "overwrite=true" in err_text(again),
        error=again.get("error"),
    )
    over = await run_call(
        session,
        ctx,
        "e_save",
        {"target": target, "type": LETTERBOX, "path": str(files / "lb"), "overwrite": True},
    )
    rec(
        ctx,
        "e15_overwrite_true_rewrites_them_and_says_which",
        over.get("ok") is True and len(_result_of(over).get("overwritten") or []) >= 1,
        error=over.get("error"),
        overwritten=_result_of(over).get("overwritten"),
    )
    if node_file is None:
        rec(ctx, "e16_a_setup_file_to_load_is_there", False)
        return
    rows = await tx_fx(ctx, wiretap, 0, one)
    saved_values = fx_values(rows, LETTERBOX) or {}
    other = await run_call(
        session,
        ctx,
        "e_set",
        {"target": target, "type": LETTERBOX, "values": {"ratio": 4.0}},
    )
    changed = fx_values(await tx_fx(ctx, wiretap, 0, one), LETTERBOX) or {}
    loaded = await run_call(
        session,
        ctx,
        "e_load",
        {"target": target, "type": LETTERBOX, "path": node_file},
    )
    back = fx_values(await tx_fx(ctx, wiretap, 0, one), LETTERBOX) or {}
    rec(
        ctx,
        "e16_load_setup_brings_the_saved_values_back_after_set_effect_changed_them",
        other.get("ok") is True
        and loaded.get("ok") is True
        and changed.get("ratio") != saved_values.get("ratio")
        and back == saved_values
        and _result_of(loaded).get("returned") is True,
        error=loaded.get("error"),
        saved=saved_values,
        changed=changed,
        back=back,
        result={k: _result_of(loaded).get(k) for k in ("returned", "exception", "changedKeys")},
    )
    snap = await state_snapshot(ctx)
    missing = await run_call(
        session,
        ctx,
        "e_load",
        {"target": target, "type": LETTERBOX, "path": str(files / "no_such_file.letterbox_node")},
    )
    rec(
        ctx,
        "e17_a_path_that_is_not_there_is_refused_on_this_machine_and_nothing_is_journaled",
        expect_error(missing, "PRECONDITION_UNMET")
        and "no setup file" in err_text(missing)
        and await state_snapshot(ctx) == snap,
        error=missing.get("error"),
    )
    nofx = await run_call(
        session,
        ctx,
        "e_load",
        {"target": target, "type": "Flip", "path": node_file},
    )
    rec(
        ctx,
        "e18_loading_into_a_type_the_segment_has_not_says_to_create_it_first",
        expect_error(nofx, "PRECONDITION_UNMET") and "create_effect first" in err_text(nofx),
        error=nofx.get("error"),
    )
    blur = await run_call(
        session, ctx, "e_save", {"target": target, "type": "Blur", "path": str(files / "bl")}
    )
    blur_file = next((f["path"] for f in _result_of(blur).get("files") or []), None)
    if blur_file is not None:
        wrong = await run_call(
            session,
            ctx,
            "e_load",
            {"target": target, "type": LETTERBOX, "path": blur_file},
        )
        after = fx_values(await tx_fx(ctx, wiretap, 0, one), LETTERBOX)
        observe(
            ctx,
            "e19_load_setup_of_another_types_setup_(not_measured_before)",
            ok=wrong.get("ok"),
            errorCode=wrong.get("errorCode"),
            error=wrong.get("error"),
            returned=_result_of(wrong).get("returned"),
            exception=_result_of(wrong).get("exception"),
            changedKeys=_result_of(wrong).get("changedKeys"),
            valuesStillTheSaved=after == back,
        )


async def save_per_type(session: Any, ctx: Ctx, case: dict[str, Any], files: Path) -> None:
    """Every measured type on ONE segment (a segment holds one of each type), saved one by one."""
    wiretap, rin = case["wt"], case["longRin"]
    target = sel_of(case, 1, rin)
    for fx_type in (*TYPES_ON_ONE_SEGMENT, LETTERBOX):
        env = await create_fx(session, ctx, case, 1, rin, fx_type)
        rec(
            ctx,
            f"e20_create_{fx_type.replace(' ', '_')}_on_the_long_segment",
            env.get("ok") is True,
            error=env.get("error"),
        )
    rows = await tx_fx(ctx, wiretap, 1, rin)
    rec(
        ctx,
        "e21_the_segment_holds_all_seven_types",
        fx_types(rows) == sorted((*TYPES_ON_ONE_SEGMENT, LETTERBOX)),
        types=fx_types(rows),
    )
    names: dict[str, list[str]] = {}
    for fx_type, extensions in MEASURED_EXTENSIONS.items():
        stem = "t_" + "".join(ch for ch in fx_type.lower() if ch.isalnum())
        before = listing(files)
        env = await run_call(
            session, ctx, "e_save", {"target": target, "type": fx_type, "path": str(files / stem)}
        )
        wrote = sorted(k for k in listing(files) if k not in before)
        names[fx_type] = wrote
        rec(
            ctx,
            f"e22_{stem}_save_setup_wrote_the_files_plan_59_measured",
            env.get("ok") is True
            and wrote == sorted(stem + ext for ext in extensions)
            and sorted(os.path.basename(f["path"]) for f in _result_of(env).get("files") or [])
            == wrote,
            error=env.get("error"),
            wrote=wrote,
            expected=[stem + ext for ext in extensions],
        )
    for fx_type in UNMEASURED_TYPES:
        created = await create_fx(session, ctx, case, 1, rin, fx_type)
        stem = "u_" + "".join(ch for ch in fx_type.lower() if ch.isalnum())
        before = listing(files)
        saved = (
            await run_call(
                session,
                ctx,
                "e_save",
                {"target": target, "type": fx_type, "path": str(files / stem)},
            )
            if created.get("ok") is True
            else None
        )
        observe(
            ctx,
            f"e23_unmeasured_type_{stem}",
            created=created.get("ok"),
            createError=created.get("error"),
            saveOk=None if saved is None else saved.get("ok"),
            saveError=None if saved is None else saved.get("error"),
            wrote=sorted(k for k in listing(files) if k not in before),
            files=[f["path"] for f in _result_of(saved or {}).get("files") or []],
        )


async def delete_and_revert(
    session: Any, ctx: Ctx, case: dict[str, Any], one: int, two: int
) -> None:
    wiretap = case["wt"]
    snap = await state_snapshot(ctx)
    none = await run_call(
        session,
        ctx,
        "e_delete",
        {"target": sel_of(case, 0, one), "type": "Resize"},
    )
    rec(
        ctx,
        "e24_deleting_a_type_the_segment_has_not_is_refused_before_anything_is_journaled",
        expect_error(none, "PRECONDITION_UNMET")
        and "no effect of type" in err_text(none)
        and await state_snapshot(ctx) == snap,
        error=none.get("error"),
    )
    fp0 = await fingerprint(session, ctx, wiretap)
    rows0 = await tx_fx(ctx, wiretap, 0, two)
    env = await run_call(
        session,
        ctx,
        "e_delete",
        {"target": sel_of(case, 0, two), "type": LETTERBOX},
    )
    rows1 = await tx_fx(ctx, wiretap, 0, two)
    ok, parts = judged(
        op_ok=env.get("ok") is True,
        transport_held_only_the_letterbox_before=fx_types(rows0) == [LETTERBOX],
        transport_holds_no_effect_after=fx_types(rows1) == [],
        result_returned_true=_result_of(env).get("returned") is True,
    )
    rec(
        ctx,
        "e25_delete_effect_takes_the_effect_off_(read_back_by_transport)",
        ok,
        clauses=parts,
        error=env.get("error"),
        before=fx_index_order(rows0),
        after=fx_index_order(rows1),
        returned=_result_of(env).get("returned"),
    )
    out = await revert(session, ctx, env)
    new_wt = restored_wt(out)
    rows2 = await tx_fx(ctx, new_wt, 0, two) if new_wt else None
    fp_after_revert = await fingerprint(session, ctx, new_wt) if new_wt else None
    ok, parts = judged(
        revert_ok=out.get("ok") is True,
        sequence_was_put_back=new_wt is not None,
        only_the_letterbox_is_back=fx_types(rows2) == [LETTERBOX],
        letterbox_settings_are_the_old_ones=fx_values(rows2, LETTERBOX)
        == fx_values(rows0, LETTERBOX),
        fingerprint_is_the_old_one=fp_after_revert is not None and fp_after_revert == fp0,
    )
    rec(
        ctx,
        "e26_a_revert_brings_the_effect_back_with_its_settings_and_the_old_fingerprint",
        ok,
        clauses=parts,
        error=out.get("error"),
        typesBefore=fx_index_order(rows0),
        typesAfter=fx_index_order(rows2),
        before=fx_values(rows0, LETTERBOX),
        after=fx_values(rows2, LETTERBOX),
        fingerprintBefore=fp0,
        fingerprintAfter=fp_after_revert,
    )
    case["wt"] = new_wt or wiretap
