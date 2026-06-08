"""Unit tests for the RefreshSession use case (rotation + reuse policy).

Covers a valid rotation (old revoked, new pair issued in one commit), rejection
of expired/unknown tokens, and the reuse-after-rotation policy: within the grace
window a re-presented rotated token is a legitimate concurrent refresh (fresh
pair, family kept); outside the window it revokes the whole family.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_FAMILY,
    REVOKED_LOGOUT,
    REVOKED_ROTATED,
    REVOKED_SUPERSEDED,
    RefreshToken,
)
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidRefreshTokenError,
    RefreshTokenReuseError,
)
from mc_server_dashboard_api.identity.domain.value_objects import RefreshTokenId, UserId
from tests.identity.fakes import FakeClock, FakeTokenService, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_REFRESH_TTL = dt.timedelta(days=14)
_REUSE_GRACE = dt.timedelta(seconds=60)
_USER = UserId.new()


def _refresh(uow: FakeUnitOfWork, clock: FakeClock) -> RefreshSession:
    return RefreshSession(
        uow=uow,
        tokens=FakeTokenService(),
        clock=clock,
        refresh_ttl=_REFRESH_TTL,
        reuse_grace=_REUSE_GRACE,
    )


def _seed_token(
    uow: FakeUnitOfWork,
    *,
    secret: str = "old-secret",
    expires_at: dt.datetime | None = None,
    revoked_at: dt.datetime | None = None,
    revoked_reason: str | None = None,
) -> str:
    # A revoked seed defaults to ``rotated`` (the graceable cause) unless the test
    # overrides it; an unrevoked seed has no reason.
    if revoked_at is not None and revoked_reason is None:
        revoked_reason = REVOKED_ROTATED
    token_hash = f"hash::{secret}"
    uow.refresh_tokens.seed(
        RefreshToken(
            id=RefreshTokenId.new(),
            user_id=_USER,
            token_hash=token_hash,
            issued_at=_NOW - dt.timedelta(days=1),
            expires_at=expires_at or (_NOW + _REFRESH_TTL),
            revoked_at=revoked_at,
            revoked_reason=revoked_reason,
        )
    )
    return token_hash


async def test_rotation_revokes_old_and_issues_new_pair() -> None:
    uow = FakeUnitOfWork()
    old_hash = _seed_token(uow, secret="old-secret")
    clock = FakeClock(_NOW)

    pair = await _refresh(uow, clock)(refresh_token="old-secret")

    assert pair.access_token == f"access::{_USER.value}"
    assert pair.refresh_token == "refresh-secret-1"
    # Old token now revoked, stamped as a *rotation* so a re-presentation within
    # the grace window is graceable (issue #369).
    assert uow.refresh_tokens.by_hash[old_hash].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash[old_hash].revoked_reason == REVOKED_ROTATED
    new = uow.refresh_tokens.by_hash["hash::refresh-secret-1"]
    assert new.user_id == _USER
    assert new.revoked_at is None
    assert uow.commits == 1


async def test_unknown_token_is_rejected() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(uow, FakeClock(_NOW))(refresh_token="nope")
    assert uow.commits == 0


async def test_expired_token_is_rejected() -> None:
    uow = FakeUnitOfWork()
    _seed_token(uow, secret="old-secret", expires_at=_NOW - dt.timedelta(seconds=1))
    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(uow, FakeClock(_NOW))(refresh_token="old-secret")
    assert uow.commits == 0


async def test_reuse_within_grace_window_rotates_without_revoking_family() -> None:
    # The predecessor was rotated 30 s ago (inside the 60 s grace window): a
    # concurrent refresh / lost-response retry, not theft. Issue a fresh pair and
    # leave the sibling session active (issue #369).
    uow = FakeUnitOfWork()
    reused_hash = _seed_token(
        uow, secret="old-secret", revoked_at=_NOW - dt.timedelta(seconds=30)
    )
    sibling_hash = _seed_token(uow, secret="sibling-secret")
    clock = FakeClock(_NOW)

    pair = await _refresh(uow, clock)(refresh_token="old-secret")

    # A fresh, active pair is issued.
    assert pair.access_token == f"access::{_USER.value}"
    assert pair.refresh_token == "refresh-secret-1"
    assert uow.refresh_tokens.by_hash["hash::refresh-secret-1"].revoked_at is None
    # The family is untouched: the sibling stays usable and the predecessor keeps
    # its original revocation time.
    assert uow.refresh_tokens.by_hash[sibling_hash].revoked_at is None
    assert uow.refresh_tokens.by_hash[reused_hash].revoked_at == _NOW - dt.timedelta(
        seconds=30
    )
    assert uow.commits == 1


async def test_reuse_at_grace_window_boundary_rotates_without_revoking_family() -> None:
    # Exactly at the window edge (60 s) the reuse is still treated as legitimate
    # (the comparison is inclusive).
    uow = FakeUnitOfWork()
    _seed_token(uow, secret="old-secret", revoked_at=_NOW - _REUSE_GRACE)
    sibling_hash = _seed_token(uow, secret="sibling-secret")
    clock = FakeClock(_NOW)

    pair = await _refresh(uow, clock)(refresh_token="old-secret")

    assert pair.refresh_token == "refresh-secret-1"
    assert uow.refresh_tokens.by_hash[sibling_hash].revoked_at is None


async def test_expired_token_within_grace_window_is_rejected() -> None:
    # A token rotated inside the grace window but already past its expiry is still
    # rejected: the expiry check comes first, the window never resurrects it.
    uow = FakeUnitOfWork()
    _seed_token(
        uow,
        secret="old-secret",
        expires_at=_NOW - dt.timedelta(seconds=1),
        revoked_at=_NOW - dt.timedelta(seconds=30),
    )
    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(uow, FakeClock(_NOW))(refresh_token="old-secret")
    assert uow.commits == 0


async def test_reuse_after_rotation_revokes_the_family() -> None:
    # Two live tokens for the user; the first was rotated well outside the grace
    # window (2 min ago), so the reuse is treated as theft.
    uow = FakeUnitOfWork()
    reused_hash = _seed_token(
        uow, secret="old-secret", revoked_at=_NOW - dt.timedelta(minutes=2)
    )
    sibling_hash = _seed_token(uow, secret="sibling-secret")
    clock = FakeClock(_NOW)

    with pytest.raises(RefreshTokenReuseError) as exc_info:
        await _refresh(uow, clock)(refresh_token="old-secret")

    # The reuse error carries the affected user so the route can attribute the
    # family-revocation audit record (FR-AUD-1).
    assert exc_info.value.user_id == _USER.value
    # It stays an InvalidRefreshTokenError so the edge keeps the uniform 401.
    assert isinstance(exc_info.value, InvalidRefreshTokenError)
    # The still-active sibling is revoked too (family revoke), and committed.
    assert uow.refresh_tokens.by_hash[sibling_hash].revoked_at == _NOW
    # The already-revoked token keeps its original revocation time.
    assert uow.refresh_tokens.by_hash[reused_hash].revoked_at == _NOW - dt.timedelta(
        minutes=2
    )
    assert uow.commits == 1


async def test_family_revoked_token_within_grace_is_rejected() -> None:
    # The security fix (issue #369): a token revoked by a *family* revoke (theft
    # response) must NOT be graced even if the revoke happened a moment ago.
    # Otherwise an attacker who re-presents a just-family-revoked successor within
    # the window would get a fresh pair and escape the theft response. Reason is
    # ``family``, revoked 5 s ago (well inside the 60 s window).
    uow = FakeUnitOfWork()
    reused_hash = _seed_token(
        uow,
        secret="stolen-successor",
        revoked_at=_NOW - dt.timedelta(seconds=5),
        revoked_reason=REVOKED_FAMILY,
    )
    clock = FakeClock(_NOW)

    with pytest.raises(RefreshTokenReuseError) as exc_info:
        await _refresh(uow, clock)(refresh_token="stolen-successor")

    # Stays on the theft path: a DENIED security event attributed to the user, no
    # fresh pair issued. Re-revoking the already-dead family is a no-op, so the
    # token keeps its original revocation time.
    assert exc_info.value.user_id == _USER.value
    assert isinstance(exc_info.value, InvalidRefreshTokenError)
    assert "hash::refresh-secret-1" not in uow.refresh_tokens.by_hash
    assert uow.refresh_tokens.by_hash[reused_hash].revoked_at == _NOW - dt.timedelta(
        seconds=5
    )
    assert uow.commits == 1


async def test_logout_revoked_token_within_grace_is_rejected() -> None:
    # A logout-revoked token is likewise not a rotation, so re-presenting it
    # within the window does not grace it: logout ended that session deliberately.
    uow = FakeUnitOfWork()
    _seed_token(
        uow,
        secret="logged-out",
        revoked_at=_NOW - dt.timedelta(seconds=5),
        revoked_reason=REVOKED_LOGOUT,
    )
    clock = FakeClock(_NOW)

    with pytest.raises(RefreshTokenReuseError):
        await _refresh(uow, clock)(refresh_token="logged-out")

    assert "hash::refresh-secret-1" not in uow.refresh_tokens.by_hash
    assert uow.commits == 1


async def test_superseded_cookie_token_is_revoked_and_successor_stays_active() -> None:
    # Both-transports refresh: the body token rotates as today, and the
    # cookie-carried token (same family, lost precedence) is revoked as a benign
    # *superseded* token -- a single-token revoke, never the family path -- so the
    # just-issued successor stays active (issue #384).
    uow = FakeUnitOfWork()
    body_hash = _seed_token(uow, secret="body-token")
    cookie_hash = _seed_token(uow, secret="cookie-token")
    clock = FakeClock(_NOW)

    pair = await _refresh(uow, clock)(
        refresh_token="body-token", superseded_token="cookie-token"
    )

    # The body token rotated normally.
    assert uow.refresh_tokens.by_hash[body_hash].revoked_reason == REVOKED_ROTATED
    # The superseded cookie token is now revoked, with the non-grace reason.
    assert uow.refresh_tokens.by_hash[cookie_hash].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash[cookie_hash].revoked_reason == REVOKED_SUPERSEDED
    # The successor the client now holds is untouched (family not nuked).
    successor = uow.refresh_tokens.by_hash[f"hash::{pair.refresh_token}"]
    assert successor.revoked_at is None
    assert uow.commits == 1


async def test_superseded_revoke_is_single_token_not_family() -> None:
    # The superseded revoke must not cascade: an unrelated sibling session of the
    # same user stays active (it would die if this went through the family path).
    uow = FakeUnitOfWork()
    _seed_token(uow, secret="body-token")
    _seed_token(uow, secret="cookie-token")
    sibling_hash = _seed_token(uow, secret="sibling")
    clock = FakeClock(_NOW)

    await _refresh(uow, clock)(
        refresh_token="body-token", superseded_token="cookie-token"
    )

    assert uow.refresh_tokens.by_hash[sibling_hash].revoked_at is None


async def test_superseded_equal_to_body_token_is_not_double_revoked() -> None:
    # Same token in both transports: it rotated as the body token, so it must keep
    # its ``rotated`` reason -- not be overwritten with ``superseded`` -- so a
    # grace-window concurrent refresh still works.
    uow = FakeUnitOfWork()
    body_hash = _seed_token(uow, secret="same-token")
    clock = FakeClock(_NOW)

    await _refresh(uow, clock)(
        refresh_token="same-token", superseded_token="same-token"
    )

    assert uow.refresh_tokens.by_hash[body_hash].revoked_reason == REVOKED_ROTATED


async def test_already_revoked_superseded_token_is_ignored() -> None:
    # An already-revoked (e.g. logout/family) cookie token alongside a valid body
    # token must not error or be re-stamped: the refresh still succeeds and the
    # stale row keeps its original reason and time.
    uow = FakeUnitOfWork()
    _seed_token(uow, secret="body-token")
    cookie_hash = _seed_token(
        uow,
        secret="cookie-token",
        revoked_at=_NOW - dt.timedelta(minutes=2),
        revoked_reason=REVOKED_LOGOUT,
    )
    clock = FakeClock(_NOW)

    pair = await _refresh(uow, clock)(
        refresh_token="body-token", superseded_token="cookie-token"
    )

    assert pair.refresh_token == "refresh-secret-1"
    stale = uow.refresh_tokens.by_hash[cookie_hash]
    assert stale.revoked_reason == REVOKED_LOGOUT
    assert stale.revoked_at == _NOW - dt.timedelta(minutes=2)


async def test_unknown_superseded_token_is_ignored() -> None:
    # A malformed / never-issued cookie token alongside a valid body token must not
    # fail the refresh.
    uow = FakeUnitOfWork()
    _seed_token(uow, secret="body-token")
    clock = FakeClock(_NOW)

    pair = await _refresh(uow, clock)(
        refresh_token="body-token", superseded_token="never-issued"
    )

    assert pair.refresh_token == "refresh-secret-1"
    assert "hash::never-issued" not in uow.refresh_tokens.by_hash
