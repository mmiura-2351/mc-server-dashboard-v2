"""The ``CatalogProvider`` seam: search and resolve mods from an external catalog.

A source-agnostic Port the mod-import flow depends on so a future
``CurseForgeAdapter`` (#1269) can be added behind the same interface without
reworking callers. The only implementation in v1 is the keyless Modrinth adapter
(:mod:`...servers.adapters.modrinth_catalog`).

The provider answers three questions and performs one download:

* :meth:`CatalogProvider.search` — list catalog projects matching a query, with
  optional loader and game-version facets.
* :meth:`CatalogProvider.get_project` — one project's detail plus its versions.
* :meth:`CatalogProvider.download` — fetch the chosen version's jar bytes from
  its resolved download URL.

The value objects below are the catalog's *uniform* shape, deliberately narrower
than any one source's payload: an adapter maps its source's fields onto them.
``side`` is the catalog's most-accurate signal (Modrinth ``client_side`` /
``server_side``); ``dependencies`` are the catalog dependency edges. The jar
manifest remains the uniform metadata source at import time — the catalog only
supplies what a manifest cannot (the published ``side`` and the download URL +
``sha512``).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.mod import ModSide


class CatalogError(Exception):
    """Base class for catalog-provider failures."""


class CatalogUnavailableError(CatalogError):
    """The external catalog could not be reached or returned an error.

    Network failure, a non-2xx status, or a malformed payload. The edge maps
    this to 502/503 so a transient source outage is not reported as a client
    error.
    """


class CatalogProjectNotFoundError(CatalogError):
    """The requested catalog project (or version) does not exist.

    The edge maps this to 404.
    """


@dataclass(frozen=True)
class CatalogSearchHit:
    """One project in a catalog search result.

    A summary, not a full project: enough to render a list and drill into a
    detail view. ``side`` is the catalog's deployment signal mapped onto our
    :data:`ModSide`.
    """

    project_id: str
    slug: str
    title: str
    description: str
    project_type: str
    side: ModSide
    loaders: list[str] = field(default_factory=list)
    game_versions: list[str] = field(default_factory=list)
    downloads: int = 0
    icon_url: str | None = None


@dataclass(frozen=True)
class CatalogSearchResult:
    """A page of catalog search hits plus the total match count."""

    hits: list[CatalogSearchHit]
    total: int


@dataclass(frozen=True)
class CatalogDependency:
    """A catalog-declared dependency edge of a version.

    ``project_id`` / ``version_id`` point at the depended-on catalog entity (both
    nullable in the source). ``dependency_type`` is the source's classification
    (``required`` / ``optional`` / ``incompatible`` / ``embedded``).
    """

    project_id: str | None
    version_id: str | None
    dependency_type: str


@dataclass(frozen=True)
class CatalogVersion:
    """One downloadable version of a catalog project.

    ``download_url`` resolves the primary jar; ``sha512`` is the source-published
    digest of that file (persisted as the library mod's ``sha512_hash``).
    """

    version_id: str
    project_id: str
    name: str
    version_number: str
    filename: str
    download_url: str
    sha512: str | None
    loaders: list[str] = field(default_factory=list)
    game_versions: list[str] = field(default_factory=list)
    dependencies: list[CatalogDependency] = field(default_factory=list)


@dataclass(frozen=True)
class CatalogProject:
    """A catalog project's detail plus its versions.

    ``side`` is the catalog's deployment signal; ``versions`` are the project's
    downloadable versions, most-recent first when the source orders them.
    """

    project_id: str
    slug: str
    title: str
    description: str
    project_type: str
    side: ModSide
    loaders: list[str] = field(default_factory=list)
    game_versions: list[str] = field(default_factory=list)
    versions: list[CatalogVersion] = field(default_factory=list)


class CatalogProvider(abc.ABC):
    """Port: search and resolve mods from an external mod catalog."""

    @abc.abstractmethod
    async def search(
        self,
        *,
        query: str,
        loader: str | None = None,
        game_version: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResult:
        """List catalog projects matching ``query``.

        ``loader`` and ``game_version`` are optional facets. Raises
        :class:`CatalogUnavailableError` if the source is unreachable.
        """

    @abc.abstractmethod
    async def get_project(self, project_id: str) -> CatalogProject:
        """Return a project's detail and its versions.

        ``project_id`` may be a source id or slug. Raises
        :class:`CatalogProjectNotFoundError` if it does not exist,
        :class:`CatalogUnavailableError` on a source failure.
        """

    @abc.abstractmethod
    async def get_version(self, version_id: str) -> CatalogVersion:
        """Return one version's downloadable metadata.

        Raises :class:`CatalogProjectNotFoundError` if the version does not
        exist, :class:`CatalogUnavailableError` on a source failure.
        """

    @abc.abstractmethod
    async def download(self, url: str) -> bytes:
        """Fetch the jar bytes at a version's resolved ``download_url``.

        Raises :class:`CatalogUnavailableError` on a source failure.
        """
