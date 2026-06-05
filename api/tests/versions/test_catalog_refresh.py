"""The :class:`CatalogRefresh` use case: cache invalidation per type (issue #286).

The use case drops the in-process manifest-cache entries that belong to a server
type (or all types), so the next catalog GET refetches from the source. It owns
the per-type URL-prefix map and translates ``server_type`` -> the cache entries to
clear; an unknown type is :class:`UnknownServerTypeError`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mc_server_dashboard_api.versions.application.catalog_refresh import CatalogRefresh
from mc_server_dashboard_api.versions.domain.cache import CacheInvalidator
from mc_server_dashboard_api.versions.domain.errors import UnknownServerTypeError
from mc_server_dashboard_api.versions.domain.value_objects import ServerType


class _RecordingInvalidator(CacheInvalidator):
    def __init__(self) -> None:
        self.cleared_for: list[str] = []

    def invalidate(self, predicate: Callable[[str], bool]) -> int:
        # Record which URLs the predicate matches over a representative sample so
        # the test can assert *which* type's entries would be dropped.
        sample = {
            ServerType.VANILLA: "https://launchermeta.mojang.com/x",
            ServerType.PAPER: "https://api.papermc.io/v2/x",
            ServerType.FABRIC: "https://meta.fabricmc.net/v2/x",
            ServerType.FORGE: "https://maven.minecraftforge.net/x",
        }
        matched = [url for url in sample.values() if predicate(url)]
        self.cleared_for.extend(matched)
        return len(matched)


_PREFIXES = {
    ServerType.VANILLA: "https://launchermeta.mojang.com",
    ServerType.PAPER: "https://api.papermc.io",
    ServerType.FABRIC: "https://meta.fabricmc.net",
    ServerType.FORGE: "https://maven.minecraftforge.net",
}


@pytest.mark.asyncio
async def test_refresh_all_invalidates_every_type() -> None:
    inv = _RecordingInvalidator()
    refresh = CatalogRefresh(invalidator=inv, prefixes=_PREFIXES)
    invalidated = await refresh(server_type=None)
    assert set(invalidated) == set(ServerType)
    assert len(inv.cleared_for) == 4


@pytest.mark.asyncio
async def test_refresh_one_type_invalidates_only_that_type() -> None:
    inv = _RecordingInvalidator()
    refresh = CatalogRefresh(invalidator=inv, prefixes=_PREFIXES)
    invalidated = await refresh(server_type=ServerType.PAPER)
    assert invalidated == [ServerType.PAPER]
    assert inv.cleared_for == ["https://api.papermc.io/v2/x"]


@pytest.mark.asyncio
async def test_refresh_unknown_type_raises() -> None:
    inv = _RecordingInvalidator()
    refresh = CatalogRefresh(invalidator=inv, prefixes={ServerType.VANILLA: "x"})
    with pytest.raises(UnknownServerTypeError):
        await refresh(server_type=ServerType.PAPER)
