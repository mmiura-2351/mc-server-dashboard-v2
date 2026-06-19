"""Model-rendered DDL for the ``mods`` table (migration 0019).

Verifies that ``mods`` carries the documented columns, constraints, CHECKs,
defaults, and the unique ``sha256_hash`` index so the model metadata matches the
hand-written migration.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, DefaultClause, String, Table

from mc_server_dashboard_api.servers.adapters.mod_models import ModModel

_MODS = cast(Table, ModModel.__table__)


def _check_constraints() -> dict[str, str]:
    return {
        cast(str, c.name): str(c.sqltext)
        for c in _MODS.constraints
        if isinstance(c, CheckConstraint)
    }


def test_mods_primary_key() -> None:
    pk_columns = {str(c.name) for c in _MODS.primary_key.columns}
    assert pk_columns == {"id"}


def test_mods_uploaded_by_has_no_foreign_key() -> None:
    assert _MODS.c.uploaded_by.foreign_keys == set()
    assert _MODS.c.uploaded_by.nullable is False


def test_mods_sha256_hash_length() -> None:
    assert cast(String, _MODS.c.sha256_hash.type).length == 64


def test_mods_sha512_hash_nullable() -> None:
    assert cast(String, _MODS.c.sha512_hash.type).length == 128
    assert _MODS.c.sha512_hash.nullable is True


def test_mods_size_bytes_not_nullable() -> None:
    assert _MODS.c.size_bytes.nullable is False


def test_mods_description_nullable() -> None:
    assert _MODS.c.description.nullable is True


def test_mods_source_ids_nullable() -> None:
    assert _MODS.c.source_project_id.nullable is True
    assert _MODS.c.source_version_id.nullable is True


def test_mods_side_default_both() -> None:
    server_default = _MODS.c.side.server_default
    assert isinstance(server_default, DefaultClause)
    assert server_default.arg == "both"


def test_mods_json_columns_not_nullable() -> None:
    assert _MODS.c.provides.nullable is False
    assert _MODS.c.mc_versions.nullable is False
    assert _MODS.c.dependencies.nullable is False


def test_mods_timestamps_not_nullable() -> None:
    assert _MODS.c.created_at.nullable is False
    assert _MODS.c.updated_at.nullable is False


def test_mods_loader_type_check() -> None:
    sqltext = _check_constraints()["ck_mods_loader_type"]
    for loader in ("fabric", "forge", "neoforge", "quilt", "paper"):
        assert loader in sqltext


def test_mods_side_check() -> None:
    sqltext = _check_constraints()["ck_mods_side"]
    for side in ("server", "client", "both"):
        assert side in sqltext


def test_mods_source_check() -> None:
    sqltext = _check_constraints()["ck_mods_source"]
    for source in ("local", "modrinth"):
        assert source in sqltext


def test_mods_sha256_hash_unique_index() -> None:
    index = next(i for i in _MODS.indexes if i.name == "uq_mods_sha256_hash")
    assert index.unique is True
    assert {c.name for c in index.columns} == {"sha256_hash"}
