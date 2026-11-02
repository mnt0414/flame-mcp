"""`reel_reorder.moves_to_reorder`: the fewest entries to move to the end of a reel."""

from __future__ import annotations

import itertools

import pytest

from flame_mcp.reel_reorder import moves_to_reorder


def apply(current: list[str], moves: list[str]) -> list[str]:
    return [u for u in current if u not in moves] + moves


def test_an_order_that_is_already_right_moves_nothing() -> None:
    assert moves_to_reorder(["a", "b", "c"], ["a", "b", "c"]) == []


def test_one_entry_that_goes_last_is_the_only_move() -> None:
    assert moves_to_reorder(["a", "b", "c"], ["b", "c", "a"]) == ["a"]


def test_entries_before_the_first_difference_stay() -> None:
    assert moves_to_reorder(["a", "b", "c", "d"], ["a", "c", "b", "d"]) == ["b", "d"]


def test_a_reversed_reel_keeps_only_the_new_first_entry() -> None:
    assert moves_to_reorder(["a", "b", "c"], ["c", "b", "a"]) == ["b", "a"]


@pytest.mark.parametrize("desired", list(itertools.permutations("abcd")))
def test_every_permutation_is_reached_by_moving_to_the_end(desired: tuple[str, ...]) -> None:
    current = list("abcd")
    moves = moves_to_reorder(current, list(desired))
    assert apply(current, moves) == list(desired)
    assert len(moves) <= len(current)


def test_the_moves_are_the_fewest() -> None:
    current = list("abcd")
    for desired in itertools.permutations(current):
        moves = moves_to_reorder(current, list(desired))
        for fewer in itertools.chain.from_iterable(
            itertools.permutations(current, n) for n in range(len(moves))
        ):
            assert apply(current, list(fewer)) != list(desired)


def test_a_different_set_of_entries_is_refused() -> None:
    with pytest.raises(ValueError):
        moves_to_reorder(["a", "b"], ["a", "c"])
    with pytest.raises(ValueError):
        moves_to_reorder(["a", "b"], ["a"])
    with pytest.raises(ValueError):
        moves_to_reorder(["a", "a"], ["a", "a"])
