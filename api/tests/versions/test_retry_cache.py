"""Retry + cached-fallback behaviour of :class:`RetryCachingFetcher` (FR-VER-2)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.versions.adapters.retry_cache import RetryCachingFetcher
from mc_server_dashboard_api.versions.domain.errors import CatalogUnavailableError
from mc_server_dashboard_api.versions.domain.fetcher import FetchNotFoundError
from tests.versions.fakes import FakeJsonFetcher, FlakyJsonFetcher

_URL = "https://example.test/manifest.json"
_PAYLOAD = {"ok": True}


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_retries_then_succeeds() -> None:
    inner = FlakyJsonFetcher({_URL: _PAYLOAD}, fail_times=2)
    fetcher = RetryCachingFetcher(inner=inner, attempts=3, sleep=_no_sleep)
    assert await fetcher.get_json(_URL) == _PAYLOAD
    assert inner.calls == 3


@pytest.mark.asyncio
async def test_exhausted_retries_with_no_cache_raise() -> None:
    inner = FakeJsonFetcher({_URL: _PAYLOAD}, fail=True)
    fetcher = RetryCachingFetcher(inner=inner, attempts=2, sleep=_no_sleep)
    # The bare FetchError is translated to the typed domain error at this choke
    # point so consumers never see a bare Exception leak out as a 500.
    with pytest.raises(CatalogUnavailableError):
        await fetcher.get_json(_URL)


@pytest.mark.asyncio
async def test_serves_last_good_cache_when_source_down() -> None:
    inner = FakeJsonFetcher({_URL: _PAYLOAD})
    fetcher = RetryCachingFetcher(inner=inner, attempts=2, sleep=_no_sleep)
    # Prime the cache with a good fetch.
    assert await fetcher.get_json(_URL) == _PAYLOAD
    # Source goes down: the last-good payload still serves.
    inner.fail = True
    assert await fetcher.get_json(_URL) == _PAYLOAD


@pytest.mark.asyncio
async def test_invalidate_drops_last_good_fallback() -> None:
    # The cache is a source-down fallback: invalidating it drops the last-good
    # payload, so a subsequent source-down fetch fails instead of serving stale.
    inner = FakeJsonFetcher({_URL: _PAYLOAD})
    fetcher = RetryCachingFetcher(inner=inner, attempts=1, sleep=_no_sleep)
    assert await fetcher.get_json(_URL) == _PAYLOAD  # primes the fallback
    cleared = fetcher.invalidate(lambda url: True)
    assert cleared == 1
    inner.fail = True
    with pytest.raises(CatalogUnavailableError):
        await fetcher.get_json(_URL)


@pytest.mark.asyncio
async def test_invalidate_predicate_clears_only_matching() -> None:
    other = "https://other.test/manifest.json"
    inner = FakeJsonFetcher({_URL: _PAYLOAD, other: {"other": True}})
    fetcher = RetryCachingFetcher(inner=inner, attempts=1, sleep=_no_sleep)
    await fetcher.get_json(_URL)
    await fetcher.get_json(other)
    cleared = fetcher.invalidate(lambda url: url == _URL)
    assert cleared == 1
    # Source down: _URL has no fallback (invalidated) and fails; ``other`` still
    # serves its last-good payload.
    inner.fail = True
    with pytest.raises(CatalogUnavailableError):
        await fetcher.get_json(_URL)
    assert await fetcher.get_json(other) == {"other": True}


@pytest.mark.asyncio
async def test_expired_cache_does_not_serve() -> None:
    clock = [0.0]
    inner = FakeJsonFetcher({_URL: _PAYLOAD})
    fetcher = RetryCachingFetcher(
        inner=inner,
        attempts=1,
        cache_ttl=10.0,
        sleep=_no_sleep,
        monotonic=lambda: clock[0],
    )
    assert await fetcher.get_json(_URL) == _PAYLOAD
    inner.fail = True
    clock[0] = 100.0  # past the TTL
    with pytest.raises(CatalogUnavailableError):
        await fetcher.get_json(_URL)


@pytest.mark.asyncio
async def test_not_found_is_not_retried() -> None:
    """A 404 is definitive — retrying won't help (#1539)."""

    inner = FakeJsonFetcher({}, not_found_urls={_URL})
    fetcher = RetryCachingFetcher(inner=inner, attempts=3, sleep=_no_sleep)
    with pytest.raises(FetchNotFoundError):
        await fetcher.get_json(_URL)
    # Only one attempt: the 404 was not retried.
    assert len(inner.calls) == 1
