"""Unit test for SqlAlchemyDatabasePing degraded behaviour (no real DB).

The liveness probe must report ``False`` rather than raise when the database is
unreachable. This needs no Postgres — it points the engine at a dead port — so
it lives with the fast unit suite, not the Postgres-gated integration tests
(TESTING.md Section 5).
"""

from __future__ import annotations

from mc_server_dashboard_api.core.adapters.database import (
    SqlAlchemyDatabasePing,
    create_engine,
)


async def test_is_reachable_false_when_unconnectable() -> None:
    # A port nothing listens on: the adapter must report False, not raise.
    engine = create_engine("postgresql+asyncpg://u:p@127.0.0.1:5/none")
    try:
        ping = SqlAlchemyDatabasePing(engine)
        assert await ping.is_reachable() is False
    finally:
        await engine.dispose()
