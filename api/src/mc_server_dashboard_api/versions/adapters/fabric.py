"""Fabric catalog adapter: the meta.fabricmc.net v2 API (FR-VER-1/2).

Resolution walks the Fabric meta API through the injected :class:`JsonFetcher`
(so the retry + cache wrapper and the offline-fixture tests compose over it):

1. ``/v2/versions/game`` lists every supported MC (game) version with a
   ``stable`` flag, newest-first. Only stable releases are listed (snapshots are
   not offered, matching the vanilla catalog's release-only policy).
2. ``/v2/versions/loader/{game}`` confirms the game version is offered (a non-empty
   loader list) — an unknown game version yields an empty list.
3. ``/v2/versions/loader`` and ``/v2/versions/installer`` give the loader and
   installer versions; the newest *stable* of each is chosen.
4. The downloadable server launcher JAR is the meta API's generated artifact at
   ``/v2/versions/loader/{game}/{loader}/{installer}/server/jar`` (verified
   empirically: 200, ``content-type: application/java-archive``).

Unlike vanilla/paper, the Fabric meta API publishes **no checksum** for the
generated launcher JAR, so the resolved :class:`JarSource` carries no expected
hash (the ensure-on-start path stores the bytes unverified but still
content-addressed by their own SHA-256).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import UnknownVersionError
from mc_server_dashboard_api.versions.domain.fetcher import JsonFetcher
from mc_server_dashboard_api.versions.domain.value_objects import (
    JarSource,
    ServerType,
    VersionRef,
)

_BASE = "https://meta.fabricmc.net/v2"
_GAME_URL = f"{_BASE}/versions/game"
_LOADER_URL = f"{_BASE}/versions/loader"
_INSTALLER_URL = f"{_BASE}/versions/installer"


def _loader_for_game_url(game: str) -> str:
    return f"{_LOADER_URL}/{quote(game, safe='')}"


def _server_jar_url(game: str, loader: str, installer: str) -> str:
    # URL-encode each segment (defense-in-depth): game is caller-supplied and
    # loader/installer come from the upstream API.
    game_q = quote(game, safe="")
    loader_q = quote(loader, safe="")
    installer_q = quote(installer, safe="")
    return f"{_LOADER_URL}/{game_q}/{loader_q}/{installer_q}/server/jar"


@dataclass(frozen=True)
class FabricCatalog(VersionCatalog):
    """Resolve Fabric server launcher JARs from the Fabric meta API."""

    fetcher: JsonFetcher

    async def list_versions(self, server_type: ServerType) -> list[VersionRef]:
        _require_fabric(server_type)
        games = await self.fetcher.get_json(_GAME_URL)
        return [
            VersionRef(server_type=ServerType.FABRIC, version=str(entry["version"]))
            for entry in _entries(games)
            if entry.get("stable") is True and "version" in entry
        ]

    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        _require_fabric(server_type)
        loaders_for_game = await self.fetcher.get_json(_loader_for_game_url(version))
        if not _entries(loaders_for_game):
            raise UnknownVersionError(f"fabric {version}")
        loaders = await self.fetcher.get_json(_LOADER_URL)
        installers = await self.fetcher.get_json(_INSTALLER_URL)
        loader = _newest_stable(loaders, f"fabric {version}: no stable loader")
        installer = _newest_stable(installers, f"fabric {version}: no stable installer")
        return JarSource(
            server_type=ServerType.FABRIC,
            version=version,
            url=_server_jar_url(version, loader, installer),
            expected_hash=None,
            hash_algorithm=None,
        )


def _require_fabric(server_type: ServerType) -> None:
    if server_type is not ServerType.FABRIC:
        raise UnknownVersionError(f"fabric catalog cannot serve {server_type.value}")


def _entries(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise UnknownVersionError("malformed fabric response")
    return [e for e in payload if isinstance(e, dict)]


def _newest_stable(payload: object, missing: str) -> str:
    # The meta API lists loader/installer versions newest-first, so the first
    # stable entry is the newest stable one.
    for entry in _entries(payload):
        if entry.get("stable") is True and "version" in entry:
            return str(entry["version"])
    raise UnknownVersionError(missing)
