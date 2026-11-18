"""The database's installation id (DATABASE.md Section 1.1, issue #3870).

One row, minted by the 0045 migration and never changed afterwards. The store
owner marker records it, so an API can tell whether the bucket (or fs root) it is
configured with belongs to its own database before a GC diffs one against the
other (STORAGE.md Section 4.3).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class InstallationModel(Base):
    """The one row of the ``installation`` table."""

    __tablename__ = "installation"
    # ``singleton`` is the primary key and may only be true, so the table holds at
    # most one row.
    __table_args__ = (CheckConstraint("singleton", name="ck_installation_singleton"),)

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


async def read_installation_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Return this database's installation id.

    Raises when the row is missing, which only a schema not built by the
    migrations can be.
    """

    async with session_factory() as session:
        return (await session.execute(select(InstallationModel.id))).scalar_one()
