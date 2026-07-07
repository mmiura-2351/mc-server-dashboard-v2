"""Model-rendered DDL for the resource pack tables (migration 0018).

Verifies that ``resource_packs`` and ``server_resource_pack_assignments`` carry
the documented columns, constraints, FKs, and defaults so the model metadata
matches the hand-written migration.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import DefaultClause, String, Table

from mc_server_dashboard_api.servers.adapters.resource_pack_models import (
    ResourcePackModel,
    ServerResourcePackAssignmentModel,
)

_PACKS = cast(Table, ResourcePackModel.__table__)
_ASSIGNMENTS = cast(Table, ServerResourcePackAssignmentModel.__table__)


# -- resource_packs table --


def test_resource_packs_primary_key() -> None:
    pk_columns = {str(c.name) for c in _PACKS.primary_key.columns}
    assert pk_columns == {"id"}


def test_resource_packs_uploaded_by_has_no_foreign_key() -> None:
    assert _PACKS.c.uploaded_by.foreign_keys == set()
    assert _PACKS.c.uploaded_by.nullable is False


def test_resource_packs_sha1_hash_length() -> None:
    assert cast(String, _PACKS.c.sha1_hash.type).length == 40


def test_resource_packs_sha256_hash_length() -> None:
    assert cast(String, _PACKS.c.sha256_hash.type).length == 64


def test_resource_packs_size_bytes_not_nullable() -> None:
    assert _PACKS.c.size_bytes.nullable is False


def test_resource_packs_description_nullable() -> None:
    assert _PACKS.c.description.nullable is True


def test_resource_packs_timestamps_not_nullable() -> None:
    assert _PACKS.c.created_at.nullable is False
    assert _PACKS.c.updated_at.nullable is False


# -- server_resource_pack_assignments table --


def test_assignments_primary_key_is_server_id() -> None:
    pk_columns = {str(c.name) for c in _ASSIGNMENTS.primary_key.columns}
    assert pk_columns == {"server_id"}


def test_assignments_server_id_cascade_delete() -> None:
    fks = list(_ASSIGNMENTS.c.server_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"


def test_assignments_resource_pack_id_foreign_key() -> None:
    fks = list(_ASSIGNMENTS.c.resource_pack_id.foreign_keys)
    assert len(fks) == 1
    assert "resource_packs.id" in str(fks[0].target_fullname)


def test_assignments_require_resource_pack_default_false() -> None:
    server_default = _ASSIGNMENTS.c.require_resource_pack.server_default
    assert isinstance(server_default, DefaultClause)
    assert server_default.arg == "false"


def test_assignments_resource_pack_prompt_nullable() -> None:
    assert _ASSIGNMENTS.c.resource_pack_prompt.nullable is True


def test_assignments_assigned_by_has_no_foreign_key() -> None:
    assert _ASSIGNMENTS.c.assigned_by.foreign_keys == set()
    assert _ASSIGNMENTS.c.assigned_by.nullable is False


def test_assignments_timestamps_not_nullable() -> None:
    assert _ASSIGNMENTS.c.created_at.nullable is False
    assert _ASSIGNMENTS.c.updated_at.nullable is False


def test_assignments_resource_pack_id_index() -> None:
    index_names = {idx.name for idx in _ASSIGNMENTS.indexes}
    assert "ix_srv_rp_assignments_resource_pack_id" in index_names
