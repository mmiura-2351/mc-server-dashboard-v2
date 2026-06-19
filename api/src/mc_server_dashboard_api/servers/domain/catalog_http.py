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


class CatalogHostNotAllowedError(CatalogHttpError):
    """A request URL's host was not on the client's allowlist (SSRF guard).

    The download URL (and the JSON base) come from a third-party API response, so
    a crafted/compromised payload — or a redirect — could point at an internal
    address. The client rejects any URL whose host is not allowlisted before the
    request is made, so the server never fetches it. A subclass of
    :class:`CatalogHttpError` (carrying no status) so the adapter maps it to
    ``CatalogUnavailableError`` like any other non-not-found failure.
    """


class CatalogTooLargeError(CatalogHttpError):
    """A streamed download crossed the byte cap and was aborted mid-stream.

    The body is streamed and aborted the moment it exceeds the cap, so an
    oversized/runaway upstream file is rejected without buffering it whole. The
    adapter maps this to the same too-large error the upload path uses (a 413),
    not to an unavailable source.
    """


class CatalogHttpClient(abc.ABC):
    """Port: GET a catalog URL as JSON or as raw bytes."""

    @abc.abstractmethod
    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> object:
        """Fetch ``url`` (with optional query ``params``) and return parsed JSON.

        Raises :class:`CatalogHttpError` on failure, or
        :class:`CatalogHostNotAllowedError` if ``url``'s host is not allowlisted.
        """

    @abc.abstractmethod
    async def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        """Fetch ``url``, streaming the body and aborting past ``max_bytes``.

        Raises :class:`CatalogHttpError` on failure,
        :class:`CatalogHostNotAllowedError` if ``url``'s host is not allowlisted,
        or :class:`CatalogTooLargeError` if the body exceeds ``max_bytes``.
        """
