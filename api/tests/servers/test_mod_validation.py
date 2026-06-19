"""Unit tests for the phase-B mod-set validator (issue #1263).

The validator is a pure function over (server loader + MC version, assigned mods)
returning a checklist of findings. These tests cover each finding kind in
isolation -- the canonical missing-Fabric-API case, a data-supported conflict,
loader mismatch, MC mismatch -- plus a fully-valid set that yields nothing.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.application.mod_validation import (
    ModValidation,
    validate_mod_set,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId, ModLoader, ModSide

_NOW = dt.datetime(2026, 6, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


def _mod(
    *,
    mod_identifier: str,
    loader_type: ModLoader = "fabric",
    provides: list[str] | None = None,
    mc_versions: list[str] | None = None,
    dependencies: list[dict[str, object]] | None = None,
    side: ModSide = "both",
) -> Mod:
    return Mod(
        id=ModId.new(),
        filename=f"{mod_identifier}.jar",
        display_name=mod_identifier,
        description=None,
        loader_type=loader_type,
        mod_identifier=mod_identifier,
        provides=provides or [],
        version_number="1.0.0",
        mc_versions=mc_versions if mc_versions is not None else ["1.21"],
        side=side,
        dependencies=dependencies or [],
        sha256_hash="a" * 64,
        sha512_hash=None,
        size_bytes=10,
        source="local",
        source_project_id=None,
        source_version_id=None,
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
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
        """The canonical case: a fabric mod needs Fabric API and it is absent."""

        mod = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True, version_range=">=0.90.0")],
        )
        result = validate_mod_set(server_type="fabric", mc_version="1.21", mods=[mod])

        assert len(result.missing_deps) == 1
        finding = result.missing_deps[0]
        assert finding.mod_id == "sodium"
        assert finding.depends_on == "fabric-api"
        assert finding.version_range == ">=0.90.0"

    def test_required_dep_present_by_identifier(self) -> None:
        sodium = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True)],
        )
        fabric_api = _mod(mod_identifier="fabric-api")
        result = validate_mod_set(
            server_type="fabric", mc_version="1.21", mods=[sodium, fabric_api]
        )

        assert result.missing_deps == []

    def test_required_dep_satisfied_by_provides(self) -> None:
        """A dependency satisfied by another mod's ``provides`` is not missing."""

        sodium = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("fabric-api", required=True)],
        )
        # A single jar that bundles and *provides* fabric-api.
        all_in_one = _mod(mod_identifier="qfapi", provides=["fabric-api"])
        result = validate_mod_set(
            server_type="fabric", mc_version="1.21", mods=[sodium, all_in_one]
        )

        assert result.missing_deps == []

    def test_optional_dep_absent_is_not_flagged(self) -> None:
        mod = _mod(
            mod_identifier="sodium",
            dependencies=[_dep("modmenu", required=False)],
        )
        result = validate_mod_set(server_type="fabric", mc_version="1.21", mods=[mod])

        assert result.missing_deps == []


class TestConflicts:
    def test_marked_conflict_present_is_flagged(self) -> None:
        """A dependency entry marked as a conflict whose target is present."""

        # The parser does not yet emit break/conflict entries; a hand-built dep
        # with a ``conflict`` flag exercises the check the data will eventually
        # supply (see mod_validation docstring).
        mod_a = _mod(
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
        sodium = _mod(mod_identifier="sodium")
        result = validate_mod_set(
            server_type="fabric", mc_version="1.21", mods=[mod_a, sodium]
        )

        assert len(result.conflicts) == 1
        assert result.conflicts[0].mod_id == "optifabric"
        assert result.conflicts[0].conflicts_with == "sodium"

    def test_marked_conflict_absent_is_not_flagged(self) -> None:
        mod_a = _mod(
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
        result = validate_mod_set(server_type="fabric", mc_version="1.21", mods=[mod_a])

        assert result.conflicts == []


class TestLoaderMismatch:
    def test_forge_mod_on_fabric_server_is_flagged(self) -> None:
        forge_mod = _mod(mod_identifier="jei", loader_type="forge")
        result = validate_mod_set(
            server_type="fabric", mc_version="1.21", mods=[forge_mod]
        )

        assert len(result.loader_mismatch) == 1
        finding = result.loader_mismatch[0]
        assert finding.mod_id == "jei"
        assert finding.mod_loader == "forge"
        assert finding.server_loader == "fabric"

    def test_quilt_mod_on_fabric_server_is_compatible(self) -> None:
        quilt_mod = _mod(mod_identifier="qmod", loader_type="quilt")
        result = validate_mod_set(
            server_type="fabric", mc_version="1.21", mods=[quilt_mod]
        )

        assert result.loader_mismatch == []

    def test_neoforge_mod_on_forge_server_is_compatible(self) -> None:
        neo = _mod(mod_identifier="neomod", loader_type="neoforge")
        result = validate_mod_set(server_type="forge", mc_version="1.21", mods=[neo])

        assert result.loader_mismatch == []

    def test_paper_plugin_on_spigot_server_is_compatible(self) -> None:
        plugin = _mod(mod_identifier="essentials", loader_type="paper", side="server")
        result = validate_mod_set(
            server_type="spigot", mc_version="1.21", mods=[plugin]
        )

        assert result.loader_mismatch == []

    def test_any_mod_on_vanilla_server_is_flagged(self) -> None:
        mod = _mod(mod_identifier="sodium", loader_type="fabric")
        result = validate_mod_set(server_type="vanilla", mc_version="1.21", mods=[mod])

        assert len(result.loader_mismatch) == 1
        assert result.loader_mismatch[0].server_loader == "vanilla"


class TestMcMismatch:
    def test_version_not_listed_is_flagged(self) -> None:
        mod = _mod(mod_identifier="sodium", mc_versions=["1.20.4"])
        result = validate_mod_set(server_type="fabric", mc_version="1.21", mods=[mod])

        assert len(result.mc_mismatch) == 1
        finding = result.mc_mismatch[0]
        assert finding.mod_id == "sodium"
        assert finding.mod_mc_versions == ["1.20.4"]
        assert finding.server_mc_version == "1.21"

    def test_version_listed_is_ok(self) -> None:
        mod = _mod(mod_identifier="sodium", mc_versions=["1.20.4", "1.21"])
        result = validate_mod_set(server_type="fabric", mc_version="1.21", mods=[mod])

        assert result.mc_mismatch == []

    def test_unconstrained_mod_is_never_flagged(self) -> None:
        """A mod declaring no MC versions is treated as compatible everywhere."""

        mod = _mod(mod_identifier="sodium", mc_versions=[])
        result = validate_mod_set(server_type="fabric", mc_version="1.21", mods=[mod])

        assert result.mc_mismatch == []


class TestFullyValidSet:
    def test_no_findings(self) -> None:
        sodium = _mod(
            mod_identifier="sodium",
            mc_versions=["1.21"],
            dependencies=[_dep("fabric-api", required=True)],
        )
        fabric_api = _mod(mod_identifier="fabric-api", mc_versions=["1.21"])
        result = validate_mod_set(
            server_type="fabric", mc_version="1.21", mods=[sodium, fabric_api]
        )

        assert result == ModValidation()

    def test_empty_set_is_valid(self) -> None:
        result = validate_mod_set(server_type="fabric", mc_version="1.21", mods=[])

        assert result == ModValidation()
