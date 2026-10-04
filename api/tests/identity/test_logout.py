"""Unit tests for the Logout use case (refresh-token revocation, FR-AUTH-3)."""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.application.refresh_session import (
    RefreshSession,
)
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_FAMILY,
    REVOKED_LOGOUT,
    REVOKED_ROTATED,
    REVOKED_SUPERSEDED,
    RefreshToken,
)
from mc_server_dashboard_api.identity.domain.errors import InvalidRefreshTokenError
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    RotationChainId,
    UserId,
)
from tests.identity.fakes import FakeClock, FakeTokenService, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _logout(uow: FakeUnitOfWork) -> Logout:
    return Logout(uow=uow, tokens=FakeTokenService(), clock=FakeClock(_NOW))


def _refresh(uow: FakeUnitOfWork) -> RefreshSession:
    return RefreshSession(
        uow=uow,
        tokens=FakeTokenService(),
        clock=FakeClock(_NOW),
        refresh_ttl=dt.timedelta(days=14),
        reuse_grace=dt.timedelta(seconds=60),
    )


def _seed(
    uow: FakeUnitOfWork,
    *,
    secret: str,
    user_id: UserId | None = None,
    chain_id: RotationChainId | None = None,
    revoked_at: dt.datetime | None = None,
    revoked_reason: str | None = None,
) -> str:
    token_hash = f"hash::{secret}"
    uow.refresh_tokens.seed(
        RefreshToken(
            id=RefreshTokenId.new(),
            user_id=user_id or UserId.new(),
            chain_id=chain_id or RotationChainId.new(),
            token_hash=token_hash,
            issued_at=_NOW,
            expires_at=_NOW + dt.timedelta(days=14),
            revoked_at=revoked_at,
            revoked_reason=revoked_reason,
        )
    )
    return token_hash


async def test_logout_revokes_the_token() -> None:
    uow = FakeUnitOfWork()
    uow.refresh_tokens.seed(
        RefreshToken(
            id=RefreshTokenId.new(),
            user_id=UserId.new(),
            chain_id=RotationChainId.new(),
            token_hash="hash::session",
            issued_at=_NOW,
            expires_at=_NOW + dt.timedelta(days=14),
        )
    )

    await _logout(uow)(refresh_token="session")

    assert uow.refresh_tokens.by_hash["hash::session"].revoked_at == _NOW
    assert uow.commits == 1


async def test_logout_unknown_token_is_idempotent() -> None:
    uow = FakeUnitOfWork()
    await _logout(uow)(refresh_token="never-issued")
    assert uow.refresh_tokens.by_hash == {}
    assert uow.commits == 1


async def test_logout_revokes_both_body_and_superseded_cookie_token() -> None:
    # Both-transports logout: the body token is revoked as ``logout`` and the
    # superseded cookie token is revoked too, as ``superseded`` (issue #384).
    uow = FakeUnitOfWork()
    body_hash = _seed(uow, secret="body-token")
    cookie_hash = _seed(uow, secret="cookie-token")

    await _logout(uow)(refresh_token="body-token", superseded_token="cookie-token")

    assert uow.refresh_tokens.by_hash[body_hash].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash[body_hash].revoked_reason == REVOKED_LOGOUT
    assert uow.refresh_tokens.by_hash[cookie_hash].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash[cookie_hash].revoked_reason == REVOKED_SUPERSEDED
    assert uow.commits == 1


async def test_logout_same_token_in_both_transports_is_not_double_revoked() -> None:
    # The cookie carried the same token as the body: revoked once as ``logout``,
    # not re-stamped ``superseded``.
    uow = FakeUnitOfWork()
    token_hash = _seed(uow, secret="same-token")

    await _logout(uow)(refresh_token="same-token", superseded_token="same-token")

    assert uow.refresh_tokens.by_hash[token_hash].revoked_reason == REVOKED_LOGOUT


async def test_logout_unknown_superseded_token_is_idempotent() -> None:
    # A malformed / never-issued cookie token alongside a valid body token must not
    # fail logout.
    uow = FakeUnitOfWork()
    body_hash = _seed(uow, secret="body-token")

    await _logout(uow)(refresh_token="body-token", superseded_token="never-issued")

    assert uow.refresh_tokens.by_hash[body_hash].revoked_at == _NOW
    assert "hash::never-issued" not in uow.refresh_tokens.by_hash


async def test_logout_with_a_rotated_token_revokes_its_successor() -> None:
    # The refresh that rotated ``old`` is still in flight when the browser logs
    # out with ``old``: the successor it minted must die with the session, or its
    # late Set-Cookie revives the session in that browser (issue #3249).
    uow = FakeUnitOfWork()
    user, chain = UserId.new(), RotationChainId.new()
    rotated_at = _NOW - dt.timedelta(seconds=5)
    old_hash = _seed(
        uow,
        secret="old",
        user_id=user,
        chain_id=chain,
        revoked_at=rotated_at,
        revoked_reason=REVOKED_ROTATED,
    )
    successor_hash = _seed(uow, secret="successor", user_id=user, chain_id=chain)

    await _logout(uow)(refresh_token="old")

    successor = uow.refresh_tokens.by_hash[successor_hash]
    assert (successor.revoked_at, successor.revoked_reason) == (_NOW, REVOKED_LOGOUT)
    # The rotated predecessor keeps its rotation time but is no longer graceable.
    old = uow.refresh_tokens.by_hash[old_hash]
    assert (old.revoked_at, old.revoked_reason) == (rotated_at, REVOKED_LOGOUT)


async def test_logout_leaves_the_users_other_sessions_alone() -> None:
    uow = FakeUnitOfWork()
    user = UserId.new()
    _seed(uow, secret="this-device", user_id=user)
    other_hash = _seed(uow, secret="other-device", user_id=user)

    await _logout(uow)(refresh_token="this-device")

    assert uow.refresh_tokens.by_hash[other_hash].revoked_at is None


async def test_logout_keeps_the_cause_of_an_already_dead_chain_token() -> None:
    # Only live and rotated tokens are stamped ``logout``: a token revoked for
    # another cause keeps that cause and its time.
    uow = FakeUnitOfWork()
    chain = RotationChainId.new()
    earlier = _NOW - dt.timedelta(hours=1)
    dead_hash = _seed(
        uow,
        secret="dead",
        chain_id=chain,
        revoked_at=earlier,
        revoked_reason=REVOKED_FAMILY,
    )
    _seed(uow, secret="live", chain_id=chain)

    await _logout(uow)(refresh_token="live")

    dead = uow.refresh_tokens.by_hash[dead_hash]
    assert (dead.revoked_at, dead.revoked_reason) == (earlier, REVOKED_FAMILY)


@pytest.mark.parametrize(
    ("logged_out", "presented_after"),
    [("old", "successor"), ("successor", "old")],
    ids=["successor-after-logout-with-old", "graced-old-after-logout"],
)
async def test_no_token_of_a_logged_out_chain_refreshes(
    logged_out: str, presented_after: str
) -> None:
    # Whichever token of the session logout was given, neither the successor nor
    # the rotated predecessor -- still inside the reuse grace window -- can mint a
    # new pair afterwards (issue #3249).
    uow = FakeUnitOfWork()
    user, chain = UserId.new(), RotationChainId.new()
    _seed(
        uow,
        secret="old",
        user_id=user,
        chain_id=chain,
        revoked_at=_NOW - dt.timedelta(seconds=5),
        revoked_reason=REVOKED_ROTATED,
    )
    _seed(uow, secret="successor", user_id=user, chain_id=chain)
    await _logout(uow)(refresh_token=logged_out)

    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(uow)(refresh_token=presented_after)

    assert await uow.refresh_tokens.list_active_for_user(user, now=_NOW) == []
