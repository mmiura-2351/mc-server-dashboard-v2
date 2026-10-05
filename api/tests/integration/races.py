"""Shared harness for the DB race tests.

A race test pauses one transaction at a chosen point, starts a competitor on
another connection, waits until the competitor has either finished or blocked
on a lock (:func:`await_settled`), and only then resumes the paused one. The
interleaving is explicit, not timed; the waits are bounded only so that a
harness bug fails the test instead of hanging it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.migrate import downgrade_base, upgrade_head

# Upper bound on how long a transaction may take to reach its pause point, or a
# competitor to settle (commit, or block on a lock), before the test fails
# instead of hanging.
SETTLE_TIMEOUT = 10.0


@asynccontextmanager
async def race_database(url: str) -> AsyncIterator[AsyncEngine]:
    """Yield an engine on a freshly migrated database; downgrade on exit."""

    await downgrade_base(url)
    await upgrade_head(url)
    engine = create_async_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()
        await downgrade_base(url)


async def await_settled(
    engine: AsyncEngine,
    competitor: asyncio.Task[Any],
    pid: Callable[[], int | None],
) -> None:
    """Return once ``competitor`` has finished or is blocked on a lock.

    ``pid`` reports the backend pid of the competitor's current transaction, or
    ``None`` while it has none yet.
    """

    query = text(
        "SELECT 1 FROM pg_stat_activity WHERE pid = :pid AND wait_event_type = 'Lock'"
    )
    deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT
    while not competitor.done():
        backend = pid()
        if backend is not None:
            async with engine.connect() as conn:
                if (await conn.execute(query, {"pid": backend})).first():
                    return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("competitor neither committed nor blocked on a lock")
        await asyncio.sleep(0.02)
