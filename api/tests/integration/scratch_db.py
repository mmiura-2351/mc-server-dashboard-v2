"""Per-run scratch database for the DB-gated integration tests (issue #379).

``MCD_TEST_DATABASE_URL`` is treated as a *maintenance/base* connection, not the
database the tests run against. Before collection, the test session creates a
fresh ``<dbname>_<short-uuid>`` database off that base URL, points
``MCD_TEST_DATABASE_URL`` at it, and drops it on teardown. Two sessions sharing
one base URL therefore get disjoint databases, so the ``downgrade base`` /
``upgrade head`` dance in the fixtures can no longer race or leave orphan tables
behind (the failure mode described in the issue).

CI is unaffected: it provisions a fresh Postgres service per run, and the per-run
database derived from that service's URL is just as fresh.

``CREATE DATABASE`` / ``DROP DATABASE`` cannot run inside a transaction, so we
talk to the cluster's ``postgres`` maintenance database over a raw ``asyncpg``
connection (which autocommits each statement). These helpers are driven from the
synchronous pytest session hooks, where no event loop is running, so they wrap
the asyncpg coroutines in ``asyncio.run``.
"""

from __future__ import annotations

import asyncio

import asyncpg
from sqlalchemy.engine.url import make_url


def derive_scratch_url(base_url: str, token: str) -> str:
    """Return ``base_url`` with its database name suffixed by ``_<token>``."""
    url = make_url(base_url)
    return url.set(database=f"{url.database}_{token}").render_as_string(
        hide_password=False
    )


def _asyncpg_dsn(url_str: str, *, database: str) -> str:
    """A libpq DSN for ``database`` on the cluster ``url_str`` points at."""
    url = make_url(url_str).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


async def _create(base_url: str, name: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(base_url, database="postgres"))
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop(base_url: str, name: str) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(base_url, database="postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


def create_scratch_database(base_url: str, scratch_url: str) -> None:
    """Create the scratch database named in ``scratch_url``."""
    name = make_url(scratch_url).database
    assert name is not None
    asyncio.run(_create(base_url, name))


def drop_scratch_database(base_url: str, scratch_url: str) -> None:
    """Drop the scratch database, evicting any lingering backends first.

    Best-effort: teardown must not mask a test failure, so connection or drop
    errors are swallowed. ``WITH (FORCE)`` (PostgreSQL 13+) evicts open
    connections so an aborted async cleanup cannot pin the database open.
    """
    name = make_url(scratch_url).database
    assert name is not None
    try:
        asyncio.run(_drop(base_url, name))
    except (asyncpg.PostgresError, OSError):
        pass
