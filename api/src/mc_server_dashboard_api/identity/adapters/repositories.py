"""Async-SQLAlchemy implementations of the identity repository Ports.

Each repository works on an ``AsyncSession`` owned by the enclosing
``UnitOfWork``; it stages rows and runs reads but never commits — commit is the
unit of work's job (DATABASE.md Section 1). Rows are translated to/from the
framework-free domain entities here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.identity.adapters.integrity import (
    translate_integrity_error,
)
from mc_server_dashboard_api.identity.adapters.models import (
    RefreshTokenModel,
    UserModel,
)
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_FAMILY,
    RefreshToken,
    User,
)
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

# Fixed 64-bit key for the first-user bootstrap advisory lock (#909). A constant
# (not a hashed id) because there is a single global bootstrap, not one per
# resource; an arbitrary but stable value distinct from other subsystems' keys.
_BOOTSTRAP_LOCK_KEY = 0x6D63_7364_0001


def _to_user(row: UserModel) -> User:
    return User(
        id=UserId(row.id),
        username=Username(row.username),
        email=EmailAddress(row.email),
        password_hash=row.password_hash,
        is_platform_admin=row.is_platform_admin,
        active=row.active,
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
        revoked_reason=row.revoked_reason,
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
                active=user.active,
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

    async def usernames_by_id(self, user_ids: list[UserId]) -> dict[UserId, Username]:
        if not user_ids:
            return {}
        ids = [uid.value for uid in user_ids]
        stmt = select(UserModel.id, UserModel.username).where(UserModel.id.in_(ids))
        rows = (await self._session.execute(stmt)).all()
        return {UserId(row.id): Username(row.username) for row in rows}

    async def update(self, user: User) -> None:
        stmt = (
            update(UserModel)
            .where(UserModel.id == user.id.value)
            .values(
                username=user.username.value,
                email=user.email.value,
                password_hash=user.password_hash,
                is_platform_admin=user.is_platform_admin,
                active=user.active,
                updated_at=user.updated_at,
            )
        )
        # The Core UPDATE executes eagerly (unlike a staged ORM insert flushed at
        # commit), so a concurrent rename into a taken username/email raises the
        # IntegrityError here; translate it to the same domain conflict the
        # commit-time path raises so the update race is not a raw 500.
        try:
            await self._session.execute(stmt)
        except IntegrityError as exc:
            translate_integrity_error(exc)
            raise

    async def delete(self, user_id: UserId) -> None:
        stmt = delete(UserModel).where(UserModel.id == user_id.value)
        await self._session.execute(stmt)

    async def list_page(self, *, limit: int, offset: int) -> list[User]:
        stmt = (
            select(UserModel)
            .order_by(UserModel.created_at, UserModel.id)
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_user(row) for row in rows]

    async def count_all(self) -> int:
        stmt = select(func.count()).select_from(UserModel)
        return (await self._session.execute(stmt)).scalar_one()

    async def lock_for_bootstrap(self) -> int:
        # Serialize concurrent first-user bootstraps on a fixed advisory key
        # (#909). pg_advisory_xact_lock blocks until any other transaction holding
        # the same key commits/rolls back, and is released automatically at the end
        # of this transaction -- no explicit unlock. A row lock cannot serialize the
        # empty-table case (nothing to lock), so the bootstrap decision is gated on
        # this lock instead. The count is read under the lock so the second racer,
        # unblocked after the first commits, sees the incremented set.
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)").bindparams(
                key=_BOOTSTRAP_LOCK_KEY
            )
        )
        stmt = select(func.count()).select_from(UserModel)
        return (await self._session.execute(stmt)).scalar_one()

    async def count_active_platform_admins(self) -> int:
        stmt = select(func.count()).where(
            UserModel.is_platform_admin.is_(True), UserModel.active.is_(True)
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def lock_active_platform_admins(self) -> int:
        # Lock the matched user rows FOR UPDATE so concurrent last-admin guards
        # serialize on them (#260): the second transaction blocks until the first
        # commits, then this re-read under READ COMMITTED sees the decremented
        # set. A bare count(*) cannot be row-locked, so select the rows under the
        # lock and count them here.
        stmt = (
            select(UserModel.id)
            .where(UserModel.is_platform_admin.is_(True), UserModel.active.is_(True))
            .with_for_update()
        )
        return len((await self._session.execute(stmt)).all())


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

    async def revoke(
        self, token_hash: str, *, revoked_at: dt.datetime, reason: str
    ) -> None:
        stmt = (
            update(RefreshTokenModel)
            .where(RefreshTokenModel.token_hash == token_hash)
            .values(revoked_at=revoked_at, revoked_reason=reason)
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
            .values(revoked_at=revoked_at, revoked_reason=REVOKED_FAMILY)
        )
        await self._session.execute(stmt)

    async def list_active_for_user(
        self, user_id: UserId, *, now: dt.datetime
    ) -> list[RefreshToken]:
        stmt = (
            select(RefreshTokenModel)
            .where(
                RefreshTokenModel.user_id == user_id.value,
                RefreshTokenModel.revoked_at.is_(None),
                RefreshTokenModel.expires_at > now,
            )
            .order_by(RefreshTokenModel.issued_at.desc(), RefreshTokenModel.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_refresh_token(row) for row in rows]

    async def revoke_by_id(
        self,
        token_id: RefreshTokenId,
        user_id: UserId,
        *,
        revoked_at: dt.datetime,
        reason: str,
    ) -> bool:
        # Scope the UPDATE to (id, user_id) and still-active so a caller can only
        # revoke their own live session; rowcount tells the caller whether a row
        # matched (else 404, no existence leak).
        stmt = (
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.id == token_id.value,
                RefreshTokenModel.user_id == user_id.value,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at, revoked_reason=reason)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount > 0

    async def revoke_all_for_user_except(
        self,
        user_id: UserId,
        *,
        keep_token_hash: str | None,
        keep_session_id: RefreshTokenId | None = None,
        revoked_at: dt.datetime,
        reason: str,
    ) -> None:
        stmt = update(RefreshTokenModel).where(
            RefreshTokenModel.user_id == user_id.value,
            RefreshTokenModel.revoked_at.is_(None),
        )
        if keep_token_hash is not None:
            stmt = stmt.where(RefreshTokenModel.token_hash != keep_token_hash)
        if keep_session_id is not None:
            stmt = stmt.where(RefreshTokenModel.id != keep_session_id.value)
        stmt = stmt.values(revoked_at=revoked_at, revoked_reason=reason)
        await self._session.execute(stmt)
