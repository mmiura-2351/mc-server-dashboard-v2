"""The ``CatalogHttpClient`` seam: the transport edge for catalog adapters.

A narrow Port the catalog adapters depend on so the external HTTP layer is a
single replaceable boundary: the real adapter is httpx-backed, tests inject a
fake that serves recorded payloads with no network (TESTING.md Section 4, the
issue's NO-live-network rule).

Mirrors the versions context's ``JsonFetcher`` seam but adds query params (the
Modrinth search endpoint needs them) and a binary :meth:`get_bytes` (import
downloads the chosen version's jar). Kept in the servers domain so a future
CurseForge adapter reuses the same boundary.
"""

from __future__ import annotations

import abc


class CatalogHttpError(Exception):
    """The request failed (network failure, non-2xx status, or bad payload).

    The catalog adapter catches this and raises a domain ``CatalogError``.
    ``status`` carries the HTTP status code when the failure was a non-2xx
    response (``None`` for a transport error or a bad payload), so the adapter
    can map a 404 to a not-found error and everything else to unavailable.
    """

    def __init__(self, message: str = "", *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CatalogHttpClient(abc.ABC):
    """Port: GET a catalog URL as JSON or as raw bytes."""

    @abc.abstractmethod
    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> object:
        """Fetch ``url`` (with optional query ``params``) and return parsed JSON.

        Raises :class:`CatalogHttpError` on failure.
        """

    @abc.abstractmethod
    async def get_bytes(self, url: str) -> bytes:
        """Fetch ``url`` and return the response body as bytes.

        Raises :class:`CatalogHttpError` on failure.
        """
