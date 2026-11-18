"""The bound on a Worker report's free-text ``detail`` (issue #3281).

The last crash's detail is persisted with the observed state, so the text a
Worker sends is bounded before it reaches the column or the live frame: at most
``MAX_STATUS_DETAIL_BYTES`` of UTF-8, cut on a character boundary and marked when
cut, and without the NUL that a PostgreSQL ``text`` column refuses.
"""

from __future__ import annotations

from mc_server_dashboard_api.servers.domain.value_objects import (
    MAX_STATUS_DETAIL_BYTES,
    STATUS_DETAIL_TRUNCATION_MARKER,
    bound_status_detail,
)


def test_a_detail_within_the_bound_is_kept_as_sent() -> None:
    detail = "x" * MAX_STATUS_DETAIL_BYTES

    assert bound_status_detail(detail) == detail


def test_a_longer_detail_is_cut_to_the_bound_and_marked() -> None:
    bounded = bound_status_detail("x" * (MAX_STATUS_DETAIL_BYTES + 1))

    assert bounded.endswith(STATUS_DETAIL_TRUNCATION_MARKER)
    assert len(bounded.encode()) == MAX_STATUS_DETAIL_BYTES
    assert bounded.startswith("x" * 100)


def test_the_cut_never_splits_a_character() -> None:
    # Three-byte characters: the byte budget left for the text is not a multiple
    # of three, so a byte cut would land inside one.
    bounded = bound_status_detail("あ" * MAX_STATUS_DETAIL_BYTES)

    text = bounded.removesuffix(STATUS_DETAIL_TRUNCATION_MARKER)
    assert set(text) == {"あ"}
    assert len(bounded.encode()) <= MAX_STATUS_DETAIL_BYTES


def test_a_nul_is_dropped() -> None:
    assert bound_status_detail("exit\x00 1") == "exit 1"
