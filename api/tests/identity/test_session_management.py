"""Unit tests for the session-management use cases (issue #387).

Cover the three use cases against fakes (no DB, NFR-TEST-1):

- :class:`ListSessions` returns only the caller's active sessions.
- :class:`RevokeSession` revokes only the caller's own session and reports a miss
  (so the edge can 404) for an unknown id or one owned by another user.
- :class:`RevokeOtherSessions` keeps the presented session alive and revokes the
  rest, and revoked sessions can no longer refresh.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.application.list_sessions import ListSessions
from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.application.revoke_other_sessions import (
    RevokeOtherSessions,
)
from mc_server_dashboard_api.identity.application.revoke_session import RevokeSession
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_USER,
    RefreshToken,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    UserId,
)
from tests.identity.fakes import FakeClock, FakeTokenService, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_TTL = dt.timedelta(days=14)


def _token(
    *,
    user_id: UserId,
    secret: str,
    issued_at: dt.datetime = _NOW,
    expires_at: dt.datetime | None = None,
    revoked_at: dt.datetime | None = None,
    token_id: RefreshTokenId | None = None,
) -> RefreshToken:
    return RefreshToken(
        id=token_id or RefreshTokenId.new(),
        user_id=user_id,
        token_hash=f"hash::{secret}",
        issued_at=issued_at,
        expires_at=expires_at or (issued_at + _TTL),
        revoked_at=revoked_at,
    )


# --- ListSessions ----------------------------------------------------------


async def test_list_returns_only_callers_active_sessions() -> None:
    uow = FakeUnitOfWork()
    alice = UserId.new()
    bob = UserId.new()
    active = _token(user_id=alice, secret="a-active")
    uow.refresh_tokens.seed(active)
    # Excluded: revoked, expired, and another user's token.
    uow.refresh_tokens.seed(_token(user_id=alice, secret="a-revoked", revoked_at=_NOW))
    uow.refresh_tokens.seed(
        _token(
            user_id=alice,
            secret="a-expired",
            issued_at=_NOW - 2 * _TTL,
            expires_at=_NOW - _TTL,
        )
    )
    uow.refresh_tokens.seed(_token(user_id=bob, secret="b-active"))

    use_case = ListSessions(uow=uow, clock=FakeClock(_NOW))
    sessions = await use_case(user_id=alice)

    assert [s.id for s in sessions] == [active.id]


async def test_list_orders_newest_first() -> None:
    uow = FakeUnitOfWork()
    alice = UserId.new()
    older = _token(user_id=alice, secret="older", issued_at=_NOW - dt.timedelta(days=1))
    newer = _token(user_id=alice, secret="newer", issued_at=_NOW)
    uow.refresh_tokens.seed(older)
    uow.refresh_tokens.seed(newer)

    sessions = await ListSessions(uow=uow, clock=FakeClock(_NOW))(user_id=alice)

    assert [s.id for s in sessions] == [newer.id, older.id]


# --- RevokeSession ---------------------------------------------------------


async def test_revoke_one_revokes_only_the_target_with_user_reason() -> None:
    uow = FakeUnitOfWork()
    alice = UserId.new()
    target = _token(user_id=alice, secret="target")
    other = _token(user_id=alice, secret="other")
    uow.refresh_tokens.seed(target)
    uow.refresh_tokens.seed(other)

    revoked = await RevokeSession(uow=uow, clock=FakeClock(_NOW))(
        user_id=alice, session_id=target.id
    )

    assert revoked is True
    assert uow.refresh_tokens.by_hash["hash::target"].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash["hash::target"].revoked_reason == REVOKED_USER
    assert uow.refresh_tokens.by_hash["hash::other"].revoked_at is None
    assert uow.commits == 1


async def test_revoke_one_unknown_id_reports_miss() -> None:
    uow = FakeUnitOfWork()
    alice = UserId.new()
    uow.refresh_tokens.seed(_token(user_id=alice, secret="alice"))

    revoked = await RevokeSession(uow=uow, clock=FakeClock(_NOW))(
        user_id=alice, session_id=RefreshTokenId.new()
    )

    assert revoked is False


async def test_revoke_one_other_users_session_reports_miss_and_keeps_it() -> None:
    uow = FakeUnitOfWork()
    alice = UserId.new()
    bob = UserId.new()
    bobs = _token(user_id=bob, secret="bobs")
    uow.refresh_tokens.seed(bobs)

    # Alice tries to revoke Bob's session by its real id: a miss, and Bob's
    # session stays active (cross-user isolation).
    revoked = await RevokeSession(uow=uow, clock=FakeClock(_NOW))(
        user_id=alice, session_id=bobs.id
    )

    assert revoked is False
    assert uow.refresh_tokens.by_hash["hash::bobs"].revoked_at is None


# --- RevokeOtherSessions ---------------------------------------------------


def _revoke_others(uow: FakeUnitOfWork) -> RevokeOtherSessions:
    return RevokeOtherSessions(
        uow=uow, tokens=FakeTokenService(), clock=FakeClock(_NOW)
    )


async def test_revoke_others_keeps_current_and_revokes_rest() -> None:
    uow = FakeUnitOfWork()
    alice = UserId.new()
    bob = UserId.new()
    current = _token(user_id=alice, secret="current")
    other = _token(user_id=alice, secret="other")
    bobs = _token(user_id=bob, secret="bobs")
    for tok in (current, other, bobs):
        uow.refresh_tokens.seed(tok)

    await _revoke_others(uow)(user_id=alice, current_refresh_token="current")

    assert uow.refresh_tokens.by_hash["hash::current"].revoked_at is None
    assert uow.refresh_tokens.by_hash["hash::other"].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash["hash::other"].revoked_reason == REVOKED_USER
    # Another user's session is untouched (cross-user isolation).
    assert uow.refresh_tokens.by_hash["hash::bobs"].revoked_at is None
    assert uow.commits == 1


async def test_revoke_others_without_token_revokes_all_of_callers() -> None:
    uow = FakeUnitOfWork()
    alice = UserId.new()
    one = _token(user_id=alice, secret="one")
    two = _token(user_id=alice, secret="two")
    uow.refresh_tokens.seed(one)
    uow.refresh_tokens.seed(two)

    await _revoke_others(uow)(user_id=alice, current_refresh_token=None)

    assert uow.refresh_tokens.by_hash["hash::one"].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash["hash::two"].revoked_at == _NOW


async def test_revoked_session_can_no_longer_refresh() -> None:
    uow = FakeUnitOfWork()
    alice = UserId.new()
    current = _token(user_id=alice, secret="current")
    other = _token(user_id=alice, secret="other")
    uow.refresh_tokens.seed(current)
    uow.refresh_tokens.seed(other)

    await _revoke_others(uow)(user_id=alice, current_refresh_token="current")

    refresh = RefreshSession(
        uow=uow,
        tokens=FakeTokenService(),
        clock=FakeClock(_NOW),
        refresh_ttl=_TTL,
        reuse_grace=dt.timedelta(seconds=10),
    )
    # The revoked "other" session is user-revoked, never graced: refreshing it is
    # rejected (and trips the theft path, revoking the family).
    try:
        await refresh(refresh_token="other")
    except Exception as exc:  # noqa: BLE001 - assert the type below
        assert "RefreshToken" in type(exc).__name__
    else:  # pragma: no cover - the refresh must be rejected
        raise AssertionError("expected the revoked session to be unrefreshable")
