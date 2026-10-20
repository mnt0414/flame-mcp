"""Stage b (the Resize Flame adds for a bit depth) of the segment-edits real-Flame e2e; see
p3_3_segment_e2e.py.

JUDGED: behaviour measured on Flame 2025.2.4 and 2026.2.3. A sequence whose bit depth
differs from the bit depth of the media put on it gets a TimelineFX Resize on that segment from
Flame itself, and a sequence of the same bit depth gets none; `timeline.create_sequence(bit_depth=)`
makes the sequence with the depth asked for and reads it back; `segment.delete_effect` refuses the
Resize Flame added (Flame 2026.2.3 raised "The Timeline FX cannot be deleted." for it after the
checkpoint) before anything is copied or journaled, and deletes a Resize that `create_effect` put
on a sequence of the media's bit depth. OBSERVED: the bit depth a sequence gets when none is asked
for (the project's own: 10 in one test project, 8 in another).

The sequences are made through the op under test; the media is put on them by transport
(`overwrite`), and the effects of the segment are read both through `segment.list_effects` and by
transport, so the two are compared."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "src"))

from p1_mcp_e2e import _result_of  # noqa: E402
from p2_2a_journal_e2e import Ctx, judged, rec, server_session  # noqa: E402
from p2_4_common import observe  # noqa: E402
from p3_1a_read_e2e import fill, is_error, tx_val  # noqa: E402
from p3_2a_e2e_common import media_row  # noqa: E402
from p3_3_e2e_common import (  # noqa: E402
    CLIP_DEPTH_CODE,
    PUT_CODE,
    SEGMENT_DEPTH_CODE,
    err_text,
    expect_error,
    fingerprint,
    fx_index_order,
    name_of,
    run_call,
    seg_at,
    sel,
    state_snapshot,
    tx_fx,
    tx_struct,
)

MEDIA_CLIP: Final[str] = "clip1"
BIT_DEPTHS: Final[tuple[int, ...]] = (8, 10, 12, 16, 32)
"""What `PyReel.create_sequence(bit_depth=)` takes (measured on both versions)."""
RESIZE: Final[str] = "Resize"


def other_depth(media_depth: int) -> int:
    """A bit depth `create_sequence` takes that is not the media's (8 for a 10-bit clip)."""
    return next(d for d in BIT_DEPTHS if d != media_depth)


def op_effects(env: dict[str, Any]) -> list[str] | None:
    """The effect types `segment.list_effects` answered, in the order it lists them."""
    items = _result_of(env).get("effects")
    if env.get("ok") is not True or not isinstance(items, list):
        return None
    return [str(i.get("type")) for i in items if isinstance(i, dict)]


async def made_sequence(session: Any, ctx: Ctx, tail: str, bit_depth: int | None) -> dict[str, Any]:
    """`timeline.create_sequence` (with `bit_depth` when given), then the media put on it by
    transport. Returns what both said; `row` is the media segment's row (None when it is not
    there)."""
    ids = ctx.ids
    args: dict[str, Any] = {
        "parent": sel(ids["seqReelWt"]),
        "name": name_of(ctx, tail),
        "frame_rate": ids["fps"],
        "width": ids["width"],
        "height": ids["height"],
    }
    if bit_depth is not None:
        args["bit_depth"] = bit_depth
    env = await run_call(session, ctx, "b_create_sequence", args)
    res = _result_of(env)
    wiretap = ((res.get("created") or {}).get("selector") or {}).get("id")
    out: dict[str, Any] = {"env": env, "res": res, "wt": wiretap, "put": None, "row": None}
    if env.get("ok") is not True or not isinstance(wiretap, str):
        return out
    out["put"] = await tx_val(
        ctx, fill(PUT_CODE, SEQ=wiretap, CLIP=ids["clipWt"][MEDIA_CLIP]), f"p33_put_{tail}"
    )
    struct = await tx_struct(ctx, wiretap)
    out["row"] = media_row(struct, 0, MEDIA_CLIP)
    return out


def target_of(case: dict[str, Any]) -> dict[str, Any]:
    return seg_at(sel(case["wt"]), 0, case["row"]["rin"])


async def both_effects(
    session: Any, ctx: Ctx, case: dict[str, Any]
) -> tuple[list[str] | None, list[str]]:
    """(through `segment.list_effects`, by transport), each in the order Flame lists them."""
    listed = await run_call(session, ctx, "b_list_effects", {"target": target_of(case)})
    rows = await tx_fx(ctx, case["wt"], 0, case["row"]["rin"])
    return op_effects(listed), fx_index_order(rows)


def created_as_asked(case: dict[str, Any], depth: int) -> tuple[bool, dict[str, bool]]:
    put = case["put"] if isinstance(case["put"], dict) else {}
    return judged(
        op_ok=case["env"].get("ok") is True,
        result_reads_back_the_bit_depth=(case["res"].get("sequence") or {}).get("bitDepth")
        == depth,
        transport_reads_the_sequence_with_it=put.get("seqBitDepthBefore") == depth,
        no_warning=case["res"].get("warnings") == [],
        the_media_is_on_the_sequence=case["row"] is not None,
    )


async def stage_b(ctx: Ctx) -> None:
    ids = ctx.ids
    media_depth = await tx_val(
        ctx, fill(CLIP_DEPTH_CODE, WT=ids["clipWt"][MEDIA_CLIP]), "p33_clip_depth"
    )
    if is_error(media_depth) or media_depth not in BIT_DEPTHS:
        rec(ctx, "b0_the_bit_depth_of_the_media_is_read", False, got=media_depth)
        return
    other = other_depth(media_depth)
    rec(ctx, "b0_the_bit_depth_of_the_media_is_read", True, media=media_depth, other=other)
    async with server_session(ctx, "p33-b") as session:
        default = await made_sequence(session, ctx, "BDD", None)
        observe(
            ctx,
            "b0_the_bit_depth_a_sequence_gets_when_none_is_asked_for",
            default=(default["res"].get("sequence") or {}).get("bitDepth"),
            media=media_depth,
            error=default["env"].get("error"),
        )

        wrong = await made_sequence(session, ctx, "BDA", other)
        ok, parts = created_as_asked(wrong, other)
        rec(
            ctx,
            "b1_create_sequence_with_a_bit_depth_other_than_the_medias_reads_back_as_asked",
            ok,
            clauses=parts,
            asked=other,
            media=media_depth,
            error=wrong["env"].get("error"),
            sequence=wrong["res"].get("sequence"),
            transport=wrong["put"],
        )
        if wrong["row"] is None:
            return
        through_op, by_transport = await both_effects(session, ctx, wrong)
        source_depth = await tx_val(
            ctx,
            fill(SEGMENT_DEPTH_CODE, WT=wrong["wt"], RECORD_IN=wrong["row"]["rin"]),
            "p33_segment_depth",
        )
        ok, parts = judged(
            transport_has_one_resize=by_transport == [RESIZE],
            op_has_one_resize=through_op == [RESIZE],
            the_two_agree=through_op == by_transport,
            the_segment_source_has_the_media_depth=source_depth == media_depth,
            the_sequence_has_another_depth=other != source_depth,
        )
        rec(
            ctx,
            "b2_media_put_on_it_gets_a_Resize_from_Flame_(segment_list_effects_and_transport)",
            ok,
            clauses=parts,
            opEffects=through_op,
            transportEffects=by_transport,
            segmentSourceBitDepth=source_depth,
            sequenceBitDepth=other,
        )

        snap = await state_snapshot(ctx)
        fp_before = await fingerprint(session, ctx, wrong["wt"])
        refused = await run_call(
            session, ctx, "b_delete_resize", {"target": target_of(wrong), "type": RESIZE}
        )
        after_op, after_transport = await both_effects(session, ctx, wrong)
        fp_after = await fingerprint(session, ctx, wrong["wt"])
        ok, parts = judged(
            refused_as_a_precondition=expect_error(refused, "PRECONDITION_UNMET"),
            the_error_names_the_resize="Resize" in err_text(refused),
            nothing_was_journaled_or_copied=await state_snapshot(ctx) == snap,
            effects_through_the_op_are_unchanged=after_op == [RESIZE],
            effects_by_transport_are_unchanged=after_transport == [RESIZE],
            the_fingerprint_is_unchanged=fp_before is not None and fp_after == fp_before,
        )
        rec(
            ctx,
            "b3_delete_effect_refuses_the_Resize_Flame_added_and_nothing_changes",
            ok,
            clauses=parts,
            error=refused.get("error"),
            errorCode=refused.get("errorCode"),
            opEffectsAfter=after_op,
            transportEffectsAfter=after_transport,
        )

        same = await made_sequence(session, ctx, "BDB", media_depth)
        ok, parts = created_as_asked(same, media_depth)
        rec(
            ctx,
            "b4_create_sequence_with_the_bit_depth_of_the_media_reads_back_as_asked",
            ok,
            clauses=parts,
            asked=media_depth,
            error=same["env"].get("error"),
            sequence=same["res"].get("sequence"),
            transport=same["put"],
        )
        if same["row"] is None:
            return
        through_op, by_transport = await both_effects(session, ctx, same)
        ok, parts = judged(
            transport_has_no_effect=by_transport == [],
            op_has_no_effect=through_op == [],
        )
        rec(
            ctx,
            "b5_media_put_on_a_sequence_of_its_bit_depth_gets_no_Resize",
            ok,
            clauses=parts,
            opEffects=through_op,
            transportEffects=by_transport,
        )

        made = await run_call(
            session, ctx, "b_create_resize", {"target": target_of(same), "type": RESIZE}
        )
        with_resize = await both_effects(session, ctx, same)
        deleted = await run_call(
            session, ctx, "b_delete_resize", {"target": target_of(same), "type": RESIZE}
        )
        without = await both_effects(session, ctx, same)
        ok, parts = judged(
            create_effect_ok=made.get("ok") is True,
            the_resize_is_there=with_resize == ([RESIZE], [RESIZE]),
            delete_effect_ok=deleted.get("ok") is True,
            the_resize_is_gone=without == ([], []),
        )
        rec(
            ctx,
            "b6_a_Resize_made_by_create_effect_on_that_sequence_is_deleted_by_delete_effect",
            ok,
            clauses=parts,
            createError=made.get("error"),
            deleteError=deleted.get("error"),
            withResize=with_resize,
            without=without,
        )
