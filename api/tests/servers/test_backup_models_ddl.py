"""Model-rendered DDL for the ``backup`` table (DATABASE.md Section 8).

Verifies the table carries the documented columns, the ``source`` CHECK enum, the
``(server_id, created_at)`` index, the cascade FK on ``server_id``, and the
FK-free soft-reference ``created_by``.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Index, Table

from mc_server_dashboard_api.servers.adapters.backup_models import BackupModel

_TABLE = cast(Table, BackupModel.__table__)


def test_source_check_lists_documented_values() -> None:
    checks = {
        c.name: str(c.sqltext)
        for c in _TABLE.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_backup_source" in checks
    for value in ("manual", "scheduled", "event", "uploaded"):
        assert value in checks["ck_backup_source"]


def test_server_id_cascade_delete() -> None:
    fks = list(_TABLE.c.server_id.foreign_keys)
    assert len(fks) == 1
    assert fks[0].ondelete == "CASCADE"


def test_created_by_has_no_foreign_key() -> None:
    # Soft reference: the row survives the actor's deletion (Section 9).
    assert _TABLE.c.created_by.foreign_keys == set()
    assert _TABLE.c.created_by.nullable is True


def test_listing_index_present() -> None:
    indexes = {i.name for i in _TABLE.indexes if isinstance(i, Index)}
    assert "ix_backup_server_id_created_at" in indexes


def test_size_bytes_is_nullable() -> None:
    assert _TABLE.c.size_bytes.nullable is True
