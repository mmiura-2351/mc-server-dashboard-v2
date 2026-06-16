"""Use-case tests for Modrinth catalog integration (issue #1151)."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import pytest

from mc_server_dashboard_api.servers.application.catalog import (
    CheckPluginUpdate,
    CheckUpdates,
    GetCatalogProject,
    InstallFromCatalog,
    ListPluginDependencies,
    SearchCatalog,
    UpdatePlugin,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogDependency,
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
    PluginNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
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


async def test_install_from_catalog_filters_versions_by_loader_and_game_version() -> (
    None
):
    """InstallFromCatalog passes loader and game_versions to list_versions."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    # Capture the list_versions call args via a recording subclass.
    list_versions_calls: list[tuple[str, str | None, list[str] | None]] = []
    _original = catalog.list_versions

    async def _recording_list_versions(
        project_id_or_slug: str,
        *,
        loader: str | None = None,
        game_versions: list[str] | None = None,
    ) -> list[CatalogVersion]:
        list_versions_calls.append((project_id_or_slug, loader, game_versions))
        return await _original(
            project_id_or_slug, loader=loader, game_versions=game_versions
        )

    catalog.list_versions = _recording_list_versions  # type: ignore[method-assign]

    uc = InstallFromCatalog(
        uow=uow, catalog=catalog, file_store=fs, clock=FakeClock(_NOW)
    )
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    # Should have been called with loader and game_versions.
    assert len(list_versions_calls) == 1
    _proj_id, _loader, _gv = list_versions_calls[0]
    assert _loader == "fabric"
    assert _gv == ["1.20.4"]


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


# -- helpers for update/dependency tests --


def _plugin(
    *,
    server_id: ServerId,
    source: PluginSource = PluginSource.MODRINTH,
    source_project_id: str | None = "proj-1",
    source_version_id: str | None = "ver-1",
    version_number: str | None = "0.92.0",
    rel_path: str = "mods/fabric-api-0.92.0.jar",
    filename: str = "fabric-api-0.92.0.jar",
    display_name: str = "Fabric API",
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=rel_path,
        filename=filename,
        display_name=display_name,
        description="A core mod library",
        loader_type=LoaderType.MOD,
        source=source,
        source_project_id=source_project_id,
        source_version_id=source_version_id,
        version_number=version_number,
        checksum_sha512="a" * 128,
        size_bytes=100,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


# -- CheckUpdates --


async def test_check_updates_update_available() -> None:
    """CheckUpdates returns latest_version when a newer version exists."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(server_id=server.id, source_version_id="ver-1")
    uow.plugins.seed(plugin)

    project = _project()
    ver_old, _ = _version(version_id="ver-1")
    ver_new, _ = _version(version_id="ver-2", version_number="0.93.0")
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_new, ver_old])

    uc = CheckUpdates(uow=uow, catalog=catalog)
    results = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert len(results) == 1
    assert results[0].latest_version is not None
    assert results[0].latest_version.version_id == "ver-2"


async def test_check_updates_no_update() -> None:
    """CheckUpdates returns None when the installed version is the latest."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(server_id=server.id, source_version_id="ver-1")
    uow.plugins.seed(plugin)

    project = _project()
    ver, _ = _version(version_id="ver-1")
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver])

    uc = CheckUpdates(uow=uow, catalog=catalog)
    results = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert len(results) == 1
    assert results[0].latest_version is None


async def test_check_updates_mixed_batch() -> None:
    """CheckUpdates handles mixed: one updatable, one up-to-date."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    p1 = _plugin(
        server_id=server.id,
        source_project_id="proj-1",
        source_version_id="ver-1",
        display_name="Fabric API",
    )
    p2 = _plugin(
        server_id=server.id,
        source_project_id="proj-2",
        source_version_id="ver-3",
        display_name="Other Mod",
        rel_path="mods/other-mod.jar",
        filename="other-mod.jar",
    )
    uow.plugins.seed(p1)
    uow.plugins.seed(p2)

    proj1 = _project(project_id="proj-1")
    proj2 = _project(project_id="proj-2", slug="other-mod", title="Other Mod")
    v1_old, _ = _version(version_id="ver-1")
    v1_new, _ = _version(version_id="ver-2", version_number="0.93.0")
    v2, _ = _version(version_id="ver-3")
    catalog = FakeCatalogProvider()
    catalog.seed_project(proj1, [v1_new, v1_old])
    catalog.seed_project(proj2, [v2])

    uc = CheckUpdates(uow=uow, catalog=catalog)
    results = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert len(results) == 2
    by_name = {r.plugin.display_name: r for r in results}
    assert by_name["Fabric API"].latest_version is not None
    assert by_name["Other Mod"].latest_version is None


async def test_check_updates_server_not_found() -> None:
    uow = FakeUnitOfWork()
    catalog = FakeCatalogProvider()
    uc = CheckUpdates(uow=uow, catalog=catalog)
    with pytest.raises(ServerNotFoundError):
        await uc(community_id=_COMMUNITY, server_id=ServerId.new())


# -- CheckPluginUpdate --


async def test_check_plugin_update_happy_path() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(server_id=server.id, source_version_id="ver-1")
    uow.plugins.seed(plugin)

    project = _project()
    ver_new, _ = _version(version_id="ver-2", version_number="0.93.0")
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_new])

    uc = CheckPluginUpdate(uow=uow, catalog=catalog)
    result = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id)
    assert result.latest_version is not None
    assert result.latest_version.version_id == "ver-2"


async def test_check_plugin_update_local_returns_none() -> None:
    """LOCAL plugins have no catalog source; latest_version is None."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(
        server_id=server.id,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
    )
    uow.plugins.seed(plugin)

    catalog = FakeCatalogProvider()
    uc = CheckPluginUpdate(uow=uow, catalog=catalog)
    result = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id)
    assert result.latest_version is None


async def test_check_plugin_update_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    catalog = FakeCatalogProvider()
    uc = CheckPluginUpdate(uow=uow, catalog=catalog)
    with pytest.raises(PluginNotFoundError):
        await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=PluginId.new())


# -- UpdatePlugin --


async def test_update_plugin_same_filename() -> None:
    """Update with same filename overwrites the file in place."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()

    plugin = _plugin(server_id=server.id, source_version_id="ver-1")
    uow.plugins.seed(plugin)

    project = _project()
    new_content = b"new-jar-bytes"
    ver_new, _ = _version(
        version_id="ver-2",
        version_number="0.93.0",
        filename="fabric-api-0.92.0.jar",  # same filename
        file_content=new_content,
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_new])
    catalog.seed_file(ver_new.files[0].url, new_content)

    uc = UpdatePlugin(uow=uow, catalog=catalog, file_store=fs, clock=FakeClock(_NOW))
    result = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        version_id="ver-2",
    )
    assert result.source_version_id == "ver-2"
    assert result.version_number == "0.93.0"
    assert result.checksum_sha512 == hashlib.sha512(new_content).hexdigest()
    assert "mods/fabric-api-0.92.0.jar" in fs.files
    assert uow.commits == 1


async def test_update_plugin_different_filename() -> None:
    """Update with different filename writes new file and deletes old."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/fabric-api-0.92.0.jar"] = b"old"

    plugin = _plugin(server_id=server.id, source_version_id="ver-1")
    uow.plugins.seed(plugin)

    project = _project()
    new_content = b"new-jar-bytes-v2"
    ver_new, _ = _version(
        version_id="ver-2",
        version_number="0.93.0",
        filename="fabric-api-0.93.0.jar",  # different filename
        file_content=new_content,
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_new])
    catalog.seed_file(ver_new.files[0].url, new_content)

    uc = UpdatePlugin(uow=uow, catalog=catalog, file_store=fs, clock=FakeClock(_NOW))
    result = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        version_id="ver-2",
    )
    assert result.rel_path == "mods/fabric-api-0.93.0.jar"
    assert result.filename == "fabric-api-0.93.0.jar"
    assert "mods/fabric-api-0.93.0.jar" in fs.files
    assert "mods/fabric-api-0.92.0.jar" not in fs.files


async def test_update_plugin_not_at_rest() -> None:
    uow = FakeUnitOfWork()
    server = _server(
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
    )
    uow.servers.seed(server)

    plugin = _plugin(server_id=server.id)
    uow.plugins.seed(plugin)

    project = _project()
    new_content = b"new-jar"
    ver_new, _ = _version(
        version_id="ver-2", version_number="0.93.0", file_content=new_content
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_new])
    catalog.seed_file(ver_new.files[0].url, new_content)

    uc = UpdatePlugin(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(ServerFilesUnsettledError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            version_id="ver-2",
        )


async def test_update_plugin_checksum_mismatch() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(server_id=server.id)
    uow.plugins.seed(plugin)

    project = _project()
    ver_new, _ = _version(version_id="ver-2", version_number="0.93.0", sha512="0" * 128)
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_new])
    catalog.seed_file(ver_new.files[0].url, b"fake-jar-bytes")

    uc = UpdatePlugin(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(CatalogChecksumMismatchError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            version_id="ver-2",
        )


async def test_update_plugin_local_raises() -> None:
    """LOCAL plugins cannot be updated from catalog."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(
        server_id=server.id,
        source=PluginSource.LOCAL,
        source_project_id=None,
    )
    uow.plugins.seed(plugin)

    catalog = FakeCatalogProvider()
    uc = UpdatePlugin(
        uow=uow, catalog=catalog, file_store=FakeFileStore(), clock=FakeClock(_NOW)
    )
    with pytest.raises(PluginNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            version_id="ver-2",
        )


async def test_update_plugin_rel_path_collision() -> None:
    """Update rejects a new filename that collides with another plugin."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()

    plugin_a = _plugin(
        server_id=server.id,
        source_project_id="proj-1",
        source_version_id="ver-1",
        rel_path="mods/fabric-api-0.92.0.jar",
        filename="fabric-api-0.92.0.jar",
        display_name="Fabric API",
    )
    plugin_b = _plugin(
        server_id=server.id,
        source_project_id="proj-2",
        source_version_id="ver-10",
        rel_path="mods/other-mod-1.0.jar",
        filename="other-mod-1.0.jar",
        display_name="Other Mod",
    )
    uow.plugins.seed(plugin_a)
    uow.plugins.seed(plugin_b)

    project = _project(project_id="proj-2", slug="other-mod", title="Other Mod")
    # New version of other-mod has the same filename as plugin_a's jar.
    new_content = b"colliding-jar"
    ver_new, _ = _version(
        version_id="ver-11",
        version_number="2.0.0",
        filename="fabric-api-0.92.0.jar",  # collides with plugin_a
        file_content=new_content,
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_new])
    catalog.seed_file(ver_new.files[0].url, new_content)

    uc = UpdatePlugin(uow=uow, catalog=catalog, file_store=fs, clock=FakeClock(_NOW))
    with pytest.raises(PluginAlreadyExistsError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin_b.id,
            version_id="ver-11",
        )


# -- ListPluginDependencies --


async def test_list_plugin_dependencies_happy_path() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(server_id=server.id, source_version_id="ver-1")
    uow.plugins.seed(plugin)

    dep_project = _project(project_id="dep-1", slug="dep-mod", title="Dep Mod")
    project = _project()
    ver, _ = _version(version_id="ver-1")
    # Manually create a version with dependencies
    ver_with_deps = CatalogVersion(
        version_id="ver-1",
        version_number="0.92.0",
        name="Fabric API 0.92.0",
        game_versions=["1.20.4"],
        loaders=["fabric"],
        files=ver.files,
        date_published="2024-01-15T12:00:00Z",
        dependencies=[
            CatalogDependency(
                version_id="dep-ver-1",
                project_id="dep-1",
                dependency_type="required",
            ),
            CatalogDependency(
                version_id=None,
                project_id="opt-1",
                dependency_type="optional",
            ),
        ],
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_with_deps])
    catalog.seed_project(dep_project)

    uc = ListPluginDependencies(uow=uow, catalog=catalog)
    deps = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id)
    assert len(deps) == 2
    required = next(d for d in deps if d.dependency_type == "required")
    assert required.project_id == "dep-1"
    assert required.project_title == "Dep Mod"
    assert required.project_slug == "dep-mod"
    assert required.installed is False

    optional = next(d for d in deps if d.dependency_type == "optional")
    assert optional.project_id == "opt-1"
    assert optional.version_id is None
    assert optional.project_title is None  # not seeded, so lookup fails gracefully


async def test_list_plugin_dependencies_local_returns_empty() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(
        server_id=server.id,
        source=PluginSource.LOCAL,
        source_project_id=None,
    )
    uow.plugins.seed(plugin)

    catalog = FakeCatalogProvider()
    uc = ListPluginDependencies(uow=uow, catalog=catalog)
    deps = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id)
    assert deps == []


async def test_list_plugin_dependencies_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    catalog = FakeCatalogProvider()
    uc = ListPluginDependencies(uow=uow, catalog=catalog)
    with pytest.raises(PluginNotFoundError):
        await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=PluginId.new())


async def test_list_plugin_dependencies_installed_flag() -> None:
    """The installed flag is True when the dependency project is installed."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(server_id=server.id, source_version_id="ver-1")
    uow.plugins.seed(plugin)

    dep_plugin = _plugin(
        server_id=server.id,
        source_project_id="dep-1",
        source_version_id="dep-ver-1",
        display_name="Dep Mod",
        rel_path="mods/dep-mod.jar",
        filename="dep-mod.jar",
    )
    uow.plugins.seed(dep_plugin)

    dep_project = _project(project_id="dep-1", slug="dep-mod", title="Dep Mod")
    project = _project()
    ver, _ = _version(version_id="ver-1")
    ver_with_deps = CatalogVersion(
        version_id="ver-1",
        version_number="0.92.0",
        name="Fabric API 0.92.0",
        game_versions=["1.20.4"],
        loaders=["fabric"],
        files=ver.files,
        date_published="2024-01-15T12:00:00Z",
        dependencies=[
            CatalogDependency(
                version_id="dep-ver-1",
                project_id="dep-1",
                dependency_type="required",
            ),
        ],
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_with_deps])
    catalog.seed_project(dep_project)

    uc = ListPluginDependencies(uow=uow, catalog=catalog)
    deps = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id)
    assert len(deps) == 1
    assert deps[0].installed is True
