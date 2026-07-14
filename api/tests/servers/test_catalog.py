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
    side_from_modrinth,
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
    PortRangeExhaustedError,
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
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from tests.servers.fakes import (
    FakeCatalogProvider,
    FakeClock,
    FakeFileStore,
    FakePluginCacheStore,
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


# -- Modrinth side mapping (issue #1308) --


class TestSideFromModrinth:
    """Map Modrinth ``client_side`` / ``server_side`` to a :data:`PluginSide`."""

    def test_server_only_when_client_unsupported(self) -> None:
        assert side_from_modrinth("unsupported", "required") == "server"

    def test_client_only_when_server_unsupported(self) -> None:
        assert side_from_modrinth("required", "unsupported") == "client"

    def test_both_when_required_on_both(self) -> None:
        assert side_from_modrinth("required", "required") == "both"

    def test_both_when_optional_on_both(self) -> None:
        assert side_from_modrinth("optional", "optional") == "both"

    def test_both_when_unknown(self) -> None:
        assert side_from_modrinth("unknown", "unknown") == "both"

    def test_server_when_client_optional_but_server_required(self) -> None:
        # Only an explicit ``unsupported`` on one side narrows the side.
        assert side_from_modrinth("optional", "required") == "both"


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
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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


async def test_install_from_catalog_parses_manifest() -> None:
    # The downloaded jar's manifest is parsed at ingest (issue #1307): the same
    # uniform source as a local upload, populated on the Modrinth path too.
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "fabric.mod.json",
            json.dumps(
                {
                    "id": "fabric-api",
                    "version": "0.92.0",
                    "depends": {"minecraft": "1.20.4"},
                    "provides": ["fabric-api-base"],
                }
            ),
        )
    jar = buf.getvalue()

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    version, _ = _version(file_content=jar)
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, jar)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    assert plugin.mod_identifier == "fabric-api"
    assert plugin.provides == ["fabric-api-base"]
    assert plugin.mc_versions == ["1.20.4"]


async def test_install_from_catalog_captures_required_catalog_deps() -> None:
    # The selected Modrinth version's REQUIRED catalog deps are captured at
    # ingest (issue #1321), keyed by project_id, with the dep project's slug/title
    # for display. Optional deps are not stored.
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    rei_project = _project(project_id="REI", slug="rei", title="Roughly Enough Items")
    rei_version, content = _version()
    rei_version = CatalogVersion(
        version_id=rei_version.version_id,
        version_number=rei_version.version_number,
        name=rei_version.name,
        game_versions=rei_version.game_versions,
        loaders=rei_version.loaders,
        files=rei_version.files,
        date_published=rei_version.date_published,
        dependencies=[
            CatalogDependency(
                version_id=None, project_id="ARCH", dependency_type="required"
            ),
            CatalogDependency(
                version_id=None, project_id="OPT", dependency_type="optional"
            ),
        ],
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(rei_project, [rei_version])
    catalog.seed_file(rei_version.files[0].url, content)
    catalog.seed_project(
        _project(project_id="ARCH", slug="architectury-api", title="Architectury")
    )

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="REI",
        version_id="ver-1",
    )
    assert plugin.catalog_dependencies == [
        {
            "project_id": "ARCH",
            "required": True,
            "slug": "architectury-api",
            "title": "Architectury",
        }
    ]


async def test_install_from_catalog_captures_incompatible_catalog_deps() -> None:
    # The selected version's INCOMPATIBLE catalog edges are captured at ingest
    # (issue #1318), keyed by project_id and marked ``incompatible`` (distinct from
    # the ``required`` flag), with the dep project's slug/title for display.
    # Embedded edges are not stored.
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    mod_project = _project(project_id="MOD", slug="mod", title="Mod")
    mod_version, content = _version()
    mod_version = CatalogVersion(
        version_id=mod_version.version_id,
        version_number=mod_version.version_number,
        name=mod_version.name,
        game_versions=mod_version.game_versions,
        loaders=mod_version.loaders,
        files=mod_version.files,
        date_published=mod_version.date_published,
        dependencies=[
            CatalogDependency(
                version_id=None, project_id="RIVAL", dependency_type="incompatible"
            ),
            CatalogDependency(
                version_id=None, project_id="EMB", dependency_type="embedded"
            ),
        ],
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(mod_project, [mod_version])
    catalog.seed_file(mod_version.files[0].url, content)
    catalog.seed_project(_project(project_id="RIVAL", slug="rival-mod", title="Rival"))

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="MOD",
        version_id="ver-1",
    )
    assert plugin.catalog_dependencies == [
        {
            "project_id": "RIVAL",
            "incompatible": True,
            "slug": "rival-mod",
            "title": "Rival",
        }
    ]


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
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    assert plugin.rel_path == "plugins/worldguard-7.0.jar"
    assert plugin.loader_type is LoaderType.PLUGIN


def _geyser_project() -> CatalogProject:
    """A GeyserMC-sourced Floodgate project (issue #1905)."""
    return CatalogProject(
        project_id="floodgate",
        slug="floodgate",
        title="Floodgate",
        description="Bedrock auth companion",
        body="Floodgate",
        author="GeyserMC",
        icon_url=None,
        downloads=0,
        categories=["bedrock"],
        game_versions=[],
        loaders=["paper"],
        source="geyser",
    )


def _geyser_version(
    *,
    version_id: str = "2.2.5-138",
    build: str = "138",
    content: bytes = b"floodgate-jar",
    sha256: str | None = None,
) -> tuple[CatalogVersion, bytes]:
    computed = sha256 or hashlib.sha256(content).hexdigest()
    url = (
        "https://download.geysermc.org/v2/projects/floodgate"
        f"/versions/2.2.5/builds/{build}/downloads/spigot"
    )
    version = CatalogVersion(
        version_id=version_id,
        version_number="2.2.5",
        name=f"Floodgate 2.2.5 (build {build})",
        game_versions=[],
        loaders=["paper"],
        files=[
            CatalogFile(
                url=url,
                filename="floodgate-spigot.jar",
                size=0,
                sha512="",
                primary=True,
                sha256=computed,
            ),
        ],
        date_published="2026-06-29T18:24:08.000Z",
    )
    return version, content


async def test_install_from_catalog_geyser_source_stores_provenance() -> None:
    # A Floodgate install from GeyserMC records the GEYSER source and verifies
    # the sha256 (Modrinth's sha512 is absent for this artifact) (issue #1905).
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    fs = FakeFileStore()
    cache = FakePluginCacheStore()

    project = _geyser_project()
    version, content = _geyser_version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=cache,
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="floodgate",
        version_id="2.2.5-138",
    )
    assert plugin.source is PluginSource.GEYSER
    assert plugin.source_project_id == "floodgate"
    assert plugin.checksum_sha512 is None
    assert plugin.sha256 == hashlib.sha256(content).hexdigest()
    assert plugin.rel_path == "plugins/floodgate-spigot.jar"
    assert plugin.size_bytes == len(content)
    assert await cache.has(plugin.sha256)


async def test_install_from_catalog_geyser_sha256_mismatch() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)

    project = _geyser_project()
    version, _ = _geyser_version(sha256="0" * 64)  # Wrong checksum
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, b"floodgate-jar")

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(CatalogChecksumMismatchError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="floodgate",
            version_id="2.2.5-138",
        )


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
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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


async def test_install_from_catalog_duplicate_project_different_version() -> None:
    """Installing the same Modrinth project at a different version is rejected.

    Two versions of the same mod crash at MC runtime (issue #1332 item 1).
    The guard checks source_project_id, not just rel_path.
    """
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()

    project = _project()
    ver_a, content_a = _version(
        version_id="ver-1",
        version_number="0.92.0",
        filename="fabric-api-0.92.0.jar",
    )
    ver_b_content = b"different-jar-bytes-v2"
    ver_b, _ = _version(
        version_id="ver-2",
        version_number="0.93.0",
        filename="fabric-api-0.93.0.jar",
        file_content=ver_b_content,
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [ver_a, ver_b])
    catalog.seed_file(ver_a.files[0].url, content_a)
    catalog.seed_file(ver_b.files[0].url, ver_b_content)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    # First install succeeds.
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    # Second install of the SAME project at a DIFFERENT version is rejected.
    with pytest.raises(PluginAlreadyExistsError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-2",
        )


async def test_install_from_catalog_different_project_succeeds() -> None:
    """Installing a different Modrinth project on the same server succeeds."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()

    proj_a = _project(project_id="proj-1", slug="fabric-api", title="Fabric API")
    ver_a, content_a = _version(
        version_id="ver-1",
        version_number="0.92.0",
        filename="fabric-api-0.92.0.jar",
    )
    proj_b = _project(project_id="proj-2", slug="lithium", title="Lithium")
    content_b = b"lithium-jar-bytes"
    ver_b, _ = _version(
        version_id="ver-10",
        version_number="0.15.3",
        filename="lithium-0.15.3.jar",
        file_content=content_b,
    )

    catalog = FakeCatalogProvider()
    catalog.seed_project(proj_a, [ver_a])
    catalog.seed_project(proj_b, [ver_b])
    catalog.seed_file(ver_a.files[0].url, content_a)
    catalog.seed_file(ver_b.files[0].url, content_b)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    # Different project succeeds.
    plugin_b = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-2",
        version_id="ver-10",
    )
    assert plugin_b.source_project_id == "proj-2"


async def test_install_from_catalog_local_upload_unaffected_by_project_guard() -> None:
    """A local upload (no source_project_id) does not block a Modrinth install."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    # Seed a pre-existing local plugin (no source_project_id).
    local_plugin = _plugin(
        server_id=server.id,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        rel_path="mods/some-local-mod.jar",
        filename="some-local-mod.jar",
        display_name="Local Mod",
    )
    uow.plugins.seed(local_plugin)

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    # Modrinth install should succeed despite the existing local plugin.
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    assert plugin.source_project_id == "proj-1"


async def test_install_from_catalog_unavailable() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    catalog = FakeCatalogProvider(unavailable=True)
    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
    checksum_sha512: str | None = "a" * 128,
    sha256: str | None = None,
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
        checksum_sha512=checksum_sha512,
        sha256=sha256,
        size_bytes=100,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _geyser_plugin(
    *,
    server_id: ServerId,
    source_version_id: str = "2.2.5-138",
    content: bytes = b"floodgate-jar",
) -> ServerPlugin:
    """An installed GeyserMC-sourced Floodgate plugin (issues #1905, #1916).

    Paper-only, server-side, ``version-build`` version id, and no SHA-512 (the
    GeyserMC artifact publishes only a SHA-256), mirroring what InstallFromCatalog
    stores for a Floodgate install.
    """

    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path="plugins/floodgate-spigot.jar",
        filename="floodgate-spigot.jar",
        display_name="Floodgate",
        description="Bedrock auth companion",
        loader_type=LoaderType.PLUGIN,
        source=PluginSource.GEYSER,
        source_project_id="floodgate",
        source_version_id=source_version_id,
        version_number="2.2.5",
        checksum_sha512=None,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        side="server",
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


async def test_check_updates_partial_catalog_unavailable() -> None:
    """CatalogUnavailableError on one plugin does not fail the entire batch."""
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
        display_name="Failing Mod",
        rel_path="mods/failing-mod.jar",
        filename="failing-mod.jar",
    )
    uow.plugins.seed(p1)
    uow.plugins.seed(p2)

    proj1 = _project(project_id="proj-1")
    v1_new, _ = _version(version_id="ver-2", version_number="0.93.0")
    catalog = FakeCatalogProvider()
    catalog.seed_project(proj1, [v1_new])

    # Make list_versions raise CatalogUnavailableError for proj-2 only.
    _original = catalog.list_versions

    async def _partial_unavailable(
        project_id_or_slug: str,
        *,
        loader: str | None = None,
        game_versions: list[str] | None = None,
    ) -> list[CatalogVersion]:
        if project_id_or_slug == "proj-2":
            raise CatalogUnavailableError("fake partial unavailable")
        return await _original(
            project_id_or_slug, loader=loader, game_versions=game_versions
        )

    catalog.list_versions = _partial_unavailable  # type: ignore[method-assign]

    uc = CheckUpdates(uow=uow, catalog=catalog)
    results = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert len(results) == 2
    by_name = {r.plugin.display_name: r for r in results}
    # proj-1 should still report the update.
    assert by_name["Fabric API"].latest_version is not None
    assert by_name["Fabric API"].latest_version.version_id == "ver-2"
    # proj-2 should gracefully return no update (not raise).
    assert by_name["Failing Mod"].latest_version is None


async def test_check_updates_includes_geyser() -> None:
    """CheckUpdates covers GeyserMC-sourced plugins, not only Modrinth (#1916)."""
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)

    plugin = _geyser_plugin(server_id=server.id, source_version_id="2.2.5-138")
    uow.plugins.seed(plugin)

    latest, _ = _geyser_version(version_id="2.2.5-139", build="139")
    catalog = FakeCatalogProvider()
    catalog.seed_project(_geyser_project(), [latest])

    uc = CheckUpdates(uow=uow, catalog=catalog)
    results = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert len(results) == 1
    assert results[0].plugin.source is PluginSource.GEYSER
    assert results[0].latest_version is not None
    assert results[0].latest_version.version_id == "2.2.5-139"


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


async def test_check_plugin_update_geyser_available() -> None:
    """A GeyserMC Floodgate with an older build reports an in-place update (#1916).

    The ``version-build`` id ("2.2.5-139" != "2.2.5-138") is compared as a whole,
    so a newer resolved build is detected.
    """
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)

    plugin = _geyser_plugin(server_id=server.id, source_version_id="2.2.5-138")
    uow.plugins.seed(plugin)

    latest, _ = _geyser_version(version_id="2.2.5-139", build="139")
    catalog = FakeCatalogProvider()
    catalog.seed_project(_geyser_project(), [latest])

    uc = CheckPluginUpdate(uow=uow, catalog=catalog)
    result = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id)
    assert result.latest_version is not None
    assert result.latest_version.version_id == "2.2.5-139"


async def test_check_plugin_update_geyser_no_update() -> None:
    """When the installed build is the latest, no update is offered (#1916)."""
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)

    plugin = _geyser_plugin(server_id=server.id, source_version_id="2.2.5-138")
    uow.plugins.seed(plugin)

    latest, _ = _geyser_version(version_id="2.2.5-138", build="138")
    catalog = FakeCatalogProvider()
    catalog.seed_project(_geyser_project(), [latest])

    uc = CheckPluginUpdate(uow=uow, catalog=catalog)
    result = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=plugin.id)
    assert result.latest_version is None


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

    uc = UpdatePlugin(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
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


async def test_update_plugin_reparses_manifest() -> None:
    # The updated jar's manifest is re-parsed so the stored dependency metadata
    # tracks the new version (issue #1307).
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "fabric.mod.json",
            json.dumps(
                {
                    "id": "fabric-api",
                    "version": "0.93.0",
                    "depends": {"minecraft": "1.20.4", "newdep": ">=1.0.0"},
                }
            ),
        )
    new_content = buf.getvalue()

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    plugin = _plugin(server_id=server.id, source_version_id="ver-1")
    uow.plugins.seed(plugin)

    ver_new, _ = _version(
        version_id="ver-2",
        version_number="0.93.0",
        filename="fabric-api-0.92.0.jar",
        file_content=new_content,
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(_project(), [ver_new])
    catalog.seed_file(ver_new.files[0].url, new_content)

    uc = UpdatePlugin(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    result = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        version_id="ver-2",
    )
    assert result.mod_identifier == "fabric-api"
    deps = {d["mod_identifier"]: d for d in result.dependencies}
    assert "newdep" in deps


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

    uc = UpdatePlugin(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
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


async def test_update_disabled_plugin_keeps_disabled_path_no_orphan() -> None:
    """Updating a DISABLED server/both plugin must keep the .disabled invariant:
    the new bytes land at the new .disabled path, the old .disabled file is
    removed, and rel_path stays suffixed (issue #1308 reconcile)."""

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/fabric-api-0.92.0.jar.disabled"] = b"old"

    plugin = _plugin(
        server_id=server.id,
        source_version_id="ver-1",
        rel_path="mods/fabric-api-0.92.0.jar.disabled",
    )
    plugin.enabled = False
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

    uc = UpdatePlugin(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    result = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        version_id="ver-2",
    )

    assert result.enabled is False
    assert result.rel_path == "mods/fabric-api-0.93.0.jar.disabled"
    assert fs.files["mods/fabric-api-0.93.0.jar.disabled"] == new_content
    # The old .disabled file is gone (no orphan) and no clean file was written.
    assert "mods/fabric-api-0.92.0.jar.disabled" not in fs.files
    assert "mods/fabric-api-0.93.0.jar" not in fs.files


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
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
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
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(PluginNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            version_id="ver-2",
        )


async def test_update_plugin_geyser_reresolves_latest() -> None:
    """Updating a GeyserMC Floodgate downloads the newer build in place (#1916).

    The GeyserMC artifact carries a SHA-256 (no SHA-512), so the update verifies
    against it and stores ``checksum_sha512=None``, matching the install path.
    """
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    fs = FakeFileStore()

    plugin = _geyser_plugin(server_id=server.id, source_version_id="2.2.5-138")
    uow.plugins.seed(plugin)

    new_content = b"floodgate-jar-139"
    latest, _ = _geyser_version(
        version_id="2.2.5-139", build="139", content=new_content
    )
    catalog = FakeCatalogProvider()
    catalog.seed_project(_geyser_project(), [latest])
    catalog.seed_file(latest.files[0].url, new_content)

    uc = UpdatePlugin(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    result = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        plugin_id=plugin.id,
        version_id="2.2.5-139",
    )
    assert result.source_version_id == "2.2.5-139"
    assert result.version_number == "2.2.5"
    assert result.sha256 == hashlib.sha256(new_content).hexdigest()
    assert result.checksum_sha512 is None
    assert "plugins/floodgate-spigot.jar" in fs.files
    assert uow.commits == 1


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

    uc = UpdatePlugin(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
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


# -- Content-addressed cache + download cache (issue #1306) --


async def test_install_from_catalog_stores_sha256_and_caches_blob() -> None:
    """A Modrinth install caches the jar under its sha256 and records the address."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)
    cache = FakePluginCacheStore()

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=cache,
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    expected_sha256 = hashlib.sha256(content).hexdigest()
    assert plugin.sha256 == expected_sha256
    assert plugin.checksum_sha512 == hashlib.sha512(content).hexdigest()
    assert await cache.has(expected_sha256)
    # The first install downloaded once.
    assert catalog.downloads == [version.files[0].url]


async def test_install_from_catalog_same_version_skips_redownload() -> None:
    """A second per-server install of the same version serves from the download cache.

    The first install downloads + caches; the second resolves the version's
    published sha512 to the cached sha256 and skips the HTTP fetch entirely.
    """
    uow = FakeUnitOfWork()
    server_a = _server()
    server_b = _server()
    uow.servers.seed(server_a)
    uow.servers.seed(server_b)

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)
    cache = FakePluginCacheStore()

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=cache,
        clock=FakeClock(_NOW),
    )

    plugin_a = await uc(
        community_id=_COMMUNITY,
        server_id=server_a.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    plugin_b = await uc(
        community_id=_COMMUNITY,
        server_id=server_b.id,
        project_id="proj-1",
        version_id="ver-1",
    )

    expected_sha256 = hashlib.sha256(content).hexdigest()
    assert plugin_a.sha256 == expected_sha256
    assert plugin_b.sha256 == expected_sha256
    # Downloaded only once across both per-server installs (download cache hit).
    assert catalog.downloads == [version.files[0].url]


async def test_install_from_catalog_cache_hit_skips_download_when_url_dead() -> None:
    """A cached version installs even if the catalog can no longer serve the file.

    Pre-seed the DB index (sha512 -> sha256) and the cache blob, then drop the
    file from the catalog. A successful install proves the bytes came from the
    cache, not an HTTP download.
    """
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    # NOTE: deliberately do NOT seed_file, so download_file would raise.

    sha256 = hashlib.sha256(content).hexdigest()
    cache = FakePluginCacheStore()
    cache.blobs[sha256] = content
    # Seed the download-cache index: a prior install on another server.
    prior = _plugin(
        server_id=ServerId.new(),
        checksum_sha512=version.files[0].sha512,
        sha256=sha256,
    )
    uow.plugins.seed(prior)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=cache,
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    assert plugin.sha256 == sha256
    # No HTTP download happened (the cache served the bytes).
    assert catalog.downloads == []


async def test_cache_hit_verifies_sha512_rejects_corrupted_blob() -> None:
    """A cache hit re-hashes the blob and rejects it when SHA-512 no longer matches.

    If the cached blob is corrupted or tampered with in object storage, the
    cache-hit path must detect the mismatch instead of silently serving bad data
    (issue #1402).
    """
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    # Do NOT seed the file URL: if the cache hit falls through, the test fails
    # for the wrong reason (download error instead of checksum mismatch).

    sha256 = hashlib.sha256(content).hexdigest()
    cache = FakePluginCacheStore()
    # Seed the cache with CORRUPTED bytes under the correct sha256 key.
    cache.blobs[sha256] = b"corrupted-blob-bytes"
    # Seed the download-cache index so the cache-hit path is taken.
    prior = _plugin(
        server_id=ServerId.new(),
        checksum_sha512=version.files[0].sha512,
        sha256=sha256,
    )
    uow.plugins.seed(prior)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=cache,
        clock=FakeClock(_NOW),
    )
    with pytest.raises(CatalogChecksumMismatchError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-1",
        )


async def test_cache_hit_does_not_reupload_blob() -> None:
    """A cache hit skips the upload; GC protection is in the GC re-check (#1404).

    The real object-store adapter's ``put`` skips the upload when the blob
    already exists (dedup-on-ingest), so re-putting would be a no-op anyway.
    The GC race is handled by re-checking live() before each delete in
    ``RunPluginCacheGc`` instead.
    """
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])

    sha256 = hashlib.sha256(content).hexdigest()
    cache = FakePluginCacheStore()
    cache.blobs[sha256] = content
    prior = _plugin(
        server_id=ServerId.new(),
        checksum_sha512=version.files[0].sha512,
        sha256=sha256,
    )
    uow.plugins.seed(prior)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=cache,
        clock=FakeClock(_NOW),
    )
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    assert plugin.sha256 == sha256
    # No put call on the cache-hit path (the blob already exists).
    assert cache.puts == []


# -- Bedrock port on Geyser detection via catalog install (issue #1541) --


def _geyser_manifest_jar() -> bytes:
    """A minimal Paper jar whose plugin.yml declares the Geyser-Spigot name."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.yml", "name: Geyser-Spigot\nversion: 2.4.2\n")
    return buf.getvalue()


def _geyser_install_uc(
    uow: FakeUnitOfWork,
    *,
    project_id: str,
    slug: str,
    file_content: bytes,
    bedrock_port_range: PortRange | None,
    file_store: FakeFileStore | None = None,
) -> InstallFromCatalog:
    project = _project(project_id=project_id, slug=slug, title="Geyser")
    version, content = _version(filename="Geyser-Spigot.jar", file_content=file_content)
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)
    return InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=file_store or FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
        bedrock_port_range=bedrock_port_range,
    )


async def test_install_from_catalog_geyser_by_project_id_allocates_port() -> None:
    # Secondary signal: the jar carries no readable manifest, but the Modrinth
    # project identifies Geyser.
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    uc = _geyser_install_uc(
        uow,
        project_id="wKkoqHrH",
        slug="geyser",
        file_content=b"not-a-zip",
        bedrock_port_range=PortRange(start=19132, end=19141),
    )
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="wKkoqHrH",
        version_id="ver-1",
    )
    assert uow.servers.by_id[server.id].bedrock_port == 19132


async def test_install_from_catalog_geyser_by_manifest_name_allocates_port() -> None:
    # Primary signal: the manifest name identifies Geyser even under an
    # unrecognized catalog project id (e.g. a re-hosted build).
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    uc = _geyser_install_uc(
        uow,
        project_id="other-project",
        slug="other-geyser",
        file_content=_geyser_manifest_jar(),
        bedrock_port_range=PortRange(start=19132, end=19141),
    )
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="other-project",
        version_id="ver-1",
    )
    assert uow.servers.by_id[server.id].bedrock_port == 19132


async def test_install_from_catalog_geyser_without_gate_leaves_port_unset() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    uc = _geyser_install_uc(
        uow,
        project_id="wKkoqHrH",
        slug="geyser",
        file_content=b"not-a-zip",
        bedrock_port_range=None,
    )
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="wKkoqHrH",
        version_id="ver-1",
    )
    assert uow.servers.by_id[server.id].bedrock_port is None


async def test_install_from_catalog_non_geyser_does_not_allocate() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    project = _project(project_id="proj-1", slug="worldguard", title="WorldGuard")
    version, content = _version(filename="worldguard.jar")
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)
    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=FakeFileStore(),
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
        bedrock_port_range=PortRange(start=19132, end=19141),
    )
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        project_id="proj-1",
        version_id="ver-1",
    )
    assert uow.servers.by_id[server.id].bedrock_port is None


async def test_install_from_catalog_geyser_exhausted_window_aborts_install() -> None:
    uow = FakeUnitOfWork()
    other = _server(server_type=ServerType.PAPER)
    other.bedrock_port = 19132
    uow.servers.seed(other)
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    fs = FakeFileStore()
    uc = _geyser_install_uc(
        uow,
        project_id="wKkoqHrH",
        slug="geyser",
        file_content=b"not-a-zip",
        bedrock_port_range=PortRange(start=19132, end=19132),
        file_store=fs,
    )
    with pytest.raises(PortRangeExhaustedError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="wKkoqHrH",
            version_id="ver-1",
        )
    assert uow.commits == 0
    assert uow.servers.by_id[server.id].bedrock_port is None
    # The file store is outside the SQL transaction: the failed install must not
    # leave an orphaned working-set jar behind (allocation runs before the write).
    assert fs.files == {}


# -- Commit-before-file-ops ordering (issue #1826) --


class _FailingCommitUoW(FakeUnitOfWork):
    """A UoW whose commit always raises, simulating a DB commit failure."""

    async def commit(self) -> None:
        raise RuntimeError("simulated commit failure")


async def test_install_from_catalog_failed_commit_leaves_no_orphan_jar() -> None:
    """A failed DB commit must not leave a working-set jar behind (#1826)."""
    uow = _FailingCommitUoW()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()

    project = _project()
    version, content = _version()
    catalog = FakeCatalogProvider()
    catalog.seed_project(project, [version])
    catalog.seed_file(version.files[0].url, content)

    uc = InstallFromCatalog(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            project_id="proj-1",
            version_id="ver-1",
        )
    # The jar must NOT have been written to the working set.
    assert fs.files == {}


async def test_update_plugin_failed_commit_leaves_files_unchanged() -> None:
    """A failed DB commit must not write/delete working-set files (#1826)."""
    uow = _FailingCommitUoW()
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

    uc = UpdatePlugin(
        uow=uow,
        catalog=catalog,
        file_store=fs,
        cache=FakePluginCacheStore(),
        clock=FakeClock(_NOW),
    )
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin.id,
            version_id="ver-2",
        )
    # The old jar must still exist and the new jar must NOT have been written.
    assert "mods/fabric-api-0.92.0.jar" in fs.files
    assert "mods/fabric-api-0.93.0.jar" not in fs.files
