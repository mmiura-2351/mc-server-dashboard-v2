"""Test doubles for the versions context (offline, deterministic).

No live network ever (TESTING.md Section 4, the issue's NO-live-network rule): the
fake :class:`JsonFetcher` serves recorded fixture payloads keyed by URL, and the
fake :class:`JarFetcher` / :class:`JarPool` hold bytes in memory.
"""

from __future__ import annotations

from mc_server_dashboard_api.versions.domain.fetcher import (
    FetchError,
    FetchNotFoundError,
    JsonFetcher,
)
from mc_server_dashboard_api.versions.domain.jar_fetcher import JarFetcher
from mc_server_dashboard_api.versions.domain.jar_pool import (
    JarPool,
    PoolEntry,
    PoolStats,
)


class FakeJsonFetcher(JsonFetcher):
    """Serve recorded JSON by URL; count calls; optionally fail every call."""

    def __init__(
        self,
        payloads: dict[str, object],
        *,
        fail: bool = False,
        not_found_urls: set[str] | None = None,
    ) -> None:
        self._payloads = payloads
        self.fail = fail
        self._not_found_urls = not_found_urls or set()
        self.calls: list[str] = []

    async def get_json(self, url: str) -> object:
        self.calls.append(url)
        if self.fail:
            raise FetchError(f"forced failure for {url}")
        if url in self._not_found_urls:
            raise FetchNotFoundError(f"404 for {url}")
        if url not in self._payloads:
            raise FetchError(f"no fixture for {url}")
        return self._payloads[url]

    async def get_text(self, url: str) -> str:
        if url in self._not_found_urls:
            raise FetchNotFoundError(f"404 for {url}")
        raise FetchError(f"no text fixture for {url}")


class FlakyJsonFetcher(JsonFetcher):
    """Fail the first ``fail_times`` calls, then serve the payload (retry tests)."""

    def __init__(self, payloads: dict[str, object], *, fail_times: int) -> None:
        self._payloads = payloads
        self._remaining_failures = fail_times
        self.calls = 0

    async def get_json(self, url: str) -> object:
        self.calls += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise FetchError(f"transient failure for {url}")
        return self._payloads[url]

    async def get_text(self, url: str) -> str:
        raise FetchError(f"no text fixture for {url}")


class FakeDocumentFetcher(JsonFetcher):
    """Serve recorded text and JSON documents by URL (the Forge catalog needs both)."""

    def __init__(
        self,
        *,
        texts: dict[str, str],
        payloads: dict[str, object],
        fail: bool = False,
        not_found_urls: set[str] | None = None,
    ) -> None:
        self.texts = texts
        self._payloads = payloads
        self.fail = fail
        self._not_found_urls = not_found_urls or set()
        self.calls: list[str] = []

    async def get_json(self, url: str) -> object:
        self.calls.append(url)
        if self.fail:
            raise FetchError(f"forced failure for {url}")
        if url not in self._payloads:
            raise FetchError(f"no fixture for {url}")
        return self._payloads[url]

    async def get_text(self, url: str) -> str:
        self.calls.append(url)
        if self.fail:
            raise FetchError(f"forced failure for {url}")
        if url in self._not_found_urls:
            raise FetchNotFoundError(f"404 for {url}")
        if url not in self.texts:
            raise FetchError(f"no text fixture for {url}")
        return self.texts[url]


class FakeJarFetcher(JarFetcher):
    """Return recorded JAR bytes by URL."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs
        self.calls: list[str] = []

    async def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        return self._blobs[url]


class FakeJarPool(JarPool):
    """In-memory content-addressed JAR pool."""

    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}
        self.put_calls = 0

    async def has(self, sha256: str) -> bool:
        return sha256 in self.stored

    async def put(self, data: bytes) -> str:
        import hashlib

        self.put_calls += 1
        key = hashlib.sha256(data).hexdigest()
        self.stored[key] = data
        return key

    async def stats(self) -> PoolStats:
        return PoolStats(
            count=len(self.stored),
            total_bytes=sum(len(b) for b in self.stored.values()),
        )

    async def list_entries(self) -> list[PoolEntry]:
        import datetime as dt

        return [
            PoolEntry(
                sha256=key,
                size_bytes=len(data),
                modified_at=dt.datetime.now(dt.UTC),
            )
            for key, data in self.stored.items()
        ]

    async def delete(self, sha256: str) -> None:
        self.stored.pop(sha256, None)
