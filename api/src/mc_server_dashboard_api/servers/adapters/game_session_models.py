"""SQLAlchemy ORM model for the ``game_session`` table (RELAY.md Section 14).

Maps the ``game_session`` table: one row per accepted login session recorded by
the relay (issue #957). The ``id`` is the relay-minted UUID (the idempotency key
for ``ReportSessions`` upserts). ``player_ip`` is a PostgreSQL ``INET``. The
``server_id`` column FKs ``server`` (``ON DELETE CASCADE``) and is indexed with
``started_at`` for the newest-first listing. ``username`` / ``player_uuid`` are
the values *claimed* in Login Start and may be ``NULL``; ``ended_at`` is ``NULL``
while the session is open.

``server_id`` / ``hostname`` / ``player_ip`` / ``started_at`` are nullable: a
``SessionEnd`` that arrives before its ``SessionStart`` (an out-of-order batch
retry) creates a placeholder row carrying only ``id`` + ``ended_at`` until the
start fills the rest in (RELAY.md Section 6 idempotency). In steady state every
row has them; the nullability exists only to make the end-before-start upsert
representable. The ``server_id`` FK still cascades for the populated rows.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class GameSessionModel(Base):
    """Row of the ``game_session`` table (RELAY.md Section 14)."""

    __tablename__ = "game_session"
    __table_args__ = (
        # List a server's sessions newest-first (the ``session:read`` listing).
        Index("ix_game_session_server_id_started_at", "server_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("server.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Nullable to make the end-before-start placeholder representable; populated
    # by record_start in steady state (see the module docstring).
    hostname: Mapped[str | None] = mapped_column(String, nullable=True)
    player_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    player_uuid: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
