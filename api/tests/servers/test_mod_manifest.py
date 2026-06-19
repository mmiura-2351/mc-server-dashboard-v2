"""Tests for the mod jar manifest parser (issue #1260).

Builds tiny synthetic jars (zips) carrying one loader manifest each and asserts
the structured metadata extracted from them: loader, mod identifier, provides,
version, MC-version compatibility, dependencies, and side.

Jars are constructed programmatically with ``zipfile.ZipFile`` + ``io.BytesIO``
(the resource-pack zip test precedent).
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from mc_server_dashboard_api.servers.application.mod_manifest import (
    ParsedModMetadata,
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

        meta = parse_manifest(jar)

        assert meta.loader_type == "fabric"
        assert meta.mod_identifier == "examplemod"
        assert meta.version_number == "1.2.3"
        assert meta.provides == ["examplemod-api"]

    def test_mc_versions_from_minecraft_depend(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "depends": {"minecraft": "~1.20.4"},
        }
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
        assert meta.mc_versions == ["~1.20.4"]

    def test_mc_versions_accepts_list_range(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "depends": {"minecraft": [">=1.20", "<1.21"]},
        }
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
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
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
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
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["modmenu"]["required"] is False

    def test_breaks_become_conflict_entries(self) -> None:
        manifest = {
            "id": "m",
            "version": "1.0.0",
            "breaks": {"oldmod": "<1.0.0"},
        }
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
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
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["fabric-api"]["conflict"] is False

    def test_environment_client_is_client_side(self) -> None:
        manifest = {"id": "m", "version": "1.0.0", "environment": "client"}
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
        assert meta.side == "client"

    def test_environment_server_is_server_side(self) -> None:
        manifest = {"id": "m", "version": "1.0.0", "environment": "server"}
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
        assert meta.side == "server"

    def test_environment_star_is_both(self) -> None:
        manifest = {"id": "m", "version": "1.0.0", "environment": "*"}
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
        assert meta.side == "both"

    def test_missing_environment_defaults_to_both(self) -> None:
        manifest = {"id": "m", "version": "1.0.0"}
        meta = parse_manifest(_make_jar({"fabric.mod.json": json.dumps(manifest)}))
        assert meta.side == "both"


class TestQuilt:
    def test_extracts_core_fields(self) -> None:
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

        meta = parse_manifest(jar)

        assert meta.loader_type == "quilt"
        assert meta.mod_identifier == "examplemod"
        assert meta.version_number == "2.0.0"
        assert meta.provides == ["examplemod-api"]
        assert meta.mc_versions == ["1.20.4"]
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert "minecraft" not in deps
        assert deps["quilt_base"]["required"] is True
        assert deps["qsl"]["required"] is False

    def test_provides_accepts_plain_string_id(self) -> None:
        manifest = {
            "quilt_loader": {
                "id": "m",
                "version": "1.0.0",
                "provides": ["alias"],
            }
        }
        meta = parse_manifest(_make_jar({"quilt.mod.json": json.dumps(manifest)}))
        assert meta.provides == ["alias"]

    def test_breaks_become_conflict_entries(self) -> None:
        manifest = {
            "quilt_loader": {
                "id": "m",
                "version": "1.0.0",
                "breaks": [{"id": "oldmod", "versions": "<1.0.0"}],
            }
        }
        meta = parse_manifest(_make_jar({"quilt.mod.json": json.dumps(manifest)}))
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

        meta = parse_manifest(jar)

        assert meta.loader_type == "forge"
        assert meta.mod_identifier == "examplemod"
        assert meta.version_number == "3.1.0"
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

    def test_side_defaults_to_both_even_with_hints(self) -> None:
        # Forge side hints are unreliable, so the parser ignores them.
        toml = """
[[mods]]
modId = "clientonly"
version = "1.0.0"

[[dependencies.clientonly]]
modId = "forge"
mandatory = true
versionRange = "*"
side = "CLIENT"
"""
        meta = parse_manifest(_make_jar({"META-INF/mods.toml": toml}))
        assert meta.side == "both"

    def test_incompatible_dependency_becomes_conflict(self) -> None:
        # NeoForge-style ``type = "incompatible"`` (also accepted in Forge
        # toolchains) marks a hard break, not just an optional dependency.
        toml = """
[[mods]]
modId = "m"
version = "1.0.0"

[[dependencies.m]]
modId = "oldmod"
type = "incompatible"
versionRange = "[1.0,2.0)"
"""
        meta = parse_manifest(_make_jar({"META-INF/mods.toml": toml}))
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["oldmod"] == {
            "mod_identifier": "oldmod",
            "version_range": "[1.0,2.0)",
            "required": False,
            "conflict": True,
        }

    def test_discouraged_dependency_becomes_conflict(self) -> None:
        toml = """
[[mods]]
modId = "m"
version = "1.0.0"

[[dependencies.m]]
modId = "shaky"
type = "discouraged"
versionRange = "*"
"""
        meta = parse_manifest(_make_jar({"META-INF/mods.toml": toml}))
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["shaky"]["conflict"] is True

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
        meta = parse_manifest(_make_jar({"META-INF/mods.toml": toml}))
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["jei"]["conflict"] is False
        assert deps["jei"]["required"] is False

    def test_version_placeholder_kept_verbatim(self) -> None:
        # Forge jars commonly carry ``${file.jarVersion}``; the parser does not
        # resolve it (no jar manifest substitution), it keeps the raw string.
        toml = """
[[mods]]
modId = "m"
version = "${file.jarVersion}"
"""
        meta = parse_manifest(_make_jar({"META-INF/mods.toml": toml}))
        assert meta.version_number == "${file.jarVersion}"


class TestNeoForge:
    def test_neoforge_manifest(self) -> None:
        toml = """
[[mods]]
modId = "neomod"
version = "1.0.0"

[[dependencies.neomod]]
modId = "neoforge"
type = "required"
versionRange = "[21,)"
side = "BOTH"

[[dependencies.neomod]]
modId = "minecraft"
type = "required"
versionRange = "[1.21,1.22)"
side = "BOTH"
"""
        jar = _make_jar({"META-INF/neoforge.mods.toml": toml})

        meta = parse_manifest(jar)

        assert meta.loader_type == "neoforge"
        assert meta.mod_identifier == "neomod"
        assert meta.mc_versions == ["[1.21,1.22)"]
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert "neoforge" not in deps
        assert "minecraft" not in deps

    def test_neoforge_required_via_type_field(self) -> None:
        # NeoForge dropped ``mandatory`` in favour of ``type = "required"``.
        toml = """
[[mods]]
modId = "m"
version = "1.0.0"

[[dependencies.m]]
modId = "somelib"
type = "optional"
versionRange = "*"
"""
        meta = parse_manifest(_make_jar({"META-INF/neoforge.mods.toml": toml}))
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["somelib"]["required"] is False

    def test_neoforge_incompatible_becomes_conflict(self) -> None:
        toml = """
[[mods]]
modId = "neomod"
version = "1.0.0"

[[dependencies.neomod]]
modId = "oldmod"
type = "incompatible"
versionRange = "*"
"""
        meta = parse_manifest(_make_jar({"META-INF/neoforge.mods.toml": toml}))
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["oldmod"]["conflict"] is True
        assert deps["oldmod"]["required"] is False


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

        meta = parse_manifest(jar)

        assert meta.loader_type == "paper"
        assert meta.mod_identifier == "ExamplePlugin"
        assert meta.version_number == "4.2.0"
        assert meta.mc_versions == ["1.20"]
        deps = {d["mod_identifier"]: d for d in meta.dependencies}
        assert deps["Vault"]["required"] is True
        assert deps["WorldEdit"]["required"] is True
        assert deps["PlaceholderAPI"]["required"] is False

    def test_paper_plugin_yml_preferred(self) -> None:
        paper_yml = "name: NewStyle\nversion: 1.0.0\n"
        plugin_yml = "name: OldStyle\nversion: 0.0.1\n"
        meta = parse_manifest(
            _make_jar({"plugin.yml": plugin_yml, "paper-plugin.yml": paper_yml})
        )
        assert meta.mod_identifier == "NewStyle"

    def test_paper_side_is_server(self) -> None:
        # A Bukkit/Paper plugin only ever runs server-side.
        meta = parse_manifest(_make_jar({"plugin.yml": "name: P\nversion: 1.0.0\n"}))
        assert meta.side == "server"


class TestLoaderPrecedence:
    def test_fabric_wins_over_paper_when_both_present(self) -> None:
        # A jar carrying both manifests is parsed as the modern modloader,
        # not the legacy plugin descriptor.
        fabric = json.dumps({"id": "m", "version": "1.0.0"})
        jar = _make_jar(
            {
                "fabric.mod.json": fabric,
                "plugin.yml": "name: P\nversion: 0.0.1\n",
            }
        )
        meta = parse_manifest(jar)
        assert meta.loader_type == "fabric"


class TestUnknownAndGarbled:
    def test_no_manifest_returns_unknown(self) -> None:
        jar = _make_jar({"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n"})
        meta = parse_manifest(jar)
        assert meta == ParsedModMetadata.unknown()
        assert meta.loader_type == "unknown"
        assert meta.side == "both"

    def test_garbled_fabric_json_returns_unknown(self) -> None:
        jar = _make_jar({"fabric.mod.json": "{ this is not json"})
        meta = parse_manifest(jar)
        assert meta.loader_type == "unknown"

    def test_garbled_forge_toml_returns_unknown(self) -> None:
        jar = _make_jar({"META-INF/mods.toml": "this = = not toml"})
        meta = parse_manifest(jar)
        assert meta.loader_type == "unknown"

    def test_fabric_without_id_returns_unknown(self) -> None:
        # A recognized manifest file that lacks the required identity is not a
        # usable parse result.
        jar = _make_jar({"fabric.mod.json": json.dumps({"version": "1.0.0"})})
        meta = parse_manifest(jar)
        assert meta.loader_type == "unknown"


class TestInvalidJar:
    def test_non_zip_bytes_raise(self) -> None:
        with pytest.raises(InvalidModJarError):
            parse_manifest(b"not a zip at all")

    def test_empty_bytes_raise(self) -> None:
        with pytest.raises(InvalidModJarError):
            parse_manifest(b"")


class TestZipSafety:
    def test_too_many_entries_raise(self) -> None:
        from mc_server_dashboard_api.servers.application import mod_manifest

        entries: dict[str, str | bytes] = {
            f"f{i}.txt": b"x" for i in range(mod_manifest._MAX_ENTRY_COUNT + 1)
        }
        with pytest.raises(InvalidModJarError):
            parse_manifest(_make_jar(entries))

    def test_decompression_bomb_raises(self) -> None:
        from mc_server_dashboard_api.servers.application import mod_manifest

        # One highly-compressible entry that decompresses past the cap.
        bomb = b"\x00" * (mod_manifest._MAX_DECOMPRESSED_BYTES + 1)
        entries: dict[str, str | bytes] = {"fabric.mod.json": bomb}
        with pytest.raises(InvalidModJarError):
            parse_manifest(_make_jar(entries))
