"""Domain tests for the plugin entity, value objects, and mapping functions."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.domain.errors import (
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
    content_dir_for_server_type,
    loader_type_for_server_type,
    working_set_present,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId, ServerType


class TestPluginId:
    def test_new_generates_uuid(self) -> None:
        pid = PluginId.new()
        assert isinstance(pid.value, uuid.UUID)

    def test_two_new_ids_differ(self) -> None:
        assert PluginId.new() != PluginId.new()


class TestContentDirForServerType:
    def test_fabric_returns_mods(self) -> None:
        assert content_dir_for_server_type(ServerType.FABRIC) == "mods"

    def test_forge_returns_mods(self) -> None:
        assert content_dir_for_server_type(ServerType.FORGE) == "mods"

    def test_paper_returns_plugins(self) -> None:
        assert content_dir_for_server_type(ServerType.PAPER) == "plugins"

    def test_vanilla_raises(self) -> None:
        with pytest.raises(UnsupportedPluginServerTypeError):
            content_dir_for_server_type(ServerType.VANILLA)

    def test_spigot_raises(self) -> None:
        with pytest.raises(UnsupportedPluginServerTypeError):
            content_dir_for_server_type(ServerType.SPIGOT)


class TestLoaderTypeForServerType:
    def test_fabric_returns_mod(self) -> None:
        assert loader_type_for_server_type(ServerType.FABRIC) is LoaderType.MOD

    def test_forge_returns_mod(self) -> None:
        assert loader_type_for_server_type(ServerType.FORGE) is LoaderType.MOD

    def test_paper_returns_plugin(self) -> None:
        assert loader_type_for_server_type(ServerType.PAPER) is LoaderType.PLUGIN

    def test_vanilla_raises(self) -> None:
        with pytest.raises(UnsupportedPluginServerTypeError):
            loader_type_for_server_type(ServerType.VANILLA)


class TestServerPluginEntity:
    def test_construction(self) -> None:
        now = dt.datetime.now(tz=dt.timezone.utc)
        plugin = ServerPlugin(
            id=PluginId.new(),
            server_id=ServerId.new(),
            rel_path="mods/test.jar",
            filename="test.jar",
            display_name="Test Plugin",
            description=None,
            loader_type=LoaderType.MOD,
            source=PluginSource.LOCAL,
            source_project_id=None,
            source_version_id=None,
            version_number=None,
            checksum_sha512="abc123",
            sha256="def456",
            size_bytes=1024,
            enabled=True,
            installed_by=None,
            created_at=now,
            updated_at=now,
        )
        assert plugin.display_name == "Test Plugin"
        assert plugin.sha256 == "def456"
        assert plugin.enabled is True
        assert plugin.loader_type is LoaderType.MOD
        assert plugin.source is PluginSource.LOCAL

    def test_side_defaults_to_both(self) -> None:
        now = dt.datetime.now(tz=dt.timezone.utc)
        plugin = ServerPlugin(
            id=PluginId.new(),
            server_id=ServerId.new(),
            rel_path="mods/test.jar",
            filename="test.jar",
            display_name="Test Plugin",
            description=None,
            loader_type=LoaderType.MOD,
            source=PluginSource.LOCAL,
            source_project_id=None,
            source_version_id=None,
            version_number=None,
            checksum_sha512="abc123",
            sha256="def456",
            size_bytes=1024,
            enabled=True,
            installed_by=None,
            created_at=now,
            updated_at=now,
        )
        assert plugin.side == "both"


class TestWorkingSetPresent:
    """A jar belongs in the running working set iff enabled and not client-only."""

    def test_enabled_both_is_present(self) -> None:
        assert working_set_present(enabled=True, side="both") is True

    def test_enabled_server_is_present(self) -> None:
        assert working_set_present(enabled=True, side="server") is True

    def test_enabled_client_is_absent(self) -> None:
        assert working_set_present(enabled=True, side="client") is False

    def test_disabled_both_is_absent(self) -> None:
        assert working_set_present(enabled=False, side="both") is False

    def test_disabled_client_is_absent(self) -> None:
        assert working_set_present(enabled=False, side="client") is False


class TestEnumValues:
    def test_loader_type_values(self) -> None:
        assert LoaderType.MOD.value == "mod"
        assert LoaderType.PLUGIN.value == "plugin"

    def test_plugin_source_values(self) -> None:
        assert PluginSource.LOCAL.value == "local"
        assert PluginSource.MODRINTH.value == "modrinth"
