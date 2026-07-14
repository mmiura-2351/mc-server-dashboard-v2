"""Domain port and value objects for external plugin/mod catalog integration.

A :class:`CatalogProvider` abstracts any external catalog API (Modrinth, future
CurseForge, etc.) behind a stable domain seam. The value objects are frozen
dataclasses that carry catalog metadata without framework or transport types.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatalogSearchResult:
    """One hit from a catalog search."""

    project_id: str
    slug: str
    title: str
    description: str
    author: str
    icon_url: str | None
    downloads: int
    categories: list[str]
    latest_game_versions: list[str]


@dataclass(frozen=True)
class CatalogProject:
    """Full project detail from the catalog.

    ``client_side`` / ``server_side`` are the catalog's per-environment support
    declarations (Modrinth: ``required`` / ``optional`` / ``unsupported`` /
    ``unknown``), the most accurate source for a content's side (issue #1308).
    They default to ``"unknown"`` for catalogs that do not declare them.
    """

    project_id: str
    slug: str
    title: str
    description: str
    body: str
    author: str | None
    icon_url: str | None
    downloads: int
    categories: list[str]
    game_versions: list[str]
    loaders: list[str]
    client_side: str = "unknown"
    server_side: str = "unknown"
    # Which catalog served this project (issue #1905). ``"modrinth"`` for the
    # default catalog, ``"geyser"`` for GeyserMC's download API; maps to the
    # persisted :class:`PluginSource` at install so provenance is not lost.
    source: str = "modrinth"


@dataclass(frozen=True)
class CatalogFile:
    """A downloadable file within a catalog version.

    ``sha512`` is the integrity hash Modrinth publishes; ``sha256`` is the hash
    GeyserMC's download API publishes per build (issue #1905). A catalog file
    carries whichever its source serves -- exactly one is populated -- and the
    install path verifies the downloaded bytes against the one present.
    """

    url: str
    filename: str
    size: int
    sha512: str
    primary: bool
    sha256: str = ""


@dataclass(frozen=True)
class CatalogDependency:
    """A dependency declaration within a catalog version."""

    version_id: str | None
    project_id: str
    dependency_type: str  # "required" | "optional" | "incompatible" | "embedded"


@dataclass(frozen=True)
class CatalogVersion:
    """One version (release) of a catalog project."""

    version_id: str
    version_number: str
    name: str
    game_versions: list[str]
    loaders: list[str]
    files: list[CatalogFile]
    date_published: str
    dependencies: list[CatalogDependency] = field(default_factory=list)


@dataclass(frozen=True)
class CatalogSearchResponse:
    """Paginated search results from the catalog."""

    hits: list[CatalogSearchResult]
    total_hits: int
    offset: int
    limit: int


class CatalogProvider(abc.ABC):
    """Port: search, browse, and download from an external mod/plugin catalog."""

    @abc.abstractmethod
    async def search(
        self,
        *,
        query: str,
        loader: str,
        game_versions: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResponse: ...

    @abc.abstractmethod
    async def get_project(self, project_id_or_slug: str) -> CatalogProject: ...

    @abc.abstractmethod
    async def list_versions(
        self,
        project_id_or_slug: str,
        *,
        loader: str | None = None,
        game_versions: list[str] | None = None,
    ) -> list[CatalogVersion]: ...

    @abc.abstractmethod
    async def download_file(self, url: str) -> bytes: ...
