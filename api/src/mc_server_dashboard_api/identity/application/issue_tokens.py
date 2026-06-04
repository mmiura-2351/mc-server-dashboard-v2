"""Shared helper: issue an access+refresh pair and persist the refresh row.

Both :class:`~.login.Login` and :class:`~.refresh_session.RefreshSession` mint a
new pair on success; this keeps that single sequence in one place. The caller
owns the :class:`UnitOfWork` transaction — this only stages the new refresh-token
row through it, so issuing and (for rotation) revoking stay in one transaction.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    UserId,
)


async def issue_token_pair(
    *,
    uow: UnitOfWork,
    tokens: TokenService,
    user_id: UserId,
    now: dt.datetime,
    refresh_ttl: dt.timedelta,
) -> TokenPair:
    """Mint a pair, stage the refresh row in ``uow``, and return the plaintext."""

    access = tokens.issue_access_token(user_id)
    issued = tokens.issue_refresh_token()
    await uow.refresh_tokens.add(
        RefreshToken(
            id=RefreshTokenId.new(),
            user_id=user_id,
            token_hash=issued.token_hash,
            issued_at=now,
            expires_at=now + refresh_ttl,
        )
    )
    return TokenPair(access_token=access, refresh_token=issued.secret)
