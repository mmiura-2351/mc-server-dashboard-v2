"""Async-SQLAlchemy adapter for the :class:`LoginAttemptStore` Port.

Persists brute-force / lockout state in the ``login_attempt`` and
``account_lockout`` tables (SECURITY.md Section 3). Each operation runs in its own
short transaction from the session factory: attempt recording and lockout writes
must survive a *failed* login, which deliberately does not commit the identity
:class:`UnitOfWork`, so this store cannot share that transaction.

``record_attempt`` and ``lock`` upsert/insert and commit; the counts are bounded
``COUNT`` queries over the windowed indexes; ``prune_attempts`` is the bounded
periodic delete that keeps the append-only table from growing without bound.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.identity.adapters.models import (
    AccountLockoutModel,
    LoginAttemptModel,
)
from mc_server_dashboard_api.identity.domain.login_attempt_store import (
    Lockout,
    LoginAttemptStore,
)


class SqlAlchemyLoginAttemptStore(LoginAttemptStore):
    """:class:`LoginAttemptStore` adapter over an async-SQLAlchemy session factory."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_attempt(
        self,
        *,
        username: str,
        ip: str | None,
        success: bool,
        at: dt.datetime,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                LoginAttemptModel(
                    username=username, ip=ip, success=success, created_at=at
                )
            )
            await session.commit()

    async def count_username_failures(
        self, username: str, *, since: dt.datetime
    ) -> int:
        async with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(LoginAttemptModel)
                .where(
                    LoginAttemptModel.username == username,
                    LoginAttemptModel.success.is_(False),
                    LoginAttemptModel.created_at >= since,
                )
            )
            return (await session.execute(stmt)).scalar_one()

    async def count_ip_failures(self, ip: str, *, since: dt.datetime) -> int:
        async with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(LoginAttemptModel)
                .where(
                    LoginAttemptModel.ip == ip,
                    LoginAttemptModel.success.is_(False),
                    LoginAttemptModel.created_at >= since,
                )
            )
            return (await session.execute(stmt)).scalar_one()

    async def get_lockout(self, username: str) -> Lockout | None:
        async with self._session_factory() as session:
            row = await session.get(AccountLockoutModel, username)
            if row is None:
                return None
            return Lockout(
                locked_until=row.locked_until, lockout_count=row.lockout_count
            )

    async def lock(
        self, username: str, *, locked_until: dt.datetime, lockout_count: int
    ) -> None:
        async with self._session_factory() as session:
            stmt = insert(AccountLockoutModel).values(
                username=username,
                locked_until=locked_until,
                lockout_count=lockout_count,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[AccountLockoutModel.username],
                set_={
                    "locked_until": locked_until,
                    "lockout_count": lockout_count,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def clear_lockout(self, username: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(AccountLockoutModel).where(
                    AccountLockoutModel.username == username
                )
            )
            await session.commit()

    async def prune_attempts(self, *, older_than: dt.datetime) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(LoginAttemptModel).where(
                    LoginAttemptModel.created_at < older_than
                )
            )
            await session.commit()
