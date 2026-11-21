"""The order in which a storage GC pass walks its listing (issue #3934)."""

from __future__ import annotations

from mc_server_dashboard_api.gc_pass import ResumePoint


def _after(key: str) -> ResumePoint:
    resume_point = ResumePoint()
    resume_point.key = key
    return resume_point


def test_a_pass_with_no_resume_point_walks_the_keys_in_order() -> None:
    walk = ResumePoint(start=lambda count: 0).order(["d", "a", "c", "b"], str)

    assert walk == ["a", "b", "c", "d"]


def test_a_resumed_pass_starts_after_the_key_and_comes_round_to_it_last() -> None:
    # Listed out of key order, as the fs pool lists in directory order: "after"
    # is by key, not by where the listing happens to put the key.
    walk = _after("c").order(["c", "e", "a", "d", "b"], str)

    assert walk == ["d", "e", "a", "b", "c"]


def test_a_resume_key_no_longer_listed_still_splits_the_walk_there() -> None:
    # The object the last pass ended at may be gone by the next one.
    walk = _after("c").order(["e", "a", "d", "b"], str)

    assert walk == ["d", "e", "a", "b"]
