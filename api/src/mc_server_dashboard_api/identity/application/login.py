"""Login use case: verify credentials and issue a token pair (FR-AUTH-2).

Looks up the user by username, verifies the password against the stored hash via
the :class:`PasswordHasher`, and on success issues an access + refresh pair,
persisting the refresh row atomically. On *any* failure it raises a single
:class:`InvalidCredentialsError` — the unknown-user and wrong-password paths are
indistinguishable to the caller (SECURITY.md Section 2, enumeration defence) —
after awaiting the :class:`LoginFailureDelay` seam (#57 fills in the real delay).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.identity.application.issue_tokens import issue_token_pair
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.errors import InvalidCredentialsError
from mc_server_dashboard_api.identity.domain.login_failure_delay import (
    LoginFailureDelay,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import Username


@dataclass(frozen=True)
class Login:
    """Authenticate a username/password and mint a session token pair."""

    uow: UnitOfWork
    hasher: PasswordHasher
    tokens: TokenService
    clock: Clock
    failure_delay: LoginFailureDelay
    refresh_ttl: dt.timedelta

    async def __call__(self, *, username: str, password: str) -> TokenPair:
        name = Username(username)
        async with self.uow:
            user = await self.uow.users.get_by_username(name)
            if user is None or not self.hasher.verify(password, user.password_hash):
                await self.failure_delay.apply()
                raise InvalidCredentialsError
            pair = await issue_token_pair(
                uow=self.uow,
                tokens=self.tokens,
                user_id=user.id,
                now=self.clock.now(),
                refresh_ttl=self.refresh_ttl,
            )
            await self.uow.commit()
        return pair
