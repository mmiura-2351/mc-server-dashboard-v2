"""The servers-side JAR-provisioning seam (the start path's view of the catalog).

Start ensures the resolved server JAR is in the content-addressed pool before
placement/dispatch (the issue's ensure-on-start ruling): a download/verify failure
fails the start cleanly, before any Worker is touched. The servers
domain/application may not import the versions or storage contexts (import-linter
contract), so they depend on this narrow Port; the wiring binds it to a
versions-``EnsureJar``-backed adapter that fetches + verifies + stores.

The returned content key (the JAR's SHA-256, a plain string) is recorded on the
server so a later start can short-circuit the re-download and the hydrate tar can
inject the right JAR.
"""

from __future__ import annotations

import abc


class JarProvisioningError(Exception):
    """The resolved JAR could not be fetched / verified / stored.

    Wraps any versions-context failure (catalog unavailable, hash mismatch,
    download error) so the lifecycle layer surfaces one typed start failure without
    importing the versions domain. The edge maps it to a clean failure before
    placement.
    """


class JarProvisioner(abc.ABC):
    """Port: ensure the resolved JAR is pooled; return its content key (FR-VER-3)."""

    @abc.abstractmethod
    async def ensure(
        self, *, server_type: str, version: str, known_key: str | None
    ) -> str:
        """Ensure the JAR for ``(server_type, version)`` is pooled; return its key.

        ``known_key`` is the previously-recorded content key, if any: when the JAR
        is already pooled under it, the download is skipped. Raises
        :class:`JarProvisioningError` on any failure to obtain a verified JAR.
        """
