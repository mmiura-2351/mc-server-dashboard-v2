"""The order in which a storage GC pass walks its listing (issues #3934, #3965)."""

from __future__ import annotations

import random
from collections import Counter
from typing import Never

import pytest

from mc_server_dashboard_api.gc_pass import ResumePoint

_KEYS = ["d", "a", "c", "e", "b"]


def _after(key: str) -> ResumePoint:
    resume_point = ResumePoint()
    resume_point.key = key
    return resume_point


def _picked(first: int, *, descending: bool) -> ResumePoint:
    return ResumePoint(start=lambda count: first, descending=lambda: descending)


def test_a_pass_with_no_resume_point_starts_where_its_picker_says() -> None:
    # The picker is given the number of keys and names a place in key order.
    picked: list[int] = []

    def _third(count: int) -> int:
        picked.append(count)
        return 2

    walk = ResumePoint(start=_third, descending=lambda: False).order(_KEYS, str)

    assert walk == ["c", "d", "e", "a", "b"]
    assert picked == [5]


@pytest.mark.parametrize(
    ("first", "walk"),
    [
        (0, ["a", "e", "d", "c", "b"]),
        (2, ["c", "b", "a", "e", "d"]),
        (4, ["e", "d", "c", "b", "a"]),
    ],
)
def test_a_pass_with_no_resume_point_walks_down_from_its_start_when_picked(
    first: int, walk: list[str]
) -> None:
    # Down from the picked place in key order, wrapping round to the top
    # (issue #3965).
    assert _picked(first, descending=True).order(_KEYS, str) == walk


def test_half_of_all_starts_and_directions_reach_each_key_before_a_bad_one() -> None:
    # A first pass that meets a key whose failure ends it reaches only the keys
    # before that one in its walk. Upward from a random start, a key just after
    # the bad one is reached only from a start right on it. Up and down together,
    # every other key is reached from exactly half of all starts and directions,
    # wherever it lies, so a process that restarts before every pass reaches it
    # in two restarts on average (issue #3965).
    keys = [*"abcdefghij"]
    bad = "d"
    reached: Counter[str] = Counter()

    for first in range(len(keys)):
        for descending in (False, True):
            walk = _picked(first, descending=descending).order(keys, str)
            reached.update(walk[: walk.index(bad)])

    assert reached == {key: len(keys) for key in keys if key != bad}


def test_seeded_starts_and_directions_with_no_resume_point_walk_every_way() -> None:
    # A pass with no mark has nothing to resume after: the first of a process, or
    # one after a pass that came round. A random start and direction mean a
    # process that ends before its next pass, or one that restarts, cannot walk
    # the same way every time and starve the keys behind a bad one: every key
    # comes first, followed by either neighbour (issues #3934, #3965).
    rng = random.Random(3965)

    def _pass() -> list[str]:
        return ResumePoint(
            start=rng.randrange, descending=lambda: rng.random() < 0.5
        ).order(_KEYS, str)

    openings = {tuple(_pass()[:2]) for _ in range(100)}

    assert openings == {
        *zip("abcde", "bcdea", strict=True),
        *zip("abcde", "eabcd", strict=True),
    }


def test_the_default_picker_does_not_start_every_pass_at_one_key() -> None:
    # What the app wires: 200 passes all starting at one of five keys would take
    # a chance of 5 in 5**200.
    firsts = {ResumePoint().order(_KEYS, str)[0] for _ in range(200)}

    assert len(firsts) > 1


def test_the_default_direction_picker_does_not_walk_every_pass_one_way() -> None:
    # What the app wires: 200 passes from the first key all walking the same way
    # would take a chance of 2 in 2**200 (issue #3965).
    seconds = {
        ResumePoint(start=lambda count: 0).order(_KEYS, str)[1] for _ in range(200)
    }

    assert seconds == {"b", "e"}


def test_a_resumed_pass_starts_after_the_key_and_comes_round_to_it_last() -> None:
    # Listed out of key order, as the fs pool lists in directory order: "after"
    # is by key, not by where the listing happens to put the key. Neither picker
    # is asked: a pass with a mark always walks up from it (issue #3965).

    def _never(*_: int) -> Never:
        raise AssertionError("a pass with a mark asked for a start or a direction")

    resume_point = ResumePoint(start=_never, descending=_never)
    resume_point.key = "c"

    walk = resume_point.order(["c", "e", "a", "d", "b"], str)

    assert walk == ["d", "e", "a", "b", "c"]


def test_a_resume_key_no_longer_listed_still_splits_the_walk_there() -> None:
    # The object the last pass ended at may be gone by the next one.
    walk = _after("c").order(["e", "a", "d", "b"], str)

    assert walk == ["d", "e", "a", "b"]


def test_an_empty_listing_is_an_empty_walk() -> None:
    # ``random.randrange(0)`` raises: the picker must not be asked for nothing.
    assert ResumePoint().order([], str) == []
