"""SQLAlchemy ORM model for the fleet context (DATABASE.md Section 7).

The fleet registry is in memory and has no ``worker`` table; ``worker_drain`` is
the one durable fleet record: which Worker ids an operator has drained, so the
intent survives an API restart (issue #3535). ``worker_id`` carries no FK, since
there is no ``worker`` table to reference.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class WorkerDrainModel(Base):
    """Row of the ``worker_drain`` table (DATABASE.md Section 7, issue #3535)."""

    __tablename__ = "worker_drain"

    worker_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    drained_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
