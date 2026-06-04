"""Async-SQLAlchemy implementations of the identity repository Ports.

Each repository works on an ``AsyncSession`` owned by the enclosing
``UnitOfWork``; it stages rows and runs reads but never commits — commit is the
unit of work's job (DATABASE.md Section 1). Rows are translated to/from the
framework-free domain entities here.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.identity.adapters.models import (
    RefreshTokenModel,
    UserModel,
)
from mc_server_dashboard_api.identity.domain.entities import RefreshToken, User
from mc_server_dashboard_api.identity.domain.repositories import (
    RefreshTokenRepository,
    UserRepository,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    RefreshTokenId,
    UserId,
    Username,
)


def _to_user(row: UserModel) -> User:
    return User(
        id=UserId(row.id),
        username=Username(row.username),
        email=EmailAddress(row.email),
        password_hash=row.password_hash,
        is_platform_admin=row.is_platform_admin,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_refresh_token(row: RefreshTokenModel) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId(row.id),
        user_id=UserId(row.user_id),
        token_hash=row.token_hash,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


class SqlAlchemyUserRepository(UserRepository):
    """:class:`UserRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user: User) -> None:
        self._session.add(
            UserModel(
                id=user.id.value,
                username=user.username.value,
                email=user.email.value,
                password_hash=user.password_hash,
                is_platform_admin=user.is_platform_admin,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )
        )

    async def get_by_id(self, user_id: UserId) -> User | None:
        row = await self._session.get(UserModel, user_id.value)
        return _to_user(row) if row is not None else None

    async def get_by_username(self, username: Username) -> User | None:
        stmt = select(UserModel).where(
            func.lower(UserModel.username) == username.value.lower()
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user(row) if row is not None else None

    async def get_by_email(self, email: EmailAddress) -> User | None:
        stmt = select(UserModel).where(UserModel.email == email.value)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user(row) if row is not None else None


class SqlAlchemyRefreshTokenRepository(RefreshTokenRepository):
    """:class:`RefreshTokenRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: RefreshToken) -> None:
        self._session.add(
            RefreshTokenModel(
                id=token.id.value,
                user_id=token.user_id.value,
                token_hash=token.token_hash,
                issued_at=token.issued_at,
                expires_at=token.expires_at,
                revoked_at=token.revoked_at,
            )
        )

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshTokenModel).where(
            RefreshTokenModel.token_hash == token_hash
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_refresh_token(row) if row is not None else None

    async def revoke(self, token_hash: str, *, revoked_at: dt.datetime) -> None:
        stmt = (
            update(RefreshTokenModel)
            .where(RefreshTokenModel.token_hash == token_hash)
            .values(revoked_at=revoked_at)
        )
        await self._session.execute(stmt)

    async def revoke_all_for_user(
        self, user_id: UserId, *, revoked_at: dt.datetime
    ) -> None:
        stmt = (
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.user_id == user_id.value,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        await self._session.execute(stmt)
