"""Shared fixtures for ``tests/integration`` (issue #1563).

``assert_max_queries`` lets a DB-integration test pin a repository's or
endpoint's SQL query budget directly, rather than only through call-shape
assertions against a stubbed repository (which is all PR #1562 / issue #1555
could do before this fixture existed).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
def assert_max_queries(
    engine: AsyncEngine,
) -> Callable[[int], AbstractContextManager[None]]:
    """Context-manager factory: fail if the wrapped block runs too many queries.

    Counts SQL statements via the ``before_cursor_execute`` event registered on
    ``engine``'s underlying sync engine (the documented way to hook SQLAlchemy
    events onto an :class:`AsyncEngine`) -- the actual statements the DB driver
    executes, so it cannot be fooled by call-count stubbing at the repository
    boundary. The listener is scoped to this one engine and torn down when the
    block exits, so it does not leak across tests. Usage::

        async with ServersUnitOfWork(factory) as uow:
            with assert_max_queries(1):
                await uow.plugins.enabled_geyser_server_ids(server_ids)
    """

    @contextmanager
    def _assert_max_queries(limit: int) -> Iterator[None]:
        count = 0

        def _count(*_args: object, **_kwargs: object) -> None:
            nonlocal count
            count += 1

        sync_engine = engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", _count)
        try:
            yield
        finally:
            event.remove(sync_engine, "before_cursor_execute", _count)
        assert count <= limit, f"expected at most {limit} queries, executed {count}"

    return _assert_max_queries
