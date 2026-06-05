"""The ``JsonFetcher`` seam: fetch a document over HTTP (FR-VER-2).

A narrow Port the catalog adapters depend on so the external HTTP layer is a
single replaceable boundary: the real adapter is httpx-backed; tests inject a
fake that serves recorded fixture documents with no network. Keeping the
transport behind this Port is what lets the retry + cache-fallback wrapper
compose over it uniformly and lets the adapter tests run offline (the issue's
NO-live-network rule).

Most catalogs fetch JSON manifests; the Forge catalog also needs the raw text of
``maven-metadata.xml`` (issue #307), so the Port carries a sibling
:meth:`get_text` for non-JSON documents that flows through the same retry/cache
wrapper. The name is kept as ``JsonFetcher`` to avoid churning the many call
sites; treat it as a document fetcher.
"""

from __future__ import annotations

import abc


class FetchError(Exception):
    """The document could not be fetched (network failure or non-2xx status).

    The retry/cache wrapper catches this to decide between a retry, a cached
    fallback, or surfacing a catalog-unavailable error (FR-VER-2).
    """


class JsonFetcher(abc.ABC):
    """Port: GET a URL as JSON or raw text (a document fetcher)."""

    @abc.abstractmethod
    async def get_json(self, url: str) -> object:
        """Fetch ``url`` and return the parsed JSON, or raise :class:`FetchError`."""

    @abc.abstractmethod
    async def get_text(self, url: str) -> str:
        """Fetch ``url`` and return the response body as text (FR-VER-2).

        Used for non-JSON documents (the Forge ``maven-metadata.xml``); the
        catalog parses the text itself. Raises :class:`FetchError` on failure.
        """
