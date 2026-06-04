"""The ``JsonFetcher`` seam: fetch a JSON document over HTTP (FR-VER-2).

A narrow Port the catalog adapters depend on so the external HTTP layer is a
single replaceable boundary: the real adapter is httpx-backed; tests inject a
fake that serves recorded fixture JSON with no network. Keeping the transport
behind this Port is what lets the retry + cache-fallback wrapper compose over it
uniformly and lets the adapter tests run offline (the issue's NO-live-network
rule).
"""

from __future__ import annotations

import abc


class FetchError(Exception):
    """The document could not be fetched (network failure or non-2xx status).

    The retry/cache wrapper catches this to decide between a retry, a cached
    fallback, or surfacing a catalog-unavailable error (FR-VER-2).
    """


class JsonFetcher(abc.ABC):
    """Port: GET a URL and parse the body as JSON."""

    @abc.abstractmethod
    async def get_json(self, url: str) -> object:
        """Fetch ``url`` and return the parsed JSON, or raise :class:`FetchError`."""
