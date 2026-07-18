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

``test_metadata_constraint_names_exist_in_db`` is the autogenerate-quiet guard
for the naming convention (issue #60): every constraint/index name the ORM
models render on ``Base.metadata`` must already exist in the migrated database,
so an Alembic autogenerate would see no spurious rename. It checks one direction
only -- names the models declare must exist in the DB; it does not assert the
reverse, because a migration-only construct the models do not declare is a
separate concern (a spurious *drop*, not a rename).

``test_revision_id_length`` guards against revision IDs exceeding Alembic's
``varchar(32)`` ceiling for ``alembic_version.version_num`` (#2069). This is a
pure filesystem check and runs without a database.

Database-dependent tests run only when ``MCD_TEST_DATABASE_URL`` is set (the CI
Postgres service); skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

_needs_db = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

# Alembic's ``alembic_version.version_num`` column is ``varchar(32)``. A
# revision ID exceeding this length silently passes every non-Postgres check
# and fails only on a real ``upgrade head`` (#2069).
_ALEMBIC_VERSION_NUM_MAX = 32

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


def _metadata_names() -> dict[str, set[str]]:
    """Constraint and index names the ORM models render, keyed by table.

    Imports the same registration path ``env.py`` uses so ``Base.metadata`` is
    fully populated; this is exactly the metadata Alembic autogenerate diffs.
    """

    sys.path.insert(0, str(_MIGRATIONS_DIR))
    from model_registry import target_metadata

    names: dict[str, set[str]] = {}
    for table in target_metadata.tables.values():
        table_names = {c.name for c in table.constraints if isinstance(c.name, str)}
        table_names |= {i.name for i in table.indexes if isinstance(i.name, str)}
        names[table.name] = table_names
    return names


def _db_names(sync_conn: Connection) -> dict[str, set[str]]:
    """All constraint and index names present in the live database, by table."""

    inspector = inspect(sync_conn)
    names: dict[str, set[str]] = {}
    for table in inspector.get_table_names():
        if table in _ALEMBIC_BOOKKEEPING:
            continue
        reflected: list[Mapping[str, object]] = [
            inspector.get_pk_constraint(table),
            *inspector.get_foreign_keys(table),
            *inspector.get_unique_constraints(table),
            *inspector.get_check_constraints(table),
            *inspector.get_indexes(table),
        ]
        names[table] = {
            name for entry in reflected if isinstance(name := entry.get("name"), str)
        }
    return names


@_needs_db
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


@_needs_db
async def test_metadata_constraint_names_exist_in_db() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            db_names = await conn.run_sync(_db_names)
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)

    metadata_names = _metadata_names()

    mismatches: dict[str, set[str]] = {}
    for table, names in metadata_names.items():
        missing = names - db_names.get(table, set())
        if missing:
            mismatches[table] = missing
    assert not mismatches, (
        "constraint/index names rendered by the ORM models that the migrated "
        "database does not have -- the naming convention diverges from the "
        f"migration-created names (autogenerate would emit a rename): {mismatches}"
    )


def test_revision_id_length() -> None:
    """Reject migration filenames whose stem exceeds varchar(32) (#2069)."""

    versions_dir = _MIGRATIONS_DIR / "versions"
    too_long = {
        path.stem: len(path.stem)
        for path in versions_dir.glob("*.py")
        if len(path.stem) > _ALEMBIC_VERSION_NUM_MAX
    }
    assert not too_long, (
        f"migration revision IDs exceed Alembic's varchar(32) ceiling for "
        f"alembic_version.version_num — rename to <= {_ALEMBIC_VERSION_NUM_MAX} "
        f"characters: {too_long}"
    )
