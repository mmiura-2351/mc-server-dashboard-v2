"""Vanilla catalog adapter: the Mojang version manifest (FR-VER-1/2).

Resolution is two hops, both through the injected :class:`JsonFetcher` (so the
retry + cache wrapper and the offline-fixture tests compose over it):

1. ``version_manifest_v2.json`` lists every version id and a per-version URL.
2. the per-version JSON carries ``downloads.server`` — the ``server.jar`` URL and
   its SHA-1.

Only ``release`` versions are listed (snapshots are not offered at M1). A version
whose JSON has no ``downloads.server`` (very old releases predate the server JAR)
is treated as unresolvable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import UnknownVersionError
from mc_server_dashboard_api.versions.domain.fetcher import JsonFetcher
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    JarSource,
    ServerType,
    VersionRef,
)

_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"


@dataclass(frozen=True)
class VanillaCatalog(VersionCatalog):
    """Resolve vanilla server JARs from the Mojang version manifest."""

    fetcher: JsonFetcher

    async def list_versions(self, server_type: ServerType) -> list[VersionRef]:
        _require_vanilla(server_type)
        manifest = await self.fetcher.get_json(_MANIFEST_URL)
        return [
            VersionRef(server_type=ServerType.VANILLA, version=str(entry["id"]))
            for entry in _versions(manifest)
            if entry.get("type") == "release"
        ]

    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        _require_vanilla(server_type)
        manifest = await self.fetcher.get_json(_MANIFEST_URL)
        entry = next(
            (e for e in _versions(manifest) if str(e.get("id")) == version), None
        )
        if entry is None or "url" not in entry:
            raise UnknownVersionError(f"vanilla {version}")
        detail = await self.fetcher.get_json(str(entry["url"]))
        server = _server_download(detail)
        if server is None:
            raise UnknownVersionError(f"vanilla {version} has no server download")
        return JarSource(
            server_type=ServerType.VANILLA,
            version=version,
            url=str(server["url"]),
            expected_hash=str(server["sha1"]),
            hash_algorithm=HashAlgorithm.SHA1,
        )


def _require_vanilla(server_type: ServerType) -> None:
    if server_type is not ServerType.VANILLA:
        raise UnknownVersionError(f"vanilla catalog cannot serve {server_type.value}")


def _versions(manifest: object) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        raise UnknownVersionError("malformed vanilla manifest")
    versions = manifest.get("versions", [])
    if not isinstance(versions, list):
        raise UnknownVersionError("malformed vanilla manifest")
    return [v for v in versions if isinstance(v, dict)]


def _server_download(detail: object) -> dict[str, Any] | None:
    if not isinstance(detail, dict):
        return None
    downloads = detail.get("downloads")
    if not isinstance(downloads, dict):
        return None
    server = downloads.get("server")
    if not isinstance(server, dict) or "url" not in server or "sha1" not in server:
        return None
    return server
