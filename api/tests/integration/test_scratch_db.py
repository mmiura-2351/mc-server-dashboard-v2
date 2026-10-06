"""Unit tests for the per-run scratch-database helpers (issue #379).

The URL-derivation tests do not touch a real database; they pin the pure logic
that makes the integration fixture concurrency-safe. The create/drop round-trip
against a live Postgres is exercised implicitly by every DB-gated integration
test under this package; the one property of the created database pinned here is
that its sessions do not wait for the WAL flush on commit (issue #3257).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.scratch_db import derive_scratch_url

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")


def test_derive_scratch_url_suffixes_the_database_name() -> None:
    base = "postgresql+asyncpg://mcsd:mcsd@localhost:5432/mcsd_test"
    scratch = derive_scratch_url(base, "abc123")
    assert scratch == "postgresql+asyncpg://mcsd:mcsd@localhost:5432/mcsd_test_abc123"


def test_derive_scratch_url_preserves_driver_and_credentials() -> None:
    base = "postgresql+asyncpg://user:p%40ss@db.example:6543/app"
    scratch = derive_scratch_url(base, "xy")
    assert scratch.startswith("postgresql+asyncpg://user:")
    assert scratch.endswith("/app_xy")


def test_derive_scratch_url_distinct_tokens_yield_distinct_names() -> None:
    base = "postgresql+asyncpg://mcsd:mcsd@localhost/mcsd_test"
    assert derive_scratch_url(base, "aaa") != derive_scratch_url(base, "bbb")


@pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)
async def test_scratch_database_sessions_do_not_wait_for_the_wal_flush() -> None:
    # A plain engine with no per-connection settings, as the fixtures and the
    # Alembic environment build theirs: the setting must come from the database.
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            setting = (await conn.execute(text("SHOW synchronous_commit"))).scalar()
    finally:
        await engine.dispose()
    assert setting == "off"
