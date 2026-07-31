"""The declared-length guards for streaming response bodies (issues #2318, #2415)."""

from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.http_streaming import (
    ShortResponseBodyError,
    counted,
    started,
)


async def _chunks(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _drain(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


async def test_yields_the_source_chunks_unchanged() -> None:
    chunks = [b"first", b"second", b"third"]
    declared = sum(len(chunk) for chunk in chunks)
    assert [chunk async for chunk in counted(_chunks(chunks), declared)] == chunks


async def test_raises_when_the_source_ends_below_the_declared_length() -> None:
    with pytest.raises(ShortResponseBodyError):
        await _drain(counted(_chunks([b"short"]), 99))


async def test_an_empty_source_matching_a_zero_declaration_is_fine() -> None:
    assert await _drain(counted(_chunks([]), 0)) == b""


# --- started(): locate the source before the headers (issue #2415) ---------


class _OpeningError(Exception):
    """Stands in for the store's miss, raised where the real one is: the open."""


async def _failing_open() -> AsyncIterator[bytes]:
    raise _OpeningError
    yield b""  # pragma: no cover - unreachable, keeps this an async generator


async def _failing_after(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk
    raise _OpeningError


async def test_started_delivers_the_same_bytes_in_the_same_chunks() -> None:
    # The pulled-ahead chunk is replayed, so the body a caller declared a length
    # for is byte-for-byte what it was.
    chunks = [b"first", b"second", b"third"]
    assert [chunk async for chunk in await started(_chunks(chunks))] == chunks


async def test_started_raises_the_sources_opening_failure_at_the_call() -> None:
    # The point of the helper: a store that reports its miss on the first
    # iteration reports it HERE, where the caller can still choose a status,
    # rather than after the response has started (issue #2415).
    with pytest.raises(_OpeningError):
        await started(_failing_open())


async def test_started_leaves_a_later_failure_where_it_was() -> None:
    # Only the opening half moves. A failure once the bytes are flowing still
    # surfaces during iteration, where the route's byte count fails it (#2318).
    stream = await started(_failing_after([b"first"]))
    with pytest.raises(_OpeningError):
        await _drain(stream)


async def test_started_on_an_empty_source_yields_nothing() -> None:
    # An empty source is exhausted by the pull itself; the returned stream must
    # still be an empty body rather than a replay of a chunk that never came.
    assert await _drain(await started(_chunks([]))) == b""


async def test_started_composes_with_the_declared_length_guard() -> None:
    # The two guards are used together on the download route: the count still
    # sees every byte, including the one pulled ahead.
    with pytest.raises(ShortResponseBodyError):
        await _drain(counted(await started(_chunks([b"short"])), 99))
