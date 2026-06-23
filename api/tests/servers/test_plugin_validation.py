"""Unit tests for the phase-B plugin-set validator (issue #1307).

The validator is a pure function over (server loader + MC version, the server's
installed plugins) returning a checklist of findings. These tests cover each
finding kind in isolation -- the canonical missing-Fabric-API case, a
data-supported conflict, a present-but-out-of-range dep, and MC mismatch -- plus
a fully-valid set that yields nothing.

Per-server adaptation: there is no per-plugin loader-mismatch finding (every
plugin in a server shares the server's loader, so a mismatch is structurally
impossible), unlike the global-library variant this was ported from.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import uuid
import zipfile

from mc_server_dashboard_api.servers.application.plugin_manifest import parse_manifest
from mc_server_dashboard_api.servers.application.plugin_validation import (
    PluginValidation,
    validate_plugin_set,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_NOW = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.timezone.utc)


def _jar(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _plugin(
    *,
    mod_identifier: str | None,
    provides: list[str] | None = None,
    mc_versions: list[str] | None = None,
    dependencies: list[dict[str, object]] | None = None,
    version_number: str = "1.0.0",
    source: PluginSource = PluginSource.LOCAL,
    source_project_id: str | None = None,
    catalog_dependencies: list[dict[str, object]] | None = None,
) -> ServerPlugin:
    name = mod_identifier or "plugin"
    return ServerPlugin(
        id=PluginId.new(),
        server_id=ServerId(uuid.uuid4()),
        rel_path=f"mods/{name}.jar",
        filename=f"{name}.jar",
        display_name=name,
        description=None,
        loader_type=LoaderType.MOD,
        source=source,
        source_project_id=source_project_id,
        source_version_id=None,
        version_number=version_number,
        checksum_sha512=None,
        sha256=None,
        size_bytes=10,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        mod_identifier=mod_identifier,
        provides=provides or [],
        dependencies=dependencies or [],
        mc_versions=mc_versions if mc_versions is not None else ["1.21"],
        catalog_dependencies=catalog_dependencies or [],
    )


def _catalog_dep(
    project_id: str,
    *,
    required: bool = True,
    slug: str | None = None,
    title: str | None = None,
) -> dict[str, object]:
    return {
        "project_id": project_id,
        "required": required,
        "slug": slug,
        "title": title,
    }


def _incompatible_catalog_dep(
    project_id: str, *, slug: str | None = None, title: str | None = None
) -> dict[str, object]:
    return {
        "project_id": project_id,
        "incompatible": True,
        "slug": slug,
        "title": title,
    }


def _dep(
    mod_identifier: str, *, required: bool, version_range: str = ""
) -> dict[str, object]:
    return {
        "mod_identifier": mod_identifier,
        "version_range": version_range,
        "required": required,
    }


class TestMissingDeps:
    def test_required_dep_absent_is_flagged(self) -> None:
        plugin = _plugin(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True, version_range=">=0.90.0")],
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[plugin]
        )

        assert len(result.missing_deps) == 1
        finding = result.missing_deps[0]
        assert finding.mod_id == "sodium"
        assert finding.depends_on == "fabric-api"
        assert finding.version_range == ">=0.90.0"

    def test_required_dep_present_by_identifier(self) -> None:
        sodium = _plugin(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True)],
        )
        fabric_api = _plugin(mod_identifier="fabric-api")
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[sodium, fabric_api]
        )

        assert result.missing_deps == []

    def test_required_dep_satisfied_by_provides(self) -> None:
        sodium = _plugin(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True)],
        )
        all_in_one = _plugin(mod_identifier="qfapi", provides=["fabric-api"])
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[sodium, all_in_one]
        )

        assert result.missing_deps == []

    def test_optional_dep_absent_is_not_flagged(self) -> None:
        plugin = _plugin(
            mod_identifier="sodium",
            dependencies=[_dep("modmenu", required=False)],
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[plugin]
        )

        assert result.missing_deps == []

    def test_plugin_without_manifest_id_is_ignored(self) -> None:
        # A jar with no recognized manifest carries no mod_identifier; it cannot
        # satisfy a dep nor declare one, so it does not affect the checklist.
        sodium = _plugin(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True)],
        )
        unknown = _plugin(mod_identifier="fabric-api")
        unknown.mod_identifier = None
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[sodium, unknown]
        )

        assert len(result.missing_deps) == 1


class TestVersionUnsatisfied:
    def test_present_but_out_of_range_is_flagged(self) -> None:
        sodium = _plugin(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True, version_range=">=0.90.0")],
        )
        fabric_api = _plugin(mod_identifier="fabric-api", version_number="0.80.0")
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[sodium, fabric_api]
        )

        assert result.missing_deps == []
        assert len(result.version_unsatisfied) == 1
        finding = result.version_unsatisfied[0]
        assert finding.mod_id == "sodium"
        assert finding.depends_on == "fabric-api"
        assert finding.version_range == ">=0.90.0"
        assert finding.present_version == "0.80.0"

    def test_present_and_in_range_is_not_flagged(self) -> None:
        sodium = _plugin(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True, version_range=">=0.90.0")],
        )
        fabric_api = _plugin(mod_identifier="fabric-api", version_number="0.95.0")
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[sodium, fabric_api]
        )

        assert result.version_unsatisfied == []

    def test_empty_range_present_is_not_flagged(self) -> None:
        sodium = _plugin(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True)],
        )
        fabric_api = _plugin(mod_identifier="fabric-api", version_number="0.1.0")
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[sodium, fabric_api]
        )

        assert result.version_unsatisfied == []

    def test_forge_maven_interval_out_of_range_is_flagged(self) -> None:
        jei = _plugin(
            mod_identifier="jei",
            dependencies=[_dep("forge-lib", required=True, version_range="[2.0,)")],
        )
        lib = _plugin(mod_identifier="forge-lib", version_number="1.5")
        result = validate_plugin_set(
            server_type="forge", mc_version="1.21", plugins=[jei, lib]
        )

        assert len(result.version_unsatisfied) == 1
        assert result.version_unsatisfied[0].version_range == "[2.0,)"


class TestConflicts:
    def test_marked_conflict_present_is_flagged(self) -> None:
        optifabric = _plugin(
            mod_identifier="optifabric",
            dependencies=[
                {
                    "mod_identifier": "sodium",
                    "version_range": "",
                    "required": False,
                    "conflict": True,
                }
            ],
        )
        sodium = _plugin(mod_identifier="sodium")
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[optifabric, sodium]
        )

        assert len(result.conflicts) == 1
        assert result.conflicts[0].mod_id == "optifabric"
        assert result.conflicts[0].conflicts_with == "sodium"

    def test_marked_conflict_absent_is_not_flagged(self) -> None:
        optifabric = _plugin(
            mod_identifier="optifabric",
            dependencies=[
                {
                    "mod_identifier": "sodium",
                    "version_range": "",
                    "required": False,
                    "conflict": True,
                }
            ],
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[optifabric]
        )

        assert result.conflicts == []

    def test_parsed_break_against_present_plugin_is_flagged(self) -> None:
        breaking = _plugin(
            mod_identifier="optifabric",
            dependencies=parse_manifest(
                _jar(
                    {
                        "fabric.mod.json": json.dumps(
                            {
                                "id": "optifabric",
                                "version": "1.0.0",
                                "breaks": {"sodium": "*"},
                            }
                        )
                    }
                ),
                server_type="fabric",
            ).dependencies,
        )
        sodium = _plugin(mod_identifier="sodium")
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[breaking, sodium]
        )

        assert len(result.conflicts) == 1
        assert result.conflicts[0].conflicts_with == "sodium"

    def test_break_range_out_of_range_is_not_flagged(self) -> None:
        # REI breaks cloth-config2 only below 6.2; Cloth Config v15.0.140
        # provides cloth-config2 -- well above the range, so no conflict (#1324).
        rei = _plugin(
            mod_identifier="roughlyenoughitems",
            dependencies=parse_manifest(
                _jar(
                    {
                        "fabric.mod.json": json.dumps(
                            {
                                "id": "roughlyenoughitems",
                                "version": "1.0.0",
                                "breaks": {"cloth-config2": "<6.2-"},
                            }
                        )
                    }
                ),
                server_type="fabric",
            ).dependencies,
        )
        cloth = _plugin(
            mod_identifier="cloth-config",
            provides=["cloth-config2"],
            version_number="15.0.140",
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[rei, cloth]
        )

        assert result.conflicts == []

    def test_break_range_in_range_is_flagged(self) -> None:
        # The same break edge fires when the present cloth-config2 (v6.1) falls
        # inside the declared ``<6.2`` break range (#1324).
        rei = _plugin(
            mod_identifier="roughlyenoughitems",
            dependencies=parse_manifest(
                _jar(
                    {
                        "fabric.mod.json": json.dumps(
                            {
                                "id": "roughlyenoughitems",
                                "version": "1.0.0",
                                "breaks": {"cloth-config2": "<6.2-"},
                            }
                        )
                    }
                ),
                server_type="fabric",
            ).dependencies,
        )
        cloth = _plugin(
            mod_identifier="cloth-config",
            provides=["cloth-config2"],
            version_number="6.1",
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[rei, cloth]
        )

        assert len(result.conflicts) == 1
        assert result.conflicts[0].mod_id == "roughlyenoughitems"
        assert result.conflicts[0].conflicts_with == "cloth-config2"

    def test_break_empty_range_is_flagged_regardless_of_version(self) -> None:
        # An empty break range means "any version" -- still a conflict (#1324).
        breaking = _plugin(
            mod_identifier="optifabric",
            dependencies=[
                {
                    "mod_identifier": "sodium",
                    "version_range": "",
                    "required": False,
                    "conflict": True,
                }
            ],
        )
        sodium = _plugin(mod_identifier="sodium", version_number="0.5.99")
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[breaking, sodium]
        )

        assert len(result.conflicts) == 1
        assert result.conflicts[0].conflicts_with == "sodium"

    def test_catalog_incompatible_against_installed_project_is_flagged(self) -> None:
        # A Modrinth catalog ``incompatible`` edge (issue #1318), keyed by
        # project_id, is flagged when an installed plugin has that
        # source_project_id; the finding reports that plugin's mod_identifier.
        mod = _plugin(
            mod_identifier="mod-a",
            source=PluginSource.MODRINTH,
            source_project_id="MODA",
            catalog_dependencies=[_incompatible_catalog_dep("RIVAL")],
        )
        rival = _plugin(
            mod_identifier="rival",
            source=PluginSource.MODRINTH,
            source_project_id="RIVAL",
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[mod, rival]
        )

        assert len(result.conflicts) == 1
        assert result.conflicts[0].mod_id == "mod-a"
        assert result.conflicts[0].conflicts_with == "rival"

    def test_catalog_incompatible_target_absent_is_not_flagged(self) -> None:
        mod = _plugin(
            mod_identifier="mod-a",
            source=PluginSource.MODRINTH,
            source_project_id="MODA",
            catalog_dependencies=[_incompatible_catalog_dep("RIVAL")],
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[mod]
        )

        assert result.conflicts == []


class TestMcMismatch:
    def test_version_not_listed_is_flagged(self) -> None:
        plugin = _plugin(mod_identifier="sodium", mc_versions=["1.20.4"])
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[plugin]
        )

        assert len(result.mc_mismatch) == 1
        finding = result.mc_mismatch[0]
        assert finding.mod_id == "sodium"
        assert finding.mod_mc_versions == ["1.20.4"]
        assert finding.server_mc_version == "1.21"

    def test_version_listed_is_ok(self) -> None:
        plugin = _plugin(mod_identifier="sodium", mc_versions=["1.20.4", "1.21"])
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[plugin]
        )

        assert result.mc_mismatch == []

    def test_unconstrained_plugin_is_never_flagged(self) -> None:
        plugin = _plugin(mod_identifier="sodium", mc_versions=[])
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[plugin]
        )

        assert result.mc_mismatch == []

    def test_forge_interval_covering_server_version_is_ok(self) -> None:
        # A Forge mod declares its MC compat as a Maven interval; the server
        # version inside it must not be flagged (no false positive).
        mod = _plugin(mod_identifier="jei", mc_versions=["[1.20.4,1.21)"])
        result = validate_plugin_set(
            server_type="forge", mc_version="1.20.4", plugins=[mod]
        )

        assert result.mc_mismatch == []

    def test_forge_interval_excluding_server_version_is_flagged(self) -> None:
        mod = _plugin(mod_identifier="jei", mc_versions=["[1.20.4,1.21)"])
        result = validate_plugin_set(
            server_type="forge", mc_version="1.21", plugins=[mod]
        )

        assert len(result.mc_mismatch) == 1


class TestPaperMcMismatch:
    # A Bukkit/Paper ``api-version`` is a major.minor *minimum floor*, not an
    # exact version: a server at any ``1.21.x`` (or newer) satisfies
    # ``api-version: 1.21``; an older minor does not. (Bug 1.)

    def test_patch_level_server_satisfies_api_version(self) -> None:
        plugin = _plugin(mod_identifier="EssentialsX", mc_versions=["1.21"])
        result = validate_plugin_set(
            server_type="paper", mc_version="1.21.1", plugins=[plugin]
        )

        assert result.mc_mismatch == []

    def test_exact_minor_server_satisfies_api_version(self) -> None:
        plugin = _plugin(mod_identifier="EssentialsX", mc_versions=["1.21"])
        result = validate_plugin_set(
            server_type="paper", mc_version="1.21.0", plugins=[plugin]
        )

        assert result.mc_mismatch == []

    def test_older_minor_server_is_flagged(self) -> None:
        plugin = _plugin(mod_identifier="EssentialsX", mc_versions=["1.21"])
        result = validate_plugin_set(
            server_type="paper", mc_version="1.20.4", plugins=[plugin]
        )

        assert len(result.mc_mismatch) == 1
        assert result.mc_mismatch[0].mod_id == "EssentialsX"

    def test_newer_minor_server_satisfies_api_version(self) -> None:
        plugin = _plugin(mod_identifier="EssentialsX", mc_versions=["1.21"])
        result = validate_plugin_set(
            server_type="paper", mc_version="1.22", plugins=[plugin]
        )

        assert result.mc_mismatch == []


class TestFullyValidSet:
    def test_no_findings(self) -> None:
        sodium = _plugin(
            mod_identifier="sodium",
            mc_versions=["1.21"],
            dependencies=[_dep("fabric-api", required=True)],
        )
        fabric_api = _plugin(mod_identifier="fabric-api", mc_versions=["1.21"])
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[sodium, fabric_api]
        )

        assert result == PluginValidation()

    def test_empty_set_is_valid(self) -> None:
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21", plugins=[]
        )

        assert result == PluginValidation()


class TestMissingCatalogDeps:
    # A Modrinth-sourced plugin whose jar manifest declares no deps but whose
    # Modrinth catalog declares required deps (keyed by project_id). The canonical
    # case: Roughly Enough Items, manifest depends only on minecraft, catalog
    # requires Architectury / Cloth Config / Fabric API.

    def test_unsatisfied_catalog_dep_is_flagged(self) -> None:
        rei = _plugin(
            mod_identifier="roughlyenoughitems",
            source=PluginSource.MODRINTH,
            source_project_id="nfn13YXA",
            catalog_dependencies=[
                _catalog_dep("lhGA9TYQ", slug="architectury-api", title="Architectury")
            ],
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21.1", plugins=[rei]
        )

        assert len(result.missing_catalog_deps) == 1
        finding = result.missing_catalog_deps[0]
        assert finding.mod_id == "roughlyenoughitems"
        assert finding.project_id == "lhGA9TYQ"
        assert finding.slug == "architectury-api"
        assert finding.title == "Architectury"
        # The manifest-driven missing-deps list is untouched.
        assert result.missing_deps == []

    def test_catalog_dep_satisfied_by_installed_project_id(self) -> None:
        rei = _plugin(
            mod_identifier="roughlyenoughitems",
            source=PluginSource.MODRINTH,
            source_project_id="nfn13YXA",
            catalog_dependencies=[_catalog_dep("lhGA9TYQ")],
        )
        architectury = _plugin(
            mod_identifier="architectury",
            source=PluginSource.MODRINTH,
            source_project_id="lhGA9TYQ",
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21.1", plugins=[rei, architectury]
        )

        assert result.missing_catalog_deps == []

    def test_optional_catalog_dep_is_not_flagged(self) -> None:
        rei = _plugin(
            mod_identifier="roughlyenoughitems",
            source=PluginSource.MODRINTH,
            source_project_id="nfn13YXA",
            catalog_dependencies=[_catalog_dep("modmenu", required=False)],
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21.1", plugins=[rei]
        )

        assert result.missing_catalog_deps == []

    def test_local_plugin_catalog_deps_are_ignored(self) -> None:
        # A local upload never carries catalog deps; even if some were present
        # they must not be evaluated (only Modrinth-sourced plugins are).
        local = _plugin(
            mod_identifier="some-mod",
            source=PluginSource.LOCAL,
            source_project_id=None,
            catalog_dependencies=[_catalog_dep("lhGA9TYQ")],
        )
        result = validate_plugin_set(
            server_type="fabric", mc_version="1.21.1", plugins=[local]
        )

        assert result.missing_catalog_deps == []
