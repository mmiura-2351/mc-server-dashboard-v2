"""Verify the SQL shape of ``lock_active_platform_admins``.

The query locks the matched ``user`` rows FOR UPDATE to serialize concurrent
last-admin guards (#260).  For correctness one property matters:

**ORDER BY id** — all transactions acquire row locks in the same deterministic
order, preventing deadlocks when Postgres chooses different scan orders for
concurrent plans (#2226, same class as #2149).

This is a single-table query, so no ``OF`` clause is needed to scope the lock.
The invariant is a safety property of the query itself, so we assert on the
compiled SQL rather than on end-to-end behavior.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from mc_server_dashboard_api.identity.adapters.repositories import (
    SqlAlchemyUserRepository,
)


@pytest.fixture
def _captured_stmt() -> list[Any]:
    """Container that the mock session populates with the executed statement."""
    return []


@pytest.fixture
def repo(_captured_stmt: list[Any]) -> SqlAlchemyUserRepository:
    """Repository wired to a mock session that captures the executed stmt."""
    session = AsyncMock()

    async def _capture_execute(stmt: Select[Any]) -> MagicMock:
        _captured_stmt.append(stmt)
        result = MagicMock()
        result.all.return_value = []
        return result

    session.execute = _capture_execute
    return SqlAlchemyUserRepository(session)


def _compile(stmt: Select[Any]) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_lock_active_platform_admins_has_order_by_id(
    repo: SqlAlchemyUserRepository,
    _captured_stmt: list[Any],
) -> None:
    """ORDER BY prevents deadlocks from non-deterministic scan order."""
    await repo.lock_active_platform_admins()
    assert len(_captured_stmt) == 1
    sql = _compile(_captured_stmt[0])
    assert 'ORDER BY "user".id' in sql


@pytest.mark.asyncio
async def test_lock_active_platform_admins_for_update(
    repo: SqlAlchemyUserRepository,
    _captured_stmt: list[Any],
) -> None:
    """The rows are locked FOR UPDATE so concurrent guards serialize on them."""
    await repo.lock_active_platform_admins()
    assert len(_captured_stmt) == 1
    sql = _compile(_captured_stmt[0])
    assert "FOR UPDATE" in sql
