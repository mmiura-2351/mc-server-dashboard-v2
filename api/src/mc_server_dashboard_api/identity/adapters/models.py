"""SQLAlchemy ORM models for the identity tables (DATABASE.md Section 4).

These map the ``user`` and ``refresh_token`` tables. They are an adapter detail:
the domain entities (``domain.entities``) are framework-free, and these models
are translated to/from them in the repositories.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class UserModel(Base):
    """Row of the ``user`` table: a global identity (DATABASE.md Section 4)."""

    __tablename__ = "user"
    __table_args__ = (
        # Case-insensitive username uniqueness (DATABASE.md Section 4).
        Index("uq_user_username_lower", text("lower(username)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.false()
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class RefreshTokenModel(Base):
    """Row of the ``refresh_token`` table (DATABASE.md Section 4)."""

    __tablename__ = "refresh_token"
    __table_args__ = (
        # "Revoke all sessions" for a user (DATABASE.md Section 4).
        Index("ix_refresh_token_user_id", "user_id"),
        # Expiry sweeps over still-live tokens (DATABASE.md Section 4).
        Index(
            "ix_refresh_token_expires_at",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
