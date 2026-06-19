"""Domain entity unit tests for the mod library module.

Tests the value objects and dataclass: ``ModId`` and ``Mod``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.domain.mod import Mod, ModId


def test_mod_id_new_generates_unique_ids() -> None:
    a = ModId.new()
    b = ModId.new()
    assert a != b
    assert isinstance(a.value, uuid.UUID)


def test_mod_id_wraps_uuid() -> None:
    raw = uuid.uuid4()
    mod_id = ModId(raw)
    assert mod_id.value is raw


def test_mod_id_is_frozen() -> None:
    mod_id = ModId.new()
    try:
        mod_id.value = uuid.uuid4()  # type: ignore[misc]
        assert False, "should be frozen"
    except AttributeError:
        pass


def _make_mod(**overrides: object) -> Mod:
    now = dt.datetime.now(dt.timezone.utc)
    defaults: dict[str, object] = {
        "id": ModId.new(),
        "filename": "sodium.jar",
        "display_name": "Sodium",
        "description": "Rendering optimization",
        "loader_type": "fabric",
        "mod_identifier": "sodium",
        "provides": [],
        "version_number": "0.5.0",
        "mc_versions": ["1.21", "1.21.1"],
        "side": "both",
        "dependencies": [
            {"mod_identifier": "fabric", "version_range": "*", "required": True}
        ],
        "sha256_hash": "a" * 64,
        "sha512_hash": None,
        "size_bytes": 2048,
        "source": "local",
        "source_project_id": None,
        "source_version_id": None,
        "uploaded_by": uuid.uuid4(),
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return Mod(**defaults)  # type: ignore[arg-type]


def test_mod_fields() -> None:
    mod = _make_mod(filename="lithium.jar", display_name="Lithium")
    assert mod.filename == "lithium.jar"
    assert mod.display_name == "Lithium"
    assert mod.loader_type == "fabric"
    assert mod.side == "both"
    assert mod.source == "local"


def test_mod_description_nullable() -> None:
    mod = _make_mod(description=None)
    assert mod.description is None


def test_mod_sha512_and_source_ids_nullable() -> None:
    mod = _make_mod(sha512_hash=None, source_project_id=None, source_version_id=None)
    assert mod.sha512_hash is None
    assert mod.source_project_id is None
    assert mod.source_version_id is None


def test_mod_modrinth_source_carries_ids() -> None:
    mod = _make_mod(
        source="modrinth",
        sha512_hash="b" * 128,
        source_project_id="AANobbMI",
        source_version_id="vXyZ1234",
    )
    assert mod.source == "modrinth"
    assert mod.sha512_hash == "b" * 128
    assert mod.source_project_id == "AANobbMI"
    assert mod.source_version_id == "vXyZ1234"


def test_mod_collections_carry_parsed_metadata() -> None:
    mod = _make_mod(
        provides=["fabric-api"],
        mc_versions=["1.20.4"],
        dependencies=[
            {"mod_identifier": "fabric", "version_range": ">=0.92", "required": True}
        ],
    )
    assert mod.provides == ["fabric-api"]
    assert mod.mc_versions == ["1.20.4"]
    assert mod.dependencies[0]["mod_identifier"] == "fabric"


def test_mod_is_mutable() -> None:
    mod = _make_mod()
    new_name = "Updated Mod"
    mod.display_name = new_name
    assert mod.display_name == new_name
