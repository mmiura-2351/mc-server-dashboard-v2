"""The order in which a storage GC pass walks its listing (issue #3934)."""

from __future__ import annotations

import random

from mc_server_dashboard_api.gc_pass import ResumePoint

_KEYS = ["d", "a", "c", "e", "b"]


def _after(key: str) -> ResumePoint:
    resume_point = ResumePoint()
    resume_point.key = key
    return resume_point


def test_a_pass_with_no_resume_point_starts_where_its_picker_says() -> None:
    # The picker is given the number of keys and names a place in key order.
    picked: list[int] = []

    def _third(count: int) -> int:
        picked.append(count)
        return 2

    walk = ResumePoint(start=_third, descending=lambda: False).order(_KEYS, str)

    assert walk == ["c", "d", "e", "a", "b"]
    assert picked == [5]


def test_seeded_starts_with_no_resume_point_reach_every_key_first() -> None:
    # A pass with no mark has nothing to resume after: the first of a process, or
    # one after a pass that came round. A random start means a process that ends
    # before its next pass, or one that restarts, cannot meet the same key first
    # every time and starve the keys after it.
    rng = random.Random(3934)

    firsts = {ResumePoint(start=rng.randrange).order(_KEYS, str)[0] for _ in range(50)}

    assert firsts == set(_KEYS)


def test_the_default_picker_does_not_start_every_pass_at_one_key() -> None:
    # What the app wires: 200 passes all starting at one of five keys would take
    # a chance of 5 in 5**200.
    firsts = {ResumePoint().order(_KEYS, str)[0] for _ in range(200)}

    assert len(firsts) > 1


def test_a_resumed_pass_starts_after_the_key_and_comes_round_to_it_last() -> None:
    # Listed out of key order, as the fs pool lists in directory order: "after"
    # is by key, not by where the listing happens to put the key. The picker is
    # not asked.

    def _never(count: int) -> int:
        raise AssertionError("a pass with a mark asked for a start")

    resume_point = ResumePoint(start=_never)
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
