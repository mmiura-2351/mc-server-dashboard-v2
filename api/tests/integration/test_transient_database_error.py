"""A full database, as SQLAlchemy over asyncpg really reports it (#3995).

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). It checks the shape the unit tests' ``server_error`` fake builds
against what the driver really raises.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
import sqlalchemy.exc
from sqlalchemy import make_url, text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi

from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    is_transient_database_error,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


async def test_a_database_with_no_connection_slot_left_is_transient() -> None:
    # Taking every one of the cluster's connections would starve the other
    # tests that share it, so a role with no connection slot stands in: the
    # server refuses it with the SQLSTATE of "too many clients", 53300.
    assert _DB_URL is not None
    role = f"mcsd_full_{uuid.uuid4().hex}"
    admin = create_engine(_DB_URL)
    try:
        async with admin.begin() as conn:
            await conn.execute(
                text(f"CREATE ROLE {role} LOGIN PASSWORD 'full' CONNECTION LIMIT 0")
            )
        full = create_engine(
            make_url(_DB_URL)
            .set(username=role, password="full")
            .render_as_string(hide_password=False)
        )
        try:
            with pytest.raises(sqlalchemy.exc.DBAPIError) as caught:
                async with full.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        finally:
            await full.dispose()
    finally:
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()

    orig = caught.value.orig
    assert type(caught.value) is sqlalchemy.exc.DBAPIError
    assert caught.value.connection_invalidated is False
    assert type(orig) is AsyncAdapt_asyncpg_dbapi.Error
    assert orig.sqlstate == "53300"
    assert type(orig.__cause__) is asyncpg.TooManyConnectionsError
    assert is_transient_database_error(caught.value) is True
