"""Servers-backed adapter for the fleet :class:`SessionSink` Port (issue #957).

The relay's ``ReportSessions`` / ``Register`` (fleet adapters, RELAY.md Section 6)
write game-session records but must not reach into the servers domain
(import-linter); this edge module fulfils the fleet-domain Port against the
``game_session`` table, opening its own transaction per call from the injected
session factory (the RelayService servicer has no request-scoped UnitOfWork —
same shape as the state sink and the route resolver).

All three operations are idempotent so the relay's at-least-once retries and
crash-recovery re-registrations are safe:

- ``record_start`` is an INSERT … ON CONFLICT (id) that fills in the claimed
  fields without clearing a recorded ``ended_at`` (so it reconciles an
  end-before-start placeholder), but never overwrites an already-recorded start.
- ``record_end`` is an INSERT (placeholder) … ON CONFLICT (id) that sets
  ``ended_at`` only when it is still NULL, so a duplicate end is a no-op.
- ``close_absent`` sets ``ended_at`` on every open row absent from the active set.

This is an adapter-layer composition across contexts: a fleet Port implemented
with the servers ``game_session`` table. The servers domain/application never
reach into fleet (import-linter); only this edge module bridges the two.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.domain.session_sink import SessionSink, SessionStart
from mc_server_dashboard_api.servers.adapters.game_session_models import (
    GameSessionModel,
)

_LOG = logging.getLogger(__name__)


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class ServersSessionSink(SessionSink):
    """:class:`SessionSink` adapter writing the ``game_session`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_start(self, start: SessionStart) -> None:
        session_id = _parse_uuid(start.session_id)
        server_id = _parse_uuid(start.server_id)
        if session_id is None or server_id is None:
            # The relay mints UUID ids and carries the API-issued server id; a
            # non-UUID is an invariant violation at the seam. Drop it loudly rather
            # than crashing the batch.
            _LOG.error(
                "ReportSessions start has a non-UUID id; dropping",
                extra={"session_id": start.session_id, "server_id": start.server_id},
            )
            return
        values = {
            "id": session_id,
            "server_id": server_id,
            "hostname": start.hostname,
            "player_ip": start.player_ip,
            "username": start.username,
            "player_uuid": _parse_uuid(start.player_uuid),
            "started_at": start.started_at,
            "ended_at": None,
        }
        stmt = pg_insert(GameSessionModel).values(**values)
        # On a pre-existing row, fill in the start fields but keep any recorded
        # ended_at (end-before-start reconciliation) and never overwrite an
        # already-recorded started_at (duplicate start is a no-op).
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "server_id": stmt.excluded.server_id,
                "hostname": stmt.excluded.hostname,
                "player_ip": stmt.excluded.player_ip,
                "username": stmt.excluded.username,
                "player_uuid": stmt.excluded.player_uuid,
                "started_at": stmt.excluded.started_at,
            },
            where=GameSessionModel.started_at.is_(None),
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def record_end(self, *, session_id: str, ended_at: dt.datetime) -> None:
        parsed = _parse_uuid(session_id)
        if parsed is None:
            _LOG.error(
                "ReportSessions end has a non-UUID id; dropping",
                extra={"session_id": session_id},
            )
            return
        # Insert a placeholder if the start has not arrived; on conflict set
        # ended_at only while still NULL so a duplicate end is a no-op.
        stmt = pg_insert(GameSessionModel).values(id=parsed, ended_at=ended_at)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"ended_at": stmt.excluded.ended_at},
            where=GameSessionModel.ended_at.is_(None),
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def close_absent(
        self, *, active_session_ids: Sequence[str], ended_at: dt.datetime
    ) -> int:
        active = [parsed for parsed in map(_parse_uuid, active_session_ids) if parsed]
        stmt = update(GameSessionModel).where(GameSessionModel.ended_at.is_(None))
        if active:
            stmt = stmt.where(GameSessionModel.id.not_in(active))
        stmt = stmt.values(ended_at=ended_at)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
        return cast("CursorResult[Any]", result).rowcount
