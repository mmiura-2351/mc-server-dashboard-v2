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
    mod_identifier: str,
    provides: list[str] | None = None,
    mc_versions: list[str] | None = None,
    dependencies: list[dict[str, object]] | None = None,
    version_number: str = "1.0.0",
) -> ServerPlugin:
    return ServerPlugin(
        id=PluginId.new(),
        server_id=ServerId(uuid.uuid4()),
        rel_path=f"mods/{mod_identifier}.jar",
        filename=f"{mod_identifier}.jar",
        display_name=mod_identifier,
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.LOCAL,
        source_project_id=None,
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
    )


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
