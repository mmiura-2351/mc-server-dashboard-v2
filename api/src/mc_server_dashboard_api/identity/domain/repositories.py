"""Persistence Ports for the identity context.

The ``<Entity>Repository`` interfaces (ARCHITECTURE.md Section 5.1) the domain
depends on; concrete async-SQLAlchemy adapters implement them. Lookups return
``None`` when absent rather than raising, so callers decide policy.
"""

from __future__ import annotations

import abc
import datetime as dt

from mc_server_dashboard_api.identity.domain.entities import RefreshToken, User
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    RefreshTokenId,
    UserId,
    Username,
)


class UserRepository(abc.ABC):
    """Port: persistence for :class:`User` aggregates."""

    @abc.abstractmethod
    async def add(self, user: User) -> None:
        """Stage a new user for persistence within the current transaction."""

    @abc.abstractmethod
    async def get_by_id(self, user_id: UserId) -> User | None:
        """Return the user with ``user_id``, or ``None`` if absent."""

    @abc.abstractmethod
    async def get_by_username(self, username: Username) -> User | None:
        """Return the user with ``username`` (case-insensitive), or ``None``."""

    @abc.abstractmethod
    async def get_by_email(self, email: EmailAddress) -> User | None:
        """Return the user with ``email``, or ``None`` if absent."""

    @abc.abstractmethod
    async def usernames_by_id(self, user_ids: list[UserId]) -> dict[UserId, Username]:
        """Resolve ``user_ids`` to their usernames in a single indexed query.

        Returns a mapping for the ids that exist; absent ids are omitted. Backs
        the community context's user-directory seam so member listings can be
        enriched with usernames without N+1 lookups (issue #78).
        """

    @abc.abstractmethod
    async def update(self, user: User) -> None:
        """Persist a mutated user (profile / password change, FR-AUTH self-service)."""

    @abc.abstractmethod
    async def delete(self, user_id: UserId) -> None:
        """Delete the user with ``user_id``; cascades remove their dependent rows."""

    @abc.abstractmethod
    async def list_page(self, *, limit: int, offset: int) -> list[User]:
        """Return a page of users ordered by ``created_at`` (admin listing, #278)."""

    @abc.abstractmethod
    async def count_all(self) -> int:
        """Count every user row (the total for the admin listing's pagination, #278)."""

    @abc.abstractmethod
    async def lock_for_bootstrap(self) -> int:
        """Serialize the first-user bootstrap and return the current user count (#909).

        Takes a transaction-scoped advisory lock on a fixed key, then counts the
        user rows under it. The empty-table case cannot be serialized by a
        ``SELECT ... FOR UPDATE`` (there are no rows to lock), so concurrent first
        registrations would each read a count of 0 and both auto-grant
        platform-admin. The advisory lock makes them serialize on the same key:
        the second transaction blocks until the first commits, then re-counts the
        now-incremented set (1) and does not auto-grant. Exactly one user wins the
        bootstrap grant, mirroring the FOR UPDATE last-admin guard's intent (#260).
        The lock is released automatically when the caller's transaction ends.
        """

    @abc.abstractmethod
    async def count_active_platform_admins(self) -> int:
        """Count *active* platform admins (the last-active-admin invariant, #278).

        A deactivated admin cannot act, so it does not count toward the "platform
        must keep at least one administrator" invariant that the delete /
        deactivate / revoke guards enforce.
        """

    @abc.abstractmethod
    async def lock_active_platform_admins(self) -> int:
        """Lock the active-admin rows ``FOR UPDATE`` and return their count (#260).

        Like :meth:`count_active_platform_admins`, but takes a row lock on the
        matched ``user`` rows inside the caller's transaction. Concurrent guards
        that reduce the active-admin set (last-admin self-delete / deactivate /
        revoke) therefore serialize on the same rows: the second transaction
        blocks until the first commits, then re-counts the now-decremented set
        and refuses. Callers invoke it only on paths that reduce the set, so
        non-reducing hot paths (grant, reactivate, non-admin delete) stay
        lock-free.
        """


class RefreshTokenRepository(abc.ABC):
    """Port: persistence for :class:`RefreshToken` session records."""

    @abc.abstractmethod
    async def add(self, token: RefreshToken) -> None:
        """Stage a new refresh token for persistence in the current transaction."""

    @abc.abstractmethod
    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        """Return the token with ``token_hash``, or ``None`` if absent."""

    @abc.abstractmethod
    async def revoke(
        self, token_hash: str, *, revoked_at: dt.datetime, reason: str
    ) -> None:
        """Set ``revoked_at`` / ``revoked_reason`` on the ``token_hash`` row.

        ``reason`` records *why* (a ``REVOKED_*`` code) so the reuse grace window
        can grace only ``rotated`` predecessors (issue #369). A no-op if no such
        row exists; callers establish existence first.
        """

    @abc.abstractmethod
    async def revoke_all_for_user(
        self, user_id: UserId, *, revoked_at: dt.datetime
    ) -> None:
        """Revoke every still-active token of ``user_id`` (family revoke).

        Stamps ``revoked_reason = 'family'`` so none of the revoked tokens is
        graceable in the reuse window: a family revoke is the theft response (or
        a password change / deactivate / delete), never a rotation (issue #369).
        """

    @abc.abstractmethod
    async def list_active_for_user(
        self, user_id: UserId, *, now: dt.datetime
    ) -> list[RefreshToken]:
        """Return ``user_id``'s active (unrevoked, unexpired) tokens (issue #387).

        Backs the session listing. Ordered newest-first by ``issued_at``.
        """

    @abc.abstractmethod
    async def revoke_by_id(
        self,
        token_id: RefreshTokenId,
        user_id: UserId,
        *,
        revoked_at: dt.datetime,
        reason: str,
    ) -> bool:
        """Revoke ``user_id``'s active token ``token_id`` (issue #387).

        Scoped to ``user_id`` so a caller can only revoke their own session: a
        ``token_id`` owned by another user matches no row. Returns whether an
        active row was revoked, so the caller maps a miss to 404 (the id is
        unknown *or* belongs to someone else — no existence leak).
        """

    @abc.abstractmethod
    async def revoke_all_for_user_except(
        self,
        user_id: UserId,
        *,
        keep_token_hash: str | None,
        revoked_at: dt.datetime,
        reason: str,
    ) -> None:
        """Revoke ``user_id``'s active tokens except ``keep_token_hash`` (issue #387).

        Backs everywhere-else logout: the caller's current session (identified by
        the refresh token it presented) is kept alive, the rest revoked. With
        ``keep_token_hash`` ``None`` no row is spared (the caller could not
        identify its current session).
        """
