"""The statement bound against a real PostgreSQL (issue #4042).

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). A statement that outlasts the bound fails as an outage and is
cancelled on the server too. A connection that goes silent fails typed wherever
the engine meets it. A cancel that someone else sends is not an outage.
Migrations are not held to the bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

import pytest
import sqlalchemy.exc
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.core.adapters.database import (
    DatabaseTimeoutError,
    create_engine,
    is_transient_database_error,
)
from tests.core.fakes import first_failure
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

# Longer than the stalled-server guard, so a statement left unbounded fails the
# test at the guard rather than finishing.
_SLEEP = "SELECT pg_sleep(30)"


async def _sleepers(engine: AsyncEngine) -> object:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE query = :sleep"
                    " AND state = 'active' AND datname = current_database()"
                ),
                {"sleep": _SLEEP},
            )
        ).scalar_one()


async def _sleep(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text(_SLEEP))


async def test_a_statement_past_the_bound_is_cancelled_as_an_outage() -> None:
    # The driver waits up to the bound again for the server to confirm the
    # cancel, then gives up on the connection and the cancel with it, so the
    # bound leaves room for a cancel's round trip on a loaded host.
    assert _DB_URL is not None
    engine = create_engine(_DB_URL, statement_timeout=1.0)
    try:
        error, elapsed = await first_failure(lambda: _sleep(engine))
        # The driver sent the server a cancel, and the connection waited for it
        # to land before going back to the pool.
        assert await _sleepers(engine) == 0
    finally:
        await engine.dispose()

    assert elapsed < 2.5
    assert isinstance(error, DatabaseTimeoutError)
    assert is_transient_database_error(error) is True


class _StallableProxy:
    """Forwards connections to the database until ``stalled`` is set, and then
    forwards nothing more, as a frozen server answers nothing."""

    def __init__(self, host: str, port: int) -> None:
        self.stalled = False
        self._target = (host, port)
        self.writers: list[asyncio.StreamWriter] = []

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while data := await reader.read(65536):
                if not self.stalled:
                    writer.write(data)
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def serve(
        self, client: asyncio.StreamReader, to_client: asyncio.StreamWriter
    ) -> None:
        upstream, to_upstream = await asyncio.open_connection(*self._target)
        self.writers += [to_client, to_upstream]
        await asyncio.gather(
            self._pump(client, to_upstream), self._pump(upstream, to_client)
        )


@contextlib.asynccontextmanager
async def _stallable(url: str) -> AsyncIterator[tuple[str, _StallableProxy]]:
    target = make_url(url)
    assert target.host is not None and target.port is not None
    proxy = _StallableProxy(target.host, target.port)
    server = await asyncio.start_server(proxy.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield (
            target.set(host="127.0.0.1", port=port).render_as_string(
                hide_password=False
            ),
            proxy,
        )
    finally:
        server.close()
        for writer in proxy.writers:
            writer.close()
        await server.wait_closed()


@pytest.mark.parametrize("where", ["pre-ping", "statement", "commit"])
async def test_a_connection_that_went_silent_fails_typed_wherever_it_is_met(
    where: str,
) -> None:
    # The engine meets a hung connection in three places: the pool's ping before
    # it hands out a pooled connection, a statement, and the commit. Each raises
    # the driver's bound typed, not as the bare ``TimeoutError`` a store or a
    # Worker raises too.
    assert _DB_URL is not None
    async with _stallable(_DB_URL) as (url, proxy):
        engine = create_engine(url, statement_timeout=0.3)
        try:
            # A pooled connection, set up while the server still answers.
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            with pytest.raises(DatabaseTimeoutError) as caught:
                async with asyncio.timeout(10):
                    if where == "pre-ping":
                        proxy.stalled = True
                        async with engine.connect():
                            pass
                    async with engine.begin() as conn:
                        await conn.execute(text("SELECT 1"))
                        proxy.stalled = True
                        if where == "statement":
                            await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    assert isinstance(caught.value.__cause__, TimeoutError)
    assert is_transient_database_error(caught.value) is True


async def _cancel_when_asleep(engine: AsyncEngine, pid: int) -> None:
    while True:
        async with engine.connect() as conn:
            cancelled = (
                await conn.execute(
                    text(
                        "SELECT pg_cancel_backend(pid) FROM pg_stat_activity"
                        " WHERE pid = :pid AND query = :sleep AND state = 'active'"
                    ),
                    {"pid": pid, "sleep": _SLEEP},
                )
            ).scalar()
        if cancelled:
            return
        await asyncio.sleep(0.05)


async def test_a_statement_someone_cancelled_is_not_an_outage() -> None:
    # Decision for #4042: a 57014 is never the engine's own bound, which the
    # driver raises as a ``TimeoutError``. It is a user's or an administrator's
    # cancel, or a server-side ``statement_timeout`` the operator set, and the
    # server is up.
    assert _DB_URL is not None
    engine = create_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            canceller = asyncio.create_task(_cancel_when_asleep(engine, pid))
            with pytest.raises(sqlalchemy.exc.DBAPIError) as caught:
                async with asyncio.timeout(10):
                    await conn.execute(text(_SLEEP))
            await canceller
    finally:
        await engine.dispose()

    assert getattr(caught.value.orig, "sqlstate", None) == "57014"
    assert is_transient_database_error(caught.value) is False


@pytest.fixture
async def unmigrated() -> AsyncIterator[str]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    try:
        yield _DB_URL
    finally:
        await downgrade_base(_DB_URL)


async def test_migrations_are_not_held_to_the_statement_bound(
    unmigrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bound no exchange with the server can meet: a migration held to it would
    # fail at its first query.
    monkeypatch.setenv("MCD_API_DATABASE__STATEMENT_TIMEOUT_SECONDS", "0.000001")

    await upgrade_head(unmigrated)
