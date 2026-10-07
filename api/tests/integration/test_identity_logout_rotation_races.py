"""No interleaving of a revocation and a refresh may leave a session alive.

A refresh rotated server-side whose response is still in flight when the browser
logs out with the old cookie used to leave its successor valid; the late
``Set-Cookie`` then installed it over the next user's cookie (#3249). Logout now
revokes the presented token's whole rotation chain. The same race hit every
revocation meant to end sessions a rotation may be extending -- the bulk
revocations of a password change, deactivation, account deletion, theft response
or "revoke all other sessions", and the superseded cookie of a both-transports
logout (#3251). Each of them serializes with rotation on the owner's session
lock, so every successor -- committed, or still being minted -- dies with the
session. So do the last three paths that create or end a session (#3254): the
single-session revoke ends the session's whole chain, the superseded cookie of a
both-transports refresh has its chain revoked as logout's does, and a login
re-checks the password and the active flag under the lock, so a password change,
deactivation or deletion either sees its new session or refuses it.

Each test pauses one use case before it commits -- after it has read the token
it acts on and written what it decided -- then starts the other on its own
connection, waits until it has either committed or blocked on a lock, and only
then resumes the paused one. The interleaving is therefore explicit, not timed.
Afterwards no token of the chain may refresh.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5), mirroring
``test_identity_admin_invariant_races.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.repositories import (
    SqlAlchemyRefreshTokenRepository,
)
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.identity.application.admin_delete_user import (
    AdminDeleteUser,
)
from mc_server_dashboard_api.identity.application.change_password import (
    ChangePassword,
)
from mc_server_dashboard_api.identity.application.login import Login, LoginResult
from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.application.refresh_session import (
    RefreshSession,
)
from mc_server_dashboard_api.identity.application.revoke_other_sessions import (
    RevokeOtherSessions,
)
from mc_server_dashboard_api.identity.application.revoke_session import (
    RevokeSession,
)
from mc_server_dashboard_api.identity.application.set_user_active import (
    SetUserActive,
)
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_ROTATED,
    RefreshToken,
)
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
)
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.token_service import IssuedRefreshToken
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    RotationChainId,
    UserId,
)
from tests.identity.fakes import (
    FakeClock,
    FakeCommunityOwnership,
    FakeLoginAttemptStore,
    FakeTokenService,
    RecordingFailureDelay,
    StubHasher,
    make_brute_force_config,
    make_user,
)
from tests.integration.races import SETTLE_TIMEOUT, await_settled, race_database

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_REFRESH_TTL = dt.timedelta(days=14)

# The secret of the user's independent second sign-in.
_OTHER_DEVICE = "other-device"
# The secret of a third session's token rotated long past the grace window:
# presenting it is the theft response.
_STALE = "stale"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    """Holds a use case at its pause point until released."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        if self.reached.is_set():
            return
        self.reached.set()
        await self.resume.wait()


class _PausingRefreshTokenRepository(SqlAlchemyRefreshTokenRepository):
    """Pauses a rotation before staging its successor, a revocation after revoking.

    A rotation reaches ``add`` after it has revoked the presented token (when
    that token was still active), so any paused use case holds every lock it
    takes before it commits.
    """

    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def add(self, token: RefreshToken) -> None:
        await self._pause.hold()
        await super().add(token)

    async def revoke_chain(
        self, chain_id: RotationChainId, *, revoked_at: dt.datetime, reason: str
    ) -> None:
        await super().revoke_chain(chain_id, revoked_at=revoked_at, reason=reason)
        await self._pause.hold()

    async def revoke_all_for_user(
        self, user_id: UserId, *, revoked_at: dt.datetime
    ) -> None:
        await super().revoke_all_for_user(user_id, revoked_at=revoked_at)
        await self._pause.hold()

    async def revoke_all_for_user_except(
        self,
        user_id: UserId,
        *,
        keep_token_hash: str | None,
        keep_session_id: RefreshTokenId | None = None,
        revoked_at: dt.datetime,
        reason: str,
    ) -> None:
        await super().revoke_all_for_user_except(
            user_id,
            keep_token_hash=keep_token_hash,
            keep_session_id=keep_session_id,
            revoked_at=revoked_at,
            reason=reason,
        )
        await self._pause.hold()

    async def revoke_by_id(
        self,
        token_id: RefreshTokenId,
        user_id: UserId,
        *,
        revoked_at: dt.datetime,
        reason: str,
    ) -> bool:
        revoked = await super().revoke_by_id(
            token_id, user_id, revoked_at=revoked_at, reason=reason
        )
        await self._pause.hold()
        return revoked


class _PausingUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], pause: _Pause
    ) -> None:
        super().__init__(session_factory)
        self._pause = pause

    async def __aenter__(self) -> _PausingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.refresh_tokens = _PausingRefreshTokenRepository(self._session, self._pause)
        return self


class _Backend:
    """The backend pid of a competitor's current transaction, once it has one."""

    pid: int | None = None


class _TrackedUnitOfWork(SqlAlchemyUnitOfWork):
    """Records its transaction's backend pid."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], backend: _Backend
    ) -> None:
        super().__init__(session_factory)
        self._backend = backend

    async def __aenter__(self) -> _TrackedUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self._backend.pid = (
            await self._session.execute(text("SELECT pg_backend_pid()"))
        ).scalar_one()
        return self


class _TokenService(FakeTokenService):
    """Mints refresh secrets under its own prefix.

    Two use cases that both mint in one test must not mint the same secret: the
    token hash is unique.
    """

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix

    def issue_refresh_token(self) -> IssuedRefreshToken:
        secret = f"{self._prefix}-{super().issue_refresh_token().secret}"
        return IssuedRefreshToken(
            secret=secret, token_hash=self.hash_refresh_token(secret)
        )


def _refresh(uow: SqlAlchemyUnitOfWork, prefix: str = "rotation") -> RefreshSession:
    return RefreshSession(
        uow=uow,
        tokens=_TokenService(prefix),
        clock=FakeClock(_NOW),
        refresh_ttl=_REFRESH_TTL,
        reuse_grace=dt.timedelta(seconds=60),
    )


def _logout(uow: SqlAlchemyUnitOfWork) -> Logout:
    return Logout(uow=uow, tokens=FakeTokenService(), clock=FakeClock(_NOW))


async def _seed(engine: AsyncEngine) -> UserId:
    """Seed one user with a rotated session (``old`` -> ``current``) and others.

    ``old`` was rotated five seconds ago, inside the reuse grace window, so it is
    still graceable; ``current`` is its live successor. ``other-device`` is an
    independent sign-in of the same user, and ``stale`` the predecessor of a
    third one, rotated an hour ago. Returns the user's id.
    """

    user = make_user()
    chain = RotationChainId.new()

    def token(secret: str, chain_id: RotationChainId) -> RefreshToken:
        return RefreshToken(
            id=RefreshTokenId.new(),
            user_id=user.id,
            chain_id=chain_id,
            token_hash=f"hash::{secret}",
            issued_at=_NOW - dt.timedelta(minutes=1),
            expires_at=_NOW + _REFRESH_TTL,
        )

    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.refresh_tokens.add(token("old", chain))
        await uow.refresh_tokens.add(token("current", chain))
        await uow.refresh_tokens.add(token(_OTHER_DEVICE, RotationChainId.new()))
        await uow.refresh_tokens.add(token(_STALE, RotationChainId.new()))
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.refresh_tokens.revoke(
            "hash::old",
            revoked_at=_NOW - dt.timedelta(seconds=5),
            reason=REVOKED_ROTATED,
        )
        await uow.refresh_tokens.revoke(
            f"hash::{_STALE}",
            revoked_at=_NOW - dt.timedelta(hours=1),
            reason=REVOKED_ROTATED,
        )
        await uow.commit()
    return user.id


_UseCase = Callable[[SqlAlchemyUnitOfWork], Coroutine[Any, Any, object]]


async def _interleave(
    engine: AsyncEngine, paused: _UseCase, competitor: _UseCase
) -> tuple[object, object]:
    """Pause ``paused`` before it commits, settle ``competitor``, then resume.

    Returns each use case's outcome (its result or the exception it raised),
    paused one first.
    """

    factory = create_session_factory(engine)
    pause = _Pause()
    first = asyncio.create_task(paused(_PausingUnitOfWork(factory, pause)))
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)

    backend = _Backend()
    second = asyncio.create_task(competitor(_TrackedUnitOfWork(factory, backend)))
    await await_settled(engine, second, lambda: backend.pid)

    pause.resume.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), SETTLE_TIMEOUT
    )
    # A refresh refused because its session ended, or a login refused because
    # its credentials no longer hold, is the only acceptable failure: anything
    # else (a deadlock, a serialization error) is a bug.
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(
            outcome, (InvalidRefreshTokenError, InvalidCredentialsError)
        ):
            raise outcome
    return outcomes[0], outcomes[1]


async def _active_secrets(engine: AsyncEngine, user: UserId) -> set[str]:
    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        active = await uow.refresh_tokens.list_active_for_user(user, now=_NOW)
    return {token.token_hash.removeprefix("hash::") for token in active}


# (token the refresh presents, token the logout presents). ``old`` refreshed is a
# grace-window reuse, which mints a successor without revoking anything.
_PAIRS = [
    ("current", "current"),
    ("current", "old"),
    ("old", "current"),
]
_PAIR_IDS = ["same-token", "logout-with-old-cookie", "graced-refresh"]


@pytest.mark.parametrize(("refreshed", "logged_out"), _PAIRS, ids=_PAIR_IDS)
async def test_logout_racing_a_paused_rotation_revokes_its_successor(
    engine: AsyncEngine, refreshed: str, logged_out: str
) -> None:
    # The rotation has decided and is about to mint its successor when the
    # logout starts. Had the logout not waited for it, the successor would be
    # committed after the logout's revoke had already chosen its rows.
    user = await _seed(engine)

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    async def logout(uow: SqlAlchemyUnitOfWork) -> None:
        await _logout(uow)(refresh_token=logged_out)

    pair, logout_outcome = await _interleave(engine, rotate, logout)

    assert logout_outcome is None
    assert await _active_secrets(engine, user) == {_OTHER_DEVICE}
    # The successor the rotation delivered -- the late Set-Cookie -- is refused.
    assert isinstance(pair, TokenPair)
    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(SqlAlchemyUnitOfWork(create_session_factory(engine)))(
            refresh_token=pair.refresh_token
        )


@pytest.mark.parametrize(("refreshed", "logged_out"), _PAIRS, ids=_PAIR_IDS)
async def test_rotation_racing_a_paused_logout_mints_no_live_successor(
    engine: AsyncEngine, refreshed: str, logged_out: str
) -> None:
    # The logout has revoked the chain but not committed when the refresh starts.
    # Had the refresh not waited for it, it would have read the presented token
    # as still live and minted a successor the logout never saw.
    user = await _seed(engine)

    async def logout(uow: SqlAlchemyUnitOfWork) -> None:
        await _logout(uow)(refresh_token=logged_out)

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    logout_outcome, refresh_outcome = await _interleave(engine, logout, rotate)

    assert logout_outcome is None
    assert isinstance(refresh_outcome, InvalidRefreshTokenError)
    assert await _active_secrets(engine, user) <= {_OTHER_DEVICE}


async def _assert_refused(engine: AsyncEngine, outcome: object) -> None:
    """The pair a rotation delivered -- the late ``Set-Cookie`` -- cannot refresh."""

    assert isinstance(outcome, TokenPair)
    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(SqlAlchemyUnitOfWork(create_session_factory(engine)))(
            refresh_token=outcome.refresh_token
        )


_Revocation = Callable[[SqlAlchemyUnitOfWork, UserId], Coroutine[Any, Any, object]]


async def _change_password(uow: SqlAlchemyUnitOfWork, user: UserId) -> None:
    policy = PasswordPolicy(
        min_length=12,
        max_length=128,
        max_bytes=None,
        require_complexity=True,
        complexity_classes=3,
        check_common_list=True,
        forbid_user_info=True,
        forbid_simple_patterns=True,
        common_passwords=frozenset(),
    )
    await ChangePassword(
        uow=uow, hasher=StubHasher(), clock=FakeClock(_NOW), policy=policy
    )(user_id=user, current_password="Wm7!qz#Lp2vT", new_password="Np4@xZ#Lq9wR")


async def _deactivate(uow: SqlAlchemyUnitOfWork, user: UserId) -> None:
    await SetUserActive(uow=uow, clock=FakeClock(_NOW))(
        actor_id=UserId.new(), target_id=user, active=False
    )


async def _admin_delete(uow: SqlAlchemyUnitOfWork, user: UserId) -> None:
    await AdminDeleteUser(
        uow=uow, ownership=FakeCommunityOwnership(), clock=FakeClock(_NOW)
    )(actor_id=UserId.new(), target_id=user)


async def _theft_response(uow: SqlAlchemyUnitOfWork, user: UserId) -> object:
    return await _refresh(uow)(refresh_token=_STALE)


async def _revoke_other_sessions(uow: SqlAlchemyUnitOfWork, user: UserId) -> None:
    await RevokeOtherSessions(
        uow=uow, tokens=FakeTokenService(), clock=FakeClock(_NOW)
    )(user_id=user, current_refresh_token=_OTHER_DEVICE)


# (revocation, the sessions it spares).
_REVOCATIONS: list[tuple[_Revocation, set[str]]] = [
    (_change_password, set()),
    (_deactivate, set()),
    (_admin_delete, set()),
    (_theft_response, set()),
    (_revoke_other_sessions, {_OTHER_DEVICE}),
]
_REVOCATION_IDS = [
    "change-password",
    "deactivate",
    "admin-delete",
    "theft-response",
    "revoke-other-sessions",
]
# ``old`` refreshed is a grace-window reuse: it mints a successor without
# revoking -- or row-locking -- anything.
_REFRESHED = ["current", "old"]
_REFRESHED_IDS = ["active-token", "graced-refresh"]


@pytest.mark.parametrize("refreshed", _REFRESHED, ids=_REFRESHED_IDS)
@pytest.mark.parametrize(("revoke", "spared"), _REVOCATIONS, ids=_REVOCATION_IDS)
async def test_bulk_revocation_racing_a_paused_rotation_revokes_its_successor(
    engine: AsyncEngine, revoke: _Revocation, spared: set[str], refreshed: str
) -> None:
    # The rotation is about to mint its successor when the revocation starts.
    # Had the revocation not waited for it, its UPDATE would have chosen its rows
    # before the successor was committed (#3251).
    user = await _seed(engine)

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    async def revocation(uow: SqlAlchemyUnitOfWork) -> object:
        return await revoke(uow, user)

    pair, _ = await _interleave(engine, rotate, revocation)

    assert await _active_secrets(engine, user) == spared
    await _assert_refused(engine, pair)


@pytest.mark.parametrize("refreshed", _REFRESHED, ids=_REFRESHED_IDS)
@pytest.mark.parametrize(("revoke", "spared"), _REVOCATIONS, ids=_REVOCATION_IDS)
async def test_rotation_racing_a_paused_bulk_revocation_mints_no_live_successor(
    engine: AsyncEngine, revoke: _Revocation, spared: set[str], refreshed: str
) -> None:
    # The revocation has revoked the user's tokens but not committed when the
    # refresh starts. Had the refresh not waited for it, it would have read the
    # presented token as it was before and minted a successor the revocation
    # never saw (#3251).
    user = await _seed(engine)

    async def revocation(uow: SqlAlchemyUnitOfWork) -> object:
        return await revoke(uow, user)

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    _, refresh_outcome = await _interleave(engine, revocation, rotate)

    assert isinstance(refresh_outcome, InvalidRefreshTokenError)
    assert await _active_secrets(engine, user) <= spared


_STRANGER = "stranger"


async def _seed_stranger(engine: AsyncEngine) -> None:
    """Seed another user signed in with ``stranger``, say on a shared browser."""

    user = make_user(username="bob", email="bob@example.com")
    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.refresh_tokens.add(
            RefreshToken(
                id=RefreshTokenId.new(),
                user_id=user.id,
                chain_id=RotationChainId.new(),
                token_hash=f"hash::{_STRANGER}",
                issued_at=_NOW - dt.timedelta(minutes=1),
                expires_at=_NOW + _REFRESH_TTL,
            )
        )
        await uow.commit()


# (the logout's body token, the seeded user's sessions it leaves). The body token
# is the user's own other session, or another user's whose sign-in overwrote the
# user's cookie in a shared browser.
_BODIES = [(_OTHER_DEVICE, set()), (_STRANGER, {_OTHER_DEVICE})]
_BODY_IDS = ["own-body-token", "other-users-body-token"]


@pytest.mark.parametrize("refreshed", _REFRESHED, ids=_REFRESHED_IDS)
@pytest.mark.parametrize(("body", "spared"), _BODIES, ids=_BODY_IDS)
async def test_both_transports_logout_racing_a_paused_rotation_of_the_cookie(
    engine: AsyncEngine, body: str, spared: set[str], refreshed: str
) -> None:
    # The cookie's session is being rotated when a logout carrying another token
    # in its body supersedes the cookie. Revoking only the cookie token would
    # leave the successor of its chain alive (#3251).
    user = await _seed(engine)
    await _seed_stranger(engine)

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    async def logout(uow: SqlAlchemyUnitOfWork) -> None:
        await _logout(uow)(refresh_token=body, superseded_token=refreshed)

    pair, logout_outcome = await _interleave(engine, rotate, logout)

    assert logout_outcome is None
    assert await _active_secrets(engine, user) == spared
    await _assert_refused(engine, pair)


@pytest.mark.parametrize("refreshed", _REFRESHED, ids=_REFRESHED_IDS)
@pytest.mark.parametrize(("body", "spared"), _BODIES, ids=_BODY_IDS)
async def test_rotation_of_the_cookie_racing_a_paused_both_transports_logout(
    engine: AsyncEngine, body: str, spared: set[str], refreshed: str
) -> None:
    # The logout has locked both sessions and revoked the body token's when the
    # cookie's session is refreshed. Had the refresh not waited for it, it would
    # have minted a successor of the superseded cookie's chain (#3251).
    user = await _seed(engine)
    await _seed_stranger(engine)

    async def logout(uow: SqlAlchemyUnitOfWork) -> None:
        await _logout(uow)(refresh_token=body, superseded_token="current")

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    logout_outcome, refresh_outcome = await _interleave(engine, logout, rotate)

    assert logout_outcome is None
    assert isinstance(refresh_outcome, InvalidRefreshTokenError)
    assert await _active_secrets(engine, user) == spared


async def _session_id(engine: AsyncEngine, secret: str) -> RefreshTokenId:
    """The session id the session listing reports for the token ``secret``."""

    async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        token = await uow.refresh_tokens.get_by_token_hash(f"hash::{secret}")
    assert token is not None
    return token.id


@pytest.mark.parametrize("refreshed", _REFRESHED, ids=_REFRESHED_IDS)
async def test_single_session_revoke_racing_a_paused_rotation_ends_the_session(
    engine: AsyncEngine, refreshed: str
) -> None:
    # The session listed with ``current`` is being rotated when the user revokes
    # it. Revoking only the listed row would either miss it -- rotated meanwhile,
    # a 404 -- or leave the successor alive (#3254).
    user = await _seed(engine)
    session_id = await _session_id(engine, "current")

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    async def revoke(uow: SqlAlchemyUnitOfWork) -> object:
        return await RevokeSession(uow=uow, clock=FakeClock(_NOW))(
            user_id=user, session_id=session_id
        )

    pair, revoked = await _interleave(engine, rotate, revoke)

    assert revoked is True
    assert await _active_secrets(engine, user) == {_OTHER_DEVICE}
    await _assert_refused(engine, pair)


@pytest.mark.parametrize("refreshed", _REFRESHED, ids=_REFRESHED_IDS)
async def test_rotation_racing_a_paused_single_session_revoke_mints_no_successor(
    engine: AsyncEngine, refreshed: str
) -> None:
    # The revoke has ended the session but not committed when it is refreshed.
    # Had the refresh not waited for it, it would have overwritten the
    # revocation with its rotation, or graced the predecessor, and minted a live
    # successor (#3254).
    user = await _seed(engine)
    session_id = await _session_id(engine, "current")

    async def revoke(uow: SqlAlchemyUnitOfWork) -> object:
        return await RevokeSession(uow=uow, clock=FakeClock(_NOW))(
            user_id=user, session_id=session_id
        )

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    revoked, refresh_outcome = await _interleave(engine, revoke, rotate)

    assert revoked is True
    assert isinstance(refresh_outcome, InvalidRefreshTokenError)
    assert await _active_secrets(engine, user) <= {_OTHER_DEVICE}


def _login(uow: SqlAlchemyUnitOfWork) -> Login:
    return Login(
        uow=uow,
        attempts=FakeLoginAttemptStore(),
        brute_force=make_brute_force_config(),
        hasher=StubHasher(),
        dummy_password_hash="hashed::__dummy__",
        tokens=_TokenService("login"),
        clock=FakeClock(_NOW),
        failure_delay=RecordingFailureDelay(),
        refresh_ttl=_REFRESH_TTL,
    )


async def _sign_in(uow: SqlAlchemyUnitOfWork) -> object:
    return await _login(uow)(username="alice", password="Wm7!qz#Lp2vT")


# The revocations that end every session because the credentials behind them
# stopped holding.
_CREDENTIAL_REVOCATIONS = [_change_password, _deactivate, _admin_delete]
_CREDENTIAL_REVOCATION_IDS = ["change-password", "deactivate", "admin-delete"]


@pytest.mark.parametrize(
    "revoke", _CREDENTIAL_REVOCATIONS, ids=_CREDENTIAL_REVOCATION_IDS
)
async def test_revocation_racing_a_paused_login_revokes_its_session(
    engine: AsyncEngine, revoke: _Revocation
) -> None:
    # The login has checked the credentials and is about to store its session
    # when the revocation starts. Had the revocation not waited for it, it would
    # have revoked the user's sessions before the new one was committed (#3254).
    user = await _seed(engine)

    async def revocation(uow: SqlAlchemyUnitOfWork) -> object:
        return await revoke(uow, user)

    signed_in, _ = await _interleave(engine, _sign_in, revocation)

    assert isinstance(signed_in, LoginResult)
    assert await _active_secrets(engine, user) == set()
    await _assert_refused(engine, signed_in.pair)


@pytest.mark.parametrize(
    "revoke", _CREDENTIAL_REVOCATIONS, ids=_CREDENTIAL_REVOCATION_IDS
)
async def test_login_racing_a_paused_revocation_is_refused(
    engine: AsyncEngine, revoke: _Revocation
) -> None:
    # The revocation has changed the user and revoked their sessions but not
    # committed when the login checks the old credentials. Had the login not
    # waited for it and re-checked them, it would have stored a session the
    # revocation never saw (#3254).
    user = await _seed(engine)

    async def revocation(uow: SqlAlchemyUnitOfWork) -> object:
        return await revoke(uow, user)

    _, login_outcome = await _interleave(engine, revocation, _sign_in)

    assert isinstance(login_outcome, InvalidCredentialsError)
    assert await _active_secrets(engine, user) == set()


# (the both-transports refresh's body token, the seeded user's sessions it
# leaves). With the user's own other session in the body, what is left is the
# successor that refresh minted.
_REFRESH_BODIES = [
    (_OTHER_DEVICE, {"both-refresh-secret-1"}),
    (_STRANGER, {_OTHER_DEVICE}),
]


async def _refresh_both(uow: SqlAlchemyUnitOfWork, body: str, cookie: str) -> object:
    return await _refresh(uow, prefix="both")(
        refresh_token=body, superseded_token=cookie
    )


@pytest.mark.parametrize("refreshed", _REFRESHED, ids=_REFRESHED_IDS)
@pytest.mark.parametrize(("body", "spared"), _REFRESH_BODIES, ids=_BODY_IDS)
async def test_both_transports_refresh_racing_a_paused_rotation_of_the_cookie(
    engine: AsyncEngine, body: str, spared: set[str], refreshed: str
) -> None:
    # The cookie's session is being rotated when a refresh carrying another token
    # in its body supersedes the cookie. Revoking only the cookie token -- which
    # the rotation has already retired -- would leave the successor of its chain
    # alive (#3254).
    user = await _seed(engine)
    await _seed_stranger(engine)

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    async def refresh_both(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh_both(uow, body, refreshed)

    pair, both_outcome = await _interleave(engine, rotate, refresh_both)

    assert isinstance(both_outcome, TokenPair)
    assert await _active_secrets(engine, user) == spared
    await _assert_refused(engine, pair)


@pytest.mark.parametrize("refreshed", _REFRESHED, ids=_REFRESHED_IDS)
@pytest.mark.parametrize(("body", "spared"), _REFRESH_BODIES, ids=_BODY_IDS)
async def test_rotation_of_the_cookie_racing_a_paused_both_transports_refresh(
    engine: AsyncEngine, body: str, spared: set[str], refreshed: str
) -> None:
    # The both-transports refresh holds both sessions' locks when the cookie's
    # session is refreshed. Had it revoked only the cookie token, a graced
    # predecessor of the cookie's chain would mint a live successor once it
    # waited (#3254).
    user = await _seed(engine)
    await _seed_stranger(engine)

    async def refresh_both(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh_both(uow, body, "current")

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    both_outcome, refresh_outcome = await _interleave(engine, refresh_both, rotate)

    assert isinstance(both_outcome, TokenPair)
    assert isinstance(refresh_outcome, InvalidRefreshTokenError)
    assert await _active_secrets(engine, user) == spared
