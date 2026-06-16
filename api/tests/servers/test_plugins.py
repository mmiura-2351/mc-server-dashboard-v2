"""Use-case tests for plugin management (issue #1150)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.plugins import (
    GetPlugin,
    InstallPlugin,
    ListPlugins,
    RemovePlugin,
    TogglePlugin,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidFilePathError,
    PluginAlreadyExistsError,
    PluginNotFoundError,
    ServerFileNotFoundError,
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
    desired_state: DesiredState = DesiredState.STOPPED,
    observed_state: ObservedState = ObservedState.STOPPED,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=community_id,
        name=ServerName("test-server"),
        mc_edition="java",
        mc_version="1.20.4",
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


def _plugin(
    *,
    server_id: ServerId,
    enabled: bool = True,
    rel_path: str | None = None,
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=server_id,
        rel_path=rel_path or "mods/test.jar",
        filename="test.jar",
        display_name="Test Plugin",
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.LOCAL,
        source_project_id=None,
        source_version_id=None,
        version_number=None,
        checksum_sha512="abc",
        size_bytes=100,
        enabled=enabled,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


# -- ListPlugins --


async def test_list_plugins_empty() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = ListPlugins(uow=uow)
    plugins = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert plugins == []


async def test_list_plugins_returns_seeded() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    uc = ListPlugins(uow=uow)
    plugins = await uc(community_id=_COMMUNITY, server_id=server.id)
    assert len(plugins) == 1
    assert plugins[0].id == p.id


async def test_list_plugins_unknown_server() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = ListPlugins(uow=uow)
    with pytest.raises(ServerNotFoundError):
        await uc(community_id=_COMMUNITY, server_id=ServerId.new())


async def test_list_plugins_unsupported_server_type() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.VANILLA)
    uow.servers.seed(server)
    uc = ListPlugins(uow=uow)
    with pytest.raises(UnsupportedPluginServerTypeError):
        await uc(community_id=_COMMUNITY, server_id=server.id)


# -- GetPlugin --


async def test_get_plugin_happy_path() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    uc = GetPlugin(uow=uow)
    result = await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    assert result.id == p.id


async def test_get_plugin_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = GetPlugin(uow=uow)
    with pytest.raises(PluginNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=PluginId.new(),
        )


async def test_get_plugin_server_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = GetPlugin(uow=uow)
    with pytest.raises(ServerNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=ServerId.new(),
            plugin_id=PluginId.new(),
        )


# -- InstallPlugin --


async def test_install_fabric_mod() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    uc = InstallPlugin(uow=uow, file_store=fs, clock=FakeClock(_NOW))
    content = b"fake-jar-bytes"
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="fabric-api.jar",
        display_name="Fabric API",
        content=content,
    )
    assert plugin.filename == "fabric-api.jar"
    assert plugin.rel_path == "mods/fabric-api.jar"
    assert plugin.loader_type is LoaderType.MOD
    assert plugin.source is PluginSource.LOCAL
    assert plugin.enabled is True
    assert plugin.size_bytes == len(content)
    assert plugin.checksum_sha512 is not None
    assert "mods/fabric-api.jar" in fs.files


async def test_install_paper_plugin() -> None:
    uow = FakeUnitOfWork()
    server = _server(server_type=ServerType.PAPER)
    uow.servers.seed(server)
    uc = InstallPlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="worldguard.jar",
        display_name="WorldGuard",
        content=b"jar",
    )
    assert plugin.rel_path == "plugins/worldguard.jar"
    assert plugin.loader_type is LoaderType.PLUGIN


async def test_install_rejects_non_jar() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = InstallPlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    with pytest.raises(InvalidFilePathError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="bad.txt",
            display_name="Bad",
            content=b"data",
        )


async def test_install_accepts_uppercase_jar_extension() -> None:
    """Case-insensitive .jar check: .JAR and .Jar should be accepted."""
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = InstallPlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    plugin = await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="MyPlugin.JAR",
        display_name="My Plugin",
        content=b"jar-bytes",
    )
    assert plugin.filename == "MyPlugin.JAR"


async def test_install_requires_at_rest() -> None:
    uow = FakeUnitOfWork()
    server = _server(
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
    )
    uow.servers.seed(server)
    uc = InstallPlugin(uow=uow, file_store=FakeFileStore(), clock=FakeClock(_NOW))
    with pytest.raises(ServerFilesUnsettledError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="test.jar",
            display_name="Test",
            content=b"jar",
        )


# -- RemovePlugin --


async def test_remove_plugin_deletes_file_and_record() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    uc = RemovePlugin(uow=uow, file_store=fs)
    await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    assert p.id not in uow.plugins.by_id
    assert "mods/test.jar" not in fs.files


async def test_remove_plugin_not_found() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    uc = RemovePlugin(uow=uow, file_store=FakeFileStore())
    with pytest.raises(PluginNotFoundError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=PluginId.new(),
        )


# -- TogglePlugin --


async def test_disable_plugin() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id, enabled=True, rel_path="mods/test.jar")
    uow.plugins.seed(p)
    uc = TogglePlugin(uow=uow, file_store=fs, clock=FakeClock(_NOW))
    result = await uc(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, enable=False
    )
    assert result.enabled is False
    assert result.rel_path == "mods/test.jar.disabled"
    assert "mods/test.jar" not in fs.files
    assert "mods/test.jar.disabled" in fs.files


async def test_enable_plugin() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar.disabled"] = b"jar"
    p = _plugin(server_id=server.id, enabled=False, rel_path="mods/test.jar.disabled")
    uow.plugins.seed(p)
    uc = TogglePlugin(uow=uow, file_store=fs, clock=FakeClock(_NOW))
    result = await uc(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, enable=True
    )
    assert result.enabled is True
    assert result.rel_path == "mods/test.jar"


async def test_toggle_noop_if_already_desired_state() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    fs.files["mods/test.jar"] = b"jar"
    p = _plugin(server_id=server.id, enabled=True, rel_path="mods/test.jar")
    uow.plugins.seed(p)
    uc = TogglePlugin(uow=uow, file_store=fs, clock=FakeClock(_NOW))
    result = await uc(
        community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id, enable=True
    )
    assert result.enabled is True
    assert uow.commits == 0


# -- Duplicate install (Bug 1) --


async def test_install_duplicate_raises_already_exists() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    uc = InstallPlugin(uow=uow, file_store=fs, clock=FakeClock(_NOW))
    await uc(
        community_id=_COMMUNITY,
        server_id=server.id,
        filename="fabric-api.jar",
        display_name="Fabric API",
        content=b"jar-bytes",
    )
    with pytest.raises(PluginAlreadyExistsError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            filename="fabric-api.jar",
            display_name="Fabric API v2",
            content=b"other-jar-bytes",
        )


# -- Toggle collision (Bug 2) --


async def test_enable_collision_raises_already_exists() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    fs = FakeFileStore()
    # Plugin A occupies mods/foo.jar.
    plugin_a = _plugin(server_id=server.id, enabled=True, rel_path="mods/foo.jar")
    uow.plugins.seed(plugin_a)
    fs.files["mods/foo.jar"] = b"jar-a"
    # Plugin B is disabled at mods/foo.jar.disabled.
    plugin_b = _plugin(
        server_id=server.id, enabled=False, rel_path="mods/foo.jar.disabled"
    )
    uow.plugins.seed(plugin_b)
    fs.files["mods/foo.jar.disabled"] = b"jar-b"
    uc = TogglePlugin(uow=uow, file_store=fs, clock=FakeClock(_NOW))
    with pytest.raises(PluginAlreadyExistsError):
        await uc(
            community_id=_COMMUNITY,
            server_id=server.id,
            plugin_id=plugin_b.id,
            enable=True,
        )


# -- Remove with missing file (Improvement 3) --


async def test_remove_plugin_succeeds_when_jar_already_gone() -> None:
    """Removing a plugin whose jar was already gone still cleans up the DB record."""

    class _RaisingFileStore(FakeFileStore):
        async def delete_file(
            self, *, community_id: object, server_id: object, rel_path: str
        ) -> None:
            raise ServerFileNotFoundError(str(rel_path))

    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    p = _plugin(server_id=server.id)
    uow.plugins.seed(p)
    uc = RemovePlugin(uow=uow, file_store=_RaisingFileStore())
    await uc(community_id=_COMMUNITY, server_id=server.id, plugin_id=p.id)
    assert p.id not in uow.plugins.by_id
