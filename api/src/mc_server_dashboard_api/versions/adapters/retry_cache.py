"""Retry + cached-fallback wrapper over a :class:`JsonFetcher` (FR-VER-2).

The cache/fallback design, kept simple and honest (the issue's guidance): an
**in-process TTL cache** of the last-good payload per URL. A fetch first tries the
upstream with a bounded, jittered retry budget; on success it refreshes the cache
and returns. If the budget is spent, it serves the last-good cached payload when
one is still within its TTL, so a transient source outage degrades gracefully
rather than failing the whole catalog. Only when there is no usable cache does the
failure surface — as a :class:`CatalogUnavailableError` (a ``VersionError``), the
single choke point that gives every consumer a typed domain error to map (the edge
turns it into a 503), so a cold-cache source outage never leaks a bare
``FetchError`` out as a 500.

This is deliberately *not* persisted to Storage: the manifest payloads are small
and re-fetched cheaply on a cold process, and the content-addressed JarStore is a
blob store, not a key/value cache — a disk-backed manifest cache would add a
second persistence path for marginal benefit. The honest limitation is that a
cold process with the source down cannot list/resolve until the source recovers;
that is acceptable at M1 (a started server already has its JAR in the pool).

The clock and sleeper are injected so tests are deterministic (no wall-clock,
no real delay); jitter is drawn from an injected ``random`` callable.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mc_server_dashboard_api.versions.domain.cache import CacheInvalidator
from mc_server_dashboard_api.versions.domain.errors import CatalogUnavailableError
from mc_server_dashboard_api.versions.domain.fetcher import (
    FetchError,
    FetchNotFoundError,
    JsonFetcher,
)


@dataclass
class _CacheEntry:
    payload: object
    stored_monotonic: float


@dataclass
class RetryCachingFetcher(JsonFetcher, CacheInvalidator):
    """Wrap a :class:`JsonFetcher` with bounded jittered retry + TTL cache fallback.

    ``attempts`` is the total number of upstream tries (>= 1). ``base_delay`` and
    ``max_delay`` bound the exponential backoff between tries; the actual sleep is
    ``min(max_delay, base_delay * 2**(try-1))`` scaled by a per-try jitter draw in
    ``[0.5, 1.5)`` so concurrent callers do not retry in lockstep. ``cache_ttl`` is
    how long a last-good payload may serve as a fallback after a failed refresh.
    """

    inner: JsonFetcher
    attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 2.0
    cache_ttl: float = 3600.0
    sleep: Callable[[float], Awaitable[None]] | None = None
    monotonic: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = field(default=lambda: 1.0)
    _cache: dict[str, _CacheEntry] = field(default_factory=dict)

    async def get_json(self, url: str) -> object:
        return await self._fetch_cached(url, self.inner.get_json)

    async def get_text(self, url: str) -> str:
        # Raw-text documents (the Forge maven-metadata.xml) flow through the same
        # retry + TTL-cache fallback as JSON; the cached value is the text body.
        payload = await self._fetch_cached(url, self.inner.get_text)
        assert isinstance(payload, str)
        return payload

    async def _fetch_cached(
        self, url: str, fetch: Callable[[str], Awaitable[object]]
    ) -> object:
        last_error: FetchError | None = None
        for attempt in range(self.attempts):
            try:
                payload = await fetch(url)
            except FetchNotFoundError:
                # A 404 is definitive ("this resource does not exist"), not a
                # transient outage — retrying won't help. Re-raise immediately
                # so catalog adapters can translate it to UnknownVersionError
                # rather than the retryable CatalogUnavailableError (#1539).
                raise
            except FetchError as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    await self._backoff(attempt)
                continue
            self._cache[url] = _CacheEntry(
                payload=payload, stored_monotonic=self.monotonic()
            )
            return payload
        cached = self._fresh_cache(url)
        if cached is not None:
            return cached.payload
        # Retry budget spent with no usable cache: this is THE choke point where
        # every catalog consumer (list, resolve, ensure-on-start) sees a typed
        # domain error instead of a bare FetchError leaking out as a 500. Translate
        # to CatalogUnavailableError so the edge maps it to a 503 (FR-VER-2).
        assert last_error is not None
        raise CatalogUnavailableError(str(last_error)) from last_error

    def invalidate(self, predicate: Callable[[str], bool]) -> int:
        """Drop every cached entry whose URL matches ``predicate`` (issue #286).

        The manual-refresh seam. The cache is a source-down fallback (a successful
        ``get_json`` always refetches and refreshes it), so dropping an entry clears
        its last-good payload: a subsequent source-down ``get_json`` for that URL
        then fails fast instead of serving the stale fallback. Returns how many
        entries were dropped so the refresh can report what it invalidated.
        """

        to_drop = [url for url in self._cache if predicate(url)]
        for url in to_drop:
            del self._cache[url]
        return len(to_drop)

    def _fresh_cache(self, url: str) -> _CacheEntry | None:
        entry = self._cache.get(url)
        if entry is None:
            return None
        if self.monotonic() - entry.stored_monotonic > self.cache_ttl:
            return None
        return entry

    async def _backoff(self, attempt: int) -> None:
        delay = min(self.max_delay, self.base_delay * (2**attempt)) * self.jitter()
        if self.sleep is not None:
            await self.sleep(delay)
