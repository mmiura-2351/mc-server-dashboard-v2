"""Integration tests for the audit :class:`SqlAlchemyNameResolver` on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The real migrations create the
``user``/``server``/``community`` tables the resolver reads, so it runs against
the documented shape (DATABASE.md Sections 4, 5, 7). Verifies the batched
``WHERE id IN (...)`` lookups return display names and that a deleted/absent
subject simply drops out of the returned map (the read-time enrichment fallback,
issue #682).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.audit.adapters.name_resolver import SqlAlchemyNameResolver
from mc_server_dashboard_api.community.adapters.models import CommunityModel
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.models import UserModel
from mc_server_dashboard_api.servers.adapters.models import ServerModel
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


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


def _community(community_id: uuid.UUID, name: str) -> CommunityModel:
    return CommunityModel(id=community_id, name=name, created_at=_T0, updated_at=_T0)


def _user(user_id: uuid.UUID, username: str) -> UserModel:
    return UserModel(
        id=user_id,
        username=username,
        email=f"{username}@example.test",
        password_hash="x",
        created_at=_T0,
        updated_at=_T0,
    )


def _server(server_id: uuid.UUID, community_id: uuid.UUID, name: str) -> ServerModel:
    return ServerModel(
        id=server_id,
        community_id=community_id,
        name=name,
        mc_edition="java",
        mc_version="1.21",
        server_type="vanilla",
        execution_backend="container",
        config={},
        desired_state="stopped",
        observed_state="stopped",
        created_at=_T0,
        updated_at=_T0,
        slug=f"slug-{server_id}",
    )


async def test_resolves_names_and_drops_absent_ids(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    community_id = uuid.uuid4()
    user_id = uuid.uuid4()
    server_id = uuid.uuid4()
    async with factory() as session:
        session.add(_community(community_id, "Acme"))
        session.add(_user(user_id, "alice"))
        session.add(_server(server_id, community_id, "survival"))
        await session.commit()

    resolver = SqlAlchemyNameResolver(factory)
    missing = uuid.uuid4()

    # Each lookup is one batched IN query; an unknown id is absent from the map.
    usernames = await resolver.resolve_usernames([user_id, missing])
    assert usernames == {user_id: "alice"}

    server_names = await resolver.resolve_server_names([server_id, missing])
    assert server_names == {server_id: "survival"}

    community_names = await resolver.resolve_community_names([community_id, missing])
    assert community_names == {community_id: "Acme"}


async def test_empty_id_lists_resolve_to_empty_maps(engine: AsyncEngine) -> None:
    resolver = SqlAlchemyNameResolver(create_session_factory(engine))

    assert await resolver.resolve_usernames([]) == {}
    assert await resolver.resolve_server_names([]) == {}
    assert await resolver.resolve_community_names([]) == {}
