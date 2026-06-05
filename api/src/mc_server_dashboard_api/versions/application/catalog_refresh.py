"""Manual catalog refresh: invalidate the in-process manifest cache (issue #286).

The platform-admin operational window over the versions context. The shared
:class:`RetryCachingFetcher` keys its source-down fallback cache by URL; this use
case owns the per-type URL-prefix map and drops the cached last-good payloads that
belong to a type (or every type) through the :class:`CacheInvalidator` seam.
Because the cache is a fallback (a successful GET always refetches), the practical
effect is to clear a stale last-good payload so a subsequent source-down GET fails
fast rather than serving data the admin knows is stale.

All-or-one-type: ``server_type=None`` invalidates every catalogued type;
``server_type=X`` invalidates only X. A type absent from the prefix map (i.e. not
catalogued at M1) is :class:`UnknownServerTypeError`. The return value lists which
types were invalidated, for the endpoint's response.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.versions.domain.cache import CacheInvalidator
from mc_server_dashboard_api.versions.domain.errors import UnknownServerTypeError
from mc_server_dashboard_api.versions.domain.value_objects import ServerType


@dataclass(frozen=True)
class CatalogRefresh:
    """Invalidate the manifest cache for one catalogued type or all of them."""

    invalidator: CacheInvalidator
    prefixes: dict[ServerType, str]

    async def __call__(self, *, server_type: ServerType | None) -> list[ServerType]:
        if server_type is None:
            targets = list(self.prefixes)
        else:
            if server_type not in self.prefixes:
                raise UnknownServerTypeError(server_type.value)
            targets = [server_type]
        prefixes = tuple(self.prefixes[t] for t in targets)
        self.invalidator.invalidate(lambda url: url.startswith(prefixes))
        return targets
