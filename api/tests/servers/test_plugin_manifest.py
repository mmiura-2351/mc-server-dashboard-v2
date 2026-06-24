"""Tests for the jar manifest parser (issue #1307).

Builds tiny synthetic jars (zips) carrying one loader manifest each and asserts
the structured metadata extracted from them: mod identifier, provides, MC-version
compatibility, and dependencies. The parser is told which loader family to look
for (derived from the server's ``ServerType`` at ingest time), so a Forge server
reads ``META-INF/(neoforge.)mods.toml`` and a Paper server reads
``(paper-)plugin.yml``.

Jars are constructed programmatically with ``zipfile.ZipFile`` + ``io.BytesIO``
(the resource-pack zip test precedent).
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from mc_server_dashboard_api.servers.application.plugin_manifest import (
    ParsedManifest,
    parse_manifest,
)
from mc_server_dashboard_api.servers.domain.errors import InvalidModJarError


def _make_jar(entries: dict[str, str | bytes]) -> bytes:
    """Build a jar (zip) in memory from {path: content} pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestFabric:
    def test_extracts_core_fields(self) -> None:
        manifest = {
            "schemaVersion": 1,
            "id": "examplemod",
            "version": "1.2.3",
            "depends": {
                "fabricloader": ">=0.15.0",
                "minecraft": "~1.20.4",
                "fabric-api": "*",
            },
            "recommends": {"modmenu": "*"},
            "breaks": {"oldmod": "<1.0.0"},
            "provides": ["examplemod-api"],
        }
        jar = _make_jar({"fabric.mod.json": json.dumps(manifest)})

        meta = parse_manifest(jar, server_type="fabric")

        assert meta.mod_identifier == "examplemod"
        assert meta.provides == ["examplemod-api"]

    def test_mc_versions_from_minecraft_depend(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "depends": {"minecraft": "~1.20.4"},
        }
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        assert meta.mc_versions == ["~1.20.4"]

    def test_mc_versions_accepts_list_range(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "depends": {"minecraft": [">=1.20", "<1.21"]},
        }
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        assert meta.mc_versions == [">=1.20", "<1.21"]

    def test_dependencies_exclude_loader_and_minecraft(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "depends": {
                "fabricloader": ">=0.15.0",
                "minecraft": "1.20.4",
                "java": ">=17",
                "fabric-api": ">=0.90.0",
            },
        }
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert "fabricloader" not in deps
        assert "minecraft" not in deps
        assert "java" not in deps
        assert deps["fabric-api"] == {
            "mod_identifier": "fabric-api",
            "version_range": ">=0.90.0",
            "required": True,
            "conflict": False,
        }

    def test_recommends_are_optional_dependencies(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "recommends": {"modmenu": "*"},
        }
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["modmenu"]["required"] is False

    def test_breaks_become_conflict_entries(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "breaks": {"oldmod": "<1.0.0"},
        }
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["oldmod"] == {
            "mod_identifier": "oldmod",
            "version_range": "<1.0.0",
            "required": False,
            "conflict": True,
        }

    def test_normal_deps_are_not_conflicts(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "depends": {"fabric-api": "*"},
        }
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["fabric-api"]["conflict"] is False


class TestSide:
    """Side auto-detection from the Fabric ``environment`` field (issue #1308)."""

    def test_environment_client_maps_to_client(self) -> None:
        manifest = {"id": "m", "version": "1.0.0", "environment": "client"}
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        assert meta.side == "client"

    def test_environment_server_maps_to_server(self) -> None:
        manifest = {"id": "m", "version": "1.0.0", "environment": "server"}
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        assert meta.side == "server"

    def test_environment_star_maps_to_both(self) -> None:
        manifest = {"id": "m", "version": "1.0.0", "environment": "*"}
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        assert meta.side == "both"

    def test_missing_environment_defaults_to_both(self) -> None:
        manifest = {"id": "m", "version": "1.0.0"}
        meta = parse_manifest(
            _make_jar({"fabric.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        assert meta.side == "both"

    def test_forge_side_defaults_to_both(self) -> None:
        # Forge/NeoForge side hints are unreliable -> default both.
        toml = '[[mods]]\nmodId = "m"\nversion = "1.0.0"\n'
        meta = parse_manifest(
            _make_jar({"META-INF/mods.toml": toml}), server_type="forge"
        )
        assert meta.side == "both"

    def test_paper_side_is_server(self) -> None:
        """Paper plugins are always server-side only (issue #1342)."""
        yml = "name: P\nversion: 1.0.0\n"
        meta = parse_manifest(_make_jar({"plugin.yml": yml}), server_type="paper")
        assert meta.side == "server"


class TestQuilt:
    def test_quilt_parsed_on_fabric_server(self) -> None:
        # A Fabric server can run Quilt mods; the parser tries quilt.mod.json too.
        manifest = {
            "schema_version": 1,
            "quilt_loader": {
                "id": "examplemod",
                "version": "2.0.0",
                "depends": [
                    {"id": "quilt_base", "versions": ">=1.0.0"},
                    {"id": "minecraft", "versions": "1.20.4"},
                    {"id": "qsl", "versions": "*", "optional": True},
                ],
                "provides": [{"id": "examplemod-api"}],
            },
        }
        jar = _make_jar({"quilt.mod.json": json.dumps(manifest)})

        meta = parse_manifest(jar, server_type="fabric")

        assert meta.mod_identifier == "examplemod"
        assert meta.provides == ["examplemod-api"]
        assert meta.mc_versions == ["1.20.4"]
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert "minecraft" not in deps
        assert deps["quilt_base"]["required"] is True
        assert deps["qsl"]["required"] is False

    def test_quilt_breaks_become_conflict_entries(self) -> None:
        manifest = {
            "quilt_loader": {
                "id": "m",
                "version": "1.0.0",
                "breaks": [{"id": "oldmod", "versions": "<1.0.0"}],
            }
        }
        meta = parse_manifest(
            _make_jar({"quilt.mod.json": json.dumps(manifest)}), server_type="fabric"
        )
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["oldmod"] == {
            "mod_identifier": "oldmod",
            "version_range": "<1.0.0",
            "required": False,
            "conflict": True,
        }


class TestForge:
    def test_extracts_core_fields(self) -> None:
        toml = """
modLoader = "javafml"
loaderVersion = "[47,)"

[[mods]]
modId = "examplemod"
version = "3.1.0"

[[dependencies.examplemod]]
modId = "forge"
mandatory = true
versionRange = "[47,)"
side = "BOTH"

[[dependencies.examplemod]]
modId = "minecraft"
mandatory = true
versionRange = "[1.20.4,1.21)"
side = "BOTH"

[[dependencies.examplemod]]
modId = "jei"
mandatory = false
versionRange = "*"
side = "BOTH"
"""
        jar = _make_jar({"META-INF/mods.toml": toml})

        meta = parse_manifest(jar, server_type="forge")

        assert meta.mod_identifier == "examplemod"
        assert meta.mc_versions == ["[1.20.4,1.21)"]
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert "forge" not in deps
        assert "minecraft" not in deps
        assert deps["jei"] == {
            "mod_identifier": "jei",
            "version_range": "*",
            "required": False,
            "conflict": False,
        }

    def test_incompatible_dependency_becomes_conflict(self) -> None:
        toml = """
[[mods]]
modId = "m"
version = "1.0.0"

[[dependencies.m]]
modId = "oldmod"
type = "incompatible"
versionRange = "[1.0,2.0)"
"""
        meta = parse_manifest(
            _make_jar({"META-INF/mods.toml": toml}), server_type="forge"
        )
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["oldmod"] == {
            "mod_identifier": "oldmod",
            "version_range": "[1.0,2.0)",
            "required": False,
            "conflict": True,
        }

    def test_optional_dependency_is_not_conflict(self) -> None:
        toml = """
[[mods]]
modId = "m"
version = "1.0.0"

[[dependencies.m]]
modId = "jei"
mandatory = false
versionRange = "*"
"""
        meta = parse_manifest(
            _make_jar({"META-INF/mods.toml": toml}), server_type="forge"
        )
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["jei"]["conflict"] is False
        assert deps["jei"]["required"] is False

    def test_neoforge_parsed_on_forge_server(self) -> None:
        # A Forge server reads both mods.toml and neoforge.mods.toml.
        toml = """
[[mods]]
modId = "neomod"
version = "1.0.0"

[[dependencies.neomod]]
modId = "neoforge"
type = "required"
versionRange = "[21,)"

[[dependencies.neomod]]
modId = "minecraft"
type = "required"
versionRange = "[1.21,1.22)"

[[dependencies.neomod]]
modId = "somelib"
type = "optional"
versionRange = "*"
"""
        meta = parse_manifest(
            _make_jar({"META-INF/neoforge.mods.toml": toml}), server_type="forge"
        )
        assert meta.mod_identifier == "neomod"
        assert meta.mc_versions == ["[1.21,1.22)"]
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert "neoforge" not in deps
        assert "minecraft" not in deps
        assert deps["somelib"]["required"] is False


class TestPaper:
    def test_plugin_yml(self) -> None:
        yml = """
name: ExamplePlugin
version: 4.2.0
main: com.example.ExamplePlugin
api-version: '1.20'
depend: [Vault, WorldEdit]
softdepend: [PlaceholderAPI]
"""
        jar = _make_jar({"plugin.yml": yml})

        meta = parse_manifest(jar, server_type="paper")

        assert meta.mod_identifier == "ExamplePlugin"
        assert meta.mc_versions == ["1.20"]
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["Vault"]["required"] is True
        assert deps["WorldEdit"]["required"] is True
        assert deps["PlaceholderAPI"]["required"] is False

    def test_plugin_yml_block_list_deps(self) -> None:
        yml = """
name: P
version: 1.0.0
depend:
  - Vault
  - WorldEdit
softdepend:
  - PlaceholderAPI
"""
        meta = parse_manifest(_make_jar({"plugin.yml": yml}), server_type="paper")
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["Vault"]["required"] is True
        assert deps["PlaceholderAPI"]["required"] is False

    def test_plugin_yml_trailing_comments_stripped(self) -> None:
        # Trailing inline comments are common in real descriptors; they must not
        # pollute the scalar value (Bug 2).
        yml = """
name: ExamplePlugin # the plugin name
version: 4.2.0   # release
api-version: '1.20' # min server
"""
        meta = parse_manifest(_make_jar({"plugin.yml": yml}), server_type="paper")
        assert meta.mod_identifier == "ExamplePlugin"
        assert meta.mc_versions == ["1.20"]

    def test_plugin_yml_inline_list_dep_with_trailing_comment(self) -> None:
        # `depend: [Vault] # comment` must keep the dependency: the unquoted
        # trailing comment is stripped before the `]`-terminated list is parsed
        # (without the fix the value no longer ends in `]` and is silently
        # dropped). (Bug 2.)
        yml = """
name: P
version: 1.0.0
depend: [Vault, WorldEdit] # needs these
"""
        meta = parse_manifest(_make_jar({"plugin.yml": yml}), server_type="paper")
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["Vault"]["required"] is True
        assert deps["WorldEdit"]["required"] is True

    def test_plugin_yml_block_list_dep_with_trailing_comment(self) -> None:
        yml = """
name: P
version: 1.0.0
depend:
  - Vault # economy
  - WorldEdit
"""
        meta = parse_manifest(_make_jar({"plugin.yml": yml}), server_type="paper")
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["Vault"]["required"] is True
        assert deps["WorldEdit"]["required"] is True

    def test_plugin_yml_hash_inside_quoted_scalar_kept(self) -> None:
        # A `#` inside a quoted scalar is not a comment and must be preserved.
        yml = 'name: "Foo # Bar"\nversion: 1.0.0\n'
        meta = parse_manifest(_make_jar({"plugin.yml": yml}), server_type="paper")
        assert meta.mod_identifier == "Foo # Bar"

    def test_plugin_yml_hash_without_leading_space_kept(self) -> None:
        # A `#` not preceded by whitespace does not begin a comment (YAML rule).
        yml = "name: Foo#Bar\nversion: 1.0.0\n"
        meta = parse_manifest(_make_jar({"plugin.yml": yml}), server_type="paper")
        assert meta.mod_identifier == "Foo#Bar"

    def test_paper_manifest_side_is_server(self) -> None:
        """Paper plugins are always server-side only (issue #1342)."""
        yml = "name: P\nversion: 1.0.0\n"
        meta = parse_manifest(_make_jar({"plugin.yml": yml}), server_type="paper")
        assert meta.side == "server"

    def test_paper_plugin_yml_preferred(self) -> None:
        paper_yml = "name: NewStyle\nversion: 1.0.0\n"
        plugin_yml = "name: OldStyle\nversion: 0.0.1\n"
        meta = parse_manifest(
            _make_jar({"plugin.yml": plugin_yml, "paper-plugin.yml": paper_yml}),
            server_type="paper",
        )
        assert meta.mod_identifier == "NewStyle"


class TestLoaderTargeting:
    def test_paper_server_ignores_fabric_manifest(self) -> None:
        # A Paper server reads only the plugin descriptor: a stray fabric.mod.json
        # must not be picked up.
        fabric = json.dumps({"id": "fabricmod", "version": "1.0.0"})
        plugin = "name: RealPlugin\nversion: 1.0.0\n"
        jar = _make_jar({"fabric.mod.json": fabric, "plugin.yml": plugin})
        meta = parse_manifest(jar, server_type="paper")
        assert meta.mod_identifier == "RealPlugin"

    def test_fabric_server_ignores_plugin_yml(self) -> None:
        fabric = json.dumps({"id": "realmod", "version": "1.0.0"})
        plugin = "name: StrayPlugin\nversion: 0.0.1\n"
        jar = _make_jar({"fabric.mod.json": fabric, "plugin.yml": plugin})
        meta = parse_manifest(jar, server_type="fabric")
        assert meta.mod_identifier == "realmod"


class TestUnknownAndGarbled:
    def test_no_manifest_returns_empty(self) -> None:
        jar = _make_jar({"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n"})
        meta = parse_manifest(jar, server_type="fabric")
        assert meta == ParsedManifest.empty()
        assert meta.mod_identifier == ""

    def test_garbled_fabric_json_returns_empty(self) -> None:
        jar = _make_jar({"fabric.mod.json": "{ this is not json"})
        meta = parse_manifest(jar, server_type="fabric")
        assert meta.mod_identifier == ""

    def test_garbled_forge_toml_returns_empty(self) -> None:
        jar = _make_jar({"META-INF/mods.toml": "this = = not toml"})
        meta = parse_manifest(jar, server_type="forge")
        assert meta.mod_identifier == ""

    def test_fabric_without_id_returns_empty(self) -> None:
        jar = _make_jar({"fabric.mod.json": json.dumps({"version": "1.0.0"})})
        meta = parse_manifest(jar, server_type="fabric")
        assert meta.mod_identifier == ""


class TestInvalidJar:
    def test_non_zip_bytes_raise(self) -> None:
        with pytest.raises(InvalidModJarError):
            parse_manifest(b"not a zip at all", server_type="fabric")

    def test_empty_bytes_raise(self) -> None:
        with pytest.raises(InvalidModJarError):
            parse_manifest(b"", server_type="fabric")


class TestZipSafety:
    def test_too_many_entries_raise(self) -> None:
        from mc_server_dashboard_api.servers.application import plugin_manifest

        entries: dict[str, str | bytes] = {
            f"f{i}.txt": b"x" for i in range(plugin_manifest._MAX_ENTRY_COUNT + 1)
        }
        with pytest.raises(InvalidModJarError):
            parse_manifest(_make_jar(entries), server_type="fabric")

    def test_decompression_bomb_raises(self) -> None:
        from mc_server_dashboard_api.servers.application import plugin_manifest

        bomb = b"\x00" * (plugin_manifest._MAX_MANIFEST_BYTES + 1)
        entries: dict[str, str | bytes] = {"fabric.mod.json": bomb}
        with pytest.raises(InvalidModJarError):
            parse_manifest(_make_jar(entries), server_type="fabric")
