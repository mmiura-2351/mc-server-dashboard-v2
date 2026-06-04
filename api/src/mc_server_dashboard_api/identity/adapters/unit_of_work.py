"""Async-SQLAlchemy implementation of the identity ``UnitOfWork`` Port.

Opens a session from the factory on ``__aenter__`` and binds the repositories to
it; ``commit`` flushes and commits the transaction, while leaving the block
without committing rolls back (the session is closed either way). This gives use
cases the all-or-nothing transaction the Port promises (DATABASE.md Section 1).
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.identity.adapters.repositories import (
    SqlAlchemyRefreshTokenRepository,
    SqlAlchemyUserRepository,
)
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork

# Unique constraints/indexes on ``user`` (migration 0002) mapped to the domain
# error to raise when a concurrent insert violates them, so the duplicate race
# surfaces as the same error as the use case's pre-check.
_USERNAME_CONSTRAINTS = frozenset({"uq_user_username", "uq_user_username_lower"})
_EMAIL_CONSTRAINTS = frozenset({"uq_user_email"})


class SqlAlchemyUnitOfWork(UnitOfWork):
    """:class:`UnitOfWork` adapter over an async-SQLAlchemy session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.users = SqlAlchemyUserRepository(self._session)
        self.refresh_tokens = SqlAlchemyRefreshTokenRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            await self.rollback()
        finally:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        assert self._session is not None
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            _translate_integrity_error(exc)
            raise

    async def rollback(self) -> None:
        assert self._session is not None
        await self._session.rollback()


def _translate_integrity_error(exc: IntegrityError) -> None:
    """Raise the matching domain error for a known unique violation, else return.

    The constraint name comes from the asyncpg driver error underneath the
    SQLAlchemy wrapper; an unrecognised violation is left for the caller to
    re-raise as-is.
    """

    constraint = getattr(exc.orig, "constraint_name", None)
    if constraint in _USERNAME_CONSTRAINTS:
        raise UsernameAlreadyExistsError(str(constraint)) from exc
    if constraint in _EMAIL_CONSTRAINTS:
        raise EmailAlreadyExistsError(str(constraint)) from exc
