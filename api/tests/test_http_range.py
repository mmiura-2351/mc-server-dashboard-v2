"""The single-range ``Range`` header parser (issue #2372).

Range arithmetic is off-by-one-prone, so every boundary is pinned here: the
first byte, the last byte, a single-byte range, a range ending exactly at EOF,
and the unsatisfiable/ignorable spellings.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.http_range import (
    ByteRange,
    RangeNotSatisfiableError,
    parse_byte_range,
)

_SIZE = 1000


def test_absent_header_is_no_range() -> None:
    assert parse_byte_range(None, size=_SIZE) is None


def test_first_and_last_position() -> None:
    assert parse_byte_range("bytes=0-99", size=_SIZE) == ByteRange(0, 99)


def test_open_ended_range_runs_to_the_last_byte() -> None:
    assert parse_byte_range("bytes=500-", size=_SIZE) == ByteRange(500, 999)


def test_first_byte_only() -> None:
    assert parse_byte_range("bytes=0-0", size=_SIZE) == ByteRange(0, 0)


def test_last_byte_only() -> None:
    assert parse_byte_range("bytes=999-999", size=_SIZE) == ByteRange(999, 999)


def test_range_ending_exactly_at_eof() -> None:
    assert parse_byte_range("bytes=900-999", size=_SIZE) == ByteRange(900, 999)


def test_last_position_past_eof_clamps_to_the_last_byte() -> None:
    assert parse_byte_range("bytes=900-4999", size=_SIZE) == ByteRange(900, 999)


def test_suffix_range_takes_the_final_bytes() -> None:
    assert parse_byte_range("bytes=-100", size=_SIZE) == ByteRange(900, 999)


def test_suffix_longer_than_the_representation_is_the_whole_thing() -> None:
    assert parse_byte_range("bytes=-4000", size=_SIZE) == ByteRange(0, 999)


def test_length_counts_both_endpoints() -> None:
    assert ByteRange(0, 0).length == 1
    assert ByteRange(900, 999).length == 100


def test_first_position_at_or_past_eof_is_unsatisfiable() -> None:
    with pytest.raises(RangeNotSatisfiableError):
        parse_byte_range("bytes=1000-", size=_SIZE)


def test_zero_length_suffix_is_unsatisfiable() -> None:
    with pytest.raises(RangeNotSatisfiableError):
        parse_byte_range("bytes=-0", size=_SIZE)


def test_any_range_over_an_empty_representation_is_unsatisfiable() -> None:
    with pytest.raises(RangeNotSatisfiableError):
        parse_byte_range("bytes=0-", size=0)


@pytest.mark.parametrize(
    "header",
    [
        "bytes=100-50",  # last position before the first
        "items=0-99",  # a unit we do not understand
        "bytes=abc",  # not a range spec
        "bytes=-",  # neither positions nor a suffix length
        "bytes=",  # empty range set
        "0-99",  # no unit
        "bytes=0-99,200-299",  # multi-range: served as if Range were absent
    ],
)
def test_unusable_range_is_ignored(header: str) -> None:
    assert parse_byte_range(header, size=_SIZE) is None
