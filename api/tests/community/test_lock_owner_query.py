"""Verify the SQL shape of ``lock_owner_role_holders``.

The query locks ``membership_role`` rows FOR UPDATE to serialize concurrent
last-Owner guards (#1959).  Two properties matter for correctness:

1. **ORDER BY membership_id** — all transactions acquire row locks in the same
   deterministic order, preventing deadlocks when Postgres chooses different
   scan orders for concurrent plans.
2. **OF membership_role** — the FOR UPDATE clause targets only the
   ``membership_role`` table, not the joined ``membership`` table, keeping the
   lock scope minimal.

These are safety invariants of the query itself, so we assert on the compiled
SQL rather than on end-to-end behavior (the latter is covered by
``test_community_owner_concurrency.py``).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyMembershipRepository,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    RoleId,
)


@pytest.fixture
def _captured_stmt() -> list[Any]:
    """Container that the mock session populates with the executed statement."""
    return []


@pytest.fixture
def repo(_captured_stmt: list[Any]) -> SqlAlchemyMembershipRepository:
    """Repository wired to a mock session that captures the executed stmt."""
    session = AsyncMock()

    async def _capture_execute(stmt: Select[Any]) -> MagicMock:
        _captured_stmt.append(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    session.execute = _capture_execute
    return SqlAlchemyMembershipRepository(session)


def _compile(stmt: Select[Any]) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_lock_owner_query_has_order_by_membership_id(
    repo: SqlAlchemyMembershipRepository,
    _captured_stmt: list[Any],
) -> None:
    """ORDER BY prevents deadlocks from non-deterministic scan order."""
    await repo.lock_owner_role_holders(CommunityId(uuid.uuid4()), RoleId(uuid.uuid4()))
    assert len(_captured_stmt) == 1
    sql = _compile(_captured_stmt[0])
    assert "ORDER BY membership_role.membership_id" in sql


@pytest.mark.asyncio
async def test_lock_owner_query_for_update_targets_membership_role_only(
    repo: SqlAlchemyMembershipRepository,
    _captured_stmt: list[Any],
) -> None:
    """OF clause narrows the lock to membership_role, not the joined table."""
    await repo.lock_owner_role_holders(CommunityId(uuid.uuid4()), RoleId(uuid.uuid4()))
    assert len(_captured_stmt) == 1
    sql = _compile(_captured_stmt[0])
    assert "FOR UPDATE OF membership_role" in sql
