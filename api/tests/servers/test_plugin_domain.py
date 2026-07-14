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
    has_enabled_geyser,
    is_geyser_plugin,
    loader_type_for_server_type,
    sanitize_plugin_filename,
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


class TestSanitizePluginFilename:
    """Filenames are reduced to a safe basename to prevent zip-slip (#1400)."""

    def test_normal_filename_unchanged(self) -> None:
        assert sanitize_plugin_filename("my-plugin-1.0.jar") == "my-plugin-1.0.jar"

    def test_backslash_path_traversal(self) -> None:
        assert sanitize_plugin_filename("a\\..\\..\\evil.jar") == "evil.jar"

    def test_forward_slash_subdir(self) -> None:
        assert sanitize_plugin_filename("subdir/evil.jar") == "evil.jar"

    def test_mixed_separators(self) -> None:
        assert sanitize_plugin_filename("a/b\\c.jar") == "c.jar"

    def test_empty_after_strip_raises(self) -> None:
        with pytest.raises(ValueError):
            sanitize_plugin_filename("")

    def test_dot_raises(self) -> None:
        with pytest.raises(ValueError):
            sanitize_plugin_filename(".")

    def test_dotdot_raises(self) -> None:
        with pytest.raises(ValueError):
            sanitize_plugin_filename("..")


class TestIsGeyserPlugin:
    """Geyser detection drives the bedrock_port lifecycle (issue #1541)."""

    @staticmethod
    def _plugin(
        *,
        mod_identifier: str | None = None,
        source_project_id: str | None = None,
        loader_type: LoaderType = LoaderType.PLUGIN,
    ) -> ServerPlugin:
        now = dt.datetime.now(tz=dt.timezone.utc)
        return ServerPlugin(
            id=PluginId.new(),
            server_id=ServerId.new(),
            rel_path="plugins/test.jar",
            filename="test.jar",
            display_name="Test Plugin",
            description=None,
            loader_type=loader_type,
            source=PluginSource.LOCAL,
            source_project_id=source_project_id,
            source_version_id=None,
            version_number=None,
            checksum_sha512="abc123",
            sha256="def456",
            size_bytes=1024,
            enabled=True,
            installed_by=None,
            created_at=now,
            updated_at=now,
            mod_identifier=mod_identifier,
        )

    def test_manifest_name_matches(self) -> None:
        assert is_geyser_plugin(self._plugin(mod_identifier="Geyser-Spigot"))

    def test_manifest_name_matches_case_insensitively(self) -> None:
        assert is_geyser_plugin(self._plugin(mod_identifier="geyser-spigot"))

    def test_fabric_manifest_id_matches(self) -> None:
        # Geyser-Fabric's fabric.mod.json ``id`` (issue #1910): a locally-uploaded
        # Geyser-Fabric jar is detected symmetrically with the catalog route.
        assert is_geyser_plugin(self._plugin(mod_identifier="geyser-fabric"))

    def test_neoforge_manifest_id_matches(self) -> None:
        # Geyser-NeoForge's neoforge.mods.toml ``modId`` (issue #1910); also the id
        # a Forge-family server detects, whose parser reads the NeoForge descriptor.
        assert is_geyser_plugin(self._plugin(mod_identifier="geyser_neoforge"))

    def test_modrinth_project_id_matches(self) -> None:
        assert is_geyser_plugin(self._plugin(source_project_id="wKkoqHrH"))

    def test_modrinth_slug_matches(self) -> None:
        assert is_geyser_plugin(self._plugin(source_project_id="geyser"))

    def test_modrinth_signal_is_loader_agnostic(self) -> None:
        # Detection is loader-independent by design (issue #1589): the one
        # Modrinth Geyser project serves every loader, so a mod-loader install
        # is still detected. Guards against a future "scope to Paper" regression.
        assert is_geyser_plugin(
            self._plugin(source_project_id="geyser", loader_type=LoaderType.MOD)
        )

    def test_other_plugin_does_not_match(self) -> None:
        assert not is_geyser_plugin(
            self._plugin(mod_identifier="WorldGuard", source_project_id="proj-1")
        )

    def test_unrelated_fabric_mod_does_not_match(self) -> None:
        # A genuine, unrelated Fabric/Forge mod id must not trip Geyser detection
        # now that mod-loader manifest ids are recognized (issue #1910).
        assert not is_geyser_plugin(self._plugin(mod_identifier="fabric-api"))

    def test_floodgate_is_not_the_detection_key(self) -> None:
        # Floodgate is the expected companion but does not own the UDP listener.
        assert not is_geyser_plugin(self._plugin(mod_identifier="floodgate"))

    def test_no_identity_does_not_match(self) -> None:
        assert not is_geyser_plugin(self._plugin())


class TestHasEnabledGeyser:
    """The shared tunnel-dispatch / response-gate predicate (issues #1544, #1555)."""

    @staticmethod
    def _plugin(
        *,
        mod_identifier: str | None = None,
        enabled: bool = True,
        rel_path: str = "plugins/test.jar",
    ) -> ServerPlugin:
        now = dt.datetime.now(tz=dt.timezone.utc)
        return ServerPlugin(
            id=PluginId.new(),
            server_id=ServerId.new(),
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            display_name="Test Plugin",
            description=None,
            loader_type=LoaderType.PLUGIN,
            source=PluginSource.LOCAL,
            source_project_id=None,
            source_version_id=None,
            version_number=None,
            checksum_sha512="abc123",
            sha256="def456",
            size_bytes=1024,
            enabled=enabled,
            installed_by=None,
            created_at=now,
            updated_at=now,
            mod_identifier=mod_identifier,
        )

    def test_false_for_no_plugins(self) -> None:
        assert has_enabled_geyser([]) is False

    def test_false_when_sole_geyser_disabled(self) -> None:
        plugins = [self._plugin(mod_identifier="Geyser-Spigot", enabled=False)]
        assert has_enabled_geyser(plugins) is False

    def test_true_when_geyser_enabled(self) -> None:
        plugins = [self._plugin(mod_identifier="Geyser-Spigot", enabled=True)]
        assert has_enabled_geyser(plugins) is True

    def test_true_when_one_of_two_geyser_copies_enabled(self) -> None:
        plugins = [
            self._plugin(
                mod_identifier="Geyser-Spigot", enabled=True, rel_path="plugins/a.jar"
            ),
            self._plugin(
                mod_identifier="Geyser-Spigot",
                enabled=False,
                rel_path="plugins/b.jar.disabled",
            ),
        ]
        assert has_enabled_geyser(plugins) is True

    def test_false_when_only_non_geyser_plugins_enabled(self) -> None:
        plugins = [self._plugin(mod_identifier="WorldGuard", enabled=True)]
        assert has_enabled_geyser(plugins) is False

    def test_true_when_enabled_geyser_fabric(self) -> None:
        # A locally-uploaded Geyser-Fabric jar drives the predicate too (#1910).
        plugins = [self._plugin(mod_identifier="geyser-fabric", enabled=True)]
        assert has_enabled_geyser(plugins) is True

    def test_true_when_enabled_geyser_neoforge(self) -> None:
        # ...as does a Geyser-NeoForge jar (issue #1910).
        plugins = [self._plugin(mod_identifier="geyser_neoforge", enabled=True)]
        assert has_enabled_geyser(plugins) is True
