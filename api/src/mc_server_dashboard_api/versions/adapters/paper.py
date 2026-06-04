"""Paper catalog adapter: the PaperMC v2 API (FR-VER-1/2).

Resolution walks the PaperMC v2 API through the injected :class:`JsonFetcher`:

1. ``/projects/paper`` lists every supported MC version.
2. ``/projects/paper/versions/{version}`` lists the builds for a version; the
   newest build is chosen.
3. ``/projects/paper/versions/{version}/builds/{build}`` carries
   ``downloads.application`` — the JAR file name and its SHA-256; the download URL
   is the conventional ``.../builds/{build}/downloads/{name}``.
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

_BASE = "https://api.papermc.io/v2/projects/paper"


@dataclass(frozen=True)
class PaperCatalog(VersionCatalog):
    """Resolve Paper server JARs from the PaperMC v2 API."""

    fetcher: JsonFetcher

    async def list_versions(self, server_type: ServerType) -> list[VersionRef]:
        _require_paper(server_type)
        project = await self.fetcher.get_json(_BASE)
        versions = _string_list(project, "versions")
        # The PaperMC project lists versions oldest-first; present newest-first to
        # match the vanilla catalog's ordering.
        return [
            VersionRef(server_type=ServerType.PAPER, version=v)
            for v in reversed(versions)
        ]

    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        _require_paper(server_type)
        version_detail = await self.fetcher.get_json(f"{_BASE}/versions/{version}")
        builds = _int_list(version_detail, "builds")
        if not builds:
            raise UnknownVersionError(f"paper {version}")
        build = max(builds)
        build_detail = await self.fetcher.get_json(
            f"{_BASE}/versions/{version}/builds/{build}"
        )
        application = _application(build_detail)
        if application is None:
            raise UnknownVersionError(f"paper {version} build {build} has no download")
        name = str(application["name"])
        return JarSource(
            server_type=ServerType.PAPER,
            version=version,
            url=f"{_BASE}/versions/{version}/builds/{build}/downloads/{name}",
            expected_hash=str(application["sha256"]),
            hash_algorithm=HashAlgorithm.SHA256,
        )


def _require_paper(server_type: ServerType) -> None:
    if server_type is not ServerType.PAPER:
        raise UnknownVersionError(f"paper catalog cannot serve {server_type.value}")


def _string_list(payload: object, key: str) -> list[str]:
    if not isinstance(payload, dict):
        raise UnknownVersionError("malformed paper response")
    values = payload.get(key, [])
    if not isinstance(values, list):
        raise UnknownVersionError("malformed paper response")
    return [str(v) for v in values]


def _int_list(payload: object, key: str) -> list[int]:
    if not isinstance(payload, dict):
        raise UnknownVersionError(f"unknown paper version: {payload!r}")
    values = payload.get(key, [])
    if not isinstance(values, list):
        raise UnknownVersionError("malformed paper response")
    return [int(v) for v in values]


def _application(build_detail: object) -> dict[str, Any] | None:
    if not isinstance(build_detail, dict):
        return None
    downloads = build_detail.get("downloads")
    if not isinstance(downloads, dict):
        return None
    application = downloads.get("application")
    if (
        not isinstance(application, dict)
        or "name" not in application
        or "sha256" not in application
    ):
        return None
    return application
