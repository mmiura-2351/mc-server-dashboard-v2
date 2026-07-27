"""The declared-length guard for streaming response bodies (issue #2318)."""

from collections.abc import AsyncIterator

import pytest

from mc_server_dashboard_api.http_streaming import ShortResponseBodyError, counted


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
