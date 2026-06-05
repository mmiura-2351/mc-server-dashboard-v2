"""The ``CacheInvalidator`` seam over the manifest cache (issue #286).

The catalog's manifest cache is a *source-down fallback* (FR-VER-2): a successful
GET always refetches from the source and refreshes the cached last-good payload;
the cache only serves when the source is unreachable. So the manual-refresh use
case drops the last-good payloads — clearing stale fallbacks so a subsequent
source-down GET fails fast rather than serving a payload an admin knows is stale.
It depends on this narrow Port rather than the concrete
:class:`RetryCachingFetcher`, which the wiring binds at the edge. The predicate
selects which cached URLs to drop (a per-type prefix match), and the method returns
how many entries were cleared so the refresh can report what it invalidated.
"""

from __future__ import annotations

import abc
from collections.abc import Callable


class CacheInvalidator(abc.ABC):
    """Port: drop cached manifest entries selected by a URL predicate."""

    @abc.abstractmethod
    def invalidate(self, predicate: Callable[[str], bool]) -> int:
        """Drop every cached entry whose URL matches ``predicate``; return the count."""
