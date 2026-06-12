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

from asyncpg import DataError as AsyncpgDataError
from sqlalchemy import CursorResult, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.domain.session_sink import SessionSink, SessionStart
from mc_server_dashboard_api.servers.adapters.game_session_models import (
    GameSessionModel,
)

_LOG = logging.getLogger(__name__)

# Protocol-derived caps so a malformed event cannot blow past the column or
# wedge the batch: the Minecraft username max is 16 chars and a DNS name max is
# 253; we truncate defensively at the seam rather than reject the whole batch.
_USERNAME_MAX = 16
_HOSTNAME_MAX = 253


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


def _is_poison_value(exc: DBAPIError) -> bool:
    """Whether ``exc`` is a per-event data defect to drop, not a transient fault.

    A malformed ``player_ip`` fails asyncpg's client-side INET coercion, which
    SQLAlchemy surfaces as a generic :class:`DBAPIError` wrapping an asyncpg
    ``DataError`` (it is *not* mapped to ``sqlalchemy.exc.DataError``). We unwrap
    to confirm that root cause so connection/operational ``DBAPIError``s still
    propagate (the relay's at-least-once retry is correct for those).
    """

    cause = exc.orig.__cause__ if exc.orig is not None else None
    return isinstance(cause, AsyncpgDataError)


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
            "hostname": _truncate(start.hostname, _HOSTNAME_MAX),
            "player_ip": start.player_ip,
            "username": _truncate(start.username, _USERNAME_MAX),
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
        # Poison-event isolation (issue #957): a start for a deleted server (FK
        # violation -> IntegrityError) or a malformed player_ip (INET cast ->
        # DataError) must not wedge the whole ReportSessions batch. Each event has
        # its own transaction (own session here), so we drop the bad event with a
        # WARN and let the relay keep advancing; connection/operational errors
        # still propagate so the relay's at-least-once retry kicks in.
        async with self._session_factory() as session:
            try:
                await session.execute(stmt)
                await session.commit()
            except IntegrityError:
                # FK violation: the start references a server that no longer
                # exists (honors the "unknown server_id is dropped" promise).
                await session.rollback()
                self._log_dropped_start(session_id, server_id)
            except DBAPIError as exc:
                if not _is_poison_value(exc):
                    raise
                # Malformed player_ip (asyncpg INET DataError): drop this event.
                await session.rollback()
                self._log_dropped_start(session_id, server_id)

    @staticmethod
    def _log_dropped_start(session_id: uuid.UUID, server_id: uuid.UUID) -> None:
        _LOG.warning(
            "ReportSessions start is unpersistable; dropping",
            extra={"session_id": str(session_id), "server_id": str(server_id)},
        )

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
