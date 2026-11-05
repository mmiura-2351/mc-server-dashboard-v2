"""The data-plane assignment lookup does not draw from the shared pool (#3544).

The snapshot commit runs the lookup while it holds the storage per-server
publish lock. A start holds its lifecycle-lock and unit-of-work connections from
the shared pool across a storage write that waits on that lock, so a lookup
drawing from a two-connection shared pool would wait on the start, and the start
on the commit.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service); skipped
otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request

from mc_server_dashboard_api.core.adapters.database import create_engine
from mc_server_dashboard_api.dependencies import get_assigned_worker_lookup
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


@pytest.fixture
async def migrated() -> AsyncIterator[str]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    try:
        yield _DB_URL
    finally:
        await downgrade_base(_DB_URL)


async def test_assignment_lookup_answers_while_the_shared_pool_is_held(
    migrated: str,
) -> None:
    # The smallest accepted shared pool (issue #3536), both connections held as
    # a start holds them: one for its lifecycle lock, one for its unit of work.
    shared = create_engine(migrated, pool_size=2, max_overflow=0)
    fence = create_engine(migrated, pool_size=1, max_overflow=0)
    state = SimpleNamespace(engine=shared, assignment_fence_engine=fence)
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))
    try:
        async with shared.connect(), shared.connect():
            lookup = get_assigned_worker_lookup(request)
            assigned = await asyncio.wait_for(lookup(uuid.uuid4(), uuid.uuid4()), 10)
        assert assigned is None
    finally:
        await fence.dispose()
        await shared.dispose()
