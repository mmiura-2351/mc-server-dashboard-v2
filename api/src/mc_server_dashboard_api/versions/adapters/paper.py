"""Paper catalog adapter: the PaperMC v3 "Fill" API (FR-VER-1/2).

Resolution walks the PaperMC Fill API through the injected :class:`JsonFetcher`
(the v2 API was sunset, returning HTTP 410 Gone):

1. ``/projects/paper/versions`` lists every supported MC version, newest-first,
   each as ``{"version": {"id": ...}}``.
2. ``/projects/paper/versions/{version}/builds/latest`` carries the newest build
   directly; ``downloads["server:default"]`` holds the JAR name, its SHA-256, and
   the ready-to-use download URL (served from ``fill-data.papermc.io``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import UnknownVersionError
from mc_server_dashboard_api.versions.domain.fetcher import JsonFetcher
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    JarSource,
    ServerType,
    VersionRef,
)

_BASE = "https://fill.papermc.io/v3/projects/paper"


@dataclass(frozen=True)
class PaperCatalog(VersionCatalog):
    """Resolve Paper server JARs from the PaperMC v3 Fill API."""

    fetcher: JsonFetcher

    async def list_versions(self, server_type: ServerType) -> list[VersionRef]:
        _require_paper(server_type)
        project = await self.fetcher.get_json(f"{_BASE}/versions")
        # The Fill /versions array is newest-first already, matching the vanilla
        # catalog's ordering, so it is presented as-is.
        return [
            VersionRef(server_type=ServerType.PAPER, version=version_id)
            for version_id in _version_ids(project)
        ]

    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        _require_paper(server_type)
        version_q = quote(version, safe="")
        build_detail = await self.fetcher.get_json(
            f"{_BASE}/versions/{version_q}/builds/latest"
        )
        download = _server_default(build_detail)
        if download is None:
            raise UnknownVersionError(f"paper {version} has no download")
        return JarSource(
            server_type=ServerType.PAPER,
            version=version,
            url=str(download["url"]),
            expected_hash=str(download["checksums"]["sha256"]),
            hash_algorithm=HashAlgorithm.SHA256,
        )


def _require_paper(server_type: ServerType) -> None:
    if server_type is not ServerType.PAPER:
        raise UnknownVersionError(f"paper catalog cannot serve {server_type.value}")


def _version_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        raise UnknownVersionError("malformed paper response")
    versions = payload.get("versions", [])
    if not isinstance(versions, list):
        raise UnknownVersionError("malformed paper response")
    ids: list[str] = []
    for entry in versions:
        if not isinstance(entry, dict):
            continue
        version = entry.get("version")
        if isinstance(version, dict) and "id" in version:
            ids.append(str(version["id"]))
    return ids


def _server_default(build_detail: object) -> dict[str, Any] | None:
    if not isinstance(build_detail, dict):
        return None
    downloads = build_detail.get("downloads")
    if not isinstance(downloads, dict):
        return None
    download = downloads.get("server:default")
    if not isinstance(download, dict) or "url" not in download:
        return None
    checksums = download.get("checksums")
    if not isinstance(checksums, dict) or "sha256" not in checksums:
        return None
    return download
