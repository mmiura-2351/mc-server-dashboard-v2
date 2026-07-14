"""Async-SQLAlchemy implementation of the ``GameSessionRepository`` Port.

Works on an ``AsyncSession`` owned by the enclosing ``UnitOfWork``; it runs reads
and the retention prune but never commits — commit is the unit of work's job
(DATABASE.md Section 1). Rows are translated to the framework-free domain entity
here. Ingestion (start/end/orphan-healing) lives in a separate adapter behind the
fleet ``SessionSink`` Port.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mc_server_dashboard_api.servers.adapters.game_session_models import (
    GameSessionModel,
)
from mc_server_dashboard_api.servers.domain.game_session import (
    GameSession,
    GameSessionSource,
)
from mc_server_dashboard_api.servers.domain.game_session_repository import (
    GameSessionRepository,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def _to_game_session(row: GameSessionModel) -> GameSession:
    return GameSession(
        id=row.id,
        server_id=None if row.server_id is None else ServerId(row.server_id),
        hostname=row.hostname,
        # The PostgreSQL INET column round-trips as an ``ipaddress`` object; the
        # framework-free domain holds the plain string form.
        player_ip=None if row.player_ip is None else str(row.player_ip),
        username=row.username,
        player_uuid=row.player_uuid,
        started_at=row.started_at,
        ended_at=row.ended_at,
        # A NULL source column is a legacy row (recorded before the
        # discriminator existed); it reads back as UNSPECIFIED (issue #1912).
        source=(
            GameSessionSource.UNSPECIFIED
            if row.source is None
            else GameSessionSource(row.source)
        ),
    )


class SqlAlchemyGameSessionRepository(GameSessionRepository):
    """:class:`GameSessionRepository` adapter over an ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_server(
        self, server_id: ServerId, *, limit: int, offset: int
    ) -> list[GameSession]:
        rows = (
            (
                await self._session.execute(
                    select(GameSessionModel)
                    .where(GameSessionModel.server_id == server_id.value)
                    .order_by(
                        GameSessionModel.started_at.desc(),
                        GameSessionModel.id.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return [_to_game_session(row) for row in rows]

    async def delete_started_before(self, cutoff: dt.datetime) -> int:
        # Prune normal rows by started_at, plus end-only placeholders (start lost
        # to the relay's drop-oldest cap, or the server deleted before a late
        # start) by their ended_at so they don't live forever (issue #957).
        result = await self._session.execute(
            delete(GameSessionModel).where(
                or_(
                    GameSessionModel.started_at < cutoff,
                    and_(
                        GameSessionModel.started_at.is_(None),
                        GameSessionModel.ended_at < cutoff,
                    ),
                )
            )
        )
        return cast("CursorResult[Any]", result).rowcount
