"""Async-SQLAlchemy implementations of the identity repository Ports.

Each repository works on an ``AsyncSession`` owned by the enclosing
``UnitOfWork``; it stages rows and runs reads but never commits — commit is the
unit of work's job (DATABASE.md Section 1). Rows are translated to/from the
framework-free domain entities here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import (
    CursorResult,
    and_,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
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
    REVOKED_ROTATED,
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
    RotationChainId,
    UserId,
    Username,
)

# Fixed 64-bit key for the first-user bootstrap advisory lock (#909). A constant
# (not a hashed id) because there is a single global bootstrap, not one per
# resource; an arbitrary but stable value distinct from other subsystems' keys.
_BOOTSTRAP_LOCK_KEY = 0x6D63_7364_0001
_SESSION_LOCK_NAMESPACE = "mcsd:refresh-token-sessions"


def _session_lock_key(user_id: uuid.UUID) -> int:
    """Fold a user id into the signed 64-bit key of that user's session lock.

    Same scheme as the server lifecycle lock: a collision only over-serializes
    two unrelated users, never skips the lock.
    """

    digest = hashlib.blake2b(
        f"{_SESSION_LOCK_NAMESPACE}:{user_id}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") - (1 << 63)


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
        chain_id=RotationChainId(row.chain_id),
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

    async def update_profile(
        self,
        user_id: UserId,
        *,
        username: Username | None,
        email: EmailAddress | None,
        updated_at: dt.datetime,
    ) -> User | None:
        values: dict[str, Any] = {"updated_at": updated_at}
        if username is not None:
            values["username"] = username.value
        if email is not None:
            values["email"] = email.value
        # RETURNING hands back the row as written, so the caller sees columns
        # committed concurrently instead of its stale read. populate_existing
        # makes it overwrite any identity-map copy an earlier read in this
        # transaction left alive (the map holds rows weakly, so usually none).
        stmt = (
            update(UserModel)
            .where(UserModel.id == user_id.value)
            .values(**values)
            .returning(UserModel)
            .execution_options(populate_existing=True)
        )
        # The UPDATE executes eagerly (unlike a staged ORM insert flushed at
        # commit), so a concurrent rename into a taken username/email raises the
        # IntegrityError here; translate it to the same domain conflict the
        # commit-time path raises so the update race is not a raw 500.
        try:
            row = (await self._session.execute(stmt)).scalar_one_or_none()
        except IntegrityError as exc:
            translate_integrity_error(exc)
            raise
        return _to_user(row) if row is not None else None

    async def change_password_hash(
        self,
        user_id: UserId,
        *,
        expected_hash: str,
        new_hash: str,
        updated_at: dt.datetime,
    ) -> bool:
        # Compare-and-set: under READ COMMITTED an UPDATE blocked behind a
        # concurrent change re-checks its WHERE against the committed row, so a
        # replaced hash matches nothing.
        stmt = (
            update(UserModel)
            .where(
                UserModel.id == user_id.value,
                UserModel.password_hash == expected_hash,
            )
            .values(password_hash=new_hash, updated_at=updated_at)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount > 0

    async def set_active(
        self, user_id: UserId, *, active: bool, updated_at: dt.datetime
    ) -> None:
        stmt = (
            update(UserModel)
            .where(UserModel.id == user_id.value)
            .values(active=active, updated_at=updated_at)
        )
        await self._session.execute(stmt)

    async def set_platform_admin(
        self, user_id: UserId, *, is_platform_admin: bool, updated_at: dt.datetime
    ) -> None:
        stmt = (
            update(UserModel)
            .where(UserModel.id == user_id.value)
            .values(is_platform_admin=is_platform_admin, updated_at=updated_at)
        )
        await self._session.execute(stmt)

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

    async def lock_with_active_admins(self, user_id: UserId) -> tuple[User | None, int]:
        # One statement over the admin set and the target (the Port documents
        # the lock order). ORDER BY id makes every transaction acquire the row
        # locks in the same order -- the locking runs on the sorted rows -- so
        # concurrent guards cannot deadlock on scan order (#2226). A row waited
        # on is re-checked against the WHERE once its writer commits, so an
        # admin removed meanwhile drops out and the target comes back as
        # committed. populate_existing overwrites any stale identity-map copy.
        stmt = (
            select(UserModel)
            .where(
                or_(
                    and_(
                        UserModel.is_platform_admin.is_(True),
                        UserModel.active.is_(True),
                    ),
                    UserModel.id == user_id.value,
                )
            )
            .order_by(UserModel.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        target = next((row for row in rows if row.id == user_id.value), None)
        active_admins = sum(1 for row in rows if row.is_platform_admin and row.active)
        return (_to_user(target) if target is not None else None), active_admins


class SqlAlchemyRefreshTokenRepository(RefreshTokenRepository):
    """:class:`RefreshTokenRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: RefreshToken) -> None:
        self._session.add(
            RefreshTokenModel(
                id=token.id.value,
                user_id=token.user_id.value,
                chain_id=token.chain_id.value,
                token_hash=token.token_hash,
                issued_at=token.issued_at,
                expires_at=token.expires_at,
                revoked_at=token.revoked_at,
                revoked_reason=token.revoked_reason,
            )
        )

    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshTokenModel).where(
            RefreshTokenModel.token_hash == token_hash
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_refresh_token(row) if row is not None else None

    async def _lock_sessions(self, user_ids: Sequence[uuid.UUID]) -> None:
        # A row lock cannot serialize a session: a rotation's successor is a new
        # row, invisible to a revocation whose UPDATE already chose its rows. So
        # each takes this transaction-scoped advisory lock of the owner first
        # (issues #3249, #3251). Ascending key order is the Port's lock order;
        # one statement per key, because the order a single SELECT evaluates
        # the locks in is not guaranteed.
        for key in sorted({_session_lock_key(user_id) for user_id in user_ids}):
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=key)
            )

    async def lock_sessions_by_token_hashes(
        self, token_hashes: Sequence[str]
    ) -> dict[str, RefreshToken]:
        owners = (
            (
                await self._session.execute(
                    select(RefreshTokenModel.user_id).where(
                        RefreshTokenModel.token_hash.in_(token_hashes)
                    )
                )
            )
            .scalars()
            .all()
        )
        if not owners:
            return {}
        await self._lock_sessions(owners)
        # Re-read under the locks: under READ COMMITTED this statement sees what
        # a competitor that held them committed.
        stmt = (
            select(RefreshTokenModel)
            .where(RefreshTokenModel.token_hash.in_(token_hashes))
            .execution_options(populate_existing=True)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.token_hash: _to_refresh_token(row) for row in rows}

    async def revoke(
        self, token_hash: str, *, revoked_at: dt.datetime, reason: str
    ) -> None:
        stmt = (
            update(RefreshTokenModel)
            .where(RefreshTokenModel.token_hash == token_hash)
            .values(revoked_at=revoked_at, revoked_reason=reason)
        )
        await self._session.execute(stmt)

    async def revoke_chain(
        self, chain_id: RotationChainId, *, revoked_at: dt.datetime, reason: str
    ) -> None:
        stmt = (
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.chain_id == chain_id.value,
                (RefreshTokenModel.revoked_at.is_(None))
                | (RefreshTokenModel.revoked_reason == REVOKED_ROTATED),
            )
            .values(
                revoked_at=func.coalesce(RefreshTokenModel.revoked_at, revoked_at),
                revoked_reason=reason,
            )
        )
        await self._session.execute(stmt)

    async def revoke_all_for_user(
        self, user_id: UserId, *, revoked_at: dt.datetime
    ) -> None:
        await self._lock_sessions([user_id.value])
        stmt = (
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.user_id == user_id.value,
                (RefreshTokenModel.revoked_at.is_(None))
                | (RefreshTokenModel.revoked_reason == REVOKED_ROTATED),
            )
            .values(
                revoked_at=func.coalesce(RefreshTokenModel.revoked_at, revoked_at),
                revoked_reason=REVOKED_FAMILY,
            )
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
        await self._lock_sessions([user_id.value])
        stmt = update(RefreshTokenModel).where(
            RefreshTokenModel.user_id == user_id.value,
            (RefreshTokenModel.revoked_at.is_(None))
            | (RefreshTokenModel.revoked_reason == REVOKED_ROTATED),
        )
        if keep_token_hash is not None:
            stmt = stmt.where(RefreshTokenModel.token_hash != keep_token_hash)
        if keep_session_id is not None:
            stmt = stmt.where(RefreshTokenModel.id != keep_session_id.value)
        stmt = stmt.values(
            revoked_at=func.coalesce(RefreshTokenModel.revoked_at, revoked_at),
            revoked_reason=reason,
        )
        await self._session.execute(stmt)
