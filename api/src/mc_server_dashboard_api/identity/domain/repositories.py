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

    # The writers below are operation-specific (#3214): each persists only the
    # columns its operation owns, plus ``updated_at``, so a writer acting on a
    # stale read cannot restore columns another operation changed meanwhile (a
    # profile edit resurrecting ``active`` / ``is_platform_admin`` or an old
    # password hash). A writer whose row is gone matches nothing.

    @abc.abstractmethod
    async def update_profile(
        self,
        user_id: UserId,
        *,
        username: Username | None,
        email: EmailAddress | None,
        updated_at: dt.datetime,
    ) -> User | None:
        """Set the supplied profile fields; return the persisted user, or ``None``.

        An omitted (``None``) field is left untouched. The returned user is the
        row as written, so its other columns reflect any concurrently committed
        change rather than the caller's earlier read. A username/email taken by
        another user raises the domain conflict error.
        """

    @abc.abstractmethod
    async def change_password_hash(
        self,
        user_id: UserId,
        *,
        expected_hash: str,
        new_hash: str,
        updated_at: dt.datetime,
    ) -> bool:
        """Replace the hash only if it is still ``expected_hash``; return whether.

        The caller verified the current password against ``expected_hash``; if the
        stored hash changed since, that verification is stale and nothing is
        written.
        """

    @abc.abstractmethod
    async def set_active(
        self, user_id: UserId, *, active: bool, updated_at: dt.datetime
    ) -> None:
        """Set the account lifecycle flag (issue #278)."""

    @abc.abstractmethod
    async def set_platform_admin(
        self, user_id: UserId, *, is_platform_admin: bool, updated_at: dt.datetime
    ) -> None:
        """Set the platform-admin flag (FR-AUTH-6)."""

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
    async def lock_with_active_admins(self, user_id: UserId) -> tuple[User | None, int]:
        """Lock ``user_id``'s row with the active admins'; return it and their count.

        Returns the target as locked (``None`` if absent) and the number of
        active platform admins among the locked rows, the target included when
        it is one. Every guard of the at-least-one-active-admin invariant (#260)
        -- deactivate, revoke, admin delete, self delete -- decides from this
        result, never from an earlier unlocked read: the target can be granted
        admin, or deactivated, between such a read and the write (#3239).

        Lock order, the same for every caller:

        1. ``user`` rows: the active platform admins and the target, in one
           statement, in ascending id order, ``FOR NO KEY UPDATE``, before any
           other lock in the transaction;
        2. the target's dependent rows: its refresh tokens, then, for a delete,
           the row itself (``FOR UPDATE``) and its ``ON DELETE CASCADE`` rows. A
           guard that must also lock rows of another context for the same
           decision (the target's community ownership, #3217) takes them here,
           after step 1.

        One statement, not "admin set, then target": the target can become an
        admin in between, and a competing guard that locked it (a lower id)
        first would then wait on the admin rows held here while this waits on
        the target. ``FOR NO KEY UPDATE`` serializes the guards with each other
        and with every write of these rows (an UPDATE takes it, a DELETE takes
        ``FOR UPDATE``), but not with the ``FOR KEY SHARE`` a foreign-key check
        takes when a row referencing the user is inserted: such an insert does
        not touch the invariant, and a token rotation that holds its old token
        row would otherwise deadlock with the guard revoking that token.

        Only the locked rows are counted, and each stays an active admin until
        the caller's transaction ends; an admin granted while the statement
        waited is not seen, which can only refuse, never allow, a removal.
        Grant and reactivation never reduce the set and take no lock beyond
        their own row's UPDATE, which waits for a guard holding that row.
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

        Also re-stamps any already-revoked ``'rotated'`` predecessors to
        ``'family'``, preserving their original ``revoked_at`` via COALESCE, so
        they are no longer eligible for the reuse grace window (issue #1960).

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
        keep_session_id: RefreshTokenId | None = None,
        revoked_at: dt.datetime,
        reason: str,
    ) -> None:
        """Revoke ``user_id``'s active tokens except the kept one (issue #387, #606).

        Also re-stamps any already-revoked ``'rotated'`` predecessors to
        ``reason``, preserving their original ``revoked_at`` via COALESCE, so
        they are no longer eligible for the reuse grace window (mirroring the
        ``revoke_all_for_user`` fix from issue #1960; see issue #2172).

        The session to keep can be identified two ways (at most one should be set):

        - ``keep_token_hash`` — the hash of the refresh token the caller presented
          (original mechanism).
        - ``keep_session_id`` — the row id of the session to spare (issue #606),
          usable by clients that know their session id but cannot present the
          refresh token (e.g. the SPA whose cookie is ``/api/auth``-confined).

        With both ``None`` no row is spared (the caller could not identify its
        current session).
        """
