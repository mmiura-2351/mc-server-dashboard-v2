"""Model-rendered DDL for the ``server_mods`` table (migration 0020).

Verifies that ``server_mods`` carries the documented columns, FKs, unique
constraint, index, and default so the model metadata matches the hand-written
migration.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import DefaultClause, Table, UniqueConstraint

from mc_server_dashboard_api.servers.adapters.server_mod_models import ServerModModel

_TABLE = cast(Table, ServerModModel.__table__)


def test_server_mods_primary_key() -> None:
    pk_columns = {str(c.name) for c in _TABLE.primary_key.columns}
    assert pk_columns == {"id"}


def test_server_mods_server_id_cascades() -> None:
    fks = list(_TABLE.c.server_id.foreign_keys)
    assert len(fks) == 1
    assert "server.id" in str(fks[0].target_fullname)
    assert fks[0].ondelete == "CASCADE"
    assert _TABLE.c.server_id.nullable is False


def test_server_mods_mod_id_references_mods() -> None:
    fks = list(_TABLE.c.mod_id.foreign_keys)
    assert len(fks) == 1
    assert "mods.id" in str(fks[0].target_fullname)
    assert _TABLE.c.mod_id.nullable is False


def test_server_mods_assigned_by_has_no_foreign_key() -> None:
    assert _TABLE.c.assigned_by.foreign_keys == set()
    assert _TABLE.c.assigned_by.nullable is False


def test_server_mods_enabled_default_true() -> None:
    server_default = _TABLE.c.enabled.server_default
    assert isinstance(server_default, DefaultClause)
    assert server_default.arg == "true"
    assert _TABLE.c.enabled.nullable is False


def test_server_mods_unique_server_mod() -> None:
    uniques = {
        tuple(sorted(col.name for col in c.columns))
        for c in _TABLE.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert ("mod_id", "server_id") in uniques


def test_server_mods_server_id_index() -> None:
    index = next(i for i in _TABLE.indexes if i.name == "ix_server_mods_server_id")
    assert {c.name for c in index.columns} == {"server_id"}


def test_server_mods_timestamps_not_nullable() -> None:
    assert _TABLE.c.created_at.nullable is False
    assert _TABLE.c.updated_at.nullable is False
