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


class RefreshTokenRepository(abc.ABC):
    """Port: persistence for :class:`RefreshToken` session records."""

    @abc.abstractmethod
    async def add(self, token: RefreshToken) -> None:
        """Stage a new refresh token for persistence in the current transaction."""

    @abc.abstractmethod
    async def get_by_token_hash(self, token_hash: str) -> RefreshToken | None:
        """Return the token with ``token_hash``, or ``None`` if absent."""

    @abc.abstractmethod
    async def revoke(self, token_hash: str, *, revoked_at: dt.datetime) -> None:
        """Set ``revoked_at`` on the token with ``token_hash`` (logout / rotation).

        A no-op if no such row exists; callers establish existence first.
        """

    @abc.abstractmethod
    async def revoke_all_for_user(
        self, user_id: UserId, *, revoked_at: dt.datetime
    ) -> None:
        """Revoke every still-active token of ``user_id`` (reuse-family revoke)."""
