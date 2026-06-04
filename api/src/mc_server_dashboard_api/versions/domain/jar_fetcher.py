"""The ``JarFetcher`` seam: download a JAR's bytes from an external source.

Separate from :class:`JsonFetcher` because it returns raw bytes (a JAR) rather
than parsed JSON, and because the ensure-on-start use case must hash the bytes to
verify the source's expected digest before storing (FR-VER-2/3). The bytes are
buffered whole: an M1 server JAR is tens of MB, well within memory, which keeps
the verify-then-store path simple and lets the hash be checked before a single
byte reaches the content-addressed store. A streaming, bounded-memory fetch is a
later optimisation if a backend ever serves much larger artifacts.
"""

from __future__ import annotations

import abc


class JarFetcher(abc.ABC):
    """Port: download the JAR at a URL and return its full bytes."""

    @abc.abstractmethod
    async def fetch(self, url: str) -> bytes:
        """Download ``url`` and return its bytes, or raise on a transport failure."""
