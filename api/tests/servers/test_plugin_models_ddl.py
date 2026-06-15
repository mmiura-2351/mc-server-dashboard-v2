"""Model-rendered DDL for the ``server_plugin`` table (migration 0018).

Verifies the table carries the documented columns, the ``loader_type`` and
``source`` CHECK enums, the ``(server_id, rel_path)`` unique constraint, the
cascade FK on ``server_id``, and the FK-free soft-reference ``installed_by``.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Index, Table, UniqueConstraint

from mc_server_dashboard_api.servers.adapters.plugin_models import ServerPluginModel

_TABLE = cast(Table, ServerPluginModel.__table__)


def test_loader_type_check_lists_documented_values() -> None:
    checks = {
        c.name: str(c.sqltext)
        for c in _TABLE.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_server_plugin_loader_type" in checks
    for value in ("mod", "plugin"):
        assert value in checks["ck_server_plugin_loader_type"]


def test_source_check_lists_documented_values() -> None:
    checks = {
        c.name: str(c.sqltext)
        for c in _TABLE.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_server_plugin_source" in checks
    for value in ("local", "modrinth"):
        assert value in checks["ck_server_plugin_source"]


def test_server_id_cascade_delete() -> None:
    fks = list(_TABLE.c.server_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"


def test_installed_by_has_no_foreign_key() -> None:
    assert _TABLE.c.installed_by.foreign_keys == set()
    assert _TABLE.c.installed_by.nullable is True


def test_unique_constraint_server_rel_path() -> None:
    uqs = {
        c.name
        for c in _TABLE.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_server_plugin_server_rel" in uqs


def test_server_id_index_present() -> None:
    indexes = {i.name for i in _TABLE.indexes if isinstance(i, Index)}
    assert "ix_server_plugin_server_id" in indexes


def test_size_bytes_is_nullable() -> None:
    assert _TABLE.c.size_bytes.nullable is True


def test_enabled_default_true() -> None:
    assert _TABLE.c.enabled.server_default is not None
