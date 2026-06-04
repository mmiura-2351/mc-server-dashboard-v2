"""Migrations and ``Base.metadata`` must describe the same tables (issue #130).

``migrations/env.py`` imports each adapters model module so its tables register
on the shared ``Base.metadata`` that Alembic autogenerate diffs against. If a
module is forgotten (as ``backup_models`` was), the table exists in the database
after ``upgrade head`` but is absent from ``Base.metadata``, so autogenerate
would emit a spurious drop. This test catches that gap class structurally by
comparing the tables created by migrations at head against the metadata keys.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from mc_server_dashboard_api.core.adapters.database import Base
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

# Alembic's own bookkeeping table is created by the migration runner, not by an
# ORM model, so it is never part of ``Base.metadata``.
_ALEMBIC_BOOKKEEPING = {"alembic_version"}


async def test_head_tables_match_base_metadata() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    # ``upgrade_head`` runs Alembic, which imports ``migrations/env.py`` in this
    # process; that import registers each adapters model module on the shared
    # ``Base.metadata`` examined below.
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            db_tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)

    db_tables -= _ALEMBIC_BOOKKEEPING
    metadata_tables = set(Base.metadata.tables)

    missing_from_metadata = db_tables - metadata_tables
    extra_in_metadata = metadata_tables - db_tables
    assert not missing_from_metadata, (
        "tables created by migrations but absent from Base.metadata "
        f"(model module not imported in migrations/env.py?): {missing_from_metadata}"
    )
    assert not extra_in_metadata, (
        f"tables on Base.metadata with no corresponding migration: {extra_in_metadata}"
    )
