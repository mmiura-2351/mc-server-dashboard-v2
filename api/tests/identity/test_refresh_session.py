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


async def test_superseded_revoked_token_re_presented_is_plainly_invalid() -> None:
    # Issue #384: a *superseded* token was retired because the body token won
    # precedence on a both-transports request -- no client holds it and it is NOT
    # evidence of theft. Re-presenting it must be a plain InvalidRefreshTokenError,
    # NOT the reuse/family path: tripping family-wide revocation here would log out
    # the benign device that owned it. Unlike ``family`` / ``logout`` (which stay on
    # the theft path), ``superseded`` is exempted. Revoked 5 s ago, well inside the
    # grace window, to show recency does not pull it onto the theft path either.
    uow = FakeUnitOfWork()
    sibling_hash = _seed_token(uow, secret="sibling")
    _seed_token(
        uow,
        secret="superseded-cookie",
        revoked_at=_NOW - dt.timedelta(seconds=5),
        revoked_reason=REVOKED_SUPERSEDED,
    )
    clock = FakeClock(_NOW)

    with pytest.raises(InvalidRefreshTokenError) as exc_info:
        await _refresh(uow, clock)(refresh_token="superseded-cookie")

    # Plain invalid: not a RefreshTokenReuseError, so the family is not nuked --
    # the user's other active session survives -- and nothing is committed.
    assert not isinstance(exc_info.value, RefreshTokenReuseError)
    assert uow.refresh_tokens.by_hash[sibling_hash].revoked_at is None
    assert uow.commits == 0


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


async def test_rotated_predecessor_after_family_revoke_is_rejected() -> None:
    # Issue #1960 regression: a token rotated *before* a family revoke (password
    # change) must not be graced after the family revoke re-stamps it to 'family'.
    # Attack scenario: attacker holds stolen token A; legitimate client rotates
    # A->B at T0; user changes password at T0+10s (revokes B, re-stamps A to
    # 'family'); attacker presents A at T0+20s -- must be rejected.
    uow = FakeUnitOfWork()
    t0 = _NOW - dt.timedelta(seconds=20)

    # Token A: rotated at T0 (predecessor of the legitimate rotation A->B).
    _seed_token(uow, secret="stolen-A", revoked_at=t0, revoked_reason=REVOKED_ROTATED)
    # Token B: the successor, still active until the family revoke.
    _seed_token(uow, secret="successor-B")

    # Simulate the password change at T0+10s: revoke_all_for_user re-stamps
    # rotated tokens to 'family' (the fix) and revokes active ones.
    family_revoke_time = t0 + dt.timedelta(seconds=10)
    await uow.refresh_tokens.revoke_all_for_user(_USER, revoked_at=family_revoke_time)

    # Attacker presents A at T0+20s (== _NOW), within original grace of T0.
    clock = FakeClock(_NOW)
    with pytest.raises(RefreshTokenReuseError):
        await _refresh(uow, clock)(refresh_token="stolen-A")

    # No fresh pair issued.
    assert "hash::refresh-secret-1" not in uow.refresh_tokens.by_hash


async def test_revoke_all_restamps_rotated_to_family_preserving_revoked_at() -> None:
    # After revoke_all_for_user, a 'rotated' row is restamped to 'family' keeping
    # its original revoked_at; a 'superseded' row is untouched.
    uow = FakeUnitOfWork()
    t0 = _NOW - dt.timedelta(seconds=30)

    _seed_token(
        uow, secret="rotated-tok", revoked_at=t0, revoked_reason=REVOKED_ROTATED
    )
    _seed_token(
        uow, secret="superseded-tok", revoked_at=t0, revoked_reason=REVOKED_SUPERSEDED
    )
    _seed_token(uow, secret="active-tok")

    sweep_at = _NOW
    await uow.refresh_tokens.revoke_all_for_user(_USER, revoked_at=sweep_at)

    rotated = uow.refresh_tokens.by_hash["hash::rotated-tok"]
    superseded = uow.refresh_tokens.by_hash["hash::superseded-tok"]
    active = uow.refresh_tokens.by_hash["hash::active-tok"]

    # Rotated: reason changed to 'family', revoked_at preserved (COALESCE).
    assert rotated.revoked_reason == REVOKED_FAMILY
    assert rotated.revoked_at == t0

    # Superseded: untouched (not in the new WHERE clause).
    assert superseded.revoked_reason == REVOKED_SUPERSEDED
    assert superseded.revoked_at == t0

    # Active: newly revoked as 'family'.
    assert active.revoked_reason == REVOKED_FAMILY
    assert active.revoked_at == sweep_at
