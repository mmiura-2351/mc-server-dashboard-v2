"""Use-case tests for Modrinth catalog integration (issue #1151)."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import pytest

from mc_server_dashboard_api.servers.application.catalog import (
    GetCatalogProject,
    InstallFromCatalog,
    SearchCatalog,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogFile,
    CatalogProject,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogChecksumMismatchError,
    CatalogProjectNotFoundError,
    CatalogUnavailableError,
    InvalidFilePathError,
    PluginAlreadyExistsError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginSource,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import (
    FakeCatalogProvider,
    FakeClock,
    FakeFileStore,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _server(
    *,
    community_id: CommunityId = _COMMUNITY,
    server_type: ServerType = ServerType.FABRIC,
    mc_version: str = "1.20.4",
    desired_state: DesiredState = DesiredState.STOPPED,
    observed_state: ObservedState = ObservedState.STOPPED,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community_id,
        name=ServerName("test-server"),
        mc_edition="java",
        mc_version=mc_version,
        server_type=server_type,
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=desired_state,
        observed_state=observed_state,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _project(
    *,
    project_id: str = "proj-1",
    slug: str = "fabric-api",
    title: str = "Fabric API",
) -> CatalogProject:
    return CatalogProject(
        project_id=project_id,
        slug=slug,
        title=title,
        description="A core mod library",
        body="# Fabric API\nDetailed info",
        author="FabricMC",
        icon_url="https://cdn.modrinth.com/icon.png",
        downloads=1000000,
        categories=["library"],
        game_versions=["1.20.4"],
        loaders=["fabric"],
    )


def _version(
    *,
    version_id: str = "ver-1",
    version_number: str = "0.92.0",
    filename: str = "fabric-api-0.92.0.jar",
    sha512: str | None = None,
    file_content: bytes = b"fake-jar-bytes",
) -> tuple[CatalogVersion, bytes]:
    computed = sha512 or hashlib.sha512(file_content).hexdigest()
    version = CatalogVersion(
        version_id=version_id,
        version_number=version_number,
        name="Fabric API 0.92.0",
        game_versions=["1.20.4"],
        loaders=["fabric"],
        files=[
            CatalogFile(
                url=f"https://cdn.modrinth.com/data/{filename}",
                filename=filename,
                size=len(file_content),
                sha512=computed,
                primary=True,
            ),
        ],
        date_published="2024-01-15T12:00:00Z",
    )
    return version, file_content


# -- SearchCatalog --


async def test_search_catalog_auto_facets() -> None:
    """SearchCatalog derives loader + game_versions from the server."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    catalog = FakeCatalogProvider()
    catalog.seed_project(_project())

    uc = SearchCatalog(uow=uow, catalog=catalog)
    result = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        query="fabric",
    )
    assert result.total_hits == 1
    assert result.hits[0].project_id == "proj-1"


async def test_search_catalog_server_not_found() -> None:
    uow = FakeUnitOfWork()
    catalog = FakeCatalogProvider()
    uc = SearchCatalog(uow=uow, catalog=catalog)
    with pytest.raises(ServerNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=ServerId.new(),
            query="test",
        )


async def test_search_catalog_unsupported_server_type() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.VANILLA)
    uow.servers.seed(server)
    catalog = FakeCatalogProvider()
    uc = SearchCatalog(uow=uow, catalog=catalog)
    with pytest.raises(UnsupportedPluginServerTypeError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            query="test",
        )


async def test_search_catalog_unavailable() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    catalog = FakeCatalogProvider(unavailable=True)
    uc = SearchCatalog(uow=uow, catalog=catalog)
    with pytest.raises(CatalogUnavailableError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            query="test",
        )


# -- GetCatalogProject --


async def test_get_catalog_project_returns_project_and_versions() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    version, _ = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])

    uc = GetCatalogProject(uow=uow, catalog=catalog)
    result_project, result_versions = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id_or_slug="proj-1",
    )
    assert result_project.project_id == "proj-1"
    assert len(result_versions) == 1
    assert result_versions[0].version_id == "ver-1"


async def test_get_catalog_project_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    catalog = FakeCatalogProvider()
    uc = GetCatalogProject(uow=uow, catalog=catalog)
    with pytest.raises(CatalogProjectNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id_or_slug="nonexistent",
        )


# -- InstallFromCatalog --


async def test_install_from_catalog_happy_path() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=fs, clock=FakeClock(_NOW)
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    assert plugin.source is PluginSource.MODRINTH
    assert plugin.source_project_id == "proj-1"
    assert plugin.source_version_id == "ver-1"
    assert plugin.version_number == "0.92.0"
    assert plugin.display_name == "Fabric API"
    assert plugin.description == "A core mod library"
    assert plugin.rel_path == "mods/fabric-api-0.92.0.jar"
    assert plugin.loader_type is LoaderType.MOD
    assert plugin.enabled is True
    assert plugin.size_bytes == len(content)
    assert plugin.checksum_sha512 == hashlib.sha512(content).hexdigest()
    # File was written to the store.
    assert "mods/fabric-api-0.92.0.jar" in fs.files
    assert uow.commits == 1


async def test_install_from_catalog_paper_server() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    fs = FakeFileStore()

    project = _project(title="WorldGuard")
    version, content = _version(filename="worldguard-7.0.jar")
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=fs, clock=FakeClock(_NOW)
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    assert plugin.rel_path == "plugins/worldguard-7.0.jar"
    assert plugin.loader_type is LoaderType.PLUGIN


async def test_install_from_catalog_checksum_mismatch() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    version, _ = _version(sha512="0" * 128)  # Wrong checksum
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, b"fake-jar-bytes")

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(CatalogChecksumMismatchError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-1",
        )


async def test_install_from_catalog_version_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project)  # No versions seeded

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(CatalogProjectNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="nonexistent",
        )


async def test_install_from_catalog_not_at_rest() -> None:
    uow = FakeUnitOfWork()
    server = _server(
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
    )
    uow.servers.seed(server)

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(ServerFilesUnsettledError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-1",
        )


async def test_install_from_catalog_duplicate() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=fs, clock=FakeClock(_NOW)
    )
    # First install.
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    # Second install (same filename).
    with pytest.raises(PluginAlreadyExistsError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-1",
        )


async def test_install_from_catalog_unavailable() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    catalog = FakeCatalogProvider(unavailable=True)
    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(CatalogUnavailableError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-1",
        )


async def test_install_from_catalog_empty_sha512_fails() -> None:
    """Mandatory checksum: empty sha512 must raise CatalogChecksumMismatchError."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    file_content = b"fake-jar-bytes"
    version = CatalogVersion(
        version_id="ver-1",
        version_number="0.92.0",
        name="Fabric API 0.92.0",
        game_versions=["1.20.4"],
        loaders=["fabric"],
        files=[
            CatalogFile(
                url="https://cdn.modrinth.com/data/fabric-api-0.92.0.jar",
                filename="fabric-api-0.92.0.jar",
                size=len(file_content),
                sha512="",  # Empty hash
                primary=True,
            ),
        ],
        date_published="2024-01-15T12:00:00Z",
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, file_content)

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(CatalogChecksumMismatchError, match="no sha512"):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-1",
        )


async def test_install_from_catalog_non_jar_filename_fails() -> None:
    """Catalog filenames must end with .jar."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    file_content = b"fake-content"
    computed = hashlib.sha512(file_content).hexdigest()
    version = CatalogVersion(
        version_id="ver-1",
        version_number="0.92.0",
        name="Fabric API 0.92.0",
        game_versions=["1.20.4"],
        loaders=["fabric"],
        files=[
            CatalogFile(
                url="https://cdn.modrinth.com/data/fabric-api-0.92.0.zip",
                filename="fabric-api-0.92.0.zip",
                size=len(file_content),
                sha512=computed,
                primary=True,
            ),
        ],
        date_published="2024-01-15T12:00:00Z",
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, file_content)

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(InvalidFilePathError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-1",
        )
