"""Migrations and ``migrations/model_registry`` must describe the same tables (#130).

``migrations/env.py`` imports ``migrations/model_registry``, which imports each
adapters model module so its tables register on the shared ``Base.metadata`` that
Alembic autogenerate diffs against. If a module is forgotten (as ``backup_models``
was), the table exists in the database after ``upgrade head`` but is absent from
the registry, so autogenerate would emit a spurious drop.

The registry tables must be read in a *subprocess* that imports ONLY
``model_registry`` -- not the app, not this test package's conftest. ``Base.metadata``
is a process-global: many test modules import the app (which imports every model
module), so in this process it is fully populated regardless of what ``env.py``
registers. Asserting against it in-process would pass vacuously even if the
``env.py`` fix were reverted. The subprocess imports the registration path in
isolation, so the comparison reflects what migrations actually pull in.

The database half (the tables migrations create at head) stays in-process.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

# Alembic's own bookkeeping table is created by the migration runner, not by an
# ORM model, so it is never part of the registry's metadata.
_ALEMBIC_BOOKKEEPING = {"alembic_version"}

# Directory holding ``model_registry`` (alongside ``env.py``), added to the
# subprocess's path so it can be imported standalone -- exactly as ``env.py``
# imports it (``prepend_sys_path = src:migrations`` in alembic.ini).
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# Snippet run in a fresh interpreter: import ONLY the registration path, then
# print the tables it registered. No app import, no test conftest.
_REGISTRY_SNIPPET = (
    "import sys; "
    f"sys.path.insert(0, {str(_MIGRATIONS_DIR)!r}); "
    "from model_registry import target_metadata; "
    "print('\\n'.join(sorted(target_metadata.tables)))"
)


def _registry_tables() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", _REGISTRY_SNIPPET],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


async def test_head_tables_match_registry() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
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
    registry_tables = _registry_tables()

    missing_from_registry = db_tables - registry_tables
    extra_in_registry = registry_tables - db_tables
    assert not missing_from_registry, (
        "tables created by migrations but absent from migrations/model_registry "
        f"(model module not imported there?): {missing_from_registry}"
    )
    assert not extra_in_registry, (
        f"tables in migrations/model_registry with no corresponding migration: "
        f"{extra_in_registry}"
    )
