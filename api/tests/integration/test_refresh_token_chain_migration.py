"""Round-trip for the 0040 ``refresh_token.chain_id`` migration (issue #3249).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- upgrade puts each user's pre-upgrade tokens into one shared legacy chain --
  the schema records no rotation lineage, so a rotated predecessor and its
  successor cannot be told apart from two sign-ins -- distinct per user, and
  makes the column mandatory;
- a rotation spanning the upgrade is covered: logging out with the rotated
  predecessor kills its live successor;
- downgrade drops the column again.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.application.refresh_session import (
    RefreshSession,
)
from mc_server_dashboard_api.identity.application.restore_session import (
    RestoreSession,
)
from mc_server_dashboard_api.identity.domain.errors import InvalidRefreshTokenError
from tests.identity.fakes import FakeClock, FakeTokenService
from tests.integration.migrate import downgrade_base, downgrade_to, upgrade_to

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_PRE_MIGRATION = "0039_resource_grant_parent_fks"
_MIGRATION = "0040_refresh_token_chain"


async def _insert_user(conn: AsyncConnection, username: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text(
            'INSERT INTO "user" '
            "(id, username, email, password_hash, is_platform_admin, "
            "created_at, updated_at) VALUES "
            "(:id, :username, :email, 'h', false, now(), now())"
        ),
        {"id": user_id, "username": username, "email": f"{username}@e.com"},
    )
    return user_id


async def _insert_token(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    token_hash: str,
    *,
    rotated: bool = False,
) -> uuid.UUID:
    token_id = uuid.uuid4()
    revoked = "now() - interval '5 seconds'" if rotated else "NULL"
    reason = "'rotated'" if rotated else "NULL"
    await conn.execute(
        text(
            "INSERT INTO refresh_token "
            "(id, user_id, token_hash, issued_at, expires_at, revoked_at, "
            f"revoked_reason) VALUES (:id, :uid, :hash, now(), "
            f"now() + interval '14 days', {revoked}, {reason})"
        ),
        {"id": token_id, "uid": user_id, "hash": token_hash},
    )
    return token_id


async def test_upgrade_gives_each_users_tokens_one_legacy_chain() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_PRE_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            alice = await _insert_user(conn, "alice")
            bob = await _insert_user(conn, "bob")
            alice_first = await _insert_token(conn, alice, "alice-first")
            alice_second = await _insert_token(conn, alice, "alice-second")
            bobs = await _insert_token(conn, bob, "bobs")

        await upgrade_to(_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT id, chain_id FROM refresh_token"))
            chains = {row[0]: row[1] for row in rows}
        assert chains[alice_first] == chains[alice_second]
        assert chains[bobs] != chains[alice_first]
        # A token without a chain is rejected.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await _insert_token(conn, alice, "chainless")

        await downgrade_to(_PRE_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            columns = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'refresh_token'"
                )
            )
            assert "chain_id" not in {row[0] for row in columns}
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


async def test_logout_with_a_pre_upgrade_predecessor_kills_its_successor() -> None:
    # A rotation committed before the upgrade, its response still in flight:
    # the browser logs out after the upgrade with the rotated predecessor. The
    # successor the late response delivers must be dead, for restore (reload)
    # and refresh alike.
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_PRE_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            alice = await _insert_user(conn, "alice")
            await _insert_token(conn, alice, "hash::predecessor", rotated=True)
            await _insert_token(conn, alice, "hash::successor")

        await upgrade_to(_MIGRATION, _DB_URL)

        factory = create_session_factory(engine)
        clock = FakeClock(dt.datetime.now(tz=dt.timezone.utc))
        await Logout(
            uow=SqlAlchemyUnitOfWork(factory), tokens=FakeTokenService(), clock=clock
        )(refresh_token="predecessor")

        with pytest.raises(InvalidRefreshTokenError):
            await RestoreSession(
                uow=SqlAlchemyUnitOfWork(factory),
                tokens=FakeTokenService(),
                clock=clock,
            )(refresh_token="successor")
        with pytest.raises(InvalidRefreshTokenError):
            await RefreshSession(
                uow=SqlAlchemyUnitOfWork(factory),
                tokens=FakeTokenService(),
                clock=clock,
                refresh_ttl=dt.timedelta(days=14),
                reuse_grace=dt.timedelta(seconds=60),
            )(refresh_token="successor")
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
