"""No interleaving of a logout and a refresh may leave the session alive (#3249).

A refresh rotated server-side whose response is still in flight when the browser
logs out with the old cookie used to leave its successor valid; the late
``Set-Cookie`` then installed it over the next user's cookie. Logout now revokes
the presented token's whole rotation chain, and a rotation and a logout of one
chain serialize, so every successor -- committed, or still being minted -- dies
with the session.

Each test pauses one use case right before its write -- after it has read the
token it acts on -- then starts the other on its own connection, waits until it
has either committed or blocked on a lock, and only then resumes the paused one.
The interleaving is therefore explicit, not timed. Afterwards no token of the
chain may refresh, and the user's other session is untouched.

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
    create_async_engine,
)

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.repositories import (
    SqlAlchemyRefreshTokenRepository,
)
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.application.refresh_session import (
    RefreshSession,
)
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_ROTATED,
    RefreshToken,
)
from mc_server_dashboard_api.identity.domain.errors import InvalidRefreshTokenError
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    RotationChainId,
    UserId,
)
from tests.identity.fakes import FakeClock, FakeTokenService, make_user
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_REFRESH_TTL = dt.timedelta(days=14)

# Upper bound on how long a transaction may take to reach its pause point, or a
# competitor to settle (commit, or block on a lock), before the test fails
# instead of hanging.
_SETTLE_TIMEOUT = 10.0

# The secret of the user's independent second sign-in.
_OTHER_DEVICE = "other-device"


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


class _Pause:
    """Holds a use case right before its first token write until released."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        if self.reached.is_set():
            return
        self.reached.set()
        await self.resume.wait()


class _PausingRefreshTokenRepository(SqlAlchemyRefreshTokenRepository):
    """Pauses a rotation before staging its successor, a logout before revoking.

    A rotation reaches ``add`` after it has revoked the presented token (when
    that token was still active), so the paused rotation holds every lock it
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
        await self._pause.hold()
        await super().revoke_chain(chain_id, revoked_at=revoked_at, reason=reason)


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


async def _await_settled(
    engine: AsyncEngine, competitor: asyncio.Task[object], backend: _Backend
) -> None:
    """Return once ``competitor`` has finished or is blocked on a lock."""

    query = text(
        "SELECT 1 FROM pg_stat_activity WHERE pid = :pid AND wait_event_type = 'Lock'"
    )
    deadline = asyncio.get_running_loop().time() + _SETTLE_TIMEOUT
    while not competitor.done():
        if backend.pid is not None:
            async with engine.connect() as conn:
                if (await conn.execute(query, {"pid": backend.pid})).first():
                    return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("competitor neither committed nor blocked on a lock")
        await asyncio.sleep(0.02)


def _refresh(uow: SqlAlchemyUnitOfWork) -> RefreshSession:
    return RefreshSession(
        uow=uow,
        tokens=FakeTokenService(),
        clock=FakeClock(_NOW),
        refresh_ttl=_REFRESH_TTL,
        reuse_grace=dt.timedelta(seconds=60),
    )


def _logout(uow: SqlAlchemyUnitOfWork) -> Logout:
    return Logout(uow=uow, tokens=FakeTokenService(), clock=FakeClock(_NOW))


async def _seed(engine: AsyncEngine) -> UserId:
    """Seed one user with a rotated session (``old`` -> ``current``) and another.

    ``old`` was rotated five seconds ago, inside the reuse grace window, so it is
    still graceable; ``current`` is its live successor. ``other-device`` is an
    independent sign-in of the same user. Returns the user's id.
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
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.refresh_tokens.revoke(
            "hash::old",
            revoked_at=_NOW - dt.timedelta(seconds=5),
            reason=REVOKED_ROTATED,
        )
        await uow.commit()
    return user.id


_UseCase = Callable[[SqlAlchemyUnitOfWork], Coroutine[Any, Any, object]]


async def _interleave(
    engine: AsyncEngine, paused: _UseCase, competitor: _UseCase
) -> tuple[object, object]:
    """Pause ``paused`` before its write, settle ``competitor``, then resume.

    Returns each use case's outcome (its result or the exception it raised),
    paused one first.
    """

    factory = create_session_factory(engine)
    pause = _Pause()
    first = asyncio.create_task(paused(_PausingUnitOfWork(factory, pause)))
    await asyncio.wait_for(pause.reached.wait(), _SETTLE_TIMEOUT)

    backend = _Backend()
    second = asyncio.create_task(competitor(_TrackedUnitOfWork(factory, backend)))
    await _await_settled(engine, second, backend)

    pause.resume.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), _SETTLE_TIMEOUT
    )
    # A refresh refused because its session ended is the only acceptable
    # failure: anything else (a deadlock, a serialization error) is a bug.
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(
            outcome, InvalidRefreshTokenError
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
    # The logout has read the presented token and is about to revoke its chain
    # when the refresh starts. Whether the refresh is refused or commits first,
    # the session ends with no live token.
    user = await _seed(engine)

    async def logout(uow: SqlAlchemyUnitOfWork) -> None:
        await _logout(uow)(refresh_token=logged_out)

    async def rotate(uow: SqlAlchemyUnitOfWork) -> object:
        return await _refresh(uow)(refresh_token=refreshed)

    logout_outcome, _ = await _interleave(engine, logout, rotate)

    assert logout_outcome is None
    assert await _active_secrets(engine, user) <= {_OTHER_DEVICE}
