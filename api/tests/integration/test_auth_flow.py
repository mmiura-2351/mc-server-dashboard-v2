"""Integration tests for the auth use cases against PostgreSQL.

Exercises the refresh-token DB paths end to end with the real SqlAlchemy
UnitOfWork and JWT TokenService: login issues a persisted pair, refresh rotates
it (old token rejected afterwards), and reuse of a rotated token past the grace
window revokes the family. Runs only when ``MCD_TEST_DATABASE_URL`` is set (CI
Postgres service).
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.clock import SystemClock
from mc_server_dashboard_api.identity.adapters.login_attempt_store import (
    SqlAlchemyLoginAttemptStore,
)
from mc_server_dashboard_api.identity.adapters.login_failure_delay import (
    FixedLoginFailureDelay,
)
from mc_server_dashboard_api.identity.adapters.password_hasher import (
    Argon2PasswordHasher,
)
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.identity.application.login import Login
from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.application.restore_session import RestoreSession
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from tests.identity.fakes import (
    FakeClock,
    RecordingSleeper,
    make_brute_force_config,
)
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_PASSWORD = "Wm7!qz#Lp2vT"
_REFRESH_TTL = dt.timedelta(days=14)
_REUSE_GRACE = dt.timedelta(seconds=60)
# Static dummy hash for the unknown-user verify path (login timing equalization).
_DUMMY_HASH = Argon2PasswordHasher().hash("dummy")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


def _tokens() -> JwtTokenService:
    return JwtTokenService(
        signing_key="integration-key",
        algorithm="HS256",
        access_ttl=dt.timedelta(minutes=15),
        clock=SystemClock(),
    )


async def _seed_user(engine: AsyncEngine) -> User:
    hasher = Argon2PasswordHasher()
    now = dt.datetime.now(tz=dt.timezone.utc)
    user = User(
        id=UserId.new(),
        username=Username("alice"),
        email=EmailAddress("alice@example.com"),
        password_hash=hasher.hash(_PASSWORD),
        created_at=now,
        updated_at=now,
    )
    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()
    return user


async def test_login_then_rotate_then_reuse(engine: AsyncEngine) -> None:
    await _seed_user(engine)
    factory = create_session_factory(engine)

    login = Login(
        uow=SqlAlchemyUnitOfWork(factory),
        attempts=SqlAlchemyLoginAttemptStore(factory),
        brute_force=make_brute_force_config(),
        hasher=Argon2PasswordHasher(),
        dummy_password_hash=_DUMMY_HASH,
        tokens=_tokens(),
        clock=SystemClock(),
        failure_delay=FixedLoginFailureDelay(
            delay=dt.timedelta(), sleeper=RecordingSleeper()
        ),
        refresh_ttl=_REFRESH_TTL,
    )
    pair = (await login(username="alice", password=_PASSWORD)).pair

    # A controllable clock lets the test step the reuse past the grace window so
    # it exercises the theft path (family revoke) deterministically (issue #369).
    clock = FakeClock(dt.datetime.now(tz=dt.timezone.utc))
    refresh = RefreshSession(
        uow=SqlAlchemyUnitOfWork(factory),
        tokens=_tokens(),
        clock=clock,
        refresh_ttl=_REFRESH_TTL,
        reuse_grace=_REUSE_GRACE,
    )
    rotated = await refresh(refresh_token=pair.refresh_token)
    assert rotated.refresh_token != pair.refresh_token

    # Step past the grace window so re-presenting the rotated token is treated as
    # theft, not a concurrent refresh.
    clock.set(clock.now() + _REUSE_GRACE + dt.timedelta(seconds=1))

    # The old (now-rotated) token must be rejected.
    with pytest.raises(InvalidRefreshTokenError):
        await refresh(refresh_token=pair.refresh_token)

    # That reuse revoked the whole family, including the rotated successor, *at
    # the current clock time*. The successor was revoked by a *family* revoke, not
    # a rotation, so re-presenting it must fail immediately -- no clock step --
    # even though its revocation is only an instant old: a family-revoked token is
    # never graced (issue #369). This pins the fix that closes the hole where an
    # attacker auto-refreshing within the window escaped the family revoke.
    with pytest.raises(InvalidRefreshTokenError):
        await refresh(refresh_token=rotated.refresh_token)


async def test_restore_yields_access_token_and_leaves_family_intact(
    engine: AsyncEngine,
) -> None:
    # Issue #512: the non-rotating bootstrap. Restore validates the persisted
    # refresh token and mints an access token WITHOUT rotating it, so repeated
    # restores never touch the family and the cookie stays valid. The subsequent
    # in-session /refresh still rotates and reuse-detection still applies there.
    user = await _seed_user(engine)
    factory = create_session_factory(engine)

    login = Login(
        uow=SqlAlchemyUnitOfWork(factory),
        attempts=SqlAlchemyLoginAttemptStore(factory),
        brute_force=make_brute_force_config(),
        hasher=Argon2PasswordHasher(),
        dummy_password_hash=_DUMMY_HASH,
        tokens=_tokens(),
        clock=SystemClock(),
        failure_delay=FixedLoginFailureDelay(
            delay=dt.timedelta(), sleeper=RecordingSleeper()
        ),
        refresh_ttl=_REFRESH_TTL,
    )
    pair = (await login(username="alice", password=_PASSWORD)).pair

    restore = RestoreSession(
        uow=SqlAlchemyUnitOfWork(factory), tokens=_tokens(), clock=SystemClock()
    )
    # Restore twice against the same live token: both succeed (idempotent, no
    # torn-rotation race) and the access token resolves back to the same user.
    first = await restore(refresh_token=pair.refresh_token)
    second = await restore(refresh_token=pair.refresh_token)
    assert _tokens().verify_access_token(first) == user.id
    assert _tokens().verify_access_token(second) == user.id

    # The refresh token was never rotated by restore, so the original still
    # rotates normally on the in-session refresh path — the family is intact.
    refresh = RefreshSession(
        uow=SqlAlchemyUnitOfWork(factory),
        tokens=_tokens(),
        clock=SystemClock(),
        refresh_ttl=_REFRESH_TTL,
        reuse_grace=_REUSE_GRACE,
    )
    rotated = await refresh(refresh_token=pair.refresh_token)
    assert rotated.refresh_token != pair.refresh_token


async def test_restore_with_revoked_token_does_not_revoke_family(
    engine: AsyncEngine,
) -> None:
    # A rotated predecessor re-presented to restore is rejected with the plain
    # invalid-token error and triggers NO family revoke — restore has no rotation
    # to disambiguate, so it never walks the theft path (issue #512). The rotated
    # successor stays usable.
    user = await _seed_user(engine)
    factory = create_session_factory(engine)

    login = Login(
        uow=SqlAlchemyUnitOfWork(factory),
        attempts=SqlAlchemyLoginAttemptStore(factory),
        brute_force=make_brute_force_config(),
        hasher=Argon2PasswordHasher(),
        dummy_password_hash=_DUMMY_HASH,
        tokens=_tokens(),
        clock=SystemClock(),
        failure_delay=FixedLoginFailureDelay(
            delay=dt.timedelta(), sleeper=RecordingSleeper()
        ),
        refresh_ttl=_REFRESH_TTL,
    )
    pair = (await login(username="alice", password=_PASSWORD)).pair

    refresh = RefreshSession(
        uow=SqlAlchemyUnitOfWork(factory),
        tokens=_tokens(),
        clock=SystemClock(),
        refresh_ttl=_REFRESH_TTL,
        reuse_grace=_REUSE_GRACE,
    )
    rotated = await refresh(refresh_token=pair.refresh_token)

    restore = RestoreSession(
        uow=SqlAlchemyUnitOfWork(factory), tokens=_tokens(), clock=SystemClock()
    )
    # The original token is now rotation-revoked; restore rejects it...
    with pytest.raises(InvalidRefreshTokenError):
        await restore(refresh_token=pair.refresh_token)

    # ...but did NOT revoke the family: the rotated successor still restores fine.
    assert (
        _tokens().verify_access_token(
            await restore(refresh_token=rotated.refresh_token)
        )
        == user.id
    )


async def test_brute_force_lockout_then_backoff_growth(engine: AsyncEngine) -> None:
    """End-to-end on Postgres: lockout after N failures, then exponential growth.

    Drives the real LoginAttemptStore + UnitOfWork with a controllable clock so
    the lockout window can be advanced deterministically (no real sleeping). It
    is the scripted evidence for the FR-AUTH-4 algorithm against the DB.
    """

    await _seed_user(engine)
    factory = create_session_factory(engine)
    clock = FakeClock(dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc))
    store = SqlAlchemyLoginAttemptStore(factory)
    # threshold 3, base lockout 60s so the test can step past it cheaply.
    config = make_brute_force_config(
        username_threshold=3,
        lockout_base=dt.timedelta(seconds=60),
    )

    def _login() -> Login:
        return Login(
            uow=SqlAlchemyUnitOfWork(factory),
            attempts=store,
            brute_force=config,
            hasher=Argon2PasswordHasher(),
            dummy_password_hash=_DUMMY_HASH,
            tokens=_tokens(),
            clock=clock,
            failure_delay=FixedLoginFailureDelay(
                delay=dt.timedelta(), sleeper=RecordingSleeper()
            ),
            refresh_ttl=_REFRESH_TTL,
        )

    # 3 wrong passwords trip the per-username threshold and lock the account.
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            await _login()(username="alice", password="wrong", ip="198.51.100.5")

    first = await store.get_lockout("alice")
    assert first is not None
    assert first.lockout_count == 1
    locked_at = clock.now()
    assert first.locked_until == locked_at + dt.timedelta(seconds=60)

    # Even the correct password is rejected while the lockout is active.
    with pytest.raises(InvalidCredentialsError):
        await _login()(username="alice", password=_PASSWORD, ip="198.51.100.5")

    # Step past the first lockout; the next batch of failures re-locks with a
    # doubled (back-off) duration.
    clock.set(first.locked_until + dt.timedelta(seconds=1))
    for _ in range(3):
        with pytest.raises(InvalidCredentialsError):
            await _login()(username="alice", password="wrong", ip="198.51.100.5")

    second = await store.get_lockout("alice")
    assert second is not None
    assert second.lockout_count == 2
    # base * 2**1 = 120s.
    assert second.locked_until == clock.now() + dt.timedelta(seconds=120)

    # A successful login after the lockout elapses clears the back-off state.
    clock.set(second.locked_until + dt.timedelta(seconds=1))
    await _login()(username="alice", password=_PASSWORD, ip="198.51.100.5")
    assert await store.get_lockout("alice") is None
