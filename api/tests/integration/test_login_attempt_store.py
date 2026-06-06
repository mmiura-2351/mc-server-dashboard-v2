"""Integration tests for the LoginAttemptStore adapter + migration on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The 0003 migration creates/teardowns
the ``login_attempt`` / ``account_lockout`` tables, so the adapter runs against
the documented shape (SECURITY.md Section 3).
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.login_attempt_store import (
    SqlAlchemyLoginAttemptStore,
)
from mc_server_dashboard_api.identity.application.prune_login_attempts import (
    PruneLoginAttempts,
)
from tests.identity.fakes import FakeClock, make_brute_force_config
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


def _store(engine: AsyncEngine) -> SqlAlchemyLoginAttemptStore:
    return SqlAlchemyLoginAttemptStore(create_session_factory(engine))


async def _record(
    store: SqlAlchemyLoginAttemptStore,
    *,
    username: str,
    ip: str | None,
    success: bool,
    at: dt.datetime,
    failure_reason: str | None = None,
) -> None:
    await store.record_attempt(
        username=username,
        ip=ip,
        success=success,
        failure_reason=failure_reason,
        at=at,
    )


async def test_migration_creates_tables_and_indexes(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert {"login_attempt", "account_lockout"} <= tables

        indexes = await conn.run_sync(
            lambda c: {ix["name"] for ix in inspect(c).get_indexes("login_attempt")}
        )
        columns = await conn.run_sync(
            lambda c: {col["name"] for col in inspect(c).get_columns("login_attempt")}
        )
    assert "ix_login_attempt_username_created_at" in indexes
    assert "ix_login_attempt_ip_created_at" in indexes
    # SECURITY.md Section 3: the attempt row carries the failure reason.
    assert "failure_reason" in columns


async def test_count_username_failures_within_window(engine: AsyncEngine) -> None:
    store = _store(engine)
    await _record(store, username="alice", ip="10.0.0.1", success=False, at=_NOW)
    await _record(store, username="alice", ip="10.0.0.1", success=False, at=_NOW)
    # A success and an out-of-window failure must not be counted.
    await _record(store, username="alice", ip="10.0.0.1", success=True, at=_NOW)
    await _record(
        store,
        username="alice",
        ip="10.0.0.1",
        success=False,
        at=_NOW - dt.timedelta(hours=1),
    )

    count = await store.count_username_failures(
        "alice", since=_NOW - dt.timedelta(minutes=15)
    )
    assert count == 2


async def test_count_ip_failures_within_window(engine: AsyncEngine) -> None:
    store = _store(engine)
    await _record(store, username="alice", ip="10.0.0.2", success=False, at=_NOW)
    await _record(store, username="bob", ip="10.0.0.2", success=False, at=_NOW)
    await _record(store, username="carol", ip="10.0.0.3", success=False, at=_NOW)

    count = await store.count_ip_failures(
        "10.0.0.2", since=_NOW - dt.timedelta(minutes=5)
    )
    assert count == 2


async def test_count_ip_registrations_within_window(engine: AsyncEngine) -> None:
    store = _store(engine)
    await store.record_registration(ip="10.0.0.9", at=_NOW)
    await store.record_registration(ip="10.0.0.9", at=_NOW)
    await store.record_registration(ip="10.0.0.8", at=_NOW)
    # An out-of-window registration must not be counted.
    await store.record_registration(ip="10.0.0.9", at=_NOW - dt.timedelta(hours=2))

    count = await store.count_ip_registrations(
        "10.0.0.9", since=_NOW - dt.timedelta(hours=1)
    )
    assert count == 2


async def test_registration_rows_isolated_from_login_failure_counts(
    engine: AsyncEngine,
) -> None:
    # Registration rows share the table but must never feed the login per-IP
    # failure count, and login failures must never feed the registration count
    # (issue #362).
    store = _store(engine)
    await store.record_registration(ip="10.0.0.7", at=_NOW)
    await _record(store, username="alice", ip="10.0.0.7", success=False, at=_NOW)

    ip_failures = await store.count_ip_failures(
        "10.0.0.7", since=_NOW - dt.timedelta(hours=1)
    )
    registrations = await store.count_ip_registrations(
        "10.0.0.7", since=_NOW - dt.timedelta(hours=1)
    )
    assert ip_failures == 1
    assert registrations == 1


async def test_lock_upserts_and_get_reads_back(engine: AsyncEngine) -> None:
    store = _store(engine)
    until = _NOW + dt.timedelta(minutes=15)
    await store.lock("alice", locked_until=until, lockout_count=1)

    lockout = await store.get_lockout("alice")
    assert lockout is not None
    assert lockout.locked_until == until
    assert lockout.lockout_count == 1

    # Re-locking the same account updates the single row (one row per username).
    later = _NOW + dt.timedelta(minutes=60)
    await store.lock("alice", locked_until=later, lockout_count=3)
    lockout = await store.get_lockout("alice")
    assert lockout is not None
    assert lockout.locked_until == later
    assert lockout.lockout_count == 3


async def test_clear_lockout_removes_row(engine: AsyncEngine) -> None:
    store = _store(engine)
    await store.lock(
        "alice", locked_until=_NOW + dt.timedelta(minutes=15), lockout_count=1
    )
    await store.clear_lockout("alice")
    assert await store.get_lockout("alice") is None


async def test_prune_deletes_only_old_attempts(engine: AsyncEngine) -> None:
    store = _store(engine)
    await _record(store, username="alice", ip="10.0.0.1", success=False, at=_NOW)
    await _record(
        store,
        username="alice",
        ip="10.0.0.1",
        success=False,
        at=_NOW - dt.timedelta(days=2),
    )

    await store.prune_attempts(older_than=_NOW - dt.timedelta(days=1))

    # The recent attempt survives; the old one is gone.
    recent = await store.count_username_failures(
        "alice", since=_NOW - dt.timedelta(minutes=15)
    )
    assert recent == 1
    everything = await store.count_username_failures(
        "alice", since=_NOW - dt.timedelta(days=10)
    )
    assert everything == 1


async def test_missing_lockout_returns_none(engine: AsyncEngine) -> None:
    store = _store(engine)
    assert await store.get_lockout("nobody") is None


async def test_prune_loop_tick_drops_old_keeps_in_window(engine: AsyncEngine) -> None:
    # The periodic prune use case, driven by a faked clock against the real store:
    # one tick deletes rows past the longest window and keeps in-window rows
    # (SECURITY.md Section 3), with no login event involved.
    store = _store(engine)
    bf = make_brute_force_config()  # longest window: 15 minutes (username).
    await _record(
        store,
        username="alice",
        ip="10.0.0.1",
        success=False,
        at=_NOW - dt.timedelta(minutes=20),
    )
    await _record(
        store,
        username="alice",
        ip="10.0.0.1",
        success=False,
        at=_NOW - dt.timedelta(minutes=1),
    )

    pruner = PruneLoginAttempts(attempts=store, brute_force=bf, clock=FakeClock(_NOW))
    await pruner.tick()

    everything = await store.count_username_failures(
        "alice", since=_NOW - dt.timedelta(days=10)
    )
    assert everything == 1
