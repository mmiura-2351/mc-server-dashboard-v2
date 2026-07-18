"""Forge catalog adapter: the Forge Maven + promotions feed (FR-VER-1/2, #307).

Resolution walks two upstream documents through the injected document fetcher (so
the retry + cache wrapper and the offline-fixture tests compose over it). The
shapes were verified empirically with curl (issue #307):

1. ``maven.minecraftforge.net/.../forge/maven-metadata.xml`` lists every
   published ``<mcversion>-<forgeversion>`` pair under ``<versioning><versions>``
   (not globally sorted). ``list_versions`` projects these to the *distinct* MC
   versions, newest-first (consistent with the sibling catalogs).
2. ``files.minecraftforge.net/.../forge/promotions_slim.json`` marks the
   ``<mc>-recommended`` / ``<mc>-latest`` build per MC version; the value is the
   *forge-version* segment only (e.g. ``"58.1.0"``), so the full version is
   ``<mc>-<value>``. ``resolve`` picks the recommended build, falling back to the
   latest when an MC line has no recommended promotion.

The resolved :class:`JarSource` points at the **installer** JAR
(``.../forge/<v>/forge-<v>-installer.jar``); the worker runs the supervised
``--installServer`` step on first start (the ``LAUNCH_MODE_FORGE_ARGSFILE`` path,
issue #305/#306). Forge publishes a sibling ``.sha1`` for the installer (body: a
bare lowercase-hex SHA-1), so the download is verified through the existing
hash-verification seam (SHA-1, like vanilla).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote
from xml.etree import ElementTree

from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import (
    CatalogUnavailableError,
    UnknownVersionError,
)
from mc_server_dashboard_api.versions.domain.fetcher import (
    FetchError,
    FetchNotFoundError,
    JsonFetcher,
)
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    JarSource,
    ServerType,
    VersionRef,
)

_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
_METADATA_URL = f"{_MAVEN}/maven-metadata.xml"
_PROMOTIONS_URL = (
    "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
)


def _installer_url(full_version: str) -> str:
    v = quote(full_version, safe="")
    return f"{_MAVEN}/{v}/forge-{v}-installer.jar"


def _installer_sha1_url(full_version: str) -> str:
    return f"{_installer_url(full_version)}.sha1"


@dataclass(frozen=True)
class ForgeCatalog(VersionCatalog):
    """Resolve Forge installer JARs from the Forge Maven + promotions feed."""

    fetcher: JsonFetcher

    async def list_versions(self, server_type: ServerType) -> list[VersionRef]:
        _require_forge(server_type)
        metadata = await self.fetcher.get_text(_METADATA_URL)
        return [
            VersionRef(server_type=ServerType.FORGE, version=mc)
            for mc in _distinct_mc_versions(metadata)
        ]

    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        _require_forge(server_type)
        metadata = await self.fetcher.get_text(_METADATA_URL)
        if version not in set(_distinct_mc_versions(metadata)):
            raise UnknownVersionError(f"forge {version}")
        promotions = await self.fetcher.get_json(_PROMOTIONS_URL)
        forge_version = _promoted_build(promotions, version)
        if forge_version is None:
            raise UnknownVersionError(f"forge {version} has no promoted build")
        full_version = f"{version}-{forge_version}"
        try:
            url = _installer_sha1_url(full_version)
            sha1 = (await self.fetcher.get_text(url)).strip()
        except (FetchError, CatalogUnavailableError):
            # Legacy Maven naming: some old Forge versions
            # (1.7.10, 1.8.9, 1.9.4) append the MC version.
            full_version = f"{version}-{forge_version}-{version}"
            url = _installer_sha1_url(full_version)
            try:
                sha1 = (await self.fetcher.get_text(url)).strip()
            except FetchNotFoundError:
                raise UnknownVersionError(f"forge {version}")
        return JarSource(
            server_type=ServerType.FORGE,
            version=version,
            url=_installer_url(full_version),
            expected_hash=sha1,
            hash_algorithm=HashAlgorithm.SHA1,
        )


def _require_forge(server_type: ServerType) -> None:
    if server_type is not ServerType.FORGE:
        raise UnknownVersionError(f"forge catalog cannot serve {server_type.value}")


def _distinct_mc_versions(metadata: str) -> list[str]:
    """MC versions present in the metadata, distinct and newest-first.

    The metadata lists ``<mcversion>-<forgeversion>`` entries in publish order
    (not globally sorted), so the projected MC versions are sorted newest-first to
    match the sibling catalogs' ordering.
    """

    mcs: list[str] = []
    seen: set[str] = set()
    for full in _versions(metadata):
        mc, _, forge = full.partition("-")
        if not forge:  # malformed entry without a forge segment; skip it.
            continue
        if mc not in seen:
            seen.add(mc)
            mcs.append(mc)
    return sorted(mcs, key=_mc_sort_key, reverse=True)


def _versions(metadata: str) -> list[str]:
    try:
        root = ElementTree.fromstring(metadata)
    except ElementTree.ParseError as exc:
        raise UnknownVersionError("malformed forge metadata") from exc
    return [
        e.text.strip()
        for e in root.findall("./versioning/versions/version")
        if e.text and e.text.strip()
    ]


def _mc_sort_key(mc: str) -> tuple[int, ...]:
    """Sort key for an MC version string by numeric dot-segments.

    A non-numeric segment sorts as 0 so a malformed entry never crashes the sort.
    """

    parts: list[int] = []
    for segment in mc.split("."):
        parts.append(int(segment) if segment.isdigit() else 0)
    return tuple(parts)


def _promoted_build(promotions: object, mc: str) -> str | None:
    """The recommended (else latest) forge-version segment for ``mc``, or None."""

    if not isinstance(promotions, dict):
        raise UnknownVersionError("malformed forge promotions")
    promos = promotions.get("promos")
    if not isinstance(promos, dict):
        raise UnknownVersionError("malformed forge promotions")
    recommended = promos.get(f"{mc}-recommended")
    if isinstance(recommended, str):
        return recommended
    latest = promos.get(f"{mc}-latest")
    if isinstance(latest, str):
        return latest
    return None
