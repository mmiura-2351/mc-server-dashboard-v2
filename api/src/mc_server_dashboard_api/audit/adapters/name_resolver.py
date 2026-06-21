"""Async-SQLAlchemy adapter for the :class:`NameResolver` Port (issue #682).

Resolves the audit page's soft-referenced ids to display names against the live
``user``/``server``/``community`` tables. Read-only: opens its own session and
never writes, mirroring :class:`SqlAlchemyAuditQuery`. Each lookup is a single
batched ``WHERE id IN (...)`` over the distinct ids — never per row. An id with no
matching row (the subject was deleted) is simply absent from the returned map.

Crossing into the other contexts' ORM models is an *adapter* detail (the audit
domain stays unaware of identity/servers/community, DATABASE.md Section 9), the
same edge-only cross-context pattern as ``IdentityUserDirectory``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.audit.domain.name_resolver import NameResolver
from mc_server_dashboard_api.community.adapters.models import CommunityModel
from mc_server_dashboard_api.identity.adapters.models import UserModel
from mc_server_dashboard_api.servers.adapters.models import ServerModel


class SqlAlchemyNameResolver(NameResolver):
    """:class:`NameResolver` adapter over an async-SQLAlchemy session factory."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve_usernames(
        self, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not user_ids:
            return {}
        stmt = select(UserModel.id, UserModel.username).where(
            UserModel.id.in_(user_ids)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return {row.id: row.username for row in rows}

    async def resolve_server_names(
        self, server_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not server_ids:
            return {}
        stmt = select(ServerModel.id, ServerModel.name).where(
            ServerModel.id.in_(server_ids)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return {row.id: row.name for row in rows}

    async def resolve_community_names(
        self, community_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not community_ids:
            return {}
        stmt = select(CommunityModel.id, CommunityModel.name).where(
            CommunityModel.id.in_(community_ids)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return {row.id: row.name for row in rows}
